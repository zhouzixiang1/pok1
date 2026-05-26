# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated from v8–v17)

1. **bb_vs_raise/sb_vs_reraise fixed thresholds ALWAYS harmful** (v8,v11,v15). bot5 returns None, letting simulation decide. PROVEN.
2. **thin_cap = 0.30 (round≤1) / 0.38 (round≥2)**, NO `to_call==0` guard. The 0.46+0.08w formula persisted 8+ gens.
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
- **Source**: claude_v6 (r=1510, rd=43.5). Trend: stable 1470–1510 over 200 periods. ~83pts behind v2 (1593).
- **Reference**: bot5 (anti-exploitation framework, Rank 1).
- **Anti-lock params already correct**: v6 chase=0.90, bluff=0.13 EXCEEDS bot5 (0.85/0.11). No tuning needed.
- **match_pressure_profile**: threshold_delta coefficients differ slightly (v6: 0.055/0.055 vs bot5: 0.050/0.060). Minor tuning opportunity.
- **5 critical gaps vs bot5**: (1) No anti-bot4 detection/adjustments, (2) bb_vs_raise/sb_vs_reraise hardcoded instead of None, (3) thin_cap uses wrong formula 0.46+0.08w instead of 0.30/0.38, (4) No river overbet for nut hands, (5) No anti_bot4 bypasses in conservative guards (bad_river_bluff_candidate, thin_static_showdown_control).
