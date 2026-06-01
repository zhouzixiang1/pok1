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
    git_has_tag,
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


def _record_quality_failure(gen, worker_id, role, error):
    """Record a quality gate rejection (reviewer/critic) to worker_failures.jsonl."""
    from evolution_core import WORKER_FAILURES_FILE, locked_file
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error}
    with locked_file(WORKER_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ──────────────────────────────────────────────
# Master Stage
# ──────────────────────────────────────────────

_TUNER_STRUCTURAL_PATTERNS = [
    "add parameter", "add a parameter", "function signature",
    "add function", "new function", "add method",
    "add class", "new class",
    "add import", "new import",
    "before the clamp", "after the existing",
]


def _validate_master_plan(plan):
    """Validate master plan constraints before dispatching workers."""
    errors = []
    tasks = plan.get("tasks", [])
    if len(tasks) > 3:
        errors.append(f"Too many tasks: {len(tasks)} > 3")
    for i, task in enumerate(tasks):
        targets = task.get("target_files", [])
        if len(targets) > 3:
            errors.append(f"Task {i}: too many target_files ({len(targets)} > 3)")
        prompt = task.get("worker_prompt", "")
        if len(prompt) > 3000:
            errors.append(f"Task {i}: worker_prompt too long ({len(prompt)} > 3000 chars)")
        role = str(task.get("role", "")).lower()
        if "hyperparameter" in role or "tuner" in role:
            prompt_lower = prompt.lower()
            for kw in _TUNER_STRUCTURAL_PATTERNS:
                if kw in prompt_lower:
                    errors.append(
                        f"Task {i} boundary warning: Hyperparameter Tuner prompt contains structural instruction "
                        f"'{kw}' — Tuner should only change numeric constants. "
                        f"Either rephrase the prompt to only specify constant changes, or reassign this task "
                        f"as Algorithmic Logic Architect."
                    )
                    break
    return errors

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

    plan_errors = _validate_master_plan(data)
    if plan_errors:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Master plan validation failed", "validation_errors": plan_errors, "plan": data, "logs": ui.get_output()})}]}

    # Persist master plan to checkpoint so it survives crashes between master and workers
    from evolution_infra import write_pipeline_checkpoint
    write_pipeline_checkpoint(next_v, source_v, "prepared", master_plan=data)

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
    if not ckpt.get("master_plan"):
        return _json_tool_result({
            "error": "execute_workers requires a master plan. Call run_master first to produce a task plan.",
            "next_v": next_v,
            "source_v": source_v,
        })

    # Circuit breaker: limit total worker invocations per generation
    invocation_count = ckpt.get("worker_invocation_count", 0)
    MAX_WORKER_INVOCATIONS = 6
    if invocation_count + len(tasks) > MAX_WORKER_INVOCATIONS:
        return _json_tool_result({
            "error": f"CIRCUIT BREAKER: {invocation_count} worker invocations already used this generation (max {MAX_WORKER_INVOCATIONS}). Abandon this generation and start a new one.",
            "invocation_count": invocation_count,
            "next_v": next_v,
            "source_v": source_v,
        })

    # When retrying after workers already ran, code has been reset from source.
    # Warn workers that previous modifications no longer exist.
    if reviewer_feedback and ckpt.get("stage") == "workers_done":
        reviewer_feedback += (
            f"\n\nNOTE: This is a retry. The code in bots/claude_v{next_v}/ has been reset "
            f"from source bots/claude_v{source_v}/. Any modifications described in the feedback "
            f"above no longer exist in the code — you must re-implement them from scratch."
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
        from evolution_infra import write_pipeline_checkpoint
        # Preserve the master plan structure (with analysis) from checkpoint,
        # rather than replacing it with the raw tasks list
        plan = ckpt.get("master_plan", tasks) if ckpt else tasks
        write_pipeline_checkpoint(next_v, source_v, "workers_done",
                                  master_plan=plan, reviewer_feedback=reviewer_feedback,
                                  worker_invocation_count=invocation_count + len(tasks))

    result = {
        "success": success,
        "boundary_errors": boundary_errors,
        "logs": ui.get_output(),
        "costs": ui.costs,
    }
    return _json_tool_result(result)


class RunQualityGatesInput(TypedDict):
    version: Annotated[int, "Bot version to test"]
    source_v: Annotated[int, "Source version this bot was derived from"]


@tool("run_quality_gates", "Run all quality gates on a bot: compile check, smoke test, decision tests, and file size check.", {"version": int, "source_v": int})
async def run_quality_gates(args):
    v = args["version"]
    source_v = args.get("source_v")
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

    _ckpt = _matching_checkpoint(v, source_v) if source_v is not None else _matching_checkpoint(v)
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

    if next_v <= source_v:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"next_v ({next_v}) must be greater than source_v ({source_v})"})}]}

    source_dir = get_bot_dir(source_v)
    next_dir = get_bot_dir(next_v)

    if not source_dir.exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": f"Source bot v{source_v} not found"})}]}

    # Guard: warn if source bot is not completed (may be broken)
    if not (source_dir / ".completed").exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": f"Source bot v{source_v} is not marked completed. Cannot use incomplete code as source."})}]}

    # Guard: refuse to overwrite a completed bot
    if next_dir.exists() and (next_dir / ".completed").exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": f"Target v{next_v} already exists and is completed. Refusing to overwrite."})}]}

    # Guard: refuse to re-prepare if pipeline has already progressed past "prepared"
    _ckpt = _matching_checkpoint(next_v, source_v)
    if _ckpt and _ckpt.get("stage") not in (None, "prepared"):
        return {"content": [{"type": "text", "text": json.dumps({"error": f"Pipeline for v{next_v} already at stage '{_ckpt['stage']}'. Refusing to overwrite worker output. Call abandon_generation first if you want to restart."})}]}

    if next_dir.exists():
        shutil.rmtree(next_dir)
    shutil.copytree(source_dir, next_dir, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (next_dir / ".completed").unlink(missing_ok=True)

    # Write "prepared" checkpoint so a kill+restart shows "Workers not yet run → call execute_workers"
    from evolution_infra import write_pipeline_checkpoint
    write_pipeline_checkpoint(next_v, source_v, "prepared", worker_invocation_count=0)

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
        approved = data["approved"] is True
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
        if not approved:
            _record_quality_failure(v, "reviewer", "Code Reviewer",
                                    f"Rejected (score={data.get('quality_score', 0)}): {feedback[:200]}")
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
        error_msg = (
            "Reviewer returned valid JSON but missing 'approved' field"
            if data and isinstance(data, dict)
            else "Reviewer failed to produce valid JSON"
        )
        gate = _gate_payload(
            v,
            source_v,
            False,
            approved=False,
            error=error_msg,
            raw_output=output[:500] if output else "",
        )
        checkpoint_recorded = _record_gate(
            v,
            source_v,
            "review",
            gate,
            stage=None,
            master_plan=plan,
            reviewer_feedback=error_msg,
        )
        result = {
            "approved": False,
            "error": error_msg,
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
    prev_critic = ckpt.get("gate_results", {}).get("critic", {}).get("prev_critic") if ckpt else None
    ui = _get_ui()
    data = await _run_critic(v, source_v, master_plan_str, ui, prev_critic_result=prev_critic)

    if not isinstance(data, dict):
        data = {}
    score = data.get("score", 0)
    try:
        score_num = float(score)
    except (TypeError, ValueError):
        score_num = 0.0
    raw_approved = data.get("approved", score_num >= 6)
    approved = bool(raw_approved) and score_num >= 6
    force_advanced = force_advance and not approved
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
        force_advanced=force_advanced,
    )
    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "critic",
        gate,
        stage="critic_checked" if approved or force_advanced else None,
        master_plan=plan,
        reviewer_feedback=reviewer_feedback,
    )
    if not approved:
        _record_quality_failure(v, "critic", "Strategy Critic",
                                f"Rejected (score={score_num}): {data.get('feedback', '')[:200]}")

    result = {
        **data,
        "approved": approved,
        "raw_approved": raw_approved,
        "score": score_num,
        "logs": ui.get_output(),
        "action": "approve" if approved else ("force_commit" if force_advanced else "retry_workers"),
        "force_advanced": force_advanced,
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
    # Short-circuit: no point running battles if the bot can't even compile or run
    if compile_errors or smoke_errors:
        result = {
            "version": v, "source_v": source_v, "n_games": n_games,
            "opponents": [], "matchups": [],
            "total_wins": 0, "total_losses": 0, "total_draws": 0,
            "passed": False, "blockers": blockers,
        }
        _record_gate(v, source_v, "precommit_eval", _gate_payload(v, source_v, False, **{
            k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}
        }), stage=None)
        return _json_tool_result(result)

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

    # Guard: refuse to run while daemon is active (read-modify-write race on ratings)
    from evolution_infra import daemon_proc
    if daemon_proc is not None and daemon_proc.poll() is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Daemon is running. Stop it first with stop_daemon to avoid ratings race condition."})}]}

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

    from evolution_core import RATINGS_FILE, H2H_FILE, BOT_STATS_FILE, MATCH_HISTORY_FILE, locked_file
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
        match_wins, draws, n_played, _ = mirror_battle(
            str(_bot_main(bot_name)),
            str(_bot_main(opp)),
            n_games=n_games, verbose=False, save_log=False
        )
        w_a, w_b = match_wins[0], match_wins[1]
        total = w_a + w_b + draws
        results_summary.append({"opponent": opp, "wins": w_a, "losses": w_b, "draws": draws})

        # Update H2H
        k = f"{bot_name} vs {opp}" if bot_name < opp else f"{opp} vs {bot_name}"
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
                "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
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
            critic_force_advanced = critic.get("force_advanced", False)
            if critic_force_advanced:
                pass  # force_advance allows committing despite low score
            elif critic.get("approved") is not True or critic_score < 6:
                failed_gates.append({
                    "gate": "critic",
                    "reason": "critic score must be >= 6 and approved must be true (or force_advanced)",
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

    parent2_v = ckpt.get("parent2_v") if ckpt else None
    push_ok = git_commit_bot(v, source_v, strategy, rating_info=rating_info, parent2_v=parent2_v)

    # Verify tag was created
    if not git_has_tag(v):
        return _json_tool_result({
            "error": f"Git tag bot-v{v} not found after commit. Git operations may have failed.",
            "version": v,
        })

    (bot_dir / ".completed").touch()

    # Archive this generation's state snapshot
    try:
        from evolution_infra import archive_generation, archive_rotate_files, archive_old_logs
        archive_generation(v, source_v, ckpt)
        archive_rotate_files(v)
        archive_old_logs()
    except Exception:
        pass

    clear_pipeline_checkpoint()

    try:
        from server.state import app_state
        app_state.set_generation(v, v + 1)
    except Exception:
        pass

    # Signal daemon to pick up the new bot
    reap_signal = Path(__file__).parent / "results" / ".reap_signal"
    reap_signal.touch()

    result = {"committed": True, "version": v, "source_v": source_v, "push_ok": push_ok}
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        result["needs_reap"] = True
        result["pool_size"] = len(active_bots)
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Archivist Stage
# ──────────────────────────────────────────────

@tool("run_archivist", "Run post-commit archive audit for a completed generation. Verifies consistency, auto-reaps if needed, optionally calls LLM for strategic assessment.", {"version": int, "source_v": int})
async def run_archivist(args):
    v = args["version"]
    source_v = args["source_v"]
    ui = _get_ui()

    # 1. Verify post-commit consistency
    bot_dir = get_bot_dir(v)
    consistency_issues = []
    if not (bot_dir / ".completed").exists():
        consistency_issues.append(f".completed missing for v{v}")
    if not git_has_tag(v):
        consistency_issues.append(f"git tag bot-v{v} missing")
    ratings = load_ratings()
    if f"claude_v{v}" not in ratings:
        consistency_issues.append(f"v{v} not in glicko_ratings.json")

    # 2. Auto-reap if pool exceeds limit
    reap_result = None
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        try:
            from tool_status import reap_weakest as _reap_weakest
            reap_result = await _reap_weakest({"quiet": True})
        except Exception as e:
            reap_result = {"error": str(e)}

    # 3. Load archive snapshot for LLM context
    from evolution_infra import ARCHIVE_DIR
    archive_path = ARCHIVE_DIR / f"v{v}.json"
    snapshot = {}
    if archive_path.exists():
        try:
            with open(archive_path, "r") as f:
                snapshot = json.load(f)
        except Exception:
            pass

    # 4. Conditional LLM archivist analysis
    llm_result = None
    needs_llm = False
    # Check if rating has been declining for 3+ gens
    try:
        rating_trend = []
        for check_v in range(max(1, v - 4), v + 1):
            check_archive = ARCHIVE_DIR / f"v{check_v}.json"
            if check_archive.exists():
                with open(check_archive, "r") as f:
                    s = json.load(f)
                r = s.get("rating", {}).get("r")
                if r:
                    rating_trend.append((check_v, r))
        if len(rating_trend) >= 3:
            declining = all(rating_trend[i][1] > rating_trend[i + 1][1] for i in range(len(rating_trend) - 1))
            if declining:
                needs_llm = True
    except Exception:
        pass

    if needs_llm or os.environ.get("EVOLUTION_ALWAYS_ARCHIVE_LLM") == "1":
        try:
            from agent_master import _run_archivist_analysis
            llm_result = await _run_archivist_analysis(v, source_v, snapshot, ui)
            # Append LLM notes to archive snapshot
            if llm_result and archive_path.exists():
                snapshot["archivist_notes"] = llm_result
                with open(archive_path, "w") as f:
                    json.dump(snapshot, f, indent=2, ensure_ascii=False)
        except Exception as e:
            llm_result = {"error": str(e)}

    result = {
        "version": v,
        "source_v": source_v,
        "consistency_ok": len(consistency_issues) == 0,
        "consistency_issues": consistency_issues if consistency_issues else None,
        "reap_result": reap_result,
        "pool_size": len(active_bots),
        "snapshot": snapshot,
        "llm_analysis": llm_result,
    }

    # Record archived stage in checkpoint (then clear)
    _ckpt = _matching_checkpoint(v, source_v)
    if _ckpt:
        from evolution_infra import write_pipeline_checkpoint
        write_pipeline_checkpoint(v, source_v, "archived",
                                  master_plan=_ckpt.get("master_plan"),
                                  gate_results=_ckpt.get("gate_results"))
    clear_pipeline_checkpoint()

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

    # Guard: refuse to overwrite a completed bot
    if target_dir.exists() and (target_dir / ".completed").exists():
        return _json_tool_result({"error": f"Target v{target_v} already exists and is completed. Refusing to overwrite."})

    # Guard: parent must exist
    parent_a_dir = get_bot_dir(parent_a)
    if not parent_a_dir.exists():
        return _json_tool_result({"error": f"Parent A bot v{parent_a} not found"})

    parent_b_dir = get_bot_dir(parent_b)
    if not parent_b_dir.exists():
        return _json_tool_result({"error": f"Parent B bot v{parent_b} not found"})

    ui = _get_ui()
    success = await _run_crossover(parent_a, parent_b, target_v, ui)

    # Write checkpoint so quality gates → review → critic → commit can proceed
    if success:
        from evolution_core import write_pipeline_checkpoint
        write_pipeline_checkpoint(target_v, parent_a, "workers_done",
                                  parent2_v=parent_b)

    result = {"success": success, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
