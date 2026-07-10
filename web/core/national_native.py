"""Native national TCP execution backend for evolved bots.

The legacy national backend runs Botzone JSON bots through ``sever/bot_adapter.py``.
This module is the native path: a candidate must contain ``national_bot.py`` that
connects to the national TCP server directly and sends wire actions itself.
"""

from __future__ import annotations

import asyncio
import ast
from dataclasses import dataclass
from datetime import datetime
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
from typing import Any

from eval_stats import paired_bootstrap_ci
from bot_namespace import ACTIVE_BOT_PREFIX, bot_name, parse_bot_version, version_sort_key
from national_runtime_telemetry import (
    empty_bot_log_summary as _empty_bot_log_summary,
    empty_runtime_telemetry as _empty_runtime_telemetry,
    merge_runtime_telemetry as _merge_runtime_telemetry,
    parse_native_bot_log as _parse_native_bot_log,
    server_action_latency as _server_action_latency,
)
from pipeline_schema import NationalAcceptanceResult


ROOT = Path(__file__).resolve().parents[2]
SEVER_DIR = ROOT / "sever"
NATIVE_ENTRY = "national_bot.py"
SEEDED_NATIVE_LAUNCHER = (
    "import os, random, runpy, sys\n"
    "entry = os.environ['POK_NATIVE_ENTRY']\n"
    "seed = os.environ.get('POK_NATIVE_BOT_SEED')\n"
    "if seed not in (None, ''):\n"
    "    random.seed(int(seed))\n"
    "sys.argv = [entry] + sys.argv[1:]\n"
    "runpy.run_path(entry, run_name='__main__')\n"
)
TRACE_PREFIX = "POK_TRACE_DECISION "


