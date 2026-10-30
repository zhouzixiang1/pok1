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


def _failure(matchups, ci=None):
    result = {
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
    if ci is not None:
        result["paired_bootstrap"] = {
            "aggregate_ci_lower": ci[0],
            "aggregate_ci_upper": ci[1],
            "net_chips_samples": sum(
                item["wins"] + item["losses"] + item["draws"] for item in matchups
            ),
        }
    return result


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
    "matchups,ci",
    [
        # Aggregate 5W-15L over 20 decided matches: an exact one-sided
        # binomial tail of 0.021 — the sample proves a regression.
        ([_matchup(2, 6), _matchup(3, 9, opponent="national_cloud_v5")], None),
        # Aggregate 6W-14L is inside the noise band (p=0.058), but the paired
        # net-chip bootstrap CI is entirely below zero: the chip channel
        # proves the loss on its own.
        (
            [_matchup(3, 7), _matchup(3, 7, opponent="national_cloud_v5")],
            (-5000.0, -100.0),
        ),
    ],
)
def test_decisive_regression_keeps_the_in_place_repair(monkeypatch, matchups, ci):
    checkpoint = _checkpoint()
    writes, events = _install_restore_harness(monkeypatch, checkpoint)
    result = _failure(matchups, ci=ci)
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


# --- calibration against every real precommit report on disk -----------------
#
# The 17 failed round reports between v589 and v628 (persisted under
# RESULTS_DIR/precommit_reports as (wins, losses, aggregate net-chip CI)).
# Only two of them are a statistically established regression; the other 15
# are coincidences at parity — exactly the samples where an unguided repair
# walks the candidate away from its parent on noise.
REAL_FAILED_SAMPLES = {
    "v589": (20, 28, -5962.4, 2501.0),
    "v590": (21, 27, -5345.3, 2492.9),
    "v595": (19, 21, -5218.2, 3910.0),
    "v597": (13, 27, -8772.2, -691.1),
    "v599": (21, 27, -6229.6, 1548.8),
    "v600": (21, 19, -3446.3, 4926.0),
    "v601": (20, 28, -6401.4, 1470.1),
    "v605": (15, 25, -7815.9, 1169.4),
    "v609": (23, 25, -4068.3, 3652.7),
    "v610": (25, 23, -2638.7, 5525.5),
    "v611": (22, 26, -4935.7, 3199.4),
    "v617": (21, 27, -6235.2, 1537.3),
    "v621": (19, 29, -7893.3, 774.2),
    "v622": (21, 27, -5068.3, 2831.5),
    "v625": (20, 20, -4298.4, 4339.3),
    "v627": (16, 32, -7423.1, 278.0),
    "v628": (20, 20, -4355.5, 4515.4),
}

DECISIVE_SAMPLES = {"v597", "v627"}


@pytest.mark.parametrize("name", sorted(REAL_FAILED_SAMPLES))
def test_real_precommit_samples_classify_decisive_only_when_proven(name):
    wins, losses, ci_lower, ci_upper = REAL_FAILED_SAMPLES[name]
    result = {
        "passed": False,
        "total_wins": wins,
        "total_losses": losses,
        "total_draws": 0,
        "paired_bootstrap": {
            "aggregate_ci_lower": ci_lower,
            "aggregate_ci_upper": ci_upper,
        },
    }
    assert tool_eval._precommit_failure_is_decisive(result) is (
        name in DECISIVE_SAMPLES
    )


def test_round_report_shape_classifies_the_same_way():
    # The persisted report carries the aggregate block (wins/losses/net_chips_ci)
    # instead of the live gate result's total_* + paired_bootstrap pair.
    report = {
        "precommit_passed": False,
        "aggregate": {
            "wins": 20,
            "losses": 20,
            "draws": 0,
            "net_chips_ci": [-4355.5, 4515.4],
        },
        "matchups": [{"opponent": "national_cloud_v608", "wins": 4, "losses": 4, "draws": 0}],
    }
    assert tool_eval._precommit_failure_is_decisive(report) is False
    report["aggregate"] = {"wins": 13, "losses": 27, "draws": 0,
                           "net_chips_ci": [-8772.2, -691.1]}
    assert tool_eval._precommit_failure_is_decisive(report) is True


def test_binomial_tail_is_exact_at_the_sample_depths_this_gate_uses():
    # n=40: 14W is p=0.040 (decisive), 15W is p=0.077 (not).
    assert tool_eval._binomial_tail_p(26, 40) < 0.05
    assert tool_eval._binomial_tail_p(25, 40) > 0.05
    # n=48: 17W is p=0.023 (decisive), 18W is p=0.056 (not).
    assert tool_eval._binomial_tail_p(31, 48) < 0.05
    assert tool_eval._binomial_tail_p(30, 48) > 0.05
    assert tool_eval._binomial_tail_p(0, 0) == 1.0


def test_thin_or_missing_evidence_is_not_decisive():
    # A 4W-4L sample cannot distinguish anything, and an unparsable payload
    # must not be turned into a decisive regression.
    assert tool_eval._precommit_failure_is_decisive(
        {"total_wins": 4, "total_losses": 4, "total_draws": 0}
    ) is False
    assert tool_eval._precommit_failure_is_decisive({}) is False
    assert tool_eval._precommit_failure_is_decisive(None) is False


