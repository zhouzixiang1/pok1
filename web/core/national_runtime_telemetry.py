"""Deterministic runtime telemetry for native national TCP matches.

The local national server and generated native entrypoint expose two clocks:
server wait time includes transport and scheduling, while bot decision time
measures strategy work. Keeping both sources separate makes timeout feedback
useful without treating the official EXE as a strength evaluator.
"""

from __future__ import annotations

import json
import re
import statistics
from typing import Any


SCHEMA_VERSION = 3
DECISION_BUDGET_SEC = 60.0

DECIDE_START_RE = re.compile(
    r"DECIDE start .*?\bhand=(?P<hand>\d+)\s+stage=(?P<stage>[a-zA-Z0-9_]+)"
)
DECIDE_DONE_RE = re.compile(
    r"DECIDE done action=(?P<action>.*?)\s+elapsed=(?P<elapsed>[0-9.]+)s"
)
SEND_RE = re.compile(
    r"SEND .*?\bhand=(?P<hand>\d+)\s+stage=(?P<stage>[a-zA-Z0-9_]+).*?\bmsg=(?P<msg>.+)$"
)
OFFICIAL_DELAY_RE = re.compile(
    r"OFFICIAL_ACTION_DELAY wait=(?P<wait>[0-9.]+)s target=(?P<target>[0-9.]+)s"
)
REFINEMENT_RE = re.compile(
    r"DECIDE refinement decision_id=(?P<decision_id>\d+) sequence=(?P<sequence>\d+) "
    r"action=(?P<action>-?\d+) elapsed=(?P<elapsed>[0-9.]+)s "
    r"trusted_step=(?P<trusted_step>\d+|None) "
    r"trusted_cpu_ms=(?P<trusted_cpu>[0-9.]+|None)"
    r"(?P<reported_tail>.*)$"
)
REPORTED_SAMPLES_RE = re.compile(r"\breported_samples=(?P<value>[^\s]+)")
REPORTED_CONFIDENCE_RE = re.compile(r"\breported_confidence=(?P<value>[^\s]+)")
WORKER_DONE_RE = re.compile(
    r"DECIDE worker_done decision_id=(?P<decision_id>\d+) sequence=(?P<sequence>\d+) "
    r"latest_safe=(?P<action>-?\d+) elapsed=(?P<elapsed>[0-9.]+)s "
    r"trusted_steps=(?P<trusted_steps>\d+) "
    r"trusted_cpu_ms=(?P<trusted_cpu>[0-9.]+) "
    r"iterator_exhausted=(?P<exhausted>True|False) "
    r"termination=(?P<termination>[^\s]+)"
)
DEADLINE_TERMINATION_RE = re.compile(
    r"DECIDE (?P<termination>refinement_deadline|hard_deadline) "
    r"decision_id=(?P<decision_id>\d+)"
)
OPPONENT_TRACKER_PREFIX = "OPPONENT_TRACKER "


def _optional_number(value: str, caster):
    return None if value == "None" else caster(value)


def _optional_reported_number(pattern: re.Pattern, text: str, caster):
    match = pattern.search(text or "")
    if not match or match.group("value") == "None":
        return None
    try:
        return caster(match.group("value"))
    except (TypeError, ValueError, OverflowError):
        # Candidate-reported fields are diagnostics only.  A malformed value
        # must not hide the preceding system-trusted step/CPU evidence.
        return None


