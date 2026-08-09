"""Regression guard for the precompute_runtime_influence quality-rework deadlock.

A candidate descended from a source that statically demonstrates
``precompute_runtime_influence`` (the source baseline records it as passed via
``import precompute``) used to "regress" it forever, because the typed runtime
probe hardcodes its dynamic state to False (there is no digest-bound precompute
counterfactual variant) and ``_apply_typed_runtime_probe`` overwrote the
static ``passed`` with that conservative False. The static-only baseline then
listed the check as passed, so every candidate failed the regression gate at
``evaluate_architecture_transition`` and entered an unbounded quality-rework
loop (this killed v15/v19 and blocked v20).

The fix preserves the static ``passed`` for the fail-closed "no variant"
checks (``_PROBE_FAIL_CLOSED_CHECKS``) so they do not fabricate a regression,
while ``selected_dynamic_failures`` still gates them via their recorded
``dynamic_passed`` evidence when they are the ledger-selected focus.
"""

import copy

import national_runtime_probe
import runtime_architecture_policy
from runtime_architecture_policy import RUNTIME_FLOOR_CHECKS

# Reuse the validated probe-sealing helpers from the probe test module.
from test_national_runtime_probe import (
    _passing_gate_probe,
    _seal_passing_repeatability_probe,
)


def _capabilities(*, precompute_static_passed: bool):
    """Minimal capability record. Only the runtime floor + precompute check;
    no advisory causal checks (their probe evidence is out of scope here)."""
    check_ids = sorted(set(RUNTIME_FLOOR_CHECKS) | {"precompute_runtime_influence"})
    checks = [
        {
            "check_id": check_id,
            "name": check_id,
            "passed": True,
            "required": check_id in RUNTIME_FLOOR_CHECKS,
            "guidance": check_id,
            "evidence": {},
        }
        for check_id in check_ids
    ]
    for item in checks:
        if item["check_id"] == "precompute_runtime_influence":
            item["passed"] = precompute_static_passed
    return {
        "schema_version": 2,
        "epoch": runtime_architecture_policy.ACTIVE_EPOCH,
        "conclusive": True,
        "ok": True,
        "outcome": "passed",
        "checks": checks,
        "checks_by_id": {item["check_id"]: item for item in checks},
        "required_checks": sorted(RUNTIME_FLOOR_CHECKS),
        "required_failures": [],
        "advisory_warnings": [],
        "infrastructure_failures": [],
    }


def _install(monkeypatch, *, source_precompute: bool, candidate_precompute: bool):
    """Wire the evaluator to static capabilities + a sealed passing probe.

    The typed probe (``_dynamic_probe_states``) still hardcodes
    ``precompute_runtime_influence`` to False; the fix ensures that value does
    not overwrite the candidate's static ``passed``.
    """
    source_cap = _capabilities(precompute_static_passed=source_precompute)
    candidate_cap = _capabilities(precompute_static_passed=candidate_precompute)

    seq = iter([source_cap, candidate_cap])
    monkeypatch.setattr(
        runtime_architecture_policy,
        "evaluate_national_capabilities",
        lambda _path: copy.deepcopy(next(seq)),
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "build_architecture_policy",
        lambda *_args, **_kwargs: {
            "effective_baseline_checks": {
                check_id: True
                for check_id in source_cap["checks_by_id"]
                if source_cap["checks_by_id"][check_id]["passed"]
            },
            "baseline_passed_checks": [
                check_id
                for check_id in sorted(source_cap["checks_by_id"])
                if source_cap["checks_by_id"][check_id]["passed"]
            ],
            "selected_focus": {"focus_id": "runtime_architecture", "required_checks": []},
        },
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: copy.deepcopy(
            _seal_passing_repeatability_probe(_passing_gate_probe())
        ),
    )
    return candidate_cap


def test_candidate_with_precompute_does_not_regress_it(monkeypatch, tmp_path):
    """A candidate that keeps ``import precompute`` must NOT regress
    ``precompute_runtime_influence`` even though the typed probe hardcodes its
    dynamic state to False. The transition passes (no fake regression)."""
    _install(monkeypatch, source_precompute=True, candidate_precompute=True)

    transition = runtime_architecture_policy.evaluate_architecture_transition(
        tmp_path,  # source (unused under the monkeypatch)
        tmp_path,  # candidate
    )

    assert transition["ok"] is True
    regression_ids = [item["check_id"] for item in transition["regressions"]]
    assert "precompute_runtime_influence" not in regression_ids


