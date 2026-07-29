"""Worker command dispatcher -- phase-decomposed ``_execute_workers_command``.

The 76-return / 64-distinct-reason / 8-abandon dispatch body lives in four
contiguous module-level phase sub-functions orchestrated by the thin
``_execute_workers_command`` wrapper at the bottom of this module:

- ``_execute_workers_phase_a_preamble``        : arg validation, checkpoint
                                                  hydration, early dispatch (27).
- ``_execute_workers_phase_b_rework_synthesis``: rework task/authority synthesis
                                                  + circuit breakers (25).
- ``_execute_workers_phase_c_rework_preparation``: one-time repair preparation
                                                  (source reset, hygiene, freeze)
                                                  (10; owns the nested
                                                  ``rollback_rework_preparation``).
- ``_execute_workers_phase_d_projection``      : baseline drift recheck, idle
                                                  envelope prepare, final dispatch
                                                  (14).

Continuation protocol: every return in the original body is preserved VERBATIM
(no syntax changes). Each phase returns either the early-return value (any
non-tuple) or a 1-tuple ``(ctx_updates,)`` to fall through. No real exit ever
returns a bare tuple (AST-verified), so ``isinstance(result, tuple)`` is an
unambiguous continuation signal. The fixture in ``worker_exit_path_fixture.py``
walks the orchestrator + the four phases as one call graph and excludes the
per-phase continuation trailers.

``_tw`` is the parent ``tool_planning_worker`` (re-exports all helper symbols;
tests monkeypatch it). ``_dur`` is the durable companion owning the projection /
effect engine. The parent re-exports ``_execute_workers_command`` from here so
every historic caller keeps resolving.
"""

from __future__ import annotations

import tool_planning_worker as _tw
import tool_planning_worker_durable as _dur
import tool_planning_worker_phases_rework as _phr


