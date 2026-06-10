"""Non-pipeline MCP tools: status queries, daemon control, bot management, and analysis.

Most tools query data, manage the daemon, and handle bot lifecycle operations.
The `diagnose_environment` tool is the exception — it calls LLM for one-shot analysis.
"""

import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

log = logging.getLogger("pok.tools")
from typing import Annotated, TypedDict

from claude_agent_sdk import tool

from evolution_core import (
    MAX_ACTIVE_BOTS,
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
    read_pipeline_checkpoint,
    find_current_v,
    _analyze_recent_matches,
    _analyze_stagnation,
    RATINGS_FILE, BOT_STATS_FILE, H2H_FILE, MATCH_HISTORY_FILE, REPLAY_DIR,
    RESULTS_DIR,
    locked_file,
)
from glicko2 import Glicko2Player
from tool_helpers import load_h2h_avg_winrates

from tool_helpers import (
    _get_ui, _ratings_summary, _json_tool_result, _bot_main,
    PROJECT_ROOT,
)
from evolution_infra import count_lines
from system_log import log_system_event




class GetStatusInput(TypedDict):
    pass


@tool("get_status", "Get the current evolution system status: latest bot version, top ratings, active bot count, and daemon status.", {})
async def get_status(args):
    """Get full system status."""
    active_bots = get_active_bots()

    current_v = find_current_v()

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
    if BOT_STATS_FILE.exists():
        try:
            with locked_file(BOT_STATS_FILE, "r") as f:
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

    # Code size info — use parent as source_dir for adaptive limits
    if bot_dir.exists():
        py_files = list(bot_dir.glob("*.py"))
        result["files"] = [f.name for f in py_files]
        result["total_lines"] = sum(count_lines(f) for f in py_files)
        source_dir = get_bot_dir(parent) if parent else None
        _, oversized = check_code_size(bot_dir, source_dir=source_dir)
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

    history_file = MATCH_HISTORY_FILE
    if not history_file.exists():
        return {"content": [{"type": "text", "text": json.dumps({"matches": []})}]}

    entries = []
    with locked_file(history_file, "r") as f:
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
    workers = args.get("workers", max(1, int(os.cpu_count() * 28 / 32)))
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
    if BOT_STATS_FILE.exists():
        try:
            with locked_file(BOT_STATS_FILE, "r") as f:
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

    h2h_file = H2H_FILE
    if not h2h_file.exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": "No H2H data yet", "bot_name": bot_name})}]}

    try:
        with locked_file(h2h_file, "r") as f:
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

    bot_stats_file = BOT_STATS_FILE
    if not bot_stats_file.exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": "No bot stats yet", "bot_name": bot_name})}]}

    try:
        with locked_file(bot_stats_file, "r") as f:
            all_stats = json.load(f)
    except Exception:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Failed to read bot stats"})}]}

    bs = all_stats.get(bot_name)
    if not bs:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"No stats for {bot_name}"})}]}

    return {"content": [{"type": "text", "text": json.dumps({"bot_name": bot_name, **bs}, indent=2)}]}


# ──────────────────────────────────────────────
# Startup Diagnosis
# ──────────────────────────────────────────────

