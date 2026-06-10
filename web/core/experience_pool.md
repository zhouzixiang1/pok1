## OPPONENT_MODELING
- Opponent-pressure clamps, sizing-tendency deltas, barrel/sizing modulation, and bet-size pattern classification are exhausted tuning variants with no measurable H2H gain through v30. [POSSIBLY EXHAUSTED]
- Per-street big-bet tracking with smooth_rate priors is useful as input data, but should not become a direct fold gate.
- Do not target aggressive-opponent weakness claims from pre-v22 bots; these are resolved.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; new exceptions or overrides need explicit confidence, pot-odds, and realized-equity validation.
- River fold logic must be bet-size-aware: unconditional river folding, especially versus small bets, is exploitable.
- Overlapping fold gates with close thresholds create redundancy; prefer unified threshold tables or priority-ordered gates. Workers repeatedly ignore this — v30 added 2 more 'return True' paths (total 11) despite this warning.
- Draw-call margins must be grounded in equity vs pot odds and protected by has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds before activation.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet expansion) carry promise but need battle validation; do not add them from stale weakness claims alone — require ≥100-game H2H backing before targeting a matchup.
- Bluff/barrel modulation via tiny parameter deltas has not produced measurable gains and should not be repeated without a structural exploit hypothesis. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); new structural paths (e.g., strong-tier overbet) can extend these but don't retune base values.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with only ~51% equity versus over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas have repeatedly failed through v30. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries are critical: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist, otherwise tags/version tracking break.
- Trust early negative Critic signals; first-rejection scores are often more reliable than retry approvals.
- H2H weakness data below 100 games is directional only; require ≥100-game confirmation before using as an evolution target.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect before declaring success.
- Single-file crossover is low-risk only when combining genuinely new structural features.
- Recombination of similar-lineage bots shows diminishing returns; crossover should target genuinely divergent parents. [POSSIBLY EXHAUSTED]

## RECENT_LESSONS
- **v30**: Timed out after 3600s. Workers tightened fold constants (FOLD_RIVER_WEAK 0.35→0.40, FOLD_RIVER_MED 0.40→0.45) and added textured-board fold gate despite EXHAUSTED warnings on parameter tuning and redundancy warnings on fold gates. H2H weakness claims from <100 games were used despite pool rules requiring ≥100. Pattern: workers ignore EXHAUSTED/GENERAL warnings — consider adding explicit pre-check in worker prompts.
- **v29**: Strong-tier overbet on dry rivers (wetness≤0.35, risk≤0.04, freq≤0.45) was a sound structural experiment, but v29 regressed ~26 rating points from v28. The nutted_risk≤0.04 threshold may be too tight. Real leak vs worst matchup (v21) is likely preflop/flop, not river value extraction.
- **v28**: Crossover (v22×v27) added size_bucket river fold gates + pot_odds_call_threshold() + overbet.py module. Carries re-raise baseline fix. v28 is currently #2 at r=1592.4, validating crossover approach with divergent parents.
