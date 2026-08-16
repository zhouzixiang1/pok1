"""Pipeline checkpoint CAS internals for evolution_infra.

Extracted as a cohesive business cluster from evolution_infra.py: the repo
baseline capture + HEAD-drift stage logic, the publication-reconciliation /
identity-replan validators, and the three pipeline-checkpoint I/O bodies
(``write_pipeline_checkpoint``, ``read_pipeline_checkpoint``,
``clear_pipeline_checkpoint``) plus their helper validators.

evolution_infra.py retains thin delegate shells so external
``from evolution_infra import <name>`` sites and
``monkeypatch.setattr(evolution_infra, "<name>", ...)`` patches keep resolving.

IMPORTANT -- shared-symbol access model
---------------------------------------
Many names referenced by these bodies remain in ``evolution_infra`` because
they are part of that module's monkeypatch surface -- the test suite patches
``evolution_infra.PIPELINE_STATE_FILE``, ``evolution_infra.RESULTS_DIR``,
``evolution_infra.PROJECT_ROOT``, ``evolution_infra.BOTS_DIR``,
``evolution_infra._atomic_publish_state_text``,
``evolution_infra._locked_state_sidecar``,
``evolution_infra._read_regular_state_text``,
``evolution_infra._preflight_state_sidecar``,
``evolution_infra._fsync_directory``, ``evolution_infra._git``,
``evolution_infra._git_command_succeeds``,
``evolution_infra.get_bot_dir``,
``evolution_infra.checkpoint_allocation_authority``,
``evolution_infra.load_abandoned_version_receipts``,
``evolution_infra._tagged_bot_versions``,
``evolution_infra._abandon_authority_from_receipts``,
``evolution_infra.is_active_bot_protocol_eligible``,
``evolution_infra.load_reaped_bot_versions``,
``evolution_infra.write_pipeline_checkpoint``,
``evolution_infra.read_pipeline_checkpoint``,
``evolution_infra.clear_pipeline_checkpoint`` and reads them back through the
checkpoint code paths.  Binding them at import time would freeze the pre-patch
value and silently break those tests.

Every such reference in this file is written ``_ei.<name>`` so it resolves
against the live module attribute at call time.  References between members of
*this* module (e.g. ``write_pipeline_checkpoint`` calling
``_capture_repo_baseline`` / ``_prune_gate_results_for_stage`` /
``_publication_checkpoint_reconciliation_allowed`` /
``_identity_replan_replacement_contract_errors``) are kept as bare globals,
exactly as they were inline.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import stat
import time
import uuid
from copy import deepcopy
from pathlib import Path

import evolution_infra as _ei  # for _ei.PIPELINE_STATE_FILE, _ei.RESULTS_DIR,
                               # _ei.PROJECT_ROOT, _ei.BOTS_DIR,
                               # _ei._atomic_publish_state_text,
                               # _ei._locked_state_sidecar,
                               # _ei._read_regular_state_text,
                               # _ei._preflight_state_sidecar,
                               # _ei._fsync_directory, _ei._git,
                               # _ei._git_command_succeeds, _ei.get_bot_dir,
                               # _ei.checkpoint_allocation_authority,
                               # _ei.load_abandoned_version_receipts,
                               # _ei._tagged_bot_versions,
                               # _ei._abandon_authority_from_receipts,
                               # _ei.is_active_bot_protocol_eligible,
                               # _ei.load_reaped_bot_versions, log, and the thin
                               # delegate shells that pick up test monkeypatches.

# Immutable constants / pure functions re-exported by evolution_infra.
# Imported here directly so the moved bodies can keep referencing them as bare
# globals, exactly as they did inline.
from bot_namespace import (
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    bot_tag,
    high_water_tag,
)

log = logging.getLogger("pok.infra")


def _state_file_for_slot(slot_id=None):
    """Resolve the per-slot checkpoint file path.

    ``slot_id=None`` resolves to the primary/canonical checkpoint file
    (``pipeline_state.json``) — backward-compatible with all existing callers.
    A non-None ``slot_id`` resolves to ``pipeline_state_<slot_id>.json`` for a
    concurrent generation.  Each slot file has its own sidecar lock (derived
    from the data file path), so per-file-per-slot gets per-slot locking.
    """
    return _ei.pipeline_state_path(slot_id)


def _capture_repo_baseline(stage, *, next_v=None, source_v=None, checkpoint=None):
    """Capture the git baseline persisted with an active generation checkpoint."""
    try:
        from repo_state import git_worktree_snapshot
        snapshot = git_worktree_snapshot()
        contract = _ei.build_evaluation_contract(
            _ei.PROJECT_ROOT,
            candidate_v=next_v,
            source_v=source_v,
            checkpoint=checkpoint,
            stage=stage,
            include_hash=True,
        )
        return {
            "branch": snapshot.get("branch", ""),
            "head": snapshot.get("head", ""),
            "entry_count": snapshot.get("entry_count", 0),
            "dirty_count": snapshot.get("dirty_count", 0),
            "untracked_count": snapshot.get("untracked_count", 0),
            "entries": (snapshot.get("entries") or [])[:40],
            "truncated": bool(snapshot.get("truncated")),
            "evaluation_contract": contract,
            "captured_stage": stage,
            "captured_ts": time.time(),
        }
    except Exception as exc:
        return {
            "branch": "",
            "head": "",
            "entry_count": 0,
            "dirty_count": 0,
            "untracked_count": 0,
            "entries": [],
            "truncated": False,
            "evaluation_contract": {},
            "captured_stage": stage,
            "captured_ts": time.time(),
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


_REPO_BASELINE_VALIDATION_STAGES = frozenset({
    "quality_failed",
    "quality_passed",
    "precommit_failed",
    "verified",
    "official_bootstrap_required",
    "official_certifying",
    "official_failed",
    "official_inconclusive",
    "publishing",
})
_REPO_BASELINE_VALIDATION_GATES = {
    "quality_failed": "quality",
    "quality_passed": "quality",
    "precommit_failed": "precommit_eval",
    "verified": "precommit_eval",
    "official_bootstrap_required": "official_full",
    "official_certifying": "official_full",
    "official_failed": "official_full",
    "official_inconclusive": "official_full",
}
_REPO_BASELINE_PLANNING_STAGES = frozenset({
    "direction_audited",
    "master_planned",
})

def _prune_gate_results_for_stage(stage, gate_results):
    """Drop gate results that no longer validate the current stage/code.

    Gate payloads are evidence for a specific code snapshot. When the pipeline
    regresses to a code-mutating stage and later returns to workers_done, old
    review/critic/precommit evidence must not survive and steer recovery for the
    new code.
    """
    if not isinstance(gate_results, dict):
        return {}
    allowed = _ei.STAGE_GATE_ALLOWLIST.get(stage)
    if allowed is None:
        return dict(gate_results)
    return {name: value for name, value in gate_results.items() if name in allowed}



def _stage_refreshes_repo_baseline(old_stage, new_stage, gate_results=None) -> bool:
    """Return True when a checkpoint stage proves the candidate on this HEAD.

    HEAD-drift recovery can legitimately route a candidate through a hard gate
    after infrastructure changes. Once that gate finishes, the persisted
    baseline must move forward to the HEAD that actually ran the validation;
    otherwise later recovery health checks keep comparing against stale code.

    Pre-worker planning stages also refresh the baseline on stage advance. They
    do not validate candidate strength, but they do bind the next deterministic
    handoff to prompts, guard policy, and route logic from the current HEAD.
    """
    if old_stage != new_stage and new_stage in _REPO_BASELINE_PLANNING_STAGES:
        return True
    if new_stage not in _REPO_BASELINE_VALIDATION_STAGES:
        return False
    if old_stage != new_stage:
        return True
    required_gate = _REPO_BASELINE_VALIDATION_GATES.get(new_stage)
    return bool(required_gate and required_gate in (gate_results or {}))



def _publication_checkpoint_reconciliation_allowed(checkpoint, authority):
    """Recognize only the fully proven intent-bound publication window.

    A missing ``.completed`` is allowed solely because creating that sentinel is
    itself one of the remaining idempotent recovery effects.  If it exists, its
    publication id must match exactly.  Lightweight tags, one-tag-only state,
    wrong-tree refs, invalid certificates, or a mismatched sentinel all fail.
    """

    if not isinstance(checkpoint, dict) or checkpoint.get("stage") != "publishing":
        return False
    target = checkpoint.get("next_v")
    if (
        type(target) is not int
        or int(authority.get("published_high_water") or 0) != target
    ):
        return False
    intent = checkpoint.get("publication_intent")
    try:
        from publication_transaction import publication_intent_structure_errors

        if publication_intent_structure_errors(intent):
            return False
    except Exception:
        return False
    if (
        intent.get("version") != target
        or intent.get("workflow_run_id") != checkpoint.get("workflow_run_id")
        or intent.get("completion_tag") != bot_tag(target)
    ):
        return False
    try:
        commit_oid = _ei._git(
            "rev-parse",
            f"refs/tags/{intent['completion_tag']}^{{commit}}",
            check=False,
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit_oid):
            return False
        _ei._validate_local_publication_refs(intent, commit_oid)
        _ei._validate_existing_publication_commit(intent, commit_oid)

        from national_runtime_authority import build_pending_local_publication_proof

        bot_dir = _ei.get_bot_dir(target)
        proof = build_pending_local_publication_proof(bot_dir)
        if (
            proof.get("version") != target
            or proof.get("artifact_hash") != intent.get("candidate_artifact_hash")
            or proof.get("commit_oid") != commit_oid
            or proof.get("tag") != intent.get("completion_tag")
        ):
            return False
        spec = _ei.resolve_national_bot_spec(
            bot_dir,
            role=_ei.ROLE_PARENT_SOURCE,
            repo_root=_ei.PROJECT_ROOT,
        )
        # Certificate removal (2026-08-08) left the two "no certificate"
        # spellings asymmetric: the resolved spec carries None while legacy
        # publication intents carry the empty string. A raw != there refused
        # the post-publish repo-baseline bind for EVERY cert-less publication
        # (the v27/v29/v79/v83/v88/v105/v186 stuck-publishing deadlock class),
        # stranding a published bot at `publishing` until manual recovery.
        # Normalize both sides: absent/empty/None all mean "no certificate".
        if (
            not spec.eligible
            or (spec.certificate_digest or None)
            != (intent.get("official_certificate_digest") or None)
        ):
            return False
        sentinel = bot_dir / ".completed"
        if os.path.lexists(sentinel):
            metadata = os.lstat(sentinel)
            if not stat.S_ISREG(metadata.st_mode) or sentinel.is_symlink():
                return False
            if sentinel.read_text(encoding="utf-8") != (
                f"publication_id={intent.get('publication_id')}\n"
            ):
                return False
        return True
    except Exception:
        return False



def partial_publication_checkpoint_recovery_allowed(
    checkpoint,
    *,
    namespace_authority,
):
    """Prove the sole one-ref publication crash window without reallocating it."""

    if not isinstance(checkpoint, dict) or checkpoint.get("stage") != "publishing":
        return False
    paired = int(getattr(namespace_authority, "high_water", 0) or 0)
    completion_only = set(
        getattr(namespace_authority, "unpaired_completion_versions", ()) or ()
    )
    high_water_only = set(
        getattr(namespace_authority, "unpaired_high_water_versions", ()) or ()
    )
    occupied = completion_only | high_water_only
    target = checkpoint.get("next_v")
    if (
        type(target) is not int
        or target != paired + 1
        or occupied != {target}
        or target in completion_only.intersection(high_water_only)
    ):
        return False
    try:
        from bot_artifact import hash_path
        from checkpoint_schema import (
            checkpoint_epoch_errors,
            live_checkpoint_allocation_authority_errors,
            live_checkpoint_parent_authority_errors,
            live_policy_epoch_reset_receipt_errors,
        )
        from publication_transaction import publication_intent_structure_errors

        if checkpoint_epoch_errors(checkpoint):
            return False
        if live_checkpoint_parent_authority_errors(
            checkpoint,
            repo_root=_ei.PROJECT_ROOT,
        ):
            return False
        if live_policy_epoch_reset_receipt_errors(
            checkpoint,
            project_root=_ei.PROJECT_ROOT,
        ):
            return False
        receipts = _ei.load_abandoned_version_receipts(
            project_root=_ei.PROJECT_ROOT,
        )
        abandon_authority = _ei._abandon_authority_from_receipts(
            receipts,
            published_high_water=paired,
            retryable_first_strict=(paired < FIRST_STRICT_POLICY_VERSION),
        )
        if target != max(paired, int(abandon_authority["floor"])) + 1:
            return False
        if live_checkpoint_allocation_authority_errors(
            checkpoint,
            published_high_water=paired,
            abandoned_receipt_floor=int(abandon_authority["floor"]),
            abandoned_receipt_head_digest=abandon_authority["head_digest"],
        ):
            return False
        binding = checkpoint.get("epoch_binding") or {}
        if binding.get("published_high_water") != paired:
            return False
        intent = checkpoint.get("publication_intent")
        if publication_intent_structure_errors(intent):
            return False
        if (
            intent.get("version") != target
            or intent.get("workflow_run_id") != checkpoint.get("workflow_run_id")
            or intent.get("completion_tag") != bot_tag(target)
            or intent.get("high_water_tag") != high_water_tag(target)
        ):
            return False
        present_tag = (
            intent["completion_tag"]
            if target in completion_only
            else intent["high_water_tag"]
        )
        ref = f"refs/tags/{present_tag}"
        if _ei._git("cat-file", "-t", ref, check=False).strip() != "tag":
            return False
        commit_oid = _ei._git(
            "rev-parse",
            f"{ref}^{{commit}}",
            check=False,
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit_oid):
            return False
        _ei._validate_existing_publication_commit(intent, commit_oid)
        _ei._validate_publication_certificate_file(intent)
        if hash_path(_ei.get_bot_dir(target)) != intent.get("candidate_artifact_hash"):
            return False
        return True
    except Exception:
        return False


_IDENTITY_REPLAN_PREPARED_CONTRACT_FIELDS = frozenset({
    "schema_version",
    "source_v",
    "next_v",
    "prepared_bot",
    "prepared_artifact_hash",
    "prepared_artifact_manifest",
    "contract_digest",
})
_IDENTITY_REPLAN_RECEIPT_FIELDS = frozenset({
    "schema_version",
    "kind",
    "source_v",
    "next_v",
    "workflow_run_id",
    "checkpoint_preimage_revision",
    "checkpoint_preimage_stage",
    "source_stage",
    "recovery_mode",
    "identity_errors",
    "source_artifact_hash",
    "replaced_artifact_hash",
    "prepared_artifact_hash",
    "prepared_artifact_contract_digest",
    "runtime_manifest_digest",
    "epoch_receipt_digest",
    "runtime_manifest_file_sha256",
    "epoch_receipt_file_sha256",
    "materialization_operation_id",
    "materialization_expected_destination_digest",
    "materialization_receipt_digest",
    "candidate_reset_to_source",
    "target_identity_refreshed",
    "stale_worker_gate_identity_cleared",
    "receipt_digest",
})
_IDENTITY_REPLAN_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def _identity_replan_replacement_contract_errors(
    *,
    replacement,
    next_v,
    source_v,
    workflow_run_id,
    checkpoint_revision,
    checkpoint_stage,
    epoch_binding,
):
    """Validate the closed, subject-bound destructive replan contract.

    A canonical digest proves integrity only; it does not grant mutation
    authority.  This validator binds the replacement to the exact checkpoint
    CAS subject, parent publication identity, prepared manifest, and journaled
    materialization shape before any durable field may be cleared.
    """

    from bot_artifact import canonical_digest

    errors = []
    if not isinstance(replacement, dict):
        return ["identity_replan_replacement_not_object"]
    prepared = replacement.get("prepared_artifact_contract")
    replan = replacement.get("architecture_policy_identity_replan")
    if not isinstance(prepared, dict):
        errors.append("identity_replan_prepared_contract_not_object")
        prepared = {}
    if not isinstance(replan, dict):
        errors.append("identity_replan_receipt_not_object")
        replan = {}
    if set(prepared) != _IDENTITY_REPLAN_PREPARED_CONTRACT_FIELDS:
        errors.append("identity_replan_prepared_contract_fields_mismatch")
    if set(replan) != _IDENTITY_REPLAN_RECEIPT_FIELDS:
        errors.append("identity_replan_receipt_fields_mismatch")

    digest_re = re.compile(r"^[0-9a-f]{64}$")
    prepared_manifest = prepared.get("prepared_artifact_manifest")
    prepared_hash = str(prepared.get("prepared_artifact_hash") or "")
    if prepared.get("schema_version") != 1:
        errors.append("identity_replan_prepared_schema_mismatch")
    if prepared.get("source_v") != int(source_v):
        errors.append("identity_replan_prepared_source_mismatch")
    if prepared.get("next_v") != int(next_v):
        errors.append("identity_replan_prepared_target_mismatch")
    if prepared.get("prepared_bot") != bot_name(next_v):
        errors.append("identity_replan_prepared_bot_mismatch")
    if not digest_re.fullmatch(prepared_hash):
        errors.append("identity_replan_prepared_hash_invalid")
    if (
        not isinstance(prepared_manifest, dict)
        or set(prepared_manifest) != {"schema_version", "artifact_type", "entries"}
        or prepared_manifest.get("artifact_type") != "directory"
        or not isinstance(prepared_manifest.get("entries"), list)
    ):
        errors.append("identity_replan_prepared_manifest_invalid")
    elif prepared_hash != canonical_digest(prepared_manifest):
        errors.append("identity_replan_prepared_manifest_hash_mismatch")
    prepared_unsigned = {
        key: value for key, value in prepared.items() if key != "contract_digest"
    }
    if prepared.get("contract_digest") != canonical_digest(prepared_unsigned):
        errors.append("identity_replan_prepared_contract_digest_mismatch")

    if replan.get("schema_version") != 2:
        errors.append("identity_replan_receipt_schema_mismatch")
    if replan.get("kind") != "single-parent-architecture-policy-identity-replan-v2":
        errors.append("identity_replan_receipt_kind_mismatch")
    if replan.get("source_v") != int(source_v):
        errors.append("identity_replan_receipt_source_mismatch")
    if replan.get("next_v") != int(next_v):
        errors.append("identity_replan_receipt_target_mismatch")
    if replan.get("workflow_run_id") != str(workflow_run_id):
        errors.append("identity_replan_receipt_workflow_mismatch")
    if replan.get("checkpoint_preimage_revision") != int(checkpoint_revision):
        errors.append("identity_replan_receipt_revision_mismatch")
    if replan.get("checkpoint_preimage_stage") != str(checkpoint_stage):
        errors.append("identity_replan_receipt_stage_mismatch")
    if replan.get("source_stage") not in {
        "quality_failed",
        "repair_planned",
        "rework_running",
    }:
        errors.append("identity_replan_receipt_source_stage_invalid")
    expected_mode = (
        "legacy_parent_copy_recovery"
        if checkpoint_stage == "direction_audited"
        else "quality_identity_replan"
    )
    if replan.get("recovery_mode") != expected_mode:
        errors.append("identity_replan_receipt_recovery_mode_mismatch")
    identity_errors = replan.get("identity_errors")
    if (
        not isinstance(identity_errors, list)
        or not identity_errors
        or any(not isinstance(item, str) or not item for item in identity_errors)
    ):
        errors.append("identity_replan_receipt_identity_errors_invalid")
    for field in (
        "source_artifact_hash",
        "replaced_artifact_hash",
        "prepared_artifact_hash",
        "prepared_artifact_contract_digest",
        "runtime_manifest_digest",
        "epoch_receipt_digest",
        "runtime_manifest_file_sha256",
        "epoch_receipt_file_sha256",
        "materialization_expected_destination_digest",
        "materialization_receipt_digest",
    ):
        if not digest_re.fullmatch(str(replan.get(field) or "")):
            errors.append(f"identity_replan_receipt_{field}_invalid")
    if not _IDENTITY_REPLAN_OPERATION_ID_RE.fullmatch(
        str(replan.get("materialization_operation_id") or "")
    ):
        errors.append("identity_replan_receipt_operation_id_invalid")
    if replan.get("materialization_expected_destination_digest") != replan.get(
        "replaced_artifact_hash"
    ):
        errors.append("identity_replan_materialization_preimage_mismatch")
    if replan.get("prepared_artifact_hash") != prepared_hash:
        errors.append("identity_replan_receipt_prepared_hash_mismatch")
    if replan.get("prepared_artifact_contract_digest") != prepared.get(
        "contract_digest"
    ):
        errors.append("identity_replan_receipt_prepared_contract_mismatch")
    for field in (
        "candidate_reset_to_source",
        "target_identity_refreshed",
        "stale_worker_gate_identity_cleared",
    ):
        if replan.get(field) is not True:
            errors.append(f"identity_replan_receipt_{field}_not_true")

    entries = (
        prepared_manifest.get("entries")
        if isinstance(prepared_manifest, dict)
        else []
    )
    files = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in entries or []
        if isinstance(item, dict) and item.get("type") == "file"
    }
    if files.get("national_runtime_manifest.json") != replan.get(
        "runtime_manifest_file_sha256"
    ):
        errors.append("identity_replan_runtime_manifest_binding_mismatch")
    if files.get("policy_epoch_receipt.json") != replan.get(
        "epoch_receipt_file_sha256"
    ):
        errors.append("identity_replan_epoch_receipt_binding_mismatch")

    parent_identities = (
        (epoch_binding or {}).get("published_parent_identities") or []
    )
    source_bindings = [
        item
        for item in parent_identities
        if isinstance(item, dict) and item.get("version") == int(source_v)
    ]
    if (
        len(source_bindings) != 1
        or source_bindings[0].get("tag_artifact_hash")
        != replan.get("source_artifact_hash")
    ):
        errors.append("identity_replan_source_publication_binding_mismatch")

    replan_unsigned = {
        key: value for key, value in replan.items() if key != "receipt_digest"
    }
    if replan.get("receipt_digest") != canonical_digest(replan_unsigned):
        errors.append("identity_replan_receipt_digest_mismatch")
    return list(dict.fromkeys(errors))


def _identity_replan_live_materialization_errors(
    replacement,
    *,
    candidate_dir=None,
    artifact_root=None,
):
    """Cross-bind one closed receipt to live bytes and durable CAS evidence."""

    from bot_artifact import hash_path
    from worker_workflow import WorkerArtifactStore

    if not isinstance(replacement, dict):
        return ["identity_replan_replacement_not_object"]
    prepared = replacement.get("prepared_artifact_contract") or {}
    replan = replacement.get("architecture_policy_identity_replan") or {}
    next_v = replan.get("next_v")
    try:
        target = (
            Path(candidate_dir)
            if candidate_dir is not None
            else _ei.BOTS_DIR / bot_name(int(next_v))
        )
        prepared_hash = str(prepared.get("prepared_artifact_hash") or "")
        if hash_path(target) != prepared_hash:
            return ["identity_replan_live_candidate_hash_mismatch"]
        WorkerArtifactStore(
            Path(artifact_root)
            if artifact_root is not None
            else _ei.RESULTS_DIR / "workflow" / "artifacts"
        ).verify_materialization_receipt(
            str(replan.get("materialization_operation_id") or ""),
            destination=target,
            digest=prepared_hash,
            expected_destination_digest=str(
                replan.get("materialization_expected_destination_digest") or ""
            ),
            receipt_digest=str(
                replan.get("materialization_receipt_digest") or ""
            ),
        )
    except Exception as exc:
        return [
            "identity_replan_materialization_receipt_invalid:"
            + type(exc).__name__
        ]
    return []


def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None,
                               reviewer_feedback="", generation_attempt=0,
                               gate_results=None, worker_failure_count=None,
                               worker_invocation_count=None,
                               parent2_v=None, direction_audit=None,
                               audit_context=None, reset_generation_attempt=False,
                               replace_audit_context=False,
                               audit_context_replacement_reason=None,
                               audit_attempt=None, reset_audit_attempt=False,
                               precommit_attempt=None, reset_precommit_attempt=False,
                               precommit_rework_count=None,
                               official_rework_count=None,
                               timeout_extensions=None, touch_stage_timestamp=False,
                               literature_probe=None, prepare_scope_files=None,
                               clear_reviewer_feedback=False,
                               infra_failure=None, clear_infra_failure=False,
                               infra_failure_owner=None,
                               expected_infra_failure_digest=None,
                               official_job=None, clear_official_job=False,
                               expected_official_job_id=None,
                               repair_baseline_artifact_hash=None,
                               clear_repair_baseline_artifact_hash=False,
                               reset_runtime_contract_ledger=False,
                               expected_runtime_contract_ledger_digest=None,
                               runtime_contract_ledger_reset_reason=None,
                               publication_intent=None,
                               expected_checkpoint_revision=None,
                               expected_checkpoint_stage=None,
                               expected_workflow_run_id=None,
                               workflow_run_id=None,
                               terminal_gate_outcome=None,
                               review_attempt_journal=None,
                               identity_replan_history=None,
                               candidate_artifact_hash=None,
                               candidate_manifest_digest=None,
                               charter_digest=None,
                               publication_tier=None,
                               slot_id=None,
                               bind_repo_baseline_head=None):
    """Write pipeline stage checkpoint so a killed process can resume.

    ``bind_repo_baseline_head`` is an explicit override that pins the frozen
    ``repo_baseline.head`` to a caller-supplied commit OID, bypassing the
    ``_stage_refreshes_repo_baseline`` stage predicate. It exists for the one
    case where the pipeline itself advances HEAD *inside* a stage transition
    that the predicate does not recognize as a refresh point: the publication
    transaction commits ``stage=publishing`` *before* the git commit, then
    re-writes the same stage *after* the commit. Because ``publishing ->
    publishing`` is not a refresh transition (``publishing`` is absent from
    ``_REPO_BASELINE_VALIDATION_GATES``), the predicate returns False and the
    baseline stays pinned to the pre-commit HEAD — which then hard-blocks the
    post-publication handoff's crash recovery with
    ``repo_baseline_head_mismatch``. The post-commit refresh passes the publish
    commit OID here so the baseline follows HEAD to the commit the pipeline
    itself just produced.

    Uses atomic tmp+rename under exclusive lock to prevent concurrent
    read-merge-write races (POSIX guarantees os.replace is atomic). Runtime
    contract ledgers remain append-only unless a state-machine-authorized plan
    rejection supplies an explicit reset reason and expected ledger digest.
    """
    from workflow_profiles import get_workflow_profile

    _profile = get_workflow_profile()
    current_workflow_profile_id = getattr(_profile, "profile_id", "")
    current_national_execution_mode = getattr(
        _profile, "national_execution_mode", "native_tcp"
    )

    # Lock a stable sidecar inode. Locking the state file itself is unsafe
    # because os.replace swaps that inode while waiters may still hold an open
    # descriptor to the retired file and later overwrite a newer projection.
    state_file = _state_file_for_slot(slot_id)
    # Draft shadow identity: a draft slot is not a live allocation claim.
    # Detect both an explicit draft slot_id (draft / draft1 / draft2 / ...) and
    # the ambient override used by stage handlers that omit slot_id while
    # running under active_slot_override.  Multi-ahead generalizes the former
    # literal ``== "draft"`` to a prefix match on the draft slot set.
    try:
        _resolved_slot = slot_id
        if _resolved_slot is None:
            _resolved_slot = _ei.current_slot_override()
    except Exception:
        _resolved_slot = slot_id
    is_draft_slot = _ei.is_draft_slot(_resolved_slot)
    try:
        _ei._preflight_state_sidecar(state_file)
    except OSError as exc:
        log.error("Checkpoint sidecar path is unsafe: %s", exc)
        return False
    with _ei._locked_state_sidecar(state_file, lock_type=fcntl.LOCK_EX):
        try:
            raw = _ei._read_regular_state_text(
                state_file,
                allow_missing=True,
            )
        except (OSError, UnicodeError) as exc:
            log.error("Checkpoint path is unsafe or unreadable: %s", exc)
            return False
        existing = None
        if raw.strip():
            try:
                existing = json.loads(raw)
            except Exception as exc:
                log.error(
                    "Refusing to overwrite non-empty corrupt pipeline checkpoint: %s",
                    exc,
                )
                return False
            if not isinstance(existing, dict):
                log.error("Refusing non-object pipeline checkpoint")
                return False
        try:
            # Draft shadow checkpoints skip the live floor+1 successor CAS;
            # promotion remaps them onto the formal next_v later. The
            # post-publish repo_baseline bind (bind_repo_baseline_head) likewise
            # writes AFTER the publish commit has already advanced the high-water
            # tag, so next_v is no longer allocation_floor+1 at that point; the
            # existing-checkpoint publication_reconciliation path below validates
            # the real invariant instead.
            allocation_authority = _ei.checkpoint_allocation_authority(
                expected_next_v=None if (is_draft_slot or bind_repo_baseline_head) else next_v,
            )
        except Exception as exc:
            log.error(
                "Checkpoint allocation authority is unavailable: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False
        if isinstance(existing, dict):
            try:
                from pipeline_infrastructure import normalize_checkpoint_infrastructure

                existing = normalize_checkpoint_infrastructure(existing)
            except Exception as exc:
                log.error("Checkpoint infrastructure normalization failed closed: %s", exc)
                return False
            active_stage = existing.get("stage")
            if active_stage not in {
                None,
                "archived",
                "abandoned",
            }:
                from checkpoint_schema import (
                    checkpoint_epoch_errors,
                    live_checkpoint_allocation_authority_errors,
                    live_checkpoint_parent_authority_errors,
                )

                epoch_errors = checkpoint_epoch_errors(existing)
                if not epoch_errors:
                    epoch_errors.extend(
                        live_checkpoint_parent_authority_errors(
                            existing,
                            repo_root=_ei.PROJECT_ROOT,
                        )
                    )
                if not epoch_errors:
                    epoch_errors.extend(
                        live_checkpoint_allocation_authority_errors(
                            existing,
                            published_high_water=allocation_authority[
                                "published_high_water"
                            ],
                            abandoned_receipt_floor=allocation_authority[
                                "abandoned_receipt_floor"
                            ],
                            abandoned_receipt_head_digest=allocation_authority[
                                "abandoned_receipt_head_digest"
                            ],
                            allow_published_target_reconciliation=(
                                _publication_checkpoint_reconciliation_allowed(
                                    existing,
                                    allocation_authority,
                                )
                            ),
                        )
                    )
                if epoch_errors:
                    log.error(
                        "Refusing implicit epoch/schema upgrade of active "
                        "checkpoint at %s; operator archive/reset is required: %s",
                        active_stage,
                        epoch_errors,
                    )
                    return False
            try:
                active_revision = int(existing.get("checkpoint_revision") or 0)
            except (TypeError, ValueError):
                active_revision = -1
            if active_stage not in {
                None,
                "archived",
                "abandoned",
            } and (
                not str(existing.get("workflow_run_id") or "").strip()
                or active_revision < 1
            ):
                log.error(
                    "Refusing implicit upgrade of active legacy checkpoint at %s; "
                    "central abandon is required",
                    active_stage,
                )
                return False

        if expected_checkpoint_revision is not None:
            current_revision = (
                int(existing.get("checkpoint_revision") or 0)
                if isinstance(existing, dict)
                else 0
            )
            if current_revision != int(expected_checkpoint_revision):
                log.warning(
                    "Checkpoint revision compare-and-swap rejected: expected=%s current=%s",
                    expected_checkpoint_revision,
                    current_revision,
                )
                return False
        if expected_checkpoint_stage is not None and (
            not isinstance(existing, dict)
            or str(existing.get("stage") or "") != str(expected_checkpoint_stage)
        ):
            log.warning(
                "Checkpoint stage compare-and-swap rejected: expected=%s current=%s",
                expected_checkpoint_stage,
                existing.get("stage") if isinstance(existing, dict) else None,
            )
            return False
        if expected_workflow_run_id is not None and (
            not isinstance(existing, dict)
            or str(
                existing.get("workflow_run_id")
                or existing.get("run_id")
                or (
                    f"{int(existing.get('next_v'))}#"
                    f"{int(existing.get('generation_attempt') or 0)}"
                )
            ) != str(expected_workflow_run_id)
        ):
            log.warning("Checkpoint workflow identity compare-and-swap rejected")
            return False

        # Merge with existing — preserve gate_results, master_plan, etc.
        existing_gate_results = {}
        existing_failure_count = 0
        existing_master_plan = master_plan
        existing_reviewer_feedback = reviewer_feedback
        existing_generation_attempt = generation_attempt
        existing_audit_attempt = audit_attempt
        existing_parent2_v = parent2_v
        existing_direction_audit = None
        existing_audit_context = {}
        existing_precommit_attempt = precommit_attempt
        existing_precommit_rework_count = precommit_rework_count
        existing_official_rework_count = official_rework_count
        existing_timeout_extensions = 0
        existing_literature_probe = None
        existing_repo_baseline = None
        existing_prepare_scope_files = []
        existing_runtime_contract_ledger = None
        existing_infra_failure = None
        existing_official_job = None
        existing_repair_baseline_artifact_hash = None
        existing_publication_intent = None
        existing_terminal_gate_outcome = None
        existing_review_attempt_journal = []
        existing_identity_replan_history = []
        existing_candidate_artifact_hash = None
        existing_candidate_manifest_digest = None
        existing_charter_digest = None
        existing_publication_tier = None
        existing_is_draft = bool(is_draft_slot)
        existing_epoch_binding = None
        existing_workflow_run_id = ""
        requested_workflow_run_id = str(workflow_run_id or "").strip()
        existing_checkpoint_revision = 0

        if existing and existing.get("next_v") == next_v and existing.get("source_v") == source_v:
            existing_gate_results = existing.get("gate_results", {}) or {}
            existing_failure_count = existing.get("worker_failure_count", 0)
            existing_timeout_extensions = existing.get("timeout_extensions", 0)
            if master_plan is None:
                existing_master_plan = existing.get("master_plan")
            if clear_reviewer_feedback:
                existing_reviewer_feedback = ""
            elif not reviewer_feedback:
                existing_reviewer_feedback = existing.get("reviewer_feedback", "")
            if generation_attempt == 0:
                existing_generation_attempt = existing.get("generation_attempt", 0)
            if audit_attempt is None:
                existing_audit_attempt = existing.get("audit_attempt", 0)
            if precommit_attempt is None:
                existing_precommit_attempt = existing.get("precommit_attempt", 0)
            if precommit_rework_count is None:
                existing_precommit_rework_count = existing.get("precommit_rework_count", 0)
            if official_rework_count is None:
                existing_official_rework_count = existing.get("official_rework_count", 0)
            if timeout_extensions is not None:
                existing_timeout_extensions = int(timeout_extensions)
            if parent2_v is None:
                existing_parent2_v = existing.get("parent2_v")
            existing_direction_audit = existing.get("direction_audit")
            existing_audit_context = existing.get("audit_context", {}) or {}
            existing_literature_probe = existing.get("literature_probe")
            existing_repo_baseline = existing.get("repo_baseline")
            existing_prepare_scope_files = [
                str(item).strip()
                for item in existing.get("prepare_scope_files", []) or []
                if str(item).strip()
            ]
            existing_runtime_contract_ledger = existing.get("runtime_contract_ledger")
            if existing_runtime_contract_ledger is None:
                legacy_master_plan = existing.get("master_plan")
                if isinstance(legacy_master_plan, dict):
                    existing_runtime_contract_ledger = legacy_master_plan.get(
                        "runtime_contract_ledger"
                    )
            existing_infra_failure = existing.get("infra_failure")
            existing_official_job = existing.get("official_job")
            existing_repair_baseline_artifact_hash = existing.get(
                "repair_baseline_artifact_hash"
            )
            existing_candidate_artifact_hash = existing.get(
                "candidate_artifact_hash"
            )
            existing_candidate_manifest_digest = existing.get(
                "candidate_manifest_digest"
            )
            existing_charter_digest = existing.get("charter_digest")
            existing_publication_intent = existing.get("publication_intent")
            existing_terminal_gate_outcome = existing.get(
                "terminal_gate_outcome"
            )
            # Preserve publication_tier across generic merges unless the caller
            # supplies an explicit replacement.
            if publication_tier is None:
                existing_publication_tier = existing.get("publication_tier")
            else:
                existing_publication_tier = publication_tier
            # Draft shadow flag is sticky for the draft slot; primary writes
            # never inherit it.
            if is_draft_slot:
                existing_is_draft = True
            elif existing.get("is_draft") is True and not is_draft_slot:
                # Promoting into primary clears the shadow flag at write time
                # (caller writes without slot_id / draft override).
                existing_is_draft = False
            existing_review_attempt_journal = deepcopy(
                existing.get("review_attempt_journal") or []
            )
            if identity_replan_history is None:
                existing_identity_replan_history = [
                    str(item)
                    for item in (existing.get("identity_replan_history") or [])
                    if isinstance(item, str)
                ]
            else:
                existing_identity_replan_history = [
                    str(item) for item in identity_replan_history if isinstance(item, str)
                ]
            existing_epoch_binding = existing.get("epoch_binding")
            existing_workflow_run_id = str(
                existing.get("workflow_run_id")
                or expected_workflow_run_id
                or ""
            )
            if (
                requested_workflow_run_id
                and existing_workflow_run_id
                and requested_workflow_run_id != existing_workflow_run_id
            ):
                log.error("Refusing checkpoint workflow identity replacement")
                return False
            existing_checkpoint_revision = int(
                existing.get("checkpoint_revision") or 0
            )
        elif existing:
            active_stage = existing.get("stage")
            dead_stages = {None, "archived", "abandoned"}
            if active_stage not in dead_stages:
                log.warning(
                    "Refusing checkpoint identity mismatch: active v%s/source v%s stage=%s, attempted v%s/source v%s stage=%s",
                    existing.get("next_v"), existing.get("source_v"), active_stage,
                    next_v, source_v, stage,
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.identity_mismatch_blocked", "error",
                        f"Blocked checkpoint identity mismatch: active v{existing.get('next_v')} "
                        f"from v{existing.get('source_v')} at {active_stage}; attempted v{next_v} from v{source_v}",
                        {"ckpt_next_v": existing.get("next_v"),
                         "ckpt_source_v": existing.get("source_v"),
                         "ckpt_stage": active_stage,
                         "args_next_v": next_v,
                         "args_source_v": source_v,
                         "args_stage": stage},
                    )
                except Exception:
                    pass
                return False

        # Explicit reset: a newly accepted Master plan starts a fresh durable
        # generation-attempt identity. Critic verdicts are advisory and do not
        # increment this counter or authorize Worker rework.
        if reset_generation_attempt:
            existing_generation_attempt = 0
        if reset_audit_attempt:
            existing_audit_attempt = 0
        if reset_precommit_attempt:
            existing_precommit_attempt = 0
        if timeout_extensions is not None:
            existing_timeout_extensions = int(timeout_extensions)

        old_stage_for_replacement = (
            existing.get("stage") if isinstance(existing, dict) else None
        )
        identity_replan_replacement = bool(
            audit_context_replacement_reason
            == "architecture_policy_identity_replan"
            and stage == "direction_audited"
            and old_stage_for_replacement in {
                "quality_failed",
                "repair_planned",
                "rework_running",
                "direction_audited",
            }
        )
        destructive_identity_reset = bool(
            replace_audit_context or clear_repair_baseline_artifact_hash
        )
        if destructive_identity_reset:
            if not identity_replan_replacement:
                log.error(
                    "Checkpoint destructive field reset is not an authorized "
                    "architecture policy identity replan"
                )
                return False
            replacement = audit_context if isinstance(audit_context, dict) else {}
            replan = replacement.get("architecture_policy_identity_replan")
            prepared = replacement.get("prepared_artifact_contract")
            stale_keys = {
                "strict_policy_identity_refresh",
                "durable_worker_output",
                "durable_worker_failure",
                "worker_execution_failed_replan",
                "quality_native_match_timing_plan",
                "quality_native_match_timing_plan_digest",
                "precommit_eval_plan",
            }
            try:
                explicit_cas = bool(
                    isinstance(expected_checkpoint_revision, int)
                    and not isinstance(expected_checkpoint_revision, bool)
                    and expected_checkpoint_revision > 0
                    and expected_checkpoint_revision
                    == existing_checkpoint_revision
                    and isinstance(expected_checkpoint_stage, str)
                    and bool(expected_checkpoint_stage.strip())
                    and expected_checkpoint_stage == old_stage_for_replacement
                    and isinstance(expected_workflow_run_id, str)
                    and bool(expected_workflow_run_id.strip())
                    and expected_workflow_run_id == existing_workflow_run_id
                )
                replacement_errors = (
                    _identity_replan_replacement_contract_errors(
                        replacement=replacement,
                        next_v=next_v,
                        source_v=source_v,
                        workflow_run_id=existing_workflow_run_id,
                        checkpoint_revision=existing_checkpoint_revision,
                        checkpoint_stage=old_stage_for_replacement,
                        epoch_binding=existing_epoch_binding,
                    )
                )
                if not replacement_errors:
                    replacement_errors.extend(
                        _identity_replan_live_materialization_errors(
                            replacement,
                            candidate_dir=_ei.BOTS_DIR / bot_name(next_v),
                            artifact_root=(
                                _ei.RESULTS_DIR / "workflow" / "artifacts"
                            ),
                        )
                    )
                replacement_contract_valid = bool(
                    explicit_cas
                    and replace_audit_context
                    and clear_repair_baseline_artifact_hash
                    and master_plan == {}
                    and not replacement_errors
                    and not stale_keys.intersection(replacement)
                    and existing_parent2_v is None
                    and existing_publication_intent is None
                    and existing_official_job is None
                    and existing_infra_failure is None
                )
            except Exception:
                replacement_contract_valid = False
            if not replacement_contract_valid:
                log.error(
                    "Checkpoint architecture identity replan replacement "
                    "contract is invalid: %s",
                    replacement_errors if 'replacement_errors' in locals() else [],
                )
                return False

        if gate_results:
            existing_gate_results.update(gate_results)
        if review_attempt_journal is not None:
            if not isinstance(review_attempt_journal, list):
                log.error("Invalid Reviewer attempt journal projection")
                return False
            # The caller supplies the complete append-only projection.  Never
            # accept truncation or mutation of an already durable prefix.
            if (
                len(review_attempt_journal) < len(existing_review_attempt_journal)
                or review_attempt_journal[: len(existing_review_attempt_journal)]
                != existing_review_attempt_journal
            ):
                log.error("Refusing Reviewer attempt journal rewrite")
                return False
            existing_review_attempt_journal = deepcopy(review_attempt_journal)
        existing_gate_results = _prune_gate_results_for_stage(stage, existing_gate_results)
        if worker_failure_count is not None:
            existing_failure_count = worker_failure_count
        elif worker_invocation_count is not None:
            existing_failure_count = worker_invocation_count
        if direction_audit is not None:
            existing_direction_audit = direction_audit
        if replace_audit_context:
            if not isinstance(audit_context, dict):
                log.error("Audit context replacement requires an object")
                return False
            existing_audit_context = deepcopy(audit_context)
        elif audit_context is not None:
            existing_audit_context.update(audit_context)
        if existing_epoch_binding is None:
            try:
                from checkpoint_schema import build_checkpoint_epoch_binding

                existing_epoch_binding = build_checkpoint_epoch_binding(
                    next_v=next_v,
                    source_v=source_v,
                    parent2_v=existing_parent2_v,
                    audit_context=existing_audit_context,
                    published_high_water=allocation_authority[
                        "published_high_water"
                    ],
                    abandoned_receipt_floor=allocation_authority[
                        "abandoned_receipt_floor"
                    ],
                    abandoned_receipt_head_digest=allocation_authority[
                        "abandoned_receipt_head_digest"
                    ],
                    repo_root=_ei.PROJECT_ROOT,
                    allow_shadow_allocation=is_draft_slot,
                )
            except Exception as exc:
                errors = list(getattr(exc, "errors", ()) or ())
                log.error(
                    "Refusing checkpoint without a valid strict epoch binding: %s",
                    errors or f"{type(exc).__name__}: {exc}",
                )
                return False
        if literature_probe is not None:
            existing_literature_probe = literature_probe
        if prepare_scope_files is not None:
            existing_prepare_scope_files = sorted({
                *existing_prepare_scope_files,
                *(
                    str(item).strip()
                    for item in prepare_scope_files
                    if str(item).strip()
                ),
            })
        if clear_repair_baseline_artifact_hash:
            if repair_baseline_artifact_hash is not None:
                log.error(
                    "Repair baseline clear cannot carry a replacement hash"
                )
                return False
            existing_repair_baseline_artifact_hash = None
        elif repair_baseline_artifact_hash is not None:
            repair_hash = str(repair_baseline_artifact_hash).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", repair_hash):
                log.error("Invalid repair baseline artifact hash")
                return False
            existing_repair_baseline_artifact_hash = repair_hash

        # Slice 2b sealed-candidate identity digests.  These are content-bound
        # sha256 projections written at workers_done so the producer-consumer
        # seal seam can freeze an opaque draft for background gate validation.
        # Like repair_baseline_artifact_hash they are preserved across generic
        # checkpoint rewrites; a None argument leaves the existing value intact.
        if candidate_artifact_hash is not None:
            _h = str(candidate_artifact_hash).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", _h):
                log.error("Invalid candidate_artifact_hash digest")
                return False
            existing_candidate_artifact_hash = _h
        if candidate_manifest_digest is not None:
            _m = str(candidate_manifest_digest).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", _m):
                log.error("Invalid candidate_manifest_digest digest")
                return False
            existing_candidate_manifest_digest = _m
        if charter_digest is not None:
            _c = str(charter_digest).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", _c):
                log.error("Invalid charter_digest digest")
                return False
            existing_charter_digest = _c

        # publication_tier: explicit arg wins; otherwise preserve existing; else
        # leave None (caller/selected write sites supply the default).
        if publication_tier is not None:
            tier = str(publication_tier).strip().lower()
            if tier not in {"native", "staging", "certified"}:
                log.error("Invalid publication_tier: %r", publication_tier)
                return False
            existing_publication_tier = tier
        if is_draft_slot:
            existing_is_draft = True

        # Publication is a one-way, immutable transaction.  Persist its intent
        # under the same checkpoint CAS before any Git mutation, then preserve
        # the exact object through every recovery attempt.  A generic checkpoint
        # rewrite must never replace or silently drop it.
        if publication_intent is not None:
            try:
                from publication_transaction import (
                    publication_intent_structure_errors,
                )

                publication_errors = publication_intent_structure_errors(
                    publication_intent
                )
            except Exception as exc:
                log.error(
                    "Publication intent validation failed closed: %s", exc
                )
                return False
            if publication_errors:
                log.error(
                    "Refusing invalid publication intent: %s",
                    publication_errors,
                )
                return False
            if stage != "publishing":
                log.error("Publication intent requires the publishing stage")
                return False
            if existing_publication_intent is not None:
                if existing_publication_intent != publication_intent:
                    log.error("Refusing publication intent replacement")
                    return False
            else:
                old_stage = existing.get("stage") if isinstance(existing, dict) else ""
                if publication_intent.get("origin_checkpoint_stage") != old_stage:
                    log.error("Publication intent origin stage mismatch")
                    return False
                if int(publication_intent.get("origin_checkpoint_revision") or 0) != int(
                    existing_checkpoint_revision
                ):
                    log.error("Publication intent origin revision mismatch")
                    return False
                if str(publication_intent.get("workflow_run_id") or "") != str(
                    existing_workflow_run_id
                ):
                    log.error("Publication intent workflow identity mismatch")
                    return False
                existing_publication_intent = dict(publication_intent)
        if existing_publication_intent is not None and stage != "publishing":
            log.error("A live publication intent cannot leave the publishing stage")
            return False
        if stage == "publishing" and existing_publication_intent is None:
            log.error("Publishing stage requires an immutable publication intent")
            return False

        terminal_stages = {
            "quality_rejected", "review_rejected", "critic_rejected",
        }
        if terminal_gate_outcome is not None:
            if stage not in terminal_stages:
                log.error("Terminal gate outcome requires a terminal gate stage")
                return False
            if existing_terminal_gate_outcome is not None and (
                existing_terminal_gate_outcome != terminal_gate_outcome
            ):
                log.error("Refusing terminal gate outcome replacement")
                return False
            existing_terminal_gate_outcome = deepcopy(terminal_gate_outcome)
        if stage in terminal_stages and existing_terminal_gate_outcome is None:
            log.error("Terminal gate stage requires an immutable outcome")
            return False
        if existing_terminal_gate_outcome is not None and stage not in terminal_stages:
            log.error("A terminal gate outcome cannot leave its terminal stage")
            return False

        if infra_failure is not None or clear_infra_failure:
            from pipeline_infrastructure import infrastructure_failure_digest

            current_infra_digest = infrastructure_failure_digest(existing_infra_failure)
            if expected_infra_failure_digest is None:
                log.error("Infrastructure overlay mutation requires an expected digest")
                return False
            if str(expected_infra_failure_digest) != current_infra_digest:
                log.warning(
                    "Infrastructure overlay compare-and-swap rejected: expected=%s current=%s",
                    expected_infra_failure_digest,
                    current_infra_digest,
                )
                return False
        if clear_infra_failure:
            if not isinstance(existing_infra_failure, dict):
                log.error("Refusing to clear absent infrastructure overlay")
                return False
            if not infra_failure_owner or existing_infra_failure.get("owner_tool") != infra_failure_owner:
                log.error(
                    "Refusing infrastructure clear by %s; owner is %s",
                    infra_failure_owner,
                    existing_infra_failure.get("owner_tool"),
                )
                return False
            existing_infra_failure = None
        elif infra_failure is not None:
            try:
                from pipeline_infrastructure import validate_infrastructure_failure

                infra_errors = validate_infrastructure_failure(infra_failure)
                if infra_errors:
                    log.error("Refusing invalid infrastructure overlay: %s", infra_errors)
                    return False
                existing_infra_failure = dict(infra_failure)
            except Exception as exc:
                log.error("Infrastructure overlay validation failed closed: %s", exc)
                return False
        if isinstance(existing_infra_failure, dict):
            resume_stage = str(existing_infra_failure.get("resume_stage") or "")
            if stage != resume_stage:
                log.error(
                    "Refusing checkpoint stage %s while infrastructure recovery is bound to %s",
                    stage,
                    resume_stage,
                )
                return False

        if official_job is not None or clear_official_job:
            current_official_job_id = (
                str(existing_official_job.get("job_id") or "")
                if isinstance(existing_official_job, dict)
                else ""
            )
            if expected_official_job_id is None:
                log.error("Official job attachment mutation requires an expected job id")
                return False
            if str(expected_official_job_id) != current_official_job_id:
                log.warning(
                    "Official job attachment compare-and-swap rejected: expected=%s current=%s",
                    expected_official_job_id,
                    current_official_job_id,
                )
                return False
        if clear_official_job:
            if not isinstance(existing_official_job, dict):
                log.error("Refusing to clear absent official job attachment")
                return False
            existing_official_job = None
        elif official_job is not None:
            if not isinstance(official_job, dict):
                log.error("Official job attachment must be an object")
                return False
            required_official_job_fields = (
                "schema_version",
                "job_id",
                "identity_digest",
                "candidate_hash",
                "policy_id",
                "state",
                "revision",
            )
            if any(not str(official_job.get(key, "")).strip() for key in required_official_job_fields):
                log.error("Official job attachment is missing required identity fields")
                return False
            existing_official_job = dict(official_job)

        incoming_runtime_contract_ledger = (
            existing_master_plan.get("runtime_contract_ledger")
            if isinstance(existing_master_plan, dict)
            else None
        )
        if reset_runtime_contract_ledger:
            old_stage = existing.get("stage") if isinstance(existing, dict) else None
            reset_allowed, reset_reason = _ei.validate_runtime_contract_ledger_reset(
                old_stage,
                stage,
            )
            if not reset_allowed:
                log.error(
                    "Refusing runtime contract ledger reset for %s -> %s: %s",
                    old_stage,
                    stage,
                    reset_reason,
                )
                return False
            if str(runtime_contract_ledger_reset_reason or "") != reset_reason:
                log.error(
                    "Runtime contract ledger reset reason mismatch: requested=%s required=%s",
                    runtime_contract_ledger_reset_reason,
                    reset_reason,
                )
                return False
            if master_plan != {} or incoming_runtime_contract_ledger is not None:
                log.error(
                    "Runtime contract ledger reset requires an explicitly empty master_plan"
                )
                return False
            if expected_runtime_contract_ledger_digest is None:
                log.error(
                    "Runtime contract ledger reset requires an expected ledger digest"
                )
                return False
            try:
                from runtime_architecture_policy import validate_runtime_contract_ledger

                if existing_runtime_contract_ledger is not None:
                    existing_errors = validate_runtime_contract_ledger(
                        existing_runtime_contract_ledger
                    )
                    if existing_errors:
                        log.error(
                            "Refusing reset of invalid runtime contract ledger: %s",
                            existing_errors,
                        )
                        return False
                current_ledger_digest = str(
                    (existing_runtime_contract_ledger or {}).get("ledger_digest") or ""
                )
            except Exception as exc:
                log.error(
                    "Runtime contract ledger reset validation failed closed: %s",
                    exc,
                )
                return False
            if str(expected_runtime_contract_ledger_digest) != current_ledger_digest:
                log.warning(
                    "Runtime contract ledger reset compare-and-swap rejected: "
                    "expected=%s current=%s",
                    expected_runtime_contract_ledger_digest,
                    current_ledger_digest,
                )
                return False
            existing_runtime_contract_ledger = None

        if incoming_runtime_contract_ledger is not None or existing_runtime_contract_ledger is not None:
            try:
                from runtime_architecture_policy import validate_runtime_contract_ledger

                if incoming_runtime_contract_ledger is not None:
                    incoming_errors = validate_runtime_contract_ledger(incoming_runtime_contract_ledger)
                    if incoming_errors:
                        log.error("Refusing invalid runtime contract ledger: %s", incoming_errors)
                        return False
                if existing_runtime_contract_ledger is not None:
                    existing_errors = validate_runtime_contract_ledger(existing_runtime_contract_ledger)
                    if existing_errors:
                        log.error("Existing runtime contract ledger is invalid: %s", existing_errors)
                        return False
                    if incoming_runtime_contract_ledger is None and master_plan is not None:
                        log.error("Refusing master_plan rewrite that drops runtime contract ledger")
                        return False
                    if incoming_runtime_contract_ledger is not None:
                        previous_entries = {
                            str(item.get("contract_digest") or "")
                            for item in existing_runtime_contract_ledger.get("entries") or []
                        }
                        incoming_entries = {
                            str(item.get("contract_digest") or "")
                            for item in incoming_runtime_contract_ledger.get("entries") or []
                        }
                        if not previous_entries.issubset(incoming_entries):
                            log.error(
                                "Refusing runtime contract ledger rewrite/removal: missing=%s",
                                sorted(previous_entries - incoming_entries),
                            )
                            return False
                if incoming_runtime_contract_ledger is not None:
                    existing_runtime_contract_ledger = incoming_runtime_contract_ledger
            except Exception as exc:
                log.error("Runtime contract ledger validation failed closed: %s", exc)
                return False

        # Merge last_stage_change_ts: take max of existing vs current time.
        # This preserves the most recent genuine stage-change time on partial re-writes
        # (e.g. gate_results update without stage change).
        existing_stage_ts = 0.0
        if existing:
            existing_stage_ts = existing.get("last_stage_change_ts", 0.0)
        now_ts = time.time()
        # Validate stage transition and update timestamps
        old_stage = existing.get("stage") if existing else None
        is_valid, reason = _ei.validate_stage_transition(old_stage, stage)
        if not is_valid:
            log.warning(
                "Illegal stage transition: %s -> %s (%s). Blocking checkpoint write.",
                old_stage, stage, reason,
            )
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.stage_transition_blocked", "error",
                    f"Blocked illegal stage transition: {old_stage} -> {stage} ({reason})",
                    {"old_stage": old_stage, "new_stage": stage, "reason": reason,
                     "next_v": next_v, "source_v": source_v},
                )
            except Exception:
                pass
            return False
        # touch_stage_timestamp forces last_stage_change_ts to now even when the
        # stage did not change, e.g. the orchestrator's timeout-extension refresh
        # so the watchdog does not immediately re-fire after a cycle resume.
        if touch_stage_timestamp:
            new_stage_ts = now_ts
        else:
            new_stage_ts = now_ts if (old_stage != stage) else existing_stage_ts

        # AUTO-RESET precommit_attempt and timeout_extensions on true rework.
        # Any regression to a code-regeneration stage means this is new bot code,
        # so counters against the previous code snapshot must restart.
        rework_resets_counters = _ei.is_rework_reset_transition(old_stage, stage)
        official_job_invalidated = _ei.invalidates_official_job_transition(old_stage, stage)
        refresh_repo_baseline = (
            rework_resets_counters
            or _stage_refreshes_repo_baseline(old_stage, stage, existing_gate_results)
        )
        if rework_resets_counters:
            existing_precommit_attempt = 0
            existing_timeout_extensions = 0
        if rework_resets_counters or official_job_invalidated:
            existing_official_job = None
            existing_gate_results.pop("official_full", None)

        # Ensure int type invariants for persisted counters. None arises on a
        # fresh checkpoint when the caller did not pass a counter; defaulting
        # here keeps log correlation complete instead of emitting
        # {"audit": null, ...} for the rest of the generation.
        if existing_generation_attempt is None:
            existing_generation_attempt = 0
        if existing_audit_attempt is None:
            existing_audit_attempt = 0
        if existing_precommit_attempt is None:
            existing_precommit_attempt = 0
        if existing_precommit_rework_count is None:
            existing_precommit_rework_count = 0
        if existing_official_rework_count is None:
            existing_official_rework_count = 0
        run_id = f"{next_v}#{existing_generation_attempt}"
        if not existing_workflow_run_id:
            existing_workflow_run_id = (
                requested_workflow_run_id
                or f"generation:{int(next_v)}:{uuid.uuid4().hex}"
            )
        next_checkpoint_revision = existing_checkpoint_revision + 1
        _contract_checkpoint = {
            "next_v": next_v,
            "source_v": source_v,
            "parent2_v": existing_parent2_v,
            "gate_results": existing_gate_results,
            "stage": stage,
        }
        if bind_repo_baseline_head:
            # The publication transaction is the one pipeline-internal HEAD
            # advance that the stage predicate cannot model: it commits
            # ``stage=publishing`` before the git commit, then re-writes the
            # same stage after. The predicate (``publishing -> publishing``)
            # returns False, so without this explicit override the baseline
            # would stay pinned to the pre-commit HEAD and the post-publication
            # handoff's crash recovery would hard-block with
            # ``repo_baseline_head_mismatch``. Capture a fresh snapshot (so
            # branch/entries/contract reflect the post-commit tree) then pin
            # ``head`` to the authoritative publish commit OID.
            _captured = _capture_repo_baseline(
                stage,
                next_v=next_v,
                source_v=source_v,
                checkpoint=_contract_checkpoint,
            )
            if not isinstance(_captured, dict):
                _captured = {}
            _captured["head"] = str(bind_repo_baseline_head)
            existing_repo_baseline = _captured
        elif refresh_repo_baseline:
            existing_repo_baseline = _capture_repo_baseline(
                stage,
                next_v=next_v,
                source_v=source_v,
                checkpoint=_contract_checkpoint,
            )
        elif not existing_repo_baseline:
            existing_repo_baseline = _capture_repo_baseline(
                stage,
                next_v=next_v,
                source_v=source_v,
                checkpoint=_contract_checkpoint,
            )
        elif isinstance(existing_repo_baseline, dict):
            existing_repo_baseline["evaluation_contract"] = _ei.build_evaluation_contract(
                _ei.PROJECT_ROOT,
                candidate_v=next_v,
                source_v=source_v,
                checkpoint=_contract_checkpoint,
                stage=stage,
                include_hash=True,
            )

        from checkpoint_schema import (
            CHECKPOINT_SCHEMA_VERSION,
            checkpoint_epoch_errors,
            live_checkpoint_allocation_authority_errors,
            live_checkpoint_parent_authority_errors,
        )

        state = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "evaluation_epoch": EVALUATION_EPOCH,
            "epoch_binding": existing_epoch_binding,
            "next_v": next_v, "source_v": source_v, "stage": stage,
            "run_id": run_id,
            "workflow_run_id": existing_workflow_run_id,
            "checkpoint_revision": next_checkpoint_revision,
            "master_plan": existing_master_plan, "reviewer_feedback": existing_reviewer_feedback,
            "generation_attempt": existing_generation_attempt,
            "audit_attempt": existing_audit_attempt,
            "precommit_attempt": existing_precommit_attempt,
            "precommit_rework_count": existing_precommit_rework_count,
            "official_rework_count": existing_official_rework_count,
            "timeout_extensions": existing_timeout_extensions,
            "worker_failure_count": existing_failure_count,
            "gate_results": existing_gate_results,
            "parent2_v": existing_parent2_v,
            "direction_audit": existing_direction_audit,
            "audit_context": existing_audit_context,
            "literature_probe": existing_literature_probe,
            "workflow_profile_id": current_workflow_profile_id,
            "national_execution_mode": current_national_execution_mode,
            "repo_baseline": existing_repo_baseline,
            "prepare_scope_files": existing_prepare_scope_files,
            "runtime_contract_ledger": existing_runtime_contract_ledger,
            "infra_failure": existing_infra_failure,
            "official_job": existing_official_job,
            "repair_baseline_artifact_hash": existing_repair_baseline_artifact_hash,
            "candidate_artifact_hash": existing_candidate_artifact_hash,
            "candidate_manifest_digest": existing_candidate_manifest_digest,
            "charter_digest": existing_charter_digest,
            "publication_intent": existing_publication_intent,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_stage_change_ts": new_stage_ts,
            "last_update_ts": now_ts,  # Always bumps on any checkpoint write
        }
        if existing_publication_tier in {"native", "staging", "certified"}:
            state["publication_tier"] = existing_publication_tier
        if existing_is_draft:
            state["is_draft"] = True
        if existing_review_attempt_journal:
            state["review_attempt_journal"] = existing_review_attempt_journal
        if existing_identity_replan_history:
            state["identity_replan_history"] = existing_identity_replan_history
        if existing_terminal_gate_outcome is not None:
            state["terminal_gate_outcome"] = existing_terminal_gate_outcome

        epoch_errors = checkpoint_epoch_errors(state)
        if stage in terminal_stages:
            try:
                from gate_outcome import validate_terminal_gate_outcome

                terminal_errors = validate_terminal_gate_outcome(
                    state,
                    candidate_dir=_ei.get_bot_dir(next_v),
                )
            except Exception as exc:
                terminal_errors = [
                    "terminal_outcome_projection_validation_error:"
                    f"{type(exc).__name__}"
                ]
            if terminal_errors:
                log.error(
                    "Refusing invalid terminal gate outcome: %s",
                    terminal_errors,
                )
                return False
        if not epoch_errors:
            epoch_errors.extend(
                live_checkpoint_parent_authority_errors(
                    state,
                    repo_root=_ei.PROJECT_ROOT,
                )
            )
        if not epoch_errors:
            epoch_errors.extend(
                live_checkpoint_allocation_authority_errors(
                    state,
                    published_high_water=allocation_authority[
                        "published_high_water"
                    ],
                    abandoned_receipt_floor=allocation_authority[
                        "abandoned_receipt_floor"
                    ],
                    abandoned_receipt_head_digest=allocation_authority[
                        "abandoned_receipt_head_digest"
                    ],
                    allow_published_target_reconciliation=(
                        _publication_checkpoint_reconciliation_allowed(
                            state,
                            allocation_authority,
                        )
                    ),
                )
            )
        if epoch_errors:
            log.error(
                "Refusing checkpoint whose strict epoch binding does not match "
                "the final CAS projection: %s",
                epoch_errors,
            )
            return False

        # Whole-file atomic publication under the stable sidecar lock.
        _ei._atomic_publish_state_text(
            state_file,
            json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False),
        )

        # RC2/RC6: refresh event_bus last-known correlation so events emitted
        # after this stage advance — and especially after clear_pipeline_checkpoint
        # post-commit — still resolve the correct run_id/stage/attempt. This makes
        # stage/attempt correlation automatic for ALL pipeline code, not just call
        # sites that manually bind(). Also invalidates the checkpoint TTL cache so
        # the next emit sees the new stage immediately rather than the stale value.
        try:
            from event_bus import update_last_known, invalidate_ckpt_cache
            update_last_known(
                run_id=run_id,
                stage=stage,
                attempt={"generation": existing_generation_attempt,
                         "audit": existing_audit_attempt,
                         "precommit": existing_precommit_attempt})
            invalidate_ckpt_cache()
        except Exception:
            pass
        return True


def read_pipeline_checkpoint(slot_id=None):
    """Return saved pipeline state dict, or None."""
    state_file = _state_file_for_slot(slot_id)
    if not os.path.lexists(state_file):
        return None
    try:
        with _ei._locked_state_sidecar(state_file, lock_type=fcntl.LOCK_SH):
            raw = _ei._read_regular_state_text(
                state_file,
                allow_missing=False,
            )
        checkpoint = json.loads(raw)
        from pipeline_infrastructure import normalize_checkpoint_infrastructure

        return normalize_checkpoint_infrastructure(checkpoint)
    except Exception:
        return None


def clear_pipeline_checkpoint(
    *,
    expected_workflow_run_id=None,
    expected_next_v=None,
    expected_source_v=None,
    expected_checkpoint_revision=None,
    expected_checkpoint_stage=None,
    slot_id=None,
):
    """Delete pipeline checkpoint (called on successful commit).

    Uses exclusive lock to prevent race with concurrent writes.
    """
    state_file = _state_file_for_slot(slot_id)
    previous = None
    try:
        _ei._preflight_state_sidecar(state_file)
    except OSError:
        return False
    guard = _ei._locked_state_sidecar(
        state_file,
        lock_type=fcntl.LOCK_EX,
    )
    with guard:
        try:
            raw = _ei._read_regular_state_text(
                state_file,
                allow_missing=True,
            )
        except (OSError, UnicodeError):
            return False
        if raw.strip():
            try:
                previous = json.loads(raw)
            except Exception:
                previous = None
        if (
            expected_workflow_run_id is not None
            or expected_next_v is not None
            or expected_source_v is not None
            or expected_checkpoint_revision is not None
            or expected_checkpoint_stage is not None
        ):
            if not isinstance(previous, dict):
                return False
            actual_workflow_run_id = str(
                previous.get("workflow_run_id")
                or previous.get("run_id")
                or (
                    f"{int(previous.get('next_v'))}#"
                    f"{int(previous.get('generation_attempt') or 0)}"
                )
            )
            if (
                expected_workflow_run_id is not None
                and actual_workflow_run_id != str(expected_workflow_run_id)
            ):
                return False
            if (
                expected_next_v is not None
                and previous.get("next_v") != expected_next_v
            ):
                return False
            if (
                expected_source_v is not None
                and previous.get("source_v") != expected_source_v
            ):
                return False
            if (
                expected_checkpoint_revision is not None
                and int(previous.get("checkpoint_revision") or 0)
                != int(expected_checkpoint_revision)
            ):
                return False
            if (
                expected_checkpoint_stage is not None
                and str(previous.get("stage") or "")
                != str(expected_checkpoint_stage)
            ):
                return False
        # Unlink under the stable sidecar lock so writers cannot race a retired
        # checkpoint inode.
        state_file.unlink(missing_ok=True)
        _ei._fsync_directory(state_file.parent)
    try:
        from event_bus import emit
        next_v = previous.get("next_v") if previous else None
        gen_attempt = previous.get("generation_attempt", 0) if previous else 0
        audit_attempt = previous.get("audit_attempt", 0) if previous else 0
        precommit_attempt = previous.get("precommit_attempt", 0) if previous else 0
        emit(
            "pipeline.checkpoint_cleared", "info",
            "Pipeline checkpoint cleared",
            run_id=(previous.get("run_id") if previous else None) or (
                f"{next_v}#{gen_attempt}" if next_v is not None else None
            ),
            stage=previous.get("stage") if previous else None,
            attempt={"generation": gen_attempt, "audit": audit_attempt,
                     "precommit": precommit_attempt},
            next_v=next_v,
            source_v=previous.get("source_v") if previous else None,
        )
    except Exception:
        try:
            from system_log import _write_system_event_raw
            _write_system_event_raw(
                "pipeline.checkpoint_cleared", "info",
                "Pipeline checkpoint cleared",
                {"next_v": previous.get("next_v") if previous else None,
                 "source_v": previous.get("source_v") if previous else None,
                 "stage": previous.get("stage") if previous else None,
                 "run_id": previous.get("run_id") if previous else None,
                 "category": "pipeline.checkpoint_cleared"},
            )
        except Exception:
            pass
    return True
