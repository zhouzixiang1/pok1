## OPPONENT_MODELING
- Use live opponent stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence/sample gates (≥30g); sub-30g matchups are directional noise only — do not record them as actionable weaknesses. OR-combine tendencies with modest magnitudes.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, pfr/vpip), not generic action confidence; never classify unknown openers as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates.
- Do not confuse `value_profile['tier']` with opponent archetype; verify claimed archetype/board-range primitives exist and are live before planning around them.

## POSTFLOP_STRATEGY
- DEFENSIVE late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated; add no new defensive guard unless it targets a distinct decision point and has ≥100g validation. [POSSIBLY EXHAUSTED]
- `facing_barrel_continuation` is now LIVE (strategy.py import+call site, def in strategy_helpers.py:272). Its status remains unresolved: neither v108 removal nor v109 re-add has ≥30g paired net-chips backing on the targeted board textures. Before next change, run ≥30g paired-texture net-chips comparison; if a removal variant leaks, use a pot-odds-grounded variant (cap 0.10-0.12), not the exhausted flat nudge. [POSSIBLY EXHAUSTED]
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation and verify reachability/fire-rate. Re-introducing a byte-identical prior attempt without new rationale is the same trap (generalized from v110≈v91).
- Confirm named primitives exist in current source before referencing them; docstrings, memories, stale planning notes, and previously live helper names are not definitions.
- Audit action-selection paths for raw-ratio bypasses, skipped `choose_raise`, downstream caps, dispatch-order shadowing, and overlapping handler order before modifying behavior.
- Verify trap-guard exclusion lists after any `_should_checkraise_trap` refactor; dropping value/bluff exclusions can suppress intended value sizing on overlapping tiers.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence and confidence; low aggression/passivity alone may indicate calling-station behavior.
- Exhaustion applies to DEFENSIVE guards only. New offensive bluff/value paths remain permitted when backed by firing-rate logs and ≥100g H2H.
- Structural bluff modules require current-source live-path verification before being treated as successful or expanded.

## PARAMETER_TUNING
- DEFENSIVE sizing constant tuning (caps/floors/defensive call thresholds) has no sustained gain; must be constants-only inside an Architect-defined structural hypothesis with per-constant H2H backing. Offensive sizing floors/tiers are NOT exhausted and remain permitted. [POSSIBLY EXHAUSTED]
- Exclude new defensive sizing-tier/floor/cap increases from Tuner work unless current source proves dispatch order, downstream caps, and target live path are not the blocker.
- Sizing floors that sit BELOW existing base ratios have narrow firing windows (base 0.60/0.70/0.85 means floor 0.50/0.55/0.60 only catches extreme downward-stacking); verify the floor actually binds before treating it as impactful.
- Do not reintroduce stacked value-sizing boosts such as `value_sizing_delta` at `choose_raise` unless current source and matchup evidence prove underbetting.

## GENERAL
- Any new structural path, constant change, or matchup target requires ≥100g H2H validation before treating it as successful, repeating it, or expanding it; <30g H2H (e.g. v105/v106 trended negative, v110's 10g matchups) is directional noise, not a weakness to act on.
- Treat commit messages as advisory; trust the git diff (v107 claimed a thin-value probe_mode mutation that was byte-identical to v102).
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone; verify the crossover tool actually executed rather than falling back to master+worker copy.
- Verify branch_from logic considers current top-rated bots, not just stagnation ancestor (v107 branched from v102 when v106 was available).
- Worker boundaries are mandatory: Architect defines structural logic; Tuner may only adjust constants within that structure, not create new logic.
- Re-verify strategy.py line count each generation rather than trusting a version-pinned figure; core limit is 2000, so bundle refactors only when source nears cap.
- Attribution test pattern: when isolating a donor trait into a base, pair candidate H2H vs opponents where donor>base by ≥3pp; if candidate doesn't recover ≥half the gap, the trait was not the edge source.
- Orphan dead-code trap: worker removes import+call site but the function def lives in a non-target file. Master/Reviewer should expand target_files OR add a post-commit cleanup gate that auto-strips orphaned defs.

## RECENT_LESSONS
- **v110**: Critic evidence: H2H weaknesses: v109 win rate 0.564 (330g) — healthy; cited '-20071/-16825/-15680 river tail' has no traceable source in H2H or replay data. Weakest matchups (v107 0.30 g=10, v79/v95/v106 0.40 g=10) are n=10 directional noise per the experience pool rule.; Experience pool refs: 'DEFENSIVE late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated; add no new defensive guard...' [POSSIBLY EXHAUSTED], RECENT_LESSONS v110: 'sizing-floor change is functionally identical to v91... Re-introducing a prior identical attempt without new rationale risks the dead-code/repeat trap.', 'Any new structural path... requires ≥100g H2H validation before treating it as successful, repeating it, or expanding it' — v91's gate was dropped without ≥100g validation either way.; Diff refs: strategy.py:697-774 NEW _spr_commitment_gate — byte-identical to v91's def at the same line ranges (verified via git show bot-v91); only GATE 2 elevated-risk cap 0.45→0.40 differs., strategy.py:1004-1015 call site inserted BEFORE call_margin logic in to_call>0 block; correctly bypasses nuts/draws but P1 pot-odds check absent., Master plan claim 'cut catastrophic river all-in tail' has no H2H/replay provenance — replay scan finds 0 hands with delta < -10000 across 33 v109 replays.
- **v110**: sizing-floor change is functionally identical to v91 (same tiers/exclusions/thresholds 0.50/0.55/0.60, cosmetic var-name diff); floor correctly inserted before clamp so it binds when low_ratio is raised, but narrow firing window means modest expected impact. Re-introducing a prior identical attempt without new rationale risks the dead-code/repeat trap.
- **v109**: Re-adding an 'exhausted'-tagged feature requires ≥30g paired net-chips validation BEFORE re-add; standalone constant tuning on it repeats the v107 no-H2H-basis pattern. Barrel-continuation re-add + reraise-guard(call MORE) pull OPPOSITE → cancellation risk.
- **v109**: When porting a guard from a confirmed-regression lineage (v103 1235→1155), require trace evidence that the SPECIFIC guard was not the regressing component — otherwise the port inherits unknown regression risk.
- **v108/v109 attribution probes** (vs v92 broadway wr<0.49, vs v93<0.53 falsification) remain unresolved — orthogonal to v110 sizing-floor work.

