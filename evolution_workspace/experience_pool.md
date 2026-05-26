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

### v6→v7 Analysis (current)
- **Source**: claude_v6 (r=1467, rd=44.3). Trend: declining 1520→1465 over 10 periods. ~145pts behind v2 (1592). BOTTOM of all claude bots.
- **Reference**: bot5 (anti-exploitation framework, Rank 1).
- **5 critical gaps vs bot5**: (1) No anti-bot4 detection/adjustments, (2) bb_vs_raise/sb_vs_reraise hardcoded instead of returning None, (3) thin_cap uses wrong formula 0.46+0.08w+to_call==0 guard instead of 0.30/0.38, (4) No river overbet for nut hands, (5) No anti_bot4 bypasses in conservative guards.
- **CORRECTION**: threshold_delta already correct in v6 (0.050/0.060 vs bot5 0.055/0.055). Experience pool had it backwards. NO change needed.
- **Anti-lock params need updating**: v6 has chase=0.85/bluff=0.11, bot5 has 0.90/0.13. Also threshold_delta cap -0.075 vs -0.070, sizing_delta 0.18 vs 0.16.
- **Strategy**: Port anti-bot4 framework from bot5. Remove hardcoded preflop spots. Fix thin_cap. Add river overbet. This addresses ALL 5 gaps simultaneously.
