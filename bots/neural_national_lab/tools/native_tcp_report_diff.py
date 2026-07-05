#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _row_key(row: dict[str, Any]) -> tuple[str, int]:
    return str(row["opponent"]), int(row["match_idx"])


def _stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {
            "samples": 0,
            "sum": 0,
            "mean": 0.0,
            "median": 0.0,
            "min": None,
            "max": None,
            "positive": 0,
            "negative": 0,
            "zero": 0,
        }
    return {
        "samples": len(values),
        "sum": int(sum(values)),
        "mean": round(statistics.mean(values), 3),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "positive": sum(1 for value in values if value > 0),
        "negative": sum(1 for value in values if value < 0),
        "zero": sum(1 for value in values if value == 0),
    }


def _top(rows: list[dict[str, Any]], reverse: bool, limit: int) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: int(row["delta_net_chips"]), reverse=reverse)
    out = []
    for row in ordered[: max(0, limit)]:
        out.append({
            "opponent": row["opponent"],
            "match_idx": row["match_idx"],
            "deck_seed_base": row["deck_seed_base"],
            "candidate_net_chips": row["candidate_net_chips"],
            "baseline_net_chips": row["baseline_net_chips"],
            "delta_net_chips": row["delta_net_chips"],
            "hand_delta_sum": row["hand_delta_sum"],
            "largest_hand_delta": row["largest_hand_delta"],
            "smallest_hand_delta": row["smallest_hand_delta"],
        })
    return out


def _diff_rows(candidate: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    candidate_rows = {_row_key(row): row for row in candidate.get("rows", [])}
    baseline_rows = {_row_key(row): row for row in baseline.get("rows", [])}
    missing = sorted(set(candidate_rows) ^ set(baseline_rows))
    if missing:
        raise SystemExit(f"reports have different row keys: {missing[:10]}")
    rows: list[dict[str, Any]] = []
    for key in sorted(candidate_rows):
        cand = candidate_rows[key]
        base = baseline_rows[key]
        cand_hands = [int(value) for value in cand.get("hand_net_chips", [])]
        base_hands = [int(value) for value in base.get("hand_net_chips", [])]
        hand_deltas = [
            cand_hands[idx] - base_hands[idx]
            for idx in range(min(len(cand_hands), len(base_hands)))
        ]
        delta = int(cand["net_chips"]) - int(base["net_chips"])
        rows.append({
            "opponent": key[0],
            "match_idx": key[1],
            "deck_seed_base": cand.get("deck_seed_base"),
            "candidate_net_chips": int(cand["net_chips"]),
            "baseline_net_chips": int(base["net_chips"]),
            "delta_net_chips": delta,
            "candidate_passed_compliance": bool(cand.get("passed_compliance")),
            "baseline_passed_compliance": bool(base.get("passed_compliance")),
            "candidate_issues": cand.get("issues", []),
            "baseline_issues": base.get("issues", []),
            "hand_delta_count": len(hand_deltas),
            "hand_delta_sum": int(sum(hand_deltas)),
            "largest_hand_delta": max(hand_deltas) if hand_deltas else None,
            "smallest_hand_delta": min(hand_deltas) if hand_deltas else None,
            "hand_deltas": hand_deltas,
        })
    return rows


def _summary(rows: list[dict[str, Any]], candidate: dict[str, Any], baseline: dict[str, Any], limit: int) -> dict[str, Any]:
    by_opponent: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_opponent.setdefault(row["opponent"], {"rows": []})["rows"].append(row)
    for opponent, payload in by_opponent.items():
        subset = payload.pop("rows")
        deltas = [int(row["delta_net_chips"]) for row in subset]
        payload.update(_stats(deltas))
        payload["worst"] = _top(subset, reverse=False, limit=limit)
        payload["best"] = _top(subset, reverse=True, limit=limit)
    all_deltas = [int(row["delta_net_chips"]) for row in rows]
    return {
        "candidate_report": candidate.get("candidate_path"),
        "baseline_report": baseline.get("candidate_path"),
        "candidate_paired": bool(candidate.get("paired")),
        "baseline_paired": bool(baseline.get("paired")),
        "hands_per_match": candidate.get("hands_per_match"),
        "rows": len(rows),
        "combined": _stats(all_deltas),
        "opponents": by_opponent,
        "worst": _top(rows, reverse=False, limit=limit),
        "best": _top(rows, reverse=True, limit=limit),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two native TCP evaluator reports on matching opponent/match_idx rows.")
    parser.add_argument("--candidate-report", required=True, type=Path)
    parser.add_argument("--baseline-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args()

    candidate = _load(args.candidate_report)
    baseline = _load(args.baseline_report)
    rows = _diff_rows(candidate, baseline)
    payload = _summary(rows, candidate, baseline, args.top)
    payload["diff_rows"] = rows
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("combined", "opponents", "worst", "best")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
