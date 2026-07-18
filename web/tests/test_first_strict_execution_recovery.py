"""Regression coverage for fenced first-strict lease recovery.

The first strict control samples are physical 70-hand executions.  A cancelled
caller must never mistake the matching live lease for a candidate regression,
nor start a second pair of subprocesses before the original fenced effect has
expired or completed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace
import time

import pytest

import first_strict_execution_journal as journal
import national_native
import precommit_eval_contract
import tool_eval
from workflow_kernel import WorkflowConflict, WorkflowStore


def _scope(timing_plan):
    return {
        "workflow_run_id": "generation:143:lease-recovery",
        "checkpoint_revision": 7,
        "candidate_version": 143,
        "candidate_label": "national_v143",
        "candidate_artifact_hash": "a" * 64,
        "control_id": "first_strict_control_v1",
        "control_artifact_hash": "b" * 64,
        "control_receipt_digest": "c" * 64,
        "precommit_plan_digest": "d" * 64,
        "evaluation_contract_digest": "e" * 64,
        "native_match_timing_plan_digest": timing_plan.digest(),
        "precommit_attempt": 1,
    }


def _first_strict_batch_plan(timing_plan):
    sample_plan = [
        {
            "opponent": "first_strict_control_v1",
            "opponent_index": 0,
            "repeat": repeat,
            "deck_seed_base": 91_000 + (repeat - 1) * 1_000,
            "bot_seed_base": 1_000_091_000 + (repeat - 1) * 1_000,
            "native_match_timing_plan_digest": timing_plan.digest(),
        }
        for repeat in range(1, 9)
    ]
    return sample_plan, precommit_eval_contract.build_native_precommit_batch_plan(
        sample_plan,
        native_timing_plan=timing_plan,
        first_strict_control=True,
    )


def _begin(scope, timing_plan, *, claim_now, deck_seed_base=91_000):
    return journal.begin_control_execution(
        scope=scope,
        repeat=1,
        deck_seed_base=deck_seed_base,
        bot_seed_base=deck_seed_base + 1_000_000_000,
        timing_plan=timing_plan,
        claim_now=claim_now,
    )


def _minimal_completion_authority(monkeypatch):
    """Install a small seal/proof boundary so lock tests isolate persistence."""

    execution = {"test_terminal_execution": True}
    terminal_proof = {"test_terminal_proof": True}
    consumed = []
    monkeypatch.setattr(
        national_native,
        "_validate_first_strict_runner_execution_seal",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        national_native,
        "_consume_first_strict_runner_execution_seal",
        lambda *_args, **_kwargs: consumed.append(True),
    )
    monkeypatch.setattr(
        journal,
        "_terminal_execution_issues",
        lambda *_args, **_kwargs: ([], terminal_proof),
    )
    return execution, consumed


def _complete_control_repeats(scope, timing_plan, execution, count):
    receipts = []
    tickets = []
    for repeat in range(1, count + 1):
        deck = 91_000 + (repeat - 1) * 1_000
        ticket = journal.begin_control_execution(
            scope=scope,
            repeat=repeat,
            deck_seed_base=deck,
            bot_seed_base=deck + 1_000_000_000,
            timing_plan=timing_plan,
            claim_now=time.time(),
        )
        receipts.append(
            journal.complete_control_execution(ticket, execution=execution)
        )
        tickets.append(ticket)
    return tickets, receipts


def _assert_completion_not_recorded(store, ticket):
    effect = store.effect(ticket["effect_id"])
    assert effect["status"] == "running"
    assert effect.get("result_payload") is None
    event_types = [
        event.event_type for event in store.events(ticket["authority_run_id"])
    ]
    assert journal.EXECUTION_EVENT_TYPE not in event_types
    assert "EffectCompleted" not in event_types
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


def test_live_control_lease_returns_pending_then_same_input_reclaims_after_expiry(
    tmp_path, monkeypatch
):
    """Cancellation/retry is a durable wait; expiry permits only same-input reclaim."""

    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)

    first = _begin(scope, timing_plan, claim_now=100.0)
    assert set(first) == {
        "authority_run_id",
        "effect_id",
        "lease_epoch",
        "match_run_id",
        "input_payload",
    }
    assert first["lease_epoch"] == 1

    # A second caller with the exact frozen scope/effect sees an explicit
    # non-executable recovery state.  It cannot be passed to the runner and it
    # does not consume a second effect attempt.
    pending = _begin(scope, timing_plan, claim_now=101.0)
    assert pending["state"] == "pending"
    assert pending["pending"] is True
    assert pending["recovered"] is False
    assert pending["authority_run_id"] == first["authority_run_id"]
    assert pending["effect_id"] == first["effect_id"]
    assert pending["match_run_id"] == first["match_run_id"]
    assert pending["input_payload"] == first["input_payload"]
    assert pending["lease_epoch"] == first["lease_epoch"]
    assert pending["attempt"] == 1
    assert pending["max_attempts"] == 3
    assert pending["lease_until"] > 101.0
    assert journal.is_pending_control_execution(
        pending,
        expected_scope=scope,
        now=101.0,
    )
    assert journal.normalize_pending_control_execution(
        pending, expected_scope=scope
    ) == pending

    store = journal._store()
    live_effect = store.effect(first["effect_id"])
    assert live_effect["status"] == "running"
    assert live_effect["attempt"] == 1
    assert live_effect["lease_epoch"] == 1
    owner = journal._LOCAL_PARENT_OWNER_V2_RE.fullmatch(
        live_effect["lease_owner"]
    )
    assert owner is not None
    assert int(owner.group("pid")) == journal.os.getpid()
    assert owner.group("boot") == journal._local_boot_id()
    assert owner.group("start") == journal._observe_local_process(
        journal.os.getpid()
    )["start_token"]

    # After expiry the identical frozen input is reclaimable.  The new lease
    # epoch fences a stale completion from the cancelled owner.
    reclaimed = _begin(
        scope,
        timing_plan,
        claim_now=pending["lease_until"] + 0.001,
    )
    assert set(reclaimed) == set(first)
    assert reclaimed["input_payload"] == first["input_payload"]
    assert reclaimed["effect_id"] == first["effect_id"]
    assert reclaimed["lease_epoch"] == 2
    reclaimed_effect = store.effect(first["effect_id"])
    assert reclaimed_effect["attempt"] == 2
    assert reclaimed_effect["lease_epoch"] == 2

    # A different seed is a different physical sample.  It must fail closed,
    # never piggy-back on the existing effect or open a parallel lease.
    with pytest.raises(WorkflowConflict, match="effect id reused with different input"):
        _begin(
            scope,
            timing_plan,
            claim_now=pending["lease_until"] + 1.0,
            deck_seed_base=92_000,
        )


@pytest.mark.parametrize(
    ("owner", "observation", "expected_reason"),
    [
        ("remote:host-a:123", {"state": "missing", "pid": 123}, None),
        (
            "parent-v2:123:11111111-1111-1111-1111-111111111111:77:"
            + "a" * 32,
            {"state": "missing", "pid": 123, "start_token": None},
            "owner_pid_missing",
        ),
        (
            "parent-v2:123:22222222-2222-2222-2222-222222222222:77:"
            + "a" * 32,
            {"state": "unknown", "pid": 123, "start_token": None},
            "owner_boot_identity_changed",
        ),
        (
            "parent-v2:123:11111111-1111-1111-1111-111111111111:77:"
            + "a" * 32,
            {"state": "present", "pid": 123, "start_token": "77"},
            None,
        ),
        (
            "parent-v2:123:11111111-1111-1111-1111-111111111111:77:"
            + "a" * 32,
            {"state": "present", "pid": 123, "start_token": "88"},
            "owner_process_start_identity_changed",
        ),
        (
            "parent:123:" + "a" * 32,
            {"state": "present", "pid": 123, "start_token": "77"},
            None,
        ),
        (
            "parent:123:" + "a" * 32,
            {"state": "missing", "pid": 123, "start_token": None},
            "legacy_owner_pid_missing",
        ),
        (
            "parent-v2:123:11111111-1111-1111-1111-111111111111:77:"
            + "a" * 32,
            {"state": "unknown", "pid": 123, "start_token": None},
            None,
        ),
    ],
)
def test_dead_owner_proof_refuses_remote_live_or_unknown_process(
    monkeypatch,
    owner,
    observation,
    expected_reason,
):
    boot_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(journal, "_local_boot_id", lambda: boot_id)
    monkeypatch.setattr(
        journal,
        "_observe_local_process",
        lambda _pid: dict(observation),
    )

    proof = journal._dead_local_parent_owner_proof(owner, observed_at=100.0)

    if expected_reason is None:
        assert proof is None
    else:
        assert proof["reason"] == expected_reason
        assert proof["owner"] == owner
        assert len(proof["proof_digest"]) == 64


def test_unavailable_boot_identity_is_never_death_authority(monkeypatch):
    owner = (
        "parent-v2:123:11111111-1111-1111-1111-111111111111:77:"
        + "a" * 32
    )
    monkeypatch.setattr(
        journal,
        "_local_boot_id",
        lambda: (_ for _ in ()).throw(
            journal.FirstStrictExecutionJournalError("boot unavailable")
        ),
    )
    monkeypatch.setattr(
        journal,
        "_observe_local_process",
        lambda _pid: pytest.fail("process observation followed missing boot identity"),
    )

    assert journal._dead_local_parent_owner_proof(
        owner,
        observed_at=100.0,
    ) is None


@pytest.mark.parametrize(
    ("owner", "observation"),
    [
        ("remote-parent:host-a:123", {"state": "missing", "pid": 123}),
        (
            "parent-v2:123:11111111-1111-1111-1111-111111111111:77:"
            + "d" * 32,
            {"state": "present", "pid": 123, "start_token": "77"},
        ),
        (
            "parent:123:" + "d" * 32,
            {"state": "present", "pid": 123, "start_token": "77"},
        ),
        (
            "parent-v2:123:11111111-1111-1111-1111-111111111111:77:"
            + "d" * 32,
            {"state": "unknown", "pid": 123, "start_token": None},
        ),
    ],
)
def test_begin_never_reclaims_remote_live_legacy_live_or_unknown_owner(
    tmp_path,
    monkeypatch,
    owner,
    observation,
):
    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)
    base_now = time.time()
    first = _begin(scope, timing_plan, claim_now=base_now)
    store = journal._store()
    with store._connect() as connection:
        connection.execute(
            "UPDATE effects SET lease_owner = ?, lease_until = ? WHERE effect_id = ?",
            (owner, base_now + 8_000.0, first["effect_id"]),
        )
    monkeypatch.setattr(
        journal,
        "_local_boot_id",
        lambda: "11111111-1111-1111-1111-111111111111",
    )
    monkeypatch.setattr(
        journal,
        "_observe_local_process",
        lambda _pid: dict(observation),
    )

    pending = _begin(scope, timing_plan, claim_now=base_now + 1.0)

    assert pending["state"] == "pending"
    assert pending["attempt"] == 1
    effect = store.effect(first["effect_id"])
    assert effect["attempt"] == 1
    assert effect["lease_epoch"] == 1
    assert effect["lease_owner"] == owner
    assert not any(
        event.event_type == "EffectLeaseReclaimed"
        for event in store.events(first["authority_run_id"])
    )


def test_v2_reused_pid_start_identity_reclaims_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)
    base_now = time.time()
    first = _begin(scope, timing_plan, claim_now=base_now)
    store = journal._store()
    boot_id = "11111111-1111-1111-1111-111111111111"
    stale_owner = f"parent-v2:123:{boot_id}:77:" + "e" * 32
    replacement_owner = f"parent-v2:456:{boot_id}:99:" + "f" * 32
    with store._connect() as connection:
        connection.execute(
            "UPDATE effects SET lease_owner = ?, lease_until = ? WHERE effect_id = ?",
            (stale_owner, base_now + 8_000.0, first["effect_id"]),
        )
    monkeypatch.setattr(journal, "_local_boot_id", lambda: boot_id)

    def observe(pid):
        if int(pid) == 123:
            return {"state": "present", "pid": 123, "start_token": "88"}
        return {"state": "present", "pid": int(pid), "start_token": "99"}

    monkeypatch.setattr(journal, "_observe_local_process", observe)
    monkeypatch.setattr(
        journal,
        "_new_local_parent_owner",
        lambda: replacement_owner,
    )

    reclaimed = _begin(scope, timing_plan, claim_now=base_now + 1.0)

    assert reclaimed["lease_epoch"] == 2
    effect = store.effect(first["effect_id"])
    assert effect["attempt"] == 2
    assert effect["lease_owner"] == replacement_owner
    reclaimed_events = [
        event
        for event in store.events(first["authority_run_id"])
        if event.event_type == "EffectLeaseReclaimed"
    ]
    assert len(reclaimed_events) == 1
    assert reclaimed_events[0].payload["proof"]["reason"] == (
        "owner_process_start_identity_changed"
    )


def test_dead_legacy_parent_reclaims_only_repeat5_once_and_rejects_late_result(
    tmp_path,
    monkeypatch,
):
    """A stopped old runtime cannot strand repeat5 behind a two-hour lease."""

    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)
    execution, _consumed = _minimal_completion_authority(monkeypatch)
    base_now = time.time()

    def begin_repeat(repeat, *, now):
        deck = 91_000 + (repeat - 1) * 1_000
        return journal.begin_control_execution(
            scope=scope,
            repeat=repeat,
            deck_seed_base=deck,
            bot_seed_base=deck + 1_000_000_000,
            timing_plan=timing_plan,
            claim_now=now,
        )

    # Already completed samples are replayed, not reclaimed or rerun.
    for repeat in range(1, 5):
        ticket = begin_repeat(repeat, now=base_now + repeat)
        journal.complete_control_execution(ticket, execution=execution)
        recovered = begin_repeat(repeat, now=base_now + 10.0)
        assert recovered["state"] == "recovered"
        assert recovered["execution"] == execution
        assert journal._store().effect(ticket["effect_id"])["attempt"] == 1

    old_ticket = begin_repeat(5, now=base_now + 10.0)
    store = journal._store()
    legacy_owner = "parent:999999:" + "b" * 32
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE effects SET lease_owner = ?, lease_until = ?
            WHERE effect_id = ?
            """,
            (legacy_owner, base_now + 8_000.0, old_ticket["effect_id"]),
        )

    boot_id = "11111111-1111-1111-1111-111111111111"
    new_owner = f"parent-v2:123:{boot_id}:77:" + "c" * 32
    monkeypatch.setattr(journal, "_local_boot_id", lambda: boot_id)

    def observe(pid):
        if int(pid) == 999999:
            return {"state": "missing", "pid": int(pid), "start_token": None}
        if int(pid) == 123:
            return {"state": "present", "pid": int(pid), "start_token": "77"}
        return {"state": "unknown", "pid": int(pid), "start_token": None}

    monkeypatch.setattr(journal, "_observe_local_process", observe)
    monkeypatch.setattr(journal, "_new_local_parent_owner", lambda: new_owner)

    reclaimed = begin_repeat(5, now=base_now + 11.0)
    assert reclaimed["lease_epoch"] == 2
    effect = store.effect(old_ticket["effect_id"])
    assert effect["attempt"] == 2
    assert effect["lease_epoch"] == 2
    assert effect["lease_owner"] == new_owner
    assert effect["lease_until"] > base_now + 11.0

    # A second restart while the replacement parent is alive does not consume
    # attempt 3 or launch another match.
    pending = begin_repeat(5, now=base_now + 12.0)
    assert pending["state"] == "pending"
    assert pending["attempt"] == 2
    assert store.effect(old_ticket["effect_id"])["attempt"] == 2

    with pytest.raises(
        journal.FirstStrictExecutionJournalError,
        match="first_strict_execution_stale_completion",
    ):
        journal.complete_control_execution(old_ticket, execution=execution)
    assert store.effect(old_ticket["effect_id"])["status"] == "running"

    receipt = journal.complete_control_execution(reclaimed, execution=execution)
    assert receipt["effect_id"] == reclaimed["effect_id"]
    recovered = begin_repeat(5, now=base_now + 13.0)
    assert recovered["state"] == "recovered"
    assert store.effect(old_ticket["effect_id"])["attempt"] == 2
    reclaimed_events = [
        event
        for event in store.events(old_ticket["authority_run_id"])
        if event.event_type == "EffectLeaseReclaimed"
    ]
    assert len(reclaimed_events) == 1
    assert reclaimed_events[0].payload["proof"]["reason"] == (
        "legacy_owner_pid_missing"
    )


