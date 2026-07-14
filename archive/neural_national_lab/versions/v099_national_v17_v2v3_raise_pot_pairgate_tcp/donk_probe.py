"""Donk bet and probe bet strategy module for claude_v27.

Donk bet: Leading into the preflop raiser (PFR) as the Big Blind on the flop.
Probe bet: Betting on turn/river after the PFR checked the previous street.

Both are exploitative lines that capitalize on:
- BB's range advantage on certain flop textures (donk)
- PFR's weakness signal from checking back (probe)

Integration: called from strategy.py:get_action() before the standard value/bluff path.
"""

from card_utils import card_number, card_suit, clamp


# ── Donk bet parameters ────────────────────────────────────────────────────────

# Donk only on flop (round_idx == 1)
_DONK_ROUND = 1

# Maximum board wetness for donk (low/dry boards only)
_DONK_MAX_WETNESS = 0.30

# Donk is only for BB (no position) facing a PFR
# We check this via spot_info["my_is_bb"] and preflop_spot == "bb_vs_raise"

# Minimum made strength for value donk
_DONK_MIN_STRENGTH = 0.45

# Maximum made strength for bluff donk
_DONK_MAX_BLUFF_STRENGTH = 0.28

# Minimum draw quality for semi-bluff donk
_DONK_MIN_DRAW_QUALITY = 0.14

# Minimum pot size for donk
_DONK_MIN_POT = 250

# Frequency cap for value donks (not every eligible spot)
_DONK_VALUE_FREQ_CAP = 0.55

# Frequency cap for bluff/semi-bluff donks
_DONK_BLUFF_FREQ_CAP = 0.35

# Minimum opponent confidence to use opponent-model exploitation
_DONK_MIN_CONFIDENCE = 0.20

# Maximum opponent c-bet frequency to justify donking
# (if they c-bet 100%, donking is less valuable than check-raising)
_DONK_MAX_CBET_FREQ = 0.75

# Minimum opponent fold-to-raise to justify bluff donks
_DONK_MIN_FTR_BLUFF = 0.46

# Donk sizing base ratio
_DONK_BASE_RATIO = 0.45

# Donk sizing range
_DONK_MIN_RATIO = 0.35
_DONK_MAX_RATIO = 0.60


# ── Probe bet parameters ───────────────────────────────────────────────────────

# Probe on turn (round_idx == 2) or river (round_idx == 3)
_PROBE_ROUNDS = (2, 3)

# Maximum board wetness for probe
_PROBE_MAX_WETNESS = 0.40

# Minimum made strength for value probe
_PROBE_MIN_STRENGTH = 0.38

# Maximum made strength for bluff probe
_PROBE_MAX_BLUFF_STRENGTH = 0.22

# Minimum draw quality for semi-bluff probe
_PROBE_MIN_DRAW_QUALITY = 0.12

# Minimum pot size for probe
_PROBE_MIN_POT = 400

# Frequency cap for value probes
_PROBE_VALUE_FREQ_CAP = 0.60

# Frequency cap for bluff probes
_PROBE_BLUFF_FREQ_CAP = 0.40

# Minimum opponent confidence
_PROBE_MIN_CONFIDENCE = 0.20

# Probe sizing base ratio
_PROBE_BASE_RATIO = 0.50

# Probe sizing range
_PROBE_MIN_RATIO = 0.40
_PROBE_MAX_RATIO = 0.60


def _donk_frequency_roll(my_cards, public_cards, round_idx):
    """Deterministic frequency roll for donk bets."""
    seed = (sum(my_cards) * 13 + sum(public_cards) * 19 + round_idx * 29) % 1000
    return seed / 1000.0


def _probe_frequency_roll(my_cards, public_cards, round_idx):
    """Deterministic frequency roll for probe bets."""
    seed = (sum(my_cards) * 17 + sum(public_cards) * 23 + round_idx * 31) % 1000
    return seed / 1000.0


