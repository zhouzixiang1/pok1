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
from bot_namespace import (
    EVOLUTION_BRANCH,
    bot_name,
    bot_tag,
    high_water_tag,
)
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")

# Branch-portable strict-policy versions. The historical main-branch literals
# (143/142/national_v143) are replaced by these so the suite resolves to the
# authoritative cloud-line versions (1/0/national_cloud_v1) on this branch.
# T = the strict target version (the published parent); T+1 = its candidate.
T = STRICT_TARGET_V


@pytest.fixture(autouse=True)
def _isolate_abandon_receipts(tmp_path, monkeypatch):
    import evolution_infra

    results = tmp_path / "abandon_receipt_results"
    results.mkdir()
    monkeypatch.setattr(tbm, "RESULTS_DIR", results)
    monkeypatch.setattr(tbm, "_is_autonomous_runtime_checkout", lambda: True)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: T)
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
        parent_versions=() if version == T else (version - 1,),
    )
    return root


def _resolve_published_parent(name, **_kwargs):
    from bot_namespace import parse_bot_version, bot_tag

    version = parse_bot_version(name)
    if version is None:
        raise IndexError(f"unresolvable bot name: {name!r}")
    return SimpleNamespace(
        eligible=True,
        version=version,
        issues=(),
        runtime_manifest={"epoch": "national_tcp_policy_v1", "version": version},
        epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
        publication_identity={
            "published": True,
            "tag": bot_tag(version),
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


def _branch_reset_receipt():
    """Branch-portable policy-epoch reset receipt.

    ``tests.test_checkpoint_epoch_recovery._policy_epoch_reset_receipt`` is
    written for the main branch (high-water 142 / first-target 143 /
    ``national_v143``).  The live reset-receipt validator rejects any field
    that does not match this branch's authoritative values, so rewrite the
    version/namespace fields and recompute the nested canonical digests.
    """

    import hashlib
    from bot_namespace import (
        ARCHIVED_VERSION_HIGH_WATER,
        FIRST_STRICT_POLICY_VERSION,
    )
    from tests.test_checkpoint_epoch_recovery import _policy_epoch_reset_receipt

    def _digest(payload):
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    receipt = dict(_policy_epoch_reset_receipt())
    claim = dict(receipt.pop("_test_claim"))
    claim_payload = {
        key: value for key, value in claim.items() if key != "claim_digest"
    }
    claim_payload["first_target_version"] = FIRST_STRICT_POLICY_VERSION
    claim = {**claim_payload, "claim_digest": _digest(claim_payload)}
    receipt.pop("receipt_digest", None)
    receipt["archived_version_high_water"] = ARCHIVED_VERSION_HIGH_WATER
    receipt["version_authority_high_water"] = ARCHIVED_VERSION_HIGH_WATER
    receipt["first_target_version"] = FIRST_STRICT_POLICY_VERSION
    receipt["active_namespace"] = {
        **receipt["active_namespace"],
        "bot": bot_name(FIRST_STRICT_POLICY_VERSION),
    }
    receipt["execution_scope"] = {
        **receipt["execution_scope"],
        "claim_digest": claim["claim_digest"],
    }
    receipt = {**receipt, "receipt_digest": _digest(receipt)}
    receipt["_test_claim"] = claim
    return receipt



class TestDoAbandonGeneration:
    def test_corrupt_checkpoint_is_preserved_for_operator_reconcile(
        self, tmp_path, monkeypatch
    ):
        import evolution_core

        corrupt = tmp_path / "pipeline_state.json"
        corrupt.write_text("{not-json", encoding="utf-8")
        candidate = tmp_path / bot_name(T + 1)
        _strict_artifact(candidate, T + 1)
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
            T + 2,
            T + 1,
            "master_planned",
            workflow_run_id=f"generation:{T + 2}:new",
            checkpoint_revision=1,
        )
        candidate = tmp_path / bot_name(T + 2)
        _strict_artifact(candidate, T + 2)
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
            expected_workflow_run_id=f"generation:{T + 1}:old",
            expected_next_v=T + 1,
            expected_source_v=T,
            expected_checkpoint_revision=7,
            expected_checkpoint_stage="master_planned",
        ))

        assert result["abandoned"] is False
        assert result["action"] == "stale_rejection_ignored"
        assert result["current_checkpoint"]["next_v"] == T + 2
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
            T + 1,
            T,
            "master_planned",
            workflow_run_id=f"generation:{T + 1}:race",
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
            expected_next_v=T + 1,
            expected_source_v=T,
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
            T + 1,
            T,
            "master_planned",
            run_id=f"{T + 1}#0",
            workflow_run_id=f"generation:{T + 1}:guard-race",
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
            T + 1,
            T,
            "master_planned",
            run_id=f"{T + 1}#0",
            workflow_run_id=f"generation:{T + 1}:active-lease",
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

        candidate = tmp_path / bot_name(T + 1)
        _strict_artifact(candidate, T + 1)
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
            T + 1,
            T,
            "rework_running",
            run_id=f"{T + 1}#0",
            workflow_run_id=f"generation:{T + 1}:nested-abandon",
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

        candidate = tmp_path / bot_name(T + 1)
        _strict_artifact(candidate, T + 1)
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
                T + 1,
                T,
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
        checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
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

        next_dir = tmp_path / bot_name(T + 1)
        _strict_artifact(next_dir, T + 1)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: False)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is True
        assert len(result["abandon_receipt_digest"]) == 64
        assert result["cleared_checkpoint"] is True
        assert result["removed_directory"] == bot_name(T + 1)
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
        next_dir = tmp_path / bot_name(T + 2)
        _strict_artifact(next_dir, T + 2)
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
        checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
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

        next_dir = tmp_path / bot_name(T + 1)
        _strict_artifact(next_dir, T + 1)
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
        checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
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

        next_dir = tmp_path / bot_name(T + 1)
        _strict_artifact(next_dir, T + 1)
        monkeypatch.setattr(tbm, "get_bot_dir", lambda v: next_dir)
        monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: True)
        monkeypatch.setattr(tbm, "log_system_event", lambda *a, **k: None)

        result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

        assert result["abandoned"] is False
        assert result["reason"] == "candidate_is_git_tracked"
        assert result["abandoned_v"] == T + 1
        assert cleared == []
        assert next_dir.exists()

    def test_candidate_is_deleted_before_checkpoint_cas_and_retry_is_idempotent(
        self,
        tmp_path,
        monkeypatch,
    ):
        checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        fake_state = tmp_path / "pipeline_state.json"
        fake_state.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
        clear_results = iter((False, True))
        clear_observations = []

        next_dir = tmp_path / bot_name(T + 1)
        _strict_artifact(next_dir, T + 1)
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
        assert first["removed_directory"] == bot_name(T + 1)
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
        checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        candidate = tmp_path / bot_name(T + 1)
        _strict_artifact(candidate, T + 1)
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
        checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        candidate = tmp_path / bot_name(T + 1)
        _strict_artifact(candidate, T + 1)
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
        source = tmp_path / bot_name(T + 1)
        _strict_artifact(source, T + 1)
        identity = tbm._candidate_tree_manifest(source)
        claim = {
            "candidate": {
                "present": True,
                "path": f"bots/{bot_name(T + 1)}",
                **identity,
            }
        }
        quarantine = tmp_path / "transaction" / "candidate"
        quarantine.parent.mkdir()
        _strict_artifact(quarantine, T + 1)

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
        checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        candidate = tmp_path / bot_name(T + 1)
        _strict_artifact(candidate, T + 1)
        displaced = tmp_path / f"displaced-{bot_name(T + 1)}"
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
        checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
        monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
        import evolution_core

        state_file = tmp_path / "pipeline_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
        candidate = tmp_path / bot_name(T + 1)
        _strict_artifact(candidate, T + 1)
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
        checkpoint = _strict_checkpoint(T + 1, T, "reviewed")
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

        next_dir = tmp_path / bot_name(T + 1)
        _strict_artifact(next_dir, T + 1)
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
        checkpoint = _strict_checkpoint(T + 1, T, "reviewed")
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

        next_dir = tmp_path / bot_name(T + 1)
        _strict_artifact(next_dir, T + 1)
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

        master_checkpoint = _strict_checkpoint(T + 1, T, "direction_audited")
        assert tbm._generic_abandon_stage_block(master_checkpoint, reason) is None

        reviewed_checkpoint = _strict_checkpoint(T + 1, T, "reviewed")
        blocked = tbm._generic_abandon_stage_block(reviewed_checkpoint, reason)
        assert blocked["blocked"] is True
        assert blocked["reason"] == "forced_abandon_reason_stage_not_allowed"
        assert blocked["stage"] == "reviewed"

    def test_crossover_forced_abandon_is_disposable_at_selected_and_running(self):
        # Bug A (v160): crossover synthesis runs at stage=selected (it advances
        # to crossover_running only on success). crossover_llm_exhausted must
        # be disposable at selected, otherwise exhausted raw-TCP smoke retries
        # deadlock re-entry.
        reason = f"crossover_llm_exhausted:v{T}xv{T + 13}"
        for stage in ("selected", "crossover_running", "preparing", "prepared"):
            ck = _strict_checkpoint(T + 17, T, stage, parent2_v=T + 13)
            assert tbm._generic_abandon_stage_block(ck, reason) is None, stage
        # Non-crossover stages still reject the prefix.
        blocked = tbm._generic_abandon_stage_block(
            _strict_checkpoint(T + 17, T, "direction_audited", parent2_v=T + 13),
            reason,
        )
        assert blocked["blocked"] is True
        assert blocked["reason"] == "forced_abandon_reason_stage_not_allowed"

    @pytest.mark.parametrize("reason", (
        "system_strict_bootstrap_master_receipt_invalid:"
        "system_bootstrap_proposal_contract_digest_mismatch",
        "system_strict_bootstrap_master_receipt_error:"
        "RuntimeError:receipt_projection_unavailable",
    ))
    def test_strict_bootstrap_master_receipt_failure_is_disposable_only_during_master(
        self,
        reason,
    ):

        master_checkpoint = _strict_checkpoint(T + 1, T, "direction_audited")
        assert tbm._generic_abandon_stage_block(master_checkpoint, reason) is None

        reviewed_checkpoint = _strict_checkpoint(T + 1, T, "reviewed")
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
            T + 1,
            T,
            "direction_audited",
            run_id=f"{T + 1}#0",
            workflow_run_id=f"generation:{T + 1}:strict-terminal",
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

        candidate = _strict_artifact(tmp_path / bot_name(T + 1), T + 1)
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


