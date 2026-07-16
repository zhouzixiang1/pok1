from __future__ import annotations

import asyncio
import json

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    UserMessage,
)
import pytest

import evolution_infra
import llm_availability_store as pause_store
import orchestrator


class _UI:
    def __init__(self):
        self.gen_cost_total = 0.0
        self.history = []
        self.status = []

    def log_history(self, message, level="info"):
        self.history.append((level, message))

    def log_io(self, *_args, **_kwargs):
        return None

    def emit_tool_call(self, *_args, **_kwargs):
        return None

    def update_cost(self, *_args, **_kwargs):
        return None

    def set_status(self, message, is_working=False):
        self.status.append((message, is_working))


@pytest.fixture
def isolated_cycle(monkeypatch, tmp_path):
    import evolution_core

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.delenv(pause_store.RESUME_ENV, raising=False)
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(orchestrator, "_build_context", lambda **_kwargs: "")
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(orchestrator, "_save_orchestrator_session", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_check_generation_cost_policy", lambda *_args: None)
    monkeypatch.setattr(
        orchestrator,
        "record_generation_cost",
        lambda *_args, **_kwargs: {"active": False, "recorded": False},
    )
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *_args, **_kwargs: None)
    clears = []
    monkeypatch.setattr(
        orchestrator,
        "_clear_orchestrator_session",
        lambda reason=None: clears.append(reason),
    )
    return tmp_path, clears


def _billing_result():
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="session-403",
        total_cost_usd=0.0,
        result="Claude Code returned an error result: success",
        errors=[],
        api_error_status=403,
    )


def test_orchestrator_stream_preserves_textblock_403_over_generic_result(
    isolated_cycle, monkeypatch
):
    tmp_path, clears = isolated_cycle

    async def stream():
        yield AssistantMessage(
            content=[
                TextBlock(
                    text=(
                        "HTTP 403: You've reached your usage limit for this "
                        "billing cycle"
                    )
                )
            ],
            model="sonnet",
        )
        yield _billing_result()

    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: stream())
    ui = _UI()
    result = asyncio.run(
        orchestrator._run_one_cycle(
            ui=ui,
            log_file=tmp_path / "orchestrator.log",
            gen_ctx=None,
        )
    )

    assert result == orchestrator.ORCH_LLM_AVAILABILITY_BLOCKED_COST
    pause = pause_store.load_llm_pause()
    assert pause["active"] is True
    assert pause["category"] == "billing_cycle_usage_limit"
    assert pause["http_status"] == 403
    assert "llm_availability_blocked" in clears
    assert not any("infrastructure error" in msg.lower() for _level, msg in ui.history)


def test_orchestrator_converts_trailing_generic_exception_using_prior_403_text(
    isolated_cycle, monkeypatch
):
    tmp_path, _clears = isolated_cycle

    async def stream():
        yield AssistantMessage(
            content=[TextBlock(text="403 usage limit for this billing cycle")],
            model="sonnet",
        )
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: stream())
    result = asyncio.run(
        orchestrator._run_one_cycle(
            ui=_UI(),
            log_file=tmp_path / "orchestrator-trailing.log",
            gen_ctx=None,
        )
    )

    assert result == orchestrator.ORCH_LLM_AVAILABILITY_BLOCKED_COST
    assert pause_store.load_llm_pause()["category"] == "billing_cycle_usage_limit"


def test_user_tool_result_stops_on_nested_persisted_pause(
    isolated_cycle,
    monkeypatch,
):
    from llm_availability import classify_llm_availability

    tmp_path, clears = isolated_cycle
    issue = classify_llm_availability(
        ["HTTP 403 usage limit for this billing cycle"],
        statuses=[403],
    )
    pause_store.persist_llm_pause(issue)
    continued = {"value": False}

    async def stream():
        yield UserMessage(content=[ToolResultBlock(
            tool_use_id="nested-master",
            content=json.dumps({
                "success": False,
                "error": "legacy_master_llm_unavailable",
            }),
            is_error=False,
        )])
        continued["value"] = True
        yield AssistantMessage(content=[TextBlock(text="must not continue")], model="sonnet")

    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: stream())

    result = asyncio.run(orchestrator._run_one_cycle(
        ui=_UI(),
        log_file=tmp_path / "orchestrator-nested-pause.log",
        gen_ctx=None,
    ))

    assert result == orchestrator.ORCH_LLM_AVAILABILITY_BLOCKED_COST
    assert continued["value"] is False
    assert "llm_availability_blocked" in clears


