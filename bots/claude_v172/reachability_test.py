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


if __name__ == "__main__":
    main()
    main_offsuit_gate()
