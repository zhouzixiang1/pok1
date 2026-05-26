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
- **Source**: claude_v6 (r=1429.6, rd=43.2). 2nd-worst, ~160pts behind v2/v12. Flatlined 15+ periods.
- **Strategy**: Incremental port of 5 proven bot5 features. Fix ALL issues simultaneously (lesson 5).
- **Tasks**: 3 workers — 2 algorithmic (preflop/raise + opponent/integration) + 1 hyperparam tuner.
- **Key risk**: bb_vs_raise/sb_vs_reraise removal is most impactful — lets simulation handle complex preflop spots instead of hardcoded thresholds that misread opponent ranges.

### Key Differences: v6 vs bot5 (remaining after v7 fix)
- v6 realized_postflop_equity has extra big_pot param (minor). v6 postflop_call_margin identical.
- v6 tournament.py identical except anti-lock params (tuned in worker 3).
- After v7, main structural difference should be minimal — v7 ≈ v6 base + bot5 proven features.
