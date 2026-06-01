# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Correct priors: vpip=0.58, pfr=0.28, confidence divisor=35. Wrong priors corrupt ALL opponent modeling.
- `classify_opponent_style` (nit/maniac/station/fold-heavy) returns threshold deltas — +2-3 pts adaptation. Must propagate deltas to ALL bluff thresholds AND strong/medium decision thresholds.
- Anti-bot4 detection with LOOSE criteria is catastrophic. Only use with very tight thresholds (±0.08).

## POSTFLOP_STRATEGY
- River overbet (1.5-2.2x pot) for nut hands on dry rivers = proven value extraction. Separate weapon: river overbet bluff with strong blockers.
- River exact equity override: when 5 public cards, use exact enumeration (0 sims) for precise decisions at showdown.
- `big_pot_safety_guard` prevents catastrophic stack-offs with thin value in big pots (>7000). Mandatory.
- `donk_bet_profile` — OOP lead on wet/dynamic boards with strong/nut hands or combo draws.
- `trap_nut_slowplay` — check nut hands on dry boards vs aggressive opponents for check-raise value.
- `spr_profile` — stack-to-pot ratio awareness (deep/medium/shallow/commit) guides sizing decisions.

## BLUFF_CALIBRATION
- bb_vs_raise/sb_vs_raise: let simulation decide. Hardcoded 3bet bluff spots are net negative.
- Deterministic hash for blocker bluff frequency reduces variance: `(int(score*7919)+37)%100 < threshold`. Random.random() is worse.
- `river_bluff_ev` (EV-based using fold_to_raise + blocker count) is more principled than threshold-only.
- Turn/river thin value betting requires STRICT conditions (top_pair+, kicker≥10, no dynamic boards, risk<0.05). Looser versions are net negative.
- Safety guards are mandatory for aggressive features: `weak_pair_river`, `bad_river_bluff_candidate`, `weak_bottom_pair_barrel`, `thin_static_showdown_control`.

## PARAMETER_TUNING
- Anti-lock proven values: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.
- EQR: LOWER values outperform (air: 0.65/0.53). Higher EQR over-realizes weak hands → calls too light. Apply pot>3000 (-0.03) and OOP (-0.05) penalties.
- `exploit_lambda` (gift_balance blending) is HARMFUL — corrupts GTO base. Use `style_deltas` instead.
- Higher jam/shove buffer caps (>0.11) cause aggressive all-in with marginal hands.
- 700 flop sims is sufficient. More sims ≠ better play if thresholds are wrong.
- When changing preflop eval, recalibrate ALL downstream thresholds. Parameter changes compound synergistically.

## GENERAL
- Chen preflop formula is sufficient; full table may introduce calibration noise.
- Wholesale copy fails. Incremental targeted port wins. Fix ALL parameter issues simultaneously.
- `state.get("min_raise_action", state["round_raise"])` is more robust than direct dict access.

## RECENT_LESSONS
- v17 collapse (-62) from compounding errors: wrong priors, higher EQR, missing classify/style_deltas propagation, missing big_pot_safety_guard, higher jam buffers, loose thin value betting. Any ONE of these alone is tolerable; combined they cascade.
- Selectively port v12 features (donk_bet_profile, river_bluff_ev, spr_profile, trap_nut_slowplay) — always pair with v12's safety guards.
- river_thin_value_profile requires strict conditions. v17's looser version was net negative.
