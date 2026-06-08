# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Opponent tracking data must be wired into decision logic, not just collected. Scale adjustments (e.g., 0.06 * clamp(...)) outperform flat per-street tweaks. CBet fold-more exploitation has max effect ~0.015 — too small to matter alone. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- Fold discipline is critical — calling-station behavior (0% postflop fold rate) is fatal. should_fold_postflop() is on an exhausted trajectory: v12 tried it (dropped 1740→1711), v13 retried with raw thresholds making folds even more aggressive. Pot-odds calibration via proportional scaling is the next step, not more hardcoded thresholds. [POSSIBLY EXHAUSTED]
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
- **v13**: Built from v11 base but removed v12's bb_vs_raise/sb_vs_reraise preflop handlers — loses the only structural fix for confirmed H2H gaps (vs v2: 42.5%, vs v6: 43.8%). Re-implemented should_fold_postflop() with raw thresholds (no conservatism offset, no texture bonuses) making folds MORE aggressive than v12 which already failed. Two critics flagged these regressions.
- **v12**: Preflop gap fill (bb_vs_raise: value 3bet ≥0.60, bluff 3bet 0.38-0.54, call ≥0.37; sb_vs_reraise: 4bet ≥0.78, call ≥0.55) was high-value structural work — v11 returned None for these spots. Crossover v11×v6 dropped 1740→1711 but only 10 games/matchup — regression unconfirmed.
- **v11**: v4 has no H2H matchup below 50% — no confirmed weakness to exploit. v10/v8/v9 are proven losers; crossover from them is risky. Key H2H gaps to validate at 100+ games: vs v6 ~40% WR (fold discipline), vs v2 ~43% (weak preflop defense), vs v5 ~30% (fold discipline).
