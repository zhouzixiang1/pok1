from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

import evolution_infra
from llm_availability import (
    BILLING_CYCLE_LIMIT,
    QUOTA_429,
    QUOTA_RESET_AUTHORITY_FALLBACK,
    QUOTA_RESET_AUTHORITY_PROVIDER,
    SERVICE_UNAVAILABLE,
    TRANSPORT_UNAVAILABLE,
    classify_llm_availability,
    service_unavailable_cooldown_seconds,
)
import llm_availability_store as store
import llm_query


# --- P1 (2026-10-05): frequency-class cooldown exponential backoff ----------


def test_service_unavailable_cooldown_curve_sequence():
    """Shared curve: cooldown(n) = min(120, 8 * 2**(n-1)), n counted from 1."""
    assert [service_unavailable_cooldown_seconds(n) for n in range(1, 7)] == [
        8, 16, 32, 64, 120, 120,
    ]
    # Out-of-range counts clamp instead of crashing or exploding.
    assert service_unavailable_cooldown_seconds(0) == 8
    assert service_unavailable_cooldown_seconds(-3) == 8
    assert service_unavailable_cooldown_seconds(99) == 120


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.delenv(store.RESUME_ENV, raising=False)
    return tmp_path


def _billing_issue():
    issue = classify_llm_availability(
        ["HTTP 403: You've reached your usage limit for this billing cycle"],
        statuses=[403],
    )
    assert issue is not None
    assert issue.category == BILLING_CYCLE_LIMIT
    return issue


def _service_issue():
    issue = classify_llm_availability(
        ["HTTP 529 service unavailable"],
        statuses=[529],
    )
    assert issue is not None
    assert issue.category == SERVICE_UNAVAILABLE
    return issue


def _quota_issue(reset_at: str | None = None):
    evidence = "API Error: Request rejected (429) · [1308][已达到 5 小时的使用上限。"
    if reset_at:
        evidence += f"]; quota reset at {reset_at}"
    else:
        evidence += "]"
    issue = classify_llm_availability([evidence], statuses=[429])
    assert issue is not None
    assert issue.category == QUOTA_429
    return issue


def test_manual_pause_survives_reload_and_requires_exact_digest(
    isolated_store, monkeypatch
):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    issue = _billing_issue()
    persisted = store.persist_llm_pause(issue, now=now)

    assert persisted["active"] is True
    assert persisted["requires_manual_resume"] is True
    assert persisted["auto_resume_at"] is None
    assert store.load_llm_pause()["evidence_digest"] == issue.evidence_digest

    monkeypatch.setenv(store.RESUME_ENV, "not-the-evidence-digest")
    rejected = store.consume_operator_resume_ack_from_env(
        now=now + timedelta(days=10)
    )
    assert rejected["active"] is True
    assert rejected["last_rejected_resume_digest"] == "not-the-evidence-digest"
    assert store.RESUME_ENV not in __import__("os").environ

    monkeypatch.setenv(store.RESUME_ENV, issue.evidence_digest)
    resumed = store.consume_operator_resume_ack_from_env(
        now=now + timedelta(days=10)
    )
    assert resumed["active"] is False
    assert resumed["resume_source"] == "operator_evidence_digest"
    assert store.RESUME_ENV not in __import__("os").environ
    assert store.active_llm_pause(now=now + timedelta(days=10)) is None


def test_runtime_env_injection_cannot_resume_manual_pause(
    isolated_store, monkeypatch
):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    issue = _billing_issue()
    store.persist_llm_pause(issue, now=now)

    # Simulate an SDK child setting the publicly known digest after the parent
    # startup boundary has passed. Runtime reconciliation must ignore it.
    monkeypatch.setenv(store.RESUME_ENV, issue.evidence_digest)
    runtime = store.reconcile_llm_pause(now=now + timedelta(days=10))

    assert runtime["active"] is True
    assert "resumed_at" not in runtime
    assert __import__("os").environ[store.RESUME_ENV] == issue.evidence_digest


