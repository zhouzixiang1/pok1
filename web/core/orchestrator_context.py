"""Orchestrator context building and PreCompact hook.

_build_context assembles the status string injected into the orchestrator prompt.
_make_precompact_hook preserves evolution state across LLM context compaction.
"""

import json
import time
from pathlib import Path

from claude_agent_sdk.types import HookMatcher, SyncHookJSONOutput

RESULTS_DIR = Path(__file__).resolve().parent / "results"


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
                    "prepared":          "Call run_direction_audit first",
                    "direction_audited": "Direction audited → call run_master",
                    "master_planned":    "Master done → call execute_workers",
                    "workers_done":      "Workers done → call run_quality_gates",
                    "quality_passed":    "Quality passed → call run_review",
                    "reviewed":          "Review passed → call run_critic",
                    "critic_checked":    "Critic done → call run_precommit_eval",
                    "verified":          "Precommit eval passed → call commit_bot",
                    "archived":          "Committed & archived → done",
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
                from evolution_infra import locked_file
                with locked_file(bot_stats_file, "r") as f:
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
                "prepared":          "Call run_direction_audit first",
                "direction_audited": "Direction audited → call run_master",
                "master_planned":    "Master done → call execute_workers",
                "workers_done":      "Workers done → call run_quality_gates",
                "quality_passed":    "Quality passed → call run_review",
                "reviewed":          "Review passed → call run_critic",
                "critic_checked":    "Critic done → call run_precommit_eval",
                "verified":          "Precommit eval passed → call commit_bot",
                "archived":          "Committed & archived → start next generation",
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
                    "prepared":          "run_direction_audit",
                    "direction_audited": "run_master",
                    "master_planned":    "execute_workers",
                    "workers_done":      "run_quality_gates",
                    "quality_passed":    "run_review",
                    "reviewed":          "run_critic",
                    "critic_checked":    "run_precommit_eval",
                    "verified":          "commit_bot",
                    "archived":          "run_archivist",
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
