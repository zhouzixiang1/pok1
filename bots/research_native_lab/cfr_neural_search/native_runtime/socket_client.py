"""Single-owner native TCP client for the Route-B sparse blueprint."""

from __future__ import annotations

import math
import socket
import time
from dataclasses import asdict, dataclass
from typing import Any

from bots.research_native_lab.common_contracts import NationalGameState
from bots.research_native_lab.common_contracts.protocol import (
    NationalProtocolSession,
    ProtocolEvent,
    StreamDecoder,
)

from ..blueprint.artifact import BlueprintPolicy
from .common_adapter import adapt_national_decision
from .decision_lease import RouteDecisionLease


WIRE_MODES = frozenset({"official-raw", "local-sever-lf"})


@dataclass(frozen=True, slots=True)
class NativeClientResult:
    bot_name: str
    wire_mode: str
    action_delay_sec: float
    policy_seed: int
    decisions: int
    exact_hits: int
    backoff_hits: int
    uniform_emergency: int
    materially_nonuniform_decisions: int
    hands_started: int
    settlements_received: int
    cumulative_net_hero: int
    completion_authority: str
    requires_external_thp: bool
    wire_complete: bool
    close_evidence: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class NativeBlueprintClient:
    """Own the socket, decoder, Common session, lease and final send."""

    def __init__(
        self,
        *,
        bot_name: str,
        policy: BlueprintPolicy,
        policy_seed: int,
        wire_mode: str,
        action_delay_sec: float,
        read_quiet_sec: float = 0.010,
    ) -> None:
        if type(bot_name) is not str or not bot_name or not bot_name.isascii():
            raise ValueError("native bot name must be nonempty ASCII")
        if type(policy) is not BlueprintPolicy:
            raise TypeError("native client requires exact BlueprintPolicy")
        if type(policy_seed) is not int:
            raise TypeError("policy_seed must be an exact integer")
        if wire_mode not in WIRE_MODES:
            raise ValueError("unknown native wire mode")
        if type(action_delay_sec) not in (int, float) or not math.isfinite(
            float(action_delay_sec)
        ) or action_delay_sec < 0:
            raise ValueError("action delay must be finite and nonnegative")
        if type(read_quiet_sec) not in (int, float) or not 0 < read_quiet_sec <= 1:
            raise ValueError("read quiet interval must be in (0,1]")
        self.bot_name = bot_name
        self.policy = policy
        self.policy_seed = policy_seed
        self.wire_mode = wire_mode
        self.action_delay_sec = float(action_delay_sec)
        self.read_quiet_sec = float(read_quiet_sec)
        self.session = NationalProtocolSession(bot_name)
        self.decoder = StreamDecoder()
        self.decision_counter = 0
        self._socket: socket.socket | None = None

    def _send(self, text: str) -> None:
        connection = self._socket
        if connection is None:
            raise RuntimeError("native socket is not connected")
        suffix = "\n" if self.wire_mode == "local-sever-lf" else ""
        connection.sendall((text + suffix).encode("ascii"))

    def _act_if_pending(self, enabling_received_at: float) -> None:
        decision_id = self.session.pending_decision_id
        if decision_id is None:
            return
        if type(decision_id) is not int:
            raise TypeError("Common decision id must remain an exact integer")
        current = self.session.current
        if type(current) is not NationalGameState:
            raise RuntimeError("pending Common decision lacks exact state")
        snapshot = adapt_national_decision(current, controlled_player=0)
        decision = self.policy.decide(
            snapshot.state,
            0,
            policy_seed=self.policy_seed,
            decision_counter=self.decision_counter,
        )
        bound = snapshot.bind(decision.action, current_state=current)
        lease = RouteDecisionLease(decision_id, snapshot.full_state_id, bound)
        remaining_delay = self.action_delay_sec - (
            time.monotonic() - enabling_received_at
        )
        if remaining_delay > 0:
            time.sleep(remaining_delay)
        lease.consume(self.session)
        self._send(bound.wire_action)
        self.decision_counter += 1

    def _handle_token(self, token: str, received_at: float) -> ProtocolEvent:
        event = self.session.receive(token)
        if event.kind == "name_requested":
            self._send(self.session.name_response())
        else:
            self._act_if_pending(received_at)
        return event

    def run(
        self,
        host: str,
        port: int,
        *,
        connect_timeout_sec: float = 10.0,
        match_timeout_sec: float = 180.0,
    ) -> NativeClientResult:
        if type(host) is not str or not host:
            raise ValueError("host must be a nonempty string")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an exact integer in 1..65535")
        if type(match_timeout_sec) not in (int, float) or match_timeout_sec <= 0:
            raise ValueError("match timeout must be positive")
        deadline = time.monotonic() + float(match_timeout_sec)
        with socket.create_connection((host, port), timeout=connect_timeout_sec) as connection:
            self._socket = connection
            connection.settimeout(self.read_quiet_sec)
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("native match exceeded client wall timeout")
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    received_at = time.monotonic()
                    for token in self.decoder.flush_numeric():
                        self._handle_token(token, received_at)
                    continue
                received_at = time.monotonic()
                if not chunk:
                    for token in self.decoder.finish():
                        self._handle_token(token, received_at)
                    break
                for token in self.decoder.feed(chunk):
                    self._handle_token(token, received_at)
        self._socket = None
        close_evidence = self.session.connection_close_evidence()
        if self.wire_mode == "local-sever-lf":
            if not close_evidence["wire_alone_proves_complete"]:
                raise RuntimeError("local sever EOF lacks all 70 wire settlements")
            completion_authority = "diagnostic_local_wire_complete"
        else:
            if not close_evidence["natural_70_boundary"]:
                raise RuntimeError("official raw EOF lacks a terminal hand-70 boundary")
            if close_evidence["requires_thp_state_69"]:
                completion_authority = "official_terminal_requires_external_thp"
            elif close_evidence["wire_alone_proves_complete"]:
                completion_authority = "wire_complete_not_officially_certified"
            else:
                raise RuntimeError("official raw EOF has an unsupported settlement shape")
        counters = self.policy.counters
        return NativeClientResult(
            bot_name=self.bot_name,
            wire_mode=self.wire_mode,
            action_delay_sec=self.action_delay_sec,
            policy_seed=self.policy_seed,
            decisions=self.decision_counter,
            exact_hits=counters.exact_hits,
            backoff_hits=counters.backoff_hits,
            uniform_emergency=counters.uniform_emergency,
            materially_nonuniform_decisions=counters.materially_nonuniform_decisions,
            hands_started=self.session.hands_started,
            settlements_received=self.session.settlements_received,
            cumulative_net_hero=self.session.cumulative_net_hero,
            completion_authority=completion_authority,
            requires_external_thp=bool(close_evidence["requires_thp_state_69"]),
            wire_complete=bool(close_evidence["wire_alone_proves_complete"]),
            close_evidence=close_evidence,
        )
