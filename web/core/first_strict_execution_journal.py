"""Fenced native-match execution receipts for the first strict control gate.

The first strict publication is the sole precommit path that may use a
system-owned, non-published opponent.  Its eight samples therefore use their
own :class:`workflow_kernel.WorkflowStore` authority stream.  The parent
runner requests and leases one effect before launching each native match, then
atomically completes that fenced effect with a ``NativeMatchRecorded`` event.
The complete canonical replay is committed inside the same SQLite effect
transaction and then projected to a read-only content-addressed file.  Keeping
the canonical body in the fenced effect is intentional: a process death after
the real 70-hand runner returns can recover the already-observed terminal
result instead of paying for (or accidentally admitting) a second match.

SQLite WAL, effect leases, immutable content addressing, and replayed payload
digests provide crash consistency and operational tamper detection.  This is
not a same-UID security boundary: a process running as the operator can still
rewrite both the runtime database and replay store.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid
from typing import Any

from bot_artifact import canonical_digest
from workflow_kernel import (
    WorkflowDeadlineExceeded,
    WorkflowStore,
    canonical_json,
)


EXECUTION_DEFINITION_VERSION = 3
EXECUTION_EFFECT_KIND = "first_strict_native_70_hand_match"
EXECUTION_EVENT_TYPE = "NativeMatchRecorded"
RECEIPT_REF_SCHEMA_VERSION = 1
RECEIPT_REF_KIND = "first-strict-native-match-execution-ref"
CONTROL_EXECUTION_ROOT = (
    Path(__file__).resolve().parent
    / "results"
    / "system_controls"
    / "first_strict_execution"
)

_HEX = frozenset("0123456789abcdef")
_SCOPE_FIELDS = (
    "workflow_run_id",
    "checkpoint_revision",
    "candidate_version",
    "candidate_label",
    "candidate_artifact_hash",
    "control_id",
    "control_artifact_hash",
    "control_receipt_digest",
    "precommit_plan_digest",
    "evaluation_contract_digest",
    "native_match_timing_plan_digest",
    "precommit_attempt",
)

_PENDING_EXECUTION_FIELDS = {
    "state",
    "pending",
    "recovered",
    "authority_run_id",
    "effect_id",
    "match_run_id",
    "input_payload",
    "lease_epoch",
    "lease_until",
    "attempt",
    "max_attempts",
}


class FirstStrictExecutionJournalError(RuntimeError):
    """Raised when a control match cannot enter the execution authority."""


class FirstStrictExecutionPending(FirstStrictExecutionJournalError):
    """A matching first-strict match is still owned by a live fenced lease.

    Callers should preserve the existing checkpoint scope and gate evidence,
    wait for the recorded lease boundary, then make the exact same request.
    This is deliberately distinct from a candidate regression or a malformed
    journal record: starting another subprocess pair while the lease is live
    would create duplicate non-replayable physical evidence.
    """

    def __init__(self, pending: dict[str, Any]):
        self.pending = normalize_pending_control_execution(pending)
        super().__init__("first_strict_execution_lease_active")


_ACTIVE_COMPLETION_DEADLINE_MONOTONIC: ContextVar[float | None] = ContextVar(
    "first_strict_completion_deadline_monotonic",
    default=None,
)


def _normalize_completion_deadline(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completion_deadline_invalid"
        )
    try:
        deadline = float(value)
    except (TypeError, ValueError) as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completion_deadline_invalid"
        ) from exc
    if not math.isfinite(deadline):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completion_deadline_invalid"
        )
    return deadline


def _effective_completion_deadline(value: Any) -> float | None:
    explicit = _normalize_completion_deadline(value)
    active = _ACTIVE_COMPLETION_DEADLINE_MONOTONIC.get()
    if explicit is None:
        return active
    if active is None:
        return explicit
    # Nested callers may tighten but can never widen the runner's original
    # post-execution authority boundary.
    return min(explicit, active)


def _require_completion_deadline(
    deadline_monotonic: float | None,
    phase: str,
) -> None:
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise FirstStrictExecutionJournalError(
            f"first_strict_execution_completion_deadline_exceeded:{phase}"
        )


@contextmanager
def control_execution_completion_deadline(deadline_monotonic: Any):
    """Bind the runner's absolute deadline across compatible wrappers.

    Some orchestrator tests and operator instrumentation wrap
    :func:`complete_control_execution` using its historical two-argument
    signature.  A context-local boundary lets those wrappers remain compatible
    without losing the original monotonic cutoff.  Direct callers may still
    pass ``deadline_monotonic=`` explicitly.
    """

    deadline = _effective_completion_deadline(deadline_monotonic)
    _require_completion_deadline(deadline, "scope_entry")
    token = _ACTIVE_COMPLETION_DEADLINE_MONOTONIC.set(deadline)
    try:
        yield deadline
    finally:
        _ACTIVE_COMPLETION_DEADLINE_MONOTONIC.reset(token)


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def normalize_execution_scope(scope: Any) -> dict[str, Any]:
    """Return the exact checkpoint/plan/artifact fence for one authority stream."""

    if not isinstance(scope, dict):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_scope_missing"
        )
    normalized = {field: scope.get(field) for field in _SCOPE_FIELDS}
    if not isinstance(normalized["workflow_run_id"], str) or not normalized[
        "workflow_run_id"
    ].strip():
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_workflow_run_id_invalid"
        )
    for field, minimum in (
        ("checkpoint_revision", 1),
        ("candidate_version", 1),
        ("precommit_attempt", 1),
    ):
        if not _plain_int(normalized[field]) or int(normalized[field]) < minimum:
            raise FirstStrictExecutionJournalError(
                f"first_strict_execution_{field}_invalid"
            )
    if not isinstance(normalized["candidate_label"], str) or not normalized[
        "candidate_label"
    ].strip():
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_candidate_label_invalid"
        )
    if normalized["control_id"] != "first_strict_control_v1":
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_control_id_invalid"
        )
    for field in (
        "candidate_artifact_hash",
        "control_artifact_hash",
        "control_receipt_digest",
        "precommit_plan_digest",
        "evaluation_contract_digest",
        "native_match_timing_plan_digest",
    ):
        if not _valid_digest(normalized[field]):
            raise FirstStrictExecutionJournalError(
                f"first_strict_execution_{field}_invalid"
            )
    normalized["workflow_run_id"] = normalized["workflow_run_id"].strip()
    normalized["candidate_label"] = normalized["candidate_label"].strip()
    return normalized


def execution_scope_digest(scope: Any) -> str:
    return canonical_digest(normalize_execution_scope(scope))


def _authority_run_id(scope: Any) -> str:
    return f"first-strict-control:{execution_scope_digest(scope)}"


def _validated_execution_ticket(ticket: Any) -> dict[str, Any]:
    """Validate the exact ticket shape even on idempotent completed reads."""

    if not isinstance(ticket, dict) or set(ticket) != {
        "authority_run_id",
        "effect_id",
        "lease_epoch",
        "match_run_id",
        "input_payload",
    }:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_ticket_invalid"
        )
    if not _plain_int(ticket.get("lease_epoch")) or int(
        ticket["lease_epoch"]
    ) < 1:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_ticket_lease_invalid"
        )
    payload = ticket.get("input_payload")
    if not isinstance(payload, dict) or set(payload) != {
        "scope",
        "scope_digest",
        "repeat",
        "deck_seed_base",
        "bot_seed_base",
        "hands",
        "timing_plan",
        "timing_plan_digest",
        "match_run_id",
    }:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_ticket_input_invalid"
        )
    scope = normalize_execution_scope(payload.get("scope"))
    scope_digest = execution_scope_digest(scope)
    repeat = payload.get("repeat")
    deck_seed_base = payload.get("deck_seed_base")
    bot_seed_base = payload.get("bot_seed_base")
    if (
        payload.get("scope") != scope
        or payload.get("scope_digest") != scope_digest
        or not _plain_int(repeat)
        or not 1 <= int(repeat) <= 8
        or not _plain_int(deck_seed_base)
        or not _plain_int(bot_seed_base)
        or int(bot_seed_base) != int(deck_seed_base) + 1_000_000_000
        or payload.get("hands") != 70
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_ticket_input_binding_invalid"
        )
    try:
        from national_native import (
            LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            require_native_match_timing_plan,
        )

        timing_plan = require_native_match_timing_plan(
            payload.get("timing_plan"),
            hands=70,
            requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
        )
    except Exception as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_ticket_timing_plan_invalid"
        ) from exc
    if (
        payload.get("timing_plan_digest") != timing_plan.digest()
        or scope.get("native_match_timing_plan_digest") != timing_plan.digest()
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_ticket_timing_plan_binding_invalid"
        )
    match_identity = {
        "scope": scope,
        "scope_digest": scope_digest,
        "repeat": int(repeat),
        "deck_seed_base": int(deck_seed_base),
        "bot_seed_base": int(bot_seed_base),
        "hands": 70,
        "timing_plan": timing_plan.snapshot(),
        "timing_plan_digest": timing_plan.digest(),
    }
    match_run_id = "first-strict-native:" + canonical_digest(match_identity)
    authority_run_id = f"first-strict-control:{scope_digest}"
    effect_id = f"{authority_run_id}:repeat-{int(repeat)}"
    expected = {
        "authority_run_id": authority_run_id,
        "effect_id": effect_id,
        "lease_epoch": int(ticket["lease_epoch"]),
        "match_run_id": match_run_id,
        "input_payload": {**match_identity, "match_run_id": match_run_id},
    }
    if ticket != expected:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_ticket_binding_invalid"
        )
    return expected


def normalize_pending_control_execution(
    pending: Any,
    *,
    expected_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the non-executable pending form returned for a live lease.

    A pending result is intentionally *not* a runner ticket.  It carries the
    canonical input and the observed live lease boundary so callers can retain
    the exact scope/receipt/critic evidence without accidentally passing it to
    the native subprocess launcher.  Treat malformed or mismatched values as
    a journal error rather than an infrastructure wait signal.
    """

    if not isinstance(pending, dict) or set(pending) != _PENDING_EXECUTION_FIELDS:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_shape_invalid"
        )
    if (
        pending.get("state") != "pending"
        or pending.get("pending") is not True
        or pending.get("recovered") is not False
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_state_invalid"
        )
    if not _plain_int(pending.get("lease_epoch")) or int(
        pending["lease_epoch"]
    ) < 1:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_lease_invalid"
        )
    for field in ("attempt", "max_attempts"):
        if not _plain_int(pending.get(field)) or int(pending[field]) < 1:
            raise FirstStrictExecutionJournalError(
                f"first_strict_execution_pending_{field}_invalid"
            )
    if int(pending["attempt"]) > int(pending["max_attempts"]):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_attempt_invalid"
        )
    lease_until = pending.get("lease_until")
    if (
        isinstance(lease_until, bool)
        or not isinstance(lease_until, (int, float))
        or not math.isfinite(float(lease_until))
        or float(lease_until) <= 0.0
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_lease_until_invalid"
        )
    canonical_ticket = _validated_execution_ticket({
        "authority_run_id": pending.get("authority_run_id"),
        "effect_id": pending.get("effect_id"),
        "lease_epoch": int(pending["lease_epoch"]),
        "match_run_id": pending.get("match_run_id"),
        "input_payload": pending.get("input_payload"),
    })
    scope = canonical_ticket["input_payload"]["scope"]
    if expected_scope is not None and scope != normalize_execution_scope(
        expected_scope
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_scope_mismatch"
        )
    return {
        "state": "pending",
        "pending": True,
        "recovered": False,
        "authority_run_id": canonical_ticket["authority_run_id"],
        "effect_id": canonical_ticket["effect_id"],
        "match_run_id": canonical_ticket["match_run_id"],
        "input_payload": canonical_ticket["input_payload"],
        "lease_epoch": canonical_ticket["lease_epoch"],
        "lease_until": float(lease_until),
        "attempt": int(pending["attempt"]),
        "max_attempts": int(pending["max_attempts"]),
    }


