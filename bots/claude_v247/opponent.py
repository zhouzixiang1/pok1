import sys
from constants import BIG_BLIND, N_PLAYERS, SMALL_BLIND
from card_utils import clamp, next_player
from state import collect_latest_requests_by_hand
from tournament import opponent_can_lock_win
from strategy_helpers import (
    postflop_call_margin, bluff_heavy_call_widen,
    check_raise_pressure, barrel_pressure_profile,
    _river_reraise_tighten, _multi_street_calldown_tax,
    _opponent_sizing_call_tighten, _weak_one_pair_river_margin,
)
from postflop import pair_domination_margin, draw_call_margin, bet_size_bucket


def smooth_rate(successes, total, prior_mean, prior_weight):
    return (successes + prior_mean * prior_weight) / (total + prior_weight)


def _first_bb_response_to_my_sb_open(req, my_id, opponent_id):
    """Return BB's immediate response to our SB open raise/all-in, or None.

    The preflop open-defense counters must describe only the spot where we are
    SB, our raise/all-in is the first voluntary/aggressive preflop decision, and
    villain is the BB responding immediately. This avoids mixing in BB 3-bets
    after villain opened or limp-reraise/all-in sequences after we limped.
    """
    dealer_id = req.get("dealer_id")
    if dealer_id is None or my_id != next_player(dealer_id, 1):
        return None

    awaiting_bb_response = False
    saw_preflop_decision = False
    for rec in req.get("history", []):
        if rec.get("round") != 0:
            continue

        pid = rec.get("player_id")
        action_type = rec.get("action_type")

        if awaiting_bb_response:
            if pid == opponent_id:
                return action_type
            return None

        if pid == my_id and action_type in ("raise", "allin") and not saw_preflop_decision:
            awaiting_bb_response = True
            saw_preflop_decision = True
            continue

        # Any preflop decision before our open means this is not an SB open spot.
        saw_preflop_decision = True
        return None

    return None


