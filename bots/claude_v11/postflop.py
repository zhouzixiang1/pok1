from constants import HAND_CLASS_SCORE
from card_utils import card_suit, card_number, evaluate_best, clamp
from state import get_hand_index


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


def board_texture_profile(public_cards):
    info = {
        "wetness": 0.0,
        "flush_pressure": 0.0,
        "straight_pressure": 0.0,
        "paired": False,
        "high_card": 0,
        "dynamic": False,
    }

    if len(public_cards) < 3:
        return info

    board_ranks = [card_number(card) for card in public_cards]
    board_suits = [card_suit(card) for card in public_cards]
    info["high_card"] = max(board_ranks)
    info["paired"] = len(set(board_ranks)) < len(board_ranks)

    suit_counts = {}
    for suit in board_suits:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    max_suit = max(suit_counts.values())

    if max_suit >= 4:
        info["flush_pressure"] = 1.0
    elif max_suit == 3:
        info["flush_pressure"] = 0.75
    elif max_suit == 2 and len(public_cards) >= 4:
        info["flush_pressure"] = 0.35

    ranks = set(board_ranks)
    expanded = set(ranks)

    best_straight_pressure = 0.0
    for start in range(1, 11):
        window = set(range(start, start + 5))
        present = len(expanded & window)
        if present >= 4:
            best_straight_pressure = max(best_straight_pressure, 1.0)
        elif present == 3:
            best_straight_pressure = max(best_straight_pressure, 0.65)
        elif present == 2 and max(window & expanded, default=start) - min(window & expanded, default=start) <= 3:
            best_straight_pressure = max(best_straight_pressure, 0.28)

    info["straight_pressure"] = best_straight_pressure

    wetness = 0.18 * info["flush_pressure"]
    wetness += 0.22 * info["straight_pressure"]
    if info["high_card"] >= 12:
        wetness += 0.03
    if len(public_cards) >= 4 and not info["paired"]:
        wetness += 0.04
    if info["paired"]:
        wetness -= 0.06

    info["wetness"] = clamp(wetness, 0.0, 1.0)
    info["dynamic"] = (
        info["flush_pressure"] >= 0.75
        or info["straight_pressure"] >= 0.65
        or info["wetness"] >= 0.45
    )
    return info


def paired_board_outcome_profile(hole_cards, public_cards):
    info = {
        "board_paired": False,
        "board_pair_rank": 0,
        "board_pair_count": 0,
        "hand_class": -1,
        "uses_board_pair": False,
        "board_two_pair": False,
        "trips_vulnerable": False,
        "strengthened": False,
        "weakened": False,
        "fragile_two_pair": False,
        "prefer_check": False,
        "fold_to_raise": False,
        "label": "none",
    }

    if len(public_cards) < 3:
        return info

    board_counts = {}
    for card in public_cards:
        rank = card_number(card)
        board_counts[rank] = board_counts.get(rank, 0) + 1
    paired_ranks = sorted(
        ((rank, count) for rank, count in board_counts.items() if count >= 2),
        reverse=True,
    )
    if not paired_ranks:
        return info

    board_pair_rank, board_pair_count = paired_ranks[0]
    score = evaluate_best(hole_cards + public_cards)
    hand_class = score[0]
    hole_ranks = [card_number(card) for card in hole_cards]
    top_unpaired_board_rank = max(
        (rank for rank in board_counts if rank != board_pair_rank),
        default=0,
    )

    info["board_paired"] = True
    info["board_pair_rank"] = board_pair_rank
    info["board_pair_count"] = board_pair_count
    info["hand_class"] = hand_class

    if hand_class >= 6:
        info["strengthened"] = True
        info["label"] = "trips_plus_on_paired_board"
        return info

    if hand_class == 3:
        trips_rank = score[1]
        if trips_rank == board_pair_rank or hole_ranks.count(trips_rank) == 2:
            info["strengthened"] = True
            info["label"] = "strong_trips_on_paired_board"
        else:
            info["weakened"] = True
            info["prefer_check"] = True
            info["label"] = "fragile_trips_on_paired_board"
        return info

    if hand_class != 2:
        return info

    high_pair = score[1]
    low_pair = score[2]
    uses_board_pair = board_pair_rank in (high_pair, low_pair)
    pocket_pair = hole_ranks[0] == hole_ranks[1]
    info["uses_board_pair"] = uses_board_pair
    info["trips_vulnerable"] = uses_board_pair

    if uses_board_pair and pocket_pair and high_pair == hole_ranks[0] and low_pair == board_pair_rank:
        info["board_two_pair"] = True
        info["fold_to_raise"] = True
        info["label"] = "overpair_two_pair_on_paired_board"
        return info

    if uses_board_pair:
        other_pair = low_pair if high_pair == board_pair_rank else high_pair
        if high_pair == board_pair_rank and other_pair <= 6:
            info["weakened"] = True
            info["fragile_two_pair"] = True
            info["label"] = "low_two_pair_on_paired_board"
        elif high_pair == board_pair_rank and top_unpaired_board_rank > other_pair:
            info["weakened"] = True
            info["fragile_two_pair"] = True
            info["label"] = "dominated_two_pair_on_paired_board"
        elif low_pair == board_pair_rank and high_pair < top_unpaired_board_rank:
            info["weakened"] = True
            info["label"] = "under_top_two_pair_on_paired_board"
        elif low_pair == board_pair_rank and high_pair < 11:
            info["weakened"] = True
            info["fragile_two_pair"] = True
            info["label"] = "thin_two_pair_on_paired_board"
        else:
            info["label"] = "top_two_pair_with_board_pair"
    else:
        if high_pair == top_unpaired_board_rank and high_pair >= 11:
            info["strengthened"] = True
            info["label"] = "top_two_pair_above_board_pair"
        else:
            info["weakened"] = True
            info["label"] = "disconnected_two_pair_on_paired_board"
            if high_pair < 12:
                info["fragile_two_pair"] = True

    if info["weakened"] and not info["strengthened"]:
        info["prefer_check"] = True
    if info["fragile_two_pair"]:
        info["prefer_check"] = True
        info["fold_to_raise"] = True
    elif info["label"] in ("under_top_two_pair_on_paired_board", "disconnected_two_pair_on_paired_board"):
        info["fold_to_raise"] = True

    return info


