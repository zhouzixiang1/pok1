"""LLM audit agents for the evolution pipeline.

Each audit function follows the same pattern:
1. Load prompt template from prompts/ directory
2. Build context data from system state
3. Call run_claude_query() for LLM analysis
4. Parse + validate output against Pydantic schema
5. Return validated dict or safe default on failure

Audit LLM infrastructure/parse failures return safe defaults, but validated audit
results can be used as hard gates by their callers. For example Master plan
audit rejection, crossover compatibility rejection, and high-confidence
precommit semantic regression can block their respective stages.
"""

import json
import logging
import difflib
import asyncio
from pathlib import Path

from bot_namespace import bot_name, bot_tag
from evolution_infra import (
    PROMPTS_DIR, RESULTS_DIR, EXPERIENCE_FILE,
    get_bot_dir, get_logs_dir,
    run_claude_query, parse_json_output, substitute_template,
    _target_rel,
)
from output_schema import validate_agent_output
from system_log import log_system_event
from llm_failure import is_llm_infra_error

log = logging.getLogger("pok.audit")


def _emit_audit_parse_failure(role, failure_mode, fields=None):
    """Emit a classifiable parse-collapse event for an audit agent.

    Audits are advisory and silently return a safe default when the LLM output
    fails to parse. This helper makes the parse collapse visible (root cause 4
    — parse failure collapsing to an opaque default) by emitting an event_bus
    warn with the classifiable failure_mode (NO_JSON/NO_FENCE/PARSE_ERROR/
    EXCEPTION). Logging only — never raises, never changes control flow.
    """
    try:
        from event_bus import warn
        warn(f"pipeline.{role}_parse_failed",
             f"{role} parse failed (mode={failure_mode}); returning safe default (advisory)",
             failure_mode=failure_mode, **(fields or {}))
    except Exception:
        pass


# ──────────────────────────────────────────────
# P0-1: Post-Master Plan Verification Audit
# ──────────────────────────────────────────────