def is_pending_control_execution(
    value: Any,
    *,
    expected_scope: dict[str, Any] | None = None,
    now: float | None = None,
) -> bool:
    """Return ``True`` only for a fully bound *currently live* lease form."""

    try:
        read_pending_control_execution(
            value,
            expected_scope=expected_scope,
            now=now,
        )
    except FirstStrictExecutionJournalError:
        return False
    return True


def read_pending_control_execution(
    pending: Any,
    *,
    expected_scope: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Re-prove a pending payload against the durable active effect lease.

    The payload alone is not authority to skip a gate failure.  It must still
    name the exact running effect, epoch, input, attempt and unexpired lease in
    the local journal.  This closes the otherwise dangerous path where a stale
    or forged ``pending`` result could indefinitely suppress recovery.
    """

    normalized = normalize_pending_control_execution(
        pending,
        expected_scope=expected_scope,
    )
    try:
        current_time = float(now if now is not None else time.time())
    except (TypeError, ValueError) as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_check_time_invalid"
        ) from exc
    if not math.isfinite(current_time):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_check_time_invalid"
        )
    effect = _store().effect(normalized["effect_id"])
    if not effect:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_effect_missing"
        )
    if (
        effect.get("run_id") != normalized["authority_run_id"]
        or effect.get("kind") != EXECUTION_EFFECT_KIND
        or effect.get("input_payload") != normalized["input_payload"]
        or effect.get("status") != "running"
        or int(effect.get("lease_epoch") or 0) != normalized["lease_epoch"]
        or int(effect.get("attempt") or 0) != normalized["attempt"]
        or int(effect.get("max_attempts") or 0) != normalized["max_attempts"]
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_effect_binding_invalid"
        )
    lease_until = effect.get("lease_until")
    if (
        isinstance(lease_until, bool)
        or not isinstance(lease_until, (int, float))
        or not math.isfinite(float(lease_until))
        or float(lease_until) != normalized["lease_until"]
        or float(lease_until) <= current_time
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_pending_lease_not_live"
        )
    return normalized


def _store(*, deadline_monotonic: float | None = None) -> WorkflowStore:
    return WorkflowStore(
        CONTROL_EXECUTION_ROOT / "events.sqlite3",
        deadline_monotonic=deadline_monotonic,
    )


def _replay_root() -> Path:
    return CONTROL_EXECUTION_ROOT / "replays"


def _terminal_execution_issues(
    execution: Any,
    *,
    deck_seed_base: int,
    bot_seed_base: int,
    timing_plan: Any,
) -> tuple[list[str], dict[str, Any]]:
    """Validate and summarize a complete local 70-hand TCP replay."""

    if not isinstance(execution, dict):
        return ["first_strict_execution_payload_invalid"], {}
    issues: list[str] = []
    if execution.get("execution_mode") != "native_tcp":
        issues.append("first_strict_execution_mode_invalid")
    if execution.get("hands_requested") != 70:
        issues.append("first_strict_execution_hands_requested_invalid")
    if execution.get("hands_played") != 70:
        issues.append("first_strict_execution_hands_played_invalid")
    if execution.get("deck_seed_base") != deck_seed_base:
        issues.append("first_strict_execution_deck_seed_mismatch")
    if execution.get("bot_seed_base") != bot_seed_base:
        issues.append("first_strict_execution_bot_seed_mismatch")
    if execution.get("passed_compliance") is not True:
        issues.append("first_strict_execution_compliance_invalid")
    if execution.get("issues") != []:
        issues.append("first_strict_execution_issues_not_empty")
    try:
        from national_native import validate_native_match_timing_evidence

        timing_issues = validate_native_match_timing_evidence(
            execution,
            timing_plan=timing_plan,
        )
    except Exception:
        timing_issues = ["native_match_timing_evidence_validator_failed"]
    issues.extend(
        "first_strict_execution_" + issue for issue in timing_issues
    )

    settlements = execution.get("settlements")
    hand_records = execution.get("hand_records")
    events = execution.get("events")
    if not isinstance(settlements, list) or len(settlements) != 70:
        issues.append("first_strict_execution_settlement_count_invalid")
        settlements = []
    if not isinstance(hand_records, list) or len(hand_records) != 70:
        issues.append("first_strict_execution_hand_record_count_invalid")
        hand_records = []
    if not isinstance(events, list) or not events:
        issues.append("first_strict_execution_events_missing")
        events = []

    expected_hands = list(range(1, 71))
    settlement_hands = [
        row.get("hand") if isinstance(row, dict) else None
        for row in settlements
    ]
    record_hands = [
        row.get("hand") if isinstance(row, dict) else None
        for row in hand_records
    ]
    if settlement_hands != expected_hands:
        issues.append("first_strict_execution_settlement_sequence_invalid")
    if record_hands != expected_hands:
        issues.append("first_strict_execution_hand_record_sequence_invalid")

    earnings_a: list[int] = []
    for index, (settlement, record) in enumerate(
        zip(settlements, hand_records), start=1
    ):
        if not isinstance(settlement, dict) or not isinstance(record, dict):
            issues.append(f"first_strict_execution_hand_{index}_record_invalid")
            continue
        earnings = settlement.get("earnings")
        record_settlement = record.get("settlement")
        record_earnings = (
            record_settlement.get("earnings")
            if isinstance(record_settlement, dict)
            else None
        )
        if (
            not isinstance(earnings, list)
            or len(earnings) != 2
            or any(not _plain_int(value) for value in earnings)
            or sum(earnings) != 0
        ):
            issues.append(f"first_strict_execution_hand_{index}_earnings_invalid")
            continue
        if record_earnings != earnings:
            issues.append(
                f"first_strict_execution_hand_{index}_settlement_projection_mismatch"
            )
        earnings_a.append(int(earnings[0]))

    settle_events = [
        event for event in events
        if isinstance(event, dict) and event.get("type") == "settle"
    ]
    event_hands = [event.get("hand") for event in settle_events]
    if event_hands != expected_hands:
        issues.append("first_strict_execution_terminal_event_sequence_invalid")
    if len(settle_events) == 70 and len(settlements) == 70:
        for index, (event, settlement) in enumerate(
            zip(settle_events, settlements), start=1
        ):
            event_projection = {
                field: event.get(field)
                for field in (
                    "hand",
                    "earnings",
                    "pot",
                    "is_showdown",
                    "winner_idx",
                )
            }
            event_projection["reason"] = event.get("reason", "")
            if any(
                event_projection.get(field) != settlement.get(field)
                for field in event_projection
            ):
                issues.append(
                    f"first_strict_execution_hand_{index}_event_projection_mismatch"
                )

    total_a = sum(earnings_a)
    if execution.get("net_chips_a") != total_a:
        issues.append("first_strict_execution_net_chips_a_invalid")
    if execution.get("net_chips_b") != -total_a:
        issues.append("first_strict_execution_net_chips_b_invalid")
    replay = {
        "events": events,
        "hand_records": hand_records,
        "settlements": settlements,
    }
    proof = {
        "hands_requested": execution.get("hands_requested"),
        "hands_played": execution.get("hands_played"),
        "settlement_count": len(settlements),
        "hand_record_count": len(hand_records),
        "terminal_event_count": len(settle_events),
        "first_hand": settlement_hands[0] if settlement_hands else None,
        "last_hand": settlement_hands[-1] if settlement_hands else None,
        "net_chips_a": execution.get("net_chips_a"),
        "net_chips_b": execution.get("net_chips_b"),
        "native_match_timing_plan_digest": execution.get(
            "native_match_timing_plan_digest"
        ),
        "native_match_timeout_phase": execution.get("native_match_timeout_phase"),
        "native_terminal_abort": execution.get("native_terminal_abort"),
        "replay_content_digest": canonical_digest(replay),
        "events_digest": canonical_digest({"events": events}),
        "hand_records_digest": canonical_digest({"hand_records": hand_records}),
    }
    return list(dict.fromkeys(issues)), proof


def _write_replay(execution: dict[str, Any]) -> tuple[str, Path]:
    encoded = canonical_json(execution).encode("utf-8")
    digest = canonical_digest(execution)
    root = _replay_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f"{digest}.json"
    if target.exists():
        if target.is_symlink() or target.read_bytes() != encoded:
            raise FirstStrictExecutionJournalError(
                "first_strict_execution_replay_digest_collision"
            )
        return digest, target
    temporary = root / f".{digest}.tmp-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(encoded)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("first strict replay short write")
            offset += int(written)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, target)
    except FileExistsError:
        if target.is_symlink() or target.read_bytes() != encoded:
            raise FirstStrictExecutionJournalError(
                "first_strict_execution_replay_digest_collision"
            )
    finally:
        temporary.unlink(missing_ok=True)
    target.chmod(0o400)
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return digest, target


def _read_replay(digest: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if not _valid_digest(digest):
        return None, ["first_strict_execution_replay_digest_invalid"]
    path = _replay_root() / f"{digest}.json"
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None, ["first_strict_execution_replay_node_invalid"]
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, ["first_strict_execution_replay_missing"]
    except Exception as exc:
        return None, [
            f"first_strict_execution_replay_read_error:{type(exc).__name__}"
        ]
    if not isinstance(value, dict) or canonical_digest(value) != digest:
        return None, ["first_strict_execution_replay_content_mismatch"]
    return value, []


def _completed_execution_reference(
    store: WorkflowStore,
    *,
    authority_run_id: str,
    effect_id: str,
    input_payload: dict[str, Any],
    deadline_monotonic: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconstruct and fully replay-validate one terminal effect reference."""

    effect = store.effect(
        effect_id,
        deadline_monotonic=deadline_monotonic,
    )
    if (
        effect.get("status") != "completed"
        or effect.get("kind") != EXECUTION_EFFECT_KIND
        or effect.get("run_id") != authority_run_id
        or effect.get("input_payload") != input_payload
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completed_effect_binding_invalid"
        )
    result_payload = effect.get("result_payload")
    if not isinstance(result_payload, dict):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completed_effect_result_missing"
        )
    recorded = [
        event
        for event in store.events(
            authority_run_id,
            deadline_monotonic=deadline_monotonic,
        )
        if event.event_type == EXECUTION_EVENT_TYPE
        and event.payload.get("receipt_id") == result_payload.get("receipt_id")
    ]
    if len(recorded) != 1:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completed_event_invalid"
        )
    event = recorded[0]
    reference = {
        "schema_version": RECEIPT_REF_SCHEMA_VERSION,
        "kind": RECEIPT_REF_KIND,
        "authority_run_id": authority_run_id,
        "effect_id": effect_id,
        "match_run_id": result_payload.get("match_run_id"),
        "receipt_id": result_payload.get("receipt_id"),
        "scope_digest": result_payload.get("scope_digest"),
        "result_digest": effect.get("result_digest"),
        "recorded_seq": event.seq,
        "recorded_payload_digest": event.payload_digest,
        "replay_digest": result_payload.get("replay_digest"),
        "receipt_chain_digest": result_payload.get("receipt_chain_digest"),
    }
    evidence, issues = read_control_execution_receipt(
        reference,
        expected_scope=input_payload.get("scope"),
        deadline_monotonic=deadline_monotonic,
    )
    if issues or evidence is None:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completed_replay_invalid:"
            + ";".join(issues[:12])
        )
    if evidence.get("input") != input_payload or not isinstance(
        evidence.get("execution"), dict
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completed_replay_binding_invalid"
        )
    return reference, deepcopy(evidence["execution"])


