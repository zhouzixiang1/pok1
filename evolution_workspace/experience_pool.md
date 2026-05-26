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

### Generation v6 → v7 Strategy (current)
- **Source**: claude_v6 (r=1480, rd=42.8). Clean 7-file modular structure.
- **Reference**: bot5 (top reference bot, proven anti-exploitation framework).
- **Key diffs (v6 vs bot5)**:
  1. v6 has bb_vs_raise/sb_vs_reraise (lines 554-597) → REMOVE, return None
  2. v6 lacks choose_overbet_river → PORT from bot5
  3. v6 thin_cap uses wrong formula + to_call==0 guard → FIX to 0.30/0.38
  4. v6 lacks detect_bot4_profile/get_anti_bot4_adjustments → PORT to opponent.py
  5. v6 allow_low_frequency_blocker_bluff lacks bluff_freq_bonus → ADD param
  6. v6 choose_raise lacks anti_bot4_bonus/allow_river_overbet → ADD params + max_ratio=2.2
  7. v6 bad_river_bluff_candidate/thin_static_showdown_control lack anti_bot4 bypass → ADD
  8. v6 threshold formulas lack anti_bot4 bluff_freq_bonus term → ADD
- **Approach**: Port complete subsystems (anti-bot4 + river overbet) as units into v6's clean structure.
- **Tournament hyperparams**: v6 chase=0.85, threshold=-0.070, sizing=0.16, bluff=0.11 vs bot5's 0.90, -0.075, 0.18, 0.13.