def test_transient_pause_auto_resumes_only_after_system_cooldown(isolated_store):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    state = store.persist_llm_pause(_service_issue(), now=now)

    assert state["active"] is True
    assert state["requires_manual_resume"] is False
    # P1 (2026-10-05): the first SERVICE_UNAVAILABLE occurrence now cools down
    # for the curve base 8s (was a flat 120s); only consecutive recurrences
    # escalate towards the 120s ceiling.
    assert store.pause_wait_seconds(state, now=now) == pytest.approx(8.0)
    assert store.active_llm_pause(now=now + timedelta(seconds=7))["active"] is True

    assert store.active_llm_pause(now=now + timedelta(seconds=8)) is None
    audit = store.load_llm_pause()
    assert audit["active"] is False
    assert audit["resume_source"] == "bounded_cooldown_elapsed"


def test_status_only_429_persists_as_short_service_cooldown(isolated_store):
    """A bare HTTP 429 without GLM 1308 must not become a 5-hour quota pause."""
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    issue = classify_llm_availability(
        ["Request rejected (429)"],
        statuses=[429],
    )
    assert issue is not None
    assert issue.category == SERVICE_UNAVAILABLE
    assert issue.quota_reset_authority is None
    state = store.persist_llm_pause(issue, now=now)
    assert state["category"] == SERVICE_UNAVAILABLE
    assert state["requires_manual_resume"] is False
    # P1 (2026-10-05): first bare-429 occurrence uses the curve base 8s (was a
    # flat 120s); it must still never become a 5-hour quota wait.
    assert store.pause_wait_seconds(state, now=now) == pytest.approx(8.0)
    assert store.active_llm_pause(now=now + timedelta(seconds=7))["active"] is True
    assert store.active_llm_pause(now=now + timedelta(seconds=8)) is None


def test_service_unavailable_recurrence_backs_off_exponentially(isolated_store):
    """A recurring frequency-class pause escalates along the shared curve.

    The durable 1302 friction repeats every few minutes in production; a flat
    cooldown re-arms the same short window forever, so each same-category
    recurrence observed while the pause is still active must extend
    ``auto_resume_at`` by cooldown(occurrences) (P1, 2026-10-05).
    """
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    first = store.persist_llm_pause(_service_issue(), now=now)
    assert first["occurrences"] == 1
    assert store.pause_wait_seconds(first, now=now) == pytest.approx(8.0)

    second = store.persist_llm_pause(
        _service_issue(), now=now + timedelta(seconds=4)
    )
    assert second["occurrences"] == 2
    assert store.pause_wait_seconds(second, now=now + timedelta(seconds=4)) == (
        pytest.approx(16.0)
    )

    third = store.persist_llm_pause(
        _service_issue(), now=now + timedelta(seconds=5)
    )
    assert third["occurrences"] == 3
    assert store.pause_wait_seconds(third, now=now + timedelta(seconds=5)) == (
        pytest.approx(32.0)
    )


def test_service_unavailable_cooldown_resets_after_pause_clears(isolated_store):
    """After the recurrence window passes, a later occurrence starts over.

    P3 (2026-10-05, F10) narrowed this contract: a same-category recurrence
    *inside* the 30-minute sliding window now carries the count (see
    test_backoff_streak_decay.py::test_occurrences_survive_pause_clear_inside_window
    — clearing it 1s after resume was exactly the sawtooth that never
    reached the 120s cap).  Only a recurrence after the window elapses
    starts the curve over.
    """
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    first = store.persist_llm_pause(_service_issue(), now=now)
    second = store.persist_llm_pause(
        _service_issue(), now=now + timedelta(seconds=4)
    )
    assert second["occurrences"] == 2
    # auto_resume_at = now+4+16s; let it elapse so the record clears.
    assert store.active_llm_pause(now=now + timedelta(seconds=4 + 16)) is None

    fresh = store.persist_llm_pause(
        _service_issue(),
        now=now
        + timedelta(seconds=4 + 16)
        + timedelta(seconds=store.SERVICE_UNAVAILABLE_RECURRENCE_WINDOW_SEC + 1),
    )
    assert fresh["occurrences"] == 1
    assert store.pause_wait_seconds(
        fresh,
        now=now
        + timedelta(seconds=4 + 16)
        + timedelta(seconds=store.SERVICE_UNAVAILABLE_RECURRENCE_WINDOW_SEC + 1),
    ) == pytest.approx(8.0)


