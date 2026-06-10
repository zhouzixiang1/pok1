## OPPONENT_MODELING
- v22+ resolves the earlier aggressive-opponent weakness (v12 73.7% @460g, v11 62.4% @380g); do not target stale v24-lineage aggressive-opponent claims.
- Bet-size pattern classification and pattern_exploit_adjustment improved Critic confidence but showed no measurable H2H gain through v27. [POSSIBLY EXHAUSTED]
- Opponent-pressure clamps, sizing-tendency deltas, and barrel/sizing modulation are exhausted tuning variants with no measurable H2H effect through v27. [POSSIBLY EXHAUSTED]
- Per-street big-bet tracking with smooth_rate priors is useful as input data, but should not become a direct fold gate.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; new exceptions or overrides need explicit confidence, pot-odds, and realized-equity validation.
- River fold logic must be bet-size-aware: unconditional river folding, especially versus small bets, is exploitable.
- Overlapping fold gates with close thresholds create redundancy; prefer unified threshold tables or priority-ordered gates.
- Draw-call margins must be grounded in equity vs pot odds and protected by has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds before activation.

## BLUFF_CALIBRATION
- Avoid adding light 4-bet or bluff-expansion mechanisms from stale weakness claims; require ≥100-game H2H backing before targeting a matchup.
- Bluff/barrel modulation via tiny parameter deltas has not produced measurable gains and should not be repeated without a structural exploit hypothesis. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Base postflop sizing ratios are stable: flop 0.60, turn 0.70, river 0.85; tune structural decision logic before retuning these.
- Preflop 3bet threshold around 0.60 (TT+, AKs) is solid; never call off 100BB with only ~51% equity versus over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, and similar threshold tuning have repeatedly failed to produce measurable gains through v27. [POSSIBLY EXHAUSTED]
- Base sizing_aggr is useful for opponent-aware sizing, but small opponent-aware delta tweaks are exhausted. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries are critical: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist, otherwise tags/version tracking break.
- Trust early negative Critic signals; first-rejection scores are often more reliable than retry approvals.
- H2H weakness data below 100 games is directional only; require ≥100-game confirmation before using it as an evolution target.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect before declaring success.
- Single-file crossover is low-risk only when combining genuinely new structural features.
- v15/v18-lineage recombination shows diminishing returns; future crossovers need genuinely new structural ingredients. [POSSIBLY EXHAUSTED]
- Pure parameter tweaks without a validated structural hypothesis waste generations.

## RECENT_LESSONS
- **v27**: Barrel modulation, tiny-bet protection, and light 4-bet logic did not address true post-v18 weak matchups; stale weakness data misdirected work, so require ≥100-game backing before acting.
- **v26**: Bet-size opponent-modeling recovered Critic score but later showed no measurable H2H gain; Critic approval alone is not battle-performance proof.
- **v25**: Bet-size-aware river fold gating is acceptable only when integrated through should_fold_postflop() with pot-odds/realized-equity safeguards; aggressive-opponent weakness claims from v24 are superseded.
