#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]


def _resolve(path: str) -> Path:
    p = Path(path)
    return (ROOT / p).resolve() if not p.is_absolute() else p


def _rank(card: int) -> int:
    return int(card) // 4 + 2


def _suit(card: int) -> int:
    return int(card) % 4


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if value < lo else hi if value > hi else value


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
        quad = groups[0][1]
        kicker = max(rank for rank in ranks if rank != quad)
        return (7, quad, kicker)
    if groups[0][0] == 3 and len(groups) > 1 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, straight_high)
    if groups[0][0] == 3:
        trips = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != trips), reverse=True)
        return (3, trips, *kickers)
    if groups[0][0] == 2 and len(groups) > 1 and groups[1][0] == 2:
        high_pair = max(groups[0][1], groups[1][1])
        low_pair = min(groups[0][1], groups[1][1])
        kicker = max(rank for rank in ranks if rank not in (high_pair, low_pair))
        return (2, high_pair, low_pair, kicker)
    if groups[0][0] == 2:
        pair = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != pair), reverse=True)
        return (1, pair, *kickers)
    return (0, *ranks)


def _best_score_and_hole_use(my_cards: list[int], public_cards: list[int]) -> tuple[tuple[int, ...], int]:
    cards = list(my_cards) + list(public_cards)
    if len(cards) < 5:
        return (0, 0), 0
    best_score: tuple[int, ...] | None = None
    best_hole_use = 0
    hole_set = set(my_cards)
    for combo in itertools.combinations(cards, 5):
        score = _score_5(list(combo))
        hole_use = sum(1 for card in combo if card in hole_set)
        if best_score is None or score > best_score or (score == best_score and hole_use > best_hole_use):
            best_score = score
            best_hole_use = hole_use
    return best_score or (0, 0), best_hole_use


def _straight_features(my_cards: list[int], public_cards: list[int]) -> tuple[float, float, float]:
    all_ranks = {_rank(card) for card in my_cards + public_cards}
    hole_ranks = {_rank(card) for card in my_cards}
    if 14 in all_ranks:
        all_ranks.add(1)
    if 14 in hole_ranks:
        hole_ranks.add(1)
    best_count = 0
    best_hole = 0
    for start in range(1, 11):
        window = set(range(start, start + 5))
        count = len(all_ranks & window)
        hole_count = len(hole_ranks & window)
        if count > best_count or (count == best_count and hole_count > best_hole):
            best_count = count
            best_hole = hole_count
    return _clip(best_count / 5.0), 1.0 if best_count >= 4 else 0.0, _clip(best_hole / 2.0)


def hand_strength_features(my_cards: list[int], public_cards: list[int]) -> list[float]:
    if len(my_cards) < 2:
        my_cards = (list(my_cards) + [0, 4])[:2]
    ranks = [_rank(card) for card in my_cards]
    board_ranks = [_rank(card) for card in public_cards]
    board_suits = [_suit(card) for card in public_cards]
    made, hole_use = _best_score_and_hole_use(my_cards, public_cards)
    all_suits = [_suit(card) for card in my_cards + public_cards]
    suit_counts = {suit: all_suits.count(suit) for suit in range(4)}
    best_suit = max(suit_counts, key=lambda suit: suit_counts[suit])
    best_suit_count = suit_counts[best_suit]
    hole_best_suit = sum(1 for card in my_cards if _suit(card) == best_suit)
    rank_matches = sum(1 for rank in ranks if rank in set(board_ranks))
    board_rank_max = max((board_ranks.count(rank) for rank in set(board_ranks)), default=0)
    board_suit_max = max((board_suits.count(suit) for suit in set(board_suits)), default=0)
    max_board_rank = max(board_ranks, default=2)
    overcards = sum(1 for rank in ranks if rank > max_board_rank)
    straight_density, straight_draw, straight_hole = _straight_features(my_cards, public_cards)
    return [
        _clip(len(public_cards) / 5.0),
        _clip(float(made[0]) / 8.0),
        _clip(float(made[1] if len(made) > 1 else 0) / 14.0),
        _clip(hole_use / 2.0),
        _clip(rank_matches / 2.0),
        _clip(overcards / 2.0),
        1.0 if made[0] >= 1 else 0.0,
        1.0 if made[0] >= 2 else 0.0,
        1.0 if made[0] >= 3 else 0.0,
        _clip(best_suit_count / 5.0),
        _clip(hole_best_suit / 2.0),
        1.0 if best_suit_count == 4 and hole_best_suit > 0 else 0.0,
        1.0 if best_suit_count == 3 and hole_best_suit > 0 else 0.0,
        straight_density,
        straight_draw,
        straight_hole,
        _clip(board_rank_max / 3.0),
        _clip(board_suit_max / 3.0),
    ]


