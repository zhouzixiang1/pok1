"""Monte-Carlo equity estimation.

This is the core of the new "pure simulation" school: instead of using a
hand-strength lookup table (Chen formula etc.), we estimate the probability
of winning by randomly completing the board + opponent hole cards and
counting wins/ties.

The implementation is deliberately self-contained (stdlib only) and uses a
tight inner loop so that a few thousand trials fit comfortably inside the
60s decision budget even on a single core.
"""

from __future__ import annotations

import random
from typing import List, Sequence, Tuple

from .cards import evaluate_best, full_deck


def estimate_equity(my_cards: Sequence[int],
                    public_cards: Sequence[int],
                    n_sim: int = 1500,
                    rng: random.Random | None = None,
                    opponents: int = 1) -> Tuple[float, float]:
    """Estimate win and tie probability.

    Returns ``(win_rate, tie_rate)`` each in ``[0, 1]``. ``win_rate`` counts
    half-credit for ties is *not* included — the policy decides how to treat
    ties; the two are reported separately so callers can compute
    ``win + 0.5 * tie`` (expected credit) when desired.

    Parameters
    ----------
    my_cards : two hole cards
    public_cards : 0/3/4/5 board cards
    n_sim : number of random completions
    opponents : number of opposing hands (heads-up: 1)
    """
    if rng is None:
        rng = random.Random()

    my_cards = list(my_cards)
    public_cards = list(public_cards)
    n_board_missing = 5 - len(public_cards)
    n_opp_cards = 2 * opponents

    # Pre-compute the candidate pool (deck without known cards).
    known = set(my_cards) | set(public_cards)
    pool = [c for c in full_deck() if c not in known]

    wins = 0
    ties = 0
    my_base = my_cards + public_cards  # partial; we append during loop

    for _ in range(n_sim):
        # Sample without replacement. random.sample is C-speed and fast.
        draw = rng.sample(pool, n_board_missing + n_opp_cards)
        board_extra = draw[:n_board_missing]
        opp_holes = draw[n_board_missing:]

        my_hand = my_base + board_extra
        my_score = evaluate_best(my_hand)

        board = public_cards + board_extra
        best_opp = None
        for o in range(opponents):
            opp_hand = opp_holes[o * 2:o * 2 + 2] + board
            opp_score = evaluate_best(opp_hand)
            if best_opp is None or opp_score > best_opp:
                best_opp = opp_score

        if my_score > best_opp:
            wins += 1
        elif my_score == best_opp:
            ties += 1

    return wins / n_sim, ties / n_sim


def expected_credit(my_cards: Sequence[int],
                    public_cards: Sequence[int],
                    n_sim: int = 1500,
                    rng: random.Random | None = None,
                    opponents: int = 1) -> float:
    """Convenience: win_rate + 0.5 * tie_rate."""
    w, t = estimate_equity(my_cards, public_cards, n_sim, rng, opponents)
    return w + 0.5 * t


# ---------------------------------------------------------------------------
# Range-restricted equity (opponent sampling is filtered by hand strength)
# ---------------------------------------------------------------------------

def _preflop_strength_rank(c1: int, c2: int) -> int:
    """Coarse preflop bucket 0..8 (higher = stronger) for range filtering.

    Cheap, no external dependency. Used only for opponent-range rejection
    sampling so precise calibration is unnecessary.
    """
    r1, r2 = c1 // 4 + 2, c2 // 4 + 2
    high, low = max(r1, r2), min(r1, r2)
    suited = (c1 % 4) == (c2 % 4)
    if r1 == r2:
        if high >= 11: return 8            # JJ+
        if high >= 8: return 7             # 88-TT
        return 5                           # 22-77
    if high == 14:
        if low >= 12: return 8             # AK AQ
        if low >= 9 or suited: return 6    # AT+ / A8s+
        if low >= 5: return 4              # A5-A9
        return 2
    if high == 13:
        if low >= 11 or (suited and low >= 9): return 6  # KJ+ / KTs+
        if low >= 9: return 5              # KT K9
        if suited: return 3
        return 1
    if high == 12:
        if low >= 10 or (suited and low >= 9): return 5  # QT+ Q9s+
        if low >= 9: return 4
        if suited: return 2
        return 1
    if high == 11:
        if (suited and low >= 8) or low >= 10: return 4  # J8s+ JTo
        if suited: return 2
        return 0
    if suited and (high - low) <= 2 and high >= 7:
        return 3                            # suited connectors/gappers
    if suited:
        return 1
    if (high - low) == 1 and high >= 8:
        return 2                            # offsuit connectors
    return 0


