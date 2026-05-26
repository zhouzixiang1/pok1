# Evolution Experience Pool
This file contains lessons learned from previous iterations of the poker bot. 
The Master Bot Architect must read this before planning the next generation to avoid repeating past mistakes.

### Consolidated Lessons (v8–v14)

1. **bb_vs_raise/sb_vs_reraise with fixed thresholds is ALWAYS harmful** (documented v8, v11, v15, ignored 4+ times). bot5 returns None for these spots, letting the general simulation-based path decide. This is PROVEN superior.
2. **thin_cap must be 0.30 (rounds ≤2) / 0.38 (round 3)** from bot5 (proven). The 0.46+0.08w formula has persisted 8+ generations. Also remove the `to_call==0` guard.
3. **River overbet (1.5–2.2x pot) for nut hands on dry rivers** is a proven edge. bot5 has `choose_overbet_river()`. Without it, the bot leaves significant value on the table.
4. **When changing preflop evaluation, ALL downstream thresholds must be recalibrated.** Chen table vs formula scale mismatch caused major regression in v13.
5. **Fix ALL parameter issues simultaneously.** Effects compound. v13→v14 failed because only 1 of 4 bugs was fixed at a time.
6. **Complex opponent profiling fails in 50-hand matches.** Confidence rarely reaches activation thresholds. Focus on additive features and parameter fixes.
7. **CBet tracking/drift detection add complexity without rating benefit.** bot5 (Rank 1 reference) doesn't have them.

### Generation v15 → v16 (2026-05-26)

- **Key Finding**: v10 = v11 = v15 = v16 (code-identical, MD5 79d511f). Rating differences (v11=1578, v15=1511) are pure Glicko variance. v16 was reset to v15 code before this evolution.
- **Reference Study**: Analyzed full diff between v15 and bot5 (432 lines in strategy.py). bot5 has: river overbet, fixed thin_cap 0.30/0.38, no bb_vs_raise/sb_vs_reraise, anti-bot4 detection (detect_bot4_profile/get_anti_bot4_adjustments), higher sim counts (900/1200/1500), VPIP prior 0.58/PFR prior 0.28, draw_potential function, check_probe_resistance_margin and must_continue_vs_raise in postflop.py.
- **Strategy**: Port ALL proven features from bot5 wholesale. Previous incremental attempts failed because changes were partial and missed dependencies. The full bot5 strategy.py (1160 lines) replaces v15's (1245 lines).
- **File Ownership**: Worker 1 handles strategy.py (copy from bot5 + verify imports). Worker 2 handles opponent.py + postflop.py (port new functions from bot5). Worker 3 handles constants.py (sim counts).
