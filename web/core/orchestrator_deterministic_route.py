"""Deterministic checkpoint-route execution + recovery classification.

Extracted from orchestrator.py as a single business responsibility: execute
safe checkpoint routes without asking the Orchestrator LLM again, classify
every checkpoint-free result (circuit-breaker abandon, crossover
incompatible/exhausted, operator shutdown, terminal proof, publication
handoff), and advance one deterministic recovery step.

Members moved here (all re-exported by orchestrator.py):

* ``_try_deterministic_checkpoint_route``  -- execute one safe checkpoint
  route deterministically.
* ``_classify_recovery_after_deterministic_route``  -- prove why a
  deterministic route no longer has a live checkpoint.
* ``_advance_deterministic_recovery``  -- run one deterministic route and
  classify every checkpoint-free result.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by these bodies that live in ``orchestrator`` are written
as ``_o.<name>`` so they resolve against the live ``orchestrator`` module
attribute, matching the pattern proven by ``orchestrator_branch_guard`` /
``orchestrator_post_generation``.  This covers every helper re-exported from
the other companions (stage routing, checkpoint recovery, tool-result
classification, post-generation cleanup, generation-context, llm-pause) plus
``log_system_event`` and the owned-session helpers.

``LLMAvailabilityBlocked`` / ``LLMAvailabilityPauseError`` and
``GenerationCostPolicy`` are imported directly (stable imports, not
monkeypatched on ``orchestrator``); ``asyncio`` is used for the timed sleeps.
"""

from __future__ import annotations

import asyncio

import orchestrator as _o
from llm_availability import LLMAvailabilityBlocked
from llm_availability_store import LLMAvailabilityPauseError
from orchestrator_cost_policy import GenerationCostPolicy