def begin_control_execution(
    *,
    scope: dict[str, Any],
    repeat: int,
    deck_seed_base: int,
    bot_seed_base: int,
    timing_plan: Any,
    claim_now: float | None = None,
) -> dict[str, Any]:
    """Fence one match effect before the parent runner launches subprocesses."""

    normalized_scope = normalize_execution_scope(scope)
    if not _plain_int(repeat) or not 1 <= int(repeat) <= 8:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_repeat_invalid"
        )
    if not _plain_int(deck_seed_base) or not _plain_int(bot_seed_base):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_seed_invalid"
        )
    if int(bot_seed_base) != int(deck_seed_base) + 1_000_000_000:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_seed_relation_invalid"
        )
    try:
        from national_native import (
            LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            require_native_match_timing_plan,
        )

        frozen_timing_plan = require_native_match_timing_plan(
            timing_plan,
            hands=70,
            requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
        )
    except Exception as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_timing_plan_invalid"
        ) from exc
    if (
        normalized_scope.get("native_match_timing_plan_digest")
        != frozen_timing_plan.digest()
    ):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_scope_timing_plan_mismatch"
        )
    store = _store()
    authority_run_id = _authority_run_id(normalized_scope)
    store.ensure_instance(
        authority_run_id,
        definition_version=EXECUTION_DEFINITION_VERSION,
    )
    effect_id = f"{authority_run_id}:repeat-{int(repeat)}"
    match_identity = {
        "scope": normalized_scope,
        "scope_digest": execution_scope_digest(normalized_scope),
        "repeat": int(repeat),
        "deck_seed_base": int(deck_seed_base),
        "bot_seed_base": int(bot_seed_base),
        "hands": 70,
        "timing_plan": frozen_timing_plan.snapshot(),
        "timing_plan_digest": frozen_timing_plan.digest(),
    }
    # A process may die after the match but before fenced completion.  The same
    # frozen sample must be reclaimable after lease expiry without changing the
    # effect input, while a different seed/scope must conflict.
    match_run_id = "first-strict-native:" + canonical_digest(match_identity)
    input_payload = {**match_identity, "match_run_id": match_run_id}
    completed_effect = False
    pending_execution = None
    try:
        claim_time = float(claim_now if claim_now is not None else time.time())
    except (TypeError, ValueError) as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_claim_time_invalid"
        ) from exc
    if not math.isfinite(claim_time):
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_claim_time_invalid"
        )
    with store.command_lock(authority_run_id, blocking=True):
        instance = store.instance(authority_run_id)
        effect = store.request_effect(
            run_id=authority_run_id,
            effect_id=effect_id,
            kind=EXECUTION_EFFECT_KIND,
            input_payload=input_payload,
            causation_id=f"control-match-requested:{effect_id}",
            max_attempts=3,
            expected_version=int(instance.get("stream_version") or 0),
        )
        completed_effect = effect.get("status") == "completed"
        if not completed_effect:
            lease_until = effect.get("lease_until")
            if (
                effect.get("status") == "running"
                and isinstance(lease_until, (int, float))
                and not isinstance(lease_until, bool)
                and math.isfinite(float(lease_until))
                and float(lease_until) > claim_time
            ):
                # The matching effect is still actively owned.  Do not turn a
                # normal cancellation/retry into a failed gate or race another
                # subprocess pair against the original physical match.
                pending_execution = {
                    "state": "pending",
                    "pending": True,
                    "recovered": False,
                    "authority_run_id": authority_run_id,
                    "effect_id": effect_id,
                    "match_run_id": match_run_id,
                    "input_payload": input_payload,
                    "lease_epoch": int(effect.get("lease_epoch") or 0),
                    "lease_until": float(lease_until),
                    "attempt": int(effect.get("attempt") or 0),
                    "max_attempts": int(effect.get("max_attempts") or 0),
                }
            else:
                lease = store.claim_effect(
                    effect_id,
                    owner=f"parent:{os.getpid()}:{uuid.uuid4().hex}",
                    lease_seconds=max(
                        1.0,
                        frozen_timing_plan.first_strict_lease_timeout_us / 1_000_000.0,
                    ),
                    now=claim_time,
                )
    if completed_effect:
        reference, execution = _completed_execution_reference(
            store,
            authority_run_id=authority_run_id,
            effect_id=effect_id,
            input_payload=input_payload,
        )
        return {
            "state": "recovered",
            "pending": False,
            "authority_run_id": authority_run_id,
            "effect_id": effect_id,
            "match_run_id": match_run_id,
            "input_payload": input_payload,
            "recovered": True,
            "execution_receipt": reference,
            "execution": execution,
        }
    if pending_execution is not None:
        return normalize_pending_control_execution(
            pending_execution,
            expected_scope=normalized_scope,
        )
    return {
        "authority_run_id": authority_run_id,
        "effect_id": effect_id,
        "lease_epoch": lease.lease_epoch,
        "match_run_id": match_run_id,
        "input_payload": input_payload,
    }