def test_transport_unavailable_keeps_flat_60s_cooldown(isolated_store):
    """TRANSPORT_UNAVAILABLE is NOT part of the backoff curve: its fixed 60s
    cooldown (and its recurrence behaviour) must be untouched by P1."""
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    transport_state = {
        "category": TRANSPORT_UNAVAILABLE,
        "summary": "connection refused",
        "retry_policy": "cooldown",
        "requires_manual_resume": False,
        "evidence_digest": "t" * 64,
    }
    state = store.persist_llm_pause(transport_state, now=now)
    assert store.pause_wait_seconds(state, now=now) == pytest.approx(60.0)

    recurring = store.persist_llm_pause(
        transport_state, now=now + timedelta(seconds=1)
    )
    assert recurring["occurrences"] == 2
    # No curve extension for transport: still due at the original now+60s.
    assert store.pause_wait_seconds(
        recurring, now=now + timedelta(seconds=1)
    ) == pytest.approx(59.0)


def test_quota_provider_reset_path_unaffected_by_backoff_curve(isolated_store):
    """A confirmed 1308 with a provider timestamp keeps the provider reset as
    the sole resume authority; same-category recurrences must not re-derive it
    from the frequency-class curve (P1 regression, 2026-10-05)."""
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    issue = _quota_issue("2026-07-13T15:00:00+00:00")
    state = store.persist_llm_pause(issue, now=now)
    assert state["auto_resume_at"] == state["provider_reset_at"]
    assert state["auto_resume_at"] == "2026-07-13T15:00:00+00:00"

    recurring = store.persist_llm_pause(
        _quota_issue("2026-07-13T15:00:00+00:00"), now=now + timedelta(minutes=1)
    )
    assert recurring["auto_resume_at"] == "2026-07-13T15:00:00+00:00"


def test_1308_without_timestamp_auto_resumes_after_quota_fallback_window(
    isolated_store,
):
    """Confirmed GLM 1308 without a parseable reset still uses the 5h fallback."""
    issue = _quota_issue()
    state = store.persist_llm_pause(issue)

    assert issue.requires_manual_resume is False
    assert issue.retry_policy == "resume_after_quota_reset"
    assert issue.quota_reset_authority == QUOTA_RESET_AUTHORITY_FALLBACK
    assert issue.provider_reset_at is not None
    assert state["requires_manual_resume"] is False
    assert state["quota_reset_authority"] == QUOTA_RESET_AUTHORITY_FALLBACK
    assert state["provider_reset_at"] is not None
    assert state["auto_resume_at"] == state["provider_reset_at"]


def test_429_auto_resumes_at_explicit_provider_reset_only(isolated_store):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    issue = _quota_issue("2026-07-13T10:10:00+00:00")
    state = store.persist_llm_pause(issue, now=now)

    assert issue.requires_manual_resume is False
    assert state["provider_reset_at"] == "2026-07-13T10:10:00+00:00"
    assert state["quota_reset_authority"] == QUOTA_RESET_AUTHORITY_PROVIDER
    assert state["auto_resume_at"] == state["provider_reset_at"]
    assert store.pause_wait_seconds(state, now=now) == pytest.approx(600.0)
    assert store.active_llm_pause(
        now=now + timedelta(seconds=599)
    )["active"] is True
    assert store.active_llm_pause(now=now + timedelta(seconds=600)) is None
    assert store.load_llm_pause()["resume_source"] == "provider_quota_reset_elapsed"


def test_legacy_guessed_quota_pause_without_reset_authority_is_dropped(
    isolated_store,
):
    """Pre-fix records invented a 5h wait from any 429, including GLM 1302.

    Those pauses have a provider_reset_at but no quota_reset_authority. They
    must not keep the pipeline dark after deploy; reconcile drops them.
    """
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    guessed = {
        "schema_version": store.SCHEMA_VERSION,
        "active": True,
        "source": "llm_availability",
        "category": QUOTA_429,
        "summary": "provider quota window is exhausted",
        "http_status": 429,
        "retry_policy": "resume_after_quota_reset",
        "requires_manual_resume": False,
        "persistent_pause": True,
        "evidence_digest": "a" * 64,
        "role": "MASTER (Try 1)",
        "first_observed_at": now.isoformat(),
        "last_observed_at": now.isoformat(),
        "occurrences": 3,
        "provider_reset_at": (now + timedelta(hours=5, seconds=60)).isoformat(),
        "auto_resume_at": (now + timedelta(hours=5, seconds=60)).isoformat(),
    }
    store.pause_path().parent.mkdir(parents=True, exist_ok=True)
    store.pause_path().write_text(json.dumps(guessed), encoding="utf-8")

    assert store.active_llm_pause(now=now) is None
    audit = store.load_llm_pause()
    assert audit["active"] is False
    assert audit["resume_source"] == "untrusted_quota_pause_without_reset_authority"


