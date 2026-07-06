"""Combo-weighted opponent range model.

Instead of a coarse preflop-strength bucket (equity._preflop_strength_rank),
this module produces an explicit *combo list* for the opponent, calibrated
to what a strong reg (like claude_v279) would actually hold given the
street-by-street action history.

The model is built from a request's ``history`` (the same data the engine
gives us each decision). At each decision point we:

1. Narrow the opponent's preflop range based on their preflop action
   (open-limp / open-raise / call-vs-raise / 3-bet / 4-bet / fold).
2. Narrow further on each postflop street based on the opponent's action
   (bet/raise -> stronger range; check/call -> wider/capped range).

The result is a set of combo "key strings" (e.g. "AKs", "TT", "76s") plus
a weight map used for rejection sampling during equity estimation.

This is the missing piece vs claude_v279: it does combo-weighted Monte-Carlo
by pinpointing the opponent's range from action history.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .cards import card_rank, card_suit, full_deck

# ---------------------------------------------------------------------------
# Preflop combo classification
# ---------------------------------------------------------------------------

# A "combo key" is a short string like "AA", "AKs", "AKo", "76s", "T9o".
# We classify each of the 1326 preflop combos into one of the standard
# opening buckets used by HU NLHE regs.


def _combo_key(c1: int, c2: int) -> str:
    r1, r2 = card_rank(c1), card_rank(c2)
    s1, s2 = card_suit(c1), card_suit(c2)
    high, low = max(r1, r2), min(r1, r2)
    ranks = "23456789TJQKA"
    rh, rl = ranks[high - 2], ranks[low - 2]
    if r1 == r2:
        return f"{rh}{rh}"
    if s1 == s2:
        return f"{rh}{rl}s"
    return f"{rh}{rl}o"


# ---------------------------------------------------------------------------
# Preflop range definitions (HU NLHE, calibrated to a strong reg)
# ---------------------------------------------------------------------------
# These are the combos a reg will *play* in each preflop spot. Offsuit
# junk (K2o-K8o, Q2o-Q8o, J4o-J9o, etc.) is excluded; speculative hands
# (small pairs, suited connectors/gappers, suited aces/king) are included.

def _all_combos():
    """Iterate over all 1326 preflop combos as (c1, c2, key)."""
    deck = full_deck()
    seen = set()
    for i in range(len(deck)):
        for j in range(i + 1, len(deck)):
            c1, c2 = deck[i], deck[j]
            key = (c1, c2) if c1 < c2 else (c2, c1)
            if key in seen:
                continue
            seen.add(key)
            yield c1, c2, _combo_key(c1, c2)


# Standard HU SB open range (~58% of hands): everything except offsuit junk.
# A strong SB opener plays: all pairs, all suited, A2o+, K9o+, Q9o+, J9o+, T9o.
_SB_OPEN_KEYS: Optional[set] = None


def _sb_open_keys() -> set:
    global _SB_OPEN_KEYS
    if _SB_OPEN_KEYS is None:
        keys = set()
        for _, _, k in _all_combos():
            if _is_sb_open(k):
                keys.add(k)
        _SB_OPEN_KEYS = keys
    return _SB_OPEN_KEYS


def _is_sb_open(key: str) -> bool:
    """~58% HU SB opening range."""
    if len(key) == 2:  # pair
        return True
    suited = key.endswith("s")
    rh, rl = key[0], key[1]
    ranks = "23456789TJQKA"
    hi, lo = ranks.index(rh) + 2, ranks.index(rl) + 2
    if suited:
        return True  # all suited is opened by aggressive HU SB
    # Offsuit: play A2o+, K9o+, Q9o+, J9o+, T9o (and 76o-class connectors).
    if hi == 14:
        return True
    if hi == 13 and lo >= 9:
        return True
    if hi == 12 and lo >= 9:
        return True
    if hi == 11 and lo >= 9:
        return True
    if hi == 10 and lo >= 9:
        return True
    if hi == 9 and lo == 8:
        return True
    if hi == 8 and lo == 7:
        return True
    return False


# BB vs SB limp (~88%): BB defends very wide vs a limp.
def _is_bb_vs_limp(key: str) -> bool:
    if len(key) == 2:
        return True
    suited = key.endswith("s")
    rh, rl = key[0], key[1]
    ranks = "23456789TJQKA"
    hi, lo = ranks.index(rh) + 2, ranks.index(rl) + 2
    if suited:
        return True
    # Offsuit: nearly everything (BB getting 3:1 on a limp).
    return not (hi <= 8 and lo <= 5 and (hi - lo) >= 3)


# BB vs SB open-raise (~40%): defend with a 3-bet/call range.
def _is_bb_vs_raise(key: str) -> bool:
    if len(key) == 2:
        return True
    suited = key.endswith("s")
    rh, rl = key[0], key[1]
    ranks = "23456789TJQKA"
    hi, lo = ranks.index(rh) + 2, ranks.index(rl) + 2
    if suited:
        if hi == 14:
            return True
        if hi == 13 and lo >= 2:
            return True
        if hi == 12 and lo >= 4:
            return True
        if hi == 11 and lo >= 6:
            return True
        if (hi - lo) <= 3 and hi >= 7:
            return True
        return False
    # Offsuit
    if hi == 14:
        return lo >= 5
    if hi == 13:
        return lo >= 9
    if hi == 12:
        return lo >= 10
    if hi == 11:
        return lo >= 10
    return False


# BB vs 3-bet (~25%): defend only strong.
def _is_bb_vs_3bet(key: str) -> bool:
    if len(key) == 2:
        r = key[0]
        return r in "789TJQKA"
    suited = key.endswith("s")
    rh, rl = key[0], key[1]
    ranks = "23456789TJQKA"
    hi, lo = ranks.index(rh) + 2, ranks.index(rl) + 2
    if hi == 14:
        return True if suited else lo >= 8
    if hi == 13:
        return (lo >= 10) or (suited and lo >= 8)
    if hi == 12 and suited and lo >= 10:
        return True
    return False


# ---------------------------------------------------------------------------
# Action-derived range narrowing
# ---------------------------------------------------------------------------

class RangeModel:
    """Holds the opponent's current combo range as a set of combo keys."""

    def __init__(self):
        # ``keys`` is the set of combo-key strings still in the range.
        # ``full_range`` (no narrowing) is the entire 1326-combo space.
        self.keys: Optional[set] = None  # None = unconstrained
        # When set, equity sampling should reject combos NOT in this set.

    def narrow_to_preflop(self, action_label: str):
        """Narrow the range based on the opponent's preflop action.

        ``action_label`` is one of: 'sb_open', 'bb_vs_limp', 'bb_vs_raise',
        'bb_vs_3bet', 'sb_vs_3bet', 'limp_call', 'caller'.
        """
        if action_label == "sb_open":
            self.keys = _sb_open_keys().copy()
        elif action_label == "bb_vs_limp":
            self.keys = set(k for _, _, k in _all_combos() if _is_bb_vs_limp(k))
        elif action_label in ("bb_vs_raise", "sb_vs_limp_raise"):
            self.keys = set(k for _, _, k in _all_combos() if _is_bb_vs_raise(k))
        elif action_label in ("bb_vs_3bet", "sb_vs_3bet"):
            self.keys = set(k for _, _, k in _all_combos() if _is_bb_vs_3bet(k))
        elif action_label == "limp_call":
            # SB limper who calls a raise: wide, capped-ish.
            self.keys = _sb_open_keys().copy()
        else:
            # Unknown: leave unconstrained.
            pass

    def narrow_postflop_bet(self, street: int, bet_size_bb: float):
        """Narrow the range when the opponent bets/raises postflop.

        A bet implies a stronger subset of their prior range. We keep only
        the top ~60-70% of preflop keys (by rough strength) so the opponent's
        betting range is value-weighted.
        """
        prior = self.keys
        if prior is None:
            prior = _sb_open_keys().copy()
        # Keep combos above a strength threshold that scales with bet size:
        # big bets keep only strong; small bets keep wider.
        if bet_size_bb >= 1.5:        # pot-sized or bigger
            thr = 0.55
        elif bet_size_bb >= 0.66:     # 2/3 pot
            thr = 0.50
        else:                         # small stab
            thr = 0.43
        self.keys = set(k for k in prior if _combo_strength_score(k) >= thr)

    def narrow_postflop_call(self, street: int, bet_size_bb: float):
        """Narrow when opponent calls a bet: capped range (rarely traps)."""
        prior = self.keys
        if prior is None:
            prior = _sb_open_keys().copy()
        # A caller is capped at ~top 75% (they'd raise the very top sometimes)
        # but includes draws / mid hands.
        thr = 0.38
        self.keys = set(k for k in prior if _combo_strength_score(k) >= thr)

    def is_unconstrained(self) -> bool:
        return self.keys is None


