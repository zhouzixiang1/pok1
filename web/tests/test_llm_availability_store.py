from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

import evolution_infra
from llm_availability import (
    BILLING_CYCLE_LIMIT,
    QUOTA_429,
    SERVICE_UNAVAILABLE,
    classify_llm_availability,
)
import llm_availability_store as store
from core import llm_query


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
    evidence = "API error 429: quota exceeded"
    if reset_at:
        evidence += f"; quota reset at {reset_at}"
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
    assert store.pause_wait_seconds(state, now=now) == pytest.approx(120.0)
    assert store.active_llm_pause(now=now + timedelta(seconds=119))["active"] is True

    assert store.active_llm_pause(now=now + timedelta(seconds=120)) is None
    audit = store.load_llm_pause()
    assert audit["active"] is False
    assert audit["resume_source"] == "bounded_cooldown_elapsed"


def test_bare_429_is_manual_and_never_uses_a_guessed_five_minute_resume(
    isolated_store,
):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    issue = _quota_issue()
    state = store.persist_llm_pause(issue, now=now)

    assert issue.requires_manual_resume is True
    assert state["requires_manual_resume"] is True
    assert state["provider_reset_at"] is None
    assert state["auto_resume_at"] is None
    assert store.pause_wait_seconds(
        state, now=now + timedelta(days=1)
    ) is None
    assert store.active_llm_pause(now=now + timedelta(days=1))["active"] is True


def test_429_auto_resumes_at_explicit_provider_reset_only(isolated_store):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    issue = _quota_issue("2026-07-13T10:10:00+00:00")
    state = store.persist_llm_pause(issue, now=now)

    assert issue.requires_manual_resume is False
    assert state["provider_reset_at"] == "2026-07-13T10:10:00+00:00"
    assert state["auto_resume_at"] == state["provider_reset_at"]
    assert store.pause_wait_seconds(state, now=now) == pytest.approx(600.0)
    assert store.active_llm_pause(
        now=now + timedelta(seconds=599)
    )["active"] is True
    assert store.active_llm_pause(now=now + timedelta(seconds=600)) is None
    assert store.load_llm_pause()["resume_source"] == "provider_quota_reset_elapsed"


def test_legacy_429_fixed_cooldown_record_is_migrated_to_manual_fail_closed(
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

    active = store.active_llm_pause(now=now + timedelta(hours=1))
    assert active["active"] is True
    assert active["requires_manual_resume"] is True
    assert active["auto_resume_at"] is None


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

    with pytest.raises(LLMAvailabilityBlocked) as caught:
        asyncio.run(
            llm_query.run_claude_query(
                "prompt",
                [],
                UI(),
                "background-analyst",
                str(isolated_store / "role.log"),
            )
        )

    assert caught.value.issue.evidence_digest == issue.evidence_digest
    assert caught.value.role == "background-analyst"
    assert sdk_calls == []
