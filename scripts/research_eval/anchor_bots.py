#!/usr/bin/env python3
"""Stable anchor opponents for evaluation: passive, aggressive, and random."""
import socket, sys, re, random, os, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "sever"))

SUIT_NAMES = {0: "S", 1: "H", 2: "D", 3: "C"}
RANK_NAMES = {0: "2", 1: "3", 2: "4", 3: "5", 4: "6", 5: "7", 6: "8",
              7: "9", 8: "T", 9: "J", 10: "Q", 11: "K", 12: "A"}


def tcp_card_to_id(suit, rank):
    """TCP suit(0=S,1=H,2=D,3=C) rank(0=2..12=A) -> internal card 0..51."""
    suit_map = {0: 2, 1: 0, 2: 1, 3: 3}
    return rank * 4 + suit_map[suit]


def parse_tcp_cards(msg):
    cards = []
    for s, r in re.findall(r"<(\d+),(\d+)>", msg):
        cards.append(tcp_card_to_id(int(s), int(r)))
    return cards


def hand_strength_rank(private_cards, board):
    """Rough hand strength: highest rank + pair detection."""
    all_cards = list(private_cards) + list(board)
    ranks = [c // 4 + 2 for c in all_cards]
    hero_ranks = [c // 4 + 2 for c in private_cards]
    max_rank = max(ranks) if ranks else 2
    has_pair = len(set(hero_ranks)) == 1
    on_board_pair = len(set(ranks)) < len(ranks)
    strength = max_rank / 14.0
    if has_pair:
        strength = min(1.0, strength + 0.3)
    if on_board_pair:
        strength = min(1.0, strength + 0.1)
    return strength


class AnchorBot:
    """Base TCP bot with configurable strategy."""
    
    def __init__(self, host, port, name, strategy="passive", seed=0):
        self.host = host
        self.port = port
        self.name = name
        self.strategy = strategy
        self.rng = random.Random(seed)
        self.buf = ""
        self.private_cards = []
        self.board = []
        self.is_small_blind = False
        self.stage = "preflop"
        self.my_action_count = 0
        self.in_allin_runout = False
        self.cumulative_earn = 0
        
    def _recv(self, sock):
        while True:
            # Try to parse from buffer
            msg = self._parse_one()
            if msg is not None:
                return msg
            try:
                data = sock.recv(4096)
            except socket.timeout:
                # Flush numeric from buffer
                msg = self._parse_one(flush=True)
                return msg
            if not data:
                msg = self._parse_one(flush=True)
                return msg
            self.buf += data.decode("ascii", errors="replace")
    
    def _parse_one(self, flush=False):
        from server.protocol import split_server_messages
        if not self.buf:
            return None
        # Handle numeric idle
        is_numeric = bool(re.fullmatch(r"(?:raise [0-9]+|earnChips -?[0-9]+)", self.buf))
        messages, self.buf = split_server_messages(self.buf, flush_boundary=flush or is_numeric)
        if messages:
            return messages[0]
        return None
    
    def _decide(self):
        """Return action string based on strategy."""
        strength = hand_strength_rank(self.private_cards, self.board)
        to_call = self._to_call()
        
        if self.strategy == "passive":
            # Always check/call, never raise
            if to_call == 0:
                return "check"
            return "call"
        
        elif self.strategy == "aggressive":
            # Raise with strong hands, call with medium, fold weak
            if strength > 0.65:
                if to_call == 0:
                    min_raise = max(200, self._pot_after() // 2)
                    return f"raise {min_raise}"
                return "call"
            elif strength > 0.4:
                if to_call == 0:
                    return "check"
                return "call"
            else:
                if to_call == 0:
                    return "check"
                return "fold"
        
        elif self.strategy == "random":
            r = self.rng.random()
            if to_call == 0:
                if r < 0.3:
                    min_raise = max(200, self._pot_after() // 3)
                    return f"raise {min_raise}"
                return "check"
            else:
                if r < 0.1:
                    return "fold"
                elif r < 0.2:
                    min_raise = max(200, self._pot_after() // 2)
                    return f"raise {min_raise}"
                return "call"
        
        elif self.strategy == "tight":
            # Only play premium hands
            hero_ranks = sorted([c // 4 + 2 for c in self.private_cards], reverse=True)
            is_premium = hero_ranks[0] >= 12 or (hero_ranks[0] >= 10 and hero_ranks[1] >= 10)
            if self.stage == "preflop":
                if is_premium:
                    if to_call == 0:
                        return "raise 300"
                    return "call"
                else:
                    if to_call == 0:
                        return "check"
                    return "fold"
            else:
                if strength > 0.6:
                    if to_call == 0:
                        return "raise 200"
                    return "call"
                if to_call == 0:
                    return "check"
                if to_call <= 200 and strength > 0.4:
                    return "call"
                return "fold"

        elif self.strategy == "nemesis_a1":
            # Exploit A1's passive post-fix play: hyper-aggressive pressure
            # A1's network outputs near-uniform, so it rarely raises back
            r = self.rng.random()
            if strength > 0.5 or r < 0.3:
                if to_call == 0:
                    # Raise frequently when checked to
                    raise_amt = max(200, self._pot_after() // 2)
                    return f"raise {raise_amt}"
                if to_call <= 500:
                    return "call"
                if r < 0.2:
                    return f"raise {to_call * 2}"
                return "call"
            else:
                if to_call == 0:
                    return "check"
                if to_call > 1000:
                    return "fold"
                return "call"

        elif self.strategy == "nemesis_a2":
            # Exploit A2's blueprint rigidity: mixed raise sizings
            # A2's blueprint can't adapt to non-standard sizing
            if self.stage == "preflop":
                if strength > 0.6:
                    return f"raise {max(400, self._pot_after())}"
                elif strength > 0.4:
                    if to_call == 0:
                        return "check"
                    return "call"
                else:
                    if to_call == 0:
                        return "check"
                    return "fold"
            else:
                # Mix between check-raise and delayed aggression
                if self.my_action_count == 0 and to_call == 0:
                    if self.rng.random() < 0.4:
                        return f"raise {max(200, self._pot_after() // 3)}"
                    return "check"
                if strength > 0.55:
                    if to_call == 0:
                        return f"raise {max(300, self._pot_after() // 2)}"
                    return "call"
                if to_call == 0:
                    return "check"
                if to_call <= 300:
                    return "call"
                return "fold"

        elif self.strategy == "nemesis_b":
            # Exploit B's CFV over-aggression: ultra-tight trapper
            # B makes large exploitable bets; fold to them unless very strong,
            # then raise big with monsters
            hero_ranks = sorted([c // 4 + 2 for c in self.private_cards], reverse=True)
            is_premium = hero_ranks[0] >= 13 or (hero_ranks[0] >= 11 and hero_ranks[1] >= 11)
            is_strong = hero_ranks[0] >= 10 and hero_ranks[1] >= 8

            if self.stage == "preflop":
                if is_premium:
                    # Raise big with premiums to build pot
                    return f"raise {max(500, self._pot_after())}"
                elif is_strong and to_call <= 200:
                    return "call"
                elif to_call == 0:
                    return "check"
                else:
                    return "fold"
            else:
                # Postflop: only continue with strong made hands
                if strength > 0.7:
                    # Monster: slow-play or raise big
                    if to_call == 0:
                        if self.rng.random() < 0.3:
                            return f"raise {max(500, self._pot_after())}"
                        return "check"
                    # Trap: call to let B keep betting
                    return "call"
                elif strength > 0.5 and to_call <= 300:
                    return "call"
                elif to_call == 0:
                    return "check"
                else:
                    # Fold to B's aggression with marginal hands
                    return "fold"
        
        # Default: passive
        if to_call == 0:
            return "check"
        return "call"
    
    def _to_call(self):
        return 0  # Simplified - will be overridden by message parsing
    
    def _pot_after(self):
        return 150  # Approximate
    
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.host, self.port))
        sock.settimeout(120)
        
        while True:
            msg = self._recv(sock)
            if msg is None:
                break
            
            if msg == "name":
                sock.sendall(self.name.encode("ascii"))
                continue
            
            if msg.startswith("preflop|"):
                parts = msg.split("|")
                self.is_small_blind = (parts[1] == "SMALLBLIND")
                self.private_cards = parse_tcp_cards(msg)
                self.board = []
                self.stage = "preflop"
                self.my_action_count = 0
                self.in_allin_runout = False
                if self.is_small_blind:
                    action = self._decide()
                    self.my_action_count += 1
                    sock.sendall(action.encode("ascii"))
                continue
            
            if msg.startswith(("flop|", "turn|", "river|")):
                self.stage = msg.split("|")[0]
                self.board.extend(parse_tcp_cards(msg))
                self.my_action_count = 0
                if not self.is_small_blind and not self.in_allin_runout:
                    action = self._decide()
                    self.my_action_count += 1
                    sock.sendall(action.encode("ascii"))
                continue
            
            if msg.startswith("earnChips"):
                try:
                    self.cumulative_earn += int(msg.split()[1])
                except (ValueError, IndexError):
                    pass
                self.in_allin_runout = False
                continue
            
            if msg.startswith("oppo_hands|"):
                continue
            
            if msg == "fold":
                continue
            
            if msg == "check":
                if self.stage != "preflop":
                    action = self._decide()
                    self.my_action_count += 1
                    sock.sendall(action.encode("ascii"))
                else:
                    action = self._decide()
                    self.my_action_count += 1
                    sock.sendall(action.encode("ascii"))
                continue
            
            if msg == "call":
                action = self._decide()
                self.my_action_count += 1
                sock.sendall(action.encode("ascii"))
                continue
            
            if msg == "allin":
                sock.sendall(b"call")
                self.in_allin_runout = True
                continue
            
            if msg.startswith("raise "):
                action = self._decide()
                self.my_action_count += 1
                sock.sendall(action.encode("ascii"))
                continue
        
        sock.close()
        
        import sys as _sys
        print(
            f'ANCHOR_TELEMETRY {{"bot_name":"{self.name}","strategy":"{self.strategy}","earnings":{self.cumulative_earn}}}',
            file=_sys.stderr,
        )


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--strategy", default="passive",
                        choices=["passive", "aggressive", "random", "tight",
                                 "nemesis_a1", "nemesis_a2", "nemesis_b"])
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    
    bot = AnchorBot(args.host, args.port, args.name, args.strategy, args.seed)
    bot.run()


if __name__ == "__main__":
    main()
