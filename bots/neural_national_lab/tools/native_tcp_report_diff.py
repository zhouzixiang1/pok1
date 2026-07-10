#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
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


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _bootstrap_mean_ci(values: list[int], *, samples: int, seed: int) -> dict[str, Any]:
    if not values:
        return {"samples": 0, "resamples": samples, "seed": seed, "low": 0.0, "high": 0.0}
    rng = random.Random(seed)
    count = len(values)
    means = [
        sum(values[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(max(1, samples))
    ]
    return {
        "samples": count,
        "resamples": max(1, samples),
        "seed": seed,
        "confidence": 0.95,
        "low": round(_percentile(means, 0.025), 3),
        "high": round(_percentile(means, 0.975), 3),
    }


def _stratified_bootstrap_mean_ci(
    groups: dict[str, list[int]], *, samples: int, seed: int
) -> dict[str, Any]:
    populated = {name: values for name, values in groups.items() if values}
    if not populated:
        return {"groups": 0, "resamples": samples, "seed": seed, "low": 0.0, "high": 0.0}
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(max(1, samples)):
        sampled: list[int] = []
        for values in populated.values():
            sampled.extend(values[rng.randrange(len(values))] for _ in values)
        means.append(sum(sampled) / len(sampled))
    return {
        "groups": len(populated),
        "samples": sum(len(values) for values in populated.values()),
        "resamples": max(1, samples),
        "seed": seed,
        "confidence": 0.95,
        "low": round(_percentile(means, 0.025), 3),
        "high": round(_percentile(means, 0.975), 3),
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
        for field in ("deck_seed_base", "bot_seed_base", "hands_played", "leg"):
            if cand.get(field) != base.get(field):
                raise SystemExit(
                    f"report metadata mismatch for {key}: {field} "
                    f"candidate={cand.get(field)!r} baseline={base.get(field)!r}"
                )
        cand_hands = [int(value) for value in cand.get("hand_net_chips", [])]
        base_hands = [int(value) for value in base.get("hand_net_chips", [])]
        if len(cand_hands) != len(base_hands):
            raise SystemExit(
                f"report hand vector mismatch for {key}: "
                f"candidate={len(cand_hands)} baseline={len(base_hands)}"
            )
        hand_deltas = [
            cand_hands[idx] - base_hands[idx]
            for idx in range(min(len(cand_hands), len(base_hands)))
        ]
        delta = int(cand["net_chips"]) - int(base["net_chips"])
        rows.append({
            "opponent": key[0],
            "match_idx": key[1],
            "deck_seed_base": cand.get("deck_seed_base"),
            "bot_seed_base": cand.get("bot_seed_base"),
            "hands_played": int(cand.get("hands_played", 0) or 0),
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


def _summary(
    rows: list[dict[str, Any]],
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    limit: int,
    *,
    bootstrap_samples: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    by_opponent: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_opponent.setdefault(row["opponent"], {"rows": []})["rows"].append(row)
    for opponent, payload in by_opponent.items():
        subset = payload.pop("rows")
        deltas = [int(row["delta_net_chips"]) for row in subset]
        hands = sum(int(row["hands_played"]) for row in subset)
        payload.update(_stats(deltas))
        payload["hands"] = hands
        payload["delta_per_hand"] = round(sum(deltas) / max(1, hands), 6)
        payload["bootstrap_mean_paired_chips"] = _bootstrap_mean_ci(
            deltas,
            samples=bootstrap_samples,
            seed=bootstrap_seed,
        )
        payload["worst"] = _top(subset, reverse=False, limit=limit)
        payload["best"] = _top(subset, reverse=True, limit=limit)
    all_deltas = [int(row["delta_net_chips"]) for row in rows]
    all_hands = sum(int(row["hands_played"]) for row in rows)
    grouped = {
        opponent: [int(row["delta_net_chips"]) for row in rows if row["opponent"] == opponent]
        for opponent in by_opponent
    }
    combined = _stats(all_deltas)
    combined["hands"] = all_hands
    combined["delta_per_hand"] = round(sum(all_deltas) / max(1, all_hands), 6)
    combined["bootstrap_mean_paired_chips"] = _bootstrap_mean_ci(
        all_deltas,
        samples=bootstrap_samples,
        seed=bootstrap_seed,
    )
    combined["stratified_bootstrap_mean_paired_chips"] = _stratified_bootstrap_mean_ci(
        grouped,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 1,
    )
    return {
        "candidate_report": candidate.get("candidate_path"),
        "baseline_report": baseline.get("candidate_path"),
        "candidate_paired": bool(candidate.get("paired")),
        "baseline_paired": bool(baseline.get("paired")),
        "hands_per_match": candidate.get("hands_per_match"),
        "rows": len(rows),
        "combined": combined,
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
    parser.add_argument("--bootstrap-samples", type=int, default=20_000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260710)
    args = parser.parse_args()

    candidate = _load(args.candidate_report)
    baseline = _load(args.baseline_report)
    rows = _diff_rows(candidate, baseline)
    payload = _summary(
        rows,
        candidate,
        baseline,
        args.top,
        bootstrap_samples=max(1, args.bootstrap_samples),
        bootstrap_seed=args.bootstrap_seed,
    )
    payload["diff_rows"] = rows
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    compact_opponents = {
        name: {key: value for key, value in stats.items() if key not in {"worst", "best"}}
        for name, stats in payload["opponents"].items()
    }
    print(json.dumps({"combined": payload["combined"], "opponents": compact_opponents}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
