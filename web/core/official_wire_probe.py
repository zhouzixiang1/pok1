"""Raw TCP probe and replay diagnostics for the official national platform.

The normal official-platform harness relies on bot-side logs. That is enough
for pass/fail smoke, but not enough to diagnose official EXE failures where the
platform reports an illegal action or a 60 second no-response path. This module
records the bytes between the EXE and each bot, parses the national wire tokens,
and replays enough betting state to classify protocol problems.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
import time
from pathlib import Path
from typing import Any


CARD_RE = re.compile(r"<(\d+),(\d+)>")
SERVER_ACTION_RE = re.compile(r"^(raise|bet)\s+(\d+)")
CLIENT_RAISE_RE = re.compile(r"^raise [1-9]\d*")
EARN_RE = re.compile(r"^earnChips\s+-?\d+")
SMALL_BLIND = 50
BIG_BLIND = 100
INITIAL_CHIPS = 20000


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _take_card_message(buffer: str, prefix: str, count: int) -> tuple[str | None, str]:
    if not buffer.startswith(prefix):
        return None, buffer
    pos = len(prefix)
    for _ in range(count):
        match = CARD_RE.match(buffer, pos)
        if match is None:
            return None, buffer
        pos = match.end()
    return buffer[:pos], buffer[pos:]


def take_server_message(buffer: str) -> tuple[str | None, str]:
    """Take one official server-to-client message from a raw stream buffer."""
    buffer = buffer.lstrip("\r\n\t ")
    if not buffer:
        return None, ""
    if buffer.startswith("name"):
        return "name", buffer[4:]
    for blind in ("SMALLBLIND", "BIGBLIND"):
        msg, rest = _take_card_message(buffer, f"preflop|{blind}|", 2)
        if msg is not None:
            return msg, rest
    for prefix, count in (("flop|", 3), ("turn|", 1), ("river|", 1), ("oppo_hands|", 2)):
        msg, rest = _take_card_message(buffer, prefix, count)
        if msg is not None:
            return msg, rest
    match = EARN_RE.match(buffer)
    if match:
        return buffer[: match.end()], buffer[match.end() :]
    match = SERVER_ACTION_RE.match(buffer)
    if match:
        return buffer[: match.end()], buffer[match.end() :]
    for word in ("allin", "check", "call", "fold"):
        if buffer.startswith(word):
            return word, buffer[len(word) :]
    return None, buffer


def split_server_messages(buffer: str) -> tuple[list[str], str]:
    messages: list[str] = []
    while buffer:
        msg, rest = take_server_message(buffer)
        if msg is None:
            return messages, rest
        messages.append(msg)
        buffer = rest
    return messages, ""


def take_client_message(buffer: str, *, allow_name: bool = False) -> tuple[str | None, str]:
    """Take one bot-to-server message.

    Bot actions are intentionally stricter than server action parsing: the
    official EXE rejects leading/trailing whitespace, tabs, and ``raise  200``.
    """
    buffer = buffer.lstrip("\r\n")
    if not buffer:
        return None, ""
    for word in ("allin", "check", "call", "fold"):
        if buffer.startswith(word):
            return word, buffer[len(word) :]
    match = CLIENT_RAISE_RE.match(buffer)
    if match:
        return buffer[: match.end()], buffer[match.end() :]
    if allow_name and buffer and not buffer.startswith(("raise", "bet")):
        return buffer.strip(), ""
    return None, buffer


def split_client_messages(buffer: str, *, allow_name: bool = False) -> tuple[list[str], str]:
    messages: list[str] = []
    while buffer:
        msg, rest = take_client_message(buffer, allow_name=allow_name)
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
    player_chips: int = INITIAL_CHIPS
    opponent_chips: int = INITIAL_CHIPS
    player_bet: int = 0
    opponent_bet: int = 0
    player_action_count: int = 0
    actions: list[tuple[str, int | None]] = field(default_factory=list)
    allin_occurred: bool = False
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
        self.allin_occurred = False
        self.expected_since = None
        self.expected_reason = ""

    def start_hand(self, blind: str) -> None:
        self.stage = "preflop"
        self.hand_num += 1
        self.hands_started += 1
        self.is_small_blind = blind == "SMALLBLIND"
        self.player_chips = INITIAL_CHIPS
        self.opponent_chips = INITIAL_CHIPS
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
        self.allin_occurred = False
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

    def _seat(self, label: str) -> SeatReplay:
        if label not in self.seats:
            self.seats[label] = SeatReplay(label=label)
        return self.seats[label]

    def consume_event(self, event: dict[str, Any]) -> None:
        self.events_seen += 1
        raw_t = event.get("t")
        t = time.time() if raw_t is None else float(raw_t)
        if self._last_event_t is not None:
            self.max_platform_silent_gap_sec = max(self.max_platform_silent_gap_sec, t - self._last_event_t)
        self._last_event_t = t
        label = str(event.get("conn") or "?")
        direction = str(event.get("direction") or "")
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
            parts = message.split("|", 2)
            blind = parts[1] if len(parts) > 1 else ""
            seat.start_hand(blind)
            if seat.is_small_blind:
                seat.expect(t, "small_blind_preflop_open")
            return
        if message.startswith(("flop|", "turn|", "river|")):
            stage = message.split("|", 1)[0]
            seat.reset_street(stage)
            if not seat.is_small_blind:
                seat.expect(t, f"{stage}_first_action")
            return
        if message.startswith("earnChips"):
            seat.settlements += 1
            seat.expected_since = None
            seat.expected_reason = ""
            return
        if message.startswith("oppo_hands|"):
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
            seat.name = message
            seat.awaiting_name = False
            seat.clear_expectation(t)
            return

        action_type, amount, format_issue = classify_client_action(message)
        if format_issue:
            self._add_issue(format_issue, seat, message, event)
            return
        if seat.expected_since is None:
            self._add_warning(
                "unsolicited_client_action",
                seat,
                message,
                event,
                reason="bot sent an action while replay had no pending platform request",
            )
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
            if last_raise is not None and amount <= last_raise * 2:
                return False, "consecutive raise must be strictly greater than 2x previous raise-to"
            return True, ""
        return False, "unrecognized action type"

    def _apply_player_action(self, seat: SeatReplay, action_type: str, amount: int | None) -> None:
        if action_type == "call":
            committed = min(max(0, seat.opponent_bet - seat.player_bet), seat.player_chips)
            seat.player_chips -= committed
            seat.player_bet += committed
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - seat.player_bet), seat.player_chips)
            seat.player_chips -= committed
            seat.player_bet += committed
        elif action_type == "allin":
            seat.player_bet += seat.player_chips
            seat.player_chips = 0
            seat.allin_occurred = True
        seat.actions.append((action_type, amount))
        seat.player_action_count += 1

    def _apply_opponent_action(self, seat: SeatReplay, action_type: str, amount: int | None) -> None:
        if action_type == "call":
            committed = min(max(0, seat.player_bet - seat.opponent_bet), seat.opponent_chips)
            seat.opponent_chips -= committed
            seat.opponent_bet += committed
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - seat.opponent_bet), seat.opponent_chips)
            seat.opponent_chips -= committed
            seat.opponent_bet += committed
        elif action_type == "allin":
            seat.opponent_bet += seat.opponent_chips
            seat.opponent_chips = 0
            seat.allin_occurred = True
        seat.actions.append((action_type, amount))

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
                "max_response_sec": round(seat.max_response_sec, 3),
                "pending_expected_action": seat.expected_since is not None,
                "expected_reason": seat.expected_reason,
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


class WireEventRecorder:
    def __init__(self, output_path: Path):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self.events: list[dict[str, Any]] = []
        self._fp = self.output_path.open("a", encoding="utf-8", buffering=1)

    def close(self) -> None:
        self._fp.close()

    def record(self, *, conn: str, direction: str, raw: bytes, messages: list[str], remaining: str) -> None:
        now = time.time()
        event = {
            "ts": _now(),
            "t": now,
            "dt": round(now - self.started_at, 6),
            "conn": conn,
            "direction": direction,
            "raw_repr": raw.decode("utf-8", "replace"),
            "raw_hex": raw.hex(),
            "messages": messages,
            "remaining": remaining,
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
        except Exception:
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
        try:
            while True:
                raw = await reader.read(4096)
                if not raw:
                    return
                writer.write(raw)
                await writer.drain()
                text = raw.decode("utf-8", "replace")
                buffer = self._buffers.get(key, "") + text
                if direction == "server_to_bot":
                    messages, remaining = split_server_messages(buffer)
                else:
                    # Name detection is handled by the replay pass. For logging,
                    # allow a non-action first packet to be emitted as a message.
                    messages, remaining = split_client_messages(buffer, allow_name=True)
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
        except (ConnectionError, OSError):
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
