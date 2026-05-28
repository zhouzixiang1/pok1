"""
Pot odds, implied odds, hand value classification, and river bluff optimization.
"""
from card_utils import clamp, card_suit, card_number, evaluate_best
from hand_evaluation import made_hand_metric, pair_board_profile, bet_size_bucket


# ---------------------------------------------------------------------------
# A. Pot Odds Calculator
# ---------------------------------------------------------------------------

def calculate_pot_odds(pot_size, bet_to_call):
    """Return the equity percentage needed to break even on a call."""
    if bet_to_call <= 0:
        return 0.0
    total_pot_after_call = pot_size + bet_to_call
    return bet_to_call / total_pot_after_call


# ---------------------------------------------------------------------------
# B. Implied Odds Estimator
# ---------------------------------------------------------------------------

def estimate_implied_odds(hole_cards, board_cards, pot_size, opponent_stack, draw_info=None):
    """
    Estimate the effective pot odds when hitting a draw.
    Returns implied_pot_ratio: the ratio of (pot + expected_future_bet) to current call cost.
    """
    if draw_info is None:
        from draw_analysis import draw_profile
        draw_info = draw_profile(hole_cards, board_cards)

    if draw_info["type"] == "none":
        return 0.0

    # Estimate outs and hit probability
    draw_type = draw_info["type"]
    if draw_type == "combo_draw":
        outs = 12
        hit_prob = 0.44
    elif draw_type == "nut_flush_draw":
        outs = 9
        hit_prob = 0.35
    elif draw_type == "flush_draw":
        outs = 9
        hit_prob = 0.35
    elif draw_type == "open_ended_straight_draw":
        outs = 8
        hit_prob = 0.31
    elif draw_type == "double_gutshot":
        outs = 8
        hit_prob = 0.31
    elif draw_type == "gutshot":
        outs = 4
        hit_prob = 0.17
    else:
        return 0.0

    # Adjust for non-nut draws (won't always win when hitting)
    if draw_info.get("flush_draw") and not draw_info.get("nut_flush_draw"):
        hit_prob *= 0.85  # Sometimes lose to higher flush
    if draw_info.get("straight_draw") == "gutshot":
        hit_prob *= 0.90  # Sometimes lose to flush/straight redraws

    # Estimate future value: assume we can extract ~50% pot on next street if we hit
    future_extract = min(opponent_stack, pot_size * 0.50)
    effective_pot = pot_size + future_extract

    return {
        "outs": outs,
        "hit_prob": hit_prob,
        "implied_pot": effective_pot,
        "draw_type": draw_type,
    }


# ---------------------------------------------------------------------------
# C. Hand Value Classification (Value Betting Tier System)
# ---------------------------------------------------------------------------

