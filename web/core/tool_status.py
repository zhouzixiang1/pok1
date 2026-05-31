"""Non-pipeline MCP tools: status queries, daemon control, bot management, and analysis.

These tools do NOT call any LLM directly. They query data, manage the daemon,
and handle bot lifecycle operations.
"""

import json
import shutil
import sys
from pathlib import Path
from typing import Annotated, TypedDict

from claude_agent_sdk import tool

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
    seed_initial_bots,
    trim_experience_pool,
    _analyze_recent_matches,
    _analyze_stagnation,
    RATINGS_FILE, BOT_STATS_FILE, H2H_FILE,
    locked_file,
)
from glicko2 import Glicko2Player
from tool_helpers import load_h2h_avg_winrates

from tool_helpers import (
    _get_ui, _ratings_summary, _json_tool_result, _bot_main,
    PROJECT_ROOT,
)


class GetStatusInput(TypedDict):
    pass


@tool("get_status", "Get the current evolution system status: latest bot version, top ratings, active bot count, and daemon status.", {})
async def get_status(args):
    """Get full system status."""
    active_bots = get_active_bots()

    # Find current_v from completed bots + git tags
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

    ratings = load_ratings()
    daemon_stats = load_daemon_stats()

    # Incomplete next-gen bot detection (in-progress from previous cycle)
    next_dir = get_bot_dir(current_v + 1)
    incomplete_next_v = (current_v + 1) if (next_dir.exists() and not (next_dir / ".completed").exists()) else None

    # Current bot rating reliability
    cur_p = ratings.get(f"claude_v{current_v}")
    current_bot_rd = round(cur_p.rd, 1) if cur_p else None

    # Load bot stats for current bot
    bot_stats_data = {}
    bot_stats_file = PROJECT_ROOT / "web" / "core" / "results" / "bot_stats.json"
    if bot_stats_file.exists():
        try:
            with open(bot_stats_file, "r") as f:
                bot_stats_data = json.load(f)
        except Exception:
            pass
    cur_bs = bot_stats_data.get(f"claude_v{current_v}", {})
    games_played = cur_bs.get("games", 0)
    rating_reliable = games_played >= 100

    # Recent worker failures for context
    from evolution_core import _load_recent_failures
    recent_failures = _load_recent_failures(3)

    result = {
        "current_v": current_v,
        "next_v": current_v + 1,
        "active_bots_count": len(active_bots),
        "top_ratings": _ratings_summary(ratings),
        "daemon_total_games": daemon_stats.get("total_games", 0),
        "incomplete_next_v": incomplete_next_v,
        "current_bot_rd": current_bot_rd,
        "current_bot_games": games_played,
        "current_bot_win_rate": cur_bs.get("win_rate", 0.0),
        "current_bot_h2h_avg_wr": load_h2h_avg_winrates().get(f"claude_v{current_v}", 0.5),
        "rating_reliable": rating_reliable,
        "recent_worker_failures": recent_failures,
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class GetBotInfoInput(TypedDict):
    version: Annotated[int, "Bot version number"]


@tool("get_bot_info", "Get detailed info about a specific bot version: rating, parent, files, code size.", {"version": int})
async def get_bot_info(args):
    v = args["version"]
    bot_name = f"claude_v{v}"
    bot_dir = get_bot_dir(v)

    if not bot_dir.exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": f"Bot v{v} not found"})}]}

    ratings = load_ratings()
    p = ratings.get(bot_name)
    parent = git_get_parent(v) if git_has_tag(v) else None

    result = {
        "version": v,
        "exists": True,
        "completed": (bot_dir / ".completed").exists(),
        "has_git_tag": git_has_tag(v),
        "rating": {"r": round(p.r, 1), "rd": round(p.rd, 1)} if p else None,
        "parent_v": parent,
    }

    # Code size info
    if bot_dir.exists():
        total_lines, oversized = check_code_size(bot_dir)
        files = [f.name for f in bot_dir.glob("*.py")]
        result["files"] = files
        result["total_lines"] = total_lines
        if oversized:
            result["oversized_files"] = {name: lines for name, lines in oversized}

    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class GetMatchHistoryInput(TypedDict):
    version: Annotated[int, "Bot version to filter for"]
    n: Annotated[int, "Number of recent matches to return"]


@tool("get_match_history", "Get recent match results for a specific bot version.", {"version": int, "n": int})
async def get_match_history(args):
    v = args["version"]
    n = args.get("n", 5)
    bot_name = f"claude_v{v}"

    history_file = PROJECT_ROOT / "web" / "core" / "results" / "match_history.jsonl"
    if not history_file.exists():
        return {"content": [{"type": "text", "text": json.dumps({"matches": []})}]}

    entries = []
    with open(history_file, "r") as f:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_SH)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("bot0") == bot_name or entry.get("bot1") == bot_name:
                entries.append(entry)
        fcntl.flock(f, fcntl.LOCK_UN)

    entries = entries[-n:]
    return {"content": [{"type": "text", "text": json.dumps({"matches": entries}, indent=2, ensure_ascii=False)}]}


