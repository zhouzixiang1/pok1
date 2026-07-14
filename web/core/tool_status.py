"""Non-pipeline MCP tools for current status and daemon control.

Planning analysis is intentionally absent. The scheduler owns the immutable
generation evidence bundle and invokes analysts through the strict pipeline;
control tools must not reopen mutable evidence as an alternate planning path.
"""

import json
import os
from pathlib import Path

from typing import Annotated, TypedDict

from claude_agent_sdk import tool

from bot_namespace import bot_name as active_bot_name, parse_bot_version
from strength_order import match_score
from evolution_core import (
    get_active_bots,
    get_bot_dir,
    load_ratings,
    load_daemon_stats,
    check_code_size,
    git_has_tag,
    git_get_parent,
    start_daemon,
    stop_daemon,
    wait_for_daemon_eval,
    find_current_v,
    find_max_committed_v,
    find_abandoned_version_floor,
    compute_next_generation_v,
    locked_file,
    strict_epoch_projection,
    unpublished_candidate_versions,
)
from tool_helpers import load_h2h_avg_winrates, load_strength_scores

from tool_helpers import (
    _ratings_summary, _json_tool_result,
)
from evolution_infra import count_lines, read_locked_json


def _infra_path(name: str) -> Path:
    import evolution_infra
    return getattr(evolution_infra, name)



class GetStatusInput(TypedDict):
    pass


@tool("get_status", "Get the current evolution system status: latest bot version, top ratings, active bot count, and daemon status.", {})
async def get_status(args):
    """Get full system status."""
    epoch = strict_epoch_projection()
    active_bots = list(epoch["active_bots"])
    current_v = int(epoch["current_v"])
    next_v = int(epoch["next_v"])

    # Runtime files are only meaningful after the central epoch reset, and
    # even then only rows belonging to currently published identities may be
    # displayed.  This prevents retired v1..v142 ratings from reappearing in
    # an operator/API status response before the reset has archived them.
    if epoch["initialized"]:
        all_ratings = load_ratings()
        ratings = {name: all_ratings[name] for name in active_bots if name in all_ratings}
        daemon_stats = load_daemon_stats()
    else:
        ratings = {}
        daemon_stats = {}

    # Incomplete next-gen bot detection (in-progress from previous cycle).
    # Use the same floor as prepare_generation/control.status so MCP tools do
    # not report a stale current_v + 1 after abandoned generations.
    next_dir = get_bot_dir(next_v)
    incomplete_next_v = next_v if (next_dir.exists() and not (next_dir / ".completed").exists()) else None

    # Current bot rating reliability
    current_bot_name = (
        max(active_bots, key=lambda name: parse_bot_version(name) or -1)
        if active_bots else None
    )
    cur_p = ratings.get(current_bot_name)
    current_bot_rd = round(cur_p.rd, 1) if cur_p else None

    # Load bot stats for current bot
    bot_stats_data = (
        read_locked_json(_infra_path("BOT_STATS_FILE"), default={})
        if epoch["initialized"] else {}
    )
    cur_bs = bot_stats_data.get(current_bot_name, {}) if current_bot_name else {}
    games_played = cur_bs.get("games", 0)
    rating_reliable = games_played >= 100

    result = {
        "current_v": current_v,
        "next_v": next_v,
        "max_committed_v": epoch["max_committed_v"],
        "abandoned_floor": epoch["abandoned_floor"],
        "active_bots_count": len(active_bots),
        "top_ratings": _ratings_summary(ratings),
        "daemon_total_games": daemon_stats.get("total_games", 0),
        "incomplete_next_v": incomplete_next_v,
        "current_bot_rd": current_bot_rd,
        "current_bot_games": games_played,
        "current_bot_win_rate": cur_bs.get("win_rate", 0.0),
        "current_bot_leaderboard_score": (
            load_strength_scores().get(current_bot_name)
            if current_bot_name else None
        ),
        "current_bot_h2h_avg_wr": (
            load_h2h_avg_winrates().get(current_bot_name)
            if current_bot_name else None
        ),
        "rating_reliable": rating_reliable,
        "evaluation_epoch": epoch["evaluation_epoch"],
        "epoch_state": epoch["state"],
        "epoch_initialized": epoch["initialized"],
        "reset_receipt_valid": epoch["reset_receipt_valid"],
        "reset_receipt_issues": epoch["reset_receipt_issues"],
        "operator_action": epoch["operator_action"],
        "operator_command": epoch["operator_command"],
        "active_generation": epoch["active_generation"],
        "ignored_checkpoint": epoch["ignored_checkpoint"],
        "unpublished_candidate_versions": unpublished_candidate_versions(),
    }
    return _json_tool_result(result)


