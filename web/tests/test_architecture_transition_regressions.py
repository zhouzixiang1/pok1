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
