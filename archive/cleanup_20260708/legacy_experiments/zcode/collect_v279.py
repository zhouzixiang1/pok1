"""Collect claude_v279 decisions on synthetic scenarios for behavior cloning.

Strategy: generate diverse legal heads-up scenarios (random hole cards,
random board, random betting history that reflects realistic action
sequences), query claude_v279 once per scenario via a persistent subprocess,
and record (scenario features, v279 action) pairs.

The output is a JSONL file consumed by ``zcode/train_student.py``.

This is behaviour cloning: we treat v279 as an oracle and learn to imitate
its action distribution. Because v279 is deterministic (no RNG-based
bluffing at fixed seeds), we get one label per scenario; we add light
feature noise / multiple bet sizings per spot to enrich the dataset.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from zcode.cards import card_rank, card_suit, full_deck

V279_PATH = os.path.join(_ROOT, "bots", "claude_v279", "main.py")
# Oracle path can be overridden via the --oracle CLI flag so the same script
# can collect from any bot (national_v18, national_v10, etc.).
INITIAL_CHIPS = 20000
SMALL_BLIND = 50
BIG_BLIND = 100


# ---------------------------------------------------------------------------
# Scenario generation
# ---------------------------------------------------------------------------

def _rand_hole_cards(rng, deck):
    a, b = rng.sample(deck, 2)
    return [a, b]


def _make_history_preflop(rng, my_id, dealer_id, mode):
    """Generate a preflop history ending with the opponent's action so it's
    the hero's turn.

    ``mode`` controls the kind of spot:
      'sb_open'   — hero is SB, no action yet (open)
      'bb_vs_limp'— hero is BB, SB limped
      'bb_vs_raise' — hero is BB, SB raised
      'bb_vs_3bet' — hero 3-bet, opp 4-bet (rare)
    Returns (history, dealer_id) or None if inconsistent.
    """
    opp = 1 - my_id
    sb = dealer_id
    bb = 1 - dealer_id
    history = []
    if mode == "sb_open":
        # hero is SB, no prior action
        if my_id != sb:
            return None
        return ([], dealer_id)
    if mode == "bb_vs_limp":
        if my_id != bb:
            return None
        history.append({"round": 0, "player_id": opp, "action": 0,
                        "action_type": "call"})
        return (history, dealer_id)
    if mode == "bb_vs_raise":
        if my_id != bb:
            return None
        # opp raised to some size
        size = rng.choice([200, 220, 250, 280, 300, 350, 400])
        history.append({"round": 0, "player_id": opp, "action": size,
                        "action_type": "raise"})
        return (history, dealer_id)
    if mode == "sb_vs_3bet":
        # hero SB opened, opp (BB) 3-bet
        if my_id != sb:
            return None
        open_size = rng.choice([200, 250, 300])
        history.append({"round": 0, "player_id": my_id, "action": open_size,
                        "action_type": "raise"})
        three_bet = open_size * rng.choice([2, 3, 4])
        history.append({"round": 0, "player_id": opp, "action": int(three_bet),
                        "action_type": "raise"})
        return (history, dealer_id)
    return None


def _advance_round_seed(rng, n_pub):
    """Pick a random board consistent with n_pub cards."""
    deck = full_deck()
    return rng.sample(deck, n_pub)


def _make_postflop_history(rng, my_id, dealer_id, n_pub, board_cards):
    """Generate a preflop + postflop history ending with opp's action."""
    opp = 1 - my_id
    sb = dealer_id
    bb = 1 - dealer_id
    history = []
    # Preflop: random plausible sequence ending in seeing the flop.
    pf_mode = rng.choice(["limp", "raise_call", "raise_call_reraise_fold"])
    if pf_mode == "limp":
        history.append({"round": 0, "player_id": sb, "action": 0,
                        "action_type": "call"})
        history.append({"round": 0, "player_id": bb, "action": 0,
                        "action_type": "check"})
    else:
        open_size = rng.choice([200, 250, 300])
        history.append({"round": 0, "player_id": sb, "action": open_size,
                        "action_type": "raise"})
        if pf_mode == "raise_call":
            history.append({"round": 0, "player_id": bb, "action": 0,
                            "action_type": "call"})
        else:
            three = open_size * 3
            history.append({"round": 0, "player_id": bb, "action": int(three),
                            "action_type": "raise"})
            history.append({"round": 0, "player_id": sb, "action": 0,
                            "action_type": "call"})

    # Postflop rounds up to the current one.
    cur_round = 0
    if n_pub >= 3:
        cur_round = 1
        # Decide flop action sequence.
        _add_street_actions(rng, history, 1, my_id, opp, board_cards[:3])
    if n_pub >= 4:
        cur_round = 2
        _add_street_actions(rng, history, 2, my_id, opp, board_cards[:4])
    if n_pub >= 5:
        cur_round = 3
        _add_street_actions(rng, history, 3, my_id, opp, board_cards[:5])

    # Ensure the final action is the opponent's (so hero is to act).
    if not history or history[-1].get("player_id") != opp:
        # Append an opp action if missing (check on the current street).
        history.append({"round": cur_round, "player_id": opp, "action": 0,
                        "action_type": "check"})
    return history, cur_round


