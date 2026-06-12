"""Pipeline tools: direction audit, master planning, and worker execution."""

import json
import shutil
import time
from pathlib import Path
from typing import Annotated, TypedDict

from claude_agent_sdk import tool

from logging_config import get_logger
_log = get_logger("planning")

from evolution_core import (
    get_bot_dir,
    _run_master_analysis,
    _run_direction_audit,
    _execute_workers,
    EXPERIENCE_FILE,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _state_blocked,
    _validate_worker_boundaries,
    _target_rel, _py_files_changed_between, _resolve_version_args,
    PROJECT_ROOT,
    _set_pipeline_status,
)
from system_log import log_system_event


# ──────────────────────────────────────────────
# Direction Audit Stage (pre-Master)
# ──────────────────────────────────────────────

@tool("run_direction_audit", "Audit recent generation directions for repetition. Returns exhausted directions and mandatory constraints for the Master.", {"source_v": int, "next_v": int})
async def run_direction_audit(args):
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return _json_tool_result({"error": "Missing source_v/next_v and no active checkpoint"})

    _set_pipeline_status(f"Auditing directions for v{next_v}")

    # Cache guard: skip LLM call if already completed for this (next_v, source_v)
    _existing = _matching_checkpoint(next_v, source_v)
    if _existing and _existing.get("stage") == "direction_audited" and _existing.get("direction_audit"):
        ui = _get_ui()
        ui.log_history("Direction audit: using cached result (already completed)", "info")
        return _json_tool_result({
            "direction_audit": _existing["direction_audit"],
            "logs": ui.get_output(),
        })

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
        worker_failure_count=_ckpt.get("worker_failure_count", 0) if _ckpt else 0,
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
            # Tuners MUST only modify constants.py — error if target_files includes other files.
            # This prevents the shared-file boundary validation false positive (Bug 1)
            # where two workers target the same file, causing all changes to be incorrectly
            # reverted as a Tuner boundary violation.
            tuner_only_files = {"constants.py"}
            non_tuner_files = [t for t in targets if Path(t).name not in tuner_only_files]
            if non_tuner_files:
                errors.append(
                    f"Task {i}: Hyperparameter Tuner targets non-constants file(s) {non_tuner_files}. "
                    f"Tuners MUST only modify constants.py. Assign {non_tuner_files} to a Logic Architect task."
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

    # Check target_files overlap between workers.
    # Architect-Tuner overlap on any file is a hard error (causes boundary false positives).
    # Other overlaps are informational — workers execute sequentially so overlap is safe,
    # but different files make each worker's scope clearer.
    architect_targets = {}
    tuner_targets = {}
    all_targets = {}
    for i, task in enumerate(tasks):
        role = str(task.get("role", "")).lower()
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v) if next_v else target.strip()
            if "architect" in role:
                architect_targets.setdefault(rel, []).append(i)
            elif "tuner" in role or "hyperparameter" in role:
                tuner_targets.setdefault(rel, []).append(i)
            if rel in all_targets:
                warnings.append(
                    f"Tasks {all_targets[rel]} and {i} share target_file '{target}'. "
                    f"This is safe (sequential execution) but consider splitting for clarity."
                )
            else:
                all_targets[rel] = i

    # Hard error: Architect and Tuner sharing any file causes boundary validation
    # false positives because the Tuner check sees the Architect's structural changes.
    overlap = set(architect_targets.keys()) & set(tuner_targets.keys())
    if overlap:
        errors.append(
            f"Architect and Tuner share target file(s): {sorted(overlap)}. "
            f"This causes boundary validation false positives (Tuner check sees Architect's changes). "
            f"Assign constants.py to Tuner only; other files go to Architect."
        )

    # Check worker prompts against exhausted directions from experience pool.
    # This is a HARD constraint: plans matching exhausted directions are rejected.
    exhausted_keywords = _extract_exhausted_keywords()
    if exhausted_keywords:
        for i, task in enumerate(tasks):
            prompt_text = (
                task.get("worker_prompt", "")
                + " " + task.get("instruction", "")
                + " " + str(task.get("targeted_failure", ""))
            ).lower()
            if _fuzzy_match_exhausted(prompt_text, exhausted_keywords):
                errors.append(
                    f"Task {i}: worker prompt matches an EXHAUSTED direction from experience pool. "
                    f"This direction has been repeatedly tried with no measurable improvement. "
                    f"Choose a fundamentally different approach."
                )
                break  # one match is enough to block the plan

    return errors, warnings

