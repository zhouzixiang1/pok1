from __future__ import annotations

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

import llm_availability_store
import llm_query
import tool_planning
from worker_workflow import (
    WORKER_WORKFLOW_DEFINITION_VERSION,
    WorkerArtifactStore,
    WorkerWorkflow,
    build_worker_envelope,
)
from workflow_kernel import WorkflowStore


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


class _UI:
    def __init__(self):
        self.history = []

    def log_history(self, message, level="info"):
        self.history.append((str(message), str(level)))
        return None

    def clear_io(self):
        return None

    def set_status(self, *_args, **_kwargs):
        return None

    def set_header(self, *_args, **_kwargs):
        return None

    def update_cost(self, *_args, **_kwargs):
        return None

    def reset_gen_cost(self, *_args, **_kwargs):
        return None


def _prepared_worker(tmp_path):
    run_id = "149#0"
    checkpoint = {
        "run_id": run_id,
        "workflow_run_id": run_id,
        "next_v": 149,
        "source_v": 143,
        "generation_attempt": 0,
        "stage": "master_planned",
        "checkpoint_revision": 8,
        "epoch_binding": {"source_artifact_inherited": True},
    }
    store = WorkflowStore(tmp_path / "events.sqlite3")
    store.ensure_instance(
        run_id,
        definition_version=WORKER_WORKFLOW_DEFINITION_VERSION,
    )
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    workflow = WorkerWorkflow(store=store, artifacts=artifacts, run_id=run_id)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n", encoding="utf-8")
    snapshot = artifacts.capture(candidate)
    envelope = build_worker_envelope(
        checkpoint=checkpoint,
        kind="initial_worker",
        source_stage="master_planned",
        prepared_artifact_hash=snapshot,
        prepared_snapshot_hash=snapshot,
        source_artifact_hash=_sha("source-artifact"),
        tasks=[{
            "worker_id": "policy",
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "worker_prompt": "implement the frozen policy task",
        }],
        reviewer_feedback="",
        worker_template_hash=_sha("worker-template"),
        work_item={"kind": "initial_worker"},
        backend_contract={"model": "sonnet"},
        precommit_rework_count=0,
        official_rework_count=0,
    )
    workflow.prepare(envelope, max_attempts=3)
    return workflow, checkpoint, envelope


def _install_execution_boundary(
    monkeypatch,
    tmp_path,
    checkpoint,
    envelope,
    *,
    shutdown_requested: bool,
    persistence_failure: bool = False,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "policy.py").write_text("source = True\n", encoding="utf-8")
    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_a: checkpoint)
    monkeypatch.setattr(
        tool_planning,
        "_durable_checkpoint_contract_matches",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        tool_planning,
        "_complete_artifact_fingerprint",
        lambda *_a, **_k: envelope["source_artifact_hash"],
    )
    monkeypatch.setattr(tool_planning, "get_bot_dir", lambda *_a: source)
    monkeypatch.setattr(tool_planning, "_get_ui", lambda: _UI())
    monkeypatch.setattr(llm_availability_store, "active_llm_pause", lambda: None)
    monkeypatch.setattr(
        llm_query,
        "is_operator_shutdown_requested",
        lambda: shutdown_requested,
    )

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError("provider process received SIGTERM")

    monkeypatch.setattr(tool_planning, "_execute_workers", cancelled)
    if persistence_failure:
        monkeypatch.setattr(
            WorkerWorkflow,
            "operator_shutdown_interrupted",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("journal unavailable")),
        )


def test_controlled_worker_shutdown_is_attempt_neutral_and_restart_claims(
    tmp_path,
    monkeypatch,
):
    workflow, checkpoint, envelope = _prepared_worker(tmp_path)
    _install_execution_boundary(
        monkeypatch,
        tmp_path,
        checkpoint,
        envelope,
        shutdown_requested=True,
    )

    result = _payload(asyncio.run(tool_planning._run_durable_worker_effect(
        workflow,
        envelope,
        tmp_path / "canonical-unused",
        "worker-template",
    )))

    assert result["error"] == "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED"
    assert result["attempt_consumed"] is False
    assert result["claimed_attempt"] == 1
    assert result["restored_attempt"] == 0
    assert result["workflow_run_id"] == checkpoint["workflow_run_id"]
    assert workflow.state()["status"] == "shutdown_interrupted"
    effect = workflow.store.effect(result["effect_id"])
    assert effect["status"] == "retry"
    assert effect["attempt"] == 0
    assert effect["lease_owner"] is None
    assert not any(
        event.event_type == "EffectFailed"
        for event in workflow.store.events(workflow.run_id)
    )

    restarted = WorkerWorkflow(
        store=WorkflowStore(workflow.store.path),
        artifacts=WorkerArtifactStore(workflow.artifacts.root),
        run_id=workflow.run_id,
    )
    reclaimed = restarted.request_or_claim(
        owner="pid:restart",
        lease_seconds=3600,
    )
    assert reclaimed.attempt == 1
    assert reclaimed.lease_epoch == result["lease_epoch"] + 1


