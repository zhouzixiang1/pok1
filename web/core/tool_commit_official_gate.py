"""Official-job checkpoint recording and official-full commit gate for tool_commit.

Extracted as a cohesive business cluster; tool_commit.py retains thin delegate
shells so external ``from tool_commit import <name>`` and
``monkeypatch.setattr(tool_commit, "<name>", ...)`` keep resolving.

Business responsibility: record official-job / official-full-gate /
bootstrap-required / inconclusive / full-pass checkpoints and run the
official-full commit gate LLM-bound step.

CRITICAL: every intra-companion call to a moved symbol routes through
``_tc.<name>(...)`` so that ``monkeypatch.setattr(tool_commit, "<name>", ...)``
fired by tests is honoured even when the body lives in this companion.
"""
from __future__ import annotations

import os
import time

import tool_commit as _tc  # for cross-refs to constants/other helpers


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
    projection = _tc._official_job_projection(official_full_gate)
    existing = (ckpt or {}).get("official_job")
    expected_job_id = str(existing.get("job_id") or "") if isinstance(existing, dict) else ""
    return bool(_tc.write_pipeline_checkpoint(
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
    # This terminal record is a side effect of one exact official-job read.
    # Never use a best-effort stage write here: a concurrently attached fresh
    # job or a newer checkpoint must win rather than being replaced by stale
    # quality-admission evidence.
    if (
        not isinstance(ckpt, dict)
        or ckpt.get("next_v") != v
        or ckpt.get("source_v") != source_v
        or type(ckpt.get("checkpoint_revision")) is not int
        or ckpt.get("checkpoint_revision") < 1
        or not isinstance(ckpt.get("stage"), str)
        or not ckpt.get("stage")
        or not isinstance(ckpt.get("workflow_run_id"), str)
        or not ckpt.get("workflow_run_id")
    ):
        return ""
    quality_admission_blocked = bool(
        official_full_gate.get("outcome") == "quality_admission_blocked"
        and official_full_gate.get("failure_class") == "quality"
    )
    bot_blocker = _tc._official_gate_is_bot_blocker(official_full_gate)
    # A live admission drift is not a platform/evidence gap and not a bot-side
    # EXE finding.  Keep the existing certification stage so the only legal
    # continuation is a fresh quality gate; its normal evidence chain then
    # drives review, critic, precommit and a brand-new official job.
    stage = (
        "official_certifying"
        if quality_admission_blocked
        else "official_failed" if bot_blocker else "official_inconclusive"
    )
    gate_payload = {
        **official_full_gate,
        "passed": False,
        "repairable_by_workers": bot_blocker and not quality_admission_blocked,
        "quality_admission_refresh": quality_admission_blocked,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    recorded = _tc.write_pipeline_checkpoint(
        v,
        source_v,
        stage,
        master_plan=(ckpt or {}).get("master_plan"),
        # A quality receipt drift is system-owned admission evidence, not
        # worker repair feedback.  Injecting it into the reviewer field would
        # create a false historical lesson for later model prompts.
        reviewer_feedback=(
            (ckpt or {}).get("reviewer_feedback", "")
            if quality_admission_blocked
            else _tc._official_gate_feedback(official_full_gate)
        ),
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
            _tc.infrastructure_failure_digest((ckpt or {}).get("infra_failure"))
            if clear_infra_failure
            else None
        ),
        clear_official_job=clear_official_job,
        expected_official_job_id=(
            str(((ckpt or {}).get("official_job") or {}).get("job_id") or "")
            if clear_official_job
            else None
        ),
        touch_stage_timestamp=quality_admission_blocked,
        expected_checkpoint_revision=ckpt["checkpoint_revision"],
        expected_checkpoint_stage=ckpt["stage"],
        expected_workflow_run_id=ckpt["workflow_run_id"],
    )
    return stage if recorded else ""


def _record_official_bootstrap_required_checkpoint(
    v: int,
    source_v: int,
    ckpt: dict | None,
    official_full_gate: dict,
    *,
    candidate_hash: str,
) -> bool:
    """Park v143 before the explicit one-time system-control authorization."""
    from official_bootstrap import build_operator_bootstrap_parked_request

    parked = build_operator_bootstrap_parked_request(
        _tc.get_bot_dir(v),
        ckpt or {},
        candidate_hash=candidate_hash,
    )
    if parked.get("valid") is not True:
        _tc.log_system_event(
            "pipeline.official_bootstrap_parking_refused",
            "error",
            f"Refused to park v{v}: bootstrap authorization contract is invalid",
            {
                "version": v,
                "source_v": source_v,
                "issues": (parked.get("issues") or [])[:20],
            },
        )
        return False
    parked_request = parked["request"]
    gate_payload = {
        **official_full_gate,
        "passed": False,
        "operator_action_required": True,
        "repairable_by_workers": False,
        "official_bootstrap_request_digest": parked_request["request_digest"],
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    audit_context = dict((ckpt or {}).get("audit_context", {}) or {})
    audit_context["official_bootstrap_request"] = parked_request
    return bool(_tc.write_pipeline_checkpoint(
        v,
        source_v,
        "official_bootstrap_required",
        master_plan=(ckpt or {}).get("master_plan"),
        generation_attempt=(ckpt or {}).get("generation_attempt", 0),
        gate_results={"official_full": gate_payload},
        worker_failure_count=(ckpt or {}).get("worker_failure_count", 0),
        parent2_v=(ckpt or {}).get("parent2_v"),
        direction_audit=(ckpt or {}).get("direction_audit"),
        audit_context=audit_context,
        audit_attempt=(ckpt or {}).get("audit_attempt", 0),
        precommit_attempt=(ckpt or {}).get("precommit_attempt", 0),
        precommit_rework_count=(ckpt or {}).get("precommit_rework_count", 0),
        literature_probe=(ckpt or {}).get("literature_probe"),
        prepare_scope_files=(ckpt or {}).get("prepare_scope_files", []) or [],
        expected_checkpoint_revision=(ckpt or {}).get("checkpoint_revision"),
        expected_checkpoint_stage="verified",
        expected_workflow_run_id=(ckpt or {}).get("workflow_run_id"),
    ))


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
    bootstrap_pass = bool(
        official_full_gate.get("bootstrap_certificate")
        or (
            (((official_full_gate.get("status") or {}).get(
                "certification_identity"
            ) or {}).get("spec") or {}).get("bootstrap_control_id")
        )
    )
    # A parked first-strict candidate is intentionally non-routable until the
    # external operator ceremony produces a complete signed certificate.  Once
    # that certificate is validated here, linearize back through ``verified``;
    # publication can then use the same verified -> publishing CAS as every
    # later bot.  Keeping the stage parked made the subsequent publishing CAS
    # an impossible official_bootstrap_required -> publishing transition.
    target_stage = (
        "verified"
        if bootstrap_pass
        and (ckpt or {}).get("stage") == "official_bootstrap_required"
        else "official_certifying"
        if (ckpt or {}).get("stage") == "official_certifying"
        else "verified"
    )
    return bool(_tc.write_pipeline_checkpoint(
        v,
        source_v,
        target_stage,
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
            _tc.infrastructure_failure_digest((ckpt or {}).get("infra_failure"))
            if clear_infra_failure
            else None
        ),
        clear_official_job=clear_official_job,
        expected_official_job_id=(
            str(((ckpt or {}).get("official_job") or {}).get("job_id") or "")
            if clear_official_job
            else None
        ),
        expected_checkpoint_revision=(ckpt or {}).get("checkpoint_revision"),
        expected_checkpoint_stage=(ckpt or {}).get("stage"),
        expected_workflow_run_id=(ckpt or {}).get("workflow_run_id"),
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
    execution_mode = _tc._checkpoint_execution_mode(ckpt, gate_results)
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

    # A manually started bootstrap-first-strict job is deliberately outside the
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
        completed_bootstrap_authorization = None
        if existing_spec.get("bootstrap_control_id"):
            from official_bootstrap import (
                validate_completed_operator_bootstrap_authorization,
            )

            completed_bootstrap_authorization = (
                validate_completed_operator_bootstrap_authorization(
                    existing_status,
                    bot_dir,
                    checkpoint=ckpt,
                )
            )
            if completed_bootstrap_authorization.get("valid") is not True:
                return {
                    "passed": False,
                    "outcome": "completed_authorization_failure",
                    "failure_class": "authorization",
                    "error": (
                        "COMMIT BLOCKED: completed bootstrap certificate no longer "
                        "matches the parked generation authorization."
                    ),
                    "version": v,
                    "source_v": source_v,
                    "status": existing_status,
                    "opponent_selection": opponent_selection,
                    "issues": completed_bootstrap_authorization.get("issues") or [],
                    "completed_bootstrap_authorization": (
                        completed_bootstrap_authorization
                    ),
                    "reused_existing_certificate": True,
                    "bootstrap_certificate": True,
                }
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
            "bootstrap_certificate": bool(
                existing_spec.get("bootstrap_control_id")
            ),
            "completed_bootstrap_authorization": (
                completed_bootstrap_authorization
            ),
        }

    opponent_selection = select_official_opponent(
        bot_dir,
        _tc.get_active_bots(),
        preferred=_tc._official_preferred_opponent(),
        allow_bootstrap_grandfather=False,
    )
    if not opponent_selection.get("selected"):
        return {
            "passed": False,
            "outcome": "operator_bootstrap_required",
            "operator_action_required": True,
            "action": "run_explicit_first_strict_bootstrap",
            "error": "OFFICIAL FULL CERTIFICATION BLOCKED: no eligible official EXE opponent.",
            "version": v,
            "source_v": source_v,
            "opponent_selection": opponent_selection,
        }

    opponent = opponent_selection["opponent"]
    opponent_path = opponent["path"]

    quality_admission = None
    if v >= _tc.FIRST_STRICT_POLICY_VERSION:
        # The automatic normal full-v5 path must bind the exact
        # checkpoint-owned quality/capability/probe receipt *before* allocating
        # a durable official job.  The worker/harness will recompute and
        # compare the same receipt immediately before EXE work; no later live
        # regeneration can fill a missing field in this identity-bearing spec.
        from official_platform_harness import build_formal_quality_admission

        quality_admission_report = build_formal_quality_admission(
            bot_dir,
            checkpoint=ckpt,
            repo_root=_tc.PROJECT_ROOT,
        )
        if quality_admission_report.get("valid") is not True:
            return {
                "passed": False,
                "outcome": "quality_admission_blocked",
                "failure_class": "quality",
                "error": (
                    "OFFICIAL FULL CERTIFICATION BLOCKED: current checkpoint-owned "
                    "quality admission is invalid."
                ),
                "version": v,
                "source_v": source_v,
                "opponent_selection": opponent_selection,
                "quality_admission": quality_admission_report,
                "issues": list(quality_admission_report.get("issues") or []),
            }
        quality_admission = quality_admission_report["admission"]
    spec = build_spec(
        "full",
        bot_dir,
        opponent=opponent_path,
        quality_admission=quality_admission,
    )
    job = await _tc.run_blocking_isolated(
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
        failure_class = str(job.get("failure_class") or "infrastructure")
        quality_blocked = failure_class == "quality"
        return {
            "passed": False,
            "pending": False,
            "outcome": (
                "quality_admission_blocked"
                if quality_blocked
                else "infrastructure_failure"
            ),
            "failure_class": failure_class,
            "error": (
                "OFFICIAL FULL CERTIFICATION BLOCKED: live quality admission drifted."
                if quality_blocked
                else None
            ),
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
            if _tc._official_gate_is_bot_blocker(result)
            else "infrastructure_failure"
        )
        if result["outcome"] == "infrastructure_failure":
            result["failure_class"] = "infrastructure"
    else:
        result["outcome"] = "passed"
    return result
