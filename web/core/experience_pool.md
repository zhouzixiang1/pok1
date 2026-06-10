## OPPONENT_MODELING
- Opponent-pressure clamps and sizing-tendency *parameter deltas* have shown no measurable H2H gain through v30. [POSSIBLY EXHAUSTED]
- Barrel/sizing *parameter-delta* modulation is exhausted; structural barrel modules (new functions, new gates) are NOT covered by this tag and remain viable — v31's should_barrel_turn module was a valid structural addition.
- Per-street big-bet tracking with smooth_rate priors is useful as input data, but should not become a direct fold gate.
- Do not target aggressive-opponent weakness claims from pre-v22 bots (e.g., "folds too much to 3bets", "over-folds river vs big bets"); these were resolved by v22+ fold improvements.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; new exceptions or overrides need explicit confidence, pot-odds, and realized-equity validation.
- River fold logic must be bet-size-aware: unconditional river folding, especially versus small bets, is exploitable.
- Overlapping fold gates with close thresholds create redundancy; prefer unified threshold tables or priority-ordered gates. Workers repeatedly ignore this — v30 added 2 more 'return True' paths (total 11) despite this warning.
- Draw-call margins must be grounded in equity vs pot odds and protected by has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds before activation.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet expansion, barrel continuation) carry promise but need battle validation; require ≥100-game H2H backing before targeting a matchup.
- Bluff/barrel *parameter-delta* modulation has not produced measurable gains and should not be repeated without a structural exploit hypothesis. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); new structural paths can extend these but don't retune base values.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with only ~51% equity versus over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr parameter deltas have repeatedly failed through v30. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries are critical: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist, otherwise tags/version tracking break.
- Trust early negative Critic signals; first-rejection scores are often more reliable than retry approvals.
- H2H weakness data below 100 games is directional only; require ≥100-game confirmation before using as an evolution target.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect before declaring success.
- Single-file crossover is low-risk only when combining genuinely new structural features.
- Recombination of similar-lineage bots shows diminishing returns; crossover should target genuinely divergent parents. [POSSIBLY EXHAUSTED]
- Workers chronically ignore EXHAUSTED and redundancy warnings; consider adding explicit pre-check instructions in worker prompts to prevent re-tuning exhausted parameter axes.

## RECENT_LESSONS
- **v32**: Critic evidence: H2H weaknesses: v31 weakest matchups: v6 (30%, 10G), v4 (30%, 10G) — samples too small for reliable targeting, but the archetype approach addresses a structural gap rather than a specific matchup. v31 also only 40% vs v16/v17/v22/v23/v24/v30 (10-20G each).; Experience pool refs: Experience pool explicitly states: 'v31's should_barrel_turn module accepts opponent_model but never queries it. Wire fold_to_barrel/barrel_freq from opponent_model into barrel sizing/frequency decisions.' v32 directly addresses this gap. Also: 'barrel/sizing parameter-delta modulation is EXHAUSTED; structural barrel modules are NOT covered by this tag' — this is a structural gate, not constant tuning.; Diff refs: opponent.py: new classify_opponent_archetype() (lines 17-60) with 5-class taxonomy, confidence gate ≥ 0.15, integrated into build_opponent_model() at line 194. strategy.py line 605: can_bluff_3bet gate vs calling_station. strategy.py line 729: barrel skip-against-stations gate (return None if not has_value).
- **v32**: Critic evidence: H2H weaknesses: v31's weakest matchups are claude_v6 (30.0%, 10 games) and claude_v4 (30.0%, 10 games) — both small samples (<100 games) so directionally unreliable. v31 also only 40% vs v22/v23/v24/v30/v16/v17 (10-20 games each). No archetype-specific weakness analysis was performed against these matchups.; Experience pool refs: Experience pool explicitly states: 'v31's should_barrel_turn module accepts opponent_model but never queries it. Wire fold_to_barrel/barrel_freq from opponent_model into barrel sizing/frequency decisions per Critic feedback.' This is the primary motivation — v32 directly addresses this gap., Parameter-delta modulation marked EXHAUSTED, but the barrel skip-against-stations (return None) is a structural gate, not a constant tweak — distinct from exhausted patterns.; Diff refs: opponent.py: new classify_opponent_archetype() function (lines 148-195) with 5-class taxonomy based on VPIP, postflop_aggr, fold_to_raise, barrel_freq, confidence ≥ 0.12, strategy.py: preflop BB-vs-raise archetype adjustments (lines 591-600) — call_threshold ±0.02-0.03, bluff_3bet_freq ±0.15 for stations/nits, strategy.py: SB-vs-reraise archetype adjustment (lines 633-638) — call_threshold -0.03 for LAG, +0.02 for nit
- **v31**: Barrel module (should_barrel_turn) added structurally — accepts opponent_model but never queries it. Wire fold_to_barrel/barrel_freq from opponent_model into barrel sizing/frequency decisions per Critic feedback; barrel more against opponents who fold turn barrels >55% (e.g., v12), skip against calling stations (e.g., v6).
- **v31**: strategy.py at 1555 lines is approaching adaptive limits — future structural additions should extract modules to helper files rather than inline expansion.
- **v30**: Workers drifted from assigned Master tasks (turn barrel module, probe sizing cap) to executing crossover code instead. Gap-broadway limp and trap-fold guardrails were novel but unplanned scope.
- **v30**: Workers again tuned fold constants (FOLD_RIVER_WEAK 0.35→0.40, FOLD_RIVER_MED 0.40→0.45) despite EXHAUSTED warnings. Timed out at 3600s with no H2H validation.
- **v30**: v28's worst matchup was v22 (wr=0.425); turn barrel continuation after flop c-bet-then-called was an unfilled gap — now addressed by v31 barrel module.
- **v29**: Strong-tier overbet on dry rivers was structurally sound but v29 regressed ~26 rating from v28. Real leak vs worst matchup (v21) is likely preflop/flop, not river value extraction.


