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
from bot_namespace import ACTIVE_BOT_PREFIX, active_bot_glob, bot_name, parse_bot_version, version_sort_key
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
TCP server, maintains line-protocol state, calls the local strategy in process,
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


def _tcp_card_to_int(suit: int, rank: int) -> int:
    return rank * 4 + TCP_TO_JUDGE_SUIT[suit]


def _parse_cards(text: str) -> list[int]:
    return [_tcp_card_to_int(int(s), int(r)) for s, r in CARD_RE.findall(text)]


def _parse_action(raw: str) -> tuple[str, int | None]:
    if raw.startswith("raise ") and raw[6:].isdigit() and raw.count(" ") == 1:
        return "raise", int(raw.split(" ", 1)[1])
    if raw in {"call", "check", "fold", "allin"}:
        return raw, None
    return "unknown", None


class NativeNationalBot:
    def __init__(self, name: str):
        self.name = name
        from main import sanitize_action
        from state import infer_remaining_hands_from_requests, reconstruct_state
        from strategy import get_action

        self.get_action = get_action
        self.reconstruct_state = reconstruct_state
        self.infer_remaining_hands = infer_remaining_hands_from_requests
        self.sanitize_action = sanitize_action
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
            action_val = amount
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

    def _zero_action(self) -> tuple[str, str, int | None]:
        if self._opponent_stage_bet > self._my_stage_bet:
            return "call", "call", None
        if self._stage == "preflop":
            return ("call", "call", None) if self._is_sb else ("check", "check", None)
        opp_acted = any(h.get("round") == self._round_num() and h.get("player_id") == self._opponent_id for h in self._history)
        return ("call", "call", None) if opp_acted else ("check", "check", None)

    def _action_to_tcp(self, action: int) -> tuple[str, str, int | None]:
        if action == -1:
            return "fold", "fold", None
        if action == -2:
            if self._opponent_chips == 0 and self._opponent_stage_bet > self._my_stage_bet:
                return "call", "call", None
            return "allin", "allin", None
        if action > 0:
            needed = action - self._my_stage_bet
            if needed >= self._my_chips:
                return "allin", "allin", None
            if needed <= 0:
                return self._zero_action()
            return f"raise {action}", "raise", action
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

    def _send_decision(self, sock: socket.socket) -> None:
        try:
            action = self._strategy_action()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            action = -1
        self._responses.append(int(action))
        msg, action_type, amount = self._action_to_tcp(int(action))
        sock.sendall((msg + "\n").encode("utf-8"))
        committed = self._apply_my_action(action_type, amount)
        self._record_action(self._my_id, action_type, amount, committed)
        if action_type == "call" and (self._current_round_has_allin() or self._my_chips == 0 or self._opponent_chips == 0):
            self._in_allin_runout = True
        self._my_action_count += 1

    def handle(self, line: str, sock: socket.socket) -> None:
        if line == "name":
            sock.sendall((self.name + "\n").encode("utf-8"))
            return
        if line.startswith("preflop|"):
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
        if line.startswith(("flop|", "turn|", "river|")):
            stage, cards = line.split("|", 1)
            self._stage = stage
            self._public_cards.extend(_parse_cards(cards))
            self._my_action_count = 0
            self._my_stage_bet = 0
            self._opponent_stage_bet = 0
            if not self._in_allin_runout and not self._is_sb:
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
        committed = self._apply_opponent_action(action_type, amount)
        self._record_action(self._opponent_id, action_type, amount, committed)
        if action_type == "call" and (self._current_round_has_allin() or self._my_chips == 0 or self._opponent_chips == 0):
            self._in_allin_runout = True
        if self._should_respond(action_type):
            self._send_decision(sock)


def run_client(host: str, port: int, name: str) -> int:
    bot = NativeNationalBot(name)
    with socket.create_connection((host, port), timeout=30) as sock:
        sock.settimeout(180)
        stream = sock.makefile("r", encoding="utf-8", newline="\n")
        while True:
            line = stream.readline()
            if not line:
                return 0
            bot.handle(line.rstrip("\r\n"), sock)