async def _execute_workers_phase_a_preamble(args, actor_lock_owned):
    """Phase A: arg validation, checkpoint resolution, system-bootstrap guard,"""
    _t0 = _tw.time.time()
    tasks = args.get("tasks", [])
    if not isinstance(tasks, list):
        return _tw._json_tool_result({
            "error": "WORKER_TASKS_NOT_LIST",
            "directive": "Pass tasks=[] to load the checkpoint-owned Master plan.",
        })
    tasks_provided = bool(tasks)
    next_v = args.get("next_v")
    source_v = args.get("source_v")
    if next_v is None or source_v is None:
        next_v, source_v = _tw._resolve_version_args(args)
    if next_v is None or source_v is None:
        return _tw._json_tool_result({"error": "Missing next_v/source_v and no active checkpoint"})
    reviewer_feedback = args.get("reviewer_feedback", "")

    _tw._set_pipeline_status(f"Executing workers for v{next_v}")

    next_dir = _tw.get_bot_dir(next_v)
    prompts_dir = _tw.PROJECT_ROOT / "web" / "core" / "prompts"
    worker_template = _dur._load_worker_prompt_template(prompts_dir)

    ckpt = _tw._matching_checkpoint(next_v, source_v)
    if not ckpt:
        return _tw._state_blocked(
            "execute_workers requires a matching checkpoint from prepare_next_gen.",
            next_v,
            source_v,
        )
    checkpoint_tasks = _tw._checkpoint_master_plan(ckpt).get("tasks", [])
    if not isinstance(checkpoint_tasks, list):
        checkpoint_tasks = []
    critic_refusal = _tw._critic_advisory_rework_refusal(
        ckpt,
        [*checkpoint_tasks, *tasks],
        next_v,
        source_v,
    )
    if critic_refusal:
        return _tw._json_tool_result(critic_refusal)
    _system_bootstrap_executor = False
    from system_strict_bootstrap import is_declared_native_bootstrap

    _declared_system_bootstrap = is_declared_native_bootstrap(ckpt)
    _system_initial_worker_stage = bool(
        ckpt.get("stage") == "master_planned" and not reviewer_feedback
    )
    if _declared_system_bootstrap and not _system_initial_worker_stage:
        return _tw._json_tool_result({
            "error": "SYSTEM_STRICT_BOOTSTRAP_REWORK_FORBIDDEN",
            "success": False,
            "action": "abandon_generation",
            "failure_class": "control_plane",
            "next_v": next_v,
            "source_v": source_v,
            "stage": ckpt.get("stage"),
            "directive": (
                "A content-bound first-migration blueprint may run only once from "
                "master_planned. If quality, Review, Critic, or precommit rejects "
                "it, abandon and change the checked-in blueprint/control contract "
                "in a fresh generation; never fall back to an LLM repair Worker."
            ),
        })

    if _declared_system_bootstrap:
        from system_strict_bootstrap import validate_master_receipt

        _system_worker_errors = validate_master_receipt(
            ckpt,
            candidate_dir=next_dir,
            require_prepared_content=True,
        )
        if _system_worker_errors:
            return _tw._json_tool_result({
                "error": "SYSTEM_STRICT_BOOTSTRAP_WORKER_AUTHORITY_INVALID",
                "success": False,
                "action": "abandon_generation",
                "failure_class": "control_plane",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": _system_worker_errors,
                "directive": (
                    "The fresh-bootstrap system receipt or prepared artifact drifted. "
                    "Abandon this generation; never fall back to an LLM Worker."
                ),
            })
        _system_bootstrap_executor = True
    if (
        not str(ckpt.get("workflow_run_id") or "").strip()
        or int(ckpt.get("checkpoint_revision") or 0) < 1
    ):
        return _tw._json_tool_result({
            "error": "STALE_WORKFLOW_ID_UNSUPPORTED",
            "failure_class": "state_migration",
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
            "directive": (
                "This active checkpoint predates the immutable generation actor "
                "identity. Abandon it while the runtime is stopped and prepare a "
                "new generation; do not migrate a half-executed workflow."
            ),
        })
    _worker_infra, _worker_infra_error = _tw._owned_infrastructure_failure(
        ckpt,
        "execute_workers",
    )
    if _worker_infra_error:
        infra_route = _tw.route_policy(ckpt)
        return _tw._state_blocked(
            _worker_infra_error + f"; next tool is {infra_route.get('next_tool')}",
            next_v,
            source_v,
            checkpoint=ckpt,
        )
    from worker_workflow import (
        WorkerWorkflow,
        next_worker_command,
        validate_worker_envelope,
    )

    worker_workflow = WorkerWorkflow.for_checkpoint(ckpt)
    if _worker_infra is not None:
        return _tw._json_tool_result({
            "error": "STALE_WORKER_INFRASTRUCTURE_STATE_UNSUPPORTED",
            "failure_class": "state_migration",
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
            "directive": (
                "This generation was created by the retired Worker overlay state "
                "machine. Abandon it from the stopped runtime and start from a new "
                "baseline; do not translate two authorities into one history."
            ),
        })
    durable_worker_state = worker_workflow.state()
    durable_worker_status = str(durable_worker_state.get("status") or "idle")
    if durable_worker_status == "completed":
        previous_envelope = durable_worker_state.get("envelope") or {}
        previous_contract = previous_envelope.get("checkpoint_contract") or {}
        current_revision = int(ckpt.get("checkpoint_revision") or 0)
        previous_revision = int(previous_contract.get("checkpoint_revision") or 0)
        worker_entry_stages = {
            "master_planned",
            "quality_failed",
            "quality_passed",
            "reviewed",
            "critic_checked",
            "precommit_failed",
            "official_failed",
            "repair_planned",
            "rework_running",
        }
        if (
            ckpt.get("stage") in worker_entry_stages
            and current_revision > previous_revision
            and _tw.route_policy(ckpt).get("next_tool") == "execute_workers"
        ):
            work_receipt = _tw.hashlib.sha256(
                _tw.json.dumps(
                    {
                        "workflow_run_id": ckpt.get("workflow_run_id"),
                        "checkpoint_revision": current_revision,
                        "stage": ckpt.get("stage"),
                        "master_plan": ckpt.get("master_plan") or {},
                        "reviewer_feedback": ckpt.get("reviewer_feedback") or "",
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            durable_worker_state = worker_workflow.open_cycle(
                f"checkpoint_work_receipt:{work_receipt}"
            )
            durable_worker_status = "idle"
    durable_worker_envelope = (
        durable_worker_state.get("envelope")
        if isinstance(durable_worker_state.get("envelope"), dict)
        else {}
    )
    worker_command = next_worker_command(durable_worker_state)
    command_name = str(worker_command.get("command") or "recover")
    if command_name == "reconcile_abandon":
        return _tw._json_tool_result({
            "error": "WORKER_WORKFLOW_ABANDONED",
            "success": False,
            "failure_class": "infrastructure",
            "action": "abandon_generation",
            "worker_abandon_reason": str(
                worker_command.get("reason") or "worker_abandoned"
            ),
            "next_v": next_v,
            "source_v": source_v,
            "stage": ckpt.get("stage"),
            "directive": (
                "The durable Worker journal is terminal while the outer "
                "checkpoint is still active. Reconcile by centrally abandoning "
                "this generation; never reopen or recreate the exhausted effect."
            ),
        })
    durable_worker_resume = command_name != "prepare"
    if durable_worker_resume and durable_worker_envelope:
        envelope_errors = validate_worker_envelope(durable_worker_envelope)
        if envelope_errors:
            worker_workflow.abandon("durable_worker_envelope_invalid")
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_ENVELOPE_INVALID",
                "validation_errors": envelope_errors,
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
            })
        if (
            int(durable_worker_envelope.get("next_v")) != int(next_v)
            or int(durable_worker_envelope.get("source_v")) != int(source_v)
        ):
            worker_workflow.abandon("durable_worker_identity_mismatch")
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_IDENTITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
            })
        current_template_hash = _tw.hashlib.sha256(
            worker_template.encode("utf-8")
        ).hexdigest()
        if (
            durable_worker_envelope.get("worker_template_hash")
            != current_template_hash
            or durable_worker_envelope.get("backend_contract")
            != _dur._expected_worker_backend_contract(
                ckpt,
                durable_worker_envelope,
            )
        ):
            worker_workflow.abandon("durable_worker_definition_drift")
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_DEFINITION_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
            })
    _worker_uses_llm = bool(
        (durable_worker_envelope.get("execution_policy") or {}).get(
            "executor"
        )
        != "system_policy_bootstrap_v1"
    )
    if (
        _worker_uses_llm
        and command_name in {
            "request_or_claim_worker",
            "claim_worker",
            "wait_for_llm_availability",
        }
    ):
        try:
            from llm_availability_store import active_llm_pause, load_llm_pause

            _active_pause = active_llm_pause()
            _pause_audit = load_llm_pause()
        except Exception as exc:
            return _tw._json_tool_result({
                "error": "LLM_AVAILABILITY_STATE_INVALID",
                "success": False,
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "worker_status": durable_worker_status,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": (
                    "The durable provider pause record could not be validated. "
                    "Do not claim or fail the Worker effect until that control "
                    "record is reconciled."
                ),
            })
        if _active_pause is not None:
            return _tw._json_tool_result({
                "error": "LLM_AVAILABILITY_BLOCKED",
                "success": False,
                "failure_class": "availability",
                "action": "wait_for_llm_availability",
                "next_v": next_v,
                "source_v": source_v,
                "worker_status": durable_worker_status,
                "attempt": int(durable_worker_state.get("attempt") or 0),
                "max_attempts": int(
                    durable_worker_state.get("max_attempts") or 0
                ),
                "effect_id": durable_worker_state.get("effect_id"),
                "availability": _active_pause,
                "directive": (
                    "The provider pause is still active. No Worker effect was "
                    "claimed and no attempt was consumed."
                ),
            })
        if command_name == "wait_for_llm_availability":
            _deferred_availability = (
                durable_worker_state.get("availability") or {}
            )
            if _deferred_availability.get("persistence_error"):
                return _tw._json_tool_result({
                    "error": "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "attempt": int(
                        durable_worker_state.get("attempt") or 0
                    ),
                    "effect_id": durable_worker_state.get("effect_id"),
                    "availability": _deferred_availability,
                    "directive": (
                        "The Worker lease was safely deferred, but the global "
                        "pause write failed. Preserve the attempt-neutral effect "
                        "and reconcile the pause record before resuming."
                    ),
                })
            _resume_receipt_errors = _dur._worker_availability_resume_receipt_errors(
                _deferred_availability,
                _pause_audit,
            )
            if _resume_receipt_errors:
                return _tw._json_tool_result({
                    "error": "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "attempt": int(
                        durable_worker_state.get("attempt") or 0
                    ),
                    "effect_id": durable_worker_state.get("effect_id"),
                    "receipt_errors": _resume_receipt_errors,
                    "availability": _deferred_availability,
                    "directive": (
                        "The global pause is not active, but no matching durable "
                        "resume receipt authorizes this deferred Worker effect. "
                        "Preserve the attempt-neutral journal and reconcile the "
                        "exact evidence digest before resuming."
                    ),
                })
            try:
                if actor_lock_owned:
                    durable_worker_state = (
                        worker_workflow.resume_availability_deferred()
                    )
                else:
                    with worker_workflow.store.command_lock(
                        worker_workflow.run_id
                    ):
                        durable_worker_state = (
                            worker_workflow.resume_availability_deferred()
                        )
            except Exception as exc:
                return _tw._json_tool_result({
                    "error": "WORKER_AVAILABILITY_RESUME_FAILED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "directive": (
                        "The provider pause cleared, but its fenced Worker effect "
                        "could not transition back to requested. Do not recreate "
                        "or fail the effect."
                    ),
                })
            durable_worker_status = str(
                durable_worker_state.get("status") or "requested"
            )
            worker_command = next_worker_command(durable_worker_state)
            command_name = str(
                worker_command.get("command") or "recover"
            )
            if command_name != "claim_worker":
                return _tw._json_tool_result({
                    "error": "WORKER_AVAILABILITY_RESUME_INVARIANT_FAILED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "next_command": command_name,
                })
    if command_name == "project_output":
        if actor_lock_owned:
            return await _dur._project_durable_worker_output(
                worker_workflow,
                next_dir,
                durable_worker_state,
            )
        with worker_workflow.store.command_lock(worker_workflow.run_id):
            return await _dur._project_durable_worker_output(
                worker_workflow,
                next_dir,
                durable_worker_state,
            )
    if command_name == "project_failure":
        if actor_lock_owned:
            return await _dur._project_durable_worker_failure(
                worker_workflow,
                durable_worker_state,
            )
        with worker_workflow.store.command_lock(worker_workflow.run_id):
            return await _dur._project_durable_worker_failure(
                worker_workflow,
                durable_worker_state,
            )
    if command_name in {"request_or_claim_worker", "claim_worker"}:
        if actor_lock_owned:
            return _dur._DeferredWorkerActivity(
                workflow=worker_workflow,
                envelope=durable_worker_envelope,
                next_dir=next_dir,
                worker_template=worker_template,
            )
        return await _dur._run_durable_worker_effect(
            worker_workflow,
            durable_worker_envelope,
            next_dir,
            worker_template,
        )
    if command_name == "abandon":
        worker_workflow.abandon("worker_infrastructure_exhausted")
        return _tw._json_tool_result({
            "error": "WORKER_INFRASTRUCTURE_EXHAUSTED",
            "failure_class": "infrastructure",
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
        })
    if command_name == "none":
        return _tw._json_tool_result({
            "error": "WORKER_CYCLE_HAS_NO_PENDING_COMMAND",
            "next_v": next_v,
            "source_v": source_v,
            "stage": ckpt.get("stage"),
            "projected_stage": durable_worker_state.get("projected_stage"),
            "next_tool": _tw.route_policy(ckpt).get("next_tool"),
        })

    return ({"_system_bootstrap_executor": _system_bootstrap_executor, "checkpoint_tasks": checkpoint_tasks, "ckpt": ckpt, "durable_worker_envelope": durable_worker_envelope, "durable_worker_resume": durable_worker_resume, "durable_worker_state": durable_worker_state, "durable_worker_status": durable_worker_status, "next_dir": next_dir, "next_v": next_v, "reviewer_feedback": reviewer_feedback, "source_v": source_v, "tasks": tasks, "tasks_provided": tasks_provided, "worker_template": worker_template, "worker_workflow": worker_workflow},)  # PHASE CONTINUATION (not an exit path)


