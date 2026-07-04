#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]


STAGES = ("preflop", "flop", "turn", "river")
ACTION_BUCKETS = ("fold", "call", "raise_small", "raise_big", "allin")


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if value < lo else hi if value > hi else value


def _rank(card: int) -> int:
    return int(card) // 4 + 2


def _suit(card: int) -> int:
    return int(card) % 4


def _action_bucket(action: int) -> str:
    if action == -1:
        return "fold"
    if action == -2:
        return "allin"
    if action == 0:
        return "call"
    return "raise_small" if action <= 250 else "raise_big"


def _onehot(value: str, labels: tuple[str, ...]) -> list[float]:
    return [1.0 if value == label else 0.0 for label in labels]


def _score_5(cards: list[int]) -> tuple[int, ...]:
    ranks = sorted((_rank(card) for card in cards), reverse=True)
    suits = [_suit(card) for card in cards]
    counts: dict[int, int] = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
    unique = sorted(set(ranks), reverse=True)
    is_flush = len(set(suits)) == 1
    is_straight = False
    straight_high = 0
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            is_straight = True
            straight_high = unique[0]
        elif set(unique) == {14, 2, 3, 4, 5}:
            is_straight = True
            straight_high = 5
    if is_flush and is_straight:
        return (8, straight_high)
    if groups[0][0] == 4:
        return (7, groups[0][1])
    if groups[0][0] == 3 and len(groups) > 1 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, straight_high)
    if groups[0][0] == 3:
        return (3, groups[0][1])
    if groups[0][0] == 2 and len(groups) > 1 and groups[1][0] == 2:
        return (2, groups[0][1], groups[1][1])
    if groups[0][0] == 2:
        return (1, groups[0][1])
    return (0, *ranks)


def _straight_density(cards: list[int]) -> tuple[float, float]:
    ranks = {_rank(card) for card in cards}
    if 14 in ranks:
        ranks.add(1)
    best = 0
    for start in range(1, 11):
        best = max(best, len(ranks & set(range(start, start + 5))))
    return _clip(best / 5.0), 1.0 if best >= 4 else 0.0


def _request_features(request: dict[str, Any]) -> list[float]:
    my_cards = [int(card) for card in (request.get("my_cards") or [])]
    public_cards = [int(card) for card in (request.get("public_cards") or [])]
    ranks = sorted((_rank(card) for card in my_cards), reverse=True) + [2, 2]
    public_ranks = sorted((_rank(card) for card in public_cards), reverse=True)
    public_rank_padded = (public_ranks + [2, 2, 2, 2, 2])[:5]
    public_suits = [_suit(card) for card in public_cards]
    suit_counts = Counter(public_suits)
    rank_counts = Counter(public_ranks)
    all_cards = my_cards + public_cards
    made = _score_5(all_cards) if len(all_cards) >= 5 else (0, 0)
    straight_density, straight_draw = _straight_density(all_cards)
    history = list(request.get("last_history") or [])
    action_counts = Counter(row.get("action_type") for row in history)
    pot = float(request.get("pot") or 150.0)
    to_call = float(request.get("to_call") or 0.0)
    my_chips = float(request.get("my_chips") or 20000.0)
    return [
        _clip((ranks[0] - 2) / 12.0),
        _clip((ranks[1] - 2) / 12.0),
        1.0 if len(my_cards) >= 2 and _suit(my_cards[0]) == _suit(my_cards[1]) else 0.0,
        1.0 if len(my_cards) >= 2 and ranks[0] == ranks[1] else 0.0,
        _clip(abs(ranks[0] - ranks[1]) / 12.0),
        *[_clip((rank - 2) / 12.0) for rank in public_rank_padded],
        _clip(max(suit_counts.values(), default=0) / 5.0),
        _clip(max(rank_counts.values(), default=0) / 3.0),
        1.0 if max(rank_counts.values(), default=0) >= 2 else 0.0,
        1.0 if max(rank_counts.values(), default=0) >= 3 else 0.0,
        _clip(sum(1 for rank in public_ranks if rank >= 11) / 5.0),
        _clip(float(made[0]) / 8.0),
        _clip(float(made[1] if len(made) > 1 else 0) / 14.0),
        straight_density,
        straight_draw,
        _clip(pot / 20000.0),
        _clip(to_call / 20000.0),
        _clip(my_chips / 20000.0),
        _clip(float(request.get("history_len") or 0) / 80.0),
        _clip(action_counts.get("raise", 0) / 3.0),
        _clip(action_counts.get("call", 0) / 3.0),
        _clip(action_counts.get("check", 0) / 3.0),
        1.0 if request.get("my_id") == request.get("dealer_id") else 0.0,
    ]


