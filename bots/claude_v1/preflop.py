"""Preflop hand evaluation and strategy."""

from itertools import combinations


def card_rank(card):
    return card // 4 + 2


def card_suit(card):
    return card % 4


def preflop_hand_strength(my_cards):
    """Classify preflop hand strength on a 0.0-1.0 scale.

    Uses a simplified Chen-style formula adapted to the integer card encoding.
    """
    r1, r2 = card_rank(my_cards[0]), card_rank(my_cards[1])
    s1, s2 = card_suit(my_cards[0]), card_suit(my_cards[1])

    high = max(r1, r2)
    low = min(r1, r2)
    gap = high - low
    suited = s1 == s2
    pair = r1 == r2

    score = 0.0

    # Base score from high card (2-14 mapped to 0.0-0.5)
    score += (high - 2) / 24.0
    score += (low - 2) / 48.0

    if pair:
        score += 0.30 + (high - 2) / 36.0
        # Premium pairs
        if high >= 12:  # QQ+
            score += 0.10
    else:
        if suited:
            score += 0.06
        if gap == 1:
            score += 0.05
        elif gap == 2:
            score += 0.03
        elif gap >= 5:
            score -= 0.04

        # Ace bonus
        if high == 14:
            score += 0.04
            if low >= 10:
                score += 0.06  # AJ+

    return max(0.0, min(1.0, score))


# --- Preflop hand categories ---
# Returns one of: "premium", "strong", "playable", "speculative", "weak"
def preflop_category(my_cards):
    score = preflop_hand_strength(my_cards)
    if score >= 0.60:
        return "premium"
    elif score >= 0.40:
        return "strong"
    elif score >= 0.28:
        return "playable"
    elif score >= 0.18:
        return "speculative"
    else:
        return "weak"


def preflop_action(my_cards, state, my_chips, position_is_sb):
    """Return a raw action for preflop play.

    Returns an integer: 0 (call/check), -1 (fold), -2 (all-in), >0 (raise).
    """
    strength = preflop_hand_strength(my_cards)
    category = preflop_category(my_cards)
    to_call = state["to_call"]
    pot = state["pot"]
    big_blind = 100

    # --- Premium hands: always raise ---
    if category == "premium":
        if to_call == 0:
            # Open raise: 3x BB from EP, 2.5x from BTN/SB
            raise_size = big_blind * 3 if not position_is_sb else int(big_blind * 2.5)
            return raise_size
        else:
            # Re-raise (3-bet): pot-sized or 3x the raise
            raise_size = max(int(pot * 0.75), to_call * 3)
            if raise_size >= my_chips:
                return -2  # all-in
            return raise_size

    # --- Strong hands: raise or call ---
    if category == "strong":
        if to_call == 0:
            raise_size = int(big_blind * 2.5)
            return raise_size
        elif to_call <= big_blind * 4:
            return 0  # call
        else:
            # Expensive: call only if we have position
            if position_is_sb:
                return 0 if to_call <= big_blind * 6 else -1
            return 0

    # --- Playable hands ---
    if category == "playable":
        if to_call == 0:
            # Check from BB, limp from SB
            return 0
        elif to_call <= big_blind * 2:
            return 0  # cheap call
        elif to_call <= big_blind * 4 and position_is_sb:
            return 0  # position call
        else:
            return -1  # fold to big raises

    # --- Speculative hands: cheap calls only ---
    if category == "speculative":
        if to_call == 0:
            return 0  # check
        elif to_call <= big_blind * 2:
            return 0  # cheap call
        else:
            return -1

    # --- Weak hands ---
    if to_call == 0:
        return 0  # free check
    return -1  # fold
