#!/usr/bin/env python3
"""HoldemRL training entry point.

Usage:
    python -m rl.scripts.train                  # Default MLP training
    python -m rl.scripts.train --arch transformer  # Transformer training
    python -m rl.scripts.train --cycles 1000    # Train for 1000 cycles
    python -m rl.scripts.train --device cpu     # Force CPU
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Add project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rl.core.config import HoldemRLConfig
from rl.training.trainer import DMCTrainer
from rl.eval import evaluate, RandomOpponent, AlwaysCallOpponent, AggressiveOpponent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("holdem_rl.train")


def main():
    parser = argparse.ArgumentParser(description="HoldemRL Training")
    parser.add_argument("--arch", type=str, default="mlp", choices=["mlp", "transformer"])
    parser.add_argument("--cycles", type=int, default=1000)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--batch-size", type=int, default=2048)
    parser.add_argument("--buffer-size", type=int, default=100_000)
    parser.add_argument("--num-actors", type=int, default=4)
    parser.add_argument("--ckpt-dir", type=str, default="rl/checkpoints")
    parser.add_argument("--resume", type=str, default=None, help="Checkpoint to resume from")
    parser.add_argument("--eval-every", type=int, default=10, help="Eval every N cycles")
    parser.add_argument("--eval-games", type=int, default=100)
    args = parser.parse_args()

    # Config
    config = HoldemRLConfig(
        architecture=args.arch,
        lr=args.lr,
        batch_size=args.batch_size,
        replay_buffer_size=args.buffer_size,
        num_actors=args.num_actors,
        ckpt_dir=args.ckpt_dir,
        eval_every_cycles=args.eval_every,
        eval_num_games=args.eval_games,
        device=args.device,
    )

    log.info(f"Config: arch={config.architecture}, lr={config.lr}, "
             f"buffer={config.replay_buffer_size}, actors={config.num_actors}")

    # Create trainer
    trainer = DMCTrainer(config)

    if args.resume:
        trainer.load_checkpoint(args.resume)

    # Create checkpoint directory
    os.makedirs(args.ckpt_dir, exist_ok=True)

    # Training loop
    t0 = time.time()
    for cycle in range(trainer.cycle, args.cycles):
        trainer.train_cycle()

        # Logging
        eps = trainer._get_epsilon()
        buffer_size = len(trainer.buffer)

        if (cycle + 1) % 5 == 0:
            elapsed = time.time() - t0
            log.info(f"Cycle {cycle+1}/{args.cycles} | "
                     f"steps={trainer.total_steps} | "
                     f"eps={eps:.3f} | "
                     f"buffer={buffer_size} | "
                     f"elapsed={elapsed:.0f}s")

        # Evaluation
        if (cycle + 1) % config.eval_every_cycles == 0:
            opponents = {
                "random": RandomOpponent(seed=42),
                "callbot": AlwaysCallOpponent(),
                "aggro": AggressiveOpponent(seed=42),
            }

            log.info(f"--- Evaluation at cycle {cycle+1} ---")
            for name, opp in opponents.items():
                result = evaluate(
                    trainer.model, opp,
                    num_games=config.eval_num_games,
                    seed=42,
                    device=config.device,
                )
                log.info(f"  vs {name}: win_rate={result['win_rate']:.1%} "
                         f"({result['wins']}W/{result['losses']}L/{result['draws']}D) "
                         f"avg_reward={result['avg_reward']:.2f}")

                # Save best model
                if name == "random" and result['avg_reward'] > trainer.best_eval_reward:
                    trainer.best_eval_reward = result['avg_reward']
                    best_path = os.path.join(args.ckpt_dir, "best_model.pt")
                    trainer.save_checkpoint(best_path)

            # Periodic checkpoint
            ckpt_path = os.path.join(args.ckpt_dir, f"cycle_{cycle+1:06d}.pt")
            trainer.save_checkpoint(ckpt_path)

    # Final save
    final_path = os.path.join(args.ckpt_dir, "final_model.pt")
    trainer.save_checkpoint(final_path)
    log.info(f"Training complete. Final model saved to {final_path}")
    log.info(f"Total time: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
