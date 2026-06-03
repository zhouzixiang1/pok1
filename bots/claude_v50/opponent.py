"""Opponent modeling and anti-bot_4 exploitation."""

from constants import BIG_BLIND, HAND_CLASS_SCORE
from card_utils import clamp, next_player, evaluate_7
from state import collect_latest_requests_by_hand
from tournament import opponent_can_lock_win


def smooth_rate(successes, total, prior_mean, prior_weight):
    return (successes + prior_mean * prior_weight) / (total + prior_weight)


def build_opponent_model(requests, my_id):
    opponent_id = next_player(my_id, 1)
    hand_requests = collect_latest_requests_by_hand(requests)

    # Accumulators for all-time stats
    preflop_opportunities = 0
    voluntary_preflop = 0
    preflop_raise = 0
    total_actions = 0
    aggressive_actions = 0
    allin_actions = 0
    postflop_actions = 0
    postflop_aggressive = 0
    postflop_checks = 0
    fold_to_raise_opportunities = 0
    fold_to_raise = 0
    raise_sizes = []

    for req in hand_requests:
        if opponent_can_lock_win(req, my_id):
            continue

        history = req.get("history", [])
        if not history:
            continue

        saw_opponent_preflop_action = False
        pending_my_pressure = False

        for record in history:
            pid = record["player_id"]
            action_type = record["action_type"]
            action = record["action"]
            round_idx = record["round"]

            if pid == my_id and action_type in ("raise", "allin"):
                pending_my_pressure = True
                continue

            if pid != opponent_id:
                continue

            total_actions += 1
            if action_type in ("raise", "allin"):
                aggressive_actions += 1
            if action_type == "allin":
                allin_actions += 1

            if round_idx == 0 and not saw_opponent_preflop_action:
                saw_opponent_preflop_action = True
                preflop_opportunities += 1
                if action_type in ("call", "raise", "allin"):
                    voluntary_preflop += 1
                if action_type in ("raise", "allin"):
                    preflop_raise += 1

            if round_idx > 0:
                postflop_actions += 1
                if action_type in ("raise", "allin"):
                    postflop_aggressive += 1
                if action_type == "check":
                    postflop_checks += 1

            if action_type == "raise":
                raise_sizes.append(action / BIG_BLIND)

            if pending_my_pressure:
                fold_to_raise_opportunities += 1
                if action_type == "fold":
                    fold_to_raise += 1
                pending_my_pressure = False

    confidence = clamp((total_actions - 5) / 35.0, 0.0, 1.0)
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else 2.6

    return {
        "confidence": confidence,
        "vpip": smooth_rate(voluntary_preflop, preflop_opportunities, 0.58, 4.0),
        "pfr": smooth_rate(preflop_raise, preflop_opportunities, 0.28, 4.0),
        "allin_rate": smooth_rate(allin_actions, total_actions, 0.05, 8.0),
        "postflop_aggr": smooth_rate(postflop_aggressive, postflop_actions, 0.36, 5.0),
        "postflop_check_rate": smooth_rate(postflop_checks, postflop_actions, 0.42, 5.0),
        "fold_to_raise": smooth_rate(fold_to_raise, fold_to_raise_opportunities, 0.44, 4.0),
        "aggression": smooth_rate(aggressive_actions, total_actions, 0.30, 6.0),
        "avg_raise_bb": avg_raise_bb,
    }