class RunMatchAnalysisInput(TypedDict):
    source_v: Annotated[int, "Bot version to analyze"]


@tool("run_match_analysis", "Analyze recent losses from replay data for a bot version. Returns weaknesses, patterns, and recommendations.", {"source_v": int})
async def run_match_analysis(args):
    source_v = args["source_v"]
    ui = _get_ui()
    output = await _analyze_recent_matches(source_v, ui)
    result = {
        "analysis": output,
        "logs": ui.get_output(),
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class StartDaemonInput(TypedDict):
    workers: Annotated[int, "Number of parallel battle workers"]
    pairs: Annotated[int, "Number of match pairs per rating period"]


@tool("start_daemon", "Start the background ELO daemon that continuously runs mirror battles and updates ratings.", {"workers": int, "pairs": int})
async def start_eval_daemon(args):
    workers = args.get("workers", 14)
    pairs = args.get("pairs", 5)
    proc = start_daemon(workers=workers, pairs=pairs)
    running = proc.poll() is None
    return {"content": [{"type": "text", "text": json.dumps({
        "daemon_started": running,
        "pid": proc.pid,
        "workers": workers,
        "pairs": pairs,
    })}]}


class StopDaemonInput(TypedDict):
    pass


@tool("stop_daemon", "Stop the background ELO daemon.", {})
async def stop_eval_daemon(args):
    stop_daemon()
    return {"content": [{"type": "text", "text": json.dumps({"daemon_stopped": True})}]}


class WaitForEvalInput(TypedDict):
    version: Annotated[int, "Bot version to wait for evaluation"]
    timeout: Annotated[int, "Timeout in seconds (default 600)"]
    min_games: Annotated[int, "Minimum games required (default 100)"]


@tool("wait_for_eval", "Wait for the daemon to evaluate a bot (enough games played). Returns whether eval completed.", {"version": int, "timeout": int, "min_games": int})
async def wait_for_eval(args):
    v = args["version"]
    timeout = args.get("timeout", 600)
    min_games = args.get("min_games", 100)
    bot_name = f"claude_v{v}"

    success = await wait_for_daemon_eval(bot_name, timeout=timeout, min_games=min_games)
    ratings = load_ratings()
    p = ratings.get(bot_name)

    # Load bot stats
    bot_stats_data = {}
    bot_stats_file = PROJECT_ROOT / "web" / "core" / "results" / "bot_stats.json"
    if bot_stats_file.exists():
        try:
            with open(bot_stats_file, "r") as f:
                bot_stats_data = json.load(f)
        except Exception:
            pass
    bs = bot_stats_data.get(bot_name, {})

    result = {
        "version": v,
        "eval_completed": success,
        "current_rating": {"r": round(p.r, 1), "rd": round(p.rd, 1)} if p else None,
        "bot_stats": {"games": bs.get("games", 0), "win_rate": bs.get("win_rate", 0.0)} if bs else None,
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class ReapWeakestInput(TypedDict):
    pass


@tool("reap_weakest", "Check if bot pool exceeds 30 and cull the weakest bot by H2H average win rate.", {})
async def reap_weakest(args):
    active_bots = get_active_bots()
    if len(active_bots) <= 30:
        return {"content": [{"type": "text", "text": json.dumps({"reaped": False, "pool_size": len(active_bots)})}]}

    ratings = load_ratings()
    h2h_winrates = load_h2h_avg_winrates()
    active_ratings = [(b, ratings.get(b, Glicko2Player())) for b in active_bots]
    active_ratings.sort(key=lambda x: h2h_winrates.get(x[0], 0.0))
    weakest = active_ratings[0]
    culled_name = weakest[0]

    graveyard = PROJECT_ROOT / "bots" / "graveyard"
    graveyard.mkdir(exist_ok=True)
    target = graveyard / culled_name
    if target.exists():
        shutil.rmtree(target)
    shutil.move(PROJECT_ROOT / "bots" / culled_name, target)

    # Clean up ratings
    if culled_name in ratings:
        del ratings[culled_name]
    with locked_file(RATINGS_FILE, "w") as f:
        json.dump({k: v.to_dict() for k, v in ratings.items()}, f, indent=2)

    # Clean up bot_stats
    if BOT_STATS_FILE.exists():
        try:
            with locked_file(BOT_STATS_FILE, "r+") as f:
                bs = json.load(f)
                if culled_name in bs:
                    del bs[culled_name]
                    f.seek(0)
                    f.truncate()
                    json.dump(bs, f, indent=2)
        except Exception:
            pass
    # Clean up H2H data
    if H2H_FILE.exists():
        try:
            with locked_file(H2H_FILE, "r+") as f:
                h2h = json.load(f)
                changed = False
                for key in list(h2h.keys()):
                    if culled_name in key.split(" vs "):
                        del h2h[key]
                        changed = True
                if changed:
                    f.seek(0)
                    f.truncate()
                    json.dump(h2h, f, indent=2)
        except Exception:
            pass

    # Signal daemon to immediately refresh bot list
    reap_signal = Path(__file__).parent / "results" / ".reap_signal"
    reap_signal.touch()

    return {"content": [{"type": "text", "text": json.dumps({
        "reaped": True,
        "culled": culled_name,
        "h2h_avg_wr": round(h2h_winrates.get(culled_name, 0.0), 4),
        "rating": {"r": round(weakest[1].r, 1), "rd": round(weakest[1].rd, 1)},
        "remaining": len(active_bots) - 1,
    })}]}


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
                    v = int(d.name.split("_v")[1])
                    if not git_has_tag(v):
                        shutil.rmtree(d)
                        cleaned.append(d.name)
    return {"content": [{"type": "text", "text": json.dumps({"cleaned": cleaned, "count": len(cleaned)})}]}


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


class AnalyzeStagnationInput(TypedDict):
    source_v: Annotated[int, "Current bot version"]
    active_bots: Annotated[list, "List of active bot names"]


@tool("analyze_stagnation", "Analyze whether the evolution is stagnating or just experiencing Glicko variance.", {"source_v": int, "active_bots": list})
async def analyze_stagnation(args):
    source_v = args["source_v"]
    active_bots_names = args.get("active_bots", [])

    ratings = load_ratings()
    ui = _get_ui()
    result = await _analyze_stagnation(source_v, active_bots_names, ratings, ui)

    return {"content": [{"type": "text", "text": json.dumps({
        "analysis": result,
        "logs": ui.get_output(),
    }, indent=2, ensure_ascii=False)}]}


class RunPerformanceVerificationInput(TypedDict):
    source_v: Annotated[int, "Bot version to analyse performance for"]


@tool("run_performance_verification", "SATLUTION-style LLM performance analysis. Synthesises rating trends, win rates, and persistent weaknesses into a structured insight for Master.", {"source_v": int})
async def run_performance_verification(args):
    from evolution_core import _run_performance_verification
    source_v = args["source_v"]
    ratings = load_ratings()
    ui = _get_ui()
    output = await _run_performance_verification(source_v, ratings, ui)

    try:
        data = json.loads(output) if output else {}
    except json.JSONDecodeError:
        data = {"raw": output}

    result = {**data, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class GetH2HInput(TypedDict):
    bot_name: Annotated[str, "Bot name (e.g. claude_v14)"]
    opponent: Annotated[str, "Optional: specific opponent name. If omitted, returns all opponents."]


@tool("get_h2h", "Get head-to-head win/loss data for a bot. Shows per-opponent win rates — who this bot beats and loses to.", {"bot_name": str, "opponent": str})
async def get_h2h(args):
    bot_name = args["bot_name"]
    opponent = args.get("opponent")

    h2h_file = PROJECT_ROOT / "web" / "core" / "results" / "head_to_head.json"
    if not h2h_file.exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": "No H2H data yet", "bot_name": bot_name})}]}

    try:
        with open(h2h_file, "r") as f:
            h2h = json.load(f)
    except Exception:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Failed to read H2H data"})}]}

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
        wr = bot_wins / g if g > 0 else 0.5
        tag = "STRENGTH" if wr > 0.60 else ("WEAKNESS" if wr < 0.40 else "neutral")
        results[opp] = {"wins": bot_wins, "losses": opp_wins, "games": g, "win_rate": round(wr, 4), "tag": tag}

    if not results:
        return {"content": [{"type": "text", "text": json.dumps({"bot_name": bot_name, "opponents": {}, "message": "No H2H data found"})}]}

    sorted_results = dict(sorted(results.items(), key=lambda x: x[1]["win_rate"]))
    return {"content": [{"type": "text", "text": json.dumps({"bot_name": bot_name, "opponents": sorted_results}, indent=2, ensure_ascii=False)}]}


class GetBotStatsInput(TypedDict):
    bot_name: Annotated[str, "Bot name (e.g. claude_v14)"]


@tool("get_bot_stats", "Get per-bot stats: total wins, losses, games, win rate.", {"bot_name": str})
async def get_bot_stats(args):
    bot_name = args["bot_name"]

    bot_stats_file = PROJECT_ROOT / "web" / "core" / "results" / "bot_stats.json"
    if not bot_stats_file.exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": "No bot stats yet", "bot_name": bot_name})}]}

    try:
        with open(bot_stats_file, "r") as f:
            all_stats = json.load(f)
    except Exception:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Failed to read bot stats"})}]}

    bs = all_stats.get(bot_name)
    if not bs:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"No stats for {bot_name}"})}]}

    return {"content": [{"type": "text", "text": json.dumps({"bot_name": bot_name, **bs}, indent=2)}]}
