from constants import N_PLAYERS, BIG_BLIND, TOTAL_HANDS, SIMULATIONS_BY_PUBLIC_COUNT, EXTRA_SIMULATIONS_BY_PUBLIC_COUNT
from card_utils import clamp
from state import (
    reconstruct_state, get_remaining_hands, estimate_preflop_strength,
    is_preflop_3bet_candidate, is_preflop_trash_hand,
)
from tournament import (
    should_lock_win, fold_gives_opponent_lock, match_risk_adjustment,
    match_pressure_profile, apply_anti_lock_pressure, anti_lock_can_continue,
)
from opponent import build_opponent_model, analyze_current_spot
from postflop import (
    made_hand_metric, pair_board_profile, pair_domination_margin,
    marginal_pair_under_pressure, board_texture_profile,
    paired_board_outcome_profile, bet_size_bucket, value_hand_tier,
    value_bet_plan, empty_draw_profile, draw_profile, draw_potential,
    draw_call_margin, made_flush_profile, blocker_bluff_profile,
    allow_low_frequency_blocker_bluff, nutted_risk_profile,
    check_probe_resistance_margin, must_continue_vs_raise,
    _board_completion_risk, _should_check_river_weak,
)
from simulation import (
    build_opponent_range, estimate_weighted_win_rate,
)


def opponent_pressure_adjustment(opponent_model, spot_info, round_idx):
    confidence = opponent_model["confidence"]
    adjustment = 0.0

    if spot_info["facing_raise"] or spot_info["facing_allin"]:
        adjustment += confidence * max(0.0, 0.44 - opponent_model["pfr"]) * 0.07
        if round_idx > 0:
            adjustment += confidence * max(0.0, 0.36 - opponent_model["postflop_aggr"]) * 0.06
        adjustment -= confidence * max(0.0, opponent_model["allin_rate"] - 0.08) * 0.08
        adjustment -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.48) * 0.05
        adjustment += min(0.04, spot_info["last_raise_pot_ratio"] * 0.04)

    return clamp(adjustment, -0.05, 0.07)


def aggressive_line_strength(spot_info, board_texture):
    strength = 0.0
    if spot_info.get("opp_postflop_bet_count", 0) >= 2:
        strength += 0.04
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        strength += 0.08 if board_texture is not None and board_texture["paired"] else 0.05
    if spot_info.get("opp_current_round_bet_count", 0) >= 3:
        strength += 0.03
    return clamp(strength, 0.0, 0.15)


def choose_anti_lock_pressure_action(
    state,
    my_chips,
    to_call,
    pot,
    round_idx,
    win_rate,
    opponent_model,
    remaining_hands,
    preflop_strength=None,
    value_profile=None,
    draw_info=None,
    blocker_profile=None,
    board_texture=None,
):
    if state["opponent_allin"] or my_chips <= 1:
        return None
    if to_call >= my_chips:
        return -2

    hands_left = remaining_hands if remaining_hands is not None else TOTAL_HANDS
    pot_after_call = pot + to_call
    fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
    confidence = opponent_model.get("confidence", 0.0)

    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    draw_quality = draw_info.get("quality", 0.0) if draw_info is not None else 0.0
    has_draw = draw_info.get("semi_bluff", False) if draw_info is not None else False
    has_blocker = blocker_profile is not None and blocker_profile.get("eligible", False)

    weak_showdown = tier in ("none", "thin") and draw_quality < 0.14 and win_rate < 0.45
    high_fold_pressure = confidence < 0.20 or fold_to_raise >= 0.42
    emergency_jam = (
        hands_left <= 3
        or (to_call > 0 and to_call / max(1, pot) >= 0.35)
        or (weak_showdown and high_fold_pressure and hands_left <= 6)
        or (win_rate < 0.18 and hands_left <= 5)
    )
    if tier in ("strong", "nut") or has_draw:
        emergency_jam = emergency_jam and hands_left <= 3

    if emergency_jam:
        return -2

    min_raise_action = state.get("min_raise_action", state["round_raise"])

    if round_idx == 0:
        ratio = 2.20 if to_call == 0 else 2.60
        target = int(to_call + pot_after_call * ratio)
        strength = preflop_strength if preflop_strength is not None else win_rate
        target = max(target, int((5.5 + max(0.0, strength - 0.50) * 3.0) * BIG_BLIND) - state["my_round_bet"])
    elif round_idx == 1:
        target = int(to_call + pot_after_call * 1.15)
    elif round_idx == 2:
        target = int(to_call + pot_after_call * 1.35)
    else:
        target = int(to_call + pot_after_call * 1.55)

    if board_texture is not None and board_texture.get("dynamic", False):
        target = int(target * 1.08)
    if has_blocker or has_draw:
        target = int(target * 1.06)
    if weak_showdown:
        target = int(target * 1.12)

    amount = max(min_raise_action, target)
    if amount >= my_chips * 0.72:
        return -2
    amount = min(amount, my_chips - 1)
    if amount <= to_call or amount < min_raise_action:
        return -2 if hands_left <= 4 else None
    return amount


def paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx):
    info = {
        "active": False,
        "severe": False,
        "line_strength": 0.0,
        "size_bucket": "small",
    }

    if round_idx <= 0 or board_texture is None or not board_texture["paired"]:
        return info

    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    line_strength = 0.0
    active = False

    if paired_board_profile is not None and paired_board_profile["board_two_pair"]:
        active = True
        line_strength += 0.05
    elif pair_profile is not None and pair_profile["pair_type"] == "overpair":
        active = True
        line_strength += 0.04

    if not active:
        return info

    if spot_info["facing_postflop_aggression"]:
        line_strength += 0.03
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        line_strength += 0.08
    elif size_bucket in ("medium", "large"):
        line_strength += 0.04
    if round_idx >= 2:
        line_strength += 0.02

    info["active"] = True
    info["severe"] = (
        spot_info["facing_postflop_aggression"]
        and spot_info.get("opp_current_round_bet_count", 0) >= 2
        and size_bucket in ("medium", "large")
    )
    info["line_strength"] = clamp(line_strength, 0.0, 0.18)
    info["size_bucket"] = size_bucket
    return info


def postflop_call_margin(spot_info, opponent_model, made_strength, draw_strength, round_idx, has_position):
    if round_idx <= 0:
        return 0.0

    margin = 0.0
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    weak_showdown = made_strength < 0.22
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])

    if weak_showdown:
        margin += 0.012
    if air_hand:
        margin += 0.018

    if spot_info["facing_postflop_aggression"]:
        margin += 0.008
        if size_bucket == "small":
            margin += 0.020
        elif size_bucket == "medium":
            margin += 0.010
        else:
            margin += 0.024

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            margin += 0.024 if size_bucket == "small" else 0.014
        if round_idx >= 2 and air_hand:
            margin += 0.010
        if round_idx == 3 and size_bucket == "large":
            margin += 0.020

    if not has_position:
        margin += 0.008

    confidence = opponent_model["confidence"]
    if air_hand:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.015
    else:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.008

    return clamp(margin, 0.0, 0.08)


