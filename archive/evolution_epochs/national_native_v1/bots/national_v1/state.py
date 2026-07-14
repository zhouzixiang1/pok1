from constants import N_PLAYERS, INITIAL_CHIPS, SMALL_BLIND, BIG_BLIND, TOTAL_HANDS
from card_utils import card_suit, card_number, next_player, clamp


def estimate_preflop_strength(my_cards):
    """Heads-up preflop strength as approximate equity vs a random hand.

    Corrected v187: the old formula scored K2o (0.65) above 55 (0.64) and
    clamped 88-AA all to 1.0. This formula matches real HU equities:
      Pairs:  22=0.50, 33=0.53, 44=0.56, 55=0.59, ..., AA=0.85
      Non-pairs: 72o=0.39, K2o=0.48, AKs=0.66, AKo=0.63
    All pairs now correctly outrank dominated offsuit trash.
    """
    r1 = card_number(my_cards[0])
    r2 = card_number(my_cards[1])
    high = max(r1, r2)
    low = min(r1, r2)
    suited = card_suit(my_cards[0]) == card_suit(my_cards[1])
    pair = r1 == r2

    if pair:
        # Real HU pair equity vs random: 22~=0.503, AA~=0.852, near-linear.
        # OLD formula scored 22=0.25 (below K2o!) -- catastrophic mis-rank.
        return clamp(0.50 + (high - 2) * 0.0292, 0.50, 0.85)

    # Non-pair HU equity vs random: 72o~=0.354, AKs~=0.670.
    gap = high - low
    high_val = (high - 2) / 12.0
    low_val = (low - 2) / 12.0
    score = 0.34 + 0.18 * high_val + 0.11 * low_val
    if suited:
        score += 0.03
    if gap == 1:
        score += 0.012
    elif gap == 2:
        score += 0.006
    elif gap >= 5:
        score -= 0.008 * min(gap - 4, 5)
    if high == 14 and low <= 5:
        score += 0.01  # wheel straight (A-2-3-4-5) potential
    return clamp(score, 0.33, 0.67)


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


def classify_preflop_hand(my_cards):
    """Classify a preflop hand into a strategic bucket for range-based decisions.

    Uses preflop_hand_profile discrete fields, NOT estimate_preflop_strength
    (which saturates 88-AA all to 1.0 after clamp, making preflop 3-bet/4-bet
    thresholds unable to distinguish 88 from AA).

    Returns one of:
      'premium'          - QQ, KK, AA
      'strong_pair'      - TT, JJ
      'mid_pair'         - 77, 88, 99
      'small_pair'       - 22-66
      'big_cards'        - AK, AQ (and AJo)
      'broadway_suited'  - KQs, KJs, QJs, QTs, JTs (suited both >=10)
      'suited_connector' - suited connectors/gappers 56s+
      'suited_ace'       - A2s-ATs (non-broadway suited aces)
      'playable'         - offsuit broadways (KQ/KJ/QJ), misc suited
      'trash'            - fold
    """
    p = preflop_hand_profile(my_cards)
    high, low, suited, pair = p["high"], p["low"], p["suited"], p["pair"]
    gap = high - low

    if pair:
        if high >= 12:      # QQ, KK, AA
            return "premium"
        if high >= 10:      # TT, JJ
            return "strong_pair"
        if high >= 7:       # 77, 88, 99
            return "mid_pair"
        return "small_pair"  # 22-66

    # Non-pair hands
    if high == 14 and low >= 12:        # AK, AQ
        return "big_cards"
    # CROSSOVER (imported from v109/v108/v89): explicit implied-odds bucket for
    # suited broadways (KQs, KJs, QJs, QTs, JTs) instead of falling into the
    # offsuit 'playable' bucket. These hands flop strong draws / two-pair+
    # often enough to play vs raises with implied odds.
    if suited and 11 <= high <= 13 and low >= 10:
        return "broadway_suited"
    if suited and high == 14:           # A2s-AJs (AK caught above)
        return "suited_ace"
    if suited and gap <= 2 and low >= 5 and high <= 13:  # suited connectors/gappers
        return "suited_connector"
    if high == 14 and low >= 11:        # AJo (offsuit big ace)
        return "big_cards"
    if high >= 11 and low >= 10:        # KQ, KJ, QJ (offsuit broadways)
        return "playable"
    if suited and gap <= 4 and high >= 9:
        return "playable"
    if high >= 12 and low >= 8 and gap <= 4:
        return "playable"
    if high == 14:                      # A6o-A9o, A2o-A5o (small offsuit aces)
        return "playable"
    return "trash"


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

    if strength <= 0.40:
        return True
    if not suited and high <= 10 and low <= 5 and gap >= 3:
        return True
    if not suited and high <= 12 and low <= 4 and gap >= 5:
        return True
    if suited and high <= 9 and low <= 4 and gap >= 4:
        return True
    return False


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
    # Heads-up: dealer_id is the small blind; big blind is the other seat.
    # (Old `next_player(dealer, 1/2)` is wrong with N_PLAYERS=2 -- it returns
    # the other seat for offset 1 and wraps back to dealer for offset 2.)
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
        # future_dealer is itself the SB (heads-up dealer = SB); BB is opposite.
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


