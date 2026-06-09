# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Light 4-bet and check-raise trap need structural reads (PFR + aggression), not threshold micro-adjustments.
- v25+ weakest matchups are mid-tier non-passive: v14 (45.7%), v13 (46.7%), v20 (48.0%), v19 (48.3%) — tight margins where structural weapons (light 4-bet, trap) could shift outcomes. v18+ dominates passive bots (v3/v4/v7 WR 0.62–0.70).
- _is_passive_opponent() 3-factor detection (postflop_aggr ≤ 0.30 + vpip ≥ 0.50 + barrel_freq ≤ 0.35, confidence ≥ 0.25) — well-grounded passive identifier.
- passive_exploit_thin_value bypasses thin_static_showdown_control on turn vs confirmed passive opponents (7 guards) — structural path beyond exhausted fold-margin tuning.
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.
- CBet/exploitation micro-adjustments max ~0.015 effect; sizing_aggr deltas ≥0.08 may produce measurable H2H shifts (0.09 threshold reached in v24 but inconclusive with sample sizes).

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE single fold gate. Any override BEFORE it bypasses all guards. No exceptions.
- Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air and bot folds bottom/middle pair.
- Check-raise trap on dry flops needs safety threshold on opponent confidence before trapping.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. sizing_aggr enables opponent-aware sizing — need larger sample sizes to confirm whether 0.08+ deltas produce measurable shifts.
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- Fold margin / clamp / EQR / SPR-commitment fold guard tuning repeatedly attempted with no measurable gain through v23. [POSSIBLY EXHAUSTED]
- passive_opponent_exploit_bonus (capped 0.08, confidence≥0.20) — gate wider thresholds behind higher confidence (≥0.35) if regresses vs aggressive bots.
- Crossover recombination of v15/v18 lineages through v24 (v18×v23) — none beat v15. v25 attempted v23+v17 light 4-bet/trap (structurally new weapons, not pure tuning). Gene pool largely converged; future crossovers must introduce genuinely new structural features, not recombine existing tuning. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in.
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals.
- H2H weakness data unreliable with small samples (<100 games). Use directional signal only for hypothesis generation; require ≥100-game backing before committing to targeted changes.
- Single-file crossover is clean and low-risk — target future crossovers with divergence in 1-2 files.
- New river/pot-odds fold gates must be validated against existing should_fold_postflop() and realized_postflop_equity checks before insertion — avoid inserting simpler gates upstream of sophisticated ones.

## RECENT_LESSONS
- **v25**: Added light 4-bet weapon (_is_fourbet_light_candidate + _should_4bet_light with PFR≥0.25 + aggr≥0.35 gates) and check-raise trap (_should_checkraise_trap, dry flops, strong hands, 40% frequency). Fixed wheel straight bug in card_utils.py. Structurally new preflop weapon, not threshold tuning. Critic noted risk of pot_odds river gate bypassing should_fold_postflop().
- **v24**: Crossover v18×v23 (Critic 6.0). H2H vs mid-tier tight: v15 47.06%, v17 48.82%, v14 49.57%. sizing_aggr metric added. SB limp-iso-raise classification fix confirmed working.
- **v23**: Opponent-model EQR adjustments + river thin value + pot_odds river gate. Critic 7.0. Branched from v18 (not texture-gated v22 which regressed to WR 0.563).
