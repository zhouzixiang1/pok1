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
18. **realized_postflop_equity: bot5 removes big_pot param entirely, no draw OOP extra branch.** Pair EQR: 0.86/0.78. Top pair weak kicker EQR: 0.92/0.86. No double_barrel OOP extra subtractions.
19. **postflop_call_margin: bot5 removes cbet_rate adjustments entirely.** v6 has cbet_rate>0.65 / cbet_rate<0.40 logic — dead weight.
20. **check_probe_resistance_margin and must_continue_vs_raise belong in postflop.py**, not strategy.py. bot5 defines them there.

### v6→v7 Execution Plan

v6 is the WORST bot (1408.1 Glicko, stuck across 10+ periods), -149pts below top bot v5 (1557.4). Every gap was verified by reading both v6 and bot5 source code line-by-line. The strategy is simultaneous incremental targeted port of ALL verified bot5 improvements across all 6 files.

**Worker 1 (Foundation + Opponent Model):** constants.py + state.py + opponent.py + postflop.py — Chen table, precomputed arrays, sim counts, opponent cleanup (remove cbet/drift dead weight, fix priors), anti-bot4 detection, blocker bluff fix, move check_probe/must_continue to postflop.py.

**Worker 2 (Strategy + Tournament):** strategy.py + tournament.py — Remove dead weight (gift_balance, exploit_lambda, gto blending, cbet_rate adjustments, fixed preflop bb_vs_raise/sb_vs_raise). Add overbet functions. Integrate anti-bot4 throughout. Fix realized_postflop_equity (remove big_pot, remove draw OOP branch, fix EQR). Fix choose_raise (remove to_call==0 guard, add params, fix thin_cap). Fix tournament anti-lock params + threshold_delta symmetry.

Both workers must complete all changes. Partial fixes compound negatively (lesson #15).
