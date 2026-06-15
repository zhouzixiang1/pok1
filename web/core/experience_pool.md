## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq), per-street fold_to_bet/call-down, and passivity_score are live; keep confidence gating (`confidence>=0.25`) before acting on opponent reads.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates.
- Five primary preflop spots now use `classify_preflop_hand()` buckets; residual helper `_should_4bet_light()` still uses raw thresholds and should be migrated before further 4-bet work.
- sb_open opponent adaptation was restored in v97 via fold_to_open_preflop / threebet_vs_open; validate >=100g before widening further.
- No live archetype classifier exists; do not confuse `value_profile['tier']` with opponent archetype.

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation at get_action/fold paths (SPR, all-in texture, pot-odds, opp-stat, intermediate/polarization folds) is saturated; add no new fold gate without >=100g evidence and a distinct decision point. [POSSIBLY EXHAUSTED]
- `_spr_commitment_gate()` before `must_continue_vs_raise` is the valid structural insertion pattern; avoid `should_fold_postflop` threshold tuning. [POSSIBLY EXHAUSTED]
- Pot-odds grounding is mandatory for large-fold decisions; folds based only on bet-size polarization or board fear risk chip-bleed/over-fold.
- Value paths need high-aggression exclusions or nut/tier guards; monitor barrel/value-bet branches vs aggressive lineages at >=100g.
- New value tiers must avoid overlap with earlier return guards; exclude handled bands or move guards to prevent dead code.
- Audit action-selection paths for raw-ratio bypasses skipping `choose_raise`; dispatch bypass bugs have produced real gains when fixed.

## BLUFF_CALIBRATION
- Do not bluff high-aggression / low-fold opponents; bluff pressure belongs only vs low-aggression / high-fold profiles with confidence.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H support before targeting a matchup.
- Contradictory behavior-signal AND gates become dead code; keep confidence gates, but combine alternative opponent tendencies with OR logic and smaller magnitudes.
- Re-measure whether pot-odds grounding alone controls sticky-caller bleed before reintroducing `bluff_suppress`.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds has no sustained gain; constants require structural rationale plus per-constant H2H backing. [POSSIBLY EXHAUSTED]
- Opponent-stat-driven value sizing boosts at `choose_raise` call sites have repeated without confirmed H2H underbetting evidence; avoid stacking more sizing deltas. [POSSIBLY EXHAUSTED]
- `bet_size_profile.py` overlaps exploit_dispatch/passive_exploit and contains a no-EV small-raise boost; monitor stacked value_sizing_delta bleed before expanding it.
- Crossover-preservation checklist: keep durable primitives (`classify_preflop_hand`, river_value_raise_tier, fold_gates, exploit_dispatch) from being silently rebased away.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H; smaller samples are directional noise only.
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone.
- One mechanism per generation except sanctioned crossover diversity rescues.
- Worker boundaries: Tuner must change constants; Architect must not tune constants.
- Crossover skips direction_audit/master/workers but must run quality gates, review, critic, precommit eval, commit, and archivist.
- Post-crossover correctness verification is mandatory: TOTAL_HANDS=70, wheel straight, and strict re-raise compliance.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation.
- Helper extraction is safe near the line cap; `fold_gates.py` is the template for strategy.py reduction.
- Live v97 primitives: per-street opponent stats, passive_exploit, `_aligned_signal_boost`, EQR clamp, overbet, donk_probe, line_reading, bluff_heavy_call_widen, exploit_dispatch, river_value_raise_tier, classify_preflop_hand, SPR/all-in fold gates, value-tier sizing floor, bet_size_profile.
- Still absent: board_range_filter and true archetype classifier.

## RECENT_LESSONS
- **v97**: SB-open adaptations should be validated by whether fold_to_open_preflop and threebet_vs_open measurably change open/limp frequency, not just by static threshold review.
- **v97**: Future SB-open widening should be EV-grounded: widen only when BB fold equity is high enough, and limp/call suited connectors only when 3-bet pressure plus implied odds justify it.
- **v97 归档建议**: Run at least 100-game mirror samples versus v47/v48/v50 and inspect SB-preflop outcomes to tune the fold_to_open_preflop and threebet_vs_open thresholds.
- **v97**: sb_open now uses opponent immediate-BB response stats plus hand buckets; this addresses the v95 adaptation gap and should be validated before further preflop widening.
- **v97**: bet_size_profile stacks with exploit_dispatch/passive_exploit and lacks proven matchup evidence; treat as risk, not a template for more sizing boosts.
- **v97**: bet-size polarization fold logic without pot-odds comparison repeats exhausted defensive-gate accumulation; do not extend this direction without strong H2H proof. [POSSIBLY EXHAUSTED]
- **v96**: Relaxing contradictory AND gates to lower-magnitude OR gates can revive dead opponent-model logic, but requires bleed checks vs balanced opponents.
- **v96**: Thin value-tier floor extension needs >=100g CS-lineage validation before raising floors or widening tiers again.
- **v95**: Preflop saturation fix succeeded only after critic forced structural bucket rewrite; use critic local_optima_warning to pivot, not to retune thresholds.
- **v95**: Remaining preflop bucket concerns are KQo limp and suited-connector iso exclusions; validate range tightening before trusting these bucket edges.