def test_control_authority_succeeds_only_after_exact_eight_receipts(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)
    execution, _consumed = _minimal_completion_authority(monkeypatch)
    _tickets, first_five = _complete_control_repeats(
        scope,
        timing_plan,
        execution,
        5,
    )

    with pytest.raises(
        journal.FirstStrictExecutionJournalError,
        match="first_strict_execution_success_effect_set_invalid",
    ):
        journal.succeed_control_execution(
            scope,
            expected_receipts=first_five,
        )
    authority_run_id = journal._authority_run_id(scope)
    assert journal._store().instance(authority_run_id)["status"] == "running"

    remaining_receipts = []
    for repeat in range(6, 9):
        deck = 91_000 + (repeat - 1) * 1_000
        ticket = journal.begin_control_execution(
            scope=scope,
            repeat=repeat,
            deck_seed_base=deck,
            bot_seed_base=deck + 1_000_000_000,
            timing_plan=timing_plan,
            claim_now=time.time(),
        )
        remaining_receipts.append(
            journal.complete_control_execution(ticket, execution=execution)
        )
    all_receipts = [*first_five, *remaining_receipts]
    terminal = journal.succeed_control_execution(
        scope,
        expected_receipts=all_receipts,
    )
    assert terminal["outcome"] == "succeeded"
    assert len(terminal["effects"]["completed"]) == 8
    assert terminal["effects"]["nonterminal"] == []
    assert journal.succeed_control_execution(
        scope,
        expected_receipts=all_receipts,
    ) == terminal
    assert journal.read_succeeded_control_execution(
        scope,
        expected_receipts=all_receipts,
        expected_terminal_receipt=terminal,
    ) == terminal

    # A verified-checkpoint crash replays exact completed effects; it cannot
    # request a ninth sample or change an existing sample identity.
    recovered = journal.begin_control_execution(
        scope=scope,
        repeat=8,
        deck_seed_base=98_000,
        bot_seed_base=1_000_098_000,
        timing_plan=timing_plan,
        claim_now=time.time(),
    )
    assert recovered["state"] == "recovered"
    with pytest.raises(
        journal.FirstStrictExecutionJournalError,
        match="first_strict_execution_succeeded_recovery_invalid",
    ):
        journal.begin_control_execution(
            scope=scope,
            repeat=8,
            deck_seed_base=999_000,
            bot_seed_base=1_000_999_000,
            timing_plan=timing_plan,
            claim_now=time.time(),
        )


