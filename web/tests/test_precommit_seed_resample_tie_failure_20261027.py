"""Tie-type precommit failure buys the fresh-seed sample, not a repair (2026-10-27).

Contract under test:

* A NON-DECISIVE round-1 failure (no precommit gate matchup shows
  ``losses >= wins + PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN`` at its own minimum
  sample depth, and the aggregate does not either) restores the UNCHANGED
  candidate from ``precommit_failed`` back to ``critic_checked`` and consumes
  the single rework counter unit (``precommit_rework_count`` 0 -> 1).  The next
  ``run_precommit_eval`` therefore rebuilds the frozen plan under seed round 2
  and re-samples the unchanged candidate under the same opponents, sample
  depths, gates and evaluation contract.
* A DECISIVE regression keeps the historical in-place Worker repair route.
* The restore runs only after the failed gate and its durable attempt record
  are on disk, and is an exact-CAS write.
* One generation still gets at most TWO independent samples: the resample
  consumes the same ``precommit_rework_count`` unit the repair path consumes,
  so a second failure is never resampled again (round cap 2).
* ``infra_timeout`` failures (harness stalls) never consume the unit.
"""

import pytest

import pipeline_state
import tool_eval
from pipeline_state import (
    is_rework_reset_transition,
    validate_stage_transition,
)
from precommit_eval_contract import plan_seed_round


def _plan(seed_round):
    return {
        "plan_digest": f"plan-round-{seed_round}",
        "settings": {
            "precommit_seed_round": seed_round,
            "deck_seed_base": 91_000 + seed_round,
        },
        "opponents": [{"name": "national_cloud_v8", "reason": "parent"}],
    }


def _matchup(wins, losses, draws=0, opponent="national_cloud_v8", **extra):
    return {
        "opponent": opponent,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        **extra,
    }


def _failure(matchups):
    return {
        "passed": False,
        "failure_class": "regression",
        "blockers": [{
            "reason": "did_not_beat_parent",
            "opponent": "national_cloud_v8",
        }],
        "matchups": matchups,
        "total_wins": sum(item["wins"] for item in matchups),
        "total_losses": sum(item["losses"] for item in matchups),
        "total_draws": sum(item["draws"] for item in matchups),
        "precommit_attempt": 1,
        "intent": {"kind": "abandon"},
    }


def _checkpoint(*, stage="precommit_failed", revision=7, rework_count=0):
    return {
        "stage": stage,
        "checkpoint_revision": revision,
        "workflow_run_id": "workflow:test:1",
        "precommit_rework_count": rework_count,
        "gate_results": {},
    }


def _install_restore_harness(monkeypatch, checkpoint):
    writes = []
    events = []
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(tool_eval, "log_system_event", lambda *a, **k: events.append((a, k)))

    def fake_write(_v, _source_v, stage, **kwargs):
        writes.append({"stage": stage, **kwargs})
        return True

    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", fake_write)
    return writes, events


def test_ambiguous_tie_failure_restores_critic_checked_and_preregisters_round_two(
    monkeypatch,
):
    checkpoint = _checkpoint()
    writes, events = _install_restore_harness(monkeypatch, checkpoint)
    # Parent leg is exactly 6W-6L (no loss margin) and the aggregate 16W-11L
    # is not a loss either: this sample cannot distinguish the candidate from
    # its parent, so the second independent seed is the informative action.
    result = _failure([
        _matchup(6, 6),
        _matchup(10, 5, opponent="national_cloud_v5"),
    ])

    applied = tool_eval._apply_precommit_seed_resample_instead_of_repair(
        v=9,
        source_v=8,
        result=result,
        precommit_plan=_plan(1),
        checkpoint=checkpoint,
        gate_failure_class="regression",
    )

    assert applied is True
    assert len(writes) == 1
    write = writes[0]
    # Exact-CAS restore to the precommit-owning stage, consuming the single
    # rework unit that the next rotation reads.
    assert write["stage"] == "critic_checked"
    assert write["precommit_rework_count"] == 1
    assert write["expected_checkpoint_stage"] == "precommit_failed"
    assert write["expected_checkpoint_revision"] == 7
    assert write["expected_workflow_run_id"] == "workflow:test:1"
    assert "UNCHANGED" in write["reviewer_feedback"]
    assert "run_precommit_eval" in write["reviewer_feedback"]
    # The returned payload sends the orchestrator straight back to precommit
    # and explicitly forbids the in-place rework.
    assert result["precommit_seed_resample_pre_registered"] is True
    assert result["precommit_rework_count"] == 1
    assert result["precommit_resample_seed_round"] == 2
    assert result["intent"]["kind"] == "retry_same_tool"
    assert result["intent"]["next_tool"] == "run_precommit_eval"
    assert "Do NOT call execute_workers" in result["directive"]
    assert "rework" in result["directive"]
    # The next precommit run sees seed round 2 from the same counter.
    assert tool_eval._precommit_rework_seed_round(
        {"precommit_rework_count": 1}
    ) == 2
    assert events and events[0][0][0] == (
        "pipeline.precommit_seed_resample_instead_of_repair"
    )


