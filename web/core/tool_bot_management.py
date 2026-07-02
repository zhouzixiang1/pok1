"""Bot management MCP tools: reaping, cleanup, abandonment, experience pool."""

import fcntl
import json
import shutil
import time
from typing import Annotated, TypedDict

from tool_runtime_guard import tool

from evolution_core import (
    get_active_bots, get_bot_dir, find_current_v, find_latest_active_v, load_ratings,
    clear_pipeline_checkpoint, git_has_tag, git_dir_is_committed,
    find_max_committed_v, find_abandoned_version_floor, compute_next_generation_v,
    MAX_ACTIVE_BOTS, RESULTS_DIR, REPLAY_DIR,
    Glicko2Player,
)
from tool_helpers import (
    _get_ui, load_h2h_avg_winrates, load_strength_scores, PROJECT_ROOT,
)
from system_log import log_system_event

from evolution_infra import read_pipeline_checkpoint
from experience_pool import trim_experience_pool
from code_verification import seed_initial_bots

# A4 (2026-06-30): rate-limit state for abandon_generation. [timestamp, reason].
_LAST_ABANDON_TS = [0.0, ""]


class ReapWeakestInput(TypedDict):
    pass


async def _do_reap_weakest(quiet: bool = False) -> dict:
    """Core reaping logic — callable directly (not via MCP)."""
    active_bots = get_active_bots()
    if len(active_bots) <= MAX_ACTIVE_BOTS:
        return {"reaped": False, "pool_size": len(active_bots)}

    ratings = load_ratings()
    h2h_winrates = load_h2h_avg_winrates()
    strength_scores = load_strength_scores()
    current_bot = f"claude_v{find_latest_active_v()}"

    # Load bot stats to protect untested bots from reaping
    from tool_helpers import _read_json
    bot_stats = _read_json(PROJECT_ROOT / "web" / "core" / "results" / "bot_stats.json", {})

    # Exclude current bot and bots with zero games (untested — deserve evaluation first)
    candidates = []
    for b in active_bots:
        if b == current_bot:
            continue
        if bot_stats.get(b, {}).get("games", 0) == 0:
            continue
        candidates.append((b, ratings.get(b, Glicko2Player())))
    if not candidates:
        return {"reaped": False, "reason": "All remaining bots are current or untested"}

    # Protect bots with insufficient evaluation. Previously this also gated on
    # `rd > 100`, but that clause existed only to compensate for the buggy
    # decay_rd that snapped idle bots' RD up to 150 every cycle (collapsing their
    # conservative_rating). Now that decay_rd follows the official Glicko-2
    # formula, an idle veteran's RD stays low and its conservative_rating (r-2*rd)
    # reflects real strength — so reaping it when it is genuinely the weakest is
    # correct. Protection is therefore sample-based only: a bot with <600 games
    # has too little data for its rating to be trusted as a reap verdict.
    protected = set()
    for name, rating in candidates:
        n_total = bot_stats.get(name, {}).get("games", 0)
        if n_total < 600:
            protected.add(name)
    # Apply protection EXCEPT when pool overflow forces reap (avoid unbounded growth)
    if len(active_bots) <= MAX_ACTIVE_BOTS + 3:  # soft cap, allow protection
        filtered = [c for c in candidates if c[0] not in protected]
        if not filtered:
            return {"reaped": False, "reason": "all_protected",
                    "remaining": len(active_bots), "protected_count": len(protected)}
        candidates = filtered

    # Sort by conservative rating (r - 2*rd) as PRIMARY key. Glicko conservative
    # rating is implicitly weighted by opponent strength, far less noisy than
    # per-opponent h2h_avg_wr at low game counts.
    candidates.sort(key=lambda x: (x[1].r - 2 * x[1].rd,))
    weakest = candidates[0]
    culled_name = weakest[0]
    conservative = weakest[1].r - 2 * weakest[1].rd

    graveyard = PROJECT_ROOT / "bots" / "graveyard"
    graveyard.mkdir(exist_ok=True)
    target = graveyard / culled_name

    # Serialize concurrent reaps via file lock
    reap_lock = RESULTS_DIR / ".reap.lock"
    with open(reap_lock, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            # Re-check after acquiring lock — another process may have reaped this bot
            if target.exists():
                shutil.rmtree(target)
            bot_src = PROJECT_ROOT / "bots" / culled_name
            if not bot_src.exists():
                return {"reaped": False, "reason": f"{culled_name} already moved"}
            shutil.move(str(bot_src), str(target))
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)

    try:
        if REPLAY_DIR.exists():
            prefix = f"_{culled_name}_"
            for f in list(REPLAY_DIR.iterdir()):
                if prefix in f.name or f.name.endswith(f"_{culled_name}.json"):
                    f.unlink()
    except Exception:
        pass

    reap_signal = RESULTS_DIR / ".reap_signal"
    reap_signal.write_text(str(time.time()))

    log_system_event(
        "bot.reaped",
        "info" if quiet else "warn",
        (
            f"{'Auto-reaped' if quiet else 'Reaped'} {culled_name} by conservative Glicko "
            f"(r-2rd={conservative:.1f}, leaderboard={strength_scores.get(culled_name, 0.0):.4f}, "
            f"h2h_wr={h2h_winrates.get(culled_name, 0.0):.2%})"
        ),
        {
            "culled": culled_name,
            "remaining": len(active_bots) - 1,
            "selection_key": "conservative_glicko",
            "conservative_rating": round(conservative, 1),
            "leaderboard_score": round(strength_scores.get(culled_name, 0.0), 4),
            "h2h_avg_wr": round(h2h_winrates.get(culled_name, 0.0), 4),
            "quiet": quiet,
        },
    )

    return {
        "reaped": True,
        "culled": culled_name,
        "selection_key": "conservative_glicko",
        "conservative_rating": round(conservative, 1),
        "leaderboard_score": round(strength_scores.get(culled_name, 0.0), 4),
        "h2h_avg_wr": round(h2h_winrates.get(culled_name, 0.0), 4),
        "rating": {"r": round(weakest[1].r, 1), "rd": round(weakest[1].rd, 1)},
        "remaining": len(active_bots) - 1,
    }