def test_candidate_that_drops_precompute_still_regresses(monkeypatch, tmp_path):
    """Genuine regression detection survives the fix: a candidate that drops
    ``import precompute`` (static False) still appears in regressions."""
    _install(monkeypatch, source_precompute=True, candidate_precompute=False)

    transition = runtime_architecture_policy.evaluate_architecture_transition(
        tmp_path,
        tmp_path,
    )

    assert transition["ok"] is False
    regression_ids = [item["check_id"] for item in transition["regressions"]]
    assert "precompute_runtime_influence" in regression_ids


# ---------------------------------------------------------------------------
# Preplan probe-driven regression must be advisory, not blocking (v107-v114 fix)
# ---------------------------------------------------------------------------


# A probe-driven (non-deterministic) capability check whose ``passed`` value is
# overwritten by the typed runtime probe at runtime (see ``_dynamic_probe_states``).
_PROBE_DRIVEN_CHECK = "delayed_probe_line_reachability"


def _capabilities_with_probe_check(*, probe_check_static_passed: bool):
    """Capability record including a probe-driven advisory causal check.

    Mirrors the parent baseline: the check is statically demonstrated (passed),
    but the typed runtime probe can still overwrite its ``passed`` to False when
    its baseline-deadline counterfactual misses under load — the exact v107-v114
    scenario where a verbatim copy of parent A "regresses" a check the parent
    itself fails the same probe on.
    """
    check_ids = sorted(
        set(RUNTIME_FLOOR_CHECKS)
        | {"precompute_runtime_influence", _PROBE_DRIVEN_CHECK}
    )
    checks = [
        {
            "check_id": check_id,
            "name": check_id,
            "required": check_id in RUNTIME_FLOOR_CHECKS,
            "passed": True,
            "guidance": check_id,
            "evidence": {},
        }
        for check_id in check_ids
    ]
    for item in checks:
        if item["check_id"] == _PROBE_DRIVEN_CHECK:
            item["passed"] = probe_check_static_passed
    return {
        "schema_version": 2,
        "epoch": runtime_architecture_policy.ACTIVE_EPOCH,
        "conclusive": True,
        "ok": True,
        "outcome": "passed",
        "checks": checks,
        "checks_by_id": {item["check_id"]: item for item in checks},
        "required_checks": sorted(RUNTIME_FLOOR_CHECKS),
        "required_failures": [],
        "advisory_warnings": [],
        "infrastructure_failures": [],
    }


def _failing_line_reachability_probe():
    """A probe whose ``delayed_probe`` line-reachability counterfactual misses.

    This makes ``_dynamic_probe_states`` return
    ``delayed_probe_line_reachability: False``, so
    ``_apply_typed_runtime_probe`` overwrites the candidate's static ``passed``
    to False — the non-deterministic failure that blocked every crossover
    v107-v114 because the parent baseline (copied verbatim) inherits the same
    timing miss. The probe is otherwise a valid candidate-class failure (not
    probe_infra), so it exercises the advisory-vs-blocking decision.
    """
    probe = _passing_gate_probe()
    # Flip the delayed_probe line counterfactual to a miss.
    probe["line_reachability"]["dimensions"]["delayed_probe"].update({
        "ok": False,
        "policy_changed": False,
    })
    probe["ok"] = False
    probe["failure_class"] = "candidate_contract"
    probe["issues"] = ["policy_baseline_not_published:delayed_probe_scenario"]
    return probe


