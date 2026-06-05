# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- **v6-v7**: Opponent tracking data (fold_to_cbet, aggression_by_street) must be WIRED INTO decision logic in strategy.py/postflop.py, not just collected. Data collection without consumption is wasted LOC.
- **v8**: Per-street fold-to-bet tracking is structurally useful. Wire into exploitative adjustments: if opp folds to cbets >60% on a street, increase cbet frequency on that street.

## POSTFLOP_STRATEGY
- **v6**: Postflop fold gates + EQR tightening produced rating improvement (v5→v7: 1700→1831). This direction works.
- **v7-v8**: Draw call margins (gutshot, flush draw) should be grounded in equity math: gutshot ~17% OTF, ~9% OTR. Margins should relate to actual equity vs pot odds, not arbitrary thresholds.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- **v6-v8**: Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) are well-tuned. Blind increases (+0.10/+0.15/+0.20) without opponent-data justification are risky — size up only when opponent fold data supports it.
- **v8**: SB open threshold 0.49 is calibrated. Changing it to 0.38 requires EV simulation evidence for OOP postflop play. Safer step: 0.49→0.45.
- **v8**: Preflop 3bet threshold 0.60 (TT+, AKs) is solid. Pot-odds check needed for all-in decisions: never call off 100BB with 51% hand vs over-shove.

## GENERAL
- **v6-v8**: Worker role boundaries are CRITICAL. Tuner must change at least one constant (zero changes = failure). Architect must NOT touch constants. Violations waste entire generation cycles.
- **v8**: Crossover bots need full pipeline (quality gates → review → critic → commit → archivist) to get git tags and archive snapshots. Skipping pipeline = no version tracking.

## RECENT_LESSONS
- **v7 (improvement)**: Postflop fold gates + EQR tightening from v5→v7 improved rating to 1831 (best). Beat v4 54% H2H. Continue structural postflop improvements.
- **v8 (under-evaluated)**: Crossover v2×v6. Rating ~1802 but only 150 games (RD=74.6). Strong vs v5 (80%), weak vs v2 (30%). Needs 500+ games for reliable assessment. Best matchup is exploiting v5's postflop leaks.