# Slice 2b one-ahead-buffer activation (default-off; see
# producer_consumer_slice2b_activation).  The activation lives in a per-process
# registry on the orchestrator module so the loop, the consumer task and the
# promotion barrier share one :class:`Slice2bActivation` instance.  These
# accessors stay no-ops when slice2b is inactive.
def _slice2b_seal_at_workers_done(checkpoint, next_v, source_v, *, ui, outcome):
    """Seal the candidate and launch the background consumer gate chain.

    Returns True iff the Slice 2b one-ahead path handled this ``workers_done``
    seam.  When True, the canonical ``run_quality_gates`` handler is NOT
    invoked inline; the consumer task runs the *unchanged* canonical gate chain
    (``run_quality_gates`` -> ``run_review`` -> ``run_critic`` ->
    ``run_precommit_eval`` -> ``commit_bot``) in the background, and the
    producer is cleared to begin the next ``prepare_generation``.  The
    promotion barrier at ``commit_bot`` (see :func:`_slice2b_promotion_barrier`)
    synchronizes publication.
    """

    try:
        from producer_consumer_slice2b_activation import (
            Slice2bActivation,
            build_snapshot_from_checkpoint,
            slice2b_active,
            stage_is_workers_done_seam,
        )
    except Exception:
        return False
    if not stage_is_workers_done_seam(checkpoint):
        return False
    if not slice2b_active():
        return False

    activation = _o._slice2b_activation_registry("get")
    if activation is None:
        return False

    # The orchestrator already computed the content-bound artifact/manifest/
    # charter digests for the canonical gate chain; Slice 2b reuses them.  When
    # they are not present on the checkpoint projection (e.g. an older
    # checkpoint), Slice 2b refuses to seal rather than guessing -- the
    # canonical inline path then runs unchanged.
    artifact_hash = checkpoint.get("candidate_artifact_hash") or checkpoint.get(
        "artifact_hash"
    )
    manifest_digest = checkpoint.get("candidate_manifest_digest") or checkpoint.get(
        "manifest_digest"
    )
    charter_digest = checkpoint.get("charter_digest")
    if not (artifact_hash and manifest_digest and charter_digest):
        return False

    snapshot = build_snapshot_from_checkpoint(
        checkpoint,
        artifact_hash=artifact_hash,
        manifest_digest=manifest_digest,
        charter_digest=charter_digest,
        quality_native_match_timing_plan=checkpoint.get(
            "quality_native_match_timing_plan"
        ),
    )
    candidate_id = snapshot["candidate_id"]

    sealed = activation.seal_at_workers_done(
        snapshot=snapshot,
        run_id=snapshot["workflow_run_id"],
        job_id=f"job:{snapshot['draft_id']}:quality-static",
        idempotency_key=f"{snapshot['draft_id']}:quality-static:v1",
        artifact_digest=artifact_hash,
        resource_claim={
            "resource_class": "cpu",
            "cpu_slots": 1,
            "memory_mb": 512,
            "gpu_slots": 0,
            "match_slots": 0,
            "official_slots": 0,
        },
        retry_policy={
            "max_attempts": 3,
            "initial_backoff_sec": 1.0,
            "backoff_multiplier": 2.0,
            "max_backoff_sec": 10.0,
            "retryable_outcomes": ["infrastructure_failure"],
        },
        deadline={
            "submitted_at_epoch": float(checkpoint.get("last_update_ts") or 0.0),
            "not_before_epoch": float(checkpoint.get("last_update_ts") or 0.0),
            "expires_at_epoch": float(checkpoint.get("last_update_ts") or 0.0) + 3600.0,
        },
        evaluation_contract_digest=str(
            checkpoint.get("evaluation_contract_digest") or charter_digest
        ),
        executor_digest=str(checkpoint.get("executor_digest") or charter_digest),
        repository_digest=str(checkpoint.get("repository_digest") or charter_digest),
        runtime_digest=str(checkpoint.get("runtime_digest") or charter_digest),
    )

    # Schedule the background consumer task running the canonical gate chain.
    # The seam runs synchronously (outside the orchestrator event loop), so we
    # register the factory here; the promotion barrier or the orchestrator loop
    # drives it via ``ensure_consumer_running`` from inside the loop.
    activation.schedule_consumer(
        candidate_id=candidate_id,
        gate_runner_factory=_o._slice2b_gate_runner_factory(next_v, source_v),
    )

    if outcome is not None:
        outcome.clear()
        outcome.update({
            "checkpoint": checkpoint,
            "route": {"next_tool": "run_quality_gates", "stage": "workers_done"},
            "result": {
                "success": True,
                "slice2b_sealed": True,
                "candidate_id": candidate_id,
                "effect_id": sealed["effect_id"],
            },
            "terminal_abandon_result": None,
        })
    try:
        _o.log_system_event(
            "pipeline.slice2b_sealed_at_workers_done",
            "info",
            f"Slice 2b sealed v{next_v}; consumer gate chain launched in background, producer may advance.",
            {
                "next_v": next_v,
                "source_v": source_v,
                "candidate_id": candidate_id,
                "effect_id": sealed["effect_id"],
            },
        )
    except Exception:
        pass
    return True


async def _slice2b_promotion_barrier(checkpoint, next_v, source_v):
    """Synchronous fail-closed barrier before ``commit_bot`` publishes.

    Returns True iff Slice 2b is active AND there is a sealed candidate for
    this generation awaiting promotion.  When True, the caller MUST first
    :func:`await activation.await_promotion` and only proceed with the
    canonical ``commit_bot`` if it returns a promoted entry.  Returns False in
    all other cases (slice2b inactive, no sealed candidate, already promoted)
    so the canonical inline ``commit_bot`` runs unchanged.
    """

    try:
        from producer_consumer_slice2b_activation import slice2b_active
    except Exception:
        return False
    if not slice2b_active():
        return False
    activation = _o._slice2b_activation_registry("get")
    if activation is None:
        return False
    candidate_id = str(
        checkpoint.get("candidate_id") or f"candidate-v{next_v}"
    )
    if candidate_id not in activation._sealed_snapshots:
        # No one-ahead seal for this generation: canonical inline path.
        return False
    if activation.ledger.is_promoted(candidate_id):
        # Already promoted by the consumer; canonical commit_bot may publish.
        return False
    # Slice 2b owns this publication: wait for the consumer to finish.
    await activation.await_promotion(candidate_id=candidate_id)
    return True


