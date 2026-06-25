"""v150 Worker 1 Task A: Full-pipeline code-reachability harness for
_street_fold_exploit_sizing_boost.

The existing isolated fixture in strategy_helpers.py only proves the boost
function returns the expected scalar for a synthetic opponent dict. It does
NOT prove the boost actually flows through get_action() and changes the
final raise sizing on a realistic daemon-shaped input. Direction audit v150
mandated this full-pipeline proof as the ONLY acceptable firing evidence
(grep != firing; isolated fixtures do not count).

Construction:
  1. Build a minimal flop request (BB first-to-act, to_call=0) where the bot
     holds pocket Queens on a low rainbow board -> value-raise path triggers.
  2. Monkeypatch strategy.build_opponent_model to inject a high-folding
     opponent (confidence=0.80, fold_to_bet_flop=0.85).
  3. Call strategy.get_action(req, requests) -> action_boosted.
  4. Monkeypatch _street_fold_exploit_sizing_boost to return 0.0 (in BOTH
     strategy and strategy_helpers namespaces, since strategy.py imports the
     function by name).
  5. Call strategy.get_action(req, requests) again -> action_baseline.
  6. Assert both are raises (>0) AND action_boosted > action_baseline.

A PASS proves the boost fires end-to-end and increases sizing. A FAIL means
the boost is inert through the live pipeline (the bug we are closing).

Card encoding: card_int = (number-2)*4 + suit; suit: 0=heart,1=diamond,
2=spade,3=club. Example: Qh=(12-2)*4+0=40, Qd=(12-2)*4+1=41.
"""
import os
import sys

# Ensure the bot directory is on sys.path so `import strategy` resolves to
# the bot under test, regardless of where the harness is invoked from.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import strategy
import strategy_helpers


def _build_flop_request():
    """Construct a realistic BB flop-first-to-act spot.

    Bot is BB (my_id=0, dealer_id=0; in heads-up dealer=SB so bb=dealer).
    SB completed (call), BB checked preflop -> limped pot, no PFR. Flop is
    dealt, BB is first to act postflop with to_call=0.

    Bot hand: pocket Queens [Qh, Qd] = [40, 41] (overpair on low board).
    Board: [2h, 4d, 6s] = [0, 9, 18] (low rainbow, very dry).
    """
    # Preflop history: SB (player_id=1) calls completing to 100, BB checks.
    preflop_history = [
        {"round": 0, "player_id": 1, "action": 100, "action_type": "call"},
        {"round": 0, "player_id": 0, "action": 0, "action_type": "check"},
    ]
    req = {
        "my_id": 0,
        "dealer_id": 0,
        "my_chips": 19900,          # 20000 - 100 BB
        "opponent_chips": 19950,    # 20000 - 50 SB
        "my_cards": [40, 41],       # Qh, Qd (pocket Queens)
        "public_cards": [0, 9, 18],  # 2h, 4d, 6s (low rainbow)
        "history": preflop_history,
        "hand": 5,
        "max_hand": 70,
    }
    return req


def _high_folding_opponent_model():
    """Opponent profile that folds 85% to flop bets.

    Realistic shape: includes every key the live build_opponent_model returns
    so downstream code never KeyErrors. Only the per-street fold signal and
    confidence are tuned to fire the boost; the rest are neutral defaults.
    """
    return {
        "confidence": 0.80,
        "vpip": 0.55,
        "pfr": 0.20,
        "allin_rate": 0.04,
        "postflop_aggr": 0.30,
        "postflop_check_rate": 0.50,
        "fold_to_raise": 0.50,
        "fold_to_open_preflop": 0.42,
        "threebet_vs_open": 0.16,
        "open_response_samples": 20,
        "open_response_confidence": 0.50,
        "aggression": 0.28,
        "avg_raise_bb": 3.5,
        "flop_aggr": 0.30,
        "turn_aggr": 0.28,
        "river_aggr": 0.25,
        "avg_flop_raise_bb": 3.0,
        "avg_turn_raise_bb": 4.5,
        "avg_river_raise_bb": 5.5,
        "barrel_freq": 0.45,
        "sizing_aggr": 0.35,
        # Per-street fold signals — flop set high to fire the boost.
        "fold_to_bet_flop": 0.85,
        "fold_to_bet_turn": 0.55,
        "fold_to_bet_river": 0.50,
        "call_down_flop_turn": 0.35,
        "call_down_turn_river": 0.35,
        "passivity_score": 0.45,
        "value_maximizer_index": 0.30,
        "sizing_tendency": {
            "tendency": "standard",
            "overbet_rate": 0.0,
            "underbet_rate": 0.0,
            "standard_rate": 1.0,
            "samples": 0,
            "per_street_overbet": {1: 0.0, 2: 0.0, 3: 0.0},
            "per_street_underbet": {1: 0.0, 2: 0.0, 3: 0.0},
            "per_street_samples": {1: 0, 2: 0, 3: 0},
            "confidence": 0.0,
        },
    }


def _disable_boost():
    """Monkeypatch the boost to a no-op in both namespaces.

    strategy.py imports _street_fold_exploit_sizing_boost by name, so the
    reference lives in strategy.__dict__. Patching strategy's attribute is
    sufficient for in-module lookups. We also patch strategy_helpers for
    completeness/symmetry and to defend against any helper-internal caller.
    """
    def _zero(_opp_model, _round_idx):
        return 0.0
    strategy._street_fold_exploit_sizing_boost = _zero
    strategy_helpers._street_fold_exploit_sizing_boost = _zero


def _restore_boost():
    """Restore the real boost in both namespaces."""
    real = strategy_helpers.__dict__.get(
        "_street_fold_exploit_sizing_boost_original"
    )
    if real is None:
        # Re-import to get a fresh reference (the helper module still holds
        # the original function object in its def-time namespace unless we
        # overwrote it; either way, reload is the safe path).
        import importlib
        importlib.reload(strategy_helpers)
        # Re-import in strategy too.
        strategy._street_fold_exploit_sizing_boost = (
            strategy_helpers._street_fold_exploit_sizing_boost
        )
    else:
        strategy._street_fold_exploit_sizing_boost = real
        strategy_helpers._street_fold_exploit_sizing_boost = real


