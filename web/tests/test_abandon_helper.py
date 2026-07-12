"""Tests for _do_abandon_generation (B2 v125 fix helper).

Validates the shared abandon logic that MASTER_EXHAUSTED (tool_planning.py) and
CYCLE_TIMEOUT (orchestrator.py B3) now call directly instead of relying on the
orchestrator LLM to obey a plain-text directive.
"""

import asyncio

import tool_bot_management as tbm


def _run(coro):
    # A4 (2026-06-30): reset the abandon rate-limit before each test call so the
    # 60s cooldown doesn't block consecutive test abandons.
    tbm._LAST_ABANDON_TS[0] = 0.0
    tbm._LAST_ABANDON_TS[1] = ""
    return asyncio.new_event_loop().run_until_complete(coro)


class TestDoAbandonGeneration:
    def test_corrupt_checkpoint_is_preserved_for_operator_reconcile(
        self, tmp_path, monkeypatch
    ):
        import evolution_core

        corrupt = tmp_path / "pipeline_state.json"
        corrupt.write_text("{not-json", encoding="utf-8")
        candidate = tmp_path / "national_v100"
        candidate.mkdir()
        (candidate / "main.py").write_text("x=1")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", corrupt)
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: None)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["reason"] == "checkpoint_corrupt"
        assert result["action"] == "operator_reconcile"
        assert corrupt.read_text(encoding="utf-8") == "{not-json"
        assert candidate.exists()

    def test_stage_guard_is_rechecked_after_generation_actor_lock(
        self, tmp_path, monkeypatch
    ):
        import evolution_core
        import evolution_infra

        results_dir = tmp_path / "results"
        results_dir.mkdir()
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
        initial = {
            "next_v": 100,
            "source_v": 99,
            "run_id": "100#0",
            "workflow_run_id": "generation:100:guard-race",
            "checkpoint_revision": 1,
            "generation_attempt": 0,
            "stage": "master_planned",
        }
        advanced = {
            **initial,
            "checkpoint_revision": 2,
            "stage": "reviewed",
        }
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        reads = iter((initial, advanced))
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: next(reads))
        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )
        monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["stage"] == "reviewed"
        assert cleared == []
        assert not (results_dir / "abandoned_versions.jsonl").exists()

    def test_active_worker_lease_is_fenced_before_checkpoint_and_candidate_cleanup(
        self, tmp_path, monkeypatch
    ):
        """Central abandon is the terminal actor command, not just filesystem cleanup."""
        import evolution_core
        import evolution_infra
        from worker_workflow import (
            WorkerWorkflow,
            build_worker_envelope,
        )

        results_dir = tmp_path / "results"
        results_dir.mkdir()
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
        checkpoint = {
            "next_v": 100,
            "source_v": 99,
            "run_id": "100#0",
            "workflow_run_id": "generation:100:active-lease",
            "checkpoint_revision": 1,
            "generation_attempt": 0,
            "stage": "master_planned",
            "worker_failure_count": 0,
        }
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)

        candidate = tmp_path / "national_v100"
        candidate.mkdir()
        (candidate / "strategy.py").write_text("value = 1\n")
        workflow = WorkerWorkflow.for_checkpoint(checkpoint)
        snapshot_hash = workflow.artifacts.capture(candidate)
        envelope = build_worker_envelope(
            checkpoint=checkpoint,
            kind="initial_worker",
            source_stage="master_planned",
            prepared_artifact_hash=snapshot_hash,
            prepared_snapshot_hash=snapshot_hash,
            source_artifact_hash="a" * 64,
            tasks=[{
                "worker_id": "logic",
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "worker_prompt": "apply the accepted plan",
            }],
            reviewer_feedback="",
            worker_template_hash="b" * 64,
            worker_execution_context={"schema_version": 1},
            work_item={"kind": "initial_worker"},
            backend_contract={"model": "test"},
            precommit_rework_count=0,
            official_rework_count=0,
        )
        workflow.prepare(envelope)
        lease = workflow.request_or_claim(owner="stale-worker", lease_seconds=3600)
        assert workflow.store.effect(lease.effect_id)["status"] == "running"

        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *_args, **_kwargs: None)

        result = _run(tbm._do_abandon_generation(reason="cycle_timeout"))

        assert result["abandoned"] is True, result
        assert result["workflow_fenced"] is True
        assert result["workflow_run_id"] == workflow.run_id
        assert cleared == [True]
        assert not candidate.exists()
        assert workflow.state()["status"] == "abandoned"
        assert workflow.store.effect(lease.effect_id)["status"] == "abandoned"

        # The old activity still holds an in-memory lease, but its result cannot
        # append WorkerOutputReady or recreate the deleted canonical candidate.
        try:
            workflow.output_ready(
                lease,
                artifact_hash=snapshot_hash,
                snapshot_hash=snapshot_hash,
                projection={"schema_version": 1},
            )
        except RuntimeError as exc:
            assert "fenced lease" in str(exc)
        else:
            raise AssertionError("late Worker completion unexpectedly crossed abandon fence")
        assert workflow.state()["status"] == "abandoned"
        assert not candidate.exists()

    def test_frozen_worker_abandon_inside_actor_lock_does_not_relock(
        self, tmp_path, monkeypatch
    ):
        """Forced abandon reuses its caller's actor lock and still cleans up."""
        from contextlib import contextmanager
        import threading

        import evolution_core
        import evolution_infra
        import tool_planning
        from worker_workflow import WorkerWorkflow, build_worker_envelope
        from workflow_kernel import WorkflowStore

        results_dir = tmp_path / "results"
        results_dir.mkdir()
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
        checkpoint = {
            "next_v": 100,
            "source_v": 99,
            "run_id": "100#0",
            "workflow_run_id": "generation:100:nested-abandon",
            "checkpoint_revision": 4,
            "generation_attempt": 0,
            "stage": "rework_running",
            "worker_failure_count": 0,
        }
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)

        candidate = tmp_path / "national_v100"
        candidate.mkdir()
        (candidate / "strategy.py").write_text("value = 1\n")
        workflow = WorkerWorkflow.for_checkpoint(checkpoint)
        snapshot_hash = workflow.artifacts.capture(candidate)
        envelope = build_worker_envelope(
            checkpoint=checkpoint,
            kind="quality_repair",
            source_stage="rework_running",
            prepared_artifact_hash=snapshot_hash,
            prepared_snapshot_hash=snapshot_hash,
            source_artifact_hash="a" * 64,
            tasks=[{
                "worker_id": "repair",
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "worker_prompt": "repair the frozen blocker",
            }],
            reviewer_feedback="quality blocker",
            worker_template_hash="b" * 64,
            worker_execution_context={"schema_version": 1},
            work_item={"kind": "quality_repair"},
            backend_contract={"model": "test"},
            precommit_rework_count=0,
            official_rework_count=0,
        )
        workflow.prepare(envelope)
        lease = workflow.request_or_claim(owner="stale-worker", lease_seconds=3600)

        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *_args, **_kwargs: None)

        # Turn an accidental nested flock into an immediate assertion instead of
        # letting the regression hang the test process forever.
        original_command_lock = WorkflowStore.command_lock
        lock_depth = threading.local()
        nested_attempts = []

        @contextmanager
        def reject_nested_lock(self, run_id, *, blocking=False):
            depth = int(getattr(lock_depth, "value", 0))
            if depth:
                nested_attempts.append((run_id, blocking))
                raise AssertionError("nested generation actor flock")
            with original_command_lock(self, run_id, blocking=blocking):
                lock_depth.value = depth + 1
                try:
                    yield
                finally:
                    lock_depth.value = depth

        monkeypatch.setattr(WorkflowStore, "command_lock", reject_nested_lock)
        with workflow.store.command_lock(workflow.run_id):
            result = _run(tool_planning._force_abandon_frozen_worker_generation(
                100,
                99,
                "frozen_rework_pre_worker_drift",
                actor_lock_owned=True,
            ))

        assert nested_attempts == []
        assert result["abandoned"] is True, result
        assert result["workflow_fenced"] is True
        assert result["cleared_checkpoint"] is True
        assert cleared == [True]
        assert not candidate.exists()
        assert workflow.state()["status"] == "abandoned"
        assert workflow.store.effect(lease.effect_id)["status"] == "abandoned"

    def test_clears_checkpoint_and_removes_incomplete_dir(self, tmp_path, monkeypatch):
        # Active checkpoint -> clear it + remove the incomplete next dir.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "master_planned"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)

        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="master_exhausted (4 fails)"))

        assert result["abandoned"] is True
        assert result["cleared_checkpoint"] is True
        assert result["removed_directory"] == "national_v100"
        assert result["reason"] == "master_exhausted (4 fails)"
        assert cleared == [True]          # clear_pipeline_checkpoint called
        assert not next_dir.exists()      # incomplete dir removed

    def test_no_checkpoint_uses_authoritative_next_version_floor(self, tmp_path, monkeypatch):
        # No checkpoint -> cleared_checkpoint stays False, but an orphaned
        # incomplete authoritative next dir is still cleaned up. Abandoned
        # version floors mean this is not always current_v + 1.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: None)
        import evolution_core
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE",
                            tmp_path / "nonexistent.json")  # .exists() == False
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda **_kwargs: True)
        monkeypatch.setattr(tbm, "find_current_v", lambda: 99)
        monkeypatch.setattr(tbm, "find_max_committed_v", lambda: 99)
        monkeypatch.setattr(tbm, "find_abandoned_version_floor", lambda: 100)
        monkeypatch.setattr(
            tbm,
            "compute_next_generation_v",
            lambda current_v, max_committed_v, abandoned_floor: max(
                current_v, max_committed_v, abandoned_floor
            ) + 1,
        )

        next_dir = tmp_path / "national_v101"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="no_ckpt"))

        assert result["abandoned"] is True
        assert result["cleared_checkpoint"] is False
        assert result["removed_directory"] == "national_v101"
        assert result["abandoned_v"] == 101
        assert not next_dir.exists()

    def test_preserves_completed_dir(self, tmp_path, monkeypatch):
        # A completed generation dir (.completed sentinel) must NOT be removed.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "master_planned"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda **_kwargs: True)

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        (next_dir / ".completed").touch()  # COMPLETED — must be preserved
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="test"))

        assert result["abandoned"] is True
        assert result["removed_directory"] is None  # not removed (completed)
        assert next_dir.exists()                    # preserved

    def test_preserves_git_tracked_incomplete_dir(self, tmp_path, monkeypatch):
        # A git-tracked dir without a tag is a bare-commit recovery case, not
        # disposable scratch. abandon_generation may clear the checkpoint, but
        # must not rmtree committed code.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "master_planned"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda **_kwargs: True)

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: True)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="test"))

        assert result["abandoned"] is True
        assert result["removed_directory"] is None
        assert result["abandoned_v"] == 100
        assert next_dir.exists()

    def test_generic_abandon_refuses_forward_only_reviewed_stage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "reviewed"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)

        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)

        events = []
        monkeypatch.setattr(
            tbm,
            "log_system_event",
            lambda event_type, severity, message, data=None: events.append(
                (event_type, severity, message, data)
            ),
        )

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["blocked"] is True
        assert result["stage"] == "reviewed"
        assert result["next_tool"] == "run_critic"
        assert "run_critic" in result["directive"]
        assert cleared == []
        assert next_dir.exists()
        assert events[0][0] == "pipeline.abandon_refused_state_guard"

    def test_forced_abandon_still_allowed_after_reviewed_stage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint",
                            lambda: {"next_v": 100, "source_v": 99, "stage": "reviewed"})
        import evolution_core
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)

        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )

        next_dir = tmp_path / "national_v100"
        next_dir.mkdir()
        (next_dir / "main.py").write_text("x=1")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="cycle_timeout"))

        assert result["abandoned"] is True
        assert result["cleared_checkpoint"] is True
        assert result["removed_directory"] == "national_v100"
        assert cleared == [True]
        assert not next_dir.exists()