class GetBotInfoInput(TypedDict):
    version: Annotated[int, "Bot version number"]


@tool("get_bot_info", "Get detailed info about a specific bot version: rating, parent, files, code size.", {"version": int})
async def get_bot_info(args):
    v = args["version"]
    bot_name = active_bot_name(v)
    bot_dir = get_bot_dir(v)

    if not bot_dir.exists():
        return _json_tool_result({"error": f"Bot v{v} not found"})

    ratings = load_ratings()
    p = ratings.get(bot_name)
    parent = git_get_parent(v) if git_has_tag(v) else None
    parent_v = None
    if parent is not None:
        try:
            parsed_parent = parse_bot_version(str(parent))
            parent_v = parsed_parent if parsed_parent is not None else int(str(parent).replace("v", ""))
        except ValueError:
            parent_v = None

    result = {
        "version": v,
        "exists": True,
        "completed": (bot_dir / ".completed").exists(),
        "has_git_tag": git_has_tag(v),
        "rating": {"r": round(p.r, 1), "rd": round(p.rd, 1)} if p else None,
        "parent_v": parent_v if parent_v is not None else parent,
    }

    # Code size info — use parent as source_dir for adaptive limits
    if bot_dir.exists():
        py_files = list(bot_dir.glob("*.py"))
        result["files"] = [f.name for f in py_files]
        result["total_lines"] = sum(count_lines(f) for f in py_files)
        source_dir = get_bot_dir(parent_v) if parent_v else None
        _, oversized = check_code_size(bot_dir, source_dir=source_dir)
        if oversized:
            result["oversized_files"] = {
                name: {"lines": lines, "limit": limit}
                for name, lines, limit in oversized
            }

    return _json_tool_result(result)


class GetMatchHistoryInput(TypedDict):
    version: Annotated[int, "Bot version to filter for"]
    n: Annotated[int, "Number of recent matches to return"]