def main():
    # Snapshot the real boost before any monkeypatching, so we can restore
    # without relying on reload semantics.
    _real_boost = strategy_helpers._street_fold_exploit_sizing_boost
    strategy_helpers._street_fold_exploit_sizing_boost_original = _real_boost

    req = _build_flop_request()
    requests_list = [req]  # single-hand request list is enough post-monkeypatch

    # Inject the high-folding opponent. Patch the symbol used inside
    # strategy.py so the in-module name resolves to our stub. analyze_current_spot
    # stays unpatched (it only reads req history, no opponent model needed).
    strategy.build_opponent_model = lambda _requests, _my_id: (
        _high_folding_opponent_model()
    )

    # Step 1: Boost ENABLED (real implementation). Should size up.
    # Ensure the real boost is wired in strategy's namespace.
    strategy._street_fold_exploit_sizing_boost = _real_boost
    action_boosted = strategy.get_action(req, requests_list)
    print("boosted action:", action_boosted)

    # Step 2: Boost DISABLED (monkeypatch to 0.0).
    _disable_boost()
    try:
        action_baseline = strategy.get_action(req, requests_list)
    finally:
        # Restore so subsequent test runs / imports see the real function.
        strategy._street_fold_exploit_sizing_boost = _real_boost
        strategy_helpers._street_fold_exploit_sizing_boost = _real_boost
    print("baseline action:", action_baseline)

    # Assertions.
    assert action_boosted is not None, "boosted action is None (get_action crashed)"
    assert action_baseline is not None, "baseline action is None (get_action crashed)"
    assert action_boosted > 0, (
        f"REACHABILITY FAIL: boosted action={action_boosted} is not a raise (>0). "
        f"Value-raise path did not fire; adjust hand/flop construction."
    )
    assert action_baseline > 0, (
        f"REACHABILITY FAIL: baseline action={action_baseline} is not a raise (>0). "
        f"Without the boost the bot checks; cannot prove a sizing delta."
    )
    assert action_boosted > action_baseline, (
        f"REACHABILITY FAIL: boosted={action_boosted} not greater than "
        f"baseline={action_baseline}. Boost is INERT through get_action()."
    )

    delta = action_boosted - action_baseline
    print(
        "REACHABILITY PASS: boosted=%d baseline=%d delta=+%d"
        % (action_boosted, action_baseline, delta)
    )

    # Bonus: prove the 3rd+4th dispatch sites (donk/probe) also reference the
    # boost by name. choose_raise path alone would only give 2 kwarg passes;
    # the new v150 donk/probe wires add 2 more call sites.
    with open(os.path.join(_THIS_DIR, "strategy.py")) as f:
        src = f.read()
    n_fn_refs = src.count("_street_fold_exploit_sizing_boost")
    n_donk_wire = src.count("_donk_fold_boost = _street_fold_exploit_sizing_boost")
    n_probe_wire = src.count("_probe_fold_boost = _street_fold_exploit_sizing_boost")
    n_kwarg_refs = src.count("street_fold_boost=street_fold_boost")
    assert n_fn_refs >= 5, (
        f"Dispatch-reachability FAIL: need >=5 fn refs in strategy.py "
        f"(1 import + 2 choose_raise assigns + 1 donk + 1 probe), found {n_fn_refs}"
    )
    assert n_donk_wire == 1, (
        f"Donk dispatch wire missing: found {n_donk_wire} (expected 1)"
    )
    assert n_probe_wire == 1, (
        f"Probe dispatch wire missing: found {n_probe_wire} (expected 1)"
    )
    assert n_kwarg_refs >= 2, (
        f"Dispatch-reachability FAIL: need >=2 choose_raise kwarg passes, "
        f"found {n_kwarg_refs}"
    )
    print(
        "DISPATCH SITES PASS: strategy.py has %d fn refs (import + 2 choose_raise "
        "+ donk + probe), %d choose_raise kwargs, %d donk wire, %d probe wire"
        % (n_fn_refs, n_kwarg_refs, n_donk_wire, n_probe_wire)
    )


def main_offsuit_gate():
    """v172: prove _is_offsuit_commitment_risk flips bb_vs_raise A6o call->fold."""
    # BB holds A6o = [Ah, 6d] = [(14-2)*4+0, (6-2)*4+1] = [48, 9]
    req = {
        'my_id': 0, 'dealer_id': 0,
        'my_chips': 19900, 'opponent_chips': 19700,
        'my_cards': [48, 9], 'public_cards': [],
        'history': [{'round': 0, 'player_id': 1, 'action': 300, 'action_type': 'raise'}],
        'hand': 5, 'max_hand': 70,
    }
    std_opener = {
        'confidence': 0.50, 'vpip': 0.55, 'pfr': 0.28, 'fold_to_raise': 0.44,
        'threebet_vs_open': 0.16, 'open_response_confidence': 0.40,
        'fold_to_open_preflop': 0.42, 'aggression': 0.28, 'avg_raise_bb': 3.5,
        'flop_aggr': 0.30, 'turn_aggr': 0.28, 'river_aggr': 0.25,
        'avg_flop_raise_bb': 3.0, 'avg_turn_raise_bb': 4.5, 'avg_river_raise_bb': 5.5,
        'allin_rate': 0.04, 'postflop_aggr': 0.30, 'postflop_check_rate': 0.50,
        'barrel_freq': 0.45, 'sizing_aggr': 0.35, 'passivity_score': 0.45,
        'value_maximizer_index': 0.30,
        'fold_to_bet_flop': 0.55, 'fold_to_bet_turn': 0.50, 'fold_to_bet_river': 0.45,
        'call_down_flop_turn': 0.35, 'call_down_turn_river': 0.35,
        'sizing_tendency': {'tendency': 'standard', 'overbet_rate': 0.0,
                           'underbet_rate': 0.0, 'standard_rate': 1.0,
                           'samples': 0, 'per_street_overbet': {1: 0.0, 2: 0.0, 3: 0.0},
                           'per_street_underbet': {1: 0.0, 2: 0.0, 3: 0.0},
                           'per_street_samples': {1: 0, 2: 0, 3: 0}, 'confidence': 0.0},
        'open_response_samples': 10,
    }
    real_build = strategy.build_opponent_model
    strategy.build_opponent_model = lambda _r, _i: std_opener
    try:
        action_gated = strategy.get_action(req, [req])
        real_pred = strategy._is_offsuit_commitment_risk
        strategy._is_offsuit_commitment_risk = lambda _c: False
        action_ungated = strategy.get_action(req, [req])
        strategy._is_offsuit_commitment_risk = real_pred
    finally:
        strategy.build_opponent_model = real_build
    print('offsuit_gate GATED action:', action_gated, ' UNGATED action:', action_ungated)
    assert action_gated == -1, ('REACHABILITY FAIL: gated A6o bb_vs_raise action=%r, expected -1 (fold).' % action_gated)
    assert action_ungated == 0, ('REACHABILITY FAIL: ungated A6o bb_vs_raise action=%r, expected 0 (call).' % action_ungated)
    print('REACHABILITY PASS: offsuit gate flips A6o bb_vs_raise call(0)->fold(-1)')


