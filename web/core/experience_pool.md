# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Opponent tracking data must be wired into decision logic, not just collected. Scale adjustments outperform flat per-street tweaks. CBet fold-more exploitation has max effect ~0.015 — too small to matter alone. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- should_fold_postflop() is on an exhausted trajectory: v12 tried it (1740→1711), v13 retried with raw thresholds — still regressed. Pot-odds calibration via proportional scaling is the next step, not more hardcoded thresholds. [POSSIBLY EXHAUSTED]
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems. Avoid overlapping fold gates.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) are well-tuned. Size up only when opponent fold data supports it. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; safe step 0.49→0.45. Sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense.

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants. Violations waste entire generations.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking. Crossover formula caps are safer than full table replacement when source bot is weaker.
- sanitize_action(): action=0 (call) must be allowed when facing all-in, preventing forced folds on callable all-ins.

## RECENT_LESSONS
- **v14**: H2H solid — no matchup below 50% (weakest: v11 50%, v2/v4/v8 53.3%). Structural changes: repeated_raise_trap split into fold/call/raise tiers (garbage folds instead of calling); must_continue_vs_raise() refactored with texture-aware pot_odds scaling (strong tier 0.36→0.45 on safe boards, new thin value tier at made≥0.50). Avoids exhausted should_fold_postflop path via proportional scaling.
- **v12→v13**: v11×v6 fold-discipline crossover failed twice (v12→r=1667, v13→unevaluated). Preflop gap handlers (bb_vs_raise, sb_vs_reraise) were high-value structural fixes but lost in v13 and not recovered. Direction exhausted. [POSSIBLY EXHAUSTED]
- **v12→v13**: Trust early negative critic signals — first-rejection (score 4) was more strategically accurate than second approval. It identified v12 already failed with same approach and preflop cap removal risks over-inflating AK/AQ.