async def _execute_workers_phase_d_projection(_system_bootstrap_executor, actor_lock_owned, ckpt, durable_worker_envelope, durable_worker_resume, durable_worker_state, durable_worker_status, force_sequential_rework, frozen_rework_resume, next_dir, next_v, official_rework_count_for_write, precommit_rework_count_for_write, prepared_candidate_dir, quality_skipper_config, replace_checkpoint_tasks, reviewer_feedback, rework_plan_metadata, rework_preparation_dir, rework_stages, source_v, tasks, worker_template, worker_workflow):
    """Phase D: repair-baseline drift recheck, frozen-rework pre-worker drift guard,"""
    if reviewer_feedback and rework_plan_metadata:
        expected_rework_hash = str(
            rework_plan_metadata.get("repair_baseline_artifact_hash") or ""
        )
        current_rework_hash = _tw._complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if (
            not expected_rework_hash
            or not current_rework_hash
            or current_rework_hash != expected_rework_hash
        ):
            return _tw._json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_rework_hash,
                "current_artifact_hash": current_rework_hash,
                "next_tool": "abandon_generation",
                "directive": (
                    "The candidate changed after the repair baseline receipt was "
                    "written and before Workers. Abandon this generation."
                ),
            })
        running_plan = (
            _tw._checkpoint_plan_with_tasks(
                ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
            )
            if ckpt else {"tasks": tasks}
        )
        running_plan = {**running_plan, "work_item": rework_plan_metadata}
        running_plan = _tw._plan_with_accumulated_repair_scope(ckpt, running_plan, tasks, next_v)
        rework_projection_ckpt = _tw._matching_checkpoint(next_v, source_v)
        if not rework_projection_ckpt:
            return _tw._json_tool_result({
                "error": "REWORK_PROJECTION_CHECKPOINT_MISSING",
                "next_v": next_v,
                "source_v": source_v,
            })
        rework_checkpoint_written = _tw.write_pipeline_checkpoint(
            next_v,
            source_v,
            "rework_running",
            master_plan=running_plan,
            reviewer_feedback=reviewer_feedback,
            worker_failure_count=ckpt.get("worker_failure_count", 0) if ckpt else 0,
            precommit_rework_count=precommit_rework_count_for_write,
            official_rework_count=official_rework_count_for_write,
            repair_baseline_artifact_hash=expected_rework_hash,
            expected_checkpoint_revision=int(
                rework_projection_ckpt.get("checkpoint_revision") or 0
            ),
            expected_checkpoint_stage=str(
                rework_projection_ckpt.get("stage") or ""
            ),
            expected_workflow_run_id=str(
                rework_projection_ckpt.get("workflow_run_id") or ""
            ),
        )
        if not rework_checkpoint_written:
            return _tw._json_tool_result({
                "error": "REWORK_RUNNING_CHECKPOINT_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_rework_hash,
                "directive": (
                    "The repair baseline was frozen but the rework-running "
                    "transition could not be persisted. Do not execute Workers."
                ),
            })

        # Recheck immediately before the Worker batch.  This closes the gap in
        # which a self-modifying test or external process edits an otherwise
        # declared repair file after checkpoint publication.
        current_rework_hash = _tw._complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if current_rework_hash != expected_rework_hash:
            return _tw._json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_rework_hash,
                "current_artifact_hash": current_rework_hash,
                "next_tool": "abandon_generation",
            })

    if frozen_rework_resume and ckpt.get("stage") in rework_stages:
        expected_retry_hash = _tw._checkpoint_repair_baseline_fingerprint(ckpt)
        current_retry_hash = _tw._complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if (
            not expected_retry_hash
            or not current_retry_hash
            or current_retry_hash != expected_retry_hash
        ):
            abandon_result = await _tw._force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "frozen_rework_pre_worker_drift",
                actor_lock_owned=actor_lock_owned,
            )
            return _tw._json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_retry_hash,
                "current_artifact_hash": current_retry_hash,
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The infrastructure retry candidate no longer matches its "
                    "frozen repair baseline. Abandon without consuming the lease."
                ),
            })

    task_digest = _dur._worker_execution_task_digest(
        tasks,
        reviewer_feedback,
        worker_template,
    )
    if durable_worker_resume:
        durable_input_digest = _dur._worker_execution_task_digest(
            durable_worker_envelope.get("tasks") or [],
            str(durable_worker_envelope.get("reviewer_feedback") or ""),
            worker_template,
        )
        if task_digest != durable_input_digest:
            abandon_result = await _tw._force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "durable_worker_frozen_input_drift",
                actor_lock_owned=actor_lock_owned,
            )
            worker_workflow.abandon("durable_worker_frozen_input_drift")
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_FROZEN_INPUT_DRIFT",
                "success": False,
                "next_v": next_v,
                "source_v": source_v,
                **abandon_result,
            })

    if durable_worker_status == "idle":
        from worker_workflow import build_worker_envelope

        projection_ckpt = _tw._matching_checkpoint(next_v, source_v)
        if not projection_ckpt:
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_CHECKPOINT_MISSING_BEFORE_PREPARE",
                "next_v": next_v,
                "source_v": source_v,
            })
        prepared_artifact_hash = _tw._complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        prepared_snapshot_hash = worker_workflow.artifacts.capture(
            prepared_candidate_dir
        )
        if prepared_artifact_hash != prepared_snapshot_hash:
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_PREPARED_SNAPSHOT_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "prepared_artifact_hash": prepared_artifact_hash,
                "prepared_snapshot_hash": prepared_snapshot_hash,
                "next_tool": "abandon_generation",
            })
        active_work_item = rework_plan_metadata or (
            (_tw._checkpoint_master_plan(ckpt).get("work_item") or {})
            if isinstance(_tw._checkpoint_master_plan(ckpt).get("work_item"), dict)
            else {}
        )
        worker_kind = str(active_work_item.get("kind") or "initial_worker")
        projection_plan = _tw._checkpoint_plan_with_tasks(
            projection_ckpt,
            tasks,
            replace_existing_tasks=replace_checkpoint_tasks,
        )
        if active_work_item:
            projection_plan = {
                **projection_plan,
                "work_item": active_work_item,
            }
        if reviewer_feedback:
            projection_plan = _tw._plan_with_accumulated_repair_scope(
                projection_ckpt,
                projection_plan,
                tasks,
                next_v,
            )
        projection_preimage_artifact_hash = str(
            active_work_item.get("projection_preimage_artifact_hash")
            or prepared_artifact_hash
        )
        projection_preimage_snapshot_hash = (
            str(
                active_work_item.get("projection_preimage_snapshot_hash")
                or ""
            )
            or prepared_snapshot_hash
        )
        try:
            worker_workflow.artifacts.path_for(
                projection_preimage_snapshot_hash
            )
        except Exception as exc:
            return _tw._json_tool_result({
                "error": "DURABLE_WORKER_PROJECTION_PREIMAGE_UNAVAILABLE",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "projection_preimage_artifact_hash": (
                    projection_preimage_artifact_hash
                ),
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
        checkpoint_contract = {
            "workflow_run_id": str(
                projection_ckpt.get("workflow_run_id")
                or projection_ckpt.get("run_id")
                or worker_workflow.run_id
                or ""
            ),
            "checkpoint_revision": int(
                projection_ckpt.get("checkpoint_revision") or 0
            ),
            "checkpoint_stage": str(projection_ckpt.get("stage") or ""),
        }
        execution_policy = {
            "force_sequential": bool(force_sequential_rework),
            "quality_skipper": quality_skipper_config is not None,
            "expected_architecture_policy": (
                _tw.deepcopy(
                    quality_skipper_config.get(
                        "expected_architecture_policy"
                    )
                )
                if isinstance(quality_skipper_config, dict)
                else None
            ),
            **(
                {"executor": "system_policy_bootstrap_v1"}
                if _system_bootstrap_executor
                else {}
            ),
        }
        envelope = build_worker_envelope(
            checkpoint=projection_ckpt,
            kind=worker_kind,
            source_stage=str(projection_ckpt.get("stage") or ""),
            prepared_artifact_hash=prepared_artifact_hash,
            prepared_snapshot_hash=prepared_snapshot_hash,
            source_artifact_hash=(
                prepared_artifact_hash
                if _system_bootstrap_executor
                else _tw._complete_artifact_fingerprint(
                    _tw.get_bot_dir(source_v)
                )
            ),
            tasks=tasks,
            reviewer_feedback=reviewer_feedback,
            worker_template_hash=_tw.hashlib.sha256(
                worker_template.encode("utf-8")
            ).hexdigest(),
            work_item=active_work_item,
            backend_contract=_dur._expected_worker_backend_contract(
                projection_ckpt,
                {"execution_policy": execution_policy},
            ),
            precommit_rework_count=(
                int(precommit_rework_count_for_write)
                if precommit_rework_count_for_write is not None
                else int(projection_ckpt.get("precommit_rework_count") or 0)
            ),
            official_rework_count=(
                int(official_rework_count_for_write)
                if official_rework_count_for_write is not None
                else int(projection_ckpt.get("official_rework_count") or 0)
            ),
            projection_plan=projection_plan,
            audit_context=_tw.deepcopy(projection_ckpt.get("audit_context") or {}),
            execution_policy=execution_policy,
            checkpoint_contract=checkpoint_contract,
            worker_failure_count=int(
                projection_ckpt.get("worker_failure_count") or 0
            ),
            projection_preimage_artifact_hash=(
                projection_preimage_artifact_hash
            ),
            projection_preimage_snapshot_hash=(
                projection_preimage_snapshot_hash
            ),
        )
        durable_worker_state = worker_workflow.prepare(
            envelope,
            max_attempts=1 if _system_bootstrap_executor else 3,
        )
        durable_worker_envelope = durable_worker_state["envelope"]
        durable_worker_status = durable_worker_state["status"]
        if rework_preparation_dir is not None:
            worker_workflow.artifacts.discard_workspace(
                rework_preparation_dir
            )
        if not _system_bootstrap_executor:
            try:
                from llm_availability_store import active_llm_pause

                _active_pause = active_llm_pause()
            except Exception as exc:
                return _tw._json_tool_result({
                    "error": "LLM_AVAILABILITY_STATE_INVALID",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "directive": (
                        "Worker preparation is durable, but the provider pause "
                        "record is invalid. No effect was claimed."
                    ),
                })
            if _active_pause is not None:
                return _tw._json_tool_result({
                    "error": "LLM_AVAILABILITY_BLOCKED",
                    "success": False,
                    "failure_class": "availability",
                    "action": "wait_for_llm_availability",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "attempt": int(
                        durable_worker_state.get("attempt") or 0
                    ),
                    "max_attempts": int(
                        durable_worker_state.get("max_attempts") or 0
                    ),
                    "effect_id": durable_worker_state.get("effect_id"),
                    "availability": _active_pause,
                    "directive": (
                        "Worker input was frozen, but the provider pause is "
                        "active. No effect was claimed and no attempt was consumed."
                    ),
                })
        if actor_lock_owned:
            return _dur._DeferredWorkerActivity(
                workflow=worker_workflow,
                envelope=durable_worker_envelope,
                next_dir=next_dir,
                worker_template=worker_template,
            )
        return await _dur._run_durable_worker_effect(
            worker_workflow,
            durable_worker_envelope,
            next_dir,
            worker_template,
        )

    return _tw._json_tool_result({
        "error": "DURABLE_WORKER_COMMAND_DISPATCH_INVARIANT",
        "workflow_status": durable_worker_status,
        "next_v": next_v,
        "source_v": source_v,
    })



async def _execute_workers_command(args, *, actor_lock_owned=False):
    """Thin orchestrator over the four phase sub-functions (see module docstring)."""
    # Phase A: preamble + early command-name dispatch (yields the shared ctx).
    result = await _execute_workers_phase_a_preamble(args, actor_lock_owned=actor_lock_owned)
    if isinstance(result, tuple):
        ctx = dict(result[0])
    else:
        return result

    # Phase B: rework synthesis + circuit breakers (lives in the rework companion).
    result = await _phr._execute_workers_phase_b_rework_synthesis(
        actor_lock_owned=actor_lock_owned,
        **{k: ctx[k] for k in ('checkpoint_tasks', 'ckpt', 'durable_worker_envelope', 'durable_worker_resume', 'durable_worker_status', 'next_dir', 'next_v', 'reviewer_feedback', 'source_v', 'tasks', 'tasks_provided', 'worker_workflow') if k in ctx}
    )
    if isinstance(result, tuple):
        ctx.update(result[0])
    else:
        return result

    # Phase C: one-time repair preparation (lives in the rework companion).
    result = await _phr._execute_workers_phase_c_rework_preparation(
        actor_lock_owned=actor_lock_owned,
        **{k: ctx[k] for k in ('ckpt', 'durable_worker_state', 'durable_worker_status', 'frozen_rework_resume', 'next_dir', 'next_v', 'replace_checkpoint_tasks', 'review_rework_checkpoint', 'reviewer_feedback', 'source_v', 'tasks', 'worker_template', 'worker_workflow') if k in ctx}
    )
    if isinstance(result, tuple):
        ctx.update(result[0])
    else:
        return result

    # Phase D: projection + final dispatch (terminal phase).
    result = await _execute_workers_phase_d_projection(
        actor_lock_owned=actor_lock_owned,
        **{k: ctx[k] for k in ('_system_bootstrap_executor', 'ckpt', 'durable_worker_envelope', 'durable_worker_resume', 'durable_worker_state', 'durable_worker_status', 'force_sequential_rework', 'frozen_rework_resume', 'next_dir', 'next_v', 'official_rework_count_for_write', 'precommit_rework_count_for_write', 'prepared_candidate_dir', 'quality_skipper_config', 'replace_checkpoint_tasks', 'reviewer_feedback', 'rework_plan_metadata', 'rework_preparation_dir', 'rework_stages', 'source_v', 'tasks', 'worker_template', 'worker_workflow') if k in ctx}
    )
    return result
