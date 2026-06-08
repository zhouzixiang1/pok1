"""Generation scheduler — three-phase evolution cycle.

Phase 1 (prepare_generation): Code-layer analysis and strategy decision.
Phase 2 (run_one_generation): LLM-driven pipeline execution (in orchestrator.py).
Phase 3 (post_generation_cleanup): Code-layer cleanup and maintenance.

Phase 1 is disposable (interrupt → re-run with fresh data).
Phase 2 preserves state on interrupt (session + checkpoint files).
Phase 3 is idempotent (interrupt → re-run safely).
"""

import asyncio
import json
import logging
import time
import traceback
from dataclasses import dataclass, field

log = logging.getLogger("pok.scheduler")


@dataclass
class GenerationContext:
    """Pre-computed context for one generation."""
    current_v: int
    next_v: int
    strategy: str              # "master" | "crossover"
    source_v: int              # branch_from or current_v
    crossover_parents: tuple = ()  # (parent_a, parent_b) if crossover
    stagnation_info: str = ""
    match_analysis: str = ""
    performance_verification: str = ""
    gen_count: int = 0


async def prepare_generation(shutdown_mgr, ui=None, min_games=None) -> GenerationContext | None:
    """Phase 1: Analyze state, decide strategy. Disposable on interrupt."""
    from evolution_infra import (
        MAX_ACTIVE_BOTS, MIN_GAMES_FOR_EVAL, find_current_v, find_latest_active_v, get_active_bots, load_ratings,
        wait_for_daemon_eval,
    )

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    current_v = find_current_v()       # 版本编号（含 graveyard），用于 next_v
    active_v = find_latest_active_v()  # 活跃 bot（排除 graveyard），用于 eval/分析
    active_bots = get_active_bots()
    ratings = load_ratings()
    bot_name = f"claude_v{active_v}"   # 等待活跃 bot 的 eval（核心 fix）

    # Reap bots if pool exceeds limit — reduces starvation in match selection
    if len(active_bots) > MAX_ACTIVE_BOTS:
        from tool_bot_management import _do_reap_weakest
        reap_count = 0
        while len(get_active_bots()) > MAX_ACTIVE_BOTS and reap_count < 10:
            try:
                result = await _do_reap_weakest(quiet=True)
                if not result.get("reaped"):
                    break
                if ui:
                    ui.log_history(f"淘汰 {result['culled']} (池 {result['remaining']}/{MAX_ACTIVE_BOTS})", "info")
            except Exception as e:
                log.warning("Pre-eval reap failed: %s\n%s", e, traceback.format_exc())
                if ui:
                    ui.log_history(f"淘汰失败: {e}", "warn")
                break
            reap_count += 1

    # Wait for sufficient evaluation
    eval_kwargs = {"ui": ui, "shutdown_event": shutdown_mgr}
    if min_games is not None:
        eval_kwargs["min_games"] = min_games
    eval_ok = await wait_for_daemon_eval(bot_name, **eval_kwargs)
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None
    if not eval_ok:
        if ui:
            ui.log_history("Waiting for evaluation (insufficient games)...", "info")
        return None

    # Cleanup incomplete bot dirs from previous interrupted cycles
    _cleanup_incomplete()
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    # Load prev critic insights from archive
    prev_critic_info = ""
    try:
        from evolution_infra import RESULTS_DIR
        archive_dir = RESULTS_DIR / "archive"
        if archive_dir.exists():
            archives = sorted(archive_dir.glob("v*.json"), reverse=True)
            if archives:
                latest = json.loads(archives[0].read_text())
                critic_data = latest.get("critic_data", {})
                if critic_data:
                    sa = critic_data.get("strategic_assessment", "")
                    lo = critic_data.get("local_optima_warning", False)
                    if sa or lo:
                        prev_critic_info = f"Previous Critic assessment: {sa}"
                        if lo:
                            prev_critic_info += "\n⚠ LOCAL OPTIMA WARNING: Critic detected potential local optimum in previous generation."
    except Exception:
        pass

    # Combined analysis (stagnation + performance) + match analysis — run in parallel
    from combined_analyst import _run_combined_analysis
    from agent_master import _analyze_recent_matches

    combined_result, match_result = await asyncio.gather(
        _run_combined_analysis(active_v, active_bots, ratings, ui, prev_critic_info),
        _analyze_recent_matches(active_v, ui),
        return_exceptions=True,
    )

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    # Unpack results, treating exceptions as failures
    combined = combined_result if not isinstance(combined_result, BaseException) else None
    match_analysis = match_result if not isinstance(match_result, BaseException) else ""

    if isinstance(combined_result, BaseException):
        log.warning("Combined analysis failed: %s", combined_result)
    if isinstance(match_result, BaseException):
        log.warning("Match analysis failed: %s", match_result)

    # Strategy decision (code-layer, deterministic)
    strategy, source_v, parents = _decide_strategy(combined, active_v, ratings)

    stagnation_text = json.dumps(combined, ensure_ascii=False) if combined else ""
    perf_text = stagnation_text  # Combined result serves as both
    match_text = match_analysis or ""

    return GenerationContext(
        current_v=active_v,
        next_v=current_v + 1,
        strategy=strategy,
        source_v=source_v,
        crossover_parents=parents,
        stagnation_info=stagnation_text,
        match_analysis=match_text,
        performance_verification=perf_text,
        gen_count=current_v,
    )