def _schema2_claim_fixture(
    tmp_path,
    monkeypatch,
    *,
    checkpoint=None,
    reason="abandon_generation",
):
    import evolution_core
    import evolution_infra

    checkpoint = checkpoint or _strict_checkpoint(T + 1, T, "master_planned")
    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = tmp_path / bot_name(T + 1)
    _strict_artifact(candidate, T + 1)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tbm.RESULTS_DIR)
    monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)
    claim, _candidate_path, transaction_dir = _persist_schema2_claim(
        checkpoint,
        reason=reason,
    )
    return checkpoint, state_file, candidate, claim, transaction_dir


def test_schema3_first_strict_fence_survives_checkpoint_clear_and_revalidates(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import evolution_infra
    import first_strict_execution_journal as execution_journal
    from bot_artifact import canonical_digest
    from epoch_authority import (
        validate_abandon_claim_structure,
        validate_abandon_finalize_receipt,
    )

    checkpoint = _strict_checkpoint(T + 1, T, "precommit_failed")
    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = tmp_path / bot_name(T + 1)
    _strict_artifact(candidate, T + 1)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tbm.RESULTS_DIR)
    monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(
        execution_journal,
        "CONTROL_EXECUTION_ROOT",
        tmp_path / "first_strict_journal",
    )
    scope = {
        "workflow_run_id": checkpoint["workflow_run_id"],
        "checkpoint_revision": checkpoint["checkpoint_revision"],
        "candidate_version": checkpoint["next_v"],
        "candidate_label": bot_name(T + 1),
        "candidate_artifact_hash": "a" * 64,
        "control_id": "first_strict_control_v1",
        "control_artifact_hash": "b" * 64,
        "control_receipt_digest": "c" * 64,
        "precommit_plan_digest": "d" * 64,
        "evaluation_contract_digest": "e" * 64,
        "native_match_timing_plan_digest": "f" * 64,
        "precommit_attempt": 1,
    }
    terminal_receipt = execution_journal.abandon_control_execution(
        scope,
        reason="abandon_generation",
    )
    fence = {
        "present": True,
        "abandoned": True,
        "scope": scope,
        "terminal_receipt": terminal_receipt,
        "proof_digest": canonical_digest({
            "scope": scope,
            "terminal_receipt": terminal_receipt,
        }),
    }

    def clear(**_expected):
        state_file.unlink()
        return True

    transaction = tbm._finalize_checkpoint_abandon_transaction(
        checkpoint,
        reason="abandon_generation",
        infra_failure=None,
        timestamp=10.0,
        recorded_abandon_receipt=None,
        first_strict_execution_fence=fence,
        clear_pipeline_state=clear,
    )
    transaction_dir = (
        tbm.RESULTS_DIR
        / "policy_epoch_abandon_transactions"
        / transaction["transaction_id"]
    )
    claim = json.loads(
        (transaction_dir / "claim.json").read_text(encoding="utf-8")
    )
    finalize = json.loads(
        (transaction_dir / "receipt.json").read_text(encoding="utf-8")
    )
    rows = evolution_infra.load_abandoned_version_receipts(
        path=tbm.RESULTS_DIR / "abandoned_versions.jsonl",
        project_root=tbm.PROJECT_ROOT,
    )

    assert claim["schema_version"] == 3
    assert claim["first_strict_execution_fence"] == fence
    assert finalize["schema_version"] == 3
    assert finalize["first_strict_execution_fence_digest"] == fence[
        "proof_digest"
    ]
    assert validate_abandon_claim_structure(claim) == claim
    assert validate_abandon_finalize_receipt(claim, finalize, rows) == finalize
    assert transaction["first_strict_execution_fence"] == fence
    assert not state_file.exists()
    assert not candidate.exists()

    forged = json.loads(json.dumps(claim))
    forged["first_strict_execution_fence"]["scope"][
        "checkpoint_revision"
    ] += 1
    unsigned = {
        key: value for key, value in forged.items() if key != "claim_digest"
    }
    forged["claim_digest"] = canonical_digest(unsigned)
    with pytest.raises(RuntimeError, match="first_strict"):
        validate_abandon_claim_structure(forged)


