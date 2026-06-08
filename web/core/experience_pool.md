# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Opponent tracking data must be wired into decision logic, not just collected. v15 added per-street profiling (flop/turn/river aggr, barrel_freq) with magnitudes 4-10x larger (0.06, 0.08, clamp [-0.12, 0.15]). Awaiting eval for impact. [POSSIBLY EXHAUSTED]
- CBet fold-more exploitation has max effect ~0.015 — too small to matter alone. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- should_fold_postflop() rewritten in v15 to pot-odds formula (pot_odds + street_overlay + size_scale + aggression_adjustment) replacing 6 hardcoded thresholds. Awaiting eval. [POSSIBLY EXHAUSTED]
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems. Avoid overlapping fold gates.
- repeated_raise_trap: 3-tier fold/call/raise logic (v14) still leaks vs v4 (51.2%). must_continue_vs_raise() needs texture-aware pot_odds scaling. All fold/raise guards must verify branch consistency within the same decision block.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. Size up only when opponent fold data supports it. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; safe step 0.49→0.45. Sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense.

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants. Violations waste entire generations.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking. Crossover formula caps are safer than full table replacement when source bot is weaker.
- sanitize_action(): action=0 (call) must be allowed when facing all-in, preventing forced folds on callable all-ins.
- Crossover strategy attempted 5+ consecutive generations (v8→v14) with diminishing returns. v6 fold-discipline injection also exhausted. Avoid both unless novel structural angle identified. [POSSIBLY EXHAUSTED]
- Trust early negative critic signals — first-rejection scores are often more strategically accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.

## RECENT_LESSONS
- **v15**: Per-street opponent tracking (flop/turn/river aggr, barrel_freq, avg raise BB) wired into opponent_pressure_adjustment() and realized_postflop_equity(). Magnitudes 4-10x prior (0.06–0.08, clamp [-0.12, 0.15]). Pot-odds should_fold_postflop() rewrite replaces hardcoded thresholds. No eval data yet.
- **v15 gap**: Preflop gap handlers (bb_vs_raise, sb_vs_reraise) from v11 still missing — high-value recovery target. should_fold_postflop() has no opponent_model parameter.
- **v15 H2H**: Weakest matchups: v4 (50.7–52.0%, tight/aggressive), v5 (53.3%), v14 (54.5%). v4 has highest overall win rate (59.3%) — per-street reads targeting it may yield most gain.
- **v14 context**: v14 (1726.9) failed to beat v13 (1735.0). repeated_raise_trap 3-tier fix + texture-aware must_continue_vs_raise() still insufficient vs v4. Missing strong_made_continue guard may leak chips vs river barrels.
- **v13**: v11×v6 fold-discipline crossover failed twice (v12→r=1667, v13→unevaluated). Preflop gap handlers lost and not recovered.
