# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated v8-v17, re-validated v6->v7)

1. **bb_vs_raise/sb_vs_reraise fixed thresholds ALWAYS harmful** (v8,v11,v15). Let simulation decide.
2. **thin_cap = 0.30 (round<=2) / 0.38 (round>=3)**, NO to_call==0 guard.
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** is proven edge (bot5 choose_overbet_river).
4. **When changing preflop eval, recalibrate ALL downstream thresholds.** Chen vs formula mismatch caused v13 regression.
5. **Fix ALL parameter issues simultaneously.** Effects compound. One-at-a-time fails.
6. **Complex opponent profiling fails in 50-hand matches.** Focus on additive features.
7. **CBet/drift detection = dead weight.** bot5 does not have them. Remove.
8. **Anti-bot4 detection + adjustments are proven value.** Bypass conservative checks when bot4 detected.
9. **Wholesale copy fails** (v16=1349). Over-engineering fails (v17=1450, 7753 lines). Incremental port wins.
10. **allow_low_frequency_blocker_bluff: use random.random() + bluff_freq_bonus param**, not deterministic hash.
11. **choose_raise needs anti_bot4_bonus + allow_river_overbet params.** Max_ratio 2.2 on river with nuts.
12. **EQR air: 0.72 IP / 0.62 OOP, lower bound 0.45.** Under-realized bluff equity loses value.
13. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.** Wrong priors shift entire range.
14. **gift_balance / exploit_lambda / gto_strong blending = dead weight.** Remove.
15. **Chen preflop table essential.** Formula-based estimate_preflop_strength inaccurate. 169-hand table.
16. **Simulation counts: {0:900, 3:1200, 4:1500}** extras {0:300, 3:350, 4:300}.
17. **Dead EQR branches in v6** (draw_strength OOP, big_pot, double_barrel OOP extra). bot5 does not have these. Remove.
18. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.**
19. **threshold_delta: 0.055*protect - 0.055*chase** (symmetric, not 0.050/0.060).
20. **CARD_RANKS/CARD_SUITS precomputed arrays** in constants.py.
21. **check_probe_resistance_margin + must_continue_vs_raise** are critical call/fold helpers in bot5 postflop.

### v6->v7 Strategy
- **Source**: claude_v6 (r=1408, worst claude bot, 131pts behind leader v15=1540)
- **Reference**: bot5 (anti-exploitation framework, structural features)
- **v6 trend**: Declined 1462->1408 over 101 periods. Dead last among claude bots.
- **Root cause**: v6 has NO anti-bot4 detection, NO river overbet, formula-based preflop (not Chen table), wrong EQR/priors, dead weight code adding noise. These are structural gaps, not parameter issues.
- **Priority**: Chen table > anti-bot4 > river overbet > missing postflop funcs > dead weight removal > param fixes
- **4 workers**: W1(A): constants+card_utils+state, W2(A): opponent+postflop, W3(A): strategy, W4(B): tournament params
