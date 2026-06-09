# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]
- Light 4-bet and check-raise trap need structural reads (PFR + aggression), not threshold micro-adjustments.
- v18+ dominates passive bots (v3/v4/v7 WR 0.62–0.70). Weakest matchups are mid-tier non-passive: v12 (0.321), v11 (0.390), v10 (0.417), v13 (0.462), v14/v16/v20 (~0.48–0.50). Prioritize exploitative adjustments vs these close-range opponents.
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.
- passive_exploit_thin_value bypasses thin_static_showdown_control on turn vs confirmed passive opponents (7 guards) — structural path beyond exhausted fold-margin tuning.
- _is_passive_opponent() 3-factor detection (postflop_aggr ≤ 0.30 + vpip ≥ 0.50 + barrel_freq ≤ 0.35, confidence ≥ 0.25) — well-grounded passive identifier.
- CBet/exploitation micro-adjustments max ~0.015 effect; sizing_aggr metric (v24) now provides structural data — re-evaluate if larger deltas (≥0.08) produce measurable H2H shifts.

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE single fold gate. Any override BEFORE it bypasses all guards. No exceptions.
- Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air and bot folds bottom/middle pair.
- Board texture classification (5-tier) is a high-value structural axis — combine texture with SPR/opponent-model rather than replace. [POSSIBLY EXHAUSTED]
- Check-raise trap on dry flops needs safety threshold on opponent confidence before trapping.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. sizing_aggr (v24) now enables opponent-aware sizing — deltas 0.07/0.09 still below the ≥0.08 threshold for reliable impact.
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- Fold margin / clamp value / EQR lower-bound tuning repeatedly attempted with no measurable gain — v23 re-attempted EQR clamps (0.45→0.38, 0.65→0.55) and confirmed exhaustion. [POSSIBLY EXHAUSTED]
- passive_opponent_exploit_bonus (capped 0.08, confidence≥0.20) — gate wider thresholds behind higher confidence (≥0.35) if regresses vs aggressive bots.
- SPR/commitment-based fold guards [POSSIBLY EXHAUSTED] — repeated tuning with no measurable gain.
- Crossover recombination of v15/v18 lineages attempted through v24 (v18×v23), none beat v15. Gene pool appears converged. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in.
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals.
- H2H weakness data unreliable with small samples (<100 games). Use directional signal only for hypothesis generation; require ≥100-game backing before committing to targeted changes.
- Single-file crossover is clean and low-risk — target future crossovers with divergence in 1-2 files.
- New river/pot-odds fold gates must be validated against existing should_fold_postflop() and realized_postflop_equity checks before insertion — avoid inserting simpler gates upstream of sophisticated ones.

## RECENT_LESSONS
- **v24**: Crossover v18×v23 validated (Critic 6.0). H2H: v18 vs v15 47.06% (170 games), v18 vs v17 48.82% (170), v18 vs v14 49.57% (230) — tight mid-tier matchups where sizing exploitation or river barrel adjustments could shift margins. sizing_aggr metric added (sizing_exploit_adjustment deltas 0.07/0.09 — still below ≥0.08 actionable threshold). SB limp-iso-raise classification fix confirmed working.
- **v23**: Opponent-model EQR adjustments + river thin value + pot_odds river gate. Critic 7.0. Key risk acknowledged: pot_odds river gate inserted BEFORE should_fold_postflop() may bypass guards — verify in next gen. Branch from v18 (not texture-gated v22 which regressed to WR 0.563).
- **v22**: Board texture classification added but regressed vs v18 (WR 0.563). Fold-path restructuring without H2H evidence of over-folding is risky — v18's fold gates were deliberate.