def build_opponent_model(requests, my_id):
    opponent_id = next_player(my_id, 1)
    hand_requests = collect_latest_requests_by_hand(requests)

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
    preflop_open_opp = 0
    preflop_open_fold = 0
    preflop_open_3bet = 0
    ftr_flop_opp = 0
    ftr_flop_fold = 0
    ftr_turn_opp = 0
    ftr_turn_fold = 0
    ftr_river_opp = 0
    ftr_river_fold = 0
    call_down_flop_turn = 0
    call_down_flop_turn_call = 0
    call_down_turn_river = 0
    call_down_turn_river_call = 0
    raise_sizes = []
    flop_bets = 0
    turn_bets = 0
    river_bets = 0
    flop_acts = 0
    turn_acts = 0
    river_acts = 0
    flop_raise_bb = []
    turn_raise_bb = []
    river_raise_bb = []
    barrel_hands = 0
    barrel_continue = 0
    # v155 NEW: turn-to-river barrel continuation counters. Track how often
    # opponent gives up range advantage by checking back after barreling prior
    # streets. Exploitable because their range is capped when they abandon.
    barrel_hands_turn = 0
    barrel_turn_continue = 0
    opp_bet_flop = False
    opp_bet_turn = False
    opp_small_bet_count = 0
    opp_large_bet_count = 0

    # NEW: per-street opponent bet-sizing profiler samples.
    # Each entry is (round_idx, bet_to_pot_ratio) recorded at the moment the
    # opponent makes a postflop raise, using a reconstructed pot estimate.
    per_street_sizing_samples = []

    # v193 NEW: per-street bet-size-MAGNITUDE polarity samples.
    # Parallel to per_street_sizing_samples but tracks a 3-tuple
    # (round_idx, bet_to_pot_ratio, is_allin) with a LOWER activation threshold
    # (n>=4) so polarity profiling stays LIVE even in short 70-hand HU matches
    # where classify_sizing_tendency (n>=6, often 'unknown') goes INERT.
    _betsize_magnitude_samples = []

    # v151 NEW: per-street call-down frequency tracking.
    # _pending_my_bet_ratio captures OUR bet-to-pot ratio when we raise
    # postflop. _calldown_samples accumulates (street, bet_pot_ratio, did_call)
    # tuples across hands so we can detect opponents who call flop bets but
    # fold turn/river bets (street-declining call-down pattern).
    _pending_my_bet_ratio = 0.5
    _calldown_samples = []

    for req in hand_requests:
        if opponent_can_lock_win(req, my_id):
            continue

        opp_bet_flop = False
        opp_bet_turn = False

        # NEW: Per-hand pot reconstruction state. Tracks each player's
        # contribution to the current round so we can estimate the pot at any
        # point. Accounts for blinds baseline (SB+BB) and call equalization,
        # avoiding the over-inflation bias of a max-raise-only tracker.
        sb_player = req.get("dealer_id")
        if sb_player is None:
            sb_player = my_id  # safe fallback; only used for blind attribution
        bb_player = next_player(sb_player, 1)
        round_bets = {my_id: 0, opponent_id: 0}
        # Blinds are posted before history begins (judge clears them), so the
        # preflop round_bets start at the blind levels. prior_rounds_pot tracks
        # ONLY chips from COMPLETED prior rounds (starts at 0 for preflop) —
        # blinds are accounted for via round_bets, NOT prior_rounds_pot, to
        # avoid double-counting the SB+BB baseline (which would inflate the pot
        # by 150 chips on every street and bias bet-to-pot ratios downward).
        round_bets[sb_player] = SMALL_BLIND
        round_bets[bb_player] = BIG_BLIND
        prior_rounds_pot = 0
        last_round_seen = 0

        history = req.get("history", [])
        if not history:
            continue

        saw_opponent_preflop_action = False
        pending_my_pressure = False
        pending_round = None

        open_response = _first_bb_response_to_my_sb_open(req, my_id, opponent_id)
        if open_response is not None:
            preflop_open_opp += 1
            if open_response == "fold":
                preflop_open_fold += 1
            elif open_response in ("raise", "allin"):
                preflop_open_3bet += 1

        for record in history:
            pid = record["player_id"]
            action_type = record["action_type"]
            action = record["action"]
            round_idx = record["round"]

            # NEW: Round transition detection. When the round advances, commit
            # both players' contributions from the prior round to the pot total
            # and reset the current-round contribution tracker.
            if round_idx > last_round_seen:
                prior_rounds_pot += round_bets[my_id] + round_bets[opponent_id]
                round_bets[my_id] = 0
                round_bets[opponent_id] = 0
                last_round_seen = round_idx

            # NEW: Update per-player round contribution for ALL players BEFORE
            # any early-exit continue. This accounts for calls (equalizing to
            # the current high bet) AND raises (raise-to-total), giving an
            # accurate pot estimate that fixes the inflation bias of a
            # max-raise-only tracker. "allin" action=-2 hides the exact amount,
            # so we approximate by matching the current high bet (lower bound).
            if action_type == "raise" and action > 0:
                # "raise" action field = raise-to-total for this round.
                # Sample opponent's postflop sizing BEFORE updating tracker.
                if pid == opponent_id and round_idx > 0:
                    pot_estimate = prior_rounds_pot + round_bets[my_id] + round_bets[opponent_id]
                    if pot_estimate > 0:
                        per_street_sizing_samples.append((round_idx, action / pot_estimate))
                        # v193: piggyback betsize-polarity sample on the SAME
                        # recording site. action_type is "raise" here, so
                        # is_allin is False; the field keeps schema parity so
                        # _opp_betsize_polarity can classify shoves if they are
                        # ever recorded upstream.
                        _betsize_magnitude_samples.append(
                            (round_idx, action / pot_estimate, action_type == "allin"))
                # v151 NEW: capture OUR bet-to-pot ratio before updating tracker.
                # pot_estimate uses pre-update round_bets, giving pot-before-our-bet.
                if pid == my_id and round_idx > 0:
                    pot_estimate = prior_rounds_pot + round_bets[my_id] + round_bets[opponent_id]
                    _pending_my_bet_ratio = action / pot_estimate if pot_estimate > 0 else 0.5
                round_bets[pid] = max(round_bets[pid], action)
            elif action_type == "call":
                # Caller matches the current high bet (equalization).
                high_bet = max(round_bets.values())
                round_bets[pid] = max(round_bets[pid], high_bet)
            elif action_type == "allin":
                # action=-2 hides exact amount; approximate as matching the
                # current high bet so pot estimate stays a lower bound (avoids
                # over-inflation flagged in reviewer SECONDARY risk).
                high_bet = max(round_bets.values())
                round_bets[pid] = max(round_bets[pid], high_bet)
                # v194 FIX: record betsize-polarity sample for all-ins. action=-2
                # hides the exact amount; approximate ratio via high_bet/pot_estimate
                # (lower bound, capped at 1.0 since all-ins cover >= pot). is_allin=True
                # unblocks _opp_betsize_polarity shove_rate (was permanently 0 because
                # the raise branch above evaluated action_type=='allin' to False).
                if pid == opponent_id and round_idx > 0:
                    pot_estimate = prior_rounds_pot + round_bets[my_id] + round_bets[opponent_id]
                    if pot_estimate > 0:
                        _allin_ratio = max(1.0, high_bet / pot_estimate)
                        _betsize_magnitude_samples.append((round_idx, _allin_ratio, True))

            if pid == my_id and action_type in ("raise", "allin"):
                pending_my_pressure = True
                pending_round = round_idx
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
                if action_type == 'raise':
                    sizing_bb = action / BIG_BLIND
                    if sizing_bb >= 8.0:
                        opp_large_bet_count += 1
                    else:
                        opp_small_bet_count += 1
                # allin: action=-2 (Holdem.ALLIN), 无法获取实际筹码量，跳过 sizing 分类
                postflop_actions += 1
                if action_type in ("raise", "allin"):
                    postflop_aggressive += 1
                if action_type == "check":
                    postflop_checks += 1
                if round_idx == 1:
                    flop_acts += 1
                    if action_type in ('raise', 'allin'):
                        flop_bets += 1
                        opp_bet_flop = True
                    if action_type == 'raise':
                        flop_raise_bb.append(action / BIG_BLIND)
                elif round_idx == 2:
                    turn_acts += 1
                    if action_type in ('raise', 'allin'):
                        turn_bets += 1
                        opp_bet_turn = True
                    if action_type == 'raise':
                        turn_raise_bb.append(action / BIG_BLIND)
                elif round_idx == 3:
                    river_acts += 1
                    if action_type in ('raise', 'allin'):
                        river_bets += 1
                    if action_type == 'raise':
                        river_raise_bb.append(action / BIG_BLIND)

            if action_type == "raise":
                raise_sizes.append(action / BIG_BLIND)

            if pending_my_pressure:
                fold_to_raise_opportunities += 1
                if action_type == "fold":
                    fold_to_raise += 1
                if pending_round == 1:
                    ftr_flop_opp += 1
                    if action_type == "fold":
                        ftr_flop_fold += 1
                elif pending_round == 2:
                    ftr_turn_opp += 1
                    if action_type == "fold":
                        ftr_turn_fold += 1
                elif pending_round == 3:
                    ftr_river_opp += 1
                    if action_type == "fold":
                        ftr_river_fold += 1
                # v151 NEW: record call-down sample for per-street profile.
                # Did the opponent call our postflop bet, or fold?
                if pending_round is not None and pending_round > 0:
                    did_call = action_type in ('call', 'allin')
                    _calldown_samples.append((pending_round, _pending_my_bet_ratio, 1 if did_call else 0))
                pending_my_pressure = False
                pending_round = None

        if opp_bet_flop:
            barrel_hands += 1
            if opp_bet_turn:
                barrel_continue += 1
        # v155 NEW: turn-to-river barrel continuation. Tracks whether opponent
        # barrels river after betting turn. Opponents who abandon on the river
        # after barreling the turn are capped and exploitable.
        if opp_bet_turn:
            barrel_hands_turn += 1
            opp_bet_river = any(
                r["player_id"] == opponent_id and r["round"] == 3
                and r["action_type"] in ("raise", "allin")
                for r in history
            )
            if opp_bet_river:
                barrel_turn_continue += 1

        we_bet_flop = any(r["player_id"] == my_id and r["round"] == 1 and r["action_type"] in ("raise", "allin") for r in history)
        opp_called_flop = we_bet_flop and any(r["player_id"] == opponent_id and r["round"] == 1 and r["action_type"] == "call" for r in history)
        we_bet_turn = opp_called_flop and any(r["player_id"] == my_id and r["round"] == 2 and r["action_type"] in ("raise", "allin") for r in history)
        if we_bet_turn:
            call_down_flop_turn += 1
            if any(r["player_id"] == opponent_id and r["round"] == 2 and r["action_type"] == "call" for r in history):
                call_down_flop_turn_call += 1
        opp_called_turn = we_bet_turn and any(r["player_id"] == opponent_id and r["round"] == 2 and r["action_type"] == "call" for r in history)
        we_bet_river = opp_called_turn and any(r["player_id"] == my_id and r["round"] == 3 and r["action_type"] in ("raise", "allin") for r in history)
        if we_bet_river:
            call_down_turn_river += 1
            if any(r["player_id"] == opponent_id and r["round"] == 3 and r["action_type"] == "call" for r in history):
                call_down_turn_river_call += 1

    confidence = clamp((total_actions - 5) / 35.0, 0.0, 1.0)
    open_response_confidence = clamp((preflop_open_opp - 2) / 8.0, 0.0, 1.0)
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else 2.6

    ftr_flop = smooth_rate(ftr_flop_fold, ftr_flop_opp, 0.44, 2.0)
    ftr_turn = smooth_rate(ftr_turn_fold, ftr_turn_opp, 0.44, 2.0)
    ftr_river = smooth_rate(ftr_river_fold, ftr_river_opp, 0.44, 2.0)
    call_down_flop_turn_rate = smooth_rate(call_down_flop_turn_call, call_down_flop_turn, 0.35, 2.0)
    call_down_turn_river_rate = smooth_rate(call_down_turn_river_call, call_down_turn_river, 0.35, 2.0)
    passivity_score = clamp(
        ((1.0 - ftr_flop) + (1.0 - ftr_turn) + (1.0 - ftr_river) + call_down_flop_turn_rate + call_down_turn_river_rate) / 5.0,
        0.0, 1.0,
    )

    # NEW signal: turn+river calling-station stickiness. Distinct from
    # passivity_score (which weights flop + check rates). Pure non-fold behavior
    # on streets where value sizing matters most for value extraction.
    turn_sticky = 1.0 - ftr_turn
    river_sticky = 1.0 - ftr_river
    value_maximizer_index = clamp(
        call_down_flop_turn_rate * 0.25
        + call_down_turn_river_rate * 0.35
        + turn_sticky * 0.20
        + river_sticky * 0.20,
        0.0, 1.0,
    )

    # v151 NEW: per-street call-down frequency profile.
    # Tracks how often the opponent calls our postflop bets at each street.
    # Detects "sticky early, foldy late" pattern exploitable by delayed bluffs.
    calldown_profile = {}
    for street in (1, 2, 3):
        street_s = [(ratio, called) for ridx, ratio, called in _calldown_samples if ridx == street]
        n = len(street_s)
        if n >= 4:
            small = [c for ratio, c in street_s if ratio < 0.40]
            large = [c for ratio, c in street_s if ratio >= 0.60]
            calldown_profile[street] = {
                'rate': sum(c for _, c in street_s) / n,
                'samples': n,
                'small_bet_rate': sum(small) / len(small) if small else None,
                'large_bet_rate': sum(large) / len(large) if large else None,
            }
        else:
            calldown_profile[street] = {'rate': 0.5, 'samples': n, 'small_bet_rate': None, 'large_bet_rate': None}

    # v159 NEW signal: average pot-fraction of RIVER bets opponent has CALLED.
    # Tracks WHAT SIZES opponent tolerates on the river (distinct from
    # value_maximizer_index which tracks WHETHER they call). Used to gate
    # river value sizing: only size up vs confirmed calling stations.
    _river_called_ratios = [ratio for ridx, ratio, called in _calldown_samples
                            if ridx == 3 and called]
    if len(_river_called_ratios) >= 2:
        river_call_size_ratio = sum(_river_called_ratios) / len(_river_called_ratios)
    else:
        river_call_size_ratio = 0.50  # default: no strong-band license yet

    # v179 NEW: per-street call-size ratios (parallel to river_call_size_ratio).
    # Tracks WHAT bet sizes opponent tolerates calling on flop/turn, enabling
    # per-street value-sizing exploitation. Default 0.50 (no license yet).
    _turn_called_ratios = [ratio for ridx, ratio, called in _calldown_samples
                           if ridx == 2 and called]
    if len(_turn_called_ratios) >= 2:
        turn_call_size_ratio = sum(_turn_called_ratios) / len(_turn_called_ratios)
    else:
        turn_call_size_ratio = 0.50

    _flop_called_ratios = [ratio for ridx, ratio, called in _calldown_samples
                           if ridx == 1 and called]
    if len(_flop_called_ratios) >= 2:
        flop_call_size_ratio = sum(_flop_called_ratios) / len(_flop_called_ratios)
    else:
        flop_call_size_ratio = 0.50

    # v166 NEW: Continuous large-bet proportion.
    _large_n = len(per_street_sizing_samples)
    _large_hits = sum(1 for _, r in per_street_sizing_samples if r >= 0.70)
    large_bet_ratio = _large_hits / _large_n if _large_n > 0 else 0.32

    return {
        "confidence": confidence,
        "vpip": smooth_rate(voluntary_preflop, preflop_opportunities, 0.58, 4.0),
        "pfr": smooth_rate(preflop_raise, preflop_opportunities, 0.28, 4.0),
        "allin_rate": smooth_rate(allin_actions, total_actions, 0.05, 8.0),
        "postflop_aggr": smooth_rate(postflop_aggressive, postflop_actions, 0.36, 5.0),
        "postflop_check_rate": smooth_rate(postflop_checks, postflop_actions, 0.42, 5.0),
        "fold_to_raise": smooth_rate(fold_to_raise, fold_to_raise_opportunities, 0.44, 2.0),
        "fold_to_open_preflop": smooth_rate(preflop_open_fold, preflop_open_opp, 0.42, 2.0),
        "threebet_vs_open": smooth_rate(preflop_open_3bet, preflop_open_opp, 0.16, 2.0),
        "open_response_samples": preflop_open_opp,
        "open_response_confidence": open_response_confidence,
        "aggression": smooth_rate(aggressive_actions, total_actions, 0.30, 6.0),
        "avg_raise_bb": avg_raise_bb,
        "flop_aggr": smooth_rate(flop_bets, flop_acts, 0.36, 5.0),
        "turn_aggr": smooth_rate(turn_bets, turn_acts, 0.32, 5.0),
        "river_aggr": smooth_rate(river_bets, river_acts, 0.28, 5.0),
        "avg_flop_raise_bb": sum(flop_raise_bb)/len(flop_raise_bb) if flop_raise_bb else 3.0,
        "avg_turn_raise_bb": sum(turn_raise_bb)/len(turn_raise_bb) if turn_raise_bb else 4.5,
        "avg_river_raise_bb": sum(river_raise_bb)/len(river_raise_bb) if river_raise_bb else 5.5,
        "barrel_freq": smooth_rate(barrel_continue, barrel_hands, 0.45, 2.0),
        # v155 NEW: per-street barrel abandonment signals.
        # turn_to_river_barrel: how often opp continues to barrel river after turn bet.
        # barrel_abandon_turn: 1 - flop-to-turn barrel freq (prior_mean=0.45 → default 0.55).
        # barrel_abandon_river: 1 - turn-to-river barrel freq (prior_mean=0.35 → default 0.65).
        "turn_to_river_barrel": smooth_rate(barrel_turn_continue, barrel_hands_turn, 0.35, 2.0),
        "barrel_abandon_turn": 1.0 - smooth_rate(barrel_continue, barrel_hands, 0.45, 2.0),
        "barrel_abandon_river": 1.0 - smooth_rate(barrel_turn_continue, barrel_hands_turn, 0.35, 2.0),
        "sizing_aggr": smooth_rate(opp_large_bet_count, opp_small_bet_count + opp_large_bet_count, 0.35, 2.0),
        "fold_to_bet_flop": ftr_flop,
        "fold_to_bet_turn": ftr_turn,
        "fold_to_bet_river": ftr_river,
        "call_down_flop_turn": call_down_flop_turn_rate,
        "call_down_turn_river": call_down_turn_river_rate,
        "passivity_score": passivity_score,
        "value_maximizer_index": value_maximizer_index,
        "sizing_tendency": classify_sizing_tendency(per_street_sizing_samples),
        "betsize_polarity": _opp_betsize_polarity(_betsize_magnitude_samples),
        "calldown_profile": calldown_profile,
        "river_call_size_ratio": river_call_size_ratio,
        "turn_call_size_ratio": turn_call_size_ratio,
        "flop_call_size_ratio": flop_call_size_ratio,
        "large_bet_ratio": large_bet_ratio,
    }


