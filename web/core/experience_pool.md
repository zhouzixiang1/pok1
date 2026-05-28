# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (v1–v8 consolidated)

1. **Chen preflop table: v4 (#1 at 1623) succeeds WITHOUT it using formula.** Table may introduce calibration noise. Formula is sufficient.
2. **Anti-bot4 detection with LOOSE criteria is catastrophic.** v14 collapsed 100pts. Only use with VERY tight thresholds (±0.08).
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** = proven value extraction. v4 lacks it. Must add.
4. **Simulation counts: 700 flop sims is fine.** v2 rated #1 with {0:500}. More sims ≠ better play if thresholds are wrong.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.** v4 uses these. Proven correct.
6. **EQR calibration: v4 air 0.72/0.62 is optimal.** v14 lowered to 0.68/0.56 + extra penalties causing over-folding. DO NOT over-discount.
7. **Blocker bluff: random.random() or deterministic hash both work.** Not a major differentiator.
8. **exploit_lambda (gift_balance blending) is HARMFUL.** It corrupts GTO base thresholds. DO NOT use.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** Proven values.
10. **bb_vs_raise/sb_vs_raise: Let simulation decide.** Hardcoded 3bet bluff spots are net negative.
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Incremental targeted port wins.**
13. **Fix ALL parameter issues simultaneously.** Effects compound.
14. **thin_cap: 0.30 (round<=2) / 0.38 (round==3).** v4's flat values work best.

### v9–v15 Era: Field Compression & v11 Champion

15. **Field compressed to ~35pts (1597-1632).** Small edges matter enormously. Only port proven features.
16. **v11 is current champion at 1632.** Has river exact equity, classify_opponent_style, big_pot_safety_guard, river overbet bluff, CBet bluff exploitation.
17. **CBet tracking IS useful.** v15 has it with tighter thresholds (0.60/0.35 vs v11's 0.65/0.40). v15's tighter values may be better.
18. **v15 EQR regression: air 0.68/0.56, OOP draw 0.85/0.75, pair 0.84/0.73** all lower than v11's proven values (0.72/0.62, 0.88/0.78, 0.86/0.78). Over-folding is the #1 issue.
19. **v15 shove_buffer cap 0.14 vs v11's 0.11** makes v15 more conservative in all-in spots.
20. **River exact equity override (0 sims when 5 public cards) is a proven v11 feature.** Better river call/fold/bluff decisions. v15 lacks this entirely.
21. **River overbet bluff (choose_overbet_bluff_river) is a net-positive v11 feature.** Uses blocker analysis for bluff overbets on dry rivers. v15 lacks this.
22. **classify_opponent_style (nit/maniac/station/fold-heavy) provides +2-3 pts of adaptation.** v15 lacks opponent style classification entirely.
23. **allow_low_frequency_blocker_bluff should accept bluff_freq_bonus param** — enables more bluffing vs exploitable opponents. v15 regressed by dropping this.
24. **Big pot call margin (pot>5000 guard) prevents over-folding in large pots.** v15 lacks this inline check.
25. **v15's river overbet expansion to strong-tier is promising** (1.3-1.7x pot) but unproven. Keep it but ensure it doesn't conflict with nut overbet.
26. **File size: strategy.py at 996 lines is near 1000-line limit.** Any additions must be paired with removals or move to existing files.
