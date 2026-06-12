## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merging.
- Bluff and fold logic must be opponent-type gated — aggressive opponents exploit uniform play. Currently wired via EQR barrel adjustment (barrel_freq ≥ 0.55 → eqr -= 0.04).

## POSTFLOP_STRATEGY
- `should_fold_postflop()` active components: tier-based equity gating, EQR-adjusted equity vs pot-odds, opponent-model barrel-frequency modifier. Do not re-add removed standalone gates (SPR fold, river multi-barrel fold) — they are already integrated into EQR. [POSSIBLY EXHAUSTED]
- EV-based selectors must wire ALL received params (position, texture, opponent model) and gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Preserve pot-odds thresholds for shove/all-in situations — removing equity checks causes regression.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff (never bluff calling stations, boost vs NIT) validated through v50+.
- Bluff-catch signals and trap-fold thresholds are generation-specific; exact constants do not generalize. Must validate with ≥100-game H2H per opponent archetype.

## PARAMETER_TUNING
- Base postflop sizing ratios stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths not ratio bumps.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- **HARD GATE — constant tuning deadlock (v30→v51)**: Workers chronically modify constants despite [EXHAUSTED] warnings. ALL constant changes require per-constant H2H ≥100 games. Reviewer MUST reject constant-only diffs without H2H data. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure ongoing — extract standalone functions to helper modules before adding new logic.

## RECENT_LESSONS
- **v51**: Bluff-catch signal added then removed in v52 — the pattern is valid but exact constants and module structure were not stable. Future implementations must use opponent-type-specific thresholds with H2H validation.
- **v50→v51**: Over-folding vs barrel aggression coexists with trap-fold conservatism — fixes must be opponent-type-specific. Requires ≥100-game H2H per opponent.
- **v52 rejection**: Pipeline correctly rejected a constant-only worker attempt (sizing bumps, phantom edits, zero H2H data). Confirms HARD GATE enforcement works; keep the gate.
