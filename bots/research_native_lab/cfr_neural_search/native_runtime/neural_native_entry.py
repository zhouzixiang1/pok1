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
    from ..blueprint.artifact import BlueprintPolicy, BlueprintDecision, load_blueprint_artifact
    from ..blueprint.hunl_game import HUNLTrainingGame
    from .socket_client import NativeBlueprintClient
except ImportError:
    from bots.research_native_lab.cfr_neural_search.blueprint.artifact import BlueprintPolicy, BlueprintDecision, load_blueprint_artifact
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

    Uses CFV network for depth-1 lookahead action evaluation, opponent
    posteriors for exploitation, and match controller for risk management.
    """

    def __init__(self, artifact, cfv_model_path=None, seed=42):
        super().__init__(artifact)
        self.cfv_model = None
        self.seed = seed
        self._decision_count = 0
        self._cfv_used = 0
        self._blueprint_used = 0
        self._opponent_tracker = None
        self._match_controller = None
        try:
            from ..opponent_model.tracker import OpponentTracker
            from ..match_controller.controller import MatchController
            self._opponent_tracker = OpponentTracker()
            self._match_controller = MatchController()
        except ImportError:
            try:
                from bots.research_native_lab.cfr_neural_search.opponent_model.tracker import OpponentTracker
                from bots.research_native_lab.cfr_neural_search.match_controller.controller import MatchController
                self._opponent_tracker = OpponentTracker()
                self._match_controller = MatchController()
            except Exception:
                pass

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
                self._cfv_combo_count = 1326
            except Exception as exc:
                print(f"[neural_policy] CFV model load failed: {exc}", file=sys.stderr)

    def decide(self, state, player, policy_seed, decision_counter):
        """Use CFV network for depth-1 lookahead; fallback to blueprint."""
        self._decision_count = decision_counter
        if self.cfv_model is not None:
            action = self._cfv_decide(state, player)
            if action is not None:
                self._cfv_used += 1
                return BlueprintDecision(action, "cfv_neural", (), (), 0.0)
        self._blueprint_used += 1
        return super().decide(
            state, player,
            policy_seed=policy_seed,
            decision_counter=decision_counter,
        )

    def _cfv_decide(self, state, player):
        """Depth-1 lookahead using CFV network. Returns Action or None."""
        try:
            import torch
            from bots.research_native_lab.cfr_neural_search_m5.cfv.public_state import (
                PublicHUNLState,
            )
            from bots.research_native_lab.cfr_neural_search_m5.cfv.range_cfv_network import (
                encode_public_state,
            )
            from bots.research_native_lab.cfr_neural_search_m5.cfv.combo_index import (
                COMBO_TO_INDEX,
            )

            legal = state.legal_actions()
            candidate_actions = legal.representative_actions()
            if len(candidate_actions) <= 1:
                return candidate_actions[0] if candidate_actions else None

            our_hole = tuple(sorted(state.hole_cards[player]))
            combo_idx = COMBO_TO_INDEX.get(our_hole)
            if combo_idx is None:
                return None

            range0 = torch.ones(1, self._cfv_combo_count) / self._cfv_combo_count
            range1 = torch.ones(1, self._cfv_combo_count) / self._cfv_combo_count

            best_action = None
            best_value = float("-inf")

            for action in candidate_actions:
                try:
                    next_state = state.apply_action(action)
                except Exception:
                    continue

                if next_state.is_terminal:
                    if next_state.winner == player:
                        val = float(state.pot) / 20000.0
                    elif next_state.winner == 1 - player:
                        val = -float(state.total_contributions[player]) / 20000.0
                    else:
                        val = 0.0
                elif next_state.chance_pending:
                    val = 0.0
                else:
                    try:
                        pub = PublicHUNLState.from_common_state(next_state)
                        pf = encode_public_state(pub).unsqueeze(0)
                        with torch.no_grad():
                            cfv = self.cfv_model(pf, range0, range1)
                        val = float(cfv[0, player, combo_idx])
                    except Exception:
                        continue

                # Apply opponent-model and match-controller adjustments
                if self._opponent_tracker and self._match_controller:
                    adj = self._opponent_tracker.exploit_adjustment()
                    risk = self._match_controller.risk_adjustment()
                    # Boost aggressive actions vs tight opponents
                    if action.kind.value == "raise":
                        val *= adj.get("aggression_mult", 1.0) * risk.get("aggression_mult", 1.0)
                    elif action.kind.value == "allin":
                        val *= adj.get("aggression_mult", 1.0) * risk.get("aggression_mult", 1.0)

                if val > best_value:
                    best_value = val
                    best_action = action

            return best_action
        except Exception as exc:
            print(f"[neural_policy] CFV decide failed: {exc}", file=sys.stderr)
            return None


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

    policy = NeuralBlueprintPolicy(
        artifact,
        cfv_model_path=args.cfv_model,
        seed=args.policy_seed,
    )
    print(
        f"[B-neural] policy=NeuralBlueprintPolicy cfv_model={'loaded' if policy.cfv_model else 'none'}"
        f" seed={args.policy_seed}",
        file=sys.stderr,
    )

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