@tool("get_match_history", "Get recent match results for a specific bot version.", {"version": int, "n": int})
async def get_match_history(args):
    v = args["version"]
    n = args.get("n", 5)
    bot_name = active_bot_name(v)

    history_file = _infra_path("MATCH_HISTORY_FILE")
    if not history_file.exists():
        return _json_tool_result({"matches": []})

    entries = []
    from rating_snapshot import _admitted_70_hand_history_sample
    with locked_file(history_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _admitted_70_hand_history_sample(entry) is None:
                continue
            if entry.get("bot0") == bot_name or entry.get("bot1") == bot_name:
                entries.append(entry)

    entries = entries[-n:]
    return _json_tool_result({"matches": entries})


class StartDaemonInput(TypedDict):
    workers: Annotated[int, "Number of parallel battle workers"]
    pairs: Annotated[int, "Number of match pairs per rating period"]


@tool("start_daemon", "Start the background ELO daemon that continuously runs mirror battles and updates ratings.", {"workers": int, "pairs": int})
async def start_eval_daemon(args):
    workers = args.get("workers", max(1, int(os.cpu_count() * 28 / 32)))
    pairs = args.get("pairs", 5)
    proc = start_daemon(workers=workers, pairs=pairs)
    running = proc.poll() is None
    return _json_tool_result({
        "daemon_started": running,
        "pid": proc.pid,
        "workers": workers,
        "pairs": pairs,
    })


class StopDaemonInput(TypedDict):
    pass


@tool("stop_daemon", "Stop the background ELO daemon.", {})
async def stop_eval_daemon(args):
    stop_daemon()
    return _json_tool_result({"daemon_stopped": True})


class WaitForEvalInput(TypedDict):
    version: Annotated[int, "Bot version to wait for evaluation"]
    timeout: Annotated[int, "Timeout in seconds (default 600)"]
    min_games: Annotated[int, "Minimum games required (default 100)"]


@tool("wait_for_eval", "Wait for the daemon to evaluate a bot (enough games played). Returns whether eval completed.", {"version": int, "timeout": int, "min_games": int})
async def wait_for_eval(args):
    v = args["version"]
    timeout = args.get("timeout", 600)
    min_games = args.get("min_games", 100)
    bot_name = active_bot_name(v)

    success = await wait_for_daemon_eval(bot_name, timeout=timeout, min_games=min_games)
    ratings = load_ratings()
    p = ratings.get(bot_name)

    # Load bot stats
    bot_stats_data = read_locked_json(_infra_path("BOT_STATS_FILE"), default={})
    bs = bot_stats_data.get(bot_name, {})

    result = {
        "version": v,
        "eval_completed": success,
        "current_rating": {"r": round(p.r, 1), "rd": round(p.rd, 1)} if p else None,
        "bot_stats": {"games": bs.get("games", 0), "win_rate": bs.get("win_rate", 0.0)} if bs else None,
    }
    return _json_tool_result(result)


class GetH2HInput(TypedDict):
    bot_name: Annotated[str, "Bot name (e.g. national_v14)"]
    opponent: Annotated[str, "Optional: specific opponent name. If omitted, returns all opponents."]


@tool("get_h2h", "Get head-to-head win/loss data for a bot. Shows per-opponent win rates — who this bot beats and loses to.", {"bot_name": str, "opponent": str})
async def get_h2h(args):
    bot_name = args["bot_name"]
    opponent = args.get("opponent")

    h2h_file = _infra_path("H2H_FILE")
    h2h_source = "live"
    try:
        from evolution_infra import read_pipeline_checkpoint
        ckpt = read_pipeline_checkpoint() or {}
        next_v = ckpt.get("next_v")
        if next_v is not None:
            from evidence_snapshot import load_generation_snapshot_identity
            snapshot = load_generation_snapshot_identity(next_v)
            if snapshot.get("available"):
                h2h_file = Path(snapshot["h2h_path"])
                h2h_source = "generation_snapshot"
    except Exception:
        pass
    if not h2h_file.exists():
        return _json_tool_result({"error": "No H2H data yet", "bot_name": bot_name})

    try:
        with locked_file(h2h_file, "r") as f:
            h2h = json.load(f)
    except Exception:
        return _json_tool_result({"error": "Failed to read H2H data"})

    results = {}
    for k, v in h2h.items():
        parts = k.split(" vs ")
        if len(parts) != 2:
            continue
        a, b = parts
        if bot_name not in (a, b):
            continue
        opp = b if bot_name == a else a
        if opponent and opp != opponent:
            continue
        g = v.get("games", 0)
        bot_wins = v.get("a_wins", 0) if bot_name == a else v.get("b_wins", 0)
        opp_wins = v.get("b_wins", 0) if bot_name == a else v.get("a_wins", 0)
        draws = v.get("draws", 0)
        wr = match_score(bot_wins, draws, g)
        wr = wr if wr is not None else 0.5
        tag = "STRENGTH" if wr > 0.60 else ("WEAKNESS" if wr < 0.40 else "neutral")
        results[opp] = {
            "wins": bot_wins,
            "losses": opp_wins,
            "draws": draws,
            "games": g,
            "win_rate": round(wr, 4),
            "tag": tag,
        }

    if not results:
        return _json_tool_result({"bot_name": bot_name, "opponents": {}, "message": "No H2H data found"})

    sorted_results = dict(sorted(results.items(), key=lambda x: x[1]["win_rate"]))
    return _json_tool_result({"bot_name": bot_name, "opponents": sorted_results, "source": h2h_source})


class GetBotStatsInput(TypedDict):
    bot_name: Annotated[str, "Bot name (e.g. national_v14)"]


@tool("get_bot_stats", "Get per-bot stats: total wins, losses, games, win rate.", {"bot_name": str})
async def get_bot_stats(args):
    bot_name = args["bot_name"]

    bot_stats_file = _infra_path("BOT_STATS_FILE")
    if not bot_stats_file.exists():
        return _json_tool_result({"error": "No bot stats yet", "bot_name": bot_name})

    try:
        with locked_file(bot_stats_file, "r") as f:
            all_stats = json.load(f)
    except Exception:
        return _json_tool_result({"error": "Failed to read bot stats"})

    bs = all_stats.get(bot_name)
    if not bs:
        return _json_tool_result({"error": f"No stats for {bot_name}"})

    return _json_tool_result({"bot_name": bot_name, **bs})
