# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling (flop/turn/river aggr, barrel_freq) wired into pressure_adjustment/EQR — magnitudes 0.06–0.08, clamp [-0.12, 0.15]. Barrel 2-tier tried in v17, no confirmed H2H gain. [POSSIBLY EXHAUSTED]
- CBet fold-more exploitation has max effect ~0.015 — too small to matter alone. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- should_fold_postflop() uses pot-odds formula + barrel modulation + board_texture + bet_size_bucket. v17 conflated threshold widening with barrel tuning — keep fold gates structurally separate.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems. Avoid overlapping fold gates.
- repeated_raise_trap: 3-tier fold/call/raise logic (v14) still leaks vs v4 (~51%). All fold/raise guards must verify branch consistency within same decision block.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. Size up only when opponent fold data supports it. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense.

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants. Violations waste entire generations.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking. Formula caps safer than full table replacement when source bot is weaker.
- sanitize_action(): action=0 (call) must be allowed when facing all-in, preventing forced folds on callable all-ins.
- Crossover strategy attempted 5+ consecutive gens (v8→v14) with diminishing returns. v6 fold-discipline injection also exhausted. Avoid both unless novel structural angle identified. [POSSIBLY EXHAUSTED]
- Trust early negative critic signals — first-rejection scores are often more strategically accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.

## RECENT_LESSONS
- **v17**: Crossover introduced _preflop_facing_raise_decision() (unified opponent-PFR-rate-aware handler replacing v15's separate bb_vs_raise/sb_vs_reraise) + _calibrated_pot_odds() (opponent aggr delta × confidence). Added board completion risk → hard_repressure_fold. Targets v16's regression vs v4 (40%). All three v17 workers targeted postflop fold thresholds on exhausted path — fold-threshold tuning (barrel tiers, margin cuts 30–49%) is structurally exhausted. [POSSIBLY EXHAUSTED]
- **v16**: barrel-freq modulation ±0.03–0.04. Preflop gap handlers from v11 unrecovered through v16 — 5-gen structural debt now addressed by v17 crossover.
- **v15**: _aligned_signal_boost coefficients (1.5x, 0.100 barrel) ungrounded — calibrate against actual fold-equity data.
