"""Phase 4: multi-slot pipeline checkpoint (per-file-per-slot) verification.

The pipeline checkpoint now supports a ``slot_id`` so two generations can run
concurrently:

  - ``slot_id=None`` (default everywhere) -> primary slot ->
    ``pipeline_state.json`` (byte-identical to all 60+ existing callers).
  - ``slot_id="draft"`` (or any non-None value) -> secondary slot ->
    ``pipeline_state_<slot_id>.json``.

Each slot file has its own sidecar lock (the lock path is derived from the data
file path), so per-file-per-slot gets per-slot locking for free.  These tests
use the same fixtures as the rest of the checkpoint suite
(``isolate_state`` + ``synthetic_checkpoint_authority``), so they exercise the
real CAS with proper authority.
"""

import threading

import pytest

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")

import evolution_infra
from evolution_infra import (
    write_pipeline_checkpoint,
    read_pipeline_checkpoint,
    clear_pipeline_checkpoint,
    read_all_pipeline_checkpoints,
    pipeline_state_path,
)


def test_path_resolver_primary_is_canonical_constant():
    """slot_id=None must resolve to the exact PIPELINE_STATE_FILE constant."""
    assert pipeline_state_path(None) is evolution_infra.PIPELINE_STATE_FILE
    assert (
        pipeline_state_path(None)
        == evolution_infra.RESULTS_DIR / "pipeline_state.json"
    )


def test_path_resolver_secondary_slot():
    """A non-None slot_id resolves to a per-slot filename."""
    assert (
        pipeline_state_path("draft")
        == evolution_infra.RESULTS_DIR / "pipeline_state_draft.json"
    )


def test_path_resolver_sanitizes_unsafe_characters():
    """slot_id is sanitized so it cannot escape the results dir or add slashes."""
    resolved = pipeline_state_path("weird/slot..id")
    assert resolved == evolution_infra.RESULTS_DIR / "pipeline_state_weird_slot__id.json"
    # Must stay inside RESULTS_DIR
    assert resolved.parent == evolution_infra.RESULTS_DIR
    assert "/" not in resolved.name
    assert ".." not in resolved.name


def test_primary_slot_backward_compat_byte_identical_path():
    """Calling without slot_id writes/reads/clears pipeline_state.json only."""
    assert write_pipeline_checkpoint(next_v=10, source_v=9, stage="testing")
    assert (evolution_infra.RESULTS_DIR / "pipeline_state.json").exists()
    # No secondary file should be created by a primary write.
    assert not list(evolution_infra.RESULTS_DIR.glob("pipeline_state_*.json"))

    ckpt = read_pipeline_checkpoint()
    assert ckpt is not None
    assert ckpt["next_v"] == 10
    assert ckpt["stage"] == "testing"


def test_secondary_slot_isolated_file():
    """A secondary slot writes to its own file and does not touch the primary."""
    # Seed the primary.
    assert write_pipeline_checkpoint(next_v=10, source_v=9, stage="testing")
    # Write a secondary slot.
    assert write_pipeline_checkpoint(
        next_v=20, source_v=5, stage="evaluation", slot_id="draft"
    )

    assert (evolution_infra.RESULTS_DIR / "pipeline_state_draft.json").exists()
    # Primary untouched.
    primary = read_pipeline_checkpoint()
    assert primary["next_v"] == 10
    assert primary["stage"] == "testing"
    # Secondary reads back independently.
    draft = read_pipeline_checkpoint(slot_id="draft")
    assert draft["next_v"] == 20
    assert draft["stage"] == "evaluation"


def test_slots_are_independent_reads():
    """Each slot reads its own state; defaults still hit the primary."""
    write_pipeline_checkpoint(next_v=10, source_v=9, stage="testing")
    write_pipeline_checkpoint(
        next_v=30, source_v=2, stage="precommit", slot_id="draft"
    )

    assert read_pipeline_checkpoint()["next_v"] == 10
    assert read_pipeline_checkpoint(slot_id="draft")["next_v"] == 30
    # Default (no kwarg) is the primary.
    assert read_pipeline_checkpoint()["next_v"] == 10


def test_clear_is_slot_scoped():
    """clear_pipeline_checkpoint only removes the targeted slot."""
    write_pipeline_checkpoint(next_v=10, source_v=9, stage="testing")
    write_pipeline_checkpoint(
        next_v=20, source_v=5, stage="evaluation", slot_id="draft"
    )

    # Clear primary, draft must survive.
    assert clear_pipeline_checkpoint()
    assert read_pipeline_checkpoint() is None
    assert (evolution_infra.RESULTS_DIR / "pipeline_state_draft.json").exists()
    assert read_pipeline_checkpoint(slot_id="draft") is not None

    # Now clear the draft explicitly.
    assert clear_pipeline_checkpoint(slot_id="draft")
    assert read_pipeline_checkpoint(slot_id="draft") is None
    assert not (evolution_infra.RESULTS_DIR / "pipeline_state_draft.json").exists()


def test_read_all_pipeline_checkpoints_aggregates_slots():
    """read_all returns {'primary': ...} plus any slot files present."""
    assert write_pipeline_checkpoint(next_v=10, source_v=9, stage="testing")
    assert write_pipeline_checkpoint(
        next_v=20, source_v=5, stage="evaluation", slot_id="draft"
    )
    assert write_pipeline_checkpoint(
        next_v=40, source_v=1, stage="precommit", slot_id="canary"
    )

    all_ckpts = read_all_pipeline_checkpoints()
    assert set(all_ckpts) == {"primary", "draft", "canary"}
    assert all_ckpts["primary"]["next_v"] == 10
    assert all_ckpts["draft"]["next_v"] == 20
    assert all_ckpts["canary"]["next_v"] == 40


def test_read_all_skips_missing_primary():
    """If primary is absent but a slot exists, only the slot is returned."""
    assert write_pipeline_checkpoint(
        next_v=20, source_v=5, stage="evaluation", slot_id="draft"
    )
    all_ckpts = read_all_pipeline_checkpoints()
    assert all_ckpts == {"draft": read_pipeline_checkpoint(slot_id="draft")}


def test_concurrent_writes_to_different_slots_do_not_collide():
    """Two threads writing different slots must both succeed independently.

    This exercises the per-file-per-slot locking: each slot's sidecar lock is
    derived from its own data file path, so they do not contend.
    """
    errors = []

    def write_slot(slot_id, next_v):
        try:
            assert write_pipeline_checkpoint(
                next_v=next_v, source_v=1, stage="eval", slot_id=slot_id
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=write_slot, args=("draft", 11)),
        threading.Thread(target=write_slot, args=("canary", 13)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert read_pipeline_checkpoint(slot_id="draft")["next_v"] == 11
    assert read_pipeline_checkpoint(slot_id="canary")["next_v"] == 13
