## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` or `classify_preflop_hand()` for hand-class gates.
- `classify_preflop_hand()` (state.py, 9 buckets incl. broadway_suited for KQs/KJs/QJs/QTs/JTs) is live; all three preflop defense spots (bb_vs_raise, sb_vs_reraise, sb_vs_iso_raise) now use it — NO saturation-derived preflop gates remain.
- NO archetype classifier (LAG/NIT/CS) live as of v89 — dropped on re-base and not restored. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH) with opponent archetype.

## POSTFLOP_STRATEGY
- Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold.
- Structural pre-dispatch commitment gate (v73/v75-style) ABSENT on v89; pot-odds-grounded fold DEFENSE is high-value and under-developed (NOT exhausted) — surface audit-vs-match conflicts; default to OFFENSIVE/structural but fold-DEFENSE is an explicit high-value target.
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch, river value-bet blocks); add `postflop_aggr<0.30` or tier!=nut exclusion if H2H vs high-aggr lineage regresses >=100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code. (v87 river_value_raise_tier verified: tier=='nut' excluded, fires before thin_static_showdown_control, after paired-board safety guards.)
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H backing before targeting a matchup.
- Multi-signal AND-gated detectors (bluff_heavy, exploit_dispatch, bluff_heavy_call_widen) risk near-zero firing rates vs real opponents — measure firing count over >=100g; relax conjunction or lower thresholds if inert.
- OPEN TENSION (time-boxed at v89): v86 dropped bluff_suppress from v75's exploit_dispatch. Pool has NOT reconciled whether v86's value-boost offensive sizing is a net gain or needs the `bluff_suppress=True when fold_to_bet<0.30` re-add to stop chip bleed vs sticky callers — Master must decide one side, not both.

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
- v89 is current. LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py, bluff_heavy_call_widen(), exploit_dispatch, river_value_raise_tier(), classify_preflop_hand() (incl. broadway_suited) in state.py. STILL ABSENT: board_range_filter, archetype classifier, structural pre-dispatch commitment gate. "bluff_heavy_raise_to_extract" is a phantom module — do not assume it is live.
- NOTE: the POSTFLOP_STRATEGY fold-DEFENSE-vs-call-widening tension and the BLUFF_CALIBRATION bluff_suppress tension are INDEPENDENT next-gen decisions (a fold gate vs a bluff-spew suppressor), NOT one either/or — Master may pursue each on its own merits.

## RECENT_LESSONS
- **v90**: Critic evidence: H2H weaknesses: v89 has only 470 total games with 10-20g per matchup (noise-dominated); the 0.40 wr vs v81/v85 are 10g samples. The strategic motivation comes from the multi-generation 0% postflop fold leak documented in memory, not current H2H snapshots.; Experience pool refs: 'Structural pre-dispatch commitment gate (v73/v75-style) ABSENT on v89; pot-odds-grounded fold DEFENSE is high-value and under-developed (NOT exhausted)' — EXPLICIT green light for this exact change, 'Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold' — the gate partially satisfies this via commit_ratio/pot_ratio axes but does not compute explicit pot odds or use win_rate equity, v87/v88/v89 all targeted preflop/value paths — this is a different axis entirely, NO local optima risk; Diff refs: NEW function _spr_commitment_gate() strategy.py:601-673 — 75-line structural fold gate with 3 graduated sub-gates, Wired at strategy.py:909-914 inside to_call>0 block BEFORE must_continue_vs_raise call at line 981 — correct intercept point, tier=='nut' excluded (line 623), draw_strength>=0.25 excluded (line 626), preflop excluded (line 618) — safe regression boundaries
- **v89**: Preflop hand-class saturation fix COMPLETE — all three preflop defense spots (bb_vs_raise, sb_vs_reraise, sb_vs_iso_raise) now use classify_preflop_hand(); no saturation-derived preflop gates remain. Future preflop work must target new structural axes (limp/call ranges, 4-bet sizing, blind defense width), not strength thresholds.
- **v89**: v88's 0.65 h2h_avg_wr is inflated by beating weak v13/v29/v34 lineage (60-75% wr, 20-30g each) — validate v89 with >=100g vs CS lineage v47/v48/v50 (currently only 10-20g at parity) before trusting the rating climb; check whether broadway_suited's wider sb_vs_iso_raise call path bleeds chips vs calling stations that never fold postflop.
- **v88**: Validate v88 preflop 3-bet->call change (mid-pairs QQ- now call, premium+AK/AQ 3-bet) >=100g vs calling-station lineage (v49/v50) to confirm it doesn't over-fold equity; if v88 regresses vs CS, gate the mid_pair call with a pot-odds floor.
- **v87**: Graduated river value tier (strategy_helpers.py, wired strategy.py before thin_static_showdown_control) — 0.50-0.80x pot for made_strength 0.50-0.82, tier!=nut, conf>=0.10. New structural branch (NOT constant tuning). Validate >=100g vs passive-caller lineage (v47/v51/v62) — converting thin checks to bets risks value-owning calling stations.
- **v86**: exploit_dispatch AND-gate (call_down_flop_turn>=0.55 AND fold_turn<=0.30) — if firing count is zero post-commit >=100g, relax to single-street threshold like v75's original.