# ---------------------------------------------------------------------------
# Combo strength scoring (for postflop narrowing)
# ---------------------------------------------------------------------------

def _combo_strength_score(key: str) -> float:
    """Return a 0..1 preflop strength score for a combo key."""
    if len(key) == 2:
        # pair
        ranks = "23456789TJQKA"
        r = ranks.index(key[0]) + 2
        # 22=0.50, AA=0.86 linear
        return 0.50 + (r - 2) * (0.86 - 0.50) / 12
    suited = key.endswith("s")
    rh, rl = key[0], key[1]
    ranks = "23456789TJQKA"
    hi, lo = ranks.index(rh) + 2, ranks.index(rl) + 2
    # Base by high card
    base = {14: 0.55, 13: 0.46, 12: 0.40, 11: 0.35,
            10: 0.31, 9: 0.28, 8: 0.26, 7: 0.24}.get(hi, 0.22)
    # Add kicker / suited bonus
    base += (lo - 2) * 0.012
    if suited:
        base += 0.06
    # Connector bonus
    gap = hi - lo - 1
    if gap == 0:
        base += 0.03
    elif gap == 1:
        base += 0.015
    return max(0.0, min(1.0, base))


# ---------------------------------------------------------------------------
# Build a RangeModel from a request's history
# ---------------------------------------------------------------------------

