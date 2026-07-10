"""Deterministic runtime telemetry for native national TCP matches.

The local national server and generated native entrypoint expose two clocks:
server wait time includes transport and scheduling, while bot decision time
measures strategy work. Keeping both sources separate makes timeout feedback
useful without treating the official EXE as a strength evaluator.
"""

from __future__ import annotations

import re
import statistics
from typing import Any


SCHEMA_VERSION = 1
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
        "send_count": 0,
        "exception_count": 0,
    }


def parse_native_bot_log(log_text: str) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    delays: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    exceptions = 0
    for line in log_text.splitlines():
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
        "send_count": len(sends),
        "exception_count": exceptions,
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
        "matches_with_bot_log": 0,
        "trace_decision_count": 0,
        "exception_count": 0,
    }


def merge_runtime_telemetry(rows: list[dict[str, Any]]) -> dict[str, Any]:
    merged = empty_runtime_telemetry()
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
