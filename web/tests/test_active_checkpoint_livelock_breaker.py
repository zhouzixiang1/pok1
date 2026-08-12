"""Regression tests for the cross-cycle active-checkpoint livelock breaker.

The breaker (orchestrator_loop_phases.py, 2026-08-12) force-abandons a
generation after N consecutive active-checkpoint cycles stuck at the same
(version, stage), using the reason prefix
``infrastructure_exhausted:active_checkpoint_livelock (...)``.

This reason must be admitted by ``generic_abandon_block`` at every disposable
stage (pipeline_state.py ``broad_infra_stages``) so the breaker can escape a
directive-path livelock at ANY stage, not just direction_audited. These tests
lock that invariant in: if a future change narrows ``broad_infra_stages`` or
renames the prefix, the breaker would silently fail to fire and the livelock
class (v170 spun 36h) would return.
"""
import pytest

import pipeline_state


# Stages where the breaker must be able to abandon (every disposable stage that
# a directive-path livelock could spin at).
_DISPOSABLE_STAGES = [
    "selected", "preparing", "prepared", "crossover_running",
    "direction_audited", "master_planned", "workers_done",
    "quality_failed", "quality_passed", "reviewed", "critic_checked",
    "precommit_failed", "repair_planned", "rework_running", "official_failed",
]
_NEVER_DISPOSABLE_STAGES = ["verified", "publishing", "archived"]
_BREAKER_REASON = (
    "infrastructure_exhausted:active_checkpoint_livelock (5 cycles at stage)"
)


def _valid_route(_checkpoint):
    """Stand-in for route_policy on a well-formed epoch-bound checkpoint."""
    return {"intent": "advance", "next_tool": "run_master"}


def test_breaker_reason_admitted_at_every_disposable_stage(monkeypatch):
    """The breaker's reason prefix must pass generic_abandon_block at all
    disposable stages (the breaker fires at whatever stage the livelock spins).
    """
    monkeypatch.setattr(pipeline_state, "route_policy", _valid_route)
    for stage in _DISPOSABLE_STAGES:
        ck = {"stage": stage, "next_v": 999, "source_v": 1}
        refusal = pipeline_state.generic_abandon_block(ck, reason=_BREAKER_REASON)
        assert refusal is None, (
            f"breaker reason must be admitted at disposable stage {stage}; "
            f"got refusal {refusal}"
        )


def test_breaker_reason_still_blocked_at_never_disposable_stages(monkeypatch):
    """Publication/certification/finalization stages remain non-disposable:
    the breaker must NOT be able to abandon there."""
    monkeypatch.setattr(pipeline_state, "route_policy", _valid_route)
    for stage in _NEVER_DISPOSABLE_STAGES:
        ck = {"stage": stage, "next_v": 999, "source_v": 1}
        refusal = pipeline_state.generic_abandon_block(ck, reason=_BREAKER_REASON)
        assert refusal is not None and refusal.get("blocked"), (
            f"never-disposable stage {stage} must stay blocked for the breaker reason"
        )


def test_breaker_reason_prefix_is_infrastructure_exhausted():
    """Guard against accidentally narrowing the reason: the breaker relies on
    the broad ``infrastructure_exhausted:`` admission."""
    assert _BREAKER_REASON.startswith("infrastructure_exhausted:")
