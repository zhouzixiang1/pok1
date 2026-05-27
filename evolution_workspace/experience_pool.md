# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated v8-v17)

1. **Chen preflop table essential — worth ~130pts alone.** 169-hand lookup from bot5 replaces crude formula.
2. **Anti-bot4 detection + adjustments = proven edge.** detect_bot4_profile + get_anti_bot4_adjustments from bot5.
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** is proven value extraction (bot5 choose_overbet_river).
4. **Simulation counts must match bot5: {0:900, 3:1200, 4:1500}** extras {0:300, 3:350, 4:300}. Low counts = massive accuracy loss.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.**
6. **EQR air: 0.72 IP / 0.62 OOP, lower bound 0.45.** Pair EQR: 0.86/0.78 bounds [0.65,0.92]. Remove big_pot param entirely.
7. **allow_low_frequency_blocker_bluff: use random.random() + bluff_freq_bonus param**, not deterministic hash.
8. **Dead weight to remove: cbet_rate, fold_to_cbet, drift detection, hand_vpip/pfr flags, gift_balance, exploit_lambda, gto_strong blending.** bot5 removed all of these.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** threshold_delta symmetric: 0.055*protect - 0.055*chase.
10. **bb_vs_raise/sb_vs_raise fixed thresholds ALWAYS harmful.** Let simulation decide (return None).
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Over-engineering fails. Incremental targeted port wins.**
13. **Fix ALL parameter issues simultaneously.** Effects compound. One-at-a-time fails.
14. **choose_raise: thin_cap = 0.30 (round≤2) / 0.38 (round==3).** max_ratio conditional: 2.2 for river overbet nut hands, else 1.45.
15. **cbet_rate checks in call margin logic are harmful noise.** bot5 removed them.

### v2→v7: Branching from Top Performer (v2 rated 1552.3)

16. **Stagnation: v6 evolution failed. Branching from v2 (#1 rated) for divergent exploration.**
17. **v2 has same gaps as v6 vs bot5:** missing anti-bot4, river overbet, must_continue_vs_raise, min_raise_action, wrong sim counts (500/700/900), wrong priors (0.52/0.24 vs 0.58/0.28).
18. **v2 realized_postflop_equity has 3 extra penalties:** OOP double_barrel (-0.05), big_pot param (-0.03), OOP draw block. bot5 removed ALL three. Fix EQR bounds: air [0.45,0.85], pair [0.65,0.92].
19. **v2 choose_preflop_spot_action has hardcoded bb_vs_raise/sb_vs_reraise blocks.** bot5 returns None. Lesson #10 confirmed.
20. **v2 allow_low_frequency_blocker_bluff uses deterministic hash.** bot5 uses random.random() + bluff_freq_bonus.
21. **Match analysis: catastrophic all-in timing, poor pot control, exploitable by aggression.** Root cause: missing must_continue_vs_raise, wrong EQR, gift_balance/cbet_rate noise.
22. **min_raise_action needed:** bot5 tracks judge_round_raise separately for correct min-raise calculation.
23. **must_continue_vs_raise prevents folding strong hands to pressure.** Critical for avoiding exploitable folds.
24. **anti_bot4 adjustments affect 5 decision points:** bluff thresholds, raise sizes, call thresholds, thin_static_showdown, bad_river_bluff_candidate.
