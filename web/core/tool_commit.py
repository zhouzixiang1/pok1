"""Pipeline tools: commit, archivist, and crossover."""

import json
import os
import time
from typing import Annotated, TypedDict

from logging_config import get_logger
_log = get_logger("commit")

from bot_namespace import bot_name, bot_tag
from tool_runtime_guard import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
    load_ratings,
    git_commit_bot,
    git_has_tag,
    git_dir_is_committed,
    clear_pipeline_checkpoint,
    RESULTS_DIR,
    MAX_ACTIVE_BOTS,
    _run_crossover,
    locked_file,
    EXPERIENCE_FILE,
    ARCHIVE_DIR,
    write_pipeline_checkpoint,
    archive_generation,
    archive_rotate_files,
    archive_old_logs,
)
from evolution_infra import (
    _git,
    _git_ensure_main_branch,
    evolution_git_push_enabled,
    evolution_git_push_required,
    git_push_refs,
    publish_runtime_expected_head,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _resolve_version_args,
    PROJECT_ROOT,
    _set_pipeline_status,
    compute_h2h_avg_winrate, _load_h2h_data,
    read_pipeline_checkpoint,
    _py_files_changed_between,
    _execute_exhausted_infrastructure_failure,
    _owned_infrastructure_failure,
    _record_infrastructure_failure,
)
from system_log import log_system_event
from pipeline_infrastructure import infrastructure_failure_digest
from blocking_runtime import run_blocking_isolated

# ──────────────────────────────────────────────
# Commit Stage
# ──────────────────────────────────────────────

class CommitBotInput(TypedDict):
    version: Annotated[int, "Bot version to commit"]
    source_v: Annotated[int, "Parent version"]
    strategy: Annotated[str, "Strategy description"]
    review_approved: Annotated[bool, "Must be true — confirms run_review() returned approved:true"]


def _existing_local_bot_tag_matches_certificate(version, certificate):
    """Validate a local commit/tag left behind by an interrupted required push."""
    tag = bot_tag(version)
    if not git_has_tag(version) or not git_dir_is_committed(version):
        return False, "local tag or committed bot directory is missing"
    expected = {
        "official-certificate": str(certificate.get("certificate_digest") or ""),
        "official-candidate-hash": str(certificate.get("candidate_hash") or ""),
        "official-policy": str(certificate.get("policy_id") or ""),
    }
    certificate_path = f"official_certificates/{bot_name(version)}.json"
    from bot_artifact import validate_completion_tag

    validation = validate_completion_tag(
        get_bot_dir(version),
        expected_metadata=expected,
        certificate_path=certificate_path,
    )
    if not validation.get("valid"):
        return False, ", ".join(validation.get("issues") or [f"invalid {tag}"])
    return True, ""


def _push_existing_bot_refs(version):
    refs = ["main", bot_tag(version)]
    high_water = f"national-high-water-v{int(version)}"
    if _git("tag", "-l", high_water, check=False).strip():
        refs.append(high_water)
    ok = git_push_refs(*refs)
    publish_runtime_expected_head("bot_commit_push_retry", version=version)
    return ok


def _position_semantics_failed_gate(errors: list[str]) -> dict:
    return {
        "passed": False,
        "all_passed": False,
        "critical_scenarios_passed": False,
        "position_semantics_ok": False,
        "position_semantics_errors": errors[:10],
        "failed_gates": [
            f"position_semantics({'; '.join(err[:120] for err in errors[:3])})"
        ],
    }


def _position_semantics_feedback(errors: list[str]) -> str:
    return "Quality gates failed: " + "; ".join(
        f"position_semantics({err})" for err in errors[:6]
    )


def validate_commit_gate_ledger(v, source_v, ckpt, bot_dir=None):
    """Validate the gate ledger and code fingerprint for finalizing a bot.

    This is intentionally shared by normal ``commit_bot`` and bare-commit
    recovery. Recovery must not tag code unless the current files still match
    the exact code that passed quality and precommit.
    """
    v = int(v)
    source_v = int(source_v) if source_v is not None else None
    bot_dir = bot_dir or get_bot_dir(v)
    try:
        from tool_gates import _bot_code_fingerprint
        current_code_fingerprint = _bot_code_fingerprint(bot_dir)
    except Exception:
        current_code_fingerprint = ""

    missing_gates = []
    failed_gates = []
    gate_results = {}
    if not ckpt:
        missing_gates.append("pipeline_checkpoint")
    else:
        try:
            from workflow_profiles import get_workflow_profile
            workflow_profile = get_workflow_profile()
            expected_profile_id = getattr(workflow_profile, "profile_id", "")
            expected_execution_mode = getattr(workflow_profile, "national_execution_mode", "adapter")
            expected_evaluation_protocol = getattr(workflow_profile, "evaluation_protocol", "local_json")
        except Exception:
            expected_profile_id = ""
            expected_execution_mode = ""
            expected_evaluation_protocol = ""
        checkpoint_profile_id = str(ckpt.get("workflow_profile_id") or "")
        checkpoint_execution_mode = str(ckpt.get("national_execution_mode") or "")
        if expected_profile_id and checkpoint_profile_id and checkpoint_profile_id != expected_profile_id:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "workflow_profile_id mismatch",
                "expected": expected_profile_id,
                "current": checkpoint_profile_id,
            })
        if expected_execution_mode and checkpoint_execution_mode and checkpoint_execution_mode != expected_execution_mode:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "national_execution_mode mismatch",
                "expected": expected_execution_mode,
                "current": checkpoint_execution_mode,
            })
        gate_results = ckpt.get("gate_results", {}) or {}
        if source_v is not None and int(ckpt.get("source_v") or -1) != source_v:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "source_v mismatch",
                "expected": source_v,
                "current": ckpt.get("source_v"),
            })
        if int(ckpt.get("next_v") or -1) != v:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "next_v mismatch",
                "expected": v,
                "current": ckpt.get("next_v"),
            })
        if not current_code_fingerprint:
            failed_gates.append({
                "gate": "code_fingerprint",
                "reason": "current candidate code fingerprint is unavailable",
                "path": str(bot_dir),
            })

        quality = gate_results.get("quality")
        if not quality:
            missing_gates.append("quality")
        else:
            quality_profile_id = str(quality.get("workflow_profile_id") or quality.get("profile_id") or "")
            quality_execution_mode = str(quality.get("national_execution_mode") or "")
            if expected_profile_id and quality_profile_id != expected_profile_id:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "workflow_profile_id mismatch",
                    "expected": expected_profile_id,
                    "current": quality_profile_id or "missing",
                })
            if expected_execution_mode and quality_execution_mode != expected_execution_mode:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "national_execution_mode mismatch",
                    "expected": expected_execution_mode,
                    "current": quality_execution_mode or "missing",
                })
            if expected_execution_mode == "native_tcp" and quality.get("national_native_contract_ok") is not True:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "national native TCP contract did not pass",
                    "value": quality.get("national_native_contract_ok"),
                })
            if quality.get("all_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "all_passed is not true", "value": quality})
            if quality.get("critical_scenarios_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "critical_scenarios_passed is not true", "value": quality})
            quality_fingerprint = quality.get("code_fingerprint")
            if not quality_fingerprint:
                missing_gates.append("quality_code_fingerprint")
            elif current_code_fingerprint and quality_fingerprint != current_code_fingerprint:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "code_fingerprint changed since quality gates",
                    "expected": quality_fingerprint,
                    "current": current_code_fingerprint,
                })
            if expected_execution_mode == "native_tcp":
                try:
                    from national_runtime_probe import (
                        RUNTIME_PROBE_LIMITS_DIGEST,
                        RUNTIME_PROBE_IDENTITY_DIGEST,
                        RUNTIME_PROBE_ORCHESTRATOR_VERSION,
                        RUNTIME_PROBE_SCENARIO_DIGEST,
                        RUNTIME_PROBE_SCHEMA_VERSION,
                    )
                    from runtime_architecture_policy import (
                        runtime_contract_ledger_digest,
                        validate_runtime_contract_ledger,
                    )

                    checkpoint_ledger = ckpt.get("runtime_contract_ledger")
                    plan_ledger = (
                        (ckpt.get("master_plan") or {}).get("runtime_contract_ledger")
                        if isinstance(ckpt.get("master_plan"), dict)
                        else None
                    )
                    ledger_errors = [
                        *(f"checkpoint:{item}" for item in validate_runtime_contract_ledger(checkpoint_ledger)),
                        *(f"master_plan:{item}" for item in validate_runtime_contract_ledger(plan_ledger)),
                    ]
                    checkpoint_ledger_digest = runtime_contract_ledger_digest(checkpoint_ledger)
                    plan_ledger_digest = runtime_contract_ledger_digest(plan_ledger)
                    if checkpoint_ledger_digest != plan_ledger_digest:
                        ledger_errors.append("checkpoint_master_plan_ledger_digest_mismatch")
                    if ledger_errors:
                        failed_gates.append({
                            "gate": "runtime_contract_identity",
                            "reason": "runtime contract ledger is invalid",
                            "errors": ledger_errors[:10],
                        })
                    expected_runtime_identity = {
                        "runtime_contract_ledger_digest": checkpoint_ledger_digest,
                        "runtime_probe_schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
                        "runtime_probe_orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
                        "runtime_probe_scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
                        "runtime_probe_limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
                        "runtime_probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
                    }
                    mismatches = {
                        key: {"expected": value, "quality": quality.get(key)}
                        for key, value in expected_runtime_identity.items()
                        if quality.get(key) != value
                    }
                    if mismatches:
                        failed_gates.append({
                            "gate": "runtime_probe_identity",
                            "reason": "quality evidence does not match current runtime probe/ledger identity",
                            "mismatches": mismatches,
                        })
                except Exception as exc:
                    failed_gates.append({
                        "gate": "runtime_probe_identity",
                        "reason": f"identity validation error: {type(exc).__name__}: {str(exc)[:200]}",
                    })

        review = gate_results.get("review")
        if not review:
            missing_gates.append("review")
        elif review.get("approved") is not True:
            failed_gates.append({"gate": "review", "reason": "reviewer did not approve", "value": review})

        critic = gate_results.get("critic")
        if not critic:
            missing_gates.append("critic")
        elif critic.get("approved") is not True:
            failed_gates.append({
                "gate": "critic",
                "reason": "critic advisory role did not complete successfully",
                "value": critic,
            })

        precommit = gate_results.get("precommit_eval")
        if not precommit:
            missing_gates.append("precommit_eval")
        elif precommit.get("passed") is not True:
            failed_gates.append({"gate": "precommit_eval", "reason": "precommit eval did not pass", "value": precommit})
        else:
            precommit_profile_id = str(precommit.get("workflow_profile_id") or precommit.get("profile_id") or "")
            precommit_execution_mode = str(precommit.get("national_execution_mode") or "")
            if expected_profile_id and precommit_profile_id != expected_profile_id:
                failed_gates.append({
                    "gate": "precommit_eval",
                    "reason": "workflow_profile_id mismatch",
                    "expected": expected_profile_id,
                    "current": precommit_profile_id or "missing",
                })
            if expected_execution_mode and precommit_execution_mode != expected_execution_mode:
                failed_gates.append({
                    "gate": "precommit_eval",
                    "reason": "national_execution_mode mismatch",
                    "expected": expected_execution_mode,
                    "current": precommit_execution_mode or "missing",
                })
            precommit_fingerprint = precommit.get("code_fingerprint")
            if not precommit_fingerprint:
                missing_gates.append("precommit_code_fingerprint")
            elif current_code_fingerprint and precommit_fingerprint != current_code_fingerprint:
                failed_gates.append({
                    "gate": "precommit_eval",
                    "reason": "code_fingerprint changed since precommit eval",
                    "expected": precommit_fingerprint,
                    "current": current_code_fingerprint,
                })
            if expected_execution_mode == "native_tcp":
                try:
                    from precommit_eval_contract import (
                        validate_evaluation_contract,
                        validate_precommit_plan,
                    )

                    precommit_plan = (
                        (ckpt.get("audit_context") or {}).get("precommit_eval_plan")
                    )
                    plan_issues = validate_precommit_plan(
                        precommit_plan,
                        candidate_version=v,
                        source_version=source_v,
                        profile_id=expected_profile_id,
                        execution_mode=expected_execution_mode,
                        evaluation_protocol=expected_evaluation_protocol,
                    )
                    contract_issues = (
                        validate_evaluation_contract(
                            precommit.get("precommit_eval_contract"),
                            precommit_plan,
                            candidate_code_fingerprint=current_code_fingerprint,
                        )
                        if not plan_issues
                        else []
                    )
                    contract = precommit.get("precommit_eval_contract") or {}
                    if precommit.get("precommit_eval_contract_digest") != contract.get("contract_digest"):
                        contract_issues.append("precommit_evaluation_contract_digest_mismatch")
                    if plan_issues or contract_issues:
                        failed_gates.append({
                            "gate": "precommit_eval_contract",
                            "reason": "frozen precommit evaluator/opponent contract is invalid or drifted",
                            "errors": [*plan_issues, *contract_issues][:12],
                        })
                except Exception as exc:
                    failed_gates.append({
                        "gate": "precommit_eval_contract",
                        "reason": (
                            "precommit contract validation error: "
                            f"{type(exc).__name__}: {str(exc)[:200]}"
                        ),
                    })

        if expected_execution_mode == "native_tcp":
            try:
                from national_native import check_native_contract
                native_contract_errors = check_native_contract(
                    bot_dir,
                    require_current_stream_decoder=True,
                    require_current_decision_runtime=True,
                )
            except Exception as exc:
                native_contract_errors = [f"{type(exc).__name__}: {str(exc)[:200]}"]
            if native_contract_errors:
                failed_gates.append({
                    "gate": "native_contract",
                    "reason": "candidate is not a valid native national TCP bot",
                    "errors": native_contract_errors[:5],
                })
            try:
                from national_position_contract import detect_position_semantics_errors
                position_errors = detect_position_semantics_errors(bot_dir)
            except Exception as exc:
                position_errors = [f"position_contract_check_error: {type(exc).__name__}: {str(exc)[:200]}"]
            if position_errors:
                failed_gates.append({
                    "gate": "position_semantics",
                    "reason": "candidate violates national heads-up position semantics",
                    "errors": position_errors[:10],
                })

    return {
        "ok": not missing_gates and not failed_gates,
        "missing_gates": missing_gates,
        "failed_gates": failed_gates,
        "gate_results": gate_results,
        "current_code_fingerprint": current_code_fingerprint,
        "checkpoint_stage": ckpt.get("stage") if ckpt else None,
    }