def bet_size_bucket(last_raise_pot_ratio):
    if last_raise_pot_ratio <= 0.30:
        return "small"
    if last_raise_pot_ratio <= 0.75:
        return "medium"
    return "large"


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
        flush_profile = made_flush_profile(hole_cards, public_cards, board_texture)
        if flush_profile["nut_like"]:
            tier = "nut"
            size_bonus = 0.18 + 0.06 * wetness
        elif flush_profile["high_hole_rank"] >= 12 and flush_profile["better_unseen_ranks"] <= 1:
            tier = "strong"
            size_bonus = 0.15 + 0.06 * wetness
        elif flush_profile["high_hole_rank"] >= 10:
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
        if dynamic_board or draw_heavy:
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

    plan["size_delta"] = clamp(plan["size_delta"], -0.18, 0.16)
    return plan


def straight_draw_value(cards):
    ranks = {card_number(card) for card in cards}
    expanded = set(ranks)

    best = 0.0
    for start in range(1, 11):
        straight = set(range(start, start + 5))
        present = len(expanded & straight)
        if present != 4:
            continue
        missing = next(iter(straight - expanded))
        if missing in (start, start + 4):
            best = max(best, 0.17)
        else:
            best = max(best, 0.09)
    return best


def empty_draw_profile():
    return {
        "quality": 0.0,
        "type": "none",
        "flush_draw": False,
        "nut_flush_draw": False,
        "near_nut_flush_draw": False,
        "high_flush_draw": False,
        "flush_draw_rank": 0,
        "better_flush_draw_ranks": 0,
        "straight_draw": "none",
        "combo_draw": False,
        "overcards": 0,
        "semi_bluff": False,
        "fold_threshold_delta": 0.0,
        "size_bonus": 0.0,
    }


