"""RSS-aware soft-band admission for the LLM saturator (2026-10-11).

The 2026-10-10 gradient opened a bounded low-memory lane for cgroup headroom
in ``[SOFT_FLOOR, HARD_FLOOR)`` but was deployed dormant
(``POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT=0``): one claude child is
~200-300 MB RSS, so admitting a packet into a 150-300 MB band would land the
service cgroup past ``memory.high`` straight into kernel-reclaim throttle
(native 60s decision budget at risk).

2026-10-11 gate (``llm_saturator.saturator_may_launch``): the lane's
admission line now charges the admitted stream's OWN RSS::

    headroom - STREAM_RSS_MB >= SOFT_FLOOR_MB - OVERSHOOT_MB

* ``POK_LLM_SATURATOR_STREAM_RSS_MB`` (default 250 — measured claude-child
  RSS) is subtracted from the CURRENT headroom, so admission can never
  pretend the new stream is free.
* ``POK_LLM_SATURATOR_BAND_OVERSHOOT_MB`` (default 0) is the explicit cap on
  how far a band admission may dip below the soft floor (and therefore
  potentially past ``memory.high``). 0 = no dip at all.
* A band refusal due to the RSS line gets its own typed reason
  ``low_memory_cgroup_stream_rss`` (distinct from the inflight-cap refusal
  ``low_memory_cgroup_soft``), so journal lines distinguish "not enough
  headroom for the stream itself" from "lane occupancy full".

Effect on the observed w6 in-band headroom range (203-252 MB, 2026-10-10
measurement): every value refuses under the committed defaults
(203-250=-47 .. 252-250=2, both < soft floor 150) — the lane stays closed
exactly as in the dormant deploy, but with an honest, greppable reason.

Three tiers (defaults, hard floor 300 / soft floor 150 / RSS 250 / overshoot 0):

* headroom < 150 -> ``low_memory_cgroup`` (unchanged).
* 150 <= headroom < 300 -> ``low_memory_cgroup_stream_rss`` (the lane needs
  headroom >= soft_floor + stream_rss = 400, above the hard floor, so the
  defaults keep the band closed — nothing ever crosses memory.high).
* headroom >= 300 -> ``ok`` (above-band launches were never RSS-charged and
  still pass every other gate).
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
    monkeypatch.setattr(llm_concurrency, "llm_semaphore_has_capacity", lambda n=1: True)
    monkeypatch.setattr(llm_concurrency, "get_capacity", lambda: 8)
    monkeypatch.delenv("POK_LLM_AIMD_CGROUP_HEADROOM_MB", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_STREAM_RSS_MB", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_BAND_OVERSHOOT_MB", raising=False)


def _headroom(monkeypatch, mb):
    monkeypatch.setattr(
        llm_saturator, "_cgroup_memory_headroom_mb", lambda **_kw: mb
    )


# --- env knob parsing ---------------------------------------------------------


def test_stream_rss_and_overshoot_defaults_and_spellings_20261011(monkeypatch):
    assert llm_saturator._saturator_stream_rss_mb() == 250
    assert llm_saturator._saturator_band_overshoot_mb() == 0
    monkeypatch.setenv("POK_LLM_SATURATOR_STREAM_RSS_MB", "300")
    assert llm_saturator._saturator_stream_rss_mb() == 300
    monkeypatch.setenv("POK_LLM_SATURATOR_STREAM_RSS_MB", "not-a-number")
    assert llm_saturator._saturator_stream_rss_mb() == 250
    monkeypatch.setenv("POK_LLM_SATURATOR_STREAM_RSS_MB", "-20")
    assert llm_saturator._saturator_stream_rss_mb() == 0
    monkeypatch.setenv("POK_LLM_SATURATOR_BAND_OVERSHOOT_MB", "150")
    assert llm_saturator._saturator_band_overshoot_mb() == 150
    monkeypatch.setenv("POK_LLM_SATURATOR_BAND_OVERSHOOT_MB", "bogus")
    assert llm_saturator._saturator_band_overshoot_mb() == 0


# --- three tiers under the committed defaults ---------------------------------


def test_below_soft_floor_refuses_unchanged_20261011(monkeypatch):
    _headroom(monkeypatch, 149)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup")


def test_band_under_defaults_refuses_with_stream_rss_reason_20261011(monkeypatch):
    # The whole band needs headroom >= 150 + 250 = 400, above the 300 hard
    # floor: the defaults keep the lane closed with the typed RSS reason.
    for mb in (150, 203, 252, 299):
        _headroom(monkeypatch, mb)
        ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
        assert (ok, reason) == (False, "low_memory_cgroup_stream_rss"), mb
    # Even with the inflight lane wide open, the RSS line refuses first.
    monkeypatch.setenv("POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT", "4")
    _headroom(monkeypatch, 250)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup_stream_rss")


def test_at_or_above_hard_floor_unchanged_20261011(monkeypatch):
    _headroom(monkeypatch, 300)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")
    _headroom(monkeypatch, 2000)
    ok, reason = saturator_may_launch(in_flight=3, soft_cap=4)
    assert (ok, reason) == (True, "ok")


# --- the RSS admission line itself --------------------------------------------


def test_band_admission_line_charges_stream_rss_20261011(monkeypatch):
    # Widen the hard floor so the admission boundary is INSIDE the band:
    # band [150, 600), line = headroom - 250 >= 150 -> headroom >= 400.
    monkeypatch.setenv("POK_LLM_AIMD_CGROUP_HEADROOM_MB", "600")
    _headroom(monkeypatch, 399)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup_stream_rss")
    _headroom(monkeypatch, 400)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")
    # Stream-RSS budget tightened: line moves to headroom >= 150 + 300.
    monkeypatch.setenv("POK_LLM_SATURATOR_STREAM_RSS_MB", "300")
    _headroom(monkeypatch, 449)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup_stream_rss")
    _headroom(monkeypatch, 450)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")


def test_inflight_cap_still_binds_after_rss_line_passes_20261011(monkeypatch):
    monkeypatch.setenv("POK_LLM_AIMD_CGROUP_HEADROOM_MB", "600")
    _headroom(monkeypatch, 500)  # 500 - 250 = 250 >= 150: RSS line passes.
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")
    ok, reason = saturator_may_launch(in_flight=1, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup_soft")
    # Cap 0 keeps the whole band closed via the cap, after the RSS line.
    monkeypatch.setenv("POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT", "0")
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup_soft")


def test_overshoot_cap_moves_the_line_exactly_20261011(monkeypatch):
    monkeypatch.setenv("POK_LLM_AIMD_CGROUP_HEADROOM_MB", "600")
    # OVERSHOOT=150: line = headroom >= soft_floor + rss - overshoot = 250.
    monkeypatch.setenv("POK_LLM_SATURATOR_BAND_OVERSHOOT_MB", "150")
    _headroom(monkeypatch, 249)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup_stream_rss")
    _headroom(monkeypatch, 250)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")
    # OVERSHOOT=250: the line reaches the soft floor itself; the admitted
    # stream's projected post-launch headroom may dip to -100 — exactly the
    # configured 250 MB below the soft floor (100 of them past memory.high).
    monkeypatch.setenv("POK_LLM_SATURATOR_BAND_OVERSHOOT_MB", "250")
    _headroom(monkeypatch, 150)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (True, "ok")
    _headroom(monkeypatch, 149)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "low_memory_cgroup")


def test_w6_measured_band_range_all_refuses_under_defaults_20261011(monkeypatch):
    """The observed 203-252 MB headroom window stays closed, honestly.

    This is the exact w6 measurement that motivated the dormant deploy: any
    admission there would cross memory.high by up to 47 MB (at 203 MB) after
    the ~250 MB stream lands. The RSS-aware line refuses each value with the
    dedicated reason instead of silently overshooting.
    """
    for mb in range(203, 253, 7):
        _headroom(monkeypatch, mb)
        ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
        assert (ok, reason) == (False, "low_memory_cgroup_stream_rss"), mb
