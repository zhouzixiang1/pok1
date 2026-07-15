"""Bounded LLM analysis for official-platform compliance evidence."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from pathlib import Path
import re
from typing import Any, Awaitable, Callable


ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "web" / "core" / "prompts" / "official_platform_analysis.md"
DEFAULT_MAX_EVIDENCE_CHARS = 45000

AnalysisRunner = Callable[[str], str | Awaitable[str]]

ALLOWED_ANALYSIS_STATUSES = {"explained", "no_findings", "insufficient_evidence"}
ALLOWED_FAILURE_CLASSES = {
    "protocol",
    "communication",
    "state_machine",
    "timeout",
    "platform_race",
    "harness",
    "obvious_decision_error",
    "none",
}
ALLOWED_CLASSIFICATIONS = ALLOWED_FAILURE_CLASSES | {"pass", "inconclusive"}

MAX_ROUNDS = 12
MAX_DETERMINISTIC_ISSUES = 80
MAX_ROUND_ISSUES = 30
MAX_THP_SUMMARIES = 12
MAX_WIRE_ISSUES = 20
MAX_WIRE_WARNINGS = 10
MAX_PENDING_ACTIONS = 10
MAX_STATE_SEQUENCE_EVENTS = 5
MAX_STATE_MESSAGES = 4
MAX_ISSUE_CHARS = 400

_ROUND_ID_RE = re.compile(
    r"^(?:self_play|opponent|round)_[0-9]{1,3}(?:_[0-9]{8}_[0-9]{6})?$"
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\"'<>\r\n|,;\]\}\)]+)")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?i)\b[A-Z]:[\\/][^\"'<>\r\n|,;\]\}\)]+")
_UNC_ABSOLUTE_PATH_RE = re.compile(r"\\\\[^\"'<>\r\n|,;\]\}\)]+")
_LONG_HEX_RE = re.compile(r"(?i)\b[0-9a-f]{32,}\b")
_RAISE_RE = re.compile(r"^raise ([0-9]{1,10})$")

_ROUND_KINDS = {"self_play", "opponent", "round"}
_STREETS = {"preflop", "flop", "turn", "river", "handshake", "settlement", "unknown"}
_DIRECTIONS = {"server_to_bot", "bot_to_server", "unknown"}
_ACTIONS = {"call", "check", "fold", "allin", "raise", "bet", "unknown"}
_EXPECTED_REASONS = {
    "name_handshake",
    "small_blind_preflop_open",
    "preflop_first_action",
    "flop_first_action",
    "turn_first_action",
    "river_first_action",
    "respond_to_call",
    "respond_to_check",
    "respond_to_raise",
    "respond_to_allin",
    "bot_action",
    "none",
    "unknown",
}
_WIRE_FINDING_KINDS = {
    "wire_bet_token",
    "wire_action_whitespace",
    "wire_raise_format",
    "wire_action_format",
    "unsolicited_client_action",
    "bot_response_timeout",
    "bot_response_slow",
    "pending_bot_response_timeout",
    "pending_bot_response_slow",
    "platform_silent_timeout_gap",
    "platform_silent_idle_gap",
    "platform_silent_slow_gap",
    "unknown_server_message",
    "illegal_call",
    "illegal_check",
    "illegal_fold",
    "illegal_allin",
    "illegal_raise",
    "illegal_unknown",
    "wire_replay_error",
    "wire_stream_eof_remainder",
    "wire_stream_error",
    "wire_probe_upstream_connect_failed",
    "unknown",
}
_EVENT_TYPES = {
    "action",
    "hand_start",
    "street_start",
    "name_handshake",
    "settlement",
    "showdown",
    "unknown",
}

_EXPECTED_RULE_BY_KIND = {
    "wire_bet_token": "use_raise_not_bet",
    "wire_action_whitespace": "exact_action_token_format",
    "wire_raise_format": "raise_single_space_integer",
    "wire_action_format": "recognized_action_token",
    "unsolicited_client_action": "act_only_with_pending_request",
    "bot_response_timeout": "respond_within_decision_timeout",
    "pending_bot_response_timeout": "respond_within_decision_timeout",
    "illegal_call": "call_legal_for_current_state",
    "illegal_check": "check_legal_for_current_state",
    "illegal_fold": "fold_legal_for_current_state",
    "illegal_allin": "allin_legal_for_current_state",
    "illegal_raise": "raise_to_legal_for_current_state",
    "illegal_unknown": "recognized_action_token",
    "platform_silent_timeout_gap": "platform_progress_or_harness_attribution",
}

_STRENGTH_TEXT_RE = re.compile(
    r"(?i)(?:\brating\b|\bwin[ _-]?rate\b|\bwon\b|\blost\b|"
    r"\bprofit\b|\bnet[ _-]?chips?\b|\bchip outcome\b|\bstronger\b|"
    r"\bweaker\b|\bimprove strategy\b|\bweak calls?\b|\bexploit(?:ation)?\b|"
    r"\bEV\b|\bequity\b|\bbluff(?:ing)?\b|\baggression\b|\baggressive\b|"
    r"胜率|强度|收益|盈利|筹码优势|权益|诈唬|激进|剥削)"
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _stable_evidence_id(prefix: str, payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _bounded_int(value: Any, *, default: int | None = None) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(-1_000_000_000, min(1_000_000_000, number))


def _bounded_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return round(max(-1_000_000_000.0, min(1_000_000_000.0, number)), 3)


def _allowed_enum(value: Any, allowed: set[str], *, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _connection_label(value: Any) -> str:
    text = str(value or "?").strip()
    if text.upper() in {"A", "B"}:
        return text.upper()
    if text.lower() in {"candidate", "opponent"}:
        return text.lower()
    return "unknown"


def _sanitize_issue_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("kind") or value.get("issue") or "structured_issue"
    text = str(value or "")
    text = _UNC_ABSOLUTE_PATH_RE.sub("<absolute-path>", text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("<absolute-path>", text)
    text = _POSIX_ABSOLUTE_PATH_RE.sub("<absolute-path>", text)
    text = _LONG_HEX_RE.sub("<hex-payload>", text)
    text = " ".join(text.split())
    if len(text) > MAX_ISSUE_CHARS:
        text = text[: MAX_ISSUE_CHARS - 3] + "..."
    return text


def _round_identity(round_item: dict[str, Any], ordinal: int) -> tuple[str, str, int]:
    kind = _allowed_enum(round_item.get("round_kind"), _ROUND_KINDS, default="round")
    index = _bounded_int(round_item.get("round_index"), default=ordinal) or ordinal
    raw_round_id = str(round_item.get("round_id") or "")
    round_id = raw_round_id if _ROUND_ID_RE.fullmatch(raw_round_id) else f"{kind}_{index:02d}"
    evidence_id = _stable_evidence_id(
        "round",
        {"round_id": round_id, "round_kind": kind, "round_index": index},
    )
    return round_id, evidence_id, index


def _compact_issue_list(
    values: Any,
    *,
    prefix: str,
    context: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    for index, value in enumerate(values[:limit], start=1):
        issue = _sanitize_issue_text(value)
        if not issue:
            continue
        identity = {**context, "index": index, "issue": issue}
        result.append({"evidence_id": _stable_evidence_id(prefix, identity), "issue": issue})
    return result


def _summarize_action(message: Any) -> dict[str, Any]:
    text = str(message or "")
    if text in {"call", "check", "fold", "allin"}:
        return {"action": text}
    match = _RAISE_RE.fullmatch(text)
    if match:
        return {"action": "raise", "amount": int(match.group(1))}
    if text.startswith("raise"):
        return {"action": "raise", "format": "invalid"}
    if text.startswith("bet"):
        return {"action": "bet", "format": "invalid"}
    return {"action": "unknown"}


def _summarize_wire_message(message: Any) -> dict[str, Any]:
    text = str(message or "")
    if text == "name":
        return {"event_type": "name_handshake"}
    if text.startswith("preflop|"):
        parts = text.split("|", 2)
        blind = parts[1].lower() if len(parts) > 1 else "unknown"
        if blind not in {"smallblind", "bigblind"}:
            blind = "unknown"
        return {"event_type": "hand_start", "street": "preflop", "blind": blind}
    for street in ("flop", "turn", "river"):
        if text.startswith(f"{street}|"):
            return {"event_type": "street_start", "street": street}
    if text.startswith("earnChips"):
        return {"event_type": "settlement"}
    if text.startswith("oppo_hands|"):
        return {"event_type": "showdown"}
    action = _summarize_action(text)
    if action["action"] != "unknown" or text in {"call", "check", "fold", "allin"}:
        return {"event_type": "action", **action}
    return {"event_type": "unknown"}


def _compact_state_event(
    event: Any,
    *,
    parent_evidence_id: str,
    event_index: int,
) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    compact: dict[str, Any] = {"sequence_index": event_index}
    direction = _allowed_enum(event.get("direction"), _DIRECTIONS, default="unknown")
    compact["direction"] = direction
    compact["connection"] = _connection_label(event.get("conn", event.get("connection")))
    timing_sec = _bounded_float(event.get("dt", event.get("timing_sec")))
    if timing_sec is not None:
        compact["timing_sec"] = timing_sec
    street = _allowed_enum(event.get("stage", event.get("street")), _STREETS, default="unknown")
    if street != "unknown":
        compact["street"] = street

    messages = event.get("messages")
    if not isinstance(messages, list):
        messages = [event.get("message")] if event.get("message") is not None else []
    compact_messages = [_summarize_wire_message(item) for item in messages[:MAX_STATE_MESSAGES]]
    if compact_messages:
        compact["events"] = compact_messages
    else:
        event_type = _allowed_enum(event.get("event_type"), _EVENT_TYPES, default="unknown")
        action = _allowed_enum(event.get("action"), _ACTIONS, default="unknown")
        compact["events"] = [{"event_type": event_type, "action": action}]
    compact["evidence_id"] = _stable_evidence_id(
        "state",
        {"parent": parent_evidence_id, "event": compact},
    )
    return compact


def _compact_state_sequence(
    finding: dict[str, Any],
    *,
    finding_evidence_id: str,
) -> list[dict[str, Any]]:
    raw_sequence = finding.get("state_sequence")
    if isinstance(raw_sequence, list):
        candidates = raw_sequence[-MAX_STATE_SEQUENCE_EVENTS:]
    else:
        candidates = []
        if isinstance(finding.get("previous_event"), dict):
            candidates.append(finding["previous_event"])
        if finding.get("message") is not None:
            candidates.append({
                "conn": finding.get("conn"),
                "direction": "bot_to_server",
                "dt": finding.get("dt"),
                "stage": finding.get("stage"),
                "messages": [finding.get("message")],
            })
        if isinstance(finding.get("next_event"), dict):
            candidates.append(finding["next_event"])
    result: list[dict[str, Any]] = []
    for event_index, event in enumerate(candidates[:MAX_STATE_SEQUENCE_EVENTS], start=1):
        compact = _compact_state_event(
            event,
            parent_evidence_id=finding_evidence_id,
            event_index=event_index,
        )
        if compact is not None:
            result.append(compact)
    return result


def _compact_wire_finding(
    value: Any,
    *,
    category: str,
    index: int,
    round_id: str,
    round_evidence_id: str,
) -> dict[str, Any]:
    finding = value if isinstance(value, dict) else {"kind": value}
    kind = _allowed_enum(finding.get("kind"), _WIRE_FINDING_KINDS, default="unknown")
    connection = _connection_label(finding.get("conn", finding.get("connection")))
    hand = _bounded_int(finding.get("hand"))
    street = _allowed_enum(finding.get("stage", finding.get("street")), _STREETS, default="unknown")
    action = _summarize_action(finding.get("message", finding.get("observed_action")))
    expected_reason = _allowed_enum(
        finding.get("expected_reason"),
        _EXPECTED_REASONS,
        default="unknown",
    )
    identity = {
        "category": category,
        "index": index,
        "round_evidence_id": round_evidence_id,
        "kind": kind,
        "connection": connection,
        "hand": hand,
        "street": street,
        "action": action,
    }
    evidence_id = _stable_evidence_id(f"wire-{category}", identity)
    compact: dict[str, Any] = {
        "evidence_id": evidence_id,
        "round_evidence_id": round_evidence_id,
        "round_id": round_id,
        "kind": kind,
        "connection": connection,
        "hand": hand,
        "street": street,
        "observed_action": action["action"],
    }
    if "amount" in action:
        compact["observed_amount"] = action["amount"]
    if "format" in action:
        compact["observed_format"] = action["format"]
    expected_rule = _EXPECTED_RULE_BY_KIND.get(kind)
    if expected_rule:
        compact["expected_rule"] = expected_rule
    if expected_reason != "unknown":
        compact["expected_reason"] = expected_reason
    for source_key, target_key in (("dt", "timing_sec"), ("waited_sec", "waited_sec")):
        number = _bounded_float(finding.get(source_key))
        if number is not None:
            compact[target_key] = number
    state_sequence = _compact_state_sequence(finding, finding_evidence_id=evidence_id)
    if state_sequence:
        compact["state_sequence"] = state_sequence
    return compact


def _compact_pending_action(
    value: Any,
    *,
    index: int,
    round_id: str,
    round_evidence_id: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    compact: dict[str, Any] = {
        "round_evidence_id": round_evidence_id,
        "round_id": round_id,
        "connection": _connection_label(value.get("conn", value.get("connection"))),
        "hand": _bounded_int(value.get("hand")),
        "street": _allowed_enum(value.get("stage", value.get("street")), _STREETS, default="unknown"),
        "expected_reason": _allowed_enum(
            value.get("expected_reason"),
            _EXPECTED_REASONS,
            default="unknown",
        ),
    }
    waited_sec = _bounded_float(value.get("waited_sec"))
    if waited_sec is not None:
        compact["waited_sec"] = waited_sec
    compact["evidence_id"] = _stable_evidence_id(
        "wire-pending",
        {"index": index, **compact},
    )
    return compact


def _compact_wire_replay(
    replay: Any,
    *,
    round_id: str,
    round_evidence_id: str,
) -> dict[str, Any]:
    if not isinstance(replay, dict) or not replay:
        return {}
    compact: dict[str, Any] = {
        "evidence_id": _stable_evidence_id("wire-summary", {"round": round_evidence_id}),
    }
    for key in ("events_seen", "hands_started_min", "settlements_min"):
        value = _bounded_int(replay.get(key))
        if value is not None:
            compact[key] = value
    silent_gap = _bounded_float(replay.get("max_platform_silent_gap_sec"))
    if silent_gap is not None:
        compact["max_platform_silent_gap_sec"] = silent_gap

    seat_summaries: list[dict[str, Any]] = []
    seats = replay.get("seats")
    if isinstance(seats, dict):
        sorted_seats = sorted(seats.items(), key=lambda item: str(item[0]))
        for seat_index, (label, raw_seat) in enumerate(sorted_seats[:4], start=1):
            if not isinstance(raw_seat, dict):
                continue
            seat: dict[str, Any] = {"connection": _connection_label(label)}
            for key in ("hands_started", "settlements"):
                value = _bounded_int(raw_seat.get(key))
                if value is not None:
                    seat[key] = value
            response_sec = _bounded_float(raw_seat.get("max_response_sec"))
            if response_sec is not None:
                seat["max_response_sec"] = response_sec
            seat["pending_expected_action"] = bool(raw_seat.get("pending_expected_action"))
            expected_reason = _allowed_enum(
                raw_seat.get("expected_reason"),
                _EXPECTED_REASONS,
                default="unknown",
            )
            if expected_reason != "unknown":
                seat["expected_reason"] = expected_reason
            seat["evidence_id"] = _stable_evidence_id(
                "wire-seat",
                {"round": round_evidence_id, "index": seat_index, "seat": seat},
            )
            seat_summaries.append(seat)
    if seat_summaries:
        compact["seat_summaries"] = seat_summaries

    for key, category, limit in (
        ("issues", "issue", MAX_WIRE_ISSUES),
        ("warnings", "warning", MAX_WIRE_WARNINGS),
    ):
        values = replay.get(key)
        if not isinstance(values, list):
            continue
        compact[key] = [
            _compact_wire_finding(
                item,
                category=category,
                index=index,
                round_id=round_id,
                round_evidence_id=round_evidence_id,
            )
            for index, item in enumerate(values[:limit], start=1)
        ]

    pending = replay.get("pending_expected_actions")
    if isinstance(pending, list):
        compact["pending_expected_actions"] = [
            item
            for index, value in enumerate(pending[:MAX_PENDING_ACTIONS], start=1)
            if (item := _compact_pending_action(
                value,
                index=index,
                round_id=round_id,
                round_evidence_id=round_evidence_id,
            )) is not None
        ]
    return compact


def _compact_thp_summaries(
    values: Any,
    *,
    round_evidence_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    for index, value in enumerate(values[:MAX_THP_SUMMARIES], start=1):
        if not isinstance(value, dict):
            continue
        summary: dict[str, Any] = {"summary_index": index}
        if "exists" in value:
            summary["exists"] = bool(value.get("exists"))
        hand_records = _bounded_int(value.get("hand_records"))
        if hand_records is not None:
            summary["hand_records"] = hand_records
        size_bytes = _bounded_int(value.get("bytes", value.get("size_bytes")))
        if size_bytes is not None:
            summary["size_bytes"] = size_bytes
        if value.get("issue"):
            summary["issue"] = _sanitize_issue_text(value.get("issue"))
        summary["evidence_id"] = _stable_evidence_id(
            "thp",
            {"round": round_evidence_id, "summary": summary},
        )
        result.append(summary)
    return result


def _compact_round(round_item: dict[str, Any], *, ordinal: int) -> dict[str, Any]:
    round_id, round_evidence_id, round_index = _round_identity(round_item, ordinal)
    round_kind = _allowed_enum(round_item.get("round_kind"), _ROUND_KINDS, default="round")
    compact: dict[str, Any] = {
        "evidence_id": round_evidence_id,
        "round_id": round_id,
        "round_kind": round_kind,
        "round_index": round_index,
        "target_hands": _bounded_int(round_item.get("target_hands")),
        "passed": bool(round_item.get("passed")),
        "classification": _allowed_enum(
            round_item.get("classification"),
            ALLOWED_CLASSIFICATIONS,
            default="inconclusive",
        ),
        "issues": _compact_issue_list(
            round_item.get("issues"),
            prefix="round-issue",
            context={"round": round_evidence_id},
            limit=MAX_ROUND_ISSUES,
        ),
        "thp_summaries": _compact_thp_summaries(
            round_item.get("thp_summaries"),
            round_evidence_id=round_evidence_id,
        ),
        "wire_replay_summary": _compact_wire_replay(
            round_item.get("wire_replay_summary"),
            round_id=round_id,
            round_evidence_id=round_evidence_id,
        ),
    }
    attribution = round_item.get("attribution") if isinstance(round_item.get("attribution"), dict) else {}
    compact_findings: list[dict[str, Any]] = []
    for finding in (attribution.get("findings") or [])[:MAX_WIRE_ISSUES]:
        if not isinstance(finding, dict):
            continue
        finding_id = str(finding.get("finding_id") or "")
        if not finding_id:
            continue
        raw_finding_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        compact_finding = {
            "evidence_id": finding_id,
            "round_evidence_id": round_evidence_id,
            "round_id": round_id,
            "code": _sanitize_issue_text(finding.get("code")),
            "category": _allowed_enum(
                finding.get("category"),
                ALLOWED_FAILURE_CLASSES | {"runtime", "platform"},
                default="harness",
            ),
            "subject_domain": _allowed_enum(
                finding.get("subject_domain"),
                {"candidate", "opponent", "platform", "harness"},
                default="harness",
            ),
            "subject_instance_id": _sanitize_issue_text(finding.get("subject_instance_id")),
            "candidate_impact": _allowed_enum(
                finding.get("candidate_impact"),
                {"block", "retry", "review", "none"},
                default="review",
            ),
            "connection": _connection_label(finding.get("connection")),
        }
        hand = _bounded_int(raw_finding_evidence.get("hand"))
        if hand is not None:
            compact_finding["hand"] = hand
        compact_finding["street"] = _allowed_enum(
            raw_finding_evidence.get("stage", raw_finding_evidence.get("street")),
            _STREETS,
            default="unknown",
        )
        action = _summarize_action(
            raw_finding_evidence.get("message", raw_finding_evidence.get("observed_action"))
        )
        compact_finding["observed_action"] = action["action"]
        compact_findings.append(compact_finding)
    compact["attribution"] = {
        "policy_id": _sanitize_issue_text(attribution.get("policy_id")),
        "candidate_verdict": _allowed_enum(
            attribution.get("candidate_verdict"),
            {"pass", "fail", "inconclusive"},
            default="inconclusive",
        ),
        "candidate_blocking": bool(attribution.get("candidate_blocking")),
        "countable": bool(attribution.get("countable")),
        "retry_required": bool(attribution.get("retry_required")),
        "findings": compact_findings,
    }
    return compact


def _compact_deterministic(
    value: Any,
    *,
    rounds: list[dict[str, Any]],
) -> dict[str, Any]:
    deterministic = value if isinstance(value, dict) else {}
    compact: dict[str, Any] = {
        "passed": bool(deterministic.get("passed")),
        "classification": _allowed_enum(
            deterministic.get("classification"),
            ALLOWED_CLASSIFICATIONS,
            default="inconclusive",
        ),
        "buckets": [
            bucket
            for raw in _list_value(deterministic.get("buckets"))[:10]
            if (bucket := _allowed_enum(raw, ALLOWED_CLASSIFICATIONS, default=""))
        ],
        "blocking": bool(deterministic.get("blocking")),
        "inconclusive": bool(deterministic.get("inconclusive")),
        "violation": bool(deterministic.get("violation")),
        "blocking_issue_count": _bounded_int(deterministic.get("blocking_issue_count"), default=0),
        "issues": _compact_issue_list(
            deterministic.get("issues"),
            prefix="det-issue",
            context={"classification": deterministic.get("classification")},
            limit=MAX_DETERMINISTIC_ISSUES,
        ),
    }
    raw_rounds = deterministic.get("round_classifications")
    if isinstance(raw_rounds, list):
        compact_rounds: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_rounds[:MAX_ROUNDS]):
            if not isinstance(raw, dict):
                continue
            round_ref = rounds[index] if index < len(rounds) else None
            item: dict[str, Any] = {
                "round_evidence_id": round_ref.get("evidence_id") if round_ref else None,
                "classification": _allowed_enum(
                    raw.get("classification"),
                    ALLOWED_CLASSIFICATIONS,
                    default="inconclusive",
                ),
                "buckets": [
                    bucket
                    for bucket_raw in _list_value(raw.get("buckets"))[:10]
                    if (bucket := _allowed_enum(bucket_raw, ALLOWED_CLASSIFICATIONS, default=""))
                ],
                "blocking": bool(raw.get("blocking")),
                "inconclusive": bool(raw.get("inconclusive")),
                "violation": bool(raw.get("violation")),
                "blocking_issue_count": _bounded_int(raw.get("blocking_issue_count"), default=0),
            }
            item["evidence_id"] = _stable_evidence_id(
                "det-round",
                {"index": index + 1, **item},
            )
            compact_rounds.append(item)
        compact["round_classifications"] = compact_rounds
    return compact


def _trim_compact_evidence(compact: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    def size() -> int:
        return len(json.dumps(compact, ensure_ascii=False, indent=2))

    if size() <= max_chars:
        return compact
    for round_item in compact["rounds"]:
        replay = round_item.get("wire_replay_summary") or {}
        for key in ("warnings", "issues"):
            for finding in replay.get(key) or []:
                finding.pop("state_sequence", None)
    if size() <= max_chars:
        return compact
    for round_item in compact["rounds"]:
        replay = round_item.get("wire_replay_summary") or {}
        replay.pop("warnings", None)
        replay["issues"] = (replay.get("issues") or [])[:5]
        round_item["issues"] = round_item.get("issues", [])[:10]
    compact["deterministic"]["issues"] = compact["deterministic"].get("issues", [])[:30]
    if size() <= max_chars:
        return compact
    compact["rounds"] = compact["rounds"][:3]
    return compact


def compact_evidence_for_llm(
    evidence: dict[str, Any],
    *,
    max_chars: int = DEFAULT_MAX_EVIDENCE_CHARS,
) -> dict[str, Any]:
    """Build the strict, allowlisted compliance evidence boundary for the LLM."""
    raw_rounds = _list_value(evidence.get("rounds")) if isinstance(evidence, dict) else []
    rounds = [
        _compact_round(dict(item), ordinal=index)
        for index, item in enumerate(raw_rounds[:MAX_ROUNDS], start=1)
        if isinstance(item, dict)
    ]
    compact: dict[str, Any] = {
        "schema_version": 2,
        "purpose": "official_platform_compliance",
        "strength_evaluation": "not_applicable",
        "deterministic": _compact_deterministic(
            evidence.get("deterministic") if isinstance(evidence, dict) else None,
            rounds=rounds,
        ),
        "rounds": rounds,
    }
    compact["evidence_id"] = _stable_evidence_id(
        "official",
        {"deterministic": compact["deterministic"], "rounds": compact["rounds"]},
    )
    return _trim_compact_evidence(compact, max_chars=max(1, int(max_chars)))


def build_official_analysis_prompt(evidence: dict[str, Any], *, prompt_template: str | None = None) -> str:
    template = prompt_template
    if template is None:
        template = PROMPT_PATH.read_text(encoding="utf-8")
    compact = compact_evidence_for_llm(evidence)
    evidence_json = json.dumps(compact, ensure_ascii=False, indent=2)
    return template.replace("{evidence_json}", evidence_json)


def _render_official_provider_prompt(inputs: dict[str, Any]):
    from llm_query import LLMRenderedMaterial

    if not isinstance(inputs, dict) or set(inputs) != {"evidence"}:
        raise ValueError("Official analysis renderer input contract mismatch")
    evidence = inputs["evidence"]
    if not isinstance(evidence, dict):
        raise ValueError("Official analysis evidence must be an object")
    compact = compact_evidence_for_llm(evidence)

    template = (
        Path(__file__).resolve().parent / "prompts" / "official_platform_analysis.md"
    ).read_text(encoding="utf-8")
    return LLMRenderedMaterial(
        text=build_official_analysis_prompt(evidence, prompt_template=template),
        evidence_kind="compact_official_compliance_evidence",
        evidence_provenance={
            "evidence_id": str(compact.get("evidence_id") or ""),
            "compact_evidence_digest": hashlib.sha256(
                json.dumps(
                    compact,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        },
    )


def safe_default_analysis(evidence: dict[str, Any], *, reason: str = "llm_not_run") -> dict[str, Any]:
    deterministic = evidence.get("deterministic") or {}
    blocking = bool(deterministic.get("blocking"))
    inconclusive = bool(deterministic.get("inconclusive"))
    passed = bool(deterministic.get("passed"))
    classification = str(deterministic.get("classification") or "none")
    hypothesis_class = classification if classification in ALLOWED_FAILURE_CLASSES else "none"
    return {
        "schema_version": 2,
        "analysis_source": "default",
        "authority": "advisory_only",
        "analysis_status": "no_findings" if passed and not blocking and not inconclusive else "insufficient_evidence",
        "hypothesis_class": hypothesis_class,
        "confidence": 0.0,
        "deterministic_context": {
            "passed": passed,
            "blocking": blocking,
            "inconclusive": inconclusive,
            "classification": classification,
        },
        "evidence": [],
        "root_cause_hypothesis": "",
        "repair_guidance": "",
        "prompt_feedback": "",
        "strength_evaluation": "not_applicable",
        "ignored_strength_fields": [],
        "ignored_authority_fields": [],
        "notes": [reason],
    }


def _parse_json_output(text: str) -> tuple[dict[str, Any] | None, str]:
    try:
        from llm_query import parse_json_output_with_mode

        parsed, mode = parse_json_output_with_mode(text)
        return (parsed if isinstance(parsed, dict) else None), mode
    except Exception:
        try:
            data = json.loads(text)
            return (data if isinstance(data, dict) else None), "OK"
        except Exception:
            return None, "PARSE_ERROR"


def _authoritative_evidence_index(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    compact = compact_evidence_for_llm(evidence)
    result: dict[str, dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            evidence_id = value.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id:
                result[evidence_id] = value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(compact)
    return result


def _compliance_text(value: Any, *, field: str, ignored: list[str]) -> str:
    text = str(value or "").strip()
    if text and _STRENGTH_TEXT_RE.search(text):
        ignored.append(field)
        return ""
    return text


def normalize_official_analysis(raw: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    deterministic = evidence.get("deterministic") or {}
    deterministic_blocking = bool(deterministic.get("blocking"))
    deterministic_passed = bool(deterministic.get("passed"))
    deterministic_inconclusive = bool(deterministic.get("inconclusive"))
    deterministic_class = str(deterministic.get("classification") or "none")

    default_status = (
        "no_findings"
        if deterministic_passed and not deterministic_blocking and not deterministic_inconclusive
        else "insufficient_evidence"
    )
    analysis_status = str(raw.get("analysis_status") or default_status).lower()
    if analysis_status not in ALLOWED_ANALYSIS_STATUSES:
        analysis_status = default_status
    hypothesis_class = str(
        raw.get("hypothesis_class") or raw.get("failure_class") or "none"
    ).lower()
    if hypothesis_class not in ALLOWED_FAILURE_CLASSES:
        hypothesis_class = (
            deterministic_class if deterministic_class in ALLOWED_FAILURE_CLASSES else "none"
        )
    try:
        confidence = float(raw.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    notes: list[str] = []
    ignored_authority_fields = sorted(
        key for key in raw if key in {"blocking", "compliance_verdict", "verdict", "passed"}
    )
    if ignored_authority_fields:
        notes.append("llm_authority_fields_ignored")

    evidence_items = raw.get("evidence")
    if not isinstance(evidence_items, list):
        evidence_items = []
    authoritative = _authoritative_evidence_index(evidence)
    normalized_evidence: list[dict[str, Any]] = []
    rejected_evidence_ids: list[str] = []
    allowed_fields = (
        "evidence_id",
        "round_id",
        "hand",
        "street",
        "connection",
        "observed_action",
        "expected_rule",
        "subject_domain",
        "subject_instance_id",
        "candidate_impact",
        "code",
        "category",
    )
    for item in evidence_items[:20]:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "")
        source = authoritative.get(evidence_id)
        if source is None:
            rejected_evidence_ids.append(evidence_id or "<missing>")
            continue
        normalized_evidence.append({
            key: source[key]
            for key in allowed_fields
            if key in source
        })
    if rejected_evidence_ids:
        notes.append("llm_evidence_ids_rejected:" + ",".join(rejected_evidence_ids[:10]))
    ignored_strength_fields = sorted(
        key for key in raw
        if key.lower() in {"strength", "strength_score", "rating", "rating_delta", "winrate", "win_rate"}
    )
    ignored_strength_text_fields: list[str] = []
    root_cause_hypothesis = _compliance_text(
        raw.get("root_cause_hypothesis", raw.get("root_cause")),
        field="root_cause_hypothesis",
        ignored=ignored_strength_text_fields,
    )
    repair_guidance = _compliance_text(
        raw.get("repair_guidance"),
        field="repair_guidance",
        ignored=ignored_strength_text_fields,
    )
    prompt_feedback = _compliance_text(
        raw.get("prompt_feedback"),
        field="prompt_feedback",
        ignored=ignored_strength_text_fields,
    )
    # Explanations and repair instructions must cite an allowlisted deterministic
    # evidence id.  A clean deterministic pass or an uncited hypothesis cannot
    # create feedback that later workers might mistake for a proven defect.
    grounded = bool(normalized_evidence)
    clean_pass = deterministic_passed and not deterministic_blocking and not deterministic_inconclusive
    if clean_pass:
        analysis_status = "no_findings"
        # A deterministic clean pass contains no defect for an advisory model
        # to repair. Even a valid round citation cannot manufacture one.
        if root_cause_hypothesis or repair_guidance or prompt_feedback:
            notes.append("clean_pass_llm_feedback_removed")
        root_cause_hypothesis = ""
        repair_guidance = ""
        prompt_feedback = ""
        hypothesis_class = "none"
    elif analysis_status == "no_findings":
        analysis_status = "insufficient_evidence"
    if not grounded:
        if root_cause_hypothesis or repair_guidance or prompt_feedback:
            notes.append("ungrounded_llm_feedback_removed")
        root_cause_hypothesis = ""
        repair_guidance = ""
        prompt_feedback = ""
        if not clean_pass:
            analysis_status = "insufficient_evidence"
    elif analysis_status == "insufficient_evidence" and root_cause_hypothesis:
        analysis_status = "explained"

    return {
        "schema_version": 2,
        "analysis_source": "llm",
        "authority": "advisory_only",
        "analysis_status": analysis_status,
        "hypothesis_class": hypothesis_class,
        "confidence": confidence,
        "deterministic_context": {
            "passed": deterministic_passed,
            "blocking": deterministic_blocking,
            "inconclusive": deterministic_inconclusive,
            "classification": deterministic_class,
        },
        "evidence": normalized_evidence,
        "root_cause_hypothesis": root_cause_hypothesis,
        "repair_guidance": repair_guidance,
        "prompt_feedback": prompt_feedback,
        "strength_evaluation": "not_applicable",
        "ignored_strength_fields": ignored_strength_fields,
        "ignored_strength_text_fields": sorted(set(ignored_strength_text_fields)),
        "ignored_authority_fields": ignored_authority_fields,
        "notes": notes,
    }


def advisory_analysis_contract_issues(analysis: Any) -> list[str]:
    """Validate that an LLM sidecar cannot masquerade as a gate verdict."""
    if not isinstance(analysis, dict):
        return ["official_llm_analysis_not_object"]
    issues: list[str] = []
    if analysis.get("schema_version") != 2:
        issues.append("official_llm_analysis_schema_mismatch")
    if analysis.get("authority") != "advisory_only":
        issues.append("official_llm_analysis_authority_mismatch")
    forbidden = sorted(
        key
        for key in {"blocking", "compliance_verdict", "passed", "verdict"}
        if key in analysis
    )
    if forbidden:
        issues.append("official_llm_analysis_forbidden_authority_fields:" + ",".join(forbidden))
    if analysis.get("analysis_status") not in ALLOWED_ANALYSIS_STATUSES:
        issues.append("official_llm_analysis_status_invalid")
    if analysis.get("hypothesis_class") not in ALLOWED_FAILURE_CLASSES:
        issues.append("official_llm_analysis_hypothesis_class_invalid")
    if analysis.get("strength_evaluation") != "not_applicable":
        issues.append("official_llm_analysis_strength_boundary_missing")
    evidence_items = analysis.get("evidence")
    if not isinstance(evidence_items, list):
        issues.append("official_llm_analysis_evidence_invalid")
        evidence_items = []
    if not evidence_items and any(
        str(analysis.get(field) or "").strip()
        for field in ("root_cause_hypothesis", "repair_guidance", "prompt_feedback")
    ):
        issues.append("official_llm_analysis_feedback_without_evidence")
    return issues


async def run_official_llm_analysis(
    evidence: dict[str, Any],
    *,
    runner: AnalysisRunner | None = None,
    output_path: str | Path | None = None,
    log_file: str | Path | None = None,
    ui: Any = None,
) -> dict[str, Any]:
    """Run bounded LLM compliance analysis.

    ``runner`` is intentionally injectable so tests and offline tools can verify
    parsing and normalization without touching the live LLM backend.
    """
    prompt = build_official_analysis_prompt(evidence)
    if runner is None:
        async def _default_runner(prompt_text: str) -> str:
            from llm_query import render_llm_prompt, run_claude_query

            rendered_prompt = render_llm_prompt(
                "OFFICIAL PLATFORM COMPLIANCE ANALYST",
                producer=_render_official_provider_prompt,
                renderer_inputs={"evidence": evidence},
            )

            output, _, _ = await run_claude_query(
                rendered_prompt,
                [],
                ui,
                "OFFICIAL PLATFORM COMPLIANCE ANALYST",
                Path(log_file) if log_file else None,
                tools=[],
            )
            return output

        runner = _default_runner
    try:
        result = runner(prompt)
        output = await result if inspect.isawaitable(result) else result
        raw, parse_mode = _parse_json_output(str(output or ""))
        if raw is None:
            analysis = safe_default_analysis(evidence, reason=f"llm_parse_failed:{parse_mode}")
            analysis["analysis_source"] = "default_parse_failed"
        else:
            analysis = normalize_official_analysis(raw, evidence)
            analysis["parse_mode"] = parse_mode
    except Exception as exc:
        analysis = safe_default_analysis(evidence, reason=f"llm_analysis_error:{type(exc).__name__}: {exc}")
        analysis["analysis_source"] = "default_error"
    if output_path is not None:
        _write_json(Path(output_path), analysis)
        analysis["analysis_path"] = str(output_path)
    return analysis


def run_official_llm_analysis_sync(
    evidence: dict[str, Any],
    *,
    runner: AnalysisRunner | None = None,
    output_path: str | Path | None = None,
    log_file: str | Path | None = None,
    ui: Any = None,
) -> dict[str, Any]:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            run_official_llm_analysis(
                evidence,
                runner=runner,
                output_path=output_path,
                log_file=log_file,
                ui=ui,
            )
        )
    raise RuntimeError("run_official_llm_analysis_sync cannot run inside an active event loop")