def _empty_refinement_summary() -> dict[str, Any]:
    return {
        "message_count": 0,
        "decision_count": 0,
        "trusted_steps_sum": 0,
        "trusted_steps_max": 0,
        "trusted_cpu": summarize_durations([]),
        "iterator_exhausted_count": 0,
        "termination_reasons": {},
        "reported_sample_count_max": None,
        "reported_confidence_max": None,
        "candidate_reported_fields_authoritative": False,
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * max(0.0, min(1.0, q))
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None else None


def _hand_bucket(hand: int | None) -> str:
    if hand is None or hand <= 0:
        return "unknown"
    start = ((int(hand) - 1) // 10) * 10 + 1
    return f"{start}-{min(start + 9, 70)}"


def summarize_durations(
    values: list[float],
    *,
    budget_sec: float | None = None,
) -> dict[str, Any]:
    clean = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and float(value) >= 0
    ]
    if clean:
        summary: dict[str, Any] = {
            "count": len(clean),
            "sum_sec": _rounded(sum(clean)),
            "mean_sec": _rounded(statistics.fmean(clean)),
            "p50_sec": _rounded(_percentile(clean, 0.50)),
            "p95_sec": _rounded(_percentile(clean, 0.95)),
            "p95_method": "exact_nearest_linear",
            "max_sec": _rounded(max(clean)),
        }
    else:
        summary = {
            "count": 0,
            "sum_sec": 0.0,
            "mean_sec": None,
            "p50_sec": None,
            "p95_sec": None,
            "p95_method": "exact_nearest_linear",
            "max_sec": None,
        }
    if budget_sec is not None and budget_sec > 0:
        max_sec = summary.get("max_sec")
        summary["budget_sec"] = _rounded(budget_sec)
        summary["budget_utilization_max"] = _rounded(
            (float(max_sec) / budget_sec) if max_sec is not None else 0.0
        )
    return summary


def _summarize_rows_by_key(
    rows: list[dict[str, Any]],
    value_key: str,
    group_key: str,
) -> dict[str, Any]:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        value = row.get(value_key)
        if not isinstance(value, (int, float)):
            continue
        key = str(row.get(group_key) or "unknown")
        grouped.setdefault(key, []).append(float(value))
    return {
        key: summarize_durations(values)
        for key, values in sorted(grouped.items())
    }


def empty_bot_log_summary() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "bot_log",
        "decision_latency": summarize_durations([], budget_sec=DECISION_BUDGET_SEC),
        "official_action_delay": summarize_durations([]),
        "refinement": _empty_refinement_summary(),
        "send_count": 0,
        "exception_count": 0,
        "opponent_tracker": {
            "available": False,
            "snapshot_count": 0,
            "latest": None,
        },
    }