def _is_bb_facing_pfr(spot_info):
    """Check if we are BB who faced a preflop raise."""
    return (
        spot_info.get("my_is_bb", False)
        and spot_info.get("preflop_spot") == "bb_vs_raise"
    )


def _pfr_checked_previous_street(history, state):
    """Check if the PFR checked on the previous street.

    For turn probe: PFR checked flop.
    For river probe: PFR checked turn.
    """
    if not history:
        return False

    current_round = state["round"]
    prev_round = current_round - 1
    if prev_round < 1:
        return False

    # Find the last action by the opponent in the previous round
    opponent_id = None
    for record in reversed(history):
        if record["round"] == prev_round:
            if opponent_id is None:
                opponent_id = record["player_id"]
            if record["player_id"] == opponent_id:
                return record["action_type"] == "check"

    return False


def _board_low_disconnected(public_cards):
    """Check if the board is low and disconnected (favorable for BB donk).

    Returns a score 0.0-1.0 where higher means more favorable.
    """
    if len(public_cards) < 3:
        return 0.0

    board_ranks = sorted([card_number(c) for c in public_cards])
    high_card = max(board_ranks)

    # Low boards favor BB's wider range
    low_score = 1.0 - max(0, high_card - 8) * 0.10

    # Disconnected boards favor BB
    gaps = []
    for i in range(1, len(board_ranks)):
        gaps.append(board_ranks[i] - board_ranks[i - 1])
    avg_gap = sum(gaps) / len(gaps) if gaps else 0
    disconnected_score = min(1.0, avg_gap / 3.0)

    # Rainbow boards favor BB (less PFR nut advantage)
    suits = [card_suit(c) for c in public_cards]
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    max_suit = max(suit_counts.values())
    rainbow_score = 1.0 if max_suit == 1 else 0.5 if max_suit == 2 else 0.0

    return clamp((low_score * 0.40 + disconnected_score * 0.35 + rainbow_score * 0.25), 0.0, 1.0)


