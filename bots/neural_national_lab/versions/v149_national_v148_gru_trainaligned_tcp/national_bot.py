#!/usr/bin/env python3
"""Native national TCP entrypoint for this bot.

This file is the formal national-platform submission entry. It connects to the
TCP server, maintains raw-stream state, calls the local strategy in process,
and sends only national wire actions: raise <amount>, fold, call, check, allin.
It deliberately uses no legacy bridge module and prints no JSON responses to
stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
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
TCP_NUMERIC_COALESCE_SEC = 0.05
TRACE_PREFIX = "POK_TRACE_DECISION "
_LOG_FP = None


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"TRACE_ENV_ERROR {name}={raw!r}", file=sys.stderr)
        return None


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
        _LOG_FP.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
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


def _take_message(buffer: str, *, flush_numeric: bool = True) -> tuple[str | None, str]:
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
        if not flush_numeric and match.end() == len(buffer):
            return None, buffer
        return buffer[:match.end()], buffer[match.end():]
    match = ACTION_PREFIX_RE.match(buffer)
    if match:
        if not flush_numeric and match.end() == len(buffer):
            return None, buffer
        return buffer[:match.end()], buffer[match.end():]
    for word in ("allin", "check", "call", "fold"):
        if buffer.startswith(word):
            return word, buffer[len(word):]
    return None, buffer


def _split_messages(buffer: str, *, flush_numeric: bool = True) -> tuple[list[str], str]:
    messages: list[str] = []
    while buffer:
        msg, rest = _take_message(buffer, flush_numeric=flush_numeric)
        if msg is None:
            return messages, rest
        messages.append(msg)
        buffer = rest
    return messages, ""


def _is_trailing_numeric_message(buffer: str) -> bool:
    value = buffer.lstrip("\r\n\t ")
    return bool(EARN_PREFIX_RE.fullmatch(value) or ACTION_PREFIX_RE.fullmatch(value))


def _recv_messages(sock: socket.socket, buffer: str) -> tuple[list[str], str, bool]:
    """Receive one stream batch without committing a partial numeric token.

    The official platform does not frame messages. A recv boundary can split
    ``raise 200`` after any digit, so a trailing numeric token is held for one
    short quiet window. A following protocol prefix ends it immediately; an
    idle window commits it because the server may be waiting for our action.
    """
    messages: list[str] = []
    data = sock.recv(4096)
    if not data:
        return messages, buffer, True
    chunk = data.decode("utf-8", "replace")
    buffer += chunk
    _log(f"RECV raw={chunk!r} buffer={buffer!r}")

    while True:
        batch, buffer = _split_messages(buffer, flush_numeric=False)
        messages.extend(batch)
        if not _is_trailing_numeric_message(buffer):
            return messages, buffer, False

        readable, _, _ = select.select([sock], [], [], TCP_NUMERIC_COALESCE_SEC)
        if not readable:
            batch, buffer = _split_messages(buffer, flush_numeric=True)
            messages.extend(batch)
            return messages, buffer, False

        data = sock.recv(4096)
        if not data:
            batch, buffer = _split_messages(buffer, flush_numeric=True)
            messages.extend(batch)
            return messages, buffer, True
        chunk = data.decode("utf-8", "replace")
        buffer += chunk
        _log(f"RECV coalesced={chunk!r} buffer={buffer!r}")


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
        self._trace_enabled = _env_bool("POK_TRACE_DECISIONS")
        self._force_hand = _env_int("POK_FORCE_HAND")
        self._force_decision = _env_int("POK_FORCE_DECISION")
        self._force_action = _env_int("POK_FORCE_ACTION")
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
        self._decision_serial = 0
        self._hand_decision_index = 0
        self._opponent_profile_counts = {
            "actions_total": 0,
            "fold": 0,
            "call": 0,
            "check": 0,
            "raise": 0,
            "allin": 0,
            "preflop_actions": 0,
            "preflop_raises": 0,
            "postflop_actions": 0,
            "postflop_raises": 0,
        }

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

    def _opponent_profile(self) -> dict:
        counts = self._opponent_profile_counts
        total = max(1, int(counts.get("actions_total", 0)))
        preflop_total = max(1, int(counts.get("preflop_actions", 0)))
        postflop_total = max(1, int(counts.get("postflop_actions", 0)))
        aggressive = int(counts.get("raise", 0)) + int(counts.get("allin", 0))
        return {
            "actions_total": int(counts.get("actions_total", 0)),
            "confidence": min(1.0, float(counts.get("actions_total", 0)) / 20.0),
            "actions_total_norm": min(1.0, float(counts.get("actions_total", 0)) / 40.0),
            "fold_rate": float(counts.get("fold", 0)) / total,
            "call_rate": float(counts.get("call", 0)) / total,
            "check_rate": float(counts.get("check", 0)) / total,
            "raise_rate": float(counts.get("raise", 0)) / total,
            "allin_rate": float(counts.get("allin", 0)) / total,
            "aggression": float(aggressive) / total,
            "preflop_actions_norm": min(1.0, float(counts.get("preflop_actions", 0)) / 20.0),
            "preflop_raise_rate": float(counts.get("preflop_raises", 0)) / preflop_total,
            "postflop_actions_norm": min(1.0, float(counts.get("postflop_actions", 0)) / 20.0),
            "postflop_raise_rate": float(counts.get("postflop_raises", 0)) / postflop_total,
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
            "opponent_profile": self._opponent_profile(),
            **self._betting_snapshot(),
        }
        if "remaining_hands" not in req:
            req["remaining_hands"] = self.infer_remaining_hands(self._requests + [req])
        return req

    def _should_force(self, decision_index: int) -> bool:
        if self._force_action is None:
            return False
        if self._force_hand is not None and self._force_hand != self._hand_num:
            return False
        if self._force_decision is not None and self._force_decision != decision_index:
            return False
        return True

    def _trace_decision(
        self,
        *,
        req: dict,
        state: dict,
        decision_index: int,
        rule_action: int,
        advised_action: int,
        sanitized_action: int,
        final_action: int,
        forced: bool,
    ) -> None:
        if not self._trace_enabled:
            return
        row = {
            "type": "decision",
            "name": self.name,
            "hand": self._hand_num,
            "decision_serial": self._decision_serial,
            "hand_decision_index": decision_index,
            "stage": self._stage,
            "round": self._round_num(),
            "is_small_blind": self._is_sb,
            "rule_action": int(rule_action),
            "advised_action": int(advised_action),
            "sanitized_action": int(sanitized_action),
            "final_action": int(final_action),
            "forced": bool(forced),
            "force_action": self._force_action,
            "request": req,
            "state": state,
        }
        print(TRACE_PREFIX + json.dumps(row, separators=(",", ":")), file=sys.stderr, flush=True)

    def _strategy_action(self, decision_index: int) -> int:
        req = self._request()
        self._requests.append(req)
        action = self.get_action(req, list(self._requests))
        rule_action = action
        advised_action = action
        sanitized_action = 0
        final_action = 0
        forced = False
        try:
            state = self.reconstruct_state(req)
            if self.apply_neural_advice is not None:
                try:
                    action = self.apply_neural_advice(req, state, int(action))
                    advised_action = action
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    action = rule_action
            action = self.sanitize_action(action, state, req["my_chips"])
            sanitized_action = int(action)
            final_action = sanitized_action
            if self._should_force(decision_index):
                final_action = int(self._force_action)
                forced = True
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return 0
        try:
            self._trace_decision(
                req=req,
                state=state,
                decision_index=decision_index,
                rule_action=int(rule_action),
                advised_action=int(advised_action),
                sanitized_action=int(sanitized_action),
                final_action=int(final_action),
                forced=forced,
            )
            self._decision_serial += 1
            return int(final_action)
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
        if player_id == self._opponent_id:
            self._update_opponent_profile(action_type)

    def _update_opponent_profile(self, action_type: str) -> None:
        if action_type not in {"fold", "call", "check", "raise", "allin"}:
            return
        counts = self._opponent_profile_counts
        counts["actions_total"] += 1
        counts[action_type] += 1
        if self._round_num() == 0:
            counts["preflop_actions"] += 1
            if action_type in {"raise", "allin"}:
                counts["preflop_raises"] += 1
        else:
            counts["postflop_actions"] += 1
            if action_type in {"raise", "allin"}:
                counts["postflop_raises"] += 1

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
        decision_index = self._hand_decision_index
        self._hand_decision_index += 1
        try:
            action = self._strategy_action(decision_index)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            action = -1
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
            self._hand_decision_index = 0
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
            messages, buffer, closed = _recv_messages(sock, buffer)
            for line in messages:
                _log(f"DISPATCH line={line!r}")
                bot.handle(line, sock)
            if closed:
                _log("RECV empty -> server closed")
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Native national TCP bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="Bot")
    parser.add_argument("--seat", choices=("auto", "upper", "lower"), default="auto")
    parser.add_argument("--log", default="", help="Log file path. Empty disables file logging.")
    args = parser.parse_args()
    try:
        return run_client(args.host, args.port, args.name, args.log, args.seat)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
