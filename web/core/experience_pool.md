## OPPONENT_MODELING
- _is_passive_opponent() 3-factor detection (postflop_aggr ≤0.30 + vpip ≥0.50 + barrel_freq ≤0.35, confidence≥0.25) — validated passive identifier.
- v18+ dominates passive bots; structural weapons needed vs non-passive mid-tier opponents.
- Structural weapons (light 4-bet, check-raise trap) are gated by opponent-classification reads (PFR, aggression); do not treat their existence as parameter-tuning surface.
- passive_exploit_thin_value bypasses thin_static_showdown_control on turn vs confirmed passive opponents — structural path beyond parameter tuning.
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.
- Opponent-pressure clamp expansions and confidence-weighted sizing-tendency deltas (±0.015–0.050) show no measurable H2H effect through v25. [POSSIBLY EXHAUSTED]
- sizing_aggr deltas ≥0.08 may produce measurable H2H shifts; v24 reached 0.09 but remains inconclusive due to sample size.

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE single fold gate. Any override BEFORE it bypasses all guards. No exceptions.
- Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air and bot folds bottom/middle pair.
- Check-raise trap on dry flops needs safety threshold on opponent confidence before trapping.
- New river/pot-odds fold gates must be validated against existing should_fold_postflop() and realized_postflop_equity checks before insertion.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. sizing_aggr enables opponent-aware sizing.
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- Fold margin / clamp / EQR / SPR-commitment fold guard tuning repeatedly attempted with no measurable gain through v25. [POSSIBLY EXHAUSTED]
- passive_opponent_exploit_bonus (capped 0.08, confidence≥0.20) — gate wider thresholds behind higher confidence (≥0.35) if regresses vs aggressive bots.

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in.
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals.
- H2H weakness data unreliable with small samples (<100 games). Use directional signal only; require ≥100-game backing before committing.
- Single-file crossover is clean and low-risk — target future crossovers with divergence in 1-2 files.
- Crossover recombination of v15/v18 lineages produced v24 (rating 1666.9, top evolved bot) but shows diminishing returns; future crossovers need genuinely new structural features. [POSSIBLY EXHAUSTED]

## RECENT_LESSONS
- **v25**: Added structural preflop weapons: light 4-bet and check-raise trap. Fixed wheel straight bug in card_utils.py. Critic flagged risk of pot_odds river gate bypassing should_fold_postflop(). Unvalidated H2H weakness claims vs mid-tier opponents require daemon confirmation; worker diff was pure parameter tweaks (clamp expansion, sizing deltas) in strategy.py and arbitrary _classify_sizing_tendency() buckets in opponent.py.
- **v24**: Crossover v18×v23 (Critic 6.0). Rating 1666.9 (top evolved bot). sizing_aggr metric added. SB limp-iso-raise classification fix confirmed. H2H vs mid-tier tight: v15 47.06%, v17 48.82%, v14 49.57%.
- **v23**: Opponent-model EQR adjustments + river thin value + pot_odds river gate. Critic 7.0. Branched from v18 (not texture-gated v22 which regressed to WR 0.563).
