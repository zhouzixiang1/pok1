"""
Bluff analysis: blocker bluffs, nutted risk profiles, and bluff eligibility.
"""
from card_utils import clamp, card_suit, card_number, evaluate_best
from state import get_hand_index


def blocker_bluff_profile(hole_cards, public_cards, pair_profile=None, board_texture=None):
    info = {
        "eligible": False,
        "score": 0.0,
        "type": "none",
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        from hand_evaluation import pair_board_profile
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        from board_analysis import board_texture_profile
        board_texture = board_texture_profile(public_cards)

    score = evaluate_best(hole_cards + public_cards)
    if score[0] >= 1 and pair_profile["pair_type"] != "board_pair":
        return info

    board_suits = [card_suit(card) for card in public_cards]
    suit_counts = {}
    for suit in board_suits:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    target_suit = max(suit_counts, key=suit_counts.get)
    max_board_suit = suit_counts[target_suit]

    blocker_score = 0.0
    bluff_type = "none"
    suited_hole_ranks = sorted(
        (card_number(card) for card in hole_cards if card_suit(card) == target_suit),
        reverse=True,
    )
    if max_board_suit >= 3 and suited_hole_ranks:
        high_blocker = suited_hole_ranks[0]
        if high_blocker == 14:
            blocker_score += 0.24
            bluff_type = "flush_ace_blocker"
        elif high_blocker == 13:
            blocker_score += 0.18
            bluff_type = "flush_king_blocker"
        elif high_blocker == 12:
            blocker_score += 0.11
            bluff_type = "flush_queen_blocker"

    hole_ranks = [card_number(card) for card in hole_cards]
    if board_texture["paired"] and max(hole_ranks) >= 13:
        blocker_score += 0.05
        if bluff_type == "none":
            bluff_type = "paired_board_blocker"
    if board_texture["high_card"] >= 12 and 14 in hole_ranks:
        blocker_score += 0.04
        if bluff_type == "none":
            bluff_type = "ace_high_blocker"
    if board_texture["straight_pressure"] >= 0.65 and max(hole_ranks) >= max(10, board_texture["high_card"] - 1):
        blocker_score += 0.04
        if bluff_type == "none":
            bluff_type = "straight_pressure_blocker"

    info["score"] = blocker_score
    info["type"] = bluff_type
    info["eligible"] = blocker_score >= 0.14
    return info


def allow_low_frequency_blocker_bluff(req, hole_cards, public_cards, blocker_profile, round_idx, bluff_freq_bonus=0.0):
    if not blocker_profile["eligible"]:
        return False

    hand_idx = get_hand_index(req) or 0
    token = (sum(hole_cards) * 7 + sum(public_cards) * 11 + hand_idx * 13 + round_idx * 17) % 100
    threshold = clamp(blocker_profile["score"] * 35.0, 5.0, 18.0) + bluff_freq_bonus * 100.0
    return token < int(threshold)


def nutted_risk_profile(hole_cards, public_cards, pair_profile=None, board_texture=None, value_profile=None, paired_board_profile=None):
    info = {
        "risk": 0.0,
        "label": "none",
        "vulnerable": False,
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        from hand_evaluation import pair_board_profile
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        from board_analysis import board_texture_profile
        board_texture = board_texture_profile(public_cards)
    if paired_board_profile is None:
        from board_analysis import paired_board_outcome_profile
        paired_board_profile = paired_board_outcome_profile(hole_cards, public_cards)
    if value_profile is None:
        from hand_evaluation import value_hand_tier
        value_profile = value_hand_tier(hole_cards, public_cards, pair_profile, board_texture, paired_board_profile)

    score = evaluate_best(hole_cards + public_cards)
    hand_class = score[0]
    board_ranks = [card_number(card) for card in public_cards]
    hole_ranks = [card_number(card) for card in hole_cards]

    risk = 0.0
    label = "none"

    if hand_class == 6:
        if len(set(board_ranks)) <= 3 and score[1] < max(board_ranks):
            risk += 0.045
            label = "low_full_house"
    elif hand_class == 5:
        from hand_evaluation import made_flush_profile
        flush_profile = made_flush_profile(hole_cards, public_cards, board_texture)
        if flush_profile["nut_like"]:
            label = "nut_like_flush"
        elif flush_profile["high_hole_rank"] >= 12 and flush_profile["better_unseen_ranks"] <= 1:
            risk += 0.010
            label = "near_nut_flush"
        elif flush_profile["high_hole_rank"] >= 10:
            risk += 0.025
            label = "medium_flush"
        else:
            risk += 0.050
            label = "weak_flush"
        if board_texture["paired"]:
            risk += 0.03
            label += "_paired_board"
    elif hand_class == 4:
        if board_texture["flush_pressure"] >= 0.75:
            risk += 0.04
            label = "straight_on_flush_board"
        if board_texture["paired"]:
            risk += 0.03
            label = "straight_on_paired_board"
    elif hand_class == 3:
        if board_texture["paired"]:
            if paired_board_profile["strengthened"]:
                risk += 0.02
                label = paired_board_profile["label"]
            else:
                risk += 0.04
                label = "trips_on_paired_board"
        if score[1] < max(board_ranks):
            risk += 0.02
    elif hand_class == 2 and board_texture["paired"]:
        if paired_board_profile["board_two_pair"]:
            risk += 0.07
            label = paired_board_profile["label"]
        elif paired_board_profile["fragile_two_pair"]:
            risk += 0.08
            label = paired_board_profile["label"]
        elif paired_board_profile["weakened"]:
            risk += 0.06
            label = paired_board_profile["label"]
        elif paired_board_profile["strengthened"]:
            risk += 0.03
            label = paired_board_profile["label"]
        else:
            risk += 0.05
            label = "two_pair_on_paired_board"
    elif hand_class == 1 and value_profile["tier"] in ("strong", "thin"):
        if board_texture["flush_pressure"] >= 1.0:
            risk += 0.03
            label = "pair_on_four_flush"
        if board_texture["straight_pressure"] >= 1.0:
            risk += 0.02
            label = "pair_on_four_straight"

    info["risk"] = clamp(risk, 0.0, 0.14)
    info["label"] = label
    info["vulnerable"] = info["risk"] >= 0.04
    return info