async def _try_deterministic_checkpoint_route(
    recovery,
    ui=None,
    *,
    log_level: str = "warn",
    label: str = "[Recovery]",
    cost_policy: GenerationCostPolicy | None = None,
    shutdown_mgr=None,
    outcome: dict | None = None,
):
    """Execute safe checkpoint routes without asking the Orchestrator LLM again."""
    if not recovery or recovery.get("action") != "resume":
        return False
    checkpoint = recovery.get("checkpoint") or {}
    _o._bind_generation_cost_runtime(
        checkpoint,
        ui=ui,
        policy=cost_policy,
    )
    _o._check_generation_cost_policy(ui)
    route = _o._resolve_recovery_route(checkpoint)
    if not route:
        return False

    next_tool = route.get("next_tool")
    next_v = route.get("next_v")
    source_v = route.get("source_v")
    parent2_v = route.get("parent2_v")
    stage = route.get("stage")

    if _o._deterministic_route_requires_llm(checkpoint, str(next_tool)):
        if not await _o._honor_active_llm_pause(ui, shutdown_mgr):
            return False

    saved_session_id = _o._load_orchestrator_session()
    if saved_session_id:
        session_clear_reason = (
            "deterministic_master_planned_route"
            if stage == "master_planned"
            else f"deterministic_{next_tool}_route"
        )
        _o._clear_orchestrator_session(reason=session_clear_reason)

    if next_v is None or source_v is None:
        return False

    if next_tool == "run_crossover":
        try:
            parent2_v = int(parent2_v) if parent2_v is not None else None
        except (TypeError, ValueError):
            parent2_v = None
        if parent2_v is None:
            return False

    # Slice 2b one-ahead seam: at workers_done, seal the candidate and launch
    # the background consumer gate chain instead of blocking on the inline
    # run_quality_gates.  The canonical gate chain runs unchanged inside the
    # consumer task; the producer is cleared to begin the next prepare.  When
    # slice2b is inactive (the default) or the seam refuses (missing digests,
    # high-water full), this returns False and the inline path runs unchanged.
    if (
        next_tool == "run_quality_gates"
        and stage == "workers_done"
        and _slice2b_seal_at_workers_done(
            checkpoint, next_v, source_v, ui=ui, outcome=outcome
        )
    ):
        return True

    # Slice 2b promotion barrier: at commit_bot, the canonical publication may
    # only proceed once the background consumer has promoted the sealed
    # candidate.  When slice2b is inactive or there is no sealed candidate for
    # this generation, this is a no-op and the inline commit_bot runs unchanged.
    if next_tool == "commit_bot":
        if await _slice2b_promotion_barrier(checkpoint, next_v, source_v):
            # Consumer promoted; fall through to the canonical commit_bot, which
            # owns the actual publication authority (unchanged).
            pass

    try:
        handler, args = _o._deterministic_route_handler_and_args(
            next_tool,
            checkpoint,
            next_v,
            source_v,
            parent2_v,
        )
    except Exception:
        handler, args = None, None

    if not callable(handler):
        return False

    msg = (
        f"{label} Deterministically routing v{next_v} at {stage} "
        f"to {next_tool}."
    )
    if ui:
        ui.log_history(msg, log_level)
    else:
        if log_level == "info":
            _o.log.info(msg)
        else:
            _o.log.warning(msg)
    try:
        _o.log_system_event(
            f"pipeline.deterministic_route_{next_tool}",
            log_level,
            msg,
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": stage,
                "parent2_v": checkpoint.get("parent2_v"),
                "route": route,
            },
        )
    except Exception:
        pass

    try:
        if recovery.get("post_publication_handoff") is True:
            from tool_runtime_guard import system_deterministic_route_authority

            with system_deterministic_route_authority(next_tool, checkpoint):
                result = await handler(args)
        else:
            result = await handler(args)
        data = _o._extract_tool_result_json(result)
        if outcome is not None:
            outcome.clear()
            outcome.update({
                "checkpoint": checkpoint,
                "route": route,
                "result": data,
                "terminal_abandon_result": _o._completed_abandon_tool_result(data),
            })
        # Direct checkpoint recovery bypasses the SDK ToolResult stream where
        # this conversion normally happens.  Re-establish the same typed
        # availability boundary before generic tool-error routing can retry an
        # Orchestrator/Worker or consume an infrastructure attempt.
        _o._raise_for_llm_availability_tool_result(data)
    except LLMAvailabilityBlocked as exc:
        # Direct deterministic routes do not pass through _run_one_cycle's SDK
        # stream catch. Persist the same durable control state here and leave the
        # checkpoint/attempt untouched.
        try:
            _o.persist_llm_pause(exc)
        except Exception as pause_exc:
            if ui:
                ui.log_history(
                    f"[Recovery] LLM pause persistence failed closed: {pause_exc}",
                    "error",
                )
                ui.set_status(
                    "Stopped: LLM pause persistence failed",
                    is_working=False,
                )
            _o.log.error(
                "Deterministic route could not persist LLM pause: %s",
                pause_exc,
            )
            raise LLMAvailabilityPauseError(
                "deterministic route could not persist the classified LLM pause"
            ) from pause_exc
        _o._clear_orchestrator_session(reason="deterministic_llm_availability_blocked")
        if await _o._honor_active_llm_pause(ui, shutdown_mgr):
            return True
        return False
    except LLMAvailabilityPauseError as exc:
        if ui:
            ui.log_history(f"[Recovery] LLM pause control failed closed: {exc}", "error")
            ui.set_status("Stopped: LLM pause control invalid", is_working=False)
        _o.log.error("Deterministic route LLM pause control failed closed: %s", exc)
        raise
    _o._check_generation_cost_policy(ui)
    error = data.get("error")
    success = data.get("success")
    worker_terminal_abandon = (
        next_tool == "execute_workers"
        and _o._is_worker_terminal_abandon_result(data)
    )
    if data.get("pending") and data.get("action") == "poll_commit_bot":
        wait_sec = max(5.0, min(60.0, float(data.get("retry_after_sec", 30) or 30)))
        try:
            _o.log_system_event(
                "pipeline.deterministic_route_pending",
                "info",
                f"Durable official certification remains pending for v{next_v}; polling in {wait_sec:g}s",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "next_tool": next_tool,
                    "retry_after_sec": wait_sec,
                },
            )
        except Exception:
            pass
        await asyncio.sleep(wait_sec)
        return True
    if next_tool == "run_master" and _o._is_master_ensemble_pending_retry(
        data,
        checkpoint,
    ):
        wait_sec = max(
            5.0,
            min(60.0, float(data.get("retry_after_sec", 5) or 5)),
        )
        try:
            _o.log_system_event(
                "pipeline.deterministic_master_role_retry_pending",
                "warn" if not data.get("needs_attention") else "error",
                (
                    f"Journaled Master role {data.get('slot')} remains pending "
                    f"for v{next_v}; retrying in {wait_sec:g}s"
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "slot": data.get("slot"),
                    "role_attempt": data.get("role_attempt"),
                    "accepted_slots": data.get("accepted_slots"),
                    "pending_slots": data.get("pending_slots"),
                    "retry_after_sec": wait_sec,
                    "needs_attention": bool(data.get("needs_attention")),
                },
            )
        except Exception:
            pass
        await asyncio.sleep(wait_sec)
        return True
    if (
        next_tool == "execute_workers"
        and _o._is_worker_operator_shutdown_interrupted(data, checkpoint)
        and shutdown_mgr is not None
        and shutdown_mgr.is_shutting_down
    ):
        try:
            _o.log_system_event(
                "pipeline.worker_operator_shutdown_interrupted",
                "info",
                (
                    f"Worker lease for v{next_v} was fenced attempt-neutrally "
                    "during operator shutdown"
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "workflow_run_id": data.get("workflow_run_id"),
                    "effect_id": data.get("effect_id"),
                    "lease_epoch": data.get("lease_epoch"),
                    "claimed_attempt": data.get("claimed_attempt"),
                    "restored_attempt": data.get("restored_attempt"),
                    "checkpoint_preserved": True,
                },
            )
        except Exception:
            pass
        if ui:
            ui.log_history(
                "[Recovery] Worker stopped at the operator shutdown edge; "
                "the same frozen activity will be reclaimed after restart.",
                "info",
            )
        return False
    if error or worker_terminal_abandon:
        if next_tool == "execute_workers" and (
            _o._is_worker_circuit_breaker_result(data)
            or _o._is_precommit_rework_circuit_breaker_result(data)
            or _o._is_official_rework_circuit_breaker_result(data)
            or worker_terminal_abandon
        ):
            if _o._is_precommit_rework_circuit_breaker_result(data):
                abandon_reason = "precommit_rework_circuit_breaker"
            elif _o._is_official_rework_circuit_breaker_result(data):
                abandon_reason = "official_rework_circuit_breaker"
            elif _o._is_worker_circuit_breaker_result(data):
                abandon_reason = "worker_circuit_breaker"
            else:
                abandon_reason = _o._worker_terminal_abandon_reason(data)
            if (
                _o._is_official_rework_circuit_breaker_result(data)
                and data.get("abandoned") is True
            ):
                abandon_result = data.get("abandon_result") or {
                    "abandoned": True,
                    "reason": abandon_reason,
                }
                abandoned = True
            else:
                from evolution_core import read_pipeline_checkpoint
                from tool_bot_management import (
                    _do_abandon_generation,
                    expected_abandon_identity,
                )
                abandon_result = await _do_abandon_generation(
                    reason=abandon_reason,
                    **expected_abandon_identity(read_pipeline_checkpoint()),
                )
                abandoned = bool(abandon_result.get("abandoned"))
            if outcome is not None:
                outcome["router_abandon_result"] = abandon_result
                outcome["terminal_abandon_result"] = (
                    _o._completed_abandon_tool_result(abandon_result)
                )
            msg_abandon = (
                f"{abandon_reason} reached for v{next_v}; "
                f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
            )
            if ui:
                ui.log_history(f"[Recovery] {msg_abandon}", "error" if not abandoned else "warn")
            else:
                _o.log.warning(msg_abandon)
            try:
                _o.log_system_event(
                    "pipeline.deterministic_route_abandoned",
                    "warn" if abandoned else "error",
                    msg_abandon,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": stage,
                        "result": data,
                        "abandon_result": abandon_result,
                    },
                )
            except Exception:
                pass
            return abandoned

        if next_tool == "run_crossover" and _o._is_crossover_incompatible_result(data):
            abandoned = bool(data.get("abandoned"))
            msg_abandon = (
                f"crossover_incompatible reached for v{next_v}; "
                f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
            )
            if ui:
                ui.log_history(f"[Recovery] {msg_abandon}", "warn" if abandoned else "error")
            else:
                _o.log.warning(msg_abandon) if abandoned else _o.log.error(msg_abandon)
            try:
                _o.log_system_event(
                    "pipeline.deterministic_route_abandoned",
                    "warn" if abandoned else "error",
                    msg_abandon,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": stage,
                        "result": data,
                    },
                )
            except Exception:
                pass
            return abandoned

        if next_tool == "run_crossover" and _o._is_crossover_llm_exhausted_result(data):
            abandoned = bool(data.get("abandoned"))
            msg_abandon = (
                f"crossover_llm_exhausted reached for v{next_v}; "
                f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
            )
            if ui:
                ui.log_history(f"[Recovery] {msg_abandon}", "warn" if abandoned else "error")
            else:
                _o.log.warning(msg_abandon) if abandoned else _o.log.error(msg_abandon)
            try:
                _o.log_system_event(
                    "pipeline.deterministic_route_abandoned",
                    "warn" if abandoned else "error",
                    msg_abandon,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": stage,
                        "result": data,
                    },
                )
            except Exception:
                pass
            return abandoned

        detail = f"Deterministic {next_tool} route failed for v{next_v}: {str(error)[:180]}"
        if ui:
            ui.log_history(f"[Recovery] {detail}", "error")
        else:
            _o.log.error(detail)
        try:
            _o.log_system_event(
                "pipeline.deterministic_route_failed",
                "error",
                detail,
                {"next_v": next_v, "source_v": source_v, "stage": stage, "result": data},
            )
        except Exception:
            pass
        return False

    try:
        route_succeeded = success is not False
        _o.log_system_event(
            "pipeline.deterministic_route_done",
            "success" if route_succeeded else "warn",
            f"Deterministic {next_tool} route completed for v{next_v}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": stage,
                "success": route_succeeded,
                "reported_success": success,
            },
        )
    except Exception:
        pass
    return True


