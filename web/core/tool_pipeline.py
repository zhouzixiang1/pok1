"""Core evolution pipeline MCP tools.

Tools arranged by pipeline stage: Master → Workers → Quality Gates →
Review → Critic → Precommit Eval → Commit. Each tool maps directly
to an LLM agent call or a pipeline gate.
"""

import json
import shutil
import sys
from pathlib import Path
from typing import Annotated, TypedDict

from claude_agent_sdk import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
    get_logs_dir,
    load_ratings,
    verify_code,
    check_code_size,
    run_smoke_test,
    run_decision_test_details,
    parse_json_output,
    run_claude_query,
    git_commit_bot,
    clear_pipeline_checkpoint,
    MAX_ACTIVE_BOTS,
    _run_master_analysis,
    _execute_workers,
    _run_crossover,
    _run_critic,
)
from glicko2 import Glicko2Player, update_rating_period

from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _record_gate, _gate_payload, _state_blocked,
    _quality_gate_ok, _review_gate_ok, _critic_gate_ok,
    _validate_worker_boundaries, _select_precommit_opponents, _bot_main,
    PROJECT_ROOT,
)


# ──────────────────────────────────────────────
# Master Stage
# ──────────────────────────────────────────────

class RunMasterInput(TypedDict):
    source_v: Annotated[int, "Source bot version"]
    next_v: Annotated[int, "Target next version"]
    stagnation_info: Annotated[str, "Stagnation context (or 'No stagnation')"]
    match_analysis: Annotated[str, "Match analysis context from run_match_analysis (or '')"]
    performance_verification: Annotated[str, "Performance verification output from run_performance_verification (or '')"]


