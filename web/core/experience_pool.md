# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- **v6-v7**: Opponent tracking data (fold_to_cbet, aggression_by_street) must be WIRED INTO decision logic in strategy.py/postflop.py, not just collected. Data collection without consumption is wasted LOC.
- **v8**: Per-street fold-to-bet tracking is structurally useful. Wire into exploitative adjustments: if opp folds to cbets >60% on a street, increase cbet frequency on that street.

## POSTFLOP_STRATEGY
- **v6**: Postflop fold gates + EQR tightening produced rating improvement (v5→v7: 1700→1831). This direction works.
- **v7-v8**: Draw call margins (gutshot, flush draw) should be grounded in equity math: gutshot ~17% OTF, ~9% OTR. Margins should relate to actual equity vs pot odds, not arbitrary thresholds.

## PARAMETER_TUNING
- **v6-v8**: Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) are well-tuned. Blind increases (+0.10/+0.15/+0.20) without opponent-data justification are risky — size up only when opponent fold data supports it.
- **v8**: SB open threshold 0.49 is calibrated. Changing it to 0.38 requires EV simulation evidence for OOP postflop play. Safer step: 0.49→0.45.
- **v8**: Preflop 3bet threshold 0.60 (TT+, AKs) is solid. Pot-odds check needed for all-in decisions: never call off 100BB with 51% hand vs over-shove.

## GENERAL
- **v6-v8**: Worker role boundaries are CRITICAL. Tuner must change at least one constant (zero changes = failure). Architect must NOT touch constants. Violations waste entire generation cycles.
- **v8**: Crossover bots need full pipeline (quality gates → review → critic → commit → archivist) to get git tags and archive snapshots. Skipping pipeline = no version tracking.

## RECENT_LESSONS
- **v9**: Critic evidence: H2H weaknesses: v8 loses to ALL opponents: 43.2% vs v6 (worst), 46.0% vs v4, 46.2% vs v2, 44.8% vs v3, 47.0% vs v5, 49.5% vs v7, v8 overall win rate 46.9% — worst in pool of 7 bots, Experience pool: 'v8: Calling station: 0% postflop fold rate. Loses to ALL opponents'; Experience pool refs: v9 combined lesson: 'Tier-based fold system correctly targets marginal pair folding but needs draw-aware thresholds' — this change adds pair_profile-aware folding that respects has_draw guards, v6-v7 lesson: 'Postflop fold gates + EQR tightening produced rating improvement (v5→v7: 1700→1831). This direction works.' — reinstating fold discipline in v8 follows proven direction, v7-v8 lesson: 'Margins should relate to actual equity vs pot odds, not arbitrary thresholds' — the constant tweaks (0.20→0.18, 0.25→0.22) lack this grounding; Diff refs: main.py line 21: sanitize_action() now allows action=0 (call) when facing all-in, fixing a bug that forced folds on all-in calls, strategy.py lines 590-606: New pair_profile-aware marginal pair folding — bottom_pair/underpair/board_pair fold on turn/river vs medium/large bets or 2+ barrels; thin value fold on river; garbage fold <0.15, strategy.py line 542: BB call_threshold 0.42→0.37 (wider defense)
- **v9 (combined)**: v6 (parent) strongest at 52-58% WR depending on opponent coverage; v8 crossover is weakest at 46-47% WR. v6 loses to v2/v3 (aggressive opponents exploit postflop passivity). Tier-based fold system (v9 iterations) correctly targets marginal pair folding but needs draw-aware thresholds. Postflop_call_margin reversal (positive→negative) is the right direction for weak hands but magnitude must be calibrated vs pot odds.
- **v7 (improvement)**: Postflop fold gates + EQR tightening from v5→v7 improved rating to 1831 (best). Beat v4 54% H2H. Continue structural postflop improvements.
- **v8**: Crossover v2×v6 with 3360+ games. Loses to ALL opponents (worst: vs v6 at 43.2%). Calling station: 0% postflop fold rate. Not a viable evolution source — use v6 or v4 instead.

