from constants import (
    N_PLAYERS, INITIAL_CHIPS, SMALL_BLIND, BIG_BLIND, TOTAL_HANDS,
    TRASH_STRENGTH_THRESHOLD,
)
from card_utils import card_suit, card_number, next_player, clamp


def estimate_preflop_strength(my_cards):
    r1 = card_number(my_cards[0])
    r2 = card_number(my_cards[1])
    s1 = card_suit(my_cards[0])
    s2 = card_suit(my_cards[1])
    high = max(r1, r2)
    low = min(r1, r2)
    gap = high - low
    suited = s1 == s2
    pair = r1 == r2

    score = 0.0
    score += (high - 2) / 16.0
    score += (low - 2) / 28.0

    if pair:
        score += 0.25 + (high - 2) / 30.0
    else:
        if suited:
            score += 0.06
        if gap == 1:
            score += 0.06
        elif gap == 2:
            score += 0.03
        elif gap >= 4:
            score -= 0.04

    if high == 14:
        score += 0.04
        if low >= 10:
            score += 0.04

    # ── [v43] Tier caps: prevent dominated hands from clamping at 1.0 ────────
    # v42 mis-scores KQo/KJo/QJo/AJo/ATo/KTo/JTo AND small/mid pairs (22-99)
    # at 1.0 (same as AA/AKs), inflating big-pot stack-offs vs tight openers
    # whose raising range is AK/AQ/JJ+ (H2H: v42 vs v1/v3/v11/v16 = 0.20-0.30
    # WR, replay evidence ev_4a9bbf66/ev_2c44d44a tags high_aggression+
    # big_pot_losses). Cap dominated buckets so the downstream thresholds
    # BB_VALUE_3BET(0.58)/BB_ISO(0.57)/SB_FACING_ALLIN_CALL(0.55)/
    # SB_PREMIUM_4BET(0.78) exclude them. Premiums (AK/AQ high==14 low>=12)
    # and AA/KK/QQ remain above the caps naturally.
    _cap_fired = ''
    if pair:
        # Pairs: 22=0.45 ... 55=0.59 ... 99=0.77 ... AA=1.00 (differentiate
        # the over-clamped 22-99 bucket; previously all hit 1.0).
        _pair_cap = 0.45 + (high - 2) / 22.0  # 0.45 (22) -> 1.00 (AA)
        if score > _pair_cap:
            score = _pair_cap
            _cap_fired = 'pair'
    elif not suited:
        # Offsuit broadway (high>=11, low>=10). EXEMPT premium AKo/AQo
        # (high==14 AND low>=12) which dominate rather than get dominated.
        if high >= 11 and low >= 10 and not (high == 14 and low >= 12):
            # KQo~0.55, AJo~0.56, KJo~0.54, QJo~0.53, ATo~0.55, KTo~0.52,
            # JTo~0.52. Below BB_VALUE_3BET=0.58 and BB_ISO=0.57.
            _ob_cap = 0.50 + (high - 11) * 0.014 + (low - 10) * 0.010
            if score > _ob_cap:
                score = _ob_cap
                _cap_fired = 'offsuit_broadway'
    else:
        # Suited broadway (high>=11, low>=10). EXEMPT premium AKs/AQs.
        if high >= 11 and low >= 10 and not (high == 14 and low >= 12):
            # KQs~0.78, AJs~0.79, ATs~0.78, KJs~0.77, QJs~0.76, JTs~0.75.
            _sb_cap = 0.74 + (high - 11) * 0.012 + (low - 10) * 0.008
            if score > _sb_cap:
                score = _sb_cap
                _cap_fired = 'suited_broadway'
    if _cap_fired:
        import sys as _sys
        _sys.stderr.write(
            f'PREFLOP_TIER_CAP cap={_cap_fired} high={high} low={low} '
            f'suited={int(suited)} pair={int(pair)} score={score:.3f}\n'
        )

    return clamp(score, 0.0, 1.0)


def preflop_hand_profile(my_cards):
    ranks = sorted((card_number(card) for card in my_cards), reverse=True)
    suited = card_suit(my_cards[0]) == card_suit(my_cards[1])
    pair = ranks[0] == ranks[1]
    return {
        "high": ranks[0],
        "low": ranks[1],
        "suited": suited,
        "pair": pair,
    }


def is_preflop_3bet_candidate(my_cards):
    profile = preflop_hand_profile(my_cards)
    if profile["pair"]:
        return True
    return profile["high"] == 14 and profile["low"] >= 12


