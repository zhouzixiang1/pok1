"""Anytime first-strict policy for the national raw-TCP typed-intent ABI.

The system runtime owns TCP, state reconstruction, deadlines, legality and
wire serialization.  This module produces a sub-250 ms baseline, then spends
only the remaining monotonic budget on deterministic equity/EV batches.  Every
published refinement remains a closed typed intent.
"""

from __future__ import annotations

import itertools
import math
import time

import precompute


ACTION_AGGRESSION_PRIOR = 0.30
FOLD_TO_RAISE_PRIOR = 0.35
FOLD_TO_JAM_PRIOR = 0.28
RIVER_OVERCALL_PRIOR = 0.55
SHOWDOWN_TIGHTNESS_PRIOR = 190.0 / 1326.0
MAX_ADAPTATION_WEIGHT = 0.65
MAX_EQUITY_SAMPLES = 524_288
INITIAL_BATCH_SIZE = 32
MAX_BATCH_SIZE = 512
DEADLINE_GUARD_SECONDS = 0.002


def _number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = float(default)
    return float(default) if result != result else result


def _integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _bounded(value, lower, upper, default=0.0):
    return max(lower, min(upper, _number(value, default)))


def _card_id(card):
    card = card or {}
    return precompute.card_id(
        _integer(card.get("suit"), -1),
        _integer(card.get("rank"), -1),
    )


def _card_ids(cards):
    try:
        result = tuple(_card_id(card) for card in cards)
    except (TypeError, ValueError):
        return ()
    return result if len(set(result)) == len(result) else ()


def _opponent_posterior(context):
    """Return a bounded range/response projection, never raw history.

    Revealed hands are sampled conditional on reaching showdown.  Bucket
    weighting is therefore admitted only under the reducer's exact selection
    guard and remains discounted by its already reach-scaled adaptation weight.
    """

    opponent = context.get("opponent", {}) or {}
    rates = opponent.get("rates", {}) or {}
    terminal = opponent.get("terminal_response", {}) or {}
    showdown = opponent.get("showdown_range", {}) or {}
    action_weight = _bounded(
        opponent.get("adaptation_weight"), 0.0, MAX_ADAPTATION_WEIGHT
    )
    terminal_weight = _bounded(
        terminal.get(
            "adaptation_weight",
            _bounded(terminal.get("confidence"), 0.0, 1.0)
            * MAX_ADAPTATION_WEIGHT,
        ),
        0.0,
        MAX_ADAPTATION_WEIGHT,
    )
    showdown_guarded = bool(
        showdown.get("selection_scope") == "reached_showdown_only"
        and showdown.get("selection_bias_guard")
        == "reach_rate_discount_and_capped_influence"
    )
    showdown_weight = (
        _bounded(
            showdown.get("adaptation_weight"),
            0.0,
            MAX_ADAPTATION_WEIGHT,
        )
        if showdown_guarded
        else 0.0
    )
    aggression = _bounded(
        rates.get("aggression"), 0.0, 1.0, ACTION_AGGRESSION_PRIOR
    )
    aggression_delta = action_weight * (aggression - ACTION_AGGRESSION_PRIOR)
    tightness_delta = showdown_weight * (
        _bounded(
            showdown.get("tightness"),
            0.0,
            1.0,
            SHOWDOWN_TIGHTNESS_PRIOR,
        )
        - SHOWDOWN_TIGHTNESS_PRIOR
    )

    bucket_multipliers = {}
    bucket_rates = showdown.get("bucket_rates", {}) or {}
    bucket_priors = showdown.get("bucket_priors", {}) or {}
    if showdown_guarded and showdown_weight > 0.0:
        for bucket, observed in bucket_rates.items():
            prior = _bounded(bucket_priors.get(bucket), 1e-6, 1.0, 0.0)
            if prior <= 1e-6:
                continue
            relative = _bounded(_number(observed) / prior - 1.0, -0.75, 1.50)
            bucket_multipliers[str(bucket)] = _bounded(
                1.0 + showdown_weight * relative,
                0.50,
                1.75,
                1.0,
            )

    def posterior_rate(field, prior):
        observed = _bounded(terminal.get(field), 0.0, 1.0, prior)
        return _bounded(
            prior + terminal_weight * (observed - prior), 0.05, 0.90, prior
        )

    return {
        "action_weight": action_weight,
        "terminal_weight": terminal_weight,
        "showdown_weight": showdown_weight,
        "showdown_guarded": showdown_guarded,
        "bucket_multipliers": bucket_multipliers,
        # Positive values place modestly more simulation mass on weak holdings
        # when the action profile demonstrates a wider/aggressive range.
        "wide_range_tilt": _bounded(aggression_delta, -0.18, 0.35),
        "range_strength": _bounded(
            0.10 * aggression_delta - 0.12 * tightness_delta,
            -0.08,
            0.08,
        ),
        "raise_fraction": _bounded(
            0.08 * aggression_delta
            + 0.55
            * terminal_weight
            * (
                _bounded(
                    terminal.get("fold_to_raise"),
                    0.0,
                    1.0,
                    FOLD_TO_RAISE_PRIOR,
                )
                - FOLD_TO_RAISE_PRIOR
            )
            - 0.12 * tightness_delta,
            -0.20,
            0.28,
        ),
        "fold_to_raise": posterior_rate("fold_to_raise", FOLD_TO_RAISE_PRIOR),
        "fold_to_jam": posterior_rate("fold_to_jam", FOLD_TO_JAM_PRIOR),
        "river_overcall": posterior_rate("river_overcall", RIVER_OVERCALL_PRIOR),
    }


