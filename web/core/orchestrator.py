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
from claude_agent_sdk.types import HookMatcher, SyncHookJSONOutput
from tools import evolution_server, inject_ui
from shutdown_manager import ShutdownManager
from system_log import log_system_event, set_ui as set_system_log_ui


ORCHESTRATOR_PROMPT = (Path(__file__).parent / "prompts" / "orchestrator.md").read_text()
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
ORCHESTRATOR_SESSION_FILE = RESULTS_DIR / "orchestrator_session.json"


def _is_rate_limited(output: str) -> bool:
    return "529" in output or "该模型当前访问量过大" in output or "rate limit" in output.lower()


# ── Orchestrator session persistence (process recovery) ──

def _save_orchestrator_session(session_id: str):
    """Persist session_id so a killed process can resume the exact conversation."""
    tmp = ORCHESTRATOR_SESSION_FILE.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, json.dumps({"session_id": session_id}).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(ORCHESTRATOR_SESSION_FILE))


def _load_orchestrator_session() -> "str | None":
    """Return saved session_id, or None."""
    if not ORCHESTRATOR_SESSION_FILE.exists():
        return None
    try:
        return json.loads(ORCHESTRATOR_SESSION_FILE.read_text())["session_id"]
    except Exception:
        return None


def _clear_orchestrator_session():
    """Delete session file after a naturally completed cycle."""
    ORCHESTRATOR_SESSION_FILE.unlink(missing_ok=True)


# ── Startup recovery — assess interrupted state on process start ──

def _startup_recovery(ui=None) -> dict:
    """Assess interrupted state on startup. Returns recovery action dict.

    Decision matrix:
        checkpoint + session → Case C: resume LLM conversation + pipeline
        checkpoint + no session → Case B: new LLM session, resume from checkpoint stage
        no checkpoint + session → Case D: stale session, clear and start fresh
        no checkpoint + no session → Case A: fresh start
    """
    from evolution_core import read_pipeline_checkpoint, clear_pipeline_checkpoint
    checkpoint = read_pipeline_checkpoint()
    session_id = _load_orchestrator_session()

    if not checkpoint:
        if session_id:
            if ui:
                ui.log_history("[Recovery] Stale session file (no pipeline checkpoint). Clearing.", "warn")
            else:
                print("[Recovery] Stale session file (no pipeline checkpoint). Clearing.")
            _clear_orchestrator_session()
        return {"action": "fresh_start"}

    stage = checkpoint.get("stage", "unknown")
    next_v = checkpoint.get("next_v")

    # archived or prepared with no master_plan = no real work to recover
    if stage == "archived" or (stage == "prepared" and not checkpoint.get("master_plan")):
        if ui:
            ui.log_history(f"[Recovery] Pipeline at '{stage}' for v{next_v}. Clearing stale checkpoint.", "warn")
        else:
            print(f"[Recovery] Pipeline at '{stage}' for v{next_v}. Clearing stale checkpoint.")
        clear_pipeline_checkpoint()
        _clear_orchestrator_session()
        return {"action": "fresh_start"}

    # Significant work was done — attempt recovery
    recovery = {
        "action": "resume",
        "checkpoint": checkpoint,
        "session_id": session_id,
        "stage": stage,
        "next_v": next_v,
        "source_v": checkpoint.get("source_v"),
    }
    if session_id:
        msg = f"[Recovery] Resuming v{next_v} at '{stage}' with session {session_id[:8]}..."
    else:
        msg = f"[Recovery] Resuming v{next_v} at '{stage}' (new LLM session)."
    if ui:
        ui.log_history(msg, "warn")
    else:
        print(msg)
    return recovery


# ── PreCompact hook — preserve generation state across LLM context compaction ──

