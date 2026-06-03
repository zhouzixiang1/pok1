"""Evolution Orchestrator — LLM-driven bot evolution pipeline.

Usage (standalone CLI):
    python orchestrator/orchestrator.py              # Run continuous evolution
    python orchestrator/orchestrator.py --one-gen    # Run one generation then stop
    python orchestrator/orchestrator.py --dry-run    # Only check status, no changes

Usage (from dashboard/backend/app.py):
    from orchestrator.orchestrator import orchestrator_loop
    await orchestrator_loop(web_ui, no_daemon=False)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_agent_sdk import (
    query as claude_query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    CLINotFoundError,
    ProcessError,
)
from tools import evolution_server, inject_ui
from shutdown_manager import ShutdownManager
from system_log import log_system_event, set_ui as set_system_log_ui
import logging

log = logging.getLogger("pok.orchestrator")

ORCHESTRATOR_PROMPT = (Path(__file__).parent / "prompts" / "orchestrator.md").read_text()
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

from orchestrator_context import _build_context, _make_precompact_hook  # noqa: E402
from orchestrator_session import (  # noqa: E402
    _rotate_orchestrator_logs, _is_rate_limited,
    _save_orchestrator_session, _load_orchestrator_session, _clear_orchestrator_session,
    _startup_recovery,
)
from evolution_infra import find_current_v  # noqa: E402
async def _run_one_cycle(ui, log_file, one_gen=False, dry_run=False, max_turns=None, gen_ctx=None, shutdown_mgr=None):
    """Run one Orchestrator cycle (one LLM agent session). Returns total cost."""
    context = _build_context(one_gen=one_gen, dry_run=dry_run, gen_ctx=gen_ctx)
    prompt = ORCHESTRATOR_PROMPT.replace("{context}", context)

    if dry_run:
        prompt += "\n\nIMPORTANT: This is a DRY RUN. Only call get_status() and report the current state. Do NOT modify anything."

    # Session resume: if orchestrator_session.json exists (written on every tool call),
    # the previous cycle was interrupted — resume the exact conversation.
    # The file is cleared on natural cycle completion, so its presence reliably means
    # the process was killed mid-gen.  No need to gate this on pipeline_state.json.
    from evolution_core import read_pipeline_checkpoint
    checkpoint = read_pipeline_checkpoint()
    saved_session_id = _load_orchestrator_session()

    resume_kwargs = {"resume": saved_session_id} if saved_session_id else {}
    if saved_session_id and ui:
        stage_info = checkpoint.get("stage", "unknown") if checkpoint else "no checkpoint"
        ui.log_history(
            f"[Orchestrator] Resuming session {saved_session_id[:8]}... "
            f"(pipeline stage={stage_info})",
            "warn",
        )

    from evolution_core import _BLOCKED_MCP_TOOLS
    options = ClaudeAgentOptions(
        model="sonnet",
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
        mcp_servers={"evolution": evolution_server},
        strict_mcp_config=True,
        disallowed_tools=_BLOCKED_MCP_TOOLS,
        hooks=_make_precompact_hook(),
        max_turns=max_turns,
        thinking={"type": "adaptive", "display": "summarized"},
        **resume_kwargs,
    )

    total_cost = 0.0
    cycle_completed = False
    auth_error = False

    with open(log_file, "a") as lf:
        lf.write(f"\n{'='*60}\n[ORCHESTRATOR CYCLE] {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n")
        lf.write(f"[PROMPT]\n{prompt}\n\n[OUTPUT]\n")

        async def _stream_response(opts, max_retries=3):
            """Run a single streaming query. Returns (full_text, cost, cycle_ok, gen, auth_error)."""
            texts = []
            cost = 0.0
            ok = False
            gen = None
            auth_err = False
            try:
                gen = claude_query(prompt=prompt, options=opts)
                async for message in gen:
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                texts.append(block.text)
                                if ui:
                                    ui.log_io(block.text, "claude", "Orchestrator")
                                else:
                                    log.debug("%s", block.text.rstrip())
                                    print(block.text, end="", flush=True)
                                lf.write(block.text)
                            elif isinstance(block, ToolUseBlock):
                                if ui:
                                    ui.log_history(f"[Orchestrator] Calling tool: {block.name}", "info")
                                    ui.log_io(f"\n[tool: {block.name}]", "tool", "Orchestrator")
                                    ui.emit_tool_call(block.name, block.input, "Orchestrator")
                                else:
                                    log.info("Calling tool: %s", block.name)
                                    print(f"\n[tool: {block.name}]", end=" ", flush=True)
                                lf.write(f"\n[tool: {block.name}]\n")
                            elif isinstance(block, ThinkingBlock):
                                if ui:
                                    ui.log_io(block.thinking or "[thinking...]", "thinking", "Orchestrator")
                                else:
                                    log.debug("[thinking...]")
                                    print("[thinking...]", end=" ", flush=True)
                            elif isinstance(block, ToolResultBlock):
                                content = block.content if isinstance(block.content, str) else (
                                    json.dumps(block.content, ensure_ascii=False) if block.content is not None else ""
                                )
                                if content and ui:
                                    ui.log_io(content[:3000], "tool_result", "Orchestrator")
                    elif isinstance(message, ResultMessage):
                        if message.total_cost_usd:
                            cost += message.total_cost_usd
                        if not message.is_error:
                            ok = True
                            if message.session_id:
                                _save_orchestrator_session(message.session_id)
                        else:
                            error_text = str(getattr(message, 'error', 'Unknown SDK error'))
                            lf.write(f"\n[API ERROR] {error_text}\n")
                            if ui:
                                ui.log_history(f"[Orchestrator] API error: {error_text[:200]}", "error")
                            _clear_orchestrator_session()
                            if any(code in error_text for code in ["401", "403"]):
                                auth_err = True
            except (CLINotFoundError, ProcessError) as e:
                if ui:
                    ui.log_io(f"[ERROR] {e}", "error", "Orchestrator")
                else:
                    log.error("LLM error: %s", e)
                    print(f"\n[ERROR] {e}")
            return "".join(texts), cost, ok, gen, auth_err

        query_gen = None
        try:
            full_output, total_cost, cycle_completed, query_gen, auth_error = await _stream_response(options)

            # 529 rate-limit retry with exponential backoff
            if _is_rate_limited(full_output):
                _clear_orchestrator_session()
                retry_opts = ClaudeAgentOptions(
                    model="sonnet",
                    permission_mode="bypassPermissions",
                    cwd=str(PROJECT_ROOT),
                    mcp_servers={"evolution": evolution_server},
                    strict_mcp_config=True,
                    disallowed_tools=_BLOCKED_MCP_TOOLS,
                    hooks=_make_precompact_hook(),
                    max_turns=max_turns,
                    thinking={"type": "adaptive", "display": "summarized"},
                )
                for backoff in [30, 60, 120]:
                    if ui:
                        ui.log_history(f"Orchestrator rate limited (529). Retrying in {backoff}s...", "warn")
                    lf.write(f"\n[529 RETRY] backing off {backoff}s\n")
                    if shutdown_mgr:
                        try:
                            await asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=backoff)
                            return total_cost
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(backoff)
                    if query_gen is not None:
                        try:
                            await query_gen.aclose()
                        except Exception:
                            pass
                    full_output, retry_cost, cycle_completed, query_gen, auth_error = await _stream_response(retry_opts)
                    total_cost += retry_cost
                    if not _is_rate_limited(full_output):
                        break

            if ui:
                ui.update_cost("Orchestrator", total_cost, None)
            lf.write(f"\n[CYCLE DONE] cost=${total_cost:.4f}\n")

        except KeyboardInterrupt:
            if query_gen is not None:
                try:
                    await query_gen.aclose()
                except Exception:
                    pass
            if ui:
                ui.log_history("[Orchestrator] Interrupted by user.", "warn")
            else:
                log.warning("Interrupted by user.")
            lf.write("\n[INTERRUPTED]\n")

        except asyncio.CancelledError:
            if query_gen is not None:
                try:
                    await query_gen.aclose()
                except Exception:
                    pass
            # Session file PRESERVED — next startup can resume from checkpoint
            if ui:
                ui.log_history("[Orchestrator] Cancelled — session preserved for resume.", "warn")
            else:
                log.warning("Cancelled — session preserved for resume.")
            lf.write("\n[CANCELLED — session preserved for resume]\n")
            raise

        except Exception as e:
            if query_gen is not None:
                try:
                    await query_gen.aclose()
                except Exception:
                    pass
            # Session file PRESERVED — next startup can assess recovery
            if ui:
                ui.log_history(f"[Orchestrator] Error: {e}", "error")
            else:
                log.error("Error: %s", e)
            lf.write(f"\n[ERROR] {e}\n")

    # Only clear session file on natural (non-error) cycle completion.
    # If killed, the session file remains so next startup can resume.
    if cycle_completed:
        _clear_orchestrator_session()

    # Return negative cost to signal auth error for fast backoff
    if auth_error:
        return -abs(total_cost) if total_cost > 0 else -1.0

    return total_cost


async def orchestrator_loop(ui, shutdown_mgr=None, no_daemon=False, daemon_workers=14, daemon_pairs=5):
    """Orchestrator entry point — three-phase generation loop.

    Args:
        ui: BaseUI instance (WebUI for Dashboard). Can be None for silent mode.
        shutdown_mgr: ShutdownManager for graceful signal handling.
        no_daemon: If True, skip daemon startup.
        daemon_workers: Number of parallel workers for the daemon subprocess.
        daemon_pairs: Mirror pairs per match for the daemon subprocess.
    """
    from tools import inject_ui
    inject_ui(ui)
    set_system_log_ui(ui)

    os.makedirs(LOGS_DIR, exist_ok=True)
    _rotate_orchestrator_logs(LOGS_DIR)

    if ui:
        ui.log_history("🔥 Orchestrator starting...", "success")
        ui.set_header("🔥 LLM Orchestrator Evolution 🔥")

    log_system_event("orchestrator.started", "success", "Orchestrator started",
                     {"daemon_enabled": not no_daemon})
    log.info("Orchestrator loop started (daemon=%s)", not no_daemon)

    # Start daemon
    _daemon_stop = None
    if not no_daemon:
        from evolution_core import start_daemon, daemon_monitor_thread
        import threading
        try:
            start_daemon(workers=daemon_workers, pairs=daemon_pairs)
        except Exception as e:
            if ui:
                ui.log_history(f"Daemon start failed: {e}", "error")
            log.error("Daemon start failed: %s", e)
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

    log_file = LOGS_DIR / f"orchestrator_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    gen_count = 0
    consecutive_prep_fails = 0

    # Startup recovery — assess interrupted state
    recovery = _startup_recovery(ui)

    try:
        while True:
            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                break

            gen_count += 1
            log_system_event("orchestrator.cycle_start", "info", f"Cycle {gen_count} starting",
                             {"gen_count": gen_count})

            # If recovering, skip Phase 1 (context already known from checkpoint)
            if recovery and recovery.get("action") == "resume":
                from generation_scheduler import GenerationContext
                ckpt = recovery["checkpoint"]
                parent2_v = ckpt.get("parent2_v")
                strategy = "crossover" if parent2_v else "master"
                gen_ctx = GenerationContext(
                    current_v=ckpt.get("source_v", find_current_v()),
                    next_v=ckpt["next_v"],
                    strategy=strategy,
                    source_v=ckpt["source_v"],
                    crossover_parents=(ckpt["source_v"], parent2_v) if parent2_v else (),
                    gen_count=gen_count,
                )
                recovery = None  # consume recovery, only used once
            else:
                # Phase 1: Prepare (disposable on interrupt)
                # Use degraded min_games after repeated eval timeouts
                degraded_min = None
                if consecutive_prep_fails >= 3:
                    degraded_min = 30
                    if ui:
                        ui.log_history("评估等待连续超时，降低评估要求 (30 局) 继续进化...", "warn")

                gen_ctx = await _prepare_or_fail(shutdown_mgr, ui, min_games=degraded_min)
                if gen_ctx is None:
                    if shutdown_mgr and shutdown_mgr.is_shutting_down:
                        break
                    consecutive_prep_fails += 1
                    from evolution_infra import is_daemon_alive
                    if not is_daemon_alive() and ui:
                        ui.log_history(f"Daemon 未运行，等待恢复中... (连续失败 {consecutive_prep_fails} 次)", "error")
                    backoff = min(10 * (2 ** min(consecutive_prep_fails - 1, 4)), 300)
                    if shutdown_mgr:
                        try:
                            await asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=backoff)
                            break
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(backoff)
                    continue
                consecutive_prep_fails = 0

            # Phase 2: Run one generation (preserves state on interrupt)
            cost = await _run_one_cycle(
                ui=ui,
                log_file=log_file,
                one_gen=False,
                dry_run=False,
                max_turns=None,
                gen_ctx=gen_ctx,
                shutdown_mgr=shutdown_mgr,
            )

            # Phase 3: Cleanup (idempotent) — after any successful generation
            if cost >= 0:
                from generation_scheduler import post_generation_cleanup
                await post_generation_cleanup(shutdown_mgr, ui, gen_ctx)
                if ui:
                    ui.log_history(f"Orchestrator gen {gen_count} complete. Cost: ${cost:.4f}", "info")
                log_system_event("orchestrator.cycle_done", "info", f"Cycle {gen_count} done (cost=${cost:.4f})",
                                 {"gen_count": gen_count, "cost": round(cost, 4)})

            # Auth error fast-fail
            if cost < 0:
                if ui:
                    ui.log_history("Orchestrator: API auth error (401/403). Backing off 300s.", "error")
                if shutdown_mgr:
                    try:
                        await asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=300)
                        break
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(300)
                _clear_orchestrator_session()
                continue

            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                break

            await asyncio.sleep(5)

    except asyncio.CancelledError:
        if ui:
            ui.set_status("Stopped", is_working=False)
            ui.log_history("Orchestrator stopped.", "warn")
        log_system_event("orchestrator.stopped", "warn", "Orchestrator stopped")
        try:
            from server.state import app_state
            app_state.set_running(False)
        except Exception:
            pass
    except Exception as e:
        if ui:
            ui.log_history(f"Orchestrator crashed: {e}", "error")
        log_system_event("orchestrator.crashed", "error", f"Orchestrator crashed: {e}",
                         {"error": str(e)[:200]})
        _clear_orchestrator_session()
        # Preserve checkpoint for crash recovery regardless of error type.
        # The checkpoint stage-tracking allows startup recovery to assess state.
        try:
            from server.state import app_state
            app_state.set_running(False)
        except Exception:
            pass
    finally:
        if _daemon_stop is not None:
            _daemon_stop.set()
        # Don't stop daemon — it runs independently and survives orchestrator restarts
        # Daemon is only stopped on full process exit (app.py lifespan) or explicit stop


async def _prepare_or_fail(shutdown_mgr, ui, min_games=None):
    """Run prepare_generation with error handling. Returns ctx or None."""
    from generation_scheduler import prepare_generation
    try:
        return await prepare_generation(shutdown_mgr, ui, min_games=min_games)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if ui:
            ui.log_history(f"prepare_generation failed: {e}", "error")
        else:
            log.error("prepare_generation failed: %s", e)
        return None


async def run_orchestrator_cli(args, shutdown_mgr=None):
    """Run Orchestrator in standalone CLI mode."""
    from logging_config import configure_logging
    configure_logging()
    os.makedirs(LOGS_DIR, exist_ok=True)

    log_file = LOGS_DIR / f"orchestrator_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    mode = 'dry-run' if args.dry_run else 'one-gen' if args.one_gen else 'continuous'
    log.info("Starting. Mode: %s", mode)
    log.info("Log: %s", log_file)

    # In CLI mode, inject None (uses ToolUI fallback)
    inject_ui(None)
    set_system_log_ui(None)

    try:
        if args.one_gen or args.dry_run:
            if args.dry_run:
                cost = await _run_one_cycle(
                    ui=None,
                    log_file=log_file,
                    one_gen=args.one_gen,
                    dry_run=args.dry_run,
                    max_turns=args.max_turns,
                )
            else:
                # one-gen mode: use three phases
                from generation_scheduler import prepare_generation, post_generation_cleanup
                gen_ctx = await prepare_generation(shutdown_mgr, None)
                if gen_ctx is None:
                    if shutdown_mgr and shutdown_mgr.is_shutting_down:
                        log.warning("Cancelled during preparation.")
                    else:
                        log.warning("Preparation returned no context.")
                    return
                cost = await _run_one_cycle(
                    ui=None, log_file=log_file,
                    one_gen=True, dry_run=False,
                    max_turns=args.max_turns,
                    gen_ctx=gen_ctx,
                )
                if cost >= 0:
                    await post_generation_cleanup(shutdown_mgr, None, gen_ctx)
            log.info("Done. Cost: $%.4f", cost)
        else:
            await orchestrator_loop(
                ui=None,
                shutdown_mgr=shutdown_mgr,
                no_daemon=args.no_daemon,
            )
    finally:
        try:
            from evolution_infra import stop_daemon
            stop_daemon()
        except Exception:
            pass


def main():
    import signal
    parser = argparse.ArgumentParser(description="LLM Evolution Orchestrator")
    parser.add_argument("--one-gen", action="store_true", help="Run one generation then stop")
    parser.add_argument("--dry-run", action="store_true", help="Only check status, no changes")
    parser.add_argument("--no-daemon", action="store_true", help="Skip daemon startup")
    parser.add_argument("--max-turns", type=int, default=None, help="Max tool call turns per cycle")
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown_mgr = ShutdownManager(grace_period=15.0)
    shutdown_mgr.install_signal_handlers(loop)

    try:
        loop.run_until_complete(run_orchestrator_cli(args, shutdown_mgr))
    except KeyboardInterrupt:
        log.warning("Forced exit.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
