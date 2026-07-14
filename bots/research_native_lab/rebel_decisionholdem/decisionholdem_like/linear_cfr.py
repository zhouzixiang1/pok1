"""Clean-room alternating Linear CFR for exact Kuhn poker.

Brown and Sandholm (AAAI 2019) define LCFR as CFR with iteration ``t``
weighting both regret updates and average-strategy contributions.  This module
implements that definition directly.  It does not contain or derive from the
AGPL DecisionHoldem source code.  The DecisionHoldem paper and README opening
say Linear CFR, while the same README's framework section calls the shipped
blueprint code MCCFR.  This toy implementation validates the published LCFR
claim but does not resolve that route-level fidelity conflict.
"""

from __future__ import annotations

import hashlib
import json
from math import isfinite
from pathlib import Path

from ..common_runtime.kuhn import (
    InfoSet,
    StrategyProfile,
    all_infosets,
    current_player,
    is_terminal,
    legal_actions,
    next_history,
    ordered_deals,
    terminal_utility,
)

CHECKPOINT_FORMAT = "route-a-kuhn-lcfr-v1"


class LinearCFR:
    """Deterministic full-tree LCFR with alternating player updates."""

    def __init__(self) -> None:
        self.iterations_completed = 0
        self.regrets: dict[InfoSet, list[float]] = {}
        self.strategy_sums: dict[InfoSet, list[float]] = {}
        for key in all_infosets():
            action_count = len(legal_actions(key[2]))
            self.regrets[key] = [0.0] * action_count
            self.strategy_sums[key] = [0.0] * action_count

    def current_strategy(self, key: InfoSet) -> tuple[float, ...]:
        positive = [max(0.0, regret) for regret in self.regrets[key]]
        normalizer = sum(positive)
        if normalizer > 0.0:
            return tuple(regret / normalizer for regret in positive)
        probability = 1.0 / len(positive)
        return tuple(probability for _ in positive)

    def train(self, iterations: int) -> None:
        if iterations < 0:
            raise ValueError("iterations must be non-negative")
        chance_reach = 1.0 / len(ordered_deals())
        for _ in range(iterations):
            iteration = self.iterations_completed + 1
            for update_player in (0, 1):
                # Freeze the policy for the complete alternating update.  An
                # infoset spans multiple chance deals, so applying a deal's
                # regret before traversing the next deal would introduce an
                # invalid chance-order dependency.
                frozen_strategy = {
                    key: self.current_strategy(key) for key in all_infosets()
                }
                regret_deltas = {
                    key: [0.0] * len(values) for key, values in self.regrets.items()
                }
                average_deltas = {
                    key: [0.0] * len(values)
                    for key, values in self.strategy_sums.items()
                }
                for deal in ordered_deals():
                    self._traverse(
                        deal=deal,
                        history="",
                        reaches=(1.0, 1.0),
                        chance_reach=chance_reach,
                        update_player=update_player,
                        frozen_strategy=frozen_strategy,
                        regret_deltas=regret_deltas,
                        average_deltas=average_deltas,
                    )
                iteration_weight = float(iteration)
                for key in all_infosets():
                    for index in range(len(self.regrets[key])):
                        self.regrets[key][index] += (
                            iteration_weight * regret_deltas[key][index]
                        )
                        self.strategy_sums[key][index] += (
                            iteration_weight * average_deltas[key][index]
                        )
            self.iterations_completed = iteration

    def _traverse(
        self,
        *,
        deal: tuple[int, int],
        history: str,
        reaches: tuple[float, float],
        chance_reach: float,
        update_player: int,
        frozen_strategy: dict[InfoSet, tuple[float, ...]],
        regret_deltas: dict[InfoSet, list[float]],
        average_deltas: dict[InfoSet, list[float]],
    ) -> float:
        if is_terminal(history):
            return terminal_utility(deal, history, update_player)

        actor = current_player(history)
        key = (actor, deal[actor], history)
        actions = legal_actions(history)
        strategy = frozen_strategy[key]

        if actor == update_player:
            own_reach = reaches[actor]
            for index, action_probability in enumerate(strategy):
                average_deltas[key][index] += (
                    chance_reach * own_reach * action_probability
                )

        action_values: list[float] = []
        node_value = 0.0
        for index, action in enumerate(actions):
            next_reaches = list(reaches)
            next_reaches[actor] *= strategy[index]
            value = self._traverse(
                deal=deal,
                history=next_history(history, action),
                reaches=(next_reaches[0], next_reaches[1]),
                chance_reach=chance_reach,
                update_player=update_player,
                frozen_strategy=frozen_strategy,
                regret_deltas=regret_deltas,
                average_deltas=average_deltas,
            )
            action_values.append(value)
            node_value += strategy[index] * value

        if actor == update_player:
            counterfactual_reach = chance_reach * reaches[1 - actor]
            for index, value in enumerate(action_values):
                regret_deltas[key][index] += (
                    counterfactual_reach * (value - node_value)
                )
        return node_value

    def average_strategy(self) -> StrategyProfile:
        profile: StrategyProfile = {}
        for key in all_infosets():
            actions = legal_actions(key[2])
            contributions = self.strategy_sums[key]
            normalizer = sum(contributions)
            if normalizer <= 0.0:
                probabilities = [1.0 / len(actions)] * len(actions)
            else:
                probabilities = [value / normalizer for value in contributions]
            profile[key] = dict(zip(actions, probabilities, strict=True))
        return profile

    @staticmethod
    def _encode_key(key: InfoSet) -> str:
        player, card, history = key
        return f"{player}|{card}|{history}"

    @staticmethod
    def _decode_key(encoded: str) -> InfoSet:
        player, card, history = encoded.split("|", 2)
        return int(player), int(card), history

    def checkpoint_payload(self) -> dict[str, object]:
        return {
            "format": CHECKPOINT_FORMAT,
            "game": "kuhn_poker",
            "algorithm": "alternating_linear_cfr",
            "iterations_completed": self.iterations_completed,
            "regrets": {
                self._encode_key(key): list(values)
                for key, values in sorted(self.regrets.items())
            },
            "strategy_sums": {
                self._encode_key(key): list(values)
                for key, values in sorted(self.strategy_sums.items())
            },
        }

    def checkpoint_digest(self) -> str:
        canonical = json.dumps(
            self.checkpoint_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def save_checkpoint(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.checkpoint_payload(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load_checkpoint(cls, path: str | Path) -> "LinearCFR":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("format") != CHECKPOINT_FORMAT:
            raise ValueError(f"unsupported checkpoint format: {payload.get('format')}")
        if payload.get("game") != "kuhn_poker":
            raise ValueError(f"checkpoint is for another game: {payload.get('game')}")
        if payload.get("algorithm") != "alternating_linear_cfr":
            raise ValueError(
                f"checkpoint uses another algorithm: {payload.get('algorithm')}"
            )
        solver = cls()
        completed = payload.get("iterations_completed")
        if type(completed) is not int or completed < 0:
            raise ValueError("checkpoint iteration count must be non-negative")
        solver.iterations_completed = completed
        expected = set(all_infosets())
        for field in ("regrets", "strategy_sums"):
            raw = payload.get(field)
            if not isinstance(raw, dict):
                raise ValueError(f"checkpoint {field} must be an object")
            decoded: dict[InfoSet, list[float]] = {}
            for encoded, values in raw.items():
                try:
                    key = cls._decode_key(encoded)
                except (AttributeError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"checkpoint {field} key is invalid: {encoded!r}"
                    ) from exc
                if cls._encode_key(key) != encoded:
                    raise ValueError(f"checkpoint {field} key is not canonical")
                if key in decoded:
                    raise ValueError(f"checkpoint {field} repeats decoded infoset {key}")
                if not isinstance(values, list) or any(
                    type(value) not in (int, float) for value in values
                ):
                    raise ValueError(f"checkpoint {field} row is not numeric at {key}")
                normalized = [float(value) for value in values]
                if (
                    key not in expected
                    or len(normalized) != len(legal_actions(key[2]))
                    or any(not isfinite(value) for value in normalized)
                    or (
                        field == "strategy_sums"
                        and any(value < 0.0 for value in normalized)
                    )
                ):
                    raise ValueError(f"checkpoint {field} row is invalid at {key}")
                decoded[key] = normalized
            if set(decoded) != expected:
                raise ValueError(f"checkpoint {field} infosets do not match Kuhn")
            setattr(solver, field, decoded)
        return solver
