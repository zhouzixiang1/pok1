import math
import random
from constants import ( N_PLAYERS, BIG_BLIND, INITIAL_CHIPS, SIMULATIONS_BY_PUBLIC_COUNT, EXTRA_SIMULATIONS_BY_PUBLIC_COUNT, STYLE_NAMES, N_STYLES, STYLE_PARAMS, SIZING_TABLE, SIZING_TIER_RATIOS, )
from card_utils import clamp, next_player
from state import ( estimate_preflop_strength, is_preflop_3bet_candidate, is_preflop_trash_hand, get_hand_index, get_remaining_hands, reconstruct_state, collect_latest_requests_by_hand, )
from tournament import ( should_lock_win, fold_gives_opponent_lock, match_risk_adjustment, match_pressure_profile, apply_anti_lock_pressure, anti_lock_can_continue, choose_anti_lock_pressure_action, )
from opponent import build_opponent_model, analyze_current_spot, classify_opponent_style
from postflop import ( made_hand_metric, pair_board_profile, pair_domination_margin, marginal_pair_under_pressure, board_texture_profile, paired_board_outcome_profile, bet_size_bucket, value_hand_tier, value_bet_plan, empty_draw_profile, draw_profile, draw_call_margin, made_flush_profile, blocker_bluff_profile, allow_low_frequency_blocker_bluff, nutted_risk_profile, postflop_call_margin, realized_postflop_equity, check_probe_resistance_margin, paired_board_stackoff_profile, )
from simulation import build_opponent_range, estimate_weighted_win_rate
class EXP3MetaLearner(object):
    def __init__(self):
        self.gamma = 0.10
        self.weights = [1.0] * N_STYLES
        self.style_gifts = [0.0] * N_STYLES
        self.last_style_idx = 0
        self.hand_count = 0
        self._prob_cache = None
    def _update_probabilities(self):
        total_w = sum(self.weights)
        n = N_STYLES
        probs = []
        for w in self.weights:
            p = (1.0 - self.gamma) * (w / total_w) + self.gamma / n
            probs.append(p)
        self._prob_cache = probs
        return probs
    def choose_style(self, rng):
        probs = self._update_probabilities()
        r = rng.random()
        cumul = 0.0
        for i, p in enumerate(probs):
            cumul += p
            if r < cumul:
                self.last_style_idx = i
                return i
        self.last_style_idx = N_STYLES - 1
        return N_STYLES - 1
    def observe_reward(self, reward, style_idx=None):
        if style_idx is None:
            style_idx = self.last_style_idx
        probs = self._prob_cache if self._prob_cache is not None else self._update_probabilities()
        p_i = max(probs[style_idx], 1e-6)
        scaled_reward = reward / p_i
        self.weights[style_idx] *= math.exp(self.gamma * scaled_reward / N_STYLES)
        max_w = max(self.weights)
        if max_w > 1e6:
            factor = 1e6 / max_w
            self.weights = [w * factor for w in self.weights]
        self._prob_cache = None
        self.hand_count += 1
    def update_style_gift(self, style_idx, chip_delta):
        if chip_delta > 0:
            self.style_gifts[style_idx] += chip_delta / INITIAL_CHIPS
    def get_effective_lambda(self, style_idx, confidence):
        if confidence < 0.25:
            return 0.0
        gift = max(0.0, self.style_gifts[style_idx])
        lam = confidence * min(1.0, gift / 2.0)
        return clamp(lam, 0.0, 0.85)
    def initialize_bias(self, initial_weights):
        for i, w in enumerate(initial_weights):
            if 0 <= i < N_STYLES:
                self.weights[i] = max(0.1, w)
    def to_dict(self):
        return { "w": self.weights, "g": self.style_gifts, "lsi": self.last_style_idx, "hc": self.hand_count, }
    @classmethod
    def from_dict(cls, d):
        obj = cls()
        if d and isinstance(d, dict):
            obj.weights = list(d.get("w", obj.weights))
            obj.style_gifts = list(d.get("g", obj.style_gifts))
            obj.last_style_idx = d.get("lsi", 0)
            obj.hand_count = d.get("hc", 0)
        return obj
