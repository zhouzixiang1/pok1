import asyncio
from types import SimpleNamespace

import pytest

from bot_namespace import bot_name, bot_tag
from national_arena import manager as arena_manager
from national_arena.manager import (
    ArenaConflict,
    ArenaError,
    ArenaInfrastructureError,
    NationalArenaManager,
)
from national_arena.models import ArenaSession
from national_arena.sandbox import ArenaSandboxUnavailable
from national_arena.storage import ArenaStore


def _session_epoch_fields():
    from epoch_authority import require_policy_epoch_initialized

    return NationalArenaManager.session_epoch_fields(
        require_policy_epoch_initialized("test.national_arena.session_fixture")
    )


def _strict_epoch_authority(*, workflow_run_id="workflow-test-1"):
    return {
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "fresh_bootstrap_ready",
        "initialized": True,
        "strict_published": False,
        "strict_published_bots": [],
        "reset_receipt_valid": True,
        "reset_receipt_digest": "a" * 64,
        "active_generation": {
            "workflow_run_id": workflow_run_id,
        } if workflow_run_id else None,
    }


def test_arena_store_round_trips_session_and_monotonic_events(tmp_path):
    store = ArenaStore(tmp_path / "arena")
    session = ArenaSession(session_id="arena_20260711_abcd1234", mode="external_tcp")
    store.create_session(session)
    store.append_event(session.session_id, {
        "event_id": 1,
        "session_id": session.session_id,
        "type": "session_created",
    })
    store.append_event(session.session_id, {
        "event_id": 2,
        "session_id": session.session_id,
        "type": "server_listening",
    })

    assert store.load_session(session.session_id).result_authority == "diagnostic_only"
    assert [row["event_id"] for row in store.read_events(
        session.session_id, after_event_id=1
    )] == [2]
    assert store.event_high_watermark(session.session_id) == 2

    store.append_wire_batch(session.session_id, [
        {"sequence": 1, "payload": "preflop|SMALLBLIND|<0,1><1,2>"},
        {"sequence": 2, "payload": "call"},
    ])
    assert [row["sequence"] for row in store.read_wire(
        session.session_id, after_sequence=1
    )] == [2]


def test_arena_authority_fields_are_model_invariants():
    session = ArenaSession.from_dict({
        "session_id": "arena_20260711_bad0cafe",
        "mode": "external_tcp",
        "result_authority": "official",
        "affects_glicko": True,
        "official_exe_certification": True,
        "compliance_oracle": "local_arena",
    })

    payload = session.to_dict()
    assert payload["result_authority"] == "diagnostic_only"
    assert payload["affects_glicko"] is False
    assert payload["official_exe_certification"] is False
    assert payload["compliance_oracle"] == "official_windows_exe"

    quarantined = ArenaSession.from_dict({
        "session_id": "arena_20260711_fence123",
        "mode": "managed_bots",
        "status": "quarantined",
        "cleanup_completed": True,
        "resource_fence_held": False,
    }).to_dict()
    assert quarantined["cleanup_completed"] is False
    assert quarantined["resource_fence_held"] is True


def test_manager_startup_guard_precedes_every_store_write(tmp_path, monkeypatch):
    from epoch_authority import PolicyEpochInitializationRequired
    import epoch_authority

    root = tmp_path / "must_not_exist"
    state = {
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "reset_required",
        "initialized": False,
        "reset_receipt_valid": False,
        "reset_receipt_digest": None,
        "operator_action": "execute_policy_epoch_reset",
    }

    def blocked(operation):
        raise PolicyEpochInitializationRequired(operation, state)

    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", blocked)

    async def scenario():
        manager = NationalArenaManager(ArenaStore(root))
        with pytest.raises(PolicyEpochInitializationRequired):
            await manager.startup()
        assert manager.started is False
        assert not root.exists()

    asyncio.run(scenario())


