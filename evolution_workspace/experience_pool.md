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
- **Source**: claude_v6 (r=1438.8, rd=43.6). 2nd-worst of all claude bots. 130pts behind v12 (1568). Declining trend (1476→1439 over last 10 periods).
- **Reference**: bot5 (anti-exploitation framework, Rank 1). bot5 has NO bb_vs_raise/sb_vs_reraise handlers, returns None.
- **5 critical gaps identified**:
  1. `bb_vs_raise` has hardcoded logic (lines 526-558): 3bet with strength≥0.72, bluff with 0.38-0.52, fold/call thresholds. bot5 returns None → simulation handles it. REMOVE these handlers.
  2. `sb_vs_reraise` has hardcoded logic (lines 560-573): 4bet with 0.85+, call with 0.60+. bot5 returns None. REMOVE.
  3. `thin_cap` wrong formula: `0.46 + 0.08*wetness + 0.05*max(0, round_idx-1)` with `to_call==0` guard (line 452). Should be `0.30 if round_idx<=2 else 0.38` with NO guard.
  4. No `choose_overbet_river` function. Need river overbet (1.5-2.2x pot) for nut hands on dry rivers.
  5. No anti-bot4 framework. Need `detect_bot4_profile`, `get_anti_bot4_adjustments` in opponent.py. Integration in strategy.py: `bluff_freq_bonus` → `allow_low_frequency_blocker_bluff`, `raise_size_bonus` → `choose_raise`, `call_threshold_delta` → strong/medium thresholds.
- **Anti-lock params gap**: v6 chase=0.85/bluff=0.11 vs bot5 0.90/0.13. threshold_delta cap -0.070 vs -0.075. sizing_delta 0.16 vs 0.18.
- **Integration points**: anti_bot4 adjustments → strong/medium thresholds, bluff thresholds, choose_raise (anti_bot4_bonus, allow_river_overbet, max_ratio=2.2), bad_river_bluff_candidate/thin_static_showdown_control guards with `bluff_freq_bonus < 0.05`.
