"""Identity-replan recovery subsystem for tool_planning.

Extracted as a cohesive business cluster; ``tool_planning.py`` retains thin
delegate shells so external ``from tool_planning import <name>`` and
``monkeypatch.setattr(tool_planning, "<name>", ...)`` keep resolving.

Business responsibility (single cohesive domain):
* Compiled-task-context reset / cleanup before an identity refresh.
* Architecture-policy identity error extraction and runtime-contract digest
  checkpointing.
* Identity-replan fingerprinting, consecutive-attempt counting, and the
  ``IDENTITY_REPLAN_ABANDON_THRESHOLD`` circuit breaker.
* Materialization of an identity-replan candidate (large reset/replan flow).
* Recovery of architecture-policy identity (immediate and persisted across
  replan attempts).

Stateful pipeline recovery: reads/writes checkpoint state but performs no LLM
I/O of its own.

Cross-references to symbols that remain in ``tool_planning`` (the identity-
replan audit-key table, the operation-id builder, and the legacy receipt-error
helper) are reached through ``_tp.<name>`` so that test monkeypatches on
``tool_planning.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_tp.<name>(...)`` so monkeypatches on
``tool_planning.<name>`` propagate even when both call sites now live in
this companion.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from copy import deepcopy
from pathlib import Path

import tool_planning as _tp  # for cross-refs


def _incremental_reset_next_dir(next_dir, source_dir):
    """Incremental reset: overwrite files present in source (undo worker edits to
    existing files), PRESERVE worker-created NEW files (absent from source). Returns
    the list of preserved NEW filenames.

    Invariants after this call:
      - files in both source+next -> identical to source (authoritative overwrite)
      - files only in next (worker-created NEW) -> untouched (survive the reset)
      - files only in source -> created
      - parent .completed sentinels are removed; commit_bot is the only writer
        allowed to mark a candidate complete
    """
    from evolution_infra import candidate_copy_ignore, is_candidate_copy_ignored_name

    source_names = {
        item.name
        for item in source_dir.iterdir()
        if not is_candidate_copy_ignored_name(item.name)
    }
    preserved = []
    # Walk next_dir entries: clean stale bytecode, preserve NEW files, remove files
    # that exist in source so the source copy overwrites authoritatively.
    for item in next_dir.iterdir():
        if is_candidate_copy_ignored_name(item.name):
            # Clean parent/runtime artifacts. .task_context is generated per
            # current plan by plan_compiler and must not survive resets.
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        elif item.name not in source_names:
            # Worker-created NEW file absent from source: PRESERVE it.
            preserved.append(item.name)
        else:
            # Exists in source: remove so source copy overwrites authoritatively.
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    # Copy all source entries into next_dir (skip parent/runtime artifacts).
    # Source files are recreated/overwritten; NEW files preserved above are untouched.
    for item in source_dir.iterdir():
        if is_candidate_copy_ignored_name(item.name):
            continue
        if item.is_dir():
            shutil.copytree(item, next_dir / item.name,
                            ignore=candidate_copy_ignore)
        else:
            shutil.copy2(item, next_dir / item.name)
    return preserved



def _clear_compiled_task_context(next_dir):
    """Remove the system-owned Worker brief after a successful batch.

    ``.task_context`` is control-plane input, not part of the bot artifact.
    Keeping it through quality/official certification could hide an accidental
    runtime dependency because publication intentionally excludes the brief.
    """
    from candidate_hygiene import cleanup_transient_candidate_artifacts

    cleanup_transient_candidate_artifacts(
        next_dir,
        include_task_context=True,
    )



def _cleanup_worker_transients_before_identity_refresh(next_dir):
    """Remove host-owned compile caches before rebuilding strict identity.

    The Worker contract permits only an exact-file ``py_compile`` probe and
    explicitly denies cache cleanup to the model.  ``py_compile`` nevertheless
    creates ``__pycache__`` beside ``policy.py``.  Snapshot/delta accounting
    intentionally excludes that transient output, while the strict five-file
    identity validator correctly rejects it.  Close that work-phase boundary
    here: after the Worker write audit has passed, the host removes only the
    centrally defined transient cache surface and deliberately retains the
    compiler-owned ``.task_context`` until the refreshed identity is bound.

    The shared hygiene helper rejects symlinks and non-regular entries before
    removing anything.  Arbitrary extra files/directories remain untouched and
    therefore continue to fail the strict layout check below.
    """
    from candidate_hygiene import cleanup_transient_candidate_artifacts

    return cleanup_transient_candidate_artifacts(
        next_dir,
        include_task_context=False,
    )



def _materialize_identity_replan_candidate(
    ckpt,
    next_dir,
    source_dir,
    *,
    recover_persisted_reset: bool,
):
    """Rebuild and publish one single-parent prepared identity transaction.

    Candidate bytes are projected with the existing journaled
    ``RENAME_EXCHANGE`` content CAS.  The checkpoint then uses its independent
    revision/stage/workflow CAS.  If that second CAS loses, the exact immutable
    preimage is restored; a crash inside an unfinished exchange is recovered
    from the destination journal before a fresh forward operation is opened.
    """

    from bot_artifact import canonical_digest, hash_path
    from bot_namespace import (
        POLICY_EPOCH_RECEIPT,
        policy_identity_document_errors,
        refresh_policy_identity_documents,
        strict_lineage_parent_versions,
    )
    from candidate_hygiene import sanitize_candidate_dir
    from evolution_infra import RESULTS_DIR, copy_bot_tree_for_candidate
    from prepared_baseline_contract import build_prepared_artifact_contract
    from worker_workflow import WorkerArtifactStore

    next_dir = Path(next_dir)
    source_dir = Path(source_dir)
    next_v = int(ckpt.get("next_v"))
    source_v = int(ckpt.get("source_v"))
    if ckpt.get("parent2_v") is not None:
        raise RuntimeError("identity replan cannot reconstruct crossover lineage")
    if not source_dir.is_dir() or source_dir.is_symlink():
        raise RuntimeError("identity replan source is missing or nonregular")
    if not next_dir.is_dir() or next_dir.is_symlink():
        raise RuntimeError("identity replan candidate is missing or nonregular")
    if (
        ckpt.get("publication_intent") is not None
        or ckpt.get("official_job") is not None
        or ckpt.get("infra_failure") is not None
    ):
        raise RuntimeError("identity replan has an incompatible durable overlay")

    try:
        source_receipt = json.loads(
            (source_dir / POLICY_EPOCH_RECEIPT).read_text(encoding="utf-8")
        )
        source_parents = tuple(
            int(item)
            for item in ((source_receipt.get("lineage") or {}).get("parent_versions") or [])
        )
    except Exception as exc:
        raise RuntimeError(
            f"identity replan source receipt unavailable:{type(exc).__name__}"
        ) from exc
    source_identity_errors = policy_identity_document_errors(
        source_dir,
        source_v,
        parent_versions=source_parents,
    )
    if source_identity_errors:
        raise RuntimeError(
            "identity replan source identity invalid:"
            + ";".join(source_identity_errors[:8])
        )
    source_hash = hash_path(source_dir)
    parent_identities = (
        (ckpt.get("epoch_binding") or {}).get("published_parent_identities")
        or []
    )
    source_bindings = [
        item
        for item in parent_identities
        if isinstance(item, dict) and item.get("version") == source_v
    ]
    if (
        len(source_bindings) != 1
        or source_bindings[0].get("tag_artifact_hash") != source_hash
    ):
        raise RuntimeError("identity replan source tag artifact binding mismatch")

    next_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{next_dir.name}.identity-replan-build-",
        dir=next_dir.parent,
    ) as temporary:
        staged_dir = Path(temporary) / next_dir.name
        copy_bot_tree_for_candidate(source_dir, staged_dir)
        lineage = strict_lineage_parent_versions(next_v, source_v, None)
        refreshed_identity = refresh_policy_identity_documents(
            staged_dir,
            next_v,
            parent_versions=lineage,
        )
        sanitize_candidate_dir(staged_dir, require_native_tcp=True)
        staged_errors = policy_identity_document_errors(
            staged_dir,
            next_v,
            parent_versions=lineage,
        )
        if staged_errors:
            raise RuntimeError(
                "identity replan target identity invalid:"
                + ";".join(staged_errors[:8])
            )
        prepared_contract = build_prepared_artifact_contract(
            staged_dir,
            source_v=source_v,
            next_v=next_v,
        )
        prepared_hash = str(prepared_contract["prepared_artifact_hash"])
        current_hash = hash_path(next_dir)
        if recover_persisted_reset:
            legacy_errors = _tp._legacy_identity_replan_receipt_errors(
                ckpt,
                source_hash=source_hash,
                current_hash=current_hash,
                prepared_contract=prepared_contract,
            )
            if legacy_errors:
                return _json_tool_result({
                    "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_RECOVERY_INVALID",
                    "failure_class": "state_migration",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "validation_errors": legacy_errors,
                    "candidate_overwritten": False,
                    "directive": (
                        "The persisted identity-replan preimage is not the exact "
                        "legacy transaction produced by the system. Preserve all "
                        "bytes and use canonical checkpoint reconciliation; never "
                        "rewrite JSON or copy a parent by hand."
                    ),
                })

        artifact_store = WorkerArtifactStore(
            Path(RESULTS_DIR) / "workflow" / "artifacts"
        )
        preimage_snapshot = artifact_store.capture(next_dir)
        prepared_snapshot = artifact_store.capture(staged_dir)
        if preimage_snapshot != current_hash or prepared_snapshot != prepared_hash:
            raise RuntimeError("identity replan immutable snapshot mismatch")

    operation_id = _tp._identity_replan_operation_id(ckpt, prepared_hash)
    materialization = artifact_store.materialize(
        prepared_snapshot,
        next_dir,
        expected_destination_digest=current_hash,
        operation_id=operation_id,
    )
    if hash_path(next_dir) != prepared_hash:
        raise RuntimeError("identity replan materialization hash mismatch")

    prior_audit = ckpt.get("audit_context") or {}
    policy_errors = (
        (prior_audit.get("architecture_policy_identity_replan") or {}).get(
            "identity_errors"
        )
        if recover_persisted_reset
        else _tp._checkpoint_architecture_policy_identity_errors(ckpt)
    ) or []
    if materialization.installed:
        materialization_proof = artifact_store.verify_materialization_receipt(
            materialization.operation_id,
            destination=next_dir,
            digest=prepared_hash,
            expected_destination_digest=current_hash,
            receipt_digest=materialization.receipt_digest,
        )
    else:
        materialization_proof = (
            artifact_store.find_installed_materialization_receipt(
                destination=next_dir,
                digest=prepared_hash,
            )
        )
        if materialization_proof is None:
            raise RuntimeError(
                "identity replan lacks installed materialization proof"
            )

    replan_receipt = {
        "schema_version": 2,
        "kind": "single-parent-architecture-policy-identity-replan-v2",
        "source_v": source_v,
        "next_v": next_v,
        "workflow_run_id": str(ckpt.get("workflow_run_id") or ""),
        "checkpoint_preimage_revision": int(
            ckpt.get("checkpoint_revision") or 0
        ),
        "checkpoint_preimage_stage": str(ckpt.get("stage") or ""),
        "source_stage": str(
            (prior_audit.get("architecture_policy_identity_replan") or {}).get(
                "source_stage"
            )
            if recover_persisted_reset
            else ckpt.get("stage")
        ),
        "recovery_mode": (
            "legacy_parent_copy_recovery"
            if recover_persisted_reset
            else "quality_identity_replan"
        ),
        "identity_errors": [str(item) for item in policy_errors],
        "source_artifact_hash": source_hash,
        "replaced_artifact_hash": materialization_proof[
            "expected_destination_digest"
        ],
        "prepared_artifact_hash": prepared_hash,
        "prepared_artifact_contract_digest": prepared_contract["contract_digest"],
        "runtime_manifest_digest": refreshed_identity["runtime_manifest_digest"],
        "epoch_receipt_digest": refreshed_identity["epoch_receipt_digest"],
        "runtime_manifest_file_sha256": next(
            str(item.get("sha256") or "")
            for item in prepared_contract["prepared_artifact_manifest"]["entries"]
            if item.get("type") == "file"
            and item.get("path") == "national_runtime_manifest.json"
        ),
        "epoch_receipt_file_sha256": next(
            str(item.get("sha256") or "")
            for item in prepared_contract["prepared_artifact_manifest"]["entries"]
            if item.get("type") == "file"
            and item.get("path") == "policy_epoch_receipt.json"
        ),
        "materialization_operation_id": materialization_proof["operation_id"],
        "materialization_expected_destination_digest": materialization_proof[
            "expected_destination_digest"
        ],
        "materialization_receipt_digest": materialization_proof[
            "receipt_digest"
        ],
        "candidate_reset_to_source": True,
        "target_identity_refreshed": True,
        "stale_worker_gate_identity_cleared": True,
    }
    replan_receipt["receipt_digest"] = canonical_digest(replan_receipt)
    replacement_audit = {
        key: deepcopy(prior_audit[key])
        for key in _tp._IDENTITY_REPLAN_AUDIT_KEYS
        if key in prior_audit
    }
    replacement_audit.update({
        "prepared_artifact_contract": prepared_contract,
        "architecture_policy_identity_replan": replan_receipt,
    })

    old_stage = str(ckpt.get("stage") or "")
    reset_ledger = old_stage != "direction_audited"
    write_kwargs = {}
    if reset_ledger:
        write_kwargs = {
            "reset_runtime_contract_ledger": True,
            "expected_runtime_contract_ledger_digest": (
                _tp._checkpoint_runtime_contract_ledger_digest(ckpt)
            ),
            "runtime_contract_ledger_reset_reason": (
                "architecture_policy_identity_replan"
            ),
        }
    written = write_pipeline_checkpoint(
        next_v,
        source_v,
        "direction_audited",
        master_plan={},
        direction_audit=ckpt.get("direction_audit"),
        audit_context=replacement_audit,
        replace_audit_context=True,
        audit_context_replacement_reason=(
            "architecture_policy_identity_replan"
        ),
        worker_failure_count=0,
        clear_reviewer_feedback=True,
        reset_generation_attempt=True,
        reset_audit_attempt=True,
        reset_precommit_attempt=True,
        precommit_rework_count=0,
        official_rework_count=0,
        clear_repair_baseline_artifact_hash=True,
        touch_stage_timestamp=True,
        expected_checkpoint_revision=int(ckpt.get("checkpoint_revision") or 0),
        expected_checkpoint_stage=old_stage,
        expected_workflow_run_id=str(ckpt.get("workflow_run_id") or ""),
        **write_kwargs,
    )
    if not written:
        current = _matching_checkpoint(next_v, source_v) or {}
        current_prepared = (
            (current.get("audit_context") or {}).get("prepared_artifact_contract")
        )
        current_replan = (
            (current.get("audit_context") or {}).get(
                "architecture_policy_identity_replan"
            )
        )
        current_replan_unsigned = (
            {
                key: value
                for key, value in current_replan.items()
                if key != "receipt_digest"
            }
            if isinstance(current_replan, dict)
            else {}
        )
        if (
            current.get("stage") == "direction_audited"
            and current_prepared == prepared_contract
            and isinstance(current_replan, dict)
            and current_replan.get("schema_version") == 2
            and current_replan.get("receipt_digest")
            == canonical_digest(current_replan_unsigned)
            and current_replan.get("prepared_artifact_hash")
            == prepared_hash
            and current_replan.get("prepared_artifact_contract_digest")
            == prepared_contract.get("contract_digest")
            and current_replan.get("target_identity_refreshed") is True
            and current_replan.get("stale_worker_gate_identity_cleared") is True
            and str(current.get("workflow_run_id") or "")
            == str(ckpt.get("workflow_run_id") or "")
            and hash_path(next_dir) == prepared_hash
        ):
            return _json_tool_result({
                "success": True,
                "recovered": True,
                "idempotent_checkpoint_projection": True,
                "next_v": next_v,
                "source_v": source_v,
                "next_tool": "run_master",
            })
        # Candidate and checkpoint publication are two fenced CAS operations,
        # not one filesystem transaction.  Rollback is safe only while the
        # checkpoint authority is still the exact preimage this invocation
        # read.  A concurrent recovery may have published revision N+1 and a
        # second run_master may already have bound the prepared bytes in N+2;
        # rolling those bytes back merely because N+2 is no longer the
        # direction_audited idempotency shape would corrupt the successor.
        checkpoint_preimage_unchanged = current == ckpt
        if not checkpoint_preimage_unchanged:
            return _json_tool_result({
                "error": (
                    "ARCHITECTURE_POLICY_IDENTITY_REPLAN_"
                    "CHECKPOINT_CONCURRENTLY_ADVANCED"
                ),
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "candidate_forward_preserved": (
                    hash_path(next_dir) == prepared_hash
                ),
                "candidate_preimage_restored": False,
                "expected_checkpoint_revision": int(
                    ckpt.get("checkpoint_revision") or 0
                ),
                "current_checkpoint_revision": current.get(
                    "checkpoint_revision"
                ),
                "expected_checkpoint_stage": old_stage,
                "current_checkpoint_stage": current.get("stage"),
                "expected_workflow_run_id": str(
                    ckpt.get("workflow_run_id") or ""
                ),
                "current_workflow_run_id": str(
                    current.get("workflow_run_id") or ""
                ),
                "directive": (
                    "Checkpoint authority changed after candidate content-CAS. "
                    "The forward prepared bytes were preserved because a "
                    "successor may already bind them. Re-read the canonical "
                    "route; never roll back or edit the candidate by hand."
                ),
            })
        if materialization.installed and current_hash != prepared_hash:
            rollback_id = f"{operation_id}-rollback"
            artifact_store.materialize(
                preimage_snapshot,
                next_dir,
                expected_destination_digest=prepared_hash,
                operation_id=rollback_id,
            )
        return _json_tool_result({
            "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_CHECKPOINT_CAS_FAILED",
            "failure_class": "control_plane",
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "candidate_preimage_restored": hash_path(next_dir) == current_hash,
        })

    log_system_event(
        "pipeline.architecture_policy_identity_replan",
        "error",
        (
            f"Rebuilt target-version prepared identity for v{next_v} from "
            f"strict parent v{source_v}; fresh Master plan required"
        ),
        {
            "next_v": next_v,
            "source_v": source_v,
            "source_stage": old_stage,
            "prepared_artifact_hash": prepared_hash,
            "prepared_artifact_contract_digest": prepared_contract[
                "contract_digest"
            ],
            "receipt_digest": replan_receipt["receipt_digest"],
            "legacy_recovery": recover_persisted_reset,
        },
    )
    return _json_tool_result({
        "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN",
        "recovered": True,
        "next_v": next_v,
        "source_v": source_v,
        "identity_errors": list(policy_errors),
        "candidate_reset_to_source": True,
        "target_identity_refreshed": True,
        "prepared_artifact_hash": prepared_hash,
        "prepared_artifact_contract_digest": prepared_contract[
            "contract_digest"
        ],
        "replan_receipt_digest": replan_receipt["receipt_digest"],
        "next_tool": "run_master",
        "directive": (
            "The stale Worker/gate identity was cleared and the exact strict "
            "parent was rematerialized as a target-version prepared artifact. "
            "Call run_master again to build a fresh policy-bound plan."
        ),
    })



def _checkpoint_architecture_policy_identity_errors(ckpt):
    if not isinstance(ckpt, dict):
        return []
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    transition = quality.get("national_architecture_transition") or {}
    if not isinstance(transition, dict):
        return []
    return [str(item) for item in transition.get("policy_identity_errors") or [] if str(item)]



# Identity replan circuit breaker. A deterministic identity error that is not
# actually fixed by the replan path (e.g. a frozen-vs-recomputed digest
# mismatch caused by non-determinism, as observed in the v152 loop) would
# otherwise retrigger the same recovery forever, burning LLM budget with zero
# possibility of success. Track consecutive identical error fingerprints; once
# the threshold is crossed, abandon the generation and surface to the operator
# instead of looping. Distinct fingerprints reset the count, so genuine
# progressive repair is unaffected.
IDENTITY_REPLAN_ABANDON_THRESHOLD = 3



def _identity_replan_fingerprint(errors):
    """Stable, deduplicated string key for the identity error set.

    Serialized to a single string so the value round-trips through JSON
    checkpoint storage and string comparisons in the circuit breaker.
    """
    items = sorted(set(str(item) for item in (errors or []) if str(item)))
    return "|".join(items)



def _identity_replan_counts(ckpt):
    """Return the history list of recorded replan fingerprints (strings).

    Stored under checkpoint key ``identity_replan_history``. Only the trailing
    run identical to the most recent entry matters for the circuit breaker,
    but the full list is kept for diagnostics.
    """
    if not isinstance(ckpt, dict):
        return []
    history = ckpt.get("identity_replan_history")
    return [str(item) for item in (history or []) if isinstance(item, str)]



def _identity_replan_consecutive_count(history, fingerprint):
    """Count trailing history entries equal to ``fingerprint``."""
    if not fingerprint:
        return 0
    count = 0
    for item in reversed(history):
        if item == fingerprint:
            count += 1
        else:
            break
    return count



def _record_identity_replan_attempt(ckpt, fingerprint):
    """Record one replan attempt and return the updated history list.

    A different fingerprint from the prior attempt resets the consecutive run
    (progressive repair). Caller is responsible for writing the checkpoint.
    """
    if not isinstance(ckpt, dict) or not fingerprint:
        return _tp._identity_replan_counts(ckpt)
    history = _tp._identity_replan_counts(ckpt)
    if history and history[-1] != fingerprint:
        history = []
    history.append(fingerprint)
    ckpt["identity_replan_history"] = list(history)
    return history



def _checkpoint_runtime_contract_ledger_digest(ckpt):
    ledger = ckpt.get("runtime_contract_ledger") if isinstance(ckpt, dict) else None
    if ledger is None and isinstance(ckpt, dict):
        master_plan = ckpt.get("master_plan")
        if isinstance(master_plan, dict):
            ledger = master_plan.get("runtime_contract_ledger")
    return str((ledger or {}).get("ledger_digest") or "")



def _recover_architecture_policy_identity(ckpt, next_dir, source_dir):
    """Discard stale-policy code and route through a fresh system-owned Master plan."""
    errors = _tp._checkpoint_architecture_policy_identity_errors(ckpt)
    if not errors:
        return None
    next_v = ckpt.get("next_v")
    source_v = ckpt.get("source_v")
    parent2_v = ckpt.get("parent2_v")
    if parent2_v is not None:
        # Resetting a crossover child to Parent A while retaining parent2_v and
        # crossover metadata fabricates a two-parent lineage.  The prepared
        # child is itself the authoritative baseline; once its policy identity
        # is stale there is no trusted single-parent reconstruction path.
        log_system_event(
            "pipeline.crossover_policy_identity_fail_closed",
            "error",
            f"Crossover v{next_v} policy identity is stale; refusing Parent-A reset",
            {
                "next_v": next_v,
                "source_v": source_v,
                "parent2_v": parent2_v,
                "source_stage": ckpt.get("stage"),
                "identity_errors": errors,
            },
        )
        return _json_tool_result({
            "error": "CROSSOVER_ARCHITECTURE_POLICY_IDENTITY_STALE",
            "next_v": next_v,
            "source_v": source_v,
            "parent2_v": parent2_v,
            "identity_errors": errors,
            "candidate_reset_to_source": False,
            "next_tool": "abandon_generation",
            "directive": (
                "Fail closed: do not reset this two-parent child to Parent A while "
                "claiming crossover lineage. Abandon this generation, then rerun "
                "crossover from a fresh selected checkpoint under the current policy."
            ),
        })
    return _tp._materialize_identity_replan_candidate(
        ckpt,
        next_dir,
        source_dir,
        recover_persisted_reset=False,
    )



def _recover_persisted_architecture_policy_identity_replan(
    ckpt,
    next_dir,
    source_dir,
):
    """Repair the exact direction_audited state emitted by the retired reset.

    A valid new receipt with a matching prepared artifact is already complete.
    Any other Direction checkpoint is outside this migration and remains under
    the ordinary prepared-artifact drift gate.
    """

    if not isinstance(ckpt, dict) or ckpt.get("stage") != "direction_audited":
        return None
    audit = ckpt.get("audit_context") or {}
    receipt = audit.get("architecture_policy_identity_replan")
    if not isinstance(receipt, dict):
        return None
    from prepared_baseline_contract import validate_prepared_artifact_contract
    from evolution_infra import (
        RESULTS_DIR,
        _identity_replan_live_materialization_errors,
        _identity_replan_replacement_contract_errors,
    )

    prepared = audit.get("prepared_artifact_contract")
    prepared_errors = validate_prepared_artifact_contract(
        prepared,
        prepared_dir=next_dir,
        source_v=ckpt.get("source_v"),
        next_v=ckpt.get("next_v"),
        verify_live_content=True,
    )
    if receipt.get("schema_version") == 2:
        try:
            receipt_revision = int(receipt.get("checkpoint_preimage_revision"))
        except (TypeError, ValueError):
            receipt_revision = -1
        receipt_stage = str(receipt.get("checkpoint_preimage_stage") or "")
        replan_errors = _identity_replan_replacement_contract_errors(
            replacement=audit,
            next_v=ckpt.get("next_v"),
            source_v=ckpt.get("source_v"),
            workflow_run_id=str(ckpt.get("workflow_run_id") or ""),
            checkpoint_revision=receipt_revision,
            checkpoint_stage=receipt_stage,
            epoch_binding=ckpt.get("epoch_binding"),
        )
        if not replan_errors:
            replan_errors.extend(
                _identity_replan_live_materialization_errors(
                    audit,
                    candidate_dir=next_dir,
                    artifact_root=Path(RESULTS_DIR) / "workflow" / "artifacts",
                )
            )
        if int(ckpt.get("checkpoint_revision") or 0) != receipt_revision + 1:
            replan_errors.append(
                "identity_replan_checkpoint_projection_revision_mismatch"
            )
        if not prepared_errors and not replan_errors:
            return None
        return _json_tool_result({
            "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_RECOVERY_INVALID",
            "failure_class": "state_migration",
            "action": "operator_reconcile",
            "next_v": ckpt.get("next_v"),
            "source_v": ckpt.get("source_v"),
            "validation_errors": list(dict.fromkeys([
                *prepared_errors,
                *replan_errors,
            ])),
            "candidate_overwritten": False,
            "directive": (
                "The schema-2 identity-replan projection is not the exact "
                "closed receipt published by its checkpoint CAS. Preserve all "
                "bytes and reconcile canonical authority; never rewrite JSON "
                "or copy a parent by hand."
            ),
        })
    return _tp._materialize_identity_replan_candidate(
        ckpt,
        next_dir,
        source_dir,
        recover_persisted_reset=True,
    )



