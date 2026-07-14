"""Raw-stream decoding and state reconstruction for the official TCP protocol."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

from .actions import Action
from .cards import CARD_RE, parse_cards_exact
from .constants import HANDS_PER_MATCH
from .national_state import ActionRecord, NationalGameState, StateInvariantError, Street


class ProtocolDecodeError(ValueError):
    pass


class ProtocolStateError(RuntimeError):
    pass


_EARN_RE = re.compile(r"earnChips (-?[0-9]+)")
_RAISE_RE = re.compile(r"raise ([0-9]+)")
_PREFLOP_RE = re.compile(r"preflop\|(SMALLBLIND|BIGBLIND)\|(.+)")
_CARD_SPECS = (
    ("preflop|SMALLBLIND|", 2),
    ("preflop|BIGBLIND|", 2),
    ("oppo_hands|", 2),
    ("flop|", 3),
    ("turn|", 1),
    ("river|", 1),
)
_FIXED_TOKENS = ("allin", "check", "call", "fold", "name")
_KNOWN_PREFIXES = tuple(prefix for prefix, _ in _CARD_SPECS) + (
    "earnChips ",
    "raise ",
) + _FIXED_TOKENS


def _take_card_token(buffer: str, prefix: str, count: int) -> tuple[str, str] | None:
    if not buffer.startswith(prefix):
        return None
    position = len(prefix)
    for _ in range(count):
        match = CARD_RE.match(buffer, position)
        if match is None:
            return None
        position = match.end()
    return buffer[:position], buffer[position:]


def _could_be_prefix(buffer: str) -> bool:
    return any(prefix.startswith(buffer) for prefix in _KNOWN_PREFIXES)


def _take_token(buffer: str, *, flush_numeric: bool) -> tuple[str, str] | None:
    buffer = buffer.lstrip("\r\n\t ")
    if not buffer:
        return None

    for prefix, count in _CARD_SPECS:
        parsed = _take_card_token(buffer, prefix, count)
        if parsed is not None:
            return parsed

    for regex in (_EARN_RE, _RAISE_RE):
        match = regex.match(buffer)
        if match is not None:
            if match.end() == len(buffer) and not flush_numeric:
                return None
            return buffer[: match.end()], buffer[match.end() :]

    for token in _FIXED_TOKENS:
        if buffer.startswith(token):
            return token, buffer[len(token) :]
    if _could_be_prefix(buffer):
        return None
    return None


class StreamDecoder:
    """Incrementally split an unframed TCP byte stream.

    Numeric messages are deliberately held when their digits end exactly at a
    recv boundary: ``raise 2`` might become ``raise 200`` in the next packet.
    The socket owner calls :meth:`flush_numeric` after a short read-quiet
    boundary.  Fixed words and card messages are self-delimiting.
    """

    def __init__(self, *, max_buffer_bytes: int = 64 * 1024):
        self._buffer = ""
        self._max_buffer_bytes = max_buffer_bytes

    @property
    def buffered(self) -> str:
        return self._buffer

    def feed(self, chunk: bytes | str) -> list[str]:
        if isinstance(chunk, bytes):
            try:
                text = chunk.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ProtocolDecodeError("national wire data must be ASCII") from exc
        else:
            text = chunk
        self._buffer += text
        return self._drain(flush_numeric=False, strict=False)

    def flush_numeric(self) -> list[str]:
        """Flush a trailing numeric token after the socket read-quiet guard."""

        # Read-quiet is only a heuristic boundary for an otherwise complete
        # numeric token.  It must never turn a partial fixed/card prefix into a
        # protocol error; EOF is the only strict incomplete-frame boundary.
        return self._drain(flush_numeric=True, strict=False)

    def finish(self) -> list[str]:
        """Flush at EOF and reject incomplete or unknown trailing bytes."""

        return self._drain(flush_numeric=True, strict=True)

    def _drain(self, *, flush_numeric: bool, strict: bool) -> list[str]:
        messages: list[str] = []
        while self._buffer:
            stripped = self._buffer.lstrip("\r\n\t ")
            if not stripped:
                self._buffer = ""
                break
            self._buffer = stripped
            parsed = _take_token(self._buffer, flush_numeric=flush_numeric)
            if parsed is None:
                if strict:
                    if _could_be_prefix(self._buffer) and not flush_numeric:
                        break
                    raise ProtocolDecodeError(
                        f"incomplete or unknown national wire bytes: {self._buffer!r}"
                    )
                if len(self._buffer.encode("utf-8")) > self._max_buffer_bytes:
                    raise ProtocolDecodeError("national wire buffer exceeded safety limit")
                break
            message, self._buffer = parsed
            messages.append(message)
        return messages


@dataclass(frozen=True, slots=True)
class ProtocolEvent:
    kind: str
    hand_number: int | None = None
    payload: dict[str, Any] | None = None


class NationalProtocolSession:
    """Reconstruct public state and enforce one-shot, owner-thread decisions.

    Player 0 is always this bot and player 1 is the opponent.  Blind order is
    carried separately by :class:`NationalGameState`.  Strategy workers may
    compute snapshots, but only the thread that created this session can accept
    wire input or submit the final action.
    """

    def __init__(self, bot_name: str):
        if not bot_name or any(character in bot_name for character in "\r\n"):
            raise ValueError("bot_name must be non-empty and line-free")
        self.bot_name = bot_name
        self._owner_thread = threading.get_ident()
        self.current: NationalGameState | None = None
        self.hands_started = 0
        self.settlements_received = 0
        self.cumulative_net_hero = 0
        self.current_earn: int | None = None
        self.current_showdown = False
        self._pending_earn: int | None = None
        self._last_hero_small_blind: bool | None = None
        self._decision_serial = 0
        self._pending_decision_id: int | None = None
        self._name_request_pending = False
        self._name_response_sent = False

    def _assert_owner(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise ProtocolStateError("only the socket-owner thread may mutate the session")

    @property
    def pending_decision_id(self) -> int | None:
        return self._pending_decision_id

    @property
    def has_pending_decision(self) -> bool:
        return self._pending_decision_id is not None

    def name_response(self) -> str:
        self._assert_owner()
        if not self._name_request_pending or self._name_response_sent:
            raise ProtocolStateError("name response is stale or unsolicited")
        self._name_request_pending = False
        self._name_response_sent = True
        return self.bot_name

    def _refresh_pending(self) -> int | None:
        state = self.current
        should_open = (
            state is not None
            and not state.is_terminal
            and not state.chance_pending
            and state.actor == 0
            and self.current_earn is None
        )
        if should_open:
            self._decision_serial += 1
            self._pending_decision_id = self._decision_serial
        else:
            self._pending_decision_id = None
        return self._pending_decision_id

    def receive(self, token: str) -> ProtocolEvent:
        self._assert_owner()
        if token == "name":
            if (
                self._name_request_pending
                or self._name_response_sent
                or self.current is not None
                or self.hands_started != 0
            ):
                raise ProtocolStateError("duplicate or out-of-order name request")
            self._name_request_pending = True
            return ProtocolEvent("name_requested")
        if self._name_request_pending:
            raise ProtocolStateError("platform state arrived before the name response")
        if not self._name_response_sent:
            raise ProtocolStateError("platform state arrived before the name handshake")
        if token.startswith("preflop|"):
            return self._receive_preflop(token)
        if token.startswith(("flop|", "turn|", "river|")):
            return self._receive_stage(token)
        if token.startswith("earnChips "):
            return self._receive_earn(token)
        if token.startswith("oppo_hands|"):
            return self._receive_showdown(token)
        try:
            action = Action.from_wire(token)
        except ValueError as exc:
            raise ProtocolStateError(f"unknown server token {token!r}") from exc
        if self.current is None:
            raise ProtocolStateError("opponent action arrived before a hand")
        if self.current.actor != 1 or self._pending_decision_id is not None:
            raise ProtocolStateError("opponent action arrived outside the opponent turn")
        self.current = self.current.apply_action(action)
        decision = self._refresh_pending()
        return ProtocolEvent(
            "opponent_action",
            self.current.hand_number,
            {"action": action.to_wire(), "decision_id": decision},
        )

    def _receive_preflop(self, token: str) -> ProtocolEvent:
        match = _PREFLOP_RE.fullmatch(token)
        if match is None:
            raise ProtocolStateError(f"malformed preflop token {token!r}")
        if self._pending_decision_id is not None:
            raise ProtocolStateError("new hand arrived while an action was pending")
        if self.hands_started >= HANDS_PER_MATCH:
            raise ProtocolStateError("platform started more than 70 hands")
        if (
            self.current is not None
            and (
                self.current_earn is None
                or not self.current.is_terminal
                or (
                    self.current.terminal_reason == "showdown"
                    and not self.current_showdown
                )
            )
        ):
            raise ProtocolStateError("new hand arrived before prior terminal evidence was complete")

        blind, raw_cards = match.groups()
        cards = parse_cards_exact(raw_cards, expected=2)
        hero_small_blind = blind == "SMALLBLIND"
        if (
            self._last_hero_small_blind is not None
            and hero_small_blind == self._last_hero_small_blind
        ):
            raise ProtocolStateError("blind role did not alternate between consecutive hands")
        self.hands_started += 1
        small_blind = 0 if hero_small_blind else 1
        self.current = NationalGameState.new_hand(
            self.hands_started,
            small_blind=small_blind,
            hole_cards=(cards, ()),
            match_net_before=(self.cumulative_net_hero, -self.cumulative_net_hero),
        )
        self.current_earn = None
        self.current_showdown = False
        self._pending_earn = None
        self._last_hero_small_blind = hero_small_blind
        decision = self._refresh_pending()
        return ProtocolEvent(
            "hand_started",
            self.hands_started,
            {"blind": blind, "hero_cards": list(cards), "decision_id": decision},
        )

    def _receive_stage(self, token: str) -> ProtocolEvent:
        if self.current is None:
            raise ProtocolStateError("stage cards arrived before a hand")
        prefix, raw_cards = token.split("|", 1)
        expected = 3 if prefix == "flop" else 1
        cards = parse_cards_exact(raw_cards, expected=expected)
        state = self.current
        inferred: ActionRecord | None = None
        if not state.chance_pending:
            if state.actor != 1:
                raise ProtocolStateError("stage boundary cannot infer this bot's own action")
            try:
                state, inferred = state.infer_omitted_closing_action()
            except StateInvariantError as exc:
                raise ProtocolStateError("stage arrived before a provable street close") from exc
        try:
            state = state.apply_chance(cards)
        except StateInvariantError as exc:
            raise ProtocolStateError(f"invalid {prefix} transition") from exc
        if state.street.value != prefix:
            raise ProtocolStateError(
                f"out-of-order public cards: token={prefix}, state={state.street.value}"
            )
        self.current = state
        decision = self._refresh_pending()
        payload: dict[str, Any] = {"cards": list(cards), "decision_id": decision}
        if inferred is not None:
            payload["inferred_closing_action"] = inferred.action.to_wire()
        return ProtocolEvent("stage", state.hand_number, payload)

    def _receive_earn(self, token: str) -> ProtocolEvent:
        match = _EARN_RE.fullmatch(token)
        if match is None:
            raise ProtocolStateError(f"malformed earnChips token {token!r}")
        if self.current is None:
            raise ProtocolStateError("settlement arrived before a hand")
        if self.current_earn is not None or self._pending_earn is not None:
            raise ProtocolStateError("duplicate settlement for this seat and hand")
        if self._pending_decision_id is not None:
            raise ProtocolStateError("settlement arrived while this bot still owed an action")
        amount = int(match.group(1))
        if not -20_000 <= amount <= 20_000:
            raise ProtocolStateError("per-hand earnChips lies outside the legal stack range")
        if not self.current.is_terminal:
            # A bare settlement never proves whether the peer folded or called.
            # The only accepted deferred shape is a river peer close that a
            # following oppo_hands token can prove was the unique call/check.
            if self.current.street is not Street.RIVER or self.current.actor != 1:
                raise ProtocolStateError("settlement arrived before a terminal state")
            try:
                candidate, _ = self.current.infer_omitted_closing_action()
            except StateInvariantError as exc:
                raise ProtocolStateError("settlement has no provable terminal continuation") from exc
            if candidate.terminal_reason != "showdown":
                raise ProtocolStateError("deferred settlement is not a possible showdown close")
            self._pending_earn = amount
            return ProtocolEvent(
                "settlement_pending_showdown_proof",
                self.current.hand_number,
                {"hero_net": amount},
            )
        self._finalize_earn(amount)
        return ProtocolEvent(
            "settlement",
            self.current.hand_number,
            {"hero_net": amount, "cumulative_net": self.cumulative_net_hero},
        )

    def _finalize_earn(self, amount: int) -> None:
        assert self.current is not None and self.current.is_terminal
        if self.current.terminal_reason == "fold":
            expected = self.current.terminal_utility()[0]
            if amount != expected:
                raise ProtocolStateError(
                    f"wire settlement {amount} disagrees with fold utility {expected}"
                )
        elif self.current.terminal_reason == "showdown" and self.current_showdown:
            expected = self.current.terminal_utility()[0]
            if amount != expected:
                raise ProtocolStateError(
                    f"wire settlement {amount} disagrees with showdown utility {expected}"
                )
        self.current_earn = amount
        self._pending_earn = None
        self.cumulative_net_hero += amount
        self.settlements_received += 1
        self._pending_decision_id = None

    def _receive_showdown(self, token: str) -> ProtocolEvent:
        if self.current is None:
            raise ProtocolStateError("opponent cards arrived before a hand")
        if self.current_showdown:
            raise ProtocolStateError("duplicate opponent showdown disclosure")
        cards = parse_cards_exact(token.split("|", 1)[1], expected=2)
        state = self.current
        inferred: ActionRecord | None = None
        if not state.is_terminal:
            if state.street is not Street.RIVER or state.actor != 1:
                raise ProtocolStateError("opponent cards do not prove the missing state transition")
            state, inferred = state.infer_omitted_closing_action()
        if state.terminal_reason != "showdown":
            raise ProtocolStateError("opponent cards received for a non-showdown hand")
        state = state.with_hole_cards(1, cards)
        self.current = state
        self.current_showdown = True
        utility = state.terminal_utility()
        if self.current_earn is not None and utility[0] != self.current_earn:
            raise ProtocolStateError(
                f"wire settlement {self.current_earn} disagrees with showdown utility {utility[0]}"
            )
        if self._pending_earn is not None:
            self._finalize_earn(self._pending_earn)
        payload: dict[str, Any] = {
            "opponent_cards": list(cards),
            "terminal_utility": list(utility),
        }
        if inferred is not None:
            payload["inferred_closing_action"] = inferred.action.to_wire()
        return ProtocolEvent("showdown", state.hand_number, payload)

    def submit_action(self, decision_id: int, wire_action: str) -> ProtocolEvent:
        """Consume the current one-shot decision lease on the owner thread."""

        self._assert_owner()
        if type(decision_id) is not int:
            raise ProtocolStateError("decision id must be an exact integer")
        if decision_id != self._pending_decision_id:
            raise ProtocolStateError("stale, duplicate or unsolicited action")
        if self.current is None or self.current.actor != 0:
            raise ProtocolStateError("this bot is not the pending actor")
        action = Action.from_wire(wire_action)
        self.current = self.current.apply_action(action)
        self._pending_decision_id = None
        return ProtocolEvent(
            "hero_action",
            self.current.hand_number,
            {"action": action.to_wire()},
        )

    def connection_close_evidence(self) -> dict[str, Any]:
        """Describe completion without inventing the official hand-70 settlement."""

        self._assert_owner()
        if self._pending_decision_id is not None:
            raise ProtocolStateError("connection closed with a pending decision")
        terminal_wire_state = (
            self.current is not None
            and self.current.is_terminal
            and (
                self.current.terminal_reason != "showdown"
                or self.current_showdown
            )
        )
        natural_70_boundary = self.hands_started == HANDS_PER_MATCH and terminal_wire_state
        official_69_settlement_shape = natural_70_boundary and self.settlements_received == 69
        return {
            "hands_started": self.hands_started,
            "wire_settlements": self.settlements_received,
            "natural_70_boundary": natural_70_boundary,
            "hand_70_terminal_wire_state": terminal_wire_state,
            "requires_thp_state_69": official_69_settlement_shape,
            "wire_alone_proves_complete": natural_70_boundary and self.settlements_received == 70,
        }
