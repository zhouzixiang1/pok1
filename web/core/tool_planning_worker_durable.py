"""Worker durable execution projection/effect cluster (Group F).

Extracted from ``tool_planning_worker.py`` for maintainability.  This companion
holds the durable Worker projection and effect-execution engine:

- Worker prompt template loading (``_load_worker_prompt_template``)
- Durable checkpoint-contract / output-projection matching
  (``_durable_checkpoint_contract_matches``,
   ``_durable_output_already_projected``,
   ``_project_durable_worker_output``,
   ``_project_durable_worker_failure``)
- The single fenced Worker effect runner
  (``_run_durable_worker_effect``)
- Worker execution identity contract helpers
  (``_worker_execution_task_digest``, ``_worker_backend_contract``,
   ``_expected_worker_backend_contract``,
   ``_worker_availability_resume_receipt_errors``)
- The deferred-activity dataclass (``_DeferredWorkerActivity``)
- The internal Worker command dispatcher (``_execute_workers_command``)
- The public MCP entry (``execute_workers``), called by the thin delegate
  in ``tool_planning_worker.py``.

Import contract
---------------
The parent ``tool_planning_worker`` module re-exports every symbol defined
here, and (via its own header imports + the ``_TPCallableProxy`` /
``_bootstrap_ad_symbols`` machinery) exposes the external helper symbols the
durable bodies call.  Every such symbol is referenced in this companion as
``_tw.<name>`` so that:

* monkeypatch compatibility is preserved -- a ``monkeypatch.setattr(
  tool_planning, "<name>", fake)`` issued by a test is observed the next
  time the body calls ``_tw.<name>(...)``, because ``_tw`` is the parent
  module and the parent re-reads ``tool_planning.<name>`` live through its
  proxy; and
* the A-D snapshot symbols (``IDENTITY_REPLAN_ABANDON_THRESHOLD`` etc.) and
  Group E quality-contract helpers resolve through the parent's already-
  populated ``__dict__``.

Intra-cluster calls (one moved function calling another) remain bare, since
both caller and callee now live in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import tool_planning_worker as _tw


def _load_worker_prompt_template(prompts_dir, *, native_tcp=None):
    """Compose the worker harness for the sole national-native profile."""
    prompts_dir = _tw.Path(prompts_dir)
    if native_tcp is None:
        from workflow_profiles import get_workflow_profile

        native_tcp = (
            getattr(get_workflow_profile(), "national_execution_mode", "native_tcp")
            == "native_tcp"
        )
    if not native_tcp:
        raise RuntimeError("active Worker execution requires national native TCP")
    common = (prompts_dir / "worker_prompt.md").read_text(encoding="utf-8")
    marker = "{execution_profile_contract}"
    if common.count(marker) != 1:
        raise RuntimeError(
            "worker_prompt.md must contain exactly one execution profile marker"
        )
    profile = (prompts_dir / "worker_profile_national_native.md").read_text(
        encoding="utf-8"
    )
    return common.replace(marker, profile)


def _durable_checkpoint_contract_matches(checkpoint, contract):
    if not isinstance(checkpoint, dict) or not isinstance(contract, dict):
        return False
    checkpoint_workflow_id = str(
        checkpoint.get("workflow_run_id")
        or checkpoint.get("run_id")
        or (
            f"{int(checkpoint.get('next_v'))}#"
            f"{int(checkpoint.get('generation_attempt') or 0)}"
        )
    )
    return (
        checkpoint_workflow_id
        == str(contract.get("workflow_run_id") or "")
        and int(checkpoint.get("checkpoint_revision") or 0)
        == int(contract.get("checkpoint_revision") or 0)
        and str(checkpoint.get("stage") or "")
        == str(contract.get("checkpoint_stage") or "")
    )


def _durable_output_already_projected(checkpoint, projection):
    if not isinstance(checkpoint, dict):
        return False
    contract = projection.get("checkpoint_contract") or {}
    checkpoint_workflow_id = str(
        checkpoint.get("workflow_run_id")
        or checkpoint.get("run_id")
        or ""
    )
    if checkpoint_workflow_id != str(contract.get("workflow_run_id") or ""):
        return False
    receipt = (
        (checkpoint.get("audit_context") or {}).get("durable_worker_output")
        if isinstance(checkpoint.get("audit_context"), dict)
        else None
    )
    expected = projection.get("durable_worker_output") or {}
    return bool(
        isinstance(receipt, dict)
        and receipt.get("artifact_hash") == expected.get("artifact_hash")
        and receipt.get("envelope_digest") == expected.get("envelope_digest")
    )


async def _project_durable_worker_output(worker_workflow, next_dir, state):
    """Project a completed immutable Worker receipt without invoking an LLM."""
    projection = _tw.deepcopy(state.get("projection") or {})
    envelope = state.get("envelope") or {}
    next_v = int(envelope.get("next_v"))
    source_v = int(envelope.get("source_v"))
    contract = projection.get("checkpoint_contract") or {}
    checkpoint = _tw._matching_checkpoint(next_v, source_v)
    if _durable_output_already_projected(checkpoint, projection):
        # At the immediate workers_done projection, reconcile a missing or
        # poisoned canonical tree from the immutable artifact.  If downstream
        # gates already advanced the checkpoint, their matching receipt proves
        # this output was published; never rewind candidate bytes that a later
        # authorized stage may have transformed.
        if checkpoint.get("stage") == "workers_done":
            expected_output = str(state.get("output_artifact_hash") or "")
            canonical_exists = _tw.Path(next_dir).exists()
            canonical_hash = (
                _tw._complete_artifact_fingerprint(next_dir)
                if canonical_exists
                else ""
            )
            if canonical_exists and canonical_hash != expected_output:
                return _tw._json_tool_result({
                    "error": "DURABLE_WORKER_PROJECTED_ARTIFACT_MISMATCH",
                    "success": False,
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                })
            if not canonical_exists:
                worker_workflow.artifacts.materialize(
                    str(state.get("output_snapshot_hash") or ""),
                    next_dir,
                    expected_destination_digest=None,
                )
            if _tw._complete_artifact_fingerprint(next_dir) != expected_output:
                return _tw._json_tool_result({
                    "error": "DURABLE_WORKER_PROJECTED_ARTIFACT_MISMATCH",
                    "success": False,
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                })
        worker_workflow.projected("workers_done")
        return _tw._json_tool_result({
            "success": True,
            "durable_recovery": (
                "confirmed_existing_worker_projection"
                if checkpoint.get("stage") == "workers_done"
                else "confirmed_downstream_worker_projection"
            ),
            "current_checkpoint_stage": checkpoint.get("stage"),
            "output_artifact_hash": state.get("output_artifact_hash"),
            "next_v": next_v,
            "source_v": source_v,
        })
    if not _durable_checkpoint_contract_matches(checkpoint, contract):
        return _tw._json_tool_result({
            "error": "DURABLE_WORKER_OUTPUT_PROJECTION_CONFLICT",
            "success": False,
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "expected_checkpoint": contract,
            "current_checkpoint": {
                "workflow_run_id": (
                    checkpoint.get("workflow_run_id") if checkpoint else None
                ),
                "checkpoint_revision": (
                    checkpoint.get("checkpoint_revision") if checkpoint else None
                ),
                "stage": checkpoint.get("stage") if checkpoint else None,
            },
            "directive": (
                "The immutable output is safe, but another command advanced the "
                "checkpoint. Do not rewind it or call the LLM; reconcile the actor "
                "history with the current projection."
            ),
        })
    projection_preimage_hash = str(
        envelope.get("projection_preimage_artifact_hash") or ""
    )
    projection_preimage_snapshot = str(
        envelope.get("projection_preimage_snapshot_hash") or ""
    )
    output_hash = str(state.get("output_artifact_hash") or "")
    current_artifact_hash = _tw._complete_artifact_fingerprint(next_dir)
    if (
        _tw.Path(next_dir).exists()
        and current_artifact_hash not in {
            projection_preimage_hash,
            output_hash,
        }
    ):
        return _tw._json_tool_result({
            "error": "DURABLE_WORKER_PRE_PROJECTION_ARTIFACT_DRIFT",
            "success": False,
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "expected_output_artifact_hash": output_hash,
            "expected_projection_preimage_artifact_hash": (
                projection_preimage_hash
            ),
            "current_artifact_hash": current_artifact_hash,
            "directive": (
                "The canonical candidate no longer matches either immutable "
                "Worker boundary. Do not overwrite concurrent or operator bytes."
            ),
        })
    materialization_receipt = worker_workflow.artifacts.materialize(
        str(state.get("output_snapshot_hash") or ""),
        next_dir,
        expected_destination_digest=(
            current_artifact_hash if _tw.Path(next_dir).exists() else None
        ),
    )
    audit_context = _tw.deepcopy(projection.get("audit_context") or {})
    audit_context["durable_worker_output"] = _tw.deepcopy(
        projection.get("durable_worker_output") or {}
    )
    projected = _tw.write_pipeline_checkpoint(
        next_v,
        source_v,
        "workers_done",
        master_plan=_tw.deepcopy(projection.get("master_plan") or {}),
        reviewer_feedback=str(projection.get("reviewer_feedback") or ""),
        worker_failure_count=int(projection.get("worker_failure_count") or 0),
        audit_context=audit_context,
        precommit_rework_count=int(
            projection.get("precommit_rework_count") or 0
        ),
        official_rework_count=int(
            projection.get("official_rework_count") or 0
        ),
        expected_checkpoint_revision=int(contract.get("checkpoint_revision") or 0),
        expected_checkpoint_stage=str(contract.get("checkpoint_stage") or ""),
        expected_workflow_run_id=str(contract.get("workflow_run_id") or ""),
        candidate_artifact_hash=current_artifact_hash,
        candidate_manifest_digest=current_artifact_hash,
        charter_digest=str(
            ((checkpoint or {}).get("repo_baseline") or {})
            .get("evaluation_contract", {})
            .get("hash")
            or ""
        ),
    )
    if not projected:
        current_checkpoint = _tw._matching_checkpoint(next_v, source_v)
        if _durable_output_already_projected(current_checkpoint, projection):
            if (
                current_checkpoint.get("stage") == "workers_done"
                and _tw._complete_artifact_fingerprint(next_dir) != output_hash
            ):
                return _tw._json_tool_result({
                    "error": "DURABLE_WORKER_CONCURRENT_PROJECTION_ARTIFACT_MISMATCH",
                    "success": False,
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "expected_output_artifact_hash": output_hash,
                    "current_artifact_hash": _tw._complete_artifact_fingerprint(next_dir),
                })
            worker_workflow.projected("workers_done")
            return _tw._json_tool_result({
                "success": True,
                "durable_recovery": "confirmed_concurrent_worker_projection",
                "current_checkpoint_stage": current_checkpoint.get("stage"),
                "output_artifact_hash": output_hash,
                "next_v": next_v,
                "source_v": source_v,
            })

        if not materialization_receipt.installed:
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_PREEXISTED_FAILED_CHECKPOINT_CAS",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "output_artifact_hash": output_hash,
                "materialization_receipt_digest": (
                    materialization_receipt.receipt_digest
                ),
                "directive": (
                    "The output bytes predated this command, so this command has "
                    "no authority to roll them back after losing the checkpoint CAS."
                ),
            })

        # Candidate bytes and checkpoint projection are one semantic effect.
        # If the CAS lost, restore the exact immutable preimage, but only while
        # the canonical tree is still the output written by this command.  A
        # different hash proves a concurrent writer and must never be clobbered.
        post_cas_artifact_hash = _tw._complete_artifact_fingerprint(next_dir)
        if post_cas_artifact_hash != output_hash:
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_PROJECTION_CONCURRENT_DRIFT",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "expected_output_artifact_hash": output_hash,
                "current_artifact_hash": post_cas_artifact_hash,
                "directive": (
                    "The checkpoint CAS failed and another writer changed the "
                    "candidate. Preserve both histories for operator reconciliation."
                ),
            })
        try:
            worker_workflow.artifacts.materialize(
                projection_preimage_snapshot,
                next_dir,
                expected_destination_digest=output_hash,
            )
        except BaseException as exc:
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_ROLLBACK_FAILED",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
        restored_hash = _tw._complete_artifact_fingerprint(next_dir)
        if restored_hash != projection_preimage_hash:
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_ROLLBACK_MISMATCH",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "expected_projection_preimage_artifact_hash": (
                    projection_preimage_hash
                ),
                "restored_artifact_hash": restored_hash,
            })
        return _tw._json_tool_result({
            "error": "DURABLE_WORKER_OUTPUT_PROJECTION_FAILED",
            "success": False,
            "action": "retry_same_tool",
            "next_v": next_v,
            "source_v": source_v,
            "output_artifact_hash": state.get("output_artifact_hash"),
            "canonical_artifact_restored": True,
            "restored_artifact_hash": restored_hash,
            "directive": (
                "The immutable Worker output receipt is safe. Retry execute_workers "
                "to project it; the LLM will not be called again."
            ),
        })
    post_commit_artifact_hash = _tw._complete_artifact_fingerprint(next_dir)
    if post_commit_artifact_hash != output_hash:
        return _tw._json_tool_result({
            "error": "DURABLE_WORKER_POST_COMMIT_ARTIFACT_MISMATCH",
            "success": False,
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "expected_output_artifact_hash": output_hash,
            "current_artifact_hash": post_commit_artifact_hash,
        })
    worker_workflow.projected("workers_done")
    return _tw._json_tool_result({
        "success": True,
        "durable_recovery": "projected_existing_worker_output",
        "output_artifact_hash": state.get("output_artifact_hash"),
        "next_v": next_v,
        "source_v": source_v,
    })


async def _project_durable_worker_failure(worker_workflow, state):
    """Project a semantic failure receipt before another Worker cycle can open."""
    projection = _tw.deepcopy(state.get("failure_projection") or {})
    envelope = state.get("envelope") or {}
    next_v = int(envelope.get("next_v"))
    source_v = int(envelope.get("source_v"))
    contract = projection.get("checkpoint_contract") or {}
    checkpoint = _tw._matching_checkpoint(next_v, source_v)
    target_stage = str(projection.get("stage") or "repair_planned")
    receipt = (
        (checkpoint.get("audit_context") or {}).get("durable_worker_failure")
        if isinstance(checkpoint, dict)
        and isinstance(checkpoint.get("audit_context"), dict)
        else None
    )
    expected_receipt = projection.get("durable_worker_failure") or {}
    already_projected = bool(
        isinstance(checkpoint, dict)
        and str(
            checkpoint.get("workflow_run_id")
            or checkpoint.get("run_id")
            or ""
        ) == str(contract.get("workflow_run_id") or "")
        and isinstance(receipt, dict)
        and receipt.get("envelope_digest")
        == expected_receipt.get("envelope_digest")
        and receipt.get("semantic_attempt")
        == expected_receipt.get("semantic_attempt")
    )
    if not already_projected:
        if not _durable_checkpoint_contract_matches(checkpoint, contract):
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_FAILURE_PROJECTION_CONFLICT",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
            })
        audit_context = _tw.deepcopy(projection.get("audit_context") or {})
        audit_context["durable_worker_failure"] = expected_receipt
        checkpoint_kwargs = {}
        if target_stage == "direction_audited" and projection.get(
            "runtime_contract_ledger_digest"
        ):
            checkpoint_kwargs = {
                "reset_runtime_contract_ledger": True,
                "expected_runtime_contract_ledger_digest": projection[
                    "runtime_contract_ledger_digest"
                ],
                "runtime_contract_ledger_reset_reason": (
                    "master_plan_rejected_replan"
                ),
            }
        written = _tw.write_pipeline_checkpoint(
            next_v,
            source_v,
            target_stage,
            master_plan=_tw.deepcopy(projection.get("master_plan") or {}),
            direction_audit=projection.get("direction_audit"),
            reviewer_feedback=str(projection.get("reviewer_feedback") or ""),
            worker_failure_count=int(projection.get("worker_failure_count") or 0),
            audit_context=audit_context,
            precommit_rework_count=int(
                projection.get("precommit_rework_count") or 0
            ),
            official_rework_count=int(
                projection.get("official_rework_count") or 0
            ),
            touch_stage_timestamp=True,
            expected_checkpoint_revision=int(
                contract.get("checkpoint_revision") or 0
            ),
            expected_checkpoint_stage=str(contract.get("checkpoint_stage") or ""),
            expected_workflow_run_id=str(contract.get("workflow_run_id") or ""),
            **checkpoint_kwargs,
        )
        if not written:
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_FAILURE_PROJECTION_FAILED",
                "success": False,
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
            })
    evidence = projection.get("evidence") or {}
    if target_stage == "direction_audited":
        worker_workflow.supersede(
            "initial_worker_semantic_failure_requires_master_replan",
            evidence,
            stage=target_stage,
        )
    else:
        worker_workflow.failure_projected(target_stage)
    try:
        import logging
        _log = logging.getLogger("pok.planning_worker")
        _tw._log.error(
            "Worker semantic failure projected: failure_class=%s boundary_errors=%s",
            "semantic",
            evidence.get("boundary_errors") or [],
        )
        import event_bus
        event_bus.emit(
            "pipeline.worker_semantic_failure_projected",
            "error",
            "Worker semantic failure projected",
            failure_class="semantic",
            boundary_errors=evidence.get("boundary_errors") or [],
            semantic_attempt=evidence.get("semantic_attempt"),
            next_v=next_v,
            source_v=source_v,
            next_stage=target_stage,
        )
    except Exception:
        pass
    return _tw._json_tool_result({
        "success": False,
        "failure_class": "semantic",
        "next_v": next_v,
        "source_v": source_v,
        "next_stage": target_stage,
        "boundary_errors": evidence.get("boundary_errors") or [],
    })


def _dispatch_tasks_from_envelope(envelope):
    """Dispatch-copy of a frozen Worker envelope's tasks (R3, F2 fix 2026-10-09).

    The envelope bytes and every frozen-input digest (Phase C's
    ``frozen_worker_input.get("tasks") != tasks`` drift comparison,
    ``_worker_execution_task_digest``, checkpoint contracts) must keep
    seeing the ORIGINAL planned tasks. The repair-contract satisfiability
    flip happens ONLY on this dispatch copy, immediately before
    ``_execute_workers`` consumes it — that covers BOTH dispatch paths
    (fresh rework preparation and frozen/durable resume) including old
    pre-fix tuner repair contracts that a resumed checkpoint may still
    carry, without ever introducing frozen-input drift (which used to
    abandon the generation) and without bypassing the second line of
    defense. Non-repair innovation tasks are returned untouched.
    """
    tasks = _tw.deepcopy(envelope.get("tasks") or [])
    from worker_role_policy import enforce_repair_contract_satisfiability

    try:
        next_v = int(envelope.get("next_v") or 0) or None
    except (TypeError, ValueError):
        next_v = None
    return [
        enforce_repair_contract_satisfiability(task, next_v)
        for task in tasks
    ]


async def _run_durable_worker_effect(
    worker_workflow,
    envelope,
    next_dir,
    worker_template,
):
    """Run exactly one fenced Worker activity from a frozen envelope."""
    from agent_workers import WorkerInfrastructureError
    from llm_availability import LLMAvailabilityBlocked
    from worker_boundary import (
        diff_file_snapshot,
        restore_complete_artifact_snapshot,
        snapshot_python_files,
    )

    next_v = int(envelope["next_v"])
    source_v = int(envelope["source_v"])
    tasks = _dispatch_tasks_from_envelope(envelope)
    reviewer_feedback = str(envelope.get("reviewer_feedback") or "")
    policy = _tw.deepcopy(envelope.get("execution_policy") or {})
    contract = envelope.get("checkpoint_contract") or {}
    checkpoint = _tw._matching_checkpoint(next_v, source_v)
    if not _durable_checkpoint_contract_matches(checkpoint, contract):
        worker_workflow.abandon("worker_checkpoint_contract_drift_before_claim")
        return _tw._json_tool_result({
            "error": "DURABLE_WORKER_CHECKPOINT_CONTRACT_DRIFT",
            "success": False,
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
        })
    _eb = checkpoint.get("epoch_binding") or {}
    _source_inherited = bool(_eb.get("source_artifact_inherited", True))
    source_hash = (
        _tw._complete_artifact_fingerprint(next_dir)
        if not _source_inherited
        else _tw._complete_artifact_fingerprint(_tw.get_bot_dir(source_v))
    )
    if source_hash != str(envelope.get("source_artifact_hash") or ""):
        worker_workflow.abandon("worker_source_artifact_drift_before_claim")
        return _tw._json_tool_result({
            "error": "DURABLE_WORKER_SOURCE_ARTIFACT_DRIFT",
            "success": False,
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
            "expected_source_hash": envelope.get("source_artifact_hash"),
            "current_source_hash": source_hash,
        })

    _worker_uses_llm = policy.get("executor") != "system_policy_bootstrap_v1"
    if _worker_uses_llm:
        try:
            from llm_availability_store import active_llm_pause

            active_pause = active_llm_pause()
        except Exception as exc:
            return _tw._json_tool_result({
                "error": "LLM_AVAILABILITY_STATE_INVALID",
                "success": False,
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": (
                    "The provider pause record is invalid. No Worker effect "
                    "was claimed."
                ),
            })
        if active_pause is not None:
            state = worker_workflow.state()
            return _tw._json_tool_result({
                "error": "LLM_AVAILABILITY_BLOCKED",
                "success": False,
                "failure_class": "availability",
                "action": "wait_for_llm_availability",
                "next_v": next_v,
                "source_v": source_v,
                "worker_status": state.get("status"),
                "attempt": int(state.get("attempt") or 0),
                "max_attempts": int(state.get("max_attempts") or 0),
                "effect_id": state.get("effect_id"),
                "availability": active_pause,
                "directive": (
                    "The provider pause became active before lease claim. No "
                    "Worker attempt was consumed."
                ),
            })

    lease_owner = f"pid:{_tw.os.getpid()}"
    # The Worker effect lease must outlive the longest single Worker attempt
    # (600s, QUALITY_REWORK_WORKER_TIMEOUT) but stay well under the 4h cycle
    # timeout. A 1h lease created an oversized dead-owner window: a host crash
    # mid-attempt left the effect row "running" under a dead PID until
    # wall-clock expiry, forcing every resumed cycle into the busy branch for
    # up to an hour. 900s covers the 600s attempt plus scheduling/jitter while
    # bounding the dead-owner window to ~15 minutes.
    try:
        from workflow_kernel import WorkflowBusy as _WorkerEffectWorkflowBusy
    except Exception:  # pragma: no cover - defensive import
        _WorkerEffectWorkflowBusy = None
    try:
        lease = worker_workflow.request_or_claim(
            owner=lease_owner,
            lease_seconds=900,
        )
    except Exception as exc:
        # A transient WorkflowBusy (effect lease still active, typically held by
        # a dead owner after a crash/reboot until its wall-clock lease expires)
        # must NOT collapse into the generic infrastructure
        # DURABLE_WORKER_EFFECT_CLAIM_FAILED bucket: that result is classified
        # as retry_same_tool and re-dispatches execute_workers every cycle until
        # the lease expires, producing a noisy unbounded loop with no LLM burn.
        # Surface it as a distinct transient busy result so the router waits
        # for the lease window instead of hammering the claim. Mirrors the
        # WORKER_COMMAND_BUSY handling already used at the command-lock site.
        if (
            _WorkerEffectWorkflowBusy is not None
            and isinstance(exc, _WorkerEffectWorkflowBusy)
        ):
            return _tw._json_tool_result({
                "error": "WORKER_EFFECT_LEASE_BUSY",
                "failure_class": "infrastructure",
                "action": "retry_same_tool",
                "transient": True,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "The Worker effect lease is still active (commonly held by "
                    "a dead owner after a crash/reboot until its wall-clock "
                    "expiry). Retry without editing the candidate; the lease "
                    "self-expires and the next claim succeeds."
                ),
            })
        return _tw._json_tool_result({
            "error": "DURABLE_WORKER_EFFECT_CLAIM_FAILED",
            "failure_class": "infrastructure",
            "action": "retry_same_tool",
            "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            "next_v": next_v,
            "source_v": source_v,
        })

    workspace = None
    availability_defer_failed = False
    operator_shutdown_observed = False
    try:
        if _worker_uses_llm:
            try:
                from llm_availability_store import active_llm_pause

                active_pause = active_llm_pause()
            except Exception as exc:
                with worker_workflow.store.command_lock(
                    worker_workflow.run_id,
                    blocking=True,
                ):
                    worker_workflow.availability_deferred(
                        lease,
                        {
                            "schema_version": 1,
                            "active": True,
                            "category": "availability_control_invalid",
                            "summary": (
                                "provider pause state could not be read after claim"
                            ),
                            "evidence_digest": _tw.hashlib.sha256(
                                (
                                    f"{type(exc).__name__}:"
                                    f"{str(exc)[:300]}"
                                ).encode("utf-8")
                            ).hexdigest(),
                            "persistence_error": (
                                f"{type(exc).__name__}: {str(exc)[:300]}"
                            ),
                        },
                    )
                return _tw._json_tool_result({
                    "error": "LLM_AVAILABILITY_STATE_INVALID",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "effect_id": lease.effect_id,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        worker_workflow.state().get("attempt") or 0
                    ),
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
            if active_pause is not None:
                with worker_workflow.store.command_lock(
                    worker_workflow.run_id,
                    blocking=True,
                ):
                    deferred_state = worker_workflow.availability_deferred(
                        lease,
                        active_pause,
                    )
                return _tw._json_tool_result({
                    "error": "LLM_AVAILABILITY_BLOCKED",
                    "success": False,
                    "failure_class": "availability",
                    "action": "wait_for_llm_availability",
                    "next_v": next_v,
                    "source_v": source_v,
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        deferred_state.get("attempt") or 0
                    ),
                    "max_attempts": lease.max_attempts,
                    "availability": active_pause,
                    "directive": (
                        "The provider pause became active at the claim boundary. "
                        "The lease was deferred without consuming an attempt."
                    ),
                })
        workspace = worker_workflow.artifacts.workspace_for(
            lease,
            str(envelope.get("prepared_snapshot_hash") or ""),
        )
        task_skipper = None
        if policy.get("quality_skipper"):
            task_skipper = _tw._quality_rework_skipper(
                workspace,
                _tw.get_bot_dir(source_v),
                next_v,
                source_v,
                expected_architecture_policy=policy.get(
                    "expected_architecture_policy"
                ),
                master_plan=_tw.deepcopy(envelope.get("projection_plan") or {}),
            )
        baseline = snapshot_python_files(workspace)
        ui = _tw._get_ui()
        system_worker_receipt = None
        try:
            if policy.get("executor") == "system_policy_bootstrap_v1":
                from system_strict_bootstrap import (
                    apply_blueprint,
                    bind_worker_effect_receipt,
                )

                worker_snapshots, audit_focus_areas, system_worker_receipt = (
                    apply_blueprint(
                        workspace,
                        checkpoint=checkpoint,
                        envelope=envelope,
                    )
                )
                system_worker_receipt = bind_worker_effect_receipt(
                    system_worker_receipt,
                    effect_id=lease.effect_id,
                    lease_epoch=lease.lease_epoch,
                )
                success = True
                ui.log_history(
                    "Applied the content-bound strict-v1 consumer blueprint "
                    "without invoking an LLM Worker.",
                    "info",
                )
            else:
                success, worker_snapshots, audit_focus_areas = await _tw._execute_workers(
                    tasks,
                    worker_template,
                    workspace,
                    next_v,
                    [],
                    ui,
                    reviewer_feedback=reviewer_feedback,
                    source_v=source_v,
                    force_sequential=bool(policy.get("force_sequential")),
                    task_skipper=task_skipper,
                    worker_effect_identity={
                        "workflow_run_id": str(
                            checkpoint.get("workflow_run_id") or ""
                        ),
                        "envelope_digest": str(
                            envelope.get("envelope_digest") or ""
                        ),
                        "effect_id": str(lease.effect_id),
                        "lease_epoch": int(lease.lease_epoch),
                    },
                )
        except BaseException as exc:
            rollback_error = ""
            try:
                restore_complete_artifact_snapshot(workspace, baseline)
            except BaseException as rollback_exc:
                rollback_error = (
                    f"{type(rollback_exc).__name__}: {str(rollback_exc)[:300]}"
                )
            # Only a contemporaneous cancellation plus the owner-fenced
            # process shutdown edge is attempt-neutral.  An unexpected Claude
            # SIGTERM is also surfaced as CancelledError, but with no shutdown
            # edge it continues through the ordinary failure path below.
            import asyncio as _asyncio
            from llm_query import is_operator_shutdown_requested

            if (
                isinstance(exc, _asyncio.CancelledError)
                and is_operator_shutdown_requested()
            ):
                operator_shutdown_observed = True
                try:
                    shutdown_deadline = _tw.time.monotonic() + 10.0
                    with worker_workflow.store.command_lock(
                        worker_workflow.run_id,
                        blocking=True,
                        deadline_monotonic=shutdown_deadline,
                    ):
                        interrupted_state = (
                            worker_workflow.operator_shutdown_interrupted(
                                lease,
                                owner=lease_owner,
                                deadline_monotonic=shutdown_deadline,
                            )
                        )
                except Exception as interrupt_exc:
                    return _tw._json_tool_result({
                        "error": "WORKER_OPERATOR_SHUTDOWN_PERSIST_FAILED",
                        "success": False,
                        "failure_class": "control_plane",
                        "action": "operator_reconcile",
                        "recovery_blocked": True,
                        "checkpoint_preserved": True,
                        "attempt_neutral_persisted": False,
                        "next_v": next_v,
                        "source_v": source_v,
                        "workflow_run_id": worker_workflow.run_id,
                        "effect_id": lease.effect_id,
                        "lease_epoch": lease.lease_epoch,
                        "claimed_attempt": lease.attempt,
                        "message": (
                            f"{type(interrupt_exc).__name__}: "
                            f"{str(interrupt_exc)[:300]}"
                        ),
                        "rollback_error": rollback_error,
                        "validation_errors": [
                            "worker_operator_shutdown_receipt_not_durable"
                        ],
                        "directive": (
                            "The process shutdown edge was observed, but its exact "
                            "attempt-neutral Worker receipt was not durable. Preserve "
                            "the running lease and reconcile it; never translate this "
                            "ambiguity into EffectFailed or abandon the generation."
                        ),
                    })
                return _tw._json_tool_result({
                    "error": "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED",
                    "success": False,
                    "failure_class": "operator_shutdown",
                    "action": "retry_same_tool",
                    "pending": True,
                    "shutdown_requested": True,
                    "checkpoint_preserved": True,
                    "attempt_consumed": False,
                    "attempt_neutral_persisted": True,
                    "next_v": next_v,
                    "source_v": source_v,
                    "workflow_run_id": worker_workflow.run_id,
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        interrupted_state.get("attempt") or 0
                    ),
                    "max_attempts": lease.max_attempts,
                    "rollback_error": rollback_error,
                    "directive": (
                        "The operator stopped this process. The exact Worker lease "
                        "was fenced and returned to the same frozen envelope without "
                        "consuming an attempt; a fresh process may claim it."
                    ),
                })
            if isinstance(exc, _tw.LLMAvailabilityBlocked):
                pause_state = exc.pause_state()
                # Fence and release the Worker lease *before* publishing the
                # cross-process pause.  If the process dies immediately after
                # the pause file is fsynced, replay already sees EffectDeferred
                # and the claim's attempt increment has been rolled back.
                try:
                    with worker_workflow.store.command_lock(
                        worker_workflow.run_id,
                        blocking=True,
                    ):
                        deferred_state = (
                            worker_workflow.availability_deferred(
                                lease,
                                pause_state,
                            )
                        )
                except Exception as defer_exc:
                    availability_defer_failed = True
                    return _tw._json_tool_result({
                        "error": "WORKER_AVAILABILITY_DEFER_FAILED",
                        "success": False,
                        "failure_class": "control_plane",
                        "action": "operator_reconcile",
                        "next_v": next_v,
                        "source_v": source_v,
                        "message": (
                            f"{type(defer_exc).__name__}: "
                            f"{str(defer_exc)[:300]}"
                        ),
                        "persistence_error": "",
                        "rollback_error": rollback_error,
                        "directive": (
                            "The LLM availability pause could not be fenced into "
                            "the durable Worker journal. Do not classify or retry "
                            "it as a Worker infrastructure failure."
                        ),
                    })
                persistence_error = ""
                try:
                    from llm_availability_store import persist_llm_pause

                    pause_state = persist_llm_pause(pause_state)
                except Exception as pause_exc:
                    persistence_error = (
                        f"{type(pause_exc).__name__}: {str(pause_exc)[:300]}"
                    )
                    return _tw._json_tool_result({
                        "error": "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
                        "success": False,
                        "failure_class": "control_plane",
                        "action": "operator_reconcile",
                        "next_v": next_v,
                        "source_v": source_v,
                        "effect_id": lease.effect_id,
                        "lease_epoch": lease.lease_epoch,
                        "claimed_attempt": lease.attempt,
                        "restored_attempt": int(
                            deferred_state.get("attempt") or 0
                        ),
                        "max_attempts": lease.max_attempts,
                        "availability": exc.pause_state(),
                        "persistence_error": persistence_error,
                        "rollback_error": rollback_error,
                        "directive": (
                            "The Worker lease is safely deferred and attempt-neutral, "
                            "but the global pause was not published. Reconcile the "
                            "pause record before resuming this exact effect."
                        ),
                    })
                return _tw._json_tool_result({
                    "error": "LLM_AVAILABILITY_BLOCKED",
                    "success": False,
                    "failure_class": "availability",
                    "action": "wait_for_llm_availability",
                    "next_v": next_v,
                    "source_v": source_v,
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        deferred_state.get("attempt") or 0
                    ),
                    "max_attempts": lease.max_attempts,
                    "availability": pause_state,
                    "persistence_error": persistence_error,
                    "rollback_error": rollback_error,
                    "directive": (
                        "The provider is unavailable. The Worker lease was "
                        "released without consuming an attempt; resume only "
                        "through the content-bound LLM availability control."
                    ),
                })

            from system_strict_bootstrap import (
                SystemStrictBootstrapError,
            )

            if isinstance(exc, SystemStrictBootstrapError):
                try:
                    with worker_workflow.store.command_lock(worker_workflow.run_id):
                        worker_workflow.execution_failed(
                            lease,
                            list(exc.errors),
                            retryable=False,
                        )
                    worker_workflow.abandon(
                        "system_strict_bootstrap_execution_failed"
                    )
                except Exception:
                    pass
                return _tw._json_tool_result({
                    "error": "SYSTEM_STRICT_BOOTSTRAP_EXECUTION_FAILED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "abandon_generation",
                    "next_v": next_v,
                    "source_v": source_v,
                    "validation_errors": list(exc.errors),
                    "rollback_error": rollback_error,
                    "directive": (
                        "The checked-in blueprint failed its exact workspace or output "
                        "identity. Abandon; never retry it as an LLM Worker."
                    ),
                })
            if isinstance(exc, WorkerInfrastructureError) and not rollback_error:
                with worker_workflow.store.command_lock(worker_workflow.run_id):
                    failed_state = worker_workflow.infrastructure_failed(
                        lease,
                        exc.issues,
                    )
                exhausted = failed_state.get("status") == "exhausted"
                if exhausted:
                    worker_workflow.abandon("worker_infrastructure_exhausted")
                return _tw._json_tool_result({
                    **(
                        {"error": "WORKER_INFRASTRUCTURE_EXHAUSTED"}
                        if exhausted
                        else {}
                    ),
                    "success": False,
                    "failure_class": "infrastructure",
                    "action": (
                        "abandon_generation" if exhausted else "retry_same_tool"
                    ),
                    "attempt": lease.attempt,
                    "max_attempts": lease.max_attempts,
                    "attempt_key": envelope.get("envelope_digest"),
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "next_v": next_v,
                    "source_v": source_v,
                })
            issues = [
                f"{type(exc).__name__}: {str(exc)[:500]}",
                *( [f"rollback: {rollback_error}"] if rollback_error else [] ),
            ]
            with worker_workflow.store.command_lock(worker_workflow.run_id):
                failed_state = worker_workflow.execution_failed(
                    lease,
                    issues,
                    retryable=not bool(rollback_error),
                )
            if rollback_error or failed_state.get("status") == "exhausted":
                worker_workflow.abandon("worker_harness_failure")
            return _tw._json_tool_result({
                "error": (
                    "WORKER_BATCH_EXCEPTION_ROLLBACK_FAILED"
                    if rollback_error
                    else "DURABLE_WORKER_HARNESS_FAILED"
                ),
                "success": False,
                "failure_class": "infrastructure",
                "action": (
                    "abandon_generation"
                    if rollback_error or failed_state.get("status") == "exhausted"
                    else "retry_same_tool"
                ),
                "next_v": next_v,
                "source_v": source_v,
                "message": "; ".join(issues),
            })

        boundary_errors = []
        policy_identity_refresh_receipt = None
        if success:
            changed = diff_file_snapshot(workspace, baseline)
            if not changed:
                success = False
                boundary_errors.append({"type": "worker_zero_artifact_changes"})
        if success:
            boundary_errors = _tw._validate_worker_boundaries(
                tasks,
                source_v,
                next_v,
                worker_snapshots=worker_snapshots,
                candidate_dir=workspace,
                source_artifact_inherited=_source_inherited,
            )
            success = not boundary_errors
        if success:
            # The model-facing boundary has now proved that only policy.py was
            # candidate-written (the deterministic v143 bootstrap has already
            # proved its exact three-file blueprint separately).  Only after
            # that proof may the host remove compiler caches and rebuild the
            # two digest-bound identities.  Cache cleanup is host-owned because
            # the Worker is required to leave ``py_compile`` output in place.
            try:
                from bot_artifact import canonical_digest
                from bot_namespace import (
                    SYSTEM_DERIVED_IDENTITY_FILES,
                    refresh_policy_identity_documents,
                    strict_lineage_parent_versions,
                )

                pre_refresh_changed = sorted(changed)
                expected_pre_refresh = (
                    {"policy.py", *SYSTEM_DERIVED_IDENTITY_FILES}
                    if policy.get("executor") == "system_policy_bootstrap_v1"
                    else {"policy.py"}
                )
                if set(pre_refresh_changed) != expected_pre_refresh:
                    raise RuntimeError(
                        "candidate change set before identity refresh mismatch: "
                        f"expected={sorted(expected_pre_refresh)}:"
                        f"actual={pre_refresh_changed}"
                    )
                _tw._cleanup_worker_transients_before_identity_refresh(workspace)
                lineage_parents = strict_lineage_parent_versions(
                    next_v,
                    source_v,
                    checkpoint.get("parent2_v"),
                )
                identity = refresh_policy_identity_documents(
                    workspace,
                    next_v,
                    parent_versions=lineage_parents,
                )
                final_changed = diff_file_snapshot(workspace, baseline)
                expected_final = {"policy.py", *SYSTEM_DERIVED_IDENTITY_FILES}
                if set(final_changed) != expected_final:
                    raise RuntimeError(
                        "final strict artifact delta mismatch: "
                        f"expected={sorted(expected_final)}:actual={final_changed}"
                    )
                receipt_subject = {
                    "schema_version": 1,
                    "kind": "strict-policy-identity-refresh-v1",
                    "version": next_v,
                    "parent_versions": list(lineage_parents),
                    "candidate_changed_files": ["policy.py"],
                    "system_derived_files": sorted(SYSTEM_DERIVED_IDENTITY_FILES),
                    "final_changed_files": final_changed,
                    "runtime_manifest_digest": identity[
                        "runtime_manifest_digest"
                    ],
                    "epoch_receipt_digest": identity["epoch_receipt_digest"],
                    "envelope_digest": envelope.get("envelope_digest"),
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                }
                policy_identity_refresh_receipt = {
                    **receipt_subject,
                    "receipt_digest": canonical_digest(receipt_subject),
                }
            except Exception as exc:
                rollback_error = ""
                try:
                    restore_complete_artifact_snapshot(workspace, baseline)
                except Exception as rollback_exc:
                    rollback_error = (
                        f"{type(rollback_exc).__name__}: "
                        f"{str(rollback_exc)[:300]}"
                    )
                issue = (
                    "system policy identity refresh failed: "
                    f"{type(exc).__name__}: {str(exc)[:500]}"
                )
                with worker_workflow.store.command_lock(worker_workflow.run_id):
                    failed_state = worker_workflow.execution_failed(
                        lease,
                        [issue, *([f"rollback: {rollback_error}"] if rollback_error else [])],
                        retryable=not bool(rollback_error),
                    )
                if rollback_error or failed_state.get("status") == "exhausted":
                    worker_workflow.abandon("system_policy_identity_refresh_failed")
                return _tw._json_tool_result({
                    "error": "SYSTEM_POLICY_IDENTITY_REFRESH_FAILED",
                    "success": False,
                    "failure_class": "infrastructure",
                    "action": (
                        "abandon_generation"
                        if rollback_error or failed_state.get("status") == "exhausted"
                        else "retry_same_tool"
                    ),
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": issue,
                    "rollback_error": rollback_error,
                })
        if success:
            try:
                _tw._clear_compiled_task_context(workspace)
            except Exception as exc:
                success = False
                boundary_errors.append({
                    "type": "transient_control_artifact_cleanup_failed",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })

        if not success:
            try:
                restore_complete_artifact_snapshot(workspace, baseline)
            except Exception as exc:
                worker_workflow.execution_failed(
                    lease,
                    [f"semantic rollback failed: {type(exc).__name__}: {exc}"],
                    retryable=False,
                )
                worker_workflow.abandon("worker_semantic_rollback_failed")
                return _tw._json_tool_result({
                    "error": "WORKER_BATCH_ROLLBACK_FAILED",
                    "success": False,
                    "action": "abandon_generation",
                    "next_v": next_v,
                    "source_v": source_v,
                })
            evidence = {
                "boundary_errors": boundary_errors,
                "audit_focus_areas": audit_focus_areas,
                "worker_reported_success": False,
            }
            target_stage = (
                "repair_planned" if reviewer_feedback else "direction_audited"
            )
            next_failure_count = int(envelope.get("worker_failure_count") or 0) + 1
            audit_context = _tw.deepcopy(envelope.get("audit_context") or {})
            failure_plan = (
                _tw.deepcopy(envelope.get("projection_plan") or {})
                if reviewer_feedback
                else {}
            )
            if not reviewer_feedback:
                audit_context["worker_execution_failed_replan"] = {
                    "failed_tasks": [
                        {
                            "worker_id": task.get("worker_id"),
                            "role": task.get("role"),
                            "target_files": task.get("target_files", []),
                        }
                        for task in tasks[:5]
                    ],
                    "worker_failure_count": next_failure_count,
                }
            failure_projection = {
                "schema_version": 1,
                "stage": target_stage,
                "checkpoint_contract": _tw.deepcopy(contract),
                "master_plan": failure_plan,
                "direction_audit": checkpoint.get("direction_audit"),
                "reviewer_feedback": reviewer_feedback,
                "worker_failure_count": next_failure_count,
                "audit_context": audit_context,
                "precommit_rework_count": int(
                    envelope.get("precommit_rework_count") or 0
                ),
                "official_rework_count": int(
                    envelope.get("official_rework_count") or 0
                ),
                "runtime_contract_ledger_digest": (
                    _tw._checkpoint_runtime_contract_ledger_digest(checkpoint)
                    if target_stage == "direction_audited"
                    and checkpoint.get("runtime_contract_ledger") is not None
                    else ""
                ),
                "evidence": evidence,
                "durable_worker_failure": {
                    "envelope_digest": envelope.get("envelope_digest"),
                    "semantic_attempt": int(
                        worker_workflow.state().get("semantic_attempt") or 0
                    ) + 1,
                },
            }
            with worker_workflow.store.command_lock(worker_workflow.run_id):
                semantic_state = worker_workflow.semantic_failed(
                    lease,
                    evidence,
                    projection=failure_projection,
                )
                return await _project_durable_worker_failure(
                    worker_workflow,
                    semantic_state,
                )

        try:
            artifact_hash = _tw._complete_artifact_fingerprint(workspace)
            snapshot_hash = worker_workflow.artifacts.capture(workspace)
            if not artifact_hash or artifact_hash != snapshot_hash:
                raise RuntimeError("Worker output snapshot mismatch")
        except Exception as exc:
            worker_workflow.execution_failed(
                lease,
                [f"output capture failed: {type(exc).__name__}: {exc}"],
                retryable=True,
            )
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_CAPTURE_FAILED",
                "success": False,
                "failure_class": "infrastructure",
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
            })
        audit_context = _tw.deepcopy(envelope.get("audit_context") or {})
        if audit_focus_areas:
            audit_context["worker_cot_focus_areas"] = audit_focus_areas
        if system_worker_receipt is not None:
            audit_context["system_strict_bootstrap_worker"] = (
                system_worker_receipt
            )
        if policy_identity_refresh_receipt is not None:
            policy_identity_refresh_receipt = {
                **policy_identity_refresh_receipt,
                "output_artifact_hash": artifact_hash,
            }
            from bot_artifact import canonical_digest

            policy_identity_refresh_receipt["receipt_digest"] = canonical_digest({
                key: value
                for key, value in policy_identity_refresh_receipt.items()
                if key != "receipt_digest"
            })
            audit_context["strict_policy_identity_refresh"] = (
                policy_identity_refresh_receipt
            )
        projection = {
            "schema_version": 1,
            "checkpoint_contract": _tw.deepcopy(contract),
            "master_plan": _tw.deepcopy(envelope.get("projection_plan") or {}),
            "reviewer_feedback": reviewer_feedback,
            "worker_failure_count": int(envelope.get("worker_failure_count") or 0),
            "audit_context": audit_context,
            "precommit_rework_count": int(
                envelope.get("precommit_rework_count") or 0
            ),
            "official_rework_count": int(
                envelope.get("official_rework_count") or 0
            ),
            "durable_worker_output": {
                "artifact_hash": artifact_hash,
                "snapshot_hash": snapshot_hash,
                "envelope_digest": envelope.get("envelope_digest"),
                "effect_id": lease.effect_id,
                "lease_epoch": lease.lease_epoch,
            },
        }
        try:
            with worker_workflow.store.command_lock(worker_workflow.run_id):
                output_state = worker_workflow.output_ready(
                    lease,
                    artifact_hash=artifact_hash,
                    snapshot_hash=snapshot_hash,
                    projection=projection,
                )
                return await _project_durable_worker_output(
                    worker_workflow,
                    next_dir,
                    output_state,
                )
        except Exception as exc:
            try:
                worker_workflow.execution_failed(
                    lease,
                    [f"output receipt failed: {type(exc).__name__}: {exc}"],
                    retryable=True,
                )
            except Exception:
                pass
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_RECEIPT_FAILED",
                "success": False,
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
            })
    finally:
        # Lease-outcome invariant: every path after claim must durably complete,
        # fail, exhaust, or abandon the effect. This guard covers injected
        # failures in workspace creation, validators, receipt construction, and
        # future hooks without relying on each branch remembering cleanup.
        try:
            effect = worker_workflow.store.effect(lease.effect_id)
            if (
                not availability_defer_failed
                and not operator_shutdown_observed
                and effect.get("status") == "running"
                and int(effect.get("lease_epoch") or 0) == int(lease.lease_epoch)
            ):
                worker_workflow.execution_failed(
                    lease,
                    ["Worker activity exited without a durable outcome"],
                    retryable=True,
                )
        except Exception:
            pass
        if workspace is not None:
            try:
                worker_workflow.artifacts.discard_workspace(workspace)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Worker-execution identity contract.
#
# These three helpers define the durable Worker execution identity: the digest
# of every frozen input supplied to one outer Worker batch, and the backend
# (provider/model/endpoint) contract that the frozen execution policy bound the
# batch to. They were previously housed in tool_planning_quality_contracts (the
# Group E quality-contract companion) but they belong to the Worker durable-
# execution business, so they live here as first-class members of the Worker
# module. tool_planning.py re-exports them via its
# ``from tool_planning_worker import (...)`` block, and the existing
# ``from tool_planning_quality_contracts import (...)`` companion surface no
# longer needs to carry them.
# ---------------------------------------------------------------------------
def _worker_execution_task_digest(
    tasks,
    reviewer_feedback,
    worker_template,
):
    """Identity of every frozen input supplied to one outer Worker batch."""
    return _tw.hashlib.sha256(_tw.json.dumps({
        "tasks": tasks,
        "reviewer_feedback": reviewer_feedback,
        "worker_template": worker_template,
    }, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _worker_backend_contract():
    return {
        key: _tw.os.environ.get(key, "")
        for key in (
            "ANTHROPIC_MODEL",
            "CLAUDE_MODEL",
            "POK_LLM_MODEL",
            "ANTHROPIC_BASE_URL",
        )
    }


def _expected_worker_backend_contract(checkpoint, envelope=None):
    """Return the backend identity selected by the frozen execution policy."""
    policy = (
        (envelope or {}).get("execution_policy")
        if isinstance(envelope, dict)
        else None
    ) or {}
    if policy.get("executor") == "system_policy_bootstrap_v1":
        from system_strict_bootstrap import system_worker_backend_contract

        master_receipt = (
            ((checkpoint or {}).get("audit_context") or {}).get(
                "system_strict_bootstrap"
            )
            or {}
        )
        return system_worker_backend_contract(master_receipt)
    return _worker_backend_contract()


#: Bounded FIFO archive read from the pause store's ``resume_receipt_history``
#: (schema 2). A burst of new pauses overwrites the store's single record
#: before a deferred Worker resumes; the archived receipts are the only
#: durable proof that the deferred evidence was reconciled through an
#: allowed resume path (2026-10-05 wedge fix).
#: F-A (2026-10-09): raised 8 -> 64 in lockstep with the store's
#: ``RESUME_RECEIPT_HISTORY_CAP`` so the scan always covers the whole
#: archive; ``_resume_receipt_scan_limit`` additionally takes the max with
#: the live store constant, so the two can never drift apart silently.
_RESUME_RECEIPT_HISTORY_SCAN_LIMIT = 64


def _resume_receipt_scan_limit() -> int:
    try:
        from llm_availability_store import RESUME_RECEIPT_HISTORY_CAP as _cap

        return max(_RESUME_RECEIPT_HISTORY_SCAN_LIMIT, int(_cap))
    except Exception:
        return _RESUME_RECEIPT_HISTORY_SCAN_LIMIT


def _snapshot_now(now) -> "datetime":
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


#: F-M2 (2026-10-09 re-review): the system event emitted whenever the
#: suppressed-evidence chain (not a resume receipt) is what authorizes a
#: deferred Worker effect to resume.  Precedent:
#: ``pipeline.crossover_degraded_to_parent_copy`` (agent_review.py).
WORKER_RESUME_SNAPSHOT_AUTHORIZED_EVENT = (
    "pipeline.worker_resume_snapshot_authorized"
)


def _suppressed_evidence_chain_authorization(
    deferred,
    pause_audit,
    *,
    now: "datetime",
):
    """Store-held suppressed-evidence authorization for a deferred resume.

    F-H (2026-10-09 re-review — the decisive v532 root cause).  The real
    incident shape is *suppressed divergence*, not receipt eviction: the
    Worker exception path freezes ``exc.pause_state()`` (the INCOMING
    projection: ``observed_at``, no ``auto_resume_at``) and then persists it;
    when the store already holds an ACTIVE same-category record the persist
    takes the suppressed-recurrence branch, the store KEEPS the old record
    digest, and the frozen digest only ever lands in
    ``last_suppressed_evidence_digest``.  The frozen digest therefore never
    becomes a record digest and never has a matching resume receipt — no
    archive cap can fix that, and the incident-time archive (2 receipts) was
    nowhere near full.

    The store itself, however, still holds the proof: the suppression marker
    pair (``last_suppressed_category`` +
    ``last_suppressed_evidence_digest``) on the inactive audit record, and —
    once that record is itself replaced — on its archived receipt projection
    (the marker is carried into ``resume_receipt_history`` entries).  That
    chain is *structurally bound to the frozen digest* (F-M1): a forged or
    unrelated digest never matches any marker, so unlike the removed
    capacity-based lane it cannot authorize anything the store never held.
    R1/R2 (2026-10-09 round-3 audit): the marker is a bounded *history list*
    (``last_suppressed_records`` — every genuinely suppressed digest, not
    just the newest), and an ACTIVE marker-bearing record discarded by a
    higher-priority overwrite (1302 storm -> 1308 quota escalation)
    contributes a ``suppressed_marker_eviction`` archive projection, so the
    chain keeps every suppressed digest durable across same-window double
    freezes and priority-replacement alike.

    Authorization is deliberately narrow and fail-closed:

    * the deferred pause must be transient (non-manual) — manual pauses keep
      requiring an exact operator evidence receipt, in the archive or in the
      current record;
    * the live audit record must exist and be inactive (an active pause
      blocks earlier, at the claim boundary; re-checked here so this branch
      can never bypass it);
    * the frozen digest must match the suppression marker chain (current
      record first, then every retained archived receipt projection) with an
      equal category — the structural binding;
    * the deferral snapshot's system-owned resume horizon (see
      ``llm_availability_store.system_resume_horizon``) must have elapsed.

    Returns the authorization descriptor (dict) on success, else ``None``.
    """

    if not isinstance(deferred, dict) or not deferred:
        return None
    if bool(deferred.get("requires_manual_resume")):
        return None
    if not isinstance(pause_audit, dict) or not pause_audit:
        return None
    if pause_audit.get("active") is not False:
        return None
    digest = str(deferred.get("evidence_digest") or "")
    category = str(deferred.get("category") or "")
    if not digest or not category:
        return None
    try:
        from llm_availability_store import system_resume_horizon

        horizon = system_resume_horizon(deferred)
    except Exception:
        return None
    if horizon is None or now < horizon:
        return None

    history = pause_audit.get("resume_receipt_history")
    entries = [entry for entry in history if isinstance(entry, dict)] if isinstance(
        history, list
    ) else []

    def _marker_matches(record) -> bool:
        if (
            str(record.get("last_suppressed_evidence_digest") or "") == digest
            and str(record.get("last_suppressed_category") or "") == category
        ):
            return True
        # R1 (2026-10-09 round-3 audit): the marker is a bounded history list
        # (``last_suppressed_records``), so an earlier freezer survives a later
        # same-window freeze.  Scan every entry on the current record AND on
        # each archived projection — receipt projections carry the list, and
        # ``suppressed_marker_eviction`` projections (R2: an ACTIVE record
        # discarded by a higher-priority overwrite) flatten it.  The binding
        # stays structural: only digests the store genuinely suppressed are
        # ever written into any of these surfaces, so a forged or unrelated
        # digest still matches nothing at any depth.
        entries = record.get("last_suppressed_records")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if (
                    str(entry.get("evidence_digest") or "") == digest
                    and str(entry.get("category") or "") == category
                ):
                    return True
        return False

    matched_where = None
    if _marker_matches(pause_audit):
        matched_where = "current"
    else:
        # Newest-first: a later storm record replaces the suppressed record,
        # so its marker survives only inside the archived receipt projection.
        for projection in reversed(entries):
            if _marker_matches(projection):
                matched_where = "archived"
                break
    if matched_where is None:
        return None
    return {
        "authorization_shape": "suppressed_evidence_chain",
        "evidence_digest": digest,
        "category": category,
        "horizon": horizon.astimezone(timezone.utc).isoformat(),
        "archive_depth": len(entries),
        "matched_record": matched_where,
    }


def _emit_worker_resume_snapshot_authorized(authorization, *, effect_id=None):
    """F-M2 (2026-10-09 re-review): publish one audit event for a snapshot
    (suppressed-evidence) resume authorization.

    The three authorization lanes were previously byte-indistinguishable in
    the journal/events/log surface.  Receipt-based lanes are already audited
    through the store's own records; this event marks the lane that exists
    ONLY in the deferral snapshot + store-held suppression markers.  Best
    effort: an observability failure must never break the resume itself.
    """

    if not isinstance(authorization, dict):
        return
    try:
        from system_log import log_system_event

        log_system_event(
            WORKER_RESUME_SNAPSHOT_AUTHORIZED_EVENT,
            "warn",
            "Deferred Worker availability resume authorized through the "
            "suppressed-evidence chain (no resume receipt exists for this "
            f"digest by construction); effect_id={effect_id or 'unknown'}",
            {
                "effect_id": effect_id,
                "evidence_digest": authorization.get("evidence_digest"),
                # R3 (2026-10-09 round-3 audit): ``category`` is a reserved
                # key on the persisted surface — ``log_system_event`` pops it
                # (``event_bus.emit``'s first positional parameter is named
                # ``category`` = the event type) and the bus then backfills
                # the event type into ``data["category"]``.  The pause
                # category must travel under ``pause_category`` or it never
                # reaches events.jsonl (verified against the real bus).
                "pause_category": authorization.get("category"),
                "authorization_shape": authorization.get("authorization_shape"),
                "archive_depth": authorization.get("archive_depth"),
                "horizon": authorization.get("horizon"),
                "matched_record": authorization.get("matched_record"),
            },
        )
    except Exception:
        pass


def _resume_receipt_identity_errors(deferred, projection):
    """Identity-field checks: does this receipt describe the deferred pause?"""

    errors = []
    for key in ("category", "evidence_digest", "retry_policy", "http_status"):
        if projection.get(key) != deferred.get(key):
            errors.append(f"global_pause_resume_receipt_{key}_mismatch")
    if bool(projection.get("requires_manual_resume")) != bool(
        deferred.get("requires_manual_resume")
    ):
        errors.append("global_pause_resume_receipt_manual_policy_mismatch")
    if not str(projection.get("resumed_at") or ""):
        errors.append("global_pause_resume_receipt_timestamp_missing")
    return errors


def _resume_receipt_policy_errors(
    projection,
    *,
    digest: str,
    manual: bool,
    require_auto_resume_deadline: bool,
):
    """Resume-path policy checks for one receipt projection.

    ``require_auto_resume_deadline`` is False for archived projections: the
    store does not archive ``auto_resume_at``, and an archived
    ``resume_source == "bounded_cooldown_elapsed"`` token is already proof the
    receipt was produced by the system-owned reconcile path (only
    ``_reconcile_llm_pause`` writes that token, and only once the deadline
    elapsed). Manual policy is enforced identically for archived receipts.
    """
    errors = []
    resume_source = str(projection.get("resume_source") or "")
    resume_digest = str(projection.get("resume_evidence_digest") or "")
    if manual:
        if resume_source != "operator_evidence_digest":
            errors.append("manual_pause_operator_receipt_missing")
        if resume_digest != digest:
            errors.append("manual_pause_resume_evidence_digest_mismatch")
    else:
        if resume_source != "bounded_cooldown_elapsed":
            errors.append("transient_pause_cooldown_receipt_missing")
        if resume_digest:
            errors.append("transient_pause_unexpected_operator_digest")
        if require_auto_resume_deadline and not str(
            projection.get("auto_resume_at") or ""
        ):
            errors.append("transient_pause_auto_resume_deadline_missing")
    return errors


def _resume_receipt_projection_errors(
    deferred,
    projection,
    *,
    digest: str,
    manual: bool,
    require_auto_resume_deadline: bool,
):
    return _resume_receipt_identity_errors(
        deferred, projection
    ) + _resume_receipt_policy_errors(
        projection,
        digest=digest,
        manual=manual,
        require_auto_resume_deadline=require_auto_resume_deadline,
    )


def _worker_availability_resume_validation(
    deferred,
    pause_audit,
    *,
    now: datetime | None = None,
):
    """Validate the global resume receipt against the deferred Worker effect.

    Returns ``(errors, authorization)``: ``errors`` empty means the resume is
    authorized; ``authorization`` is a descriptor dict when the authorizing
    lane is the store-held *suppressed-evidence chain* (F-M2 audit trail) and
    ``None`` for the ordinary receipt lanes (current record / archived
    receipt), which are already audited through the store's own records.

    The Worker journal is the authority for *which* provider failure suspended
    this effect.  Absence of an active global pause is therefore necessary but
    not sufficient to resume: the inactive audit record must prove that the
    same evidence was reconciled through the allowed manual/cooldown path.
    Since 2026-10-05 the store also archives the resume receipts of the
    records a later pause overwrote (``resume_receipt_history``); when the
    current record does not match, any single archived projection that matches
    under the same per-record rules authorizes the resume. Manual pauses in
    the archive still require the exact operator evidence digest, and with no
    match anywhere the effect stays deferred (fail-closed semantics are
    unchanged from the single-record contract).

    F-H (2026-10-09 re-review, v532 root cause — suppressed divergence): the
    Worker exception path freezes ``exc.pause_state()`` and persists it onto
    an ACTIVE same-category record, which takes the store's
    suppressed-recurrence branch: the store keeps the old digest and the
    frozen digest only lands in ``last_suppressed_evidence_digest``.  Such a
    frozen digest structurally NEVER has a resume receipt (any cap is
    irrelevant; the incident-time archive held 2 receipts).  The third
    authorization lane therefore matches the frozen digest against the
    store-held suppression marker chain (current record + archived receipt
    projections), requires the deferral snapshot's system-owned resume
    horizon to have elapsed, and does NOT require the archive to be full —
    the binding is the digest match itself (F-M1: the earlier capacity-based
    lane authorized forged digests against a full archive and was removed).
    Genuine mismatches — a manual pause without the operator digest, a
    horizon that has not elapsed, an active audit record, a digest absent
    from the marker chain — still fail closed with the same error tokens.
    """
    errors = []
    if not isinstance(deferred, dict) or not deferred:
        return ["worker_deferred_availability_missing"], None

    timestamp = _snapshot_now(now)
    digest = str(deferred.get("evidence_digest") or "")
    category = str(deferred.get("category") or "")
    manual = bool(deferred.get("requires_manual_resume"))
    if len(digest) != 64 or any(
        ch not in "0123456789abcdef" for ch in digest.lower()
    ):
        errors.append("worker_deferred_evidence_digest_invalid")
    if not category:
        errors.append("worker_deferred_category_missing")
    if not isinstance(pause_audit, dict) or not pause_audit:
        errors.append("global_pause_resume_receipt_missing")
        return errors, None

    current_errors = []
    if pause_audit.get("active") is not False:
        current_errors.append("global_pause_resume_receipt_not_inactive")
    if str(pause_audit.get("source") or "") != "llm_availability":
        current_errors.append("global_pause_resume_receipt_source_invalid")
    current_errors.extend(
        _resume_receipt_projection_errors(
            deferred,
            pause_audit,
            digest=digest,
            manual=manual,
            require_auto_resume_deadline=True,
        )
    )
    if not errors and not current_errors:
        return [], None

    if not errors:
        archived_policy_errors = []
        history = pause_audit.get("resume_receipt_history")
        if isinstance(history, list) and history:
            for projection in history[-_resume_receipt_scan_limit():]:
                if not isinstance(projection, dict):
                    continue
                identity_errors = _resume_receipt_identity_errors(
                    deferred, projection
                )
                policy_errors = _resume_receipt_policy_errors(
                    projection,
                    digest=digest,
                    manual=manual,
                    require_auto_resume_deadline=False,
                )
                if not identity_errors and not policy_errors:
                    # An archived receipt proves this deferred evidence was
                    # reconciled through an allowed resume path before a
                    # newer pause overwrote the record.
                    return [], None
                if not identity_errors and not archived_policy_errors:
                    # Identity matched but the resume path itself was not
                    # allowed (e.g. a manual pause whose archived receipt lacks
                    # the operator evidence digest): surface the actionable
                    # policy tokens instead of a generic archive miss.
                    archived_policy_errors = policy_errors
        # F-H: no receipt can exist for a suppressed freeze — the remaining
        # durable proof is the store-held suppression marker chain, which is
        # structurally bound to the frozen digest (F-M1).
        authorization = _suppressed_evidence_chain_authorization(
            deferred, pause_audit, now=timestamp
        )
        if authorization is not None:
            return [], authorization
        if isinstance(history, list) and history:
            # Review follow-up: the archive was scanned and nothing matched —
            # return the fully-assembled list directly so ``current_errors``
            # appears exactly once (the final ``+ current_errors`` below is
            # only for the no-archive path).
            return (
                errors
                + current_errors
                + archived_policy_errors
                + ["global_pause_resume_receipt_no_archived_match"]
            ), None
    return errors + current_errors, None


def _worker_availability_resume_receipt_errors(
    deferred,
    pause_audit,
    *,
    now: datetime | None = None,
):
    """Errors-only projection of ``_worker_availability_resume_validation``.

    Kept as the historical entry point (parent re-export in
    ``tool_planning_worker`` plus the audit replay tooling); the
    ``wait_for_llm_availability`` path uses the full validation so it can
    emit the F-M2 authorization event.
    """

    errors, _authorization = _worker_availability_resume_validation(
        deferred, pause_audit, now=now
    )
    return errors


@dataclass(frozen=True)
class _DeferredWorkerActivity:
    workflow: object
    envelope: dict
    next_dir: Path
    worker_template: str


async def _execute_workers_command(args, *, actor_lock_owned=False):
    # Moved verbatim to ``tool_planning_worker_phases`` (second cut of the
    # Group F cluster); the 76-exit dispatch body is unchanged and is
    # only reachable through this delegate so every historic caller --
    # ``tool_planning_worker._execute_workers_command`` (parent),
    # ``execute_workers`` below, and the test fixtures that import this
    # module by name -- keeps resolving to one shared implementation.
    from tool_planning_worker_phases import _execute_workers_command as _impl
    return await _impl(args, actor_lock_owned=actor_lock_owned)



async def execute_workers(args):
    """Serialize deterministic preparation, then run the leased LLM outside it.

    Only idle/completed histories can perform one-time preparation or open a
    new cycle.  They enter the generation actor before replaying again.  The
    resulting Worker activity is returned as an internal dispatch token so the
    expensive model call never holds the actor lock and a central abandon can
    fence it immediately.
    """
    next_v = args.get("next_v") or args.get("version")
    source_v = args.get("source_v")
    if next_v is None or source_v is None:
        next_v, source_v = _tw._resolve_version_args(args)
    checkpoint = (
        _tw._matching_checkpoint(next_v, source_v)
        if next_v is not None and source_v is not None
        else None
    )
    if not isinstance(checkpoint, dict):
        return await _execute_workers_command(args)

    try:
        from worker_workflow import WorkerWorkflow
        from workflow_kernel import WorkflowBusy

        workflow = WorkerWorkflow.for_checkpoint(checkpoint)
        try:
            with workflow.store.command_lock(workflow.run_id):
                result = await _execute_workers_command(
                    args,
                    actor_lock_owned=True,
                )
        except WorkflowBusy:
            return _tw._json_tool_result({
                "error": "WORKER_COMMAND_BUSY",
                "failure_class": "infrastructure",
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "Another process is publishing the deterministic Worker "
                    "preparation for this generation. Retry without editing the "
                    "candidate or rebuilding the prompt."
                ),
            })
        if isinstance(result, _DeferredWorkerActivity):
            return await _run_durable_worker_effect(
                result.workflow,
                result.envelope,
                result.next_dir,
                result.worker_template,
            )
        return result
    except WorkflowBusy:
        return _tw._json_tool_result({
            "error": "WORKER_COMMAND_BUSY",
            "failure_class": "infrastructure",
            "action": "retry_same_tool",
            "next_v": next_v,
            "source_v": source_v,
        })
