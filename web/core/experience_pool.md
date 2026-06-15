## OPPONENT_MODELING
- Continuous opponent stats are live (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity); use confidence gates before acting, but avoid contradictory multi-signal AND gates that make logic dead.
- SB-open adaptation now has dedicated open-response confidence (`open_response_samples`, `open_response_confidence`); validate changes by firing-rate/open-frequency shifts and >=100g H2H before widening.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for all preflop range gates.
- Five primary preflop spots use hand-class buckets; residual `_should_4bet_light()` still uses raw thresholds and should be migrated before more 4-bet work.
- No live archetype classifier exists; do not confuse `value_profile['tier']` with opponent archetype.
- Still absent: board_range_filter and true archetype classifier.

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation is saturated; add no new SPR/all-in/texture/pot-odds/opponent-stat/polarization fold gate without >=100g evidence and a distinct decision point. [POSSIBLY EXHAUSTED]
- `_spr_commitment_gate()` before `must_continue_vs_raise` is the existing successful structural pattern, not a license to keep adding SPR-style fold gates; avoid `should_fold_postflop` threshold tuning. [POSSIBLY EXHAUSTED]
- Pot-odds grounding is mandatory for large-fold decisions; folds based only on bet-size polarization or board fear risk over-folding.
- Value paths should use selection/guard changes, not stacked sizing deltas: add high-aggression exclusions, nut/tier guards, and >=100g validation vs aggressive lineages.
- New value tiers must avoid overlap with earlier return guards; exclude handled bands or move guards to prevent dead code.
- Audit action-selection paths for raw-ratio bypasses skipping `choose_raise`; dispatch-bypass fixes have produced real gains.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation.

## BLUFF_CALIBRATION
- Bluff only with opponent evidence: prefer high-fold OR low-aggression profiles with confidence; suppress bluffs against high-aggression / low-fold opponents.
- Structural bluff modules (`4-bet_light`, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H support before targeting a matchup.
- Contradictory behavior-signal AND gates become dead code; combine alternative opponent tendencies with OR logic and smaller magnitudes.
- Re-measure whether pot-odds grounding alone controls sticky-caller bleed before reintroducing `bluff_suppress`.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds has no sustained gain; constants require structural rationale plus per-constant H2H backing. [POSSIBLY EXHAUSTED]
- Opponent-stat-driven value sizing boosts at `choose_raise` have repeated without confirmed underbetting evidence; avoid stacking more sizing deltas. [POSSIBLY EXHAUSTED]
- `bet_size_profile.py` overlaps exploit_dispatch/passive_exploit and contains a no-EV small-raise boost; treat stacked value_sizing_delta as risk until matchup evidence proves value.
- Thin value-tier floors need >=100g calling-station-lineage validation before raising floors or widening tiers again.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H; smaller samples are directional noise only.
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone.
- One mechanism per generation except sanctioned crossover diversity rescues.
- Worker boundaries: Tuner must change constants; Architect must not tune constants.
- Crossover skips direction_audit/master/workers but must run quality gates, review, critic, precommit eval, commit, and archivist.
- Post-crossover correctness verification is mandatory: `TOTAL_HANDS=70`, wheel straight, strict re-raise compliance.
- Helper extraction is safe near the line cap; `fold_gates.py` is the template for `strategy.py` reduction.
- Crossover-preservation checklist: keep durable primitives (`classify_preflop_hand`, `river_value_raise_tier`, `fold_gates`, `exploit_dispatch`) from being silently rebased away.

## RECENT_LESSONS
- **v99**: Do not treat v99's SB-open bucket split as validated until >=100-game H2H samples show whether marginal/implied-odds hands gain EV versus disciplined BB defenders.
- **v99 归档建议**: Next validation should isolate SB open/limp/fold and fold-to-3bet outcomes versus v87, v29, and v88-style disciplined BB defenders.
- **v99**: Critic evidence: H2H weaknesses: Parent claude_v98 has a weak low-sample matchup vs claude_v87: claude_v87 vs claude_v98 is 7-3, so v98 win rate is 30%., Master also targeted borderline weak 10-game matchups vs claude_v29 and claude_v88, each implying v98 at 40%., Overall parent claude_v98 stats are 95-75 over 170 games, win_rate 55.88%, so this is not a global collapse but a matchup-specific refinement.; Experience pool refs: OPPONENT_MODELING: SB-open adaptation has dedicated open-response confidence and should use confidence gates., RECENT_LESSONS v98: preserve premium/strong/mid-pair/big-card raises while experimenting with open-response confidence., RECENT_LESSONS v97: future SB-open widening must be EV-grounded — widen only with high BB fold equity; limp/call suited connectors only when 3-bet pressure plus implied odds justify it.; Diff refs: bots/claude_v99/strategy.py::_sb_open_bucket_action now separates implied hands ('small_pair', 'suited_ace', 'suited_connector') from generic 'playable' marginal hands., Premium, strong_pair, mid_pair, and big_cards still return 'raise', preserving strong preflop value range., Against high-fold BB, implied and marginal hands raise; against pressure/sticky BB, implied hands call while generic playable hands fold; trash raises only against high-fold BB and otherwise folds.
- **v98**: SB-open opponent-response reads must be validated with open-response sample counts, firing-rate/open-frequency logs, and >=100g H2H before treating fold_to_open/threebet gates as proven.
- **v98**: Dedicated `open_response_confidence` is better than generic total-action confidence for SB-open adaptation; preserve premium/strong/mid-pair/big-card raises while experimenting.
- **v97**: Future SB-open widening must be EV-grounded — widen only with high BB fold equity; limp/call suited connectors only when 3-bet pressure plus implied odds justify it.
- **v97**: `bet_size_profile` stacks with exploit_dispatch/passive_exploit without proven matchup evidence; treat as risk, not a template for more sizing boosts.
- **v97**: Bet-size polarization fold logic without pot-odds comparison repeats exhausted defensive-gate accumulation; do not extend without strong H2H proof. [POSSIBLY EXHAUSTED]
- **v96**: Relaxing contradictory AND gates to lower-magnitude OR gates can revive dead opponent-model logic, but requires bleed checks vs balanced opponents.


