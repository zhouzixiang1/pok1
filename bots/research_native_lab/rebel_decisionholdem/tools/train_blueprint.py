"""Train/resume and atomically export the A2 M4 projection prototype."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..decisionholdem_like.blueprint import (
    BlueprintTrainer,
    export_blueprint_atomic,
    verify_blueprint_export,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    if args.resume and args.checkpoint is None:
        parser.error("--resume requires --checkpoint")
    trainer = (
        BlueprintTrainer.load_checkpoint(args.checkpoint)
        if args.resume
        else BlueprintTrainer()
    )
    trainer.train_to(args.iterations)
    if args.checkpoint is not None:
        trainer.save_checkpoint(args.checkpoint)
    manifest = None
    if args.export is not None:
        manifest = export_blueprint_atomic(trainer, args.export)
        verify_blueprint_export(args.export)
    print(
        json.dumps(
            {
                "iterations_completed": trainer.iterations_completed,
                "checkpoint_digest": trainer.checkpoint_digest(),
                "export_manifest": manifest,
                "fidelity": {
                    "lcfr": "paper-faithful-clean-room-small-game",
                    "national_projection": (
                        "functional-adaptation-not-decisionholdem-blueprint"
                    ),
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