def test_private_bootstrap_abandon_preserves_real_succeeded_first_strict_journal(
    tmp_path,
    monkeypatch,
):
    """Reproduce the live 8/8 terminal conflict through the real abandon owner."""

    import evolution_core
    import evolution_infra
    import first_strict_execution_journal as execution_journal
    import national_native
    import precommit_eval_contract
    import tool_eval
    import tool_gates
    import bootstrap_contract_recovery as bootstrap_recovery
    from bot_artifact import canonical_digest, hash_path
    from tests.test_checkpoint_epoch_recovery import _write_reset_authority

    runtime_root = tmp_path / ".evolution_pok"
    reset_receipt = _write_reset_authority(
        runtime_root,
        _branch_reset_receipt(),
    )
    results_dir = runtime_root / "web" / "core" / "results"
    monkeypatch.setattr(tbm, "PROJECT_ROOT", runtime_root)
    monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", runtime_root)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_SOURCE_V)
    monkeypatch.setattr(
        evolution_infra,
        "ABANDONED_VERSIONS_FILE",
        results_dir / "abandoned_versions.jsonl",
    )
    candidate = _strict_artifact(runtime_root / "bots" / bot_name(T), T)
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=(
            national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC
        ),
    )
    workflow_run_id = f"generation:{T}:workflow-v62"
    scope = {
        "workflow_run_id": workflow_run_id,
        "checkpoint_revision": 13,
        "candidate_version": T,
        "candidate_label": bot_name(T),
        "candidate_artifact_hash": hash_path(candidate),
        "control_id": "first_strict_control_v1",
        "control_artifact_hash": "b" * 64,
        "control_receipt_digest": "c" * 64,
        "precommit_plan_digest": "d" * 64,
        "evaluation_contract_digest": "e" * 64,
        "native_match_timing_plan_digest": timing_plan.digest(),
        "precommit_attempt": 1,
    }
    precommit_plan = {
        "opponents": [{"authority": "system_first_strict_control"}],
    }
    from system_strict_bootstrap import build_fresh_bootstrap_receipt

    protocol_bootstrap = build_fresh_bootstrap_receipt(
        active_bots=(),
        epoch_reset_receipt_digest=reset_receipt["receipt_digest"],
    )
    checkpoint = _strict_checkpoint(
        T,
        STRICT_SOURCE_V,
        "official_bootstrap_required",
        workflow_run_id=workflow_run_id,
        checkpoint_revision=22,
        precommit_attempt=1,
        publication_intent=None,
        official_job=None,
        audit_context={
            "protocol_bootstrap": protocol_bootstrap,
            "first_strict_control_execution_scope": scope,
            "precommit_eval_plan": precommit_plan,
        },
    )
    state_file = results_dir / "pipeline_state.json"
    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    current = {"checkpoint": checkpoint}
    monkeypatch.setattr(
        tbm,
        "read_pipeline_checkpoint",
        lambda: current["checkpoint"],
    )
    monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)

    monkeypatch.setattr(
        execution_journal,
        "CONTROL_EXECUTION_ROOT",
        tmp_path / "first_strict_execution",
    )
    monkeypatch.setattr(
        national_native,
        "_validate_first_strict_runner_execution_seal",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        national_native,
        "_consume_first_strict_runner_execution_seal",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        execution_journal,
        "_terminal_execution_issues",
        lambda *_a, **_k: ([], {"terminal": True}),
    )
    receipts = []
    for repeat in range(1, 9):
        deck = 91_000 + (repeat - 1) * 1_000
        ticket = execution_journal.begin_control_execution(
            scope=scope,
            repeat=repeat,
            deck_seed_base=deck,
            bot_seed_base=deck + 1_000_000_000,
            timing_plan=timing_plan,
            claim_now=__import__("time").time(),
        )
        receipts.append(execution_journal.complete_control_execution(
            ticket,
            execution={"complete_70_hand_match": True, "repeat": repeat},
        ))
    terminal = execution_journal.succeed_control_execution(
        scope,
        expected_receipts=receipts,
    )
    proof_payload = {
        "scope": scope,
        "expected_receipts": receipts,
        "terminal_receipt": terminal,
    }
    success_proof = {
        **proof_payload,
        "proof_digest": canonical_digest(proof_payload),
    }
    external = {"first_strict_execution_success": success_proof}
    authority_run_id = execution_journal._authority_run_id(scope)
    store = execution_journal._store()
    before = {
        "instance": store.instance(authority_run_id),
        "events": [dict(event.__dict__) for event in store.events(authority_run_id)],
        "effects": store.effects_for_run(authority_run_id),
        "terminal": terminal,
    }

    monkeypatch.setattr(
        precommit_eval_contract,
        "opponents_from_plan",
        lambda _plan: [],
    )
    monkeypatch.setattr(
        precommit_eval_contract,
        "build_evaluation_contract",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        tool_eval,
        "_validate_first_strict_control_execution_scope",
        lambda *_a, **_k: (scope, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_bot_code_fingerprint",
        lambda _path: scope["candidate_artifact_hash"],
    )
    monkeypatch.setattr(
        tbm,
        "_bootstrap_contract_change_abandon_authority",
        lambda *_a, **_k: external,
    )

    def reopen_external(_claim):
        bootstrap_recovery.validate_first_strict_execution_success(
            success_proof
        )
        return external

    monkeypatch.setattr(
        tbm,
        "_validate_external_bootstrap_contract_abandon_proof",
        reopen_external,
    )

    def clear(**_expected):
        current["checkpoint"] = None
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    reason = "official_bootstrap_contract_change:" + "a" * 64
    result = _run(tbm._do_abandon_generation(
        reason=reason,
        _bypass_rate_limit=True,
        expected_workflow_run_id=workflow_run_id,
        expected_next_v=T,
        expected_source_v=STRICT_SOURCE_V,
        expected_checkpoint_revision=22,
        expected_checkpoint_stage="official_bootstrap_required",
        _operator_bootstrap_contract_change_claim_digest="a" * 64,
    ))

    assert result["abandoned"] is True, result
    assert result["first_strict_execution_fence"]["abandoned"] is False
    assert result["first_strict_execution_fence"]["proof_digest"] == (
        success_proof["proof_digest"]
    )
    transaction = (
        tbm.RESULTS_DIR
        / "policy_epoch_abandon_transactions"
        / result["abandon_transaction_id"]
    )
    canonical = json.loads(
        (transaction / "claim.json").read_text(encoding="utf-8")
    )
    assert canonical["schema_version"] == 2
    assert "first_strict_execution_fence" not in canonical
    assert (transaction / "receipt.json").is_file()
    assert (transaction / "candidate").is_dir()
    assert not candidate.exists()
    assert not state_file.exists()
    after = {
        "instance": store.instance(authority_run_id),
        "events": [dict(event.__dict__) for event in store.events(authority_run_id)],
        "effects": store.effects_for_run(authority_run_id),
        "terminal": execution_journal.read_succeeded_control_execution(
            scope,
            expected_receipts=receipts,
            expected_terminal_receipt=terminal,
        ),
    }
    assert after == before

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
    assert checkpoint["next_v"] == T + 1


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


def test_active_claim_reopener_requires_external_bootstrap_preimage(
    tmp_path,
    monkeypatch,
):
    _checkpoint, state_file, candidate, _claim, _transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch)
    )
    monkeypatch.setattr(
        tbm,
        "_validate_external_bootstrap_contract_abandon_proof",
        lambda _claim: (_ for _ in ()).throw(
            RuntimeError("external bootstrap proof missing")
        ),
    )

    result = _run(tbm._do_abandon_generation(reason="abandon_generation"))

    assert result["abandoned"] is False
    assert result["action"] == "operator_reconcile"
    assert "external bootstrap proof missing" in result["error"]
    assert state_file.exists()
    assert candidate.exists()


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
            # The abandon validators now read the primary checkpoint under
            # no_slot_override() (infra is the injected evolution_infra
            # namespace in production); mirror that member here.
            no_slot_override=evolution_infra.no_slot_override,
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

    import bootstrap_contract_recovery as bootstrap_recovery

    monkeypatch.setattr(
        bootstrap_recovery,
        "validate_canonical_abandon_external_binding",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("external bootstrap journal drift")
        ),
    )
    external_drift = epoch_authority._runtime_reconciliation_claim_status(
        live,
        results_dir=tbm.RESULTS_DIR,
        bots_dir=candidate.parent,
        infra=fake_infra("a" * 40),
    )
    assert external_drift["claimed"] is True
    assert external_drift["valid"] is False
    assert any(
        "external bootstrap journal drift" in issue
        for issue in external_drift["issues"]
    )


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

    checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = tmp_path / bot_name(T + 1)
    _strict_artifact(candidate, T + 1)
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
        T + 2,
        T,
        "master_planned",
        published_high_water=T,
        abandoned_receipt_floor=T + 1,
        abandoned_receipt_head_digest=evolution_infra._abandoned_ledger_head_digest(
            first_rows
        ),
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

    timed_out = _strict_checkpoint(T + 1, T, "timed_out")
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
        "workflow_run_id": f"generation:{T + 1}:foreign-workflow",
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


