# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling (flop/turn/river aggr, barrel_freq) is infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]
- CBet fold-more exploitation max effect ~0.015; betsize exploit ±0.04 too small alone. [POSSIBLY EXHAUSTED]
- Light 4-bet and check-raise trap use opponent PFR + aggression reads — structural features, not threshold micro-adjustments.
- Weakest matchups are passive bots (v4/v5/v6/v8). Exploitative adjustments on turn/river must be priority — general improvements don't move these matchups. v19 added passive_opponent_exploit_bonus (capped 0.08, confidence-gated) and sb_limp_vs_raise handler — correct pivot direction.
- Per-street big-bet tracking (≥6/8/10 BB) with smooth_rate priors is useful infrastructure — keep as data input, not fold gate.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the single fold gate. Any fold/call override placed BEFORE it bypasses all guards and produces dead parameters — SPR commitment (v18), sizing_fold (v18), equity manipulation (v18). All fold gates must live INSIDE should_fold_postflop, no exceptions.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems. Avoid overlapping fold gates.
- repeated_raise_trap: 3-tier fold/call/raise logic leaks ~51% vs v4. All fold/raise guards must verify branch consistency within same decision block.
- Check-raise trap on dry flops for strong/nut hands returns 0 (check) — needs safety threshold on opponent confidence before trapping.
- Removing a double-dip (opponent model applied twice) is architecturally correct but neutral for performance — implicit compensation may be lost.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. Size up only when opponent fold data supports it. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense.
- Fold margin / clamp value tuning repeatedly attempted with no measurable gain. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in.
- Crossover strategy (v8→v14) and v6 fold-discipline injection both exhausted. [POSSIBLY EXHAUSTED]
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.
- H2H weakness data unreliable with small samples (10-20 games). Targeted changes need targeted evidence, not assumed weaknesses.

## RECENT_LESSONS
- **v19**: Critic evidence: H2H weaknesses: v18 vs v16: 45% WR (20 games) — only below-50% matchup; v18 vs v4/v8: 50% (20 games each) — previously flagged as passive bot weaknesses per experience pool; Experience pool refs: 'Weakest matchups are passive bots (v4/v5/v6/v8). Exploitative adjustments on turn/river must be priority.' — directly addressed by passive_opponent_exploit_bonus, 'Per-street profiling is infrastructure — tune coefficients, don't rebuild' [POSSIBLY EXHAUSTED] — this change uses existing model signals, not rebuilding, 'Fold margin / clamp value tuning repeatedly attempted with no measurable gain' [POSSIBLY EXHAUSTED] — v19 avoids this pattern entirely; Diff refs: opponent.py:216-220 — sb_limp_vs_raise classification based on checking SB's first preflop action type (call=limp vs raise=reraise), strategy.py:46-63 — passive_opponent_exploit_bonus() with 3-factor confidence-gated bonus capped at 0.08, strategy.py:571-586 — sb_limp_vs_raise handler: raise at >=0.60, call at >=0.28 or pot-odds, fold otherwise
- **v19**: Added passive_opponent_exploit_bonus() (confidence-gated, capped 0.08) and sb_limp_vs_raise handler (raises ≥0.60, calls ≥0.28). Correctly pivots to passive exploitation per pool guidance. H2H baseline: v18 vs v4 30% WR (10 games), vs v5/v8 40%, vs v11 35% — small samples, verify after eval.
- **v18**: Clamp narrowing is fold-margin tuning (exhausted pattern). SPR/sizing_fold gates placed before should_fold_postflop created dead parameters (same bypass flagged in pool). Double-dip barrel penalty removal was correct cleanup but neutral — implicit compensation lost.
- **v17**: Light 4-bet + check-raise trap added. 65.8% WR (190 games, small sample). Neither feature targets passive exploitation — weakest matchups unchanged. Preflop gap handlers from v11 recovered via crossover.

