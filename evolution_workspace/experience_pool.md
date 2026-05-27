# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated v8-v17)

1. **Chen preflop table essential — worth ~130pts alone.** 169-hand lookup from bot5 replaces crude formula.
2. **Anti-bot4 detection + adjustments = proven edge.** detect_bot4_profile + get_anti_bot4_adjustments from bot5.
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** is proven value extraction (bot5 choose_overbet_river).
4. **Simulation counts must match bot5: {0:900, 3:1200, 4:1500}** extras {0:300, 3:350, 4:300}. Low counts = massive accuracy loss.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35 (NOT 30).**
6. **EQR air: 0.72 IP / 0.62 OOP, lower bound 0.45.** Pair EQR: 0.86/0.78. Remove big_pot param entirely.
7. **allow_low_frequency_blocker_bluff: use random.random() + bluff_freq_bonus param**, not deterministic hash.
8. **Dead weight to remove: cbet_rate, fold_to_cbet, drift detection, hand_vpip/pfr flags, hand_postflop tracking, gift_balance, exploit_lambda, gto_strong blending.** bot5 removed all of these.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** threshold_delta symmetric: 0.055*protect - 0.055*chase.
10. **bb_vs_raise/sb_vs_raise fixed thresholds ALWAYS harmful.** Let simulation decide (return None).
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Over-engineering fails. Incremental targeted port wins.**
13. **Fix ALL parameter issues simultaneously.** Effects compound. One-at-a-time fails.
14. **choose_raise: thin_cap = 0.30 (round≤2) / 0.38 (round==3).** max_ratio conditional: 2.2 for river overbet nut hands, else 1.45.
15. **cbet_rate checks in call margin logic are harmful noise.** bot5 removed them. Remove from v6.

### v6→v7: Comprehensive Bot5 Port (Study #3 — Confirmed All Gaps)

16. **v6 rating ~1464, ~80pts behind v13 (~1541).** Bottom quartile. All 15 lessons apply.
17. **v6 state.py uses crude formula for estimate_preflop_strength.** Must port Chen table from bot5 (PREFLOP_STRENGTH_TABLE in constants.py).
18. **v6 anti-lock flow checks preflop_trash_hand — bot5 does NOT.** v6 line ~658: `if not preflop_trash_hand:` blocks aggression. bot5 always attempts anti-lock attack.
19. **v6 realized_postflop_equity has 3 extra penalties: OOP double_barrel (-0.05), big_pot param (-0.03), OOP draw block (entire section).** bot5 removed ALL three. Fix EQR: air 0.72/0.62 bounds [0.45,0.85]; pair 0.86/0.78 bounds [0.65,0.92].
20. **v6 choose_preflop_spot_action has hardcoded bb_vs_raise (line 560-585) and sb_vs_reraise (line 587-598) blocks.** bot5 returns None. Lesson #10 confirmed.
21. **v6 choose_raise lacks anti_bot4_bonus and allow_river_overbet params.** thin_cap wrong: 0.46+0.08*wetness vs bot5's 0.30/0.38. max_ratio hardcoded 1.45 vs bot5's conditional 2.2.
22. **v6 opponent.py has 4 categories dead weight:** cbet_rate/fold_to_cbet tracking, drift detection, hand flag arrays, wrong priors (0.52/0.24 vs 0.58/0.28, divisor 30 vs 35).
23. **v6 strategy.py has dead weight:** gift_balance/exploit_lambda/gto_strong blending, cbet_rate checks in call margin, `pot` param in realized_postflop_equity.
24. **v6 has NO detect_bot4_profile or get_anti_bot4_adjustments.** Must port from bot5/opponent.py.
25. **v6 has NO choose_overbet_river or choose_overbet_bluff_river.** Must port from bot5/strategy.py.
26. **v6 allow_low_frequency_blocker_bluff uses deterministic hash.** bot5 uses random.random() + bluff_freq_bonus param.
27. **bot5 anti-bot4 adjustments affect: bluff thresholds (subtract bluff_freq_bonus), raise sizes (add raise_size_bonus), call thresholds (subtract call_threshold_delta), thin_static_showdown_control (override when bot4 detected), bad_river_bluff_candidate (override when bot4 detected).** Must wire all 5 through get_action flow.
