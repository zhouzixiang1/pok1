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
from types import SimpleNamespace

import checkpoint_schema
import pytest

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "core"))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "server"))


@pytest.fixture(autouse=True)
def _strict_parent_authority(monkeypatch):
    def resolve(label, **_kwargs):
        version = int(str(label).rsplit("_v", 1)[1])
        return SimpleNamespace(
            eligible=True,
            version=version,
            issues=(),
            runtime_manifest={"epoch": "national_tcp_policy_v1"},
            epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
            publication_identity={"published": True, "version": version},
            certificate_digest="a" * 64,
        )

    monkeypatch.setattr(checkpoint_schema, "resolve_national_bot_spec", resolve)


@pytest.fixture(autouse=True)
def _restore_pipeline_state_paths_after_test():
    import evolution_core
    import evolution_infra

    original_core = evolution_core.PIPELINE_STATE_FILE
    original_infra = evolution_infra.PIPELINE_STATE_FILE
    try:
        yield
    finally:
        evolution_core.PIPELINE_STATE_FILE = original_core
        evolution_infra.PIPELINE_STATE_FILE = original_infra


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

    write_pipeline_checkpoint(202, 200, stage, master_plan={"workers": []})
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

    def set_status(self, *a, **kw):
        pass

    def update_cost(self, *a, **kw):
        pass

    def reset_gen_cost(self, *a, **kw):
        pass


def test_orchestrator_default_daemon_workers_uses_authoritative_safe_cap(
    monkeypatch,
):
    import daemon_management
    import orchestrator

    monkeypatch.setattr(daemon_management.os, "cpu_count", lambda: 128)

    resolved = orchestrator._resolve_daemon_workers(None)

    assert resolved == daemon_management.MAX_SAFE_DAEMON_WORKERS == 12
    assert orchestrator._resolve_daemon_workers(3) == 3


@pytest.mark.parametrize(
    "recovery",
    [
        {
            "action": "blocked",
            "reason": "unrecoverable_checkpoint",
            "diagnostics": {"issues": ["checkpoint_identity_invalid"]},
        },
        {
            "action": "operator_action_required",
            "checkpoint": {
                "stage": "official_bootstrap_required",
                "next_v": 143,
                "source_v": 142,
            },
        },
    ],
)
def test_startup_recovery_stops_before_rating_daemon_side_effect(
    monkeypatch,
    recovery,
):
    import evolution_core
    import orchestrator
    import rate_limiter

    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda _ui: recovery)
    monkeypatch.setattr(
        orchestrator,
        "load_llm_pause",
        lambda: pytest.fail(
            "durable pause was read before blocked startup recovery stopped launch"
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "consume_operator_resume_ack_from_env",
        lambda: pytest.fail(
            "one-shot resume acknowledgement was consumed on blocked startup"
        ),
    )
    monkeypatch.setattr(orchestrator, "_runtime_branch_guard_enabled", lambda: False)
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)
    monkeypatch.setattr(
        evolution_core,
        "start_daemon",
        lambda **_kwargs: pytest.fail(
            "rating daemon started before recovery authority was accepted"
        ),
    )

    outcome = asyncio.run(
        orchestrator.orchestrator_loop(_FakeUI(), no_daemon=False)
    )

    assert outcome == (
        orchestrator.ORCH_RECOVERY_BLOCKED_COST
        if recovery["action"] == "blocked"
        else orchestrator.ORCH_OPERATOR_ACTION_REQUIRED_COST
    )


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


def test_timeout_overlay_cas_cannot_overwrite_late_checkpoint_progress(
    tmp_path,
):
    import evolution_core
    import orchestrator

    observed = _write_checkpoint(
        tmp_path,
        "critic_checked",
        timeout_extensions=0,
    )
    state_file = evolution_core.PIPELINE_STATE_FILE
    advanced = {
        **observed,
        "stage": "verified",
        "checkpoint_revision": observed["checkpoint_revision"] + 1,
    }
    state_file.write_text(json.dumps(advanced, indent=2), encoding="utf-8")

    written = orchestrator._write_timeout_checkpoint_from_exact_snapshot(
        observed,
        "infra_timed_out",
        master_plan=observed.get("master_plan"),
    )

    assert written is False
    current = json.loads(state_file.read_text(encoding="utf-8"))
    assert current["stage"] == "verified"
    assert current["checkpoint_revision"] == advanced["checkpoint_revision"]