def parse_native_bot_log(log_text: str) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    delays: list[dict[str, Any]] = []
    refinements: list[dict[str, Any]] = []
    refinement_done: list[dict[str, Any]] = []
    deadline_terminations: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    exceptions = 0
    opponent_tracker_snapshots: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        marker_index = line.find(OPPONENT_TRACKER_PREFIX)
        if marker_index >= 0:
            raw = line[marker_index + len(OPPONENT_TRACKER_PREFIX):].strip()
            try:
                snapshot = json.loads(raw)
            except Exception:
                snapshot = None
            if isinstance(snapshot, dict):
                opponent_tracker_snapshots.append(snapshot)
            continue
        refinement = REFINEMENT_RE.search(line)
        if refinement:
            reported_tail = refinement.group("reported_tail") or ""
            refinements.append({
                "decision_id": int(refinement.group("decision_id")),
                "sequence": int(refinement.group("sequence")),
                "action": int(refinement.group("action")),
                "elapsed_sec": float(refinement.group("elapsed")),
                "trusted_step": _optional_number(
                    refinement.group("trusted_step"), int
                ),
                "trusted_cpu_ms": _optional_number(
                    refinement.group("trusted_cpu"), float
                ),
                "reported_samples": _optional_reported_number(
                    REPORTED_SAMPLES_RE, reported_tail, int
                ),
                "reported_confidence": _optional_reported_number(
                    REPORTED_CONFIDENCE_RE, reported_tail, float
                ),
            })
            continue
        worker_done = WORKER_DONE_RE.search(line)
        if worker_done:
            refinement_done.append({
                "decision_id": int(worker_done.group("decision_id")),
                "trusted_steps": int(worker_done.group("trusted_steps")),
                "trusted_cpu_ms": float(worker_done.group("trusted_cpu")),
                "iterator_exhausted": worker_done.group("exhausted") == "True",
                "termination": worker_done.group("termination"),
            })
            continue
        deadline_termination = DEADLINE_TERMINATION_RE.search(line)
        if deadline_termination:
            deadline_terminations.append({
                "decision_id": int(deadline_termination.group("decision_id")),
                "termination": deadline_termination.group("termination"),
            })
            continue
        start = DECIDE_START_RE.search(line)
        if start:
            pending = {
                "hand": int(start.group("hand")),
                "stage": start.group("stage"),
            }
            continue
        done = DECIDE_DONE_RE.search(line)
        if done:
            row = dict(pending or {})
            row.update({
                "action": done.group("action").strip(),
                "elapsed_sec": float(done.group("elapsed")),
            })
            decisions.append(row)
            pending = None
            continue
        delay = OFFICIAL_DELAY_RE.search(line)
        if delay:
            delays.append({
                "wait_sec": float(delay.group("wait")),
                "target_sec": float(delay.group("target")),
            })
            continue
        send = SEND_RE.search(line)
        if send:
            sends.append({
                "hand": int(send.group("hand")),
                "stage": send.group("stage"),
                "msg": send.group("msg").strip(),
            })
            continue
        if "DECIDE exception" in line or "Traceback" in line:
            exceptions += 1

    decision_values = [
        float(row["elapsed_sec"])
        for row in decisions
        if isinstance(row.get("elapsed_sec"), (int, float))
    ]
    delay_values = [
        float(row["wait_sec"])
        for row in delays
        if isinstance(row.get("wait_sec"), (int, float))
    ]
    delay_targets = [
        float(row["target_sec"])
        for row in delays
        if isinstance(row.get("target_sec"), (int, float))
    ]
    trusted_by_decision: dict[int, dict[str, float | int]] = {}
    for row in refinements:
        decision = trusted_by_decision.setdefault(
            int(row["decision_id"]), {"steps": 0, "cpu_ms": 0.0}
        )
        if isinstance(row.get("trusted_step"), int):
            decision["steps"] = max(
                int(decision["steps"]), int(row["trusted_step"])
            )
        if isinstance(row.get("trusted_cpu_ms"), (int, float)):
            decision["cpu_ms"] = max(
                float(decision["cpu_ms"]), float(row["trusted_cpu_ms"])
            )
    for row in refinement_done:
        decision = trusted_by_decision.setdefault(
            int(row["decision_id"]), {"steps": 0, "cpu_ms": 0.0}
        )
        decision["steps"] = max(
            int(decision["steps"]), int(row.get("trusted_steps", 0) or 0)
        )
        decision["cpu_ms"] = max(
            float(decision["cpu_ms"]), float(row.get("trusted_cpu_ms", 0.0) or 0.0)
        )
    refinement_decision_ids = {
        int(row["decision_id"]) for row in refinements
    } | {
        int(row["decision_id"])
        for row in refinement_done
        if int(row.get("trusted_steps", 0) or 0) > 0
    }
    trusted_by_decision = {
        decision_id: row
        for decision_id, row in trusted_by_decision.items()
        if decision_id in refinement_decision_ids
    }
    termination_reasons: dict[str, int] = {}
    for row in [*refinement_done, *deadline_terminations]:
        if int(row["decision_id"]) not in refinement_decision_ids:
            continue
        reason = str(row.get("termination") or "unknown")
        termination_reasons[reason] = termination_reasons.get(reason, 0) + 1
    trusted_cpu_seconds = [
        float(row["cpu_ms"]) / 1000.0 for row in trusted_by_decision.values()
    ]
    reported_samples = [
        int(row["reported_samples"])
        for row in refinements
        if isinstance(row.get("reported_samples"), int)
    ]
    reported_confidences = [
        float(row["reported_confidence"])
        for row in refinements
        if isinstance(row.get("reported_confidence"), (int, float))
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "bot_log",
        "decision_latency": {
            **summarize_durations(decision_values, budget_sec=DECISION_BUDGET_SEC),
            "by_stage": _summarize_rows_by_key(decisions, "elapsed_sec", "stage"),
            "by_hand_bucket": _summarize_rows_by_key(
                [
                    {**row, "hand_bucket": _hand_bucket(row.get("hand"))}
                    for row in decisions
                ],
                "elapsed_sec",
                "hand_bucket",
            ),
        },
        "official_action_delay": {
            **summarize_durations(delay_values),
            "target_sec": _rounded(max(delay_targets)) if delay_targets else 0.0,
        },
        "refinement": {
            "message_count": len(refinements),
            "decision_count": len(trusted_by_decision),
            "trusted_steps_sum": sum(
                int(row.get("steps", 0) or 0) for row in trusted_by_decision.values()
            ),
            "trusted_steps_max": max(
                (int(row.get("steps", 0) or 0) for row in trusted_by_decision.values()),
                default=0,
            ),
            "trusted_cpu": summarize_durations(trusted_cpu_seconds),
            "iterator_exhausted_count": sum(
                1
                for row in refinement_done
                if row.get("iterator_exhausted")
                and int(row["decision_id"]) in refinement_decision_ids
            ),
            "termination_reasons": dict(sorted(termination_reasons.items())),
            "reported_sample_count_max": max(reported_samples, default=None),
            "reported_confidence_max": max(reported_confidences, default=None),
            "candidate_reported_fields_authoritative": False,
        },
        "send_count": len(sends),
        "exception_count": exceptions,
        "opponent_tracker": {
            "available": bool(opponent_tracker_snapshots),
            "snapshot_count": len(opponent_tracker_snapshots),
            "latest": (
                opponent_tracker_snapshots[-1]
                if opponent_tracker_snapshots
                else None
            ),
        },
    }