def main_reraise_tighten_widened():
    """v176 crossover mutation: prove _river_reraise_tighten fires for the
    newly-covered marginal band (made_strength 0.40-0.55). Before v176, the
    gate was 0.55-0.80 so these hands were exempt.
    """
    # River re-raise spot: opp re-raised 3x (ratio=3.0), paired board, made=0.45
    state_high = {'my_round_bet': 300, 'round_raise': 900}
    facing = {'facing_postflop_aggression': True}
    bt_paired = {'paired': True, 'flush_pressure': 0.0, 'straight_pressure': 0.0}
    vp_thin = {'tier': 'thin'}

    # v176: marginal hand (0.45) now fires — before v176 it returned 0.0
    delta_marginal = strategy_helpers._river_reraise_tighten(
        state_high, facing, 0.45, vp_thin, 3, bt_paired, None)
    assert delta_marginal > 0.0, (
        f"REACHABILITY FAIL: v176 marginal made=0.45 should fire, "
        f"got {delta_marginal:.4f}"
    )

    # still below floor: made=0.35 should NOT fire
    delta_below = strategy_helpers._river_reraise_tighten(
        state_high, facing, 0.35, vp_thin, 3, bt_paired, None)
    assert delta_below == 0.0, (
        f"REACHABILITY FAIL: made=0.35 below 0.40 floor should not fire, "
        f"got {delta_below:.4f}"
    )

    # verify the delta scales: marginal (0.45) > strong (0.62) due to
    # strength_factor scaling
    delta_strong = strategy_helpers._river_reraise_tighten(
        state_high, facing, 0.62, vp_thin, 3, bt_paired, None)
    assert delta_marginal > delta_strong, (
        f"REACHABILITY FAIL: marginal {delta_marginal:.4f} should be > "
        f"strong {delta_strong:.4f} due to strength_factor scaling"
    )

    print(
        "REACHABILITY PASS: v176 widened reraise tighten fires for "
        "marginal 0.45 (delta=%.4f) > strong 0.62 (delta=%.4f)"
        % (delta_marginal, delta_strong)
    )


def main_turn_board_danger_margin():
    """v180: prove _turn_board_danger_margin fires for the documented -20k
    turn stack-off scenarios (G5H12 flush, G10H14 straight) and is correctly
    gated off for benign boards, river, and strong-tier hands.

    Direct unit calls against strategy._turn_board_danger_margin (the function
    is defined in strategy.py, NOT strategy_helpers.py which is at 2500/2500
    EXACT CAP). Mirrors main_reraise_tighten_widened() scaffolding.
    """
    facing = {'facing_postflop_aggression': True}
    vp_none = {'tier': 'none'}
    vp_strong = {'tier': 'strong'}

    # 1. G5H12 scenario: pair of 4s (made~0.22) on JsKs4s7s3s (4 spades ->
    #    flush_pressure=1.0), opp allin, pot_odds~0.50. High-danger fold margin.
    bt_flush = {'flush_pressure': 1.0, 'straight_pressure': 0.0}
    delta_g5h12 = strategy._turn_board_danger_margin(
        2, 0.22, 0.05, vp_none, bt_flush, facing, 0.50)
    assert delta_g5h12 >= 0.08, (
        "REACHABILITY FAIL: G5H12 flush-danger made=0.22 should fire "
        "delta>=0.08, got %.4f" % delta_g5h12)

    # 2. G10H14 scenario: pair of Qs (made~0.30) on 3cJhQdKs6h (K-Q-J
    #    open-ended straight draw -> straight_pressure=0.65), pot_odds~0.45.
    bt_straight = {'flush_pressure': 0.0, 'straight_pressure': 0.65}
    delta_g10h14 = strategy._turn_board_danger_margin(
        2, 0.30, 0.08, vp_none, bt_straight, facing, 0.45)
    assert delta_g10h14 >= 0.04, (
        "REACHABILITY FAIL: G10H14 straight-danger made=0.30 should fire "
        "delta>=0.04 (M5 floor), got %.4f" % delta_g10h14)

    # 3. Benign turn: made=0.30, board straight_pressure=0.28 (gutshot-only,
    #    below 0.65 danger gate). Function MUST return 0.0 (no margin added).
    bt_benign = {'flush_pressure': 0.0, 'straight_pressure': 0.28}
    delta_benign = strategy._turn_board_danger_margin(
        2, 0.30, 0.05, vp_none, bt_benign, facing, 0.40)
    assert delta_benign == 0.0, (
        "REACHABILITY FAIL: benign board (danger 0.28 < 0.65) must return "
        "0.0, got %.4f" % delta_benign)

    # 4. River guard: round_idx=3 (NOT turn). v177 handles river; this function
    #    is TURN-scoped and MUST return 0.0 on the river.
    delta_river = strategy._turn_board_danger_margin(
        3, 0.22, 0.05, vp_none, bt_flush, facing, 0.50)
    assert delta_river == 0.0, (
        "REACHABILITY FAIL: river (round_idx=3) must return 0.0 "
        "(turn-scoped), got %.4f" % delta_river)

    # 5. Strong-tier guard: made=0.30 but tier='strong'. Strong hands
    #    legitimately call down, so the margin MUST NOT fire.
    delta_strong = strategy._turn_board_danger_margin(
        2, 0.30, 0.05, vp_strong, bt_flush, facing, 0.50)
    assert delta_strong == 0.0, (
        "REACHABILITY FAIL: tier='strong' must bypass (calls down), "
        "got %.4f" % delta_strong)

    # Severity ordering: G5H12 (flush=1.0) > G10H14 (straight=0.65 at floor)
    assert delta_g5h12 >= delta_g10h14, (
        "REACHABILITY FAIL: G5H12 flush (delta=%.4f) should be >= "
        "G10H14 straight (delta=%.4f) due to higher board danger"
        % (delta_g5h12, delta_g10h14))

    print(
        "REACHABILITY PASS: v180 turn board-danger margin fires G5H12=%.4f "
        "(>=0.08), G10H14=%.4f (>=0.04); benign=%.4f, river=%.4f, strong=%.4f "
        "all correctly 0.0" % (delta_g5h12, delta_g10h14, delta_benign,
                                delta_river, delta_strong))