def classify_archetype(om):
    """v183 NEW: Classify opponent into a strategic archetype bucket.

    Returns (archetype_str, confidence_float). Buckets:
      - 'calling_station': high VPIP, low aggression, low fold-to-raise, high VMI
      - 'rock': tight (low VPIP), high fold-to-raise, low aggression
      - 'aggro': high aggression + high PFR + low fold-to-raise + polarized sizing
      - 'standard': none of the above dominate (default)

    Uses ONLY existing opponent_model fields. Scores are continuous composites
    (no hard thresholds) so the classification degrades gracefully with thin data.
    """
    conf = om.get('confidence', 0.0)
    vpip = om.get('vpip', 0.58)
    pfr = om.get('pfr', 0.28)
    postflop_aggr = om.get('postflop_aggr', 0.36)
    fold_to_raise = om.get('fold_to_raise', 0.44)
    river_aggr = om.get('river_aggr', 0.28)
    allin_rate = om.get('allin_rate', 0.05)
    large_bet_ratio = om.get('large_bet_ratio', 0.32)
    vmi = om.get('value_maximizer_index', 0.40)

    # Aggro: raises a lot postflop + preflop, doesn't fold, polarized sizing.
    aggro_score = (
        max(0.0, postflop_aggr - 0.36) / 0.20 * 0.30 +
        max(0.0, pfr - 0.28) / 0.15 * 0.20 +
        max(0.0, 0.44 - fold_to_raise) / 0.15 * 0.15 +
        max(0.0, river_aggr - 0.28) / 0.15 * 0.15 +
        max(0.0, allin_rate - 0.05) / 0.08 * 0.10 +
        max(0.0, large_bet_ratio - 0.32) / 0.20 * 0.10
    )
    # Calling station: calls everything, rarely bets or folds.
    station_score = (
        max(0.0, vpip - 0.55) / 0.15 * 0.30 +
        max(0.0, 0.36 - postflop_aggr) / 0.15 * 0.25 +
        max(0.0, 0.44 - fold_to_raise) / 0.15 * 0.25 +
        max(0.0, vmi - 0.40) / 0.20 * 0.20
    )
    # Rock: tight, folds to pressure.
    rock_score = (
        max(0.0, fold_to_raise - 0.50) / 0.15 * 0.40 +
        max(0.0, 0.50 - vpip) / 0.15 * 0.30 +
        max(0.0, 0.30 - postflop_aggr) / 0.10 * 0.30
    )
    best = max(aggro_score, station_score, rock_score)
    if best < 0.25:
        return 'standard', conf
    if aggro_score == best:
        return 'aggro', conf
    if station_score == best:
        return 'calling_station', conf
    return 'rock', conf


def _opp_bluff_prone(om):
    """v184: detect opponents whose bets include many bluffs (not value-polarized).

    Suppresses bluff-catcher folds vs these opponents — calling wider is +EV.
    Signals (all from the existing opponent model; guard with .get defaults):
      - LOW large_bet_ratio (<0.50): bets are NOT big/polarized -> includes small bluffs/probes.
      - HIGH postflop_aggr (>=0.40): barrels often -> bluffs in their betting range.
      - LOW fold_to_bet (<0.35): loose/calling -> bets are not reliable value.
    Fires (bluff-prone) if EITHER the (low-lbr + high-aggr) barreling profile OR
    the loose-caller profile holds. Does NOT fire for value-polarized bettors
    (high large_bet_ratio, big bets only with the goods) -> fold still applies.

    NOTE on fold_to_bet lookup: the model exposes per-street fold_to_bet_turn /
    fold_to_bet_river (NOT a top-level fold_to_bet). We deliberately do NOT fall
    back to fold_to_raise — aggro opponents naturally have low fold_to_raise
    (they don't fold to raises), which would false-positive EVERY aggro as
    bluff-prone and kill the aggro fold gate. Prefer per-street fold_to_bet;
    skip the loose_caller check if no fold_to_bet signal is available.
    """
    lbr = om.get('large_bet_ratio', 0.5)
    aggr = om.get('postflop_aggr', 0.35)
    barreling_bluffy = (lbr < 0.50) and (aggr >= 0.40)
    # loose_caller uses fold_to_bet (vs BETS). Prefer top-level fold_to_bet if
    # present (test mocks), then per-street turn/river; skip if absent.
    ftb = om.get('fold_to_bet')
    if ftb is None:
        per_street = [v for v in (
            om.get('fold_to_bet_turn'), om.get('fold_to_bet_river')
        ) if v is not None]
        ftb = min(per_street) if per_street else None
    loose_caller = ftb is not None and ftb < 0.35
    return barreling_bluffy or loose_caller


def _aggro_bluffcatcher_should_fold(om, round_idx, to_call, pot,
                                     made_strength, value_profile, draw_strength):
    """v184: Bluff-catcher fold discipline vs confirmed aggro archetype.

    Theory: vs a value-heavy aggressor, a bluff-catcher (weak one-pair, made ~0.22)
    only beats bluffs. Aggro opponents under-deliver bluffs -> the call is -EV.
    Folds when pot-odds demand >7% more equity than the hand has.

    v184 WIDENED (vs v183): made band 0.45->0.50, conf 0.15->0.12, ev_margin
    0.10->0.07. This catches more -EV calls vs aggro opponents.

    v184 BLUFF-AWARE GUARD: suppresses the fold vs bluff-prone opponents (low
    fold-to-bet or very aggressive + never folds). Vs a bluff-heavy aggro,
    our bluff-catcher has positive equity, so folding converts would-be wins
    into losses. The guard is checked BEFORE the archetype gate so it protects
    even when the aggro classification is firm.

    Guards: fires ONLY on turn/river (round_idx>=2) + NOT bluff-prone
    + aggro archetype (conf>=0.12) + bluff-catcher band (made 0.20-0.50)
    + no draw + tier not nut/strong + bet is significant (pot_odds >= 0.33).
    """
    if round_idx < 2 or to_call <= 0:
        return False
    # v184 bluff-aware guard: suppress fold vs bluff-prone opponents.
    if _opp_bluff_prone(om):
        return False
    archetype, conf = classify_archetype(om)
    if archetype != 'aggro' or conf < 0.12:
        return False
    if value_profile is not None and value_profile.get('tier') in ('nut', 'strong'):
        return False
    if draw_strength >= 0.15:
        return False
    if made_strength < 0.20 or made_strength >= 0.50:
        return False
    pot_odds = to_call / max(1, pot + to_call)
    if pot_odds < 0.33:
        return False
    # EV gate: fold when pot-odds exceed made-strength by >0.07 (clearly -EV call).
    ev_margin = pot_odds - made_strength
    if ev_margin <= 0.07:
        return False
    return True


def _rock_value_bet_fold(om, round_idx, to_call, pot,
                          made_strength, value_profile, draw_strength):
    """v184 NEW: Thin-value-call suppression vs confirmed rock archetype.

    Theory: rocks (tight, high fold-to-raise, low aggression) only bet/raise
    with strong hands. A marginal made hand (0.20-0.50) facing a rock's
    aggression is dominated — the rock's range is value-heavy. Folding
    discipline is +EV because calling only beats bluffs the rock doesn't make.

    This is the COMPLEMENT of _aggro_bluffcatcher_should_fold on a separate
    archetype axis: aggro opponents under-deliver bluffs by polarization;
    rocks under-deliver bluffs by tightness. Both produce value-heavy ranges
    that crush our bluff-catchers.

    v184 BLUFF-AWARE GUARD: suppresses the fold vs bluff-prone opponents.
    A "rock" that shows low fold-to-bet or high postflop aggression is
    misclassified or transitioning; we don't trust the read enough to fold.

    Guards: fires ONLY on turn/river (round_idx>=2) + NOT bluff-prone
    + rock archetype (conf>=0.12) + marginal band (made 0.20-0.50)
    + no draw + tier not nut/strong + bet is significant (pot_odds >= 0.33).
    """
    if round_idx < 2 or to_call <= 0:
        return False
    # v184 bluff-aware guard: suppress fold vs bluff-prone opponents.
    if _opp_bluff_prone(om):
        return False
    archetype, conf = classify_archetype(om)
    if archetype != 'rock' or conf < 0.12:
        return False
    if value_profile is not None and value_profile.get('tier') in ('nut', 'strong'):
        return False
    if draw_strength >= 0.15:
        return False
    if made_strength < 0.20 or made_strength >= 0.50:
        return False
    pot_odds = to_call / max(1, pot + to_call)
    if pot_odds < 0.33:
        return False
    # EV gate: fold when pot-odds exceed made-strength by >0.07 (clearly -EV call).
    ev_margin = pot_odds - made_strength
    if ev_margin <= 0.07:
        return False
    return True