def is_preflop_trash_hand(my_cards, preflop_strength=None):
    profile = preflop_hand_profile(my_cards)
    if profile["pair"]:
        return False

    high = profile["high"]
    low = profile["low"]
    gap = high - low
    suited = profile["suited"]
    strength = estimate_preflop_strength(my_cards) if preflop_strength is None else preflop_strength

    if high == 14:
        return False
    if suited and gap <= 2 and high >= 6:
        return False
    if high >= 11 and low >= 8 and gap <= 4:
        return False

    if strength <= TRASH_STRENGTH_THRESHOLD:
        return True
    if not suited and high <= 10 and low <= 5 and gap >= 3:
        return True
    if not suited and high <= 12 and low <= 4 and gap >= 5:
        return True
    if suited and high <= 9 and low <= 4 and gap >= 4:
        return True
    return False


def preflop_domination_penalty(my_cards, opponent_model=None, opp_archetype='unknown'):
    """Strength penalty for offsuit broadway combos prone to domination in big pots.

    Hands like KQo/KJo/QJo/ATo enter big pots, flop dominated top-pair-good-kicker,
    and stack off to AK/AQ/AJ overpairs and sets. Returns a penalty subtracted
    from preflop_strength in the bb_vs_raise / sb_vs_reraise call decisions so
    these hands fold preflop instead of calling. This is the upstream range
    gate (CROSS_GEN_PIVOT) — NOT a postflop fold-margin tweak.

    [v42 crossover mutation] Opponent-aware gating per experience_pool
    OPPONENT_MODELING sanction ("preflop fold-gates need ... opp width
    (pfr<=0.22)"). The domination penalty only applies vs TIGHT openers whose
    raising range is dominated by AK/AQ/AJ/overpairs (pfr <= 0.30) — exactly
    the elite cluster (v17/v7) where v37's protection earns its rating edge.
    vs LOOSE openers (calling_station archetype, or pfr > 0.30, or high vpip),
    KQo/QJo/ATo are PROFITABLE calls because they dominate the wide range's
    Kx/Qx/weak-ace portion. Skipping the penalty there recovers v11's
    weak-opponent coverage (v9/v12/v29) without touching v37's elite defense.
    When opponent_model is None (legacy caller), the penalty fires as before
    (conservative default preserves the v37 baseline).
    """
    profile = preflop_hand_profile(my_cards)
    if profile['pair'] or profile['suited']:
        return 0.0
    high, low = profile['high'], profile['low']
    if high < 11 or low < 10:
        return 0.0
    if high == 14 and low >= 12:
        return 0.0
    # Base penalty for offsuit broadway domination-prone combos.
    _base_penalty = 0.035
    # Opponent-aware gate: skip penalty vs loose/wide openers where KQo/QJo/ATo
    # are profitable calls (they dominate the wide range's Kx/Qx/weak-ace).
    if opponent_model is not None:
        if opp_archetype == 'calling_station':
            return 0.0
        pfr = opponent_model.get('pfr', 0.28)
        vpip = opponent_model.get('vpip', 0.58)
        if pfr > 0.30 or vpip > 0.62:
            return 0.0
    return _base_penalty


def get_hand_index(req):
    for key in ("hand", "hand_id", "hand_index", "round_id", "round_index", "game_id", "game_index"):
        if key in req:
            try:
                return int(req[key])
            except (TypeError, ValueError):
                pass
    return None


def get_remaining_hands(req):
    if "hand" in req and "max_hand" in req:
        try:
            return max(0, int(req["max_hand"]) - int(req["hand"]))
        except (TypeError, ValueError):
            pass

    direct_keys = (
        "remaining_hands",
        "remain_hands",
        "hands_left",
        "left_hands",
        "remaining_rounds",
        "remain_rounds",
        "rounds_left",
        "left_rounds",
    )
    for key in direct_keys:
        if key in req:
            try:
                value = int(req[key])
                if value >= 0:
                    return value
            except (TypeError, ValueError):
                pass

    hand_idx = get_hand_index(req)
    if hand_idx is not None:
        candidates = [TOTAL_HANDS - hand_idx, TOTAL_HANDS - hand_idx + 1]
        candidates = [value for value in candidates if value >= 0]
        if candidates:
            return max(candidates)
    return None