def test_unexpected_worker_sigterm_consumes_infrastructure_attempt(
    tmp_path,
    monkeypatch,
):
    workflow, checkpoint, envelope = _prepared_worker(tmp_path)
    _install_execution_boundary(
        monkeypatch,
        tmp_path,
        checkpoint,
        envelope,
        shutdown_requested=False,
    )

    result = _payload(asyncio.run(tool_planning._run_durable_worker_effect(
        workflow,
        envelope,
        tmp_path / "canonical-unused",
        "worker-template",
    )))

    assert result["error"] == "DURABLE_WORKER_HARNESS_FAILED"
    assert result["action"] == "retry_same_tool"
    effect = workflow.store.effect(workflow.state()["effect_id"])
    assert effect["status"] == "retry"
    assert effect["attempt"] == 1
    failed = [
        event for event in workflow.store.events(workflow.run_id)
        if event.event_type == "EffectFailed"
    ]
    assert len(failed) == 1
    assert "CancelledError" in failed[0].payload["error"]
    assert not any(
        event.event_type == "EffectInterrupted"
        for event in workflow.store.events(workflow.run_id)
    )


def test_controlled_shutdown_journal_failure_preserves_running_lease_fail_closed(
    tmp_path,
    monkeypatch,
):
    workflow, checkpoint, envelope = _prepared_worker(tmp_path)
    _install_execution_boundary(
        monkeypatch,
        tmp_path,
        checkpoint,
        envelope,
        shutdown_requested=True,
        persistence_failure=True,
    )

    result = _payload(asyncio.run(tool_planning._run_durable_worker_effect(
        workflow,
        envelope,
        tmp_path / "canonical-unused",
        "worker-template",
    )))

    assert result["error"] == "WORKER_OPERATOR_SHUTDOWN_PERSIST_FAILED"
    assert result["recovery_blocked"] is True
    assert result["attempt_neutral_persisted"] is False
    effect = workflow.store.effect(result["effect_id"])
    assert effect["status"] == "running"
    assert effect["attempt"] == 1
    assert effect["lease_owner"] == f"pid:{os.getpid()}"
    assert not any(
        event.event_type in {"EffectFailed", "EffectInterrupted"}
        for event in workflow.store.events(workflow.run_id)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"shutdown_requested": False},
        {"attempt_consumed": True},
        {"restored_attempt": 0},
        {"workflow_run_id": "foreign"},
        {"lease_epoch": 0},
    ],
)
def test_orchestrator_rejects_malformed_worker_shutdown_projection(mutation):
    checkpoint = {"workflow_run_id": "149#0"}
    payload = {
        "error": "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED",
        "success": False,
        "failure_class": "operator_shutdown",
        "action": "retry_same_tool",
        "pending": True,
        "shutdown_requested": True,
        "checkpoint_preserved": True,
        "attempt_consumed": False,
        "attempt_neutral_persisted": True,
        "workflow_run_id": "149#0",
        "effect_id": "worker:149#0:cycle-0:deadbeef",
        "lease_epoch": 1,
        "claimed_attempt": 2,
        "restored_attempt": 1,
        "max_attempts": 3,
    }
    payload.update(mutation)

    import orchestrator

    assert not orchestrator._is_worker_operator_shutdown_interrupted(
        payload,
        checkpoint,
    )


def test_orchestrator_accepts_exact_worker_shutdown_projection():
    import orchestrator

    assert orchestrator._is_worker_operator_shutdown_interrupted(
        {
            "error": "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED",
            "success": False,
            "failure_class": "operator_shutdown",
            "action": "retry_same_tool",
            "pending": True,
            "shutdown_requested": True,
            "checkpoint_preserved": True,
            "attempt_consumed": False,
            "attempt_neutral_persisted": True,
            "workflow_run_id": "149#0",
            "effect_id": "worker:149#0:cycle-0:deadbeef",
            "lease_epoch": 4,
            "claimed_attempt": 2,
            "restored_attempt": 1,
            "max_attempts": 3,
        },
        {"workflow_run_id": "149#0"},
    )


