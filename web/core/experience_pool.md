## OPPONENT_MODELING
- Use live continuous stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence/sample gates; avoid contradictory multi-signal AND gates that make logic dead.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, `open_response_confidence`, pfr/vpip), not generic action confidence; do NOT treat unknown opener as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates, and audit any residual raw-threshold 4-bet logic before more hand-class work. [POSSIBLY EXHAUSTED]
- Do not confuse `value_profile['tier']` with a true opponent archetype; verify any claimed archetype/board-range primitive is live before planning around it.
- Validate opponent reads with sample counts, firing-rate/open-frequency logs, and >=100g H2H; sub-30g samples are directional noise only.

## POSTFLOP_STRATEGY
- Defensive fold/all-in guard accumulation is saturated; add no new SPR/all-in/texture/pot-odds/opponent-stat/polarization/barrel fold gate without >=100g validation and a distinct decision point. [POSSIBLY EXHAUSTED]
- Confirm any named primitive exists in current source before referencing it; docstrings, memories, stale planning notes, and previously live helper names are not definitions.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation and verify reachability/fire-rate.
- Audit action-selection paths for raw-ratio bypasses, skipped `choose_raise`, and dispatch-order shadowing; dispatch-bypass/order fixes have produced real gains.
- Sizing changes need a structural hypothesis: verify current live formulas and downstream caps/branches before adding board-texture or value-extraction logic, then validate with H2H.

## BLUFF_CALIBRATION
- Bluff only with opponent evidence: prefer high-fold OR low-aggression profiles with confidence; suppress bluffs against high-aggression / low-fold opponents.
- Structural bluff modules (`4-bet_light`, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H validation before being treated as successful or expanded.
- Contradictory behavior-signal AND gates become dead code; combine alternative opponent tendencies with OR logic and smaller magnitudes.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds has no sustained gain; Tuner changes must adjust constants inside an Architect-defined structural hypothesis with per-constant H2H backing. [POSSIBLY EXHAUSTED]
- Do not reintroduce stacked value-sizing boosts (`value_sizing_delta`) at `choose_raise` — absent from current source and counterproductive without matchup evidence of underbetting.
- Thin value-tier floors need >=100g calling-station-lineage validation before raising floors or widening tiers.
- Before adding or citing texture-conditional sizing constants, verify the live formula and downstream caps in current source; stale v104 wetness-scaling rationale is only a negative caution.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H validation before treating as successful, repeating it, or expanding it; sub-30g mirror samples and critic claims are directional only.
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone.
- Use one mechanism per generation except sanctioned crossover diversity rescues; line-cap/dispatch refactors should be infrastructure-only or their own generation.
- Worker boundaries: Architect defines structural logic; Tuner may only adjust constants within that structure, not create new logic.
- Crossover skips direction_audit/master/workers but must still run quality gates, review, critic, precommit eval, commit, and archivist; verify `TOTAL_HANDS=70`, wheel straight, and strict re-raise semantics afterward.
- Helper extraction is safe near the line cap; verify live primitives remain wired before/after rebases and confirm against current source, not stale lists.
- `strategy.py` near the 1500-line cap needs prioritized dispatch/raise-decision table refactor before more sizing work, to expose shadowing and prevent inert additions.

## RECENT_LESSONS
- **v106**: Critic evidence: H2H weaknesses: claude_v105 overall bot_stats: 223 wins / 147 losses / 370 games, win_rate 0.6027., Only current weak H2H below 40% is claude_v105 vs claude_v94 at 30.0%, but this is just 10 games and experience_pool warns v104-v106 current-bot H2H is sub-30g directional noise, not validated., claude_v102 vs claude_v105 is 20 games with v105_wr 50.0%, so the v105 dry-barrel change has not yet shown a validated parent improvement.; Experience pool refs: POSTFLOP_STRATEGY: 'Audit action-selection paths for raw-ratio bypasses, skipped choose_raise, and dispatch-order shadowing; dispatch-bypass/order fixes have produced real gains.', RECENT_LESSONS v105: '_dry_board_value_barrel() targeted dry-board strong/nut sizing but was shadowed by earlier passive_exploit, making the turn path likely inert vs passive opponents; do not expand dry-barrel work until dispatch order and H2H movement are proven.', RECENT_LESSONS v106 pending/unvalidated: 'Claimed fix moves dry-board value barrel before passive_exploit; diff suggests ordering improved and overbet/donk/probe priority preserved, but verify actual commit/tag, source order, fire-rate, and >=100g H2H before treating it as completed.'; Diff refs: bots/claude_v106/strategy.py lines 1329-1369 preserve overbet, donk, and probe priority before the dry-barrel path., bots/claude_v106/strategy.py lines 1371-1381 now compute sizing_delta/exploit_dispatch and call _dry_board_value_barrel before passive_exploit_trigger at lines 1383-1396., The diff removes the old _dry_board_value_barrel call from inside the later generic choose_raise branch after passive_exploit, eliminating the shadowing order that made dry-barrel inert in passive-station spots.
- **v106 pending/unvalidated**: Claimed fix moves dry-board value barrel before `passive_exploit`; diff suggests ordering improved and overbet/donk/probe priority preserved, but verify actual commit/tag, source order, fire-rate, and >=100g H2H before treating it as completed.
- **v105**: `_dry_board_value_barrel()` targeted dry-board strong/nut sizing but was shadowed by earlier `passive_exploit`, making the turn path likely inert vs passive opponents; do not expand dry-barrel work until dispatch order and H2H movement are proven.
- **v105**: `_river_subpremium_commitment_penalty()` and the 0.14→0.30 all-tier buffer-cap raise are verified gone from v105 source; ignore planning premised on that fold-buffer regression unless reintroduced with >=100g evidence.
- **v105**: Archivist showed v105 trending negative vs v102 parent; v102 `probe_mode` fix remains the only confirmed offensive value-sizing success, while v105 dry-barrel remains unvalidated.
- **v104**: Wetness-scaled `induce_cap`/`thin_cap`/`low_ratio` rationale is stale vs current source; re-verify live formulas before any texture-sizing plan.
- **v104-v106**: No current-bot H2H is validated and all cited weak matchups are sub-30g directional noise; do not infer matchup-specific river/all-in/calling weakness without larger samples.
- **v104-v106 pattern**: Incremental sizing cap/constant tuning and late-street sub-premium all-in/raise defensive guards have repeated without confirmed H2H gain; avoid repeating them unless tied to a distinct live path and >=100g validation. [POSSIBLY EXHAUSTED]

