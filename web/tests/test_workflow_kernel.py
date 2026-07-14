import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from workflow_kernel import (
    InvalidCompletion,
    WorkflowBusy,
    WorkflowConflict,
    WorkflowStore,
    reduce_events,
)


def _store(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    store.ensure_instance("149#0", definition_version=1)
    return store


def test_store_uses_wal_full_sync_and_foreign_keys(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        # foreign_keys is connection-local; the store enables it on every own connection.
        with store._connect() as configured:
            assert configured.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_event_append_is_versioned_and_causation_idempotent(tmp_path):
    store = _store(tmp_path)
    first = store.append_event(
        "149#0",
        "RepairPrepared",
        {"artifact": "a" * 64},
        causation_id="prepare-149",
        expected_version=0,
    )
    duplicate = store.append_event(
        "149#0",
        "RepairPrepared",
        {"artifact": "a" * 64},
        causation_id="prepare-149",
        expected_version=0,
    )

    assert first == duplicate
    assert len(store.events("149#0")) == 1
    with pytest.raises(WorkflowConflict, match="stale stream version"):
        store.append_event(
            "149#0",
            "WorkerStarted",
            {},
            causation_id="worker-start",
            expected_version=0,
        )
    with pytest.raises(WorkflowConflict, match="causation id reused"):
        store.append_event(
            "149#0",
            "RepairPrepared",
            {"artifact": "b" * 64},
            causation_id="prepare-149",
        )


def test_effect_request_and_outbox_are_atomic_and_idempotent(tmp_path):
    store = _store(tmp_path)
    effect = store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={"task_digest": "abc"},
        causation_id="request-worker-149",
        expected_version=0,
    )
    duplicate = store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={"task_digest": "abc"},
        causation_id="request-worker-149",
    )

    assert effect["input_digest"] == duplicate["input_digest"]
    assert [item["effect_id"] for item in store.pending_outbox()] == ["worker-149"]
    events = store.events("149#0")
    assert [event.event_type for event in events] == ["EffectRequested"]
    assert events[0].payload["input_digest"] == effect["input_digest"]

    with pytest.raises(WorkflowConflict, match="different input"):
        store.request_effect(
            run_id="149#0",
            effect_id="worker-149",
            kind="worker_llm",
            input_payload={"task_digest": "changed"},
            causation_id="request-worker-changed",
        )


def test_fenced_completion_rejects_stale_worker(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={"task_digest": "abc"},
        causation_id="request-worker-149",
    )
    first = store.claim_effect(
        "worker-149",
        owner="worker-a",
        lease_seconds=1,
        now=10,
    )
    store.fail_effect(
        "worker-149",
        lease_epoch=first.lease_epoch,
        error="transport timeout",
        retryable=True,
        causation_id="worker-149-failed-1",
    )
    second = store.claim_effect(
        "worker-149",
        owner="worker-b",
        lease_seconds=1,
        now=20,
    )

    stale = store.complete_effect(
        "worker-149",
        lease_epoch=first.lease_epoch,
        completion_id="completion-a",
        result_payload={"artifact": "old"},
        causation_id="worker-old-complete",
    )
    accepted = store.complete_effect(
        "worker-149",
        lease_epoch=second.lease_epoch,
        completion_id="completion-b",
        result_payload={"artifact": "new"},
        causation_id="worker-new-complete",
    )
    duplicate = store.complete_effect(
        "worker-149",
        lease_epoch=second.lease_epoch,
        completion_id="completion-b",
        result_payload={"artifact": "new"},
        causation_id="ignored-duplicate-causation",
    )

    assert stale == {
        "accepted": False,
        "duplicate": False,
        "reason": "stale_lease_epoch",
        "effect": stale["effect"],
    }
    assert accepted["accepted"] is True
    assert duplicate["accepted"] is True
    assert duplicate["duplicate"] is True
    assert store.effect("worker-149")["result_payload"] == {"artifact": "new"}


def test_strict_completion_rejects_current_but_expired_lease(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="strict-worker-149",
        kind="strict-native-match",
        input_payload={"hands": 70},
        causation_id="request-strict-worker-149",
    )
    lease = store.claim_effect(
        "strict-worker-149",
        owner="worker-a",
        lease_seconds=1,
        now=10,
    )

    expired = store.complete_effect(
        "strict-worker-149",
        lease_epoch=lease.lease_epoch,
        completion_id="expired-completion",
        result_payload={"hands": 70},
        causation_id="expired-complete",
        require_live_lease=True,
        now=11,
    )

    assert expired["accepted"] is False
    assert expired["reason"] == "expired_lease"
    assert store.effect("strict-worker-149")["status"] == "running"


