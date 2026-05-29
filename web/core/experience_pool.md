# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Correct priors: vpip=0.58, pfr=0.28, confidence divisor=35. Wrong priors (v17 used 0.52/0.24) corrupt ALL opponent modeling and explain much of its -62 collapse.
- classify_opponent_style (nit/maniac/station/fold-heavy) returns threshold deltas — +2-3 pts adaptation. v17 lacked it → couldn't adapt bluff frequency to opponent type.
- style_deltas MUST propagate to ALL bluff thresholds (subtract bluff_freq_bonus) AND strong/medium decision thresholds (add deltas). v17 missed both propagations.
- Anti-bot4 detection with LOOSE criteria is catastrophic (v14 collapsed 100pts). Only use with VERY tight thresholds (±0.08).

## POSTFLOP_STRATEGY
- River overbet (1.5-2.2x pot) for nut hands on dry rivers = proven value extraction. Separate weapon: river overbet bluff with strong blockers.
- River exact equity override: when 5 public cards, use exact enumeration (0 sims) for precise raise/fold decisions at showdown.
- big_pot_safety_guard prevents catastrophic stack-offs with thin value in big pots (>7000). v17 lacked it entirely.
- donk_bet_profile — OOP lead on wet/dynamic boards with strong/nut hands or combo draws. Addresses a gap.
- trap_nut_slowplay — check nut hands on dry boards vs aggressive opponents for check-raise value.
- spr_profile — stack-to-pot ratio awareness (deep/medium/shallow/commit) guides sizing decisions.

## BLUFF_CALIBRATION
- bb_vs_raise/sb_vs_raise: let simulation decide. Hardcoded 3bet bluff spots are net negative.
- Deterministic hash for blocker bluff frequency reduces variance: `(int(score*7919)+37)%100 < threshold`. Random.random() is worse.
- river_bluff_ev (EV-based using fold_to_raise + blocker count) is more principled than threshold-only.
- v17's turn_probe_medium_strength and river_thin_value_bet were NET NEGATIVE — too loose, betting medium into strong ranges. Strict conditions required (top_pair+, kicker≥10, no dynamic boards, risk<0.05).
- Safety guards are mandatory when adding aggressive features: weak_pair_river, bad_river_bluff_candidate, weak_bottom_pair_barrel, thin_static_showdown_control.

## PARAMETER_TUNING
- Anti-lock proven values: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.
- EQR: v9's LOWER values outperform (air: 0.65/0.53 vs v17's 0.72/0.62). Higher EQR over-realizes weak hands → calls too light. Apply pot>3000 (-0.03) and OOP (-0.05) penalties.
- exploit_lambda (gift_balance blending) is HARMFUL — corrupts GTO base. Use style_deltas instead.
- v17's higher jam/shove buffer caps (0.14 vs v9's 0.11) caused more aggressive all-in with marginal hands.
- Simulation counts: 700 flop sims is sufficient. More sims ≠ better play if thresholds are wrong.
- When changing preflop eval, recalibrate ALL downstream thresholds. Parameter changes compound synergistically.

## GENERAL
- Chen preflop formula is sufficient; full table may introduce calibration noise.
- Wholesale copy fails. Incremental targeted port wins. Fix ALL parameter issues simultaneously.
- `state.get("min_raise_action", state["round_raise"])` is more robust than direct dict access.

## RECENT_LESSONS
- v17 (-62 from peak) failed from compounding errors: wrong priors, higher EQR, missing classify_opponent_style, missing style_deltas propagation, missing big_pot_safety_guard, higher jam buffers, loose thin value betting.
- v12 features to selectively port: donk_bet_profile, river_bluff_ev, spr_profile, trap_nut_slowplay. Always pair with v12's safety guards.
- river_thin_value_profile requires STRICT conditions (top_pair/overpair, kicker≥10, no dynamic boards, risk<0.05). v17's looser version was net negative.
