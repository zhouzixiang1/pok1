"""Raw TCP probe and replay diagnostics for the official national platform.

The normal official-platform harness relies on bot-side logs. That is enough
for pass/fail smoke, but not enough to diagnose official EXE failures where the
platform reports an illegal action or a 60 second no-response path. This module
records the bytes between the EXE and each bot, parses the national wire tokens,
and replays enough betting state to classify protocol problems.
"""

from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
import time
from pathlib import Path
from typing import Any

from sever.server.protocol import (
    take_client_action as _official_take_client_action,
    take_server_message as _official_take_server_message,
)


SERVER_ACTION_RE = re.compile(r"^(raise) ([0-9]+)$")
CLIENT_RAISE_RE = re.compile(r"^raise [1-9]\d*")
EARN_RE = re.compile(r"^earnChips (-?[0-9]+)$")
CARD_RE = re.compile(r"<(\d+),(\d+)>")
SMALL_BLIND = 50
BIG_BLIND = 100
INITIAL_CHIPS = 20000


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def take_server_message(buffer: str, *, flush_numeric: bool = True) -> tuple[str | None, str]:
    """Take one official server-to-client message from a raw stream buffer."""
    return _official_take_server_message(
        buffer,
        flush_boundary=flush_numeric,
    )


def split_server_messages(buffer: str, *, flush_numeric: bool = True) -> tuple[list[str], str]:
    messages: list[str] = []
    while buffer:
        msg, rest = take_server_message(buffer, flush_numeric=flush_numeric)
        if msg is None:
            return messages, rest
        messages.append(msg)
        buffer = rest
    return messages, ""


def take_client_message(
    buffer: str,
    *,
    allow_name: bool = False,
    flush_numeric: bool = True,
) -> tuple[str | None, str]:
    """Take one bot-to-server message.

    Bot actions are intentionally stricter than server action parsing: the
    official EXE rejects leading/trailing whitespace, tabs, and ``raise  200``.
    """
    if not buffer:
        return None, ""
    if allow_name:
        # Team names have no lexical terminator. Commit only at the proxy's
        # idle/EOF boundary and preserve Unicode/whitespace exactly so the
        # replay cannot turn a malformed handshake into a valid one.
        return (buffer, "") if flush_numeric else (None, buffer)
    message, remainder = _official_take_client_action(
        buffer,
        flush_boundary=flush_numeric,
    )
    if message is None and flush_numeric:
        return buffer, ""
    return message, remainder


def split_client_messages(
    buffer: str,
    *,
    allow_name: bool = False,
    flush_numeric: bool = True,
) -> tuple[list[str], str]:
    messages: list[str] = []
    while buffer:
        msg, rest = take_client_message(
            buffer,
            allow_name=allow_name,
            flush_numeric=flush_numeric,
        )
        if msg is None:
            return messages, rest
        messages.append(msg)
        buffer = rest
        allow_name = False
    return messages, ""


def classify_client_action(message: str) -> tuple[str, int | None, str | None]:
    if message in {"call", "check", "fold", "allin"}:
        return message, None, None
    if CLIENT_RAISE_RE.fullmatch(message):
        return "raise", int(message.split(" ", 1)[1]), None
    if message.startswith("bet"):
        return "unknown", None, "wire_bet_token"
    if message.strip() != message:
        return "unknown", None, "wire_action_whitespace"
    if message.startswith("raise"):
        return "unknown", None, "wire_raise_format"
    return "unknown", None, "wire_action_format"


def classify_server_action(message: str) -> tuple[str, int | None] | None:
    if message in {"call", "check", "fold", "allin"}:
        return message, None
    match = SERVER_ACTION_RE.fullmatch(message)
    if match:
        return "raise", int(match.group(2))
    return None


def _parse_protocol_cards(payload: str, *, expected: int) -> tuple[list[tuple[int, int]], str | None]:
    """Parse one exact national card payload without normalizing bad bytes."""
    matches = list(CARD_RE.finditer(payload))
    if len(matches) != expected or "".join(match.group(0) for match in matches) != payload:
        return [], f"expected exactly {expected} contiguous <suit,rank> cards"
    cards = [(int(match.group(1)), int(match.group(2))) for match in matches]
    if any(not (0 <= suit <= 3 and 0 <= rank <= 12) for suit, rank in cards):
        return [], "card suit/rank is outside the national 52-card encoding"
    if len(set(cards)) != len(cards):
        return [], "card payload contains a duplicate card"
    return cards, None


