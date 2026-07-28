"""Canonical-abandon proof + tool-result decoding + LLM-availability pause +
generation cost-policy helpers + log rotation + provider prompt rendering.

Extracted from orchestrator.py as a single business responsibility group: the
self-contained helper layer that the main ``_run_one_cycle`` /
``orchestrator_loop`` bodies call as bare globals.

Members moved here (all re-exported by orchestrator.py):

* Abandon-proof: ``_canonical_abandon_proof_identity``,
  ``_remember_verified_canonical_abandon``, ``_remembered_canonical_abandon_proof``.
* Tool-result decoding: ``_tool_result_payload``,
  ``_completed_abandon_tool_result``, ``_raise_for_llm_availability_tool_result``.
* LLM-availability pause: ``_honor_active_llm_pause``,
  ``_is_cycle_infra_error``.
* Log rotation / provider prompt: ``_rotate_orchestrator_logs``.
  (``_render_orchestrator_provider_prompt`` stays in ``orchestrator.py`` — the
  LLM role-contract registry binds the orchestrator producer to
  ``producer_file='web/core/orchestrator.py'`` via ``inspect.getsourcefile``.)
* Generation cost-policy: ``_bind_generation_cost_runtime``,
  ``_check_generation_cost_policy``, ``_project_generation_cost_runtime``.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by these bodies that live in ``orchestrator`` are written
as ``_o.<name>`` so they resolve against the live ``orchestrator`` module
attribute, matching the pattern proven by ``orchestrator_branch_guard`` /
``orchestrator_post_generation``.  This is *required* for every name the test
suite monkeypatches on ``orchestrator``:

* ``log`` (module logger), ``log_system_event``.
* cost-policy functions: ``load_operator_generation_cost_policy``,
  ``configure_runtime_cost_policy``, ``generation_cost_status``.
* intra-cluster helpers: ``_tool_result_payload``,
  ``_raise_for_llm_availability_tool_result``,
  ``_project_generation_cost_runtime`` (all re-exported and therefore
  monkeypatchable).
* the ``_LLM_AVAILABILITY_CONTROL_ERRORS`` constant set.

``active_llm_pause`` / ``blocked_from_pause_state`` / ``pause_wait_seconds``
and the LLM-availability exception types are imported directly from
``llm_availability`` / ``llm_availability_store`` (stable imports, not
monkeypatched on ``orchestrator``).  The non-monkeypatched
``orchestrator_cost_policy`` symbols (``activate_generation_cost_scope``,
``generation_identity``, ``claim_generation_cost_notice``,
``current_generation_cost_scope``,
``assert_operator_cost_limit_available``,
``OperatorGenerationCostLimitExceeded``, ``GenerationCostPolicy``) are imported
directly from ``orchestrator_cost_policy``.
"""

from __future__ import annotations

import asyncio
import json

import orchestrator as _o
from llm_availability import LLMAvailabilityBlocked
from llm_availability_store import (
    LLMAvailabilityPauseError,
    active_llm_pause,
    blocked_from_pause_state,
    pause_wait_seconds,
)
from llm_failure import is_llm_infra_error, is_shutdown_cancel_error as _is_shutdown_cancel_error
from orchestrator_cost_policy import (
    GenerationCostPolicy,
    OperatorGenerationCostLimitExceeded,
    activate_generation_cost_scope,
    assert_operator_cost_limit_available,
    claim_generation_cost_notice,
    current_generation_cost_scope,
    generation_identity,
)


def _canonical_abandon_proof_identity(proof):
    """Return the exact identity carried by a previously re-proven abandon.

    A bare ``ORCH_GENERATION_ABANDONED_COST`` is deliberately not authority to
    prepare another workflow.  The producer path must have run
    ``validate_completed_abandon_handoff`` and carry its compact proof forward
    to the outer-loop handoff.  Keep the structural check local as a second
    boundary before a scheduler decision or a UI event consumes the proof.
    """

    if not isinstance(proof, dict):
        return None

    def digest(value):
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        )

    identity = proof.get("checkpoint_identity")
    if (
        not digest(proof.get("transaction_id"))
        or not digest(proof.get("abandon_receipt_digest"))
        or not digest(proof.get("finalize_receipt_digest"))
        or not isinstance(identity, dict)
        or not digest(identity.get("digest"))
        or not isinstance(identity.get("workflow_run_id"), str)
        or not identity["workflow_run_id"].strip()
        or type(identity.get("next_v")) is not int
        or type(identity.get("source_v")) is not int
        or type(identity.get("checkpoint_revision")) is not int
        or identity["checkpoint_revision"] < 1
        or not isinstance(identity.get("stage"), str)
        or not identity["stage"].strip()
    ):
        return None
    return dict(identity)


