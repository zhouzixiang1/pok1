"""
Opponent range building and equity estimation via simulation.
"""
import bisect
import itertools
import math
import random

from card_utils import clamp, evaluate_7
from state import estimate_preflop_strength
from postflop import made_hand_metric, draw_potential

# Module-level cache for cumulative weights to avoid recomputation
_cumulative_cache = {}
_CUMULATIVE_CACHE_MAX_SIZE = 256


def combo_range_weight(combo, public_cards, state, opponent_model, spot_info):
    preflop = max(0.05, estimate_preflop_strength(list(combo)))
    confidence = opponent_model["confidence"]

    weight = 0.15 + preflop

    # Detect extreme aggression pattern: raise + re-raise preflop
    extreme_aggr = spot_info["opp_preflop_raises"] >= 2
    # Detect passive multi-street calling: opponent called on multiple postflop streets
    passive_multi_call = (
        spot_info.get("opp_postflop_check_count", 0) >= 2
        and not spot_info["facing_postflop_aggression"]
        and not spot_info["facing_allin"]
    )

    if spot_info["opp_preflop_raises"] > 0:
        pressure = 0.95 + 0.35 * spot_info["opp_preflop_raises"] + 0.20 * spot_info["last_raise_bb"]
        pressure += confidence * max(0.0, 0.42 - opponent_model["pfr"]) * 1.8
        pressure -= confidence * max(0.0, opponent_model["pfr"] - 0.38) * 0.9
        pressure -= confidence * opponent_model["allin_rate"] * 0.8
        clamped_pressure = clamp(pressure, 0.70, 2.80)
        # Extreme aggression (raise + re-raise): use sqrt(preflop) for tighter range
        if extreme_aggr:
            clamped_pressure = max(clamped_pressure, 1.60)
            effective_power = clamped_pressure * 0.5  # preflop^0.5 effect via power halving
        else:
            effective_power = clamped_pressure
        weight *= preflop ** effective_power
    else:
        flatten = 0.80 - confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.5
        # Passive multi-street calling: use flatter power 0.6 to widen range
        if passive_multi_call:
            flatten = min(flatten, 0.60)
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
            # Passive multi-street calling: apply flatter weighting postflop too
            if passive_multi_call:
                weight *= 0.50 + post_metric + loose_bonus * 0.15
            else:
                weight *= 0.40 + post_metric + loose_bonus * 0.10

    if spot_info["facing_allin"] and state["round"] == 0:
        jam_pressure = 0.90 + confidence * max(0.0, 0.06 - opponent_model["allin_rate"]) * 4.0
        weight *= preflop ** clamp(jam_pressure, 0.90, 2.80)

    return max(weight, 1e-6)


def build_opponent_range(my_cards, public_cards, state, opponent_model, spot_info):
    used = set(my_cards + public_cards)
    deck = [card for card in range(52) if card not in used]
    combos = []
    weights = []
    for first, second in itertools.combinations(deck, 2):
        combo = (first, second)
        combos.append(combo)
        weights.append(combo_range_weight(combo, public_cards, state, opponent_model, spot_info))
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
    early_term_60 = int(iterations * 0.60)
    early_term_80 = int(iterations * 0.80)

    for i in range(iterations):
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

        # Early termination after 60% of iterations
        if i + 1 == early_term_60 and early_term_60 > 0:
            current_rate = wins / (i + 1)
            # If clearly above 0.70 or below 0.30, stop early
            if current_rate >= 0.70 or current_rate <= 0.30:
                return current_rate

        # Early termination after 80% of iterations if convergence is strong
        if i + 1 == early_term_80 and early_term_80 > 0:
            current_rate = wins / (i + 1)
            # Approximate standard deviation via binomial: sqrt(p*(1-p)/n)
            std_est = math.sqrt(current_rate * (1.0 - current_rate) / (i + 1))
            if std_est < 0.02:
                return current_rate

    return wins / max(1, iterations)


def estimate_weighted_win_rate(my_cards, public_cards, combos, weights, iterations, preflop_strength=None):
    if len(public_cards) == 5:
        return exact_weighted_river_equity(my_cards, public_cards, combos, weights)

    # Simulation count optimization: reduce iterations for very strong preflop hands
    effective_iterations = iterations
    if preflop_strength is not None and preflop_strength >= 0.85 and len(public_cards) == 0:
        effective_iterations = max(200, int(iterations * 0.70))

    # Check cumulative weights cache
    cache_key = (frozenset(my_cards), frozenset(public_cards))
    cached = _cumulative_cache.get(cache_key)
    if cached is not None:
        cumulative, total_weight = cached
    else:
        cumulative, total_weight = build_cumulative_weights(weights)
        # Store in cache (with eviction if too large)
        if len(_cumulative_cache) >= _CUMULATIVE_CACHE_MAX_SIZE:
            # Evict oldest entry (first key)
            _cumulative_cache.pop(next(iter(_cumulative_cache)))
        _cumulative_cache[cache_key] = (cumulative, total_weight)

    return monte_carlo_weighted_equity(my_cards, public_cards, combos, cumulative, total_weight, effective_iterations)