def _decide_strategy(combined, current_v, ratings):
    """Deterministic strategy selection based on combined analysis results.

    The combined analysis merges stagnation and performance data into one dict:
    - is_stagnant + confidence → branch or crossover
    - diversity_needed → crossover injection
    - recommendation + branch_from → branch from specific ancestor
    """
    if combined is None:
        return "master", current_v, ()

    # Priority 1: Stagnation with high/medium confidence → crossover
    # This is the PRIMARY escape hatch from local optima — must fire before
    # recommended_source so stagnation always triggers diversity injection.
    if combined.get("is_stagnant") and combined.get("confidence") != "low":
        parents = _pick_crossover_parents(ratings, current_v)
        if parents:
            return "crossover", parents[0], parents

    # Priority 2: LLM-recommended source (only for non-stagnant systems).
    # Validates that the recommended bot is active (not in graveyard).
    rec_source = combined.get("recommended_source", "")
    if rec_source:
        rec_v = _parse_branch_from(rec_source)
        if rec_v is not None and rec_v >= 1:
            from evolution_infra import get_active_bots, get_bot_dir
            # Only accept active bots (not graveyard) as evolution source
            active = get_active_bots()
            if f"claude_v{rec_v}" in active:
                if rec_v != current_v:
                    rationale = combined.get("source_rationale", "")
                    log.info("LLM recommended source: v%d (instead of latest v%d). %s",
                             rec_v, current_v, rationale[:200])
                return "master", rec_v, ()

    # Priority 3: Explicit branch recommendation
    if combined.get("recommendation") == "branch" and combined.get("branch_from"):
        branch_v = _parse_branch_from(combined["branch_from"])
        if branch_v is not None and branch_v >= 1:
            return "master", branch_v, ()

    # Priority 4: Diversity injection
    if combined.get("diversity_needed"):
        parents = _pick_crossover_parents(ratings, current_v)
        if parents:
            log.info("Diversity injection: forcing crossover (%s, %s) to break local optimum",
                     f"v{parents[0]}", f"v{parents[1]}")
            return "crossover", parents[0], parents

    # Fallback: LLM did not recommend a source, use current_v
    return "master", current_v, ()


def _parse_branch_from(branch_str: str) -> int | None:
    try:
        return int(branch_str)
    except ValueError:
        pass
    try:
        return int(branch_str.split("_v")[1])
    except (ValueError, IndexError):
        pass
    try:
        return int(branch_str.lstrip("v"))
    except (ValueError, IndexError):
        return None


