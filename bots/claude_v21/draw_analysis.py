"""
Draw evaluation: flush draws, straight draws, combo draws, draw margins.
"""
from card_utils import clamp, card_suit, card_number


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
        from board_analysis import board_texture_profile
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
    from hand_evaluation import bet_size_bucket
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
