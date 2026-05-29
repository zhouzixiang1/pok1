"""
Hand evaluation: made hand metrics, pair profiles, value tiers, flush profiles.
"""
from constants import HAND_CLASS_SCORE
from card_utils import clamp, card_suit, card_number, evaluate_best
from state import get_hand_index
from board_analysis import board_texture_profile
from draw_analysis import straight_draw_value


def made_hand_metric(hole_cards, public_cards):
    if len(public_cards) < 3:
        return 0.0
    score = evaluate_best(hole_cards + public_cards)
    metric = HAND_CLASS_SCORE[score[0]]
    detail = 0.0
    for idx, rank in enumerate(score[1:4]):
        detail += rank / (16.0 * (2 ** idx))
    return clamp(metric + detail * 0.008, 0.0, 0.995)


def pair_board_profile(hole_cards, public_cards):
    info = {
        "made_class": -1,
        "pair_rank": None,
        "pair_type": "none",
        "kicker_rank": 0,
        "board_overcards": 0,
        "uses_hole_card": False,
        "weak_kicker": False,
    }

    if len(public_cards) < 3:
        return info

    score = evaluate_best(hole_cards + public_cards)
    info["made_class"] = score[0]
    if score[0] != 1:
        return info

    pair_rank = score[1]
    hole_ranks = [card_number(card) for card in hole_cards]
    board_ranks = [card_number(card) for card in public_cards]
    board_unique = sorted(set(board_ranks), reverse=True)

    info["pair_rank"] = pair_rank
    info["board_overcards"] = sum(1 for rank in set(board_ranks) if rank > pair_rank)

    uses_hole = pair_rank in hole_ranks
    info["uses_hole_card"] = uses_hole

    hole_kickers = [rank for rank in hole_ranks if rank != pair_rank]
    if hole_kickers:
        info["kicker_rank"] = max(hole_kickers)
    else:
        board_kickers = [rank for rank in board_ranks if rank != pair_rank]
        info["kicker_rank"] = max(board_kickers, default=0)

    info["weak_kicker"] = info["kicker_rank"] <= 9

    if not uses_hole:
        info["pair_type"] = "board_pair"
        return info

    pocket_pair = hole_ranks[0] == hole_ranks[1] and hole_ranks[0] == pair_rank
    if pocket_pair:
        if board_ranks and pair_rank > max(board_ranks):
            info["pair_type"] = "overpair"
        elif info["board_overcards"] >= 1:
            info["pair_type"] = "underpair"
        else:
            info["pair_type"] = "pocket_pair"
        return info

    if board_unique and pair_rank == board_unique[0]:
        info["pair_type"] = "top_pair"
    elif len(board_unique) >= 2 and pair_rank == board_unique[1]:
        info["pair_type"] = "middle_pair"
    else:
        info["pair_type"] = "bottom_pair"

    return info


def pair_domination_margin(pair_profile, spot_info, round_idx):
    if pair_profile is None or pair_profile["made_class"] != 1:
        return 0.0

    pair_type = pair_profile["pair_type"]
    margin = 0.0

    if pair_type == "top_pair":
        margin += 0.012 if pair_profile["weak_kicker"] else 0.004
    elif pair_type == "middle_pair":
        margin += 0.030
        if pair_profile["weak_kicker"]:
            margin += 0.012
    elif pair_type == "bottom_pair":
        margin += 0.050
        if pair_profile["weak_kicker"]:
            margin += 0.012
    elif pair_type == "underpair":
        margin += 0.045 + 0.010 * pair_profile["board_overcards"]
    elif pair_type == "board_pair":
        margin += 0.065

    if spot_info["facing_postflop_aggression"]:
        margin += 0.010
    if spot_info.get("opp_postflop_bet_count", 0) >= 2:
        margin += 0.012
    if round_idx == 3 and pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
        margin += 0.015

    return clamp(margin, 0.0, 0.10)


def marginal_pair_under_pressure(pair_profile, board_texture):
    if pair_profile is None or pair_profile["made_class"] != 1:
        return False

    pair_type = pair_profile["pair_type"]
    if pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
        return True
    if pair_type == "top_pair" and pair_profile["weak_kicker"]:
        return True
    if pair_type == "top_pair" and board_texture is not None:
        return board_texture["high_card"] >= 14 and pair_profile["kicker_rank"] <= 11
    return False


