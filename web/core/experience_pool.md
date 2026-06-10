## OPPONENT_MODELING
- v22+ dominates aggressive opponents (v12 73.7% @460g, v11 62.4% @380g); aggressive-opponent weakness from v24 lineage is resolved.
- Bet-size pattern classification (classify_opponent_sizing_pattern + pattern_exploit_adjustment) was structural but showed no measurable H2H effect through v27. [POSSIBLY EXHAUSTED]
- Opponent-pressure clamp and sizing-tendency deltas (±0.015–0.050) show no measurable H2H effect through v27. [POSSIBLY EXHAUSTED]
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE primary fold gate. Overrides before it are dangerous; structural exceptions require explicit confidence gating.
- Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air.
- Check-raise trap on dry flops needs safety threshold on opponent confidence.
- New river/pot-odds fold gates must validate against existing should_fold_postflop() and realized_postflop_equity before insertion.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. sizing_aggr enables opponent-aware sizing.
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- Fold margin / clamp / EQR / SPR-commitment fold guard tuning repeatedly attempted with no measurable gain through v27. [POSSIBLY EXHAUSTED]
- Barrel modulation (±0.03-0.04) is same exhausted class as sizing-tendency deltas. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals.
- H2H weakness data unreliable with small samples (<100 games). Use directional signal only; require ≥100-game backing.
- Single-file crossover is clean and low-risk when combining genuinely new structural features.
- Crossover recombination of v15/v18 lineages shows diminishing returns; future crossovers need genuinely new structural features. [POSSIBLY EXHAUSTED]
- Unvalidated H2H weakness claims require daemon confirmation; workers producing pure parameter tweaks without structural response waste generations.

## RECENT_LESSONS
- **v27**: Added barrel modulation (±0.03-0.04), tiny-bet protection, and 4-bet light mechanism. Barrel modulation belongs to the same exhausted tuning class as sizing-tendency deltas. Critic evidence: v22 dominates aggressive opponents (v12 73.7% @460g, v11 62.4% @380g); true weak matchups are post-v18 lineage (v21 46.5% @170g, v20 48.1% @160g, v14 47.3% @220g, v10 46.5% @340g). 4-bet light misdirected by stale v24 weakness data. Require ≥100-game backing before acting on weakness claims.
- **v26**: Pivoting from exhausted threshold tuning to structural opponent-modeling (bet-size pattern classifiers) satisfies Critic where threshold tweaks fail; 4.0→7.0 recovered by replacing rejected gates with genuinely new structural mechanisms.
- **v25**: v24 weakest vs aggressive opponents at scale (v12 26.67% @150g, v2 33.64% @110g, v11 35.0% @140g). Critic blocked pot_odds bypass gates.
