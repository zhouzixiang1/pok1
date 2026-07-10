import bisect
import itertools
import random

from card_utils import evaluate_7, clamp
from state import estimate_preflop_strength
from postflop import made_hand_metric, draw_potential


def combo_range_weight(combo, public_cards, state, opponent_model, spot_info):
    preflop = max(0.05, estimate_preflop_strength(list(combo)))
    confidence = opponent_model["confidence"]

    weight = 0.15 + preflop
    if spot_info["opp_preflop_raises"] > 0:
        pressure = 0.95 + 0.35 * spot_info["opp_preflop_raises"] + 0.20 * spot_info["last_raise_bb"]
        pressure += confidence * max(0.0, 0.42 - opponent_model["pfr"]) * 1.8
        pressure -= confidence * max(0.0, opponent_model["pfr"] - 0.38) * 0.9
        pressure -= confidence * opponent_model["allin_rate"] * 0.8
        weight *= preflop ** clamp(pressure, 0.70, 2.80)
    else:
        flatten = 0.80 - confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.5
        weight *= preflop ** clamp(flatten, 0.55, 1.10)

    if len(public_cards) >= 3:
        made = made_hand_metric(list(combo), public_cards)
        draw = draw_potential(list(combo), public_cards)
        post_metric = max(made, 0.16 + draw)

        if spot_info["facing_postflop_aggression"] or (spot_info["facing_allin"] and state["round"] > 0):
            pressure = 0.95 + 0.25 * spot_info["opp_round_raises"] + 0.35 * spot_info["last_raise_pot_ratio"]
            pressure += confidence * max(0.0, 0.34 - opponent_model["postflop_aggr"]) * 1.6
            pressure -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.46) * 0.8
            weight *= max(0.08, post_metric) ** clamp(pressure, 0.75, 2.80)
        else:
            loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.50) * 0.35
            weight *= 0.40 + post_metric + loose_bonus * 0.10

    if spot_info["facing_allin"] and state["round"] == 0:
        jam_pressure = 0.90 + confidence * max(0.0, 0.06 - opponent_model["allin_rate"]) * 4.0
        weight *= preflop ** clamp(jam_pressure, 0.90, 2.80)

    return max(weight, 1e-6)


def board_range_filter(combos, weights, public_cards, state, spot_info, opponent_model):
    """Post-filter opponent combo weights based on action sequence consistency.

    If opponent raised preflop, deprioritize trash hands (they wouldn't raise).
    If facing postflop aggression, deprioritize pure air (they wouldn't bet).
    Uses soft weighting (0.1-0.4 factors) rather than hard elimination to
    preserve Monte Carlo variance reduction.
    """
    filtered_weights = list(weights)
    pfr = opponent_model.get("pfr", 0.28)

    # Preflop action consistency
    if spot_info.get("opp_preflop_raises", 0) > 0:
        for i, combo in enumerate(combos):
            pf_str = estimate_preflop_strength(list(combo))
            if pf_str < 0.22 and pfr < 0.30:
                filtered_weights[i] *= 0.10
            elif pf_str < 0.28:
                filtered_weights[i] *= 0.40

    # Postflop action consistency
    if (
        (spot_info.get("facing_postflop_aggression") or spot_info.get("facing_allin"))
        and len(public_cards) >= 3
    ):
        for i, combo in enumerate(combos):
            made = made_hand_metric(list(combo), public_cards)
            draw = draw_potential(list(combo), public_cards)
            if made < 0.15 and draw < 0.08:
                filtered_weights[i] *= 0.30

    return combos, filtered_weights


def build_opponent_range(my_cards, public_cards, state, opponent_model, spot_info):
    used = set(my_cards + public_cards)
    deck = [card for card in range(52) if card not in used]
    combos = []
    weights = []
    for first, second in itertools.combinations(deck, 2):
        combo = (first, second)
        combos.append(combo)
        weights.append(combo_range_weight(combo, public_cards, state, opponent_model, spot_info))
    combos, weights = board_range_filter(combos, weights, public_cards, state, spot_info, opponent_model)
    return combos, weights


def build_cumulative_weights(weights):
    cumulative = []
    total = 0.0
    for weight in weights:
        total += weight
        cumulative.append(total)
    return cumulative, total


def weighted_choice_index(cumulative, total_weight):
    target = random.random() * total_weight
    return bisect.bisect_left(cumulative, target)


def exact_weighted_river_equity(my_cards, public_cards, combos, weights):
    my_score = evaluate_7(my_cards + public_cards)
    wins = 0.0
    total = 0.0

    for combo, weight in zip(combos, weights):
        opponent_score = evaluate_7(list(combo) + public_cards)
        total += weight
        if my_score > opponent_score:
            wins += weight
        elif my_score == opponent_score:
            wins += 0.5 * weight

    return 0.5 if total <= 0 else wins / total


