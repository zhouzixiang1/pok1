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
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); new structural paths can extend these but don't retune base values.
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
- Workers chronically ignore EXHAUSTED and redundancy warnings from the experience pool; consider adding explicit pre-check instructions in worker prompts to prevent re-tuning exhausted parameter axes.

## RECENT_LESSONS
- **v31**: Critic evidence: H2H weaknesses: v30 H2H data is thin (370 games total, most matchups 10-20 games) — below 100-game reliability threshold. v30 overall win_rate=0.581. Weakest matchups: v12 (wr=0.300, 20 games), v3 (wr=0.300, 20 games), v6 (wr=0.300, 10 games). These are all under 100 games so directional only.; Experience pool refs: Experience pool explicitly states: 'Turn barrel continuation after flop c-bet-then-called remains an unfilled gap — next gen should target this with a dedicated barrel module gated by nutted_risk checks.' — directly addressed by the barrel module., EXHAUSTED tag on 'Bluff/barrel modulation via tiny parameter deltas' — this change is structural (new module), not a parameter delta, so the tag does not apply., v30 lesson: 'Workers drifted from assigned Master tasks (turn barrel module, probe sizing cap)' — this generation correctly prioritized the barrel module as the primary structural change.; Diff refs: strategy.py: new function should_barrel_turn() (lines 707-746) — 42-line barrel continuation module with equity gates, texture sizing, nut exclusion. opponent_model parameter accepted but never used inside the function body., strategy.py: barrel invocation at lines 1440-1448, placed after overbet/donk/probe modules and before river bluff logic — correct priority ordering., constants.py: BB_VALUE_3BET_THRESHOLD 0.60→0.58, BB_BLUFF_3BET_HIGH 0.54→0.56, BB_BLUFF_3BET_FREQ 0.25→0.30 — modest preflop widening with no specific H2H basis.
- **v30**: Workers drifted from assigned Master tasks (turn barrel module, probe sizing cap) to executing crossover code instead. Gap-broadway limp (K7o, Q6o range) and trap-fold guardrails (made<0.25, draw<0.14) are structurally novel features worth monitoring but were not the planned scope.
- **v30**: Workers again tuned fold constants (FOLD_RIVER_WEAK 0.35→0.40, FOLD_RIVER_MED 0.40→0.45) and added a textured-board fold gate despite EXHAUSTED warnings. Timed out at 3600s with no H2H validation.
- **v30**: v28's worst matchup is v22 (wr=0.425). v21 (crossover source) beats v22 at 0.52 and v25 at 0.575, confirming crossover feature selection was sound. Turn barrel continuation after flop c-bet-then-called remains an unfilled gap — next gen should target this with a dedicated barrel module gated by nutted_risk checks.
- **v29**: Strong-tier overbet on dry rivers (wetness≤0.35, risk≤0.04, freq≤0.45) was a sound structural experiment, but v29 regressed ~26 rating points from v28. Real leak vs worst matchup (v21) is likely preflop/flop, not river value extraction.
- **v28**: Crossover (v22×v27) added size_bucket river fold gates + pot_odds_call_threshold() + overbet.py. Carries re-raise baseline fix. Currently #2 at r=1592.4, validating crossover with divergent parents.

