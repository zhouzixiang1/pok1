"""Fold-gate functions extracted from strategy.py (v93).

Hosts the structural postflop fold gates that intercept the historical
0% postflop fold rate:
  - _should_checkraise_trap: flop slow-play vs aggressive opponents.
  - _spr_commitment_gate: stack-preservation gate for large commitment bets.
  - _allin_board_texture_fold: all-in path fold gate on scary board textures,
    now grounded by pot-odds (equity-vs-price) comparison.
"""


def _should_checkraise_trap(value_profile, round_idx, board_texture, opponent_model, my_cards, public_cards):
    """Check with a strong hand on a dry flop to trap aggressive opponents.

    Returns True to activate: check flop -> call opponent bet -> raise turn.
    Only fires on the flop with strong/nut hands on dry boards vs aggressive
    opponents. ~40% activation frequency via hand-based seed.
    """
    if round_idx != 1:
        return False

    if value_profile is None or value_profile.get("tier") not in ("strong", "nut"):
        return False

    if board_texture is None:
        return False
    if board_texture.get("dynamic", False):
        return False
    if board_texture.get("wetness", 0.0) > 0.25:
        return False
    if board_texture.get("paired", False):
        return False

    confidence = opponent_model.get("confidence", 0.0)
    if confidence < 0.15:
        return False

    flop_aggr = opponent_model.get("flop_aggr", 0.36)
    postflop_aggr = opponent_model.get("postflop_aggr", 0.36)
    effective_aggr = max(flop_aggr, postflop_aggr)
    if effective_aggr < 0.35:
        return False

    seed = (sum(my_cards) * 7 + sum(public_cards) * 13) % 100
    if seed >= 40:
        return False

    return True


def _spr_commitment_gate(round_idx, to_call, pot, my_chips, made_strength,
                         draw_strength, value_profile, board_texture, nutted_risk,
                         opponent_model=None):
    """Structural fold gate: disengage marginal hands facing large commitment bets.

    Fires BEFORE must_continue_vs_raise inside the to_call>0 block to break the
    historical 0% postflop fold rate. must_continue_vs_raise() returns True for
    made_strength>=0.58 (overpairs / top-pair-good-kicker), which historically
    overrides every downstream fold path; this gate intercepts that override by
    folding on NEW axes the existing gates ignore:
      - commit_ratio = to_call / my_chips  (bet-to-stack polarization)
      - pot_ratio    = to_call / pot        (pot-sized bet pressure)
      - spr          = my_chips / pot       (stack-to-pot commitment)
      - board scariness (flush/straight pressure + paired board on turn/river)

    Returns -1 to fold, or None to proceed to the normal decision logic.
    """
    # Only fires postflop when actually facing a bet and chips are at stake.
    if round_idx <= 0 or to_call <= 0 or my_chips <= 1:
        return None

    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    # Never fold genuinely nutted hands — sets+, full houses, nut flushes.
    if tier == "nut":
        return None
    # Never fold strong combo draws — they have their own equity to continue.
    if draw_strength >= 0.25:
        return None

    # nutted_risk: how likely the opponent holds a monster (set+). Higher risk
    # means a big bet is more polarized toward value, so we fold more readily.
    risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0

    commit_ratio = to_call / my_chips if my_chips > 0 else 1.0
    pot_ratio = to_call / pot if pot > 0 else 0.0
    spr = my_chips / pot if pot > 0 else 999.0
    has_draw = draw_strength >= 0.18

    # Board scariness: completed/likely flush & straight textures, or a paired
    # board on turn/river that could give the opponent a full house/trips.
    scary = False
    if board_texture is not None:
        if board_texture.get("flush_pressure", 0) >= 0.75 or board_texture.get("straight_pressure", 0) >= 0.75:
            scary = True
        if board_texture.get("paired", False) and round_idx >= 2:
            scary = True

    # GATE 1: Near-all-in commitment (>=50% of remaining stack) on turn/river.
    # Folding second-best hands (one-pair / weak two-pair / overpair on scary
    # boards) that are being polarized against by a big bet. When nutted_risk is
    # elevated (opponent likely holds a monster), fold slightly stronger hands.
    if round_idx >= 2 and commit_ratio >= 0.50:
        strength_cap = 0.55 if risk >= 0.08 else 0.60
        if made_strength < strength_cap and not has_draw:
            return -1
        if made_strength < 0.70 and scary and not has_draw:
            return -1

    # GATE 2: Large river pot bet (>=75% pot) with marginal made hand.
    if round_idx == 3 and pot_ratio >= 0.75:
        strength_cap = 0.45 if risk >= 0.08 else 0.50
        if made_strength < strength_cap and not has_draw:
            return -1
        if made_strength < 0.62 and scary and not has_draw:
            return -1

    # GATE 3: Low SPR (<3) with a sizable bet on turn/river — at this depth a
    # big bet strongly polarizes the opponent's range to value/bluff; marginal
    # made hands without a draw lack the equity to continue.
    if spr < 3.0 and round_idx >= 2 and commit_ratio >= 0.35:
        if made_strength < 0.55 and not has_draw and tier not in ("strong", "nut"):
            return -1

    # GATE 4: Opponent-stat-grounded intermediate fold.
    # Fires in the 0.30-0.49 commit_ratio gap — uncovered by GATE 1's 0.50
    # threshold — ONLY when opponent profiling confirms value-heavy tendencies.
    # This preserves call-downs vs passive opponents (the bot still calls
    # stations in this range) while folding marginal made hands vs confirmed
    # value-heavy aggressors, directly attacking the persistent 0% postflop
    # fold rate in the intermediate-commitment band.
    if opponent_model is not None and round_idx >= 2 and 0.30 <= commit_ratio < 0.50:
        confidence = opponent_model.get("confidence", 0.0)
        if confidence >= 0.20:
            post_aggr = opponent_model.get("postflop_aggr", 0.36)
            barrel_freq = opponent_model.get("barrel_freq", 0.45)
            value_heavy = post_aggr >= 0.42 or barrel_freq >= 0.50
            if value_heavy:
                strength_cap = 0.48 if risk >= 0.06 else 0.52
                if (made_strength < strength_cap and not has_draw
                        and tier not in ("strong", "nut")):
                    return -1

    return None


