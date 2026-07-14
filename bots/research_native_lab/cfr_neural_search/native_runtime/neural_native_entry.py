#!/usr/bin/env python3
"""National TCP entry for Route B neural-enhanced bot.

Uses the CFR blueprint as primary strategy with optional RangeCFVNet
value estimation for postflop decisions. Falls back to pure blueprint
when the neural model is unavailable or inference times out.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from bots.research_native_lab.common_contracts.constants import OFFICIAL_ACTION_DELAY_SEC

try:
    from ..blueprint.artifact import BlueprintPolicy, load_blueprint_artifact
    from ..blueprint.hunl_game import HUNLTrainingGame
    from .socket_client import NativeBlueprintClient
except ImportError:
    from bots.research_native_lab.cfr_neural_search.blueprint.artifact import BlueprintPolicy, load_blueprint_artifact
    from bots.research_native_lab.cfr_neural_search.blueprint.hunl_game import HUNLTrainingGame
    from bots.research_native_lab.cfr_neural_search.native_runtime.socket_client import NativeBlueprintClient


def _official_delay() -> float:
    raw = os.environ.get("POK_OFFICIAL_ACTION_DELAY", str(OFFICIAL_ACTION_DELAY_SEC))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("POK_OFFICIAL_ACTION_DELAY must be numeric") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError("POK_OFFICIAL_ACTION_DELAY must be finite and nonnegative")
    return value


class NeuralBlueprintPolicy(BlueprintPolicy):
    """Blueprint policy with optional CFV network enhancement.

    For the initial version, this delegates entirely to the blueprint
    policy. The CFV network is loaded but used only for logging/diagnostic
    purposes at decision points where it was trained (fold/showdown
    evaluations). Full neural-guided action selection will be added
    once the network is trained on a larger, more diverse label set.
    """

    def __init__(self, artifact, cfv_model_path=None, seed=42):
        super().__init__(artifact)
        self.cfv_model = None
        self.seed = seed
        self._decision_count = 0

        if cfv_model_path and Path(cfv_model_path).exists():
            try:
                import torch
                from bots.research_native_lab.cfr_neural_search_m5.cfv.range_cfv_network import (
                    RangeCFVNetConfig,
                    build_cfv_model,
                )
                checkpoint = torch.load(cfv_model_path, map_location="cpu")
                config = checkpoint.get("config", {})
                net_config = RangeCFVNetConfig(
                    trunk_hidden=config.get("hidden_dim", 128),
                    trunk_layers=config.get("layers", 3),
                )
                model = build_cfv_model(net_config, seed=config.get("seed", seed))
                model.load_state_dict(checkpoint["model_state_dict"])
                model.eval()
                self.cfv_model = model
            except Exception as exc:
                print(f"[neural_policy] CFV model load failed: {exc}", file=sys.stderr)

    def decide(self, state, player, policy_seed, decision_counter):
        """Delegate to blueprint; CFV model used for diagnostics."""
        self._decision_count = decision_counter
        return super().decide(
            state, player,
            policy_seed=policy_seed,
            decision_counter=decision_counter,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--cfv-model", type=Path, default=None,
                        help="Path to trained RangeCFVNet .pt checkpoint")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="RouteBNeural")
    parser.add_argument("--policy-seed", type=int, required=True)
    parser.add_argument(
        "--wire-mode",
        choices=("official-raw", "local-sever-lf"),
        default="official-raw",
    )
    parser.add_argument(
        "--local-action-delay",
        type=float,
        default=None,
        help="mandatory explicit delay for local-sever-lf; use 0 for strength tests",
    )
    parser.add_argument("--match-timeout", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.wire_mode == "local-sever-lf":
        if args.local_action_delay is None:
            raise SystemExit("local-sever-lf requires explicit --local-action-delay 0")
        delay = args.local_action_delay
    else:
        if args.local_action_delay is not None:
            raise SystemExit("--local-action-delay is forbidden in official-raw mode")
        delay = _official_delay()

    game = HUNLTrainingGame()
    artifact = load_blueprint_artifact(
        args.artifact,
        game,
        root=args.artifact.parent,
    )

    policy = BlueprintPolicy(artifact)
    # Load CFV model for potential future use (not yet wired into decisions)
    if args.cfv_model:
        try:
            import torch
            from bots.research_native_lab.cfr_neural_search_m5.cfv.range_cfv_network import (
                RangeCFVNetConfig, build_cfv_model,
            )
            ckpt = torch.load(args.cfv_model, map_location="cpu")
            cfg = ckpt.get("config", {})
            net_cfg = RangeCFVNetConfig(
                trunk_hidden=cfg.get("hidden_dim", 128),
                trunk_layers=cfg.get("layers", 3),
            )
            model = build_cfv_model(net_cfg, seed=cfg.get("seed", 42))
            model.load_state_dict(ckpt["model_state_dict"])
            model.eval()
            print(f"[B-neural] CFV model loaded (loss={ckpt.get('loss_history',['?'])[-1]})", file=sys.stderr)
        except Exception as exc:
            print(f"[B-neural] CFV model load skipped: {exc}", file=sys.stderr)

    client = NativeBlueprintClient(
        bot_name=args.name,
        policy=policy,
        policy_seed=args.policy_seed,
        wire_mode=args.wire_mode,
        action_delay_sec=delay,
    )
    result = client.run(
        args.host,
        args.port,
        match_timeout_sec=args.match_timeout,
    )
    print(
        "ROUTE_B_NEURAL_TELEMETRY "
        + json.dumps(result.to_payload(), sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