def test_real_state_machine_backs_the_resample_restore():
    # Exercises the production transition validator the checkpoint writer calls,
    # not a monkeypatched stand-in: the edge must be authorized ...
    ok, reason = pipeline_state.validate_stage_transition(
        "precommit_failed", "critic_checked"
    )
    assert ok is True
    assert reason == "precommit_seed_resample_restore"
    # ... the destination's gate allowlist must drop the failed precommit gate
    # (otherwise route_policy would send the generation back to execute_workers
    # and the resample would never run) ...
    assert "precommit_eval" not in pipeline_state.STAGE_GATE_ALLOWLIST["critic_checked"]
    assert pipeline_state.NEXT_TOOL_BY_STAGE["critic_checked"] == "run_precommit_eval"
    # ... and the restore must not be treated as a rework reset, so the
    # consumed precommit_rework_count unit and precommit_attempt survive.
    assert (
        pipeline_state.is_rework_reset_transition("precommit_failed", "critic_checked")
        is False
    )


# ── pool-leg decisive channel (2026-10-28 audit) ────────────────────
# The fresh-seed round cannot rescue a candidate measurably worse than the
# pool's OWN weakest member: v634 round1 pool 10W-14L (tail 0.27) stayed
# ambiguous (correctly resampled), while a 8W-16L pool leg (tail 0.076) is
# a proven admission regression and must abandon immediately instead of
# burning another full 48-match native round.
from tool_eval import _pool_leg_tail_p, _precommit_failure_is_decisive


def _pool_failure(pool_opponent, pool_wins, pool_losses, aggregate):
    agg_wins, agg_losses, ci_lower, ci_upper = aggregate
    return {
        "passed": False,
        "failure_class": "regression",
        "blockers": [
            {"reason": "did_not_beat_pool_weakest", "opponent": pool_opponent},
            {"reason": "aggregate_native_regression", "opponent": "aggregate"},
        ],
        "matchups": [
            {"opponent": pool_opponent, "wins": pool_wins, "losses": pool_losses, "draws": 0},
            {"opponent": "parent", "wins": 0, "losses": 0, "draws": 0},
        ],
        "total_wins": agg_wins, "total_losses": agg_losses, "total_draws": 0,
        "paired_bootstrap": {"ci_lower": ci_lower, "ci_upper": ci_upper},
    }


def test_pool_leg_tail_reads_the_blocked_opponent_matchup():
    # real v634 round1 shape: aggregate 25W-23L (ambiguous), pool 10W-14L
    r = _pool_failure("national_cloud_v173", 10, 14, (25, 23, -6000.0, 3000.0))
    assert round(_pool_leg_tail_p(r), 4) == 0.2706
    assert _precommit_failure_is_decisive(r) is False  # still gets the fresh-seed round


def test_significantly_losing_pool_leg_is_decisive():
    # pool leg 8W-16L (tail 0.076) while aggregate is merely below bar
    r = _pool_failure("national_cloud_v173", 8, 16, (25, 23, -6000.0, 3000.0))
    assert _pool_leg_tail_p(r) < 0.10
    assert _precommit_failure_is_decisive(r) is True


def test_missing_pool_blocker_leaves_aggregate_rule_in_charge():
    r = {
        "passed": False,
        "failure_class": "regression",
        "blockers": [{"reason": "did_not_beat_parent", "opponent": "national_cloud_v632"}],
        "matchups": [{"opponent": "national_cloud_v632", "wins": 3, "losses": 5, "draws": 0}],
        "total_wins": 25, "total_losses": 23,
        "paired_bootstrap": {"ci_lower": -6000.0, "ci_upper": 3000.0},
    }
    assert _pool_leg_tail_p(r) is None
    assert _precommit_failure_is_decisive(r) is False


# ── CI channel needs bootstrap-thickness (2026-10-30 −1·SE band) ─────
# v641 round1 measured ci_upper=-8.1 chips with SE=2167.9 (std 13711.1,
# n=40): the bare ci_upper<0 rule called THAT decisive and forced the
# expensive repair lane; a 400-run resample flips the verdict with p~0.37.
# The band requires ci_upper < -1.0*SE. Full 50-report replay: exactly one
# dispatch flips (v641_round1), zero collateral.


def _ci_failure(ci_lower, ci_upper, std=None, samples=None, wins=15, losses=25):
    agg = {
        "wins": wins, "losses": losses, "draws": 0,
        "net_chips_ci": [ci_lower, ci_upper],
        "samples": samples or (wins + losses),
    }
    if std is not None:
        agg["net_chips_std"] = std
        agg["net_chips_samples"] = samples or (wins + losses)
    return {"passed": False, "failure_class": "regression",
            "blockers": [{"reason": "aggregate_native_regression", "opponent": "aggregate"}],
            "matchups": [], "aggregate": agg}


def test_ci_just_below_zero_with_known_se_is_ambiguous():
    # the real v641 round1 numbers
    r = _ci_failure(-8090.4, -8.1, std=13711.1, samples=40)
    assert _precommit_failure_is_decisive(r) is False  # SE=2167.9, -8.1 > -2167.9


def test_ci_clearly_negative_beyond_one_se_stays_decisive():
    # 2+ SE below zero: beyond bootstrap luck
    r = _ci_failure(-9000.0, -5000.0, std=13711.1, samples=40)
    assert _precommit_failure_is_decisive(r) is True  # -5000 < -2167.9


def test_missing_std_falls_back_to_original_ci_rule():
    # legacy payloads without std/net_chips_samples keep historical behavior
    r = _ci_failure(-8090.4, -8.1)  # no std
    assert _precommit_failure_is_decisive(r) is True  # bare ci_upper<0 applies