def draw_profile(hole_cards, public_cards, board_texture=None):
    info = empty_draw_profile()
    if len(public_cards) < 3 or len(public_cards) >= 5:
        return info

    if board_texture is None:
        board_texture = board_texture_profile(public_cards)

    cards = hole_cards + public_cards
    hole_ranks = [card_number(card) for card in hole_cards]
    board_high = max(card_number(card) for card in public_cards)
    info["overcards"] = sum(1 for rank in hole_ranks if rank > board_high)

    suit_counts = {}
    for card in cards:
        suit = card_suit(card)
        suit_counts[suit] = suit_counts.get(suit, 0) + 1

    flush_quality = 0.0
    best_flush_rank = 0
    best_better_flush_ranks = 0
    for suit, count in suit_counts.items():
        if count != 4:
            continue
        hole_flush_ranks = sorted(
            (card_number(card) for card in hole_cards if card_suit(card) == suit),
            reverse=True,
        )
        if not hole_flush_ranks:
            continue

        board_flush_ranks = [card_number(card) for card in public_cards if card_suit(card) == suit]
        high_flush_rank = max(hole_flush_ranks)
        seen_flush_ranks = set(hole_flush_ranks + board_flush_ranks)
        better_flush_ranks = len([rank for rank in range(high_flush_rank + 1, 15) if rank not in seen_flush_ranks])
        nut_draw = better_flush_ranks == 0
        info["flush_draw"] = True
        info["nut_flush_draw"] = info["nut_flush_draw"] or nut_draw

        candidate = 0.21 if nut_draw else 0.16
        if not nut_draw and high_flush_rank >= 12 and better_flush_ranks <= 1:
            candidate = max(candidate, 0.185)
        elif not nut_draw and high_flush_rank >= 11:
            candidate = max(candidate, 0.170)
        if high_flush_rank <= 9:
            candidate -= 0.025
        if board_texture["paired"] and not nut_draw:
            candidate -= 0.020
        if candidate > flush_quality:
            best_flush_rank = high_flush_rank
            best_better_flush_ranks = better_flush_ranks
        flush_quality = max(flush_quality, candidate)

    if info["flush_draw"]:
        info["flush_draw_rank"] = best_flush_rank
        info["better_flush_draw_ranks"] = best_better_flush_ranks
        info["near_nut_flush_draw"] = best_flush_rank >= 12 and best_better_flush_ranks <= 1
        info["high_flush_draw"] = best_flush_rank >= 11 and best_better_flush_ranks <= 3

    ranks = {card_number(card) for card in cards}
    expanded = set(ranks)
    hole_expanded = set(hole_ranks)

    straight_quality = 0.0
    gutshot_count = 0
    has_open_ended = False
    has_gutshot = False
    for start in range(1, 11):
        window = set(range(start, start + 5))
        present = expanded & window
        if len(present) != 4 or not (hole_expanded & present):
            continue
        missing = next(iter(window - present))
        if missing in (start, start + 4):
            has_open_ended = True
            straight_quality = max(straight_quality, 0.17)
        else:
            has_gutshot = True
            gutshot_count += 1
            straight_quality = max(straight_quality, 0.10)

    if has_open_ended:
        info["straight_draw"] = "open_ended"
    elif gutshot_count >= 2:
        info["straight_draw"] = "double_gutshot"
        straight_quality = max(straight_quality, 0.13)
    elif has_gutshot:
        info["straight_draw"] = "gutshot"

    info["combo_draw"] = info["flush_draw"] and info["straight_draw"] != "none"
    quality = max(flush_quality, straight_quality)
    if info["flush_draw"] and info["straight_draw"] != "none":
        quality = max(quality, flush_quality + straight_quality + 0.04)
    if len(public_cards) == 3:
        quality += 0.025 * info["overcards"]
    elif info["overcards"] >= 2:
        quality += 0.015

    if info["combo_draw"]:
        info["type"] = "combo_draw"
        info["fold_threshold_delta"] = 0.07
        info["size_bonus"] = 0.06
    elif info["nut_flush_draw"]:
        info["type"] = "nut_flush_draw"
        info["fold_threshold_delta"] = 0.05
        info["size_bonus"] = 0.04
    elif info["flush_draw"]:
        info["type"] = "flush_draw"
        if info["near_nut_flush_draw"]:
            info["fold_threshold_delta"] = 0.04
            info["size_bonus"] = 0.035
        elif info["high_flush_draw"]:
            info["fold_threshold_delta"] = 0.03
            info["size_bonus"] = 0.025
        else:
            info["fold_threshold_delta"] = 0.01 if info["flush_draw_rank"] >= 10 else 0.0
            info["size_bonus"] = 0.015
    elif info["straight_draw"] == "open_ended":
        info["type"] = "open_ended_straight_draw"
        info["fold_threshold_delta"] = 0.03
        info["size_bonus"] = 0.02
    elif info["straight_draw"] == "double_gutshot":
        info["type"] = "double_gutshot"
        info["fold_threshold_delta"] = 0.02
        info["size_bonus"] = 0.01
    elif info["straight_draw"] == "gutshot":
        info["type"] = "gutshot"
        info["fold_threshold_delta"] = -0.03
        info["size_bonus"] = -0.02

    info["quality"] = clamp(quality, 0.0, 0.35)
    info["semi_bluff"] = (
        info["combo_draw"]
        or info["nut_flush_draw"]
        or info["straight_draw"] in ("open_ended", "double_gutshot")
        or (info["flush_draw"] and info["quality"] >= 0.16)
        or (info["straight_draw"] == "gutshot" and info["overcards"] >= 1 and info["quality"] >= 0.13)
    )
    return info


def draw_potential(hole_cards, public_cards):
    return draw_profile(hole_cards, public_cards)["quality"]


