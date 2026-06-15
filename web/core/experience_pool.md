## OPPONENT_MODELING
- Use live continuous stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence gates; avoid contradictory multi-signal AND gates that make logic dead.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, `open_response_confidence`, pfr/vpip), not generic action confidence or unknown-profile assumptions; do NOT treat unknown opener as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates. Preflop hand-class threshold/range work is mature; verify residual raw-threshold 4-bet logic before adding more. [POSSIBLY EXHAUSTED]
- Do not confuse `value_profile['tier']` with a true opponent archetype; verify any claimed archetype/board-range primitive is live before planning around it.
- Validate opponent reads with sample counts, firing-rate/open-frequency logs, and >=100g H2H.

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation is saturated (≥12 return-True fold paths); add no new SPR/all-in/texture/pot-odds/opponent-stat/polarization/barrel fold gate without >=100g validation and a distinct decision point. [POSSIBLY EXHAUSTED]
- SPR logic is inlined into `should_fold_postflop` (spr>4.0 gate); `_spr_commitment_gate()` is a dead primitive (zero matches) — do not reference or tune it.
- Value paths should use selection/guard changes, not stacked sizing deltas: add high-aggression exclusions, nut/tier guards, and >=100g validation vs aggressive lineages. New value tiers must avoid overlap with earlier return guards.
- Audit action-selection paths for raw-ratio bypasses skipping `choose_raise`; dispatch-bypass fixes have produced real gains.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation.
- The live line module is `line_polarization_profile` (line_reading.py) + `facing_barrel_continuation` (strategy_helpers.py); `postflop_line_plan`/`postflop_line_sizing` were dropped in a rebase and do NOT exist — do not reference them.

## BLUFF_CALIBRATION
- Bluff only with opponent evidence: prefer high-fold OR low-aggression profiles with confidence; suppress bluffs against high-aggression / low-fold opponents.
- Structural bluff modules (`4-bet_light`, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H validation before being treated as successful or expanded.
- Contradictory behavior-signal AND gates become dead code; combine alternative opponent tendencies with OR logic and smaller magnitudes.
- `bluff_suppress` is absent in current bots; re-measure whether pot-odds grounding alone suffices before reintroducing sticky-caller bleed control.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds has no sustained gain; Tuner changes must attach constants to a structural hypothesis and per-constant H2H backing. [POSSIBLY EXHAUSTED]
- (Retired) `value_sizing_delta` opponent-stat boosts at `choose_raise` are fully absent from current source (0 matches); do not reintroduce stacked value sizing without matchup evidence of underbetting.
- Thin value-tier floors need >=100g calling-station-lineage validation before raising floors or widening tiers.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H validation before treating it as successful, repeating it, or expanding it; smaller samples (sub-15-game) are directional noise only.
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone.
- One mechanism per generation except sanctioned crossover diversity rescues.
- Worker boundaries: Tuner changes constants only when tied to structural rationale; Architect must not tune constants.
- Crossover skips direction_audit/master/workers but must run quality gates, review, critic, precommit eval, commit, and archivist; post-crossover correctness verification (`TOTAL_HANDS=70`, wheel straight, strict re-raise) is mandatory.
- Helper extraction is safe near the line cap; verify live primitives (`classify_preflop_hand`, `river_value_raise_tier`, `exploit_dispatch`, `_sb_open_bucket_action`, `_bb_vs_raise_bucket_action`) remain wired before/after rebases — confirm against current source, not stale lists.

## RECENT_LESSONS
- **v102**: Critic evidence: H2H weaknesses: v101 H2H: 0.4 WR vs v97/v96/v98/v82 (10g each, noise-level). Match analysis identifies avg flop raise 0.4x pot as underbet giving draws profitable odds — root cause traced to probe_mode capping thin-value hands.; Experience pool refs: PARAMETER_TUNING: 'Standalone constant/margin tuning has no sustained gain' — this change is control-flow/guard, not constant tuning. POSTFLOP_STRATEGY: 'Value paths should use selection/guard changes, not stacked sizing deltas' — this is a guard removal, aligning with guidance. RECENT_LESSONS v101: previous generation noted monitoring need for raise sizing distribution; this generation is the fix that makes sizing measurable.; Diff refs: strategy.py L1392: removed `(value_profile and value_profile['tier']=='thin' and board_texture and not board_texture['dynamic'])` from probe_mode OR chain. Previously this triggered probe_mode=True → choose_raise() capped ratio to 0.25+0.08*wetness (L241-249) and low_ratio=0.22 (L254). Now thin-value hands on static boards use base ratio 0.60/0.70/0.85 (L200-205) with low_ratio=0.40.
- **v102**: Critic evidence: H2H weaknesses: v101 H2H data shows 0.4-0.5% WR across all matchups (10g each) — likely data artifact, not usable as weakness targeting. Changes are driven by v101 self-assessment (inert probe cap) rather than specific H2H evidence.; Experience pool refs: v101 RECENT_LESSONS: 'Thin-value probe-bluff sizing cap removed... If signal proves inert vs v88/v100, raise cap to 0.15-0.20 or apply multiplicatively.' v102 instead removes the cap entirely. PARAMETER_TUNING: 'Standalone constant/margin tuning...has no sustained gain' — the strategy.py change is borderline parameter tuning but the passive_exploit restructure is structural.; Diff refs: strategy.py L1392: removed `(value_profile and value_profile['tier']=='thin' and board_texture and not board_texture['dynamic'])` from probe_mode OR chain, passive_exploit.py L20-27: split delayed_cbet into tiered unconditional paths (strong≥0.55, thin≥0.50) moved BEFORE confidence/passivity gates at L31-32, passive_exploit.py L23: strength threshold raised from 0.42→0.55 for unconditional path, dropping 0.42-0.50 range entirely
- **v101**: Thin-value probe-bluff sizing cap removed (strategy.py probe_mode): thin-value hands now use standard value ratio (0.60-0.85x) instead of ~0.25-0.33x cap. Structural control-flow/guard change, not constant tweak — aligns with PARAMETER_TUNING exhaustion. passive_exploit.py: 3-tier delayed c-bet (strong→unconditional, thin→confidence≥0.10, marginal→strict gate); second_barrel/river_thin_value paths retain strict confidence≥0.25/passivity≥0.60 gates. Needs >=100g validation.
- **v101**: Precommit volatility across 4 rounds (8-1-1, 24-40, 8-1-1, 21-19) for a 0.06-capped nudge indicates mirror-battle noise dominates at small n_games — increase n_games or require cross-run consistency for small-magnitude changes. If signal proves inert vs v88/v100, raise cap to 0.15-0.20 or apply multiplicatively to call_margin.
- **v101**: Synthetic critic score 0.0 from invalid JSON wasted a full critic round (681.8s) — validate output parsing before propagating as a rejection signal.
- **v101**: `facing_barrel_continuation()` barrel fold signal (opp_prior_postflop_raise_count detection; barrel_freq≥0.55*1.15 / postflop_aggr≥0.50*0.7), wired into call_margin stack. Based on 20g H2H weakness; crosses noise floor but needs >=100g validation. New fold gate in already-saturated accumulation — validate before expanding.


