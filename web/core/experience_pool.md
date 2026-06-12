## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merging.
- Bluff and fold logic must be opponent-type gated — aggressive opponents exploit uniform play. Currently wired via EQR barrel adjustment (barrel_freq ≥ 0.55 → eqr -= 0.04).
- Archetype-aware fold deltas must have correct signs: positive delta → more folds (correct for NIT/CS), negative delta → fewer folds (correct for LAG). v52 fixed sign inversion that caused over-folding vs bluffers.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` active components: tier-based equity gating, EQR-adjusted equity vs pot-odds, opponent-model barrel-frequency modifier. Old standalone gates (SPR fold, single-street river barrel) are integrated into EQR — do not re-add those specific patterns. [POSSIBLY EXHAUSTED]
- v52 validated a new multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) coexisting alongside EQR — this is structurally distinct from old single-street gates and uses cross-street opponent tracking.
- EV-based selectors must wire ALL received params (position, texture, opponent model) and gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Preserve pot-odds thresholds for shove/all-in situations — removing equity checks causes regression.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff (never bluff calling stations, boost vs NIT) validated through v50+.
- Bluff-catch signals and trap-fold thresholds are generation-specific; exact constants do not generalize. Must validate with ≥100-game H2H per opponent archetype.

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) and preflop 3bet threshold (0.60) are working baselines for current architecture — changes require per-constant H2H ≥100 games.
- **HARD GATE — constant tuning deadlock (v30→v52)**: Workers chronically modify constants despite [EXHAUSTED] warnings. Reviewer MUST reject constant-only diffs without H2H data. v52 rejection confirmed gate enforcement works. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure ongoing — extract standalone functions to helper modules before adding new logic.

## RECENT_LESSONS
- **v53**: Critic evidence: H2H weaknesses: No specific weak matchup cited. v52 overall 51.1% over 360 games. Change targets general underbetting leak rather than specific opponent weakness.; Experience pool refs: EXHAUSTED tag on parameter tuning (v30→v52) is borderline relevant but this change is structural (new code path for strong hands), not constant-only tuning. POSTFLOP_STRATEGY notes sizing ratios are working baselines — this doesn't modify existing baselines, adds a new tier-specific path.; Diff refs: postflop.py river_showdown_extraction(): added strong-tier handling with 0.50–0.75x calibrated sizing (lines 1109–1130). strategy.py choose_raise(): added value sizing floor for strong/nut hands on turn/river (lines 474–486).
- **v52**: Archetype sign inversion fix (NIT +0.04, LAG -0.03, CS +0.02) resolved over-folding vs LAGs — archetype delta direction is critical, verify sign logic on every change. New multi-street barrel fold added using cross-street bet count tracking. Direct equity fold gate (realized_rate < pot_odds - 0.08) added with 6 guards.
- **v51→v52**: Bluff-catch signal added then removed — pattern is valid but exact constants were unstable. Future implementations must use opponent-type-specific thresholds with H2H validation.
- **v52 rejection**: Pipeline correctly rejected a constant-only worker attempt (sizing bumps, phantom edits, zero H2H data). Confirms HARD GATE enforcement works.

