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
import time
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


def test_main_loop_actionable_handoff_routes_without_backoff(tmp_path, monkeypatch):
    """The normal handoff sentinel should immediately resume deterministic routing."""
    import orchestrator
    import evolution_core

    _write_checkpoint(tmp_path, "prepared", timeout_extensions=0)
    evolution_core.PIPELINE_STATE_FILE.unlink()

    route_calls = []
    backoff_logs = []

    class _UI:
        def __init__(self):
            self.gen_cost_total = 0.0
        def log_history(self, msg, level="info"):
            if "backing off" in msg.lower() or "back off" in msg.lower():
                backoff_logs.append((level, msg))
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

    async def _fake_run_one_cycle(**kw):
        _write_checkpoint(tmp_path, "master_planned", timeout_extensions=0)
        return orchestrator.ORCH_ACTIONABLE_HANDOFF_COST

    async def _fake_route(recovery, ui=None, **kwargs):
        route_calls.append((recovery, kwargs))
        return True

    monkeypatch.setattr(orchestrator, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(orchestrator, "_prepare_or_fail", _fake_prepare)
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", _fake_route)
    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda ui: None)

    async def _no_watchdog(ui, shutdown_mgr, check_interval=60):
        return

    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", _no_watchdog)

    import pipeline_recovery
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda checkpoint: {"active": True, "recoverable": True, "issues": []},
    )

    import rate_limiter
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)

    real_sleep = asyncio.sleep

    async def _fast_sleep(*a, **kw):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    class _ShutdownAfterRoute:
        @property
        def is_shutting_down(self):
            return bool(route_calls)

    asyncio.new_event_loop().run_until_complete(
        orchestrator.orchestrator_loop(
            ui=_UI(), shutdown_mgr=_ShutdownAfterRoute(), no_daemon=True,
        )
    )

    assert len(route_calls) == 1
    assert route_calls[0][0]["checkpoint"]["stage"] == "master_planned"
    assert route_calls[0][1]["log_level"] == "info"
    assert backoff_logs == []


def test_main_loop_preserves_info_log_level_across_deterministic_route_chain(tmp_path, monkeypatch):
    """A normal selected route should keep info severity on the follow-up route."""
    import orchestrator
    import evolution_core

    _write_checkpoint(tmp_path, "prepared", timeout_extensions=0)
    evolution_core.PIPELINE_STATE_FILE.unlink()

    route_calls = []

    class _UI:
        def __init__(self):
            self.gen_cost_total = 0.0
        def log_history(self, *a, **kw): pass
        def log_io(self, *a, **kw): pass
        def emit_tool_call(self, *a, **kw): pass
        def set_header(self, *a, **kw): pass
        def set_status(self, *a, **kw): pass
        def update_cost(self, *a, **kw): pass
        def reset_gen_cost(self, *a, **kw): pass

    class _Ctx:
        pass

    async def _fake_prepare(shutdown_mgr, ui, min_games=None):
        _write_checkpoint(tmp_path, "selected", timeout_extensions=0)
        return _Ctx()

    async def _fake_run_one_cycle(**kw):
        raise AssertionError("_run_one_cycle should not run before deterministic route chain")

    async def _fake_route(recovery, ui=None, **kwargs):
        route_calls.append((recovery, kwargs))
        if len(route_calls) == 1:
            # This test exercises orchestrator log-level propagation, not the
            # stage machine. Simulate a completed deterministic route without
            # asking the now-explicit early-edge validator to accept the
            # intentionally compressed selected -> workers_done jump.
            checkpoint_path = tmp_path / "pipeline_state.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["stage"] = "workers_done"
            checkpoint_path.write_text(json.dumps(checkpoint, indent=2))
        return True

    monkeypatch.setattr(orchestrator, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(orchestrator, "_prepare_or_fail", _fake_prepare)
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", _fake_route)
    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda ui: None)

    async def _no_watchdog(ui, shutdown_mgr, check_interval=60):
        return

    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", _no_watchdog)

    import pipeline_recovery
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda checkpoint: {"active": True, "recoverable": True, "issues": []},
    )

    import rate_limiter
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)

    real_sleep = asyncio.sleep

    async def _fast_sleep(*a, **kw):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    class _ShutdownAfterSecondRoute:
        @property
        def is_shutting_down(self):
            return len(route_calls) >= 2

    asyncio.new_event_loop().run_until_complete(
        orchestrator.orchestrator_loop(
            ui=_UI(), shutdown_mgr=_ShutdownAfterSecondRoute(), no_daemon=True,
        )
    )

    assert [call[0]["checkpoint"]["stage"] for call in route_calls] == [
        "selected",
        "workers_done",
    ]
    assert [call[1]["log_level"] for call in route_calls] == ["info", "info"]
    assert [call[1]["label"] for call in route_calls] == ["[Pipeline]", "[Pipeline]"]


