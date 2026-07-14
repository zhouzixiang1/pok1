"""Fail-closed adapter from frozen Common state into route-B decisions.

This module intentionally does not define another poker state, action enum, or
legality oracle.  It keeps the exact Common ``NationalGameState``, ``Action``,
and ``LegalActionSet`` objects at the runtime boundary.  The only route-local
objects are immutable bindings that prevent a computation made for one state
from being submitted after the Common state has advanced.

The adapter is an M3 integration seam, not a native TCP bot.  A later socket
owner will still have to bind the Common protocol decision lease, deadline,
resource receipt, and wire capture before this can be used in a match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bots.research_native_lab.common_contracts import (
    Action,
    LegalActionSet,
    NationalGameState,
)

# Content binding for the exact Common seam audited by route B.  The route
# test suite hashes these files from the merged checkout, so a later Common
# state/action/protocol drift reopens this M3 integration gate instead of being
# silently accepted.  The complete Common tree binding is recorded separately
# in the route M3 manifest.
COMMON_CONTRACT_COMMIT = "a938d7cbc36016cb7b5cb444a7eb2e0f00cae73e"
COMMON_CONTRACT_GIT_TREE = "8066a0741bfefc42026d098f0ffc46cbfb424f45"
COMMON_RUNTIME_FILE_SHA256 = (
    ("__init__.py", "5b843901602df8299f3fd845b346385fa6ff87c9aa807ef0023abf55ff8ff384"),
    ("actions.py", "69d1f5667f35ef7db3092f8afc358d8fa14f26f430246983caac8d1ac43dacaa"),
    ("cards.py", "492e89baf3b1db4f9b87f62d5f63964e22fdc998e27928c8bcb75bec6df52bce"),
    ("constants.py", "8f7116becae35ccbdf6d1ff5004a7b07dec7b6ac793ecb6d55bd05ffc8818783"),
    ("national_state.py", "6bdb467fcedf114948843419ffdf58abd8c5e545243fb6b63a15d6be6d02dbc4"),
    ("protocol.py", "0009ef501cb00e303d530dfb254695bde9076071c3c01de552eaf364ed1ddd2e"),
    (
        "contracts/national_game_v1.json",
        "e23831c0e83349a576658938b450b044cf527a1c4452284b6efa21445c09ffab",
    ),
)


class CommonContractAdapterError(ValueError):
    """The Common state/action boundary was stale or otherwise invalid."""


class NationalPolicy(Protocol):
    """The mandatory Common-typed entry point for later route-B policies."""

    def __call__(self, decision: "NationalDecisionSnapshot") -> Action:
        """Return a shared Common action for the immutable decision snapshot."""


def _require_sha256_identity(value: object, context: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{context} must be an exact string")
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise CommonContractAdapterError(
            f"{context} must be a 64-character lowercase SHA-256 identity"
        )


def _require_common_state(state: NationalGameState) -> None:
    if type(state) is not NationalGameState:
        raise TypeError("state must be the exact shared Common NationalGameState type")
    # This invokes Common's replay-backed history guard.  A dataclass copy or
    # forged route-local lookalike cannot cross the adapter boundary.
    state.assert_invariants()


@dataclass(frozen=True, slots=True)
class BoundNationalAction:
    """A Common action bound to the exact full-state identity it was chosen for."""

    full_state_id: str
    action: Action

    def __post_init__(self) -> None:
        _require_sha256_identity(self.full_state_id, "bound full_state_id")
        if type(self.action) is not Action:
            raise TypeError("bound action must use the exact shared Common Action type")

    @property
    def wire_action(self) -> str:
        """Return Common's exact official-protocol serialization."""

        return self.action.to_wire()

    def apply_to(self, current_state: NationalGameState) -> NationalGameState:
        """Apply only if the socket owner's Common state is still identical."""

        _require_common_state(current_state)
        if current_state.full_state_id() != self.full_state_id:
            raise CommonContractAdapterError("bound action is stale for current state")
        legal, reason = current_state.validate_action(self.action)
        if not legal:
            raise CommonContractAdapterError(
                f"Common legality oracle rejected bound action: {reason}"
            )
        return current_state.apply_action(self.action)