def test_timeout_overlay_passes_full_workflow_cas_identity(monkeypatch):
    import evolution_core
    import orchestrator

    checkpoint = {
        "workflow_run_id": "generation:144:timeout-cas",
        "checkpoint_revision": 11,
        "stage": "critic_checked",
        "next_v": 144,
        "source_v": 143,
    }
    calls = []

    def write(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(evolution_core, "write_pipeline_checkpoint", write)

    assert orchestrator._write_timeout_checkpoint_from_exact_snapshot(
        checkpoint,
        "infra_timed_out",
        master_plan={"strategy": "single_parent"},
    ) is True
    assert calls == [
        (
            (144, 143, "infra_timed_out"),
            {
                "expected_checkpoint_revision": 11,
                "expected_checkpoint_stage": "critic_checked",
                "expected_workflow_run_id": "generation:144:timeout-cas",
                "master_plan": {"strategy": "single_parent"},
            },
        )
    ]


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

    # Must NOT be the extension sentinel.  The timeout cannot erase the
    # precommit-stage authority: outer recovery will retry/rework from the exact
    # critic_checked checkpoint instead of laundering it through generic abandon.
    assert cost != -99999.0, "critic_checked must not trigger the extension grant"
    after = json.loads(pipe_file.read_text())
    assert after.get("stage") == "critic_checked", after


def test_precommit_infra_timeout_is_not_misclassified_by_old_master_attempts(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import orchestrator

    checkpoint = _write_checkpoint(
        tmp_path,
        "critic_checked",
        timeout_extensions=0,
    )
    checkpoint["audit_attempt"] = 2
    checkpoint["gate_results"] = {
        "quality": {"passed": True},
        "review": {"passed": True},
        "critic": {"passed": True},
    }
    pipe_file = evolution_core.PIPELINE_STATE_FILE
    pipe_file.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")

    cost = asyncio.new_event_loop().run_until_complete(
        _drive_cycle(
            orchestrator,
            tmp_path / "precommit_infra_old_master_attempts.txt",
            _FakeUI(),
        )
    )

    assert cost != orchestrator.ORCH_RECOVERY_BLOCKED_COST
    after = json.loads(pipe_file.read_text(encoding="utf-8"))
    assert after["stage"] == "infra_timed_out"
    assert after["audit_attempt"] == 2


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

    # Refused -> returns a real cost, but preserves verified.  Generic timeout
    # abandonment may not erase a passed precommit/certification boundary; outer
    # deterministic recovery owns the idempotent commit route.
    assert cost != -99999.0, "second extension must be refused (no -99999.0)"
    after = json.loads(pipe_file.read_text())
    assert after.get("stage") == "verified", after
    assert after.get("timeout_extensions") == 1


def test_master_timeout_breaker_returns_only_after_exact_abandon_proof(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import orchestrator
    import tool_bot_management

    checkpoint = _write_checkpoint(
        tmp_path,
        "direction_audited",
        timeout_extensions=0,
    )
    checkpoint["audit_attempt"] = 2
    pipe_file = evolution_core.PIPELINE_STATE_FILE
    pipe_file.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    terminal = {
        "abandoned": True,
        "cleared_checkpoint": True,
        "workflow_run_id": checkpoint["workflow_run_id"],
        "abandon_transaction_id": "1" * 64,
        "abandon_receipt_digest": "2" * 64,
        "finalize_receipt_digest": "3" * 64,
        "abandon_checkpoint_identity": {
            "workflow_run_id": checkpoint["workflow_run_id"],
            "next_v": checkpoint["next_v"],
            "source_v": checkpoint["source_v"],
            "checkpoint_revision": checkpoint["checkpoint_revision"],
            "stage": "direction_audited",
        },
    }
    calls = []

    async def abandon(*, reason, _bypass_rate_limit, **identity):
        calls.append((reason, _bypass_rate_limit, identity))
        pipe_file.unlink()
        return dict(terminal)

    monkeypatch.setattr(
        tool_bot_management,
        "_do_abandon_generation",
        abandon,
    )
    monkeypatch.setattr(
        tool_bot_management,
        "validate_completed_abandon_handoff",
        lambda baseline, result: (
            {"valid": True}
            if baseline == checkpoint and result == terminal
            else (_ for _ in ()).throw(AssertionError("abandon proof mismatch"))
        ),
    )

    cost = asyncio.new_event_loop().run_until_complete(
        _drive_cycle(
            orchestrator,
            tmp_path / "master_timeout_exact_abandon.txt",
            _FakeUI(),
        )
    )

    assert cost == orchestrator.ORCH_GENERATION_ABANDONED_COST
    assert calls == [
        (
            "cycle_timeout_master_stuck (2 fails)",
            True,
            {
                "expected_workflow_run_id": checkpoint["workflow_run_id"],
                "expected_next_v": checkpoint["next_v"],
                "expected_source_v": checkpoint["source_v"],
                "expected_checkpoint_revision": checkpoint[
                    "checkpoint_revision"
                ],
                "expected_checkpoint_stage": "direction_audited",
            },
        )
    ]
    assert not pipe_file.exists()


async def _drive_cycle(orchestrator, log_file, ui):
    """Run _run_one_cycle forcing the timeout-handling branch.

    _run_one_cycle defines _stream_response as a nested closure, so force a
    TimeoutError at its cycle-owner boundary. This isolates the stage-aware
    extension logic from provider lifecycle behavior, which has dedicated
    cancellation-resistant tests below.
    """
    original_boundary = orchestrator._await_orchestrator_stream_response_bounded

    async def _fast_cycle_boundary(coro, **_kwargs):
        coro.close()
        raise asyncio.TimeoutError()

    orchestrator._await_orchestrator_stream_response_bounded = _fast_cycle_boundary
    try:
        return await orchestrator._run_one_cycle(
            ui=ui, log_file=log_file, one_gen=False, dry_run=False,
            max_turns=None, gen_ctx=None, shutdown_mgr=None,
        )
    finally:
        orchestrator._await_orchestrator_stream_response_bounded = original_boundary


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
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: None,
    )

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


def test_main_loop_recovery_blocked_signal_stops_without_successor_prepare(
    monkeypatch,
):
    import orchestrator

    prepare_calls = []
    run_calls = []
    events = []

    class _Ctx:
        pass

    async def _prepare(shutdown_mgr, ui, min_games=None):
        prepare_calls.append(True)
        return _Ctx()

    async def _run(**kwargs):
        run_calls.append(kwargs)
        return orchestrator.ORCH_RECOVERY_BLOCKED_COST

    async def _no_watchdog(ui, shutdown_mgr, check_interval=60):
        return

    monkeypatch.setattr(orchestrator, "_prepare_or_fail", _prepare)
    monkeypatch.setattr(orchestrator, "_run_one_cycle", _run)
    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda ui: None)
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", _no_watchdog)
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )
    import rate_limiter
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)

    asyncio.new_event_loop().run_until_complete(
        orchestrator.orchestrator_loop(
            ui=_FakeUI(),
            shutdown_mgr=None,
            no_daemon=True,
        )
    )

    assert prepare_calls == [True]
    assert len(run_calls) == 1
    assert any(
        event[0] == "orchestrator.recovery_authority_blocked_stop"
        for event in events
    )


