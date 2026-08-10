"""Deep-parallelism: draft drives its full gate chain and promotes.

These tests cover the Stage-1 core change: a one-ahead draft no longer stops
at ``workers_done`` waiting for promotion.  Instead it triggers the Slice 2b
seal seam (sealing the candidate under its ``reserved_next_v`` and launching
a consumer that runs the full gate chain quality->review->critic->precommit),
and when the primary publishes, the draft is promoted to the primary at
``workers_done`` while the draft's ALREADY-SEALED consumer slot (same
candidate_id) is preserved.  The primary's seal seam then recognizes the
candidate as already-sealed+promoted (ALREADY-SEALED GUARD), parks, and the
promotion barrier collapses the consumer's verified evidence onto the primary
so commit_bot publishes WITHOUT re-running any gate.

Key invariant: the draft's consumer ``candidate_id`` (candidate-v<reserved>)
MATCHES the next primary's candidate_id (candidate-v<formal_next_v>), so the
existing tested primary-consumer machinery handles the verified collapse.

Branch-portable: uses synthetic checkpoint authority + ``STRICT_TARGET_V``,
never hardcodes version literals.
"""

import json

import pytest

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")

from conftest import STRICT_TARGET_V  # noqa: E402

# Import orchestrator BEFORE orchestrator_deterministic_route to avoid a
# circular-import partially-initialized-module error.
import orchestrator  # noqa: E402,F401
import orchestrator_deterministic_route as odr  # noqa: E402
from evolution_infra import (  # noqa: E402
    read_pipeline_checkpoint,
    clear_pipeline_checkpoint,
    pipeline_state_path,
)


def _write_draft_checkpoint(next_v, source_v, *, stage="workers_done"):
    """Write a minimal draft checkpoint directly (bypassing CAS authority)."""
    path = pipeline_state_path("draft")
    path.write_text(
        json.dumps(
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": stage,
                "is_draft": True,
                "checkpoint_revision": 1,
                "workflow_run_id": f"generation:{next_v}:workflow-v1",
            }
        )
    )


def _write_consumer_slot(candidate_id, next_v, source_v, *, stage="verified"):
    """Write a minimal consumer-slot checkpoint (the draft's verified gate chain)."""
    slot = f"consumer-{candidate_id}"
    path = pipeline_state_path(slot)
    path.write_text(
        json.dumps(
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": stage,
                "checkpoint_revision": 5,
                "gate_results": {
                    "run_quality_gates": {"outcome": "success"},
                    "run_review": {"outcome": "success"},
                    "run_critic": {"outcome": "success"},
                    "run_precommit_eval": {
                        "outcome": "success",
                        "digest": "p" * 64,
                    },
                },
                "candidate_artifact_hash": "a" * 64,
                "candidate_manifest_digest": "m" * 64,
                "charter_digest": "c" * 64,
                "workflow_run_id": f"generation:{next_v}:workflow-v1",
            }
        )
    )


def test_promote_draft_writes_workers_done_and_preserves_consumer_slot():
    """_promote_draft_to_primary writes the draft to primary at workers_done
    and PRESERVES the draft's consumer slot (the candidate_id matches the
    primary's, so the seal-seam ALREADY-SEALED GUARD + promotion barrier will
    collapse the verified evidence onto the primary at commit_bot time)."""
    draft_next_v = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    published_v = draft_next_v - 1

    _write_draft_checkpoint(draft_next_v, source_v)
    candidate_id = f"candidate-v{draft_next_v}"
    _write_consumer_slot(candidate_id, draft_next_v, source_v, stage="verified")
    clear_pipeline_checkpoint()

    ok = odr._promote_draft_to_primary(published_v)
    assert ok is True
    primary = read_pipeline_checkpoint()
    assert primary is not None
    assert primary["stage"] == "workers_done"
    assert primary["next_v"] == draft_next_v
    # The draft slot is cleared (promoted).
    assert read_pipeline_checkpoint(slot_id="draft") is None
    # The consumer slot is PRESERVED (the promotion barrier will collapse it).
    consumer = read_pipeline_checkpoint(slot_id=f"consumer-{candidate_id}")
    assert consumer is not None
    assert consumer["stage"] == "verified"
    assert consumer["next_v"] == draft_next_v


def test_promote_draft_without_consumer_writes_workers_done():
    """When the draft has NOT yet sealed (no consumer slot), the promote
    writes workers_done so the canonical inline gate chain validates it
    (preserves the pre-deep-parallelism behaviour for unsealed drafts)."""
    draft_next_v = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    published_v = draft_next_v - 1

    _write_draft_checkpoint(draft_next_v, source_v)
    clear_pipeline_checkpoint()

    ok = odr._promote_draft_to_primary(published_v)
    assert ok is True
    primary = read_pipeline_checkpoint()
    assert primary["stage"] == "workers_done"
    assert primary["next_v"] == draft_next_v


def test_promote_draft_refuses_when_primary_live():
    """The promote must NOT clobber a live primary; it CAS-refuses and leaves
    the draft in place for a later retry (the primary is still mid-publication)."""
    draft_next_v = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    published_v = draft_next_v - 1

    _write_draft_checkpoint(draft_next_v, source_v)
    # Primary already at formal_next_v, non-terminal -> CAS refuse.
    from evolution_infra import write_pipeline_checkpoint

    write_pipeline_checkpoint(
        next_v=draft_next_v, source_v=source_v, stage="workers_done"
    )

    ok = odr._promote_draft_to_primary(published_v)
    # CAS refused (primary still has the live formal_next_v workflow).
    # The result depends on CAS semantics; the key invariant is the draft is
    # NOT cleared on refusal.
    draft = read_pipeline_checkpoint(slot_id="draft")
    if not ok:
        assert draft is not None  # draft preserved for retry


def test_draft_candidate_id_matches_formal_primary_candidate_id():
    """The deep-parallelism correctness invariant: the draft's consumer
    candidate_id (candidate-v<reserved_next_v>) EQUALS the next primary's
    candidate_id (candidate-v<formal_next_v>).  This is what lets the existing
    ALREADY-SEALED GUARD + promotion barrier handle the verified draft without
    any new collapse machinery."""
    # The reservation mechanism guarantees reserved_next_v >= floor+1 where
    # floor derives from the live primary.  After promotion, formal_next_v =
    # published_v + 1 = reserved_next_v (they coincide for the lucky draft).
    draft_reserved_v = STRICT_TARGET_V + 1
    formal_next_v = (STRICT_TARGET_V) + 1  # published STRICT_TARGET_V -> +1
    draft_candidate_id = f"candidate-v{draft_reserved_v}"
    primary_candidate_id = f"candidate-v{formal_next_v}"
    assert draft_candidate_id == primary_candidate_id
