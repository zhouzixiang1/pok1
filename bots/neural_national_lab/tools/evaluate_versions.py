#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from engine.battle import mirror_battle  # noqa: E402


def _resolve(path: str) -> Path:
    p = Path(path)
    return (ROOT / p).resolve() if not p.is_absolute() else p


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="append", required=True)
    parser.add_argument("--opponent", action="append", required=True)
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = []
    for v_arg in args.version:
        v = _resolve(v_arg)
        v_main = v / "main.py" if v.is_dir() else v
        for o_arg in args.opponent:
            o = _resolve(o_arg)
            o_main = o / "main.py" if o.is_dir() else o
            wins, draws, played, _, nets = mirror_battle(str(v_main), str(o_main), n_games=args.games)
            row = {
                "version": str(v_main.relative_to(ROOT)),
                "opponent": str(o_main.relative_to(ROOT)),
                "games": args.games,
                "played": played,
                "wins": wins,
                "draws": draws,
                "net_chips": nets,
                "mean_net": statistics.mean(nets) if nets else 0.0,
                "median_net": statistics.median(nets) if nets else 0.0,
            }
            rows.append(row)
            print(f"{row['version']} vs {row['opponent']}: mean={row['mean_net']:.1f} wins={wins} nets={nets}")
    if args.output:
        out = args.output if args.output.is_absolute() else ROOT / args.output
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
