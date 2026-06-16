## OPPONENT_MODELING
- Use live opponent stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence/sample gates; OR-combine tendencies with modest magnitudes.
- Opponent modeling must target a distinct live decision point with firing-rate logs and >=100g H2H; sub-30g matchup samples are directional noise only.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, `open_response_confidence`, pfr/vpip), not generic action confidence; never classify unknown openers as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates.
- Do not confuse `value_profile['tier']` with opponent archetype; verify claimed archetype/board-range primitives exist and are live before planning around them.

## POSTFLOP_STRATEGY
- Defensive late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated; add no new guard unless it targets a distinct decision point and has >=100g validation. [POSSIBLY EXHAUSTED]
- Dry-board value-barrel work is permitted only as dispatch-order repair (e.g. moving a value handler ahead of an intercepting passive-exploit), not as new guard accumulation; treat such reorder as infrastructure-only until fire-rate/avg_raise/>=100g H2H confirm.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation and verify reachability/fire-rate.
- Confirm named primitives exist in current source before referencing them; docstrings, memories, stale planning notes, and previously live helper names are not definitions.
- Audit action-selection paths for raw-ratio bypasses, skipped `choose_raise`, downstream caps, dispatch-order shadowing, and overlapping handler order before modifying behavior.
- Near line cap, prioritize infrastructure-only dispatch/raise-decision table refactors over bundled behavior changes.
- Verify trap-guard exclusion lists after any `_should_checkraise_trap` refactor; dropping value/bluff exclusions can suppress intended value sizing on overlapping tiers.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence and confidence; low aggression/passivity alone may indicate calling-station behavior.
- Suppress bluffs against high-aggression or low-fold opponents unless a distinct live exploit path has firing-rate and >=100g H2H proof.
- Structural bluff modules require current-source live-path verification before being treated as successful or expanded.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios, caps, floors, and call thresholds has no sustained gain; Tuner changes must be constants-only inside an Architect-defined structural hypothesis with per-constant H2H backing. [POSSIBLY EXHAUSTED]
- Exclude new sizing-tier/floor/cap increases from Tuner work unless current source proves dispatch order, downstream caps, and target live path are not the blocker.
- Do not reintroduce stacked value-sizing boosts such as `value_sizing_delta` at `choose_raise` unless current source and matchup evidence prove underbetting.
- Thin value-tier floors or texture-conditional sizing constants need >=100g calling-station/archetype validation and current-source formula/cap verification before tuning.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H validation before treating it as successful, repeating it, or expanding it.
- v102 was later confirmed as a rating regression (1235→1155) and its offspring v105/v106 trended negative; do not anchor "offensive value-sizing success" on the v102 probe_mode fix — re-derive the success benchmark from current validated bots.
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone; verify the crossover tool actually executed rather than falling back to master+worker copy.
- Use one mechanism per generation except sanctioned crossover diversity rescues; helper extraction, line-cap relief, and dispatch refactors should be infrastructure-only generations.
- Worker boundaries are mandatory: Architect defines structural logic; Tuner may only adjust constants within that structure, not create new logic.
- Helper extraction is safe near the line cap only when it preserves behavior and verifies live primitives remain wired before/after rebases against current source.

## RECENT_LESSONS
- **v107**: Critic evidence: H2H weaknesses: v102 (win_rate 0.589, 1497W/1042L over 2540g) — strong overall but per memory v102 struggles vs v93/v95/v106 where v89 wins. No v107 H2H games yet (bot just created).; Experience pool refs: 'Standalone constant/margin tuning of sizing ratios, caps, floors, and call thresholds has no sustained gain' [POSSIBLY EXHAUSTED] — v107's barrel fold-gate changes (cap 0.06→0.07, deficit threshold 0.05→0.04) directly repeat this pattern., 'v104-v106 exhausted pattern: Incremental sizing cap/constant tuning ... repeated without confirmed H2H gain' — same violation here on the barrel fold cap.; Diff refs: strategy_helpers.py:87-90 — NEW broadway_suited bucket in classify_preflop_hand (structural, from v89)., strategy.py:369,403 — broadway_suited added to implied-odds list (sound wiring)., strategy_helpers.py:302-324 — barrel-continuation constants tuned with no H2H basis (exhausted pattern).
- **v106 dispatch-order repair**: Moving `_dry_board_value_barrel` ahead of `passive_exploit_trigger` is a valid infrastructure-only reorder (not guard accumulation); do not expand sizing until fire-rate, avg_raise, and >=100g H2H prove improvement.
- **v105/v106 unvalidated**: v105 trended negative vs v102, and v102 itself is a confirmed regression; both v105/v106 H2H remain sub-30g noise — do not treat them as a live strategic target or matchup-specific basis.
- **v104-v106 exhausted pattern**: Incremental sizing cap/constant tuning, value-underbet fixes without validated dispatch reachability, and late-street defensive all-in/fold guard accumulation repeated without confirmed H2H gain; avoid repeating unless tied to a distinct live path and >=100g validation. [POSSIBLY EXHAUSTED]

