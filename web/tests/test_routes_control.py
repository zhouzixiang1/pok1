"""Tests for /api/control/* endpoints."""

import asyncio
import json
import os
import sys
import threading
import time

import pytest


class TestEvolutionTaskOwnership:
    @pytest.mark.parametrize("outcome", ["return", "raise", "cancel"])
    def test_owned_task_always_clears_running(self, outcome):
        from server.state import app_state, run_evolution_task

        async def scenario():
            app_state.set_running(True)

            async def owned():
                if outcome == "return":
                    return "done"
                if outcome == "raise":
                    raise RuntimeError("owned failure")
                await asyncio.Future()

            task = asyncio.create_task(run_evolution_task(owned()))
            await asyncio.sleep(0)
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "raise":
                with pytest.raises(RuntimeError, match="owned failure"):
                    await task
            else:
                assert await task == "done"
            assert app_state.to_dict()["running"] is False

        asyncio.run(scenario())

    def test_stop_preserves_live_owner_and_late_old_cleanup_cannot_clear_new_owner(
        self,
        tmp_path,
    ):
        from server.state import AppState

        class FakeTask:
            def __init__(self, *, done=False):
                self._done = done
                self._cancelled = False

            def done(self):
                return self._done

            def cancelled(self):
                return self._cancelled

            def cancel(self):
                self._cancelled = True

        state = AppState(config_file=tmp_path / "app_config.json")
        old_owner = state.begin_runtime_owner()
        old_task = FakeTask()
        state.set_task(old_task, owner_id=old_owner)

        assert state.stop_running() is old_task
        assert state.task_snapshot()["present"] is True
        assert state.begin_runtime_owner() is None

        old_task._done = True
        state.stop_running()
        new_owner = state.begin_runtime_owner()
        new_task = FakeTask()
        state.set_task(new_task, owner_id=new_owner)

        state.clear_task_if(old_task, owner_id=old_owner)

        assert state.to_dict()["running"] is True
        assert state.task_snapshot()["owner_id"] == new_owner
        assert state.task_snapshot()["present"] is True
        new_task._done = True
        state.stop_running()

    def test_task_owner_listener_observes_replacement_without_polling(self, tmp_path):
        """A browser invalidator receives every owner edge synchronously."""

        from server.state import AppState

        class FakeTask:
            def __init__(self):
                self._done = False

            def done(self):
                return self._done

            def cancelled(self):
                return False

        state = AppState(config_file=tmp_path / "app_config.json")
        observed = []
        state.add_task_snapshot_listener(observed.append)

        owner_a = state.begin_runtime_owner()
        task_a = FakeTask()
        state.set_task(task_a, owner_id=owner_a)
        task_a._done = True
        state.stop_running()

        owner_b = state.begin_runtime_owner()
        task_b = FakeTask()
        state.set_task(task_b, owner_id=owner_b)

        assert [snapshot["owner_id"] for snapshot in observed] == [
            owner_a,
            owner_a,
            None,
            owner_b,
            owner_b,
        ]
        assert [snapshot["lifecycle_revision"] for snapshot in observed] == [
            1,
            2,
            3,
            4,
            5,
        ]
        assert observed[-1] == {
            "present": True,
            "done": False,
            "cancelled": False,
            "shutdown_requested": False,
            "status_eligible": True,
            "owner_id": owner_b,
            "lifecycle_revision": 5,
        }

        # A late finally block from owner A cannot emit a rollback after B is
        # live; the browser keeps B's lifecycle projection without polling.
        observed_before_late_cleanup = list(observed)
        state.clear_task_if(task_a, owner_id=owner_a)
        assert observed == observed_before_late_cleanup

        task_b._done = True
        state.stop_running()

    def test_start_does_not_cancel_and_overlap_a_stale_live_owner(self):
        from server.state import app_state

        async def scenario():
            async def wait_forever():
                await asyncio.Future()

            app_state.stop_running()
            owner = asyncio.create_task(wait_forever())
            app_state.set_task(owner)
            try:
                assert app_state.try_set_running(True) is False
                assert owner.cancelled() is False
                assert owner.done() is False
            finally:
                owner.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await owner
                app_state.stop_running()

        asyncio.run(scenario())

    def test_shutdown_manager_requires_exact_reserved_owner(self, tmp_path):
        from server.state import AppState

        state = AppState(config_file=tmp_path / "app_config.json")
        owner = state.begin_runtime_owner()
        manager = type("Manager", (), {"is_shutting_down": False})()

        with pytest.raises(RuntimeError, match="owner fencing conflict"):
            state.set_shutdown_mgr(manager)
        with pytest.raises(RuntimeError, match="owner fencing conflict"):
            state.set_shutdown_mgr(manager, owner_id="foreign-owner")

        state.set_shutdown_mgr(manager, owner_id=owner)
        assert state.task_snapshot()["owner_id"] == owner
        state.abort_runtime_owner(owner)

    def test_direct_shutdown_manager_request_publishes_one_fenced_lifecycle_edge(
        self,
        tmp_path,
    ):
        """A direct manager stop invalidates current UI status exactly once."""

        from shutdown_manager import ShutdownManager
        from server.state import AppState

        class FakeTask:
            def __init__(self):
                self._done = False

            def done(self):
                return self._done

            def cancelled(self):
                return False

        state = AppState(config_file=tmp_path / "app_config.json")
        observed = []
        state.add_task_snapshot_listener(observed.append)
        owner_a = state.begin_runtime_owner()
        task_a = FakeTask()
        state.set_task(task_a, owner_id=owner_a)
        manager_a = ShutdownManager()
        state.set_shutdown_mgr(manager_a, owner_id=owner_a)

        before = state.task_snapshot()
        assert before["status_eligible"] is True
        assert manager_a.request_shutdown() is True
        after = state.task_snapshot()
        assert after["owner_id"] == owner_a
        assert after["shutdown_requested"] is True
        assert after["status_eligible"] is False
        assert after["lifecycle_revision"] == before["lifecycle_revision"] + 1
        assert observed[-1] == after
        assert manager_a.request_shutdown() is False
        assert state.task_snapshot()["lifecycle_revision"] == after["lifecycle_revision"]

        # The old callback is identity-fenced: it cannot alter a successor.
        task_a._done = True
        state.stop_running()
        owner_b = state.begin_runtime_owner()
        task_b = FakeTask()
        state.set_task(task_b, owner_id=owner_b)
        manager_b = ShutdownManager()
        state.set_shutdown_mgr(manager_b, owner_id=owner_b)
        successor = state.task_snapshot()
        state._on_shutdown_requested(manager_a, owner_a)
        assert state.task_snapshot() == successor
        task_b._done = True
        state.stop_running()

    def test_llm_shutdown_manager_rejects_stale_owner_replace_and_clear(self):
        import llm_query

        class Manager:
            def __init__(self, shutting_down):
                self.is_shutting_down = shutting_down

        owner_a = "runtime-owner-a"
        owner_b = "runtime-owner-b"
        manager_a = Manager(True)
        manager_b = Manager(False)
        assert llm_query.set_shutdown_manager(None) is True
        try:
            assert llm_query.set_shutdown_manager(
                manager_a,
                owner_id=owner_a,
            ) is True
            # The inner Orchestrator may repeat the exact bind without erasing
            # the outer launch transaction's owner identity.
            assert llm_query.set_shutdown_manager(manager_a) is True
            assert llm_query.set_shutdown_manager(
                manager_b,
                owner_id=owner_b,
            ) is False
            assert llm_query.set_shutdown_manager(
                None,
                owner_id=owner_b,
            ) is False
            assert llm_query.set_shutdown_manager(None) is False
            assert llm_query._is_shutdown_requested() is True
        finally:
            assert llm_query.set_shutdown_manager(
                None,
                owner_id=owner_a,
            ) is True
        assert llm_query._is_shutdown_requested() is False