def test_deterministic_route_preserves_interrupted_worker_for_restart(
    monkeypatch,
):
    import orchestrator

    checkpoint = {
        "workflow_run_id": "149#0",
        "next_v": 149,
        "source_v": 143,
        "stage": "master_planned",
    }
    payload = {
        "error": "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED",
        "success": False,
        "failure_class": "operator_shutdown",
        "action": "retry_same_tool",
        "pending": True,
        "shutdown_requested": True,
        "checkpoint_preserved": True,
        "attempt_consumed": False,
        "attempt_neutral_persisted": True,
        "workflow_run_id": "149#0",
        "effect_id": "worker:149#0:cycle-0:deadbeef",
        "lease_epoch": 4,
        "claimed_attempt": 2,
        "restored_attempt": 1,
        "max_attempts": 3,
    }

    async def handler(_args):
        return {
            "content": [{
                "type": "text",
                "text": json.dumps(payload),
            }]
        }

    monkeypatch.setattr(
        orchestrator,
        "_resolve_recovery_route",
        lambda _checkpoint: {
            "next_tool": "execute_workers",
            "next_v": 149,
            "source_v": 143,
            "parent2_v": None,
            "stage": "master_planned",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_deterministic_route_handler_and_args",
        lambda *_a, **_k: (handler, {}),
    )
    monkeypatch.setattr(
        orchestrator,
        "_deterministic_route_requires_llm",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        orchestrator,
        "_bind_generation_cost_runtime",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_check_generation_cost_policy",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *_a, **_k: None)
    outcome = {}
    ui = _UI()

    routed = asyncio.run(orchestrator._try_deterministic_checkpoint_route(
        {"action": "resume", "checkpoint": checkpoint},
        ui,
        shutdown_mgr=SimpleNamespace(is_shutting_down=True),
        outcome=outcome,
    ))

    assert routed is False
    assert outcome["result"] == payload
    assert any(
        "same frozen activity will be reclaimed" in message
        for message, _level in ui.history
    )


class _ShutdownEdge:
    def __init__(self):
        self.requested = False

    @property
    def is_shutting_down(self):
        return self.requested

    async def wait_for_shutdown(self):
        while not self.requested:
            await asyncio.sleep(0)


def _shutdown_projection(checkpoint):
    return {
        "error": "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED",
        "success": False,
        "failure_class": "operator_shutdown",
        "action": "retry_same_tool",
        "pending": True,
        "shutdown_requested": True,
        "checkpoint_preserved": True,
        "attempt_consumed": False,
        "attempt_neutral_persisted": True,
        "workflow_run_id": checkpoint["workflow_run_id"],
        "effect_id": "worker:149#0:cycle-0:deadbeef",
        "lease_epoch": 4,
        "claimed_attempt": 2,
        "restored_attempt": 1,
        "max_attempts": 3,
    }


@pytest.mark.asyncio
async def test_one_gen_shutdown_interruption_never_opens_provider_stream(
    monkeypatch,
    tmp_path,
):
    import orchestrator

    checkpoint = {
        "workflow_run_id": "149#0",
        "checkpoint_revision": 8,
        "next_v": 149,
        "source_v": 143,
        "parent2_v": None,
        "stage": "master_planned",
    }
    recovery = {"action": "resume", "checkpoint": checkpoint}
    shutdown = _ShutdownEdge()
    provider_calls = []

    async def route(_recovery, _ui=None, *, outcome=None, **_kwargs):
        shutdown.requested = True
        outcome.update({
            "checkpoint": checkpoint,
            "route": {"next_tool": "execute_workers"},
            "result": _shutdown_projection(checkpoint),
        })
        return False

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        raise AssertionError("shutdown interruption opened a provider stream")

    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_a, **_k: recovery,
    )
    monkeypatch.setattr(orchestrator, "_run_one_cycle", provider)

    cost = await orchestrator._run_one_generation_cli_impl(
        log_file=tmp_path / "one-gen-shutdown.log",
        max_turns=None,
        shutdown_mgr=shutdown,
        cost_policy=None,
        startup_recovery=recovery,
    )

    assert cost == orchestrator.SHUTDOWN_CANCEL_COST
    assert provider_calls == []


@pytest.mark.asyncio
async def test_advance_blocks_malformed_shutdown_without_provider_fallback(
    monkeypatch,
):
    import orchestrator

    checkpoint = {
        "workflow_run_id": "149#0",
        "checkpoint_revision": 8,
        "next_v": 149,
        "source_v": 143,
        "parent2_v": None,
        "stage": "master_planned",
    }
    recovery = {"action": "resume", "checkpoint": checkpoint}
    shutdown = _ShutdownEdge()

    async def route(_recovery, _ui=None, *, outcome=None, **_kwargs):
        shutdown.requested = True
        malformed = _shutdown_projection(checkpoint)
        malformed["restored_attempt"] = 0
        outcome.update({
            "checkpoint": checkpoint,
            "route": {"next_tool": "execute_workers"},
            "result": malformed,
        })
        return False

    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_a, **_k: recovery,
    )

    advanced = await orchestrator._advance_deterministic_recovery(
        recovery,
        None,
        cost_policy=None,
        shutdown_mgr=shutdown,
    )

    assert advanced["routed"] is True
    assert advanced["terminal_action"] == "operator_shutdown_projection_invalid"
    assert advanced["recovery"]["action"] == "blocked"


