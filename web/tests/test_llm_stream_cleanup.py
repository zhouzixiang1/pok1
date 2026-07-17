import asyncio

import pytest
from claude_agent_sdk import ClaudeAgentOptions


class _FakeProcess:
    def __init__(self):
        self.returncode = None


class _ResistantQuery:
    def __init__(self, release):
        self.release = release
        self.running = False
        self.cancel_seen = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
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


class _OwnedTransport:
    def __init__(self, stream_release):
        self._process = _FakeProcess()
        self._owned_process = self._process
        self.stream_release = stream_release
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self._owned_process.returncode = -15
        self._process = None
        self.stream_release.set()


class _StuckOwnedTransport(_OwnedTransport):
    def __init__(self, stream_release):
        super().__init__(stream_release)
        self.close_release = asyncio.Event()

    async def close(self):
        self.close_calls += 1
        try:
            await self.close_release.wait()
        except asyncio.CancelledError:
            await self.close_release.wait()
        self._owned_process.returncode = -9
        self._process = None
        self.stream_release.set()


class _UnprovenExitTransport(_OwnedTransport):
    async def close(self):
        self.close_calls += 1
        # A cleared transport pointer is not proof that the exact child exited.
        self._process = None
        self.stream_release.set()


class _UI:
    def log_io(self, *_args, **_kwargs):
        pass

    def log_history(self, *_args, **_kwargs):
        pass

    def emit_tool_call(self, *_args, **_kwargs):
        pass

    def update_cost(self, *_args, **_kwargs):
        pass


def _short_timeout_policy(_role):
    return {
        "policy_key": "TEST",
        "first_activity_timeout": 0.01,
        "idle_timeout": 0.02,
        "stall_timeout": 0.01,
        "total_timeout": 0.05,
    }


@pytest.fixture(autouse=True)
def _isolated_provider_cleanup_registry():
    import llm_query

    with llm_query._PROVIDER_CLEANUP_LOCK:
        llm_query._UNRESOLVED_PROVIDER_ATTEMPTS.clear()
    yield
    with llm_query._PROVIDER_CLEANUP_LOCK:
        llm_query._UNRESOLVED_PROVIDER_ATTEMPTS.clear()


@pytest.mark.asyncio
async def test_pending_after_timeout_terminates_exact_transport_and_never_retries(
    monkeypatch,
):
    import llm_query
    from llm_failure import is_llm_infra_error

    release = asyncio.Event()
    transport = _OwnedTransport(release)
    query = _ResistantQuery(release)
    dispatches = []

    monkeypatch.setattr(llm_query, "_role_timeout_policy", _short_timeout_policy)
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: transport,
    )

    def fake_query(**kwargs):
        dispatches.append(kwargs)
        return query

    monkeypatch.setattr(llm_query, "claude_query", fake_query)
    monkeypatch.setenv("POK_LLM_NEXT_CANCEL_GRACE", "0")

    with pytest.raises(llm_query.LLMProviderCleanupError) as exc:
        await llm_query._run_stream_with_signature_retry(
            "prompt",
            ClaudeAgentOptions(),
            "/tmp/llm-stream-cleanup.log",
            _UI(),
            "MASTER",
        )

    assert exc.value.provider_exit_confirmed is True
    assert is_llm_infra_error(exc.value) is True
    assert len(dispatches) == 1
    assert dispatches[0]["transport"] is transport
    assert transport.close_calls == 1
    assert transport._owned_process.returncode == -15
    assert query.cancel_seen is True
    assert query.running is False
    assert query.closed is True
    with llm_query._PROVIDER_CLEANUP_LOCK:
        assert llm_query._UNRESOLVED_PROVIDER_ATTEMPTS == {}


@pytest.mark.asyncio
async def test_confirmed_parent_cancel_preserves_cancelled_error(monkeypatch):
    import llm_query

    release = asyncio.Event()
    transport = _OwnedTransport(release)
    query = _ResistantQuery(release)
    events = []

    monkeypatch.setattr(
        llm_query,
        "_role_timeout_policy",
        lambda _role: {
            "policy_key": "TEST",
            "first_activity_timeout": 60,
            "idle_timeout": 60,
            "stall_timeout": 60,
            "total_timeout": 120,
        },
    )
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: transport,
    )
    monkeypatch.setattr(llm_query, "claude_query", lambda **_kwargs: query)
    monkeypatch.setenv("POK_LLM_NEXT_CANCEL_GRACE", "0")
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    owner = asyncio.create_task(
        llm_query._run_stream_with_signature_retry(
            "prompt",
            ClaudeAgentOptions(),
            "/tmp/llm-parent-cancel.log",
            _UI(),
            "MASTER PROPOSAL mechanism",
        )
    )
    for _ in range(100):
        if query.running:
            break
        await asyncio.sleep(0)
    assert query.running is True
    owner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await owner

    assert transport.close_calls == 1
    assert transport._owned_process.returncode == -15
    assert query.running is False
    assert query.closed is True
    assert any(
        event[0]
        == "pipeline.llm_role_provider_cleanup_completed_after_parent_cancel"
        for event in events
    )
    assert not any(
        event[0] == "pipeline.llm_role_provider_cleanup_failure"
        for event in events
    )
    with llm_query._PROVIDER_CLEANUP_LOCK:
        assert llm_query._UNRESOLVED_PROVIDER_ATTEMPTS == {}