def main_spr_value_ship():
    """v181: prove _spr_calibrated_value_ship fires for low-SPR nut/strong
    hands and is correctly gated off for marginal hands and deep SPR.

    Direct unit calls against strategy._spr_calibrated_value_ship (defined in
    strategy.py, NOT strategy_helpers.py which is at 2500/2500 EXACT CAP).
    Mirrors main_turn_board_danger_margin() and main_reraise_tighten_widened()
    scaffolding.
    """
    bt_safe = {'flush_pressure': 0.0, 'straight_pressure': 0.0}
    opp_conf = {'confidence': 0.50}

    # 1. Nut trips on low-SPR turn: SPR~1.5 (my_chips=3000, pot=2000),
    #    tier='nut', made=0.72, safe board -> near-ship (>= my_chips*0.9).
    amt_nut_turn = strategy._spr_calibrated_value_ship(
        2, 0, 0.72, {'tier': 'nut'}, bt_safe, opp_conf,
        2000, 3000, 100, 0, 0.0)
    assert amt_nut_turn is not None and amt_nut_turn >= int(3000 * 0.9), (
        "REACHABILITY FAIL: nut trips low-SPR turn should near-ship "
        "(>=2700), got %r" % amt_nut_turn)

    # 2. Nut flush on low-SPR river: SPR~2.0 (my_chips=4000, pot=2000),
    #    tier='nut', made=0.80, safe board -> full ship (== my_chips).
    amt_nut_river = strategy._spr_calibrated_value_ship(
        3, 0, 0.80, {'tier': 'nut'}, bt_safe, opp_conf,
        2000, 4000, 100, 0, 0.0)
    assert amt_nut_river is not None and amt_nut_river == 4000, (
        "REACHABILITY FAIL: nut flush low-SPR river should ship "
        "(==4000), got %r" % amt_nut_river)

    # 3. Strong two-pair on mid-SPR turn: SPR~3.5 (my_chips=3500, pot=1000),
    #    tier='strong', made=0.60, safe board -> ~55% of stack.
    amt_strong_turn = strategy._spr_calibrated_value_ship(
        2, 0, 0.60, {'tier': 'strong'}, bt_safe, opp_conf,
        1000, 3500, 100, 0, 0.0)
    _lo, _hi = int(3500 * 0.45), int(3500 * 0.65)
    assert amt_strong_turn is not None and _lo <= amt_strong_turn <= _hi, (
        "REACHABILITY FAIL: strong two-pair mid-SPR turn should commit "
        "in [%d,%d], got %r" % (_lo, _hi, amt_strong_turn))

    # 4. Negative: marginal pair (tier='thin', made=0.35) -> None.
    amt_marginal = strategy._spr_calibrated_value_ship(
        3, 0, 0.35, {'tier': 'thin'}, bt_safe, opp_conf,
        2000, 3000, 100, 0, 0.0)
    assert amt_marginal is None, (
        "REACHABILITY FAIL: marginal pair (made=0.35, tier=thin) must "
        "return None, got %r" % amt_marginal)

    # 5. Negative: high SPR (SPR=6.0, tier='nut') -> None.
    amt_deep = strategy._spr_calibrated_value_ship(
        2, 0, 0.80, {'tier': 'nut'}, bt_safe, opp_conf,
        500, 3000, 100, 0, 0.0)
    assert amt_deep is None, (
        "REACHABILITY FAIL: high SPR (6.0) must return None, got %r" % amt_deep)

    # v182 NEW: 6. Donk-path SPR ship scenario — river, nut, SPR=2.5.
    #    round_idx=3, to_call=0, made=0.70, tier='nut', safe board.
    #    SPR=2.5 (my_chips=2500, pot=1000) -> near-ship 75% = 1875.
    amt_donk_river = strategy._spr_calibrated_value_ship(
        3, 0, 0.70, {'tier': 'nut'}, bt_safe, opp_conf,
        1000, 2500, 100, 0, 0.0)
    assert amt_donk_river is not None and amt_donk_river > 0, (
        "REACHABILITY FAIL: donk-path nut SPR=2.5 river should ship "
        "(>0), got %r" % amt_donk_river)

    # v182 NEW: 7. Probe-path SPR ship scenario — turn, strong, SPR=1.5.
    #    round_idx=2, to_call=0, made=0.60, tier='strong', safe board.
    #    SPR=1.5 (my_chips=3000, pot=2000) -> tier-differentiated 75% = 2250
    #    (NOT 100% ship — v182 change_c: strong hands preserve 25% behind).
    amt_probe_turn = strategy._spr_calibrated_value_ship(
        2, 0, 0.60, {'tier': 'strong'}, bt_safe, opp_conf,
        2000, 3000, 100, 0, 0.0)
    _strong_lo, _strong_hi = int(3000 * 0.70), int(3000 * 0.80)
    assert (amt_probe_turn is not None
            and _strong_lo <= amt_probe_turn <= _strong_hi), (
        "REACHABILITY FAIL: probe-path strong SPR=1.5 turn should "
        "tier-differentiate to ~75%% (in [%d,%d]), got %r"
        % (_strong_lo, _strong_hi, amt_probe_turn))
    # Explicitly verify NOT full ship (tier-differentiation working).
    assert amt_probe_turn < 3000, (
        "REACHABILITY FAIL: strong hand at SPR<=2.0 must NOT full-ship "
        "(should be 75%% per v182 change_c), got %r" % amt_probe_turn)

    print(
        "REACHABILITY PASS: v181/v182 SPR value ship nut_turn=%d (>=2700), "
        "nut_river=%d (==4000), strong_turn=%d (in [%d,%d]), marginal=%s, "
        "deep_spr=%s, donk_river=%d (>0), probe_strong_turn=%d (~75%%) "
        "all correct"
        % (amt_nut_turn, amt_nut_river, amt_strong_turn, _lo, _hi,
           amt_marginal, amt_deep, amt_donk_river, amt_probe_turn))