def test_main_loop_blocks_successor_when_publication_accounting_is_invalid(
    monkeypatch,
):
    import orchestrator
    import rate_limiter

    recovery = {
        "action": "resume",
        "checkpoint": {
            "workflow_run_id": "generation:143:accounting-test",
            "checkpoint_revision": 9,
            "stage": "archived",
            "next_v": 143,
            "source_v": 142,
        },
        "stage": "post_publication_handoff",
        "post_publication_handoff": True,
    }
    events = []

    async def advance(*_args, **_kwargs):
        return {
            "routed": True,
            "recovery": None,
            "outcome": {"result": {"success": True}},
            "terminal_action": "publication_handoff_completed",
        }

    async def no_watchdog(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda _ui: recovery)
    monkeypatch.setattr(orchestrator, "_advance_deterministic_recovery", advance)
    monkeypatch.setattr(orchestrator, "_watchdog_coroutine", no_watchdog)
    monkeypatch.setattr(orchestrator, "_runtime_branch_guard_enabled", lambda: False)
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)
    monkeypatch.setattr(
        orchestrator,
        "generation_cost_status",
        lambda: {
            "active": True,
            "accounting_ok": False,
            "accounting_errors": ["missing_provider_cost_receipt"],
            "generation_id": "generation:143:accounting-test",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )

    outcome = asyncio.run(
        orchestrator.orchestrator_loop(_FakeUI(), no_daemon=True)
    )

    assert outcome == orchestrator.ORCH_ACCOUNTING_BLOCKED_COST
    assert any(
        event[0] == "orchestrator.accounting_blocked_stop"
        for event in events
    )


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
        assert resumed.next_v == 202
        assert resumed.source_v == 200
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
    assert all(ctx.next_v == 202 and ctx.source_v == 200 for ctx in run_contexts)


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


def test_main_loop_post_cleanup_timeout_blocks_cycle_completion(monkeypatch):
    """Post-generation verification timeout must block successor scheduling."""
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
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: None,
    )
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
    timeout_event = next(
        e for e in events if e[0] == "orchestrator.post_cleanup_timeout"
    )
    assert "stopping before successor scheduling" in timeout_event[2]
    assert "continuing evolution" not in timeout_event[2]
    assert any(
        e[0] == "orchestrator.post_cleanup_verification_blocked_stop"
        for e in events
    )
    assert not any(e[0] == "orchestrator.cycle_done" for e in events)


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

    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: _silent_gen())
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
            202,
            200,
            "quality_failed",
            master_plan={"strategy": "master", "tasks": []},
            gate_results={
                "quality": {
                    "all_passed": False,
                    "failed_gates": ["position_semantics(policy.py:1)"],
                }
            },
        )
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: _stalls_after_first_message())
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "ORCH_ACTIONABLE_STAGE_TIMEOUT", 0.01)
    # Immediate handoff has its own coverage below. Disable it here so this
    # case isolates the idle-timeout fallback after a legal gate transition.
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_handoff",
        lambda **_kwargs: None,
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

    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: _stalls_after_first_message())
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
        "workflow_run_id": "wf-current",
        "stage": "direction_audited",
        "last_update_ts": 10.0,
        "last_stage_change_ts": 10.0,
    }
    raw_events = [
        {
            "ts": 20.0,
            "type": "pipeline.llm_role_progress",
            "message": "old generation progress",
            "data": {"version": 135, "run_id": "135#0", "workflow_run_id": "wf-old", "emitter_proc": "web"},
        },
        {
            "ts": 21.0,
            "type": "pipeline.llm_role_progress",
            "message": "daemon progress",
            "data": {"version": 136, "run_id": "136#0", "workflow_run_id": "wf-current", "emitter_proc": "daemon"},
        },
        {
            "ts": 22.0,
            "type": "pipeline.llm_role_stream_silent",
            "message": "warning, not progress",
            "data": {"version": 136, "run_id": "136#0", "workflow_run_id": "wf-current", "emitter_proc": "web"},
        },
        {
            "ts": 23.0,
            "type": "pipeline.llm_role_progress",
            "message": "MASTER (Try 1): LLM stream active for 120.0s",
            "data": {
                "version": 136,
                "run_id": "136#0",
                "workflow_run_id": "wf-current",
                "emitter_proc": "web",
                "role": "MASTER (Try 1)",
                "stage": "direction_audited",
            },
        },
        {
            "ts": 24.0,
            "type": "pipeline.llm_role_progress",
            "message": "same version but stale workflow",
            "data": {
                "version": 136,
                "run_id": "136#0",
                "workflow_run_id": "wf-stale",
                "emitter_proc": "web",
                "role": "STALE",
            },
        },
    ]
    monkeypatch.setattr(orchestrator, "_read_active_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(orchestrator, "_read_structured_events_tail", lambda max_bytes=None: [
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


def test_actionable_timeout_does_not_cancel_fresh_stream_owned_route(monkeypatch):
    """A long Master call still owns the direction_audited startup stage."""
    import orchestrator

    baseline_checkpoint = {
        "workflow_run_id": "generation:143:workflow-v19",
        "checkpoint_revision": 6,
        "stage": "direction_audited",
        "next_v": 143,
        "source_v": 142,
    }
    # Same-stage Master retry metadata may advance the durable revision while
    # the nested call is still running.  That is not a new recovery route.
    current_checkpoint = {
        **baseline_checkpoint,
        "checkpoint_revision": 7,
    }
    resolved_route = {
        "next_tool": "run_master",
        "route": {"intent": "pipeline"},
    }
    baseline_owner = orchestrator._checkpoint_stream_owned_route_identity(
        baseline_checkpoint,
        resolved_route=resolved_route,
    )
    current_owner = orchestrator._checkpoint_stream_owned_route_identity(
        current_checkpoint,
        resolved_route=resolved_route,
    )
    route = {
        "next_v": 143,
        "source_v": 142,
        "stage": "direction_audited",
        "next_tool": "run_master",
        "elapsed_sec": 300.0,
        "stream_owned_route_identity": current_owner,
    }
    events = []
    actionable_polls = {"count": 0}

    async def _delayed_message():
        await asyncio.sleep(0.2)
        yield "master-result"

    def _stale_owned_route(timeout_sec=None):
        actionable_polls["count"] += 1
        return dict(route)

    monkeypatch.setattr(orchestrator, "ORCH_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_STALL_TIMEOUT", 1.0)
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_stall",
        _stale_owned_route,
    )
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            (event_type, severity, message, data or {})
        ),
    )

    stream = _delayed_message().__aiter__()
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            orchestrator._await_next_stream_message(
                stream,
                baseline_owned_route_identity=baseline_owner,
            )
        )
    finally:
        loop.run_until_complete(stream.aclose())
        loop.close()

    assert result == "master-result"
    assert baseline_owner == current_owner
    assert actionable_polls["count"] >= 1
    assert not any(e[0] == "pipeline.actionable_stage_timeout" for e in events)


