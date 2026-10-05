"""Resume-receipt history for the durable LLM availability pause store.

Worker availability deferral freezes the *then-current* pause projection
(category/evidence_digest/retry_policy/http_status/requires_manual_resume)
into the Worker journal.  On resume,
``_worker_availability_resume_receipt_errors`` compares that frozen
projection against the pause store's single audit record.  Before this
contract the store kept only the newest record: persisting a new pause
overwrote the resumed receipt of the pause the Worker actually deferred on,
so under a 1302 burst every resume failed closed forever
(``WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID`` wedge, 2026-10-05 v511).

Contract (fail-closed semantics unchanged):

* ``persist_llm_pause`` archives the overwritten record's resume receipt
  (only when the record is inactive AND carries ``resumed_at`` +
  ``resume_source``) into a bounded ``resume_receipt_history`` (cap 8, FIFO)
  on the new record; the store schema moves 1 -> 2 and v1 records without
  the field still load.
* The validator accepts a deferred effect when either the current audit
  record matches (unchanged) OR any history projection matches under the
  same per-record rules.  A manual pause in history still requires
  ``resume_source == "operator_evidence_digest"`` plus an exact digest
  match; no match anywhere still rejects.
"""

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
import tool_planning_worker  # noqa: F401  (import order: parent first, see below)
import tool_planning_worker_durable
from tool_planning_worker_durable import _worker_availability_resume_receipt_errors


BASE = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.delenv(store.RESUME_ENV, raising=False)
    return tmp_path


def _service_issue(text: str = "HTTP 529 service unavailable"):
    issue = classify_llm_availability([text], statuses=[529])
    assert issue is not None
    assert issue.category == SERVICE_UNAVAILABLE
    return issue


def _billing_issue():
    issue = classify_llm_availability(
        ["HTTP 403: You've reached your usage limit for this billing cycle"],
        statuses=[403],
    )
    assert issue is not None
    assert issue.category == BILLING_CYCLE_LIMIT
    return issue


def _cool_down(now):
    """Advance past the live record's cooldown window and reconcile.

    P3 (2026-10-05): same-category recurrences inside the 30-minute sliding
    window now carry ``occurrences`` forward, so the cooldown climbs
    (8->16->...->120s) across this file's 30s-cadence pauses.  Advancing a
    fixed 9s only cleared the first occurrence; read the record's own
    ``auto_resume_at`` instead so the receipt-archive semantics under test
    stay intact.
    """
    record = store.load_llm_pause()
    due = store._parse_time((record or {}).get("auto_resume_at")) if record else None
    if due is None:
        return store.active_llm_pause(now=now + timedelta(seconds=9))
    return store.active_llm_pause(now=due + timedelta(seconds=1))


def test_deferred_worker_resumes_via_overwritten_receipt_history(isolated_store):
    """The 1302-burst wedge: receipt overwritten before the Worker resumes."""
    pause_a = store.persist_llm_pause(_service_issue(), now=BASE)
    deferred = dict(pause_a)  # what availability_deferred froze

    # Pause A cools down -> inactive audit record with a resume receipt.
    assert _cool_down(BASE) is None

    # A NEW pause with different evidence overwrites the record.
    pause_b = store.persist_llm_pause(
        _service_issue("HTTP 529 later blip"), now=BASE + timedelta(seconds=20)
    )
    audit = store.load_llm_pause()
    assert audit["evidence_digest"] == pause_b["evidence_digest"]
    assert audit["schema_version"] == 2

    history = audit.get("resume_receipt_history")
    assert isinstance(history, list) and len(history) == 1
    entry = history[0]
    assert entry["evidence_digest"] == pause_a["evidence_digest"]
    assert entry["category"] == SERVICE_UNAVAILABLE
    assert entry["retry_policy"] == pause_a["retry_policy"]
    assert entry["http_status"] == 529
    assert entry["requires_manual_resume"] is False
    assert entry["resume_source"] == "bounded_cooldown_elapsed"
    assert entry["resumed_at"]
    assert entry["resume_evidence_digest"] is None

    # Pause B also cools down; the deferred Worker must resume through the
    # archived receipt of pause A.
    assert _cool_down(BASE) is None
    audit = store.load_llm_pause()
    assert _worker_availability_resume_receipt_errors(deferred, audit) == []


def test_history_without_matching_receipt_still_fails_closed(isolated_store):
    pause_a = store.persist_llm_pause(_service_issue(), now=BASE)
    assert _cool_down(BASE) is None
    store.persist_llm_pause(
        _service_issue("HTTP 529 later blip"), now=BASE + timedelta(seconds=20)
    )
    assert _cool_down(BASE) is None
    audit = store.load_llm_pause()

    stranger = dict(pause_a)
    stranger["evidence_digest"] = "f" * 64
    errors = _worker_availability_resume_receipt_errors(stranger, audit)
    assert errors
    assert "global_pause_resume_receipt_evidence_digest_mismatch" in errors
    # Review follow-up: with an archive present but unmatched, the current
    # record's errors must appear exactly once (no double concatenation).
    assert errors.count(
        "global_pause_resume_receipt_evidence_digest_mismatch"
    ) == 1
    assert len(errors) == len(set(errors))


