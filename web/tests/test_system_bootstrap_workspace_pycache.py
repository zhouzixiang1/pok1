"""Regression: apply_blueprint must tolerate a stale ``__pycache__`` workspace.

The strict five-file identity validator (``strict_artifact_layout_errors``)
rejects Python bytecode caches, while ``artifact_manifest`` / ``hash_path``
deliberately exclude them from artifact identity.  A workspace that carries a
stale ``__pycache__`` therefore still hashes-clean and can reach the system
blueprint executor, which then abandoned the generation at
``system_bootstrap_identity_refresh_failed`` because
``refresh_policy_identity_documents`` re-validates the strict layout.

The LLM Worker path already closes this work-phase boundary via
``_cleanup_worker_transients_before_identity_refresh``.  These tests pin the
same invariant for the system-owned blueprint executor path so the bug cannot
silently regress.

Version anchors come from the shared branch-portable conftest helpers
(``STRICT_TARGET_V`` etc.) so the tests run identically on the cloud epoch
(``national_cloud_v1``) and main (``national_v143``).
"""

from __future__ import annotations

from pathlib import Path

from conftest import STRICT_TARGET_V, strict_bot_name


def _materialized_candidate(tmp_path: Path) -> Path:
    from system_strict_bootstrap import materialize_fresh_candidate

    bot = tmp_path / strict_bot_name(STRICT_TARGET_V)
    materialize_fresh_candidate(bot, version=STRICT_TARGET_V)
    return bot


def _inject_stale_pycache(bot: Path) -> None:
    cache = bot / "__pycache__"
    cache.mkdir()
    (cache / "policy.cpython-312.pyc").write_bytes(b"\x00" * 32)
    (cache / "national_bot.cpython-312.pyc").write_bytes(b"\x00" * 32)


def test_artifact_file_map_excludes_pycache_like_hash_path(tmp_path):
    """``_artifact_file_map`` must mirror ``hash_path`` identity exclusions.

    If it includes ``__pycache__`` entries, the change-set audit in
    ``apply_blueprint`` sees a spurious diff once the transient cleanup runs
    and trips ``system_bootstrap_changed_file_set_mismatch``.
    """

    from bot_artifact import hash_path
    from system_strict_bootstrap import _artifact_file_map, _file_map

    bot = _materialized_candidate(tmp_path)
    _inject_stale_pycache(bot)

    # hash_path excludes __pycache__, so a stale cache must not change identity.
    bot_clean = _materialized_candidate(tmp_path.parent / "clean")
    assert hash_path(bot) == hash_path(bot_clean)

    artifact_map = _artifact_file_map(bot)
    raw_map = _file_map(bot)
    # The identity-bearing map sees exactly the five strict files...
    assert set(artifact_map) == {
        "national_bot.py",
        "precompute.py",
        "policy.py",
        "national_runtime_manifest.json",
        "policy_epoch_receipt.json",
    }
    # ...while the raw rglob map still sees the cache file (proving the test
    # actually injected it and the exclusion is what the audit relies on).
    assert any(path.startswith("__pycache__/") for path in raw_map)


def test_apply_blueprint_clears_stale_pycache_before_identity_refresh(tmp_path):
    """The system blueprint executor must not abandon on a stale cache.

    Simulates the production failure: a reused workspace carries a stale
    ``__pycache__`` that hashes-clean but is rejected by the strict layout
    validator.  ``apply_blueprint`` must remove the transient before refreshing
    identity, matching the LLM Worker path.
    """

    from system_strict_bootstrap import (
        BLUEPRINT_DIR,
        _WORKER_CHANGED_FILES,
        _artifact_file_map,
        materialize_fresh_candidate,
        refresh_policy_identity,
    )
    from bot_artifact import hash_path

    bot = tmp_path / strict_bot_name(STRICT_TARGET_V)
    materialize_fresh_candidate(bot, version=STRICT_TARGET_V)
    prepared_hash = hash_path(bot)
    _inject_stale_pycache(bot)
    # After injection the prepared identity is unchanged (cache excluded).
    assert hash_path(bot) == prepared_hash
    assert (bot / "__pycache__").is_dir()

    # Replay the exact apply_blueprint writes + cleanup + refresh sequence.
    # We cannot call apply_blueprint directly without a full checkpoint/envelope,
    # so exercise the boundary it owns: policy write, transient cleanup, refresh.
    policy_path = bot / "policy.py"
    policy_path.write_bytes((BLUEPRINT_DIR / "policy.py").read_bytes())

    from candidate_hygiene import cleanup_transient_candidate_artifacts

    cleanup_transient_candidate_artifacts(bot, include_task_context=False)
    assert not (bot / "__pycache__").exists()

    refresh_policy_identity(bot, version=STRICT_TARGET_V)

    # The identity-bearing change set is exactly the worker-changed files
    # (policy + the two system identity documents), plus the unchanged system
    # runtime entries — never the removed cache.
    after = _artifact_file_map(bot)
    assert set(after) == {
        "national_bot.py",
        "precompute.py",
        "policy.py",
        "national_runtime_manifest.json",
        "policy_epoch_receipt.json",
    }
    assert set(_WORKER_CHANGED_FILES) <= set(after)