def complete_control_execution(
    ticket: Any,
    *,
    execution: dict[str, Any],
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Store the replay and atomically complete the leased match effect."""

    deadline = _effective_completion_deadline(deadline_monotonic)
    _require_completion_deadline(deadline, "entry")
    ticket = _validated_execution_ticket(ticket)
    input_payload = ticket["input_payload"]
    try:
        from national_native import (
            LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            require_native_match_timing_plan,
        )

        frozen_timing_plan = require_native_match_timing_plan(
            input_payload.get("timing_plan"),
            hands=70,
            requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
        )
    except Exception as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completion_timing_plan_invalid"
        ) from exc
    _require_completion_deadline(deadline, "timing_plan_validation")
    try:
        store = _store(deadline_monotonic=deadline)
    except WorkflowDeadlineExceeded as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completion_deadline_exceeded:store_open"
        ) from exc
    authority_run_id = str(ticket.get("authority_run_id") or "")
    effect_id = str(ticket.get("effect_id") or "")

    # The runner now completes the authority before returning its dictionary.
    # The outer precommit layer may therefore call this function once more to
    # obtain the compact reference.  That replay is safe only when the already
    # completed effect has exactly the same frozen input and execution bytes.
    try:
        existing = store.effect(
            effect_id,
            deadline_monotonic=deadline,
        )
    except WorkflowDeadlineExceeded as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completion_deadline_exceeded:effect_read"
        ) from exc
    _require_completion_deadline(deadline, "effect_read")
    if existing.get("status") == "completed":
        if (
            existing.get("run_id") != authority_run_id
            or existing.get("kind") != EXECUTION_EFFECT_KIND
            or existing.get("input_payload") != input_payload
            or int(existing.get("lease_epoch") or 0) != ticket["lease_epoch"]
        ):
            raise FirstStrictExecutionJournalError(
                "first_strict_execution_completed_effect_binding_invalid"
            )
        reference, recovered_execution = _completed_execution_reference(
            store,
            authority_run_id=authority_run_id,
            effect_id=effect_id,
            input_payload=input_payload,
            deadline_monotonic=deadline,
        )
        try:
            same_execution = canonical_json(execution) == canonical_json(
                recovered_execution
            )
        except (TypeError, ValueError):
            same_execution = False
        if not same_execution:
            raise FirstStrictExecutionJournalError(
                "first_strict_execution_completed_replay_binding_invalid"
            )
        _require_completion_deadline(deadline, "completed_replay_validation")
        return reference

    try:
        from national_native import _validate_first_strict_runner_execution_seal

        _validate_first_strict_runner_execution_seal(ticket, execution)
    except FirstStrictExecutionJournalError:
        raise
    except Exception as exc:
        raise FirstStrictExecutionJournalError(str(exc)) from exc
    _require_completion_deadline(deadline, "runner_seal_validation")
    issues, terminal_proof = _terminal_execution_issues(
        execution,
        deck_seed_base=input_payload.get("deck_seed_base"),
        bot_seed_base=input_payload.get("bot_seed_base"),
        timing_plan=frozen_timing_plan,
    )
    if issues:
        raise FirstStrictExecutionJournalError(";".join(issues))
    _require_completion_deadline(deadline, "terminal_validation")
    normalized_execution = json.loads(canonical_json(execution))
    replay_digest = canonical_digest(normalized_execution)
    _require_completion_deadline(deadline, "replay_normalization")
    try:
        with store.command_lock(
            authority_run_id,
            blocking=True,
            deadline_monotonic=deadline,
        ):
            prior_receipts = [
                event for event in store.events(
                    authority_run_id,
                    deadline_monotonic=deadline,
                )
                if event.event_type == EXECUTION_EVENT_TYPE
            ]
            previous_receipt_digest = (
                str(prior_receipts[-1].payload.get("receipt_chain_digest") or "")
                if prior_receipts
                else ""
            )
            receipt_id = canonical_digest({
                "authority_run_id": authority_run_id,
                "effect_id": ticket.get("effect_id"),
                "lease_epoch": ticket.get("lease_epoch"),
                "match_run_id": ticket.get("match_run_id"),
                "input_payload": input_payload,
                "replay_digest": replay_digest,
                "terminal_proof": terminal_proof,
            })
            result_payload = {
                "receipt_id": receipt_id,
                "match_run_id": ticket.get("match_run_id"),
                "scope_digest": input_payload.get("scope_digest"),
                "repeat": input_payload.get("repeat"),
                "deck_seed_base": input_payload.get("deck_seed_base"),
                "bot_seed_base": input_payload.get("bot_seed_base"),
                "replay_digest": replay_digest,
                "terminal_proof": terminal_proof,
                "previous_receipt_digest": previous_receipt_digest,
                # This body is deliberately inside the atomic effect transaction.
                # The external replay file is a recoverable projection, not the
                # sole record of a successfully completed 70-hand match.
                "execution": normalized_execution,
            }
            result_payload["receipt_chain_digest"] = canonical_digest(result_payload)
            completion_id = f"control-match-completed:{receipt_id}"
            recorded_causation = f"control-match-recorded:{receipt_id}"
            _require_completion_deadline(deadline, "receipt_construction")
            completed = store.complete_effect(
                str(ticket.get("effect_id") or ""),
                lease_epoch=int(ticket.get("lease_epoch") or 0),
                completion_id=completion_id,
                result_payload=result_payload,
                causation_id=f"effect-completed:{receipt_id}",
                followup_events=[{
                    "event_type": EXECUTION_EVENT_TYPE,
                    "causation_id": recorded_causation,
                    "payload": result_payload,
                }],
                require_live_lease=True,
                deadline_monotonic=deadline,
            )
            if completed.get("accepted") is not True:
                raise FirstStrictExecutionJournalError(
                    "first_strict_execution_stale_completion"
                )
            recorded = [
                event for event in completed.get("followup_events", ())
                if event.event_type == EXECUTION_EVENT_TYPE
                and event.causation_id == recorded_causation
            ]
    except WorkflowDeadlineExceeded as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completion_deadline_exceeded:durable_commit"
        ) from exc

    # Consume the one-shot in-memory authority only after SQLite has committed
    # the terminal effect and event.  A validation/write/lease failure before
    # this point remains retryable with the same real runner result.
    try:
        from national_native import _consume_first_strict_runner_execution_seal

        _consume_first_strict_runner_execution_seal(ticket, execution)
    except FirstStrictExecutionJournalError:
        raise
    except Exception as exc:
        raise FirstStrictExecutionJournalError(str(exc)) from exc

    projected_digest, _path = _write_replay(normalized_execution)
    if projected_digest != replay_digest:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_replay_projection_mismatch"
        )
    if len(recorded) != 1 or recorded[0].payload != result_payload:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_recorded_event_missing"
        )
    effect = completed.get("effect") or {}
    return {
        "schema_version": RECEIPT_REF_SCHEMA_VERSION,
        "kind": RECEIPT_REF_KIND,
        "authority_run_id": ticket["authority_run_id"],
        "effect_id": ticket["effect_id"],
        "match_run_id": ticket["match_run_id"],
        "receipt_id": receipt_id,
        "scope_digest": input_payload["scope_digest"],
        "result_digest": effect.get("result_digest"),
        "recorded_seq": recorded[0].seq,
        "recorded_payload_digest": recorded[0].payload_digest,
        "replay_digest": replay_digest,
        "receipt_chain_digest": result_payload["receipt_chain_digest"],
    }


def read_control_execution_receipt(
    reference: Any,
    *,
    expected_scope: dict[str, Any] | None = None,
    deadline_monotonic: float | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Resolve a small result reference through the authority stream and replay."""

    if not isinstance(reference, dict):
        return None, ["first_strict_execution_receipt_ref_missing"]
    issues: list[str] = []
    if reference.get("schema_version") != RECEIPT_REF_SCHEMA_VERSION or reference.get(
        "kind"
    ) != RECEIPT_REF_KIND:
        issues.append("first_strict_execution_receipt_ref_schema_invalid")
    for field in (
        "receipt_id",
        "scope_digest",
        "result_digest",
        "recorded_payload_digest",
        "replay_digest",
        "receipt_chain_digest",
    ):
        if not _valid_digest(reference.get(field)):
            issues.append(f"first_strict_execution_receipt_ref_{field}_invalid")
    for field in ("authority_run_id", "effect_id", "match_run_id"):
        if not isinstance(reference.get(field), str) or not reference.get(field):
            issues.append(f"first_strict_execution_receipt_ref_{field}_invalid")
    if not _plain_int(reference.get("recorded_seq")) or int(
        reference.get("recorded_seq") or 0
    ) < 1:
        issues.append("first_strict_execution_receipt_ref_recorded_seq_invalid")
    if issues:
        return None, issues

    deadline = _effective_completion_deadline(deadline_monotonic)
    _require_completion_deadline(deadline, "receipt_read_entry")
    try:
        store = _store(deadline_monotonic=deadline)
        events = store.events(
            str(reference["authority_run_id"]),
            deadline_monotonic=deadline,
        )
        effect = store.effect(
            str(reference["effect_id"]),
            deadline_monotonic=deadline,
        )
    except WorkflowDeadlineExceeded as exc:
        raise FirstStrictExecutionJournalError(
            "first_strict_execution_completion_deadline_exceeded:receipt_read"
        ) from exc
    except Exception as exc:
        return None, [
            f"first_strict_execution_authority_read_error:{type(exc).__name__}"
        ]
    _require_completion_deadline(deadline, "receipt_authority_read")
    if effect.get("status") != "completed" or effect.get("kind") != EXECUTION_EFFECT_KIND:
        issues.append("first_strict_execution_effect_not_completed")
    input_payload = effect.get("input_payload")
    result_payload = effect.get("result_payload")
    if not isinstance(input_payload, dict) or not isinstance(result_payload, dict):
        return None, [*issues, "first_strict_execution_effect_payload_missing"]
    scope = input_payload.get("scope")
    try:
        normalized_scope = normalize_execution_scope(scope)
    except FirstStrictExecutionJournalError as exc:
        normalized_scope = {}
        issues.append(str(exc))
    receipt_timing_plan = None
    try:
        from national_native import (
            LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            require_native_match_timing_plan,
        )

        receipt_timing_plan = require_native_match_timing_plan(
            input_payload.get("timing_plan"),
            hands=70,
            requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
        )
        if (
            input_payload.get("timing_plan_digest") != receipt_timing_plan.digest()
            or (
                normalized_scope
                and normalized_scope.get("native_match_timing_plan_digest")
                != receipt_timing_plan.digest()
            )
        ):
            issues.append("first_strict_execution_receipt_timing_plan_mismatch")
    except Exception:
        issues.append("first_strict_execution_receipt_timing_plan_invalid")
    if normalized_scope and _authority_run_id(normalized_scope) != reference.get(
        "authority_run_id"
    ):
        issues.append("first_strict_execution_authority_scope_mismatch")
    if expected_scope is not None:
        try:
            normalized_expected = normalize_execution_scope(expected_scope)
        except FirstStrictExecutionJournalError as exc:
            normalized_expected = {}
            issues.append(str(exc))
        if normalized_expected and normalized_scope != normalized_expected:
            issues.append("first_strict_execution_expected_scope_mismatch")
    if input_payload.get("scope_digest") != reference.get("scope_digest"):
        issues.append("first_strict_execution_scope_digest_mismatch")
    if effect.get("result_digest") != reference.get("result_digest"):
        issues.append("first_strict_execution_result_digest_mismatch")
    if result_payload.get("receipt_id") != reference.get("receipt_id"):
        issues.append("first_strict_execution_receipt_id_mismatch")
    if result_payload.get("match_run_id") != reference.get("match_run_id") or input_payload.get(
        "match_run_id"
    ) != reference.get("match_run_id"):
        issues.append("first_strict_execution_match_run_id_mismatch")
    if result_payload.get("replay_digest") != reference.get("replay_digest"):
        issues.append("first_strict_execution_replay_ref_mismatch")
    recorded_stream = [
        event for event in events
        if event.event_type == EXECUTION_EVENT_TYPE
    ]
    previous_receipt_digest = ""
    seen_receipt_ids: set[str] = set()
    seen_match_runs: set[str] = set()
    for event in recorded_stream:
        payload = event.payload
        observed_chain_digest = str(payload.get("receipt_chain_digest") or "")
        unsigned_chain_payload = {
            key: value
            for key, value in payload.items()
            if key != "receipt_chain_digest"
        }
        if payload.get("previous_receipt_digest") != previous_receipt_digest or (
            observed_chain_digest != canonical_digest(unsigned_chain_payload)
        ):
            issues.append("first_strict_execution_receipt_chain_invalid")
            break
        receipt_id = str(payload.get("receipt_id") or "")
        match_run_id = str(payload.get("match_run_id") or "")
        if receipt_id in seen_receipt_ids or match_run_id in seen_match_runs:
            issues.append("first_strict_execution_receipt_chain_duplicate")
            break
        seen_receipt_ids.add(receipt_id)
        seen_match_runs.add(match_run_id)
        previous_receipt_digest = observed_chain_digest
    if result_payload.get("receipt_chain_digest") != reference.get(
        "receipt_chain_digest"
    ):
        issues.append("first_strict_execution_receipt_chain_ref_mismatch")
    recorded = [
        event for event in recorded_stream
        if event.payload.get("receipt_id") == reference.get("receipt_id")
    ]
    if len(recorded) != 1:
        issues.append("first_strict_execution_recorded_event_invalid")
    else:
        event = recorded[0]
        if any((
            event.seq != reference.get("recorded_seq"),
            event.payload_digest != reference.get("recorded_payload_digest"),
            event.payload != result_payload,
        )):
            issues.append("first_strict_execution_recorded_event_binding_mismatch")

    embedded_execution = result_payload.get("execution")
    try:
        embedded_digest = (
            canonical_digest(embedded_execution)
            if isinstance(embedded_execution, dict)
            else None
        )
    except (TypeError, ValueError):
        embedded_digest = None
    if embedded_digest != reference.get("replay_digest"):
        issues.append("first_strict_execution_embedded_replay_invalid")
    else:
        # The SQLite effect/event transaction is the durable authority.  The
        # external read-only replay is only its content-addressed projection,
        # and may legitimately be absent after a crash between commit and
        # projection.  Re-materialize it from the exact committed bytes.
        _require_completion_deadline(deadline, "receipt_projection_start")
        try:
            projected_digest, _projected_path = _write_replay(
                embedded_execution
            )
            if projected_digest != reference.get("replay_digest"):
                issues.append(
                    "first_strict_execution_replay_projection_mismatch"
                )
        except Exception as exc:
            issues.append(
                "first_strict_execution_replay_projection_error:"
                f"{type(exc).__name__}"
            )
        _require_completion_deadline(deadline, "receipt_projection_complete")

    _require_completion_deadline(deadline, "receipt_replay_read_start")
    replay, replay_issues = _read_replay(reference.get("replay_digest"))
    _require_completion_deadline(deadline, "receipt_replay_read_complete")
    issues.extend(replay_issues)
    if replay is not None and receipt_timing_plan is not None:
        if isinstance(embedded_execution, dict) and replay != embedded_execution:
            issues.append("first_strict_execution_embedded_replay_mismatch")
        terminal_issues, proof = _terminal_execution_issues(
            replay,
            deck_seed_base=input_payload.get("deck_seed_base"),
            bot_seed_base=input_payload.get("bot_seed_base"),
            timing_plan=receipt_timing_plan,
        )
        issues.extend(terminal_issues)
        if proof != result_payload.get("terminal_proof"):
            issues.append("first_strict_execution_terminal_proof_mismatch")
    _require_completion_deadline(deadline, "receipt_validation_complete")
    payload = {
        "scope": normalized_scope,
        "input": deepcopy(input_payload),
        "result": deepcopy(result_payload),
        "execution": deepcopy(replay),
    }
    return (payload if not issues else None), list(dict.fromkeys(issues))


__all__ = [
    "CONTROL_EXECUTION_ROOT",
    "FirstStrictExecutionJournalError",
    "begin_control_execution",
    "complete_control_execution",
    "control_execution_completion_deadline",
    "execution_scope_digest",
    "normalize_execution_scope",
    "read_control_execution_receipt",
]
