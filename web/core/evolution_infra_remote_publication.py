"""Remote publication proof and active-pool sentinel helpers for evolution_infra.

Extracted as a cohesive business cluster from evolution_infra.py: the TTL'd,
single-flight remote publication proof cache, the local-then-remote tag/commit
verification, plus the active-bot sentinel restore and the in-flight
publication-version filter.

evolution_infra.py retains thin delegate shells so external
``from evolution_infra import <name>`` sites and
``monkeypatch.setattr(evolution_infra, "<name>", ...)`` patches keep resolving.

IMPORTANT -- shared-symbol access model
---------------------------------------
Many names referenced by these bodies remain in ``evolution_infra`` because
they are part of that module's monkeypatch surface -- the test suite patches
``evolution_infra._git``, ``evolution_infra._git_command_succeeds``,
``evolution_infra._tagged_bot_versions``, ``evolution_infra.load_reaped_bot_versions``,
``evolution_infra._registry_may_be_virgin``,
``evolution_infra.is_active_bot_protocol_eligible``,
``evolution_infra.read_pipeline_checkpoint``,
``evolution_infra.BOTS_DIR``,
``evolution_infra._REMOTE_PUBLICATION_CACHE_TTL_SEC`` and reads them back
through the remote-publication code paths.  Binding them at import time would
freeze the pre-patch value and silently break those tests.

Every such reference in this file is written ``_ei.<name>`` so it resolves
against the live module attribute at call time.  This includes references
between the moved bodies (``_clear_remote_publication_cache`` mutating the
cache consumed by ``_remote_published_completion_versions``): those are kept
as bare globals because they live in *this* module, exactly as they did
inline.  ``_ei._REMOTE_PUBLICATION_CACHE_TTL_SEC`` stays in evolution_infra
(it is asserted after ``importlib.reload(evolution_infra)``) and is therefore
read through ``_ei._REMOTE_PUBLICATION_CACHE_TTL_SEC``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

import evolution_infra as _ei  # for _ei._git, _ei._git_command_succeeds,
                               # _ei._tagged_bot_versions, _ei.load_reaped_bot_versions,
                               # _ei._registry_may_be_virgin,
                               # _ei.is_active_bot_protocol_eligible,
                               # _ei.read_pipeline_checkpoint, _ei.BOTS_DIR,
                               # _ei._REMOTE_PUBLICATION_CACHE_TTL_SEC, log, and
                               # the thin delegate shells that pick up test
                               # monkeypatches.

# Immutable constants re-exported by evolution_infra.  Imported here directly
# so the moved bodies can keep referencing them as bare globals.
from bot_namespace import (
    ACTIVE_TAG_PREFIX,
    HIGH_WATER_TAG_PREFIX,
    EVOLUTION_BRANCH,
    bot_name,
    bot_tag,
    bot_tag_glob,
    high_water_tag,
    parse_tag_version,
    parse_bot_version,
)

log = logging.getLogger("pok.infra")


_REMOTE_PUBLICATION_CACHE_LOCK = threading.RLock()
_REMOTE_PUBLICATION_CACHE_CONDITION = threading.Condition(
    _REMOTE_PUBLICATION_CACHE_LOCK
)
_REMOTE_PUBLICATION_CACHE = {
    "key": None,
    "checked_at": 0.0,
    "versions": frozenset(),
    "generation": 0,
    "inflight_key": None,
    "inflight_generation": None,
}


def _clear_remote_publication_cache():
    with _REMOTE_PUBLICATION_CACHE_CONDITION:
        _REMOTE_PUBLICATION_CACHE.update({
            "key": None,
            "checked_at": 0.0,
            "versions": frozenset(),
            "generation": int(
                _REMOTE_PUBLICATION_CACHE.get("generation") or 0
            ) + 1,
        })
        _REMOTE_PUBLICATION_CACHE_CONDITION.notify_all()



def _remote_published_completion_versions(tag_versions) -> set[int]:
    """Return versions whose exact completion/high-water refs are on origin.

    In the long-running evolution checkout a local annotated tag is only a
    recoverable intermediate state.  It must not restore ``.completed`` or
    enter the active pool until origin independently exposes both annotated
    refs and its main branch contains the peeled publication commit.
    """

    versions = tuple(sorted({int(item) for item in tag_versions}))
    if not versions:
        return set()
    local_rows = []
    for version in versions:
        completion = bot_tag(version)
        high_water = high_water_tag(version)
        local_rows.append((
            version,
            _ei._git("rev-parse", f"refs/tags/{completion}", check=False).strip(),
            _ei._git(
                "rev-parse",
                f"refs/tags/{completion}^{{commit}}",
                check=False,
            ).strip(),
            _ei._git("rev-parse", f"refs/tags/{high_water}", check=False).strip(),
            _ei._git(
                "rev-parse",
                f"refs/tags/{high_water}^{{commit}}",
                check=False,
            ).strip(),
        ))
    cache_key = tuple(local_rows)
    # A Dashboard can ask for status, health, evolution state and strength at
    # the same time.  Once the proof cache expires those callers must share
    # one remote transaction; otherwise each observer launches its own
    # ``git ls-remote``/fetch and a slow origin amplifies into an ASGI
    # outage.  Mutation/launch callers still wait for this exact fresh proof --
    # no stale remote result is accepted at an effect boundary.
    while True:
        now = time.monotonic()
        with _REMOTE_PUBLICATION_CACHE_CONDITION:
            if (
                _REMOTE_PUBLICATION_CACHE.get("key") == cache_key
                and now
                - float(_REMOTE_PUBLICATION_CACHE.get("checked_at") or 0.0)
                <= _ei._REMOTE_PUBLICATION_CACHE_TTL_SEC
            ):
                return set(_REMOTE_PUBLICATION_CACHE.get("versions") or ())
            inflight_key = _REMOTE_PUBLICATION_CACHE.get("inflight_key")
            if inflight_key is not None:
                _REMOTE_PUBLICATION_CACHE_CONDITION.wait()
                continue
            refresh_generation = int(
                _REMOTE_PUBLICATION_CACHE.get("generation") or 0
            )
            _REMOTE_PUBLICATION_CACHE["inflight_key"] = cache_key
            _REMOTE_PUBLICATION_CACHE["inflight_generation"] = (
                refresh_generation
            )
            break
    try:
        raw = _ei._git(
            "ls-remote",
            "origin",
            f"refs/heads/{EVOLUTION_BRANCH}",
            f"refs/tags/{ACTIVE_TAG_PREFIX}*",
            f"refs/tags/{HIGH_WATER_TAG_PREFIX}*",
        )
        remote: dict[str, str] = {}
        for line in raw.splitlines():
            oid, separator, ref = line.partition("\t")
            if separator and oid and ref:
                remote[ref] = oid
        remote_main = remote.get(f"refs/heads/{EVOLUTION_BRANCH}", "")
        if len(remote_main) != 40:
            raise RuntimeError("remote main ref is missing")
        current_remote_tracking = _ei._git(
            "rev-parse", f"refs/remotes/origin/{EVOLUTION_BRANCH}", check=False
        ).strip()
        if current_remote_tracking != remote_main:
            _ei._git(
                "fetch",
                "--no-tags",
                "origin",
                f"refs/heads/{EVOLUTION_BRANCH}:refs/remotes/origin/{EVOLUTION_BRANCH}",
            )
        verified: set[int] = set()
        for version, tag_object, commit_oid, water_object, water_commit in local_rows:
            completion = bot_tag(version)
            high_water = high_water_tag(version)
            if not all((tag_object, commit_oid, water_object, water_commit)):
                continue
            if _ei._git(
                "cat-file", "-t", f"refs/tags/{completion}", check=False
            ).strip() != "tag":
                continue
            if _ei._git(
                "cat-file", "-t", f"refs/tags/{high_water}", check=False
            ).strip() != "tag":
                continue
            if (
                remote.get(f"refs/tags/{completion}") != tag_object
                or remote.get(f"refs/tags/{completion}^{{}}") != commit_oid
                or remote.get(f"refs/tags/{high_water}") != water_object
                or remote.get(f"refs/tags/{high_water}^{{}}") != water_commit
                or water_commit != commit_oid
                or not _ei._git_command_succeeds(
                    "merge-base", "--is-ancestor", commit_oid, remote_main
                )
            ):
                continue
            verified.add(version)
    except Exception as exc:
        log.error(
            "Remote publication proof unavailable; active pool fails closed: %s",
            exc,
        )
        verified = set()
    with _REMOTE_PUBLICATION_CACHE_CONDITION:
        refresh_is_current = bool(
            _REMOTE_PUBLICATION_CACHE.get("generation")
            == refresh_generation
            and _REMOTE_PUBLICATION_CACHE.get("inflight_key") == cache_key
            and _REMOTE_PUBLICATION_CACHE.get("inflight_generation")
            == refresh_generation
        )
        if refresh_is_current:
            _REMOTE_PUBLICATION_CACHE.update({
                "key": cache_key,
                "checked_at": time.monotonic(),
                "versions": frozenset(verified),
            })
        _REMOTE_PUBLICATION_CACHE["inflight_key"] = None
        _REMOTE_PUBLICATION_CACHE["inflight_generation"] = None
        _REMOTE_PUBLICATION_CACHE_CONDITION.notify_all()
    # Cache invalidation is an authority movement.  A remote response which
    # began before that movement is not allowed to escape to its caller.
    return verified if refresh_is_current else set()



def _ensure_completed_sentinels_for_tagged_bots(tag_versions=None, reaped_versions=None):
    """Restore local .completed sentinels for bot dirs that already have tags.

    The sentinel is runtime metadata and may be absent in isolated clones because
    it is gitignored. The active-epoch tag remains the authoritative completion proof,
    so restoring the local sentinel keeps runtime active-bot discovery consistent
    without trusting untagged or abandoned directories. Intentionally reaped bots
    are skipped because they are tagged but no longer active.
    """
    if tag_versions is None:
        tag_versions = _ei._tagged_bot_versions()
    if reaped_versions is None:
        try:
            reaped_versions = _ei.load_reaped_bot_versions()
        except Exception as exc:
            if _ei._registry_may_be_virgin():
                # Fresh cloud bootstrap (no completion tags, no migration marker):
                # nothing has ever been reaped. Empty set is correct; restore can
                # still proceed because the early-return below handles empty
                # tag_versions. Suppress the operator-noise ERROR.
                reaped_versions = set()
            else:
                log.error("National reaped registry unavailable; refusing sentinel restore: %s", exc)
                return []
    if not tag_versions or not _ei.BOTS_DIR.exists():
        return []

    restored = []
    for version in sorted(tag_versions):
        if version in reaped_versions:
            continue
        bot_dir = _ei.BOTS_DIR / bot_name(version)
        sentinel = bot_dir / ".completed"
        if not bot_dir.is_dir() or sentinel.exists():
            continue
        if not _ei.is_active_bot_protocol_eligible(version):
            continue
        try:
            sentinel.write_text(f"restored from {bot_tag(version)} tag\n", encoding="utf-8")
            restored.append(version)
        except OSError as exc:
            log.warning("Failed to restore .completed sentinel for %s: %s", bot_name(version), exc)

    if restored:
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.completed_sentinel_restored",
                "warning",
                f"Restored .completed sentinels for tagged bots: {restored}",
                {"versions": restored},
            )
        except Exception:
            pass
    return restored



def _incomplete_checkpoint_publication_versions(tag_versions) -> set[int]:
    """Keep a locally tagged in-flight publication out of the active pool.

    A completion tag can exist before the publication transaction has proven
    its remote refs (when required), materialized the durable sentinel, and
    cleared the checkpoint.  In particular, local-only deployments must not
    let the generic tag-to-sentinel repair path skip those final transaction
    phases after a crash.  Once the exact intent-bound sentinel exists, the
    candidate may be observed as complete while the final checkpoint CAS is
    retried.
    """

    versions = {int(item) for item in (tag_versions or set())}
    if not versions:
        return set()
    try:
        checkpoint = _ei.read_pipeline_checkpoint()
    except Exception:
        return set()
    if not isinstance(checkpoint, dict) or checkpoint.get("stage") != "publishing":
        return set()
    try:
        version = int(checkpoint.get("next_v"))
    except (TypeError, ValueError):
        return set()
    if version not in versions:
        return set()

    intent = checkpoint.get("publication_intent")
    try:
        from publication_transaction import publication_intent_checkpoint_errors

        intent_errors = publication_intent_checkpoint_errors(intent, checkpoint)
    except Exception:
        intent_errors = ["publication_intent_validation_unavailable"]
    publication_id = (
        str(intent.get("publication_id") or "")
        if isinstance(intent, dict)
        else ""
    )
    sentinel = _ei.BOTS_DIR / bot_name(version) / ".completed"
    try:
        sentinel_matches = (
            bool(publication_id)
            and sentinel.is_file()
            and not sentinel.is_symlink()
            and sentinel.read_text(encoding="utf-8")
            == f"publication_id={publication_id}\n"
        )
    except OSError:
        sentinel_matches = False
    return {version} if intent_errors or not sentinel_matches else set()


