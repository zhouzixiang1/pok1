import json
import os
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


def test_concurrent_first_open_serializes_schema_and_wal_initialization(tmp_path):
    path = tmp_path / "concurrent-first-open.sqlite3"

    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = list(executor.map(lambda _index: WorkflowStore(path), range(32)))

    assert {store.path for store in stores} == {path}
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_short_lived_store_connections_do_not_leak_file_descriptors(tmp_path):
    store = _store(tmp_path)
    fd_dir = "/proc/self/fd"
    if not os.path.isdir(fd_dir):
        pytest.skip("Linux fd accounting is unavailable")
    before = len(os.listdir(fd_dir))

    for _ in range(300):
        assert store.instance("149#0")["run_id"] == "149#0"
        assert store.events("149#0") == []

    after = len(os.listdir(fd_dir))
    assert after - before < 5


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


def test_lease_heartbeat_is_durable_idempotent_and_does_not_consume_attempt(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="native-149",
        kind="strict-native-match",
        input_payload={"hands": 70},
        causation_id="native-requested",
        max_attempts=3,
    )
    claimed = store.claim_effect(
        "native-149", owner="consumer-a", lease_seconds=10, now=100
    )

    renewed = store.renew_effect_lease(
        "native-149",
        owner="consumer-a",
        lease_epoch=claimed.lease_epoch,
        lease_seconds=30,
        causation_id="native-heartbeat-1",
        now=105,
    )
    duplicate = store.renew_effect_lease(
        "native-149",
        owner="consumer-a",
        lease_epoch=claimed.lease_epoch,
        lease_seconds=30,
        causation_id="native-heartbeat-1",
        now=105,
    )

    assert renewed == duplicate
    assert renewed.attempt == claimed.attempt == 1
    assert renewed.lease_epoch == claimed.lease_epoch == 1
    assert renewed.lease_until == 135
    restarted = WorkflowStore(store.path)
    durable = restarted.effect("native-149")
    assert durable["attempt"] == 1
    assert durable["lease_epoch"] == 1
    assert durable["lease_until"] == 135
    assert [
        event.event_type for event in restarted.events("149#0")
    ].count("EffectLeaseHeartbeat") == 1

    completed = restarted.complete_effect(
        "native-149",
        lease_epoch=renewed.lease_epoch,
        completion_id="native-completed",
        result_payload={"hands": 70},
        causation_id="native-completed",
        require_live_lease=True,
        now=120,
    )
    assert completed["accepted"] is True


def test_lease_heartbeat_rejects_foreign_stale_expired_and_terminal_effect(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="native-149",
        kind="strict-native-match",
        input_payload={"hands": 70},
        causation_id="native-requested",
    )
    lease = store.claim_effect(
        "native-149", owner="consumer-a", lease_seconds=10, now=100
    )

    for owner, epoch, now in (
        ("consumer-b", lease.lease_epoch, 101),
        ("consumer-a", lease.lease_epoch + 1, 101),
        ("consumer-a", lease.lease_epoch, 110),
    ):
        with pytest.raises(InvalidCompletion, match="stale effect lease heartbeat"):
            store.renew_effect_lease(
                "native-149",
                owner=owner,
                lease_epoch=epoch,
                lease_seconds=10,
                causation_id=f"heartbeat-{owner}-{epoch}-{now}",
                now=now,
            )

    accepted = store.complete_effect(
        "native-149",
        lease_epoch=lease.lease_epoch,
        completion_id="native-terminal",
        result_payload={"hands": 70},
        causation_id="native-terminal",
        now=105,
    )
    assert accepted["accepted"] is True
    with pytest.raises(InvalidCompletion, match="stale effect lease heartbeat"):
        store.renew_effect_lease(
            "native-149",
            owner="consumer-a",
            lease_epoch=lease.lease_epoch,
            lease_seconds=10,
            causation_id="heartbeat-after-terminal",
            now=106,
        )


