## OPPONENT_MODELING
- Opponent-pressure clamps, sizing-tendency deltas, barrel/sizing modulation, and bet-size pattern classification are exhausted tuning variants with no measurable H2H gain through v27. [POSSIBLY EXHAUSTED]
- Per-street big-bet tracking with smooth_rate priors is useful as input data, but should not become a direct fold gate.
- Do not target aggressive-opponent weakness claims from pre-v22 bots; these are resolved.

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
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas, and similar threshold tuning have repeatedly failed to produce measurable gains through v27. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries are critical: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist, otherwise tags/version tracking break.
- Trust early negative Critic signals; first-rejection scores are often more reliable than retry approvals.
- H2H weakness data below 100 games is directional only; require ≥100-game confirmation before using it as an evolution target.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect before declaring success.
- Single-file crossover is low-risk only when combining genuinely new structural features.
- v15/v18-lineage recombination shows diminishing returns; pure parameter tweaks without a validated structural hypothesis waste generations. [POSSIBLY EXHAUSTED]

## RECENT_LESSONS
- **v29**: Critic evidence: H2H weaknesses: v28 weakest matchups: v12 (20% WR, 20 games), v11 (30% WR, 20 games), v27/v14/v24/v2 (40% WR). All below the 100-game confidence threshold cited in experience pool.; Experience pool refs: v28 lesson: 'overbet, donk/probe, 4-bet light modules carry promise but need battle validation' — this generation tests the overbet expansion., EXHAUSTED warning: 'parameter tuning via threshold deltas has repeatedly failed' — but this is structural (new code path), not constant tuning., Warning: 'Structural changes can inflate Critic scores without improving battle performance' — must verify via mirror battles.; Diff refs: overbet.py: New strong-tier path (lines 212-249) fires when nut-tier overbet_risk_check fails. Imports evaluate_best from card_utils (hand_class >= 3 = trips/straight/flush/full house/quads/straight flush). 8 new constants with tighter bounds than nut-tier. Properly guards against trips on paired boards and hyper-aggressive opponents.
- **v28**: v27 structural modules (overbet, donk/probe, 4-bet light) carry promise but v27 itself regressed to 1486.8 — modules need battle validation in v28's integrated context. v28 also carries the min_raise_action re-raise baseline fix (2*last_raise_to + 1).
- **v28**: Crossover mutation adds size_bucket gating to river fold gates (prevents folding to small blocking bets) + mathematically grounded pot_odds_call_threshold() + 276-line overbet.py module. Monitor v28 vs v22 (current #1 at 1614.9) to validate crossover success.
- **v27**: Barrel modulation, tiny-bet protection, and light 4-bet logic did not address true weak matchups; stale weakness data misdirected work. Reinforces ≥100-game backing requirement.
- **v26**: Bet-size opponent-modeling recovered Critic score but showed no measurable H2H gain; Critic approval alone is not battle-performance proof.

