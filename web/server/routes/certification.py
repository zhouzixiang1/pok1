"""Strict-epoch official EXE certification projections.

The durable official-job store is an execution detail, not a historical HTTP
catalogue.  This router exposes at most the full-v5 job attached to the exact
current strict workflow or the sole parked-request-bound manual v143 bootstrap
job, plus content-bound status for current published bots.  Unpublished
directories, retired-epoch jobs, smoke/compliance probes, and unbound bootstrap
jobs have no API authority.
"""

from __future__ import annotations

import fcntl
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from blocking_runtime import run_blocking_isolated
from bot_artifact import canonical_digest, hash_path, published_bot_identity
from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    ROLE_CANDIDATE,
    ROLE_PARENT_SOURCE,
    bot_name,
    resolve_national_bot_spec,
)
from checkpoint_schema import (
    CheckpointSchemaError,
    strict_checkpoint_event_identity,
)
from epoch_authority import (
    first_strict_operator_transition,
    strict_epoch_projection,
)
from official_certification import (
    FULL_POLICY_ID,
    _opponent_selection_issues,
    _spec_from_mapping,
    authoritative_verdict_status_issues,
    official_compliance_verdict,
    official_certification_profile_projection,
    official_full_certified,
    official_opponent_eligibility,
    stable_official_opponent_selection,
    status_payload,
)
from official_certification_job import (
    _read_json as _read_official_job_json,
    _validate_request as _validate_official_job_request,
    cancel_job,
    get_job,
    job_root,
)
from official_job_envelope import job_envelope_issues
from first_strict_control import CONTROL_ID as FIRST_STRICT_CONTROL_ID
from server.operator_control import require_operator_mutation


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOTS_DIR = PROJECT_ROOT / "bots"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OFFICIAL_STAGE = "official_certifying"
_BOOTSTRAP_STAGE = "official_bootstrap_required"
_NORMAL_JOB_STAGES = frozenset({
    _OFFICIAL_STAGE,
    "official_failed",
    "official_inconclusive",
    "publishing",
})
_FORMAL_ROUNDS = {"self_play": 5, "opponent": 3}

router = APIRouter(prefix="/api/certification", tags=["certification"])


def _formal_job_profile(*, bootstrap: bool) -> dict[str, Any]:
    """Return the already-validated official execution/profile projection."""

    return {
        "certification_profile": (
            "first_strict_control_v1" if bootstrap else FULL_POLICY_ID
        ),
        "opponent_authority": (
            "system_control" if bootstrap else "strict_published_pool"
        ),
        "formal_profile": {
            "self_play_rounds": _FORMAL_ROUNDS["self_play"],
            "opponent_rounds": _FORMAL_ROUNDS["opponent"],
            "target_hands": 70,
        },
        # Official EXE evidence is compliance-only in both profiles.
        "strength_evidence_weight": 0,
        "strategy_evidence_weight": 0,
    }


def _normal_opponent_authority_issues(
    selection: dict[str, Any],
    spec: Any,
    identity: dict[str, Any],
) -> list[str]:
    """Reopen the selected normal opponent's publication and verdict chain."""

    issues = list(_opponent_selection_issues(selection, spec, identity))
    opponent = selection.get("opponent")
    if issues or not isinstance(opponent, dict):
        return list(dict.fromkeys(issues or ["normal_opponent_selection_invalid"]))
    try:
        path = Path(str(opponent.get("path") or "")).expanduser().resolve()
        publication = published_bot_identity(path)
    except Exception as exc:
        return [f"normal_opponent_publication_error:{type(exc).__name__}"]
    if publication.get("published") is not True:
        issues.extend(
            str(item)
            for item in (
                publication.get("issues")
                or ["normal_opponent_not_strict_published"]
            )
        )
    expected_publication = {
        "bot": publication.get("label"),
        "path": publication.get("path"),
        "artifact_hash": publication.get("artifact_hash"),
        "tag": publication.get("tag"),
        "tag_object": publication.get("tag_object"),
    }
    observed_publication = {
        "bot": opponent.get("bot"),
        "path": str(path),
        "artifact_hash": opponent.get("artifact_hash"),
        "tag": opponent.get("tag"),
        "tag_object": opponent.get("tag_object"),
    }
    if observed_publication != expected_publication:
        issues.append("normal_opponent_published_identity_mismatch")
    try:
        eligibility = official_opponent_eligibility(path)
    except Exception as exc:
        issues.append(f"normal_opponent_certificate_validation_error:{type(exc).__name__}")
        eligibility = {}
    if (
        eligibility.get("eligible") is not True
        or eligibility.get("reason") != "official_certified"
    ):
        issues.append("normal_opponent_latest_official_certificate_invalid")
    if eligibility.get("eligibility_receipt") != opponent.get("eligibility_receipt"):
        issues.append("normal_opponent_eligibility_receipt_stale")
    return list(dict.fromkeys(str(item) for item in issues if str(item)))


def _job_status_projection(
    state: dict[str, Any],
    candidate: Path,
) -> dict[str, Any]:
    """Expose terminal/running status without deriving truth from job.state."""

    status = state.get("status") if isinstance(state.get("status"), dict) else None
    verdict = official_compliance_verdict(status) if status is not None else None
    issues: list[str] = []
    if status is not None:
        issues.extend(str(item) for item in (status.get("issues") or []))
    for key in ("error", "failure_reason", "cancel_reason"):
        value = state.get(key)
        if value:
            issues.append(f"{key}:{str(value)[:300]}")
    failure = state.get("failure")
    if isinstance(failure, dict):
        for key in ("code", "classification", "message", "error"):
            value = failure.get(key)
            if value:
                issues.append(f"failure_{key}:{str(value)[:300]}")
    elif failure:
        issues.append(f"failure:{str(failure)[:300]}")
    certificate_digest = None
    if (
        status is not None
        and state.get("state") == "completed"
        and status.get("status") != "official-certified"
    ):
        status_issues = authoritative_verdict_status_issues(status)
        if status_issues:
            issues.extend(status_issues)
            verdict = {
                "ok": False,
                "classification": "terminal_status_validation_failed",
                "blocking": True,
                "inconclusive": True,
            }
    if (
        status is not None
        and state.get("state") == "completed"
        and status.get("status") == "official-certified"
    ):
        try:
            if official_full_certified(status, candidate):
                certificate_digest = str(status.get("certificate_digest") or "") or None
            else:
                issues.append("completed_certificate_validation_failed")
                verdict = {
                    "ok": False,
                    "classification": "certificate_validation_failed",
                    "blocking": True,
                    "inconclusive": True,
                }
        except Exception as exc:
            issues.append(
                f"completed_certificate_validation_error:{type(exc).__name__}"
            )
            verdict = {
                "ok": False,
                "classification": "certificate_validation_error",
                "blocking": True,
                "inconclusive": True,
            }
    return {
        "status": status,
        "official_status": status.get("status") if status is not None else None,
        "compliance_verdict": verdict,
        "issues": list(dict.fromkeys(issues)),
        "certificate_digest": certificate_digest,
    }


