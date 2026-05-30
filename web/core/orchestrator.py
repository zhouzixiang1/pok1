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
    ThinkingBlock,
)
from claude_agent_sdk.types import HookMatcher, SyncHookJSONOutput
from tools import evolution_server, inject_ui


ORCHESTRATOR_PROMPT = (Path(__file__).parent / "prompts" / "orchestrator.md").read_text()
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
ORCHESTRATOR_SESSION_FILE = RESULTS_DIR / "orchestrator_session.json"


# ── Orchestrator session persistence (process recovery) ──

def _save_orchestrator_session(session_id: str):
    """Persist session_id so a killed process can resume the exact conversation."""
    ORCHESTRATOR_SESSION_FILE.write_text(json.dumps({"session_id": session_id}))


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


# ── PreCompact hook — preserve generation state across LLM context compaction ──

def _make_precompact_hook():
    """Return hooks dict that injects evolution state before Claude compacts context."""
    async def handler(hook_input, tool_use_id, context) -> SyncHookJSONOutput:
        from evolution_core import read_pipeline_checkpoint
        lines = ["=== EVOLUTION STATE — PRESERVE DURING COMPACTION ==="]
        try:
            current_v = _find_current_v()  # defined in this module
            lines.append(f"Current completed bot: claude_v{current_v}")
            checkpoint = read_pipeline_checkpoint()
            if checkpoint:
                stage_hints = {
                    "prepared":       "execute_workers",
                    "workers_done":   "run_quality_gates",
                    "quality_passed": "run_review",
                    "reviewed":       "run_critic",
                    "critic_checked": "commit_bot",
                }
                stage = checkpoint.get("stage", "unknown")
                next_step = stage_hints.get(stage, "check get_status")
                lines.append(
                    f"ACTIVE GENERATION: v{checkpoint['next_v']} (from v{checkpoint['source_v']}), "
                    f"stage={stage}. Next tool: {next_step}. "
                    "DO NOT restart this generation — continue from this stage."
                )
        except Exception:
            pass
        return SyncHookJSONOutput(reason="\n".join(lines))
    return {"PreCompact": [HookMatcher(matcher="*", hooks=[handler])]}


def _find_current_v():
    """Find the latest completed bot version."""
    from evolution_core import get_bot_dir, git_has_tag
    current_v = 1
    while True:
        d = get_bot_dir(current_v)
        if d.exists() and (d / ".completed").exists():
            if current_v <= 6 or git_has_tag(current_v):
                current_v += 1
            else:
                break
        else:
            break
    return current_v - 1


def _build_context(one_gen=False, dry_run=False):
    """Build context string injected into the orchestrator prompt."""
    from evolution_core import (
        get_active_bots, load_ratings, git_ensure_clean,
        get_bot_dir, git_has_tag, _load_recent_failures, _git,
    )
    from glicko2 import Glicko2Player

    git_ensure_clean()
    active_bots = get_active_bots()
    ratings = load_ratings()
    current_v = _find_current_v()

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
                with open(bot_stats_file, "r") as f:
                    bs = json.load(f)
                games = bs.get(bot_name, {}).get("games", 0)
                wr = bs.get(bot_name, {}).get("win_rate", 0.0)
            except Exception:
                pass
        reliable = "RELIABLE" if games >= 100 else f"UNRELIABLE ({games}/100 games — wait for more matches)"
        lines.append(f"Current bot {bot_name}: r={cur_p.r:.1f}, rd={cur_p.rd:.1f}, wr={wr:.0%} ({games} games) [{reliable}]")

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
                "critic_checked": "Critic done → call commit_bot",
            }
            stage = checkpoint.get("stage", "unknown")
            hint = stage_hints.get(stage, "call get_status to assess")
            # master_plan is only persisted starting from workers_done; prepared checkpoint
            # does NOT carry a plan (prepare_next_gen writes it without master_plan).
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

    if one_gen:
        lines.append("MODE: Run exactly ONE generation, then stop.")
    elif dry_run:
        lines.append("MODE: DRY RUN — only check status, do NOT modify anything.")
    else:
        lines.append("MODE: Continuous evolution. After completing one generation, immediately start the next.")

    return "\n".join(lines)


