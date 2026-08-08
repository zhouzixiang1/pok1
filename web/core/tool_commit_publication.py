"""Publication transaction cluster, extracted from tool_commit.

Holds the immutable publication-intent reconciliation path used when resuming
a commit at the publishing stage, plus the durable completed-sentinel
materialization and the local tag/certificate validation helpers.

The parent module (`tool_commit`) keeps thin delegate shells so monkeypatching
`tool_commit._official_certificate_projection` and importing
`tool_commit._resume_publication_transaction` continue to work.
"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

import tool_commit as _tc

from logging_config import get_logger

log = get_logger("tool_commit_publication")


def _existing_local_bot_tag_matches_certificate(version, certificate):
    """Validate a local commit/tag left behind by an interrupted required push."""
    tag = _tc.bot_tag(version)
    if not _tc.git_has_tag(version) or not _tc.git_dir_is_committed(version):
        return False, "local tag or committed bot directory is missing"
    expected = {
        "official-certificate": str(certificate.get("certificate_digest") or ""),
        "official-candidate-hash": str(certificate.get("candidate_hash") or ""),
        "official-policy": str(certificate.get("policy_id") or ""),
    }
    certificate_path = f"official_certificates/{_tc.bot_name(version)}.json"
    from bot_artifact import validate_completion_tag

    validation = validate_completion_tag(
        _tc.get_bot_dir(version),
        expected_metadata=expected,
        certificate_path=certificate_path,
    )
    if not validation.get("valid"):
        return False, ", ".join(validation.get("issues") or [f"invalid {tag}"])
    return True, ""


def _push_existing_bot_refs(version):
    refs = [_tc.EVOLUTION_BRANCH, _tc.bot_tag(version)]
    high_water = _tc.high_water_tag(int(version))
    if _tc._git("tag", "-l", high_water, check=False).strip():
        refs.append(high_water)
    ok = _tc.git_push_refs(*refs)
    _tc.publish_runtime_expected_head("bot_commit_push_retry", version=version)
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
    from publication_transaction import (
        PUBLICATION_INTENT_KIND_STAGING,
        publication_gate_ledger_digest,
        publication_intent_checkpoint_errors,
        publication_intent_live_errors,
    )

    current = _tc.read_pipeline_checkpoint()
    checkpoint_errors = publication_intent_checkpoint_errors(intent, current)
    if checkpoint_errors:
        raise RuntimeError(
            "pre-push publishing checkpoint changed: "
            + "; ".join(checkpoint_errors[:30])
        )
    staging_intent = isinstance(intent, dict) and (
        intent.get("kind") == PUBLICATION_INTENT_KIND_STAGING
    )
    official_status = (
        (((current or {}).get("gate_results") or {}).get("official_full") or {})
        .get("status")
        or {}
    )
    proof = build_pending_local_publication_proof(bot_dir)
    ledger = _tc.validate_commit_gate_ledger(
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
            repo_root=_tc.PROJECT_ROOT,
            official_status=official_status,
            final_gate_ledger_digest=publication_gate_ledger_digest(ledger),
            current_strict_bots=strict_published_bot_names(),
            current_remote_required=_tc.evolution_git_push_required(),
        )
    )
    # Certification system removed: no certificate-bound identity check runs
    # at the pre-push revalidation point.  Every publication uses the staging
    # intent path (no certificate fields).
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
    from publication_transaction import (
        PUBLICATION_INTENT_KIND_STAGING,
        publication_gate_ledger_digest,
        publication_intent_live_errors,
    )

    bot_dir = _tc.get_bot_dir(v)
    intent = (ckpt or {}).get("publication_intent")
    staging_intent = isinstance(intent, dict) and (
        intent.get("kind") == PUBLICATION_INTENT_KIND_STAGING
    )
    official_status = (
        (((ckpt or {}).get("gate_results") or {}).get("official_full") or {})
        .get("status")
        or {}
    )
    official_certificate = _tc._official_certificate_projection(official_status)
    pending_proof = None
    if _tc.git_has_tag(v):
        if staging_intent:
            # Staging completion tags carry staging metadata, not official
            # certificate lines; skip the certificate-bound tag match.
            try:
                pending_proof = build_pending_local_publication_proof(bot_dir)
            except Exception as exc:
                return _publication_pending_result(
                    v,
                    source_v,
                    error="COMMIT BLOCKED: local publication proof is invalid.",
                    reason=f"{type(exc).__name__}: {str(exc)[:300]}",
                )
        else:
            tag_matches, mismatch = _tc._existing_local_bot_tag_matches_certificate(
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
        candidate_remote_proof = _tc.verify_remote_bot_publication(intent)
        if candidate_remote_proof.get("valid") is True:
            linearized_remote_proof = candidate_remote_proof

    if linearized_remote_proof is None:
        ledger = _tc.validate_commit_gate_ledger(
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
        else _tc.evolution_git_push_required()
    )
    try:
        live_errors = publication_intent_live_errors(
            intent,
            checkpoint=ckpt,
            candidate_dir=bot_dir,
            repo_root=_tc.PROJECT_ROOT,
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
    # Certification system removed: no official_full_certified recheck runs
    # after the frozen gate ledger validates.  Publication proceeds without a
    # certificate.

    def pre_push_authority():
        return _revalidate_publication_authority_before_push(
            v,
            source_v,
            intent=intent,
            bot_dir=bot_dir,
        )

    try:
        local_state = _tc.ensure_bot_git_publication(
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
            local_committed=bool(_tc.git_has_tag(v)),
        )

    if remote_required:
        remote_proof = (
            linearized_remote_proof
            or _tc.verify_remote_bot_publication(intent, local_state=local_state)
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
    # Certification system removed: the post-push official_full_certified
    # attestation recheck no longer runs.  The frozen local/remote publication
    # proof above is the sole post-push authority.

    _tc._write_completed_sentinel_durable(bot_dir, intent.get("publication_id"))
    current = _tc.read_pipeline_checkpoint()
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
    # Pin the checkpoint's repo_baseline.head to the publish commit OID.
    # commit_bot writes stage=publishing BEFORE the git commit (tool_commit.py),
    # so the frozen baseline head is the pre-commit HEAD.  The git publish
    # commit then advances HEAD, and because that commit touches
    # bots/national_cloud_v<N>/ (a contract-critical path) the publishing
    # stage's requires_contract_unchanged=True makes any later checkpoint
    # revalidation hard-block with repo_baseline_head_mismatch.  The
    # publishing -> publishing re-write below is NOT a refresh transition
    # (_stage_refreshes_repo_baseline returns False for same-stage), so the
    # CAS writer's default path would leave the stale pre-commit head in
    # place.  Passing bind_repo_baseline_head=commit_oid makes the CAS writer
    # pin repo_baseline.head to the authoritative publish commit the pipeline
    # itself just produced, so the post-publication handoff's crash recovery
    # sees baseline_head == current_head and can resume cleanly.
    commit_oid = local_state.get("commit_oid")
    if isinstance(commit_oid, str) and len(commit_oid) >= 40:
        try:
            post_commit_ckpt = _tc.read_pipeline_checkpoint()
            if isinstance(post_commit_ckpt, dict):
                frozen_rb = post_commit_ckpt.get("repo_baseline") or {}
                if frozen_rb.get("head") != commit_oid:
                    _bind_ok = _tc.write_pipeline_checkpoint(
                        int(post_commit_ckpt["next_v"]),
                        int(post_commit_ckpt["source_v"]),
                        "publishing",
                        publication_intent=post_commit_ckpt.get("publication_intent"),
                        expected_checkpoint_revision=post_commit_ckpt.get("checkpoint_revision"),
                        expected_checkpoint_stage="publishing",
                        expected_workflow_run_id=post_commit_ckpt.get("workflow_run_id"),
                        bind_repo_baseline_head=commit_oid,
                    )
                    if not _bind_ok:
                        # write_pipeline_checkpoint returns False (does not
                        # raise) when the CAS writer rejects the write — e.g. an
                        # allocation-authority guard. The post-commit baseline
                        # pin is load-bearing for crash recovery of the handoff:
                        # without it a crash between here and checkpoint clear
                        # strands the published bot at `publishing` with
                        # repo_baseline_head_mismatch (the recurring v27/v29/
                        # v79/v83/v88/v105 deadlock). Surface a False return as
                        # an explicit error so a failed pin is never invisible.
                        log.error(
                            "post-commit repo_baseline bind CAS returned False for v%s "
                            "(baseline still %s, expected publish commit %s) — recovery will block",
                            post_commit_ckpt.get("next_v"),
                            (frozen_rb.get("head") or "?")[:12],
                            commit_oid[:12],
                        )
        except Exception as exc:
            # Same rationale as above: log so a failure is visible instead of
            # silently degrading; the publication transaction itself is already
            # durable at this point.
            try:
                log.warning(
                    "post-commit repo_baseline bind failed for v%s: %s: %s",
                    post_commit_ckpt.get("next_v") if isinstance(post_commit_ckpt, dict) else "?",
                    type(exc).__name__,
                    str(exc)[:200],
                )
            except Exception:
                pass
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
        with _tc.bot_publication_lock():
            handoff = ensure_post_publication_handoff(
                version=v,
                source_v=source_v,
                publishing_checkpoint=current,
                publication_result=publication_result,
                allow_local_only=allow_local_only,
            )
            cleared = _tc.clear_pipeline_checkpoint(
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
