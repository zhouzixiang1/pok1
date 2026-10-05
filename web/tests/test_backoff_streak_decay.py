"""P3 (2026-10-05): exponential-backoff streaks must not be zeroed by success.

F10 evidence: 19:44:20→19:46:07 the saturator's ``_rate_limit_streak`` climbed
to 4 and four successful session launches immediately zeroed it, restarting
the 8s cooldown sawtooth (orchestrator journal showed 15/31/30/62s zig-zag
instead of climbing to the 120s cap).  Under sustained GLM 1302 pressure a
launched session is not proof the pressure is gone.

Contracts under test:

* ``llm_saturator._note_saturator_launch_success`` decays the frequency
  streak instead of zeroing it: full reset only after a >=10-minute quiet
  gap since the last 1302-class failure, otherwise the streak halves.
* ``llm_availability_store.persist_llm_pause`` keeps the SERVICE_UNAVAILABLE
  ``occurrences`` count across an auto-resumed pause when the same category
  recurs inside a 30-minute sliding window, so the durable cooldown keeps
  walking the shared exponential curve toward the 120s cap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import evolution_infra
from llm_availability import (
    SERVICE_UNAVAILABLE,
    classify_llm_availability,
    service_unavailable_cooldown_seconds,
)
import llm_availability_store as store
import llm_saturator as saturator


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.delenv(store.RESUME_ENV, raising=False)
    return tmp_path


def _service_issue():
    issue = classify_llm_availability(
        ["HTTP 529 service unavailable"],
        statuses=[529],
    )
    assert issue is not None
    assert issue.category == SERVICE_UNAVAILABLE
    return issue


def _rate_limit_error():
    return RuntimeError(
        "Request rejected (429) · [1302][您的账户已达到速率限制，请您控制请求频率]"
    )


# ── saturator streak decay ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_saturator_streak():
    # Reset every module-level pause/streak marker this module writes:
    # _note_saturator_provider_failure arms _quota_pause_until into the
    # future, and a leftover future pause changes other suites' saturator
    # launch-loop behaviour (observed: test_saturator_pipeline_liveness_gate
    # failures when run after this file).
    saturator._rate_limit_streak = 0
    saturator._rate_limit_streak_last_failure_ts = 0.0
    saturator._quota_pause_until = 0.0
    saturator._fail_pause_until = 0.0
    saturator._fail_streak = 0
    yield
    saturator._rate_limit_streak = 0
    saturator._rate_limit_streak_last_failure_ts = 0.0
    saturator._quota_pause_until = 0.0
    saturator._fail_pause_until = 0.0
    saturator._fail_streak = 0


def test_success_no_longer_zeroes_the_streak():
    for _ in range(4):
        saturator._note_saturator_provider_failure(_rate_limit_error())
    assert saturator._rate_limit_streak == 4
    saturator._note_saturator_launch_success()
    assert saturator._rate_limit_streak == 2  # halved, not zeroed (F10)


def test_decay_keeps_climbing_under_sustained_pressure():
    saturator._note_saturator_provider_failure(_rate_limit_error())
    saturator._note_saturator_provider_failure(_rate_limit_error())
    saturator._note_saturator_launch_success()  # 2 -> 1
    assert saturator._rate_limit_streak == 1
    saturator._note_saturator_provider_failure(_rate_limit_error())  # 1 -> 2
    assert saturator._rate_limit_streak == 2
    saturator._note_saturator_launch_success()  # 2 -> 1
    saturator._note_saturator_provider_failure(_rate_limit_error())  # 1 -> 2
    saturator._note_saturator_provider_failure(_rate_limit_error())  # 2 -> 3
    saturator._note_saturator_launch_success()  # 3 -> 1
    saturator._note_saturator_provider_failure(_rate_limit_error())
    saturator._note_saturator_provider_failure(_rate_limit_error())
    saturator._note_saturator_provider_failure(_rate_limit_error())
    saturator._note_saturator_provider_failure(_rate_limit_error())
    saturator._note_saturator_provider_failure(_rate_limit_error())  # 1+5 -> 6
    assert saturator._rate_limit_streak == 6
    assert service_unavailable_cooldown_seconds(saturator._rate_limit_streak) == 120


def test_quiet_gap_fully_resets_the_streak(monkeypatch):
    saturator._note_saturator_provider_failure(_rate_limit_error())
    saturator._note_saturator_provider_failure(_rate_limit_error())
    assert saturator._rate_limit_streak == 2

    real_time = saturator.time.time

    def later():
        return (
            real_time()
            + saturator._RATE_LIMIT_STREAK_DECAY_SEC
            + 1.0
        )

    monkeypatch.setattr(saturator.time, "time", later)
    saturator._note_saturator_launch_success()
    assert saturator._rate_limit_streak == 0


def test_zero_streak_stays_zero_on_success():
    saturator._note_saturator_launch_success()
    assert saturator._rate_limit_streak == 0


# ── durable-store occurrences sliding window ──────────────────────────────


def test_occurrences_survive_pause_clear_inside_window(isolated_store):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    first = store.persist_llm_pause(_service_issue(), now=now)
    assert first["occurrences"] == 1
    second = store.persist_llm_pause(
        _service_issue(), now=now + timedelta(seconds=4)
    )
    assert second["occurrences"] == 2
    # auto_resume_at = now+4+16s; let it elapse so the record clears.
    assert store.active_llm_pause(now=now + timedelta(seconds=4 + 16)) is None

    # P3: recurrence 60s after the clear is the same pressure episode
    # (inside the 30-minute sliding window) — the count must carry so the
    # cooldown keeps climbing toward the 120s cap instead of sawtoothing
    # from 8s again.
    carried = store.persist_llm_pause(
        _service_issue(), now=now + timedelta(seconds=4 + 16 + 60)
    )
    assert carried["occurrences"] == 3
    assert store.pause_wait_seconds(
        carried, now=now + timedelta(seconds=4 + 16 + 60)
    ) == pytest.approx(32.0)


def test_occurrences_reset_after_window_expires(isolated_store):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    first = store.persist_llm_pause(_service_issue(), now=now)
    second = store.persist_llm_pause(
        _service_issue(), now=now + timedelta(seconds=4)
    )
    assert second["occurrences"] == 2
    assert store.active_llm_pause(now=now + timedelta(seconds=4 + 16)) is None

    fresh = store.persist_llm_pause(
        _service_issue(),
        now=now + timedelta(seconds=4 + 16 + store.SERVICE_UNAVAILABLE_RECURRENCE_WINDOW_SEC + 60),
    )
    assert fresh["occurrences"] == 1
    assert store.pause_wait_seconds(
        fresh,
        now=now + timedelta(seconds=4 + 16 + store.SERVICE_UNAVAILABLE_RECURRENCE_WINDOW_SEC + 60),
    ) == pytest.approx(8.0)


def test_window_does_not_cross_categories(isolated_store):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    service = store.persist_llm_pause(_service_issue(), now=now)
    assert service["occurrences"] == 1
    later = now + timedelta(seconds=30)
    # Let the service pause auto-resume so the store is genuinely idle —
    # otherwise the billing persist lands in the suppressed-recurrence
    # branch against the still-recorded-active service pause.
    assert store.active_llm_pause(now=later) is None
    billing_state = {
        "category": "billing_cycle_limit",
        "summary": "billing limit",
        "retry_policy": "manual",
        "requires_manual_resume": True,
        "evidence_digest": "b" * 64,
    }
    billing = store.persist_llm_pause(billing_state, now=later)
    assert billing["occurrences"] == 1  # different category: no carry
