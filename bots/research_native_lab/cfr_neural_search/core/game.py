"""Minimal extensive-form game interfaces for solver correctness tests."""

from __future__ import annotations

from typing import Hashable, Protocol, Sequence, TypeAlias

Action: TypeAlias = Hashable
CHANCE_PLAYER = -1
TERMINAL_PLAYER = -2


class GameState(Protocol):
    """Immutable, perfect-recall, sequential extensive-form state."""

    @property
    def current_player(self) -> int:
        """Return a player id, ``CHANCE_PLAYER``, or ``TERMINAL_PLAYER``."""

    @property
    def depth(self) -> int:
        """Return a monotonic history depth used by diagnostics."""

    def legal_actions(self) -> tuple[Action, ...]:
        """Return legal actions for the current decision node."""

    def chance_outcomes(self) -> tuple[tuple[Action, float], ...]:
        """Return normalized chance outcomes at a chance node."""

    def child(self, action: Action) -> "GameState":
        """Return the immutable successor after ``action``."""

    def information_state_key(self, player: int) -> str:
        """Return a perfect-recall information-set key for ``player``."""

    def returns(self) -> tuple[float, float]:
        """Return terminal zero-sum utilities."""


class ExtensiveGame(Protocol):
    """Factory for a finite two-player zero-sum sequential game."""

    name: str

    def new_initial_state(self) -> GameState:
        """Return a fresh immutable root state."""


def validate_distribution(outcomes: Sequence[tuple[Action, float]]) -> None:
    """Raise when a chance distribution is malformed."""

    if not outcomes:
        raise ValueError("chance node must expose at least one outcome")
    if any(probability <= 0.0 for _, probability in outcomes):
        raise ValueError("chance probabilities must be positive")
    total = sum(probability for _, probability in outcomes)
    if abs(total - 1.0) > 1e-12:
        raise ValueError(f"chance probabilities sum to {total}, expected 1")
