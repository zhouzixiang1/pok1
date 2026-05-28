# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (v1–v8 consolidated)

1. **Chen preflop table: v4 (#1 at 1623) succeeds WITHOUT it using formula.** Table may introduce calibration noise. Formula is sufficient.
2. **Anti-bot4 detection with LOOSE criteria is catastrophic.** v14 collapsed 100pts (1608→1509). abs(vpip-0.55)<0.15 matches ~80% of pool, causing excessive bluff/fold deltas vs all opponents. Only port with VERY tight thresholds (±0.08).
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** = proven value extraction. v4 lacks it. Must add.
4. **Simulation counts: 700 flop sims is fine.** v2 rated #1 with {0:500}. More sims ≠ better play if thresholds are wrong.
5. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.** v4 uses these. Proven correct.
6. **EQR calibration: v4 air 0.72/0.62 is optimal.** v14 lowered to 0.68/0.56 + extra penalties (OOP double-barrel -0.05, big_pot -0.03, lower floor 0.40) causing over-folding. DO NOT over-discount.
7. **Blocker bluff: random.random() or deterministic hash both work.** Not a major differentiator.
8. **exploit_lambda (gift_balance blending) is HARMFUL.** v14 has it, v4 (#1) doesn't. It corrupts GTO base thresholds with noisy gift_balance signal. DO NOT port.
9. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** Proven values.
10. **bb_vs_raise/sb_vs_raise: Let simulation decide.** v14's hardcoded 3bet bluff spots with token hashing are net negative. v4 delegates to simulation. Correct approach.
11. **When changing preflop eval, recalibrate ALL downstream thresholds.**
12. **Wholesale copy fails. Incremental targeted port wins.**
13. **Fix ALL parameter issues simultaneously.** Effects compound.
14. **thin_cap: 0.30 (round<=2) / 0.38 (round==3).** v4's flat values. v14's wetness formula (0.46+0.08w) lets through 0.46 ratio thin bets on dry boards — regression.

### v9–v14 Era: Field Compression & v14 Collapse

15. **Field compressed to 1590–1623 range (~30pts).** Small edges matter enormously. Only port proven features.
16. **v14 = v4 base + anti-bot4 + exploit_lambda + lower EQR + wet thin_cap + preflop 3bet bluff + CBet tracking + drift detection + river overbet.** Of these, ONLY CBet tracking and river overbet are net positive. Everything else hurt.
17. **CBet tracking IS useful** (lesson #29 from v14 era). v4 lacks it. Port with v4's priors.
18. **Drift detection (12-hand window) is neutral-to-slightly-positive.** Doesn't hurt but hasn't proven decisive. Safe to port.
19. **v4 is the cleanest, highest-rated base at 1623.** Always branch from the #1 bot when current version is catastrophically worse.
20. **OOP draw EQR 0.85/0.75 + extra penalties is over-conservative.** v4's draw EQR is simpler (no dedicated OOP draw path) and works better.
21. **River strong dry overbet (1.3-1.5x pot) is promising but unproven at scale.** v14 had it but its overall collapse makes it hard to evaluate. Port cautiously.
22. **Catastrophic failures come from systemic corruption of base thresholds, not individual feature bugs.** v14's anti-bot4 + exploit_lambda BOTH modify strong/medium/bluff thresholds, compounding errors.

### v15 Strategy: Branch from v7, Port CBet Tracking

23. **v7 = v4 base + anti-bot4 + river overbet + check_probe_resistance + must_continue_vs_raise.** v7 is current #1 at 1615. The anti-bot4 detection works in v7 because it LACKS exploit_lambda, lower EQR, and preflop bluff spots that compound errors.
24. **CBet tracking is v14's only proven positive feature that v7 lacks.** Adjusts call margin ±0.025 based on opponent cbet_rate vs baseline 0.55/0.35. Safe to port.
25. **v7's preflop delegation (no bb_vs_raise/sb_vs_reraise spots) is CORRECT.** Lesson #10 reinforced: simulation decides preflop facing raises. v14's hardcoded token-hash bluff spots are net negative.
26. **Anti-bot4 VPIP center matters:** v7 uses 0.58 (matches priors from lesson #5), v14 used 0.55 (3pts off priors). Align detection with priors.
27. **Field is ~30pts compressed (1590-1623).** Small edges matter enormously. Port 1 feature at a time, verify each incrementally.