def test_legacy_429_fixed_cooldown_record_is_dropped_as_untrusted(
    isolated_store,
):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    legacy = {
        "schema_version": store.SCHEMA_VERSION,
        "active": True,
        "source": "llm_availability",
        "category": QUOTA_429,
        "summary": "provider quota window is exhausted",
        "http_status": 429,
        "retry_policy": "resume_after_quota_reset",
        "requires_manual_resume": False,
        "persistent_pause": True,
        "evidence_digest": "a" * 64,
        "role": "worker",
        "first_observed_at": now.isoformat(),
        "last_observed_at": now.isoformat(),
        "occurrences": 1,
        "auto_resume_at": (now + timedelta(seconds=300)).isoformat(),
    }
    store.pause_path().parent.mkdir(parents=True, exist_ok=True)
    store.pause_path().write_text(json.dumps(legacy), encoding="utf-8")

    assert store.active_llm_pause(now=now + timedelta(hours=1)) is None
    assert store.load_llm_pause()["resume_source"] == (
        "untrusted_quota_pause_without_reset_authority"
    )


def test_weaker_transient_evidence_cannot_replace_manual_pause(isolated_store):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    billing = store.persist_llm_pause(_billing_issue(), now=now)
    service = store.persist_llm_pause(
        _service_issue(), now=now + timedelta(seconds=1)
    )

    assert service["category"] == billing["category"]
    assert service["evidence_digest"] == billing["evidence_digest"]
    assert service["occurrences"] == 2
    assert service["last_suppressed_category"] == SERVICE_UNAVAILABLE


def test_corrupt_pause_record_fails_closed(isolated_store):
    store.pause_path().write_text("{broken", encoding="utf-8")

    with pytest.raises(store.LLMAvailabilityPauseError):
        store.load_llm_pause()
    with pytest.raises(store.LLMAvailabilityPauseError):
        store.active_llm_pause()


def test_pause_projection_is_atomic_json(isolated_store):
    store.persist_llm_pause(_billing_issue())

    raw = json.loads(store.pause_path().read_text(encoding="utf-8"))
    assert raw["schema_version"] == store.SCHEMA_VERSION
    assert not list(isolated_store.glob(".*.tmp"))


def test_every_llm_role_fails_before_sdk_while_pause_is_active(
    isolated_store, monkeypatch
):
    from llm_availability import LLMAvailabilityBlocked

    issue = _billing_issue()
    store.persist_llm_pause(issue)
    sdk_calls = []
    monkeypatch.setattr(
        llm_query,
        "claude_query",
        lambda **_kwargs: sdk_calls.append(True),
    )

    class UI:
        def log_io(self, *_args, **_kwargs):
            pass

        def log_history(self, *_args, **_kwargs):
            pass

        def update_cost(self, *_args, **_kwargs):
            pass

    import asyncio
    import combined_analyst

    rendered_prompt = llm_query.render_llm_prompt(
        "COMBINED ANALYST",
        producer=combined_analyst._render_combined_provider_prompt,
        renderer_inputs={
            "source_v": 149,
            "frozen_bundle": {
                "marker": "prompt",
                "rendered_view": {
                    "bot_name": "prompt",
                    "opp_eval": "1",
                    "opp_total": "1",
                    "opp_coverage": "100%",
                    "rd_warning": "",
                    "top_bots": "none",
                    "generation_trend": "none",
                    "lineage": "none",
                    "daemon_history": "none",
                    "bot_stats": "none",
                    "h2h_results": "none",
                },
            },
        },
    )

    with pytest.raises(LLMAvailabilityBlocked) as caught:
        asyncio.run(
            llm_query.run_claude_query(
                rendered_prompt,
                [],
                UI(),
                "COMBINED ANALYST",
                str(isolated_store / "role.log"),
            )
        )

    assert caught.value.issue.evidence_digest == issue.evidence_digest
    assert caught.value.role == "COMBINED ANALYST"
    assert sdk_calls == []
