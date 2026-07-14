import sys
from constants import (
    BIG_BLIND, SMALL_BLIND, N_PLAYERS,
    PRIOR_VPIP, PRIOR_PFR, PRIOR_ALLIN_RATE, PRIOR_POSTFLOP_AGGR,
    PRIOR_POSTFLOP_CHECK, PRIOR_FOLD_TO_RAISE, PRIOR_AGGRESSION,
    PRIOR_FLOP_AGGR, PRIOR_TURN_AGGR, PRIOR_RIVER_AGGR, PRIOR_BARREL_FREQ,
    PRIOR_VPID_WEIGHT, PRIOR_PFR_WEIGHT, PRIOR_ALLIN_WEIGHT,
    PRIOR_POSTFLOP_AGGR_WEIGHT, PRIOR_POSTFLOP_CHECK_WEIGHT, PRIOR_FTR_WEIGHT, PRIOR_AGGRESSION_WEIGHT,
    PRIOR_FLOP_AGGR_WEIGHT, PRIOR_TURN_AGGR_WEIGHT, PRIOR_RIVER_AGGR_WEIGHT, PRIOR_BARREL_WEIGHT,
    DEFAULT_AVG_RAISE_BB, DEFAULT_FLOP_RAISE_BB, DEFAULT_TURN_RAISE_BB, DEFAULT_RIVER_RAISE_BB,
    CONFIDENCE_OFFSET, CONFIDENCE_SCALE,
)

# Local opponent-model constants (avoid cross-file import race during parallel worker builds)
PRIOR_FOLD_TO_JAM = 0.45
PRIOR_FOLD_TO_JAM_WEIGHT = 6.0
from card_utils import clamp, next_player
from state import collect_latest_requests_by_hand
from tournament import opponent_can_lock_win

# River overcall stat prior (callers > folders in HU, so prior slightly above 0.50)
PRIOR_RIVER_OVERCALL = 0.55
PRIOR_RIVER_OVERCALL_WEIGHT = 6.0

# Bet-size polarity and postflop shove-rate opponent priors.
# betsize_polarity is continuous [-1,+1]: +1 = polarized (mix of large/small
# raises), -1 = uniform sizing. shove_rate is the fraction of postflop
# aggressive actions that are all-in shoves.
PRIOR_BETSIZE_POLARITY = 0.0
PRIOR_BETSIZE_POLARITY_WEIGHT = 6.0
PRIOR_SHOVE_RATE = 0.08
PRIOR_SHOVE_RATE_WEIGHT = 8.0


def classify_opponent_archetype(opponent_model):
    """Classify opponent into behavioral archetype for structural preflop adjustments.

    Ported from claude_v38 (Parent B). Returns one of:
      'calling_station', 'nit', 'lag', 'tag', 'unknown'.

    Requires confidence >= 0.15 to avoid noisy misclassification on small samples.
    Uses the shared PRIOR_* constants as neutral fallbacks so this classifier
    stays consistent with the rest of v45's opponent modeling rather than
    introducing a parallel hardcoded prior set.

    H2H motivation (head_to_head.json):
      - v38 beats v269 +26.7pp, v235 +20.5pp, v236 +15.3pp where v45 loses.
        The differentiator is v38's archetype-aware preflop play: it skips
        bluff 3-bets vs calling stations, gates light 4-bets by archetype,
        and calls wider vs LAGs that 3-bet light. v45 lacks this classifier.

    Archetype definitions:
      - calling_station: high VPIP, low fold-to-raise, passive postflop
        -> don't bluff 3bet (they don't fold enough)
      - nit: low VPIP, high fold-to-raise
        -> wider bluff range (they fold)
      - lag: high VPIP with high postflop aggression
        -> respect their raises, call wider (they 3bet light)
      - tag: balanced tight-aggressive
        -> play standard
    """
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.15:
        return 'unknown'

    vpip = opponent_model.get('vpip', PRIOR_VPIP)
    pfr = opponent_model.get('pfr', PRIOR_PFR)
    postflop_aggr = opponent_model.get('postflop_aggr', PRIOR_POSTFLOP_AGGR)
    fold_to_raise = opponent_model.get('fold_to_raise', PRIOR_FOLD_TO_RAISE)

    if vpip > 0.65 and fold_to_raise < 0.35 and postflop_aggr < 0.28:
        return 'calling_station'
    if vpip < 0.40 and fold_to_raise > 0.55:
        return 'nit'
    if vpip > 0.60 and postflop_aggr > 0.45:
        return 'lag'
    if vpip > 0.45 and pfr > 0.25 and postflop_aggr > 0.30:
        return 'tag'

    return 'unknown'


