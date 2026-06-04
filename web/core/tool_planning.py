"""Pipeline tools: direction audit, master planning, and worker execution."""

import json
import shutil
import time
from pathlib import Path
from typing import Annotated, TypedDict

from claude_agent_sdk import tool

from evolution_core import (
    get_bot_dir,
    _run_master_analysis,
    _run_direction_audit,
    _execute_workers,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _state_blocked,
    _validate_worker_boundaries,
    _target_rel,
    PROJECT_ROOT,
)
from system_log import log_system_event


# ──────────────────────────────────────────────
# Direction Audit Stage (pre-Master)
# ──────────────────────────────────────────────

@tool("run_direction_audit", "Audit recent generation directions for repetition. Returns exhausted directions and mandatory constraints for the Master.", {"source_v": int, "next_v": int})
async def run_direction_audit(args):
    source_v = args["source_v"]
    next_v = args["next_v"]

    ui = _get_ui()
    result = await _run_direction_audit(source_v, ui)

    repetition = result.get("repetition_detected", False)
    exhausted = result.get("exhausted_directions", [])
    constraints = result.get("mandatory_constraints")
    suggested = result.get("suggested_direction")
    confidence = result.get("confidence", "low")

    direction_audit_payload = {
        "repetition_detected": repetition,
        "exhausted_directions": exhausted,
        "mandatory_constraints": constraints,
        "suggested_direction": suggested,
        "confidence": confidence,
        "resolved": False,
    }

    # Persist to checkpoint
    from evolution_infra import write_pipeline_checkpoint
    _ckpt = _matching_checkpoint(next_v, source_v)
    existing_plan = _ckpt.get("master_plan") if _ckpt else None
    write_pipeline_checkpoint(
        next_v, source_v, "direction_audited",
        direction_audit=direction_audit_payload,
        master_plan=existing_plan,
        worker_invocation_count=_ckpt.get("worker_invocation_count", 0) if _ckpt else 0,
    )

    event_type = "pipeline.direction_audit_warning" if repetition else "pipeline.direction_audit_passed"
    severity = "warn" if repetition else "success"
    msg = (f"Direction audit: repetition detected ({', '.join(exhausted)})" if repetition
           else "Direction audit: no repetition detected")
    log_system_event(event_type, severity, msg, {
        "next_v": next_v, "source_v": source_v,
        "repetition_detected": repetition,
        "exhausted_directions": exhausted,
    })

    return _json_tool_result({
        "direction_audit": direction_audit_payload,
        "logs": ui.get_output(),
    })


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


def _validate_master_plan(plan, next_v=None):
    """Validate master plan constraints before dispatching workers.

    Returns (errors, warnings) — only errors block plan storage.
    Boundary warnings are logged but non-blocking; the reviewer/critic
    enforce actual role boundaries during code review.
    """
    errors = []
    warnings = []
    tasks = plan.get("tasks", [])
    if len(tasks) > 3:
        errors.append(f"Too many tasks: {len(tasks)} > 3")
    for i, task in enumerate(tasks):
        targets = task.get("target_files", [])
        if len(targets) > 3:
            errors.append(f"Task {i}: too many target_files ({len(targets)} > 3)")
        prompt = task.get("worker_prompt", "")
        if len(prompt) > 5000:
            errors.append(f"Task {i}: worker_prompt too long ({len(prompt)} > 5000 chars)")
        role = str(task.get("role", "")).lower()
        if "hyperparameter" in role or "tuner" in role:
            # Tuners should only modify constants.py — warn if target_files includes other files
            tuner_only_files = {"constants.py"}
            non_tuner_files = [t for t in targets if Path(t).name not in tuner_only_files]
            if non_tuner_files:
                warnings.append(
                    f"Task {i}: Hyperparameter Tuner targets non-constants file(s) {non_tuner_files}. "
                    f"Tuners should only modify constants.py."
                )
            prompt_lower = prompt.lower()
            # Skip structural keywords that appear in constraint/negative contexts
            _skip_contexts = ("do not", "don't", "must not", "never", "preserve",
                              "keep", "unchanged", "maintain", "no new", "forbidden",
                              "avoid", "except", "aside from", "other than",
                              "should not", "cannot", "do not change", "do not add")
            for kw in _TUNER_STRUCTURAL_PATTERNS:
                # Find the keyword in context — skip if it's in a constraint sentence
                idx = prompt_lower.find(kw)
                if idx >= 0:
                    # Check surrounding context (200 chars before) for negative cues
                    context_before = prompt_lower[max(0, idx - 200):idx]
                    if any(cue in context_before for cue in _skip_contexts):
                        continue
                    # Keyword found in an affirmative (structural) context — warn only
                    warnings.append(
                        f"Task {i} boundary warning: Hyperparameter Tuner prompt contains structural instruction "
                        f"'{kw}' — Tuner should only change numeric constants. "
                        f"The reviewer/critic will enforce this boundary."
                    )
                    break

    # Check target_files overlap between workers (normalized paths)
    # NOTE: Downgraded from error to warning because _execute_workers already handles
    # overlap correctly by running workers sequentially. Rejecting the plan here forces
    # the orchestrator to re-run Master (costing $0.8-1.0 and 3-5 min per attempt),
    # which was the primary cause of the 1800s timeout death spiral.
    all_targets = {}
    for i, task in enumerate(tasks):
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v) if next_v else target.strip()
            if rel in all_targets:
                warnings.append(
                    f"Tasks {all_targets[rel]} and {i} share target_file '{target}' — "
                    f"workers will execute sequentially instead of in parallel. "
                    f"For maximum parallelism, assign different files to each worker."
                )
            else:
                all_targets[rel] = i

    return errors, warnings

