"""Tests for _do_abandon_generation (B2 v125 fix helper).

Validates the shared abandon logic that MASTER_EXHAUSTED (tool_planning.py) and
CYCLE_TIMEOUT (orchestrator.py B3) now call directly instead of relying on the
orchestrator LLM to obey a plain-text directive.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

import tool_bot_management as tbm
import strict_authority_workflow as strict_authority

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


@pytest.fixture(autouse=True)
def _isolate_abandon_receipts(tmp_path, monkeypatch):
    import evolution_infra

    results = tmp_path / "abandon_receipt_results"
    results.mkdir()
    monkeypatch.setattr(tbm, "RESULTS_DIR", results)
    monkeypatch.setattr(tbm, "_is_autonomous_runtime_checkout", lambda: True)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 143)
    monkeypatch.setattr(
        tbm,
        "_evolution_git",
        lambda *args, **_kwargs: (
            "a" * 40 if args == ("rev-parse", "HEAD") else ""
        ),
    )
    monkeypatch.setattr(tbm, "git_has_publication_ref", lambda _version: False)
    monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)


def _strict_artifact(root, version, *, action="pass"):
    from bot_namespace import refresh_policy_identity_documents

    root.mkdir(parents=True)
    (root / "national_bot.py").write_text("def run():\n    return None\n", encoding="utf-8")
    (root / "policy.py").write_text(
        f"def decide(_context):\n    return {{'kind': '{action}'}}\n",
        encoding="utf-8",
    )
    (root / "precompute.py").write_text("TABLE = ()\n", encoding="utf-8")
    (root / "national_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "policy_epoch_receipt.json").write_text("{}\n", encoding="utf-8")
    refresh_policy_identity_documents(
        root,
        version,
        parent_versions=() if version == 143 else (version - 1,),
    )
    return root


def _resolve_published_parent(name, **_kwargs):
    version = int(str(name).rsplit("national_v", 1)[1])
    return SimpleNamespace(
        eligible=True,
        version=version,
        issues=(),
        runtime_manifest={"epoch": "national_tcp_policy_v1", "version": version},
        epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
        publication_identity={
            "published": True,
            "tag": f"national-bot-v{version}",
            "version": version,
        },
        certificate_digest="b" * 64,
    )


def _strict_checkpoint(next_v, source_v, stage, **extra):
    import checkpoint_schema

    audit_context = dict(extra.pop("audit_context", {}) or {})
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=next_v,
        source_v=source_v,
        parent2_v=extra.get("parent2_v"),
        audit_context=audit_context,
        published_high_water=extra.pop("published_high_water", next_v - 1),
        abandoned_receipt_floor=extra.pop("abandoned_receipt_floor", 0),
        abandoned_receipt_head_digest=extra.pop(
            "abandoned_receipt_head_digest", None
        ),
        parent_resolver=_resolve_published_parent,
    )
    return {
        "checkpoint_schema_version": checkpoint_schema.CHECKPOINT_SCHEMA_VERSION,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": binding,
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": extra.pop("parent2_v", None),
        "stage": stage,
        "workflow_run_id": f"generation:{next_v}:workflow-v1",
        "checkpoint_revision": 1,
        "audit_context": audit_context,
        **extra,
    }


def _run(coro):
    # A4 (2026-06-30): reset the abandon rate-limit before each test call so the
    # 60s cooldown doesn't block consecutive test abandons.
    tbm._LAST_ABANDON_TS[0] = 0.0
    tbm._LAST_ABANDON_TS[1] = ""
    return asyncio.new_event_loop().run_until_complete(coro)


def _persist_schema2_claim(checkpoint, reason="abandon_generation"):
    claim, candidate, transaction_dir = tbm._build_recorded_abandon_claim(
        checkpoint,
        reason=reason,
    )
    tbm._ensure_transaction_directory(transaction_dir)
    tbm._ensure_durable_json(transaction_dir / "claim.json", claim)
    tbm._ensure_durable_json(
        tbm.RESULTS_DIR / "policy_epoch_reconciliation_claim.json",
        claim,
    )
    return claim, candidate, transaction_dir


def _resign_schema2_claim(claim):
    from bot_artifact import canonical_digest

    unsigned = {key: value for key, value in claim.items() if key != "claim_digest"}
    return {**unsigned, "claim_digest": canonical_digest(unsigned)}


class TestDoAbandonGeneration:
    def test_corrupt_checkpoint_is_preserved_for_operator_reconcile(
        self, tmp_path, monkeypatch
    ):
        import evolution_core

        corrupt = tmp_path / "pipeline_state.json"
        corrupt.write_text("{not-json", encoding="utf-8")
        candidate = tmp_path / "national_v144"
        _strict_artifact(candidate, 144)
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", corrupt)
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: None)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["reason"] == "checkpoint_corrupt"
        assert result["action"] == "operator_reconcile"
        assert corrupt.read_text(encoding="utf-8") == "{not-json"
        assert candidate.exists()

    def test_expected_identity_refuses_stale_generation_before_fencing(
        self, tmp_path, monkeypatch
    ):
        import evolution_core

        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        current = _strict_checkpoint(
            145,
            144,
            "master_planned",
            workflow_run_id="generation:145:new",
            checkpoint_revision=1,
        )
        candidate = tmp_path / "national_v145"
        _strict_artifact(candidate, 145)
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: current)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )

        result = _run(tbm._do_abandon_generation(
            reason="worker_circuit_breaker",
            _bypass_rate_limit=True,
            expected_workflow_run_id="generation:144:old",
            expected_next_v=144,
            expected_source_v=143,
            expected_checkpoint_revision=7,
            expected_checkpoint_stage="master_planned",
        ))

        assert result["abandoned"] is False
        assert result["action"] == "stale_rejection_ignored"
        assert result["current_checkpoint"]["next_v"] == 145
        assert cleared == []
        assert candidate.exists()

    def test_expected_revision_is_rechecked_under_generation_actor_lock(
        self, tmp_path, monkeypatch
    ):
        import evolution_core
        import evolution_infra

        results_dir = tmp_path / "results"
        results_dir.mkdir()
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
        initial = _strict_checkpoint(
            144,
            143,
            "master_planned",
            workflow_run_id="generation:144:race",
            checkpoint_revision=1,
        )
        advanced = {**initial, "checkpoint_revision": 2}
        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        reads = iter((initial, advanced))
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: next(reads))
        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )
        monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)

        result = _run(tbm._do_abandon_generation(
            reason="worker_circuit_breaker",
            _bypass_rate_limit=True,
            expected_workflow_run_id=initial["workflow_run_id"],
            expected_next_v=144,
            expected_source_v=143,
            expected_checkpoint_revision=1,
            expected_checkpoint_stage="master_planned",
        ))

        assert result["abandoned"] is False
        assert result["action"] == "stale_rejection_ignored"
        assert result["current_checkpoint"]["checkpoint_revision"] == 2
        assert cleared == []

    def test_stage_guard_is_rechecked_after_generation_actor_lock(
        self, tmp_path, monkeypatch
    ):
        import evolution_core
        import evolution_infra

        results_dir = tmp_path / "results"
        results_dir.mkdir()
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
        initial = _strict_checkpoint(
            144,
            143,
            "master_planned",
            run_id="144#0",
            workflow_run_id="generation:144:guard-race",
            checkpoint_revision=1,
            generation_attempt=0,
        )
        advanced = {
            **initial,
            "checkpoint_revision": 2,
            "stage": "publishing",
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
        assert result["stage"] == "publishing"
        assert result["reason"] == "publication_or_certification_stage_not_disposable"
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
        checkpoint = _strict_checkpoint(
            144,
            143,
            "master_planned",
            run_id="144#0",
            workflow_run_id="generation:144:active-lease",
            checkpoint_revision=1,
            generation_attempt=0,
            worker_failure_count=0,
        )
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        monkeypatch.setattr(
            evolution_core,
            "read_pipeline_checkpoint",
            lambda: checkpoint,
        )
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)

        candidate = tmp_path / "national_v144"
        _strict_artifact(candidate, 144)
        workflow = WorkerWorkflow.for_checkpoint(checkpoint)
        strict_run_id = strict_authority.authority_run_id(
            checkpoint["workflow_run_id"]
        )
        workflow.store.ensure_instance(
            strict_run_id,
            definition_version=strict_authority.DEFINITION_VERSION,
        )
        strict_effect_id = "strict-llm-" + "9" * 64
        workflow.store.request_effect(
            run_id=strict_run_id,
            effect_id=strict_effect_id,
            kind=strict_authority.EFFECT_KIND,
            input_payload={"slot": "proposal:mechanism"},
            causation_id="strict-provider-started-before-abandon",
            max_attempts=1,
        )
        workflow.store.claim_effect(
            strict_effect_id,
            owner="stale-strict-provider",
            lease_seconds=3600,
        )
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
                "target_files": ["policy.py"],
                "worker_prompt": "apply the accepted plan",
            }],
            reviewer_feedback="",
            worker_template_hash="b" * 64,
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

        result = _run(tbm._do_abandon_generation(
            reason="worker_circuit_breaker",
            **tbm.expected_abandon_identity(checkpoint),
        ))

        assert result["abandoned"] is True, result
        assert result["workflow_fenced"] is True
        assert result["workflow_run_id"] == workflow.run_id
        assert cleared == [True]
        assert not candidate.exists()
        assert workflow.state()["status"] == "abandoned"
        assert workflow.store.effect(lease.effect_id)["status"] == "abandoned"
        assert workflow.store.instance(strict_run_id)["status"] == "abandoned"
        assert workflow.store.instance(strict_run_id)["fence_epoch"] == 1
        assert workflow.store.effect(strict_effect_id)["status"] == "abandoned"

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
        checkpoint = _strict_checkpoint(
            144,
            143,
            "rework_running",
            run_id="144#0",
            workflow_run_id="generation:144:nested-abandon",
            checkpoint_revision=4,
            generation_attempt=0,
            worker_failure_count=0,
        )
        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        monkeypatch.setattr(
            evolution_core,
            "read_pipeline_checkpoint",
            lambda: checkpoint,
        )
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)

        candidate = tmp_path / "national_v144"
        _strict_artifact(candidate, 144)
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
                "target_files": ["policy.py"],
                "worker_prompt": "repair the frozen blocker",
            }],
            reviewer_feedback="quality blocker",
            worker_template_hash="b" * 64,
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
                144,
                143,
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
        checkpoint = _strict_checkpoint(144, 143, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
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

        next_dir = tmp_path / "national_v144"
        _strict_artifact(next_dir, 144)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is True
        assert len(result["abandon_receipt_digest"]) == 64
        assert result["cleared_checkpoint"] is True
        assert result["removed_directory"] == "national_v144"
        assert result["reason"] == "abandon_generation"
        assert cleared == [True]          # clear_pipeline_checkpoint called
        assert not next_dir.exists()      # incomplete dir removed

    def test_no_checkpoint_preserves_unowned_candidate_debris(self, tmp_path, monkeypatch):
        # A directory name is not cleanup authority. Without the exact
        # checkpoint, preserve bytes instead of inferring a target/floor.
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: None)
        import evolution_core
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE",
                            tmp_path / "nonexistent.json")  # .exists() == False
        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda **_kwargs: True)
        next_dir = tmp_path / "national_v145"
        _strict_artifact(next_dir, 145)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["cleared_checkpoint"] is False
        assert result["reason"] == "no_checkpoint_cleanup_authority"
        assert next_dir.exists()
        assert not (tbm.RESULTS_DIR / "abandoned_versions.jsonl").exists()

    def test_preserves_completed_dir(self, tmp_path, monkeypatch):
        # A completed generation dir is publication evidence: preserve both it
        # and the checkpoint instead of pretending terminal cleanup succeeded.
        checkpoint = _strict_checkpoint(144, 143, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
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

        next_dir = tmp_path / "national_v144"
        _strict_artifact(next_dir, 144)
        (next_dir / ".completed").touch()  # COMPLETED — must be preserved
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["reason"] == "candidate_has_completed_sentinel"
        assert cleared == []
        assert next_dir.exists()

    def test_preserves_git_tracked_incomplete_dir(self, tmp_path, monkeypatch):
        # A git-tracked dir without a tag is a bare-commit recovery case, not
        # disposable scratch. Preserve both bytes and checkpoint for operator
        # reconciliation.
        checkpoint = _strict_checkpoint(144, 143, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
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

        next_dir = tmp_path / "national_v144"
        _strict_artifact(next_dir, 144)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: True)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["reason"] == "candidate_is_git_tracked"
        assert result["abandoned_v"] == 144
        assert cleared == []
        assert next_dir.exists()

    def test_candidate_is_deleted_before_checkpoint_cas_and_retry_is_idempotent(
        self,
        tmp_path,
        monkeypatch,
    ):
        checkpoint = _strict_checkpoint(144, 143, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        clear_results = iter((False, True))
        clear_observations = []

        next_dir = tmp_path / "national_v144"
        _strict_artifact(next_dir, 144)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)

        def clear(**_kwargs):
            clear_observations.append(next_dir.exists())
            return next(clear_results)

        monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)

        first = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert first["abandoned"] is False
        assert first["reason"] == "checkpoint_identity_conflict"
        assert first["removed_directory"] == "national_v144"
        assert clear_observations == [False]
        assert not next_dir.exists()

        real_reproof = tbm._fsync_regular_state_file_and_parent
        reproof_attempts = []

        def fail_first_ledger_reproof(path):
            reproof_attempts.append(path)
            if len(reproof_attempts) == 1:
                raise OSError("injected abandon ledger durability failure")
            return real_reproof(path)

        monkeypatch.setattr(
            tbm,
            "_fsync_regular_state_file_and_parent",
            fail_first_ledger_reproof,
        )
        second = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert second["abandoned"] is False
        assert "injected abandon ledger durability failure" in second["error"]
        assert clear_observations == [False]
        third = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert third["abandoned"] is True
        assert third["cleared_checkpoint"] is True
        assert len(reproof_attempts) == 2
        assert clear_observations == [False, False]
        assert not next_dir.exists()

    def test_partial_publication_ref_preserves_candidate_and_checkpoint(
        self,
        tmp_path,
        monkeypatch,
    ):
        checkpoint = _strict_checkpoint(144, 143, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        candidate = tmp_path / "national_v144"
        _strict_artifact(candidate, 144)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
        monkeypatch.setattr(tbm, "git_has_publication_ref", lambda _version: True)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)
        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["reason"] == "candidate_has_publication_ref"
        assert candidate.exists()
        assert cleared == []

    def test_quarantine_rename_failure_preserves_checkpoint_and_candidate(
        self,
        tmp_path,
        monkeypatch,
    ):
        checkpoint = _strict_checkpoint(144, 143, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        candidate = tmp_path / "national_v144"
        _strict_artifact(candidate, 144)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
        monkeypatch.setattr(tbm, "git_has_publication_ref", lambda _version: False)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)
        real_replace = tbm.os.replace

        def fail_candidate_rename(source, destination):
            if source == candidate:
                live_claim = (
                    tbm.RESULTS_DIR / "policy_epoch_reconciliation_claim.json"
                )
                assert live_claim.is_file()
                claim = json.loads(live_claim.read_text(encoding="utf-8"))
                archived_claim = (
                    tbm.RESULTS_DIR
                    / "policy_epoch_abandon_transactions"
                    / claim["transaction_id"]
                    / "claim.json"
                )
                assert archived_claim.is_file()
                assert json.loads(archived_claim.read_text(encoding="utf-8")) == claim
                raise OSError("injected quarantine rename failure")
            return real_replace(source, destination)

        monkeypatch.setattr(tbm.os, "replace", fail_candidate_rename)
        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert "injected quarantine rename failure" in result["error"]
        assert candidate.exists()
        assert cleared == []
        assert (tbm.RESULTS_DIR / "policy_epoch_reconciliation_claim.json").exists()

        (candidate / "policy.py").write_text(
            "# drift after durable claim\n",
            encoding="utf-8",
        )
        retry = _run(tbm._do_abandon_generation(reason="abandon_generation"))
        assert retry["abandoned"] is False
        assert retry["reason"] == "claimed_candidate_preimage_drifted"
        assert candidate.exists()
        assert cleared == []

    def test_claimed_candidate_requires_exactly_one_source_or_quarantine(
        self,
        tmp_path,
    ):
        source = tmp_path / "national_v144"
        _strict_artifact(source, 144)
        identity = tbm._candidate_tree_manifest(source)
        claim = {
            "candidate": {
                "present": True,
                "path": "bots/national_v144",
                **identity,
            }
        }
        quarantine = tmp_path / "transaction" / "candidate"
        quarantine.parent.mkdir()
        _strict_artifact(quarantine, 144)

        with pytest.raises(
            RuntimeError,
            match="candidate_exists_at_source_and_quarantine",
        ):
            tbm._validate_claim_candidate_state(claim, source, quarantine)

        for child in sorted(source.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        source.rmdir()
        for child in sorted(quarantine.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        quarantine.rmdir()

        with pytest.raises(RuntimeError, match="claimed_candidate_disappeared"):
            tbm._validate_claim_candidate_state(claim, source, quarantine)

    def test_quarantine_symlink_swap_does_not_follow_outside_tree_or_clear_checkpoint(
        self,
        tmp_path,
        monkeypatch,
    ):
        checkpoint = _strict_checkpoint(144, 143, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        candidate = tmp_path / "national_v144"
        _strict_artifact(candidate, 144)
        displaced = tmp_path / "displaced-national_v144"
        outside = tmp_path / "outside"
        outside.mkdir()
        marker = outside / "must-survive"
        marker.write_text("safe", encoding="utf-8")
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
        monkeypatch.setattr(tbm, "git_has_publication_ref", lambda _version: False)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)
        real_replace = tbm.os.replace

        def swap_then_replace(source, destination):
            if source == candidate:
                source.rename(displaced)
                source.symlink_to(outside, target_is_directory=True)
            return real_replace(source, destination)

        monkeypatch.setattr(tbm.os, "replace", swap_then_replace)
        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert "candidate_not_single_link_directory" in result["error"]
        assert marker.read_text(encoding="utf-8") == "safe"
        assert not candidate.exists()
        assert displaced.is_dir()
        assert cleared == []

    def test_parent_fsync_failure_requires_absent_candidate_reproof_before_clear(
        self,
        tmp_path,
        monkeypatch,
    ):
        checkpoint = _strict_checkpoint(144, 143, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        candidate = tmp_path / "national_v144"
        _strict_artifact(candidate, 144)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
        monkeypatch.setattr(tbm, "git_has_publication_ref", lambda _version: False)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)
        candidate_fsync_attempts = []

        real_fsync_parent = tbm._fsync_parent_directory

        def fsync_parent(path):
            if path == candidate and not candidate.exists():
                candidate_fsync_attempts.append(True)
                if len(candidate_fsync_attempts) == 1:
                    raise OSError("injected parent fsync failure")
            return real_fsync_parent(path)

        monkeypatch.setattr(tbm, "_fsync_parent_directory", fsync_parent)
        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )

        first = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert first["abandoned"] is False
        assert "injected parent fsync failure" in first["error"]
        assert not candidate.exists()
        assert cleared == []

        second = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert second["abandoned"] is True
        assert second["cleared_checkpoint"] is True
        assert candidate_fsync_attempts == [True, True]
        assert cleared == [True]

    def test_generic_abandon_refuses_forward_only_reviewed_stage(self, tmp_path, monkeypatch):
        checkpoint = _strict_checkpoint(144, 143, "reviewed")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
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

        next_dir = tmp_path / "national_v144"
        _strict_artifact(next_dir, 144)
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

    def test_forced_abandon_cannot_bypass_reviewed_stage(self, tmp_path, monkeypatch):
        checkpoint = _strict_checkpoint(144, 143, "reviewed")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
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

        next_dir = tmp_path / "national_v144"
        _strict_artifact(next_dir, 144)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(
            reason="worker_circuit_breaker",
            **tbm.expected_abandon_identity(checkpoint),
        ))

        assert result["abandoned"] is False
        assert result["reason"] == "forced_abandon_reason_stage_not_allowed"
        assert cleared == []
        assert next_dir.exists()

    def test_strict_authority_terminal_failure_is_disposable_only_during_master(self):
        reason = (
            "system_strict_authority_invalid:"
            "strict_authority_schema_retry_exhausted:proposal:counterfactual"
        )

        master_checkpoint = _strict_checkpoint(144, 143, "direction_audited")
        assert tbm._generic_abandon_stage_block(master_checkpoint, reason) is None

        reviewed_checkpoint = _strict_checkpoint(144, 143, "reviewed")
        blocked = tbm._generic_abandon_stage_block(reviewed_checkpoint, reason)
        assert blocked["blocked"] is True
        assert blocked["reason"] == "forced_abandon_reason_stage_not_allowed"
        assert blocked["stage"] == "reviewed"

    def test_strict_authority_terminal_failure_canonically_abandons_master(
        self,
        tmp_path,
        monkeypatch,
    ):
        import evolution_core
        import evolution_infra

        results_dir = tmp_path / "results"
        results_dir.mkdir()
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
        checkpoint = _strict_checkpoint(
            144,
            143,
            "direction_audited",
            run_id="144#0",
            workflow_run_id="generation:144:strict-terminal",
            checkpoint_revision=5,
            audit_attempt=1,
        )
        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        monkeypatch.setattr(
            evolution_core,
            "read_pipeline_checkpoint",
            lambda: checkpoint,
        )
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)

        candidate = _strict_artifact(tmp_path / "national_v144", 144)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *_args, **_kwargs: None)
        cleared = []
        monkeypatch.setattr(
            tbm,
            "clear_pipeline_checkpoint",
            lambda **_kwargs: cleared.append(True) or True,
        )
        reason = (
            "system_strict_authority_invalid:"
            "strict_authority_schema_retry_exhausted:proposal:counterfactual"
        )

        result = _run(tbm._do_abandon_generation(
            reason=reason,
            _bypass_rate_limit=True,
            **tbm.expected_abandon_identity(checkpoint),
        ))

        assert result["abandoned"] is True, result
        assert result["workflow_fenced"] is True
        assert result["workflow_run_id"] == checkpoint["workflow_run_id"]
        assert result["cleared_checkpoint"] is True
        assert cleared == [True]
        assert not candidate.exists()


def _schema2_claim_fixture(tmp_path, monkeypatch, *, checkpoint=None):
    import evolution_core
    import evolution_infra

    checkpoint = checkpoint or _strict_checkpoint(144, 143, "master_planned")
    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = tmp_path / "national_v144"
    _strict_artifact(candidate, 144)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tbm.RESULTS_DIR)
    monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)
    claim, _candidate_path, transaction_dir = _persist_schema2_claim(checkpoint)
    return checkpoint, state_file, candidate, claim, transaction_dir


def test_schema2_resigned_forgery_extra_key_and_path_traversal_fail_closed(
    tmp_path,
    monkeypatch,
):
    from epoch_authority import (
        schema2_abandon_transaction_preimage,
        validate_schema2_abandon_claim_structure,
    )
    from bot_artifact import canonical_digest

    checkpoint, state_file, candidate, claim, _transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )
    ledger = tbm.RESULTS_DIR / "abandoned_versions.jsonl"

    forged_tx = json.loads(json.dumps(claim))
    forged_tx["transaction_id"] = "f" * 64
    forged_tx = _resign_schema2_claim(forged_tx)
    with pytest.raises(RuntimeError, match="transaction_id_invalid"):
        validate_schema2_abandon_claim_structure(forged_tx)

    extra = {**claim, "forged_phase": "checkpoint_cleared"}
    extra = _resign_schema2_claim(extra)
    with pytest.raises(RuntimeError, match="fields_invalid"):
        validate_schema2_abandon_claim_structure(extra)

    traversal = json.loads(json.dumps(claim))
    traversal["candidate"]["path"] = "bots/../outside"
    traversal["transaction_id"] = canonical_digest(
        schema2_abandon_transaction_preimage(traversal)
    )
    traversal = _resign_schema2_claim(traversal)
    with pytest.raises(RuntimeError, match="candidate_identity_invalid"):
        validate_schema2_abandon_claim_structure(traversal)

    quarantine_traversal = json.loads(json.dumps(claim))
    quarantine_traversal["quarantine"]["leaf"] = "../candidate"
    quarantine_traversal["transaction_id"] = canonical_digest(
        schema2_abandon_transaction_preimage(quarantine_traversal)
    )
    quarantine_traversal = _resign_schema2_claim(quarantine_traversal)
    with pytest.raises(RuntimeError, match="quarantine_contract_invalid"):
        validate_schema2_abandon_claim_structure(quarantine_traversal)

    # Put a fully re-signed forgery at both durable locations: cleanup still
    # stops before ledger append, candidate mutation, or checkpoint CAS.
    live = tbm.RESULTS_DIR / "policy_epoch_reconciliation_claim.json"
    live.write_text(json.dumps(forged_tx) + "\n", encoding="utf-8")
    result = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    assert result["abandoned"] is False
    assert result["action"] == "operator_reconcile"
    assert state_file.exists()
    assert candidate.exists()
    assert not ledger.exists()
    assert checkpoint["next_v"] == 144


def test_schema2_active_claim_rejects_old_head_and_hardlinked_preimage(
    tmp_path,
    monkeypatch,
):
    _checkpoint, state_file, candidate, _claim, _transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )
    monkeypatch.setattr(
        tbm,
        "_evolution_git",
        lambda *args, **_kwargs: (
            "b" * 40 if args == ("rev-parse", "HEAD") else ""
        ),
    )
    old_head = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    assert old_head["abandoned"] is False
    assert old_head["reason"] == "recorded_abandon_active_git_state_changed"
    assert state_file.exists() and candidate.exists()

    monkeypatch.setattr(
        tbm,
        "_evolution_git",
        lambda *args, **_kwargs: (
            "a" * 40 if args == ("rev-parse", "HEAD") else ""
        ),
    )
    outside_link = tmp_path / "policy-hardlink"
    os.link(candidate / "policy.py", outside_link)
    hardlink = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    assert hardlink["abandoned"] is False
    assert "candidate_hardlink_entry" in hardlink["error"]
    assert state_file.exists() and candidate.exists()


def test_epoch_status_revalidates_schema2_live_git_and_filesystem_state(
    tmp_path,
    monkeypatch,
):
    import epoch_authority
    import evolution_infra

    checkpoint, state_file, candidate, _claim, _transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )
    live = tbm.RESULTS_DIR / "policy_epoch_reconciliation_claim.json"

    def fake_infra(head):
        return SimpleNamespace(
            PROJECT_ROOT=tbm.PROJECT_ROOT,
            PIPELINE_STATE_FILE=state_file,
            _git=lambda *args, **_kwargs: (
                head if args == ("rev-parse", "HEAD") else ""
            ),
            git_dir_is_committed=lambda _version: False,
            git_has_publication_ref=lambda _version: False,
            load_abandoned_version_receipts=(
                evolution_infra.load_abandoned_version_receipts
            ),
            read_pipeline_checkpoint=lambda: checkpoint,
        )

    valid = epoch_authority._runtime_reconciliation_claim_status(
        live,
        results_dir=tbm.RESULTS_DIR,
        bots_dir=candidate.parent,
        infra=fake_infra("a" * 40),
    )
    assert valid["valid"] is True
    assert valid["kind"] == "recorded_abandon_finalize"

    old_head = epoch_authority._runtime_reconciliation_claim_status(
        live,
        results_dir=tbm.RESULTS_DIR,
        bots_dir=candidate.parent,
        infra=fake_infra("b" * 40),
    )
    assert old_head["claimed"] is True
    assert old_head["valid"] is False
    assert any("active_git_state_changed" in issue for issue in old_head["issues"])


def test_schema2_checkpoint_clear_never_adopts_reappearing_source_candidate(
    tmp_path,
    monkeypatch,
):
    checkpoint, state_file, candidate, _claim, transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )
    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda **_kwargs: False)

    first = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    assert first["abandoned"] is False
    assert first["reason"] == "checkpoint_identity_conflict"
    quarantine = transaction_dir / "candidate"
    assert quarantine.is_dir() and not candidate.exists()

    # Model the crash window after checkpoint unlink, then move even the exact
    # old bytes back to the source label.  Phase authority, not byte equality,
    # controls recovery after the checkpoint has gone.
    state_file.unlink()
    quarantine.rename(candidate)
    monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: None)
    retry = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    assert retry["abandoned"] is False
    assert retry["reason"] == "recorded_abandon_source_invalid_after_checkpoint_clear"
    assert candidate.is_dir()
    rows = __import__("evolution_infra").load_abandoned_version_receipts(
        path=tbm.RESULTS_DIR / "abandoned_versions.jsonl",
        project_root=tbm.PROJECT_ROOT,
    )
    assert len(rows) == 1
    assert rows[0]["workflow_run_id"] == checkpoint["workflow_run_id"]


@pytest.mark.parametrize(
    ("drift_kind", "expected_reason"),
    (
        ("candidate", "claimed_candidate_preimage_drifted"),
        ("head", "recorded_abandon_active_git_state_changed"),
    ),
)
def test_schema2_revalidates_after_live_claim_before_irreversible_ledger_append(
    tmp_path,
    monkeypatch,
    drift_kind,
    expected_reason,
):
    import evolution_core

    checkpoint = _strict_checkpoint(144, 143, "master_planned")
    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = tmp_path / "national_v144"
    _strict_artifact(candidate, 144)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)
    head = ["a" * 40]
    monkeypatch.setattr(
        tbm,
        "_evolution_git",
        lambda *args, **_kwargs: (
            head[0] if args == ("rev-parse", "HEAD") else ""
        ),
    )
    clear_calls = []
    monkeypatch.setattr(
        tbm,
        "clear_pipeline_checkpoint",
        lambda **_kwargs: clear_calls.append(True) or True,
    )
    real_ensure = tbm._ensure_durable_json
    injected = []

    def inject_after_live_claim(path, payload):
        real_ensure(path, payload)
        if (
            path
            == tbm.RESULTS_DIR / "policy_epoch_reconciliation_claim.json"
            and not injected
        ):
            injected.append(True)
            if drift_kind == "candidate":
                (candidate / "policy.py").write_text(
                    "# same-call drift after LIVE claim\n",
                    encoding="utf-8",
                )
            else:
                head[0] = "b" * 40

    monkeypatch.setattr(tbm, "_ensure_durable_json", inject_after_live_claim)

    result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

    assert injected == [True]
    assert result["abandoned"] is False
    assert result["reason"] == expected_reason
    assert not (tbm.RESULTS_DIR / "abandoned_versions.jsonl").exists()
    assert candidate.is_dir()
    assert state_file.exists()
    assert clear_calls == []


def test_schema2_completed_receipt_survives_later_head_and_ledger_append(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import evolution_infra
    from epoch_authority import validate_schema2_abandon_finalize_receipt

    checkpoint, state_file, candidate, claim, transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )

    def clear(**_kwargs):
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    completed = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    assert completed["abandoned"] is True
    finalize = json.loads(
        (transaction_dir / "receipt.json").read_text(encoding="utf-8")
    )
    first_rows = evolution_infra.load_abandoned_version_receipts(
        path=tbm.RESULTS_DIR / "abandoned_versions.jsonl",
        project_root=tbm.PROJECT_ROOT,
    )
    first = first_rows[-1]
    later_checkpoint = _strict_checkpoint(
        145,
        143,
        "master_planned",
        published_high_water=143,
        abandoned_receipt_floor=144,
        abandoned_receipt_head_digest=first["receipt_digest"],
    )
    evolution_infra.append_abandoned_version_receipt(
        later_checkpoint,
        reason="later-legitimate-abandon",
        timestamp=99.0,
        path=tbm.RESULTS_DIR / "abandoned_versions.jsonl",
        project_root=tbm.PROJECT_ROOT,
    )
    rows = evolution_infra.load_abandoned_version_receipts(
        path=tbm.RESULTS_DIR / "abandoned_versions.jsonl",
        project_root=tbm.PROJECT_ROOT,
    )
    with pytest.raises(RuntimeError, match="active_ledger_advanced"):
        tbm.validate_completed_abandon_handoff(checkpoint, completed)
    monkeypatch.setattr(
        tbm,
        "_evolution_git",
        lambda *args, **_kwargs: (
            "c" * 40 if args == ("rev-parse", "HEAD") else ""
        ),
    )
    assert validate_schema2_abandon_finalize_receipt(
        claim,
        finalize,
        rows,
    ) == finalize
    assert len(rows) == 2
    assert not candidate.exists()


def test_timed_out_checkpoint_completes_real_schema2_abandon_transaction(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    timed_out = _strict_checkpoint(144, 143, "timed_out")
    checkpoint, state_file, candidate, claim, transaction_dir = (
        _schema2_claim_fixture(
            tmp_path,
            monkeypatch,
            checkpoint=timed_out,
        )
    )

    def clear(**expected):
        assert expected == {
            "expected_workflow_run_id": checkpoint["workflow_run_id"],
            "expected_next_v": checkpoint["next_v"],
            "expected_source_v": checkpoint["source_v"],
            "expected_checkpoint_revision": checkpoint["checkpoint_revision"],
            "expected_checkpoint_stage": "timed_out",
        }
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)

    result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

    assert result["abandoned"] is True
    assert result["workflow_run_id"] == checkpoint["workflow_run_id"]
    assert result["abandon_checkpoint_identity"] == claim["checkpoint"]
    assert not state_file.exists()
    assert not candidate.exists()
    assert (transaction_dir / "receipt.json").is_file()
    rows = evolution_infra.load_abandoned_version_receipts(
        path=tbm.RESULTS_DIR / "abandoned_versions.jsonl",
        project_root=tbm.PROJECT_ROOT,
    )
    assert rows[-1]["checkpoint_stage"] == "timed_out"
    assert rows[-1]["workflow_run_id"] == checkpoint["workflow_run_id"]


def test_completed_abandon_handoff_reproves_exact_live_terminal_result(
    tmp_path,
    monkeypatch,
):
    checkpoint, state_file, candidate, claim, transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )

    def clear(**_kwargs):
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

    assert result["abandoned"] is True
    assert result["abandon_transaction_id"] == claim["transaction_id"]
    assert result["abandon_checkpoint_identity"] == claim["checkpoint"]
    assert result["finalize_receipt_digest"] == json.loads(
        (transaction_dir / "receipt.json").read_text(encoding="utf-8")
    )["receipt_digest"]
    proof = tbm.validate_completed_abandon_handoff(checkpoint, result)
    assert proof["transaction_id"] == claim["transaction_id"]
    assert proof["abandon_receipt_digest"] == result["abandon_receipt_digest"]
    assert proof["finalize_receipt_digest"] == result["finalize_receipt_digest"]
    assert proof["checkpoint_identity"] == claim["checkpoint"]
    assert proof["workflow_fences"]["worker"]["fence_epoch"] >= 1
    assert (
        proof["workflow_fences"]["strict_authority"]["fence_epoch"] >= 1
    )
    assert not candidate.exists()

    forged_result = {
        **result,
        "finalize_receipt_digest": "f" * 64,
    }
    with pytest.raises(
        RuntimeError,
        match="completed_abandon_result_finalize_receipt_digest_mismatch",
    ):
        tbm.validate_completed_abandon_handoff(checkpoint, forged_result)

    wrong_baseline = {
        **checkpoint,
        "workflow_run_id": "generation:144:foreign-workflow",
    }
    with pytest.raises(
        RuntimeError,
        match="completed_abandon_checkpoint_identity_mismatch",
    ):
        tbm.validate_completed_abandon_handoff(wrong_baseline, result)

    newer_baseline = {**checkpoint, "checkpoint_revision": 2}
    with pytest.raises(
        RuntimeError,
        match="completed_abandon_checkpoint_revision_invalid",
    ):
        tbm.validate_completed_abandon_handoff(newer_baseline, result)

    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    with pytest.raises(
        RuntimeError,
        match="completed_abandon_terminal_paths_still_live",
    ):
        tbm.validate_completed_abandon_handoff(checkpoint, result)
    state_file.unlink()

    live_claim = tbm.RESULTS_DIR / "policy_epoch_reconciliation_claim.json"
    live_claim.write_text(json.dumps(claim), encoding="utf-8")
    with pytest.raises(
        RuntimeError,
        match="completed_abandon_terminal_paths_still_live",
    ):
        tbm.validate_completed_abandon_handoff(checkpoint, result)


def test_completed_abandon_handoff_allows_monotonic_terminal_revision(
    tmp_path,
    monkeypatch,
):
    terminal_checkpoint = _strict_checkpoint(
        144,
        143,
        "master_planned",
        checkpoint_revision=2,
    )
    checkpoint, state_file, _candidate, claim, _transaction_dir = (
        _schema2_claim_fixture(
            tmp_path,
            monkeypatch,
            checkpoint=terminal_checkpoint,
        )
    )

    def clear(**_kwargs):
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    result = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    baseline = {**checkpoint, "checkpoint_revision": 1}

    proof = tbm.validate_completed_abandon_handoff(baseline, result)

    assert proof["checkpoint_identity"] == claim["checkpoint"]
    assert proof["checkpoint_identity"]["checkpoint_revision"] == 2


@pytest.mark.parametrize(
    ("run_suffix", "event_type"),
    (
        ("", "WorkerAbandoned"),
        (f":{strict_authority.RUN_SUFFIX}", "StrictAuthorityAbandoned"),
    ),
)
def test_completed_abandon_handoff_rejects_unfenced_workflow_journal(
    tmp_path,
    monkeypatch,
    run_suffix,
    event_type,
):
    import sqlite3

    checkpoint, state_file, _candidate, _claim, _transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )

    def clear(**_kwargs):
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    result = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    assert result["abandoned"] is True

    database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE workflow_instances SET status = 'running', fence_epoch = 0 "
            "WHERE run_id = ?",
            (checkpoint["workflow_run_id"] + run_suffix,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        RuntimeError,
        match=f"completed_abandon_{event_type}_terminal_invalid",
    ):
        tbm.validate_completed_abandon_handoff(checkpoint, result)


def test_completed_abandon_handoff_rejects_workflow_event_payload_drift(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    checkpoint, state_file, _candidate, _claim, _transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )

    def clear(**_kwargs):
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    result = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    assert result["abandoned"] is True

    database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE workflow_events SET payload = ? "
            "WHERE run_id = ? AND event_type = 'WorkerAbandoned'",
            (
                json.dumps({
                    "reason": "abandon_generation",
                    "forged": True,
                }),
                checkpoint["workflow_run_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        RuntimeError,
        match="completed_abandon_WorkerAbandoned_history_digest_invalid",
    ):
        tbm.validate_completed_abandon_handoff(checkpoint, result)


def test_completed_abandon_handoff_rejects_workflow_event_sequence_gap(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    checkpoint, state_file, _candidate, _claim, _transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )

    def clear(**_kwargs):
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    result = _run(tbm._do_abandon_generation(reason="abandon_generation"))
    assert result["abandoned"] is True

    database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "UPDATE workflow_events SET seq = 2 WHERE run_id = ?",
            (checkpoint["workflow_run_id"],),
        )
        connection.execute(
            "UPDATE workflow_instances SET stream_version = 2 WHERE run_id = ?",
            (checkpoint["workflow_run_id"],),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        RuntimeError,
        match="completed_abandon_WorkerAbandoned_history_sequence_invalid",
    ):
        tbm.validate_completed_abandon_handoff(checkpoint, result)


def test_abandon_json_reader_rejects_same_inode_same_size_rewrite(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "claim.json"
    original = '{"value":1}\n'
    replacement = '{"value":2}\n'
    assert len(original) == len(replacement)
    path.write_text(original, encoding="utf-8")
    real_read = tbm.os.read
    injected = []

    def rewrite_after_read(descriptor, amount):
        raw = real_read(descriptor, amount)
        if not injected:
            injected.append(True)
            path.write_text(replacement, encoding="utf-8")
        return raw

    monkeypatch.setattr(tbm.os, "read", rewrite_after_read)
    with pytest.raises(RuntimeError, match="abandon_transaction_json_unsafe"):
        tbm._read_json_regular(path)
    assert injected == [True]