def _per_street_barrel_profile(spot_info, opponent_model, round_idx, req):
    """Calibrate defense against per-street opponent barrel patterns.

    Analyzes multi-street opponent actions to detect whether the current
    aggression is a continuation of prior-street aggression (barrel) or
    a new aggression line. This provides a street-level resolution that
    complements the broader _opponent_line_context.

    Returns dict with:
      barrel_count: int — how many postflop streets opponent has bet
      current_street_barrel: bool — opponent is continuing on this street
      is_multi_barrel: bool — opponent bet 2+ postflop streets
      defense_adjustment: float — margin adjustment for call/fold calibration
        positive = fold more (opponent barrel is credible)
        negative = call more (opponent barrel is less credible)
    """
    my_id = req["my_id"]
    opponent_id = (my_id + 1) % N_PLAYERS
    history = req.get("history", [])

    # Collect per-street opponent bet/raise actions and sizing data
    street_bets = {}
    street_raise_totals = {}
    for record in history:
        if record["player_id"] != opponent_id:
            continue
        rnd = record["round"]
        if rnd <= 0:
            continue
        if record["action_type"] in ("raise", "allin"):
            if rnd not in street_bets:
                street_bets[rnd] = 0
            street_bets[rnd] += 1
            # Track raise sizing for trend detection
            if rnd not in street_raise_totals:
                street_raise_totals[rnd] = []
            if record["action_type"] == "raise":
                street_raise_totals[rnd].append(record["action"])
            else:
                # All-in treated as maximum escalation
                street_raise_totals[rnd].append(100000)

    barrel_count = len(street_bets)
    current_street_barrel = round_idx in street_bets and round_idx > 0
    is_multi_barrel = barrel_count >= 2

    # Derive defense adjustment from opponent model + barrel pattern
    confidence = opponent_model.get("confidence", 0.0)
    postflop_aggr = opponent_model.get("postflop_aggr", 0.36)

    defense_adjustment = 0.0
    if is_multi_barrel and round_idx >= 2:
        # Multi-barrel on turn/river is a stronger signal
        # Scale by how much the opponent's aggression exceeds baseline
        aggr_signal = max(0.0, postflop_aggr - 0.42) * confidence
        defense_adjustment += aggr_signal * 0.08
        # Triple barrel (3 streets) is even stronger
        if barrel_count >= 3:
            defense_adjustment += confidence * 0.04
    elif current_street_barrel and round_idx == 1:
        # Single flop barrel — context depends on opponent tendency
        # Aggressive opponents barrel wide, passive ones barrel strong only
        if postflop_aggr < 0.35:
            # Passive opponent betting flop → stronger range → fold more
            defense_adjustment += confidence * 0.02

    # Compute sizing trend across streets: escalating / declining / stable
    # Escalating barrels (bigger bets on later streets) indicate value-heavy
    # ranges. Declining barrels (smaller bets on later streets) suggest the
    # opponent is giving up on semi-bluffs or has a weakened range.
    sizing_trend = "none"
    sizing_defense_modifier = 0.0
    sorted_betting_streets = sorted(street_raise_totals.keys())
    if len(sorted_betting_streets) >= 2:
        avg_sizings = []
        for street in sorted_betting_streets:
            sizes = street_raise_totals[street]
            avg_sizings.append(sum(sizes) / len(sizes))

        if avg_sizings[0] > 0:
            normalized_change = (avg_sizings[-1] - avg_sizings[0]) / avg_sizings[0]
        else:
            normalized_change = 0.0

        if normalized_change > 0.30:
            sizing_trend = "escalating"
            sizing_defense_modifier = confidence * min(0.04, abs(normalized_change) * 0.05)
        elif normalized_change < -0.30:
            sizing_trend = "declining"
            sizing_defense_modifier = -confidence * min(0.03, abs(normalized_change) * 0.04)
        else:
            sizing_trend = "stable"

    return {
        "barrel_count": barrel_count,
        "current_street_barrel": current_street_barrel,
        "is_multi_barrel": is_multi_barrel,
        "defense_adjustment": defense_adjustment + sizing_defense_modifier,
        "sizing_trend": sizing_trend,
    }


def _line_context_margin_adjustment(line_context, round_idx):
    """Derive a call-margin adjustment from opponent's betting line pattern.

    The opponent line context encodes multi-street behavioral signals:
    - Triple barrel (strength_indicator ~0.85) → opponent range is strong → increase fold tendency
    - Float bet (strength_indicator ~0.40) → opponent is polarized → decrease fold tendency
    - Single barrel (strength_indicator ~0.35) → weak aggression → minimal adjustment

    The adjustment is derived from the already-computed strength_indicator,
    centered on its neutral baseline of 0.50. Multi-street aggression compounds
    the signal on later streets.
    """
    if line_context is None or round_idx <= 0:
        return 0.0

    strength = line_context.get("strength_indicator", 0.50)
    # Deviation from baseline: positive means opponent line appears stronger
    adjustment = (strength - 0.50) * 0.04

    # Multi-street aggression is a stronger signal on turn/river
    if line_context.get("multi_street_aggr", False) and round_idx >= 2:
        adjustment += 0.02

    return adjustment


def _spr_commitment_guard(state, my_chips, value_profile, round_idx=0):
    """Determine if we're pot-committed based on stack-to-pot ratio.

    At low SPR, the cost of folding (surrendering the pot) is large relative
    to the remaining stack we'd save. This structural guard prevents
    over-folding when we've already invested heavily and have a made hand.

    On the river (round_idx == 3), the commitment threshold is widened for
    thin hands because there are no more cards to come — the pot equity
    locked in by calling is fully realized, not speculative.

    Returns True if the hand is pot-committed and should not be folded.
    """
    pot = max(1, state["pot"])
    spr = my_chips / pot

    tier = value_profile.get("tier", "none") if value_profile else "none"

    # At very low SPR with any decent made hand, we're pot-committed
    if spr <= 0.5:
        return tier in ("strong", "nut", "thin")

    # At low SPR with strong/nut hands, folding is usually a mistake
    # On the river, include thin hands at this SPR level because there
    # are no more streets to realize equity — the hand is what it is
    if spr <= 1.0:
        if round_idx == 3:
            return tier in ("strong", "nut", "thin")
        return tier in ("strong", "nut")

    return False


