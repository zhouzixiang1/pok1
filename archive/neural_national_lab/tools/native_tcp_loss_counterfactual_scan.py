#!/usr/bin/env python3
"""Scan losing native TCP matches with paired single-decision force probes.

The input is one or more `native_tcp_evaluate.py` JSON reports. For each losing
paired row, this tool reruns the exact native TCP seed with decision tracing,
selects the worst paired hands, and forces legal alternatives at candidate
decisions on those hands. It records both full-match and target-hand deltas.
No legacy national adapter path is used.
"""

from __future__ import annotations

import argparse
import asyncio
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

from feature_spec import LABELS, label_action  # noqa: E402
from national_native import run_native_tcp_pair  # noqa: E402


def _resolve(path: str | Path) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


@contextlib.contextmanager
def _probe_env(*, trace: bool = False, force: dict[str, int] | None = None):
    keys = ["POK_TRACE_DECISIONS", "POK_FORCE_HAND", "POK_FORCE_DECISION", "POK_FORCE_ACTION"]
    old = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        if trace:
            os.environ["POK_TRACE_DECISIONS"] = "1"
        if force:
            os.environ["POK_FORCE_HAND"] = str(int(force["hand"]))
            os.environ["POK_FORCE_DECISION"] = str(int(force["decision"]))
            os.environ["POK_FORCE_ACTION"] = str(int(force["action"]))
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
    trace: bool = False,
    force: dict[str, int] | None = None,
) -> dict[str, Any]:
    with _probe_env(trace=trace, force=force):
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


def _hand_net_chips(result: dict[str, Any], candidate_idx: int) -> list[int]:
    out: list[int] = []
    for row in result.get("settlements", []):
        earnings = row.get("earnings")
        if isinstance(earnings, list) and len(earnings) > candidate_idx:
            out.append(int(earnings[candidate_idx]))
    return out


def _native_trace(result: dict[str, Any], label: str) -> list[dict[str, Any]]:
    pdata = result.get("per_player", {}).get(label, {})
    native = pdata.get("native", {}) if isinstance(pdata, dict) else {}
    rows = native.get("decision_trace", [])
    return [row for row in rows if isinstance(row, dict) and row.get("type") == "decision"]


def _leg_row(result: dict[str, Any], *, candidate_is_a: bool) -> dict[str, Any]:
    bot_a = str(result["bot_a"])
    bot_b = str(result["bot_b"])
    candidate_label = bot_a if candidate_is_a else bot_b
    candidate_idx = 0 if candidate_is_a else 1
    candidate_key = candidate_label
    net_chips = int(result["net_chips_a"] if candidate_is_a else result["net_chips_b"])
    pdata = result.get("per_player", {}).get(candidate_key, {})
    return {
        "candidate_label": candidate_label,
        "net_chips": net_chips,
        "hand_net_chips": _hand_net_chips(result, candidate_idx),
        "trace": _native_trace(result, candidate_label),
        "passed_compliance": bool(result.get("passed_compliance")),
        "issues": list(result.get("issues", [])),
        "candidate_illegal": int(pdata.get("illegal_actions", 0) or 0),
        "candidate_timeouts": int(pdata.get("timeouts", 0) or 0),
        "adapter_actions_candidate": int((pdata.get("adapter") or {}).get("actions_sent", 0) or 0),
    }