def test_three_failures_exhaust_one_logical_effect(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={"task_digest": "stable"},
        causation_id="request-worker-149",
        max_attempts=3,
    )

    statuses = []
    epochs = []
    for attempt in range(1, 4):
        lease = store.claim_effect(
            "worker-149",
            owner=f"worker-{attempt}",
            lease_seconds=1,
            now=float(attempt * 10),
        )
        epochs.append(lease.lease_epoch)
        effect = store.fail_effect(
            "worker-149",
            lease_epoch=lease.lease_epoch,
            error="timeout",
            retryable=True,
            causation_id=f"worker-149-failed-{attempt}",
        )
        statuses.append(effect["status"])

    assert epochs == [1, 2, 3]
    assert statuses == ["retry", "retry", "exhausted"]
    with pytest.raises(WorkflowConflict, match="terminal"):
        store.claim_effect(
            "worker-149",
            owner="worker-4",
            lease_seconds=1,
        )


def test_deferred_effect_releases_lease_without_consuming_attempt(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={"task_digest": "stable"},
        causation_id="request-worker-149",
        max_attempts=3,
    )
    first = store.claim_effect(
        "worker-149", owner="worker-a", lease_seconds=60, now=10
    )

    deferred = store.defer_effect(
        "worker-149",
        lease_epoch=first.lease_epoch,
        reason="billing-cycle usage limit",
        metadata={"availability": {"evidence_digest": "a" * 64}},
        causation_id="worker-149-deferred-1",
    )

    assert deferred["status"] == "deferred"
    assert deferred["attempt"] == 0
    assert deferred["lease_epoch"] == 1
    assert deferred["lease_owner"] is None
    assert deferred["lease_until"] is None
    assert store.pending_outbox(now=100) == []
    event = store.events("149#0")[-1]
    assert event.event_type == "EffectDeferred"
    assert event.payload["claimed_attempt"] == 1
    assert event.payload["restored_attempt"] == 0
    assert event.payload["metadata"]["availability"]["evidence_digest"] == (
        "a" * 64
    )
    with pytest.raises(WorkflowConflict, match="explicit resume"):
        store.claim_effect(
            "worker-149", owner="worker-b", lease_seconds=60, now=20
        )

    resumed = store.resume_effect(
        "worker-149", causation_id="worker-149-resumed-1"
    )
    assert resumed["status"] == "retry"
    assert [row["effect_id"] for row in store.pending_outbox()] == [
        "worker-149"
    ]
    second = store.claim_effect(
        "worker-149", owner="worker-b", lease_seconds=60, now=20
    )
    assert second.attempt == 1
    assert second.lease_epoch == 2

    stale = store.complete_effect(
        "worker-149",
        lease_epoch=first.lease_epoch,
        completion_id="stale-after-deferral",
        result_payload={"artifact": "stale"},
        causation_id="stale-after-deferral",
    )
    assert stale["accepted"] is False


def test_stale_failure_cannot_overwrite_new_lease(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={},
        causation_id="request-worker-149",
    )
    first = store.claim_effect("worker-149", owner="a", lease_seconds=1, now=1)
    second = store.claim_effect("worker-149", owner="b", lease_seconds=1, now=3)
    with pytest.raises(InvalidCompletion, match="stale effect failure"):
        store.fail_effect(
            "worker-149",
            lease_epoch=first.lease_epoch,
            error="late timeout",
            retryable=True,
            causation_id="late-a",
        )
    assert store.effect("worker-149")["lease_epoch"] == second.lease_epoch


def test_command_lock_is_nonblocking_and_generation_scoped(tmp_path):
    store = _store(tmp_path)
    with store.command_lock("149#0"):
        with pytest.raises(WorkflowBusy):
            with store.command_lock("149#0"):
                pass
        with store.command_lock("150#0"):
            pass


def test_concurrent_events_have_contiguous_unique_sequence(tmp_path):
    store = _store(tmp_path)

    def append(index):
        return store.append_event(
            "149#0",
            "Observation",
            {"index": index},
            causation_id=f"observation-{index}",
        ).seq

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(append, range(40)))

    assert sorted(sequences) == list(range(1, 41))
    assert [event.seq for event in store.events("149#0")] == list(range(1, 41))