def _multibarrel_line_fold(om, spot_info, round_idx, to_call, pot,
                            made_strength, value_profile, draw_strength):
    """v192 NEW DEFENSE: Line-evidence multi-barrel bluff-catcher fold.

    DISTINCT from _aggro_bluffcatcher_should_fold / _rock_value_bet_fold (which
    key on classify_archetype and are INERT when the read returns 'standard').
    This gate keys on DIRECT IN-HAND HISTORY EVIDENCE: the opponent has ACTUALLY
    bet a prior postflop street (opp_prior_postflop_raise_count >= 1) AND is
    betting the current street (opp_current_round_bet_count >= 1) — a confirmed
    multi-barrel line. No archetype dependency, so it fires in the 'standard'
    case where the archetype siblings are dead.

    Fires when ALL hold:
      - round_idx >= 2 (turn/river) AND to_call > 0 (facing a bet)
      - NOT a loose caller (fold_to_bet >= 0.35; absent signal => allow fold)
      - Multi-barrel line: opp bet a prior postflop street AND bets this street
      - made_strength < 0.42 (weak one-pair / two-pair bluff-catcher; trips+ preserved)
      - value_profile tier=='nut' NEVER folds; tier=='strong' folds ONLY on the
        TWO-PAIR band (made_strength >= 0.35) and is protected on the ONE-PAIR
        band (made_strength < 0.35, overpair/TPTK made~0.22)
      - draw_strength < 0.15 (no live draw to continue)
      - pot_odds >= 0.33 (bet >= half-pot — significant; aligned with
        _aggro_bluffcatcher_should_fold)

    Theory: a confirmed multi-barrel line from a value-polarized opponent (NOT
    bluff-prone) crushes weak one-pair. The hand only beats bluffs, and a
    non-bluff-prone opponent is not bluffing enough to make the call +EV.
    """
    # Guard 1: turn/river only + facing a bet.
    if round_idx < 2 or to_call <= 0:
        return False
    # v192-rework FIX 1: Narrow bluff carve-out. The full _opp_bluff_prone
    # detector (barreling_bluffy = low large_bet_ratio AND high postflop_aggr)
    # FALSE-POSITIVES on value-heavy multi-barrelers like v182 — the exact
    # opponent whose barrels produce the -20k stack-offs this gate targets —
    # so it suppressed the fold on the very spots it must fire. Only suppress
    # when there is DIRECT evidence calling is +EV: a loose caller who
    # under-folds to BETS (fold_to_bet < 0.35). Prefer top-level fold_to_bet,
    # then min(fold_to_bet_turn, fold_to_bet_river); skip if absent.
    ftb = om.get('fold_to_bet')
    if ftb is None:
        _per_street = [v for v in (om.get('fold_to_bet_turn'), om.get('fold_to_bet_river')) if v is not None]
        ftb = min(_per_street) if _per_street else None
    if ftb is not None and ftb < 0.35:
        return False
    # Guard 2: confirmed multi-barrel LINE EVIDENCE from actual in-hand history.
    # opp_prior_postflop_raise_count counts raise/allin on PAST postflop streets;
    # opp_current_round_bet_count counts bets THIS street. Both >= 1 => multi-barrel.
    if spot_info.get('opp_prior_postflop_raise_count', 0) < 1:
        return False
    if spot_info.get('opp_current_round_bet_count', 0) < 1:
        return False
    # Guard 3: weak bluff-catcher band (preserve trips+ at >= 0.42; two-pair
    # at 0.40 is INSIDE the band and is the documented -20k call-down leak).
    if made_strength >= 0.42:
        return False
    # v192-rework FIX 2: tier protection. The prior guard excluded tier in
    # ('nut','strong'), but postflop.py assigns 'strong' to ALL two-pair
    # (made_strength~0.40, the documented -20k call-down leak) AND to strong
    # one-pairs (overpair/TPTK, made_strength~0.22). Excluding 'strong' made
    # the gate INERT on the two-pair stack-offs. Now: protect nutted hands
    # always (tier=='nut'); protect strong-labeled ONE-PAIR hands
    # (made_strength < 0.35 — overpair/TPTK, made~0.22) from over-fold; but
    # ALLOW folding strong-labeled TWO-PAIR (made_strength >= 0.35 — the
    # 0.40 band, the leak). 0.35 cleanly separates one-pair (max ~0.232) from
    # two-pair (~0.40); trips+ (0.58) already excluded by made_strength<0.42.
    if value_profile is not None:
        _tier = value_profile.get('tier')
        if _tier == 'nut':
            return False
        if _tier == 'strong' and made_strength < 0.35:
            return False
    # Guard 5: no live draw.
    if draw_strength >= 0.15:
        return False
    # Guard 6: bet is significant (pot_odds >= 0.33, bet >= ~half-pot).
    # v192 rework FIX 2: tightened from 0.30 -> 0.33 to align with the sibling
    # _aggro_bluffcatcher_should_fold gate and avoid over-folding vs medium /
    # blocking bets where calling a weak bluff-catcher is reasonable.
    pot_odds = to_call / max(1, pot + to_call)
    if pot_odds < 0.33:
        return False
    return True


def _board_texture_bluff_raise(om, round_idx, to_call, made_strength, draw_strength,
                                board_texture, value_profile, pot, my_chips,
                                min_raise, my_round_bet):
    """v185 NEW OFFENSE: Board-texture-keyed +EV bluff raise on to_call==0.

    Fires on turn/river with AIR hands when board texture favors our perceived
    range (A-high unpaired, paired, monotone, K-high). DISTINCT from existing
    bluff paths: river_blocker_bluff needs ACE blockers + fold_to_raise>0.50;
    _delayed_calldown_bluff needs calldown_profile samples>=4; semi_bluff needs
    draw_strength>=0.12. This keys on BOARD TEXTURE (new signal axis) with
    explicit +EV math and polarized 0.60-0.75x pot sizing.
    """
    if round_idx < 2 or to_call > 0:
        return None
    if made_strength >= 0.18 or draw_strength >= 0.10:
        return None
    if value_profile is not None and value_profile.get('tier') in ('nut', 'strong'):
        return None
    if board_texture is None:
        return None
    conf = om.get('confidence', 0.0)
    if conf < 0.20:
        return None
    # v191 SAFETY FLOOR: skip bluff vs sticky opps (smoothed fold_to_raise <0.40).
    fold_to_raise = om.get('fold_to_raise', 0.44)
    if fold_to_raise < 0.40:
        return None

    # Board texture gates: textures where our PERCEIVED range is strong,
    # boosting opponent fold equity beyond the base fold_to_raise.
    boost = 0.0
    tag = 'none'
    high = board_texture.get('high_card', 0)
    paired = board_texture.get('paired', False)
    flush_p = board_texture.get('flush_pressure', 0.0)

    if high >= 14 and not paired:
        boost = 0.12; tag = 'ace_high'
    elif paired:
        boost = 0.10; tag = 'paired'
    elif flush_p >= 0.75:
        boost = 0.10; tag = 'monotone'
    elif high >= 13:
        boost = 0.06; tag = 'king_high'

    if boost < 0.06:
        return None

    # fold_to_raise smoothed prior = 0.44 (weight 2.0). Texture boost scales
    # by confidence so we don't over-trust the boost at low sample counts.
    base_ftr = om.get('fold_to_raise', 0.44)
    fold_equity = min(0.85, base_ftr + boost * conf)

    # Polarized sizing 0.60-0.75x pot scaled by confidence.
    bluff_ratio = 0.60 + 0.15 * conf
    target = int(pot * bluff_ratio)
    bet = max(min_raise, target - my_round_bet)
    bet = min(bet, my_chips - 1)
    if bet < min_raise:
        return None

    # +EV gate: fold_equity > bet / (pot + bet).  For 0.65x pot this is ~0.394.
    ev_threshold = bet / max(1, pot + bet)
    if fold_equity <= ev_threshold:
        try:
            sys.stderr.write('BLUFF_TEXTURE_RAISE fe=%.3f evt=%.3f tag=%s reason=ev_neg\n'
                             % (fold_equity, ev_threshold, tag))
        except Exception:
            pass
        return None

    # Never convert a bluff to all-in: cap at 65% of remaining chips.
    if bet >= my_chips * 0.72:
        bet = int(my_chips * 0.65)
        if bet < min_raise:
            return None

    try:
        sys.stderr.write('BLUFF_TEXTURE_RAISE fe=%.3f evt=%.3f tag=%s ratio=%.2f reason=fired\n'
                         % (fold_equity, ev_threshold, tag, bluff_ratio))
    except Exception:
        pass
    return bet


def _turn_float_value_donk(om, round_idx, to_call, made_strength, value_profile,
                            spot_info, board_texture, pot, my_chips,
                            min_raise, my_round_bet):
    """v193 NEW OFFENSE (option c): Turn/river value-donk after BB floats flop.

    Fires when: BB called a flop bet (float), PFR checked the turn/river,
    bot has a strong-but-not-nut hand, AND opponent betsize_polarity
    indicates an UNDERBETTOR (condensed thin-value range -> their flop bet
    was thin value, so they're capped on the turn).
    LEADS for value (creates a new betting spot; currently bot checks).
    DISTINCT from: donk (flop-only), probe (requires PFR checked prev street),
    value-sizing-UP (sizes EXISTING bets), bluff (requires air).
    Returns raise-to-total int, or None.
    """
    # Turn/river only, and only when facing a check (no bet to call).
    if round_idx not in (2, 3) or to_call != 0:
        return None
    # We must be the BB who defended vs a preflop raise.
    if not spot_info.get('my_is_bb', False):
        return None
    if spot_info.get('preflop_spot') != 'bb_vs_raise':
        return None
    # Opponent bet (raised) a previous round -> we floated that flop bet.
    if spot_info.get('opp_previous_round_raise_count', 0) < 1:
        return None
    # Opponent has NOT bet this round -> they checked the turn/river to us.
    if spot_info.get('opp_current_round_bet_count', 0) > 0:
        return None
    if spot_info.get('opp_current_round_check_count', 0) < 1:
        return None
    # Strong-but-not-nut made hand (top-pair-good-kicker through two-pair/sets
    # capped). Below 0.50 we lack the showdown value to lead for thin value.
    if made_strength < 0.50 or made_strength > 0.78:
        return None
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    if tier == 'nut':
        return None
    # v193: consume the betsize_polarity signal added by the Opponent Modeler.
    # Underbettor => their flop bet was condensed thin value -> capped on turn.
    polarity = om.get('betsize_polarity', {}) if om else {}
    if polarity.get('tendency') != 'underbettor':
        return None
    if polarity.get('confidence', 0.0) < 0.20:
        return None
    # Don't lead into scary runouts where we may be drawing dead vs monsters.
    if board_texture is not None:
        if board_texture.get('flush_pressure', 0.0) >= 0.75:
            return None
        if board_texture.get('straight_pressure', 0.0) >= 0.65:
            return None
    # Opponent model must be confident enough to trust the polarity signal.
    if om.get('confidence', 0.0) < 0.20:
        return None
    # Polarized value sizing 0.45-0.60x pot scaled by opponent confidence.
    conf = om.get('confidence', 0.20)
    base = 0.45 + 0.15 * min(1.0, conf / 0.30)
    target = int(pot * base)
    bet = max(min_raise, target - my_round_bet)
    bet = min(bet, my_chips - 1)
    if bet < min_raise or bet >= my_chips:
        return None
    return bet


