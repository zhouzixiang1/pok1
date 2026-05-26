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

### v6→v7 Plan (current)
- **Source**: claude_v6 (r=1429.6, rd=43.2). 2nd-worst, ~150pts behind v2/v5. Completely flatlined across 120+ rating periods.
- **Strategy**: Comprehensive incremental port of ALL bot5 proven features simultaneously (lesson 5). 3 workers: strategy refactor + opponent/postflop + hyperparams.
- **Key changes**: (a) Remove bb_vs_raise/sb_vs_reraise → return None (lesson 1), (b) Fix thin_cap to 0.30/0.38 (lesson 2), (c) Add river overbet (lesson 3), (d) Add anti-bot4 detection + integration (lessons 8,10,11), (e) Chen preflop table + precomputed lookups, (f) Higher sim counts (900/1200/1500), (g) Fix EQR values to bot5 levels, (h) Remove gift tracking/cbet adjustments/drift detection (lessons 6,7), (i) Fix anti-lock pressure values.

### Key Differences: v6 vs bot5 (comprehensive)
- **strategy.py**: v6 has hardcoded preflop logic (bb_vs_raise, sb_vs_reraise) — bot5 returns None. v6 missing choose_overbet_river, choose_overbet_bluff_river. v6 thin_cap wrong formula. v6 choose_raise missing anti_bot4_bonus/allow_river_overbet. v6 has gift tracking (not in bot5). v6 EQR values lower (0.68/0.56 vs 0.72/0.62) with tighter clamps. v6 has cbet adjustments (not in bot5). v6 has preflop_trash_hand guard on anti_lock (bot5 doesn't).
- **opponent.py**: v6 has cbet tracking + drift detection (bot5 doesn't). v6 priors differ (vpip 0.52, pfr 0.24 vs bot5 0.58/0.28). v6 confidence divisor 30 vs bot5 35. v6 missing detect_bot4_profile, get_anti_bot4_adjustments.
- **postflop.py**: v6 allow_low_frequency_blocker_bluff missing bluff_freq_bonus param. v6 check_probe_resistance_margin and must_continue_vs_raise are in strategy.py (bot5 has them in postflop.py).
- **constants.py**: v6 sim counts too low (400/800/900 vs 900/1200/1500). v6 missing PREFLOP_STRENGTH_TABLE, CARD_RANKS, CARD_SUITS.
- **state.py**: v6 uses formula-based estimate_preflop_strength. bot5 uses Chen lookup table.
- **tournament.py**: v6 anti-lock pressure values lower (chase 0.85 vs 0.90, threshold -0.070 vs -0.075, sizing 0.16 vs 0.18, bluff 0.11 vs 0.13).
