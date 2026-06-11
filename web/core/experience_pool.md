## OPPONENT_MODELING
- Structural barrel modules (new functions, gates) remain viable — v31's should_barrel_turn was valid despite parameter-delta exhaustion.
- Per-street big-bet tracking with smooth_rate priors is useful input data but should not become a direct fold gate.
- Wiring opponent_model into street-specific decision functions (barrel, sizing) is the confirmed incremental path — v33 validated this.
- Opponent archetype classification should be exploited in more decision points; river fold addressed in v34, flop c-bet and check-raise still open.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; new exceptions need explicit confidence, pot-odds, and realized-equity validation.
- Fold gate sprawl is an ongoing risk — prefer unified threshold tables over new gate functions. Any new fold logic must integrate with existing bet-size-aware guards (v28 L704-714) rather than bypassing them. [POSSIBLY EXHAUSTED]
- Draw-call margins must be grounded in equity vs pot odds and protected by has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds before activation.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet expansion, barrel continuation) need ≥100-game H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, don't retune base values.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with only ~51% equity versus over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr parameter deltas have repeatedly failed from v30 through v36. [EXHAUSTED — do not retry these parameter categories]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are often more reliable than retry approvals.
- H2H weakness data below 100 games is directional only; require ≥100-game confirmation before targeting.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Workers chronically ignore EXHAUSTED and redundancy warnings; add explicit pre-check instructions in worker prompts.
- Strategy.py at 1585 lines (v33); future additions should target helper modules (opponent.py, postflop.py).

## RECENT_LESSONS
- **v36**: Critic evidence: H2H weaknesses: v22 loses to v11 at 35.4% WR (500 games) and v2 at 38.8% (430 games) — the archetype classifier enables opponent-type-specific adjustments against these unknown opponent styles, v33 showed promise vs v32 (60.0% WR, 30 games) and v29 (56.7% WR, 30 games), suggesting structural improvements from v33 are worth merging; Experience pool refs: EXPERIENCE_POOL confirms: 'Opponent archetype classification should be exploited in more decision points; river fold addressed in v34, flop c-bet and check-raise still open' — this crossover implements the archetype classification, EXPERIENCE_POOL warns: 'Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr parameter deltas have repeatedly failed from v30 through v36. [EXHAUSTED]' — the EQR -0.06 for heavy barrelers touches this territory but is structurally different (opponent_model-driven, not standalone tuning), EXPERIENCE_POOL notes: 'Structural barrel modules remain viable' and 'Structural changes can inflate Critic scores without improving battle performance; verify H2H effect' — new modules (overbet/donk/probe) are structural, which is the viable path; Diff refs: constants.py: TOTAL_HANDS 50→70 — critical fix, v22 was playing wrong game length, card_utils.py: Added wheel straight (A-2-3-4-5) detection — bug fix, state.py L262: min_raise_action fix +1 for re-raise baseline compliance
- **v36**: Preflop structural changes must not introduce reversed opponent-model logic — v36 folded speculative SB hands vs 'tight opponents' (fold_to_raise > 0.55) when tight opponents are least likely to exploit limpers. Verify conditionals match stated intent.
- **v36**: Replacing preflop defense modules must preserve or widen coverage — v36's new bb_defense_vs_raise() (48.7%) was actually tighter than old code (~55-65%). Always compare numerical coverage before/after.
- **v36**: Removing all-in equity checks is dangerous — v36's sb_response_vs_3bet() called with strong_pair (JJ-88) vs all-in without equity validation. Preserve pot-odds-based thresholds for shove situations.
- **v36**: Hand-tuned thresholds in 'structural' modules (0.42, 0.28, 0.35) are still parameter tuning — EXHAUSTED warnings apply regardless of file location.
- **v35**: thin_control gate exempts 'nut' and 'strong' tiers; strong postflop raises floored at 0.50 pot. Monitor H2H vs v20 (56.8% opp WR) and v22 (57.0% opp WR).
- **v34**: Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate — otherwise archetype assumptions override equity calculations.