def _bootstrap_operator_transition(
    context: dict[str, Any],
    job: dict[str, Any] | None,
    *,
    discovery_issue: str | None = None,
) -> dict[str, Any]:
    """Resolve the four-state operator handoff from one validated job view."""

    checkpoint = context["checkpoint"]
    if discovery_issue:
        return first_strict_operator_transition(
            checkpoint,
            state="bootstrap_failed",
            reason=discovery_issue,
        )
    if job is None:
        return first_strict_operator_transition(checkpoint)

    job_id = str(job.get("job_id") or "") or None
    state = str(job.get("state") or "")
    bindings: dict[str, Any] = {"job_id": job_id}
    if state in {
        "created",
        "queued",
        "starting",
        "running",
        "finalizing",
        "cancel_requested",
    }:
        return first_strict_operator_transition(
            checkpoint,
            state="bootstrap_running",
            reason=f"authorized_bootstrap_job_{state}",
            **bindings,
        )
    if state == "completed":
        digest = str(job.get("certificate_digest") or "")
        verdict = job.get("compliance_verdict")
        if (
            job.get("official_status") == "official-certified"
            and _HEX64.fullmatch(digest) is not None
            and isinstance(verdict, dict)
            and verdict.get("ok") is True
            and verdict.get("blocking") is False
            and verdict.get("inconclusive") is False
            and not job.get("issues")
        ):
            return first_strict_operator_transition(
                checkpoint,
                state="ready_to_finalize",
                reason="bootstrap_certificate_and_authorization_verified",
                certificate_digest=digest,
                **bindings,
            )
        reason = "bootstrap_completed_without_valid_certificate"
        if isinstance(verdict, dict) and verdict.get("classification"):
            reason = f"bootstrap_completed:{verdict['classification']}"
    elif state in {"failed", "cancelled"}:
        reason = f"authorized_bootstrap_job_{state}"
    else:
        reason = "authorized_bootstrap_job_state_invalid"
    return first_strict_operator_transition(
        checkpoint,
        state="bootstrap_failed",
        reason=reason,
        **bindings,
    )


def _projection() -> dict[str, Any]:
    """Return a fail-closed canonical epoch projection for HTTP readers."""

    try:
        value = strict_epoch_projection()
    except Exception:
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {
        "evaluation_epoch": EVALUATION_EPOCH,
        "state": str(value.get("state") or "unavailable"),
        "initialized": value.get("initialized") is True,
        "reset_receipt_valid": value.get("reset_receipt_valid") is True,
        "active_bots": list(value.get("active_bots") or []),
        "active_generation": (
            dict(value["active_generation"])
            if isinstance(value.get("active_generation"), dict)
            else None
        ),
    }


def _authority_fields(
    projection: dict[str, Any],
    *,
    workflow_run_id: str | None = None,
    candidate_version: int | None = None,
) -> dict[str, Any]:
    return {
        "evaluation_epoch": EVALUATION_EPOCH,
        "epoch_state": projection.get("state"),
        "epoch_initialized": projection.get("initialized") is True,
        "workflow_run_id": workflow_run_id,
        "candidate_version": candidate_version,
        "formal_policy_id": FULL_POLICY_ID,
        "formal_mode": "full",
    }


