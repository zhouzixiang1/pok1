#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
import copy
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = Path(__file__).resolve().parent
ENGINE = ROOT / "engine"
for path in (ROOT, ENGINE, TOOL_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from paired_evaluate import _label, _main_path, _mirror_initdata, _rel, _resolve, _seeded_initdata  # noqa: E402
from template_action_prefilter import _scan_side_template  # noqa: E402


SLOT_TO_INDEX = {
    "hole0": -1,
    "hole1": -2,
    "flop0": -5,
    "flop1": -6,
    "flop2": -7,
}


def _abs_index(deck: list[int], rel_index: int) -> int:
    return len(deck) + rel_index if rel_index < 0 else rel_index


def _card_desc(card: int) -> dict[str, Any]:
    return {
        "card": int(card),
        "rank": int(card) // 4 + 2,
        "suit": int(card) % 4,
    }


def _card_from(rank: int, suit: int) -> int | None:
    if rank < 2 or rank > 14 or suit < 0 or suit > 3:
        return None
    return (rank - 2) * 4 + suit


def _neighbor_cards(card: int, rank_window: int, include_same_rank_suits: bool) -> list[int]:
    rank = int(card) // 4 + 2
    suit = int(card) % 4
    cards: set[int] = set()
    if include_same_rank_suits:
        for next_suit in range(4):
            next_card = _card_from(rank, next_suit)
            if next_card is not None:
                cards.add(next_card)
    for delta in range(-max(0, int(rank_window)), max(0, int(rank_window)) + 1):
        if delta == 0:
            continue
        next_rank = rank + delta
        for next_suit in range(4):
            next_card = _card_from(next_rank, next_suit)
            if next_card is not None:
                cards.add(next_card)
    cards.discard(int(card))
    return sorted(cards)


def _swap_target_card(deck: list[int], slot: str, card: int) -> list[int] | None:
    if slot not in SLOT_TO_INDEX:
        raise ValueError(f"unknown slot: {slot}")
    next_deck = list(deck)
    slot_index = _abs_index(next_deck, SLOT_TO_INDEX[slot])
    if next_deck[slot_index] == int(card):
        return None
    try:
        swap_index = next_deck.index(int(card))
    except ValueError:
        return None
    next_deck[slot_index], next_deck[swap_index] = next_deck[swap_index], next_deck[slot_index]
    return next_deck


def _request_target_cards(deck: list[int]) -> dict[str, int]:
    return {slot: int(deck[_abs_index(deck, rel_index)]) for slot, rel_index in SLOT_TO_INDEX.items()}


def _validate_hit_deck(deck: list[int], request: dict[str, Any]) -> list[str]:
    expected = _request_target_cards(deck)
    actual = {
        "hole0": int(request["my_cards"][0]),
        "hole1": int(request["my_cards"][1]),
        "flop0": int(request["public_cards"][0]),
        "flop1": int(request["public_cards"][1]),
        "flop2": int(request["public_cards"][2]),
    }
    return [
        f"{slot}:deck={expected[slot]} request={actual[slot]}"
        for slot in SLOT_TO_INDEX
        if expected[slot] != actual[slot]
    ]


def _side_initdata(seed: int, side: str, max_hands: int) -> dict[str, Any]:
    initdata = _seeded_initdata(int(seed), int(max_hands))
    if side == "mirror":
        return _mirror_initdata(initdata)
    return initdata


def _source_hits(
    payload: dict[str, Any],
    max_source_hits: int,
    opponent_labels: set[str] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pair in payload.get("pairs", []):
        opponent_label = str(pair.get("opponent_label"))
        if opponent_labels is not None and opponent_label not in opponent_labels:
            continue
        for side in ("normal", "mirror"):
            side_row = pair.get(side, {})
            for hit_index, hit in enumerate(side_row.get("hits") or []):
                rows.append({
                    "opponent_index": int(pair.get("opponent_index", 0)),
                    "opponent_label": opponent_label,
                    "idx": int(pair["idx"]),
                    "seed": int(pair["seed"]),
                    "side": side,
                    "side_status": side_row.get("status"),
                    "hit_index": hit_index,
                    "hit": hit,
                    "bot_seeds": pair.get("bot_seeds", {}).get(side),
                })
                if max_source_hits > 0 and len(rows) >= max_source_hits:
                    return rows
    return rows


def _variant_key(source: dict[str, Any], mutation: dict[str, Any]) -> str:
    return (
        f"{source['opponent_label']}:{source['idx']}:{source['side']}:"
        f"h{source['hit']['request']['hand']}:hit{source['hit_index']}:"
        f"{mutation['slot']}:{mutation['from']['card']}->{mutation['to']['card']}"
    )


def _make_variants(source: dict[str, Any], args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[str]]:
    hit = source["hit"]
    request = hit["request"]
    hand = int(request["hand"])
    initdata = _side_initdata(source["seed"], source["side"], args.max_hands)
    deck = list(initdata["decks"][hand])
    mismatches = _validate_hit_deck(deck, request)
    if mismatches:
        return [], mismatches

    variants: list[dict[str, Any]] = []
    seen_decks: set[tuple[int, ...]] = set()
    for slot in args.slot:
        original = deck[_abs_index(deck, SLOT_TO_INDEX[slot])]
        for next_card in _neighbor_cards(original, args.rank_window, args.include_same_rank_suits):
            next_deck = _swap_target_card(deck, slot, next_card)
            if next_deck is None:
                continue
            deck_key = tuple(next_deck)
            if deck_key in seen_decks:
                continue
            seen_decks.add(deck_key)
            mutation = {
                "slot": slot,
                "from": _card_desc(original),
                "to": _card_desc(next_card),
            }
            variant = {
                "variant_id": _variant_key(source, mutation),
                "source": {
                    "opponent_label": source["opponent_label"],
                    "idx": source["idx"],
                    "seed": source["seed"],
                    "side": source["side"],
                    "side_status": source["side_status"],
                    "hit_index": source["hit_index"],
                    "hand": hand,
                    "decision_index": hit.get("decision_index"),
                    "request": request,
                    "bot_seeds": source.get("bot_seeds"),
                },
                "mutation": mutation,
                "target_deck": next_deck,
            }
            variants.append(variant)
            if args.max_variants_per_hit > 0 and len(variants) >= args.max_variants_per_hit:
                return variants, []
    return variants, []


def _scan_variant(
    task: dict[str, Any],
    baseline: Path,
    candidate: Path,
    opponent: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    source = task["source"]
    initdata = _side_initdata(source["seed"], source["side"], args.max_hands)
    initdata["decks"][int(source["hand"])] = list(task["target_deck"])
    bot_seeds = tuple(source["bot_seeds"]) if source.get("bot_seeds") is not None else None
    result = _scan_side_template(baseline, candidate, opponent, initdata, bot_seeds, args)
    row = {
        "variant_id": task["variant_id"],
        "source": source,
        "mutation": task["mutation"],
        "target_deck": task["target_deck"],
        "has_hit": bool(result.get("has_hit")),
        "scan": result,
    }
    return row


def _existing_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in payload.get("variants", []):
        variant_id = row.get("variant_id")
        if isinstance(variant_id, str):
            rows[variant_id] = row
    return rows


def _hit_count(payload: dict[str, Any]) -> int:
    return sum(1 for row in payload.get("variants", []) if row.get("has_hit"))


def _stop_reached(payload: dict[str, Any], target: int) -> bool:
    return int(target) > 0 and _hit_count(payload) >= int(target)


def _summarize(payload: dict[str, Any]) -> None:
    rows = list(payload.get("variants", []))
    by_source: dict[str, list[dict[str, Any]]] = {}
    by_slot: dict[str, list[dict[str, Any]]] = {}
    side_statuses: dict[str, int] = {}
    for row in rows:
        source = row.get("source", {})
        key = (
            f"{source.get('opponent_label')}:{source.get('idx')}:"
            f"{source.get('side')}:h{source.get('hand')}:hit{source.get('hit_index')}"
        )
        by_source.setdefault(key, []).append(row)
        slot = str(row.get("mutation", {}).get("slot"))
        by_slot.setdefault(slot, []).append(row)
        status = str(row.get("scan", {}).get("status", "unknown"))
        side_statuses[status] = side_statuses.get(status, 0) + 1

    payload["summary"] = {
        "variants": len(rows),
        "hit_variants": _hit_count(payload),
        "hit_rate": _hit_count(payload) / max(1, len(rows)),
        "side_statuses": side_statuses,
        "by_source": {
            key: {
                "variants": len(items),
                "hit_variants": sum(1 for item in items if item.get("has_hit")),
            }
            for key, items in sorted(by_source.items())
        },
        "by_slot": {
            key: {
                "variants": len(items),
                "hit_variants": sum(1 for item in items if item.get("has_hit")),
            }
            for key, items in sorted(by_slot.items())
        },
    }
    payload["tasks_completed"] = len(rows)
    if "tasks_total" in payload:
        payload["tasks_remaining"] = max(0, int(payload.get("tasks_total") or 0) - len(rows))


def _write(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    out = path if path.is_absolute() else ROOT / path
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(out)


def _resolve_default_path(path: str | None, payload: dict[str, Any], key: str) -> str | None:
    if path:
        return path
    value = payload.get(key, {})
    if isinstance(value, dict):
        return value.get("path")
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--candidate")
    parser.add_argument("--opponent")
    parser.add_argument("--source-opponent-label", action="append")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--executor", choices=["process", "thread"], default="process")
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--max-source-hits", type=int, default=0)
    parser.add_argument("--max-variants-per-hit", type=int, default=64)
    parser.add_argument("--slot", action="append", choices=sorted(SLOT_TO_INDEX), default=None)
    parser.add_argument("--rank-window", type=int, default=1)
    parser.add_argument("--include-same-rank-suits", action="store_true", default=True)
    parser.add_argument("--no-same-rank-suits", dest="include_same_rank_suits", action="store_false")
    parser.add_argument("--max-own-decisions-per-side", type=int, default=80)
    parser.add_argument("--baseline-action", type=int, default=101)
    parser.add_argument("--candidate-action", type=int, default=0)
    parser.add_argument("--stage", default="flop")
    parser.add_argument("--max-hits-per-side", type=int, default=1)
    parser.add_argument("--stop-after-hits", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.slot is None:
        args.slot = ["hole0", "hole1", "flop0", "flop1", "flop2"]

    source_path = args.source if args.source.is_absolute() else ROOT / args.source
    source_payload = json.loads(source_path.read_text(encoding="utf-8"))
    baseline_arg = _resolve_default_path(args.baseline, source_payload, "baseline")
    candidate_arg = _resolve_default_path(args.candidate, source_payload, "candidate")
    opponent_arg = args.opponent
    if opponent_arg is None:
        opponents = source_payload.get("opponents", [])
        if len(opponents) != 1:
            raise SystemExit("--opponent is required when source has zero or multiple opponents")
        opponent_arg = opponents[0].get("path")
    if not baseline_arg or not candidate_arg or not opponent_arg:
        raise SystemExit("baseline, candidate, and opponent paths are required")

    baseline = _main_path(_resolve(str(baseline_arg)))
    candidate = _main_path(_resolve(str(candidate_arg)))
    opponent = _main_path(_resolve(str(opponent_arg)))
    source_labels = {str(label) for label in args.source_opponent_label} if args.source_opponent_label else None
    sources = _source_hits(source_payload, args.max_source_hits, source_labels)
    generated: list[dict[str, Any]] = []
    skipped_sources: list[dict[str, Any]] = []
    for source in sources:
        variants, errors = _make_variants(source, args)
        if errors:
            skipped_sources.append({
                "source": {
                    "opponent_label": source["opponent_label"],
                    "idx": source["idx"],
                    "side": source["side"],
                    "hit_index": source["hit_index"],
                },
                "errors": errors,
            })
            continue
        generated.extend(variants)

    payload: dict[str, Any] = {
        "mode": "template_neighborhood_prefilter_v1",
        "source": str(args.source),
        "baseline": {"label": _label(baseline), "path": _rel(baseline)},
        "candidate": {"label": _label(candidate), "path": _rel(candidate)},
        "opponent": {"label": _label(opponent), "path": _rel(opponent)},
        "workers": args.workers,
        "executor": args.executor,
        "max_hands": args.max_hands,
        "max_source_hits": args.max_source_hits,
        "source_opponent_labels": sorted(source_labels) if source_labels else None,
        "max_variants_per_hit": args.max_variants_per_hit,
        "slot": args.slot,
        "rank_window": args.rank_window,
        "include_same_rank_suits": args.include_same_rank_suits,
        "max_own_decisions_per_side": args.max_own_decisions_per_side,
        "baseline_action": args.baseline_action,
        "candidate_action": args.candidate_action,
        "stage": args.stage,
        "max_hits_per_side": args.max_hits_per_side,
        "stop_after_hits": args.stop_after_hits,
        "source_hits": len(sources),
        "skipped_sources": skipped_sources,
        "tasks_total": 0,
        "tasks_existing": 0,
        "tasks_submitted": 0,
        "tasks_skipped": 0,
        "tasks_completed": 0,
        "tasks_remaining": 0,
        "early_stopped": False,
        "variants": [],
        "summary": {},
    }

    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    existing: dict[str, dict[str, Any]] = {}
    if args.resume and output_path.exists():
        old = json.loads(output_path.read_text(encoding="utf-8"))
        existing = _existing_rows(old)
        payload["variants"] = list(existing.values())
        payload["tasks_existing"] = len(existing)

    tasks = [variant for variant in generated if variant["variant_id"] not in existing]
    payload["tasks_total"] = len(tasks) + len(existing)
    _summarize(payload)
    _write(args.output, payload)

    def _consume(row: dict[str, Any]) -> None:
        payload["variants"].append(row)
        payload["variants"].sort(key=lambda item: item["variant_id"])
        _summarize(payload)
        _write(args.output, payload)
        source = row["source"]
        mutation = row["mutation"]
        print(
            f"{source['opponent_label']} idx={source['idx']} {source['side']} "
            f"h{source['hand']} {mutation['slot']}->{mutation['to']['card']} "
            f"hit={row['has_hit']} status={row['scan']['status']}",
            flush=True,
        )

    submitted = 0
    if args.workers <= 1:
        for task in tasks:
            if _stop_reached(payload, args.stop_after_hits):
                payload["early_stopped"] = True
                break
            submitted += 1
            payload["tasks_submitted"] = submitted
            _consume(_scan_variant(task, baseline, candidate, opponent, args))
    else:
        executor_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
        with executor_cls(max_workers=max(1, args.workers)) as executor:
            remaining = list(tasks)
            futures = {}

            def _submit_next() -> None:
                nonlocal submitted
                if not remaining:
                    return
                if _stop_reached(payload, args.stop_after_hits):
                    payload["early_stopped"] = True
                    return
                task = remaining.pop(0)
                submitted += 1
                payload["tasks_submitted"] = submitted
                futures[executor.submit(_scan_variant, task, baseline, candidate, opponent, args)] = task

            while remaining and len(futures) < max(1, args.workers):
                _submit_next()
            while futures:
                done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future)
                    _consume(future.result())
                while remaining and len(futures) < max(1, args.workers):
                    before = len(futures)
                    _submit_next()
                    if len(futures) == before:
                        break
                if payload.get("early_stopped") and not futures:
                    break

    payload["tasks_submitted"] = submitted
    payload["tasks_skipped"] = max(0, len(tasks) - submitted)
    if _stop_reached(payload, args.stop_after_hits):
        payload["early_stopped"] = bool(payload["tasks_skipped"])
    _summarize(payload)
    _write(args.output, payload)
    if not tasks:
        print("all requested rows already present", flush=True)


if __name__ == "__main__":
    main()
