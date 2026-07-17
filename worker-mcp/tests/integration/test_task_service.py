from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import pytest

from conftest import run_git
from worker_mcp.agent_executor import (
    AgentCancelled,
    AgentExecution,
    AgentExecutionError,
    AgentTimedOut,
    MockAgentExecutor,
)
from worker_mcp.persistence import IdempotencyConflict
from worker_mcp.schemas import (
    ExecutionProfile,
    ListTasksRequest,
    TaskEnvelope,
    TaskStatus,
    TaskType,
    WorkerReportedResult,
)
from worker_mcp.task_service import TaskService, _iter_original_strings


def request(git_repo, *, key="service-test-0001", read_only=True, task_type=TaskType.ANALYZE):
    return TaskEnvelope(
        goal="inspect or patch source",
        context="integration test",
        repo=str(git_repo),
        base_commit=run_git(git_repo, "rev-parse", "HEAD"),
        allowed_paths=["src", "tests"],
        forbidden_paths=["archive"],
        constraints=[],
        acceptance_criteria=["return evidence"],
        execution=ExecutionProfile(
            read_only=read_only, use_worktree=True, max_turns=4, timeout_sec=30
        ),
        idempotency_key=key,
        task_type=task_type,
    )


async def wait_terminal(service: TaskService, task_id: str, timeout=10):
    async with asyncio.timeout(timeout):
        while True:
            status = service.status(task_id)
            if status.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.TIMED_OUT,
                TaskStatus.NEEDS_REVIEW,
            }:
                return status
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_read_task_async_success_and_strict_idempotency(worker_config, git_repo):
    service = TaskService(worker_config, executor=MockAgentExecutor())
    await service.start()
    try:
        first = await service.submit(request(git_repo))
        replay = await service.submit(request(git_repo))
        assert replay.task_id == first.task_id and replay.idempotent_replay
        status = await wait_terminal(service, first.task_id)
        assert status.status is TaskStatus.SUCCEEDED and status.attempt == 1
        result = service.result(first.task_id)
        assert result.status.value == "succeeded"
        assert result.files_changed == []
        with pytest.raises(IdempotencyConflict):
            await service.submit(
                request(git_repo).model_copy(update={"goal": "different request"})
            )
        with pytest.raises(IdempotencyConflict):
            await service.submit(
                request(git_repo).model_copy(
                    update={"context": "different execution evidence"}
                )
            )
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_terminal_history_requires_explicit_list_opt_in(worker_config, git_repo):
    service = TaskService(worker_config, executor=MockAgentExecutor())
    await service.start()
    try:
        submitted = await service.submit(
            request(git_repo, key="service-list-history-0001")
        )
        assert (await wait_terminal(service, submitted.task_id)).status is TaskStatus.SUCCEEDED

        assert service.list(ListTasksRequest()).tasks == []
        history = service.list(ListTasksRequest(include_terminal=True)).tasks
        assert [item.task_id for item in history] == [submitted.task_id]
        explicit = service.list(
            ListTasksRequest(status=TaskStatus.SUCCEEDED)
        ).tasks
        assert [item.task_id for item in explicit] == [submitted.task_id]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_write_task_returns_actual_diff_without_touching_primary(worker_config, git_repo):
    primary_before = run_git(git_repo, "status", "--porcelain")

    async def patch(request, worktree, calls, cancel_event):
        assert calls == 1 and not cancel_event.is_set()
        (worktree / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        return AgentExecution(
            reported=WorkerReportedResult(
                summary="updated value", acceptance_result="patch generated"
            ),
            audit={
                "files_read": [str(worktree / "src" / "module.py")],
                "commands": [
                    {
                        "command": "git diff --check",
                        "exit_code": 0,
                        "duration_ms": 1,
                        "allowed": True,
                    }
                ],
                "denied": [],
            },
            session_id="mock-write",
            turns=2,
            duration_ms=5,
        )

    service = TaskService(worker_config, executor=MockAgentExecutor(patch))
    await service.start()
    try:
        submitted = await service.submit(
            request(
                git_repo,
                key="service-write-0001",
                read_only=False,
                task_type=TaskType.PATCH,
            )
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.SUCCEEDED
        result = service.result(submitted.task_id)
        assert result.files_changed == ["src/module.py"]
        assert "VALUE = 2" in result.diff
        assert run_git(git_repo, "status", "--porcelain") == primary_before
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_read_task_retries_once_after_executor_failure(worker_config, git_repo):
    async def flaky(request, worktree, calls, cancel_event):
        if calls == 1:
            raise AgentExecutionError("simulated SDK crash")
        return AgentExecution(
            reported=WorkerReportedResult(summary="recovered", acceptance_result="ok"),
            audit={
                "files_read": [str(worktree / "src" / "module.py")],
                "commands": [],
                "denied": [],
            },
            session_id="retry",
            turns=1,
            duration_ms=1,
        )

    executor = MockAgentExecutor(flaky)
    service = TaskService(worker_config, executor=executor)
    await service.start()
    try:
        submitted = await service.submit(
            request(git_repo, key="service-retry-0001")
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.SUCCEEDED
        assert status.attempt == 2 and executor.calls == 2
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_read_task_without_successful_read_evidence_fails(
    worker_config, git_repo
):
    async def unsupported_claim(request, worktree, calls, cancel_event):
        return AgentExecution(
            reported=WorkerReportedResult(
                summary="claimed without evidence", acceptance_result="claimed"
            ),
            audit={"files_read": [], "commands": [], "denied": []},
            session_id="no-evidence",
            turns=1,
            duration_ms=1,
        )

    service = TaskService(
        worker_config, executor=MockAgentExecutor(unsupported_claim)
    )
    await service.start()
    try:
        submitted = await service.submit(
            request(git_repo, key="service-no-read-evidence-0001")
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.FAILED
        assert "no successful file-read evidence" in service.result(
            submitted.task_id
        ).summary
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_runtime_failure_with_unverifiable_worktree_needs_review(
    worker_config, git_repo, monkeypatch
):
    async def failed_executor(request, worktree, calls, cancel_event):
        raise AgentExecutionError("retry would be unsafe without snapshot evidence")

    service = TaskService(worker_config, executor=MockAgentExecutor(failed_executor))

    def snapshot_failure(_path):
        raise RuntimeError("snapshot evidence unavailable")

    monkeypatch.setattr(service.worktrees, "snapshot", snapshot_failure)
    await service.start()
    try:
        submitted = await service.submit(
            request(git_repo, key="service-unverifiable-worktree-0001")
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.NEEDS_REVIEW
        assert service.executor.calls == 1
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_non_utf8_worktree_path_fails_closed_without_stuck_lease(
    worker_config, git_repo
):
    async def write_non_utf8_path(request, worktree, calls, cancel_event):
        filename = os.fsencode(worktree / "src") + b"/bad-\xff.py"
        descriptor = os.open(filename, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, b"VALUE = 1\n")
        finally:
            os.close(descriptor)
        return AgentExecution(
            reported=WorkerReportedResult(
                summary="created an invalid path", acceptance_result="claimed"
            ),
            audit={"files_read": [], "commands": [], "denied": []},
            session_id="non-utf8-path",
            turns=1,
            duration_ms=1,
        )

    service = TaskService(
        worker_config, executor=MockAgentExecutor(write_non_utf8_path)
    )
    await service.start()
    try:
        submitted = await service.submit(
            request(
                git_repo,
                key="service-non-utf8-path-0001",
                read_only=False,
                task_type=TaskType.PATCH,
            )
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.NEEDS_REVIEW
        row = service.persistence.get_task(submitted.task_id)
        assert row["lease_owner"] is None
        assert service.result(submitted.task_id).status.value == "partial"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_cancelled_dirty_write_enters_needs_review(worker_config, git_repo):
    async def blocking(request, worktree, calls, cancel_event):
        (worktree / "src" / "module.py").write_text("VALUE = 9\n", encoding="utf-8")
        await cancel_event.wait()
        raise AgentCancelled("cancelled")

    service = TaskService(worker_config, executor=MockAgentExecutor(blocking))
    await service.start()
    try:
        submitted = await service.submit(
            request(
                git_repo,
                key="service-cancel-0001",
                read_only=False,
                task_type=TaskType.PATCH,
            )
        )
        async with asyncio.timeout(5):
            while service.status(submitted.task_id).status is not TaskStatus.RUNNING:
                await asyncio.sleep(0.05)
        cancelled = await service.cancel(submitted.task_id)
        assert cancelled.status is TaskStatus.NEEDS_REVIEW
        result = service.result(submitted.task_id)
        assert result.status.value == "partial"
        assert result.files_changed == ["src/module.py"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_cancel_flag_wins_even_if_executor_returns_normally(
    worker_config, git_repo
):
    started = asyncio.Event()
    release = asyncio.Event()

    async def ignores_cancel(request, worktree, calls, cancel_event):
        started.set()
        await release.wait()
        return AgentExecution(
            reported=WorkerReportedResult(
                summary="late success must not win", acceptance_result="claimed"
            ),
            audit={
                "files_read": [str(worktree / "src" / "module.py")],
                "commands": [],
                "denied": [],
            },
            session_id="late-success",
            turns=1,
            duration_ms=1,
        )

    service = TaskService(worker_config, executor=MockAgentExecutor(ignores_cancel))
    await service.start()
    try:
        submitted = await service.submit(
            request(git_repo, key="service-cancel-return-race-0001")
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        cancelling = asyncio.create_task(service.cancel(submitted.task_id))
        await asyncio.sleep(0.05)
        release.set()
        cancelled = await asyncio.wait_for(cancelling, timeout=5)
        assert cancelled.status is TaskStatus.CANCELLED
        assert service.status(submitted.task_id).status is TaskStatus.CANCELLED
    finally:
        release.set()
        await service.stop()


@pytest.mark.asyncio
async def test_timeout_without_diff_is_timed_out(worker_config, git_repo):
    async def timed(request, worktree, calls, cancel_event):
        raise AgentTimedOut("simulated timeout")

    service = TaskService(worker_config, executor=MockAgentExecutor(timed))
    await service.start()
    try:
        submitted = await service.submit(
            request(git_repo, key="service-timeout-0001")
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.TIMED_OUT
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_custom_named_credential_is_redacted_from_failure_state_and_audit(
    worker_config, git_repo, monkeypatch
):
    secret = "custom-credential-value-987654"
    monkeypatch.setenv("WORKER_MCP_CRED", secret)
    config = worker_config.model_copy(
        update={
            "gateway": worker_config.gateway.model_copy(
                update={"auth_token_env": "WORKER_MCP_CRED"}
            )
        }
    )

    async def leaking_failure(request, worktree, calls, cancel_event):
        (worktree / "src" / "leak.py").write_text(
            f"TOKEN = {secret!r}\n", encoding="utf-8"
        )
        raise RuntimeError(f"executor exposed {secret}")

    service = TaskService(config, executor=MockAgentExecutor(leaking_failure))
    await service.start()
    try:
        with pytest.raises(ValueError, match="contains the dedicated Worker credential"):
            await service.submit(
                request(
                    git_repo,
                    key="service-credential-in-envelope-0001",
                ).model_copy(update={"context": f"do not store {secret}"})
            )
        submitted = await service.submit(
            request(
                git_repo,
                key="service-custom-credential-redaction-0001",
                read_only=False,
                task_type=TaskType.PATCH,
            )
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.NEEDS_REVIEW
        result_json = service.result(submitted.task_id).model_dump_json()
        row = service.persistence.get_task(submitted.task_id)
        assert secret not in result_json
        assert secret not in (row["error_message"] or "")
    finally:
        await service.stop()
    audit_text = (config.state_dir / "logs" / "worker-mcp.jsonl").read_text(
        encoding="utf-8"
    )
    assert secret not in audit_text


@pytest.mark.asyncio
async def test_http_access_token_is_rejected_before_persistence_and_redacted_from_audit(
    worker_config, git_repo
):
    access_token = "local-http-access-token-" + "z" * 40
    service = TaskService(
        worker_config,
        executor=MockAgentExecutor(),
        additional_redaction_secrets=(access_token,),
    )
    await service.start()
    try:
        with pytest.raises(ValueError, match="contains the dedicated Worker credential"):
            await service.submit(
                request(
                    git_repo,
                    key="service-http-credential-in-envelope-0001",
                ).model_copy(update={"context": f"never persist {access_token}"})
            )
        service.audit.log("security.probe", message=access_token)
    finally:
        await service.stop()

    database = worker_config.state_dir / "tasks.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    audit = worker_config.state_dir / "logs" / "worker-mcp.jsonl"
    assert access_token.encode() not in database.read_bytes()
    audit_text = audit.read_text(encoding="utf-8")
    assert access_token not in audit_text
    assert "<redacted>" in audit_text


@pytest.mark.parametrize(
    ("secret", "location", "source"),
    [
        ('local-http-quote-"-credential-123456', "context", "additional"),
        (r"model-backslash-\-credential-123456", "context", "model"),
        ("local-http-control-\n-credential-123456", "context", "additional"),
        ("模型-访问凭据-credential-123456", "context", "model"),
        ('local-http-nested-"\\\n-凭据-123456', "constraints", "additional"),
    ],
    ids=["quote", "backslash", "control", "non-ascii", "nested-dict-list"],
)
@pytest.mark.asyncio
async def test_escaped_credential_is_rejected_before_any_durable_write(
    worker_config, git_repo, monkeypatch, secret, location, source
):
    config = worker_config
    additional_secrets = (secret,)
    if source == "model":
        credential_env = "WORKER_MCP_ESCAPED_TEST_TOKEN"
        monkeypatch.setenv(credential_env, secret)
        config = worker_config.model_copy(
            update={
                "gateway": worker_config.gateway.model_copy(
                    update={"auth_token_env": credential_env}
                )
            }
        )
        additional_secrets = ()
    service = TaskService(
        config,
        executor=MockAgentExecutor(),
        additional_redaction_secrets=additional_secrets,
    )
    await service.start()
    try:
        candidate = request(
            git_repo,
            key=f"service-escaped-credential-{source}-{location}-0001",
        )
        update = (
            {"context": f"never persist [{secret}]"}
            if location == "context"
            else {"constraints": ["ordinary", f"nested [{secret}]"]}
        )
        with pytest.raises(ValueError, match="contains the dedicated Worker credential"):
            await service.submit(candidate.model_copy(update=update))
        service.audit.log("security.probe", message=secret)
    finally:
        await service.stop()

    database = config.state_dir / "tasks.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert secret.encode("utf-8") not in database.read_bytes()

    audit = config.state_dir / "logs" / "worker-mcp.jsonl"
    audit_bytes = audit.read_bytes()
    assert secret.encode("utf-8") not in audit_bytes
    records = [json.loads(line) for line in audit_bytes.decode("utf-8").splitlines()]
    assert records == [{"event": "security.probe", "message": "<redacted>"}]


def test_original_string_walker_covers_mapping_keys_sequences_sets_and_cycles():
    cycle = []
    cycle.append(cycle)
    payload = {
        "mapping-value": ["list-value", ("tuple-value", {"set-value"})],
        "mapping-key": {"nested-key": "nested-value"},
        "cycle": cycle,
    }

    assert set(_iter_original_strings(payload)) == {
        "mapping-value",
        "list-value",
        "tuple-value",
        "set-value",
        "mapping-key",
        "nested-key",
        "nested-value",
        "cycle",
    }


@pytest.mark.asyncio
async def test_custom_credential_is_redacted_during_read_retry(
    worker_config, git_repo, monkeypatch
):
    secret = "retry-custom-credential-246810"
    monkeypatch.setenv("WORKER_MCP_CRED", secret)
    config = worker_config.model_copy(
        update={
            "gateway": worker_config.gateway.model_copy(
                update={"auth_token_env": "WORKER_MCP_CRED"}
            )
        }
    )
    recorded_retry_errors = []

    async def retry_then_wait(request, worktree, calls, cancel_event):
        if calls == 1:
            raise AgentExecutionError(f"transient failure exposed {secret}")
        return AgentExecution(
            reported=WorkerReportedResult(summary="recovered", acceptance_result="ok"),
            audit={
                "files_read": [str(worktree / "src" / "module.py")],
                "commands": [],
                "denied": [],
            },
            session_id="retry-redaction",
            turns=1,
            duration_ms=1,
        )

    service = TaskService(config, executor=MockAgentExecutor(retry_then_wait))
    original_transition = service.persistence.transition

    def recording_transition(task_id, target, **kwargs):
        if target is TaskStatus.QUEUED and kwargs.get("error_message"):
            recorded_retry_errors.append(kwargs["error_message"])
        return original_transition(task_id, target, **kwargs)

    monkeypatch.setattr(service.persistence, "transition", recording_transition)
    await service.start()
    try:
        submitted = await service.submit(
            request(git_repo, key="service-retry-credential-redaction-0001")
        )
        assert (await wait_terminal(service, submitted.task_id)).status is TaskStatus.SUCCEEDED
        assert len(recorded_retry_errors) == 1
        assert secret not in recorded_retry_errors[0]
        assert "[REDACTED]" in recorded_retry_errors[0]
    finally:
        await service.stop()