def draw_call_margin(draw_info, board_texture, round_idx, spot_info):
    if draw_info is None or draw_info["type"] == "none":
        return 0.0

    margin = 0.0
    draw_type = draw_info["type"]
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])

    if draw_type == "combo_draw":
        margin -= 0.035
    elif draw_type == "nut_flush_draw":
        margin -= 0.025
    elif draw_type == "open_ended_straight_draw":
        margin -= 0.012
    elif draw_type == "double_gutshot":
        margin -= 0.006
    elif draw_type == "gutshot":
        margin += 0.040
    elif draw_type == "flush_draw" and not draw_info["nut_flush_draw"]:
        if draw_info.get("near_nut_flush_draw", False):
            margin -= 0.010
        elif draw_info.get("high_flush_draw", False):
            margin += 0.004
        else:
            margin += 0.020

    if (
        draw_type == "flush_draw"
        and draw_info.get("high_flush_draw", False)
        and size_bucket in ("small", "medium")
        and spot_info.get("has_position", False)
        and (board_texture is None or not board_texture["paired"])
    ):
        margin -= 0.006

    if board_texture is not None:
        if board_texture["paired"] and draw_info["flush_draw"] and not draw_info["nut_flush_draw"]:
            margin += 0.030
        if board_texture["flush_pressure"] >= 0.75 and draw_type == "open_ended_straight_draw":
            margin += 0.008

    if round_idx == 2:
        if draw_type == "gutshot":
            margin += 0.020
        elif draw_type == "flush_draw":
            if draw_info.get("near_nut_flush_draw", False) and size_bucket != "large" and (board_texture is None or not board_texture["paired"]):
                margin += 0.000
            elif draw_info.get("high_flush_draw", False) and size_bucket == "small" and (board_texture is None or not board_texture["paired"]):
                margin += 0.006
            else:
                margin += 0.020
    elif round_idx == 3:
        margin += 0.055

    if size_bucket == "large":
        if draw_type == "gutshot":
            margin += 0.018
        elif draw_type == "flush_draw":
            if draw_info.get("near_nut_flush_draw", False):
                margin += 0.006
            elif draw_info.get("high_flush_draw", False):
                margin += 0.012
            else:
                margin += 0.018

    return clamp(margin, -0.04, 0.08)


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


