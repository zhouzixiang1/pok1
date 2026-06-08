"""Pipeline tools: quality gates, code preparation, review, and critic."""

import json
import shutil
from typing import Annotated, TypedDict

from claude_agent_sdk import tool

from evolution_core import (
    get_bot_dir,
    get_logs_dir,
    find_current_v,
    verify_code,
    check_code_size,
    run_smoke_test,
    run_decision_test_details,
    parse_json_output,
    run_claude_query,
    _run_critic,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _record_gate, _gate_payload, _state_blocked,
    _quality_gate_ok, _review_gate_ok, _critic_gate_ok,
    _py_files_changed_between, _resolve_version_args, PROJECT_ROOT,
)
from system_log import log_system_event


def _record_quality_failure(gen, worker_id, role, error, **extra):
    """Record a quality gate rejection (reviewer/critic) to worker_failures.jsonl."""
    import time
    from evolution_core import WORKER_FAILURES_FILE, locked_file
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error, "timestamp": time.time()}
    entry.update({k: v for k, v in extra.items() if v is not None and v is not False})
    with locked_file(WORKER_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ──────────────────────────────────────────────
# Quality Gates
# ──────────────────────────────────────────────

class RunQualityGatesInput(TypedDict):
    version: Annotated[int, "Bot version to test"]
    source_v: Annotated[int, "Source version this bot was derived from"]


@tool("run_quality_gates", "Run all quality gates on a bot: compile check, smoke test, decision tests, and file size check.", {"version": int, "source_v": int})
async def run_quality_gates(args):
    v, source_v = _resolve_version_args(args)
    if v is None:
        return _json_tool_result({"error": "Missing version and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v) if source_v is not None else None
    bot_dir = get_bot_dir(v)

    # CRITICAL: Check that code actually changed vs source.
    # Prevents zombie loop where workers reset code but quality gates pass on unchanged (parent) code.
    code_changed = True
    changed_files_list = []
    if source_v is not None:
        source_dir = get_bot_dir(source_v)
        changed_files_list = [p for p in _py_files_changed_between(source_dir, bot_dir) if 'backup' not in p]
        code_changed = len(changed_files_list) > 0
        if not code_changed:
            log_system_event("pipeline.quality_no_changes", "error",
                             f"Quality gates: v{v} is byte-for-byte identical to v{source_v} -- workers made zero changes",
                             {"version": v, "source_v": source_v})

    # Evaluation cascade: Stage 1 (compile) → Stage 2 (smoke test) → Stage 3 (decision tests + size)
    # Each stage short-circuits on failure to avoid wasting time on doomed candidates.
    compile_errors = verify_code(bot_dir)
    if compile_errors:
        # Stage 1 failed — skip expensive stages
        smoke_errors = []
        decision_detail = {"pass_rate": 0.0, "passed": 0, "total": 0, "critical_passed": 0, "critical_total": 0, "critical_failures": [], "failures": [], "scenarios": []}
        total_lines, oversized = 0, []
    else:
        smoke_errors = run_smoke_test(bot_dir)
        if smoke_errors:
            # Stage 2 failed — skip decision tests
            decision_detail = {"pass_rate": 0.0, "passed": 0, "total": 0, "critical_passed": 0, "critical_total": 0, "critical_failures": [], "failures": [], "scenarios": []}
            total_lines, oversized = 0, []
        else:
            # Stage 3: decision tests + file size (run together)
            decision_detail = run_decision_test_details(bot_dir)
            total_lines, oversized = check_code_size(bot_dir)
    decision_rate = decision_detail.get("pass_rate", 0.0)
    critical_failures = decision_detail.get("critical_failures", [])
    critical_ok = len(critical_failures) == 0
    decision_ok = decision_rate >= 0.7 and critical_ok

    all_passed = (
        len(compile_errors) == 0
        and len(smoke_errors) == 0
        and decision_ok
        and len(oversized) == 0
        and code_changed  # MUST have at least one changed .py file
    )

    result = {
        "version": v,
        "code_changed": code_changed,
        "changed_files": changed_files_list,
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
        "oversized_files": {name: lines for name, lines, _ in oversized} if oversized else {},
        "size_ok": len(oversized) == 0,
        "all_passed": all_passed,
    }

    # Build list of which specific gates failed (for diagnostics)
    failed_gates_detail = []
    if compile_errors:
        failed_gates_detail.append("compile")
    if smoke_errors:
        failed_gates_detail.append("smoke_test")
    if not decision_ok:
        failed_gates_detail.append(f"decision_tests({decision_rate:.0%})")
    if not critical_ok:
        crit_names = [f["id"] for f in critical_failures]
        failed_gates_detail.append(f"CRITICAL decision tests FAILED: {', '.join(crit_names)}")
    if not code_changed:
        failed_gates_detail.append(f"no_code_changes(v{v} identical to v{source_v})")
    if oversized:
        failed_gates_detail.append(f"file_size({', '.join(f'{n}:{l}L/{lim}L' for n, l, lim in oversized)})")

    log_system_event(
        "pipeline.quality_passed" if all_passed else "pipeline.quality_failed",
        "success" if all_passed else "error",
        f"Quality gates {'passed' if all_passed else 'failed'} for v{v}: {', '.join(failed_gates_detail) or 'all checks passed'}",
        {"version": v, "pass_rate": round(decision_rate, 2), "all_passed": all_passed,
         "failed_gates": failed_gates_detail if not all_passed else [],
         "critical_failures": critical_failures if not all_passed and not critical_ok else []},
    )

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


# ──────────────────────────────────────────────
# Prepare Next Gen
# ──────────────────────────────────────────────

class PrepareNextGenInput(TypedDict):
    source_v: Annotated[int, "Source bot version to copy from"]
    next_v: Annotated[int, "Target version"]


@tool("prepare_next_gen", "Prepare the next generation directory by copying from source bot.", {"source_v": int, "next_v": int})
async def prepare_next_gen(args):
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing source_v/next_v and no active checkpoint"})}]}

    if next_v <= source_v:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"next_v ({next_v}) must be greater than source_v ({source_v})"})}]}

    # Guard against clearly invalid version numbers (test artifacts)
    if next_v >= 900:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"next_v ({next_v}) is invalid. Version numbers must be < 900."})}]}

    current_v = find_current_v()
    if next_v > current_v + 10:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"next_v ({next_v}) is too far ahead of current_v ({current_v}). Use next_v = {current_v + 1}."})}]}

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
    write_pipeline_checkpoint(next_v, source_v, "prepared", worker_failure_count=0)

    log_system_event("pipeline.prepare", "info", f"Prepared v{next_v} from v{source_v}",
                     {"next_v": next_v, "source_v": source_v})

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
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    plan = args.get("plan", [])

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
    try:
        output, _, _ = await run_claude_query(
            reviewer_prompt, [], ui, "LEAD CODE REVIEWER", log_file, tools=["Bash", "Read"]
        )
    except Exception as e:
        ui.log_history(f"Reviewer error: {e}. Defaulting to rejected.", "warn")
        output = None
    data = parse_json_output(output)

    if data and "approved" in data:
        approved = data["approved"] is True
        feedback = data.get("feedback", "")
        log_system_event(
            "pipeline.review_passed" if approved else "pipeline.review_rejected",
            "success" if approved else "warn",
            f"Review {'approved' if approved else 'rejected'} v{v} (score={data.get('quality_score', 0)})",
            {"version": v, "score": data.get("quality_score", 0), "approved": approved},
        )
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
            master_plan=ckpt.get("master_plan") if ckpt else plan,
            reviewer_feedback=feedback,
        )
        if not approved:
            _record_quality_failure(v, "reviewer", "Code Reviewer",
                                    f"Rejected (score={data.get('quality_score', 0)}): {feedback[:2000]}")
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
        # Regex fallback: try to extract approval + score from raw text
        import re as _re
        data = None
        if output:
            approved_match = _re.search(
                r'(?:approved|APPROVED|approve)[\s:="\']+(true|yes|1)',
                output, _re.IGNORECASE,
            )
            score_match = _re.search(
                r'(?:quality.?score|score)[\s:="\']*(\d+(?:\.\d+)?)',
                output, _re.IGNORECASE,
            )
            if approved_match and score_match:
                fallback_score = float(score_match.group(1))
                if fallback_score >= 6:
                    data = {
                        "approved": True,
                        "quality_score": fallback_score,
                        "feedback": output[-2000:],
                        "change_summary": "",
                        "risk_areas": [],
                    }
                    ui.log_history(
                        f"Reviewer JSON parse failed but regex extracted approval (score={fallback_score})",
                        "warn",
                    )
        if data:
            # Reuse the approval path with regex-extracted data
            approved = True
            feedback = data.get("feedback", "")
            gate = _gate_payload(
                v, source_v, True,
                approved=True, quality_score=data.get("quality_score", 0),
                feedback=feedback, change_summary="", risk_areas=[],
            )
            checkpoint_recorded = _record_gate(
                v, source_v, "review", gate,
                stage="reviewed",
                master_plan=ckpt.get("master_plan") if ckpt else plan,
                reviewer_feedback=feedback,
            )
            result = {
                "approved": True,
                "quality_score": data.get("quality_score", 0),
                "change_summary": "",
                "risk_areas": [],
                "feedback": feedback,
                "checkpoint_recorded": checkpoint_recorded,
                "logs": ui.get_output(),
            }
            return _json_tool_result(result)
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
            master_plan=ckpt.get("master_plan") if ckpt else plan,
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
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    plan = args.get("plan", [])
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
    # Detect auto-degraded critic (LLM failure → score=5, approved=True)
    auto_degraded = (
        score_num == 5
        and data.get("approved") is True
        and "auto-approved with low confidence" in str(data.get("feedback", ""))
    )
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

    # Track intra-gen retry count: increment when critic rejects (retry_workers)
    current_attempt = (ckpt.get("generation_attempt", 0) or 0) if ckpt else 0
    if not approved and not force_advanced:
        current_attempt += 1

    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "critic",
        gate,
        stage="critic_checked" if approved or force_advanced else None,
        master_plan=ckpt.get("master_plan") if ckpt else plan,
        reviewer_feedback=reviewer_feedback,
        generation_attempt=current_attempt,
    )
    if not approved:
        _record_quality_failure(v, "critic", "Strategy Critic",
                                f"Rejected (score={score_num}): {data.get('feedback', '')[:2000]}",
                                local_optima_warning=data.get("local_optima_warning", False),
                                local_optima_reason=data.get("local_optima_reason"))

    log_system_event(
        "pipeline.critic_passed" if approved else "pipeline.critic_rejected",
        "success" if approved else "warn",
        f"Critic {'approved' if approved else 'rejected'} v{v} (score={score_num})",
        {"version": v, "score": score_num, "approved": approved},
    )

    # Extract Critic evidence and append to experience pool
    evidence = data.get("evidence") if isinstance(data, dict) else None
    if evidence:
        try:
            from tool_commit import _append_experience_updates
            ev_parts = []
            h2h_w = evidence.get("h2h_weaknesses", [])
            if h2h_w:
                ev_parts.append(f"H2H weaknesses: {', '.join(str(w) for w in h2h_w[:5])}")
            ep_refs = evidence.get("experience_pool_refs", [])
            if ep_refs:
                ev_parts.append(f"Experience pool refs: {', '.join(str(r) for r in ep_refs[:3])}")
            diff_refs = evidence.get("diff_refs", [])
            if diff_refs:
                ev_parts.append(f"Diff refs: {', '.join(str(r) for r in diff_refs[:3])}")
            if ev_parts:
                evidence_summary = "; ".join(ev_parts)
                _append_experience_updates(
                    version=v,
                    updates=[f"Critic evidence: {evidence_summary}"],
                    strategic_advice="",
                    generation_assessment="info",
                )
        except Exception:
            pass  # Non-critical: evidence write failure should not block pipeline

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
