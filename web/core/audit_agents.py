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
import hashlib
import logging
import difflib
import asyncio
import stat
from pathlib import Path

from bot_namespace import bot_name, bot_tag
from evolution_infra import (
    PROMPTS_DIR, RESULTS_DIR,
    get_bot_dir, get_logs_dir,
    run_claude_query, parse_json_output, substitute_template,
    _target_rel,
)
from output_schema import validate_agent_output
from system_log import log_system_event
from llm_failure import is_llm_infra_error
from llm_availability import LLMAvailabilityBlocked
from worker_boundary import is_binary_artifact_path, read_regular_file_bytes

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
        "evidence_alignment": "unrelated",
        "direction_novelty": "novel",
        "overall_pass": True,
        "feedback": "",
        "retry_recommended": False,
    }

    try:
        template = (PROMPTS_DIR / "master_plan_audit.md").read_text()

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
            h2h_snapshot_contract = h2h_snapshot_contract_text(
                target_v, source_v=source_v, include_json=True
            )
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
                "evidence_alignment": "misaligned",
                "direction_novelty": "repetitive",
                "overall_pass": False,
                "feedback": feedback,
                "retry_recommended": True,
            }

        prompt = substitute_template(template, {
            "master_plan": json.dumps(master_plan, indent=2, ensure_ascii=False),
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
    except LLMAvailabilityBlocked:
        # A provider/billing stop must remain attempt-neutral.  Returning the
        # advisory safe default here would incorrectly consume the audit and
        # allow the generation to advance after resume.
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

def _cot_after_file_state(path):
    """Return a safe text-or-bytes state for Worker CoT evidence."""
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError:
        return False, "missing", b""
    if not stat.S_ISREG(metadata.st_mode):
        return False, "invalid", b""
    try:
        data = read_regular_file_bytes(path.parent, path, metadata)
    except OSError:
        return False, "invalid", b""
    if is_binary_artifact_path(path):
        return True, "binary", data
    try:
        return True, "text", data.decode("utf-8")
    except UnicodeDecodeError:
        return True, "binary", data


def _cot_binary_metadata(label, present, content):
    if not present:
        return f"{label}: missing"
    data = content if isinstance(content, bytes) else str(content).encode("utf-8")
    return (
        f"{label}: {len(data)} bytes, sha256={hashlib.sha256(data).hexdigest()}"
    )

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
            before_present = snapshot_key in worker_snapshots
            before = worker_snapshots.get(snapshot_key, "")
            after_path = next_dir / rel
            after_present, after_kind, after = _cot_after_file_state(after_path)
            binary_evidence = isinstance(before, bytes) or after_kind in {
                "binary", "invalid"
            }
            if binary_evidence:
                before_meta = _cot_binary_metadata(
                    "before", before_present, before
                )
                after_meta = _cot_binary_metadata(
                    "after", after_present, after
                )
                changed = not before_present or not after_present or before != after
                diff_metadata.append(
                    f"- {rel}: binary artifact; {before_meta}; {after_meta}; "
                    f"changed={str(changed).lower()}"
                )
                if changed:
                    diff_parts.append(
                        f"--- before/{rel} (binary metadata)\n"
                        f"+++ after/{rel} (binary metadata)\n"
                        f"-{before_meta}\n+{after_meta}\n"
                    )
                continue

            before_text = str(before) if before_present else ""
            after_text = str(after) if after_present else ""
            before_lines = len(before_text.splitlines())
            after_lines = len(after_text.splitlines())
            diff_metadata.append(
                f"- {rel}: pre-worker snapshot {before_lines} lines; "
                f"post-worker file {after_lines} lines; delta {after_lines - before_lines:+d}"
            )
            if not before_present or not after_present or before_text != after_text:
                diff = difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
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

    except LLMAvailabilityBlocked:
        raise
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

# ──────────────────────────────────────────────
# P0-4: Precommit Eval Semantic Interpretation
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
            "h2h_changes": (
                "No per-opponent H2H delta rows were supplied to this advisory "
                "role; opponent-specific attribution is unknown."
            ),
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

    except LLMAvailabilityBlocked:
        # Provider availability is global control flow, not an advisory
        # no-degeneration judgement.  The prepare stage must remain byte- and
        # attempt-neutral until the exact pause receipt is reconciled.
        raise
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

async def _run_crossover_compatibility_audit(
    parent_a_v,
    parent_b_v,
    ui,
    *,
    target_v=None,
    architecture_context=None,
):
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

        # policy.py is the sole candidate-owned source artifact.  Runtime and
        # precompute bytes are system-owned and never crossover inputs.
        core_files = ["policy.py"]
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

        rating_a = "unknown"
        rating_b = "unknown"
        h2h_context = "Stable H2H snapshot unavailable. Treat matchup strength as unknown."
        if target_v is not None:
            try:
                from evidence_snapshot import load_generation_evaluation_snapshot
                from evolution_infra import pair_key

                frozen = load_generation_evaluation_snapshot(target_v)
                if not frozen.get("available"):
                    raise RuntimeError(
                        f"generation snapshot unavailable: {frozen.get('reason')}"
                    )
                ratings = frozen.get("ratings") or {}
                ra = ratings.get(bot_name(parent_a_v)) or {}
                rb = ratings.get(bot_name(parent_b_v)) or {}
                if isinstance(ra, dict) and ra:
                    rating_a = f"{float(ra.get('r', 1500)):.1f} ± {float(ra.get('rd', 350)):.1f}"
                if isinstance(rb, dict) and rb:
                    rating_b = f"{float(rb.get('r', 1500)):.1f} ± {float(rb.get('rd', 350)):.1f}"
                h2h = frozen.get("h2h") or {}
                key = pair_key(bot_name(parent_a_v), bot_name(parent_b_v))
                row = h2h.get(key) if isinstance(h2h, dict) else None
                if isinstance(row, dict):
                    h2h_context = (
                        f"{key}: games={int(row.get('games', 0) or 0)}, "
                        f"a_wins={int(row.get('a_wins', 0) or 0)}, "
                        f"b_wins={int(row.get('b_wins', 0) or 0)}, "
                        f"draws={int(row.get('draws', 0) or 0)}"
                    )
                else:
                    h2h_context = f"Stable snapshot has no row for {key}; matchup is sparse/unknown."
            except Exception as exc:
                h2h_context = f"Stable H2H snapshot read failed: {type(exc).__name__}: {str(exc)[:160]}"

        prompt = substitute_template(template, {
            "parent_a_version": str(parent_a_v),
            "parent_b_version": str(parent_b_v),
            "parent_a_code": json.dumps(parent_a_code, indent=2, ensure_ascii=False)[:5000],
            "parent_b_code": json.dumps(parent_b_code, indent=2, ensure_ascii=False)[:5000],
            "parent_a_rating": str(rating_a),
            "parent_b_rating": str(rating_b),
            "h2h_a_vs_b": h2h_context,
            "architecture_context": json.dumps(
                architecture_context or {},
                indent=2,
                ensure_ascii=False,
            )[:8000],
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

    except LLMAvailabilityBlocked:
        raise
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
