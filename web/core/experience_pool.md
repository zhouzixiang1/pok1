## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merging.
- Bluff and fold logic must be opponent-type gated — aggressive opponents exploit uniform play. Currently wired via EQR barrel adjustment (barrel_freq ≥ 0.55 → eqr -= 0.04).

## POSTFLOP_STRATEGY
- `should_fold_postflop()` active components: tier-based equity gating, EQR-adjusted equity vs pot-odds, opponent-model barrel-frequency modifier. Do not re-add removed standalone gates (SPR fold, river multi-barrel fold) — already integrated into EQR. [POSSIBLY EXHAUSTED]
- EV-based selectors must wire ALL received params (position, texture, opponent model) and gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Preserve pot-odds thresholds for shove/all-in situations — removing equity checks causes regression.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff (never bluff calling stations, boost vs NIT) validated through v50+.
- Bluff-catch signals and trap-fold thresholds are generation-specific; exact constants do not generalize. Must validate with ≥100-game H2H per opponent archetype.

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) and preflop 3bet threshold (0.60) are established baselines — extend via structural paths, not ratio bumps.
- **HARD GATE — constant tuning deadlock (v30→v51)**: Workers chronically modify constants despite [EXHAUSTED] warnings. ALL constant changes require per-constant H2H ≥100 games. Reviewer MUST reject constant-only diffs without H2H data. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure ongoing — extract standalone functions to helper modules before adding new logic.

## RECENT_LESSONS
- **v52**: Critic evidence: H2H weaknesses: No v52 H2H data available (bot not yet evaluated). The experience pool notes v50→v51 'over-folding vs barrel aggression coexists with trap-fold conservatism' — the archetype sign inversion is the root cause of the over-folding against LAGs (inverted delta made bot fold MORE vs bluffers).; Experience pool refs: POSTFLOP_STRATEGY: 'Do not re-add removed standalone gates (SPR fold, river multi-barrel fold) — already integrated into EQR. [POSSIBLY EXHAUSTED]' — the new multi-street barrel fold partially contradicts this., PARAMETER_TUNING: 'HARD GATE — constant tuning deadlock (v30→v51)' — this generation avoids constant tuning; the changes are structural (sign fix, new fold paths)., RECENT_LESSONS: 'v50→v51: Over-folding vs barrel aggression coexists with trap-fold conservatism — fixes must be opponent-type-specific.' — the archetype fix directly addresses this.; Diff refs: postflop.py lines 1160-1164: archetype_delta sign inversion fixed — NIT delta -0.04→+0.04 (now folds more vs NITs), LAG delta 0.03→-0.03 (now folds less vs LAGs), CS delta -0.02→+0.02 (now folds more vs CS). Verified: eff_made = made_strength - archetype_delta, so positive delta lowers eff_made → more folds., postflop.py lines 1197-1203: New multi-street barrel fold using opp_postflop_bet_count (tracked in opponent.py across all postflop streets). Folds on turn (eff_made < 0.30) and river (eff_made < 0.38) when opponent bet 2+ streets., strategy.py lines 1016-1025: Direct equity fold gate — realized_rate < pot_odds - 0.08 with guards (no strong/nut, draw < 0.14, made < 0.40, not anti_lock, not strong_made_continue).
- **v51→v52**: Bluff-catch signal added then removed — the pattern is valid but exact constants and module structure were not stable. Future implementations must use opponent-type-specific thresholds with H2H validation.
- **v50→v51**: Over-folding vs barrel aggression coexists with trap-fold conservatism — fixes must be opponent-type-specific. Requires ≥100-game H2H per opponent.
- **v52 rejection**: Pipeline correctly rejected a constant-only worker attempt (sizing bumps, phantom edits, zero H2H data). Confirms HARD GATE enforcement works; keep the gate.