def test_fresh_stream_owned_route_deadlock_uses_generic_stall_ceiling(monkeypatch):
    """Owned-route suppression cannot disable the generic dead-stream bound."""
    import orchestrator

    owner = (
        "generation:143:workflow-v19",
        "direction_audited",
        143,
        142,
        "run_master",
        "pipeline",
    )
    route = {
        "next_v": 143,
        "source_v": 142,
        "stage": "direction_audited",
        "next_tool": "run_master",
        "elapsed_sec": 300.0,
        "stream_owned_route_identity": owner,
    }
    actionable_polls = {"count": 0}

    async def _never_yields():
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    def _stale_owned_route(timeout_sec=None):
        actionable_polls["count"] += 1
        return dict(route)

    monkeypatch.setattr(orchestrator, "ORCH_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_STALL_TIMEOUT", 0.3)
    monkeypatch.setattr(
        orchestrator,
        "_latest_orchestrator_external_progress",
        lambda _since: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_stall",
        _stale_owned_route,
    )
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *a, **kw: None)

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(orchestrator._OrchStreamStallTimeout):
            loop.run_until_complete(
                orchestrator._await_next_stream_message(
                    _never_yields().__aiter__(),
                    last_message_at=time.time(),
                    baseline_owned_route_identity=owner,
                )
            )
    finally:
        loop.close()

    assert actionable_polls["count"] >= 1


def test_actionable_timeout_detects_real_same_stage_route_transition(
    tmp_path,
    monkeypatch,
):
    """Production route policy distinguishes two critic_checked routes."""
    import orchestrator

    checkpoint = _write_checkpoint(tmp_path, "critic_checked")
    regression_checkpoint = {
        **checkpoint,
        "gate_results": {
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "chip_regression"}],
            }
        },
    }
    passed_checkpoint = {
        **checkpoint,
        "gate_results": {
            "precommit_eval": {
                "passed": True,
                "blockers": [],
            }
        },
    }
    regression_route = orchestrator._resolve_recovery_route(
        regression_checkpoint
    )
    passed_route = orchestrator._resolve_recovery_route(passed_checkpoint)
    assert regression_route["next_tool"] == "execute_workers"
    assert regression_route["route"]["intent"] == "precommit_rework"
    assert passed_route["next_tool"] == "run_precommit_eval"
    assert passed_route["route"]["intent"] == "precommit_eval"

    regression_owner = orchestrator._checkpoint_stream_owned_route_identity(
        regression_checkpoint,
        resolved_route=regression_route,
    )
    passed_owner = orchestrator._checkpoint_stream_owned_route_identity(
        passed_checkpoint,
        resolved_route=passed_route,
    )
    route = {
        "next_v": checkpoint["next_v"],
        "source_v": checkpoint["source_v"],
        "stage": "critic_checked",
        "next_tool": "run_precommit_eval",
        "elapsed_sec": 300.0,
        "stream_owned_route_identity": passed_owner,
    }

    async def _never_yields():
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "ORCH_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "ORCH_STREAM_STALL_TIMEOUT", 1.0)
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_stall",
        lambda timeout_sec=None: dict(route),
    )
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *a, **kw: None)

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(orchestrator._OrchActionableStageTimeout):
            loop.run_until_complete(
                orchestrator._await_next_stream_message(
                    _never_yields().__aiter__(),
                    baseline_owned_route_identity=regression_owner,
                )
            )
    finally:
        loop.close()

    assert regression_owner != passed_owner


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
    """Reviewer/precommit rework routes pass exact checkpoint feedback to workers."""
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
            "reviewer_feedback": "REVIEW_REJECTION: fix pfr sign",
        },
    }

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, _FakeUI())
    )

    assert handled is True
    fake_execute.handler.assert_awaited_once_with({
        "next_v": 268,
        "source_v": 249,
        "reviewer_feedback": "REVIEW_REJECTION: fix pfr sign",
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
            "master_plan": {"tasks": [{"worker_id": "w1", "target_files": ["policy.py"]}]},
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
    terminal_result = {
        "abandoned": True,
        "cleared_checkpoint": True,
        "workflow_run_id": "generation:268:test",
        "abandon_transaction_id": "a" * 64,
        "abandon_receipt_digest": "b" * 64,
        "finalize_receipt_digest": "c" * 64,
        "abandon_checkpoint_identity": {
            "workflow_run_id": "generation:268:test",
            "checkpoint_revision": 1,
            "stage": "repair_planned",
            "next_v": 268,
            "source_v": 249,
            "digest": "d" * 64,
        },
    }
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

    async def _fake_abandon(reason="abandon_generation", **_identity):
        abandoned.append(reason)
        return {**terminal_result, "reason": reason, "abandoned_v": 268}

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
        SimpleNamespace(
            _do_abandon_generation=_fake_abandon,
            expected_abandon_identity=lambda _checkpoint: {},
        ),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "workflow_run_id": "generation:268:test",
            "checkpoint_revision": 1,
            "stage": "repair_planned",
            "next_v": 268,
            "source_v": 249,
            "parent2_v": 205,
            "worker_failure_count": 6,
        },
    }
    ui = _FakeUI()
    outcome = {}

    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(
            recovery,
            ui,
            outcome=outcome,
        )
    )

    assert handled is True
    fake_execute.handler.assert_awaited_once_with({"next_v": 268, "source_v": 249})
    assert abandoned == ["worker_circuit_breaker"]
    assert outcome["terminal_abandon_result"] == {
        **terminal_result,
        "reason": "worker_circuit_breaker",
        "abandoned_v": 268,
    }
    assert any(e[0] == "pipeline.deterministic_route_abandoned" for e in events)
    assert not any(e[0] == "pipeline.deterministic_route_failed" for e in events)


