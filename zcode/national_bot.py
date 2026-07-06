"""National competition TCP entry point.

Connects to the national self-play TCP server, maintains the line-protocol
state machine described in ``sever/国赛平台/通信协议.docx``, and emits only
legal national wire actions::

    raise <amount>     # amount = raise-to-total (stage total), single space
    call
    check
    fold
    allin

This file is the formal national-platform submission entry. It uses no
adapter / bridge module and prints nothing to stdout except the client name
on the initial ``name`` query. All strategy logic is imported from the
``zcode`` package (cards / equity / state / policy).

Card conversion
---------------
The TCP protocol uses ``<suit,rank>`` strings where:
    suit: 0=Spade, 1=Heart, 2=Diamond, 3=Club
    rank: 0=2, 1=3, ... 11=King, 12=Ace   (so tcp_rank = card_rank - 2)

The local engine (and the zcode package) uses integers 0..51:
    rank = card // 4 + 2  (2..14, Ace=14)
    suit = card % 4       (0=Heart, 1=Diamond, 2=Spade, 3=Club)

Hence the suit mapping when converting TCP -> local:
    TCP 0 (Spade) -> local 2
    TCP 1 (Heart) -> local 0
    TCP 2 (Diamond) -> local 1
    TCP 3 (Club) -> local 3
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import sys
import time

# ---------------------------------------------------------------------------
# Make the zcode package importable regardless of the CWD.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from zcode.policy import Policy, PolicyConfig, sanitize_action
from zcode.state import GameState, BIG_BLIND, SMALL_BLIND, reconstruct_state

logger = logging.getLogger("zcode.national_bot")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INITIAL_CHIPS = 20000
TOTAL_HANDS = 70
TIMEOUT_SECONDS = 60

# TCP suit -> local engine/judge suit
_TCP_TO_LOCAL_SUIT = {0: 2, 1: 0, 2: 1, 3: 3}
_LOCAL_TO_TCP_SUIT = {v: k for k, v in _TCP_TO_LOCAL_SUIT.items()}

_CARD_RE = re.compile(r"<(\d+),(\d+)>")
_ACTION_RE = re.compile(r"^(raise|call|check|fold|allin)(?:\s+(\d+))?$")


# ---------------------------------------------------------------------------
# Card / action conversion
# ---------------------------------------------------------------------------

def tcp_cards_to_local(text: str) -> list[int]:
    """Parse TCP ``<suit,rank>`` cards into local integers."""
    out = []
    for suit_s, rank_s in _CARD_RE.findall(text):
        tcp_suit = int(suit_s)
        tcp_rank = int(rank_s)            # 0=2 ... 12=A
        local_suit = _TCP_TO_LOCAL_SUIT[tcp_suit]
        local_rank = tcp_rank + 2         # 2..14
        out.append((local_rank - 2) * 4 + local_suit)
    return out


def parse_opponent_action(line: str) -> tuple[str, int | None]:
    """Parse a received opponent-action line.

    Returns ``(action_type, amount)`` where ``amount`` is the raise-to-total
    for ``raise``, else ``None``.
    """
    line = line.strip()
    if line == "call":
        return ("call", None)
    if line == "check":
        return ("check", None)
    if line == "fold":
        return ("fold", None)
    if line == "allin":
        return ("allin", None)
    m = re.fullmatch(r"raise (\d+)", line)
    if m:
        return ("raise", int(m.group(1)))
    return ("unknown", None)


def int_action_to_wire(action: int, st: GameState) -> str:
    """Translate a zcode policy action integer into a national wire string.

    zcode semantics:
      >0  raise-to-total (round total)  -> ``raise <amount>``
       0  call/check (depends on to_call)
      -1  fold
      -2  all-in                         -> ``allin``
    """
    if action == -1:
        return "fold"
    if action == -2:
        return "allin"
    if action == 0:
        # call if there is a bet to call, else check.
        return "call" if st.to_call > 0 else "check"
    # raise: ``action`` is a round-total. National ``raise <amount>`` is also
    # a raise-to-total per sever/engine/validator.py, so we send it directly.
    # Clamp to legal minimum to be defensive.
    target = int(action)
    if st.to_call == 0 and target < st.min_raise_to:
        target = st.min_raise_to
    return f"raise {target}"


# ---------------------------------------------------------------------------
# Match state machine
# ---------------------------------------------------------------------------

class NationalMatch:
    """One TCP connection's worth of match state.

    The server drives the conversation; we only react to incoming lines and
    keep enough state to translate the next decision request into a single
    zcode policy call.
    """

    def __init__(self, name: str, policy: Policy):
        self.name = name
        self.policy = policy
        # Per-hand state.
        self.my_cards: list[int] = []
        self.public_cards: list[int] = []
        self.hand_history: list[dict] = []
        self.betting_round = 0
        # Blind position this hand: True if we are the small blind (dealer).
        self.is_sb: bool | None = None
        self.my_id: int | None = None
        # We track contributions to be able to build a request for the policy.
        self.my_round_bet = 0
        self.opp_round_bet = 0
        self.round_bet = 0
        self.my_chips = INITIAL_CHIPS
        self.opp_chips = INITIAL_CHIPS
        self.actions_this_round = 0
        # Cumulative.
        self.hand = 0
        self.total_win_chips = [0, 0]

    # ------------------------------------------------------------------
    # State reconstruction for the policy
    # ------------------------------------------------------------------
    def _build_request(self) -> dict:
        """Build a request dict equivalent to the local engine's request."""
        return {
            "num_players": 2,
            "dealer_id": 0 if self.is_sb else 1,
            "my_id": self.my_id,
            "my_chips": self.my_chips,
            "my_cards": list(self.my_cards),
            "public_cards": list(self.public_cards),
            "history": list(self.hand_history),
            "hand": self.hand,
            "max_hand": TOTAL_HANDS,
            "total_win_chips": list(self.total_win_chips),
        }

    def _decide_and_emit(self) -> str:
        req = self._build_request()
        st = reconstruct_state(req)
        action = self.policy.decide(st)
        action = sanitize_action(action, st)
        wire = int_action_to_wire(action, st)
        # Record our own action in history for subsequent state builds.
        self._record_own_action(action, wire)
        return wire

    def _record_own_action(self, action: int, wire: str) -> None:
        at, amt = parse_opponent_action(wire)
        self.hand_history.append({
            "round": self.betting_round,
            "player_id": self.my_id,
            "action": action,
            "action_type": at,
        })

    def _record_opp_action(self, wire: str) -> None:
        at, amt = parse_opponent_action(wire)
        # ``raise`` stores raise-to-total in ``action`` to match local engine.
        if at == "raise":
            action_int = amt if amt is not None else 0
        elif at == "call":
            action_int = 0
        elif at == "check":
            action_int = 0
        elif at == "fold":
            action_int = -1
        elif at == "allin":
            action_int = -2
        else:
            return
        opp_id = 1 - self.my_id if self.my_id is not None else 1
        self.hand_history.append({
            "round": self.betting_round,
            "player_id": opp_id,
            "action": action_int,
            "action_type": at,
        })

    # ------------------------------------------------------------------
    # Incoming message handlers
    # ------------------------------------------------------------------
    def on_line(self, line: str) -> str | None:
        """Process one inbound line. Return a wire action to send, or None."""
        line = line.strip()
        if not line:
            return None

        if line == "name":
            return f"name {self.name}"

        if line.startswith("preflop|"):
            self._begin_hand(line)
            # In heads-up the small blind (dealer) acts first preflop. If we
            # are the SB the server expects our action immediately; if we
            # are the BB we wait for the opponent's action first.
            if self.is_sb:
                return self._decide_and_emit()
            return None

        if line.startswith("flop|"):
            self._advance_round(1, line)
            return self._maybe_act_postflop()
        if line.startswith("turn|"):
            self._advance_round(2, line)
            return self._maybe_act_postflop()
        if line.startswith("river|"):
            self._advance_round(3, line)
            return self._maybe_act_postflop()

        if line.startswith("earnChips"):
            self._end_hand(line)
            return None
        if line.startswith("oppo_hands|"):
            return None

        # Otherwise: opponent's action forwarded by the server.
        at, _ = parse_opponent_action(line)
        if at != "unknown":
            self._record_opp_action(line)
            self.actions_this_round += 1
            # After the opponent acts it is (usually) our turn now. Decide.
            # We avoid deciding on lines that are clearly not "your turn"
            # (e.g. an ``allin`` we cannot match); the policy's sanitize
            # step handles those gracefully.
            if self.my_id is not None and not self._hand_settled():
                return self._decide_and_emit()
        return None

    # ------------------------------------------------------------------
    def _begin_hand(self, line: str) -> None:
        # preflop|<BLIND_TYPE>|<card> <card>
        parts = line.split("|")
        blind_type = parts[1].strip()
        self.is_sb = (blind_type == "SMALLBLIND")
        # In heads-up the dealer == small blind. my_id is 0 when we are SB.
        self.my_id = 0 if self.is_sb else 1
        cards_text = parts[2] if len(parts) > 2 else ""
        self.my_cards = tcp_cards_to_local(cards_text)
        self.public_cards = []
        self.hand_history = []
        self.betting_round = 0
        self.my_round_bet = SMALL_BLIND if self.is_sb else BIG_BLIND
        self.opp_round_bet = BIG_BLIND if self.is_sb else SMALL_BLIND
        self.round_bet = BIG_BLIND
        self.actions_this_round = 0
        self.my_chips = INITIAL_CHIPS
        self.opp_chips = INITIAL_CHIPS

    def _advance_round(self, new_round: int, line: str) -> None:
        # flop|<card><card><card>   turn|<card>   river|<card>
        parts = line.split("|", 1)
        cards_text = parts[1] if len(parts) > 1 else ""
        new_cards = tcp_cards_to_local(cards_text)
        if new_round == 1:
            self.public_cards = new_cards            # 3 cards
        else:
            self.public_cards = self.public_cards + new_cards
        self.betting_round = new_round
        # Round contributions reset for the new betting round.
        self.actions_this_round = 0

    def _maybe_act_postflop(self) -> str | None:
        """Post-flop the BB (non-dealer) acts first. If we are BB we act
        immediately on receiving the flop/turn/river; if we are SB we wait
        for the opponent's action (the server will forward it next)."""
        if self.is_sb:
            return None
        # We are BB: decide now (to_call is 0 on a fresh postflop round).
        return self._decide_and_emit()

    def _hand_settled(self) -> bool:
        # If a fold has already occurred this hand the hand is over; do not
        # decide. (All-in does NOT settle the hand here because the other
        # player still has to call/fold, but the server will then run out the
        # board and only send earnChips/oppo_hands afterwards.)
        for h in self.hand_history:
            if h.get("action_type") == "fold":
                return True
        return False

    def _end_hand(self, line: str) -> None:
        try:
            amount = int(line.split()[1])
        except (IndexError, ValueError):
            amount = 0
        if self.my_id is not None:
            self.total_win_chips[self.my_id] += amount
            self.total_win_chips[1 - self.my_id] -= amount
        self.hand += 1


# ---------------------------------------------------------------------------
# TCP client loop
# ---------------------------------------------------------------------------

def run_client(host: str, port: int, name: str, policy: Policy,
               timeout: float = TIMEOUT_SECONDS) -> int:
    """Connect to the TCP server and play matches until disconnected."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    match = NationalMatch(name, policy)

    buf = ""
    try:
        while True:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                logger.warning("recv timeout, sending keepalive check")
                continue
            if not data:
                logger.info("server closed connection")
                break
            try:
                buf += data.decode("utf-8", errors="replace")
            except Exception:
                buf += data.decode("latin-1", errors="replace")

            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                logger.debug("<= %s", line)
                reply = match.on_line(line)
                if reply is not None:
                    logger.debug("=> %s", reply)
                    sock.sendall((reply + "\n").encode("utf-8"))
    except KeyboardInterrupt:
        logger.info("interrupted by user")
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="zcode national TCP bot client")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=10001)
    p.add_argument("--name", default="zcode")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = PolicyConfig(seed=args.seed)
    policy = Policy(cfg)
    logger.info("connecting to %s:%s as %s", args.host, args.port, args.name)
    return run_client(args.host, args.port, args.name, policy)


if __name__ == "__main__":
    raise SystemExit(main())