def test_main_loop_infra_error_resumes_checkpoint_without_prepare(tmp_path, monkeypatch):
    """An active checkpoint must resume directly instead of starting Phase 1."""
    import orchestrator
    from generation_scheduler import GenerationContext

    _write_checkpoint(tmp_path, "direction_audited", timeout_extensions=0)

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
    import pipeline_recovery
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda checkpoint: {"active": True, "recoverable": True, "issues": []},
    )

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
    assert prepare_calls == [], "active checkpoint recovery must not run prepare_generation"
    for resumed in run_contexts:
        assert resumed.next_v == 102
        assert resumed.source_v == 100
        assert resumed.strategy == "master"


def test_main_loop_success_with_active_checkpoint_skips_cleanup_and_resumes(tmp_path, monkeypatch):
    """A clean LLM cycle is not a completed generation while checkpoint remains active."""
    import orchestrator

    _write_checkpoint(tmp_path, "quality_failed", timeout_extensions=0)

    cleanup_calls = []
    run_contexts = []

    async def _fake_prepare(shutdown_mgr, ui, min_games=None):
        raise AssertionError("prepare_generation must not run for active checkpoint")

    async def _fake_run_one_cycle(**kw):
        run_contexts.append(kw["gen_ctx"])
        if len(run_contexts) == 1:
            return 0.25
        return -99999.0

    async def _fake_cleanup(*_args, **_kwargs):
        cleanup_calls.append(True)
        return True

    monkeypatch.setattr(orchestrator, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(orchestrator, "_prepare_or_fail", _fake_prepare)
    monkeypatch.setattr(orchestrator, "_run_post_generation_cleanup_with_timeout", _fake_cleanup)
    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda ui: None)
    monkeypatch.setattr(orchestrator, "_watchdog_triggered", False)
    import pipeline_recovery
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda checkpoint: {"active": True, "recoverable": True, "issues": []},
    )
    async def _no_deterministic_route(*_args, **_kwargs):
        return False

    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", _no_deterministic_route)

    async def _no_watchdog(ui, shutdown_mgr, check_interval=60):
        return

    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", _no_watchdog)

    import rate_limiter
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)

    real_sleep = asyncio.sleep

    async def _fast_sleep(*a, **kw):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", _fast_sleep)

    class _ShutdownAfterSecondRun:
        @property
        def is_shutting_down(self):
            return len(run_contexts) >= 2

    asyncio.new_event_loop().run_until_complete(
        orchestrator.orchestrator_loop(
            ui=_FakeUI(),
            shutdown_mgr=_ShutdownAfterSecondRun(),
            no_daemon=True,
        )
    )

    assert len(run_contexts) == 2
    assert cleanup_calls == []
    assert all(ctx.next_v == 102 and ctx.source_v == 100 for ctx in run_contexts)


