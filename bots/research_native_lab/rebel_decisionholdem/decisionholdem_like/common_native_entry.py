"""Common-authoritative A2 strategy runtime for the M3 integration seam.

The socket owner may feed decoded platform tokens and transmit only the strings
returned here.  State reconstruction, one-shot decision leases and legality are
owned by ``common_contracts``; A2 contributes only abstraction and policy
sampling.  Network lifecycle/deadline productization remains M10 work, and the
older self-contained export shell remains labelled an M4 projection prototype.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

from ...common_contracts.protocol import (
    NationalProtocolSession,
    ProtocolEvent,
    StreamDecoder,
)
from .a2_runtime import SparseBlueprint
from .common_adapter import choose_blueprint_action_from_common_state


@dataclass(frozen=True, slots=True)
class CommonDecisionTrace:
    hand_number: int
    decision_number: int
    decision_id: int
    information_state_id: str
    action: str
    legal_actions: tuple[str, ...]
    lookup_key: str
    full_state_id: str
    used_legality_fallback: bool
    available_policy_mass: float
    dropped_policy_mass: float
    legal_raise_bounds: tuple[int | None, int | None]


class CommonA2StrategyRuntime:
    """Actual A2 policy entry over Common state/lease/action contracts."""

    def __init__(self, name: str, blueprint: SparseBlueprint, seed: int = 0) -> None:
        self.session = NationalProtocolSession(name)
        self.blueprint = blueprint
        self.seed = int(seed)
        self.decoder = StreamDecoder()
        self.decisions_completed = 0
        self.trace: list[CommonDecisionTrace] = []

    @classmethod
    def from_path(
        cls,
        name: str,
        blueprint_path: str | Path,
        seed: int = 0,
    ) -> "CommonA2StrategyRuntime":
        return cls(name, SparseBlueprint.load(blueprint_path), seed)

    def _random_unit(
        self,
        information_state_id: str,
        decision_number: int,
    ) -> float:
        """Counter-based policy RNG, separate from the strategy lookup key."""

        material = (
            f"{self.seed}|{decision_number}|{information_state_id}|{self.blueprint.digest}"
        ).encode("ascii")
        return int.from_bytes(hashlib.sha256(material).digest(), "big") / (1 << 256)

    def _act_if_pending(self) -> str | None:
        decision_id = self.session.pending_decision_id
        state = self.session.current
        if decision_id is None:
            return None
        if state is None or state.actor != 0:
            raise AssertionError("Common opened an A2 lease without a hero state")
        decision_number = self.decisions_completed + 1
        information_state_id = state.information_state_id(0)
        selected = choose_blueprint_action_from_common_state(
            self.blueprint,
            state=state,
            hero=0,
            random_unit=self._random_unit(information_state_id, decision_number),
        )
        action = selected.action.to_wire()
        legal_wires = tuple(
            candidate.to_wire()
            for candidate in selected.legal_actions.representative_actions()
        )
        selected.assert_fresh(self.session.current, hero=0)
        self.session.submit_action(decision_id, action)
        self.decisions_completed = decision_number
        self.trace.append(
            CommonDecisionTrace(
                hand_number=state.hand_number,
                decision_number=decision_number,
                decision_id=decision_id,
                information_state_id=information_state_id,
                action=action,
                legal_actions=legal_wires,
                lookup_key=selected.route_decision.lookup.matched_key,
                full_state_id=selected.full_state_id,
                used_legality_fallback=selected.route_decision.used_legality_fallback,
                available_policy_mass=selected.route_decision.available_policy_mass,
                dropped_policy_mass=selected.route_decision.dropped_policy_mass,
                legal_raise_bounds=(
                    selected.legal_actions.min_raise_to,
                    selected.legal_actions.max_raise_to,
                ),
            )
        )
        return action

    def on_token(self, token: str) -> tuple[ProtocolEvent, str | None]:
        """Consume one platform token and return at most one authorized send."""

        event = self.session.receive(token)
        if event.kind == "name_requested":
            return event, self.session.name_response()
        return event, self._act_if_pending()

    def feed(self, chunk: bytes | str) -> list[tuple[ProtocolEvent, str | None]]:
        return [self.on_token(token) for token in self.decoder.feed(chunk)]

    def flush_numeric(self) -> list[tuple[ProtocolEvent, str | None]]:
        return [self.on_token(token) for token in self.decoder.flush_numeric()]

    def trace_payload(self) -> list[dict[str, object]]:
        return [asdict(item) for item in self.trace]
