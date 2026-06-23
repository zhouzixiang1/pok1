from constants import BIG_BLIND, N_PLAYERS, SMALL_BLIND
from card_utils import clamp, next_player
from state import collect_latest_requests_by_hand
from tournament import opponent_can_lock_win


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
        "calldown_profile": calldown_profile,
        "river_call_size_ratio": river_call_size_ratio,
    }


def classify_archetype(om):
    """Classify opponent into archetype using already-LIVE signals.
    Returns 'calling_station', 'rock', 'aggro', or 'standard'.
    Requires confidence >= 0.20; below that returns 'standard' (no behavior change).

    calling_station: never folds postflop (ftb_avg<0.38), passive (aggr<0.34), loose (vpip>0.52)
    rock: folds too much (ftb_avg>0.50), passive (aggr<0.32)
    aggro: very aggressive (postflop_aggr>0.42 or aggression>0.38)
    standard: default (backward compatible)"""
    if om is None or om.get('confidence', 0) < 0.20:
        return 'standard'
    ftb_avg = (om.get('fold_to_bet_flop', 0.44) +
               om.get('fold_to_bet_turn', 0.44) +
               om.get('fold_to_bet_river', 0.44)) / 3.0
    vpip = om.get('vpip', 0.58)
    aggr = om.get('postflop_aggr', 0.36)
    overall_aggr = om.get('aggression', 0.30)
    if ftb_avg < 0.38 and aggr < 0.34 and vpip > 0.52:
        return 'calling_station'
    if ftb_avg > 0.50 and aggr < 0.32:
        return 'rock'
    if aggr > 0.42 or overall_aggr > 0.38:
        return 'aggro'
    return 'standard'


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


if __name__ == '__main__':
    # Verify smooth_rate weight change: a TRUE 53% folder at n=20 should
    # produce a higher smoothed value with weight=2.0 vs weight=4.0
    w2 = smooth_rate(11, 20, 0.44, 2.0)  # (11+0.88)/22 = 0.5382
    w4 = smooth_rate(11, 20, 0.44, 4.0)  # (11+1.76)/24 = 0.5317
    assert w2 > w4, f'weight=2.0 should be more responsive: {w2} vs {w4}'
    assert w2 > 0.50, f'53%% folder should cross 0.50 gate with weight=2.0: {w2}'
    print(f'smooth_rate weight verification PASS: w2={w2:.4f} > w4={w4:.4f}')

    cs = {'confidence':0.30,'fold_to_bet_flop':0.30,'fold_to_bet_turn':0.35,'fold_to_bet_river':0.32,'vpip':0.62,'postflop_aggr':0.28,'aggression':0.25}
    assert classify_archetype(cs) == 'calling_station', classify_archetype(cs)
    rock_m = {'confidence':0.30,'fold_to_bet_flop':0.55,'fold_to_bet_turn':0.52,'fold_to_bet_river':0.50,'vpip':0.40,'postflop_aggr':0.28,'aggression':0.22}
    assert classify_archetype(rock_m) == 'rock', classify_archetype(rock_m)
    assert classify_archetype(None) == 'standard'
    assert classify_archetype({'confidence':0.10}) == 'standard'
    std = {'confidence':0.30,'fold_to_bet_flop':0.44,'fold_to_bet_turn':0.44,'fold_to_bet_river':0.44,'vpip':0.58,'postflop_aggr':0.36,'aggression':0.30}
    assert classify_archetype(std) == 'standard', classify_archetype(std)
    print('classify_archetype: ALL TESTS PASS')
