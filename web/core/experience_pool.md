## OPPONENT_MODELING
- Use live opponent stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence/sample gates (≥30g); sub-30g matchups are directional noise only. OR-combine tendencies with modest magnitudes.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, pfr/vpip), not generic action confidence; never classify unknown openers as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates.
- Do not confuse `value_profile['tier']` with opponent archetype; verify claimed archetype/board-range primitives exist and are live before planning around them.

## POSTFLOP_STRATEGY
- DEFENSIVE late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated; add no new defensive guard unless it targets a distinct decision point and has ≥100g validation. [POSSIBLY EXHAUSTED]
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation and verify reachability/fire-rate.
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
- Do not reintroduce stacked value-sizing boosts such as `value_sizing_delta` at `choose_raise` unless current source and matchup evidence prove underbetting.

## GENERAL
- Any new structural path, constant change, or matchup target requires ≥100g H2H validation before treating it as successful, repeating it, or expanding it.
- Treat commit messages as advisory; trust the git diff (v107 claimed a thin-value probe_mode mutation that was byte-identical to v102).
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone; verify the crossover tool actually executed rather than falling back to master+worker copy.
- Verify branch_from logic considers current top-rated bots, not just stagnation ancestor (v107 branched from v102 when v106 was available).
- Worker boundaries are mandatory: Architect defines structural logic; Tuner may only adjust constants within that structure, not create new logic.
- strategy.py is at 1404 lines (v108), under the 2000-line core limit — line-cap pressure is not currently a binding constraint; bundle refactors only when source nears cap.

## RECENT_LESSONS
- **v109**: Critic evidence: H2H weaknesses: v108 weakest: vs v87 wr=0.300 (10g), vs v77/v14/v102/v95/v101 wr=0.400 (10g each, sub-30g = directional only per experience pool line 1), v108 overall wr=0.526 over 230 games — at plateau; weakest matchups are all 10g samples (no ≥30g confirmation), v103 (guard donor) was a CONFIRMED REGRESSION: rating 1235→1155, H2H .604→.537 per memory v103-tracking; Experience pool refs: Line 8: 'DEFENSIVE late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated' [POSSIBLY EXHAUSTED] — v109 adds a new defensive guard AND re-adds barrel-continuation, Line 36: 'Before locking facing_barrel_continuation removal as permanent, run a ≥30g paired net-chips comparison of v108 vs v101 on paired/highly-connected boards' — v109 re-adds it WITHOUT this evidence, Line 37: v107 barrel-continuation constants tuned with no H2H basis [POSSIBLY EXHAUSTED] — v109's re-add repeats this pattern; Diff refs: strategy.py:34 — re-imports facing_barrel_continuation (v108 removed this), strategy.py:409 — broadway_suited gate mutated 0.36→0.32 (tighten call vs tight 3-bet openers), strategy.py:668-694 — NEW _single_reraise_stackoff_guard (29 lines, fires on opp_bet_count==1 + tier!=nut + spr<3.0 + made_strength<0.70)
- **v108**: Orphan dead code trap — when a worker removes an import+call site but the function def lives in a non-target file (strategy_helpers.py:272-324 `facing_barrel_continuation`), reviewer flags it but worker cannot delete. Master/Reviewer should expand target_files OR add a post-commit cleanup gate that auto-strips orphaned defs in helper modules.
- **v108**: Crossover attribution test pattern — when isolating a donor trait (v89 `broadway_suited`) into a base (v102), pair the candidate's H2H vs the SAME opponents where donor>base by ≥3pp (v90/v92/v93/v95); if v108 doesn't recover ≥half the gap (v108 vs v92 <0.49 or vs v93 <0.53), the trait was not the edge source and the hypothesis is falsified.
- **v108**: broadway_suited (KQs/KJs/QJs/QTs/JTs) wired as structural hand-class into SB-open implied-odds bucket and BB-vs-raise implied pool (strategy.py:374, 410; `pot_odds<=0.34 or win_rate>=pot_odds-0.01`); removed exhausted `facing_barrel_continuation` defensive nudge. Critic H2H (>100g): v89 beats v102 vs v92/v93/v95/v90 by +3.1/+3.9/+3.3/+4.4pp. Needs ≥100g H2H vs v93/v95.
- **v108 归档建议**: Before locking `facing_barrel_continuation` removal as permanent, run a ≥30g paired net-chips comparison of v108 vs v101 on paired/highly-connected boards; if v108 leaks there vs v101's 0.06-cap, re-add a pot-odds-grounded variant (cap 0.10-0.12) rather than the exhausted flat nudge.
- **v107**: barrel-continuation constants tuned (cap 0.06→0.07, deficit 0.05→0.04) with no H2H basis — repeats standalone-constant-tuning exhaustion. [POSSIBLY EXHAUSTED]
- **v105/v106**: sub-30g H2H, trended negative; do not treat as live strategic targets.

