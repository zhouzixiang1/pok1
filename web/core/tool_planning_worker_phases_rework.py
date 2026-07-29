"""Rework-phase companion to ``tool_planning_worker_phases``.

Wave-8 slimming: the two largest phase sub-functions -- the rework-synthesis
phase (B) and the rework-preparation phase (C) -- were moved VERBATIM out of
``tool_planning_worker_phases.py`` into this sibling module so the phases
module could shrink under 2000 lines. Every ``return`` statement is preserved
byte-for-byte; the fixture + contract test walk both modules as one call
graph (see ``web/tests/worker_exit_path_fixture.py`` and
``web/tests/test_worker_exit_path_contract.py``).

``_tw`` is the parent ``tool_planning_worker`` (re-exports all helper symbols;
tests monkeypatch it). ``_dur`` is the durable companion owning the projection
/ effect engine. The orchestrator in ``tool_planning_worker_phases`` calls
back into ``_phr._execute_workers_phase_b_rework_synthesis`` and
``_phr._execute_workers_phase_c_rework_preparation`` here.
"""

from __future__ import annotations

import tool_planning_worker as _tw
import tool_planning_worker_durable as _dur


async def _execute_workers_phase_b_rework_synthesis(actor_lock_owned, checkpoint_tasks, ckpt, durable_worker_envelope, durable_worker_resume, durable_worker_status, next_dir, next_v, reviewer_feedback, source_v, tasks, tasks_provided, worker_workflow):
    """Phase B: architecture-policy identity recovery, prepared-artifact drift"""
    if _tw._checkpoint_architecture_policy_identity_errors(ckpt):
        if _tw._is_fresh_empty_pool_bootstrap(ckpt):
            return _tw._json_tool_result({
                "error": "FIRST_STRICT_ARCHITECTURE_POLICY_IDENTITY_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
                "directive": (
                    "The fresh first-strict architecture identity drifted. "
                    "Abandon and rematerialize the system blueprint; never "
                    "recover it from numeric high-water source bytes."
                ),
            })
        identity_errors = _tw._checkpoint_architecture_policy_identity_errors(ckpt)
        identity_fingerprint = _tw._identity_replan_fingerprint(identity_errors)
        identity_history = _tw._identity_replan_counts(ckpt)
        identity_consecutive = _tw._identity_replan_consecutive_count(
            identity_history, identity_fingerprint
        )
        if identity_consecutive >= _tw.IDENTITY_REPLAN_ABANDON_THRESHOLD:
            _tw.log_system_event(
                "pipeline.architecture_policy_identity_replan_abandoned",
                "error",
                (
                    f"Abandoning v{next_v}: identical architecture policy "
                    f"identity error recurred {identity_consecutive} times "
                    f"without progress; recovery is unable to resolve it."
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": ckpt.get("stage"),
                    "identity_errors": identity_errors,
                    "consecutive_count": identity_consecutive,
                    "threshold": _tw.IDENTITY_REPLAN_ABANDON_THRESHOLD,
                },
            )
            return _tw._json_tool_result({
                "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_EXHAUSTED",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
                "failure_class": "deterministic",
                "consecutive_count": identity_consecutive,
                "threshold": _tw.IDENTITY_REPLAN_ABANDON_THRESHOLD,
                "identity_errors": identity_errors,
                "directive": (
                    "The same architecture policy identity error survived "
                    "multiple replan attempts. Recovery cannot fix a frozen "
                    "vs. recomputed mismatch deterministically; abandon this "
                    "generation and let the planner rebuild on the current "
                    "policy code, or escalate the identity comparator."
                ),
            })
        updated_history = _tw._record_identity_replan_attempt(ckpt, identity_fingerprint)
        # Persist the circuit-breaker counter before recovery runs, so a crash
        # or stage rewrite inside recovery cannot lose the attempt record.
        # Stage is preserved; only the identity_replan_history field advances.
        try:
            _tw.write_pipeline_checkpoint(
                next_v,
                source_v,
                ckpt.get("stage"),
                identity_replan_history=updated_history,
            )
        except Exception:
            # Counter persistence is best-effort; the in-memory ckpt copy still
            # carries the update through this call's recovery path.
            pass
        try:
            recovery = _tw._recover_architecture_policy_identity(
                ckpt,
                next_dir,
                _tw.get_bot_dir(source_v),
            )
        except Exception as exc:
            _tw.log_system_event(
                "pipeline.architecture_policy_identity_replan_failed",
                "error",
                f"Could not reset stale-policy candidate v{next_v}: {type(exc).__name__}: {exc}",
                {"next_v": next_v, "source_v": source_v, "stage": ckpt.get("stage")},
            )
            return _tw._json_tool_result({
                "error": "ARCHITECTURE_POLICY_IDENTITY_RECOVERY_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": "Do not run bot workers; repair checkpoint/source synchronization first.",
            })
        if recovery is not None:
            return recovery
    if ckpt.get("stage") == "master_planned":
        from prepared_baseline_contract import validate_prepared_artifact_contract

        prepared_artifact_contract = (
            (ckpt.get("audit_context") or {}).get("prepared_artifact_contract")
        )
        prepared_artifact_errors = validate_prepared_artifact_contract(
            prepared_artifact_contract,
            prepared_dir=next_dir,
            source_v=source_v,
            next_v=next_v,
            verify_live_content=True,
        )
        if prepared_artifact_errors:
            return _tw._json_tool_result({
                "error": "PREPARED_ARTIFACT_DRIFT_BEFORE_WORKERS",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": prepared_artifact_errors,
                "next_tool": "abandon_generation",
                "directive": (
                    "The candidate changed after Master accepted the frozen prepared "
                    "baseline but before Workers. Abandon and restart; do not grant "
                    "the drift a repair scope."
                ),
            })
    rework_stages = {"quality_failed", "precommit_failed", "official_failed", "repair_planned", "rework_running"}
    checkpoint_work_item = (
        durable_worker_envelope.get("work_item")
        if durable_worker_resume
        and isinstance(durable_worker_envelope.get("work_item"), dict)
        else _tw._checkpoint_master_plan(ckpt).get("work_item")
        if isinstance(_tw._checkpoint_master_plan(ckpt).get("work_item"), dict)
        else {}
    )
    checkpoint_has_frozen_preparation = bool(
        isinstance(checkpoint_work_item, dict)
        and checkpoint_work_item.get("repair_baseline_artifact_hash")
        and checkpoint_work_item.get("prepared_snapshot_hash")
    )
    frozen_rework_resume = bool(
        durable_worker_resume
        and durable_worker_envelope.get("kind") != "initial_worker"
        or (
            ckpt.get("stage") in {"repair_planned", "rework_running"}
            and checkpoint_work_item.get("repair_baseline_artifact_hash")
        )
    )
    prepared_repair_resume_dir = None
    prepared_repair_resume_hash = ""
    if (
        durable_worker_status == "idle"
        and ckpt.get("stage") in {"repair_planned", "rework_running"}
        and isinstance(checkpoint_work_item, dict)
    ):
        prepared_repair_resume_hash = str(
            checkpoint_work_item.get("prepared_snapshot_hash") or ""
        )
        if (
            checkpoint_work_item.get("repair_baseline_artifact_hash")
            and not prepared_repair_resume_hash
        ):
            return _tw._json_tool_result({
                "error": "DURABLE_REPAIR_PREPARATION_RECEIPT_MISSING",
                "failure_class": "state_migration",
                "action": "abandon_generation",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "A repair work item claims a prepared baseline but does not "
                    "bind its immutable snapshot. Do not reconstruct or rerun "
                    "one-time preparation from mutable candidate bytes."
                ),
            })
        if prepared_repair_resume_hash:
            try:
                prepared_repair_resume_dir = worker_workflow.artifacts.path_for(
                    prepared_repair_resume_hash
                )
            except Exception:
                prepared_repair_resume_dir = None
    if ckpt.get("stage") in rework_stages:
        expected_repair_baseline = _tw._checkpoint_repair_baseline_fingerprint(ckpt)
        # Once repair preparation has been captured and projected into the
        # checkpoint, that immutable artifact is the recovery authority.  The
        # canonical candidate intentionally still contains the pre-preparation
        # bytes, so comparing it here would turn a crash between checkpoint
        # publication and WorkerPrepared into a false drift/abandon.
        current_repair_baseline = _tw._complete_artifact_fingerprint(
            prepared_repair_resume_dir
            if prepared_repair_resume_dir is not None
            else next_dir
        )
        if not expected_repair_baseline:
            return _tw._json_tool_result({
                "error": "REPAIR_BASELINE_RECEIPT_MISSING",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "next_tool": "abandon_generation",
                "directive": (
                    "The failed gate/repair plan does not bind the exact complete "
                    "candidate artifact. Abandon; do not infer repair authority "
                    "from file paths or the live diff."
                ),
            })
        if (
            not current_repair_baseline
            or current_repair_baseline != expected_repair_baseline
        ):
            abandon_result = {}
            if frozen_rework_resume:
                abandon_result = await _tw._force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "frozen_rework_baseline_drift",
                    actor_lock_owned=actor_lock_owned,
                )
            else:
                # See REWORK_TASK_AUTHORITY_INVALID: a non-frozen rework stage with a
                # drifted repair baseline cannot be repaired and must be abandoned in
                # tool, or the deterministic router loops on execute_workers by stage.
                abandon_result = await _tw._force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "worker_terminal_abandon_repair_baseline_drift",
                    actor_lock_owned=actor_lock_owned,
                )
            return _tw._json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "expected_artifact_hash": expected_repair_baseline,
                "current_artifact_hash": current_repair_baseline,
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The candidate changed after the gate evidence or repair plan "
                    "was frozen. Abandon; the drift cannot piggyback on a declared "
                    "repair file."
                ),
            })
        canonical_feedback = (
            str(durable_worker_envelope.get("reviewer_feedback") or "")
            if durable_worker_resume
            else _tw._checkpoint_rework_feedback(ckpt)
        )
        if not canonical_feedback:
            abandon_result = await _tw._force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "worker_terminal_abandon_rework_feedback_missing",
                actor_lock_owned=actor_lock_owned,
            )
            return _tw._json_tool_result({
                "error": "REWORK_FEEDBACK_AUTHORITY_MISSING",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The checkpoint/gate receipt contains no canonical repair "
                    "feedback. Caller feedback cannot create repair authority."
                ),
            })
        if reviewer_feedback and not _tw._transport_equivalent_feedback(
            reviewer_feedback,
            canonical_feedback,
        ):
            _tw.log_system_event(
                "pipeline.worker_rework_feedback_mismatch",
                "error",
                f"Rejected caller-rewritten rework feedback for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": ckpt.get("stage"),
                    "canonical_feedback_digest": _tw.hashlib.sha256(
                        canonical_feedback.encode("utf-8")
                    ).hexdigest(),
                    "supplied_feedback_digest": _tw.hashlib.sha256(
                        str(reviewer_feedback).encode("utf-8")
                    ).hexdigest(),
                },
            )
            abandon_result = await _tw._force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "worker_terminal_abandon_rework_feedback_mismatch",
                actor_lock_owned=actor_lock_owned,
            )
            return _tw._json_tool_result({
                "error": "REWORK_FEEDBACK_AUTHORITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "Pass empty reviewer_feedback to load the checkpoint receipt, "
                    "or echo that receipt exactly. Caller-authored feedback cannot "
                    "add files, blockers, or repair instructions."
                ),
            })
        reviewer_feedback = canonical_feedback

        if frozen_rework_resume:
            authoritative_rework_tasks = _tw.deepcopy(
                durable_worker_envelope.get("tasks")
                if durable_worker_resume
                else _tw._checkpoint_master_plan(ckpt).get("tasks") or []
            )
            authority_errors = _tw._frozen_rework_task_authority_errors(
                ckpt,
                authoritative_rework_tasks,
            )
        else:
            authoritative_rework_tasks, authority_errors = (
                _tw._authoritative_rework_tasks(
                    ckpt,
                    canonical_feedback,
                )
            )
        if authority_errors:
            abandon_result = {}
            if frozen_rework_resume:
                abandon_result = await _tw._force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "frozen_rework_task_authority_invalid",
                    actor_lock_owned=actor_lock_owned,
                )
            else:
                # Non-frozen rework stage (quality_failed / precommit_failed /
                # official_failed / repair_planned / rework_running) whose checkpoint
                # or gate receipt cannot authorize any worker-writable repair task
                # (e.g. a system-owned precompute/architecture regression that maps
                # to no policy.py edit). Abandon here instead of returning a bare
                # REWORK_TASK_AUTHORITY_INVALID: the deterministic router dispatches
                # execute_workers purely by stage and would otherwise reschedule it
                # forever. The worker_terminal_abandon_ prefix is allowed for every
                # rework stage by forced_rules (pipeline_state.py), unlike the
                # frozen_rework_ prefix which quality_failed rejects.
                abandon_result = await _tw._force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "worker_terminal_abandon_rework_task_authority_invalid",
                    actor_lock_owned=actor_lock_owned,
                )
            return _tw._json_tool_result({
                "error": "REWORK_TASK_AUTHORITY_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "validation_errors": authority_errors,
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The system could not derive signed, file-scoped repair tasks "
                    "from the checkpoint/gate receipt. Do not execute caller tasks."
                ),
            })
        if tasks_provided and _tw._canonical_tasks_digest(tasks) != _tw._canonical_tasks_digest(
            authoritative_rework_tasks
        ):
            unsigned_workers = [
                str(task.get("worker_id") or f"task_{index}")
                for index, task in enumerate(tasks)
                if not _tw._repair_contract_signature(task, next_v)
            ]
            _tw.log_system_event(
                "pipeline.worker_rework_task_authority_mismatch",
                "error",
                f"Rejected caller-rewritten rework tasks for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": ckpt.get("stage"),
                    "expected_digest": _tw._canonical_tasks_digest(authoritative_rework_tasks),
                    "supplied_digest": _tw._canonical_tasks_digest(tasks),
                    "unsigned_worker_ids": unsigned_workers,
                },
            )
            return _tw._json_tool_result({
                "error": "REWORK_TASK_AUTHORITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "expected_digest": _tw._canonical_tasks_digest(authoritative_rework_tasks),
                "supplied_digest": _tw._canonical_tasks_digest(tasks),
                "unsigned_worker_ids": unsigned_workers,
                "next_tool": "abandon_generation",
                "directive": (
                    "Pass tasks=[] to load system-synthesized repair tasks, or echo "
                    "the exact canonical list. Extra, shortened, or unsigned tasks "
                    "cannot expand repair authority."
                ),
            })
        tasks = _tw.deepcopy(authoritative_rework_tasks)
    declared_scope_violations = _tw._declared_scope_violation_files(
        ckpt,
        reviewer_feedback,
    )
    if declared_scope_violations:
        _tw.log_system_event(
            "pipeline.declared_scope_integrity_violation",
            "error",
            f"Refusing repair workers for v{next_v}: undeclared artifact edits",
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
                "violation_files": sorted(declared_scope_violations),
            },
        )
        return _tw._json_tool_result({
            "error": "DECLARED_SCOPE_INTEGRITY_VIOLATION",
            "next_v": next_v,
            "source_v": source_v,
            "violation_files": sorted(declared_scope_violations),
            "next_tool": "abandon_generation",
            "directive": (
                "A failed diff cannot authorize itself through a repair ledger. "
                "Abandon this candidate and restart from a frozen prepared/source "
                "baseline with explicit Master task scope."
            ),
        })
    if not ckpt.get("master_plan") and ckpt.get("stage") not in rework_stages:
        return _tw._json_tool_result({
            "error": "execute_workers requires a master plan. Call run_master first to produce a task plan.",
            "next_v": next_v,
            "source_v": source_v,
        })

    # Initial execution is owned by the accepted Master checkpoint.  The outer
    # orchestrator may echo that list (the MCP schema currently requires a tasks
    # argument) or pass [], but it cannot shorten/rewrite prompts, targets,
    # checks, or runtime contracts.  Rework stages use their separate,
    # deterministic synthesis/replacement routes below.
    if ckpt.get("stage") == "master_planned":
        if reviewer_feedback:
            _tw.log_system_event(
                "pipeline.worker_initial_feedback_rejected",
                "error",
                f"Rejected caller feedback on initial worker plan for v{next_v}",
                {"next_v": next_v, "source_v": source_v},
            )
            return _tw._json_tool_result({
                "error": "WORKER_INITIAL_FEEDBACK_FORBIDDEN",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "Initial master_planned execution must use the checkpoint task "
                    "verbatim with empty reviewer_feedback. Feedback is accepted only "
                    "on an explicit review/quality/precommit rework route."
                ),
            })
        _authoritative_tasks = _tw._checkpoint_master_plan(ckpt).get("tasks")
        _authority_errors = _tw._checkpoint_master_task_authority_errors(
            ckpt,
            _authoritative_tasks,
        )
        if _authority_errors:
            _tw.log_system_event(
                "pipeline.worker_task_authority_invalid",
                "error",
                f"Checkpoint worker authority invalid for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "errors": _authority_errors,
                },
            )
            return _tw._json_tool_result({
                "error": "WORKER_TASK_AUTHORITY_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": _authority_errors,
                "directive": (
                    "Do not execute workers. The accepted Master task/ledger "
                    "authority must be repaired or the generation abandoned."
                ),
            })
        if tasks_provided and tasks != _authoritative_tasks:
            _expected_digest = _tw._canonical_tasks_digest(_authoritative_tasks)
            _supplied_digest = _tw._canonical_tasks_digest(tasks)
            _tw.log_system_event(
                "pipeline.worker_task_plan_mismatch",
                "error",
                f"Rejected caller-rewritten worker tasks for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "expected_digest": _expected_digest,
                    "supplied_digest": _supplied_digest,
                    "expected_worker_ids": [
                        task.get("worker_id") for task in _authoritative_tasks
                        if isinstance(task, dict)
                    ],
                    "supplied_worker_ids": [
                        task.get("worker_id") for task in tasks if isinstance(task, dict)
                    ],
                },
            )
            return _tw._json_tool_result({
                "error": "WORKER_TASK_PLAN_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "expected_digest": _expected_digest,
                "supplied_digest": _supplied_digest,
                "directive": (
                    "Pass tasks=[] to load the checkpoint-owned plan, or pass the "
                    "exact tasks returned by run_master. Do not paraphrase them."
                ),
            })
        if durable_worker_resume:
            durable_tasks = durable_worker_envelope.get("tasks") or []
            if _tw._canonical_tasks_digest(durable_tasks) != _tw._canonical_tasks_digest(
                _authoritative_tasks
            ):
                abandon_result = await _tw._force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "durable_initial_worker_task_drift",
                    actor_lock_owned=actor_lock_owned,
                )
                worker_workflow.abandon("durable_initial_worker_task_drift")
                return _tw._json_tool_result({
                    "error": "DURABLE_INITIAL_WORKER_TASK_DRIFT",
                    "next_v": next_v,
                    "source_v": source_v,
                    **abandon_result,
                })
            _authoritative_tasks = durable_tasks
        tasks = _tw.deepcopy(_authoritative_tasks)

    review_rework_checkpoint = _tw._is_review_rework_checkpoint(ckpt)
    official_rework_checkpoint = _tw._is_official_rework_checkpoint(ckpt)
    replace_checkpoint_tasks = ckpt.get("stage") in rework_stages

    if official_rework_checkpoint and not frozen_rework_resume:
        checkpoint_tasks = _tw._checkpoint_master_plan(ckpt).get("tasks", [])
        supplied_tasks = tasks
        tasks = _tw._official_repair_tasks(ckpt, reviewer_feedback)
        replace_checkpoint_tasks = True
        _tw.log_system_event(
            "pipeline.official_repair_tasks_forced",
            "warn",
            f"Replaced prior/supplied tasks with deterministic official repair for v{next_v}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
                "old_target_files": sorted(_tw._task_target_filenames(checkpoint_tasks)),
                "supplied_target_files": sorted(_tw._task_target_filenames(supplied_tasks)),
                "new_target_files": sorted(_tw._task_target_filenames(tasks)),
                "worker_id": tasks[0].get("worker_id") if tasks else None,
            },
        )

    # If tasks are not provided, load them from the authoritative checkpoint.
    # Provider sessions are always fresh and never carry task authority in
    # remote conversation history.
    if not tasks:
        plan = _tw._checkpoint_master_plan(ckpt)
        checkpoint_tasks = plan.get("tasks", [])
        precommit_stale_reason = (
            _tw._precommit_repair_task_refresh_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if checkpoint_tasks and _tw._is_precommit_rework_checkpoint(ckpt) else ""
        )
        review_stale_reason = (
            _tw._review_repair_task_refresh_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if checkpoint_tasks and review_rework_checkpoint else ""
        )
        quality_stale_reason = (
            _tw._stale_quality_task_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if (
                checkpoint_tasks
                and not _tw._is_precommit_rework_checkpoint(ckpt)
                and not _tw._is_official_rework_checkpoint(ckpt)
                and not review_rework_checkpoint
            ) else ""
        )
        if ckpt.get("stage") in rework_stages and (
            not checkpoint_tasks
            or quality_stale_reason
            or precommit_stale_reason
            or review_stale_reason
        ):
            tasks = _tw._synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
            if tasks:
                replace_checkpoint_tasks = bool(checkpoint_tasks)
                event_type = (
                    "pipeline.workers_tasks_refreshed"
                    if checkpoint_tasks else "pipeline.workers_tasks_synthesized"
                )
                if checkpoint_tasks and _tw._is_precommit_rework_checkpoint(ckpt):
                    event_message = (
                        f"Refreshed precommit repair task(s) for v{next_v}: {precommit_stale_reason}"
                    )
                elif checkpoint_tasks and review_stale_reason:
                    event_message = (
                        f"Refreshed review repair task(s) for v{next_v}: {review_stale_reason}"
                    )
                elif quality_stale_reason:
                    event_message = (
                        f"Refreshed quality repair task(s) for v{next_v}: {quality_stale_reason}"
                    )
                else:
                    event_message = (
                        f"Synthesized {len(tasks)} rework task(s) for v{next_v} from checkpoint gate feedback"
                    )
                _tw.log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "parent2_v": ckpt.get("parent2_v"),
                        "old_target_files": sorted(_tw._task_target_filenames(checkpoint_tasks)),
                        "new_target_files": sorted(_tw._task_target_filenames(tasks)),
                        "refresh_reason": (
                            precommit_stale_reason
                            or review_stale_reason
                            or quality_stale_reason
                        ),
                        "num_tasks": len(tasks),
                        "task_kind": tasks[0].get("task_kind") if tasks else None,
                    },
                )
        elif checkpoint_tasks:
            tasks = checkpoint_tasks
            _tw.log_system_event("pipeline.workers_tasks_from_checkpoint", "info",
                             f"Tasks loaded from checkpoint for v{next_v} (LLM omitted tasks arg)",
                             {"next_v": next_v, "num_tasks": len(tasks)})
        else:
            return _tw._json_tool_result({
                "error": "No tasks provided and checkpoint has no task plan. Call run_master first.",
                "next_v": next_v,
                "source_v": source_v,
                })
        if not tasks:
            return _tw._json_tool_result({
                "error": "No tasks provided and checkpoint has no task plan. Call run_master first.",
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
            })

    if (
        not frozen_rework_resume
        and tasks
        and ckpt.get("stage") in {"quality_failed", "repair_planned", "rework_running"}
        and not _tw._is_precommit_rework_checkpoint(ckpt)
        and not _tw._is_official_rework_checkpoint(ckpt)
        and not review_rework_checkpoint
    ):
        failure_files = _tw._quality_failure_target_files(ckpt, reviewer_feedback)
        task_files = _tw._task_target_filenames(tasks)
        missing_files = sorted(failure_files - task_files)
        quality_stale_reason = _tw._stale_quality_task_reason(tasks, ckpt, reviewer_feedback)
        if missing_files or quality_stale_reason:
            refreshed_tasks = _tw._synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
            if refreshed_tasks:
                tasks = refreshed_tasks
                replace_checkpoint_tasks = True
                refresh_reason = (
                    f"old task targets missed {missing_files}" if missing_files else quality_stale_reason
                )
                _tw.log_system_event(
                    "pipeline.workers_tasks_refreshed",
                    "warn",
                    f"Refreshed quality repair task(s) for v{next_v}; {refresh_reason}",
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "missing_files": missing_files,
                        "refresh_reason": quality_stale_reason,
                        "old_target_files": sorted(task_files),
                        "new_target_files": sorted(_tw._task_target_filenames(refreshed_tasks)),
                        "num_tasks": len(refreshed_tasks),
                    },
                )

    if (
        not frozen_rework_resume
        and tasks
        and _tw._is_precommit_rework_checkpoint(ckpt)
    ):
        precommit_stale_reason = _tw._precommit_repair_task_refresh_reason(tasks, ckpt, reviewer_feedback)
    else:
        precommit_stale_reason = ""
    if tasks and _tw._is_precommit_rework_checkpoint(ckpt) and precommit_stale_reason:
        refreshed_tasks = _tw._synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
        if refreshed_tasks:
            old_files = sorted(_tw._task_target_filenames(tasks))
            tasks = refreshed_tasks
            replace_checkpoint_tasks = True
            _tw.log_system_event(
                "pipeline.workers_tasks_refreshed",
                "warn",
                f"Refreshed precommit repair task(s) for v{next_v}; {precommit_stale_reason}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_target_files": old_files,
                    "new_target_files": sorted(_tw._task_target_filenames(refreshed_tasks)),
                    "num_tasks": len(refreshed_tasks),
                    "task_kind": refreshed_tasks[0].get("task_kind") if refreshed_tasks else None,
                    "refresh_reason": precommit_stale_reason,
                },
            )

    if not frozen_rework_resume and tasks and review_rework_checkpoint:
        review_stale_reason = _tw._review_repair_task_refresh_reason(tasks, ckpt, reviewer_feedback)
    else:
        review_stale_reason = ""
    if tasks and review_rework_checkpoint and review_stale_reason:
        refreshed_tasks = _tw._synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
        if refreshed_tasks:
            old_files = sorted(_tw._task_target_filenames(tasks))
            tasks = refreshed_tasks
            replace_checkpoint_tasks = True
            _tw.log_system_event(
                "pipeline.workers_tasks_refreshed",
                "warn",
                f"Refreshed review repair task(s) for v{next_v}; {review_stale_reason}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_target_files": old_files,
                    "new_target_files": sorted(_tw._task_target_filenames(refreshed_tasks)),
                    "num_tasks": len(refreshed_tasks),
                    "task_kind": refreshed_tasks[0].get("task_kind") if refreshed_tasks else None,
                    "refresh_reason": review_stale_reason,
                },
            )

    if (
        not frozen_rework_resume
        and tasks
        and ckpt.get("stage") in rework_stages
        and not _tw._is_precommit_rework_checkpoint(ckpt)
        and not _tw._is_official_rework_checkpoint(ckpt)
        and not review_rework_checkpoint
    ):
        ordered_tasks = _tw._order_quality_repair_tasks(tasks)
        old_order = [str(task.get("worker_id", idx + 1)) for idx, task in enumerate(tasks)]
        new_order = [str(task.get("worker_id", idx + 1)) for idx, task in enumerate(ordered_tasks)]
        if new_order != old_order:
            tasks = ordered_tasks
            replace_checkpoint_tasks = True
            _tw.log_system_event(
                "pipeline.quality_repair_tasks_reordered",
                "info",
                f"Reordered quality repair tasks for v{next_v}; file_size cleanup will run last",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_order": old_order,
                    "new_order": new_order,
                },
            )

    critic_refusal = _tw._critic_advisory_rework_refusal(
        ckpt,
        tasks,
        next_v,
        source_v,
    )
    if critic_refusal:
        return _tw._json_tool_result(critic_refusal)

    task_write_scope_errors = _tw._task_write_scope_errors(tasks, next_v)
    if task_write_scope_errors:
        return _tw._json_tool_result({
            "error": "WORKER_TASK_WRITE_SCOPE_INVALID",
            "next_v": next_v,
            "source_v": source_v,
            "validation_errors": task_write_scope_errors,
            "next_tool": "abandon_generation",
            "directive": (
                "must_change_files is a completion requirement, not write "
                "authority. Every required file must already be in "
                "target_files/files_allowed."
            ),
        })

    # B6 (2026-06-30): redundant-call guard. execute_workers is NOT idempotent —
    # a redundant call (no reviewer_feedback) when workers already ran resets code
    # from source + re-runs every Worker-LLM (the single most expensive pipeline
    # step), wasting cost and mutating already-gated code. Only allow a re-run when
    # there is reviewer_feedback (a legitimate retry-after-reviewer-reject). A pure
    # redundant call must be refused so the orchestrator proceeds to the next gate.
    _b6_stage = ckpt.get("stage")
    if (not reviewer_feedback
            and _b6_stage in ("workers_done", "quality_failed", "quality_passed", "reviewed", "critic_checked", "precommit_failed", "verified")):
        if _b6_stage == "precommit_failed":
            return _tw._json_tool_result({
                "error": (
                    "Precommit failed, but execute_workers was called without reviewer_feedback. "
                    "Pass the exact precommit_eval directive/blockers as reviewer_feedback."
                ),
                "next_v": next_v,
                "source_v": source_v,
                "stage": _b6_stage,
                "intent": {
                    "kind": "rework",
                    "next_tool": "execute_workers",
                    "failure_class": "regression",
                    "authority": "tool:execute_workers",
                    "safe_to_auto_execute": False,
                },
            })
        try:
            _tw.log_system_event(
                "pipeline.workers_redundant_call_blocked", "warn",
                f"execute_workers called again for v{next_v} at stage={_b6_stage} with no "
                f"reviewer_feedback — refusing re-run (would reset code + waste Worker-LLM "
                f"cost). Proceed to the next gate instead.",
                {"next_v": next_v, "source_v": source_v, "stage": _b6_stage},
            )
        except Exception:
            pass
        return _tw._json_tool_result({
            "info": (f"Workers already ran for v{next_v} (stage={_b6_stage}). The code is in place. "
                     f"Do NOT call execute_workers again — proceed to the next pipeline gate "
                     f"(run_quality_gates / run_review / run_critic / run_precommit_eval / commit_bot)."),
            "next_v": next_v,
            "source_v": source_v,
            "stage": _b6_stage,
            "redundant_call_blocked": True,
        })

    # Circuit breaker: limit total worker failures per generation
    # Backward compat: old checkpoints used worker_invocation_count instead of worker_failure_count
    failure_count = ckpt.get("worker_failure_count", ckpt.get("worker_invocation_count", 0))
    MAX_WORKER_FAILURES = 6
    if failure_count >= MAX_WORKER_FAILURES:
        try:
            _tw.log_system_event('pipeline.circuit_breaker', 'error',
                f'Circuit breaker: {failure_count} worker failures',
                {'next_v': next_v, 'source_v': source_v, 'failure_count': failure_count})
        except Exception:
            pass
        return _tw._json_tool_result({
            "error": f"CIRCUIT BREAKER: {failure_count} worker failures already recorded this generation (max {MAX_WORKER_FAILURES}). Abandon this generation and start a new one.",
            "failure_count": failure_count,
            "next_v": next_v,
            "source_v": source_v,
        })

    return ({"frozen_rework_resume": frozen_rework_resume, "replace_checkpoint_tasks": replace_checkpoint_tasks, "review_rework_checkpoint": review_rework_checkpoint, "reviewer_feedback": reviewer_feedback, "rework_stages": rework_stages, "tasks": tasks},)  # PHASE CONTINUATION (not an exit path)