def test_main_loop_routes_fresh_selected_checkpoint_without_llm_cycle(monkeypatch):
    """After Phase 1 writes selected, deterministic preparation should run before LLM."""
    import orchestrator
    from generation_scheduler import GenerationContext

    route_calls = []
    recovery_context_calls = []

    async def _fake_prepare(shutdown_mgr, ui, min_games=None):
        return GenerationContext(
            current_v=72,
            next_v=97,
            strategy="crossover",
            source_v=72,
            crossover_parents=(72, 8),
        )

    async def _fake_route(recovery, ui, **kwargs):
        route_calls.append((recovery, kwargs))
        return True

    def _fake_recovery_context(reason, ui, **kwargs):
        recovery_context_calls.append((reason, kwargs))
        if reason == "selected_after_prepare":
            return {
                "action": "resume",
                "checkpoint": {
                    "stage": "selected",
                    "next_v": 97,
                    "source_v": 72,
                    "parent2_v": 8,
                },
            }
        return None

    async def _run_one_cycle_should_not_run(**_kwargs):
        raise AssertionError("selected deterministic route should skip _run_one_cycle")

    async def _no_watchdog(ui, shutdown_mgr, check_interval=60):
        return

    monkeypatch.setattr(orchestrator, "_prepare_or_fail", _fake_prepare)
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", _fake_route)
    monkeypatch.setattr(orchestrator, "_checkpoint_recovery_context", _fake_recovery_context)
    monkeypatch.setattr(orchestrator, "_run_one_cycle", _run_one_cycle_should_not_run)
    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda ui: None)
    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", _no_watchdog)
    monkeypatch.setattr(orchestrator, "_watchdog_triggered", False)

    import rate_limiter
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)

    real_sleep = asyncio.sleep

    async def _fast_sleep(*_args, **_kwargs):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", _fast_sleep)

    class _ShutdownAfterRoute:
        @property
        def is_shutting_down(self):
            return bool(route_calls)

    asyncio.new_event_loop().run_until_complete(
        orchestrator.orchestrator_loop(
            ui=_FakeUI(),
            shutdown_mgr=_ShutdownAfterRoute(),
            no_daemon=True,
        )
    )

    assert len(route_calls) == 1
    assert route_calls[0][0]["checkpoint"]["stage"] == "selected"
    assert route_calls[0][1]["log_level"] == "info"
    assert ("selected_after_prepare", {"log_level": "info", "label": "[Pipeline]"}) in recovery_context_calls


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


def test_actionable_stage_idle_timeout_is_infra_and_preserves_checkpoint(tmp_path, monkeypatch):
    """After a gate writes quality_failed, a silent stream should short-retry."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    import orchestrator
    import evolution_core

    _write_checkpoint(tmp_path, "workers_done", timeout_extensions=0)
    pipe_file = evolution_core.PIPELINE_STATE_FILE
    events = []

    async def _stalls_after_first_message():
        yield AssistantMessage(content=[TextBlock(text="seen")], model="sonnet")
        evolution_core.write_pipeline_checkpoint(
            102,
            100,
            "quality_failed",
            master_plan={"strategy": "master", "tasks": []},
            gate_results={
                "quality": {
                    "all_passed": False,
                    "failed_gates": ["position_semantics(state.py:1)"],
                }
            },
        )
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "claude_query", lambda prompt, options: _stalls_after_first_message())
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "ORCH_ACTIONABLE_STAGE_TIMEOUT", 0.01)
    # Immediate handoff has its own coverage below. Disable it here so this
    # case isolates the idle-timeout fallback after a legal gate transition.
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_handoff",
        lambda: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )

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
    assert after.get("stage") == "quality_failed"
    assert any(e[0] == "pipeline.actionable_stage_timeout" for e in events)
    assert not any(e[0] == "pipeline.actionable_stage_handoff" for e in events)


def test_stream_stall_timeout_aborts_without_checkpoint(tmp_path, monkeypatch):
    """D (2026-07-09): a stalled main-agent stream must abort via the generic
    stall ceiling even when there is NO checkpoint / actionable stage.

    Previously the orchestrator only aborted on an actionable-stage stall
    (requires a checkpoint). Early in a cycle — before any tool runs — there is
    no checkpoint, so a stalled stream waited the full CYCLE_TIMEOUT (5400s).
    The generic ORCH_STREAM_STALL_TIMEOUT ceiling must fire on pure wall-clock
    silence so the cycle can retry instead of hanging for 90 minutes.
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    import orchestrator
    import evolution_core

    # NO checkpoint written — simulates early-cycle stall before any tool.
    pipe_file = evolution_core.PIPELINE_STATE_FILE
    if pipe_file.exists():
        pipe_file.unlink()
    events = []

    async def _stalls_after_first_message():
        # one substantive message, then silence forever (the stall signature)
        yield AssistantMessage(content=[TextBlock(text="seen")], model="sonnet")
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "claude_query", lambda prompt, options: _stalls_after_first_message())
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_STALL_TIMEOUT", 0.05)
    # Disable the actionable-stage path so only the generic ceiling can fire.
    monkeypatch.setattr(orchestrator, "ORCH_ACTIONABLE_STAGE_TIMEOUT", 0)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )

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

    # Treated as infra (short backoff), not a hard business failure.
    assert cost == -0.5
    assert any(e[0] == "pipeline.orchestrator_stream_stall_timeout" for e in events)


