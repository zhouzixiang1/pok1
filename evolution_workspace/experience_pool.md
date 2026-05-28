# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (v1–v10 consolidated)

1. **Chen preflop table worth ~130pts.** 169-hand lookup replaces crude formula.
2. **Anti-bot4 detection = modest edge vs bot4, neutral vs field.** Not a major differentiator.
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** = proven value extraction.
4. **Simulation counts: lower is fine with good calibration.** v2 #1 with {0:500}. v13 #1 with same.
5. **Priors: v13 uses vpip=0.52, pfr=0.24 (better than 0.58/0.28).** Lower priors = less aggressive early exploitation.
6. **EQR values NOT universally lower-is-better.** v13 (#1, 1646) uses air IP=0.68/OOP=0.56, pair IP=0.84/OOP=0.73. Context matters.
7. **Blocker bluff: deterministic token-based randomization (v13) works better than random.random().** Reproducible.
8. ~~Dead weight removed~~ **REVISED:** v13 proves cbet_rate, drift detection, gift_balance, exploit_lambda ARE valuable. Lesson 8 was wrong for v8's context.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.**
10. ~~bb_vs_raise: Let simulation decide~~ **REVISED:** v13 has explicit bb_vs_raise 3bet bluff + sb_vs_reraise 4bet logic and is #1. Complete preflop logic matters.
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Incremental targeted port wins.** CONFIRMED by v10 breakthrough.
13. **Fix ALL parameter issues simultaneously.** Effects compound.
14. **choose_raise thin_cap: v13 uses wetness-aware formula (0.46+0.08*wet+0.05*round)** — superior to v9's flat 0.30/0.38.

### Structural Advantages (v13 analysis — #1 at 1646)

15. **Complete preflop logic is critical.** bb_vs_raise (3bet bluff) and sb_vs_reraise (4bet) add significant EV vs bots that fold too much preflop.
16. **gift_balance + exploit_lambda modulates GTO/exploit blend.** Track opponent losses, exploit harder when ahead.
17. **Concept drift detection adjusts model when opponent shifts behavior.** Uses recent-10 data when drift detected.
18. **CBet rate adjustments:** cbet>0.65 → call_margin-=0.02, cbet<0.40 → call_margin+=0.02. Exploits cbet patterns.
19. **OOP draw EQR discount path:** draw_str>=0.08 + made<0.18 + OOP → EQR 0.85(flop)/0.75(turn+). Prevents overvaluing OOP draws.
20. **must_continue_vs_raise should protect strong combo draws.** draw_strength>=0.20, pot_odds<=0.38. From v9.
21. **big_pot_safety_guard prevents catastrophic barreling.** pot>7000, turn/river, thin/marginal, no draw.
22. **River exact equity override (0 sims):** raise if wr>0.85, fold if wr<0.15-pot_odds_margin. Pure win.
23. **min_raise_action .get() fallback is essential.** state.get("min_raise_action", state["round_raise"]).

### Strategy Principles

24. **Starting from #1 bot + targeted tweaks > complex rewrites.** v10 confirmed this. Now branching from v13.
25. **ADD new decision paths only, never modify existing calibrated logic.** Style deltas default to zero for unknown opponents.
26. **Anti-bot4 and style classification: uncertain value vs field.** v13 leads without them. Port only proven features.
