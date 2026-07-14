"""Pipeline tools: commit, archivist, and crossover."""

import json
import os
import stat
import time
import uuid
from pathlib import Path
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
    ensure_bot_git_publication,
    verify_remote_bot_publication,
    remote_completion_ref_snapshot,
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
    _review_gate_ok,
    _critic_gate_ok,
)
from system_log import log_system_event
from pipeline_infrastructure import infrastructure_failure_digest
from blocking_runtime import run_blocking_isolated
from llm_availability import LLMAvailabilityBlocked

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


def _official_certificate_projection(status):
    identity = (
        status.get("certification_identity")
        if isinstance((status or {}).get("certification_identity"), dict)
        else {}
    )
    return {
        "certificate_digest": (status or {}).get("certificate_digest"),
        "candidate_hash": identity.get("candidate_hash"),
        "policy_id": (status or {}).get("policy_id"),
        "certificate_path": (status or {}).get("certificate_path"),
        "certification_identity": identity,
    }


def _write_completed_sentinel_durable(bot_dir, publication_id):
    """Durably materialize the local cache only after publication proof."""

    root = os.fspath(Path(bot_dir))
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError("completed sentinel parent is not a regular bot directory")

    expected = f"publication_id={publication_id}\n"
    sentinel = os.path.join(root, ".completed")

    def validate_existing():
        try:
            metadata = os.lstat(sentinel)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(
                "existing completed sentinel is not a regular non-symlink file"
            )
        try:
            with open(sentinel, "r", encoding="utf-8") as handle:
                observed = handle.read()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                "existing completed sentinel is unreadable"
            ) from exc
        if observed != expected:
            raise RuntimeError(
                "existing completed sentinel belongs to a different publication"
            )
        return True

    if validate_existing():
        return True

    temporary = os.path.join(
        root,
        f".completed.{os.getpid()}.{uuid.uuid4().hex}.tmp",
    )
    linked = False
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Create-only publication: unlike os.replace(), link cannot clobber
            # a sentinel raced in by another process.
            os.link(temporary, sentinel, follow_symlinks=False)
            linked = True
        except FileExistsError:
            validate_existing()
        os.unlink(temporary)
        temporary = ""
        descriptor = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if linked and not validate_existing():
            raise RuntimeError("completed sentinel disappeared after publication")
        return True
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _publication_pending_result(v, source_v, *, error, **extra):
    return {
        "error": error,
        "version": int(v),
        "source_v": int(source_v),
        "committed": False,
        "checkpoint_preserved": True,
        **extra,
    }


def _revalidate_publication_authority_before_push(
    v,
    source_v,
    *,
    intent,
    bot_dir,
):
    """Re-open the latest durable authority at the remote linearization point."""

    from national_runtime_authority import (
        build_pending_local_publication_proof,
        strict_published_bot_names,
    )
    from official_certification import official_full_certified
    from publication_transaction import (
        publication_gate_ledger_digest,
        publication_intent_checkpoint_errors,
        publication_intent_live_errors,
    )

    current = read_pipeline_checkpoint()
    checkpoint_errors = publication_intent_checkpoint_errors(intent, current)
    if checkpoint_errors:
        raise RuntimeError(
            "pre-push publishing checkpoint changed: "
            + "; ".join(checkpoint_errors[:30])
        )
    official_status = (
        (((current or {}).get("gate_results") or {}).get("official_full") or {})
        .get("status")
        or {}
    )
    proof = build_pending_local_publication_proof(bot_dir)
    ledger = validate_commit_gate_ledger(
        v,
        source_v,
        current,
        bot_dir=bot_dir,
        pending_local_publication=proof,
    )
    errors = []
    if ledger.get("missing_gates") or ledger.get("failed_gates"):
        errors.append("pre_push_gate_ledger_invalid")
    errors.extend(
        publication_intent_live_errors(
            intent,
            checkpoint=current,
            candidate_dir=bot_dir,
            repo_root=PROJECT_ROOT,
            official_status=official_status,
            final_gate_ledger_digest=publication_gate_ledger_digest(ledger),
            current_strict_bots=strict_published_bot_names(),
            current_remote_required=evolution_git_push_required(),
        )
    )
    tag_matches, tag_mismatch = _existing_local_bot_tag_matches_certificate(
        v,
        _official_certificate_projection(official_status),
    )
    if not tag_matches:
        errors.append(
            "pre_push_completion_identity_invalid:"
            + (tag_mismatch or "unknown")
        )
    if not official_full_certified(
        official_status,
        bot_dir,
        require_published=True,
    ):
        errors.append("pre_push_official_certificate_identity_invalid")
    errors = list(dict.fromkeys(errors))
    if errors:
        raise RuntimeError(
            "pre-push publication authority changed: "
            + "; ".join(errors[:30])
        )
    return {
        "proof": proof,
        "ledger": ledger,
        "checkpoint_revision": current.get("checkpoint_revision"),
    }


