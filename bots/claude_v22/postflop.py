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


def classify_street_texture(public_cards):
    if len(public_cards) < 3:
        return {"class": "none", "dry_score": 0.5, "bluff_combos": 0.5}
    bt = board_texture_profile(public_cards)
    suits = [c % 4 for c in public_cards]
    max_suit = max(suits.count(s) for s in set(suits))
    if max_suit >= 3 and bt["flush_pressure"] >= 0.75:
        return {"class": "monotone", "dry_score": 0.1, "bluff_combos": 0.85}
    if bt["paired"]:
        return {"class": "paired", "dry_score": 0.4, "bluff_combos": 0.3}
    if bt["flush_pressure"] >= 0.75 or bt["straight_pressure"] >= 0.65 or bt["wetness"] >= 0.45:
        return {"class": "draw_heavy", "dry_score": 0.15, "bluff_combos": 0.8}
    if bt["flush_pressure"] >= 0.35 or bt["straight_pressure"] >= 0.28 or bt["wetness"] >= 0.20:
        return {"class": "semi_connected", "dry_score": 0.35, "bluff_combos": 0.5}
    return {"class": "dry", "dry_score": 0.85, "bluff_combos": 0.15}


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
        margin += 0.050

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


def must_continue_vs_raise(value_profile, made_strength, pot_odds, nutted_risk, board_texture):
    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0
    extreme_texture = (
        board_texture is not None
        and (board_texture["flush_pressure"] >= 1.0 or board_texture["straight_pressure"] >= 1.0)
    )

    if tier == "nut":
        return True
    if made_strength >= 0.58 and pot_odds <= 0.42 and risk <= 0.07:
        return not (extreme_texture and risk >= 0.04)
    if tier == "strong" and pot_odds <= 0.36 and risk <= 0.05:
        return True
    return False