@pytest.mark.asyncio
async def test_advance_rejects_shutdown_projection_from_non_worker_route(monkeypatch):
    import orchestrator

    checkpoint = {
        "workflow_run_id": "149#0",
        "checkpoint_revision": 8,
        "next_v": 149,
        "source_v": 143,
        "parent2_v": None,
        "stage": "master_planned",
    }
    recovery = {"action": "resume", "checkpoint": checkpoint}
    shutdown = _ShutdownEdge()

    async def route(_recovery, _ui=None, *, outcome=None, **_kwargs):
        shutdown.requested = True
        outcome.update({
            "checkpoint": checkpoint,
            "route": {"next_tool": "run_master"},
            "result": _shutdown_projection(checkpoint),
        })
        return False

    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_a, **_k: recovery,
    )

    advanced = await orchestrator._advance_deterministic_recovery(
        recovery,
        None,
        cost_policy=None,
        shutdown_mgr=shutdown,
    )

    assert advanced["routed"] is True
    assert advanced["terminal_action"] == "operator_shutdown_projection_invalid"
    assert advanced["recovery"]["action"] == "blocked"


@pytest.mark.asyncio
async def test_continuous_shutdown_interruption_never_opens_provider_stream(
    monkeypatch,
    tmp_path,
):
    import epoch_authority
    import llm_query
    import orchestrator
    import rate_limiter
    import stability_observation
    import tools

    checkpoint = {
        "workflow_run_id": "149#0",
        "checkpoint_revision": 8,
        "next_v": 149,
        "source_v": 143,
        "parent2_v": None,
        "stage": "master_planned",
    }
    recovery = {"action": "resume", "checkpoint": checkpoint}
    shutdown = _ShutdownEdge()
    provider_calls = []

    async def route(_recovery, _ui=None, *, outcome=None, **_kwargs):
        shutdown.requested = True
        outcome.update({
            "checkpoint": checkpoint,
            "route": {"next_tool": "execute_workers"},
            "result": _shutdown_projection(checkpoint),
        })
        return False

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        raise AssertionError("shutdown interruption opened a provider stream")

    async def dormant(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        epoch_authority,
        "require_policy_epoch_initialized",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(stability_observation, "bind_runtime_configuration", lambda *_a: None)
    monkeypatch.setattr(tools, "inject_ui", lambda *_a: None)
    monkeypatch.setattr(orchestrator, "set_system_log_ui", lambda *_a: None)
    monkeypatch.setattr(llm_query, "set_shutdown_manager", lambda *_a, **_k: True)
    monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(orchestrator, "_rotate_orchestrator_logs", lambda *_a: None)
    monkeypatch.setattr(orchestrator, "load_llm_pause", lambda: None)
    monkeypatch.setattr(orchestrator, "consume_operator_resume_ack_from_env", lambda: None)
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "_runtime_branch_guard_enabled", lambda: False)
    monkeypatch.setattr(
        orchestrator,
        "_runtime_git_identity",
        lambda: {"branch": "main", "head": "a" * 40},
    )
    monkeypatch.setattr(orchestrator, "_set_runtime_expected_head", lambda value: value)
    monkeypatch.setattr(
        orchestrator,
        "load_operator_generation_cost_policy",
        lambda: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "configure_runtime_cost_policy",
        lambda _policy: SimpleNamespace(receipt=lambda: {}),
    )
    monkeypatch.setattr(
        orchestrator,
        "_stability_projection_maintenance_coroutine",
        dormant,
    )
    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", dormant)
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_a, **_k: recovery,
    )
    monkeypatch.setattr(orchestrator, "_run_one_cycle", provider)

    outcome = await orchestrator.orchestrator_loop(
        _UI(),
        shutdown_mgr=shutdown,
        no_daemon=True,
        startup_recovery=recovery,
    )

    assert outcome == 0.0
    assert provider_calls == []
