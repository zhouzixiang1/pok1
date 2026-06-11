## OPPONENT_MODELING
- Structural barrel modules remain viable — v31's should_barrel_turn validated despite parameter-delta exhaustion.
- Wiring opponent_model into street-specific decisions (barrel, sizing) is the confirmed incremental path — v33 validated.
- Opponent archetype classification should be exploited in more decision points; river fold addressed in v34, flop c-bet and check-raise still open.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- Fold gate sprawl confirmed EXHAUSTED — must_continue_vs_raise has 4+ conditional paths (v38). Do NOT add more branches; refactor into a decision table or extract river_raise_response(). [EXHAUSTED]
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
- Preflop defense replacements must preserve coverage — always compare numerical coverage before/after.
- thin_control gate exempts nut/strong tiers; strong postflop raises floored at 0.50 pot.

## RECENT_LESSONS
- **v39**: Critic evidence: H2H weaknesses: v38 overall win_rate 50.26% (380 games); v37 loses ~30% WR to v27 and ~40% WR to v34/v22/v26/v16/v2 — no specific matchup analysis links losses to BB over-folding or repeated-raise calling, but BB defense is a broad H2H improvement in heads-up play; Experience pool refs: EXHAUSTED: 'Fold gate sprawl — must_continue_vs_raise has 4+ conditional paths. Do NOT add more branches; refactor into decision table or extract.' This change EXTRACTS into a function rather than adding inline branches, partially following this guidance., EXHAUSTED: 'PARAMETER_TUNING fold margin/clamp/EQR/SPR-commitment/sizing_aggr deltas have failed v30→v38.' Neither change here is parameter tuning — both are structural.; Diff refs: strategy.py:489-518 — new `_bb_defend_vs_raise()` function: defends pairs (set-mining), suited hands (flush draw playability), aces (high card + blocker), two-broadway (strong combos), connected offsuit gap≤2 low≥8 (playability). Covers 642/1326 combos (48.4%), only fires when old strength threshold already failed., strategy.py:606-608 — BB vs raise: adds `_bb_defend_vs_raise()` call as defense floor before `return -1` fold., strategy.py:713-735 — new `_handle_repeated_raise()` function: nut hands fall through to raise logic, weak hands (made<0.25, draw<0.14) fold vs medium/large sizing, everything else falls through to normal evaluation.
- **v38**: must_continue_vs_raise has 4+ conditional fold-gate paths — confirmed fold gate sprawl. Do NOT add more branches; refactor into decision table or extract river_raise_response(). [EXHAUSTED]
- **v38**: Critic explicitly requested investigating v37's 70% loss rate to v27 with 50 verbose mirror games before any structural change. Data-first approach over assumption-first.
- **v38**: H2H weaknesses: v37 loses to v27 (~30% WR), v34/v22/v26/v16/v2 (~40% WR). No match analysis links losses to river raise calling or probe-mode river sizing — investigation needed.
- **v37**: Light 4-bet bluff wiring used 8 EXHAUSTED constants — exactly the pattern PARAMETER_TUNING warns against. No H2H backing existed. Verify battle performance before extending.
- **v37**: Top opponents should be identified from current ratings at generation time, not from stale snapshots.