@dataclass(frozen=True, slots=True)
class NationalDecisionSnapshot:
    """Read-only route view whose semantic fields remain Common-owned."""

    state: NationalGameState
    controlled_player: int
    full_state_id: str
    public_state_id: str
    information_state_id: str
    legal_actions: LegalActionSet

    def __post_init__(self) -> None:
        _require_common_state(self.state)
        if type(self.controlled_player) is not int or self.controlled_player not in (0, 1):
            raise CommonContractAdapterError("controlled_player must be 0 or 1")
        if self.state.actor != self.controlled_player:
            raise CommonContractAdapterError(
                "Common state is not a pending decision for controlled_player"
            )
        _require_sha256_identity(self.full_state_id, "snapshot full_state_id")
        _require_sha256_identity(self.public_state_id, "snapshot public_state_id")
        _require_sha256_identity(
            self.information_state_id,
            "snapshot information_state_id",
        )
        if type(self.legal_actions) is not LegalActionSet:
            raise TypeError(
                "snapshot legal_actions must be the exact shared Common LegalActionSet type"
            )
        expected = (
            self.state.full_state_id(),
            self.state.hand_public_state_id(),
            self.state.information_state_id(self.controlled_player),
            self.state.legal_actions(),
        )
        actual = (
            self.full_state_id,
            self.public_state_id,
            self.information_state_id,
            self.legal_actions,
        )
        if actual != expected:
            raise CommonContractAdapterError(
                "snapshot fields disagree with the shared Common state"
            )
        if not self.legal_actions.representative_actions():
            raise CommonContractAdapterError("pending Common decision has no legal action")

    def representative_actions(self) -> tuple[Action, ...]:
        """Return Common actions only; no route-local legality approximation."""

        actions = self.legal_actions.representative_actions()
        for action in actions:
            legal, reason = self.state.validate_action(action)
            if not legal:
                raise CommonContractAdapterError(
                    f"Common LegalActionSet/state disagreement: {reason}"
                )
        return actions

    def bind(
        self,
        action: Action,
        *,
        current_state: NationalGameState,
    ) -> BoundNationalAction:
        """Validate a Common action and bind it against a fresh Common state read."""

        if type(action) is not Action:
            raise TypeError("route policy must return the exact shared Common Action type")
        _require_common_state(current_state)
        if current_state.full_state_id() != self.full_state_id:
            raise CommonContractAdapterError("decision snapshot is stale")
        if not self.legal_actions.contains(action):
            raise CommonContractAdapterError(
                "action lies outside the frozen Common LegalActionSet"
            )
        legal, reason = current_state.validate_action(action)
        if not legal:
            raise CommonContractAdapterError(
                f"Common legality oracle rejected action: {reason}"
            )
        return BoundNationalAction(self.full_state_id, action)


def adapt_national_decision(
    state: NationalGameState,
    *,
    controlled_player: int = 0,
) -> NationalDecisionSnapshot:
    """Create an identity-bound decision view from a trusted Common state."""

    _require_common_state(state)
    if type(controlled_player) is not int or controlled_player not in (0, 1):
        raise CommonContractAdapterError("controlled_player must be 0 or 1")
    if state.actor != controlled_player:
        raise CommonContractAdapterError(
            "Common state is not a pending decision for controlled_player"
        )
    return NationalDecisionSnapshot(
        state=state,
        controlled_player=controlled_player,
        full_state_id=state.full_state_id(),
        public_state_id=state.hand_public_state_id(),
        information_state_id=state.information_state_id(controlled_player),
        legal_actions=state.legal_actions(),
    )


def invoke_route_policy(
    state: NationalGameState,
    policy: NationalPolicy,
    *,
    controlled_player: int = 0,
) -> BoundNationalAction:
    """Invoke a route policy only through the tested Common-typed boundary.

    This function is the future strategy entry seam.  It does not provide a
    policy itself: M3 has no HUNL blueprint.  A later blueprint/search module
    must implement ``NationalPolicy`` and the socket owner must still submit
    the returned binding through Common's decision-lease runtime.
    """

    snapshot = adapt_national_decision(
        state,
        controlled_player=controlled_player,
    )
    action = policy(snapshot)
    return snapshot.bind(action, current_state=state)
