"""Tests for the precommit_attempt field on the pipeline checkpoint.

Global interface contract: pipeline_state.json gains a "precommit_attempt"
field that counts how many times run_precommit_eval has been called against
the CURRENT bot code for this generation. It:

  - defaults to 0 on a fresh checkpoint,
  - is settable via the precommit_attempt kwarg,
  - is resettable via reset_precommit_attempt=True,
  - AUTO-RESETS to 0 on a canonical code-mutating rework transition, because
    Worker output changes the bot snapshot measured by precommit.

The autouse isolate_state fixture in conftest.py redirects
evolution_infra.PIPELINE_STATE_FILE to a tmp directory, so these tests never
touch the real bots/ dir or production checkpoint file.
"""

from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")

import checkpoint_schema
import evolution_infra
from evolution_infra import (
    clear_pipeline_checkpoint as _clear_pipeline_checkpoint,
    write_pipeline_checkpoint as _write_pipeline_checkpoint,
    read_pipeline_checkpoint,
    MAX_PRECOMMIT_RETRIES,
)
from pipeline_state import route_policy


def write_pipeline_checkpoint(*args, **kwargs):
    """Map retired compact fixture numbers into the strict policy namespace."""

    mutable = list(args)
    if mutable:
        mutable[0] = 200 if mutable[0] == 100 else mutable[0]
    if len(mutable) > 1:
        mutable[1] = 199 if mutable[1] == 99 else mutable[1]
    if kwargs.get("next_v") == 100:
        kwargs["next_v"] = 200
    if kwargs.get("source_v") == 99:
        kwargs["source_v"] = 199
    return _write_pipeline_checkpoint(*mutable, **kwargs)


def clear_pipeline_checkpoint(**kwargs):
    if kwargs.get("expected_next_v") == 100:
        kwargs["expected_next_v"] = 200
    if kwargs.get("expected_source_v") == 99:
        kwargs["expected_source_v"] = 199
    return _clear_pipeline_checkpoint(**kwargs)


@pytest.fixture(autouse=True)
def _strict_parent_authority(monkeypatch):
    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: SimpleNamespace(
            eligible=True,
            version=199,
            issues=(),
            runtime_manifest={"epoch": "national_tcp_policy_v1"},
            epoch_receipt={"epoch": "national_tcp_policy_v1", "version": 199},
            publication_identity={"published": True, "version": 199},
            certificate_digest="a" * 64,
        ),
    )


def _write_basic(stage="prepared", **kwargs):
    """Helper: write a minimal checkpoint and return the persisted dict."""
    write_pipeline_checkpoint(next_v=100, source_v=99, stage=stage, **kwargs)
    return read_pipeline_checkpoint()


def _passing_quality_gate():
    return {
        "all_passed": True,
        "critical_scenarios_passed": True,
        "workflow_profile_id": "national_native",
        "national_execution_mode": "native_tcp",
        "national_native_contract_ok": True,
    }


def test_fresh_checkpoint_defaults_precommit_attempt_to_zero():
    """A brand-new checkpoint has precommit_attempt == 0 (no precommit calls yet)."""
    state = _write_basic(stage="prepared")
    assert state is not None, "checkpoint should have been written"
    assert state.get("precommit_attempt") == 0


def test_fresh_checkpoint_persists_log_correlation_fields():
    """Fresh checkpoints carry the same correlation key the event bus emits."""
    state = _write_basic(stage="prepared")
    assert state.get("run_id") == "200#0"
    assert state.get("workflow_run_id", "").startswith("generation:200:")
    assert state.get("checkpoint_revision") == 1
    assert state.get("generation_attempt") == 0
    assert state.get("audit_attempt") == 0
    assert state.get("precommit_attempt") == 0


