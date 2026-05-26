# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated from v8–v17)

1. **bb_vs_raise/sb_vs_reraise fixed thresholds ALWAYS harmful** (v8,v11,v15). bot5 returns None, letting simulation decide. PROVEN.
2. **thin_cap = 0.30 (round≤2) / 0.38 (round≥3)**, NO `to_call==0` guard. The 0.46+0.08w formula persisted 8+ gens.
3. **River overbet (1.5–2.2x pot) for nut hands on dry rivers** is proven edge (bot5 `choose_overbet_river`).
4. **When changing preflop eval, recalibrate ALL downstream thresholds.** Chen vs formula scale mismatch caused v13 regression.
5. **Fix ALL parameter issues simultaneously.** Effects compound. v13→v14 failed fixing 1 of 4 bugs at a time.
6. **Complex opponent profiling fails in 50-hand matches.** Focus on additive features.
7. **CBet/drift detection adds complexity without rating benefit.** bot5 doesn't have them.
8. **Anti-bot4 detection + adjustments are proven value** (bot5 detect_bot4_profile, get_anti_bot4_adjustments). Bypass conservative checks when bot4 detected.
9. **Wholesale copy fails** (v16=1349). Over-engineering fails (v17=1450, 7753 lines). Incremental port wins.
10. **allow_low_frequency_blocker_bluff needs bluff_freq_bonus param** for anti-bot4 integration. Use random.random(), not deterministic hash.
11. **choose_raise needs anti_bot4_bonus + allow_river_overbet params.** Max_ratio 2.2 on river with nut hands extracts maximum value.
12. **EQR air values: 0.72 IP / 0.62 OOP** (bot5). Lower bounds 0.45 (v6 has 0.40). Under-realized bluff equity loses value.
13. **Opponent model priors: vpip=0.58, pfr=0.28** (bot5). v6 uses 0.52/0.24 — shifts entire range evaluation.
14. **Confidence divisor: 35** (bot5) vs 30 (v6). Faster trust in opponent model is better.
15. **gift_balance / exploit_lambda / cbet / drift are dead weight.** bot5 doesn't have them. Remove.
16. **Chen preflop table is essential.** Formula-based estimate_preflop_strength is inaccurate. Precomputed 169-hand table in constants.py.
17. **Simulation counts: {0:900, 3:1200, 4:1500}** with extras {0:300, 3:350, 4:300}. v6 runs too few sims.
18. **Dead EQR branches in v6**: draw_strength OOP bonus, big_pot adjustment, double_barrel OOP extra penalty. bot5 doesn't have these.
19. **Anti-lock thresholds: chase 0.90, threshold -0.075, sizing 0.18, bluff 0.13** (v6 has 0.85, -0.070, 0.16, 0.11).
20. **threshold_delta formula: 0.055*protect - 0.055*chase** (v6 has 0.050/0.060 asymmetry).
21. **CARD_RANKS/CARD_SUITS precomputed arrays** avoid redundant computation. Use from constants.

### v6→v7 Strategy
- **Source**: claude_v6 (r=1417, lowest claude bot, ~150 pts behind leaders)
- **Reference bot studied**: bot5 (anti-exploitation framework, highest-complexity reference)
- **Root cause analysis**: v6 lacks bot5's proven structural features (Chen table, anti-bot4, river overbet) AND has wrong parameter values across 5 files. Both logic and parameters need fixing simultaneously.
- **Key gaps confirmed by diff analysis**: Chen table, sim counts, opponent priors/confidence, EQR values/dead branches, anti-bot4 detection/integration, river overbet, dead weight (gift/cbet/drift/exploit_lambda), anti-lock thresholds, threshold_delta symmetry, blocker bluff random.random+bonus, thin_cap formula, choose_raise max_ratio, bb_vs_raise/sb_vs_reraise removal
- **3 workers, strict file ownership**:
  - Worker 1 (A): constants.py, state.py, card_utils.py, opponent.py — infrastructure + opponent model
  - Worker 2 (A): postflop.py, strategy.py — structural integration (anti-bot4, river overbet, EQR, dead weight removal, preflop simplification)
  - Worker 3 (B): tournament.py — anti-lock thresholds + threshold_delta symmetry
