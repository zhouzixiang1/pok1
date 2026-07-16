import asyncio
import json
import time

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKError, TextBlock


class _FakeProcess:
    def __init__(self):
        self.returncode = None


class _OwnedTransport:
    def __init__(self, stream_release=None):
        self._process = _FakeProcess()
        self._owned_process = self._process
        self.stream_release = stream_release
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self._owned_process.returncode = -15
        self._process = None
        if self.stream_release is not None:
            self.stream_release.set()


class _UnconfirmedTransport(_OwnedTransport):
    async def close(self):
        self.close_calls += 1
        await asyncio.Event().wait()


class _ResistantQuery:
    def __init__(self, release, messages=()):
        self.release = release
        self.messages = list(messages)
        self.running = False
        self.cancel_seen = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            return self.messages.pop(0)
        self.running = True
        try:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_seen = True
                await self.release.wait()
        finally:
            self.running = False
        raise StopAsyncIteration

    async def aclose(self):
        if self.running:
            raise RuntimeError("aclose(): asynchronous generator is already running")
        self.closed = True


class _SignatureCleanupFailureQuery:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise ClaudeSDKError(
            "Missing required field in assistant message: signature"
        )

    async def aclose(self):
        raise RuntimeError("synthetic generator cleanup failure")


class _ProviderEnvelopeQuery:
    def __init__(self, transport):
        self.transport = transport
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return AssistantMessage(
            content=[TextBlock(text="529 overloaded")],
            model="sonnet",
        )

    async def aclose(self):
        await self.transport.close()


class _UI:
    def __init__(self):
        self.gen_cost_total = 0.0
        self.history = []

    def log_history(self, message, level="info"):
        self.history.append((level, message))

    def log_io(self, *_args, **_kwargs):
        return None

    def emit_tool_call(self, *_args, **_kwargs):
        return None

    def update_cost(self, *_args, **_kwargs):
        return None

    def set_status(self, *_args, **_kwargs):
        return None


@pytest.fixture(autouse=True)
def _isolated_owned_provider_registry():
    import llm_query

    with llm_query._PROVIDER_CLEANUP_LOCK:
        llm_query._UNRESOLVED_PROVIDER_ATTEMPTS.clear()
    yield
    with llm_query._PROVIDER_CLEANUP_LOCK:
        llm_query._UNRESOLVED_PROVIDER_ATTEMPTS.clear()


@pytest.fixture
def isolated_cycle(monkeypatch, tmp_path):
    import evolution_core
    import evolution_infra
    import orchestrator
    import rate_limiter

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(orchestrator, "_build_context", lambda **_kwargs: "")
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(orchestrator, "_save_orchestrator_session", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_check_generation_cost_policy", lambda *_args: None)
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_handoff",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "record_generation_cost",
        lambda *_args, **_kwargs: {"active": False, "recorded": False},
    )
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)
    monkeypatch.setenv("POK_LLM_NEXT_CANCEL_GRACE", "0")
    monkeypatch.setenv("POK_ORCH_CYCLE_CANCEL_GRACE", "0")
    return tmp_path


def test_legacy_provider_session_sidecar_is_never_loaded_or_rewritten(
    tmp_path,
    monkeypatch,
):
    import orchestrator_session
    import system_log

    sidecar = tmp_path / "orchestrator_session.json"
    monkeypatch.setattr(
        orchestrator_session,
        "ORCHESTRATOR_SESSION_FILE",
        sidecar,
    )
    monkeypatch.setattr(system_log, "log_system_event", lambda *_args, **_kwargs: None)

    sidecar.write_text('{"session_id":"old-epoch-provider-history"}', encoding="utf-8")
    assert orchestrator_session._load_orchestrator_session() is None
    assert not sidecar.exists()

    orchestrator_session._save_orchestrator_session("new-opaque-session")
    assert not sidecar.exists()


@pytest.mark.asyncio
async def test_orchestrator_provider_options_reject_legacy_resume_identity(
    isolated_cycle,
    monkeypatch,
):
    import llm_query
    import orchestrator

    release = asyncio.Event()
    transport = _OwnedTransport(release)
    query = _ResistantQuery(release)
    options_seen = []

    def _transport(_prompt, options):
        options_seen.append(options)
        return transport

    monkeypatch.setattr(llm_query, "_new_owned_sdk_transport", _transport)
    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: query)
    monkeypatch.setattr(
        orchestrator,
        "_load_orchestrator_session",
        lambda: "old-epoch-provider-history",
    )
    monkeypatch.setattr(orchestrator, "ORCH_FIRST_ACTIVITY_TIMEOUT", 0.01)

    result = await orchestrator._run_one_cycle(
        ui=_UI(),
        log_file=isolated_cycle / "no-provider-resume.log",
        gen_ctx=None,
    )

    assert result == -0.5
    assert options_seen
    assert all(options.resume is None for options in options_seen)