def test_per_effect_cancel_is_fenced_idempotent_and_rejects_late_completion(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="native-149",
        kind="strict-native-match",
        input_payload={"hands": 70},
        causation_id="native-requested",
    )
    lease = store.claim_effect(
        "native-149", owner="consumer-a", lease_seconds=20, now=100
    )

    for override in (
        {"expected_attempt": 2},
        {"expected_lease_epoch": lease.lease_epoch + 1},
        {"expected_owner": "consumer-b"},
    ):
        values = {
            "expected_status": "running",
            "expected_attempt": lease.attempt,
            "expected_lease_epoch": lease.lease_epoch,
            "expected_owner": "consumer-a",
            "reason": "operator_contract_change",
            "causation_id": f"cancel-stale-{sorted(override.items())}",
            "now": 105,
        }
        values.update(override)
        with pytest.raises(WorkflowConflict, match="stale effect cancellation"):
            store.cancel_effect("native-149", **values)

    cancelled = store.cancel_effect(
        "native-149",
        expected_status="running",
        expected_attempt=lease.attempt,
        expected_lease_epoch=lease.lease_epoch,
        expected_owner="consumer-a",
        reason="operator_contract_change",
        causation_id="native-cancelled",
        now=105,
    )
    duplicate = store.cancel_effect(
        "native-149",
        expected_status="running",
        expected_attempt=lease.attempt,
        expected_lease_epoch=lease.lease_epoch,
        expected_owner="consumer-a",
        reason="operator_contract_change",
        causation_id="native-cancelled",
        now=105,
    )
    assert cancelled["status"] == duplicate["status"] == "abandoned"
    assert store.pending_outbox(now=106) == []
    assert store.events("149#0")[-1].event_type == "EffectCancelled"

    late = store.complete_effect(
        "native-149",
        lease_epoch=lease.lease_epoch,
        completion_id="native-late",
        result_payload={"hands": 70},
        causation_id="native-late",
        require_live_lease=True,
        now=106,
    )
    assert late["accepted"] is False
    with pytest.raises(WorkflowConflict, match="stale effect cancellation"):
        store.cancel_effect(
            "native-149",
            expected_status="running",
            expected_attempt=lease.attempt,
            expected_lease_epoch=lease.lease_epoch,
            expected_owner="consumer-a",
            reason="second_cancel",
            causation_id="native-second-cancel",
            now=106,
        )


def test_per_effect_cancel_rejects_expired_running_lease(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="native-149",
        kind="strict-native-match",
        input_payload={"hands": 70},
        causation_id="native-requested",
    )
    lease = store.claim_effect(
        "native-149", owner="consumer-a", lease_seconds=10, now=100
    )
    with pytest.raises(WorkflowConflict, match="stale effect cancellation"):
        store.cancel_effect(
            "native-149",
            expected_status="running",
            expected_attempt=lease.attempt,
            expected_lease_epoch=lease.lease_epoch,
            expected_owner="consumer-a",
            reason="expired_owner_cannot_cancel",
            causation_id="expired-cancel",
            now=110,
        )


def test_concurrent_per_effect_cancel_has_one_cas_winner(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="native-149",
        kind="strict-native-match",
        input_payload={"hands": 70},
        causation_id="native-requested",
    )
    lease = store.claim_effect(
        "native-149", owner="consumer-a", lease_seconds=30, now=100
    )
    barrier = __import__("threading").Barrier(2)

    def cancel(index):
        barrier.wait()
        try:
            store.cancel_effect(
                "native-149",
                expected_status="running",
                expected_attempt=lease.attempt,
                expected_lease_epoch=lease.lease_epoch,
                expected_owner="consumer-a",
                reason="contract_changed",
                causation_id=f"concurrent-cancel-{index}",
                now=105,
            )
            return "cancelled"
        except WorkflowConflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(cancel, range(2)))
    assert sorted(results) == ["cancelled", "stale"]
    assert store.effect("native-149")["status"] == "abandoned"
    assert len([
        event
        for event in store.events("149#0")
        if event.event_type == "EffectCancelled"
    ]) == 1


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


