"""Gradient cgroup-headroom gate for the LLM saturator (2026-10-10, w6).

The w6 window measured headroom < 300 MB for 76 consecutive minutes with
ZERO packets emitted: the flat ``POK_LLM_AIMD_CGROUP_HEADROOM_MB`` (300 MB)
floor parked the whole background lane while permits sat free. With
MemoryHigh raised to 2700M, sub-300MB windows are common under real burn.

Contract (``llm_saturator.saturator_may_launch``):

* headroom >= hard floor (default 300 MB): unchanged — launches proceed.
* soft floor (``POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB``, default 150 MB)
  <= headroom < hard floor: a bounded low-memory lane admits at most
  ``POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT`` (default 1) in-flight packets;
  excess is refused with ``low_memory_cgroup_soft``. Every OTHER gate
  (permits / claude children / RAM / quota pacing / restart fill) still
  applies on the way through.
* headroom < soft floor: refused ``low_memory_cgroup`` exactly as before.
* A soft floor configured above the hard floor is clamped to the hard
  floor, so the ``headroom >= hard floor`` behavior can never tighten.
"""

from __future__ import annotations

import pytest

import llm_concurrency
import llm_saturator
from llm_saturator import saturator_may_launch


@pytest.fixture(autouse=True)
def _isolate_gates(monkeypatch):
    monkeypatch.setattr(llm_saturator, "_saturator_provider_paused", lambda: False)
    monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 2048)
    monkeypatch.setattr(llm_saturator, "_min_free_mb", lambda: 512)
    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 0)
    monkeypatch.delenv("POK_LLM_AIMD_CGROUP_HEADROOM_MB", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT", raising=False)
    # Keep the permit/child gates permissive by default; individual tests
    # tighten them explicitly.
    monkeypatch.setattr(llm_concurrency, "llm_semaphore_has_capacity", lambda n=1: True)
    monkeypatch.setattr(llm_concurrency, "get_capacity", lambda: 8)
    yield


def _headroom(monkeypatch, mb):
    monkeypatch.setattr(
        llm_saturator, "_cgroup_memory_headroom_mb", lambda **_kw: mb
    )


# --- unit: env knob parsing -------------------------------------------------


def test_soft_floor_defaults_to_150_and_clamps_to_hard_floor():
    assert llm_saturator._cgroup_memory_soft_floor_mb() == 150
    assert llm_saturator._low_memory_inflight_cap() == 1


def test_soft_floor_env_and_clamping(monkeypatch):
    monkeypatch.setenv("POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB", "250")
    assert llm_saturator._cgroup_memory_soft_floor_mb() == 250
    # Floor above the hard floor is clamped to the hard floor (env floor
    # 999 vs default min 300).
    monkeypatch.setenv("POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB", "999")
    assert llm_saturator._cgroup_memory_soft_floor_mb() == 300
    # Invalid values fall back to the defaults, never widen the gate.
    monkeypatch.setenv("POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB", "not-a-number")
    assert llm_saturator._cgroup_memory_soft_floor_mb() == 150
    monkeypatch.setenv("POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT", "-3")
    assert llm_saturator._low_memory_inflight_cap() == 0


# --- gate behavior -----------------------------------------------------------


def test_at_or_above_hard_floor_unchanged(monkeypatch):
    _headroom(monkeypatch, 300)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")
    # Well above the floor the soft lane never binds, even at high inflight.
    _headroom(monkeypatch, 1200)
    ok, reason = saturator_may_launch(in_flight=3, soft_cap=4)
    assert (ok, reason) == (True, "ok")


def test_soft_band_admits_one_packet_then_caps(monkeypatch):
    _headroom(monkeypatch, 299)  # default band [150, 300)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")
    ok, reason = saturator_may_launch(in_flight=1, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup_soft")
    # At the soft floor boundary itself (inclusive).
    _headroom(monkeypatch, 150)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")


def test_below_soft_floor_refuses_as_before(monkeypatch):
    _headroom(monkeypatch, 149)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup")
    # Even with inflight budget left, and even with the low-memory inflight
    # cap raised: below the soft floor the hard refusal is absolute.
    monkeypatch.setenv("POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT", "4")
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup")


def test_low_memory_inflight_env_raises_band_ceiling(monkeypatch):
    _headroom(monkeypatch, 200)
    monkeypatch.setenv("POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT", "2")
    ok, reason = saturator_may_launch(in_flight=1, soft_cap=4)
    assert (ok, reason) == (True, "ok")
    ok, reason = saturator_may_launch(in_flight=2, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup_soft")
    # Cap 0 restores the pre-gradient hard refusal across the whole band.
    monkeypatch.setenv("POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT", "0")
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup_soft")


def test_soft_floor_env_moves_the_hard_boundary(monkeypatch):
    _headroom(monkeypatch, 200)
    monkeypatch.setenv("POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB", "250")
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup")
    _headroom(monkeypatch, 260)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")


def test_soft_floor_above_hard_floor_never_tightens(monkeypatch):
    # floor 999 clamps to the 300 hard floor: >= 300 stays a full pass.
    monkeypatch.setenv("POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB", "999")
    _headroom(monkeypatch, 350)
    ok, reason = saturator_may_launch(in_flight=5, soft_cap=6)
    assert (ok, reason) == (True, "ok")


def test_other_gates_still_refuse_inside_the_soft_band(monkeypatch):
    _headroom(monkeypatch, 200)  # inside the band: lane is open ...

    # ... but permits still bind.
    monkeypatch.setattr(llm_concurrency, "llm_semaphore_has_capacity", lambda n=1: False)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "no_permit")

    # ... but the claude-children RAM guard still binds.
    monkeypatch.setattr(llm_concurrency, "llm_semaphore_has_capacity", lambda n=1: True)
    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 9)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "claude_children")

    # ... but the host MemAvailable gate still binds.
    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 0)
    monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 300)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory")