def classify_opp_open_sizing(opponent_model):
    """Classify opponent's HISTORICAL preflop open-raise size tendency.

    Cross-over import from national_v14. Distinct from strategy's immediate
    raise-size bucketing and from classify_opponent_archetype (VPIP/aggr).
    Returns one of:
      'small'    avg_raise_bb <= 2.2 (wide range, exploitable)
      'standard' 2.2 < avg_raise_bb < 4.0
      'large'    4.0 <= avg_raise_bb < 6.0 (polarized, doesn't fold to 3bets)
      'xl'       avg_raise_bb >= 6.0
      'unknown'  insufficient samples or confidence.

    DEFAULT_AVG_RAISE_BB=2.6 is the prior; we need raise_samples>=4 to trust
    the empirical mean (Bayesian credibility: 4 samples vs prior_weight=4
    gives ~50% weight to observed data). The 'unknown' default is safe: it
    cannot misfire vs a brand-new opponent.
    """
    confidence = opponent_model.get('confidence', 0.0)
    samples = opponent_model.get('raise_samples', 0)
    avg_raise_bb = opponent_model.get('avg_raise_bb', DEFAULT_AVG_RAISE_BB)
    bucket = 'unknown'
    if confidence >= 0.15 and samples >= 4:
        if avg_raise_bb <= 2.2:
            bucket = 'small'
        elif avg_raise_bb >= 6.0:
            bucket = 'xl'
        elif avg_raise_bb >= 4.0:
            bucket = 'large'
        else:
            bucket = 'standard'
    sys.stderr.write(
        f"OPP_OPEN_SIZING bucket={bucket} avg_raise_bb={avg_raise_bb:.2f} "
        f"samples={samples} conf={confidence:.2f}\n"
    )
    return bucket


def smooth_rate(successes, total, prior_mean, prior_weight):
    return (successes + prior_mean * prior_weight) / (total + prior_weight)


