import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from national_arena.manager import ArenaConflict, NationalArenaManager, _ArenaRuntime
from national_arena.models import ArenaSession
from national_arena.storage import ArenaStore
from national_bot_launcher import build_native_bot_launch
from runtime_capacity import (
    CAPACITY_FIRST_SLOT_ENV,
    CAPACITY_TOTAL_SLOTS_ENV,
    DEFAULT_CAPACITY_ROOT,
    MAX_MATCH_SLOTS,
    acquire_match_slots_async,
    runtime_capacity_root,
    try_acquire_match_slots,
)


def test_native_launcher_uses_allowlisted_environment_for_arena(tmp_path, monkeypatch):
    bot = tmp_path / "national_v1"
    bot.mkdir()
    entry = bot / "national_bot.py"
    entry.write_text(
        "import argparse\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--log')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-bot")
    monkeypatch.setenv("PATH", "/usr/bin")

    plan = build_native_bot_launch(
        bot_dir=bot,
        entry=entry,
        label="national_v1",
        host="127.0.0.1",
        port=10001,
        action_delay=0.3,
        hard_deadline=55,
        refinement_budget=54,
        baseline_target=0.25,
        decision_log=tmp_path / "decision.log",
        extra_environment={"POK_ARENA_SESSION_ID": "arena_test"},
    )

    assert "ANTHROPIC_API_KEY" not in plan.environment
    assert plan.environment["POK_ARENA_SESSION_ID"] == "arena_test"
    assert plan.environment["POK_OFFICIAL_ACTION_DELAY"] == "0.3"
    assert "--log" in plan.command


def test_native_launcher_respects_explicit_empty_environment(tmp_path, monkeypatch):
    bot = tmp_path / "national_v1"
    bot.mkdir()
    entry = bot / "national_bot.py"
    entry.write_text("print('unused')\n", encoding="utf-8")
    monkeypatch.setenv("PARENT_ONLY_SECRET", "must-not-be-inherited")

    plan = build_native_bot_launch(
        bot_dir=bot,
        entry=entry,
        label="national_v1",
        host="127.0.0.1",
        port=10001,
        action_delay=0.0,
        hard_deadline=2.0,
        refinement_budget=1.8,
        baseline_target=0.2,
        base_environment={},
        inherit_all_environment=True,
    )

    assert "PARENT_ONLY_SECRET" not in plan.environment


def test_runtime_capacity_lease_reserves_slots_across_callers(tmp_path):
    root = tmp_path / "capacity"
    first = try_acquire_match_slots("arena", 2, root=root, total_slots=2)
    assert first is not None and first.slots == 2
    assert try_acquire_match_slots("daemon", 1, root=root, total_slots=2) is None
    first.release()
    second = try_acquire_match_slots("daemon", 1, root=root, total_slots=2)
    assert second is not None
    second.release()


def test_runtime_capacity_default_layout_remains_slots_zero_through_eleven(
    tmp_path,
):
    root = tmp_path / "capacity"
    lease = try_acquire_match_slots("default", 12, root=root)
    assert lease is not None and lease.slots == 12
    assert sorted(path.name for path in root.iterdir()) == [
        f"match-slot-{index:02d}.lock" for index in range(12)
    ]
    lease.release()


def test_runtime_capacity_offset_layout_reserves_operator_slots(tmp_path):
    root = tmp_path / "capacity"
    collector = try_acquire_match_slots(
        "collector",
        24,
        root=root,
        first_slot=4,
        total_slots=MAX_MATCH_SLOTS,
    )
    assert collector is not None and collector.slots == 24
    assert sorted(path.name for path in root.iterdir()) == [
        f"match-slot-{index:02d}.lock" for index in range(4, 28)
    ]

    operator = try_acquire_match_slots(
        "operator",
        4,
        root=root,
        total_slots=4,
    )
    assert operator is not None and operator.slots == 4
    assert sorted(path.name for path in root.iterdir()) == [
        f"match-slot-{index:02d}.lock" for index in range(28)
    ]

    operator.release()
    collector.release()


