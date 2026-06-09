# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]
- CBet/exploitation micro-adjustments max ~0.015 effect; betsize exploit ±0.04 too small alone. [POSSIBLY EXHAUSTED]
- Light 4-bet and check-raise trap need structural reads (PFR + aggression), not threshold micro-adjustments.
- v18+ dominates passive bots (v3/v4/v7 WR 0.62–0.70). Weakest matchups are mid-tier non-passive: v13 (WR=0.462), v12 (0.321), v11 (0.390), v10 (0.417), v14/v16/v20 (~0.48–0.50). Prioritize exploitative adjustments vs these close-range opponents.
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.
- passive_exploit_thin_value bypasses thin_static_showdown_control on turn vs confirmed passive opponents (7 guards) — structural path beyond exhausted fold-margin tuning.
- _is_passive_opponent() 3-factor detection (postflop_aggr ≤ 0.30 + vpip ≥ 0.50 + barrel_freq ≤ 0.35, confidence ≥ 0.25) — well-grounded passive identifier.

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE single fold gate. Any override BEFORE it bypasses all guards. No exceptions.
- Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air and bot folds bottom/middle pair.
- Board texture classification (5-tier) is a high-value structural axis — combine texture with SPR/opponent-model rather than replace. [POSSIBLY EXHAUSTED]
- Check-raise trap on dry flops needs safety threshold on opponent confidence before trapping.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. Size up only with opponent fold data support. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- Fold margin / clamp value / EQR lower-bound tuning repeatedly attempted with no measurable gain. [POSSIBLY EXHAUSTED]
- passive_opponent_exploit_bonus (capped 0.08, confidence≥0.20) — gate wider thresholds behind higher confidence (≥0.35) if regresses vs aggressive bots.
- SPR/commitment-based fold guards [POSSIBLY EXHAUSTED] — repeated tuning with no measurable gain.
- Crossover recombination of v15/v18 lineages (v18–v22, 5 gens, none beat v15). [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in.
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals.
- H2H weakness data unreliable with small samples (<100 games). Targeted changes need ≥100-game backing.
- Single-file crossover is clean and low-risk — target future crossovers with divergence in 1-2 files.
- SB limp-then-face-raise misclassified as sb_vs_reraise — keep limp-raise and raise-reraise paths distinct.
- New river/pot-odds fold gates must be validated against existing should_fold_postflop() and realized_postflop_equity checks before insertion — avoid inserting simpler gates upstream of sophisticated ones.

## RECENT_LESSONS
- **v24**: Critic evidence: H2H weaknesses: v18 loses to v15 at 47.06% (170 games) and v23 at 45.00% (60 games) — close-range matchups where correct SB limp play could shift margins, v18 vs v17 at 48.82% (170 games), v18 vs v14 at 49.57% (230 games) — tight matchups where sizing exploitation could help, v23 overall win rate 58.93% slightly edges v18's 58.55%, validating the crossover source; Experience pool refs: Experience pool RECENT_LESSONS: 'SB limp-iso-raise was misclassified as sb_vs_reraise for multiple gens — Fixed with new sb_vs_iso_raise handler' — directly addressed, Experience pool RECENT_LESSONS: 'sizing_exploit_adjustment() returns max ±0.04 — Either abandon this axis or increase delta to ≥0.08' — deltas increased to 0.07/0.09 (partially addressed, 0.07 still below 0.08 threshold), Experience pool GENERAL: 'SB limp-then-face-raise misclassified as sb_vs_reraise — keep limp-raise and raise-reraise paths distinct' — fixed with new sb_first_action detection in opponent.py; Diff refs: opponent.py: Added opp_large_bet_count/opp_small_bet_count tracking (8BB threshold) + sizing_aggr metric using smooth_rate — new structural data input, opponent.py lines 225-234: New sb_first_action detection — checks if SB's first preflop action was 'call' (limp) to classify sb_vs_iso_raise vs sb_vs_reraise correctly, strategy.py lines 321-333: New sizing_exploit_adjustment() — over-bettors (sizing_aggr≥0.55) get -0.07*confidence delta, under-bettors (≤0.20) get +0.09*confidence
- **v23**: SB limp-iso-raise was misclassified as sb_vs_reraise for multiple gens — when evolving preflop handlers, explicitly audit whether spot labels match actual action sequences. Fixed with new sb_vs_iso_raise handler (pot-odds-aware 0.34 call threshold, 0.58+ limp-reraise).
- **v23**: sizing_exploit_adjustment() returns max ±0.04 — in the exhausted micro-adjustment zone. Either abandon this axis or increase delta to ≥0.08; focus on v18's weakest matchups (v13 46.2%, v14 50.0%, v20 48.8%) where river/turn barrel frequency matters more than sizing tweaks.
- **v23**: Opponent-model EQR adjustments (aggressive penalty, passive bonus, lower clamps 0.45→0.38, 0.65→0.55) + river thin value for all opponents + pot_odds_engine river gate. Critic 7.0. Key risk: lower EQR clamps overlap with exhausted fold-margin tuning; pot_odds river gate inserted BEFORE should_fold_postflop() may bypass guards. H2H samples still small (~340 games). Branch from v18 (not texture-gated v22 which regressed to WR 0.563).
- **v22**: Board texture classification added but regressed vs v18 (WR 0.563). Fold-path restructuring without H2H evidence of over-folding is risky — v18's fold gates were deliberate.
- **v21**: Gap Broadway limp (J4s+/Q3s+/K2s+/T5s+) and wider pressure clamp (-0.12, 0.15) — watch H2H vs v16.