def test_manager_ignores_mismatched_epoch_session_without_touching_it(tmp_path):
    authority = _strict_epoch_authority()
    store = ArenaStore(tmp_path / "arena")
    old = ArenaSession(
        session_id="arena_20260711_oldepoch",
        mode="external_tcp",
        status="running",
    )
    store.create_session(old)
    old_dir = store.session_dir(old.session_id)
    (old_dir / ".lock").unlink()
    old_snapshot = (old_dir / "session.json").read_bytes()

    current = ArenaSession(
        session_id="arena_20260711_newepoch",
        mode="external_tcp",
        **NationalArenaManager.session_epoch_fields(authority),
    )
    store.create_session(current)

    async def scenario():
        manager = NationalArenaManager(store, epoch_authority=authority)
        await manager.startup(epoch_authority=authority)
        assert [row["session_id"] for row in manager.list_sessions()] == [
            current.session_id
        ]
        assert manager.get_session(current.session_id)["workflow_run_id"] == (
            "workflow-test-1"
        )
        with pytest.raises(ArenaError, match="not found"):
            manager.get_session(old.session_id)
        assert not (old_dir / ".lock").exists()
        assert not (old_dir / "events.jsonl").exists()
        assert (old_dir / "session.json").read_bytes() == old_snapshot
        await manager.shutdown()

    asyncio.run(scenario())


def test_arena_certification_snapshot_fails_closed_when_status_unavailable(
    tmp_path, monkeypatch
):
    def fail(_candidate):
        raise RuntimeError("status store unavailable")

    monkeypatch.setattr("official_certification.read_status", fail)
    snapshot = NationalArenaManager._certification_snapshot(tmp_path)

    assert snapshot["arena_launch_eligible"] is False
    assert snapshot["official_exe_passed"] is False
    assert snapshot["eligibility_basis"] == "ineligible"


def test_manager_create_and_stop_never_claims_official_certification(tmp_path):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        created = await manager.create_session(
            mode="external_tcp",
            host="127.0.0.1",
            port=0,
            hands=70,
        )
        assert created["result_authority"] == "diagnostic_only"
        assert created["official_exe_certification"] is False
        assert created["affects_glicko"] is False
        stopped = await manager.stop_session(created["session_id"])
        assert stopped["status"] == "stopped"
        events = await manager.read_events(created["session_id"])
        assert [row["event_id"] for row in events] == list(range(1, len(events) + 1))
        await manager.shutdown()

    asyncio.run(scenario())