def _completed_split_reason_abandon(tmp_path, monkeypatch):
    """Build the v48 shape without changing any durable historical receipt.

    The Worker has already recorded its bounded executor failure when the
    outer actor later records ``worker_terminal_abandon`` and fences strict
    authority.  This is a normal schema-2 claim shape, not a compatibility
    rewrite of the claim or ledger.
    """

    from worker_workflow import WorkerWorkflow

    outer_reason = "worker_terminal_abandon"
    inner_reason = "system_strict_bootstrap_execution_failed"
    checkpoint, state_file, candidate, claim, transaction_dir = (
        _schema2_claim_fixture(
            tmp_path,
            monkeypatch,
            reason=outer_reason,
        )
    )
    workflow = WorkerWorkflow.for_checkpoint(checkpoint)
    workflow.abandon(inner_reason)

    def clear(**_kwargs):
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    result = _run(tbm._do_abandon_generation(
        reason=outer_reason,
        _bypass_rate_limit=True,
        **tbm.expected_abandon_identity(checkpoint),
    ))
    assert result["abandoned"] is True, result
    return {
        "checkpoint": checkpoint,
        "candidate": candidate,
        "claim": claim,
        "transaction_dir": transaction_dir,
        "result": result,
        "inner_reason": inner_reason,
        "outer_reason": outer_reason,
    }


