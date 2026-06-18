"""Turn double-barrel planner (NEW offensive primitive for v117).

ORTHOGONAL to `passive_exploit.py:passive_exploit_trigger`:
- passive_exploit fires ONLY for passive stations (passivity_score >= 0.60)
  and only for value/semi-value made hands (made_strength >= 0.48 OR
  tier in thin/strong/nut).
- This module fires for NON-passive opponents (passivity < 0.60) on
  flop-cbet-called turn spots where v111/v116 check too often, exploiting
  turn texture transitions (blank / overcard) that strengthen our c-bet
  range and weaken the caller's.

EVIDENCE (from `bots/claude_v111/.task_context/w1.md`):
- v111 turn_raise = 33.5% vs v104 turn_raise = 38% — more aggression converts.
- v111 frequently checks turn after c-bet+flop-call on blank/overcard runouts.

BIRTH REQUIREMENTS (per experience_pool.md BLUFF_CALIBRATION):
- NEW detector (classify_turn_transition) + NEW opponent-line signal
  (turn texture transition bucket) + multiple reachable wiring sites
  (the planner is consulted once per turn decision; the sizing fn is
  called via the same path as passive_exploit_sizing — NO new sizing
  constant introduced).
- confidence >= 0.20 gate so we don't fire on noise.
- fold_to_bet_turn signal drives bluff-barrel branch.
"""
from card_utils import card_number, card_suit


def classify_turn_transition(flop_cards, turn_card):
    """Classify the flop->turn transition card into 5 buckets.

    Returns one of:
      'blank'          - low card not completing draws (GOOD barrel card)
      'overcard'       - rank > flop max (GOOD barrel card; widens our high-card range)
      'bricked_pair'   - turn pairs the board (NEUTRAL; mostly checks)
      'draw_completer' - 4-flush or 4-straight on board (BAD barrel; we are capped)
    """
    if not flop_cards or turn_card is None or len(flop_cards) < 3:
        return 'blank'
    flop_ranks = [card_number(c) for c in flop_cards]
    flop_suits = [card_suit(c) for c in flop_cards]
    flop_max = max(flop_ranks)
    flop_paired = len(set(flop_ranks)) < len(flop_ranks)

    turn_rank = card_number(turn_card)
    turn_suit = card_suit(turn_card)

    # 4-flush on board (flop 3 same suit + turn same suit) — draw completed/capped
    suit_counts = {}
    for s in flop_suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    suit_counts[turn_suit] = suit_counts.get(turn_suit, 0) + 1
    if max(suit_counts.values()) >= 4:
        return 'draw_completer'

    # 4-to-straight on board — also draw-completer territory
    all_ranks = sorted(set(flop_ranks + [turn_rank]))
    if len(all_ranks) >= 4:
        for i in range(len(all_ranks) - 3):
            window = all_ranks[i:i + 4]
            if window[3] - window[0] == 3 and len(set(window)) == 4:
                return 'draw_completer'

    # Turn pairs the board (and flop was unpaired) — neutral/slightly bad
    if turn_rank in flop_ranks and not flop_paired:
        return 'bricked_pair'

    # Turn is higher than any flop card — broadens high-card range
    if turn_rank > flop_max:
        return 'overcard'

    return 'blank'