def _opponent_aggression_credibility(opponent_model, spot_info, line_context, round_idx):
    """Assess how credible the opponent's postflop aggression is.

    Returns a float in [0.0, 1.0] indicating aggression credibility:
    - High (~0.7+): opponent's aggression likely represents a strong hand
    - Low (~0.3-): opponent is likely over-aggressive or bluffing

    This modulates fold decisions: against low-credibility aggression,
    fragile fold checks are suppressed (the bot calls more); against
    high-credibility aggression, the bot respects the aggression and folds.
    """
    confidence = opponent_model.get("confidence", 0.0)
    if confidence < 0.10:
        return 0.50  # No model data — neutral assumption

    postflop_aggr = opponent_model.get("postflop_aggr", 0.36)
    fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
    aggression = opponent_model.get("aggression", 0.30)

    credibility = 0.50

    # Passive opponents who bet have stronger ranges → higher credibility
    if postflop_aggr < 0.30:
        credibility += confidence * (0.30 - postflop_aggr) * 1.2
    # Aggressive opponents bet wider ranges → lower credibility
    elif postflop_aggr > 0.48:
        credibility -= confidence * (postflop_aggr - 0.48) * 1.5

    # Opponents who rarely fold to raises are honest bettors
    if fold_to_raise < 0.35:
        credibility += confidence * (0.35 - fold_to_raise) * 0.5
    # Opponents who fold often may be exploiting with bluffs
    elif fold_to_raise > 0.55:
        credibility -= confidence * (fold_to_raise - 0.55) * 0.4

    # Overall aggression rate refinement
    if aggression > 0.40:
        credibility -= confidence * (aggression - 0.40) * 0.6

    # Line context: multi-barrel from a passive player is highly credible
    if line_context is not None:
        line_type = line_context.get("line_type", "standard")
        if line_type == "triple_barrel":
            # Triple barrel is strong, but from aggressive players could be a bluff spree
            if postflop_aggr < 0.40:
                credibility += confidence * 0.12
            else:
                credibility += confidence * 0.04
        elif line_type == "double_barrel":
            if postflop_aggr < 0.38:
                credibility += confidence * 0.08
        elif line_type == "float_bet":
            # Float bet is polarized — moderate credibility reduction
            credibility -= confidence * 0.04
        elif line_type == "snap_reraise":
            # Snap reraise is strong but aggression-scaled
            if postflop_aggr < 0.35:
                credibility += confidence * 0.10

    return clamp(credibility, 0.15, 0.85)


def realized_postflop_equity(
    win_rate,
    made_strength,
    draw_strength,
    round_idx,
    has_position,
    spot_info,
    pair_profile=None,
    board_texture=None,
):
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    if round_idx <= 0:
        return win_rate

    # Classify hand category for board texture equity modifier
    if air_hand:
        hand_category = "air"
    elif pair_profile is not None and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]
        if pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
            hand_category = "weak_pair"
        elif pair_type == "top_pair" and pair_profile["weak_kicker"]:
            hand_category = "top_pair_weak_kicker"
        else:
            hand_category = "medium_pair"
    elif made_strength >= 0.50:
        hand_category = "strong"
    else:
        hand_category = None

    # Compute texture modifier once — wet/dynamic boards reduce equity
    # realization for vulnerable hand categories
    texture_modifier = _board_texture_equity_modifier(board_texture, hand_category) if hand_category else 1.0

    eqr = 1.0

    if air_hand:
        eqr = 0.72 if has_position else 0.62

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            eqr -= 0.10
        if round_idx == 2:
            eqr -= 0.05
        elif round_idx == 3:
            eqr -= 0.12

        eqr = clamp(eqr, 0.45, 0.85)
        return win_rate * eqr * texture_modifier

    if pair_profile is not None and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]

        if pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
            eqr = 0.86 if has_position else 0.78

            if pair_profile["weak_kicker"]:
                eqr -= 0.05
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.06
            if round_idx == 3:
                eqr -= 0.06

            eqr = clamp(eqr, 0.65, 0.92)
            return win_rate * eqr * texture_modifier

        if pair_type == "top_pair" and pair_profile["weak_kicker"]:
            eqr = 0.92 if has_position else 0.86
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.04
            eqr = clamp(eqr, 0.75, 0.95)
            return win_rate * eqr * texture_modifier

    return win_rate


def _board_texture_equity_modifier(board_texture, hand_category):
    """Compute how board texture affects equity realization for a hand category.

    Board texture has a structural impact on equity realization:
    - Wet boards reduce equity for marginal made hands because opponents
      can draw out more easily
    - Dynamic boards specifically hurt vulnerable pairs that can't improve
    - Dry boards preserve equity for made hands
    - The modifier is multiplicative on the equity realization ratio

    hand_category: "air" | "weak_pair" | "medium_pair" | "top_pair_weak_kicker" | "strong"
    Returns a multiplicative modifier (typically 0.94-1.00).
    """
    if board_texture is None:
        return 1.0

    wetness = board_texture.get("wetness", 0.0)
    is_dynamic = board_texture.get("dynamic", False)
    flush_pressure = board_texture.get("flush_pressure", 0.0)
    straight_pressure = board_texture.get("straight_pressure", 0.0)

    modifier = 1.0

    if hand_category == "weak_pair":
        # Weak pairs (bottom, middle, underpair, board pair) are most
        # vulnerable to board texture — draws completing beat them AND
        # they can't improve to beat better hands
        if flush_pressure >= 0.75:
            modifier -= wetness * 0.06
        if straight_pressure >= 0.65:
            modifier -= wetness * 0.04
        if is_dynamic:
            modifier -= (wetness - 0.20) * 0.05 if wetness > 0.20 else 0.0
    elif hand_category == "top_pair_weak_kicker":
        # Top pair weak kicker is moderately affected — it beats most
        # draws but can be outdrawn by a better kicker + texture shift
        if is_dynamic and wetness > 0.30:
            modifier -= (wetness - 0.30) * 0.03
    elif hand_category == "air":
        # Air hands: wet boards mean more semi-bluff potential for us
        # but also more draws opponent can call with, reducing fold equity
        if is_dynamic:
            modifier -= wetness * 0.02
    # "medium_pair" and "strong": minimal impact from texture alone

    return modifier


