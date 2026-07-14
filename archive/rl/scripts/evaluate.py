#!/usr/bin/env python3
"""HoldemRL evaluation entry point.

Usage:
    python -m rl.scripts.evaluate --checkpoint rl/checkpoints/best_model.pt
    python -m rl.scripts.evaluate --checkpoint rl/checkpoints/best_model.pt --games 500 --opponent aggro
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import torch

from rl.core.config import HoldemRLConfig
from rl.training.trainer import DMCTrainer, build_model
from rl.eval import evaluate, RandomOpponent, AlwaysCallOpponent, AggressiveOpponent


def main():
    parser = argparse.ArgumentParser(description="HoldemRL Evaluation")
    parser.add_argument("--checkpoint", type=str, required=True)
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--opponent", type=str, default="random",
                        choices=["random", "callbot", "aggro", "all"])
    parser.add_argument("--device", type=str, default="cpu")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    # Load checkpoint
    ckpt = torch.load(args.checkpoint, map_location=args.device, weights_only=False)
    config = HoldemRLConfig.from_dict(ckpt["config"])

    model = build_model(config).to(args.device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()

    print(f"Evaluating: {args.checkpoint}")
    print(f"Config: arch={config.architecture}, cycle={ckpt.get('cycle', '?')}, "
          f"steps={ckpt.get('total_steps', '?')}")
    print()

    opponents = {
        "random": ("Random", RandomOpponent(seed=args.seed)),
        "callbot": ("Always-Call", AlwaysCallOpponent()),
        "aggro": ("Aggressive", AggressiveOpponent(seed=args.seed)),
    }

    if args.opponent == "all":
        selected = list(opponents.items())
    else:
        selected = [(args.opponent, opponents[args.opponent])]

    for name, (display_name, opp) in selected:
        result = evaluate(model, opp, num_games=args.games, seed=args.seed, device=args.device)
        print(f"vs {display_name}: win_rate={result['win_rate']:.1%} "
              f"({result['wins']}W/{result['losses']}L/{result['draws']}D) "
              f"avg_reward={result['avg_reward']:.2f} ± {result['std_reward']:.2f}")


if __name__ == "__main__":
    main()
