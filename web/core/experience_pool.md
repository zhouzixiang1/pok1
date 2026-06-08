# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling (flop/turn/river aggr, barrel_freq) wired into pressure_adjustment/EQR — magnitudes 0.06–0.08, clamp [-0.12, 0.15]. No confirmed H2H gain beyond v14 baseline. [POSSIBLY EXHAUSTED]
- CBet fold-more exploitation max effect ~0.015; betsize exploit (±0.04 thresholds) continues same small-adjustment paradigm. Both too small to matter alone. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- should_fold_postflop() uses pot-odds formula + barrel modulation + board_texture + bet_size_bucket. Keep fold gates structurally separate from barrel tuning — v17 conflated them.
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
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in, preventing forced folds on callable all-ins.
- Crossover strategy attempted 5+ consecutive gens (v8→v14) with diminishing returns. v6 fold-discipline injection also exhausted. Avoid both unless novel structural angle identified. [POSSIBLY EXHAUSTED]
- Trust early negative critic signals — first-rejection scores are often more strategically accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.

## RECENT_LESSONS
- **v17**: Critic evidence: H2H weaknesses: v16 vs v12: 30% WR (30 games) — worst matchup, not specifically targeted but light 4-bet could help if v12 3-bets wide, v16 vs v5: 35% WR (20 games) — second-worst, not targeted, v16 vs v8: 45% WR (20 games) — weak matchup, not targeted, v16 vs v14: 50% WR (30 games) — mediocre matchup; Experience pool refs: Previous v17: 'All three workers targeted postflop fold thresholds — this path is structurally exhausted. [POSSIBLY EXHAUSTED]' — NOW addressed with two structural additions (light 4-bet, check-raise trap) that are NOT threshold tweaks, Previous v17: 'betsize exploit (±0.04 thresholds) continues same small-adjustment paradigm' — NEW features use opponent PFR and aggression reads, not threshold micro-adjustments, Previous v17: 'Per-street profiling … No confirmed H2H gain beyond v14 baseline. [POSSIBLY EXHAUSTED]' — light 4-bet and check-raise trap leverage opponent model fields (pfr, flop_aggr, postflop_aggr) for exploitative play, not profiling refinements; Diff refs: _is_fourbet_light_candidate() (lines 471-502): New function identifying light 4-bet hand types — small pairs 22-44, suited connectors 45s-JTs, suited one-gappers 46s-9Js, suited A2s-A5s. Sound poker theory: blocker value (Ax), set-mine equity (small pairs), playability (suited connectors), _should_4bet_light() (lines 504-557): Requires opponent PFR>25%, confidence>15%, hand strength 0.30-0.55, 60% random frequency. Sizing: 2.5x opponent 3-bet total, capped at 25% stack, aborts if ≥50% stack. Stack protection prevents over-commitment, _should_checkraise_trap() (lines 709-756): Only fires on flop (round 1), strong/nut hands, dry boards (wetness≤0.25, not paired, not dynamic), opponent aggression>0.35, 40% hand-based randomization. Returns 0 (check) to induce opponent bets
- **v17**: Critic evidence: H2H weaknesses: v16 vs v5: 35% WR (20 games) — NOT targeted by either function, v16 vs v8: 45% WR (20 games) — NOT targeted, v16 vs v14: 45% WR (20 games) — NOT targeted; Experience pool refs: betsize exploit (±0.04 thresholds) continues same small-adjustment paradigm. Both too small to matter alone. [POSSIBLY EXHAUSTED], Per-street profiling … No confirmed H2H gain beyond v14 baseline. [POSSIBLY EXHAUSTED], All three workers targeted postflop fold thresholds — this path is structurally exhausted. [POSSIBLY EXHAUSTED]; Diff refs: _spr_value_sizing_boost(): lines 470-486, adds 0.05-0.08 sizing boost for nut/strong in SPR 1.5-4.0, integrated at lines 1093 and 1297 via match_sizing_delta, _opponent_betsize_exploit(): lines 489-514, per-street bet_size×aggression pattern detection clamped to ±0.04, subtracted from strong/medium thresholds at lines 808-809, No changes to preflop logic, no changes to core fold/call/raise decision structure — purely additive threshold/sizing adjustments
- **v17**: Crossover introduced _preflop_facing_raise_decision() (unified opponent-PFR-rate-aware handler) + _calibrated_pot_odds(). Also added _opponent_betsize_exploit() and _spr_value_sizing_boost(). v17's worst matchup is v5 (WR=0.200). All three workers targeted postflop fold thresholds — this path is structurally exhausted. [POSSIBLY EXHAUSTED]
- **v17**: v16 over-folds vs weaker opponents — loses to v8 (30%), v11 (30%). Neither SPR sizing boost nor betsize exploit specifically targets these matchups; they are general improvements, not targeted exploitation.
- **v16**: barrel-freq modulation ±0.03–0.04. Preflop gap handlers from v11 unrecovered through v16 — 5-gen structural debt now addressed by v17 crossover.
- **v15**: _aligned_signal_boost coefficients (1.5x, 0.100 barrel) ungrounded — calibrate against actual fold-equity data.