@pytest.mark.parametrize(
    ("worker_result", "expected_reason"),
    [
        (
            {
                "error": "WORKER_WORKFLOW_ABANDONED",
                "success": False,
                "failure_class": "infrastructure",
                "action": "abandon_generation",
                "worker_abandon_reason": "worker_infrastructure_exhausted",
            },
            "worker_workflow_abandoned",
        ),
        (
            {
                "success": False,
                "failure_class": "infrastructure",
                "action": "abandon_generation",
            },
            "worker_terminal_abandon",
        ),
    ],
)
def test_deterministic_route_reconciles_abandoned_worker_journal(
    monkeypatch,
    worker_result,
    expected_reason,
):
    """A terminal Worker journal must close the still-active outer checkpoint."""
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
                    "text": json.dumps(worker_result),
                }]
            }
        )
    )

    async def _fake_abandon(reason="abandon_generation", **_identity):
        abandoned.append(reason)
        return {"abandoned": True, "reason": reason, "abandoned_v": 152}

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
        SimpleNamespace(
            _do_abandon_generation=_fake_abandon,
            expected_abandon_identity=lambda _checkpoint: {},
        ),
    )

    recovery = {
        "action": "resume",
        "checkpoint": {
            "stage": "rework_running",
            "next_v": 152,
            "source_v": 142,
        },
    }
    handled = asyncio.new_event_loop().run_until_complete(
        orchestrator._try_deterministic_checkpoint_route(recovery, _FakeUI())
    )

    assert handled is True
    assert abandoned == [expected_reason]
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

    async def _fake_abandon(reason="abandon_generation", **_identity):
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
        SimpleNamespace(
            _do_abandon_generation=_fake_abandon,
            expected_abandon_identity=lambda _checkpoint: {},
        ),
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

    async def _fake_abandon(reason="abandon_generation", **_identity):
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
        SimpleNamespace(
            _do_abandon_generation=_fake_abandon,
            expected_abandon_identity=lambda _checkpoint: {},
        ),
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

    master_plan = {"strategy": "crossover", "tasks": [{"target_files": ["policy.py"]}]}
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
            202,
            200,
                "quality_failed",
                master_plan={"strategy": "crossover", "tasks": []},
                gate_results={
                "quality": {
                    "all_passed": False,
                    "failed_gates": ["position_semantics(policy.py:1)"],
                }
            },
        )
        yield AssistantMessage(content=[TextBlock(text="quality gate returned")], model="sonnet")
        await asyncio.sleep(999)
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: _quality_failed_after_message())
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