def main_aggro_bluffcatcher_fold():
    """v183: prove _aggro_bluffcatcher_should_fold fires for a confirmed aggro
    opponent with a bluff-catcher hand (made ~0.25, pot_odds 0.50) and does NOT
    fire for a standard opponent or a strong hand.

    Mirrors the direct unit-call scaffolding of main_spr_value_ship(). Tests
    opponent._aggro_bluffcatcher_should_fold directly (NOT via get_action, since
    assembling a realistic turn request with a 3-street value-heavy line is
    brittle; the function-level proof is the correct reachability evidence).
    """
    import opponent

    # Confirmed aggro: high postflop aggression + PFR + low fold + polarized sizing
    aggro = {
        'confidence': 0.50, 'vpip': 0.60, 'pfr': 0.42, 'postflop_aggr': 0.62,
        'fold_to_raise': 0.28, 'river_aggr': 0.52, 'allin_rate': 0.14,
        'large_bet_ratio': 0.58, 'value_maximizer_index': 0.30,
        'passivity_score': 0.20,
    }
    archetype, conf = opponent.classify_archetype(aggro)
    assert archetype == 'aggro', (
        "REACHABILITY FAIL: aggro mock should classify as 'aggro', got %r"
        % archetype)

    # 1. Bluff-catcher fold fires: turn, made=0.25, to_call=2000, pot=2000
    #    pot_odds = 2000/(2000+2000) = 0.50; ev_margin = 0.50 - 0.25 = 0.25 > 0.10
    vp_thin = {'tier': 'thin'}
    folds_aggro = opponent._aggro_bluffcatcher_should_fold(
        aggro, 2, 2000, 2000, 0.25, vp_thin, 0.05)
    assert folds_aggro is True, (
        "REACHABILITY FAIL: aggro bluff-catcher (made=0.25, pot_odds=0.50) "
        "should fold, got %r" % folds_aggro)

    # 2. Strong hand does NOT fold: made=0.50 -> tier='strong' exempts + outside band
    vp_strong = {'tier': 'strong'}
    no_fold_strong = opponent._aggro_bluffcatcher_should_fold(
        aggro, 2, 2000, 2000, 0.50, vp_strong, 0.05)
    assert no_fold_strong is False, (
        "REACHABILITY FAIL: strong hand (made=0.50, tier='strong') must NOT "
        "fold, got %r" % no_fold_strong)

    # 3. Standard opponent does NOT fold (no aggro archetype confirmation)
    std = {
        'confidence': 0.50, 'vpip': 0.58, 'pfr': 0.28, 'postflop_aggr': 0.36,
        'fold_to_raise': 0.44, 'river_aggr': 0.28, 'allin_rate': 0.05,
        'large_bet_ratio': 0.32, 'value_maximizer_index': 0.40,
        'passivity_score': 0.50,
    }
    archetype_std, _ = opponent.classify_archetype(std)
    assert archetype_std == 'standard', (
        "REACHABILITY FAIL: standard mock should classify as 'standard', got %r"
        % archetype_std)
    no_fold_std = opponent._aggro_bluffcatcher_should_fold(
        std, 2, 2000, 2000, 0.25, vp_thin, 0.05)
    assert no_fold_std is False, (
        "REACHABILITY FAIL: standard opponent must NOT fold bluff-catcher, "
        "got %r" % no_fold_std)

    # 4. Negative: small bet (pot_odds < 0.33) does NOT fold even vs aggro
    no_fold_small = opponent._aggro_bluffcatcher_should_fold(
        aggro, 2, 300, 2000, 0.25, vp_thin, 0.05)
    assert no_fold_small is False, (
        "REACHABILITY FAIL: small bet (pot_odds < 0.33) must NOT fold, got %r"
        % no_fold_small)

    print(
        "REACHABILITY PASS: v183 aggro bluff-catcher fold fires vs aggro "
        "(made=0.25 pot_odds=0.50); NOT vs standard; NOT for strong hand "
        "(made=0.50); NOT for small bet")


