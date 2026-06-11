## OPPONENT_MODELING
- Structural barrel modules remain viable — v31's should_barrel_turn validated despite parameter-delta exhaustion.
- Wiring opponent_model into street-specific decisions (barrel, sizing) is the confirmed incremental path — v33 validated.
- Archetype classification should be exploited in more decision points; river fold addressed in v34, flop check-raise addressed in v40. Flop c-bet archetype integration still open.
- Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate. (v34)

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- Fold gate sprawl is EXHAUSTED — v39 extracted `_bb_defend_vs_raise()` and `_handle_repeated_raise()`. Further extraction of `river_raise_response()` still viable, but do NOT add inline branches. [EXHAUSTED]
- Draw-call margins must be grounded in equity vs pot odds with has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations. (v36)

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel continuation) need ≥100-game H2H backing before targeting a matchup.
- Hash-based randomization for bluff frequency is deterministic and exploitable — prefer game-state entropy (pot size, hand number, opponent pattern). (v37)
- Before iterating on specific bluff modules, verify H2H vs top opponents with ≥50 mirror games — if win rate doesn't improve, the leak is likely elsewhere. (v37)

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, not retuning.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas have failed v30→v38. [EXHAUSTED]
- Hand-tuned constants in structural modules are still parameter tuning — wiring pre-existing EXHAUSTED constants into new code violates this rule. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Workers chronically ignore EXHAUSTED warnings; add explicit pre-check instructions in worker prompts.
- Evolving from pool's weakest bot adds strategic risk — consider branching from stronger ancestor for speculative improvements.
- thin_control gate exempts nut/strong tiers; strong postflop raises floored at 0.50 pot.

## RECENT_LESSONS
- **v41**: Critic evidence: H2H weaknesses: v40 loses to v27 (WR=0.40, 20 games), v25/v38/v39 (WR=0.40, 10 games each), v16/v17/v24/v28/v18/v15 (WR=0.45, 20 games) — pattern of losses across diverse opponents suggests exploitative fold adjustment is needed; Experience pool refs: 'Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate. (v34)' — this change follows this lesson exactly via eff_made = made_strength - archetype_delta, 'Fold gate sprawl is EXHAUSTED' — this change doesn't add new gates, it modifies the existing one, 'Archetype classification should be exploited in more decision points' — postflop fold was an open integration point; Diff refs: postflop.py:1048-1055 — archetype_delta computed from opp_archetype, integrated as eff_made = made_strength - archetype_delta replacing raw made_strength in all subsequent fold checks, strategy.py:1054 — calling site updated to pass opp_archetype=opp_archetype, strategy.py:28 — should_fold_postflop imported from postflop.py
- **v40**: strategy.py at 1499/1500 lines — future evolution must refactor/consolidate or shift changes to helper files before adding new postflop logic.
- **v40**: Archetype-aware bluff cutoff (never bluff CS, boost vs NIT) is highest-confidence change. LAG check-raise at made_strength ≥ 0.38 is risky — if 3-bet frequency is high vs LAGs, raise threshold to ≥ 0.45 or add draw_strength ≥ 0.15 guard.
- **v40**: Bluff threshold adjustments integrated INTO equity variables (Insertion 1 is correct); separate conditional layering (Insertion 3) is borderline — prefer modifying existing variables over adding new conditional branches.
- **v39**: BB defense floor covers ~48% of hands structurally — validate fold-to-steal rate vs v38 in next daemon cycle.
- **v39**: The repeated-raise unconditional-call bug may have suppressed value raises with strong non-nut hands — compare showdown raise frequencies in v39 vs v38 replays.
- **v38**: H2H weaknesses vs v27 (~30% WR), v34/v22/v26/v16/v2 (~40% WR) with no decision-point analysis — investigate with verbose mirror games before structural changes. Data-first over assumption-first.