def _rewrite_terminal_payload(database, run_id, event_type, payload):
    import hashlib
    import sqlite3

    from workflow_kernel import canonical_json

    raw = canonical_json(payload)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE workflow_events SET payload = ?, payload_digest = ? "
            "WHERE run_id = ? AND event_type = ?",
            (
                raw,
                hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                run_id,
                event_type,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_completed_abandon_handoff_reproves_schema2_split_worker_reason(
    tmp_path,
    monkeypatch,
):
    state = _completed_split_reason_abandon(tmp_path, monkeypatch)

    proof = tbm.validate_completed_abandon_handoff(
        state["checkpoint"],
        state["result"],
    )

    assert state["claim"]["schema_version"] == 2
    assert proof["workflow_fences"]["worker"]["terminal_reason"] == (
        state["inner_reason"]
    )
    assert proof["workflow_fences"]["strict_authority"]["terminal_reason"] == (
        state["outer_reason"]
    )
    assert not state["candidate"].exists()
    assert (state["transaction_dir"] / "receipt.json").is_file()


def test_completed_abandon_handoff_rejects_missing_worker_inner_reason(
    tmp_path,
    monkeypatch,
):
    state = _completed_split_reason_abandon(tmp_path, monkeypatch)
    database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
    _rewrite_terminal_payload(
        database,
        state["checkpoint"]["workflow_run_id"],
        "WorkerAbandoned",
        {},
    )

    with pytest.raises(
        RuntimeError,
        match="completed_abandon_WorkerAbandoned_reason_invalid",
    ):
        tbm.validate_completed_abandon_handoff(
            state["checkpoint"],
            state["result"],
        )


def test_completed_abandon_handoff_rejects_mismatched_outer_reason(
    tmp_path,
    monkeypatch,
):
    state = _completed_split_reason_abandon(tmp_path, monkeypatch)
    database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
    _rewrite_terminal_payload(
        database,
        strict_authority.authority_run_id(
            state["checkpoint"]["workflow_run_id"]
        ),
        "StrictAuthorityAbandoned",
        {
            "reason": "other_outer_reason",
            "workflow_run_id": state["checkpoint"]["workflow_run_id"],
        },
    )

    with pytest.raises(
        RuntimeError,
        match="completed_abandon_StrictAuthorityAbandoned_outer_reason_mismatch",
    ):
        tbm.validate_completed_abandon_handoff(
            state["checkpoint"],
            state["result"],
        )


def test_completed_abandon_handoff_rejects_unbound_worker_inner_reason(
    tmp_path,
    monkeypatch,
):
    state = _completed_split_reason_abandon(tmp_path, monkeypatch)
    database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
    _rewrite_terminal_payload(
        database,
        state["checkpoint"]["workflow_run_id"],
        "WorkerAbandoned",
        {"reason": "worker_harness_failure"},
    )

    with pytest.raises(
        RuntimeError,
        match="completed_abandon_WorkerAbandoned_reason_unbound",
    ):
        tbm.validate_completed_abandon_handoff(
            state["checkpoint"],
            state["result"],
        )


@pytest.mark.parametrize(
    ("outer_length", "legacy_causation"),
    ((999, False), (1000, False), (1001, True), (4096, True)),
)
def test_completed_abandon_handoff_reproves_boundary_outer_reason(
    tmp_path,
    monkeypatch,
    outer_length,
    legacy_causation,
):
    """Accept the exact old/new causation constructions and nothing broader."""

    from worker_workflow import WorkerWorkflow
    from workflow_kernel import content_digest

    outer_prefix = "worker_terminal_abandon:"
    outer_reason = outer_prefix + "x" * (outer_length - len(outer_prefix))
    checkpoint, state_file, _candidate, _claim, _transaction_dir = (
        _schema2_claim_fixture(
            tmp_path,
            monkeypatch,
            reason=outer_reason,
        )
    )
    workflow = WorkerWorkflow.for_checkpoint(checkpoint)
    workflow.abandon(outer_reason)
    terminal = workflow.store.events(workflow.run_id)[-1]
    bounded_reason = outer_reason[:1000]
    assert terminal.payload == {"reason": bounded_reason}
    assert terminal.causation_id.endswith(f":{content_digest(bounded_reason)}")

    if legacy_causation:
        # Before the producer repair, the payload was bounded but the
        # causation id used the unbounded outer argument.  Model that exact
        # immutable schema-2 history without changing a real receipt.
        import sqlite3

        database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE workflow_events SET causation_id = ? "
                "WHERE run_id = ? AND event_type = 'WorkerAbandoned'",
                (
                    f"worker-abandoned:{workflow.run_id}:cycle-0:"
                    f"{content_digest(outer_reason)}",
                    workflow.run_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def clear(**_kwargs):
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    result = _run(tbm._do_abandon_generation(
        reason=outer_reason,
        _bypass_rate_limit=True,
        **tbm.expected_abandon_identity(checkpoint),
    ))

    proof = tbm.validate_completed_abandon_handoff(checkpoint, result)
    assert proof["workflow_fences"]["worker"]["terminal_reason"] == bounded_reason
    assert proof["workflow_fences"]["strict_authority"]["terminal_reason"] == (
        bounded_reason
    )


def _configure_historical_terminal_main(
    monkeypatch,
    *,
    recorded_head="a" * 40,
    current_head="b" * 40,
    remote_head=None,
    branch=EVOLUTION_BRANCH,
    tracked_status="",
    ancestor=True,
):
    """Model a clean fetched ``main`` descendant without using test Git."""

    remote_head = remote_head if remote_head is not None else current_head
    remote_main_ref = f"refs/remotes/origin/{EVOLUTION_BRANCH}"

    def fake_git(*args, **_kwargs):
        values = {
            ("rev-parse", f"{recorded_head}^{{commit}}"):
                recorded_head,
            ("rev-parse", "HEAD"): current_head,
            ("rev-parse", remote_main_ref): remote_head,
            ("branch", "--show-current"): branch,
            ("status", "--porcelain", "--untracked-files=no"):
                tracked_status,
        }
        if args in values:
            return values[args]
        raise AssertionError(f"unexpected historical git call: {args!r}")

    monkeypatch.setattr(tbm, "_evolution_git", fake_git)
    monkeypatch.setattr(
        tbm,
        "_historical_head_is_ancestor",
        lambda old, new: bool(
            ancestor and old == recorded_head and new == current_head
        ),
    )


def _completed_historical_reproof_fixture(
    tmp_path,
    monkeypatch,
    *,
    reason="abandon_generation",
    inner_reason=None,
    legacy_causation=False,
):
    """Finalize a schema-2 transaction, then model a source-only sync."""

    from worker_workflow import WorkerWorkflow
    from workflow_kernel import content_digest

    checkpoint, state_file, candidate, claim, transaction_dir = (
        _schema2_claim_fixture(tmp_path, monkeypatch, reason=reason)
    )
    if inner_reason is not None:
        workflow = WorkerWorkflow.for_checkpoint(checkpoint)
        workflow.abandon(inner_reason)
    elif legacy_causation:
        workflow = WorkerWorkflow.for_checkpoint(checkpoint)
        workflow.abandon(reason)
        database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
        connection = __import__("sqlite3").connect(database)
        try:
            connection.execute(
                "UPDATE workflow_events SET causation_id = ? "
                "WHERE run_id = ? AND event_type = 'WorkerAbandoned'",
                (
                    f"worker-abandoned:{workflow.run_id}:cycle-0:"
                    f"{content_digest(reason)}",
                    workflow.run_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def clear(**_kwargs):
        state_file.unlink()
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)
    result = _run(tbm._do_abandon_generation(
        reason=reason,
        _bypass_rate_limit=True,
        **tbm.expected_abandon_identity(checkpoint),
    ))
    assert result["abandoned"] is True, result
    _configure_historical_terminal_main(monkeypatch)
    return {
        "checkpoint": checkpoint,
        "state_file": state_file,
        "candidate": candidate,
        "claim": claim,
        "transaction_dir": transaction_dir,
        "result": result,
    }


def test_historical_completed_abandon_reproof_allows_clean_main_descendant(
    tmp_path,
    monkeypatch,
):
    state = _completed_historical_reproof_fixture(tmp_path, monkeypatch)
    # The historical path must not try to recreate/read a vanished checkpoint
    # or invoke the mutating clear primitive.  Its only state input is the
    # immutable finalized transaction and the fenced journals.
    monkeypatch.setattr(
        tbm,
        "read_pipeline_checkpoint",
        lambda: (_ for _ in ()).throw(AssertionError("checkpoint read")),
    )
    monkeypatch.setattr(
        tbm,
        "clear_pipeline_checkpoint",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("checkpoint clear")),
    )

    proof = tbm.reprove_historical_completed_abandon(
        state["claim"]["transaction_id"]
    )

    assert proof["kind"] == "national-policy-historical-completed-abandon-reproof-v1"
    assert proof["authority"] == "completed_abandon_terminal_evidence_only"
    assert proof["prepare_authorized"] is False
    assert proof["next_tool"] is None
    assert proof["transaction_id"] == state["claim"]["transaction_id"]
    assert proof["checkpoint_identity"] == state["claim"]["checkpoint"]
    assert proof["source"] == {
        "recorded_git_head": "a" * 40,
        "current_git_head": "b" * 40,
        "remote_main_ref": f"refs/remotes/origin/{EVOLUTION_BRANCH}",
        "remote_main_head": "b" * 40,
        "source_descendant_verified": True,
    }
    assert proof["workflow_fences"]["worker"]["terminal_event"] == (
        "WorkerAbandoned"
    )
    assert not state["state_file"].exists()
    assert not state["candidate"].exists()


def test_historical_completed_abandon_reproof_requires_external_preimage(
    tmp_path,
    monkeypatch,
):
    state = _completed_historical_reproof_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        tbm,
        "_validate_external_bootstrap_contract_abandon_proof",
        lambda _claim: (_ for _ in ()).throw(
            RuntimeError("historical external proof missing")
        ),
    )

    with pytest.raises(RuntimeError, match="historical external proof missing"):
        tbm.reprove_historical_completed_abandon(
            state["claim"]["transaction_id"]
        )


def test_historical_head_ancestry_accepts_actual_v48_recorded_main_lineage():
    """The v48 claim records edbf; this source branch is its descendant."""

    import subprocess

    recorded_v48_head = "edbfcbfd9ede858606d4910b8074c90a82aeb6e7"
    current = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tbm.PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tbm._historical_head_is_ancestor(recorded_v48_head, current)


@pytest.mark.parametrize(
    ("outer_length", "legacy_causation"),
    ((999, False), (1000, False), (1001, True), (4096, True)),
)
def test_historical_completed_abandon_reproof_accepts_bounded_reason_compatibility(
    tmp_path,
    monkeypatch,
    outer_length,
    legacy_causation,
):
    prefix = "worker_terminal_abandon:"
    outer_reason = prefix + "x" * (outer_length - len(prefix))
    state = _completed_historical_reproof_fixture(
        tmp_path,
        monkeypatch,
        reason=outer_reason,
        legacy_causation=legacy_causation,
    )

    proof = tbm.reprove_historical_completed_abandon(
        state["claim"]["transaction_id"]
    )

    assert proof["workflow_fences"]["worker"]["terminal_reason"] == (
        outer_reason[:1000]
    )
    assert proof["workflow_fences"]["strict_authority"]["terminal_reason"] == (
        outer_reason[:1000]
    )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    (
        ({"branch": "feature"}, "historical_completed_abandon_not_on_main"),
        (
            {"remote_head": "c" * 40},
            "historical_completed_abandon_main_not_fetched",
        ),
        (
            {"tracked_status": " M web/core/tool_bot_management.py"},
            "historical_completed_abandon_tracked_worktree_dirty",
        ),
        (
            {"ancestor": False},
            "historical_completed_abandon_main_not_descendant",
        ),
    ),
)
def test_historical_completed_abandon_reproof_rejects_untrusted_current_source(
    tmp_path,
    monkeypatch,
    kwargs,
    error,
):
    state = _completed_historical_reproof_fixture(tmp_path, monkeypatch)
    _configure_historical_terminal_main(monkeypatch, **kwargs)

    with pytest.raises(RuntimeError, match=error):
        tbm.reprove_historical_completed_abandon(state["claim"]["transaction_id"])


