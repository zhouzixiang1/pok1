"""Pipeline rate-budget priority in the saturator (2026-10-23).

Observed live (2026-10-23 18:46–19:08): the fill lane's local rate-limit
pause is capped at 30s while the pipeline's durable ``service_unavailable``
cooldown walks 8→120s, so under sustained GLM 1302 pressure the saturator
relaunched first, re-tripped the account request-rate ceiling, and three
consecutive master attempts (~2M scout tokens each) died mid-stream —
v605 frozen at ``direction_audited`` while 94% of tokens went to filler.

Contract:

* ``_pipeline_rate_block_state`` recognises ONLY an active
  ``service_unavailable`` durable pause (pipeline-scoped by the 2026-10-18
  amplifier fix — the saturator never persists this pause itself) and
  parses its auto-resume timestamp; quota windows, inactive records, and
  foreign shapes do not count.
* ``saturator_rate_preempt_n`` clears ALL in-flight fill packets, once per
  rate-block episode, only after the episode has persisted at least the
  shared ``POK_LLM_SATURATOR_PREEMPT_AFTER_SEC`` threshold.
* The yield grace is env-tunable (``POK_LLM_SATURATOR_PIPELINE_RATE_YIELD_SEC``,
  default 90s) and the launch gate honours the armed yield window.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

import llm_saturator
from llm_saturator import (
    _pipeline_rate_block_state,
    _pipeline_rate_yield_grace_sec,
    _poll_pipeline_rate_block,
    saturator_rate_preempt_n,
)


def _active_pause(category: str = "service_unavailable", resume_in_sec: float = 60.0):
    return {
        "active": True,
        "category": category,
        "auto_resume_at": (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=resume_in_sec)
        ).isoformat(),
    }


class TestRateBlockState:
    def test_active_service_unavailable_blocks_with_resume_epoch(self):
        blocked, resume_ts = _pipeline_rate_block_state(_active_pause())
        assert blocked is True
        assert time.time() - 60 < resume_ts <= time.time() + 60.001

    def test_inactive_pause_does_not_block(self):
        pause = _active_pause()
        pause["active"] = False
        assert _pipeline_rate_block_state(pause) == (False, 0.0)

    def test_quota_window_is_not_frequency_class(self):
        # 1308 quota windows carry their own long stand-down in
        # _note_saturator_provider_failure; the yield must not double-fire.
        assert _pipeline_rate_block_state(_active_pause("quota_429")) == (False, 0.0)

    def test_missing_store_and_foreign_shapes_do_not_block(self):
        assert _pipeline_rate_block_state(None) == (False, 0.0)
        assert _pipeline_rate_block_state({}) == (False, 0.0)
        assert _pipeline_rate_block_state("nope") == (False, 0.0)

    def test_unparseable_resume_stays_blocked_with_zero_epoch(self):
        pause = _active_pause()
        pause["auto_resume_at"] = "not-a-timestamp"
        blocked, resume_ts = _pipeline_rate_block_state(pause)
        assert blocked is True
        assert resume_ts == 0.0

    def test_long_expired_resume_is_stale_not_blocked(self):
        # Audit yield-1: load_llm_pause never reconciles an expired record —
        # without this guard the yield re-armed now+30s every tick forever.
        pause = _active_pause(resume_in_sec=-7200.0)
        blocked, _ = _pipeline_rate_block_state(pause)
        assert blocked is False

    def test_recently_expired_resume_still_blocks_for_the_grace_window(self):
        pause = _active_pause(resume_in_sec=-30.0)  # grace 90s still running
        blocked, _ = _pipeline_rate_block_state(pause)
        assert blocked is True


class TestRatePreemptN:
    def test_fires_for_all_in_flight_after_threshold(self):
        assert (
            saturator_rate_preempt_n(
                rate_blocked=True,
                rate_block_age_sec=16.0,
                in_flight=5,
                already_done=False,
                min_age_sec=15.0,
            )
            == 5
        )

    def test_below_persistence_threshold_fires_zero(self):
        # Transient single-1302 blips must not kill healthy packets.
        assert (
            saturator_rate_preempt_n(
                rate_blocked=True,
                rate_block_age_sec=5.0,
                in_flight=5,
                already_done=False,
                min_age_sec=15.0,
            )
            == 0
        )

    def test_once_per_episode(self):
        kwargs = dict(
            rate_blocked=True, rate_block_age_sec=120.0, in_flight=4, min_age_sec=15.0
        )
        assert saturator_rate_preempt_n(already_done=False, **kwargs) == 4
        assert saturator_rate_preempt_n(already_done=True, **kwargs) == 0

    def test_not_blocked_or_nothing_in_flight_is_zero(self):
        assert (
            saturator_rate_preempt_n(
                rate_blocked=False,
                rate_block_age_sec=120.0,
                in_flight=4,
                already_done=False,
            )
            == 0
        )
        assert (
            saturator_rate_preempt_n(
                rate_blocked=True,
                rate_block_age_sec=120.0,
                in_flight=0,
                already_done=False,
            )
            == 0
        )


class TestYieldGrace:
    def test_default_grace_is_90s(self, monkeypatch):
        monkeypatch.delenv("POK_LLM_SATURATOR_PIPELINE_RATE_YIELD_SEC", raising=False)
        assert _pipeline_rate_yield_grace_sec() == 90.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("POK_LLM_SATURATOR_PIPELINE_RATE_YIELD_SEC", "45")
        assert _pipeline_rate_yield_grace_sec() == 45.0

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("POK_LLM_SATURATOR_PIPELINE_RATE_YIELD_SEC", "abc")
        assert _pipeline_rate_yield_grace_sec() == 90.0


class TestPollThrottle:
    def test_poll_reads_store_then_uses_cache(self, monkeypatch):
        import asyncio

        monkeypatch.setattr(llm_saturator, "_last_rate_pause_poll_ts", 0.0)
        monkeypatch.setattr(llm_saturator, "_last_rate_pause_poll", None)
        reads = []

        def fake_load():
            reads.append(1)
            return _active_pause()

        import llm_availability_store

        monkeypatch.setattr(llm_availability_store, "load_llm_pause", fake_load)
        ok1, blocked1, _ = asyncio.run(_poll_pipeline_rate_block())
        ok2, blocked2, _ = asyncio.run(_poll_pipeline_rate_block())
        assert ok1 is True and blocked1 is True
        assert ok2 is True and blocked2 is True
        assert len(reads) == 1  # second call served from the throttled cache

    def test_store_failure_keeps_episode_state(self, monkeypatch):
        # Audit yield-4: a failed read must NOT read as "cleared" — the
        # caller keeps its episode (poll_ok=False).
        import asyncio

        monkeypatch.setattr(llm_saturator, "_last_rate_pause_poll_ts", 0.0)
        monkeypatch.setattr(llm_saturator, "_last_rate_pause_poll", None)

        def boom():
            raise RuntimeError("store unreadable")

        import llm_availability_store

        monkeypatch.setattr(llm_availability_store, "load_llm_pause", boom)
        ok, blocked, _ = asyncio.run(_poll_pipeline_rate_block())
        assert ok is False
        assert blocked is False  # caller must ignore this, not clear on it


def test_launch_gate_source_honours_yield_window(monkeypatch):
    """The inner launch gate must break while the yield window is armed."""
    import inspect

    src = inspect.getsource(llm_saturator.run_llm_saturator)
    assert "_pipeline_rate_yield_until" in src
    # The gate breaks on the armed window ...
    assert "time.time() < _pipeline_rate_yield_until" in src
    # ... and the sleep branch parks on it instead of spinning, capped at
    # 20s so the loop wakes by the rate-preempt persistence threshold.
    assert "_pipeline_rate_yield_until - time.time()" in src
    assert "0.5), 20.0" in src
