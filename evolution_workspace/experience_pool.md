# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated v1-v7)

1. **Chen preflop table essential — worth ~130pts alone.** 169-hand lookup from bot5 replaces crude formula.
2. **Anti-bot4 detection + adjustments = proven edge.** detect_bot4_profile + get_anti_bot4_adjustments from bot5.
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** is proven value extraction (bot5 choose_overbet_river).
4. **Simulation counts: bot5 uses {0:900,3:1200,4:1500} but v2 rated #1 with {0:500}.** Counts matter less than threshold calibration. Keep bot5 counts but prioritize EQR tuning.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.**
6. **EQR calibration is THE differentiator.** v2 rated 1552 with LOWER EQR (air 0.68/0.56, pair 0.84/0.73) vs bot5 (0.72/0.62, 0.86/0.78). Lower EQR = tighter folding = fewer catastrophic losses. Bounds: air [0.40,0.85], pair [0.60,0.92].
7. **allow_low_frequency_blocker_bluff: use random.random() + bluff_freq_bonus param**, not deterministic hash.
8. **Dead weight: cbet_rate, fold_to_cbet, drift detection, gift_balance, exploit_lambda, gto_strong blending.** Removed in bot5. Keep removed.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** threshold_delta symmetric: 0.055*protect - 0.055*chase.
10. **bb_vs_raise/sb_vs_raise: Let simulation decide (return None).** Fixed thresholds are harmful.
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Over-engineering fails. Incremental targeted port wins.**
13. **Fix ALL parameter issues simultaneously.** Effects compound. One-at-a-time fails.
14. **choose_raise: thin_cap = 0.30 (round<=2) / 0.38 (round==3).** max_ratio conditional: 2.2 for river overbet nut hands, else 1.45.
15. **cbet_rate checks in call margin logic are harmful noise.** bot5 removed them.

### v7→v8: Breaking the Plateau

16. **Stagnation: v2/v3/v15 cluster within ~21pts (RD ~46).** v7=bot5+fixes has no data. v2 trending upward (+85pts).
17. **jam_buffer accumulates too many bonuses for thin hands.** Base 0.02 + thin bonus 0.04 + nutted_risk + 0.04*protect + line_strength + check_resistance = can reach 0.14 cap, allowing calls with win_rate as low as ~0.45. **Cap at 0.11 for thin/marginal.**
18. **Catastrophic all-in root cause: jam_buffer too permissive for non-nut hands.** With thin value + aggression indicators, we call off stacks with marginal top pairs. Reduce thin tier bonus from 0.04 to 0.02.
19. **choose_overbet_bluff_river is a critical unused weapon.** Blocker-based river bluffs on dry boards add fold equity with near-zero showdown value. Implement with blocker_profile + low wetness + opponent fold_to_raise > 0.50.
20. **v3's EXP3 meta-learner rated below v2's simpler approach.** Complex style adaptation costs ~20pts vs simpler calibrated thresholds. v8 should stick with threshold-based approach, not EXP3.
21. **min_raise_action fix is essential.** v2 uses state["round_raise"] which can be wrong; v7 uses state.get("min_raise_action", state["round_raise"]).
22. **v2 has OOP double_barrel EQR penalty (-0.05) and big_pot air discount (-0.03) that bot5 lacks.** Port these to v8 for additional conservatism in dangerous spots.
23. **must_continue_vs_raise should extend to strong combo draws.** Currently only protects nut/strong made hands. Add draw_strength >= 0.20 with favorable pot odds.