@dataclass
class SeatReplay:
    label: str
    name: str = ""
    awaiting_name: bool = False
    stage: str = ""
    is_small_blind: bool = False
    hand_num: int = 0
    hands_started: int = 0
    settlements: int = 0
    settlement_records: list[dict[str, int]] = field(default_factory=list)
    player_chips: int = INITIAL_CHIPS
    opponent_chips: int = INITIAL_CHIPS
    player_bet: int = 0
    opponent_bet: int = 0
    pot: int = 0
    player_action_count: int = 0
    actions: list[tuple[str, int | None]] = field(default_factory=list)
    action_actors: list[str] = field(default_factory=list)
    hand_actions: list[dict[str, Any]] = field(default_factory=list)
    hole_cards: list[tuple[int, int]] = field(default_factory=list)
    public_cards: list[tuple[int, int]] = field(default_factory=list)
    blind_by_hand: dict[int, str] = field(default_factory=dict)
    hole_cards_by_hand: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    public_cards_by_hand: dict[
        int,
        dict[str, list[tuple[int, int]]],
    ] = field(default_factory=dict)
    opponent_cards_by_hand: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    allin_occurred: bool = False
    fold_occurred: bool = False
    expected_since: float | None = None
    expected_reason: str = ""
    max_response_sec: float = 0.0
    response_samples: list[float] = field(default_factory=list)

    def expect(self, now: float, reason: str) -> None:
        if self.expected_since is None:
            self.expected_since = now
            self.expected_reason = reason

    def clear_expectation(self, now: float) -> None:
        if self.expected_since is not None:
            elapsed = max(0.0, now - self.expected_since)
            self.max_response_sec = max(self.max_response_sec, elapsed)
            self.response_samples.append(elapsed)
        self.expected_since = None
        self.expected_reason = ""

    def reset_street(self, stage: str) -> None:
        self.stage = stage
        self.player_bet = 0
        self.opponent_bet = 0
        self.player_action_count = 0
        self.actions = []
        self.action_actors = []
        self.expected_since = None
        self.expected_reason = ""

    def start_hand(self, blind: str, cards: list[tuple[int, int]]) -> None:
        self.stage = "preflop"
        self.hand_num += 1
        self.hands_started += 1
        self.is_small_blind = blind == "SMALLBLIND"
        self.player_chips = INITIAL_CHIPS
        self.opponent_chips = INITIAL_CHIPS
        self.pot = SMALL_BLIND + BIG_BLIND
        self.hole_cards = list(cards)
        self.public_cards = []
        self.blind_by_hand[self.hand_num] = blind
        self.hole_cards_by_hand[self.hand_num] = list(cards)
        self.public_cards_by_hand[self.hand_num] = {}
        if self.is_small_blind:
            self.player_chips -= SMALL_BLIND
            self.opponent_chips -= BIG_BLIND
            self.player_bet = SMALL_BLIND
            self.opponent_bet = BIG_BLIND
        else:
            self.player_chips -= BIG_BLIND
            self.opponent_chips -= SMALL_BLIND
            self.player_bet = BIG_BLIND
            self.opponent_bet = SMALL_BLIND
        self.player_action_count = 0
        self.actions = []
        self.action_actors = []
        self.hand_actions = []
        self.allin_occurred = False
        self.fold_occurred = False
        self.expected_since = None
        self.expected_reason = ""