def test_interrupted_effect_is_attempt_neutral_reclaimable_and_owner_fenced(
    tmp_path,
):
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
        "worker-149", owner="pid:101", lease_seconds=3600, now=10
    )
    kwargs = {
        "expected_owner": "pid:101",
        "lease_epoch": first.lease_epoch,
        "claimed_attempt": first.attempt,
        "interruption_kind": "operator_shutdown",
        "reason": "operator_shutdown",
        "metadata": {
            "workflow_run_id": "149#0",
            "shutdown_requested": True,
        },
        "causation_id": "worker-149-operator-shutdown-1",
        "now": 11,
    }

    with pytest.raises(InvalidCompletion, match="stale effect interruption"):
        store.interrupt_effect(
            "worker-149",
            **{**kwargs, "expected_owner": "pid:forged"},
        )
    assert store.effect("worker-149")["status"] == "running"
    assert store.effect("worker-149")["attempt"] == 1

    interrupted = store.interrupt_effect("worker-149", **kwargs)
    replay = store.interrupt_effect("worker-149", **kwargs)

    assert interrupted["status"] == "retry"
    assert interrupted["attempt"] == 0
    assert interrupted["lease_epoch"] == 1
    assert interrupted["lease_owner"] is None
    assert interrupted["lease_until"] is None
    assert replay == interrupted
    assert [row["effect_id"] for row in store.pending_outbox(now=11)] == [
        "worker-149"
    ]
    event = store.events("149#0")[-1]
    assert event.event_type == "EffectInterrupted"
    assert event.payload == {
        "effect_id": "worker-149",
        "claimed_attempt": 1,
        "restored_attempt": 0,
        "lease_epoch": 1,
        "lease_owner": "pid:101",
        "interruption_kind": "operator_shutdown",
        "reason": "operator_shutdown",
        "metadata": {
            "workflow_run_id": "149#0",
            "shutdown_requested": True,
        },
    }
    assert not any(
        item.event_type == "EffectFailed" for item in store.events("149#0")
    )

    # Reopening the durable store models process restart.  The exact frozen
    # effect is immediately claimable with the same attempt and a new epoch.
    restarted = WorkflowStore(store.path)
    second = restarted.claim_effect(
        "worker-149", owner="pid:202", lease_seconds=3600, now=12
    )
    assert second.attempt == first.attempt
    assert second.lease_epoch == first.lease_epoch + 1
    assert restarted.effect("worker-149")["lease_owner"] == "pid:202"

    stale = restarted.complete_effect(
        "worker-149",
        lease_epoch=first.lease_epoch,
        completion_id="late-old-worker",
        result_payload={"artifact": "stale"},
        causation_id="late-old-worker",
    )
    assert stale["accepted"] is False
    with pytest.raises(InvalidCompletion, match="stale effect interruption replay"):
        restarted.interrupt_effect("worker-149", **kwargs)
    assert restarted.effect("worker-149")["lease_owner"] == "pid:202"


def test_interrupted_effect_missing_outbox_rolls_back_event_and_projection(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={"task_digest": "stable"},
        causation_id="request-worker-149",
        max_attempts=3,
    )
    lease = store.claim_effect(
        "worker-149",
        owner="pid:101",
        lease_seconds=3600,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM outbox WHERE effect_id = ?",
            (lease.effect_id,),
        )

    with pytest.raises(WorkflowConflict, match="interruption outbox missing"):
        store.interrupt_effect(
            lease.effect_id,
            expected_owner="pid:101",
            lease_epoch=lease.lease_epoch,
            claimed_attempt=lease.attempt,
            interruption_kind="operator_shutdown",
            reason="operator_shutdown",
            metadata={
                "workflow_run_id": "149#0",
                "shutdown_requested": True,
            },
            causation_id="worker-149-operator-shutdown-1",
        )

    effect = store.effect(lease.effect_id)
    assert effect["status"] == "running"
    assert effect["attempt"] == lease.attempt
    assert effect["lease_owner"] == "pid:101"
    assert not any(
        event.event_type == "EffectInterrupted"
        for event in store.events("149#0")
    )


