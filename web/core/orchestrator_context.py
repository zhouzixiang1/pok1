"""Orchestrator context building and PreCompact hook.

_build_context assembles the status string injected into the orchestrator prompt.
_make_precompact_hook preserves evolution state across LLM context compaction.
"""

import json
import time
from pathlib import Path

from claude_agent_sdk.types import HookMatcher, SyncHookJSONOutput

from evolution_infra import locked_file

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Module-level cycle start time — set by orchestrator._run_one_cycle at cycle start,
# read by _build_context and PreCompact hook for time-budget awareness.
_cycle_start_time = None
CYCLE_TIMEOUT = 3600  # Must match orchestrator.py


def set_cycle_start_time(t):
    """Called from orchestrator._run_one_cycle to mark the cycle start."""
    global _cycle_start_time
    _cycle_start_time = t


def _get_time_budget_info():
    """Return a string describing cycle time budget, or empty string if not in a cycle."""
    if _cycle_start_time is None:
        return ""
    elapsed = int(time.time() - _cycle_start_time)
    remaining = max(0, CYCLE_TIMEOUT - elapsed)
    pct = int(elapsed / CYCLE_TIMEOUT * 100)
    return (
        f"CYCLE TIME BUDGET: {elapsed}s elapsed / {CYCLE_TIMEOUT}s total "
        f"({remaining}s remaining, {pct}% used). "
        f"{'⚠️ Less than 15 minutes remaining — do NOT start new retry loops.' if remaining < 900 else ''}"
    )