def _make_precompact_hook():
    """Return hooks dict that injects evolution state before Claude compacts context."""
    async def handler(hook_input, tool_use_id, context) -> SyncHookJSONOutput:
        from evolution_core import read_pipeline_checkpoint, find_current_v
        lines = ["=== EVOLUTION STATE — PRESERVE DURING COMPACTION ==="]
        try:
            current_v = find_current_v()
            lines.append(f"Current completed bot: claude_v{current_v}")
            checkpoint = read_pipeline_checkpoint()
            if checkpoint:
                stage_hints = {
                    "prepared":       "execute_workers",
                    "workers_done":   "run_quality_gates",
                    "quality_passed": "run_review",
                    "reviewed":       "run_critic",
                    "critic_checked": "run_precommit_eval",
                    "verified":       "commit_bot",
                    "archived":       "run_archivist",
                }
                stage = checkpoint.get("stage", "unknown")
                next_step = stage_hints.get(stage, "check get_status")
                lines.append(
                    f"ACTIVE GENERATION: v{checkpoint['next_v']} (from v{checkpoint['source_v']}), "
                    f"stage={stage}. Next tool: {next_step}. "
                    "DO NOT restart this generation — continue from this stage."
                )
                if checkpoint.get("master_plan"):
                    tasks = checkpoint["master_plan"].get("tasks", [])
                    if tasks:
                        lines.append("Master plan tasks:")
                        for i, t in enumerate(tasks):
                            lines.append(
                                f"  Worker {t.get('worker_id', i)}: {t.get('role', '?')} "
                                f"— {t.get('objective', '?')[:100]}"
                            )
        except Exception:
            pass
        return SyncHookJSONOutput(reason="\n".join(lines))
    return {"PreCompact": [HookMatcher(matcher="*", hooks=[handler])]}


