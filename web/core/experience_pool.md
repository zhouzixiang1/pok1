# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling (flop/turn/river aggr, barrel_freq) is now infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]
- CBet fold-more exploitation max effect ~0.015; betsize exploit ±0.04 too small alone. [POSSIBLY EXHAUSTED]
- Light 4-bet and check-raise trap use opponent PFR + aggression reads for exploitative play — structural features, not threshold micro-adjustments.

## POSTFLOP_STRATEGY
- should_fold_postflop() uses pot-odds + barrel modulation + board_texture + bet_size_bucket. Keep fold gates structurally separate from barrel tuning — v17 conflated them.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems. Avoid overlapping fold gates.
- repeated_raise_trap: 3-tier fold/call/raise logic still leaks vs v4 (~51%). All fold/raise guards must verify branch consistency within same decision block.
- Check-raise trap on dry flops for strong/nut hands returns 0 (check) — if aggression read is inaccurate, strong hands sacrifice flop value entirely. Needs safety threshold on opponent confidence before trapping.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. Size up only when opponent fold data supports it. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense.

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants. Violations waste entire generations.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in, preventing forced folds on callable all-ins.
- Crossover strategy attempted 5+ consecutive gens (v8→v14) with diminishing returns. v6 fold-discipline injection also exhausted. Avoid both unless novel structural angle identified. [POSSIBLY EXHAUSTED]
- Trust early negative critic signals — first-rejection scores are often more strategically accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.

## RECENT_LESSONS
- **v17**: Added light 4-bet (22-44, suited connectors, suited A2s-A5s; 60% freq, 2.5x opponent 3bet, capped 25% stack) + check-raise trap (dry flops, strong/nut, aggr>0.35, 40% randomization). Early data: 65.8% WR on 190 games — promising but small sample.
- **v17**: Weakest matchups remain v5 (20–35% WR) and v8 (30–45% WR). Neither new feature specifically targets passive opponents — general improvements, not targeted exploitation. Monitor v17 vs v5/v6/v7/v9 where v14 was already strong (82%/56%/70%).
- **v16**: Barrel-freq modulation ±0.03–0.04. Preflop gap handlers from v11 unrecovered through v16 — 5-gen structural debt now addressed by v17 crossover.
- **v15**: _aligned_signal_boost coefficients (1.5x, 0.100 barrel) ungrounded — calibrate against actual fold-equity data.