@pytest.mark.asyncio
async def test_first_activity_resistant_stream_closes_exact_transport(
    isolated_cycle,
    monkeypatch,
):
    import llm_query
    import orchestrator

    release = asyncio.Event()
    transport = _OwnedTransport(release)
    query = _ResistantQuery(release)
    dispatches = []
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: transport,
    )
    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **kwargs: dispatches.append(kwargs) or query,
    )
    monkeypatch.setattr(orchestrator, "ORCH_FIRST_ACTIVITY_TIMEOUT", 0.01)

    started = time.monotonic()
    result = await orchestrator._run_one_cycle(
        ui=_UI(),
        log_file=isolated_cycle / "first-resistant.log",
        gen_ctx=None,
    )

    assert result == -0.5
    assert time.monotonic() - started < 1.0
    assert len(dispatches) == 1
    assert dispatches[0]["transport"] is transport
    assert transport.close_calls == 1
    assert transport._owned_process.returncode == -15
    assert query.cancel_seen is True
    assert query.running is False
    assert query.closed is True
    llm_query._assert_no_unresolved_provider_attempts()


@pytest.mark.asyncio
async def test_midstream_resistant_stream_stall_is_bounded_and_owned(
    isolated_cycle,
    monkeypatch,
):
    import llm_query
    import orchestrator

    release = asyncio.Event()
    transport = _OwnedTransport(release)
    query = _ResistantQuery(
        release,
        messages=(
            AssistantMessage(
                content=[TextBlock(text="first")],
                model="sonnet",
            ),
        ),
    )
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: transport,
    )
    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: query)
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_POLL_INTERVAL", 0.005)
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_STALL_TIMEOUT", 0.02)
    monkeypatch.setattr(orchestrator, "ORCH_ACTIONABLE_STAGE_TIMEOUT", 0)

    started = time.monotonic()
    result = await orchestrator._run_one_cycle(
        ui=_UI(),
        log_file=isolated_cycle / "mid-resistant.log",
        gen_ctx=None,
    )

    assert result == -0.5
    assert time.monotonic() - started < 1.0
    assert transport.close_calls == 1
    assert transport._owned_process.returncode == -15
    assert query.cancel_seen is True
    assert query.running is False
    assert query.closed is True
    llm_query._assert_no_unresolved_provider_attempts()


@pytest.mark.asyncio
async def test_cycle_timeout_cancels_owner_and_confirms_exact_process(
    isolated_cycle,
    monkeypatch,
):
    import llm_query
    import orchestrator

    release = asyncio.Event()
    transport = _OwnedTransport(release)
    query = _ResistantQuery(release)
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: transport,
    )
    attempt = llm_query.create_owned_provider_attempt(
        "prompt",
        ClaudeAgentOptions(),
    )
    attempt_ref = [attempt]
    gen_ref = [query]

    async def owner():
        with llm_query.owned_provider_attempt_scope(attempt):
            try:
                await llm_query.await_provider_stream_next_bounded(query, 60)
            finally:
                await llm_query.cleanup_owned_provider_attempt(
                    query,
                    attempt,
                    "ORCHESTRATOR",
                    isolated_cycle / "cycle-resistant.log",
                )

    with pytest.raises(asyncio.TimeoutError) as exc:
        await orchestrator._await_orchestrator_stream_response_bounded(
            owner(),
            timeout=0.01,
            attempt_ref=attempt_ref,
            gen_ref=gen_ref,
            log_file_path=isolated_cycle / "cycle-resistant.log",
        )

    assert isinstance(exc.value.__cause__, llm_query.LLMProviderCleanupError)
    assert exc.value.__cause__.provider_exit_confirmed is True
    assert transport.close_calls == 1
    assert transport._owned_process.returncode == -15
    assert query.running is False
    assert query.closed is True
    llm_query._assert_no_unresolved_provider_attempts()