def test_runtime_capacity_default_acquire_uses_strict_environment_layout(
    tmp_path, monkeypatch
):
    root = tmp_path / "capacity"
    monkeypatch.setenv(CAPACITY_FIRST_SLOT_ENV, "4")
    monkeypatch.setenv(CAPACITY_TOTAL_SLOTS_ENV, "28")

    collector = try_acquire_match_slots("collector", 24, root=root)

    assert collector is not None and collector.slots == 24
    assert sorted(path.name for path in root.iterdir()) == [
        f"match-slot-{index:02d}.lock" for index in range(4, 28)
    ]
    collector.release()


@pytest.mark.parametrize("value", ["", " 4", "04", "+4", "four"])
def test_runtime_capacity_rejects_noncanonical_layout_environment(
    tmp_path, monkeypatch, value
):
    monkeypatch.setenv(CAPACITY_FIRST_SLOT_ENV, value)
    monkeypatch.setenv(CAPACITY_TOTAL_SLOTS_ENV, "28")

    with pytest.raises(ValueError, match="environment"):
        try_acquire_match_slots("collector", 1, root=tmp_path / "capacity")


@pytest.mark.parametrize(
    ("first_slot", "total_slots", "message"),
    [
        (-1, 12, "first_slot must be non-negative"),
        (12, 12, "first_slot must be lower than total_slots"),
        (13, 12, "first_slot must be lower than total_slots"),
        (0, 0, "total_slots must be between 1 and 28"),
        (0, 29, "total_slots must be between 1 and 28"),
    ],
)
def test_runtime_capacity_rejects_invalid_slot_layout(
    tmp_path, first_slot, total_slots, message
):
    root = tmp_path / "capacity"
    with pytest.raises(ValueError, match=message):
        try_acquire_match_slots(
            "invalid",
            1,
            root=root,
            first_slot=first_slot,
            total_slots=total_slots,
        )
    assert not root.exists()


def test_runtime_capacity_rejects_request_larger_than_offset_range(tmp_path):
    root = tmp_path / "capacity"
    with pytest.raises(ValueError, match=r"exceeds available slot range 4\.\.27"):
        try_acquire_match_slots(
            "collector",
            25,
            root=root,
            first_slot=4,
            total_slots=MAX_MATCH_SLOTS,
        )
    assert not root.exists()


def test_runtime_capacity_default_is_host_shared_not_checkout_local(monkeypatch):
    monkeypatch.delenv("POK_RUNTIME_CAPACITY_ROOT", raising=False)
    resolved = runtime_capacity_root()
    assert resolved == DEFAULT_CAPACITY_ROOT
    assert "web/core/results" not in resolved.as_posix()
    assert resolved.name.startswith("pok-runtime-capacity-")


def test_runtime_capacity_environment_override_is_resolved_per_call(
    tmp_path, monkeypatch
):
    root = tmp_path / "shared-capacity"
    monkeypatch.setenv("POK_RUNTIME_CAPACITY_ROOT", str(root))
    lease = try_acquire_match_slots("operator", 1, total_slots=1)
    assert lease is not None
    assert root.exists()
    assert (root.stat().st_mode & 0o777) == 0o700
    lease.release()


def test_async_capacity_wait_does_not_block_event_loop(tmp_path):
    async def scenario():
        root = tmp_path / "capacity"
        held = try_acquire_match_slots("held", 1, root=root, total_slots=1)
        assert held is not None
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(3):
                await asyncio.sleep(0.01)
                ticks += 1

        waiter = asyncio.create_task(
            acquire_match_slots_async(
                "waiter",
                1,
                root=root,
                total_slots=1,
                timeout=0.04,
                poll_interval=0.01,
            )
        )
        await ticker()
        with pytest.raises(TimeoutError):
            await waiter
        held.release()
        assert ticks == 3

    asyncio.run(scenario())


def test_store_owner_lease_rejects_multiple_arena_managers(tmp_path):
    first = ArenaStore(tmp_path / "arena")
    second = ArenaStore(tmp_path / "arena")
    first.acquire_owner()
    with pytest.raises(RuntimeError, match="already has a process owner"):
        second.acquire_owner()
    first.release_owner()
    second.acquire_owner()
    second.release_owner()