def main_rock_value_bet_fold():
    """v184: prove _rock_value_bet_fold fires for a confirmed rock opponent
    with a marginal hand (made ~0.30, pot_odds 0.50) and does NOT fire for
    a standard opponent, a strong-tier hand, or a small bet.

    Mirrors the direct unit-call scaffolding of main_aggro_bluffcatcher_fold().
    Tests opponent._rock_value_bet_fold directly (NOT via get_action).
    """
    import opponent

    # Confirmed rock: tight (low vpip), high fold-to-raise, low aggression.
    rock = {
        'confidence': 0.50, 'vpip': 0.35, 'pfr': 0.15, 'postflop_aggr': 0.22,
        'fold_to_raise': 0.60, 'river_aggr': 0.18, 'allin_rate': 0.03,
        'large_bet_ratio': 0.25, 'value_maximizer_index': 0.25,
        'passivity_score': 0.65,
    }
    archetype, conf = opponent.classify_archetype(rock)
    assert archetype == 'rock', (
        "REACHABILITY FAIL: rock mock should classify as 'rock', got %r"
        % archetype)

    # 1. Rock value-bet fold fires: turn, made=0.30, to_call=2000, pot=2000
    #    pot_odds = 0.50; ev_margin = 0.50 - 0.30 = 0.20 > 0.07
    vp_thin = {'tier': 'thin'}
    folds_rock = opponent._rock_value_bet_fold(
        rock, 2, 2000, 2000, 0.30, vp_thin, 0.05)
    assert folds_rock is True, (
        "REACHABILITY FAIL: rock value-bet fold (made=0.30, pot_odds=0.50) "
        "should fold, got %r" % folds_rock)

    # 2. Strong-tier hand does NOT fold: tier='strong' exempts.
    vp_strong = {'tier': 'strong'}
    no_fold_strong = opponent._rock_value_bet_fold(
        rock, 2, 2000, 2000, 0.40, vp_strong, 0.05)
    assert no_fold_strong is False, (
        "REACHABILITY FAIL: strong-tier hand must NOT fold vs rock, got %r"
        % no_fold_strong)

    # 3. Standard opponent does NOT fold (no rock archetype confirmation).
    std = {
        'confidence': 0.50, 'vpip': 0.58, 'pfr': 0.28, 'postflop_aggr': 0.36,
        'fold_to_raise': 0.44, 'river_aggr': 0.28, 'allin_rate': 0.05,
        'large_bet_ratio': 0.32, 'value_maximizer_index': 0.40,
        'passivity_score': 0.50,
    }
    archetype_std, _ = opponent.classify_archetype(std)
    assert archetype_std == 'standard', (
        "REACHABILITY FAIL: standard mock should classify as 'standard', got %r"
        % archetype_std)
    no_fold_std = opponent._rock_value_bet_fold(
        std, 2, 2000, 2000, 0.30, vp_thin, 0.05)
    assert no_fold_std is False, (
        "REACHABILITY FAIL: standard opponent must NOT trigger rock fold, "
        "got %r" % no_fold_std)

    # 4. Negative: small bet (pot_odds < 0.33) does NOT fold even vs rock.
    no_fold_small = opponent._rock_value_bet_fold(
        rock, 2, 300, 2000, 0.30, vp_thin, 0.05)
    assert no_fold_small is False, (
        "REACHABILITY FAIL: small bet (pot_odds < 0.33) must NOT fold, got %r"
        % no_fold_small)

    print(
        "REACHABILITY PASS: v184 rock value-bet fold fires vs rock "
        "(made=0.30 pot_odds=0.50); NOT vs standard; NOT for strong-tier; "
        "NOT for small bet")


def main_bluff_awareness_guard():
    """v184: prove the _opp_bluff_prone guard suppresses fold gates vs bluff-prone
    opponents (the v172 4W-12L regression fix) while still folding vs value-polarized
    bettors.

    Uses the master-specified _opp_bluff_prone signals:
      - barreling_bluffy = (large_bet_ratio < 0.50) and (postflop_aggr >= 0.40)
      - loose_caller = fold_to_bet < 0.35
    """
    import opponent
    vp_thin = {'tier': 'thin'}
    # Bluffy-tight opponent (v172-like): tight-ish preflop but barrels bluffs postflop.
    # low large_bet_ratio + high postflop_aggr => _opp_bluff_prone True.
    bluff = {'confidence': 0.50, 'vpip': 0.48, 'pfr': 0.22, 'postflop_aggr': 0.52,
             'fold_to_raise': 0.42, 'fold_to_bet': 0.30, 'river_aggr': 0.46,
             'allin_rate': 0.08, 'large_bet_ratio': 0.32, 'value_maximizer_index': 0.35,
             'passivity_score': 0.30}
    assert opponent._opp_bluff_prone(bluff) is True, 'bluffy opp must be bluff-prone'
    # Aggro fold must NOT fire vs bluff-prone opp (the regression fix):
    assert opponent._aggro_bluffcatcher_should_fold(bluff, 2, 2000, 2000, 0.30, vp_thin, 0.05) is False
    # Value-polarized bettor: high large_bet_ratio => NOT bluff-prone => fold still fires.
    value = {'confidence': 0.50, 'vpip': 0.58, 'pfr': 0.30, 'postflop_aggr': 0.58,
             'fold_to_raise': 0.40, 'fold_to_bet': 0.45, 'river_aggr': 0.50,
             'allin_rate': 0.12, 'large_bet_ratio': 0.72, 'value_maximizer_index': 0.45,
             'passivity_score': 0.25}
    assert opponent._opp_bluff_prone(value) is False, 'value-polarized opp must NOT be bluff-prone'
    assert opponent._aggro_bluffcatcher_should_fold(value, 2, 2000, 2000, 0.30, vp_thin, 0.05) is True
    # Rock fold: a value-polarized rock still folds; a bluffy rock does not.
    rock_value = dict(value); rock_value['fold_to_raise'] = 0.62; rock_value['vpip'] = 0.35; rock_value['postflop_aggr'] = 0.18
    a, _ = opponent.classify_archetype(rock_value)
    assert a == 'rock', f'rock_value should classify rock, got {a}'
    assert opponent._rock_value_bet_fold(rock_value, 2, 2000, 2000, 0.25, vp_thin, 0.05) is True
    rock_bluffy = dict(rock_value); rock_bluffy['postflop_aggr'] = 0.45; rock_bluffy['large_bet_ratio'] = 0.30
    # if rock_bluffy still classifies rock, the guard must suppress the fold:
    if opponent.classify_archetype(rock_bluffy)[0] == 'rock':
        assert opponent._rock_value_bet_fold(rock_bluffy, 2, 2000, 2000, 0.25, vp_thin, 0.05) is False
    print('REACHABILITY PASS: v184 bluff-awareness guard suppresses folds vs bluffy opps; fires vs value-polarized')