def _estimate_bluff_frequency(om):
    """v190: Estimate opponent bluffing frequency in their bets (0.10-0.40).

    When opponent bets/raises on river, what fraction of their range is
    bluff (weak/valueless) vs value (strong)? Drives opponent-adaptive
    polarization discount in _river_potodds_equity_margin.

    Low (0.10-0.20): value-heavy (calling_station, rock) -> fold more.
    Mid (0.25): standard -> preserve current 0.65 discount.
    High (0.30-0.40): bluff-heavy (aggro, bluff-prone) -> fold less.

    Signals read (with smooth_rate priors from build_opponent_model):
      - archetype from classify_archetype(): primary signal
      - postflop_aggr: smoothed prior_mean=0.36, prior_weight=5.0
      - large_bet_ratio: RAW, default=0.32 (no smooth_rate wrapper)
      - _opp_bluff_prone(): composite boolean (existing v184 detector)
    """
    if om is None:
        return 0.25
    conf = om.get('confidence', 0.0)
    if conf < 0.15:
        return 0.25

    archetype, arch_conf = classify_archetype(om)

    # Archetype base: calling_station/rock bet value-heavy; aggro includes bluffs.
    if arch_conf >= 0.15:
        if archetype == 'calling_station':
            base = 0.15
        elif archetype == 'rock':
            base = 0.12
        elif archetype == 'aggro':
            base = 0.32
        else:
            base = 0.25  # standard
    else:
        base = 0.25

    # Adjustment: postflop_aggr (smoothed prior 0.36, weight 5.0).
    # Higher aggression -> more bluffs in range. Continuous, no deadzone.
    aggr = om.get('postflop_aggr', 0.36)
    base += max(0.0, (aggr - 0.36) / 0.20) * 0.06

    # Adjustment: large_bet_ratio (raw, default 0.32).
    # High LBR -> polarized big bets -> bluffs in range. Continuous.
    lbr = om.get('large_bet_ratio', 0.32)
    base += max(0.0, (lbr - 0.32) / 0.28) * 0.05

    # Bluff-prone soft boost (v184 detector): if opponent's bets include
    # bluffs (low LBR+high aggr, or low fold_to_bet), floor at 0.30.
    if _opp_bluff_prone(om):
        base = max(base, 0.30)

    return clamp(base, 0.10, 0.40)


def _river_potodds_equity_margin(round_idx, made_strength, draw_strength,
                                  value_profile, spot_info, pot_odds,
                                  opponent_model):
    '''v188: Pot-odds-vs-equity fold margin for river marginal hands.

    Targets the -20k / 0%-postflop-fold leak (18+ gens). realized_rate
    (monte_carlo vs random hands) overestimates equity for weak made hands
    facing a polarized value-betting range. This uses made_strength as a
    CALIBRATED river equity proxy (pair~=0.22, two-pair~=0.40 per the
    authoritative made-strength table; v187-corrected preflop equity makes
    this proxy trustworthy) and returns a POSITIVE continuous margin that
    raises the `realized_rate < pot_odds + call_margin` threshold so weak
    hands fold when their calibrated equity is below the price.

    Extends _river_stackoff_guard (binary, >=0.75x pot only) DOWN to the
    >=0.5x pot band where the leak concentrates (match analysis:
    'facing a bet >=0.5x pot'). FOLD-SIDE-RULE compliant: continuous
    margin (not binary return -1), archetype-gated (overbettor exempt),
    EV-gated (equity_gap > 0), and exempt for strong/nut/draw hands.

    v189 FIX: the v188 raw `equity_gap = pot_odds - made_strength`
    compared a price ratio (~0.27) against a hand-class ORDINAL
    (0.30+ for medium pairs), making the gate DEAD for made > 0.28.
    A polarization discount maps made_strength to an approximate equity
    vs a polarized value-betting range so the gap fires across the full
    leak band. The leak-band floor is also lowered from 0.50x to 0.40x
    pot to catch the uncovered medium-bet band (0.40-0.50x) where the
    same over-calling leak applies.
    '''
    # v190: Opponent-adaptive polarization discount (replaces v189 global 0.65).
    # discount = 0.40 + bluff_freq maps bluff_freq [0.10, 0.40] -> discount [0.50, 0.80].
    # At bluff_freq=0.25 (standard/unknown): discount=0.65 — identical to v189.
    # Lower (value-heavy) -> fold more; higher (bluff-heavy) -> fold less.
    bluff_freq = _estimate_bluff_frequency(opponent_model)
    discount = 0.40 + bluff_freq
    # v189: Leak-band floor lowered from 0.50 to 0.40 (preserved).
    MIN_LEAK_BET_RATIO = 0.40
    # 1. River only
    if round_idx != 3:
        return 0.0
    # 2. Must be facing a postflop bet
    if not spot_info.get('facing_postflop_aggression', False):
        return 0.0
    # 3. Bet-size gate: >=0.4x pot (v189: lowered from 0.50 to catch the
    #    uncovered medium-bet leak band; _river_stackoff_guard already handles
    #    >=0.75x via binary fold, so overlap is harmless)
    bet_ratio = spot_info.get('last_raise_pot_ratio', 0.0)
    if bet_ratio < MIN_LEAK_BET_RATIO:
        return 0.0
    # 4. Marginal made band: weak one-pair to weak two-pair.
    #    Air (<0.20) already folds via upstream gates; strong (>=0.50) keeps calling.
    if made_strength < 0.20 or made_strength >= 0.50:
        return 0.0
    # 5. Tier exemption: never fold strong/nut value
    if value_profile is not None and value_profile.get('tier') in ('strong', 'nut'):
        return 0.0
    # 6. Draw exemption: missed draws fold elsewhere; combo draws keep equity
    if draw_strength >= 0.15:
        return 0.0
    # 7. Overbettor exemption (polarized range includes bluffs — preserve v137 defense)
    if opponent_model is not None:
        sizing = opponent_model.get('sizing_tendency')
        if (sizing is not None
                and sizing.get('samples', 0) >= 8
                and sizing.get('confidence', 0.0) >= 0.30
                and sizing.get('tendency') == 'overbettor'):
            return 0.0
    # 8. Pot-odds-vs-equity gate: fire when calibrated equity (made_strength
    #    discounted for range polarization) is below the price (pot_odds).
    #    v189 FIX: the v188 raw `pot_odds - made_strength` compared a price
    #    ratio (~0.27) against a hand-class ordinal (0.30+ for medium pairs),
    #    making the gate dead for made > 0.28. The discount maps made_strength
    #    to an approximate equity vs a polarized value-betting range.
    calibrated_equity = made_strength * discount
    equity_gap = pot_odds - calibrated_equity
    if equity_gap <= 0.0:
        return 0.0
    # 9. Positive margin sized to overcome the monte_carlo overestimate.
    #    realized_rate for made 0.20-0.45 sits ~0.35-0.46 vs polarized ranges;
    #    we lift the threshold so those fold while two-pair+ (realized ~0.55+)
    #    continues. v197 MUTATION: TARGET_THRESHOLD 0.48 -> 0.42 (12.5% decrease)
    #    to reduce over-folding vs wide-barreling opponents (v177/v159). Combined
    #    with the crossover removing the direct binary fold gates in the to_call>0
    #    block, this restores v179's more robust call-down behavior while keeping
    #    the continuous EV-gated fold for the worst river bluff-catchers.
    target_threshold = 0.42
    # delta lifts threshold to target_threshold, plus a severity term scaled
    #    by how badly the hand misses the price. Clamp to [0, 0.25] (CONSTANT B).
    delta = (target_threshold - pot_odds) + equity_gap * 0.5
    delta = clamp(delta, 0.0, 0.25)
    try:
        sys.stderr.write(
            'RIVER_POTODDS_EQUITY delta_milli=%+d made=%.2f cal_eq=%.3f '
            'pot_odds=%.3f gap=%.3f bet_ratio=%.2f bluff_freq=%.3f '
            'discount=%.3f reason=fired\n'
            % (round(delta * 1000), made_strength, calibrated_equity,
               pot_odds, equity_gap, bet_ratio, bluff_freq, discount))
    except Exception:
        pass
    return delta


def _postflop_response_margin(round_idx, spot_info, opponent_model, made_strength,
                              draw_strength, value_profile, pair_profile,
                              draw_info, board_texture, paired_board_profile,
                              paired_board_stackoff, nutted_risk, line_profile,
                              line_strength, check_resistance, pot_odds,
                              state, blocker_profile):
    '''v186 REFACTOR: Unified postflop call_margin aggregator.
    Consolidates 16 previously-scattered additives from strategy.py into one
    function. BEHAVIORAL EQUIVALENCE — same computation, same result, same
    constants (0.035/0.04/0.10/0.05/0.50), same conditionals, same order.
    Returns the total call_margin (float).'''
    margin = postflop_call_margin(
        spot_info, opponent_model, made_strength, draw_strength,
        round_idx, spot_info['has_position'],
    )
    margin += pair_domination_margin(pair_profile, spot_info, round_idx)
    margin += draw_call_margin(draw_info, board_texture, round_idx, spot_info)
    if (round_idx == 2 and spot_info.get('facing_postflop_aggression')
            and pair_profile is not None
            and pair_profile.get('made_class') == 1
            and pair_profile.get('pair_type') in ('middle_pair', 'bottom_pair', 'underpair')):
        margin += 0.035
    margin += line_strength + paired_board_stackoff.get('line_strength', 0.0)
    margin += check_resistance
    margin += 0.50 * nutted_risk.get('risk', 0.0)
    margin += bluff_heavy_call_widen(
        line_profile, value_profile, made_strength, draw_strength,
        round_idx, opponent_model,
    )
    if round_idx == 3 and made_strength < 0.40 and not (blocker_profile and blocker_profile.get('eligible', False)):
        margin += 0.04
    if round_idx == 3 and made_strength < 0.55 and bet_size_bucket(spot_info.get('last_raise_pot_ratio', 0.0)) in ('medium', 'large'):
        margin += 0.10
    if round_idx == 3 and paired_board_profile is not None and paired_board_profile.get('fold_to_raise'):
        margin += 0.05
    if round_idx == 3:
        margin += _river_reraise_tighten(
            state, spot_info, made_strength, value_profile,
            round_idx, board_texture, pair_profile,
        )
    if opponent_model is not None:
        _cr_active, _cr_sev = check_raise_pressure(spot_info, opponent_model)
        if _cr_active:
            margin += _cr_sev
        _bp_active, _bp_sev = barrel_pressure_profile(spot_info, opponent_model, round_idx)
        if _bp_active:
            margin += _bp_sev
    margin += _multi_street_calldown_tax(
        spot_info, made_strength, draw_strength, value_profile, round_idx,
    )
    margin += _opponent_sizing_call_tighten(
        spot_info, opponent_model, round_idx, spot_info.get('has_position', False),
    )
    margin += _weak_one_pair_river_margin(
        round_idx, made_strength, draw_strength, spot_info, pot_odds,
    )
    margin += _river_potodds_equity_margin(
        round_idx, made_strength, draw_strength, value_profile,
        spot_info, pot_odds, opponent_model,
    )
    return margin