@tool("diagnose_environment", "Analyze environment state and recommend cleanup before starting evolution. Call this when the context reports anomalies (incomplete bots, stale checkpoints, session residue).", {})
async def diagnose_environment(args):
    """One-shot LLM analysis of the environment before starting evolution."""
    ui = _get_ui()
    current_v = find_current_v()
    active_bots = get_active_bots()
    ratings = load_ratings()

    # Collect environment snapshot
    snapshot_lines = [
        f"Current highest completed bot: v{current_v}",
        f"Active bots: {len(active_bots)}",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    # Incomplete bot directories
    incomplete = []
    for d in sorted(PROJECT_ROOT.joinpath("bots").glob("claude_v*")):
        v_num = d.name.replace("claude_v", "")
        try:
            v_int = int(v_num)
        except ValueError:
            continue
        if not d.joinpath(".completed").exists() and not git_has_tag(v_int):
            incomplete.append(v_int)

    if incomplete:
        snapshot_lines.append(f"INCOMPLETE bots (no .completed, no tag): {incomplete}")
    else:
        snapshot_lines.append("No incomplete bot directories.")

    # Pipeline checkpoint
    checkpoint = read_pipeline_checkpoint()
    if checkpoint:
        snapshot_lines.append(
            f"PIPELINE CHECKPOINT: v{checkpoint['next_v']} (from v{checkpoint['source_v']}) "
            f"at stage='{checkpoint.get('stage', 'unknown')}'"
        )
    else:
        snapshot_lines.append("No pipeline checkpoint (clean state).")

    # Session residue
    session_file = PROJECT_ROOT / "web" / "core" / "results" / "orchestrator_session.json"
    if session_file.exists():
        snapshot_lines.append("WARNING: orchestrator_session.json exists (previous cycle was interrupted).")
    else:
        snapshot_lines.append("No session residue.")

    # Worker failures (last 24h)
    failures_file = PROJECT_ROOT / "web" / "core" / "results" / "worker_failures.jsonl"
    recent_failures = []
    if failures_file.exists():
        cutoff = time.time() - 86400
        from evolution_infra import locked_file as _locked_file
        with _locked_file(failures_file, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("timestamp", 0) > cutoff:
                        recent_failures.append(entry)
                except (json.JSONDecodeError, ValueError):
                    pass
    if recent_failures:
        snapshot_lines.append(f"Worker failures in last 24h: {len(recent_failures)}")
    else:
        snapshot_lines.append("No recent worker failures.")

    # Rating summary for top bots
    sorted_bots = sorted(
        [(name, p) for name, p in ratings.items() if name.startswith("claude_v")],
        key=lambda x: x[1].r, reverse=True,
    )[:10]
    if sorted_bots:
        snapshot_lines.append("")
        snapshot_lines.append("Top 10 rated bots:")
        for name, p in sorted_bots:
            v_str = name.replace("claude_v", "")
            tag = "✓" if git_has_tag(int(v_str)) else "✗"
            snapshot_lines.append(f"  {name}: r={p.r:.0f} rd={p.rd:.0f} {tag}")

    # Daemon status
    daemon_running = False
    try:
        from daemon_management import daemon_proc as dp
        daemon_running = dp is not None and dp.poll() is None
    except Exception:
        pass
    snapshot_lines.append(f"\nDaemon running: {daemon_running}")

    # Ask LLM for analysis
    prompt = (
        "You are an environment diagnostician for a poker bot evolution system.\n"
        "Analyze the following environment snapshot and return a JSON response:\n\n"
        f"{chr(10).join(snapshot_lines)}\n\n"
        "Return JSON with:\n"
        '- "clean": true/false — whether the environment is ready for evolution\n'
        '- "issues": list of strings describing problems found\n'
        '- "actions": list of recommended actions (e.g. "call cleanup_incomplete", "call abandon_generation")\n'
        '- "summary": one-line summary\n'
        "Only respond with valid JSON, no other text."
    )

    if ui:
        ui.log_history("[diagnose_environment] Running LLM analysis...", "info")

    from evolution_infra import run_claude_query, RESULTS_DIR
    log_path = str(RESULTS_DIR / "logs" / "diagnostician_io.txt")
    response_text, cost, _ = await run_claude_query(
        prompt=prompt,
        context_files=[],
        ui=ui,
        role_name="DIAGNOSTICIAN",
        log_file_path=log_path,
        tools=[],
    )

    if ui:
        ui.log_history(f"[diagnose_environment] Analysis complete (cost: ${cost:.3f})", "info")

    return {"content": [{"type": "text", "text": response_text.strip()}]}

# ──────────────────────────────────────────────
# Re-exports from extracted module
# ──────────────────────────────────────────────
from tool_bot_management import (  # noqa: F401
    reap_weakest, cleanup_incomplete, abandon_generation,
    trim_experience, seed_initial_bots_tool, consolidate_experience,
)
