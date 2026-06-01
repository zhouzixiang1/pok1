"""Generation scheduler — three-phase evolution cycle.

Phase 1 (prepare_generation): Code-layer analysis and strategy decision.
Phase 2 (run_one_generation): LLM-driven pipeline execution (in orchestrator.py).
Phase 3 (post_generation_cleanup): Code-layer cleanup and maintenance.

Phase 1 is disposable (interrupt → re-run with fresh data).
Phase 2 preserves state on interrupt (session + checkpoint files).
Phase 3 is idempotent (interrupt → re-run safely).
"""

import json
import time
from dataclasses import dataclass, field


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
        MAX_ACTIVE_BOTS, MIN_GAMES_FOR_EVAL, find_current_v, get_active_bots, load_ratings,
        wait_for_daemon_eval,
    )

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    current_v = find_current_v()
    active_bots = get_active_bots()
    ratings = load_ratings()
    bot_name = f"claude_v{current_v}"

    # Wait for sufficient evaluation
    shutdown_event = shutdown_mgr if shutdown_mgr else None
    eval_kwargs = {"ui": ui, "shutdown_event": shutdown_event}
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

    # Three analysis LLM calls — each checks shutdown after
    from agent_master import _analyze_stagnation, _analyze_recent_matches
    from agent_review import _run_performance_verification

    stagnation = await _analyze_stagnation(current_v, active_bots, ratings, ui)
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    match_analysis = await _analyze_recent_matches(current_v, ui)
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    perf = await _run_performance_verification(current_v, ratings, ui)
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    # Strategy decision (code-layer, deterministic)
    strategy, source_v, parents = _decide_strategy(stagnation, perf, current_v, ratings)

    stagnation_text = json.dumps(stagnation, ensure_ascii=False) if stagnation else ""
    perf_text = perf or ""
    match_text = match_analysis or ""

    return GenerationContext(
        current_v=current_v,
        next_v=current_v + 1,
        strategy=strategy,
        source_v=source_v,
        crossover_parents=parents,
        stagnation_info=stagnation_text,
        match_analysis=match_text,
        performance_verification=perf_text,
    )


def _decide_strategy(stagnation, perf, current_v, ratings):
    """Deterministic strategy selection based on analysis results."""
    if stagnation and stagnation.get("is_stagnant") and stagnation.get("confidence") == "high":
        parents = _pick_crossover_parents(ratings, current_v)
        if parents:
            return "crossover", parents[0], parents

    if stagnation and stagnation.get("recommendation") == "branch" and stagnation.get("branch_from"):
        branch_v = _parse_branch_from(stagnation["branch_from"])
        if branch_v is not None:
            return "master", branch_v, ()

    return "master", current_v, ()


def _parse_branch_from(branch_str: str) -> int | None:
    try:
        return int(branch_str.split("_v")[1])
    except (ValueError, IndexError):
        return None


def _pick_crossover_parents(ratings, current_v) -> tuple | None:
    """Select two diverse parents for crossover."""
    from evolution_infra import get_active_bots
    from tool_helpers import load_h2h_avg_winrates

    active = get_active_bots()
    if len(active) < 2:
        return None
    h2h = load_h2h_avg_winrates()
    # Sort by rating descending
    from glicko2 import Glicko2Player
    rated = sorted(
        active,
        key=lambda b: ratings.get(b, Glicko2Player()).r,
        reverse=True,
    )
    if len(rated) < 2:
        return None
    parent_a = rated[0]
    # Pick parent with most different playstyle
    parent_b = rated[1]
    try:
        va = int(parent_a.split("_v")[1])
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
            from tool_status import reap_weakest
            await reap_weakest({"quiet": True})
        except Exception:
            pass

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return

    # Experience pool consolidation (every 3 generations)
    if ctx.gen_count > 0 and ctx.gen_count % 3 == 0:
        try:
            from agent_master import _consolidate_experience_pool
            await _consolidate_experience_pool(ui)
        except Exception:
            pass
