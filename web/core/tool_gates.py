"""Pipeline tools: quality gates, code preparation, review, and critic."""

import asyncio
import json
import shutil
import time
from pathlib import Path

from claude_agent_sdk import tool

from logging_config import get_logger
_log = get_logger("gates")

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
    _set_pipeline_status,
)
from fix_verification import verify_fixes
from system_log import log_system_event
from llm_failure import is_llm_infra_error, infra_payload
import spot_analyzer


def _record_quality_failure(gen, worker_id, role, error, **extra):
    """Record a quality gate rejection (reviewer/critic) to worker_failures.jsonl."""
    from evolution_core import WORKER_FAILURES_FILE, locked_file
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error, "timestamp": time.time()}
    entry.update({k: v for k, v in extra.items() if v is not None and v is not False})
    with locked_file(WORKER_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _idempotency_check(v, source_v, stage_set, gate_name, approval_key="approved",
                       extra_ok_keys=(), directive=""):
    """Check if a pipeline stage has already been completed; return cached result or None.

    Args:
        v: Bot version.
        source_v: Parent version.
        stage_set: Tuple/list of stage strings that mean "this stage passed".
        gate_name: Key inside gate_results (e.g. "quality", "review", "critic").
        approval_key: The key to check for truthiness (default "approved").
        extra_ok_keys: Additional keys that count as truthy (e.g. ("force_advanced",)).
        directive: Message to include when returning cached result.

    Returns:
        An MCP-formatted result dict if the stage already passed, or None.
    """
    ckpt = _matching_checkpoint(v, source_v)
    if not ckpt or ckpt.get("stage") not in stage_set:
        return None
    gate = ckpt.get("gate_results", {}).get(gate_name, {})
    if gate.get(approval_key) is True or any(gate.get(k) is True for k in extra_ok_keys):
        gate["idempotent_cache"] = True
        gate["checkpoint_recorded"] = True
        gate["directive"] = directive
        return _json_tool_result(gate)
    return None


# ──────────────────────────────────────────────
# Quality Gates
# ──────────────────────────────────────────────

@tool("run_quality_gates", "Run all quality gates on a bot: compile check, smoke test, decision tests, and file size check.", {"version": int, "source_v": int})
async def run_quality_gates(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None:
        return _json_tool_result({"error": "Missing version and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v) if source_v is not None else None
    bot_dir = get_bot_dir(v)

    _set_pipeline_status(f"Running quality gates for v{v}")

    # Idempotency guard: skip if quality gates already passed for this version
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("quality_passed", "reviewed", "critic_checked", "verified", "archived"),
        gate_name="quality",
        approval_key="all_passed",
        directive="Quality gates ALREADY PASSED. Call run_review next.",
    )
    if _cached:
        return _cached

    # CRITICAL: Check that code actually changed vs source.
    # Prevents zombie loop where workers reset code but quality gates pass on unchanged (parent) code.
    code_changed = True
    changed_files_list = []
    source_dir = None
    if source_v is not None:
        source_dir = get_bot_dir(source_v)
        changed_files_list = [p for p in _py_files_changed_between(source_dir, bot_dir) if 'backup' not in p]
        code_changed = len(changed_files_list) > 0
        if not code_changed:
            log_system_event("pipeline.quality_no_changes", "error",
                             f"Quality gates: v{v} is byte-for-byte identical to v{source_v} -- workers made zero changes",
                             {"version": v, "source_v": source_v})

    compile_errors = verify_code(bot_dir)
    smoke_errors = run_smoke_test(bot_dir)

    # --- P0-3: LLM-Generated Dynamic Decision Tests ---
    dynamic_scenarios = []
    if source_v is not None and changed_files_list:
        try:
            from audit_agents import _generate_dynamic_tests
            # Get existing scenario IDs to avoid duplicates
            from decision_tester import SCENARIOS_FILE
            existing_ids = []
            if SCENARIOS_FILE.exists():
                with open(SCENARIOS_FILE) as _f:
                    for s in json.load(_f):
                        existing_ids.append(s.get("id", ""))
            ckpt_dt = _matching_checkpoint(v, source_v)
            master_plan_dt = ckpt_dt.get("master_plan", {}) if ckpt_dt else {}
            ui = _get_ui()
            # Timeout: LLM call should complete in 60s; if not, skip dynamic tests
            dynamic_scenarios = await asyncio.wait_for(
                _generate_dynamic_tests(
                    v, source_v, changed_files_list, master_plan_dt, existing_ids, ui
                ),
                timeout=60,
            )
        except asyncio.TimeoutError:
            pass  # LLM timed out — use only predefined scenarios
        except Exception as e:
            _log.warning("Dynamic test generation error: %s", e)

    # --- B3: Heuristic Dynamic Regression Tests from Diff ---
    heuristic_scenarios = []
    if source_v is not None and changed_files_list:
        try:
            import difflib as _difflib
            from decision_tester import generate_scenarios_from_diff, save_dynamic_scenarios, load_dynamic_scenarios
            from decision_tester import DYNAMIC_SCENARIOS_FILE
            _src_dir = get_bot_dir(source_v)
            _dst_dir = get_bot_dir(v)

            # Build diff text from changed files
            _diff_parts = []
            for _rel in changed_files_list:
                _src_file = _src_dir / _rel
                _dst_file = _dst_dir / _rel
                _before = _src_file.read_text() if _src_file.exists() else ""
                _after = _dst_file.read_text() if _dst_file.exists() else ""
                if _before != _after:
                    _diff = _difflib.unified_diff(
                        _before.splitlines(keepends=True),
                        _after.splitlines(keepends=True),
                        fromfile=f"v{source_v}/{_rel}", tofile=f"v{v}/{_rel}",
                        n=2,
                    )
                    _diff_text = "".join(_diff)
                    if _diff_text:
                        _diff_parts.append(_diff_text)

            if _diff_parts:
                _full_diff = "\n".join(_diff_parts)[-8000:]
                heuristic_scenarios = generate_scenarios_from_diff(
                    _full_diff, str(_src_dir), str(_dst_dir)
                )
                if heuristic_scenarios:
                    # Persist to file for future runs
                    _existing = load_dynamic_scenarios()
                    _existing_ids = {s.get("id") for s in _existing}
                    _new_to_save = [s for s in heuristic_scenarios
                                    if s.get("id") not in _existing_ids]
                    save_dynamic_scenarios(_existing + _new_to_save)
                    _log.info(
                        "B3: Generated %d heuristic scenarios from diff for v%d",
                        len(heuristic_scenarios), v
                    )
        except Exception as e:
            _log.warning("B3 heuristic scenario generation error: %s", e)

    # Combine both dynamic sources
    _all_dynamic = (dynamic_scenarios or []) + heuristic_scenarios

    decision_detail = run_decision_test_details(bot_dir, extra_scenarios=_all_dynamic or None)
    decision_rate = decision_detail.get("pass_rate", 0.0)
    critical_failures = decision_detail.get("critical_failures", [])
    critical_ok = len(critical_failures) == 0
    total_lines, oversized = check_code_size(bot_dir, source_dir=source_dir)
    decision_ok = decision_rate >= 0.7 and critical_ok

    # --- P1-3: Structural fix-verification gate (authoritative fix-present judgment) ---
    # fix_injection.py uses substring matching which silently misses when a worker
    # refactors the target function. verify_fixes() runs STRUCTURAL/RUNTIME checks in
    # subprocess isolation so a confirmed invariant violation blocks the pipeline
    # regardless of how the code was written. A verifier FAILURE (exception) is never
    # blocking — only a CONFIRMED invariant violation is.
    fix_results = verify_fixes(bot_dir)
    fix_ok = all(r.get("ok", False) for r in fix_results.values())
    fix_failed = {fid: r for fid, r in fix_results.items() if not r.get("ok", False)}

    all_passed = (
        len(compile_errors) == 0
        and len(smoke_errors) == 0
        and decision_ok
        and len(oversized) == 0
        and code_changed  # MUST have at least one changed .py file
        and fix_ok  # P1-3: missing mandatory fix blocks the pipeline
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
        "fix_verification": fix_results,
        "fix_ok": fix_ok,
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
    if not code_changed:
        failed_gates_detail.append(f"no_code_changes(v{v} identical to v{source_v})")
    if oversized:
        failed_gates_detail.append(f"file_size({', '.join(f'{n}:{l}L/{lim}L' for n, l, lim in oversized)})")
    if not fix_ok:
        detail_parts = [f"{fid}: {r.get('reason', 'unknown')[:160]}" for fid, r in fix_failed.items()]
        failed_gates_detail.append(f"missing_fix({'; '.join(detail_parts)})")
        # Record to worker_failures.jsonl so future worker prompts see the missing fix
        # (this is the primary feedback path into workers; reviewer_feedback injection
        # is intentionally omitted to avoid an out-of-order _ckpt reference here).
        for fid, r in fix_failed.items():
            _record_quality_failure(
                v, "fix_verifier", fid,
                f"Mandatory fix {fid} NOT present: {r.get('reason', '')[:2000]}",
            )

    log_system_event(
        "pipeline.quality_passed" if all_passed else "pipeline.quality_failed",
        "success" if all_passed else "error",
        f"Quality gates {'passed' if all_passed else 'failed'} for v{v}: {', '.join(failed_gates_detail) or 'all checks passed'}",
        {"version": v, "pass_rate": round(decision_rate, 2), "all_passed": all_passed,
         "failed_gates": failed_gates_detail if not all_passed else []},
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

    try:
        log_system_event("pipeline.quality_gates", "info",
                         f"Quality gates finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "all_passed": all_passed, "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Prepare Next Gen
# ──────────────────────────────────────────────

@tool("prepare_next_gen", "Prepare the next generation directory by copying from source bot.", {"source_v": int, "next_v": int})
async def prepare_next_gen(args):
    _t0 = time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return _json_tool_result({"error": "Missing source_v/next_v and no active checkpoint"})

    _set_pipeline_status(f"Preparing v{next_v}")

    if next_v <= source_v:
        return _json_tool_result({"error": f"next_v ({next_v}) must be greater than source_v ({source_v})"})

    # Guard against clearly invalid version numbers (test artifacts)
    if next_v >= 900:
        return _json_tool_result({"error": f"next_v ({next_v}) is invalid. Version numbers must be < 900."})

    current_v = find_current_v()
    if next_v > current_v + 10:
        return _json_tool_result({"error": f"next_v ({next_v}) is too far ahead of current_v ({current_v}). Use next_v = {current_v + 1}."})

    source_dir = get_bot_dir(source_v)
    next_dir = get_bot_dir(next_v)

    if not source_dir.exists():
        return _json_tool_result({"error": f"Source bot v{source_v} not found"})

    # Guard: warn if source bot is not completed (may be broken)
    if not (source_dir / ".completed").exists():
        return _json_tool_result({"error": f"Source bot v{source_v} is not marked completed. Cannot use incomplete code as source."})

    # Guard: verify git tag exists for source bot (authoritative commit proof)
    from evolution_infra import git_has_tag
    if not git_has_tag(source_v):
        return _json_tool_result({"error": f"Source bot v{source_v} has .completed but no git tag 'bot-v{source_v}'. Cannot evolve from uncommitted code. Try a different source version."})

    # Guard: refuse to overwrite a completed bot
    if next_dir.exists() and (next_dir / ".completed").exists():
        return _json_tool_result({"error": f"Target v{next_v} already exists and is completed. Refusing to overwrite."})

    # Guard: refuse to re-prepare if pipeline has already progressed past "prepared"
    _ckpt = _matching_checkpoint(next_v, source_v)
    if _ckpt and _ckpt.get("stage") not in (None, "prepared", "timed_out"):
        return _json_tool_result({"error": f"Pipeline for v{next_v} already at stage '{_ckpt['stage']}'. Refusing to overwrite worker output. Call abandon_generation first if you want to restart."})

    if next_dir.exists():
        shutil.rmtree(next_dir)
    shutil.copytree(source_dir, next_dir, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    # Apply known critical fixes regardless of source bot state
    from fix_injection import apply_known_fixes, log_fix_application
    applied, skipped = apply_known_fixes(next_dir)
    if applied or skipped:
        log_fix_application(applied, skipped, next_dir, source_v)
    if skipped:
        _log.info("Fix patches skipped for v%d: %s", next_v, skipped)

    (next_dir / ".completed").unlink(missing_ok=True)

    # Write "prepared" checkpoint so a kill+restart shows "Workers not yet run → call execute_workers"
    from evolution_infra import write_pipeline_checkpoint
    write_pipeline_checkpoint(next_v, source_v, "prepared", worker_failure_count=0)

    log_system_event("pipeline.prepare_done", "info", f"Prepared v{next_v} from v{source_v}",
                     {"next_v": next_v, "source_v": source_v, "elapsed_sec": round(time.time() - _t0, 2)})

    return _json_tool_result({"prepared": True, "next_v": next_v, "source_v": source_v})


# ──────────────────────────────────────────────
# Review Stage
# ──────────────────────────────────────────────

@tool("run_review", "Run Lead Code Reviewer on the bot changes. Returns approval decision with quality score.", {"version": int, "source_v": int, "plan": list})
async def run_review(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    plan = args.get("plan", [])

    _set_pipeline_status(f"Reviewing v{v}")

    # Idempotency guard: skip if review already approved
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("reviewed", "critic_checked", "verified", "archived"),
        gate_name="review",
        directive="Review ALREADY PASSED. Call run_critic next.",
    )
    if _cached:
        return _cached

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

    # Inject Worker CoT audit_focus_areas into reviewer prompt
    _review_ckpt = _matching_checkpoint(v, source_v)
    if _review_ckpt:
        _audit_context = _review_ckpt.get("audit_context", {}) or {}
        _focus_areas = _audit_context.get("worker_cot_focus_areas", [])
        if not _focus_areas:
            # Also check gate_results for audit_focus_areas stored by execute_workers
            _worker_gate = _review_ckpt.get("gate_results", {}).get("workers", {})
            _focus_areas = _worker_gate.get("audit_focus_areas", [])
        if _focus_areas:
            _focus_block = (
                "\n\n# Worker CoT Audit Findings (from execute_workers)\n"
                "The Worker Chain-of-Thought audit detected these concerns.\n"
                "Pay EXTRA attention to these areas during your review:\n"
            )
            for _fa in _focus_areas:
                _focus_block += f"- {_fa}\n"
            _focus_block += "\n"
            reviewer_prompt += _focus_block

    log_file = get_logs_dir(v) / "reviewer_io.txt"

    ui = _get_ui()
    try:
        output, _, _ = await run_claude_query(
            reviewer_prompt, [], ui, "LEAD CODE REVIEWER", log_file, tools=["Bash", "Read"]
        )
    except Exception as e:
        # ── LLM infrastructure error short-circuit ──
        # If the Reviewer LLM call crashed (SDK error / timeout / connection), do NOT
        # treat it as an approved:False rejection (which would block the pipeline).
        # Retry the review gate (not the workers), and soft-abandon after 3 attempts
        # while keeping stage=quality_passed so the orchestrator re-calls run_review.
        # No generation_attempt increment, no quality-failure record, no rejection gate.
        if is_llm_infra_error(e):
            prev = ckpt.get("gate_results", {}).get("review", {}).get("review_infra_retry", 0) if ckpt else 0
            infra_count = prev + 1
            _record_gate(
                v, source_v, "review",
                {"llm_failed": True, "approved": False,
                 "review_infra_retry": 0 if infra_count >= 3 else infra_count,  # reset on abandon
                 "error": str(e)},
                stage=None,                                    # keep current stage (quality_passed)
                master_plan=ckpt.get("master_plan") if ckpt else plan,
                reviewer_feedback=f"Reviewer LLM infra error: {e}",
                generation_attempt=ckpt.get("generation_attempt", 0),  # do NOT increment
            )
            try:
                log_system_event(
                    "pipeline.review_infra_error", "warn",
                    f"Reviewer v{v} LLM crashed (infra) attempt {infra_count}/3",
                    {"version": v, "infra_retry": infra_count},
                )
            except Exception:
                pass
            ui.log_history(f"Reviewer LLM infrastructure error (NOT a rejection): {e}", "warn")
            if infra_count >= 3:
                return _json_tool_result({"action": "abandon_cycle",
                    "directive": (f"Reviewer LLM crashed {infra_count}x (infrastructure, NOT a code rejection). "
                                  f"Soft-abandon: stage stays 'quality_passed', next cycle resumes v{v} at run_review. "
                                  f"Do NOT retry_workers or run_master. End this cycle."),
                    "llm_failed": True})
            return _json_tool_result({"action": "retry_review",
                "directive": (f"Reviewer LLM crashed (infra, NOT a code rejection). Call run_review AGAIN "
                              f"(attempt {infra_count}/3). Do NOT retry_workers or run_master."),
                "llm_failed": True})
        ui.log_history(f"Reviewer error: {e}. Defaulting to rejected.", "warn")
        output = None
    data = parse_json_output(output)

    if data and "approved" in data:
        approved = data["approved"] is True
        feedback = data.get("feedback", "")
        try:
            log_system_event(
                "pipeline.review_passed" if approved else "pipeline.review_rejected",
                "success" if approved else "warn",
                f"Review {'approved' if approved else 'rejected'} v{v} (score={data.get('quality_score', 0)})",
                {"version": v, "score": data.get("quality_score", 0), "approved": approved},
            )
        except Exception:
            pass
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

    try:
        log_system_event("pipeline.review_done", "info",
                         f"Review finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "approved": result.get("approved", False),
                          "score": result.get("quality_score", 0), "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Critic Stage
# ──────────────────────────────────────────────

@tool("run_critic", "Run Poker Strategy Critic on bot changes. Returns score 1-10 and strategic feedback. score ≥ 6 = approved.", {"version": int, "source_v": int, "plan": list, "reviewer_feedback": str, "force_advance": bool})
async def run_critic(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    plan = args.get("plan", [])
    reviewer_feedback = args.get("reviewer_feedback", "")
    force_advance = args.get("force_advance", False)

    _set_pipeline_status(f"Critic evaluating v{v}")

    # Idempotency guard: skip if critic already approved
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("critic_checked", "verified", "archived"),
        gate_name="critic",
        extra_ok_keys=("force_advanced",),
        directive="Critic ALREADY PASSED. Call run_precommit_eval next.",
    )
    if _cached:
        return _cached

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

    # ── LLM infrastructure error short-circuit ──
    # If the Critic LLM call crashed (NOT a strategic rejection), do NOT treat it
    # as a score=0 rejection. Retry the critic gate (not the workers), and soft-
    # abandon after 3 attempts while keeping stage=reviewed so the next cycle
    # resumes here. No generation_attempt increment, no quality-failure record,
    # no guardian trigger, no critic_rejected log.
    if isinstance(data, dict) and data.get("llm_failed"):
        prev = ckpt.get("gate_results", {}).get("critic", {}).get("critic_infra_retry", 0) if ckpt else 0
        infra_count = prev + 1
        _record_gate(
            v, source_v, "critic",
            {"llm_failed": True, "approved": False,
             "critic_infra_retry": 0 if infra_count >= 3 else infra_count,  # reset on abandon
             "error": data.get("error", "")},
            stage=None,                                    # keep current stage (reviewed)
            master_plan=ckpt.get("master_plan") if ckpt else plan,
            reviewer_feedback=reviewer_feedback,
            generation_attempt=ckpt.get("generation_attempt", 0),  # do NOT increment
        )
        try:
            log_system_event(
                "pipeline.critic_infra_error", "warn",
                f"Critic v{v} LLM crashed (infra) attempt {infra_count}/3",
                {"version": v, "infra_retry": infra_count},
            )
        except Exception:
            pass
        if infra_count >= 3:
            return _json_tool_result({"action": "abandon_cycle",
                "directive": (f"Critic LLM crashed {infra_count}x (infrastructure, NOT strategy). "
                              f"Soft-abandon: stage stays 'reviewed', next cycle resumes v{v} at run_critic. "
                              f"Do NOT retry_workers or run_master. End this cycle."),
                "llm_failed": True})
        return _json_tool_result({"action": "retry_critic",
            "directive": (f"Critic LLM crashed (infra, NOT strategy). Call run_critic AGAIN "
                          f"(attempt {infra_count}/3). Do NOT retry_workers or run_master."),
            "llm_failed": True})

    if not isinstance(data, dict):
        data = {}
    score = data.get("score", 0)
    try:
        score_num = float(score)
    except (TypeError, ValueError):
        score_num = 0.0
    raw_approved = data.get("approved", score_num >= 6)
    # Critic is now ADVISORY — final approve/reject is decided by precommit
    # (Step 2's paired-bootstrap statistical gate). score and feedback still
    # surface to workers as improvement hints, but do NOT block the pipeline.
    advisory_approved = bool(raw_approved) and score_num >= 6  # for telemetry/logging
    approved = True  # advisory: precommit statistical gate (Step 2) is the final judge
    # In advisory mode approved is always True, so force_advanced follows
    # force_advance directly (kept for backward-compat with downstream gates).
    force_advanced = bool(force_advance)
    gate = _gate_payload(
        v,
        source_v,
        approved,
        approved=approved,
        raw_approved=raw_approved,
        advisory_approved=advisory_approved,
        advisory_score=score_num,
        score=score_num,
        feedback=data.get("feedback", ""),
        strategic_assessment=data.get("strategic_assessment", ""),
        local_optima_warning=data.get("local_optima_warning", False),
        force_advanced=force_advanced,
    )

    # Track intra-gen retry count: increment when critic rejects (retry_workers).
    # ADVISORY-ONLY: critic no longer blocks, so we never bump current_attempt or
    # emit retry_workers here. Keep the read for downstream telemetry only.
    current_attempt = (ckpt.get("generation_attempt", 0) or 0) if ckpt else 0

    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "critic",
        gate,
        stage="critic_checked",  # always advance: critic is advisory, precommit is final judge
        master_plan=ckpt.get("master_plan") if ckpt else plan,
        reviewer_feedback=reviewer_feedback,
        generation_attempt=current_attempt,
    )
    guardian_diagnosis = None
    if not advisory_approved:
        # Telemetry only: record critic rejection diagnostics so they surface to
        # the next worker prompt as improvement hints. Does NOT block the pipeline.
        _record_quality_failure(v, "critic", "Strategy Critic",
                                f"Rejected (score={score_num}): {data.get('feedback', '')[:2000]}",
                                local_optima_warning=data.get("local_optima_warning", False),
                                local_optima_reason=data.get("local_optima_reason"))
        # Meta-2: Trigger Regression Guardian on very low critic score.
        # Run synchronously so the diagnosis is visible to the Orchestrator
        # (merged into the tool result below). This is advisory only — it is
        # NOT a hard second gate; precommit remains the final judge.
        # _run_regression_guardian has a safe_default so it never throws.
        if score_num < 4:
            try:
                from audit_agents import _run_regression_guardian
                _c = _matching_checkpoint(v, source_v)
                _history = {
                    "score": score_num,
                    "feedback": data.get("feedback", "")[:500],
                    "strategic_assessment": data.get("strategic_assessment", "")[:500],
                    "master_plan": _c.get("master_plan", {}) if _c else {},
                    "gate_results": _c.get("gate_results", {}) if _c else {},
                }
                guardian_diagnosis = await _run_regression_guardian(
                    v, source_v, _history,
                    f"Critic score {score_num} < 4: {data.get('feedback', '')[:200]}",
                    ui,
                )
            except Exception as e:
                _log.warning("Regression guardian dispatch failed for v%s: %s", v, e)

    try:
        log_system_event(
            "pipeline.critic_passed" if advisory_approved else "pipeline.critic_rejected",
            "success" if advisory_approved else "warn",
            f"Critic {'approved' if advisory_approved else 'rejected (advisory)'} v{v} (score={score_num})",
            {"version": v, "score": score_num, "approved": approved,
             "advisory_approved": advisory_approved},
        )
    except Exception:
        pass

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
        "advisory_score": score_num,
        "advisory_approved": advisory_approved,
        "logs": ui.get_output(),
        "action": "approve",  # advisory: orchestrator proceeds to run_precommit_eval (final judge)
        "force_advanced": force_advanced,
        "checkpoint_recorded": checkpoint_recorded,
    }
    if guardian_diagnosis:
        result["regression_guardian"] = {
            "severity": guardian_diagnosis.get("severity", "minor"),
            "failure_stage": guardian_diagnosis.get("failure_stage", "unknown"),
            "recovery_recommendation": guardian_diagnosis.get("recovery_recommendation", ""),
            "diagnosis": guardian_diagnosis.get("diagnosis", ""),
            "root_cause": guardian_diagnosis.get("root_cause", ""),
            "confidence": guardian_diagnosis.get("confidence", "low"),
        }
    try:
        log_system_event("pipeline.critic_done", "info",
                         f"Critic finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "approved": approved, "score": score_num,
                          "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Spot Check Stage
# ──────────────────────────────────────────────

@tool("run_spot_check", "Run spot check on changed functions: parse diff, generate scenarios, run bot, verify behavior.", {"parent_version": int, "current_version": int, "master_plan": dict})
async def run_spot_check(args):
    parent_version = args.get("parent_version")
    current_version = args.get("current_version")
    master_plan = args.get("master_plan", {})

    if parent_version is None or current_version is None:
        return _json_tool_result({"error": "Missing parent_version or current_version"})

    parent_dir = str(get_bot_dir(int(parent_version)))
    current_dir = str(get_bot_dir(int(current_version)))

    changed_functions = spot_analyzer.parse_diff(parent_dir, current_dir)

    bot_code = {}
    for change in changed_functions:
        fp = change.get("file")
        if fp and Path(fp).exists():
            bot_code[fp] = Path(fp).read_text()

    scenarios = spot_analyzer.generate_test_scenarios(changed_functions, bot_code)

    bot_main = Path(current_dir) / "main.py"
    actual_actions = []
    for scenario in scenarios:
        result = spot_analyzer.run_bot_scenario(str(bot_main), scenario)
        actual_actions.append(result)

    verification = spot_analyzer.verify_behavior(master_plan, scenarios, actual_actions)

    result = {
        "status": "success",
        "result": {
            "passed": verification.get("passed", False),
            "assessment": f"Spot check {verification.get('passed_count', 0)}/{verification.get('total', 0)} passed, confidence={verification.get('confidence', 'unknown')}",
            "details": verification,
            "changed_functions": changed_functions,
            "scenarios_count": len(scenarios),
        },
    }
    return _json_tool_result(result)