def classify_hand_value(hole_cards, board_cards, equity):
    """
    Classify the current hand into a value tier for bet sizing decisions.

    Returns:
        dict with keys:
            'tier': one of 'monster', 'strong', 'medium', 'weak', 'draw'
            'sizing': suggested pot fraction for value betting
            'desc': human-readable description
    """
    if len(board_cards) < 3:
        # Preflop: classify by equity only
        if equity >= 0.70:
            return {"tier": "monster", "sizing": 0.75, "desc": "preflop monster"}
        if equity >= 0.58:
            return {"tier": "strong", "sizing": 0.55, "desc": "preflop strong"}
        if equity >= 0.45:
            return {"tier": "medium", "sizing": 0.35, "desc": "preflop medium"}
        return {"tier": "weak", "sizing": 0.0, "desc": "preflop weak"}

    made = made_hand_metric(hole_cards, board_cards)
    score = evaluate_best(hole_cards + board_cards)
    hand_class = score[0]
    pair_prof = pair_board_profile(hole_cards, board_cards)

    # Check for draws first (semi-bluff territory)
    from draw_analysis import draw_profile
    draw_info = draw_profile(hole_cards, board_cards)
    has_strong_draw = (
        draw_info["quality"] >= 0.15
        or draw_info.get("combo_draw", False)
        or draw_info.get("nut_flush_draw", False)
    )

    if hand_class >= 6:
        # Full house or better: monster
        return {"tier": "monster", "sizing": 0.85, "desc": "quads/full house"}
    if hand_class == 5:
        flush_high = pair_prof.get("kicker_rank", 0)
        if flush_high >= 12:
            return {"tier": "monster", "sizing": 0.80, "desc": "nut/near-nut flush"}
        return {"tier": "strong", "sizing": 0.60, "desc": "non-nut flush"}
    if hand_class == 4:
        # Straight
        if equity >= 0.60:
            return {"tier": "strong", "sizing": 0.65, "desc": "strong straight"}
        return {"tier": "medium", "sizing": 0.45, "desc": "straight on scary board"}
    if hand_class == 3:
        # Trips / set
        if pair_prof.get("uses_hole_card", False):
            return {"tier": "monster", "sizing": 0.80, "desc": "set"}
        return {"tier": "strong", "sizing": 0.65, "desc": "trips"}
    if hand_class == 2:
        # Two pair
        pair_type = pair_prof.get("pair_type", "none")
        if pair_type in ("top_pair", "overpair") or made >= 0.50:
            return {"tier": "strong", "sizing": 0.60, "desc": "strong two pair"}
        return {"tier": "medium", "sizing": 0.40, "desc": "two pair"}
    if hand_class == 1:
        pair_type = pair_prof.get("pair_type", "none")
        if pair_type == "overpair":
            return {"tier": "strong", "sizing": 0.55, "desc": "overpair"}
        if pair_type == "top_pair":
            kicker = pair_prof.get("kicker_rank", 0)
            if kicker >= 11:
                return {"tier": "strong", "sizing": 0.55, "desc": "top pair good kicker"}
            return {"tier": "medium", "sizing": 0.35, "desc": "top pair weak kicker"}
        if pair_type == "middle_pair":
            return {"tier": "medium", "sizing": 0.30, "desc": "middle pair"}
        if pair_type in ("bottom_pair", "underpair", "board_pair"):
            return {"tier": "weak", "sizing": 0.0, "desc": "weak pair"}

    # No made hand — check for draws
    if has_strong_draw:
        return {"tier": "draw", "sizing": 0.45, "desc": "strong draw (semi-bluff)"}

    # Pure air
    if equity >= 0.40:
        return {"tier": "weak", "sizing": 0.0, "desc": "weak showdown equity"}
    return {"tier": "weak", "sizing": 0.0, "desc": "air"}


# ---------------------------------------------------------------------------
# D. River Bluff / Catch Balance
# ---------------------------------------------------------------------------

def river_bluff_frequency(bet_size, pot_size):
    """
    Calculate the optimal bluffing frequency based on bet size.
    In game theory, bluff_freq = bet / (bet + pot) to make opponent indifferent.
    """
    if bet_size <= 0 or pot_size <= 0:
        return 0.0
    return bet_size / (bet_size + pot_size)