def _add_street_actions(rng, history, rnd, my_id, opp, board_cards):
    """Add a plausible action sequence on one postflop street.

    Ends with the opponent acting last so the hero is to act afterwards
    (the caller will append one more if needed).
    """
    # Hero acts first postflop if BB, opp first if SB (heads-up: BB acts
    # first postflop). We use dealer_id from outside via history context.
    # For diversity, choose who acts first based on the prior history.
    # Simpler: 50% hero bets, 50% checks; opp responds.
    mode = rng.choice(["check_check", "hero_check_opp_bet", "hero_bet_opp_call",
                       "hero_bet_opp_raise", "opp_first_check", "opp_first_bet"])
    pot_so_far = _pot_from_history(history)
    if mode == "check_check":
        history.append({"round": rnd, "player_id": my_id, "action": 0,
                        "action_type": "check"})
        history.append({"round": rnd, "player_id": opp, "action": 0,
                        "action_type": "check"})
    elif mode == "hero_check_opp_bet":
        history.append({"round": rnd, "player_id": my_id, "action": 0,
                        "action_type": "check"})
        bet = max(BIG_BLIND, int(pot_so_far * rng.choice([0.33, 0.5, 0.66, 1.0])))
        history.append({"round": rnd, "player_id": opp, "action": bet,
                        "action_type": "raise"})
    elif mode == "hero_bet_opp_call":
        bet = max(BIG_BLIND, int(pot_so_far * rng.choice([0.33, 0.5, 0.66])))
        history.append({"round": rnd, "player_id": my_id, "action": bet,
                        "action_type": "raise"})
        history.append({"round": rnd, "player_id": opp, "action": 0,
                        "action_type": "call"})
    elif mode == "hero_bet_opp_raise":
        bet = max(BIG_BLIND, int(pot_so_far * 0.5))
        history.append({"round": rnd, "player_id": my_id, "action": bet,
                        "action_type": "raise"})
        reraise = int(bet * rng.choice([2, 3]))
        history.append({"round": rnd, "player_id": opp, "action": reraise,
                        "action_type": "raise"})
    elif mode == "opp_first_check":
        history.append({"round": rnd, "player_id": opp, "action": 0,
                        "action_type": "check"})
        history.append({"round": rnd, "player_id": my_id, "action": 0,
                        "action_type": "check"})
        # opp checks again so hero acts — but then hero already acted.
        # Remove last hero check to make it opp-to-act:
        history.pop()
    else:  # opp_first_bet
        bet = max(BIG_BLIND, int(pot_so_far * rng.choice([0.5, 0.66])))
        history.append({"round": rnd, "player_id": opp, "action": bet,
                        "action_type": "raise"})


def _pot_from_history(history):
    """Crude pot estimate from history (for sizing)."""
    pot = SMALL_BLIND + BIG_BLIND
    for rec in history:
        at = rec.get("action_type")
        act = rec.get("action", 0)
        if at == "raise":
            pot += max(0, int(act) - 100)
        elif at == "call":
            pot += 100
    return max(pot, BIG_BLIND)


# ---------------------------------------------------------------------------
# v279 persistent subprocess
# ---------------------------------------------------------------------------