def classify_sizing_tendency(samples):
    """Per-street opponent bet-sizing profiler.

    Classifies an opponent into a sizing tendency bucket based on observed
    bet-to-pot ratios across postflop streets. Returns a dict.

    Buckets:
      - "overbettor": >=35% of samples have ratio >= 1.0 (polarized big bets)
      - "underbettor": >=40% of samples have ratio <= 0.30 (small/min bets)
      - "standard": majority in 0.30-1.0 pot range
      - "unknown": fewer than 6 samples

    Returned dict keys:
      - tendency: str bucket label (see above)
      - overbet_rate / underbet_rate / standard_rate: float fractions in [0,1]
      - samples: int count of valid postflop raise samples used
      - per_street_overbet {1,2,3}: float fraction of overbets per street
      - per_street_underbet {1,2,3}: float fraction of underbets per street
      - per_street_samples {1,2,3}: int raw sample count per street (debug aid)
      - confidence: float in [0,1] = min(1.0, n/20) — sample-size reliability
    """
    info = {
        "tendency": "unknown",
        "overbet_rate": 0.0,
        "underbet_rate": 0.0,
        "standard_rate": 0.0,
        "samples": 0,
        "per_street_overbet": {1: 0.0, 2: 0.0, 3: 0.0},
        "per_street_underbet": {1: 0.0, 2: 0.0, 3: 0.0},
        "per_street_samples": {1: 0, 2: 0, 3: 0},
        "confidence": 0.0,
    }
    if len(samples) < 6:
        return info

    street_counts = {1: 0, 2: 0, 3: 0}
    street_over = {1: 0, 2: 0, 3: 0}
    street_under = {1: 0, 2: 0, 3: 0}
    total_over = total_under = total_standard = 0
    for ridx, ratio in samples:
        if ridx not in (1, 2, 3):
            continue
        # Defensive guard: skip non-finite ratios (NaN/inf) so a single
        # corrupted sample can't poison the per-street rates. Guards against
        # rare malformed-history edge cases (e.g., zero-pot_estimate slipping
        # through if future callers bypass the pot_estimate>0 filter).
        if ratio != ratio or ratio in (float('inf'), float('-inf')):
            continue
        street_counts[ridx] += 1
        if ratio >= 1.0:
            total_over += 1
            street_over[ridx] += 1
        elif ratio <= 0.30:
            total_under += 1
            street_under[ridx] += 1
        else:
            total_standard += 1

    n = sum(street_counts.values())
    if n < 6:
        return info
    info["samples"] = n
    info["overbet_rate"] = total_over / n
    info["underbet_rate"] = total_under / n
    info["standard_rate"] = total_standard / n
    for ridx in (1, 2, 3):
        info["per_street_samples"][ridx] = street_counts[ridx]
        if street_counts[ridx] > 0:
            info["per_street_overbet"][ridx] = street_over[ridx] / street_counts[ridx]
            info["per_street_underbet"][ridx] = street_under[ridx] / street_counts[ridx]
    info["confidence"] = min(1.0, n / 20.0)

    if info["overbet_rate"] >= 0.35:
        info["tendency"] = "overbettor"
    elif info["underbet_rate"] >= 0.40:
        info["tendency"] = "underbettor"
    elif info["standard_rate"] >= 0.50:
        info["tendency"] = "standard"
    return info


def _opp_betsize_polarity(samples):
    """Per-street opponent bet-size-MAGNITUDE polarity profile (deal-local).

    More granular + lower-threshold (n>=4) than classify_sizing_tendency
    (which needs n>=6 and is empirically INERT — 'unknown'/'standard' — for
    most opponents in 70-hand HU matches). Buckets each postflop raise by
    ABSOLUTE magnitude:
      - 'shove': allin
      - 'overbet': ratio >= 1.0 (polarized nuts-or-air)
      - 'small': ratio <= 0.33 (condensed thin-value, ~50-80% equity)
      - 'standard': 0.33 < ratio < 1.0
    Returns dict with tendency + per-street rates + confidence.
    """
    info = {
        'tendency': 'unknown', 'overbet_rate': 0.0, 'small_rate': 0.0,
        'shove_rate': 0.0, 'avg_fraction': 0.0, 'samples': 0,
        'late_street_small_rate': 0.0,  # turn+river small-bet fraction
        'confidence': 0.0,
    }
    if len(samples) < 3:
        return info
    total = len(samples)
    n_over = n_small = n_shove = 0
    late_small = late_total = 0
    frac_sum = 0.0
    for ridx, ratio, is_allin in samples:
        frac_sum += ratio
        if is_allin:
            n_shove += 1
        elif ratio >= 1.0:
            n_over += 1
        elif ratio <= 0.33:
            n_small += 1
            if ridx >= 2:
                late_small += 1
        if ridx >= 2:
            late_total += 1
    info['samples'] = total
    info['overbet_rate'] = n_over / total
    info['small_rate'] = n_small / total
    info['shove_rate'] = n_shove / total
    info['avg_fraction'] = frac_sum / total
    info['late_street_small_rate'] = (late_small / late_total) if late_total > 0 else 0.0
    info['confidence'] = min(1.0, total / 12.0)
    # Classify: under-bettor if small-bets dominate (condensed thin-value range);
    # over-bettor if overbets/shoves dominate (polarized nuts-or-air).
    if info['small_rate'] >= 0.45 and info['overbet_rate'] + info['shove_rate'] <= 0.25:
        info['tendency'] = 'underbettor'
    elif info['overbet_rate'] + info['shove_rate'] >= 0.40:
        info['tendency'] = 'overbettor'
    else:
        info['tendency'] = 'standard'
    return info


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
                # Detect if SB limped (call) vs raised
                sb_first_action = None
                for rec in history:
                    if rec["player_id"] == my_id and rec["round"] == 0:
                        sb_first_action = rec["action_type"]
                        break
                if sb_first_action == "call":
                    info["preflop_spot"] = "sb_vs_iso_raise"
                else:
                    info["preflop_spot"] = "sb_vs_reraise"

    return info


def _semibluff_raise_construct(om, round_idx, to_call, pot, made_strength,
                                draw_info, draw_strength, board_texture,
                                value_profile, spot_info, my_chips,
                                min_raise, my_round_bet):
    """v236 OFFENSE: Semi-bluff raise construction on flop/turn.
    Ported from v233 with loosened gates per experience-pool guidance.
    Fires when facing a bet with 8+ out draw + marginal hand vs over-folding opp.
    Constructs raise_to = to_call * 2.8 to exploit fold equity.
    """
    import sys as _sys
    if round_idx < 1 or to_call <= 0:
        return None
    if draw_info is None or not draw_info.get('semi_bluff', False):
        return None
    if draw_strength < 0.14:
        return None
    if made_strength < 0.12 or made_strength > 0.44:
        return None
    if value_profile is not None and value_profile.get('tier') in ('strong', 'nut'):
        return None
    if board_texture is None:
        return None
    wetness = board_texture.get('wetness', 0.0)
    has_flush = draw_info.get('flush_draw', False)
    has_combo = draw_info.get('combo_draw', False)
    if not (wetness >= 0.30 or has_flush or has_combo):
        return None
    opp_bet_ratio = spot_info.get('last_raise_pot_ratio', 0.0)
    if opp_bet_ratio < 0.25 or opp_bet_ratio > 0.65:
        return None
    fold_to_raise = om.get('fold_to_raise', 0.44)
    if fold_to_raise < 0.45:
        return None
    conf = om.get('confidence', 0.0)
    if conf < 0.20:
        return None
    spr = my_chips / max(1, pot)
    if spr <= 3.0:
        return None
    raise_to_total = int(to_call * 2.8) + my_round_bet
    if raise_to_total < min_raise:
        raise_to_total = min_raise
    if raise_to_total > my_chips:
        return None
    if raise_to_total <= to_call + my_round_bet:
        return None
    bet_increment = raise_to_total - my_round_bet - to_call
    pot_if_called = pot + to_call + bet_increment
    ev_threshold = bet_increment / max(1, pot_if_called)
    fold_equity = min(0.85, fold_to_raise + 0.08 * conf)
    if fold_equity <= ev_threshold:
        try:
            _sys.stderr.write('SEMIBLUFF_RAISE fe=%.3f evt=%.3f reason=ev_neg\n'
                              % (fold_equity, ev_threshold))
        except Exception:
            pass
        return None
    try:
        _sys.stderr.write('SEMIBLUFF_RAISE fe=%.3f evt=%.3f rto=%d made=%.2f '
                          'ds=%.2f dt=%s ftr=%.2f spr=%.1f reason=fired\n'
                          % (fold_equity, ev_threshold, raise_to_total,
                             made_strength, draw_strength,
                             draw_info.get('type', 'none'),
                             fold_to_raise, spr))
    except Exception:
        pass
    return raise_to_total