def _install_probe_driven(
    monkeypatch,
    *,
    source_probe_check: bool,
    candidate_probe_check: bool,
    probe_fn,
):
    source_cap = _capabilities_with_probe_check(
        probe_check_static_passed=source_probe_check
    )
    candidate_cap = _capabilities_with_probe_check(
        probe_check_static_passed=candidate_probe_check
    )
    seq = iter([source_cap, candidate_cap])
    monkeypatch.setattr(
        runtime_architecture_policy,
        "evaluate_national_capabilities",
        lambda _path: copy.deepcopy(next(seq)),
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "build_architecture_policy",
        lambda *_args, **_kwargs: {
            "effective_baseline_checks": {
                check_id: True
                for check_id in source_cap["checks_by_id"]
                if source_cap["checks_by_id"][check_id]["passed"]
            },
            "baseline_passed_checks": [
                check_id
                for check_id in sorted(source_cap["checks_by_id"])
                if source_cap["checks_by_id"][check_id]["passed"]
            ],
            "selected_focus": {
                "focus_id": "runtime_architecture",
                "required_checks": [],
            },
        },
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: copy.deepcopy(probe_fn()),
    )
    return candidate_cap


def test_preplan_probe_driven_regression_is_advisory(monkeypatch, tmp_path):
    """At the preplan phase (crossover), a probe-driven regression must NOT
    block the transition. The candidate is a verbatim copy of parent A whose
    baseline statically demonstrates ``delayed_probe_line_reachability``; the
    typed runtime probe's non-deterministic miss must be recorded as advisory,
    not a blocking regression — exactly the v107-v114 deadlock scenario."""
    _install_probe_driven(
        monkeypatch,
        source_probe_check=True,
        candidate_probe_check=True,
        probe_fn=_failing_line_reachability_probe,
    )

    transition = runtime_architecture_policy.evaluate_architecture_transition(
        tmp_path,
        tmp_path,
        evaluation_phase=(
            runtime_architecture_policy.ARCHITECTURE_TRANSITION_PHASE_PREPLAN
        ),
    )

    assert transition["ok"] is True
    regression_ids = [item["check_id"] for item in transition["regressions"]]
    assert _PROBE_DRIVEN_CHECK not in regression_ids
    advisory_ids = [item["check_id"] for item in transition["preplan_probe_advisory"]]
    assert _PROBE_DRIVEN_CHECK in advisory_ids


def test_final_probe_driven_regression_still_blocks(monkeypatch, tmp_path):
    """At the final phase (quality gate / full candidate), the same probe-driven
    regression must remain a hard block. The advisory deferral is preplan-only."""
    _install_probe_driven(
        monkeypatch,
        source_probe_check=True,
        candidate_probe_check=True,
        probe_fn=_failing_line_reachability_probe,
    )

    transition = runtime_architecture_policy.evaluate_architecture_transition(
        tmp_path,
        tmp_path,
        evaluation_phase=(
            runtime_architecture_policy.ARCHITECTURE_TRANSITION_PHASE_FINAL
        ),
    )

    assert transition["ok"] is False
    regression_ids = [item["check_id"] for item in transition["regressions"]]
    assert _PROBE_DRIVEN_CHECK in regression_ids


def test_preplan_static_regression_still_blocks(monkeypatch, tmp_path):
    """A genuine static regression (candidate statically drops a capability the
    probe does not vouch for) must still block even at preplan — the advisory
    deferral applies only to probe-driven regressions, not real static drops.
    Uses ``precompute_runtime_influence`` (fail-closed: probe never vouches for
    it, so a static drop is a real regression at every phase)."""
    _install(monkeypatch, source_precompute=True, candidate_precompute=False)

    transition = runtime_architecture_policy.evaluate_architecture_transition(
        tmp_path,
        tmp_path,
        evaluation_phase=(
            runtime_architecture_policy.ARCHITECTURE_TRANSITION_PHASE_PREPLAN
        ),
    )

    assert transition["ok"] is False
    regression_ids = [item["check_id"] for item in transition["regressions"]]
    assert "precompute_runtime_influence" in regression_ids