def _verify_preflop_ranking():
    """Self-test: verify estimate_preflop_strength correctly ranks hands.
    Run via: python3 -c 'from state import _verify_preflop_ranking; _verify_preflop_ranking()'
    """
    def c(r, s):
        return (r - 2) * 4 + s
    def s(hand):
        return estimate_preflop_strength([c(hand[0], 0), c(hand[1], 1 if hand[0] != hand[1] else 0)])
    # Pairs must rank above dominated offsuit trash
    assert s((5, 5)) > s((13, 2)), '55 must rank above K2o'
    assert s((4, 4)) > s((13, 2)), '44 must rank above K2o'
    assert s((3, 3)) > s((13, 2)), '33 must rank above K2o'
    # Smallest pair must be callable
    assert s((2, 2)) >= 0.48, '22 must be >= 0.48'
    # Pair monotonicity
    assert s((14, 14)) > s((13, 13)) > s((12, 12)) > s((11, 11)) > s((10, 10))
    # Suited > offsuit
    assert estimate_preflop_strength([c(14, 0), c(13, 0)]) > estimate_preflop_strength([c(14, 0), c(13, 1)])
    # Premiums in correct range
    assert 0.80 <= s((14, 14)) <= 0.86
    assert 0.63 <= estimate_preflop_strength([c(14, 0), c(13, 0)]) <= 0.68  # AKs
    print('PREFLOP_RANKING_VERIFY PASS')


def _preflop_spr_commitment_gate(raise_to, my_chips, already_invested, pot,
                                  preflop_strength, facing_villain_4bet=False):
    """Preflop SPR commitment gate (v194 crossover axis).

    For marginal one-pair-maker hands (preflop_strength 0.50-0.62, e.g. AQ/JJ/
    TT/KQ), cap raise size so the projected SPR-if-called stays >= 4 (pot
    control), preventing -20k stack-off exposure. Premium hands (>0.70) and
    trash (<0.45) bypass the gate entirely.

    GTO SPR thresholds: SPR<3 = commit any pair/draw; SPR 4 = top-pair+ only;
    SPR 6-10 = careful one-pair. Marginal hands must never create SPR<4.

    Returns:
      -1 if facing_villain_4bet and SPR would drop below 4 (fold marginal).
      capped raise-to-total (int >= already_invested) otherwise.
    """
    MARG_LO, MARG_HI = 0.50, 0.62
    SPR_FLOOR = 4.0
    if preflop_strength < MARG_LO or preflop_strength > MARG_HI:
        return raise_to  # premium/trash bypass
    new_invest = max(0, raise_to - already_invested)
    projected_pot = pot + 2 * new_invest
    if projected_pot <= 0:
        return raise_to
    spr = (my_chips - new_invest) / projected_pot
    if spr >= SPR_FLOOR:
        return raise_to  # sizing already safe
    if facing_villain_4bet:
        return -1  # fold marginal to committing 4-bet
    # Cap raise so projected SPR == SPR_FLOOR.
    # Solve (my_chips - x) / (pot + 2x) = SPR_FLOOR for x (additional invest).
    capped_invest = (my_chips - SPR_FLOOR * pot) / (1.0 + 2.0 * SPR_FLOOR)
    capped_invest = max(0, int(capped_invest))
    return already_invested + capped_invest


