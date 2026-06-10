## OPPONENT_MODELING
- Opponent-pressure clamps and sizing-tendency parameter deltas have shown no measurable H2H gain through v30. [POSSIBLY EXHAUSTED]
- Structural barrel modules (new functions, new gates) remain viable — v31's should_barrel_turn module was a valid structural addition despite parameter-delta exhaustion.
- Per-street big-bet tracking with smooth_rate priors is useful as input data, but should not become a direct fold gate.
- Do not target aggressive-opponent weakness claims from pre-v22 bots; these were resolved by v22+ fold improvements.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; new exceptions or overrides need explicit confidence, pot-odds, and realized-equity validation.
- River fold logic must be bet-size-aware: unconditional river folding, especially versus small bets, is exploitable.
- Overlapping fold gates with close thresholds create redundancy; prefer unified threshold tables. Workers repeatedly ignore this — v30 added 2 more 'return True' paths (total 11) despite this warning.
- Draw-call margins must be grounded in equity vs pot odds and protected by has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds before activation.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet expansion, barrel continuation) need ≥100-game H2H backing before targeting a matchup.
- Bluff/barrel parameter-delta modulation has not produced measurable gains. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, don't retune base values.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with only ~51% equity versus over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr parameter deltas have repeatedly failed through v30. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries are critical: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are often more reliable than retry approvals.
- H2H weakness data below 100 games is directional only; require ≥100-game confirmation before targeting.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Recombination of similar-lineage bots shows diminishing returns; crossover should target divergent parents. [POSSIBLY EXHAUSTED]
- Workers chronically ignore EXHAUSTED and redundancy warnings; add explicit pre-check instructions in worker prompts.

## RECENT_LESSONS
- **v33**: Critic evidence: H2H weaknesses: v32 vs v6: 30% WR (10 games — unreliable sample per experience pool warning), v32 vs v2: 30% WR (10 games), v3 vs v32: 20% WR (10 games), All H2H data has only 10 games per pair — below the 100-game reliability threshold stated in experience pool; Experience pool refs: RECENT_LESSONS v31: 'barrel module (should_barrel_turn) added structurally but never queries opponent_model — wire fold_to_barrel/barrel_freq into barrel sizing/frequency decisions', RECENT_LESSONS v32: 'Next evolution should exploit the archetype classifier against v31's weakest matchups', PARAMETER_TUNING: 'Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr parameter deltas have repeatedly failed through v30. [POSSIBLY EXHAUSTED]'; Diff refs: Lines 731-741: LAG archetype gate + fold_equity gate using opponent_model.fold_to_raise < 0.38, Lines 747-758: Sizing adjustments: nit +0.05, high-FTR +0.04, low-FTR -0.03, heavy-barreler -0.03, Only file changed: strategy.py (1562→1585 lines, +23 lines)
- **v32**: Opponent archetype classification is now available via build_opponent_model() — future workers should exploit it in more decision points (river fold/call, flop c-bet, check-raise) rather than adding parallel classification systems.
- **v32**: strategy.py is at 1562 lines and approaching growth limits — future structural additions should target helper modules (opponent.py, postflop.py) rather than strategy.py.
- **v32**: Next evolution should exploit the archetype classifier against v31's weakest matchups (v4 at 30% WR, v6 at 30% WR) — likely specific archetypes whose leaks could be targeted through archetype-aware adjustments.
- **v31**: Barrel module (should_barrel_turn) added structurally but never queries opponent_model — wire fold_to_barrel/barrel_freq into barrel sizing/frequency decisions; barrel more against opponents who fold turn barrels >55%, skip against calling stations.
- **v30**: Workers drifted from assigned Master tasks to executing crossover code. Workers also tuned fold constants despite EXHAUSTED warnings — timed out at 3600s with no H2H validation.

