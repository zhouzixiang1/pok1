"""Generation-loop companion to ``orchestrator.orchestrator_loop`` (phase-decomposed).

The 1156-line ``orchestrator_loop`` body was split into two contiguous module-level
phase sub-functions orchestrated by the thin ``orchestrator_loop`` wrapper that
remains in ``orchestrator.py``:

- ``_loop_phase_a_setup``        : epoch/daemon/task startup, recovery resolution,
                                   runtime branch-guard task creation, and the
                                   per-loop state init. Returns either an
                                   early-exit value (bare integer/None) or a
                                   1-tuple ``(ctx,)`` carrying the shared loop
                                   context dict.
- ``_loop_phase_b_generation_loop``: the three nested abandon/accounting helpers
                                   (``_reset_canonical_abandon_streak``,
                                   ``_record_verified_canonical_abandon``,
                                   ``_publication_accounting_allows_successor``)
                                   plus the main generation ``try``/``except``/
                                   ``finally`` body -- prepare_generation /
                                   _run_one_cycle / post-generation cleanup /
                                   daemon-dead backoff / watchdog / cost-limit /
                                   availability handling / task teardown.
                                   Returns ``terminal_outcome``.

Continuation protocol (mirrors ``orchestrator_cycle_phases`` and
``tool_planning_worker_phases``): a bare non-tuple return from phase A is an
early exit; a 1-tuple ``(ctx,)`` continues to phase B. Phase B always returns
``terminal_outcome``. All moved code reaches module globals via ``_orch.<name>``
so test monkeypatches on ``orchestrator.<name>`` keep working at call time.
"""

from __future__ import annotations

import orchestrator as _orch

# Strong references for fire-and-forget draft-prepare tasks.  Without holding
# a reference, asyncio.create_task tasks can be garbage-collected before they
# complete (a documented asyncio footgun), which silently kills the speculative
# draft mid-flight.  Tasks self-remove on completion via done_callback.
_PENDING_DRAFT_TASKS: set = set()

# Strong references for the eval-wait draft-tick launcher tasks.  The hook
# registered in wait_for_daemon_eval schedules the async _try_launch_draft_prepare
# via create_task (it can no longer run inline because the slot-scan read is
# awaited off the event loop).  Hold a reference so the GC foot-gun cannot kill
# the speculative-draft tick before it reaches its own _draft_prepare_task.
_PENDING_EVAL_WAIT_DRAFT_TICK_TASKS: set = set()


async def _loop_phase_a_setup(ui, shutdown_mgr, no_daemon, daemon_workers,
                              daemon_pairs, startup_recovery):
    """Phase A: epoch/daemon/task startup + recovery + state init.
    Returns (ctx,) to continue, or a bare value to early-exit."""
    """Orchestrator entry point — three-phase generation loop.

    Args:
        ui: BaseUI instance (WebUI for Dashboard). Can be None for silent mode.
        shutdown_mgr: ShutdownManager for graceful signal handling.
        no_daemon: If True, skip daemon startup.
        daemon_workers: Number of parallel workers for the daemon subprocess.
        daemon_pairs: Complete 70-hand native matches per scheduled bot pairing.
    """
    try:
        from epoch_authority import require_policy_epoch_initialized

        require_policy_epoch_initialized("orchestrator_loop")
    except Exception as exc:
        state = getattr(exc, "state", {})
        epoch_state = state.get("state", "epoch_authority_unavailable")
        msg = (
            "Orchestrator not started: policy epoch initialization is "
            f"{epoch_state}"
        )
        if ui:
            ui.log_history(msg, "warn")
            ui.set_status(f"Stopped: {epoch_state}", is_working=False)
        _orch.log.error(msg)
        # Do not emit a structured event: its destination still belongs to the
        # retired epoch.  Web and CLI launchers expose the canonical state.
        return
    # Keep the orchestrator, daemon subprocess manager, web config and
    # stability identity on one resource contract.  The prior uncapped
    # CPU-derived default produced 28 workers on a 32-core host even though
    # daemon_management's OOM-safe authority caps the runtime at 12.
    daemon_workers = _orch._resolve_daemon_workers(daemon_workers)
    from stability_observation import bind_runtime_configuration

    bind_runtime_configuration({
        "daemon_enabled": not no_daemon,
        "daemon_workers": int(daemon_workers),
        "daemon_pairs": int(daemon_pairs),
    })
    from tools import inject_ui
    inject_ui(ui)
    _orch.set_system_log_ui(ui)
    try:
        from llm_query import set_shutdown_manager
        set_shutdown_manager(shutdown_mgr)
    except Exception:
        pass

    # Parse once at the operator-facing process boundary.  The selected policy
    # is then passed internally; prompts, MCP calls, checkpoints, and candidate
    # artifacts have no field that can alter it.
    try:
        operator_cost_policy = _orch.configure_runtime_cost_policy(
            _orch.load_operator_generation_cost_policy()
        )
    except _orch.CostPolicyConfigurationError as exc:
        msg = f"Invalid operator generation cost policy: {exc}"
        if ui:
            ui.log_history(msg, "error")
            ui.set_status("Stopped: invalid operator cost policy", is_working=False)
        _orch.log.error(msg)
        _orch.log_system_event(
            "orchestrator.cost_policy_invalid",
            "error",
            msg,
            {"operator_action_required": True},
        )
        return 5

    _orch.os.makedirs(_orch.LOGS_DIR, exist_ok=True)
    _orch._rotate_orchestrator_logs(_orch.LOGS_DIR)

    if ui:
        ui.log_history("🔥 Orchestrator starting...", "success")
        ui.set_header("🔥 LLM Orchestrator Evolution 🔥")

    # Canonical checkpoint/handoff recovery is the launch authority.  Prove it
    # before consuming the one-shot resume acknowledgement or clearing a durable
    # provider pause.  A CLI preflight may pass this exact object so the lower
    # loop cannot make a second, drifting startup decision.
    recovery = (
        _orch._startup_recovery(ui)
        if startup_recovery is _orch._STARTUP_RECOVERY_UNSET
        else startup_recovery
    )
    startup_terminal_cost = _orch._startup_recovery_terminal_cost(recovery)
    recovery_stops_launch = startup_terminal_cost is not None

    # P0-3b: boot-time orphan draft reconcile.  A Slice 2b one-ahead draft
    # checkpoint survives a process restart only if the driving task is still
    # alive -- but the fire-and-forget task dies with the process.  Without
    # this reconcile an orphan mid-flight draft (any stage except workers_done)
    # deadlocks _try_launch_draft_prepare (it sees a non-None draft and returns
    # early forever).  Reap genuinely orphaned drafts and best-effort promote a
    # complete workers_done buffer.  Wrapped so boot never crashes on it.
    # The reconcile is pure synchronous file/git work (read_all_pipeline_
    # checkpoints, strict_epoch_projection, clear_pipeline_checkpoint, promote);
    # run it in an owned worker thread so a slow boot-time draft scan does not
    # stall the ASGI event loop (AGENTS.md blocking boundary).
    try:
        await _orch.run_blocking_isolated(
            _reconcile_orphan_draft_at_boot,
            ui,
            thread_name_prefix="boot-draft-reconcile",
        )
    except Exception as _draft_reconcile_exc:
        _orch.log.debug(
            "orphan draft reconcile failed (non-fatal): %s", _draft_reconcile_exc
        )

    pause_before_reconcile = None
    pause_after_reconcile = None
    if not recovery_stops_launch:
        try:
            pause_before_reconcile = _orch.load_llm_pause()
            # This is the parent-process launch boundary.  Consume and remove the
            # operator acknowledgement before daemon/SDK children can inherit it.
            pause_after_reconcile = _orch.consume_operator_resume_ack_from_env()
        except Exception as exc:
            msg = f"Invalid/unwritable LLM availability pause state: {exc}"
            if ui:
                ui.log_history(msg, "error")
                ui.set_status("Stopped: invalid LLM pause state", is_working=False)
            _orch.log.exception(msg)
            try:
                _orch.log_system_event(
                    "orchestrator.llm_availability_state_invalid",
                    "error",
                    msg,
                    {"operator_action_required": True},
                )
            except Exception:
                pass
            return 5
    if (
        pause_before_reconcile
        and pause_before_reconcile.get("active")
        and pause_after_reconcile
        and not pause_after_reconcile.get("active")
    ):
        resume_source = pause_after_reconcile.get("resume_source")
        msg = (
            "LLM availability pause cleared by "
            f"{resume_source}; deterministic checkpoint recovery will continue."
        )
        if ui:
            ui.log_history(msg, "info")
        _orch.log.info(msg)
        try:
            _orch.log_system_event(
                "orchestrator.llm_availability_resumed",
                "info",
                msg,
                {
                    "category": pause_after_reconcile.get("category"),
                    "evidence_digest": pause_after_reconcile.get("evidence_digest"),
                    "resume_source": resume_source,
                },
            )
        except Exception:
            pass

    _orch.log_system_event("orchestrator.started", "success", "Orchestrator started",
                     {
                         "daemon_enabled": not no_daemon,
                         "generation_cost_policy": operator_cost_policy.receipt(),
                     })
    _orch.log.info("Orchestrator loop started (daemon=%s)", not no_daemon)
    try:
        from evolution_infra import EVOLUTION_BRANCH
    except Exception:
        EVOLUTION_BRANCH = "main"
    _runtime_identity = _orch._runtime_git_identity()
    _expected_runtime_head = (
        _runtime_identity.get("head", "")
        if _runtime_identity.get("branch") == EVOLUTION_BRANCH else ""
    )
    _orch.os.environ["POK_RUNTIME_EXPECTED_BRANCH"] = EVOLUTION_BRANCH
    _expected_runtime_head = _orch._set_runtime_expected_head(_expected_runtime_head)

    # Register the speculative-draft launcher so the LLM-idle eval-wait window
    # is filled with Master/Workers work in a one-ahead draft slot.  The hook
    # is fired periodically inside wait_for_daemon_eval; it is best-effort and
    # gated by the slice2b activation (producer_may_draft_ahead_of_eval).  The
    # gen_count captured here is the loop's current count; the launcher only
    # uses it for logging.
    #
    # _try_launch_draft_prepare is async because its slot-scan read
    # (read_all_pipeline_checkpoints) is awaited off the event loop via
    # run_blocking_isolated (AGENTS.md blocking boundary) so the ~30s eval-wait
    # poll never starves HTTP.  The hook itself stays synchronous (it is called
    # from the sync _maybe_fire_draft_tick), so it schedules the coroutine as a
    # fire-and-forget task on the loop and returns immediately.  create_task
    # needs a running loop in the current thread, which holds here because the
    # hook fires from inside wait_for_daemon_eval (an async coroutine on the
    # loop).  A strong reference is kept so the asyncio GC foot-gun cannot kill
    # the tick before it reaches its own _draft_prepare_task.
    def _eval_wait_draft_launcher():
        try:
            _tick_task = _orch.asyncio.create_task(
                _try_launch_draft_prepare(ui, shutdown_mgr, 0)
            )
            _PENDING_EVAL_WAIT_DRAFT_TICK_TASKS.add(_tick_task)
            _tick_task.add_done_callback(_PENDING_EVAL_WAIT_DRAFT_TICK_TASKS.discard)
        except RuntimeError:
            # No running loop (e.g. tests / non-async caller) — nothing to do.
            pass
        except Exception:
            pass

    try:
        _orch.register_eval_wait_draft_hook(_eval_wait_draft_launcher)
    except Exception:
        pass

    _branch_guard_task = None
    _stability_maintenance_task = None
    _runtime_hard_stop_event = _orch.asyncio.Event()
    if not recovery_stops_launch and _orch._runtime_branch_guard_enabled():
        _branch_guard_task = _orch.asyncio.create_task(
            _orch._runtime_branch_guard_coroutine(
                ui,
                shutdown_mgr,
                expected_branch=EVOLUTION_BRANCH,
                expected_head=_expected_runtime_head,
                owner_task=_orch.asyncio.current_task(),
                hard_stop_event=_runtime_hard_stop_event,
            )
        )
        _orch.log_system_event(
            "repo.runtime_branch_guard_started",
            "info",
            "Runtime branch guard started",
            {
                "expected_branch": EVOLUTION_BRANCH,
                "expected_head": _expected_runtime_head,
                "current_branch": _runtime_identity.get("branch", ""),
                "current_head": _runtime_identity.get("head", ""),
                "check_interval": _orch.RUNTIME_BRANCH_GUARD_INTERVAL,
            },
        )
    if not recovery_stops_launch:
        _stability_maintenance_task = _orch.asyncio.create_task(
            _orch._stability_projection_maintenance_coroutine(shutdown_mgr),
            name="stability-observation-maintenance",
        )

    # Start daemon only after recovery authority permits the workflow.
    _daemon_stop = None
    if not no_daemon and not recovery_stops_launch:
        from evolution_core import start_daemon, daemon_monitor_thread
        import threading
        try:
            start_daemon(workers=daemon_workers, pairs=daemon_pairs)
        except Exception as e:
            if ui:
                ui.log_history(f"Daemon start failed: {e}", "error")
            _orch.log.error("Daemon start failed: %s", e)
            no_daemon = True
        if not no_daemon:
            _daemon_stop = threading.Event()
            monitor = threading.Thread(
                target=daemon_monitor_thread,
                args=(ui, _daemon_stop, daemon_workers, daemon_pairs),
                daemon=True,
            )
            monitor.start()
            if ui:
                ui.log_history("Daemon started.", "info")

    log_file = _orch.LOGS_DIR / f"orchestrator_{_orch.time.strftime('%Y%m%d_%H%M%S')}.txt"
    gen_count = 0
    consecutive_prep_fails = 0

    # Launch background watchdog coroutine to detect stuck pipelines
    _watchdog_task = _orch.asyncio.create_task(
        _orch.asyncio.sleep(0)
        if recovery_stops_launch
        else _orch._watchdog_coroutine(ui, shutdown_mgr, check_interval=60)
    )
    terminal_outcome = 0.0
    consecutive_canonical_abandons = 0
    canonical_abandon_target = None

    return ({
        'log_file': log_file,
        'operator_cost_policy': operator_cost_policy,
        '_branch_guard_task': _branch_guard_task,
        '_stability_maintenance_task': _stability_maintenance_task,
        '_runtime_hard_stop_event': _runtime_hard_stop_event,
        '_daemon_stop': _daemon_stop,
        '_watchdog_task': _watchdog_task,
        'gen_count': gen_count,
        'recovery': recovery,
        'consecutive_prep_fails': consecutive_prep_fails,
        'consecutive_canonical_abandons': consecutive_canonical_abandons,
        'canonical_abandon_target': canonical_abandon_target,
        'terminal_outcome': terminal_outcome,
    },)