class OfficialWireReplay:
    """Replay parsed wire events from a bot's point of view."""

    def __init__(self, *, response_warn_sec: float = 55.0, response_timeout_sec: float = 60.0):
        self.response_warn_sec = float(response_warn_sec)
        self.response_timeout_sec = float(response_timeout_sec)
        self.seats: dict[str, SeatReplay] = {}
        self.issues: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.events_seen = 0
        self.max_platform_silent_gap_sec = 0.0
        self._last_event_t: float | None = None
        self._last_event_brief: dict[str, Any] | None = None
        self._card_integrity_issue_keys: set[tuple[Any, ...]] = set()
        self._blind_integrity_issue_keys: set[tuple[Any, ...]] = set()

    def _seat(self, label: str) -> SeatReplay:
        if label not in self.seats:
            self.seats[label] = SeatReplay(label=label)
        return self.seats[label]

    def consume_event(self, event: dict[str, Any]) -> None:
        self.events_seen += 1
        raw_t = event.get("t")
        t = time.time() if raw_t is None else float(raw_t)
        if self._last_event_t is not None:
            gap = t - self._last_event_t
            self.max_platform_silent_gap_sec = max(self.max_platform_silent_gap_sec, gap)
            if gap >= self.response_timeout_sec:
                pending = [seat for seat in self.seats.values() if seat.expected_since is not None]
                if pending:
                    for seat in pending:
                        self.issues.append({
                            "kind": "pending_bot_response_timeout",
                            "conn": seat.label,
                            "hand": seat.hand_num,
                            "stage": seat.stage,
                            "message": "",
                            "dt": event.get("dt"),
                            "waited_sec": round(gap, 3),
                            "expected_reason": seat.expected_reason,
                            "previous_event": self._last_event_brief,
                            "next_event": _event_brief(event),
                        })
                else:
                    self.issues.append({
                        "kind": "platform_silent_idle_gap",
                        "conn": str(event.get("conn") or "?"),
                        "hand": None,
                        "stage": None,
                        "message": "",
                        "dt": event.get("dt"),
                        "waited_sec": round(gap, 3),
                        "previous_event": self._last_event_brief,
                        "next_event": _event_brief(event),
                        "reason": "official EXE produced no wire traffic while no bot action was pending",
                    })
            elif gap >= self.response_warn_sec:
                self.warnings.append({
                    "kind": "platform_silent_slow_gap",
                    "conn": str(event.get("conn") or "?"),
                    "hand": None,
                    "stage": None,
                    "message": "",
                    "dt": event.get("dt"),
                    "waited_sec": round(gap, 3),
                    "previous_event": self._last_event_brief,
                    "next_event": _event_brief(event),
                    "reason": "official EXE produced no wire traffic for an unusually long interval",
                })
        self._last_event_t = t
        self._last_event_brief = _event_brief(event)
        label = str(event.get("conn") or "?")
        direction = str(event.get("direction") or "")
        event_type = str(event.get("event_type") or "data")
        remaining = str(event.get("remaining") or "")
        if event_type == "upstream_connect_failed":
            self.issues.append({
                "kind": "wire_probe_upstream_connect_failed",
                "conn": label,
                "direction": direction,
                "details": event.get("details") or {},
            })
        elif event_type == "stream_error":
            self.issues.append({
                "kind": "wire_stream_error",
                "conn": label,
                "direction": direction,
                "remaining": remaining,
                "details": event.get("details") or {},
            })
        elif event_type == "stream_encoding_error":
            self.issues.append({
                "kind": "wire_stream_encoding_error",
                "conn": label,
                "direction": direction,
                "remaining": remaining,
                "details": event.get("details") or {},
            })
        elif event_type == "stream_eof" and remaining:
            self.issues.append({
                "kind": "wire_stream_eof_remainder",
                "conn": label,
                "direction": direction,
                "remaining": remaining,
                "reason": "TCP stream closed with an unparseable protocol tail",
            })
        for message in event.get("messages") or []:
            if direction == "server_to_bot":
                self._consume_server(label, str(message), t, event)
            elif direction == "bot_to_server":
                self._consume_client(label, str(message), t, event)

    def _add_issue(self, kind: str, seat: SeatReplay, message: str, event: dict[str, Any], **extra: Any) -> None:
        payload = {
            "kind": kind,
            "conn": seat.label,
            "hand": seat.hand_num,
            "stage": seat.stage,
            "message": message,
            "dt": event.get("dt"),
            **extra,
        }
        self.issues.append(payload)

    def _add_warning(self, kind: str, seat: SeatReplay, message: str, event: dict[str, Any], **extra: Any) -> None:
        self.warnings.append({
            "kind": kind,
            "conn": seat.label,
            "hand": seat.hand_num,
            "stage": seat.stage,
            "message": message,
            "dt": event.get("dt"),
            **extra,
        })

    def _consume_server(self, label: str, message: str, t: float, event: dict[str, Any]) -> None:
        seat = self._seat(label)
        if message == "name":
            seat.awaiting_name = True
            seat.expect(t, "name_handshake")
            return
        if message.startswith("preflop|"):
            self._infer_omitted_closer(seat, "hand_start")
            parts = message.split("|", 2)
            blind = parts[1] if len(parts) > 1 else ""
            payload = parts[2] if len(parts) > 2 else ""
            cards, card_issue = _parse_protocol_cards(payload, expected=2)
            if blind not in {"SMALLBLIND", "BIGBLIND"}:
                self._add_issue("preflop_blind_invalid", seat, message, event)
            if card_issue:
                self._add_issue(
                    "preflop_cards_invalid",
                    seat,
                    message,
                    event,
                    reason=card_issue,
                )
            seat.start_hand(blind, cards)
            self._validate_cross_seat_blinds(
                seat.hand_num,
                seat,
                message,
                event,
            )
            if not card_issue:
                self._validate_cross_seat_card_integrity(
                    seat.hand_num,
                    seat,
                    message,
                    event,
                )
            if seat.is_small_blind:
                seat.expect(t, "small_blind_preflop_open")
            return
        if message.startswith(("flop|", "turn|", "river|")):
            stage, payload = message.split("|", 1)
            self._infer_omitted_closer(seat, f"street:{stage}")
            expected_cards = 3 if stage == "flop" else 1
            cards, card_issue = _parse_protocol_cards(payload, expected=expected_cards)
            seat.reset_street(stage)
            if card_issue:
                self._add_issue(
                    "public_cards_invalid",
                    seat,
                    message,
                    event,
                    reason=card_issue,
                )
            else:
                if set(cards) & set(seat.hole_cards + seat.public_cards):
                    self._add_issue(
                        "public_cards_collision",
                        seat,
                        message,
                        event,
                        reason="public card duplicates a known hole/public card",
                    )
                self._record_cross_seat_public_cards(
                    seat,
                    stage,
                    cards,
                    message,
                    event,
                )
                seat.public_cards.extend(cards)
                self._validate_cross_seat_card_integrity(
                    seat.hand_num,
                    seat,
                    message,
                    event,
                )
            if not seat.is_small_blind and not seat.allin_occurred:
                seat.expect(t, f"{stage}_first_action")
            return
        if message.startswith("earnChips"):
            self._infer_omitted_closer(seat, "settlement")
            match = EARN_RE.fullmatch(message)
            if match is None:
                self._add_issue("settlement_format_invalid", seat, message, event)
                return
            amount = int(match.group(1))
            expected_hand = seat.settlements + 1
            if seat.hand_num != expected_hand:
                self._add_issue(
                    "settlement_hand_sequence",
                    seat,
                    message,
                    event,
                    expected_hand=expected_hand,
                )
            if any(item["hand"] == seat.hand_num for item in seat.settlement_records):
                self._add_issue("duplicate_settlement", seat, message, event)
            else:
                seat.settlement_records.append({"hand": seat.hand_num, "amount": amount})
                seat.settlements = len(seat.settlement_records)
            seat.expected_since = None
            seat.expected_reason = ""
            return
        if message.startswith("oppo_hands|"):
            self._infer_omitted_closer(seat, "showdown")
            self._consume_showdown(seat, message, event)
            return

        action = classify_server_action(message)
        if action is None:
            self._add_warning("unknown_server_message", seat, message, event)
            return
        action_type, amount = action
        self._apply_opponent_action(seat, action_type, amount)
        if self._opponent_action_requires_response(seat, action_type):
            seat.expect(t, f"respond_to_{action_type}")

    def _consume_client(self, label: str, message: str, t: float, event: dict[str, Any]) -> None:
        seat = self._seat(label)
        if seat.awaiting_name and message not in {"call", "check", "fold", "allin"} and not message.startswith("raise "):
            if not message or any(ord(char) < 0x20 or ord(char) == 0x7f for char in message):
                self._add_issue(
                    "wire_name_format",
                    seat,
                    message,
                    event,
                    reason="official raw team names must not contain framing/control characters",
                )
                return
            seat.name = message
            seat.awaiting_name = False
            seat.clear_expectation(t)
            return

        action_type, amount, format_issue = classify_client_action(message)
        if format_issue:
            self._add_issue(format_issue, seat, message, event)
            return
        if seat.expected_since is None:
            self._add_issue(
                "unsolicited_client_action",
                seat,
                message,
                event,
                reason="bot sent an action while replay had no pending platform request",
            )
            return
        if seat.expected_since is not None and t - seat.expected_since >= self.response_timeout_sec:
            self._add_issue(
                "bot_response_timeout",
                seat,
                message,
                event,
                waited_sec=round(t - seat.expected_since, 3),
                expected_reason=seat.expected_reason,
            )
        elif seat.expected_since is not None and t - seat.expected_since >= self.response_warn_sec:
            self._add_warning(
                "bot_response_slow",
                seat,
                message,
                event,
                waited_sec=round(t - seat.expected_since, 3),
                expected_reason=seat.expected_reason,
            )
        seat.clear_expectation(t)

        ok, reason = self._validate_action(seat, action_type, amount)
        if not ok:
            self._add_issue(f"illegal_{action_type}", seat, message, event, reason=reason)
        self._apply_player_action(seat, action_type, amount)

    def _add_unique_blind_issue(
        self,
        key: tuple[Any, ...],
        kind: str,
        seat: SeatReplay,
        message: str,
        event: dict[str, Any],
        **extra: Any,
    ) -> None:
        if key in self._blind_integrity_issue_keys:
            return
        self._blind_integrity_issue_keys.add(key)
        self._add_issue(kind, seat, message, event, **extra)

    def _validate_cross_seat_blinds(
        self,
        hand: int,
        seat: SeatReplay,
        message: str,
        event: dict[str, Any],
    ) -> None:
        """Bind complementary, alternating blind roles to each hand identity."""

        blind = seat.blind_by_hand.get(hand, "")
        previous = seat.blind_by_hand.get(hand - 1)
        if (
            hand > 1
            and previous in {"SMALLBLIND", "BIGBLIND"}
            and blind in {"SMALLBLIND", "BIGBLIND"}
            and previous == blind
        ):
            self._add_unique_blind_issue(
                ("blind_not_alternating", seat.label, hand),
                "blind_not_alternating",
                seat,
                message,
                event,
                previous_hand=hand - 1,
                previous_blind=previous,
                observed_blind=blind,
            )

        blind_rows = [
            (label, replay.blind_by_hand[hand])
            for label, replay in sorted(self.seats.items())
            if hand in replay.blind_by_hand
        ]
        if len(blind_rows) < 2:
            return
        observed = [role for _label, role in blind_rows]
        if len(blind_rows) == 2 and set(observed) == {
            "SMALLBLIND",
            "BIGBLIND",
        }:
            return
        labels = tuple(label for label, _role in blind_rows)
        self._add_unique_blind_issue(
            ("blind_cross_seat_mismatch", hand, *labels),
            "blind_cross_seat_mismatch",
            seat,
            message,
            event,
            blind_bindings={
                label: role for label, role in blind_rows
            },
            reason=(
                "each hand must bind exactly one SMALLBLIND and one BIGBLIND "
                "across the two official connections"
            ),
        )

    def _add_unique_card_issue(
        self,
        key: tuple[Any, ...],
        kind: str,
        seat: SeatReplay,
        message: str,
        event: dict[str, Any],
        **extra: Any,
    ) -> None:
        if key in self._card_integrity_issue_keys:
            return
        self._card_integrity_issue_keys.add(key)
        self._add_issue(kind, seat, message, event, **extra)

    def _record_cross_seat_public_cards(
        self,
        seat: SeatReplay,
        stage: str,
        cards: list[tuple[int, int]],
        message: str,
        event: dict[str, Any],
    ) -> None:
        """Bind each public-card payload to the same hand/street on both wires."""

        if seat.hand_num <= 0:
            return
        stages = seat.public_cards_by_hand.setdefault(seat.hand_num, {})
        previous = stages.get(stage)
        if previous is not None:
            if previous != cards:
                self._add_unique_card_issue(
                    (
                        "public_cards_same_seat_mismatch",
                        seat.hand_num,
                        seat.label,
                        stage,
                    ),
                    "public_cards_same_seat_mismatch",
                    seat,
                    message,
                    event,
                    board_stage=stage,
                    previous_cards=[list(card) for card in previous],
                    observed_cards=[list(card) for card in cards],
                )
            return
        stages[stage] = list(cards)

        for peer_label, peer in sorted(self.seats.items()):
            if peer_label == seat.label:
                continue
            peer_cards = (
                peer.public_cards_by_hand.get(seat.hand_num, {}).get(stage)
            )
            if peer_cards is None or peer_cards == cards:
                continue
            seat_pair = tuple(sorted((seat.label, peer_label)))
            self._add_unique_card_issue(
                (
                    "public_cards_cross_seat_mismatch",
                    seat.hand_num,
                    stage,
                    *seat_pair,
                ),
                "public_cards_cross_seat_mismatch",
                seat,
                message,
                event,
                board_stage=stage,
                peer_conn=peer_label,
                observed_cards=[list(card) for card in cards],
                peer_cards=[list(card) for card in peer_cards],
            )

    def _validate_cross_seat_card_integrity(
        self,
        hand: int,
        seat: SeatReplay,
        message: str,
        event: dict[str, Any],
    ) -> None:
        """Prove two disjoint holes and each peer hole disjoint from the board."""

        if hand <= 0:
            return
        hole_rows = [
            (label, replay.hole_cards_by_hand[hand])
            for label, replay in sorted(self.seats.items())
            if len(replay.hole_cards_by_hand.get(hand) or []) == 2
        ]
        for index, (left_label, left_cards) in enumerate(hole_rows):
            for right_label, right_cards in hole_rows[index + 1 :]:
                collision = sorted(set(left_cards) & set(right_cards))
                if not collision:
                    continue
                self._add_unique_card_issue(
                    (
                        "cross_seat_hole_collision",
                        hand,
                        left_label,
                        right_label,
                        *collision,
                    ),
                    "cross_seat_hole_collision",
                    seat,
                    message,
                    event,
                    left_conn=left_label,
                    right_conn=right_label,
                    collision=[list(card) for card in collision],
                )

        for board_label, board_seat in sorted(self.seats.items()):
            by_stage = board_seat.public_cards_by_hand.get(hand) or {}
            for stage, board_cards in sorted(by_stage.items()):
                for hole_label, hole_cards in hole_rows:
                    if hole_label == board_label:
                        # The existing local public_cards_collision check owns
                        # this same-wire invariant.  This branch proves the
                        # other seat's private cards against the observed board.
                        continue
                    collision = sorted(set(hole_cards) & set(board_cards))
                    if not collision:
                        continue
                    self._add_unique_card_issue(
                        (
                            "cross_seat_hole_board_collision",
                            hand,
                            hole_label,
                            stage,
                            *collision,
                        ),
                        "cross_seat_hole_board_collision",
                        seat,
                        message,
                        event,
                        hole_conn=hole_label,
                        board_conn=board_label,
                        board_stage=stage,
                        collision=[list(card) for card in collision],
                    )

    def _infer_omitted_closer(self, seat: SeatReplay, boundary: str) -> str | None:
        """Apply only the peer pass uniquely proven by a later wire boundary."""
        if seat.hand_num <= 0 or not seat.actions or not seat.action_actors:
            return None
        if seat.action_actors[-1] != "player":
            return None
        player_action = seat.actions[-1][0]
        inferred: str | None = None
        if player_action in {"raise", "allin"}:
            inferred = "call"
        elif seat.stage in {"flop", "turn", "river"} and player_action == "check":
            inferred = "call"
        elif (
            seat.stage == "preflop"
            and seat.is_small_blind
            and player_action == "call"
            and len(seat.actions) == 1
        ):
            inferred = "check"
        if inferred is None:
            return None
        self._apply_opponent_action(
            seat,
            inferred,
            None,
            inferred_boundary=boundary,
        )
        return inferred

    def _consume_showdown(
        self,
        seat: SeatReplay,
        message: str,
        event: dict[str, Any],
    ) -> None:
        payload = message.split("|", 1)[1] if "|" in message else ""
        cards, card_issue = _parse_protocol_cards(payload, expected=2)
        if card_issue:
            self._add_issue(
                "showdown_cards_invalid",
                seat,
                message,
                event,
                reason=card_issue,
            )
            return
        if seat.hand_num <= 0 or len(seat.public_cards) != 5 or seat.fold_occurred:
            self._add_issue(
                "showdown_boundary_invalid",
                seat,
                message,
                event,
                reason="oppo_hands is valid only at a five-card non-fold showdown",
            )
        if set(cards) & set(seat.hole_cards + seat.public_cards):
            self._add_issue(
                "showdown_cards_collision",
                seat,
                message,
                event,
                reason="revealed opponent cards collide with hero or board cards",
            )
        if seat.hand_num in seat.opponent_cards_by_hand:
            self._add_issue("duplicate_showdown_cards", seat, message, event)
        else:
            seat.opponent_cards_by_hand[seat.hand_num] = list(cards)

        peers = [
            peer
            for label, peer in self.seats.items()
            if label != seat.label and seat.hand_num in peer.hole_cards_by_hand
        ]
        if len(peers) != 1:
            self._add_issue(
                "showdown_cross_seat_hole_missing",
                seat,
                message,
                event,
                peer_count=len(peers),
            )
            return
        peer = peers[0]
        peer_hole = peer.hole_cards_by_hand[seat.hand_num]
        if set(cards) != set(peer_hole):
            self._add_issue(
                "showdown_cross_seat_hole_mismatch",
                seat,
                message,
                event,
                peer_conn=peer.label,
                revealed=[list(card) for card in cards],
                actual=[list(card) for card in peer_hole],
            )
    def _opponent_action_requires_response(self, seat: SeatReplay, action_type: str) -> bool:
        if action_type == "fold":
            return False
        if action_type in {"raise", "allin"}:
            return True
        if action_type == "call":
            return seat.stage == "preflop" and not seat.is_small_blind and seat.player_action_count == 0
        if action_type == "check":
            return seat.stage in {"flop", "turn", "river"} and seat.player_action_count == 0
        return False

    def _last_raise(self, seat: SeatReplay) -> int | None:
        for action_type, amount in reversed(seat.actions):
            if action_type == "raise" and amount is not None:
                return amount
        return None

    def _validate_action(self, seat: SeatReplay, action_type: str, amount: int | None) -> tuple[bool, str]:
        if action_type == "unknown":
            return False, "unrecognized action"
        if action_type == "fold":
            return True, ""
        is_first = len(seat.actions) == 0
        if action_type == "call":
            if seat.stage in {"flop", "turn", "river"} and is_first:
                return False, "call is illegal as first postflop action"
            if seat.stage == "preflop" and not seat.is_small_blind and seat.player_action_count == 0:
                if seat.actions and seat.actions[-1][0] == "call":
                    return False, "BB call is illegal after SB call preflop; use check"
            return True, ""
        if action_type == "check":
            if seat.stage == "preflop":
                if not (not seat.is_small_blind and seat.player_action_count == 0 and seat.opponent_bet <= seat.player_bet):
                    return False, "preflop check only allowed for BB first action with no pending bet"
                return True, ""
            if not is_first:
                return False, "postflop check is illegal after the first action; use call to pass"
            return True, ""
        if action_type == "allin":
            if seat.allin_occurred:
                return False, "allin after an allin is illegal; use call or fold"
            return True, ""
        if action_type == "raise":
            if amount is None:
                return False, "raise amount missing"
            if amount <= seat.player_bet:
                return False, "raise-to total must exceed current player bet"
            needed = amount - seat.player_bet
            if needed == seat.player_chips:
                return False, "raise using all remaining chips must be allin"
            if needed > seat.player_chips:
                return False, "raise exceeds remaining chips"
            if seat.allin_occurred:
                return False, "raise after allin is illegal; use call or fold"
            last_raise = self._last_raise(seat)
            if seat.stage == "preflop" and last_raise is None and amount < 2 * BIG_BLIND:
                return False, "first preflop raise must be at least 200"
            if seat.stage in {"flop", "turn", "river"} and last_raise is None and amount < BIG_BLIND:
                return False, "first postflop raise must be at least 100"
            if last_raise is not None and amount < last_raise * 2:
                return False, "consecutive raise must be at least 2x previous raise-to"
            return True, ""
        return False, "unrecognized action type"

    def _record_action(
        self,
        seat: SeatReplay,
        actor: str,
        action_type: str,
        amount: int | None,
        committed: int,
        *,
        inferred_boundary: str | None = None,
    ) -> None:
        seat.actions.append((action_type, amount))
        seat.action_actors.append(actor)
        record: dict[str, Any] = {
            "hand": seat.hand_num,
            "stage": seat.stage,
            "actor": actor,
            "action_type": action_type,
            "committed": int(committed),
            "player_bet_after": seat.player_bet,
            "opponent_bet_after": seat.opponent_bet,
            "player_chips_after": seat.player_chips,
            "opponent_chips_after": seat.opponent_chips,
            "pot_after": seat.pot,
            "inferred": inferred_boundary is not None,
        }
        if amount is not None:
            record["raise_to"] = amount
        if inferred_boundary is not None:
            record["inference_boundary"] = inferred_boundary
        seat.hand_actions.append(record)

    def _apply_player_action(self, seat: SeatReplay, action_type: str, amount: int | None) -> int:
        committed = 0
        if action_type == "call":
            committed = min(max(0, seat.opponent_bet - seat.player_bet), seat.player_chips)
            seat.player_chips -= committed
            seat.player_bet += committed
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - seat.player_bet), seat.player_chips)
            seat.player_chips -= committed
            seat.player_bet += committed
        elif action_type == "allin":
            committed = seat.player_chips
            seat.player_bet += committed
            seat.player_chips = 0
            seat.allin_occurred = True
        elif action_type == "fold":
            seat.fold_occurred = True
        seat.pot += committed
        self._record_action(seat, "player", action_type, amount, committed)
        seat.player_action_count += 1
        return committed

    def _apply_opponent_action(
        self,
        seat: SeatReplay,
        action_type: str,
        amount: int | None,
        *,
        inferred_boundary: str | None = None,
    ) -> int:
        committed = 0
        if action_type == "call":
            committed = min(max(0, seat.player_bet - seat.opponent_bet), seat.opponent_chips)
            seat.opponent_chips -= committed
            seat.opponent_bet += committed
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - seat.opponent_bet), seat.opponent_chips)
            seat.opponent_chips -= committed
            seat.opponent_bet += committed
        elif action_type == "allin":
            committed = seat.opponent_chips
            seat.opponent_bet += committed
            seat.opponent_chips = 0
            seat.allin_occurred = True
        elif action_type == "fold":
            seat.fold_occurred = True
        seat.pot += committed
        self._record_action(
            seat,
            "opponent",
            action_type,
            amount,
            committed,
            inferred_boundary=inferred_boundary,
        )
        return committed

    def summary(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        pending: list[dict[str, Any]] = []
        for seat in self.seats.values():
            if seat.expected_since is None:
                continue
            waited = current - seat.expected_since
            item = {
                "conn": seat.label,
                "hand": seat.hand_num,
                "stage": seat.stage,
                "waited_sec": round(waited, 3),
                "expected_reason": seat.expected_reason,
            }
            pending.append(item)
            if waited >= self.response_timeout_sec:
                self.issues.append({"kind": "pending_bot_response_timeout", **item})
            elif waited >= self.response_warn_sec:
                self.warnings.append({"kind": "pending_bot_response_slow", **item})

        seat_summaries = {
            label: {
                "name": seat.name,
                "hands_started": seat.hands_started,
                "settlements": seat.settlements,
                "settlement_records": list(seat.settlement_records),
                "max_response_sec": round(seat.max_response_sec, 3),
                "pending_expected_action": seat.expected_since is not None,
                "expected_reason": seat.expected_reason,
                "current_hand": seat.hand_num,
                "current_stage": seat.stage,
                "pot": seat.pot,
                "player_chips": seat.player_chips,
                "opponent_chips": seat.opponent_chips,
                "player_bet": seat.player_bet,
                "opponent_bet": seat.opponent_bet,
                "hand_actions": list(seat.hand_actions),
                "blind_records": [
                    {"hand": hand, "blind": blind}
                    for hand, blind in sorted(seat.blind_by_hand.items())
                ],
                "public_card_records": [
                    {
                        "hand": hand,
                        "streets": {
                            stage: [list(card) for card in cards]
                            for stage, cards in sorted(stages.items())
                        },
                    }
                    for hand, stages in sorted(
                        seat.public_cards_by_hand.items()
                    )
                ],
                "showdown_records": [
                    {
                        "hand": hand,
                        "opponent_cards": [list(card) for card in cards],
                    }
                    for hand, cards in sorted(seat.opponent_cards_by_hand.items())
                ],
            }
            for label, seat in sorted(self.seats.items())
        }
        hands = [item["hands_started"] for item in seat_summaries.values()]
        settlements = [item["settlements"] for item in seat_summaries.values()]
        return {
            "events_seen": self.events_seen,
            "hands_started_min": min(hands) if hands else 0,
            "settlements_min": min(settlements) if settlements else 0,
            "seats": seat_summaries,
            "issues": _dedupe_dicts(self.issues),
            "warnings": _dedupe_dicts(self.warnings),
            "pending_expected_actions": pending,
            "max_platform_silent_gap_sec": round(self.max_platform_silent_gap_sec, 3),
        }


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _event_brief(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "dt": event.get("dt"),
        "conn": event.get("conn"),
        "direction": event.get("direction"),
        "messages": list(event.get("messages") or []),
    }


class WireEventRecorder:
    def __init__(self, output_path: Path):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self.events: list[dict[str, Any]] = []
        self._fp = self.output_path.open("a", encoding="utf-8", buffering=1)

    def close(self) -> None:
        self._fp.close()

    def record(
        self,
        *,
        conn: str,
        direction: str,
        raw: bytes,
        messages: list[str],
        remaining: str,
        event_type: str = "data",
        details: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        event = {
            "ts": _now(),
            "t": now,
            "dt": round(now - self.started_at, 6),
            "conn": conn,
            "direction": direction,
            "event_type": event_type,
            # Keep each raw chunk independently inspectable without inventing
            # U+FFFD when a valid UTF-8 code point spans TCP reads.  The
            # incrementally decoded protocol token is recorded in ``messages``
            # and ``raw_hex`` remains the exact byte authority.
            "raw_repr": raw.decode("utf-8", "backslashreplace"),
            "raw_hex": raw.hex(),
            "messages": messages,
            "remaining": remaining,
            "details": details or {},
        }
        self.events.append(event)
        self._fp.write(json.dumps(event, ensure_ascii=False) + "\n")


class TcpWireProbe:
    """Two-port transparent proxy between bots and the official EXE."""

    def __init__(self, *, platform_host: str, platform_port: int, recorder: WireEventRecorder):
        self.platform_host = platform_host
        self.platform_port = int(platform_port)
        self.recorder = recorder
        self._servers: list[asyncio.AbstractServer] = []
        self._tasks: set[asyncio.Task] = set()
        self._buffers: dict[tuple[str, str], str] = {}
        self._decoders: dict[tuple[str, str], codecs.IncrementalDecoder] = {}
        self._awaiting_name: dict[str, bool] = {"A": True, "B": True}

    async def start(self, host: str = "127.0.0.1") -> dict[str, int]:
        ports: dict[str, int] = {}
        for label in ("A", "B"):
            server = await asyncio.start_server(
                lambda reader, writer, label=label: self._accept(label, reader, writer),
                host,
                0,
            )
            self._servers.append(server)
            sock = server.sockets[0]
            ports[label] = int(sock.getsockname()[1])
        return ports

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
            await server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._servers.clear()
        self._tasks.clear()

    async def _accept(self, label: str, bot_reader: asyncio.StreamReader, bot_writer: asyncio.StreamWriter) -> None:
        try:
            server_reader, server_writer = await asyncio.open_connection(self.platform_host, self.platform_port)
        except Exception as exc:
            self.recorder.record(
                conn=label,
                direction="probe_lifecycle",
                raw=b"",
                messages=[],
                remaining="",
                event_type="upstream_connect_failed",
                details={"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
            bot_writer.close()
            await bot_writer.wait_closed()
            return
        tasks = {
            asyncio.create_task(self._pipe(label, "bot_to_server", bot_reader, server_writer)),
            asyncio.create_task(self._pipe(label, "server_to_bot", server_reader, bot_writer)),
        }
        self._tasks.update(tasks)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            self._tasks.difference_update(tasks)
            for writer in (bot_writer, server_writer):
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def _pipe(
        self,
        label: str,
        direction: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        key = (label, direction)
        decoder = self._decoders.setdefault(
            key,
            codecs.getincrementaldecoder("utf-8")("strict"),
        )
        try:
            while True:
                buffered = self._buffers.get(key, "")
                try:
                    if buffered:
                        raw = await asyncio.wait_for(reader.read(4096), timeout=0.05)
                    else:
                        raw = await reader.read(4096)
                except asyncio.TimeoutError:
                    # An idle gap is not a message boundary in the middle of a
                    # UTF-8 code point.  Keep waiting for the missing bytes so
                    # a fragmented official team name is never rewritten with
                    # a replacement character.
                    pending_bytes, _decoder_flag = decoder.getstate()
                    if pending_bytes:
                        continue
                    if direction == "server_to_bot":
                        messages, remaining = split_server_messages(buffered, flush_numeric=True)
                    else:
                        messages, remaining = split_client_messages(
                            buffered,
                            allow_name=self._awaiting_name.get(label, False),
                            flush_numeric=True,
                        )
                    if messages:
                        self._buffers[key] = remaining
                        if direction == "bot_to_server" and self._awaiting_name.get(label, False):
                            self._awaiting_name[label] = False
                        self.recorder.record(
                            conn=label,
                            direction=direction,
                            raw=b"",
                            messages=messages,
                            remaining=remaining,
                            event_type="idle_flush",
                        )
                    continue
                if not raw:
                    buffered = self._buffers.get(key, "")
                    try:
                        buffered += decoder.decode(b"", final=True)
                    except UnicodeDecodeError as exc:
                        self.recorder.record(
                            conn=label,
                            direction=direction,
                            raw=b"",
                            messages=[],
                            remaining=buffered,
                            event_type="stream_encoding_error",
                            details={"error": f"UnicodeDecodeError: {str(exc)[:300]}"},
                        )
                        return
                    if direction == "server_to_bot":
                        messages, remaining = split_server_messages(buffered, flush_numeric=True)
                    else:
                        messages, remaining = split_client_messages(
                            buffered,
                            allow_name=self._awaiting_name.get(label, False),
                            flush_numeric=True,
                        )
                    self._buffers[key] = remaining
                    self.recorder.record(
                        conn=label,
                        direction=direction,
                        raw=b"",
                        messages=messages,
                        remaining=remaining,
                        event_type="stream_eof",
                    )
                    return
                writer.write(raw)
                await writer.drain()
                try:
                    text = decoder.decode(raw, final=False)
                except UnicodeDecodeError as exc:
                    self.recorder.record(
                        conn=label,
                        direction=direction,
                        raw=raw,
                        messages=[],
                        remaining=self._buffers.get(key, ""),
                        event_type="stream_encoding_error",
                        details={"error": f"UnicodeDecodeError: {str(exc)[:300]}"},
                    )
                    return
                buffer = self._buffers.get(key, "") + text
                if direction == "server_to_bot":
                    messages, remaining = split_server_messages(buffer, flush_numeric=False)
                else:
                    messages, remaining = split_client_messages(
                        buffer,
                        allow_name=self._awaiting_name.get(label, False),
                        flush_numeric=False,
                    )
                    if messages and self._awaiting_name.get(label, False):
                        self._awaiting_name[label] = False
                self._buffers[key] = remaining
                self.recorder.record(
                    conn=label,
                    direction=direction,
                    raw=raw,
                    messages=messages,
                    remaining=remaining,
                )
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            self.recorder.record(
                conn=label,
                direction=direction,
                raw=b"",
                messages=[],
                remaining=self._buffers.get(key, ""),
                event_type="stream_error",
                details={"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
            return


def replay_events(events: list[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
    replay = OfficialWireReplay()
    for event in events:
        replay.consume_event(event)
    return replay.summary(now=now)


def load_events(path: str | Path) -> list[dict[str, Any]]:
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events