def test_history_manual_pause_requires_operator_digest(isolated_store):
    """A manual pause archived in history never resumes without the digest."""
    # A manual quota pause with no trusted reset authority (legacy shape).
    untrusted_quota = store.persist_llm_pause(
        {
            "category": QUOTA_429,
            "summary": "guessed five-hour wait from a bare 429",
            "http_status": 429,
            "retry_policy": "manual_resume",
            "requires_manual_resume": True,
            "evidence_digest": "a" * 64,
            "quota_reset_authority": "",
        },
        now=BASE,
    )
    assert untrusted_quota["requires_manual_resume"] is True
    deferred = dict(untrusted_quota)

    # Persisting the manual billing pause force-clears the untrusted quota
    # record (resume_source="untrusted_quota_pause_without_reset_authority")
    # and archives its receipt into the history.
    billing = store.persist_llm_pause(_billing_issue(), now=BASE + timedelta(minutes=1))
    audit = store.load_llm_pause()
    assert audit["evidence_digest"] == billing["evidence_digest"]
    history = audit.get("resume_receipt_history") or []
    assert any(
        e.get("evidence_digest") == untrusted_quota["evidence_digest"]
        for e in history
    )

    # Operator resumes the billing pause with the exact digest.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(store.RESUME_ENV, billing["evidence_digest"])
        resumed = store.consume_operator_resume_ack_from_env(
            now=BASE + timedelta(minutes=2)
        )
    assert resumed["active"] is False

    audit = store.load_llm_pause()
    errors = _worker_availability_resume_receipt_errors(deferred, audit)
    assert errors
    assert "manual_pause_operator_receipt_missing" in errors
    assert "manual_pause_resume_evidence_digest_mismatch" in errors


def test_history_manual_pause_with_operator_digest_passes(isolated_store):
    billing = store.persist_llm_pause(_billing_issue(), now=BASE)
    deferred = dict(billing)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(store.RESUME_ENV, billing["evidence_digest"])
        resumed = store.consume_operator_resume_ack_from_env(
            now=BASE + timedelta(minutes=5)
        )
    assert resumed["active"] is False
    assert resumed["resume_source"] == "operator_evidence_digest"

    # A later transient pause overwrites the record; the operator-cleared
    # manual receipt is archived and still authorizes the deferred Worker.
    store.persist_llm_pause(_service_issue(), now=BASE + timedelta(minutes=6))
    audit = store.load_llm_pause()
    history = audit.get("resume_receipt_history") or []
    assert any(
        e.get("resume_evidence_digest") == billing["evidence_digest"]
        for e in history
    )
    assert _worker_availability_resume_receipt_errors(deferred, audit) == []


def test_resume_receipt_history_is_bounded_fifo(isolated_store):
    seen = []
    moment = BASE
    for index in range(10):
        pause = store.persist_llm_pause(
            _service_issue(f"HTTP 529 blip {index}"), now=moment
        )
        seen.append(pause["evidence_digest"])
        assert _cool_down(moment) is None
        moment = moment + timedelta(seconds=30)

    audit = store.load_llm_pause()
    history = audit.get("resume_receipt_history")
    assert isinstance(history, list)
    assert len(history) == 8
    # Ten pauses archive nine receipts (the tenth is still the live record);
    # FIFO keeps the newest eight, evicting the oldest receipt and the pause
    # that was never overwritten.
    assert [entry["evidence_digest"] for entry in history] == seen[1:9]


def test_v1_record_without_history_loads_and_upgrades(isolated_store):
    """A pre-schema-2 record (no history field) must keep loading unchanged."""
    legacy = {
        "schema_version": 1,
        "active": False,
        "source": "llm_availability",
        "category": SERVICE_UNAVAILABLE,
        "summary": "HTTP 529 service unavailable",
        "http_status": 529,
        "retry_policy": "bounded_cooldown",
        "requires_manual_resume": False,
        "persistent_pause": True,
        "evidence_digest": "b" * 64,
        "resumed_at": "2026-10-05T09:00:00+00:00",
        "resume_source": "bounded_cooldown_elapsed",
        "resume_evidence_digest": None,
        "auto_resume_at": "2026-10-05T08:59:52+00:00",
    }
    store.pause_path().write_text(json.dumps(legacy), encoding="utf-8")

    loaded = store.load_llm_pause()
    assert loaded is not None
    assert loaded["schema_version"] == 1

    # The v1 record still validates directly (no history required).
    deferred = dict(legacy)
    assert _worker_availability_resume_receipt_errors(deferred, loaded) == []

    # Persisting over it upgrades the store to schema 2 and archives the
    # legacy receipt into the new bounded history.
    state = store.persist_llm_pause(
        _service_issue("HTTP 529 after upgrade"), now=BASE
    )
    assert state["schema_version"] == 2
    history = state.get("resume_receipt_history")
    assert isinstance(history, list) and len(history) == 1
    assert history[0]["evidence_digest"] == legacy["evidence_digest"]