@pytest.mark.asyncio
async def test_stream_cancellation_revokes_native_dispatch_before_task_exit(
    isolated_cycle,
    monkeypatch,
):
    """A detached native callback cannot retain a cancelled stream's nonce."""

    import orchestrator
    import pipeline_state

    nonce = "c" * 32
    monkeypatch.setenv("POK_ORCH_CYCLE_CANCEL_GRACE", "0.01")
    heartbeat = isolated_cycle / "cancel-native-heartbeat.json"
    heartbeat.write_text(
        json.dumps({
            "native_match_progress": {
                "provider_dispatch_nonce": nonce,
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pipeline_state,
        "PIPELINE_RUNTIME_HEARTBEAT_FILE",
        heartbeat,
    )
    token = pipeline_state.activate_native_match_dispatch_nonce(nonce)
    pipeline_state._NATIVE_MATCH_TERMINAL_HANDOFFS[nonce] = {"sentinel": True}
    parked = asyncio.Event()

    async def owner():
        await parked.wait()

    task = asyncio.create_task(owner())
    try:
        await asyncio.sleep(0)
        assert pipeline_state.native_match_dispatch_nonce_is_active(nonce)
        assert await orchestrator._cancel_orchestrator_stream_task_bounded(
            task,
            attempt_ref=[{"attempt_id": nonce}],
            gen_ref=[None],
            reason="test_native_dispatch_revocation",
            log_file_path=isolated_cycle / "native-dispatch-revocation.log",
        ) is None
        assert task.done()
        assert not pipeline_state.native_match_dispatch_nonce_is_active(nonce)
        assert nonce not in pipeline_state._NATIVE_MATCH_TERMINAL_HANDOFFS
        assert not heartbeat.exists()
    finally:
        pipeline_state.reset_native_match_dispatch_nonce(token)
        pipeline_state.revoke_native_match_dispatch_nonce(nonce)


@pytest.mark.asyncio
async def test_unconfirmed_signature_cleanup_blocks_retry_and_next_cycle(
    isolated_cycle,
    monkeypatch,
):
    import llm_query
    import orchestrator

    transport = _UnconfirmedTransport()
    query = _SignatureCleanupFailureQuery()
    dispatches = []
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: transport,
    )
    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **kwargs: dispatches.append(kwargs) or query,
    )
    monkeypatch.setenv("POK_LLM_TRANSPORT_CLOSE_TIMEOUT", "0.01")

    first = await orchestrator._run_one_cycle(
        ui=_UI(),
        log_file=isolated_cycle / "signature-unconfirmed.log",
        gen_ctx=None,
    )
    second = await orchestrator._run_one_cycle(
        ui=_UI(),
        log_file=isolated_cycle / "next-cycle-blocked.log",
        gen_ctx=None,
    )

    assert first == -0.5
    assert second == -0.5
    assert len(dispatches) == 1
    assert transport.close_calls == 1
    with pytest.raises(llm_query.LLMProviderCleanupBlocked):
        llm_query._assert_no_unresolved_provider_attempts()

    transport._owned_process.returncode = -9
    transport._process = None
    llm_query._assert_no_unresolved_provider_attempts()


@pytest.mark.asyncio
async def test_unresolved_attempt_blocks_529_retry_before_dispatch(
    isolated_cycle,
    monkeypatch,
):
    import llm_query
    import orchestrator

    main_transport = _OwnedTransport()
    orphan_transport = _OwnedTransport()
    transports = iter((main_transport, orphan_transport))
    dispatches = []
    orphan_ref = [None]
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: next(transports),
    )

    def query_factory(**kwargs):
        dispatches.append(kwargs)
        return _ProviderEnvelopeQuery(kwargs["transport"])

    async def inject_orphan(_delay):
        if orphan_ref[0] is None:
            orphan_ref[0] = llm_query.create_owned_provider_attempt(
                "orphan",
                ClaudeAgentOptions(),
            )
            llm_query.mark_owned_provider_attempt_unresolved(
                orphan_ref[0],
                "test_external_unconfirmed_provider",
            )

    monkeypatch.setattr(orchestrator, "claude_query", query_factory)
    monkeypatch.setattr(orchestrator.asyncio, "sleep", inject_orphan)

    result = await orchestrator._run_one_cycle(
        ui=_UI(),
        log_file=isolated_cycle / "529-blocked.log",
        gen_ctx=None,
    )

    assert result == -0.5
    assert len(dispatches) == 1
    assert main_transport._owned_process.returncode == -15
    assert orphan_ref[0] is not None
    with pytest.raises(llm_query.LLMProviderCleanupBlocked):
        llm_query._assert_no_unresolved_provider_attempts()

    orphan_transport._owned_process.returncode = -9
    orphan_transport._process = None
    orphan_ref[0]["transport_close_attempted"] = True
    orphan_ref[0]["transport_close_confirmed"] = True
    llm_query._assert_no_unresolved_provider_attempts()


@pytest.mark.asyncio
async def test_exhausted_529_retries_fail_as_fresh_stream_infrastructure(
    isolated_cycle,
    monkeypatch,
):
    import llm_query
    import orchestrator

    transports = [_OwnedTransport() for _ in range(4)]
    transport_iter = iter(transports)
    dispatches = []
    ui = _UI()

    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: next(transport_iter),
    )

    def query_factory(**kwargs):
        dispatches.append(kwargs)
        return _ProviderEnvelopeQuery(kwargs["transport"])

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(orchestrator, "claude_query", query_factory)
    monkeypatch.setattr(orchestrator.asyncio, "sleep", no_sleep)

    result = await orchestrator._run_one_cycle(
        ui=ui,
        log_file=isolated_cycle / "529-exhausted.log",
        gen_ctx=None,
    )

    assert result == -0.5
    assert len(dispatches) == 4
    assert all(item._owned_process.returncode == -15 for item in transports)
    assert any(
        "fresh checkpoint-bound provider stream" in message
        for _level, message in ui.history
    )
    assert not any("Session 保留" in message for _level, message in ui.history)
    log_text = (isolated_cycle / "529-exhausted.log").read_text(encoding="utf-8")
    assert "[529 RETRIES EXHAUSTED]" in log_text
    assert "[CYCLE DONE]" not in log_text