def blocker_bluff_profile(hole_cards, public_cards, pair_profile=None, board_texture=None):
    info = {
        "eligible": False,
        "score": 0.0,
        "type": "none",
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
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
    info["eligible"] = blocker_score >= 0.12
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
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        board_texture = board_texture_profile(public_cards)
    if paired_board_profile is None:
        paired_board_profile = paired_board_outcome_profile(hole_cards, public_cards)
    if value_profile is None:
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


def check_probe_resistance_margin(spot_info, opponent_model, round_idx):
    if round_idx <= 0 or not spot_info["facing_postflop_aggression"]:
        return 0.0

    margin = 0.0
    same_street_check_raise = (
        spot_info.get("opp_current_round_check_count", 0) > 0
        and spot_info.get("opp_current_round_bet_count", 0) > 0
    )
    delayed_resistance = (
        spot_info.get("opp_prior_postflop_check_count", 0) >= 2
        and spot_info.get("opp_current_round_bet_count", 0) > 0
    )

    if same_street_check_raise:
        margin += 0.035
    if delayed_resistance:
        margin += 0.018

    confidence = opponent_model.get("confidence", 0.0)
    if opponent_model.get("postflop_check_rate", 0.42) >= 0.52:
        margin += confidence * 0.018

    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    if size_bucket == "large":
        margin += 0.020
    elif size_bucket == "medium":
        margin += 0.010

    return clamp(margin, 0.0, 0.085)


def must_continue_vs_raise(value_profile, made_strength, pot_odds, nutted_risk, board_texture, spr=None, round_idx=0):
    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0
    extreme_texture = (
        board_texture is not None
        and (board_texture["flush_pressure"] >= 1.0 or board_texture["straight_pressure"] >= 1.0)
    )

    if tier == "nut":
        return True

    # SPR-based commitment: at low SPR, folding surrenders a large pot relative
    # to the remaining stack saved. Tier determines the SPR threshold:
    # - thin hands commit at SPR <= 0.5 (consistent with _spr_commitment_guard)
    # - strong/nut hands commit at SPR <= 1.0
    if spr is not None:
        if spr <= 0.5 and tier in ("strong", "nut", "thin"):
            thin_extra_risk = 0.02 if tier == "thin" else 0.0
            return not (extreme_texture and risk >= 0.04 + thin_extra_risk)
        if spr <= 1.0 and tier in ("strong", "nut"):
            return not (extreme_texture and risk >= 0.04)
        # On the river, thin hands commit at SPR <= 1.0 because there are
        # no more cards to come — the pot equity is fully realized, not
        # speculative. Uses the same thin_extra_risk pattern as SPR <= 0.5.
        if round_idx == 3 and spr <= 1.0 and tier == "thin":
            thin_extra_risk = 0.02
            return not (extreme_texture and risk >= 0.04 + thin_extra_risk)

    if made_strength >= 0.58 and pot_odds <= 0.42 and risk <= 0.07:
        return not (extreme_texture and risk >= 0.04)
    if tier == "strong" and pot_odds <= 0.36 and risk <= 0.05:
        return True
    return False


def _completion_blocker_profile(hole_cards, public_cards, board_texture, completion_risk):
    """Detect when our hand blocks a draw that just completed on the board.

    When a scare card completes a flush or straight draw, holding key blocking
    cards makes our bluff more credible because the opponent is less likely to
    hold the completed hand. This profile identifies such situations for
    enhanced river bluff decisions.

    Returns dict with:
      eligible: bool — whether our hand has a meaningful completion blocker
      score: float — blocker strength (higher = better blocker)
      type: str — "flush_completion_blocker" or "straight_completion_blocker" or "none"
      blocked_draw: str — "flush" or "straight" or "none"
    """
    info = {
        "eligible": False,
        "score": 0.0,
        "type": "none",
        "blocked_draw": "none",
    }

    if completion_risk is None or not (completion_risk.get("completed_flush") or completion_risk.get("completed_straight")):
        return info

    if board_texture is None or len(public_cards) < 4:
        return info

    hole_ranks = [card_number(card) for card in hole_cards]

    # Flush completion blocker: we hold a high card of the flush suit
    if completion_risk.get("completed_flush"):
        board_suits = [card_suit(card) for card in public_cards]
        suit_counts = {}
        for suit in board_suits:
            suit_counts[suit] = suit_counts.get(suit, 0) + 1
        target_suit = max(suit_counts, key=suit_counts.get)

        suited_hole_ranks = sorted(
            (card_number(card) for card in hole_cards if card_suit(card) == target_suit),
            reverse=True,
        )
        if suited_hole_ranks:
            high_blocker = suited_hole_ranks[0]
            # Higher blockers are more credible: Ace > King > Queen
            if high_blocker >= 14:
                info["score"] += 0.22
                info["type"] = "flush_completion_blocker"
                info["blocked_draw"] = "flush"
            elif high_blocker >= 13:
                info["score"] += 0.16
                info["type"] = "flush_completion_blocker"
                info["blocked_draw"] = "flush"
            elif high_blocker >= 12:
                info["score"] += 0.10
                info["type"] = "flush_completion_blocker"
                info["blocked_draw"] = "flush"

    # Straight completion blocker: we hold a card that would be needed for the straight
    if completion_risk.get("completed_straight"):
        all_ranks = {card_number(card) for card in hole_cards + public_cards}
        # Find which straights are present on the board
        best_blocker_score = 0.0
        for start in range(1, 11):
            window = set(range(start, start + 5))
            present = all_ranks & window
            if len(present) >= 4:
                # A straight is possible; check if our hole cards contain one of the straight ranks
                hole_in_straight = [r for r in hole_ranks if r in window]
                if hole_in_straight:
                    # The higher our card in the straight, the more it blocks
                    high_in_straight = max(hole_in_straight)
                    if high_in_straight >= start + 4:
                        # We hold the top end — blocks the nut straight
                        candidate = 0.14
                    elif high_in_straight >= start + 3:
                        candidate = 0.10
                    else:
                        candidate = 0.06
                    best_blocker_score = max(best_blocker_score, candidate)

        if best_blocker_score > 0 and info["score"] < best_blocker_score:
            info["score"] = best_blocker_score
            info["type"] = "straight_completion_blocker"
            info["blocked_draw"] = "straight"
        elif best_blocker_score > 0:
            # Already have a flush blocker, keep it (flush blockers are usually stronger)
            pass

    info["eligible"] = info["score"] >= 0.10
    return info


def _completion_risk_call_margin(completion_risk, board_texture, value_profile, round_idx):
    """Compute a call-margin adjustment when facing a bet on a board where a draw completed.

    When a scare card completes a flush or straight draw on the turn/river,
    non-nut made hands are much more likely to be beaten by the opponent's
    betting range. This structural function produces a positive margin
    adjustment (toward folding) proportional to the number of completed draws
    and inversely proportional to the hand's tier.

    This is the call-facing counterpart to the blunt `completion_folds` gate
    in get_action(). While that gate force-folds weak hands when a draw
    completes, this function provides a graduated adjustment for marginal
    hands that might still call with sufficient pot odds.

    Returns a float margin delta (positive = fold more, 0.0 = no adjustment).
    """
    if completion_risk is None or round_idx < 2:
        return 0.0

    completed_flush = completion_risk.get("completed_flush", False)
    completed_straight = completion_risk.get("completed_straight", False)
    if not completed_flush and not completed_straight:
        return 0.0

    tier = value_profile.get("tier", "none") if value_profile else "none"
    # Nut hands are unaffected — they beat all completed draws
    if tier == "nut":
        return 0.0

    margin = 0.0
    draw_count = int(completed_flush) + int(completed_straight)

    # Tier-scaled caution: weaker hands need more margin to call
    # Strong hands get a small bump; thin/none get the full adjustment
    if tier == "strong":
        tier_scale = 0.5
    elif tier == "thin":
        tier_scale = 1.0
    else:
        tier_scale = 1.2

    if completed_flush:
        # Flush completion is more dangerous — fewer opponent combos needed
        margin += 0.02 * draw_count * tier_scale
    if completed_straight:
        margin += 0.015 * tier_scale

    # On the river, completions are definitive (no more cards to come)
    # so the adjustment is structural, not speculative
    if round_idx == 3:
        margin *= 1.0
    else:
        # On the turn, there's still a river card — less certainty
        margin *= 0.7

    return clamp(margin, 0.0, 0.06)


def _board_completion_risk(hole_cards, public_cards, board_texture):
    """Detect when a draw-completing card has arrived on turn or river.

    Compares the texture of all-but-the-last-card against the full board.
    If the last card caused flush_pressure or straight_pressure to spike,
    it signals a completion scare card that should increase caution for
    non-nut made hands.
    """
    info = {
        "completed_flush": False,
        "completed_straight": False,
        "completion_street": -1,
        "risk_label": "none",
    }

    if board_texture is None or len(public_cards) < 4:
        return info

    # Compute texture without the last card to see if the last card completed something
    prior_cards = public_cards[:-1]
    prior_texture = board_texture_profile(prior_cards)

    if board_texture["flush_pressure"] > prior_texture["flush_pressure"]:
        info["completed_flush"] = True
        info["risk_label"] = "flush_completed"

    if board_texture["straight_pressure"] > prior_texture["straight_pressure"]:
        info["completed_straight"] = True
        if info["risk_label"] == "none":
            info["risk_label"] = "straight_completed"
        else:
            info["risk_label"] = "flush_and_straight_completed"

    if info["completed_flush"] or info["completed_straight"]:
        info["completion_street"] = len(public_cards) - 1

    return info


def _barrel_value_calibrator(barrel_profile, round_idx, has_position):
    """Derive value-bet threshold adjustments from opponent barrel sizing trends.

    When an opponent's barrel sizing is declining across streets, they are likely
    giving up on semi-bluffs or weakening their range. This creates an opportunity
    to widen our value range by lowering thresholds — we can bet more marginal
    hands for value against their weakened range.

    When sizing is escalating, the opponent's range is likely value-heavy, so
    we should tighten our value requirements to avoid value-owning ourselves
    (betting a hand that only gets called by better).

    The adjustment magnitude is derived from the barrel_profile's defense_adjustment
    (which already incorporates confidence-weighted aggression signals) and the
    sizing_trend label, but uses a different calibration than the fold-defense
    path in _should_check_river_weak.

    Returns dict with:
      threshold_delta: float — adjustment to add to 'medium'/'strong' thresholds
        (negative = lower thresholds = bet more aggressively)
      sizing_signal: str — "declining" | "escalating" | "stable" | "none"
    """
    result = {
        "threshold_delta": 0.0,
        "sizing_signal": "none",
    }

    if barrel_profile is None or round_idx < 2:
        return result

    sizing_trend = barrel_profile.get("sizing_trend", "none")
    is_multi_barrel = barrel_profile.get("is_multi_barrel", False)
    defense_adj = barrel_profile.get("defense_adjustment", 0.0)

    result["sizing_signal"] = sizing_trend

    if sizing_trend == "declining" and is_multi_barrel:
        # Opponent is giving up — lower thresholds to widen value range.
        # The base adjustment is proportional to the defense_adjustment
        # (which is negative for declining sizing), but we use the
        # absolute magnitude as a signal strength indicator.
        decline_strength = min(0.05, abs(defense_adj) * 1.2)
        # Position modulates: OOP needs stronger conviction to bet
        position_factor = 0.8 if not has_position else 1.0
        result["threshold_delta"] = -decline_strength * position_factor

    elif sizing_trend == "escalating" and is_multi_barrel:
        # Opponent range is value-heavy — tighten value thresholds.
        # We raise thresholds so only stronger hands bet for value.
        escalation_strength = min(0.04, abs(defense_adj) * 0.8)
        result["threshold_delta"] = escalation_strength

    elif sizing_trend == "stable" and is_multi_barrel:
        # Stable sizing gives no additional information — neutral
        # but if defense_adjustment is large, the barrel itself is credible
        if defense_adj > 0.03:
            result["threshold_delta"] = min(0.02, defense_adj * 0.5)

    return result


def _opponent_range_polarization(spot_info, opponent_model, line_context, board_texture, round_idx):
    """Classify opponent's current betting range as polarized, condensed, or balanced.

    A polarized range contains mostly nuts or air (bluffs), with few medium-strength
    hands. Signals include large bet sizing, check-raises, triple barrels, and
    delayed large bets after checking prior streets.

    A condensed range contains mostly medium-strength hands. Signals include
    small-to-medium bet sizing, check-calling across streets, and donk betting.

    Against a polarized range, we should call wider with bluff catchers (our
    medium-strength hands beat their bluffs) but fold non-bluff-catcher medium
    hands that lose to their value. Against a condensed range, we should bluff
    more aggressively (they fold medium hands to pressure) and call less with
    marginal bluff catchers (their range is medium-heavy and beats ours).

    Returns dict with:
      polarization: str — "polarized" | "condensed" | "balanced"
      strength: float — confidence weight in [0.0, 1.0]
      call_adjustment: float — margin delta for call/fold (positive = fold more)
      bluff_adjustment: float — threshold delta for bluff initiation
        (negative = bluff more, positive = bluff less)
    """
    if round_idx <= 0:
        return {"polarization": "balanced", "strength": 0.0, "call_adjustment": 0.0, "bluff_adjustment": 0.0}

    confidence = opponent_model.get("confidence", 0.0)
    if confidence < 0.15:
        return {"polarization": "balanced", "strength": 0.0, "call_adjustment": 0.0, "bluff_adjustment": 0.0}

    polarization_score = 0.0  # positive = polarized, negative = condensed
    signals = 0

    # Signal 1: Bet sizing relative to pot
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    if size_bucket == "large":
        polarization_score += 0.18
        signals += 1
    elif size_bucket == "small":
        polarization_score -= 0.12
        signals += 1

    # Signal 2: Multi-street aggression pattern
    if line_context is not None:
        line_type = line_context.get("line_type", "standard")
        if line_type == "triple_barrel":
            polarization_score += 0.22
            signals += 1
        elif line_type == "double_barrel":
            polarization_score += 0.10
            signals += 1
        elif line_type == "float_bet":
            # Float bet is polarized (bluff or strong made hand)
            polarization_score += 0.14
            signals += 1
        elif line_type == "delayed_aggression":
            polarization_score += 0.16
            signals += 1
        elif line_type == "single_barrel":
            polarization_score -= 0.06
            signals += 1

    # Signal 3: Same-street check-raise or snap re-raise
    if spot_info.get("opp_current_round_bet_count", 0) >= 2 and round_idx > 0:
        polarization_score += 0.20
        signals += 1

    # Signal 4: Board texture interaction
    if board_texture is not None:
        if board_texture["dynamic"]:
            # Large bets on dynamic boards are more polarized (to nuts or draws)
            if size_bucket == "large":
                polarization_score += 0.08
                signals += 1
        else:
            # Small bets on dry boards are more condensed (thin value/probes)
            if size_bucket == "small":
                polarization_score -= 0.08
                signals += 1

    # Signal 5: Opponent's historical tendency vs current action
    postflop_aggr = opponent_model.get("postflop_aggr", 0.36)
    if postflop_aggr < 0.32 and spot_info.get("facing_postflop_aggression", False):
        # Passive player betting = polarized to strong hands
        polarization_score += 0.10
        signals += 1
    elif postflop_aggr > 0.50 and spot_info.get("facing_postflop_aggression", False):
        # Aggressive player betting = could be wide, but small sizing suggests condensed
        if size_bucket != "large":
            polarization_score -= 0.08
            signals += 1

    if signals == 0:
        return {"polarization": "balanced", "strength": 0.0, "call_adjustment": 0.0, "bluff_adjustment": 0.0}

    # Normalize by signal count and scale by confidence
    avg_score = polarization_score / signals
    scaled_score = clamp(avg_score * confidence, -0.15, 0.15)

    if scaled_score > 0.06:
        polarization = "polarized"
        strength = min(1.0, scaled_score / 0.15)
        # Polarized range: call wider with bluff catchers (reduce fold margin)
        # but don't bluff into it (they call with nuts or fold air)
        call_adjustment = -0.02 * strength
        bluff_adjustment = 0.03 * strength
    elif scaled_score < -0.06:
        polarization = "condensed"
        strength = min(1.0, abs(scaled_score) / 0.15)
        # Condensed range: fold bluff catchers (increase fold margin), bluff more
        call_adjustment = 0.02 * strength
        bluff_adjustment = -0.03 * strength
    else:
        polarization = "balanced"
        strength = 0.0
        call_adjustment = 0.0
        bluff_adjustment = 0.0

    return {
        "polarization": polarization,
        "strength": strength,
        "call_adjustment": call_adjustment,
        "bluff_adjustment": bluff_adjustment,
    }


def _should_check_river_weak(
    round_idx, to_call, pair_profile, made_strength, draw_strength,
    blocker_profile, value_profile, spot_info, paired_board_profile,
    nutted_risk, paired_board_stackoff, board_texture, pot, match_profile,
    opponent_model=None, barrel_profile=None,
):
    """Consolidate the multiple river/late-street weak-hand checks.

    Returns True if the hand is too weak to bet/raise and should check instead.
    Each condition is evaluated independently; any single match means check.

    When opponent_model is provided, the checks become opponent-aware:
    - Aggressive opponents who check river are more likely trapping
    - Passive opponents who check river are more honest about weakness

    When barrel_profile is provided, sizing trend modulates decisions:
    - Declining barrel sizing suggests opponent weakness → suppress checks
    - Escalating barrel sizing suggests opponent strength → enhance checks
    """
    # Derive opponent tendency signals for opponent-aware modulation
    opp_aggr_signal = 0.0
    if opponent_model is not None:
        confidence = opponent_model.get("confidence", 0.0)
        postflop_aggr = opponent_model.get("postflop_aggr", 0.36)
        fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
        # Aggressive player checking is suspicious — scale by confidence
        # to avoid noise from limited samples
        if postflop_aggr > 0.42:
            opp_aggr_signal = confidence * (postflop_aggr - 0.42)

    # River weak pair: middle/bottom/underpair/board_pair on the river
    weak_pair_river = (
        round_idx == 3
        and pair_profile is not None
        and pair_profile["made_class"] == 1
        and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair", "board_pair")
    )

    # Opponent double-barreled then checked river — they may be trapping
    # Opponent-aware: aggressive opponents trapping is more dangerous
    opp_double_barrel_then_river_check = (
        round_idx == 3
        and to_call == 0
        and spot_info.get("opp_postflop_bet_count", 0) >= 2
        and spot_info["last_opp_action_type"] == "check"
    )

    # Bad river bluff: middling made hand with no blockers and no value tier
    # Opponent-aware: if aggressive opponent checks, our middling hand is
    # more likely beaten (they'd bet hands they want value from)
    bad_river_bluff_candidate = (
        round_idx == 3
        and to_call == 0
        and made_strength >= 0.18
        and made_strength < 0.40
        and not (blocker_profile and blocker_profile["eligible"])
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
    )

    # Opponent-model trap detection: aggressive player double-barrel then
    # river check is a strong trap signal. Even without weak_pair_river,
    # a bad_river_bluff_candidate should check against a trapping aggressor.
    opp_trap_river = (
        opp_double_barrel_then_river_check
        and bad_river_bluff_candidate
        and opp_aggr_signal > 0.0
    )

    # Weak bottom pair / underpair / board pair barrel on turn/river
    weak_bottom_pair_barrel = (
        round_idx >= 2
        and to_call == 0
        and pair_profile is not None
        and pair_profile["made_class"] == 1
        and pair_profile["pair_type"] in ("bottom_pair", "underpair", "board_pair")
        and made_strength < 0.40
        and draw_strength < 0.12
    )

    # Marginal pair after opponent showed prior aggression
    marginal_pair = marginal_pair_under_pressure(pair_profile, board_texture)
    weak_pair_after_raise_barrel = (
        round_idx >= 2
        and to_call == 0
        and marginal_pair
        and draw_strength < 0.14
        and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))
        and (
            spot_info.get("opp_previous_round_raise_count", 0) > 0
            or spot_info.get("opp_prior_postflop_raise_count", 0) > 0
        )
    )

    # Bad river value bet on paired board with non-nut two pair
    bad_river_value_bet = (
        round_idx == 3
        and to_call == 0
        and paired_board_profile is not None
        and paired_board_profile["board_paired"]
        and paired_board_profile["prefer_check"]
        and paired_board_profile["hand_class"] == 2
        and nutted_risk["risk"] >= 0.05
        and (value_profile is None or value_profile["tier"] != "nut")
    )

    # Bad stackoff with overpair on large paired-board pot
    bad_stackoff_overpair = (
        round_idx > 0
        and to_call == 0
        and paired_board_stackoff["active"]
        and pot > 3000
        and (value_profile is None or value_profile["tier"] != "nut")
    )

    # Big pot river weak-hand check: in large pots on the river, only strong/nut
    # hands should bet for value. Weak hands with no blocker equity should check.
    # Opponent-aware: in big pots, even aggressive opponents are less likely to
    # bluff-raise, so our weak hands get less fold equity when we bet.
    big_pot_threshold = int(clamp(
        1500 - 350 * match_profile.get("protect", 0.0) + 250 * match_profile.get("chase", 0.0),
        1100, 1800,
    ))
    big_pot = pot >= big_pot_threshold
    big_pot_river_weak = (
        big_pot
        and round_idx == 3
        and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))
        and (blocker_profile is None or not blocker_profile["eligible"])
    )

    if opp_double_barrel_then_river_check and weak_pair_river:
        return True
    if opp_trap_river:
        return True

    # Barrel sizing modulation: when opponent's barrel sizing is declining,
    # they are likely giving up on semi-bluffs or weakening their range.
    # Suppress weak-hand checks so we can bet into their weak range for value.
    declining_barrel = (
        barrel_profile is not None
        and barrel_profile.get("sizing_trend") == "declining"
        and round_idx >= 2
    )
    if declining_barrel:
        # With declining barrels, suppress checks for hands that have some
        # showdown value — betting into a weak range extracts value from
        # worse hands that would otherwise check behind.
        if bad_river_bluff_candidate and made_strength >= 0.25 and not opp_trap_river:
            return False
        if weak_bottom_pair_barrel and made_strength >= 0.25:
            return False

    if bad_river_bluff_candidate:
        return True
    if weak_bottom_pair_barrel:
        return True
    if weak_pair_after_raise_barrel:
        return True
    if bad_river_value_bet:
        return True
    if bad_stackoff_overpair:
        return True
    if big_pot_river_weak:
        return True

    return False