def _classify_recovery_after_deterministic_route(
    recovery,
    outcome,
    next_recovery,
):
    """Prove why a deterministic route no longer has a live checkpoint."""

    result = (outcome or {}).get("result")
    if isinstance(result, dict) and result.get("recovery_blocked") is True:
        issues = list(result.get("validation_errors") or [])
        return {
            "action": "blocked",
            "reason": str(result.get("error") or "deterministic_authority_blocked"),
            "checkpoint": (recovery or {}).get("checkpoint"),
            "diagnostics": {
                "active": True,
                "recoverable": False,
                "issues": issues or ["deterministic_authority_blocked"],
                "failure_class": result.get("failure_class"),
                "operator_action": result.get("action"),
            },
        }
    if next_recovery is not None:
        return next_recovery
    checkpoint = (recovery or {}).get("checkpoint") or {}
    if (recovery or {}).get("post_publication_handoff") is True:
        return {
            "action": "publication_handoff_completed",
            "checkpoint": checkpoint,
        }
    terminal_result = (outcome or {}).get("terminal_abandon_result")
    if terminal_result is not None:
        try:
            from tool_bot_management import validate_completed_abandon_handoff

            proof = validate_completed_abandon_handoff(
                checkpoint,
                terminal_result,
            )
        except Exception as exc:
            return {
                "action": "blocked",
                "reason": "deterministic_terminal_proof_invalid",
                "checkpoint": None,
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": [
                        "deterministic_terminal_proof_invalid:"
                        f"{str(exc)[:240]}"
                    ],
                },
            }
        return {
            "action": "generation_abandoned",
            "checkpoint": None,
            "terminal_proof": proof,
        }
    return {
        "action": "blocked",
        "reason": "deterministic_checkpoint_disappeared_without_proof",
        "checkpoint": None,
        "diagnostics": {
            "active": True,
            "recoverable": False,
            "issues": [
                "deterministic_checkpoint_disappeared_without_proof"
            ],
        },
    }


