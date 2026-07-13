"""Alternating Linear CFR over the independent exact Leduc tree.

LCFR's iteration weighting is the paper-faithful algorithmic kernel.  The
particular Leduc rules/tree are a clean-room correctness gate, not evidence
that DecisionHoldem's conflicting LCFR/MCCFR blueprint implementation or its
unreleased assets have been reproduced.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from ..common_runtime.leduc import (
    LeducDeal,
    LeducInfoSet,
    LeducState,
    LeducStrategy,
    actions_by_infoset,
    all_infosets,
    apply_action,
    information_set,
    initial_state,
    legal_actions,
    ordered_deals,
    terminal_utility,
)


CHECKPOINT_FORMAT = "route-a-leduc-lcfr-v1"
CHECKPOINT_GAME = "limit_leduc_clean_room_v1"
CHECKPOINT_ALGORITHM = "alternating_linear_cfr"
CHECKPOINT_FIDELITY = {
    "lcfr": "paper-faithful-clean-room-algorithm",
    "decisionholdem_blueprint": "unresolved-lcfr-vs-mccfr-gap",
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class LeducLinearCFR:
    """Deterministic full-tree Leduc LCFR with alternating updates."""

    def __init__(self) -> None:
        self.iterations_completed = 0
        actions = actions_by_infoset()
        self.regrets: dict[LeducInfoSet, list[float]] = {
            key: [0.0] * len(actions[key]) for key in all_infosets()
        }
        self.strategy_sums: dict[LeducInfoSet, list[float]] = {
            key: [0.0] * len(actions[key]) for key in all_infosets()
        }

    def current_strategy(self, key: LeducInfoSet) -> tuple[float, ...]:
        positive = [max(0.0, regret) for regret in self.regrets[key]]
        total = sum(positive)
        if total > 0.0:
            return tuple(value / total for value in positive)
        probability = 1.0 / len(positive)
        return tuple(probability for _ in positive)

    def train(self, iterations: int) -> None:
        if type(iterations) is not int or iterations < 0:
            raise ValueError("iterations must be a non-negative integer")
        infosets = all_infosets()
        deals = ordered_deals()
        chance_reach = 1.0 / len(deals)
        for _ in range(iterations):
            iteration = self.iterations_completed + 1
            for update_player in (0, 1):
                frozen = {key: self.current_strategy(key) for key in infosets}
                regret_deltas = {
                    key: [0.0] * len(self.regrets[key]) for key in infosets
                }
                average_deltas = {
                    key: [0.0] * len(self.strategy_sums[key]) for key in infosets
                }
                for deal in deals:
                    self._traverse(
                        state=initial_state(),
                        deal=deal,
                        reaches=(1.0, 1.0),
                        chance_reach=chance_reach,
                        update_player=update_player,
                        frozen=frozen,
                        regret_deltas=regret_deltas,
                        average_deltas=average_deltas,
                    )
                weight = float(iteration)
                for key in infosets:
                    for index in range(len(self.regrets[key])):
                        self.regrets[key][index] += (
                            weight * regret_deltas[key][index]
                        )
                        self.strategy_sums[key][index] += (
                            weight * average_deltas[key][index]
                        )
            self.iterations_completed = iteration

    def _traverse(
        self,
        *,
        state: LeducState,
        deal: LeducDeal,
        reaches: tuple[float, float],
        chance_reach: float,
        update_player: int,
        frozen: dict[LeducInfoSet, tuple[float, ...]],
        regret_deltas: dict[LeducInfoSet, list[float]],
        average_deltas: dict[LeducInfoSet, list[float]],
    ) -> float:
        if state.terminal:
            return terminal_utility(state, deal, update_player)
        actor = state.actor
        key = information_set(state, deal)
        actions = legal_actions(state)
        strategy = frozen[key]

        if actor == update_player:
            for index, probability in enumerate(strategy):
                average_deltas[key][index] += (
                    chance_reach * reaches[actor] * probability
                )

        action_values: list[float] = []
        node_value = 0.0
        for index, action in enumerate(actions):
            next_reaches = list(reaches)
            next_reaches[actor] *= strategy[index]
            value = self._traverse(
                state=apply_action(state, action),
                deal=deal,
                reaches=(next_reaches[0], next_reaches[1]),
                chance_reach=chance_reach,
                update_player=update_player,
                frozen=frozen,
                regret_deltas=regret_deltas,
                average_deltas=average_deltas,
            )
            action_values.append(value)
            node_value += strategy[index] * value

        if actor == update_player:
            counterfactual_reach = chance_reach * reaches[1 - actor]
            for index, value in enumerate(action_values):
                regret_deltas[key][index] += counterfactual_reach * (
                    value - node_value
                )
        return node_value

    def average_strategy(self) -> LeducStrategy:
        actions = actions_by_infoset()
        profile: LeducStrategy = {}
        for key in all_infosets():
            accumulated = self.strategy_sums[key]
            total = sum(accumulated)
            if total > 0.0:
                probabilities = [value / total for value in accumulated]
            else:
                probabilities = [1.0 / len(accumulated)] * len(accumulated)
            profile[key] = dict(zip(actions[key], probabilities, strict=True))
        return profile

    @staticmethod
    def _encode_key(key: LeducInfoSet) -> str:
        player, private_rank, public_rank, history = key
        return f"{player}|{private_rank}|{public_rank}|{history}"

    @staticmethod
    def _decode_key(value: str) -> LeducInfoSet:
        player, private_rank, public_rank, history = value.split("|", 3)
        return int(player), int(private_rank), int(public_rank), history

    def checkpoint_payload(self) -> dict[str, object]:
        return {
            "format": CHECKPOINT_FORMAT,
            "game": CHECKPOINT_GAME,
            "algorithm": CHECKPOINT_ALGORITHM,
            "fidelity": dict(CHECKPOINT_FIDELITY),
            "iterations_completed": self.iterations_completed,
            "regrets": {
                self._encode_key(key): list(self.regrets[key])
                for key in all_infosets()
            },
            "strategy_sums": {
                self._encode_key(key): list(self.strategy_sums[key])
                for key in all_infosets()
            },
        }

    def checkpoint_digest(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.checkpoint_payload())).hexdigest()

    def save_checkpoint(self, path: str | Path) -> None:
        rendered = json.dumps(
            self.checkpoint_payload(),
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        _atomic_write(Path(path), rendered)

    @classmethod
    def load_checkpoint(cls, path: str | Path) -> "LeducLinearCFR":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("format") != CHECKPOINT_FORMAT:
            raise ValueError("unsupported Leduc LCFR checkpoint format")
        if payload.get("game") != CHECKPOINT_GAME:
            raise ValueError("checkpoint uses another game")
        if payload.get("algorithm") != CHECKPOINT_ALGORITHM:
            raise ValueError("checkpoint uses another algorithm")
        if payload.get("fidelity") != CHECKPOINT_FIDELITY:
            raise ValueError("checkpoint fidelity boundary differs")
        solver = cls()
        completed = payload.get("iterations_completed")
        if type(completed) is not int or completed < 0:
            raise ValueError("checkpoint iteration count is invalid")
        solver.iterations_completed = completed
        expected_actions = actions_by_infoset()
        expected_keys = set(all_infosets())
        for field in ("regrets", "strategy_sums"):
            raw = payload.get(field)
            if not isinstance(raw, dict):
                raise ValueError(f"checkpoint {field} must be an object")
            decoded: dict[LeducInfoSet, list[float]] = {}
            for encoded, values in raw.items():
                try:
                    key = cls._decode_key(encoded)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"checkpoint {field} key is invalid: {encoded!r}"
                    ) from exc
                if cls._encode_key(key) != encoded:
                    raise ValueError(f"checkpoint {field} key is not canonical")
                if key in decoded:
                    raise ValueError(f"checkpoint {field} repeats decoded infoset {key}")
                if not isinstance(values, list):
                    raise ValueError(f"checkpoint {field} row is not a list")
                if any(type(value) not in (int, float) for value in values):
                    raise ValueError(f"checkpoint {field} row is not numeric at {key}")
                normalized = [float(value) for value in values]
                if (
                    key not in expected_actions
                    or len(normalized) != len(expected_actions[key])
                    or any(not math.isfinite(value) for value in normalized)
                    or (field == "strategy_sums" and any(value < 0.0 for value in normalized))
                ):
                    raise ValueError(f"checkpoint {field} row is invalid at {key}")
                decoded[key] = normalized
            if set(decoded) != expected_keys:
                raise ValueError(f"checkpoint {field} infosets do not match Leduc")
            setattr(solver, field, decoded)
        return solver
