"""Completed-rotation adoption carve-out for the handoff (2026-10-28).

Production wedge (v632): ``archive_rotation`` completed, then the rating
daemon's evaluation-bundle crash-recovery rewrote ``match_history.jsonl``
back to the last committed cycle cutoff — a legitimate move of bytes BELOW
the frozen plan snapshot.  Every ``plan_handoff_step`` retry re-validates the
frozen set plan against live bytes, and the snapshot-prefix check failed with
``archive rotation source prefix changed: match_history.jsonl``, wedging the
handoff at ``archive_rotation: completed / housekeeping: planned`` and
blocking ``prepare_generation`` forever (bounded 8-attempt park, restart just
resets the counter).

Contract pinned here:

1. ``validate_archive_rotation_plan(..., require_live_source_proof=False)``
   waives ONLY the live snapshot-prefix re-proof and the cold-range recompute;
   the completed rotation's watermark / low-plan / archive digest chain is
   still re-proved against the live file (a tamper inside the watermark range
   or a truncated source still fails closed).
2. The default stays fail-closed: an execution path that sees a rewritten
   source still raises ``source prefix changed``.
3. ``plan_handoff_step`` passes ``require_live_source_proof=False`` exactly
   when the step row is already ``completed`` (adoption), and ``True``
   otherwise (pending/planned rows still get the full proof).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import evolution_infra
import evolution_infra_archive_rotation as rotation_module
import post_publication_handoff as handoff


_PUBLICATION_ID = "d" * 64
_VERSION = 632


def _seed_match_history(results: Path, *, cold_lines: int, keep_lines: int):
    """Write match_history.jsonl of exactly one full plan snapshot size.

    keep_lines must exceed the rotation rule's keep (500) so a cold range is
    provably present.
    """
    assert keep_lines > 500
    line = lambda i: json.dumps({"row": i}).encode() + b"\n"
    data = b"".join(line(i) for i in range(cold_lines + keep_lines))
    (results / "match_history.jsonl").write_bytes(data)
    return data


def _crash_recovery_rewrite(results: Path, original: bytes, watermark_end: int):
    """Mirror evaluation-bundle crash-recovery: truncate to the committed
    cycle cutoff (= the completed watermark end) and append NEW rows that
    differ from the bytes the frozen snapshot saw."""
    rewritten = original[:watermark_end] + b"".join(
        json.dumps({"row": 900000 + i}).encode() + b"\n" for i in range(400)
    )
    (results / "match_history.jsonl").write_bytes(rewritten)


@pytest.fixture
def rotation_env(tmp_path, monkeypatch):
    results = tmp_path / "results"
    archive = results / "archive"
    results.mkdir()
    archive.mkdir()
    for name, filename in (
        ("WORKER_FAILURES_FILE", "worker_failures.jsonl"),
        ("MATCH_HISTORY_FILE", "match_history.jsonl"),
        ("RATING_HISTORY_FILE", "rating_history.jsonl"),
        ("LLM_COSTS_FILE", "llm_costs.jsonl"),
    ):
        monkeypatch.setattr(evolution_infra, name, results / filename)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive)
    monkeypatch.setenv(
        rotation_module._ROTATION_BATCH_MAX_BYTES_ENV, str(256 * 1024 * 1024)
    )
    return results, archive


def _complete_rotation(results: Path, archive: Path):
    """Build + execute a real rotation; return (plan, watermark)."""
    plan = evolution_infra.build_archive_rotation_plan(
        _VERSION, _PUBLICATION_ID
    )
    receipts = evolution_infra.archive_rotate_files(_VERSION, plan)
    assert receipts
    snapshot = next(
        item
        for item in plan["source_snapshots"]
        if item["source"] == "match_history.jsonl"
    )
    assert snapshot["expected_rotation"] is not None
    watermark = json.loads(
        (archive / "match_history.rotation-watermark.json").read_bytes()
    )
    return plan, snapshot, watermark


def test_default_validate_still_fails_closed_on_rewritten_source(rotation_env):
    """Reproduce the v632 wedge: crash-recovery rewrite trips the prefix."""
    results, archive = rotation_env
    original = _seed_match_history(results, cold_lines=4000, keep_lines=800)
    plan, _snapshot, watermark = _complete_rotation(results, archive)
    _crash_recovery_rewrite(results, original, watermark["end_offset"])
    with pytest.raises(RuntimeError, match="source prefix changed"):
        rotation_module.validate_archive_rotation_plan(
            plan, version=_VERSION, publication_id=_PUBLICATION_ID
        )


def test_completed_adoption_accepts_rewritten_source_and_reproves_chain(
    rotation_env,
):
    results, archive = rotation_env
    original = _seed_match_history(results, cold_lines=4000, keep_lines=800)
    plan, _snapshot, watermark = _complete_rotation(results, archive)
    _crash_recovery_rewrite(results, original, watermark["end_offset"])
    adopted = rotation_module.validate_archive_rotation_plan(
        plan,
        version=_VERSION,
        publication_id=_PUBLICATION_ID,
        require_live_source_proof=False,
    )
    assert adopted == plan


def test_adoption_carve_out_does_not_weaken_the_watermark_chain(rotation_env):
    """A tamper INSIDE the watermark range still fails the adoption proof."""
    results, archive = rotation_env
    original = _seed_match_history(results, cold_lines=4000, keep_lines=800)
    plan, _snapshot, watermark = _complete_rotation(results, archive)
    tampered = bytearray(original)
    tampered[100] ^= 0x01  # inside [0, watermark_end)
    (results / "match_history.jsonl").write_bytes(bytes(tampered))
    with pytest.raises(RuntimeError):
        rotation_module.validate_archive_rotation_plan(
            plan,
            version=_VERSION,
            publication_id=_PUBLICATION_ID,
            require_live_source_proof=False,
        )


def test_adoption_carve_out_rejects_truncated_below_watermark(rotation_env):
    """Losing watermark-covered bytes is never adoption-able."""
    results, archive = rotation_env
    original = _seed_match_history(results, cold_lines=4000, keep_lines=800)
    plan, _snapshot, watermark = _complete_rotation(results, archive)
    (results / "match_history.jsonl").write_bytes(
        original[: watermark["end_offset"] - 5000]
    )
    with pytest.raises(RuntimeError):
        rotation_module.validate_archive_rotation_plan(
            plan,
            version=_VERSION,
            publication_id=_PUBLICATION_ID,
            require_live_source_proof=False,
        )


def test_execution_paths_keep_the_mandatory_proof(rotation_env):
    """archive_rotate_files / validate_receipts delegate WITHOUT the flag,
    so a rewritten source still blocks a real execution attempt."""
    results, archive = rotation_env
    original = _seed_match_history(results, cold_lines=4000, keep_lines=800)
    plan, _snapshot, watermark = _complete_rotation(results, archive)
    _crash_recovery_rewrite(results, original, watermark["end_offset"])
    with pytest.raises(RuntimeError, match="source prefix changed"):
        rotation_module.archive_rotate_files(_VERSION, plan)


# ── plan_handoff_step wiring ─────────────────────────────────────────


class _NoopLock:
    def __init__(self, _path):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fabricate_record(module, step_status):
    """Minimal running handoff record with one archive_rotation row at
    ``step_status``; identity carries the publication id only."""
    return {
        "state": "running",
        "owner": {"claim_id": "claim-1"},
        "identity": {
            "publication_id": _PUBLICATION_ID,
            "version": _VERSION,
            "source_v": 173,
        },
        "steps": {"archive_rotation": {"status": step_status}},
    }


def _drive_plan_handoff(module, monkeypatch, record, plan):
    monkeypatch.setattr(module, "_find_exact", lambda v, s: (Path("/x.json"), None))
    monkeypatch.setattr(module, "_JournalLock", _NoopLock)
    monkeypatch.setattr(
        module, "_load_active_pair",
        lambda reopen_archive=True: (Path("/x.json"), None, record),
    )
    monkeypatch.setattr(module, "local_handoff_identity_errors", lambda _r: [])
    return module.plan_handoff_step(
        _VERSION, 173, "claim-1", "archive_rotation", plan
    )


def test_plan_handoff_completed_row_adopts_despite_prefix_rewrite(
    rotation_env, monkeypatch
):
    results, archive = rotation_env
    original = _seed_match_history(results, cold_lines=4000, keep_lines=800)
    plan, _snapshot, watermark = _complete_rotation(results, archive)
    _crash_recovery_rewrite(results, original, watermark["end_offset"])
    normalized = json.loads(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, allow_nan=False)
    )
    record = _fabricate_record(handoff, "completed")
    record["steps"]["archive_rotation"]["receipt"] = {
        "plan_digest": handoff.canonical_digest(normalized)
    }
    out = _drive_plan_handoff(handoff, monkeypatch, record, plan)
    assert out is record  # adopted, no contract_errors raised


def test_plan_handoff_pending_row_still_fails_closed(rotation_env, monkeypatch):
    results, archive = rotation_env
    original = _seed_match_history(results, cold_lines=4000, keep_lines=800)
    plan, _snapshot, watermark = _complete_rotation(results, archive)
    _crash_recovery_rewrite(results, original, watermark["end_offset"])
    record = _fabricate_record(handoff, "executing")
    with pytest.raises(handoff.PostPublicationHandoffError) as exc:
        _drive_plan_handoff(handoff, monkeypatch, record, plan)
    assert "handoff_archive_rotation_plan_live_validation_failed" in str(
        exc.value
    )
    assert "source prefix changed" in str(exc.value)


def test_plan_handoff_completed_row_still_rejects_tampered_chain(
    rotation_env, monkeypatch
):
    """The adoption path only waives the snapshot proof; a completed row
    whose low-plan chain is broken still raises."""
    results, archive = rotation_env
    original = _seed_match_history(results, cold_lines=4000, keep_lines=800)
    plan, _snapshot, watermark = _complete_rotation(results, archive)
    tampered = bytearray(original)
    tampered[100] ^= 0x01
    (results / "match_history.jsonl").write_bytes(bytes(tampered))
    normalized = json.loads(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, allow_nan=False)
    )
    record = _fabricate_record(handoff, "completed")
    record["steps"]["archive_rotation"]["receipt"] = {
        "plan_digest": handoff.canonical_digest(normalized)
    }
    with pytest.raises(handoff.PostPublicationHandoffError) as exc:
        _drive_plan_handoff(handoff, monkeypatch, record, plan)
    assert "handoff_archive_rotation_plan_live_validation_failed" in str(
        exc.value
    )