def monte_carlo_weighted_equity(my_cards, public_cards, combos, cumulative, total_weight, iterations):
    if total_weight <= 0 or not combos:
        return 0.5

    used = set(my_cards + public_cards)
    deck = [card for card in range(52) if card not in used]
    need_public = 5 - len(public_cards)

    wins = 0.0
    for _ in range(iterations):
        combo = combos[weighted_choice_index(cumulative, total_weight)]
        combo_used = set(combo)
        rest_public_pool = [card for card in deck if card not in combo_used]
        rest_public = random.sample(rest_public_pool, need_public)
        board = public_cards + rest_public
        my_score = evaluate_7(my_cards + board)
        opponent_score = evaluate_7(list(combo) + board)
        if my_score > opponent_score:
            wins += 1.0
        elif my_score == opponent_score:
            wins += 0.5

    return wins / max(1, iterations)


def estimate_weighted_win_rate(my_cards, public_cards, combos, weights, iterations):
    if len(public_cards) == 5:
        return exact_weighted_river_equity(my_cards, public_cards, combos, weights)

    cumulative, total_weight = build_cumulative_weights(weights)
    return monte_carlo_weighted_equity(my_cards, public_cards, combos, cumulative, total_weight, iterations)


def polarized_jam_equity(my_cards, public_cards, combos, weights, opponent_model, iterations=200):
    """Compute equity vs a polarized {value, bluff} jam range.

    When facing an all-in jam from a polarized shover, the generic weighted
    range overestimates our equity because it includes marginal one-pair hands
    the jammer would never shove. This splits the opponent range into a
    value-jam tier (two-pair+) and a bluff-jam tier (pure air), computes
    equity vs each, and blends by an observed bluff fraction.
    """
    VALUE_MADE_MIN = 0.45   # two-pair+ (HAND_CLASS_SCORE[2]=0.40 + detail)
    BLUFF_MADE_MAX = 0.15   # below one-pair base (HAND_CLASS_SCORE[1]=0.22)
    BLUFF_DRAW_MAX = 0.08

    value_combos, value_weights = [], []
    bluff_combos, bluff_weights = [], []
    for combo, weight in zip(combos, weights):
        made = made_hand_metric(list(combo), public_cards)
        if made >= VALUE_MADE_MIN:
            value_combos.append(combo)
            value_weights.append(weight)
        elif made < BLUFF_MADE_MAX:
            draw = draw_potential(list(combo), public_cards)
            if draw < BLUFF_DRAW_MAX:
                bluff_combos.append(combo)
                bluff_weights.append(weight)

    # Bluff fraction from opponent model: polarized jammers bluff more
    shove_rate = opponent_model.get("shove_rate", 0.08)
    polarity = opponent_model.get("betsize_polarity", 0.0)
    bluff_fraction = clamp(
        0.20 + (shove_rate - 0.08) * 1.5 + max(0.0, polarity) * 0.3,
        0.10, 0.60,
    )

    if len(public_cards) == 5:
        eq_vs_value = exact_weighted_river_equity(
            my_cards, public_cards, value_combos, value_weights
        ) if value_combos else 0.50
        eq_vs_bluff = exact_weighted_river_equity(
            my_cards, public_cards, bluff_combos, bluff_weights
        ) if bluff_combos else 0.90
    else:
        if value_combos:
            vcum, vtot = build_cumulative_weights(value_weights)
            eq_vs_value = monte_carlo_weighted_equity(
                my_cards, public_cards, value_combos, vcum, vtot, iterations)
        else:
            eq_vs_value = 0.50
        if bluff_combos:
            bcum, btot = build_cumulative_weights(bluff_weights)
            eq_vs_bluff = monte_carlo_weighted_equity(
                my_cards, public_cards, bluff_combos, bcum, btot, iterations)
        else:
            eq_vs_bluff = 0.90

    return bluff_fraction * eq_vs_bluff + (1.0 - bluff_fraction) * eq_vs_value


if __name__ == "__main__":
    from itertools import combinations as _combos
    _mc = [48, 52]  # two aces
    _pc = [40, 24, 8]  # K-7-3 rainbow
    _used = set(_mc + _pc)
    _deck = [c for c in range(52) if c not in _used]
    _tc = list(_combos(_deck, 2))
    _tw = [1.0] * len(_tc)
    _opp = {"shove_rate": 0.20, "betsize_polarity": 0.3}
    _eq = polarized_jam_equity(_mc, _pc, _tc, _tw, _opp)
    assert 0.0 < _eq < 1.0, f"equity out of range: {_eq}"
    print(f"polarized_jam_equity self-test PASS eq={_eq:.3f}")
