"""Master Architect dispatch subsystem.

Extracted from tool_planning.py as a single business responsibility:
the ``run_master`` tool's inline orchestration of the Master-plan /
audit / compile / validation loop. The giant ``run_master`` body (and its
nested ``_compile_and_hard_validate_master_plan``) live here verbatim as
``run_master_impl``; the ``@tool`` decoration stays in ``tool_planning`` so
the runtime git/worktree guard and ``run_master.handler`` access point are
preserved exactly on the parent module.

Every other Master-stage helper (fail-count bumping, abandon/abort handlers,
ensemble-provider parking, prepared-artifact validation, etc.) stays in
``tool_planning`` and is reached through the ``_tp`` alias.

Cross-reference policy
----------------------
* All names that the parent ``tool_planning`` module defines or imports and
  that tests monkeypatch on ``tool_planning.<name>`` are routed through
  ``_tp.`` so the monkeypatch is honored at call time. This covers:
  ``_get_ui``, ``log_system_event``, ``write_pipeline_checkpoint``,
  ``_matching_checkpoint``, ``_run_master_analysis``, ``get_bot_dir``.
* All other parent-defined Master helpers/constants used inside the body are
  ALSO routed through ``_tp.`` to avoid an import cycle (``tool_planning``
  imports this companion at module load, before those defs exist) and to
  keep call-time resolution identical to the original.
* Names that originate from third-party / stdlib modules and are never
  monkeypatched on ``tool_planning`` are imported directly here:
  ``json``, ``hashlib``, ``time``, ``deepcopy``, ``bot_name``,
  ``PROJECT_ROOT``, ``LLMAvailabilityBlocked``, the ``tool_helpers``
  utilities, etc.
"""

import hashlib
import json
import time
from copy import deepcopy

from bot_namespace import bot_name
from llm_availability import LLMAvailabilityBlocked
from tool_helpers import (
    PROJECT_ROOT,
    _json_tool_result,
    _resolve_version_args,
    _state_blocked,
    _owned_infrastructure_failure,
    _execute_exhausted_infrastructure_failure,
    _record_infrastructure_failure,
    _set_pipeline_status,
)

# Lazy reference back to tool_planning so monkeypatches on
# ``tool_planning.<name>`` are respected at call time, and so the
# parent-defined Master helpers resolve without an import cycle.
import tool_planning as _tp  # noqa: E402,F401