def build_range_from_history(history: List[dict], my_id: int) -> RangeModel:
    """Construct the opponent's range by replaying the action history.

    ``history`` is the per-hand action list (each entry has ``round``,
    ``player_id``, ``action_type``, ``action``). ``my_id`` is the hero id;
    the opponent is ``1 - my_id``.
    """
    opp_id = 1 - my_id
    model = RangeModel()

    # --- Preflop phase ---
    # Determine opponent's preflop role/action.
    pf_opp_actions = [r for r in history
                      if r.get("round", 0) == 0 and r.get("player_id") == opp_id]
    pf_hero_actions = [r for r in history
                       if r.get("round", 0) == 0 and r.get("player_id") == my_id]
    hero_raised_pf = any(r.get("action_type") == "raise" for r in pf_hero_actions)
    opp_raised_pf = any(r.get("action_type") == "raise" for r in pf_opp_actions)

    # Did hero 3-bet? Did opp 3-bet?
    pf_raises = [r for r in history
                 if r.get("round", 0) == 0 and r.get("action_type") == "raise"]
    n_pf_raises = len(pf_raises)

    if opp_raised_pf and n_pf_raises == 1:
        # Opp opened (SB raise or BB iso).
        model.narrow_to_preflop("sb_open")
    elif opp_raised_pf and n_pf_raises >= 2:
        # There was a raise war; if opp put in the last raise, they 3bet+.
        last_raise = pf_raises[-1]
        if last_raise.get("player_id") == opp_id:
            model.narrow_to_preflop("bb_vs_3bet")
        else:
            # Opp called hero's 3-bet.
            model.narrow_to_preflop("bb_vs_3bet")
    elif hero_raised_pf and not opp_raised_pf:
        # Hero raised, opp called (BB vs raise defense, or SB limp-call).
        model.narrow_to_preflop("bb_vs_raise")
    elif pf_opp_actions:
        # Opp limped / called a limp.
        opp_first = pf_opp_actions[0].get("action_type")
        if opp_first in ("call", "check"):
            model.narrow_to_preflop("bb_vs_limp")
    # else: no preflop action yet — leave unconstrained.

    # --- Postflop phases ---
    cur_round = 0
    for rec in history:
        r = rec.get("round", 0)
        if r != cur_round and r > 0:
            cur_round = r
        if rec.get("player_id") != opp_id:
            continue
        at = rec.get("action_type")
        act = rec.get("action")
        if at == "raise" and act is not None:
            # action is raise-to-total; size in BB roughly = act / 100
            model.narrow_postflop_bet(cur_round, (act or 0) / 100.0)
        elif at == "allin":
            model.narrow_postflop_bet(cur_round, 100.0)
        elif at == "call":
            # Approximate the bet they called (we don't track exact size).
            model.narrow_postflop_call(cur_round, 0.66)
        # check/fold don't narrow further.

    return model


# ---------------------------------------------------------------------------
# Combo key matching for rejection sampling
# ---------------------------------------------------------------------------

def combo_in_range(c1: int, c2: int, model: RangeModel) -> bool:
    """True if a sampled opponent combo is within the model's range."""
    if model is None or model.is_unconstrained():
        return True
    return _combo_key(c1, c2) in model.keys


if __name__ == "__main__":  # pragma: no cover - sanity
    # Verify range sizes.
    sb = _sb_open_keys()
    print(f"SB open range: {len(sb)} combo-keys")
    bb_r = set(k for _, _, k in _all_combos() if _is_bb_vs_raise(k))
    print(f"BB vs raise: {len(bb_r)} combo-keys")
    bb_3b = set(k for _, _, k in _all_combos() if _is_bb_vs_3bet(k))
    print(f"BB vs 3bet: {len(bb_3b)} combo-keys")
    # Sample combo keys in SB open
    print("SB open includes AKs?", "AKs" in sb, "72o?", "72o" in sb, "AA?", "AA" in sb)
    # Strength scores
    for k in ["AA", "KK", "AKs", "AKo", "QTs", "76s", "JTo", "72o", "K2o"]:
        print(f"  {k}: strength={_combo_strength_score(k):.3f}")
    # Range build from history: opp opens SB, bets flop.
    hist = [
        {"round": 0, "player_id": 1, "action": 200, "action_type": "raise"},
        {"round": 0, "player_id": 0, "action": 0, "action_type": "call"},
        {"round": 1, "player_id": 1, "action": 300, "action_type": "raise"},
    ]
    m = build_range_from_history(hist, my_id=0)
    print(f"after open+flop bet: range size = {len(m.keys) if m.keys else 'none'}")
