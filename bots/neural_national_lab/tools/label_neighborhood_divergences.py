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
ENGINE = ROOT / "engine"
TOOL_DIR = Path(__file__).resolve().parent
for path in (ROOT, ENGINE, TOOL_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from active_divergence_scan import _scan_side  # noqa: E402
from paired_evaluate import _label, _main_path, _mirror_initdata, _rel, _resolve, _seeded_initdata, _stats  # noqa: E402


def _side_initdata(seed: int, side: str, max_hands: int) -> dict[str, Any]:
    initdata = _seeded_initdata(int(seed), int(max_hands))
    if side == "mirror":
        return _mirror_initdata(initdata)
    return initdata


def _empty_side() -> dict[str, Any]:
    return {
        "baseline": {"winner": -1, "bot0_chips": 0.0, "bot1_chips": 0.0},
        "candidate": {"winner": -1, "bot0_chips": 0.0, "bot1_chips": 0.0},
        "delta_chips": 0.0,
        "action_compare": {
            "baseline_decisions": 0,
            "candidate_decisions": 0,
            "compared_decisions": 0,
            "divergence_count_capped": 0,
            "has_divergence": False,
            "length_mismatch": False,
            "first_divergence": None,
            "divergences": [],
        },
    }


def _label_variant(
    task: dict[str, Any],
    baseline: Path,
    candidate: Path,
    opponent: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    source = task["source"]
    side = str(source["side"])
    initdata = _side_initdata(source["seed"], side, args.max_hands)
    initdata["decks"][int(source["hand"])] = list(task["target_deck"])
    bot_seeds = tuple(source["bot_seeds"]) if source.get("bot_seeds") is not None else None
    side_result = _scan_side(
        baseline,
        candidate,
        opponent,
        initdata,
        bot_seeds,
        args.max_divergences_per_side,
    )
    baseline_net = float(side_result["baseline"]["bot0_chips"])
    candidate_net = float(side_result["candidate"]["bot0_chips"])
    normal = side_result if side == "normal" else _empty_side()
    mirror = side_result if side == "mirror" else _empty_side()
    return {
        "opponent_index": 0,
        "opponent_label": _label(opponent),
        "opponent": _rel(opponent),
        "idx": int(source["idx"]),
        "variant_id": task["variant_id"],
        "seed": int(source["seed"]),
        "side": side,
        "source": source,
        "mutation": task["mutation"],
        "target_deck": task["target_deck"],
        "bot_seeds": {
            "normal": list(bot_seeds) if side == "normal" and bot_seeds is not None else None,
            "mirror": list(bot_seeds) if side == "mirror" and bot_seeds is not None else None,
        },
        "baseline_net_chips": baseline_net,
        "candidate_net_chips": candidate_net,
        "delta_net_chips": candidate_net - baseline_net,
        "has_divergence": bool(side_result["action_compare"]["has_divergence"]),
        "normal": normal,
        "mirror": mirror,
    }


def _candidate_tasks(payload: dict[str, Any], max_variants: int) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for row in payload.get("variants", []):
        if not row.get("has_hit"):
            continue
        tasks.append(copy.deepcopy(row))
        if max_variants > 0 and len(tasks) >= max_variants:
            break
    return tasks


def _existing_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in payload.get("pairs", []):
        variant_id = row.get("variant_id")
        if isinstance(variant_id, str):
            rows[variant_id] = row
    return rows


def _summarize(payload: dict[str, Any]) -> None:
    rows = list(payload.get("pairs", []))
    values = [float(row.get("delta_net_chips", 0.0)) for row in rows]
    changed = [row for row in rows if row.get("has_divergence")]
    by_source: dict[str, list[float]] = {}
    by_slot: dict[str, list[float]] = {}
    for row in rows:
        source = row.get("source", {})
        source_key = (
            f"{source.get('opponent_label')}:{source.get('idx')}:"
            f"{source.get('side')}:h{source.get('hand')}:hit{source.get('hit_index')}"
        )
        by_source.setdefault(source_key, []).append(float(row.get("delta_net_chips", 0.0)))
        slot = str(row.get("mutation", {}).get("slot"))
        by_slot.setdefault(slot, []).append(float(row.get("delta_net_chips", 0.0)))
    payload["summary"] = {
        "pairs": len(rows),
        "divergence_pairs": len(changed),
        "divergence_rate": len(changed) / max(1, len(rows)),
        "positive": sum(1 for value in values if value > 0.0),
        "zero": sum(1 for value in values if value == 0.0),
        "negative": sum(1 for value in values if value < 0.0),
        "delta_stats": _stats(values, 70),
        "by_source": {
            key: {
                "pairs": len(items),
                "positive": sum(1 for value in items if value > 0.0),
                "zero": sum(1 for value in items if value == 0.0),
                "negative": sum(1 for value in items if value < 0.0),
                "delta_stats": _stats(items, 70),
            }
            for key, items in sorted(by_source.items())
        },
        "by_slot": {
            key: {
                "pairs": len(items),
                "positive": sum(1 for value in items if value > 0.0),
                "zero": sum(1 for value in items if value == 0.0),
                "negative": sum(1 for value in items if value < 0.0),
                "delta_stats": _stats(items, 70),
            }
            for key, items in sorted(by_slot.items())
        },
        "largest_abs_deltas": sorted(
            (
                {
                    "variant_id": row.get("variant_id"),
                    "idx": row.get("idx"),
                    "delta_net_chips": row.get("delta_net_chips"),
                    "has_divergence": row.get("has_divergence"),
                    "mutation": row.get("mutation"),
                }
                for row in rows
            ),
            key=lambda item: abs(float(item["delta_net_chips"] or 0.0)),
            reverse=True,
        )[:10],
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
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--baseline")
    parser.add_argument("--candidate")
    parser.add_argument("--opponent")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--executor", choices=["process", "thread"], default="process")
    parser.add_argument("--max-variants", type=int, default=0)
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--max-divergences-per-side", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    source_path = args.source if args.source.is_absolute() else ROOT / args.source
    source_payload = json.loads(source_path.read_text(encoding="utf-8"))
    baseline_arg = _resolve_default_path(args.baseline, source_payload, "baseline")
    candidate_arg = _resolve_default_path(args.candidate, source_payload, "candidate")
    opponent_arg = _resolve_default_path(args.opponent, source_payload, "opponent")
    if not baseline_arg or not candidate_arg or not opponent_arg:
        raise SystemExit("baseline, candidate, and opponent paths are required")

    baseline = _main_path(_resolve(str(baseline_arg)))
    candidate = _main_path(_resolve(str(candidate_arg)))
    opponent = _main_path(_resolve(str(opponent_arg)))
    all_tasks = _candidate_tasks(source_payload, args.max_variants)

    payload: dict[str, Any] = {
        "mode": "label_neighborhood_divergences_v1",
        "source": str(args.source),
        "baseline": {"label": _label(baseline), "path": _rel(baseline)},
        "candidate": {"label": _label(candidate), "path": _rel(candidate)},
        "opponent": {"label": _label(opponent), "path": _rel(opponent)},
        "workers": args.workers,
        "executor": args.executor,
        "max_variants": args.max_variants,
        "max_hands": args.max_hands,
        "max_divergences_per_side": args.max_divergences_per_side,
        "tasks_total": 0,
        "tasks_existing": 0,
        "tasks_submitted": 0,
        "tasks_skipped": 0,
        "tasks_completed": 0,
        "tasks_remaining": 0,
        "pairs": [],
        "summary": {},
    }

    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    existing: dict[str, dict[str, Any]] = {}
    if args.resume and output_path.exists():
        old = json.loads(output_path.read_text(encoding="utf-8"))
        existing = _existing_rows(old)
        payload["pairs"] = list(existing.values())
        payload["tasks_existing"] = len(existing)

    tasks = [task for task in all_tasks if task["variant_id"] not in existing]
    payload["tasks_total"] = len(tasks) + len(existing)
    _summarize(payload)
    _write(args.output, payload)

    def _consume(row: dict[str, Any]) -> None:
        payload["pairs"].append(row)
        payload["pairs"].sort(key=lambda item: item["variant_id"])
        _summarize(payload)
        _write(args.output, payload)
        mutation = row["mutation"]
        print(
            f"{row['variant_id']} delta={row['delta_net_chips']} "
            f"divergence={row['has_divergence']} "
            f"{mutation['slot']}:{mutation['from']['card']}->{mutation['to']['card']}",
            flush=True,
        )

    submitted = 0
    if args.workers <= 1:
        for task in tasks:
            submitted += 1
            payload["tasks_submitted"] = submitted
            _consume(_label_variant(task, baseline, candidate, opponent, args))
    else:
        executor_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
        with executor_cls(max_workers=max(1, args.workers)) as executor:
            remaining = list(tasks)
            futures = {}

            def _submit_next() -> None:
                nonlocal submitted
                if not remaining:
                    return
                task = remaining.pop(0)
                submitted += 1
                payload["tasks_submitted"] = submitted
                futures[executor.submit(_label_variant, task, baseline, candidate, opponent, args)] = task

            while remaining and len(futures) < max(1, args.workers):
                _submit_next()
            while futures:
                done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future)
                    _consume(future.result())
                while remaining and len(futures) < max(1, args.workers):
                    _submit_next()

    payload["tasks_submitted"] = submitted
    payload["tasks_skipped"] = max(0, len(tasks) - submitted)
    _summarize(payload)
    _write(args.output, payload)
    if not tasks:
        print("all requested rows already present", flush=True)


if __name__ == "__main__":
    main()