def test_runtime_terminal_event_is_persisted_only_after_cleanup(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        created = await manager.create_session(mode="external_tcp", port=0)

        async def fail_listener(_session, _runtime):
            raise ArenaError("listener failed")

        async def observed_cleanup(session, _runtime):
            await manager._emit(session, "cleanup_observed", {})

        monkeypatch.setattr(manager, "_open_listener", fail_listener)
        monkeypatch.setattr(manager, "_cleanup_runtime", observed_cleanup)

        with pytest.raises(ArenaError, match="listener failed"):
            await manager.start_session(created["session_id"])

        events = await manager.read_events(created["session_id"])
        event_types = [row["type"] for row in events]
        assert event_types[-3:] == [
            "session_finalizing",
            "cleanup_observed",
            "session_failed",
        ]
        assert manager.get_session(created["session_id"])["status"] == "failed"
        await manager.shutdown()

    asyncio.run(scenario())


def test_manager_managed_mode_uses_server_side_launchable_whitelist(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        monkeypatch.setattr(manager, "list_launchable_bots", lambda: [])
        with pytest.raises(ArenaError, match="not active/native/official-eligible"):
            await manager.create_session(
                mode="managed_bots",
                top_bot=bot_name(1),
                bottom_bot="national_v2",
            )
        await manager.shutdown()

    asyncio.run(scenario())


def test_managed_session_revalidates_published_bot_identity_before_start(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        original = {
            "label": bot_name(1),
            "artifact_hash": "a" * 64,
            "tag": bot_tag(1),
            "tag_object": "tag-a",
            "commit_oid": "commit-a",
            "current_tree_oid": "tree-a",
        }
        changed = {**original, "artifact_hash": "b" * 64}
        certification = {
            "official_full_certified": True,
            "arena_launch_eligible": True,
        }

        def catalog(*, force_refresh=False):
            identity = changed if force_refresh else original
            return [
                {
                    "id": bot_name(1),
                    "artifact_identity": identity,
                    "certification": certification,
                }
            ]

        monkeypatch.setattr(manager, "list_launchable_bots", catalog)
        created = await manager.create_session(
            mode="managed_bots",
            top_bot=bot_name(1),
            bottom_bot=bot_name(1),
        )

        with pytest.raises(ArenaConflict, match="publication identity changed"):
            await manager.start_session(created["session_id"])

        assert manager.get_session(created["session_id"])["status"] == "created"
        await manager.stop_session(created["session_id"])
        await manager.shutdown()

    asyncio.run(scenario())


def test_manager_rejects_second_active_session_before_binding_port(tmp_path):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        first = await manager.create_session(mode="external_tcp")
        second = await manager.create_session(mode="external_tcp")
        manager._sessions[first["session_id"]].status = "waiting_for_players"
        with pytest.raises(ArenaConflict, match="another arena session is active"):
            await manager.start_session(second["session_id"])
        manager._sessions[first["session_id"]].status = "stopped"
        await manager.stop_session(second["session_id"])
        await manager.shutdown()

    asyncio.run(scenario())


def test_manager_marks_interrupted_session_failed_on_startup(tmp_path):
    store = ArenaStore(tmp_path / "arena")
    session = ArenaSession(
        session_id="arena_20260711_deadbeef",
        mode="external_tcp",
        status="running",
        **_session_epoch_fields(),
    )
    store.create_session(session)

    async def scenario():
        manager = NationalArenaManager(store)
        await manager.startup()
        recovered = manager.get_session(session.session_id)
        assert recovered["status"] == "failed"
        assert recovered["failure_reason"] == "web_process_restarted"
        events = await manager.read_events(session.session_id)
        assert events[-1]["type"] == "session_failed"
        await manager.shutdown()

    asyncio.run(scenario())


def test_manager_recovers_event_id_high_watermark_after_snapshot_lag(tmp_path):
    store = ArenaStore(tmp_path / "arena")
    session = ArenaSession(
        session_id="arena_20260711_fadedcab",
        mode="external_tcp",
        **_session_epoch_fields(),
    )
    store.create_session(session)
    store.append_event(session.session_id, {
        "event_id": 7,
        "session_id": session.session_id,
        "type": "crash_window_event",
    })

    async def scenario():
        manager = NationalArenaManager(store)
        await manager.startup()
        stopped = await manager.stop_session(session.session_id)
        assert stopped["last_event_id"] == 9
        events = await manager.read_events(session.session_id)
        assert [row["event_id"] for row in events] == [7, 8, 9]
        await manager.shutdown()

    asyncio.run(scenario())


def _managed_catalog(*, force_refresh=False):
    del force_refresh
    identity = {
        "label": bot_name(1),
        "artifact_hash": "a" * 64,
        "tag": bot_tag(1),
        "tag_object": "tag-a",
        "commit_oid": "commit-a",
        "current_tree_oid": "tree-a",
    }
    return [{
        "id": bot_name(1),
        "artifact_identity": identity,
        "certification": {
            "official_full_certified": True,
            "arena_launch_eligible": True,
        },
    }]


def test_managed_defaults_to_ephemeral_loopback_and_rejects_public_bind(
    tmp_path, monkeypatch
):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        monkeypatch.setattr(manager, "list_launchable_bots", _managed_catalog)

        created = await manager.create_session(
            mode="managed_bots",
            top_bot=bot_name(1),
            bottom_bot=bot_name(1),
        )
        assert created["host"] == "127.0.0.1"
        assert created["port"] == 0
        assert created["requested_port"] == 0

        with pytest.raises(ArenaError, match="must be loopback"):
            await manager.create_session(
                mode="managed_bots",
                host="0.0.0.0",
                top_bot=bot_name(1),
                bottom_bot=bot_name(1),
            )
        await manager.stop_session(created["session_id"])
        await manager.shutdown()

    asyncio.run(scenario())


def test_managed_start_without_bwrap_is_infrastructure_failure(
    tmp_path, monkeypatch
):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        monkeypatch.setattr(manager, "list_launchable_bots", _managed_catalog)
        created = await manager.create_session(
            mode="managed_bots",
            top_bot=bot_name(1),
            bottom_bot=bot_name(1),
        )

        def unavailable():
            raise ArenaSandboxUnavailable(
                "arena_sandbox_bwrap_unavailable; managed execution has no fallback"
            )

        monkeypatch.setattr(arena_manager, "require_managed_sandbox", unavailable)
        with pytest.raises(ArenaInfrastructureError, match="no fallback"):
            await manager.start_session(created["session_id"])
        assert manager.get_session(created["session_id"])["status"] == "created"
        assert created["session_id"] not in manager._runtimes
        await manager.stop_session(created["session_id"])
        await manager.shutdown()

    asyncio.run(scenario())


def test_managed_seat_endpoints_ignore_connection_order_and_reported_name(
    tmp_path, monkeypatch
):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        monkeypatch.setattr(manager, "list_launchable_bots", _managed_catalog)
        created = await manager.create_session(
            mode="managed_bots",
            top_bot=bot_name(1),
            bottom_bot=bot_name(1),
        )
        session = manager._sessions[created["session_id"]]
        runtime = arena_manager._ArenaRuntime(session_id=session.session_id)
        runtime.launch_labels = ["AUTH_TOP", "AUTH_BOTTOM"]

        class FakeSocket:
            def __init__(self, port):
                self.port = port

            def getsockname(self):
                return ("127.0.0.1", self.port)

        class FakeServer:
            def __init__(self, handler, port):
                self.handler = handler
                self.sockets = [FakeSocket(port)]

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class FakeReader:
            def __init__(self, payload):
                self.payload = payload

            async def read(self, _limit):
                payload, self.payload = self.payload, b""
                return payload

        class FakeWriter:
            def __init__(self, peer_port):
                self.peer_port = peer_port
                self.output = bytearray()

            def get_extra_info(self, name):
                return ("127.0.0.1", self.peer_port) if name == "peername" else None

            def write(self, payload):
                self.output.extend(payload)

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        fake_servers = []

        async def fake_start_server(handler, _host, requested_port):
            port = int(requested_port) or 41000 + len(fake_servers)
            server = FakeServer(handler, port)
            fake_servers.append(server)
            return server

        monkeypatch.setattr(arena_manager.asyncio, "start_server", fake_start_server)
        await manager._open_listener(session, runtime)
        bottom_writer = FakeWriter(51001)
        top_writer = FakeWriter(51002)
        await fake_servers[1].handler(FakeReader(b"CLAIM_TOP"), bottom_writer)
        await fake_servers[0].handler(FakeReader(b"CLAIM_BOTTOM"), top_writer)
        await asyncio.wait_for(runtime.connected.wait(), timeout=2.0)
        assert list(runtime.clients_by_seat) == ["bottom", "top"]
        assert runtime.clients == [
            runtime.clients_by_seat["top"],
            runtime.clients_by_seat["bottom"],
        ]
        assert session.managed_endpoints["top"]["port"] != (
            session.managed_endpoints["bottom"]["port"]
        )

        ordered, names = await manager._handshake(session, runtime)
        assert ordered == [
            runtime.clients_by_seat["top"],
            runtime.clients_by_seat["bottom"],
        ]
        assert names == ["AUTH_TOP", "AUTH_BOTTOM"]
        assert top_writer.output == b"name"
        assert bottom_writer.output == b"name"

        await manager._close_servers(runtime)
        for client in runtime.clients:
            await client.close()
        await manager.stop_session(session.session_id)
        await manager.shutdown()

    asyncio.run(scenario())


def test_managed_launch_records_central_executor_profile(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        identity = {"artifact_hash": "a" * 64}
        session = ArenaSession(
            session_id="arena_20260711_feedbabe",
            mode="managed_bots",
            top_bot=bot_name(1),
            bottom_bot=bot_name(1),
            managed_bot_identities={"top": identity, "bottom": identity},
            managed_endpoints={
                "top": {"host": "127.0.0.1", "port": 41001},
                "bottom": {"host": "127.0.0.1", "port": 41002},
            },
        )
        manager.store.create_session(session)
        runtime = arena_manager._ArenaRuntime(session_id=session.session_id)
        runtime.sandbox_capability = object()
        emitted = []

        async def capture_event(_session, event_type, payload):
            emitted.append((event_type, payload))

        class FakeEndpoint:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                return False

        class FakeEndpointLease:
            @staticmethod
            def connect(*_args, **_kwargs):
                return FakeEndpoint()

        launched = []

        def fake_launch(*_args, **_kwargs):
            process = SimpleNamespace(pid=42000 + len(launched))
            managed = SimpleNamespace(
                process=process,
                isolation=SimpleNamespace(policy_sha256="b" * 64),
            )
            launched.append(managed)
            return managed

        monkeypatch.setattr(manager, "_emit", capture_event)
        monkeypatch.setattr(
            manager,
            "_certification_snapshot",
            lambda _bot: {"artifact_identity": identity},
        )
        monkeypatch.setattr(manager, "_proc_start_ticks", lambda _pid: 123)
        monkeypatch.setattr(
            arena_manager,
            "resolve_bot",
            lambda label: (label, tmp_path / label),
        )
        monkeypatch.setattr(arena_manager, "check_native_contract", lambda _bot: [])
        monkeypatch.setattr(
            arena_manager,
            "current_system_native_runtime_errors",
            lambda _bot: [],
        )
        monkeypatch.setattr(
            arena_manager,
            "seal_bot_artifact",
            lambda _source, destination, expected_hash: SimpleNamespace(
                root=destination,
                artifact_hash=expected_hash,
                manifest_digest="c" * 64,
            ),
        )
        monkeypatch.setattr(arena_manager, "EndpointLease", FakeEndpointLease)
        monkeypatch.setattr(arena_manager, "launch_sandboxed_bot", fake_launch)
        monkeypatch.setattr(arena_manager.os, "getpgid", lambda pid: pid)

        await manager._launch_managed_bots(session, runtime)

        expected_profile = "central-managed-executor:" + "b" * 16
        assert session.sandbox_profile == expected_profile
        assert len(session.managed_processes) == 2
        assert all(
            record["sandbox_profile"] == expected_profile
            for record in session.managed_processes
        )
        process_events = [
            payload
            for event_type, payload in emitted
            if event_type == "bot_process_started"
        ]
        assert len(process_events) == 2
        assert all(
            payload["sandbox_profile"] == expected_profile
            for payload in process_events
        )
        for managed in runtime.processes:
            managed.stdout_handle.close()
            managed.stderr_handle.close()

    asyncio.run(scenario())


def test_managed_capacity_is_acquired_before_official_port_lease(
    tmp_path, monkeypatch
):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        monkeypatch.setattr(manager, "list_launchable_bots", _managed_catalog)
        created = await manager.create_session(
            mode="managed_bots",
            port=10001,
            top_bot=bot_name(1),
            bottom_bot=bot_name(1),
        )
        session = manager._sessions[created["session_id"]]
        runtime = arena_manager._ArenaRuntime(session_id=session.session_id)
        runtime.cleanup_future = asyncio.get_running_loop().create_future()
        manager._runtimes[session.session_id] = runtime
        order = []

        class Lease:
            slots = 2

            def release(self):
                order.append("capacity_release")

        async def acquire(_session):
            order.append("capacity")
            return Lease()

        def claim(_session, _runtime):
            order.append("official_port")
            raise ArenaError("ordering probe complete")

        async def cleanup(_session, cleanup_runtime):
            cleanup_runtime.capacity_lease.release()
            cleanup_runtime.capacity_lease = None
            return arena_manager._CleanupOutcome(clean=True)

        monkeypatch.setattr(manager, "_acquire_capacity", acquire)
        monkeypatch.setattr(manager, "_claim_official_platform_resource", claim)
        monkeypatch.setattr(manager, "_cleanup_runtime", cleanup)
        await manager._run_session(session, runtime)

        assert order[:2] == ["capacity", "official_port"]
        assert session.status == "failed"
        await manager.shutdown()

    asyncio.run(scenario())


def test_capacity_wait_has_a_hard_timeout(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        session = ArenaSession(
            session_id="arena_20260711_1234abcd",
            mode="managed_bots",
            capacity_wait_seconds=0.05,
        )
        monkeypatch.setattr(arena_manager, "try_acquire_match_slots", lambda *a, **k: None)
        with pytest.raises(ArenaInfrastructureError, match="runtime_capacity_timeout"):
            await asyncio.wait_for(manager._acquire_capacity(session), timeout=1.0)

    asyncio.run(scenario())


def test_cleanup_pending_preserves_leases_as_resource_fence(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        created = await manager.create_session(mode="external_tcp", port=0)
        session = manager._sessions[created["session_id"]]
        runtime = arena_manager._ArenaRuntime(session_id=session.session_id)

        class Lease:
            def __init__(self):
                self.released = False

            def release(self):
                self.released = True

        capacity = Lease()
        official = Lease()
        runtime.capacity_lease = capacity
        runtime.official_platform_lease = official
        runtime.processes = [SimpleNamespace(label="stuck")]
        child = asyncio.create_task(
            asyncio.Event().wait(),
            name="arena-test-child",
        )
        runtime.child_tasks.add(child)

        async def stuck(_session, _managed):
            return "process_group:stuck:4321"

        monkeypatch.setattr(manager, "_terminate_managed_process", stuck)
        outcome = await manager._cleanup_runtime(session, runtime)

        assert outcome.clean is False
        assert outcome.pending == ("process_group:stuck:4321",)
        assert capacity.released is False
        assert official.released is False
        assert runtime.capacity_lease is capacity
        assert runtime.official_platform_lease is official
        assert child.done() and child.cancelled()
        runtime.processes.clear()
        runtime.capacity_lease = None
        runtime.official_platform_lease = None
        await manager.stop_session(session.session_id)
        await manager.shutdown()

    asyncio.run(scenario())


def test_cleanup_failure_quarantines_and_blocks_next_match(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        first = await manager.create_session(mode="external_tcp", port=0)

        async def fail_listener(_session, _runtime):
            raise ArenaError("listener failed")

        original_cleanup = manager._cleanup_runtime

        async def incomplete_cleanup(cleanup_session, cleanup_runtime):
            clean_outcome = await original_cleanup(cleanup_session, cleanup_runtime)
            assert clean_outcome.clean is True
            return arena_manager._CleanupOutcome(
                clean=False,
                pending=("process_group:stuck:4321",),
            )

        monkeypatch.setattr(manager, "_open_listener", fail_listener)
        monkeypatch.setattr(manager, "_cleanup_runtime", incomplete_cleanup)
        with pytest.raises(ArenaError, match="listener failed"):
            await manager.start_session(first["session_id"])
        quarantined = manager.get_session(first["session_id"])
        assert quarantined["status"] == "quarantined"
        assert quarantined["resource_fence_held"] is True
        assert first["session_id"] in manager._runtimes

        second = await manager.create_session(mode="external_tcp", port=0)
        with pytest.raises(ArenaConflict, match="another arena session is active"):
            await manager.start_session(second["session_id"])
        assert (await manager.stop_session(first["session_id"]))["status"] == "quarantined"
        manager._runtimes.pop(first["session_id"])
        manager._sessions[first["session_id"]].status = "failed"
        await manager.stop_session(second["session_id"])
        await manager.shutdown()

    asyncio.run(scenario())


def test_concurrent_stop_calls_share_one_cleanup_future(tmp_path, monkeypatch):
    async def scenario():
        manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
        await manager.startup()
        created = await manager.create_session(mode="external_tcp", port=0)

        class FakeServer:
            def close(self):
                return None

            async def wait_closed(self):
                return None

        async def fake_open(session, current_runtime):
            server = FakeServer()
            current_runtime.server = server
            current_runtime.servers["external"] = server
            session.port = 42000
            session.status = "waiting_for_players"
            await manager._emit(session, "server_listening", {
                "host": session.host,
                "port": session.port,
                "mode": session.mode,
            })

        monkeypatch.setattr(manager, "_open_listener", fake_open)
        await manager.start_session(created["session_id"])
        runtime = manager._runtimes[created["session_id"]]
        cleanup_future = runtime.cleanup_future
        cleanup_calls = 0
        original_cleanup = manager._cleanup_runtime

        async def counted_cleanup(session, current_runtime):
            nonlocal cleanup_calls
            cleanup_calls += 1
            await asyncio.sleep(0.05)
            return await original_cleanup(session, current_runtime)

        monkeypatch.setattr(manager, "_cleanup_runtime", counted_cleanup)
        first, second = await asyncio.gather(
            manager.stop_session(created["session_id"]),
            manager.stop_session(created["session_id"]),
        )

        assert first["status"] == second["status"] == "stopped"
        assert cleanup_calls == 1
        assert cleanup_future is not None and cleanup_future.done()
        events = await manager.read_events(created["session_id"])
        assert sum(row["type"] == "session_stopping" for row in events) == 1
        assert sum(row["type"] == "session_stopped" for row in events) == 1
        await manager.shutdown()

    asyncio.run(scenario())