def _features_match(left: list[Any], right: list[Any]) -> bool:
    if len(left) != len(right):
        return False
    return all(abs(float(a) - float(b)) <= 1e-9 for a, b in zip(left, right))


def _load_json(path: Path, cache: dict[Path, dict[str, Any]]) -> dict[str, Any]:
    if path not in cache:
        cache[path] = json.loads(path.read_text(encoding="utf-8"))
    return cache[path]


def _find_probe_context(row: dict[str, Any], cache: dict[Path, dict[str, Any]]) -> tuple[list[int], list[int]] | None:
    source = row.get("source")
    if not isinstance(source, str):
        return None
    path = _resolve(source)
    if not path.exists() or path.suffix != ".json":
        return None
    payload = _load_json(path, cache)
    features = row.get("features") or []
    if "probes" in payload:
        for probe in payload.get("probes", []):
            if _features_match(features, probe.get("advantage_features") or []):
                return list(probe.get("my_cards") or []), list(probe.get("public_cards") or [])
    if "pairs" in payload and row.get("trace_side") is not None:
        side = str(row.get("trace_side"))
        decision_index = int(row.get("trace_decision_index", -1))
        for pair in payload.get("pairs", []):
            section = pair.get(side) or {}
            for change in section.get("changes", []):
                if int(change.get("decision_index", -2)) == decision_index:
                    return list(change.get("my_cards") or []), list(change.get("public_cards") or [])
    return None


def _iter_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _extra_trace_row(spec: str, cache: dict[Path, dict[str, Any]]) -> dict[str, Any]:
    parts = spec.split(",")
    if len(parts) != 5:
        raise SystemExit("--extra-trace-change must be path,side,decision_index,target,weight")
    path = _resolve(parts[0])
    side = parts[1]
    decision_index = int(parts[2])
    target = 1 if int(parts[3]) else 0
    weight = float(parts[4])
    payload = _load_json(path, cache)
    for pair in payload.get("pairs", []):
        section = pair.get(side) or {}
        for change in section.get("changes", []):
            if int(change.get("decision_index", -1)) != decision_index:
                continue
            features = [float(value) for value in change["advantage_features"]]
            extra = hand_strength_features(
                list(change.get("my_cards") or []),
                list(change.get("public_cards") or []),
            )
            return {
                "features": features + extra,
                "target": target,
                "weight": weight,
                "delta": float(change.get("hand_delta", 0.0) or 0.0),
                "source": str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path),
                "trace_side": side,
                "trace_hand": change.get("hand"),
                "trace_decision_index": decision_index,
                "stage": change.get("stage"),
                "kind": change.get("kind"),
                "top_label": change.get("top_label"),
                "top_conf": change.get("top_conf"),
                "advised_final": change.get("advised_final"),
                "base_final": change.get("base_final"),
                "label_source": "extra_trace_change",
                "extra_features": extra,
            }
    raise SystemExit(f"extra trace change not found: {spec}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--extra-trace-change", action="append", default=[])
    args = parser.parse_args()

    cache: dict[Path, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    missing_context = 0
    for row in _iter_rows(args.input):
        context = _find_probe_context(row, cache)
        if context is None:
            missing_context += 1
            extra = [0.0] * 18
        else:
            extra = hand_strength_features(context[0], context[1])
        out = dict(row)
        out["features"] = [float(value) for value in row["features"]] + extra
        out["extra_features"] = extra
        rows.append(out)

    for spec in args.extra_trace_change:
        rows.append(_extra_trace_row(spec, cache))

    if not rows:
        raise SystemExit("no rows generated")
    dim = len(rows[0]["features"])
    bad_dims = [len(row.get("features") or []) for row in rows if len(row.get("features") or []) != dim]
    if bad_dims:
        raise SystemExit(f"inconsistent feature dimensions: expected {dim}, got {bad_dims[:5]}")
    positives = sum(1 for row in rows if int(row.get("target", 0)) == 1)
    summary = {
        "input": str(args.input),
        "rows": len(rows),
        "input_dim": dim,
        "base_dim": dim - 18,
        "extra_dim": 18,
        "positive": positives,
        "negative": len(rows) - positives,
        "positive_rate": positives / max(1, len(rows)),
        "missing_context": missing_context,
        "extra_trace_changes": len(args.extra_trace_change),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