def test_historical_completed_abandon_reproof_rejects_live_or_unfinalized_paths(
    tmp_path,
    monkeypatch,
):
    state = _completed_historical_reproof_fixture(tmp_path, monkeypatch)
    state["state_file"].write_text("{}", encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="historical_completed_abandon_terminal_paths_live",
    ):
        tbm.reprove_historical_completed_abandon(state["claim"]["transaction_id"])

    state["state_file"].unlink()
    (state["transaction_dir"] / "receipt.json").unlink()
    with pytest.raises(
        RuntimeError,
        match="historical_completed_abandon_finalize_receipt_missing",
    ):
        tbm.reprove_historical_completed_abandon(state["claim"]["transaction_id"])


def test_historical_completed_abandon_reproof_rejects_advanced_ledger(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    state = _completed_historical_reproof_fixture(tmp_path, monkeypatch)
    original_rows = evolution_infra.load_abandoned_version_receipts(
        path=tbm.RESULTS_DIR / "abandoned_versions.jsonl",
        project_root=tbm.PROJECT_ROOT,
    )
    original = original_rows[-1]
    later = _strict_checkpoint(
        T + 2,
        T,
        "master_planned",
        published_high_water=T,
        abandoned_receipt_floor=T + 1,
        abandoned_receipt_head_digest=evolution_infra._abandoned_ledger_head_digest(
            original_rows
        ),
    )
    evolution_infra.append_abandoned_version_receipt(
        later,
        reason="later-legitimate-abandon",
        timestamp=99.0,
        path=tbm.RESULTS_DIR / "abandoned_versions.jsonl",
        project_root=tbm.PROJECT_ROOT,
    )
    with pytest.raises(RuntimeError, match="recorded_abandon_active_ledger_advanced"):
        tbm.reprove_historical_completed_abandon(state["claim"]["transaction_id"])


def test_historical_completed_abandon_reproof_rejects_tampered_journal(
    tmp_path,
    monkeypatch,
):
    # A fresh terminal transaction reaches the journal proof; a forged event
    # with a re-signed payload digest still fails its causal binding.
    state = _completed_historical_reproof_fixture(tmp_path, monkeypatch)
    database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
    _rewrite_terminal_payload(
        database,
        state["checkpoint"]["workflow_run_id"],
        "WorkerAbandoned",
        {"reason": "unbound_worker_reason"},
    )
    with pytest.raises(
        RuntimeError,
        match="completed_abandon_WorkerAbandoned_reason_unbound",
    ):
        tbm.reprove_historical_completed_abandon(state["claim"]["transaction_id"])


def test_historical_completed_abandon_reproof_rejects_unbound_strict_reason(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    state = _completed_historical_reproof_fixture(tmp_path, monkeypatch)
    database = tbm.RESULTS_DIR / "workflow" / "events.sqlite3"
    strict_run_id = strict_authority.authority_run_id(
        state["checkpoint"]["workflow_run_id"]
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE workflow_events SET causation_id = ? "
            "WHERE run_id = ? AND event_type = 'StrictAuthorityAbandoned'",
            ("strict-authority-abandoned:forged", strict_run_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        RuntimeError,
        match="completed_abandon_StrictAuthorityAbandoned_reason_unbound",
    ):
        tbm.reprove_historical_completed_abandon(state["claim"]["transaction_id"])


def test_historical_completed_abandon_reproof_rejects_resurrected_candidate(
    tmp_path,
    monkeypatch,
):
    state = _completed_historical_reproof_fixture(tmp_path, monkeypatch)
    quarantine = state["transaction_dir"] / "candidate"
    quarantine.rename(state["candidate"])

    with pytest.raises(
        RuntimeError,
        match="historical_completed_abandon_candidate_not_finalized",
    ):
        tbm.reprove_historical_completed_abandon(state["claim"]["transaction_id"])


def test_completed_abandon_handoff_allows_monotonic_terminal_revision(
    tmp_path,
    monkeypatch,
):
    terminal_checkpoint = _strict_checkpoint(
        T + 1,
        T,
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


def test_worker_terminal_abandon_rework_reasons_allowed_in_all_rework_stages():
    """v165 quality_failed 死循环修复回归。

    execute_workers 的 4 个 rework authority 失败点（REWORK_TASK_AUTHORITY_INVALID /
    REPAIR_BASELINE_ARTIFACT_DRIFT / REWORK_FEEDBACK_AUTHORITY_MISSING /
    REWORK_FEEDBACK_AUTHORITY_MISMATCH）现在在工具内用 worker_terminal_abandon_*
    reason 调 _force_abandon_frozen_worker_generation 实际放弃，而非返回裸
    next_tool:abandon_generation。这些 reason 必须在所有 rework_stages 被
    forced_rules 接受，否则 deterministic routing（pipeline_state.py
    quality_failed→execute_workers 等）会按 stage 无限重发 → 死循环
    （v160 Bug A 教训：crossover_ reason 漏 selected 被 forced_rules 拒 + 死循环）。
    """
    import pipeline_state

    rework_stages = [
        "quality_failed",
        "precommit_failed",
        "official_failed",
        "repair_planned",
        "rework_running",
    ]
    reasons = [
        "worker_terminal_abandon_rework_task_authority_invalid",
        "worker_terminal_abandon_repair_baseline_drift",
        "worker_terminal_abandon_rework_feedback_missing",
        "worker_terminal_abandon_rework_feedback_mismatch",
    ]
    for stage in rework_stages:
        checkpoint = _strict_checkpoint(T + 23, T, stage)
        for reason in reasons:
            block = pipeline_state.generic_abandon_block(checkpoint, reason=reason)
            if block:
                assert block.get("reason") != "forced_abandon_reason_stage_not_allowed", (
                    f"{reason} forced_rules-rejected at stage={stage}: {block}"
                )


def test_rework_authority_failure_paths_abandon_in_tool_source():
    """防御回归：4 个 rework authority 失败路径必须在工具内调
    _force_abandon_frozen_worker_generation 并带 worker_terminal_abandon_ reason，
    而非返回裸 next_tool:abandon_generation（v165 死循环根因）。
    """
    import inspect
    import tool_planning
    import tool_planning_worker

    # After the Group-E quality-contract extraction (commit af4187a5), the
    # worker-execution body that emits these reasons lives in tool_planning_worker
    # (re-exported by tool_planning). The F-group durable projection/effect
    # extraction (wave 6) then moved that body into tool_planning_worker_durable,
    # and the second F-cut moved the _execute_workers_command dispatcher body
    # into tool_planning_worker_phases. The wave-8 slimming then moved the two
    # rework phase functions (which own these abandon reasons) into the
    # tool_planning_worker_phases_rework companion. Inspect all five modules so
    # the regression guard tracks the reasons wherever the extraction placed them.
    import tool_planning_worker_durable
    import tool_planning_worker_phases
    import tool_planning_worker_phases_rework
    source = (
        inspect.getsource(tool_planning)
        + inspect.getsource(tool_planning_worker)
        + inspect.getsource(tool_planning_worker_durable)
        + inspect.getsource(tool_planning_worker_phases)
        + inspect.getsource(tool_planning_worker_phases_rework)
    )
    for reason in [
        "worker_terminal_abandon_rework_task_authority_invalid",
        "worker_terminal_abandon_repair_baseline_drift",
        "worker_terminal_abandon_rework_feedback_missing",
        "worker_terminal_abandon_rework_feedback_mismatch",
    ]:
        assert reason in source, (
            f"{reason} 缺失——rework authority 工具内 abandon 回归"
        )


# ---------------------------------------------------------------------------
# General contract-change abandon authority (verified/publishing + contract
# deploy deadlock). Mirrors the bootstrap bypass pattern but for any
# publication-family generation stranded by a contract-critical deploy.
# Regression anchors for the recovery path added 2026-07-31.
# ---------------------------------------------------------------------------


def _verified_contract_change_checkpoint(next_v, source_v, *, revision=17):
    """A publication-family checkpoint with a repo_baseline that has drifted.

    Models the v25 deadlock: stage=verified, repo_baseline.head points at the
    pre-deploy commit and the live HEAD has advanced (contract changed).
    """
    return _strict_checkpoint(
        next_v,
        source_v,
        "verified",
        run_id=f"{next_v}#0",
        workflow_run_id=f"generation:{next_v}:workflow-v1",
        checkpoint_revision=revision,
        publication_tier="staging",
        repo_baseline={
            "branch": EVOLUTION_BRANCH,
            "head": "57a76b23328d",  # pre-deploy short HEAD (baseline)
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": [f"?? bots/{bot_name(next_v)}/"],
            "truncated": False,
            "evaluation_contract": {"version": 42, "hash": "0" * 64},
            "captured_stage": "verified",
            "captured_ts": 1785446247.0,
        },
    )


def _build_contract_change_proof(checkpoint, *, current_head, contract_paths):
    """Build the operator proof the authority must match (mirrors the script)."""
    from bot_artifact import canonical_digest

    identity = tbm._checkpoint_transaction_identity(checkpoint)
    rebuilt = {
        "schema_version": 1,
        "kind": "national-contract-change-abandon-proof",
        "evaluation_epoch": checkpoint.get("evaluation_epoch"),
        "baseline_head": checkpoint["repo_baseline"]["head"],
        "current_head": current_head,
        "changed_contract_paths": sorted(contract_paths),
        "checkpoint": identity,
        "stage": checkpoint.get("stage"),
    }
    rebuilt["claim_digest"] = canonical_digest(rebuilt)
    return rebuilt


def _wire_contract_change_drift(monkeypatch, *, current_head, contract_paths):
    """Monkeypatch the head-drift + git primitives the authority re-proves."""
    monkeypatch.setattr(
        tbm,
        "_evolution_git",
        lambda *args, **_kw: current_head if args == ("rev-parse", "HEAD") else "",
    )

    def fake_evaluate_head_drift(_root, baseline_head, _current_head, **_kw):
        # contract_unchanged is False iff heads differ AND contract paths changed.
        unchanged = bool(baseline_head) and baseline_head == current_head
        return unchanged, {
            "head_drift_paths_available": True,
            "evaluation_contract_unchanged": unchanged,
            "head_contract_paths": list(contract_paths) if not unchanged else [],
        }

    import evaluation_contract

    monkeypatch.setattr(
        evaluation_contract, "evaluate_head_drift", fake_evaluate_head_drift
    )


def test_contract_change_authority_is_none_without_proof():
    """No proof => authority returns None => default never_disposable guard holds."""
    checkpoint = _verified_contract_change_checkpoint(T + 1, T)
    assert tbm._contract_change_abandon_authority(
        checkpoint, reason="abandon_generation", contract_change_proof=None
    ) is None


def test_contract_change_authority_rejects_out_of_scope_stage():
    """Only publication-family stages (verified/publishing/official_certifying)
    are eligible; a master-stage checkpoint cannot use this path."""
    checkpoint = _strict_checkpoint(T + 1, T, "master_planned")
    checkpoint["repo_baseline"] = {"head": "57a76b23328d"}
    proof = {"claim_digest": "a" * 64}
    with pytest.raises(RuntimeError, match="stage_not_publication_family"):
        tbm._contract_change_abandon_authority(
            checkpoint,
            reason="national_contract_change_abandon:" + "a" * 64,
            contract_change_proof=proof,
        )


def test_contract_change_authority_validates_real_drift(monkeypatch):
    """A proof built from the live drift passes; a forged one is rejected."""
    checkpoint = _verified_contract_change_checkpoint(T + 1, T)
    current_head = "c94c8a7b4ee4bf985cdad796eb951bcd0a524fc9"
    contract_paths = ["web/core/publication_transaction.py"]
    _wire_contract_change_drift(
        monkeypatch, current_head=current_head, contract_paths=contract_paths
    )
    proof = _build_contract_change_proof(
        checkpoint, current_head=current_head, contract_paths=contract_paths
    )
    reason = tbm._contract_change_abandon_reason(proof)
    assert reason.startswith("national_contract_change_abandon:")

    authority = tbm._contract_change_abandon_authority(
        checkpoint, reason=reason, contract_change_proof=proof
    )
    assert authority is not None
    assert authority["changed_contract_paths"] == contract_paths
    assert authority["current_head"] == current_head

    # A proof whose current_head was tampered (but whose digest was honestly
    # recomputed from the tampered fields) must be rejected: the authority
    # re-derives the live current_head from git and requires an exact match, so
    # a proof built against a different HEAD is refused before digest binding.
    forged = _build_contract_change_proof(
        checkpoint,
        current_head="deadbeef" * 5,
        contract_paths=contract_paths,
    )
    forged_reason = tbm._contract_change_abandon_reason(forged)
    with pytest.raises(RuntimeError, match="current_head_mismatch"):
        tbm._contract_change_abandon_authority(
            checkpoint, reason=forged_reason, contract_change_proof=forged
        )

    # A proof whose changed_contract_paths list omits a real changed path is
    # rejected (operator cannot understate the deploy's contract impact).
    understated = _build_contract_change_proof(
        checkpoint,
        current_head=current_head,
        contract_paths=[],  # claims nothing changed, but live drift has a path
    )
    understated_reason = tbm._contract_change_abandon_reason(understated)
    with pytest.raises(RuntimeError, match="contract_paths_mismatch"):
        tbm._contract_change_abandon_authority(
            checkpoint,
            reason=understated_reason,
            contract_change_proof=understated,
        )


def test_verified_stage_still_refused_without_contract_change_proof(
    tmp_path, monkeypatch
):
    """Negative anchor: verified without a proof is still never_disposable.

    The new authority must NOT weaken the default guard — verified remains
    non-disposable unless an explicit contract-change proof is supplied.
    """
    import evolution_core
    import evolution_infra

    checkpoint = _verified_contract_change_checkpoint(T + 1, T)
    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = tmp_path / bot_name(T + 1)
    _strict_artifact(candidate, T + 1)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tbm.RESULTS_DIR)
    monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
    cleared = []
    monkeypatch.setattr(
        tbm, "clear_pipeline_checkpoint", lambda **_kw: cleared.append(True) or True
    )
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)

    result = _run(
        tbm._do_abandon_generation(
            reason="abandon_generation",
            _bypass_rate_limit=True,
            expected_workflow_run_id=checkpoint["workflow_run_id"],
            expected_next_v=T + 1,
            expected_source_v=T,
            expected_checkpoint_revision=17,
            expected_checkpoint_stage="verified",
        )
    )
    assert result["abandoned"] is False
    assert result["stage"] == "verified"
    assert result["reason"] == "publication_or_certification_stage_not_disposable"
    assert cleared == []
    assert candidate.exists()


def test_verified_stage_abandons_with_valid_contract_change_proof(
    tmp_path, monkeypatch
):
    """Positive anchor: a verified gen stranded by a contract-critical deploy is
    abandoned via the proof authority — candidate quarantined, checkpoint
    cleared, abandoned_versions.jsonl receipt appended."""
    import evolution_core
    import evolution_infra

    checkpoint = _verified_contract_change_checkpoint(T + 1, T)
    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = tmp_path / bot_name(T + 1)
    _strict_artifact(candidate, T + 1)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tbm.RESULTS_DIR)
    monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)

    def clear(**_expected):
        state_file.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear)

    current_head = "c94c8a7b4ee4bf985cdad796eb951bcd0a524fc9"
    contract_paths = ["web/core/publication_transaction.py"]
    _wire_contract_change_drift(
        monkeypatch, current_head=current_head, contract_paths=contract_paths
    )
    proof = _build_contract_change_proof(
        checkpoint, current_head=current_head, contract_paths=contract_paths
    )
    reason = tbm._contract_change_abandon_reason(proof)

    result = _run(
        tbm._do_abandon_generation(
            reason=reason,
            _bypass_rate_limit=True,
            expected_workflow_run_id=checkpoint["workflow_run_id"],
            expected_next_v=T + 1,
            expected_source_v=T,
            expected_checkpoint_revision=17,
            expected_checkpoint_stage="verified",
            _operator_contract_change_proof=proof,
        )
    )
    assert result["abandoned"] is True, result

    # Candidate was quarantined (moved, not deleted) into the transaction dir.
    transaction = (
        tbm.RESULTS_DIR
        / "policy_epoch_abandon_transactions"
        / result["abandon_transaction_id"]
    )
    assert (transaction / "candidate").is_dir()
    assert not candidate.exists()
    assert not state_file.exists()

    # A terminal abandon receipt was appended for this version at verified.
    ledger = tbm.RESULTS_DIR / "abandoned_versions.jsonl"
    assert ledger.is_file()
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(
        r.get("version") == T + 1
        and r.get("checkpoint_stage") == "verified"
        and r.get("reason") == reason
        for r in rows
    )


