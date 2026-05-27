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
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "evolution_workspace"))
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
from tools import evolution_server, inject_ui


ORCHESTRATOR_PROMPT = (Path(__file__).parent / "prompts" / "orchestrator.md").read_text()
LOGS_DIR = Path(__file__).parent / "logs"


def _find_current_v():
    """Find the latest completed bot version."""
    from evolution_workspace.evolution_core import get_bot_dir, git_has_tag
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
    from evolution_workspace.evolution_core import get_active_bots, load_ratings, git_ensure_clean

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

    options = ClaudeAgentOptions(
        model="sonnet",
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
        mcp_servers={"evolution": evolution_server},
    )

    if max_turns:
        options.max_turns = max_turns

    total_cost = 0.0

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
                            else:
                                print(f"\n[tool: {tool_name}]", end=" ", flush=True)
                            lf.write(f"\n[tool: {tool_name}]\n")
                        elif isinstance(block, ThinkingBlock):
                            if ui:
                                ui.log_io("[thinking...]", "thinking")
                            else:
                                print("[thinking...]", end=" ", flush=True)

                elif isinstance(message, ResultMessage):
                    if message.total_cost_usd:
                        total_cost += message.total_cost_usd
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
        from evolution_workspace.evolution_core import start_daemon, daemon_monitor_thread, stop_daemon
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
            max_turns = 80  # Limit per-cycle turns to prevent runaway

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
            max_turns=args.max_turns or 50,
        )
        print(f"\n[Orchestrator] Done. Cost: ${cost:.4f}")
    else:
        gen_count = 0
        while True:
            gen_count += 1
            cost = await _run_one_cycle(
                ui=None, log_file=log_file,
                one_gen=False, dry_run=False,
                max_turns=args.max_turns or 80,
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