async def _loop_phase_b_generation_loop(ctx, ui, shutdown_mgr, no_daemon,
                                        daemon_workers, daemon_pairs):
    """Phase B: nested abandon/accounting helpers + the main generation
    try/except/finally loop. Returns terminal_outcome.
    """
    log_file = ctx['log_file']
    operator_cost_policy = ctx.get('operator_cost_policy')
    _branch_guard_task = ctx.get('_branch_guard_task')
    _stability_maintenance_task = ctx.get('_stability_maintenance_task')
    _runtime_hard_stop_event = ctx['_runtime_hard_stop_event']
    _daemon_stop = ctx.get('_daemon_stop')
    _watchdog_task = ctx.get('_watchdog_task')
    gen_count = ctx.get('gen_count', 0)
    recovery = ctx.get('recovery')
    consecutive_prep_fails = ctx.get('consecutive_prep_fails', 0)
    consecutive_canonical_abandons = ctx.get('consecutive_canonical_abandons', 0)
    canonical_abandon_target = ctx.get('canonical_abandon_target')
    terminal_outcome = ctx.get('terminal_outcome')
    def _reset_canonical_abandon_streak():
        nonlocal consecutive_canonical_abandons, canonical_abandon_target
        consecutive_canonical_abandons = 0
        canonical_abandon_target = None

    def _record_verified_canonical_abandon(
        *,
        checkpoint=None,
        terminal_proof=None,
        source="provider_cycle",
        gen_ctx=None,
    ):
        """Record one terminal abandon and stop repeated same-target churn."""

        nonlocal consecutive_canonical_abandons
        nonlocal canonical_abandon_target
        nonlocal terminal_outcome

        checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
        terminal_proof = (
            terminal_proof if isinstance(terminal_proof, dict) else {}
        )
        proof_identity = _orch._canonical_abandon_proof_identity(terminal_proof)
        if proof_identity is None:
            terminal_outcome = _orch.ORCH_RECOVERY_BLOCKED_COST
            msg = (
                "Refusing successor preparation because a canonical-abandon "
                "sentinel arrived without an exact finalized transaction, "
                "ledger, and checkpoint proof."
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status(
                    "Stopped: canonical abandon proof unavailable",
                    is_working=False,
                )
            _orch.log.error(msg)
            _orch.log_system_event(
                "orchestrator.canonical_abandon_proof_blocked_stop",
                "error",
                msg,
                {
                    "source": source,
                    "gen_count": gen_count,
                    "checkpoint_present": bool(checkpoint),
                    "gen_ctx_next_v": getattr(gen_ctx, "next_v", None),
                    "gen_ctx_source_v": getattr(gen_ctx, "source_v", None),
                },
            )
            return True

        next_v = proof_identity["next_v"]
        source_v = proof_identity["source_v"]
        workflow_run_id = proof_identity["workflow_run_id"]
        checkpoint_matches_proof = (
            not checkpoint
            or (
                checkpoint.get("workflow_run_id") == workflow_run_id
                and checkpoint.get("next_v") == next_v
                and checkpoint.get("source_v") == source_v
                and type(checkpoint.get("checkpoint_revision")) is int
                and checkpoint["checkpoint_revision"]
                <= proof_identity["checkpoint_revision"]
            )
        )
        context_matches_proof = (
            gen_ctx is None
            or (
                getattr(gen_ctx, "next_v", None) == next_v
                and getattr(gen_ctx, "source_v", None) == source_v
            )
        )
        if (
            not checkpoint_matches_proof
            or not context_matches_proof
            or (not checkpoint and gen_ctx is None)
        ):
            terminal_outcome = _orch.ORCH_RECOVERY_BLOCKED_COST
            msg = (
                "Refusing successor preparation because canonical-abandon "
                "proof identity disagrees with the active checkpoint or "
                "generation context."
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status(
                    "Stopped: canonical abandon identity mismatch",
                    is_working=False,
                )
            _orch.log.error(msg)
            _orch.log_system_event(
                "orchestrator.canonical_abandon_proof_blocked_stop",
                "error",
                msg,
                {
                    "source": source,
                    "gen_count": gen_count,
                    "proof_identity": proof_identity,
                    "checkpoint": {
                        key: checkpoint.get(key)
                        for key in (
                            "workflow_run_id",
                            "next_v",
                            "source_v",
                            "checkpoint_revision",
                        )
                    },
                    "gen_ctx_next_v": getattr(gen_ctx, "next_v", None),
                    "gen_ctx_source_v": getattr(gen_ctx, "source_v", None),
                },
            )
            return True

        target = (next_v, source_v)
        if canonical_abandon_target == target:
            consecutive_canonical_abandons += 1
        else:
            canonical_abandon_target = target
            consecutive_canonical_abandons = 1
        payload = {
            "gen_count": gen_count,
            "source": source,
            "next_v": next_v,
            "source_v": source_v,
            "workflow_run_id": workflow_run_id,
            "checkpoint_revision": proof_identity["checkpoint_revision"],
            "checkpoint_stage": proof_identity["stage"],
            "consecutive_canonical_abandons": (
                consecutive_canonical_abandons
            ),
            "limit": _orch.MAX_CONSECUTIVE_CANONICAL_ABANDONS,
            "remaining": max(
                0,
                _orch.MAX_CONSECUTIVE_CANONICAL_ABANDONS
                - consecutive_canonical_abandons,
            ),
        }
        payload.update({
            "abandon_receipt_digest": terminal_proof[
                "abandon_receipt_digest"
            ],
            "abandon_transaction_id": terminal_proof["transaction_id"],
            "finalize_receipt_digest": terminal_proof[
                "finalize_receipt_digest"
            ],
        })
        _orch.deactivate_generation_cost_scope()
        if (
            consecutive_canonical_abandons
            >= _orch.MAX_CONSECUTIVE_CANONICAL_ABANDONS
        ):
            terminal_outcome = _orch.ORCH_CONSECUTIVE_ABANDON_LIMIT_COST
            payload["restart_required"] = True
            msg = (
                "Evolution stopped after "
                f"{consecutive_canonical_abandons} verified canonical "
                f"abandons for the same target v{next_v}; no successor "
                "workflow was prepared. Inspect the shared contract and "
                "explicitly restart after correction or review."
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status(
                    "Stopped: consecutive canonical abandon limit",
                    is_working=False,
                )
            _orch.log.error(msg)
            _orch.log_system_event(
                "orchestrator.consecutive_canonical_abandon_limit_stop",
                "error",
                msg,
                payload,
            )
            return True
        msg = (
            "Generation reached a verified canonical abandon boundary "
            f"({consecutive_canonical_abandons}/"
            f"{_orch.MAX_CONSECUTIVE_CANONICAL_ABANDONS} for target v{next_v}); "
            "the continuous outer scheduler may prepare one fresh successor "
            "workflow."
        )
        if ui:
            ui.log_history(msg, "warn")
        _orch.log.warning(msg)
        _orch.log_system_event(
            "orchestrator.generation_abandoned_handoff",
            "warn",
            msg,
            payload,
        )
        return False

    def _publication_accounting_allows_successor():
        """Reset the abandon streak only after durable accounting re-proves."""

        nonlocal terminal_outcome
        try:
            cost_status = _orch.generation_cost_status()
        except Exception as exc:
            cost_status = {
                "active": True,
                "accounting_ok": False,
                "accounting_errors": [
                    "generation_cost_status_unavailable:"
                    f"{type(exc).__name__}"
                ],
            }
        if (
            cost_status.get("active") is True
            and cost_status.get("accounting_ok") is not True
        ):
            terminal_outcome = _orch.ORCH_ACCOUNTING_BLOCKED_COST
            msg = (
                "Post-publication cleanup completed, but durable generation-"
                "cost accounting is invalid; refusing to prepare a successor "
                "workflow."
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status(
                    "Stopped: generation accounting invalid",
                    is_working=False,
                )
            _orch.log.error(
                "%s Errors: %s",
                msg,
                cost_status.get("accounting_errors"),
            )
            _orch.log_system_event(
                "orchestrator.accounting_blocked_stop",
                "error",
                msg,
                {
                    "accounting_errors": cost_status.get(
                        "accounting_errors"
                    ),
                    "generation_id": cost_status.get("generation_id"),
                },
            )
            return False
        _reset_canonical_abandon_streak()
        return True

    try:
        while True:
            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                break

            # Watchdog recovery: if background watchdog detected a stuck pipeline,
            # clear state and force a fresh cycle from the checkpoint stage.
            if _orch._watchdog_triggered:
                _orch._watchdog_triggered = False
                if ui:
                    ui.log_history("[Watchdog] Restarting cycle from checkpoint stage.", "warn")
                recovery = _orch._checkpoint_recovery_context("watchdog_recovery", ui)
                # Restart watchdog for the new cycle
                if _watchdog_task.done():
                    _watchdog_task = _orch.asyncio.create_task(
                        _orch._watchdog_coroutine(ui, shutdown_mgr, check_interval=60)
                    )

            # 429 quota exhaustion check — block until reset, then dispatch a
            # fresh provider stream from the validated checkpoint.
            from rate_limiter import rate_limiter
            if rate_limiter.is_blocked():
                wait = rate_limiter.wait_seconds()
                if ui:
                    ui.log_history(
                        f"⏳ API 配额耗尽，暂停进化。将在 {rate_limiter.reset_time_str()} 自动恢复 ({wait:.0f}s)",
                        "warn",
                    )
                    ui.set_status(f"⏳ 配额等待中 → {rate_limiter.reset_time_str()}", is_working=False)
                await rate_limiter.wait_until_reset(shutdown_mgr=shutdown_mgr)
                continue

            if recovery is None:
                recovery = _orch._checkpoint_recovery_context("active_checkpoint", ui)

            gen_count += 1
            _orch.log_system_event("orchestrator.cycle_start", "info", f"Cycle {gen_count} starting",
                             {"gen_count": gen_count})

            if recovery and recovery.get("action") == "operator_action_required":
                terminal_outcome = _orch.ORCH_OPERATOR_ACTION_REQUIRED_COST
                checkpoint = recovery.get("checkpoint") or {}
                msg = (
                    "Startup recovery is parked at the operator-only official "
                    f"bootstrap boundary for v{checkpoint.get('next_v')}."
                )
                if ui:
                    ui.log_history(f"[Orchestrator] {msg}", "warn")
                    ui.set_status(
                        "Stopped: operator action required",
                        is_working=False,
                    )
                _orch.log.warning(msg)
                _orch.log_system_event(
                    "orchestrator.operator_action_required_stop",
                    "warn",
                    msg,
                    {
                        "next_v": checkpoint.get("next_v"),
                        "source_v": checkpoint.get("source_v"),
                        "stage": checkpoint.get("stage"),
                    },
                )
                break

            if recovery and recovery.get("action") == "blocked":
                terminal_outcome = _orch.ORCH_RECOVERY_BLOCKED_COST
                diag = recovery.get("diagnostics") or {}
                issues = diag.get("issues") or []
                msg = (
                    "Startup recovery is blocked by an unrecoverable pipeline "
                    f"checkpoint: {', '.join(map(str, issues)) or recovery.get('reason')}"
                )
                if ui:
                    ui.log_history(f"[Orchestrator] {msg}", "error")
                    ui.set_status(
                        "Recovery blocked; governed diagnostics/operator action required",
                        is_working=False,
                    )
                _orch.log.error(msg)
                _orch.log_system_event(
                    "orchestrator.recovery_blocked_stop",
                    "error",
                    msg,
                    {
                        "reason": recovery.get("reason"),
                        "issues": issues,
                        "diagnostics": diag,
                    },
                )
                break

            # If recovering, skip Phase 1 (context already known from checkpoint)
            if recovery and recovery.get("action") == "resume":
                route_log_kwargs = _orch._recovery_route_log_kwargs(recovery)
                # Ensure the Slice 2b consumer gate-chain task is (re)driven
                # whenever we resume a ``workers_done`` checkpoint that owns a
                # sealed-but-non-terminal candidate.  This MUST run before (and
                # independently of) ``_advance_deterministic_recovery`` because a
                # transient consumer-gate infra failure (e.g. a Claude-subprocess
                # init timeout during run_review) lets the consumer asyncio task
                # exit, and the only thing that relaunches it is this ensure
                # call.  When ``route_policy`` returns ``next_tool=None`` (e.g.
                # an epoch-binding identity drift the loop cannot self-heal),
                # ``_advance_deterministic_recovery`` reports ``routed=False``
                # and the gated ensure call below (inside ``if routed``) never
                # fires, so the primary parks forever waiting for a consumer
                # that is not running.  Running it unconditionally here keeps
                # the consumer alive across transient gate failures and route
                # hiccups alike; it is idempotent (a no-op when the consumer is
                # already live or no candidate is sealed).
                _resume_ckpt = (recovery or {}).get("checkpoint") or {}
                if _resume_ckpt.get("stage") == "workers_done":
                    try:
                        await _ensure_slice2b_consumer_running(_resume_ckpt)
                    except Exception:
                        pass
                advanced = await _orch._advance_deterministic_recovery(
                    recovery,
                    ui,
                    cost_policy=operator_cost_policy,
                    shutdown_mgr=shutdown_mgr,
                    gen_count=gen_count,
                    **route_log_kwargs,
                )
                if advanced["routed"]:
                    if advanced["terminal_action"] == "generation_abandoned":
                        stopped = _record_verified_canonical_abandon(
                            checkpoint=(recovery or {}).get("checkpoint"),
                            terminal_proof=(
                                advanced.get("terminal_proof") or {}
                            ),
                            source="deterministic_recovery",
                        )
                        recovery = advanced["recovery"]
                        if stopped:
                            break
                        await _orch.asyncio.sleep(0)
                        continue
                    if (
                        advanced["terminal_action"]
                        == "publication_handoff_completed"
                    ):
                        # Async-certification self-heal for staging publications
                        # is now scheduled in the single chokepoint inside
                        # ``_advance_deterministic_recovery`` (reached by every
                        # ``publication_handoff_completed`` terminal action), so
                        # no explicit scheduling is needed at any call site.
                        if not _publication_accounting_allows_successor():
                            break
                    elif advanced["terminal_action"] in {
                        None,
                        "slice2b_consumer_parked",
                    }:
                        # Sealed candidate (gen N) with the consumer gate chain
                        # running in the background.  The primary lane is parked
                        # here while the consumer owns gen N's gates.  After a
                        # process restart, recover_at_boot re-stashed the
                        # consumer factory but does NOT launch the asyncio.Task;
                        # if we never (re)launch it the primary parks forever
                        # waiting for a consumer that is not running (restart
                        # deadlock).  Drive the consumer task first, then fill
                        # LLM idle time with a one-ahead draft for gen N+1.
                        try:
                            await _ensure_slice2b_consumer_running(
                                (recovery or {}).get("checkpoint") or {}
                            )
                        except Exception:
                            pass
                        # Attempt a one-ahead draft prepare for gen N+1 to fill
                        # LLM idle time.  This is the one-ahead producer that
                        # keeps the producer LLM permits busy while gen N's
                        # quality->review->critic->precommit chain runs
                        # concurrently in the consumer.  Best-effort and
                        # non-fatal: any failure simply continues the canonical
                        # spin-wait on the primary slot.
                        try:
                            await _try_launch_draft_prepare(ui, shutdown_mgr, gen_count)
                        except Exception:
                            pass
                        # Primary is parked for the background consumer gate
                        # chain: sleep for a bounded interval instead of
                        # re-cycling every second.  The promotion barrier at
                        # commit_bot is what unblocks publication; here we just
                        # avoid a CPU/log busy-spin while the (native, slow)
                        # consumer runs.  Shutdown remains interruptible.
                        if advanced["terminal_action"] == "slice2b_consumer_parked":
                            if await _parked_sleep(shutdown_mgr, seconds=45.0):
                                break
                    recovery = advanced["recovery"]
                    await _orch.asyncio.sleep(1)
                    continue
                ckpt = recovery["checkpoint"]
                gen_ctx = _orch._generation_context_from_checkpoint(
                    ckpt,
                    gen_count=gen_count,
                )
                recovery = None  # consume recovery, only used once
            else:
                # Phase 1: Prepare (disposable on interrupt)
                # Do not create a fresh candidate while a provider pause is
                # active. Existing deterministic recovery routes are attempted
                # above first, which lets the system strict bootstrap advance
                # without any LLM dependency.
                if not await _orch._honor_active_llm_pause(ui, shutdown_mgr):
                    break
                # After repeated eval timeouts, actually *lower* the hard
                # min_games floor (align with national_native rd_min_games=12).
                # The previous hardcode of 30 raised the bar above the profile
                # default (24) and made prepare_generation thrash forever under
                # DAEMON_EVAL_TIMEOUT=600s with pairs=5 (~7–8 min per batch).
                degraded_min = None
                if consecutive_prep_fails >= 3:
                    from workflow_profiles import get_workflow_profile

                    profile = get_workflow_profile()
                    degraded_min = max(
                        1,
                        min(
                            int(getattr(profile, "eval_wait_rd_min_games", 12) or 12),
                            int(getattr(profile, "eval_wait_min_games", 24) or 24),
                        ),
                    )
                    if ui:
                        ui.log_history(
                            f"评估等待连续超时，降低评估要求 ({degraded_min} 局) 继续进化...",
                            "warn",
                        )

                # Speculative one-ahead draft: while the primary prepare is
                # blocked on eval_wait, launch a draft (which skips eval_wait)
                # so the LLM produces Master/Workers output in parallel.  The
                # draft costs LLM tokens only; it is stale-reaped if the
                # eval_wait outcome diverges.  Best-effort and non-fatal.
                try:
                    await _try_launch_draft_prepare(ui, shutdown_mgr, gen_count)
                except Exception:
                    pass
                gen_ctx = await _orch._prepare_or_fail(shutdown_mgr, ui, min_games=degraded_min)
                if gen_ctx is None:
                    if shutdown_mgr and shutdown_mgr.is_shutting_down:
                        break
                    consecutive_prep_fails += 1
                    from evolution_infra import is_daemon_alive
                    if not is_daemon_alive() and ui:
                        daemon_dead_level = "error" if consecutive_prep_fails >= 3 else "warn"
                        ui.log_history(
                            f"Daemon 未运行，等待恢复中... (连续失败 {consecutive_prep_fails} 次)",
                            daemon_dead_level,
                        )
                    backoff = min(10 * (2 ** min(consecutive_prep_fails - 1, 4)), 300)
                    if shutdown_mgr:
                        try:
                            await _orch.asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=backoff)
                            break
                        except _orch.asyncio.TimeoutError:
                            pass
                    else:
                        await _orch.asyncio.sleep(backoff)
                    continue
                consecutive_prep_fails = 0

                selected_recovery = _orch._checkpoint_recovery_context(
                    "selected_after_prepare",
                    ui,
                    log_level="info",
                    label="[Pipeline]",
                )
                if selected_recovery and selected_recovery.get("action") in {
                    "blocked",
                    "operator_action_required",
                }:
                    recovery = selected_recovery
                    continue
                if selected_recovery and selected_recovery.get("action") == "resume":
                    advanced = await _orch._advance_deterministic_recovery(
                        selected_recovery,
                        ui,
                        log_level="info",
                        label="[Pipeline]",
                        cost_policy=operator_cost_policy,
                        shutdown_mgr=shutdown_mgr,
                        gen_ctx=gen_ctx,
                        gen_count=gen_count,
                    )
                    if advanced["routed"]:
                        if advanced["terminal_action"] == "generation_abandoned":
                            stopped = _record_verified_canonical_abandon(
                                checkpoint=(
                                    selected_recovery.get("checkpoint") or {}
                                ),
                                terminal_proof=(
                                    advanced.get("terminal_proof") or {}
                                ),
                                source="selected_deterministic_recovery",
                                gen_ctx=gen_ctx,
                            )
                            recovery = advanced["recovery"]
                            if stopped:
                                break
                            await _orch.asyncio.sleep(0)
                            continue
                        if (
                            advanced["terminal_action"]
                            == "publication_handoff_completed"
                            and not _publication_accounting_allows_successor()
                        ):
                            break
                        if advanced["terminal_action"] in {
                            None,
                            "slice2b_consumer_parked",
                        }:
                            # Same one-ahead draft-prepare hook as the primary
                            # seal branch above; the selected deterministic
                            # recovery route can also reach a sealed/consumer-
                            # running state where the producer may advance.
                            try:
                                await _try_launch_draft_prepare(ui, shutdown_mgr, gen_count)
                            except Exception:
                                pass
                            # Bounded park sleep to avoid the busy-spin (see
                            # the primary seal branch above for rationale).
                            if advanced["terminal_action"] == "slice2b_consumer_parked":
                                if await _parked_sleep(shutdown_mgr, seconds=45.0):
                                    break
                        recovery = advanced["recovery"]
                        await _orch.asyncio.sleep(1)
                        continue

            # Phase 2: Run one generation (preserves state on interrupt). A
            # deterministic route has already had priority; any remaining work
            # needs the Orchestrator LLM and must honor the durable pause.
            if not await _orch._honor_active_llm_pause(ui, shutdown_mgr):
                if not (shutdown_mgr and shutdown_mgr.is_shutting_down):
                    terminal_outcome = _orch.ORCH_LLM_AVAILABILITY_BLOCKED_COST
                break
            cost = await _orch._run_one_cycle(
                ui=ui,
                log_file=log_file,
                one_gen=False,
                dry_run=False,
                max_turns=None,
                gen_ctx=gen_ctx,
                shutdown_mgr=shutdown_mgr,
                _cost_policy=operator_cost_policy,
            )

            if cost == _orch.ORCH_OPERATOR_COST_LIMIT_COST:
                terminal_outcome = _orch.ORCH_OPERATOR_COST_LIMIT_COST
                msg = (
                    "Orchestrator stopped at the explicit operator generation cost limit. "
                    "The checkpoint is preserved; change/disable the parent-process limit "
                    "and explicitly restart to continue."
                )
                if ui:
                    ui.log_history(msg, "error")
                    ui.set_status("Stopped: operator generation cost limit", is_working=False)
                _orch.log.error(msg)
                break

            if cost == _orch.ORCH_LLM_AVAILABILITY_BLOCKED_COST:
                # A persisted manual pause ends the loop immediately; a
                # transient pause waits for its bounded cooldown and then
                # resumes from the exact active checkpoint. If persistence
                # itself failed, fail closed instead of retrying blindly.
                try:
                    pause_state = _orch.load_llm_pause()
                except Exception as exc:
                    pause_state = None
                    _orch.log.error("Cannot read LLM availability pause after block: %s", exc)
                if not pause_state or not pause_state.get("active"):
                    terminal_outcome = _orch.ORCH_LLM_AVAILABILITY_BLOCKED_COST
                    msg = (
                        "LLM availability was classified but its durable pause "
                        "record is unavailable; stopping fail-closed."
                    )
                    if ui:
                        ui.log_history(msg, "error")
                        ui.set_status("Stopped: LLM pause persistence failed", is_working=False)
                    _orch.log.error(msg)
                    break
                if not await _orch._honor_active_llm_pause(ui, shutdown_mgr):
                    if not (shutdown_mgr and shutdown_mgr.is_shutting_down):
                        terminal_outcome = _orch.ORCH_LLM_AVAILABILITY_BLOCKED_COST
                    break
                recovery = _orch._checkpoint_recovery_context(
                    "llm_availability_resumed", ui
                )
                continue

            if cost == _orch.ORCH_GENERATION_ABANDONED_COST:
                stopped = _record_verified_canonical_abandon(
                    source="provider_cycle",
                    gen_ctx=gen_ctx,
                    terminal_proof=(
                        _orch._remembered_canonical_abandon_proof(gen_ctx) or {}
                    ),
                )
                recovery = None
                if stopped:
                    break
                await _orch.asyncio.sleep(0)
                continue

            if cost == _orch.ORCH_OPERATOR_ACTION_REQUIRED_COST:
                terminal_outcome = _orch.ORCH_OPERATOR_ACTION_REQUIRED_COST
                msg = (
                    "Generation is parked at an operator-only boundary. "
                    "Automatic evolution stopped without preparing a successor."
                )
                if ui:
                    ui.log_history(msg, "warn")
                    ui.set_status(
                        "Stopped: operator action required",
                        is_working=False,
                    )
                _orch.log.warning(msg)
                _orch.log_system_event(
                    "orchestrator.operator_action_required_stop",
                    "warn",
                    msg,
                    {"gen_count": gen_count},
                )
                break

            if cost == _orch.ORCH_RECOVERY_BLOCKED_COST:
                terminal_outcome = _orch.ORCH_RECOVERY_BLOCKED_COST
                msg = (
                    "Orchestrator stopped fail-closed because checkpoint or "
                    "terminal-generation authority could not be re-proven. "
                    "Do not prepare another generation until governed recovery "
                    "diagnostics are resolved."
                )
                if ui:
                    ui.log_history(msg, "error")
                    ui.set_status(
                        "Stopped: recovery authority blocked",
                        is_working=False,
                    )
                _orch.log.error(msg)
                _orch.log_system_event(
                    "orchestrator.recovery_authority_blocked_stop",
                    "error",
                    msg,
                    {"cost_signal": cost},
                )
                break

            # Timeout-extension sentinel: a cycle timed out but commit was imminent
            # (stage=verified) so ONE extension was granted mid-cycle. The cycle is NOT
            # complete — the bot has not committed yet. Do NOT run post_generation_cleanup,
            # do NOT log 'gen complete', do NOT back off. Just resume from the checkpoint
            # next iteration. Must come BEFORE the cost >= 0 success block so the sentinel
            # is never treated as success. Value -99999.0 (distinct from auth clamp).
            if cost == _orch.ORCH_ACTIONABLE_HANDOFF_COST:
                recovery = _orch._checkpoint_recovery_context(
                    "actionable_stage_handoff",
                    ui,
                    log_level="info",
                    label="[Pipeline]",
                )
                if recovery:
                    if recovery.get("action") == "resume":
                        advanced = await _orch._advance_deterministic_recovery(
                            recovery,
                            ui,
                            log_level="info",
                            label="[Pipeline]",
                            cost_policy=operator_cost_policy,
                            shutdown_mgr=shutdown_mgr,
                            gen_ctx=gen_ctx,
                            gen_count=gen_count,
                        )
                        if advanced["routed"]:
                            if (
                                advanced["terminal_action"]
                                == "generation_abandoned"
                            ):
                                stopped = _record_verified_canonical_abandon(
                                    checkpoint=(recovery or {}).get(
                                        "checkpoint"
                                    ),
                                    terminal_proof=(
                                        advanced.get("terminal_proof") or {}
                                    ),
                                    source="actionable_deterministic_recovery",
                                    gen_ctx=gen_ctx,
                                )
                                recovery = advanced["recovery"]
                                if stopped:
                                    break
                                await _orch.asyncio.sleep(0)
                                continue
                            if (
                                advanced["terminal_action"]
                                == "publication_handoff_completed"
                                and not _publication_accounting_allows_successor()
                            ):
                                break
                            recovery = advanced["recovery"]
                            await _orch.asyncio.sleep(1)
                        else:
                            await _orch.asyncio.sleep(0)
                    continue
                recovery = {
                    "action": "blocked",
                    "reason": "actionable_handoff_authority_missing",
                    "checkpoint": None,
                    "diagnostics": {
                        "active": True,
                        "recoverable": False,
                        "issues": ["actionable_handoff_authority_missing"],
                    },
                }
                continue

            if cost == -99999.0:
                if ui:
                    ui.log_history(
                        "Orchestrator: cycle timed out but commit was imminent — granted extension, "
                        "resuming from checkpoint next cycle (no commit yet).",
                        "warn",
                    )
                continue

            if cost == _orch.SHUTDOWN_CANCEL_COST:
                if ui:
                    ui.log_history(
                        "Orchestrator: shutdown cancellation observed; exiting loop without backoff.",
                        "warn",
                    )
                break

            # Phase 3: Cleanup (idempotent) — after any successful generation
            if cost >= 0:
                active_recovery = _orch._checkpoint_recovery_context("cycle_completed_with_active_checkpoint", ui)
                if active_recovery:
                    recovery = active_recovery
                    if ui:
                        ui.log_history(
                            "Orchestrator cycle ended while checkpoint is still active; "
                            "continuing from checkpoint instead of marking generation complete.",
                            "warn",
                        )
                    try:
                        ckpt = active_recovery.get("checkpoint") or {}
                        _orch.log_system_event(
                            "orchestrator.cycle_yielded_active_checkpoint",
                            "warn",
                            "Cycle ended with active checkpoint; skipping post-generation cleanup",
                            {
                                "gen_count": gen_count,
                                "stage": ckpt.get("stage"),
                                "next_v": ckpt.get("next_v"),
                                "source_v": ckpt.get("source_v"),
                                "cost": round(cost, 4),
                            },
                        )
                    except Exception:
                        pass
                    await _orch.asyncio.sleep(5)
                    continue
                # Reset the generic-failure backoff counter — the cycle succeeded.
                if getattr(_orch.orchestrator_loop, "_gen_fail_count", 0):
                    _orch.orchestrator_loop._gen_fail_count = 0
                cleanup_ok = await _orch._run_post_generation_cleanup_with_timeout(
                    shutdown_mgr, ui, gen_ctx, gen_count=gen_count
                )
                if cleanup_ok is not True:
                    terminal_outcome = _orch.ORCH_RECOVERY_BLOCKED_COST
                    msg = (
                        "Post-generation verification did not complete; "
                        "stopping before any successor generation is prepared."
                    )
                    if ui:
                        ui.log_history(msg, "error")
                        ui.set_status(
                            "Stopped: post-generation verification failed",
                            is_working=False,
                        )
                    _orch.log.error(msg)
                    _orch.log_system_event(
                        "orchestrator.post_cleanup_verification_blocked_stop",
                        "error",
                        msg,
                        {"gen_count": gen_count, "cost": round(cost, 4)},
                    )
                    break
                if ui:
                    ui.log_history(f"Orchestrator gen {gen_count} complete. Cost: ${cost:.4f}", "info")
                _orch.log_system_event("orchestrator.cycle_done", "info", f"Cycle {gen_count} done (cost=${cost:.4f})",
                                 {"gen_count": gen_count, "cost": round(cost, 4)})
                # Reset per-generation cost tracker for next cycle
                if ui:
                    ui.reset_gen_cost()
                _reset_canonical_abandon_streak()
                _orch.deactivate_generation_cost_scope()

            # Auth error fast-fail (also catches 429 via negative cost from _stream_response)
            if cost < 0:
                # 429 quota — rate_limiter already set, loop top will handle blocking
                from rate_limiter import rate_limiter
                if rate_limiter.is_blocked():
                    continue

                # P2: LLM infra error (SDK signature/timeout/connection) — short backoff.
                # cost == -0.5 sentinel from cycle_failed+infra_error path. Session already
                # cleared inside _run_one_cycle's except handler, so no redundant clear here.
                # Was previously misclassified as "API auth error (401/403)" with 300s backoff
                # (the v97 1.5h stuck loop: signature storm → -1.0 → 300s → restart → repeat).
                if cost == -0.5:
                    _infra_backoff = 15
                    if ui:
                        ui.log_history(
                            f"Orchestrator: LLM infrastructure error (SDK signature/timeout/connection). "
                            f"Backing off {_infra_backoff}s (short, NOT auth).", "warn")
                    try:
                        _orch.log_system_event("pipeline.infra_error_short_backoff", "warn",
                            f"Infra error short backoff {_infra_backoff}s",
                            {"cost_signal": cost})
                    except Exception:
                        pass
                    if shutdown_mgr:
                        try:
                            await _orch.asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=_infra_backoff)
                            break
                        except _orch.asyncio.TimeoutError:
                            pass
                    else:
                        await _orch.asyncio.sleep(_infra_backoff)
                    # Session already cleared in _run_one_cycle except handler (infra path).
                    # Preserve the generation identity by resuming from the active checkpoint
                    # on the next loop; otherwise Phase 1 may select a new source/crossover
                    # while pipeline_state.json still points at the interrupted generation.
                    recovery = _orch._checkpoint_recovery_context("infra_error", ui)
                    continue

                # cost <= -1.0 lands here. Two distinct causes share this signal:
                #   (a) a genuine auth failure (401/403, set auth_error=True above) —
                #       credentials won't self-heal, so a long backoff is correct.
                #   (b) a generic cycle failure (crash/ProcessError/exit-143) with NO
                #       auth_error flag — usually another face of the transient SDK
                #       signature storm. Treating these as auth and waiting 300s each
                #       time turned a brief SDK hiccup into a multi-hour stuck loop.
                # Split them: real auth keeps 300s; generic failures get a short,
                # escalating backoff (30s -> 60s -> 120s -> cap 300s).
                # auth_error 只在 _run_one_cycle 作用域声明，orchestrator_loop 无法直接读
                # (root-cause-audit 2026-06-21: 引用未定义变量致 NameError crash)。从 cost
                # 推断：auth 失败返回 -max(abs(cost),1.0) (< -1.0)，generic crash 返回 -1.0。
                auth_error = cost < -1.0
                if auth_error:
                    if ui:
                        ui.log_history("Orchestrator: API auth error (401/403). Backing off 300s.", "error")
                    _wait = 300
                else:
                    _gen_fail_count = getattr(_orch.orchestrator_loop, "_gen_fail_count", 0) + 1
                    _orch.orchestrator_loop._gen_fail_count = _gen_fail_count
                    _wait = min(30 * (2 ** min(_gen_fail_count - 1, 3)), 300)
                    if ui:
                        ui.log_history(
                            f"Orchestrator: cycle failed (generic, not auth). "
                            f"Backing off {_wait}s (consecutive #{_gen_fail_count}).", "warn")
                if shutdown_mgr:
                    try:
                        await _orch.asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=_wait)
                        break
                    except _orch.asyncio.TimeoutError:
                        pass
                else:
                    await _orch.asyncio.sleep(_wait)
                _orch._clear_orchestrator_session()
                continue

            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                break

            await _orch.asyncio.sleep(5)

    except _orch.OperatorGenerationCostLimitExceeded as exc:
        # Deterministic checkpoint routes can execute LLM roles without opening
        # an Orchestrator SDK stream.  Park those paths at the same operator
        # boundary instead of reporting a generic orchestrator crash.
        _orch._clear_orchestrator_session(reason="operator_generation_cost_limit")
        if ui:
            ui.set_status("Stopped: operator generation cost limit", is_working=False)
            ui.log_history(str(exc), "error")
            _orch._project_generation_cost_runtime(ui)
        _orch.log.error("Operator generation cost limit stopped evolution: %s", exc)
        terminal_outcome = _orch.ORCH_OPERATOR_COST_LIMIT_COST
    except _orch.EvalSourceRatingIneligible as exc:
        # The active bot chosen as the eval source is structurally excluded
        # from the rating pool (e.g. a staging master published without a full
        # signed certificate), so the daemon schedules 0 matches for it and
        # wait_for_daemon_eval would loop forever on an unreachable floor.
        # Park for operator action instead of silently degrading the floor.
        _orch._clear_orchestrator_session(reason="eval_source_rating_ineligible")
        try:
            _orch.log_system_event(
                "orchestrator.eval_source_rating_ineligible",
                "error",
                str(exc),
                {
                    "bot_name": exc.bot_name,
                    "version": exc.version,
                    "issues": list(exc.issues),
                    "publication_tier": exc.publication_tier,
                    "operator_action_required": True,
                },
            )
        except Exception:
            pass
        if ui:
            ui.set_status(
                f"Stopped: eval source {exc.bot_name} rating-ineligible",
                is_working=False,
            )
            ui.log_history(
                f"评估源 {exc.bot_name} (v{exc.version}) 不在评分池"
                f"（publication_tier={exc.publication_tier}，资格: "
                f"{list(exc.issues)}）；需完成 full 认证后评级 daemon 才会排赛。"
                "停车等待。",
                "error",
            )
            ui.log_history(str(exc), "error")
        _orch.log.error("Eval source rating-ineligible stopped evolution: %s", exc)
        terminal_outcome = _orch.ORCH_OPERATOR_ACTION_REQUIRED_COST
    except _orch.LLMAvailabilityBlocked as exc:
        # Defensive boundary for an LLM role outside the normal stream/direct
        # route wrappers. Never relabel a provider stop as an orchestrator crash.
        try:
            _orch.persist_llm_pause(exc)
        except Exception as pause_exc:
            _orch.log.exception("Failed to persist outer-loop LLM pause: %s", pause_exc)
        _orch._clear_orchestrator_session(reason="outer_llm_availability_blocked")
        if ui:
            ui.set_status(f"Stopped: LLM unavailable ({exc.issue.category})", is_working=False)
            ui.log_history(str(exc), "error")
        _orch.log.error("LLM availability stopped evolution: %s", exc)
        terminal_outcome = _orch.ORCH_LLM_AVAILABILITY_BLOCKED_COST
    except _orch.LLMAvailabilityPauseError as exc:
        _orch._clear_orchestrator_session(reason="llm_availability_state_invalid")
        if ui:
            ui.set_status("Stopped: LLM availability state invalid", is_working=False)
            ui.log_history(str(exc), "error")
        _orch.log.error("LLM availability control stopped evolution: %s", exc)
        terminal_outcome = _orch.ORCH_LLM_AVAILABILITY_BLOCKED_COST
    except _orch.asyncio.CancelledError:
        if ui:
            ui.set_status("Stopped", is_working=False)
            ui.log_history("Orchestrator stopped.", "warn")
        _orch.log_system_event("orchestrator.stopped", "warn", "Orchestrator stopped")
        try:
            from server.state import app_state
            app_state.set_running(False)
        except Exception as e:
            _orch.log.debug("Loop cleanup error: %s", e)
    except Exception as e:
        if ui:
            ui.log_history(f"Orchestrator crashed: {e}", "error")
        _orch.log_system_event("orchestrator.crashed", "error", f"Orchestrator crashed: {e}",
                         {"error": str(e)[:200]})
        _orch._clear_orchestrator_session()
        # Preserve checkpoint for crash recovery regardless of error type.
        # The checkpoint stage-tracking allows startup recovery to assess state.
        try:
            from server.state import app_state
            app_state.set_running(False)
        except Exception as e:
            _orch.log.debug("Loop error cleanup: %s", e)
        terminal_outcome = -1.0
    finally:
        try:
            from server.state import app_state
            app_state.set_running(False)
        except Exception as e:
            _orch.log.debug("Loop final cleanup error: %s", e)
        if _branch_guard_task is not None and not _branch_guard_task.done():
            _branch_guard_task.cancel()
            try:
                await _branch_guard_task
            except _orch.asyncio.CancelledError:
                pass
        if (
            _stability_maintenance_task is not None
            and not _stability_maintenance_task.done()
        ):
            _stability_maintenance_task.cancel()
            try:
                await _stability_maintenance_task
            except _orch.asyncio.CancelledError:
                pass
        if not _watchdog_task.done():
            _watchdog_task.cancel()
            try:
                await _watchdog_task
            except _orch.asyncio.CancelledError:
                pass
        if _daemon_stop is not None:
            _daemon_stop.set()
        if _runtime_hard_stop_event.is_set():
            try:
                from daemon_management import stop_daemon
                await _orch.run_blocking_isolated(
                    stop_daemon,
                    thread_name_prefix="daemon-shutdown",
                )
                _orch.log_system_event(
                    "repo.runtime_branch_drift_cleanup",
                    "info",
                    "Stopped daemon after runtime branch drift",
                )
            except Exception as e:
                _orch.log_system_event(
                    "repo.runtime_branch_drift_cleanup_failed",
                    "warn",
                    f"Failed to stop daemon after runtime branch drift: {e}",
                    {"error": str(e)[:300]},
                )

    return terminal_outcome


# ---------------------------------------------------------------------------
# One-ahead draft-prepare helper (Slice 2b fill-bubble scheduler)
# ---------------------------------------------------------------------------
#
# After gen N is sealed at workers_done, the consumer gate chain
# (quality->review->critic->precommit->commit) runs in the background.
# While it runs, the producer is cleared to begin preparing gen N+1 in a
# "draft" slot so the 2-permit LLM pool stays busy instead of spin-waiting.
#
# This helper is BEST-EFFORT:
#   * Failure is non-fatal; the canonical loop continues unchanged.
#   * The draft is launched at most once per sealed candidate (guarded by the
#     existence of a "draft" slot checkpoint).
#   * gen_count and identity bookkeeping are NOT affected.
#   * Draft promotion happens at the commit_bot publication barrier.
#
# Phase 5b: ``prepare_generation`` now accepts ``slot_id`` and the draft task
# runs inside an ``active_slot_override('draft')`` ContextVar so every
# checkpoint read/write transparently targets ``pipeline_state_draft.json``.
# The draft advances selected -> preparing -> prepared -> direction_audited ->
# master_planned -> workers_done and stops; the consumer gate chain and the
# Slice 2b seal-at-workers-done seam are NOT triggered for the draft.  When
# gen N publishes, the promotion barrier promotes the draft checkpoint to the
# primary slot (see ``orchestrator_deterministic_route``).



def _reconcile_orphan_draft_at_boot(ui):
    """P0-3b: reconcile an orphan Slice 2b one-ahead draft at boot.

    The fire-and-forget ``_draft_prepare_task`` dies with the process, so a
    draft checkpoint left mid-flight on restart can never advance on its own.
    Without this reconcile it deadlocks ``_try_launch_draft_prepare`` (which
    sees a non-None draft and returns early forever).  Logic:

      * workers_done -- the intended one-ahead buffer; best-effort promote it
        via ``_maybe_promote_draft_to_primary`` (CAS-safe + idempotent).  Do
        NOT unconditionally reap a complete pre-computed candidate.
      * any other stage -- the driving task is dead; REAP it so a fresh draft
        can launch.  This is the deadlock fix.
      * defense-in-depth -- a draft whose next_v/source_v is at or behind the
        published high-water (the gen it was preparing behind already shipped)
        and is not promotable is also reaped.

    Every branch is wrapped so boot never crashes on a draft-reconcile error.
    """
    try:
        from evolution_infra import read_all_pipeline_checkpoints
    except Exception:
        return
    try:
        slots = read_all_pipeline_checkpoints()
    except Exception:
        return
    draft = slots.get("draft") if isinstance(slots, dict) else None
    if not isinstance(draft, dict) or not draft:
        # Normal case: no orphan draft.  Keep the hot path cheap.
        return

    stage = str(draft.get("stage") or "")
    next_v = 0
    source_v = 0
    try:
        next_v = int(draft.get("next_v") or 0)
        source_v = int(draft.get("source_v") or 0)
    except (TypeError, ValueError):
        pass

    try:
        _orch.log_system_event(
            "pipeline.orphan_draft_reconciled",
            "info",
            f"Orphan draft checkpoint found at boot (stage={stage}, "
            f"next_v={next_v}, source_v={source_v}); reconciling",
            {"stage": stage, "next_v": next_v, "source_v": source_v},
        )
    except Exception:
        pass

    # Resolve the published high-water for the stale check.
    published_high_water = 0
    try:
        from epoch_authority import strict_epoch_projection

        projection = strict_epoch_projection()
        published_high_water = int(projection.get("published_high_water") or 0)
    except Exception:
        published_high_water = 0

    is_stale = (
        published_high_water > 0
        and next_v > 0
        and next_v <= published_high_water
    )
    # COLLISION CHECK: a draft whose next_v equals the PRIMARY's in-flight
    # next_v is a version collision (the one-ahead draft must be strictly
    # ahead of the primary).  This arises when a prior primary was rejected/
    # abandoned and the orphaned draft reservation persisted a stale next_v
    # that the new primary then claimed.  Reap it so the next launch allocates
    # a fresh primary_next_v + 1 (reserve_draft_version now reconciles this,
    # but a stale draft CHECKPOINT file also needs clearing at boot).
    if not is_stale and next_v > 0:
        try:
            from evolution_infra import no_slot_override, read_pipeline_checkpoint

            with no_slot_override():
                _primary_ckpt = read_pipeline_checkpoint()
            if isinstance(_primary_ckpt, dict):
                _primary_next_v = int(_primary_ckpt.get("next_v") or 0)
                if _primary_next_v > 0 and next_v == _primary_next_v:
                    is_stale = True
        except Exception:
            pass

    if stage == "workers_done":
        # Complete one-ahead buffer: best-effort promote (CAS-safe).  If the
        # promotion refuses (primary still active / version mismatch) the
        # draft stays as a valid promotable buffer -- do NOT reap it.
        try:
            from generation_scheduler import _maybe_promote_draft_to_primary

            promoted = _maybe_promote_draft_to_primary()
        except Exception:
            promoted = False
        if promoted:
            try:
                _orch.log_system_event(
                    "pipeline.orphan_draft_promoted",
                    "info",
                    "Orphan workers_done draft promoted to primary slot at boot",
                    {"next_v": next_v, "source_v": source_v},
                )
            except Exception:
                pass
        elif is_stale:
            # A complete-but-stale draft behind an already-shipped version is
            # dead weight; reap rather than leaving an un-promotable marker.
            try:
                from evolution_infra import clear_pipeline_checkpoint

                clear_pipeline_checkpoint(slot_id="draft")
                _orch.log_system_event(
                    "pipeline.orphan_draft_reaped",
                    "info",
                    "Reaped stale workers_done draft at or behind "
                    f"published high-water {published_high_water}",
                    {"stage": stage, "next_v": next_v, "source_v": source_v},
                )
            except Exception:
                pass
        # else: leave the valid workers_done buffer in place for promotion.
        return

    # Any other stage: the driving task is dead, so the draft can never
    # advance on its own.  REAP it (this breaks the launch deadlock).
    try:
        from evolution_infra import clear_pipeline_checkpoint

        clear_pipeline_checkpoint(slot_id="draft")
        _orch.log_system_event(
            "pipeline.orphan_draft_reaped",
            "info",
            f"Reaped orphan mid-flight draft (stage={stage}) at boot; "
            "the driving draft task died with the previous process",
            {"stage": stage, "next_v": next_v, "source_v": source_v},
        )
    except Exception:
        pass

async def _parked_sleep(shutdown_mgr, seconds: float = 45.0) -> bool:
    """Bounded, shutdown-interruptible sleep used when the primary is parked.

    When Slice 2b parks the primary at ``workers_done`` for the background
    consumer gate chain, the primary has nothing productive to do until the
    consumer either promotes the candidate or rejects it.  Re-cycling every
    second (the previous behavior) wastes CPU and floods the log/event stream
    with "Resuming at workers_done" + incrementing cycle numbers (observed:
    cycles 1006..1013 in ~27s).  Instead, sleep for a bounded interval that is
    still short enough to react promptly once the consumer finishes and the
    promotion barrier clears.

    Returns ``True`` if shutdown fired during the wait (caller should break),
    ``False`` if the timeout elapsed normally.
    """
    if shutdown_mgr:
        try:
            await _orch.asyncio.wait_for(
                shutdown_mgr.wait_for_shutdown(), timeout=seconds
            )
            return True  # shutdown fired
        except _orch.asyncio.TimeoutError:
            return False
    await _orch.asyncio.sleep(seconds)
    return False


async def _ensure_slice2b_consumer_running(checkpoint: dict) -> None:
    """Ensure the Slice 2b consumer gate-chain task is actually running.

    The primary lane parks at ``workers_done`` when a sealed candidate's
    consumer owns the canonical gate chain (quality->review->critic->precommit
    ->commit).  ``recover_at_boot`` re-stashes the consumer factory after a
    process restart, but it does NOT launch the asyncio.Task -- only
    ``ensure_consumer_running`` (or the original seal seam) does.  Without
    this drive, the primary parks forever waiting for a consumer that is not
    running (restart deadlock), while the loop spins re-emitting the same
    "Resuming at workers_done" line every second.

    This is idempotent: if the consumer task is already live it returns it
    immediately; if no candidate is sealed/in-flight it is a no-op.
    """

    try:
        from producer_consumer_slice2b_activation import slice2b_active
    except Exception:
        return
    if not slice2b_active():
        return
    try:
        activation = _orch._slice2b_activation_registry("get")
    except Exception:
        activation = None
    if activation is None:
        return
    # Resolve the sealed candidate id for the parked primary checkpoint.  Only
    # drive the consumer for a candidate that is sealed but not terminal; a
    # terminal candidate has already published/been-rejected and the promotion
    # barrier / inline path owns the rest.
    next_v = int(checkpoint.get("next_v") or 0)
    if next_v <= 0:
        return
    candidate_id = str(checkpoint.get("candidate_id") or f"candidate-v{next_v}")
    try:
        snapshot = activation.ledger.snapshot(candidate_id)
    except Exception:
        snapshot = None
    if snapshot is None:
        return
    try:
        if activation.ledger.is_terminal(candidate_id):
            return
    except Exception:
        return
    # Re-stash the sealed snapshot into the in-memory registry if it was lost
    # across the restart (recover_at_boot does this too, but be defensive).
    if candidate_id not in activation._sealed_snapshots:
        try:
            recovered = activation.ledger.recover_snapshot(candidate_id)
        except Exception:
            recovered = None
        if recovered is not None:
            activation._sealed_snapshots[candidate_id] = dict(recovered)
            timing_plan = recovered.get("quality_native_match_timing_plan") or {}
            activation._dispatch_clocks[candidate_id] = float(
                timing_plan.get("submitted_at_epoch") or 0.0
            )
    # Ensure a factory is scheduled (recover_at_boot schedules it; if for any
    # reason it is missing, schedule the canonical chain now).
    if candidate_id not in activation._scheduled_factories:
        try:
            from producer_consumer_slice2b_activation import (
                canonical_gate_runner_factory,
            )

            rv = int(snapshot.get("next_v") or next_v)
            sv = int(snapshot.get("source_v") or 0)
            activation.schedule_consumer(
                candidate_id=candidate_id,
                gate_runner_factory=canonical_gate_runner_factory(rv, sv),
            )
        except Exception:
            pass
    # Launch (or confirm) the consumer task.  This is the load-bearing call:
    # without it the gate chain never runs after a restart.
    try:
        await activation.ensure_consumer_running(candidate_id)
    except Exception:
        pass


async def _try_launch_draft_prepare(ui, shutdown_mgr, gen_count):
    """Best-effort one-ahead draft prepare for gen N+1 after a seal.

    Called from the continuous loop after a sealed candidate (gen N) has its
    consumer gate chain running in the background and
    ``advanced["terminal_action"]`` is ``None`` or ``slice2b_consumer_parked``.
    Checks the Slice 2b activation path and the one-ahead coordinator's draft
    prepare gate, then launches a fire-and-forget asyncio task that drives the
    draft through its LLM stages.  Never raises and never blocks the loop.

    This coroutine runs ON the event loop on purpose: it ends by calling
    ``_orch.asyncio.create_task(_draft_prepare_task(...))`` (create_task needs a
    running loop in the current thread). The blocking slot-checkpoint read
    (``read_all_pipeline_checkpoints``) is awaited via
    ``run_blocking_isolated`` so it never stalls the loop (AGENTS.md blocking
    boundary); the subsequent ``create_task`` still runs on the loop.
    """

    # Lazy import through the sanctioned activation seam (inertness fence:
    # orchestrator.py must not name producer_consumer_slice2b directly).
    try:
        from producer_consumer_slice2b_activation import (
            slice2b_active,
        )
    except Exception:
        return
    if not slice2b_active():
        return

    # Resolve the per-process activation + one-ahead coordinator through the
    # same registry used by the seal seam in orchestrator_deterministic_route.
    # Use the lazy ensure helper so the activation exists on a clean start
    # before any seal has ever fired (otherwise the speculative-draft-during-
    # eval-wait path never finds an activation and the LLM stays idle).
    try:
        activation = _orch._slice2b_ensure_activation()
    except Exception:
        activation = None
        try:
            activation = _orch._slice2b_activation_registry("get")
        except Exception:
            pass
    if activation is None:
        # No live activation yet; the seal seam would also have refused, so
        # there is no sealed candidate to fill behind.  Nothing to do.
        return

    try:
        may_prepare = bool(
            activation.producer_may_draft_behind()
            or activation.producer_may_draft_ahead_of_eval()
        )
    except Exception as exc:  # pragma: no cover - defensive
        # A bug here (e.g. a missing accessor on the activation) previously
        # raised AttributeError that was silently swallowed, disabling the
        # one-ahead producer entirely and leaving the LLM idle.  Log loudly so
        # the regression cannot hide again.
        try:
            _orch.log_system_event(
                "orchestrator.slice2b_draft_prepare_guard_failed",
                "warn",
                f"_try_launch_draft_prepare gate raised {type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        return
    if not may_prepare:
        # Multi-ahead buffer is full (number of sealed-but-unresolved
        # candidates has reached max_ahead) AND no eval-wait-ahead slot is
        # free; no room for another draft.
        return

    # De-duplicate: at most one in-flight draft per draft slot.  A draft slot
    # checkpoint is the durable marker that the slot is occupied.
    try:
        from evolution_infra import (
            clear_pipeline_checkpoint,
            read_pipeline_checkpoint,
            read_all_pipeline_checkpoints,
            is_draft_slot,
        )
    except Exception:
        return
    occupied_slots = set()
    try:
        # read_all_pipeline_checkpoints globs + parses every pipeline_state
        # slot JSON from disk (AGENTS.md blocking boundary).  Run it in an
        # owned worker thread so the ASGI event loop is not blocked while the
        # primary sits in wait_for_daemon_eval (this fires every ~30s).
        all_slots = await _orch.run_blocking_isolated(
            read_all_pipeline_checkpoints,
            thread_name_prefix="draft-slot-scan",
        )
    except Exception:
        all_slots = {}
    for sid in all_slots:
        if is_draft_slot(sid):
            occupied_slots.add(sid)

    # Pick the next free draft slot.  Single-ahead (max_ahead==1) uses the
    # legacy unprefixed "draft" slot; multi-ahead uses numbered slots
    # draft1, draft2, ... up to max_ahead.
    max_ahead = int(getattr(activation.coordinator, "max_ahead", 1))
    launched_any = False
    for n in range(1, max_ahead + 1):
        # The slot loop must honor BOTH gating modes: drafting behind a sealed
        # candidate (producer_may_draft_behind) AND speculative drafting ahead
        # of an eval-waiting primary (producer_may_draft_ahead_of_eval).  The
        # former gate alone would break immediately when nothing is sealed,
        # silently disabling the eval-wait speculative draft even though the
        # top-of-function gate already admitted it.
        try:
            _may = activation.producer_may_draft_behind() or activation.producer_may_draft_ahead_of_eval()
        except Exception:
            _may = False
        if not _may:
            break
        if max_ahead == 1:
            candidate_slot = "draft"  # legacy single-ahead slot
        else:
            candidate_slot = f"draft{n}"
        if candidate_slot in occupied_slots:
            continue
        # Do not start a speculative draft while a shutdown is in flight.
        try:
            if shutdown_mgr is not None and shutdown_mgr.is_shutting_down:
                return
        except Exception:
            pass
        # Launch the draft prepare as a fire-and-forget background task.  The
        # draft runs prepare -> direction_audit -> Master -> Workers into
        # pipeline_state_<slot>.json, filling the LLM permit idle time while gen
        # N's consumer gate chain runs concurrently.  It is fenced by the
        # ahead coordinator (high-water=max_ahead) and the slot existence check.
        try:
            _draft_task = _orch.asyncio.create_task(
                _draft_prepare_task(ui, shutdown_mgr, gen_count, slot_id=candidate_slot)
            )
            # Hold a strong reference for the task's lifetime so it is not
            # garbage-collected before completion (asyncio foot-gun).
            _PENDING_DRAFT_TASKS.add(_draft_task)
            _draft_task.add_done_callback(_PENDING_DRAFT_TASKS.discard)
            occupied_slots.add(candidate_slot)
            launched_any = True
            _orch.log_system_event(
                "orchestrator.slice2b_draft_prepare_launched",
                "info",
                f"Ahead draft prepare launched for slot {candidate_slot}",
                {"gen_count": gen_count, "slot_id": candidate_slot},
            )
        except Exception:
            # If the task cannot be scheduled (e.g. no running loop), clear any
            # partial draft marker so the next tick can retry.  Offload the
            # file write off the event loop (AGENTS.md blocking boundary).
            try:
                await _orch.run_blocking_isolated(
                    clear_pipeline_checkpoint,
                    slot_id=candidate_slot,
                    thread_name_prefix="draft-slot-clear",
                )
            except Exception:
                pass
    return


async def _draft_prepare_task(ui, shutdown_mgr, gen_count, slot_id="draft"):
    """Fire-and-forget draft-prepare task body (P0-3a classify-then-decide).

    Module-level so the exception handler is directly testable without mocking
    the slice2b activation gates that guard ``_try_launch_draft_prepare``.
    A transient LLM/infra failure (ClaudeSDKError incl. 429 quota,
    asyncio.TimeoutError, ConnectionError, OSError) or any generic/unknown
    Exception PRESERVES the draft checkpoint so the completed expensive LLM
    work survives.  A preserved mid-flight draft is handled by the boot-time
    reconcile (``_reconcile_orphan_draft_at_boot``) on restart, or resumed by
    the next loop tick's ``draft_stage_advance`` recovery.  Genuine terminal
    conditions (routing loop / terminal action) are already cleared INSIDE
    ``_run_draft_cycle``, so the exception path never needs an unconditional
    clear.  This mirrors the primary generation slot's infra-error handling
    (preserve checkpoint + discard provider session) rather than wiping it.

    ``slot_id`` selects which multi-ahead draft slot the task drives (``draft``
    for single-ahead / legacy; ``draft1``/``draft2``/... for multi-ahead).
    """
    try:
        await _run_draft_cycle(ui, shutdown_mgr, gen_count, slot_id=slot_id)
    except Exception as exc:
        # P0-3a: classify-then-decide.  A transient LLM/infra failure
        # (ClaudeSDKError incl. 429 quota, asyncio.TimeoutError,
        # ConnectionError, OSError) or any unrecognized Exception PRESERVES
        # the draft checkpoint so completed expensive LLM work survives.  A
        # preserved mid-flight draft is handled by the boot-time reconcile
        # (``_reconcile_orphan_draft_at_boot``) on restart, or resumed by the
        # next loop tick's ``draft_stage_advance`` recovery.  Genuine
        # terminal conditions (routing loop / terminal action) are already
        # cleared INSIDE ``_run_draft_cycle``, so the exception path never
        # clears.  This mirrors the primary generation slot's infra-error
        # handling (preserve checkpoint + discard provider session).
        try:
            from llm_failure import is_llm_infra_error

            transient = is_llm_infra_error(exc)
        except Exception:
            transient = False
        try:
            _orch.log_system_event(
                "orchestrator.slice2b_draft_prepare_failed",
                "warn",
                f"One-ahead draft prepare failed "
                f"({'transient' if transient else 'unknown'}): "
                f"{type(exc).__name__}: {exc}; "
                "draft checkpoint retained for resume/reap",
            )
        except Exception:
            pass


async def _run_draft_cycle(ui, shutdown_mgr, gen_count, *, slot_id="draft"):
    """Drive the one-ahead draft (gen N+1) through its LLM stages.

    Runs entirely inside an ``active_slot_override(slot_id)`` asyncio task
    context so that every checkpoint read/write performed by
    ``prepare_generation`` and the deterministic stage router transparently
    targets ``pipeline_state_<slot_id>.json``.  The draft advances:

        selected -> preparing -> prepared        (prepare_generation)
        prepared -> direction_audited            (run_direction_audit)
        direction_audited -> master_planned      (run_master)
        master_planned -> workers_done           (execute_workers)

    and STOPS at ``workers_done``.  It deliberately does NOT run the consumer
    gate chain (quality->review->critic->precommit->commit) nor trigger the
    Slice 2b seal-at-workers_done seam: the draft is a pre-computed candidate
    that is promoted to the primary slot when gen N publishes (see the
    promotion barrier), after which the canonical primary loop owns its gate
    chain and publication.

    ``slot_id`` selects the multi-ahead draft slot (``draft`` for single-ahead /
    legacy; ``draft1``/``draft2``/... for multi-ahead).  Best-effort and
    non-fatal: any exception clears the draft slot so the next loop tick can
    re-prepare from scratch.
    """
    from evolution_infra import active_slot_override, clear_pipeline_checkpoint
    from generation_scheduler import prepare_generation as _prepare_generation

    with active_slot_override(slot_id):
        # Phase 1: prepare the draft generation into the draft slot.  This
        # computes the one-ahead target (primary_next_v + 1, or a distinct
        # reserved version for multi-ahead) and writes the ``selected``
        # checkpoint to pipeline_state_<slot_id>.json.
        draft_ctx = await _prepare_generation(
            shutdown_mgr, ui, slot_id=slot_id
        )
        if draft_ctx is None:
            # Prepare refused (handoff, epoch, workflow guard, shutdown, ...).
            # Nothing to drive; leave the draft slot clean.
            try:
                _orch.log_system_event(
                    "orchestrator.eval_wait_draft_prepare_refused",
                    "warn",
                    f"draft prepare_generation returned None for slot {slot_id}; draft will not advance",
                    {"slot_id": slot_id},
                )
            except Exception:
                pass
            clear_pipeline_checkpoint(slot_id=slot_id)
            return

        # Phases 2-4: drive the deterministic stage router one stage at a time
        # until the draft reaches workers_done.  Each iteration re-reads the
        # draft checkpoint (via the slot override) and routes the next safe
        # tool.  We cap the number of iterations to defend against any routing
        # loop; the happy path is exactly four routed stages.
        max_iterations = 12
        for _ in range(max_iterations):
            if shutdown_mgr is not None and shutdown_mgr.is_shutting_down:
                # Stop pre-computing on shutdown; leave the draft checkpoint in
                # place for a future session to promote or reap.
                return
            recovery = _orch._checkpoint_recovery_context(
                "draft_stage_advance",
                ui,
                log_level="info",
                label="[Draft]",
            )
            if not recovery or recovery.get("action") != "resume":
                # No resumable draft checkpoint (clean/abandoned/blocked) --
                # the draft is done or stuck; stop driving it.
                return
            checkpoint = recovery.get("checkpoint") or {}
            stage = checkpoint.get("stage")
            if stage == "workers_done":
                # Draft pre-computation complete.  Do NOT route
                # run_quality_gates: that would trigger the Slice 2b
                # seal-at-workers-done seam and launch a consumer for the
                # draft, which is not what we want.  Leave the draft sitting
                # at workers_done for the promotion barrier to promote.
                try:
                    _orch.log_system_event(
                        "orchestrator.slice2b_draft_workers_done",
                        "info",
                        "One-ahead draft reached workers_done; awaiting promotion",
                        {
                            "next_v": checkpoint.get("next_v"),
                            "source_v": checkpoint.get("source_v"),
                            "gen_count": gen_count,
                        },
                    )
                except Exception:
                    pass
                return

            # Route exactly one stage.  _advance_deterministic_recovery drives
            # the handler (prepare_next_gen / run_direction_audit / run_master /
            # execute_workers) and writes the result back to the draft slot via
            # the override.  Terminal actions (abandon, handoff) end the draft.
            advanced = await _orch._advance_deterministic_recovery(
                recovery,
                ui,
                cost_policy=_orch.load_operator_generation_cost_policy(),
                shutdown_mgr=shutdown_mgr,
                log_level="info",
                label="[Draft]",
                gen_ctx=draft_ctx,
                gen_count=gen_count,
            )
            terminal_action = advanced.get("terminal_action")
            if terminal_action:
                # The draft reached a terminal state (e.g. its worker stage
                # abandoned, or classification produced a terminal action).
                # Clear the draft slot -- it cannot be promoted.
                try:
                    _orch.log_system_event(
                        "orchestrator.slice2b_draft_terminal",
                        "info",
                        f"One-ahead draft reached terminal action "
                        f"{terminal_action}; clearing draft slot",
                        {"terminal_action": terminal_action,
                         "gen_count": gen_count},
                    )
                except Exception:
                    pass
                clear_pipeline_checkpoint(slot_id=slot_id)
                return
            if not advanced.get("routed"):
                # Could not route (e.g. blocked recovery).  Stop driving; the
                # draft checkpoint stays for inspection or a later retry.
                return
        # Iteration cap hit without reaching workers_done: clear the partial
        # draft so the next tick starts fresh rather than churning.
        try:
            _orch.log_system_event(
                "orchestrator.slice2b_draft_iteration_cap",
                "warn",
                "One-ahead draft exceeded stage-advance iteration cap; clearing",
                {"gen_count": gen_count, "slot_id": slot_id},
            )
        except Exception:
            pass
        clear_pipeline_checkpoint(slot_id=slot_id)