def estimate_equity_ranged(my_cards: Sequence[int],
                            public_cards: Sequence[int],
                            n_sim: int = 1500,
                            rng: random.Random | None = None,
                            min_opp_strength: int = 0,
                            max_reject: int = 6,
                            range_model=None) -> Tuple[float, float]:
    """Equity with opponent hand range filtering.

    Two filtering modes:
    - Legacy bucket: reject combos with preflop-strength bucket below
      ``min_opp_strength``.
    - Combo range (preferred): if ``range_model`` (a zcode.range_model.
      RangeModel) is supplied, reject any sampled combo whose combo key is
      not in the model's set. This mirrors claude_v279's combo-weighted
      range Monte-Carlo.

    ``max_reject`` caps per-trial rejection attempts so the loop always
    terminates (falling back to the last candidate).
    """
    if rng is None:
        rng = random.Random()

    my_cards = list(my_cards)
    public_cards = list(public_cards)
    n_board_missing = 5 - len(public_cards)

    known = set(my_cards) | set(public_cards)
    pool = [c for c in full_deck() if c not in known]

    # Pre-build the combo-key matching function.
    use_range = range_model is not None and not range_model.is_unconstrained()
    if use_range:
        from .range_model import combo_in_range as _in_range
        # Cache the combo-key function to avoid repeated import overhead.
        from .range_model import _combo_key as _ckey

    wins = 0
    ties = 0
    my_base = my_cards + public_cards

    for _ in range(n_sim):
        # Board first (always uniform).
        board_extra = rng.sample(pool, n_board_missing) if n_board_missing else []
        used = set(board_extra)
        opp_pool = [c for c in pool if c not in used]
        opp_holes = None
        for _try in range(max_reject + 1):
            cand = rng.sample(opp_pool, 2)
            if use_range:
                if _in_range(cand[0], cand[1], range_model):
                    opp_holes = cand
                    break
            else:
                if _preflop_strength_rank(cand[0], cand[1]) >= min_opp_strength:
                    opp_holes = cand
                    break
        if opp_holes is None:
            opp_holes = cand  # last candidate even if below threshold

        my_hand = my_base + board_extra
        my_score = evaluate_best(my_hand)
        opp_hand = opp_holes + public_cards + board_extra
        opp_score = evaluate_best(opp_hand)

        if my_score > opp_score:
            wins += 1
        elif my_score == opp_score:
            ties += 1

    return wins / n_sim, ties / n_sim


if __name__ == "__main__":  # pragma: no cover - manual sanity check
    import time
    rng = random.Random(12345)
    # Pocket aces vs one opponent on an empty board.
    t0 = time.time()
    w, t = estimate_equity([48, 49], [], n_sim=3000, rng=rng)
    dt = time.time() - t0
    print(f"AA preflop: win={w:.3f} tie={t:.4f}  (expect ~0.85 win)  "
          f"{dt:.2f}s for 3000 sims")
    # AA on a K-7-2 rainbow flop should be even stronger.
    w, t = estimate_equity([48, 49], [44, 12, 0], n_sim=3000, rng=rng)
    print(f"AA on K72: win={w:.3f} tie={t:.4f}")
    # 72o preflop should be weak.
    w, t = estimate_equity([0, 6], [], n_sim=3000, rng=rng)  # 2h,4d-ish offsuit low
    print(f"low offsuit: win={w:.3f} (expect ~0.35-0.40)")
