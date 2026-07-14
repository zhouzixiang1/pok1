"""Route-local full-state/action binding around Common's one-shot lease."""

from __future__ import annotations

from dataclasses import dataclass, field

from bots.research_native_lab.common_contracts import NationalGameState
from bots.research_native_lab.common_contracts.protocol import (
    NationalProtocolSession,
    ProtocolEvent,
    ProtocolStateError,
)

from .common_adapter import BoundNationalAction


@dataclass(slots=True)
class RouteDecisionLease:
    """Bind decision id, full state and exact Common action exactly once."""

    decision_id: int
    full_state_id: str
    bound_action: BoundNationalAction
    _consumed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.decision_id) is not int or self.decision_id <= 0:
            raise TypeError("route decision_id must be a positive exact integer")
        if type(self.full_state_id) is not str or len(self.full_state_id) != 64:
            raise TypeError("route full_state_id must be exact SHA-256 text")
        if type(self.bound_action) is not BoundNationalAction:
            raise TypeError("route lease requires exact BoundNationalAction")
        if self.bound_action.full_state_id != self.full_state_id:
            raise ValueError("route lease state/action bindings disagree")

    def consume(self, session: NationalProtocolSession) -> ProtocolEvent:
        if type(session) is not NationalProtocolSession:
            raise TypeError("route lease requires exact Common NationalProtocolSession")
        if self._consumed:
            raise ProtocolStateError("route decision lease was already consumed")
        pending = session.pending_decision_id
        if type(pending) is not int or pending != self.decision_id:
            raise ProtocolStateError("route decision id is stale or absent")
        current = session.current
        if type(current) is not NationalGameState:
            raise ProtocolStateError("route decision has no exact Common current state")
        if current.full_state_id() != self.full_state_id:
            raise ProtocolStateError("route decision full state is stale")
        expected_next = self.bound_action.apply_to(current_state=current)
        event = session.submit_action(
            self.decision_id,
            self.bound_action.wire_action,
        )
        actual_next = session.current
        if type(actual_next) is not NationalGameState:
            raise ProtocolStateError("Common submit lost the current state")
        if actual_next.full_state_id() != expected_next.full_state_id():
            raise ProtocolStateError("Common submit differs from bound action transition")
        self._consumed = True
        return event