def test_checkpoint_revision_cas_serializes_concurrent_waiters():
    state = _write_basic(stage="prepared")
    barrier = threading.Barrier(2)

    def advance(stage):
        barrier.wait()
        return write_pipeline_checkpoint(
            next_v=100,
            source_v=99,
            stage=stage,
            expected_checkpoint_revision=state["checkpoint_revision"],
            expected_checkpoint_stage="prepared",
            expected_workflow_run_id=state["workflow_run_id"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(advance, ["direction_audited", "master_planned"]))

    assert sorted(outcomes) == [False, True]
    final = read_pipeline_checkpoint()
    assert final["checkpoint_revision"] == state["checkpoint_revision"] + 1
    assert final["workflow_run_id"] == state["workflow_run_id"]


def test_checkpoint_clear_requires_exact_actor_revision_and_stage():
    state = _write_basic(stage="prepared")

    assert clear_pipeline_checkpoint(
        expected_workflow_run_id=state["workflow_run_id"],
        expected_next_v=100,
        expected_source_v=99,
        expected_checkpoint_revision=state["checkpoint_revision"] + 1,
        expected_checkpoint_stage="prepared",
    ) is False
    assert read_pipeline_checkpoint() == state

    assert clear_pipeline_checkpoint(
        expected_workflow_run_id=state["workflow_run_id"],
        expected_next_v=100,
        expected_source_v=99,
        expected_checkpoint_revision=state["checkpoint_revision"],
        expected_checkpoint_stage="prepared",
    ) is True
    assert read_pipeline_checkpoint() is None


def test_corrupt_nonempty_checkpoint_cannot_be_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "pipeline_state.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", path)

    assert write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="prepared",
    ) is False
    assert path.read_text(encoding="utf-8") == "{broken"


def test_workflow_identity_survives_generation_attempt_changes():
    first = _write_basic(stage="direction_audited", generation_attempt=2)
    write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="master_planned",
        reset_generation_attempt=True,
    )
    second = read_pipeline_checkpoint()
    assert first["run_id"] == "200#2"
    assert second["run_id"] == "200#0"
    assert second["workflow_run_id"] == first["workflow_run_id"]


def test_precommit_attempt_kwarg_persists_value():
    """write_pipeline_checkpoint(precommit_attempt=2) stores 2."""
    # First establish a checkpoint with a known source/next so the merge path
    # applies (existing.next_v == next_v).
    _write_basic(stage="reviewed")
    # Now bump the precommit attempt counter.
    write_pipeline_checkpoint(
        next_v=100, source_v=99, stage="reviewed", precommit_attempt=2
    )
    state = read_pipeline_checkpoint()
    assert state.get("precommit_attempt") == 2


def test_reset_precommit_attempt_resets_to_zero():
    """reset_precommit_attempt=True forces the counter back to 0."""
    _write_basic(stage="reviewed", precommit_attempt=3)
    assert read_pipeline_checkpoint().get("precommit_attempt") == 3

    write_pipeline_checkpoint(
        next_v=100, source_v=99, stage="reviewed", reset_precommit_attempt=True
    )
    state = read_pipeline_checkpoint()
    assert state.get("precommit_attempt") == 0


def test_precommit_attempt_preserved_on_unrelated_write():
    """When precommit_attempt is not passed, the existing value is preserved."""
    _write_basic(stage="reviewed", precommit_attempt=2)
    # A later write that does NOT touch precommit_attempt should keep it.
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="reviewed")
    state = read_pipeline_checkpoint()
    assert state.get("precommit_attempt") == 2


def test_stage_regression_auto_resets_precommit_attempt():
    """verified -> workers_done (worker rework) resets precommit_attempt to 0.

    Workers reworking the bot produces new code, so the precommit counter
    (which counts attempts against the CURRENT bot code) must restart.
    """
    # Reach a LATE stage with a non-zero precommit attempt count.
    _write_basic(stage="verified", precommit_attempt=3)
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "verified"
    assert state.get("precommit_attempt") == 3

    # Worker rework regresses the stage back to workers_done WITHOUT explicitly
    # touching precommit_attempt — the AUTO-RESET should zero it.
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="workers_done")
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "workers_done"
    assert state.get("precommit_attempt") == 0


def test_rework_to_workers_done_invalidates_stale_downstream_gates():
    """Worker rework creates new code, so old gates cannot survive the reset."""
    _write_basic(
        stage="precommit_failed",
        precommit_attempt=1,
        gate_results={
            "quality": _passing_quality_gate(),
            "review": {
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            },
            "critic": {
                "approved": True,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            },
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "aggregate_precommit_regression"}],
            },
        },
        reviewer_feedback="Precommit FAILED vs parent",
    )
    assert "precommit_eval" in read_pipeline_checkpoint()["gate_results"]

    write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="rework_running",
        reviewer_feedback="Precommit FAILED vs parent",
    )
    assert "precommit_eval" in read_pipeline_checkpoint()["gate_results"]

    write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="workers_done",
        reviewer_feedback="Precommit FAILED vs parent",
    )
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "workers_done"
    assert state.get("precommit_attempt") == 0
    assert state.get("gate_results") == {}