@pytest.mark.parametrize(
    "matchups",
    [
        # Aggregate 11W-13L: losses == wins + 2 with n >= 8.
        [_matchup(5, 7), _matchup(6, 6, opponent="national_cloud_v5")],
        # One parent leg 4W-8L: n >= 6 and losses >= wins + 2.
        [_matchup(4, 8), _matchup(10, 4, opponent="national_cloud_v5")],
    ],
)
def test_decisive_regression_keeps_the_in_place_repair(monkeypatch, matchups):
    checkpoint = _checkpoint()
    writes, events = _install_restore_harness(monkeypatch, checkpoint)
    result = _failure(matchups)
    result["directive"] = "Rework the bot against the parent."

    applied = tool_eval._apply_precommit_seed_resample_instead_of_repair(
        v=9,
        source_v=8,
        result=result,
        precommit_plan=_plan(1),
        checkpoint=checkpoint,
        gate_failure_class="regression",
    )

    assert applied is False
    assert writes == []
    assert events == []
    assert result["directive"] == "Rework the bot against the parent."
    assert result["intent"] == {"kind": "abandon"}
    assert "precommit_seed_resample_pre_registered" not in result


def test_second_failure_is_never_resampled_again(monkeypatch):
    """The resample consumes the one rework unit, so at most two samples run."""

    assert tool_eval.PRECOMMIT_SEED_ROUNDS_MAX == 2
    checkpoint = _checkpoint(rework_count=1)
    writes, _events = _install_restore_harness(monkeypatch, checkpoint)
    result = _failure([_matchup(6, 6)])

    applied = tool_eval._apply_precommit_seed_resample_instead_of_repair(
        v=9,
        source_v=8,
        result=result,
        precommit_plan=_plan(2),
        checkpoint=checkpoint,
        gate_failure_class="regression",
    )

    assert applied is False
    assert writes == []
    assert result["intent"] == {"kind": "abandon"}

    # Even a corrupted/reset counter cannot buy a third sample: the frozen
    # plan is already round 2, so the rotation guard refuses it.
    assert tool_eval._precommit_seed_resample_preferred(
        result=result,
        precommit_plan=_plan(2),
        checkpoint=_checkpoint(rework_count=0),
        gate_failure_class="regression",
    ) is False

    # A third rotation is refused outright: round 2 is the last pre-registered
    # independent sample of the generation.
    rotated, error = tool_eval._rotate_precommit_plan_for_repair_round(
        v=9,
        source_v=8,
        checkpoint={"stage": "precommit_failed", "precommit_rework_count": 2},
        stored_plan=_plan(1),
        workflow_profile=None,
    )
    assert rotated is None
    assert error["blockers"][0]["reason"] == "precommit_repair_round_exhausted"


def test_infra_timeout_never_consumes_the_rework_unit():
    assert tool_eval._precommit_seed_resample_preferred(
        result=_failure([_matchup(6, 6)]),
        precommit_plan=_plan(1),
        checkpoint=_checkpoint(rework_count=0),
        gate_failure_class="infra_timeout",
    ) is False


def test_failed_unknown_ambiguous_sample_is_also_resampled():
    result = {
        "passed": False,
        "blockers": [],
        "matchups": [_matchup(6, 6)],
        "intent": {"kind": "abandon"},
    }
    assert tool_eval._precommit_seed_resample_preferred(
        result=result,
        precommit_plan=_plan(1),
        checkpoint=_checkpoint(rework_count=0),
        gate_failure_class="failed_unknown",
    ) is True