def analyze_current_spot(req, state):
    my_id = req["my_id"]
    opponent_id = next_player(my_id, 1)
    dealer_id = req["dealer_id"]
    sb = next_player(dealer_id, 1)
    bb = next_player(dealer_id, 2)
    history = req["history"]

    info = {
        "my_is_sb": my_id == sb,
        "my_is_bb": my_id == bb,
        "has_position": my_id == bb,
        "opp_preflop_raises": 0,
        "opp_round_raises": 0,
        "opp_total_raises": 0,
        "opp_postflop_bet_count": 0,
        "opp_current_round_bet_count": 0,
        "opp_postflop_check_count": 0,
        "opp_current_round_check_count": 0,
        "opp_prior_postflop_check_count": 0,
        "opp_prior_postflop_raise_count": 0,
        "opp_previous_round_raise_count": 0,
        "facing_raise": False,
        "facing_allin": state["opponent_allin"],
        "facing_postflop_aggression": False,
        "last_opp_action_type": None,
        "last_raise_bb": 0.0,
        "last_raise_pot_ratio": 0.0,
        "preflop_spot": "other",
    }

    for record in history:
        if record["player_id"] == opponent_id and record["round"] > 0 and record["action_type"] == "check":
            info["opp_postflop_check_count"] += 1
            if record["round"] == state["round"]:
                info["opp_current_round_check_count"] += 1
            elif record["round"] < state["round"]:
                info["opp_prior_postflop_check_count"] += 1

        if record["player_id"] != opponent_id or record["action_type"] not in ("raise", "allin"):
            continue
        info["opp_total_raises"] += 1
        if record["round"] == 0:
            info["opp_preflop_raises"] += 1
        if record["round"] > 0:
            info["opp_postflop_bet_count"] += 1
            if record["round"] < state["round"]:
                info["opp_prior_postflop_raise_count"] += 1
            if record["round"] == state["round"] - 1:
                info["opp_previous_round_raise_count"] += 1
        if record["round"] == state["round"]:
            info["opp_round_raises"] += 1
            if record["round"] > 0:
                info["opp_current_round_bet_count"] += 1

    if history and history[-1]["player_id"] == opponent_id:
        last = history[-1]
        info["last_opp_action_type"] = last["action_type"]
        if last["action_type"] in ("raise", "allin"):
            info["facing_raise"] = True
            info["facing_postflop_aggression"] = state["round"] > 0
            if last["action_type"] == "raise":
                info["last_raise_bb"] = last["action"] / BIG_BLIND
                info["last_raise_pot_ratio"] = last["action"] / max(1, state["pot"])
            else:
                info["last_raise_bb"] = state["allin_call_amount"] / max(1, BIG_BLIND)
                info["last_raise_pot_ratio"] = state["allin_call_amount"] / max(1, state["pot"])

    if state["round"] == 0:
        if not history and info["my_is_sb"]:
            info["preflop_spot"] = "sb_open"
        elif history and info["my_is_bb"] and history[-1]["player_id"] == opponent_id:
            if history[-1]["action_type"] == "call":
                info["preflop_spot"] = "bb_vs_limp"
            elif history[-1]["action_type"] in ("raise", "allin"):
                info["preflop_spot"] = "bb_vs_raise"
        elif history and info["my_is_sb"] and history[-1]["player_id"] == opponent_id:
            if history[-1]["action_type"] in ("raise", "allin"):
                info["preflop_spot"] = "sb_vs_reraise"

    return info


def detect_bot4_profile(opponent_model, n_hands_played):
    """Detect if opponent exhibits bot_4's characteristic stats."""
    confidence = opponent_model["confidence"]
    if confidence < 0.10:
        return False, 0.0

    score = 0.0
    vpip = opponent_model["vpip"]
    pfr = opponent_model["pfr"]
    aggr = opponent_model["aggression"]
    post_aggr = opponent_model["postflop_aggr"]
    fold_raise = opponent_model["fold_to_raise"]

    if abs(vpip - 0.58) < 0.15:
        score += 0.20
    if abs(pfr - 0.28) < 0.13:
        score += 0.20
    if abs(post_aggr - 0.36) < 0.15:
        score += 0.20
    if abs(fold_raise - 0.44) < 0.15:
        score += 0.15
    if abs(aggr - 0.30) < 0.13:
        score += 0.15

    score *= confidence
    return score >= 0.20, score


