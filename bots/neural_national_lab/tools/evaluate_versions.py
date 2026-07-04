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


def _net_stats(values: list[float], hands_per_unit: int) -> dict[str, float | int | bool | None]:
    n = len(values)
    mean = statistics.mean(values) if values else 0.0
    median = statistics.median(values) if values else 0.0
    stddev = statistics.stdev(values) if n >= 2 else 0.0
    stderr = stddev / (n ** 0.5) if n >= 2 else 0.0
    ci_delta = 1.96 * stderr if n >= 2 else 0.0
    per_70_scale = 70.0 / max(1, hands_per_unit)
    return {
        "samples": n,
        "mean_net": mean,
        "median_net": median,
        "stddev_net": stddev,
        "stderr_net": stderr,
        "ci95_low": mean - ci_delta if n >= 2 else None,
        "ci95_high": mean + ci_delta if n >= 2 else None,
        "mean_per_hand": mean / max(1, hands_per_unit),
        "mean_per_70_hands": mean * per_70_scale,
        "ci95_low_per_70_hands": (mean - ci_delta) * per_70_scale if n >= 2 else None,
        "ci95_high_per_70_hands": (mean + ci_delta) * per_70_scale if n >= 2 else None,
        "significant_positive_95": bool(n >= 2 and mean - ci_delta > 0.0),
        "significant_negative_95": bool(n >= 2 and mean + ci_delta < 0.0),
    }


def _update_net_stats(row: dict, hands_per_unit: int) -> None:
    row.update(_net_stats(row["net_chips"], hands_per_unit))


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
        **_net_stats([], 70),
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
        _update_net_stats(row, 70)
        _write_output(rows, output)
        print(
            f"{row['version']} vs {row['opponent']} game {game_idx + 1}/{games}: "
            f"mean70={row['mean_per_70_hands']:.1f} ci70="
            f"[{row['ci95_low_per_70_hands']}, {row['ci95_high_per_70_hands']}] "
            f"wins={row['wins']} nets={row['net_chips']}"
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
        **_net_stats([], 140),
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
        _update_net_stats(row, 140)
        _write_output(rows, output)
        print(
            f"{row['version']} vs {row['opponent']} mirror {game_idx + 1}/{games}: "
            f"mean70={row['mean_per_70_hands']:.1f} ci70="
            f"[{row['ci95_low_per_70_hands']}, {row['ci95_high_per_70_hands']}] "
            f"wins={row['wins']} nets={row['net_chips']}"
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
