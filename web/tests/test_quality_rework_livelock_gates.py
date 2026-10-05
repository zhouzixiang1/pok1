"""Quality/review rework livelock double gate (P4, 2026-10-05).

v511 wedged for 11 rounds (08:41-10:09) between two unsatisfiable prompts:
the quality contract REQUIRED AST checks whose mechanisms the reviewer
feedback simultaneously demanded be REMOVED, and there was no round ceiling
for quality/review rework (only precommit/official had one), so the loop
burned Worker leases forever.

Two gates (fail-closed, no semantic loosening):

* (a) Round ceiling — ``MAX_QUALITY_REWORK_ROUNDS`` (env
  ``POK_MAX_QUALITY_REWORK_ROUNDS``, default 3) bounds quality/review rework
  rounds through the persisted checkpoint field
  ``quality_rework_round_count``; over the cap the rework refuses to dispatch
  and the deterministic route canonically abandons with reason
  ``quality_rework_circuit_breaker``.
* (b) Contradiction detection — before dispatching repair Workers, the
  intersection of Required AST checks with mechanisms the reviewer feedback
  EXPLICITLY demands removed is computed; a non-empty intersection refuses
  dispatch (``REPAIR_CONTRACT_CONTRADICTORY``, reason token
  ``repair_contract_contradictory``) instead of burning the lease on an
  unsatisfiable contract.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import tool_planning_worker  # noqa: F401  (parent-first import order)
import tool_planning_worker_phases_rework as phases_rework
from tool_planning_quality_repair_targets import (
    _repair_contract_contradictions,
)


# --- (b) contradiction detector ----------------------------------------------


def test_contradiction_detector_flags_required_check_removal_demand():
    tasks = [
        {
            "worker_id": "w1",
            "checks_required": ["delayed_probe_line_reachability"],
            "repair_contract": {
                "required_checks": ["sample_counted_candidate_batch"],
            },
        }
    ]
    feedback = (
        "REJECTED. The delayed probe path is unreachable dead weight; "
        "remove the delayed probe mechanism entirely and simplify."
    )
    contradictions = _repair_contract_contradictions(tasks, feedback)
    assert [c["check"] for c in contradictions] == [
        "delayed_probe_line_reachability"
    ]
    assert contradictions[0]["feedback_lines"]


def test_contradiction_detector_reads_contract_required_checks():
    tasks = [
        {
            "worker_id": "w2",
            "repair_contract": {
                "required_checks": ["donk_line_reachability"],
            },
        }
    ]
    feedback = "Delete the donk branch; it double-counts the probe axis."
    contradictions = _repair_contract_contradictions(tasks, feedback)
    assert [c["check"] for c in contradictions] == ["donk_line_reachability"]


def test_contradiction_detector_ignores_negated_and_unrelated_text():
    tasks = [
        {
            "worker_id": "w3",
            "checks_required": ["donk_line_reachability"],
        }
    ]
    # Negated removal is a preservation demand, not a removal demand.
    assert (
        _repair_contract_contradictions(
            tasks, "Do not remove the donk mechanism under any circumstances."
        )
        == []
    )
    # Removal of an unrelated mechanism must not trip the gate.
    assert (
        _repair_contract_contradictions(
            tasks, "Remove the legacy telemetry shim from policy.py."
        )
        == []
    )
    # No feedback / no required checks -> nothing to contradict.
    assert _repair_contract_contradictions(tasks, "") == []
    assert _repair_contract_contradictions([], "Remove the donk mechanism.") == []
    assert _repair_contract_contradictions(None, "Remove the donk mechanism.") == []


# --- 评审次要项：同段共现不得误报/漏报（子句级归因） ------------------------


def test_contradiction_detector_clause_level_no_cross_attribution():
    """A negation in one clause must neither hide nor fake another clause's
    removal demand (review: line-level attribution misfires when both live
    in the same line/sentence group)."""

    tasks = [
        {
            "worker_id": "w4",
            "checks_required": [
                "donk_line_reachability",
                "delayed_probe_line_reachability",
            ],
        }
    ]
    feedback = (
        "The donk branch is fine as-is; do not touch it. "
        "Also remove the stale delayed_probe shim."
    )
    contradictions = _repair_contract_contradictions(tasks, feedback)
    assert [c["check"] for c in contradictions] == [
        "delayed_probe_line_reachability"
    ]


def test_contradiction_detector_negated_clause_does_not_hide_later_removal():
    tasks = [
        {
            "worker_id": "w5",
            "checks_required": ["sample_counted_candidate_batch"],
        }
    ]
    feedback = (
        "Do not remove the outer guard. "
        "Remove sample_counted_candidate_batch instead."
    )
    contradictions = _repair_contract_contradictions(tasks, feedback)
    assert [c["check"] for c in contradictions] == [
        "sample_counted_candidate_batch"
    ]


# --- (a) round ceiling gate --------------------------------------------------


def _gate(ckpt, rework_kind):
    return phases_rework._quality_rework_round_gate_payload(
        ckpt, rework_kind, next_v=12, source_v=11, tasks=[]
    )


def test_round_gate_trips_only_over_cap():
    ckpt = {"stage": "quality_failed", "quality_rework_round_count": 3}
    payload = _gate(ckpt, "quality_repair")
    assert payload is not None
    assert payload["error"] == "QUALITY_REWORK_CIRCUIT_BREAKER"
    assert payload["quality_rework_round_count"] == 3
    assert payload["max_rework_rounds"] >= 1


def test_round_gate_allows_below_or_at_cap():
    assert _gate({"stage": "quality_failed"}, "quality_repair") is None
    assert (
        _gate(
            {"stage": "quality_failed", "quality_rework_round_count": 2},
            "quality_repair",
        )
        is None
    )
    assert (
        _gate(
            {"stage": "quality_failed", "quality_rework_round_count": 0},
            "review_repair",
        )
        is None
    )


def test_round_gate_ignores_precommit_and_official_rework():
    hot = {"stage": "precommit_failed", "quality_rework_round_count": 99}
    assert _gate(hot, "precommit_repair") is None
    hot_official = {"stage": "official_failed", "quality_rework_round_count": 99}
    assert _gate(hot_official, "official_repair") is None


def test_quality_rework_round_count_persists_in_checkpoint(tmp_path, monkeypatch):
    import evolution_infra
    from evolution_infra import (
        read_pipeline_checkpoint,
        write_pipeline_checkpoint,
    )

    # The checkpoint CAS resolves the state file through the
    # evolution_infra module globals at call time; retarget both and relax
    # the allocation authority to the synthetic versions under test.
    monkeypatch.setattr(
        evolution_infra, "RESULTS_DIR", tmp_path, raising=False
    )
    monkeypatch.setattr(
        evolution_infra,
        "PIPELINE_STATE_FILE",
        tmp_path / "pipeline_state.json",
        raising=False,
    )
    monkeypatch.setattr(
        evolution_infra,
        "checkpoint_allocation_authority",
        lambda **_kwargs: {
            "published_high_water": 11,
            "abandoned_receipt_floor": 0,
            "abandoned_receipt_head_digest": None,
            "allocation_floor": 11,
        },
    )

    written = write_pipeline_checkpoint(
        12,
        11,
        "repair_planned",
        quality_rework_round_count=2,
    )
    assert written
    checkpoint = read_pipeline_checkpoint()
    assert checkpoint["quality_rework_round_count"] == 2


# --- route-level abandon wiring ----------------------------------------------


def _drive_route(monkeypatch, payload, checkpoint, expected_reason):
    """Route-level abandon wiring with the REAL state-machine guard.

    The fake ``_do_abandon_generation`` consults the production
    ``pipeline_state.generic_abandon_block`` (the same call the real
    abandon path makes through ``_generic_abandon_stage_block``), so a
    reason that the guard refuses cannot silently "succeed" here — the
    route's forced call AND its one-shot generic fallback both see the
    real refusals (review follow-up: the previous always-success fake
    masked forced_abandon_reason_stage_not_allowed at rework stages).
    """

    import orchestrator
    from test_pipeline_state_machine import generic_abandon_block as strict_guard

    calls = []
    fake_execute = SimpleNamespace(
        handler=AsyncMock(
            return_value={"content": [{"type": "text", "text": json.dumps(payload)}]}
        )
    )

    def _guarded_abandon(reason="abandon_generation", **_identity):
        calls.append(reason)
        guard = strict_guard(
            dict(checkpoint, next_v=checkpoint["next_v"]),
            reason=reason,
            max_precommit_retries=3,
        )
        if guard is not None:
            return {**guard, "abandoned": False, "reason_called": reason}
        return {
            "abandoned": True,
            "reason": reason,
            "abandoned_v": checkpoint["next_v"],
        }

    async def _fake_abandon(reason="abandon_generation", **identity):
        return _guarded_abandon(reason, **identity)

    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator, "log_system_event", lambda *_a, **_k: None
    )
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "execute_workers"}),
    )
    monkeypatch.setitem(
        sys.modules, "tool_planning", SimpleNamespace(execute_workers=fake_execute)
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_bot_management",
        SimpleNamespace(
            _do_abandon_generation=_fake_abandon,
            expected_abandon_identity=lambda _checkpoint: {},
        ),
    )

    class _UI:
        def set_status(self, *_a):
            pass

        def log_history(self, *_a, **_k):
            pass

        def gen_cost_total(self, _a):
            return 0.0

    recovery = {"action": "resume", "checkpoint": dict(checkpoint)}
    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, _UI())
    )
    assert handled is True
    fake_execute.handler.assert_awaited_once_with(
        {"next_v": checkpoint["next_v"], "source_v": checkpoint["source_v"]}
    )
    return calls


# --- 评审阻塞项 1：断路器弃置 reason 必须真实通过 pipeline_state 守卫 ------
#
# forced_rules 不含这两个 reason 时，任何 stage 都会以
# forced_abandon_reason_stage_not_allowed 拒绝，路由的一次性 generic 兜底
# 又被 forward_only 拒（repair_planned/rework_running/reviewed/
# quality_passed/critic_checked）→ abandoned=False → 重派 execute_workers
# → 零进展死循环（review 实证的 stage×reason 矩阵）。

_BREAKER_STAGES = (
    "workers_done",
    "quality_failed",
    "quality_passed",
    "reviewed",
    "critic_checked",
    "repair_planned",
    "rework_running",
)
_BREAKER_REASONS = (
    "quality_rework_circuit_breaker",
    "repair_contract_contradictory",
)


@pytest.mark.parametrize("reason", _BREAKER_REASONS)
@pytest.mark.parametrize("stage", _BREAKER_STAGES)
def test_forced_abandon_guard_admits_quality_rework_breakers(stage, reason):
    """Direct guard matrix: both reasons must be admissible at every stage
    the rework preparation can emit them from (review: repair_planned /
    rework_running were refused pre-fix; the set aligns with the
    worker_terminal_abandon row plus the gate_repair-family stages)."""

    from test_pipeline_state_machine import generic_abandon_block as strict_guard

    checkpoint = {
        "next_v": 12,
        "source_v": 11,
        "stage": stage,
        "checkpoint_revision": 3,
        "workflow_run_id": "generation:12:workflow-v1",
        "gate_results": {},
    }
    block = strict_guard(checkpoint, reason=reason, max_precommit_retries=3)
    assert block is None, f"{reason} at {stage} blocked by real guard: {block}"


@pytest.mark.parametrize("reason_token,payload_error", (
    ("quality_rework_circuit_breaker", "QUALITY_REWORK_CIRCUIT_BREAKER"),
    ("repair_contract_contradictory", "REPAIR_CONTRACT_CONTRADICTORY"),
))
@pytest.mark.parametrize("stage", ("repair_planned", "rework_running"))
def test_route_abandon_reachable_with_real_guard_at_rework_stages(
    monkeypatch, stage, reason_token, payload_error
):
    """End-to-end route reachability with the REAL guard deciding the fake.

    Pre-fix, the forced call was refused (forced_abandon_reason_stage_not_
    allowed) and the one-shot generic fallback was refused again at these
    forward-only stages, leaving abandoned=False — the review's loop wedge.
    Post-fix the FIRST call carries the typed reason and succeeds; the
    generic fallback must never fire.
    """

    payload = (
        {
            "error": payload_error,
            "quality_rework_round_count": 3,
            "max_rework_rounds": 3,
        }
        if payload_error == "QUALITY_REWORK_CIRCUIT_BREAKER"
        else {
            "error": payload_error,
            "reason_token": reason_token,
            "contradictions": [
                {"check": "donk_line_reachability", "feedback_lines": ["..."]}
            ],
        }
    )
    checkpoint = {
        "stage": stage,
        "next_v": 12,
        "source_v": 11,
        "quality_rework_round_count": 3,
    }
    calls = _drive_route(monkeypatch, payload, checkpoint, reason_token)
    assert calls == [reason_token], (
        "expected exactly one successful forced abandon with the typed "
        f"reason, got {calls} (guard refused; generic fallback fired)"
    )


def test_deterministic_route_abandons_after_quality_rework_circuit_breaker(
    monkeypatch,
):
    _drive_route(
        monkeypatch,
        {
            "error": "QUALITY_REWORK_CIRCUIT_BREAKER",
            "quality_rework_round_count": 3,
            "max_rework_rounds": 3,
        },
        {
            "stage": "rework_running",
            "next_v": 12,
            "source_v": 11,
            "quality_rework_round_count": 3,
        },
        "quality_rework_circuit_breaker",
    )


def test_deterministic_route_abandons_after_repair_contract_contradiction(
    monkeypatch,
):
    _drive_route(
        monkeypatch,
        {
            "error": "REPAIR_CONTRACT_CONTRADICTORY",
            "reason_token": "repair_contract_contradictory",
            "contradictions": [
                {"check": "donk_line_reachability", "feedback_lines": ["..."]}
            ],
        },
        {
            "stage": "rework_running",
            "next_v": 12,
            "source_v": 11,
        },
        "repair_contract_contradictory",
    )


# --- phase-C source contract: both gates are consulted before dispatch -------


def test_phase_c_rework_consults_both_livelock_gates():
    import inspect

    source = inspect.getsource(
        phases_rework._execute_workers_phase_c_rework_preparation
    )
    assert "_quality_rework_round_gate_payload(" in source
    assert "_repair_contract_contradictions(" in source