def _street_polarity(sizes_bb_list):
    """Per-street betsize polarity on [-1, +1].

    Mirrors the aggregate postflop polarity computation: +1 = polarized
    (mix of large/small raise sizes), -1 = uniform sizing. Returns 0.0
    for streets with fewer than 3 observed raise sizes.
    """
    if len(sizes_bb_list) < 3:
        return 0.0
    mean_rz = sum(sizes_bb_list) / len(sizes_bb_list)
    var_rz = sum((x - mean_rz) ** 2 for x in sizes_bb_list) / len(sizes_bb_list)
    cv = (var_rz ** 0.5) / max(mean_rz, 0.1)
    return clamp((cv - 0.30) / 0.30, -1.0, 1.0)


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
    my_river_bets_vs_opp = 0       # times WE bet/raised on river
    opp_river_calls_vs_my_bet = 0  # times opponent CALLED our river bet
    pending_my_river_bet = False   # within-hand tracking
    my_jam_count = 0               # times WE moved all-in
    opp_folds_to_jam = 0           # opponent folds facing our all-in
    raise_sizes = []
    flop_bets = 0; turn_bets = 0; river_bets = 0
    flop_acts = 0; turn_acts = 0; river_acts = 0
    flop_raise_bb = []; turn_raise_bb = []; river_raise_bb = []
    flop_pot_frac = []; turn_pot_frac = []; river_pot_frac = []
    barrel_hands = 0; barrel_continue = 0
    postflop_allin_count = 0
    flop_allin_count = 0
    turn_allin_count = 0
    river_allin_count = 0
    opp_bet_flop = False; opp_bet_turn = False

    for req in hand_requests:
        if opponent_can_lock_win(req, my_id):
            continue

        opp_bet_flop = False
        opp_bet_turn = False
        pending_my_river_bet = False
        pending_my_jam = False

        _dealer = req.get('dealer_id', 0)
        _round_bets = {0: {_dealer: SMALL_BLIND, 1 - _dealer: BIG_BLIND},
                       1: {0: 0, 1: 0}, 2: {0: 0, 1: 0}, 3: {0: 0, 1: 0}}
        _hand_allin_seen = False

        history = req.get("history", [])
        if not history:
            continue

        saw_opponent_preflop_action = False
        pending_my_pressure = False

        for record in history:
            pid = record['player_id']
            action_type = record['action_type']
            action = record['action']
            round_idx = record['round']

            if action_type == 'raise' and not _hand_allin_seen:
                _cur_pot = sum(_round_bets[r][p] for r in range(round_idx + 1) for p in (0, 1))
                _round_bets[round_idx][pid] = action
                if pid != my_id and round_idx >= 1:
                    _pf = action / max(1, _cur_pot)
                    if round_idx == 1:
                        flop_pot_frac.append(_pf)
                    elif round_idx == 2:
                        turn_pot_frac.append(_pf)
                    elif round_idx == 3:
                        river_pot_frac.append(_pf)
            elif action_type in ('call', 'check'):
                _round_bets[round_idx][pid] = max(_round_bets[round_idx].values())
            elif action_type == 'allin':
                _hand_allin_seen = True
                _round_bets[round_idx][pid] = max(_round_bets[round_idx].values()) + 5000

            if pid == my_id:
                if round_idx == 3 and action_type in ('raise', 'allin'):
                    my_river_bets_vs_opp += 1
                    pending_my_river_bet = True
                if action_type == 'allin':
                    pending_my_jam = True
                    continue
                if action_type == 'raise':
                    pending_my_pressure = True
                    continue
                continue

            # opponent action from here
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
                if action_type == "allin":
                    postflop_allin_count += 1
                if action_type == "check":
                    postflop_checks += 1
                if round_idx == 1:
                    flop_acts += 1
                    if action_type in ('raise', 'allin'):
                        flop_bets += 1
                        opp_bet_flop = True
                    if action_type == 'raise':
                        flop_raise_bb.append(action / BIG_BLIND)
                    if action_type == 'allin':
                        flop_allin_count += 1
                elif round_idx == 2:
                    turn_acts += 1
                    if action_type in ('raise', 'allin'):
                        turn_bets += 1
                        opp_bet_turn = True
                    if action_type == 'raise':
                        turn_raise_bb.append(action / BIG_BLIND)
                    if action_type == 'allin':
                        turn_allin_count += 1
                elif round_idx == 3:
                    river_acts += 1
                    if action_type in ('raise', 'allin'):
                        river_bets += 1
                    if action_type == 'raise':
                        river_raise_bb.append(action / BIG_BLIND)
                    if action_type == 'allin':
                        river_allin_count += 1

            if action_type == "raise":
                raise_sizes.append(action / BIG_BLIND)

            if pending_my_jam:
                my_jam_count += 1
                if action_type == 'fold':
                    opp_folds_to_jam += 1
                pending_my_jam = False
            if pending_my_river_bet and round_idx == 3:
                if action_type == 'call':
                    opp_river_calls_vs_my_bet += 1
                pending_my_river_bet = False
            if pending_my_pressure:
                fold_to_raise_opportunities += 1
                if action_type == 'fold':
                    fold_to_raise += 1
                pending_my_pressure = False

        if opp_bet_flop:
            barrel_hands += 1
            if opp_bet_turn:
                barrel_continue += 1

    confidence = clamp((total_actions - CONFIDENCE_OFFSET) / CONFIDENCE_SCALE, 0.0, 1.0)
    _PF_PRIOR = {1: 0.55, 2: 0.65, 3: 0.75}
    _PF_WEIGHT = 4.0
    flop_pf = (sum(flop_pot_frac) + _PF_PRIOR[1]*_PF_WEIGHT) / (len(flop_pot_frac)+_PF_WEIGHT) if flop_pot_frac else _PF_PRIOR[1]
    turn_pf = (sum(turn_pot_frac) + _PF_PRIOR[2]*_PF_WEIGHT) / (len(turn_pot_frac)+_PF_WEIGHT) if turn_pot_frac else _PF_PRIOR[2]
    river_pf = (sum(river_pot_frac) + _PF_PRIOR[3]*_PF_WEIGHT) / (len(river_pot_frac)+_PF_WEIGHT) if river_pot_frac else _PF_PRIOR[3]
    sys.stderr.write(f'POT_FRAC flop={flop_pf:.3f}({len(flop_pot_frac)}) turn={turn_pf:.3f}({len(turn_pot_frac)}) river={river_pf:.3f}({len(river_pot_frac)})\n')
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else DEFAULT_AVG_RAISE_BB
    river_overcall_freq = smooth_rate(
        opp_river_calls_vs_my_bet, my_river_bets_vs_opp,
        PRIOR_RIVER_OVERCALL, PRIOR_RIVER_OVERCALL_WEIGHT,
    )

    sys.stderr.write(
        f"OPP_RIVER_OVERCALL freq={river_overcall_freq:.3f} "
        f"samples={my_river_bets_vs_opp} calls={opp_river_calls_vs_my_bet}\n"
    )

    fold_to_jam_rate = smooth_rate(
        opp_folds_to_jam, my_jam_count,
        PRIOR_FOLD_TO_JAM, PRIOR_FOLD_TO_JAM_WEIGHT,
    )

    sys.stderr.write(
        f'FOLD_TO_JAM_RATE rate={fold_to_jam_rate:.3f} samples={my_jam_count} folds={opp_folds_to_jam}\n'
    )

    # betsize_polarity: continuous [-1,+1]. +1 = polarized (mix of large/small raises), -1 = uniform.
    # Computed from coefficient of variation of postflop raise sizes (BB multiples).
    postflop_raise_sizes_bb = flop_raise_bb + turn_raise_bb + river_raise_bb
    if len(postflop_raise_sizes_bb) >= 4:
        mean_rz = sum(postflop_raise_sizes_bb) / len(postflop_raise_sizes_bb)
        var_rz = sum((x - mean_rz) ** 2 for x in postflop_raise_sizes_bb) / len(postflop_raise_sizes_bb)
        cv = (var_rz ** 0.5) / max(mean_rz, 0.1)
        polarity_observed = clamp((cv - 0.30) / 0.30, -1.0, 1.0)
    else:
        mean_rz = DEFAULT_AVG_RAISE_BB
        var_rz = 0.0
        cv = 0.0
        polarity_observed = 0.0
    polarity_weight = clamp(len(postflop_raise_sizes_bb) / 8.0, 0.0, 1.0)
    betsize_polarity = polarity_observed * polarity_weight
    shove_rate = smooth_rate(
        postflop_allin_count, max(1, postflop_aggressive),
        PRIOR_SHOVE_RATE, PRIOR_SHOVE_RATE_WEIGHT,
    )
    # Per-street shove rates: fraction of aggressive actions that are all-in.
    # Denominator base is the per-street aggressive-action count via max(1, ...).
    flop_shove_count_total = max(1, flop_bets)
    turn_shove_count_total = max(1, turn_bets)
    river_shove_count_total = max(1, river_bets)
    flop_shove_rate = smooth_rate(
        flop_allin_count, flop_shove_count_total,
        PRIOR_SHOVE_RATE, PRIOR_SHOVE_RATE_WEIGHT,
    )
    turn_shove_rate = smooth_rate(
        turn_allin_count, turn_shove_count_total,
        PRIOR_SHOVE_RATE, PRIOR_SHOVE_RATE_WEIGHT,
    )
    river_shove_rate = smooth_rate(
        river_allin_count, river_shove_count_total,
        PRIOR_SHOVE_RATE, PRIOR_SHOVE_RATE_WEIGHT,
    )
    # Per-street betsize polarity: continuous [-1,+1], 0.0 for <3 samples.
    flop_polarity = _street_polarity(flop_raise_bb)
    turn_polarity = _street_polarity(turn_raise_bb)
    river_polarity = _street_polarity(river_raise_bb)
    sys.stderr.write(
        f"BETSIZE_POLARITY polarity={betsize_polarity:+.3f} "
        f"shove_rate={shove_rate:.3f} "
        f"flop_shove={flop_shove_rate:.3f} turn_shove={turn_shove_rate:.3f} river_shove={river_shove_rate:.3f} "
        f"flop_pol={flop_polarity:+.3f} turn_pol={turn_polarity:+.3f} river_pol={river_polarity:+.3f} "
        f"postflop_raise_samples={len(postflop_raise_sizes_bb)} "
        f"postflop_shoves={postflop_allin_count} "
        f"postflop_aggr={postflop_aggressive} cv={cv:.3f}\n"
    )

    return {
        "confidence": confidence,
        "vpip": smooth_rate(voluntary_preflop, preflop_opportunities, PRIOR_VPIP, PRIOR_VPID_WEIGHT),
        "pfr": smooth_rate(preflop_raise, preflop_opportunities, PRIOR_PFR, PRIOR_PFR_WEIGHT),
        "allin_rate": smooth_rate(allin_actions, total_actions, PRIOR_ALLIN_RATE, PRIOR_ALLIN_WEIGHT),
        "postflop_aggr": smooth_rate(postflop_aggressive, postflop_actions, PRIOR_POSTFLOP_AGGR, PRIOR_POSTFLOP_AGGR_WEIGHT),
        "postflop_check_rate": smooth_rate(postflop_checks, postflop_actions, PRIOR_POSTFLOP_CHECK, PRIOR_POSTFLOP_CHECK_WEIGHT),
        "fold_to_raise": smooth_rate(fold_to_raise, fold_to_raise_opportunities, PRIOR_FOLD_TO_RAISE, PRIOR_FTR_WEIGHT),
        "aggression": smooth_rate(aggressive_actions, total_actions, PRIOR_AGGRESSION, PRIOR_AGGRESSION_WEIGHT),
        "avg_raise_bb": avg_raise_bb,
        # Crossover (v14 import): expose raise_samples so classify_opp_open_sizing
        # can require >=4 confirmed open-raises before trusting the empirical mean.
        "raise_samples": len(raise_sizes),
        "flop_aggr": smooth_rate(flop_bets, flop_acts, PRIOR_FLOP_AGGR, PRIOR_FLOP_AGGR_WEIGHT),
        "turn_aggr": smooth_rate(turn_bets, turn_acts, PRIOR_TURN_AGGR, PRIOR_TURN_AGGR_WEIGHT),
        "river_aggr": smooth_rate(river_bets, river_acts, PRIOR_RIVER_AGGR, PRIOR_RIVER_AGGR_WEIGHT),
        "avg_flop_raise_bb": sum(flop_raise_bb)/len(flop_raise_bb) if flop_raise_bb else DEFAULT_FLOP_RAISE_BB,
        "avg_turn_raise_bb": sum(turn_raise_bb)/len(turn_raise_bb) if turn_raise_bb else DEFAULT_TURN_RAISE_BB,
        "avg_river_raise_bb": sum(river_raise_bb)/len(river_raise_bb) if river_raise_bb else DEFAULT_RIVER_RAISE_BB,
        "barrel_freq": smooth_rate(barrel_continue, barrel_hands, PRIOR_BARREL_FREQ, PRIOR_BARREL_WEIGHT),
        "river_overcall_freq": river_overcall_freq,
        "river_overcall_samples": my_river_bets_vs_opp,
        "fold_to_jam_rate": fold_to_jam_rate,
        "fold_to_jam_samples": my_jam_count,
        "betsize_polarity": betsize_polarity,
        "shove_rate": shove_rate,
        "postflop_raise_samples": len(postflop_raise_sizes_bb),
        "postflop_shove_samples": postflop_allin_count,
        "flop_shove_rate": flop_shove_rate,
        "turn_shove_rate": turn_shove_rate,
        "river_shove_rate": river_shove_rate,
        "flop_shove_samples": flop_allin_count,
        "turn_shove_samples": turn_allin_count,
        "river_shove_samples": river_allin_count,
        "flop_polarity": flop_polarity,
        "turn_polarity": turn_polarity,
        "river_polarity": river_polarity,
        "flop_pot_frac": flop_pf,
        "turn_pot_frac": turn_pf,
        "river_pot_frac": river_pf,
        "flop_pot_frac_samples": len(flop_pot_frac),
        "turn_pot_frac_samples": len(turn_pot_frac),
        "river_pot_frac_samples": len(river_pot_frac),
    }