def test_post_rework_gates_route_to_fresh_precommit_eval():
    """Regression for v27 loop: stale failed precommit must not route to workers."""
    _write_basic(
        stage="precommit_failed",
        precommit_attempt=1,
        gate_results={
            "quality": _passing_quality_gate(),
            "review": {
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            },
            "critic": {
                "approved": True,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            },
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "semantic_regression"}],
            },
        },
        reviewer_feedback="Precommit FAILED vs parent",
    )

    write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="rework_running",
        reviewer_feedback="Precommit FAILED vs parent",
    )
    write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="workers_done",
        reviewer_feedback="Precommit FAILED vs parent",
    )
    write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="quality_passed",
        gate_results={"quality": _passing_quality_gate()},
    )
    write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="reviewed",
        gate_results={
            "review": {
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            }
        },
    )
    write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="critic_checked",
        gate_results={
            "critic": {
                "approved": True,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            }
        },
    )

    state = read_pipeline_checkpoint()
    assert set(state.get("gate_results")) == {"quality", "review", "critic"}
    assert route_policy(state)["next_tool"] == "run_precommit_eval"


def test_critic_checked_to_master_planned_resets_precommit_attempt():
    """critic_checked -> master_planned (the actual precommit-failed rework
    path) must reset precommit_attempt to 0.

    Precommit failures leave the stage at critic_checked (verified is only set
    on a PASS). When the LLM then reworks the bot, the real transition is
    critic_checked -> master_planned, NOT verified -> early. The STAGE_RANK
    reset must fire here so the counter restarts for the new bot code.
    """
    _write_basic(stage="critic_checked", precommit_attempt=2)
    assert read_pipeline_checkpoint().get("precommit_attempt") == 2

    # Master re-plans after precommit failure → new bot code imminent.
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="master_planned")
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "master_planned"
    assert state.get("precommit_attempt") == 0


def test_critic_checked_to_reviewed_does_not_reset_precommit_attempt():
    """A same-code regression (critic_checked -> reviewed) must NOT reset.

    That is the same bot code being re-evaluated, not regenerated, so the
    precommit counter against the current code must survive.
    """
    _write_basic(stage="critic_checked", precommit_attempt=2)
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="reviewed")
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "reviewed"
    assert state.get("precommit_attempt") == 2


def test_timeout_extensions_preserved_when_not_passed():
    """write_pipeline_checkpoint that omits timeout_extensions must preserve it.

    The canonical writer builds its state from explicitly-merged fields only;
    timeout_extensions was being dropped on every normal write. Now it must
    persist across a subsequent write that does not pass the kwarg.
    """
    _write_basic(stage="verified", timeout_extensions=1)
    assert read_pipeline_checkpoint().get("timeout_extensions") == 1

    # A later write (e.g. tool_eval persisting precommit_attempt mid-cycle)
    # that does NOT mention timeout_extensions must keep the existing 1.
    write_pipeline_checkpoint(
        next_v=100, source_v=99, stage="verified", precommit_attempt=2
    )
    state = read_pipeline_checkpoint()
    assert state.get("timeout_extensions") == 1
    assert state.get("precommit_attempt") == 2


def test_timeout_extensions_resets_on_canonical_precommit_rework_path():
    """A real precommit regression resets the budget before new Worker code."""

    failed = _write_basic(
        stage="precommit_failed",
        precommit_attempt=1,
        timeout_extensions=1,
        gate_results={
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "aggregate_precommit_regression"}],
            }
        },
    )
    assert route_policy(failed)["next_tool"] == "execute_workers"

    # The canonical Worker repair path is explicit.  Critic is advisory, so a
    # bare critic_checked -> workers_done jump is neither a regression proof nor
    # a legal state transition.
    assert write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="repair_planned",
        reviewer_feedback="Precommit regression requires Worker repair",
    ) is True
    planned = read_pipeline_checkpoint()
    assert planned.get("precommit_attempt") == 0
    assert planned.get("timeout_extensions") == 0

    assert write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="rework_running",
    ) is True
    assert write_pipeline_checkpoint(
        next_v=100,
        source_v=99,
        stage="workers_done",
    ) is True
    completed = read_pipeline_checkpoint()
    assert completed.get("stage") == "workers_done"
    assert completed.get("timeout_extensions") == 0


def test_forward_progression_does_not_reset_precommit_attempt():
    """Moving forward (reviewed -> critic_checked) must NOT reset the counter.

    Guards against an over-broad auto-reset that would wipe legitimate
    precommit attempt counts on normal forward stage transitions.
    """
    _write_basic(stage="reviewed", precommit_attempt=2)
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="critic_checked")
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "critic_checked"
    assert state.get("precommit_attempt") == 2


def test_max_precommit_retries_constant_is_three():
    """MAX_PRECOMMIT_RETRIES is exposed as the documented cap (3)."""
    assert MAX_PRECOMMIT_RETRIES == 3
