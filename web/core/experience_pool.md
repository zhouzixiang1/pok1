## OPPONENT_MODELING
- Structural barrel modules remain viable — v31's should_barrel_turn validated despite parameter-delta exhaustion.
- Wiring opponent_model into street-specific decisions (barrel, sizing) is the confirmed incremental path — v33 validated.
- Opponent archetype classification should be exploited in more decision points; river fold addressed in v34, flop c-bet and check-raise still open.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- Fold gate sprawl is an ongoing risk — prefer unified threshold tables over new gate functions. [POSSIBLY EXHAUSTED]
- Draw-call margins must be grounded in equity vs pot odds with has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds.
- Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate. (v34)
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations. (v36)

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel continuation) need ≥100-game H2H backing before targeting a matchup.
- Hash-based randomization for bluff frequency is deterministic (same hole cards → same decision) and exploitable. Prefer game-state entropy (pot size, hand number, opponent pattern). (v37)
- Before iterating on specific bluff modules, verify H2H vs top opponents with ≥50 mirror games — if win rate doesn't improve, the leak is likely elsewhere. (v37)

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, not retuning.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas have failed v30→v37. [EXHAUSTED]
- Hand-tuned constants in structural modules are still parameter tuning — wiring pre-existing EXHAUSTED constants into new code (v37 LIGHT_4BET_*) violates this rule. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Workers chronically ignore EXHAUSTED warnings; add explicit pre-check instructions in worker prompts.
- Strategy.py at 1416 lines (v37); future additions should target helper modules.
- Evolving from pool's weakest bot adds strategic risk — consider branching from stronger ancestor for speculative improvements. (v37)
- Preflop defense replacements must preserve coverage — always compare numerical coverage before/after. (v36)

## RECENT_LESSONS
- **v38**: Critic evidence: H2H weaknesses: v37 loses to v27 (30% WR, 10g), v34/v22/v26/v16/v2 (40% WR, 10g each), v30 (45% WR, 20g). No match analysis links these losses to river raise calling with marginal strong hands or probe-mode river sizing.; Experience pool refs: POSTFLOP_STRATEGY: 'Fold gate sprawl is an ongoing risk — prefer unified threshold tables over new gate functions. [POSSIBLY EXHAUSTED]', PARAMETER_TUNING: 'Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas have failed v30→v37. [EXHAUSTED]', GENERAL: 'H2H data below 100 games is directional only; require ≥100-game confirmation before targeting.'; Diff refs: postflop.py:1030-1033 — new river fold gate in must_continue_vs_raise(): round_idx==3 && made_strength<0.52 && pot_odds>0.25 → fold, strategy.py:444-448 — river strong/nut exempt from probe_ratio cap: keeps full ratio for river value bets
- **v37**: Light 4-bet bluff wiring wired 8 pre-existing EXHAUSTED constants into strategy.py — exactly the pattern PARAMETER_TUNING warns against. No H2H backing existed. Verify battle performance before extending.
- **v37**: Top opponents are now v27 (r=1500.3), v26 (r=1494.7), v29 (r=1489.1). Use current top-3 for precommit eval targets, not stale v35/v28 references.
- **v36**: Now evaluated (r=1433.3, rd=71.5). Weakest matchups: v16 (30% WR), v31 (40% WR), v14/v34 (45% WR) — all below 100-game reliability.
- **v35**: thin_control gate exempts nut/strong tiers; strong postflop raises floored at 0.50 pot.

