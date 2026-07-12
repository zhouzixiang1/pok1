"""Run the first A1/A2 correctness gate and emit machine-readable evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..common_runtime.evaluation import exploitability, nash_conv
from ..decisionholdem_like.linear_cfr import LinearCFR
from ..decisionholdem_like.resolving import CoinTossResolveGame
from ..rebel_like.toy_loop import run_toy_selfplay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="load --checkpoint and train until --iterations total iterations",
    )
    parser.add_argument("--output", type=Path)
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.iterations < 0:
        raise ValueError("iterations must be non-negative")
    if args.resume:
        if args.checkpoint is None:
            raise ValueError("--resume requires --checkpoint")
        solver = LinearCFR.load_checkpoint(args.checkpoint)
    else:
        solver = LinearCFR()
    remaining = args.iterations - solver.iterations_completed
    if remaining < 0:
        raise ValueError("checkpoint is newer than the requested iteration target")
    solver.train(remaining)
    if args.checkpoint is not None:
        solver.save_checkpoint(args.checkpoint)

    profile = solver.average_strategy()
    coin_toss = CoinTossResolveGame()
    result = {
        "schema": "route-a-small-game-validation-v1",
        "a1": run_toy_selfplay(seed=args.seed),
        "a2": {
            "iterations": solver.iterations_completed,
            "checkpoint_sha256": solver.checkpoint_digest(),
            "nash_conv": nash_conv(profile),
            "exploitability": exploitability(profile),
            "plain_resolve": coin_toss.plain_resolve().to_dict(),
            "safe_resolve": coin_toss.safe_resolve().to_dict(),
        },
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return result


def main() -> int:
    args = build_parser().parse_args()
    print(json.dumps(run(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