def test_control_authority_abandon_preserves_receipts_and_fences_running_repeat(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)
    execution, _consumed = _minimal_completion_authority(monkeypatch)
    _tickets, receipts = _complete_control_repeats(
        scope,
        timing_plan,
        execution,
        4,
    )
    deck = 95_000
    running = journal.begin_control_execution(
        scope=scope,
        repeat=5,
        deck_seed_base=deck,
        bot_seed_base=deck + 1_000_000_000,
        timing_plan=timing_plan,
        claim_now=time.time(),
    )

    terminal = journal.abandon_control_execution(
        scope,
        reason="operator_abandon_v58",
    )
    assert terminal["outcome"] == "abandoned"
    assert len(terminal["effects"]["completed"]) == 4
    assert len(terminal["effects"]["abandoned"]) == 1
    assert terminal["effects"]["nonterminal"] == []
    assert journal.abandon_control_execution(
        scope,
        reason="operator_abandon_v58",
    ) == terminal
    for reference in receipts:
        evidence, issues = journal.read_control_execution_receipt(
            reference,
            expected_scope=scope,
        )
        assert issues == []
        assert evidence["execution"] == execution
    with pytest.raises(
        journal.FirstStrictExecutionJournalError,
        match="first_strict_execution_stale_completion",
    ):
        journal.complete_control_execution(running, execution=execution)
    with pytest.raises(
        journal.FirstStrictExecutionJournalError,
        match="first_strict_execution_authority_terminal",
    ):
        journal.begin_control_execution(
            scope=scope,
            repeat=6,
            deck_seed_base=96_000,
            bot_seed_base=1_000_096_000,
            timing_plan=timing_plan,
            claim_now=time.time(),
        )