def _remember_verified_canonical_abandon(gen_ctx, proof) -> bool:
    """Bind an already-validated terminal proof to this in-memory cycle only."""

    identity = _o._canonical_abandon_proof_identity(proof)
    if identity is None or gen_ctx is None:
        return False
    if (
        getattr(gen_ctx, "next_v", None) != identity["next_v"]
        or getattr(gen_ctx, "source_v", None) != identity["source_v"]
    ):
        return False
    # Serialize before retaining it so an SDK/result object cannot mutate the
    # event proof after the strict handoff validator returned.
    try:
        retained = json.loads(json.dumps(proof, sort_keys=True))
    except (TypeError, ValueError):
        return False
    setattr(gen_ctx, "_verified_canonical_abandon_proof", retained)
    return True


def _remembered_canonical_abandon_proof(gen_ctx):
    """Read only a complete proof remembered by this exact generation cycle."""

    proof = getattr(gen_ctx, "_verified_canonical_abandon_proof", None)
    return proof if _o._canonical_abandon_proof_identity(proof) is not None else None


def _tool_result_payload(value):
    """Decode the SDK's string-or-content-block ToolResult shape."""

    if isinstance(value, dict):
        if "error" in value or "action" in value or "success" in value:
            return value
        for key in ("text", "content"):
            if key in value:
                decoded = _o._tool_result_payload(value.get(key))
                if decoded:
                    return decoded
        return {}
    if isinstance(value, list):
        for item in value:
            decoded = _o._tool_result_payload(item)
            if decoded:
                return decoded
        return {}
    if isinstance(value, str):
        try:
            return _o._tool_result_payload(json.loads(value))
        except (TypeError, json.JSONDecodeError):
            return {}
    return {}


def _completed_abandon_tool_result(value):
    """Find one exact canonical-abandon result inside an SDK tool result."""

    required = {
        "abandoned",
        "cleared_checkpoint",
        "workflow_run_id",
        "abandon_transaction_id",
        "abandon_receipt_digest",
        "finalize_receipt_digest",
        "abandon_checkpoint_identity",
    }
    matches = []

    def collect(candidate):
        if isinstance(candidate, dict):
            if required.issubset(candidate):
                matches.append(candidate)
            for key in ("abandon_result", "result", "content", "text"):
                if key in candidate:
                    collect(candidate.get(key))
            return
        if isinstance(candidate, list):
            for item in candidate:
                collect(item)
            return
        if isinstance(candidate, str):
            try:
                collect(json.loads(candidate))
            except (TypeError, json.JSONDecodeError):
                pass

    collect(value)
    return matches[0] if len(matches) == 1 else None


def _raise_for_llm_availability_tool_result(content) -> None:
    """Turn a durable Worker pause result back into local stream control."""

    payload = _o._tool_result_payload(content)
    error = str(payload.get("error") or "")
    if error == "LLM_AVAILABILITY_BLOCKED":
        state = payload.get("availability") or active_llm_pause()
        if isinstance(state, dict) and state.get("active"):
            raise blocked_from_pause_state(state, role="Orchestrator")
        raise LLMAvailabilityPauseError(
            "Worker reported LLM availability blocked without a valid durable pause"
        )
    if error in _o._LLM_AVAILABILITY_CONTROL_ERRORS:
        raise LLMAvailabilityPauseError(
            f"Worker LLM availability control failed closed: {error}"
        )


