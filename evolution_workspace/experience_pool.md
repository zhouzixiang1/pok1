# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated from v8–v17)

1. **bb_vs_raise/sb_vs_reraise fixed thresholds ALWAYS harmful** (v8,v11,v15). bot5 returns None, letting simulation decide. PROVEN.
2. **thin_cap = 0.30 (round≤2) / 0.38 (round 3)**, NO `to_call==0` guard. The 0.46+0.08w formula persisted 8+ gens.
3. **River overbet (1.5–2.2x pot) for nut hands on dry rivers** is proven edge (bot5 `choose_overbet_river`).
4. **When changing preflop eval, recalibrate ALL downstream thresholds.** Chen vs formula scale mismatch caused v13 regression.
5. **Fix ALL parameter issues simultaneously.** Effects compound. v13→v14 failed fixing 1 of 4 bugs at a time.
6. **Complex opponent profiling fails in 50-hand matches.** Focus on additive features.
7. **CBet/drift detection adds complexity without rating benefit.** bot5 (Rank 1) doesn't have them.
8. **Anti-bot4 detection + adjustments are proven value** (bot5 has detect_bot4_profile, get_anti_bot4_adjustments). These bypass conservative checks (bad_river_bluff_candidate, thin_static_showdown_control) when bot4 detected.
9. **Wholesale copy fails** (v16=1349). Over-engineering fails (v17=1450, 7753 lines). Incremental port wins.
10. **allow_low_frequency_blocker_bluff needs bluff_freq_bonus param** for anti-bot4 integration. Without it, bluff frequency can't adapt to detected opponent type.
11. **choose_raise needs anti_bot4_bonus + allow_river_overbet params.** Max_ratio 2.2 on river with nut hands extracts maximum value.

### v6→v7 Status (current)
- **Source**: claude_v6 (r=1500, clean 7-file modular structure, 6149 lines).
- **Reference**: bot5 (proven anti-exploitation framework).
- **v6 rating trend**: Stable 1443→1500 over last 20 periods. Not stagnating but ~75pts behind v2 (1575).
- **All 8 diffs from experience pool entry #7 remain unaddressed** — these are the v6→v7 tasks.

### Key v6→v7 Changes Required
1. REMOVE bb_vs_raise/sb_vs_reraise blocks (lines 554-597) → return None
2. FIX thin_cap: 0.30/0.38 without to_call==0 guard
3. PORT detect_bot4_profile + get_anti_bot4_adjustments to opponent.py
4. ADD bluff_freq_bonus param to allow_low_frequency_blocker_bluff (postflop.py)
5. ADD anti_bot4_bonus + allow_river_overbet params to choose_raise, max_ratio=2.2
6. PORT choose_overbet_river to strategy.py
7. ADD anti_bot4 bypasses to bad_river_bluff_candidate + thin_static_showdown_control
8. ADD bluff_freq_bonus to river_bluff/probe_fold/semi_bluff thresholds
9. TUNE tournament anti-lock params toward bot5 values (chase 0.85→0.90, bluff 0.11→0.13)