def test_control_authority_abandon_creates_exact_pre_effect_tombstone(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)

    terminal = journal.abandon_control_execution(scope, reason="cancelled")
    assert terminal["outcome"] == "abandoned"
    assert terminal["effects"] == {
        "completed": [],
        "abandoned": [],
        "exhausted": [],
        "nonterminal": [],
    }
    with pytest.raises(
        journal.FirstStrictExecutionJournalError,
        match="first_strict_execution_authority_terminal",
    ):
        _begin(scope, timing_plan, claim_now=time.time())


def test_begin_and_abandon_share_instance_creation_lock_and_abandon_wins(
    tmp_path,
    monkeypatch,
):
    """There is no no-instance -> running lease race across the terminal fence."""

    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)
    execution, _consumed = _minimal_completion_authority(monkeypatch)
    local = threading.local()
    original_lock = WorkflowStore.command_lock
    original_ensure = WorkflowStore.ensure_instance

    from contextlib import contextmanager

    @contextmanager
    def observed_lock(store, run_id, **kwargs):
        with original_lock(store, run_id, **kwargs):
            local.inside_command_lock = True
            try:
                yield
            finally:
                local.inside_command_lock = False

    def guarded_ensure(store, *args, **kwargs):
        assert getattr(local, "inside_command_lock", False) is True
        return original_ensure(store, *args, **kwargs)

    monkeypatch.setattr(WorkflowStore, "command_lock", observed_lock)
    monkeypatch.setattr(WorkflowStore, "ensure_instance", guarded_ensure)
    start = threading.Barrier(2)
    results = {}
    errors = {}

    def begin():
        start.wait()
        try:
            results["begin"] = _begin(
                scope,
                timing_plan,
                claim_now=time.time(),
            )
        except Exception as exc:  # abandon may create the tombstone first
            errors["begin"] = exc

    def abandon():
        start.wait()
        try:
            results["abandon"] = journal.abandon_control_execution(
                scope,
                reason="concurrent_operator_abandon",
            )
        except Exception as exc:
            errors["abandon"] = exc

    threads = [threading.Thread(target=begin), threading.Thread(target=abandon)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    assert "abandon" not in errors
    assert results["abandon"]["outcome"] == "abandoned"
    instance = journal._store().instance(journal._authority_run_id(scope))
    assert instance["status"] == "abandoned"
    assert instance["fence_epoch"] == 1
    assert results["abandon"]["effects"]["nonterminal"] == []
    if "begin" in results:
        with pytest.raises(
            journal.FirstStrictExecutionJournalError,
            match="first_strict_execution_stale_completion",
        ):
            journal.complete_control_execution(
                results["begin"],
                execution=execution,
            )
    else:
        assert isinstance(
            errors.get("begin"),
            journal.FirstStrictExecutionJournalError,
        )


def test_pending_payload_rejects_scope_or_ticket_tampering(tmp_path, monkeypatch):
    """Only a canonical live-lease payload may suppress failure/abandon paths."""

    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)
    claim_now = time.time()
    _begin(scope, timing_plan, claim_now=claim_now)
    pending = _begin(scope, timing_plan, claim_now=claim_now + 0.001)

    tampered = dict(pending)
    tampered["lease_epoch"] = 2
    assert not journal.is_pending_control_execution(
        tampered,
        expected_scope=scope,
        now=101.0,
    )
    with pytest.raises(
        journal.FirstStrictExecutionJournalError,
        match="first_strict_execution_pending_effect_binding_invalid",
    ):
        journal.read_pending_control_execution(tampered, expected_scope=scope)

    other_scope = dict(scope)
    other_scope["checkpoint_revision"] += 1
    with pytest.raises(
        journal.FirstStrictExecutionJournalError,
        match="first_strict_execution_pending_scope_mismatch",
    ):
        journal.normalize_pending_control_execution(
            pending,
            expected_scope=other_scope,
        )


