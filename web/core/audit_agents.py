"""LLM audit agents for the evolution pipeline.

Each audit function follows the same pattern:
1. Load prompt template from prompts/ directory
2. Build context data from system state
3. Call run_claude_query() for LLM analysis
4. Parse + validate output against Pydantic schema
5. Return validated dict or safe default on failure

Audits are advisory — failures are silently skipped (safe default returned).
The pipeline never blocks on an audit failure.
"""

import json
import logging
import difflib
from pathlib import Path

from evolution_infra import (
    PROMPTS_DIR, RESULTS_DIR, EXPERIENCE_FILE,
    get_bot_dir, get_logs_dir,
    run_claude_query, parse_json_output, substitute_template,
    _target_rel,
)
from output_schema import validate_agent_output
from system_log import log_system_event

log = logging.getLogger("pok.audit")


# ──────────────────────────────────────────────
# P0-1: Post-Master Plan Verification Audit
# ──────────────────────────────────────────────

async def _run_master_plan_audit(master_plan, source_v, ui):
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
                    ["git", "log", f"bot-v{latest_v}", "-5", "--format=%h %s"],
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

        prompt = substitute_template(template, {
            "master_plan": json.dumps(master_plan, indent=2, ensure_ascii=False),
            "experience_pool": experience_text[:3000] or "No experience pool data",
            "recent_commits": recent_commits or "No recent commits",
            "direction_audit": direction_audit_text,
        })

        log_file = get_logs_dir(source_v) / "master_plan_audit_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            "MASTER_PLAN_AUDIT", log_file,
        )

        data = parse_json_output(output)
        if data:
            data, errors = validate_agent_output("master_plan_auditor", data)
            if errors:
                log.warning("Master plan audit validation: %s", "; ".join(errors[:3]))
                return safe_default
            log.info("Master plan audit: pass=%s, feedback=%s",
                     data.get("overall_pass"), data.get("feedback", "")[:100])
            return data

    except Exception as e:
        log.warning("Master plan audit failed: %s. Skipping.", e)

    return safe_default


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
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if not rel:
                continue
            snapshot_key = (worker_idx, rel)
            before = worker_snapshots.get(snapshot_key, "")
            after_path = next_dir / rel
            after = after_path.read_text() if after_path.exists() else ""
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
        })

        log_file = get_logs_dir(next_v) / f"worker_{w_id}_cot_audit_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            f"WORKER_COT_CHECK_{w_id}", log_file,
        )

        data = parse_json_output(output)
        if data:
            data.setdefault("worker_id", w_id)
            data, errors = validate_agent_output("worker_cot_checker", data)
            if errors:
                log.warning("Worker CoT check validation: %s", "; ".join(errors[:3]))
                return safe_default
            consistent = data.get("cot_consistent", True)
            log.info("Worker %d CoT check: consistent=%s", w_id, consistent)
            if not consistent:
                log_system_event("pipeline.worker_cot_inconsistency", "warn",
                                 f"Worker {w_id} CoT inconsistency: {data.get('discrepancies', [])[:2]}",
                                 {"worker_id": w_id, "discrepancies": data.get("discrepancies", [])[:3]})
            return data

    except Exception as e:
        log.warning("Worker CoT check failed: %s. Skipping.", e)

    return safe_default


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

        data = parse_json_output(output)
        if data and "scenarios" in data:
            data, errors = validate_agent_output("dynamic_test_generator", data)
            if errors:
                log.warning("Dynamic test validation: %s", "; ".join(errors[:3]))
                return safe_default
            scenarios = data.get("scenarios", [])
            log.info("Dynamic test generation: %d scenarios", len(scenarios))
            return scenarios

    except Exception as e:
        log.warning("Dynamic test generation failed: %s. Skipping.", e)

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

        data = parse_json_output(output)
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

    return safe_default


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

        data = parse_json_output(output)
        if data:
            data, errors = validate_agent_output("degeneration_diagnosis", data)
            if errors:
                log.warning("Degeneration diagnosis validation: %s", "; ".join(errors[:3]))
                return safe_default
            return data

    except Exception as e:
        log.warning("Degeneration diagnosis failed: %s. Skipping.", e)

    return safe_default


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

        # Get ratings
        from evolution_infra import load_ratings
        ratings = load_ratings() or {}
        ra = ratings.get(f"claude_v{parent_a_v}", {})
        rb = ratings.get(f"claude_v{parent_b_v}", {})
        rating_a = ra.get("rating", "unknown")
        rating_b = rb.get("rating", "unknown")

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

        data = parse_json_output(output)
        if data:
            data, errors = validate_agent_output("crossover_compatibility", data)
            if errors:
                log.warning("Crossover compatibility validation: %s", "; ".join(errors[:3]))
                return safe_default
            return data

    except Exception as e:
        log.warning("Crossover compatibility audit failed: %s. Skipping.", e)

    return safe_default


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
                    ["git", "log", f"bot-v{latest_v}", "-5", "--format=%h %s"],
                    capture_output=True, text=True, timeout=10,
                    cwd=str(Path(__file__).resolve().parent.parent.parent),
                )
                if result.returncode == 0:
                    recent_outcomes = result.stdout.strip()[:2000]
        except Exception:
            pass

        prompt = substitute_template(template, {
            "pool_content": pool_content[:5000],
            "current_ratings": json.dumps(current_ratings, indent=2, ensure_ascii=False)[:2000] if current_ratings else "No rating data",
            "recent_outcomes": recent_outcomes or "No recent outcomes",
        })

        log_file = RESULTS_DIR / "experience_pool_audit_io.txt"
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            "EXPERIENCE_POOL_AUDIT", log_file,
        )

        data = parse_json_output(output)
        if data:
            data, errors = validate_agent_output("experience_pool_audit", data)
            if errors:
                log.warning("Experience pool audit validation: %s", "; ".join(errors[:3]))
                return safe_default
            return data

    except Exception as e:
        log.warning("Experience pool audit failed: %s. Skipping.", e)

    return safe_default


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

        data = parse_json_output(output)
        if data:
            log.info("Regression guardian: severity=%s, stage=%s",
                     data.get("severity"), data.get("failure_stage"))
            log_system_event("pipeline.regression_guardian", "warn",
                             f"Guardian triggered for v{v}: {data.get('diagnosis', '')[:200]}",
                             {"v": v, "severity": data.get("severity"), "data": data})
            return data

    except Exception as e:
        log.warning("Regression guardian failed: %s. Skipping.", e)

    return safe_default