def server_action_latency(
    events: list[dict[str, Any]],
    player_idx: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    budgets: list[float] = []
    for event in events:
        if event.get("type") != "action" or event.get("player_idx") != player_idx:
            continue
        wait_sec = event.get("decision_wait_sec")
        if not isinstance(wait_sec, (int, float)):
            continue
        budget_sec = event.get("timeout_budget_sec")
        if isinstance(budget_sec, (int, float)) and float(budget_sec) > 0:
            budgets.append(float(budget_sec))
        hand = int(event.get("hand", 0) or 0)
        rows.append({
            "hand": hand,
            "hand_bucket": _hand_bucket(hand),
            "stage": str(event.get("stage") or "unknown"),
            "action": str(event.get("action") or ""),
            "decision_wait_sec": float(wait_sec),
        })
    values = [row["decision_wait_sec"] for row in rows]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "server_action_events",
        **summarize_durations(values, budget_sec=max(budgets) if budgets else None),
        "by_stage": _summarize_rows_by_key(rows, "decision_wait_sec", "stage"),
        "by_hand_bucket": _summarize_rows_by_key(
            rows, "decision_wait_sec", "hand_bucket"
        ),
        "timeout_action_count": sum(
            1 for row in rows if row.get("action") == "timeout"
        ),
        "illegal_action_count": sum(
            1 for row in rows if str(row.get("action", "")).startswith("illegal:")
        ),
    }


def merge_latency_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [item for item in summaries if isinstance(item, dict)]
    total_count = sum(int(item.get("count", 0) or 0) for item in summaries)
    total_sum = sum(float(item.get("sum_sec", 0.0) or 0.0) for item in summaries)
    max_values = [
        float(item["max_sec"])
        for item in summaries
        if isinstance(item.get("max_sec"), (int, float))
    ]
    p95_values = [
        float(item["p95_sec"])
        for item in summaries
        if isinstance(item.get("p95_sec"), (int, float))
    ]
    budget_values = [
        float(item["budget_sec"])
        for item in summaries
        if isinstance(item.get("budget_sec"), (int, float))
    ]
    merged: dict[str, Any] = {
        "count": total_count,
        "sum_sec": _rounded(total_sum),
        "mean_sec": _rounded(total_sum / total_count) if total_count else None,
        "p95_sec": _rounded(max(p95_values)) if p95_values else None,
        "p95_method": "conservative_max_of_group_p95",
        "max_sec": _rounded(max(max_values)) if max_values else None,
    }
    if budget_values:
        budget_sec = max(budget_values)
        merged["budget_sec"] = _rounded(budget_sec)
        merged["budget_utilization_max"] = _rounded(
            (max(max_values) / budget_sec) if max_values else 0.0
        )
    return merged