class TestConfig:
    def test_default_daemon_workers_handles_unknown_cpu_count(self, monkeypatch):
        import server.state as state

        monkeypatch.setattr(state.os, "cpu_count", lambda: None)

        assert state._default_daemon_workers() == 1

    def test_daemon_pairs_cap_matches_effective_rating_protocol(self, monkeypatch):
        import daemon_management
        import elo_daemon
        import stability_observation
        from server.state import MAX_DAEMON_PAIRS

        monkeypatch.setenv("POK_RATING_PROTOCOL", "national")
        monkeypatch.delenv("POK_NATIONAL_RATING_MATCHES", raising=False)

        assert MAX_DAEMON_PAIRS == 8
        assert daemon_management.MAX_DAEMON_PAIRS == MAX_DAEMON_PAIRS
        assert stability_observation.MAX_DAEMON_PAIRS == MAX_DAEMON_PAIRS
        assert elo_daemon.MAX_NATIONAL_RATING_MATCHES == MAX_DAEMON_PAIRS
        assert elo_daemon._rating_protocol_config(n_pairs=99)[
            "national_matches"
        ] == MAX_DAEMON_PAIRS

    def test_get(self, client):
        resp = client.get("/api/control/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "daemon_enabled" in data
        assert "daemon_workers" in data
        assert "daemon_pairs" in data

    def test_set_partial(self, client):
        # Get current config first
        orig = client.get("/api/control/config").json()
        resp = client.put("/api/control/config", json={"daemon_pairs": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data["daemon_pairs"] == 3
        assert data["daemon_enabled"] == orig["daemon_enabled"]
        # Restore
        client.put("/api/control/config", json={"daemon_pairs": orig["daemon_pairs"]})

    def test_set_daemon_pairs_accepts_exact_evaluation_cap(self, client):
        orig = client.get("/api/control/config").json()
        try:
            resp = client.put("/api/control/config", json={"daemon_pairs": 8})
            assert resp.status_code == 200
            assert resp.json()["daemon_pairs"] == 8
        finally:
            client.put(
                "/api/control/config",
                json={"daemon_pairs": orig["daemon_pairs"]},
            )

    def test_set_invalid_type(self, client):
        resp = client.put("/api/control/config", json={"daemon_workers": "not_a_number"})
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "payload",
        (
            {"daemon_workers": 0},
            {"daemon_workers": 13},
            {"daemon_pairs": 0},
            {"daemon_pairs": 9},
        ),
    )
    def test_set_rejects_out_of_range_values_instead_of_clamping(
        self,
        client,
        payload,
    ):
        assert client.put("/api/control/config", json=payload).status_code == 422

    def test_running_runtime_rejects_config_and_session_mutation(self, client):
        from server.state import app_state

        app_state.set_running(True)
        try:
            config = client.put("/api/control/config", json={"daemon_pairs": 3})
            session = client.delete("/api/control/orchestrator/session")
        finally:
            app_state.set_running(False)

        assert config.status_code == 409
        assert session.status_code == 410
        assert config.json()["detail"]["code"] == (
            "runtime_mutation_while_evolution_active"
        )

    def test_app_state_failed_atomic_write_leaves_memory_and_file_unchanged(
        self,
        tmp_path,
        monkeypatch,
    ):
        from server.state import AppState

        path = tmp_path / "app_config.json"
        state = AppState(config_file=path)
        state.update_config(daemon_enabled=False, daemon_workers=2, daemon_pairs=4)
        before_config = state.get_config()
        before_bytes = path.read_bytes()
        monkeypatch.setattr(
            state,
            "_write_config_atomic",
            lambda _payload: (_ for _ in ()).throw(OSError("disk full")),
        )

        with pytest.raises(OSError, match="disk full"):
            state.update_config(daemon_pairs=5)

        assert state.get_config() == before_config
        assert path.read_bytes() == before_bytes

    def test_app_state_restores_prior_file_when_directory_fsync_fails(
        self,
        tmp_path,
        monkeypatch,
    ):
        from server.state import AppState

        path = tmp_path / "app_config.json"
        state = AppState(config_file=path)
        state.update_config(daemon_enabled=False, daemon_workers=2, daemon_pairs=4)
        before_config = state.get_config()
        before_bytes = path.read_bytes()
        calls = []

        def fail_publish_sync_once():
            calls.append("fsync")
            if len(calls) == 1:
                raise OSError("directory fsync failed")

        monkeypatch.setattr(state, "_fsync_config_directory", fail_publish_sync_once)

        with pytest.raises(OSError, match="directory fsync failed"):
            state.update_config(daemon_pairs=5)

        assert calls == ["fsync", "fsync"]
        assert state.get_config() == before_config
        assert path.read_bytes() == before_bytes

    def test_app_state_does_not_hold_reader_lock_during_config_fsync(
        self,
        tmp_path,
        monkeypatch,
    ):
        from server.state import AppState

        state = AppState(config_file=tmp_path / "app_config.json")
        observed = []

        def probe_reader_while_writer_is_in_io(_payload):
            reader = threading.Thread(
                target=lambda: observed.append(state.get_config()),
            )
            reader.start()
            reader.join(timeout=0.5)
            assert reader.is_alive() is False

        monkeypatch.setattr(
            state,
            "_write_config_atomic",
            probe_reader_while_writer_is_in_io,
        )

        state.update_config(daemon_pairs=4)

        assert observed and observed[0]["daemon_pairs"] in {4, 5}

    def test_config_transaction_rolls_back_when_stability_reset_fails(
        self,
        client,
        monkeypatch,
    ):
        import server.routes.control as control
        from server.state import app_state

        before = app_state.get_config()
        requested = 2 if before["daemon_pairs"] != 2 else 3
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {"configured": before["daemon_enabled"], "alive": False},
        )
        monkeypatch.setattr(
            control,
            "_bind_and_reset_stability",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("observer unavailable")
            ),
        )

        response = client.put(
            "/api/control/config",
            json={"daemon_pairs": requested},
        )

        assert response.status_code == 500
        assert response.json()["detail"]["code"] == (
            "runtime_configuration_transaction_failed"
        )
        assert app_state.get_config() == before

    def test_pre_daemon_config_failure_does_not_restart_original_daemon(
        self,
        client,
        monkeypatch,
    ):
        import server.routes.control as control
        from server.state import app_state

        before = app_state.get_config()
        requested = 2 if before["daemon_pairs"] != 2 else 3
        restored = []
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {"configured": True, "alive": True},
        )
        monkeypatch.setattr(
            control,
            "_bind_and_reset_stability",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("observer unavailable before daemon phase")
            ),
        )
        monkeypatch.setattr(
            control,
            "_restore_daemon_configuration",
            lambda *_args, **_kwargs: restored.append("restarted"),
        )

        response = client.put(
            "/api/control/config",
            json={"daemon_pairs": requested},
        )

        assert response.status_code == 500
        assert restored == []
        assert app_state.get_config() == before

    def test_cancelled_lifecycle_request_drains_inner_transaction(
        self,
        monkeypatch,
    ):
        import server.routes.control as control

        async def scenario():
            monkeypatch.setattr(control, "_RUNTIME_LIFECYCLE_LOCK", asyncio.Lock())
            started = asyncio.Event()
            completed = asyncio.Event()

            async def operation():
                started.set()
                await asyncio.sleep(0.03)
                completed.set()
                return "committed"

            request_task = asyncio.create_task(
                control._run_lifecycle_operation(operation)
            )
            await started.wait()
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            assert completed.is_set()

        asyncio.run(scenario())

    def test_lifecycle_operations_are_serialized_across_awaits(
        self,
        monkeypatch,
    ):
        import server.routes.control as control

        async def scenario():
            monkeypatch.setattr(control, "_RUNTIME_LIFECYCLE_LOCK", asyncio.Lock())
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            events = []

            async def first():
                events.append("first:start")
                first_started.set()
                await release_first.wait()
                events.append("first:end")

            async def second():
                events.append("second:start")
                events.append("second:end")

            first_task = asyncio.create_task(
                control._run_lifecycle_operation(first)
            )
            await first_started.wait()
            second_task = asyncio.create_task(
                control._run_lifecycle_operation(second)
            )
            await asyncio.sleep(0.01)
            assert events == ["first:start"]
            release_first.set()
            await asyncio.gather(first_task, second_task)
            assert events == [
                "first:start",
                "first:end",
                "second:start",
                "second:end",
            ]

        asyncio.run(scenario())

    def test_runtime_override_does_not_persist_user_config(self, tmp_path):
        from server.state import AppState

        path = tmp_path / "app_config.json"
        state = AppState(config_file=path)
        state.update_config(daemon_enabled=True, daemon_workers=2, daemon_pairs=4)

        runtime = state.override_runtime_config(daemon_enabled=False, daemon_workers=3, daemon_pairs=8)

        assert runtime["daemon_enabled"] is False
        assert runtime["daemon_workers"] == 3
        assert runtime["daemon_pairs"] == 8
        saved = json.loads(path.read_text())
        assert saved["daemon_enabled"] is True
        assert saved["daemon_workers"] == 2
        assert saved["daemon_pairs"] == 4

        reloaded = AppState(config_file=path)
        assert reloaded.get_config()["daemon_enabled"] is True
        assert reloaded.get_config()["daemon_workers"] == 2
        assert reloaded.get_config()["daemon_pairs"] == 4


