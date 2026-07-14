#!/usr/bin/env python3
"""HoldemRL training entry point.

Usage:
    python -m rl.scripts.train                  # Default MLP training
    python -m rl.scripts.train --arch transformer  # Transformer training
    python -m rl.scripts.train --cycles 1000    # Train for 1000 cycles
    python -m rl.scripts.train --device cpu     # Force CPU
    python -m rl.scripts.train --num-actors 8   # 8 parallel actor processes
    python -m rl.scripts.train --tensorboard    # Enable TensorBoard logging
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
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
    parser.add_argument("--num-actors", type=int, default=4,
                        help="Number of parallel actor processes (1=in-process)")
    parser.add_argument("--ckpt-dir", type=str, default="rl/checkpoints")
    parser.add_argument("--resume", type=str, default=None, help="Checkpoint to resume from")
    parser.add_argument("--eval-every", type=int, default=10, help="Eval every N cycles")
    parser.add_argument("--eval-games", type=int, default=100)
    parser.add_argument("--hands-per-actor", type=int, default=100,
                        help="Hands each actor plays per cycle (default: 100)")
    parser.add_argument("--tensorboard", action="store_true", help="Enable TensorBoard logging")
    parser.add_argument("--log-dir", type=str, default="rl/logs", help="TensorBoard log dir")
    args = parser.parse_args()

    # Config
    config = HoldemRLConfig(
        architecture=args.arch,
        lr=args.lr,
        batch_size=args.batch_size,
        replay_buffer_size=args.buffer_size,
        num_actors=args.num_actors,
        actor_hands_per_cycle=args.hands_per_actor,
        ckpt_dir=args.ckpt_dir,
        eval_every_cycles=args.eval_every,
        eval_num_games=args.eval_games,
        device=args.device,
    )

    # TensorBoard (optional)
    writer = None
    if args.tensorboard:
        try:
            from torch.utils.tensorboard import SummaryWriter
            log_dir = os.path.join(args.log_dir, f"{args.arch}_{time.strftime('%Y%m%d_%H%M%S')}")
            writer = SummaryWriter(log_dir=log_dir)
            log.info(f"TensorBoard logging to {log_dir}")
            log.info(f"Start TensorBoard: tensorboard --logdir {args.log_dir}")
        except ImportError:
            log.warning("tensorboard not installed. Run: pip install tensorboard")
            args.tensorboard = False

    # Print config summary
    n_params = sum(p.numel() for p in build_model_for_count(config).parameters())
    log.info(f"{'='*60}")
    log.info(f"HoldemRL Training Configuration")
    log.info(f"{'='*60}")
    log.info(f"  Architecture:  {config.architecture}")
    log.info(f"  Parameters:    {n_params:,}")
    log.info(f"  Device:        {config.device}")
    log.info(f"  Actors:        {config.num_actors} "
             f"({'parallel subprocess' if config.num_actors > 1 else 'in-process'})")
    log.info(f"  LR:            {config.lr}")
    log.info(f"  Batch size:    {config.batch_size}")
    log.info(f"  Buffer:        {config.replay_buffer_size:,}")
    log.info(f"  Cycles:        {args.cycles}")
    log.info(f"  Eval every:    {config.eval_every_cycles} cycles ({config.eval_num_games} games)")
    log.info(f"  Hands/cycle:   {config.actor_hands_per_cycle * config.num_actors:,} "
             f"({config.actor_hands_per_cycle} × {config.num_actors} actors)")
    log.info(f"{'='*60}")

    # Create trainer
    trainer = DMCTrainer(config)

    if args.resume:
        trainer.load_checkpoint(args.resume)

    # Cleanup on exit
    def _cleanup(*_):
        log.info("Shutting down...")
        trainer.shutdown()
        if writer:
            writer.close()

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    # Create checkpoint directory
    os.makedirs(args.ckpt_dir, exist_ok=True)

    # Training loop
    t0 = time.time()
    try:
        for cycle in range(trainer.cycle, args.cycles):
            cycle_t0 = time.time()
            trainer.train_cycle()
            cycle_time = time.time() - cycle_t0

            eps = trainer._get_epsilon()
            buffer_size = len(trainer.buffer)
            loss = trainer._last_loss
            collect_time = trainer._last_collect_time

            # Log every cycle
            if writer:
                global_step = trainer.total_steps
                writer.add_scalar("train/loss", loss, global_step)
                writer.add_scalar("train/epsilon", eps, global_step)
                writer.add_scalar("train/buffer_size", buffer_size, global_step)
                writer.add_scalar("train/cycle_time_s", cycle_time, cycle)
                writer.add_scalar("train/collect_time_s", collect_time, cycle)

            # Verbose log every 5 cycles
            if (cycle + 1) % 5 == 0:
                elapsed = time.time() - t0
                cycles_left = args.cycles - (cycle + 1)
                eta = (elapsed / (cycle + 1 - trainer.cycle)) * cycles_left if cycle > trainer.cycle else 0
                log.info(
                    f"Cycle {cycle+1}/{args.cycles} | "
                    f"steps={trainer.total_steps} | "
                    f"loss={loss:.4f} | "
                    f"eps={eps:.3f} | "
                    f"buffer={buffer_size:,} | "
                    f"collect={collect_time:.1f}s | "
                    f"total={cycle_time:.1f}s | "
                    f"elapsed={elapsed:.0f}s | "
                    f"ETA={eta/60:.0f}min"
                )

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
                    log.info(f"  vs {name:8s}: win={result['win_rate']:.1%} "
                             f"({result['wins']}W/{result['losses']}L/{result['draws']}D) "
                             f"reward={result['avg_reward']:+.1f} "
                             f"std={result['std_reward']:.1f}")

                    if writer:
                        writer.add_scalar(f"eval/{name}_win_rate", result['win_rate'], cycle)
                        writer.add_scalar(f"eval/{name}_avg_reward", result['avg_reward'], cycle)

                    # Save best model
                    if name == "random" and result['avg_reward'] > trainer.best_eval_reward:
                        trainer.best_eval_reward = result['avg_reward']
                        best_path = os.path.join(args.ckpt_dir, "best_model.pt")
                        trainer.save_checkpoint(best_path)

                # Periodic checkpoint
                ckpt_path = os.path.join(args.ckpt_dir, f"cycle_{cycle+1:06d}.pt")
                trainer.save_checkpoint(ckpt_path)

    except KeyboardInterrupt:
        log.info("Training interrupted by user")

    # Final save
    final_path = os.path.join(args.ckpt_dir, "final_model.pt")
    trainer.save_checkpoint(final_path)
    trainer.shutdown()

    elapsed = time.time() - t0
    log.info(f"Training complete. {trainer.cycle} cycles, {trainer.total_steps} steps in {elapsed:.0f}s")
    log.info(f"Final model: {final_path}")

    if writer:
        writer.close()


def build_model_for_count(config: HoldemRLConfig):
    """Build model just for parameter counting (no GPU needed)."""
    from rl.training.trainer import build_model
    return build_model(config)


if __name__ == "__main__":
    main()