async def _execute_workers_phase_c_rework_preparation(actor_lock_owned, ckpt, durable_worker_state, durable_worker_status, frozen_rework_resume, next_dir, next_v, replace_checkpoint_tasks, review_rework_checkpoint, reviewer_feedback, source_v, tasks, worker_template, worker_workflow):
    """Phase C: one-time repair preparation -- durable-preparation resume,"""

    # When retrying after workers already ran, actually reset code from source first.
    # Previous claim that code was reset was FALSE — now we actually do it.
    force_sequential_rework = False
    task_skipper = None
    quality_skipper_config = None
    rework_plan_metadata = None
    precommit_rework_count_for_write = None
    official_rework_count_for_write = None
    mechanical_trim_results = []
    rework_preparation_dir = None
    prepared_candidate_dir = next_dir
    durable_preparation_resume = False

    def rollback_rework_preparation():
        if rework_preparation_dir is None:
            return ""
        try:
            worker_workflow.artifacts.discard_workspace(
                rework_preparation_dir
            )
            return ""
        except Exception as rollback_exc:
            return f"{type(rollback_exc).__name__}: {str(rollback_exc)[:300]}"
    existing_prepared_work = (
        (_tw._checkpoint_master_plan(ckpt).get("work_item") or {})
        if isinstance(_tw._checkpoint_master_plan(ckpt).get("work_item"), dict)
        else {}
    )
    existing_prepared_snapshot = str(
        existing_prepared_work.get("prepared_snapshot_hash") or ""
    )
    if (
        durable_worker_status == "idle"
        and ckpt.get("stage") in {"repair_planned", "rework_running"}
        and existing_prepared_snapshot
    ):
        try:
            prepared_candidate_dir = worker_workflow.artifacts.path_for(
                existing_prepared_snapshot
            )
            expected_prepared_hash = str(
                existing_prepared_work.get("repair_baseline_artifact_hash") or ""
            )
            if (
                not expected_prepared_hash
                or _tw._complete_artifact_fingerprint(prepared_candidate_dir)
                != expected_prepared_hash
            ):
                raise RuntimeError("prepared repair snapshot hash mismatch")
            durable_preparation_resume = True
            rework_plan_metadata = _tw.deepcopy(existing_prepared_work)
            frozen_worker_input = rework_plan_metadata.get(
                "frozen_worker_input"
            )
            frozen_worker_input_digest = str(
                rework_plan_metadata.get("frozen_worker_input_digest") or ""
            )
            projection_preimage_artifact_hash = str(
                rework_plan_metadata.get(
                    "projection_preimage_artifact_hash"
                )
                or ""
            )
            projection_preimage_snapshot_hash = str(
                rework_plan_metadata.get(
                    "projection_preimage_snapshot_hash"
                )
                or ""
            )
            if not isinstance(frozen_worker_input, dict):
                raise RuntimeError("frozen Worker preparation input missing")
            actual_frozen_input_digest = _tw.hashlib.sha256(
                _tw.json.dumps(
                    frozen_worker_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if actual_frozen_input_digest != frozen_worker_input_digest:
                raise RuntimeError("frozen Worker preparation input digest mismatch")
            if (
                frozen_worker_input.get("schema_version") != 4
                or frozen_worker_input.get("tasks") != tasks
                or str(frozen_worker_input.get("reviewer_feedback") or "")
                != reviewer_feedback
                or frozen_worker_input.get("worker_template_hash")
                != _tw.hashlib.sha256(worker_template.encode("utf-8")).hexdigest()
                or frozen_worker_input.get("backend_contract")
                != _dur._worker_backend_contract()
                or "worker_execution_context" in frozen_worker_input
                or not projection_preimage_artifact_hash
                or not projection_preimage_snapshot_hash
                or frozen_worker_input.get(
                    "projection_preimage_artifact_hash"
                )
                != projection_preimage_artifact_hash
                or frozen_worker_input.get(
                    "projection_preimage_snapshot_hash"
                )
                != projection_preimage_snapshot_hash
            ):
                raise RuntimeError("frozen Worker preparation input contract drift")
            projection_preimage_dir = worker_workflow.artifacts.path_for(
                projection_preimage_snapshot_hash
            )
            if (
                _tw._complete_artifact_fingerprint(projection_preimage_dir)
                != projection_preimage_artifact_hash
            ):
                raise RuntimeError("frozen Worker projection preimage mismatch")
            if (
                _tw._complete_artifact_fingerprint(next_dir)
                != projection_preimage_artifact_hash
            ):
                raise RuntimeError("canonical Worker projection preimage drift")
            precommit_rework_count_for_write = int(
                ckpt.get("precommit_rework_count") or 0
            )
            official_rework_count_for_write = int(
                ckpt.get("official_rework_count") or 0
            )
            task_kinds = {
                str(task.get("task_kind") or "")
                for task in tasks
                if isinstance(task, dict)
            }
            if (
                "quality_repair" in str(
                    existing_prepared_work.get("kind") or ""
                )
                or any("quality_repair" in kind for kind in task_kinds)
            ) and not _tw._is_precommit_rework_checkpoint(
                ckpt
            ) and not _tw._is_official_rework_checkpoint(ckpt):
                force_sequential_rework = True
                quality_skipper_config = {
                    "source_dir": _tw.get_bot_dir(source_v),
                    "expected_architecture_policy": (
                        _tw._checkpoint_master_plan(ckpt).get(
                            "architecture_policy"
                        )
                    ),
                    "master_plan": _tw._checkpoint_master_plan(ckpt),
                }
        except Exception as exc:
            return _tw._json_tool_result({
                "error": "DURABLE_REPAIR_PREPARATION_UNAVAILABLE",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
    if (
        frozen_rework_resume
        and reviewer_feedback
        and ckpt.get("stage") in {"repair_planned", "rework_running"}
    ):
        frozen_plan = _tw._checkpoint_master_plan(ckpt)
        frozen_work_item = (
            frozen_plan.get("work_item")
            if isinstance(frozen_plan.get("work_item"), dict)
            else {}
        )
        frozen_rework_kind = str(frozen_work_item.get("kind") or "")
        frozen_task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        is_frozen_quality_rework = (
            "quality_repair" in frozen_rework_kind
            or any("quality_repair" in kind for kind in frozen_task_kinds)
        )
        if (
            is_frozen_quality_rework
            and not _tw._is_precommit_rework_checkpoint(ckpt)
            and not _tw._is_official_rework_checkpoint(ckpt)
        ):
            force_sequential_rework = True
            quality_skipper_config = {
                "source_dir": _tw.get_bot_dir(source_v),
                "expected_architecture_policy": (
                    frozen_plan.get("architecture_policy")
                    if isinstance(frozen_plan.get("architecture_policy"), dict)
                    else None
                ),
                "master_plan": frozen_plan,
            }
        if ckpt.get("stage") == "repair_planned":
            rework_plan_metadata = frozen_work_item
    if (
        not frozen_rework_resume
        and not durable_preparation_resume
        and reviewer_feedback
        and ckpt.get("stage") in (
        "workers_done", "quality_failed", "quality_passed", "reviewed", "critic_checked",
        "precommit_failed", "official_failed", "repair_planned", "rework_running"
        )
    ):
        rework_kind = "quality_repair" if ckpt.get("stage") == "quality_failed" else "gate_rework"
        if ckpt.get("stage") == "official_failed":
            rework_kind = "official_repair"
        elif ckpt.get("stage") == "precommit_failed":
            rework_kind = "precommit_repair"
        elif ckpt.get("parent2_v") is not None:
            rework_kind = f"crossover_{rework_kind}"
        existing_work_item = (
            (ckpt.get("master_plan") or {}).get("work_item")
            if isinstance(ckpt.get("master_plan"), dict) else None
        )
        if (
            ckpt.get("stage") in {"repair_planned", "rework_running"}
            and isinstance(existing_work_item, dict)
            and existing_work_item.get("kind")
        ):
            rework_kind = str(existing_work_item.get("kind"))
        task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        if review_rework_checkpoint or any("review_repair" in kind for kind in task_kinds):
            rework_kind = (
                "crossover_review_repair"
                if ckpt.get("parent2_v") is not None or rework_kind.startswith("crossover_")
                else "review_repair"
            )
        elif _tw._is_official_rework_checkpoint(ckpt) or any("official_repair" in kind for kind in task_kinds):
            rework_kind = "official_repair"
        is_precommit_rework = rework_kind == "precommit_repair" or _tw._is_precommit_rework_checkpoint(ckpt)
        is_official_rework = rework_kind == "official_repair" or _tw._is_official_rework_checkpoint(ckpt)
        if is_precommit_rework:
            prior_rework_count = int(ckpt.get("precommit_rework_count") or 0)
            precommit_rework_count_for_write = prior_rework_count + 1
            if precommit_rework_count_for_write > _tw.MAX_PRECOMMIT_REWORK_ROUNDS:
                message = (
                    f"PRECOMMIT_REWORK_CIRCUIT_BREAKER: v{next_v} already used "
                    f"{prior_rework_count} precommit repair round(s) (max {_tw.MAX_PRECOMMIT_REWORK_ROUNDS}). "
                    "Abandon this generation and start a fresh direction."
                )
                _tw.log_system_event(
                    "pipeline.precommit_rework_circuit_breaker",
                    "error",
                    message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "precommit_rework_count": prior_rework_count,
                        "max_rework_rounds": _tw.MAX_PRECOMMIT_REWORK_ROUNDS,
                        "task_targets": sorted(_tw._task_target_filenames(tasks)),
                    },
                )
                return _tw._json_tool_result({
                    "error": "PRECOMMIT_REWORK_CIRCUIT_BREAKER",
                    "message": message,
                    "next_v": next_v,
                    "source_v": source_v,
                    "precommit_rework_count": prior_rework_count,
                    "max_rework_rounds": _tw.MAX_PRECOMMIT_REWORK_ROUNDS,
                    "directive": "Abandon this generation; repeated precommit repair did not converge.",
                })
        if is_official_rework:
            prior_official_rework_count = int(ckpt.get("official_rework_count") or 0)
            official_rework_count_for_write = prior_official_rework_count + 1
            if official_rework_count_for_write > _tw.MAX_OFFICIAL_REWORK_ROUNDS:
                message = (
                    f"OFFICIAL_REWORK_CIRCUIT_BREAKER: v{next_v} already used "
                    f"{prior_official_rework_count} official repair round(s) "
                    f"(max {_tw.MAX_OFFICIAL_REWORK_ROUNDS}). Abandon this generation; "
                    "repeated formal certification repair did not converge."
                )
                _tw.log_system_event(
                    "pipeline.official_rework_circuit_breaker",
                    "error",
                    message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "official_rework_count": prior_official_rework_count,
                        "max_rework_rounds": _tw.MAX_OFFICIAL_REWORK_ROUNDS,
                        "task_targets": sorted(_tw._task_target_filenames(tasks)),
                    },
                )
                abandon_result = await _tw._force_abandon_official_rework_generation(
                    next_v,
                    source_v,
                    actor_lock_owned=actor_lock_owned,
                )
                return _tw._json_tool_result({
                    "error": "OFFICIAL_REWORK_CIRCUIT_BREAKER",
                    "message": message,
                    "next_v": next_v,
                    "source_v": source_v,
                    "official_rework_count": prior_official_rework_count,
                    "max_rework_rounds": _tw.MAX_OFFICIAL_REWORK_ROUNDS,
                    "abandoned": bool(abandon_result.get("abandoned")),
                    "abandon_result": abandon_result,
                    "directive": (
                        "This generation was abandoned by the tool layer after "
                        "repeated official repair failed to converge. Start a fresh direction."
                    ),
                })
        source_dir_r = _tw.get_bot_dir(source_v)
        try:
            preparation_base = worker_workflow.artifacts.capture(next_dir)
            projection_preimage_artifact_hash = (
                _tw._complete_artifact_fingerprint(next_dir)
            )
            projection_preimage_snapshot_hash = preparation_base
            if projection_preimage_artifact_hash != preparation_base:
                raise RuntimeError(
                    "canonical repair preimage snapshot mismatch"
                )
            preparation_digest = _tw.hashlib.sha256(
                _tw.json.dumps(
                    {
                        "stage": ckpt.get("stage"),
                        "tasks": tasks,
                        "reviewer_feedback": reviewer_feedback,
                        "source_hash": _tw._complete_artifact_fingerprint(source_dir_r),
                        "precommit_rework_count": precommit_rework_count_for_write,
                        "official_rework_count": official_rework_count_for_write,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            rework_preparation_dir = worker_workflow.artifacts.preparation_workspace(
                run_id=worker_workflow.run_id,
                cycle=int(durable_worker_state.get("cycle") or 0),
                input_digest=preparation_base,
                preparation_digest=preparation_digest,
            )
            prepared_candidate_dir = rework_preparation_dir
        except Exception as exc:
            return _tw._json_tool_result({
                "error": "REWORK_PREPARATION_SNAPSHOT_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "next_tool": "abandon_generation",
                "directive": (
                    "Could not freeze the complete candidate before one-time "
                    "repair preparation. No reset or hygiene mutation was run."
                ),
            })
        reset_before_rework = _tw._should_reset_before_rework(ckpt, tasks)
        if reset_before_rework and source_dir_r.exists() and prepared_candidate_dir.exists():
            _tw._log.info(f"Resetting v{next_v} code from source v{source_v} before worker retry (incremental, preserves NEW files)")
            # Incremental reset: overwrite source files (undo worker edits) but
            # PRESERVE worker-created NEW files absent from source. This avoids
            # wiping NEW files on redundant orchestrator re-calls of execute_workers
            # (which would otherwise cause zero-changes wasted retries).
            try:
                preserved = _tw._incremental_reset_next_dir(
                    prepared_candidate_dir,
                    source_dir_r,
                )
            except Exception as exc:
                rollback_error = rollback_rework_preparation()
                return _tw._json_tool_result({
                    "error": (
                        "REWORK_PREPARATION_ROLLBACK_FAILED"
                        if rollback_error else "REWORK_SOURCE_RESET_FAILED"
                    ),
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "rollback_error": rollback_error,
                    "next_tool": "abandon_generation" if rollback_error else "execute_workers",
                })
            if preserved:
                _tw._log.info("Preserved %d worker-created NEW file(s) across reset: %s",
                          len(preserved), preserved)
        elif not reset_before_rework:
            if rework_kind == "precommit_repair" or _tw._is_precommit_rework_checkpoint(ckpt):
                _tw.log_system_event(
                    "pipeline.precommit_repair_in_place",
                    "warn",
                    f"Repairing v{next_v} in place after precommit failure; preserving candidate code",
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )
            elif "review_repair" in rework_kind:
                event_type = (
                    "pipeline.crossover_review_repair_in_place"
                    if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None
                    else "pipeline.review_repair_in_place"
                )
                event_message = (
                    f"Repairing crossover v{next_v} in place after reviewer rejection; preserving fused candidate code"
                    if event_type == "pipeline.crossover_review_repair_in_place"
                    else f"Repairing v{next_v} in place after reviewer rejection; preserving generated candidate code"
                )
                _tw.log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )
            else:
                in_place_kind = (
                    "crossover_quality_repair"
                    if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None
                    else "quality_repair"
                )
                event_type = (
                    "pipeline.crossover_quality_repair_in_place"
                    if in_place_kind == "crossover_quality_repair"
                    else "pipeline.quality_repair_in_place"
                )
                event_message = (
                    f"Repairing crossover v{next_v} in place after quality failure; preserving fused candidate code"
                    if in_place_kind == "crossover_quality_repair"
                    else f"Repairing v{next_v} in place after quality failure; preserving generated candidate code"
                )
                _tw.log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )

        try:
            from candidate_hygiene import sanitize_candidate_dir
            from workflow_profiles import get_workflow_profile
            execution_mode = getattr(
                get_workflow_profile(), "national_execution_mode", "native_tcp"
            )
            if execution_mode != "native_tcp":
                raise RuntimeError(
                    "active candidate hygiene requires the official native_tcp "
                    f"execution mode, got {execution_mode!r}"
                )
            sanitize_candidate_dir(
                prepared_candidate_dir,
                require_native_tcp=True,
            )
        except Exception as exc:
            rollback_error = rollback_rework_preparation()
            _tw.log_system_event(
                "pipeline.candidate_hygiene_failed",
                "error",
                f"Candidate hygiene failed for v{next_v}: {exc}",
                {"next_v": next_v, "source_v": source_v, "stage": ckpt.get("stage")},
            )
            return _tw._json_tool_result({
                "error": (
                    "REWORK_PREPARATION_ROLLBACK_FAILED"
                    if rollback_error else "CANDIDATE_HYGIENE_FAILED"
                ),
                "message": f"Candidate hygiene failed: {exc}",
                "rollback_error": rollback_error,
                "next_v": next_v,
                "source_v": source_v,
                "next_tool": "abandon_generation" if rollback_error else "execute_workers",
            })

        # Write intermediate checkpoint so pipeline state reflects the in-progress retry.
        # Without this, a crash between code reset and worker execution would leave
        # the checkpoint at a stale stage (e.g. "reviewed" or "critic_checked")
        # while the actual code has been wiped back to source.
        retry_plan = _tw._checkpoint_plan_with_tasks(
            ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
        )
        rework_plan_metadata = {
            "kind": rework_kind,
            "source_stage": ckpt.get("stage"),
            "reset_performed": reset_before_rework,
            "route": _tw.route_policy(ckpt),
        }
        retry_plan = {
            **retry_plan,
            "work_item": rework_plan_metadata,
        }
        for task in tasks:
            if isinstance(task, dict):
                task.setdefault("task_kind", rework_kind)
        retry_plan = _tw._plan_with_accumulated_repair_scope(ckpt, retry_plan, tasks, next_v)
        task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        is_quality_rework = (
            ckpt.get("stage") == "quality_failed"
            or "quality_repair" in rework_kind
            or any("quality_repair" in kind for kind in task_kinds)
        )
        if (
            is_quality_rework
            and not _tw._is_precommit_rework_checkpoint(ckpt)
            and not _tw._is_official_rework_checkpoint(ckpt)
            and ckpt.get("stage") in {"quality_failed", "repair_planned", "rework_running"}
        ):
            force_sequential_rework = True
            quality_skipper_config = {
                "source_dir": source_dir_r,
                "expected_architecture_policy": (
                    (_tw._checkpoint_master_plan(ckpt).get("architecture_policy"))
                    if isinstance(_tw._checkpoint_master_plan(ckpt).get("architecture_policy"), dict)
                    else None
                ),
                "master_plan": retry_plan,
            }
            try:
                mechanical_trim_results = _tw._apply_mechanical_file_size_trims(
                    tasks,
                    prepared_candidate_dir,
                    source_dir_r,
                    next_v,
                    source_v,
                )
            except Exception as exc:
                rollback_error = rollback_rework_preparation()
                return _tw._json_tool_result({
                    "error": (
                        "REWORK_PREPARATION_ROLLBACK_FAILED"
                        if rollback_error else "REWORK_MECHANICAL_TRIM_FAILED"
                    ),
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "rollback_error": rollback_error,
                    "next_tool": "abandon_generation" if rollback_error else "execute_workers",
                })

        if reset_before_rework:
            reviewer_feedback += (
                f"\n\nNOTE: This is a retry. The code in bots/{_tw.bot_name(next_v)}/ has been ACTUALLY RESET "
                f"by the system to the exact national_v{source_v} preimage. The source path remains "
                f"unreadable to this Worker. Any modifications described in the feedback above no "
                f"longer exist in the candidate — re-implement them from the injected contract."
            )
        elif rework_kind == "precommit_repair" or _tw._is_precommit_rework_checkpoint(ckpt):
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place precommit regression repair. The current code in "
                f"bots/{_tw.bot_name(next_v)}/ is the candidate that failed precommit; preserve it except "
                f"for targeted EV/matchup regression fixes."
            )
        elif rework_kind == "official_repair" or _tw._is_official_rework_checkpoint(ckpt):
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place official EXE full-certification repair. The current code in "
                f"bots/{_tw.bot_name(next_v)}/ passed local gates but failed the real Windows national platform. "
                "Preserve the candidate except for the exact compliance/state-machine/obvious-decision blocker "
                "shown in the official evidence; do not use EXE win/loss as strength tuning evidence."
            )
        elif "review_repair" in rework_kind:
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place Lead Code Reviewer repair. The current code in "
                f"bots/{_tw.bot_name(next_v)}/ is the candidate that failed the reviewer hard gate; "
                "preserve it except for the exact code-quality blocker described above."
            )
        else:
            if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None:
                reviewer_feedback += (
                    f"\n\nNOTE: This is an in-place crossover quality repair. The current code in "
                    f"bots/{_tw.bot_name(next_v)}/ is the generated crossover candidate and must be preserved "
                    f"except for the exact quality-gate blockers above."
                )
            else:
                reviewer_feedback += (
                    f"\n\nNOTE: This is an in-place quality repair. The current code in "
                    f"bots/{_tw.bot_name(next_v)}/ is the generated candidate and must be preserved "
                    f"except for the exact quality-gate blockers above."
                )
        changed_trims = [item for item in mechanical_trim_results if item.get("changed")]
        if changed_trims:
            trim_summary = "; ".join(
                f"{_tw.Path(item.get('target', item.get('file', ''))).name}: "
                f"{item.get('before')}L->{item.get('after')}L"
                for item in changed_trims
            )
            reviewer_feedback += (
                "\n\nNOTE: Before LLM workers, the pipeline mechanically removed "
                "non-behavioral Python text (comments/docstrings/blank lines) from "
                f"large file_size targets: {trim_summary}. Continue only if a blocker remains."
            )

        repair_baseline_artifact_hash = _tw._complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if not repair_baseline_artifact_hash:
            rollback_error = rollback_rework_preparation()
            return _tw._json_tool_result({
                "error": (
                    "REWORK_PREPARATION_ROLLBACK_FAILED"
                    if rollback_error else "REPAIR_BASELINE_ARTIFACT_UNAVAILABLE"
                ),
                "next_v": next_v,
                "source_v": source_v,
                "next_tool": "abandon_generation",
                "rollback_error": rollback_error,
                "directive": (
                    "Could not freeze the complete post-reset/post-hygiene repair "
                    "baseline. Do not execute Workers without a content receipt."
                ),
            })
        prepared_repair_snapshot_hash = worker_workflow.artifacts.capture(
            prepared_candidate_dir
        )
        if prepared_repair_snapshot_hash != repair_baseline_artifact_hash:
            rollback_error = rollback_rework_preparation()
            return _tw._json_tool_result({
                "error": "REPAIR_PREPARATION_SNAPSHOT_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "rollback_error": rollback_error,
            })
        frozen_preparation_input = {
            "schema_version": 4,
            "tasks": _tw.deepcopy(tasks),
            "reviewer_feedback": reviewer_feedback,
            "worker_template_hash": _tw.hashlib.sha256(
                worker_template.encode("utf-8")
            ).hexdigest(),
            "backend_contract": _dur._worker_backend_contract(),
            "projection_preimage_artifact_hash": (
                projection_preimage_artifact_hash
            ),
            "projection_preimage_snapshot_hash": (
                projection_preimage_snapshot_hash
            ),
        }
        frozen_preparation_input_digest = _tw.hashlib.sha256(
            _tw.json.dumps(
                frozen_preparation_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        rework_plan_metadata = {
            **rework_plan_metadata,
            "projection_preimage_artifact_hash": (
                projection_preimage_artifact_hash
            ),
            "projection_preimage_snapshot_hash": (
                projection_preimage_snapshot_hash
            ),
            "repair_baseline_artifact_hash": repair_baseline_artifact_hash,
            "prepared_snapshot_hash": prepared_repair_snapshot_hash,
            "frozen_worker_input": frozen_preparation_input,
            "frozen_worker_input_digest": frozen_preparation_input_digest,
        }
        retry_plan = {
            **retry_plan,
            "work_item": rework_plan_metadata,
        }
        retry_plan = _tw._plan_with_accumulated_repair_scope(
            ckpt,
            retry_plan,
            tasks,
            next_v,
        )
        repair_checkpoint_written = _tw.write_pipeline_checkpoint(
            next_v,
            source_v,
            "repair_planned",
            master_plan=retry_plan,
            reviewer_feedback=reviewer_feedback,
            worker_failure_count=ckpt.get("worker_failure_count", 0),
            precommit_rework_count=precommit_rework_count_for_write,
            official_rework_count=official_rework_count_for_write,
            repair_baseline_artifact_hash=repair_baseline_artifact_hash,
            expected_checkpoint_revision=int(
                ckpt.get("checkpoint_revision") or 0
            ),
            expected_checkpoint_stage=str(ckpt.get("stage") or ""),
            expected_workflow_run_id=str(ckpt.get("workflow_run_id") or ""),
        )
        if not repair_checkpoint_written:
            rollback_error = rollback_rework_preparation()
            return _tw._json_tool_result({
                "error": (
                    "REWORK_PREPARATION_ROLLBACK_FAILED"
                    if rollback_error else "REPAIR_BASELINE_CHECKPOINT_FAILED"
                ),
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": repair_baseline_artifact_hash,
                "candidate_restored": not rollback_error,
                "rollback_error": rollback_error,
                "directive": (
                    "The system prepared a repair baseline but could not persist its "
                    "content receipt. Do not execute Workers or claim repair authority."
                ),
            })

    return ({"force_sequential_rework": force_sequential_rework, "official_rework_count_for_write": official_rework_count_for_write, "precommit_rework_count_for_write": precommit_rework_count_for_write, "prepared_candidate_dir": prepared_candidate_dir, "quality_skipper_config": quality_skipper_config, "reviewer_feedback": reviewer_feedback, "rework_plan_metadata": rework_plan_metadata, "rework_preparation_dir": rework_preparation_dir},)  # PHASE CONTINUATION (not an exit path)