async def _run_paired(
    candidate: Path,
    opponent: Path,
    *,
    hands: int,
    deck_seed_base: int | None,
    bot_seed_base: int | None,
    timeout_sec: float,
    trace: bool = False,
    force: dict[str, int] | None = None,
) -> dict[str, Any]:
    forward = await _run_pair(
        candidate,
        opponent,
        hands=hands,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
        trace=trace,
        force=force,
    )
    swapped = await _run_pair(
        opponent,
        candidate,
        hands=hands,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
        trace=trace,
        force=force,
    )
    forward_row = _leg_row(forward, candidate_is_a=True)
    swapped_row = _leg_row(swapped, candidate_is_a=False)
    min_hands = min(len(forward_row["hand_net_chips"]), len(swapped_row["hand_net_chips"]))
    paired_hands = [
        int(forward_row["hand_net_chips"][idx]) + int(swapped_row["hand_net_chips"][idx])
        for idx in range(min_hands)
    ]
    issues = [f"forward:{issue}" for issue in forward_row["issues"]]
    issues.extend(f"swapped:{issue}" for issue in swapped_row["issues"])
    return {
        "net_chips": int(forward_row["net_chips"]) + int(swapped_row["net_chips"]),
        "hand_net_chips": paired_hands,
        "passed_compliance": bool(forward_row["passed_compliance"] and swapped_row["passed_compliance"]),
        "issues": issues,
        "candidate_illegal": int(forward_row["candidate_illegal"]) + int(swapped_row["candidate_illegal"]),
        "candidate_timeouts": int(forward_row["candidate_timeouts"]) + int(swapped_row["candidate_timeouts"]),
        "adapter_actions_candidate": int(forward_row["adapter_actions_candidate"])
        + int(swapped_row["adapter_actions_candidate"]),
        "legs": [
            {"leg": "forward", **forward_row},
            {"leg": "swapped", **swapped_row},
        ],
    }


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
    to_call = max(0, int(state.get("to_call", req.get("to_call", 0)) or 0))
    pot = max(1, int(state.get("pot", req.get("pot", 150)) or 150))
    min_raise_action = max(1, int(state.get("min_raise_action", state.get("round_raise", 100)) or 100))
    my_chips = max(0, int(req.get("my_chips", 0) or 0))
    delta = max(min_raise_action, int(to_call + (pot + to_call) * float(ratio)))
    if delta <= to_call or delta >= my_chips:
        return None
    return my_bet + delta


def _legal_alternatives(row: dict[str, Any], max_alternatives: int, allowed_labels: set[int]) -> list[int]:
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
            if raise_action is not None:
                actions.append(int(raise_action))
    if not opponent_allin and final != -2:
        actions.append(-2)
    deduped: list[int] = []
    for action in actions:
        if action == final or action in deduped:
            continue
        if allowed_labels and _label_id(action, req) not in allowed_labels:
            continue
        deduped.append(int(action))
        if len(deduped) >= max(1, int(max_alternatives)):
            break
    return deduped


def _stage_name(row: dict[str, Any]) -> str:
    req = row.get("request") or {}
    public_cards = req.get("public_cards") or []
    n = len(public_cards)
    if n >= 5:
        return "river"
    if n == 4:
        return "turn"
    if n >= 3:
        return "flop"
    return "preflop"


def _decision_key(row: dict[str, Any]) -> tuple[int, int]:
    return int(row.get("hand", 0) or 0), int(row.get("hand_decision_index", 0) or 0)


def _iter_candidate_decisions(
    baseline: dict[str, Any],
    *,
    target_hands: set[int],
    stage_filter: set[str],
    max_decisions_per_hand: int,
) -> list[dict[str, Any]]:
    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    counts: dict[int, int] = {}
    for leg in baseline.get("legs", []):
        for row in leg.get("trace", []):
            hand = int(row.get("hand", 0) or 0)
            if hand not in target_hands:
                continue
            stage = _stage_name(row)
            if stage_filter and stage not in stage_filter:
                continue
            if counts.get(hand, 0) >= max(1, int(max_decisions_per_hand)):
                continue
            key = _decision_key(row)
            if key in by_key:
                continue
            by_key[key] = row
            counts[hand] = counts.get(hand, 0) + 1
    return [by_key[key] for key in sorted(by_key)]