@tool("run_master", "Run Master Architect analysis to plan the next generation. Returns a task plan with worker assignments.", {"source_v": int, "next_v": int, "stagnation_info": str, "match_analysis": str, "performance_verification": str})
async def run_master(args):
    source_v = args["source_v"]
    next_v = args["next_v"]
    stagnation_info = args.get("stagnation_info", "No stagnation detected. Continue from latest version.")
    match_analysis = args.get("match_analysis", "")
    performance_verification = args.get("performance_verification", "")

    ui = _get_ui()
    data = await _run_master_analysis(
        source_v, next_v, stagnation_info, ui,
        match_analysis=match_analysis,
        performance_verification=performance_verification,
    )

    if data is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Master failed to produce a valid plan after 3 retries", "logs": ui.get_output()})}]}

    result = {"plan": data, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


# ──────────────────────────────────────────────
# Worker Stage
# ──────────────────────────────────────────────

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

    ckpt = _matching_checkpoint(next_v, source_v)
    if not ckpt:
        return _state_blocked(
            "execute_workers requires a matching checkpoint from prepare_next_gen.",
            next_v,
            source_v,
        )

    ui = _get_ui()
    success = await _execute_workers(
        tasks, worker_template, next_dir, next_v,
        [], ui, reviewer_feedback=reviewer_feedback,
        source_v=source_v,
    )

    boundary_errors = []
    if success:
        boundary_errors = _validate_worker_boundaries(tasks, source_v, next_v)
        if boundary_errors:
            success = False

    if success:
        from evolution_core import write_pipeline_checkpoint
        write_pipeline_checkpoint(next_v, source_v, "workers_done",
                                  master_plan=tasks, reviewer_feedback=reviewer_feedback)

    result = {
        "success": success,
        "boundary_errors": boundary_errors,
        "logs": ui.get_output(),
        "costs": ui.costs,
    }
    return _json_tool_result(result)


class RunQualityGatesInput(TypedDict):
    version: Annotated[int, "Bot version to test"]


@tool("run_quality_gates", "Run all quality gates on a bot: compile check, smoke test, decision tests, and file size check.", {"version": int})
async def run_quality_gates(args):
    v = args["version"]
    bot_dir = get_bot_dir(v)

    compile_errors = verify_code(bot_dir)
    smoke_errors = run_smoke_test(bot_dir)
    decision_detail = run_decision_test_details(bot_dir)
    decision_rate = decision_detail.get("pass_rate", 0.0)
    critical_failures = decision_detail.get("critical_failures", [])
    critical_ok = len(critical_failures) == 0
    total_lines, oversized = check_code_size(bot_dir)
    decision_ok = decision_rate >= 0.7 and critical_ok

    all_passed = (
        len(compile_errors) == 0
        and len(smoke_errors) == 0
        and decision_ok
        and len(oversized) == 0
    )

    result = {
        "version": v,
        "compile_ok": len(compile_errors) == 0,
        "compile_errors": compile_errors[:3] if compile_errors else [],
        "smoke_ok": len(smoke_errors) == 0,
        "smoke_errors": smoke_errors[:3] if smoke_errors else [],
        "decision_pass_rate": round(decision_rate, 2),
        "decision_ok": decision_ok,
        "critical_scenarios_passed": critical_ok,
        "critical_passed": decision_detail.get("critical_passed", 0),
        "critical_total": decision_detail.get("critical_total", 0),
        "critical_failures": critical_failures,
        "decision_failures": decision_detail.get("failures", []),
        "scenario_results": decision_detail.get("scenarios", []),
        "total_lines": total_lines,
        "oversized_files": {name: lines for name, lines in oversized} if oversized else {},
        "size_ok": len(oversized) == 0,
        "all_passed": all_passed,
    }

    _ckpt = _matching_checkpoint(v)
    if _ckpt:
        source_v = _ckpt["source_v"]
        gate = _gate_payload(
            v,
            source_v,
            all_passed,
            all_passed=all_passed,
            critical_scenarios_passed=critical_ok,
            decision_pass_rate=round(decision_rate, 4),
            critical_failures=critical_failures,
        )
        _record_gate(
            v,
            source_v,
            "quality",
            gate,
            stage="quality_passed" if all_passed else _ckpt.get("stage", "workers_done"),
        )
        result["checkpoint_recorded"] = True
        result["source_v"] = source_v
    else:
        result["checkpoint_recorded"] = False

    return _json_tool_result(result)


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

    # Write "prepared" checkpoint so a kill+restart shows "Workers not yet run → call execute_workers"
    from evolution_core import write_pipeline_checkpoint
    write_pipeline_checkpoint(next_v, source_v, "prepared")

    return {"content": [{"type": "text", "text": json.dumps({"prepared": True, "next_v": next_v, "source_v": source_v})}]}


# ──────────────────────────────────────────────
# Review Stage
# ──────────────────────────────────────────────

class RunReviewInput(TypedDict):
    version: Annotated[int, "Bot version being reviewed"]
    source_v: Annotated[int, "Parent bot version"]
    plan: Annotated[list, "Master's task plan"]


@tool("run_review", "Run Lead Code Reviewer on the bot changes. Returns approval decision with quality score.", {"version": int, "source_v": int, "plan": list})
async def run_review(args):
    v = args["version"]
    source_v = args["source_v"]
    plan = args["plan"]

    ckpt = _matching_checkpoint(v, source_v)
    if not _quality_gate_ok(ckpt):
        return _state_blocked(
            "run_review requires run_quality_gates all_passed=true and critical_scenarios_passed=true for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    prompts_dir = PROJECT_ROOT / "web" / "core" / "prompts"
    reviewer_prompt = (prompts_dir / "reviewer_prompt.md").read_text()
    reviewer_prompt = reviewer_prompt.replace("{master_plan}", json.dumps(plan, indent=2))
    reviewer_prompt = reviewer_prompt.replace("{version}", str(v))
    reviewer_prompt = reviewer_prompt.replace("{parent_version}", str(source_v))

    log_file = get_logs_dir(v) / "reviewer_io.txt"

    ui = _get_ui()
    output, _, _ = await run_claude_query(
        reviewer_prompt, [], ui, "LEAD CODE REVIEWER", log_file, tools=["Bash", "Read"]
    )
    data = parse_json_output(output)

    if data and "approved" in data:
        approved = bool(data["approved"])
        feedback = data.get("feedback", "")
        gate = _gate_payload(
            v,
            source_v,
            approved,
            approved=approved,
            quality_score=data.get("quality_score", 0),
            feedback=feedback,
            change_summary=data.get("change_summary", ""),
            risk_areas=data.get("risk_areas", []),
        )
        checkpoint_recorded = _record_gate(
            v,
            source_v,
            "review",
            gate,
            stage="reviewed" if approved else None,
            master_plan=plan,
            reviewer_feedback=feedback,
        )
        result = {
            "approved": approved,
            "quality_score": data.get("quality_score", 0),
            "change_summary": data.get("change_summary", ""),
            "risk_areas": data.get("risk_areas", []),
            "feedback": feedback,
            "checkpoint_recorded": checkpoint_recorded,
            "logs": ui.get_output(),
        }
    else:
        gate = _gate_payload(
            v,
            source_v,
            False,
            approved=False,
            error="Reviewer failed to produce valid JSON",
            raw_output=output[:500] if output else "",
        )
        checkpoint_recorded = _record_gate(
            v,
            source_v,
            "review",
            gate,
            stage=None,
            master_plan=plan,
            reviewer_feedback="Reviewer failed to produce valid JSON",
        )
        result = {
            "approved": False,
            "error": "Reviewer failed to produce valid JSON",
            "raw_output": output[:500] if output else "",
            "checkpoint_recorded": checkpoint_recorded,
            "logs": ui.get_output(),
        }

    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Critic Stage
# ──────────────────────────────────────────────

class RunCriticInput(TypedDict):
    version: Annotated[int, "Bot version being evaluated"]
    source_v: Annotated[int, "Parent bot version"]
    plan: Annotated[list, "Master's task plan (list of task dicts)"]
    reviewer_feedback: Annotated[str, "Reviewer feedback if available (or '')"]
    force_advance: Annotated[bool, "Set true when retries exhausted — advances checkpoint to critic_checked regardless of score so a kill+restart does not re-trigger the retry loop"]


@tool("run_critic", "Run Poker Strategy Critic on bot changes. Returns score 1-10 and strategic feedback. score ≥ 6 = approved.", {"version": int, "source_v": int, "plan": list, "reviewer_feedback": str, "force_advance": bool})
async def run_critic(args):
    v = args["version"]
    source_v = args["source_v"]
    plan = args["plan"]
    reviewer_feedback = args.get("reviewer_feedback", "")
    force_advance = args.get("force_advance", False)

    ckpt = _matching_checkpoint(v, source_v)
    if not _quality_gate_ok(ckpt) or not _review_gate_ok(ckpt):
        return _state_blocked(
            "run_critic requires passing quality gates and reviewer approval for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    master_plan_str = json.dumps(plan, indent=2)
    ui = _get_ui()
    data = await _run_critic(v, source_v, master_plan_str, ui)

    if not isinstance(data, dict):
        data = {}
    score = data.get("score", 0)
    try:
        score_num = float(score)
    except (TypeError, ValueError):
        score_num = 0.0
    raw_approved = data.get("approved", score_num >= 6)
    approved = bool(raw_approved) and score_num >= 6
    gate = _gate_payload(
        v,
        source_v,
        approved,
        approved=approved,
        raw_approved=raw_approved,
        score=score_num,
        feedback=data.get("feedback", ""),
        strategic_assessment=data.get("strategic_assessment", ""),
        local_optima_warning=data.get("local_optima_warning", False),
        force_advanced=force_advance and not approved,
    )
    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "critic",
        gate,
        stage="critic_checked" if approved or force_advance else None,
        master_plan=plan,
        reviewer_feedback=reviewer_feedback,
    )

    result = {
        **data,
        "approved": approved,
        "raw_approved": raw_approved,
        "score": score_num,
        "logs": ui.get_output(),
        "action": "approve" if approved else "retry_workers",
        "force_advanced": force_advance and not approved,
        "checkpoint_recorded": checkpoint_recorded,
    }
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Precommit Eval + Commit Stage
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

    compile_errors = verify_code(get_bot_dir(v))
    smoke_errors = run_smoke_test(get_bot_dir(v))
    if compile_errors:
        blockers.append({"reason": "compile_errors", "details": compile_errors[:3]})
    if smoke_errors:
        blockers.append({"reason": "smoke_errors_or_timeout", "details": smoke_errors[:3]})

    opponents = _select_precommit_opponents(v, source_v)
    if not opponents:
        blockers.append({"reason": "no_opponents", "details": "No parent/top/H2H opponents with main.py found."})

    total_wins = 0
    total_losses = 0
    total_draws = 0
    sys.path.insert(0, str((PROJECT_ROOT / "engine").resolve()))
    from battle import mirror_battle

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
            match_wins, draws, n_played, _ = mirror_battle(
                str(candidate_main),
                str(opponent_main),
                n_games=n_games,
                verbose=False,
                save_log=False,
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
    from evolution_core import RATINGS_FILE, locked_file
    data = {name: p.to_dict() for name, p in ratings.items()}
    with locked_file(RATINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    result = {
        "version": v,
        "opponents_played": len(opponents),
        "games_per_opponent": n_games,
        "results": results_summary,
        "updated_rating": {"r": round(ratings[bot_name].r, 1), "rd": round(ratings[bot_name].rd, 1)},
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


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

    ckpt = _matching_checkpoint(v, source_v)
    missing_gates = []
    failed_gates = []
    gate_results = {}
    if not ckpt:
        missing_gates.append("pipeline_checkpoint")
    else:
        gate_results = ckpt.get("gate_results", {}) or {}

        quality = gate_results.get("quality")
        if not quality:
            missing_gates.append("quality")
        else:
            if quality.get("all_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "all_passed is not true", "value": quality})
            if quality.get("critical_scenarios_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "critical_scenarios_passed is not true", "value": quality})

        review = gate_results.get("review")
        if not review:
            missing_gates.append("review")
        elif review.get("approved") is not True:
            failed_gates.append({"gate": "review", "reason": "reviewer did not approve", "value": review})

        critic = gate_results.get("critic")
        if not critic:
            missing_gates.append("critic")
        else:
            try:
                critic_score = float(critic.get("score", 0))
            except (TypeError, ValueError):
                critic_score = 0.0
            if critic.get("approved") is not True or critic_score < 6:
                failed_gates.append({
                    "gate": "critic",
                    "reason": "critic score must be >= 6 and approved must be true",
                    "value": critic,
                })

        precommit = gate_results.get("precommit_eval")
        if not precommit:
            missing_gates.append("precommit_eval")
        elif precommit.get("passed") is not True:
            failed_gates.append({"gate": "precommit_eval", "reason": "precommit eval did not pass", "value": precommit})

    if missing_gates or failed_gates:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: gate ledger incomplete or failed.",
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": ckpt.get("stage") if ckpt else None,
            "missing_gates": missing_gates,
            "failed_gates": failed_gates,
            "gate_results": gate_results,
        })

    # Guard: compile check
    compile_errors = verify_code(bot_dir)
    if compile_errors:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: compile errors present. Run run_quality_gates() first and fix errors.",
            "compile_errors": compile_errors[:3],
        })

    smoke_errors = run_smoke_test(bot_dir)
    if smoke_errors:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: smoke test failed. Run run_quality_gates() first and fix runtime errors.",
            "smoke_errors": smoke_errors[:3],
        })

    decision_detail = run_decision_test_details(bot_dir)
    decision_rate = decision_detail.get("pass_rate", 0.0)
    critical_failures = decision_detail.get("critical_failures", [])
    if decision_rate < 0.7 or critical_failures:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: decision tests failed. Fix catastrophic blunders first.",
            "decision_pass_rate": round(decision_rate, 2),
            "critical_failures": critical_failures,
            "decision_failures": decision_detail.get("failures", []),
        })

    _, oversized = check_code_size(bot_dir)
    if oversized:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: code size gate failed.",
            "oversized_files": {name: lines for name, lines in oversized},
        })

    # Guard: reviewer approval required
    if not review_approved:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: review_approved=false. Call run_review() first; only pass review_approved=true if it returns approved:true.",
        })

    (bot_dir / ".completed").touch()

    ratings = load_ratings()
    p = ratings.get(f"claude_v{v}")
    h2h_wr = None
    try:
        from tool_helpers import compute_h2h_avg_winrate, _load_h2h_data
        h2h_wr = compute_h2h_avg_winrate(f"claude_v{v}", _load_h2h_data())
    except Exception:
        pass
    wr_str = f" h2h_avg_wr={h2h_wr:.2%}" if h2h_wr is not None else ""
    rating_info = f"rating: r={p.r:.1f} rd={p.rd:.1f}{wr_str}" if p else ""

    git_commit_bot(v, source_v, strategy, rating_info=rating_info)
    clear_pipeline_checkpoint()

    try:
        from server.state import app_state
        app_state.set_generation(v, v + 1)
    except Exception:
        pass

    # Signal daemon to pick up the new bot
    reap_signal = Path(__file__).parent / "results" / ".reap_signal"
    reap_signal.touch()

    result = {"committed": True, "version": v, "source_v": source_v}
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        result["needs_reap"] = True
        result["pool_size"] = len(active_bots)
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Crossover
# ──────────────────────────────────────────────

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
    success = await _run_crossover(parent_a, parent_b, target_v, ui)

    result = {"success": success, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
