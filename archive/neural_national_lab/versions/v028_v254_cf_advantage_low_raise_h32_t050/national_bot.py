#!/usr/bin/env python3
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
        try:
            from neural_policy import apply_neural_advice
        except Exception as exc:
            apply_neural_advice = None
            print(f"NEURAL_IMPORT_ERROR {exc}", file=sys.stderr)

        self.get_action = get_action
        self.apply_neural_advice = apply_neural_advice
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
        rule_action = action
        try:
            state = self.reconstruct_state(req)
            if self.apply_neural_advice is not None:
                try:
                    action = self.apply_neural_advice(req, state, int(action))
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    action = rule_action
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