def test_terminal_checkpoint_clear_hands_off_before_stale_mcp_retry(
    tmp_path,
    monkeypatch,
):
    """Canonical abandon must return control to the outer scheduler immediately."""

    from claude_agent_sdk import (
        AssistantMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    import evolution_core
    import orchestrator
    import post_publication_handoff
    import tool_bot_management

    checkpoint = _write_checkpoint(
        tmp_path,
        "direction_audited",
        timeout_extensions=0,
    )
    pipe_file = evolution_core.PIPELINE_STATE_FILE
    events = []
    continued = {"value": False}
    terminal_result = {
        "abandoned": True,
        "cleared_checkpoint": True,
        "workflow_run_id": checkpoint["workflow_run_id"],
        "abandon_transaction_id": "a" * 64,
        "abandon_receipt_digest": "b" * 64,
        "finalize_receipt_digest": "c" * 64,
        "abandon_checkpoint_identity": {
            "workflow_run_id": checkpoint["workflow_run_id"],
            "next_v": checkpoint["next_v"],
            "source_v": checkpoint["source_v"],
            "stage": checkpoint["stage"],
            "checkpoint_revision": checkpoint["checkpoint_revision"],
            "digest": "d" * 64,
        },
    }

    async def _abandon_then_offer_stale_prepare():
        yield AssistantMessage(
            content=[ToolUseBlock(
                id="abandon-1",
                name="mcp__evolution__abandon_generation",
                input={},
            )],
            model="sonnet",
        )
        pipe_file.unlink()
        yield UserMessage(content=[ToolResultBlock(
            tool_use_id="abandon-1",
            content=json.dumps(terminal_result),
            is_error=False,
        )])
        continued["value"] = True
        yield AssistantMessage(
            content=[ToolUseBlock(
                id="stale-prepare",
                name="mcp__evolution__prepare_next_gen",
                input={"source_v": 200, "next_v": 202},
            )],
            model="sonnet",
        )

    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **_kwargs: _abandon_then_offer_stale_prepare(),
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )
    def validate_terminal(baseline, result):
        assert baseline == checkpoint
        assert result == terminal_result
        return {
            "transaction_id": result["abandon_transaction_id"],
            "abandon_receipt_digest": result["abandon_receipt_digest"],
            "finalize_receipt_digest": result["finalize_receipt_digest"],
            "checkpoint_identity": result["abandon_checkpoint_identity"],
        }

    monkeypatch.setattr(
        tool_bot_management,
        "validate_completed_abandon_handoff",
        validate_terminal,
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
            log_file=tmp_path / "terminal_handoff_log.txt",
            one_gen=True,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == orchestrator.ORCH_GENERATION_ABANDONED_COST
    assert not pipe_file.exists()
    assert continued["value"] is False
    event = next(
        item for item in events
        if item[0] == "pipeline.actionable_stage_handoff"
    )
    assert event[3]["scheduler_handoff_required"] is True
    assert event[3]["next_tool"] == "prepare_generation"
    assert event[3]["stage"] == "generation_terminal"
    assert not any(item[0] == "pipeline.sdk_stream_error" for item in events)


@pytest.mark.parametrize("proof_owner", ["abandon-1", "status-1"])
def test_terminal_proof_binds_tool_use_result_id_and_owner_across_parallel_results(
    tmp_path,
    monkeypatch,
    proof_owner,
):
    from claude_agent_sdk import (
        AssistantMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    import evolution_core
    import orchestrator
    import post_publication_handoff
    import tool_bot_management

    checkpoint = _write_checkpoint(
        tmp_path,
        "direction_audited",
        timeout_extensions=0,
    )
    pipe_file = evolution_core.PIPELINE_STATE_FILE
    terminal_result = {
        "abandoned": True,
        "cleared_checkpoint": True,
        "workflow_run_id": checkpoint["workflow_run_id"],
        "abandon_transaction_id": "1" * 64,
        "abandon_receipt_digest": "2" * 64,
        "finalize_receipt_digest": "3" * 64,
        "abandon_checkpoint_identity": {
            "workflow_run_id": checkpoint["workflow_run_id"],
            "next_v": checkpoint["next_v"],
            "source_v": checkpoint["source_v"],
            "stage": checkpoint["stage"],
            "checkpoint_revision": checkpoint["checkpoint_revision"],
            "digest": "4" * 64,
        },
    }
    continued = {"value": False}

    async def _parallel_results():
        yield AssistantMessage(
            content=[
                ToolUseBlock(id="status-1", name="mcp__evolution__status", input={}),
                ToolUseBlock(
                    id="abandon-1",
                    name="mcp__evolution__abandon_generation",
                    input={},
                ),
            ],
            model="sonnet",
        )
        pipe_file.unlink()
        yield UserMessage(
            content="",
            tool_use_result={
                "tool_use_id": "abandon-1",
                "content": [{
                    "type": "text",
                    "text": json.dumps(
                        terminal_result
                        if proof_owner == "abandon-1"
                        else {"success": True}
                    ),
                }],
            },
        )
        yield UserMessage(content=[ToolResultBlock(
            tool_use_id="status-1",
            content=json.dumps(
                terminal_result
                if proof_owner == "status-1"
                else {"success": True}
            ),
            is_error=False,
        )])
        continued["value"] = True

    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **_kwargs: _parallel_results(),
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )
    monkeypatch.setattr(
        tool_bot_management,
        "validate_completed_abandon_handoff",
        lambda baseline, result: {
            "transaction_id": result["abandon_transaction_id"],
            "abandon_receipt_digest": result["abandon_receipt_digest"],
            "finalize_receipt_digest": result["finalize_receipt_digest"],
            "checkpoint_identity": result["abandon_checkpoint_identity"],
        }
        if baseline == checkpoint and result == terminal_result
        else (_ for _ in ()).throw(AssertionError("terminal binding mismatch")),
    )

    cost = asyncio.new_event_loop().run_until_complete(
        orchestrator._run_one_cycle(
            ui=_FakeUI(),
            log_file=tmp_path / "parallel_terminal_results.txt",
            one_gen=True,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == (
        orchestrator.ORCH_GENERATION_ABANDONED_COST
        if proof_owner == "abandon-1"
        else orchestrator.ORCH_RECOVERY_BLOCKED_COST
    )
    assert continued["value"] is False


def test_checkpoint_free_blocked_handoff_ends_provider_stream(
    monkeypatch,
):
    import evolution_core
    import orchestrator
    import post_publication_handoff

    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_stall",
        lambda timeout_sec=0: None,
    )
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "blocked", "issues": ["journal_tampered"]},
    )

    route = orchestrator._detect_actionable_stage_handoff(
        baseline_checkpoint_identity=(
            "generation:143:workflow-v22",
            5,
            "direction_audited",
            143,
            142,
        )
    )

    assert route["recovery_blocked"] is True
    assert route["stage"] == "post_publication_handoff_blocked"
    assert route["next_tool"] is None
    assert route["issues"] == ["journal_tampered"]


def test_checkpoint_free_postpublication_pending_precedes_abandon_proof(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import orchestrator
    import post_publication_handoff

    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_stall",
        lambda timeout_sec=0: None,
    )
    monkeypatch.setattr(
        evolution_core,
        "PIPELINE_STATE_FILE",
        tmp_path / "absent-pipeline-state.json",
    )
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {
            "status": "pending",
            "version": 144,
            "source_v": 143,
        },
    )

    route = orchestrator._detect_actionable_stage_handoff(
        baseline_checkpoint_identity=(
            "generation:144:workflow-v1",
            19,
            "verified",
            144,
            143,
        ),
        baseline_checkpoint={
            "workflow_run_id": "generation:144:workflow-v1",
            "checkpoint_revision": 19,
            "stage": "verified",
            "next_v": 144,
            "source_v": 143,
        },
        terminal_tool_result=None,
    )

    assert route["stage"] == "post_publication_handoff"
    assert route["next_tool"] == "run_archivist"
    assert route.get("recovery_blocked") is not True