def should_donk_bet(
    round_idx,
    to_call,
    spot_info,
    value_profile,
    board_texture,
    made_strength,
    draw_strength,
    draw_info,
    opponent_model,
    my_cards,
    public_cards,
    pot,
    history,
    state,
):
    """Determine if we should make a donk bet on the flop as BB.

    Returns a dict:
        {
            "eligible": bool,
            "ratio": float,
            "type": str,       # "value", "semi_bluff", "bluff"
            "frequency": float,
            "reason": str,
        }
    """
    result = {
        "eligible": False,
        "ratio": 0.0,
        "type": "none",
        "frequency": 0.0,
        "reason": "none",
    }

    if round_idx != _DONK_ROUND:
        return result

    if to_call != 0:
        return result

    if not _is_bb_facing_pfr(spot_info):
        return result

    if board_texture is None:
        return result

    if board_texture["wetness"] > _DONK_MAX_WETNESS:
        return result

    if pot < _DONK_MIN_POT:
        return result

    confidence = opponent_model.get("confidence", 0.0)

    # Check opponent c-bet frequency: if they c-bet everything, check-raise is better
    if confidence >= _DONK_MIN_CONFIDENCE:
        # Approximate c-bet freq from flop aggression
        flop_aggr = opponent_model.get("flop_aggr", 0.36)
        if flop_aggr > _DONK_MAX_CBET_FREQ:
            result["reason"] = "opponent_cbets_too_much"
            return result

    # Evaluate hand strength for donk type
    has_draw = draw_info is not None and draw_info.get("semi_bluff", False)
    tier = value_profile.get("tier", "none") if value_profile else "none"

    # Value donk: strong made hand
    if tier in ("strong", "nut") or made_strength >= _DONK_MIN_STRENGTH:
        freq_roll = _donk_frequency_roll(my_cards, public_cards, round_idx)
        if freq_roll > _DONK_VALUE_FREQ_CAP:
            result["reason"] = "value_frequency_cap"
            return result

        ratio = _DONK_BASE_RATIO
        # Adjust for board texture
        if board_texture.get("paired", False):
            ratio -= 0.05
        # Adjust for hand strength
        if tier == "nut":
            ratio += 0.05

        ratio = clamp(ratio, _DONK_MIN_RATIO, _DONK_MAX_RATIO)

        result["eligible"] = True
        result["ratio"] = ratio
        result["type"] = "value"
        result["frequency"] = _DONK_VALUE_FREQ_CAP
        result["reason"] = "bb_value_donk_vs_pfr"
        return result

    # Semi-bluff donk: draw with some fold equity
    if has_draw and draw_strength >= _DONK_MIN_DRAW_QUALITY:
        if confidence >= _DONK_MIN_CONFIDENCE:
            ftr = opponent_model.get("fold_to_raise", 0.44)
            if ftr < _DONK_MIN_FTR_BLUFF:
                result["reason"] = "insufficient_fold_equity"
                return result

        freq_roll = _donk_frequency_roll(my_cards, public_cards, round_idx)
        if freq_roll > _DONK_BLUFF_FREQ_CAP:
            result["reason"] = "semi_bluff_frequency_cap"
            return result

        ratio = _DONK_BASE_RATIO + 0.03
        ratio = clamp(ratio, _DONK_MIN_RATIO, _DONK_MAX_RATIO)

        result["eligible"] = True
        result["ratio"] = ratio
        result["type"] = "semi_bluff"
        result["frequency"] = _DONK_BLUFF_FREQ_CAP
        result["reason"] = "bb_semi_bluff_donk_vs_pfr"
        return result

    # Pure bluff donk: weak hand on very favorable board
    board_favor = _board_low_disconnected(public_cards)
    if board_favor >= 0.70 and made_strength < _DONK_MAX_BLUFF_STRENGTH:
        if confidence >= _DONK_MIN_CONFIDENCE:
            ftr = opponent_model.get("fold_to_raise", 0.44)
            if ftr < _DONK_MIN_FTR_BLUFF + 0.04:
                result["reason"] = "insufficient_bluff_fold_equity"
                return result

        freq_roll = _donk_frequency_roll(my_cards, public_cards, round_idx)
        if freq_roll > _DONK_BLUFF_FREQ_CAP * 0.60:
            result["reason"] = "bluff_frequency_cap"
            return result

        ratio = _DONK_BASE_RATIO - 0.05
        ratio = clamp(ratio, _DONK_MIN_RATIO, _DONK_MAX_RATIO)

        result["eligible"] = True
        result["ratio"] = ratio
        result["type"] = "bluff"
        result["frequency"] = _DONK_BLUFF_FREQ_CAP * 0.60
        result["reason"] = "bb_bluff_donk_favorable_board"
        return result

    result["reason"] = "hand_not_suitable"
    return result