def _feature_names() -> list[str]:
    return [
        "hole_hi",
        "hole_lo",
        "hole_suited",
        "hole_pair",
        "hole_gap",
        "board_rank_0",
        "board_rank_1",
        "board_rank_2",
        "board_rank_3",
        "board_rank_4",
        "board_max_suit_count",
        "board_max_rank_count",
        "board_paired",
        "board_trips",
        "board_broadway_density",
        "made_class",
        "made_primary_rank",
        "straight_density",
        "straight_draw",
        "pot_ratio",
        "to_call_ratio",
        "my_chips_ratio",
        "history_len",
        "recent_raise_count",
        "recent_call_count",
        "recent_check_count",
        "is_dealer",
        *[f"stage_{stage}" for stage in STAGES],
        "side_normal",
        "side_mirror",
        "decision_index",
        "baseline_action_ratio",
        "candidate_action_ratio",
        "action_delta_ratio",
        *[f"baseline_{label}" for label in ACTION_BUCKETS],
        *[f"candidate_{label}" for label in ACTION_BUCKETS],
        "candidate_checks_instead_of_raise",
    ]


def _features(divergence: dict[str, Any], side: str) -> list[float]:
    request = divergence.get("candidate_request") or divergence.get("baseline_request") or {}
    baseline_action = int(divergence.get("baseline_action", 0))
    candidate_action = int(divergence.get("candidate_action", 0))
    stage = str(request.get("stage") or "preflop")
    base_bucket = _action_bucket(baseline_action)
    cand_bucket = _action_bucket(candidate_action)
    return [
        *_request_features(request),
        *_onehot(stage, STAGES),
        1.0 if side == "normal" else 0.0,
        1.0 if side == "mirror" else 0.0,
        _clip(float(divergence.get("decision_index") or 0) / 220.0),
        _clip(max(0, baseline_action) / 20000.0),
        _clip(max(0, candidate_action) / 20000.0),
        _clip((candidate_action - baseline_action + 20000.0) / 40000.0),
        *_onehot(base_bucket, ACTION_BUCKETS),
        *_onehot(cand_bucket, ACTION_BUCKETS),
        1.0 if baseline_action > 0 and candidate_action == 0 else 0.0,
    ]


def _iter_rows(payload: dict[str, Any], source: str, all_divergences: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pair in payload.get("pairs", []):
        delta = float(pair.get("delta_net_chips", 0.0))
        for side in ("normal", "mirror"):
            compare = pair.get(side, {}).get("action_compare", {})
            divergences = list(compare.get("divergences") or [])
            if not all_divergences:
                first = compare.get("first_divergence")
                divergences = [first] if first else []
            for divergence in divergences:
                if not divergence:
                    continue
                feature_row = _features(divergence, side)
                rows.append({
                    "source": source,
                    "opponent": pair.get("opponent_label"),
                    "pair_idx": pair.get("idx"),
                    "seed": pair.get("seed"),
                    "side": side,
                    "delta": delta,
                    "target": 1.0 if delta > 0 else 0.0,
                    "weight": max(0.05, min(5.0, abs(delta) / 1000.0)),
                    "baseline_action": divergence.get("baseline_action"),
                    "candidate_action": divergence.get("candidate_action"),
                    "same_request": divergence.get("same_request"),
                    "request": divergence.get("candidate_request") or divergence.get("baseline_request"),
                    "features": feature_row,
                })
    return rows


def _summary(rows: list[dict[str, Any]], inputs: list[str]) -> dict[str, Any]:
    deltas = [float(row["delta"]) for row in rows]
    templates = Counter(f"{row['baseline_action']}->{row['candidate_action']}|{row['side']}" for row in rows)
    stages = Counter(str((row.get("request") or {}).get("stage")) for row in rows)
    dim = len(rows[0]["features"]) if rows else len(_feature_names())
    return {
        "inputs": inputs,
        "rows": len(rows),
        "input_dim": dim,
        "feature_names": _feature_names(),
        "positive": sum(1 for value in deltas if value > 0),
        "zero": sum(1 for value in deltas if value == 0),
        "negative": sum(1 for value in deltas if value < 0),
        "mean_delta": statistics.mean(deltas) if deltas else 0.0,
        "median_delta": statistics.median(deltas) if deltas else 0.0,
        "action_templates": dict(sorted(templates.items())),
        "stages": dict(sorted(stages.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--all-divergences", action="store_true")
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    input_labels: list[str] = []
    for path_arg in args.input:
        path = _resolve(path_arg)
        input_labels.append(str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.extend(_iter_rows(payload, input_labels[-1], args.all_divergences))
    if rows:
        dim = len(rows[0]["features"])
        bad = [len(row["features"]) for row in rows if len(row["features"]) != dim]
        if bad:
            raise SystemExit(f"inconsistent feature dimensions: expected {dim}, got {bad[:3]}")

    out = _resolve(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    summary = _summary(rows, input_labels)
    if args.summary_output:
        summary_out = _resolve(args.summary_output)
        summary_out.parent.mkdir(parents=True, exist_ok=True)
        summary_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
