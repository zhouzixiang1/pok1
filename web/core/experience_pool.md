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

### v8→v9: Crossover with v3 Features

21. **v2 still #1 (1552) after 164 periods.** v8 at 1498 despite more features — base calibration matters most.
22. **v3 EXP3 costs ~20pts but STYLE PARAMS are valuable.** classify_opponent_style() + direct threshold deltas. Skip the bandit.
23. **River Refinement is a clean win.** exact equity on river (0 sims), force raise exact_wr>0.85, fold exact_wr<0.15.
24. **Crossover rule: ADD new decision paths only, never modify existing v8 logic.** Style deltas default to zero for unknown opponents.
25. **5 opponent types: nit (low VPIP, high fold), maniac (high VPIP/PFR/aggr), calling station (high VPIP, low PFR), fold-heavy (high fold_to_raise), balanced/unknown.**
26. **Air EQR: lower IP 0.68→0.65, OOP 0.56→0.53.** Marginal pair: IP 0.84→0.82, OOP 0.73→0.70.