class V279Oracle:
    def __init__(self, timeout=8.0):
        self.timeout = timeout
        self.proc = None
        self.calls = 0
        self.failures = 0
        self._start()

    def _start(self):
        try:
            if self.proc is not None:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                try:
                    self.proc.kill()
                except Exception:
                    pass
        except Exception:
            pass
        self.proc = subprocess.Popen(
            [sys.executable, V279_PATH],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )

    def query(self, request_dict):
        payload = {"requests": [request_dict], "responses": []}
        line = json.dumps(payload) + "\n"
        import threading
        result = [None]
        err = [None]

        def _read():
            try:
                result[0] = self.proc.stdout.readline()
            except Exception as e:
                err[0] = e

        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except Exception:
            self.failures += 1
            self._start()
            return None

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=self.timeout)
        if t.is_alive():
            # Timed out — kill and restart the subprocess.
            self.failures += 1
            self._start()
            return None
        self.calls += 1
        if err[0] is not None or not result[0]:
            self.failures += 1
            self._start()
            return None
        try:
            r = json.loads(result[0].strip())
            return int(r.get("response", 0))
        except Exception:
            self.failures += 1
            return None

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main collection
# ---------------------------------------------------------------------------

def collect(n_scenarios=2000, seed=42, out_path=None):
    rng = random.Random(seed)
    oracle = V279Oracle()
    if out_path is None:
        out_path = os.path.join(_HERE, "nv18_dataset.jsonl")

    records = []
    deck = full_deck()
    t0 = time.time()
    for i in range(n_scenarios):
        # Choose scenario type.
        my_id = rng.choice([0, 1])
        dealer_id = rng.choice([0, 1])
        # 50% preflop, 50% postflop
        if rng.random() < 0.5:
            mode = rng.choice(["sb_open", "bb_vs_limp", "bb_vs_raise",
                               "sb_vs_3bet"])
            res = _make_history_preflop(rng, my_id, dealer_id, mode)
            if res is None:
                continue
            history, _ = res
            n_pub = 0
            public_cards = []
        else:
            n_pub = rng.choice([3, 4, 5])
            # Sample hole cards and board disjoint.
            used = rng.sample(deck, 2 + n_pub)
            public_cards = used[2:]
            res = _make_postflop_history(rng, my_id, dealer_id, n_pub, public_cards)
            history, _ = res

        # Hole cards for the hero: sample 2 from remaining deck.
        if n_pub == 0:
            known = set()
        else:
            known = set(public_cards)
        # Also exclude cards used in the hero's hand from prior sampling.
        # For postflop we already drew board; redraw hole cards disjoint.
        remaining = [c for c in deck if c not in known]
        my_cards = rng.sample(remaining, 2)

        # Build request.
        # Compute my_chips roughly (just use a plausible value).
        my_chips = INITIAL_CHIPS - _pot_from_history(history) // 2
        my_chips = max(500, my_chips)
        request = {
            "num_players": 2,
            "dealer_id": dealer_id,
            "my_id": my_id,
            "my_chips": my_chips,
            "my_cards": my_cards,
            "public_cards": public_cards,
            "history": history,
            "hand": rng.randint(0, 69),
            "max_hand": 70,
            "total_win_chips": [0, 0],
            "total_win_games": [0, 0],
        }
        action = oracle.query(request)
        if action is None:
            continue
        records.append({
            "my_cards": my_cards,
            "public_cards": public_cards,
            "history": history,
            "my_id": my_id,
            "dealer_id": dealer_id,
            "my_chips": my_chips,
            "action": action,
        })
        if (i + 1) % 200 == 0:
            dt = time.time() - t0
            print(f"  collected {len(records)}/{n_scenarios} "
                  f"({oracle.calls} queries, {dt:.1f}s)")

    oracle.close()
    with open(out_path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {len(records)} records to {out_path} "
          f"({time.time()-t0:.1f}s, {oracle.calls} queries)")
    return records


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("-n", "--n-scenarios", type=int, default=2000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default=None)
    p.add_argument("--oracle", default=None,
                   help="path to oracle bot main.py (default: claude_v279)")
    args = p.parse_args()
    if args.oracle:
        V279_PATH = os.path.abspath(args.oracle)
    collect(args.n_scenarios, args.seed, args.out)