NATIVE_BOT_TEMPLATE = r'''#!/usr/bin/env python3
"""Native national TCP entrypoint for this bot.

This file is the formal national-platform submission entry. It connects to the
TCP server, maintains raw-stream state, calls the local strategy in process,
and sends only national wire actions: raise <amount>, fold, call, check, allin.
It deliberately uses no legacy bridge module and prints no JSON responses to
stdout.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import time
import traceback


BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

SMALL_BLIND = 50
BIG_BLIND = 100
INITIAL_CHIPS = 20000
TOTAL_HANDS = 70
CARD_RE = re.compile(r"<(\d+),(\d+)>")
TCP_TO_JUDGE_SUIT = {0: 2, 1: 0, 2: 1, 3: 3}
ACTION_PREFIX_RE = re.compile(r"^(raise|bet)\s+(\d+)")
EARN_PREFIX_RE = re.compile(r"^earnChips\s+-?\d+")
DEFAULT_OFFICIAL_ACTION_DELAY_SEC = 0.30
OFFICIAL_ACTION_DELAY_ENV = "POK_OFFICIAL_ACTION_DELAY"

_LOG_FP = None


def _log_open(path: str) -> None:
    global _LOG_FP
    if not path:
        return
    try:
        _LOG_FP = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:
        _LOG_FP = None


def _log(msg: str) -> None:
    if _LOG_FP is None:
        return
    try:
        import time as _time
        _LOG_FP.write(f"[{_time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _official_action_delay_sec() -> float:
    raw = os.environ.get(OFFICIAL_ACTION_DELAY_ENV, str(DEFAULT_OFFICIAL_ACTION_DELAY_SEC))
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = DEFAULT_OFFICIAL_ACTION_DELAY_SEC
    return max(0.0, min(delay, 2.0))


def _tcp_card_to_int(suit: int, rank: int) -> int:
    return rank * 4 + TCP_TO_JUDGE_SUIT[suit]


def _parse_cards(text: str) -> list[int]:
    return [_tcp_card_to_int(int(s), int(r)) for s, r in CARD_RE.findall(text)]


def _parse_action(raw: str) -> tuple[str, int | None]:
    parts = raw.strip().split()
    if not parts:
        return "unknown", None
    head = parts[0]
    if head in {"raise", "bet"} and len(parts) >= 2 and parts[1].isdigit():
        return "raise", int(parts[1])
    if head in {"call", "check", "fold", "allin"}:
        return head, None
    return "unknown", None


def _take_card_message(buffer: str, prefix: str, count: int) -> tuple[str | None, str]:
    if not buffer.startswith(prefix):
        return None, buffer
    pos = len(prefix)
    for _ in range(count):
        match = CARD_RE.match(buffer, pos)
        if not match:
            return None, buffer
        pos = match.end()
    return buffer[:pos], buffer[pos:]


def _take_message(buffer: str) -> tuple[str | None, str]:
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
    match = EARN_PREFIX_RE.match(buffer)
    if match:
        return buffer[:match.end()], buffer[match.end():]
    match = ACTION_PREFIX_RE.match(buffer)
    if match:
        return buffer[:match.end()], buffer[match.end():]
    for word in ("allin", "check", "call", "fold"):
        if buffer.startswith(word):
            return word, buffer[len(word):]
    return None, buffer


def _split_messages(buffer: str) -> tuple[list[str], str]:
    messages: list[str] = []
    while buffer:
        msg, rest = _take_message(buffer)
        if msg is None:
            return messages, rest
        messages.append(msg)
        buffer = rest
    return messages, ""


def _resolve_seat(name: str, seat: str) -> str:
    value = seat.lower()
    if value in {"upper", "lower"}:
        return value
    lowered = name.strip().lower()
    if lowered.endswith(("b", "2", "_lower", "-lower", "lower", "bottom")):
        return "lower"
    if lowered.endswith(("a", "1", "_upper", "-upper", "upper", "top")):
        return "upper"
    return "unknown"


class NativeNationalBot:
    def __init__(self, name: str, seat: str = "auto"):
        self.name = name
        self.seat = _resolve_seat(name, seat)
        from main import sanitize_action
        from state import infer_remaining_hands_from_requests, reconstruct_state
        from strategy import get_action

        self.get_action = get_action
        self.reconstruct_state = reconstruct_state
        self.infer_remaining_hands = infer_remaining_hands_from_requests
        self.sanitize_action = sanitize_action
        self._official_action_delay_sec = _official_action_delay_sec()
        self._last_platform_message_at = 0.0
        self._reset_match()

    def _reset_match(self) -> None:
        self._buf = ""
        self._my_cards: list[int] = []
        self._public_cards: list[int] = []
        self._is_sb = False
        self._hand_num = 0
        self._history: list[dict] = []
        self._stage = "preflop"
        self._my_id = 0
        self._opponent_id = 1
        self._dealer_id = 0
        self._my_action_count = 0
        self._my_chips = INITIAL_CHIPS
        self._my_stage_bet = 0
        self._opponent_chips = INITIAL_CHIPS
        self._opponent_stage_bet = 0
        self._pot = 0
        self._in_allin_runout = False
        self._requests: list[dict] = []
        self._responses: list[int] = []
        self._total_win_chips = [0, 0]
        self._total_win_games = [0, 0]
        self._last_earned = 0
        self._showdowns: list[dict] = []

    def _acts_first_postflop(self) -> bool:
        return not self._is_sb

    def _responding_to_check(self) -> bool:
        round_num = self._round_num()
        return (
            self._my_action_count == 0
            and self._history
            and self._history[-1].get("round") == round_num
            and self._history[-1].get("player_id") == self._opponent_id
            and self._history[-1].get("action_type") == "check"
        )

    def _round_num(self) -> int:
        return {"preflop": 0, "flop": 1, "turn": 2, "river": 3}.get(self._stage, 0)

    def _betting_snapshot(self) -> dict:
        to_call = max(0, self._opponent_stage_bet - self._my_stage_bet)
        return {
            "opponent_chips": self._opponent_chips,
            "my_stage_bet": self._my_stage_bet,
            "opponent_stage_bet": self._opponent_stage_bet,
            "pot": self._pot,
            "to_call": to_call,
            "opponent_allin": self._opponent_chips == 0 and self._opponent_stage_bet > 0,
        }

    def _request(self) -> dict:
        req = {
            "num_players": 2,
            "dealer_id": self._dealer_id,
            "my_id": self._my_id,
            "my_chips": self._my_chips,
            "my_cards": list(self._my_cards),
            "public_cards": list(self._public_cards),
            "history": list(self._history),
            "hand": self._hand_num - 1,
            "max_hand": TOTAL_HANDS,
            "total_win_chips": list(self._total_win_chips),
            "total_win_games": list(self._total_win_games),
            "opponent_showdowns": list(self._showdowns),
            **self._betting_snapshot(),
        }
        if "remaining_hands" not in req:
            req["remaining_hands"] = self.infer_remaining_hands(self._requests + [req])
        return req

    def _strategy_action(self) -> int:
        req = self._request()
        self._requests.append(req)
        action = self.get_action(req, list(self._requests))
        try:
            state = self.reconstruct_state(req)
            action = self.sanitize_action(action, state, req["my_chips"])
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return 0
        try:
            return int(action)
        except (TypeError, ValueError):
            return 0

    def _current_round_has_allin(self) -> bool:
        round_num = self._round_num()
        return any(h.get("round") == round_num and h.get("action_type") == "allin" for h in self._history)

    def _record_action(self, player_id: int, action_type: str, amount: int | None, committed: int = 0) -> None:
        if action_type == "call":
            action_val = 0
        elif action_type == "check":
            action_val = 0
        elif action_type == "fold":
            action_val = -1
        elif action_type == "allin":
            action_val = -2
        elif action_type == "raise" and amount is not None:
            if player_id == self._my_id:
                action_val = self._my_stage_bet
            else:
                action_val = self._opponent_stage_bet
        else:
            return
        entry = {
            "round": self._round_num(),
            "player_id": player_id,
            "action": action_val,
            "action_type": action_type,
        }
        if action_type in {"call", "raise", "allin"}:
            if player_id == self._my_id:
                entry["stage_bet"] = self._my_stage_bet
                entry["chips_after"] = self._my_chips
            else:
                entry["stage_bet"] = self._opponent_stage_bet
                entry["chips_after"] = self._opponent_chips
            entry["committed"] = committed
        self._history.append(entry)

    def _last_raise_total(self) -> int | None:
        round_num = self._round_num()
        for record in reversed(self._history):
            if record.get("round") != round_num:
                continue
            if record.get("action_type") != "raise":
                continue
            value = record.get("stage_bet", record.get("action"))
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def _minimum_raise_total(self) -> int:
        last_raise = self._last_raise_total()
        if last_raise is not None:
            minimum = last_raise * 2 + 1
        elif self._stage == "preflop":
            minimum = 2 * BIG_BLIND
        else:
            minimum = BIG_BLIND
        return max(minimum, self._my_stage_bet + 1, self._opponent_stage_bet + 1)

    def _apply_opponent_action(self, action_type: str, amount: int | None) -> int:
        committed = 0
        if action_type == "call":
            committed = min(max(0, self._my_stage_bet - self._opponent_stage_bet), self._opponent_chips)
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - self._opponent_stage_bet), self._opponent_chips)
        elif action_type == "allin":
            committed = self._opponent_chips
        if committed > 0:
            self._opponent_chips -= committed
            self._opponent_stage_bet += committed
            self._pot += committed
        return committed

    def _apply_my_action(self, action_type: str, amount: int | None) -> int:
        committed = 0
        if action_type == "call":
            committed = min(max(0, self._opponent_stage_bet - self._my_stage_bet), self._my_chips)
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - self._my_stage_bet), self._my_chips)
        elif action_type == "allin":
            committed = self._my_chips
        if committed > 0:
            self._my_chips -= committed
            self._my_stage_bet += committed
            self._pot += committed
        return committed

    def _raise_action(self, requested_total: int) -> tuple[str, str, int | None]:
        if self._current_round_has_allin():
            return self._zero_action()
        target = max(int(requested_total), self._minimum_raise_total())
        needed = target - self._my_stage_bet
        if needed <= 0:
            return self._zero_action()
        if needed >= self._my_chips:
            return "allin", "allin", None
        return f"raise {target}", "raise", target

    def _zero_action(self) -> tuple[str, str, int | None]:
        if self._opponent_stage_bet > self._my_stage_bet:
            return "call", "call", None
        if self._responding_to_check():
            return "call", "call", None
        return "check", "check", None

    def _action_to_tcp(self, action: int) -> tuple[str, str, int | None]:
        if action == -1:
            return "fold", "fold", None
        if action == -2:
            if self._current_round_has_allin():
                return self._zero_action()
            if self._opponent_chips == 0 and self._opponent_stage_bet > self._my_stage_bet:
                return "call", "call", None
            return "allin", "allin", None
        if action > 0:
            if self._current_round_has_allin():
                return self._zero_action()
            if action <= self._my_stage_bet:
                return self._zero_action()
            if self._opponent_stage_bet > self._my_stage_bet and action <= self._opponent_stage_bet:
                return "call", "call", None
            return self._raise_action(action)
        return self._zero_action()

    def _should_respond(self, action_type: str) -> bool:
        if action_type == "fold":
            return False
        if action_type in {"raise", "allin"}:
            return True
        if action_type == "call":
            return self._stage == "preflop" and not self._is_sb and self._my_action_count == 0
        if action_type == "check":
            return self._stage != "preflop" and self._my_action_count == 0
        return False

    def _send_wire_action(self, sock: socket.socket, msg: str) -> None:
        if self._official_action_delay_sec > 0 and self._last_platform_message_at > 0:
            elapsed = time.perf_counter() - self._last_platform_message_at
            wait_sec = self._official_action_delay_sec - elapsed
            if wait_sec > 0:
                _log(f"OFFICIAL_ACTION_DELAY wait={wait_sec:.3f}s target={self._official_action_delay_sec:.3f}s")
                time.sleep(wait_sec)
        sock.sendall(msg.encode("utf-8"))

    def _send_decision(self, sock: socket.socket) -> None:
        t0 = time.perf_counter()
        _log(
            f"DECIDE start name={self.name} hand={self._hand_num} stage={self._stage} "
            f"act_cnt={self._my_action_count} my_sb={self._my_stage_bet} "
            f"opp_sb={self._opponent_stage_bet} my_chips={self._my_chips} "
            f"opp_chips={self._opponent_chips} is_sb={self._is_sb}"
        )
        try:
            action = self._strategy_action()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            _log("DECIDE exception -> fold")
            action = -1
        elapsed = time.perf_counter() - t0
        _log(f"DECIDE done action={action!r} elapsed={elapsed:.3f}s")
        self._responses.append(int(action))
        msg, action_type, amount = self._action_to_tcp(int(action))
        self._send_wire_action(sock, msg)
        _log(
            f"SEND name={self.name} hand={self._hand_num} stage={self._stage} "
            f"act_cnt={self._my_action_count} my_sb={self._my_stage_bet} "
            f"opp_sb={self._opponent_stage_bet} my_chips={self._my_chips} "
            f"opp_chips={self._opponent_chips} is_sb={self._is_sb} msg={msg!r}"
        )
        committed = self._apply_my_action(action_type, amount)
        self._record_action(self._my_id, action_type, amount, committed)
        if action_type == "call" and (self._current_round_has_allin() or self._my_chips == 0 or self._opponent_chips == 0):
            self._in_allin_runout = True
        self._my_action_count += 1

    def handle(self, line: str, sock: socket.socket) -> None:
        self._last_platform_message_at = time.perf_counter()
        if line.startswith("name"):
            sock.sendall(self.name.encode("utf-8"))
            _log(f"SEND name_handshake name={self.name!r}")
            return
        if line.startswith("preflop"):
            parts = line.split("|", 2)
            blind = parts[1]
            self._is_sb = blind == "SMALLBLIND"
            self._my_cards = _parse_cards(parts[2])
            self._public_cards = []
            self._stage = "preflop"
            self._hand_num += 1
            self._history = []
            self._my_action_count = 0
            self._my_chips = INITIAL_CHIPS
            self._opponent_chips = INITIAL_CHIPS
            self._pot = SMALL_BLIND + BIG_BLIND
            self._in_allin_runout = False
            if self._is_sb:
                self._my_chips -= SMALL_BLIND
                self._my_stage_bet = SMALL_BLIND
                self._opponent_chips -= BIG_BLIND
                self._opponent_stage_bet = BIG_BLIND
            else:
                self._my_chips -= BIG_BLIND
                self._my_stage_bet = BIG_BLIND
                self._opponent_chips -= SMALL_BLIND
                self._opponent_stage_bet = SMALL_BLIND
            self._dealer_id = 0 if self._is_sb else 1
            if self._is_sb:
                self._send_decision(sock)
            return
        if line.startswith(("flop", "turn", "river")):
            stage, cards = line.split("|", 1)
            self._stage = stage
            self._public_cards.extend(_parse_cards(cards))
            self._my_action_count = 0
            self._my_stage_bet = 0
            self._opponent_stage_bet = 0
            if not self._in_allin_runout and self._acts_first_postflop():
                self._send_decision(sock)
            return
        if line.startswith("earnChips"):
            earned = int(line.split()[1])
            self._last_earned = earned
            self._total_win_chips[self._my_id] += earned
            self._total_win_chips[self._opponent_id] -= earned
            if earned > 0:
                self._total_win_games[self._my_id] += 1
            elif earned < 0:
                self._total_win_games[self._opponent_id] += 1
            self._in_allin_runout = False
            return
        if line.startswith("oppo_hands|"):
            self._showdowns.append({
                "hand": self._hand_num,
                "opponent_cards": _parse_cards(line.split("|", 1)[1]),
                "my_cards": list(self._my_cards),
                "public_cards": list(self._public_cards),
                "history": list(self._history),
                "earned": self._last_earned,
            })
            return

        action_type, amount = _parse_action(line)
        if action_type == "unknown":
            _log(f"UNKNOWN message={line!r}")
            return
        committed = self._apply_opponent_action(action_type, amount)
        self._record_action(self._opponent_id, action_type, amount, committed)
        if action_type == "call" and (self._current_round_has_allin() or self._my_chips == 0 or self._opponent_chips == 0):
            self._in_allin_runout = True
        if self._should_respond(action_type):
            self._send_decision(sock)


def run_client(host: str, port: int, name: str, log_path: str = "", seat: str = "auto") -> int:
    _log_open(log_path)
    bot = NativeNationalBot(name, seat)
    _log(f"START name={name} seat={bot.seat} host={host} port={port} log={log_path or '-'}")
    with socket.create_connection((host, port), timeout=30) as sock:
        sock.settimeout(180)
        buffer = ""
        while True:
            try:
                data = sock.recv(4096)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
                _log(f"RECV closed_by_server exception={type(exc).__name__}: {exc}")
                return 0
            if not data:
                _log("RECV empty -> server closed")
                return 0
            chunk = data.decode("utf-8", "replace")
            buffer += chunk
            _log(f"RECV raw={chunk!r} buffer={buffer!r}")
            messages, buffer = _split_messages(buffer)
            for line in messages:
                _log(f"DISPATCH line={line!r}")
                bot.handle(line, sock)


def main() -> int:
    parser = argparse.ArgumentParser(description="Native national TCP bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="Bot")
    parser.add_argument("--seat", choices=("auto", "upper", "lower"), default="auto",
                        help="Desktop seat hint; action order is still inferred from blind state.")
    parser.add_argument("--log", default="", help="Log file path. Empty disables file logging.")
    args = parser.parse_args()
    try:
        return run_client(args.host, args.port, args.name, args.log, args.seat)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''


@dataclass(frozen=True)
class NativeBotSpec:
    label: str
    path: Path
    entry: Path
    temp_root: Path | None = None
    wrapper_used: bool = False


class _TCPClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.name = ""
        self._buffer = ""
        self.closed = False

    async def send_line(self, msg: str) -> None:
        if self.closed:
            return
        self.writer.write((msg + "\n").encode("utf-8"))
        await self.writer.drain()

    async def recv_line(self, timeout: float) -> str | None:
        if self.closed:
            return None
        try:
            async with asyncio.timeout(timeout):
                while True:
                    msg = self._take_client_message()
                    if msg is not None:
                        return msg
                    chunk = await self.reader.read(4096)
                    if not chunk:
                        self.closed = True
                        return None
                    self._buffer += chunk.decode("utf-8")
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return None

    def _take_client_message(self) -> str | None:
        self._buffer = self._buffer.lstrip("\r\n\t ")
        if not self._buffer:
            return None
        if "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                return line
            return None
        for token in ("allin", "check", "call", "fold"):
            if self._buffer.startswith(token):
                self._buffer = self._buffer[len(token):]
                return token
        if self._buffer.startswith("raise "):
            pos = len("raise ")
            while pos < len(self._buffer) and self._buffer[pos].isdigit():
                pos += 1
            if pos > len("raise "):
                msg = self._buffer[:pos]
                self._buffer = self._buffer[pos:]
                return msg
            return None
        if self._buffer.startswith("bet "):
            pos = len("bet ")
            while pos < len(self._buffer) and self._buffer[pos].isdigit():
                pos += 1
            if pos > len("bet "):
                msg = "raise " + self._buffer[len("bet "):pos]
                self._buffer = self._buffer[pos:]
                return msg
            return None
        # Name handshake: official clients send a short raw team name with no
        # delimiter. In local tests the whole write normally arrives in one read;
        # after this point there is no protocol-level marker to wait for.
        if self._buffer and not self._buffer.startswith(("raise", "bet")):
            msg = self._buffer.strip()
            self._buffer = ""
            return msg
        return None

    async def close(self, timeout: float = 1.0) -> None:
        self.closed = True
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), timeout=timeout)
        except (asyncio.TimeoutError, OSError, ConnectionError):
            pass


def _import_sever_modules():
    prefixes = ("server", "engine")
    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name in prefixes or name.startswith("server.") or name.startswith("engine.")
    }
    for name in saved:
        sys.modules.pop(name, None)

    inserted: list[str] = []
    for idx, path in ((0, str(SEVER_DIR)), (1, str(ROOT))):
        if path not in sys.path:
            sys.path.insert(idx, path)
            inserted.append(path)
    try:
        server_init = SEVER_DIR / "server" / "__init__.py"
        server_spec = importlib.util.spec_from_file_location(
            "server",
            server_init,
            submodule_search_locations=[str(SEVER_DIR / "server")],
        )
        if server_spec is not None and server_spec.loader is not None:
            server_module = importlib.util.module_from_spec(server_spec)
            sys.modules["server"] = server_module
            server_spec.loader.exec_module(server_module)
        importlib.invalidate_caches()
        from engine.deck import Deck as _Deck  # noqa: E402
        from engine.game import GameEngine as _GameEngine  # noqa: E402
        from engine.thp_recorder import THPRecorder as _THPRecorder  # noqa: E402
        return _GameEngine, _THPRecorder, _Deck
    finally:
        for name in list(sys.modules):
            if name in prefixes or name.startswith("server.") or name.startswith("engine."):
                sys.modules.pop(name, None)
        sys.modules.update(saved)
        for path in inserted:
            try:
                sys.path.remove(path)
            except ValueError:
                pass


GameEngine, THPRecorder, Deck = _import_sever_modules()


class _LimitedTCPGameEngine(GameEngine):
    def __init__(
        self,
        clients: list[_TCPClient],
        events: list[dict[str, Any]],
        deck_seed_base: int | None = None,
        action_timeout_sec: float = 60.0,
    ):
        self._clients = clients
        self.events = events
        self.action_timeout_sec = float(action_timeout_sec)
        deck_factory = None
        if deck_seed_base is not None:
            deck_factory = lambda hand_num: Deck(seed=deck_seed_base + hand_num)
        super().__init__(
            send_func=self._send_to_client,
            broadcast_func=self._record_event,
            recorder=THPRecorder(clients[0].name or "A", clients[1].name or "B"),
            deck_factory=deck_factory,
        )

    async def _send_to_client(self, player_idx: int, message: str):
        await self._clients[player_idx].send_line(message)

    async def _recv_action(self, player_idx: int) -> str | None:
        return await self._clients[player_idx].recv_line(timeout=self.action_timeout_sec)

    async def _record_event(self, event: dict[str, Any]):
        self.events.append(dict(event))

    async def run_limited_match(self, name1: str, name2: str, hands: int):
        self.players[0].name = name1
        self.players[1].name = name2
        self.total_earnings = [0, 0]
        self.match_over = False
        for hand_num in range(1, hands + 1):
            self.hand_num = hand_num
            result = await self._run_hand(hand_num)
            if result is None:
                break
            self.total_earnings[0] += result.earnings[0]
            self.total_earnings[1] += result.earnings[1]
            if self.match_over:
                break


def ensure_native_entry(bot_dir: str | Path, *, overwrite: bool = False) -> Path:
    bot_dir = Path(bot_dir)
    entry = bot_dir / NATIVE_ENTRY
    if overwrite or not entry.exists():
        entry.write_text(NATIVE_BOT_TEMPLATE, encoding="utf-8")
    return entry


def check_native_contract(bot_dir: str | Path) -> list[str]:
    bot_dir = Path(bot_dir)
    entry = bot_dir / NATIVE_ENTRY
    errors: list[str] = []
    if not entry.exists():
        return [f"{NATIVE_ENTRY} missing; national_native bots must have a direct TCP entrypoint"]
    try:
        text = entry.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{NATIVE_ENTRY} unreadable: {exc}"]
    forbidden = ("bot_adapter", "BotAdapter", '"response"', "'response'")
    for token in forbidden:
        if token in text:
            errors.append(f"{NATIVE_ENTRY}: forbidden legacy adapter/JSON response token {token!r}")
    legacy_wire_tokens = (
        "makefile(",
        ".readline(",
        "readline()",
        "newline=\"\\n\"",
        "newline='\\n'",
        "msg + \"\\n\"",
        "msg + '\\n'",
        "self.name + \"\\n\"",
        "self.name + '\\n'",
    )
    for token in legacy_wire_tokens:
        if token in text:
            errors.append(f"{NATIVE_ENTRY}: forbidden legacy newline TCP token {token!r}")
    required = ("socket", "raise ", "fold", "call", "check", "allin")
    for token in required:
        if token not in text:
            errors.append(f"{NATIVE_ENTRY}: missing native TCP token {token!r}")
    for token in ("sock.recv", "_split_messages"):
        if token not in text:
            errors.append(f"{NATIVE_ENTRY}: missing official raw TCP splitter token {token!r}")
    official_delay_tokens = ("POK_OFFICIAL_ACTION_DELAY", "_send_wire_action", "DEFAULT_OFFICIAL_ACTION_DELAY_SEC")
    for token in official_delay_tokens:
        if token not in text:
            errors.append(
                f"{NATIVE_ENTRY}: missing official EXE action throttle token {token!r}; "
                "native bots must delay action sends for the official Windows platform"
            )
    if "wire value is the extra chips added" in text:
        errors.append(f"{NATIVE_ENTRY}: TCP raise amount is documented as an increment; it must be raise-to-total")
    if "committed = min(max(0, amount), self._opponent_chips)" in text:
        errors.append(f"{NATIVE_ENTRY}: opponent raise amount is treated as an increment; it must be raise-to-total")
    if "return f\"raise {needed}\", \"raise\", action" in text:
        errors.append(f"{NATIVE_ENTRY}: outgoing raise uses delta-style wire amount; it must send raise-to-total")
    formal_wrapper = "class NativeNationalBot" in text or "def _action_to_tcp" in text or "def _zero_action" in text
    if formal_wrapper:
        action_to_tcp = _function_source(text, "_action_to_tcp")
        if action_to_tcp is None:
            errors.append(f"{NATIVE_ENTRY}: missing _action_to_tcp protocol translator")
        elif "self._current_round_has_allin()" not in action_to_tcp:
            errors.append(
                f"{NATIVE_ENTRY}: _action_to_tcp missing current-round allin guard; "
                "after any allin it must map strategy raises/allins to call/fold/check-safe actions"
            )
        zero_action = _function_source(text, "_zero_action")
        if zero_action is None:
            errors.append(f"{NATIVE_ENTRY}: missing _zero_action call/check mapper")
        elif "_responding_to_check()" not in zero_action:
            errors.append(
                f"{NATIVE_ENTRY}: _zero_action missing postflop check-response guard; "
                "second pass after an opponent check must be call, not check"
            )
    if _strategy_action_has_exception_pass(text):
        errors.append(
            f"{NATIVE_ENTRY}: _strategy_action must not continue with raw action after sanitizer failure"
        )
    return errors


def _function_source(text: str, name: str) -> str | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    return None


def _strategy_action_has_exception_pass(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_strategy_action":
            for child in ast.walk(node):
                if isinstance(child, ast.Try):
                    for handler in child.handlers:
                        if (
                            _handler_catches_broad_exception(handler)
                            and len(handler.body) == 1
                            and isinstance(handler.body[0], ast.Pass)
                        ):
                            return True
    return False


def _handler_catches_broad_exception(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in {"Exception", "BaseException"}
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(item, ast.Name) and item.id in {"Exception", "BaseException"}
            for item in handler.type.elts
        )
    return False


def _bot_version(label: str) -> int:
    return parse_bot_version(label) or -1


def resolve_bot(token: str | Path) -> tuple[str, Path]:
    token_str = str(token)
    raw = Path(token_str)
    candidates: list[Path] = []
    if raw.exists():
        candidates.append(raw)
    if token_str.startswith("v") and token_str[1:].isdigit():
        candidates.append(ROOT / "bots" / bot_name(token_str[1:]))
    if token_str.isdigit():
        candidates.append(ROOT / "bots" / bot_name(token_str))
        candidates.append(ROOT / "bots" / f"bot{token_str}")
    if token_str.startswith(ACTIVE_BOT_PREFIX) or token_str.startswith("claude_v") or token_str.startswith("bot"):
        candidates.append(ROOT / "bots" / token_str)
    for path in candidates:
        if path.is_dir() and (path / "main.py").exists():
            return path.name, path.resolve()
        if path.is_file():
            return path.parent.name if path.name == "main.py" else path.stem, path.resolve().parent
    raise ValueError(f"bot not found or missing main.py: {token_str}")


def _completed_active_bots() -> list[tuple[str, Path]]:
    from evolution_infra import get_active_bots

    specs: list[tuple[str, Path]] = []
    for name in get_active_bots():
        path = ROOT / "bots" / name
        if path.is_dir() and (path / "main.py").exists():
            specs.append((name, path.resolve()))
    return sorted(specs, key=lambda item: version_sort_key(item[0]), reverse=True)


def select_acceptance_opponents(candidate_label: str, source_v: int | None, limit: int = 2) -> list[tuple[str, Path]]:
    chosen: list[tuple[str, Path]] = []
    seen = {candidate_label}

    def add(spec: tuple[str, Path]):
        if spec[0] not in seen and spec[1].exists():
            chosen.append(spec)
            seen.add(spec[0])

    if source_v is not None:
        try:
            add(resolve_bot(bot_name(source_v)))
        except ValueError:
            pass
    for spec in _completed_active_bots():
        add(spec)
        if len(chosen) >= limit:
            break
    return chosen[:limit]


def _prepare_native_spec(
    label: str,
    bot_dir: Path,
    *,
    allow_legacy_wrapper: bool = False,
) -> NativeBotSpec:
    """Resolve an existing native entry, optionally wrapping a copied legacy bot.

    The strict path never writes to ``bot_dir``. Wrapper generation is reserved
    for the explicitly named legacy/debug runner and only touches a temporary
    copy of the source bot.
    """
    entry = bot_dir / NATIVE_ENTRY
    if entry.exists():
        contract_errors = check_native_contract(bot_dir)
        if not contract_errors:
            return NativeBotSpec(label=label, path=bot_dir, entry=entry)
        if not allow_legacy_wrapper:
            raise ValueError(f"{label}: invalid {NATIVE_ENTRY}: {'; '.join(contract_errors[:3])}")
    if not allow_legacy_wrapper:
        raise ValueError(f"{label}: missing required {NATIVE_ENTRY}")
    tmp = Path(tempfile.mkdtemp(prefix=f"pok_native_{label}_"))
    dst = tmp / bot_dir.name
    shutil.copytree(bot_dir, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return NativeBotSpec(
        label=label,
        path=dst,
        entry=ensure_native_entry(dst, overwrite=True),
        temp_root=tmp,
        wrapper_used=True,
    )


def _cleanup_specs(specs: list[NativeBotSpec]) -> None:
    for spec in specs:
        if spec.temp_root is not None:
            shutil.rmtree(spec.temp_root, ignore_errors=True)


def _native_bot_seed(bot_seed_base: int | None, player_idx: int) -> int | None:
    if bot_seed_base is None:
        return None
    return int(bot_seed_base) + int(player_idx)


def _parse_decision_trace(stderr_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in stderr_text.splitlines():
        if not raw_line.startswith(TRACE_PREFIX):
            continue
        payload = raw_line[len(TRACE_PREFIX):]
        try:
            row = json.loads(payload)
        except Exception:
            rows.append({"type": "parse_error", "raw": payload[:1000]})
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _native_entry_supports_log_arg(entry: Path) -> bool:
    try:
        text = entry.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "--log" in text and ("add_argument(\"--log\"" in text or "add_argument('--log'" in text)


def _safe_label_fragment(label: str) -> str:
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "_.-") else "_"
        for char in label
    )
    return safe[:80] or "bot"


async def _run_tcp_server_with_processes(
    bot_a: NativeBotSpec,
    bot_b: NativeBotSpec,
    *,
    hands: int,
    timeout_sec: float,
    deck_seed_base: int | None,
    bot_seed_base: int | None = None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    capture_events: bool = False,
) -> dict[str, Any]:
    clients: list[_TCPClient] = []
    connected = asyncio.Event()
    events: list[dict[str, Any]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if len(clients) >= 2:
            writer.close()
            await writer.wait_closed()
            return
        clients.append(_TCPClient(reader, writer))
        if len(clients) == 2:
            connected.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    run_labels = [bot_a.label, bot_b.label]
    if run_labels[0] == run_labels[1]:
        run_labels = [f"{run_labels[0]}_A", f"{run_labels[1]}_B"]
    procs: list[subprocess.Popen] = []
    proc_streams = []
    stdout_stderr: dict[str, dict[str, Any]] = {}
    log_temp_root = Path(tempfile.mkdtemp(prefix="pok_native_logs_"))
    bot_log_paths: dict[str, Path] = {}
    engine = None
    run_error = ""
    connect_timeout = max(1.0, min(20.0, float(timeout_sec) / 3.0))
    name_timeout = max(1.0, min(30.0, float(timeout_sec) / 3.0))
    action_timeout = max(1.0, min(60.0, float(timeout_sec)))
    process_drain_timeout = max(1.0, min(5.0, float(timeout_sec) / 6.0))
    bot_seeds: dict[str, int | None] = {}
    try:
        env_overrides = (bot_a_env_overrides or {}, bot_b_env_overrides or {})
        for idx, (spec, label) in enumerate(zip((bot_a, bot_b), run_labels)):
            env = os.environ.copy()
            env["POK_OFFICIAL_ACTION_DELAY"] = os.environ.get("POK_NATIVE_LOCAL_ACTION_DELAY", "0")
            env["PYTHONPATH"] = str(spec.path) + os.pathsep + env.get("PYTHONPATH", "")
            for key, value in env_overrides[idx].items():
                if value is None:
                    env.pop(str(key), None)
                else:
                    env[str(key)] = str(value)
            seed = _native_bot_seed(bot_seed_base, idx)
            bot_seeds[label] = seed
            if seed is None:
                cmd = [
                    sys.executable,
                    str(spec.entry),
                    "--host",
                    str(host),
                    "--port",
                    str(port),
                    "--name",
                    label,
                ]
            else:
                env["POK_NATIVE_ENTRY"] = str(spec.entry)
                env["POK_NATIVE_BOT_SEED"] = str(seed)
                env["PYTHONHASHSEED"] = str(seed % 4_294_967_295)
                cmd = [
                    sys.executable,
                    "-c",
                    SEEDED_NATIVE_LAUNCHER,
                    "--host",
                    str(host),
                    "--port",
                    str(port),
                    "--name",
                    label,
                ]
            if _native_entry_supports_log_arg(spec.entry):
                log_path = log_temp_root / f"{idx}_{_safe_label_fragment(label)}.log"
                cmd.extend(["--log", str(log_path)])
                bot_log_paths[label] = log_path
            stdout_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(spec.path),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    env=env,
                )
            except Exception:
                stdout_file.close()
                stderr_file.close()
                raise
            proc_streams.append((stdout_file, stderr_file))
            procs.append(proc)
        await asyncio.wait_for(connected.wait(), timeout=connect_timeout)
        await clients[0].send_line("name")
        await clients[1].send_line("name")
        name0 = await clients[0].recv_line(timeout=name_timeout)
        name1 = await clients[1].recv_line(timeout=name_timeout)
        if not name0 or not name1:
            raise RuntimeError("native TCP bot name handshake failed")
        clients[0].name = name0
        clients[1].name = name1
        ordered_clients = clients
        clients_by_name = {client.name: client for client in clients}
        if run_labels[0] in clients_by_name and run_labels[1] in clients_by_name:
            ordered_clients = [clients_by_name[run_labels[0]], clients_by_name[run_labels[1]]]
            if ordered_clients != clients:
                events.append({
                    "type": "client_order",
                    "order": list(run_labels),
                    "connection_order": [name0, name1],
                })
        engine = _LimitedTCPGameEngine(
            ordered_clients,
            events,
            deck_seed_base=deck_seed_base,
            action_timeout_sec=action_timeout,
        )
        await asyncio.wait_for(engine.run_limited_match(name0, name1, hands), timeout=timeout_sec)
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        try:
            server.close()
            for client in clients:
                await client.close(timeout=process_drain_timeout)
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=process_drain_timeout)
            except asyncio.TimeoutError:
                pass
            for label, proc, streams in zip(run_labels, procs, proc_streams):
                stdout_file, stderr_file = streams
                stderr_note = ""
                try:
                    proc.wait(timeout=process_drain_timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=process_drain_timeout)
                    except subprocess.TimeoutExpired:
                        stderr_note = "process did not exit after kill"
                stdout_file.seek(0)
                stderr_file.seek(0)
                out = stdout_file.read() or ""
                err = stderr_file.read() or ""
                if stderr_note:
                    err = (err + "\n" + stderr_note).strip()
                bot_log_text = ""
                bot_log_path = bot_log_paths.get(label)
                if bot_log_path is not None:
                    try:
                        bot_log_text = bot_log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError as exc:
                        err = (err + f"\nfailed to read bot log: {exc}").strip()
                stdout_file.close()
                stderr_file.close()
                stdout_stderr[label] = {
                    "returncode": proc.returncode,
                    "stdout": out or "",
                    "stderr": err or "",
                    "bot_log": bot_log_text,
                    "bot_log_supported": label in bot_log_paths,
                }
        finally:
            shutil.rmtree(log_temp_root, ignore_errors=True)

    illegal = {
        0: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 0 and str(e.get("action", "")).startswith("illegal:")),
        1: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 1 and str(e.get("action", "")).startswith("illegal:")),
    }
    timeouts = {
        0: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 0 and e.get("action") == "timeout"),
        1: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 1 and e.get("action") == "timeout"),
    }
    earnings = getattr(engine, "total_earnings", [0, 0]) if engine is not None else [0, 0]
    hands_played = int(getattr(engine, "hand_num", 0) or 0) if engine is not None else 0
    settlements = [
        {
            "hand": int(event.get("hand", 0) or 0),
            "earnings": [int(value) for value in event.get("earnings", [0, 0])],
            "pot": int(event.get("pot", 0) or 0),
            "is_showdown": bool(event.get("is_showdown", False)),
            "winner_idx": event.get("winner_idx"),
            "reason": event.get("reason", ""),
        }
        for event in events
        if event.get("type") == "settle"
    ]
    per_player = {}
    issues: list[str] = []
    if run_error:
        issues.append(f"native_tcp_match_error={run_error}")
    for idx, label in enumerate(run_labels):
        spec = (bot_a, bot_b)[idx]
        proc_info = stdout_stderr.get(label, {})
        proc_failed = bool(proc_info.get("returncode") not in (0, None))
        stdout_text = str(proc_info.get("stdout") or "")
        stderr_text = str(proc_info.get("stderr") or "")
        bot_log_text = str(proc_info.get("bot_log") or "")
        decision_trace = _parse_decision_trace(stderr_text)
        bot_log_summary = (
            _parse_native_bot_log(bot_log_text)
            if bot_log_text
            else _empty_bot_log_summary()
        )
        runtime_telemetry = {
            "schema_version": 1,
            "server_action_latency": _server_action_latency(events, idx),
            "bot_log_supported": bool(proc_info.get("bot_log_supported")),
            "bot_log": bot_log_summary,
            "trace_decision_count": len(decision_trace),
        }
        per_player[label] = {
            "earnings": int(earnings[idx]),
            "illegal_actions": illegal[idx],
            "timeouts": timeouts[idx],
            "wrapper_used": spec.wrapper_used,
            "runtime_telemetry": runtime_telemetry,
            "native": {
                "returncode": proc_info.get("returncode"),
                "bot_seed": bot_seeds.get(label),
                "stdout_tail": stdout_text[-2000:] if stdout_text else "",
                "stderr_tail": stderr_text[-2000:] if stderr_text else "",
                "bot_log_supported": bool(proc_info.get("bot_log_supported")),
                "decision_trace": decision_trace,
                "process_failures": 1 if proc_failed else 0,
                "json_response_stdout": 1 if '"response"' in stdout_text or "'response'" in stdout_text else 0,
            },
            "adapter": {
                "bot_failures": 0,
                "invalid_actions": 0,
                "actions_sent": 0,
                "clamped_raises": 0,
                "allin_conversions": 0,
                "would_be_illegal_raise": 0,
                "postflop_pass_conversions": 0,
            },
        }
        if illegal[idx]:
            issues.append(f"{label}: illegal_actions={illegal[idx]}")
        if timeouts[idx]:
            issues.append(f"{label}: timeouts={timeouts[idx]}")
        if proc_failed:
            issues.append(f"{label}: native_process_returncode={proc_info.get('returncode')}")
        if per_player[label]["native"]["json_response_stdout"]:
            issues.append(f"{label}: json_response_stdout")
    if hands_played != hands:
        issues.append(f"hands_played={hands_played}, expected={hands}")
    return {
        "bot_a": run_labels[0],
        "bot_b": run_labels[1],
        "hands_requested": hands,
        "hands_played": hands_played,
        "per_player": per_player,
        "net_chips_a": int(earnings[0]),
        "net_chips_b": int(earnings[1]),
        "net_chips_a_per_hand": round(int(earnings[0]) / max(1, hands_played), 3),
        "execution_mode": "native_tcp",
        "wrapper_used": bot_a.wrapper_used or bot_b.wrapper_used,
        "wrapper_used_by_player": {
            run_labels[0]: bot_a.wrapper_used,
            run_labels[1]: bot_b.wrapper_used,
        },
        "deck_seed_base": deck_seed_base,
        "bot_seed_base": bot_seed_base,
        "settlements": settlements,
        "passed_compliance": not issues,
        "issues": issues,
        "events_tail": events[-20:],
        **({"events": list(events)} if capture_events else {}),
    }


async def run_native_tcp_pair(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    require_native_a: bool = True,
    require_native_b: bool = True,
    deck_seed_base: int | None = None,
    bot_seed_base: int | None = None,
    timeout_sec: float | None = None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    capture_events: bool = False,
) -> dict[str, Any]:
    """Run a formal native TCP match using both bots' existing entries.

    The ``require_native_*`` arguments are retained so formal callers can state
    the contract explicitly. They cannot be disabled; legacy wrapper generation
    is available only through ``run_legacy_debug_tcp_pair_with_wrappers``.
    """
    if require_native_a is not True or require_native_b is not True:
        raise ValueError(
            "run_native_tcp_pair requires existing valid national_bot.py entries "
            "for both players; use run_legacy_debug_tcp_pair_with_wrappers only "
            "for legacy/debug regression"
        )
    return await _run_native_tcp_pair(
        bot_a_token,
        bot_b_token,
        hands,
        allow_legacy_wrappers=False,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
        bot_a_env_overrides=bot_a_env_overrides,
        bot_b_env_overrides=bot_b_env_overrides,
        capture_events=capture_events,
    )


async def run_legacy_debug_tcp_pair_with_wrappers(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    deck_seed_base: int | None = None,
    bot_seed_base: int | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Run an old regression match, wrapping missing/invalid native entries.

    This API is intentionally named for legacy/debug use. Any generated entry is
    written only to a temporary copy and reported through ``wrapper_used``.
    """
    return await _run_native_tcp_pair(
        bot_a_token,
        bot_b_token,
        hands,
        allow_legacy_wrappers=True,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
    )


async def _run_native_tcp_pair(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    allow_legacy_wrappers: bool,
    deck_seed_base: int | None,
    bot_seed_base: int | None,
    timeout_sec: float | None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    capture_events: bool = False,
) -> dict[str, Any]:
    label_a, dir_a = resolve_bot(bot_a_token)
    label_b, dir_b = resolve_bot(bot_b_token)
    specs: list[NativeBotSpec] = []
    try:
        specs.append(_prepare_native_spec(
            label_a,
            dir_a,
            allow_legacy_wrapper=allow_legacy_wrappers,
        ))
        specs.append(_prepare_native_spec(
            label_b,
            dir_b,
            allow_legacy_wrapper=allow_legacy_wrappers,
        ))
    except Exception:
        _cleanup_specs(specs)
        raise
    hands = max(1, min(70, int(hands)))
    if timeout_sec is None:
        timeout_sec = max(90.0, hands * 4.0)
    try:
        return await _run_tcp_server_with_processes(
            specs[0],
            specs[1],
            hands=hands,
            timeout_sec=float(timeout_sec),
            deck_seed_base=deck_seed_base,
            bot_seed_base=bot_seed_base,
            bot_a_env_overrides=bot_a_env_overrides,
            bot_b_env_overrides=bot_b_env_overrides,
            capture_events=capture_events,
        )
    finally:
        _cleanup_specs(specs)


async def run_native_tcp_smoke(
    candidate_token: str | Path,
    *,
    source_v: int | None = None,
    opponent_token: str | Path | None = None,
    hands: int = 1,
    timeout_sec: float | None = 90.0,
) -> dict[str, Any]:
    """Run a minimal direct-TCP national smoke match for a candidate bot."""
    hands = max(1, min(70, int(hands)))
    try:
        candidate_label, candidate_dir = resolve_bot(candidate_token)
    except Exception as exc:
        return {
            "passed": False,
            "execution_mode": "native_tcp",
            "wrapper_used": False,
            "hands": hands,
            "issues": [f"native_smoke_candidate_error={type(exc).__name__}: {str(exc)[:300]}"],
        }

    if opponent_token is not None:
        try:
            opponents = [resolve_bot(opponent_token)]
        except Exception as exc:
            return {
                "candidate": candidate_label,
                "passed": False,
                "execution_mode": "native_tcp",
                "wrapper_used": False,
                "hands": hands,
                "issues": [f"native_smoke_opponent_error={type(exc).__name__}: {str(exc)[:300]}"],
            }
    else:
        opponents = select_acceptance_opponents(candidate_label, source_v, limit=1)

    if not opponents:
        return {
            "candidate": candidate_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "wrapper_used": False,
            "hands": hands,
            "issues": ["native_smoke_no_opponent"],
        }

    opponent_label, opponent_dir = opponents[0]
    try:
        result = await run_native_tcp_pair(
            candidate_dir,
            opponent_dir,
            hands,
            require_native_a=True,
            require_native_b=True,
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        return {
            "candidate": candidate_label,
            "opponent": opponent_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "wrapper_used": False,
            "hands": hands,
            "issues": [f"native_smoke_exception={type(exc).__name__}: {str(exc)[:500]}"],
        }

    issues = list(result.get("issues") or [])
    if not result.get("passed_compliance") and not issues:
        issues.append("native_smoke_compliance_failed")
    passed = bool(result.get("passed_compliance")) and not issues
    return {
        "candidate": candidate_label,
        "opponent": opponent_label,
        "passed": passed,
        "execution_mode": "native_tcp",
        "wrapper_used": bool(result.get("wrapper_used")),
        "hands": hands,
        "issues": issues,
        "result": result,
    }


def _summary_from_results(bots: list[tuple[str, Path]], results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    runtime_rows: dict[str, list[dict[str, Any]]] = {label: [] for label, _ in bots}
    summary = {
        label: {
            "matches": 0,
            "net_chips": 0,
            "illegal_actions": 0,
            "timeouts": 0,
            "bot_failures": 0,
            "invalid_actions": 0,
            "clamped_raises": 0,
            "allin_conversions": 0,
            "would_be_illegal_raise": 0,
            "postflop_pass_conversions": 0,
            "native_process_failures": 0,
            "json_response_stdout": 0,
            "wrapper_used": False,
            "passed_compliance": True,
            "runtime_telemetry": _empty_runtime_telemetry(),
        }
        for label, _ in bots
    }
    for result in results:
        for label, pdata in result["per_player"].items():
            row = summary[label]
            row["matches"] += 1
            row["net_chips"] += int(pdata.get("earnings", 0) or 0)
            row["illegal_actions"] += int(pdata.get("illegal_actions", 0) or 0)
            row["timeouts"] += int(pdata.get("timeouts", 0) or 0)
            runtime_rows.setdefault(label, []).append(pdata.get("runtime_telemetry", {}) or {})
            native = pdata.get("native", {}) or {}
            row["native_process_failures"] += int(native.get("process_failures", 0) or 0)
            row["json_response_stdout"] += int(native.get("json_response_stdout", 0) or 0)
            row["wrapper_used"] = row["wrapper_used"] or bool(pdata.get("wrapper_used"))
            row["passed_compliance"] = row["passed_compliance"] and result.get("passed_compliance", False)
    for label, rows in runtime_rows.items():
        if label in summary:
            summary[label]["runtime_telemetry"] = _merge_runtime_telemetry(rows)
    return summary


async def run_native_acceptance_for_candidate(
    candidate_token: str | Path,
    *,
    source_v: int | None = None,
    opponent_tokens: list[str | Path] | None = None,
    hands: int = 70,
    max_opponents: int = 2,
    timeout_sec: float | None = None,
) -> NationalAcceptanceResult:
    candidate = resolve_bot(candidate_token)
    if opponent_tokens:
        opponents = [resolve_bot(token) for token in opponent_tokens]
    else:
        opponents = select_acceptance_opponents(candidate[0], source_v, limit=max_opponents)
    bots = [candidate] + [opp for opp in opponents if opp[0] != candidate[0]]
    if len(bots) < 2:
        return NationalAcceptanceResult(
            candidate=candidate[0],
            opponents=[],
            hands_per_pair=hands,
            passed=False,
            issues=["need at least one opponent for native national acceptance"],
            summary={"wrapper_used": False, "passed_compliance": False},
            report={"execution_mode": "native_tcp", "wrapper_used": False},
        )
    pair_indices = [(0, idx) for idx in range(1, len(bots))]
    if timeout_sec is None:
        timeout_sec = max(180.0, float(hands * len(pair_indices) * 5))

    results: list[dict[str, Any]] = []
    try:
        for i, j in pair_indices:
            pair_seed = None
            results.append(await run_native_tcp_pair(
                bots[i][1],
                bots[j][1],
                hands,
                require_native_a=True,
                require_native_b=True,
                deck_seed_base=pair_seed,
                timeout_sec=timeout_sec,
            ))
    except TimeoutError:
        issue = f"native_national_acceptance_timeout: exceeded {timeout_sec:g}s"
        return NationalAcceptanceResult(
            candidate=candidate[0],
            opponents=[opp[0] for opp in bots[1:]],
            hands_per_pair=hands,
            passed=False,
            issues=[issue],
            summary={
                "matches": 0,
                "net_chips": 0,
                "wrapper_used": False,
                "passed_compliance": False,
            },
            report={
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "hands_per_pair": hands,
                "execution_mode": "native_tcp",
                "wrapper_used": False,
                "candidate_only": True,
                "timeout_sec": timeout_sec,
                "timed_out": True,
                "issues": [issue],
            },
        )

    summary = _summary_from_results(bots, results)
    matrix: dict[str, dict[str, Any]] = {label: {} for label, _ in bots}
    for result in results:
        a = result["bot_a"]
        b = result["bot_b"]
        matrix[a][b] = {
            "net_chips": result["net_chips_a"],
            "per_hand": result["net_chips_a_per_hand"],
            "passed_compliance": result["passed_compliance"],
            "wrapper_used": bool(result.get("wrapper_used")),
            "issues": result["issues"],
        }
        matrix[b][a] = {
            "net_chips": result["net_chips_b"],
            "per_hand": round(result["net_chips_b"] / max(1, result["hands_played"]), 3),
            "passed_compliance": result["passed_compliance"],
            "wrapper_used": bool(result.get("wrapper_used")),
            "issues": result["issues"],
        }
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hands_per_pair": hands,
        "execution_mode": "native_tcp",
        "wrapper_used": any(bool(result.get("wrapper_used")) for result in results),
        "pair_count": len(pair_indices),
        "bots": [{"label": label, "path": str(path)} for label, path in bots],
        "results": results,
        "summary": summary,
        "matrix": matrix,
        "candidate_only": True,
        "timeout_sec": timeout_sec,
    }
    candidate_summary = summary.get(candidate[0], {})
    issues: list[str] = []
    for result in results:
        if result["bot_a"] == candidate[0] or result["bot_b"] == candidate[0]:
            issues.extend(result.get("issues", []))
    return NationalAcceptanceResult(
        candidate=candidate[0],
        opponents=[opp[0] for opp in bots[1:]],
        hands_per_pair=hands,
        passed=bool(candidate_summary.get("passed_compliance")) and not issues,
        issues=issues,
        summary=candidate_summary,
        matrix=matrix.get(candidate[0], {}),
        report=report,
    )


def _mean(values: list[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _ci(values: list[int]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return paired_bootstrap_ci(values)


async def run_native_precommit(
    candidate_token: str | Path,
    opponents: list[dict[str, Any]],
    *,
    hands: int = 70,
    matches_per_opponent: int = 1,
    parent_label: str = "",
    deck_seed_base: int | None = 91_000,
    parent_loss_threshold: float = -2000,
    aggregate_loss_threshold: float = -2000,
) -> dict[str, Any]:
    candidate = resolve_bot(candidate_token)
    hands = max(1, min(70, int(hands)))
    matches_per_opponent = max(1, int(matches_per_opponent))
    matchups: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    aggregate_net_chips: list[int] = []
    total_wins = total_losses = total_draws = 0
    resolved_opponents: list[dict[str, Any]] = []
    if not opponents:
        blockers.append({"reason": "native_no_opponents", "details": "Native precommit requires at least one opponent."})
    for opp_index, item in enumerate(opponents):
        reason = str(item.get("reason") or "precommit")
        token = item.get("path") or item.get("token") or item.get("name")
        opponent = resolve_bot(token)
        resolved_opponents.append({"name": item.get("name") or opponent[0], "reason": reason, "path": str(opponent[1])})
        samples: list[int] = []
        repeats: list[dict[str, Any]] = []
        candidate_issues: list[str] = []
        opponent_issues: list[str] = []
        hands_played_total = 0
        for repeat in range(matches_per_opponent):
            seed = None if deck_seed_base is None else int(deck_seed_base) + (opp_index * 100_000) + (repeat * 1_000)
            result = await run_native_tcp_pair(
                candidate[1],
                opponent[1],
                hands,
                require_native_a=True,
                require_native_b=True,
                deck_seed_base=seed,
            )
            net = int(result.get("net_chips_a", 0) or 0)
            samples.append(net)
            aggregate_net_chips.append(net)
            hands_played = int(result.get("hands_played", 0) or 0)
            hands_played_total += hands_played
            c_issues = [
                str(issue)
                for issue in result.get("issues", [])
                if str(issue).startswith(candidate[0] + ":") or str(issue).startswith("hands_played=")
            ]
            o_issues = [
                str(issue)
                for issue in result.get("issues", [])
                if not (str(issue).startswith(candidate[0] + ":") or str(issue).startswith("hands_played="))
            ]
            candidate_issues.extend(c_issues)
            opponent_issues.extend(o_issues)
            repeats.append({
                "repeat": repeat + 1,
                "deck_seed_base": seed,
                "hands_played": hands_played,
                "net_chips": net,
                "candidate_issues": c_issues,
                "opponent_issues": o_issues,
                "raw": result,
            })
        wins = sum(1 for value in samples if value > 0)
        losses = sum(1 for value in samples if value < 0)
        draws = sum(1 for value in samples if value == 0)
        total_wins += wins
        total_losses += losses
        total_draws += draws
        mean = _mean(samples)
        ci_lo, ci_hi = _ci(samples)
        matchup = {
            "opponent": item.get("name") or opponent[0],
            "reason": reason,
            "protocol": "national_native_tcp",
            "hands_per_match": hands,
            "matches": matches_per_opponent,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "n_played": matches_per_opponent,
            "hands_played_total": hands_played_total,
            "net_chips": samples,
            "net_chips_mean": _rounded(mean),
            "net_chip_ci": [_rounded(ci_lo), _rounded(ci_hi)],
            "candidate_compliance_issues": candidate_issues,
            "opponent_compliance_issues": opponent_issues,
            "wrapper_used": any(bool(row["raw"].get("wrapper_used")) for row in repeats),
            "repeats": repeats,
        }
        matchups.append(matchup)
        if candidate_issues:
            blockers.append({"reason": "native_candidate_compliance", "opponent": matchup["opponent"], "details": "; ".join(candidate_issues[:5])})
        if hands_played_total < hands * matches_per_opponent:
            blockers.append({"reason": "native_incomplete_match", "opponent": matchup["opponent"], "details": f"{hands_played_total}/{hands * matches_per_opponent} hands completed"})
        if parent_label and matchup["opponent"] == parent_label and mean is not None and mean < parent_loss_threshold:
            blockers.append({"reason": "lost_to_parent", "opponent": matchup["opponent"], "details": f"Native national mean net chips {mean:.0f} below {parent_loss_threshold:.0f}; samples={samples}"})
    agg_mean = _mean(aggregate_net_chips)
    agg_ci_lower, agg_ci_upper = _ci(aggregate_net_chips)
    if not aggregate_net_chips:
        blockers.append({"reason": "native_no_samples", "details": "Native precommit produced zero completed match samples."})
    if agg_mean is not None and agg_mean < aggregate_loss_threshold:
        blockers.append({"reason": "aggregate_native_regression", "details": f"Aggregate native national mean net chips {agg_mean:.0f} below {aggregate_loss_threshold:.0f}; samples={len(aggregate_net_chips)}"})
    paired_payload = {
        "protocol": "national_native_tcp",
        "hands_per_match": hands,
        "matches_per_opponent": matches_per_opponent,
        "aggregate_ci_lower": _rounded(agg_ci_lower),
        "aggregate_ci_upper": _rounded(agg_ci_upper),
        "aggregate_threshold": aggregate_loss_threshold,
        "aggregate_gate_bound": _rounded(agg_mean),
        "aggregate_gate_rule": "block_if_mean_below_threshold",
        "net_chips_samples": len(aggregate_net_chips),
        "gate_degraded": len(aggregate_net_chips) < 2,
        "net_chips_mean": _rounded(agg_mean),
        "net_chips_std": round(statistics.pstdev(aggregate_net_chips), 1) if len(aggregate_net_chips) > 1 else None,
        "net_chips_min": min(aggregate_net_chips) if aggregate_net_chips else None,
        "net_chips_max": max(aggregate_net_chips) if aggregate_net_chips else None,
        "parent_loss_threshold": parent_loss_threshold,
    }
    return {
        "evaluation_protocol": "national_native_tcp",
        "candidate": candidate[0],
        "candidate_path": str(candidate[1]),
        "opponents": resolved_opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "aggregate_net_chips": aggregate_net_chips,
        "paired_bootstrap": paired_payload,
        "wrapper_used": any(bool(matchup.get("wrapper_used")) for matchup in matchups),
        "blockers": blockers,
        "passed": not blockers,
    }
