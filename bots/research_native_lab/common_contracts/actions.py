"""Strict wire actions and finite bounds over the national action space."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ActionKind(str, Enum):
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    RAISE = "raise"
    ALLIN = "allin"


_RAISE_RE = re.compile(r"raise ([0-9]+)")


@dataclass(frozen=True, slots=True)
class Action:
    """A semantic action.

    ``amount`` is populated only for ``RAISE`` and is always a street
    raise-to total, never a delta.
    """

    kind: ActionKind
    amount: int | None = None

    def __post_init__(self) -> None:
        if self.kind is ActionKind.RAISE:
            if not isinstance(self.amount, int) or isinstance(self.amount, bool):
                raise ValueError("raise requires an integer raise-to amount")
        elif self.amount is not None:
            raise ValueError(f"{self.kind.value} must not carry an amount")

    @classmethod
    def from_wire(cls, raw: str) -> "Action":
        """Parse a client action without stripping illegal whitespace."""

        match = _RAISE_RE.fullmatch(raw)
        if match:
            return cls(ActionKind.RAISE, int(match.group(1)))
        try:
            kind = ActionKind(raw)
        except ValueError as exc:
            raise ValueError(f"invalid national action wire text: {raw!r}") from exc
        return cls(kind)

    def to_wire(self) -> str:
        if self.kind is ActionKind.RAISE:
            return f"raise {self.amount}"
        return self.kind.value


@dataclass(frozen=True, slots=True)
class LegalActionSet:
    """Finite description of legal actions, including a raise interval.

    ``max_raise_to`` excludes the all-in total because the national protocol
    requires the keyword ``allin`` when the raise would consume every
    remaining chip.  A missing interval is represented by two ``None`` values.
    """

    fold: bool
    check: bool
    call: bool
    allin: bool
    min_raise_to: int | None
    max_raise_to: int | None

    def contains(self, action: Action) -> bool:
        if action.kind is ActionKind.FOLD:
            return self.fold
        if action.kind is ActionKind.CHECK:
            return self.check
        if action.kind is ActionKind.CALL:
            return self.call
        if action.kind is ActionKind.ALLIN:
            return self.allin
        assert action.kind is ActionKind.RAISE
        return (
            self.min_raise_to is not None
            and self.max_raise_to is not None
            and self.min_raise_to <= int(action.amount) <= self.max_raise_to
        )

    def representative_actions(self) -> tuple[Action, ...]:
        """Return deterministic actions suitable for fuzzing and fallbacks."""

        actions: list[Action] = []
        for enabled, kind in (
            (self.fold, ActionKind.FOLD),
            (self.check, ActionKind.CHECK),
            (self.call, ActionKind.CALL),
        ):
            if enabled:
                actions.append(Action(kind))
        if self.min_raise_to is not None:
            actions.append(Action(ActionKind.RAISE, self.min_raise_to))
            if self.max_raise_to != self.min_raise_to:
                actions.append(Action(ActionKind.RAISE, self.max_raise_to))
        if self.allin:
            actions.append(Action(ActionKind.ALLIN))
        return tuple(actions)
