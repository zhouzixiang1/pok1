## OPPONENT_MODELING
- Opponent adjustments must integrate INTO equity-based fold checks, not layer as separate gates — validated by v34/v41, rejected in v44 (anti-pattern: sizing profile built but never consumed).
- Monitor whether opponent model reaches confidence within first 30 hands — if not, adjustments never activate.

## POSTFLOP_STRATEGY
- Two parallel fold paths now exist: `should_fold_postflop()` guard chain (range-defense) and inline equity fold gates (pot-odds + safety margins). New fold logic must choose the correct path or coordinate both.
- EV-based selectors must wire ALL received params (position, texture, opponent model) — adding new params without using old ones is a recurring defect (v43→v44).
- EV selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations.
- SPR-based commitment logic (SPR<3/6) is integrated into `pot_odds_call_threshold()` — extend, don't duplicate.
- Verify helper functions still exist before targeting them in evolution plans.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Bluff-roll entropy: strategy.py/tournament.py use `hash(tuple(...))`, postflop.py uses a different sum-based hash — both are game-state-dependent but should harmonize if touchable.
- Opponent-aware bluff cutoff (never bluff calling stations, boost vs NIT) is highest-confidence change from v40.

## PARAMETER_TUNING
- Base postflop sizing ratios stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths. [POSSIBLY EXHAUSTED]
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- **Systemic failure (v30→v44)**: Workers chronically add hand-tuned constants despite EXHAUSTED warnings. Wiring pre-existing EXHAUSTED constants into new code also counts as tuning. Must provide per-constant H2H justification ≥100 games. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure ongoing — extract standalone functions to helper modules before adding new logic.

## RECENT_LESSONS
- **v47**: Critic evidence: H2H weaknesses: v46 loses to 5 opponents at 40% win rate: v41, v30, v32, v34, v40 — these losses are directional-only (10-20 games each, below the 100-game threshold required by experience pool), No evidence these losses stem from BB preflop overcalling or river overfolding to multi-street aggression — the changes lack matchup-specific targeting; Experience pool refs: PARAMETER_TUNING [EXHAUSTED]: 'Workers chronically add hand-tuned constants despite EXHAUSTED warnings. Wiring pre-existing EXHAUSTED constants into new code also counts as tuning. Must provide per-constant H2H justification ≥100 games.' — the 0.30/0.15/0.12/0.08/1.5x/0.50/0.40 constants violate this rule, v46 lesson: 'Monitor river fold frequency via H2H vs aggressive opponents; if fold rate >30% facing half-pot+ bets, recalibrate safety margins downward' — adding MORE river fold gates before measuring v46's effect is premature, OPPONENT_MODELING: 'Opponent adjustments must integrate INTO equity-based fold checks, not layer as separate gates' — the action-sequence fold IS a separate gate layered alongside v46's inline fold, not integrated into should_fold_postflop(); Diff refs: opponent.py +55 lines: new `build_action_sequence_profile()` tracks bet_street_count, is_triple_barrel, is_double_barrel, river_bet_after_check (unused), aggression_intensity (unused), strategy.py +37 lines preflop: `preflop_call_adjustment()` wired into bb_vs_raise spot at line 641, adjusts BB_CALL_THRESHOLD by [-0.04, +0.10] based on opponent PFR/VPIP, strategy.py +14 lines river: action-sequence fold gates at lines 1136-1149 — triple barrel folds made_strength < 0.50, double barrel + medium/large bet folds made_strength < 0.40
- **v46**: Inline fold gates bypassing guard chains (strong_made_continue, anti_lock_call_continue) solve 'guards block all folds' but lose range-defense protection. Add `if strong_made_continue: return` before inline folds.
- **v46**: BB preflop call filters for unsuited disconnected gap≥4 hands have <1% decision coverage — not worth a dedicated worker task.
- **v46**: Safety margins (0.08/0.04/0.06/0.04) in inline river fold are hand-tuned without ≥100 game H2H justification — violates PARAMETER_TUNING rule, needs validation data.
- **v46**: Monitor river fold frequency via H2H vs aggressive opponents; if fold rate >30% facing half-pot+ bets, recalibrate safety margins downward.
- **v45**: Broad H2H underperformance (10-20 games each, directional only) suggests systemic postflop defense weakness — address structurally, not via threshold tuning.

