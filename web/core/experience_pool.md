# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling (flop/turn/river aggr, barrel_freq) is infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]
- CBet fold-more exploitation max effect ~0.015; betsize exploit ±0.04 too small alone. [POSSIBLY EXHAUSTED]
- Light 4-bet and check-raise trap use opponent PFR + aggression reads — structural features, not threshold micro-adjustments.
- Weakest matchups are passive bots (v4/v5/v8). New features must specifically target passive exploitation; general improvements don't move these matchups.
- Per-street big-bet tracking (≥6/8/10 BB) with smooth_rate priors is useful infrastructure (v18) — keep as data input, not fold gate.

## POSTFLOP_STRATEGY
- should_fold_postflop() uses pot-odds + barrel modulation + board_texture + bet_size_bucket. Keep fold gates structurally separate from barrel tuning — v17 conflated them.
- Any fold/call override placed BEFORE should_fold_postflop bypasses all its guards and produces dead parameters. SPR commitment (v18) and sizing_fold (v18) both violated this — all fold gates must live INSIDE should_fold_postflop, no exceptions.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems. Avoid overlapping fold gates.
- repeated_raise_trap: 3-tier fold/call/raise logic leaks ~51% vs v4. All fold/raise guards must verify branch consistency within same decision block.
- Check-raise trap on dry flops for strong/nut hands returns 0 (check) — needs safety threshold on opponent confidence before trapping.

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
- Crossover strategy (v8→v14) and v6 fold-discipline injection both exhausted. Avoid unless novel structural angle. [POSSIBLY EXHAUSTED]
- Trust early negative critic signals — first-rejection scores are often more accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.

## RECENT_LESSONS
- **v18**: Critic evidence: H2H weaknesses: v15 has no matchups below 40%. Weakest: v15 vs v6 at 48.9% (90 games), v14 vs v15 at 47.8% (90 games). Experience pool says passive bots (v4/v5/v8) are historically weakest, but these changes target aggressive opponents instead.; Experience pool refs: PARAMETER_TUNING: 'Fold margin / clamp value tuning repeatedly attempted with no measurable gain. [POSSIBLY EXHAUSTED]' — clamp narrowing from [-0.12, 0.15] to [-0.09, 0.11] is exactly this pattern., POSTFLOP_STRATEGY: 'Any fold/call override placed BEFORE should_fold_postflop bypasses all its guards and produces dead parameters' — the double-dip removal addresses this by removing equity manipulation that preceded fold decisions., OPPONENT_MODELING: 'Per-street profiling is infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]' — the value-heavy opponent fold gate reuses existing profiling data without rebuilding.; Diff refs: Lines 293-318 REMOVED: 26-line block in air_hand path of realized_postflop_rate that reduced eqr based on barrel_freq, avg_river_raise_bb, river_aggr, and aligned_signal_boost. This was applied IN ADDITION to should_fold_postflop fold gates — double penalty., Lines 583-603 ADDED in should_fold_postflop: SPR commitment folds (SPR>4, late streets, weak hands), opponent-model value-heavy folds (barrel≥0.50 or post_aggr≥0.42), river multi-barrel fold (strength<0.20). Mostly redundant with existing gates at lines 567-581., Lines 951-953 ADDED: Trap detection fold for very weak hands (<0.25 made, <0.14 draw) in repeated_raise_trap context — technically outside should_fold_postflop but limited to one specific code path.
- **v18**: SPR and sizing_fold gates placed before should_fold_postflop created dead parameters — same bypass pattern flagged in pool. All fold/call gates must live INSIDE should_fold_postflop.
- **v18**: H2H weakness data unreliable (10-20 game samples per matchup). No evidence v4/v6/v8 losses stem from river over-folding. Targeted changes need targeted evidence, not assumed weaknesses.
- **v17**: Light 4-bet + check-raise trap added. 65.8% WR (190 games, small sample). Weakest matchups remain passive bots — neither feature specifically targets passive exploitation.
- **v16**: Barrel-freq modulation ±0.03–0.04. Preflop gap handlers from v11 unrecovered through v16 — 5-gen structural debt addressed by v17 crossover.