def test_verified_stage_abandon_rejects_mismatched_contract_change_proof(
    tmp_path, monkeypatch
):
    """A proof that does not match the live drift surfaces a typed
    authority-invalid result (abandoned stays False)."""
    import evolution_core
    import evolution_infra

    checkpoint = _verified_contract_change_checkpoint(T + 1, T)
    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = tmp_path / bot_name(T + 1)
    _strict_artifact(candidate, T + 1)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tbm.RESULTS_DIR)
    monkeypatch.setattr(tbm, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tbm, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)

    current_head = "c94c8a7b4ee4bf985cdad796eb951bcd0a524fc9"
    real_paths = ["web/core/publication_transaction.py"]
    _wire_contract_change_drift(
        monkeypatch, current_head=current_head, contract_paths=real_paths
    )
    # Proof claims a DIFFERENT changed path than the live drift — must reject.
    proof = _build_contract_change_proof(
        checkpoint,
        current_head=current_head,
        contract_paths=["web/core/totally_different.py"],
    )
    reason = tbm._contract_change_abandon_reason(proof)

    result = _run(
        tbm._do_abandon_generation(
            reason=reason,
            _bypass_rate_limit=True,
            expected_workflow_run_id=checkpoint["workflow_run_id"],
            expected_next_v=T + 1,
            expected_source_v=T,
            expected_checkpoint_revision=17,
            expected_checkpoint_stage="verified",
            _operator_contract_change_proof=proof,
        )
    )
    assert result["abandoned"] is False
    assert result["reason"] == "contract_change_authority_invalid"
    assert candidate.exists()
    assert state_file.exists()
