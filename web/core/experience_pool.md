# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (v1–v8 consolidated)

1. **Chen preflop table: v4 (#1 at 1623) succeeds WITHOUT it using formula.** Table may introduce calibration noise. Formula is sufficient.
2. **Anti-bot4 detection with LOOSE criteria is catastrophic.** v14 collapsed 100pts. Only use with VERY tight thresholds (±0.08).
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** = proven value extraction. v4 lacks it. Must add.
4. **Simulation counts: 700 flop sims is fine.** v2 rated #1 with {0:500}. More sims ≠ better play if thresholds are wrong.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.** v4 uses these. Proven correct.
6. **EQR calibration: v4 air 0.72/0.62 is optimal.** v14 lowered to 0.68/0.56 + extra penalties causing over-folding. DO NOT over-discount. **v17 still uses 0.68/0.56 — MUST FIX.**
7. **Blocker bluff: random.random() or deterministic hash both work.** Not a major differentiator.
8. **exploit_lambda (gift_balance blending) is HARMFUL.** It corrupts GTO base thresholds. DO NOT use.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** Proven values.
10. **bb_vs_raise/sb_vs_raise: Let simulation decide.** Hardcoded 3bet bluff spots are net negative.
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Incremental targeted port wins.**
13. **Fix ALL parameter issues simultaneously.** Effects compound.
14. **thin_cap: wetness-aware formula (0.46+0.08*wet+0.05*round) superior to flat thresholds.** v17 uses flat 0.30/0.38 — should upgrade.

### v9–v15 Era: Field Compression & Key Feature Gaps

15. **Field compressed to ~25pts.** Small edges matter enormously. Only port proven features.
16. **CBet tracking IS useful.** v15 has tighter thresholds (0.60/0.35 vs v11's 0.65/0.40). v17 uses 0.65/0.40 — tighten to 0.60/0.35.
17. **River exact equity (0 sims when 5 public cards) is a proven feature.** v17 HAS this — keep it.
18. **classify_opponent_style (nit/maniac/station/fold-heavy) provides +2-3 pts of adaptation.** v17 LACKS this entirely. PRIORITY ADD.
19. **Big pot call margin (pot>5000 guard) prevents over-folding in large pots.** v17 lacks explicit guard.
20. **River overbet for strong-tier hands** (straights, sets, high flushes) on dry rivers at 1.3-1.7x pot. v17 doesn't have this. Potential +3-5 pts.
21. **File size: strategy.py at 1245 lines is near 1000-line soft limit.** Any additions must be paired with removals.

### v17 Architecture & Dead Code

22. **v17 has 6 dead code files NOT imported anywhere:** draws.py(261), bluffs.py(186), postflop_decision.py(269), preflop.py(133), main_backup.py(2941), odds.py(317) = 4107 lines dead. Active code: 3646 lines across 15 files. Dead files don't affect runtime but confuse future workers — DELETE them.
23. **v17 peaked at 1642.7, dropped to 1621.4.** v9 stable at 1625.9. Gap closing. Parameter fixes + missing features can reclaim lead.
24. **EQR draw OOP values:** v17 uses 0.85/0.75 (flop/turn). Experience shows 0.88/0.78 gives better realization. Adjust.
25. **Parameter changes compound synergistically.** EQR + CBet + thin_cap + opponent style all together outperform any single change.