def get_anti_bot4_adjustments(bot4_score, board_texture, spot_info, round_idx, value_profile):
    """Return strategy adjustments targeting bot_4's weaknesses."""
    adj = {
        "bluff_freq_bonus": 0.0,
        "raise_size_bonus": 0.0,
        "call_threshold_delta": 0.0,
        "fold_threshold_delta": 0.0,
        "river_overbet_enabled": False,
        "trap_defense_delta": 0.0,
    }

    # Wet board: exploit bot_4 overfold on dynamic boards
    if board_texture and board_texture["dynamic"]:
        adj["bluff_freq_bonus"] += 0.15 * bot4_score
        adj["raise_size_bonus"] += 0.08 * bot4_score

    # Paired board: exploit bot_4 paired board caution
    if board_texture and board_texture["paired"]:
        adj["bluff_freq_bonus"] += 0.10 * bot4_score
        adj["raise_size_bonus"] += 0.05 * bot4_score

    # River check exploit: bot_4 checks too much on river
    if round_idx == 3 and spot_info.get("last_opp_action_type") == "check":
        adj["bluff_freq_bonus"] += 0.12 * bot4_score

    # Preflop 3-Bet wider vs bot_4
    if round_idx == 0 and spot_info.get("preflop_spot") in ("bb_vs_raise", "sb_vs_reraise"):
        adj["call_threshold_delta"] -= 0.05 * bot4_score

    # Anti-trap: more cautious vs check-raise
    if spot_info.get("opp_current_round_check_count", 0) > 0 and spot_info["facing_raise"]:
        adj["trap_defense_delta"] += 0.08 * bot4_score

    # River overbet always enabled with strong hands (not just vs bot_4)
    if round_idx == 3 and value_profile and value_profile["tier"] in ("nut", "strong"):
        adj["river_overbet_enabled"] = True

    return adj


def classify_opponent_style(opp_model):
    """Classify opponent into 4 archetypes and return threshold deltas.

    Archetypes:
      - nit:        low VPIP, high fold-to-raise → bluff more, loosen calls
      - maniac:     high VPIP, high PFR, high aggression → tighten, call lighter
      - station:    high VPIP, low PFR, low fold-to-raise → don't bluff, value bet
      - fold-heavy: high fold-to-raise → bluff more
    Returns delta dict applied to strategy thresholds.
    """
    deltas = {
        'strong_delta': 0.0, 'medium_delta': 0.0, 'bluff_freq_bonus': 0.0,
        'call_aggression_bonus': 0.0, 'fold_vs_passive_bonus': 0.0,
    }
    confidence = opp_model.get('confidence', 0.0)
    if confidence < 0.15:
        return deltas
    vpip = opp_model.get('vpip', 0.52)
    pfr = opp_model.get('pfr', 0.24)
    fold_to_raise = opp_model.get('fold_to_raise', 0.44)
    postflop_aggr = opp_model.get('postflop_aggr', 0.36)

    # Nit: very tight, folds a lot
    if vpip < 0.35 and fold_to_raise > 0.50:
        deltas['strong_delta'] = -0.02
        deltas['medium_delta'] = -0.015
        deltas['bluff_freq_bonus'] = 0.12

    # Maniac: very loose and aggressive
    elif vpip > 0.65 and pfr > 0.40 and postflop_aggr > 0.45:
        deltas['strong_delta'] = 0.03
        deltas['medium_delta'] = 0.025
        deltas['bluff_freq_bonus'] = -0.08
        deltas['call_aggression_bonus'] = 0.04

    # Station: calls a lot, rarely raises, doesn't fold
    elif vpip > 0.55 and pfr < 0.20 and fold_to_raise < 0.38:
        deltas['strong_delta'] = -0.01
        deltas['medium_delta'] = -0.02
        deltas['bluff_freq_bonus'] = -0.12

    # Fold-heavy: folds too much to raises
    elif fold_to_raise > 0.52:
        deltas['strong_delta'] = -0.015
        deltas['medium_delta'] = -0.01
        deltas['bluff_freq_bonus'] = 0.10

    return deltas


# ---------------------------------------------------------------------------
# Opponent action-sequence line tracker
# ---------------------------------------------------------------------------