def _mcp_result(data: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


@tool("reap_weakest", f"Check if bot pool exceeds MAX_ACTIVE_BOTS and cull the weakest bot by conservative rating, reporting unified strength.", {})
async def reap_weakest(args):
    result = await _do_reap_weakest(quiet=args.get("quiet", False) if isinstance(args, dict) else False)
    return _mcp_result(result)


class CleanupIncompleteInput(TypedDict):
    pass


@tool("cleanup_incomplete", "Remove bot directories without .completed that have no git tag.", {})
async def cleanup_incomplete(args):
    cleaned = []
    preserved = []
    bots_dir = PROJECT_ROOT / "bots"
    if bots_dir.exists():
        # Check for active pipeline checkpoint to avoid deleting mid-generation bots
        active_next_v = None
        checkpoint_file = RESULTS_DIR / "pipeline_state.json"
        if checkpoint_file.exists():
            try:
                ckpt = json.loads(checkpoint_file.read_text())
                stage = ckpt.get("stage")
                if ckpt.get("next_v") and stage not in (None, "archived"):
                    active_next_v = ckpt["next_v"]
            except Exception:
                pass
        for d in sorted(bots_dir.iterdir()):
            if d.is_dir() and d.name.startswith("claude_v"):
                if not (d / ".completed").exists():
                    try:
                        v = int(d.name.split("_v")[1])
                    except (ValueError, IndexError):
                        continue
                    if v == active_next_v:
                        continue
                    if not git_has_tag(v):
                        if git_dir_is_committed(v):
                            preserved.append(d.name)
                            log_system_event(
                                "bot.cleanup_incomplete_preserved",
                                "warn",
                                f"Preserved git-tracked incomplete bot {d.name} (no tag)",
                                {
                                    "version": v,
                                    "bot": d.name,
                                    "reason": "git_tracked_without_tag",
                                },
                            )
                            continue
                        shutil.rmtree(d)
                        cleaned.append(d.name)
    return {"content": [{"type": "text", "text": json.dumps({
        "cleaned": cleaned,
        "preserved_git_tracked": preserved,
        "count": len(cleaned),
    })}]}


class AbandonGenerationInput(TypedDict):
    pass


@tool("abandon_generation", "Clear pipeline checkpoint and remove incomplete next-gen directory. Use when a generation is stuck and needs to be restarted.", {})
async def abandon_generation(args):
    result = await _do_abandon_generation(reason="abandon_generation")
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


async def _do_abandon_generation(reason: str = "abandon_generation") -> dict:
    """Core abandon logic — clears the pipeline checkpoint and removes the
    incomplete next-gen directory.

    Shared by the ``abandon_generation`` MCP tool and forced-abandon paths
    (notably ``MASTER_EXHAUSTED`` in run_master, B2 v125 fix) so the latter no
    longer relies on the orchestrator LLM obeying a plain-text directive.

    Returns the abandon result dict (also written as a ``pipeline.abandoned``
    system event). The caller is responsible for clearing the orchestrator
    session BEFORE calling this if a stale session must not be resumed.
    """
    # A4 (2026-06-30): rate-limit abandons to prevent evolution-DoS / version-space
    # leak. A rogue or stuck LLM could spam abandon_generation, monotonically
    # incrementing next_v via the abandoned_versions floor and never letting any
    # generation reach the gates. Enforce a 60s cooldown between abandons.
    import time as _t
    now = _t.time()
    if (now - _LAST_ABANDON_TS[0]) < 60:
        try:
            log_system_event(
                "pipeline.abandon_rate_limited", "warn",
                f"abandon_generation rate-limited (cooldown {60 - (now - _LAST_ABANDON_TS[0]):.0f}s remaining). "
                f"Recent abandon was {_LAST_ABANDON_TS[1]}.",
                {"cooldown_remaining": 60 - (now - _LAST_ABANDON_TS[0]),
                 "last_abandon_reason": _LAST_ABANDON_TS[1]},
            )
        except Exception:
            pass
        return {"abandoned": False, "rate_limited": True,
                "reason": f"abandon cooldown active ({60 - (now - _LAST_ABANDON_TS[0]):.0f}s remaining)"}
    from evolution_core import PIPELINE_STATE_FILE
    checkpoint = read_pipeline_checkpoint() if PIPELINE_STATE_FILE.exists() else None
    cleared_checkpoint = False
    removed_dir = None
    abandoned_v = None

    if checkpoint:
        next_v = checkpoint.get("next_v")
        abandoned_v = next_v
        clear_pipeline_checkpoint()
        cleared_checkpoint = True
        if next_v is not None:
            next_dir = get_bot_dir(next_v)
            if next_dir.exists() and not (next_dir / ".completed").exists():
                if git_dir_is_committed(next_v):
                    log_system_event(
                        "pipeline.abandon_preserved_git_tracked",
                        "warn",
                        f"Preserved git-tracked incomplete v{next_v} during abandon",
                        {"version": next_v, "reason": "git_tracked_without_tag"},
                    )
                else:
                    shutil.rmtree(next_dir)
                    removed_dir = f"claude_v{next_v}"
    else:
        # No checkpoint — clean up any incomplete dir for authoritative next
        # version. Do not reuse current_v + 1 after abandoned generations.
        current_v = find_current_v()
        next_v = compute_next_generation_v(
            current_v=current_v,
            max_committed_v=find_max_committed_v(),
            abandoned_floor=find_abandoned_version_floor(),
        )
        next_dir = get_bot_dir(next_v)
        if next_dir.exists() and not (next_dir / ".completed").exists():
            abandoned_v = next_v
            if git_dir_is_committed(next_v):
                log_system_event(
                    "pipeline.abandon_preserved_git_tracked",
                    "warn",
                    f"Preserved git-tracked incomplete v{next_v} during abandon",
                    {"version": next_v, "reason": "git_tracked_without_tag"},
                )
            else:
                shutil.rmtree(next_dir)
                removed_dir = f"claude_v{next_v}"

    # P2 (2026-06-29 reboot analysis): record the abandoned version number so the
    # next prepare_generation skips it. Without this, the same next_v is reused
    # (find_current_v returns the last TAGGED version, so next_v = tagged+1 == the
    # just-abandoned number), causing the bot to retry the exact same dead-end
    # version (observed: v218 abandoned then re-prepared as v218 and committed).
    if abandoned_v is not None:
        try:
            from evolution_infra import RESULTS_DIR
            ab_file = RESULTS_DIR / "abandoned_versions.jsonl"
            with open(ab_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"v": abandoned_v, "reason": reason,
                                    "timestamp": __import__("time").time()}) + "\n")
        except Exception:
            pass

    log_system_event("pipeline.abandoned", "warn",
                     f"Abandoned generation ({reason}, dir={removed_dir})",
                     {"removed_dir": removed_dir, "cleared_checkpoint": cleared_checkpoint,
                      "reason": reason, "abandoned_v": abandoned_v})
    # A4: update rate-limit timestamp on successful abandon.
    _LAST_ABANDON_TS[0] = now
    _LAST_ABANDON_TS[1] = reason

    return {
        "abandoned": True,
        "cleared_checkpoint": cleared_checkpoint,
        "removed_directory": removed_dir,
        "reason": reason,
        "abandoned_v": abandoned_v,
    }


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
