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
    classify_llm_availability,
)
import llm_availability_store as store
import llm_query


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
    assert store.pause_wait_seconds(state, now=now) == pytest.approx(120.0)
    assert store.active_llm_pause(now=now + timedelta(seconds=119))["active"] is True

    assert store.active_llm_pause(now=now + timedelta(seconds=120)) is None
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
    assert store.pause_wait_seconds(state, now=now) == pytest.approx(120.0)
    assert store.active_llm_pause(now=now + timedelta(seconds=119))["active"] is True
    assert store.active_llm_pause(now=now + timedelta(seconds=120)) is None


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