def main() -> int:
    parser = argparse.ArgumentParser(description="Native national TCP bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="Bot")
    args = parser.parse_args()
    try:
        return run_client(args.host, args.port, args.name)
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
                while "\n" not in self._buffer:
                    chunk = await self.reader.read(4096)
                    if not chunk:
                        self.closed = True
                        return None
                    self._buffer += chunk.decode("utf-8")
            line, self._buffer = self._buffer.split("\n", 1)
            return line.rstrip("\r")
        except (asyncio.TimeoutError, ConnectionError, OSError):
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
    required = ("socket", "raise ", "fold", "call", "check", "allin")
    for token in required:
        if token not in text:
            errors.append(f"{NATIVE_ENTRY}: missing native TCP token {token!r}")
    if _strategy_action_has_exception_pass(text):
        errors.append(
            f"{NATIVE_ENTRY}: _strategy_action must not continue with raw action after sanitizer failure"
        )
    return errors


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
    specs: list[tuple[str, Path]] = []
    for path in (ROOT / "bots").glob(active_bot_glob()):
        if path.is_dir() and (path / "main.py").exists() and (path / ".completed").exists():
            specs.append((path.name, path.resolve()))
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


def _prepare_native_spec(label: str, bot_dir: Path, *, require_existing: bool) -> NativeBotSpec:
    entry = bot_dir / NATIVE_ENTRY
    if entry.exists():
        if require_existing or not check_native_contract(bot_dir):
            return NativeBotSpec(label=label, path=bot_dir, entry=entry)
    if require_existing:
        raise ValueError(f"{label}: missing required {NATIVE_ENTRY}")
    tmp = Path(tempfile.mkdtemp(prefix=f"pok_native_{label}_"))
    dst = tmp / bot_dir.name
    shutil.copytree(bot_dir, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return NativeBotSpec(label=label, path=dst, entry=ensure_native_entry(dst, overwrite=True), temp_root=tmp)


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


async def _run_tcp_server_with_processes(
    bot_a: NativeBotSpec,
    bot_b: NativeBotSpec,
    *,
    hands: int,
    timeout_sec: float,
    deck_seed_base: int | None,
    bot_seed_base: int | None = None,
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
    stdout_stderr: dict[str, dict[str, str | int | None]] = {}
    engine = None
    run_error = ""
    connect_timeout = max(1.0, min(20.0, float(timeout_sec) / 3.0))
    name_timeout = max(1.0, min(30.0, float(timeout_sec) / 3.0))
    action_timeout = max(1.0, min(60.0, float(timeout_sec)))
    process_drain_timeout = max(1.0, min(5.0, float(timeout_sec) / 6.0))
    bot_seeds: dict[str, int | None] = {}
    try:
        for idx, (spec, label) in enumerate(zip((bot_a, bot_b), run_labels)):
            env = os.environ.copy()
            env["PYTHONPATH"] = str(spec.path) + os.pathsep + env.get("PYTHONPATH", "")
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
            stdout_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            proc_streams.append((stdout_file, stderr_file))
            procs.append(subprocess.Popen(
                cmd,
                cwd=str(spec.path),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                env=env,
            ))
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
            stdout_file.close()
            stderr_file.close()
            stdout_stderr[label] = {
                "returncode": proc.returncode,
                "stdout": out or "",
                "stderr": err or "",
            }

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
        proc_info = stdout_stderr.get(label, {})
        proc_failed = bool(proc_info.get("returncode") not in (0, None))
        stdout_text = str(proc_info.get("stdout") or "")
        stderr_text = str(proc_info.get("stderr") or "")
        decision_trace = _parse_decision_trace(stderr_text)
        per_player[label] = {
            "earnings": int(earnings[idx]),
            "illegal_actions": illegal[idx],
            "timeouts": timeouts[idx],
            "native": {
                "returncode": proc_info.get("returncode"),
                "bot_seed": bot_seeds.get(label),
                "stdout_tail": stdout_text[-2000:] if stdout_text else "",
                "stderr_tail": stderr_text[-2000:] if stderr_text else "",
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
        "deck_seed_base": deck_seed_base,
        "bot_seed_base": bot_seed_base,
        "settlements": settlements,
        "passed_compliance": not issues,
        "issues": issues,
        "events_tail": events[-20:],
    }


async def run_native_tcp_pair(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    require_native_a: bool = True,
    require_native_b: bool = False,
    deck_seed_base: int | None = None,
    bot_seed_base: int | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    label_a, dir_a = resolve_bot(bot_a_token)
    label_b, dir_b = resolve_bot(bot_b_token)
    specs = [
        _prepare_native_spec(label_a, dir_a, require_existing=require_native_a),
        _prepare_native_spec(label_b, dir_b, require_existing=require_native_b),
    ]
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
            require_native_b=False,
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        return {
            "candidate": candidate_label,
            "opponent": opponent_label,
            "passed": False,
            "execution_mode": "native_tcp",
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
        "hands": hands,
        "issues": issues,
        "result": result,
    }


def _summary_from_results(bots: list[tuple[str, Path]], results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
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
            "passed_compliance": True,
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
            native = pdata.get("native", {}) or {}
            row["native_process_failures"] += int(native.get("process_failures", 0) or 0)
            row["json_response_stdout"] += int(native.get("json_response_stdout", 0) or 0)
            row["passed_compliance"] = row["passed_compliance"] and result.get("passed_compliance", False)
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
                require_native_a=(i == 0),
                require_native_b=False,
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
            summary={"matches": 0, "net_chips": 0, "passed_compliance": False},
            report={
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "hands_per_pair": hands,
                "execution_mode": "native_tcp",
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
            "issues": result["issues"],
        }
        matrix[b][a] = {
            "net_chips": result["net_chips_b"],
            "per_hand": round(result["net_chips_b"] / max(1, result["hands_played"]), 3),
            "passed_compliance": result["passed_compliance"],
            "issues": result["issues"],
        }
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hands_per_pair": hands,
        "execution_mode": "native_tcp",
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
                require_native_b=False,
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
        "blockers": blockers,
        "passed": not blockers,
    }
