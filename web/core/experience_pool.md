# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]
- CBet/exploitation micro-adjustments max ~0.015 effect; betsize exploit ±0.04 too small alone. [POSSIBLY EXHAUSTED]
- Light 4-bet and check-raise trap use structural reads (PFR + aggression), not threshold micro-adjustments.
- Weakest matchups are passive bots (v4/v5/v6/v8). Exploitative turn/river adjustments must be priority.
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.
- passive_exploit_thin_value bypasses thin_static_showdown_control on turn vs confirmed passive opponents (7 guards) — novel structural path beyond exhausted fold-margin tuning.

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE single fold gate. Any override BEFORE it bypasses all guards. No exceptions.
- Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- repeated_raise_trap: 3-tier fold/call/raise logic leaks ~51% vs v4. Verify branch consistency within same decision block.
- Check-raise trap on dry flops needs safety threshold on opponent confidence before trapping.
- Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air and bot folds bottom/middle pair.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. Size up only with opponent fold data support. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense.
- Fold margin / clamp value tuning repeatedly attempted with no measurable gain. [POSSIBLY EXHAUSTED]
- passive_opponent_exploit_bonus (capped 0.08, confidence≥0.20) — gate wider thresholds behind higher confidence (≥0.35) if regresses vs aggressive bots.

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in.
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.
- H2H weakness data unreliable with small samples (<100 games). Targeted changes need ≥100-game backing.
- Crossover strategy and fold-discipline injection both exhausted. [POSSIBLY EXHAUSTED]
- Single-file crossover is clean and low-risk — target future crossovers with divergence in 1-2 files.
- SB limp-then-face-raise misclassified as sb_vs_reraise — keep limp-raise and raise-reraise paths distinct.

## RECENT_LESSONS
- **v22**: Adding fold discipline does not clearly target weak matchups vs crossover bots. Constant tweak patterns (turn_weak_threshold +5%, unconditional river fold) are flagged exhausted with no gain — avoid repeating.
- **v21**: Gap Broadway limp (J4s+/Q3s+/K2s+/T5s+ with high≥11, low≤6) — monitor if it over-folds SB vs aggressive steals.
- **v21**: Wider opponent_pressure_adjustment clamp (-0.12, 0.15) and EQR air-hand block from v15 crossover. Watch H2H vs v16 (v18 lost 48.0%).
- **v20**: Critic rejected preflop changes with 10-20 game samples — unreliable. Future preflop work needs ≥100-game backing.
