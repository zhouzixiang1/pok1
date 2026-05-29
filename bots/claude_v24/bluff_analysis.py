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


def allow_low_frequency_blocker_bluff(req, hole_cards, public_cards, blocker_profile, round_idx):
    if not blocker_profile["eligible"]:
        return False

    hand_idx = get_hand_index(req) or 0
    token = (sum(hole_cards) * 7 + sum(public_cards) * 11 + hand_idx * 13 + round_idx * 17) % 100
    threshold = int(clamp(blocker_profile["score"] * 35.0, 5.0, 18.0))
    return token < threshold


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


def river_thin_value_profile(hole_cards, public_cards, pair_profile, board_texture, value_profile, nutted_risk):
    """Analyze whether a thin value bet on the river is appropriate.

    Returns a dict with keys:
        eligible (bool): Whether a thin value bet is warranted.
        sizing_tier (str): 'small', 'medium', or 'large'.
        confidence (float): Confidence in the thin value bet [0, 1].
    """
    result = {
        "eligible": False,
        "sizing_tier": "small",
        "confidence": 0.0,
    }

    if pair_profile is None or board_texture is None or value_profile is None or nutted_risk is None:
        return result
    if len(public_cards) < 5:
        return result

    tier = value_profile.get("tier", "none")
    risk = nutted_risk.get("risk", 0.0)
    from hand_evaluation import made_hand_metric as _made_hand_metric
    made_strength = _made_hand_metric(hole_cards, public_cards)
    wetness = board_texture.get("wetness", 0.0)
    dynamic = board_texture.get("dynamic", False)
    pair_type = pair_profile.get("pair_type", "none")
    kicker = pair_profile.get("kicker_rank", 0)

    eligible = False
    if tier == "thin":
        eligible = True
    elif 0.40 <= made_strength <= 0.58:
        eligible = True

    if eligible:
        if dynamic:
            eligible = False
        if risk >= 0.05:
            eligible = False
        if pair_type not in ("top_pair", "overpair"):
            eligible = False
        if pair_type == "top_pair" and kicker < 10:
            eligible = False

    if eligible:
        if wetness < 0.20:
            sizing_tier = "small"
        elif wetness <= 0.35:
            sizing_tier = "medium"
        else:
            sizing_tier = "large"

        confidence = clamp((kicker - 8) / 8.0, 0.0, 1.0)
    else:
        sizing_tier = "small"
        confidence = 0.0

    result["eligible"] = eligible
    result["sizing_tier"] = sizing_tier
    result["confidence"] = confidence
    return result


def turn_barrel_profile(hole_cards, public_cards, value_profile, board_texture, draw_info, spot_info, round_idx):
    """Analyze whether a turn barrel is appropriate.

    Returns a dict with keys:
        barrel_eligible (bool): Whether to barrel the turn.
        barrel_sizing_delta (float): Sizing adjustment for the barrel.
    """
    result = {
        "barrel_eligible": False,
        "barrel_sizing_delta": 0.0,
    }

    if round_idx != 2:
        return result
    if value_profile is None or board_texture is None or draw_info is None or spot_info is None:
        return result

    tier = value_profile.get("tier", "none")
    wetness = board_texture.get("wetness", 0.0)
    semi_bluff = draw_info.get("semi_bluff", False)
    draw_quality = draw_info.get("quality", 0.0)

    eligible = False
    sizing_delta = 0.0

    if tier in ("strong", "nut"):
        eligible = True
        sizing_delta = 0.05
    elif semi_bluff and draw_quality >= 0.18:
        eligible = True
        sizing_delta = -0.03

    if eligible and wetness >= 0.55:
        eligible = False

    result["barrel_eligible"] = eligible
    result["barrel_sizing_delta"] = sizing_delta
    return result


def river_bluff_ev(hole_cards, public_cards, blocker_profile, pot, opponent_model):
    """Estimate the expected value of a river bluff.

    Returns a dict with keys:
        ev (float): Estimated bluff EV.
        recommended (bool): Whether the bluff is recommended (ev > 0).
    """
    result = {
        "ev": 0.0,
        "recommended": False,
    }

    if len(public_cards) < 5 or pot <= 0:
        return result

    bluff_size = pot * 0.55
    fold_prob = opponent_model.get("fold_to_raise", 0.44)
    fold_prob = clamp(fold_prob, 0.15, 0.75)

    blocker_count = 0
    if blocker_profile is not None and blocker_profile.get("eligible", False):
        blocker_count = int(blocker_profile.get("score", 0.0) * 10)

    used = set(hole_cards + public_cards)
    deck_size = 52 - len(used)

    blocker_value = blocker_count / max(1, deck_size) * 0.10

    ev = fold_prob * pot - (1.0 - fold_prob) * bluff_size + blocker_value * pot

    result["ev"] = ev
    result["recommended"] = ev > 0
    return result


def donk_bet_profile(hole_cards, public_cards, pair_profile, board_texture, value_profile, draw_info, spot_info, opponent_model):
    """Analyze whether a donk bet (leading out OOP) is appropriate.

    Returns dict with keys:
        eligible (bool): Whether a donk bet is warranted.
        sizing_ratio (float): Pot ratio for donk bet sizing.
        reason (str): Reason for the donk bet.
    """
    result = {
        "eligible": False,
        "sizing_ratio": 0.0,
        "reason": "none",
    }

    if len(public_cards) < 3:
        return result
    if pair_profile is None or board_texture is None or value_profile is None:
        return result

    # Only donk when OOP and opponent hasn't bet yet this street
    if spot_info.get("facing_postflop_aggression", False):
        return result
    if spot_info.get("has_position", True):
        return result

    confidence = opponent_model.get("confidence", 0.0)
    if confidence < 0.20:
        return result

    tier = value_profile.get("tier", "none")
    wetness = board_texture.get("wetness", 0.0)
    paired = board_texture.get("paired", False)

    # Donk with strong/nut hands on wet boards to protect and build pot
    if tier in ("strong", "nut") and wetness >= 0.25:
        result["eligible"] = True
        result["sizing_ratio"] = 0.55 + 0.10 * wetness
        result["reason"] = "value_protect_wet_board"
        return result

    # Donk with strong hands on paired boards (deny free cards)
    if tier == "strong" and paired:
        result["eligible"] = True
        result["sizing_ratio"] = 0.45
        result["reason"] = "value_paired_board"
        return result

    # Donk with combo/nut draws on dynamic boards (semi-bluff initiative)
    if draw_info is not None and draw_info.get("semi_bluff", False):
        draw_quality = draw_info.get("quality", 0.0)
        draw_type = draw_info.get("type", "none")
        if draw_quality >= 0.18 and wetness >= 0.30:
            result["eligible"] = True
            result["sizing_ratio"] = 0.45 + 0.05 * wetness
            result["reason"] = "semi_bluff_dynamic"
            return result
        if draw_type in ("combo_draw", "nut_flush_draw") and wetness >= 0.20:
            result["eligible"] = True
            result["sizing_ratio"] = 0.50
            result["reason"] = "strong_draw_initiative"
            return result

    # Donk with overpairs on dry boards to extract value from weak calling ranges
    if pair_profile is not None and pair_profile.get("pair_type") == "overpair":
        if tier == "strong" and wetness < 0.25:
            opp_fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
            if opp_fold_to_raise < 0.50:
                # Opponent calls a lot - extract value
                result["eligible"] = True
                result["sizing_ratio"] = 0.40 + 0.05 * (0.50 - opp_fold_to_raise)
                result["reason"] = "overpair_value_extraction"
                return result

    return result
