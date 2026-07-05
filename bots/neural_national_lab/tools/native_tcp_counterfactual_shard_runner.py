#!/usr/bin/env python3
"""Run native TCP counterfactual probe shards and merge action-value rows."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PROBE = Path(__file__).resolve().with_name("native_tcp_counterfactual_probe.py")
LABELS = ("fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _resolve(path: str) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def _stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    mean = statistics.mean(values) if values else 0.0
    median = statistics.median(values) if values else 0.0
    stddev = statistics.stdev(values) if n >= 2 else 0.0
    stderr = stddev / (n**0.5) if n >= 2 else 0.0
    margin = 1.96 * stderr if n >= 2 else 0.0
    return {
        "samples": n,
        "mean": mean,
        "median": median,
        "stddev": stddev,
        "stderr": stderr,
        "ci95_low": mean - margin if n >= 2 else None,
        "ci95_high": mean + margin if n >= 2 else None,
        "significant_positive_95": bool(n >= 2 and mean - margin > 0.0),
        "significant_negative_95": bool(n >= 2 and mean + margin < 0.0),
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    by_label: dict[str, list[float]] = {label: [] for label in LABELS}
    by_rule_label: dict[str, list[float]] = {}
    by_opponent: dict[str, list[float]] = {}
    by_stage: dict[str, list[float]] = {}
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status"))
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "ok":
            continue
        rule_idx = int(row.get("rule_label_id", -1) or -1)
        deltas = row.get("delta_vs_rule") or []
        for idx, value in enumerate(deltas):
            if value is None or idx == rule_idx:
                continue
            delta = float(value)
            values.append(delta)
            label = LABELS[idx] if 0 <= idx < len(LABELS) else str(idx)
            by_label.setdefault(label, []).append(delta)
            by_rule_label.setdefault(str(row.get("rule_label")), []).append(delta)
            by_opponent.setdefault(str(row.get("opponent")), []).append(delta)
            by_stage.setdefault(str(row.get("stage")), []).append(delta)
    return {
        "rows": len(rows),
        "ok_rows": status_counts.get("ok", 0),
        "target_samples": len(values),
        "delta": _stats(values),
        "positive": sum(1 for value in values if value > 0),
        "negative": sum(1 for value in values if value < 0),
        "zero": sum(1 for value in values if value == 0),
        "status_counts": dict(sorted(status_counts.items())),
        "by_label": {label: _stats(vals) for label, vals in sorted(by_label.items()) if vals},
        "by_rule_label": {label: _stats(vals) for label, vals in sorted(by_rule_label.items()) if vals},
        "by_opponent": {label: _stats(vals) for label, vals in sorted(by_opponent.items()) if vals},
        "by_stage": {label: _stats(vals) for label, vals in sorted(by_stage.items()) if vals},
    }


def _display_cmd(cmd: list[str]) -> str:
    out: list[str] = []
    for idx, part in enumerate(cmd):
        if idx == 0:
            out.append("python")
            continue
        path = Path(part)
        out.append(_rel(path) if path.is_absolute() else part)
    return " ".join(out)


def _seed_base(args: argparse.Namespace, opponent_idx: int, shard_idx: int) -> int:
    return int(args.seed_base) + opponent_idx * int(args.opponent_seed_stride) + shard_idx * int(args.seed_stride)


def _bot_seed_base(args: argparse.Namespace, opponent_idx: int, shard_idx: int) -> int | None:
    if args.bot_seed_base is None:
        return None
    return int(args.bot_seed_base) + opponent_idx * int(args.opponent_bot_seed_stride) + shard_idx * int(args.bot_seed_stride)


def _probe_cmd(
    args: argparse.Namespace,
    *,
    opponent: str,
    opponent_idx: int,
    shard_idx: int,
    output: Path,
    jsonl_output: Path,
) -> list[str]:
    cmd = [
        sys.executable,
        str(PROBE),
        "--candidate",
        args.candidate,
        "--opponent",
        opponent,
        "--hands",
        str(args.hands),
        "--seed-base",
        str(_seed_base(args, opponent_idx, shard_idx)),
        "--stage",
        args.stage,
        "--min-hand",
        str(args.min_hand),
        "--min-opponent-actions",
        str(args.min_opponent_actions),
        "--max-opponent-actions",
        str(args.max_opponent_actions),
        "--max-decisions",
        str(args.max_decisions_per_shard),
        "--max-alternatives",
        str(args.max_alternatives),
        "--timeout-sec",
        str(args.timeout_sec),
        "--output",
        str(output),
        "--jsonl-output",
        str(jsonl_output),
    ]
    bot_seed_base = _bot_seed_base(args, opponent_idx, shard_idx)
    if bot_seed_base is not None:
        cmd.extend(["--bot-seed-base", str(bot_seed_base)])
    if args.max_opponent_raise_rate is not None:
        cmd.extend(["--max-opponent-raise-rate", str(args.max_opponent_raise_rate)])
    if args.initial_sb_only:
        cmd.append("--initial-sb-only")
        cmd.extend(["--initial-sb-max-to-call", str(args.initial_sb_max_to_call)])
    for label in args.rule_label:
        cmd.extend(["--rule-label", label])
    for label in args.alternative_label:
        cmd.extend(["--alternative-label", label])
    return cmd


def _run_shard(
    args: argparse.Namespace,
    *,
    opponent: str,
    opponent_idx: int,
    shard_idx: int,
    output: Path,
    jsonl_output: Path,
) -> dict[str, Any]:
    cmd = _probe_cmd(
        args,
        opponent=opponent,
        opponent_idx=opponent_idx,
        shard_idx=shard_idx,
        output=output,
        jsonl_output=jsonl_output,
    )
    skipped = output.exists() and jsonl_output.exists() and not args.rerun_existing
    if skipped:
        return {
            "opponent": opponent,
            "opponent_idx": opponent_idx,
            "shard": shard_idx,
            "output": _rel(output),
            "jsonl_output": _rel(jsonl_output),
            "seed_base": _seed_base(args, opponent_idx, shard_idx),
            "bot_seed_base": _bot_seed_base(args, opponent_idx, shard_idx),
            "command": _display_cmd(cmd),
            "returncode": 0,
            "skipped_existing": True,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=args.shard_timeout_sec or None,
        )
        returncode = proc.returncode
        stdout_tail = proc.stdout[-4000:]
        stderr_tail = proc.stderr[-4000:]
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout_tail = str(exc.stdout or "")[-4000:]
        stderr_tail = str(exc.stderr or "")[-4000:]
    return {
        "opponent": opponent,
        "opponent_idx": opponent_idx,
        "shard": shard_idx,
        "output": _rel(output),
        "jsonl_output": _rel(jsonl_output),
        "seed_base": _seed_base(args, opponent_idx, shard_idx),
        "bot_seed_base": _bot_seed_base(args, opponent_idx, shard_idx),
        "command": _display_cmd(cmd),
        "returncode": returncode,
        "skipped_existing": False,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


def _merge_shard(payload: dict[str, Any], entry: dict[str, Any]) -> None:
    shard_path = ROOT / entry["output"]
    if shard_path.exists():
        shard_payload = json.loads(shard_path.read_text(encoding="utf-8"))
        rows = shard_payload.get("rows", [])
        entry["summary"] = shard_payload.get("summary", {})
        entry["row_count"] = len(rows)
        for row in rows:
            payload["rows"].append(
                {
                    **row,
                    "shard": int(entry["shard"]),
                    "shard_opponent_idx": int(entry["opponent_idx"]),
                    "shard_seed_base": entry.get("seed_base"),
                    "shard_bot_seed_base": entry.get("bot_seed_base"),
                    "source_report": entry["output"],
                }
            )
    payload["shard_results"].append(entry)
    payload["summary"] = _summarize(payload["rows"])


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_jsonl(path: Path | None, rows: list[dict[str, Any]]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run native TCP counterfactual probe shards.")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--opponent", action="append", required=True)
    parser.add_argument("--shards-per-opponent", type=int, default=4)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--hands", type=int, default=12)
    parser.add_argument("--stage", choices=["any", "preflop", "flop", "turn", "river"], default="preflop")
    parser.add_argument("--min-hand", type=int, default=1)
    parser.add_argument("--min-opponent-actions", type=int, default=0)
    parser.add_argument("--max-opponent-actions", type=int, default=0)
    parser.add_argument("--max-opponent-raise-rate", type=float, default=None)
    parser.add_argument("--initial-sb-only", action="store_true")
    parser.add_argument("--initial-sb-max-to-call", type=float, default=60.0)
    parser.add_argument("--rule-label", action="append", choices=LABELS, default=[])
    parser.add_argument("--alternative-label", action="append", choices=LABELS, default=[])
    parser.add_argument("--max-decisions-per-shard", type=int, default=4)
    parser.add_argument("--max-alternatives", type=int, default=2)
    parser.add_argument("--seed-base", type=int, default=2026071500)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--opponent-seed-stride", type=int, default=1000)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-stride", type=int, default=100)
    parser.add_argument("--opponent-bot-seed-stride", type=int, default=100000)
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--shard-timeout-sec", type=int, default=0)
    parser.add_argument("--rerun-existing", action="store_true")
    parser.add_argument("--merge-only", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jsonl-output", type=Path)
    args = parser.parse_args()

    output = args.output if args.output.is_absolute() else ROOT / args.output
    jsonl_output = args.jsonl_output
    if jsonl_output is not None and not jsonl_output.is_absolute():
        jsonl_output = ROOT / jsonl_output
    shard_dir = output.with_suffix("")
    shard_dir.mkdir(parents=True, exist_ok=True)
    opponents = list(args.opponent)
    total_shards = len(opponents) * int(args.shards_per_opponent)
    payload: dict[str, Any] = {
        "mode": "native_tcp_counterfactual_shards_v1",
        "candidate": _rel(_resolve(args.candidate)),
        "opponents": [_rel(_resolve(opponent)) for opponent in opponents],
        "labels": list(LABELS),
        "shards_per_opponent": int(args.shards_per_opponent),
        "workers": int(args.workers),
        "hands": int(args.hands),
        "seed_base": int(args.seed_base),
        "seed_stride": int(args.seed_stride),
        "opponent_seed_stride": int(args.opponent_seed_stride),
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": int(args.bot_seed_stride),
        "opponent_bot_seed_stride": int(args.opponent_bot_seed_stride),
        "filters": {
            "stage": args.stage,
            "min_hand": int(args.min_hand),
            "min_opponent_actions": int(args.min_opponent_actions),
            "max_opponent_actions": int(args.max_opponent_actions),
            "max_opponent_raise_rate": args.max_opponent_raise_rate,
            "initial_sb_only": bool(args.initial_sb_only),
            "initial_sb_max_to_call": float(args.initial_sb_max_to_call),
            "rule_label": list(args.rule_label),
            "alternative_label": list(args.alternative_label),
            "max_decisions_per_shard": int(args.max_decisions_per_shard),
            "max_alternatives": int(args.max_alternatives),
        },
        "shard_results": [],
        "rows": [],
        "summary": {},
    }
    _write(output, payload)

    work_items: list[tuple[str, int, int, Path, Path]] = []
    for opponent_idx, opponent in enumerate(opponents):
        for shard_idx in range(int(args.shards_per_opponent)):
            stem = f"op{opponent_idx:02d}_shard_{shard_idx:03d}"
            work_items.append((opponent, opponent_idx, shard_idx, shard_dir / f"{stem}.json", shard_dir / f"{stem}.jsonl"))

    entries: list[dict[str, Any] | None] = [None] * len(work_items)
    if args.merge_only:
        for idx, (opponent, opponent_idx, shard_idx, shard_output, shard_jsonl) in enumerate(work_items):
            cmd = _probe_cmd(
                args,
                opponent=opponent,
                opponent_idx=opponent_idx,
                shard_idx=shard_idx,
                output=shard_output,
                jsonl_output=shard_jsonl,
            )
            entries[idx] = {
                "opponent": opponent,
                "opponent_idx": opponent_idx,
                "shard": shard_idx,
                "output": _rel(shard_output),
                "jsonl_output": _rel(shard_jsonl),
                "seed_base": _seed_base(args, opponent_idx, shard_idx),
                "bot_seed_base": _bot_seed_base(args, opponent_idx, shard_idx),
                "command": _display_cmd(cmd),
                "returncode": 0 if shard_output.exists() else 127,
                "skipped_existing": shard_output.exists(),
                "stdout_tail": "",
                "stderr_tail": "" if shard_output.exists() else "missing shard output",
            }
    elif args.workers <= 1:
        for idx, (opponent, opponent_idx, shard_idx, shard_output, shard_jsonl) in enumerate(work_items):
            print(
                f"shard {idx + 1}/{total_shards}: "
                f"{_display_cmd(_probe_cmd(args, opponent=opponent, opponent_idx=opponent_idx, shard_idx=shard_idx, output=shard_output, jsonl_output=shard_jsonl))}"
            )
            entries[idx] = _run_shard(
                args,
                opponent=opponent,
                opponent_idx=opponent_idx,
                shard_idx=shard_idx,
                output=shard_output,
                jsonl_output=shard_jsonl,
            )
    else:
        print(f"running {total_shards} native TCP counterfactual shards with {args.workers} workers")
        with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as executor:
            future_map = {}
            for idx, (opponent, opponent_idx, shard_idx, shard_output, shard_jsonl) in enumerate(work_items):
                future = executor.submit(
                    _run_shard,
                    args,
                    opponent=opponent,
                    opponent_idx=opponent_idx,
                    shard_idx=shard_idx,
                    output=shard_output,
                    jsonl_output=shard_jsonl,
                )
                future_map[future] = idx
            for future in as_completed(future_map):
                idx = future_map[future]
                entry = future.result()
                entries[idx] = entry
                print(
                    f"  shard {idx + 1}/{total_shards} rc={entry['returncode']} "
                    f"rows={entry.get('row_count', '?')} output={entry['output']}"
                )

    for entry in entries:
        if entry is None:
            continue
        _merge_shard(payload, entry)
        _write(output, payload)
        _write_jsonl(jsonl_output, payload["rows"])
        print(
            f"merge op={entry['opponent_idx']} shard={entry['shard']} rc={entry['returncode']} "
            f"rows={entry.get('row_count', 0)} merged={len(payload['rows'])}"
        )

    returncodes: dict[str, int] = {}
    for entry in payload["shard_results"]:
        key = str(entry.get("returncode"))
        returncodes[key] = returncodes.get(key, 0) + 1
    payload["shard_returncode_counts"] = dict(sorted(returncodes.items()))
    payload["summary"] = _summarize(payload["rows"])
    _write(output, payload)
    _write_jsonl(jsonl_output, payload["rows"])
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
