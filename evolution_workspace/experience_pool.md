# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

### Core Lessons (Consolidated v8-v17, v6→v7)

1. **Chen preflop table essential — worth ~130pts alone.** 169-hand lookup from bot5 replaces crude formula.
2. **Anti-bot4 detection + adjustments = proven edge.** detect_bot4_profile + get_anti_bot4_adjustments from bot5. Exploits bot4's overfolding on dynamic/paired boards, river checks.
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
13. **CARD_RANKS/CARD_SUITS precomputed arrays** in constants.py for perf.
14. **Wholesale copy fails** (v16=1349). Over-engineering fails (v17=1450). Incremental targeted port wins.
15. **Fix ALL parameter issues simultaneously.** Effects compound. One-at-a-time fails.
16. **bb_vs_raise/sb_vs_reraise fixed thresholds ALWAYS harmful** (v8,v11,v15). Let simulation decide.
17. **When changing preflop eval, recalibrate ALL downstream thresholds.** Chen vs formula mismatch caused v13 regression.

### v6→v7 Plan (3-Worker Targeted Port from bot5)
- **Source**: claude_v6 (r=1408, worst claude bot, ~112pts behind v16=1530 best)
- **W1 (Infrastructure)**: constants.py + card_utils.py + state.py + opponent.py — Chen table, CARD_RANKS/SUITS, fix sim counts, fix priors, remove CBet dead weight, add anti-bot4 detection.
- **W2 (Strategy Logic)**: postflop.py + strategy.py — river overbet, anti-bot4 integration, fix EQR/thin_cap/blocker_bluff, remove gift_balance/gto/cbet dead weight, remove fixed preflop thresholds, update choose_raise params.
- **W3 (Hyperparams)**: tournament.py — anti-lock tuning (chase=0.90, threshold=-0.075, sizing=0.18, bluff=0.13), threshold_delta symmetry (0.055/0.055).
- **Risk**: v6 has 6+ compounding structural gaps vs bot5. Each costs ~20-40 pts. Must fix ALL simultaneously per lesson 15.
