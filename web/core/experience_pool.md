## OPPONENT_MODELING
- Use live continuous stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence gates; avoid contradictory multi-signal AND gates that make logic dead.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, `open_response_confidence`, pfr/vpip), not generic action confidence; do NOT treat unknown opener as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates. Preflop hand-class threshold/range work is mature; verify residual raw-threshold 4-bet logic before adding more. [POSSIBLY EXHAUSTED]
- Do not confuse `value_profile['tier']` with a true opponent archetype; verify any claimed archetype/board-range primitive is live before planning around it.
- Validate opponent reads with sample counts, firing-rate/open-frequency logs, and >=100g H2H; sub-15-game samples are directional noise only — never cite them as evidence of regression.

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation is saturated (>=12 return-True fold paths); add no new SPR/all-in/texture/pot-odds/opponent-stat/polarization/barrel fold gate without >=100g validation and a distinct decision point. [POSSIBLY EXHAUSTED]
- Confirm ANY named primitive exists by reading current source before referencing; several previously-named fold/commitment primitives (`_spr_commitment_gate`, `_allin_board_texture_fold`, v103's `_single_reraise_stackoff_guard`) are GONE — dropped in rebases. Docstring names are not definitions. The live line module is `line_polarization_profile` (line_reading.py) + `facing_barrel_continuation` (strategy_helpers.py).
- Sizing changes need a structural hypothesis: adding a NEW board-texture-dependent dimension (e.g. wetness-scaled `induce_cap`/`thin_cap`) counts as structural IF it materially changes the decision surface. Validate the floor change AND residual downstream caps (`thin_cap`, `low_ratio`) in >=100g before treating as successful.
- Audit action-selection paths for raw-ratio bypasses skipping `choose_raise`; dispatch-bypass fixes have produced real gains.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation.

## BLUFF_CALIBRATION
- Bluff only with opponent evidence: prefer high-fold OR low-aggression profiles with confidence; suppress bluffs against high-aggression / low-fold opponents.
- Structural bluff modules (`4-bet_light`, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H validation before being treated as successful or expanded.
- Contradictory behavior-signal AND gates become dead code; combine alternative opponent tendencies with OR logic and smaller magnitudes.
- `bluff_suppress` is absent in current bots; re-measure whether pot-odds grounding alone suffices before reintroducing sticky-caller bleed control.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds has no sustained gain; Tuner changes must attach constants to a structural hypothesis with per-constant H2H backing. The wetness-scaled-cap `induce_cap`/`thin_cap` work is the permitted EXCEPTION (NEW texture-conditional dimension = structural), but still requires >=100g proof it moved the metric. [POSSIBLY EXHAUSTED]
- Do not reintroduce stacked value-sizing boosts (`value_sizing_delta`) at `choose_raise` — absent from current source and counterproductive without matchup evidence of underbetting.
- Thin value-tier floors need >=100g calling-station-lineage validation before raising floors or widening tiers.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H validation before treating as successful, repeating it, or expanding it; sub-15-game samples are directional noise only.
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone.
- One mechanism per generation except sanctioned crossover diversity rescues.
- Worker boundaries: Tuner changes constants only when tied to structural rationale; Architect must not tune constants.
- Crossover skips direction_audit/master/workers but must run quality gates, review, critic, precommit eval, commit, and archivist; post-crossover correctness verification (`TOTAL_HANDS=70`, wheel straight, strict re-raise) is mandatory.
- Helper extraction is safe near the line cap; verify live primitives remain wired before/after rebases — confirm against current source, not stale lists.

## RECENT_LESSONS
- **v105**: Critic evidence: H2H weaknesses: v105 has 0 H2H games yet. No confirmed matchup data — this is a structural hypothesis driven by code analysis, not match data. Parent v102 H2H: vs v13 63.3% (30g), vs v100 45% (20g), vs v101 45% (20g).; Experience pool refs: v105 RECENT_LESSONS: '_dry_board_value_barrel() fires BEFORE choose_raise dispatch, bypassing induce_cap — targets the confirmed nut-hand sizing cap leak on dry boards', PARAMETER_TUNING: 'Sizing changes need a structural hypothesis: adding a NEW board-texture-dependent dimension counts as structural IF it materially changes the decision surface' — _dry_board_value_barrel IS a new structural dimension (separate function, not constant tuning), POSTFLOP_STRATEGY: 'Defensive fold-gate accumulation is saturated [POSSIBLY EXHAUSTED]' — this change is OFFENSIVE, not defensive, so does not fall in the exhausted pattern; Diff refs: strategy.py:655-735 NEW _dry_board_value_barrel() with trap-preservation guard (lines 688-695) — addresses previous critic feedback, strategy.py:1259-1274 induce_nut_value definition — the source of the induce_cap sizing leak being fixed, strategy.py:1453-1465 dispatch: trap check fires FIRST, then dry_barrel — correct ordering
- **v105 (committed, UNVALIDATED H2H)**: NEW `_dry_board_value_barrel()` (strategy.py:655-735, ~81 lines) fires BEFORE `choose_raise` dispatch, bypassing `induce_cap` — targets the confirmed nut-hand sizing cap leak on dry boards. Also NEW `_river_subpremium_commitment_penalty()` (lines 685-712) +0.06 buffer for river sub-premium (tier thin/none, strength 0.35-0.70); CRITICAL: buffer cap raised 0.14→0.30 blanket-wide (all tiers) — risk of blanket regression OR near-dead-code (existing value_heavy fold already covers sub-strong). UNRESOLVED: needs fire-rate trace to determine whether penalty is live or inert. passive_exploit.py thresholds lowered (passivity 0.60→0.50, confidence 0.25→0.20). NO >=100g H2H yet.
- **v104 (committed, UNVALIDATED)**: bot-v104 tag, commit 52e996b. Wetness-scaled sizing: `induce_cap` = 0.29 + 0.05*round_idx + 0.10*wetness (live formula); `thin_cap` 0.35/0.42 + 0.15*wetness; `low_ratio` 0.40 + 0.15*wetness. Definitive test of NEW-dimension sizing vs standalone-tuning ban. NO >=100g H2H yet (0 games, rating 1500/350). Next: run >=100g, track wet-board avg raise size — if unchanged, downstream `low_ratio`/`thin_cap` caps are absorbing the change.
- **v102 probe_mode fix (committed)**: removed thin-value + static-board from probe_mode OR chain; thin-value uses base 0.60/0.70/0.85x — the only confirmed successful offensive change. Delayed-cbet widening reverted twice (0%-fold bot amplified bleed) — do not widen c-bets while a stack-off bleed is open.
- **Mirror-noise discipline**: v101 needed 4 precommit rounds for a 0.06-capped nudge — mirror noise dominates at small n_games; increase n_games or require cross-run consistency. Critic returned synthetic 0.0 from invalid JSON (681.8s wasted) — validate output parsing before propagating as rejection.
- **v105 critic directive**: all v104/v105 H2H matchups are sub-30g samples = directional noise. No confirmed weakness in river all-in calling. Do NOT add more defensive guards on this basis; v103's `_single_reraise_stackoff_guard` is GONE (dropped in v105 rebase), so the CALL/cap-gate space is currently EMPTY and should not be re-filled without >=100g evidence.

