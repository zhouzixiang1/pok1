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


def _std_opponent_model():
    """Standard-shape opponent profile with neutral/standard reads.

    Used by the v172/v173 offsuit-gate reachability tests. Includes every key
    the live build_opponent_model returns so downstream code never KeyErrors.
    vpid/conf are tuned to a 'standard' opener (NOT ultra-loose) so the A2o-A5o
    carve-out does NOT fire -- the gate stays on for weak offsuit vs this opp.
    """
    return {
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


def main_offsuit_gate():
    """v172/v173: prove _is_offsuit_commitment_risk flips bb_vs_raise A6o
    (actually A4o via [48,9]) call->fold vs a STANDARD opener.

    v173 note: the predicate now takes (my_cards, opponent_model). vs the
    standard opener the A2o-A5o carve-out does NOT fire, so this still proves
    the gate folds weak offsuit. The monkeypatch is updated to the 2-arg
    signature.
    """
    # BB holds A6o labeled = [Ah, 6d-historical] but integers [48,9] = Ah,4d (A4o).
    # Either way: offsuit non-pair, high=14, low in gated range -> gate fires.
    req = {
        'my_id': 0, 'dealer_id': 0,
        'my_chips': 19900, 'opponent_chips': 19700,
        'my_cards': [48, 9], 'public_cards': [],
        'history': [{'round': 0, 'player_id': 1, 'action': 300, 'action_type': 'raise'}],
        'hand': 5, 'max_hand': 70,
    }
    std_opener = _std_opponent_model()
    real_build = strategy.build_opponent_model
    strategy.build_opponent_model = lambda _r, _i: std_opener
    try:
        action_gated = strategy.get_action(req, [req])
        real_pred = strategy._is_offsuit_commitment_risk
        # v173: predicate signature is now (my_cards, opponent_model=None).
        strategy._is_offsuit_commitment_risk = lambda _c, _o=None: False
        action_ungated = strategy.get_action(req, [req])
        strategy._is_offsuit_commitment_risk = real_pred
    finally:
        strategy.build_opponent_model = real_build
    print('offsuit_gate GATED action:', action_gated, ' UNGATED action:', action_ungated)
    assert action_gated == -1, ('REACHABILITY FAIL: gated A4o bb_vs_raise action=%r, expected -1 (fold).' % action_gated)
    assert action_ungated == 0, ('REACHABILITY FAIL: ungated A4o bb_vs_raise action=%r, expected 0 (call).' % action_ungated)
    print('REACHABILITY PASS: offsuit gate flips A4o bb_vs_raise call(0)->fold(-1) vs standard opener')


def main_offsuit_gate_sb_open():
    """v173: prove the offsuit gate fires at sb_open (K9o raise->limp).

    SB holds K9o = [Kh, 9c] = [(13-2)*4+0, (9-2)*4+3] = [44, 31]. K9o is a
    dominated offsuit (high=13, low=9, not protected by broadway/A9o clauses)
    so _is_offsuit_commitment_risk returns True.

    To exercise the gate's raise->limp downgrade we need the bucket to actually
    RAISE K9o first. _sb_open_bucket_action raises 'playable' (K9o) only vs a
    high-folding BB (fold_to_open_preflop>=0.55, open_response_confidence>=0.25,
    threebet<=0.18). vs a standard opener K9o is a flat 'call' and the gate is a
    no-op, so this test uses a high-fold opener to make the dispatch reachable.

    Asserts: gated action==0 (limp), ungated action>0 (raise). The flip is the
    only rigorous proof the sb_open dispatch site of the gate is live.
    """
    req = {
        'my_id': 1, 'dealer_id': 0,           # dealer=0 -> sb=1, so my_id=1 is SB
        'my_chips': 19950, 'opponent_chips': 19900,
        'my_cards': [44, 31], 'public_cards': [],   # Kh, 9c (K9o)
        'history': [],                              # SB first to act, no prior action
        'hand': 5, 'max_hand': 70,
    }
    # High-folding BB so the bucket raises 'playable' K9o (otherwise the gate
    # path is structurally inert at sb_open -- a no-op worth flagging).
    high_fold_opener = _std_opponent_model()
    high_fold_opener['fold_to_open_preflop'] = 0.60
    high_fold_opener['open_response_confidence'] = 0.40   # >=0.25 read gate
    high_fold_opener['threebet_vs_open'] = 0.16           # <=0.18
    real_build = strategy.build_opponent_model
    strategy.build_opponent_model = lambda _r, _i: high_fold_opener
    try:
        action_gated = strategy.get_action(req, [req])
        real_pred = strategy._is_offsuit_commitment_risk
        strategy._is_offsuit_commitment_risk = lambda _c, _o=None: False
        action_ungated = strategy.get_action(req, [req])
        strategy._is_offsuit_commitment_risk = real_pred
    finally:
        strategy.build_opponent_model = real_build
    print('sb_open K9o GATED action:', action_gated, ' UNGATED action:', action_ungated)
    assert action_gated == 0, (
        'REACHABILITY FAIL: gated K9o sb_open action=%r, expected 0 (limp). '
        'Gate should downgrade the raise to a call.' % action_gated)
    assert action_ungated > 0, (
        'REACHABILITY FAIL: ungated K9o sb_open action=%r, expected a raise (>0). '
        'High-fold opener should make the bucket raise K9o.' % action_ungated)
    print('REACHABILITY PASS: offsuit gate downgrades K9o sb_open raise(%d)->limp(0)'
          % action_ungated)


def main_offsuit_carveout():
    """v173: prove the A2o-A5o carve-out CHANGES behavior vs a loose opener.

    BB holds A5o = [Ac-historical, 5s-historical] but integers [48,13] =
    Ah(14), 5d(5) -> high=14, low=5 (A5o, in the carve-out range). Facing an
    SB raise to 300.

    - vs a STANDARD opener (vpip=0.55, conf=0.50): carve-out does NOT fire
      (vpip<0.65), gate stays on -> A5o folded (-1).
    - vs an ULTRA-LOOSE opener (vpip=0.72, conf=0.25): carve-out fires
      (vpip>=0.65 AND conf>=0.15) -> A5o NOT folded (defended).

    The behavioral flip (fold -> not-fold) is the only rigorous proof that the
    carve-out is actually threaded through get_action() at bb_vs_raise.
    """
    req = {
        'my_id': 0, 'dealer_id': 0,
        'my_chips': 19900, 'opponent_chips': 19700,
        'my_cards': [48, 13], 'public_cards': [],   # Ah, 5d (A5o)
        'history': [{'round': 0, 'player_id': 1, 'action': 300, 'action_type': 'raise'}],
        'hand': 5, 'max_hand': 70,
    }
    std_opener = _std_opponent_model()              # vpip=0.55, conf=0.50
    loose_opener = dict(std_opener)
    loose_opener['vpip'] = 0.72
    loose_opener['confidence'] = 0.25               # carve-out fires: >=0.65 & >=0.15

    real_build = strategy.build_opponent_model
    try:
        strategy.build_opponent_model = lambda _r, _i: std_opener
        action_std = strategy.get_action(req, [req])
        strategy.build_opponent_model = lambda _r, _i: loose_opener
        action_loose = strategy.get_action(req, [req])
    finally:
        strategy.build_opponent_model = real_build
    print('carveout A5o bb_vs_raise STD action:', action_std,
          ' LOOSE action:', action_loose)
    assert action_std == -1, (
        'REACHABILITY FAIL: A5o vs std opener action=%r, expected -1 (fold). '
        'Carve-out must NOT fire when vpip<0.65.' % action_std)
    assert action_loose != -1, (
        'REACHABILITY FAIL: A5o vs loose opener action=%r, expected NOT -1. '
        'Carve-out (vpip>=0.65, conf>=0.15) should defend A5o.' % action_loose)
    print('REACHABILITY PASS: A5o carveout flips bb_vs_raise fold(-1) -> %r vs loose opener'
          % action_loose)


def _build_river_overcall_request():
    """Construct a river spot where the bot holds a marginal one-pair facing a
    multi-street barrel from the opponent.

    Bot is BB (my_id=0). SB (player_id=1) is dealer. Limped preflop, opponent
    bet flop+turn+river (3 streets) -> opp_postflop_bet_count=3 maximizes the
    multi-street aggression factor in OVERCALL_TIGHTEN.

    Bot hand: pocket 9s [9h, 9d] = [28, 29] (underpair to J,Q on board).
    Board: [2h, 4d, 6s, Jc, Qd] = [0, 9, 18, 39, 41].
    Pot after call: 200(flop)+400(turn)+800(river bet) + blinds/history.
    """
    history = [
        # Preflop: SB calls 100, BB checks
        {"round": 0, "player_id": 1, "action": 100, "action_type": "call"},
        {"round": 0, "player_id": 0, "action": 0, "action_type": "check"},
        # Flop: BB checks, SB bets 200, BB calls
        {"round": 1, "player_id": 0, "action": 0, "action_type": "check"},
        {"round": 1, "player_id": 1, "action": 200, "action_type": "raise"},
        {"round": 1, "player_id": 0, "action": 200, "action_type": "call"},
        # Turn: BB checks, SB bets 400, BB calls
        {"round": 2, "player_id": 0, "action": 0, "action_type": "check"},
        {"round": 2, "player_id": 1, "action": 400, "action_type": "raise"},
        {"round": 2, "player_id": 0, "action": 400, "action_type": "call"},
        # River: BB checks, SB bets 800 -> bot to act
        {"round": 3, "player_id": 0, "action": 0, "action_type": "check"},
        {"round": 3, "player_id": 1, "action": 800, "action_type": "raise"},
    ]
    req = {
        "my_id": 0,
        "dealer_id": 1,
        "my_chips": 20000 - 100 - 200 - 400,  # 19300
        "opponent_chips": 20000 - 50 - 200 - 400 - 800,  # 18550
        "my_cards": [28, 29],         # 9h, 9d (pocket 9s)
        "public_cards": [0, 9, 18, 39, 41],  # 2h, 4d, 6s, Jc, Qd
        "history": history,
        "hand": 5,
        "max_hand": 70,
    }
    return req


def main_overcall_tighten():
    """v175: prove OVERCALL_TIGHTEN fires end-to-end through get_action() on a
    river multi-street barrel with a polarized opponent model.

    Injects a value-polarized opponent (high large_bet_ratio, high confidence,
    multi-street barreler). Captures stderr and asserts the OVERCALL_TIGHTEN
    telemetry appears, proving the v175 inline code path is reachable.
    """
    import io
    import contextlib

    polarized_opp = _std_opponent_model()
    polarized_opp['confidence'] = 0.50       # >= 0.15 gate
    polarized_opp['large_bet_ratio'] = 0.55  # well above 0.28 baseline -> polar=1.0

    req = _build_river_overcall_request()
    real_build = strategy.build_opponent_model
    try:
        strategy.build_opponent_model = lambda _r, _i: polarized_opp
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            action = strategy.get_action(req, [req])
    finally:
        strategy.build_opponent_model = real_build

    stderr_text = err_buf.getvalue()
    print('overcall_tighten action:', action)
    has_telemetry = 'OVERCALL_TIGHTEN' in stderr_text
    print('OVERCALL_TIGHTEN telemetry present:', has_telemetry)
    if has_telemetry:
        # Extract the delta_milli line for display
        for line in stderr_text.splitlines():
            if 'OVERCALL_TIGHTEN' in line:
                print('  ', line.strip())
                break
    assert has_telemetry, (
        'REACHABILITY FAIL: OVERCALL_TIGHTEN telemetry NOT found in stderr.\n'
        'The v175 river 0.45-0.55 call-margin tighten did not fire.\n'
        'Either made_strength is outside [0.45,0.55) or a gate is too strict.\n'
        'Action was %r. Stderr tail:\n%s'
        % (action, stderr_text[-500:] if stderr_text else '(empty)'))
    print('REACHABILITY PASS: OVERCALL_TIGHTEN fires through get_action() (action=%r)' % action)


if __name__ == "__main__":
    main()
    main_offsuit_gate()
    main_offsuit_gate_sb_open()
    main_offsuit_carveout()
    main_overcall_tighten()
