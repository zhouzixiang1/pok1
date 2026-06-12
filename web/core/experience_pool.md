## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merging.
- Confidence ramp is action-based: `clamp((total_actions - 5) / 35.0, 0, 1)`, reaching full confidence at ~40 opponent actions.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` active components: tier-based equity (hand_strength_tier + estimate_equity_from_tier), opponent-model fold (barrel_freq), multi-barrel action-sequence fold, and SPR commitment thresholds. Do NOT re-add removed components as new features. [POSSIBLY EXHAUSTED]
- EV-based selectors must wire ALL received params (position, texture, opponent model) — adding new without using old ones is a recurring defect (v43→v44).
- EV selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff (never bluff calling stations, boost vs NIT) consistently validated v40–v48.

## PARAMETER_TUNING
- Base postflop sizing ratios stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- **Systemic failure (v30→v50)**: Workers chronically add hand-tuned constants despite [EXHAUSTED] warnings. Wiring pre-existing EXHAUSTED constants into new code also counts as tuning. Must provide per-constant H2H justification ≥100 games. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure ongoing — extract standalone functions to helper modules before adding new logic.

## RECENT_LESSONS
- **v51**: Critic evidence: H2H weaknesses: v50 vs v18: WR=0.550 (11W/9L/20 games) — v18 beats v50 in mirror sample, v50 vs v26: WR=0.300 (3W/7L/10 games) — worst matchup, very low sample, v50 vs v17: WR=0.400 (8W/12L/20 games) — loses to v17, Experience pool documents: 'H2H weaknesses cluster against aggressive opponents (v30 45.0%, v21 45.6%, v20 45.6%, v18 46.4%) confirming over-folding vs barrel aggression'; Experience pool refs: v50 lesson: 'detect_bluff_catch_signal() computes _bc_conf but the fold-chain override ignores it — wire a threshold gate (≥0.6 confidence)', v50 lesson: 'Bluff-catch override preempts river fold and action-sequence fold gates — this can call down vs value-heavy large bets/all-ins. Must add minimum hand-strength floor', PARAMETER_TUNING exhausted: 'Workers chronically add hand-tuned constants despite [EXHAUSTED] warnings' — these changes wire existing variables, not add new constants; Diff refs: strategy.py:1178 — bluff-catch gate adds `_bc_conf >= 0.60 and made_strength >= 0.38` (was: unconditional `_bc_should`), strategy.py:1237-1240 — repeated-raise-trap threshold raised from 0.25→0.42 + new large-bet fold at made_strength < 0.50, strategy.py:1619 — removed thin-value probe_mode condition: `(value_profile and value_profile['tier'] == 'thin' and board_texture and not board_texture['dynamic'])`
- **v50**: `detect_bluff_catch_signal()` computes `_bc_conf` but the fold-chain override ignores it — wire a threshold gate (≥0.6 confidence) so marginal hero-calls only fire against confirmed barreling archetypes (v18, v20, v37), not tight-value bots (v30, v32).
- **v50**: Bluff-catch override preempts river fold and action-sequence fold gates — this can call down vs value-heavy large bets/all-ins. Must add minimum hand-strength floor (pair+top-kicker minimum) so the override does not bypass pot-odds thresholds on aggressive rivers.
- **v50**: H2H weaknesses cluster against aggressive opponents (v30 45.0%, v21 45.6%, v20 45.6%, v18 46.4%) confirming over-folding vs barrel aggression; any fix must avoid the opposite error (under-folding vs value) — requires ≥100-game validation per opponent.
- **v47→v50**: Action-sequence fold safety margins (0.08/0.04/0.06/0.04) still lack ≥100-game H2H validation.

