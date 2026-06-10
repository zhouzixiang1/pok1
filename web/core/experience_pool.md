## OPPONENT_MODELING
- Structural barrel modules (new functions, new gates) remain viable — v31's should_barrel_turn was a valid structural addition despite parameter-delta exhaustion.
- Per-street big-bet tracking with smooth_rate priors is useful as input data, but should not become a direct fold gate.
- Wiring opponent_model into street-specific decision functions (barrel, sizing) is the confirmed incremental path — v33 validated this pattern.
- Opponent archetype classification via build_opponent_model() should be exploited in more decision points (flop c-bet, check-raise) rather than adding parallel classification systems; river fold already addressed in v34.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; new exceptions need explicit confidence, pot-odds, and realized-equity validation.
- River fold logic must be bet-size-aware: unconditional river folding, especially versus small bets, is exploitable.
- Fold gate sprawl is an ongoing risk: should_fold_postflop return-True paths grew from 9 (v33) to 10 (v34); total fold gates across all functions now at 14. Prefer unified threshold tables over adding new gate functions.
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
- **v35**: Critic evidence: H2H weaknesses: v34 overall win rate 51.95% (213W-197L in 410 games). All individual H2H matchups at 10-20 games — statistically noisy, no confirmed per-opponent weakness. Weakest: v3 (30% in 30 games), v13 (40%), v23 (40%), v30 (40%).; Experience pool refs: POSTFLOP_STRATEGY: 'should_fold_postflop() is the primary fold gate; new exceptions need explicit validation' — this change touches raise sizing, not fold logic, so it doesn't add fold gates., PARAMETER_TUNING: 'Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr parameter deltas have repeatedly failed through v30. [POSSIBLY EXHAUSTED]' — this is NOT parameter tuning; it's fixing a tier classification bug where strong hands were incorrectly caught by thin_control., v34 lesson: 'Fold gate count at 14 total' — this change adds 0 fold gates; it modifies sizing calculation only.; Diff refs: strategy.py L488: thin_control gate changed from 'tier != nut' to 'tier not in (nut, strong)' — strong hands no longer capped at 0.30-0.38 pot by thin_control., strategy.py L513-516: New value raise floor — strong postflop raises (non-bluff, non-probe) floored at 0.50 pot, preventing undersizing., postflop.py L515-516 (unchanged): 'if paired_warning and tier != nut: plan["thin_control"] = True' — this is the trigger path that was incorrectly penalizing strong hands, now fixed by the strategy.py gate.
- **v35**: Critic evidence: H2H weaknesses: v34 has only 310 total games; all H2H matchups at 10 games — statistically meaningless. No confirmed weakness data supports this change.; Experience pool refs: EXPERIENCE_POOL POSTFLOP_STRATEGY: 'River fold logic must be bet-size-aware: unconditional river folding, especially versus small bets, is exploitable.', EXPERIENCE_POOL v34 lesson: 'Fold gate count at 14 total (10 in should_fold_postflop alone) — adding more river fold gates without consolidating existing ones increases branching complexity and contradiction risk.', v28 comment in strategy.py line 704: 'folding marginal hands to small river bets is exploitable (opponents get ~3.3:1 pot odds on blocking bets)'; Diff refs: strategy.py lines 718-727: adds 3 new return-True paths in should_fold_postflop() for small bet + weak hand on turn/river, Contradicts v28's size_bucket guards on lines 704-714 that explicitly require medium/large sizing for fold gates
- **v34**: Archetype fold gate ordering risk: archetype adjustments must be integrated INTO equity-based fold checks rather than layered as a separate gate above them, or archetype assumptions override equity calculations.
- **v34**: Fold gate count at 14 total (10 in should_fold_postflop alone) — adding more river fold gates without consolidating existing ones increases branching complexity and contradiction risk.
- **v34**: Monitor v34's H2H against v14 and v20 (v33 lost 40% to both) — archetype-aware river folding should help against these polarized/barrel-heavy opponents.
- **v33**: Archetype classifier successfully wired into barrel module — confirms opponent_model integration into street-specific decisions is the right incremental path. Strategy.py grew to 1585 lines; future additions should target helper modules (opponent.py, postflop.py).
- **v32**: Opponent archetype classification is available via build_opponent_model() — exploit it in more decision points (flop c-bet, check-raise) rather than adding parallel classification systems; river fold addressed in v34.


