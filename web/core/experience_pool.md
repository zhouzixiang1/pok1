## OPPONENT_MODELING
- Use live opponent stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence/sample gates; OR-combine tendencies with modest magnitudes; sub-30g matchups are directional noise only.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, pfr/vpip), not generic action confidence; never classify unknown openers as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates.
- Do not confuse `value_profile['tier']` with opponent archetype; verify claimed archetype/board-range primitives exist and are live before planning around them.

## POSTFLOP_STRATEGY
- Defensive late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated; add no new guard unless it targets a distinct decision point and has >=100g validation. [POSSIBLY EXHAUSTED]
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation and verify reachability/fire-rate.
- Confirm named primitives exist in current source before referencing them; docstrings, memories, stale planning notes, and previously live helper names are not definitions.
- Audit action-selection paths for raw-ratio bypasses, skipped `choose_raise`, downstream caps, dispatch-order shadowing, and overlapping handler order before modifying behavior.
- Near line cap, prioritize infrastructure-only dispatch/raise-decision table refactors over bundled behavior changes.
- Verify trap-guard exclusion lists after any `_should_checkraise_trap` refactor; dropping value/bluff exclusions can suppress intended value sizing on overlapping tiers.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence and confidence; low aggression/passivity alone may indicate calling-station behavior.
- Note: exhaustion applies to DEFENSIVE guards, not offense. New offensive bluff/value paths remain permitted when backed by firing-rate logs and >=100g H2H.
- Structural bluff modules require current-source live-path verification before being treated as successful or expanded.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios, caps, floors, and call thresholds has no sustained gain; Tuner changes must be constants-only inside an Architect-defined structural hypothesis with per-constant H2H backing. [POSSIBLY EXHAUSTED]
- Exclude new sizing-tier/floor/cap increases from Tuner work unless current source proves dispatch order, downstream caps, and target live path are not the blocker.
- Do not reintroduce stacked value-sizing boosts such as `value_sizing_delta` at `choose_raise` unless current source and matchup evidence prove underbetting.

## GENERAL
- Any new structural path, constant change, or matchup target requires >=100g H2H validation before treating it as successful, repeating it, or expanding it.
- Treat commit messages as advisory; trust the git diff (v107 claimed a thin-value probe_mode mutation that was byte-identical to v102).
- Select crossover parents by H2H win-rate and diversity, not raw Glicko alone; verify the crossover tool actually executed rather than falling back to master+worker copy.
- Verify branch_from logic considers current top-rated bots, not just stagnation ancestor (v107 branched from v102 when v106 was available).
- Use one mechanism per generation except sanctioned crossover diversity rescues; helper extraction, line-cap relief, and dispatch refactors should be infrastructure-only generations.
- Worker boundaries are mandatory: Architect defines structural logic; Tuner may only adjust constants within that structure, not create new logic.

## RECENT_LESSONS
- **v108**: Critic evidence: H2H weaknesses: v102 vs v92 = 47.8% (90g) — v102 loses; v89 vs v92 = 51.7% (120g), v102 vs v100 = 47.3% (110g) — v102 loses, v102 vs v93 = 51.8% (110g) — near-even; v89 vs v93 = 54.7% (150g), v102 vs v95 = no direct H2H; v89 vs v95 = 54.6% (130g), v102 vs v90 = 51.0% (100g); v89 vs v90 = 55.0% (140g); Experience pool refs: [POSSIBLY EXHAUSTED] Defensive late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated; facing_barrel_continuation removal is consistent with this, [POSSIBLY EXHAUSTED] Standalone constant tuning of sizing ratios — v108 avoids this by adding a structural hand-class instead; Diff refs: state.py:65,87-90 — new 'broadway_suited' hand-class bucket (11<=high<=13, low>=10, suited), strategy.py:368 — SB-open implied pool now includes broadway_suited (defaults to 'raise'), strategy.py:402-407 — BB-vs-raise pot-odds-grounded call/fold for broadway_suited (pot_odds<=0.36 or win_rate>=pot_odds-0.02)
- **v108**: broadway_suited (KQs/KJs/QJs/QTs/JTs) wired as structural hand-class into SB-open implied-odds bucket and BB-vs-raise implied pool (strategy.py:374, 410; gated `pot_odds<=0.34 or win_rate>=pot_odds-0.01`); removed exhausted `facing_barrel_continuation` defensive nudge (strategy.py:34). Critic H2H (all >100g): v89 beats v102 vs v92/v93/v95/v90 by +3.1/+3.9/+3.3/+4.4pp. Needs >=100g H2H validation vs v93/v95.
- **v107**: barrel-continuation constants tuned (cap 0.06→0.07, deficit 0.05→0.04) with no H2H basis — repeats standalone-constant-tuning exhaustion. [POSSIBLY EXHAUSTED]
- **v105/v106**: sub-30g H2H, trended negative; do not treat as live strategic targets.
- **v104-v106**: sizing cap/constant tuning, value-underbet fixes without validated dispatch reachability, and late-street defensive guard accumulation repeated without confirmed H2H gain. [POSSIBLY EXHAUSTED]