def _opponent_adjustments(context):
    posterior = _opponent_posterior(context)
    return {
        "range_strength": posterior["range_strength"],
        "raise_fraction": posterior["raise_fraction"],
    }


def _draw_bonus(cards, board_length):
    if board_length >= 5:
        return 0.0
    suits = {}
    rank_mask = 0
    for card in cards:
        suit, rank = precompute.card_parts(card)
        suits[suit] = suits.get(suit, 0) + 1
        rank_mask |= 1 << rank
    bonus = 0.075 if max(suits.values(), default=0) >= 4 else 0.0
    for high in range(12, 3, -1):
        window = 0b11111 << (high - 4)
        if (rank_mask & window).bit_count() == 4:
            bonus += 0.055
            break
    wheel = (1 << 12) | 0b1111
    if (rank_mask & wheel).bit_count() == 4:
        bonus = max(bonus, 0.055)
    return min(0.12, bonus)


def _baseline_equity(context):
    cards = context.get("cards", {}) or {}
    hole = _card_ids(cards.get("hole", ()))
    board = _card_ids(cards.get("board", ()))
    if len(hole) != 2 or len(board) not in (0, 3, 4, 5):
        return 0.35
    if not board:
        value = precompute.preflop_equity(hole[0], hole[1])
    else:
        rank = precompute.best_hand_rank((*hole, *board))
        category = rank[0]
        category_prior = {
            1: 0.31,
            2: 0.55,
            3: 0.70,
            4: 0.79,
            5: 0.84,
            6: 0.88,
            7: 0.93,
            8: 0.97,
            9: 0.985,
        }
        value = category_prior[category]
        if category == 1:
            value += 0.08 * (max(card // 4 for card in hole) / 12.0)
        value += _draw_bonus((*hole, *board), len(board))
    value += _opponent_posterior(context)["range_strength"]
    return _bounded(value, 0.02, 0.98, 0.35)


def _raise_intent(context, fraction, adaptation_scale=1.0):
    legal = context.get("legal", {}) or {}
    betting = context.get("betting", {}) or {}
    if "raise" not in set(legal.get("policy_kinds", ())):
        return None
    minimum = _integer(legal.get("min_raise_to"), 0)
    maximum = _integer(legal.get("max_raise_to"), 0)
    if minimum <= 0 or maximum < minimum:
        return None
    pot = max(1, _integer(betting.get("pot"), 1))
    hero_stage = max(0, _integer(betting.get("hero_street_bet"), 0))
    adjustment = _opponent_posterior(context)["raise_fraction"]
    effective_fraction = _bounded(
        _number(fraction, 0.45)
        + _bounded(adaptation_scale, 0.0, 1.0, 1.0) * adjustment,
        0.20,
        1.25,
        0.45,
    )
    target = max(minimum, hero_stage + int(round(pot * effective_fraction)))
    return {"kind": "raise", "raise_to": min(maximum, target)}


def _polarized_raise_fraction(context, equity):
    betting = context.get("betting", {}) or {}
    line = context.get("line", {}) or {}
    spr = _bounded(betting.get("spr"), 0.0, 200.0, 20.0)
    to_call = max(0, _integer(betting.get("to_call"), 0))
    if to_call == 0 and (line.get("can_donk") or line.get("can_delayed_probe")):
        return 0.38 if equity < 0.70 else 0.58
    if equity >= 0.78:
        if spr <= 1.5:
            return 0.90
        if spr <= 4.0:
            return 0.72
        return 0.58
    if equity <= 0.40 and to_call == 0:
        return 0.34
    return None


def get_baseline_decision(context):
    """Return a legal, I/O-free baseline without any simulation loop."""

    legal = context.get("legal", {}) or {}
    betting = context.get("betting", {}) or {}
    kinds = set(legal.get("policy_kinds", ()))
    to_call = max(0, _integer(betting.get("to_call"), 0))
    pot = max(1, _integer(betting.get("pot"), 1))
    spr = _bounded(betting.get("spr"), 0.0, 200.0, 20.0)
    equity = _baseline_equity(context)
    pot_odds = to_call / max(1.0, pot + to_call)

    if "allin" in kinds and equity >= 0.91 and spr <= 1.6:
        return {"kind": "allin"}
    fraction = _polarized_raise_fraction(context, equity)
    if fraction is not None and (to_call == 0 or equity >= pot_odds + 0.22):
        raised = _raise_intent(context, fraction, adaptation_scale=0.5)
        if raised is not None:
            return raised
    if to_call > 0 and equity + 0.035 < pot_odds and "fold" in kinds:
        return {"kind": "fold"}
    if "pass" in kinds:
        return {"kind": "pass"}
    if "fold" in kinds:
        return {"kind": "fold"}
    return {"kind": "allin"}


def _simulation_seed(context, known):
    hand = context.get("hand", {}) or {}
    seed = 0xD1B54A32D192ED03
    for value in (
        _integer(context.get("decision_id"), 0),
        _integer(hand.get("number"), 0),
        *known,
    ):
        seed ^= (int(value) + 0x9E3779B97F4A7C15 + (seed << 6) + (seed >> 2))
        seed &= 0xFFFFFFFFFFFFFFFF
    return seed or 1


def _opponent_sample_weight(posterior, opponent_hole):
    bucket = precompute.preflop_bucket(*opponent_hole)
    weight = _number(posterior["bucket_multipliers"].get(bucket), 1.0)
    class_equity = precompute.preflop_equity(*opponent_hole)
    weight *= 1.0 + posterior["wide_range_tilt"] * (0.55 - class_equity)
    return _bounded(weight, 0.35, 2.00, 1.0)


def _candidate_raise_fractions(context, equity):
    spr = _bounded((context.get("betting", {}) or {}).get("spr"), 0.0, 200.0, 20.0)
    structural = _polarized_raise_fraction(context, equity)
    fractions = [0.38, 0.66 if spr > 2.0 else 0.52, 1.05 if spr > 5.0 else 0.82]
    if structural is not None:
        fractions.append(structural)
    return tuple(dict.fromkeys(round(value, 3) for value in fractions))


def _decision_from_equity(context, equity, confidence, samples, return_margin=False):
    legal = context.get("legal", {}) or {}
    betting = context.get("betting", {}) or {}
    line = context.get("line", {}) or {}
    hand = context.get("hand", {}) or {}
    kinds = set(legal.get("policy_kinds", ()))
    pot = max(1.0, _number(betting.get("pot"), 1.0))
    to_call = max(0.0, _number(betting.get("to_call"), 0.0))
    hero_stage = max(0, _integer(betting.get("hero_street_bet"), 0))
    hero_stack = max(0.0, _number(betting.get("hero_stack"), 0.0))
    spr = _bounded(betting.get("spr"), 0.0, 200.0, 20.0)
    posterior = _opponent_posterior(context)
    river = hand.get("street") == "river"
    raise_fold_rate = posterior["fold_to_raise"]
    jam_fold_rate = posterior["fold_to_jam"]
    if river:
        overcall_fold = 1.0 - posterior["river_overcall"]
        raise_fold_rate = 0.60 * raise_fold_rate + 0.40 * overcall_fold
        jam_fold_rate = 0.65 * jam_fold_rate + 0.35 * overcall_fold
    uncertainty = min(0.22, 0.50 / math.sqrt(max(4.0, float(samples))))
    safe_equity = _bounded(equity - uncertainty * (1.0 - confidence), 0.0, 1.0)
    candidates = []
    if "fold" in kinds:
        candidates.append((0.0, {"kind": "fold"}))
    if "pass" in kinds:
        call_ev = safe_equity * pot - (1.0 - safe_equity) * to_call
        candidates.append((call_ev, {"kind": "pass"}))

    for fraction in _candidate_raise_fractions(context, safe_equity):
        intent = _raise_intent(context, fraction, adaptation_scale=1.0)
        if intent is None:
            continue
        risk = max(0.0, float(intent["raise_to"] - hero_stage))
        pressure = risk / max(1.0, pot + risk)
        # A caller's range is stronger than the sampled unconditional range.
        # Keep a prior continuation penalty even when terminal fold evidence is
        # strong; the exploit signal may change frequency/size but cannot turn
        # every small raw-equity edge into a stack-off.
        call_equity = _bounded(
            safe_equity - 0.025 - 0.065 * pressure,
            0.0,
            1.0,
        )
        called_ev = call_equity * (pot + 2.0 * risk) - risk
        score = (
            raise_fold_rate * pot
            + (1.0 - raise_fold_rate) * called_ev
            - uncertainty * risk * 0.12
        )
        if to_call == 0 and (line.get("can_donk") or line.get("can_delayed_probe")):
            score += pot * 0.08
        candidates.append((score, intent))

    if (
        "allin" in kinds
        and hero_stack > 0
        and (spr <= 2.5 or safe_equity >= 0.78)
    ):
        jam_equity = _bounded(safe_equity - 0.105, 0.0, 1.0)
        called_ev = jam_equity * (pot + 2.0 * hero_stack) - hero_stack
        jam_score = (
            jam_fold_rate * pot
            + (1.0 - jam_fold_rate) * called_ev
            - uncertainty * hero_stack * 0.15
        )
        candidates.append((jam_score, {"kind": "allin"}))
    if not candidates:
        result, margin = {"kind": "fold"}, 1.0
    else:
        ordered = sorted(candidates, key=lambda item: item[0], reverse=True)
        result = ordered[0][1]
        runner_up = ordered[1][0] if len(ordered) > 1 else ordered[0][0] - pot
        margin = _bounded(
            (ordered[0][0] - runner_up) / max(1.0, pot, abs(ordered[0][0])),
            0.0,
            1.0,
        )
    return (result, margin) if return_margin else result


def iter_decisions(context, baseline, deadline):
    """Yield deterministic progressive equity/EV refinements until ``deadline``.

    Each batch is finite, the total sample cap is fixed, and the monotonic
    deadline is checked both inside and between batches.  The runtime may kill
    the worker at the same boundary; the socket owner always retains the most
    recent already-validated intent.
    """

    deadline = _number(deadline, 0.0)
    if deadline <= time.monotonic() + DEADLINE_GUARD_SECONDS:
        return
    cards = context.get("cards", {}) or {}
    hole = _card_ids(cards.get("hole", ()))
    board = _card_ids(cards.get("board", ()))
    if len(hole) != 2 or len(board) not in (0, 3, 4, 5):
        return
    known = (*hole, *board)
    if len(set(known)) != len(known):
        return

    # Publish a cheap full-posterior sizing improvement before simulation.
    baseline_equity = _baseline_equity(context)
    fraction = _polarized_raise_fraction(context, baseline_equity)
    if baseline.get("kind") == "raise" and fraction is not None:
        adjusted = _raise_intent(context, fraction, adaptation_scale=1.0)
        if adjusted is not None and adjusted != baseline and time.monotonic() < deadline:
            yield {
                "decision": adjusted,
                "sample_count": 0,
                "confidence": 0.0,
                "reason": "bounded_posterior_sizing",
                "complete": False,
            }

    deck = precompute.deck_without(known)
    board_needed = 5 - len(board)
    state = _simulation_seed(context, known)
    posterior = _opponent_posterior(context)
    river_pairs = iter(itertools.combinations(deck, 2)) if board_needed == 0 else None
    river_exhausted = False
    samples = 0
    weighted_points = 0.0
    weight_total = 0.0
    weight_square_total = 0.0
    batch_size = 64 if river_pairs is not None else INITIAL_BATCH_SIZE
    stable_batches = 0
    last_decision = None

    while samples < MAX_EQUITY_SAMPLES:
        completed_batch = 0
        for _index in range(min(batch_size, MAX_EQUITY_SAMPLES - samples)):
            if time.monotonic() >= deadline - DEADLINE_GUARD_SECONDS:
                return
            if river_pairs is not None:
                try:
                    opponent_hole = next(river_pairs)
                except StopIteration:
                    river_exhausted = True
                    break
                runout = ()
            else:
                draw, state = precompute.deterministic_draw(
                    deck, 2 + board_needed, state
                )
                opponent_hole, runout = draw[:2], draw[2:]
            final_board = (*board, *runout)
            hero_rank = precompute.evaluate_seven((*hole, *final_board))
            opponent_rank = precompute.evaluate_seven((*opponent_hole, *final_board))
            point = 1.0 if hero_rank > opponent_rank else 0.5 if hero_rank == opponent_rank else 0.0
            weight = _opponent_sample_weight(posterior, opponent_hole)
            weighted_points += weight * point
            weight_total += weight
            weight_square_total += weight * weight
            samples += 1
            completed_batch += 1
        if completed_batch == 0:
            break
        estimate = weighted_points / max(1e-9, weight_total)
        effective_samples = weight_total * weight_total / max(1e-9, weight_square_total)
        confidence = effective_samples / (effective_samples + 192.0)
        blended_equity = (
            (1.0 - confidence) * baseline_equity + confidence * estimate
        )
        decision, score_margin = _decision_from_equity(
            context,
            _bounded(blended_equity, 0.0, 1.0),
            confidence,
            samples,
            return_margin=True,
        )
        if decision == last_decision:
            stable_batches += 1
        else:
            stable_batches = 1
            last_decision = decision
        # Clear spots finish quickly; medium-margin spots require materially
        # more evidence; genuinely close actions can consume the full official
        # budget.  This avoids burning 54 seconds on every trivial fold/check.
        converged = bool(
            (
                samples >= 4_096
                and confidence >= 0.94
                and stable_batches >= 4
                and score_margin >= 0.08
            )
            or (
                samples >= 32_768
                and confidence >= 0.99
                and stable_batches >= 8
                and score_margin >= 0.025
            )
        )
        complete = bool(
            river_exhausted or converged or samples >= MAX_EQUITY_SAMPLES
        )
        if time.monotonic() >= deadline - DEADLINE_GUARD_SECONDS:
            return
        yield {
            "decision": decision,
            "sample_count": samples,
            "confidence": round(confidence, 6),
            "reason": (
                "deterministic_equity_ev_converged"
                if converged
                else "deterministic_range_weighted_equity_ev_batch"
            ),
            "complete": complete,
        }
        if complete:
            return
        if river_pairs is None:
            batch_size = min(MAX_BATCH_SIZE, batch_size * 2)