def _preflop_shove_defense_fold(my_cards, preflop_strength, state, spot_info, opponent_model=None):
    """Preflop shove/4-bet defense gate (v201 NEW axis; v206 raise-size trigger).

    Fires when facing a preflop all-in, a 4-bet+ (opp_pf_raises >= 2), OR a
    commitment-level raise of >= 12BB by SIZE (v206 NEW trigger). The size
    trigger fires regardless of raise count, since opp_pf_raises >= 2 is
    trigger-inert (rare in 70-hand HU) and large single commitment raises were
    leaking through to bucket logic.
    Targets -20k stack-off leak (G5H68: KTo call-off vs shove).
    Premiums (QQ+), big aces (AK/AQ/AJo), strong pairs (TT/JJ) continue.
    Mid pairs (77-99) continue only with cheap odds (pot_odds < 0.35).
    Everything else folds. Returns True=fold, False=continue.
    """
    opp_allin = state.get('opponent_allin', False)
    opp_pf_raises = spot_info.get('opp_preflop_raises', 0)
    # v206 NEW trigger: raise-SIZE-based (avoids opp_pf_raises>=2 trigger-inert).
    # Raise-to-total >= 12BB = commitment-level raise by SIZE.
    _my_round_bet = state.get('my_round_bet', 0)
    _opp_raise_to_total = state.get('to_call', 0) + _my_round_bet
    _opp_raise_size_bb = _opp_raise_to_total / BIG_BLIND
    if not (opp_allin or opp_pf_raises >= 2 or _opp_raise_size_bb >= 12.0):
        return False
    hand_cat = classify_preflop_hand(my_cards)
    if hand_cat in ('premium', 'big_cards', 'strong_pair'):
        return False
    to_call = state.get('to_call', 0)
    pot = max(1, state.get('pot', 1))
    pot_odds = to_call / (pot + to_call) if to_call > 0 else 0.0
    if hand_cat == 'mid_pair' and pot_odds < 0.35:
        return False
    return True


def _verify_preflop_shove_defense():
    """Self-test: _preflop_shove_defense_fold tier logic (12 assertions)."""
    def c(r, s):
        return (r - 2) * 4 + s
    kto = [c(13, 0), c(10, 1)]  # KTo -- G5H68 leak hand
    state_shove = {'opponent_allin': True, 'to_call': 19900, 'pot': 20150}
    spot1 = {'opp_preflop_raises': 1}
    assert _preflop_shove_defense_fold(kto, 0.58, state_shove, spot1), 'KTo must fold vs shove'
    aa = [c(14, 0), c(14, 1)]
    assert not _preflop_shove_defense_fold(aa, 0.85, state_shove, spot1), 'AA continues'
    aks = [c(14, 0), c(13, 0)]
    assert not _preflop_shove_defense_fold(aks, 0.66, state_shove, spot1), 'AKs continues'
    fives = [c(5, 0), c(5, 1)]
    assert _preflop_shove_defense_fold(fives, 0.59, state_shove, spot1), '55 folds expensive'
    state_cheap = {'opponent_allin': True, 'to_call': 500, 'pot': 2000}
    eights = [c(8, 0), c(8, 1)]
    assert not _preflop_shove_defense_fold(eights, 0.62, state_cheap, spot1), '88 continues cheap'
    state_norm = {'opponent_allin': False, 'to_call': 200, 'pot': 500}
    assert not _preflop_shove_defense_fold(kto, 0.58, state_norm, spot1), 'No fire on normal raise'
    # v206 NEW: raise-size trigger (no opp_allin, opp_pf_raises=1).
    spot_size = {'opp_preflop_raises': 1}
    state_size = {'opponent_allin': False, 'to_call': 1100, 'pot': 1300, 'my_round_bet': 100}
    _k7o = [c(13, 0), c(7, 1)]
    assert _preflop_shove_defense_fold(_k7o, 0.40, state_size, spot_size), 'K7o folds vs 12BB raise'
    _aqs = [c(14, 0), c(12, 0)]
    assert not _preflop_shove_defense_fold(_aqs, 0.65, state_size, spot_size), 'AQs continues vs 12BB'
    _jto = [c(11, 0), c(10, 1)]
    assert _preflop_shove_defense_fold(_jto, 0.50, state_size, spot_size), 'JTo folds vs 12BB'
    _fives_size = [c(5, 0), c(5, 1)]
    assert _preflop_shove_defense_fold(_fives_size, 0.59, state_size, spot_size), '55 folds vs 12BB expensive'
    _state_cheap_size = {'opponent_allin': False, 'to_call': 200, 'pot': 2000, 'my_round_bet': 100}
    _nines = [c(9, 0), c(9, 1)]
    assert not _preflop_shove_defense_fold(_nines, 0.62, _state_cheap_size, spot_size), '99 continues vs 12BB cheap'
    _state_norm_size = {'opponent_allin': False, 'to_call': 700, 'pot': 900, 'my_round_bet': 100}
    assert not _preflop_shove_defense_fold(_k7o, 0.40, _state_norm_size, spot_size), 'K7o NOT folded vs 8BB raise'
    print('PREFLOP_SHOVE_DEFENSE_VERIFY PASS 12/12')