def detect_scare_card(river_card, board_cards):
    """
    Detect if the river card is a 'scare card' that completes likely draws.
    Returns a dict with scare_level (0.0-1.0) and type description.
    """
    if len(board_cards) < 4:
        return {"scare_level": 0.0, "type": "none"}

    all_cards = board_cards + [river_card]
    river_rank = card_number(river_card)
    river_suit = card_suit(river_card)

    scare_level = 0.0
    scare_type = "none"

    # Check if river completes a flush
    board_suits = [card_suit(c) for c in board_cards]
    suit_counts = {}
    for suit in board_suits:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    max_suit_before = max(suit_counts.values())

    if max_suit_before == 3:
        # 3-flush on board before river
        flush_suit = max(suit_counts, key=suit_counts.get)
        if river_suit == flush_suit:
            scare_level = 0.80
            scare_type = "flush_complete"
        else:
            scare_level = 0.20
            scare_type = "flush_miss"

    # Check if river completes a straight
    all_ranks = set(card_number(c) for c in all_cards)
    for start in range(1, 11):
        window = set(range(start, start + 5))
        if len(all_ranks & window) == 5:
            board_ranks_before = set(card_number(c) for c in board_cards)
            if len(board_ranks_before & window) == 4:
                scare_level = max(scare_level, 0.70)
                scare_type = "straight_complete"
            break

    # Check if river pairs the board (potential full house)
    board_rank_counts = {}
    for c in board_cards:
        rank = card_number(c)
        board_rank_counts[rank] = board_rank_counts.get(rank, 0) + 1
    if river_rank in board_rank_counts:
        if board_rank_counts[river_rank] >= 2:
            scare_level = max(scare_level, 0.60)
            scare_type = "trips_complete"
        elif board_rank_counts[river_rank] == 1:
            scare_level = max(scare_level, 0.30)
            scare_type = "pair_complete"

    # High river card
    if river_rank >= 13 and scare_level < 0.30:
        scare_level = max(scare_level, 0.15)
        scare_type = "high_card"

    return {"scare_level": clamp(scare_level, 0.0, 1.0), "type": scare_type}


def river_blocker_score(hole_cards, board_cards):
    """
    Evaluate how much our hole cards block opponent's strong combos on the river.
    Returns 0.0-1.0 score where higher = better for bluffing.
    """
    if len(board_cards) < 5:
        return 0.0

    hole_ranks = [card_number(c) for c in hole_cards]
    hole_suits = [card_suit(c) for c in hole_cards]
    score = 0.0

    # Board suit analysis
    board_suits = [card_suit(c) for c in board_cards]
    suit_counts = {}
    for suit in board_suits:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    flush_suit = max(suit_counts, key=suit_counts.get)
    flush_count = suit_counts[flush_suit]

    # Block flush combos
    if flush_count >= 3:
        for c in hole_cards:
            if card_suit(c) == flush_suit and card_number(c) >= 12:
                score += 0.25
            elif card_suit(c) == flush_suit:
                score += 0.10

    # Block straight combos
    all_ranks = set(card_number(c) for c in board_cards)
    high_gap_cards = [r for r in hole_ranks if any(
        abs(r - br) <= 2 for br in all_ranks if br >= 10
    )]
    score += 0.08 * len(high_gap_cards)

    # Block pairs/trips with high cards
    if 14 in hole_ranks:
        score += 0.12
    if 13 in hole_ranks:
        score += 0.06

    return clamp(score, 0.0, 1.0)


# ---------------------------------------------------------------------------
# E. Position-Aware Adjustment
# ---------------------------------------------------------------------------

def position_adjustment(has_position, round_idx, is_aggressor=False):
    """
    Return threshold adjustments based on position.
    In position: can play wider, more aggressive.
    Out of position: tighter, more check-calling.
    """
    adj = {
        "threshold_delta": 0.0,
        "aggression_delta": 0.0,
        "bluff_delta": 0.0,
        "call_delta": 0.0,
    }

    if has_position:
        adj["threshold_delta"] = -0.015  # Wider range
        adj["aggression_delta"] = 0.02   # More aggressive
        adj["bluff_delta"] = 0.03        # More float/bluff
        adj["call_delta"] = -0.01        # Can call lighter
    else:
        adj["threshold_delta"] = 0.02    # Tighter range
        adj["aggression_delta"] = -0.02  # Less aggressive
        adj["bluff_delta"] = -0.02       # Less bluffing
        adj["call_delta"] = 0.01         # Need stronger to call

        # Out of position: more check-raise bluffing
        if round_idx >= 2 and not is_aggressor:
            adj["aggression_delta"] += 0.01

    return adj