async def _advance_deterministic_recovery(
    recovery,
    ui,
    *,
    cost_policy,
    shutdown_mgr,
    log_level="info",
    label="[Pipeline]",
    gen_ctx=None,
    gen_count=None,
):
    """Run one deterministic route and classify every checkpoint-free result."""

    outcome = {}
    routed = await _o._try_deterministic_checkpoint_route(
        recovery,
        ui,
        log_level=log_level,
        label=label,
        cost_policy=cost_policy,
        shutdown_mgr=shutdown_mgr,
        outcome=outcome,
    )
    if not routed and not outcome:
        return {
            "routed": False,
            "recovery": recovery,
            "outcome": outcome,
            "terminal_action": None,
        }
    next_recovery = _o._checkpoint_recovery_context(
        "deterministic_route",
        ui,
        log_level=log_level,
        label=label,
    )
    result = outcome.get("result") if isinstance(outcome, dict) else None
    shutdown_edge = bool(
        shutdown_mgr is not None and shutdown_mgr.is_shutting_down
    )
    if (
        shutdown_edge
        and isinstance(result, dict)
        and result.get("error") == "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED"
    ):
        outcome_route = outcome.get("route") if isinstance(outcome, dict) else None
        route_is_worker = bool(
            isinstance(outcome_route, dict)
            and outcome_route.get("next_tool") == "execute_workers"
        )
        if (
            route_is_worker
            and _o._is_worker_operator_shutdown_interrupted(
                result,
                (recovery or {}).get("checkpoint") or {},
            )
        ):
            # This is a routed process-lifecycle boundary, not an invitation
            # to fall through to a new Orchestrator provider stream. Keep the
            # exact checkpoint recovery for the next process; both continuous
            # and one-gen drivers observe routed=True, loop once, and stop on
            # their shutdown edge before any provider dispatch.
            return {
                "routed": True,
                "recovery": next_recovery or recovery,
                "outcome": outcome,
                "terminal_action": "operator_shutdown_interrupted",
                "terminal_proof": None,
            }
        return {
            "routed": True,
            "recovery": {
                "action": "blocked",
                "reason": "worker_operator_shutdown_projection_invalid",
                "checkpoint": (recovery or {}).get("checkpoint"),
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": [
                        "worker_operator_shutdown_projection_invalid"
                    ],
                },
            },
            "outcome": outcome,
            "terminal_action": "operator_shutdown_projection_invalid",
            "terminal_proof": None,
        }
    if (
        not routed
        and next_recovery is not None
        and next_recovery.get("action") == "resume"
        and not (
            isinstance(outcome.get("result"), dict)
            and outcome["result"].get("recovery_blocked") is True
        )
    ):
        return {
            "routed": False,
            "recovery": next_recovery,
            "outcome": outcome,
            "terminal_action": None,
        }
    classified = _o._classify_recovery_after_deterministic_route(
        recovery,
        outcome,
        next_recovery,
    )
    action = classified.get("action")
    terminal_action = action
    terminal_proof = None
    if action == "publication_handoff_completed":
        cleanup_ctx = gen_ctx or _o._generation_context_from_checkpoint(
            (recovery or {}).get("checkpoint") or {},
            gen_count=gen_count or 1,
        )
        cleanup_ok = await _o._run_post_generation_cleanup_with_timeout(
            shutdown_mgr,
            ui,
            cleanup_ctx,
            gen_count=gen_count,
        )
        if cleanup_ok is True:
            classified = None
        else:
            classified = {
                "action": "blocked",
                "reason": "post_generation_cleanup_verification_failed",
                "checkpoint": None,
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": [
                        "post_generation_cleanup_verification_failed"
                    ],
                },
            }
            terminal_action = "post_generation_cleanup_failed"
    elif action == "generation_abandoned":
        terminal_proof = classified.get("terminal_proof")
        try:
            _o.log_system_event(
                "orchestrator.deterministic_generation_abandoned",
                "warn",
                "Deterministic route reached a verified abandon boundary",
                terminal_proof or {},
            )
        except Exception:
            pass
        classified = None
    return {
        "routed": True,
        "recovery": classified,
        "outcome": outcome,
        "terminal_action": terminal_action,
        "terminal_proof": terminal_proof,
    }