def _pick_crossover_parents(ratings, current_v) -> tuple | None:
    """Select two diverse parents for crossover.

    Parent A: highest h2h_avg_wr (strongest bot).
    Parent B: highest h2h_avg_wr with version gap >= 3 from parent A
    (strategy diversity — non-adjacent versions likely differ more).
    Falls back to second-highest h2h_avg_wr if no gap candidate exists.
    """
    from evolution_infra import get_active_bots
    from tool_helpers import load_h2h_avg_winrates

    active = get_active_bots()
    if len(active) < 2:
        return None
    h2h = load_h2h_avg_winrates()
    ranked = sorted(
        active,
        key=lambda b: h2h.get(b, 0.0),
        reverse=True,
    )
    if len(ranked) < 2:
        return None

    parent_a = ranked[0]
    try:
        va = int(parent_a.split("_v")[1])
    except (ValueError, IndexError):
        return None

    # Find diverse parent B: prefer version gap >= 3 for strategy diversity
    parent_b = None
    for candidate in ranked[1:]:
        try:
            vc = int(candidate.split("_v")[1])
        except (ValueError, IndexError):
            continue
        if abs(vc - va) >= 3:
            parent_b = candidate
            break

    # Fallback: second highest if no gap candidate
    if parent_b is None:
        parent_b = ranked[1]

    try:
        vb = int(parent_b.split("_v")[1])
        return (va, vb)
    except (ValueError, IndexError):
        return None


def _cleanup_incomplete():
    """Remove incomplete bot directories that have no git tag and no active checkpoint."""
    import shutil
    from pathlib import Path
    from evolution_infra import PROJECT_ROOT, git_has_tag, RESULTS_DIR

    bots_dir = PROJECT_ROOT / "bots"
    if not bots_dir.exists():
        return
    for d in sorted(bots_dir.iterdir()):
        if d.is_dir() and d.name.startswith("claude_v"):
            if not (d / ".completed").exists():
                try:
                    v = int(d.name.split("_v")[1])
                except (ValueError, IndexError):
                    continue
                if not git_has_tag(v):
                    # Skip if there's an active pipeline checkpoint for this version
                    checkpoint_file = RESULTS_DIR / "pipeline_state.json"
                    if checkpoint_file.exists():
                        try:
                            import json as _json
                            ckpt = _json.loads(checkpoint_file.read_text())
                            if ckpt.get("next_v") == v and ckpt.get("stage") not in (None, "archived"):
                                continue
                        except Exception:
                            pass
                    shutil.rmtree(d, ignore_errors=True)


async def post_generation_cleanup(shutdown_mgr, ui, ctx: GenerationContext):
    """Phase 3: Idempotent post-generation cleanup."""
    from evolution_infra import MAX_ACTIVE_BOTS, get_active_bots

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return

    # Auto-reap if pool exceeds limit
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        try:
            from tool_bot_management import _do_reap_weakest
            reap_count = 0
            while len(get_active_bots()) > MAX_ACTIVE_BOTS and reap_count < 10:
                result = await _do_reap_weakest(quiet=True)
                if not result.get("reaped"):
                    break
                reap_count += 1
        except Exception as e:
            log.warning("Auto-reap failed: %s\n%s", e, traceback.format_exc())
            if ui:
                ui.log_history(f"Auto-reap failed: {e}", "warn")

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return

    # Experience pool consolidation (every 3 generations, or when too many unconsolidated entries)
    should_consolidate = ctx.gen_count > 0 and ctx.gen_count % 3 == 0
    if not should_consolidate:
        # Also trigger when RECENT_LESSONS has too many entries (prevents stale/contradictory data)
        from evolution_infra import EXPERIENCE_FILE
        if EXPERIENCE_FILE.exists():
            try:
                content = EXPERIENCE_FILE.read_text()
                recent_section = content.split("## RECENT_LESSONS")[-1] if "## RECENT_LESSONS" in content else ""
                recent_entries = [line for line in recent_section.split("\n")
                                  if line.strip().startswith("- **")]
                if len(recent_entries) >= 4:
                    should_consolidate = True
                    log.info("Triggering experience consolidation: %d RECENT_LESSONS entries (threshold: 4)",
                             len(recent_entries))
            except Exception:
                pass

    if should_consolidate:
        try:
            from experience_archivist import _consolidate_experience_pool
            # Extract exhausted_directions from pipeline checkpoint
            exhausted_dirs = ""
            try:
                from evolution_infra import read_pipeline_checkpoint
                ckpt = read_pipeline_checkpoint()
                if ckpt:
                    da = ckpt.get("direction_audit", {})
                    ed = da.get("exhausted_directions", [])
                    if ed:
                        exhausted_dirs = ", ".join(ed)
            except Exception:
                pass
            await _consolidate_experience_pool(ui, exhausted_directions=exhausted_dirs)
        except Exception as e:
            if ui:
                ui.log_history(f"Experience consolidation failed: {e}", "warn")
