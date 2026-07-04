#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from engine.battle import battle, mirror_battle  # noqa: E402


def _resolve(path: str) -> Path:
    p = Path(path)
    return (ROOT / p).resolve() if not p.is_absolute() else p


def _write_output(rows: list[dict], output: Path | None) -> None:
    if output is None:
        return
    out = output if output.is_absolute() else ROOT / output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def _battle_row(v_main: Path, o_main: Path, games: int, rows: list[dict], output: Path | None) -> None:
    row = {
        "mode": "battle",
        "version": str(v_main.relative_to(ROOT)),
        "opponent": str(o_main.relative_to(ROOT)),
        "games": games,
        "played": 0,
        "wins": [0, 0],
        "draws": 0,
        "net_chips": [],
        "mean_net": 0.0,
        "median_net": 0.0,
        "per_game": [],
    }
    rows.append(row)
    _write_output(rows, output)

    for game_idx in range(games):
        wins, draws, played, logs = battle(str(v_main), str(o_main), n_games=1, save_log=True)
        row["played"] += played
        row["wins"][0] += wins[0]
        row["wins"][1] += wins[1]
        row["draws"] += draws
        for game_log in logs:
            net = float(game_log.get("bot0_chips", 0.0))
            row["net_chips"].append(net)
            row["per_game"].append(
                {
                    "idx": game_idx,
                    "winner": game_log.get("winner"),
                    "bot0_chips": game_log.get("bot0_chips"),
                    "bot1_chips": game_log.get("bot1_chips"),
                }
            )
        row["mean_net"] = statistics.mean(row["net_chips"]) if row["net_chips"] else 0.0
        row["median_net"] = statistics.median(row["net_chips"]) if row["net_chips"] else 0.0
        _write_output(rows, output)
        print(
            f"{row['version']} vs {row['opponent']} game {game_idx + 1}/{games}: "
            f"mean={row['mean_net']:.1f} wins={row['wins']} nets={row['net_chips']}"
        )


def _mirror_row(v_main: Path, o_main: Path, games: int, rows: list[dict], output: Path | None) -> None:
    row = {
        "mode": "mirror",
        "version": str(v_main.relative_to(ROOT)),
        "opponent": str(o_main.relative_to(ROOT)),
        "games": games,
        "played": 0,
        "wins": [0, 0],
        "draws": 0,
        "net_chips": [],
        "mean_net": 0.0,
        "median_net": 0.0,
    }
    rows.append(row)
    _write_output(rows, output)
    for game_idx in range(games):
        wins, draws, played, _, nets = mirror_battle(str(v_main), str(o_main), n_games=1)
        row["played"] += played
        row["wins"][0] += wins[0]
        row["wins"][1] += wins[1]
        row["draws"] += draws
        row["net_chips"].extend(float(x) for x in nets)
        row["mean_net"] = statistics.mean(row["net_chips"]) if row["net_chips"] else 0.0
        row["median_net"] = statistics.median(row["net_chips"]) if row["net_chips"] else 0.0
        _write_output(rows, output)
        print(
            f"{row['version']} vs {row['opponent']} mirror {game_idx + 1}/{games}: "
            f"mean={row['mean_net']:.1f} wins={row['wins']} nets={row['net_chips']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="append", required=True)
    parser.add_argument("--opponent", action="append", required=True)
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--mode", choices=["battle", "mirror"], default="battle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = []
    for v_arg in args.version:
        v = _resolve(v_arg)
        v_main = v / "main.py" if v.is_dir() else v
        for o_arg in args.opponent:
            o = _resolve(o_arg)
            o_main = o / "main.py" if o.is_dir() else o
            if args.mode == "mirror":
                _mirror_row(v_main, o_main, args.games, rows, args.output)
            else:
                _battle_row(v_main, o_main, args.games, rows, args.output)


if __name__ == "__main__":
    main()