@pytest.mark.asyncio
async def test_unconfirmed_parent_cancel_remains_cleanup_failure(monkeypatch):
    import llm_query

    release = asyncio.Event()
    transport = _UnprovenExitTransport(release)
    query = _ResistantQuery(release)

    monkeypatch.setattr(
        llm_query,
        "_role_timeout_policy",
        lambda _role: {
            "policy_key": "TEST",
            "first_activity_timeout": 60,
            "idle_timeout": 60,
            "stall_timeout": 60,
            "total_timeout": 120,
        },
    )
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: transport,
    )
    monkeypatch.setattr(llm_query, "claude_query", lambda **_kwargs: query)
    monkeypatch.setenv("POK_LLM_NEXT_CANCEL_GRACE", "0")

    owner = asyncio.create_task(
        llm_query._run_stream_with_signature_retry(
            "prompt",
            ClaudeAgentOptions(),
            "/tmp/llm-parent-cancel-unproven.log",
            _UI(),
            "MASTER PROPOSAL mechanism",
        )
    )
    for _ in range(100):
        if query.running:
            break
        await asyncio.sleep(0)
    owner.cancel()

    with pytest.raises(llm_query.LLMProviderCleanupError) as exc:
        await owner
    assert exc.value.provider_exit_confirmed is False
    with pytest.raises(llm_query.LLMProviderCleanupBlocked):
        llm_query._assert_no_unresolved_provider_attempts()

    transport._owned_process.returncode = -9
    llm_query._assert_no_unresolved_provider_attempts()


@pytest.mark.asyncio
async def test_unconfirmed_owned_transport_exit_blocks_fresh_provider_dispatch(
    monkeypatch,
):
    import llm_query

    release = asyncio.Event()
    transport = _StuckOwnedTransport(release)
    query = _ResistantQuery(release)
    dispatches = []

    monkeypatch.setattr(llm_query, "_role_timeout_policy", _short_timeout_policy)
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: transport,
    )
    monkeypatch.setattr(
        llm_query,
        "claude_query",
        lambda **kwargs: dispatches.append(kwargs) or query,
    )
    monkeypatch.setenv("POK_LLM_NEXT_CANCEL_GRACE", "0")
    monkeypatch.setenv("POK_LLM_TRANSPORT_CLOSE_TIMEOUT", "0.01")
    monkeypatch.setenv("POK_LLM_STREAM_TASK_EXIT_TIMEOUT", "0.01")

    with pytest.raises(llm_query.LLMProviderCleanupError) as first:
        await llm_query._run_stream_with_signature_retry(
            "prompt",
            ClaudeAgentOptions(),
            "/tmp/llm-stream-stuck.log",
            _UI(),
            "MASTER",
        )
    assert first.value.provider_exit_confirmed is False
    assert len(dispatches) == 1

    with pytest.raises(llm_query.LLMProviderCleanupBlocked):
        await llm_query._run_stream_with_signature_retry(
            "second prompt",
            ClaudeAgentOptions(),
            "/tmp/llm-stream-stuck-2.log",
            _UI(),
            "MASTER",
        )
    assert len(dispatches) == 1

    transport.close_release.set()
    await asyncio.sleep(0.05)
    llm_query._assert_no_unresolved_provider_attempts()
    assert transport._owned_process.returncode == -9
    assert query.running is False


@pytest.mark.asyncio
async def test_cleared_transport_pointer_does_not_fake_process_exit(monkeypatch):
    import llm_query

    release = asyncio.Event()
    transport = _UnprovenExitTransport(release)
    query = _ResistantQuery(release)
    dispatches = []

    monkeypatch.setattr(llm_query, "_role_timeout_policy", _short_timeout_policy)
    monkeypatch.setattr(
        llm_query,
        "_new_owned_sdk_transport",
        lambda _prompt, _options: transport,
    )
    monkeypatch.setattr(
        llm_query,
        "claude_query",
        lambda **kwargs: dispatches.append(kwargs) or query,
    )
    monkeypatch.setenv("POK_LLM_NEXT_CANCEL_GRACE", "0")

    with pytest.raises(llm_query.LLMProviderCleanupError) as exc:
        await llm_query._run_stream_with_signature_retry(
            "prompt",
            ClaudeAgentOptions(),
            "/tmp/llm-stream-unproven.log",
            _UI(),
            "MASTER",
        )

    assert exc.value.provider_exit_confirmed is False
    assert transport._process is None
    assert transport._owned_process.returncode is None
    with pytest.raises(llm_query.LLMProviderCleanupBlocked):
        llm_query._assert_no_unresolved_provider_attempts()
    assert len(dispatches) == 1

    transport._owned_process.returncode = -9
    llm_query._assert_no_unresolved_provider_attempts()


@pytest.mark.asyncio
async def test_generator_running_aclose_is_an_explicit_cleanup_failure():
    import llm_query

    release = asyncio.Event()

    async def resistant_generator():
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        yield "done"

    generator = resistant_generator()
    running = asyncio.create_task(generator.__anext__())
    await asyncio.sleep(0)

    with pytest.raises(llm_query.LLMProviderCleanupError) as exc:
        await llm_query._bounded_aclose(
            generator,
            "TEST",
            "/tmp/llm-generator-running.log",
        )
    assert "already running" in str(exc.value)
    assert running.done() is False

    release.set()
    await running
    await generator.aclose()