async def _honor_active_llm_pause(ui=None, shutdown_mgr=None) -> bool:
    """Return whether an LLM call may proceed under the durable pause policy.

    Deterministic checkpoint routes call this only after they have had a chance
    to advance.  A manual billing/auth pause stops the orchestrator without a
    retry loop.  Transient availability records wait only until their bounded
    system-owned cooldown and then reconcile themselves.
    """

    state = active_llm_pause()
    if not state:
        return True
    category = str(state.get("category") or "unknown")
    evidence_digest = str(state.get("evidence_digest") or "")
    wait = pause_wait_seconds(state)
    if wait is None:
        msg = (
            f"LLM availability is manually paused ({category}); checkpoint and "
            "Worker attempt are preserved. Restart with "
            f"POK_LLM_RESUME_EVIDENCE_DIGEST={evidence_digest} only after the "
            "provider account/credential condition is resolved."
        )
        if ui:
            ui.log_history(msg, "error")
            ui.set_status(f"Stopped: LLM unavailable ({category})", is_working=False)
        _o.log.error(msg)
        try:
            _o.log_system_event(
                "orchestrator.llm_availability_paused",
                "error",
                msg,
                {
                    "category": category,
                    "evidence_digest": evidence_digest,
                    "retry_policy": state.get("retry_policy"),
                    "operator_action_required": True,
                },
            )
        except Exception:
            pass
        return False

    wait = max(0.0, float(wait))
    if wait > 0:
        msg = (
            f"LLM availability cooldown active ({category}); retrying after "
            f"{wait:.0f}s without consuming a generation/Worker attempt."
        )
        if ui:
            ui.log_history(msg, "warn")
            ui.set_status(f"LLM cooldown ({category})", is_working=False)
        _o.log.warning(msg)
        try:
            _o.log_system_event(
                "orchestrator.llm_availability_cooldown",
                "warn",
                msg,
                {
                    "category": category,
                    "evidence_digest": evidence_digest,
                    "wait_seconds": round(wait, 3),
                    "operator_action_required": False,
                },
            )
        except Exception:
            pass
        if shutdown_mgr:
            try:
                await asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=wait)
                return False
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(wait)
    return active_llm_pause() is None


def _is_cycle_infra_error(e, *, is_shutting_down: bool = False) -> bool:
    """Classify an orchestrator exception as LLM-infra (short -0.5 backoff) vs real
    business/auth failure. Type-based via is_llm_infra_error + keyword fallback for
    SDK ProcessError/exit-143 wrapping and signature field errors."""
    if is_shutting_down and _is_shutdown_cancel_error(e):
        return False
    err_str = str(e).lower()
    return (is_llm_infra_error(e)
            or "signature" in err_str
            or "missing required field" in err_str
            or "exit code 143" in err_str
            or "command failed with exit code" in err_str
            or "processerror" in err_str
            or "claude code returned an error result" in err_str)  # root-cause-audit 2026-06-21: SDK query.py:852 裸 Exception


def _rotate_orchestrator_logs(logs_dir, keep=20):
    """Keep only the most recent N orchestrator log files."""
    if not logs_dir.exists():
        return
    files = sorted(
        (
            file
            for file in logs_dir.iterdir()
            if file.name.startswith("orchestrator_")
            and file.name.endswith(".txt")
        ),
        key=lambda file: file.stat().st_mtime,
    )
    for old_file in files[:-keep]:
        try:
            old_file.unlink()
        except OSError:
            pass


def _bind_generation_cost_runtime(
    checkpoint,
    *,
    gen_ctx=None,
    ui=None,
    policy: GenerationCostPolicy | None = None,
):
    """Bind durable cost accounting to the system-owned workflow identity."""

    selected_policy = policy or _o.load_operator_generation_cost_policy()
    _o.configure_runtime_cost_policy(selected_policy)
    scope = activate_generation_cost_scope(
        generation_identity(checkpoint, gen_ctx),
        selected_policy,
    )
    status = _o.generation_cost_status(scope)
    receipt = scope.receipt(
        spent_before_usd=float(status.get("spent_usd") or 0.0),
        ledger_errors=tuple(status.get("accounting_errors") or ()),
    )
    begin_cost = getattr(ui, "begin_generation_cost", None) if ui else None
    if callable(begin_cost):
        begin_cost(scope.generation_id, status.get("spent_usd", 0.0), receipt)
    if claim_generation_cost_notice(scope, "policy_bound"):
        severity = "info" if status.get("accounting_ok") else "warn"
        _o.log_system_event(
            "pipeline.generation_cost_policy_bound",
            severity,
            (
                f"Generation cost policy bound for {scope.generation_id}: "
                f"{selected_policy.enforcement_mode}"
            ),
            {
                **receipt,
                "accounting_ok": status.get("accounting_ok"),
                "spent_usd": status.get("spent_usd"),
            },
        )
    return scope


