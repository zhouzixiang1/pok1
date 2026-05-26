# Evolution Experience Pool
This file contains lessons learned from previous iterations of the poker bot. 
The Master Bot Architect must read this before planning the next generation to avoid repeating past mistakes.

### Consolidated Lessons (v8–v16)

1. **bb_vs_raise/sb_vs_reraise with fixed thresholds is ALWAYS harmful** (documented v8, v11, v15, ignored 4+ times). bot5 returns None for these spots, letting the general simulation-based path decide. This is PROVEN superior.
2. **thin_cap must be 0.30 (rounds ≤2) / 0.38 (round 3)** from bot5 (proven). The 0.46+0.08w formula has persisted 8+ generations. Also remove the `to_call==0` guard.
3. **River overbet (1.5–2.2x pot) for nut hands on dry rivers** is a proven edge. bot5 has `choose_overbet_river()`. Without it, the bot leaves significant value on the table.
4. **When changing preflop evaluation, ALL downstream thresholds must be recalibrated.** Chen table vs formula scale mismatch caused major regression in v13.
5. **Fix ALL parameter issues simultaneously.** Effects compound. v13→v14 failed because only 1 of 4 bugs was fixed at a time.
6. **Complex opponent profiling fails in 50-hand matches.** Confidence rarely reaches activation thresholds. Focus on additive features and parameter fixes.
7. **CBet tracking/drift detection add complexity without rating benefit.** bot5 (Rank 1 reference) doesn't have them.

### Generation v16 → v17 (2026-05-26)
- v16 attempted wholesale bot5 copy but failed spectacularly (rating 1349, worst performer).
- v17 over-engineered to 7753 lines across 21 files. Rating 1450 — too many abstractions.

### Generation v6 → v7 Analysis (current)
- **Source**: claude_v6 (rating 1449, rd=42.8). Modular 7-file structure, clean code.
- **Reference**: bot5 (the actual top-performing reference bot, v5/v9/v11 all derived from it).
- **Key gaps identified by diffing v6 strategy.py (1246 lines) vs bot5 strategy.py (1161 lines)**:
  1. v6 has bb_vs_raise/sb_vs_reraise preflop logic (lines 554-597) — PROVEN harmful, must remove.
  2. v6 lacks `choose_overbet_river()` — river overbet for nut hands on dry boards.
  3. v6 lacks `choose_overbet_bluff_river()` stub (placeholder for safety).
  4. v6 thin_cap uses formula `0.46 + 0.08 * wetness + 0.05 * max(0, round_idx - 1)` instead of fixed `0.30 / 0.38`.
  5. v6 `choose_raise` lacks `allow_river_overbet` param and max_ratio=2.2 logic.
  6. v6 lacks anti-bot4 detection (`detect_bot4_profile`, `get_anti_bot4_adjustments`).
  7. v6 `allow_low_frequency_blocker_bluff` lacks `bluff_freq_bonus` parameter.
  8. v6 lacks `bad_river_bluff_candidate` anti-bot4 bypass (`anti_bot4["bluff_freq_bonus"] < 0.05`).
  9. v6 lacks `thin_static_showdown_control` anti-bot4 bypass.
- **Strategy**: Port the 5 missing features from bot5 INCREMENTALLY into v6's clean structure. Do NOT wholesale copy — v6 already has good drift detection, cbet tracking, and realized equity that bot5 lacks.
- **Risk**: Previous wholesale copies (v16) failed. Previous incremental changes (v8-v15) partially failed because they missed dependencies. This time we port complete subsystems (anti-bot4 + river overbet) as units.