def test_external_progress_filters_to_current_generation(monkeypatch):
    import orchestrator

    checkpoint = {
        "next_v": 136,
        "run_id": "136#0",
        "stage": "direction_audited",
        "last_update_ts": 10.0,
        "last_stage_change_ts": 10.0,
    }
    raw_events = [
        {
            "ts": 20.0,
            "type": "pipeline.llm_role_progress",
            "message": "old generation progress",
            "data": {"version": 135, "run_id": "135#0", "emitter_proc": "web"},
        },
        {
            "ts": 21.0,
            "type": "pipeline.llm_role_progress",
            "message": "daemon progress",
            "data": {"version": 136, "run_id": "136#0", "emitter_proc": "daemon"},
        },
        {
            "ts": 22.0,
            "type": "pipeline.llm_role_stream_silent",
            "message": "warning, not progress",
            "data": {"version": 136, "run_id": "136#0", "emitter_proc": "web"},
        },
        {
            "ts": 23.0,
            "type": "pipeline.llm_role_progress",
            "message": "MASTER (Try 1): LLM stream active for 120.0s",
            "data": {
                "version": 136,
                "run_id": "136#0",
                "emitter_proc": "web",
                "role": "MASTER (Try 1)",
                "stage": "direction_audited",
            },
        },
    ]
    monkeypatch.setattr(orchestrator, "_read_active_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(orchestrator, "_read_system_events_tail", lambda max_bytes=None: [
        json.dumps(event) for event in raw_events
    ])

    progress = orchestrator._latest_orchestrator_external_progress(15.0)

    assert progress["ts"] == 23.0
    assert progress["event_type"] == "pipeline.llm_role_progress"
    assert progress["role"] == "MASTER (Try 1)"


def test_stream_stall_timeout_extends_on_current_generation_progress(monkeypatch):
    import orchestrator

    events = []
    started_at = time.time()
    progress_emitted = {"value": False}

    async def _never_yields():
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    def _fake_external_progress(_since_ts):
        if progress_emitted["value"]:
            return None
        if time.time() - started_at < 0.035:
            return None
        progress_emitted["value"] = True
        return {
            "ts": time.time(),
            "source": "system_event",
            "event_type": "pipeline.llm_role_progress",
            "next_v": 136,
            "stage": "direction_audited",
            "role": "MASTER (Try 1)",
        }

    monkeypatch.setattr(orchestrator, "ORCH_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_STALL_TIMEOUT", 0.05)
    monkeypatch.setattr(orchestrator, "_latest_orchestrator_external_progress", _fake_external_progress)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(orchestrator._OrchStreamStallTimeout):
            loop.run_until_complete(
                orchestrator._await_next_stream_message(
                    _never_yields().__aiter__(),
                    last_message_at=started_at,
                    stream_started_at=started_at,
                )
            )
    finally:
        loop.close()

    assert time.time() - started_at >= 0.08
    assert any(e[0] == "pipeline.orchestrator_stream_external_progress" for e in events)
    assert any(e[0] == "pipeline.orchestrator_stream_stall_timeout" for e in events)


@pytest.mark.parametrize(
    "stage",
    ["master_planned", "quality_failed", "repair_planned", "rework_running", "precommit_failed"],
)
def test_actionable_recovery_deterministically_calls_execute_workers(monkeypatch, stage):
    """Actionable execute_workers checkpoints should not rely on another Orchestrator LLM turn."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    events = []
    fake_execute = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({"success": True}),
                }]
            }
        )
    )
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "execute_workers"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_planning",
        SimpleNamespace(execute_workers=fake_execute),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": stage,
            "next_v": 268,
            "source_v": 249,
            "parent2_v": 205,
        },
    }
    ui = _FakeUI()

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, ui)
    )

    assert handled is True
    fake_execute.handler.assert_awaited_once_with({"next_v": 268, "source_v": 249})
    assert any(e[0] == "pipeline.deterministic_route_execute_workers" for e in events)
    assert any(e[0] == "pipeline.deterministic_route_done" for e in events)
    assert stage in ui.events[0][1]


def test_deterministic_execute_workers_passes_checkpoint_feedback(monkeypatch):
    """Critic/review rework routes must pass exact checkpoint feedback to workers."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    fake_execute = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({"success": True}),
                }]
            }
        )
    )
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *a, **kw: None)
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "execute_workers"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_planning",
        SimpleNamespace(execute_workers=fake_execute),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "reviewed",
            "next_v": 268,
            "source_v": 249,
            "reviewer_feedback": "CRITIC_REJECTION: fix pfr sign",
        },
    }

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, _FakeUI())
    )

    assert handled is True
    fake_execute.handler.assert_awaited_once_with({
        "next_v": 268,
        "source_v": 249,
        "reviewer_feedback": "CRITIC_REJECTION: fix pfr sign",
    })