_exp3_learner = EXP3MetaLearner()
def style_sizing_ratio(round_idx, tier, style_name, wetness):
    tier = tier if tier in ("none", "thin", "strong", "nut") else "none"
    base = SIZING_TABLE.get((round_idx, tier), 0.60)
    tier_ratio = SIZING_TIER_RATIOS.get(STYLE_PARAMS[style_name]["sizing_tier"], 0.0)
    wetness_correction = 0.0
    if wetness > 0.3:
        if tier in ("strong", "nut"):
            wetness_correction = 0.03 * wetness
        elif tier == "none":
            wetness_correction = -0.02 * wetness
    return clamp(0.70 * base + 0.30 * (base + tier_ratio) + wetness_correction, 0.22, 1.45)
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
def track_opponent_gift(requests, my_id):
    gift_balance = 0.0
    opponent_id = next_player(my_id, 1)
    hand_requests = collect_latest_requests_by_hand(requests)
    for req in hand_requests:
        total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
        if len(total_win_chips) <= opponent_id:
            continue
        opp_chips = total_win_chips[opponent_id]
        if opp_chips < -200:
            gift_balance += (-opp_chips - 200) / INITIAL_CHIPS
    return gift_balance
def safe_exploitation_lambda(gift_balance, confidence, style_idx=None):
    if style_idx is not None:
        return _exp3_learner.get_effective_lambda(style_idx, confidence)
    if confidence < 0.25:
        return 0.0
    lam = confidence * min(1.0, max(0.0, gift_balance) / 2.0)
    return clamp(lam, 0.0, 0.85)
