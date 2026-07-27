"""First-strict control-execution scope + precommit shutdown-token subsystem for tool_eval.

Extracted as a cohesive business cluster; ``tool_eval.py`` retains thin
delegate shells so external ``from tool_eval import <name>`` and
``monkeypatch.setattr(tool_eval, "<name>", ...)`` keep resolving.

Business responsibility (single cohesive domain):
* Precommit shutdown-token management (``set_precommit_shutdown`` /
  ``reset_precommit_shutdown`` / ``current_precommit_shutdown_token`` /
  ``begin_precommit_shutdown_attempt`` / ``is_precommit_shutdown``) and the
  ``_PRECOMMIT_SHUTDOWN_LOCK`` / ``_PRECOMMIT_SHUTDOWN`` globals.
* First-strict abandon helper (``_abandon_first_strict_generation``).
* Official precommit token / status plumbing (``_official_bot_token``,
  ``_request_official_precommit_status``).
* National precommit shape / sample contract (``_national_sample_contract_blockers``,
  ``_national_precommit_shape``, ``_observed_native_sample_plan``).
* First-strict control execution scope build / validate
  (``_build_first_strict_control_execution_scope``,
  ``_validate_first_strict_control_execution_scope``).
* First-strict live-lease / batch progress pending results
  (``_first_strict_live_lease_pending_result``,
  ``_control_execution_pending_from_native_result``,
  ``_validate_first_strict_batch_progress``,
  ``_persist_first_strict_batch_progress``,
  ``_first_strict_batch_pending_result``).

Cross-references to symbols that remain in ``tool_eval`` (the
``_matching_checkpoint`` / ``_json_tool_result`` / ``_state_blocked`` /
``make_intent`` / ``write_pipeline_checkpoint`` / ``get_active_bots``
helpers and the ``_FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY`` /
``_FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY`` /
``PRECOMMIT_MIN_N_GAMES`` / ``PRECOMMIT_MAX_N_GAMES`` constants) are
reached through ``_te.<name>`` so that test monkeypatches on
``tool_eval.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_te.<name>(...)`` so monkeypatches on ``tool_eval.<name>``
propagate even when both call sites now live in this companion.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import tool_eval as _te  # for cross-refs


# H1 (2026-06-29): per-attempt shutdown token for precommit cancellation.
# Blocking match work cannot always be force-cancelled.  Each precommit call
# therefore captures the current Event and passes that exact object into the
# native match loop.  A timeout sets the captured Event permanently; the next
# cycle rotates to a new Event instead of clearing the old one, so starting a
# retry can never revive a detached attempt from the previous cycle.
_PRECOMMIT_SHUTDOWN_LOCK = threading.Lock()
_PRECOMMIT_SHUTDOWN = threading.Event()


def set_precommit_shutdown(token: threading.Event | None = None):
    """Permanently cancel every precommit holding the current attempt token.

    Called by the orchestrator's CYCLE_TIMEOUT / CancelledError handler so
    subprocess-spawning loops stop before starting another complete match.
    Idempotent; ``reset_precommit_shutdown`` rotates rather than clears it.
    """
    if token is None:
        with _PRECOMMIT_SHUTDOWN_LOCK:
            token = _PRECOMMIT_SHUTDOWN
    token.set()


def reset_precommit_shutdown():
    """Rotate a cancelled token; never detach a still-live current attempt."""

    global _PRECOMMIT_SHUTDOWN
    with _PRECOMMIT_SHUTDOWN_LOCK:
        if _PRECOMMIT_SHUTDOWN.is_set():
            _PRECOMMIT_SHUTDOWN = threading.Event()


def current_precommit_shutdown_token() -> threading.Event:
    """Return the immutable-by-convention cancellation token for one attempt."""

    with _PRECOMMIT_SHUTDOWN_LOCK:
        return _PRECOMMIT_SHUTDOWN


def begin_precommit_shutdown_attempt() -> threading.Event:
    """Claim a live token, rotating only after the prior attempt was cancelled.

    Deterministic ``infra_timed_out`` recovery can call ``run_precommit_eval``
    before another provider cycle reaches the orchestrator's reset hook.  The
    handler therefore performs this atomic cancelled-to-fresh handoff itself.
    Concurrent calls while the token is live share it, so one timeout still
    fences every duplicate attempt owned by that cycle.
    """

    global _PRECOMMIT_SHUTDOWN
    with _PRECOMMIT_SHUTDOWN_LOCK:
        if _PRECOMMIT_SHUTDOWN.is_set():
            _PRECOMMIT_SHUTDOWN = threading.Event()
        return _PRECOMMIT_SHUTDOWN


async def _abandon_first_strict_generation(payload: dict, *, reason: str):
    """Fence and remove a rejected deterministic first-migration candidate.

    Returning an ``action`` string is not an execution boundary.  This helper
    invokes the existing actor-fenced abandon implementation before the tool
    returns, while retaining a deterministic pause/abandon route if cleanup is
    temporarily rate-limited or otherwise cannot complete.
    """

    try:
        from system_strict_bootstrap import abandon_rejected_blueprint

        checkpoint = _te._matching_checkpoint(
            payload.get("version"),
            payload.get("source_v"),
        )
        result = await abandon_rejected_blueprint(
            checkpoint,
            reason=str(reason),
            result=dict(payload),
        )
    except Exception as exc:
        result = {
            **dict(payload),
            "action": "abandon_generation",
            "abandoned": False,
            "abandon_error": (
                f"abandon_exception:{type(exc).__name__}:{str(exc)[:300]}"
            ),
        }
    result["abandon_reason"] = str(reason)
    result["intent"] = _te.make_intent(
        "abandoned" if result["abandoned"] else "abandon",
        next_tool=(
            None if result["abandoned"] else "abandon_generation"
        ),
        failure_class=str(result.get("failure_class") or "control_plane"),
        authority="tool:precommit_eval",
        safe_to_auto_execute=not result["abandoned"],
        reason=str(reason),
    )
    return _te._json_tool_result(result)


def is_precommit_shutdown() -> bool:
    """True if the current precommit attempt has been signalled to abort."""

    return _te.current_precommit_shutdown_token().is_set()



def _official_bot_token(value) -> str:
    path = Path(value)
    if path.name == "national_bot.py":
        return str(path.parent)
    return str(path)


def _request_official_precommit_status(
    *,
    candidate,
    self_play_rounds: int,
    opponent_rounds: int,
    target_hands: int,
) -> dict:
    """Queue compliance evidence without bypassing opponent eligibility."""
    from official_certification import (
        STATUS_INCONCLUSIVE,
        STATUS_PENDING,
        build_spec,
        official_compliance_verdict,
        select_official_opponent,
    )
    from official_certification_job import start_or_poll_job

    candidate_token = _te._official_bot_token(candidate)
    selection = None
    opponent = None
    if opponent_rounds > 0:
        selection = select_official_opponent(
            candidate_token,
            _te.get_active_bots(),
            preferred=os.environ.get("POK_OFFICIAL_OPPONENT", "").strip() or None,
            allow_bootstrap_grandfather=False,
        )
        if not selection.get("selected"):
            return {
                "status": STATUS_INCONCLUSIVE,
                "mode": "compliance",
                "passed": False,
                "blocking": False,
                "inconclusive": True,
                "classification": "inconclusive",
                "issues": ["official_precommit_no_eligible_opponent"],
                "opponent_selection": selection,
            }
        opponent = selection["opponent"]["path"]

    spec = build_spec(
        "compliance",
        candidate_token,
        opponent=opponent,
        self_play_rounds=self_play_rounds,
        opponent_rounds=opponent_rounds,
        target_hands=target_hands,
    )
    job = start_or_poll_job(spec, opponent_selection=selection)
    status = (
        job.get("status")
        if job.get("state") == "completed" and isinstance(job.get("status"), dict)
        else {
            "status": STATUS_PENDING,
            "mode": "compliance",
            "pending": bool(job.get("pending")),
            "queued": job.get("state") == "queued",
            "issues": list(job.get("issues") or []),
            "official_job": job,
            "summary": {
                "self_play_rounds": self_play_rounds,
                "opponent_rounds": opponent_rounds,
                "target_hands": target_hands,
            },
        }
    )
    verdict = official_compliance_verdict(status)
    return {
        **status,
        "blocking": False,
        "inconclusive": bool(verdict.get("inconclusive")),
        "classification": verdict.get("classification"),
        "opponent_selection": status.get("opponent_selection") or selection,
        "request_opponent_selection": selection,
        "official_job": job,
    }


def _national_sample_contract_blockers(
    paired_bootstrap: dict,
    *,
    expected_samples: int,
) -> list[dict]:
    sample_count = int(paired_bootstrap.get("net_chips_samples", 0) or 0)
    blockers: list[dict] = []
    if int(paired_bootstrap.get("hands_per_match", 0) or 0) != 70:
        blockers.append({
            "reason": "national_strength_hands_not_70",
            "details": "Every production national strength sample must be one complete 70-hand match.",
        })
    if sample_count > 0 and sample_count < expected_samples:
        blockers.append({
            "reason": "national_sample_shortfall",
            "details": (
                f"National precommit completed {sample_count}/{expected_samples} "
                "required full-match samples."
            ),
        })
    return blockers


def _national_precommit_shape(workflow_profile, sample_target: int) -> tuple[int, int]:
    if getattr(workflow_profile, "national_execution_mode", None) != "native_tcp":
        raise RuntimeError("precommit supports only national native_tcp evaluation")
    hands = 70
    configured_matches = int(os.environ.get(
        "POK_NATIONAL_PRECOMMIT_MATCHES",
        str(getattr(workflow_profile, "national_precommit_matches", 1)),
    ))
    return (
        max(1, min(70, hands)),
        max(
            2,
            configured_matches,
            min(_te.PRECOMMIT_MAX_N_GAMES, max(_te.PRECOMMIT_MIN_N_GAMES, sample_target)),
        ),
    )


def _observed_native_sample_plan(result: dict) -> list[dict]:
    rows: list[dict] = []
    for opponent_index, matchup in enumerate(result.get("matchups") or []):
        for repeat in matchup.get("repeats") or []:
            rows.append({
                "opponent": str(matchup.get("opponent") or ""),
                "opponent_index": opponent_index,
                "repeat": int(repeat.get("repeat") or 0),
                "deck_seed_base": repeat.get("deck_seed_base"),
                "bot_seed_base": repeat.get("bot_seed_base"),
                "native_match_timing_plan_digest": (
                    ((repeat.get("local_runtime_budget") or {}).get(
                        "timing_plan_digest"
                    ))
                ),
            })
    return rows


def _build_first_strict_control_execution_scope(
    *,
    v: int,
    candidate_name: str,
    code_fingerprint: str,
    opponents: list,
    precommit_plan: dict,
    evaluation_contract: dict,
    workflow_run_id: str,
    checkpoint_revision: int,
    precommit_attempt: int,
) -> dict:
    """Build the immutable identity for one first-strict match journal."""

    control = next(
        (
            item
            for item in opponents
            if str(item.get("authority") or "")
            == "system_first_strict_control"
        ),
        {},
    )
    control_receipt = control.get("control_receipt") or {}
    return {
        "workflow_run_id": str(workflow_run_id),
        "checkpoint_revision": int(checkpoint_revision),
        "candidate_version": int(v),
        "candidate_label": str(candidate_name),
        "candidate_artifact_hash": str(code_fingerprint),
        "control_id": str(control.get("name") or ""),
        "control_artifact_hash": str(
            ((control_receipt.get("control") or {}).get("artifact_hash"))
            or ""
        ),
        "control_receipt_digest": str(
            control_receipt.get("receipt_digest") or ""
        ),
        "precommit_plan_digest": str(
            precommit_plan.get("plan_digest") or ""
        ),
        "evaluation_contract_digest": str(
            evaluation_contract.get("contract_digest") or ""
        ),
        "native_match_timing_plan_digest": str(
            ((precommit_plan.get("settings") or {}).get(
                "native_match_timing_plan_digest"
            ))
            or ""
        ),
        "precommit_attempt": int(precommit_attempt),
    }


def _validate_first_strict_control_execution_scope(
    scope,
    *,
    v: int,
    candidate_name: str,
    code_fingerprint: str,
    opponents: list,
    precommit_plan: dict,
    evaluation_contract: dict,
    workflow_run_id: str,
    precommit_attempt: int,
) -> tuple[dict | None, str | None]:
    """Re-prove a frozen scope while retaining its attempt-origin revision."""

    try:
        from first_strict_execution_journal import normalize_execution_scope

        normalized = normalize_execution_scope(scope)
    except Exception as exc:
        return None, (
            "First-strict precommit execution scope is missing or invalid: "
            f"{type(exc).__name__}: {str(exc)[:240]}"
        )
    expected = _te._build_first_strict_control_execution_scope(
        v=v,
        candidate_name=candidate_name,
        code_fingerprint=code_fingerprint,
        opponents=opponents,
        precommit_plan=precommit_plan,
        evaluation_contract=evaluation_contract,
        workflow_run_id=workflow_run_id,
        checkpoint_revision=int(normalized["checkpoint_revision"]),
        precommit_attempt=precommit_attempt,
    )
    if normalized != expected:
        return None, (
            "First-strict precommit execution scope no longer binds the exact "
            "workflow, artifact, plan, control, and logical attempt."
        )
    return normalized, None


def _first_strict_live_lease_pending_result(
    *,
    v: int,
    source_v: int,
    candidate_name: str,
    precommit_attempt: int,
    control_execution_scope: dict,
    pending_execution: object,
    batch_progress: dict | None = None,
    batch_checkpoint_recorded: bool = False,
) -> dict:
    """Return a non-terminal recovery response for one active journal lease.

    A first-strict control match is physical evidence, not a pure function that
    may be retried in parallel.  The durable journal has already bound this
    scope to the quality/reviewer/critic chain.  While its matching effect is
    live, retain that chain and ask the operator/orchestrator to retry the
    exact precommit call only after the recorded lease ends or the original
    owner has written its completion receipt.
    """

    from first_strict_execution_journal import (
        FirstStrictExecutionJournalError,
        normalize_pending_control_execution,
        read_pending_control_execution,
    )

    normalized_scope = control_execution_scope
    try:
        pending = read_pending_control_execution(
            pending_execution,
            expected_scope=normalized_scope,
        )
        retryable_now = False
        pending_validation = "live_lease"
    except FirstStrictExecutionJournalError as exc:
        # A lease which expires between the native layer's observation and this
        # projection is safe to reclaim on the next exact invocation.  It is
        # still not a candidate/gate failure and must never enter abandon.
        if str(exc) != "first_strict_execution_pending_lease_not_live":
            raise RuntimeError(
                "first_strict_control_pending_journal_invalid:"
                f"{type(exc).__name__}:{str(exc)}"
            ) from exc
        pending = normalize_pending_control_execution(
            pending_execution,
            expected_scope=normalized_scope,
        )
        retryable_now = True
        pending_validation = "lease_expired_before_projection"

    checkpoint = _te._matching_checkpoint(v, source_v) or {}
    return _te._json_tool_result({
        "version": int(v),
        "source_v": int(source_v),
        "candidate": str(candidate_name),
        "passed": False,
        "pending": True,
        "failure_class": "infrastructure_pending",
        "checkpoint_recorded": False,
        "checkpoint_stage": checkpoint.get("stage"),
        "precommit_attempt": int(precommit_attempt),
        # The complete scope includes the control receipt digest and the
        # checkpoint revision which fenced the critic-approved artifact.
        "control_execution_scope": normalized_scope,
        "control_receipt_digest": normalized_scope.get(
            "control_receipt_digest"
        ),
        "preserved_gate_evidence": ["quality", "review", "critic"],
        "control_execution_pending": pending,
        "retry_not_before_epoch_s": (
            None if retryable_now else pending["lease_until"]
        ),
        "retryable_now": retryable_now,
        "directive": (
            "First-strict native precommit is already owned by a matching "
            "live journal lease. Preserve the frozen scope, control receipt, "
            "and quality/reviewer/critic evidence; do not abandon, rework, or "
            "launch a parallel match. Retry the exact precommit only after "
            "the lease expires or the durable receipt is recovered."
        ),
        "intent": _te.make_intent(
            "wait",
            next_tool="run_precommit_eval",
            failure_class="infrastructure_pending",
            authority="tool:precommit_eval",
            safe_to_auto_execute=False,
            reason=(
                "first_strict_execution_lease_expired_retry"
                if retryable_now
                else "first_strict_execution_lease_active"
            ),
        ),
        "pending_validation": pending_validation,
        "first_strict_batch_pending": batch_progress,
        "batch_checkpoint_recorded": bool(batch_checkpoint_recorded),
    })


def _control_execution_pending_from_native_result(
    national_result: object,
    *,
    control_execution_scope: dict | None,
) -> dict | None:
    """Return only the explicit live-lease signal; generic pending is unsafe.

    Batch continuation and other infrastructure paths may expose their own
    pending state.  This narrow adapter intentionally recognizes neither a
    generic ``pending`` flag nor arbitrary blocker text, because treating
    either as a live first-strict lease could suppress a genuine gate failure.
    """

    if not isinstance(national_result, dict):
        return None
    pending = national_result.get("control_execution_pending")
    if pending is None:
        return None
    if control_execution_scope is None:
        raise RuntimeError(
            "first_strict_control_pending_without_execution_scope"
        )
    from first_strict_execution_journal import normalize_pending_control_execution

    return normalize_pending_control_execution(
        pending,
        expected_scope=control_execution_scope,
    )


def _validate_first_strict_batch_progress(
    progress: object,
    *,
    precommit_plan: dict,
    control_execution_scope: dict,
) -> list[str]:
    """Verify the checkpoint projection against plan and journal authority.

    ``national_native`` produces this projection, but a control-plane checkpoint
    must not trust a returned dictionary merely because it came from an in
    process call.  Each completed reference is re-read from the fenced journal
    before it becomes durable checkpoint state.
    """

    if not isinstance(progress, dict):
        return ["first_strict_batch_progress_missing"]
    settings = precommit_plan.get("settings") or {}
    expected_batch = settings.get("native_precommit_batch_plan")
    if not isinstance(expected_batch, dict):
        return ["first_strict_batch_plan_missing"]
    from first_strict_execution_journal import (
        execution_scope_digest,
        normalize_execution_scope,
        read_control_execution_receipt,
    )

    issues: list[str] = []
    try:
        scope = normalize_execution_scope(control_execution_scope)
    except Exception:
        return ["first_strict_batch_scope_invalid"]
    expected_rows = expected_batch.get("ordered_samples")
    if not isinstance(expected_rows, list) or len(expected_rows) != 8:
        return ["first_strict_batch_plan_rows_invalid"]
    expected_digest = expected_batch.get("batch_plan_digest")
    if (
        progress.get("schema_version") != 1
        or progress.get("kind") != "first-strict-native-precommit-batch-progress"
        or progress.get("state") not in {
            "pending_next_sample",
            "waiting_live_lease",
        }
        or progress.get("batch_plan_digest") != expected_digest
        or progress.get("sample_plan_digest")
        != expected_batch.get("sample_plan_digest")
        or progress.get("scope_digest") != execution_scope_digest(scope)
        or progress.get("candidate_artifact_hash")
        != scope.get("candidate_artifact_hash")
        or progress.get("control_artifact_hash")
        != scope.get("control_artifact_hash")
        or progress.get("timing_plan_digest")
        != scope.get("native_match_timing_plan_digest")
        or progress.get("sample_count") != 8
        or progress.get("max_new_samples_per_invocation") != 1
    ):
        issues.append("first_strict_batch_progress_binding_invalid")
    planned = progress.get("planned_samples")
    if not isinstance(planned, list) or len(planned) != len(expected_rows):
        issues.append("first_strict_batch_progress_planned_shape_invalid")
        planned = []
    for index, (expected, observed) in enumerate(
        zip(expected_rows, planned), start=1
    ):
        if not isinstance(expected, dict) or not isinstance(observed, dict):
            issues.append(f"first_strict_batch_progress_planned_{index}_invalid")
            continue
        if (
            observed.get("repeat") != expected.get("repeat")
            or observed.get("deck_seed_base") != expected.get("deck_seed_base")
            or observed.get("bot_seed_base") != expected.get("bot_seed_base")
            or not isinstance(observed.get("match_run_id"), str)
            or not observed.get("match_run_id")
        ):
            issues.append(f"first_strict_batch_progress_planned_{index}_mismatch")

    completed = progress.get("completed_samples")
    if not isinstance(completed, list):
        issues.append("first_strict_batch_progress_completed_shape_invalid")
        completed = []
    completed_repeats: list[int] = []
    for entry in completed:
        if not isinstance(entry, dict) or type(entry.get("repeat")) is not int:
            issues.append("first_strict_batch_progress_completed_entry_invalid")
            continue
        repeat = int(entry["repeat"])
        if not 1 <= repeat <= len(expected_rows):
            issues.append("first_strict_batch_progress_completed_repeat_invalid")
            continue
        completed_repeats.append(repeat)
        expected = expected_rows[repeat - 1]
        planned_row = planned[repeat - 1] if len(planned) >= repeat else {}
        evidence, receipt_issues = read_control_execution_receipt(
            entry.get("execution_receipt"),
            expected_scope=scope,
        )
        if receipt_issues or not isinstance(evidence, dict):
            issues.append("first_strict_batch_progress_receipt_invalid")
            continue
        input_payload = evidence.get("input") or {}
        result_payload = evidence.get("result") or {}
        receipt = entry.get("execution_receipt") or {}
        if (
            entry.get("deck_seed_base") != expected.get("deck_seed_base")
            or entry.get("bot_seed_base") != expected.get("bot_seed_base")
            or input_payload.get("repeat") != repeat
            or input_payload.get("deck_seed_base") != expected.get("deck_seed_base")
            or input_payload.get("bot_seed_base") != expected.get("bot_seed_base")
            or entry.get("match_run_id") != planned_row.get("match_run_id")
            or input_payload.get("match_run_id") != entry.get("match_run_id")
            or result_payload.get("match_run_id") != entry.get("match_run_id")
            or receipt.get("match_run_id") != entry.get("match_run_id")
        ):
            issues.append("first_strict_batch_progress_receipt_binding_invalid")
    if completed_repeats != list(range(1, len(completed_repeats) + 1)):
        issues.append("first_strict_batch_progress_completed_order_invalid")
    next_repeat = progress.get("next_repeat")
    if type(next_repeat) is not int or next_repeat != len(completed_repeats) + 1:
        issues.append("first_strict_batch_progress_next_repeat_invalid")
    if progress.get("state") == "pending_next_sample" and not completed_repeats:
        issues.append("first_strict_batch_progress_empty_boundary_invalid")
    return list(dict.fromkeys(issues))


def _persist_first_strict_batch_progress(
    *,
    v: int,
    source_v: int,
    precommit_plan: dict,
    control_execution_scope: dict,
    batch_progress: object,
) -> tuple[bool, dict | None]:
    """CAS-persist a verified batch boundary without recording a gate result."""

    issues = _te._validate_first_strict_batch_progress(
        batch_progress,
        precommit_plan=precommit_plan,
        control_execution_scope=control_execution_scope,
    )
    checkpoint = _te._matching_checkpoint(v, source_v)
    if issues:
        return False, _te._state_blocked(
            "First-strict batch progress cannot be re-proven: "
            + ";".join(issues[:8]),
            v,
            source_v,
            checkpoint,
        )
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("stage") != "critic_checked"
        or (checkpoint.get("audit_context") or {}).get(
            _te._FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
        ) != control_execution_scope
    ):
        return False, _te._state_blocked(
            "First-strict batch continuation lost its critic-approved "
            "checkpoint scope.",
            v,
            source_v,
            checkpoint,
        )
    written = _te.write_pipeline_checkpoint(
        v,
        source_v,
        "critic_checked",
        audit_context={
            _te._FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY: batch_progress,
        },
        expected_checkpoint_revision=checkpoint.get("checkpoint_revision"),
        expected_checkpoint_stage="critic_checked",
        expected_workflow_run_id=checkpoint.get("workflow_run_id"),
    )
    after = _te._matching_checkpoint(v, source_v)
    if not written or not isinstance(after, dict) or (
        after.get("stage") != "critic_checked"
        or (after.get("audit_context") or {}).get(
            _te._FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
        ) != control_execution_scope
        or (after.get("audit_context") or {}).get(
            _te._FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY
        ) != batch_progress
    ):
        return False, _te._state_blocked(
            "First-strict batch continuation checkpoint CAS could not be "
            "re-proven.",
            v,
            source_v,
            after,
        )
    return True, None


def _first_strict_batch_pending_result(
    *,
    v: int,
    source_v: int,
    candidate_name: str,
    precommit_attempt: int,
    control_execution_scope: dict,
    batch_progress: dict,
) -> dict:
    """Return a fresh-provider boundary after exactly one durable sample."""

    checkpoint = _te._matching_checkpoint(v, source_v) or {}
    return _te._json_tool_result({
        "version": int(v),
        "source_v": int(source_v),
        "candidate": str(candidate_name),
        "passed": False,
        "pending": True,
        "failure_class": "infrastructure_pending",
        "checkpoint_recorded": False,
        "batch_checkpoint_recorded": True,
        "checkpoint_stage": checkpoint.get("stage"),
        "precommit_attempt": int(precommit_attempt),
        "control_execution_scope": control_execution_scope,
        "control_receipt_digest": control_execution_scope.get(
            "control_receipt_digest"
        ),
        "preserved_gate_evidence": ["quality", "review", "critic"],
        "first_strict_batch_pending": batch_progress,
        "directive": (
            "One frozen first-strict native control sample is durably journaled "
            "and indexed. End this provider cycle; a fresh provider cycle must "
            "re-prove the same scope and call run_precommit_eval to request only "
            "the next ordered sample. Do not abandon, rework, or skip ahead."
        ),
        "intent": _te.make_intent(
            "continue",
            next_tool="run_precommit_eval",
            failure_class="infrastructure_pending",
            authority="tool:precommit_eval",
            safe_to_auto_execute=False,
            reason="first_strict_batch_next_sample",
        ),
    })