def _river_value_raise_construct(om, round_idx, to_call, pot, made_strength,
                                  value_profile, spot_info, my_chips, min_raise,
                                  my_round_bet, board_texture, pair_profile):
    """v246 OFFENSE: River value overbet raise for nut/strong made hands facing
    a bet. Fires 1.5x-pot raise-to-total to extract max value from over-callers.
    STRONG-tier (non-nut, made 0.55-0.69) is GATED on board safety — mirrors
    _river_value_ship_guard predicates (flush_pressure<0.75 AND straight_pressure
    <0.65 AND paired-board requires made_class>=3) — so unsafe-board strong-tier
    hands fall through to choose_raise which DOES pass _river_value_ship_guard.
    NUT-tier (made>=0.70) is EXEMPT — may fire on any board.

    v246 DEFAULT-PERMIT opp-gate: the overbet fires by default for unknown /
    low-confidence opponents (om is None, confidence<0.20, or large_bet_ratio
    missing) — the +EV weapon from v245 is preserved. Block ONLY on confident
    positive evidence of a polarized re-shover (confidence>=0.20 AND
    large_bet_ratio>=0.50) who will shove over our raise. Returns int or None.
    """
    import sys as _sys
    if round_idx != 3 or to_call <= 0:
        return None
    if value_profile is None:
        return None
    tier = value_profile.get('tier', 'none')
    if tier not in ('strong', 'nut') or made_strength < 0.55:
        return None
    # STRONG-tier board-safety gate (mirror _river_value_ship_guard)
    if tier == 'strong':
        board_safe = True
        if board_texture is not None:
            if board_texture.get('flush_pressure', 0.0) >= 0.75:
                board_safe = False
            if board_texture.get('straight_pressure', 0.0) >= 0.65:
                board_safe = False
            if board_texture.get('paired', False):
                mc = pair_profile.get('made_class', -1) if pair_profile else -1
                if mc < 3:
                    board_safe = False
        if not board_safe:
            try:
                _sys.stderr.write(
                    'RIVER_VALUE_RAISE made=%.2f tier=%s reason=unsafe_board_strong\n'
                    % (made_strength, tier))
            except Exception:
                pass
            return None
    # v246: DEFAULT-PERMIT opp-gate. Fire the +EV overbet by default (unknown /
    # low-confidence opponents still get value-extracted; the STRONG-tier board-
    # safety gate above already protects unsafe boards). Block ONLY on confident
    # positive evidence of a polarized re-shover who will shove over our raise.
    if om is not None:
        _rvr_conf = om.get('confidence', 0.0)
        _rvr_lbr = om.get('large_bet_ratio', 0.0)
        if _rvr_conf >= 0.20 and _rvr_lbr >= 0.50:
            try:
                _sys.stderr.write(
                    'RIVER_VALUE_RAISE reason=opp_gate_confirmed_shover conf=%.2f lbr=%.2f\n'
                    % (_rvr_conf, _rvr_lbr))
            except Exception:
                pass
            return None
    # Sizing: 1.5x-pot overbet
    spr = my_chips / max(1, pot)
    if spr <= 2.0:
        return None
    raise_to_total = int(pot * 1.5) + my_round_bet
    if raise_to_total < min_raise:
        raise_to_total = min_raise
    if raise_to_total > my_chips or raise_to_total <= to_call + my_round_bet:
        return None
    try:
        _sys.stderr.write(
            'RIVER_VALUE_RAISE made=%.2f tier=%s rto=%d spr=%.1f reason=fired\n'
            % (made_strength, tier, raise_to_total, spr))
    except Exception:
        pass
    return raise_to_total