def _resume_publication_transaction(v, source_v, ckpt):
    """Reconcile one immutable publication intent from observed effects."""

    from national_runtime_authority import (
        build_pending_local_publication_proof,
        strict_published_bot_names,
    )
    from official_certification import official_full_certified
    from publication_transaction import (
        publication_gate_ledger_digest,
        publication_intent_live_errors,
    )

    bot_dir = get_bot_dir(v)
    intent = (ckpt or {}).get("publication_intent")
    official_status = (
        (((ckpt or {}).get("gate_results") or {}).get("official_full") or {})
        .get("status")
        or {}
    )
    official_certificate = _official_certificate_projection(official_status)
    pending_proof = None
    if git_has_tag(v):
        tag_matches, mismatch = _existing_local_bot_tag_matches_certificate(
            v, official_certificate
        )
        if not tag_matches:
            return _publication_pending_result(
                v,
                source_v,
                error=(
                    "COMMIT BLOCKED: existing completion tag does not match "
                    "the frozen publication intent."
                ),
                reason=mismatch,
            )
        try:
            pending_proof = build_pending_local_publication_proof(bot_dir)
        except Exception as exc:
            return _publication_pending_result(
                v,
                source_v,
                error="COMMIT BLOCKED: local publication proof is invalid.",
                reason=f"{type(exc).__name__}: {str(exc)[:300]}",
            )

    remote_required = bool((intent or {}).get("remote_publication_required"))
    remote_attempted = bool(
        remote_required
        or (intent or {}).get("remote_publication_enabled")
    )
    linearized_remote_proof = None
    if pending_proof is not None and remote_attempted:
        candidate_remote_proof = verify_remote_bot_publication(intent)
        if candidate_remote_proof.get("valid") is True:
            linearized_remote_proof = candidate_remote_proof

    if linearized_remote_proof is None:
        ledger = validate_commit_gate_ledger(
            v,
            source_v,
            ckpt,
            bot_dir=bot_dir,
            pending_local_publication=pending_proof,
        )
        ledger_digest = publication_gate_ledger_digest(ledger)
        if ledger.get("missing_gates") or ledger.get("failed_gates"):
            return _publication_pending_result(
                v,
                source_v,
                error="COMMIT BLOCKED: frozen publication gate ledger no longer validates.",
                missing_gates=ledger.get("missing_gates"),
                failed_gates=ledger.get("failed_gates"),
            )
        current_strict_bots = strict_published_bot_names()
    else:
        # The remote transaction is already irreversible.  Revalidate only
        # frozen bytes/checkpoint/status; later strict publications must not
        # self-lock this transaction's sentinel/CAS recovery.
        ledger_digest = str((intent or {}).get("final_gate_ledger_digest") or "")
        current_strict_bots = [
            *(intent.get("prepublication_strict_bots") or []),
            str(intent.get("bot") or ""),
        ]
    live_remote_required = (
        bool((intent or {}).get("remote_publication_required"))
        if linearized_remote_proof is not None
        else evolution_git_push_required()
    )
    try:
        live_errors = publication_intent_live_errors(
            intent,
            checkpoint=ckpt,
            candidate_dir=bot_dir,
            repo_root=PROJECT_ROOT,
            official_status=official_status,
            final_gate_ledger_digest=ledger_digest,
            current_strict_bots=current_strict_bots,
            current_remote_required=live_remote_required,
        )
    except Exception as exc:
        live_errors = [
            f"publication_intent_live_validation_error:{type(exc).__name__}:"
            f"{str(exc)[:240]}"
        ]
    if live_errors:
        return _publication_pending_result(
            v,
            source_v,
            error="COMMIT BLOCKED: publication intent or its live inputs drifted.",
            validation_errors=live_errors[:30],
        )
    if not official_full_certified(official_status, bot_dir):
        return _publication_pending_result(
            v,
            source_v,
            error="COMMIT BLOCKED: frozen official full certificate is no longer valid.",
        )

    def pre_push_authority():
        return _revalidate_publication_authority_before_push(
            v,
            source_v,
            intent=intent,
            bot_dir=bot_dir,
        )

    try:
        local_state = ensure_bot_git_publication(
            intent,
            official_certificate=official_certificate,
            pre_push_authority=pre_push_authority,
        )
    except Exception as exc:
        return _publication_pending_result(
            v,
            source_v,
            error="COMMIT PENDING: local Git publication did not converge.",
            reason=f"{type(exc).__name__}: {str(exc)[:500]}",
            local_committed=bool(git_has_tag(v)),
        )

    if remote_required:
        remote_proof = (
            linearized_remote_proof
            or verify_remote_bot_publication(intent, local_state=local_state)
        )
    elif linearized_remote_proof is not None:
        remote_proof = linearized_remote_proof
    else:
        remote_proof = {
            "valid": True,
            "local_only": True,
            "publication_id": intent.get("publication_id"),
        }
    if remote_required and remote_proof.get("valid") is not True:
        return _publication_pending_result(
            v,
            source_v,
            error="COMMIT PENDING: required origin publication is not proven.",
            local_committed=True,
            push_ok=bool(local_state.get("push_ok")),
            remote_proof=remote_proof,
            completed_sentinel_written=False,
        )

    # From this point onward, only the frozen local/remote publication proof is
    # re-opened.  Dynamic strict-pool authority was checked before the atomic
    # push and may legitimately change after this transaction linearizes.
    try:
        frozen_local_proof = build_pending_local_publication_proof(bot_dir)
    except Exception as exc:
        return _publication_pending_result(
            v,
            source_v,
            error="COMMIT BLOCKED: frozen local publication proof is invalid.",
            reason=f"{type(exc).__name__}: {str(exc)[:300]}",
            remote_proof=remote_proof,
        )
    if not official_full_certified(
        official_status,
        bot_dir,
        require_published=True,
    ):
        return _publication_pending_result(
            v,
            source_v,
            error="COMMIT BLOCKED: committed certificate/tag attestation is invalid.",
            local_publication_proof=frozen_local_proof,
            remote_proof=remote_proof,
        )

    _write_completed_sentinel_durable(bot_dir, intent.get("publication_id"))
    current = read_pipeline_checkpoint()
    from publication_transaction import publication_intent_checkpoint_errors

    current_errors = publication_intent_checkpoint_errors(intent, current)
    if current_errors:
        return _publication_pending_result(
            v,
            source_v,
            error="COMMIT PENDING: checkpoint changed before publication completion.",
            completed_sentinel_written=True,
            validation_errors=current_errors,
            remote_proof=remote_proof,
        )
    cleared = clear_pipeline_checkpoint(
        expected_workflow_run_id=current.get("workflow_run_id"),
        expected_next_v=int(v),
        expected_source_v=int(source_v),
        expected_checkpoint_revision=current.get("checkpoint_revision"),
        expected_checkpoint_stage="publishing",
    )
    if not cleared:
        return _publication_pending_result(
            v,
            source_v,
            error="COMMIT PENDING: publication completed but checkpoint CAS did not clear.",
            completed_sentinel_written=True,
            remote_proof=remote_proof,
        )
    return {
        "committed": True,
        "version": int(v),
        "source_v": int(source_v),
        "publication_id": intent.get("publication_id"),
        "commit_oid": local_state.get("commit_oid"),
        "push_ok": bool(local_state.get("push_ok")),
        "remote_proof": remote_proof,
        "completed_sentinel_written": True,
        "checkpoint_cleared": True,
    }


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