def test_orchestrator_does_not_retry_normal_overload_discussion_as_529(
    isolated_cycle,
    monkeypatch,
):
    tmp_path, _clears = isolated_cycle
    calls = 0

    async def stream():
        yield AssistantMessage(
            content=[
                TextBlock(
                    text=(
                        "We should document the overloaded-provider recovery "
                        "policy for future maintainers."
                    )
                )
            ],
            model="sonnet",
        )
        yield ResultMessage(
            subtype="error_max_turns",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="unrelated-error",
            total_cost_usd=0.0,
            errors=["maximum turns exceeded"],
        )

    def query(**_kwargs):
        nonlocal calls
        calls += 1
        return stream()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(orchestrator, "claude_query", query)
    monkeypatch.setattr(orchestrator.asyncio, "sleep", no_sleep)
    asyncio.run(
        orchestrator._run_one_cycle(
            ui=_UI(),
            log_file=tmp_path / "orchestrator-normal-discussion.log",
            gen_ctx=None,
        )
    )

    assert calls == 1
    assert pause_store.load_llm_pause() is None


def test_manual_pause_guard_never_sleeps_or_retries(isolated_cycle, monkeypatch):
    _tmp_path, _clears = isolated_cycle
    issue_text = "HTTP 403 usage limit for this billing cycle"
    from llm_availability import classify_llm_availability

    issue = classify_llm_availability([issue_text], statuses=[403])
    pause_store.persist_llm_pause(issue)

    async def forbidden_sleep(_seconds):
        raise AssertionError("manual pause must not enter a retry sleep")

    monkeypatch.setattr(orchestrator.asyncio, "sleep", forbidden_sleep)
    ui = _UI()
    allowed = asyncio.run(orchestrator._honor_active_llm_pause(ui, None))

    assert allowed is False
    assert ui.status[-1][1] is False
    assert issue.evidence_digest in ui.history[-1][1]


def _install_deterministic_worker_route(monkeypatch, payload):
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
            "next_v": 12,
            "source_v": 11,
            "stage": "master_planned",
            "parent2_v": None,
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_deterministic_route_handler_and_args",
        lambda *_args, **_kwargs: (handler, {"next_v": 12, "source_v": 11}),
    )
    monkeypatch.setattr(
        orchestrator,
        "_deterministic_route_requires_llm",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(orchestrator, "_bind_generation_cost_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "_check_generation_cost_policy", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *_a, **_k: None)


@pytest.mark.parametrize(
    "error",
    [
        "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
        "WORKER_AVAILABILITY_DEFER_FAILED",
        "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID",
    ],
)
def test_deterministic_worker_route_fails_closed_on_availability_control_result(
    isolated_cycle,
    monkeypatch,
    error,
):
    _install_deterministic_worker_route(
        monkeypatch,
        {"error": error, "success": False, "action": "operator_reconcile"},
    )
    ui = _UI()
    with pytest.raises(pause_store.LLMAvailabilityPauseError):
        asyncio.run(orchestrator._try_deterministic_checkpoint_route(
            {
                "action": "resume",
                "checkpoint": {
                    "next_v": 12,
                    "source_v": 11,
                    "stage": "master_planned",
                },
            },
            ui,
        ))

    assert ui.status[-1] == ("Stopped: LLM pause control invalid", False)
    assert any("failed closed" in message for _level, message in ui.history)


def test_deterministic_worker_block_result_activates_typed_global_pause(
    isolated_cycle,
    monkeypatch,
):
    from llm_availability import build_llm_pause_state, classify_llm_availability

    issue = classify_llm_availability(
        ["Failed to authenticate. API Error: 403 Invalid token"]
    )
    assert issue is not None
    _install_deterministic_worker_route(
        monkeypatch,
        {
            "error": "LLM_AVAILABILITY_BLOCKED",
            "success": False,
            "action": "wait_for_llm_availability",
            "availability": build_llm_pause_state(issue, role="worker"),
        },
    )
    ui = _UI()
    handled = asyncio.run(orchestrator._try_deterministic_checkpoint_route(
        {
            "action": "resume",
            "checkpoint": {
                "next_v": 12,
                "source_v": 11,
                "stage": "master_planned",
            },
        },
        ui,
    ))

    assert handled is False
    pause = pause_store.load_llm_pause()
    assert pause["active"] is True
    assert pause["category"] == "invalid_auth"


def test_deterministic_route_pause_persistence_failure_raises_terminal_control(
    isolated_cycle,
    monkeypatch,
):
    from llm_availability import build_llm_pause_state, classify_llm_availability

    issue = classify_llm_availability(
        ["Failed to authenticate. API Error: 403 Invalid token"]
    )
    assert issue is not None
    _install_deterministic_worker_route(
        monkeypatch,
        {
            "error": "LLM_AVAILABILITY_BLOCKED",
            "success": False,
            "action": "wait_for_llm_availability",
            "availability": build_llm_pause_state(issue, role="worker"),
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "persist_llm_pause",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(
        pause_store.LLMAvailabilityPauseError,
        match="could not persist",
    ):
        asyncio.run(orchestrator._try_deterministic_checkpoint_route(
            {
                "action": "resume",
                "checkpoint": {
                    "next_v": 12,
                    "source_v": 11,
                    "stage": "master_planned",
                },
            },
            _UI(),
        ))