if __name__ == '__main__':
    # Verify smooth_rate weight change: a TRUE 53% folder at n=20 should
    # produce a higher smoothed value with weight=2.0 vs weight=4.0
    w2 = smooth_rate(11, 20, 0.44, 2.0)  # (11+0.88)/22 = 0.5382
    w4 = smooth_rate(11, 20, 0.44, 4.0)  # (11+1.76)/24 = 0.5317
    assert w2 > w4, f'weight=2.0 should be more responsive: {w2} vs {w4}'
    assert w2 > 0.50, f'53%% folder should cross 0.50 gate with weight=2.0: {w2}'
    print(f'smooth_rate weight verification PASS: w2={w2:.4f} > w4={w4:.4f}')

    # v189 _river_potodds_equity_margin self-test (M5/M6: non-zero in leak band)
    _pe_spot_fire = {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.65}
    _pe_vp_marginal = {'tier': 'thin'}
    _pe_delta_fire = _river_potodds_equity_margin(
        3, 0.28, 0.05, _pe_vp_marginal, _pe_spot_fire, 0.33, None)
    assert _pe_delta_fire > 0.0, ('RIVER_POTODDS_EQUITY must fire >0 in leak band (made=0.28 pot_odds=0.33), got %.4f' % _pe_delta_fire)
    assert _pe_delta_fire >= 0.10, ('RIVER_POTODDS_EQUITY must overcome monte_carlo overestimate (>=0.10), got %.4f' % _pe_delta_fire)
    # v189 regression: made=0.30 (medium pair) at 0.6x pot was DEAD in v188
    # (made_strength 0.30 > pot_odds 0.273 -> gap<=0). The polarization discount
    # (0.65) maps made_strength to ~0.195 equity vs a polarized range, so the
    # gate now fires. This is the core INERT-bug regression guard.
    _pe_spot_06x = {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.60}
    _pe_delta_dead_band = _river_potodds_equity_margin(
        3, 0.30, 0.05, _pe_vp_marginal, _pe_spot_06x, 0.273, None)
    assert _pe_delta_dead_band > 0.0, ('RIVER_POTODDS_EQUITY v189 regression: made=0.30 at 0.6x pot must now fire (was DEAD in v188), got %.4f' % _pe_delta_dead_band)
    # v189 boundary: bet_ratio exactly at new floor 0.40 must fire; below must not.
    _pe_spot_04x = {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.40}
    assert _river_potodds_equity_margin(3, 0.30, 0.05, _pe_vp_marginal, _pe_spot_04x, 0.222, None) > 0.0, 'bet=0.40x at new floor must fire'
    # Safe-band zeros
    assert _river_potodds_equity_margin(3, 0.55, 0.05, _pe_vp_marginal, _pe_spot_fire, 0.33, None) == 0.0, 'made>=0.50 must be exempt'
    assert _river_potodds_equity_margin(3, 0.28, 0.20, _pe_vp_marginal, _pe_spot_fire, 0.33, None) == 0.0, 'draw>=0.15 must be exempt'
    assert _river_potodds_equity_margin(3, 0.28, 0.05, {'tier': 'nut'}, _pe_spot_fire, 0.33, None) == 0.0, 'nut tier must be exempt'
    assert _river_potodds_equity_margin(3, 0.28, 0.05, _pe_vp_marginal, {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.30}, 0.33, None) == 0.0, 'bet<0.4x must be exempt'
    assert _river_potodds_equity_margin(2, 0.28, 0.05, _pe_vp_marginal, _pe_spot_fire, 0.33, None) == 0.0, 'non-river must be exempt'
    _pe_vp_strong = {'tier': 'strong'}
    assert _river_potodds_equity_margin(3, 0.40, 0.05, _pe_vp_strong, _pe_spot_fire, 0.45, None) == 0.0, 'strong tier exempt even in leak made-band'

    # v190: opponent-adaptive discount self-tests
    # 1. Calling station (value-heavy): discount should be 0.55 (bluff_freq=0.15)
    #    -> fires MORE aggressively (gap larger at same made/pot_odds)
    _pe_om_cs = {'confidence': 0.6, 'postflop_aggr': 0.30, 'large_bet_ratio': 0.28}
    # classify_archetype will return 'calling_station' for low aggr+low lbr+high vpip
    # but since we don't have full model fields, set minimum needed:
    _pe_om_cs['vpip'] = 0.72  # high vpip -> calling_station
    _pe_om_cs['pfr'] = 0.20   # low pfr
    _pe_om_cs['fold_to_raise'] = 0.40
    _pe_om_cs['victory_momentum_index'] = 0.65
    _pe_delta_cs = _river_potodds_equity_margin(
        3, 0.35, 0.05, _pe_vp_marginal, _pe_spot_06x, 0.273, _pe_om_cs)
    # For made=0.35 at discount=0.55: calibrated=0.1925, gap=0.273-0.1925=0.0805
    # Old (discount=0.65): calibrated=0.2275, gap=0.273-0.2275=0.0455
    # CS delta should be LARGER (more aggressive fold)
    assert _pe_delta_cs > _pe_delta_dead_band, (
        'v190 calling_station must fold more aggressively than standard: '
        'cs_delta=%.4f vs std_delta=%.4f' % (_pe_delta_cs, _pe_delta_dead_band))

    # 2. Aggro opponent (bluff-heavy): discount should be 0.72+ (bluff_freq=0.32+)
    #    -> at made=0.40 pot_odds=0.273: old calibrated=0.26, gap=0.013 (fires)
    #    -> new calibrated=0.40*0.72=0.288, gap=0.273-0.288=-0.015<=0 (does NOT fire)
    _pe_om_aggr = {'confidence': 0.8, 'postflop_aggr': 0.52, 'large_bet_ratio': 0.45,
                   'vpip': 0.65, 'pfr': 0.45, 'fold_to_raise': 0.32,
                   'fold_to_bet_turn': 0.15, 'fold_to_bet_river': 0.20,
                   'victory_momentum_index': 0.65}
    _pe_delta_aggr = _river_potodds_equity_margin(
        3, 0.40, 0.05, _pe_vp_marginal, _pe_spot_06x, 0.273, _pe_om_aggr)
    assert _pe_delta_aggr == 0.0, (
        'v190 aggro bluff-heavy: made=0.40 should NOT fold at pot_odds=0.273 '
        '(calibrated equity exceeds pot_odds), got %.4f' % _pe_delta_aggr)

    # 3. Standard/unknown opponent: MUST match v189 (discount=0.65)
    _pe_delta_std = _river_potodds_equity_margin(
        3, 0.28, 0.05, _pe_vp_marginal, _pe_spot_fire, 0.33, {'confidence': 0.5, 'postflop_aggr': 0.36, 'large_bet_ratio': 0.32})
    assert abs(_pe_delta_std - _pe_delta_fire) < 0.001, (
        'v190 standard opponent must match v189 (discount=0.65): '
        'std=%.4f vs v189=%.4f' % (_pe_delta_std, _pe_delta_fire))
    print('RIVER_POTODDS_EQUITY self-test PASS (v190 adaptive): '
          'cs_delta=%.4f aggr_delta=%.4f std_delta=%.4f'
          % (_pe_delta_cs, _pe_delta_aggr, _pe_delta_std))
    print('RIVER_POTODDS_EQUITY self-test PASS: fire_delta=%.4f dead_band=%.4f' % (_pe_delta_fire, _pe_delta_dead_band))

    # v192-rework _multibarrel_line_fold self-tests (original 11 + 5 rework two-pair/overpair/nut/loose = 16)
    # Non-bluff-prone om (high large_bet_ratio, value-polarized) so the gate fires.
    _mb_om_value = {'large_bet_ratio': 0.65, 'postflop_aggr': 0.20, 'fold_to_bet': 0.50}
    _mb_spot_fire = {
        'opp_prior_postflop_raise_count': 1,
        'opp_current_round_bet_count': 1,
        'facing_postflop_aggression': True,
    }
    _mb_vp_thin = {'tier': 'thin'}

    # Positive fires: double-barrel on turn, weak one-pair, big bet
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 2, 400, 600, 0.30, _mb_vp_thin, 0.05) is True, \
        'turn double-barrel weak-pair should fold'

    # Positive fires: double-barrel on river
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 3, 400, 600, 0.30, _mb_vp_thin, 0.05) is True, \
        'river double-barrel weak-pair should fold'

    # Should NOT fire: preflop (round_idx < 2)
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 1, 400, 600, 0.30, _mb_vp_thin, 0.05) is False, \
        'preflop should not fire'

    # Should NOT fire: to_call <= 0 (check to us)
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 2, 0, 600, 0.30, _mb_vp_thin, 0.05) is False, \
        'no bet should not fire'

    # Should NOT fire: no prior barrel (single bet)
    _mb_spot_single = {'opp_prior_postflop_raise_count': 0, 'opp_current_round_bet_count': 1}
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_single, 2, 400, 600, 0.30, _mb_vp_thin, 0.05) is False, \
        'single barrel should not fire'

    # Should NOT fire: two-pair+ (made >= 0.42)
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 2, 400, 600, 0.50, _mb_vp_thin, 0.05) is False, \
        'strong hand should not fold'

    # Should NOT fire: strong/nut tier
    _mb_vp_nut = {'tier': 'nut'}
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 2, 400, 600, 0.30, _mb_vp_nut, 0.05) is False, \
        'nut tier should not fold'

    # Should NOT fire: has a draw (draw >= 0.15)
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 2, 400, 600, 0.30, _mb_vp_thin, 0.20) is False, \
        'draw should not fold'

    # Should NOT fire: small bet (pot_odds < 0.33) — tests tightened floor
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 2, 100, 900, 0.30, _mb_vp_thin, 0.05) is False, \
        'small bet (pot_odds=0.10) should not fire'

    # v192-rework: bare barreling-bluffy profile (low lbr + high aggr, NO
    # fold_to_bet signal) now FIRES — the loose_caller-only carve-out replaced
    # the over-broad _opp_bluff_prone detector, which false-positived on
    # value-heavy multi-barrelers like v182. Value-heavy could be v182 => fold.
    _mb_om_bluffy = {'large_bet_ratio': 0.35, 'postflop_aggr': 0.50}
    assert _multibarrel_line_fold(_mb_om_bluffy, _mb_spot_fire, 2, 400, 600, 0.30, _mb_vp_thin, 0.05) is True, \
        'barreling_bluffy WITHOUT fold_to_bet signal => fires (value-heavy could be v182)'
    # loose_caller: low fold_to_bet (<0.35) suppresses even a foldable hand.
    _mb_om_sticky = {'large_bet_ratio': 0.65, 'postflop_aggr': 0.20, 'fold_to_bet': 0.20}
    assert _multibarrel_line_fold(_mb_om_sticky, _mb_spot_fire, 2, 400, 600, 0.30, _mb_vp_thin, 0.05) is False, \
        'bluff-prone om (loose_caller) should not fold'

    # v192-rework: TWO-PAIR 'strong'-labeled hand now FIRES (was INERT — root cause).
    # made=0.40 (two-pair band, >=0.35) + tier='strong' => fold (the documented -20k leak).
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 2, 400, 600, 0.40, {'tier':'strong'}, 0.05) is True, \
        'two-pair strong-labeled MUST fold (rework fix)'
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 3, 400, 600, 0.40, {'tier':'strong'}, 0.05) is True, \
        'river two-pair strong-labeled MUST fold (rework fix)'
    # v192-rework: ONE-PAIR 'strong'-labeled (overpair/TPTK) still PROTECTED (made<0.35).
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 2, 400, 600, 0.24, {'tier':'strong'}, 0.05) is False, \
        'overpair/TPTK strong one-pair MUST NOT fold (over-fold guard)'
    # v192-rework: NUT always protected even in two-pair band.
    assert _multibarrel_line_fold(_mb_om_value, _mb_spot_fire, 2, 400, 600, 0.40, {'tier':'nut'}, 0.05) is False, \
        'nut always protected'
    # v192-rework: loose_caller carve-out (fold_to_bet<0.35) suppresses even two-pair.
    _mb_om_loose = {'fold_to_bet': 0.20, 'large_bet_ratio': 0.2, 'postflop_aggr': 0.6}
    assert _multibarrel_line_fold(_mb_om_loose, _mb_spot_fire, 2, 400, 600, 0.40, {'tier':'strong'}, 0.05) is False, \
        'loose_caller (fold_to_bet<0.35) suppresses fold'
    print('_multibarrel_line_fold rework: new two-pair/overpair/nut/loose assertions PASS')

    # v193 _opp_betsize_polarity reachability self-tests
    # 1. Five small-bet samples (ratio<=0.33, no allins) -> underbettor.
    _bsp_under = [(1, 0.25, False), (2, 0.20, False), (3, 0.30, False),
                  (1, 0.22, False), (2, 0.28, False)]
    assert _opp_betsize_polarity(_bsp_under)['tendency'] == 'underbettor', \
        '5 small-bet samples must classify as underbettor'
    # 2. Five overbet samples (ratio>=1.0, no allins) -> overbettor.
    _bsp_over = [(1, 1.5, False), (2, 2.0, False), (3, 1.2, False),
                 (1, 1.8, False), (2, 1.6, False)]
    assert _opp_betsize_polarity(_bsp_over)['tendency'] == 'overbettor', \
        '5 overbet samples must classify as overbettor'
    # 3. Fewer than 4 samples -> unknown.
    _bsp_few = [(1, 0.25, False), (2, 0.30, False), (3, 0.20, False)]
    assert _opp_betsize_polarity(_bsp_few)['tendency'] == 'unknown', \
        '<4 samples must classify as unknown'
    print('_opp_betsize_polarity reachability self-test PASS: '
          'underbettor/overbettor/unknown')

    # v193 _turn_float_value_donk reachability self-tests
    _tfd_spot = {
        'my_is_bb': True,
        'preflop_spot': 'bb_vs_raise',
        'opp_previous_round_raise_count': 1,
        'opp_current_round_bet_count': 0,
        'opp_current_round_check_count': 1,
    }
    _tfd_om = {
        'confidence': 0.30,
        'betsize_polarity': {
            'tendency': 'underbettor',
            'confidence': 0.40,
            'small_rate': 0.6,
            'overbet_rate': 0.0,
        },
    }
    _tfd_vp = {'tier': 'strong'}
    _tfd_bt = {'flush_pressure': 0.2, 'straight_pressure': 0.2}
    # POSITIVE: BB float, underbettor PFR checked turn, strong hand -> leads.
    _tfd_bet = _turn_float_value_donk(
        _tfd_om, 2, 0, 0.55, _tfd_vp, _tfd_spot, _tfd_bt, 1000, 8000, 100, 0)
    assert isinstance(_tfd_bet, int) and _tfd_bet >= 100, (
        'TURN_FLOAT_VALUE_DONK positive must return int>=100, got %r' % (_tfd_bet,))
    print('TURN_FLOAT_VALUE_DONK PASS: bet=%d (pot=1000, ~0.60x)' % _tfd_bet)
    # NEGATIVE: opponent bet this round (no free check) -> None.
    _tfd_spot_neg = dict(_tfd_spot)
    _tfd_spot_neg['opp_current_round_bet_count'] = 1
    _tfd_none = _turn_float_value_donk(
        _tfd_om, 2, 0, 0.55, _tfd_vp, _tfd_spot_neg, _tfd_bt, 1000, 8000, 100, 0)
    assert _tfd_none is None, (
        'TURN_FLOAT_VALUE_DONK negative (opp bet this round) must return None, got %r'
        % (_tfd_none,))
    print('TURN_FLOAT_VALUE_DONK NEGATIVE PASS: opp-bet-this-round -> None')

    # v245 _river_value_raise_construct self-test (4 cases)
    _rvr_om = {'confidence': 0.30, 'fold_to_raise': 0.44}
    _rvr_spot = {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.65}
    _rvr_bt_safe = {'flush_pressure': 0.2, 'straight_pressure': 0.2, 'paired': False}
    _rvr_bt_unsafe = {'flush_pressure': 0.80, 'straight_pressure': 0.2, 'paired': False}
    # Case 1: nut-tier (made=0.75) on safe board -> fires
    _rvr_r1 = _river_value_raise_construct(
        _rvr_om, 3, 200, 1000, 0.75, {'tier': 'nut'}, _rvr_spot,
        8000, 400, 0, _rvr_bt_safe, None)
    assert isinstance(_rvr_r1, int) and _rvr_r1 > 200, (
        'RIVER_VALUE_RAISE nut-tier must fire, got %r' % (_rvr_r1,))
    print('RIVER_VALUE_RAISE case1 PASS: nut-tier fires rto=%d' % _rvr_r1)
    # Case 2: weak hand (made=0.30) -> None
    _rvr_r2 = _river_value_raise_construct(
        _rvr_om, 3, 200, 1000, 0.30, {'tier': 'thin'}, _rvr_spot,
        8000, 400, 0, _rvr_bt_safe, None)
    assert _rvr_r2 is None, (
        'RIVER_VALUE_RAISE weak hand must return None, got %r' % (_rvr_r2,))
    print('RIVER_VALUE_RAISE case2 PASS: weak-hand -> None')
    # Case 3: low SPR (shallow) -> None
    _rvr_r3 = _river_value_raise_construct(
        _rvr_om, 3, 200, 1000, 0.75, {'tier': 'nut'}, _rvr_spot,
        1500, 400, 0, _rvr_bt_safe, None)
    assert _rvr_r3 is None, (
        'RIVER_VALUE_RAISE shallow SPR must return None, got %r' % (_rvr_r3,))
    print('RIVER_VALUE_RAISE case3 PASS: shallow-SPR -> None')
    # Case 4: strong-tier (made=0.62) on UNSAFE board -> None (board-safety gate)
    _rvr_r4 = _river_value_raise_construct(
        _rvr_om, 3, 200, 1000, 0.62, {'tier': 'strong'}, _rvr_spot,
        8000, 400, 0, _rvr_bt_unsafe, None)
    assert _rvr_r4 is None, (
        'RIVER_VALUE_RAISE strong-tier unsafe-board must return None, got %r'
        % (_rvr_r4,))
    print('RIVER_VALUE_RAISE case4 PASS: strong-tier unsafe-board -> None')
    # Cross-check: strong-tier on SAFE board still fires
    _rvr_r4b = _river_value_raise_construct(
        _rvr_om, 3, 200, 1000, 0.62, {'tier': 'strong'}, _rvr_spot,
        8000, 400, 0, _rvr_bt_safe, None)
    assert isinstance(_rvr_r4b, int) and _rvr_r4b > 200, (
        'RIVER_VALUE_RAISE strong-tier safe-board must fire, got %r' % (_rvr_r4b,))
    print('RIVER_VALUE_RAISE case4b PASS: strong-tier safe-board fires rto=%d'
          % _rvr_r4b)