def empty_runtime_telemetry() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "native_tcp_acceptance",
        "server_action_latency": merge_latency_summaries([]),
        "bot_decision_latency": merge_latency_summaries([]),
        "official_action_delay": merge_latency_summaries([]),
        "refinement": {
            **_empty_refinement_summary(),
            "trusted_cpu": merge_latency_summaries([]),
        },
        "matches_with_bot_log": 0,
        "trace_decision_count": 0,
        "exception_count": 0,
    }


def merge_runtime_telemetry(rows: list[dict[str, Any]]) -> dict[str, Any]:
    merged = empty_runtime_telemetry()
    termination_reasons: dict[str, int] = {}
    for row in rows:
        reasons = (((row.get("bot_log") or {}).get("refinement") or {}).get(
            "termination_reasons"
        ) or {})
        for reason, count in reasons.items():
            termination_reasons[str(reason)] = (
                termination_reasons.get(str(reason), 0) + int(count or 0)
            )
    reported_sample_maxima = [
        value
        for row in rows
        if isinstance(
            value := (((row.get("bot_log") or {}).get("refinement") or {}).get(
                "reported_sample_count_max"
            )),
            (int, float),
        )
    ]
    reported_confidence_maxima = [
        value
        for row in rows
        if isinstance(
            value := (((row.get("bot_log") or {}).get("refinement") or {}).get(
                "reported_confidence_max"
            )),
            (int, float),
        )
    ]
    merged.update({
        "server_action_latency": merge_latency_summaries([
            row.get("server_action_latency", {}) for row in rows
        ]),
        "bot_decision_latency": merge_latency_summaries([
            ((row.get("bot_log") or {}).get("decision_latency") or {})
            for row in rows
        ]),
        "official_action_delay": merge_latency_summaries([
            ((row.get("bot_log") or {}).get("official_action_delay") or {})
            for row in rows
        ]),
        "refinement": {
            "message_count": sum(
                int((((row.get("bot_log") or {}).get("refinement") or {}).get("message_count", 0)) or 0)
                for row in rows
            ),
            "decision_count": sum(
                int((((row.get("bot_log") or {}).get("refinement") or {}).get("decision_count", 0)) or 0)
                for row in rows
            ),
            "trusted_steps_sum": sum(
                int((((row.get("bot_log") or {}).get("refinement") or {}).get("trusted_steps_sum", 0)) or 0)
                for row in rows
            ),
            "trusted_steps_max": max(
                (
                    int((((row.get("bot_log") or {}).get("refinement") or {}).get("trusted_steps_max", 0)) or 0)
                    for row in rows
                ),
                default=0,
            ),
            "trusted_cpu": merge_latency_summaries([
                (((row.get("bot_log") or {}).get("refinement") or {}).get("trusted_cpu") or {})
                for row in rows
            ]),
            "iterator_exhausted_count": sum(
                int((((row.get("bot_log") or {}).get("refinement") or {}).get("iterator_exhausted_count", 0)) or 0)
                for row in rows
            ),
            "termination_reasons": dict(sorted(termination_reasons.items())),
            "reported_sample_count_max": max(reported_sample_maxima, default=None),
            "reported_confidence_max": max(
                reported_confidence_maxima,
                default=None,
            ),
            "candidate_reported_fields_authoritative": False,
        },
        "matches_with_bot_log": sum(
            1 for row in rows if bool(row.get("bot_log_supported"))
        ),
        "trace_decision_count": sum(
            int(row.get("trace_decision_count", 0) or 0) for row in rows
        ),
        "exception_count": sum(
            int(((row.get("bot_log") or {}).get("exception_count", 0)) or 0)
            for row in rows
        ),
    })
    return merged
