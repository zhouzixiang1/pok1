"""MCP tools for the Evolution Orchestrator Agent.

Wraps existing evolution_core functions as in-process MCP tools using
the claude_agent_sdk @tool decorator. The Orchestrator LLM calls these
tools to control the evolution pipeline.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated, TypedDict

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_agent_sdk import tool, create_sdk_mcp_server

from evolution_core import (
    BaseUI,
    get_active_bots,
    get_bot_dir,
    get_logs_dir,
    load_ratings,
    load_daemon_stats,
    verify_code,
    check_code_size,
    run_smoke_test,
    run_decision_tests,
    trim_experience_pool,
    git_has_tag,
    git_get_parent,
    git_commit_bot,
    git_ensure_clean,
    start_daemon,
    stop_daemon,
    wait_for_daemon_eval,
    _run_master_analysis,
    _execute_workers,
    _run_crossover,
    _analyze_recent_matches,
    _consolidate_experience_pool,
    _analyze_stagnation,
    _run_critic,
    _run_performance_verification,
    summarize_replay_for_analysis,
    parse_json_output,
)
from glicko2 import Glicko2Player, update_rating_period


# ──────────────────────────────────────────────
# UI Injection — Dashboard Integration
# ──────────────────────────────────────────────

_injected_ui = None


def inject_ui(ui):
    """Inject a real WebUI instance so tool events broadcast to Dashboard via SSE."""
    global _injected_ui
    _injected_ui = ui


def _get_ui():
    """Get UI instance: injected WebUI (Dashboard mode) or silent ToolUI (CLI mode)."""
    return _injected_ui if _injected_ui else ToolUI()


# ──────────────────────────────────────────────
# Logging UI Adapter (CLI fallback)
# ──────────────────────────────────────────────

class ToolUI(BaseUI):
    """Silent UI adapter for CLI mode — captures output for tool results only."""

    def __init__(self):
        self.messages = []
        self.costs = []

    def log_history(self, msg, status="info"):
        self.messages.append(f"[{status}] {msg}")

    def set_status(self, msg, is_working=False):
        self.messages.append(f"[status] {msg}")

    def log_io(self, msg, stream_type="default"):
        pass

    def clear_io(self):
        pass

    def update_eval_table(self, ratings, active_bots):
        pass

    def update_daemon_status(self, stats, ratings):
        pass

    def set_header(self, msg):
        pass

    def update_cost(self, role, cost_usd, usage):
        if cost_usd is not None:
            self.costs.append({"role": role, "cost_usd": cost_usd})

    def update_metrics(self, metrics):
        pass

    def get_output(self):
        return "\n".join(self.messages[-20:])


def _ratings_summary(ratings, n=10):
    """Get top N bots as a compact summary."""
    sorted_bots = sorted(
        [(name, p) for name, p in ratings.items()],
        key=lambda x: x[1].r, reverse=True,
    )[:n]
    return [
        {"name": name, "r": round(p.r, 1), "rd": round(p.rd, 1)}
        for name, p in sorted_bots
    ]


# ──────────────────────────────────────────────
# Tool Definitions
# ──────────────────────────────────────────────

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

    # Current bot rating reliability (rd <= 40 means enough matches)
    cur_p = ratings.get(f"claude_v{current_v}")
    current_bot_rd = round(cur_p.rd, 1) if cur_p else None
    rating_reliable = bool(cur_p and cur_p.rd <= 40)

    # Recent worker failures for context
    from evolution_core import _load_recent_failures
    recent_failures = _load_recent_failures(3)

    result = {
        "current_v": current_v,
        "next_v": current_v + 1,
        "active_bots_count": len(active_bots),
        "top_ratings": _ratings_summary(ratings),
        "daemon_periods": daemon_stats.get("total_periods", 0),
        "incomplete_next_v": incomplete_next_v,
        "current_bot_rd": current_bot_rd,
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
    output = await _analyze_recent_matches(source_v, ui, is_text_ui=False)
    result = {
        "analysis": output,
        "logs": ui.get_output(),
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class RunMasterInput(TypedDict):
    source_v: Annotated[int, "Source bot version"]
    next_v: Annotated[int, "Target next version"]
    stagnation_info: Annotated[str, "Stagnation context (or 'No stagnation')"]
    match_analysis: Annotated[str, "Match analysis context (or '')"]


@tool("run_master", "Run Master Architect analysis to plan the next generation. Returns a task plan with worker assignments.", {"source_v": int, "next_v": int, "stagnation_info": str, "match_analysis": str})
async def run_master(args):
    source_v = args["source_v"]
    next_v = args["next_v"]
    stagnation_info = args.get("stagnation_info", "No stagnation detected. Continue from latest version.")
    match_analysis = args.get("match_analysis", "")

    ui = _get_ui()
    data = await _run_master_analysis(
        source_v, next_v, stagnation_info, ui, is_text_ui=False, match_analysis=match_analysis
    )

    if data is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Master failed to produce a valid plan after 3 retries", "logs": ui.get_output()})}]}

    result = {"plan": data, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class ExecuteWorkersInput(TypedDict):
    tasks: Annotated[list, "List of worker task dicts from Master plan"]
    next_v: Annotated[int, "Target bot version"]
    source_v: Annotated[int, "Source bot version"]
    reviewer_feedback: Annotated[str, "Previous reviewer feedback (or '')"]


@tool("execute_workers", "Execute worker tasks to modify bot code. Each task has worker_id, role, target_files, worker_prompt.", {"tasks": list, "next_v": int, "source_v": int, "reviewer_feedback": str})
async def execute_workers(args):
    tasks = args["tasks"]
    next_v = args["next_v"]
    source_v = args["source_v"]
    reviewer_feedback = args.get("reviewer_feedback", "")

    next_dir = get_bot_dir(next_v)
    prompts_dir = PROJECT_ROOT / "web" / "core" / "prompts"
    worker_template = (prompts_dir / "worker_prompt.md").read_text()

    ui = _get_ui()
    success = await _execute_workers(
        tasks, worker_template, next_dir, next_v,
        [], ui, is_text_ui=False, reviewer_feedback=reviewer_feedback,
        source_v=source_v,
    )

    result = {"success": success, "logs": ui.get_output(), "costs": ui.costs}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class RunQualityGatesInput(TypedDict):
    version: Annotated[int, "Bot version to test"]


@tool("run_quality_gates", "Run all quality gates on a bot: compile check, smoke test, decision tests, and file size check.", {"version": int})
async def run_quality_gates(args):
    v = args["version"]
    bot_dir = get_bot_dir(v)

    compile_errors = verify_code(bot_dir)
    smoke_errors = run_smoke_test(bot_dir)
    decision_rate = run_decision_tests(bot_dir)
    total_lines, oversized = check_code_size(bot_dir)

    result = {
        "version": v,
        "compile_ok": len(compile_errors) == 0,
        "compile_errors": compile_errors[:3] if compile_errors else [],
        "smoke_ok": len(smoke_errors) == 0,
        "smoke_errors": smoke_errors[:3] if smoke_errors else [],
        "decision_pass_rate": round(decision_rate, 2),
        "decision_ok": decision_rate >= 0.7,
        "total_lines": total_lines,
        "oversized_files": {name: lines for name, lines in oversized} if oversized else {},
        "size_ok": len(oversized) == 0,
        "all_passed": (
            len(compile_errors) == 0
            and len(smoke_errors) == 0
            and decision_rate >= 0.7
            and len(oversized) == 0
        ),
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class RunReviewInput(TypedDict):
    version: Annotated[int, "Bot version being reviewed"]
    source_v: Annotated[int, "Parent bot version"]
    plan: Annotated[list, "Master's task plan"]


@tool("run_review", "Run Lead Code Reviewer on the bot changes. Returns approval decision with quality score.", {"version": int, "source_v": int, "plan": list})
async def run_review(args):
    v = args["version"]
    source_v = args["source_v"]
    plan = args["plan"]

    prompts_dir = PROJECT_ROOT / "web" / "core" / "prompts"
    reviewer_prompt = (prompts_dir / "reviewer_prompt.md").read_text()
    reviewer_prompt = reviewer_prompt.replace("{master_plan}", json.dumps(plan, indent=2))
    reviewer_prompt = reviewer_prompt.replace("{version}", str(v))
    reviewer_prompt = reviewer_prompt.replace("{parent_version}", str(source_v))

    log_file = get_logs_dir(v) / "reviewer_io.txt"

    from evolution_core import run_claude_query
    ui = _get_ui()
    output, _, _ = await run_claude_query(
        reviewer_prompt, [], ui, "LEAD CODE REVIEWER", log_file, is_text_ui=False
    )
    data = parse_json_output(output)

    if data and "approved" in data:
        result = {
            "approved": data["approved"],
            "quality_score": data.get("quality_score", 0),
            "change_summary": data.get("change_summary", ""),
            "risk_areas": data.get("risk_areas", []),
            "feedback": data.get("feedback", ""),
            "logs": ui.get_output(),
        }
    else:
        result = {
            "approved": False,
            "error": "Reviewer failed to produce valid JSON",
            "raw_output": output[:500] if output else "",
            "logs": ui.get_output(),
        }

    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class RunCriticInput(TypedDict):
    version: Annotated[int, "Bot version being evaluated"]
    source_v: Annotated[int, "Parent bot version"]
    plan: Annotated[list, "Master's task plan (list of task dicts)"]
    reviewer_feedback: Annotated[str, "Reviewer feedback if available (or '')"]


@tool("run_critic", "Run Poker Strategy Critic on bot changes. Returns score 1-10 and strategic feedback. score ≥ 6 = approved.", {"version": int, "source_v": int, "plan": list, "reviewer_feedback": str})
async def run_critic(args):
    v = args["version"]
    source_v = args["source_v"]
    plan = args["plan"]
    reviewer_feedback = args.get("reviewer_feedback", "")

    master_plan_str = json.dumps(plan, indent=2)
    ui = _get_ui()
    data = await _run_critic(v, source_v, master_plan_str, ui, is_text_ui=False)

    result = {
        **data,
        "logs": ui.get_output(),
        "action": "approve" if data.get("approved", True) else "retry_workers",
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class RunPerformanceVerificationInput(TypedDict):
    source_v: Annotated[int, "Bot version to analyse performance for"]


@tool("run_performance_verification", "SATLUTION-style LLM performance analysis. Synthesises rating trends, win rates, and persistent weaknesses into a structured insight for Master.", {"source_v": int})
async def run_performance_verification(args):
    source_v = args["source_v"]
    ratings = load_ratings()
    ui = _get_ui()
    output = await _run_performance_verification(source_v, ratings, ui, is_text_ui=False)

    try:
        data = json.loads(output) if output else {}
    except json.JSONDecodeError:
        data = {"raw": output}

    result = {**data, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class RunCrossoverInput(TypedDict):
    parent_a: Annotated[int, "First parent version"]
    parent_b: Annotated[int, "Second parent version"]
    target_v: Annotated[int, "Target child version"]


@tool("run_crossover", "Run crossover between two elite bots to create a child bot.", {"parent_a": int, "parent_b": int, "target_v": int})
async def run_crossover(args):
    parent_a = args["parent_a"]
    parent_b = args["parent_b"]
    target_v = args["target_v"]

    # Prepare target directory from parent A
    target_dir = get_bot_dir(target_v)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(get_bot_dir(parent_a), target_dir, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (target_dir / ".completed").unlink(missing_ok=True)

    ui = _get_ui()
    success = await _run_crossover(parent_a, parent_b, target_v, ui, is_text_ui=False)

    result = {"success": success, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class PrepareNextGenInput(TypedDict):
    source_v: Annotated[int, "Source bot version to copy from"]
    next_v: Annotated[int, "Target version"]


@tool("prepare_next_gen", "Prepare the next generation directory by copying from source bot.", {"source_v": int, "next_v": int})
async def prepare_next_gen(args):
    source_v = args["source_v"]
    next_v = args["next_v"]

    source_dir = get_bot_dir(source_v)
    next_dir = get_bot_dir(next_v)

    if not source_dir.exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": f"Source bot v{source_v} not found"})}]}

    if next_dir.exists():
        shutil.rmtree(next_dir)
    shutil.copytree(source_dir, next_dir, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (next_dir / ".completed").unlink(missing_ok=True)

    return {"content": [{"type": "text", "text": json.dumps({"prepared": True, "next_v": next_v, "source_v": source_v})}]}


class CommitBotInput(TypedDict):
    version: Annotated[int, "Bot version to commit"]
    source_v: Annotated[int, "Parent version"]
    strategy: Annotated[str, "Strategy description"]
    review_approved: Annotated[bool, "Must be true — confirms run_review() returned approved:true"]


@tool("commit_bot", "Commit a bot generation with git commit and tag. review_approved must be true (set after run_review returns approved:true).", {"version": int, "source_v": int, "strategy": str, "review_approved": bool})
async def commit_bot(args):
    v = args["version"]
    source_v = args["source_v"]
    strategy = args["strategy"]
    review_approved = args.get("review_approved", False)

    bot_dir = get_bot_dir(v)

    # Guard: compile check
    compile_errors = verify_code(bot_dir)
    if compile_errors:
        return {"content": [{"type": "text", "text": json.dumps({
            "error": "COMMIT BLOCKED: compile errors present. Run run_quality_gates() first and fix errors.",
            "compile_errors": compile_errors[:3],
        })}]}

    # Guard: decision tests
    decision_rate = run_decision_tests(bot_dir)
    if decision_rate < 0.7:
        return {"content": [{"type": "text", "text": json.dumps({
            "error": f"COMMIT BLOCKED: decision test pass rate {decision_rate:.0%} < 70%. Fix catastrophic blunders first.",
            "decision_pass_rate": round(decision_rate, 2),
        })}]}

    # Guard: reviewer approval required
    if not review_approved:
        return {"content": [{"type": "text", "text": json.dumps({
            "error": "COMMIT BLOCKED: review_approved=false. Call run_review() first; only pass review_approved=true if it returns approved:true.",
        })}]}

    (bot_dir / ".completed").touch()

    ratings = load_ratings()
    p = ratings.get(f"claude_v{v}")
    rating_info = f"rating: r={p.r:.1f} rd={p.rd:.1f}" if p else ""

    git_commit_bot(v, source_v, strategy, rating_info=rating_info)

    return {"content": [{"type": "text", "text": json.dumps({"committed": True, "version": v, "source_v": source_v})}]}


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
    min_matches: Annotated[int, "Minimum matches required (default 20)"]
    max_rd: Annotated[int, "Maximum rating deviation for confidence (default 40)"]


@tool("wait_for_eval", "Wait for the daemon to evaluate a bot (enough matches + low RD). Returns whether eval completed.", {"version": int, "timeout": int, "min_matches": int, "max_rd": int})
async def wait_for_eval(args):
    v = args["version"]
    timeout = args.get("timeout", 600)
    min_matches = args.get("min_matches", 20)
    max_rd = args.get("max_rd", 40)
    bot_name = f"claude_v{v}"

    success = await wait_for_daemon_eval(bot_name, timeout=timeout, min_matches=min_matches, max_rd=max_rd)
    ratings = load_ratings()
    p = ratings.get(bot_name)

    result = {
        "version": v,
        "eval_completed": success,
        "current_rating": {"r": round(p.r, 1), "rd": round(p.rd, 1)} if p else None,
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class RunInlineEvalInput(TypedDict):
    version: Annotated[int, "Bot version to evaluate"]
    n_games: Annotated[int, "Number of games per opponent (default 5)"]


@tool("run_inline_eval", "Run inline evaluation: battle the bot against all active opponents and update Glicko-2 ratings. Use when daemon is not running.", {"version": int, "n_games": int})
async def run_inline_eval(args):
    v = args["version"]
    n_games = args.get("n_games", 5)
    bot_name = f"claude_v{v}"
    bot_dir = get_bot_dir(v)

    if not (bot_dir / "main.py").exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": f"Bot v{v} main.py not found"})}]}

    # Import battle engine
    sys.path.insert(0, str((PROJECT_ROOT / "engine").resolve()))
    from battle import mirror_battle

    ratings = load_ratings()
    active_bots = get_active_bots()
    opponents = [b for b in active_bots if b != bot_name]

    if bot_name not in ratings:
        ratings[bot_name] = Glicko2Player()

    results_summary = []
    all_results = []

    for opp in opponents:
        if opp not in ratings:
            ratings[opp] = Glicko2Player()
        match_wins, draws, n_played, _ = mirror_battle(
            str(PROJECT_ROOT / "bots" / bot_name / "main.py"),
            str(PROJECT_ROOT / "bots" / opp / "main.py"),
            n_games=n_games, verbose=False, save_log=False
        )
        w_a, w_b = match_wins[0], match_wins[1]
        results_summary.append({"opponent": opp, "wins": w_a, "losses": w_b, "draws": draws})
        for _ in range(w_a):
            all_results.append((ratings[opp], 1.0))
        for _ in range(w_b):
            all_results.append((ratings[opp], 0.0))
        for _ in range(draws):
            all_results.append((ratings[opp], 0.5))

    if all_results:
        ratings[bot_name] = update_rating_period(ratings[bot_name], all_results)

    # Save updated ratings
    from evolution_core import RATINGS_FILE
    import fcntl
    data = {name: p.to_dict() for name, p in ratings.items()}
    with open(RATINGS_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)

    result = {
        "version": v,
        "opponents_played": len(opponents),
        "games_per_opponent": n_games,
        "results": results_summary,
        "updated_rating": {"r": round(ratings[bot_name].r, 1), "rd": round(ratings[bot_name].rd, 1)},
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


class ReapWeakestInput(TypedDict):
    pass


@tool("reap_weakest", "Check if bot pool exceeds 30 and cull the weakest bot by conservative rating.", {})
async def reap_weakest(args):
    active_bots = get_active_bots()
    if len(active_bots) <= 30:
        return {"content": [{"type": "text", "text": json.dumps({"reaped": False, "pool_size": len(active_bots)})}]}

    ratings = load_ratings()
    active_ratings = [(b, ratings.get(b, Glicko2Player())) for b in active_bots]
    active_ratings.sort(key=lambda x: x[1].r - 2 * x[1].rd)
    weakest = active_ratings[0]

    graveyard = PROJECT_ROOT / "bots" / "graveyard"
    graveyard.mkdir(exist_ok=True)
    shutil.move(PROJECT_ROOT / "bots" / weakest[0], graveyard / weakest[0])

    return {"content": [{"type": "text", "text": json.dumps({
        "reaped": True,
        "culled": weakest[0],
        "rating": {"r": round(weakest[1].r, 1), "rd": round(weakest[1].rd, 1)},
        "remaining": len(active_bots) - 1,
    })}]}


class TrimExperienceInput(TypedDict):
    pass


@tool("trim_experience", "Trim the experience pool to keep only the most recent entries.", {})
async def trim_experience(args):
    trim_experience_pool(max_entries=8)
    return {"content": [{"type": "text", "text": json.dumps({"trimmed": True})}]}


class ConsolidateExperienceInput(TypedDict):
    pass


@tool("consolidate_experience", "Use LLM to consolidate and deduplicate the experience pool.", {})
async def consolidate_experience(args):
    ui = _get_ui()
    await _consolidate_experience_pool(ui, is_text_ui=False)
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
    result = await _analyze_stagnation(source_v, active_bots_names, ratings, ui, is_text_ui=False)

    return {"content": [{"type": "text", "text": json.dumps({
        "analysis": result,
        "logs": ui.get_output(),
    }, indent=2, ensure_ascii=False)}]}


# ──────────────────────────────────────────────
# Register MCP Server
# ──────────────────────────────────────────────

all_tools = [
    get_status,
    get_bot_info,
    get_match_history,
    run_match_analysis,
    run_master,
    execute_workers,
    run_quality_gates,
    run_review,
    run_crossover,
    prepare_next_gen,
    commit_bot,
    start_eval_daemon,
    stop_eval_daemon,
    wait_for_eval,
    run_inline_eval,
    reap_weakest,
    trim_experience,
    consolidate_experience,
    analyze_stagnation,
]

evolution_server = create_sdk_mcp_server(
    name="evolution",
    version="1.0.0",
    tools=all_tools,
)