def infer_remaining_hands_from_requests(requests):
    if not requests:
        return TOTAL_HANDS

    direct = get_remaining_hands(requests[-1])
    if direct is not None:
        return direct

    hand_indices = [get_hand_index(req) for req in requests]
    hand_indices = [value for value in hand_indices if value is not None]
    if hand_indices:
        return max(0, TOTAL_HANDS - max(hand_indices))

    started_hands = 0
    for req in requests:
        if len(req.get("public_cards", [])) == 0 and len(req.get("history", [])) == 0:
            started_hands += 1
    if started_hands <= 0:
        return TOTAL_HANDS
    return max(0, TOTAL_HANDS - started_hands + 1)


def reconstruct_state(req):
    my_id = req["my_id"]
    dealer_id = req["dealer_id"]

    stacks = [INITIAL_CHIPS] * N_PLAYERS
    committed = [0] * N_PLAYERS
    # Heads-up contract: dealer_id is the small blind; big blind is 1 - dealer_id.
    sb = dealer_id
    bb = 1 - dealer_id

    stacks[sb] -= SMALL_BLIND
    stacks[bb] -= BIG_BLIND
    committed[sb] += SMALL_BLIND
    committed[bb] += BIG_BLIND

    current_round = 0
    round_bet = BIG_BLIND
    last_raise_to = BIG_BLIND
    round_contrib = [0] * N_PLAYERS
    round_contrib[sb] = SMALL_BLIND
    round_contrib[bb] = BIG_BLIND
    alive = [True] * N_PLAYERS
    allin = [False] * N_PLAYERS

    for record in req["history"]:
        record_round = record["round"]
        pid = record["player_id"]
        action = record["action"]
        action_type = record["action_type"]

        if record_round != current_round:
            current_round = record_round
            round_bet = 0
            last_raise_to = SMALL_BLIND
            round_contrib = [0] * N_PLAYERS

        if action_type == "fold":
            alive[pid] = False
            continue

        if not alive[pid] or allin[pid]:
            continue

        if action_type == "allin":
            add = stacks[pid]
            stacks[pid] = 0
            committed[pid] += add
            round_contrib[pid] += add
            allin[pid] = True
            round_bet = max(round_bet, round_contrib[pid])
            continue

        if action_type in ("call", "check"):
            need = max(0, round_bet - round_contrib[pid])
            need = min(need, stacks[pid])
            stacks[pid] -= need
            committed[pid] += need
            round_contrib[pid] += need
        elif action_type == "raise":
            target = max(round_bet, action)
            add = max(0, min(target - round_contrib[pid], stacks[pid]))
            stacks[pid] -= add
            committed[pid] += add
            round_contrib[pid] += add
            round_bet = max(round_bet, round_contrib[pid])
            last_raise_to = max(last_raise_to, target)

    public_cards = len(req["public_cards"])
    round_idx = 0 if public_cards == 0 else 1 if public_cards == 3 else 2 if public_cards == 4 else 3

    if current_round != round_idx:
        round_bet = 0
        last_raise_to = SMALL_BLIND
        round_contrib = [0] * N_PLAYERS

    player_bets = [0] * N_PLAYERS
    for pid in range(N_PLAYERS):
        if not alive[pid]:
            player_bets[pid] = -1
        elif allin[pid]:
            player_bets[pid] = -2
        else:
            player_bets[pid] = round_contrib[pid]

    opponent_id = next_player(my_id, 1)
    opponent_allin = allin[opponent_id] and alive[opponent_id]
    my_round_bet = 0 if player_bets[my_id] < 0 else player_bets[my_id]
    to_call = max(0, round_bet - my_round_bet)
    min_raise_action = max(0, 2 * last_raise_to + 1 - my_round_bet)
    allin_call_amount = max(
        0,
        min(committed[opponent_id], committed[my_id] + stacks[my_id]) - committed[my_id],
    )

    return {
        "round": round_idx,
        "round_bet": round_bet,
        "round_raise": last_raise_to,
        "judge_round_raise": last_raise_to,
        "min_raise_action": min_raise_action,
        "round_contrib": round_contrib,
        "player_bets": player_bets,
        "stacks": stacks,
        "committed": committed,
        "pot": committed[0] + committed[1],
        "to_call": to_call,
        "opponent_allin": opponent_allin,
        "allin_call_amount": allin_call_amount,
        "my_round_bet": my_round_bet,
    }