async def run_master_impl(args):
    _t0 = time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing source_v/next_v and no active checkpoint"})}]}
    # B1 (v125 retry-storm fix): unify next_v with the checkpoint's authoritative
    # value. The Master-failure counter (audit_attempt) is keyed on checkpoint
    # next_v; both the top-of-function circuit-breaker guard (below, ~line 336)
    # and _bump_master_fail_count gate on `checkpoint.next_v == next_v` and
    # SILENTLY zero the count on mismatch. When the orchestrator LLM passes a
    # stale next_v (e.g. from a PreCompact-injected context snapshot), every
    # failure is silently dropped and the breaker never trips — which is exactly
    # how v125 retried 47× without ever hitting MAX_MASTER_TOTAL_FAILURES.
    # Fix: if an ACTIVE checkpoint exists with a different next_v, trust the
    # checkpoint (system-authoritative) and surface the mismatch. A timed-out
    # checkpoint remains authoritative until its canonical abandon transaction
    # completes; only truly inactive archived/abandoned states are ignored.
    try:
        from evolution_infra import read_pipeline_checkpoint
        _entry_ckpt = read_pipeline_checkpoint() or {}
        _entry_next_v = _entry_ckpt.get("next_v")
        _entry_stage = _entry_ckpt.get("stage")
        _dead_stages = (None, "archived", "abandoned")
        if (_entry_next_v is not None and _entry_next_v != next_v
                and _entry_stage not in _dead_stages):
            _tp._log.warning(
                "run_master: LLM passed next_v=%s but active checkpoint is "
                "next_v=%s (stage=%s) — aligning to checkpoint to keep the "
                "Master-failure counter consistent (v125 bypass fix).",
                next_v, _entry_next_v, _entry_stage,
            )
            try:
                _tp.log_system_event(
                    "pipeline.master_next_v_mismatch", "warn",
                    f"run_master next_v={next_v} aligned to checkpoint next_v={_entry_next_v} "
                    f"(stage={_entry_stage}) — LLM passed a stale version",
                    {"args_next_v": next_v, "ckpt_next_v": _entry_next_v,
                     "source_v": source_v, "stage": _entry_stage},
                )
            except Exception:
                pass
            next_v = _entry_next_v
            if _entry_ckpt.get("source_v") is not None:
                source_v = _entry_ckpt["source_v"]
    except Exception:
        pass
    _master_entry_ckpt = _tp._matching_checkpoint(next_v, source_v)
    protocol_bootstrap_receipt = (
        (_master_entry_ckpt.get("audit_context") or {}).get("protocol_bootstrap")
        if isinstance(_master_entry_ckpt, dict)
        else None
    )
    protocol_bootstrap_no_strength = isinstance(protocol_bootstrap_receipt, dict)
    _master_infra, _master_infra_error = _owned_infrastructure_failure(
        _master_entry_ckpt,
        "run_master",
    )
    if _master_infra_error:
        return _state_blocked(
            _master_infra_error,
            next_v,
            source_v,
            _master_entry_ckpt,
        )
    _master_exhausted = await _execute_exhausted_infrastructure_failure(
        next_v,
        source_v,
        owner_tool="run_master",
    )
    if _master_exhausted is not None:
        return _json_tool_result(_master_exhausted)
    _persisted_identity_replan = (
        (_master_entry_ckpt.get("audit_context") or {}).get(
            "architecture_policy_identity_replan"
        )
        if isinstance(_master_entry_ckpt, dict)
        else None
    )
    if isinstance(_persisted_identity_replan, dict):
        try:
            persisted_identity_recovery = (
                _tp._recover_persisted_architecture_policy_identity_replan(
                    _master_entry_ckpt,
                    _tp.get_bot_dir(next_v),
                    _tp.get_bot_dir(source_v),
                )
            )
        except Exception as exc:
            _tp.log_system_event(
                "pipeline.architecture_policy_identity_replan_recovery_failed",
                "error",
                (
                    f"Could not recover persisted identity replan for v{next_v}: "
                    f"{type(exc).__name__}: {str(exc)[:240]}"
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": _master_entry_ckpt.get("stage"),
                },
            )
            return _json_tool_result({
                "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_RECOVERY_FAILED",
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": (
                    "The journaled identity migration did not close. Retry "
                    "run_master to recover the same content-CAS operation; do "
                    "not edit the checkpoint or candidate directory by hand."
                ),
            })
        if persisted_identity_recovery is not None:
            return persisted_identity_recovery
    if (
        isinstance(_master_entry_ckpt, dict)
        and _master_entry_ckpt.get("stage") == "direction_audited"
    ):
        from prepared_baseline_contract import validate_prepared_artifact_contract

        prepared_artifact_contract = (
            (_master_entry_ckpt.get("audit_context") or {}).get(
                "prepared_artifact_contract"
            )
        )
        prepared_artifact_errors = validate_prepared_artifact_contract(
            prepared_artifact_contract,
            prepared_dir=_tp.get_bot_dir(next_v),
            source_v=source_v,
            next_v=next_v,
            verify_live_content=True,
        )
        if prepared_artifact_errors:
            _tp.log_system_event(
                "pipeline.master_prepared_artifact_drift",
                "error",
                f"Master refused drifted prepared artifact v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "errors": prepared_artifact_errors,
                },
            )
            # Canonical abandon, mirroring LITERATURE_PROBE_RECEIPT_INVALID:
            # the MCP abandon_generation tool is blocked by the
            # direction_audited route guard (allowed_tools is
            # run_literature_probe/run_master only), so a "call
            # abandon_generation" directive loops forever. The ``master_``
            # reason prefix is in the direction_audited disposable allowlist,
            # so _abandon_master_generation succeeds.
            ui = _tp._get_ui()
            return await _tp._abandon_master_generation(
                next_v,
                source_v,
                error="PREPARED_ARTIFACT_CONTRACT_INVALID",
                fail_count=0,
                reason=(
                    "master_prepared_artifact_contract_invalid v"
                    + str(next_v)
                    + ": "
                    + ";".join(prepared_artifact_errors or [])[:700]
                ).rstrip(),
                event_type="pipeline.master_blocked_prepared_artifact_drift",
                event_message=(
                    f"Master v{next_v} blocked: prepared artifact contract is "
                    f"invalid and cannot be repaired by replanning; canonically "
                    f"abandoning"
                ),
                ui=ui,
                payload={"validation_errors": prepared_artifact_errors},
                directive=(
                    "The candidate changed after prepare/crossover and before "
                    "Master. This generation was canonically abandoned; rebuild "
                    "from a fresh scheduler-owned baseline."
                ),
            )
    # fix-4: idempotency guard — if master already planned for this (next_v, source_v),
    # return cached result instead of re-running (LLM intermittently violates
    # orchestrator.md:43, causing duplicate run_master calls in the same cycle).
    _ckpt_idempotent = _tp._matching_checkpoint(next_v, source_v)
    if _ckpt_idempotent and _ckpt_idempotent.get("stage") in (
        "master_planned", "workers_done", "quality_failed", "quality_passed",
        "reviewed", "critic_checked", "verified", "archived",
    ):
        _existing_plan = _ckpt_idempotent.get("master_plan")
        if _existing_plan:
            if (_ckpt_idempotent.get("parent2_v")
                    and isinstance(_existing_plan, dict)
                    and _existing_plan.get("strategy") == "crossover"):
                _tp.log_system_event(
                    "pipeline.crossover_master_call_blocked", "warn",
                    f"run_master called after crossover already produced v{next_v}; proceed to quality/retry workers, not Master",
                    {"next_v": next_v, "source_v": source_v,
                     "parent2_v": _ckpt_idempotent.get("parent2_v"),
                     "stage": _ckpt_idempotent.get("stage"),
                     "has_synthetic_plan": True},
                )
                return _json_tool_result({
                    "error": "CROSSOVER_ALREADY_DONE",
                    "next_v": next_v,
                    "source_v": source_v,
                    "parent2_v": _ckpt_idempotent.get("parent2_v"),
                    "stage": _ckpt_idempotent.get("stage"),
                    "directive": (
                        "Crossover already produced the target bot. Do NOT call run_master. "
                        "If stage=workers_done call run_quality_gates; if stage=quality_failed "
                        "call execute_workers with exact gate feedback or abandon_generation."
                    ),
                })
            _tp.log_system_event("pipeline.master_idempotent", "info",
                             f"run_master for v{next_v}: plan already exists "
                             f"(stage={_ckpt_idempotent.get('stage')}), returning cached",
                             {"next_v": next_v, "source_v": source_v})
            ui = _tp._get_ui()
            ui.log_history("Master plan already exists — returning cached (idempotent).", "info")
            return _json_tool_result({"plan": _existing_plan, "logs": ui.get_output(),
                                      "idempotent_cache": True})
        if _ckpt_idempotent.get("parent2_v") and _ckpt_idempotent.get("stage") in (
            "workers_done", "quality_failed", "quality_passed",
            "reviewed", "critic_checked", "verified", "archived",
        ):
            _tp.log_system_event(
                "pipeline.crossover_master_call_blocked", "warn",
                f"run_master called after crossover already produced v{next_v}; proceed to quality/retry workers, not Master",
                {"next_v": next_v, "source_v": source_v,
                 "parent2_v": _ckpt_idempotent.get("parent2_v"),
                 "stage": _ckpt_idempotent.get("stage")},
            )
            return _json_tool_result({
                "error": "CROSSOVER_ALREADY_DONE",
                "next_v": next_v,
                "source_v": source_v,
                "parent2_v": _ckpt_idempotent.get("parent2_v"),
                "stage": _ckpt_idempotent.get("stage"),
                "directive": (
                    "Crossover already produced the target bot. Do NOT call run_master. "
                    "If stage=workers_done call run_quality_gates; if stage=quality_failed "
                    "call execute_workers with the exact quality failure feedback or abandon_generation."
                ),
            })

    stagnation_info = args.get("stagnation_info", "No stagnation detected. Continue from latest version.")
    match_analysis = args.get("match_analysis", "")
    performance_verification = args.get("performance_verification", "")
    direction_audit_str = args.get("direction_audit", "")
    research_proposals = args.get("research_proposals", "")

    # The scheduler owns the evidence bundle.  The outer orchestrator sees the
    # same text but is not a trusted serializer for it: on v146 it converted a
    # missing-literal validation error into a contradictory instruction to omit
    # the valid reference-card id.  Prefer the digest-bound checkpoint copy and
    # make caller mismatches observable.
    _checkpoint_master_context = (
        ((_master_entry_ckpt.get("audit_context") or {}).get("master_context"))
        if isinstance(_master_entry_ckpt, dict)
        else None
    )
    if _checkpoint_master_context is not None:
        from master_context_contract import validate_master_context

        _context_errors = validate_master_context(
            _checkpoint_master_context,
            next_v=next_v,
            source_v=source_v,
        )
        if _context_errors:
            _tp.log_system_event(
                "pipeline.master_context_invalid",
                "error",
                f"Master context contract invalid for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "errors": _context_errors,
                },
            )
            return _json_tool_result({
                "error": "MASTER_CONTEXT_CONTRACT_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": _context_errors,
                "directive": (
                    "Do not run Master with caller-reconstructed evidence. Repair or "
                    "restart the scheduler-owned master context checkpoint first."
                ),
            })
        _incoming_context = {
            "stagnation_info": str(stagnation_info or ""),
            "match_analysis": str(match_analysis or ""),
            "performance_verification": str(performance_verification or ""),
        }
        _context_mismatches = [
            field
            for field, supplied in _incoming_context.items()
            if supplied and supplied != _checkpoint_master_context[field]
        ]
        stagnation_info = _checkpoint_master_context["stagnation_info"]
        match_analysis = _checkpoint_master_context["match_analysis"]
        performance_verification = _checkpoint_master_context["performance_verification"]
        if _context_mismatches:
            _tp.log_system_event(
                "pipeline.master_context_caller_mismatch",
                "warn",
                f"Ignored caller-reconstructed Master context for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "mismatched_fields": _context_mismatches,
                    "context_digest": _checkpoint_master_context.get("context_digest"),
                },
            )

    _snapshot_binding_errors = _tp._master_snapshot_binding_errors(
        _master_entry_ckpt,
        next_v,
    )
    if _snapshot_binding_errors:
        _tp.log_system_event(
            "pipeline.master_snapshot_binding_invalid",
            "error",
            f"Master v{next_v} blocked by generation evidence drift",
            {
                "next_v": next_v,
                "source_v": source_v,
                "errors": _snapshot_binding_errors,
            },
        )
        # Canonical abandon, mirroring LITERATURE_PROBE_RECEIPT_INVALID:
        # the MCP abandon_generation tool is blocked by the
        # direction_audited route guard, so a "call abandon_generation"
        # directive would loop forever. The ``master_`` reason prefix is in
        # the direction_audited disposable allowlist.
        ui = _tp._get_ui()
        return await _tp._abandon_master_generation(
            next_v,
            source_v,
            error="GENERATION_EVIDENCE_BINDING_INVALID",
            fail_count=0,
            reason=(
                "master_generation_evidence_binding_invalid v"
                + str(next_v)
                + ": "
                + ";".join(_snapshot_binding_errors or [])[:700]
            ).rstrip(),
            event_type="pipeline.master_blocked_evidence_binding",
            event_message=(
                f"Master v{next_v} blocked: generation evidence binding is "
                f"invalid and cannot be repaired by replanning; canonically "
                f"abandoning"
            ),
            ui=ui,
            payload={"validation_errors": _snapshot_binding_errors},
            directive=(
                "The selected generation snapshot is missing or no longer matches "
                "its checkpoint. This generation was canonically abandoned; "
                "re-prepare from a fresh coherent evaluation cycle."
            ),
        )

    # Literature-probe output is checkpoint-owned and producer-receipt bound.
    # Caller text and legacy four-field receipts have no injection authority.
    from pipeline_state import (
        literature_probe_receipt_binding,
        literature_probe_required,
    )

    _literature_required = literature_probe_required(_master_entry_ckpt)
    _literature_binding = None
    _literature_binding_errors = []
    if _literature_required:
        _literature_binding, _literature_binding_errors = (
            literature_probe_receipt_binding(_master_entry_ckpt)
        )
    _validated_literature_probe = None
    _literature_payload_errors: list = []
    if isinstance(_master_entry_ckpt, dict):
        _probe = _master_entry_ckpt.get("literature_probe")
        if _probe is not None:
            _validated_literature_probe = _tp._normalize_literature_probe_result(
                _probe,
                next_v,
                checkpoint=_master_entry_ckpt,
                receipt_binding=_literature_binding,
            )
            if _validated_literature_probe is None and isinstance(_probe, dict):
                # The normalize helper discards the concrete payload errors
                # when it returns None; recompute them so the terminal abandon
                # event records WHY the receipt failed instead of an empty
                # list (v180 abandoned with validation_errors: []).
                try:
                    _literature_payload_errors = (
                        _tp._literature_probe_payload_errors(
                            _probe,
                            checkpoint=_master_entry_ckpt,
                            receipt_binding=_literature_binding,
                            require_origin_checkpoint=True,
                        )
                    )
                except Exception:
                    _literature_payload_errors = []
            canonical_research = (
                str(_validated_literature_probe.get("inject_text") or "")
                if isinstance(_validated_literature_probe, dict)
                else ""
            )
            if research_proposals and research_proposals != canonical_research:
                _tp.log_system_event(
                    "pipeline.master_research_caller_mismatch",
                    "warn",
                    f"Ignored caller-reconstructed research proposal for v{next_v}",
                    {"next_v": next_v, "source_v": source_v},
                )
            research_proposals = canonical_research
        elif _checkpoint_master_context is not None:
            # New scheduler-owned checkpoints can distinguish "no probe" from
            # a lost caller argument.  Legacy checkpoints without this marker
            # retain their old caller-proposal fallback for crash compatibility.
            if research_proposals:
                _tp.log_system_event(
                    "pipeline.master_research_without_checkpoint_probe",
                    "warn",
                    f"Ignored research proposal without checkpoint probe for v{next_v}",
                    {"next_v": next_v, "source_v": source_v},
                )
            research_proposals = ""

    if protocol_bootstrap_no_strength:
        # Existing literature receipts were designed around an H2H weakness.
        # Until a separately typed non-result literature receipt exists, omit
        # them rather than laundering match conclusions through research prose.
        stagnation_info = _tp._PROTOCOL_BOOTSTRAP_NO_STRENGTH
        match_analysis = _tp._PROTOCOL_BOOTSTRAP_NO_STRENGTH
        performance_verification = _tp._PROTOCOL_BOOTSTRAP_NO_STRENGTH
        research_proposals = ""

    # A terminal attempt is satisfied only by the exact schema-v2 producer
    # receipt. The route helper's legacy four-field compatibility cannot grant
    # Master prompt authority.
    if _literature_required and _validated_literature_probe is None:
        _probe_present = isinstance(
            (_master_entry_ckpt or {}).get("literature_probe"), dict
        )
        _tp.log_system_event(
            "pipeline.master_blocked_invalid_literature_probe"
            if _probe_present
            else "pipeline.master_blocked_missing_literature_probe",
            "error",
            f"Master v{next_v} blocked: mandatory literature probe receipt is "
            + ("invalid" if _probe_present else "missing"),
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": (_master_entry_ckpt or {}).get("stage"),
                "binding_errors": _literature_binding_errors,
            },
        )
        if _probe_present:
            # An invalid literature-probe receipt is a TERMINAL, non-repairable
            # Master rejection: the receipt binds to immutable
            # master_context/direction_audit digests, so replanning can never
            # make a stale receipt valid. Per AGENTS.md ("A terminal strict
            # Master slot... must... complete canonical abandon instead of
            # re-entering run_master"), complete the canonical abandon here
            # rather than emitting a "call abandon_generation" directive. The
            # directive path loops forever: the MCP abandon_generation tool is
            # blocked by the direction_audited route guard (allowed_tools is
            # run_literature_probe/run_master only), so the abandon never
            # executes, no retry counter bumps, and no breaker fires. The
            # ``master_`` reason prefix is in the direction_audited disposable
            # allowlist, so _do_abandon_generation (called with
            # _bypass_rate_limit=True inside _abandon_master_generation)
            # succeeds.
            ui = _tp._get_ui()
            return await _tp._abandon_master_generation(
                next_v,
                source_v,
                error="LITERATURE_PROBE_RECEIPT_INVALID",
                fail_count=0,
                reason=(
                    "master_literature_probe_receipt_invalid v"
                    + str(next_v)
                    + ": "
                    + ";".join(_literature_binding_errors or [])[:700]
                ).rstrip(),
                event_type="pipeline.master_blocked_invalid_literature_probe",
                event_message=(
                    f"Master v{next_v} blocked: mandatory literature probe "
                    f"receipt is invalid and cannot be repaired by replanning; "
                    f"canonically abandoning"
                ),
                ui=ui,
                payload={
                    "validation_errors": _literature_binding_errors,
                    "literature_payload_errors": (
                        _literature_payload_errors[:20]
                    ),
                    "literature_probe_invalid": True,
                },
                directive=(
                    "The mandatory literature probe receipt is invalid and "
                    "cannot be repaired by replanning. This generation was "
                    "canonically abandoned; start a fresh generation."
                ),
            )
        return _json_tool_result({
            "error": "LITERATURE_PROBE_REQUIRED",
            "next_v": next_v,
            "source_v": source_v,
            "next_tool": "run_literature_probe",
            "validation_errors": _literature_binding_errors,
            "directive": (
                "The mandatory literature stage requires an exact schema-v2 "
                "checkpoint/dispatch/output/translation-gate producer receipt. "
                "Call run_literature_probe before run_master."
            ),
        })

    prepared_baseline = None
    prepared_capability_snapshot = None
    if isinstance(_master_entry_ckpt, dict) and _master_entry_ckpt.get("parent2_v") is not None:
        prepared_baseline = (
            (_master_entry_ckpt.get("audit_context") or {}).get(
                "prepared_baseline_contract"
            )
        )
        from prepared_baseline_contract import validate_prepared_baseline_contract

        baseline_errors = validate_prepared_baseline_contract(
            prepared_baseline,
            parent_a_dir=_tp.get_bot_dir(source_v),
            parent_b_dir=_tp.get_bot_dir(_master_entry_ckpt.get("parent2_v")),
            prepared_dir=_tp.get_bot_dir(next_v),
            source_v=source_v,
            parent2_v=_master_entry_ckpt.get("parent2_v"),
            next_v=next_v,
            verify_live_content=True,
        )
        try:
            from evidence_snapshot import load_generation_snapshot_identity

            live_h2h_identity = load_generation_snapshot_identity(next_v)
            bound_h2h_identity = (
                prepared_baseline.get("h2h_snapshot_identity")
                if isinstance(prepared_baseline, dict)
                else {}
            ) or {}
            if not live_h2h_identity.get("available"):
                baseline_errors.append(
                    "prepared_baseline_h2h_snapshot_unavailable:"
                    f"{live_h2h_identity.get('reason', 'unknown')}"
                )
            for field in ("manifest_digest", "sha256"):
                expected_value = str(bound_h2h_identity.get(field) or "")
                if expected_value and expected_value != str(
                    live_h2h_identity.get(field) or ""
                ):
                    baseline_errors.append(
                        f"prepared_baseline_h2h_{field}_mismatch"
                    )
        except Exception as exc:
            baseline_errors.append(
                "prepared_baseline_h2h_snapshot_error:"
                f"{type(exc).__name__}:{str(exc)[:200]}"
            )
        if baseline_errors:
            _tp.log_system_event(
                "pipeline.master_prepared_baseline_invalid",
                "error",
                f"Master v{next_v} blocked by invalid prepared crossover baseline",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "parent2_v": _master_entry_ckpt.get("parent2_v"),
                    "errors": baseline_errors[:20],
                },
            )
            # Canonical abandon, mirroring LITERATURE_PROBE_RECEIPT_INVALID:
            # the MCP abandon_generation tool is blocked by the
            # direction_audited route guard, so a "call abandon_generation"
            # directive would loop forever. The ``master_`` reason prefix is in
            # the direction_audited disposable allowlist.
            ui = _tp._get_ui()
            return await _tp._abandon_master_generation(
                next_v,
                source_v,
                error="CROSSOVER_PREPARED_BASELINE_INVALID",
                fail_count=0,
                reason=(
                    "master_crossover_prepared_baseline_invalid v"
                    + str(next_v)
                    + ": "
                    + ";".join(baseline_errors or [])[:700]
                ).rstrip(),
                event_type="pipeline.master_blocked_crossover_baseline",
                event_message=(
                    f"Master v{next_v} blocked: prepared crossover baseline is "
                    f"invalid and cannot be repaired by replanning; canonically "
                    f"abandoning"
                ),
                ui=ui,
                payload={
                    "validation_errors": baseline_errors,
                    "parent2_v": _master_entry_ckpt.get("parent2_v"),
                },
                directive=(
                    "The digest-bound prepared crossover child no longer matches "
                    "its checkpoint contract. This generation was canonically "
                    "abandoned; rerun crossover from a fresh baseline."
                ),
            )
        prepared_capability_snapshot = prepared_baseline.get(
            "capability_snapshot"
        )

    fresh_empty_pool_bootstrap = _tp._is_fresh_empty_pool_bootstrap(
        _master_entry_ckpt
    )
    architecture_source_dir = None
    if protocol_bootstrap_no_strength:
        try:
            if fresh_empty_pool_bootstrap:
                from runtime_architecture_policy import (
                    build_lineage_only_prepared_capability_snapshot,
                )

                prepared_capability_snapshot = (
                    build_lineage_only_prepared_capability_snapshot(
                        bot_name(source_v),
                        _tp.get_bot_dir(next_v),
                    )
                )
            else:
                from runtime_architecture_policy import (
                    build_prepared_capability_snapshot,
                )

                architecture_source_dir = _tp.get_bot_dir(source_v)
                prepared_capability_snapshot = build_prepared_capability_snapshot(
                    architecture_source_dir,
                    _tp.get_bot_dir(next_v),
                )
            architecture_assessment = _tp._build_generation_architecture_policy(
                source_v,
                prepared_capability_snapshot=prepared_capability_snapshot,
                prepared_dir=_tp.get_bot_dir(next_v),
                allow_lineage_only_source=fresh_empty_pool_bootstrap,
            )
        except Exception as exc:
            architecture_assessment = {
                "outcome": "infrastructure_failure",
                "policy": None,
                "capabilities": None,
                "infrastructure_failures": [{
                    "component": "protocol_bootstrap_capability_snapshot",
                    "failure_class": "internal_infrastructure",
                    "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
                }],
            }
    elif prepared_capability_snapshot is None:
        # Preserve the legacy/single-parent call shape for test and plugin
        # adapters that replace this helper with a one-argument provider.
        architecture_assessment = _tp._build_generation_architecture_policy(source_v)
    else:
        architecture_assessment = _tp._build_generation_architecture_policy(
            source_v,
            prepared_capability_snapshot=prepared_capability_snapshot,
        )
    if architecture_assessment.get("outcome") == "infrastructure_failure":
        from national_runtime_probe import RUNTIME_PROBE_IDENTITY_DIGEST
        from pipeline_infrastructure import infrastructure_attempt_key

        source_fingerprint = _tp._master_source_fingerprint(
            _master_entry_ckpt,
            source_v,
        )
        failures = architecture_assessment.get("infrastructure_failures") or []
        infra_component = str(
            failures[0].get("component")
            if failures and isinstance(failures[0], dict)
            else "national_runtime_probe"
        )
        issues = [
            f"{item.get('component', 'national_runtime_probe')}: "
            + ", ".join(str(issue) for issue in (item.get("issues") or [])[:8])
            for item in failures
            if isinstance(item, dict)
        ] or ["source national runtime capability probe was inconclusive"]
        attempt_key = infrastructure_attempt_key(
            component=infra_component,
            source_fingerprint=source_fingerprint,
            harness_identity=RUNTIME_PROBE_IDENTITY_DIGEST,
            extra={"source_v": source_v, "next_v": next_v, "phase": "master_policy"},
        )
        infra_result = await _record_infrastructure_failure(
            next_v,
            source_v,
            owner_tool="run_master",
            resume_stage="direction_audited",
            component=infra_component,
            code=f"{infra_component}_infrastructure_failure",
            attempt_key=attempt_key,
            issues=issues,
            max_attempts=3,
            metadata={
                "source_fingerprint": source_fingerprint,
                "runtime_probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
                "phase": "master_policy",
            },
        )
        attempt = (infra_result.get("infra_failure") or {}).get("attempt")
        _tp.log_system_event(
            "pipeline.architecture_policy_infrastructure",
            "error" if infra_result.get("action") == "abandon_generation" else "warn",
            f"Source runtime probe unavailable for v{next_v} policy (attempt {attempt or '?'}/3)",
            {
                "source_v": source_v,
                "next_v": next_v,
                "issues": issues,
                **infra_result,
            },
        )
        return _json_tool_result({
            **infra_result,
            "error": "ARCHITECTURE_POLICY_INFRASTRUCTURE",
            "source_v": source_v,
            "next_v": next_v,
            "directive": (
                "Source capability infrastructure retry exhausted; generation was safely abandoned."
                if infra_result.get("action") == "abandon_generation"
                else "Retry run_master for the same generation; do not execute workers."
            ),
        })
    if architecture_assessment.get("outcome") == "source_invalid":
        ui = _tp._get_ui()
        return await _tp._abandon_master_generation(
            next_v,
            source_v,
            error="ARCHITECTURE_POLICY_SOURCE_INVALID",
            fail_count=0,
            reason=f"architecture_source_invalid v{source_v}",
            event_type="pipeline.architecture_policy_source_invalid",
            event_message=(
                f"Native architecture source v{source_v} is invalid; abandoning v{next_v}"
            ),
            ui=ui,
            payload={"issues": architecture_assessment.get("issues") or []},
            directive=(
                "The selected native source lacks the required national entry. The generation "
                "was abandoned; repair source eligibility instead of running workers."
            ),
        )
    architecture_policy = architecture_assessment.get("policy")
    if prepared_baseline is not None:
        stored_prepared_policy = (
            ((_master_entry_ckpt.get("audit_context") or {}).get("crossover") or {})
            .get("prepared_architecture_policy")
        )
        if not isinstance(stored_prepared_policy, dict) or (
            stored_prepared_policy.get("policy_digest")
            != (architecture_policy or {}).get("policy_digest")
        ):
            # Canonical abandon, mirroring LITERATURE_PROBE_RECEIPT_INVALID:
            # the MCP abandon_generation tool is blocked by the
            # direction_audited route guard, so a "call abandon_generation"
            # directive would loop forever. The ``master_`` reason prefix is in
            # the direction_audited disposable allowlist.
            _identity_errors = [
                "prepared_architecture_policy_digest_mismatch",
                "stored=" + str(
                    (stored_prepared_policy or {}).get("policy_digest")
                    if isinstance(stored_prepared_policy, dict)
                    else ""
                ),
                "current=" + str(
                    (architecture_policy or {}).get("policy_digest")
                ),
            ]
            ui = _tp._get_ui()
            return await _tp._abandon_master_generation(
                next_v,
                source_v,
                error="CROSSOVER_PREPARED_POLICY_IDENTITY_MISMATCH",
                fail_count=0,
                reason=(
                    "master_crossover_policy_identity_mismatch v"
                    + str(next_v)
                    + ": "
                    + ";".join(_identity_errors)[:700]
                ).rstrip(),
                event_type="pipeline.master_blocked_crossover_identity",
                event_message=(
                    f"Master v{next_v} blocked: prepared crossover policy identity "
                    f"mismatch and cannot be repaired by replanning; canonically "
                    f"abandoning"
                ),
                ui=ui,
                payload={
                    "validation_errors": _identity_errors,
                    "parent2_v": _master_entry_ckpt.get("parent2_v"),
                    "stored_policy_digest": (
                        (stored_prepared_policy or {}).get("policy_digest")
                        if isinstance(stored_prepared_policy, dict)
                        else ""
                    ),
                    "current_policy_digest": (
                        (architecture_policy or {}).get("policy_digest")
                    ),
                },
                directive=(
                    "The prepared child policy no longer matches the current "
                    "system contract. This generation was canonically abandoned; "
                    "rerun crossover. Never reset the child to Parent A while "
                    "retaining two-parent lineage."
                ),
            )
    if (
        _master_infra is not None
        and _master_infra.get("component")
        not in {"master_llm", "master_plan_audit_llm"}
    ):
        from pipeline_infrastructure import infrastructure_failure_digest

        cleared = _tp.write_pipeline_checkpoint(
            next_v,
            source_v,
            "direction_audited",
            clear_infra_failure=True,
            infra_failure_owner="run_master",
            expected_infra_failure_digest=infrastructure_failure_digest(_master_infra),
            touch_stage_timestamp=True,
        )
        if not cleared:
            return _state_blocked(
                "source runtime probe recovered but its infrastructure overlay could not be cleared",
                next_v,
                source_v,
                _tp._matching_checkpoint(next_v, source_v),
            )

    _set_pipeline_status(f"Master planning for v{next_v}")
    _tp._touch_master_checkpoint(next_v, source_v, phase="run_master_start")

    # Hard cap: refuse to re-burn Master LLM budget if it has already failed
    # (plan-JSON collapse or audit rejection) MAX_MASTER_TOTAL_FAILURES times
    # this generation. See MAX_MASTER_TOTAL_FAILURES docstring.
    try:
        from evolution_infra import read_pipeline_checkpoint
        _ckpt_m = read_pipeline_checkpoint() or {}
        _master_fails = int(_ckpt_m.get("audit_attempt") or 0) if _ckpt_m.get("next_v") == next_v else 0
    except Exception:
        _master_fails = 0
    if _master_fails >= _tp.MAX_MASTER_TOTAL_FAILURES:
        _ui = _tp._get_ui()
        return await _tp._abandon_master_generation(
            next_v,
            source_v,
            error="MASTER_EXHAUSTED",
            fail_count=_master_fails,
            reason=f"master_exhausted ({_master_fails} fails)",
            event_type="pipeline.master_exhausted",
            event_message=(
                f"Master exhausted {_master_fails} attempts for v{next_v} — "
                "refusing retry and abandoning"
            ),
            ui=_ui,
            directive=(
                f"Master planning failed {_master_fails} times for v{next_v}. "
                "This generation has been abandoned (checkpoint cleared, incomplete "
                "dir removed, session cleared). The next cycle must start fresh."
            ),
        )

    # Direction audit is persisted by its owning tool. The caller copy cannot
    # override the checkpoint verdict or rewrite mandatory directions.
    direction_audit = None
    _checkpoint_direction_audit = (
        _master_entry_ckpt.get("direction_audit")
        if isinstance(_master_entry_ckpt, dict)
        else None
    )
    _caller_direction_audit = None
    if direction_audit_str:
        try:
            _caller_direction_audit = (
                json.loads(direction_audit_str)
                if isinstance(direction_audit_str, str)
                else direction_audit_str
            )
        except (json.JSONDecodeError, TypeError):
            pass
    if isinstance(_checkpoint_direction_audit, dict):
        direction_audit = deepcopy(_checkpoint_direction_audit)
        if (
            isinstance(_caller_direction_audit, dict)
            and _caller_direction_audit != _checkpoint_direction_audit
        ):
            _tp.log_system_event(
                "pipeline.direction_audit_caller_mismatch",
                "warn",
                f"Ignored caller-reconstructed direction audit for v{next_v}",
                {"next_v": next_v, "source_v": source_v},
            )
    elif isinstance(_caller_direction_audit, dict):
        direction_audit = _caller_direction_audit

    # Inject only the current, checkpoint-bound Direction audit constraint when
    # it found repetition.
    if protocol_bootstrap_no_strength:
        # Direction Audit remains a durable stage receipt, but its historical
        # match/critic conclusions are not evidence for the first strict plan.
        direction_audit = None
    elif direction_audit and direction_audit.get("llm_failed"):
        _tp._log.warning(
            "Direction audit for v%s reported LLM infrastructure failure — "
            "skipping audit mandatory_constraints injection (untrustworthy).",
            next_v,
        )
        try:
            _tp.log_system_event(
                "pipeline.direction_audit_infra", "warn",
                f"Direction audit for v{next_v} unavailable (LLM infra error). "
                "Skipping audit constraints.",
                {"next_v": next_v, "source_v": source_v},
            )
        except Exception:
            pass
    elif direction_audit and direction_audit.get("repetition_detected") and direction_audit.get("mandatory_constraints"):
        constraint_block = (
            f"\n\n# Direction Audit Constraints (MANDATORY)\n"
            f"The Direction Auditor detected that recent generations are stuck repeating the same approach.\n"
            f"**DO NOT repeat these exhausted directions:** {', '.join(direction_audit.get('exhausted_directions', []))}\n"
            f"**Mandatory constraint:** {direction_audit['mandatory_constraints']}\n"
        )
        if direction_audit.get("suggested_direction"):
            constraint_block += f"**Suggested alternative:** {direction_audit['suggested_direction']}\n"
        constraint_block += "\nYou MUST comply with these constraints. A plan that repeats an exhausted direction will be rejected.\n"
        performance_verification = (performance_verification or "") + constraint_block

    ui = _tp._get_ui()

    # --- Extract replay_spotlight for Master prompt ---
    replay_spotlight = _tp._PROTOCOL_BOOTSTRAP_NO_STRENGTH
    if not protocol_bootstrap_no_strength:
        replay_spotlight = ""
        try:
            from evidence_snapshot import load_generation_evaluation_snapshot

            _replay_evidence = load_generation_evaluation_snapshot(next_v)
            if not _replay_evidence.get("available"):
                raise RuntimeError("generation replay evidence unavailable")
            _spotlight_payload = _replay_evidence.get("replay_spotlight")
            if not isinstance(_spotlight_payload, dict):
                raise RuntimeError("generation replay spotlight payload missing")
            if _spotlight_payload.get("bot") != bot_name(source_v):
                raise RuntimeError("generation replay spotlight bot mismatch")
            if _spotlight_payload.get("evaluation_identity_digest") != (
                (_replay_evidence.get("manifest") or {}).get(
                    "evaluation_identity_digest"
                )
            ):
                raise RuntimeError("generation replay spotlight identity mismatch")
            replay_spotlight = str(_spotlight_payload.get("text") or "")
        except Exception:
            pass

    # The Replay Spotlight below is authoritative for current-generation hand
    # IDs. Side contexts can carry historical GxHy references from old audits,
    # research proposals, or match summaries; redact those before Master sees
    # them so the hard fabricated-evidence gate can remain strict.
    _anchor_map = (
        None
        if protocol_bootstrap_no_strength
        else _tp._load_replay_anchor_map(next_v)
    )
    _citation_sanitized = {}
    for _name, _value in (
        ("stagnation_info", stagnation_info),
        ("match_analysis", match_analysis),
        ("performance_verification", performance_verification),
        ("research_proposals", research_proposals),
    ):
        _clean, _count = _tp._sanitize_unverified_replay_citations(_value, _anchor_map)
        if _count:
            _citation_sanitized[_name] = _count
        if _name == "stagnation_info":
            stagnation_info = _clean
        elif _name == "match_analysis":
            match_analysis = _clean
        elif _name == "performance_verification":
            performance_verification = _clean
        elif _name == "research_proposals":
            research_proposals = _clean
    if _citation_sanitized:
        try:
            _tp.log_system_event(
                "pipeline.master_context_citations_sanitized",
                "warn",
                f"Master v{next_v} context had stale replay IDs redacted",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "counts": _citation_sanitized,
                },
            )
        except Exception:
            pass

    # --- Read frozen action diagnostics for Master prompt ---
    _master_evaluation = {}
    if not protocol_bootstrap_no_strength:
        try:
            from evidence_snapshot import load_generation_evaluation_snapshot

            _master_evaluation = load_generation_evaluation_snapshot(next_v)
            if not _master_evaluation.get("available"):
                _master_evaluation = {}
        except Exception:
            _master_evaluation = {}
    bot_action_stats = (
        _tp._PROTOCOL_BOOTSTRAP_NO_STRENGTH
        if protocol_bootstrap_no_strength
        else ""
    )
    try:
        _all_stats = _master_evaluation.get("action_stats") or {}
        if _all_stats:
            _source_bot_name = bot_name(source_v)
            _bot_stats = _all_stats.get(_source_bot_name)
            if _bot_stats:
                # Format as compact text for prompt injection
                _parts = []
                _native_trackers = _bot_stats.get("opponent_trackers") or {}
                for _street in ("preflop", "flop", "turn", "river"):
                    if _native_trackers:
                        _semantic = {}
                        for _tracker in _native_trackers.values():
                            _counts = (
                                (_tracker.get("semantic_street_actions") or {}).get(
                                    _street
                                )
                                or {}
                            )
                            for _action, _count in _counts.items():
                                _semantic[_action] = _semantic.get(_action, 0) + int(
                                    _count or 0
                                )
                        _total = sum(_semantic.values())
                        if _total > 0:
                            _parts.append(
                                f"{_street}: fold={_semantic.get('fold', 0)/_total:.1%} "
                                f"match_call={_semantic.get('call', 0)/_total:.1%} "
                                f"check={_semantic.get('check', 0)/_total:.1%} "
                                f"pass={_semantic.get('pass', 0)/_total:.1%} "
                                f"raise={_semantic.get('raise', 0)/_total:.1%} "
                                f"allin={_semantic.get('allin', 0)/_total:.1%} "
                                f"(semantic_n={_total})"
                            )
                        continue
                    _st = _bot_stats.get(_street)
                    if _st and _st.get("total", 0) > 0:
                        _total = _st["total"]
                        _parts.append(
                            f"{_street}: fold={_st.get('fold', 0)/_total:.1%} "
                            f"call={_st.get('call', 0)/_total:.1%} "
                            f"raise={_st.get('raise', 0)/_total:.1%} "
                            f"(n={_total})"
                        )
                if _parts:
                    bot_action_stats = (
                        f"Action frequencies for {_source_bot_name}:\n"
                        + "\n".join(_parts)
                    )
    except Exception:
        pass

    # --- Phase 3: per-opponent behavior profiles for Master prompt ---
    # Reads the nested per-opponent breakdown (bot_action_stats_per_opp.json,
    # written by elo_daemon alongside the flat file). For the source bot we
    # surface its most lopsided matchups (by h2h win_rate) with a compact
    # aggression / fold-to-bet / cbet / barrel line each, so the Master can
    # plan opponent-specific adaptations. Advisory: read failure -> "".
    opponent_profiles = (
        _tp._PROTOCOL_BOOTSTRAP_NO_STRENGTH
        if protocol_bootstrap_no_strength
        else ""
    )
    try:
        _per_opp_all = _master_evaluation.get("action_stats_per_opp") or {}
        if _per_opp_all:
            _source_bot = bot_name(source_v)
            _opp_map = {}
            # Rank opponents by h2h win_rate (most-beaten and most-beating) to
            # avoid prompt bloat: keep the K most extreme matchups. Strength
            # ordering comes only from the strict generation snapshot; the live
            # top-level H2H file may have advanced since source selection.
            _h2h_for_rank = {}
            try:
                from tool_helpers import _h2h_stats

                _evaluation = _master_evaluation
                if not _evaluation.get("available"):
                    raise RuntimeError("generation evidence snapshot unavailable")
                _h2h = _evaluation.get("h2h") or {}
                _active_opponents = set(
                    (_evaluation.get("selection") or {}).get("active_bots") or []
                )
                _opp_map = {
                    _opp: ((_per_opp_all.get(_opp) or {}).get(_source_bot) or {})
                    for _opp in _active_opponents
                    if _opp != _source_bot
                    and isinstance((_per_opp_all.get(_opp) or {}).get(_source_bot), dict)
                }
                for _opp in _opp_map:
                    _st = _h2h_stats(_source_bot, _opp, _h2h)
                    if _st:
                        _h2h_for_rank[_opp] = _st
            except Exception:
                # No frozen active-pool/H2H authority means no profile ranking;
                # do not silently fall back to live or inactive opponents.
                _opp_map = {}
            _PROFILES_K = 6
            _action_sample = lambda _opp: sum(
                int(_opp_map[_opp].get(_street, {}).get("total", 0) or 0)
                for _street in ("preflop", "flop", "turn", "river")
            )
            _adequate = {
                _opp: _stats
                for _opp, _stats in _h2h_for_rank.items()
                if int(_stats.get("games", 0) or 0) >= 10
            }
            if _adequate:
                _ranked = sorted(
                    _adequate.items(), key=lambda kv: kv[1]["win_rate"]
                )
                _selected = [o for o, _ in _ranked[:_PROFILES_K // 2]]
                _selected += [o for o, _ in _ranked[-(_PROFILES_K // 2):]]
                # Dedup while preserving order.
                _seen = set()
                _selected = [o for o in _selected if not (o in _seen or _seen.add(o))]
                if len(_selected) < _PROFILES_K:
                    _selected.extend(
                        _opp
                        for _opp in sorted(
                            (_opp for _opp in _opp_map if _opp not in _seen),
                            key=_action_sample,
                            reverse=True,
                        )[:_PROFILES_K - len(_selected)]
                    )
            else:
                # No adequate H2H signal: use observation volume, but every H2H
                # label below remains explicitly sparse/advisory.
                _selected = sorted(
                    _opp_map,
                    key=_action_sample,
                    reverse=True,
                )[:_PROFILES_K]
            _lines = []
            for _opp in _selected:
                _ostats = _opp_map.get(_opp, {})
                _matchup = _h2h_for_rank.get(_opp)
                _wr_str = " h2h=unobserved"
                if _matchup is not None:
                    _games = int(_matchup.get("games", 0) or 0)
                    _wr = float(_matchup.get("win_rate", 0.5) or 0.5)
                    _sample_class = (
                        "confirmed_weakness"
                        if _games >= 10 and _wr < 0.40
                        else "confirmed_strength"
                        if _games >= 10 and _wr > 0.60
                        else "adequate_context"
                        if _games >= 10
                        else "sparse_advisory"
                    )
                    _wr_str = (
                        f" h2h_games={_games} wins={int(_matchup.get('wins', 0) or 0)} "
                        f"losses={int(_matchup.get('losses', 0) or 0)} "
                        f"draws={int(_matchup.get('draws', 0) or 0)} "
                        f"h2h_wr={_wr:.2f} sample_class={_sample_class}"
                    )
                _n = (
                    sum(_ostats.get(s, {}).get("total", 0)
                        for s in ("preflop", "flop", "turn", "river"))
                )
                if _n == 0:
                    continue
                _street_bits = []
                _tracker = _ostats.get("opponent_tracker") or {}
                _is_native_tracker = (
                    _tracker.get("source")
                    == "national_native_opponent_tracker"
                )
                for _street in ("preflop", "flop", "turn", "river"):
                    _st = _ostats.get(_street, {})
                    _tot = _st.get("total", 0)
                    if _tot <= 0:
                        continue
                    _calls = _st.get("call", 0)
                    _raises = _st.get("raise", 0)
                    _folds = _st.get("fold", 0)
                    _ftb = _st.get("fold_to_bet", 0)
                    _cbet = _st.get("cbet", 0)
                    _barrel = _st.get("barrel", 0)
                    _af = (_raises / _calls) if _calls > 0 else None
                    _af_str = f"{_af:.1f}" if _af is not None else "n/a"
                    if _is_native_tracker:
                        _semantic = (
                            (_tracker.get("semantic_street_actions") or {}).get(
                                _street
                            )
                            or {}
                        )
                        _semantic_total = sum(
                            int(value or 0) for value in _semantic.values()
                        )
                        if _semantic_total <= 0:
                            continue
                        _street_bits.append(
                            f"{_street}: fold={int(_semantic.get('fold', 0) or 0)/_semantic_total:.1%} "
                            f"match_call={int(_semantic.get('call', 0) or 0)/_semantic_total:.1%} "
                            f"check={int(_semantic.get('check', 0) or 0)/_semantic_total:.1%} "
                            f"pass={int(_semantic.get('pass', 0) or 0)/_semantic_total:.1%} "
                            f"raise={int(_semantic.get('raise', 0) or 0)/_semantic_total:.1%} "
                            f"allin={int(_semantic.get('allin', 0) or 0)/_semantic_total:.1%} "
                            f"(semantic_n={_semantic_total})"
                        )
                    else:
                        _street_bits.append(
                            f"{_street}: AF={_af_str} "
                            f"ftb={_ftb}/{_folds}f cbet={_cbet} "
                            f"barrel={_barrel} (n={_tot})"
                        )
                _terminal = _tracker.get("terminal_response") or {}
                if _terminal:
                    _ftr = _terminal.get("fold_to_raise")
                    _ftj = _terminal.get("fold_to_jam")
                    _roc = _terminal.get("river_overcall")
                    _raise_n = int(
                        (_terminal.get("facing_raise") or {}).get(
                            "opportunities", 0
                        )
                        or 0
                    )
                    _jam_n = int(
                        (_terminal.get("facing_allin") or {}).get(
                            "opportunities", 0
                        )
                        or 0
                    )
                    _street_bits.append(
                        "terminal: "
                        f"fold_to_raise={_ftr:.1%} (n={_raise_n}) "
                        if isinstance(_ftr, (int, float))
                        else f"terminal: fold_to_raise=unknown (n={_raise_n}) "
                    )
                    _street_bits[-1] += (
                        f"fold_to_jam={_ftj:.1%} (n={_jam_n}) "
                        if isinstance(_ftj, (int, float))
                        else f"fold_to_jam=unknown (n={_jam_n}) "
                    )
                    _street_bits[-1] += (
                        f"river_overcall={_roc:.1%} "
                        f"(n={int(_terminal.get('river_overcall_samples', 0) or 0)})"
                        if isinstance(_roc, (int, float))
                        else "river_overcall=unknown"
                    )
                _showdown = _tracker.get("showdown_range") or {}
                if int(_showdown.get("samples", 0) or 0) > 0:
                    _street_bits.append(
                        "showdown_range: "
                        f"samples={int(_showdown.get('samples', 0) or 0)} "
                        f"buckets={json.dumps(_showdown.get('bucket_counts') or {}, sort_keys=True)}"
                    )
                if _street_bits:
                    _lines.append(
                        f"vs {_opp}{_wr_str} (n={_n}): " + "; ".join(_street_bits)
                    )
            if _lines:
                opponent_profiles = (
                    f"Opponent behavior observed against {_source_bot} "
                    f"(adequate frozen H2H extremes first; sparse rows are advisory):\n"
                    + "\n".join(_lines)
                )
    except Exception:
        pass

    _system_bootstrap_master = False
    _system_bootstrap_master_receipt = None
    from system_strict_bootstrap import is_declared_native_bootstrap

    if is_declared_native_bootstrap(_master_entry_ckpt):
        from system_strict_bootstrap import (
            validate_bootstrap_checkpoint,
        )

        _system_errors = validate_bootstrap_checkpoint(
            _master_entry_ckpt,
            architecture_policy=architecture_policy,
            candidate_dir=_tp.get_bot_dir(next_v),
            require_direction_audit=True,
        )
        if _system_errors:
            return await _tp._abandon_master_generation(
                next_v,
                source_v,
                error="SYSTEM_STRICT_BOOTSTRAP_AUTHORITY_INVALID",
                fail_count=0,
                reason=(
                    "system_strict_bootstrap_authority_invalid:"
                    + ";".join(_system_errors[:8])
                ),
                event_type="pipeline.system_strict_bootstrap_invalid",
                event_message=(
                    f"System strict bootstrap authority drifted for v{next_v}; "
                    "abandoning without falling back to an LLM"
                ),
                ui=ui,
                payload={"validation_errors": _system_errors},
                directive=(
                    "The exact first-migration receipt, prepared artifact, checked-in "
                    "blueprint, or strict-pool state changed. The generation was "
                    "abandoned; repair the control-plane contract and re-prepare."
                ),
            )
        _system_bootstrap_master = True
        _tp.log_system_event(
            "pipeline.system_strict_bootstrap_authority_verified",
            "info",
            f"Verified deterministic Worker authority before Master for v{next_v}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "executor": "system_policy_bootstrap_v1",
                "master_governance": "three_proposals_two_anonymous_ballots",
            },
        )

    # The deterministic package owns implementation bytes only.  Governance is
    # unchanged: Master must still produce three independent proposals, collect
    # two anonymous criterion ballots, and publish one schema-valid plan.
    try:
        data = await _tp._run_master_analysis(
            source_v, next_v, stagnation_info, ui,
            match_analysis=match_analysis,
            performance_verification=performance_verification,
            replay_spotlight=replay_spotlight,
            bot_action_stats=bot_action_stats,
            opponent_profiles=opponent_profiles,
            research_proposals=research_proposals,
            architecture_policy=architecture_policy,
            prepared_baseline=prepared_baseline,
            **(
                {"protocol_bootstrap": protocol_bootstrap_receipt}
                if isinstance(protocol_bootstrap_receipt, dict)
                else {}
            ),
        )
    except LLMAvailabilityBlocked:
        # Availability pauses are attempt-neutral and must park at Direction.
        _tp._clear_master_runtime_heartbeat(next_v, source_v)
        raise
    except Exception as exc:
        from agent_master import (
            MasterAuthorityError,
            MasterEnsembleInfrastructureParked,
            MasterInfrastructureError,
        )
        from strict_authority_workflow import StrictAuthorityError

        if isinstance(exc, StrictAuthorityError):
            return await _tp._abandon_strict_master_authority(
                next_v,
                source_v,
                error=exc,
                ui=ui,
            )
        if isinstance(exc, MasterAuthorityError):
            return await _tp._block_master_authority(
                next_v,
                source_v,
                error=exc,
                ui=ui,
            )
        if isinstance(exc, MasterEnsembleInfrastructureParked):
            return _tp._handle_master_ensemble_provider_parked(
                next_v,
                source_v,
                ui,
                exc,
            )
        if not isinstance(exc, MasterInfrastructureError):
            raise
        return await _tp._handle_master_llm_infrastructure(
            next_v,
            source_v,
            ui,
            component="master_llm",
            issue=exc.issue,
            prompt_digest=exc.prompt_digest,
        )

    if data is None:
        return await _tp._handle_master_analysis_failure(
            next_v,
            source_v,
            ui,
            message="Master failed to produce a valid plan after retries or LLM failure",
            reason=f"master_analysis_failed v{next_v}",
        )
    if architecture_policy is not None:
        data["architecture_policy"] = architecture_policy

    async def _compile_and_hard_validate_master_plan(plan, *, phase: str):
        """Normalize, compile, and hard-validate a Master plan before LLM audit."""
        plan = _tp._normalize_and_log_master_plan_paths(plan, source_v, next_v)
        compiler_errors = []
        try:
            from plan_compiler import compile_master_plan
            plan, _compile_meta = compile_master_plan(
                plan,
                next_v=next_v,
                target_dir=_tp.get_bot_dir(next_v),
                project_root=PROJECT_ROOT,
            )
            if _compile_meta.get("compiled"):
                _tp.log_system_event(
                    "pipeline.master_plan_compaction_forbidden",
                    "error",
                    f"Master plan v{next_v}: rejected unexpected Worker-prompt compaction",
                    {"next_v": next_v, "source_v": source_v, "phase": phase, "compiler": _compile_meta},
                )
                # A Master plan accepted through the selected-proposal binding
                # must remain losslessly inline.  Replacing it with a generated
                # task brief changes the authority/replay shape; reject it here
                # before a checkpoint, Worker lease, or bootstrap receipt.
                compiler_errors.append(
                    "master_plan_worker_prompt_externalization_forbidden"
                )
            _contract_binding = _compile_meta.get("contract_binding") or {}
            if any(_contract_binding.get(key) for key in (
                "invalid_contract_tasks",
                "invalid_prompt_tasks",
                "overflow_tasks",
            )):
                compiler_errors.append(
                    "master_plan_system_contract_binding_invalid:"
                    + json.dumps(
                        _contract_binding,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )[:2000]
                )
        except Exception as _compile_exc:
            compiler_errors.append(
                "master_plan_compile_failed:"
                f"{type(_compile_exc).__name__}:{str(_compile_exc)[:500]}"
            )
            _tp.log_system_event(
                "pipeline.master_plan_compile_failed",
                "error",
                f"Master plan compiler failed for v{next_v}: {_compile_exc}",
                {"next_v": next_v, "source_v": source_v, "phase": phase, "error": str(_compile_exc)[:500]},
            )

        plan_errors, plan_warnings = _tp._validate_master_plan(plan, next_v=next_v)
        plan_errors = list(dict.fromkeys([*compiler_errors, *plan_errors]))
        if plan_warnings:
            try:
                _tp.log_system_event(
                    "pipeline.master_boundary",
                    "warning",
                    f"Master plan boundary warnings for v{next_v}: {plan_warnings}",
                    {"next_v": next_v, "source_v": source_v, "phase": phase, "warnings": plan_warnings},
                )
            except Exception:
                pass
        if not plan_errors:
            from runtime_architecture_policy import attach_runtime_contract_ledger

            return attach_runtime_contract_ledger(plan, replace=True), None

        _validation_ctx = {
            "master_validation": {
                "phase": phase,
                "errors": plan_errors,
                "warnings": plan_warnings,
                "plan_analysis": plan.get("analysis", "")[:1000]
                if isinstance(plan, dict) else "",
            }
        }
        _nf = _tp._bump_master_fail_count(
            next_v,
            source_v,
            audit_context=_validation_ctx,
        )
        _severity = "error" if _nf >= _tp.MAX_MASTER_TOTAL_FAILURES else "warn"
        try:
            _tp.log_system_event(
                "pipeline.master_validation_failed",
                _severity,
                f"Master plan validation failed before audit for v{next_v} "
                f"(fail_count={_nf}): {'; '.join(plan_errors[:3])}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "phase": phase,
                    "fail_count": _nf,
                    "validation_errors": plan_errors,
                    "validation_warnings": plan_warnings,
                },
            )
        except Exception:
            pass
        try:
            ui.log_history(
                "Master plan validation failed before audit: " + "; ".join(plan_errors[:5]),
                "error",
            )
        except Exception:
            pass

        if _nf >= _tp.MAX_MASTER_TOTAL_FAILURES:
            return plan, await _tp._abandon_master_generation(
                next_v,
                source_v,
                error="MASTER_VALIDATION_EXHAUSTED",
                fail_count=_nf,
                reason=(
                    f"master_validation_failed v{next_v}: "
                    f"{'; '.join(plan_errors[:3])[:300]}"
                ).rstrip(),
                event_type="pipeline.master_validation_exhausted_abandon",
                event_message=(
                    f"Master plan validation failed {_nf} times for v{next_v} — "
                    "abandoning invalid generation"
                ),
                ui=ui,
                payload={
                    "validation_errors": plan_errors,
                    "validation_warnings": plan_warnings,
                },
                directive=(
                    "Master plan validation failed too many times and this "
                    "generation was abandoned. Start a fresh generation; do "
                    "not execute workers from the invalid plan."
                ),
            )

        return plan, _json_tool_result({
            "error": "MASTER_VALIDATION_FAILED",
            "fail_count": _nf,
            "validation_errors": plan_errors,
            "validation_warnings": plan_warnings,
            "invalid_plan_preview": {
                "analysis": str(plan.get("analysis", ""))[:1000]
                if isinstance(plan, dict) else "",
                "tasks": [
                    {
                        "worker_id": task.get("worker_id"),
                        "role": task.get("role"),
                        "target_files": task.get("target_files", []),
                        "worker_prompt_chars": len(str(task.get("worker_prompt", ""))),
                    }
                    for task in (plan.get("tasks", []) if isinstance(plan, dict) else [])[:3]
                    if isinstance(task, dict)
                ],
            },
            "directive": (
                "The Master plan failed hard validation before LLM audit. "
                "Do NOT execute workers from this plan. If retrying Master, "
                "the next plan must explicitly fix these validation_errors; "
                "after repeated failures the generation will be abandoned."
            ),
            "logs": ui.get_output(),
        })

    data, _early_validation_result = await _compile_and_hard_validate_master_plan(
        data, phase="master_plan_ready"
    )
    if _early_validation_result is not None:
        return _early_validation_result
    if _system_bootstrap_master:
        from system_strict_bootstrap import (
            SystemStrictBootstrapError,
            build_master_receipt,
        )

        try:
            _system_bootstrap_master_receipt = build_master_receipt(
                _tp._matching_checkpoint(next_v, source_v) or _master_entry_ckpt,
                data,
                architecture_policy=architecture_policy,
                candidate_dir=_tp.get_bot_dir(next_v),
            )
        except SystemStrictBootstrapError as exc:
            return await _tp._abandon_master_generation(
                next_v,
                source_v,
                error="SYSTEM_STRICT_BOOTSTRAP_MASTER_RECEIPT_INVALID",
                fail_count=0,
                reason="system_strict_bootstrap_master_receipt_invalid:" + str(exc)[:300],
                event_type="pipeline.system_strict_bootstrap_receipt_invalid",
                event_message=(
                    f"System strict bootstrap receipt failed closed for v{next_v}"
                ),
                ui=ui,
                payload={"validation_errors": list(exc.errors)},
                directive=(
                    "The compiled plan or its authority chain drifted. The generation "
                    "was abandoned without invoking an LLM fallback."
                ),
            )
        except Exception as exc:
            return await _tp._abandon_master_generation(
                next_v,
                source_v,
                error="SYSTEM_STRICT_BOOTSTRAP_MASTER_RECEIPT_ERROR",
                fail_count=0,
                reason=(
                    "system_strict_bootstrap_master_receipt_error:"
                    f"{type(exc).__name__}:{str(exc)[:260]}"
                ),
                event_type="pipeline.system_strict_bootstrap_receipt_error",
                event_message=(
                    f"System strict bootstrap receipt errored for v{next_v}"
                ),
                ui=ui,
                payload={"exception_type": type(exc).__name__},
                directive=(
                    "The system receipt controller failed unexpectedly. The "
                    "generation was abandoned; never fall back to an LLM Worker."
                ),
            )
    _tp._touch_master_checkpoint(next_v, source_v, phase="master_plan_ready")

    # --- P0-1: Post-Master Plan Verification Audit ---
    # Capped retry loop: on audit rejection, re-plan AND re-audit only while the
    # unified Master budget still allows it. The audit_attempt counter is
    # persisted in the checkpoint so a crash-resume does not re-burn the budget.
    master_audit_ctx = None
    try:
        from evolution_infra import read_pipeline_checkpoint
        _ckpt0 = read_pipeline_checkpoint() or {}
        # `or 0` defends against a stored null: prepare_next_gen writes the
        # checkpoint with audit_attempt=None (default), and across the next_v
        # change the merge guard fails so it serializes as JSON null. A bare
        # .get("audit_attempt", 0) returns the stored None (not the default),
        # and int(None) raises TypeError that the surrounding try/except would
        # swallow — silently disabling the audit on every normal generation.
        _audit_attempt = int(_ckpt0.get("audit_attempt") or 0)

        for _audit_iter in range(_tp.MAX_MASTER_AUDIT_RETRIES + 1):
            _tp._touch_master_checkpoint(
                next_v,
                source_v,
                phase="master_plan_audit_start",
                audit_attempt=_audit_attempt,
            )
            if protocol_bootstrap_no_strength:
                _h2h_citation_errors = []
                _h2h_repair_guidance = ""
                audit_result = _tp._protocol_bootstrap_master_audit(data)
            else:
                try:
                    from evidence_snapshot import (
                        h2h_citation_repair_guidance,
                        statistical_evidence_floor_errors,
                        validate_h2h_citations_against_snapshot,
                    )
                    _h2h_citation_errors = validate_h2h_citations_against_snapshot(data, next_v)
                    # Two-tier statistical evidence bar (sufficiency), kept
                    # separate from citation accuracy above: 2026-08-16 audit
                    # found 12/12 selected plans acting on n=4-56 rows.
                    _h2h_citation_errors = (
                        statistical_evidence_floor_errors(data, next_v)
                        + _h2h_citation_errors
                    )
                    _h2h_repair_guidance = h2h_citation_repair_guidance(
                        next_v,
                        _h2h_citation_errors,
                        source_v=source_v,
                    )
                except Exception:
                    _h2h_citation_errors = []
                    _h2h_repair_guidance = ""
            if not protocol_bootstrap_no_strength and _h2h_citation_errors:
                audit_result = {
                    "plan_coherent": False,
                    "contradiction_found": True,
                    "contradictions": _h2h_citation_errors[:10],
                    "evidence_alignment": "misaligned",
                    "direction_novelty": "incremental",
                    "overall_pass": False,
                    "feedback": _tp._h2h_citation_audit_feedback(
                        next_v,
                        _h2h_citation_errors,
                        _h2h_repair_guidance,
                    ),
                    "retry_recommended": True,
                    "deterministic_h2h_snapshot_check": True,
                    "repair_guidance": _h2h_repair_guidance,
                }
            elif not protocol_bootstrap_no_strength:
                from audit_agents import _run_master_plan_audit

                try:
                    audit_result = await _run_master_plan_audit(data, source_v, ui, next_v=next_v)
                except TypeError as _audit_te:
                    if "next_v" not in str(_audit_te) and "keyword" not in str(_audit_te):
                        raise
                    audit_result = await _run_master_plan_audit(data, source_v, ui)
            master_audit_ctx = audit_result  # Save for audit_context chain
            if (
                not isinstance(audit_result, dict)
                or audit_result.get("llm_failed")
                or audit_result.get("parse_failed")
            ):
                issue = (
                    str((audit_result or {}).get("error") or "master plan audit output unavailable")
                    if isinstance(audit_result, dict)
                    else f"master_plan_audit_not_object:{type(audit_result).__name__}"
                )
                return await _tp._handle_master_llm_infrastructure(
                    next_v,
                    source_v,
                    ui,
                    component="master_plan_audit_llm",
                    issue=issue,
                    prompt_digest=hashlib.sha256(
                        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                )
            if audit_result.get("overall_pass", True):
                break  # plan passed audit
            # Rejected
            _tp.log_system_event("pipeline.master_audit_rejected", "warn",
                             f"Master plan audit rejected for v{next_v} (attempt {_audit_attempt + 1}): {audit_result.get('feedback', '')[:200]}",
                             {"next_v": next_v, "audit": audit_result, "audit_attempt": _audit_attempt + 1})
            if _audit_attempt + 1 > _tp.MAX_MASTER_AUDIT_RETRIES:
                _nf = _tp._bump_master_fail_count(next_v, source_v, value=_audit_attempt + 1)
                return await _tp._abandon_master_generation(
                    next_v,
                    source_v,
                    error="MASTER_AUDIT_REJECTED",
                    fail_count=_nf,
                    reason=f"master_audit_rejected v{next_v}: {audit_result.get('feedback', '')[:300]}",
                    event_type="pipeline.master_audit_exhausted_abandon",
                    event_message=(
                        f"Master audit exhausted {_tp.MAX_MASTER_AUDIT_RETRIES} retries "
                        f"for v{next_v} — blocking plan and abandoning"
                    ),
                    ui=ui,
                    payload={"audit": audit_result},
                    directive=(
                        "Master plan audit is blocking. This generation was abandoned "
                        "after the corrective re-plan budget was exhausted. Start a "
                        "fresh generation; do not execute workers from the rejected plan."
                    ),
                )
            # Re-plan with rejection feedback, then re-audit the new plan
            _audit_attempt += 1
            _rejection_ckpt = read_pipeline_checkpoint() or {}
            _rejection_written = _tp.write_pipeline_checkpoint(
                next_v,
                source_v,
                _rejection_ckpt.get("stage") or "direction_audited",
                audit_attempt=_audit_attempt,
                audit_context={"master_audit_rejection": master_audit_ctx},
                touch_stage_timestamp=True,
                expected_checkpoint_revision=int(
                    _rejection_ckpt.get("checkpoint_revision") or 0
                ),
                expected_checkpoint_stage=str(
                    _rejection_ckpt.get("stage") or "direction_audited"
                ),
                expected_workflow_run_id=str(
                    _rejection_ckpt.get("workflow_run_id") or ""
                ),
            )
            if not _rejection_written:
                return _state_blocked(
                    "master audit rejection checkpoint CAS was refused",
                    next_v,
                    source_v,
                    _tp._matching_checkpoint(next_v, source_v),
                )
            _tp._touch_master_checkpoint(
                next_v,
                source_v,
                phase="master_audit_rejected",
                audit_attempt=_audit_attempt,
                audit_context={"master_audit_rejection": master_audit_ctx},
            )
            _tp.log_system_event("pipeline.master_audit_blocked", "error",
                             f"Master plan audit blocked v{next_v}; retrying Master attempt {_audit_attempt}",
                             {"next_v": next_v, "source_v": source_v,
                              "audit_attempt": _audit_attempt, "audit": audit_result})
            performance_verification += (
                f"\n\n# PLAN AUDIT REJECTION (attempt {_audit_attempt})\n"
                f"The previous plan was rejected by the Plan Verification Auditor.\n"
                f"Issues: {audit_result.get('feedback', '')}\n"
                f"Contradictions: {', '.join(audit_result.get('contradictions', []))}\n"
                f"Direction assessment: {audit_result.get('direction_novelty', 'unknown')}\n"
                f"You MUST address these issues in your new plan.\n"
            )
            performance_verification, _retry_sanitized = _tp._sanitize_unverified_replay_citations(
                performance_verification, _anchor_map
            )
            if _retry_sanitized:
                try:
                    _tp.log_system_event(
                        "pipeline.master_retry_context_citations_sanitized",
                        "warn",
                        f"Master retry v{next_v} context had stale replay IDs redacted",
                        {
                            "next_v": next_v,
                            "source_v": source_v,
                            "count": _retry_sanitized,
                        },
                    )
                except Exception:
                    pass
            try:
                data = await _tp._run_master_analysis(
                    source_v, next_v, stagnation_info, ui,
                    match_analysis=match_analysis,
                    performance_verification=performance_verification,
                    replay_spotlight=replay_spotlight,
                    bot_action_stats=bot_action_stats,
                    opponent_profiles=opponent_profiles,
                    research_proposals=research_proposals,
                    architecture_policy=architecture_policy,
                    prepared_baseline=prepared_baseline,
                    **(
                        {"protocol_bootstrap": protocol_bootstrap_receipt}
                        if isinstance(protocol_bootstrap_receipt, dict)
                        else {}
                    ),
                )
            except LLMAvailabilityBlocked:
                _tp._clear_master_runtime_heartbeat(next_v, source_v)
                raise
            except Exception as exc:
                from agent_master import (
                    MasterAuthorityError,
                    MasterEnsembleInfrastructureParked,
                    MasterInfrastructureError,
                )
                from strict_authority_workflow import StrictAuthorityError

                if isinstance(exc, StrictAuthorityError):
                    return await _tp._abandon_strict_master_authority(
                        next_v,
                        source_v,
                        error=exc,
                        ui=ui,
                    )
                if isinstance(exc, MasterAuthorityError):
                    return await _tp._block_master_authority(
                        next_v,
                        source_v,
                        error=exc,
                        ui=ui,
                    )
                if isinstance(exc, MasterEnsembleInfrastructureParked):
                    return _tp._handle_master_ensemble_provider_parked(
                        next_v,
                        source_v,
                        ui,
                        exc,
                    )
                if not isinstance(exc, MasterInfrastructureError):
                    raise
                return await _tp._handle_master_llm_infrastructure(
                    next_v,
                    source_v,
                    ui,
                    component="master_llm",
                    issue=exc.issue,
                    prompt_digest=exc.prompt_digest,
                )
            if data is None:
                return await _tp._handle_master_analysis_failure(
                    next_v,
                    source_v,
                    ui,
                    message="Master failed after audit retry",
                    reason=f"master_analysis_failed_after_audit_retry v{next_v}",
                    payload={"audit_attempt": _audit_attempt},
                )
            if architecture_policy is not None:
                data["architecture_policy"] = architecture_policy
            data, _early_validation_result = await _compile_and_hard_validate_master_plan(
                data, phase="master_retry_plan_ready"
            )
            if _early_validation_result is not None:
                return _early_validation_result
            _tp._touch_master_checkpoint(
                next_v,
                source_v,
                phase="master_retry_plan_ready",
                audit_attempt=_audit_attempt,
            )
            _tp.log_system_event("pipeline.master_audit_retry", "info",
                             f"Master re-planned after audit rejection for v{next_v} (attempt {_audit_attempt})",
                             {"next_v": next_v})
    except LLMAvailabilityBlocked:
        _tp._clear_master_runtime_heartbeat(next_v, source_v)
        raise
    except Exception as e:
        _tp._log.warning("Master plan audit infrastructure error: %s", e)
        try:
            _tp.log_system_event('pipeline.master_audit_error', 'warn',
                f'Master plan audit error for v{next_v}: {e}',
                {"next_v": next_v, "source_v": source_v, "error": str(e)})
        except Exception:
            pass
        return await _tp._handle_master_llm_infrastructure(
            next_v,
            source_v,
            ui,
            component="master_plan_audit_llm",
            issue=f"{type(e).__name__}: {str(e)[:400]}",
            prompt_digest=hashlib.sha256(
                json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        )

    # A singleton no-strength successor now uses the same six-slot durable
    # Master journal as the fresh bootstrap. Before projecting master_planned,
    # re-open all three Scouts, both Ballots, final Master and all five bound
    # invocation logs, then replay the production compiler. A sealed final role
    # alone is insufficient authority if any sibling receipt/log was lost or
    # changed across a crash.
    if protocol_bootstrap_no_strength and not _system_bootstrap_master:
        from agent_master import MasterAuthorityError
        from strict_authority_workflow import (
            StrictAuthorityError,
            validate_master_final_projection,
        )

        _projection_checkpoint = (
            _tp._matching_checkpoint(next_v, source_v) or _master_entry_ckpt
        )
        try:
            _projection_proof, _projection_errors = (
                validate_master_final_projection(
                    _projection_checkpoint,
                    data,
                    candidate_dir=_tp.get_bot_dir(next_v),
                    project_root=PROJECT_ROOT,
                    require_no_other_accepted=True,
                )
            )
        except StrictAuthorityError as exc:
            _projection_errors = list(exc.errors)
        except Exception as exc:
            _projection_errors = [
                "singleton_master_projection_unavailable:"
                f"{type(exc).__name__}:{str(exc)[:240]}"
            ]
        if _projection_errors:
            return await _tp._block_master_authority(
                next_v,
                source_v,
                error=MasterAuthorityError(
                    source_v,
                    next_v,
                    hashlib.sha256(
                        json.dumps(
                            {
                                "plan": data,
                                "errors": _projection_errors,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    _projection_errors,
                ),
                ui=ui,
            )

    # Persist master plan to checkpoint so it survives crashes between master and workers
    _ckpt = _tp._matching_checkpoint(next_v, source_v)
    existing_audit = _ckpt.get("direction_audit") if _ckpt else direction_audit
    # Mark direction_audit as resolved now that Master has produced a plan
    if existing_audit and existing_audit.get("repetition_detected"):
        existing_audit["resolved"] = True
    checkpoint_kwargs = {}
    current_master_infra = (_ckpt or {}).get("infra_failure")
    if isinstance(current_master_infra, dict):
        from pipeline_infrastructure import infrastructure_failure_digest

        checkpoint_kwargs = {
            "clear_infra_failure": True,
            "infra_failure_owner": "run_master",
            "expected_infra_failure_digest": infrastructure_failure_digest(current_master_infra),
        }
    recorded = _tp.write_pipeline_checkpoint(
        next_v,
        source_v,
        "master_planned",
        master_plan=data,
        direction_audit=existing_audit,
        worker_failure_count=_ckpt.get("worker_failure_count", 0) if _ckpt else 0,
        audit_context={
            **({"master_audit": master_audit_ctx} if master_audit_ctx else {}),
            **(
                {"system_strict_bootstrap": _system_bootstrap_master_receipt}
                if _system_bootstrap_master_receipt is not None
                else {}
            ),
        } or None,
        reset_generation_attempt=True,
        reset_audit_attempt=True,
        **checkpoint_kwargs,
    )
    if not recorded:
        return _state_blocked(
            "Master plan passed but checkpoint publication was rejected",
            next_v,
            source_v,
            _tp._matching_checkpoint(next_v, source_v),
        )

    try:
        _tp.log_system_event("pipeline.master_done", "info", f"Master planned v{next_v}: {len(data.get('tasks', []))} tasks",
                         {"next_v": next_v, "source_v": source_v, "num_tasks": len(data.get("tasks", [])),
                          "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    result = {
        "plan": data,
        "logs": ui.get_output(),
        **(
            {
                "implementation_executor": "system_policy_bootstrap_v1",
                "master_ensemble_invoked": True,
            }
            if _system_bootstrap_master
            else {}
        ),
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