def test_manager_startup_rolls_back_owner_lease_on_recovery_failure(
    tmp_path, monkeypatch
):
    async def scenario():
        root = tmp_path / "arena"
        broken = ArenaStore(root)
        manager = NationalArenaManager(broken)
        monkeypatch.setattr(
            broken,
            "list_sessions",
            lambda: (_ for _ in ()).throw(OSError("journal unavailable")),
        )

        with pytest.raises(OSError, match="journal unavailable"):
            await manager.startup()

        assert manager._started is False
        successor = ArenaStore(root)
        successor.acquire_owner()
        successor.release_owner()

    asyncio.run(scenario())


def test_wire_journal_fsync_runs_outside_event_loop_thread(tmp_path):
    class RecordingStore:
        def __init__(self):
            self.calls = []

        def append_wire_batch(self, session_id, rows):
            self.calls.append((threading.get_ident(), session_id, list(rows)))

    async def scenario():
        store = RecordingStore()
        manager = NationalArenaManager(store)
        session = ArenaSession(
            session_id="arena_20260711_ab12cd34",
            mode="external_tcp",
        )
        runtime = _ArenaRuntime(session_id=session.session_id)
        runtime.wire_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="arena-wire-test",
        )
        event_loop_thread = threading.get_ident()
        task = asyncio.create_task(manager._wire_writer(session, runtime))
        await runtime.wire_queue.put({"sequence": 1, "payload": "call"})
        await runtime.wire_queue.put(None)
        await task
        runtime.wire_executor.shutdown(wait=True)

        assert len(store.calls) == 1
        writer_thread, session_id, rows = store.calls[0]
        assert writer_thread != event_loop_thread
        assert session_id == session.session_id
        assert rows == [{"sequence": 1, "payload": "call"}]

    asyncio.run(scenario())


def test_semantic_event_journal_fsync_runs_outside_event_loop_thread(tmp_path):
    class RecordingStore(ArenaStore):
        def __init__(self):
            super().__init__(tmp_path / "arena")
            self.calls = []

        def append_event_and_session(self, session, event):
            self.calls.append((threading.get_ident(), event["event_id"]))
            super().append_event_and_session(session, event)

    async def scenario():
        store = RecordingStore()
        manager = NationalArenaManager(store)
        await manager.startup()
        event_loop_thread = threading.get_ident()
        created = await manager.create_session(mode="external_tcp", port=0)
        await manager.stop_session(created["session_id"])
        await manager.shutdown()

        assert store.calls
        assert all(thread_id != event_loop_thread for thread_id, _ in store.calls)
        assert [event_id for _, event_id in store.calls] == [1, 2, 3]

    asyncio.run(scenario())


def test_arena_official_port_defers_to_pending_exe_job(tmp_path, monkeypatch):
    import official_certification_job
    import national_arena.manager as manager_module

    lock_path = tmp_path / "official.lock"
    monkeypatch.setattr(
        manager_module,
        "OfficialPlatformConfig",
        lambda: type("Config", (), {"lock_path": lock_path})(),
    )
    monkeypatch.setattr(
        official_certification_job,
        "job_snapshot",
        lambda: {"jobs": [{"job_id": "formal", "pending": True}]},
    )
    manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
    session = ArenaSession(
        session_id="arena_20260711_deadf00d",
        mode="external_tcp",
        port=10001,
    )
    runtime = _ArenaRuntime(session_id=session.session_id)

    with pytest.raises(ArenaConflict, match="certification is queued"):
        manager._claim_official_platform_resource(session, runtime)

    assert runtime.official_platform_lease is None
    lease = manager_module.try_acquire_official_platform(lock_path, owner="after-test")
    assert lease is not None
    lease.release()