def test_deterministic_route_done_defaults_to_success_without_success_field(monkeypatch):
    """A no-error deterministic tool result should not emit a warning just because success is omitted."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    events = []
    fake_prepare = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({"status": "prepared"}),
                }]
            }
        )
    )
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "prepare_next_gen"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_gates",
        SimpleNamespace(prepare_next_gen=fake_prepare),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "selected",
            "next_v": 269,
            "source_v": 268,
        },
    }

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, _FakeUI())
    )

    assert handled is True
    done = [e for e in events if e[0] == "pipeline.deterministic_route_done"]
    assert done and done[-1][1] == "success"
    assert done[-1][3]["success"] is True
    assert done[-1][3]["reported_success"] is None


def test_master_planned_deterministic_route_clears_stale_session(monkeypatch):
    """A saved LLM session at master_planned must not resume into free-form Bash."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    cleared = []
    fake_execute = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({"success": True}),
                }]
            }
        )
    )
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: "stale-session-id")
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda reason="": cleared.append(reason))
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *a, **kw: None)
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "execute_workers"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_planning",
        SimpleNamespace(execute_workers=fake_execute),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "master_planned",
            "next_v": 280,
            "source_v": 279,
            "master_plan": {"tasks": [{"worker_id": "w1", "target_files": ["strategy.py"]}]},
        },
    }

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, _FakeUI())
    )

    assert handled is True
    assert cleared == ["deterministic_master_planned_route"]
    fake_execute.handler.assert_awaited_once_with({"next_v": 280, "source_v": 279})


def test_deterministic_route_abandons_after_worker_circuit_breaker(monkeypatch):
    """Worker circuit breaker must not loop repair_planned back into execute_workers."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    events = []
    abandoned = []
    fake_execute = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": (
                            "CIRCUIT BREAKER: 6 worker failures already recorded "
                            "this generation (max 6). Abandon this generation and start a new one."
                        )
                    }),
                }]
            }
        )
    )

    async def _fake_abandon(reason="abandon_generation"):
        abandoned.append(reason)
        return {"abandoned": True, "reason": reason, "abandoned_v": 268}

    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "execute_workers"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_planning",
        SimpleNamespace(execute_workers=fake_execute),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_bot_management",
        SimpleNamespace(_do_abandon_generation=_fake_abandon),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "repair_planned",
            "next_v": 268,
            "source_v": 249,
            "parent2_v": 205,
            "worker_failure_count": 6,
        },
    }
    ui = _FakeUI()

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, ui)
    )

    assert handled is True
    fake_execute.handler.assert_awaited_once_with({"next_v": 268, "source_v": 249})
    assert abandoned == ["worker_circuit_breaker"]
    assert any(e[0] == "pipeline.deterministic_route_abandoned" for e in events)
    assert not any(e[0] == "pipeline.deterministic_route_failed" for e in events)


def test_deterministic_route_abandons_after_precommit_rework_circuit_breaker(monkeypatch):
    """Precommit repair loop breaker should force abandon through recovery."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    events = []
    abandoned = []
    fake_execute = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": "PRECOMMIT_REWORK_CIRCUIT_BREAKER",
                        "precommit_rework_count": 3,
                        "max_rework_rounds": 3,
                    }),
                }]
            }
        )
    )

    async def _fake_abandon(reason="abandon_generation"):
        abandoned.append(reason)
        return {"abandoned": True, "reason": reason, "abandoned_v": 277}

    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "execute_workers"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_planning",
        SimpleNamespace(execute_workers=fake_execute),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_bot_management",
        SimpleNamespace(_do_abandon_generation=_fake_abandon),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "precommit_failed",
            "next_v": 277,
            "source_v": 276,
            "precommit_rework_count": 3,
        },
    }
    ui = _FakeUI()

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, ui)
    )

    assert handled is True
    fake_execute.handler.assert_awaited_once_with({"next_v": 277, "source_v": 276})
    assert abandoned == ["precommit_rework_circuit_breaker"]
    assert any(e[0] == "pipeline.deterministic_route_abandoned" for e in events)
    assert not any(e[0] == "pipeline.deterministic_route_failed" for e in events)