def choose_raise( min_raise, my_chips, my_round_bet, to_call, pot, win_rate, round_idx, spot_name, preflop_strength, has_position, opponent_model, semi_bluff=False, value_profile=None, value_plan=None, board_texture=None, draw_info=None, blocker_bluff=False, probe_mode=False, pressure_line=False, induce_mode=False, nutted_risk_score=0.0, match_sizing_delta=0.0, style_params=None, ):
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
        ratio = 0.60
    elif round_idx == 2:
        ratio = 0.70
    else:
        ratio = 0.85
    if style_params is not None:
        tier = value_profile.get("tier", "none") if value_profile else "none"
        style_name = "gto"
        for sn, sp in STYLE_PARAMS.items():
            if sp is style_params:
                style_name = sn
                break
        style_ratio = style_sizing_ratio(round_idx, tier, style_name, wetness)
        ratio = 0.80 * ratio + 0.20 * style_ratio
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
    if value_plan.get("thin_control", False) and to_call == 0 and value_profile.get("tier") != "nut":
        ratio = min(ratio, 0.46 + 0.08 * wetness + 0.05 * max(0, round_idx - 1))
    low_ratio = 0.28 if inducing_value else 0.22 if probe_mode or (blocker_bluff and to_call == 0) else 0.40
    ratio = clamp(ratio, low_ratio, 1.45)
    amount = int(to_call + pot_after_call * ratio)
    if round_idx == 0 and preflop_strength is not None:
        if spot_name == "sb_open":
            desired_total = int((2.5 + max(0.0, preflop_strength - 0.58) * 1.8) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)
        elif spot_name == "bb_vs_limp":
            desired_total = int((3.2 + max(0.0, preflop_strength - 0.60) * 1.8) * BIG_BLIND)
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
def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile, style_params=None):
    if preflop_strength >= 0.70 and spot_info.get('facing_allin', False):
        return 0
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03
    trash_hand = is_preflop_trash_hand(req["my_cards"], preflop_strength)
    sp = style_params or STYLE_PARAMS["gto"]
    open_delta = sp["open_threshold_delta"]
    threebet_width_delta = sp["threebet_range_width_delta"]
    bluff_mult = sp["bluff_frequency_mult"]
    if spot_info["preflop_spot"] == "sb_open":
        open_threshold = 0.38 + match_adjust + 0.02 + match_profile["open_delta"] + open_delta
        limp_threshold = 0.25 + match_adjust
        raise_amount = choose_raise( state["round_raise"], my_chips, state["my_round_bet"], to_call, state["pot"], max(win_rate, preflop_strength), 0, spot_info["preflop_spot"], preflop_strength, spot_info["has_position"], opponent_model, match_sizing_delta=match_profile["sizing_delta"], style_params=sp, )
        if not trash_hand and preflop_strength >= open_threshold and raise_amount is not None:
            return raise_amount
        return 0
    if spot_info["preflop_spot"] == "bb_vs_limp":
        iso_threshold = 0.48 + match_adjust - loose_bonus + match_profile["open_delta"] + open_delta
        iso_threshold -= confidence * max(0.0, opponent_model["vpip"] - 0.58) * 0.08
        iso_threshold -= confidence * max(0.0, opponent_model["fold_to_raise"] - 0.52) * 0.05
        raise_amount = choose_raise( state["round_raise"], my_chips, state["my_round_bet"], to_call, state["pot"], max(win_rate, preflop_strength), 0, spot_info["preflop_spot"], preflop_strength, spot_info["has_position"], opponent_model, match_sizing_delta=match_profile["sizing_delta"], style_params=sp, )
        if not trash_hand and preflop_strength >= iso_threshold and raise_amount is not None:
            return raise_amount
        return 0
    if spot_info["preflop_spot"] == "bb_vs_raise":
        fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
        opp_pfr = opponent_model.get("pfr", 0.24)
        threebet_value_threshold = 0.72 - threebet_width_delta
        if preflop_strength >= threebet_value_threshold:
            pot_after_call = state["pot"] + to_call
            three_bet_mult = 3.0 + clamp(fold_to_raise - 0.44, -0.5, 0.5)
            target = int(to_call + pot_after_call * three_bet_mult * 0.33)
            target = max(state["round_raise"], target)
            target = min(target, my_chips - 1)
            if target > to_call and target >= state["round_raise"] and target < my_chips:
                return target
            target = max(state["round_raise"], int(to_call + state["pot"] * 0.75))
            if target > to_call and target < my_chips:
                return target
            return 0
        bluff_low = max(0.30, 0.38 - threebet_width_delta)
        bluff_high = 0.52 + threebet_width_delta * 0.5
        if bluff_low <= preflop_strength <= bluff_high and confidence >= 0.25 and fold_to_raise > 0.45:
            hand_idx = get_hand_index(req) or 0
            cards = req["my_cards"]
            freq_token = (sum(cards) * 13 + hand_idx * 7) % 100
            bluff_freq = clamp((fold_to_raise - 0.45) * 1.2 * bluff_mult, 0.0, 0.6)
            if freq_token < int(bluff_freq * 100):
                pot_after_call = state["pot"] + to_call
                target = int(to_call + pot_after_call * 0.60)
                target = max(state["round_raise"], target)
                target = min(target, my_chips - 1)
                if target > to_call and target >= state["round_raise"] and target < my_chips:
                    return target
        call_threshold = 0.30 + match_adjust - loose_bonus + sp["call_threshold_delta"]
        call_threshold -= confidence * max(0.0, fold_to_raise - 0.50) * 0.04
        if preflop_strength >= call_threshold:
            return 0
        if preflop_strength < 0.25 and to_call > BIG_BLIND * 5:
            return -1
        return 0
    if spot_info["preflop_spot"] == "sb_vs_reraise":
        if preflop_strength >= 0.85:
            pot_after_call = state["pot"] + to_call
            target = int(to_call + pot_after_call * 0.70)
            target = max(state["round_raise"], target)
            if target >= my_chips * 0.5:
                return -2
            target = min(target, my_chips - 1)
            if target > to_call and target >= state["round_raise"]:
                return target
            return -2
        if preflop_strength >= 0.52 and to_call <= my_chips * 0.20:
            return 0
        return -1
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
    rng = random.Random(sum(my_cards) * 31 + (get_hand_index(req) or 0) * 17 + 7)
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    opp_id = next_player(my_id, 1)
    current_hand_idx = get_hand_index(req)
    if _exp3_learner.hand_count == 0 and opponent_model["confidence"] >= 0.15:
        style_weights = classify_opponent_style(opponent_model)
        _exp3_learner.initialize_bias(style_weights)
    _prev_hand_key = ("bot3_prev_hand", my_id)
    prev_hand_idx = getattr(_exp3_learner, '_prev_hand_idx', -1)
    prev_my_chips = getattr(_exp3_learner, '_prev_my_chips', 0)
    if current_hand_idx is not None and prev_hand_idx >= 0 and current_hand_idx != prev_hand_idx:
        if len(total_win_chips) > my_id:
            chip_delta = total_win_chips[my_id] - prev_my_chips
            reward = clamp(chip_delta / 20000.0, -1.0, 1.0)
            _exp3_learner.observe_reward(reward)
            if chip_delta > 0:
                _exp3_learner.update_style_gift(_exp3_learner.last_style_idx, chip_delta)
    if current_hand_idx is not None:
        _exp3_learner._prev_hand_idx = current_hand_idx
    _exp3_learner._prev_my_chips = total_win_chips[my_id] if len(total_win_chips) > my_id else 0
    style_idx = _exp3_learner.choose_style(rng)
    style_params = STYLE_PARAMS[STYLE_NAMES[style_idx]]
    match_pressure_level = max(match_profile.get("chase", 0.0), match_profile.get("protect", 0.0))
    if match_pressure_level > 0.5:
        decay = 0.5
        style_params = dict(style_params)
        style_params["open_threshold_delta"] *= decay
        style_params["call_threshold_delta"] *= decay
        style_params["bluff_frequency_mult"] = 1.0 + (style_params["bluff_frequency_mult"] - 1.0) * decay
        style_params["threebet_range_width_delta"] *= decay
        style_params["trap_probability"] *= decay
    gift_balance = track_opponent_gift(requests, my_id)
    exploit_lambda = safe_exploitation_lambda(gift_balance, opponent_model["confidence"], style_idx=style_idx)
    if anti_lock_pressure:
        match_profile = apply_anti_lock_pressure(match_profile)
    preflop_strength = estimate_preflop_strength(my_cards) if not public_cards else None
    preflop_3bet_candidate = is_preflop_3bet_candidate(my_cards) if preflop_strength is not None else False
    preflop_trash_hand = is_preflop_trash_hand(my_cards, preflop_strength) if preflop_strength is not None else False
    combos, weights = build_opponent_range(my_cards, public_cards, state, opponent_model, spot_info)
    simulations = SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 700)
    win_rate = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, simulations)
    critical_spot = to_call > 0 and ( to_call / pot >= 0.25 or to_call >= BIG_BLIND * 4 or spot_info["facing_allin"] )
    extra = EXTRA_SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 0)
    if critical_spot and extra > 0:
        refined = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, extra)
        win_rate = (win_rate * simulations + refined * extra) / (simulations + extra)
    if round_idx == 0 and preflop_strength is not None:
        spot_action = choose_preflop_spot_action( req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile, style_params=style_params, )
        if spot_action is not None:
            if anti_lock_pressure and spot_action <= 0:
                if not preflop_trash_hand:
                    anti_lock_attack = choose_anti_lock_pressure_action( state, my_chips, to_call, pot, round_idx, win_rate, opponent_model, remaining_hands, preflop_strength=preflop_strength, )
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
    nutted_risk = ( nutted_risk_profile(my_cards, public_cards, pair_profile, board_texture, value_profile, paired_board_profile) if len(public_cards) >= 3 else {"risk": 0.0, "label": "none", "vulnerable": False} )
    value_plan = ( value_bet_plan(value_profile, board_texture, paired_board_profile, pair_profile, nutted_risk, round_idx, pot) if len(public_cards) >= 3 else {"size_delta": 0.0, "induce": False, "protect": False, "thin_control": False} )
    line_strength = aggressive_line_strength(spot_info, board_texture) if len(public_cards) >= 3 else 0.0
    check_resistance = check_probe_resistance_margin(spot_info, opponent_model, round_idx) if len(public_cards) >= 3 else 0.0
    paired_board_stackoff = ( paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx) if len(public_cards) >= 3 else {"active": False, "severe": False, "line_strength": 0.0, "size_bucket": "small"} )
    repeated_raise_trap = ( round_idx > 0 and spot_info["facing_postflop_aggression"] and spot_info.get("opp_current_round_bet_count", 0) >= 2 )
    strong_flush_repressure_continue = ( flush_profile is not None and ( flush_profile["repressure_continue"] or flush_profile["nut_like"] or ( board_texture is not None and not board_texture["paired"] and flush_profile["high_hole_rank"] >= 12 and flush_profile["better_unseen_ranks"] <= 1 ) ) )
    hard_repressure_fold = ( repeated_raise_trap and not strong_flush_repressure_continue and (value_profile is None or value_profile["tier"] != "nut") and ( (board_texture is not None and board_texture["paired"]) or bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large") ) )
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
    strong += style_params["open_threshold_delta"]
    medium += style_params["open_threshold_delta"] * 0.5
    gto_strong = 0.69 if round_idx == 0 else 0.65 if round_idx == 1 else 0.61 if round_idx == 2 else 0.59
    gto_medium = 0.54 if round_idx == 0 else 0.50 if round_idx == 1 else 0.48
    if spot_info["has_position"]:
        gto_strong -= 0.015
        gto_medium -= 0.01
    else:
        gto_strong += 0.02
        gto_medium += 0.015
    gto_strong += match_adjust + match_profile["threshold_delta"]
    gto_medium += match_adjust + 0.75 * match_profile["threshold_delta"]
    strong = (1.0 - exploit_lambda) * gto_strong + exploit_lambda * strong
    medium = (1.0 - exploit_lambda) * gto_medium + exploit_lambda * medium
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
        anti_lock_jam_continue = anti_lock_can_continue( anti_lock_pressure, win_rate, jam_odds, round_idx, value_profile, draw_info, made_strength, )
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
        anti_lock_shove_continue = anti_lock_can_continue( anti_lock_pressure, win_rate, shove_odds, round_idx, value_profile, draw_info, made_strength, )
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_shove_continue:
                return -1
        shove_buffer = clamp(shove_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= shove_odds + shove_buffer or anti_lock_shove_continue else -1
    if to_call > 0:
        if round_idx == 0:
            call_margin = 0.005 + (0.010 if not spot_info["has_position"] else 0.0)
            if preflop_strength is not None and preflop_strength <= 0.40:
                call_margin += 0.015
            realized_rate = win_rate
        else:
            call_margin = postflop_call_margin( spot_info, opponent_model, made_strength, draw_strength, round_idx, spot_info["has_position"], )
            call_margin += pair_domination_margin( pair_profile, spot_info, round_idx, )
            call_margin += draw_call_margin( draw_info, board_texture, round_idx, spot_info, )
            if ( round_idx == 2 and spot_info["facing_postflop_aggression"] and pair_profile is not None and pair_profile["made_class"] == 1 and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair") ):
                call_margin += 0.035
            call_margin += line_strength + paired_board_stackoff["line_strength"]
            call_margin += check_resistance
            call_margin += 0.50 * nutted_risk["risk"]
            call_margin += style_params["call_threshold_delta"]
            if round_idx == 3 and made_strength < 0.40 and not (blocker_profile and blocker_profile["eligible"]):
                call_margin += 0.04
            if round_idx == 3 and paired_board_profile is not None and paired_board_profile["fold_to_raise"]:
                call_margin += 0.05
            realized_rate = realized_postflop_equity( win_rate, made_strength, draw_strength, round_idx, spot_info["has_position"], spot_info, pair_profile, pot, )
            cbet_rate = opponent_model.get("cbet_rate", 0.55)
            fold_to_cbet = opponent_model.get("fold_to_cbet", 0.40)
            if round_idx == 1 and spot_info["facing_postflop_aggression"]:
                if cbet_rate > 0.65:
                    call_margin -= 0.02
                elif cbet_rate < 0.40:
                    call_margin += 0.02
        if anti_lock_pressure:
            call_margin -= 0.07
        anti_lock_call_continue = anti_lock_can_continue( anti_lock_pressure, win_rate, pot_odds, round_idx, value_profile, draw_info, made_strength, )
        anti_lock_attack = None
        if anti_lock_pressure:
            if round_idx > 0 or preflop_3bet_candidate:
                anti_lock_attack = choose_anti_lock_pressure_action( state, my_chips, to_call, pot, round_idx, win_rate, opponent_model, remaining_hands, preflop_strength=preflop_strength, value_profile=value_profile, draw_info=draw_info, blocker_profile=blocker_profile, board_texture=board_texture, )
        fragile_river_raise_fold = ( round_idx == 3 and spot_info["facing_postflop_aggression"] and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large") and paired_board_profile is not None and paired_board_profile["fold_to_raise"] and paired_board_profile["hand_class"] == 2 and (value_profile is None or value_profile["tier"] != "nut") )
        fragile_pair_raise_fold = ( round_idx > 0 and spot_info["facing_postflop_aggression"] and marginal_pair and draw_strength < 0.14 and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large") and (value_profile is None or value_profile["tier"] not in ("strong", "nut")) )
        if anti_lock_attack is not None:
            return anti_lock_attack
        if fragile_river_raise_fold:
            if not anti_lock_call_continue:
                return -1
        if fragile_pair_raise_fold:
            if not anti_lock_call_continue:
                return -1
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_call_continue:
                return -1
        if realized_rate < pot_odds + call_margin:
            if not anti_lock_call_continue:
                return -1
        if repeated_raise_trap and (value_profile is None or value_profile["tier"] != "nut"):
            return 0
        raise_fold_threshold = (0.56 - 0.30 * match_profile["bluff_delta"]) / style_params["bluff_frequency_mult"]
        blocker_raise_threshold = (0.55 - 0.32 * match_profile["bluff_delta"]) / style_params["bluff_frequency_mult"]
        draw_raise_threshold = clamp(raise_fold_threshold - draw_info["fold_threshold_delta"], 0.46, 0.68)
        draw_equity_slack = 0.05 if draw_info["type"] in ("combo_draw", "nut_flush_draw") else 0.03
        semi_bluff = ( round_idx > 0 and draw_info["semi_bluff"] and draw_strength >= 0.12 and opponent_model["confidence"] >= 0.25 and opponent_model["fold_to_raise"] > draw_raise_threshold and win_rate >= pot_odds - draw_equity_slack )
        blocker_raise = ( round_idx == 1 and spot_info["facing_postflop_aggression"] and opponent_model["confidence"] >= 0.25 and opponent_model["fold_to_raise"] > blocker_raise_threshold and blocker_profile is not None and blocker_profile["eligible"] and made_strength < 0.18 and draw_strength < 0.12 and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx) )
        trap_nut_slowplay = ( round_idx in (1, 2) and value_profile is not None and value_profile["tier"] == "nut" and board_texture is not None and not board_texture["dynamic"] and spot_info["facing_postflop_aggression"] and bet_size_bucket(spot_info["last_raise_pot_ratio"]) != "large" and pot < 1400 and nutted_risk["risk"] <= 0.02 and match_profile["chase"] <= 0.45 and opponent_model["confidence"] >= 0.20 and ( opponent_model["postflop_aggr"] >= 0.38 or opponent_model["aggression"] >= 0.34 or opponent_model["fold_to_raise"] < 0.46 ) )
        flop_checkraise_exploit = ( round_idx == 1 and spot_info["facing_postflop_aggression"] and opponent_model["confidence"] >= 0.25 and opponent_model["fold_to_raise"] > blocker_raise_threshold and ( (value_profile and value_profile["tier"] in ("strong", "nut")) or (draw_info["semi_bluff"] and draw_strength >= 0.15) or blocker_raise ) )
        if trap_nut_slowplay:
            return 0
        if (round_idx in (1, 2) and value_profile is not None and value_profile["tier"] == "nut" and board_texture is not None and not board_texture["dynamic"] and nutted_risk["risk"] <= 0.05 and style_params["trap_probability"] > 0.0 and rng.random() < style_params["trap_probability"]):
            return 0
        preflop_defensive_only = ( round_idx == 0 and to_call > 0 and not preflop_3bet_candidate )
        if not preflop_defensive_only and (win_rate >= max(strong, pot_odds + 0.12) or semi_bluff or flop_checkraise_exploit):
            raise_amount = choose_raise( state["round_raise"], my_chips, state["my_round_bet"], to_call, pot, win_rate, round_idx, spot_info["preflop_spot"], preflop_strength, spot_info["has_position"], opponent_model, semi_bluff=semi_bluff or (flop_checkraise_exploit and draw_info["semi_bluff"] and draw_strength >= 0.15), value_profile=value_profile, value_plan=value_plan, board_texture=board_texture, draw_info=draw_info, blocker_bluff=blocker_raise, pressure_line=flop_checkraise_exploit, nutted_risk_score=nutted_risk["risk"], match_sizing_delta=match_profile["sizing_delta"], style_params=style_params, )
            if raise_amount is not None and raise_amount > to_call:
                return raise_amount
        return 0
    weak_pair_river = ( round_idx == 3 and pair_profile is not None and pair_profile["made_class"] == 1 and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair", "board_pair") )
    opp_double_barrel_then_river_check = ( round_idx == 3 and to_call == 0 and spot_info.get("opp_postflop_bet_count", 0) >= 2 and spot_info["last_opp_action_type"] == "check" )
    bad_river_bluff_candidate = ( round_idx == 3 and to_call == 0 and made_strength >= 0.18 and made_strength < 0.40 and not (blocker_profile and blocker_profile["eligible"]) and not (value_profile and value_profile["tier"] in ("strong", "nut")) )
    weak_bottom_pair_barrel = ( round_idx >= 2 and to_call == 0 and pair_profile is not None and pair_profile["made_class"] == 1 and pair_profile["pair_type"] in ("bottom_pair", "underpair", "board_pair") and made_strength < 0.40 and draw_strength < 0.12 )
    weak_pair_after_raise_barrel = ( round_idx >= 2 and to_call == 0 and marginal_pair and draw_strength < 0.14 and (value_profile is None or value_profile["tier"] not in ("strong", "nut")) and ( spot_info.get("opp_previous_round_raise_count", 0) > 0 or spot_info.get("opp_prior_postflop_raise_count", 0) > 0 ) )
    bad_river_value_bet = ( round_idx == 3 and to_call == 0 and paired_board_profile is not None and paired_board_profile["board_paired"] and paired_board_profile["prefer_check"] and paired_board_profile["hand_class"] == 2 and nutted_risk["risk"] >= 0.05 and (value_profile is None or value_profile["tier"] != "nut") )
    bad_stackoff_overpair = ( round_idx > 0 and to_call == 0 and paired_board_stackoff["active"] and pot > 3000 and (value_profile is None or value_profile["tier"] != "nut") )
    big_pot_threshold = int(clamp(1500 - 350 * match_profile["protect"] + 250 * match_profile["chase"], 1100, 1800))
    big_pot = pot >= big_pot_threshold
    induce_nut_value = ( round_idx > 0 and to_call == 0 and value_profile is not None and value_profile["tier"] == "nut" and board_texture is not None and not board_texture["dynamic"] and not big_pot and match_profile["chase"] <= 0.55 and opponent_model["confidence"] >= 0.20 and ( opponent_model["postflop_aggr"] >= 0.38 or opponent_model["aggression"] >= 0.34 or opponent_model["fold_to_raise"] < 0.46 ) )
    anti_lock_attack = None
    if anti_lock_pressure:
        anti_lock_attack = choose_anti_lock_pressure_action( state, my_chips, to_call, pot, round_idx, win_rate, opponent_model, remaining_hands, preflop_strength=preflop_strength, value_profile=value_profile, draw_info=draw_info, blocker_profile=blocker_profile, board_texture=board_texture, )
        if anti_lock_attack is not None:
            return anti_lock_attack
    if opp_double_barrel_then_river_check and weak_pair_river:
        return 0
    if bad_river_bluff_candidate:
        return 0
    if weak_bottom_pair_barrel:
        return 0
    if weak_pair_after_raise_barrel:
        return 0
    if bad_river_value_bet:
        return 0
    if bad_stackoff_overpair:
        return 0
    if big_pot and round_idx == 3 and (value_profile is None or value_profile["tier"] not in ("strong", "nut")):
        if blocker_profile is None or not blocker_profile["eligible"]:
            return 0
    river_bluff_threshold = (0.62 - 0.28 * match_profile["bluff_delta"]) / style_params["bluff_frequency_mult"]
    probe_fold_threshold = 0.56 - 0.32 * match_profile["bluff_delta"]
    semi_bluff_threshold = 0.58 - 0.28 * match_profile["bluff_delta"]
    draw_bet_threshold = clamp(semi_bluff_threshold - draw_info["fold_threshold_delta"], 0.46, 0.70)
    check_probe_signal = ( spot_info["last_opp_action_type"] == "check" and ( spot_info.get("opp_postflop_check_count", 0) >= 2 or ( opponent_model["confidence"] >= 0.20 and opponent_model.get("postflop_check_rate", 0.42) >= 0.52 ) ) )
    river_blocker_bluff = ( round_idx == 3 and made_strength < 0.16 and draw_strength < 0.08 and opponent_model["confidence"] >= 0.35 and opponent_model["fold_to_raise"] > river_bluff_threshold and blocker_profile is not None and blocker_profile["eligible"] and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx) )
    small_probe = ( round_idx > 0 and opponent_model["confidence"] >= 0.25 and opponent_model["fold_to_raise"] > probe_fold_threshold and made_strength < 0.62 and draw_strength < 0.16 and board_texture is not None and board_texture["wetness"] <= 0.32 and not (value_profile and value_profile["tier"] in ("strong", "nut")) )
    check_probe = ( round_idx > 0 and check_probe_signal and board_texture is not None and board_texture["wetness"] <= 0.55 and made_strength < 0.58 and draw_strength < 0.20 and not (value_profile and value_profile["tier"] in ("strong", "nut")) and not (round_idx == 3 and made_strength >= 0.18 and not (blocker_profile and blocker_profile["eligible"])) )
    blocker_bluff = ( river_blocker_bluff )
    semi_bluff = ( round_idx > 0 and draw_info["semi_bluff"] and draw_strength >= 0.12 and opponent_model["confidence"] >= 0.25 and opponent_model["fold_to_raise"] > draw_bet_threshold )
    if round_idx == 3 and len(public_cards) == 5:
        exact_wr = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, 0)
        if exact_wr > 0.85 and to_call > 0:
            raise_amount = choose_raise( state["round_raise"], my_chips, state["my_round_bet"], to_call, pot, exact_wr, round_idx, spot_info["preflop_spot"], preflop_strength, spot_info["has_position"], opponent_model, value_profile=value_profile, value_plan=value_plan, board_texture=board_texture, nutted_risk_score=nutted_risk["risk"], match_sizing_delta=match_profile["sizing_delta"], style_params=style_params, )
            if raise_amount is not None and raise_amount > to_call:
                return raise_amount
            return 0
        if exact_wr < 0.15 and to_call > 0:
            min_call_odds = to_call / (pot + to_call) if to_call > 0 else 1.0
            if exact_wr < min_call_odds - 0.10:
                return -1
        if (exact_wr < 0.20 and to_call == 0 and blocker_profile is not None and blocker_profile["eligible"] and style_params["bluff_frequency_mult"] > 0.8 and rng.random() < 0.25 * style_params["bluff_frequency_mult"]):
            blocker_bluff = True
    if win_rate >= medium or semi_bluff or blocker_bluff or small_probe or check_probe or made_strength >= 0.62 or (value_profile and value_profile["tier"] in ("strong", "nut")):
        raise_amount = choose_raise( state["round_raise"], my_chips, state["my_round_bet"], to_call, pot, win_rate, round_idx, spot_info["preflop_spot"], preflop_strength, spot_info["has_position"], opponent_model, semi_bluff=semi_bluff and win_rate < medium, value_profile=value_profile, value_plan=value_plan, board_texture=board_texture, draw_info=draw_info, blocker_bluff=blocker_bluff and win_rate < medium and not semi_bluff, probe_mode=check_probe or small_probe or (value_profile and value_profile["tier"] == "thin" and board_texture and not board_texture["dynamic"]), induce_mode=induce_nut_value or value_plan.get("induce", False), nutted_risk_score=nutted_risk["risk"], match_sizing_delta=match_profile["sizing_delta"], style_params=style_params, )
        if raise_amount is not None:
            return raise_amount
    return 0