def _check_generation_cost_policy(ui=None):
    """Warn in default mode; stop only for the explicit operator hard limit."""

    scope = current_generation_cost_scope()
    if scope is None:
        return {"active": False}
    status = _o.generation_cost_status(scope)
    if status.get("warning_reached") and claim_generation_cost_notice(scope, "warning"):
        spent = float(status.get("spent_usd") or 0.0)
        warning = float(status.get("warning_usd") or 0.0)
        msg = (
            f"Generation {scope.generation_id} LLM spend reached ${spent:.2f} "
            f"(monitoring threshold ${warning:.2f}); evolution continues."
        )
        _o.log.warning(msg)
        if ui:
            ui.log_history(f"[Orchestrator] {msg}", "warn")
        _o.log_system_event(
            "pipeline.generation_cost_warning",
            "warn",
            msg,
            {
                **status,
                "directive": "Telemetry only; no generation stop was requested by the operator.",
            },
        )
    if (
        not status.get("accounting_ok")
        and scope.policy.hard_limit_usd is None
        and claim_generation_cost_notice(scope, "accounting_warning")
    ):
        errors = [str(item) for item in status.get("accounting_errors") or ()]
        msg = (
            f"Generation {scope.generation_id} cost accounting is incomplete/unknown; "
            "monitor-only evolution continues."
        )
        _o.log.warning("%s errors=%s", msg, errors[:5])
        if ui:
            ui.log_history(f"[Orchestrator] {msg}", "warn")
        binding = scope.receipt(
            spent_before_usd=float(status.get("spent_usd") or 0.0),
            ledger_errors=tuple(errors),
        )
        _o.log_system_event(
            "pipeline.generation_cost_accounting_warning",
            "warn",
            msg,
            {
                **status,
                "policy_binding": binding,
                "directive": (
                    "Telemetry is incomplete; monitor-only mode continues. "
                    "Inspect ledger_errors before interpreting the displayed USD total."
                ),
            },
        )
        # update_cost($0) cannot communicate an unknown USD amount.  Push the
        # bound receipt so SSE/state consumers see ledger_errors immediately.
        _o._project_generation_cost_runtime(ui)
    try:
        return assert_operator_cost_limit_available(scope)
    except OperatorGenerationCostLimitExceeded as exc:
        status = dict(exc.status or status)
        if claim_generation_cost_notice(scope, "operator_hard_limit_tripped"):
            msg = f"Operator generation cost limit stopped {scope.generation_id}: {exc}"
            _o.log.error(msg)
            if ui:
                ui.log_history(f"[Orchestrator] {msg}", "error")
            _o.log_system_event(
                "pipeline.operator_generation_cost_limit_tripped",
                "error",
                msg,
                {
                    **status,
                    "policy_binding": scope.receipt(
                        spent_before_usd=float(status.get("spent_usd") or 0.0),
                        ledger_errors=tuple(status.get("accounting_errors") or ()),
                    ),
                    "operator_action_required": True,
                    "directive": (
                        "Change or disable the parent-process operator limit, then explicitly restart. "
                        "The checkpoint is preserved."
                    ),
                },
            )
        raise


def _project_generation_cost_runtime(ui=None) -> dict:
    """Refresh dashboard state from the durable ledger without rebinding."""

    scope = current_generation_cost_scope()
    status = _o.generation_cost_status(scope)
    begin_cost = getattr(ui, "begin_generation_cost", None) if ui else None
    if scope is not None and callable(begin_cost):
        begin_cost(
            scope.generation_id,
            status.get("spent_usd", 0.0),
            scope.receipt(
                spent_before_usd=float(status.get("spent_usd") or 0.0),
                ledger_errors=tuple(status.get("accounting_errors") or ()),
            ),
        )
    return status
