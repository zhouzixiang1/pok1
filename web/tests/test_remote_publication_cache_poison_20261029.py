"""Remote-publication proof: a provider error must never poison the cache.

2026-10-29 daemon-crash root cause: a `git ls-remote` timeout armed the
exception path, which cached an EMPTY verified set for the full TTL; the
rating daemon's active-pool refresh intersected local tags with that poisoned
cache, produced zero bots, and the frozen pick_matches divided by the empty
pool (29 ZeroDivision crashes matching 29 timeouts in the log history).
Contract now: the error path returns the last successful proof within a
stale-grace window, and an error NEVER writes the cache entry.
"""

from __future__ import annotations

import inspect
import time

import pytest

import evolution_infra as ei
import evolution_infra_remote_publication as rpub
from bot_namespace import EVOLUTION_BRANCH, bot_tag, high_water_tag

_OID = "0" * 40


def _fake_git_success(*args, check=True):
    """git stand-in where tags v632/v636 are fully published on origin."""
    if args[0] == "rev-parse":
        return _OID
    if args[0] == "cat-file":
        return "tag"
    if args[0] == "ls-remote":
        rows = []
        for v in (632, 636):
            rows.append(f"{_OID}\trefs/tags/{bot_tag(v)}")
            rows.append(f"{_OID}\trefs/tags/{bot_tag(v)}^{{}}")
            rows.append(f"{_OID}\trefs/tags/{high_water_tag(v)}")
            rows.append(f"{_OID}\trefs/tags/{high_water_tag(v)}^{{}}")
        rows.append(f"{_OID}\trefs/heads/{EVOLUTION_BRANCH}")
        return "\n".join(rows)
    if args[0] == "fetch":
        return ""
    raise AssertionError(f"unexpected git call {args[:1]}")


def _fake_git_timeout(*args, check=True):
    if args[0] == "ls-remote":
        raise RuntimeError("git ls-remote: timed out after 120s")
    return _fake_git_success(*args, check=check)


@pytest.fixture
def clean_cache(monkeypatch):
    rpub._clear_remote_publication_cache()
    monkeypatch.setattr(ei, "_git", _fake_git_success)
    monkeypatch.setattr(ei, "_git_command_succeeds", lambda *a: True)
    yield
    rpub._clear_remote_publication_cache()


def _prime_last_good_proof() -> None:
    # a real successful proof arms the cache for this exact key
    result = rpub._remote_published_completion_versions({632, 636})
    assert result == {632, 636}


def test_error_path_serves_last_good_and_never_writes_cache(clean_cache, monkeypatch):
    _prime_last_good_proof()
    before = dict(rpub._REMOTE_PUBLICATION_CACHE)
    monkeypatch.setattr(ei, "_git", _fake_git_timeout)
    # force a cache refresh (expire the TTL) but keep the proof inside the
    # stale-grace window
    with rpub._REMOTE_PUBLICATION_CACHE_CONDITION:
        rpub._REMOTE_PUBLICATION_CACHE["checked_at"] = (
            time.monotonic() - float(ei._REMOTE_PUBLICATION_CACHE_TTL_SEC) - 5
        )
    result = rpub._remote_published_completion_versions({632, 636})
    assert result == {632, 636}, "provider error must serve last good proof"
    # error path must not rewrite the cache entry
    assert rpub._REMOTE_PUBLICATION_CACHE["key"] == before["key"]
    assert rpub._REMOTE_PUBLICATION_CACHE["versions"] == before["versions"]


def test_error_path_beyond_grace_returns_empty_without_poisoning(clean_cache, monkeypatch):
    _prime_last_good_proof()
    before = dict(rpub._REMOTE_PUBLICATION_CACHE)
    monkeypatch.setattr(ei, "_git", _fake_git_timeout)
    with rpub._REMOTE_PUBLICATION_CACHE_CONDITION:
        rpub._REMOTE_PUBLICATION_CACHE["checked_at"] = (
            time.monotonic() - rpub._REMOTE_PUBLICATION_STALE_GRACE_SEC - 1
        )
    result = rpub._remote_published_completion_versions({632, 636})
    assert result == set(), "past the grace window the honest empty answer"
    assert rpub._REMOTE_PUBLICATION_CACHE["key"] == before["key"]
    assert rpub._REMOTE_PUBLICATION_CACHE["versions"] == before["versions"]


def test_no_last_good_proof_returns_empty(clean_cache, monkeypatch):
    monkeypatch.setattr(ei, "_git", _fake_git_timeout)
    assert rpub._remote_published_completion_versions({632, 636}) == set()


def test_stale_grace_constant_is_positive():
    assert rpub._REMOTE_PUBLICATION_STALE_GRACE_SEC > 0.0


def test_ls_remote_timeout_widened_in_git_publication():
    import evolution_infra_git_publication as gpub

    src = inspect.getsource(gpub._git)
    assert 'args[0] == "ls-remote"' in src and "120" in src