class TestStatus:
    def test_returns_state(self, client):
        resp = client.get("/api/control/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "mode" in data
        assert "daemon_enabled" in data
        assert data["stability_observation"]["count"] == 0
        assert data["stability_observation"]["target"] == 10
        assert data["stability_observation"]["strategy_evidence_weight"] == 0
        assert len(data["stability_observation_digest"]) == 64
        assert all(
            char in "0123456789abcdef"
            for char in data["stability_observation_digest"]
        )

    def test_status_and_health_snapshot_builds_do_not_block_event_loop(
        self,
        monkeypatch,
    ):
        import server.routes.control as control

        async def scenario():
            for endpoint, builder_name in (
                (control.control_status, "_control_status_snapshot"),
                (control.control_health, "_control_health_snapshot"),
            ):
                monkeypatch.setattr(
                    control,
                    builder_name,
                    lambda: time.sleep(0.08) or {"snapshot": builder_name},
                )
                ticked = asyncio.Event()

                async def ticker():
                    await asyncio.sleep(0.01)
                    ticked.set()

                snapshot_task = asyncio.create_task(endpoint())
                ticker_task = asyncio.create_task(ticker())
                await asyncio.wait_for(ticked.wait(), timeout=0.05)
                assert snapshot_task.done() is False
                await snapshot_task
                await ticker_task

        asyncio.run(scenario())

    def test_status_uses_active_checkpoint_target(self, client, monkeypatch):
        import epoch_authority
        import server.routes.control as control
        from server.state import app_state

        app_state.bootstrap(142)
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "current_v": 143,
                "next_v": 144,
                "strict_generation_count": 1,
                "active_generation": {
                    "next_v": 144,
                    "source_v": 143,
                    "stage": "prepared",
                    "run_id": "generation:144:strict",
                    "workflow_run_id": "generation:144:strict",
                    "attempt": {"generation": 0, "audit": 0, "precommit": 0},
                },
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "strict_published",
                "initialized": True,
                "version_authority_high_water": 143,
                "strict_published_versions": [143],
                "active_bots": ["national_v143"],
                "reset_receipt_valid": True,
                "reset_receipt_digest": "a" * 64,
                "reset_receipt_issues": [],
                "operator_action": None,
                "operator_command": None,
                "ignored_checkpoint": None,
                "max_committed_v": 143,
            },
        )
        monkeypatch.setattr(
            epoch_authority,
            "unpublished_candidate_versions",
            lambda: [],
        )

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["current_v"] == 143
        assert data["next_v"] == 144
        assert data["generation_count"] == 1
        assert data["active_generation"]["stage"] == "prepared"
        assert data["evaluation_epoch"] == "national_tcp_policy_v1"
        assert data["reset_receipt_digest"] == "a" * 64
        assert len(data["stream_authority_digest"]) == 64

    def test_status_propagates_content_bound_first_strict_operator_transition(
        self,
        client,
        monkeypatch,
    ):
        import epoch_authority

        transition = {
            "schema_version": 1,
            "kind": "first-strict-official-operator-transition",
            "state": "bootstrap_required",
            "action": "run_first_strict_official_certification",
            "command": "python scripts/official_certify.py bootstrap-first-strict",
            "reason": "authorized_bootstrap_job_not_started",
            "certification_profile": "first_strict_control_v1",
            "opponent_authority": "system_control",
            "strength_evidence_weight": 0,
            "strategy_evidence_weight": 0,
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": "generation:143:transition-test",
            "candidate_version": 143,
            "source_v": 142,
            "checkpoint_stage": "official_bootstrap_required",
            "checkpoint_revision": 9,
            "candidate_hash": "a" * 64,
            "parked_request_digest": "b" * 64,
            "transition_digest": "c" * 64,
        }
        monkeypatch.setattr(epoch_authority, "strict_epoch_projection", lambda: {
            "current_v": 142,
            "next_v": 143,
            "strict_generation_count": 0,
            "active_generation": {
                "next_v": 143,
                "source_v": 142,
                "stage": "official_bootstrap_required",
                "run_id": "143#0",
                "workflow_run_id": "generation:143:transition-test",
                "attempt": {"generation": 0, "audit": 0, "precommit": 0},
            },
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "fresh_bootstrap_ready",
            "initialized": True,
            "version_authority_high_water": 142,
            "strict_published_versions": [],
            "active_bots": [],
            "reset_receipt_valid": True,
            "reset_receipt_issues": [],
            "operator_action": transition["action"],
            "operator_command": transition["command"],
            "operator_transition": transition,
            "ignored_checkpoint": None,
        })
        monkeypatch.setattr(
            epoch_authority,
            "unpublished_candidate_versions",
            lambda: [143],
        )

        payload = client.get("/api/control/status").json()

        assert payload["operator_transition"] == transition

    def test_status_never_promotes_legacy_checkpoint(self, client, monkeypatch):
        import epoch_authority
        import server.routes.control as control
        from server.state import app_state

        app_state.bootstrap(155)
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "current_v": 142,
                "next_v": 143,
                "strict_generation_count": 0,
                "active_generation": None,
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "reset_required",
                "initialized": False,
                "version_authority_high_water": 142,
                "strict_published_versions": [],
                "active_bots": [],
                "reset_receipt_valid": False,
                "reset_receipt_issues": ["policy_epoch_reset_receipt_missing_or_unsafe"],
                "operator_action": "execute_policy_epoch_reset",
                "operator_command": "python scripts/reset_national_tcp_policy_epoch.py --execute --acknowledge-runtime-checkout",
                "ignored_checkpoint": {
                    "next_v": 155,
                    "source_v": 142,
                    "stage": "direction_audited",
                    "reason": "checkpoint_not_bound_to_strict_epoch",
                    "issues": ["checkpoint_schema_version_missing_or_mismatch"],
                },
                "max_committed_v": 142,
            },
        )
        monkeypatch.setattr(
            epoch_authority,
            "unpublished_candidate_versions",
            lambda: [155],
        )

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["current_v"] == 142
        assert data["next_v"] == 143
        assert data["generation_count"] == 0
        assert data["active_generation"] is None
        assert data["ignored_checkpoint"]["next_v"] == 155
        assert data["unpublished_candidate_versions"] == [155]
        assert data["epoch_initialized"] is False

    def test_status_projection_failure_is_complete_and_never_leaks_app_state(self, client, monkeypatch):
        import epoch_authority
        from server.state import app_state

        app_state.bootstrap(155)

        def unavailable():
            raise RuntimeError("projection evidence unreadable")

        monkeypatch.setattr(epoch_authority, "strict_epoch_projection", unavailable)

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["epoch_state"] == "epoch_authority_unavailable"
        assert data["epoch_initialized"] is False
        assert data["current_v"] == 0
        assert data["next_v"] == 0
        assert data["version_authority_high_water"] == 0
        assert data["strict_generation_count"] == 0
        assert data["strict_published_versions"] == []
        assert data["active_bots"] == []
        assert data["active_generation"] is None
        assert data["unpublished_candidate_versions"] == []
        assert data["reset_receipt_issues"] == [
            "canonical_epoch_projection_unavailable"
        ]
        assert "projection evidence unreadable" in data["status_sync_error"]

    def test_status_uses_current_epoch_abandoned_floor_without_checkpoint(self, client, monkeypatch):
        import epoch_authority
        import server.routes.control as control
        from server.state import app_state

        app_state.bootstrap(143)
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "current_v": 143,
                "next_v": 145,
                "strict_generation_count": 1,
                "active_generation": None,
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "strict_published",
                "initialized": True,
                "version_authority_high_water": 143,
                "strict_published_versions": [143],
                "active_bots": ["national_v143"],
                "reset_receipt_valid": True,
                "reset_receipt_issues": [],
                "operator_action": None,
                "operator_command": None,
                "ignored_checkpoint": None,
                "max_committed_v": 143,
            },
        )
        monkeypatch.setattr(
            epoch_authority,
            "unpublished_candidate_versions",
            lambda: [],
        )

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["current_v"] == 143
        assert data["next_v"] == 145
        assert data["generation_count"] == 1
        assert data["active_generation"] is None

    def test_view_only_lifespan_does_not_start_orchestrator(self, monkeypatch):
        import server.app as app_module
        from server.state import app_state

        events = []

        class TrapOrchestrator:
            def __getattr__(self, name):
                raise AssertionError(f"view-only mode imported orchestrator.{name}")

        app_state.stop_running()
        monkeypatch.setenv("POK_WEB_VIEW_ONLY", "1")
        monkeypatch.setitem(sys.modules, "orchestrator", TrapOrchestrator())
        monkeypatch.setattr(app_module, "configure_logging", lambda **_kwargs: None)
        monkeypatch.setattr(app_module.web_ui, "log_history", lambda *args: events.append(args))

        async def run_lifespan():
            async with app_module.lifespan(app_module.app):
                assert app_state.to_dict()["running"] is False
                assert app_state.task_snapshot()["present"] is False

        asyncio.run(run_lifespan())

        assert any("view-only mode" in event[0] for event in events)

    def test_health_reports_stopped_state(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.set_running(False)
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {"exists": False, "pid": None, "alive": False},
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {"exists": False, "stage": None},
        )

        resp = client.get("/api/control/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "stopped"
        assert "evolution_not_running" in data["issues"]
        assert data["task"]["present"] in (False, True)

    def test_health_degrades_when_running_task_is_done(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.set_running(True)
        app_state.override_runtime_config(daemon_enabled=True)
        monkeypatch.setattr(
            app_state,
            "task_snapshot",
            lambda: {
                "present": True,
                "done": True,
                "cancelled": False,
                "shutdown_requested": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {
                "exists": True,
                "pid": 123,
                "alive": True,
                "heartbeat_stale": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {"exists": False, "stage": None},
        )

        resp = client.get("/api/control/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "degraded"
        assert "orchestrator_task_not_active" in data["issues"]
        app_state.set_running(False)

    @pytest.mark.parametrize(
        ("daemon_enabled", "expected_overall", "expected_health_error"),
        (
            (False, "healthy", None),
            (True, "degraded", "daemon_pid_file_missing"),
        ),
    )
    def test_running_health_distinguishes_no_daemon_from_missing_enabled_daemon(
        self,
        client,
        monkeypatch,
        daemon_enabled,
        expected_overall,
        expected_health_error,
    ):
        import server.routes.control as control
        from server.state import app_state

        app_state.set_running(True)
        app_state.override_runtime_config(daemon_enabled=daemon_enabled)
        monkeypatch.setattr(
            app_state,
            "task_snapshot",
            lambda: {
                "present": True,
                "done": False,
                "cancelled": False,
                "shutdown_requested": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_sync_evolution_fields",
            lambda _state: {
                "running": True,
                "daemon_enabled": daemon_enabled,
                "epoch_initialized": True,
                "active_generation": None,
                "post_publication_handoff": None,
                "stability_observation": {
                    "continuity_valid": True,
                    "verification": {
                        "state": "fresh",
                        "fresh_until": time.time() + 60,
                    },
                },
            },
        )
        monkeypatch.setattr(
            control,
            "_read_pid_info",
            lambda _path: {
                "exists": False,
                "pid": None,
                "alive": False,
                "process_identity": "missing",
                "health_error": "daemon_pid_file_missing",
            },
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {"exists": False, "stage": None},
        )

        response = client.get("/api/control/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["overall"] == expected_overall
        assert payload["daemon"]["configured"] is daemon_enabled
        assert payload["daemon"]["health_error"] == expected_health_error
        assert payload["daemon"]["heartbeat_status"] == (
            "missing" if daemon_enabled else "not_applicable"
        )
        if daemon_enabled:
            assert "daemon_dead" in payload["issues"]
            assert (
                "daemon_health_error:daemon_pid_file_missing"
                in payload["issues"]
            )
        else:
            assert payload["issues"] == []
        app_state.set_running(False)

    def test_shutdown_requested_unfinished_task_retains_runtime_ownership(
        self,
        monkeypatch,
    ):
        import server.routes.control as control
        from server.state import app_state

        monkeypatch.setattr(
            app_state,
            "task_snapshot",
            lambda: {
                "present": True,
                "done": False,
                "cancelled": True,
                "shutdown_requested": True,
            },
        )
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {
                "configured": False,
                "exists": False,
                "pid": None,
                "alive": False,
                "heartbeat_status": "not_applicable",
            },
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {"exists": False, "stage": None},
        )

        snapshot = control._health_summary({
            "running": True,
            "daemon_enabled": False,
            "epoch_initialized": True,
            "active_generation": None,
            "stability_observation": {
                "continuity_valid": True,
                "verification": {
                    "state": "fresh",
                    "fresh_until": time.time() + 60,
                },
            },
        })

        assert "orchestrator_stop_in_progress" in snapshot["issues"]
        assert "orchestrator_task_not_active" not in snapshot["issues"]
        assert snapshot["task"]["done"] is False

    def test_health_degrades_when_daemon_heartbeat_is_stale(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.set_running(True)
        app_state.override_runtime_config(daemon_enabled=True)
        monkeypatch.setattr(
            app_state,
            "task_snapshot",
            lambda: {
                "present": True,
                "done": False,
                "cancelled": False,
                "shutdown_requested": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {
                "configured": True,
                "exists": True,
                "pid": 123,
                "alive": True,
                "heartbeat_status": "stale",
                "heartbeat_stale": True,
                "heartbeat_age_sec": 999,
            },
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {"exists": True, "stage": "direction_audited"},
        )

        resp = client.get("/api/control/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "degraded"
        assert "daemon_heartbeat_stale" in data["issues"]
        app_state.set_running(False)

    def test_health_degrades_when_checkpoint_recovery_is_unrecoverable(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.set_running(True)
        monkeypatch.setattr(
            app_state,
            "task_snapshot",
            lambda: {
                "present": True,
                "done": False,
                "cancelled": False,
                "shutdown_requested": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {
                "exists": True,
                "pid": 123,
                "alive": True,
                "heartbeat_stale": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {
                "exists": True,
                "stage": "workers_done",
                "recovery": {
                    "recoverable": False,
                    "issues": ["repo_baseline_head_mismatch"],
                },
            },
        )

        resp = client.get("/api/control/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "degraded"
        assert "pipeline_repo_baseline_head_mismatch" in data["issues"]
        app_state.set_running(False)

    def test_pipeline_health_exposes_route_from_same_revalidated_checkpoint(
        self,
        monkeypatch,
    ):
        import checkpoint_schema
        import evolution_infra
        import pipeline_recovery
        import pipeline_state
        import server.routes.control as control

        checkpoint = {
            "next_v": 143,
            "source_v": 142,
            "parent2_v": None,
            "stage": "direction_audited",
            "run_id": "143#0",
            "workflow_run_id": "generation:143:route-test",
            "checkpoint_revision": 7,
            "generation_attempt": 0,
        }
        status = {
            "epoch_initialized": True,
            "epoch_state": "fresh_bootstrap_ready",
            "active_generation": {
                "next_v": 143,
                "source_v": 142,
                "stage": "direction_audited",
                "run_id": "143#0",
                "workflow_run_id": "generation:143:route-test",
                "checkpoint_revision": 7,
                "attempt": {"generation": 0, "audit": 0, "precommit": 0},
            },
        }
        route = {
            "stage": "direction_audited",
            "next_v": 143,
            "source_v": 142,
            "parent2_v": None,
            "next_tool": "run_master",
            "allowed_tools": ["run_master"],
            "intent": "pipeline",
            "failure_class": None,
            "directive": "Call run_master.",
        }
        seen = []
        monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda value: [])
        monkeypatch.setattr(
            checkpoint_schema,
            "live_policy_epoch_reset_receipt_errors",
            lambda value, **_kwargs: [],
        )
        monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
        monkeypatch.setattr(
            pipeline_recovery,
            "checkpoint_recovery_diagnostics",
            lambda value: {"recoverable": True, "issues": []},
        )
        monkeypatch.setattr(
            pipeline_state,
            "route_policy",
            lambda value: seen.append(value) or route,
        )

        snapshot = control._read_pipeline_health(status)

        assert snapshot["route"] == route
        assert snapshot["source_v"] == 142
        assert snapshot["run_id"] == "143#0"
        assert snapshot["checkpoint_revision"] == 7
        assert seen == [checkpoint]

    def test_pipeline_health_withholds_route_when_recovery_is_unproven(
        self,
        monkeypatch,
    ):
        import checkpoint_schema
        import evolution_infra
        import pipeline_recovery
        import pipeline_state
        import server.routes.control as control

        checkpoint = {
            "next_v": 143,
            "source_v": 142,
            "parent2_v": None,
            "stage": "direction_audited",
            "run_id": "143#0",
            "workflow_run_id": "generation:143:blocked-route-test",
            "checkpoint_revision": 7,
            "generation_attempt": 0,
        }
        status = {
            "epoch_initialized": True,
            "epoch_state": "fresh_bootstrap_ready",
            "operator_action": None,
            "active_generation": {
                "next_v": 143,
                "source_v": 142,
                "stage": "direction_audited",
                "run_id": "143#0",
                "workflow_run_id": "generation:143:blocked-route-test",
                "checkpoint_revision": 7,
                "attempt": {"generation": 0, "audit": 0, "precommit": 0},
            },
        }
        route_calls = []
        monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda _value: [])
        monkeypatch.setattr(
            checkpoint_schema,
            "live_policy_epoch_reset_receipt_errors",
            lambda _value, **_kwargs: [],
        )
        monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
        monkeypatch.setattr(
            pipeline_recovery,
            "checkpoint_recovery_diagnostics",
            lambda _value: {
                "recoverable": False,
                "issues": ["repo_baseline_head_mismatch"],
            },
        )
        monkeypatch.setattr(
            pipeline_state,
            "route_policy",
            lambda value: route_calls.append(value) or {
                "stage": "direction_audited",
                "next_tool": "run_master",
            },
        )

        snapshot = control._read_pipeline_health(status)

        assert snapshot["blocked"] is True
        assert snapshot["route"] is None
        assert snapshot["issues"] == ["repo_baseline_head_mismatch"]
        assert route_calls == []

    def test_pipeline_health_projects_scheduler_owned_no_checkpoint_boundary(self):
        import server.routes.control as control

        snapshot = control._read_pipeline_health({
            "epoch_initialized": True,
            "epoch_state": "strict_published",
            "current_v": 143,
            "next_v": 145,
            "active_generation": None,
            "post_publication_handoff": {"status": "none"},
            "ignored_checkpoint": None,
        })

        assert snapshot["blocked"] is False
        assert snapshot.get("route") is None
        assert snapshot["scheduler_boundary"] == {
            "authority": "outer_scheduler",
            "state": "ready_to_prepare",
            "provider_action": "end_stream",
            "scheduler_action": "prepare_generation",
            "next_v": 145,
            "source_v": None,
        }

        operator_blocked = control._read_pipeline_health({
            "epoch_initialized": True,
            "epoch_state": "fresh_bootstrap_ready",
            "current_v": 142,
            "next_v": 143,
            "active_generation": None,
            "post_publication_handoff": {"status": "none"},
            "ignored_checkpoint": None,
            "operator_action": "finalize_first_strict_publication",
        })
        assert operator_blocked["blocked"] is True
        assert operator_blocked["operator_action_required"] is True
        assert operator_blocked["scheduler_boundary"] is None

    @pytest.mark.parametrize(
        "status_patch",
        (
            {"operator_action": "operator_reconcile_checkpoint"},
            {"ignored_checkpoint": {"reason": "checkpoint_invalid"}},
            {"epoch_initialized": False},
        ),
    )
    def test_pipeline_health_withholds_handoff_route_on_outer_authority_block(
        self,
        status_patch,
    ):
        import server.routes.control as control

        status = {
            "epoch_initialized": True,
            "epoch_state": "strict_published",
            "operator_action": None,
            "ignored_checkpoint": None,
            "active_generation": None,
            "post_publication_handoff": {
                "status": "pending",
                "state": "pending",
                "blocked": False,
                "version": 144,
                "source_v": 143,
                "workflow_run_id": "generation:144:workflow-v1",
                "identity_digest": "i" * 64,
                "projection_digest": "p" * 64,
                "publication_id": "u" * 64,
                "record_revision": 1,
                "issues": [],
            },
            **status_patch,
        }

        snapshot = control._read_pipeline_health(status)

        assert snapshot["blocked"] is True
        assert snapshot["route"] is None

    @pytest.mark.parametrize(
        ("field", "changed"),
        (
            ("source_v", 141),
            ("parent2_v", 140),
            ("run_id", "143#other"),
            ("checkpoint_revision", 8),
        ),
    )
    def test_pipeline_health_rejects_same_stage_checkpoint_identity_drift(
        self,
        monkeypatch,
        field,
        changed,
    ):
        import checkpoint_schema
        import evolution_infra
        import server.routes.control as control

        checkpoint = {
            "next_v": 143,
            "source_v": 142,
            "parent2_v": None,
            "stage": "direction_audited",
            "run_id": "143#0",
            "workflow_run_id": "generation:143:route-test",
            "checkpoint_revision": 7,
            "generation_attempt": 0,
        }
        checkpoint[field] = changed
        status = {
            "epoch_initialized": True,
            "active_generation": {
                "next_v": 143,
                "source_v": 142,
                "parent2_v": None,
                "stage": "direction_audited",
                "run_id": "143#0",
                "workflow_run_id": "generation:143:route-test",
                "checkpoint_revision": 7,
                "attempt": {"generation": 0, "audit": 0, "precommit": 0},
            },
        }
        monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda _value: [])
        monkeypatch.setattr(
            checkpoint_schema,
            "live_policy_epoch_reset_receipt_errors",
            lambda _value, **_kwargs: [],
        )
        monkeypatch.setattr(
            evolution_infra,
            "read_pipeline_checkpoint",
            lambda: checkpoint,
        )

        snapshot = control._read_pipeline_health(status)

        assert snapshot["error"] == "strict_checkpoint_revalidation_failed"
        assert field in snapshot["identity_mismatches"]
        assert "route" not in snapshot

    @pytest.mark.parametrize(
        ("route_intent", "expected_blocked"),
        (
            ("terminal_gate_abandon", False),
            ("operator_reconcile_checkpoint", True),
        ),
    )
    def test_pipeline_health_distinguishes_terminal_admission_from_recovery_block(
        self,
        monkeypatch,
        route_intent,
        expected_blocked,
    ):
        import checkpoint_schema
        import evolution_infra
        import pipeline_recovery
        import pipeline_state
        import server.routes.control as control

        digest = "a" * 64
        checkpoint = {
            "next_v": 143,
            "source_v": 142,
            "parent2_v": None,
            "stage": "review_rejected",
            "run_id": "143#0",
            "workflow_run_id": "generation:143:terminal-route-test",
            "checkpoint_revision": 9,
            "generation_attempt": 0,
            "terminal_gate_outcome": {
                "schema_version": 1,
                "kind": "pipeline-terminal-gate-outcome-v1",
                "gate_name": "review",
                "terminal_stage": "review_rejected",
                "reason_code": "review_rejected",
                "failure_class": "strategy_review",
                "disposition": "abandon_generation",
                "receipt_digest": digest,
            },
        }
        status = {
            "epoch_initialized": True,
            "active_generation": {
                "next_v": 143,
                "source_v": 142,
                "parent2_v": None,
                "stage": "review_rejected",
                "run_id": "143#0",
                "workflow_run_id": "generation:143:terminal-route-test",
                "checkpoint_revision": 9,
                "attempt": {"generation": 0, "audit": 0, "precommit": 0},
            },
        }
        monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda _value: [])
        monkeypatch.setattr(
            checkpoint_schema,
            "live_policy_epoch_reset_receipt_errors",
            lambda _value, **_kwargs: [],
        )
        monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
        monkeypatch.setattr(
            pipeline_recovery,
            "checkpoint_recovery_diagnostics",
            lambda _value: {"active": True, "recoverable": True, "issues": []},
        )
        route = {
            "stage": "review_rejected",
            "next_v": 143,
            "source_v": 142,
            "parent2_v": None,
            "next_tool": (
                "abandon_generation"
                if route_intent == "terminal_gate_abandon"
                else None
            ),
            "allowed_tools": (
                ["abandon_generation"]
                if route_intent == "terminal_gate_abandon"
                else []
            ),
            "intent": route_intent,
            "issues": (
                []
                if route_intent == "terminal_gate_abandon"
                else ["terminal_outcome_receipt_digest_invalid"]
            ),
            "terminal_gate_outcome_digest": (
                digest if route_intent == "terminal_gate_abandon" else None
            ),
        }
        monkeypatch.setattr(pipeline_state, "route_policy", lambda _value: route)

        snapshot = control._read_pipeline_health(status)

        assert snapshot["blocked"] is expected_blocked
        assert snapshot["recovery_blocked"] is expected_blocked
        if expected_blocked:
            assert snapshot["error"] == "terminal_gate_outcome_invalid"
            assert snapshot.get("terminalization_pending") is not True
            assert "terminal_gate_outcome_requires_operator_reconciliation" in snapshot["issues"]
        else:
            assert snapshot["admission_blocked"] is True
            assert snapshot["terminalization_pending"] is True
            assert snapshot["gate_outcome"]["receipt_digest"] == digest
            assert snapshot["route"]["next_tool"] == "abandon_generation"

    @pytest.mark.parametrize(
        ("heartbeat", "expected"),
        (
            (None, "missing"),
            (float("nan"), "invalid"),
            ("not-a-number", "invalid"),
            ("future", "future"),
            ("stale", "stale"),
            ("fresh", "fresh"),
        ),
    )
    def test_daemon_heartbeat_classification(
        self,
        monkeypatch,
        tmp_path,
        heartbeat,
        expected,
    ):
        import server.routes.control as control
        import daemon_management
        from server.state import app_state

        app_state.override_runtime_config(daemon_enabled=True)
        monkeypatch.setattr(control, "RESULTS_DIR", tmp_path)
        # Heartbeat classification is tested independently from the exact
        # daemon argv/owner-token proof below.
        monkeypatch.setattr(
            daemon_management,
            "_pid_record_identity",
            lambda _record: "match",
        )
        pid = os.getpid()
        # Read the real current process start identity; no process is spawned.
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            start_ticks = int(handle.read().split()[21])
        payload = {"pid": pid, "ppid": os.getppid(), "start_ticks": start_ticks}
        if heartbeat is None:
            pass
        elif heartbeat == "future":
            payload["last_heartbeat"] = time.time() + 60
        elif heartbeat == "stale":
            payload["last_heartbeat"] = time.time() - 999
        elif heartbeat == "fresh":
            payload["last_heartbeat"] = time.time()
        else:
            payload["last_heartbeat"] = heartbeat
        (tmp_path / ".daemon_pid").write_text(json.dumps(payload), encoding="utf-8")

        snapshot = control._daemon_health_snapshot()

        assert snapshot["configured"] is True
        assert snapshot["alive"] is True
        assert snapshot["heartbeat_status"] == expected
        assert (snapshot["health_error"] is None) is (expected == "fresh")

    def test_daemon_pid_reuse_and_disabled_live_process_fail_closed(
        self,
        monkeypatch,
        tmp_path,
    ):
        import server.routes.control as control
        import daemon_management
        from server.state import app_state

        monkeypatch.setattr(control, "RESULTS_DIR", tmp_path)
        pid = os.getpid()
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            start_ticks = int(handle.read().split()[21])
        path = tmp_path / ".daemon_pid"
        app_state.override_runtime_config(daemon_enabled=True)
        missing = control._daemon_health_snapshot()
        assert missing["alive"] is False
        assert missing["heartbeat_status"] == "missing"
        assert missing["health_error"] == "daemon_pid_file_missing"

        path.write_text(json.dumps({
            "pid": pid,
            "ppid": os.getppid(),
            "start_ticks": start_ticks + 1,
            "last_heartbeat": time.time(),
        }), encoding="utf-8")
        reused = control._daemon_health_snapshot()

        assert reused["alive"] is False
        assert reused["heartbeat_status"] == "invalid"
        assert reused["health_error"] == "daemon_pid_reused"

        monkeypatch.setattr(
            daemon_management,
            "_pid_record_identity",
            lambda _record: "match",
        )
        path.write_text(json.dumps({
            "pid": pid,
            "ppid": os.getppid(),
            "start_ticks": start_ticks,
            "last_heartbeat": time.time(),
        }), encoding="utf-8")
        app_state.override_runtime_config(daemon_enabled=False)
        disabled = control._daemon_health_snapshot()
        assert disabled["configured"] is False
        assert disabled["alive"] is True
        assert disabled["heartbeat_status"] == "not_applicable"
        assert disabled["health_error"] == "daemon_running_while_disabled"

    def test_disabled_daemon_treats_missing_pid_as_not_applicable(
        self,
        monkeypatch,
        tmp_path,
    ):
        import server.routes.control as control
        from server.state import app_state

        monkeypatch.setattr(control, "RESULTS_DIR", tmp_path)
        app_state.override_runtime_config(daemon_enabled=False)

        snapshot = control._daemon_health_snapshot()

        assert snapshot["configured"] is False
        assert snapshot["exists"] is False
        assert snapshot["alive"] is False
        assert snapshot["heartbeat_status"] == "not_applicable"
        assert snapshot["health_error"] is None

    @pytest.mark.parametrize(
        ("identity", "health_error"),
        (
            ("owner_mismatch", "daemon_owner_token_mismatch"),
            ("command_mismatch", "daemon_command_mismatch"),
            ("group_mismatch", "daemon_process_group_mismatch"),
            ("unavailable", "daemon_process_identity_unavailable"),
        ),
    )
    def test_fresh_heartbeat_cannot_override_daemon_process_identity_failure(
        self,
        monkeypatch,
        tmp_path,
        identity,
        health_error,
    ):
        import daemon_management
        import server.routes.control as control
        from server.state import app_state

        app_state.override_runtime_config(daemon_enabled=True)
        monkeypatch.setattr(control, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(
            daemon_management,
            "_pid_record_identity",
            lambda _record: identity,
        )
        (tmp_path / ".daemon_pid").write_text(
            json.dumps({
                "pid": os.getpid(),
                "start_ticks": 123,
                "owner_token_digest": "a" * 64,
                "last_heartbeat": time.time(),
            }),
            encoding="utf-8",
        )

        snapshot = control._daemon_health_snapshot()

        assert snapshot["alive"] is False
        assert snapshot["process_identity"] == identity
        assert snapshot["heartbeat_status"] == "invalid"
        assert snapshot["health_error"] == health_error


class TestDecisions:
    def test_returns_list(self, client):
        resp = client.get("/api/control/decisions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestTools:
    def test_catalog_is_explicit_http_capabilities_only(self, client):
        resp = client.get("/api/control/tools")
        assert resp.status_code == 200
        data = resp.json()
        capabilities = data["capabilities"]
        assert capabilities
        assert {item["id"] for item in capabilities} == set(data["tools"])
        assert all(
            set(item) == {
                "id", "method", "path", "mutation", "enabled", "blocked_reason"
            }
            for item in capabilities
        )
        assert {
            "prepare_next_gen", "execute_workers", "run_quality_gates",
            "run_precommit_eval", "commit_bot", "abandon_generation",
            "cleanup_incomplete", "reap_incomplete", "start_daemon",
            "stop_daemon",
        }.isdisjoint(data["tools"])
        assert data["operator_auth_required"] is True
        assert data["operator_token_header"] == "X-Control-Token"
        assert "operator-secret" not in resp.text

    def test_old_executor_is_gone_for_unknown_name(self, client):
        resp = client.post("/api/control/tool/nonexistent_tool_xyz", json={"args": {}})
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "control_tool_executor_retired"

    def test_old_executor_never_calls_mcp_handler(self, client, monkeypatch):
        import tools

        class TrapTool:
            name = "prepare_next_gen"

            @staticmethod
            async def handler(_args):
                raise AssertionError("retired HTTP executor invoked MCP handler")

        monkeypatch.setattr(tools, "all_tools", [TrapTool()])

        for name in (
            "get_status", "prepare_next_gen", "execute_workers",
            "run_quality_gates", "run_precommit_eval", "commit_bot",
            "abandon_generation", "cleanup_incomplete", "reap_incomplete",
            "start_daemon", "stop_daemon",
        ):
            response = client.post(
                f"/api/control/tool/{name}",
                json={"args": {"dangerous": True}},
            )
            assert response.status_code == 410


class TestOrchestratorSession:
    def test_get(self, client):
        resp = client.get("/api/control/orchestrator/session")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["session_id"] is None
        assert data["active"] is False
        assert data["resume_supported"] is False
        assert data["provider_history_persisted"] is False
        assert data["recovery_authority"] == "validated_checkpoint_only"

    def test_clear_is_retired(self, client):
        resp = client.delete("/api/control/orchestrator/session")
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == (
            "orchestrator_provider_session_resume_retired"
        )

    def test_retired_clear_never_enters_lifecycle_transaction(
        self,
        client,
        monkeypatch,
    ):
        import server.routes.control as control

        calls = []

        async def fake_lifecycle(factory):
            calls.append("lifecycle")
            return await factory()

        monkeypatch.setattr(
            control,
            "_run_lifecycle_operation",
            fake_lifecycle,
        )

        response = client.delete("/api/control/orchestrator/session")

        assert response.status_code == 410
        assert calls == []


class TestStop:
    def test_stop(self, client):
        resp = client.post("/api/control/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"


class TestRetiredEvolutionReset:
    def test_destructive_reset_endpoint_is_not_registered(self):
        from server.app import app

        assert not any(
            route.path == "/api/control/reset"
            for route in app.routes
        )


class TestStartConflict:
    def test_start_when_not_running(self, client, monkeypatch):
        client.post("/api/control/stop")
        import orchestrator
        import server.routes.control as control

        async def fake_loop(*a, **kw):
            pass
        monkeypatch.setattr(orchestrator, "orchestrator_loop", fake_loop)
        monkeypatch.setattr(
            control,
            "_control_launch_authority_snapshot",
            lambda: (
                {
                    "epoch_initialized": True,
                    "operator_action": None,
                    "active_generation": None,
                    "post_publication_handoff": {"status": "none"},
                    "current_v": 142,
                    "next_v": 143,
                },
                {
                    "blocked": False,
                    "exists": False,
                    "scheduler_boundary": {
                        "authority": "outer_scheduler",
                        "state": "ready_to_prepare",
                        "provider_action": "end_stream",
                        "scheduler_action": "prepare_generation",
                        "next_v": 143,
                        "source_v": None,
                    },
                },
            ),
        )
        resp = client.post("/api/control/start")
        assert resp.status_code == 200
        client.post("/api/control/stop")

    def test_start_refuses_second_live_stability_owner(self, client, monkeypatch):
        import stability_observation
        import server.routes.control as control
        from server.state import app_state

        client.post("/api/control/stop")
        monkeypatch.setattr(
            stability_observation,
            "reset_stability_observation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                stability_observation.StabilityObservationError(
                    "stability_observation_owner_process_still_alive"
                )
            ),
        )
        monkeypatch.setattr(
            control,
            "_control_launch_authority_snapshot",
            lambda: (
                {
                    "epoch_initialized": True,
                    "operator_action": None,
                    "active_generation": None,
                    "post_publication_handoff": {"status": "none"},
                    "current_v": 142,
                    "next_v": 143,
                },
                {
                    "blocked": False,
                    "exists": False,
                    "scheduler_boundary": {
                        "authority": "outer_scheduler",
                        "state": "ready_to_prepare",
                        "provider_action": "end_stream",
                        "scheduler_action": "prepare_generation",
                        "next_v": 143,
                        "source_v": None,
                    },
                },
            ),
        )

        response = client.post("/api/control/start")

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == (
            "stability_observation_owner_active"
        )
        assert app_state.to_dict()["running"] is False

    @pytest.mark.parametrize(
        ("status_patch", "pipeline_patch", "expected_code"),
        (
            (
                {
                    "operator_action": "run_first_strict_official_certification",
                    "operator_command": "operator-command",
                    "epoch_state": "fresh_bootstrap_ready",
                },
                {"blocked": True, "stage": "official_bootstrap_required"},
                "operator_action_required",
            ),
            (
                {"operator_action": None, "epoch_state": "strict_published"},
                {
                    "blocked": True,
                    "stage": "workers_done",
                    "issues": ["repo_baseline_head_mismatch"],
                    "recovery": {"recoverable": True, "issues": []},
                },
                "pipeline_recovery_blocked",
            ),
            (
                {"operator_action": None, "epoch_state": "strict_published"},
                {
                    "blocked": False,
                    "stage": "workers_done",
                    "issues": [],
                    "recovery": {
                        "recoverable": False,
                        "issues": ["repo_baseline_head_mismatch"],
                    },
                },
                "pipeline_recovery_blocked",
            ),
        ),
    )
    def test_start_refuses_operator_or_recovery_boundary_before_stability_reset(
        self,
        client,
        monkeypatch,
        status_patch,
        pipeline_patch,
        expected_code,
    ):
        import server.routes.control as control
        from server.state import app_state

        client.post("/api/control/stop")
        monkeypatch.setattr(control, "_require_initialized_epoch", lambda _operation: {})
        monkeypatch.setattr(
            control,
            "_control_launch_authority_snapshot",
            lambda: (
                {"epoch_initialized": True, **status_patch},
                pipeline_patch,
            ),
        )
        monkeypatch.setattr(
            control,
            "_bind_and_reset_stability",
            lambda *_args, **_kwargs: pytest.fail(
                "stability reset reached past the launch barrier"
            ),
        )

        response = client.post("/api/control/start")

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == expected_code
        assert app_state.to_dict()["running"] is False

    @pytest.mark.asyncio
    async def test_start_refuses_live_foreign_handoff_owner_before_stability_reset(
        self,
        monkeypatch,
    ):
        import server.routes.control as control
        from fastapi import HTTPException
        from server.state import app_state

        app_state.stop_running()
        status = {
            "running": False,
            "epoch_initialized": True,
            "epoch_state": "strict_published",
            "operator_action": None,
            "ignored_checkpoint": None,
            "active_generation": None,
            "post_publication_handoff": {
                "status": "running",
                "state": "running",
                "blocked": False,
                "version": 144,
                "source_v": 143,
                "workflow_run_id": "generation:144:workflow-v1",
                "identity_digest": "i" * 64,
                "projection_digest": "p" * 64,
                "publication_id": "u" * 64,
                "record_revision": 3,
                "owner_scope": "foreign_process",
                "issues": [],
            },
        }
        pipeline = control._read_pipeline_health(status)
        assert pipeline["blocked"] is True
        assert pipeline["route"] is None
        monkeypatch.setattr(
            control,
            "_require_initialized_epoch",
            lambda _operation: {},
        )
        monkeypatch.setattr(
            control,
            "_control_launch_authority_snapshot",
            lambda: (status, pipeline),
        )
        monkeypatch.setattr(
            control,
            "_bind_and_reset_stability",
            lambda *_args, **_kwargs: pytest.fail(
                "stability reset reached past foreign handoff owner"
            ),
        )

        with pytest.raises(HTTPException) as caught:
            await control._start_evolution_transaction()

        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "pipeline_recovery_blocked"
        assert "post_publication_handoff_foreign_owner_active" in (
            caught.value.detail["issues"]
        )
        assert app_state.to_dict()["running"] is False

    @pytest.mark.asyncio
    async def test_runtime_owner_reservation_rechecks_and_fences_authority_drift(
        self,
        monkeypatch,
    ):
        import server.routes.control as control
        from server.state import app_state

        app_state.stop_running()
        samples = iter((
            {
                "allowed": True,
                "denial_code": None,
                "issues": [],
                "fence_digest": "a" * 64,
                "status": {},
                "pipeline": {},
            },
            {
                "allowed": True,
                "denial_code": None,
                "issues": [],
                "fence_digest": "b" * 64,
                "status": {},
                "pipeline": {},
            },
        ))
        monkeypatch.setattr(
            control,
            "_runtime_launch_barrier_snapshot",
            lambda: next(samples),
        )

        result = await control._reserve_runtime_launch_owner()

        assert result["acquired"] is False
        assert result["reason"] == "authority_changed"
        assert result["barrier"]["denial_code"] == "launch_authority_changed"
        assert app_state.to_dict()["running"] is False
        assert app_state.task_snapshot()["present"] is False

    @pytest.mark.parametrize(
        "failure_type",
        (RuntimeError, asyncio.CancelledError),
    )
    @pytest.mark.asyncio
    async def test_runtime_owner_reservation_releases_owner_when_recheck_raises(
        self,
        monkeypatch,
        failure_type,
    ):
        import server.routes.control as control
        from server.state import app_state

        app_state.stop_running()
        sample_count = 0

        def sample():
            nonlocal sample_count
            sample_count += 1
            if sample_count == 1:
                return {
                    "allowed": True,
                    "denial_code": None,
                    "issues": [],
                    "fence_digest": "a" * 64,
                    "status": {},
                    "pipeline": {},
                }
            raise failure_type("second launch-authority sample failed")

        monkeypatch.setattr(
            control,
            "_runtime_launch_barrier_snapshot",
            sample,
        )

        with pytest.raises(failure_type, match="second launch-authority sample failed"):
            await control._reserve_runtime_launch_owner()

        assert sample_count == 2
        assert app_state.to_dict()["running"] is False
        task = app_state.task_snapshot()
        assert task["present"] is False
        assert task["owner_id"] is None
        assert app_state.runtime_owner_id() is None

    @pytest.mark.asyncio
    async def test_start_setup_exception_releases_unattached_owner(
        self,
        monkeypatch,
    ):
        import server.app as app_module
        import server.routes.control as control
        from server.state import app_state

        app_state.stop_running()
        monkeypatch.setattr(
            control,
            "_require_initialized_epoch",
            lambda _operation: {},
        )

        async def reserve_owner():
            owner_id = app_state.begin_runtime_owner()
            assert owner_id is not None
            return {
                "acquired": True,
                "reason": "acquired",
                "owner_id": owner_id,
                "barrier": {"allowed": True, "fence_digest": "a" * 64},
            }

        monkeypatch.setattr(
            control,
            "_reserve_runtime_launch_owner",
            reserve_owner,
        )
        monkeypatch.setattr(
            app_module.web_ui._broadcaster,
            "clear",
            lambda: (_ for _ in ()).throw(
                RuntimeError("broadcaster clear failed")
            ),
        )

        with pytest.raises(RuntimeError, match="broadcaster clear failed"):
            await control._start_evolution_transaction()

        assert app_state.to_dict()["running"] is False
        assert app_state.runtime_owner_id() is None
        assert app_state.task_snapshot()["present"] is False
