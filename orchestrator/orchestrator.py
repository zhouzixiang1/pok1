"""Evolution Orchestrator — LLM-driven bot evolution pipeline.

Usage:
    python orchestrator/orchestrator.py              # Run continuous evolution
    python orchestrator/orchestrator.py --one-gen    # Run one generation then stop
    python orchestrator/orchestrator.py --dry-run    # Only check status, no changes
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
from tools import evolution_server


ORCHESTRATOR_PROMPT = (Path(__file__).parent / "prompts" / "orchestrator.md").read_text()
LOGS_DIR = Path(__file__).parent / "logs"


def _build_context(args):
    """Build context string injected into the orchestrator prompt."""
    from evolution_workspace.evolution_core import get_active_bots, load_ratings, git_ensure_clean

    git_ensure_clean()
    active_bots = get_active_bots()
    ratings = load_ratings()

    # Find current_v
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
    current_v -= 1

    lines = [
        f"Current generation: v{current_v}",
        f"Next generation will be: v{current_v + 1}",
        f"Active bots: {len(active_bots)}",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    if args.one_gen:
        lines.append("MODE: Run exactly ONE generation, then stop.")
    elif args.dry_run:
        lines.append("MODE: DRY RUN — only check status, do NOT modify anything.")
    else:
        lines.append("MODE: Continuous evolution. After completing one generation, immediately start the next.")

    return "\n".join(lines)


async def run_orchestrator(args):
    """Run the Orchestrator LLM agent."""
    os.makedirs(LOGS_DIR, exist_ok=True)

    context = _build_context(args)
    prompt = ORCHESTRATOR_PROMPT.replace("{context}", context)

    log_file = LOGS_DIR / f"orchestrator_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    print(f"[Orchestrator] Starting. Mode: {'dry-run' if args.dry_run else 'one-gen' if args.one_gen else 'continuous'}")
    print(f"[Orchestrator] Log: {log_file}")

    options = ClaudeAgentOptions(
        model="sonnet",
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
        mcp_servers={"evolution": evolution_server},
    )

    if args.dry_run:
        # Dry run: just call get_status
        prompt += "\n\nIMPORTANT: This is a DRY RUN. Only call get_status() and report the current state. Do NOT modify anything."

    if args.max_turns:
        options.max_turns = args.max_turns

    total_cost = 0.0

    with open(log_file, "w") as lf:
        lf.write(f"[ORCHESTRATOR PROMPT]\n{prompt}\n\n[ORCHESTRATOR OUTPUT]\n")

        try:
            query_gen = claude_query(prompt=prompt, options=options)
            async for message in query_gen:
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(block.text, end="", flush=True)
                            lf.write(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_name = block.name
                            print(f"\n[tool: {tool_name}]", end=" ", flush=True)
                            lf.write(f"\n[tool: {tool_name}]\n")
                        elif isinstance(block, ThinkingBlock):
                            print("[thinking...]", end=" ", flush=True)

                elif isinstance(message, ResultMessage):
                    if message.total_cost_usd:
                        total_cost += message.total_cost_usd
                    lf.write(f"\n\n[Result] cost=${total_cost:.4f}\n")
                    print(f"\n\n[Orchestrator completed] Total cost: ${total_cost:.4f}")
                    lf.write(f"\n[ORCHESTRATOR DONE] cost=${total_cost:.4f}\n")

        except KeyboardInterrupt:
            print("\n[Orchestrator] Interrupted by user.")
            lf.write("\n[INTERRUPTED]\n")
        except Exception as e:
            print(f"\n[Orchestrator] Error: {e}")
            lf.write(f"\n[ERROR] {e}\n")


def main():
    parser = argparse.ArgumentParser(description="LLM Evolution Orchestrator")
    parser.add_argument("--one-gen", action="store_true", help="Run one generation then stop")
    parser.add_argument("--dry-run", action="store_true", help="Only check status, no changes")
    parser.add_argument("--max-turns", type=int, default=None, help="Max tool call turns per generation")
    args = parser.parse_args()

    asyncio.run(run_orchestrator(args))


if __name__ == "__main__":
    main()