def turn_double_barrel_planner(round_idx, to_call, my_id, opponent_id, spot_info,
                                opponent_model, value_profile, made_strength,
                                draw_strength, board_texture, history, public_cards):
    """Plan a turn second-barrel on flop-cbet-called lines vs NON-passive opp.

    Returns {"active": bool, "ratio": float, "reason": str} — ratio is a
    pot-fraction (same convention as passive_exploit_sizing, NO new sizing
    constant introduced downstream).
    """
    # Gate 1: turn round only, no facing bet (we're checked-to or it's our lead)
    if round_idx != 2 or to_call != 0:
        return {"active": False}

    # Gate 2: only fires for NON-passive opponents (passive_exploit covers >=0.60)
    passivity = opponent_model.get("passivity_score", 0.5)
    if passivity >= 0.60:
        return {"active": False}

    # Gate 3: need flop+turn (4 public cards minimum)
    if len(public_cards) < 4:
        return {"active": False}

    # Gate 4: confidence floor — don't fire on noise
    confidence = opponent_model.get("confidence", 0.0)
    if confidence < 0.20:
        return {"active": False}

    # Gate 5: we raised (c-bet) flop AND opponent called flop
    we_raised_flop = any(
        r.get("player_id") == my_id and r.get("round") == 1
        and r.get("action_type") in ("raise", "allin")
        for r in history
    )
    opp_called_flop = any(
        r.get("player_id") == opponent_id and r.get("round") == 1
        and r.get("action_type") == "call"
        for r in history
    )
    if not (we_raised_flop and opp_called_flop):
        return {"active": False}

    # Gate 6: opponent must not have shown turn aggression (we're checked-to
    # or it's our lead after a check). This is enforced by to_call==0 above
    # plus last_opp_action_type=='check' to ensure it's a real checked-to spot.
    if spot_info.get("last_opp_action_type") not in (None, "check"):
        return {"active": False}

    # Classify flop->turn transition
    transition = classify_turn_transition(public_cards[:3], public_cards[3])
    if transition == 'draw_completer':
        # Board now has 4-flush or 4-straight — we are capped, opponent's
        # calling range hits these textures harder. Skip.
        return {"active": False}

    tier = value_profile.get("tier", "none") if value_profile else "none"
    fold_to_bet_turn = opponent_model.get("fold_to_bet_turn", 0.40)
    flush_pressure = board_texture.get("flush_pressure", 0.0) if board_texture else 0.0
    straight_pressure = board_texture.get("straight_pressure", 0.0) if board_texture else 0.0
    good_card = transition in ("blank", "overcard")

    # VALUE BARREL: we have a real made hand — bet for value + fold equity.
    # Slightly wider than passive_exploit (which gates made_strength>=0.48)
    # because vs non-passive opp, thin/strong value still extracts calls
    # from flop floats that missed the turn.
    value_barrel = made_strength >= 0.42 or tier in ("thin", "strong", "nut")
    if value_barrel and good_card:
        return {"active": True, "ratio": 0.58,
                "reason": f"turn_value_barrel_{transition}"}
    if value_barrel and transition != 'bricked_pair':
        return {"active": True, "ratio": 0.50,
                "reason": "turn_value_barrel_neutral"}

    # BLUFF BARREL: weak made + no draw + good barrel card + fold equity.
    # Fold equity requires either (a) opponent folds turn >=0.42 with
    # confidence, or (b) opponent very unknown (low confidence means
    # they haven't shown they call down light).
    fold_equity_a = confidence >= 0.25 and fold_to_bet_turn >= 0.42
    fold_equity_b = confidence < 0.30  # unknown — assume default fold rate
    fold_equity = fold_equity_a or fold_equity_b
    if (good_card and made_strength < 0.42 and draw_strength < 0.18
            and flush_pressure < 0.75 and straight_pressure < 0.65
            and fold_equity):
        return {"active": True, "ratio": 0.55,
                "reason": f"turn_bluff_barrel_{transition}"}

    return {"active": False}


def barrel_planner_sizing(ratio, to_call, pot, min_raise, my_chips, my_round_bet):
    """Same sizing approach as passive_exploit_sizing — NO new sizing constant.

    `ratio` is a pot-fraction. We raise TO `to_call + pot_after_call * ratio`,
    clamped by `min_raise` (floor) and `my_chips - 1` (ceiling).
    """
    pot_after_call = pot + to_call
    target = int(to_call + pot_after_call * ratio)
    amount = max(min_raise, target)
    amount = min(amount, my_chips - 1)
    if amount <= to_call or amount < min_raise:
        return None
    return amount