def test_interrupted_effect_duplicate_replay_requires_dispatchable_outbox(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="worker_llm",
        input_payload={"task_digest": "stable"},
        causation_id="request-worker-149",
        max_attempts=3,
    )
    lease = store.claim_effect(
        "worker-149",
        owner="pid:101",
        lease_seconds=3600,
        now=10,
    )
    kwargs = {
        "expected_owner": "pid:101",
        "lease_epoch": lease.lease_epoch,
        "claimed_attempt": lease.attempt,
        "interruption_kind": "operator_shutdown",
        "reason": "operator_shutdown",
        "metadata": {
            "workflow_run_id": "149#0",
            "shutdown_requested": True,
        },
        "causation_id": "worker-149-operator-shutdown-1",
        "now": 11,
    }
    store.interrupt_effect(lease.effect_id, **kwargs)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE outbox SET dispatched_at = ? WHERE effect_id = ?",
            (11, lease.effect_id),
        )

    with pytest.raises(
        WorkflowConflict,
        match="interruption replay outbox unavailable",
    ):
        store.interrupt_effect(lease.effect_id, **kwargs)


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


def test_dead_owner_reclaim_is_owner_epoch_cas_and_rejects_late_completion(tmp_path):
    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="native_match",
        input_payload={"sample": 5},
        causation_id="requested",
    )
    old = store.claim_effect(
        "worker-149",
        owner="parent-v2:old",
        lease_seconds=100,
        now=10,
    )
    proof = {"owner": "parent-v2:old", "reason": "owner_pid_missing"}
    from workflow_kernel import content_digest

    proof["proof_digest"] = content_digest(proof)
    reclaimed = store.reclaim_effect_lease(
        "worker-149",
        expected_owner="parent-v2:old",
        expected_lease_epoch=old.lease_epoch,
        owner="parent-v2:new",
        lease_seconds=100,
        causation_id="dead-owner-reclaimed",
        proof=proof,
        now=11,
    )
    assert reclaimed.attempt == 2
    assert reclaimed.lease_epoch == 2
    assert reclaimed.lease_until == 111
    assert store.events("149#0")[-1].event_type == "EffectLeaseReclaimed"

    wrong_owner_proof = {
        "owner": "other-owner",
        "reason": "owner_pid_missing",
    }
    wrong_owner_proof["proof_digest"] = content_digest(wrong_owner_proof)
    with pytest.raises(WorkflowConflict, match="stale effect lease reclaim"):
        store.reclaim_effect_lease(
            "worker-149",
            expected_owner="other-owner",
            expected_lease_epoch=old.lease_epoch,
            owner="parent-v2:third",
            lease_seconds=100,
            causation_id="wrong-owner-reclaim",
            proof=wrong_owner_proof,
            now=11,
        )

    stale = store.complete_effect(
        "worker-149",
        lease_epoch=old.lease_epoch,
        completion_id="old-owner-completion",
        result_payload={"result": "late"},
        causation_id="old-owner-completed",
        require_live_lease=False,
        now=12,
    )
    assert stale["accepted"] is False
    assert stale["reason"] == "stale_lease_epoch"
    stale_live_required = store.complete_effect(
        "worker-149",
        lease_epoch=old.lease_epoch,
        completion_id="old-owner-completion-live-required",
        result_payload={"result": "late"},
        causation_id="old-owner-completed-live-required",
        require_live_lease=True,
        now=12,
    )
    assert stale_live_required["accepted"] is False
    assert stale_live_required["reason"] == "stale_lease_epoch"
    assert store.effect("worker-149")["status"] == "running"


