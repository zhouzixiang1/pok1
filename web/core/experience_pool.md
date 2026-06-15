## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq), per-street fold_to_bet/call-down, and passivity_score are live; apply confidence gating (`confidence>=0.25`) as reliability gating before acting on reads — not as a multi-signal AND requirement.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates.
- Five primary preflop spots use `classify_preflop_hand()` buckets; residual `_should_4bet_light()` still uses raw thresholds — migrate before further 4-bet work.
- sb_open opponent adaptation (v97) uses fold_to_open_preflop / threebet_vs_open; validate >=100g vs v47/v48/v50 before widening.
- No live archetype classifier exists; do not confuse `value_profile['tier']` with opponent archetype.

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation (SPR, all-in texture, pot-odds, opp-stat, polarization) is saturated; add no new fold gate without >=100g evidence and a distinct decision point. [POSSIBLY EXHAUSTED]
- `_spr_commitment_gate()` before `must_continue_vs_raise` is the existing successful structural pattern — descriptive of past wins, not a license to keep adding SPR-style fold gates; avoid `should_fold_postflop` threshold tuning. [POSSIBLY EXHAUSTED]
- Pot-odds grounding is mandatory for large-fold decisions; folds based only on bet-size polarization or board fear risk chip-bleed/over-fold.
- Value paths need high-aggression exclusions or nut/tier guards; monitor barrel/value-bet branches vs aggressive lineages at >=100g.
- New value tiers must avoid overlap with earlier return guards; exclude handled bands or move guards to prevent dead code.
- Audit action-selection paths for raw-ratio bypasses skipping `choose_raise`; dispatch-bypass fixes have produced real gains.

## BLUFF_CALIBRATION
- Do not bluff high-aggression / low-fold opponents; bluff only vs low-aggression / high-fold profiles with confidence.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H support before targeting a matchup.
- Contradictory behavior-signal AND gates become dead code; keep confidence gating, but combine alternative opponent tendencies with OR logic and smaller magnitudes.
- Re-measure whether pot-odds grounding alone controls sticky-caller bleed before reintroducing `bluff_suppress`.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds has no sustained gain; constants require structural rationale plus per-constant H2H backing. [POSSIBLY EXHAUSTED]
- Opponent-stat-driven value sizing boosts at `choose_raise` have repeated without confirmed underbetting evidence; avoid stacking more sizing deltas. [POSSIBLY EXHAUSTED]
- `bet_size_profile.py` overlaps exploit_dispatch/passive_exploit and contains a no-EV small-raise boost; monitor stacked value_sizing_delta bleed.
- Crossover-preservation checklist: keep durable primitives (`classify_preflop_hand`, river_value_raise_tier, fold_gates, exploit_dispatch) from being silently rebased away.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H; smaller samples are directional noise only.
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone.
- One mechanism per generation except sanctioned crossover diversity rescues.
- Worker boundaries: Tuner must change constants; Architect must not tune constants.
- Crossover skips direction_audit/master/workers but must run quality gates, review, critic, precommit eval, commit, and archivist.
- Post-crossover correctness verification is mandatory: TOTAL_HANDS=70, wheel straight, strict re-raise compliance.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation.
- Helper extraction is safe near the line cap; `fold_gates.py` is the template for strategy.py reduction.
- Still absent: board_range_filter and true archetype classifier.

## RECENT_LESSONS
- **v98**: Critic evidence: H2H weaknesses: No v98 H2H data yet in head_to_head.json or bot_stats.json; using parent v97 context., v97 weakest recorded matchup: vs v92, 2-8 over 10 games, 20.0% win rate., v97 also has 40.0% 10-game samples vs v41, v85, v96, v88, v25, v90, and v93., v97 overall bot_stats: 155 wins, 135 losses, 290 games, 53.45% win rate.; Experience pool refs: OPPONENT_MODELING: 'sb_open opponent adaptation (v97) uses fold_to_open_preflop / threebet_vs_open; validate >=100g vs v47/v48/v50 before widening.', RECENT_LESSONS v97: 'Validate SB-open adaptation by whether fold_to_open_preflop / threebet_vs_open measurably change open/limp frequency, not just static threshold review.', RECENT_LESSONS v97: 'Future SB-open widening must be EV-grounded — widen only with high BB fold equity; limp/call suited connectors only when 3-bet pressure + implied odds justify it.'; Diff refs: opponent.py: build_opponent_model now exposes open_response_samples and open_response_confidence = clamp((preflop_open_opp - 2) / 8.0), separating SB-open read confidence from generic total-action confidence., strategy.py:_sb_open_bucket_action now gates reads with open_response_confidence >= 0.25 and uses fold_to_open_preflop plus threebet_vs_open to classify high_fold_bb, pressure_bb, and sticky_bb., strategy.py:_sb_open_bucket_action preserves raises for ('premium', 'strong_pair', 'mid_pair', 'big_cards'), preventing obvious premium-hand regression.
- **v97**: Validate SB-open adaptation by whether fold_to_open_preflop / threebet_vs_open measurably change open/limp frequency, not just static threshold review (>=100g vs v47/v48/v50).
- **v97**: Future SB-open widening must be EV-grounded — widen only with high BB fold equity; limp/call suited connectors only when 3-bet pressure + implied odds justify it.
- **v97**: bet_size_profile stacks with exploit_dispatch/passive_exploit without proven matchup evidence; treat as risk, not a template for more sizing boosts.
- **v97**: bet-size polarization fold logic without pot-odds comparison repeats exhausted defensive-gate accumulation; do not extend without strong H2H proof. [POSSIBLY EXHAUSTED]
- **v96**: Relaxing contradictory AND gates to lower-magnitude OR gates can revive dead opponent-model logic, but requires bleed checks vs balanced opponents.
- **v96**: Thin value-tier floor extension needs >=100g CS-lineage validation before raising floors or widening tiers again.
- **v95**: Preflop saturation fix succeeded only after critic forced structural bucket rewrite; use critic local_optima_warning to pivot, not retune thresholds.