def main_texture_bluff():
    """v185: prove _board_texture_bluff_raise FIRES on an A-high river board with
    an air hand (made=0.10) vs a standard opponent (fold_to_raise=0.44,
    confidence=0.50), and does NOT fire on a low-card unpaired dry board
    (high_card=7).

    Tests opponent._board_texture_bluff_raise directly (NOT via get_action).
    """
    import opponent

    # Standard opponent with moderate fold_to_raise and confidence.
    std = {
        'confidence': 0.50, 'vpip': 0.58, 'pfr': 0.28, 'postflop_aggr': 0.36,
        'fold_to_raise': 0.44, 'river_aggr': 0.28, 'allin_rate': 0.05,
        'large_bet_ratio': 0.32, 'value_maximizer_index': 0.40,
        'passivity_score': 0.50,
    }

    # 1. FIRES: A-high river board, air hand (made=0.10), draw=0.05.
    #    high_card=14 -> boost=0.12 (ace_high). fold_equity = 0.44 + 0.12*0.50
    #    = 0.50. bluff_ratio = 0.60 + 0.15*0.50 = 0.675. target = int(1000*0.675)
    #    = 675. bet = max(100, 675-0) = 675. ev_threshold = 675/1675 = 0.403.
    #    fold_equity(0.50) > 0.403 -> FIRES. 675 < 15000*0.72 (no all-in cap).
    a_high_board = {
        'wetness': 0.15, 'flush_pressure': 0.0, 'straight_pressure': 0.0,
        'paired': False, 'high_card': 14, 'dynamic': False,
    }
    vp_thin = {'tier': 'thin'}
    fires = opponent._board_texture_bluff_raise(
        std, 3, 0, 0.10, 0.05, a_high_board, vp_thin, 1000, 15000, 100, 0)
    assert fires is not None, (
        "REACHABILITY FAIL: board-texture bluff raise should FIRE on A-high "
        "river board (made=0.10, fold_to_raise=0.44, conf=0.50), got %r" % fires)
    assert isinstance(fires, int) and 100 <= fires <= 9750, (
        "REACHABILITY FAIL: fired bluff bet must be in [100, 9750], got %r"
        % fires)
    assert fires < 15000 * 0.72, (
        "REACHABILITY FAIL: fired bluff bet must be < 72% of chips, got %r"
        % fires)

    # 2. DOES NOT FIRE: low-card unpaired dry board (high_card=7).
    #    high_card=7 < 13, not paired, flush_p=0.0 < 0.75 -> boost=0.0 < 0.06
    #    -> returns None.
    low_board = {
        'wetness': 0.10, 'flush_pressure': 0.0, 'straight_pressure': 0.0,
        'paired': False, 'high_card': 7, 'dynamic': False,
    }
    no_fire = opponent._board_texture_bluff_raise(
        std, 3, 0, 0.10, 0.05, low_board, vp_thin, 1000, 15000, 100, 0)
    assert no_fire is None, (
        "REACHABILITY FAIL: board-texture bluff raise must NOT fire on low-card "
        "dry board (high_card=7), got %r" % no_fire)

    print(
        "REACHABILITY PASS: v185 board-texture bluff raise FIRES on A-high "
        "river board (made=0.10); does NOT fire on low-card dry board")


