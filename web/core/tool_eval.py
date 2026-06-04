"""Pipeline tools: pre-commit evaluation and inline evaluation (battle-based)."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, TypedDict

from claude_agent_sdk import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
    load_ratings,
    CORE_DIR,
)
from glicko2 import Glicko2Player, update_rating_period

from tool_helpers import (
    _json_tool_result,
    _matching_checkpoint, _record_gate, _gate_payload, _state_blocked,
    _quality_gate_ok, _review_gate_ok, _critic_gate_ok,
    _select_precommit_opponents, _bot_main,
    PROJECT_ROOT,
)


# ──────────────────────────────────────────────
# Precommit Eval
# ──────────────────────────────────────────────

class RunPrecommitEvalInput(TypedDict):
    version: Annotated[int, "Bot version being evaluated before commit"]
    source_v: Annotated[int, "Parent bot version"]
    n_games: Annotated[int, "Mirror pairs per opponent, default 1"]


@tool("run_precommit_eval", "Run a minimal mirror-battle regression check before commit. Tests parent, current top opponents, and source H2H weaknesses; blocks obvious crashes or collapses.", {"version": int, "source_v": int, "n_games": int})
async def run_precommit_eval(args):
    v = args["version"]
    source_v = args["source_v"]
    n_games = max(1, int(args.get("n_games", 1) or 1))
    candidate_name = f"claude_v{v}"
    parent_name = f"claude_v{source_v}"
    candidate_main = _bot_main(candidate_name)
    blockers = []
    matchups = []

    ckpt = _matching_checkpoint(v, source_v)
    if not _quality_gate_ok(ckpt) or not _review_gate_ok(ckpt) or not _critic_gate_ok(ckpt):
        return _state_blocked(
            "run_precommit_eval requires passing quality, reviewer, and critic gates for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    if not candidate_main.exists():
        result = {
            "version": v,
            "source_v": source_v,
            "n_games": n_games,
            "passed": False,
            "blockers": [{"reason": "candidate_missing", "details": str(candidate_main)}],
            "opponents": [],
            "matchups": [],
        }
        gate_extra = {k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}}
        _record_gate(v, source_v, "precommit_eval", _gate_payload(v, source_v, False, **gate_extra), stage=None)
        return _json_tool_result(result)

    # compile/smoke already verified by quality gates (required by _quality_gate_ok above)

    opponents = _select_precommit_opponents(v, source_v)
    # Add crossover parent_b if applicable
    if ckpt and ckpt.get("parent2_v"):
        parent2_name = f"claude_v{ckpt['parent2_v']}"
        parent2_main = _bot_main(parent2_name)
        if parent2_main.exists() and not any(o["name"] == parent2_name for o in opponents):
            opponents.append({"name": parent2_name, "reason": "crossover_parent_b"})
    if not opponents:
        blockers.append({"reason": "no_opponents", "details": "No parent/top/H2H opponents with main.py found."})

    total_wins = 0
    total_losses = 0
    total_draws = 0
    _core = CORE_DIR if 'CORE_DIR' in dir() else Path(__file__).resolve().parent
    sys.path.insert(0, str(_core.resolve()))
    from engine.battle import mirror_battle

    # Defensive: ensure asyncio is available even if MCP server cached a stale module
    import asyncio as _asyncio

    for item in opponents:
        opponent = item["name"]
        opponent_main = _bot_main(opponent)
        matchup = {
            "opponent": opponent,
            "reason": item["reason"],
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "n_played": 0,
        }
        try:
            loop = _asyncio.get_running_loop()
            match_wins, draws, n_played, _ = await loop.run_in_executor(
                None,
                lambda: mirror_battle(
                    str(candidate_main),
                    str(opponent_main),
                    n_games=n_games,
                    verbose=False,
                    save_log=False,
                ),
            )
            matchup.update({
                "wins": int(match_wins[0]),
                "losses": int(match_wins[1]),
                "draws": int(draws),
                "n_played": int(n_played),
            })
            total_wins += matchup["wins"]
            total_losses += matchup["losses"]
            total_draws += matchup["draws"]
            if n_played < n_games:
                blockers.append({
                    "reason": "incomplete_or_timeout",
                    "opponent": opponent,
                    "details": f"Only {n_played}/{n_games} mirror pairs completed.",
                })
            if opponent == parent_name and matchup["wins"] < matchup["losses"]:
                blockers.append({
                    "reason": "lost_to_parent",
                    "opponent": opponent,
                    "details": f"{matchup['wins']}-{matchup['losses']}-{matchup['draws']}",
                })
        except Exception as exc:
            matchup["error"] = str(exc)[:500]
            blockers.append({
                "reason": "match_exception",
                "opponent": opponent,
                "details": str(exc)[:500],
            })
        matchups.append(matchup)

    if total_losses >= 3 and total_losses >= total_wins + 2:
        blockers.append({
            "reason": "aggregate_precommit_regression",
            "details": f"Aggregate mirror result {total_wins}-{total_losses}-{total_draws}.",
        })

    passed = len(blockers) == 0
    result = {
        "version": v,
        "source_v": source_v,
        "n_games": n_games,
        "opponents": opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "passed": passed,
        "blockers": blockers,
    }
    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "precommit_eval",
        _gate_payload(
            v,
            source_v,
            passed,
            **{k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}},
        ),
        stage="verified" if passed else None,
    )
    result["checkpoint_recorded"] = checkpoint_recorded
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Inline Eval
# ──────────────────────────────────────────────

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

    # Guard: refuse to run while daemon is active (read-modify-write race on ratings)
    from daemon_management import daemon_proc, _daemon_lock
    with _daemon_lock:
        _dp = daemon_proc
    if _dp is not None and _dp.poll() is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Daemon is running. Stop it first with stop_daemon to avoid ratings race condition."})}]}

    # Import battle engine
    _core = CORE_DIR if 'CORE_DIR' in dir() else Path(__file__).resolve().parent
    sys.path.insert(0, str(_core.resolve()))
    from engine.battle import mirror_battle

    ratings = load_ratings()
    active_bots = get_active_bots()
    opponents = [b for b in active_bots if b != bot_name]

    if bot_name not in ratings:
        ratings[bot_name] = Glicko2Player()

    results_summary = []
    all_results = []

    from evolution_core import RATINGS_FILE, H2H_FILE, BOT_STATS_FILE, MATCH_HISTORY_FILE, locked_file, pair_key
    h2h = {}
    if H2H_FILE.exists():
        try:
            with locked_file(H2H_FILE, "r") as f:
                h2h = json.load(f)
        except Exception:
            pass
    bot_stats_data = {}
    if BOT_STATS_FILE.exists():
        try:
            with locked_file(BOT_STATS_FILE, "r") as f:
                bot_stats_data = json.load(f)
        except Exception:
            pass

    for opp in opponents:
        if opp not in ratings:
            ratings[opp] = Glicko2Player()
        loop = asyncio.get_running_loop()
        match_wins, draws, n_played, _ = await loop.run_in_executor(
            None,
            lambda _b=str(_bot_main(bot_name)), _o=str(_bot_main(opp)): mirror_battle(
                _b, _o, n_games=n_games, verbose=False, save_log=False,
            ),
        )
        w_a, w_b = match_wins[0], match_wins[1]
        total = w_a + w_b + draws
        results_summary.append({"opponent": opp, "wins": w_a, "losses": w_b, "draws": draws})

        # Update H2H
        k = pair_key(bot_name, opp)
        h2h.setdefault(k, {"games": 0, "a_wins": 0, "b_wins": 0, "draws": 0})
        h2h[k]["games"] += total
        if bot_name < opp:
            h2h[k]["a_wins"] += w_a
            h2h[k]["b_wins"] += w_b
        else:
            h2h[k]["a_wins"] += w_b
            h2h[k]["b_wins"] += w_a
        h2h[k]["draws"] += draws

        # Update bot_stats
        for name, w, l in [(bot_name, w_a, w_b), (opp, w_b, w_a)]:
            if name not in bot_stats_data:
                bot_stats_data[name] = {"wins": 0, "losses": 0, "draws": 0, "games": 0}
            bot_stats_data[name]["wins"] += w
            bot_stats_data[name]["losses"] += l
            bot_stats_data[name]["draws"] += draws
            bot_stats_data[name]["games"] += total
            g = bot_stats_data[name]["games"]
            bot_stats_data[name]["win_rate"] = round(bot_stats_data[name]["wins"] / g, 4) if g > 0 else 0.0

        # Append to match_history
        try:
            from datetime import datetime
            summary = {
                "id": f"inline_v{v}_vs_{opp}",
                "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
                "bot0": bot_name,
                "bot1": opp,
                "bot0_wins": w_a,
                "bot1_wins": w_b,
                "draws": draws,
            }
            with locked_file(MATCH_HISTORY_FILE, "a") as f:
                f.write(json.dumps(summary) + "\n")
        except Exception:
            pass

        for _ in range(w_a):
            all_results.append((ratings[opp], 1.0))
        for _ in range(w_b):
            all_results.append((ratings[opp], 0.0))
        for _ in range(draws):
            all_results.append((ratings[opp], 0.5))

    if all_results:
        ratings[bot_name] = update_rating_period(ratings[bot_name], all_results)

    # Save updated ratings
    data = {name: p.to_dict() for name, p in ratings.items()}
    with locked_file(RATINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    # Save H2H with win_rate computed
    h2h_out = {}
    for k, h2h_entry in h2h.items():
        entry = dict(h2h_entry)
        g = entry.get("games", 0)
        entry["win_rate"] = round(entry.get("a_wins", 0) / g, 4) if g > 0 else 0.5
        h2h_out[k] = entry
    with locked_file(H2H_FILE, "w") as f:
        json.dump(h2h_out, f, indent=2)

    # Save bot_stats
    with locked_file(BOT_STATS_FILE, "w") as f:
        json.dump(bot_stats_data, f, indent=2)

    result = {
        "version": v,
        "opponents_played": len(opponents),
        "games_per_opponent": n_games,
        "results": results_summary,
        "updated_rating": {"r": round(ratings[bot_name].r, 1), "rd": round(ratings[bot_name].rd, 1)},
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