def _inject_master_plan_hint(checkpoint, lines):
    """Inject master plan task summaries into context.

    Critical: the Orchestrator LLM has no Bash/Read tools — it cannot read
    pipeline_state.json on its own.  If we only say "Master plan is saved in
    session history", a fresh (non-resumed) session has NO history and the
    model spirals calling ToolSearch trying to find Read/Bash.  Instead we
    inline a compact summary of each task so execute_workers(tasks=...)
    can be called correctly.
    """
    plan = checkpoint.get("master_plan")
    if not plan:
        lines.append("WARNING: Master plan NOT in checkpoint — call run_master first, then execute_workers.")
        return
    tasks = plan.get("tasks", [])
    if tasks:
        lines.append(
            "Master plan is saved — do NOT call run_master again. "
            "Pass these tasks to execute_workers:"
        )
        for t in tasks:
            wid = t.get("worker_id", "?")
            role = t.get("role", "?")
            targets = ", ".join(t.get("target_files", []))
            prompt_preview = t.get("worker_prompt", "")[:200]
            lines.append(
                f"  Worker {wid} ({role}): targets=[{targets}], "
                f"prompt=\"{prompt_preview}...\""
            )
    else:
        lines.append("Master plan is saved — do NOT call run_master again.")


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

        # Tool reference — prevents ToolSearch when session is fresh/resumed
        lines.append("\nAVAILABLE TOOLS (call by exact name):")
        lines.append("  prepare_next_gen(source_v, next_v) — copy source bot dir")
        lines.append("  run_direction_audit(source_v, next_v) — detect repetitive evolution directions")
        lines.append("  run_master(source_v, next_v, stagnation_info, match_analysis, performance_verification) — plan worker tasks")
        lines.append("  execute_workers(tasks, next_v, source_v, reviewer_feedback) — modify bot code sequentially")
        lines.append("  run_quality_gates(version, source_v) — compile + smoke test + decision tests + file size")
        lines.append("  run_review(version, source_v, plan) — code quality review (boundaries, size, correctness)")
        lines.append("  run_critic(version, source_v, plan, reviewer_feedback, force_advance) — strategic assessment (score >= 6)")
        lines.append("  run_precommit_eval(version, source_v, n_games) — mirror battle regression check")
        lines.append("  commit_bot(version, source_v, strategy, review_approved=true) — git commit + tag (requires all gates passed)")
        lines.append("  run_archivist(version, source_v) — archive + cleanup after commit")
        lines.append("  run_crossover(parent_a, parent_b, target_v) — merge two parent bots (alternative to master+workers)")

        if gen_ctx.stagnation_info:
            lines.append(f"\nStagnation analysis:\n{gen_ctx.stagnation_info}")
        if gen_ctx.match_analysis:
            lines.append(f"\nMatch analysis:\n{gen_ctx.match_analysis}")
        if gen_ctx.replay_spotlight:
            lines.append(f"\nReplay spotlight:\n{gen_ctx.replay_spotlight}")
        if gen_ctx.performance_verification:
            lines.append(f"\nPerformance verification:\n{gen_ctx.performance_verification}")

        # Eval round summary (deterministic cross-generation performance data)
        try:
            from eval_rounds import EvalRoundManager
            _erm = EvalRoundManager()
            bot_name = f"claude_v{gen_ctx.source_v}"
            eval_summary = _erm.get_last_round_summary(bot_name)
            if eval_summary:
                lines.append(f"\n{eval_summary}")
        except Exception:
            pass
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
                    "timed_out":         "Previous cycle timed out and was discarded. Call prepare_next_gen to start a fresh generation. Do NOT attempt to resume timed-out work.",
                }
                stage = checkpoint.get("stage", "unknown")
                hint = stage_hints.get(stage, "call get_status to assess")
                lines.append(
                    f"\nPIPELINE CHECKPOINT: v{checkpoint['next_v']} (from v{checkpoint['source_v']}) "
                    f"reached stage='{stage}'. Next step: {hint}."
                )
                gen_attempt = checkpoint.get("generation_attempt", 0)
                if gen_attempt > 0:
                    lines.append(
                        f"INTRA-GEN RETRIES: {gen_attempt} previous critic rejection(s). "
                        f"{'MAX RETRIES REACHED — do NOT retry workers again. Abandon this generation.' if gen_attempt >= 2 else 'You may retry workers at most 1 more time.'}"
                    )
                _inject_master_plan_hint(checkpoint, lines)
                last_update = checkpoint.get("last_update_ts")
                if last_update:
                    age = int(time.time() - last_update)
                    lines.append(f"Last checkpoint activity: {age}s ago")
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

    # Tool reference — prevents ToolSearch in non-gen_ctx path
    lines.append("\nAVAILABLE TOOLS (call by exact name):")
    lines.append("  prepare_next_gen | run_direction_audit | run_master | execute_workers | run_quality_gates | run_review | run_critic | run_precommit_eval | commit_bot | run_archivist | run_crossover")

    # Current bot action stats (fold/call/raise frequencies by street)
    bot_action_stats_file = RESULTS_DIR / "bot_action_stats.json"
    if bot_action_stats_file.exists():
        try:
            with locked_file(bot_action_stats_file, "r") as f:
                action_stats = json.load(f)
            bot_stats = action_stats.get(bot_name)
            if bot_stats:
                lines.append(f"\nCurrent bot action stats ({bot_name}):")
                for street in ("preflop", "flop", "turn", "river"):
                    st = bot_stats.get(street)
                    if st:
                        total = st.get("total", 0)
                        if total > 0:
                            fold_pct = st.get("fold", 0) / total * 100
                            call_pct = st.get("call", 0) / total * 100
                            raise_pct = st.get("raise", 0) / total * 100
                            lines.append(
                                f"  {street}: total={total}, fold={fold_pct:.1f}%, call={call_pct:.1f}%, raise={raise_pct:.1f}%"
                            )
        except Exception:
            pass

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
            lines.append(
                f"PIPELINE CHECKPOINT: v{checkpoint['next_v']} (from v{checkpoint['source_v']}) "
                f"reached stage='{stage}'. Next step: {hint}."
            )
            gen_attempt = checkpoint.get("generation_attempt", 0)
            if gen_attempt > 0:
                lines.append(
                    f"INTRA-GEN RETRIES: {gen_attempt} previous critic rejection(s). "
                    f"{'MAX RETRIES REACHED — do NOT retry workers again. Abandon this generation.' if gen_attempt >= 2 else 'You may retry workers at most 1 more time.'}"
                )
            _inject_master_plan_hint(checkpoint, lines)
            last_update = checkpoint.get("last_update_ts")
            if last_update:
                age = int(time.time() - last_update)
                lines.append(f"Last checkpoint activity: {age}s ago")
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

    # Cycle time budget — helps Orchestrator avoid starting retry loops near timeout
    time_budget = _get_time_budget_info()
    if time_budget:
        lines.append(time_budget)

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
                        lines.append("Master plan tasks (pass these to execute_workers):")
                        for t in tasks:
                            wid = t.get("worker_id", "?")
                            role = t.get("role", "?")
                            targets = ", ".join(t.get("target_files", []))
                            prompt_preview = t.get("worker_prompt", "")[:200]
                            lines.append(
                                f"  Worker {wid} ({role}): targets=[{targets}], "
                                f"prompt=\"{prompt_preview}...\""
                            )
        except Exception:
            pass
        # Cycle time budget for compaction survival
        time_budget = _get_time_budget_info()
        if time_budget:
            lines.append(time_budget)
        return SyncHookJSONOutput(reason="\n".join(lines))
    return {"PreCompact": [HookMatcher(matcher="*", hooks=[handler])]}