def test_dead_owner_reclaim_rejects_tampered_proof_and_exhausts_without_epoch_drift(
    tmp_path,
):
    from workflow_kernel import content_digest

    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="native_match",
        input_payload={"sample": 5},
        causation_id="requested",
        max_attempts=1,
    )
    old = store.claim_effect(
        "worker-149",
        owner="parent-v2:old",
        lease_seconds=100,
        now=10,
    )
    proof = {"owner": "parent-v2:old", "reason": "owner_pid_missing"}
    proof["proof_digest"] = content_digest(proof)
    tampered = dict(proof)
    tampered["reason"] = "owner_is_actually_live"
    with pytest.raises(ValueError, match="proof digest"):
        store.reclaim_effect_lease(
            "worker-149",
            expected_owner="parent-v2:old",
            expected_lease_epoch=old.lease_epoch,
            owner="parent-v2:new",
            lease_seconds=100,
            causation_id="tampered",
            proof=tampered,
            now=11,
        )
    unchanged = store.effect("worker-149")
    assert unchanged["status"] == "running"
    assert unchanged["attempt"] == 1
    assert unchanged["lease_epoch"] == 1

    with pytest.raises(WorkflowConflict, match="attempt budget exhausted"):
        store.reclaim_effect_lease(
            "worker-149",
            expected_owner="parent-v2:old",
            expected_lease_epoch=old.lease_epoch,
            owner="parent-v2:new",
            lease_seconds=100,
            causation_id="dead-owner-reclaim",
            proof=proof,
            now=11,
        )
    exhausted = store.effect("worker-149")
    assert exhausted["status"] == "exhausted"
    assert exhausted["attempt"] == 1
    assert exhausted["lease_epoch"] == 1
    events = store.events("149#0")
    assert not any(event.event_type == "EffectLeaseReclaimed" for event in events)
    failed = [event for event in events if event.event_type == "EffectFailed"]
    assert len(failed) == 1
    assert failed[0].payload["proof"] == proof
    late = store.complete_effect(
        "worker-149",
        lease_epoch=old.lease_epoch,
        completion_id="late-after-exhaustion",
        result_payload={"result": "late"},
        causation_id="late-after-exhaustion",
        require_live_lease=False,
        now=12,
    )
    assert late["accepted"] is False


def test_dead_owner_reclaim_concurrency_has_exactly_one_new_epoch(tmp_path):
    from workflow_kernel import content_digest

    store = _store(tmp_path)
    store.request_effect(
        run_id="149#0",
        effect_id="worker-149",
        kind="native_match",
        input_payload={"sample": 5},
        causation_id="requested",
        max_attempts=3,
    )
    old = store.claim_effect(
        "worker-149",
        owner="parent-v2:old",
        lease_seconds=100,
        now=10,
    )
    proof = {"owner": "parent-v2:old", "reason": "owner_pid_missing"}
    proof["proof_digest"] = content_digest(proof)
    barrier = __import__("threading").Barrier(2)

    def reclaim(index):
        barrier.wait()
        try:
            lease = store.reclaim_effect_lease(
                "worker-149",
                expected_owner="parent-v2:old",
                expected_lease_epoch=old.lease_epoch,
                owner=f"parent-v2:new-{index}",
                lease_seconds=100,
                causation_id=f"dead-owner-reclaim-{index}",
                proof=proof,
                now=11,
            )
            return ("claimed", lease.lease_epoch)
        except WorkflowConflict:
            return ("stale", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reclaim, range(2)))
    assert sorted(result[0] for result in results) == ["claimed", "stale"]
    effect = store.effect("worker-149")
    assert effect["attempt"] == 2
    assert effect["lease_epoch"] == 2
    assert len([
        event
        for event in store.events("149#0")
        if event.event_type == "EffectLeaseReclaimed"
    ]) == 1


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