def test_transition_always_publishes_observability_fields(monkeypatch, tmp_path):
    """The transition dict must always carry the observability fields the
    crossover rejection event + LLM feedback render, so a blocking cause is
    never hidden behind empty arrays (the v107-v114 observability gap). Both
    phases must publish ``typed_runtime_failures``/``selected_dynamic_failures``
    /``preplan_probe_advisory`` even when empty."""
    for phase in (
        runtime_architecture_policy.ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
        runtime_architecture_policy.ARCHITECTURE_TRANSITION_PHASE_FINAL,
    ):
        _install_probe_driven(
            monkeypatch,
            source_probe_check=True,
            candidate_probe_check=True,
            probe_fn=_passing_gate_probe,
        )
        transition = runtime_architecture_policy.evaluate_architecture_transition(
            tmp_path,
            tmp_path,
            evaluation_phase=phase,
        )
        for key in (
            "typed_runtime_failures",
            "selected_dynamic_failures",
            "preplan_probe_advisory",
            "runtime_probe",
        ):
            assert key in transition, f"phase {phase} missing observability key {key}"


def test_crossover_runtime_probe_summary_extracts_probe(monkeypatch, tmp_path):
    """``_crossover_runtime_probe_summary`` must surface the probe's failure
    class, repeatability, and concrete issues so an operator can distinguish an
    inherited probe-timing miss from a genuine regression (the v107-v114 fix)."""
    _install_probe_driven(
        monkeypatch,
        source_probe_check=True,
        candidate_probe_check=True,
        probe_fn=_failing_line_reachability_probe,
    )
    transition = runtime_architecture_policy.evaluate_architecture_transition(
        tmp_path,
        tmp_path,
        evaluation_phase=(
            runtime_architecture_policy.ARCHITECTURE_TRANSITION_PHASE_PREPLAN
        ),
    )

    import agent_review

    summary = agent_review._crossover_runtime_probe_summary(transition)
    assert summary["ok"] is False
    assert summary["failure_class"] == "candidate_contract"
    assert "policy_baseline_not_published:delayed_probe_scenario" in summary["issues"]


def test_preplan_probe_infra_failure_is_advisory(monkeypatch, tmp_path):
    """At preplan, a probe-infrastructure failure (probe_infra — the subprocess
    crashed/timed out under CPU contention) must be advisory, not blocking.
    This is the CROSSOVER_INFRASTRUCTURE_INCONCLUSIVE failure mode that blocked
    v128-v149 (22 generations): the probe returned infrastructure_failure class
    which the crossover path treated as a hard abort. Only the
    national_runtime_probe component is deferred; genuine non-probe infra still
    blocks."""
    _install_probe_driven(
        monkeypatch,
        source_probe_check=True,
        candidate_probe_check=True,
        probe_fn=lambda: _seal_passing_repeatability_probe(_passing_gate_probe()),
    )
    # Simulate a probe_infra failure by monkeypatching _apply_typed_runtime_probe
    # to return a conclusive=False capability with a national_runtime_probe infra
    # failure.
    from runtime_architecture_policy import _PROBE_FAIL_CLOSED_CHECKS

    original_apply = runtime_architecture_policy._apply_typed_runtime_probe

    def infra_probe_apply(capabilities, candidate, *, runtime_contract_ledger):
        merged, probe, infra = original_apply(
            capabilities, candidate,
            runtime_contract_ledger=runtime_contract_ledger,
        )
        # Force probe_infra outcome
        merged["conclusive"] = False
        merged["ok"] = False
        merged["outcome"] = "infrastructure_failure"
        merged["infrastructure_failures"] = [{
            "side": "system",
            "component": "national_runtime_probe",
            "failure_class": "internal_infrastructure",
            "issues": ["probe_infra_failure_under_load"],
        }]
        return merged, probe, merged["infrastructure_failures"]

    monkeypatch.setattr(
        runtime_architecture_policy,
        "_apply_typed_runtime_probe",
        infra_probe_apply,
    )

    transition = runtime_architecture_policy.evaluate_architecture_transition(
        tmp_path,
        tmp_path,
        evaluation_phase=(
            runtime_architecture_policy.ARCHITECTURE_TRANSITION_PHASE_PREPLAN
        ),
    )

    # At preplan the probe-infra failure is advisory: outcome is NOT
    # infrastructure_failure, ok is True, and the failure is recorded.
    assert transition["outcome"] != "infrastructure_failure"
    assert transition["ok"] is True
    assert transition["conclusive"] is True
    advisory_ids = [item["reason"] for item in transition["preplan_probe_advisory"]]
    assert "infrastructure_failure_deferred_at_preplan" in advisory_ids