def forced_fold_loss_bound(req, state, my_id, remaining_hands):
    if remaining_hands is None or remaining_hands <= 0:
        return None

    loss = state["committed"][my_id]
    current_dealer = req["dealer_id"]
    for offset in range(1, remaining_hands):
        future_dealer = next_player(current_dealer, offset)
        # Heads-up contract: dealer is the small blind; big blind is the other seat.
        future_sb = future_dealer
        future_bb = 1 - future_dealer
        if my_id == future_sb:
            loss += SMALL_BLIND
        elif my_id == future_bb:
            loss += BIG_BLIND
    return loss


def collect_latest_requests_by_hand(requests):
    latest = {}
    fallback_hand = TOTAL_HANDS
    for req in requests:
        hand = get_hand_index(req)
        if hand is None:
            hand = fallback_hand
            fallback_hand += 1
        prev = latest.get(hand)
        if prev is None or len(req.get("history", [])) >= len(prev.get("history", [])):
            latest[hand] = req
    return [latest[hand] for hand in sorted(latest)]


if __name__ == '__main__':
    # card integer = (rank-2)*4 + suit; suits 0-3
    def _c(rank, suit=0): return (rank - 2) * 4 + suit
    KQo = [_c(13), _c(12, 1)]; KQs = [_c(13), _c(12)]
    ATo = [_c(14), _c(10, 1)]; AKo = [_c(14), _c(13, 1)]
    JTs = [_c(11), _c(10)]; T9o = [_c(10), _c(9, 1)]
    assert preflop_domination_penalty(KQo) == 0.035, 'KQo domination-prone'
    assert preflop_domination_penalty(KQs) == 0.0, 'suited exempt'
    assert preflop_domination_penalty(ATo) == 0.035, 'ATo domination-prone'
    assert preflop_domination_penalty(AKo) == 0.0, 'AKo premium exempt'
    assert preflop_domination_penalty(JTs) == 0.0, 'suited connector exempt'
    assert preflop_domination_penalty(T9o) == 0.0, 'non-broadway exempt'
    print('preflop_domination_penalty self-test PASSED (6/6)')

    # [v43] estimate_preflop_strength tier-cap self-test
    def _ps(rank1, suit1, rank2, suit2):
        return estimate_preflop_strength([(rank1-2)*4+suit1, (rank2-2)*4+suit2])
    # Premium pairs and broadway MUST stay high (above 0.78 premium 4bet gate)
    assert _ps(14,0,14,1) >= 0.95, 'AA must stay premium'
    assert _ps(13,0,13,1) >= 0.90, 'KK must stay premium'
    assert _ps(14,0,13,0) >= 0.95, 'AKs must stay premium'
    assert _ps(14,0,13,1) >= 0.95, 'AKo must stay premium'
    assert _ps(14,0,12,0) >= 0.90, 'AQs premium exempt'
    assert _ps(14,0,12,1) >= 0.90, 'AQo premium exempt'
    # Dominated offsuit broadway MUST fall below value-3bet (0.58) and allin-call (0.55)
    _kqo = _ps(13,0,12,1);  assert _kqo < 0.58, f'KQo cap failed: {_kqo}'
    _ajo = _ps(14,0,11,1);  assert _ajo < 0.58, f'AJo cap failed: {_ajo}'
    _ato = _ps(14,0,10,1);  assert _ato < 0.58, f'ATo cap failed: {_ato}'
    _kjo = _ps(13,0,11,1);  assert _kjo < 0.58, f'KJo cap failed: {_kjo}'
    _qjo = _ps(12,0,11,1);  assert _qjo < 0.58, f'QJo cap failed: {_qjo}'
    _kto = _ps(13,0,10,1);  assert _kto < 0.58, f'KTo cap failed: {_kto}'
    # Suited broadway capped but still strong (above iso 0.57, below premium 4bet 0.78)
    _kqs = _ps(13,0,12,0);  assert _kqs < 0.82, f'KQs cap failed: {_kqs}'
    _ajs = _ps(14,0,11,0);  assert _ajs < 0.82, f'AJs cap failed: {_ajs}'
    # Pair differentiation: 22 must be well below AA (was both 1.0 before fix)
    _p22 = _ps(2,0,2,1);    assert _p22 <= 0.50, f'22 cap failed: {_p22}'
    _p99 = _ps(9,0,9,1);    assert _p99 <= 0.80, f'99 cap failed: {_p99}'
    _p99_hi = _ps(9,0,9,1); assert _p99_hi > _p22, '99 must outrank 22'
    # Trash hands unaffected
    _72o = _ps(7,0,2,1);    assert _72o < 0.40, f'72o should remain trash: {_72o}'
    print('estimate_preflop_strength tier-cap self-test PASSED')