@pytest.mark.asyncio
async def test_tool_eval_projects_live_control_lease_as_wait_without_gate_or_abandon(
    tmp_path,
    monkeypatch,
):
    """A native pending lease keeps critic/scope evidence and records no failure."""

    import pipeline_state

    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    scope = _scope(timing_plan)
    claim_now = time.time()
    _begin(scope, timing_plan, claim_now=claim_now)
    pending = _begin(scope, timing_plan, claim_now=claim_now + 0.001)

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    candidate_entry = candidate / "national_bot.py"
    candidate_entry.write_text("# strict candidate\n", encoding="utf-8")
    control = tmp_path / "first_strict_control_v1"
    control.mkdir()
    opponent = {
        "name": "first_strict_control_v1",
        "path": str(control),
        "authority": "system_first_strict_control",
        "control_receipt": {"receipt_digest": scope["control_receipt_digest"]},
    }
    sample_plan, batch_plan = _first_strict_batch_plan(timing_plan)
    plan = {
        "settings": {
            "hands_per_match": 70,
            "matches_per_opponent": 8,
            "native_match_timing_plan": timing_plan.snapshot(),
            "native_match_timing_plan_digest": timing_plan.digest(),
            "native_precommit_batch_plan": batch_plan,
            "native_precommit_batch_plan_digest": batch_plan["batch_plan_digest"],
        },
        "sample_plan": sample_plan,
    }
    checkpoint = {
        "stage": "critic_checked",
        "workflow_run_id": scope["workflow_run_id"],
        "checkpoint_revision": scope["checkpoint_revision"],
        "audit_context": {
            tool_eval._FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY: scope,
        },
        "gate_results": {
            "quality": {"passed": True},
            "review": {"passed": True},
            "critic": {"passed": True},
        },
    }
    native_calls = []
    batch_progress = national_native._first_strict_batch_progress(
        batch_plan=batch_plan,
        control_execution_scope=scope,
        timing_plan=timing_plan,
        completed_receipts=[],
        state="waiting_live_lease",
        next_repeat=1,
    )

    async def native_precommit(*_args, **_kwargs):
        native_calls.append(True)
        return {
            "control_execution_pending": pending,
            "first_strict_batch_pending": batch_progress,
        }

    async def should_not_abandon(*_args, **_kwargs):
        pytest.fail("a live first-strict lease must not abandon the generation")

    monkeypatch.setattr(national_native, "run_native_precommit", native_precommit)
    monkeypatch.setattr(
        tool_eval,
        "_validate_first_strict_control_execution_scope",
        lambda supplied, **_kwargs: (scope, None),
    )
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_args: checkpoint)
    def persist_batch(*_args, **kwargs):
        checkpoint.setdefault("audit_context", {}).update(
            kwargs.get("audit_context") or {}
        )
        checkpoint["checkpoint_revision"] += 1
        return True
    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", persist_batch)
    monkeypatch.setattr(
        pipeline_state,
        "make_native_match_heartbeat_reporter",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(tool_eval, "candidate_observability_identity", None)
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)
    monkeypatch.setattr(tool_eval, "_record_gate", lambda *_args, **_kwargs: pytest.fail(
        "a live lease must not persist a precommit gate failure"
    ))
    monkeypatch.setattr(
        tool_eval,
        "_abandon_first_strict_generation",
        should_not_abandon,
    )

    result = await tool_eval._run_national_precommit_backend(
        v=143,
        source_v=0,
        requested_n_games=1,
        effective_n_games=1,
        candidate_name="national_v143",
        parent_name="",
        candidate_entry=candidate_entry,
        code_fingerprint=scope["candidate_artifact_hash"],
        workflow_profile=SimpleNamespace(
            profile_id="national_native",
            evaluation_protocol="national",
            national_execution_mode="native_tcp",
        ),
        candidate_id="national_v143",
        opponents=[opponent],
        all_opponents=[opponent],
        precommit_attempt=scope["precommit_attempt"],
        initial_blockers=[],
        started_at=0.0,
        precommit_plan=plan,
        evaluation_contract={},
        workflow_run_id=scope["workflow_run_id"],
        checkpoint_revision=scope["checkpoint_revision"],
        control_execution_scope=scope,
    )
    payload = json.loads(result["content"][0]["text"])

    assert native_calls == [True]
    assert payload["passed"] is False
    assert payload["pending"] is True
    assert payload["failure_class"] == "infrastructure_pending"
    assert payload["checkpoint_recorded"] is False
    assert payload["batch_checkpoint_recorded"] is True
    assert payload["checkpoint_stage"] == "critic_checked"
    assert payload["control_execution_scope"] == scope
    assert payload["control_receipt_digest"] == scope["control_receipt_digest"]
    assert payload["preserved_gate_evidence"] == ["quality", "review", "critic"]
    assert payload["control_execution_pending"] == pending
    assert payload["first_strict_batch_pending"] == batch_progress
    assert checkpoint["audit_context"][
        tool_eval._FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY
    ] == batch_progress
    assert payload["retry_not_before_epoch_s"] == pending["lease_until"]
    assert payload["intent"] == {
        "kind": "wait",
        "next_tool": "run_precommit_eval",
        "failure_class": "infrastructure_pending",
        "authority": "tool:precommit_eval",
        "safe_to_auto_execute": False,
        "reason": "first_strict_execution_lease_active",
    }