async def _run_one_cycle(ui, log_file, one_gen=False, dry_run=False, max_turns=None):
    """Run one Orchestrator cycle (one LLM agent session). Returns total cost."""
    context = _build_context(one_gen=one_gen, dry_run=dry_run)
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
        setting_sources=[],
        hooks=_make_precompact_hook(),
        max_turns=max_turns,
        **resume_kwargs,
    )

    total_cost = 0.0
    cycle_completed = False

    with open(log_file, "a") as lf:
        lf.write(f"\n{'='*60}\n[ORCHESTRATOR CYCLE] {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n")
        lf.write(f"[PROMPT]\n{prompt}\n\n[OUTPUT]\n")

        try:
            query_gen = claude_query(prompt=prompt, options=options)
            async for message in query_gen:
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text = block.text
                            if ui:
                                ui.log_io(text, "claude")
                            else:
                                print(text, end="", flush=True)
                            lf.write(text)
                        elif isinstance(block, ToolUseBlock):
                            tool_name = block.name
                            if ui:
                                ui.log_history(f"[Orchestrator] Calling tool: {tool_name}", "info")
                                ui.log_io(f"\n[tool: {tool_name}]", "tool")
                                ui.emit_tool_call(tool_name, block.input)
                            else:
                                print(f"\n[tool: {tool_name}]", end=" ", flush=True)
                            lf.write(f"\n[tool: {tool_name}]\n")
                        elif isinstance(block, ThinkingBlock):
                            if ui:
                                ui.log_io("[thinking...]", "thinking")
                            else:
                                print("[thinking...]", end=" ", flush=True)

                elif isinstance(message, ResultMessage):
                    # Save session_id so a kill -9 can resume this exact conversation
                    if message.session_id:
                        _save_orchestrator_session(message.session_id)
                    if message.total_cost_usd:
                        total_cost += message.total_cost_usd
                    if not message.is_error:
                        cycle_completed = True
                    if ui:
                        ui.update_cost("Orchestrator", total_cost, getattr(message, 'usage', None))
                    lf.write(f"\n[CYCLE DONE] cost=${total_cost:.4f}\n")

        except KeyboardInterrupt:
            if ui:
                ui.log_history("[Orchestrator] Interrupted by user.", "warn")
            else:
                print("\n[Orchestrator] Interrupted by user.")
            lf.write("\n[INTERRUPTED]\n")
        except Exception as e:
            if ui:
                ui.log_history(f"[Orchestrator] Error: {e}", "error")
            else:
                print(f"\n[Orchestrator] Error: {e}")
            lf.write(f"\n[ERROR] {e}\n")

    # Only clear session file on natural (non-error) cycle completion.
    # If killed, the session file remains so next startup can resume.
    if cycle_completed:
        _clear_orchestrator_session()

    return total_cost


async def orchestrator_loop(ui, no_daemon=False):
    """Orchestrator entry point compatible with BaseUI interface.

    Designed to be called from dashboard/backend/app.py:
        _evolution_task = asyncio.create_task(orchestrator_loop(web_ui))

    Args:
        ui: BaseUI instance (WebUI for Dashboard, TextUI for CLI). Can be None for silent mode.
        no_daemon: If True, skip daemon startup.
    """
    from tools import inject_ui
    inject_ui(ui)

    os.makedirs(LOGS_DIR, exist_ok=True)

    if ui:
        ui.log_history("🔥 Orchestrator starting...", "success")
        ui.set_header("🔥 LLM Orchestrator Evolution 🔥")

    # Start daemon
    if not no_daemon:
        from evolution_core import start_daemon, daemon_monitor_thread, stop_daemon
        import threading
        start_daemon()
        _daemon_stop = threading.Event()
        monitor = threading.Thread(
            target=daemon_monitor_thread, args=(ui, _daemon_stop), daemon=True
        )
        monitor.start()
        if ui:
            ui.log_history("Daemon started.", "info")

    log_file = LOGS_DIR / f"orchestrator_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    gen_count = 0

    try:
        while True:
            gen_count += 1
            max_turns = None  # No turn limit — prompt constrains behavior

            cost = await _run_one_cycle(
                ui=ui,
                log_file=log_file,
                one_gen=False,
                dry_run=False,
                max_turns=max_turns,
            )

            if ui:
                ui.log_history(f"Orchestrator cycle {gen_count} complete. Cost: ${cost:.4f}", "info")

            # Brief pause between cycles
            await asyncio.sleep(5)

    except asyncio.CancelledError:
        if ui:
            ui.log_history("Orchestrator stopped.", "warn")
    except Exception as e:
        if ui:
            ui.log_history(f"Orchestrator crashed: {e}", "error")


async def run_orchestrator_cli(args):
    """Run Orchestrator in standalone CLI mode."""
    os.makedirs(LOGS_DIR, exist_ok=True)

    log_file = LOGS_DIR / f"orchestrator_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    print(f"[Orchestrator] Starting. Mode: {'dry-run' if args.dry_run else 'one-gen' if args.one_gen else 'continuous'}")
    print(f"[Orchestrator] Log: {log_file}")

    # In CLI mode, inject None (uses ToolUI fallback)
    inject_ui(None)

    if args.one_gen or args.dry_run:
        cost = await _run_one_cycle(
            ui=None,
            log_file=log_file,
            one_gen=args.one_gen,
            dry_run=args.dry_run,
            max_turns=args.max_turns,
        )
        print(f"\n[Orchestrator] Done. Cost: ${cost:.4f}")
    else:
        gen_count = 0
        while True:
            gen_count += 1
            cost = await _run_one_cycle(
                ui=None, log_file=log_file,
                one_gen=False, dry_run=False,
                max_turns=args.max_turns,
            )
            print(f"\n[Orchestrator] Cycle {gen_count} done. Cost: ${cost:.4f}")
            await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="LLM Evolution Orchestrator")
    parser.add_argument("--one-gen", action="store_true", help="Run one generation then stop")
    parser.add_argument("--dry-run", action="store_true", help="Only check status, no changes")
    parser.add_argument("--max-turns", type=int, default=None, help="Max tool call turns per cycle")
    args = parser.parse_args()

    asyncio.run(run_orchestrator_cli(args))


if __name__ == "__main__":
    main()