def validate_commit_gate_ledger(
    v,
    source_v,
    ckpt,
    bot_dir=None,
    *,
    pending_local_publication=None,
):
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
            expected_execution_mode = getattr(workflow_profile, "national_execution_mode", "")
            expected_evaluation_protocol = getattr(workflow_profile, "evaluation_protocol", "")
        except Exception as exc:
            expected_profile_id = ""
            expected_execution_mode = ""
            expected_evaluation_protocol = ""
            failed_gates.append({
                "gate": "workflow_profile",
                "reason": "active workflow profile is unavailable or invalid",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            })
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
                    quality_probe = (
                        (quality.get("national_capability_contract") or {}).get(
                            "dynamic_runtime_probe"
                        )
                        or {}
                    )
                    managed_isolation_digest = str(
                        quality_probe.get("managed_isolation_digest") or ""
                    )
                    if len(managed_isolation_digest) != 64:
                        failed_gates.append({
                            "gate": "runtime_probe_identity",
                            "reason": "managed isolation digest missing or invalid",
                        })
                    expected_runtime_identity[
                        "runtime_probe_managed_isolation_digest"
                    ] = managed_isolation_digest
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
        elif not _review_gate_ok(ckpt):
            failed_gates.append({
                "gate": "review",
                "reason": (
                    "reviewer was not schema-valid/content-bound or did not approve"
                ),
                "value": review,
            })

        critic = gate_results.get("critic")
        if not critic:
            missing_gates.append("critic")
        elif not _critic_gate_ok(ckpt):
            failed_gates.append({
                "gate": "critic",
                "reason": (
                    "critic advisory role was not schema-valid/content-bound or "
                    "did not complete successfully"
                ),
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

                    # The one-time empty-pool control is not a published bot and
                    # carries no strength/rating authority.  Recompute its full
                    # live authority at the final ledger boundary, bind it back
                    # to the exact quality receipt, and independently reapply
                    # the complete-match W/L/D floor.  A concurrently published
                    # strict bot therefore revokes this path before commit.
                    from system_strict_bootstrap import is_declared_native_bootstrap

                    declared_first_strict = is_declared_native_bootstrap(ckpt)
                    plan_opponents = (
                        precommit_plan.get("opponents") or []
                        if isinstance(precommit_plan, dict)
                        else []
                    )
                    control_opponents = [
                        item for item in plan_opponents
                        if isinstance(item, dict)
                        and item.get("authority") == "system_first_strict_control"
                    ]
                    if declared_first_strict:
                        from first_strict_control import (
                            control_gate_blockers,
                            validate_control_receipt,
                            validate_control_result,
                        )

                        control_errors = []
                        if len(control_opponents) != 1 or len(plan_opponents) != 1:
                            control_errors.append(
                                "first_strict_control_final_plan_shape_invalid"
                            )
                        quality_receipt = quality.get(
                            "first_strict_control_receipt"
                        )
                        plan_receipt = (
                            control_opponents[0].get("control_receipt")
                            if len(control_opponents) == 1
                            else None
                        )
                        if quality_receipt != plan_receipt:
                            control_errors.append(
                                "first_strict_control_quality_plan_receipt_mismatch"
                            )
                        control_errors.extend(validate_control_receipt(
                            plan_receipt,
                            checkpoint=ckpt,
                            candidate_version=v,
                            source_version=source_v,
                            force_protocol_refresh=True,
                            pending_local_publication=pending_local_publication,
                        ))
                        if precommit.get("precommit_eval_plan") != precommit_plan:
                            control_errors.append(
                                "first_strict_control_result_plan_mismatch"
                            )
                        expected_flags = {
                            "precommit_gate_admitted": True,
                            "strength_admitted": False,
                            "rating_eligible": False,
                            "official_opponent_eligible": False,
                        }
                        for field, expected in expected_flags.items():
                            if precommit.get(field) is not expected:
                                control_errors.append(
                                    f"first_strict_control_final_{field}_mismatch"
                                )
                        strength_order = precommit.get("strength_order") or {}
                        if int(strength_order.get("samples") or 0) != 0:
                            control_errors.append(
                                "first_strict_control_strength_samples_nonzero"
                            )
                        expected_control_samples = list(
                            (precommit_plan or {}).get("sample_plan") or []
                        )
                        execution_scope = precommit.get(
                            "control_execution_scope"
                        )
                        national_execution_scope = (
                            (precommit.get("national") or {}).get(
                                "control_execution_scope"
                            )
                        )
                        if execution_scope != national_execution_scope:
                            control_errors.append(
                                "first_strict_control_execution_scope_projection_mismatch"
                            )
                        expected_execution_bindings = {
                            "workflow_run_id": str(
                                ckpt.get("workflow_run_id") or ""
                            ),
                            "candidate_version": int(v),
                            "candidate_label": bot_name(v),
                            "candidate_artifact_hash": str(
                                current_code_fingerprint
                            ),
                            "control_id": "first_strict_control_v1",
                            "control_artifact_hash": str(
                                (((plan_receipt or {}).get("control") or {}).get(
                                    "artifact_hash"
                                ))
                                or ""
                            ),
                            "control_receipt_digest": str(
                                (plan_receipt or {}).get("receipt_digest") or ""
                            ),
                            "precommit_plan_digest": str(
                                (precommit_plan or {}).get("plan_digest") or ""
                            ),
                            "evaluation_contract_digest": str(
                                (precommit.get("precommit_eval_contract") or {}).get(
                                    "contract_digest"
                                )
                                or ""
                            ),
                            "precommit_attempt": int(
                                ckpt.get("precommit_attempt") or 0
                            ),
                        }
                        if not isinstance(execution_scope, dict):
                            control_errors.append(
                                "first_strict_control_execution_scope_missing"
                            )
                        else:
                            for field, expected in expected_execution_bindings.items():
                                if execution_scope.get(field) != expected:
                                    control_errors.append(
                                        "first_strict_control_execution_scope_"
                                        f"{field}_mismatch"
                                    )
                        result_errors, recomputed_control_gate = (
                            validate_control_result(
                                precommit,
                                expected_sample_plan=expected_control_samples,
                                expected_execution_scope=execution_scope,
                            )
                        )
                        control_errors.extend(result_errors)
                        control_blockers, _ = control_gate_blockers(
                            precommit,
                            expected_sample_plan=expected_control_samples,
                            expected_execution_scope=execution_scope,
                        )
                        if control_blockers:
                            control_errors.extend(
                                str(item.get("reason") or "control_gate_failed")
                                for item in control_blockers
                            )
                        if precommit.get(
                            "first_strict_control_gate"
                        ) != recomputed_control_gate:
                            control_errors.append(
                                "first_strict_control_gate_summary_mismatch"
                            )
                        if control_errors:
                            failed_gates.append({
                                "gate": "first_strict_control_final_ledger",
                                "reason": (
                                    "system control authority/content/floor is "
                                    "invalid or the strict pool changed"
                                ),
                                "errors": list(dict.fromkeys(control_errors))[:20],
                            })
                    elif control_opponents:
                        failed_gates.append({
                            "gate": "first_strict_control_final_ledger",
                            "reason": (
                                "system control appeared outside the declared "
                                "one-time empty-pool migration"
                            ),
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
        get_bot_dir(v),
        ckpt or {},
        candidate_hash=candidate_hash,
    )
    if parked.get("valid") is not True:
        log_system_event(
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
    return bool(write_pipeline_checkpoint(
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
    return bool(write_pipeline_checkpoint(
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
        get_active_bots(),
        preferred=_official_preferred_opponent(),
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
    if (active_ckpt or {}).get("stage") == "publishing":
        _set_pipeline_status(f"Recovering publication v{v}")
        return _json_tool_result(
            _resume_publication_transaction(v, source_v, active_ckpt)
        )
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
    from candidate_hygiene import (
        cleanup_transient_candidate_artifacts,
        forbidden_runtime_dependency_errors,
        transient_control_artifact_errors,
    )

    transient_artifact_errors = transient_control_artifact_errors(bot_dir)
    if transient_artifact_errors:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: transient control artifacts remain in candidate.",
            "version": v,
            "source_v": source_v,
            "validation_errors": transient_artifact_errors[:20],
            "directive": (
                "Do not certify or publish .task_context. Re-run the successful "
                "Worker cleanup path or abandon the drifted generation."
            ),
        })
    try:
        cleanup_transient_candidate_artifacts(
            bot_dir,
            include_task_context=False,
        )
    except Exception as exc:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: transient artifact cleanup failed.",
            "version": v,
            "source_v": source_v,
            "message": f"{type(exc).__name__}: {str(exc)[:300]}",
        })
    runtime_dependency_errors = forbidden_runtime_dependency_errors(bot_dir)
    if runtime_dependency_errors:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: bot references unpublished cache/control artifacts.",
            "version": v,
            "source_v": source_v,
            "validation_errors": runtime_dependency_errors[:20],
        })
    from bot_artifact import publication_shape_errors

    publication_errors = publication_shape_errors(bot_dir)
    if publication_errors:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: candidate artifact cannot be reproduced by Git.",
            "version": v,
            "source_v": source_v,
            "validation_errors": publication_errors[:20],
        })
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
    if official_full_gate.get("outcome") == "operator_bootstrap_required":
        if not _record_official_bootstrap_required_checkpoint(
            v,
            source_v,
            ckpt,
            official_full_gate,
            candidate_hash=str(ledger.get("current_code_fingerprint") or ""),
        ):
            return _json_tool_result({
                "error": "COMMIT BLOCKED: failed to park candidate for operator bootstrap.",
                "failure_class": "infrastructure",
                "version": v,
                "source_v": source_v,
                "checkpoint_stage": (ckpt or {}).get("stage"),
                "official_full_gate": official_full_gate,
            })
        try:
            log_system_event(
                "pipeline.official_bootstrap_required",
                "warn",
                f"v{v} is parked for the explicit one-time official bootstrap",
                {
                    "version": v,
                    "source_v": source_v,
                    "checkpoint_stage": "official_bootstrap_required",
                    "opponent_selection": official_full_gate.get("opponent_selection"),
                    "operator_action": "run_explicit_first_strict_bootstrap",
                    "automatic_bootstrap_forbidden": True,
                },
            )
        except Exception:
            pass
        return _json_tool_result({
            "paused": True,
            "committed": False,
            "outcome": "operator_bootstrap_required",
            "operator_action_required": True,
            "action": "run_explicit_first_strict_bootstrap",
            "automatic_bootstrap_forbidden": True,
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": "official_bootstrap_required",
            "official_full_gate": official_full_gate,
            "directive": (
                "Stop the orchestrator. Run scripts/official_certify.py "
                "bootstrap-first-strict with the current system control and explicit "
                "one-time acknowledgement. After it succeeds, call commit_bot manually."
            ),
        })
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
    if official_full_gate.get("outcome") == "completed_authorization_failure":
        log_system_event(
            "pipeline.official_bootstrap_completed_authorization_failed",
            "error",
            f"Completed bootstrap authorization drift blocked v{v} publication",
            {
                "version": v,
                "source_v": source_v,
                "issues": (official_full_gate.get("issues") or [])[:20],
                "checkpoint_stage": (ckpt or {}).get("stage"),
            },
        )
        return _json_tool_result({
            "error": official_full_gate.get("error"),
            "failure_class": "authorization",
            "checkpoint_preserved": True,
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": (ckpt or {}).get("stage"),
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

    # Diversity evidence is owned by generation-frozen native TCP snapshots.
    # Publication never reopens an auxiliary live behavior archive.
    novelty_info = {}

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
        certificate_spec = (
            identity.get("spec") if isinstance(identity.get("spec"), dict) else {}
        )
        if certificate_spec.get("bootstrap_control_id"):
            from official_bootstrap import (
                validate_completed_operator_bootstrap_authorization,
            )

            completed_rebind = validate_completed_operator_bootstrap_authorization(
                official_certification_status,
                bot_dir,
                checkpoint=ckpt,
            )
            if completed_rebind.get("valid") is not True:
                return _json_tool_result({
                    "error": (
                        "COMMIT BLOCKED: bootstrap authorization drifted immediately "
                        "before Git publication."
                    ),
                    "failure_class": "authorization",
                    "checkpoint_preserved": True,
                    "version": v,
                    "source_v": source_v,
                    "validation": completed_rebind,
                })
        official_certificate = _official_certificate_projection(
            official_certification_status
        )

    # Official certification may take minutes.  Re-open the checkpoint and
    # force the complete gate ledger (including the empty strict-pool receipt)
    # through its live validators after certification and immediately before
    # any Git tag/commit/push action.  A strict publication that appeared while
    # the EXE job was running therefore revokes the one-time control authority.
    ckpt = read_pipeline_checkpoint() or ckpt
    final_ledger = validate_commit_gate_ledger(
        v,
        source_v,
        ckpt,
        bot_dir=bot_dir,
    )
    if final_ledger["missing_gates"] or final_ledger["failed_gates"]:
        return _json_tool_result({
            "error": (
                "COMMIT BLOCKED: final gate ledger drifted after official "
                "certification."
            ),
            "version": v,
            "source_v": source_v,
            "checkpoint_stage": final_ledger["checkpoint_stage"],
            "missing_gates": final_ledger["missing_gates"],
            "failed_gates": final_ledger["failed_gates"],
            "gate_results": final_ledger["gate_results"],
        })

    # Linearize the publication before the first Git mutation.  A pre-existing
    # tracked candidate/tag without this durable intent is ambiguous legacy
    # debris and must never be adopted implicitly.
    if git_has_tag(v) or git_dir_is_committed(v):
        return _json_tool_result({
            "error": (
                "COMMIT BLOCKED: candidate already has Git publication effects "
                "but no matching durable publication intent."
            ),
            "version": v,
            "source_v": source_v,
            "checkpoint_preserved": True,
        })
    if not official_certificate:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: official certificate projection is missing.",
            "version": v,
            "source_v": source_v,
        })
    try:
        from national_runtime_authority import strict_published_bot_names
        from official_certification import publish_certificate_attestation
        from publication_transaction import (
            build_publication_intent,
            file_sha256,
            publication_gate_ledger_digest,
        )

        certificate_publication = publish_certificate_attestation(
            official_certification_status,
            bot_dir,
        )
        certificate_relative_path = str(
            certificate_publication.get("relative_path") or ""
        )
        certificate_path = PROJECT_ROOT / certificate_relative_path
        publication_intent = build_publication_intent(
            checkpoint=ckpt,
            candidate_artifact_hash=str(
                official_certificate.get("candidate_hash") or ""
            ),
            certificate_digest=str(
                official_certificate.get("certificate_digest") or ""
            ),
            certificate_policy_id=str(
                official_certificate.get("policy_id") or ""
            ),
            official_status=official_certification_status,
            certificate_relative_path=certificate_relative_path,
            certificate_file_sha256=file_sha256(certificate_path),
            certificate_attestation_digest=str(
                certificate_publication.get("attestation_digest") or ""
            ),
            final_gate_ledger_digest=publication_gate_ledger_digest(final_ledger),
            strategy_tag=strategy,
            rating_info=rating_info,
            baseline_head=_git("rev-parse", "refs/heads/main").strip(),
            baseline_remote_main=_git(
                "rev-parse", "refs/remotes/origin/main", check=False
            ).strip(),
            baseline_remote_completion_refs=(
                remote_completion_ref_snapshot()
                if (
                    evolution_git_push_required()
                    or evolution_git_push_enabled()
                )
                else {}
            ),
            prepublication_strict_bots=strict_published_bot_names(),
            remote_publication_required=evolution_git_push_required(),
            remote_publication_enabled=evolution_git_push_enabled(),
        )
    except Exception as exc:
        return _json_tool_result({
            "error": "COMMIT BLOCKED: publication intent could not be built.",
            "version": v,
            "source_v": source_v,
            "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
        })
    if not write_pipeline_checkpoint(
        v,
        source_v,
        "publishing",
        publication_intent=publication_intent,
        expected_checkpoint_revision=(ckpt or {}).get("checkpoint_revision"),
        expected_checkpoint_stage=(ckpt or {}).get("stage"),
        expected_workflow_run_id=(ckpt or {}).get("workflow_run_id"),
    ):
        return _json_tool_result({
            "error": "COMMIT BLOCKED: publication intent checkpoint CAS failed.",
            "version": v,
            "source_v": source_v,
            "checkpoint_preserved": True,
        })
    publishing_ckpt = read_pipeline_checkpoint()
    publication_result = _resume_publication_transaction(
        v,
        source_v,
        publishing_ckpt,
    )
    if publication_result.get("committed") is not True:
        return _json_tool_result(publication_result)
    push_ok = bool(publication_result.get("push_ok"))

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

    # The publication transaction already cleared the exact publishing
    # checkpoint with workflow/revision/stage CAS before advisory post-commit
    # work begins.  Never perform an unconditional second clear here.

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