def analyze_current_spot(req, state):
    my_id = req["my_id"]
    opponent_id = next_player(my_id, 1)
    dealer_id = req["dealer_id"]
    # Heads-up contract: dealer_id IS the small blind, big blind is the other seat.
    sb = dealer_id
    bb = 1 - dealer_id
    history = req["history"]

    info = {
        "my_is_sb": my_id == sb,
        "my_is_bb": my_id == bb,
        # Crossover (v14 import): heads-up positional semantics — preflop the
        # BB (non-dealer) acts last and holds position; postflop the SB
        # (dealer) acts last and holds position. v6 incorrectly marked BB as
        # always positional, which distorted position-aware postflop branches.
        "has_position": (my_id == bb) if state["round"] == 0 else (my_id == sb),
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


def _bb_defense_pressure_profile(opponent_model):
    """Continuous preflop BB-defense deltas driven by opponent PFR / fold_to_raise / VPIP.

    Crossover import from national_v71 (Beta). Targets aggressive/non-folding
    openers and loose limpers by tightening calls / cutting bluff frequency /
    tightening iso thresholds when the opponent profile demands it. The
    STANDARD arm keeps small unconditional adjustments so the detector is never
    inert at population priors. Telemetry is unconditional so fire-rate is
    honest.
    """
    pfr = opponent_model.get('pfr', PRIOR_PFR)
    ftr = opponent_model.get('fold_to_raise', PRIOR_FOLD_TO_RAISE)
    vpip = opponent_model.get('vpip', PRIOR_VPIP)
    conf = opponent_model.get('confidence', 0.0)

    def _dev(signal, prior, span=0.15):
        return clamp((signal - prior) / span, -1.0, 1.0)

    pfr_dev = _dev(pfr, PRIOR_PFR)            # +1 = aggressive opener
    ftr_dev = _dev(ftr, PRIOR_FOLD_TO_RAISE)  # +1 = folds to raises, -1 = won't fold
    vpip_dev = _dev(vpip, PRIOR_VPIP)         # +1 = loose limper

    # STANDARD arm: small unconditional preflop tightening so the detector
    # fires even for the population-default bucket.
    std_call = 0.020
    std_bluff = -0.030
    std_iso = 0.015

    # DEVIATION arms (confidence-weighted, continuous, one-directional via max(0,.)):
    #   aggressive non-folding opener (high pfr, low ftr) -> tighten calls, cut bluffs
    #   limp-caller (high vpip, low ftr) -> tighter iso
    dev_call  = conf * (0.040 * max(0.0, pfr_dev) + 0.030 * max(0.0, -ftr_dev))
    dev_bluff = conf * (-0.080 * max(0.0, -ftr_dev))
    dev_iso   = conf * (0.030 * max(0.0, vpip_dev) * max(0.0, -ftr_dev))

    call_delta       = std_call  + dev_call
    bluff_freq_delta = std_bluff + dev_bluff
    iso_delta        = std_iso   + dev_iso

    deviation_mag = abs(dev_call) + abs(dev_bluff) + abs(dev_iso)
    reason = 'deviation_fired' if deviation_mag > 0.001 else 'standard_arm'
    sys.stderr.write(
        f'BB_DEFENSE_PRESSURE call_d={call_delta:+.4f} bluff_freq_d={bluff_freq_delta:+.4f} '
        f'iso_d={iso_delta:+.4f} pfr={pfr:.3f} ftr={ftr:.3f} vpip={vpip:.3f} '
        f'conf={conf:.2f} reason={reason}\n'
    )
    return {'call_delta': call_delta, 'bluff_freq_delta': bluff_freq_delta, 'iso_delta': iso_delta}


def _sb_open_pressure_profile(opponent_model):
    """Continuous preflop SB-open threshold delta driven by opponent BB-defense stats.

    Structural counterpart to _bb_defense_pressure_profile. The SB-open SIZING
    (strategy.py SB_OPEN_SIZE block) already adapts to fold_to_raise; this adds
    the missing THRESHOLD adaptation so SB also opens WIDER vs confirmed folders
    and TIGHTER vs aggressive 3-bettors. STANDARD arm fires unconditionally at
    population priors (M5/M6 non-inertia rule).
    """
    pfr = opponent_model.get('pfr', PRIOR_PFR)
    ftr = opponent_model.get('fold_to_raise', PRIOR_FOLD_TO_RAISE)
    vpip = opponent_model.get('vpip', PRIOR_VPIP)
    conf = opponent_model.get('confidence', 0.0)

    def _dev(signal, prior, span=0.15):
        return clamp((signal - prior) / span, -1.0, 1.0)

    pfr_dev = _dev(pfr, PRIOR_PFR)            # +1 = aggressive 3-bettor BB
    ftr_dev = _dev(ftr, PRIOR_FOLD_TO_RAISE)  # +1 = folds to raises (steal target)
    vpip_dev = _dev(vpip, PRIOR_VPIP)         # +1 = loose limper/caller

    # STANDARD arm: small unconditional tightening at population priors so the
    # detector is never inert (smooth_rate pulls ftr toward 0.44 even for a true
    # 50%-folder at n=30 -> 0.491, so a tiny std delta keeps the surface live).
    std_open = 0.010

    # DEVIATION arms (confidence-weighted, continuous, one-directional via max(0,.)):
    #   confirmed folder BB (high ftr)          -> open WIDER (more steals)
    #   aggressive 3-bettor BB (high pfr)        -> open TIGHTER (avoid 3bets)
    #   non-folding limp-caller (low ftr+vpip)   -> open TIGHTER (iso > open)
    dev_open = conf * (
        -0.040 * max(0.0, ftr_dev)
        + 0.050 * max(0.0, pfr_dev)
        + 0.030 * max(0.0, -ftr_dev) * max(0.0, vpip_dev)
    )

    open_delta = std_open + dev_open

    deviation_mag = abs(dev_open)
    reason = 'deviation_fired' if deviation_mag > 0.001 else 'standard_arm'
    sys.stderr.write(
        f'SB_OPEN_PRESSURE open_d={open_delta:+.4f} pfr={pfr:.3f} ftr={ftr:.3f} '
        f'vpip={vpip:.3f} conf={conf:.2f} reason={reason}\n'
    )
    return {'open_delta': open_delta}


if __name__ == '__main__':
    # M5/M6 self-test: verify non-zero delta at LIVE POOL DEFAULTS.
    _std = _sb_open_pressure_profile({'pfr': PRIOR_PFR, 'fold_to_raise': PRIOR_FOLD_TO_RAISE, 'vpip': PRIOR_VPIP, 'confidence': 0.5})
    assert _std['open_delta'] != 0, f'INERT at priors: {_std}'
    _folder = _sb_open_pressure_profile({'pfr': PRIOR_PFR, 'fold_to_raise': 0.62, 'vpip': PRIOR_VPIP, 'confidence': 0.5})
    assert _folder['open_delta'] < _std['open_delta'], f'folder should open wider: {_folder}'
    _threebet = _sb_open_pressure_profile({'pfr': 0.45, 'fold_to_raise': 0.30, 'vpip': 0.50, 'confidence': 0.5})
    assert _threebet['open_delta'] > _std['open_delta'], f'3-bettor should tighten: {_threebet}'
    print(f'self-test ok: std={_std["open_delta"]:+.4f} folder={_folder["open_delta"]:+.4f} threebet={_threebet["open_delta"]:+.4f}')