def _load_rows(report_paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for report_path in report_paths:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        candidate_path = payload.get("candidate_path")
        hands = int(payload.get("hands_per_match", 70) or 70)
        for idx, row in enumerate(payload.get("rows", [])):
            if not isinstance(row, dict):
                continue
            rows.append({
                **row,
                "source_report": str(report_path),
                "source_row_index": idx,
                "candidate_path": candidate_path,
                "hands_per_match": hands,
            })
    return rows


def _worst_hands(hand_net_chips: list[Any], max_hands: int) -> list[int]:
    indexed = [(idx + 1, int(value)) for idx, value in enumerate(hand_net_chips)]
    losing = [item for item in indexed if item[1] < 0]
    return [hand for hand, _value in sorted(losing, key=lambda item: item[1])[: max(1, int(max_hands))]]


def _summarize(probes: list[dict[str, Any]]) -> dict[str, Any]:
    values = [int(row["full_match_delta"]) for row in probes if row.get("status") == "ok"]
    positives = [row for row in probes if row.get("status") == "ok" and int(row["full_match_delta"]) > 0]
    return {
        "probes": len(probes),
        "ok_probes": len(values),
        "positive_full_match": len(positives),
        "best_full_match_delta": max(values) if values else None,
        "mean_full_match_delta": round(statistics.mean(values), 3) if values else 0.0,
        "best": sorted(
            positives,
            key=lambda row: (int(row["full_match_delta"]), int(row["target_hand_delta"])),
            reverse=True,
        )[:20],
    }


async def _scan(args: argparse.Namespace) -> dict[str, Any]:
    report_paths = [_resolve(path) for path in args.evaluation]
    rows = _load_rows(report_paths)
    losing_rows = [row for row in rows if int(row.get("net_chips", 0) or 0) < 0]
    losing_rows.sort(key=lambda row: int(row.get("net_chips", 0) or 0))
    if int(args.limit_rows) > 0:
        losing_rows = losing_rows[: int(args.limit_rows)]
    stage_filter = set(args.stage)
    allowed_labels = {int(LABELS.index(label)) for label in args.alternative_label}
    probes: list[dict[str, Any]] = []
    baselines: list[dict[str, Any]] = []

    for row_idx, row in enumerate(losing_rows):
        candidate = _resolve(args.candidate or str(row.get("candidate_path", "")))
        opponent = _resolve(str(row["opponent_path"]))
        deck_seed = row.get("deck_seed_base")
        bot_seed = row.get("bot_seed_base")
        baseline = await _run_paired(
            candidate,
            opponent,
            hands=int(args.hands or row.get("hands_per_match", 70) or 70),
            deck_seed_base=int(deck_seed) if deck_seed is not None else None,
            bot_seed_base=int(bot_seed) if bot_seed is not None else None,
            timeout_sec=float(args.timeout_sec),
            trace=True,
        )
        target_hands = set(_worst_hands(baseline["hand_net_chips"], int(args.max_hands_per_row)))
        decisions = _iter_candidate_decisions(
            baseline,
            target_hands=target_hands,
            stage_filter=stage_filter,
            max_decisions_per_hand=int(args.max_decisions_per_hand),
        )
        baselines.append({
            "row_index": row_idx,
            "opponent": row.get("opponent"),
            "opponent_path": str(opponent),
            "deck_seed_base": deck_seed,
            "bot_seed_base": bot_seed,
            "source_net_chips": int(row.get("net_chips", 0) or 0),
            "baseline_net_chips": int(baseline["net_chips"]),
            "baseline_passed_compliance": bool(baseline["passed_compliance"]),
            "baseline_issues": baseline["issues"],
            "target_hands": sorted(target_hands),
            "decisions": len(decisions),
        })
        for decision in decisions:
            alternatives = _legal_alternatives(decision, int(args.max_alternatives), allowed_labels)
            hand = int(decision.get("hand", 0) or 0)
            decision_idx = int(decision.get("hand_decision_index", 0) or 0)
            baseline_hand = int(baseline["hand_net_chips"][hand - 1]) if 0 < hand <= len(baseline["hand_net_chips"]) else 0
            for action in alternatives:
                forced = await _run_paired(
                    candidate,
                    opponent,
                    hands=int(args.hands or row.get("hands_per_match", 70) or 70),
                    deck_seed_base=int(deck_seed) if deck_seed is not None else None,
                    bot_seed_base=int(bot_seed) if bot_seed is not None else None,
                    timeout_sec=float(args.timeout_sec),
                    trace=False,
                    force={"hand": hand, "decision": decision_idx, "action": int(action)},
                )
                forced_hand = (
                    int(forced["hand_net_chips"][hand - 1])
                    if 0 < hand <= len(forced["hand_net_chips"])
                    else None
                )
                status = "ok"
                if not forced["passed_compliance"] or forced["candidate_illegal"] or forced["adapter_actions_candidate"]:
                    status = "forced_issues"
                probes.append({
                    "status": status,
                    "row_index": row_idx,
                    "source_report": row.get("source_report"),
                    "source_row_index": row.get("source_row_index"),
                    "candidate": _rel(candidate),
                    "opponent": row.get("opponent"),
                    "opponent_path": _rel(opponent),
                    "deck_seed_base": deck_seed,
                    "bot_seed_base": bot_seed,
                    "hand": hand,
                    "hand_decision_index": decision_idx,
                    "decision_serial": int(decision.get("decision_serial", 0) or 0),
                    "stage": _stage_name(decision),
                    "rule_action": int(decision.get("rule_action", 0) or 0),
                    "final_action": int(decision.get("final_action", 0) or 0),
                    "forced_action": int(action),
                    "forced_label": LABELS[_label_id(int(action), decision.get("request") or {})],
                    "baseline_net_chips": int(baseline["net_chips"]),
                    "forced_net_chips": int(forced["net_chips"]),
                    "full_match_delta": int(forced["net_chips"]) - int(baseline["net_chips"]),
                    "baseline_hand_net_chips": baseline_hand,
                    "forced_hand_net_chips": forced_hand,
                    "target_hand_delta": None if forced_hand is None else int(forced_hand) - baseline_hand,
                    "candidate_illegal": int(forced["candidate_illegal"]),
                    "candidate_timeouts": int(forced["candidate_timeouts"]),
                    "adapter_actions_candidate": int(forced["adapter_actions_candidate"]),
                    "issues": forced["issues"],
                    "request": decision.get("request") or {},
                    "state": decision.get("state") or {},
                })
                print(
                    f"{row_idx + 1}/{len(losing_rows)} {row.get('opponent')} seed={deck_seed} "
                    f"h{hand} d{decision_idx} action={action} "
                    f"delta={probes[-1]['full_match_delta']} status={status}",
                    flush=True,
                )
    return {
        "mode": "native_tcp_loss_counterfactual_scan_v1",
        "input_reports": [str(path) for path in report_paths],
        "filters": {
            "limit_rows": int(args.limit_rows),
            "max_hands_per_row": int(args.max_hands_per_row),
            "max_decisions_per_hand": int(args.max_decisions_per_hand),
            "max_alternatives": int(args.max_alternatives),
            "stage": list(args.stage),
            "alternative_label": list(args.alternative_label),
        },
        "baselines": baselines,
        "probes": probes,
        "summary": _summarize(probes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan losing native TCP evaluation rows with force probes.")
    parser.add_argument("--evaluation", action="append", required=True, help="native_tcp_evaluate.py JSON report. Repeatable.")
    parser.add_argument("--candidate", default="", help="Override candidate path. Defaults to report candidate_path.")
    parser.add_argument("--hands", type=int, default=0, help="Override hands per match. Defaults to report hands_per_match.")
    parser.add_argument("--limit-rows", type=int, default=0)
    parser.add_argument("--max-hands-per-row", type=int, default=1)
    parser.add_argument("--max-decisions-per-hand", type=int, default=4)
    parser.add_argument("--max-alternatives", type=int, default=3)
    parser.add_argument("--stage", action="append", choices=["preflop", "flop", "turn", "river"], default=[])
    parser.add_argument("--alternative-label", action="append", choices=list(LABELS), default=[])
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = asyncio.run(_scan(args))
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
