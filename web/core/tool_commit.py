"""Pipeline tools: commit, archivist, and crossover."""

import json
import hashlib
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

from logging_config import get_logger
_log = get_logger("commit")

from bot_namespace import (
    FIRST_STRICT_POLICY_VERSION,
    EVOLUTION_BRANCH,
    bot_name,
    bot_tag,
    high_water_tag,
    parse_bot_version,
)
from tool_runtime_guard import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
    load_ratings,
    git_has_tag,
    git_dir_is_committed,
    clear_pipeline_checkpoint,
    RESULTS_DIR,
    MAX_ACTIVE_BOTS,
    _run_crossover,
    ARCHIVE_DIR,
    write_pipeline_checkpoint,
    archive_rotate_files,
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
    bot_publication_lock,
    build_archive_rotation_plan,
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

import tool_commit_gate_ledger as _gl  # noqa: E402
import tool_commit_official_gate as _og  # noqa: E402

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
    refs = [EVOLUTION_BRANCH, bot_tag(version)]
    high_water = high_water_tag(int(version))
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
    publication_result = {
        "committed": True,
        "version": int(v),
        "source_v": int(source_v),
        "publication_id": intent.get("publication_id"),
        "commit_oid": local_state.get("commit_oid"),
        "local_refs": local_state.get("local_refs") or {},
        "local_publication_proof": frozen_local_proof,
        "push_ok": bool(local_state.get("push_ok")),
        "remote_proof": remote_proof,
        "completed_sentinel_written": True,
        "checkpoint_cleared": False,
    }
    try:
        from post_publication_handoff import ensure_post_publication_handoff

        allow_local_only = (
            not remote_required
            and os.environ.get(
                "POK_ALLOW_LOCAL_ONLY_POST_PUBLICATION_HANDOFF_FOR_TESTS"
            )
            == "1"
        )
        # Keep the publication lock across handoff/archive durability proof and
        # the exact checkpoint CAS.  A clear can never outrun the discoverable
        # post-publication obligation.
        with bot_publication_lock():
            handoff = ensure_post_publication_handoff(
                version=v,
                source_v=source_v,
                publishing_checkpoint=current,
                publication_result=publication_result,
                allow_local_only=allow_local_only,
            )
            cleared = clear_pipeline_checkpoint(
                expected_workflow_run_id=current.get("workflow_run_id"),
                expected_next_v=int(v),
                expected_source_v=int(source_v),
                expected_checkpoint_revision=current.get("checkpoint_revision"),
                expected_checkpoint_stage="publishing",
            )
    except Exception as exc:
        return _publication_pending_result(
            v,
            source_v,
            error=(
                "COMMIT PENDING: publication is proven but the durable "
                "post-publication handoff did not converge."
            ),
            reason=f"{type(exc).__name__}: {str(exc)[:500]}",
            completed_sentinel_written=True,
            remote_proof=remote_proof,
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
        **publication_result,
        "checkpoint_cleared": True,
        "archivist_pending": True,
        "post_publication_handoff_identity_digest": handoff.get(
            "identity_digest"
        ),
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


def validate_commit_gate_ledger(*args, **kwargs):
    """Delegate to tool_commit_gate_ledger."""
    return _gl.validate_commit_gate_ledger(*args, **kwargs)


def _checkpoint_execution_mode(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._checkpoint_execution_mode(*args, **kwargs)


def _truthy_env(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._truthy_env(*args, **kwargs)


def _official_preferred_opponent(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._official_preferred_opponent(*args, **kwargs)


def _official_gate_feedback(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._official_gate_feedback(*args, **kwargs)


def _official_gate_is_bot_blocker(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._official_gate_is_bot_blocker(*args, **kwargs)


def _official_job_projection(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._official_job_projection(*args, **kwargs)


def _record_official_job_checkpoint(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._record_official_job_checkpoint(*args, **kwargs)


def _record_official_full_gate_checkpoint(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._record_official_full_gate_checkpoint(*args, **kwargs)


def _record_official_bootstrap_required_checkpoint(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._record_official_bootstrap_required_checkpoint(*args, **kwargs)


def _record_official_full_pass_checkpoint(*args, **kwargs):
    """Delegate to tool_commit_official_gate."""
    return _og._record_official_full_pass_checkpoint(*args, **kwargs)


async def _run_official_full_commit_gate(*args, **kwargs):
    """Delegate to tool_commit_official_gate (async)."""
    return await _og._run_official_full_commit_gate(*args, **kwargs)


@tool("commit_bot", "Commit a bot generation with git commit and tag. review_approved must be true (set after run_review returns approved:true).", {"version": int, "source_v": int, "strategy": str, "review_approved": bool})
async def commit_bot(args):
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
            baseline_head=_git("rev-parse", f"refs/heads/{EVOLUTION_BRANCH}").strip(),
            baseline_remote_main=_git(
                "rev-parse", f"refs/remotes/origin/{EVOLUTION_BRANCH}", check=False
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

    # Every initial or resumed publication now stops at the same durable
    # handoff boundary.  Stability accounting, daemon signals, archive
    # rotation, annotation, reap, and housekeeping are owned exclusively by
    # run_archivist's step journal; starting any of them here would recreate a
    # clear-before-archive crash window and make recovery path-dependent.
    _set_pipeline_status(f"Published v{v}; Archivist handoff pending", is_working=False)
    try:
        log_system_event(
            "pipeline.publication_handoff_pending",
            "success",
            f"Published v{v}; durable post-publication handoff is pending",
            {
                "version": v,
                "source_v": source_v,
                "publication_id": publication_result.get("publication_id"),
                "handoff_identity_digest": publication_result.get(
                    "post_publication_handoff_identity_digest"
                ),
                "checkpoint_cleared": True,
                "next_tool": "run_archivist",
            },
        )
    except Exception:
        pass
    handoff_result = {
        **publication_result,
        "push_ok": push_ok,
        "next_tool": "run_archivist",
        "directive": (
            "Call run_archivist for this exact version/source before preparing "
            "another generation."
        ),
    }
    if official_full_gate:
        handoff_result["official_full_gate"] = {
            "status": official_certification_status.get("status"),
            "mode": official_certification_status.get("mode"),
            "cache_hit": official_certification_status.get("cache_hit"),
            "official_evidence_path": official_certification_status.get(
                "official_evidence_path"
            ),
            "opponent": (official_full_gate.get("opponent_selection") or {}).get(
                "opponent"
            ),
        }
    if novelty_info:
        handoff_result["novelty_gate"] = novelty_info
    return _json_tool_result(handoff_result)


# ──────────────────────────────────────────────
# Archivist Stage
# ──────────────────────────────────────────────



def _safe_log_tree_manifest(root: Path, *, version: int) -> dict:
    """Hash one exact strict-generation log tree without following links."""

    from bot_artifact import canonical_digest

    if int(version) < FIRST_STRICT_POLICY_VERSION:
        raise RuntimeError("legacy_log_tree_forbidden")
    expected_parent = RESULTS_DIR / f"v{int(version)}"
    if root.parent != expected_parent:
        raise RuntimeError("log_tree_root_outside_strict_generation")
    for directory in (RESULTS_DIR, expected_parent):
        directory_stat = os.lstat(directory)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_ISLNK(directory_stat.st_mode)
        ):
            raise RuntimeError("log_tree_parent_unsafe")
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise RuntimeError("log_tree_root_unsafe")
    entries: list[dict] = []
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        before = os.lstat(directory)
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise RuntimeError("log_tree_directory_unsafe")
        with os.scandir(directory) as scanner:
            children = sorted(scanner, key=lambda item: item.name)
        if len(entries) + len(children) > 4096:
            raise RuntimeError("log_tree_member_limit")
        for child in children:
            if (
                not child.name
                or child.name in {".", ".."}
                or "/" in child.name
                or "\\" in child.name
                or any(ord(char) < 32 for char in child.name)
            ):
                raise RuntimeError("log_tree_member_name_unsafe")
            relative = f"{prefix}/{child.name}" if prefix else child.name
            if len(relative.encode("utf-8")) > 1024:
                raise RuntimeError("log_tree_member_path_too_long")
            metadata = child.stat(follow_symlinks=False)
            child_path = Path(child.path)
            if stat.S_ISDIR(metadata.st_mode):
                entries.append({"path": relative, "kind": "directory"})
                pending.append((child_path, relative))
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise RuntimeError("log_tree_member_unsafe")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(child_path, flags)
            hasher = hashlib.sha256()
            size = 0
            try:
                opened = os.fstat(descriptor)
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    size += len(chunk)
                live = os.lstat(child_path)
            finally:
                os.close(descriptor)
            if (
                opened.st_nlink != 1
                or live.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or (live.st_dev, live.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or opened.st_size != size
                or live.st_size != size
                or opened.st_mtime_ns != live.st_mtime_ns
                or opened.st_ctime_ns != live.st_ctime_ns
            ):
                raise RuntimeError("log_tree_member_changed")
            entries.append({
                "path": relative,
                "kind": "file",
                "size": size,
                "sha256": hasher.hexdigest(),
            })
        after = os.lstat(directory)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise RuntimeError("log_tree_directory_changed")
    entries.sort(key=lambda row: (row["path"], row["kind"]))
    payload = {
        "schema_version": 1,
        "kind": "strict-generation-log-tree",
        "version": int(version),
        "source_relative_path": f"v{int(version)}/logs",
        "entries": entries,
    }
    return {**payload, "tree_digest": canonical_digest(payload)}



def _converge_and_verify_reaped_target(name: str, record: dict) -> dict:
    """Finish a planned tombstone crash and return exact local/remote proof."""

    from bot_namespace import parse_bot_version
    from evolution_infra import load_reaped_bot_versions
    from national_epoch_registry import REAPED_TAG_PREFIX

    version = parse_bot_version(name)
    if version is None or version < FIRST_STRICT_POLICY_VERSION:
        raise RuntimeError("planned_reap_target_invalid")
    completion_ref = f"refs/tags/{bot_tag(version)}"
    tombstone_name = f"{REAPED_TAG_PREFIX}{version}"
    tombstone_ref = f"refs/tags/{tombstone_name}"
    if _git("cat-file", "-t", completion_ref, check=False).strip() != "tag":
        raise RuntimeError("reap_completion_tag_missing")
    if _git("cat-file", "-t", tombstone_ref, check=False).strip() != "tag":
        raise RuntimeError("reap_tombstone_tag_missing")
    completion_commit = _git(
        "rev-parse", f"{completion_ref}^{{commit}}", check=False
    ).strip()
    tombstone_object = _git("rev-parse", tombstone_ref, check=False).strip()
    tombstone_commit = _git(
        "rev-parse", f"{tombstone_ref}^{{commit}}", check=False
    ).strip()
    if (
        len(tombstone_object) != 40
        or len(completion_commit) != 40
        or tombstone_commit != completion_commit
    ):
        raise RuntimeError("reap_tombstone_identity_mismatch")

    remote = record["identity"]["remote_publication"]
    remote_proof = {"required": remote.get("required") is True}
    if remote.get("required") is True:
        wanted = (
            f"refs/heads/{EVOLUTION_BRANCH}",
            tombstone_ref,
            f"{tombstone_ref}^{{}}",
        )
        refs = _remote_ref_snapshot(*wanted)
        if (
            refs.get(tombstone_ref) != tombstone_object
            or refs.get(f"{tombstone_ref}^{{}}") != completion_commit
        ):
            if not git_push_refs(tombstone_name):
                raise RuntimeError("reap_tombstone_remote_push_failed")
            refs = _remote_ref_snapshot(*wanted)
        if (
            refs.get(tombstone_ref) != tombstone_object
            or refs.get(f"{tombstone_ref}^{{}}") != completion_commit
        ):
            raise RuntimeError("reap_remote_proof_mismatch")
        remote_main = str(refs.get(f"refs/heads/{EVOLUTION_BRANCH}") or "")
        if len(remote_main) != 40 or any(
            char not in "0123456789abcdef" for char in remote_main
        ):
            raise RuntimeError("reap_remote_main_invalid")
        from evolution_infra import _git_command_succeeds

        tracking = _git(
            "rev-parse", f"refs/remotes/origin/{EVOLUTION_BRANCH}", check=False
        ).strip()
        if tracking != remote_main:
            _git(
                "fetch",
                "--no-tags",
                "origin",
                f"refs/heads/{EVOLUTION_BRANCH}:refs/remotes/origin/{EVOLUTION_BRANCH}",
            )
        if not _git_command_succeeds(
            "merge-base",
            "--is-ancestor",
            record["identity"]["commit_oid"],
            remote_main,
        ):
            raise RuntimeError("reap_publication_not_on_remote_main")
        remote_proof.update({
            "publication_remote_main_oid": remote.get("remote_main_oid"),
            "verified_remote_main_oid": remote_main,
            "publication_commit_is_ancestor": True,
            "tombstone_object_oid": refs[tombstone_ref],
            "tombstone_commit_oid": refs[f"{tombstone_ref}^{{}}"],
        })
    elif remote.get("explicit_test_mode") is not True:
        raise RuntimeError("reap_local_only_mode_unproven")

    # Once the durable local/required-remote tombstone is proven, removing the
    # ignored completion capability is the idempotent final half of reaping.
    sentinel = get_bot_dir(version) / ".completed"
    if os.path.lexists(sentinel):
        metadata = os.lstat(sentinel)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("reap_completed_sentinel_unsafe")
        sentinel.unlink()
    from evolution_infra import _fsync_directory

    # Durably prove the directory entry absence even on a crash retry where
    # the original reaper already removed the sentinel before its own fsync.
    _fsync_directory(sentinel.parent)
    if os.path.lexists(sentinel):
        raise RuntimeError("reap_completed_sentinel_still_present")
    if version not in load_reaped_bot_versions():
        raise RuntimeError("reap_registry_projection_missing")
    if name in set(get_active_bots()):
        raise RuntimeError("reaped_target_still_active")
    if _git("rev-parse", "HEAD").strip() != record["identity"]["commit_oid"]:
        raise RuntimeError("reap_changed_head")
    return {
        "bot": name,
        "version": version,
        "completion_commit_oid": completion_commit,
        "tombstone_tag": tombstone_name,
        "tombstone_object_oid": tombstone_object,
        "tombstone_commit_oid": tombstone_commit,
        "completed_sentinel_absent": True,
        "registry_projection_present": True,
        "remote_proof": remote_proof,
    }



async def _run_durable_post_publication_archivist(v: int, source_v: int):
    """Execute or resume every post-publication effect through one journal."""

    from bot_artifact import canonical_digest
    from post_publication_handoff import (
        claim_post_publication_handoff,
        complete_handoff_step,
        complete_post_publication_handoff,
        load_archive_snapshot,
        plan_handoff_step,
        release_post_publication_handoff_claim,
        write_archive_annotation,
    )

    claim_id = ""
    try:
        record, claim_id = claim_post_publication_handoff(v, source_v)
        if record.get("state") == "completed":
            return {
                "version": v,
                "source_v": source_v,
                "archivist_completed": True,
                "idempotent_replay": True,
            }
        _set_pipeline_status(f"Archiving v{v}")
        _git_ensure_main_branch()
        if _git("rev-parse", "HEAD").strip() != record["identity"]["commit_oid"]:
            raise RuntimeError("post_publication_head_not_publication_commit")
        if _git_dirty_paths():
            raise RuntimeError("post_publication_worktree_not_clean")
        snapshot = load_archive_snapshot(v)
        publishing_checkpoint = snapshot["publishing_checkpoint_projection"]
        publication_result = _handoff_publication_result(record)

        def done(name):
            return record["steps"][name].get("status") == "completed"

        if not done("stability_observation"):
            from stability_observation import record_published_generation

            row = record["steps"]["stability_observation"]
            if row.get("status") == "pending":
                plan = {
                    "schema_version": 1,
                    "kind": "stability-observation-plan",
                    "publication_id": record["identity"]["publication_id"],
                    "publishing_checkpoint_digest": record["identity"][
                        "publishing_checkpoint_digest"
                    ],
                    "strength_evidence_identity_digest": canonical_digest(
                        snapshot["strength_evidence_identity"]
                    ),
                }
                record = plan_handoff_step(
                    v, source_v, claim_id, "stability_observation", plan
                )
                row = record["steps"]["stability_observation"]
            projection = record_published_generation(
                version=v,
                publication_result=publication_result,
                publishing_checkpoint=publishing_checkpoint,
            )
            record = complete_handoff_step(
                v,
                source_v,
                claim_id,
                "stability_observation",
                {
                    "plan_digest": row["plan_digest"],
                    "publication_id": record["identity"]["publication_id"],
                    "continuity_id": projection.get("continuity_id"),
                    "count": projection.get("count"),
                    "target": projection.get("target"),
                    "complete": projection.get("complete"),
                },
            )

        if not done("reap_signal"):
            row = record["steps"]["reap_signal"]
            if row.get("status") == "pending":
                signal_text = f"{time.time():.6f}\n"
                plan = {
                    "schema_version": 1,
                    "kind": "rating-daemon-refresh-plan",
                    "publication_id": record["identity"]["publication_id"],
                    "signal_text": signal_text,
                    "signal_sha256": hashlib.sha256(
                        signal_text.encode("utf-8")
                    ).hexdigest(),
                }
                record = plan_handoff_step(
                    v, source_v, claim_id, "reap_signal", plan
                )
                row = record["steps"]["reap_signal"]
            plan = row["plan"]
            signal_path = RESULTS_DIR / ".reap_signal"
            signal_text = str(plan["signal_text"])
            # The daemon may consume this file before the receipt write. A
            # retry safely reissues the same refresh capability.
            from evolution_infra import _atomic_publish_state_text, _locked_state_sidecar
            import fcntl

            with _locked_state_sidecar(signal_path, lock_type=fcntl.LOCK_EX):
                _atomic_publish_state_text(signal_path, signal_text)
            record = complete_handoff_step(
                v, source_v, claim_id, "reap_signal", {
                    "plan_digest": row["plan_digest"],
                    "publication_id": record["identity"]["publication_id"],
                    "signal_sha256": hashlib.sha256(
                        signal_text.encode("utf-8")
                    ).hexdigest(),
                }
            )

        if not done("priority_eval"):
            row = record["steps"]["priority_eval"]
            if row.get("status") == "pending":
                priority = {
                    "bot": bot_name(v),
                    "min_games": 500,
                    "since": time.time(),
                    "publication_id": record["identity"]["publication_id"],
                }
                record = plan_handoff_step(
                    v,
                    source_v,
                    claim_id,
                    "priority_eval",
                    {
                        "schema_version": 1,
                        "kind": "priority-evaluation-plan",
                        "payload": priority,
                    },
                )
                row = record["steps"]["priority_eval"]
            priority = row["plan"]["payload"]
            priority_sha = _durable_archivist_state_write(
                RESULTS_DIR / "priority_eval.json", priority
            )
            record = complete_handoff_step(
                v, source_v, claim_id, "priority_eval", {
                    "plan_digest": row["plan_digest"],
                    "bot": bot_name(v),
                    "min_games": 500,
                    "publication_id": record["identity"]["publication_id"],
                    "payload_sha256": priority_sha,
                }
            )

        if not done("archive_rotation"):
            row = record["steps"]["archive_rotation"]
            if row.get("status") == "pending":
                rotation_plan = build_archive_rotation_plan(
                    v,
                    record["identity"]["publication_id"],
                )
                record = plan_handoff_step(
                    v,
                    source_v,
                    claim_id,
                    "archive_rotation",
                    rotation_plan,
                )
                row = record["steps"]["archive_rotation"]
            rotations = archive_rotate_files(v, row["plan"])
            if any(
                not isinstance(item, dict)
                or item.get("source_preserved_append_only") is not True
                or len(str(item.get("rotation_id") or "")) != 64
                or len(str(item.get("archive_sha256") or "")) != 64
                for item in rotations
            ):
                raise RuntimeError("archive_rotation_receipt_invalid")
            record = complete_handoff_step(
                v, source_v, claim_id, "archive_rotation", {
                    "plan_digest": row["plan_digest"],
                    "version": v,
                    "rotations": rotations,
                    "rotation_set_digest": canonical_digest(rotations),
                }
            )

        if not done("log_cleanup"):
            row = record["steps"]["log_cleanup"]
            if row.get("status") == "pending":
                log_plan = _build_strict_log_cleanup_plan(v)
                log_plan["publication_id"] = record["identity"]["publication_id"]
                record = plan_handoff_step(
                    v, source_v, claim_id, "log_cleanup", log_plan
                )
                row = record["steps"]["log_cleanup"]
            log_archives = _execute_strict_log_cleanup(
                row["plan"],
                expected_handoff_version=v,
                expected_publication_id=record["identity"]["publication_id"],
            )
            record = complete_handoff_step(
                v, source_v, claim_id, "log_cleanup", {
                    "plan_digest": row["plan_digest"],
                    "version": v,
                    "archives": log_archives,
                    "archive_set_digest": canonical_digest(log_archives),
                }
            )

        reap_row = record["steps"]["pool_reap"]
        if reap_row.get("status") != "completed":
            if reap_row.get("status") == "pending":
                reap_plan = _build_pool_reap_plan(record)
                record = plan_handoff_step(
                    v, source_v, claim_id, "pool_reap", reap_plan
                )
                reap_row = record["steps"]["pool_reap"]
            reap_plan = reap_row["plan"]
            reap_output = await _execute_pool_reap_plan(reap_plan, record)
            record = complete_handoff_step(
                v,
                source_v,
                claim_id,
                "pool_reap",
                {**reap_output, "plan_digest": reap_row["plan_digest"]},
            )

        if not done("cycle_annotation"):
            snapshot = load_archive_snapshot(v)
            row = record["steps"]["cycle_annotation"]
            if row.get("status") == "pending":
                unannotated = dict(snapshot)
                unannotated.pop("archivist_notes", None)
                record = plan_handoff_step(
                    v,
                    source_v,
                    claim_id,
                    "cycle_annotation",
                    {
                        "schema_version": 1,
                        "kind": "cycle-archivist-annotation-plan",
                        "publication_id": record["identity"]["publication_id"],
                        "archive_pre_annotation_digest": canonical_digest(
                            unannotated
                        ),
                    },
                )
                row = record["steps"]["cycle_annotation"]
            unannotated = dict(snapshot)
            unannotated.pop("archivist_notes", None)
            if canonical_digest(unannotated) != row["plan"].get(
                "archive_pre_annotation_digest"
            ):
                raise RuntimeError("cycle_annotation_archive_preimage_changed")
            from post_publication_handoff import local_handoff_identity_errors

            local_cycle_issues = local_handoff_identity_errors(record)
            if local_cycle_issues:
                raise RuntimeError(
                    "cycle_annotation_local_identity_invalid:"
                    + ";".join(local_cycle_issues[:30])
                )
            existing_annotation = snapshot.get("archivist_notes")
            if existing_annotation is not None:
                from cycle_archivist import (
                    _offline_cycle_input_errors,
                    annotation_identity_errors,
                )

                issues = _offline_cycle_input_errors(
                    snapshot,
                    record,
                    version=v,
                    source_v=source_v,
                )
                issues.extend(annotation_identity_errors(
                    existing_annotation,
                    snapshot,
                    version=v,
                    source_v=source_v,
                ))
                if issues:
                    raise RuntimeError("existing_cycle_annotation_invalid")
                annotation = existing_annotation
            else:
                from cycle_archivist import run_cycle_archivist_analysis

                annotation = await run_cycle_archivist_analysis(
                    v,
                    source_v,
                    snapshot,
                    _get_ui(),
                    handoff_record=record,
                )
                if annotation.get("status") != "annotated":
                    raise RuntimeError(
                        "cycle_archivist_required_analysis_unavailable:"
                        + ";".join(annotation.get("issues") or [])
                    )
            annotation_receipt = write_archive_annotation(
                v, source_v, claim_id, annotation
            )
            record = complete_handoff_step(
                v,
                source_v,
                claim_id,
                "cycle_annotation",
                {**annotation_receipt, "plan_digest": row["plan_digest"]},
            )

        if not done("housekeeping"):
            row = record["steps"]["housekeeping"]
            dependency_receipts = {
                name: record["steps"][name]["receipt"]["receipt_digest"]
                for name in (
                    "archive_rotation", "log_cleanup", "pool_reap",
                    "cycle_annotation",
                )
            }
            if row.get("status") == "pending":
                record = plan_handoff_step(
                    v,
                    source_v,
                    claim_id,
                    "housekeeping",
                    {
                        "schema_version": 1,
                        "kind": "post-publication-worktree-verification-plan",
                        "expected_head_oid": record["identity"]["commit_oid"],
                        "expected_dirty_paths": [],
                        "tracked_housekeeping_commit_allowed": False,
                        "dependency_receipts": dependency_receipts,
                    },
                )
                row = record["steps"]["housekeeping"]
            if row["plan"].get("dependency_receipts") != dependency_receipts:
                raise RuntimeError("post_publication_dependency_receipt_changed")
            rotation_output = record["steps"]["archive_rotation"][
                "receipt"
            ]["output"]
            recorded_rotations = rotation_output.get("rotations")
            if canonical_digest(recorded_rotations) != rotation_output.get(
                "rotation_set_digest"
            ):
                raise RuntimeError("archive_rotation_final_reproof_mismatch")
            from evolution_infra import validate_archive_rotation_receipts

            if validate_archive_rotation_receipts(
                v,
                recorded_rotations,
                rotation_plan=record["steps"]["archive_rotation"]["plan"],
            ) != recorded_rotations:
                raise RuntimeError("archive_rotation_final_receipt_mismatch")
            log_row = record["steps"]["log_cleanup"]
            _revalidate_strict_log_archives(
                log_row["plan"],
                log_row["receipt"]["output"].get("archives"),
                expected_handoff_version=v,
                expected_publication_id=record["identity"]["publication_id"],
            )
            pool_output = record["steps"]["pool_reap"]["receipt"]["output"]
            _initial_pool, target_names, _selection_snapshot = (
                _validate_pool_reap_plan(
                    record["steps"]["pool_reap"]["plan"],
                    record,
                )
            )
            if (
                pool_output.get("required_reaps") != len(target_names)
                or pool_output.get("removed_bots") != sorted(target_names)
            ):
                raise RuntimeError("pool_reap_final_target_set_mismatch")
            prior_reap_proofs = {
                proof.get("bot"): proof
                for proof in pool_output.get("reap_proofs") or []
                if isinstance(proof, dict)
            }
            final_reap_proofs = []
            for name in target_names:
                proof = _converge_and_verify_reaped_target(name, record)
                prior = prior_reap_proofs.get(name) or {}
                for field in (
                    "version", "completion_commit_oid", "tombstone_tag",
                    "tombstone_object_oid", "tombstone_commit_oid",
                ):
                    if proof.get(field) != prior.get(field):
                        raise RuntimeError("pool_reap_final_reproof_mismatch")
                final_reap_proofs.append(proof)
            housekeeping = _verify_post_publication_worktree(
                expected_head=row["plan"]["expected_head_oid"],
                expected_dirty=set(row["plan"]["expected_dirty_paths"]),
            )
            housekeeping.update({
                "archive_rotation_revalidated": True,
                "strict_log_archives_revalidated": True,
                "reap_proofs": final_reap_proofs,
                "reap_proof_set_digest": canonical_digest(final_reap_proofs),
            })
            record = complete_handoff_step(
                v,
                source_v,
                claim_id,
                "housekeeping",
                {**housekeeping, "plan_digest": row["plan_digest"]},
            )

        completed = complete_post_publication_handoff(v, source_v, claim_id)
        claim_id = ""
        _set_pipeline_status(f"Archived v{v}", is_working=False)
        result = {
            "version": v,
            "source_v": source_v,
            "archivist_completed": True,
            "handoff_identity_digest": completed["identity_digest"],
            "publication_id": completed["identity"]["publication_id"],
            "steps": completed["steps"],
            "next_tool": "prepare_generation",
        }
        # Completion telemetry is deliberately downstream of the durable
        # archive/record linearization.  It has no marker file, is not a
        # required handoff step, and failure cannot reopen the generation.
        try:
            log_system_event(
                "pipeline.archivist_done",
                "success",
                f"Archivist completed required effects for v{v}",
                {
                    "version": v,
                    "source_v": source_v,
                    "publication_id": completed["identity"]["publication_id"],
                    "handoff_identity_digest": completed["identity_digest"],
                },
            )
        except Exception:
            pass
        return result
    except LLMAvailabilityBlocked:
        if claim_id:
            release_post_publication_handoff_claim(
                v, source_v, claim_id, error="llm_availability_blocked"
            )
        raise
    except Exception as exc:
        if claim_id:
            release_post_publication_handoff_claim(
                v,
                source_v,
                claim_id,
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
            )
        return {
            "error": "POST_PUBLICATION_ARCHIVIST_PENDING",
            "version": v,
            "source_v": source_v,
            "archivist_completed": False,
            "checkpoint_cleared_by_archivist": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:500]}",
            "directive": (
                "Repair the required effect and retry run_archivist for the same "
                "durable handoff; do not prepare another generation."
            ),
        }


@tool("run_archivist", "Run the one-shot post-commit consistency/archive audit. Advisory notes stay in the content-bound archive snapshot and never enter prompt evidence directly.", {"version": int, "source_v": int})
async def run_archivist(args):
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)

    return _json_tool_result(
        await _run_durable_post_publication_archivist(v, source_v)
    )


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

    parent_a_evidence_dir = parent_a_dir
    parent_b_evidence_dir = parent_b_dir
    bound_parent_snapshot = None
    stored_crossover_context = (
        ((authoritative_ckpt.get("audit_context") or {}).get("crossover") or {})
        if isinstance(authoritative_ckpt, dict)
        else {}
    )
    stored_compatibility = (
        stored_crossover_context.get("compatibility")
        if isinstance(stored_crossover_context, dict)
        else None
    )
    if authoritative_stage == "crossover_running" and isinstance(
        stored_compatibility, dict
    ):
        try:
            from audit_agents import resolve_crossover_parent_snapshots

            bound_parent_snapshot = resolve_crossover_parent_snapshots(
                stored_compatibility.get("parent_snapshot_receipt"),
                checkpoint=authoritative_ckpt,
                parent_a_v=parent_a,
                parent_b_v=parent_b,
                target_v=target_v,
            )
            parent_a_evidence_dir = bound_parent_snapshot[
                "frozen_parent_a_dir"
            ]
            parent_b_evidence_dir = bound_parent_snapshot[
                "frozen_parent_b_dir"
            ]
        except Exception as exc:
            return _json_tool_result({
                "error": "CROSSOVER_PARENT_SNAPSHOT_RECEIPT_INVALID",
                "success": False,
                "failure_class": "integrity",
                "detail": f"{type(exc).__name__}: {str(exc)[:500]}",
                "target_v": target_v,
            })
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
        parent_a_position_errors = detect_position_semantics_errors(
            parent_a_evidence_dir
        )
        parent_b_position_errors = detect_position_semantics_errors(
            parent_b_evidence_dir
        )
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
        if (parent_a_evidence_dir / "national_bot.py").exists():
            from national_capability_contract import evaluate_national_capabilities
            from runtime_architecture_policy import build_architecture_policy

            parent_a_capabilities = evaluate_national_capabilities(
                parent_a_evidence_dir
            )
            parent_b_capabilities = evaluate_national_capabilities(
                parent_b_evidence_dir
            )
            architecture_policy = build_architecture_policy(
                parent_a_evidence_dir,
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
                from tool_bot_management import (
                    _do_abandon_generation,
                    expected_abandon_identity,
                )

                abandon_result = await _do_abandon_generation(
                    reason=f"crossover_pre_synthesis_artifact_unexpected:v{target_v}",
                    **expected_abandon_identity(read_pipeline_checkpoint()),
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
        candidate_missing = not target_dir.is_dir()
        stage_drift = (active_crossover_ckpt or {}).get("stage") != "crossover_running"
        if candidate_missing or stage_drift:
            # Bug B backstop (v160): the overlay promised a "preserved
            # candidate" retry but no such candidate exists (or the stage
            # drifted). The old fail-closed return caused a no-op re-entry
            # death-loop. Abandon so prepare_generation selects a fresh
            # generation. (crossover_infrastructure_resume_target_missing:
            # matches the crossover_ forced-rule prefix, disposable at
            # selected/crossover_running after Bug A.)
            from tool_bot_management import (
                _do_abandon_generation,
                expected_abandon_identity,
            )
            try:
                abandon_result = await _do_abandon_generation(
                    reason=f"crossover_infrastructure_resume_target_missing:v{target_v}",
                    **expected_abandon_identity(read_pipeline_checkpoint()),
                )
            except Exception as exc:
                abandon_result = {
                    "abandoned": False,
                    "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            try:
                log_system_event(
                    "pipeline.crossover_resume_target_missing_abandoned",
                    "warn" if abandon_result.get("abandoned") else "error",
                    f"Crossover v{target_v} infra overlay could not resume "
                    f"(candidate_missing={candidate_missing}, stage_drift={stage_drift}); "
                    f"{'abandoned' if abandon_result.get('abandoned') else 'abandon did not complete'}.",
                    {
                        "target_v": target_v,
                        "parent_a": parent_a,
                        "parent_b": parent_b,
                        "candidate_missing": candidate_missing,
                        "stage_drift": stage_drift,
                        "stage": (active_crossover_ckpt or {}).get("stage"),
                        "abandon_result": abandon_result,
                    },
                )
            except Exception:
                pass
            return _json_tool_result({
                "error": "CROSSOVER_LLM_EXHAUSTED",
                "success": False,
                "abandoned": bool(abandon_result.get("abandoned")),
                "failure_class": "infrastructure",
                "abandon_reason": "crossover_infrastructure_resume_target_missing",
                "target_v": target_v,
                "directive": (
                    "The crossover infrastructure overlay could not resume its preserved "
                    "candidate (the candidate bytes are gone). The generation was abandoned; "
                    "let prepare_generation select a fresh generation."
                ),
                "abandon_result": abandon_result,
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
                from tool_bot_management import (
                    _do_abandon_generation,
                    expected_abandon_identity,
                )

                abandon_result = await _do_abandon_generation(
                    reason=f"crossover_preserved_child_drift:v{target_v}",
                    **expected_abandon_identity(read_pipeline_checkpoint()),
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
                parent_a_evidence_dir,
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
                authoritative_checkpoint=authoritative_ckpt,
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
        from audit_agents import CrossoverParentSnapshotError

        if isinstance(e, CrossoverParentSnapshotError):
            return _json_tool_result({
                "error": "CROSSOVER_PARENT_SNAPSHOT_CAPTURE_FAILED",
                "success": False,
                "failure_class": "integrity",
                "detail": str(e)[:500],
                "target_v": target_v,
                "provider_called": False,
            })
        _log.warning("Crossover compat audit error (skipping): %s", e)

    if isinstance(compat, dict) and isinstance(
        compat.get("parent_snapshot_receipt"), dict
    ):
        try:
            from audit_agents import (
                frozen_crossover_parent_architecture,
                resolve_crossover_parent_snapshots,
            )

            bound_parent_snapshot = resolve_crossover_parent_snapshots(
                compat["parent_snapshot_receipt"],
                checkpoint=authoritative_ckpt,
                parent_a_v=parent_a,
                parent_b_v=parent_b,
                target_v=target_v,
            )
            frozen_architecture = frozen_crossover_parent_architecture(
                bound_parent_snapshot
            )
            parent_a_evidence_dir = bound_parent_snapshot[
                "frozen_parent_a_dir"
            ]
            parent_b_evidence_dir = bound_parent_snapshot[
                "frozen_parent_b_dir"
            ]
            architecture_policy = frozen_architecture["architecture_policy"]
            capability_context = frozen_architecture["capability_context"]
        except Exception as exc:
            return _json_tool_result({
                "error": "CROSSOVER_PARENT_SNAPSHOT_RECEIPT_INVALID",
                "success": False,
                "failure_class": "integrity",
                "detail": f"{type(exc).__name__}: {str(exc)[:500]}",
                "target_v": target_v,
            })

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

    if isinstance(success, dict) and success.get("outcome") == "synthesis_effect_unrecoverable":
        # Bug B (v160): the effect-id namespace is poisoned (bound to a
        # different input_digest in a prior re-entry). Synthesis can never
        # succeed idempotently, so a "preserved candidate" retry overlay would
        # loop forever: the overlay claims "preserved" but no candidate exists.
        # Abandon directly; the next preparation rebinds a clean effect
        # namespace. (crossover_effect_prepare_conflict: matches the crossover_
        # forced-rule prefix, disposable at selected/crossover_running.)
        issue = success.get("issue")
        try:
            log_system_event(
                "pipeline.crossover_effect_prepare_conflict_unrecoverable",
                "error",
                f"Crossover v{parent_a}xv{parent_b} -> v{target_v} synthesis effect "
                f"namespace is unrecoverable: {issue}",
                {
                    "target_v": target_v,
                    "parent_a": parent_a,
                    "parent_b": parent_b,
                    "component": success.get("component"),
                    "issue": str(issue)[:500],
                },
            )
        except Exception:
            pass
        from tool_bot_management import (
            _do_abandon_generation,
            expected_abandon_identity,
        )
        try:
            abandon_result = await _do_abandon_generation(
                reason=f"crossover_effect_prepare_conflict:v{parent_a}xv{parent_b}",
                **expected_abandon_identity(read_pipeline_checkpoint()),
            )
        except Exception as abandon_exc:
            abandon_result = {
                "abandoned": False,
                "reason": f"{type(abandon_exc).__name__}: {abandon_exc}",
            }
        try:
            log_system_event(
                "pipeline.crossover_effect_prepare_conflict_abandoned",
                "warn" if abandon_result.get("abandoned") else "error",
                f"Crossover v{parent_a}xv{parent_b} effect-id conflict; "
                f"{'abandoned generation' if abandon_result.get('abandoned') else 'abandon did not complete'}.",
                {
                    "target_v": target_v,
                    "parent_a": parent_a,
                    "parent_b": parent_b,
                    "issue": str(issue)[:500],
                    "abandon_result": abandon_result,
                },
            )
        except Exception:
            pass
        return _json_tool_result({
            "error": "CROSSOVER_LLM_EXHAUSTED",
            "success": False,
            "abandoned": bool(abandon_result.get("abandoned")),
            "failure_class": "infrastructure",
            "abandon_reason": "crossover_effect_prepare_conflict",
            "target_v": target_v,
            "parent_a": parent_a,
            "parent_b": parent_b,
            "directive": (
                f"Crossover v{parent_a}xv{parent_b} -> v{target_v} has an unrecoverable "
                "synthesis-effect id conflict (the effect was previously bound to a different "
                "input). The generation was abandoned; let prepare_generation select a fresh generation."
            ),
            "message": f"Crossover v{parent_a}xv{parent_b} effect-id conflict abandoned.",
            "abandon_result": abandon_result,
            "logs": ui.get_output(),
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
                    parent_a_evidence_dir,
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
                    parent_a_evidence_dir,
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
                    parent_a_evidence_dir,
                    target_dir,
                    parent_capabilities=prepared_transition.get(
                        "source_capabilities"
                    ),
                    prepared_capabilities=prepared_transition.get(
                        "candidate_capabilities"
                    ),
                )
                prepared_baseline_contract = build_prepared_baseline_contract(
                    parent_a_evidence_dir,
                    parent_b_evidence_dir,
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
                    parent_a_evidence_dir,
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
            from tool_bot_management import (
                _do_abandon_generation,
                expected_abandon_identity,
            )
            abandon_result = await _do_abandon_generation(
                reason=f"crossover_llm_exhausted:v{parent_a}xv{parent_b}",
                **expected_abandon_identity(read_pipeline_checkpoint()),
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


# ──────────────────────────────────────────────
# Archivist helpers re-export
# ──────────────────────────────────────────────
# The post-publication archivist helpers live in ``tool_commit_archivist``.
# They are re-exported here so external importers (e.g.
# ``post_publication_handoff``) and the test-suite continue to resolve them as
# ``tool_commit.<name>``, and so the main-module entry points that LOAD_GLOBAL
# these names at runtime still find them in this module's namespace.  This
# import must stay at the very bottom of the file: the companion module imports
# ``tool_commit`` itself, so importing it earlier would create a circular
# import.  At this point every ``def`` in this module has executed, so the
# companion's ``import tool_commit as _tc`` binds a fully-populated module
# object.
from tool_commit_archivist import (  # noqa: F401
    _ARCHIVIST_STORAGE_OWNER_LOCK,
    _ARCHIVIST_STORAGE_OWNER_LOCK_PORCELAIN,
    STRICT_LOG_KEEP_GENERATIONS,
    _POOL_REAP_PLAN_KEYS,
    _validated_archivist_storage_owner_lock,
    _git_dirty_paths,
    _verify_post_publication_worktree,
    _durable_archivist_state_write,
    _build_strict_log_cleanup_plan,
    _validate_strict_log_cleanup_plan,
    _read_safe_json,
    _publish_log_tar,
    _validate_log_tar,
    _recover_linked_log_tar,
    _execute_strict_log_cleanup,
    _revalidate_strict_log_archives,
    _handoff_publication_result,
    _build_pool_reap_plan,
    _lower_hex,
    _validate_pool_reap_plan,
    _remote_ref_snapshot,
    _execute_pool_reap_plan,
)
