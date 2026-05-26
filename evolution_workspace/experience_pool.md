# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated from v8–v17)

1. **bb_vs_raise/sb_vs_reraise fixed thresholds ALWAYS harmful** (v8,v11,v15). bot5 returns None, letting simulation decide. PROVEN.
2. **thin_cap = 0.30 (round≤2) / 0.38 (round≥3)**, NO `to_call==0` guard. The 0.46+0.08w formula persisted 8+ gens.
3. **River overbet (1.5–2.2x pot) for nut hands on dry rivers** is proven edge (bot5 `choose_overbet_river`).
4. **When changing preflop eval, recalibrate ALL downstream thresholds.** Chen vs formula scale mismatch caused v13 regression.
5. **Fix ALL parameter issues simultaneously.** Effects compound. v13→v14 failed fixing 1 of 4 bugs at a time.
6. **Complex opponent profiling fails in 50-hand matches.** Focus on additive features.
7. **CBet/drift detection adds complexity without rating benefit.** bot5 (Rank 1) doesn't have them.
8. **Anti-bot4 detection + adjustments are proven value** (bot5 detect_bot4_profile, get_anti_bot4_adjustments). Bypass conservative checks when bot4 detected.
9. **Wholesale copy fails** (v16=1349). Over-engineering fails (v17=1450, 7753 lines). Incremental port wins.
10. **allow_low_frequency_blocker_bluff needs bluff_freq_bonus param** for anti-bot4 integration.
11. **choose_raise needs anti_bot4_bonus + allow_river_overbet params.** Max_ratio 2.2 on river with nut hands extracts maximum value.
12. **EQR air values must match bot5: 0.72 IP / 0.62 OOP** (v6 has 0.68/0.56). Under-realized bluff equity loses value.
13. **Opponent model priors: vpip=0.58, pfr=0.28** (bot5). v6 uses 0.52/0.24 — shifts entire range evaluation.
14. **Confidence divisor: 35** (bot5) vs 30 (v6). Faster trust in opponent model is better.
15. **gift_balance / exploit_lambda / cbet / drift are dead weight.** bot5 doesn't have them. Remove.
16. **Chen preflop table is essential.** Formula-based estimate_preflop_strength is inaccurate. Precomputed 169-hand table in constants.py.
17. **Simulation counts matter: {0:900, 3:1200, 4:1500}** with extras {0:300, 3:350, 4:300}. v6 runs too few sims.
18. **check_probe_resistance_margin + must_continue_vs_raise belong in postflop.py** (bot5 structure). Keep imports clean.

### v6→v7 Execution Plan
- **Source**: claude_v6 (r=1420, rd=43.3). ~150pts behind leaders (v3=1571, v11=1540).
- **Strategy**: Port ALL 8+ bot5 gaps simultaneously (lessons 5, 12-18).
- **3 workers**: (A1) constants+state+opponent+postflop structural, (A2) strategy logic rewrite, (B) sim counts.