def _build_context(one_gen=False, dry_run=False, gen_ctx=None):
    """Build context string injected into the orchestrator prompt.

    When gen_ctx (GenerationContext) is provided, injects pre-computed analysis
    data from the code-layer scheduler instead of raw status data.
    """
    from evolution_core import (
        get_active_bots, load_ratings,
        get_bot_dir, git_has_tag, _load_recent_failures, _git,
        find_current_v,
    )
    from glicko2 import Glicko2Player

    # If GenerationContext is provided, build streamlined context
    if gen_ctx is not None:
        lines = [
            f"Current generation: v{gen_ctx.current_v}",
            f"Next generation: v{gen_ctx.next_v}",
            f"Strategy: {gen_ctx.strategy}",
            f"Source bot: claude_v{gen_ctx.source_v}",
            f"Active bots: {len(get_active_bots())}",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if gen_ctx.strategy == "crossover" and gen_ctx.crossover_parents:
            lines.append(f"Crossover parents: claude_v{gen_ctx.crossover_parents[0]} x claude_v{gen_ctx.crossover_parents[1]}")
        if gen_ctx.stagnation_info:
            lines.append(f"\nStagnation analysis:\n{gen_ctx.stagnation_info}")
        if gen_ctx.match_analysis:
            lines.append(f"\nMatch analysis:\n{gen_ctx.match_analysis}")
        if gen_ctx.performance_verification:
            lines.append(f"\nPerformance verification:\n{gen_ctx.performance_verification}")
        if one_gen:
            lines.append("MODE: Run exactly ONE generation, then stop.")
        else:
            lines.append("MODE: Execute this generation using the pipeline tools.")
        # Pipeline checkpoint still relevant for resume
        try:
            from evolution_core import read_pipeline_checkpoint
            checkpoint = read_pipeline_checkpoint()
            if checkpoint:
                stage_hints = {
                    "prepared":       "Workers not yet run → call execute_workers",
                    "workers_done":   "Workers done → call run_quality_gates",
                    "quality_passed": "Quality passed → call run_review",
                    "reviewed":       "Review passed → call run_critic",
                    "critic_checked": "Critic done → call run_precommit_eval",
                    "verified":       "Precommit eval passed → call commit_bot",
                    "archived":       "Committed & archived → done",
                }
                stage = checkpoint.get("stage", "unknown")
                hint = stage_hints.get(stage, "call get_status to assess")
                lines.append(
                    f"\nPIPELINE CHECKPOINT: v{checkpoint['next_v']} (from v{checkpoint['source_v']}) "
                    f"reached stage='{stage}'. Next step: {hint}."
                )
                if checkpoint.get("master_plan"):
                    lines.append("Master plan is saved in session history — do NOT call run_master again.")
                else:
                    lines.append("WARNING: Master plan NOT in checkpoint — call run_master first, then execute_workers.")
        except Exception:
            pass
        return "\n".join(lines)

    active_bots = get_active_bots()
    ratings = load_ratings()
    current_v = find_current_v()

    lines = [
        f"Current generation: v{current_v}",
        f"Next generation will be: v{current_v + 1}",
        f"Active bots: {len(active_bots)}",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    # Current bot rating reliability
    cur_p = ratings.get(f"claude_v{current_v}")
    bot_name = f"claude_v{current_v}"
    if cur_p:
        # Load bot_stats for games-based reliability
        bot_stats_file = RESULTS_DIR / "bot_stats.json"
        games = 0
        wr = 0.0
        if bot_stats_file.exists():
            try:
                import fcntl
                with open(bot_stats_file, "r") as f:
                    fcntl.flock(f, fcntl.LOCK_SH)
                    bs = json.load(f)
                games = bs.get(bot_name, {}).get("games", 0)
                wr = bs.get(bot_name, {}).get("win_rate", 0.0)
            except Exception:
                pass
        reliable = "RELIABLE" if games >= 100 else f"UNRELIABLE ({games}/100 games — wait for more matches)"
        # Compute H2H avg win rate for the current bot
        try:
            from tool_helpers import load_h2h_avg_winrates
            h2h_wrs = load_h2h_avg_winrates()
            h2h_wr = h2h_wrs.get(bot_name, 0.5)
            h2h_str = f"h2h_avg_wr={h2h_wr:.2%}"
        except Exception:
            h2h_str = "h2h_avg_wr=N/A"
        lines.append(f"Current bot {bot_name}: {h2h_str}, r={cur_p.r:.1f}, rd={cur_p.rd:.1f}, wr={wr:.0%} ({games} games) [{reliable}]")

    # Incomplete bot detection — previous cycle may have been interrupted
    next_dir = get_bot_dir(current_v + 1)
    if next_dir.exists() and not (next_dir / ".completed").exists():
        lines.append(
            f"WARNING: claude_v{current_v + 1} directory exists but is NOT completed "
            f"(previous cycle was interrupted). Decide: resume workers or clean up and restart."
        )

    # Recent completed generations (from git tags)
    try:
        tag_output = _git("tag", "-l", "bot-v*", "--sort=-version:refname", check=False)
        recent_tags = [t.strip() for t in tag_output.splitlines() if t.strip()][:5]
        if recent_tags:
            lines.append(f"Recent completed gens: {', '.join(recent_tags)}")
    except Exception:
        pass

    # Recent worker failures
    try:
        recent_failures = _load_recent_failures(3)
        if recent_failures:
            lines.append("Recent worker failures (last 3):")
            for f in recent_failures:
                lines.append(f"  - Gen {f['gen']} Worker {f['worker_id']} ({f['role']}): {f['error'][:120]}")
    except Exception:
        pass

    # Pipeline checkpoint — tell Orchestrator exactly where a killed cycle left off
    try:
        from evolution_core import read_pipeline_checkpoint
        checkpoint = read_pipeline_checkpoint()
        if checkpoint:
            stage_hints = {
                "prepared":       "Workers not yet run → call execute_workers",
                "workers_done":   "Workers done → call run_quality_gates",
                "quality_passed": "Quality passed → call run_review",
                "reviewed":       "Review passed → call run_critic",
                "critic_checked": "Critic done → call run_precommit_eval",
                "verified":       "Precommit eval passed → call commit_bot",
                "archived":       "Committed & archived → start next generation",
            }
            stage = checkpoint.get("stage", "unknown")
            hint = stage_hints.get(stage, "call get_status to assess")
            if checkpoint.get("master_plan"):
                plan_note = "Master plan is saved in session history — do NOT call run_master again."
            else:
                plan_note = "WARNING: Master plan NOT in checkpoint — call run_master first, then execute_workers."
            lines.append(
                f"PIPELINE CHECKPOINT: v{checkpoint['next_v']} (from v{checkpoint['source_v']}) "
                f"reached stage='{stage}'. Next step: {hint}. {plan_note}"
            )
    except Exception:
        pass

    # Environment anomaly detection
    anomalies = []
    if next_dir.exists() and not (next_dir / ".completed").exists():
        anomalies.append("incomplete bot directory")
    try:
        from evolution_core import _load_recent_failures
        if _load_recent_failures(1):
            anomalies.append("recent worker failures")
    except Exception:
        pass
    if anomalies:
        lines.append(
            f"ENVIRONMENT ANOMALIES DETECTED: {', '.join(anomalies)}."
        )

    if one_gen:
        lines.append("MODE: Run exactly ONE generation, then stop.")
    elif dry_run:
        lines.append("MODE: DRY RUN — only check status, do NOT modify anything.")
    else:
        lines.append("MODE: Continuous evolution. After completing one generation, immediately start the next.")

    return "\n".join(lines)


async def _run_one_cycle(ui, log_file, one_gen=False, dry_run=False, max_turns=None, gen_ctx=None):
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
                                    print(block.text, end="", flush=True)
                                lf.write(block.text)
                            elif isinstance(block, ToolUseBlock):
                                if ui:
                                    ui.log_history(f"[Orchestrator] Calling tool: {block.name}", "info")
                                    ui.log_io(f"\n[tool: {block.name}]", "tool", "Orchestrator")
                                    ui.emit_tool_call(block.name, block.input, "Orchestrator")
                                else:
                                    print(f"\n[tool: {block.name}]", end=" ", flush=True)
                                lf.write(f"\n[tool: {block.name}]\n")
                            elif isinstance(block, ThinkingBlock):
                                if ui:
                                    ui.log_io(block.thinking or "[thinking...]", "thinking", "Orchestrator")
                                else:
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
                    await asyncio.sleep(backoff)
                    if query_gen is not None:
                        try:
                            await query_gen.aclose()
                        except Exception:
                            pass
                    full_output, total_cost, cycle_completed, query_gen, auth_error = await _stream_response(retry_opts)
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
                print("\n[Orchestrator] Interrupted by user.")
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
                print("\n[Orchestrator] Cancelled — session preserved for resume.")
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
                print(f"\n[Orchestrator] Error: {e}")
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

    if ui:
        ui.log_history("🔥 Orchestrator starting...", "success")
        ui.set_header("🔥 LLM Orchestrator Evolution 🔥")

    log_system_event("orchestrator.started", "success", "Orchestrator started",
                     {"daemon_enabled": not no_daemon})

    # Start daemon
    _daemon_stop = None
    if not no_daemon:
        from evolution_core import start_daemon, daemon_monitor_thread
        import threading
        start_daemon(workers=daemon_workers, pairs=daemon_pairs)
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
                gen_ctx = GenerationContext(
                    current_v=ckpt.get("source_v", find_current_v()),
                    next_v=ckpt["next_v"],
                    strategy="master",  # recovery always uses master
                    source_v=ckpt["source_v"],
                    gen_count=gen_count,
                )
                recovery = None  # consume recovery, only used once
            else:
                # Phase 1: Prepare (disposable on interrupt)
                gen_ctx = await _prepare_or_fail(shutdown_mgr, ui)
                if gen_ctx is None:
                    if shutdown_mgr and shutdown_mgr.is_shutting_down:
                        break
                    await asyncio.sleep(10)
                    continue

            # Phase 2: Run one generation (preserves state on interrupt)
            cost = await _run_one_cycle(
                ui=ui,
                log_file=log_file,
                one_gen=False,
                dry_run=False,
                max_turns=None,
                gen_ctx=gen_ctx,
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
        try:
            from evolution_core import clear_pipeline_checkpoint
            clear_pipeline_checkpoint()
        except Exception:
            pass
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


async def _prepare_or_fail(shutdown_mgr, ui):
    """Run prepare_generation with error handling. Returns ctx or None."""
    from generation_scheduler import prepare_generation
    try:
        return await prepare_generation(shutdown_mgr, ui)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if ui:
            ui.log_history(f"prepare_generation failed: {e}", "error")
        else:
            print(f"[Orchestrator] prepare_generation failed: {e}")
        return None


async def run_orchestrator_cli(args, shutdown_mgr=None):
    """Run Orchestrator in standalone CLI mode."""
    os.makedirs(LOGS_DIR, exist_ok=True)

    log_file = LOGS_DIR / f"orchestrator_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    print(f"[Orchestrator] Starting. Mode: {'dry-run' if args.dry_run else 'one-gen' if args.one_gen else 'continuous'}")
    print(f"[Orchestrator] Log: {log_file}")

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
                        print("[Orchestrator] Cancelled during preparation.")
                    else:
                        print("[Orchestrator] Preparation returned no context.")
                    return
                cost = await _run_one_cycle(
                    ui=None, log_file=log_file,
                    one_gen=True, dry_run=False,
                    max_turns=args.max_turns,
                    gen_ctx=gen_ctx,
                )
                if cost >= 0:
                    await post_generation_cleanup(shutdown_mgr, None, gen_ctx)
            print(f"\n[Orchestrator] Done. Cost: ${cost:.4f}")
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
        print("\n[Orchestrator] Forced exit.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
