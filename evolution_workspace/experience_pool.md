# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated v8-v17)

1. **Chen preflop table essential — worth ~130pts alone.** 169-hand lookup from bot5 replaces crude formula.
2. **Anti-bot4 detection + adjustments = proven edge.** detect_bot4_profile + get_anti_bot4_adjustments from bot5.
3. **River overbet (1.5-2.2x pot) for nut hands on dry rivers** is proven value extraction (bot5 choose_overbet_river).
4. **choose_raise needs anti_bot4_bonus + allow_river_overbet params.** max_ratio=2.2 on river with nuts. thin_cap=0.30(round<=2)/0.38(round>=3), NO to_call==0 guard.
5. **Simulation counts must match bot5: {0:900, 3:1200, 4:1500}** extras {0:300, 3:350, 4:300}. v6 had {0:400,3:800,4:900} — massive accuracy loss.
6. **Priors: vpip=0.58, pfr=0.28, confidence divisor=35.** v6 had wrong vpip=0.52, pfr=0.24, divisor=30.
7. **EQR air: 0.72 IP / 0.62 OOP, lower bound 0.45.** v6 had 0.68/0.56, lb=0.40. No big_pot subtract in draw OOP branch.
8. **allow_low_frequency_blocker_bluff: use random.random() + bluff_freq_bonus param**, not deterministic hash.
9. **CBet/drift detection/cbet_rate usage = dead weight.** bot5 removed them. Remove from opponent model and strategy.
10. **gift_balance / exploit_lambda / gto_strong blending = dead weight.** bot5 doesn't have them. Simplify threshold logic.
11. **Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13.** v6 had 0.85/-0.070/0.16/0.11.
12. **threshold_delta: 0.055*protect - 0.055*chase** (symmetric, not v6's 0.050/0.060).
13. **CARD_RANKS/CARD_SUITS/PREFLOP_STRENGTH_TABLE precomputed** in constants.py for perf + accuracy.
14. **Wholesale copy fails** (v16=1349). Over-engineering fails (v17=1450). Incremental targeted port wins.
15. **Fix ALL parameter issues simultaneously.** Effects compound. One-at-a-time fails.
16. **bb_vs_raise/sb_vs_raise fixed thresholds ALWAYS harmful** (v8,v11,v15). Let simulation decide.
17. **When changing preflop eval, recalibrate ALL downstream thresholds.** Chen vs formula mismatch caused v13 regression.
18. **realized_postflop_equity: bot5 removes big_pot from draw OOP, uses simpler EQR for pairs.** Pair EQR: 0.86/0.78 (bot5) vs 0.84/0.73 (v6). No double_barrel OOP extra -0.05.
19. **postflop_call_margin: bot5 removes cbet_rate adjustments entirely.** v6 has cbet_rate>0.65 / cbet_rate<0.40 logic — dead weight.

### v6→v7 Confirmed Gaps (Code-Level Diff)

**constants.py**: Missing PREFLOP_STRENGTH_TABLE (169-entry Chen lookup), CARD_RANKS, CARD_SUITS arrays. Sim counts wrong: {0:400,3:800,4:900} vs bot5 {0:900,3:1200,4:1500}. Extra sims wrong: {0:200,3:280,4:180} vs bot5 {0:300,3:350,4:300}.

**card_utils.py**: Uses inline `card % 4` / `card // 4 + 2` instead of precomputed CARD_RANKS/CARD_SUITS arrays.

**state.py**: Crude formula-based estimate_preflop_strength() vs bot5's PREFLOP_STRENGTH_TABLE lookup.

**opponent.py**: (a) Dead weight: cbet_rate, fold_to_cbet, drift_detected, hand-level VPIP/PFR tracking. (b) Wrong priors: vpip=0.52, pfr=0.24, divisor=30. (c) Missing: detect_bot4_profile(), get_anti_bot4_adjustments(). (d) Extra complexity: hand_vpip_flags, hand_pfr_flags, hand_postflop_aggr_counts lists.

**postflop.py**: (a) allow_low_frequency_blocker_bluff uses deterministic hash, missing bluff_freq_bonus param. (b) Missing: check_probe_resistance_margin(), must_continue_vs_raise().

**strategy.py**: (a) Dead weight: track_opponent_gift(), safe_exploitation_lambda(), gift_balance/exploit_lambda/gto blending, cbet_rate adjustments in postflop_call_margin. (b) Missing: choose_overbet_river(), choose_overbet_bluff_river(), anti-bot4 integration. (c) choose_raise: thin_cap has wrong formula (0.46+0.08*wetness vs 0.30/0.38), missing anti_bot4_bonus and allow_river_overbet params, max_ratio capped at 1.45 (should be 2.2 for river overbet). (d) realized_postflop_equity: wrong EQR values, extra big_pot param, extra double_barrel OOP subtraction.

**tournament.py**: Anti-lock params wrong: chase=0.85/-0.070/0.16/0.11 vs bot5 0.90/-0.075/0.18/0.13. threshold_delta asymmetric 0.050/0.060 vs symmetric 0.055/0.055.

### Priority Order for v7
1. constants.py + card_utils.py + state.py (Chen table + precomputed arrays + sim counts) — FOUNDATIONAL
2. opponent.py (remove dead weight, fix priors, add anti-bot4) — OPPONENT MODEL
3. postflop.py (fix blocker bluff randomness, add bluff_freq_bonus param) — EVALUATION
4. strategy.py (remove dead weight, add overbet, fix choose_raise, fix EQR, integrate anti-bot4) — STRATEGY
5. tournament.py (fix anti-lock params, symmetric threshold_delta) — HYPERPARAMS

### v7 Analysis (Master Architect)
- v6 is the WORST bot in the population (1408.1 Glicko), 162 points below top bot v11 (1569.9).
- Every gap documented above was verified by reading both v6 and bot5 source code.
- v6's preflop evaluation uses a crude formula instead of the 169-hand Chen lookup table (proven ~130pts gain).
- Simulation counts are drastically too low (400/800/900 vs 900/1200/1500), causing massive accuracy loss.
- Opponent model has wrong priors (vpip=0.52/0.24 vs 0.58/0.28) and dead weight inflating computation.
- Missing anti-bot4 detection means no exploitation of the most common opponent profile.
- choose_raise thin_cap is formula-based (0.46+0.08*wetness) instead of fixed (0.30/0.38).
- No river overbet capability means leaving value on the table with nut hands.
- EQR air values are too low (0.68/0.56 vs 0.72/0.62) and big_pot parameter adds noise.
- Anti-lock params are too conservative (0.85 vs 0.90 chase, etc.).
- **Strategy**: Fix ALL gaps simultaneously across all files. Each gap alone loses ~20-40 points; together they compound to -160.
