## OPPONENT_MODELING
- Use live continuous stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence gates; avoid contradictory multi-signal AND gates that make logic dead.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, `open_response_confidence`, pfr/vpip), not generic action confidence; do NOT treat unknown opener as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates. Preflop hand-class threshold/range work is mature; verify residual raw-threshold 4-bet logic before adding more. [POSSIBLY EXHAUSTED]
- Do not confuse `value_profile['tier']` with a true opponent archetype; verify any claimed archetype/board-range primitive is live before planning around it.
- Validate opponent reads with sample counts, firing-rate/open-frequency logs, and >=100g H2H; 10g samples are directional noise only.

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation is saturated (≥12 return-True fold paths); add no new SPR/all-in/texture/pot-odds/opponent-stat/polarization/barrel fold gate without >=100g validation and a distinct decision point. [POSSIBLY EXHAUSTED]
- SPR logic is inlined into `should_fold_postflop` (spr>4.0 gate); `_spr_commitment_gate()` is a dead primitive (zero matches) — do not reference or tune it.
- Value paths should prefer selection/guard changes over stacked sizing deltas — BUT a deliberate sizing-floor raise (e.g. probe_mode thin-value fix lifting 0.25x→0.60-0.85x) is acceptable when removing a downstream cap that bled value; validate both the floor change and any residual caps (`thin_cap`, `low_ratio`) in >=100g before treating as successful.
- Audit action-selection paths for raw-ratio bypasses skipping `choose_raise`; dispatch-bypass fixes have produced real gains.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation.
- The live line module is `line_polarization_profile` (line_reading.py) + `facing_barrel_continuation` (strategy_helpers.py); `postflop_line_plan`/`postflop_line_sizing`/`_spr_commitment_gate` were dropped in rebases and do NOT exist.

