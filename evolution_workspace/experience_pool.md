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

### v6→v7 Execution Plan (3-Worker Split)

v6 is the WORST bot (1408 Glicko, rating NEVER CHANGED across 110 periods — not playing matches). -126pts below top bots. All gaps verified by line-by-line comparison with bot5 source code.

21. **v6 rating literally frozen at 1408.06 for ALL 110 history periods.** The daemon may be skipping it (lowest-rated gets fewest matches?). Regardless, the code gaps are clear.
22. **bot5 choose_preflop_spot_action omits bb_vs_raise and sb_vs_reraise branches entirely** — returns None for these spots, letting simulation-driven logic handle them. v6 has fixed-threshold branches that are ALWAYS harmful (lesson #16).
23. **bot5 removes preflop_trash_hand calculation from get_action** — only uses it in choose_preflop_spot_action. v6 computes it separately in get_action (dead weight, minor perf).

**Worker 1 (Preflop + Opponent Model):** state.py + opponent.py — Rewrite estimate_preflop_strength to use PREFLOP_STRENGTH_TABLE (Chen lookup), fix priors (vpip=0.58, pfr=0.28, div=35), add detect_bot4_profile + get_anti_bot4_adjustments, remove dead weight (CBet tracking, drift detection, fold_to_cbet, hand_vpip_flags/pfr_flags/aggr tracking arrays).

**Worker 2 (Strategy + Postflop):** strategy.py + postflop.py — Fix allow_low_frequency_blocker_bluff (random.random+bluff_freq_bonus), add choose_overbet_river + choose_overbet_bluff_river, fix choose_raise (add anti_bot4_bonus + allow_river_overbet params, thin_cap 0.30/0.38, max_ratio 2.2), fix realized_postflop_equity (remove pot param, EQR 0.72/0.62 air 0.86/0.78 pair lb 0.45/0.65), remove dead weight (gift_balance/track_opponent_gift, safe_exploitation_lambda, gto_strong blending, cbet_rate adjustments in postflop_call_margin, bb_vs_raise/sb_vs_reraise fixed thresholds in choose_preflop_spot_action), move check_probe_resistance_margin + must_continue_vs_raise to postflop.py, wire anti-bot4 throughout get_action (detect_bot4_profile, get_anti_bot4_adjustments, pass anti_bot4_bonus to choose_raise, bluff_freq_bonus to blocker bluffs, thin_static_showdown_control anti-bot4 bypass, river overbet logic).

**Worker 3 (Constants + Tournament Params):** constants.py + tournament.py — Add PREFLOP_STRENGTH_TABLE, CARD_RANKS, CARD_SUITS to constants.py. Fix sim counts ({0:900,3:1200,4:1500}, extras {0:300,3:350,4:300}). Anti-lock: chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13. threshold_delta symmetry: 0.055/0.055.

All 3 workers must complete. Partial fixes compound negatively (lesson #15).