def should_probe_bet(
    round_idx,
    to_call,
    spot_info,
    value_profile,
    board_texture,
    made_strength,
    draw_strength,
    draw_info,
    opponent_model,
    my_cards,
    public_cards,
    pot,
    history,
    state,
):
    """Determine if we should make a probe bet after PFR checked previous street.

    Returns a dict with same structure as should_donk_bet().
    """
    result = {
        "eligible": False,
        "ratio": 0.0,
        "type": "none",
        "frequency": 0.0,
        "reason": "none",
    }

    if round_idx not in _PROBE_ROUNDS:
        return result

    if to_call != 0:
        return result

    # Must be facing a check (not a bet)
    if spot_info.get("last_opp_action_type") != "check":
        return result

    # Must have position (PFR checked to us)
    if not spot_info.get("has_position", False):
        return result

    # PFR must have checked the PREVIOUS street
    if not _pfr_checked_previous_street(history, state):
        return result

    if board_texture is None:
        return result

    if board_texture["wetness"] > _PROBE_MAX_WETNESS:
        return result

    if pot < _PROBE_MIN_POT:
        return result

    confidence = opponent_model.get("confidence", 0.0)
    has_draw = draw_info is not None and draw_info.get("semi_bluff", False)
    tier = value_profile.get("tier", "none") if value_profile else "none"

    # Value probe: medium-to-strong hand that benefits from denying equity
    if tier in ("strong", "nut") or made_strength >= _PROBE_MIN_STRENGTH:
        freq_roll = _probe_frequency_roll(my_cards, public_cards, round_idx)
        if freq_roll > _PROBE_VALUE_FREQ_CAP:
            result["reason"] = "value_frequency_cap"
            return result

        ratio = _PROBE_BASE_RATIO
        if tier == "nut":
            ratio += 0.05
        elif tier == "thin":
            ratio -= 0.05

        ratio = clamp(ratio, _PROBE_MIN_RATIO, _PROBE_MAX_RATIO)

        result["eligible"] = True
        result["ratio"] = ratio
        result["type"] = "value"
        result["frequency"] = _PROBE_VALUE_FREQ_CAP
        result["reason"] = "probe_value_after_pfr_check"
        return result

    # Semi-bluff probe: draw with fold equity
    if has_draw and draw_strength >= _PROBE_MIN_DRAW_QUALITY:
        if confidence >= _PROBE_MIN_CONFIDENCE:
            ftr = opponent_model.get("fold_to_raise", 0.44)
            if ftr < 0.42:
                result["reason"] = "insufficient_fold_equity"
                return result

        freq_roll = _probe_frequency_roll(my_cards, public_cards, round_idx)
        if freq_roll > _PROBE_BLUFF_FREQ_CAP:
            result["reason"] = "semi_bluff_frequency_cap"
            return result

        ratio = _PROBE_BASE_RATIO + 0.03
        ratio = clamp(ratio, _PROBE_MIN_RATIO, _PROBE_MAX_RATIO)

        result["eligible"] = True
        result["ratio"] = ratio
        result["type"] = "semi_bluff"
        result["frequency"] = _PROBE_BLUFF_FREQ_CAP
        result["reason"] = "probe_semi_bluff_after_pfr_check"
        return result

    # Thin value probe: medium pair, top pair weak kicker on dry board
    if tier == "thin" and made_strength >= 0.30 and board_texture["wetness"] <= 0.20:
        freq_roll = _probe_frequency_roll(my_cards, public_cards, round_idx)
        if freq_roll > _PROBE_VALUE_FREQ_CAP * 0.50:
            result["reason"] = "thin_value_frequency_cap"
            return result

        ratio = _PROBE_BASE_RATIO - 0.08
        ratio = clamp(ratio, _PROBE_MIN_RATIO, _PROBE_MAX_RATIO)

        result["eligible"] = True
        result["ratio"] = ratio
        result["type"] = "thin_value"
        result["frequency"] = _PROBE_VALUE_FREQ_CAP * 0.50
        result["reason"] = "probe_thin_value_after_pfr_check"
        return result

    result["reason"] = "hand_not_suitable"
    return result


def donk_probe_sizing(
    ratio,
    to_call,
    pot,
    min_raise,
    my_chips,
    my_round_bet,
):
    """Compute exact donk/probe bet sizing.

    Args:
        ratio: bet ratio (0.35-0.60)
        to_call: amount to call (should be 0 for donk/probe)
        pot: current pot
        min_raise: minimum legal raise
        my_chips: remaining chips
        my_round_bet: already committed this round

    Returns:
        int: raise-to-total amount, or None if invalid
    """
    pot_after_call = pot + to_call
    target = int(to_call + pot_after_call * ratio)
    amount = max(min_raise, target)
    amount = min(amount, my_chips - 1)

    if amount <= to_call or amount < min_raise or amount >= my_chips:
        return None
    return amount