async def _run_master_plan_audit(master_plan, source_v, ui, next_v=None):
    """Verify Master plan coherence and alignment before Workers execute.

    Returns MasterPlanAuditResult dict.
    Safe default: overall_pass=True (non-blocking).
    """
    safe_default = {
        "plan_coherent": True,
        "contradiction_found": False,
        "contradictions": [],
        "experience_alignment": "unrelated",
        "direction_novelty": "novel",
        "overall_pass": True,
        "feedback": "",
        "retry_recommended": False,
    }

    try:
        template = (PROMPTS_DIR / "master_plan_audit.md").read_text()

        # Load experience pool
        experience_text = ""
        if EXPERIENCE_FILE.exists():
            experience_text = EXPERIENCE_FILE.read_text()[:3000]

        # Load recent commit messages (last 5)
        recent_commits = ""
        try:
            from evolution_infra import find_latest_active_v
            latest_v = find_latest_active_v()
            if latest_v:
                import subprocess
                result = subprocess.run(
                    ["git", "log", bot_tag(latest_v), "-5", "--format=%h %s"],
                    capture_output=True, text=True, timeout=10,
                    cwd=str(Path(__file__).resolve().parent.parent.parent),
                )
                if result.returncode == 0:
                    recent_commits = result.stdout.strip()[:2000]
        except Exception:
            pass

        # Load direction audit from checkpoint
        direction_audit_text = "No direction audit available"
        try:
            from evolution_infra import read_pipeline_checkpoint
            ckpt = read_pipeline_checkpoint()
            if ckpt and ckpt.get("direction_audit"):
                da = ckpt["direction_audit"]
                if da.get("repetition_detected"):
                    direction_audit_text = json.dumps(da, indent=2, ensure_ascii=False)
        except Exception:
            pass

        target_v = next_v
        if target_v is None:
            target_v = master_plan.get("next_v") or master_plan.get("target_v") or "unknown"
        try:
            from evidence_snapshot import h2h_snapshot_contract_text
            h2h_snapshot_contract = h2h_snapshot_contract_text(target_v, include_json=True)
        except Exception:
            h2h_snapshot_contract = (
                "Stable H2H snapshot unavailable. Do not compare plan citations "
                "against a live head_to_head.json file that may have changed after planning."
            )

        identity_errors = []
        if next_v is not None:
            for key in ("next_v", "target_v", "version"):
                value = master_plan.get(key)
                if value is not None:
                    try:
                        if int(value) != int(next_v):
                            identity_errors.append(f"{key}=v{value} but checkpoint target is v{next_v}")
                    except Exception:
                        identity_errors.append(f"{key}={value!r} but checkpoint target is v{next_v}")
        for key in ("source_v", "parent_version", "branch_from"):
            value = master_plan.get(key)
            if value is not None:
                try:
                    if int(value) != int(source_v):
                        identity_errors.append(f"{key}=v{value} but checkpoint source is v{source_v}")
                except Exception:
                    identity_errors.append(f"{key}={value!r} but checkpoint source is v{source_v}")
        if identity_errors:
            feedback = "; ".join(identity_errors)
            log_system_event(
                "pipeline.master_plan_identity_mismatch", "error",
                f"Master plan identity mismatch for v{target_v}: {feedback}",
                {"source_v": source_v, "next_v": target_v, "errors": identity_errors},
            )
            return {
                "plan_coherent": False,
                "contradiction_found": True,
                "contradictions": identity_errors,
                "experience_alignment": "misaligned",
                "direction_novelty": "repetitive",
                "overall_pass": False,
                "feedback": feedback,
                "retry_recommended": True,
            }

        prompt = substitute_template(template, {
            "master_plan": json.dumps(master_plan, indent=2, ensure_ascii=False),
            "experience_pool": experience_text[:3000] or "No experience pool data",
            "recent_commits": recent_commits or "No recent commits",
            "direction_audit": direction_audit_text,
            "source_v": str(source_v),
            "next_v": str(target_v),
            "h2h_snapshot_contract": h2h_snapshot_contract,
            "branch_from_note": (
                f"This generation evolves FROM v{source_v}. The source ancestor is "
                f"decided automatically by the system in prepare_generation; the Master "
                f"plan MUST NOT set 'branch_from' (it is a dead, rejected field). Only "
                f"flag a 'data staleness' problem if the plan's analysis references a "
                f"version OTHER than v{source_v} as if it were the evolution base. Do NOT "
                f"reject a plan just because it fixes bugs in v{source_v} that happen to "
                f"already be fixed in a later version — evolution starts from v{source_v}, "
                f"not the latest version."
            ),
        })

        log_version = target_v if isinstance(target_v, int) else source_v
        log_file = get_logs_dir(log_version) / "master_plan_audit_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            "MASTER_PLAN_AUDIT", log_file,
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data, errors = validate_agent_output("master_plan_auditor", data)
            if errors:
                log.warning("Master plan audit validation: %s", "; ".join(errors[:3]))
                return safe_default
            log.info("Master plan audit: pass=%s, feedback=%s",
                     data.get("overall_pass"), data.get("feedback", "")[:100])
            return data

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("Master plan audit failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.master_plan_audit_infra", "warn",
                             f"Master plan audit LLM crashed (infra): {e}",
                             {"source_v": source_v, "error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("master_plan_audit", locals().get("failure_mode", "EXCEPTION"),
                              {"source_v": source_v})
    return {**safe_default, "parse_failed": True}


# ──────────────────────────────────────────────
# P0-2: Worker CoT Reasoning Consistency Check
# ──────────────────────────────────────────────

async def _run_worker_cot_check(task, worker_idx, next_v, source_v, next_dir, worker_snapshots, ui):
    """Check Worker output consistency: claimed changes vs actual diff.

    Returns WorkerCoTCheckResult dict.
    Safe default: cot_consistent=True (non-blocking).
    """
    w_id = task.get("worker_id", worker_idx + 1)
    safe_default = {
        "worker_id": w_id,
        "cot_consistent": True,
        "discrepancies": [],
        "logical_contradictions": [],
        "boundary_violations": [],
        "focus_areas": [],
    }

    try:
        template = (PROMPTS_DIR / "worker_cot_check.md").read_text()

        # Get worker output from log file
        worker_log = get_logs_dir(next_v) / f"worker_{w_id}_io.txt"
        worker_output = ""
        if worker_log.exists():
            worker_output = worker_log.read_text()[-5000:]

        if not worker_output:
            return safe_default

        # Compute diff for this worker's target files using snapshots
        diff_parts = []
        diff_metadata = []
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if not rel:
                continue
            snapshot_key = (worker_idx, rel)
            before = worker_snapshots.get(snapshot_key, "")
            after_path = next_dir / rel
            after = after_path.read_text() if after_path.exists() else ""
            before_lines = len(before.splitlines())
            after_lines = len(after.splitlines())
            diff_metadata.append(
                f"- {rel}: pre-worker snapshot {before_lines} lines; "
                f"post-worker file {after_lines} lines; delta {after_lines - before_lines:+d}"
            )
            if before != after:
                diff = difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"before/{rel}", tofile=f"after/{rel}",
                    n=3,
                )
                diff_text = "".join(diff)
                if diff_text:
                    diff_parts.append(diff_text)

        if not diff_parts:
            return safe_default

        code_diff = "\n".join(diff_parts)[-6000:]

        prompt = substitute_template(template, {
            "worker_role": task.get("role", "Worker"),
            "worker_task": task.get("worker_prompt", task.get("instruction", ""))[:2000],
            "worker_output": worker_output[:3000],
            "code_diff": code_diff,
            "diff_metadata": "\n".join(diff_metadata) or "- no target file metadata",
        })

        log_file = get_logs_dir(next_v) / f"worker_{w_id}_cot_audit_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            f"WORKER_COT_CHECK_{w_id}", log_file,
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data.setdefault("worker_id", w_id)
            data, errors = validate_agent_output("worker_cot_checker", data)
            if errors:
                log.warning("Worker CoT check validation: %s", "; ".join(errors[:3]))
                return safe_default
            consistent = data.get("cot_consistent", True)
            log.info("Worker %s CoT check: consistent=%s", w_id, consistent)
            if not consistent:
                log_system_event("pipeline.worker_cot_inconsistency", "warn",
                                 f"Worker {w_id} CoT inconsistency: {data.get('discrepancies', [])[:2]}",
                                 {"worker_id": w_id, "discrepancies": data.get("discrepancies", [])[:3]})
            return data

    except Exception as e:
        log.warning("Worker CoT check failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.worker_cot_check_infra", "warn",
                             f"Worker {w_id} CoT check LLM crashed (infra): {e}",
                             {"worker_id": w_id, "next_v": next_v, "error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("worker_cot_check", locals().get("failure_mode", "EXCEPTION"),
                              {"worker_id": w_id, "next_v": next_v})
    return {**safe_default, "parse_failed": True}


# ──────────────────────────────────────────────
# P0-3: LLM-Generated Dynamic Decision Tests
# ──────────────────────────────────────────────

async def _generate_dynamic_tests(next_v, source_v, changed_files, master_plan, existing_scenario_ids, ui):
    """Generate dynamic decision test scenarios based on Worker code changes.

    Returns list of scenario dicts.
    Safe default: empty list (non-blocking).
    """
    safe_default = []

    try:
        template = (PROMPTS_DIR / "dynamic_test_generator.md").read_text()

        # Build diff text from changed files
        src_dir = get_bot_dir(source_v)
        next_dir = get_bot_dir(next_v)

        diff_parts = []
        for rel in changed_files:
            src_file = src_dir / rel
            dst_file = next_dir / rel
            before = src_file.read_text() if src_file.exists() else ""
            after = dst_file.read_text() if dst_file.exists() else ""
            if before != after:
                diff = difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"v{source_v}/{rel}", tofile=f"v{next_v}/{rel}",
                    n=3,
                )
                diff_text = "".join(diff)
                if diff_text:
                    diff_parts.append(diff_text)

        if not diff_parts:
            return safe_default

        code_diff = "\n".join(diff_parts)[-6000:]

        tasks_text = json.dumps(
            master_plan.get("tasks", []) if isinstance(master_plan, dict) else [],
            indent=2, ensure_ascii=False
        )[:2000]

        existing_ids_str = ", ".join(existing_scenario_ids) if existing_scenario_ids else "none"

        prompt = substitute_template(template, {
            "changed_files_diff": code_diff,
            "worker_tasks": tasks_text,
            "existing_scenario_ids": existing_ids_str,
        })

        log_file = get_logs_dir(next_v) / "dynamic_test_gen_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            "DYNAMIC_TEST_GEN", log_file,
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data and "scenarios" in data:
            data, errors = validate_agent_output("dynamic_test_generator", data)
            if errors:
                log.warning("Dynamic test validation: %s", "; ".join(errors[:3]))
                return safe_default
            scenarios = data.get("scenarios", [])
            log.info("Dynamic test generation: %d scenarios", len(scenarios))
            return scenarios

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("Dynamic test generation failed: %s. Skipping.", e)
        # Advisory infra telemetry (8th advisory agent): safe_default=[] is non-blocking,
        # but emit a distinct event so the infra crash is observable like its 7 siblings.
        if is_llm_infra_error(e):
            try:
                log_system_event("pipeline.dynamic_test_gen_infra", "warn",
                                 f"Dynamic test generation LLM crashed (infra): {e}",
                                 {"next_v": next_v, "source_v": source_v, "error": str(e)})
            except Exception:
                pass
            if ui:
                ui.log_history(f"DYNAMIC_TEST_GEN: LLM infrastructure error (infra) — using predefined scenarios only: {e}", "warn")

    # Parse collapse (NO_JSON/NO_FENCE/PARSE_ERROR with no exception) or a generic
    # exception skipped the LLM output. Make it visible (root cause 4). The infra
    # branch above already emits its own event, so only emit here when this is a
    # genuine parse failure (failure_mode set by the parser, not an exception path).
    _fm = locals().get("failure_mode")
    if _fm and _fm != "EXCEPTION":
        _emit_audit_parse_failure("dynamic_test_gen", _fm,
                                  {"next_v": next_v, "source_v": source_v})
    return safe_default


