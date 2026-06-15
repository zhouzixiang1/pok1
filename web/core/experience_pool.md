## OPPONENT_MODELING
- Use live continuous stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence gates; avoid contradictory multi-signal AND gates that make logic dead.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, `open_response_confidence`, pfr/vpip), not generic action confidence or unknown-profile assumptions; do NOT treat unknown opener as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates. preflop hand-class threshold/range work is now mature; verify residual raw-threshold 4-bet logic before adding more. [POSSIBLY EXHAUSTED]
- Do not confuse `value_profile['tier']` with a true opponent archetype; verify any claimed archetype/board-range primitive is live before planning around it.
- SB-open opponent-response reads must use `open_response_confidence`; validate with sample counts, firing-rate/open-frequency logs, and >=100g H2H.

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation is saturated; add no new SPR/all-in/texture/pot-odds/opponent-stat/polarization fold gate without >=100g validation and a distinct decision point. [POSSIBLY EXHAUSTED]
- SPR logic is inlined into `should_fold_postflop` (spr>4.0 gate); the old `_spr_commitment_gate()` module name is not a live primitive — avoid `should_fold_postflop` threshold tuning. [POSSIBLY EXHAUSTED]
- Value paths should use selection/guard changes, not stacked sizing deltas: add high-aggression exclusions, nut/tier guards, and >=100g validation vs aggressive lineages. New value tiers must avoid overlap with earlier return guards.
- Audit action-selection paths for raw-ratio bypasses skipping `choose_raise`; dispatch-bypass fixes have produced real gains.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation.

## BLUFF_CALIBRATION
- Bluff only with opponent evidence: prefer high-fold OR low-aggression profiles with confidence; suppress bluffs against high-aggression / low-fold opponents.
- Structural bluff modules (`4-bet_light`, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H validation before being treated as successful or expanded.
- Contradictory behavior-signal AND gates become dead code; combine alternative opponent tendencies with OR logic and smaller magnitudes.
- `bluff_suppress` is absent in current bots; re-measure whether pot-odds grounding alone suffices before reintroducing sticky-caller bleed control.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds has no sustained gain; Tuner changes must attach constants to a structural hypothesis and per-constant H2H backing. [POSSIBLY EXHAUSTED]
- Opponent-stat-driven value sizing boosts at `choose_raise` have repeated without confirmed underbetting evidence; stacked `value_sizing_delta` is risk until matchup evidence proves value. [POSSIBLY EXHAUSTED]
- Thin value-tier floors need >=100g calling-station-lineage validation before raising floors or widening tiers.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H validation before treating it as successful, repeating it, or expanding it; smaller samples (sub-15-game) are directional noise only.
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone.
- One mechanism per generation except sanctioned crossover diversity rescues.
- Worker boundaries: Tuner changes constants only when tied to structural rationale; Architect must not tune constants.
- Crossover skips direction_audit/master/workers but must run quality gates, review, critic, precommit eval, commit, and archivist; post-crossover correctness verification (`TOTAL_HANDS=70`, wheel straight, strict re-raise) is mandatory.
- Helper extraction is safe near the line cap; verify wired durable primitives (`classify_preflop_hand`, `river_value_raise_tier`, `exploit_dispatch`, `_sb_open_bucket_action`, `_bb_vs_raise_bucket_action`, `postflop_line_plan`, `postflop_line_sizing`) remain live before and after rebases/crossovers.

## RECENT_LESSONS
- **v101**: Precommit volatility across 4 rounds (8-1-1, 24-40, 8-1-1, 21-19) for a bot with only a 0.06-capped nudge suggests mirror battle noise dominates at small n_games — increase n_games or require consistency across runs for small-magnitude changes.
- **v101**: Synthetic critic score 0.0 from invalid JSON output wasted a full critic round (681.8s) and required regression guardian intervention — output parsing should validate before propagating as a rejection signal.
- **v101 归档建议**: If v101's 0.06 barrel-continuation signal proves inert against v88 and v100 (track fold-to-turn-barrel and call_margin distribution), the next generation should either raise the cap to 0.15-0.20 or apply the signal multiplicatively to call_margin rather than as an additive nudge.
- **v101**: New `facing_barrel_continuation()` barrel fold signal (opp_prior_postflop_raise_count detection, opponent_model barrel_freq≥0.55 *1.15 / postflop_aggr≥0.50 *0.7), wired into call_margin stack. Based on 20g H2H weakness evidence (40% vs v79/v89/v93/v98); crosses sub-15g noise floor but requires >=100g validation before expansion. Addresses prior critic feedback on barrel over-calling.
- **v101**: New `postflop_lines.py` — `postflop_line_plan()` (delayed_cbet, second_barrel, river_give_up, planned_river_value) and `postflop_line_sizing()` (fixed ratios per line). Line-selection hypothesis is directionally defensible, but fixed-ratio sizing IS the exhausted standalone sizing-tuning direction — must earn >=100g H2H proof before assuming success. [POSSIBLY EXHAUSTED]
- **v100**: `_bb_vs_raise_bucket_action()` BB-vs-SB-open bucket split. Present in v101 working tree (strategy.py:391-417). Uses `tight_opener = (not has_read) or (...)` — **active risk**: violates "do NOT treat unknown opener as tight by default" pool lesson. If evolving, must switch to pfr/vpip/open-response evidence and validate suited-ace/suited-connector bluff 3-bets over >=100g H2H. [POSSIBLY EXHAUSTED]
- **v99**: `_sb_open_bucket_action()` separates implied hands from playable marginals. Present in v101 working tree (strategy.py:356-388). Unvalidated; isolate open/limp/fold + fold-to-3bet outcomes vs disciplined BB defenders over >=100g H2H before expanding. [POSSIBLY EXHAUSTED]

