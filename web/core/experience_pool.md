## OPPONENT_MODELING
- _is_passive_opponent() 3-factor detection (postflop_aggr ≤0.30 + vpip ≥0.50 + barrel_freq ≤0.35, confidence≥0.25) — validated passive identifier.
- v18+ dominates passive bots but struggles vs non-passive profiles (v24 <50% vs tight mid-tier; v25 ~30-40% vs aggressive); structural weapons (light 4-bet, check-raise trap) are needed but must be gated by opponent-classification reads (PFR, aggression) — not a parameter-tuning surface.
- passive_exploit_thin_value bypasses thin_static_showdown_control on turn vs confirmed passive opponents — structural path beyond parameter tuning.
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.
- Opponent-pressure clamp expansions and confidence-weighted sizing-tendency deltas (±0.015–0.050) show no measurable H2H effect through v25. [POSSIBLY EXHAUSTED]
- sizing_aggr deltas ≥0.08 may produce measurable H2H shifts; v24 reached 0.09 but remains inconclusive due to sample size.

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE primary fold gate. Overrides before it are dangerous and must be validated against existing equity checks; structural exceptions require explicit confidence gating.
- Overlapping fold gates with close thresholds create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- Unconditional river fold (including small bets) is exploitable — opponent can min-bet with air and bot folds bottom/middle pair.
- Check-raise trap on dry flops needs safety threshold on opponent confidence before trapping.
- New river/pot-odds fold gates must be validated against existing should_fold_postflop() and realized_postflop_equity checks before insertion; v25 critic flagged bypass risk when gates precede jam_odds/shove_odds.

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
- Avoid branching from regressed versions — v22 texture-gated system regressed to WR 0.563; v23 recovered by branching from stable v18.
- Unvalidated H2H weakness claims require daemon confirmation; v25 worker produced pure parameter tweaks without structural response.

## RECENT_LESSONS
- **v26**: Critic evidence: H2H weaknesses: claude_v12 vs claude_v25: 28% win rate (50 games) — worst matchup, claude_v11 vs claude_v25: 40% win rate (40 games), claude_v10 vs claude_v25: 42.5% win rate (40 games), claude_v2 vs claude_v25: 42.5% win rate (40 games); Experience pool refs: v25: v24 weakest vs aggressive opponents at scale (v12 26.67% @150g, v2 33.64% @110g, v11 35.0% @140g, v10 40.77% @130g), structural weapons (light 4-bet, check-raise trap) are needed but must be gated by opponent-classification reads (PFR, aggression) — not a parameter-tuning surface, Opponent-pressure clamp expansions and confidence-weighted sizing-tendency deltas (±0.015–0.050) show no measurable H2H effect through v25. [POSSIBLY EXHAUSTED]; Diff refs: opponent.py: new classify_opponent_sizing_pattern() with over_bluff detection (large_rate>0.55 AND postflop_aggr>0.42) targeting aggressive profiles, strategy.py: pattern_exploit_adjustment() gives +0.05 bluff_catch_boost vs over_bluff, +0.04 vs polarized large bets (lrr>0.75), and -0.03 vs merged, strategy.py: realized_rate fold check modified to (realized_rate + pattern_adj['bluff_catch_boost']) < pot_odds + call_margin — equity-grounded
- **v25**: v24 weakest vs aggressive opponents at scale (v12 26.67% @150g, v2 33.64% @110g, v11 35.0% @140g, v10 40.77% @130g). Critic blocked pot_odds bypass gates.
- **v24**: Crossover v18×v23, rating 1666.9. sizing_aggr metric added. Persistent weakness vs tight mid-tier (v15 47.06%, v17 48.82%, v14 49.57%).
- **v23**: Recovered from v22 regression (WR 0.563) by branching from stable v18; opponent-model EQR + river thin value + pot_odds gate. Critic 7.0.

