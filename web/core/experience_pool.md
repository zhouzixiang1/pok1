## OPPONENT_MODELING
- Structural barrel modules remain viable — v31's should_barrel_turn was valid despite parameter-delta exhaustion.
- Wiring opponent_model into street-specific decisions (barrel, sizing) is the confirmed incremental path — v33 validated.
- Opponent archetype classification should be exploited in more decision points; river fold addressed in v34, flop c-bet and check-raise still open.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- Fold gate sprawl is an ongoing risk — prefer unified threshold tables over new gate functions. [POSSIBLY EXHAUSTED]
- Draw-call margins must be grounded in equity vs pot odds with has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel continuation) need ≥100-game H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, not retuning.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas have failed v30→v36. [EXHAUSTED]
- Hand-tuned thresholds in 'structural' modules are still parameter tuning — EXHAUSTED applies regardless of file location.

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Workers chronically ignore EXHAUSTED warnings; add explicit pre-check instructions in worker prompts.
- Strategy.py at 1393 lines (v36); future additions should target helper modules.

## RECENT_LESSONS
- **v37**: Critic evidence: H2H weaknesses: v36's weakest matchups: v16 (30% WR, 10 games), v31 (40% WR, 10 games), v14 (45% WR, 20 games), v34 (45% WR, 20 games) — all below 100-game reliability threshold per experience pool; Experience pool refs: BLUFF_CALIBRATION: 'Structural bluff modules (4-bet light...) need ≥100-game H2H backing before targeting a matchup' — no such backing exists, EXHAUSTED: 'Hand-tuned thresholds in structural modules are still parameter tuning — EXHAUSTED applies regardless of file location', RECENT: 'Strategy.py at 1393 lines (v36); future additions should target helper modules' — now at 1416 lines; Diff refs: sb_vs_reraise handler: added light 4-bet bluff block (lines 591-602), medium-strength call (lines 603-606), LAG exploit call (lines 607-609), 8 LIGHT_4BET_* constants were pre-existing in v36 constants.py but unused — v37 wires them into strategy.py
- **v34**: Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate.
- **v35**: thin_control gate exempts nut/strong tiers; strong postflop raises floored at 0.50 pot.
- **v36**: Built on fixed v22 base + v33 archetype modules + overbet/donk/probe additions. **Unevaluated** (rd 351, zero H2H games). Verify H2H vs v29/v6 before deciding next direction.
- **v36**: Preflop defense replacements must preserve coverage — v36's bb_defense_vs_raise() was tighter than old code. Always compare numerical coverage before/after.
- **v36**: Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations.

