# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (v1–v8 consolidated)

1. **Chen preflop table: v4 (#1 at 1623) succeeds WITHOUT it using formula.** Table may introduce calibration noise. Formula is sufficient.
2. **Anti-bot4 detection with LOOSE criteria is catastrophic.** v14 collapsed 100pts. Only use with VERY tight thresholds (±0.08).
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** = proven value extraction. v4 lacks it. Must add.
4. **Simulation counts: 700 flop sims is fine.** v2 rated #1 with {0:500}. More sims ≠ better play if thresholds are wrong.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.** v4 uses these. Proven correct.
6. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** Proven values.
7. **bb_vs_raise/sb_vs_raise: Let simulation decide.** Hardcoded 3bet bluff spots are net negative.
8. **When changing preflop eval, recalibrate ALL downstream thresholds.**
9. **Wholesale copy fails. Incremental targeted port wins.**
10. **Fix ALL parameter issues simultaneously.** Effects compound.
11. **exploit_lambda (gift_balance blending) is HARMFUL.** It corrupts GTO base thresholds. DO NOT use.
12. **Parameter changes compound synergistically.** EQR + CBet + thin_cap + opponent style all together outperform any single change.

### v9–v17 Era: Key Feature Gaps

13. **classify_opponent_style (nit/maniac/station/fold-heavy) provides +2-3 pts of adaptation.** v5 LACKS this. PRIORITY ADD.
14. **River overbet bluff with strong blockers** on dry boards is a distinct weapon. v5 has it DISABLED (stub returns None).
15. **big_pot_safety_guard prevents catastrophic stack-offs** with thin value in big pots (>7000). v5 LACKS this entirely.
16. **River exact equity override:** when 5 public cards, use exact enumeration (0 sims) for precise raise/fold decisions. v5 LACKS this. v9 has it and gains crucial edge at showdown.
17. **EQR calibration: v9's lower values OUTPERFORM v5's.** v9 air: 0.65/0.53 vs v5's 0.72/0.62. v9 draw: 0.82/0.70 vs v5's 0.86/0.78. v9 also applies pot>3000 (-0.03) and OOP (-0.05) penalties. v5's higher EQR causes over-realization of weak hands.
18. **style_deltas must propagate to ALL bluff thresholds.** v9 subtracts bluff_freq_bonus from river_bluff, probe_fold, semi_bluff thresholds. v5 doesn't apply style-based adjustments anywhere in bluff logic.
19. **style_deltas must adjust strong/medium decision thresholds.** v9 adds strong_delta/medium_delta to the raise/call decision thresholds. v5 misses this adaptation layer.
20. **Deterministic hash for blocker bluff frequency** reduces variance vs random.random(). v9: `(int(score*7919)+37)%100 < threshold`. v5 still uses `random.random()`.
21. **state.get("min_raise_action", state["round_raise"])** is more robust than direct dict access. v9 uses this throughout; v5 has some direct accesses that could cause KeyError edge cases.

### v5 Rating Trend & Strategy

22. **v5 peaked at 1664.9 (period 166), dropped to 1610.9 (period 176).** -54 pts decline. Field adapted to v5's tendencies. v9 now tied at 1611.9. Must port v9's proven features to stay competitive.
23. **v5's structural advantages over v9:** same simulation engine, same card eval. Gap is purely in decision logic features (classify_opponent, overbet bluff, big pot guard, river exact, EQR tuning, style deltas).
24. **File size: strategy.py at 1160 lines is over 1000-line soft limit.** Adding new features requires either extraction to betting.py or condensing existing code.
25. **v9 moved choose_raise/choose_preflop_spot_action/choose_overbet_river to betting.py.** This modularization reduces strategy.py from 1160 to ~991 lines. v5 should do the same to make room for new features.