LINE_STRENGTH_MAP = {
    # 3+ consecutive aggressive streets (barrel barrel barrel)
    "aggressive-aggressive-aggressive": 0.035,
    # Polarized: bet, check/give-up, bet again
    "aggressive-passive-aggressive": 0.020,
    # Two consecutive aggressive streets
    "aggressive-aggressive": 0.010,
    # Donk bet then checks down (weak — probing, gave up)
    "aggressive-passive-passive": -0.015,
    # All passive across 3+ streets (very weak/passive)
    "passive-passive-passive": -0.020,
    # Two passive streets
    "passive-passive": -0.015,
    # Raises then passive (pot-controlling marginal hand)
    "aggressive-passive": -0.010,
    # One-and-done: passive-aggro-passive
    "passive-aggressive-passive": -0.010,
    # Single aggressive street
    "aggressive": 0.005,
    # Single mixed street (erratic)
    "mixed": 0.005,
}

# Check-raise signal — handled specially, not in the map.
_CHECK_RAISE_SIGNAL = 0.040


def _default_line_profile():
    """Return a neutral line profile when no history is available."""
    return {
        "line": "none",
        "strength_signal": 0.0,
        "aggression_count": 0,
        "passive_count": 0,
        "last_street_action": "none",
        "check_raise_detected": False,
    }


def _classify_street(history, opponent_id, street_idx):
    """Classify opponent's actions on a single street.

    Returns (label, check_raise) where label is one of
    'passive' / 'aggressive' / 'mixed' and check_raise is True when
    the opponent first checked/called then raised/all-in on this street.
    """
    actions = []
    for record in history:
        if record["player_id"] != opponent_id:
            continue
        if record["round"] != street_idx:
            continue
        actions.append(record["action_type"])

    if not actions:
        return None, False

    has_aggressive = any(a in ("raise", "allin") for a in actions)
    has_passive = any(a in ("check", "call") for a in actions)

    check_raise = False
    if has_aggressive and has_passive:
        # Check-raise: first action passive, later action aggressive
        first_passive = actions[0] in ("check", "call")
        later_aggressive = any(a in ("raise", "allin") for a in actions[1:])
        if first_passive and later_aggressive:
            check_raise = True
        return "mixed", check_raise
    elif has_aggressive:
        return "aggressive", False
    else:
        return "passive", False


def _lookup_line_strength(line, check_raise):
    """Look up strength signal from LINE_STRENGTH_MAP.

    Uses longest-match semantics (most specific pattern wins).
    """
    if check_raise:
        return _CHECK_RAISE_SIGNAL

    if line == "none":
        return 0.0

    best_signal = 0.0
    best_len = 0
    for pattern, signal in LINE_STRENGTH_MAP.items():
        plen = len(pattern)
        if plen <= best_len:
            continue
        if (
            line == pattern
            or line.endswith("-" + pattern)
            or ("-" + pattern + "-") in line
            or line.startswith(pattern + "-")
        ):
            best_signal = signal
            best_len = plen

    return best_signal


def build_opponent_line_profile(requests, my_id, state):
    """Analyze the current hand's opponent action sequence.

    Encodes each street as passive / aggressive / mixed, looks up the
    resulting line in LINE_STRENGTH_MAP, and returns a strength signal
    together with summary counters.

    Returns dict with keys:
        line, strength_signal, aggression_count, passive_count,
        last_street_action, check_raise_detected
    """
    opponent_id = next_player(my_id, 1)
    req = requests[-1] if requests else None
    if req is None:
        return _default_line_profile()

    history = req.get("history", [])
    if not history:
        return _default_line_profile()

    current_round = state["round"]

    street_labels = []
    aggression_count = 0
    passive_count = 0
    any_check_raise = False
    last_action = "none"

    for street_idx in range(current_round + 1):
        label, check_raise = _classify_street(history, opponent_id, street_idx)
        if label is None:
            continue
        street_labels.append(label)
        if label == "aggressive" or (label == "mixed" and check_raise):
            aggression_count += 1
        if label in ("passive", "mixed"):
            passive_count += 1
        if check_raise:
            any_check_raise = True
        # Capture last opponent action on this street
        for record in reversed(history):
            if record["player_id"] == opponent_id and record["round"] == street_idx:
                last_action = record["action_type"]
                break

    line = "-".join(street_labels) if street_labels else "none"
    strength_signal = _lookup_line_strength(line, any_check_raise)

    return {
        "line": line,
        "strength_signal": clamp(strength_signal, -0.04, 0.04),
        "aggression_count": aggression_count,
        "passive_count": passive_count,
        "last_street_action": last_action,
        "check_raise_detected": any_check_raise,
    }