def bet_size_bucket(last_raise_pot_ratio):
    if last_raise_pot_ratio <= 0.30:
        return "small"
    if last_raise_pot_ratio <= 0.75:
        return "medium"
    return "large"


def made_flush_profile(hole_cards, public_cards, board_texture=None):
    info = {
        "is_flush": False,
        "flush_suit": None,
        "hole_flush_ranks": [],
        "board_flush_ranks": [],
        "high_hole_rank": 0,
        "better_unseen_ranks": 0,
        "nut_like": False,
        "repressure_continue": False,
    }

    if len(public_cards) < 3:
        return info

    if board_texture is None:
        board_texture = board_texture_profile(public_cards)

    score = evaluate_best(hole_cards + public_cards)
    if score[0] != 5:
        return info

    suit_counts = {}
    for card in hole_cards + public_cards:
        suit = card_suit(card)
        suit_counts[suit] = suit_counts.get(suit, 0) + 1

    flush_suits = [suit for suit, count in suit_counts.items() if count >= 5]
    if not flush_suits:
        return info

    flush_suit = max(
        flush_suits,
        key=lambda suit: sorted(
            (card_number(card) for card in hole_cards + public_cards if card_suit(card) == suit),
            reverse=True,
        )[:5],
    )
    hole_flush_ranks = sorted(
        (card_number(card) for card in hole_cards if card_suit(card) == flush_suit),
        reverse=True,
    )
    board_flush_ranks = sorted(
        (card_number(card) for card in public_cards if card_suit(card) == flush_suit),
        reverse=True,
    )

    if not hole_flush_ranks:
        return info

    high_hole = hole_flush_ranks[0]
    seen_flush_ranks = set(hole_flush_ranks + board_flush_ranks)
    better_unseen = [rank for rank in range(high_hole + 1, 15) if rank not in seen_flush_ranks]

    info["is_flush"] = True
    info["flush_suit"] = flush_suit
    info["hole_flush_ranks"] = hole_flush_ranks
    info["board_flush_ranks"] = board_flush_ranks
    info["high_hole_rank"] = high_hole
    info["better_unseen_ranks"] = len(better_unseen)
    info["nut_like"] = len(better_unseen) == 0

    three_flush_board = len(board_flush_ranks) == 3
    high_private_flush = high_hole >= 11 and len(better_unseen) <= 2
    info["repressure_continue"] = (
        not board_texture["paired"]
        and three_flush_board
        and high_private_flush
    )
    return info


def value_hand_tier(hole_cards, public_cards, pair_profile=None, board_texture=None, paired_board_profile=None):
    info = {
        "tier": "none",
        "is_value": False,
        "size_bonus": 0.0,
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        board_texture = board_texture_profile(public_cards)
    if paired_board_profile is None:
        from board_analysis import paired_board_outcome_profile
        paired_board_profile = paired_board_outcome_profile(hole_cards, public_cards)

    score = evaluate_best(hole_cards + public_cards)
    hand_class = score[0]
    wetness = board_texture["wetness"]
    hole_ranks = [card_number(card) for card in hole_cards]
    size_bonus = 0.0
    tier = "none"

    if hand_class >= 6:
        tier = "nut"
        size_bonus = 0.22 + 0.08 * wetness
    elif hand_class == 5:
        flush_prof = made_flush_profile(hole_cards, public_cards, board_texture)
        if flush_prof["nut_like"]:
            tier = "nut"
            size_bonus = 0.18 + 0.06 * wetness
        elif flush_prof["high_hole_rank"] >= 12 and flush_prof["better_unseen_ranks"] <= 1:
            tier = "strong"
            size_bonus = 0.15 + 0.06 * wetness
        elif flush_prof["high_hole_rank"] >= 10:
            tier = "strong"
            size_bonus = 0.12 + 0.04 * wetness
        else:
            tier = "thin"
            size_bonus = 0.05 + 0.02 * wetness
    elif hand_class == 4:
        tier = "strong"
        size_bonus = 0.12 + 0.05 * wetness
    elif hand_class == 3:
        set_made = hole_ranks.count(score[1]) == 2
        tier = "nut" if set_made and board_texture["dynamic"] else "strong"
        size_bonus = 0.20 if tier == "nut" else 0.13 + 0.05 * wetness
    elif hand_class == 2:
        if paired_board_profile["board_paired"]:
            if paired_board_profile["board_two_pair"]:
                tier = "strong"
                size_bonus = 0.02 - 0.02 * wetness
            elif paired_board_profile["fragile_two_pair"]:
                tier = "thin"
                size_bonus = -0.01 - 0.03 * wetness
            elif paired_board_profile["weakened"]:
                tier = "thin"
                size_bonus = 0.02 - 0.03 * wetness
            elif paired_board_profile["label"] == "top_two_pair_above_board_pair":
                tier = "strong"
                size_bonus = 0.07 + 0.03 * wetness
            else:
                tier = "strong"
                size_bonus = 0.07 + 0.04 * wetness
        else:
            tier = "strong"
            size_bonus = 0.10 + 0.06 * wetness
    elif hand_class == 1 and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]
        if pair_type == "overpair":
            tier = "strong"
            size_bonus = 0.13 + 0.07 * wetness
        elif pair_type == "top_pair":
            if pair_profile["weak_kicker"]:
                tier = "thin"
                size_bonus = 0.01 - 0.03 * wetness
            else:
                tier = "strong" if pair_profile["pair_rank"] >= 11 else "thin"
                size_bonus = 0.09 + 0.03 * wetness if tier == "strong" else 0.03 - 0.02 * wetness
        elif pair_type == "pocket_pair":
            tier = "thin"
            size_bonus = 0.00 - 0.03 * wetness

    info["tier"] = tier
    info["is_value"] = tier != "none"
    info["size_bonus"] = clamp(size_bonus, -0.04, 0.24)
    return info