def test_completion_deadline_bounds_held_command_lock_without_false_receipt(
    tmp_path,
    monkeypatch,
):
    """The durable cutoff polls flock and leaves the live lease retryable."""

    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    ticket = _begin(
        _scope(timing_plan),
        timing_plan,
        claim_now=time.time(),
    )
    execution, consumed = _minimal_completion_authority(monkeypatch)
    store = journal._store()

    started = time.monotonic()
    with store.command_lock(ticket["authority_run_id"], blocking=False):
        with pytest.raises(
            journal.FirstStrictExecutionJournalError,
            match=(
                "first_strict_execution_completion_deadline_exceeded:"
                "durable_commit"
            ),
        ):
            journal.complete_control_execution(
                ticket,
                execution=execution,
                deadline_monotonic=time.monotonic() + 0.06,
            )
    assert time.monotonic() - started < 0.75
    assert consumed == []
    _assert_completion_not_recorded(store, ticket)

    # Releasing only the contended resource is sufficient to recover the same
    # fenced effect/result; no state deletion or second physical match occurs.
    receipt = journal.complete_control_execution(
        ticket,
        execution=execution,
        deadline_monotonic=time.monotonic() + 2.0,
    )
    assert consumed == [True]
    evidence, issues = journal.read_control_execution_receipt(
        receipt,
        expected_scope=ticket["input_payload"]["scope"],
    )
    assert issues == []
    assert evidence is not None
    assert evidence["execution"] == execution