class RunMasterInput(TypedDict):
    source_v: Annotated[int, "Source bot version"]
    next_v: Annotated[int, "Target next version"]
    stagnation_info: Annotated[str, "Stagnation context (or 'No stagnation')"]
    match_analysis: Annotated[str, "Match analysis context from run_match_analysis (or '')"]
    performance_verification: Annotated[str, "Performance verification output from run_performance_verification (or '')"]


@tool("run_master", "Run Master Architect analysis to plan the next generation. Returns a task plan with worker assignments.", {"source_v": int, "next_v": int, "stagnation_info": str, "match_analysis": str, "performance_verification": str, "direction_audit": str})
async def run_master(args):
    source_v = args["source_v"]
    next_v = args["next_v"]
    stagnation_info = args.get("stagnation_info", "No stagnation detected. Continue from latest version.")
    match_analysis = args.get("match_analysis", "")
    performance_verification = args.get("performance_verification", "")
    direction_audit_str = args.get("direction_audit", "")

    # Parse direction audit from arg or checkpoint
    direction_audit = None
    if direction_audit_str:
        try:
            direction_audit = json.loads(direction_audit_str) if isinstance(direction_audit_str, str) else direction_audit_str
        except (json.JSONDecodeError, TypeError):
            pass
    if not direction_audit:
        _ckpt = _matching_checkpoint(next_v, source_v)
        direction_audit = _ckpt.get("direction_audit") if _ckpt else None

    # Inject mandatory constraints into performance_verification if audit found repetition
    if direction_audit and direction_audit.get("repetition_detected") and direction_audit.get("mandatory_constraints"):
        constraint_block = (
            f"\n\n# Direction Audit Constraints (MANDATORY)\n"
            f"The Direction Auditor detected that recent generations are stuck repeating the same approach.\n"
            f"**DO NOT repeat these exhausted directions:** {', '.join(direction_audit.get('exhausted_directions', []))}\n"
            f"**Mandatory constraint:** {direction_audit['mandatory_constraints']}\n"
        )
        if direction_audit.get("suggested_direction"):
            constraint_block += f"**Suggested alternative:** {direction_audit['suggested_direction']}\n"
        constraint_block += "\nYou MUST comply with these constraints. A plan that repeats an exhausted direction will be rejected.\n"
        performance_verification = (performance_verification or "") + constraint_block

    ui = _get_ui()
    data = await _run_master_analysis(
        source_v, next_v, stagnation_info, ui,
        match_analysis=match_analysis,
        performance_verification=performance_verification,
    )

    if data is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Master failed to produce a valid plan after 3 retries", "logs": ui.get_output()})}]}

    plan_errors, plan_warnings = _validate_master_plan(data, next_v=next_v)
    if plan_warnings:
        log_system_event("pipeline.master_boundary", "warning",
                         f"Master plan boundary warnings for v{next_v}: {plan_warnings}",
                         {"next_v": next_v, "warnings": plan_warnings})
    if plan_errors:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Master plan validation failed", "validation_errors": plan_errors, "validation_warnings": plan_warnings, "plan": data, "logs": ui.get_output()})}]}

    # Persist master plan to checkpoint so it survives crashes between master and workers
    from evolution_infra import write_pipeline_checkpoint
    _ckpt = _matching_checkpoint(next_v, source_v)
    existing_audit = _ckpt.get("direction_audit") if _ckpt else direction_audit
    # Mark direction_audit as resolved now that Master has produced a plan
    if existing_audit and existing_audit.get("repetition_detected"):
        existing_audit["resolved"] = True
    write_pipeline_checkpoint(next_v, source_v, "master_planned",
                              master_plan=data,
                              direction_audit=existing_audit,
                              worker_invocation_count=_ckpt.get("worker_invocation_count", 0) if _ckpt else 0)

    log_system_event("pipeline.master_done", "info", f"Master planned v{next_v}: {len(data.get('tasks', []))} tasks",
                     {"next_v": next_v, "source_v": source_v, "num_tasks": len(data.get("tasks", []))})

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
            # Selective reset: only revert files modified by violating workers
            src_dir = get_bot_dir(source_v)
            if src_dir.exists() and next_dir.exists():
                violated_files = set()
                for err in boundary_errors:
                    f = err.get("file", "")
                    if f:
                        violated_files.add(f)
                for rel in violated_files:
                    src_file = src_dir / rel
                    dst_file = next_dir / rel
                    if src_file.exists():
                        dst_file.write_text(src_file.read_text())
                    elif dst_file.exists():
                        dst_file.unlink()

    if success:
        from evolution_infra import write_pipeline_checkpoint
        # Preserve the master plan structure (with analysis) from checkpoint,
        # rather than replacing it with the raw tasks list
        plan = ckpt.get("master_plan", tasks) if ckpt else tasks
        write_pipeline_checkpoint(next_v, source_v, "workers_done",
                                  master_plan=plan, reviewer_feedback=reviewer_feedback,
                                  worker_invocation_count=invocation_count + len(tasks))

    sev = "success" if success else "error"
    log_system_event("pipeline.workers_done", sev,
                     f"Workers {'passed' if success else 'failed'} for v{next_v}",
                     {"next_v": next_v, "num_workers": len(tasks), "success": success})

    result = {
        "success": success,
        "boundary_errors": boundary_errors,
        "logs": ui.get_output(),
        "costs": ui.costs,
    }
    return _json_tool_result(result)