def test_deterministic_route_abandons_after_official_rework_circuit_breaker(monkeypatch):
    """Repeated formal 5+3 repairs must terminate in a fresh generation."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    events = []
    abandoned = []
    fake_execute = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": "OFFICIAL_REWORK_CIRCUIT_BREAKER",
                        "official_rework_count": 2,
                        "max_rework_rounds": 2,
                        "abandoned": True,
                        "abandon_result": {
                            "abandoned": True,
                            "reason": "official_rework_circuit_breaker",
                            "abandoned_v": 278,
                        },
                    }),
                }]
            }
        )
    )

    async def _fake_abandon(reason="abandon_generation"):
        abandoned.append(reason)
        return {"abandoned": True, "reason": reason, "abandoned_v": 278}

    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "execute_workers"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_planning",
        SimpleNamespace(execute_workers=fake_execute),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_bot_management",
        SimpleNamespace(_do_abandon_generation=_fake_abandon),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "official_failed",
            "next_v": 278,
            "source_v": 277,
            "official_rework_count": 2,
        },
    }
    ui = _FakeUI()

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, ui)
    )

    assert handled is True
    fake_execute.handler.assert_awaited_once_with({"next_v": 278, "source_v": 277})
    assert abandoned == []
    assert any(e[0] == "pipeline.deterministic_route_abandoned" for e in events)
    assert not any(e[0] == "pipeline.deterministic_route_failed" for e in events)


def test_actionable_recovery_calls_prepare_next_gen_from_selected(monkeypatch):
    """selected-stage recovery should dispatch prepare_next_gen."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    events = []
    fake_prepare = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({"success": True}),
                }]
            }
        )
    )
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "prepare_next_gen"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_gates",
        SimpleNamespace(prepare_next_gen=fake_prepare),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "selected",
            "next_v": 280,
            "source_v": 279,
        },
    }
    ui = _FakeUI()

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, ui)
    )

    assert handled is True
    fake_prepare.handler.assert_awaited_once_with({"source_v": 279, "next_v": 280})
    assert any(e[0] == "pipeline.deterministic_route_prepare_next_gen" for e in events)
    assert any(e[0] == "pipeline.deterministic_route_done" for e in events)
    assert "selected" in ui.events[0][1]


def test_actionable_recovery_calls_run_crossover_from_selected(monkeypatch):
    """selected crossover state should dispatch run_crossover deterministically."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    events = []
    fake_run = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({"success": True}),
                }]
            }
        )
    )
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "run_crossover"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_commit",
        SimpleNamespace(run_crossover=fake_run),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "selected",
            "next_v": 300,
            "source_v": 299,
            "parent2_v": 205,
        },
    }
    ui = _FakeUI()

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, ui)
    )

    assert handled is True
    fake_run.handler.assert_awaited_once_with(
        {"parent_a": 299, "parent_b": 205, "target_v": 300}
    )
    assert any(e[0] == "pipeline.deterministic_route_run_crossover" for e in events)
    assert any(e[0] == "pipeline.deterministic_route_done" for e in events)


def test_actionable_recovery_run_crossover_requires_parent2(monkeypatch):
    """run_crossover deterministic route requires parent2_v from checkpoint."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    events = []
    fake_run = SimpleNamespace(handler=AsyncMock(return_value={"content": []}))
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "run_crossover"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_commit",
        SimpleNamespace(run_crossover=fake_run),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "selected",
            "next_v": 300,
            "source_v": 299,
        },
    }

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, _FakeUI())
    )

    assert handled is False
    assert fake_run.handler.await_count == 0