# ──────────────────────────────────────────────
# P0-4: Precommit Eval Semantic Interpretation
# ──────────────────────────────────────────────

async def _run_precommit_semantic(v, source_v, matchups, master_plan, ui):
    """Semantic interpretation of precommit eval mirror battle results.

    Returns PrecommitSemanticResult dict.
    Safe default: recommended_action="proceed" (non-blocking).
    """
    safe_default = {
        "win_pattern_analysis": "",
        "top_opponent_assessment": "",
        "regression_semantics": "safe",
        "recommended_action": "proceed",
        "confidence": "low",
    }

    try:
        template = (PROMPTS_DIR / "precommit_semantic.md").read_text()

        # Build matchup results text
        matchup_text = json.dumps(matchups, indent=2, ensure_ascii=False)[:4000]

        # Build H2H context
        h2h_text = ""
        try:
            from evolution_infra import H2H_FILE
            if H2H_FILE.exists():
                h2h_data = json.loads(H2H_FILE.read_text())
                relevant = {}
                v_str = str(v)
                for key, val in h2h_data.items():
                    if v_str in key:
                        relevant[key] = val
                h2h_text = json.dumps(relevant, indent=2, ensure_ascii=False)[:2000]
        except Exception:
            pass

        plan_text = json.dumps(
            master_plan.get("tasks", []) if isinstance(master_plan, dict) else [],
            indent=2, ensure_ascii=False
        )[:2000]

        prompt = substitute_template(template, {
            "matchup_results": matchup_text,
            "master_plan": plan_text,
            "h2h_context": h2h_text or "No H2H data available",
        })

        log_file = get_logs_dir(v) / "precommit_semantic_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            "PRECOMMIT_SEMANTIC", log_file,
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data, errors = validate_agent_output("precommit_semantic", data)
            if errors:
                log.warning("Precommit semantic validation: %s", "; ".join(errors[:3]))
                return safe_default
            action = data.get("recommended_action", "proceed")
            log.info("Precommit semantic: action=%s, confidence=%s", action, data.get("confidence"))
            return data

    except Exception as e:
        log.warning("Precommit semantic analysis failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.precommit_semantic_infra", "warn",
                             f"Precommit semantic v{v} LLM crashed (infra): {e}",
                             {"version": v, "source_v": source_v, "error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("precommit_semantic", locals().get("failure_mode", "EXCEPTION"),
                              {"version": v, "source_v": source_v})
    return {**safe_default, "parse_failed": True}


# ──────────────────────────────────────────────
# P1-1: Continuous Degeneration Diagnosis
# ──────────────────────────────────────────────

async def _run_degeneration_diagnosis(source_v, recent_commits, strategy_changes, rating_curve, ui):
    """Diagnose root cause of continuous rating degeneration.

    Returns DegenerationDiagnosis dict.
    Safe default: is_degenerating=False (non-blocking).
    """
    safe_default = {
        "is_degenerating": False,
        "root_causes": [],
        "commit_evidence": [],
        "strategy_drift_evidence": [],
        "recommendation": "continue",
        "urgent_intervention": False,
    }

    try:
        template = (PROMPTS_DIR / "degeneration_diagnosis.md").read_text()

        prompt = substitute_template(template, {
            "generation_history": recent_commits[:3000],
            "rating_curve": rating_curve[:2000],
            "h2h_changes": "See generation history above",
            "strategy_changes": strategy_changes[:3000],
        })

        log_file = get_logs_dir(source_v) / "degeneration_diagnosis_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            "DEGENERATION_DIAGNOSIS", log_file,
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data, errors = validate_agent_output("degeneration_diagnosis", data)
            if errors:
                log.warning("Degeneration diagnosis validation: %s", "; ".join(errors[:3]))
                return safe_default
            return data

    except Exception as e:
        log.warning("Degeneration diagnosis failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.degeneration_diagnosis_infra", "warn",
                             f"Degeneration diagnosis v{source_v} LLM crashed (infra): {e}",
                             {"source_v": source_v, "error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("degeneration_diagnosis", locals().get("failure_mode", "EXCEPTION"),
                              {"source_v": source_v})
    return {**safe_default, "parse_failed": True}


# ──────────────────────────────────────────────
# P1-3: Crossover Parent Compatibility Audit
# ──────────────────────────────────────────────

async def _run_crossover_compatibility_audit(parent_a_v, parent_b_v, ui):
    """Audit compatibility of two crossover parent bots.

    Returns CrossoverCompatibilityResult dict.
    Safe default: compatible=True (non-blocking).
    """
    safe_default = {
        "compatible": True,
        "compatibility_score": 7,
        "conflict_areas": [],
        "suggested_merge_approach": "",
        "files_to_take_from_a": [],
        "files_to_take_from_b": [],
    }

    try:
        template = (PROMPTS_DIR / "crossover_compatibility.md").read_text()

        # Read core files from both parents
        core_files = ["strategy.py", "postflop.py", "constants.py"]
        parent_a_code = {}
        parent_b_code = {}
        dir_a = get_bot_dir(parent_a_v)
        dir_b = get_bot_dir(parent_b_v)

        for fname in core_files:
            fa = dir_a / fname
            fb = dir_b / fname
            if fa.exists():
                parent_a_code[fname] = fa.read_text()[:4000]
            if fb.exists():
                parent_b_code[fname] = fb.read_text()[:4000]

        # Get ratings (load_ratings returns {name: Glicko2Player} objects)
        from evolution_infra import load_ratings
        ratings = load_ratings() or {}
        ra = ratings.get(bot_name(parent_a_v))
        rb = ratings.get(bot_name(parent_b_v))
        rating_a = f"{ra.rating:.1f}" if ra and hasattr(ra, 'rating') else "unknown"
        rating_b = f"{rb.rating:.1f}" if rb and hasattr(rb, 'rating') else "unknown"

        prompt = substitute_template(template, {
            "parent_a_version": str(parent_a_v),
            "parent_b_version": str(parent_b_v),
            "parent_a_code": json.dumps(parent_a_code, indent=2, ensure_ascii=False)[:5000],
            "parent_b_code": json.dumps(parent_b_code, indent=2, ensure_ascii=False)[:5000],
            "parent_a_rating": str(rating_a),
            "parent_b_rating": str(rating_b),
            "h2h_a_vs_b": "See ratings above",
        })

        log_file = get_logs_dir(parent_a_v) / f"crossover_compat_{parent_a_v}x{parent_b_v}_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            f"CROSSOVER_COMPAT_{parent_a_v}x{parent_b_v}", log_file,
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data, errors = validate_agent_output("crossover_compatibility", data)
            if errors:
                log.warning("Crossover compatibility validation: %s", "; ".join(errors[:3]))
                return safe_default
            return data

    except Exception as e:
        log.warning("Crossover compatibility audit failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.crossover_compat_infra", "warn",
                             f"Crossover compat v{parent_a_v}xv{parent_b_v} LLM crashed (infra): {e}",
                             {"parent_a_v": parent_a_v, "parent_b_v": parent_b_v, "error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("crossover_compatibility", locals().get("failure_mode", "EXCEPTION"),
                              {"parent_a_v": parent_a_v, "parent_b_v": parent_b_v})
    return {**safe_default, "parse_failed": True}


# ──────────────────────────────────────────────
# P1-4: Experience Pool Quality Audit
# ──────────────────────────────────────────────

async def _run_experience_pool_audit(pool_content, current_ratings, ui):
    """Audit experience pool for stale/contradictory entries.

    Returns ExperiencePoolAuditResult dict.
    Safe default: overall_health="healthy" (non-blocking).
    """
    safe_default = {
        "stale_entries": [],
        "contradictions": [],
        "relevance_issues": [],
        "recommended_removals": [],
        "recommended_additions": [],
        "overall_health": "healthy",
    }

    try:
        template = (PROMPTS_DIR / "experience_pool_audit.md").read_text()

        # Get recent outcomes from commit history
        recent_outcomes = ""
        try:
            from evolution_infra import find_latest_active_v
            latest_v = find_latest_active_v()
            if latest_v:
                import subprocess
                result = subprocess.run(
                    ["git", "log", bot_tag(latest_v), "-5", "--format=%h %s"],
                    capture_output=True, text=True, timeout=10,
                    cwd=str(Path(__file__).resolve().parent.parent.parent),
                )
                if result.returncode == 0:
                    recent_outcomes = result.stdout.strip()[:2000]
        except Exception:
            pass

        # Convert Glicko2Player objects to serializable dicts for prompt injection
        serializable_ratings = {}
        for name, r in current_ratings.items():
            if hasattr(r, 'rating'):
                serializable_ratings[name] = {"r": round(r.rating, 1), "rd": round(r.rd, 1)}
            elif isinstance(r, dict):
                serializable_ratings[name] = r
        prompt = substitute_template(template, {
            "pool_content": pool_content[:5000],
            "current_ratings": json.dumps(serializable_ratings, indent=2, ensure_ascii=False)[:2000] if serializable_ratings else "No rating data",
            "recent_outcomes": recent_outcomes or "No recent outcomes",
        })

        log_file = RESULTS_DIR / "experience_pool_audit_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            "EXPERIENCE_POOL_AUDIT", log_file,
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            data, errors = validate_agent_output("experience_pool_audit", data)
            if errors:
                log.warning("Experience pool audit validation: %s", "; ".join(errors[:3]))
                return safe_default
            return data

    except Exception as e:
        log.warning("Experience pool audit failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.experience_pool_audit_infra", "warn",
                             f"Experience pool audit LLM crashed (infra): {e}",
                             {"error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("experience_pool_audit", locals().get("failure_mode", "EXCEPTION"), {})
    return {**safe_default, "parse_failed": True}


# ──────────────────────────────────────────────
# Meta-2: Regression Guardian
# ──────────────────────────────────────────────

async def _run_regression_guardian(v, source_v, pipeline_history, trigger_reason, ui):
    """Independent deep analysis when regression signals are detected.

    Returns dict with diagnosis and recommendations.
    Safe default: empty diagnosis (non-blocking).
    """
    safe_default = {
        "diagnosis": "",
        "failure_stage": "unknown",
        "root_cause": "",
        "systematic_issue": "",
        "recovery_recommendation": "",
        "severity": "minor",
        "confidence": "low",
    }

    try:
        template = (PROMPTS_DIR / "regression_guardian.md").read_text()

        prompt = substitute_template(template, {
            "trigger_reason": trigger_reason[:1000],
            "pipeline_history": json.dumps(pipeline_history, indent=2, ensure_ascii=False)[:4000],
            "rating_trend": "See pipeline history",
            "worker_changes": "See pipeline history",
            "evaluation_results": "See pipeline history",
        })

        log_file = get_logs_dir(v) / "regression_guardian_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            f"REGRESSION_GUARDIAN_v{v}", log_file,
        )

        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
        if data:
            log.info("Regression guardian: severity=%s, stage=%s",
                     data.get("severity"), data.get("failure_stage"))
            log_system_event("pipeline.regression_guardian", "warn",
                             f"Guardian triggered for v{v}: {data.get('diagnosis', '')[:200]}",
                             {"v": v, "severity": data.get("severity"), "data": data})
            return data

    except Exception as e:
        log.warning("Regression guardian failed: %s. Skipping.", e)
        if is_llm_infra_error(e):
            log_system_event("pipeline.regression_guardian_infra", "warn",
                             f"Regression guardian v{v} LLM crashed (infra): {e}",
                             {"v": v, "source_v": source_v, "error": str(e)})
            return {**safe_default, "llm_failed": True}

    # Parse collapse: output failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or an
    # exception skipped the parse. Previously this returned safe_default silently.
    _emit_audit_parse_failure("regression_guardian", locals().get("failure_mode", "EXCEPTION"),
                              {"v": v, "source_v": source_v})
    return {**safe_default, "parse_failed": True}
