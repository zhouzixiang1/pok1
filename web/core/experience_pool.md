# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling (flop/turn/river aggr, barrel_freq) wired into pressure_adjustment/EQR — magnitudes 4-10x prior (0.06–0.08, clamp [-0.12, 0.15]). Awaiting eval. [POSSIBLY EXHAUSTED]
- CBet fold-more exploitation has max effect ~0.015 — too small to matter alone. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- should_fold_postflop() rewritten to pot-odds formula replacing 6 hardcoded thresholds. Needs opponent_model parameter to modulate fold thresholds by barrel_freq — high barrel_freq (≥0.60) opponents should see medium-strength hands folded more readily. [POSSIBLY EXHAUSTED]
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems. Avoid overlapping fold gates.
- repeated_raise_trap: 3-tier fold/call/raise logic (v14) still leaks vs v4 (~51%). All fold/raise guards must verify branch consistency within same decision block.

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
- **v16**: Critic evidence: H2H weaknesses: v15 weakest matchups: v5 (50%), v6 (50%), v8 (50%). v15 vs v4 improved to 70% (from v14's ~51%). The changes target postflop over-folding vs aggression, which primarily affects v5/v6/v8 type opponents who barrel frequently.; Experience pool refs: v15 lesson: 'should_fold_postflop() rewritten to pot-odds formula. Needs opponent_model parameter to modulate fold thresholds by barrel_freq — high barrel_freq (≥0.60) opponents should see medium-strength hands folded more readily. [POSSIBLY EXHAUSTED]' — This change directly implements this lesson., v15 gap: 'Preflop gap handlers (bb_vs_raise, sb_vs_reraise) from v11 still missing — high-value recovery target' — NOT addressed in this generation.; Diff refs: strategy.py: should_fold_postflop() rewritten (lines 582-614): 6-branch if/else → single threshold formula with opp_bets and barrel-freq modifiers., strategy.py line 594: New tiny bet guard 'if last_raise_pot_ratio <= 0.20: return False' replaces v15's bet_size_bucket('small'=≤0.30) check., strategy.py line 589-590: New draw early return 'if has_draw: return False' replaces scattered 'not has_draw' conditions in v15.
- **v15**: Per-street profiling wired. should_fold_postflop() rewritten to pot-odds formula. No eval data yet. Highest-value next step: add opponent_model parameter to modulate fold thresholds by barrel_freq — directly targets v4 matchup (50.7–52.0%).
- **v15**: _aligned_signal_boost (geometric mean of per-street/aggregate deviations) is sound but coefficients (1.5x, 0.100 barrel) ungrounded — calibrate against actual fold-equity data once eval arrives.
- **v15 gap**: Preflop gap handlers (bb_vs_raise, sb_vs_reraise) from v11 still missing — high-value recovery target.
- **v15 H2H**: Weakest matchups: v4 (50.7–52.0%), v5 (53.3%), v14 (54.5%). v4 has highest overall win rate (59.3%) — per-street reads targeting it may yield most gain.
- **v14**: Failed to beat v13 (1726.9 vs 1735.0). repeated_raise_trap 3-tier fix + texture-aware must_continue_vs_raise() still insufficient vs v4.
- **v13**: v11×v6 fold-discipline crossover failed twice (v12→r=1667, v13→unevaluated). Preflop gap handlers lost and not recovered.

