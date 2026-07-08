#!/usr/bin/env python3
"""Deterministic national-protocol client for official EXE diagnostics.

This script is intentionally simple and strategy-free. It lets the official
wire probe drive fixed action paths against the Windows platform so protocol
edge cases can be isolated from bot decision logic.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import socket
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "web" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from official_wire_probe import split_server_messages  # noqa: E402


CHECK_CALL_DOWN = "check_call_down"
PREFLOP_RAISE_FOLD = "preflop_raise_fold"
FLOP_BET_FOLD = "flop_bet_fold"
FLOP_BET_CALL_TURN_BET_CALL = "flop_bet_call_turn_bet_call"
FLOP_CHECK_SB_BET_BB_FOLD = "flop_check_sb_bet_bb_fold"

SCENARIOS = {
    CHECK_CALL_DOWN,
    PREFLOP_RAISE_FOLD,
    FLOP_BET_FOLD,
    FLOP_BET_CALL_TURN_BET_CALL,
    FLOP_CHECK_SB_BET_BB_FOLD,
}


class ScriptedClient:
    def __init__(self, *, scenario: str, name: str, log_path: Path | None, action_delay: float) -> None:
        self.scenario = scenario
        self.name = name
        self.log_path = log_path
        self.action_delay = max(0.0, float(action_delay))
        self.stage = ""
        self.is_small_blind = False
        self.hand = 0
        self.buffer = ""
        self.street_opened = False
        self.closed = False
        self._log_fp = log_path.open("a", encoding="utf-8", buffering=1) if log_path else None

    def close(self) -> None:
        if self._log_fp is not None:
            self._log_fp.close()

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        if self._log_fp is not None:
            self._log_fp.write(line + "\n")
        else:
            print(line, file=sys.stderr)

    def send(self, sock: socket.socket, message: str, *, reason: str) -> None:
        if reason != "name_handshake" and self.action_delay > 0:
            time.sleep(self.action_delay)
        sock.sendall(message.encode("utf-8"))
        self.log(f"SEND msg={message!r} reason={reason} hand={self.hand} stage={self.stage}")

    def dispatch_raw(self, sock: socket.socket, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace")
        self.buffer += text
        messages, self.buffer = split_server_messages(self.buffer)
        if self.buffer:
            self.log(f"BUFFER remaining={self.buffer!r}")
        for message in messages:
            self.dispatch(sock, message)

    def dispatch(self, sock: socket.socket, message: str) -> None:
        self.log(f"RECV line={message!r} hand={self.hand} stage={self.stage}")
        if message == "name":
            self.send(sock, self.name, reason="name_handshake")
            return
        if message.startswith("preflop|"):
            self._start_hand(message)
            if self.is_small_blind:
                if self.scenario == PREFLOP_RAISE_FOLD:
                    self.send(sock, "raise 200", reason="sb_open_raise")
                else:
                    self.send(sock, "call", reason="sb_limp")
            return
        if message.startswith(("flop|", "turn|", "river|")):
            self._start_street(message)
            if not self.is_small_blind:
                action = self._bb_first_postflop_action()
                self.send(sock, action, reason="bb_first_postflop")
            return
        if message.startswith("earnChips"):
            self.log(f"SETTLE {message!r}")
            return
        if message.startswith("oppo_hands|"):
            return
        self._respond_to_action(sock, message)

    def _start_hand(self, message: str) -> None:
        parts = message.split("|", 2)
        blind = parts[1] if len(parts) > 1 else ""
        self.hand += 1
        self.stage = "preflop"
        self.is_small_blind = blind == "SMALLBLIND"
        self.street_opened = False
        self.closed = False

    def _start_street(self, message: str) -> None:
        self.stage = message.split("|", 1)[0]
        self.street_opened = False
        self.closed = False

    def _bb_first_postflop_action(self) -> str:
        self.street_opened = True
        if self.scenario in {FLOP_BET_FOLD, FLOP_BET_CALL_TURN_BET_CALL}:
            if self.stage in {"flop", "turn"}:
                return "raise 100"
            return "check"
        return "check"

    def _respond_to_action(self, sock: socket.socket, message: str) -> None:
        if self.stage == "preflop":
            self._respond_preflop(sock, message)
            return
        self._respond_postflop(sock, message)

    def _respond_preflop(self, sock: socket.socket, message: str) -> None:
        if not self.is_small_blind and message == "call":
            self.send(sock, "check", reason="bb_check_after_limp")
            return
        if not self.is_small_blind and message.startswith("raise "):
            if self.scenario == PREFLOP_RAISE_FOLD:
                self.send(sock, "fold", reason="bb_fold_to_open_raise")
            else:
                self.send(sock, "call", reason="bb_call_preflop_raise")
            return
        if self.is_small_blind and message.startswith("raise "):
            self.send(sock, "call", reason="sb_call_preflop_raise")

    def _respond_postflop(self, sock: socket.socket, message: str) -> None:
        if message == "check":
            if self.is_small_blind and self.scenario == FLOP_CHECK_SB_BET_BB_FOLD and self.stage == "flop":
                self.send(sock, "raise 100", reason="sb_bet_after_bb_check")
            elif self.is_small_blind:
                self.send(sock, "call", reason="sb_pass_after_check")
            return
        if message.startswith("raise "):
            if self.is_small_blind and self.scenario == FLOP_BET_FOLD and self.stage == "flop":
                self.send(sock, "fold", reason="sb_fold_to_flop_bet")
            elif not self.is_small_blind and self.scenario == FLOP_CHECK_SB_BET_BB_FOLD and self.stage == "flop":
                self.send(sock, "fold", reason="bb_fold_to_sb_bet")
            else:
                self.send(sock, "call", reason="call_scripted_raise")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", default="Scripted")
    parser.add_argument("--seat", default="auto")
    parser.add_argument("--log")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default=os.environ.get("POK_SCENARIO", CHECK_CALL_DOWN))
    parser.add_argument("--action-delay", type=float, default=float(os.environ.get("POK_SCRIPTED_ACTION_DELAY", "0")))
    parser.add_argument("--idle-timeout", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = ScriptedClient(
        scenario=args.scenario,
        name=args.name,
        log_path=Path(args.log).expanduser() if args.log else None,
        action_delay=args.action_delay,
    )
    try:
        with socket.create_connection((args.host, args.port), timeout=20.0) as sock:
            sock.settimeout(1.0)
            last_rx = time.time()
            while time.time() - last_rx <= args.idle_timeout:
                try:
                    raw = sock.recv(4096)
                except socket.timeout:
                    continue
                if not raw:
                    break
                last_rx = time.time()
                client.dispatch_raw(sock, raw)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
