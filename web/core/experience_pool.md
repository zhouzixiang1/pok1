# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (v1–v8 consolidated)

1. **Chen preflop table: v4 (#1 at 1623) succeeds WITHOUT it using formula.** Table may introduce calibration noise. Formula is sufficient.
2. **Anti-bot4 detection with LOOSE criteria is catastrophic.** v14 collapsed 100pts. Only use with VERY tight thresholds (±0.08).
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** = proven value extraction. v4 lacks it. Must add.
4. **Simulation counts: 700 flop sims is fine.** v2 rated #1 with {0:500}. More sims ≠ better play if thresholds are wrong.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.** v9 uses these. Proven correct. v17 used WRONG priors (0.52/0.24) — catastrophic.
6. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** Proven values.
7. **bb_vs_raise/sb_vs_raise: Let simulation decide.** Hardcoded 3bet bluff spots are net negative.
8. **When changing preflop eval, recalibrate ALL downstream thresholds.**
9. **Wholesale copy fails. Incremental targeted port wins.**
10. **Fix ALL parameter issues simultaneously.** Effects compound.
11. **exploit_lambda (gift_balance blending) is HARMFUL.** It corrupts GTO base thresholds. v9 uses style_deltas instead — PROVEN better.
12. **Parameter changes compound synergistically.** EQR + CBet + thin_cap + opponent style all together outperform any single change.

### v9 Foundation Features (PROVEN at 1611 ELO)

13. **classify_opponent_style** classifies nit/maniac/station/fold-heavy and returns threshold deltas. +2-3 pts adaptation. v17 LACKED this.
14. **River overbet bluff with strong blockers** (choose_overbet_bluff_river) on dry boards. Separate weapon from value overbet.
15. **big_pot_safety_guard** prevents catastrophic stack-offs with thin value in big pots (>7000). v17 LACKED this entirely.
16. **River exact equity override:** when 5 public cards, use exact enumeration (0 sims) for precise raise/fold decisions. Crucial edge at showdown.
17. **EQR calibration: v9's lower values OUTPERFORM v5/v17's.** v9 air: 0.65/0.53 vs v17's 0.72/0.62. v9 applies pot>3000 (-0.03) and OOP (-0.05) penalties.
18. **style_deltas propagate to ALL bluff thresholds.** v9 subtracts bluff_freq_bonus from river_bluff, probe_fold, semi_bluff thresholds. v17 didn't.
19. **style_deltas adjust strong/medium decision thresholds.** v9 adds strong_delta/medium_delta to raise/call thresholds. v17 missed this.
20. **Deterministic hash for blocker bluff frequency** reduces variance. v9: `(int(score*7919)+37)%100 < threshold`. v17 used random.random().
21. **`state.get("min_raise_action", state["round_raise"])`** is more robust than direct dict access. v9 uses this throughout.

### v17 Failure Analysis (1559 ELO, -62 from peak)

22. **v17 WRONG priors (0.52/0.24) corrupted ALL opponent modeling.** This alone explains much of the rating decline. Priors determine initial opponent read before data accumulates.
23. **v17 HIGHER EQR (0.72/0.62 air, 0.86/0.78 draw) vs v9 (0.65/0.53, 0.82/0.70).** Over-realizes weak hands → calls too light → loses chips.
24. **v17's turn_probe_medium_strength and river_thin_value_bet were NET NEGATIVE.** Too loose — betting medium hands into strong ranges. v12 has similar features but with MORE safety guards (weak_pair_river, bad_river_bluff_candidate, thin_static_showdown_control).
25. **Missing classify_opponent_style + style_deltas** meant v17 couldn't adapt bluff frequency to opponent type. Bluffed stations, didn't bluff nits.
26. **v17 higher jam/shove buffer caps (0.14 vs v9's 0.11)** caused more aggressive all-in plays with marginal hands.

### v12 Feature Analysis (1610 ELO — features to selectively port)

27. **donk_bet_profile** — leading out OOP on wet/dynamic boards with strong/nut hands or combo draws. Addresses a gap in v9.
28. **river_bluff_ev** — EV-based river bluff calculation using fold_to_raise and blocker count. More principled than threshold-based approach.
29. **spr_profile** — stack-to-pot ratio awareness for sizing. Deep (>10), medium (5-10), shallow (2.5-5), commit (<2.5).
30. **v12 safety guards are critical when adding aggressive features:** weak_pair_river, bad_river_bluff_candidate, weak_bottom_pair_barrel, thin_static_showdown_control all prevent catastrophic plays.
31. **river_thin_value_profile** requires strict conditions: only top_pair/overpair with kicker≥10, no dynamic boards, risk<0.05. v17's version was too loose.
32. **trap_nut_slowplay** — check nut hands on dry boards vs aggressive opponents for check-raise value. v9 doesn't have this explicitly.