def test_completion_deadline_bounds_sqlite_busy_and_rolls_back_for_recovery(
    tmp_path,
    monkeypatch,
):
    """SQLite's busy timeout is the remaining budget, not a fresh 30 seconds."""

    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    ticket = _begin(
        _scope(timing_plan),
        timing_plan,
        claim_now=time.time(),
    )
    execution, consumed = _minimal_completion_authority(monkeypatch)
    store = journal._store()
    blocker = sqlite3.connect(store.path, timeout=0.1, isolation_level=None)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(
            journal.FirstStrictExecutionJournalError,
            match="first_strict_execution_completion_deadline_exceeded:",
        ):
            # Exercise the same context-local propagation used by the real
            # native runner, including historical two-argument wrappers.
            with journal.control_execution_completion_deadline(
                time.monotonic() + 0.08
            ):
                journal.complete_control_execution(ticket, execution=execution)
    finally:
        blocker.rollback()
        blocker.close()
    assert time.monotonic() - started < 0.75
    assert consumed == []
    _assert_completion_not_recorded(store, ticket)

    receipt = journal.complete_control_execution(
        ticket,
        execution=execution,
        deadline_monotonic=time.monotonic() + 2.0,
    )
    assert consumed == [True]
    recovered = journal.begin_control_execution(
        scope=ticket["input_payload"]["scope"],
        repeat=ticket["input_payload"]["repeat"],
        deck_seed_base=ticket["input_payload"]["deck_seed_base"],
        bot_seed_base=ticket["input_payload"]["bot_seed_base"],
        timing_plan=timing_plan,
        claim_now=time.time(),
    )
    assert recovered["state"] == "recovered"
    assert recovered["execution_receipt"] == receipt
    assert recovered["execution"] == execution


