## OPPONENT_MODELING
- _is_passive_opponent() 3-factor detection (postflop_aggr ≤0.30 + vpip ≥0.50 + barrel_freq ≤0.35, confidence≥0.25) — validated passive identifier.
- v18+ dominates passive bots; structural weapons needed vs non-passive mid-tier opponents.
- Structural weapons (light 4-bet, check-raise trap) are gated by opponent-classification reads (PFR, aggression); do not treat their existence as parameter-tuning surface.
- passive_exploit_thin_value bypasses thin_static_showdown_control on turn vs confirmed passive opponents — structural path beyond parameter tuning.
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.
- Opponent-pressure clamp expansions and confidence-weighted sizing-tendency deltas (±0.015–0.050) show no measurable H2H effect through v25. [POSSIBLY EXHAUSTED]
- sizing_aggr deltas ≥0.08 may produce measurable H2H shifts; v24 reached 0.09 but remains inconclusive due to sample size.

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE primary fold gate. Overrides before it are dangerous and must be validated against existing equity checks; structural exceptions (e.g., passive_exploit_thin_value) require explicit confidence gating.
- Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air and bot folds bottom/middle pair.
- Check-raise trap on dry flops needs safety threshold on opponent confidence before trapping.
- New river/pot-odds fold gates must be validated against existing should_fold_postflop() and realized_postflop_equity checks before insertion.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. sizing_aggr enables opponent-aware sizing.
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- Fold margin / clamp / EQR / SPR-commitment fold guard tuning repeatedly attempted with no measurable gain through v25. [POSSIBLY EXHAUSTED]
- passive_opponent_exploit_bonus (capped 0.08, confidence≥0.20) — gate wider thresholds behind higher confidence (≥0.35) if regresses vs aggressive bots.

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals.
- H2H weakness data unreliable with small samples (<100 games). Use directional signal only; require ≥100-game backing before committing.
- Single-file crossover is clean and low-risk when combining genuinely new structural features; target divergence in 1-2 files.
- Crossover recombination of v15/v18 lineages produced v24 (rating 1666.9, top evolved bot) but shows diminishing returns; future crossovers need genuinely new structural features. [POSSIBLY EXHAUSTED]

## RECENT_LESSONS
- **v25**: Critic evidence: H2H weaknesses: v24 vs v10: 40.77% win rate over 130 games — v10 is the weakest significant matchup for v24, confirming aggressive opponents exploit v24's river folds., v24 vs v16: 43.33% over 60 games — secondary weakness in the same direction., Pre-commit mirror failure cited in context: 0-3-0 sweep vs v10 with n=3, indicating the prior over-fold change was too permissive and got exploited.; Experience pool refs: POSTFLOP_STRATEGY: 'Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air and bot folds bottom/middle pair.' — direct match for the problem being fixed., PARAMETER_TUNING: 'Fold margin / clamp / EQR / SPR-commitment fold guard tuning repeatedly attempted with no measurable gain through v25. [POSSIBLY EXHAUSTED]' — but the experience pool explicitly notes this change is 'gate-structure cleanup, NOT parameter tuning', placing it outside the exhausted pattern., RECENT_LESSONS/v25: 'Critic flagged risk of pot_odds river gate bypassing should_fold_postflop()' — establishes precedent that river fold gates need size/pot-odds awareness, which this change provides.; Diff refs: strategy.py:615 — added `and size_bucket in ("medium", "large")` to the existing `made_strength < 0.40` river fold gate, restricting it to larger bets., strategy.py:617 — new guard `made_strength < 0.25 and not has_draw and opp_bets >= 2 and size_bucket == "small"` folds only the very bottom of the range to small multi-barrel bets., strategy.py:636-638 (v24) removed — deleted the unconditional `made_strength < 0.20` block, which is now redundant because the new < 0.25 small-bet guard covers the same territory.
- **v25**: Critic evidence: H2H weaknesses: v24 vs v12: 26.67% (150 games) — older bot may exploit over-folding, v24 vs v11: 35.0% (140 games) — same pattern, v24 vs v2: 33.64% (110 games), v24 vs v14: 43.75% (80 games), v15: 46.25% (80 games) — closer matchups where small river over-folds likely cost EV, v24 overall win rate 56.97% with only 1,780 games — sample is still thin but direction is clear: weakest vs older over-folding victims; Experience pool refs: POSTFLOP_STRATEGY: 'Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air and bot folds bottom/middle pair.' — direct match for removed 0.20 block, PARAMETER_TUNING: 'Fold margin / clamp / EQR / SPR-commitment fold guard tuning repeatedly attempted with no measurable gain through v25. [POSSIBLY EXHAUSTED]' — this change is NOT parameter tuning; it is gate-structure cleanup, RECENT_LESSONS/v25: 'Critic flagged risk of pot_odds river gate bypassing should_fold_postflop()' — relevant precedent that river fold gates need pot-odds/bet-size awareness; Diff refs: strategy.py:615 — added `and size_bucket in ("medium", "large")` to `made_strength < 0.40 and not has_draw and opp_bets >= 2` river fold gate, strategy.py:636-638 (v24) removed — deleted unconditional `round_idx == 3 and made_strength < 0.20 and not has_draw and opp_bets >= 2` fold block
- **v25**: Added structural preflop weapons: light 4-bet and check-raise trap. Fixed wheel straight bug in card_utils.py. Critic flagged risk of pot_odds river gate bypassing should_fold_postflop(). Unvalidated H2H weakness claims vs mid-tier opponents require daemon confirmation; worker diff was pure parameter tweaks (clamp expansion, sizing deltas) in strategy.py and arbitrary _classify_sizing_tendency() buckets in opponent.py.
- **v24**: Crossover v18×v23 (Critic 6.0). Rating 1666.9 (top evolved bot). sizing_aggr metric added. SB limp-iso-raise classification fix confirmed. H2H vs mid-tier tight: v15 47.06%, v17 48.82%, v14 49.57%.
- **v23**: Opponent-model EQR adjustments + river thin value + pot_odds river gate. Critic 7.0. Branched from v18 (not texture-gated v22 which regressed to WR 0.563).


