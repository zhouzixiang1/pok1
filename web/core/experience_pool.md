# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]
- CBet/exploitation micro-adjustments max ~0.015 effect; betsize exploit ±0.04 too small alone. [POSSIBLY EXHAUSTED]
- Light 4-bet and check-raise trap use structural reads (PFR + aggression), not threshold micro-adjustments.
- Weakest matchups are passive bots (v4/v5/v6/v8). Exploitative turn/river adjustments must be priority.
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE single fold gate. Any override BEFORE it bypasses all guards. No exceptions.
- Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- repeated_raise_trap: 3-tier fold/call/raise logic leaks ~51% vs v4. Verify branch consistency within same decision block.
- Check-raise trap on dry flops needs safety threshold on opponent confidence before trapping.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. Size up only with opponent fold data support. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense.
- Fold margin / clamp value tuning repeatedly attempted with no measurable gain. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in.
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.
- H2H weakness data unreliable with small samples (<100 games). Targeted changes need ≥100-game backing.
- Crossover strategy and fold-discipline injection both exhausted. [POSSIBLY EXHAUSTED]

## RECENT_LESSONS
- **v22**: Critic evidence: H2H weaknesses: v21's weakest matchups with decent samples: vs v14 40.0% (20 games), vs v13 40.0% (20 games), vs v10 43.3% (30 games). Many 'weak' matchups have only 10 games and are unreliable per experience pool ('H2H weakness data unreliable with small samples'). Adding fold discipline does not clearly target any of these — v14/v13 are crossover bots, not confirmed aggressive barrelers.; Experience pool refs: EXPERIENCE_POOL:PARAMETER_TUNING — 'Fold margin / clamp value tuning repeatedly attempted with no measurable gain. [POSSIBLY EXHAUSTED]', EXPERIENCE_POOL:GENERAL — 'Crossover strategy and fold-discipline injection both exhausted. [POSSIBLY EXHAUSTED]', EXPERIENCE_POOL:POSTFLOP_STRATEGY — 'Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.'; Diff refs: FLOP: New gate `made_strength < 0.16 and not has_draw → fold` at line 616 — mostly redundant with existing 0.20 medium/large and 0.22 multi-barrel gates. Only marginal effect: folds 0.16-0.20 hands to small flop bets., TURN: `turn_weak_threshold = 0.25 + 0.05` at line 620 — constant tweak by 5%, no equity basis, the exact pattern flagged exhausted., RIVER: `made_strength < 0.28 and not has_draw → fold` at line 631 — unconditional river fold including small bets. Exploitable: opponent can min-bet river with air and bot folds bottom pair/middle pair weak kicker.
- **v21**: Gap Broadway limp (J4s+/Q3s+/K2s+/T5s+ with high≥11, low≤6) — monitor if it over-folds SB vs aggressive steals.
- **v21**: passive_exploit_thin_value bypasses thin_static_showdown_control on turn vs confirmed passive opponents (7 guards: to_call==0, confidence≥0.25, aggr≤0.30, vpip≥0.50, strength≥0.40, nutted_risk≤0.05) — novel structural path beyond exhausted fold-margin tuning.
- **v21**: From v15 crossover — wider opponent_pressure_adjustment clamp (-0.12, 0.15) and EQR air-hand block with _aligned_signal_boost. Watch H2H vs v16 (v18 lost 48.0%).
- **v20**: Single-file crossover (v15 vs v18: only strategy.py differed) is clean and low-risk — target future crossovers with divergence in 1-2 files.
- **v20**: Critic rejected preflop changes with 10-20 game samples — unreliable. Future preflop work needs ≥100-game backing.
- **v19**: SB limp-then-face-raise misclassified as sb_vs_reraise — keep limp-raise and raise-reraise paths distinct.
- **v19**: passive_opponent_exploit_bonus (capped 0.08, confidence≥0.20) — gate wider thresholds behind higher confidence (≥0.35) if regresses vs aggressive bots.

