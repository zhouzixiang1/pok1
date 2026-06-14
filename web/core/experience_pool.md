## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` or `classify_preflop_hand()` for hand-class gates.
- NO archetype classifier (LAG/NIT/CS) live as of v88 — dropped on re-base and not restored. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH) with opponent archetype.

## POSTFLOP_STRATEGY
- Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold.
- Structural pre-dispatch commitment gate (v73/v75-style) ABSENT on v88; pot-odds-grounded fold DEFENSE is high-value and under-developed (NOT exhausted) — surface audit-vs-match conflicts; default to OFFENSIVE/structural but fold-DEFENSE is an explicit high-value target.
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch, river value-bet blocks); add `postflop_aggr<0.30` or tier!=nut exclusion if H2H vs high-aggr lineage regresses >=100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code. (v87 river_value_raise_tier verified: tier=='nut' excluded, fires before thin_static_showdown_control, after paired-board safety guards.)
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H backing before targeting a matchup.
- Multi-signal AND-gated detectors (bluff_heavy, exploit_dispatch, bluff_heavy_call_widen) risk near-zero firing rates vs real opponents — measure firing count over >=100g; relax conjunction or lower thresholds if inert.
- OPEN TENSION (time-boxed at v88): v86 dropped bluff_suppress from v75's exploit_dispatch. Pool has NOT reconciled whether v86's value-boost offensive sizing is a net gain or needs the `bluff_suppress=True when fold_to_bet<0.30` re-add to stop chip bleed vs sticky callers — Master must decide one side, not both.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds yielded no sustained gain; constants allowed only with structural rationale AND per-constant H2H backing — never standalone. [POSSIBLY EXHAUSTED]

## GENERAL
- Any new structural path, constant change, or matchup targeting requires >=100g H2H; <100g is directional noise.
- Select crossover parents by H2H win-rate, not raw Glicko r; prioritize diversity over deepening an over-fit lineage.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues.
- Worker role boundaries: Tuner changes >=1 constant; Architect must not touch constants. Crossover bots need full pipeline.
- Trust early negative Critic signals; structural changes can inflate Critic scores without improving battle — verify H2H.
- Post-crossover verification mandatory: crossover LLM can derive correctness fixes absent from both parents — verify TOTAL_HANDS, wheel straight, re-raise compliance.
- DETECTION-WITHOUT-HANDLER is a recurring dead-code pattern (v77 sb_limp, v81 classify_street_texture, v83 bluff_heavy); ALWAYS wire a consuming action site in the same generation.
- Helper extraction is a safe high-value move near the 1500-line cap; prefer extraction over compression when headroom <50.
- Graduated river value tier is a recurring structural primitive that keeps getting rebased away (v76->lost at v83->restored v87) — consider a permanent-primitives list that survives crossovers.
- v88 is current (db81f89). LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py, bluff_heavy_call_widen(), exploit_dispatch, river_value_raise_tier(), classify_preflop_hand() in state.py. STILL ABSENT: board_range_filter, archetype classifier, structural pre-dispatch commitment gate. "bluff_heavy_raise_to_extract" is a phantom module — do not assume it is live.
- NOTE: the POSTFLOP_STRATEGY fold-DEFENSE-vs-call-widening tension and the BLUFF_CALIBRATION bluff_suppress tension are INDEPENDENT next-gen decisions (a fold gate vs a bluff-spew suppressor), NOT one either/or — Master may pursue each on its own merits.

## RECENT_LESSONS
- **v89**: Critic evidence: H2H weaknesses: v88 is strong overall (65.8% win rate, 290 games), but v89 H2H data is not yet available. The change targets algorithmic correctness (saturation bug) rather than a specific weak matchup — appropriate for a preflop structural fix.; Experience pool refs: RECENT_LESSONS v88: 'extend [classify_preflop_hand] to sb_vs_iso_raise (strategy.py:447-463 still uses old saturated preflop_strength>=0.58 gate)' — DIRECTLY ADDRESSED, RECENT_LESSONS v88: 'fix KQs/KJs/QJs classification (currently suited_connector instead of broadway_suited)' — DIRECTLY ADDRESSED, OPPONENT_MODELING: 'estimate_preflop_strength saturates to 1.0 for all pocket pairs — use preflop_hand_profile or classify_preflop_hand() for hand-class gates' — APPLIED; Diff refs: state.py:89-90 — new broadway_suited bucket: `if suited and high >= 11 and low >= 10: return 'broadway_suited'` (KQs, KJs, QJs, QTs, JTs), strategy.py:449-452 — sb_vs_iso_raise gate changed from `preflop_strength >= 0.58` to `hand_cat_iso in ('premium', 'big_cards')` (same pattern as v88's bb_vs_raise fix), strategy.py:440 — bb_vs_raise call list: adds 'broadway_suited' alongside strong_pair/mid_pair/playable
- **v88**: classify_preflop_hand() (state.py:52-100, 9 buckets) is live — extend it to sb_vs_iso_raise (strategy.py:447-463 still uses old saturated preflop_strength>=0.58 gate) and fix KQs/KJs/QJs classification (currently 'suited_connector' instead of 'broadway_suited').
- **v88**: Validate v88 preflop 3-bet->call change (mid-pairs QQ- now call, premium+AK/AQ 3-bet) >=100g vs calling-station lineage (v49/v50) to confirm it doesn't over-fold equity; if v88 regresses vs CS, gate the mid_pair call with a pot-odds floor.
- **v87**: Graduated river value tier (strategy_helpers.py:270-305, wired strategy.py:1143-1149 before thin_static_showdown_control) — 0.50-0.80x pot for made_strength 0.50-0.82, tier!=nut, conf>=0.10. New structural branch (NOT constant tuning). Validate >=100g vs passive-caller lineage (v47/v51/v62) — converting thin checks to bets risks value-owning calling stations.
- **v86**: exploit_dispatch AND-gate (call_down_flop_turn>=0.55 AND fold_turn<=0.30) — if firing count is zero post-commit >=100g, relax to single-street threshold like v75's original.

