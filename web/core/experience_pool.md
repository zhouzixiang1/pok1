## OPPONENT_MODELING
- v18+ dominates passive bots but struggles vs non-passive profiles; structural weapons must be gated by opponent-classification reads — not a parameter-tuning surface.
- Per-street big-bet tracking with smooth_rate priors is data input, not fold gate.
- Opponent-pressure clamp and sizing-tendency deltas (±0.015–0.050) show no measurable H2H effect through v27. [POSSIBLY EXHAUSTED]
- sizing_aggr deltas ≥0.08 inconclusive since v24 without validation through v27. [POSSIBLY EXHAUSTED]
- classify_opponent_sizing_pattern() + pattern_exploit_adjustment() (bet-size pattern classification, bluff_catch_boost) — structural but unvalidated through v27; needs daemon H2H confirmation vs aggressive opponents (v12 most cited target). [POSSIBLY EXHAUSTED]

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

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals.
- H2H weakness data unreliable with small samples (<100 games). Use directional signal only; require ≥100-game backing.
- Single-file crossover is clean and low-risk when combining genuinely new structural features.
- Crossover recombination of v15/v18 lineages shows diminishing returns; future crossovers need genuinely new structural features. [POSSIBLY EXHAUSTED]
- Unvalidated H2H weakness claims require daemon confirmation; workers producing pure parameter tweaks without structural response waste generations.

## RECENT_LESSONS
- **v27**: Critic evidence: H2H weaknesses: v22 weakest matchups: v25 (45.0%, 60g), v21 (46.5%, 170g), v20 (48.1%, 160g) — all post-v18 evolution lineage, NOT wide 3-bettors. v22 already dominates aggressive opponents: v12 (73.7%, 460g), v11 (62.4%, 380g). Light 4-bet targets a non-weakness.; Experience pool refs: 'Opponent-pressure clamp and sizing-tendency deltas show no measurable H2H effect through v27. [POSSIBLY EXHAUSTED]', 'classify_opponent_sizing_pattern() + pattern_exploit_adjustment() ... structural but unvalidated through v27; needs daemon H2H confirmation vs aggressive opponents. [POSSIBLY EXHAUSTED]', 'Crossover recombination of v15/v18 lineages shows diminishing returns; future crossovers need genuinely new structural features. [POSSIBLY EXHAUSTED]'; Diff refs: strategy.py:635-711 — `_is_fourbet_light_candidate()` + `_should_4bet_light()`: identical candidate selection to v25 crossover (22-44 pairs, 45s-JTs connectors, A2s-A5s), 2.5x sizing, 40% frequency, strategy.py:714-754 — `_should_checkraise_trap()`: strong/nut hands on dry flops vs aggressive opponents, 25% activation, does NOT use v22's 5-tier board_texture classifier, strategy.py:572-575 — 4-bet light call inserted in sb_vs_reraise handler after premium-value and strong-hand calls
- **v27**: Critic evidence: H2H weaknesses: v22 weakest H2H: v14 at 47.3% (220g), v10 at 46.5% (340g). v22 overall 58.18% win rate (5210 games)., 4-bet light targets wide 3-bettors — confirmed weakness pattern from experience pool: v24 weakest vs aggressive opponents (v12 26.67%, v2 33.64%, v11 35.0%).; Experience pool refs: EXPERIENCE_POOL: 'Opponent-pressure clamp and sizing-tendency deltas (±0.015–0.050) show no measurable H2H effect through v27. [POSSIBLY EXHAUSTED]' — barrel modulation (±0.03-0.04) is this same class., EXPERIENCE_POOL: 'Unconditional river fold (including small bets) is exploitable' — tiny-bet protection directly addresses this., EXPERIENCE_POOL: 'Fold margin / clamp / EQR / SPR-commitment fold guard tuning repeatedly attempted with no measurable gain through v27. [POSSIBLY EXHAUSTED]'; Diff refs: strategy.py:464-506 — _is_fourbet_light_candidate() + _should_4bet_light(): new 4-bet light mechanism with hand selection, opponent data gates, frequency cap, stack safety., strategy.py:594-599 — 4-bet light invoked in sb_vs_reraise spot, before premium 4-bet/call logic., strategy.py:629-667 — should_fold_postflop() gains opponent_model param, barrel modulation (±0.03-0.04), tiny-bet protection (pot ratio ≤ 0.20).
- **v26**: Pivoting from [POSSIBLY EXHAUSTED] tuning to structural opponent-modeling (bet-size pattern classifiers) satisfies Critic where threshold tweaks fail; 4.0→7.0 recovered by replacing rejected gates with genuinely new structural mechanisms.
- **v26**: classify_opponent_sizing_pattern() detects over_bluff (large_rate>0.55 AND postflop_aggr>0.42); pattern_exploit_adjustment() applies bluff_catch_boost (+0.05 over_bluff, +0.04 polarized, -0.03 merged). Unvalidated — needs daemon H2H data. [POSSIBLY EXHAUSTED]
- **v25**: v24 weakest vs aggressive opponents at scale (v12 26.67% @150g, v2 33.64% @110g, v11 35.0% @140g). Critic blocked pot_odds bypass gates.


