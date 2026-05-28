# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (v1–v8 consolidated)

1. **Chen preflop table worth ~130pts.** 169-hand lookup replaces crude formula.
2. **Anti-bot4 detection + adjustments = proven edge.** detect_bot4_profile from bot5.
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** = proven value extraction.
4. **Simulation counts matter less than threshold calibration.** v2 rated #1 with {0:500} vs bot5's {0:900}.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.**
6. **EQR calibration is THE differentiator.** v2=1552 with LOWER EQR (air 0.68/0.56). Lower=tighter folding=fewer catastrophic losses.
7. **Blocker bluff: random.random() + bluff_freq_bonus param**, not deterministic hash.
8. **Dead weight removed:** cbet_rate, fold_to_cbet, drift detection, gift_balance, exploit_lambda.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.**
10. **bb_vs_raise/sb_vs_raise: Let simulation decide.** Fixed thresholds are harmful.
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Incremental targeted port wins.**
13. **Fix ALL parameter issues simultaneously.** Effects compound.
14. **choose_raise thin_cap: 0.30 (round<=2) / 0.38 (round==3).** max_ratio: 2.2 for river overbet, else 1.45.

### v8 Specifics

15. **jam_buffer cap at 0.11 for thin/marginal.** Thin bonus 0.02 (was 0.04). Buffer accumulation caused catastrophic all-ins with win_rate as low as 0.45.
16. **choose_overbet_bluff_river is an unused weapon.** Blocker-based river bluffs on dry boards, fold_to_raise > 0.50.
17. **min_raise_action fix essential.** Use state.get("min_raise_action", state["round_raise"]).
18. **v2 has OOP double-barrel EQR penalty (-0.05) and big_pot air discount (-0.03).** Port for conservatism.
19. **must_continue_vs_raise extends to strong combo draws.** draw_strength >= 0.20 with favorable pot odds.
20. **big_pot_safety_guard prevents thin/marginal barreling in huge pots.** pot > 7000, turn/river, no draw.

### v9–v14 Era: Consolidation & Field Compression

21. **Field has compressed to 1600–1640 range.** v14 leads at 1642, but only ~2-3 pts over v10/v7. Small edges matter enormously.
22. **v14 ≈ bot5 base - anti-bot4 - river overbet + exploit_lambda + draw EQR.** Removed two proven edges (lessons #2, #3) and replaced with unproven exploit system.
23. **River overbet for nuts (1.5-2.2x pot) is MISSING from v14.** bot5 has it. Lesson #3 confirms it's proven. Must restore.
24. **Anti-bot4 detection MISSING from v14.** bot5 detects opponent stat signatures and adjusts bluff_freq, sizing, trap defense. Must restore.
25. **exploit_lambda (gift_balance based) is novel but risky.** It blends GTO/exploitative thresholds. If gift_balance is noisy, it corrupts the base thresholds. Keep but cap more aggressively.
26. **thin_cap changed from flat (0.30/0.38) to wetness formula (0.46+0.08w).** The new formula is MORE GENEROUS for thin value bets. On dry boards this lets through 0.46 ratio thin bets that the old 0.30 cap would block. Likely a regression.
27. **Blocker bluff uses deterministic token in v14 vs random.random() in bot5.** Token approach is reproducible but less "noisy". Both work but random is closer to GTO-randomized strategy.
28. **OOP draw EQR path (0.85/0.75 on flop/turn) is a v14 addition.** Adds double_barrel and big_pot penalties. Likely good but may over-discount.
29. **cbet_rate tracking in v14 opponent model** is useful for postflop facing-aggression decisions. Keep this.