def _checkpoint_execution_mode(ckpt, gate_results) -> str:
    if ckpt:
        mode = str(ckpt.get("national_execution_mode") or "")
        if mode:
            return mode
    for gate_name in ("quality", "precommit_eval"):
        gate = (gate_results or {}).get(gate_name) or {}
        mode = str(gate.get("national_execution_mode") or "")
        if mode:
            return mode
    return ""


def _truthy_env(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on", "required"}


def _official_preferred_opponent() -> str | None:
    return os.environ.get("POK_OFFICIAL_OPPONENT", "").strip() or None


def _official_gate_feedback(official_full_gate: dict) -> str:
    """Build bounded official-full feedback for checkpoint repair/investigation."""
    status = official_full_gate.get("status") or {}
    verdict = official_full_gate.get("verdict") or {}
    evidence_summary = official_full_gate.get("official_evidence_summary") or {}
    parts = [
        "Official EXE full certification failed before commit.",
        "The official platform is a compliance/state-machine oracle here, not a strength rating source.",
        f"status={status.get('status')} mode={status.get('mode')} "
        f"classification={verdict.get('classification') or evidence_summary.get('classification')} "
        f"blocking={bool(verdict.get('blocking'))} "
        f"inconclusive={bool(verdict.get('inconclusive'))}",
    ]
    evidence_path = official_full_gate.get("official_evidence_path")
    if evidence_path:
        parts.append(f"evidence_path={evidence_path}")
    issues = [str(item) for item in (official_full_gate.get("issues") or []) if str(item).strip()]
    if issues:
        parts.append("issues:\n- " + "\n- ".join(issues[:20]))
    llm_summary = status.get("official_llm_analysis_summary") or {}
    repair_guidance = status.get("official_llm_repair_guidance") or llm_summary.get("repair_guidance")
    prompt_feedback = status.get("official_llm_prompt_feedback") or llm_summary.get("prompt_feedback")
    if repair_guidance:
        parts.append(f"llm_repair_guidance:\n{str(repair_guidance)[:2000]}")
    if prompt_feedback:
        parts.append(f"llm_prompt_feedback:\n{str(prompt_feedback)[:2000]}")
    return "\n\n".join(parts)[:8000]


def _official_gate_is_bot_blocker(official_full_gate: dict) -> bool:
    """Return only the deterministic oracle's content-bound block decision."""
    verdict = official_full_gate.get("verdict") or {}
    return bool(verdict.get("blocking")) and not bool(verdict.get("inconclusive"))


def _official_job_projection(official_full_gate: dict) -> dict:
    from official_certification import _spec_from_mapping, certification_identity

    job = official_full_gate.get("job") or {}
    spec = _spec_from_mapping(official_full_gate.get("spec") or {})
    identity = certification_identity(spec)
    progress = job.get("progress") or {}
    opponent = ((official_full_gate.get("opponent_selection") or {}).get("opponent") or {})
    return {
        "schema_version": 1,
        "job_id": str(job.get("job_id") or ""),
        "identity_digest": str(identity.get("identity_digest") or ""),
        "candidate_hash": str(identity.get("candidate_hash") or ""),
        "opponent_hash": str(identity.get("opponent_hash") or ""),
        "opponent": str(opponent.get("bot") or ""),
        "policy_id": spec.policy_id,
        "state": str(job.get("state") or ""),
        "phase": str(job.get("phase") or ""),
        "revision": int(job.get("revision", 0) or 0),
        "attempt": int(job.get("attempt", 0) or 0),
        "heartbeat_at_epoch": float(job.get("heartbeat_at_epoch", 0.0) or 0.0),
        "rounds_completed": int(progress.get("rounds_completed", 0) or 0),
        "rounds_requested": int(progress.get("rounds_requested", 0) or 0),
    }


def _record_official_job_checkpoint(
    v: int,
    source_v: int,
    ckpt: dict | None,
    official_full_gate: dict,
) -> bool:
    projection = _official_job_projection(official_full_gate)
    existing = (ckpt or {}).get("official_job")
    expected_job_id = str(existing.get("job_id") or "") if isinstance(existing, dict) else ""
    return bool(write_pipeline_checkpoint(
        v,
        source_v,
        "official_certifying",
        master_plan=(ckpt or {}).get("master_plan"),
        generation_attempt=(ckpt or {}).get("generation_attempt", 0),
        worker_failure_count=(ckpt or {}).get("worker_failure_count", 0),
        parent2_v=(ckpt or {}).get("parent2_v"),
        direction_audit=(ckpt or {}).get("direction_audit"),
        audit_context=(ckpt or {}).get("audit_context", {}) or {},
        audit_attempt=(ckpt or {}).get("audit_attempt", 0),
        precommit_attempt=(ckpt or {}).get("precommit_attempt", 0),
        precommit_rework_count=(ckpt or {}).get("precommit_rework_count", 0),
        literature_probe=(ckpt or {}).get("literature_probe"),
        prepare_scope_files=(ckpt or {}).get("prepare_scope_files", []) or [],
        official_job=projection,
        expected_official_job_id=expected_job_id,
    ))


def _record_official_full_gate_checkpoint(
    v: int,
    source_v: int,
    ckpt: dict | None,
    official_full_gate: dict,
    *,
    clear_infra_failure: bool = False,
    clear_official_job: bool = False,
) -> str:
    """Persist a non-reentrant official-full outcome and return the new stage."""
    bot_blocker = _official_gate_is_bot_blocker(official_full_gate)
    stage = "official_failed" if bot_blocker else "official_inconclusive"
    gate_payload = {
        **official_full_gate,
        "passed": False,
        "repairable_by_workers": bot_blocker,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    recorded = write_pipeline_checkpoint(
        v,
        source_v,
        stage,
        master_plan=(ckpt or {}).get("master_plan"),
        reviewer_feedback=_official_gate_feedback(official_full_gate),
        generation_attempt=(ckpt or {}).get("generation_attempt", 0),
        gate_results={"official_full": gate_payload},
        worker_failure_count=(ckpt or {}).get("worker_failure_count", 0),
        parent2_v=(ckpt or {}).get("parent2_v"),
        direction_audit=(ckpt or {}).get("direction_audit"),
        audit_context=(ckpt or {}).get("audit_context", {}) or {},
        audit_attempt=(ckpt or {}).get("audit_attempt", 0),
        precommit_attempt=(ckpt or {}).get("precommit_attempt", 0),
        precommit_rework_count=(ckpt or {}).get("precommit_rework_count", 0),
        literature_probe=(ckpt or {}).get("literature_probe"),
        prepare_scope_files=(ckpt or {}).get("prepare_scope_files", []) or [],
        clear_infra_failure=clear_infra_failure,
        infra_failure_owner="commit_bot" if clear_infra_failure else None,
        expected_infra_failure_digest=(
            infrastructure_failure_digest((ckpt or {}).get("infra_failure"))
            if clear_infra_failure
            else None
        ),
        clear_official_job=clear_official_job,
        expected_official_job_id=(
            str(((ckpt or {}).get("official_job") or {}).get("job_id") or "")
            if clear_official_job
            else None
        ),
    )
    return stage if recorded else ""


def _record_official_full_pass_checkpoint(
    v: int,
    source_v: int,
    ckpt: dict | None,
    official_full_gate: dict,
    *,
    clear_infra_failure: bool = False,
    clear_official_job: bool = False,
) -> bool:
    """Persist the exact content-bound certificate before any Git mutation."""
    gate_payload = {
        **official_full_gate,
        "passed": True,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    return bool(write_pipeline_checkpoint(
        v,
        source_v,
        (
            "official_certifying"
            if (ckpt or {}).get("stage") == "official_certifying"
            else "verified"
        ),
        master_plan=(ckpt or {}).get("master_plan"),
        gate_results={"official_full": gate_payload},
        worker_failure_count=(ckpt or {}).get("worker_failure_count", 0),
        parent2_v=(ckpt or {}).get("parent2_v"),
        direction_audit=(ckpt or {}).get("direction_audit"),
        audit_context=(ckpt or {}).get("audit_context", {}) or {},
        audit_attempt=(ckpt or {}).get("audit_attempt", 0),
        precommit_attempt=(ckpt or {}).get("precommit_attempt", 0),
        precommit_rework_count=(ckpt or {}).get("precommit_rework_count", 0),
        literature_probe=(ckpt or {}).get("literature_probe"),
        prepare_scope_files=(ckpt or {}).get("prepare_scope_files", []) or [],
        clear_infra_failure=clear_infra_failure,
        infra_failure_owner="commit_bot" if clear_infra_failure else None,
        expected_infra_failure_digest=(
            infrastructure_failure_digest((ckpt or {}).get("infra_failure"))
            if clear_infra_failure
            else None
        ),
        clear_official_job=clear_official_job,
        expected_official_job_id=(
            str(((ckpt or {}).get("official_job") or {}).get("job_id") or "")
            if clear_official_job
            else None
        ),
    ))


async def _run_official_full_commit_gate(
    v: int,
    source_v: int,
    bot_dir,
    ckpt,
    gate_results,
    *,
    retry_terminal: bool = False,
) -> dict:
    execution_mode = _checkpoint_execution_mode(ckpt, gate_results)
    if execution_mode != "native_tcp":
        return {
            "passed": False,
            "error": (
                "OFFICIAL FULL CERTIFICATION BLOCKED: only national_native/native_tcp "
                "candidates may enter the national-bot completion namespace."
            ),
            "reason": "formal_submission_requires_native_tcp",
            "national_execution_mode": execution_mode,
        }

    from official_certification import (
        build_spec,
        official_compliance_verdict,
        official_full_certified,
        read_status,
        select_official_opponent,
        spec_record,
    )
    from official_certification_job import start_or_poll_job

    # A manually started bootstrap-full job is deliberately outside the
    # automatic evolution path.  Once it has produced a valid content-bound
    # full certificate, commit_bot must publish that exact certificate instead
    # of requiring another already-published opponent (which cannot exist for
    # the first anchor).  The full validator rechecks candidate hash, signed
    # receipt, evidence, ledger and policy here; commit_bot repeats the same
    # validation immediately before Git staging/tagging below.
    existing_status = read_status(bot_dir)
    if official_full_certified(existing_status, bot_dir):
        identity = (
            existing_status.get("certification_identity")
            if isinstance(existing_status.get("certification_identity"), dict)
            else {}
        )
        existing_spec = (
            identity.get("spec")
            if isinstance(identity.get("spec"), dict)
            else {}
        )
        opponent_selection = (
            existing_status.get("opponent_selection")
            if isinstance(existing_status.get("opponent_selection"), dict)
            else {}
        )
        return {
            "passed": True,
            "outcome": "passed",
            "version": v,
            "source_v": source_v,
            "spec": existing_spec,
            "status": existing_status,
            "verdict": official_compliance_verdict(existing_status),
            "opponent_selection": opponent_selection,
            "official_evidence_path": existing_status.get("official_evidence_path"),
            "official_evidence_summary": (
                existing_status.get("official_evidence_summary") or {}
            ),
            "certificate_digest": existing_status.get("certificate_digest"),
            "certificate_path": existing_status.get("certificate_path"),
            "certification_identity": identity,
            "issues": existing_status.get("issues") or [],
            "reused_existing_certificate": True,
            "bootstrap_certificate": bool(existing_spec.get("bootstrap_root_id")),
        }

    opponent_selection = select_official_opponent(
        bot_dir,
        get_active_bots(),
        preferred=_official_preferred_opponent(),
        allow_bootstrap_grandfather=False,
    )
    if not opponent_selection.get("selected"):
        return {
            "passed": False,
            "error": "OFFICIAL FULL CERTIFICATION BLOCKED: no eligible official EXE opponent.",
            "version": v,
            "source_v": source_v,
            "opponent_selection": opponent_selection,
        }

    opponent = opponent_selection["opponent"]
    opponent_path = opponent["path"]

    spec = build_spec("full", bot_dir, opponent=opponent_path)
    job = await run_blocking_isolated(
        start_or_poll_job,
        spec,
        thread_name_prefix="official-commit",
        opponent_selection=opponent_selection,
        source_v=source_v,
        retry_terminal=retry_terminal,
    )
    if job.get("pending"):
        return {
            "passed": False,
            "pending": True,
            "outcome": "pending",
            "version": v,
            "source_v": source_v,
            "spec": spec_record(spec),
            "job": job,
            "opponent_selection": opponent_selection,
            "issues": [],
        }
    if job.get("state") != "completed" or not isinstance(job.get("status"), dict):
        return {
            "passed": False,
            "pending": False,
            "outcome": "infrastructure_failure",
            "failure_class": "infrastructure",
            "version": v,
            "source_v": source_v,
            "spec": spec_record(spec),
            "job": job,
            "opponent_selection": opponent_selection,
            "issues": list(job.get("issues") or [
                str(job.get("failure") or "official certification job failed")
            ]),
        }
    status = job["status"]
    verdict = official_compliance_verdict(status)
    passed = official_full_certified(status, bot_dir)
    result = {
        "passed": passed,
        "version": v,
        "source_v": source_v,
        "spec": spec_record(spec),
        "status": status,
        "verdict": verdict,
        "opponent_selection": opponent_selection,
        "official_evidence_path": status.get("official_evidence_path"),
        "official_evidence_summary": status.get("official_evidence_summary"),
        "certificate_digest": status.get("certificate_digest"),
        "certificate_path": status.get("certificate_path"),
        "certification_identity": status.get("certification_identity"),
        "issues": status.get("issues") or [],
        "job": job,
    }
    if not passed:
        result["outcome"] = (
            "candidate_failure"
            if _official_gate_is_bot_blocker(result)
            else "infrastructure_failure"
        )
        if result["outcome"] == "infrastructure_failure":
            result["failure_class"] = "infrastructure"
    else:
        result["outcome"] = "passed"
    return result


@tool("commit_bot", "Commit a bot generation with git commit and tag. review_approved must be true (set after run_review returns approved:true).", {"version": int, "source_v": int, "strategy": str, "review_approved": bool})
async def commit_bot(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    strategy = args.get("strategy", "")
    review_approved = args.get("review_approved", False)

    active_ckpt = _matching_checkpoint(v, source_v)
    existing_infra, infra_error = _owned_infrastructure_failure(active_ckpt, "commit_bot")
    if infra_error:
        return _json_tool_result({
            "error": f"STATE BLOCKED: {infra_error}",
            "version": v,
            "source_v": source_v,
            "failure_class": "infrastructure",
        })
    exhausted_result = await _execute_exhausted_infrastructure_failure(
        v,
        source_v,
        owner_tool="commit_bot",
    )
    if exhausted_result is not None:
        return _json_tool_result({
            **exhausted_result,
            "version": v,
            "source_v": source_v,
        })

    _set_pipeline_status(f"Committing v{v}")

    bot_dir = get_bot_dir(v)
    ckpt = _matching_checkpoint(v, source_v)
    ledger = validate_commit_gate_ledger(v, source_v, ckpt, bot_dir=bot_dir)
    missing_gates = ledger["missing_gates"]
    failed_gates = ledger["failed_gates"]
    gate_results = ledger["gate_results"]

    if missing_gates or failed_gates:
        try:
            log_system_event('pipeline.commit_blocked', 'error',
                f'Commit blocked for v{v}: missing={missing_gates} failed={failed_gates}',
                {'version': v, 'source_v': source_v, 'missing_gates': missing_gates,
                 'failed_gates': failed_gates})
        except Exception:
            pass
        return _json_tool_result({
            "error": "COMMIT BLOCKED: gate ledger incomplete or failed.",
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": ledger["checkpoint_stage"],
            "missing_gates": missing_gates,
            "failed_gates": failed_gates,
            "gate_results": gate_results,
        })

    # Guard: reviewer approval required
    if not review_approved:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: review_approved=false. Call run_review() first; only pass review_approved=true if it returns approved:true.",
        })

    official_certification_status = {}
    official_full_gate = await _run_official_full_commit_gate(
        v,
        source_v,
        bot_dir,
        ckpt,
        gate_results,
        retry_terminal=existing_infra is not None,
    )
    if official_full_gate.get("pending"):
        job = official_full_gate.get("job") or {}
        if not _record_official_job_checkpoint(v, source_v, ckpt, official_full_gate):
            return _json_tool_result({
                "error": "COMMIT BLOCKED: failed to attach durable official job to checkpoint.",
                "failure_class": "infrastructure",
                "version": v,
                "source_v": source_v,
                "checkpoint_stage": (ckpt or {}).get("stage"),
                "official_full_gate": official_full_gate,
            })
        try:
            log_system_event(
                "pipeline.official_full_pending",
                "info",
                f"Official EXE certification is running for v{v}",
                {
                    "version": v,
                    "source_v": source_v,
                    "job_id": job.get("job_id"),
                    "attempt": job.get("attempt"),
                    "progress": job.get("progress"),
                },
            )
        except Exception:
            pass
        return _json_tool_result({
            "pending": True,
            "action": "poll_commit_bot",
            "retry_after_sec": 30,
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": "official_certifying",
            "official_full_gate": official_full_gate,
        })
    if official_full_gate.get("outcome") == "infrastructure_failure":
        from pipeline_infrastructure import infrastructure_attempt_key

        job = official_full_gate.get("job") or {}
        attempt_key = infrastructure_attempt_key(
            component="official_exe_full",
            candidate_fingerprint=str(ledger.get("current_code_fingerprint") or ""),
            source_fingerprint="",
            harness_identity=str(job.get("job_id") or "official-job-selection"),
            contract_identity=str((official_full_gate.get("spec") or {}).get("policy_id") or ""),
            extra={
                "opponent": ((official_full_gate.get("opponent_selection") or {}).get("opponent") or {}).get("artifact_hash"),
            },
        )
        resume_stage = (
            "official_certifying"
            if (ckpt or {}).get("stage") == "official_certifying"
            else "verified"
        )
        infra_result = await _record_infrastructure_failure(
            v,
            source_v,
            owner_tool="commit_bot",
            resume_stage=resume_stage,
            component="official_exe_full",
            code="official_certification_infrastructure_failure",
            attempt_key=attempt_key,
            issues=official_full_gate.get("issues") or ["official certification inconclusive"],
            max_attempts=3,
            metadata={
                "job_id": job.get("job_id"),
                "job_dir": job.get("job_dir"),
                "job_attempt": job.get("attempt"),
                "opponent_selection": official_full_gate.get("opponent_selection"),
            },
        )
        return _json_tool_result({
            **infra_result,
            "error": "COMMIT BLOCKED: official EXE infrastructure is inconclusive.",
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": resume_stage,
            "official_full_gate": official_full_gate,
        })
    if not official_full_gate.get("passed"):
        official_stage = _record_official_full_gate_checkpoint(
            v,
            source_v,
            ckpt,
            official_full_gate,
            clear_infra_failure=existing_infra is not None,
            clear_official_job=bool((ckpt or {}).get("official_job")),
        )
        if not official_stage:
            return _json_tool_result({
                "error": "COMMIT BLOCKED: official terminal result could not be recorded atomically.",
                "failure_class": "infrastructure",
                "version": v,
                "source_v": source_v,
                "checkpoint_stage": (ckpt or {}).get("stage"),
                "official_full_gate": official_full_gate,
            })
        try:
            log_system_event(
                "pipeline.commit_blocked_official_full",
                "error",
                f"Commit blocked for v{v}: official EXE full certification did not pass",
                {
                    "version": v,
                    "source_v": source_v,
                    "status": (official_full_gate.get("status") or {}).get("status"),
                    "mode": (official_full_gate.get("status") or {}).get("mode"),
                    "issues": official_full_gate.get("issues", [])[:10],
                    "opponent_selection": official_full_gate.get("opponent_selection"),
                    "official_evidence_path": official_full_gate.get("official_evidence_path"),
                    "checkpoint_stage": official_stage,
                },
            )
        except Exception:
            pass
        return _json_tool_result({
            "error": official_full_gate.get("error") or "COMMIT BLOCKED: official EXE full certification did not pass.",
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": official_stage,
            "official_full_gate": official_full_gate,
        })
    official_certification_status = official_full_gate.get("status") or {}
    if not _record_official_full_pass_checkpoint(
        v,
        source_v,
        ckpt,
        official_full_gate,
        clear_infra_failure=existing_infra is not None,
        clear_official_job=bool((ckpt or {}).get("official_job")),
    ):
        return _json_tool_result({
            "error": "COMMIT BLOCKED: failed to persist official full certificate in checkpoint ledger.",
            "version": v,
            "source_v": source_v,
        })
    ckpt = read_pipeline_checkpoint() or ckpt

    # fix-6: novelty gate — warn (advisory) if new bot doesn't add behavioral
    # diversity. This is advisory-only: it does NOT block the commit, because
    # a bot can improve by fine-tuning within a niche. The warning feeds the
    # archivist and next generation's Master context.
    novelty_info = {}
    try:
        from behavior_diversity import (
            compute_decision_fingerprint, compute_delta_vendi,
            load_fingerprints, save_fingerprint,
        )
        from evolution_infra import get_active_bots
        candidate_bot = bot_name(v)
        new_fp = compute_decision_fingerprint(candidate_bot)
        pool_bots = get_active_bots()
        # Build pool fingerprints from stored data
        stored = load_fingerprints()
        pool_fps = [stored[b] for b in pool_bots if b in stored and b != candidate_bot]
        if pool_fps:
            import numpy as np
            pool_arr = np.stack(pool_fps)
            delta_vs = compute_delta_vendi(pool_arr, new_fp)
            novelty_info = {
                "delta_vendi_score": round(float(delta_vs), 4),
                "pool_size": len(pool_fps),
            }
            if delta_vs < 0.05:
                novelty_info["novelty_warning"] = (
                    f"Low behavioral novelty: delta_VS={delta_vs:.4f} < 0.05. "
                    f"The new bot occupies a similar behavioral niche as existing pool bots."
                )
                _log.warning(
                    "Novelty gate advisory for v%d: delta_VS=%.4f < 0.05",
                    v, delta_vs,
                )
        # Save the new bot's fingerprint for future novelty checks
        save_fingerprint(candidate_bot, new_fp)
    except Exception as e:
        _log.warning("Novelty gate skipped (non-fatal): %s", e)

    ratings = load_ratings()
    p = ratings.get(bot_name(v))
    h2h_wr = None
    try:
        h2h_wr = compute_h2h_avg_winrate(bot_name(v), _load_h2h_data())
    except Exception as e:
        _log.warning("H2H win rate computation failed for v%d: %s", v, e)
    wr_str = f" h2h_avg_wr={h2h_wr:.2%}" if h2h_wr is not None else ""
    rating_info = f"rating: r={p.r:.1f} rd={p.rd:.1f}{wr_str}" if p else ""

    official_certificate = None
    if official_certification_status:
        from official_certification import official_full_certified

        if not official_full_certified(
            official_certification_status,
            bot_dir,
        ):
            return _json_tool_result({
                "error": "COMMIT BLOCKED: candidate or official certificate changed before Git commit.",
                "version": v,
                "source_v": source_v,
            })
        identity = official_certification_status.get("certification_identity") or {}
        official_certificate = {
            "certificate_digest": official_certification_status.get("certificate_digest"),
            "candidate_hash": identity.get("candidate_hash"),
            "policy_id": official_certification_status.get("policy_id"),
            "certificate_path": official_certification_status.get("certificate_path"),
            "certification_identity": identity,
        }

    parent2_v = ckpt.get("parent2_v") if ckpt else None
    local_publish_retry = git_has_tag(v)
    if local_publish_retry:
        tag_matches, mismatch = _existing_local_bot_tag_matches_certificate(
            v,
            official_certificate or {},
        )
        if not tag_matches:
            return _json_tool_result({
                "error": "COMMIT BLOCKED: existing local bot tag does not match the current certified artifact.",
                "version": v,
                "source_v": source_v,
                "reason": mismatch,
            })
        push_ok = (
            _push_existing_bot_refs(v)
            if evolution_git_push_enabled() or evolution_git_push_required()
            else False
        )
    else:
        push_ok = git_commit_bot(
            v,
            source_v,
            strategy,
            rating_info=rating_info,
            parent2_v=parent2_v,
            official_certificate=official_certificate,
        )

    # Verify tag was created
    if not git_has_tag(v):
        return _json_tool_result({
            "error": f"Git tag {bot_tag(v)} not found after commit. Git operations may have failed.",
            "version": v,
        })

    if evolution_git_push_required() and not push_ok:
        log_system_event(
            "pipeline.bot_publish_required_failed",
            "error",
            f"v{v} is committed and tagged locally but required origin publication failed",
            {
                "version": v,
                "source_v": source_v,
                "tag": bot_tag(v),
                "local_publish_retry": local_publish_retry,
                "checkpoint_preserved": True,
            },
        )
        return _json_tool_result({
            "error": "COMMIT PENDING: required push to origin failed.",
            "version": v,
            "source_v": source_v,
            "committed": False,
            "local_committed": True,
            "push_ok": False,
            "checkpoint_preserved": True,
            "completed_sentinel_written": False,
            "directive": (
                "Keep the verified checkpoint and retry commit_bot after origin is reachable. "
                "The retry will verify the existing tag/certificate and push refs without creating another commit."
            ),
        })

    (bot_dir / ".completed").touch()

    # Write reap_signal early so daemon discovers new bot immediately, even if archive/timeout interrupts later
    reap_signal = RESULTS_DIR / ".reap_signal"
    reap_signal.write_text(str(time.time()))

    # Write priority eval signal so daemon schedules this bot heavily
    priority_file = RESULTS_DIR / "priority_eval.json"
    try:
        with locked_file(priority_file, "w") as f:
            json.dump({"bot": bot_name(v), "min_games": 500, "since": time.time()}, f)
    except Exception as e:
        _log.warning("Priority eval signal write failed for v%d: %s", v, e)

    # LOG GAP FIX (2026-06-29): enrich the commit audit event with rating,
    # file_size, and gate_results summary so a committed generation is fully
    # auditable from the event log alone (previously only version/source/strategy).
    _commit_audit = {"version": v, "source_v": source_v, "strategy": strategy[:120]}
    try:
        if p is not None:
            _commit_audit["rating"] = {"r": round(p.r, 1), "rd": round(p.rd, 1)}
        if h2h_wr is not None:
            _commit_audit["h2h_avg_wr"] = round(h2h_wr, 4)
    except Exception:
        pass
    try:
        _py_files = list(bot_dir.glob("*.py"))
        _commit_audit["file_size_total"] = sum(f.stat().st_size for f in _py_files)
        _commit_audit["n_py_files"] = len(_py_files)
    except Exception:
        pass
    try:
        _gr = (ckpt or {}).get("gate_results", {}) or {}
        _commit_audit["gate_results"] = {
            "quality_passed": (_gr.get("quality") or {}).get("passed"),
            "review_score": (_gr.get("review") or {}).get("score"),
            "critic_score": (_gr.get("critic") or {}).get("score"),
            "precommit_passed": (_gr.get("precommit_eval") or {}).get("passed"),
        }
        if official_certification_status:
            _commit_audit["official_certification"] = {
                "status": official_certification_status.get("status"),
                "mode": official_certification_status.get("mode"),
                "cache_key": official_certification_status.get("cache_key"),
                "official_evidence_path": official_certification_status.get("official_evidence_path"),
            }
    except Exception:
        pass
    log_system_event("pipeline.committed", "success",
                     f"Committed v{v} from v{source_v}: {strategy[:80]}", _commit_audit)

    _set_pipeline_status(f"Committed v{v}", is_working=False)

    # Archive this generation's state snapshot
    try:
        archive_generation(v, source_v, ckpt)
        archive_rotate_files(v)
        archive_old_logs()
    except Exception as e:
        _log.warning("Archive generation failed for v%d: %s", v, e)

    # --- Meta-3: Record Critic Calibration Data (before clearing checkpoint) ---
    # fix-2: Write rating_delta=None as placeholder. The real delta is backfilled
    # asynchronously by reconcile_critic_calibration() once the daemon converges
    # the bot's rating (rd < 60, games >= MIN_GAMES_FOR_EVAL). Writing the stale
    # r-2*rd value at commit time was 98% zero (new bot rd=350 → delta~0),
    # rendering calibration inert.
    try:
        if ckpt:
            critic_gate = ckpt.get("gate_results", {}).get("critic", {})
            critic_score = critic_gate.get("score", 0)
            cal_file = RESULTS_DIR / "critic_calibration.jsonl"
            cal_entry = json.dumps({
                "version": v, "source_v": source_v,
                "critic_score": critic_score,
                "rating_delta": None,  # backfilled by reconcile_critic_calibration()
                "reconciled": False,   # marker for reconcile to find unfilled rows
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            with open(cal_file, "a", encoding="utf-8") as _cf:
                _cf.write(cal_entry + "\n")
    except Exception:
        pass  # Calibration recording is advisory

    # --- Phase 3: FAMOU nemesis archive (advisory) ---
    # Recompute the nemesis/champion relationships from the on-disk h2h so the
    # next generation's precommit nemesis probe has a fallback snapshot when
    # the live h2h scan finds no qualifying nemesis. The new bot itself has no
    # h2h yet (it just got tagged), but committing it refreshes every other
    # bot's nemesis mapping. Best-effort: never blocks the commit path.
    try:
        from nemesis_archive import write_nemesis_archive
        write_nemesis_archive(get_active_bots())
    except Exception as e:
        _log.warning("Nemesis archive write failed for v%d: %s", v, e)

    clear_pipeline_checkpoint()

    try:
        from server.state import app_state
        app_state.set_generation(v, v + 1)
    except Exception as e:
        _log.warning("App state update failed for v%d: %s", v, e)

    # ── Update eval table + metrics in evolution state snapshot ──
    try:
        ratings = load_ratings()
        active_bots = get_active_bots()
        ui = _get_ui()
        ui.update_eval_table(ratings, active_bots)
        ui.update_metrics({
            "current_v": v,
            "next_v": v + 1,
            "success_rate": 1.0,  # generation succeeded
        })
    except Exception:
        pass  # non-blocking enrichment

    result = {"committed": True, "version": v, "source_v": source_v, "push_ok": push_ok}
    if official_full_gate:
        result["official_full_gate"] = {
            "status": official_certification_status.get("status"),
            "mode": official_certification_status.get("mode"),
            "cache_hit": official_certification_status.get("cache_hit"),
            "official_evidence_path": official_certification_status.get("official_evidence_path"),
            "opponent": (official_full_gate.get("opponent_selection") or {}).get("opponent"),
        }
    if novelty_info:
        result["novelty_gate"] = novelty_info
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        result["needs_reap"] = True
        result["pool_size"] = len(active_bots)
    try:
        log_system_event("pipeline.commit_done", "info",
                         f"Commit finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Archivist Stage
# ──────────────────────────────────────────────

def _append_experience_updates(version: int, updates: list[str],
                                strategic_advice: str = "", generation_assessment: str = "",
                                require_committed: bool = True):
    """Append archivist experience_updates, strategic_advice, and assessment to experience_pool.md."""
    if require_committed and not git_has_tag(version):
        try:
            log_system_event(
                "pipeline.experience_write_blocked_uncommitted", "warn",
                f"Blocked experience_pool.md write for uncommitted v{version}",
                {"version": version, "updates": updates[:5],
                 "generation_assessment": generation_assessment},
            )
        except Exception:
            pass
        return

    # Build the lines to insert
    new_lines = [f"- **v{version}**: {u}" for u in updates if u.strip()]

    # Add strategic_advice as a separate line so Master sees it
    if strategic_advice and strategic_advice.strip():
        label = f" ({generation_assessment})" if generation_assessment and generation_assessment != "neutral" else ""
        new_lines.append(f"- **v{version} 归档建议{label}**: {strategic_advice.strip()}")

    if not new_lines:
        return

    with locked_file(EXPERIENCE_FILE, "r") as f:
        content = f.read()

    lines = content.split("\n")

    # Find the RECENT_LESSONS section and append after it
    recent_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## RECENT_LESSONS":
            recent_idx = i
            break

    if recent_idx is not None:
        # Insert after the ## RECENT_LESSONS header
        insert_at = recent_idx + 1
        for j, new_line in enumerate(new_lines):
            lines.insert(insert_at + j, new_line)
    else:
        # Fallback: append at end
        lines.append("")
        lines.append("## RECENT_LESSONS")
        lines.extend(new_lines)

    with locked_file(EXPERIENCE_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


def _git_dirty_paths() -> set[str]:
    """Return porcelain dirty paths without mutating git state."""
    out = _git("status", "--porcelain", check=False)
    paths: set[str] = set()
    for line in out.splitlines():
        if not line:
            continue
        # Porcelain v1: XY<space>path, rename: XY old -> new.
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            old, new = path.split(" -> ", 1)
            paths.add(old.strip())
            paths.add(new.strip())
        else:
            paths.add(path.strip())
    return paths


def _path_was_dirty(path: str, preexisting_dirty: set[str]) -> bool:
    prefix = path.rstrip("/") + "/"
    return any(p == path or p.startswith(prefix) for p in preexisting_dirty)


def _archive_housekeeping_commit(version: int, reap_result: dict | None,
                                 experience_touched: bool,
                                 preexisting_dirty: set[str]) -> dict:
    """Commit archivist/reap tracked-file side effects so the worktree stays clean.

    commit_bot owns the bot commit and tag. run_archivist can still create tracked
    housekeeping changes after that point: experience_pool.md updates and tracked
    bot deletions from auto-reap. Those must be explicit, path-scoped commits
    rather than hidden user-facing dirty state.
    """
    _git_ensure_main_branch()

    preexisting_staged = [
        p for p in _git("diff", "--cached", "--name-only", check=False).splitlines()
        if p
    ]
    if preexisting_staged:
        log_system_event(
            "pipeline.archivist_housekeeping_skip_staged", "warn",
            f"v{version}: skipped housekeeping commit because staged files already exist",
            {"version": version, "staged_files": preexisting_staged[:40]},
        )
        return {
            "committed": False,
            "reason": "preexisting_staged_files",
            "preexisting_staged": preexisting_staged,
        }

    candidates: list[tuple[str, str]] = []
    if experience_touched:
        try:
            candidates.append((str(EXPERIENCE_FILE.relative_to(PROJECT_ROOT)), "add"))
        except ValueError:
            pass
    if reap_result and reap_result.get("reaped") and reap_result.get("culled"):
        candidates.append((f"bots/{reap_result['culled']}", "add-u"))

    staged_paths: list[str] = []
    skipped_preexisting: list[str] = []
    for path, mode in candidates:
        if _path_was_dirty(path, preexisting_dirty):
            skipped_preexisting.append(path)
            continue
        dirty_now = _git("status", "--porcelain", "--", path, check=False).strip()
        if not dirty_now:
            continue
        if mode == "add-u":
            _git("add", "-u", "--", path, check=False)
        else:
            _git("add", "--", path, check=False)
        staged_paths.extend(
            p for p in _git("diff", "--cached", "--name-only", "--", path, check=False).splitlines()
            if p and p not in staged_paths
        )

    if skipped_preexisting:
        log_system_event(
            "pipeline.archivist_housekeeping_skip_dirty", "warn",
            f"v{version}: skipped pre-existing dirty housekeeping path(s)",
            {"version": version, "paths": skipped_preexisting},
        )
    if not staged_paths:
        return {
            "committed": False,
            "reason": "no_housekeeping_changes",
            "skipped_preexisting": skipped_preexisting,
        }
    staged_set = {
        p for p in _git("diff", "--cached", "--name-only", check=False).splitlines()
        if p
    }
    allowed_set = set(staged_paths)
    unexpected = sorted(staged_set - allowed_set)
    if unexpected:
        for path in staged_paths:
            _git("restore", "--staged", "--", path, check=False)
        log_system_event(
            "pipeline.archivist_housekeeping_skip_unexpected_staged", "warn",
            f"v{version}: skipped housekeeping commit because unrelated staged files appeared",
            {"version": version, "unexpected_staged": unexpected[:40],
             "housekeeping_paths": staged_paths[:40]},
        )
        return {
            "committed": False,
            "reason": "unexpected_staged_files",
            "unexpected_staged": unexpected,
            "staged_files": staged_paths,
            "skipped_preexisting": skipped_preexisting,
        }

    log_system_event(
        "pipeline.archivist_git_commit_staged", "info",
        f"v{version}: staging {len(staged_paths)} archivist housekeeping file(s)",
        {"version": version, "staged_files": staged_paths[:40]},
    )
    _git("commit", "-m", f"chore: archive v{version} evolution housekeeping", "--", *staged_paths)
    commit_hash = _git("rev-parse", "--short", "HEAD", check=False).strip()
    publish_runtime_expected_head("archivist_housekeeping_commit", version=version)
    push_ok = False
    if evolution_git_push_enabled():
        push_ok = git_push_refs("main")
        publish_runtime_expected_head("archivist_housekeeping_push", version=version)
    log_system_event(
        "pipeline.archivist_git_commit_done", "success",
        f"v{version}: committed archivist housekeeping {commit_hash}",
        {"version": version, "commit": commit_hash, "push_ok": push_ok},
    )
    return {
        "committed": True,
        "commit": commit_hash,
        "push_ok": push_ok,
        "staged_files": staged_paths,
        "skipped_preexisting": skipped_preexisting,
    }


@tool("run_archivist", "Run post-commit archive audit for a completed generation. Verifies consistency, auto-reaps if needed, calls LLM for strategic assessment and experience pool updates.", {"version": int, "source_v": int})
async def run_archivist(args):
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)

    _set_pipeline_status(f"Archiving v{v}")

    ui = _get_ui()
    preexisting_dirty = _git_dirty_paths()

    # 1. Verify post-commit consistency
    bot_dir = get_bot_dir(v)
    consistency_issues = []
    if not (bot_dir / ".completed").exists():
        consistency_issues.append(f".completed missing for v{v}")
    if not git_has_tag(v):
        consistency_issues.append(f"git tag {bot_tag(v)} missing")
    ratings = load_ratings()
    if bot_name(v) not in ratings:
        consistency_issues.append(f"v{v} not in glicko_ratings.json")

    # 2. Auto-reap if pool exceeds limit
    reap_result = None
    active_bots = get_active_bots()
    if len(active_bots) > MAX_ACTIVE_BOTS:
        try:
            from tool_bot_management import _do_reap_weakest
            reap_result = await _do_reap_weakest()
        except Exception as e:
            reap_result = {"error": str(e)}

    # 3. Load archive snapshot for LLM context
    archive_path = ARCHIVE_DIR / f"v{v}.json"
    snapshot = {}
    if archive_path.exists():
        try:
            with open(archive_path, "r") as f:
                snapshot = json.load(f)
        except Exception:
            pass

    # Inject reviewer context into snapshot — prefer archive data (checkpoint is cleared by commit_bot)
    review_info = ""
    reviewer_context = snapshot.get("reviewer_context", "")
    if reviewer_context:
        review_info = reviewer_context
    else:
        # Fallback: try checkpoint (only works if run_archivist is called before commit clears it)
        try:
            ckpt = read_pipeline_checkpoint()
            if ckpt:
                review_gate = ckpt.get("gate_results", {}).get("review", {})
                cs = review_gate.get("change_summary", "")
                ra = review_gate.get("risk_areas", [])
                if cs:
                    review_info += f"\nReviewer Change Summary: {cs}"
                if ra:
                    review_info += f"\nReviewer Risk Areas: {', '.join(ra) if isinstance(ra, list) else str(ra)}"
        except Exception:
            pass

    # Also extract reviewer info from archive snapshot fields
    if not review_info:
        cs = snapshot.get("reviewer_change_summary", "")
        ra = snapshot.get("reviewer_risk_areas", [])
        if cs:
            review_info += f"\nReviewer Change Summary: {cs}"
        if ra:
            review_info += f"\nReviewer Risk Areas: {', '.join(ra) if isinstance(ra, list) else str(ra)}"

    # Inject review info into snapshot for archivist LLM
    if review_info:
        snapshot["reviewer_context"] = review_info

    # 4. LLM archivist analysis — run every commit to populate experience pool
    llm_result = None
    experience_touched = False
    try:
        from experience_archivist import _run_archivist_analysis
        llm_result = await _run_archivist_analysis(v, source_v, snapshot, ui)
        # Append LLM notes to archive snapshot
        if llm_result and archive_path.exists():
            snapshot["archivist_notes"] = llm_result
            with locked_file(archive_path, "w") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)

        # Write experience_updates + strategic_advice to experience_pool.md
        if llm_result and isinstance(llm_result, dict):
            updates = llm_result.get("experience_updates", [])
            advice = llm_result.get("strategic_advice", "")
            assessment = llm_result.get("generation_assessment", "")
            if updates or (advice and advice.strip()):
                _append_experience_updates(
                    v, updates,
                    strategic_advice=advice,
                    generation_assessment=assessment,
                )
                experience_touched = True
    except Exception as e:
        llm_result = {"error": str(e)}

    housekeeping_commit = None
    try:
        housekeeping_commit = _archive_housekeeping_commit(
            v, reap_result, experience_touched, preexisting_dirty
        )
    except Exception as e:
        housekeeping_commit = {"error": str(e)}
        log_system_event(
            "pipeline.archivist_git_commit_failed", "error",
            f"v{v}: archivist housekeeping commit failed: {str(e)[:180]}",
            {"version": v, "error": str(e)[:500]},
        )

    result = {
        "version": v,
        "source_v": source_v,
        "consistency_ok": len(consistency_issues) == 0,
        "consistency_issues": consistency_issues if consistency_issues else None,
        "reap_result": reap_result,
        "pool_size": len(active_bots),
        "snapshot": snapshot,
        "llm_analysis": llm_result,
        "housekeeping_commit": housekeeping_commit,
    }

    # Record archived stage in checkpoint (then clear)
    _ckpt = _matching_checkpoint(v, source_v)
    if _ckpt:
        write_pipeline_checkpoint(v, source_v, "archived",
                                  master_plan=_ckpt.get("master_plan"),
                                  gate_results=_ckpt.get("gate_results"))
    clear_pipeline_checkpoint()

    try:
        log_system_event('pipeline.archivist_done', 'info',
            f'Archivist completed for v{v}',
            {'version': v, 'source_v': source_v,
             'consistency_ok': len(consistency_issues) == 0,
             'pool_size': len(active_bots)})
    except Exception:
        pass

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
    parent_a = args.get("parent_a")
    parent_b = args.get("parent_b")
    target_v = args.get("target_v")
    if target_v is None:
        _v, parent_a = _resolve_version_args(args)
        target_v = target_v or _v
    if parent_a is None or parent_b is None or target_v is None:
        return _json_tool_result({"error": "Missing parent_a/parent_b/target_v"})

    _set_pipeline_status(f"Crossover for v{target_v}")

    # Guard: prevent self-crossover
    if parent_a == parent_b:
        return _json_tool_result({"error": "Cannot crossover with self (parent_a == parent_b)"})

    # Prepare target directory from parent A
    target_dir = get_bot_dir(target_v)

    # Guard: refuse to overwrite a completed bot
    if target_dir.exists() and (target_dir / ".completed").exists():
        return _json_tool_result({"error": f"Target v{target_v} already exists and is completed. Refusing to overwrite."})

    # Guard: refuse to overwrite a BARE-COMMITTED target (root-cause fix for the
    # v117 repeated-regeneration loop, 2026-06-18). A target dir that is
    # git-tracked but lacks an active-epoch tag was created by a bare `git commit`
    # bypassing commit_bot. Silently re-running crossover on it regenerates the
    # same version forever — find_current_v() only trusts tags, so it stays
    # stale and the orchestrator keeps picking the same target_v. Require
    # commit_bot finalization or explicit abandon/clear first. (This is the
    # crossover-side mirror of prepare_next_gen's stage guard, which crossover
    # previously lacked — the deepest root cause per adversarial verification.)
    if target_dir.exists() and git_dir_is_committed(target_v) and not git_has_tag(target_v):
        return _json_tool_result({
            "error": f"Target v{target_v} is git-committed but has no {bot_tag(target_v)} tag (bare commit bypassing commit_bot). "
                     f"Refusing to overwrite — re-running crossover here causes infinite regeneration. "
                     f"Run commit_bot for v{target_v} to finalize it, or abandon/clear the untagged dir first."
        })

    # Guard: parent must exist and be completed
    parent_a_dir = get_bot_dir(parent_a)
    if not parent_a_dir.exists():
        return _json_tool_result({"error": f"Parent A bot v{parent_a} not found"})
    if not (parent_a_dir / ".completed").exists():
        return _json_tool_result({"error": f"Parent A bot v{parent_a} is incomplete (no .completed sentinel)"})

    parent_b_dir = get_bot_dir(parent_b)
    if not parent_b_dir.exists():
        return _json_tool_result({"error": f"Parent B bot v{parent_b} not found"})
    if not (parent_b_dir / ".completed").exists():
        return _json_tool_result({"error": f"Parent B bot v{parent_b} is incomplete (no .completed sentinel)"})

    # Guard: both parents must have git tags (authoritative commit proof)
    if not git_has_tag(parent_a):
        return _json_tool_result({"error": f"Parent A v{parent_a} has no git tag '{bot_tag(parent_a)}'. Cannot use uncommitted code."})
    if not git_has_tag(parent_b):
        return _json_tool_result({"error": f"Parent B v{parent_b} has no git tag '{bot_tag(parent_b)}'. Cannot use uncommitted code."})
    active_parent_set = set(get_active_bots())
    ineligible_parents = [
        bot_name(version)
        for version in (parent_a, parent_b)
        if bot_name(version) not in active_parent_set
    ]
    if ineligible_parents:
        return _json_tool_result({
            "error": "CROSSOVER_PARENT_NOT_ACTIVE_ELIGIBLE",
            "success": False,
            "ineligible_parents": ineligible_parents,
            "directive": (
                "Select parents from get_active_bots(); direct tagged/reaped or "
                "uncertified historical paths cannot bypass role eligibility."
            ),
        })
    try:
        from national_position_contract import detect_position_semantics_errors
        parent_a_position_errors = detect_position_semantics_errors(parent_a_dir)
        parent_b_position_errors = detect_position_semantics_errors(parent_b_dir)
    except Exception as exc:
        parent_a_position_errors = [f"position_contract_check_error: {type(exc).__name__}: {str(exc)[:200]}"]
        parent_b_position_errors = []
    parent_position_errors = {}
    if parent_a_position_errors:
        parent_position_errors[bot_name(parent_a)] = parent_a_position_errors[:10]
    if parent_b_position_errors:
        parent_position_errors[bot_name(parent_b)] = parent_b_position_errors[:10]
    if parent_position_errors:
        log_system_event(
            "pipeline.crossover_parent_position_contract_failed",
            "error",
            f"Crossover refused for v{target_v}: parent position contract violation",
            {
                "target_v": target_v,
                "parent_a": parent_a,
                "parent_b": parent_b,
                "position_errors": parent_position_errors,
            },
        )
        return _json_tool_result({
            "error": "CROSSOVER_PARENT_POSITION_CONTRACT_FAILED",
            "success": False,
            "directive": (
                "Selected crossover parent violates the national heads-up position contract. "
                "Let prepare_generation select a protocol-eligible active parent."
            ),
            "position_errors": parent_position_errors,
        })

    ui = _get_ui()

    architecture_policy = None
    capability_context = {}
    try:
        from workflow_profiles import get_workflow_profile

        native_tcp = getattr(get_workflow_profile(), "national_execution_mode", "adapter") == "native_tcp"
        if native_tcp and (parent_a_dir / "national_bot.py").exists():
            from national_capability_contract import evaluate_national_capabilities
            from runtime_architecture_policy import build_architecture_policy

            parent_a_capabilities = evaluate_national_capabilities(parent_a_dir)
            parent_b_capabilities = evaluate_national_capabilities(parent_b_dir)
            architecture_policy = build_architecture_policy(
                parent_a_dir,
                source_capabilities=parent_a_capabilities,
            )

            def _compact_capabilities(payload):
                return {
                    "detector_version": payload.get("detector_version"),
                    "checks": {
                        item.get("check_id"): bool(item.get("passed"))
                        for item in payload.get("checks") or []
                        if item.get("check_id")
                    },
                    "decision_path_risks": {
                        key: (payload.get("decision_path_risks") or {}).get(key, [])[:5]
                        for key in ("external_io", "history_scans", "large_runtime_tables")
                    },
                }

            capability_context = {
                bot_name(parent_a): _compact_capabilities(parent_a_capabilities),
                bot_name(parent_b): _compact_capabilities(parent_b_capabilities),
            }
    except Exception as exc:
        log_system_event(
            "pipeline.crossover_architecture_policy_failed",
            "error",
            f"Crossover architecture policy failed for v{parent_a}×v{parent_b}: {type(exc).__name__}: {str(exc)[:240]}",
            {"parent_a": parent_a, "parent_b": parent_b, "target_v": target_v, "error": str(exc)[:500]},
        )
        return _json_tool_result({
            "error": "CROSSOVER_ARCHITECTURE_POLICY_FAILED",
            "success": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
            "directive": "Do not synthesize a crossover child without a stable parent capability contract.",
        })

    # --- P1-3: Crossover Parent Compatibility Audit ---
    compat = {
        "compatible": True,
        "compatibility_score": None,
        "conflict_areas": [],
        "suggested_merge_approach": "",
        "audit_unavailable": True,
    }
    try:
        from audit_agents import _run_crossover_compatibility_audit
        compat = await _run_crossover_compatibility_audit(
            parent_a,
            parent_b,
            ui,
            target_v=target_v,
            architecture_context={
                "architecture_policy": architecture_policy,
                "parent_capabilities": capability_context,
            },
        )
        if not compat.get("compatible", True):
            log_system_event("pipeline.crossover_incompatible", "warn",
                             f"Parents v{parent_a}×v{parent_b} may be incompatible: {compat.get('conflict_areas', [])[:3]}",
                             {"parent_a": parent_a, "parent_b": parent_b, "compat": compat})
            if compat.get("compatibility_score", 10) <= 3:
                try:
                    from crossover_compat import record_incompatible_crossover
                    incompat_record = record_incompatible_crossover(
                        parent_a,
                        parent_b,
                        target_v=target_v,
                        compatibility=compat,
                    )
                except Exception as record_exc:
                    incompat_record = {"record_error": f"{type(record_exc).__name__}: {record_exc}"}
                    _log.warning("Failed to record incompatible crossover pair: %s", record_exc)

                try:
                    from tool_bot_management import _do_abandon_generation
                    abandon_result = await _do_abandon_generation(
                        reason=f"crossover_incompatible:v{parent_a}xv{parent_b}"
                    )
                except Exception as abandon_exc:
                    abandon_result = {
                        "abandoned": False,
                        "reason": f"{type(abandon_exc).__name__}: {abandon_exc}",
                    }
                    _log.warning("Failed to abandon incompatible crossover generation: %s", abandon_exc)

                log_system_event(
                    "pipeline.crossover_incompatible_abandoned",
                    "warn" if abandon_result.get("abandoned") else "error",
                    f"Crossover v{parent_a}×v{parent_b} rejected as incompatible for v{target_v}",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a,
                        "parent_b": parent_b,
                        "compat": compat,
                        "incompat_record": incompat_record,
                        "abandon_result": abandon_result,
                    },
                )
                return _json_tool_result({
                    "error": "CROSSOVER_INCOMPATIBLE",
                    "success": False,
                    "abandoned": bool(abandon_result.get("abandoned")),
                    "directive": (
                        f"Parents v{parent_a} and v{parent_b} are fundamentally incompatible "
                        f"(score={compat.get('compatibility_score')}). This pair has been recorded "
                        "and the generation was abandoned; let prepare_generation select a fresh "
                        "generation and avoid this pair."
                    ),
                    "message": f"Parents v{parent_a} and v{parent_b} are fundamentally incompatible.",
                    "conflicts": compat.get("conflict_areas", [])[:8],
                    "suggestion": compat.get("suggested_merge_approach", "Select different parents."),
                    "compatibility": compat,
                    "incompat_record": incompat_record,
                    "abandon_result": abandon_result,
                })
    except Exception as e:
        _log.warning("Crossover compat audit error (skipping): %s", e)

    success = await _run_crossover(
        parent_a,
        parent_b,
        target_v,
        ui,
        compatibility=compat,
        architecture_policy=architecture_policy,
        capability_context=capability_context,
    )

    # Write checkpoint so quality gates → review → critic → commit can proceed
    if success:
        prepare_scope_files = []
        try:
            prepare_scope_files = [
                p for p in _py_files_changed_between(
                    get_bot_dir(parent_a),
                    get_bot_dir(target_v),
                )
                if "backup" not in p
            ]
            if prepare_scope_files:
                log_system_event(
                    "pipeline.crossover_scope_captured",
                    "info",
                    f"Crossover baseline for v{target_v} changed {len(prepare_scope_files)} file(s)",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a,
                        "parent_b": parent_b,
                        "prepare_scope_files": prepare_scope_files[:20],
                    },
                )
        except Exception as exc:
            _log.warning("Failed to capture crossover prepare scope for v%s: %s", target_v, exc)
        crossover_plan = {
            "strategy": "crossover",
            "tasks": [],
            "parents": [parent_a, parent_b],
            "source_v": parent_a,
            "next_v": target_v,
            "architecture_policy": architecture_policy,
            "crossover_compatibility": compat,
            "parent_capabilities": capability_context,
            "note": "Crossover already generated bot code. Skip run_master and execute_workers; proceed to run_quality_gates.",
        }
        from runtime_architecture_policy import build_runtime_contract_ledger
        from tool_planning import _architecture_default_runtime_contract

        crossover_focus = (architecture_policy or {}).get("selected_focus") or {}
        crossover_floor_checks = list(
            (architecture_policy or {}).get("runtime_floor_checks") or []
        )
        crossover_contract_task = {
            "worker_id": "system_crossover_runtime_floor",
            "skill_layer": "runtime_architecture",
            "architecture_focus_id": str(crossover_focus.get("focus_id") or ""),
            "runtime_contract": _architecture_default_runtime_contract(
                str(crossover_focus.get("focus_id") or ""),
                "runtime_architecture",
                "strategy.py",
                required_checks=crossover_floor_checks,
            ),
        }
        crossover_plan["runtime_contract_ledger"] = build_runtime_contract_ledger({
            "tasks": [crossover_contract_task],
        })
        try:
            from national_position_contract import detect_position_semantics_errors
            target_position_errors = detect_position_semantics_errors(get_bot_dir(target_v))
        except Exception as exc:
            target_position_errors = [f"position_contract_check_error: {type(exc).__name__}: {str(exc)[:200]}"]
        if target_position_errors:
            quality_result = _position_semantics_failed_gate(target_position_errors)
            feedback = _position_semantics_feedback(target_position_errors)
            write_pipeline_checkpoint(
                target_v,
                parent_a,
                "quality_failed",
                master_plan=crossover_plan,
                gate_results={"quality": quality_result},
                reviewer_feedback=feedback,
                parent2_v=parent_b,
                prepare_scope_files=prepare_scope_files,
                audit_context={
                    "crossover": {
                        "parent_a": parent_a,
                        "parent_b": parent_b,
                        "compatibility": compat,
                        "architecture_policy": architecture_policy,
                    }
                },
            )
            try:
                log_system_event(
                    "pipeline.crossover_position_contract_failed",
                    "error",
                    f"Crossover v{parent_a}×v{parent_b} → v{target_v} produced position semantics violations",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a,
                        "parent_b": parent_b,
                        "errors": target_position_errors[:10],
                    },
                )
            except Exception:
                pass
            return _json_tool_result({
                "success": True,
                "contract_failed": True,
                "stage": "quality_failed",
                "failed_gates": quality_result["failed_gates"],
                "position_semantics_errors": target_position_errors[:10],
                "directive": (
                    "Crossover generated code, but the national position contract failed. "
                    "The checkpoint is quality_failed; next action is execute_workers for the recorded repair contract."
                ),
                "logs": ui.get_output(),
            })
        write_pipeline_checkpoint(target_v, parent_a, "workers_done",
                                  master_plan=crossover_plan,
                                  parent2_v=parent_b,
                                  prepare_scope_files=prepare_scope_files,
                                  audit_context={
                                      "crossover": {
                                          "parent_a": parent_a,
                                          "parent_b": parent_b,
                                          "compatibility": compat,
                                          "architecture_policy": architecture_policy,
                                      }
                                  })
        try:
            log_system_event('pipeline.crossover_done', 'info',
                f'Crossover v{parent_a}×v{parent_b} → v{target_v} succeeded',
                {'target_v': target_v, 'parent_a': parent_a, 'parent_b': parent_b})
            log_system_event(
                "pipeline.crossover_resume_quality", "info",
                f"Crossover v{target_v} checkpoint ready; next step is run_quality_gates",
                {"target_v": target_v, "parent_a": parent_a,
                 "parent_b": parent_b, "next_step": "run_quality_gates"},
            )
        except Exception:
            pass
    else:
        try:
            log_system_event('pipeline.crossover_failed', 'error',
                f'Crossover v{parent_a}×v{parent_b} → v{target_v} failed',
                {'target_v': target_v, 'parent_a': parent_a, 'parent_b': parent_b})
        except Exception:
            pass
        # B1 (2026-07-09): when the crossover LLM retries are exhausted (e.g.
        # repeated idle timeouts / SDK stream stalls) WITHOUT a compatibility
        # rejection, the checkpoint stays at "crossover_running". Previously
        # run_crossover returned a bare {"success": False} with no "error", so
        # the orchestrator deterministic router fell through to "route done,
        # re-enter loop" and re-routed to run_crossover again — an infinite
        # deadlock that consumed ~28 min per cycle without progress.
        #
        # Mirror the CROSSOVER_INCOMPATIBLE contract: abandon the generation
        # (clear checkpoint + remove the incomplete dir) and return a distinct
        # CROSSOVER_LLM_EXHAUSTED token so the orchestrator recognizes the
        # abandon instead of looping.
        try:
            from tool_bot_management import _do_abandon_generation
            abandon_result = await _do_abandon_generation(
                reason=f"crossover_llm_exhausted:v{parent_a}xv{parent_b}"
            )
        except Exception as abandon_exc:
            abandon_result = {
                "abandoned": False,
                "reason": f"{type(abandon_exc).__name__}: {abandon_exc}",
            }
            _log.warning("Failed to abandon crossover-LLM-exhausted generation: %s", abandon_result)

        log_system_event(
            "pipeline.crossover_llm_exhausted_abandoned",
            "warn" if abandon_result.get("abandoned") else "error",
            f"Crossover v{parent_a}×v{parent_b} → v{target_v} exhausted all LLM retries; "
            f"{'generation abandoned' if abandon_result.get('abandoned') else 'abandon did not complete'}.",
            {
                "target_v": target_v,
                "parent_a": parent_a,
                "parent_b": parent_b,
                "abandon_result": abandon_result,
            },
        )
        return _json_tool_result({
            "error": "CROSSOVER_LLM_EXHAUSTED",
            "success": False,
            "abandoned": bool(abandon_result.get("abandoned")),
            "directive": (
                f"Crossover v{parent_a}×v{parent_b} exhausted all LLM retries "
                f"(repeated timeout/SDK stream stall). The generation was abandoned; "
                "let prepare_generation select a fresh generation."
            ),
            "message": f"Crossover v{parent_a}×v{parent_b} failed after exhausting all LLM retries.",
            "abandon_result": abandon_result,
            "logs": ui.get_output(),
        })

    result = {"success": success, "logs": ui.get_output()}
    return _json_tool_result(result)
