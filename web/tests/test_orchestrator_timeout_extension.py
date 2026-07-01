"""Tests for the orchestrator timeout-extension logic (v101 death-loop fix).

The v101 bug: a cycle that timed out at stage `critic_checked` was treated as
"commit imminent" and granted an extension, which returned a real cost — the
main loop then ran post_generation_cleanup + logged 'gen complete' (false
complete) while no commit actually happened, looping forever.

These tests pin the corrected behavior:
  - Extension is granted ONLY at stage `verified` (commit is the next gate).
  - Only ONE extension per version (counter persisted in checkpoint as
    `timeout_extensions`).
  - Granting returns the -99999.0 sentinel (distinct from -0.5 infra / -1.0 generic
    / <0 auth), so the main loop does NOT treat it as success.

The heavy `_run_one_cycle` is exercised by mocking `_stream_response` to raise
`asyncio.TimeoutError` (the exact path under test), with a real on-disk
checkpoint so the read/rewrite logic runs against tmp_path.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "core"))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "server"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_checkpoint(tmp_path, stage, timeout_extensions=0):
    """Write a pipeline_state.json with the given stage + timeout_extensions.

    Writes via write_pipeline_checkpoint for the base fields, then overlays
    timeout_extensions directly so we control the exact on-disk state without
    depending on the precommit_attempt merge semantics (another agent's file).
    """
    from evolution_core import write_pipeline_checkpoint
    import evolution_infra
    import evolution_core

    pipe_file = tmp_path / "pipeline_state.json"
    # Patch BOTH module references so the orchestrator's
    # `from evolution_core import PIPELINE_STATE_FILE` (value import) and the
    # read_pipeline_checkpoint function (reads evolution_infra global) both
    # resolve to the same tmp file.
    evolution_infra.PIPELINE_STATE_FILE = pipe_file
    evolution_core.PIPELINE_STATE_FILE = pipe_file

    write_pipeline_checkpoint(102, 100, stage, master_plan={"workers": []})
    # Overlay timeout_extensions directly (preserve other fields)
    raw = pipe_file.read_text()
    data = json.loads(raw)
    data["timeout_extensions"] = timeout_extensions
    pipe_file.write_text(json.dumps(data, indent=2))
    return data


class _FakeUI:
    """Minimal UI stub capturing log_history / update_cost / reset_gen_cost."""

    def __init__(self):
        self.gen_cost_total = 0.0
        self.events = []

    def log_history(self, msg, level="info"):
        self.events.append((level, msg))

    def log_io(self, *a, **kw):
        pass

    def emit_tool_call(self, *a, **kw):
        pass

    def set_header(self, *a, **kw):
        pass

    def update_cost(self, *a, **kw):
        pass

    def reset_gen_cost(self, *a, **kw):
        pass


# ---------------------------------------------------------------------------
# Task A/B/C: extension grant logic inside _run_one_cycle
# ---------------------------------------------------------------------------


def test_timeout_at_verified_grants_extension_returns_sentinel(tmp_path, monkeypatch):
    """stage==verified + timeout_extensions==0 -> grants, returns -99999.0, counter becomes 1."""
    import orchestrator
    import evolution_core

    ckpt = _write_checkpoint(tmp_path, "verified", timeout_extensions=0)
    pipe_file = evolution_core.PIPELINE_STATE_FILE

    # _run_one_cycle opens log_file itself; pass a tmp path.
    log_file = tmp_path / "orch_log.txt"
    ui = _FakeUI()

    cost = asyncio.new_event_loop().run_until_complete(
        _drive_cycle(orchestrator, log_file, ui)
    )

    assert cost == -99999.0, f"expected -99999.0 sentinel, got {cost}"
    # Counter must now be 1 on disk
    after = json.loads(pipe_file.read_text())
    assert after.get("timeout_extensions") == 1, after


def test_timeout_at_critic_checked_does_not_grant(tmp_path, monkeypatch):
    """Regression for the v101 bug: stage==critic_checked must NOT grant extension."""
    import orchestrator
    import evolution_core

    _write_checkpoint(tmp_path, "critic_checked", timeout_extensions=0)
    pipe_file = evolution_core.PIPELINE_STATE_FILE

    log_file = tmp_path / "orch_log.txt"
    ui = _FakeUI()

    cost = asyncio.new_event_loop().run_until_complete(
        _drive_cycle(orchestrator, log_file, ui)
    )

    # Must NOT be the extension sentinel — falls through to normal timeout
    # handling which marks the checkpoint timed_out and returns a real cost.
    assert cost != -99999.0, "critic_checked must not trigger the extension grant"
    after = json.loads(pipe_file.read_text())
    # Normal timeout path marks the stage timed_out
    assert after.get("stage") == "timed_out", after


def test_second_timeout_at_verified_refuses_extension(tmp_path, monkeypatch):
    """stage==verified + timeout_extensions>=1 -> refuses, falls through to normal handling."""
    import orchestrator
    import evolution_core

    _write_checkpoint(tmp_path, "verified", timeout_extensions=1)
    pipe_file = evolution_core.PIPELINE_STATE_FILE

    log_file = tmp_path / "orch_log.txt"
    ui = _FakeUI()

    cost = asyncio.new_event_loop().run_until_complete(
        _drive_cycle(orchestrator, log_file, ui)
    )

    # Refused -> falls through to normal timeout handling (returns a real cost,
    # NOT -99999.0), and marks the checkpoint timed_out. The fall-through rewrites
    # the checkpoint via write_pipeline_checkpoint which only preserves its
    # known fields, so timeout_extensions may be dropped — that is fine (the
    # generation is abandoning this version via timed_out anyway).
    assert cost != -99999.0, "second extension must be refused (no -99999.0)"
    after = json.loads(pipe_file.read_text())
    assert after.get("stage") == "timed_out", after


async def _drive_cycle(orchestrator, log_file, ui):
    """Run _run_one_cycle forcing the timeout-handling branch.

    _run_one_cycle defines _stream_response as a nested closure, so we cannot
    patch it by name. Instead we force a TimeoutError out of the real closure:
      - patch claude_query to return an async generator that never yields, so
        the real _stream_response's `async for message in gen` blocks.
      - patch asyncio.wait_for to immediately raise TimeoutError (the exact
        exception the cycle's outer try/except handles), exercising the
        stage-aware extension logic against the real on-disk checkpoint.
    """
    original_wait_for = asyncio.wait_for
    original_claude_query = orchestrator.claude_query

    async def _hang_gen():
        # Never yields — real _stream_response's `async for` blocks forever
        if False:
            yield  # pragma: no cover (never reached)

    def _fake_claude_query(prompt, options):
        return _hang_gen()

    async def _fast_wait_for(coro, timeout):
        t = asyncio.ensure_future(coro)
        t.cancel()
        try:
            await t
        except BaseException:
            pass
        raise asyncio.TimeoutError()

    orchestrator.claude_query = _fake_claude_query
    orchestrator.asyncio.wait_for = _fast_wait_for
    try:
        return await orchestrator._run_one_cycle(
            ui=ui, log_file=log_file, one_gen=False, dry_run=False,
            max_turns=None, gen_ctx=None, shutdown_mgr=None,
        )
    finally:
        orchestrator.claude_query = original_claude_query
        orchestrator.asyncio.wait_for = original_wait_for


# ---------------------------------------------------------------------------
# Task D: main loop handles -99999.0 sentinel (does NOT run cleanup / emit cycle_done)
# ---------------------------------------------------------------------------


def test_main_loop_sentinel_minus3_skips_cleanup_and_complete(tmp_path, monkeypatch):
    """cost == -99999.0 must NOT call post_generation_cleanup and must NOT log 'gen complete'."""
    import orchestrator

    cleanup_calls = []
    complete_logs = []
    infra_backoff_logs = []

    class _UI:
        def __init__(self):
            self.gen_cost_total = 0.0
        def log_history(self, msg, level="info"):
            low = msg.lower()
            if "complete" in low and "gen" in low:
                complete_logs.append(msg)
            if "backing off" in low or "back off" in low:
                infra_backoff_logs.append(msg)
        def log_io(self, *a, **kw): pass
        def emit_tool_call(self, *a, **kw): pass
        def set_header(self, *a, **kw): pass
        def set_status(self, *a, **kw): pass
        def update_cost(self, *a, **kw): pass
        def reset_gen_cost(self, *a, **kw): pass

    class _Ctx:
        pass

    async def _fake_prepare(shutdown_mgr, ui, min_games=None):
        return _Ctx()

    # _run_one_cycle returns the -99999.0 sentinel once. The shutdown manager below
    # flips to shutting_down after that return so the loop exits cleanly on the
    # next top-of-loop check.
    state = {"calls": 0, "sentinel_processed": False}

    async def _fake_run_one_cycle(**kw):
        state["calls"] += 1
        state["sentinel_processed"] = True
        return -99999.0

    async def _fake_post_generation_cleanup(shutdown_mgr, ui, gen_ctx):
        cleanup_calls.append(gen_ctx)

    monkeypatch.setattr(orchestrator, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(orchestrator, "_prepare_or_fail", _fake_prepare)
    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda ui: None)

    async def _no_watchdog(ui, shutdown_mgr, check_interval=60):
        return

    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", _no_watchdog)

    # generation_scheduler.post_generation_cleanup is imported lazily inside the
    # cost>=0 block; patch it on the module before it's imported there.
    import generation_scheduler
    monkeypatch.setattr(generation_scheduler, "post_generation_cleanup", _fake_post_generation_cleanup)

    # rate_limiter.is_blocked must be False so the loop proceeds to _run_one_cycle.
    import rate_limiter
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)

    # Neutralize the 5s sleep at the loop bottom so the test doesn't hang.
    real_sleep = asyncio.sleep

    async def _fast_sleep(*a, **kw):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    ui = _UI()

    class _ShutdownAfterSentinel:
        @property
        def is_shutting_down(self):
            return state["sentinel_processed"]

    asyncio.new_event_loop().run_until_complete(
        orchestrator.orchestrator_loop(
            ui=ui, shutdown_mgr=_ShutdownAfterSentinel(), no_daemon=True,
        )
    )

    assert cleanup_calls == [], "post_generation_cleanup must NOT run for -99999.0 sentinel"
    assert complete_logs == [], "must NOT log 'gen complete' for -99999.0 sentinel"
    assert infra_backoff_logs == [], "must NOT apply infra/auth backoff for -99999.0 sentinel"
    assert state["calls"] == 1, f"loop should process one sentinel cycle then stop; got {state['calls']} calls"


def test_main_loop_infra_error_resumes_checkpoint_without_prepare(tmp_path, monkeypatch):
    """cost == -0.5 must resume the active checkpoint instead of starting Phase 1 again."""
    import orchestrator
    from generation_scheduler import GenerationContext

    _write_checkpoint(tmp_path, "reviewed", timeout_extensions=0)

    prepare_calls = []
    run_contexts = []

    async def _fake_prepare(shutdown_mgr, ui, min_games=None):
        prepare_calls.append(min_games)
        return GenerationContext(
            current_v=999,
            next_v=1000,
            strategy="master",
            source_v=999,
            gen_count=0,
        )

    async def _fake_run_one_cycle(**kw):
        run_contexts.append(kw["gen_ctx"])
        if len(run_contexts) == 1:
            return -0.5
        return -99999.0

    monkeypatch.setattr(orchestrator, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(orchestrator, "_prepare_or_fail", _fake_prepare)
    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda ui: None)
    monkeypatch.setattr(orchestrator, "_watchdog_triggered", False)

    async def _no_watchdog(ui, shutdown_mgr, check_interval=60):
        return

    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", _no_watchdog)

    import rate_limiter
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)

    async def _fast_wait_for(coro, timeout):
        try:
            coro.close()
        except AttributeError:
            pass
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _fast_wait_for)

    class _ShutdownAfterSecondRun:
        @property
        def is_shutting_down(self):
            return len(run_contexts) >= 2

        async def wait_for_shutdown(self):
            await asyncio.sleep(999)

    ui = _FakeUI()
    asyncio.new_event_loop().run_until_complete(
        orchestrator.orchestrator_loop(
            ui=ui, shutdown_mgr=_ShutdownAfterSecondRun(), no_daemon=True,
        )
    )

    assert len(run_contexts) == 2
    assert len(prepare_calls) == 1, "infra retry must not run prepare_generation again"
    resumed = run_contexts[1]
    assert resumed.next_v == 102
    assert resumed.source_v == 100
    assert resumed.strategy == "master"


def test_main_loop_post_cleanup_timeout_still_marks_cycle_done(monkeypatch):
    """Post-generation housekeeping timeout should not block the next evolution cycle."""
    import orchestrator
    import generation_scheduler
    from generation_scheduler import GenerationContext

    events = []
    cleanup_calls = []

    async def _fake_prepare(shutdown_mgr, ui, min_games=None):
        return GenerationContext(
            current_v=242,
            next_v=243,
            strategy="master",
            source_v=242,
            gen_count=1,
        )

    async def _fake_run_one_cycle(**kw):
        return 0.25

    async def _hanging_cleanup(shutdown_mgr, ui, gen_ctx):
        cleanup_calls.append(gen_ctx)
        await asyncio.Event().wait()

    def _fake_log(event_type, severity, message, data=None):
        events.append((event_type, severity, message, data or {}))

    monkeypatch.setattr(orchestrator, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(orchestrator, "_prepare_or_fail", _fake_prepare)
    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda ui: None)
    monkeypatch.setattr(orchestrator, "_watchdog_triggered", False)
    monkeypatch.setattr(orchestrator, "POST_GENERATION_CLEANUP_TIMEOUT", 0.01)
    monkeypatch.setattr(orchestrator, "log_system_event", _fake_log)
    monkeypatch.setattr(generation_scheduler, "post_generation_cleanup", _hanging_cleanup)

    async def _no_watchdog(ui, shutdown_mgr, check_interval=60):
        return

    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", _no_watchdog)

    import rate_limiter
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)

    real_sleep = asyncio.sleep

    async def _fast_sleep(*a, **kw):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", _fast_sleep)

    class _ShutdownAfterCycleDone:
        @property
        def is_shutting_down(self):
            return any(e[0] == "orchestrator.cycle_done" for e in events)

    asyncio.new_event_loop().run_until_complete(
        orchestrator.orchestrator_loop(
            ui=_FakeUI(),
            shutdown_mgr=_ShutdownAfterCycleDone(),
            no_daemon=True,
        )
    )

    assert len(cleanup_calls) == 1
    assert any(e[0] == "orchestrator.post_cleanup_timeout" for e in events)
    assert any(e[0] == "orchestrator.cycle_done" for e in events)


def test_first_activity_timeout_is_infra_and_preserves_checkpoint(tmp_path, monkeypatch):
    """No first LLM stream message should short-retry as infra, not mark checkpoint timed_out."""
    import orchestrator
    import evolution_core

    _write_checkpoint(tmp_path, "reviewed", timeout_extensions=0)
    pipe_file = evolution_core.PIPELINE_STATE_FILE

    async def _silent_gen():
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "claude_query", lambda prompt, options: _silent_gen())
    monkeypatch.setattr(orchestrator, "ORCH_FIRST_ACTIVITY_TIMEOUT", 0.01)

    ui = _FakeUI()
    cost = asyncio.new_event_loop().run_until_complete(
        orchestrator._run_one_cycle(
            ui=ui,
            log_file=tmp_path / "orch_log.txt",
            one_gen=False,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == -0.5
    after = json.loads(pipe_file.read_text())
    assert after.get("stage") == "reviewed"
    assert any("no first stream message" in msg for _, msg in ui.events)