def _current_candidate_context(
    projection: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve the exact current checkpoint candidate and workflow identity."""

    projection = projection or _projection()
    active = projection.get("active_generation")
    if (
        projection.get("initialized") is not True
        or projection.get("reset_receipt_valid") is not True
        or not isinstance(active, dict)
        or type(active.get("next_v")) is not int
    ):
        return None

    import evolution_infra as infra

    checkpoint = infra.read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        return None
    version = active["next_v"]
    try:
        identity = strict_checkpoint_event_identity(
            checkpoint,
            expected_gen=version,
            project_root=PROJECT_ROOT,
        )
    except Exception:
        return None

    workflow_run_id = str(identity.get("workflow_run_id") or "")
    generation_attempt = int(checkpoint.get("generation_attempt") or 0)
    checkpoint_run_id = checkpoint.get("run_id") or f"{version}#{generation_attempt}"
    if (
        not workflow_run_id
        or checkpoint.get("workflow_run_id") != workflow_run_id
        or active.get("workflow_run_id") != workflow_run_id
        or checkpoint.get("next_v") != version
        or active.get("source_v") != checkpoint.get("source_v")
        or active.get("parent2_v") != checkpoint.get("parent2_v")
        or active.get("stage") != checkpoint.get("stage")
        or active.get("run_id") != checkpoint_run_id
        or active.get("checkpoint_revision")
        != checkpoint.get("checkpoint_revision")
    ):
        return None

    try:
        candidate = BOTS_DIR / bot_name(version)
        spec = resolve_national_bot_spec(
            candidate,
            ROLE_CANDIDATE,
            repo_root=BOTS_DIR.parent,
            require_completion=False,
            require_certificate=False,
        )
    except Exception:
        return None
    if not spec.eligible:
        return None
    try:
        candidate_hash = hash_path(candidate)
    except Exception:
        return None
    if _HEX64.fullmatch(candidate_hash) is None:
        return None
    return {
        "projection": projection,
        "checkpoint": checkpoint,
        "version": version,
        "workflow_run_id": workflow_run_id,
        "run_id": checkpoint_run_id,
        "candidate": candidate.resolve(),
        "candidate_hash": candidate_hash,
    }


def _published_subject(version: int, projection: dict[str, Any]) -> Path | None:
    if projection.get("initialized") is not True:
        return None
    try:
        name = bot_name(version)
    except (TypeError, ValueError):
        return None
    if name not in set(projection.get("active_bots") or []):
        return None
    path = BOTS_DIR / name
    try:
        spec = resolve_national_bot_spec(
            path,
            ROLE_PARENT_SOURCE,
            repo_root=BOTS_DIR.parent,
        )
    except Exception:
        return None
    return path.resolve() if spec.eligible else None


def _visible_subject(version: int) -> tuple[Path, str, dict[str, Any], str | None]:
    projection = _projection()
    published = _published_subject(version, projection)
    if published is not None:
        return published, "strict_published", projection, None
    current = _current_candidate_context(projection)
    if current is not None and current["version"] == version:
        return (
            current["candidate"],
            "active_candidate",
            projection,
            current["workflow_run_id"],
        )
    raise HTTPException(status_code=404, detail=f"Bot v{version} not found")


def _official_prerequisite_issues(context: dict[str, Any]) -> list[str]:
    """Reuse the commit gate ledger; HTTP never defines a weaker ready state."""

    checkpoint = context["checkpoint"]
    if checkpoint.get("stage") not in _NORMAL_JOB_STAGES:
        return ["checkpoint_not_official_certifying"]
    if checkpoint.get("national_execution_mode") != "native_tcp":
        return ["checkpoint_execution_mode_not_native_tcp"]
    try:
        from tool_commit import validate_commit_gate_ledger

        ledger = validate_commit_gate_ledger(
            context["version"],
            checkpoint.get("source_v"),
            checkpoint,
            bot_dir=context["candidate"],
        )
    except Exception as exc:
        return [f"commit_gate_ledger_error:{type(exc).__name__}"]
    issues: list[str] = []
    if ledger.get("ok") is not True:
        issues.extend(str(item) for item in ledger.get("missing_gates") or [])
        issues.extend(
            str(item.get("gate") or item.get("reason") or "failed_gate")
            if isinstance(item, dict)
            else str(item)
            for item in ledger.get("failed_gates") or []
        )
    if ledger.get("current_code_fingerprint") != context["candidate_hash"]:
        issues.append("candidate_artifact_hash_mismatch")
    return list(dict.fromkeys(issues))


def _safe_job_request(job_id: str) -> tuple[dict[str, Any], Path] | None:
    if _HEX64.fullmatch(job_id) is None:
        return None
    root = job_root()
    directory = root / job_id
    try:
        if root.is_symlink() or directory.is_symlink() or not directory.is_dir():
            return None
        if directory.parent.resolve() != root.resolve():
            return None
        request = _read_official_job_json(directory / "request.json")
    except Exception:
        return None
    try:
        request_issues = _validate_official_job_request(request or {})
    except Exception:
        return None
    if not isinstance(request, dict) or request_issues:
        return None
    if request.get("job_id") != job_id:
        return None
    return request, directory.resolve()


def _unvalidated_job_request(directory: Path) -> dict[str, Any] | None:
    """Read identity hints without granting a malformed request authority."""

    path = directory / "request.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = _read_official_job_json(path)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _parked_bootstrap_request(context: dict[str, Any]) -> dict[str, Any] | None:
    """Return the digest-bound v143 parking request, or fail closed.

    The exceptional first-strict run is authorized by the parked checkpoint,
    not by the presence of a job directory.  Keep the cheap, local bindings
    here as an independent boundary before invoking the full bootstrap
    authority validator below.
    """

    checkpoint = context["checkpoint"]
    if (
        checkpoint.get("stage") != _BOOTSTRAP_STAGE
        or checkpoint.get("national_execution_mode") != "native_tcp"
        or context.get("version") != FIRST_STRICT_POLICY_VERSION
        or checkpoint.get("next_v") != FIRST_STRICT_POLICY_VERSION
        or checkpoint.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
        or context.get("workflow_run_id") != checkpoint.get("workflow_run_id")
        or context.get("projection", {}).get("active_bots") != []
    ):
        return None
    audit_context = checkpoint.get("audit_context")
    parked = (
        audit_context.get("official_bootstrap_request")
        if isinstance(audit_context, dict)
        else None
    )
    if not isinstance(parked, dict):
        return None
    unsigned = {key: value for key, value in parked.items() if key != "request_digest"}
    request_digest = parked.get("request_digest")
    try:
        parked_candidate = Path(str(parked.get("candidate_path") or "")).resolve()
    except Exception:
        return None
    if (
        not isinstance(request_digest, str)
        or _HEX64.fullmatch(request_digest) is None
        or request_digest != canonical_digest(unsigned)
        or parked.get("schema_version") != 1
        or parked.get("kind")
        != "official-first-strict-control-parked-request"
        or parked.get("candidate_version") != FIRST_STRICT_POLICY_VERSION
        or parked.get("candidate_label") != bot_name(FIRST_STRICT_POLICY_VERSION)
        or parked_candidate != context.get("candidate")
        or parked.get("candidate_hash") != context.get("candidate_hash")
        or parked.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
        or parked.get("workflow_run_id") != context.get("workflow_run_id")
        or parked.get("bootstrap_control_id") != FIRST_STRICT_CONTROL_ID
        or parked.get("active_bots") != []
        or parked.get("strict_published_bots") != []
    ):
        return None
    return parked


def _formal_progress_valid(state: dict[str, Any]) -> bool:
    """Reject forged/corrupt progress instead of rendering it as 5+3 truth."""

    progress = state.get("progress")
    if progress is None:
        return not (
            state.get("state") in {"starting", "running", "finalizing"}
            and int(state.get("attempt", 0) or 0) > 0
        )
    if not isinstance(progress, dict):
        return False
    if state.get("progress_digest") != canonical_digest(progress):
        return False
    try:
        requested = int(progress.get("rounds_requested"))
        completed = int(progress.get("rounds_completed"))
        passed = int(progress.get("rounds_passed"))
    except (TypeError, ValueError):
        return False
    if (
        requested != sum(_FORMAL_ROUNDS.values())
        or completed < 0
        or completed > requested
        or passed < 0
        or passed > completed
    ):
        return False
    rows = progress.get("rounds")
    if not isinstance(rows, list) or len(rows) > requested:
        return False
    seen: set[tuple[str, int]] = set()
    completed_rows = 0
    active_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            return False
        kind = str(row.get("kind") or "")
        try:
            index = int(row.get("index"))
            hands = int(row.get("hands_started", 0) or 0)
            settlements = int(row.get("settlements", 0) or 0)
            issue_count = int(row.get("issue_count", 0) or 0)
        except (TypeError, ValueError):
            return False
        key = (kind, index)
        if (
            kind not in _FORMAL_ROUNDS
            or index < 1
            or index > _FORMAL_ROUNDS[kind]
            or key in seen
            or hands < 0
            or hands > 70
            or settlements < 0
            or settlements > 70
            or issue_count < 0
        ):
            return False
        seen.add(key)
        duration = row.get("duration_sec")
        if duration is None:
            active_rows.append(row)
        else:
            try:
                if float(duration) < 0:
                    return False
            except (TypeError, ValueError):
                return False
            completed_rows += 1
    active = progress.get("active_round")
    if active is None:
        if active_rows:
            return False
    elif (
        not isinstance(active, dict)
        or len(active_rows) != 1
        or active != active_rows[0]
    ):
        return False
    return completed_rows == completed


def _bootstrap_request_view(
    context: dict[str, Any],
    parked: dict[str, Any],
    request: dict[str, Any],
    directory: Path,
) -> dict[str, Any] | None:
    """Validate one durable job as the exact current manual v143 run."""

    request_spec = request.get("spec")
    identity = request.get("identity")
    selection = request.get("opponent_selection")
    if not all(isinstance(value, dict) for value in (request_spec, identity, selection)):
        return None
    try:
        spec = _spec_from_mapping(request_spec)
        candidate = Path(spec.candidate).expanduser().resolve()
        opponent = Path(str(spec.opponent or "")).expanduser().resolve()
        selected_candidate = Path(str(selection.get("candidate") or "")).resolve()
        selected_opponent = selection.get("opponent")
        selected_opponent_path = Path(
            str((selected_opponent or {}).get("path") or "")
        ).resolve()
    except Exception:
        return None
    if not isinstance(selected_opponent, dict):
        return None
    identity_payload = {
        key: value for key, value in identity.items() if key != "identity_digest"
    }
    platform = identity.get("platform")
    authorization = selection.get("operator_bootstrap_authorization")
    authorization_payload = (
        {
            key: value
            for key, value in authorization.items()
            if key != "authorization_digest"
        }
        if isinstance(authorization, dict)
        else {}
    )
    if (
        spec.mode != "full"
        or spec.policy_id != FULL_POLICY_ID
        or spec.bootstrap_control_id != FIRST_STRICT_CONTROL_ID
        or spec.self_play_rounds != _FORMAL_ROUNDS["self_play"]
        or spec.opponent_rounds != _FORMAL_ROUNDS["opponent"]
        or spec.target_hands != 70
        or candidate != context["candidate"]
        or selected_candidate != context["candidate"]
        or selected_opponent_path != opponent
        or selection.get("selected") is not True
        or selection.get("eligible") is not True
        or selection.get("bootstrap_control_id") != FIRST_STRICT_CONTROL_ID
        or selected_opponent.get("eligible") is not True
        or identity.get("identity_digest") != canonical_digest(identity_payload)
        or identity.get("policy_id") != FULL_POLICY_ID
        or identity.get("spec") != request_spec
        or identity.get("candidate_hash") != context["candidate_hash"]
        or identity.get("opponent_hash") != selected_opponent.get("artifact_hash")
        or identity.get("runner_provenance") != "official-exe"
        or identity.get("authority_scope") != "production"
        or identity.get("test_only") is not False
        or request.get("source_v") is not None
        or not isinstance(request.get("manager_sha256"), str)
        or _HEX64.fullmatch(request["manager_sha256"]) is None
        or not isinstance(platform, dict)
        or identity.get("platform_fingerprint") != canonical_digest(platform)
        or not isinstance(authorization, dict)
        or authorization.get("authorization_digest")
        != canonical_digest(authorization_payload)
        or authorization.get("parked_request_digest")
        != parked.get("request_digest")
        or authorization.get("workflow_run_id") != context["workflow_run_id"]
        or authorization.get("candidate_path") != str(context["candidate"])
        or authorization.get("candidate_version") != FIRST_STRICT_POLICY_VERSION
        or authorization.get("candidate_hash") != context["candidate_hash"]
        or authorization.get("bootstrap_control_id") != FIRST_STRICT_CONTROL_ID
    ):
        return None

    try:
        state = get_job(str(request["job_id"]))
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    try:
        state_candidate = Path(str(state.get("candidate") or "")).resolve()
        state_directory = Path(str(state.get("job_dir") or "")).resolve()
    except Exception:
        return None
    if (
        state.get("schema_version") != request.get("schema_version")
        or state.get("manager_version") != request.get("manager_version")
        or state.get("job_id") != request.get("job_id")
        or state.get("request_digest") != request.get("request_digest")
        or state.get("state") not in {
            "created",
            "queued",
            "starting",
            "running",
            "finalizing",
            "cancel_requested",
            "completed",
            "failed",
            "cancelled",
        }
        or state_candidate != context["candidate"]
        or state_directory != directory
        or not _formal_progress_valid(state)
    ):
        return None

    status = state.get("status")
    terminal_validation_issues: list[str] = []
    if state.get("state") == "completed":
        if not isinstance(status, dict):
            return None
        status_selection = status.get("opponent_selection")
        envelope = status.get("official_job_envelope")
        if (
            status.get("mode") != "full"
            or status.get("policy_id") != FULL_POLICY_ID
            or status.get("certification_identity") != identity
            or not isinstance(status_selection, dict)
            or stable_official_opponent_selection(status_selection)
            != stable_official_opponent_selection(selection)
            or not isinstance(envelope, dict)
            or envelope.get("certification_identity_digest")
            != identity.get("identity_digest")
            or envelope.get("opponent_selection") != selection
            or job_envelope_issues(
                envelope,
                expected_job_id=str(request["job_id"]),
                expected_request_digest=str(request["request_digest"]),
                expected_attempt=int(state.get("attempt", 0) or 0),
                expected_candidate_hash=context["candidate_hash"],
                expected_opponent_hash=str(identity.get("opponent_hash") or ""),
            )
        ):
            return None
        if status.get("status") == "official-certified":
            try:
                from official_bootstrap import (
                    validate_completed_operator_bootstrap_authorization,
                )

                completed = validate_completed_operator_bootstrap_authorization(
                    status,
                    context["candidate"],
                    checkpoint=context["checkpoint"],
                )
            except Exception:
                return None
            if completed.get("valid") is not True:
                terminal_validation_issues.extend(
                    str(item)
                    for item in (
                        completed.get("issues")
                        or ["completed_bootstrap_authorization_invalid"]
                    )
                )
            try:
                certified = official_full_certified(
                    status,
                    context["candidate"],
                )
            except Exception as exc:
                certified = False
                terminal_validation_issues.append(
                    f"completed_certificate_validation_error:{type(exc).__name__}"
                )
            if not certified:
                terminal_validation_issues.append(
                    "completed_certificate_validation_failed"
                )
        else:
            terminal_validation_issues.extend(
                authoritative_verdict_status_issues(status)
            )
            try:
                from official_bootstrap import (
                    validate_operator_bootstrap_authorized_selection,
                )

                live_authorization = validate_operator_bootstrap_authorized_selection(
                    selection,
                    FIRST_STRICT_CONTROL_ID,
                    context["candidate"],
                    checkpoint=context["checkpoint"],
                )
            except Exception:
                return None
            if live_authorization.get("valid") is not True:
                terminal_validation_issues.extend(
                    str(item)
                    for item in (
                        live_authorization.get("issues")
                        or ["bootstrap_authorization_invalid"]
                    )
                )
    else:
        if status is not None:
            return None
        try:
            from official_bootstrap import (
                validate_operator_bootstrap_authorized_selection,
            )

            live_authorization = validate_operator_bootstrap_authorized_selection(
                selection,
                FIRST_STRICT_CONTROL_ID,
                context["candidate"],
                checkpoint=context["checkpoint"],
            )
        except Exception:
            return None
        if live_authorization.get("valid") is not True:
            return None

    status_projection = _job_status_projection(state, context["candidate"])
    if terminal_validation_issues:
        status_projection["issues"] = list(dict.fromkeys([
            *(status_projection.get("issues") or []),
            *terminal_validation_issues,
        ]))
        status_projection["certificate_digest"] = None
        status_projection["compliance_verdict"] = {
            "ok": False,
            "classification": "bootstrap_terminal_validation_failed",
            "blocking": True,
            "inconclusive": True,
        }
    return {
        **state,
        **_authority_fields(
            context["projection"],
            workflow_run_id=context["workflow_run_id"],
            candidate_version=context["version"],
        ),
        "subject_kind": "active_candidate",
        "formal_authority": "operator_bootstrap_full_v5_job",
        "bootstrap_control_id": FIRST_STRICT_CONTROL_ID,
        "read_only": True,
        "cancel_allowed": False,
        **_formal_job_profile(bootstrap=True),
        **status_projection,
    }


def _bootstrap_request_related(
    context: dict[str, Any],
    parked: dict[str, Any],
    request: dict[str, Any],
) -> bool:
    """Identify a job claiming the exact parked authority without trusting it."""

    raw_spec = request.get("spec") if isinstance(request.get("spec"), dict) else {}
    try:
        candidate = Path(str(raw_spec.get("candidate") or "")).expanduser().resolve()
    except Exception:
        candidate = None
    selection = request.get("opponent_selection")
    selection = selection if isinstance(selection, dict) else {}
    authorization = (
        selection.get("operator_bootstrap_authorization")
        if isinstance(selection.get("operator_bootstrap_authorization"), dict)
        else {}
    )
    control_claimed = bool(
        raw_spec.get("bootstrap_control_id") == FIRST_STRICT_CONTROL_ID
        or selection.get("bootstrap_control_id") == FIRST_STRICT_CONTROL_ID
    )
    candidate_claimed = candidate == context["candidate"]
    parked_binding_claimed = bool(
        authorization.get("parked_request_digest") == parked.get("request_digest")
        or authorization.get("workflow_run_id") == context["workflow_run_id"]
        or authorization.get("candidate_hash") == context["candidate_hash"]
    )
    return bool(
        (control_claimed and candidate_claimed)
        or (control_claimed and parked_binding_claimed)
        or (
            candidate_claimed
            and request.get("source_v") is None
            and bool(authorization)
        )
    )


def _bootstrap_job_resolution(
    context: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Discover one authorized job and preserve ambiguity as a hard failure."""

    parked = _parked_bootstrap_request(context)
    if parked is None:
        return None, "parked_bootstrap_request_invalid"
    root = job_root()
    try:
        if root.is_symlink():
            return None, "bootstrap_job_store_symlink_forbidden"
        if not root.exists():
            return None, None
        if not root.is_dir():
            return None, "bootstrap_job_store_invalid"
        directories = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None, "bootstrap_job_store_unavailable"
    matches: list[dict[str, Any]] = []
    invalid_related: list[str] = []
    for directory in directories:
        if _HEX64.fullmatch(directory.name) is None:
            continue
        if directory.is_symlink() or not directory.is_dir():
            invalid_related.append(directory.name)
            continue
        raw_request = _unvalidated_job_request(directory)
        loaded = _safe_job_request(directory.name)
        if loaded is None:
            # Once a 64-hex durable-job entry exists, malformed bytes cannot
            # prove that they are unrelated to the one-time bootstrap.  Treat
            # the unknown identity as an interrupted/tampered attempt instead
            # of offering a fresh non-force launch.
            invalid_related.append(directory.name)
            continue
        request, resolved_directory = loaded
        view = _bootstrap_request_view(
            context,
            parked,
            request,
            resolved_directory,
        )
        if view is not None:
            matches.append(view)
            if len(matches) > 1:
                # Ambiguity is itself an authority failure.  Never pick the
                # newest/oldest directory heuristically.
                return None, "multiple_authorized_bootstrap_jobs"
        elif _bootstrap_request_related(
            context,
            parked,
            raw_request if raw_request is not None else request,
        ):
            from bootstrap_contract_recovery import (
                is_finalized_historical_bootstrap_job,
            )

            if is_finalized_historical_bootstrap_job(
                PROJECT_ROOT,
                current_workflow_run_id=context["workflow_run_id"],
                job_directory=resolved_directory,
            ):
                # The exact old 0/8 job remains immutable operational evidence,
                # but its old workflow was canonically abandoned under a
                # content-bound contract-migration claim.  It is not an active
                # authorization candidate for this new workflow.
                continue
            invalid_related.append(str(request.get("job_id") or directory.name))
    if matches and invalid_related:
        return None, "authorized_bootstrap_job_identity_ambiguous"
    if invalid_related:
        return None, "authorized_bootstrap_job_validation_failed"
    return (matches[0], None) if len(matches) == 1 else (None, None)


def _bootstrap_job_view(context: dict[str, Any]) -> dict[str, Any] | None:
    """Compatibility wrapper returning only an unambiguous authorized job."""

    return _bootstrap_job_resolution(context)[0]


def _attached_job_view(context: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the attached manager request against checkpoint and artifact."""

    if _official_prerequisite_issues(context):
        return None
    checkpoint = context["checkpoint"]
    attached = checkpoint.get("official_job")
    if not isinstance(attached, dict) or attached.get("schema_version") != 1:
        return None
    job_id = str(attached.get("job_id") or "")
    loaded = _safe_job_request(job_id)
    if loaded is None:
        return None
    request, directory = loaded
    request_spec = request.get("spec")
    request_identity = request.get("identity")
    if not isinstance(request_spec, dict) or not isinstance(request_identity, dict):
        return None
    try:
        spec = _spec_from_mapping(request_spec)
    except Exception:
        return None
    # The ordinary HTTP API must never create or reinterpret the exceptional
    # first-v143 control authorization.  That path remains CLI + commit_bot.
    if (
        spec.mode != "full"
        or spec.policy_id != FULL_POLICY_ID
        or spec.bootstrap_control_id is not None
        or spec.self_play_rounds != _FORMAL_ROUNDS["self_play"]
        or spec.opponent_rounds != _FORMAL_ROUNDS["opponent"]
        or spec.target_hands != 70
        or request.get("source_v") != checkpoint.get("source_v")
    ):
        return None
    try:
        candidate = Path(spec.candidate).expanduser().resolve()
    except Exception:
        return None
    if candidate != context["candidate"]:
        return None

    expected_identity = {
        "identity_digest": attached.get("identity_digest"),
        "candidate_hash": attached.get("candidate_hash"),
        "opponent_hash": attached.get("opponent_hash"),
    }
    if any(
        not isinstance(value, str) or _HEX64.fullmatch(value) is None
        for value in expected_identity.values()
    ):
        return None
    if any(request_identity.get(key) != value for key, value in expected_identity.items()):
        return None
    if (
        request_identity.get("policy_id") != FULL_POLICY_ID
        or request_identity.get("spec") != request_spec
        or request_identity.get("runner_provenance") != "official-exe"
        or request_identity.get("authority_scope") != "production"
        or request_identity.get("test_only") is not False
    ):
        return None
    if expected_identity["candidate_hash"] != context["candidate_hash"]:
        return None
    if attached.get("policy_id") != FULL_POLICY_ID:
        return None

    selection = request.get("opponent_selection")
    selected_opponent = (
        selection.get("opponent")
        if isinstance(selection, dict)
        and isinstance(selection.get("opponent"), dict)
        else {}
    )
    try:
        selected_candidate = Path(str(selection.get("candidate") or "")).resolve()
        selected_opponent_path = Path(
            str(selected_opponent.get("path") or "")
        ).resolve()
        spec_opponent_path = Path(str(spec.opponent or "")).resolve()
    except Exception:
        return None
    if (
        not isinstance(selection, dict)
        or selection.get("selected") is not True
        or selection.get("bootstrap_control_id") is not None
        or selection.get("operator_bootstrap_authorization") is not None
        or selected_candidate != context["candidate"]
        or selected_opponent.get("eligible") is not True
        or selected_opponent_path != spec_opponent_path
        or selected_opponent.get("artifact_hash")
        != expected_identity["opponent_hash"]
    ):
        return None
    if _normal_opponent_authority_issues(
        selection,
        spec,
        request_identity,
    ):
        return None

    state = get_job(job_id)
    if not isinstance(state, dict):
        return None
    try:
        state_candidate = Path(str(state.get("candidate") or "")).resolve()
        state_directory = Path(str(state.get("job_dir") or "")).resolve()
    except Exception:
        return None
    if (
        state.get("schema_version") != request.get("schema_version")
        or state.get("manager_version") != request.get("manager_version")
        or state.get("job_id") != job_id
        or state.get("request_digest") != request.get("request_digest")
        or state.get("state") not in {
            "created",
            "queued",
            "starting",
            "running",
            "finalizing",
            "cancel_requested",
            "completed",
            "failed",
            "cancelled",
        }
        or state_candidate != context["candidate"]
        or state_directory != directory
        or not _formal_progress_valid(state)
    ):
        return None

    status = state.get("status")
    if state.get("state") == "completed":
        if (
            not isinstance(status, dict)
            or status.get("mode") != "full"
            or status.get("policy_id") != FULL_POLICY_ID
            or status.get("certification_identity") != request_identity
            or status.get("opponent_selection") != selection
        ):
            return None
        envelope = status.get("official_job_envelope")
        if (
            not isinstance(envelope, dict)
            or envelope.get("certification_identity_digest")
            != request_identity.get("identity_digest")
            or envelope.get("opponent_selection") != selection
            or job_envelope_issues(
                envelope,
                expected_job_id=job_id,
                expected_request_digest=str(request.get("request_digest") or ""),
                expected_attempt=int(state.get("attempt", 0) or 0),
                expected_candidate_hash=context["candidate_hash"],
                expected_opponent_hash=expected_identity["opponent_hash"],
            )
        ):
            return None
    elif status is not None:
        # The manager materializes result status only from a digest-bound
        # completed result file.  Any status embedded in running/failed state
        # JSON is stale or injected and must not become a public verdict.
        return None

    return {
        **state,
        **_authority_fields(
            context["projection"],
            workflow_run_id=context["workflow_run_id"],
            candidate_version=context["version"],
        ),
        "subject_kind": "active_candidate",
        "formal_authority": "pipeline_attached_full_v5_job",
        **_formal_job_profile(bootstrap=False),
        **_job_status_projection(state, context["candidate"]),
    }


def _current_attached_job() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return only the normal pipeline-attached job used by cancellation."""

    projection = _projection()
    context = _current_candidate_context(projection)
    if context is None:
        return None, None
    if context["checkpoint"].get("stage") != _OFFICIAL_STAGE:
        return None, context
    return _attached_job_view(context), context


def _current_visible_job() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the sole current read-authorized normal or bootstrap job."""

    projection = _projection()
    context = _current_candidate_context(projection)
    if context is None:
        return None, None
    stage = context["checkpoint"].get("stage")
    if stage in _NORMAL_JOB_STAGES:
        return _attached_job_view(context), context
    if stage == _BOOTSTRAP_STAGE:
        return _bootstrap_job_view(context), context
    return None, context


def _jobs_payload() -> dict[str, Any]:
    projection = _projection()
    context = _current_candidate_context(projection)
    job = None
    operator_transition = None
    if context is not None:
        stage = context["checkpoint"].get("stage")
        if stage in _NORMAL_JOB_STAGES:
            job = _attached_job_view(context)
        elif stage == _BOOTSTRAP_STAGE:
            job, discovery_issue = _bootstrap_job_resolution(context)
            operator_transition = _bootstrap_operator_transition(
                context,
                job,
                discovery_issue=discovery_issue,
            )
    workflow = context.get("workflow_run_id") if context is not None else None
    version = context.get("version") if context is not None else None
    checkpoint = context.get("checkpoint") if context is not None else None
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    rows = [job] if job is not None else []
    return {
        "schema_version": 1,
        **_authority_fields(
            projection,
            workflow_run_id=workflow,
            candidate_version=version,
        ),
        # Complete checkpoint identity for browser pairing.  Candidate version
        # plus workflow is insufficient: the same workflow/stage can advance a
        # revision while a formerly current job response is in flight.
        "next_v": version,
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "checkpoint_stage": checkpoint.get("stage"),
        "checkpoint_revision": checkpoint.get("checkpoint_revision"),
        "run_id": (
            context.get("run_id")
            or checkpoint.get("run_id")
            or (
                f"{version}#{int(checkpoint.get('generation_attempt') or 0)}"
                if type(version) is int
                else None
            )
        ) if context is not None else None,
        "pending": sum(1 for item in rows if item.get("pending") is True),
        "running": sum(
            1
            for item in rows
            if item.get("state") in {"starting", "running", "finalizing"}
        ),
        "jobs": rows,
        "operator_transition": operator_transition,
    }


def operator_transition_for_epoch_projection(
    projection: dict[str, Any],
) -> dict[str, Any] | None:
    """Refine the parked v143 transition for one already-sampled epoch.

    The control route uses this helper only after it has obtained a stable
    epoch/handoff sample.  ``_current_candidate_context`` then reopens the
    checkpoint and requires the exact version, parents, stage, run id,
    workflow id and revision before any durable job may refine the default
    ``bootstrap_required`` transition.  An identity race therefore returns
    ``None`` and leaves the epoch-owned fail-closed transition untouched.
    """

    if not isinstance(projection, dict):
        return None
    active = projection.get("active_generation")
    if (
        not isinstance(active, dict)
        or active.get("stage") != _BOOTSTRAP_STAGE
        or active.get("next_v") != FIRST_STRICT_POLICY_VERSION
    ):
        return None
    context = _current_candidate_context(projection)
    if context is None:
        return None
    job, discovery_issue = _bootstrap_job_resolution(context)
    transition = _bootstrap_operator_transition(
        context,
        job,
        discovery_issue=discovery_issue,
    )
    return transition if isinstance(transition, dict) else None


def _cancel_exact_job_sync(
    job_id: str,
    *,
    workflow_run_id: str,
    candidate_version: int,
    checkpoint_revision: int,
) -> dict[str, Any]:
    """Cancel while holding the checkpoint CAS lock for exact ownership."""

    import evolution_infra as infra

    with infra._locked_state_sidecar(
        infra.PIPELINE_STATE_FILE,
        lock_type=fcntl.LOCK_EX,
    ):
        projection = _projection()
        active = projection.get("active_generation")
        if (
            projection.get("initialized") is not True
            or projection.get("reset_receipt_valid") is not True
            or not isinstance(active, dict)
            or active.get("next_v") != candidate_version
            or active.get("workflow_run_id") != workflow_run_id
            or active.get("stage") != _OFFICIAL_STAGE
        ):
            return {"state": "identity_mismatch", "job_id": job_id}
        checkpoint = infra.read_pipeline_checkpoint()
        try:
            identity = strict_checkpoint_event_identity(
                checkpoint,
                expected_gen=candidate_version,
                project_root=PROJECT_ROOT,
            )
        except Exception:
            return {"state": "identity_mismatch", "job_id": job_id}
        attached = (
            checkpoint.get("official_job")
            if isinstance(checkpoint, dict)
            and isinstance(checkpoint.get("official_job"), dict)
            else {}
        )
        if (
            checkpoint.get("stage") != _OFFICIAL_STAGE
            or checkpoint.get("workflow_run_id") != workflow_run_id
            or identity.get("workflow_run_id") != workflow_run_id
            or checkpoint.get("checkpoint_revision") != checkpoint_revision
            or attached.get("job_id") != job_id
        ):
            return {"state": "identity_mismatch", "job_id": job_id}
        try:
            live_candidate_hash = hash_path(
                BOTS_DIR / bot_name(candidate_version)
            )
        except Exception:
            live_candidate_hash = ""
        if (
            _HEX64.fullmatch(live_candidate_hash) is None
            or attached.get("candidate_hash") != live_candidate_hash
        ):
            return {"state": "identity_mismatch", "job_id": job_id}
        return cancel_job(job_id, reason="api_operator_cancelled")


@router.get("/jobs")
async def get_jobs():
    return await run_blocking_isolated(
        _jobs_payload,
        thread_name_prefix="official-certification-jobs",
    )


def _certification_job_payload(job_id: str) -> dict[str, Any]:
    job, _context = _current_visible_job()
    if job is None or job.get("job_id") != job_id:
        raise HTTPException(status_code=404, detail="Official certification job not found")
    return job


@router.get("/jobs/{job_id}")
async def get_certification_job(job_id: str):
    return await run_blocking_isolated(
        _certification_job_payload,
        job_id,
        thread_name_prefix="official-certification-job",
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel_certification_job(job_id: str, request: Request):
    require_operator_mutation(request, operation="certification_cancel")
    job, context = _current_attached_job()
    if job is None or context is None or job.get("job_id") != job_id:
        raise HTTPException(status_code=404, detail="Official certification job not found")
    checkpoint = context["checkpoint"]
    payload = await run_blocking_isolated(
        _cancel_exact_job_sync,
        job_id,
        workflow_run_id=context["workflow_run_id"],
        candidate_version=context["version"],
        checkpoint_revision=int(checkpoint.get("checkpoint_revision", 0) or 0),
        thread_name_prefix="official-api",
    )
    if payload.get("state") in {"missing", "identity_mismatch"}:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "official_job_identity_changed",
                "evaluation_epoch": EVALUATION_EPOCH,
                "workflow_run_id": context["workflow_run_id"],
                "job_id": job_id,
            },
        )
    return {
        **payload,
        **_authority_fields(
            context["projection"],
            workflow_run_id=context["workflow_run_id"],
            candidate_version=context["version"],
        ),
    }


def _certification_payload(version: int) -> dict[str, Any]:
    candidate, subject_kind, projection, workflow_run_id = _visible_subject(version)
    raw_payload = status_payload(candidate)
    is_full_record = (
        raw_payload.get("mode") == "full"
        and raw_payload.get("policy_id") == FULL_POLICY_ID
    )
    formal = False
    diagnostic = None
    if not is_full_record and raw_payload.get("status") not in {
        None,
        "official-uncertified",
    }:
        diagnostic = {
            "status": raw_payload.get("status"),
            "mode": raw_payload.get("mode"),
            "policy_id": raw_payload.get("policy_id"),
            "authority": "diagnostic_only",
        }
    payload = dict(raw_payload)
    # Profile-looking mutable status fields have no authority.  Re-add them
    # below only from the validated signed certificate projection.
    for key in (
        "certification_profile",
        "opponent_authority",
        "strength_evidence_weight",
        "strategy_evidence_weight",
        "formal_summary",
    ):
        payload.pop(key, None)
    if not is_full_record:
        # Existing clients historically rendered `status` without checking the
        # mode.  Keep diagnostic evidence separately and make the primary
        # certification projection unambiguously full-v5-only.
        payload.update({
            "status": "official-uncertified",
            "mode": "full",
            "policy_id": FULL_POLICY_ID,
            "compliance_verdict": {
                "ok": False,
                "classification": "full_v5_not_run",
                "blocking": False,
                "inconclusive": True,
            },
        })
    profile: dict[str, Any] = {}
    if is_full_record:
        try:
            profile = official_certification_profile_projection(
                raw_payload,
                candidate,
                require_published=subject_kind == "strict_published",
            )
        except Exception:
            profile = {}
        formal = bool(profile)
    return {
        **payload,
        **_authority_fields(
            projection,
            workflow_run_id=workflow_run_id,
            candidate_version=(version if subject_kind == "active_candidate" else None),
        ),
        "subject_kind": subject_kind,
        "formal_certified": formal,
        "formal_authority": "signed_full_v5" if formal else "none",
        "diagnostic_evidence": diagnostic,
        **profile,
    }


@router.get("/{version:int}")
async def get_certification(version: int):
    return await run_blocking_isolated(
        _certification_payload,
        version,
        thread_name_prefix="official-certification-status",
    )


@router.post("/{version:int}/enqueue", status_code=410)
async def enqueue_retired(version: int, request: Request):
    """Retire the unsafe arbitrary-path/mode certification launcher.

    Normal full-v5 work is started only by ``commit_bot`` after the immutable
    local gate ledger passes.  The first v143 exception is started only by the
    explicit acknowledged ``bootstrap-first-strict`` operator CLI.
    """

    require_operator_mutation(request, operation="certification_enqueue")
    return {
        "retired": True,
        "code": "certification_http_enqueue_retired",
        "version": version,
        "evaluation_epoch": EVALUATION_EPOCH,
        "formal_mode": "full",
        "formal_policy_id": FULL_POLICY_ID,
        "normal_entrypoint": "commit_bot",
        "first_strict_entrypoint": "scripts/official_certify.py bootstrap-first-strict",
        "message": (
            "HTTP enqueue is retired. Use the pipeline commit_bot full-v5 gate; "
            "v143 requires the explicit acknowledged bootstrap-first-strict CLI."
        ),
    }
