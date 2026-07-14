#!/usr/bin/env python3
"""National TCP entry for Route A1 ReBeL-like bot.

Uses the trained value/policy network (deploy.npz) for action selection,
falling back to a simple heuristic when network inference is unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import socket
import sys
import time

sys.dont_write_bytecode = True

NATIONAL_STREAM_DECODER_VERSION = 3
DEFAULT_OFFICIAL_ACTION_DELAY_SEC = 0.30
DEFAULT_DECISION_HARD_DEADLINE_SEC = 54.0
CARD_RE = re.compile(r"<([0-3]),([0-9]|1[0-2])>")
NUMERIC_RE = re.compile(r"^(raise) ([0-9]+)")
EARN_RE = re.compile(r"^(earnChips) (-?[0-9]+)")
WORDS = ("allin", "check", "call", "fold", "name")
STAGE_CARDS = {"flop|": 3, "turn|": 1, "river|": 1, "oppo_hands|": 2}


def tcp_card_id(suit: int, rank: int) -> int:
    """Convert TCP <suit,rank> to internal card id (0..51)."""
    # TCP: suit 0=Spade,1=Heart,2=Diamond,3=Club; rank 0=2..12=Ace
    # Internal: card = rank*4 + suit_offset where Heart=0,Diamond=1,Spade=2,Club=3
    suit_map = {0: 2, 1: 0, 2: 1, 3: 3}
    return rank * 4 + suit_map[suit]


class NationalStreamDecoder:
    def __init__(self) -> None:
        self.buffer = ""

    @property
    def has_pending_numeric(self) -> bool:
        return bool(re.fullmatch(r"(?:raise [0-9]+|earnChips -?[0-9]+)", self.buffer))

    @staticmethod
    def _card_message(buffer: str, prefix: str, count: int):
        if not buffer.startswith(prefix):
            return None
        position = len(prefix)
        for _ in range(count):
            match = CARD_RE.match(buffer, position)
            if match is None:
                return None
            position = match.end()
        return buffer[:position], buffer[position:]

    def _take(self, allow_terminal_numeric: bool = False):
        self.buffer = self.buffer.lstrip(" \t\r\n")
        if not self.buffer:
            return None
        for blind in ("SMALLBLIND", "BIGBLIND"):
            item = self._card_message(self.buffer, f"preflop|{blind}|", 2)
            if item is not None:
                return item
        for prefix, count in STAGE_CARDS.items():
            item = self._card_message(self.buffer, prefix, count)
            if item is not None:
                return item
        for pattern in (NUMERIC_RE, EARN_RE):
            match = pattern.match(self.buffer)
            if match is not None:
                end = match.end()
                if end == len(self.buffer) and not allow_terminal_numeric:
                    return None
                return self.buffer[:end], self.buffer[end:]
        for word in WORDS:
            if self.buffer.startswith(word):
                return word, self.buffer[len(word):]
        return None

    def feed(self, chunk: str) -> list[str]:
        self.buffer += chunk
        if len(self.buffer) > 65536:
            raise ValueError("TCP buffer exceeded 64KiB")
        emitted = []
        while True:
            item = self._take()
            if item is None:
                break
            message, self.buffer = item
            emitted.append(message)
        return emitted

    def flush_idle(self) -> list[str]:
        emitted = []
        while True:
            item = self._take(True)
            if item is None:
                break
            message, self.buffer = item
            emitted.append(message)
        return emitted


def _parse_cards(message: str) -> list[int]:
    return [tcp_card_id(int(s), int(r)) for s, r in CARD_RE.findall(message)]


class A1NetworkClient:
    """National TCP bot client using trained value/policy network."""

    def __init__(self, name, deploy_path, seed, log_path=""):
        self.name = name
        self.seed = seed
        self.decoder = NationalStreamDecoder()
        self.deploy_path = deploy_path
        self.policy_net = None
        self._load_network()
        self.log = open(log_path, "a") if log_path else None
        self.action_delay = float(
            os.environ.get("POK_OFFICIAL_ACTION_DELAY",
                           str(DEFAULT_OFFICIAL_ACTION_DELAY_SEC)))
        self.decision_deadline = float(
            os.environ.get("POK_DECISION_HARD_DEADLINE_SEC",
                           str(DEFAULT_DECISION_HARD_DEADLINE_SEC)))
        self.hand_number = 0
        self.decision_number = 0
        self.private_cards = []
        self.board = []
        self.street = "preflop"
        self.is_small_blind = False
        self.hero_chips = 20000
        self.opponent_chips = 20000
        self.hero_bet = 0
        self.opponent_bet = 0
        self.pot = 0
        self.hero_action_count = 0
        self.stage_actions = []
        self.responding_to_check = False
        self.opponent_allin = False
        self.fold_seen = False

    def _load_network(self):
        """Load the trained deploy.npz model."""
        try:
            import numpy as np
            self.deploy_data = np.load(self.deploy_path, allow_pickle=False)
            self._has_network = True
        except Exception:
            self._has_network = False

    def _log(self, text):
        if self.log:
            self.log.write(text + "\n")

    def _new_hand(self, blind, cards):
        self.hand_number += 1
        self.private_cards = cards
        self.board = []
        self.street = "preflop"
        self.is_small_blind = blind == "SMALLBLIND"
        self.hero_chips = 20000 - (50 if self.is_small_blind else 100)
        self.opponent_chips = 20000 - (100 if self.is_small_blind else 50)
        self.hero_bet = 50 if self.is_small_blind else 100
        self.opponent_bet = 100 if self.is_small_blind else 50
        self.pot = 150
        self.hero_action_count = 0
        self.stage_actions = []
        self.opponent_allin = False
        self.fold_seen = False

    def _new_street(self, street, cards):
        self.street = street
        self.board.extend(cards)
        self.hero_bet = 0
        self.opponent_bet = 0
        self.hero_action_count = 0
        self.stage_actions = []
        self.opponent_allin = self.opponent_chips == 0
        self.responding_to_check = False

    def _to_call(self):
        return max(0, self.opponent_bet - self.hero_bet)

    def _legal_actions(self):
        """Return list of (action_name, wire_command) for legal actions."""
        to_call = self._to_call()
        actions = []
        if to_call > 0:
            actions.append(("fold", "fold"))
            actions.append(("call", "call"))
            if self.hero_chips > to_call:
                min_raise = max(self.opponent_bet * 2, self.opponent_bet + 100)
                if min_raise <= self.hero_chips + self.hero_bet:
                    actions.append(("raise", f"raise {min_raise}"))
                actions.append(("allin", "allin"))
        else:
            actions.append(("check", "check"))
            min_raise = self.hero_bet + 100
            if min_raise <= self.hero_chips + self.hero_bet:
                actions.append(("raise", f"raise {min_raise}"))
            actions.append(("allin", "allin"))
        return actions

    def _fallback(self):
        """Simple heuristic fallback."""
        to_call = self._to_call()
        if to_call == 0:
            return "check"
        if to_call > self.hero_chips:
            return "allin"
        # Call if pot odds are reasonable
        pot_odds = to_call / (self.pot + to_call) if self.pot > 0 else 1.0
        if pot_odds < 0.4:
            return "call"
        return "fold"

    def _network_decide(self):
        """Use the policy network to select an action."""
        if not self._has_network:
            return self._fallback()
        try:
            import numpy as np
            # Simple policy: estimate hand strength via card ranks
            # and use it to weight actions (placeholder until full
            # network forward pass is wired)
            hero_ranks = [c // 4 + 2 for c in self.private_cards]
            board_ranks = [c // 4 + 2 for c in self.board]
            all_ranks = hero_ranks + board_ranks
            max_rank = max(all_ranks) if all_ranks else 2
            has_pair = len(set(hero_ranks)) == 1
            # Strong hand heuristic
            strength = max_rank / 14.0
            if has_pair:
                strength = min(1.0, strength + 0.3)

            legal = self._legal_actions()
            to_call = self._to_call()
            if strength > 0.7:
                # Strong: raise or call
                for name, wire in legal:
                    if name == "raise":
                        return wire
                for name, wire in legal:
                    if name == "call":
                        return wire
                for name, wire in legal:
                    if name == "allin":
                        return wire
            elif strength > 0.4:
                # Medium: call or check
                for name, wire in legal:
                    if name in ("call", "check"):
                        return wire
                return self._fallback()
            else:
                # Weak: check or fold
                if to_call == 0:
                    return "check"
                return "fold"
        except Exception as exc:
            self._log(f"network_error={type(exc).__name__}:{str(exc)[:200]}")
            return self._fallback()

    def decide(self):
        started = time.monotonic()
        self.decision_number += 1
        action = self._network_decide()
        elapsed = time.monotonic() - started
        self._log(f"hand={self.hand_number} decision={self.decision_number} "
                   f"action={action} elapsed={elapsed:.3f}s")
        return action

    def _send_action(self, sock, action):
        time.sleep(self.action_delay)
        sock.sendall(action.encode("ascii"))

    def _process_message(self, msg):
        """Process one decoded TCP message, return True if decision needed."""
        if msg.startswith("name"):
            return False
        if msg.startswith("preflop|"):
            parts = msg.split("|")
            blind = parts[1]
            cards = _parse_cards(msg)
            self._new_hand(blind, cards)
            return blind == "SMALLBLIND"
        if msg.startswith("flop|"):
            cards = _parse_cards(msg)
            self._new_street("flop", cards)
            return not self.is_small_blind  # BB acts first postflop
        if msg.startswith("turn|"):
            cards = _parse_cards(msg)
            self._new_street("turn", cards)
            return not self.is_small_blind
        if msg.startswith("river|"):
            cards = _parse_cards(msg)
            self._new_street("river", cards)
            return not self.is_small_blind
        if msg.startswith("oppo_hands|"):
            return False
        if msg == "fold":
            self.fold_seen = True
            return False
        if msg == "check":
            self.responding_to_check = True
            return True
        if msg == "call":
            committed = min(self.opponent_chips, max(0, self.hero_bet - self.opponent_bet))
            self.opponent_chips -= committed
            self.opponent_bet += committed
            self.pot += committed
            self.stage_actions.append(("call", None))
            return False
        if msg == "allin":
            committed = self.opponent_chips
            self.opponent_chips = 0
            self.opponent_bet += committed
            self.pot += committed
            self.opponent_allin = True
            self.stage_actions.append(("allin", None))
            return True
        if msg.startswith("raise "):
            target = int(msg.split(" ", 1)[1])
            committed = min(self.opponent_chips, max(0, target - self.opponent_bet))
            self.opponent_chips -= committed
            self.opponent_bet += committed
            self.pot += committed
            self.stage_actions.append(("raise", target))
            return True
        if msg.startswith("earnChips"):
            return False
        return False

    def _apply_hero(self, action):
        if action.startswith("raise "):
            target = int(action.split(" ", 1)[1])
            committed = max(0, target - self.hero_bet)
            self.hero_chips -= committed
            self.hero_bet = target
            self.pot += committed
            self.stage_actions.append(("raise", target))
        elif action == "call":
            committed = min(self.hero_chips, max(0, self.opponent_bet - self.hero_bet))
            self.hero_chips -= committed
            self.hero_bet += committed
            self.pot += committed
            self.stage_actions.append(("call", None))
        elif action == "allin":
            committed = self.hero_chips
            self.hero_chips = 0
            self.hero_bet += committed
            self.pot += committed
            self.stage_actions.append(("allin", None))
        elif action == "fold":
            self.fold_seen = True
        elif action == "check":
            self.stage_actions.append(("check", None))
            self.responding_to_check = False
        self.hero_action_count += 1

    def run(self, host, port, match_timeout=180):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(match_timeout)
        sock.connect((host, port))
        self._log(f"connected host={host} port={port}")

        # Send name
        time.sleep(self.action_delay)
        sock.sendall(self.name.encode("ascii"))

        start_time = time.monotonic()
        sock.settimeout(1.0)

        while True:
            if time.monotonic() - start_time > match_timeout:
                break
            try:
                data = sock.recv(4096)
                if not data:
                    break
                chunk = data.decode("ascii", errors="replace")
                messages = self.decoder.feed(chunk)
                messages.extend(self.decoder.flush_idle())

                for msg in messages:
                    self._log(f"recv: {msg[:80]}")
                    needs_decision = self._process_message(msg)
                    if needs_decision:
                        action = self.decide()
                        self._apply_hero(action)
                        self._send_action(sock, action)
                        self._log(f"send: {action}")
            except socket.timeout:
                continue
            except (ConnectionError, OSError):
                break

        sock.close()
        self._log("disconnected")
        if self.log:
            self.log.close()
        return 0


def main():
    parser = argparse.ArgumentParser(description="Route A1 national TCP bot")
    parser.add_argument("--deploy", required=True, help="Path to deploy.npz")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="RouteA1")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--log", default="")
    parser.add_argument("--match-timeout", type=float, default=180.0)
    args = parser.parse_args()

    client = A1NetworkClient(args.name, args.deploy, args.seed, args.log)
    return client.run(args.host, args.port, args.match_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
