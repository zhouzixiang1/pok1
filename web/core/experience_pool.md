# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Opponent tracking data (fold_to_cbet, aggression_by_street) must be wired into decision logic, not just collected. Scale adjustments (e.g., 0.06 * clamp(...)) outperform flat per-street tweaks. CBet fold-more exploitation has max effect ~0.015 — too small to matter alone. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- Fold discipline is critical — calling-station behavior (0% postflop fold rate) is fatal. Postflop fold gates + EQR tightening drove 1700→1831 (best). [POSSIBLY EXHAUSTED]
- Draw call margins (gutshot ~17% OTF, ~9% OTR) must be grounded in equity math vs pot odds, not arbitrary thresholds. Use has_draw guards in tier-based fold systems.
- Pot-odds calibration via proportional scaling (pot_odds_boost = max(0, pot_odds-0.25)*0.12) outperforms per-street thresholds. Avoid overlapping fold gates — always check existing guards before adding new ones.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) are well-tuned. Size up only when opponent fold data supports it. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; safe step 0.49→0.45, not 0.38. Non-pair cap 0.80 in PREFLOP_STRENGTH_TABLE has negligible sizing effect.
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense in future generations.

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant (zero changes = failure), Architect must NOT touch constants. Violations waste entire generations.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action() bug: action=0 (call) must be allowed when facing all-in, preventing forced folds on callable all-ins.

## RECENT_LESSONS
- **v11**: v4 has no H2H matchup below 50% WR — no confirmed weakness to exploit. v10/v8/v9 are proven losers; crossover from them is risky.
- **v11**: Sizing coefficient 1.8→2.2 for sb_open/bb_vs_limp has real impact on pair sizing (+5%). Non-pair cap changes are negligible.
- **v11**: Fold-more pattern repeated across multiple gens with diminishing returns. CBet exploitation via fold-more has max effect ~0.015. [POSSIBLY EXHAUSTED]
