"""Botzone / local-engine protocol parsing.

Reconstructs the full betting state from a single ``request`` dict.

Key correctness points (verified against engine/judge.py, fixing bugs that
exist in bots/bot5/state.py and bots/bot6/state.py):

* The ``action`` integer for a raise is a **raise-to-total** (the round
  contribution target), NOT a delta. We set ``round_contrib[pid] = action``
  and derive the chip delta from the difference vs the previous value.
* Fold history records may omit the ``round`` key; we use ``.get``.
* Blinds are not present in ``history``; we pre-seed them.
* SB == dealer_id, BB == (dealer_id + 1) % 2 in the local 2-player engine.
* Post-flop the BB (non-dealer) acts first.

The function returns a compact ``GameState`` with everything the policy needs
to make an EV-based decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

INITIAL_CHIPS = 20000
SMALL_BLIND = 50
BIG_BLIND = 100


@dataclass
class GameState:
    my_id: int
    opp_id: int
    dealer_id: int
    is_sb: bool
    my_cards: List[int]
    public_cards: List[int]
    my_chips: int
    opp_chips: int
    pot: int
    to_call: int          # chips needed to call right now
    my_round_bet: int     # my contribution this betting round
    opp_round_bet: int
    round_bet: int        # current max round bet
    min_raise_to: int     # minimum legal raise-to-total
    betting_round: int    # 0 preflop, 1 flop, 2 turn, 3 river
    hand: int
    max_hand: int
    total_win_chips: List[int]
    my_allin: bool
    opp_allin: bool
    opp_folded: bool
    i_folded: bool
    actions_this_round: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


def _round_from_public(public_cards: List[int]) -> int:
    n = len(public_cards)
    if n == 0:
        return 0
    if n == 3:
        return 1
    if n == 4:
        return 2
    return 3


def reconstruct_state(req: Dict[str, Any]) -> GameState:
    """Build a GameState from the latest request payload (``requests[-1]``)."""
    my_id: int = int(req["my_id"])
    dealer_id: int = int(req["dealer_id"])
    opp_id = 1 - my_id
    sb = dealer_id            # local engine: dealer is SB in heads-up
    bb = 1 - dealer_id

    my_cards: List[int] = [int(c) for c in req["my_cards"]]
    public_cards: List[int] = [int(c) for c in req.get("public_cards", [])]

    stacks = [INITIAL_CHIPS, INITIAL_CHIPS]
    committed = [0, 0]                 # total invested this hand
    round_contrib = [0, 0]             # invested this betting round
    round_contrib[sb] = SMALL_BLIND
    round_contrib[bb] = BIG_BLIND
    stacks[sb] -= SMALL_BLIND
    stacks[bb] -= BIG_BLIND
    committed[sb] += SMALL_BLIND
    committed[bb] += BIG_BLIND

    round_bet = BIG_BLIND              # current max round contribution
    last_raise_to = BIG_BLIND          # last raise-to-total this round
    baseline = BIG_BLIND               # min opening raise-to (preflop = 2BB)

    alive = [True, True]
    allin = [False, False]
    folded = [False, False]

    public_round = _round_from_public(public_cards)
    cur_round = 0
    actions_this_round = 0

    history: List[Dict[str, Any]] = req.get("history", [])
    for rec in history:
        rround = rec.get("round", cur_round)
        if rround != cur_round:
            # New betting round: reset round-contributions.
            cur_round = rround
            round_contrib = [0, 0]
            round_bet = 0
            baseline = BIG_BLIND // 2 if cur_round > 0 else BIG_BLIND
            last_raise_to = baseline
            actions_this_round = 0

        pid = int(rec["player_id"])
        at = rec.get("action_type")
        act = rec.get("action")

        if pid < 0 or pid > 1:
            continue

        if at == "fold":
            folded[pid] = True
            alive[pid] = False
            continue
        if not alive[pid] or allin[pid]:
            continue

        actions_this_round += 1

        if at == "allin":
            add = stacks[pid]
            stacks[pid] = 0
            committed[pid] += add
            round_contrib[pid] += add
            allin[pid] = True
            if round_contrib[pid] > round_bet:
                round_bet = round_contrib[pid]
                last_raise_to = round_bet
        elif at in ("call", "check"):
            need = max(0, round_bet - round_contrib[pid])
            need = min(need, stacks[pid])
            stacks[pid] -= need
            committed[pid] += need
            round_contrib[pid] += need
        elif at == "raise":
            # ``act`` is a raise-to-total. Compute the delta vs current
            # round contribution, clamp to the player's remaining stack.
            target = max(int(act), round_contrib[pid] + 1)
            delta = target - round_contrib[pid]
            delta = min(delta, stacks[pid])
            stacks[pid] -= delta
            committed[pid] += delta
            round_contrib[pid] += delta
            target = round_contrib[pid]   # may be capped by all-in
            if target > round_bet:
                round_bet = target
                last_raise_to = target

    # Pot is total committed by both players this hand.
    pot = committed[0] + committed[1]

    # If we are on a fresh round with no history record yet, align baseline
    # with the public-card-derived round (post-flop first action).
    if not history and public_round > 0:
        cur_round = public_round
        round_contrib = [0, 0]
        round_bet = 0
        baseline = BIG_BLIND // 2
        last_raise_to = baseline

    # Compute to-call for the hero.
    if allin[my_id] or folded[my_id]:
        my_to_call = 0
    else:
        my_to_call = max(0, round_bet - round_contrib[my_id])

    # Minimum legal raise-to-total for the hero (raise-to-total form).
    if last_raise_to > baseline:
        # Re-raise must be strictly greater than 2x the previous raise-to.
        min_raise_to = last_raise_to * 2 + 1
    else:
        # First raise of the round: must be at least 2x baseline.
        min_raise_to = baseline * 2
    # Cannot raise beyond what puts us all-in; policy will clamp.

    return GameState(
        my_id=my_id,
        opp_id=opp_id,
        dealer_id=dealer_id,
        is_sb=(my_id == dealer_id),
        my_cards=my_cards,
        public_cards=public_cards,
        my_chips=int(req.get("my_chips", stacks[my_id])),
        opp_chips=stacks[opp_id],
        pot=pot,
        to_call=my_to_call,
        my_round_bet=round_contrib[my_id],
        opp_round_bet=round_contrib[opp_id],
        round_bet=round_bet,
        min_raise_to=min_raise_to,
        betting_round=cur_round,
        hand=int(req.get("hand", 0)),
        max_hand=int(req.get("max_hand", 70)),
        total_win_chips=[int(x) for x in req.get("total_win_chips", [0, 0])],
        my_allin=allin[my_id],
        opp_allin=allin[opp_id],
        opp_folded=folded[opp_id],
        i_folded=folded[my_id],
        actions_this_round=actions_this_round,
        raw=req,
    )


if __name__ == "__main__":  # pragma: no cover - ad-hoc verification
    # Construct a synthetic request and check reconstruction.
    # Preflop: SB(dealer=0) is asked. SB has posted 50, BB 100.
    # No history yet.
    req = {
        "num_players": 2,
        "dealer_id": 0,
        "my_id": 0,
        "my_chips": 19950,
        "my_cards": [48, 49],     # AhAd
        "public_cards": [],
        "history": [],
        "hand": 0,
        "max_hand": 70,
        "total_win_chips": [0, 0],
        "total_win_games": [0, 0],
    }
    st = reconstruct_state(req)
    print("preflop SB:", st.to_call, st.pot, st.min_raise_to, st.my_round_bet)
    print("  (expect to_call=50, pot=150, min_raise_to=200, my_round_bet=50)")

    # After SB calls (50), BB checks; flop comes. BB acts first.
    # Simulate flop: BB bets 200, ask hero (SB) with to_call=200.
    req2 = {
        "dealer_id": 0,
        "my_id": 0,
        "my_chips": 19900,
        "my_cards": [48, 49],
        "public_cards": [44, 12, 0],   # K-2-... rainbow-ish
        "history": [
            {"round": 0, "player_id": 0, "action": 0, "action_type": "call"},
            {"round": 0, "player_id": 1, "action": 0, "action_type": "check"},
            {"round": 1, "player_id": 1, "action": 200, "action_type": "raise"},
        ],
        "hand": 0,
        "max_hand": 70,
    }
    st2 = reconstruct_state(req2)
    print("flop facing 200:", st2.to_call, st2.pot, st2.min_raise_to,
          st2.my_round_bet, st2.betting_round)
    print("  (expect to_call=200, pot=350, min_raise_to>=400+, round=1)")