def test_concurrent_start_is_claimed_atomically_before_listener(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        created = await manager.create_session(mode="external_tcp")
        release = asyncio.Event()

        async def fake_run(session, runtime):
            runtime.ready.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                return

        monkeypatch.setattr(manager, "_run_session", fake_run)
        results = await asyncio.gather(
            manager.start_session(created["session_id"]),
            manager.start_session(created["session_id"]),
            return_exceptions=True,
        )
        assert sum(isinstance(item, ArenaConflict) for item in results) == 1
        assert manager.get_session(created["session_id"])["status"] == "starting"
        runtime = manager._runtimes.pop(created["session_id"])
        runtime.task.cancel()
        await runtime.task
        manager._sessions[created["session_id"]].status = "stopped"
        await manager.shutdown()

    asyncio.run(scenario())


def test_recovery_kills_only_content_bound_recorded_process(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        session = ArenaSession(
            session_id="arena_20260711_feedbeef",
            mode="managed_bots",
            status="running",
            managed_processes=[{
                "pid": 43210,
                "pgid": 43210,
                "start_ticks": 123,
                "session_marker": "arena_20260711_feedbeef",
            }],
        )
        ticks = iter([123, None, None])
        monkeypatch.setattr(manager, "_proc_start_ticks", lambda _pid: next(ticks, None))
        monkeypatch.setattr(manager, "_proc_has_session_marker", lambda _pid, _marker: True)
        killed = []
        monkeypatch.setattr("national_arena.manager.os.killpg", lambda pgid, sig: killed.append((pgid, sig)))

        outcome = await manager._reap_persisted_processes(session)

        assert outcome[0]["identity_matches"] is True
        assert outcome[0]["action"] == "sigterm"
        assert killed and killed[0][0] == 43210
        assert session.managed_processes == []

    asyncio.run(scenario())


def test_recovery_kills_marked_descendant_group_after_leader_exits(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        session = ArenaSession(
            session_id="arena_20260711_descfeed",
            mode="managed_bots",
            status="finalizing",
            managed_processes=[{
                "pid": 43210,
                "pgid": 43210,
                "start_ticks": 123,
                "session_marker": "arena_20260711_descfeed",
            }],
        )
        monkeypatch.setattr(manager, "_proc_start_ticks", lambda _pid: None)
        members = iter([[43211], []])
        monkeypatch.setattr(
            manager,
            "_marked_process_group_members",
            lambda _pgid, _marker: next(members, []),
        )
        killed = []
        monkeypatch.setattr(
            "national_arena.manager.os.killpg",
            lambda pgid, sig: killed.append((pgid, sig)),
        )

        outcome = await manager._reap_persisted_processes(session)

        assert outcome[0]["identity_matches"] is True
        assert outcome[0]["marked_members"] == [43211]
        assert killed and killed[0][0] == 43210
        assert session.managed_processes == []

    asyncio.run(scenario())


def test_startup_reaps_process_ledger_even_when_session_was_terminal(tmp_path, monkeypatch):
    async def scenario():
        store = ArenaStore(tmp_path / "arena")
        session = ArenaSession(
            session_id="arena_20260711_termfeed",
            mode="managed_bots",
            status="finished",
            finished_at="2026-07-11T00:00:00+00:00",
            managed_processes=[{"pid": 43210, "pgid": 43210}],
        )
        store.create_session(session)
        manager = NationalArenaManager(store)

        async def reap(incoming):
            incoming.managed_processes = []
            return [{"pid": 43210, "action": "sigkill"}]

        monkeypatch.setattr(manager, "_reap_persisted_processes", reap)
        await manager.startup()

        recovered = manager.get_session(session.session_id)
        assert recovered["status"] == "failed"
        assert recovered["failure_reason"] == "terminal_session_had_unfinished_process_cleanup"
        assert recovered["managed_processes"] == []
        await manager.shutdown()

    asyncio.run(scenario())


def test_recovery_never_kills_reused_pid_identity(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        session = ArenaSession(
            session_id="arena_20260711_cafebabe",
            mode="managed_bots",
            status="running",
            managed_processes=[{
                "pid": 43211,
                "pgid": 43211,
                "start_ticks": 123,
            }],
        )
        monkeypatch.setattr(manager, "_proc_start_ticks", lambda _pid: 999)
        monkeypatch.setattr(manager, "_proc_has_session_marker", lambda _pid, _marker: True)
        monkeypatch.setattr(
            "national_arena.manager.os.killpg",
            lambda _pgid, _sig: (_ for _ in ()).throw(AssertionError("must not kill")),
        )

        outcome = await manager._reap_persisted_processes(session)

        assert outcome[0]["identity_matches"] is False
        assert outcome[0]["action"] == "identity_mismatch_not_killed"

    asyncio.run(scenario())