def _opponent_line_context(spot_info, round_idx, req):
    """Classify the opponent's betting line pattern across streets.

    Analyzes per-street opponent actions to detect common patterns that
    provide structural context for call/fold/raise decisions:
    - triple_barrel: opponent bet all 3 postflop streets → strong value
    - double_barrel: opponent bet flop + turn → credible value range
    - float_bet: opponent called flop, then bet turn → polarized range
    - delayed_aggression: opponent checked flop, bet later street → trap or hit
    - standard: no particular pattern detected

    Returns dict with:
      line_type: str pattern name
      strength_indicator: float 0.0-1.0 (higher = line appears stronger)
      multi_street_aggr: bool whether opponent showed multi-street aggression
    """
    my_id = req["my_id"]
    opponent_id = (my_id + 1) % N_PLAYERS
    history = req.get("history", [])

    # Collect per-street opponent actions
    street_actions = {}
    for record in history:
        if record["player_id"] != opponent_id:
            continue
        rnd = record["round"]
        if rnd not in street_actions:
            street_actions[rnd] = []
        street_actions[rnd].append(record["action_type"])

    preflop = street_actions.get(0, [])
    flop = street_actions.get(1, [])
    turn = street_actions.get(2, [])
    river = street_actions.get(3, [])

    flop_bet = "raise" in flop or "allin" in flop
    flop_check = "check" in flop and not flop_bet
    flop_call = "call" in flop
    turn_bet = "raise" in turn or "allin" in turn
    turn_check = "check" in turn and not turn_bet
    river_bet = "raise" in river or "allin" in river

    line_type = "standard"
    strength_indicator = 0.50
    multi_street_aggr = False

    # Triple barrel: bet all three postflop streets — very strong line
    if flop_bet and turn_bet and river_bet:
        line_type = "triple_barrel"
        strength_indicator = 0.85
        multi_street_aggr = True

    # Double barrel: bet flop + turn — credible value range
    elif flop_bet and turn_bet and not river_bet:
        line_type = "double_barrel"
        strength_indicator = 0.65
        multi_street_aggr = True

    # Float bet: called flop, then bet turn — polarized (bluff or strong)
    elif (flop_call or flop_check) and turn_bet and not flop_bet:
        line_type = "float_bet"
        strength_indicator = 0.40
        multi_street_aggr = False

    # Delayed aggression: checked flop, bet turn or river
    elif flop_check and not flop_bet and (turn_bet or river_bet):
        line_type = "delayed_aggression"
        strength_indicator = 0.55
        multi_street_aggr = turn_bet and river_bet

    # Single street aggression
    elif flop_bet and not turn_bet and not river_bet:
        line_type = "single_barrel"
        strength_indicator = 0.35
        multi_street_aggr = False

    # Snap re-raise in same street: opponent raised back immediately
    if spot_info.get("opp_current_round_bet_count", 0) >= 2 and round_idx > 0:
        line_type = "snap_reraise"
        strength_indicator = 0.75
        multi_street_aggr = True

    return {
        "line_type": line_type,
        "strength_indicator": strength_indicator,
        "multi_street_aggr": multi_street_aggr,
    }


def _thin_value_feasibility(value_profile, board_texture, draw_strength,
                            opponent_model, round_idx, pot, to_call,
                            anti_lock_pressure, match_profile):
    """Determine whether a thin value hand should bet or check on late streets.

    Unlike the static thin_static_showdown_control, this function incorporates
    opponent modeling: against calling stations (high vpip, low fold_to_raise),
    thin value bets are more profitable because the opponent calls with worse.
    Against tight players (low vpip, high fold_to_raise), thin value bets lose
    money because the opponent only calls with better hands.

    Returns True if the hand should check (thin value bet is not feasible).
    Returns False if a thin value bet is warranted.
    """
    if round_idx < 2:
        return False
    if value_profile is None or value_profile["tier"] != "thin":
        return False
    if anti_lock_pressure:
        return False
    if board_texture is not None and board_texture["dynamic"]:
        return False
    if draw_strength >= 0.12:
        return False
    if to_call > 0:
        return False

    confidence = opponent_model.get("confidence", 0.0)
    fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
    vpip = opponent_model.get("vpip", 0.58)

    # Base case: static check for thin hands on dry boards
    should_check = True

    # Opponent-model overrides:
    if confidence >= 0.20:
        # Calling stations (high vpip + low fold_to_raise) call with worse
        # → thin value bets are profitable → don't check
        calling_station_score = max(0.0, vpip - 0.55) - max(0.0, fold_to_raise - 0.45)
        if calling_station_score > 0.10:
            should_check = False
        # Tight players (low vpip + high fold_to_raise) only call with better
        # → thin value bets are unprofitable → check (keep should_check=True)

    # Match pressure modulation: when chasing, thin value bets are less relevant
    if match_profile.get("chase", 0.0) > 0.50:
        should_check = True

    return should_check


def _calibrated_pot_odds(pot_odds, opponent_model, spot_info, round_idx, made_strength, value_profile):
    """Adjust raw pot-odds based on opponent tendencies and hand strength.

    Against aggressive opponents who bet a wide range, our raw equity
    realization is better than pot-odds suggest (they have more bluffs).
    Against passive opponents who only bet strong hands, we need more
    equity than pot-odds imply (their range is polarized to value).
    This structural adjustment factors opponent modeling into the
    call/fold breakpoint without changing any numeric constants elsewhere.
    """
    if round_idx <= 0:
        return pot_odds

    confidence = opponent_model.get("confidence", 0.0)
    if confidence < 0.15:
        return pot_odds

    adjusted = pot_odds

    # Passive opponents bet only strong hands → require more equity
    postflop_aggr = opponent_model.get("postflop_aggr", 0.42)
    aggr_delta = postflop_aggr - 0.50
    if aggr_delta < 0 and spot_info.get("facing_postflop_aggression", False):
        # Passive bettor → their range is strong → increase required odds
        adjusted += confidence * abs(aggr_delta) * 0.15
    elif aggr_delta > 0 and spot_info.get("facing_postflop_aggression", False):
        # Aggressive bettor → wider range including bluffs → relax required odds
        adjusted -= confidence * aggr_delta * 0.10

    # Late-street refinement: on the river, opponent aggression signal is cleaner
    if round_idx == 3 and spot_info.get("facing_postflop_aggression", False):
        opp_bets = spot_info.get("opp_current_round_bet_count", 0)
        if opp_bets >= 2 and aggr_delta > 0:
            # Multi-barrel from aggressive player — even more likely to include bluffs
            adjusted -= confidence * 0.03

    # Strong made hands get a discount on required odds (they beat most of range)
    tier = value_profile.get("tier", "none") if value_profile else "none"
    if tier in ("strong", "nut") and made_strength >= 0.55:
        adjusted -= confidence * 0.02

    return adjusted