class RunMasterInput(TypedDict):
    source_v: Annotated[int, "Source bot version"]
    next_v: Annotated[int, "Target next version"]
    stagnation_info: Annotated[str, "Stagnation context (or 'No stagnation')"]
    match_analysis: Annotated[str, "Match analysis context from run_match_analysis (or '')"]
    performance_verification: Annotated[str, "Performance verification output from run_performance_verification (or '')"]


@tool("run_master", "Run Master Architect analysis to plan the next generation. Returns a task plan with worker assignments.", {"source_v": int, "next_v": int, "stagnation_info": str, "match_analysis": str, "performance_verification": str, "direction_audit": str})
async def run_master(args):
    _t0 = time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing source_v/next_v and no active checkpoint"})}]}
    stagnation_info = args.get("stagnation_info", "No stagnation detected. Continue from latest version.")
    match_analysis = args.get("match_analysis", "")
    performance_verification = args.get("performance_verification", "")
    direction_audit_str = args.get("direction_audit", "")

    _set_pipeline_status(f"Master planning for v{next_v}")

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

    # --- Extract replay_spotlight for Master prompt ---
    replay_spotlight = ""
    try:
        from generation_scheduler import GenerationContext
        # replay_spotlight is computed in prepare_generation() and stored in
        # GenerationContext, but the MCP tool layer doesn't have direct access
        # to the gen_ctx object. Re-compute from the replay files instead.
        from replay_spotlight import find_critical_hands
        from evolution_infra import RESULTS_DIR
        replays_dir = str(RESULTS_DIR / "match_replay")
        replay_spotlight = find_critical_hands(
            bot_name=f"claude_v{source_v}",
            replays_dir=replays_dir,
            max_hands=10,
            recent_n_files=20,
        )
    except Exception:
        pass

    # --- Read bot_action_stats for Master prompt ---
    bot_action_stats = ""
    try:
        from evolution_infra import RESULTS_DIR
        _stats_file = RESULTS_DIR / "bot_action_stats.json"
        if _stats_file.exists():
            import json as _json
            with open(_stats_file, "r") as _f:
                _all_stats = _json.load(_f)
            _bot_stats = _all_stats.get(f"claude_v{source_v}")
            if _bot_stats:
                # Format as compact text for prompt injection
                _parts = []
                for _street in ("preflop", "flop", "turn", "river"):
                    _st = _bot_stats.get(_street)
                    if _st and _st.get("total", 0) > 0:
                        _total = _st["total"]
                        _parts.append(
                            f"{_street}: fold={_st.get('fold', 0)/_total:.1%} "
                            f"call={_st.get('call', 0)/_total:.1%} "
                            f"raise={_st.get('raise', 0)/_total:.1%} "
                            f"(n={_total})"
                        )
                if _parts:
                    bot_action_stats = (
                        f"Action frequencies for claude_v{source_v}:\n"
                        + "\n".join(_parts)
                    )
    except Exception:
        pass

    data = await _run_master_analysis(
        source_v, next_v, stagnation_info, ui,
        match_analysis=match_analysis,
        performance_verification=performance_verification,
        replay_spotlight=replay_spotlight,
        bot_action_stats=bot_action_stats,
    )

    if data is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Master failed to produce a valid plan after 3 retries", "logs": ui.get_output()})}]}

    # --- P0-1: Post-Master Plan Verification Audit ---
    master_audit_ctx = None
    try:
        from audit_agents import _run_master_plan_audit
        audit_result = await _run_master_plan_audit(data, source_v, ui)
        master_audit_ctx = audit_result  # Save for audit_context chain
        if not audit_result.get("overall_pass", True):
            log_system_event("pipeline.master_audit_rejected", "warn",
                             f"Master plan audit rejected for v{next_v}: {audit_result.get('feedback', '')[:200]}",
                             {"next_v": next_v, "audit": audit_result})
            if audit_result.get("retry_recommended"):
                # Inject contradictions and re-run Master once
                performance_verification += (
                    f"\n\n# PLAN AUDIT REJECTION\n"
                    f"The previous plan was rejected by the Plan Verification Auditor.\n"
                    f"Issues: {audit_result.get('feedback', '')}\n"
                    f"Contradictions: {', '.join(audit_result.get('contradictions', []))}\n"
                    f"Direction assessment: {audit_result.get('direction_novelty', 'unknown')}\n"
                    f"You MUST address these issues in your new plan.\n"
                )
                data = await _run_master_analysis(
                    source_v, next_v, stagnation_info, ui,
                    match_analysis=match_analysis,
                    performance_verification=performance_verification,
                    replay_spotlight=replay_spotlight,
                    bot_action_stats=bot_action_stats,
                )
                if data is None:
                    return {"content": [{"type": "text", "text": json.dumps({"error": "Master failed after audit retry", "logs": ui.get_output()})}]}
                log_system_event("pipeline.master_audit_retry", "info",
                                 f"Master re-planned after audit rejection for v{next_v}",
                                 {"next_v": next_v})
    except Exception as e:
        _log.warning("Master plan audit error (skipping): %s", e)
        try:
            log_system_event('pipeline.master_audit_error', 'warn',
                f'Master plan audit error for v{next_v}: {e}',
                {"next_v": next_v, "source_v": source_v, "error": str(e)})
        except Exception:
            pass

    plan_errors, plan_warnings = _validate_master_plan(data, next_v=next_v)
    if plan_warnings:
        try:
            log_system_event("pipeline.master_boundary", "warning",
                             f"Master plan boundary warnings for v{next_v}: {plan_warnings}",
                             {"next_v": next_v, "warnings": plan_warnings})
        except Exception:
            pass
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
                              worker_failure_count=_ckpt.get("worker_failure_count", 0) if _ckpt else 0,
                              audit_context={"master_audit": master_audit_ctx} if master_audit_ctx else None)

    try:
        log_system_event("pipeline.master_done", "info", f"Master planned v{next_v}: {len(data.get('tasks', []))} tasks",
                         {"next_v": next_v, "source_v": source_v, "num_tasks": len(data.get("tasks", [])),
                          "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    result = {"plan": data, "logs": ui.get_output()}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


# ──────────────────────────────────────────────
# Worker Stage
# ──────────────────────────────────────────────

def _extract_exhausted_keywords():
    """Extract focused topic keywords from EXHAUSTED experience pool entries.

    For each [POSSIBLY EXHAUSTED] line, extracts:
    1. The section header (e.g., OPPONENT_MODELING, PARAMETER_TUNING)
    2. A cleaned short phrase (first clause before the explanation)
    Returns a list of (section, phrase) tuples for fuzzy matching.
    Returns an empty list if the file doesn't exist or has no EXHAUSTED entries.
    """
    if not EXPERIENCE_FILE.exists():
        return []
    try:
        text = EXPERIENCE_FILE.read_text(encoding="utf-8")
    except Exception:
        return []

    keywords = []
    current_section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            current_section = line.replace("## ", "").strip()
            continue
        if "[POSSIBLY EXHAUSTED]" not in line:
            continue
        # Extract the topic phrase: everything before the explanation
        cleaned = line.replace("[POSSIBLY EXHAUSTED]", "").strip(" -•")
        if not cleaned:
            continue
        # Take the first clause (before common joiners) as the core topic
        for sep in [" are exhausted", " has not ", " have repeatedly ", " shows "]:
            if sep in cleaned:
                cleaned = cleaned[:cleaned.index(sep)]
                break
        keywords.append((current_section.lower(), cleaned.lower()))
    return keywords


def _fuzzy_match_exhausted(prompt_text: str, keywords: list) -> bool:
    """Check if prompt_text matches an EXHAUSTED keyword using fuzzy token matching.

    For each (section, phrase) pair:
    1. Extract alphanumeric tokens (len > 3) from both the phrase and the section name
    2. Check how many tokens appear in the prompt
    3. Match if >=2 distinctive tokens from either the phrase OR section match

    This catches prompts like "Adjust fold threshold" matching the PARAMETER_TUNING
    EXHAUSTED entry about "fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr".
    """
    import re
    prompt_clean = re.sub(r'[^a-z0-9\s]', '', prompt_text.lower())
    prompt_tokens = set(prompt_clean.split())

    for section, phrase in keywords:
        # Extract tokens from both section name and topic phrase
        phrase_tokens = set(re.sub(r'[^a-z0-9]', '', t) for t in phrase.split()
                            if len(re.sub(r'[^a-z0-9]', '', t)) > 3)
        section_tokens = set(re.sub(r'[^a-z0-9]', '', t) for t in section.split()
                             if len(re.sub(r'[^a-z0-9]', '', t)) > 3)
        all_tokens = phrase_tokens | section_tokens

        if not all_tokens:
            continue

        # Count matching tokens
        matches = sum(1 for t in all_tokens if t in prompt_clean)
        # Match if >=2 distinctive tokens OR >=40% of tokens match
        if matches >= min(2, len(all_tokens)) and matches >= len(all_tokens) * 0.25:
            return True
    return False


class ExecuteWorkersInput(TypedDict):
    tasks: Annotated[list, "List of worker task dicts from Master plan"]
    next_v: Annotated[int, "Target bot version"]
    source_v: Annotated[int, "Source bot version"]
    reviewer_feedback: Annotated[str, "Previous reviewer feedback (or '')"]


@tool("execute_workers", "Execute worker tasks to modify bot code. Each task has worker_id, role, target_files, worker_prompt.", {"tasks": list, "next_v": int, "source_v": int, "reviewer_feedback": str})
async def execute_workers(args):
    _t0 = time.time()
    tasks = args.get("tasks", [])
    next_v = args.get("next_v")
    source_v = args.get("source_v")
    if next_v is None or source_v is None:
        next_v, source_v = _resolve_version_args(args)
    if next_v is None or source_v is None:
        return _json_tool_result({"error": "Missing next_v/source_v and no active checkpoint"})
    reviewer_feedback = args.get("reviewer_feedback", "")

    _set_pipeline_status(f"Executing workers for v{next_v}")

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

    # Fallback: if tasks not provided, load from checkpoint master_plan.
    # This happens when the orchestrator session is fresh (not resumed) and
    # the LLM doesn't have the task list in its conversation history.
    if not tasks:
        tasks = ckpt["master_plan"].get("tasks", [])
        if tasks:
            log_system_event("pipeline.workers_tasks_from_checkpoint", "info",
                             f"Tasks loaded from checkpoint for v{next_v} (LLM omitted tasks arg)",
                             {"next_v": next_v, "num_tasks": len(tasks)})
        else:
            return _json_tool_result({
                "error": "No tasks provided and checkpoint has no task plan. Call run_master first.",
                "next_v": next_v,
                "source_v": source_v,
            })

    # Circuit breaker: limit total worker failures per generation
    # Backward compat: old checkpoints used worker_invocation_count instead of worker_failure_count
    failure_count = ckpt.get("worker_failure_count", ckpt.get("worker_invocation_count", 0))
    MAX_WORKER_FAILURES = 6
    if failure_count >= MAX_WORKER_FAILURES:
        try:
            log_system_event('pipeline.circuit_breaker', 'error',
                f'Circuit breaker: {failure_count} worker failures',
                {'next_v': next_v, 'source_v': source_v, 'failure_count': failure_count})
        except Exception:
            pass
        return _json_tool_result({
            "error": f"CIRCUIT BREAKER: {failure_count} worker failures already recorded this generation (max {MAX_WORKER_FAILURES}). Abandon this generation and start a new one.",
            "failure_count": failure_count,
            "next_v": next_v,
            "source_v": source_v,
        })

    # When critic has rejected, force re-planning on 2nd+ rejection.
    # Re-using the same plan that the critic already rejected guarantees repeated failure.
    generation_attempt = ckpt.get("generation_attempt", 0)
    if reviewer_feedback and generation_attempt >= 1:
        return _json_tool_result({
            "error": f"generation_attempt={generation_attempt}. The critic rejected the plan {generation_attempt} time(s). "
                     f"You MUST call run_master first to generate a NEW plan incorporating the critic feedback, "
                     f"then call execute_workers with the new plan.",
            "require_new_plan": True,
            "generation_attempt": generation_attempt,
            "next_v": next_v,
            "source_v": source_v,
        })

    # When retrying after workers already ran, actually reset code from source first.
    # Previous claim that code was reset was FALSE — now we actually do it.
    if reviewer_feedback and ckpt.get("stage") in ("workers_done", "reviewed", "critic_checked"):
        import shutil
        source_dir_r = get_bot_dir(source_v)
        if source_dir_r.exists() and next_dir.exists():
            _log.info(f"Resetting v{next_v} code from source v{source_v} before worker retry")
            # Remove all files except .completed
            for item in next_dir.iterdir():
                if item.name != ".completed":
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
            # Copy fresh source code (skip .completed, __pycache__, .pyc)
            for item in source_dir_r.iterdir():
                if item.name in (".completed", "__pycache__"):
                    continue
                if item.suffix == ".pyc":
                    continue
                if item.is_dir():
                    shutil.copytree(item, next_dir / item.name,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    shutil.copy2(item, next_dir / item.name)

        # Re-apply known fixes after resetting from source (source may be older/unfixed)
        from fix_injection import apply_known_fixes, log_fix_application
        applied, skipped = apply_known_fixes(next_dir)
        if applied or skipped:
            log_fix_application(applied, skipped, next_dir, source_v)

        # Write intermediate checkpoint so pipeline state reflects the in-progress retry.
        # Without this, a crash between code reset and worker execution would leave
        # the checkpoint at a stale stage (e.g. "reviewed" or "critic_checked")
        # while the actual code has been wiped back to source.
        from evolution_infra import write_pipeline_checkpoint
        write_pipeline_checkpoint(next_v, source_v, "master_planned",
                                  master_plan=ckpt.get("master_plan"),
                                  reviewer_feedback=reviewer_feedback,
                                  worker_failure_count=ckpt.get("worker_failure_count", 0))

        reviewer_feedback += (
            f"\n\nNOTE: This is a retry. The code in bots/claude_v{next_v}/ has been ACTUALLY RESET "
            f"from source bots/claude_v{source_v}/. Any modifications described in the feedback "
            f"above no longer exist in the code — you must re-implement them from scratch."
        )

    # P2: Validate worker prompts against EXHAUSTED directions from experience pool.
    # If a worker_prompt contains keywords matching an EXHAUSTED direction, append
    # a prominent warning. This is a safety net — the primary enforcement is the
    # <forbidden_directions> block injected by agent_workers.py.
    exhausted_keywords = _extract_exhausted_keywords()
    if exhausted_keywords:
        for task in tasks:
            prompt_text = task.get("worker_prompt", task.get("instruction", "")).lower()
            if _fuzzy_match_exhausted(prompt_text, exhausted_keywords):
                original = task.get("worker_prompt", task.get("instruction", ""))
                task["worker_prompt"] = (
                    original +
                    "\n\n⚠️ WARNING: This task may violate an EXHAUSTED direction. "
                    "Verify carefully — the experience pool marks this area as exhausted "
                    "with no measurable H2H gain. Consider an alternative approach."
                )
                log_system_event(
                    "pipeline.worker_exhausted_warning", "warn",
                    f"Worker {task.get('worker_id', '?')} prompt matches EXHAUSTED direction",
                    {"next_v": next_v},
                )
                break  # One warning per task is sufficient

    ui = _get_ui()
    success, worker_snapshots, audit_focus_areas = await _execute_workers(
        tasks, worker_template, next_dir, next_v,
        [], ui, reviewer_feedback=reviewer_feedback,
        source_v=source_v,
    )

    boundary_errors = []
    if success:
        # Pre-gate: check that code actually changed before proceeding to quality gates.
        # This catches zero-change workers early, saving Reviewer + Critic LLM calls.
        src_dir = get_bot_dir(source_v)
        if src_dir.exists() and next_dir.exists():
            changed = [p for p in _py_files_changed_between(src_dir, next_dir) if 'backup' not in p]
            if not changed:
                success = False
                log_system_event("pipeline.workers_zero_changes", "error",
                                 f"Workers reported success but zero .py files changed for v{next_v}",
                                 {"next_v": next_v, "source_v": source_v})

    if success:
        boundary_errors = _validate_worker_boundaries(tasks, source_v, next_v,
                                                          worker_snapshots=worker_snapshots)
        if boundary_errors:
            success = False
            # Selective reset: only revert files modified by violating workers
            src_dir = get_bot_dir(source_v)
            if src_dir.exists() and next_dir.exists():
                violated_files = set()
                for err in boundary_errors:
                    # Only revert files from hyperparameter boundary violations.
                    # target_file_violation and new_file_violation are logged but should
                    # not trigger selective reset — they may flag files that the Architect
                    # correctly modified outside declared targets.
                    if err.get("type") == "hyperparameter_boundary_violation":
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
                # After resetting files, check if ANY .py files still differ.
                # If not, the code is back to source state — revert checkpoint stage
                # so the orchestrator knows workers need to re-run from scratch.
                remaining_changes = [p for p in _py_files_changed_between(src_dir, next_dir) if 'backup' not in p]
                if not remaining_changes:
                    # All changes were reset — code is identical to source.
                    # Do NOT advance checkpoint to workers_done.
                    log_system_event("pipeline.workers_all_reset", "warn",
                                     f"All worker changes reset for v{next_v} — code identical to v{source_v}",
                                     {"next_v": next_v, "source_v": source_v})

    if success:
        from evolution_infra import write_pipeline_checkpoint
        # Preserve the master plan structure (with analysis) from checkpoint,
        # rather than replacing it with the raw tasks list
        plan = ckpt.get("master_plan", tasks) if ckpt else tasks
        write_pipeline_checkpoint(next_v, source_v, "workers_done",
                                  master_plan=plan, reviewer_feedback=reviewer_feedback,
                                  worker_failure_count=failure_count)
    else:
        # Increment failure count on worker failure; successful batches do not consume the budget.
        # Always set stage to 'master_planned' on failure — this clearly indicates
        # that workers need re-execution, rather than preserving a stale stage
        # from before the failure (e.g. "reviewed" or "critic_checked").
        from evolution_infra import write_pipeline_checkpoint
        plan = ckpt.get("master_plan", tasks) if ckpt else tasks
        write_pipeline_checkpoint(next_v, source_v,
                                  "master_planned",
                                  master_plan=plan, reviewer_feedback=reviewer_feedback,
                                  worker_failure_count=failure_count + 1)

    sev = "success" if success else "error"
    log_system_event("pipeline.workers_done", sev,
                     f"Workers {'passed' if success else 'failed'} for v{next_v}",
                     {"next_v": next_v, "num_workers": len(tasks), "success": success,
                      "elapsed_sec": round(time.time() - _t0, 2)})

    result = {
        "success": success,
        "boundary_errors": boundary_errors,
        "logs": ui.get_output(),
        "costs": ui.costs,
    }
    return _json_tool_result(result)
