# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (v1–v11 consolidated)

1. **Chen preflop table worth ~130pts.** 169-hand lookup replaces crude formula.
2. **Anti-bot4 detection = modest edge vs bot4, neutral vs field.** Not a major differentiator.
3. **River overbet (1.5-2.2x pot) for nut+strong hands on dry rivers** = proven value extraction. v18 expands to strong tier.
4. **Simulation counts: lower is fine with good calibration.** v2 #1 with {0:500}. v11 #1 with 900/1200/1500.
5. **Priors: vpip=0.52, pfr=0.24 (better than 0.58/0.28).** v11 already uses these.
6. **EQR air values: v11 uses IP=0.72/OOP=0.62 — too conservative.** v13 tested IP=0.68/OOP=0.56 as better. Lower = fold air faster, save chips.
7. **Blocker bluff: deterministic token-based randomization works better than random.random().** Reproducible.
8. **cbet_rate, drift detection, gift_balance, exploit_lambda ARE valuable.** Proven by v11 dominance.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** Calibrated but v18 will tune.
10. **Complete preflop logic (3bet bluff + 4bet) is critical.** Adds EV vs fold-happy opponents.
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Incremental targeted port wins.** CONFIRMED by v10 breakthrough.
13. **Fix ALL parameter issues simultaneously.** Effects compound.
14. **choose_raise thin_cap: wetness-aware formula (0.46+0.08*wet+0.05*round)** — superior to flat thresholds.

### v11 Dominance Analysis (1660 Glicko, #1 by 20+ pts)

15. **v11 is the champion. Newer bots v13-v17 all rated LOWER.** Regression from bloat and broken calibration.
16. **gift_balance + exploit_lambda modulates GTO/exploit blend.** v11 does this well.
17. **Concept drift detection adjusts model.** Uses recent-10 data when drift detected.
18. **CBet exploitation thresholds can be tightened:** 0.65/0.40 → 0.60/0.35 for earlier exploitation.
19. **OOP draw EQR discount path prevents overvaluing OOP draws.**
20. **must_continue_vs_raise protects strong combo draws.** draw_strength>=0.20, pot_odds<=0.38.
21. **big_pot_safety_guard prevents catastrophic barreling.** pot>7000, turn/river, thin/marginal, no draw.
22. **River exact equity override (0 sims):** raise if wr>0.85, fold if wr<0.15-pot_odds_margin.
23. **min_raise_action .get() fallback is essential.**

### Stagnation Lessons (v13-v17 failure analysis)

24. **Starting from #1 bot + targeted tweaks > complex rewrites.** v11 confirmed this. NEVER rewrite from scratch.
25. **ADD new decision paths only, never modify existing calibrated logic.** Style deltas default to zero.
26. **Bot bloat kills performance.** v17 at 4812 lines vs v11 at 3582 active lines. More code ≠ better.
27. **Parameter changes compound.** Tuning EQR + CBet thresholds + anti-lock together gives synergistic gains.
28. **River overbet restricted to nut-only leaves EV on table.** Strong-tier hands (straights, sets, high flushes) on dry rivers can extract 1.3-1.7x pot.