def choose_raise(
    min_raise,
    my_chips,
    my_round_bet,
    to_call,
    pot,
    win_rate,
    round_idx,
    spot_name,
    preflop_strength,
    has_position,
    opponent_model,
    semi_bluff=False,
    value_profile=None,
    value_plan=None,
    board_texture=None,
    draw_info=None,
    blocker_bluff=False,
    probe_mode=False,
    pressure_line=False,
    induce_mode=False,
    nutted_risk_score=0.0,
    match_sizing_delta=0.0,
):
    if my_chips <= max(min_raise, to_call) + 1:
        return None

    pot_after_call = pot + to_call
    confidence = opponent_model["confidence"]
    fold_to_raise = opponent_model["fold_to_raise"]
    if value_profile is None:
        value_profile = {"tier": "none", "size_bonus": 0.0}
    if value_plan is None:
        value_plan = {"size_delta": 0.0, "induce": False, "protect": False, "thin_control": False}
    if board_texture is None:
        board_texture = {"wetness": 0.0, "dynamic": False}
    if draw_info is None:
        draw_info = empty_draw_profile()
    wetness = board_texture["wetness"]

    if round_idx == 0:
        ratio = 0.55 if to_call == 0 else 0.75
    elif round_idx == 1:
        ratio = 0.65
    elif round_idx == 2:
        ratio = 0.70
    else:
        ratio = 0.82

    ratio += max(0.0, win_rate - 0.55) * (0.90 + 0.20 * round_idx)
    ratio += -0.05 if has_position else 0.05
    ratio += confidence * max(0.0, fold_to_raise - 0.52) * (0.20 if semi_bluff else 0.10)
    ratio += value_profile.get("size_bonus", 0.0)
    ratio += value_plan.get("size_delta", 0.0)
    ratio += match_sizing_delta
    if round_idx > 0 and value_profile.get("tier") == "strong" and not semi_bluff and not pressure_line:
        if not board_texture["dynamic"]:
            ratio -= 0.05
        if wetness <= 0.20:
            ratio -= 0.02
    if board_texture["dynamic"]:
        if value_profile.get("tier") in ("strong", "nut"):
            ratio += 0.05 * wetness
        elif value_profile.get("tier") == "thin":
            ratio -= 0.04 * wetness
    if semi_bluff:
        ratio -= 0.08
        ratio += 0.02 * wetness
        ratio += draw_info.get("size_bonus", 0.0)
        if draw_info.get("type") == "gutshot":
            ratio -= 0.04
    if pressure_line:
        ratio += 0.05 + 0.04 * wetness
    if nutted_risk_score > 0.0 and value_profile.get("tier") != "nut":
        ratio -= min(0.10, nutted_risk_score * 0.55)
    if blocker_bluff:
        ratio = min(ratio, 0.54 + 0.18 * wetness + 0.08 * max(0, round_idx - 1))
        ratio += confidence * max(0.0, fold_to_raise - 0.58) * 0.22
    inducing_value = (induce_mode or value_plan.get("induce", False)) and to_call == 0 and value_profile.get("tier") == "nut"
    if inducing_value:
        induce_cap = 0.29 + 0.05 * round_idx + 0.05 * wetness
        ratio = min(ratio, induce_cap)
    if probe_mode:
        probe_ratio = 0.25 + 0.08 * wetness
        if value_profile.get("tier") == "thin":
            probe_ratio += 0.08
        if blocker_bluff and round_idx == 3:
            probe_ratio = max(probe_ratio, 0.34 + 0.08 * wetness)
        elif round_idx == 3:
            probe_ratio += 0.05
        ratio = min(ratio, probe_ratio)
    thin_cap = None
    if value_plan.get("thin_control", False) and value_profile.get("tier") != "nut":
        thin_cap = 0.33 if round_idx <= 2 else 0.35
        ratio = min(ratio, thin_cap)
    low_ratio = 0.28 if inducing_value else 0.22 if probe_mode or (blocker_bluff and to_call == 0) else 0.40
    if thin_cap is not None:
        low_ratio = min(low_ratio, thin_cap)
    ratio = clamp(ratio, low_ratio, 1.45)

    amount = int(to_call + pot_after_call * ratio)

    if round_idx == 0 and preflop_strength is not None:
        if spot_name == "sb_open":
            desired_total = int((2.5 + max(0.0, preflop_strength - 0.58) * 2.2) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)
        elif spot_name == "bb_vs_limp":
            desired_total = int((3.2 + max(0.0, preflop_strength - 0.60) * 2.2) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)

    amount = max(min_raise, amount)
    if semi_bluff and fold_to_raise < 0.45:
        amount = min(amount, max(min_raise, int(to_call + pot_after_call * 0.60)))
    if blocker_bluff:
        bluff_cap = max(min_raise, int(to_call + pot_after_call * (0.45 if round_idx == 3 and to_call == 0 else 0.56 + 0.16 * wetness)))
        amount = min(amount, bluff_cap)
    amount = min(amount, my_chips - 1)

    if amount <= to_call or amount < min_raise or amount >= my_chips:
        return None
    return amount


def _preflop_facing_raise_decision(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    """Handle bb_vs_raise and sb_vs_reraise preflop spots using opponent modeling."""
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    pot = max(1, state["pot"])
    pot_odds = to_call / (pot + to_call) if to_call > 0 else 0.0
    confidence = opponent_model["confidence"]
    pfr = opponent_model["pfr"]
    fold_to_raise = opponent_model["fold_to_raise"]
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))

    is_bb = spot_info["preflop_spot"] == "bb_vs_raise"
    position_offset = -0.02 if is_bb else 0.03

    call_threshold = 0.38 + position_offset + match_adjust
    fold_threshold = 0.32 + position_offset + match_adjust
    reraise_threshold = 0.60 - position_offset + match_adjust

    if confidence >= 0.20:
        call_threshold -= confidence * (pfr - 0.28) * 0.15
        fold_threshold -= confidence * (pfr - 0.28) * 0.10
        reraise_threshold -= confidence * (pfr - 0.28) * 0.08

    if preflop_strength <= fold_threshold and not is_preflop_3bet_candidate(req["my_cards"]):
        return -1

    if is_preflop_3bet_candidate(req["my_cards"]) and preflop_strength >= reraise_threshold:
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            pot,
            max(win_rate, preflop_strength),
            0,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if raise_amount is not None:
            return raise_amount

    if preflop_strength >= call_threshold:
        return 0

    if preflop_strength <= fold_threshold:
        return -1

    return 0


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03
    trash_hand = is_preflop_trash_hand(req["my_cards"], preflop_strength)

    if spot_info["preflop_spot"] == "sb_open":
        open_threshold = 0.49 + match_adjust + 0.02 + match_profile["open_delta"]
        limp_threshold = 0.36 + match_adjust
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            state["pot"],
            max(win_rate, preflop_strength),
            0,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if not trash_hand and preflop_strength >= open_threshold and raise_amount is not None:
            return raise_amount
        if preflop_strength <= limp_threshold - loose_bonus:
            return -1
        return 0

    if spot_info["preflop_spot"] == "bb_vs_limp":
        iso_threshold = 0.57 + match_adjust - loose_bonus + match_profile["open_delta"]
        iso_threshold -= confidence * max(0.0, opponent_model["vpip"] - 0.58) * 0.08
        iso_threshold -= confidence * max(0.0, opponent_model["fold_to_raise"] - 0.52) * 0.05
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            state["pot"],
            max(win_rate, preflop_strength),
            0,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if not trash_hand and preflop_strength >= iso_threshold and raise_amount is not None:
            return raise_amount
        return 0

    if spot_info["preflop_spot"] in ("bb_vs_raise", "sb_vs_reraise"):
        return _preflop_facing_raise_decision(
            req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile,
        )

    return None


