"""Pipeline tools: pre-commit evaluation and inline evaluation (battle-based)."""

import asyncio
import json
import logging
import os
from pathlib import Path
import threading
import time

from bot_namespace import bot_name as active_bot_name, parse_bot_version
from tool_runtime_guard import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
)

from tool_helpers import (
    _json_tool_result, _get_ui,
    _matching_checkpoint, _record_gate, _gate_payload, _state_blocked,
    _quality_gate_ok, _review_gate_ok, _critic_gate_ok,
    _select_precommit_opponents, _resolve_version_args,
    _set_pipeline_status,
    _prepare_official_profile_refresh,
)
from evolution_infra import write_pipeline_checkpoint, MAX_PRECOMMIT_RETRIES
from system_log import log_system_event
from pipeline_schema import GateResult, ScoreCard
from workflow_profiles import get_workflow_profile
from failure_classification import INFRA_BLOCKER_REASONS, is_infra_blocker
from pipeline_intents import make_intent
from precommit_eval_contract import (
    PrecommitEvalContractError,
    build_evaluation_contract,
    create_precommit_plan,
    opponents_from_plan,
    validate_evaluation_contract,
    validate_precommit_plan,
)
from strength_order import (
    is_precommit_gate_matchup,
    is_strength_matchup,
)

try:
    from candidate_store import (
        append_candidate_event,
        candidate_observability_identity,
    )
except Exception:  # pragma: no cover
    append_candidate_event = None
    candidate_observability_identity = None

from logging_config import get_logger
log = get_logger("tool_eval")

_FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY = (
    "first_strict_control_execution_scope"
)
_FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY = (
    "first_strict_control_batch_progress"
)


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

        checkpoint = _matching_checkpoint(
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
    result["intent"] = make_intent(
        "abandoned" if result["abandoned"] else "abandon",
        next_tool=(
            None if result["abandoned"] else "abandon_generation"
        ),
        failure_class=str(result.get("failure_class") or "control_plane"),
        authority="tool:precommit_eval",
        safe_to_auto_execute=not result["abandoned"],
        reason=str(reason),
    )
    return _json_tool_result(result)


def is_precommit_shutdown() -> bool:
    """True if the current precommit attempt has been signalled to abort."""

    return current_precommit_shutdown_token().is_set()


# Group A (root-cause-audit follow-up 2026-06-22): blocker reasons that indicate
# INFRASTRUCTURE failure (daemon crash / CPU contention / slow battle-MC), NOT a
# bot regression. These must NOT force the Orchestrator to rework worker code
# (which is unchanged and would give the same result) — they trigger an
# infra-aware retry with lower n_games instead. v147 timed out on attempt 1/2,
# passed on attempt 3 at n_games=6: the bot was fine, the infra wasn't.
def _is_infra_blocker(reason):
    """True if this blocker reason is an infrastructure failure, not a bot
    regression. Infra blockers trigger retry-with-lower-n_games; regression
    blockers (lost_to_parent / aggregate_precommit_regression / semantic_regression)
    still hard-fail the gate."""
    return is_infra_blocker(reason)


# ──────────────────────────────────────────────
# Precommit eval tuning constants
# ──────────────────────────────────────────────
# Default and max n_games per opponent for precommit eval. 8 gives enough paired
# net-chip observations for the bootstrap gate; 16 is the hard ceiling so
# precommit eval still fits within the cycle budget.
PRECOMMIT_DEFAULT_N_GAMES = 8
PRECOMMIT_MIN_N_GAMES = 4
PRECOMMIT_MAX_N_GAMES = 16

def _env_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _official_gate_enabled(name: str, *, include_required: bool = True) -> bool:
    return (include_required and _env_enabled("POK_OFFICIAL_REQUIRED")) or _env_enabled(name)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


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

    candidate_token = _official_bot_token(candidate)
    selection = None
    opponent = None
    if opponent_rounds > 0:
        selection = select_official_opponent(
            candidate_token,
            get_active_bots(),
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
            min(PRECOMMIT_MAX_N_GAMES, max(PRECOMMIT_MIN_N_GAMES, sample_target)),
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
    expected = _build_first_strict_control_execution_scope(
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

    checkpoint = _matching_checkpoint(v, source_v) or {}
    return _json_tool_result({
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
        "intent": make_intent(
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

    issues = _validate_first_strict_batch_progress(
        batch_progress,
        precommit_plan=precommit_plan,
        control_execution_scope=control_execution_scope,
    )
    checkpoint = _matching_checkpoint(v, source_v)
    if issues:
        return False, _state_blocked(
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
            _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
        ) != control_execution_scope
    ):
        return False, _state_blocked(
            "First-strict batch continuation lost its critic-approved "
            "checkpoint scope.",
            v,
            source_v,
            checkpoint,
        )
    written = write_pipeline_checkpoint(
        v,
        source_v,
        "critic_checked",
        audit_context={
            _FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY: batch_progress,
        },
        expected_checkpoint_revision=checkpoint.get("checkpoint_revision"),
        expected_checkpoint_stage="critic_checked",
        expected_workflow_run_id=checkpoint.get("workflow_run_id"),
    )
    after = _matching_checkpoint(v, source_v)
    if not written or not isinstance(after, dict) or (
        after.get("stage") != "critic_checked"
        or (after.get("audit_context") or {}).get(
            _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
        ) != control_execution_scope
        or (after.get("audit_context") or {}).get(
            _FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY
        ) != batch_progress
    ):
        return False, _state_blocked(
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

    checkpoint = _matching_checkpoint(v, source_v) or {}
    return _json_tool_result({
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
        "intent": make_intent(
            "continue",
            next_tool="run_precommit_eval",
            failure_class="infrastructure_pending",
            authority="tool:precommit_eval",
            safe_to_auto_execute=False,
            reason="first_strict_batch_next_sample",
        ),
    })


async def _run_national_precommit_backend(
    *,
    v: int,
    source_v: int,
    requested_n_games: int,
    effective_n_games: int | None = None,
    candidate_name: str,
    parent_name: str,
    candidate_entry,
    code_fingerprint: str,
    workflow_profile,
    candidate_id: str,
    opponents: list,
    all_opponents: list,
    precommit_attempt: int,
    initial_blockers: list,
    started_at: float,
    precommit_plan: dict,
    evaluation_contract: dict,
    workflow_run_id: str = "",
    checkpoint_revision: int = 0,
    shutdown_token: threading.Event | None = None,
    control_execution_scope: dict | None = None,
):
    """Run the sole active 70-hand native TCP precommit backend."""
    candidate_observability = (
        candidate_observability_identity(v, source_v)
        if candidate_observability_identity is not None
        else {
            "candidate_id": candidate_id,
            "parent_ids": [],
            "lineage_kind": "unavailable",
        }
    )
    candidate_id = str(candidate_observability["candidate_id"])
    candidate_parent_ids = list(candidate_observability["parent_ids"])
    candidate_lineage_metrics = {
        key: candidate_observability[key]
        for key in (
            "lineage_kind",
            "numeric_high_water_version",
            "source_artifact_inherited",
        )
        if key in candidate_observability
    }
    settings = precommit_plan.get("settings") or {}
    national_hands = int(settings.get("hands_per_match") or 0)
    national_matches = int(settings.get("matches_per_opponent") or 0)
    try:
        from national_native import (
            LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            require_native_match_timing_plan,
        )

        native_match_timing_plan = require_native_match_timing_plan(
            settings.get("native_match_timing_plan"),
            hands=national_hands,
            requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
        )
        if settings.get("native_match_timing_plan_digest") != (
            native_match_timing_plan.digest()
        ):
            raise RuntimeError("precommit native timing plan digest mismatch")
    except Exception as exc:
        raise RuntimeError(
            "precommit native timing plan unavailable:"
            f"{type(exc).__name__}: {str(exc)[:240]}"
        ) from exc

    if getattr(workflow_profile, "national_execution_mode", None) != "native_tcp":
        raise RuntimeError("precommit supports only national native_tcp evaluation")
    try:
        from national_runtime_probe import runtime_probe_native_template_evidence

        native_template_evidence = runtime_probe_native_template_evidence()
    except Exception as exc:
        raise RuntimeError(
            "precommit native runtime identity unavailable:"
            f"{type(exc).__name__}:{str(exc)[:200]}"
        ) from exc
    native_tcp_mode = True
    system_control_plan = any(
        str(item.get("authority") or "") == "system_first_strict_control"
        for item in opponents
    )
    native_precommit_batch_plan = None
    if system_control_plan:
        try:
            from precommit_eval_contract import build_native_precommit_batch_plan

            native_precommit_batch_plan = (
                build_native_precommit_batch_plan(
                    list(precommit_plan.get("sample_plan") or []),
                    native_timing_plan=native_match_timing_plan,
                    first_strict_control=True,
                )
            )
            if (
                settings.get("native_precommit_batch_plan")
                != native_precommit_batch_plan
                or settings.get("native_precommit_batch_plan_digest")
                != native_precommit_batch_plan.get("batch_plan_digest")
            ):
                raise RuntimeError("first strict batch plan digest mismatch")
        except Exception as exc:
            raise RuntimeError(
                "first strict native batch plan unavailable:"
                f"{type(exc).__name__}: {str(exc)[:240]}"
            ) from exc
    opponents_with_paths = []
    for item in opponents:
        copied = dict(item)
        if str(item.get("authority") or "") != "system_first_strict_control":
            try:
                copied["path"] = str(
                    get_bot_dir(parse_bot_version(item["name"]))
                )
            except Exception:
                pass
        opponents_with_paths.append(copied)

    blockers = list(initial_blockers or [])
    native_precommit_progress_callback = None
    try:
        from pipeline_state import make_native_match_heartbeat_reporter

        progress_checkpoint = _matching_checkpoint(v, source_v)
        if (
            isinstance(progress_checkpoint, dict)
            and str(progress_checkpoint.get("workflow_run_id") or "")
            == str(workflow_run_id or progress_checkpoint.get("workflow_run_id") or "")
        ):
            native_precommit_progress_callback = (
                make_native_match_heartbeat_reporter(
                    progress_checkpoint,
                    owner_tool="run_precommit_eval",
                )
            )
    except Exception:
        native_precommit_progress_callback = None
    execution_protocol = "national_native_tcp" if native_tcp_mode else "national"
    if system_control_plan:
        if control_execution_scope is None:
            control_execution_scope = (
                _build_first_strict_control_execution_scope(
                    v=v,
                    candidate_name=candidate_name,
                    code_fingerprint=code_fingerprint,
                    opponents=opponents_with_paths,
                    precommit_plan=precommit_plan,
                    evaluation_contract=evaluation_contract,
                    workflow_run_id=workflow_run_id,
                    checkpoint_revision=checkpoint_revision,
                    precommit_attempt=precommit_attempt,
                )
            )
        control_execution_scope, scope_error = (
            _validate_first_strict_control_execution_scope(
                control_execution_scope,
                v=v,
                candidate_name=candidate_name,
                code_fingerprint=code_fingerprint,
                opponents=opponents_with_paths,
                precommit_plan=precommit_plan,
                evaluation_contract=evaluation_contract,
                workflow_run_id=workflow_run_id,
                precommit_attempt=precommit_attempt,
            )
        )
        if scope_error:
            raise RuntimeError(scope_error)
    if national_hands != 70:
        blockers.append({
            "reason": "national_strength_hands_not_70",
            "details": f"Production precommit requires 70 hands per match; plan requested {national_hands}.",
        })
    if not blockers and opponents_with_paths:
        try:
            from national_native import run_native_precommit

            national_result = await run_native_precommit(
                str(candidate_entry),
                opponents_with_paths,
                hands=national_hands,
                matches_per_opponent=national_matches,
                parent_label=parent_name,
                sample_plan=list(precommit_plan.get("sample_plan") or []),
                batch_plan=native_precommit_batch_plan,
                control_execution_scope=control_execution_scope,
                cancel_token=shutdown_token,
                timing_plan=native_match_timing_plan,
                progress_callback=native_precommit_progress_callback,
            )
            live_pending = _control_execution_pending_from_native_result(
                national_result,
                control_execution_scope=control_execution_scope,
            )
            batch_pending = (
                national_result.get("first_strict_batch_pending")
                if isinstance(national_result, dict)
                else None
            )
            if live_pending is not None or batch_pending is not None:
                if (
                    not system_control_plan
                    or control_execution_scope is None
                    or not isinstance(batch_pending, dict)
                ):
                    raise RuntimeError(
                        "first_strict_batch_pending_outside_bound_control_plan"
                    )
                persisted, blocked_result = _persist_first_strict_batch_progress(
                    v=v,
                    source_v=source_v,
                    precommit_plan=precommit_plan,
                    control_execution_scope=control_execution_scope,
                    batch_progress=batch_pending,
                )
                if not persisted:
                    return blocked_result
            if live_pending is not None:
                return _first_strict_live_lease_pending_result(
                    v=v,
                    source_v=source_v,
                    candidate_name=candidate_name,
                    precommit_attempt=precommit_attempt,
                    control_execution_scope=control_execution_scope,
                    pending_execution=live_pending,
                    batch_progress=batch_pending,
                    batch_checkpoint_recorded=True,
                )
            if batch_pending is not None:
                return _first_strict_batch_pending_result(
                    v=v,
                    source_v=source_v,
                    candidate_name=candidate_name,
                    precommit_attempt=precommit_attempt,
                    control_execution_scope=control_execution_scope,
                    batch_progress=batch_pending,
                )
            blockers.extend(national_result.get("blockers") or [])
            if system_control_plan:
                from first_strict_control import validate_control_receipt

                control_receipt = opponents_with_paths[0].get("control_receipt")
                control_issues = validate_control_receipt(
                    control_receipt,
                    candidate_version=v,
                    source_version=source_v,
                )
                if control_issues:
                    blockers.append({
                        "reason": "first_strict_control_contract_drift",
                        "details": ";".join(control_issues[:12]),
                    })
            if native_tcp_mode and _observed_native_sample_plan(national_result) != list(
                precommit_plan.get("sample_plan") or []
            ):
                blockers.append({
                    "reason": "native_precommit_sample_plan_mismatch",
                    "details": "Native precommit did not execute the frozen deck/bot seed schedule.",
                })
        except Exception as exc:
            # The journal may be used directly by a native runner which
            # propagates this typed state rather than returning the structured
            # ``control_execution_pending`` result above.  It has the same
            # scope/lease proof and is never a strategy or control regression.
            try:
                from first_strict_execution_journal import FirstStrictExecutionPending

                live_pending_exception = isinstance(
                    exc,
                    FirstStrictExecutionPending,
                )
            except Exception:
                live_pending_exception = False
            if live_pending_exception:
                if not system_control_plan or control_execution_scope is None:
                    raise RuntimeError(
                        "control_execution_pending_outside_first_strict_plan"
                    ) from exc
                return _first_strict_live_lease_pending_result(
                    v=v,
                    source_v=source_v,
                    candidate_name=candidate_name,
                    precommit_attempt=precommit_attempt,
                    control_execution_scope=control_execution_scope,
                    pending_execution=exc.pending,
                )
            national_result = {
                "evaluation_protocol": execution_protocol,
                "candidate": candidate_name,
                "opponents": opponents_with_paths,
                "matchups": [],
                "total_wins": 0,
                "total_losses": 0,
                "total_draws": 0,
                "paired_bootstrap": {
                    "protocol": execution_protocol,
                    "hands_per_match": national_hands,
                    "matches_per_opponent": national_matches,
                    "net_chips_samples": 0,
                    "net_chips_mean": None,
                    "gate_degraded": True,
                },
                "blockers": [{
                    "reason": "native_precommit_exception" if native_tcp_mode else "national_precommit_exception",
                    "details": f"{type(exc).__name__}: {str(exc)[:500]}",
                }],
                "passed": False,
            }
            blockers.extend(national_result["blockers"])
    else:
        national_result = {
            "evaluation_protocol": execution_protocol,
            "candidate": candidate_name,
            "opponents": opponents_with_paths,
            "matchups": [],
            "total_wins": 0,
            "total_losses": 0,
            "total_draws": 0,
            "paired_bootstrap": {
                "protocol": execution_protocol,
                "hands_per_match": national_hands,
                "matches_per_opponent": national_matches,
                "net_chips_samples": 0,
                "net_chips_mean": None,
                "gate_degraded": True,
            },
            "blockers": blockers,
            "passed": False,
        }

    official_platform_result = {}
    if native_tcp_mode and _official_gate_enabled("POK_OFFICIAL_PRECOMMIT_GATE") and not blockers:
        # The official Windows platform is a protocol/compliance oracle here.
        # Strength and long-run tracking stay on the local native TCP harness.
        official_self_rounds = max(0, _env_int("POK_OFFICIAL_PRECOMMIT_SELF_ROUNDS", 1))
        official_opponent_rounds = max(0, _env_int("POK_OFFICIAL_PRECOMMIT_OPPONENT_ROUNDS", 1))
        official_hands = max(1, min(70, _env_int("POK_OFFICIAL_PRECOMMIT_TARGET_HANDS", 10)))
        try:
            official_platform_result = _request_official_precommit_status(
                candidate=candidate_entry,
                self_play_rounds=official_self_rounds,
                opponent_rounds=official_opponent_rounds,
                target_hands=official_hands,
            )
            national_result["official_platform"] = official_platform_result
        except Exception as exc:
            official_platform_result = {
                "passed": False,
                "blocking": False,
                "issues": [f"official_platform_compliance_exception: {type(exc).__name__}: {str(exc)[:500]}"],
            }
            national_result["official_platform"] = official_platform_result

    total_wins = int(national_result.get("total_wins", 0) or 0)
    total_losses = int(national_result.get("total_losses", 0) or 0)
    total_draws = int(national_result.get("total_draws", 0) or 0)
    matchups = list(national_result.get("matchups") or [])
    paired_bootstrap_payload = dict(national_result.get("paired_bootstrap") or {})
    from strength_order import summarize_70_hand_net_chips, summarize_match_outcomes

    gate_samples = [
        int(value)
        for matchup in matchups
        if is_precommit_gate_matchup(matchup)
        for value in (matchup.get("net_chips") or [])
    ]
    strength_samples = [
        int(value)
        for matchup in matchups
        if is_strength_matchup(matchup)
        for value in (matchup.get("net_chips") or [])
    ]
    precommit_gate_order = summarize_70_hand_net_chips(gate_samples)
    strength_order = summarize_70_hand_net_chips(strength_samples)
    outcome_order = summarize_match_outcomes(total_wins, total_losses, total_draws)
    gate_sample_count = int(
        paired_bootstrap_payload.get("net_chips_samples", 0) or 0
    )
    strength_sample_count = int(
        paired_bootstrap_payload.get(
            "strength_net_chips_samples",
            gate_sample_count,
        )
        or 0
    )
    expected_gate_samples = national_matches * sum(
        1 for item in opponents_with_paths if is_precommit_gate_matchup(item)
    )
    expected_strength_samples = national_matches * sum(
        1 for item in opponents_with_paths if is_strength_matchup(item)
    )
    strength_evidence_required = bool(
        settings.get("strength_evidence_required", True)
    )
    minimum_gate_samples = int(
        settings.get("control_min_samples") or 2
    )
    if gate_sample_count <= 0 and not blockers:
        blockers.append({
            "reason": "national_no_samples",
            "details": "National precommit produced zero completed match samples.",
        })
    blockers.extend(
        _national_sample_contract_blockers(
            paired_bootstrap_payload,
            expected_samples=expected_gate_samples,
        )
    )
    if gate_sample_count != outcome_order["samples"]:
        blockers.append({
            "reason": "national_outcome_sample_mismatch",
            "details": (
                f"Outcome counts describe {outcome_order['samples']} samples but "
                f"the admitted precommit vector contains {gate_sample_count}."
            ),
        })
    if precommit_gate_order["samples"] != gate_sample_count or (
        precommit_gate_order["positive_matches"] != total_wins
        or precommit_gate_order["negative_matches"] != total_losses
        or precommit_gate_order["zero_matches"] != total_draws
    ):
        blockers.append({
            "reason": "national_precommit_sign_mismatch",
            "details": "Admitted precommit net-chip signs disagree with the recorded W/L/D outcomes.",
        })
    if strength_order["samples"] != strength_sample_count:
        blockers.append({
            "reason": "national_strength_sample_mismatch",
            "details": (
                f"Strength vector contains {strength_order['samples']} samples but "
                f"the runtime declared {strength_sample_count}."
            ),
        })
    if strength_evidence_required and (
        strength_order["positive_matches"] != total_wins
        or strength_order["negative_matches"] != total_losses
        or strength_order["zero_matches"] != total_draws
    ):
        blockers.append({
            "reason": "national_strength_sign_mismatch",
            "details": "Admitted strength signs disagree with the recorded W/L/D outcomes.",
        })
    if system_control_plan:
        from first_strict_control import (
            control_gate_blockers,
            validate_control_receipt,
        )

        control_blockers, control_gate = control_gate_blockers(
            national_result,
            expected_sample_plan=list(precommit_plan.get("sample_plan") or []),
            expected_execution_scope=control_execution_scope,
        )
        existing_reasons = {
            str(item.get("reason") or "")
            for item in blockers
            if isinstance(item, dict)
        }
        blockers.extend(
            item for item in control_blockers
            if str(item.get("reason") or "") not in existing_reasons
        )
        final_control_issues = validate_control_receipt(
            opponents_with_paths[0].get("control_receipt"),
            candidate_version=v,
            source_version=source_v,
        )
        if final_control_issues and not any(
            isinstance(item, dict)
            and item.get("reason") == "first_strict_control_contract_drift"
            for item in blockers
        ):
            blockers.append({
                "reason": "first_strict_control_contract_drift",
                "details": ";".join(final_control_issues[:12]),
            })
    else:
        control_gate = None
    passed = (
        bool(national_result.get("passed"))
        and len(blockers) == 0
        and gate_sample_count == expected_gate_samples
        and strength_sample_count == expected_strength_samples
        and gate_sample_count >= minimum_gate_samples
        and (not strength_evidence_required or strength_sample_count >= 2)
    )

    try:
        log_system_event(
            "pipeline.precommit_eval.national",
            "info" if passed else "warn",
            f"National {'native TCP ' if native_tcp_mode else ''}precommit "
            f"{'passed' if passed else 'FAILED'} for v{v}: "
            f"{total_wins}W-{total_losses}L-{total_draws}D vs {len(all_opponents)} opponents",
            {
                "version": v,
                "source_v": source_v,
                "passed": passed,
                "evaluation_protocol": execution_protocol,
                "execution_mode": "native_tcp",
                "hands_per_match": national_hands,
                "matches_per_opponent": national_matches,
                "blockers": blockers,
                "paired_bootstrap": paired_bootstrap_payload,
                "elapsed_sec": round(time.time() - started_at, 2),
            },
        )
    except Exception:
        pass

    result = {
        "version": v,
        "source_v": source_v,
        "n_games": national_matches,
        "requested_n_games": requested_n_games,
        "workflow_profile_id": workflow_profile.profile_id,
        "evaluation_protocol": execution_protocol,
        "national_execution_mode": "native_tcp",
        **native_template_evidence,
        "hands_per_match": national_hands,
        "matches_per_opponent": national_matches,
        "expected_net_chips_samples": expected_gate_samples,
        "expected_strength_net_chips_samples": expected_strength_samples,
        "opponents": all_opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "passed": passed,
        "blockers": blockers,
        "paired_bootstrap": paired_bootstrap_payload,
        "precommit_gate_order": precommit_gate_order,
        "strength_order": strength_order,
        "outcome_order": outcome_order,
        "precommit_evidence_authority": (
            "first_strict_bootstrap_regression_v1"
            if system_control_plan
            else "local_precommit_strength"
        ),
        "precommit_gate_admitted": True,
        "strength_admitted": not system_control_plan,
        "rating_eligible": not system_control_plan,
        "official_opponent_eligible": not system_control_plan,
        "first_strict_control_gate": control_gate,
        "primary_70_hand_match_score": outcome_order.get("primary_match_score"),
        "secondary_net_chips_total": strength_order.get("secondary_net_chips_total"),
        "secondary_net_chips_mean": strength_order.get("secondary_net_chips_mean"),
        "precommit_gate_net_chips_total": precommit_gate_order.get(
            "secondary_net_chips_total"
        ),
        "precommit_gate_net_chips_mean": precommit_gate_order.get(
            "secondary_net_chips_mean"
        ),
        "national": national_result,
        "official_platform": official_platform_result,
        "code_fingerprint": code_fingerprint,
        "precommit_eval_plan": precommit_plan,
        "precommit_eval_contract": evaluation_contract,
        "precommit_eval_contract_digest": evaluation_contract.get("contract_digest"),
        "control_execution_scope": (
            national_result.get("control_execution_scope")
            if system_control_plan
            else None
        ),
    }

    scorecard = ScoreCard(
        name="precommit_eval",
        primary_score=outcome_order.get("primary_match_score"),
        metrics={
            "evaluation_protocol": execution_protocol,
            "national_execution_mode": "native_tcp",
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_draws": total_draws,
            "n_opponents": len(all_opponents),
            "hands_per_match": national_hands,
            "matches_per_opponent": national_matches,
            "primary_70_hand_match_score": outcome_order.get("primary_match_score"),
            "secondary_net_chips_mean": strength_order.get("secondary_net_chips_mean"),
            "precommit_evidence_authority": (
                "first_strict_bootstrap_regression_v1"
                if system_control_plan
                else "local_precommit_strength"
            ),
            "strength_admitted": not system_control_plan,
            "rating_eligible": not system_control_plan,
            "official_opponent_eligible": not system_control_plan,
        },
    )
    scorecard.add(GateResult.from_bool(
        "national_precommit_regression",
        passed,
        metrics=paired_bootstrap_payload,
        failures=[str(b)[:500] for b in blockers],
    ))
    if official_platform_result:
        official_status = str(official_platform_result.get("status") or "")
        official_issues = official_platform_result.get("issues", []) or []
        try:
            from official_certification import official_compliance_verdict as _official_compliance_verdict
            official_verdict = _official_compliance_verdict(official_platform_result)
        except Exception:
            official_verdict = {
                "ok": True,
                "blocking": False,
                "inconclusive": True,
                "classification": "inconclusive",
            }
        scorecard.add(GateResult.from_bool(
            "official_platform_compliance",
            bool(official_verdict.get("ok")),
            metrics={
                "status": official_status,
                "mode": official_platform_result.get("mode"),
                "queued": official_platform_result.get("queued"),
                "cache_hit": official_platform_result.get("cache_hit"),
                "blocking": official_verdict.get("blocking"),
                "inconclusive": official_verdict.get("inconclusive"),
                "classification": official_verdict.get("classification"),
                **(official_platform_result.get("summary", {}) or {}),
            },
            failures=official_issues[:5] if bool(official_verdict.get("blocking")) else [],
            artifacts={"report": official_platform_result},
            blocking=False,
        ))
    result["scorecard"] = scorecard.model_dump()

    if passed:
        result["failure_class"] = "passed"
        result["intent"] = make_intent(
            "continue",
            next_tool="commit_bot",
            authority="tool:precommit_eval",
            safe_to_auto_execute=True,
        )
    else:
        worst_opponent = _worst_precommit_opponent(matchups, blockers)
        worst_wins, worst_losses = _worst_wins_losses(matchups, worst_opponent)
        if system_control_plan:
            result["directive"] = (
                f"First-strict system-control precommit FAILED for v{v} "
                f"({worst_wins}W-{worst_losses}L). Do not invoke an ordinary "
                "Worker repair and do not retry unchanged code. Abandon this "
                "generation, revise the checked-in deterministic blueprint/control "
                "contract, and restart from a fresh empty-pool authority receipt."
            )
            result["failure_class"] = "system_bootstrap_regression"
            result["intent"] = make_intent(
                "pause",
                failure_class="system_bootstrap_regression",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="first_strict_control_regression",
            )
        else:
            result["directive"] = (
                f"National precommit FAILED (attempt {precommit_attempt}/{MAX_PRECOMMIT_RETRIES}) — "
                f"the final gate now uses {'native TCP ' if native_tcp_mode else ''}national 70-hand rules, not local mirror battle. "
                f"Do NOT call run_precommit_eval again on unchanged code. Rework the bot against "
                f"{worst_opponent} ({worst_wins}W-{worst_losses}L) and the listed blockers."
            )
            result["failure_class"] = "regression"
            result["intent"] = make_intent(
                "rework",
                next_tool="execute_workers",
                failure_class="regression",
                authority="tool:precommit_eval",
                safe_to_auto_execute=True,
                reason="national_precommit_regression",
            )

    checkpoint_stage = "verified" if passed else "precommit_failed"
    checkpoint_feedback = None if passed else result.get("directive")
    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "precommit_eval",
        _gate_payload(
            v,
            source_v,
            passed,
            **{k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}},
        ),
        stage=checkpoint_stage,
        reviewer_feedback=checkpoint_feedback,
    )
    result["checkpoint_recorded"] = checkpoint_recorded

    if append_candidate_event:
        try:
            append_candidate_event(
                "precommit_finished",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage="verified" if passed else "precommit_failed",
                parent_ids=candidate_parent_ids,
                gate="precommit_eval",
                scorecard=scorecard,
                gate_results=scorecard.gates,
                metrics={
                    **candidate_lineage_metrics,
                    "passed": passed,
                    "evaluation_protocol": execution_protocol,
                    "national_execution_mode": "native_tcp",
                    "total_wins": total_wins,
                    "total_losses": total_losses,
                    "total_draws": total_draws,
                    "net_chips_mean": paired_bootstrap_payload.get("net_chips_mean"),
                },
                failures=[str(b)[:500] for b in blockers],
                failure_class=(
                    ""
                    if passed
                    else "system_bootstrap_regression"
                    if system_control_plan
                    else "national_precommit_regression"
                ),
            )
        except Exception as e:
            log.warning("candidate ledger national precommit_finished write failed: %s", e)
    if system_control_plan and not passed:
        return await _abandon_first_strict_generation(
            result,
            reason="first_strict_control_precommit_rejected",
        )
    return _json_tool_result(result)


async def _run_national_precommit_attempt(
    shutdown_token: threading.Event,
    **kwargs,
):
    """Run one backend attempt and fence its exact token on task cancellation."""

    try:
        return await _run_national_precommit_backend(
            shutdown_token=shutdown_token,
            **kwargs,
        )
    except asyncio.CancelledError:
        # Deterministic checkpoint routes can be cancelled outside
        # ``_run_one_cycle``.  Set the locally captured identity rather than the
        # module's possibly-rotated current token.
        set_precommit_shutdown(shutdown_token)
        raise


# ──────────────────────────────────────────────
# Precommit Eval
# ──────────────────────────────────────────────


def _worst_precommit_opponent(matchups, blockers):
    """Return the opponent name most responsible for a precommit failure.

    Priority: the first blocker that names a regression opponent
    (lost_to_parent / lost_to_opponent), else the matchup with the most losses,
    else the matchup with the worst W-L margin. Returns "unknown" if there are
    no matchups and no named blockers.
    """
    if blockers:
        for b in blockers:
            reason = b.get("reason") if isinstance(b, dict) else None
            if reason in ("lost_to_parent", "lost_to_opponent"):
                opp = b.get("opponent")
                if opp:
                    return opp
    if matchups:
        best = None
        best_key = None
        for m in matchups:
            # Typed non-gate matchups are not valid failure-attribution targets.
            if m.get("precommit_gate_admitted") is False:
                continue
            opp = m.get("opponent")
            losses = int(m.get("losses", 0) or 0)
            wins = int(m.get("wins", 0) or 0)
            # Sort by (most losses, then worst margin) so the heaviest defeat wins.
            key = (losses, losses - wins)
            if best_key is None or key > best_key:
                best_key = key
                best = opp
        if best is not None:
            return best
    return "unknown"


def _worst_wins_losses(matchups, opponent):
    """Return (wins, losses) for the given opponent across matchups, else (0, 0)."""
    if not opponent or opponent == "unknown" or not matchups:
        return 0, 0
    for m in matchups:
        if m.get("opponent") == opponent:
            return int(m.get("wins", 0) or 0), int(m.get("losses", 0) or 0)
    return 0, 0


def _infra_timeout_retry_authority_error(
    checkpoint,
    *,
    candidate_dir,
    code_fingerprint,
    version,
    source_v,
):
    """Return why an infra-timeout candidate cannot reuse passed gate evidence.

    ``infra_timed_out`` is only a transport/evaluation overlay.  Removing it is
    safe only while the complete candidate artifact is still the byte identity
    that passed the active quality -> review -> critic chain.  The quality gate
    owns the complete-artifact fingerprint and ``repair_baseline_artifact_hash``
    carries that same frozen identity through the later non-mutating gates.
    """

    candidate_dir = Path(candidate_dir)
    candidate_entry = candidate_dir / "national_bot.py"
    if (
        not candidate_dir.is_dir()
        or not candidate_entry.is_file()
        or not isinstance(code_fingerprint, str)
        or len(code_fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in code_fingerprint)
    ):
        return "Infra-timeout retry candidate artifact is missing or unreadable."

    if not (
        _quality_gate_ok(checkpoint)
        and _review_gate_ok(checkpoint)
        and _critic_gate_ok(checkpoint)
    ):
        return (
            "Infra-timeout retry quality/review/critic gate chain is incomplete "
            "or invalid."
        )

    precommit_attempt = checkpoint.get("precommit_attempt")
    if type(precommit_attempt) is not int or precommit_attempt < 1:
        return (
            "Infra-timeout retry checkpoint is missing its frozen logical "
            "precommit attempt identity."
        )

    gates = checkpoint.get("gate_results") or {}
    for gate_name in ("quality", "review", "critic"):
        gate = gates.get(gate_name) or {}
        if (
            gate.get("version") != int(version)
            or gate.get("source_v") != int(source_v)
        ):
            return (
                "Infra-timeout retry quality/review/critic gate identity does "
                "not match the active generation."
            )

    quality_fingerprint = str(
        ((gates.get("quality") or {}).get("code_fingerprint")) or ""
    )
    frozen_fingerprint = str(
        checkpoint.get("repair_baseline_artifact_hash") or ""
    )
    for fingerprint in (quality_fingerprint, frozen_fingerprint):
        if (
            len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            return (
                "Infra-timeout retry checkpoint is missing its frozen candidate "
                "artifact identity."
            )
    if quality_fingerprint != frozen_fingerprint:
        return (
            "Infra-timeout retry quality gate and checkpoint artifact bindings "
            "disagree."
        )
    if code_fingerprint != quality_fingerprint:
        return (
            "Infra-timeout retry candidate artifact drifted from the passed "
            "quality/review/critic evidence."
        )

    audit_context = checkpoint.get("audit_context") or {}
    stored_plan = audit_context.get("precommit_eval_plan")
    plan_opponents = (
        stored_plan.get("opponents")
        if isinstance(stored_plan, dict)
        else []
    )
    system_control_plan = any(
        isinstance(item, dict)
        and str(item.get("authority") or "")
        == "system_first_strict_control"
        for item in (plan_opponents or [])
    )
    if system_control_plan:
        try:
            frozen_opponents = opponents_from_plan(stored_plan)
            frozen_contract = build_evaluation_contract(
                stored_plan,
                candidate_code_fingerprint=code_fingerprint,
            )
        except Exception as exc:
            return (
                "Infra-timeout retry first-strict plan identity is invalid: "
                f"{type(exc).__name__}: {str(exc)[:240]}"
            )
        _scope, scope_error = _validate_first_strict_control_execution_scope(
            audit_context.get(
                _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
            ),
            v=int(version),
            candidate_name=active_bot_name(int(version)),
            code_fingerprint=code_fingerprint,
            opponents=frozen_opponents,
            precommit_plan=stored_plan,
            evaluation_contract=frozen_contract,
            workflow_run_id=str(checkpoint.get("workflow_run_id") or ""),
            precommit_attempt=int(precommit_attempt),
        )
        if scope_error:
            return "Infra-timeout retry cannot re-prove its journal: " + scope_error
    return None


@tool("run_precommit_eval", "Run the final native national-TCP regression check before commit.", {"version": int, "source_v": int, "n_games": int})
async def run_precommit_eval(args):
    _t0 = time.time()
    # Capture before any await or checkpoint transition.  The outer cycle may
    # rotate the module's current token as soon as this attempt is cancelled;
    # this local reference must remain bound to the old, permanently-set token.
    precommit_shutdown_token = begin_precommit_shutdown_attempt()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    # Cap n_games: precommit eval is a quick regression check, NOT a full evaluation.
    # Default is PRECOMMIT_DEFAULT_N_GAMES (8), clamped to
    # [PRECOMMIT_MIN_N_GAMES, PRECOMMIT_MAX_N_GAMES]. The regression gate now uses paired net-chip
    # bootstrap CIs, which are much less noisy than binary W/L at the same n_games.
    requested = int(args.get("n_games", PRECOMMIT_DEFAULT_N_GAMES) or PRECOMMIT_DEFAULT_N_GAMES)
    n_games = min(max(PRECOMMIT_MIN_N_GAMES, requested), PRECOMMIT_MAX_N_GAMES)

    candidate_name = active_bot_name(v)
    parent_name = active_bot_name(source_v)
    candidate_dir = get_bot_dir(v)
    try:
        from tool_gates import _bot_code_fingerprint
        code_fingerprint = _bot_code_fingerprint(candidate_dir)
    except Exception:
        code_fingerprint = ""

    workflow_profile = get_workflow_profile()
    if (
        getattr(workflow_profile, "national_execution_mode", None) != "native_tcp"
        or getattr(workflow_profile, "evaluation_protocol", None) != "national"
    ):
        return _json_tool_result({"error": "only national native_tcp precommit is supported"})
    native_tcp_mode = True
    expected_execution_mode = "native_tcp"
    evaluation_protocol = "national"
    national_evaluation = True
    candidate_entry = candidate_dir / "national_bot.py"
    try:
        from national_runtime_probe import (
            runtime_probe_native_template_evidence,
            runtime_probe_native_template_evidence_matches,
        )

        native_template_evidence = runtime_probe_native_template_evidence()
    except Exception as exc:
        # Never replace a missing current system runtime identity with a
        # best-effort cache hit.  Retry only after the operator restores the
        # checked-in native runtime authority.
        return _json_tool_result({
            "error": "native_runtime_identity_unavailable",
            "version": v,
            "source_v": source_v,
            "passed": False,
            "failure_class": "infrastructure",
            "details": f"{type(exc).__name__}: {str(exc)[:200]}",
        })

    # Idempotency guard: skip if precommit eval already passed for the same code snapshot
    # under the same workflow profile and national execution mode.
    _precommit_ckpt = _matching_checkpoint(v, source_v)
    infra_timeout_retry = bool(
        _precommit_ckpt
        and _precommit_ckpt.get("stage") == "infra_timed_out"
    )
    if infra_timeout_retry:
        authority_error = _infra_timeout_retry_authority_error(
            _precommit_ckpt,
            candidate_dir=candidate_dir,
            code_fingerprint=code_fingerprint,
            version=v,
            source_v=source_v,
        )
        if authority_error:
            return _state_blocked(
                authority_error,
                v,
                source_v,
                _precommit_ckpt,
            )
        # The timeout overlay preserves the already-approved candidate and gate
        # evidence but is not itself a legal predecessor of verified/failed.
        # Restore the exact critic_checked state by checkpoint CAS before any
        # expensive retry so the normal precommit transitions remain valid.
        restored = write_pipeline_checkpoint(
            v,
            source_v,
            "critic_checked",
            expected_checkpoint_revision=_precommit_ckpt.get(
                "checkpoint_revision"
            ),
            expected_checkpoint_stage="infra_timed_out",
            expected_workflow_run_id=_precommit_ckpt.get("workflow_run_id"),
        )
        if not restored:
            return _state_blocked(
                "Failed to restore infra_timed_out checkpoint for exact "
                "precommit retry.",
                v,
                source_v,
                _precommit_ckpt,
            )
        _precommit_ckpt = _matching_checkpoint(v, source_v)
        if (
            not isinstance(_precommit_ckpt, dict)
            or _precommit_ckpt.get("stage") != "critic_checked"
        ):
            return _state_blocked(
                "Infra-timeout checkpoint restoration could not be re-proven.",
                v,
                source_v,
                _precommit_ckpt,
            )
    stored_plan = (
        ((_precommit_ckpt.get("audit_context") or {}).get("precommit_eval_plan"))
        if _precommit_ckpt
        else None
    )
    stored_plan_issues = (
        validate_precommit_plan(
            stored_plan,
            candidate_version=v,
            source_version=source_v,
            profile_id=workflow_profile.profile_id,
            execution_mode=expected_execution_mode,
            evaluation_protocol=evaluation_protocol,
        )
        if national_evaluation and stored_plan is not None
        else []
    )
    current_evaluation_contract = (
        build_evaluation_contract(
            stored_plan,
            candidate_code_fingerprint=code_fingerprint,
        )
        if national_evaluation and stored_plan is not None and not stored_plan_issues
        else None
    )
    profile_refresh = _prepare_official_profile_refresh(_precommit_ckpt, "run_precommit_eval")
    if not profile_refresh.get("ok"):
        return _state_blocked(
            str(profile_refresh.get("error") or "official profile refresh preparation failed"),
            v,
            source_v,
            _precommit_ckpt,
        )
    if _precommit_ckpt and _precommit_ckpt.get("stage") in (
        "verified", "archived"
    ):
        precommit_gate = _precommit_ckpt.get("gate_results", {}).get("precommit_eval", {})
        cached_fingerprint = precommit_gate.get("code_fingerprint")
        cached_profile_id = str(precommit_gate.get("workflow_profile_id") or precommit_gate.get("profile_id") or "")
        cached_execution_mode = str(precommit_gate.get("national_execution_mode") or "")
        cached_contract = precommit_gate.get("precommit_eval_contract")
        if workflow_profile.profile_id == "default":
            cache_profile_matches = (
                cached_profile_id in {"", "default"}
                and cached_execution_mode in {"", expected_execution_mode}
            )
        else:
            cache_profile_matches = (
                cached_profile_id == workflow_profile.profile_id
                and cached_execution_mode == expected_execution_mode
            )
        contract_matches = (
            not national_evaluation
            or (
                current_evaluation_contract is not None
                and not validate_evaluation_contract(
                    cached_contract,
                    stored_plan,
                    candidate_code_fingerprint=code_fingerprint,
                )
            )
        )
        if (
            precommit_gate.get("passed") is True
            and cached_fingerprint == code_fingerprint
            and cache_profile_matches
            and contract_matches
            and runtime_probe_native_template_evidence_matches(precommit_gate)
            # A historical precommit result is only reusable while the whole
            # gate chain remains reusable.  In particular, the quality gate
            # owns the runtime-probe repeatability receipt; checking just the
            # precommit's native-template projection would otherwise allow a
            # direct caller to bypass a now-missing or malformed receipt.
            and _quality_gate_ok(_precommit_ckpt)
            and _review_gate_ok(_precommit_ckpt)
            and _critic_gate_ok(_precommit_ckpt)
        ):
            precommit_gate["idempotent_cache"] = True
            precommit_gate["directive"] = (
                "Precommit eval ALREADY PASSED. Do NOT re-run. "
                "Call commit_bot(version, source_v, strategy, review_approved=true) next."
            )
            return _json_tool_result(precommit_gate)
        if precommit_gate.get("passed") is True:
            log_system_event(
                "pipeline.precommit_cache_stale",
                "warn",
                f"Precommit cache stale for v{v}; cached code/profile does not match active eval requirements.",
                {
                    "version": v,
                    "source_v": source_v,
                    "cached_fingerprint": cached_fingerprint,
                    "current_fingerprint": code_fingerprint,
                    "cached_workflow_profile_id": cached_profile_id,
                    "active_workflow_profile_id": workflow_profile.profile_id,
                    "cached_execution_mode": cached_execution_mode,
                    "active_execution_mode": expected_execution_mode,
                    "cached_native_runtime_template_digest": precommit_gate.get(
                        "native_runtime_template_digest"
                    ),
                    "active_native_runtime_template_digest": (
                        native_template_evidence[
                            "native_runtime_template_digest"
                        ]
                    ),
                    "precommit_plan_issues": stored_plan_issues,
                    "cached_contract_digest": precommit_gate.get("precommit_eval_contract_digest"),
                    "active_contract_digest": (
                        current_evaluation_contract.get("contract_digest")
                        if current_evaluation_contract
                        else None
                    ),
                },
            )

    _set_pipeline_status(f"Pre-commit eval for v{v}")

    candidate_observability = (
        candidate_observability_identity(v, source_v)
        if candidate_observability_identity is not None
        else {
            "candidate_id": candidate_name,
            "parent_ids": [],
            "lineage_kind": "unavailable",
        }
    )
    candidate_id = str(candidate_observability["candidate_id"])
    candidate_parent_ids = list(candidate_observability["parent_ids"])
    candidate_lineage_metrics = {
        key: candidate_observability[key]
        for key in (
            "lineage_kind",
            "numeric_high_water_version",
            "source_artifact_inherited",
        )
        if key in candidate_observability
    }
    if append_candidate_event:
        try:
            append_candidate_event(
                "precommit_started",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage="precommit_eval",
                parent_ids=candidate_parent_ids,
                gate="precommit_eval",
                metrics={
                    **candidate_lineage_metrics,
                    "n_games": n_games,
                },
            )
        except Exception as e:
            log.warning("candidate ledger precommit_started write failed: %s", e)
    blockers = []
    matchups = []

    ckpt = _matching_checkpoint(v, source_v)
    if not _quality_gate_ok(ckpt) or not _review_gate_ok(ckpt) or not _critic_gate_ok(ckpt):
        return _state_blocked(
            "run_precommit_eval requires passing quality/reviewer gates and a completed advisory critic role for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    try:
        from system_strict_bootstrap import is_declared_native_bootstrap

        declared_first_strict = is_declared_native_bootstrap(ckpt)
    except Exception:
        declared_first_strict = False
    planned_system_control = bool(
        stored_plan
        and any(
            str(item.get("authority") or "") == "system_first_strict_control"
            for item in (stored_plan.get("opponents") or [])
            if isinstance(item, dict)
        )
    )
    first_strict_control_receipt = None
    if declared_first_strict:
        from first_strict_control import validate_control_receipt

        first_strict_control_receipt = (
            ((ckpt.get("gate_results") or {}).get("quality") or {}).get(
                "first_strict_control_receipt"
            )
        )
        control_issues = validate_control_receipt(
            first_strict_control_receipt,
            checkpoint=ckpt,
            candidate_version=v,
            source_version=source_v,
        )
        if stored_plan is not None and not planned_system_control:
            control_issues.append(
                "first_strict_control_declared_plan_authority_mismatch"
            )
        if planned_system_control:
            planned_receipt = (
                ((stored_plan.get("opponents") or [{}])[0]).get(
                    "control_receipt"
                )
            )
            if planned_receipt != first_strict_control_receipt:
                control_issues.append(
                    "first_strict_control_quality_plan_receipt_mismatch"
                )
        if control_issues:
            return await _abandon_first_strict_generation({
                "error": "FIRST_STRICT_CONTROL_AUTHORITY_INVALID",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "action": "abandon_generation",
                "failure_class": "control_plane",
                "validation_errors": list(dict.fromkeys(control_issues))[:20],
                "intent": make_intent(
                    "pause",
                    failure_class="control_plane",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="first_strict_control_replan_required",
                ),
                "directive": (
                    "The empty-pool/system-control authority drifted. A newly "
                    "published strict bot or changed control/runtime invalidates "
                    "this plan; abandon it and create a fresh opponent plan."
                ),
            }, reason="first_strict_control_authority_invalid")
    elif planned_system_control:
        return await _abandon_first_strict_generation({
            "error": "UNDECLARED_FIRST_STRICT_CONTROL_PLAN",
            "version": v,
            "source_v": source_v,
            "passed": False,
            "action": "abandon_generation",
            "failure_class": "control_plane",
            "intent": make_intent(
                "pause",
                failure_class="control_plane",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="undeclared_first_strict_control_plan",
            ),
        }, reason="undeclared_first_strict_control_plan")

    if not candidate_entry.exists():
        result = {
            "version": v,
            "source_v": source_v,
            "n_games": n_games,
            "code_fingerprint": code_fingerprint,
            "passed": False,
            "blockers": [{"reason": "candidate_missing", "details": str(candidate_entry)}],
            "opponents": [],
            "matchups": [],
        }
        gate_extra = {k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}}
        _record_gate(v, source_v, "precommit_eval", _gate_payload(v, source_v, False, **gate_extra), stage=None)
        if declared_first_strict:
            return await _abandon_first_strict_generation(
                result,
                reason="first_strict_control_candidate_missing",
            )
        return _json_tool_result(result)

    # compile/smoke already verified by quality gates (required by _quality_gate_ok above)

    if national_evaluation and stored_plan is not None:
        if stored_plan_issues:
            payload = {
                "error": "PRECOMMIT CONTRACT DRIFT: restart the generation from a fresh repository baseline.",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "blockers": [{
                    "reason": "precommit_contract_drift",
                    "details": "; ".join(stored_plan_issues[:12]),
                }],
                "precommit_eval_plan": stored_plan,
                "failure_class": "infrastructure",
                "intent": make_intent(
                    "pause",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="precommit_contract_drift",
                ),
            }
            if declared_first_strict:
                return await _abandon_first_strict_generation(
                    payload,
                    reason="first_strict_control_plan_drift",
                )
            return _json_tool_result(payload)
        opponents = opponents_from_plan(stored_plan)
        frozen_settings = stored_plan.get("settings") or {}
        n_games = int(frozen_settings.get("matches_per_opponent") or n_games)
    elif national_evaluation and declared_first_strict:
        try:
            from first_strict_control import opponent_from_receipt

            opponents = [opponent_from_receipt(first_strict_control_receipt)]
        except Exception as exc:
            return await _abandon_first_strict_generation({
                "error": "FIRST_STRICT_CONTROL_OPPONENT_INVALID",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "action": "abandon_generation",
                "failure_class": "control_plane",
                "message": f"{type(exc).__name__}: {str(exc)[:800]}",
                "intent": make_intent(
                    "pause",
                    failure_class="control_plane",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="first_strict_control_opponent_invalid",
                ),
            }, reason="first_strict_control_opponent_invalid")
    else:
        opponents = _select_precommit_opponents(
            v,
            source_v,
            checkpoint=ckpt,
        )
    # Add crossover parent_b if applicable
    if (
        stored_plan is None
        and not declared_first_strict
        and ckpt
        and ckpt.get("parent2_v")
    ):
        parent2_name = active_bot_name(ckpt["parent2_v"])
        parent2_path = get_bot_dir(parse_bot_version(parent2_name))
        if parent2_path.exists() and not any(o["name"] == parent2_name for o in opponents):
            opponents.append({"name": parent2_name, "reason": "crossover_parent_b"})

    if not opponents:
        blockers.append({"reason": "no_opponents", "details": "No eligible current-epoch native TCP opponents found."})
    all_opponents = list(opponents)  # preserve full list for result reporting

    precommit_plan = stored_plan
    evaluation_contract = current_evaluation_contract
    if national_evaluation and precommit_plan is None and opponents:
        national_hands, national_matches = _national_precommit_shape(workflow_profile, n_games)
        try:
            precommit_plan = create_precommit_plan(
                candidate_version=v,
                source_version=source_v,
                profile_id=workflow_profile.profile_id,
                execution_mode=expected_execution_mode,
                evaluation_protocol=evaluation_protocol,
                opponents=opponents,
                hands_per_match=national_hands,
                matches_per_opponent=national_matches,
                path_resolver=lambda item: (
                    item.get("path")
                    or get_bot_dir(parse_bot_version(item["name"]))
                ),
                require_published_opponents=True,
            )
        except PrecommitEvalContractError as exc:
            payload = {
                "error": f"PRECOMMIT PLAN CREATION FAILED: {exc}",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "blockers": [{
                    "reason": "precommit_plan_creation_failed",
                    "details": str(exc)[:800],
                }],
                "failure_class": "infrastructure",
                "intent": make_intent(
                    "pause",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="precommit_plan_creation_failed",
                ),
            }
            if declared_first_strict:
                return await _abandon_first_strict_generation(
                    payload,
                    reason="first_strict_control_plan_creation_failed",
                )
            return _json_tool_result(payload)
        current_stage = ckpt.get("stage", "critic_checked") if ckpt else "critic_checked"
        if not write_pipeline_checkpoint(
            v,
            source_v,
            current_stage,
            audit_context={"precommit_eval_plan": precommit_plan},
        ):
            if declared_first_strict:
                return await _abandon_first_strict_generation({
                    "error": "Failed to persist immutable precommit evaluation plan.",
                    "version": v,
                    "source_v": source_v,
                    "passed": False,
                    "failure_class": "control_plane",
                }, reason="first_strict_control_plan_persist_failed")
            return _state_blocked(
                "Failed to persist immutable precommit evaluation plan.",
                v,
                source_v,
                ckpt,
            )
        evaluation_contract = build_evaluation_contract(
            precommit_plan,
            candidate_code_fingerprint=code_fingerprint,
        )
        opponents = opponents_from_plan(precommit_plan)
        all_opponents = list(opponents)
        n_games = int((precommit_plan.get("settings") or {}).get("matches_per_opponent") or n_games)

    if national_evaluation and precommit_plan is None:
        payload = {
            "error": "PRECOMMIT PLAN UNAVAILABLE: no immutable opponent set could be created.",
            "version": v,
            "source_v": source_v,
            "passed": False,
            "blockers": blockers or [{
                "reason": "precommit_plan_unavailable",
                "details": "No eligible national opponent was available.",
            }],
            "failure_class": "infrastructure",
            "intent": make_intent(
                "pause",
                failure_class="infrastructure",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="precommit_plan_unavailable",
            ),
        }
        if declared_first_strict:
            return await _abandon_first_strict_generation(
                payload,
                reason="first_strict_control_plan_unavailable",
            )
        return _json_tool_result(payload)

    # A normal battle round gets a new logical attempt.  An interrupted attempt
    # is different: its frozen system-control journal scope (and, explicitly,
    # ``infra_timed_out``) resumes the exact same identity, so a completed match
    # can be recovered instead of relaunched under a new revision/attempt scope.
    attempt_ckpt = _matching_checkpoint(v, source_v) if opponents else ckpt
    precommit_attempt = (
        int((attempt_ckpt or {}).get("precommit_attempt", 0) or 0)
    )
    system_control_plan = any(
        str(item.get("authority") or "") == "system_first_strict_control"
        for item in opponents
    )
    control_execution_scope = None
    execution_ckpt = attempt_ckpt
    resume_control_attempt = False
    if opponents and system_control_plan and precommit_attempt >= 1:
        frozen_scope = (
            ((attempt_ckpt or {}).get("audit_context") or {}).get(
                _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
            )
        )
        control_execution_scope, scope_error = (
            _validate_first_strict_control_execution_scope(
                frozen_scope,
                v=v,
                candidate_name=candidate_name,
                code_fingerprint=code_fingerprint,
                opponents=opponents,
                precommit_plan=precommit_plan,
                evaluation_contract=evaluation_contract,
                workflow_run_id=str(
                    (attempt_ckpt or {}).get("workflow_run_id") or ""
                ),
                precommit_attempt=precommit_attempt,
            )
        )
        if scope_error:
            return _state_blocked(
                scope_error,
                v,
                source_v,
                attempt_ckpt,
            )
        # A persisted scope with no terminal precommit stage is the durable
        # in-flight marker.  Reuse it after process cancellation/crash as well
        # as after an explicit infra_timed_out overlay.
        resume_control_attempt = True
    if opponents and not infra_timeout_retry and not resume_control_attempt:
        current_stage = (
            (attempt_ckpt or {}).get("stage", "critic_checked")
        )
        precommit_attempt += 1
        audit_context_update = None
        predicted_scope = None
        if system_control_plan:
            current_revision = (attempt_ckpt or {}).get(
                "checkpoint_revision"
            )
            workflow_run_id = str(
                (attempt_ckpt or {}).get("workflow_run_id") or ""
            )
            if (
                type(current_revision) is not int
                or current_revision < 1
                or not workflow_run_id
            ):
                return _state_blocked(
                    "First-strict precommit cannot freeze an invalid checkpoint "
                    "execution identity.",
                    v,
                    source_v,
                    attempt_ckpt,
                )
            predicted_scope = _build_first_strict_control_execution_scope(
                v=v,
                candidate_name=candidate_name,
                code_fingerprint=code_fingerprint,
                opponents=opponents,
                precommit_plan=precommit_plan,
                evaluation_contract=evaluation_contract,
                workflow_run_id=workflow_run_id,
                checkpoint_revision=current_revision + 1,
                precommit_attempt=precommit_attempt,
            )
            audit_context_update = {
                _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY: predicted_scope,
            }
        persisted_attempt = write_pipeline_checkpoint(
            v,
            source_v,
            current_stage,
            precommit_attempt=precommit_attempt,
            audit_context=audit_context_update,
            expected_checkpoint_revision=(
                (attempt_ckpt or {}).get("checkpoint_revision")
            ),
            expected_checkpoint_stage=(attempt_ckpt or {}).get("stage"),
            expected_workflow_run_id=(
                (attempt_ckpt or {}).get("workflow_run_id")
            ),
        )
        if not persisted_attempt:
            return _state_blocked(
                "Failed to persist the exact precommit attempt identity.",
                v,
                source_v,
                attempt_ckpt,
            )
        execution_ckpt = _matching_checkpoint(v, source_v)
        if system_control_plan:
            stored_scope = (
                ((execution_ckpt or {}).get("audit_context") or {}).get(
                    _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
                )
            )
            if (
                not isinstance(execution_ckpt, dict)
                or execution_ckpt.get("checkpoint_revision")
                != predicted_scope["checkpoint_revision"]
                or stored_scope != predicted_scope
            ):
                return _state_blocked(
                    "First-strict precommit execution identity persistence "
                    "could not be re-proven.",
                    v,
                    source_v,
                    execution_ckpt,
                )
            control_execution_scope = predicted_scope

    if national_evaluation:
        return await _run_national_precommit_attempt(
            precommit_shutdown_token,
            v=v,
            source_v=source_v,
            requested_n_games=requested,
            effective_n_games=n_games,
            candidate_name=candidate_name,
            parent_name=parent_name,
            candidate_entry=candidate_entry,
            code_fingerprint=code_fingerprint,
            workflow_profile=workflow_profile,
            candidate_id=candidate_id,
            opponents=opponents,
            all_opponents=all_opponents,
            precommit_attempt=precommit_attempt,
            initial_blockers=blockers,
            started_at=_t0,
            precommit_plan=precommit_plan,
            evaluation_contract=evaluation_contract,
            workflow_run_id=str(
                (execution_ckpt or {}).get("workflow_run_id") or ""
            ),
            checkpoint_revision=int(
                (execution_ckpt or {}).get("checkpoint_revision") or 0
            ),
            control_execution_scope=control_execution_scope,
        )

# ──────────────────────────────────────────────
# Inline Eval
# ──────────────────────────────────────────────

@tool("run_inline_eval", "Run a non-authoritative diagnostic evaluation without modifying Glicko/H2H. The rating daemon is the only authoritative rating writer.", {"version": int, "n_games": int})
async def run_inline_eval(args):
    _inline_eval_start = time.time()
    v, _source_v = _resolve_version_args(args)
    if v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing version and no active pipeline checkpoint"})}]}
    v = int(v)
    n_games = args.get("n_games", 5)
    bot_name = active_bot_name(v)

    _set_pipeline_status(f"Running inline eval for v{v}")

    bot_dir = get_bot_dir(v)

    from workflow_profiles import get_workflow_profile

    profile = get_workflow_profile()
    if getattr(profile, "national_execution_mode", None) != "native_tcp":
        return _json_tool_result({"error": "only native_tcp inline evaluation is supported"})
    expected_entry = bot_dir / "national_bot.py"
    if not expected_entry.exists():
        return {"content": [{"type": "text", "text": json.dumps({
            "error": f"Bot v{v} entry not found: {expected_entry.name}"
        })}]}

    # Guard: refuse to run while daemon is active (read-modify-write race on ratings)
    from daemon_management import daemon_proc, _daemon_lock
    with _daemon_lock:
        _dp = daemon_proc
    if _dp is not None and _dp.poll() is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Daemon is running. Stop it first with stop_daemon to avoid ratings race condition."})}]}

    active_bots = get_active_bots()
    opponents = [b for b in active_bots if b != bot_name]

    if getattr(profile, "national_execution_mode", None) == "native_tcp":
        from national_native import run_native_acceptance_for_candidate
        from evolution_infra import RESULTS_DIR
        from evaluation_data_identity import current_evaluation_digest
        from datetime import datetime as _dt

        acceptance = await run_native_acceptance_for_candidate(
            bot_dir,
            opponent_tokens=[get_bot_dir(int(name.removeprefix("national_v"))) for name in opponents],
            hands=70,
            max_opponents=max(1, len(opponents)),
        )
        payload = acceptance.model_dump()
        payload.update({
            "authoritative": False,
            "ratings_updated": False,
            "h2h_updated": False,
            "evaluation_identity_digest": current_evaluation_digest(RESULTS_DIR),
            "source": "inline_native_diagnostic",
        })
        diagnostic_dir = RESULTS_DIR / "inline_eval_diagnostics"
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        diagnostic_path = diagnostic_dir / f"v{v}-{_dt.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        diagnostic_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["diagnostic_path"] = str(diagnostic_path)
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}]}
