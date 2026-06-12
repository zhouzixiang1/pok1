"""Overbet strategy module for claude_v27.

Provides river overbet sizing (1.3x-1.8x pot) for nut hands on dry/static boards.
Designed to maximize value against bluff-catchers and induce crying calls.

Integration: called from strategy.py:get_action() before the standard value bet path.
Only fires on the river (round_idx == 3) with nut-tier hands on dry/static boards.
"""

from card_utils import card_number, card_suit, clamp, evaluate_best


# ── Overbet eligibility thresholds ─────────────────────────────────────────────

# Minimum hand tier required for overbet
_OVERBET_MIN_TIER = "nut"

# Maximum board wetness for overbet (dry/static only)
_OVERBET_MAX_WETNESS = 0.25

# Maximum board dynamic flag (must be static)
_OVERBET_ALLOW_DYNAMIC = False

# Maximum nutted risk score for overbet (must be near-unbreakable)
_OVERBET_MAX_RISK = 0.02

# Minimum pot size for overbet (must be meaningful)
_OVERBET_MIN_POT = 800

# Minimum opponent confidence to use opponent-model exploitation
_OVERBET_MIN_CONFIDENCE = 0.20

# Maximum opponent postflop aggression to avoid overbetting into trappers
_OVERBET_MAX_POSTFLOP_AGGR = 0.48

# Minimum opponent fold-to-raise to justify overbet (they call too much)
_OVERBET_MIN_FTR = 0.40

# Frequency cap: overbet at most 65% of eligible spots (hand-seeded)
_OVERBET_FREQ_CAP = 0.65

# ── Overbet sizing parameters ──────────────────────────────────────────────────

# Base overbet ratio relative to pot_after_call
_OVERBET_BASE_RATIO = 1.30

# Maximum overbet ratio
_OVERBET_MAX_RATIO = 1.80

# Ratio increment per 0.10 of made_strength above 0.70
_OVERBET_STRENGTH_INCREMENT = 0.12

# Ratio adjustment for opponent fold-to-raise (higher FTR = smaller overbet)
_OVERBET_FTR_ADJUST = -0.15

# Ratio adjustment for board dry_score (drier = larger overbet)
_OVERBET_DRY_ADJUST = 0.20

# Minimum effective stack ratio (overbet must leave at least 15% of stack)
_OVERBET_MIN_STACK_RATIO = 0.15

# ── Strong-tier overbet parameters ────────────────────────────────────────────
# Strong-tier overbet: full houses, top sets, strong flushes
_OVERBET_STRONG_MAX_WETNESS = 0.35
_OVERBET_STRONG_MAX_RISK = 0.04
_OVERBET_STRONG_FREQ_CAP = 0.45
_OVERBET_STRONG_BASE_RATIO = 1.25
_OVERBET_STRONG_MAX_RATIO = 1.55
_OVERBET_STRONG_MIN_POT = 1000


def _board_dry_score(public_cards):
    """Compute a 0.0-1.0 dry score for the board (higher = drier)."""
    if len(public_cards) < 3:
        return 0.5

    board_ranks = [card_number(c) for c in public_cards]
    board_suits = [card_suit(c) for c in public_cards]

    # Paired boards are less dry for overbetting
    paired = len(set(board_ranks)) < len(board_ranks)

    # Suit diversity: more suits = drier
    suit_counts = {}
    for s in board_suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    max_suit = max(suit_counts.values())
    suit_dry = 1.0 - (max_suit - 2) * 0.25

    # Straight connectivity: less connected = drier
    ranks_set = set(board_ranks)
    expanded = set(ranks_set)
    straight_dry = 1.0
    for start in range(1, 11):
        window = set(range(start, start + 5))
        present = len(expanded & window)
        if present >= 4:
            straight_dry = 0.0
            break
        elif present == 3:
            straight_dry = min(straight_dry, 0.5)

    # High card dryness: lower cards = drier (fewer overcards)
    high_card = max(board_ranks)
    high_dry = 1.0 - max(0, high_card - 10) * 0.08

    dry_score = suit_dry * 0.35 + straight_dry * 0.35 + high_dry * 0.30
    if paired:
        dry_score *= 0.70

    return clamp(dry_score, 0.0, 1.0)


def _overbet_frequency_roll(my_cards, public_cards, round_idx):
    """Deterministic frequency roll based on hand cards and board."""
    seed = (sum(my_cards) * 11 + sum(public_cards) * 17 + round_idx * 23) % 1000
    return seed / 1000.0


def overbet_risk_check(
    value_profile,
    board_texture,
    nutted_risk,
    paired_board_profile,
    opponent_model,
    pot,
    my_chips,
):
    """Check if the current spot is too risky for an overbet.

    Returns True if overbetting is SAFE, False if it should be blocked.
    """
    if value_profile is None or value_profile.get("tier") != _OVERBET_MIN_TIER:
        return False

    if board_texture is None:
        return False

    if board_texture["wetness"] > _OVERBET_MAX_WETNESS:
        return False

    if board_texture["dynamic"] and not _OVERBET_ALLOW_DYNAMIC:
        return False

    if nutted_risk is not None and nutted_risk.get("risk", 1.0) > _OVERBET_MAX_RISK:
        return False

    if paired_board_profile is not None and paired_board_profile.get("board_paired", False):
        # Paired boards increase risk of full houses/quads
        if paired_board_profile.get("fragile_two_pair", False):
            return False
        if paired_board_profile.get("weakened", False) and not paired_board_profile.get("strengthened", False):
            return False

    if pot < _OVERBET_MIN_POT:
        return False

    # Don't overbet if opponent is hyper-aggressive (they might be trapping)
    confidence = opponent_model.get("confidence", 0.0)
    if confidence >= _OVERBET_MIN_CONFIDENCE:
        if opponent_model.get("postflop_aggr", 0.36) > _OVERBET_MAX_POSTFLOP_AGGR:
            return False

    # Stack depth check: overbet must not commit too much of stack
    if my_chips > 0 and pot / my_chips > 1.0 - _OVERBET_MIN_STACK_RATIO:
        return False

    return True