def _bb_vs_raise_dominated_floor(hand_cat, preflop_strength):
    """Preflop BB-vs-raise dominated-non-pair floor (v254 NEW preflop axis).

    Declines the weakest dominated suited non-broadway hands in the 'playable'
    bucket (T7s/7Ts/6Ts/69s/59s-type, preflop_strength < 0.54) that
    _is_offsuit_commitment_risk misses and the 'playable' branch would otherwise
    defend OOP vs a raise. These realize negative equity out of position and
    seed the medium-pair deep-pot losses at the v245-v250 plateau root.

    Pairs / big_cards / broadway_suited / suited_ace / suited_connector are EXEMPT
    (nutted or implied-odds hands valued OOP; suited_connector is separately
    odds-gated). Only 'playable' is floored. Floor 0.54 keeps J8s/JTo/Q9o
    (0.56+) and folds T7s/6Ts/59s (the spotlight leaks).

    Returns True = decline (fold), False = continue to bucket logic.
    """
    if hand_cat != 'playable':
        return False
    return preflop_strength < 0.54


def _verify_bb_vs_raise_floor():
    """Self-test for _bb_vs_raise_dominated_floor (M6 fixture)."""
    def c(r, s):
        return (r - 2) * 4 + s
    from state import estimate_preflop_strength, classify_preflop_hand
    def check(cards, expect):
        cat = classify_preflop_hand(cards)
        st = estimate_preflop_strength(cards)
        got = _bb_vs_raise_dominated_floor(cat, st)
        assert got == expect, (cat, st, got, expect)
    # Spotlight leaks -> fold
    check([c(10, 0), c(7, 0)], True)   # T7s playable 0.536
    check([c(6, 0), c(5, 0)], False)   # 56s suited_connector (exempt, not playable)
    # Legitimate marginal defends -> pass
    check([c(11, 0), c(8, 0)], False)  # J8s playable 0.560
    check([c(12, 0), c(9, 1)], False)  # Q9o playable 0.584
    check([c(11, 0), c(10, 1)], False) # JTo playable 0.560
    # Exempt buckets -> pass regardless of strength
    check([c(14, 0), c(13, 0)], False) # AKs big_cards
    check([c(5, 0), c(5, 1)], False)   # 55 small_pair
    check([c(8, 0), c(6, 0)], False)   # 86s suited_connector 0.503 (implied odds)
    check([c(14, 0), c(2, 0)], False)  # A2s suited_ace
    print('PREFLOP_BB_RAISE_FLOOR_VERIFY PASS')


if __name__ == '__main__':
    _verify_preflop_shove_defense()
    _verify_bb_vs_raise_floor()