def get_action(req, requests):
    my_id = req["my_id"]
    my_chips = req["my_chips"]
    my_cards = req["my_cards"]
    public_cards = req["public_cards"]

    state = reconstruct_state(req)
    if should_lock_win(req, state, my_id):
        return -1

    opponent_model = build_opponent_model(requests, my_id)
    spot_info = analyze_current_spot(req, state)
    round_idx = state["round"]
    to_call = state["to_call"]
    pot = max(1, state["pot"])
    remaining_hands = get_remaining_hands(req)
    match_profile = match_pressure_profile(req, my_id, remaining_hands)
    anti_lock_pressure = fold_gives_opponent_lock(req, state, my_id)
    if anti_lock_pressure:
        match_profile = apply_anti_lock_pressure(match_profile)

    preflop_strength = estimate_preflop_strength(my_cards) if not public_cards else None
    preflop_3bet_candidate = is_preflop_3bet_candidate(my_cards) if preflop_strength is not None else False
    combos, weights = build_opponent_range(my_cards, public_cards, state, opponent_model, spot_info)

    simulations = SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 700)

    win_rate = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, simulations)

    critical_spot = to_call > 0 and (
        to_call / pot >= 0.25 or to_call >= BIG_BLIND * 4 or spot_info["facing_allin"]
    )
    extra = EXTRA_SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 0)
    if critical_spot and extra > 0:
        refined = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, extra)
        win_rate = (win_rate * simulations + refined * extra) / (simulations + extra)

    if round_idx == 0 and preflop_strength is not None:
        spot_action = choose_preflop_spot_action(
            req,
            state,
            spot_info,
            opponent_model,
            preflop_strength,
            win_rate,
            match_profile,
        )
        if spot_action is not None:
            if anti_lock_pressure and spot_action <= 0:
                anti_lock_attack = choose_anti_lock_pressure_action(
                    state,
                    my_chips,
                    to_call,
                    pot,
                    round_idx,
                    win_rate,
                    opponent_model,
                    remaining_hands,
                    preflop_strength=preflop_strength,
                )
                if anti_lock_attack is not None:
                    return anti_lock_attack
                if spot_action == -1 and to_call < my_chips:
                    return 0
            return spot_action

    pot_odds = to_call / (pot + to_call) if to_call > 0 else 0.0
    made_strength = made_hand_metric(my_cards, public_cards) if len(public_cards) >= 3 else 0.0
    pair_profile = pair_board_profile(my_cards, public_cards) if len(public_cards) >= 3 else None
    board_texture = board_texture_profile(public_cards) if len(public_cards) >= 3 else None
    draw_info = draw_profile(my_cards, public_cards, board_texture) if len(public_cards) >= 3 else empty_draw_profile()
    draw_strength = draw_info["quality"]
    marginal_pair = marginal_pair_under_pressure(pair_profile, board_texture) if len(public_cards) >= 3 else False
    paired_board_profile = paired_board_outcome_profile(my_cards, public_cards) if len(public_cards) >= 3 else None
    value_profile = value_hand_tier(my_cards, public_cards, pair_profile, board_texture, paired_board_profile) if len(public_cards) >= 3 else None
    flush_profile = made_flush_profile(my_cards, public_cards, board_texture) if len(public_cards) >= 3 else None
    blocker_profile = blocker_bluff_profile(my_cards, public_cards, pair_profile, board_texture) if len(public_cards) >= 3 else None
    nutted_risk = (
        nutted_risk_profile(my_cards, public_cards, pair_profile, board_texture, value_profile, paired_board_profile)
        if len(public_cards) >= 3
        else {"risk": 0.0, "label": "none", "vulnerable": False}
    )
    value_plan = (
        value_bet_plan(value_profile, board_texture, paired_board_profile, pair_profile, nutted_risk, round_idx, pot)
        if len(public_cards) >= 3
        else {"size_delta": 0.0, "induce": False, "protect": False, "thin_control": False}
    )
    line_strength = aggressive_line_strength(spot_info, board_texture) if len(public_cards) >= 3 else 0.0
    check_resistance = check_probe_resistance_margin(spot_info, opponent_model, round_idx) if len(public_cards) >= 3 else 0.0
    paired_board_stackoff = (
        paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx)
        if len(public_cards) >= 3
        else {"active": False, "severe": False, "line_strength": 0.0, "size_bucket": "small"}
    )
    # Opponent line context: classifies multi-street betting patterns for
    # fold/call calibration (triple barrel, float bet, delayed aggression, etc.)
    line_context = _opponent_line_context(spot_info, round_idx, req) if len(public_cards) >= 3 else None
    # Per-street barrel profile: calibrates defense against multi-barrel patterns
    barrel_profile = _per_street_barrel_profile(spot_info, opponent_model, round_idx, req) if len(public_cards) >= 3 else None
    # SPR commitment guard: prevents over-folding at low stack-to-pot ratios
    spr_committed = _spr_commitment_guard(state, my_chips, value_profile, round_idx) if len(public_cards) >= 3 else False
    repeated_raise_trap = (
        round_idx > 0
        and spot_info["facing_postflop_aggression"]
        and spot_info.get("opp_current_round_bet_count", 0) >= 2
    )
    strong_flush_repressure_continue = (
        flush_profile is not None
        and (
            flush_profile["repressure_continue"]
            or flush_profile["nut_like"]
            or (
                board_texture is not None
                and not board_texture["paired"]
                and flush_profile["high_hole_rank"] >= 12
                and flush_profile["better_unseen_ranks"] <= 1
            )
        )
    )
    hard_repressure_fold = (
        repeated_raise_trap
        and not strong_flush_repressure_continue
        and (value_profile is None or value_profile["tier"] != "nut")
        and (
            (board_texture is not None and board_texture["paired"])
            or bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
        )
    )

    # Detect scare-card completion on turn/river
    completion_risk = _board_completion_risk(my_cards, public_cards, board_texture)
    # If a draw just completed and we don't have a nut/strong hand, treat it
    # like hard_repressure_fold when facing aggression
    completion_folds = (
        (completion_risk["completed_flush"] or completion_risk["completed_straight"])
        and spot_info["facing_postflop_aggression"]
        and (value_profile is None or value_profile["tier"] not in ("nut", "strong"))
        and round_idx >= 2
    )
    if completion_folds:
        hard_repressure_fold = True

    strong = 0.69 if round_idx == 0 else 0.65 if round_idx == 1 else 0.61 if round_idx == 2 else 0.59
    medium = 0.54 if round_idx == 0 else 0.50 if round_idx == 1 else 0.48

    if spot_info["has_position"]:
        strong -= 0.015
        medium -= 0.01
    else:
        strong += 0.02
        medium += 0.015

    if preflop_strength is not None:
        if preflop_strength >= 0.72:
            strong -= 0.03
            medium -= 0.02
        elif preflop_strength <= 0.40:
            strong += 0.04
            medium += 0.03

    match_adjust = match_risk_adjustment(req, my_id, remaining_hands)
    pressure_adjust = opponent_pressure_adjustment(opponent_model, spot_info, round_idx)
    strong += match_adjust + pressure_adjust + match_profile["threshold_delta"]
    medium += match_adjust + pressure_adjust * 0.8 + 0.75 * match_profile["threshold_delta"]
    strong += 0.30 * line_strength + 0.45 * paired_board_stackoff["line_strength"]
    medium += 0.18 * line_strength + 0.22 * paired_board_stackoff["line_strength"]
    strong += 0.30 * check_resistance
    medium += 0.20 * check_resistance
    if value_profile is not None:
        if value_profile["tier"] == "nut":
            strong -= 0.07
            medium -= 0.04
        elif value_profile["tier"] == "strong":
            strong -= 0.04
            medium -= 0.02
        elif value_profile["tier"] == "thin":
            medium -= 0.01
    strong += 0.45 * nutted_risk["risk"]
    medium += 0.30 * nutted_risk["risk"]

    if state["opponent_allin"]:
        jam_cost = max(state["allin_call_amount"], to_call)
        jam_odds = jam_cost / (pot + jam_cost) if jam_cost > 0 else 0.0
        jam_buffer = 0.02 + max(0.0, strong - 0.65) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            jam_buffer += 0.04
        jam_buffer += nutted_risk["risk"]
        jam_buffer += 0.04 * match_profile["protect"]
        jam_buffer += line_strength + paired_board_stackoff["line_strength"]
        jam_buffer += check_resistance
        if remaining_hands == 1:
            total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
            if len(total_win_chips) > my_id and total_win_chips[my_id] < 0:
                jam_buffer -= 0.03
        if preflop_strength is not None and preflop_strength < 0.42:
            jam_buffer += 0.02
        if anti_lock_pressure:
            jam_buffer -= 0.10
        anti_lock_jam_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            jam_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_jam_continue:
                return -1
        jam_buffer = clamp(jam_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= jam_odds + jam_buffer or anti_lock_jam_continue else -1

    if to_call >= my_chips:
        shove_odds = my_chips / (pot + my_chips)
        shove_buffer = 0.01 + max(0.0, strong - 0.64) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            shove_buffer += 0.04
        shove_buffer += nutted_risk["risk"]
        shove_buffer += 0.04 * match_profile["protect"]
        shove_buffer += line_strength + paired_board_stackoff["line_strength"]
        shove_buffer += check_resistance
        if anti_lock_pressure:
            shove_buffer -= 0.10
        anti_lock_shove_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            shove_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_shove_continue:
                return -1
        shove_buffer = clamp(shove_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= shove_odds + shove_buffer or anti_lock_shove_continue else -1

    if to_call > 0:
        if round_idx == 0:
            call_margin = 0.008 + (0.012 if not spot_info["has_position"] else 0.0)
            if preflop_strength is not None and preflop_strength <= 0.40:
                call_margin += 0.015
            realized_rate = win_rate
        else:
            call_margin = postflop_call_margin(
                spot_info,
                opponent_model,
                made_strength,
                draw_strength,
                round_idx,
                spot_info["has_position"],
            )
            call_margin += pair_domination_margin(
                pair_profile,
                spot_info,
                round_idx,
            )
            call_margin += draw_call_margin(
                draw_info,
                board_texture,
                round_idx,
                spot_info,
            )
            if (
                round_idx == 2
                and spot_info["facing_postflop_aggression"]
                and pair_profile is not None
                and pair_profile["made_class"] == 1
                and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair")
            ):
                call_margin += 0.050
            call_margin += line_strength + paired_board_stackoff["line_strength"]
            call_margin += check_resistance
            call_margin += 0.50 * nutted_risk["risk"]
            # Wire opponent line context into call margin:
            # triple barrel → more fold-prone (+margin), float bet → less fold-prone (-margin)
            call_margin += _line_context_margin_adjustment(line_context, round_idx)
            # Per-street barrel calibration: multi-barrel from credible opponent → fold more
            if barrel_profile is not None:
                call_margin += barrel_profile["defense_adjustment"]
            if round_idx == 3 and made_strength < 0.40 and not (blocker_profile and blocker_profile["eligible"]):
                call_margin += 0.04
            if round_idx == 3 and paired_board_profile is not None and paired_board_profile["fold_to_raise"]:
                call_margin += 0.05
            realized_rate = realized_postflop_equity(
                win_rate,
                made_strength,
                draw_strength,
                round_idx,
                spot_info["has_position"],
                spot_info,
                pair_profile,
                board_texture,
            )
        if anti_lock_pressure:
            call_margin -= 0.07
        anti_lock_call_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            pot_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        strong_made_continue = must_continue_vs_raise(
            value_profile,
            made_strength,
            pot_odds,
            nutted_risk,
            board_texture,
            spr=my_chips / max(1, pot),
            round_idx=round_idx,
        )
        anti_lock_attack = None
        if anti_lock_pressure:
            anti_lock_attack = choose_anti_lock_pressure_action(
                state,
                my_chips,
                to_call,
                pot,
                round_idx,
                win_rate,
                opponent_model,
                remaining_hands,
                preflop_strength=preflop_strength,
                value_profile=value_profile,
                draw_info=draw_info,
                blocker_profile=blocker_profile,
                board_texture=board_texture,
            )
        fragile_river_raise_fold = (
            round_idx == 3
            and spot_info["facing_postflop_aggression"]
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
            and paired_board_profile is not None
            and paired_board_profile["fold_to_raise"]
            and paired_board_profile["hand_class"] == 2
            and (value_profile is None or value_profile["tier"] != "nut")
        )
        fragile_pair_raise_fold = (
            round_idx > 0
            and spot_info["facing_postflop_aggression"]
            and marginal_pair
            and draw_strength < 0.14
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
            and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))
        )
        if anti_lock_attack is not None:
            return anti_lock_attack
        # Opponent aggression credibility: modulates fragile fold decisions.
        # When opponent aggression is not credible (over-aggressive/bluffing),
        # fragile fold checks are suppressed to avoid over-folding to bluffs.
        opp_credibility = _opponent_aggression_credibility(
            opponent_model, spot_info, line_context, round_idx,
        )
        # SPR commitment guard: at low SPR with made hands, don't over-fold
        # This check runs BEFORE fragile fold checks to prevent folding
        # strong hands that are pot-committed
        if spr_committed:
            pass  # Skip fragile fold checks — we're pot-committed
        elif opp_credibility < 0.35:
            # Opponent aggression is not credible — suppress fragile folds
            # to avoid being exploited by over-aggressive players
            pass
        else:
            # Crossover from v10: include strong_made_continue guard in fragile fold checks
            # Prevents over-folding genuinely strong hands facing aggression
            if fragile_river_raise_fold:
                if not anti_lock_call_continue and not strong_made_continue:
                    return -1
            if fragile_pair_raise_fold:
                if not anti_lock_call_continue and not strong_made_continue:
                    return -1
            if hard_repressure_fold or paired_board_stackoff["severe"]:
                if not anti_lock_call_continue and not strong_made_continue:
                    return -1
        calibrated_odds = _calibrated_pot_odds(pot_odds, opponent_model, spot_info, round_idx, made_strength, value_profile)
        if realized_rate < calibrated_odds + call_margin:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if repeated_raise_trap and (value_profile is None or value_profile["tier"] != "nut"):
            return 0

        raise_fold_threshold = 0.56 - 0.30 * match_profile["bluff_delta"]
        blocker_raise_threshold = 0.55 - 0.32 * match_profile["bluff_delta"]
        draw_raise_threshold = clamp(raise_fold_threshold - draw_info["fold_threshold_delta"], 0.46, 0.68)
        draw_equity_slack = 0.05 if draw_info["type"] in ("combo_draw", "nut_flush_draw") else 0.03
        semi_bluff = (
            round_idx > 0
            and draw_info["semi_bluff"]
            and draw_strength >= 0.12
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > draw_raise_threshold
            and win_rate >= pot_odds - draw_equity_slack
        )
        blocker_raise = (
            round_idx == 1
            and spot_info["facing_postflop_aggression"]
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > blocker_raise_threshold
            and blocker_profile is not None
            and blocker_profile["eligible"]
            and made_strength < 0.18
            and draw_strength < 0.12
            and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx)
        )
        trap_nut_slowplay = (
            round_idx in (1, 2)
            and value_profile is not None
            and value_profile["tier"] == "nut"
            and board_texture is not None
            and not board_texture["dynamic"]
            and spot_info["facing_postflop_aggression"]
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) != "large"
            and pot < 1400
            and nutted_risk["risk"] <= 0.02
            and match_profile["chase"] <= 0.45
            and opponent_model["confidence"] >= 0.20
            and (
                opponent_model["postflop_aggr"] >= 0.38
                or opponent_model["aggression"] >= 0.34
                or opponent_model["fold_to_raise"] < 0.46
            )
        )
        flop_checkraise_exploit = (
            round_idx == 1
            and spot_info["facing_postflop_aggression"]
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > blocker_raise_threshold
            and (
                (value_profile and value_profile["tier"] in ("strong", "nut"))
                or (draw_info["semi_bluff"] and draw_strength >= 0.15)
                or blocker_raise
            )
        )

        if trap_nut_slowplay:
            return 0
        preflop_defensive_only = (
            round_idx == 0
            and to_call > 0
            and not preflop_3bet_candidate
        )
        if not preflop_defensive_only and (win_rate >= max(strong, pot_odds + 0.12) or semi_bluff or flop_checkraise_exploit):
            raise_amount = choose_raise(
                state["min_raise_action"],
                my_chips,
                state["my_round_bet"],
                to_call,
                pot,
                win_rate,
                round_idx,
                spot_info["preflop_spot"],
                preflop_strength,
                spot_info["has_position"],
                opponent_model,
                semi_bluff=semi_bluff or (flop_checkraise_exploit and draw_info["semi_bluff"] and draw_strength >= 0.15),
                value_profile=value_profile,
                value_plan=value_plan,
                board_texture=board_texture,
                draw_info=draw_info,
                blocker_bluff=blocker_raise,
                pressure_line=flop_checkraise_exploit,
                nutted_risk_score=nutted_risk["risk"],
                match_sizing_delta=match_profile["sizing_delta"],
            )
            if raise_amount is not None and raise_amount > to_call:
                return raise_amount
        return 0

    # Use consolidated river weak-hand check instead of inline booleans
    if _should_check_river_weak(
        round_idx, to_call, pair_profile, made_strength, draw_strength,
        blocker_profile, value_profile, spot_info, paired_board_profile,
        nutted_risk, paired_board_stackoff, board_texture, pot, match_profile,
        opponent_model, barrel_profile,
    ):
        return 0
    big_pot_threshold = int(clamp(1500 - 350 * match_profile["protect"] + 250 * match_profile["chase"], 1100, 1800))
    big_pot = pot >= big_pot_threshold
    induce_nut_value = (
        round_idx > 0
        and to_call == 0
        and value_profile is not None
        and value_profile["tier"] == "nut"
        and board_texture is not None
        and not board_texture["dynamic"]
        and not big_pot
        and match_profile["chase"] <= 0.55
        and opponent_model["confidence"] >= 0.20
        and (
            opponent_model["postflop_aggr"] >= 0.38
            or opponent_model["aggression"] >= 0.34
            or opponent_model["fold_to_raise"] < 0.46
        )
    )
    anti_lock_attack = None
    if anti_lock_pressure:
        anti_lock_attack = choose_anti_lock_pressure_action(
            state,
            my_chips,
            to_call,
            pot,
            round_idx,
            win_rate,
            opponent_model,
            remaining_hands,
            preflop_strength=preflop_strength,
            value_profile=value_profile,
            draw_info=draw_info,
            blocker_profile=blocker_profile,
            board_texture=board_texture,
        )
        if anti_lock_attack is not None:
            return anti_lock_attack

    # Big pot river weak-hand check now lives in _should_check_river_weak above
    # (consolidated into the function for opponent-awareness)
    if _thin_value_feasibility(
        value_profile, board_texture, draw_strength,
        opponent_model, round_idx, pot, to_call,
        anti_lock_pressure, match_profile,
    ):
        return 0

    river_bluff_threshold = 0.62 - 0.28 * match_profile["bluff_delta"]
    probe_fold_threshold = 0.56 - 0.32 * match_profile["bluff_delta"]
    semi_bluff_threshold = 0.58 - 0.28 * match_profile["bluff_delta"]
    draw_bet_threshold = clamp(semi_bluff_threshold - draw_info["fold_threshold_delta"], 0.46, 0.70)
    check_probe_signal = (
        spot_info["last_opp_action_type"] == "check"
        and (
            spot_info.get("opp_postflop_check_count", 0) >= 2
            or (
                opponent_model["confidence"] >= 0.20
                and opponent_model.get("postflop_check_rate", 0.42) >= 0.52
            )
        )
    )
    river_blocker_bluff = (
        round_idx == 3
        and made_strength < 0.16
        and draw_strength < 0.08
        and opponent_model["confidence"] >= 0.35
        and opponent_model["fold_to_raise"] > river_bluff_threshold
        and blocker_profile is not None
        and blocker_profile["eligible"]
        and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx)
    )
    small_probe = (
        round_idx > 0
        and opponent_model["confidence"] >= 0.25
        and opponent_model["fold_to_raise"] > probe_fold_threshold
        and made_strength < 0.62
        and draw_strength < 0.16
        and board_texture is not None
        and board_texture["wetness"] <= 0.32
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
    )
    check_probe = (
        round_idx > 0
        and check_probe_signal
        and board_texture is not None
        and board_texture["wetness"] <= 0.55
        and made_strength < 0.58
        and draw_strength < 0.20
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
        and not (round_idx == 3 and made_strength >= 0.18 and not (blocker_profile and blocker_profile["eligible"]))
    )
    blocker_bluff = (
        river_blocker_bluff
    )
    semi_bluff = (
        round_idx > 0
        and draw_info["semi_bluff"]
        and draw_strength >= 0.12
        and opponent_model["confidence"] >= 0.25
        and opponent_model["fold_to_raise"] > draw_bet_threshold
    )
    if win_rate >= medium or semi_bluff or blocker_bluff or small_probe or check_probe or made_strength >= 0.62 or (value_profile and value_profile["tier"] in ("strong", "nut")):
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            pot,
            win_rate,
            round_idx,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            semi_bluff=semi_bluff and win_rate < medium,
            value_profile=value_profile,
            value_plan=value_plan,
            board_texture=board_texture,
            draw_info=draw_info,
            blocker_bluff=blocker_bluff and win_rate < medium and not semi_bluff,
            probe_mode=check_probe or small_probe or (value_profile and value_profile["tier"] == "thin" and board_texture and not board_texture["dynamic"]),
            induce_mode=induce_nut_value or value_plan.get("induce", False),
            nutted_risk_score=nutted_risk["risk"],
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if raise_amount is not None:
            return raise_amount
    return 0