def should_overbet(
    round_idx,
    to_call,
    value_profile,
    board_texture,
    nutted_risk,
    paired_board_profile,
    opponent_model,
    my_cards,
    public_cards,
    pot,
    my_chips,
):
    """Determine if we should make an overbet on this street.

    Returns a dict:
        {
            "eligible": bool,
            "ratio": float,      # suggested overbet ratio (1.3-1.8)
            "frequency": float,  # activation frequency (0.0-1.0)
            "reason": str,       # human-readable reason
        }
    """
    result = {
        "eligible": False,
        "ratio": 0.0,
        "frequency": 0.0,
        "reason": "none",
    }

    # Overbet only on river (round_idx == 3) when first to act or facing a check
    if round_idx != 3:
        return result

    if to_call != 0:
        return result

    if not overbet_risk_check(
        value_profile, board_texture, nutted_risk,
        paired_board_profile, opponent_model, pot, my_chips,
    ):
        # === Strong-tier overbet path ===
        score = evaluate_best(my_cards + public_cards)
        hand_class = score[0]
        tier = value_profile.get("tier", "none") if value_profile else "none"

        strong_eligible = (
            tier == "strong"
            and hand_class >= 3
            and board_texture is not None
            and board_texture["wetness"] <= _OVERBET_STRONG_MAX_WETNESS
            and not board_texture["dynamic"]
            and (nutted_risk is None or nutted_risk.get("risk", 1.0) <= _OVERBET_STRONG_MAX_RISK)
            and pot >= _OVERBET_STRONG_MIN_POT
            and my_chips > 0
            and pot / my_chips <= 1.0 - _OVERBET_MIN_STACK_RATIO
        )
        # Trips on paired board are vulnerable — reject
        if strong_eligible and hand_class == 3:
            if paired_board_profile is not None and paired_board_profile.get("board_paired", False):
                strong_eligible = False
        # Very aggressive opponents may trap
        if strong_eligible:
            confidence = opponent_model.get("confidence", 0.0)
            if confidence >= _OVERBET_MIN_CONFIDENCE:
                if opponent_model.get("postflop_aggr", 0.36) > _OVERBET_MAX_POSTFLOP_AGGR:
                    strong_eligible = False

        if strong_eligible:
            freq_roll = _overbet_frequency_roll(my_cards, public_cards, round_idx)
            if freq_roll <= _OVERBET_STRONG_FREQ_CAP:
                dry_score = _board_dry_score(public_cards)
                ratio = _OVERBET_STRONG_BASE_RATIO + dry_score * 0.15
                ratio = clamp(ratio, _OVERBET_STRONG_BASE_RATIO, _OVERBET_STRONG_MAX_RATIO)
                result["eligible"] = True
                result["ratio"] = ratio
                result["frequency"] = _OVERBET_STRONG_FREQ_CAP
                result["reason"] = "strong_tier_river_overbet"
                return result

        return result

    # Frequency management: not every eligible spot gets overbet
    freq_roll = _overbet_frequency_roll(my_cards, public_cards, round_idx)
    if freq_roll > _OVERBET_FREQ_CAP:
        result["reason"] = "frequency_cap"
        return result

    # Compute sizing ratio
    dry_score = _board_dry_score(public_cards)
    confidence = opponent_model.get("confidence", 0.0)
    ftr = opponent_model.get("fold_to_raise", 0.44)

    ratio = _OVERBET_BASE_RATIO

    # Strength scaling: nuttier = larger overbet
    # made_strength is not passed directly; infer from value_profile tier
    # nut hands get full strength bonus
    ratio += 0.15

    # Board dryness: drier = larger overbet (opponent has fewer draws to fear)
    ratio += dry_score * _OVERBET_DRY_ADJUST

    # Opponent model: high FTR opponents call too much, so overbet less
    if confidence >= _OVERBET_MIN_CONFIDENCE:
        ftr_dev = max(0, ftr - 0.44)
        ratio += ftr_dev * _OVERBET_FTR_ADJUST

    # Position adjustment: OOP overbet slightly smaller (less info)
    # This is applied by caller via match_sizing_delta, not here

    ratio = clamp(ratio, _OVERBET_BASE_RATIO, _OVERBET_MAX_RATIO)

    result["eligible"] = True
    result["ratio"] = ratio
    result["frequency"] = _OVERBET_FREQ_CAP
    result["reason"] = "nut_dry_river_overbet"
    return result


def overbet_sizing(
    ratio,
    to_call,
    pot,
    min_raise,
    my_chips,
    my_round_bet,
):
    """Compute the exact overbet raise-to-total amount.

    Args:
        ratio: overbet ratio (e.g. 1.5 for 1.5x pot)
        to_call: amount to call (should be 0 for pure overbet)
        pot: current pot
        min_raise: minimum legal raise amount
        my_chips: remaining chips
        my_round_bet: already committed this round

    Returns:
        int: raise-to-total amount, or None if invalid
    """
    pot_after_call = pot + to_call
    target = int(to_call + pot_after_call * ratio)
    amount = max(min_raise, target)

    # Cap at leaving at least _OVERBET_MIN_STACK_RATIO of stack
    max_commit = int(my_chips * (1.0 - _OVERBET_MIN_STACK_RATIO))
    amount = min(amount, max_commit)
    amount = min(amount, my_chips - 1)

    if amount <= to_call or amount < min_raise or amount >= my_chips:
        return None
    return amount