def _archive_housekeeping_commit(
    version: int,
    reap_result: dict | None,
    preexisting_dirty: set[str],
) -> dict:
    """Commit archivist/reap tracked-file side effects so the worktree stays clean.

    commit_bot owns the bot commit and tag. run_archivist can still create tracked
    housekeeping changes after that point: tracked bot deletions from auto-reap.
    Those must be explicit, path-scoped commits
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


@tool("run_archivist", "Run the one-shot post-commit consistency/archive audit. Advisory notes stay in the content-bound archive snapshot and never enter prompt evidence directly.", {"version": int, "source_v": int})
async def run_archivist(args):
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)

    from evolution_infra import consume_post_commit_archivist_receipt

    receipt_ok, receipt_error, receipt = consume_post_commit_archivist_receipt(
        v,
        source_v,
    )
    if not receipt_ok:
        return _json_tool_result({
            "error": "POST_COMMIT_ARCHIVIST_RECEIPT_REQUIRED",
            "version": v,
            "source_v": source_v,
            "detail": receipt_error,
            "directive": (
                "run_archivist is a one-shot post-commit handoff. It cannot be "
                "replayed for a historical bot or a different source version."
            ),
        })

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

    # 4. Content-bound archive annotation; never a strategy-memory writer.
    llm_result = None
    try:
        from cycle_archivist import run_cycle_archivist_analysis
        llm_result = await run_cycle_archivist_analysis(v, source_v, snapshot, ui)
        # Append LLM notes to archive snapshot
        if llm_result and archive_path.exists():
            snapshot["archivist_notes"] = llm_result
            with locked_file(archive_path, "w") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)

        # Cross-generation lessons are produced only by the identity-bound
        # native replay memory pipeline.  Free-form Archivist text remains
        # advisory inside this exact archive snapshot and is never promoted to
        # prompt evidence or a tracked markdown pool.
    except Exception as e:
        llm_result = {"error": str(e)}

    housekeeping_commit = None
    try:
        housekeeping_commit = _archive_housekeeping_commit(
            v, reap_result, preexisting_dirty
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
        "post_commit_archivist_receipt_digest": str(
            (receipt or {}).get("receipt_digest") or ""
        ),
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


async def _record_crossover_infrastructure(
    target_v,
    parent_a,
    parent_b,
    *,
    component,
    code,
    issues,
    architecture_policy=None,
    metadata=None,
):
    """Persist a neutral crossover retry without consuming an LLM retry."""
    from bot_artifact import hash_path
    from national_runtime_probe import RUNTIME_PROBE_IDENTITY_DIGEST
    from pipeline_infrastructure import infrastructure_attempt_key

    target_dir = get_bot_dir(target_v)
    parent_dir = get_bot_dir(parent_a)
    parent2_dir = get_bot_dir(parent_b)
    candidate_fingerprint = (
        hash_path(target_dir) if target_dir.is_dir() else "missing"
    )
    source_fingerprint = hash_path(parent_dir)
    parent2_fingerprint = hash_path(parent2_dir)
    attempt_key = infrastructure_attempt_key(
        component=str(component or "crossover_preplan"),
        candidate_fingerprint=candidate_fingerprint,
        source_fingerprint=source_fingerprint,
        harness_identity=RUNTIME_PROBE_IDENTITY_DIGEST,
        contract_identity=str(
            (architecture_policy or {}).get("policy_digest") or ""
        ),
        extra={
            "target_v": int(target_v),
            "parent_a": int(parent_a),
            "parent_b": int(parent_b),
            "parent2_fingerprint": parent2_fingerprint,
            "phase": "crossover_preplan",
        },
    )
    result = await _record_infrastructure_failure(
        target_v,
        parent_a,
        owner_tool="run_crossover",
        resume_stage="crossover_running",
        component=str(component or "crossover_preplan"),
        code=str(code),
        attempt_key=attempt_key,
        issues=[str(item)[:500] for item in issues or []],
        max_attempts=3,
        cumulative_attempt_field="crossover_generation_attempt",
        metadata={
            "parent2_v": int(parent_b),
            "candidate_fingerprint": candidate_fingerprint,
            "source_fingerprint": source_fingerprint,
            "parent2_fingerprint": parent2_fingerprint,
            "architecture_policy_digest": str(
                (architecture_policy or {}).get("policy_digest") or ""
            ),
            **(metadata or {}),
        },
    )
    return _json_tool_result({
        **result,
        "error": "CROSSOVER_INFRASTRUCTURE_INCONCLUSIVE",
        "success": False,
        "target_v": target_v,
        "parent_a": parent_a,
        "parent_b": parent_b,
        "directive": (
            "Crossover infrastructure exhausted and the generation was abandoned."
            if result.get("abandoned")
            else "Retry run_crossover for the same checkpoint. The prepared child is "
                 "preserved and only the inconclusive deterministic probe will rerun."
        ),
    })


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

    try:
        parent_a = int(parent_a)
        parent_b = int(parent_b)
        target_v = int(target_v)
    except (TypeError, ValueError):
        return _json_tool_result({
            "error": "CROSSOVER_CHECKPOINT_IDENTITY_MISMATCH",
            "success": False,
            "detail": "parent_a, parent_b, and target_v must be integer versions",
        })

    # The scheduler-owned checkpoint is the lineage authority.  The generic
    # MCP route guard controls which tool may run, but it does not interpret
    # crossover-specific parent roles.  Enforce the complete tuple again at
    # this mutating boundary so a weak caller cannot substitute Parent B or
    # start an unscheduled generation.
    authoritative_ckpt = read_pipeline_checkpoint()
    if not isinstance(authoritative_ckpt, dict):
        return _json_tool_result({
            "error": "CROSSOVER_CHECKPOINT_MISSING",
            "success": False,
            "directive": (
                "Crossover requires the scheduler-owned selected checkpoint; "
                "do not choose parents or a target directly."
            ),
        })
    try:
        checkpoint_identity = (
            int(authoritative_ckpt.get("source_v")),
            int(authoritative_ckpt.get("parent2_v")),
            int(authoritative_ckpt.get("next_v")),
        )
    except (TypeError, ValueError):
        checkpoint_identity = None
    requested_identity = (parent_a, parent_b, target_v)
    if checkpoint_identity != requested_identity:
        return _json_tool_result({
            "error": "CROSSOVER_CHECKPOINT_IDENTITY_MISMATCH",
            "success": False,
            "requested": {
                "source_v": parent_a,
                "parent2_v": parent_b,
                "next_v": target_v,
            },
            "checkpoint": {
                "source_v": authoritative_ckpt.get("source_v"),
                "parent2_v": authoritative_ckpt.get("parent2_v"),
                "next_v": authoritative_ckpt.get("next_v"),
                "stage": authoritative_ckpt.get("stage"),
            },
            "directive": "Use exactly the scheduler-selected crossover lineage.",
        })
    authoritative_stage = str(authoritative_ckpt.get("stage") or "")
    if authoritative_stage not in {"selected", "crossover_running"}:
        return _json_tool_result({
            "error": "CROSSOVER_STAGE_BLOCKED",
            "success": False,
            "stage": authoritative_stage,
            "allowed_stages": ["selected", "crossover_running"],
            "directive": (
                "A prepared or later generation may not rerun crossover. Follow "
                "the checkpoint route or abandon and select a fresh generation."
            ),
        })

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

        if getattr(get_workflow_profile(), "national_execution_mode", None) != "native_tcp":
            raise RuntimeError("crossover supports only native_tcp parents")
        if (parent_a_dir / "national_bot.py").exists():
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
        # Capability/policy assessment is a control-plane prerequisite.  A
        # deterministic parent violation is returned by the evaluator as data;
        # exceptions here mean the assessment itself was inconclusive.  Put it
        # on the same bounded infrastructure ledger so a weak orchestrator
        # cannot call forever, while recording that no child exists yet.
        return await _record_crossover_infrastructure(
            target_v,
            parent_a,
            parent_b,
            component="crossover_parent_capability_policy",
            code="crossover_parent_capability_policy_inconclusive",
            issues=[f"{type(exc).__name__}: {str(exc)[:500]}"],
            architecture_policy=None,
            metadata={"pre_synthesis": True},
        )

    # If a previous invocation already synthesized the child but its preplan
    # runtime probe was inconclusive, retry only that deterministic probe.  Do
    # not reset the directory or spend another crossover LLM attempt.
    resume_prepared_transition = None
    active_crossover_ckpt = authoritative_ckpt
    crossover_infra, crossover_infra_error = _owned_infrastructure_failure(
        active_crossover_ckpt,
        "run_crossover",
    )
    if crossover_infra_error:
        return _json_tool_result({
            "error": "CROSSOVER_INFRASTRUCTURE_STATE_BLOCKED",
            "failure_class": "infrastructure",
            "detail": crossover_infra_error,
            "infra_failure": crossover_infra,
        })
    exhausted = await _execute_exhausted_infrastructure_failure(
        target_v,
        parent_a,
        owner_tool="run_crossover",
    )
    if exhausted is not None:
        return _json_tool_result(exhausted)
    if crossover_infra is not None and bool(
        (crossover_infra.get("metadata") or {}).get("pre_synthesis")
    ):
        # The parent capability probe has now succeeded.  Its overlay was
        # created before a child existed, so clear it before entering the
        # synthesis loop.  An unexpected artifact at this point is not a
        # resumable child and must never inherit the pre-synthesis receipt.
        if target_dir.exists():
            try:
                from tool_bot_management import _do_abandon_generation

                abandon_result = await _do_abandon_generation(
                    reason=f"crossover_pre_synthesis_artifact_unexpected:v{target_v}"
                )
            except Exception as exc:
                abandon_result = {
                    "abandoned": False,
                    "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            return _json_tool_result({
                "error": "CROSSOVER_PRE_SYNTHESIS_ARTIFACT_UNEXPECTED",
                "success": False,
                "failure_class": "integrity",
                "abandoned": bool(abandon_result.get("abandoned")),
                "abandon_result": abandon_result,
            })
        cleared = write_pipeline_checkpoint(
            target_v,
            parent_a,
            "crossover_running",
            parent2_v=parent_b,
            clear_infra_failure=True,
            infra_failure_owner="run_crossover",
            expected_infra_failure_digest=infrastructure_failure_digest(
                crossover_infra
            ),
            touch_stage_timestamp=True,
        )
        if not cleared:
            return _json_tool_result({
                "error": "CROSSOVER_INFRASTRUCTURE_CLEAR_REFUSED",
                "failure_class": "infrastructure",
                "target_v": target_v,
            })
        crossover_infra = None
    if crossover_infra is not None:
        if (
            not target_dir.is_dir()
            or (active_crossover_ckpt or {}).get("stage") != "crossover_running"
        ):
            return _json_tool_result({
                "error": "CROSSOVER_INFRASTRUCTURE_RESUME_TARGET_MISSING",
                "failure_class": "infrastructure",
                "target_v": target_v,
                "directive": (
                    "The infrastructure overlay is bound to a preserved crossover "
                    "candidate, but that candidate/stage is unavailable. Fail closed "
                    "and inspect the checkpoint before any regeneration."
                ),
            })

        # This retry skips synthesis and reuses the earlier deterministic
        # compile/import/smoke/provenance evidence.  Bind that shortcut to the
        # exact complete artifacts captured when the overlay was written;
        # otherwise an edit during the pause would bypass every preplan gate.
        from bot_artifact import hash_path

        infra_metadata = crossover_infra.get("metadata") or {}
        current_candidate_fingerprint = hash_path(target_dir)
        current_source_fingerprint = hash_path(parent_a_dir)
        current_parent2_fingerprint = hash_path(parent_b_dir)
        preserved_child_drift = bool(
            str(infra_metadata.get("candidate_fingerprint") or "")
            != current_candidate_fingerprint
            or str(infra_metadata.get("source_fingerprint") or "")
            != current_source_fingerprint
            or str(infra_metadata.get("parent2_fingerprint") or "")
            != current_parent2_fingerprint
            or str(infra_metadata.get("parent2_v") or "") != str(parent_b)
        )
        if preserved_child_drift:
            try:
                from tool_bot_management import _do_abandon_generation

                abandon_result = await _do_abandon_generation(
                    reason=f"crossover_preserved_child_drift:v{target_v}"
                )
            except Exception as exc:
                abandon_result = {
                    "abandoned": False,
                    "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            log_system_event(
                "pipeline.crossover_preserved_child_drift",
                "error",
                f"Preserved crossover v{target_v} drifted during infrastructure pause",
                {
                    "target_v": target_v,
                    "parent_a": parent_a,
                    "parent_b": parent_b,
                    "expected_candidate_fingerprint": infra_metadata.get(
                        "candidate_fingerprint"
                    ),
                    "current_candidate_fingerprint": current_candidate_fingerprint,
                    "expected_source_fingerprint": infra_metadata.get(
                        "source_fingerprint"
                    ),
                    "current_source_fingerprint": current_source_fingerprint,
                    "expected_parent2_fingerprint": infra_metadata.get(
                        "parent2_fingerprint"
                    ),
                    "current_parent2_fingerprint": current_parent2_fingerprint,
                    "expected_parent2_v": infra_metadata.get("parent2_v"),
                    "abandon_result": abandon_result,
                },
            )
            return _json_tool_result({
                "error": "CROSSOVER_PRESERVED_CHILD_DRIFT",
                "success": False,
                "abandoned": bool(abandon_result.get("abandoned")),
                "failure_class": "integrity",
                "target_v": target_v,
                "abandon_result": abandon_result,
                "directive": (
                    "The preserved artifact no longer matches its retry receipt; "
                    "it was not re-synthesized or revalidated. Start a fresh "
                    "scheduler-selected generation."
                ),
            })
        from runtime_architecture_policy import (
            ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
            evaluate_architecture_transition,
        )

        try:
            resume_prepared_transition = evaluate_architecture_transition(
                parent_a_dir,
                target_dir,
                expected_policy=architecture_policy,
                evaluation_phase=ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
            )
        except Exception as exc:
            resume_prepared_transition = {
                "ok": False,
                "outcome": "infrastructure_failure",
                "infrastructure_failures": [{
                    "component": "runtime_architecture_policy",
                    "failure_class": "internal_infrastructure",
                    "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
                }],
            }
        if resume_prepared_transition.get("outcome") == "infrastructure_failure":
            failures = resume_prepared_transition.get("infrastructure_failures") or []
            return await _record_crossover_infrastructure(
                target_v,
                parent_a,
                parent_b,
                component=(
                    (failures[0] or {}).get("component")
                    if failures and isinstance(failures[0], dict)
                    else "national_runtime_probe"
                ),
                code="crossover_preplan_probe_inconclusive",
                issues=[
                    f"{item.get('component', 'probe')}: "
                    + ", ".join(str(issue) for issue in item.get("issues") or [])
                    for item in failures
                    if isinstance(item, dict)
                ] or ["crossover preplan runtime probe was inconclusive"],
                architecture_policy=architecture_policy,
                metadata={"resumed_preserved_candidate": True},
            )
        if not resume_prepared_transition.get("ok"):
            # The probe recovered and reached a conclusive bot-side rejection.
            # Regeneration is now legitimate, but it starts from Parent A under
            # the normal bounded crossover loop rather than a repair bypass.
            # Clear only in this branch: a successful preserved-child retry
            # keeps the overlay until the final prepared checkpoint so later
            # H2H/position/contract infrastructure failures share one bounded
            # retry budget instead of resetting to attempt 1 forever.
            cleared = write_pipeline_checkpoint(
                target_v,
                parent_a,
                "crossover_running",
                parent2_v=parent_b,
                clear_infra_failure=True,
                infra_failure_owner="run_crossover",
                expected_infra_failure_digest=infrastructure_failure_digest(
                    crossover_infra
                ),
                touch_stage_timestamp=True,
            )
            if not cleared:
                return _json_tool_result({
                    "error": "CROSSOVER_INFRASTRUCTURE_CLEAR_REFUSED",
                    "failure_class": "infrastructure",
                    "target_v": target_v,
                })
            resume_prepared_transition = None

    # --- P1-3: Crossover Parent Compatibility Audit ---
    compat = {
        "compatible": True,
        "compatibility_score": None,
        "conflict_areas": [],
        "suggested_merge_approach": "",
        "audit_unavailable": True,
    }
    committed_projection_receipt = (
        ((authoritative_ckpt.get("audit_context") or {}).get("crossover") or {})
        if isinstance(authoritative_ckpt, dict)
        else {}
    )
    committed_projection_resume = False
    if (
        authoritative_stage == "crossover_running"
        and isinstance(committed_projection_receipt, dict)
        and len(str(
            committed_projection_receipt.get("isolated_output_artifact_hash")
            or ""
        )) == 64
        and target_dir.is_dir()
    ):
        try:
            from bot_artifact import hash_path

            committed_projection_resume = hash_path(target_dir) == str(
                committed_projection_receipt["isolated_output_artifact_hash"]
            )
        except Exception:
            committed_projection_resume = False
    if committed_projection_resume:
        stored_compat = committed_projection_receipt.get("compatibility")
        if not isinstance(stored_compat, dict):
            return _json_tool_result({
                "error": "CROSSOVER_COMMITTED_COMPATIBILITY_RECEIPT_MISSING",
                "success": False,
                "failure_class": "integrity",
                "target_v": target_v,
            })
        compat = stored_compat
    if resume_prepared_transition is not None and isinstance(active_crossover_ckpt, dict):
        stored_compat = (
            ((active_crossover_ckpt.get("audit_context") or {}).get("crossover") or {})
            .get("compatibility")
        )
        if isinstance(stored_compat, dict):
            compat = stored_compat
    try:
        from audit_agents import _run_crossover_compatibility_audit
        if resume_prepared_transition is None and not committed_projection_resume:
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
            # Weak-model compatibility judgement is merge advice only. A single
            # speculative score must never permanently block a parent pair or
            # abandon a generation; deterministic artifact/capability conflicts
            # and actual gate failures own those state transitions.
            log_system_event(
                "pipeline.crossover_compatibility_advisory",
                "warn",
                f"Advisory compatibility concerns for v{parent_a}×v{parent_b}",
                {
                    "parent_a": parent_a,
                    "parent_b": parent_b,
                    "compat": compat,
                    "control_effect": "merge_guidance_only",
                },
            )
    except LLMAvailabilityBlocked:
        raise
    except Exception as e:
        _log.warning("Crossover compat audit error (skipping): %s", e)

    success = True
    if resume_prepared_transition is None:
        success = await _run_crossover(
            parent_a,
            parent_b,
            target_v,
            ui,
            compatibility=compat,
            architecture_policy=architecture_policy,
            capability_context=capability_context,
        )

    if (
        isinstance(success, dict)
        and success.get("outcome") == "concurrent_effect_in_progress"
    ):
        # Another process owns the only valid synthesis lease.  This is a
        # transient idempotency response: do not write an infrastructure
        # overlay or mutate the selected checkpoint underneath the owner.
        return _json_tool_result({
            "error": "CROSSOVER_SYNTHESIS_IN_PROGRESS",
            "success": False,
            "retryable": True,
            "failure_class": "concurrency",
            "target_v": target_v,
            "parent_a": parent_a,
            "parent_b": parent_b,
            "issue": success.get("issue"),
        })

    if isinstance(success, dict) and success.get("outcome") == "infrastructure_failure":
        failures = success.get("infrastructure_failures") or []
        return await _record_crossover_infrastructure(
            target_v,
            parent_a,
            parent_b,
            component=str(success.get("component") or "national_runtime_probe"),
            code="crossover_preplan_probe_inconclusive",
            issues=[
                f"{item.get('component', 'probe')}: "
                + ", ".join(str(issue) for issue in item.get("issues") or [])
                for item in failures
                if isinstance(item, dict)
            ] or ["crossover preplan runtime probe was inconclusive"],
            architecture_policy=architecture_policy,
            metadata={"resumed_preserved_candidate": False},
        )

    post_synthesis_checkpoint = read_pipeline_checkpoint() or {}
    post_synthesis_crossover_receipt = (
        ((post_synthesis_checkpoint.get("audit_context") or {}).get("crossover") or {})
        if isinstance(post_synthesis_checkpoint, dict)
        else {}
    )
    if (
        success
        and resume_prepared_transition is None
        and post_synthesis_checkpoint.get("stage") == "crossover_running"
    ):
        expected_output_hash = str(
            post_synthesis_crossover_receipt.get("isolated_output_artifact_hash")
            or ""
        )
        receipt_compat = post_synthesis_crossover_receipt.get("compatibility")
        try:
            from bot_artifact import hash_path

            actual_output_hash = hash_path(target_dir)
        except Exception as exc:
            return _json_tool_result({
                "error": "CROSSOVER_COMMITTED_OUTPUT_IDENTITY_UNAVAILABLE",
                "success": False,
                "failure_class": "integrity",
                "message": f"{type(exc).__name__}: {str(exc)[:240]}",
            })
        if (
            len(expected_output_hash) != 64
            or actual_output_hash != expected_output_hash
            or not isinstance(receipt_compat, dict)
        ):
            return _json_tool_result({
                "error": "CROSSOVER_COMMITTED_RECEIPT_MISMATCH",
                "success": False,
                "failure_class": "integrity",
                "expected_output_artifact_hash": expected_output_hash,
                "actual_output_artifact_hash": actual_output_hash,
            })
        # The compatibility evidence that guided synthesis is authoritative;
        # a resumed wrapper invocation may not replace it with a fresh LLM call.
        compat = receipt_compat

    # Crossover is a preparation operator.  Its child is a recombination
    # baseline, not a completed generation plan: direction audit, optional
    # literature probe, Master, and Workers must still produce the generation's
    # reviewed innovation before quality gates can run.
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

        prepared_transition = resume_prepared_transition
        if isinstance(architecture_policy, dict) and prepared_transition is None:
            try:
                from runtime_architecture_policy import (
                    ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
                    evaluate_architecture_transition,
                )

                prepared_transition = evaluate_architecture_transition(
                    parent_a_dir,
                    target_dir,
                    expected_policy=architecture_policy,
                    evaluation_phase=ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
                )
            except Exception as exc:
                prepared_transition = {
                    "ok": False,
                    "outcome": "infrastructure_failure",
                    "infrastructure_failures": [{
                        "component": "runtime_architecture_policy",
                        "failure_class": "internal_infrastructure",
                        "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
                    }],
                }
        if isinstance(prepared_transition, dict):
            if prepared_transition.get("outcome") == "infrastructure_failure":
                failures = prepared_transition.get("infrastructure_failures") or []
                return await _record_crossover_infrastructure(
                    target_v,
                    parent_a,
                    parent_b,
                    component=(
                        (failures[0] or {}).get("component")
                        if failures and isinstance(failures[0], dict)
                        else "national_runtime_probe"
                    ),
                    code="crossover_prepared_snapshot_probe_inconclusive",
                    issues=[
                        f"{item.get('component', 'probe')}: "
                        + ", ".join(str(issue) for issue in item.get("issues") or [])
                        for item in failures
                        if isinstance(item, dict)
                    ] or ["prepared baseline probe was inconclusive"],
                    architecture_policy=architecture_policy,
                )
            if not prepared_transition.get("ok"):
                log_system_event(
                    "pipeline.crossover_preplan_postcheck_divergence",
                    "error",
                    f"Crossover v{target_v} failed the final preplan contract recheck",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a,
                        "parent_b": parent_b,
                        "policy_identity_errors": prepared_transition.get(
                            "policy_identity_errors"
                        ) or [],
                        "regressions": prepared_transition.get("regressions") or [],
                        "runtime_floor_failures": prepared_transition.get(
                            "runtime_floor_failures"
                        ) or [],
                    },
                )
                return _json_tool_result({
                    "error": "CROSSOVER_PREPLAN_POSTCHECK_DIVERGENCE",
                    "success": False,
                    "failure_class": "candidate_contract",
                    "target_v": target_v,
                    "transition": prepared_transition,
                    "directive": (
                        "Fail closed: the crossover agent's accepted preplan result "
                        "did not reproduce. Do not route this candidate directly to "
                        "quality or synthetic repair; abandon/re-run crossover."
                    ),
                })
        try:
            from national_position_contract import detect_position_semantics_errors
            target_position_errors = detect_position_semantics_errors(get_bot_dir(target_v))
        except Exception as exc:
            target_position_errors = [f"position_contract_check_error: {type(exc).__name__}: {str(exc)[:200]}"]
        if target_position_errors:
            # _run_crossover already checked this exact deterministic contract.
            # Disagreement is an infrastructure/control-plane fault, never a
            # license to synthesize a quality_failed repair plan that skips
            # direction audit and Master.
            log_system_event(
                "pipeline.crossover_position_postcheck_divergence",
                "error",
                f"Crossover v{parent_a}×v{parent_b} position postcheck diverged",
                {
                    "target_v": target_v,
                    "parent_a": parent_a,
                    "parent_b": parent_b,
                    "errors": target_position_errors[:10],
                },
            )
            return await _record_crossover_infrastructure(
                target_v,
                parent_a,
                parent_b,
                component="national_position_contract",
                code="crossover_position_postcheck_divergence",
                issues=target_position_errors[:10],
                architecture_policy=architecture_policy,
            )

        prepared_baseline_contract = None
        prepared_architecture_policy = architecture_policy
        if isinstance(architecture_policy, dict):
            try:
                from evidence_snapshot import load_generation_snapshot_identity
                from prepared_baseline_contract import (
                    build_prepared_baseline_contract,
                )
                from runtime_architecture_policy import (
                    build_architecture_policy,
                    build_prepared_capability_snapshot,
                )

                h2h_identity = load_generation_snapshot_identity(target_v)
                if not h2h_identity.get("available"):
                    raise RuntimeError(
                        "generation H2H snapshot unavailable: "
                        f"{h2h_identity.get('reason', 'unknown')}"
                    )
                selection_identity = (
                    ((active_crossover_ckpt or {}).get("audit_context") or {})
                    .get("selection") or {}
                )
                expected_manifest_digest = str(
                    selection_identity.get("h2h_snapshot_manifest_digest") or ""
                )
                expected_h2h_sha = str(
                    selection_identity.get("h2h_snapshot_sha256") or ""
                )
                if (
                    expected_manifest_digest
                    and expected_manifest_digest
                    != str(h2h_identity.get("manifest_digest") or "")
                ):
                    raise RuntimeError("generation H2H snapshot manifest identity drift")
                if (
                    expected_h2h_sha
                    and expected_h2h_sha != str(h2h_identity.get("sha256") or "")
                ):
                    raise RuntimeError("generation H2H snapshot payload identity drift")
                capability_snapshot = build_prepared_capability_snapshot(
                    parent_a_dir,
                    target_dir,
                    parent_capabilities=prepared_transition.get(
                        "source_capabilities"
                    ),
                    prepared_capabilities=prepared_transition.get(
                        "candidate_capabilities"
                    ),
                )
                prepared_baseline_contract = build_prepared_baseline_contract(
                    parent_a_dir,
                    parent_b_dir,
                    target_dir,
                    source_v=parent_a,
                    parent2_v=parent_b,
                    next_v=target_v,
                    capability_snapshot=capability_snapshot,
                    preplan_transition=prepared_transition,
                    expected_policy_digest=str(
                        (architecture_policy or {}).get("policy_digest") or ""
                    ),
                    prepare_scope_files=prepare_scope_files,
                    compatibility=compat,
                    h2h_snapshot_identity=h2h_identity,
                )
                prepared_architecture_policy = build_architecture_policy(
                    parent_a_dir,
                    source_capabilities=prepared_transition.get(
                        "source_capabilities"
                    ),
                    prepared_capability_snapshot=capability_snapshot,
                )
            except Exception as exc:
                return await _record_crossover_infrastructure(
                    target_v,
                    parent_a,
                    parent_b,
                    component="prepared_baseline_contract",
                    code="prepared_baseline_contract_build_failed",
                    issues=[f"{type(exc).__name__}: {str(exc)[:500]}"],
                    architecture_policy=architecture_policy,
                )
        prepared_clear_kwargs = {}
        if crossover_infra is not None:
            prepared_clear_kwargs = {
                "clear_infra_failure": True,
                "infra_failure_owner": "run_crossover",
                "expected_infra_failure_digest": infrastructure_failure_digest(
                    crossover_infra
                ),
            }
        prepared_checkpoint_ok = write_pipeline_checkpoint(
            target_v,
            parent_a,
            "prepared",
            parent2_v=parent_b,
            prepare_scope_files=prepare_scope_files,
            audit_context={
                "crossover": {
                    **(
                        post_synthesis_crossover_receipt
                        if isinstance(post_synthesis_crossover_receipt, dict)
                        else {}
                    ),
                    "parent_a": parent_a,
                    "parent_b": parent_b,
                    "compatibility": compat,
                    "source_architecture_policy": architecture_policy,
                    "prepared_architecture_policy": prepared_architecture_policy,
                    "prepared_baseline_contract_digest": str(
                        (prepared_baseline_contract or {}).get("contract_digest") or ""
                    ),
                    "baseline_prepared": True,
                },
                "prepared_baseline_contract": prepared_baseline_contract,
                "prepared_artifact_contract": (
                    (prepared_baseline_contract or {}).get(
                        "prepared_artifact_contract"
                    )
                ),
            },
            touch_stage_timestamp=True,
            **prepared_clear_kwargs,
        )
        if not prepared_checkpoint_ok:
            log_system_event(
                "pipeline.crossover_prepared_checkpoint_refused",
                "error",
                f"Crossover v{target_v} baseline could not enter prepared stage",
                {
                    "target_v": target_v,
                    "parent_a": parent_a,
                    "parent_b": parent_b,
                },
            )
            return _json_tool_result({
                "error": "CROSSOVER_PREPARED_CHECKPOINT_REFUSED",
                "success": False,
                "target_v": target_v,
                "directive": (
                    "Do not run quality gates or regenerate the child. Inspect the "
                    "checkpoint CAS/ledger refusal and resume only from durable state."
                ),
            })
        try:
            log_system_event('pipeline.crossover_done', 'info',
                f'Crossover v{parent_a}×v{parent_b} → v{target_v} succeeded',
                {
                    'target_v': target_v,
                    'parent_a': parent_a,
                    'parent_b': parent_b,
                    'checkpoint_stage': 'prepared',
                })
            log_system_event(
                "pipeline.crossover_resume_direction_audit", "info",
                f"Crossover v{target_v} baseline ready; next step is run_direction_audit",
                {"target_v": target_v, "parent_a": parent_a,
                 "parent_b": parent_b, "next_step": "run_direction_audit"},
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

    result = {
        "success": success,
        "stage": "prepared" if success else None,
        "next_tool": "run_direction_audit" if success else None,
        "directive": (
            "Crossover produced only the recombination baseline. Continue with "
            "run_direction_audit, the governance-required literature probe when "
            "stagnant, run_master, and execute_workers before quality gates."
            if success
            else None
        ),
        "logs": ui.get_output(),
    }
    return _json_tool_result(result)