def test_successful_commit_crossing_clock_boundary_is_acknowledged(
    tmp_path,
    monkeypatch,
):
    """A returned COMMIT is authority even if its return crosses the clock."""

    monkeypatch.setattr(journal, "CONTROL_EXECUTION_ROOT", tmp_path / "journal")
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    ticket = _begin(
        _scope(timing_plan),
        timing_plan,
        claim_now=time.time(),
    )
    execution, consumed = _minimal_completion_authority(monkeypatch)
    real_commit = WorkflowStore._commit

    def commit_then_return_late(self, connection, **kwargs):
        real_commit(self, connection, **kwargs)
        if kwargs.get("operation") == "complete_effect_commit":
            # Model an uninterruptible fsync/COMMIT that began within budget
            # but returned after the monotonic boundary.  The durable result
            # must be acknowledged, not reclassified as a failed write.
            time.sleep(0.08)

    monkeypatch.setattr(WorkflowStore, "_commit", commit_then_return_late)
    receipt = journal.complete_control_execution(
        ticket,
        execution=execution,
        deadline_monotonic=time.monotonic() + 0.05,
    )
    assert consumed == [True]
    assert journal._store().effect(ticket["effect_id"])["status"] == "completed"
    assert receipt["effect_id"] == ticket["effect_id"]


def test_runner_awaits_bounded_writer_without_starving_event_loop(
    monkeypatch,
):
    """The runner returns only after its writer exits; no detached write remains."""

    execution = {"test_terminal_execution": True}
    writer_started = threading.Event()
    writer_finished = threading.Event()

    def bounded_writer(_ticket, *, execution):
        assert execution is not None
        writer_started.set()
        try:
            time.sleep(0.08)
        finally:
            writer_finished.set()
        return {"receipt": True}

    monkeypatch.setattr(journal, "complete_control_execution", bounded_writer)
    async def scenario():
        ticks = 0
        stop_ticker = False

        async def ticker():
            nonlocal ticks
            while not stop_ticker:
                ticks += 1
                await asyncio.sleep(0.005)

        ticker_task = asyncio.create_task(ticker())
        try:
            result = await national_native._await_first_strict_control_completion(
                {"ticket": True},
                execution,
                deadline_monotonic=time.monotonic() + 1.0,
            )
        finally:
            stop_ticker = True
            await ticker_task
        return result, ticks

    result, ticks = asyncio.run(scenario())

    assert result == {"receipt": True}
    assert writer_started.is_set()
    assert writer_finished.is_set()
    assert ticks >= 5


def test_runner_self_wakes_after_delayed_writer_without_external_ticker(
    monkeypatch,
):
    writer_finished = threading.Event()

    def delayed_writer(_ticket, *, execution):
        assert execution == {"terminal": True}
        try:
            time.sleep(0.08)
            return {"receipt": True}
        finally:
            writer_finished.set()

    monkeypatch.setattr(journal, "complete_control_execution", delayed_writer)

    async def scenario():
        return await national_native._await_first_strict_control_completion(
            {"ticket": True},
            {"terminal": True},
            deadline_monotonic=time.monotonic() + 1.0,
        )

    assert asyncio.run(scenario()) == {"receipt": True}
    assert writer_finished.is_set()


def test_runner_propagates_writer_failure_only_after_writer_exits(monkeypatch):
    writer_finished = threading.Event()

    def failing_writer(_ticket, *, execution):
        assert execution == {"terminal": True}
        try:
            raise RuntimeError("journal-write-failed")
        finally:
            writer_finished.set()

    monkeypatch.setattr(journal, "complete_control_execution", failing_writer)

    async def scenario():
        await national_native._await_first_strict_control_completion(
            {"ticket": True},
            {"terminal": True},
            deadline_monotonic=time.monotonic() + 1.0,
        )

    with pytest.raises(RuntimeError, match="journal-write-failed"):
        asyncio.run(scenario())
    assert writer_finished.is_set()


def test_runner_cancellation_waits_for_bounded_writer_without_detaching(
    monkeypatch,
):
    writer_started = threading.Event()
    writer_finished = threading.Event()

    def bounded_writer(_ticket, *, execution):
        assert execution == {"terminal": True}
        writer_started.set()
        try:
            time.sleep(0.08)
            return {"receipt": True}
        finally:
            writer_finished.set()

    monkeypatch.setattr(journal, "complete_control_execution", bounded_writer)

    async def scenario():
        task = asyncio.create_task(
            national_native._await_first_strict_control_completion(
                {"ticket": True},
                {"terminal": True},
                deadline_monotonic=time.monotonic() + 1.0,
            )
        )
        while not writer_started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        with pytest.raises(asyncio.CancelledError):
            await task
        assert writer_finished.is_set()

    asyncio.run(scenario())
