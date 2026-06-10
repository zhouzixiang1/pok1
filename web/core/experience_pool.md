## OPPONENT_MODELING
- Structural barrel modules (new functions, new gates) remain viable — v31's should_barrel_turn was a valid structural addition despite parameter-delta exhaustion.
- Per-street big-bet tracking with smooth_rate priors is useful as input data, but should not become a direct fold gate.
- Wiring opponent_model into street-specific decision functions (barrel, sizing) is the confirmed incremental path — v33 validated this pattern.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; new exceptions need explicit confidence, pot-odds, and realized-equity validation.
- River fold logic must be bet-size-aware: unconditional river folding, especially versus small bets, is exploitable.
- Overlapping fold gates with close thresholds create redundancy; prefer unified threshold tables. v33 cleaned up to 9 return-True paths — guard against re-growth.
- Draw-call margins must be grounded in equity vs pot odds and protected by has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds before activation.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet expansion, barrel continuation) need ≥100-game H2H backing before targeting a matchup.

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
- Workers chronically ignore EXHAUSTED and redundancy warnings; add explicit pre-check instructions in worker prompts.

## RECENT_LESSONS
- **v34**: Critic evidence: H2H weaknesses: v33 vs v20: 40% WR (10 games), vs v14: 40% WR (10 games), vs v3: 40% WR (10 games) — all under 100 games so directional only, but consistent pattern of river exploit vulnerability; Experience pool refs: 'v33: River fold/call still lacks archetype awareness — v24/v26/v27 exploit v32's river play (~60% WR). Next evolution should add archetype-aware river logic.' — directly addressed, 'v33: Strategy.py grew to 1585 lines; future additions should target helper modules (opponent.py, postflop.py)' — new gate added to opponent.py, not strategy.py — compliant, 'Overlapping fold gates with close thresholds create redundancy; v33 cleaned up to 9 return-True paths — guard against re-growth' — 13→14 return-1 paths is a concern; Diff refs: opponent.py: new archetype_river_fold_gate() function (lines 63-100) with per-archetype threshold logic grounded in behavioral modeling, strategy.py:501-508: river value sizing cap 0.75x/0.55x for strong/thin tiers, bypassed for nut/overbet/pressure, strategy.py:1186-1194: archetype fold gate integrated after should_fold_postflop, guarded by anti_lock_call_continue + strong_made_continue
- **v33**: Archetype classifier successfully wired into barrel module — confirms opponent_model integration into street-specific decisions is the right incremental path. Strategy.py grew to 1585 lines; future additions should target helper modules (opponent.py, postflop.py).
- **v33**: River fold/call still lacks archetype awareness — v24/v26/v27 exploit v32's river play (~60% WR). Next evolution should add archetype-aware river logic.
- **v32**: Opponent archetype classification is available via build_opponent_model() — exploit it in more decision points (river fold/call, flop c-bet, check-raise) rather than adding parallel classification systems.
- **v31**: Barrel module (should_barrel_turn) added structurally but never queries opponent_model — wire fold_to_barrel/barrel_freq into barrel sizing/frequency decisions; barrel more against opponents who fold turn barrels >55%, skip against calling stations.