def test_reducer_replay_is_byte_deterministic(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        "149#0",
        "RepairPrepared",
        {"artifact": "prepared"},
        causation_id="prepared",
    )
    store.append_event(
        "149#0",
        "WorkerAttemptFailed",
        {"attempt": 1, "failure_class": "infrastructure"},
        causation_id="failed-1",
    )

    def reducer(state, event):
        return {
            "events": [*state["events"], event.event_type],
            "attempt": max(state["attempt"], int(event.payload.get("attempt", 0))),
        }

    left = reduce_events(
        {"events": [], "attempt": 0},
        store.events("149#0"),
        reducer,
    )
    right = reduce_events(
        {"events": [], "attempt": 0},
        store.events("149#0"),
        reducer,
    )
    assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def test_definition_version_is_bound_for_lifetime(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(WorkflowConflict, match="definition version mismatch"):
        store.ensure_instance("149#0", definition_version=2)


def test_completion_and_followup_event_rollback_as_one_transaction(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={},
        causation_id="requested",
    )
    lease = store.claim_effect(
        "worker-149", owner="worker", lease_seconds=10, now=1
    )
    original = store._append_event_locked

    def fail_domain_followup(connection, **kwargs):
        if kwargs.get("event_type") == "WorkerOutputReady":
            raise RuntimeError("crash before domain receipt")
        return original(connection, **kwargs)

    monkeypatch.setattr(store, "_append_event_locked", fail_domain_followup)
    with pytest.raises(RuntimeError, match="crash before domain receipt"):
        store.complete_effect(
            "worker-149",
            lease_epoch=lease.lease_epoch,
            completion_id="completion",
            result_payload={"artifact": "a"},
            causation_id="effect-completed",
            followup_events=[{
                "event_type": "WorkerOutputReady",
                "payload": {"artifact": "a"},
                "causation_id": "output-ready",
            }],
        )

    assert store.effect("worker-149")["status"] == "running"
    assert [event.event_type for event in store.events("149#0")] == [
        "EffectRequested"
    ]
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


def test_causation_identity_is_scoped_to_workflow(tmp_path):
    store = _store(tmp_path)
    store.ensure_instance("150#0", definition_version=1)
    first = store.append_event(
        "149#0", "Observation", {"run": 149}, causation_id="same"
    )
    second = store.append_event(
        "150#0", "Observation", {"run": 150}, causation_id="same"
    )
    assert first.seq == second.seq == 1


def test_terminal_transition_fences_active_lease_and_late_results(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={},
        causation_id="requested",
    )
    lease = store.claim_effect(
        "worker-149", owner="worker", lease_seconds=10, now=1
    )
    version = store.instance("149#0")["stream_version"]
    store.terminal_transition(
        "149#0",
        event_type="WorkerAbandoned",
        payload={"reason": "operator"},
        causation_id="abandoned",
        expected_version=version,
        status="abandoned",
    )

    assert store.effect("worker-149")["status"] == "abandoned"
    assert store.pending_outbox() == []
    late = store.complete_effect(
        "worker-149",
        lease_epoch=lease.lease_epoch,
        completion_id="late-output",
        result_payload={"artifact": "late"},
        causation_id="late-complete",
    )
    assert late["accepted"] is False
    with pytest.raises(InvalidCompletion):
        store.fail_effect(
            "worker-149",
            lease_epoch=lease.lease_epoch,
            error="late",
            retryable=True,
            causation_id="late-failure",
        )


def test_repeated_process_death_replays_to_exhausted_event(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={},
        causation_id="requested",
        max_attempts=3,
    )
    leases = [
        store.claim_effect(
            "worker-149",
            owner=f"worker-{index}",
            lease_seconds=1,
            now=float(index * 2 + 1),
        )
        for index in range(3)
    ]
    assert [lease.attempt for lease in leases] == [1, 2, 3]
    with pytest.raises(WorkflowConflict, match="attempt budget exhausted"):
        store.claim_effect(
            "worker-149",
            owner="worker-4",
            lease_seconds=1,
            now=10,
        )
    assert store.effect("worker-149")["status"] == "exhausted"
    terminal = store.events("149#0")[-1]
    assert terminal.event_type == "EffectFailed"
    assert terminal.payload["retryable"] is False


def test_expired_running_effect_returns_to_pending_outbox(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={},
        causation_id="requested",
    )
    store.claim_effect(
        "worker-149", owner="dead-worker", lease_seconds=2, now=10
    )

    assert store.pending_outbox(now=11) == []
    assert [row["effect_id"] for row in store.pending_outbox(now=12)] == [
        "worker-149"
    ]


def test_event_replay_rejects_payload_digest_tampering(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        "149#0", "Observation", {"value": "trusted"}, causation_id="one"
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE workflow_events SET payload = ? WHERE run_id = ? AND seq = 1",
            (json.dumps({"value": "tampered"}), "149#0"),
        )

    with pytest.raises(WorkflowConflict, match="payload digest mismatch"):
        store.events("149#0")


def test_unknown_database_schema_version_fails_closed(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    WorkflowStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(WorkflowConflict, match="unsupported workflow database schema"):
        WorkflowStore(path)