def build_line_showdown_tracker(requests, my_id):
    """Track which opponent lines resulted in strong/weak showdown hands.

    Parses completed hands from *requests* to find showdowns where the
    opponent's hole cards are visible.  Accumulates line → avg-strength
    correlations for future exploitation.

    Best-effort: silently skips hands without visible opponent cards.
    """
    opponent_id = next_player(my_id, 1)
    hand_requests = collect_latest_requests_by_hand(requests)

    line_strengths = {}
    n_showdowns = 0

    for req in hand_requests:
        public_cards = req.get("public_cards", [])
        if len(public_cards) != 5:
            continue

        # Try common field names for opponent hole cards
        opp_cards = (
            req.get("opponent_cards")
            or req.get("oppo_cards")
            or req.get("opp_cards")
        )
        if not opp_cards or not isinstance(opp_cards, (list, tuple)) or len(opp_cards) != 2:
            continue

        # Compute opponent's showdown hand strength
        try:
            all_cards = list(opp_cards) + list(public_cards)
            if len(all_cards) != 7:
                continue
            hand_score = evaluate_7(all_cards)
            hand_class = hand_score[0]  # 0–8
            normalized = HAND_CLASS_SCORE[min(hand_class, 8)]
        except Exception:
            continue

        # Extract opponent's line for this hand
        history = req.get("history", [])
        labels = []
        for street_idx in range(4):  # preflop through river
            label, _ = _classify_street(history, opponent_id, street_idx)
            if label is not None:
                labels.append(label)

        line = "-".join(labels) if labels else "none"

        if line not in line_strengths:
            line_strengths[line] = []
        line_strengths[line].append(normalized)
        n_showdowns += 1

    return {
        "line_strengths": line_strengths,
        "n_showdowns": n_showdowns,
    }


def line_adjustment(line_profile, showdown_tracker):
    """Compute threshold adjustments from opponent's action line.

    Blends the lookup-table signal with observed showdown data
    (when ≥ 2 observations exist).  Returns deltas intended to be
    added to the *strong* and *medium* thresholds in strategy.py.
    """
    line = line_profile["line"]
    lookup_signal = line_profile["strength_signal"]

    threshold_delta = lookup_signal

    # Blend with showdown observations if enough data
    if showdown_tracker is not None and showdown_tracker.get("n_showdowns", 0) >= 2:
        observed = showdown_tracker.get("line_strengths", {}).get(line, [])
        if len(observed) >= 2:
            avg_observed = sum(observed) / len(observed)
            # observed strength is 0–1; centre at 0.5 and scale to a small delta
            observed_signal = (avg_observed - 0.5) * 0.08
            # Blend: 60 % lookup, 40 % observed
            threshold_delta = 0.6 * lookup_signal + 0.4 * observed_signal

    # Fold delta: positive → fold more (opponent is strong), negative → fold less
    fold_delta = 0.0
    if lookup_signal > 0.02:
        fold_delta = lookup_signal * 0.5
    elif lookup_signal < -0.01:
        fold_delta = lookup_signal * 0.3

    return {
        "threshold_delta": clamp(threshold_delta, -0.04, 0.04),
        "fold_delta": clamp(fold_delta, -0.02, 0.02),
        "label": line,
    }