# ---------------------------------------------------------------------------
# Defect E (b) regression: request_effect status gate narrowed by effect kind.
#
# The Slice-2b seal deliberately attaches a ``producer-consumer-job:*`` effect
# to a worker-journal run_id whose instance is already ``status="completed"``
# at the seal point (worker_workflow.projected flips the worker journal to
# "completed" at workers_done).  request_effect must admit such seal effects on
# a completed instance (otherwise the seal crashes -- the historical "0
# producer-consumer effects in the entire runtime history" + commit 7682ce95
# disable).  Worker effects (worker_llm / system_blueprint) and any unknown
# kind still require a running instance, so worker-effect safety is unchanged.
# ---------------------------------------------------------------------------


def _completed_store(tmp_path):
    """A WorkflowStore whose single instance is status='completed'."""
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    store.ensure_instance("200#0", definition_version=1)  # born "running"
    store.append_event_and_set_status(
        "200#0",
        event_type="WorkerProjected",
        payload={"stage": "workers_done"},
        causation_id="projected-200",
        expected_version=0,
        status="completed",
    )
    return store


def test_request_effect_accepts_producer_consumer_kind_on_completed_instance(tmp_path):
    """Seal effect (producer-consumer-job:*) on a COMPLETED journal succeeds."""
    store = _completed_store(tmp_path)
    effect = store.request_effect(
        run_id="200#0",
        effect_id="slice2b-seal-200",
        kind="producer-consumer-job:quality-static",
        input_payload={"envelope_digest": "abc"},
        causation_id="seal-200",
    )
    assert effect["effect_id"] == "slice2b-seal-200"
    assert effect["kind"] == "producer-consumer-job:quality-static"
    assert effect["status"] == "requested"
    # The idempotent replay path is now also reachable for seal effects on a
    # completed instance (previously the status gate defeated replay).
    duplicate = store.request_effect(
        run_id="200#0",
        effect_id="slice2b-seal-200",
        kind="producer-consumer-job:quality-static",
        input_payload={"envelope_digest": "abc"},
        causation_id="seal-200",
    )
    assert duplicate["effect_id"] == "slice2b-seal-200"


def test_request_effect_rejects_worker_kind_on_completed_instance(tmp_path):
    """worker_llm effect on a COMPLETED journal is still rejected."""
    store = _completed_store(tmp_path)
    with pytest.raises(WorkflowConflict, match="not running"):
        store.request_effect(
            run_id="200#0",
            effect_id="worker-200",
            kind="worker_llm",
            input_payload={"task_digest": "abc"},
            causation_id="request-worker-200",
        )


def test_request_effect_rejects_unknown_kind_on_completed_instance(tmp_path):
    """Unknown kind on a COMPLETED journal is rejected (worker safety)."""
    store = _completed_store(tmp_path)
    with pytest.raises(WorkflowConflict, match="not running"):
        store.request_effect(
            run_id="200#0",
            effect_id="mystery-200",
            kind="something-else",
            input_payload={"x": 1},
            causation_id="request-mystery-200",
        )


def test_request_effect_still_requires_running_for_worker_kind_on_running(tmp_path):
    """Baseline: worker_llm still works on a RUNNING instance (unchanged)."""
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    store.ensure_instance("201#0", definition_version=1)  # running
    effect = store.request_effect(
        run_id="201#0",
        effect_id="worker-201",
        kind="worker_llm",
        input_payload={"task_digest": "abc"},
        causation_id="request-worker-201",
    )
    assert effect["status"] == "requested"


def test_request_effect_rejects_any_kind_on_missing_instance(tmp_path):
    """No instance at all still raises (seal-kind relaxation is not a bypass)."""
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    with pytest.raises(WorkflowConflict, match="not running"):
        store.request_effect(
            run_id="never-created",
            effect_id="slice2b-seal-x",
            kind="producer-consumer-job:quality-static",
            input_payload={"envelope_digest": "abc"},
            causation_id="seal-x",
        )