def test_failed_restore_write_keeps_the_historical_repair_route(monkeypatch):
    checkpoint = _checkpoint()
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(tool_eval, "log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(
        tool_eval, "write_pipeline_checkpoint", lambda *a, **k: False
    )
    result = _failure([_matchup(6, 6)])
    result["directive"] = "original"

    applied = tool_eval._apply_precommit_seed_resample_instead_of_repair(
        v=9,
        source_v=8,
        result=result,
        precommit_plan=_plan(1),
        checkpoint=checkpoint,
        gate_failure_class="regression",
    )

    assert applied is False
    assert result["directive"] == "original"
    assert result["intent"] == {"kind": "abandon"}
    assert "precommit_seed_resample_pre_registered" not in result


def test_real_checkpoint_write_restores_critic_checked_and_prunes_the_failed_gate(
    synthetic_checkpoint_authority,
):
    """The restore must survive the REAL stage machine / gate pruning writer."""

    from evolution_infra import read_pipeline_checkpoint, write_pipeline_checkpoint

    assert write_pipeline_checkpoint(
        next_v=10,
        source_v=9,
        stage="critic_checked",
        gate_results={
            "quality": {"passed": True},
            "review": {"passed": True},
            "critic": {"approved": True},
        },
    )
    assert write_pipeline_checkpoint(
        next_v=10,
        source_v=9,
        stage="precommit_failed",
        gate_results={"precommit_eval": {
            "passed": False,
            "blockers": [{"reason": "did_not_beat_parent"}],
        }},
        reviewer_feedback="round 1 failed",
        precommit_attempt=1,
    )
    before = read_pipeline_checkpoint()
    assert before["stage"] == "precommit_failed"
    assert before["precommit_attempt"] == 1
    assert "precommit_eval" in before["gate_results"]

    assert tool_eval._pre_register_precommit_seed_resample(
        v=10, source_v=9, prior_rework_count=0
    )

    after = read_pipeline_checkpoint()
    assert after["stage"] == "critic_checked"
    assert after["precommit_rework_count"] == 1
    # The write's stage gate allowlist prunes the failed precommit gate, which
    # is what makes route_policy choose run_precommit_eval again.
    assert "precommit_eval" not in after["gate_results"]
    assert set(after["gate_results"]) == {"quality", "review", "critic"}
    assert "UNCHANGED" in after["reviewer_feedback"]
    # Not a rework reset: the precommit attempt budget is untouched.
    assert after["precommit_attempt"] == 1


def test_seed_resample_restore_is_an_explicit_stage_machine_edge(monkeypatch):
    ok, reason = validate_stage_transition("precommit_failed", "critic_checked")
    assert ok is True
    assert reason == "precommit_seed_resample_restore"
    # The restore is not a rework reset: the precommit attempt budget is NOT
    # laundered by it.
    assert is_rework_reset_transition("precommit_failed", "critic_checked") is False

    # The restored checkpoint lost ``gate_results.precommit_eval`` (the write's
    # stage allowlist prunes it), so route_policy sends run_precommit_eval
    # instead of execute_workers.
    monkeypatch.setattr(
        "checkpoint_schema.checkpoint_epoch_errors", lambda *a, **k: []
    )
    monkeypatch.setattr(
        pipeline_state,
        "_quality_gate_matches_active_workflow",
        lambda *a, **k: True,
    )
    gates = {
        "quality": {"all_passed": True},
        "review": {"approved": True},
        "critic": {
            "approved": True,
            "llm_invoked": True,
            "critic_llm_executed": True,
            "schema_valid": True,
        },
    }
    restored = {
        "stage": "critic_checked",
        "next_v": 9,
        "source_v": 8,
        "gate_results": gates,
    }
    route = pipeline_state.route_policy(restored)
    assert route["next_tool"] == "run_precommit_eval"
    assert route["allowed_tools"] == ["run_precommit_eval"]

    # With the failed precommit gate still present the same stage routes to
    # execute_workers; pruning it is what makes the resample reachable.
    stale = {
        **restored,
        "gate_results": {
            **gates,
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "did_not_beat_parent"}],
            },
        },
    }
    assert pipeline_state.route_policy(stale)["next_tool"] == "execute_workers"
    assert plan_seed_round(_plan(2)) == 2