def main_river_potodds_equity_margin():
    """v189: prove _river_potodds_equity_margin FIRES (>0 delta) for weak
    one-pair / weak-two-pair made hands facing a >=0.4x pot river bet where
    CALIBRATED equity (made_strength * POLARIZATION_DISCOUNT) is below the
    price (pot_odds), and does NOT fire for strong-tier hands, draw hands,
    overbettor opponents, or bets below the 0.4x floor.

    v189 FIX: the v188 raw `pot_odds - made_strength` gate was DEAD for the
    0.28-0.50 made band (made_strength 0.30 > pot_odds 0.273 -> gap<=0). The
    polarization discount (0.65) maps made_strength to ~0.195 equity vs a
    polarized range so the gate fires across the full leak band. The leak-band
    floor is also lowered from 0.50x to 0.40x pot.

    Tests opponent._river_potodds_equity_margin directly (NOT via get_action).
    The positive delta raises the `realized_rate < pot_odds + call_margin`
    threshold at strategy.py, converting leak-band calls into folds.
    """
    import opponent

    vp_thin = {'tier': 'thin'}

    # FIRE 1: made=0.25 one-pair facing 0.6x pot river bet, pot_odds=0.30.
    #    v189: calibrated_equity = 0.25 * 0.65 = 0.1625.
    #    equity_gap = 0.30 - 0.1625 = 0.1375 > 0 -> FIRES.
    #    delta = (0.48 - 0.30) + 0.1375*0.5 = 0.249.
    spot_06x = {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.60}
    delta_fire1 = opponent._river_potodds_equity_margin(
        3, 0.25, 0.05, vp_thin, spot_06x, 0.30, None)
    assert delta_fire1 > 0.0, (
        "REACHABILITY FAIL: made=0.25 facing 0.6x pot (pot_odds=0.30) should "
        "fire delta>0, got %.4f" % delta_fire1)

    # FIRE 2: made=0.30 medium-pair facing 0.6x pot, pot_odds=0.273.
    #    v189 REGRESSION: was DEAD in v188 (made_strength 0.30 > pot_odds 0.273).
    #    calibrated_equity = 0.30 * 0.65 = 0.195 -> gap = 0.078 -> FIRES.
    #    delta = (0.48 - 0.273) + 0.078*0.5 = 0.246.
    delta_fire2 = opponent._river_potodds_equity_margin(
        3, 0.30, 0.05, vp_thin, spot_06x, 0.273, None)
    assert delta_fire2 > 0.0, (
        "REACHABILITY FAIL: made=0.30 facing 0.6x pot (pot_odds=0.273) must "
        "fire delta>0 in v189 (was DEAD in v188), got %.4f" % delta_fire2)

    # FIRE 3b: made=0.40 weak-two-pair facing 0.75x pot, pot_odds=0.300.
    #    v189 REGRESSION: was DEAD in v188 (made_strength 0.40 > pot_odds 0.30).
    #    calibrated_equity = 0.40 * 0.65 = 0.26 -> gap = 0.04 -> FIRES.
    #    delta = (0.48 - 0.30) + 0.04*0.5 = 0.20.
    spot_075x = {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.75}
    delta_fire3b = opponent._river_potodds_equity_margin(
        3, 0.40, 0.05, vp_thin, spot_075x, 0.300, None)
    assert delta_fire3b > 0.0, (
        "REACHABILITY FAIL: made=0.40 facing 0.75x pot (pot_odds=0.300) must "
        "fire delta>0 in v189 (was DEAD in v188), got %.4f" % delta_fire3b)

    # FIRE 3c: made=0.30 at new floor 0.40x pot, pot_odds=0.222.
    #    v189 NEW BOUNDARY: bet_ratio 0.40 is now >= floor (was <0.50, exempt).
    #    calibrated_equity = 0.195 -> gap = 0.027 -> FIRES.
    #    delta = (0.48 - 0.222) + 0.027*0.5 = 0.272 -> clamped 0.25.
    spot_04x = {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.40}
    delta_fire3c = opponent._river_potodds_equity_margin(
        3, 0.30, 0.05, vp_thin, spot_04x, 0.222, None)
    assert delta_fire3c > 0.0, (
        "REACHABILITY FAIL: made=0.30 at 0.40x pot (new floor) must fire "
        "delta>0, got %.4f" % delta_fire3c)

    # NON-FIRE 1: strong-tier hand must NOT fire even in leak made-band.
    spot_07x = {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.70}
    vp_strong = {'tier': 'strong'}
    delta_strong = opponent._river_potodds_equity_margin(
        3, 0.40, 0.05, vp_strong, spot_07x, 0.45, None)
    assert delta_strong == 0.0, (
        "REACHABILITY FAIL: strong-tier hand must be exempt, got %.4f"
        % delta_strong)

    # NON-FIRE 2: draw_strength>=0.15 must NOT fire (combo draws keep equity).
    delta_draw = opponent._river_potodds_equity_margin(
        3, 0.30, 0.18, vp_thin, spot_06x, 0.35, None)
    assert delta_draw == 0.0, (
        "REACHABILITY FAIL: draw_strength>=0.15 must be exempt, got %.4f"
        % delta_draw)

    # NON-FIRE 3: overbettor opponent must NOT fire (polarized range has bluffs).
    overbettor = {
        'confidence': 0.50, 'vpip': 0.55, 'pfr': 0.28, 'postflop_aggr': 0.40,
        'fold_to_raise': 0.40, 'river_aggr': 0.30, 'allin_rate': 0.10,
        'large_bet_ratio': 0.55, 'value_maximizer_index': 0.35,
        'passivity_score': 0.35,
        'sizing_tendency': {
            'tendency': 'overbettor', 'overbet_rate': 0.50,
            'underbet_rate': 0.0, 'standard_rate': 0.50,
            'samples': 12, 'confidence': 0.45,
            'per_street_overbet': {1: 0.0, 2: 0.0, 3: 0.0},
            'per_street_underbet': {1: 0.0, 2: 0.0, 3: 0.0},
            'per_street_samples': {1: 0, 2: 0, 3: 0},
        },
    }
    delta_overbettor = opponent._river_potodds_equity_margin(
        3, 0.30, 0.05, vp_thin, spot_06x, 0.40, overbettor)
    assert delta_overbettor == 0.0, (
        "REACHABILITY FAIL: overbettor opponent must be exempt, got %.4f"
        % delta_overbettor)

    # NON-FIRE 4 (bonus): bet<0.4x pot must NOT fire (below v189 leak floor).
    spot_small = {'facing_postflop_aggression': True, 'last_raise_pot_ratio': 0.35}
    delta_small = opponent._river_potodds_equity_margin(
        3, 0.30, 0.05, vp_thin, spot_small, 0.40, None)
    assert delta_small == 0.0, (
        "REACHABILITY FAIL: bet<0.4x pot must be exempt, got %.4f"
        % delta_small)

    print(
        "REACHABILITY PASS: v189 river pot-odds-equity margin FIRES for "
        "made=0.25(%.4f), 0.30@0.6x(%.4f), 0.40@0.75x(%.4f), 0.30@0.4x(%.4f); "
        "does NOT fire for strong(%.4f), draw(%.4f), overbettor(%.4f), "
        "small_bet(%.4f)"
        % (delta_fire1, delta_fire2, delta_fire3b, delta_fire3c,
           delta_strong, delta_draw, delta_overbettor, delta_small))


if __name__ == "__main__":
    main()
    main_offsuit_gate()
    main_reraise_tighten_widened()
    main_turn_board_danger_margin()
    main_spr_value_ship()
    main_aggro_bluffcatcher_fold()
    main_rock_value_bet_fold()
    main_bluff_awareness_guard()
    main_texture_bluff()
    main_river_potodds_equity_margin()
