"""Pipeline tools: direction audit, master planning, and worker execution."""

import json
import re
import shutil
import time
from pathlib import Path

from claude_agent_sdk import tool

from logging_config import get_logger
_log = get_logger("planning")

from evolution_core import (
    get_bot_dir,
    _run_master_analysis,
    _run_direction_audit,
    _execute_workers,
    EXPERIENCE_FILE,
    write_pipeline_checkpoint,
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


def _validate_master_plan(plan, next_v=None, precomputed_exhausted_keywords=None):
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
    exhausted_keywords = precomputed_exhausted_keywords if precomputed_exhausted_keywords is not None else _extract_exhausted_keywords()
    if exhausted_keywords:
        for i, task in enumerate(tasks):
            prompt_text = (
                task.get("worker_prompt", "")
                + " " + task.get("instruction", "")
                + " " + str(task.get("targeted_failure", ""))
            ).lower()
            if _fuzzy_match_exhausted(prompt_text, exhausted_keywords, require_direction_token=True):
                errors.append(
                    f"Task {i}: worker prompt matches an EXHAUSTED direction from experience pool. "
                    f"This direction has been repeatedly tried with no measurable improvement. "
                    f"Choose a fundamentally different approach."
                )
                break  # one match is enough to block the plan

    return errors, warnings


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

    # Cross-gen mechanical backstop: inject prior critic local-optima rejections
    # + experience-pool EXHAUSTED directions directly into performance_verification,
    # independent of the direction_audit LLM gate (which historically under-detects
    # — v82 repetition_detected=false despite the pool flagging constant-tuning
    # EXHAUSTED). No-op when there is no prior critic local-optima rejection and
    # no EXHAUSTED direction (first-ever gen / clean crossover unaffected).
    # Idempotent: guarded by CROSS_GEN_MARKER so run_master retries don't stack it.
    _cross_gen_block = _build_cross_gen_constraint_block(next_v)
    if _cross_gen_block and CROSS_GEN_MARKER not in (performance_verification or ""):
        performance_verification = (performance_verification or "") + _cross_gen_block

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
            with open(_stats_file, "r") as _f:
                _all_stats = json.load(_f)
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

    # --- Read battle experience for Master prompt ---
    battle_experience = ""
    try:
        from battle_experience import get_battle_experience
        battle_experience = get_battle_experience()
    except Exception:
        pass

    # --- Read exploitability probe results for Master prompt ---
    # exploitability.json is written by exploitability_prober.run_exploitability_probes()
    # (called from generation_scheduler.post_generation_cleanup against the
    # PREVIOUS generation's bot). It is write-only until consumed here.
    exploitability_weaknesses = ""
    try:
        from evolution_infra import RESULTS_DIR as _RES
        _exploit_file = _RES / "exploitability.json"
        if _exploit_file.exists():
            with open(_exploit_file, "r") as _f:
                _exploit = json.load(_f)
            _overall = _exploit.get("overall_score")
            _weak_list = _exploit.get("weaknesses", []) or []
            _games = _exploit.get("num_hands")
            _bot_path = _exploit.get("bot_path", "")
            _source_bot = f"claude_v{source_v}"
            # Stale-safe: only inject the cached probe data if it was actually
            # run for the CURRENT source bot. The post-gen probe refreshes this
            # file per generation; if it hasn't run the file holds stale data for
            # a DIFFERENT bot — injecting that would mislabel another bot's
            # weaknesses as this bot's (active misinformation into Master).
            # Stale-safe + reliability gate (defense in depth):
            # (a) Inject cached data ONLY when the cached bot_path's parent dir
            #     is EXACTLY the current source bot. A substring match
            #     (_source_bot in _bot_path) would mis-fire on e.g. claude_v80
            #     vs claude_v800, and a cached result for a DIFFERENT bot would
            #     mislabel another bot's weaknesses as this bot's (active
            #     misinformation into Master).
            # (b) Require enough hands per probe to be statistically meaningful.
            #     A tiny sample (e.g. a 2-hand diagnostic run) yields near-random
            #     win_rates that would inject noise into the Master's direction.
            _MIN_RELIABLE_PROBE_GAMES = 30
            _cached_bot = Path(_bot_path).parent.name if _bot_path else ""
            _reliable = _games is None or int(_games) >= _MIN_RELIABLE_PROBE_GAMES
            if _bot_path and _cached_bot != _source_bot:
                exploitability_weaknesses = (
                    f"No fresh exploitability probe data for {_source_bot} "
                    f"(cached result is for a different bot: {_bot_path})."
                )
            elif not _reliable:
                exploitability_weaknesses = (
                    f"Exploitability probe data for {_source_bot} is unreliable "
                    f"(only {_games} games/probe, need >= {_MIN_RELIABLE_PROBE_GAMES}). "
                    f"Treating as no data."
                )
                _log.warning("exploitability probe unreliable: %s hands for %s",
                             _games, _source_bot)
            else:
                _parts = []
                if _overall is not None:
                    _parts.append(f"overall_score={_overall:.2f}/1.0")
                if _games is not None:
                    _parts.append(f"{int(_games)} games per probe")
                if _bot_path:
                    _parts.append(f"vs {_bot_path}")
                header = ("Exploitability probe results (4 probe bots: min_bettor, "
                          "overbettor, check_raiser, always_caller): "
                          + ", ".join(_parts)) if _parts else (
                          "Exploitability probe results (4 probe bots):")
                if _weak_list:
                    exploitability_weaknesses = header + "\nWEAKNESSES:\n- " + "\n- ".join(_weak_list)
                else:
                    exploitability_weaknesses = header + "\nNo exploitable weaknesses detected."
    except Exception as e:
        # Never silent: a parse/read failure here used to swallow the whole
        # block. Log it so a corrupt/missing exploitability.json stays observable.
        _log.warning("Exploitability probe read failed for source_v=%s: %s", source_v, e)

    data = await _run_master_analysis(
        source_v, next_v, stagnation_info, ui,
        match_analysis=match_analysis,
        performance_verification=performance_verification,
        replay_spotlight=replay_spotlight,
        bot_action_stats=bot_action_stats,
        battle_experience=battle_experience,
        exploitability_weaknesses=exploitability_weaknesses,
    )

    if data is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Master failed to produce a valid plan after 3 retries", "logs": ui.get_output()})}]}

    # --- P0-1: Post-Master Plan Verification Audit ---
    # Capped retry loop: on audit rejection with retry_recommended, re-plan AND
    # re-audit the new plan, up to MAX_MASTER_AUDIT_RETRIES. The audit_attempt
    # counter is persisted in the checkpoint so a crash-resume does not re-burn
    # the master LLM budget (bug #6b). Without this cap, a persistently-rejecting
    # auditor + an orchestrator that re-calls run_master forms a token-burning
    # loop that consumes the whole CYCLE_TIMEOUT (observed: v81 stuck 3x run_master).
    master_audit_ctx = None
    MAX_MASTER_AUDIT_RETRIES = 2
    try:
        from audit_agents import _run_master_plan_audit
        from evolution_infra import read_pipeline_checkpoint
        _ckpt0 = read_pipeline_checkpoint() or {}
        # `or 0` defends against a stored null: prepare_next_gen writes the
        # checkpoint with audit_attempt=None (default), and across the next_v
        # change the merge guard fails so it serializes as JSON null. A bare
        # .get("audit_attempt", 0) returns the stored None (not the default),
        # and int(None) raises TypeError that the surrounding try/except would
        # swallow — silently disabling the audit on every normal generation.
        _audit_attempt = int(_ckpt0.get("audit_attempt") or 0)

        for _audit_iter in range(MAX_MASTER_AUDIT_RETRIES + 1):
            audit_result = await _run_master_plan_audit(data, source_v, ui)
            master_audit_ctx = audit_result  # Save for audit_context chain
            if audit_result.get("overall_pass", True):
                break  # plan passed audit
            # Rejected
            log_system_event("pipeline.master_audit_rejected", "warn",
                             f"Master plan audit rejected for v{next_v} (attempt {_audit_attempt + 1}): {audit_result.get('feedback', '')[:200]}",
                             {"next_v": next_v, "audit": audit_result, "audit_attempt": _audit_attempt + 1})
            if not audit_result.get("retry_recommended"):
                break  # advisory-only rejection: accept the plan as-is
            if _audit_attempt + 1 > MAX_MASTER_AUDIT_RETRIES:
                log_system_event("pipeline.master_audit_exhausted", "warn",
                                 f"Master audit exhausted {MAX_MASTER_AUDIT_RETRIES} retries for v{next_v} — accepting plan to avoid retry loop",
                                 {"next_v": next_v})
                break
            # Re-plan with rejection feedback, then re-audit the new plan
            _audit_attempt += 1
            performance_verification += (
                f"\n\n# PLAN AUDIT REJECTION (attempt {_audit_attempt})\n"
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
                battle_experience=battle_experience,
                exploitability_weaknesses=exploitability_weaknesses,
            )
            if data is None:
                return {"content": [{"type": "text", "text": json.dumps({"error": "Master failed after audit retry", "logs": ui.get_output()})}]}
            # Persist audit_attempt so crash-resume resumes at this count (not 0)
            try:
                write_pipeline_checkpoint(next_v, source_v, "master_planned",
                                          master_plan=data, audit_attempt=_audit_attempt)
            except Exception:
                pass
            log_system_event("pipeline.master_audit_retry", "info",
                             f"Master re-planned after audit rejection for v{next_v} (attempt {_audit_attempt})",
                             {"next_v": next_v})
    except Exception as e:
        _log.warning("Master plan audit error (skipping): %s", e)
        try:
            log_system_event('pipeline.master_audit_error', 'warn',
                f'Master plan audit error for v{next_v}: {e}',
                {"next_v": next_v, "source_v": source_v, "error": str(e)})
        except Exception:
            pass

    # Pre-compute exhausted keywords once (used by _validate_master_plan and potentially others)
    _exhausted_kw = _extract_exhausted_keywords()
    plan_errors, plan_warnings = _validate_master_plan(data, next_v=next_v, precomputed_exhausted_keywords=_exhausted_kw)
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
    _ckpt = _matching_checkpoint(next_v, source_v)
    existing_audit = _ckpt.get("direction_audit") if _ckpt else direction_audit
    # Mark direction_audit as resolved now that Master has produced a plan
    if existing_audit and existing_audit.get("repetition_detected"):
        existing_audit["resolved"] = True
    write_pipeline_checkpoint(next_v, source_v, "master_planned",
                              master_plan=data,
                              direction_audit=existing_audit,
                              worker_failure_count=_ckpt.get("worker_failure_count", 0) if _ckpt else 0,
                              audit_context={"master_audit": master_audit_ctx} if master_audit_ctx else None,
                              reset_generation_attempt=True,
                              reset_audit_attempt=True)

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
    # Tolerant marker: matches [POSSIBLY EXHAUSTED] AND [EXHAUSTED — hard gate]
    # (any bracketed tag containing the word EXHAUSTED). Using a regex avoids the
    # round-trip closure bug where an LLM-appended "— hard gate" suffix made the
    # old literal "[POSSIBLY EXHAUSTED]" check silently miss every marker,
    # disabling the exhausted-direction hard gate (returned []  -> gate no-op).
    marker_re = re.compile(r"\[[A-Z ]*EXHAUSTED[^\]]*\]")
    for line in text.splitlines():
        if line.startswith("## "):
            current_section = line.replace("## ", "").strip()
            continue
        if not marker_re.search(line):
            continue
        # Skip non-direction sections: RECENT_LESSONS holds free-form critic
        # commentary (e.g. a 1188-char v82 review dump) that can contain an
        # inline [POSSIBLY EXHAUSTED] reference but is NOT a direction — extracted
        # verbatim it becomes a parasitic 84-token keyword that matches almost
        # any plan. Only top-level strategy sections hold real directions.
        if current_section.upper() == "RECENT_LESSONS":
            continue
        # Extract the topic phrase: everything before the explanation
        cleaned = marker_re.sub("", line).strip(" -•")
        if not cleaned:
            continue
        # Length cap: a genuine direction phrase is a clause, not a paragraph.
        # Real directions run ~300-400 chars; over-long entries (e.g. a 1188-char
        # critic-review dump) are commentary, not directions. 500 keeps real
        # directions while excluding dumps.
        if len(cleaned) > 500:
            continue
        # Take the first clause (before common joiners) as the core topic
        for sep in [" are exhausted", " has not ", " have repeatedly ", " shows "]:
            if sep in cleaned:
                cleaned = cleaned[:cleaned.index(sep)]
                break
        keywords.append((current_section.lower(), cleaned.lower()))
    return keywords


# Generic poker action/street vocabulary that appears in almost every plan.
# Excluded from "distinctive" token matching so that a legitimate novel plan
# mentioning fold/call/sizing isn't falsely flagged as repeating an EXHAUSTED
# direction. Only direction-characteristic words (parameter/tuning/structural/
# commitment/barrel/archetype/...) count as distinctive.
_EXHAUSTED_BLOCKLIST = frozenset({
    "fold", "call", "raise", "bet", "bets", "check", "allin", "pot",
    "sizing", "threshold", "thresholds", "margin", "margins",
    "equity", "hand", "hands", "street", "streets",
    "flop", "turn", "river", "preflop", "postflop",
})


# Direction-characteristic tokens that uniquely identify an EXHAUSTED direction
# (as opposed to generic poker vocabulary). The HARD gate (_validate_master_plan)
# additionally requires >=1 of these in the prompt so a legitimate novel plan
# that merely shares generic strategy words (value/strategy/strong/tier/...) is
# not falsely rejected. Excludes constant/margin/fold/grounded — too generic,
# they appear in legitimate opponent-stat / continuous-stat reframes (the very
# reframe v82's critic asked for).
_EXHAUSTED_DIRECTION_TOKENS = frozenset({
    "parameter", "tuning", "mechanism", "canonical", "archetype",
    "commitment", "refactor", "continuous",
})


def _fuzzy_match_exhausted(prompt_text: str, keywords: list, require_direction_token: bool = False) -> bool:
    """Check if prompt_text matches an EXHAUSTED keyword using fuzzy token matching.

    Distinctive tokens EXCLUDE generic poker vocabulary (_EXHAUSTED_BLOCKLIST) so
    that a legitimate novel plan isn't rejected just for mentioning fold/call/
    sizing. A match requires >=2 distinctive tokens (the BLOCKLIST makes "2
    tokens" meaningful — direction-characteristic words, not fold/call/sizing).

    When require_direction_token=True (HARD gate in _validate_master_plan), also
    requires >=1 _EXHAUSTED_DIRECTION_TOKEN in the prompt. This eliminates the
    remaining false-positive class where a long EXHAUSTED prose entry shares
    generic words (value/strategy/strong/tier) with a legitimate novel plan,
    without losing true positives (a real fold-gate reintroduction mentions
    mechanism/canonical/archetype; a real constant-tuning plan mentions
    parameter/tuning). The soft warning path (execute_workers) keeps the default
    False to preserve recall — warnings are cheap.
    """
    import re
    prompt_clean = re.sub(r'[^a-z0-9\s]', '', prompt_text.lower())

    for section, phrase in keywords:
        distinctive = set()
        for src in (phrase, section):
            # Split on any non-alphanumeric (spaces, underscores, slashes, dashes)
            # so 'parameter_tuning' and 'constant/margin' tokenize the same way
            # the prompt does ('parameter tuning', 'constant margin').
            for t in re.split(r'[^a-z0-9]+', src):
                if len(t) > 3 and t not in _EXHAUSTED_BLOCKLIST:
                    distinctive.add(t)
        if not distinctive:
            continue
        matches = sum(1 for t in distinctive if t in prompt_clean)
        # Match on >=2 distinctive (non-generic) tokens; for very short keywords
        # (<=2 distinctive, e.g. a bare section name) require all to match.
        if matches < min(2, len(distinctive)):
            continue
        # HARD gate: additionally require a direction-characteristic token, so a
        # plan that merely shares generic words (value/strategy/strong/tier) is
        # not rejected.
        if require_direction_token:
            direction_hits = sum(1 for t in _EXHAUSTED_DIRECTION_TOKENS if t in prompt_clean)
            if direction_hits < 1:
                continue
        return True
    return False


# ──────────────────────────────────────────────
# Cross-generation local-optima constraint (mechanical backstop)
# ──────────────────────────────────────────────
# When the previous generation was rejected by the Critic as a local optimum,
# or the experience pool marks a direction EXHAUSTED, inject a hard constraint
# into the Master so it stops re-proposing the same exhausted direction
# (observed: v82 master re-proposed constant-tuning after critic rejected it
# for exactly that). This is independent of the direction_audit LLM gate
# (which historically under-detects — v82 repetition_detected=false despite
# the pool flagging constant-tuning EXHAUSTED), so it works even when the
# LLM auditor fails to flag repetition.

CROSS_GEN_MARKER = "# CROSS-GEN LOCAL-OPTIMA CONSTRAINT"


def _load_recent_critic_local_optima(next_v, max_entries=3):
    """Load recent critic local-optima rejections from worker_failures.jsonl.

    The file is append-only and accumulates across generations. Selects critic
    entries with local_optima_warning=True and gen <= next_v (the just-rejected
    version is included — that is the loop we want to break), dedups by gen
    (latest timestamp wins; retry_workers can reject the same gen repeatedly).

    Returns [(gen, reason, error_first_line), ...] most-recent-gen first.

    _record_quality_failure (tool_gates.py) filters `v is not False`, so
    local_optima_warning=False is never written — `is True` selection is exact,
    and reviewer/worker records (which lack the field) are skipped.
    """
    try:
        from evolution_infra import WORKER_FAILURES_FILE, locked_file
    except Exception:
        return []
    if not WORKER_FAILURES_FILE.exists():
        return []
    by_gen = {}
    try:
        with locked_file(WORKER_FAILURES_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("local_optima_warning") is not True:
                    continue
                if str(e.get("worker_id", "")) != "critic":
                    continue
                g = e.get("gen")
                if g is None or g > next_v:
                    continue
                ts = e.get("timestamp", 0)
                if g not in by_gen or ts > by_gen[g][3]:
                    reason = (e.get("local_optima_reason") or "").strip()
                    err_first = (e.get("error", "")).split("\n")[0][:150]
                    by_gen[g] = (g, reason, err_first, ts)
    except Exception:
        return []
    return [t[:3] for t in sorted(by_gen.values(), key=lambda x: -x[0])][:max_entries]


def _build_cross_gen_constraint_block(next_v):
    """Build a cross-generation mandatory constraint block from prior critic
    local-optima rejections + experience-pool EXHAUSTED directions.

    Returns "" (no injection) when there is neither a recent critic local-optima
    rejection nor any EXHAUSTED direction — so first-ever generations and
    crossovers with no prior rejection are unaffected.

    Wording is deliberately NOT an unconditional FORBIDDEN: a Master that brings
    a structural new method + H2H evidence may still proceed in the direction,
    and legitimate opponent-stat-driven sizing (the very reframe v82's critic
    asked for) is explicitly permitted — this prevents over-generalized refusal.
    """
    lo_entries = _load_recent_critic_local_optima(next_v)
    exhausted = _extract_exhausted_keywords()
    if not lo_entries and not exhausted:
        return ""
    parts = [f"\n\n{CROSS_GEN_MARKER} (MANDATORY)\n"]
    if lo_entries:
        parts.append(
            "The PREVIOUS generation(s) were REJECTED by the Critic as a LOCAL OPTIMUM "
            "(stuck repeating the same exhausted direction). To proceed in that same "
            "direction you MUST provide a STRUCTURAL new method AND H2H evidence "
            "(>=100g vs a confirmed weak matchup); pure constant/margin tuning will be "
            "rejected again.\n"
            "Recent critic local-optima rejections:\n"
        )
        for g, reason, err_short in lo_entries:
            parts.append(f"- v{g}: {reason or err_short}\n")
    if exhausted:
        parts.append(
            "\nDirections the experience pool marks EXHAUSTED (tried repeatedly, no gain):\n"
        )
        for sec, phrase in exhausted:
            parts.append(f"- [{sec}] {phrase}\n")
    parts.append(
        "\nUnless you have a genuinely structural alternative + H2H evidence, AVOID these "
        "exact patterns. Do NOT over-generalize: legitimate opponent-stat-driven sizing, "
        "new decision systems, or structural refactors are still permitted and encouraged.\n"
    )
    return "".join(parts)



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
        # Preserve the master plan structure (with analysis) from checkpoint,
        # rather than replacing it with the raw tasks list
        plan = ckpt.get("master_plan", tasks) if ckpt else tasks
        # Store audit_focus_areas in audit_context so reviewer can read them
        _audit_ctx = None
        if audit_focus_areas:
            _existing_audit = ckpt.get("audit_context", {}) if ckpt else {}
            _audit_ctx = {**_existing_audit, "worker_cot_focus_areas": audit_focus_areas}
        write_pipeline_checkpoint(next_v, source_v, "workers_done",
                                  master_plan=plan, reviewer_feedback=reviewer_feedback,
                                  worker_failure_count=failure_count,
                                  audit_context=_audit_ctx)
    else:
        # Increment failure count on worker failure; successful batches do not consume the budget.
        # Always set stage to 'master_planned' on failure — this clearly indicates
        # that workers need re-execution, rather than preserving a stale stage
        # from before the failure (e.g. "reviewed" or "critic_checked").
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
        "audit_focus_areas": audit_focus_areas,
    }
    return _json_tool_result(result)
