# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated v8-v17)

1. **Chen preflop table essential — worth ~130pts alone.** 169-hand lookup from bot5 replaces crude formula.
2. **Anti-bot4 detection + adjustments = proven edge.** detect_bot4_profile + get_anti_bot4_adjustments from bot5.
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** is proven value extraction (bot5 choose_overbet_river).
4. **Simulation counts must match bot5: {0:900, 3:1200, 4:1500}** extras {0:300, 3:350, 4:300}. Low counts = massive accuracy loss.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.**
6. **EQR air: 0.72 IP / 0.62 OOP, lower bound 0.45.** Pair EQR: 0.86/0.78. Remove big_pot param entirely.
7. **allow_low_frequency_blocker_bluff: use random.random() + bluff_freq_bonus param**, not deterministic hash.
8. **Dead weight to remove: cbet_rate, fold_to_cbet, drift detection, hand_vpip/pfr flags, hand_postflop tracking, gift_balance, exploit_lambda, gto_strong blending.** bot5 removed all of these.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** threshold_delta symmetric: 0.055*protect - 0.055*chase.
10. **bb_vs_raise/sb_vs_raise fixed thresholds ALWAYS harmful.** Let simulation decide (return None).
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Over-engineering fails. Incremental targeted port wins.**
13. **Fix ALL parameter issues simultaneously.** Effects compound. One-at-a-time fails.
14. **choose_raise: thin_cap = 0.30 (round≤2) / 0.38 (round==3).** max_ratio conditional 2.2 for river overbet nut hands.
15. **cbet_rate checks in call margin logic are harmful noise.** bot5 removed them. Remove from v6.

### v6→v7: Comprehensive Bot5 Port (Study #2 — Full Code Diff)

16. **v6 rating ~1495, ~77pts behind top (v2=1573).** Consistently weakest active bot. All 15 lessons apply.
17. **All 15 gaps confirmed via full file-by-file diff of v6 vs bot5.** No new algorithmic features in v6 beyond bot5.
18. **v6 anti-lock flow checks preflop_trash_hand — bot5 does NOT.** Blocks aggression with trash in anti-lock mode. Remove guard.
19. **v6 realized_postflop_equity has extra OOP double_barrel penalty + big_pot param + OOP draw block.** bot5 removed all three. Fix EQR values: 0.72/0.62 air, 0.86/0.78 pair, bounds 0.45-0.85/0.65-0.92.
20. **v6 choose_preflop_spot_action has hardcoded bb_vs_raise and sb_vs_reraise blocks.** bot5 returns None. Lesson #10 confirmed.
21. **Three-worker split with clean file ownership:** W1={state,opponent}, W2={strategy,postflop}, W3={constants,tournament}.
22. **Key risk: recalibration after Chen table port.** ALL preflop thresholds downstream must work with new strength scale.
23. **bot5 choose_raise has anti_bot4_bonus and allow_river_overbet params.** v6 lacks both. Port structure + integration.
24. **bot5 bad_river_bluff_candidate adds bot4 bluff_freq_bonus exception.** v6 lacks anti-bot4 so check is too conservative.
25. **Chen table port is the single highest-impact change.** It changes preflop_strength scale, affecting ALL downstream thresholds.
26. **v6 opponent.py has 4 categories of dead weight:** cbet_rate/fold_to_cbet, drift detection, hand flag arrays, wrong priors.
27. **v6 strategy.py has 3 categories of dead weight:** gift_balance/exploit_lambda/gto_strong, cbet_rate checks, pot param in EQR.
28. **bot5 anti-lock preflop: no round_idx guard.** v6 has `if round_idx > 0 or preflop_3bet_candidate` — bot5 always attempts anti-lock attack.
