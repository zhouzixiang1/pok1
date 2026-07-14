"""Train and evaluate the deterministic M3 small-game solver."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

from bots.research_native_lab.cfr_neural_search.blueprint.evaluation import exploitability
from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
    average_policy,
    load_checkpoint,
    save_checkpoint,
    train_batches,
)
from bots.research_native_lab.cfr_neural_search.blueprint.small_games import make_game


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="JSON experiment configuration")
    parser.add_argument("--game", choices=("kuhn", "leduc"), default="kuhn")
    parser.add_argument(
        "--rule", choices=("vanilla", "linear", "cfr_plus", "dcfr"), default="linear"
    )
    parser.add_argument("--averaging", choices=("sampled", "full"), default="sampled")
    parser.add_argument("--batches", type=int, default=1000)
    parser.add_argument("--samples-per-player", type=int, default=1)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260712)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--resume", action="store_true")
    return parser


def _load_experiment(args: argparse.Namespace) -> tuple[str, SolverConfig, int, int]:
    if args.config is None:
        config = SolverConfig(
            update_rule=args.rule,
            averaging_mode=args.averaging,
            seed=args.seed,
            samples_per_player=args.samples_per_player,
        )
        return args.game, config, args.batches, args.shards
    payload = json.loads(args.config.read_text(encoding="utf-8"))
    return (
        str(payload["game"]),
        SolverConfig.from_payload(payload["solver"]),
        int(payload["batches"]),
        int(payload["shards"]),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    game_name, config, batches, shards = _load_experiment(args)
    game = make_game(game_name)
    if batches < 0 or shards <= 0:
        raise ValueError("batches must be nonnegative and shards must be positive")

    if args.resume:
        if args.checkpoint is None or not args.checkpoint.is_file():
            raise ValueError("--resume requires an existing --checkpoint")
        state = load_checkpoint(args.checkpoint)
        if state.game_name != game.name or state.config != config:
            raise ValueError("checkpoint game/config differs from requested experiment")
    else:
        state = SolverState.new_for_game(game, config)

    started = perf_counter()
    train_batches(game, state, batches=batches, shard_count=shards)
    elapsed = perf_counter() - started
    result = exploitability(game, average_policy(state))
    checkpoint_digest = None
    if args.checkpoint is not None:
        checkpoint_digest = save_checkpoint(args.checkpoint, state)

    summary = {
        "game": game.name,
        "config": config.to_payload(),
        "batches_completed": state.batch_index,
        "batches_added": batches,
        "trajectories": state.trajectories,
        "node_touches": state.node_touches,
        "infosets_seen": len(state.actions),
        "elapsed_seconds": elapsed,
        "batches_per_second": batches / elapsed if elapsed > 0 else None,
        "nodes_per_second": state.node_touches / elapsed if elapsed > 0 else None,
        "on_policy_returns": result.on_policy_returns,
        "best_response_values": result.best_response_values,
        "nash_conv": result.nash_conv,
        "exploitability": result.exploitability,
        "state_sha256": state.digest,
        "checkpoint_sha256": checkpoint_digest,
    }
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