def _allin_board_texture_fold(round_idx, made_strength, draw_strength,
                              value_profile, board_texture, nutted_risk,
                              to_call, pot):
    """Fold non-nut hands facing all-in on scary board textures.

    An all-in on turn/river polarizes the opponent's range to nuts or bluff.
    Non-nut made hands (sets on paired boards, non-nut flushes, straights on
    flush boards, overpairs on draw-completion boards) are dominated by the
    value portion of the shove range. This gate targets the all-in commitment
    path (opponent_allin / to_call>=my_chips) which _spr_commitment_gate does
    not cover.

    Pot-odds grounding (v93): folds only fire when the price (pot_odds =
    to_call / (pot + to_call)) exceeds 90% of our equity. This prevents
    over-folding to cheap all-in bluffs while still folding expensive value
    shoves where our equity cannot justify the call.

    Returns -1 to fold, or None to continue normal logic.
    """
    # Only fires on turn/river — flop is too early for the shove range to polarize.
    if round_idx < 2:
        return None
    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    # Never fold nutted hands.
    if tier == "nut":
        return None
    # Strong combo draws have their own equity to continue.
    if draw_strength >= 0.22:
        return None
    # Near-nut made hands still call off.
    if made_strength >= 0.80:
        return None

    if board_texture is None:
        return None

    # Pot-odds grounding: only fold when the price exceeds 90% of our equity.
    # This prevents over-folding to cheap all-in bluffs while still folding
    # expensive value shoves where our equity can't justify the call.
    pot_odds = to_call / (pot + to_call) if (pot + to_call) > 0 else 0.0
    # If pot odds are very cheap (< 20% of pot), we're getting good enough
    # price to call even with marginal hands — skip the texture fold.
    if pot_odds < 0.20:
        return None

    scary_flush = board_texture.get("flush_pressure", 0) >= 1.0
    scary_straight = board_texture.get("straight_pressure", 0) >= 1.0
    paired = board_texture.get("paired", False)

    # Completed flush on board + we don't have a strong/nut holding.
    if scary_flush and made_strength < 0.72 and tier != "strong" and pot_odds >= made_strength * 0.9:
        return -1
    # Completed straight on board + we don't have a strong/nut holding.
    if scary_straight and made_strength < 0.68 and tier != "strong" and pot_odds >= made_strength * 0.9:
        return -1
    # Paired board on turn/river: trips / full-house risk dominates marginal hands.
    if paired and round_idx >= 2 and made_strength < 0.65 and pot_odds >= made_strength * 0.9:
        return -1

    # Elevated nutted_risk on any texture — opponent likely holds a monster.
    risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0
    if risk >= 0.06 and made_strength < 0.70 and pot_odds >= made_strength * 0.9:
        return -1

    return None
