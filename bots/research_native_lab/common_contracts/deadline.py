"""Anytime decision clock and atomic complete-strategy snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from .actions import Action
from .constants import (
    ABSOLUTE_COMPUTE_STOP_SEC,
    ANYTIME_MILESTONES_SEC,
    DECISION_TIMEOUT_SEC,
)
from .national_state import NationalGameState


@dataclass(frozen=True, slots=True)
class DecisionClock:
    started_at: float
    decision_budget_sec: float
    platform_action_timeout_sec: float
    compute_stop_at: float

    @classmethod
    def start(cls, requested_budget_sec: float) -> "DecisionClock":
        if (
            isinstance(requested_budget_sec, bool)
            or not isinstance(requested_budget_sec, (int, float))
            or not math.isfinite(requested_budget_sec)
            or requested_budget_sec <= 0
        ):
            raise ValueError("decision budget must be positive")
        started = time.monotonic()
        usable = min(float(requested_budget_sec), ABSOLUTE_COMPUTE_STOP_SEC)
        return cls(started, float(requested_budget_sec), DECISION_TIMEOUT_SEC, started + usable)

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def remaining(self) -> float:
        return max(0.0, self.compute_stop_at - time.monotonic())

    def should_stop(self) -> bool:
        return self.remaining() <= 0.0

    def reached_milestones(self) -> tuple[float, ...]:
        elapsed = self.elapsed()
        return tuple(value for value in ANYTIME_MILESTONES_SEC if value <= elapsed)


@dataclass(frozen=True, slots=True)
class StrategySnapshot:
    iteration: int
    probabilities: tuple[tuple[str, float], ...]
    value_estimate: float | None
    published_at: float
    source: str

    def digest(self) -> str:
        payload = {
            "iteration": self.iteration,
            "probabilities": self.probabilities,
            "value_estimate": self.value_estimate,
            "source": self.source,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class SnapshotPublisher:
    """Keep the last complete legal strategy while refinements run."""

    def __init__(
        self,
        state: NationalGameState,
        fallback: Iterable[tuple[str, float]],
        *,
        source: str = "fallback",
    ):
        self._state = state
        self._lock = threading.Lock()
        self._snapshot = self._validate_and_build(0, fallback, None, source)

    def _validate_and_build(
        self,
        iteration: int,
        probabilities: Iterable[tuple[str, float]],
        value_estimate: float | None,
        source: str,
    ) -> StrategySnapshot:
        rows = tuple((str(wire), float(probability)) for wire, probability in probabilities)
        if isinstance(iteration, bool) or not isinstance(iteration, int) or not rows or iteration < 0:
            raise ValueError("a complete snapshot needs actions and a nonnegative iteration")
        if any(not math.isfinite(probability) or probability < 0.0 for _, probability in rows):
            raise ValueError("strategy probabilities must be finite and nonnegative")
        if value_estimate is not None and not math.isfinite(value_estimate):
            raise ValueError("strategy value estimate must be finite")
        if not source:
            raise ValueError("strategy snapshot source is required")
        total = sum(probability for _, probability in rows)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"strategy probabilities must sum to one, got {total}")
        seen: set[str] = set()
        for wire, _ in rows:
            if wire in seen:
                raise ValueError("duplicate action in strategy snapshot")
            seen.add(wire)
            action = Action.from_wire(wire)
            legal, reason = self._state.validate_action(action)
            if not legal:
                raise ValueError(f"snapshot contains illegal action {wire!r}: {reason}")
        return StrategySnapshot(
            iteration=iteration,
            probabilities=rows,
            value_estimate=value_estimate,
            published_at=time.monotonic(),
            source=source,
        )

    def publish(
        self,
        iteration: int,
        probabilities: Iterable[tuple[str, float]],
        *,
        value_estimate: float | None = None,
        source: str,
    ) -> StrategySnapshot:
        snapshot = self._validate_and_build(iteration, probabilities, value_estimate, source)
        with self._lock:
            if iteration == self._snapshot.iteration:
                if snapshot.digest() == self._snapshot.digest():
                    return self._snapshot
                raise ValueError("an iteration cannot be overwritten with different content")
            if iteration < self._snapshot.iteration:
                raise ValueError("strategy iteration must increase monotonically")
            self._snapshot = snapshot
        return snapshot

    def latest(self) -> StrategySnapshot:
        with self._lock:
            return self._snapshot

    def sample(self, seed: int) -> tuple[str, StrategySnapshot]:
        snapshot = self.latest()
        rng = random.Random(seed)
        threshold = rng.random()
        cumulative = 0.0
        for wire, probability in snapshot.probabilities:
            cumulative += probability
            if threshold <= cumulative:
                return wire, snapshot
        return snapshot.probabilities[-1][0], snapshot
