#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = Path(__file__).resolve().parent
PAIRED = TOOL_DIR / "paired_evaluate.py"

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from paired_evaluate import _stats  # noqa: E402


def _resolve(path: str) -> Path:
    p = Path(path)
    return (ROOT / p).resolve() if not p.is_absolute() else p


def _main_path(path: Path) -> Path:
    return path / "main.py" if path.is_dir() else path


def _label(path: str) -> str:
    main = _main_path(_resolve(path))
    return main.parent.name if main.name == "main.py" else main.stem


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _safe(text: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return clean.strip("_") or "item"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _opponent_output(args: argparse.Namespace, opponent_label: str) -> Path:
    candidates = "_".join(_safe(_label(candidate)) for candidate in args.candidate)
    baseline = _safe(_label(args.baseline))
    name = f"paired_{baseline}_vs_{candidates}_opp_{_safe(opponent_label)}.json"
    return (ROOT / args.output_dir / name).resolve()


def _paired_command(
    args: argparse.Namespace,
    opponent: str,
    opponent_index: int,
    output: Path,
) -> list[str]:
    cmd = [
        sys.executable,
        str(PAIRED),
        "--baseline",
        args.baseline,
        "--opponent",
        opponent,
        "--games",
        str(args.games),
        "--workers",
        str(args.workers),
        "--seed-offset",
        str(args.seed_offset + opponent_index * args.opponent_seed_stride),
        "--seed-stride",
        str(args.seed_stride),
        "--bot-seed-stride",
        str(args.bot_seed_stride),
        "--max-hands",
        str(args.max_hands),
        "--output",
        str(output),
    ]
    for candidate in args.candidate:
        cmd.extend(["--candidate", candidate])
    if args.seed_base is not None:
        cmd.extend(["--seed-base", str(args.seed_base)])
    if args.bot_seed_base is not None:
        cmd.extend([
            "--bot-seed-base",
            str(args.bot_seed_base + opponent_index * args.opponent_bot_seed_stride),
        ])
    if args.resume:
        cmd.append("--resume")
    return cmd


def _split(values: list[float]) -> dict[str, int]:
    return {
        "positive": sum(1 for value in values if value > 0.0),
        "zero": sum(1 for value in values if value == 0.0),
        "negative": sum(1 for value in values if value < 0.0),
    }


def _aggregate(args: argparse.Namespace, opponent_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_label = _label(args.baseline)
    candidates = [_label(candidate) for candidate in args.candidate]
    aggregate: dict[str, Any] = {
        "mode": "multi_opponent_common_deck_mirror_pair",
        "baseline": {
            "label": baseline_label,
            "path": _rel(_main_path(_resolve(args.baseline))),
        },
        "candidates": {
            candidate_label: {
                "path": _rel(_main_path(_resolve(candidate_path))),
            }
            for candidate_label, candidate_path in zip(candidates, args.candidate)
        },
        "opponents": [],
        "games_per_opponent": args.games,
        "workers_per_opponent": args.workers,
        "seed_base": args.seed_base,
        "seed_offset": args.seed_offset,
        "seed_stride": args.seed_stride,
        "opponent_seed_stride": args.opponent_seed_stride,
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": args.bot_seed_stride,
        "opponent_bot_seed_stride": args.opponent_bot_seed_stride,
        "max_hands": args.max_hands,
        "aggregate_vs_baseline": {},
    }

    by_candidate: dict[str, list[float]] = {label: [] for label in candidates}
    per_opponent: dict[str, list[dict[str, Any]]] = {label: [] for label in candidates}
    for payload in opponent_payloads:
        opponent_meta = payload.get("_multi_opponent_meta", {})
        opponent_label = str(opponent_meta.get("opponent_label") or "unknown")
        opponent_row = {
            "label": opponent_label,
            "path": opponent_meta.get("opponent_path"),
            "output": opponent_meta.get("output"),
            "seed_offset": payload.get("seed_offset"),
            "bot_seed_base": payload.get("bot_seed_base"),
        }
        aggregate["opponents"].append(opponent_row)
        paired = payload.get("paired_vs_baseline", {})
        for candidate in candidates:
            result = paired.get(candidate)
            if not result:
                continue
            values = [float(value) for value in result.get("delta_net_chips", [])]
            by_candidate[candidate].extend(values)
            per_opponent[candidate].append({
                "opponent": opponent_label,
                "output": opponent_meta.get("output"),
                "samples": result.get("samples"),
                "mean_per_70_hands": result.get("mean_per_70_hands"),
                "ci95_low_per_70_hands": result.get("ci95_low_per_70_hands"),
                "ci95_high_per_70_hands": result.get("ci95_high_per_70_hands"),
                "split": _split(values),
            })

    for candidate, values in by_candidate.items():
        aggregate["aggregate_vs_baseline"][candidate] = {
            "baseline": baseline_label,
            "candidate": candidate,
            "delta_net_chips": values,
            "split": _split(values),
            "by_opponent": per_opponent[candidate],
            **_stats(values, 140),
        }
    return aggregate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", action="append", required=True)
    parser.add_argument("--opponent", action="append", required=True)
    parser.add_argument("--games", type=int, default=4)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed-base", type=int)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--opponent-seed-stride", type=int, default=100000)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-stride", type=int, default=10000)
    parser.add_argument("--opponent-bot-seed-stride", type=int, default=10000000)
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--output-dir", type=Path, default=Path("bots/neural_national_lab/data/multi_opponent_runs"))
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    opponent_payloads: list[dict[str, Any]] = []
    commands: list[list[str]] = []
    for idx, opponent in enumerate(args.opponent):
        opponent_label = _label(opponent)
        output = _opponent_output(args, opponent_label)
        cmd = _paired_command(args, opponent, idx, output)
        commands.append(cmd)
        print(" ".join(cmd), flush=True)
        if args.dry_run:
            continue
        subprocess.run(cmd, cwd=ROOT, check=True)
        payload = _read_json(output)
        payload["_multi_opponent_meta"] = {
            "opponent_label": opponent_label,
            "opponent_path": _rel(_main_path(_resolve(opponent))),
            "output": _rel(output),
        }
        opponent_payloads.append(payload)

    summary_output = args.summary_output if args.summary_output.is_absolute() else ROOT / args.summary_output
    if args.dry_run:
        _write_json(summary_output, {"mode": "multi_opponent_dry_run", "commands": commands})
        return
    summary = _aggregate(args, opponent_payloads)
    _write_json(summary_output, summary)
    for candidate, result in summary["aggregate_vs_baseline"].items():
        print(
            f"aggregate {candidate}: mean70={result['mean_per_70_hands']:.2f} "
            f"ci70=[{result['ci95_low_per_70_hands']}, {result['ci95_high_per_70_hands']}] "
            f"split={result['split']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
