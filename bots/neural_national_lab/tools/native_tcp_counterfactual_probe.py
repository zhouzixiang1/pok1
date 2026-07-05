#!/usr/bin/env python3
"""Collect native TCP single-decision counterfactual action-value rows.

The probe runs a trace-capable native bot once as the baseline, then replays
the same deck/bot seed while forcing one alternative final action at one
decision. Labels are hand-scoped deltas versus the baseline action on that
same hand. This stays inside the national TCP protocol and does not use the
legacy national adapter.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import contextlib
import json
import os
from pathlib import Path
import statistics
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
WEB_CORE = ROOT / "web" / "core"
TOOLS = Path(__file__).resolve().parent
for path in (WEB_CORE, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from feature_spec import LABELS, encode_features, label_action  # noqa: E402
from national_native import run_native_tcp_pair  # noqa: E402


def _resolve(path: str) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


@contextlib.contextmanager
def _probe_env(force: dict[str, int | None] | None = None):
    keys = ["POK_TRACE_DECISIONS", "POK_FORCE_HAND", "POK_FORCE_DECISION", "POK_FORCE_ACTION"]
    old = {key: os.environ.get(key) for key in keys}
    try:
        os.environ["POK_TRACE_DECISIONS"] = "1"
        for key in keys[1:]:
            os.environ.pop(key, None)
        if force:
            mapping = {
                "POK_FORCE_HAND": force.get("hand"),
                "POK_FORCE_DECISION": force.get("decision"),
                "POK_FORCE_ACTION": force.get("action"),
            }
            for key, value in mapping.items():
                if value is not None:
                    os.environ[key] = str(int(value))
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _run_pair(
    candidate: Path,
    opponent: Path,
    *,
    hands: int,
    deck_seed_base: int | None,
    bot_seed_base: int | None,
    timeout_sec: float,
    force: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    with _probe_env(force):
        return await run_native_tcp_pair(
            candidate,
            opponent,
            hands,
            require_native_a=True,
            require_native_b=True,
            deck_seed_base=deck_seed_base,
            bot_seed_base=bot_seed_base,
            timeout_sec=timeout_sec,
        )


def _stage_name(row: dict[str, Any]) -> str:
    req = row.get("request") or {}
    n = len(req.get("public_cards") or [])
    if n >= 5:
        return "river"
    if n == 4:
        return "turn"
    if n >= 3:
        return "flop"
    return "preflop"


def _settlement_map(result: dict[str, Any], player_idx: int) -> dict[int, int]:
    out: dict[int, int] = {}
    for item in result.get("settlements", []):
        earnings = item.get("earnings")
        if not isinstance(earnings, list) or len(earnings) <= player_idx:
            continue
        out[int(item.get("hand", 0) or 0)] = int(earnings[player_idx])
    return out


def _trace_rows(result: dict[str, Any], label: str) -> list[dict[str, Any]]:
    pdata = result.get("per_player", {}).get(label, {})
    native = pdata.get("native", {}) if isinstance(pdata, dict) else {}
    rows = native.get("decision_trace", [])
    return [row for row in rows if isinstance(row, dict) and row.get("type") == "decision"]


def _label_id(action: int, req: dict[str, Any]) -> int:
    try:
        return int(label_action(int(action), req, None))
    except Exception:
        if action == -1:
            return 0
        if action == -2:
            return 5
        return 1 if action == 0 else 3


def _raise_total(req: dict[str, Any], state: dict[str, Any], ratio: float) -> int | None:
    my_bet = int(state.get("my_round_bet", req.get("my_stage_bet", 0)) or 0)
    to_call = max(0, int(state.get("to_call", 0) or 0))
    pot = max(1, int(state.get("pot", req.get("pot", 150)) or 150))
    min_raise_action = max(1, int(state.get("min_raise_action", state.get("round_raise", 100)) or 100))
    my_chips = max(0, int(req.get("my_chips", 0) or 0))
    delta = max(min_raise_action, int(to_call + (pot + to_call) * float(ratio)))
    if delta <= to_call or delta >= my_chips:
        return None
    return my_bet + delta


def _legal_alternatives(row: dict[str, Any], max_alternatives: int) -> list[int]:
    req = row.get("request") or {}
    state = row.get("state") or {}
    final = int(row.get("final_action", 0) or 0)
    to_call = max(0, int(state.get("to_call", req.get("to_call", 0)) or 0))
    opponent_allin = bool(state.get("opponent_allin") or req.get("opponent_allin"))
    actions: list[int] = []
    if to_call > 0 and final != -1:
        actions.append(-1)
    if final != 0:
        actions.append(0)
    if not opponent_allin:
        for ratio in (0.5, 1.0):
            raise_action = _raise_total(req, state, ratio)
            if raise_action is not None and raise_action != final:
                actions.append(int(raise_action))
    if not opponent_allin and final != -2:
        actions.append(-2)
    deduped: list[int] = []
    for action in actions:
        if action == final or action in deduped:
            continue
        deduped.append(action)
        if len(deduped) >= max(1, int(max_alternatives)):
            break
    return deduped


def _empty_targets() -> list[float | None]:
    return [None for _ in LABELS]


def _target_mask(targets: list[float | None]) -> list[int]:
    return [1 if value is not None else 0 for value in targets]


def _row_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    by_label: dict[str, list[float]] = defaultdict(list)
    statuses = Counter(str(row.get("status")) for row in rows)
    for row in rows:
        for idx, value in enumerate(row.get("delta_vs_rule", [])):
            if value is None:
                continue
            if idx == int(row.get("rule_label_id", -1)):
                continue
            values.append(float(value))
            by_label[LABELS[idx]].append(float(value))
    return {
        "rows": len(rows),
        "target_samples": len(values),
        "mean_delta": round(statistics.mean(values), 3) if values else 0.0,
        "median_delta": statistics.median(values) if values else 0.0,
        "positive": sum(1 for value in values if value > 0),
        "negative": sum(1 for value in values if value < 0),
        "zero": sum(1 for value in values if value == 0),
        "status_counts": dict(sorted(statuses.items())),
        "by_label": {
            label: {
                "samples": len(label_values),
                "mean_delta": round(statistics.mean(label_values), 3) if label_values else 0.0,
                "positive": sum(1 for value in label_values if value > 0),
                "negative": sum(1 for value in label_values if value < 0),
                "zero": sum(1 for value in label_values if value == 0),
            }
            for label, label_values in sorted(by_label.items())
        },
    }


async def _collect(args: argparse.Namespace) -> dict[str, Any]:
    candidate = _resolve(args.candidate)
    opponent = _resolve(args.opponent)
    baseline = await _run_pair(
        candidate,
        opponent,
        hands=int(args.hands),
        deck_seed_base=args.seed_base,
        bot_seed_base=args.bot_seed_base,
        timeout_sec=float(args.timeout_sec),
    )
    candidate_label = baseline["bot_a"]
    trace = _trace_rows(baseline, candidate_label)
    settlements = _settlement_map(baseline, 0)
    rows: list[dict[str, Any]] = []
    probe_details: list[dict[str, Any]] = []
    stage_filter = None if args.stage == "any" else str(args.stage)
    selected = 0

    for decision in trace:
        if selected >= int(args.max_decisions):
            break
        stage = _stage_name(decision)
        if stage_filter and stage != stage_filter:
            continue
        hand = int(decision.get("hand", 0) or 0)
        if hand <= 0 or hand not in settlements:
            continue
        alternatives = _legal_alternatives(decision, int(args.max_alternatives))
        if not alternatives:
            continue
        req = decision.get("request") or {}
        final_action = int(decision.get("final_action", 0) or 0)
        rule_label = _label_id(final_action, req)
        targets = _empty_targets()
        targets[rule_label] = 0.0
        action_values = _empty_targets()
        action_values[rule_label] = float(settlements[hand])
        legal_mask = [0 for _ in LABELS]
        legal_mask[rule_label] = 1
        probe_records: list[dict[str, Any]] = []
        for alt in alternatives:
            forced = await _run_pair(
                candidate,
                opponent,
                hands=int(args.hands),
                deck_seed_base=args.seed_base,
                bot_seed_base=args.bot_seed_base,
                timeout_sec=float(args.timeout_sec),
                force={
                    "hand": hand,
                    "decision": int(decision.get("hand_decision_index", 0) or 0),
                    "action": int(alt),
                },
            )
            forced_settlements = _settlement_map(forced, 0)
            forced_value = forced_settlements.get(hand)
            alt_label = _label_id(int(alt), req)
            legal_mask[alt_label] = 1
            if forced_value is None:
                status = "missing_forced_settlement"
                delta = None
            else:
                delta = int(forced_value) - int(settlements[hand])
                status = "ok" if forced.get("passed_compliance") else "forced_issues"
                existing = targets[alt_label]
                if existing is None or float(delta) > float(existing):
                    targets[alt_label] = float(delta)
                    action_values[alt_label] = float(forced_value)
            probe_records.append({
                "forced_action": int(alt),
                "forced_label": LABELS[alt_label],
                "forced_hand_earnings": forced_value,
                "delta_vs_rule": delta,
                "status": status,
                "issues": forced.get("issues", []),
                "illegal_actions": forced.get("per_player", {}).get(candidate_label, {}).get("illegal_actions"),
            })
        if sum(_target_mask(targets)) <= 1:
            continue
        selected += 1
        features = [float(value) for value in encode_features(req, None)]
        train_targets = [0.0 if value is None else float(value) for value in targets]
        row = {
            "status": "ok" if all(record["status"] == "ok" for record in probe_records) else "partial",
            "opponent": baseline["bot_b"],
            "deck_seed_base": args.seed_base,
            "bot_seed_base": args.bot_seed_base,
            "hand": hand,
            "stage": stage,
            "hand_decision_index": int(decision.get("hand_decision_index", 0) or 0),
            "decision_serial": int(decision.get("decision_serial", 0) or 0),
            "rule_final": final_action,
            "rule_label_id": rule_label,
            "rule_label": LABELS[rule_label],
            "rule_value": int(settlements[hand]),
            "state_features": features,
            "delta_vs_rule": targets,
            "action_values": action_values,
            "target_mask": _target_mask(targets),
            "legal_mask": legal_mask,
            "features": features,
            "targets": train_targets,
            "raw_targets": train_targets,
            "feature_set": "state",
            "target": "delta_vs_rule",
            "weight": max(0.05, min(5.0, max(abs(value) for value in train_targets) / 1000.0)),
            "request": req,
            "state": decision.get("state") or {},
            "probes": probe_records,
        }
        rows.append(row)
        probe_details.extend(probe_records)

    return {
        "execution_mode": "native_tcp_counterfactual",
        "candidate_path": str(candidate),
        "opponent_path": str(opponent),
        "hands": int(args.hands),
        "deck_seed_base": args.seed_base,
        "bot_seed_base": args.bot_seed_base,
        "trace_decisions": len(trace),
        "baseline_net_chips": baseline.get("net_chips_a"),
        "baseline_passed_compliance": baseline.get("passed_compliance"),
        "baseline_issues": baseline.get("issues", []),
        "rows": rows,
        "summary": _row_summary(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Native TCP single-decision counterfactual probe.")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--opponent", required=True)
    parser.add_argument("--hands", type=int, default=10)
    parser.add_argument("--seed-base", type=int, default=None)
    parser.add_argument("--bot-seed-base", type=int, default=None)
    parser.add_argument("--stage", choices=["any", "preflop", "flop", "turn", "river"], default="any")
    parser.add_argument("--max-decisions", type=int, default=8)
    parser.add_argument("--max-alternatives", type=int, default=2)
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jsonl-output", type=Path)
    args = parser.parse_args()

    payload = asyncio.run(_collect(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.jsonl_output:
        args.jsonl_output.parent.mkdir(parents=True, exist_ok=True)
        args.jsonl_output.write_text(
            "\n".join(json.dumps(row, separators=(",", ":")) for row in payload["rows"]) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
