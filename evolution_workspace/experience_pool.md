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
- **Source**: claude_v6 (r=1462, rd=44.1). BOTTOM of all claude bots. ~120pts behind v2 (1580). Declining trend.
- **Reference**: bot5 (anti-exploitation framework, Rank 1).
- **5 critical gaps confirmed**: (1) bb_vs_raise/sb_vs_reraise hardcoded (lines 554-600) instead of returning None like bot5, (2) thin_cap uses wrong formula 0.46+0.08*w with to_call==0 guard instead of 0.30/0.38, (3) No river overbet for nut hands, (4) No anti-bot4 detection framework, (5) allow_low_frequency_blocker_bluff missing bluff_freq_bonus param.
- **Anti-lock params**: v6 chase=0.85/bluff=0.11 vs bot5 0.90/0.13. threshold_delta cap -0.070 vs -0.075. sizing_delta 0.16 vs 0.18.
- **Strategy**: Fix ALL 5 gaps simultaneously. Port detect_bot4_profile + get_anti_bot4_adjustments from bot5/opponent.py. Remove hardcoded bb_vs_raise/sb_vs_reraise blocks. Fix thin_cap. Add choose_overbet_river + integration. Update anti-lock params.
- **Key integration points**: anti_bot4 adjustments affect strong/medium thresholds (line 684-686 bot5), bluff thresholds (line 1078-1080), choose_raise params (anti_bot4_bonus, allow_river_overbet), and bad_river_bluff_candidate/thin_static_showdown_control guards.