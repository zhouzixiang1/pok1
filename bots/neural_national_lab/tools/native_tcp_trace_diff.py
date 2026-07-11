#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _only_row(report: dict[str, Any]) -> dict[str, Any]:
    rows = report.get("rows") or []
    if len(rows) != 1:
        raise SystemExit(f"trace report must contain exactly one paired row, got {len(rows)}")
    row = rows[0]
    if row.get("leg") != "paired" or len(row.get("legs") or []) != 2:
        raise SystemExit("trace report must be a paired native TCP result")
    return row


def _decision_rows(leg: dict[str, Any]) -> list[dict[str, Any]]:
    native = leg.get("candidate_native") or {}
    trace = native.get("decision_trace") or []
    return [row for row in trace if isinstance(row, dict) and row.get("type") == "decision"]


def _decision_key(row: dict[str, Any]) -> tuple[int, int]:
    return int(row.get("hand", 0) or 0), int(row.get("hand_decision_index", 0) or 0)


def _action_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    request = row.get("request") or {}
    state = row.get("state") or {}
    profile = request.get("opponent_profile") or {}
    return {
        "hand": int(row.get("hand", 0) or 0),
        "decision": int(row.get("hand_decision_index", 0) or 0),
        "stage": row.get("stage"),
        "rule_action": row.get("rule_action"),
        "advised_action": row.get("advised_action"),
        "final_action": row.get("final_action"),
        "cards": request.get("my_cards"),
        "board": request.get("public_cards"),
        "pot": state.get("pot"),
        "to_call": state.get("to_call"),
        "my_stage_bet": state.get("my_round_bet"),
        "opponent_stage_bet": request.get("opponent_stage_bet"),
        "opponent_profile": {
            "actions_total": profile.get("actions_total"),
            "preflop_raise_rate": profile.get("preflop_raise_rate"),
            "aggression": profile.get("aggression"),
            "raise_rate": profile.get("raise_rate"),
            "fold_rate": profile.get("fold_rate"),
        },
    }


def _hand_values(leg: dict[str, Any]) -> dict[int, int]:
    return {
        hand: int(value)
        for hand, value in enumerate(leg.get("hand_net_chips") or [], start=1)
    }


def build_diff(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    cand_row = _only_row(candidate)
    base_row = _only_row(baseline)
    for field in ("opponent", "deck_seed_base", "bot_seed_base", "hands_played", "leg"):
        if cand_row.get(field) != base_row.get(field):
            raise SystemExit(
                f"trace metadata mismatch: {field} "
                f"candidate={cand_row.get(field)!r} baseline={base_row.get(field)!r}"
            )

    action_diffs: list[dict[str, Any]] = []
    hand_diffs: list[dict[str, Any]] = []
    for cand_leg, base_leg in zip(cand_row["legs"], base_row["legs"], strict=True):
        if cand_leg.get("leg") != base_leg.get("leg"):
            raise SystemExit("forward/swapped leg order mismatch")
        leg = str(cand_leg.get("leg"))
        cand_actions = {_decision_key(row): row for row in _decision_rows(cand_leg)}
        base_actions = {_decision_key(row): row for row in _decision_rows(base_leg)}
        for key in sorted(set(cand_actions) | set(base_actions)):
            cand_action = cand_actions.get(key)
            base_action = base_actions.get(key)
            cand_final = None if cand_action is None else cand_action.get("final_action")
            base_final = None if base_action is None else base_action.get("final_action")
            if cand_final == base_final:
                continue
            action_diffs.append({
                "leg": leg,
                "hand": key[0],
                "decision": key[1],
                "candidate": _action_view(cand_action),
                "baseline": _action_view(base_action),
            })

        cand_hands = _hand_values(cand_leg)
        base_hands = _hand_values(base_leg)
        for hand in sorted(set(cand_hands) | set(base_hands)):
            cand_value = cand_hands.get(hand)
            base_value = base_hands.get(hand)
            if cand_value is None or base_value is None or cand_value == base_value:
                continue
            hand_diffs.append({
                "leg": leg,
                "hand": hand,
                "candidate_net_chips": cand_value,
                "baseline_net_chips": base_value,
                "delta_net_chips": cand_value - base_value,
            })

    first_action_by_hand: dict[tuple[str, int], dict[str, Any]] = {}
    for row in action_diffs:
        first_action_by_hand.setdefault((row["leg"], row["hand"]), row)
    for row in hand_diffs:
        row["first_action_difference"] = first_action_by_hand.get((row["leg"], row["hand"]))

    ordered_hands = sorted(hand_diffs, key=lambda row: int(row["delta_net_chips"]))
    return {
        "candidate_report": candidate.get("candidate_path"),
        "baseline_report": baseline.get("candidate_path"),
        "opponent": cand_row.get("opponent"),
        "deck_seed_base": cand_row.get("deck_seed_base"),
        "bot_seed_base": cand_row.get("bot_seed_base"),
        "candidate_net_chips": cand_row.get("net_chips"),
        "baseline_net_chips": base_row.get("net_chips"),
        "delta_net_chips": int(cand_row.get("net_chips", 0)) - int(base_row.get("net_chips", 0)),
        "action_differences": len(action_diffs),
        "changed_hands": len(hand_diffs),
        "first_action_difference": action_diffs[0] if action_diffs else None,
        "worst_hands": ordered_hands[:10],
        "best_hands": list(reversed(ordered_hands[-10:])),
        "action_diff_rows": action_diffs,
        "hand_diff_rows": ordered_hands,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare candidate and baseline native TCP decision traces.")
    parser.add_argument("--candidate-report", required=True, type=Path)
    parser.add_argument("--baseline-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    payload = build_diff(_load(args.candidate_report), _load(args.baseline_report))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in (
        "opponent",
        "deck_seed_base",
        "bot_seed_base",
        "delta_net_chips",
        "action_differences",
        "changed_hands",
        "first_action_difference",
        "worst_hands",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