def test_terminal_checkpoint_clear_without_exact_proof_stops_fail_closed(
    tmp_path,
    monkeypatch,
):
    from claude_agent_sdk import (
        AssistantMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    import evolution_core
    import orchestrator
    import post_publication_handoff

    _write_checkpoint(tmp_path, "direction_audited", timeout_extensions=0)
    pipe_file = evolution_core.PIPELINE_STATE_FILE
    continued = {"value": False}

    async def _unproved_abandon_then_continue():
        yield AssistantMessage(
            content=[ToolUseBlock(
                id="abandon-unproved",
                name="mcp__evolution__abandon_generation",
                input={},
            )],
            model="sonnet",
        )
        pipe_file.unlink()
        yield UserMessage(content=[ToolResultBlock(
            tool_use_id="abandon-unproved",
            content=json.dumps({"abandoned": True}),
            is_error=False,
        )])
        continued["value"] = True
        yield AssistantMessage(content=[], model="sonnet")

    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **_kwargs: _unproved_abandon_then_continue(),
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )

    cost = asyncio.new_event_loop().run_until_complete(
        orchestrator._run_one_cycle(
            ui=_FakeUI(),
            log_file=tmp_path / "terminal_proof_blocked_log.txt",
            one_gen=True,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST
    assert continued["value"] is False


def test_unknown_tool_result_id_stops_provider_stream_fail_closed(
    tmp_path,
    monkeypatch,
):
    from claude_agent_sdk import (
        AssistantMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    import orchestrator

    _write_checkpoint(tmp_path, "direction_audited", timeout_extensions=0)
    continued = {"value": False}

    async def _unknown_result():
        yield AssistantMessage(
            content=[ToolUseBlock(
                id="expected-tool",
                name="mcp__evolution__run_master",
                input={},
            )],
            model="sonnet",
        )
        yield UserMessage(content=[ToolResultBlock(
            tool_use_id="foreign-tool",
            content=json.dumps({"success": True}),
            is_error=False,
        )])
        continued["value"] = True

    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **_kwargs: _unknown_result(),
    )

    cost = asyncio.new_event_loop().run_until_complete(
        orchestrator._run_one_cycle(
            ui=_FakeUI(),
            log_file=tmp_path / "unknown_result.txt",
            one_gen=True,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST
    assert continued["value"] is False


def test_settled_tool_use_id_cannot_be_reused_in_same_provider_stream(
    tmp_path,
    monkeypatch,
):
    from claude_agent_sdk import (
        AssistantMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    import orchestrator

    _write_checkpoint(tmp_path, "direction_audited", timeout_extensions=0)
    continued = {"value": False}

    async def _duplicate_tool_use_id():
        yield AssistantMessage(
            content=[ToolUseBlock(
                id="reused-tool-id",
                name="mcp__evolution__status",
                input={},
            )],
            model="sonnet",
        )
        yield UserMessage(content=[ToolResultBlock(
            tool_use_id="reused-tool-id",
            content=json.dumps({"success": True}),
            is_error=False,
        )])
        yield AssistantMessage(
            content=[ToolUseBlock(
                id="reused-tool-id",
                name="mcp__evolution__abandon_generation",
                input={},
            )],
            model="sonnet",
        )
        continued["value"] = True

    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **_kwargs: _duplicate_tool_use_id(),
    )

    cost = asyncio.new_event_loop().run_until_complete(
        orchestrator._run_one_cycle(
            ui=_FakeUI(),
            log_file=tmp_path / "duplicate_tool_use_id.txt",
            one_gen=True,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST
    assert continued["value"] is False


def test_missing_tool_use_id_stops_provider_stream_fail_closed(
    tmp_path,
    monkeypatch,
):
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    import orchestrator

    _write_checkpoint(tmp_path, "direction_audited", timeout_extensions=0)
    continued = {"value": False}

    async def _missing_tool_use_id():
        yield AssistantMessage(
            content=[ToolUseBlock(
                id="",
                name="mcp__evolution__run_master",
                input={},
            )],
            model="sonnet",
        )
        continued["value"] = True

    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **_kwargs: _missing_tool_use_id(),
    )

    cost = asyncio.new_event_loop().run_until_complete(
        orchestrator._run_one_cycle(
            ui=_FakeUI(),
            log_file=tmp_path / "missing_tool_use_id.txt",
            one_gen=True,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST
    assert continued["value"] is False


def test_provider_eof_with_unsettled_tool_use_stops_fail_closed(
    tmp_path,
    monkeypatch,
):
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    import evolution_core
    import orchestrator

    _write_checkpoint(tmp_path, "direction_audited", timeout_extensions=0)
    pipe_file = evolution_core.PIPELINE_STATE_FILE

    async def _missing_result():
        yield AssistantMessage(
            content=[ToolUseBlock(
                id="missing-result",
                name="mcp__evolution__abandon_generation",
                input={},
            )],
            model="sonnet",
        )
        pipe_file.unlink()

    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **_kwargs: _missing_result(),
    )

    cost = asyncio.new_event_loop().run_until_complete(
        orchestrator._run_one_cycle(
            ui=_FakeUI(),
            log_file=tmp_path / "missing_result.txt",
            one_gen=True,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST


def test_existing_unreadable_checkpoint_is_not_generation_terminal(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import orchestrator

    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_stall",
        lambda timeout_sec=0: None,
    )

    route = orchestrator._detect_actionable_stage_handoff(
        baseline_checkpoint_identity=(
            "generation:143:workflow-v22",
            5,
            "direction_audited",
            143,
            142,
        ),
        baseline_checkpoint={
            "workflow_run_id": "generation:143:workflow-v22",
            "checkpoint_revision": 5,
            "stage": "direction_audited",
            "next_v": 143,
            "source_v": 142,
        },
    )

    assert route["recovery_blocked"] is True
    assert route["stage"] == "checkpoint_recovery_blocked"
    assert route["issues"] == ["checkpoint_unreadable_or_invalid"]

    recovery = orchestrator._checkpoint_recovery_context(
        "test_unreadable",
    )
    assert recovery["action"] == "blocked"
    assert recovery["reason"] == "checkpoint_unreadable_or_invalid"
    assert recovery["diagnostics"]["checkpoint_path_exists"] is True


def test_checkpoint_unlinked_during_read_is_fail_closed(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import orchestrator

    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)

    def unlink_while_reading():
        state_file.unlink()
        return None

    monkeypatch.setattr(
        evolution_core,
        "read_pipeline_checkpoint",
        unlink_while_reading,
    )

    observation = orchestrator._pipeline_checkpoint_observation()

    assert observation["path_existed_before"] is True
    assert observation["path_exists"] is False
    assert observation["error"] == "checkpoint_disappeared_during_read"


def test_unreadable_checkpoint_blocks_before_provider_dispatch(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import orchestrator

    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    provider_calls = []
    monkeypatch.setattr(
        orchestrator,
        "claude_query",
        lambda **kwargs: provider_calls.append(kwargs),
    )

    cost = asyncio.new_event_loop().run_until_complete(
        orchestrator._run_one_cycle(
            ui=_FakeUI(),
            log_file=tmp_path / "unreadable_checkpoint_log.txt",
            one_gen=True,
            dry_run=False,
            max_turns=None,
            gen_ctx=None,
            shutdown_mgr=None,
        )
    )

    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST
    assert provider_calls == []


def test_official_bootstrap_recovery_accepts_only_expected_operator_diagnostic(
    tmp_path,
    monkeypatch,
):
    import orchestrator
    import pipeline_recovery

    checkpoint = _write_checkpoint(
        tmp_path,
        "official_bootstrap_required",
        timeout_extensions=0,
    )
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda _checkpoint: {
            "active": True,
            "recoverable": False,
            "issues": ["official_bootstrap_requires_operator_action"],
        },
    )

    recovery = orchestrator._checkpoint_recovery_context(
        "operator_boundary_test",
    )

    assert recovery["action"] == "operator_action_required"
    assert recovery["checkpoint"] == checkpoint
    assert recovery["operator_action_required"] is True

    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda _checkpoint: {
            "active": True,
            "recoverable": False,
            "issues": [
                "official_bootstrap_requires_operator_action",
                "repo_head_drift",
            ],
        },
    )
    blocked = orchestrator._checkpoint_recovery_context(
        "operator_boundary_with_drift",
    )
    assert blocked["action"] == "blocked"
    assert "repo_head_drift" in blocked["diagnostics"]["issues"]


def test_actionable_handoff_fences_checkpoint_already_owned_by_fresh_stream(
    monkeypatch,
):
    """A restarted one-gen stream must execute its existing next tool once."""
    import evolution_core
    import orchestrator

    checkpoint = {
        "workflow_run_id": "generation:143:workflow-v19",
        "checkpoint_revision": 1,
        "stage": "selected",
        "next_v": 143,
        "source_v": 142,
    }
    route = {
        "next_v": 143,
        "source_v": 142,
        "stage": "selected",
        "next_tool": "prepare_next_gen",
        "checkpoint_actionable_identity": (
            orchestrator._checkpoint_actionable_identity(checkpoint)
        ),
    }
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_stall",
        lambda timeout_sec=0: dict(route),
    )
    monkeypatch.setattr(
        evolution_core,
        "read_pipeline_checkpoint",
        lambda: dict(checkpoint),
    )
    baseline = orchestrator._checkpoint_actionable_identity(checkpoint)

    assert orchestrator._detect_actionable_stage_handoff(
        baseline_checkpoint_identity=baseline
    ) is None

    checkpoint["checkpoint_revision"] = 2
    checkpoint["stage"] = "prepared"
    route.update({
        "stage": "prepared",
        "next_tool": "run_direction_audit",
        "checkpoint_actionable_identity": (
            orchestrator._checkpoint_actionable_identity(checkpoint)
        ),
    })
    assert orchestrator._detect_actionable_stage_handoff(
        baseline_checkpoint_identity=baseline
    ) == route


def test_actionable_stall_carries_identities_from_one_checkpoint_snapshot(
    monkeypatch,
):
    """Polling must not re-read a drifting checkpoint to build its fences."""
    import evolution_core
    import orchestrator
    import pipeline_state

    checkpoint = {
        "workflow_run_id": "generation:143:workflow-v19",
        "checkpoint_revision": 6,
        "stage": "direction_audited",
        "next_v": 143,
        "source_v": 142,
        "last_update_ts": time.time() - 600,
        "last_stage_change_ts": time.time() - 600,
    }
    resolved_route = {
        "next_tool": "run_master",
        "directive": "Call run_master",
        "route": {"next_tool": "run_master", "intent": "pipeline"},
    }
    reads = {"count": 0}

    def _read_once():
        reads["count"] += 1
        if reads["count"] > 1:
            raise AssertionError("checkpoint reread")
        return dict(checkpoint)

    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", _read_once)
    monkeypatch.setattr(
        pipeline_state,
        "pipeline_runtime_activity_ts",
        lambda _checkpoint: 0.0,
    )
    monkeypatch.setattr(
        orchestrator,
        "_resolve_recovery_route",
        lambda _checkpoint: dict(resolved_route),
    )

    stall = orchestrator._detect_actionable_stage_stall(timeout_sec=0)

    assert reads["count"] == 1
    assert stall["checkpoint_actionable_identity"] == (
        orchestrator._checkpoint_actionable_identity(checkpoint)
    )
    assert stall["stream_owned_route_identity"] == (
        orchestrator._checkpoint_stream_owned_route_identity(
            checkpoint,
            resolved_route=resolved_route,
        )
    )


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
            202,
            200,
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
        lambda **_kwargs: _park_after_message(),
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

    assert cost == orchestrator.ORCH_OPERATOR_ACTION_REQUIRED_COST
    assert json.loads(pipe_file.read_text())["stage"] == "official_bootstrap_required"
    event = next(e for e in events if e[0] == "pipeline.actionable_stage_handoff")
    assert event[3]["operator_action_required"] is True
    assert not any(e[0] == "pipeline.sdk_stream_error" for e in events)