def test_critic_checked_deterministic_route_calls_precommit_eval(monkeypatch):
    """critic_checked recovery must proceed to precommit, not rerun workers."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    events = []
    fake_precommit = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({"success": True, "passed": True}),
                }]
            }
        )
    )
    fake_workers = SimpleNamespace(handler=AsyncMock(return_value={"content": []}))
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "run_precommit_eval"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_eval",
        SimpleNamespace(run_precommit_eval=fake_precommit),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_planning",
        SimpleNamespace(execute_workers=fake_workers),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "critic_checked",
            "next_v": 327,
            "source_v": 310,
        },
    }

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, _FakeUI())
    )

    assert handled is True
    fake_precommit.handler.assert_awaited_once_with({"version": 327, "source_v": 310})
    assert fake_workers.handler.await_count == 0
    assert any(e[0] == "pipeline.deterministic_route_run_precommit_eval" for e in events)
    assert any(e[0] == "pipeline.deterministic_route_done" for e in events)


def test_reviewed_deterministic_route_passes_saved_plan_to_critic(monkeypatch):
    """reviewed recovery should call critic with checkpoint plan and review feedback."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import orchestrator

    master_plan = {"strategy": "crossover", "tasks": [{"target_files": ["strategy.py"]}]}
    fake_critic = SimpleNamespace(
        handler=AsyncMock(
            return_value={
                "content": [{
                    "type": "text",
                    "text": json.dumps({"success": True, "approved": True}),
                }]
            }
        )
    )
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *a, **kw: None)
    monkeypatch.setitem(
        sys.modules,
        "pipeline_state",
        SimpleNamespace(route_policy=lambda _ckpt: {"next_tool": "run_critic"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tool_gates",
        SimpleNamespace(run_critic=fake_critic),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "reviewed",
            "next_v": 328,
            "source_v": 310,
            "master_plan": master_plan,
            "reviewer_feedback": "approved with notes",
        },
    }

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, _FakeUI())
    )

    assert handled is True
    fake_critic.handler.assert_awaited_once_with({
        "version": 328,
        "source_v": 310,
        "plan": master_plan,
        "reviewer_feedback": "approved with notes",
        "force_advance": False,
    })


def test_actionable_stage_handoff_interrupts_active_stream(tmp_path, monkeypatch):
    """A gate-produced quality_failed checkpoint should hand off without infra telemetry."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    import evolution_core
    import orchestrator

    _write_checkpoint(tmp_path, "workers_done", timeout_extensions=0)
    pipe_file = evolution_core.PIPELINE_STATE_FILE
    events = []

    async def _quality_failed_after_message():
        evolution_core.write_pipeline_checkpoint(
            102,
            100,
            "quality_failed",
            master_plan={"strategy": "crossover", "tasks": []},
            parent2_v=99,
            gate_results={
                "quality": {
                    "all_passed": False,
                    "failed_gates": ["position_semantics(state.py:1)"],
                }
            },
        )
        yield AssistantMessage(content=[TextBlock(text="quality gate returned")], model="sonnet")
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "claude_query", lambda prompt, options: _quality_failed_after_message())
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )

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

    assert cost == orchestrator.ORCH_ACTIONABLE_HANDOFF_COST
    after = json.loads(pipe_file.read_text())
    assert after.get("stage") == "quality_failed"
    assert any(
        e[0] == "pipeline.actionable_stage_handoff" and e[1] == "info"
        for e in events
    )
    assert not any(e[0] == "pipeline.sdk_stream_error" for e in events)


def test_operator_bootstrap_stage_parks_active_stream_without_retry(tmp_path, monkeypatch):
    """The one-time root barrier must end automation as normal control flow."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    import evolution_core
    import orchestrator

    _write_checkpoint(tmp_path, "verified", timeout_extensions=0)
    pipe_file = evolution_core.PIPELINE_STATE_FILE
    events = []

    async def _park_after_message():
        evolution_core.write_pipeline_checkpoint(
            102,
            100,
            "official_bootstrap_required",
            master_plan={"strategy": "master", "tasks": []},
            gate_results={
                "official_full": {
                    "passed": False,
                    "operator_action_required": True,
                    "action": "run_explicit_bootstrap_full",
                }
            },
        )
        yield AssistantMessage(
            content=[TextBlock(text="no published opponent; operator action required")],
            model="sonnet",
        )
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda prompt, options: _park_after_message(),
    )
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )

    cost = asyncio.new_event_loop().run_until_complete(
        orchestrator._run_one_cycle(
            ui=_FakeUI(),
            log_file=tmp_path / "operator_park_log.txt",
            one_gen=False,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == orchestrator.ORCH_ACTIONABLE_HANDOFF_COST
    assert json.loads(pipe_file.read_text())["stage"] == "official_bootstrap_required"
    event = next(e for e in events if e[0] == "pipeline.actionable_stage_handoff")
    assert event[3]["operator_action_required"] is True
    assert not any(e[0] == "pipeline.sdk_stream_error" for e in events)
