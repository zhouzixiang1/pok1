"""Bot management MCP tools: reaping, cleanup, abandonment, experience pool."""

import json
import shutil
import time
from typing import Annotated, TypedDict

from claude_agent_sdk import tool

from evolution_core import (
    get_active_bots, get_bot_dir, find_current_v, load_ratings,
    clear_pipeline_checkpoint, git_has_tag,
    MAX_ACTIVE_BOTS, RESULTS_DIR, REPLAY_DIR,
    Glicko2Player,
)
from tool_helpers import (
    _get_ui, load_h2h_avg_winrates, PROJECT_ROOT,
)
from system_log import log_system_event

from evolution_infra import read_pipeline_checkpoint
from experience_pool import trim_experience_pool
from code_verification import seed_initial_bots


class ReapWeakestInput(TypedDict):
    pass


async def _do_reap_weakest(quiet: bool = False) -> dict:
    """Core reaping logic — callable directly (not via MCP)."""
    active_bots = get_active_bots()
    if len(active_bots) <= MAX_ACTIVE_BOTS:
        return {"reaped": False, "pool_size": len(active_bots)}

    ratings = load_ratings()
    h2h_winrates = load_h2h_avg_winrates()
    current_bot = f"claude_v{find_current_v()}"
    active_ratings = [(b, ratings.get(b, Glicko2Player())) for b in active_bots if b != current_bot]
    if not active_ratings:
        return {"reaped": False, "reason": "Only current bot in pool"}
    active_ratings.sort(key=lambda x: h2h_winrates.get(x[0], 0.0))
    weakest = active_ratings[0]
    culled_name = weakest[0]

    graveyard = PROJECT_ROOT / "bots" / "graveyard"
    graveyard.mkdir(exist_ok=True)
    target = graveyard / culled_name
    if target.exists():
        shutil.rmtree(target)
    bot_src = PROJECT_ROOT / "bots" / culled_name
    if not bot_src.exists():
        return {"reaped": False, "reason": f"{culled_name} already moved"}
    shutil.move(str(bot_src), str(target))

    try:
        if REPLAY_DIR.exists():
            for f in list(REPLAY_DIR.iterdir()):
                if culled_name in f.name:
                    f.unlink()
    except Exception:
        pass

    reap_signal = RESULTS_DIR / ".reap_signal"
    reap_signal.write_text(str(time.time()))

    if not quiet:
        log_system_event("bot.reaped", "warn", f"Reaped {culled_name} (h2h_wr={h2h_winrates.get(culled_name, 0.0):.2%})",
                         {"culled": culled_name, "remaining": len(active_bots) - 1})

    return {
        "reaped": True,
        "culled": culled_name,
        "h2h_avg_wr": round(h2h_winrates.get(culled_name, 0.0), 4),
        "rating": {"r": round(weakest[1].r, 1), "rd": round(weakest[1].rd, 1)},
        "remaining": len(active_bots) - 1,
    }


def _mcp_result(data: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


@tool("reap_weakest", f"Check if bot pool exceeds MAX_ACTIVE_BOTS and cull the weakest bot by H2H average win rate.", {})
async def reap_weakest(args):
    result = await _do_reap_weakest(quiet=args.get("quiet", False) if isinstance(args, dict) else False)
    return _mcp_result(result)


class CleanupIncompleteInput(TypedDict):
    pass


@tool("cleanup_incomplete", "Remove bot directories without .completed that have no git tag.", {})
async def cleanup_incomplete(args):
    cleaned = []
    bots_dir = PROJECT_ROOT / "bots"
    if bots_dir.exists():
        for d in sorted(bots_dir.iterdir()):
            if d.is_dir() and d.name.startswith("claude_v"):
                if not (d / ".completed").exists():
                    try:
                        v = int(d.name.split("_v")[1])
                    except (ValueError, IndexError):
                        continue
                    if not git_has_tag(v):
                        shutil.rmtree(d)
                        cleaned.append(d.name)
    return {"content": [{"type": "text", "text": json.dumps({"cleaned": cleaned, "count": len(cleaned)})}]}


class AbandonGenerationInput(TypedDict):
    pass


@tool("abandon_generation", "Clear pipeline checkpoint and remove incomplete next-gen directory. Use when a generation is stuck and needs to be restarted.", {})
async def abandon_generation(args):
    from evolution_core import PIPELINE_STATE_FILE
    checkpoint = read_pipeline_checkpoint() if PIPELINE_STATE_FILE.exists() else None
    cleared_checkpoint = False
    removed_dir = None

    if checkpoint:
        next_v = checkpoint.get("next_v")
        clear_pipeline_checkpoint()
        cleared_checkpoint = True
        if next_v is not None:
            next_dir = get_bot_dir(next_v)
            if next_dir.exists() and not (next_dir / ".completed").exists():
                shutil.rmtree(next_dir)
                removed_dir = f"claude_v{next_v}"
    else:
        # No checkpoint — clean up any incomplete dir for next version
        current_v = find_current_v()
        next_dir = get_bot_dir(current_v + 1)
        if next_dir.exists() and not (next_dir / ".completed").exists():
            shutil.rmtree(next_dir)
            removed_dir = f"claude_v{current_v + 1}"

    log_system_event("pipeline.abandoned", "warn", f"Abandoned generation (dir={removed_dir})",
                     {"removed_dir": removed_dir, "cleared_checkpoint": cleared_checkpoint})

    return {"content": [{"type": "text", "text": json.dumps({
        "abandoned": True,
        "cleared_checkpoint": cleared_checkpoint,
        "removed_directory": removed_dir,
    })}]}


class TrimExperienceInput(TypedDict):
    pass


@tool("trim_experience", "Trim the experience pool to keep only the most recent entries.", {})
async def trim_experience(args):
    trim_experience_pool(max_entries=8)
    return {"content": [{"type": "text", "text": json.dumps({"trimmed": True})}]}


@tool("seed_initial_bots", "Seed claude_v1 through claude_v6 from reference bots if they don't exist. Call this when get_status() returns current_v=0 or no completed bots.", {})
async def seed_initial_bots_tool(args):
    ui = _get_ui()
    seeded = seed_initial_bots(ui)
    return {"content": [{"type": "text", "text": json.dumps({"seeded": seeded})}]}


class ConsolidateExperienceInput(TypedDict):
    pass


@tool("consolidate_experience", "Use LLM to consolidate and deduplicate the experience pool.", {})
async def consolidate_experience(args):
    from evolution_core import _consolidate_experience_pool
    ui = _get_ui()
    await _consolidate_experience_pool(ui)
    return {"content": [{"type": "text", "text": json.dumps({"consolidated": True, "logs": ui.get_output()})}]}