## BLUFF_CALIBRATION
- Bluff only with opponent evidence: prefer high-fold OR low-aggression profiles with confidence; suppress bluffs against high-aggression / low-fold opponents.
- Structural bluff modules (`4-bet_light`, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H validation before being treated as successful or expanded.
- Contradictory behavior-signal AND gates become dead code; combine alternative opponent tendencies with OR logic and smaller magnitudes.
- `bluff_suppress` is absent in current bots; re-measure whether pot-odds grounding alone suffices before reintroducing sticky-caller bleed control.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds has no sustained gain; Tuner changes must attach constants to a structural hypothesis and per-constant H2H backing. [POSSIBLY EXHAUSTED]
- Do not reintroduce stacked value-sizing boosts (`value_sizing_delta`) at `choose_raise` — absent from current source and counterproductive without matchup evidence of underbetting.
- Thin value-tier floors need >=100g calling-station-lineage validation before raising floors or widening tiers.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H validation before treating it as successful, repeating it, or expanding it; smaller samples (sub-15-game) are directional noise only.
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone.
- One mechanism per generation except sanctioned crossover diversity rescues.
- Worker boundaries: Tuner changes constants only when tied to structural rationale; Architect must not tune constants.
- Crossover skips direction_audit/master/workers but must run quality gates, review, critic, precommit eval, commit, and archivist; post-crossover correctness verification (`TOTAL_HANDS=70`, wheel straight, strict re-raise) is mandatory.
- Helper extraction is safe near the line cap; verify live primitives (`classify_preflop_hand`, `river_value_raise_tier`, `exploit_dispatch`, `_sb_open_bucket_action`, `_bb_vs_raise_bucket_action`) remain wired before/after rebases — confirm against current source, not stale lists.

## RECENT_LESSONS
- **v103**: DEAD-CODE TRAP re-occurred: first critic found guard placed AFTER repeated_raise_trap (unreachable), worker fixed by moving it BEFORE. Future workers must verify new guards are placed BEFORE any existing return-producing block that shares the same trigger condition.
- **v103**: Critic advisory: _single_reraise_stackoff_guard ignores draw_strength — combo draws (draw_strength≥0.25, made_strength≥0.40) forced to call instead of semi-bluff raising in low-SPR. Future gen should add a carve-out.
- **v103 归档建议**: At ≥100g, validate the guard fires against v89 (the only well-evaluated top bot at r=1202/rd=76) by checking whether turn/river all-in frequency with sub-nut hands drops — if unchanged, opp_current_round_bet_count may not increment on opponent raises in opponent.py.
- **v103**: Critic evidence: H2H weaknesses: v102 vs v90: 40% WR (10g) — v90 is the SPR commitment gate bot, directly relevant to stack-off behavior. v102 vs v91: 40% WR (10g) — v91 is the value-tier sizing floor bot. Both are 10g noise-level samples and cannot serve as confirmed targeting evidence.; Experience pool refs: POSTFLOP_STRATEGY: 'Defensive fold-gate accumulation is saturated (≥12 return-True fold paths)' — this is a new CALL gate (return 0), not a fold gate, so it does not violate the saturation warning., GENERAL: 'Any new structural path requires >=100g H2H validation' — the guard needs >=100g validation before being treated as confirmed successful., POSTFLOP_STRATEGY: 'Detection-without-handler is recurring dead code' — this guard IS wired (line 1117-1121), consuming the detection in the action selection path.; Diff refs: strategy.py:655-681 — _single_reraise_stackoff_guard(): fires for round_idx>=2, opp_bet_count==1, facing_postflop_aggression=True, tier!='nut', spr<3.0, made_strength<0.70 → return 0 (call), strategy.py:1114-1121 — wired AFTER trap_nut_slowplay check, BEFORE the raise decision at line 1128. This is the correct insertion point: it prevents the bot from reaching choose_raise() with sub-nut hands vs a single re-raise in committed territory., strategy.py:781-784 — repeated_raise_trap requires opp_current_round_bet_count>=2; the new guard targets ==1, confirming these are distinct decision points (previous critic's 'unreachable' claim was incorrect).
- **v103**: Critic evidence: H2H weaknesses: No H2H evidence cited. No specific opponent matchup where AA/sub-nut stack-off is bleeding chips was identified. v102 has no H2H data yet (0 games in H2H matrix; v103 has 0 games).; Experience pool refs: POSTFLOP_STRATEGY: 'Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation.', POSTFLOP_STRATEGY: '_spr_commitment_gate() is a dead primitive (zero matches) — do not reference or tune it.' This guard risks becoming the same., GENERAL: 'Any new structural path, constant change, or matchup target requires >=100g H2H validation before treating it as successful.'; Diff refs: strategy.py:1055-1059 — `repeated_raise_trap` block already returns 0 or -1 for non-nut hands facing opp_current_round_bet_count >= 2 with medium/large sizing, strategy.py:1064-1069 — new `_reraise_stackoff_guard` placed AFTER the trap block; unreachable because trap already returned, opponent.py:315-318 — `opp_round_raises` (line 316) and `opp_current_round_bet_count` (line 318) increment on the same condition on turn/river; the two triggers are equivalent
- **v101→v102**: probe_mode sizing fix — removed thin-value + static-board from probe_mode OR chain (strategy.py), so thin-value hands now use base ratio 0.60/0.70/0.85x with low_ratio=0.40 instead of the ~0.25-0.33x cap that gave opponents 5:1 to chase draws. Structural guard removal, not constant tuning. Track flop/turn avg raise size vs v101 in next 100+g; if thin-value hands still fire ≤0.35x, the fix may be inert via downstream caps (`thin_cap`, `low_ratio` clamps).
- **v102 passive_exploit**: delayed_c-bet split into tiered unconditional paths (strong≥0.55, thin≥0.50) moved BEFORE confidence/passivity gates; strength threshold 0.42→0.55 (dropped 0.42-0.50 range). second_barrel/river_thin_value retain strict gates. Needs >=100g validation.
- **v101**: `facing_barrel_continuation()` barrel fold signal (barrel_freq≥0.55*1.15 / postflop_aggr≥0.50*0.7), wired into call_margin stack. New fold gate in already-saturated accumulation — validate >=100g before expanding.
- **v101 precommit volatility**: 4 rounds (8-1-1, 24-40, 8-1-1, 21-19) for a 0.06-capped nudge shows mirror-battle noise dominates at small n_games — increase n_games or require cross-run consistency for small-magnitude changes.
- **v101**: Critic returned synthetic 0.0 from invalid JSON, wasting a 681.8s round — validate output parsing before propagating as a rejection signal.
- **v101/v102 evidence caveat**: v101 H2H weaknesses drawn from 10g samples against old lineages (v97/v96/v98/v82) are noise-level and not usable as targeting; changes were driven by self-assessment (inert probe cap), not specific H2H evidence.



