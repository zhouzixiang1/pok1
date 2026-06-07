# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- **v6-v8**: Opponent tracking data (fold_to_cbet, aggression_by_street) must be WIRED INTO decision logic, not just collected. If opp folds to cbets >60% on a street, increase cbet frequency there.

## POSTFLOP_STRATEGY
- **v5→v7**: Postflop fold gates + EQR tightening improved rating 1700→1831 (best). This structural direction works.
- **v7-v8**: Draw call margins (gutshot ~17% OTF, ~9% OTR) must be grounded in equity math vs pot odds, not arbitrary thresholds.
- **v9**: Tier-based fold system correctly targets marginal pair folding but needs draw-aware thresholds (has_draw guards). Postflop_call_margin reversal (positive→negative) is right for weak hands but magnitude needs pot-odds calibration.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- **v6-v8**: Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) are well-tuned. Size up only when opponent fold data supports it.
- **v8**: SB open threshold 0.49 is calibrated. Safer step: 0.49→0.45, not 0.38.
- **v8**: Preflop 3bet threshold 0.60 (TT+, AKs) is solid. Pot-odds check required for all-in calls — never call off 100BB with 51% hand vs over-shove.

## GENERAL
- **v6-v8**: Worker role boundaries are CRITICAL. Tuner must change ≥1 constant (zero changes = failure). Architect must NOT touch constants. Violations waste entire generations.
- **v8**: Crossover bots need full pipeline (gates → review → critic → commit → archivist) for git tags and version tracking.

## RECENT_LESSONS
- **v9**: v6 (parent) strongest at 52–58% WR; v8 crossover weakest at 46–47% WR, loses to ALL opponents. v6 loses to v2/v3 (aggressive opponents exploit postflop passivity). Use v6 or v4 as evolution source, not v8.
- **v9**: sanitize_action() bug fix: action=0 (call) now allowed when facing all-in, preventing forced folds on callable all-ins.
- **v9**: BB call_threshold widened 0.42→0.37. Monitor for over-defense in future generations.
- **v9**: Calling-station behavior (0% postflop fold rate) is fatal. Fold discipline must be preserved across all mutations.
