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

### v6→v7 Verified Gaps (Confirmed by Full Diff)

14. **v6 rating stuck ~1408-1424 for 120+ periods.** ~138pts below top (v5=1562). All gaps below confirmed by line-by-line diff with bot5.
15. **constants.py**: Missing PREFLOP_STRENGTH_TABLE (169 Chen entries), CARD_RANKS/CARD_SUITS precomputed arrays. Wrong simulation counts {0:400,3:800,4:900} vs bot5 {0:900,3:1200,4:1500}. Wrong extras {0:200,3:280,4:180} vs bot5 {0:300,3:350,4:300}.
16. **state.py**: estimate_preflop_strength uses crude formula. Bot5 uses PREFLOP_STRENGTH_TABLE lookup — far more accurate.
17. **opponent.py**: Wrong priors (vpip=0.52/pfr=0.24/divisor=30 vs bot5 0.58/0.28/35). Dead weight: cbet tracking, drift detection, hand-level flags. Missing detect_bot4_profile and get_anti_bot4_adjustments functions.
18. **strategy.py — anti-bot4 wiring MISSING**: bot5 detects bot4 after computing board_texture, then applies bluff_freq_bonus to blocker bluffs, raise_size_bonus to choose_raise, call_threshold_delta to showdown thresholds, trap_defense_delta to aggression thresholds. v6 has NONE of this.
19. **strategy.py — river overbet MISSING**: bot5 has choose_overbet_river (1.5-2.2x pot with nuts on river with to_call==0) firing BEFORE main decision tree. v6 missing entirely.
20. **strategy.py — dead weight**: gift_balance, exploit_lambda, gto_strong/gto_medium blending. bot5 removed all. v6 keeps them.
21. **strategy.py — realized_postflop_equity wrong**: EQR air 0.68/0.56 vs bot5 0.72/0.62. Lower bound 0.40 vs 0.45. big_pot subtraction (bot5 removed). Draw OOP extra subtractions (bot5 removed). Pair EQR 0.84/0.73 vs bot5 0.86/0.78.
22. **strategy.py — choose_raise**: Missing anti_bot4_bonus and allow_river_overbet params. thin_cap wrong formula. max_ratio always 1.45 vs bot5 2.2 for river overbet.
23. **strategy.py — choose_preflop_spot_action**: Has harmful fixed branches for bb_vs_raise/sb_vs_reraise. bot5 returns None for both.
24. **strategy.py — anti_lock preflop**: v6 guards with preflop_trash_hand. bot5 removed this guard.
25. **strategy.py — bad_river_bluff_candidate and thin_static_showdown_control**: Missing anti_bot4["bluff_freq_bonus"] < 0.05 check. bot5 allows bluffs vs detected bot4.
26. **postflop.py — allow_low_frequency_blocker_bluff**: v6 uses deterministic hash + no bluff_freq_bonus param. bot5 uses random.random() + bluff_freq_bonus parameter.
27. **tournament.py**: Anti-lock params too conservative. Chase 0.85 vs 0.90, threshold_delta -0.070 vs -0.075, sizing 0.16 vs 0.18, bluff 0.11 vs 0.13. threshold_delta asymmetric 0.050/0.060 vs symmetric 0.055/0.055.

### v7 Strategy: Full Targeted Port from bot5

28. **Priority order: Chen table → anti-bot4 → river overbet → remove dead weight → fix EQR/params.** Each independently validated by bot5. Apply ALL simultaneously for compounding effect.

### Current State (v6→v7, Period 819)

29. **v6 rating 1409 (rd=43), stuck for 819 periods.** Lowest of all claude bots. ~155pts behind top (v5=1563, v17=1548, v3=1545). v6 is effectively a broken baseline bot.
30. **Rating history confirms v6 has NEVER changed** — stuck at 1408.06 from periods 696-809, then drifted to 1409.49 by period 819. Daemon only recently started using v6 (periods 810+). The bot was never actually competitive.
31. **All 27 gaps from experience pool are STILL PRESENT in v6.** None have been ported. v6 is essentially bot6 (the simplest reference bot) with no Chen table, no anti-bot4, no river overbet, wrong sim counts, dead weight everywhere.
32. **Key insight: v6 uses crude estimate_preflop_strength formula instead of PREFLOP_STRENGTH_TABLE.** This single change affects every preflop decision. Must port the 169-entry Chen lookup table first.
33. **v6 has harmful fixed preflop branches (bb_vs_raise, sb_vs_reraise) that bot5 removed.** These cause systematic misplays in common spots.
34. **Anti-bot4 system is completely absent from v6.** bot5 detects bot4 via stats and applies adjustments (bluff_freq_bonus, raise_size_bonus, call_threshold_delta, trap_defense_delta). v6 has zero counter-strategy.
35. **River overbet (choose_overbet_river) missing entirely from v6.** bot5 fires 1.5-2.2x pot with nuts on river for value extraction. This is a proven +EV play.