def value_bet_plan(value_profile, board_texture, paired_board_profile, pair_profile, nutted_risk, round_idx, pot):
    plan = {
        "size_delta": 0.0,
        "induce": False,
        "protect": False,
        "thin_control": False,
        "label": "normal",
    }

    if value_profile is None or board_texture is None or round_idx <= 0:
        return plan

    tier = value_profile.get("tier", "none")
    if tier == "none":
        return plan

    wetness = board_texture["wetness"]
    dynamic_board = board_texture["dynamic"]
    draw_heavy = board_texture["flush_pressure"] >= 0.75 or board_texture["straight_pressure"] >= 0.65
    paired_warning = (
        paired_board_profile is not None
        and paired_board_profile["board_paired"]
        and paired_board_profile["prefer_check"]
    )
    risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0

    if tier == "nut":
        if risk > 0.03:
            plan["size_delta"] -= min(0.08, 0.80 * risk)
            plan["label"] = "nutted_risk_control"
            return plan
        if dynamic_board:
            plan["protect"] = True
            plan["size_delta"] += 0.03 + 0.04 * wetness
            plan["label"] = "nut_value_dynamic"
        else:
            plan["induce"] = pot < 2600
            plan["size_delta"] -= 0.12 if round_idx < 3 else 0.16
            plan["label"] = "nut_value_induce"
        return plan

    if tier == "strong":
        vulnerable_pair = (
            pair_profile is not None
            and pair_profile["made_class"] == 1
            and pair_profile["pair_type"] in ("overpair", "top_pair")
        )
        if round_idx == 3 and not dynamic_board and wetness <= 0.25 and risk <= 0.03 and not paired_warning:
            # River overbet for strong hands on dry boards (straights, sets, high flushes)
            # Enables 1.3-1.7x pot extraction against calling stations
            plan["size_delta"] += 0.18
            plan["label"] = "strong_river_overbet"
        elif dynamic_board or draw_heavy:
            plan["protect"] = True
            plan["size_delta"] += 0.07 + 0.05 * wetness
            if round_idx == 2:
                plan["size_delta"] += 0.02
            plan["label"] = "strong_value_protect"
        elif vulnerable_pair:
            plan["size_delta"] -= 0.03
            plan["label"] = "strong_pair_static_control"

    if tier == "thin":
        plan["thin_control"] = True
        plan["size_delta"] -= 0.04 + 0.04 * wetness
        plan["label"] = "thin_value_control"

    if paired_warning and tier != "nut":
        plan["thin_control"] = True
        plan["size_delta"] -= 0.06
        plan["label"] = "paired_board_control"

    if risk > 0.0 and tier != "nut":
        plan["size_delta"] -= min(0.08, 0.45 * risk)

    plan["size_delta"] = clamp(plan["size_delta"], -0.18, 0.28)
    return plan
