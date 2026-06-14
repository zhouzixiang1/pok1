## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` for hand-class gates.
- NO archetype classifier (LAG/NIT/CS) live as of v87 — dropped on re-base and not restored. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH) with opponent archetype.

## POSTFLOP_STRATEGY
- Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold.
- Structural pre-dispatch commitment gate (v73/v75-style) ABSENT on v87; pot-odds-grounded fold DEFENSE is high-value and under-developed (NOT exhausted).
- OPEN TENSION (time-boxed at v88): fold-DEFENSE (pot-odds gate) vs offensive call-widening address DIFFERENT paths; the one-mechanism HARD GATE means next-gen picks ONE — Master must not run both.
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch, river value-bet blocks); add `postflop_aggr<0.30` or tier≠nut exclusion if H2H vs high-aggr lineage regresses ≥100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code. (v87 river_value_raise_tier verified: tier=='nut' excluded, fires before thin_static_showdown_control, after paired-board safety guards.)
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need ≥100g H2H backing before targeting a matchup.
- Multi-signal AND-gated detectors (bluff_heavy, exploit_dispatch, bluff_heavy_call_widen) risk near-zero firing rates vs real opponents — measure firing count over ≥100g; relax conjunction or lower thresholds if inert.
- OPEN TENSION (time-boxed at v88): v86 dropped bluff_suppress from v75's exploit_dispatch. Pool has NOT reconciled whether v86's value-boost offensive sizing is a net gain or needs the `bluff_suppress=True when fold_to_bet<0.30` re-add to stop chip bleed vs sticky callers — Master must decide one side, not both.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds yielded no sustained gain; constants allowed only with structural rationale AND per-constant H2H backing — never standalone. [POSSIBLY EXHAUSTED]

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100g H2H; <100g is directional noise.
- Select crossover parents by H2H win-rate, not raw Glicko r; prioritize diversity over deepening an over-fit lineage.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues.
- Worker role boundaries: Tuner changes ≥1 constant; Architect must not touch constants. Crossover bots need full pipeline.
- Trust early negative Critic signals; structural changes can inflate Critic scores without improving battle — verify H2H.
- Direction-Audit fold-forbiddance is sliding-window, not permanent; default to OFFENSIVE/structural work, but fold-DEFENSE is explicitly high-value — surface audit-vs-match conflicts.
- Post-crossover verification mandatory: crossover LLM can derive correctness fixes absent from both parents — verify TOTAL_HANDS, wheel straight, re-raise compliance.
- DETECTION-WITHOUT-HANDLER is a recurring dead-code pattern (v77 sb_limp, v81 classify_street_texture, v83 bluff_heavy); ALWAYS wire a consuming action site in the same generation.
- Helper extraction is a safe high-value move near the 1500-line cap; prefer extraction over compression when headroom <50.
- Graduated river value tier is a recurring structural primitive that keeps getting rebased away (v76→lost at v83→restored v87) — consider a permanent-primitives list that survives crossovers.
- v87 is current (9569bf1). LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py, bluff_heavy_call_widen(), exploit_dispatch, river_value_raise_tier(). STILL ABSENT: board_range_filter, archetype classifier, structural pre-dispatch commitment gate. "bluff_heavy_raise_to_extract" is a phantom module — do not assume it is live.

## RECENT_LESSONS
- **v88**: Critic evidence: H2H weaknesses: No v88 H2H data yet (just committed). v87 overall 51.94% over 360g (near plateau). Preflop over-aggression was not specifically identified as a top H2H weakness from match data — the fix is theory/experience-pool driven, not data-driven.; Experience pool refs: Experience pool line 4: 'estimate_preflop_strength saturates to 1.0 for all pocket pairs — use preflop_hand_profile for hand-class gates.' — this exact lesson is implemented., Experience pool line 9: pot-odds-grounded fold DEFENSE is high-value and under-developed — this change is preflop not postflop, so orthogonal., PARAMETER_TUNING [POSSIBLY EXHAUSTED]: standalone constant tuning yielded no gain — but this is NOT constant tuning, it's a new discrete classifier replacing a broken continuous gate.; Diff refs: state.py:52-100 — new classify_preflop_hand() with 9 buckets; verified correct pair detection (AA=premium, TT=strong_pair, 88=mid_pair, 22=small_pair) via engine card integers., strategy.py:410-444 (bb_vs_raise) — 3-bet now gated on hand_cat in ('premium','big_cards') instead of preflop_strength>=0.60; bluff-3bet on ('suited_connector','suited_ace','small_pair'); call on ('strong_pair','mid_pair','playable')., strategy.py:467-499 (sb_vs_reraise) — 4-bet now gated on hand_cat=='premium' or AKs instead of preflop_strength>=0.78; allin-call restricted to ('premium','strong_pair','big_cards').
- **v87**: Graduated river value tier (strategy_helpers.py:270-305, wired strategy.py:1143-1149 before thin_static_showdown_control) — 0.50-0.80x pot for made_strength 0.50-0.82, tier≠nut, conf>=0.10. Precommit 36-36 parity. This is NOT constant tuning (new structural decision branch). v86 overall 51.94% over 360g (near plateau); weakest H2H all <100g directional noise.
- **v87**: Validate new river value bets ≥100g vs passive-caller lineage (v47/v51/v62) — converting thin checks to 0.50-0.80x pot bets risks value-owning calling stations that never fold better. v86's worst meaningful-sample matchup is v47 (40%, 10g).
- **v86**: exploit_dispatch AND-gate (call_down_flop_turn≥0.55 AND fold_turn≤0.30) — if firing count is zero post-commit ≥100g, relax to single-street threshold like v75's original. Re-add bluff_suppress (fold_to_bet<0.30) dropped from v75 to stop chip bleed vs sticky callers.
- **v85**: bluff_heavy_call_widen() wired (strategy_helpers.py:221-244, strategy.py:844-848) — clamp(0.0,0.08). 4-signal conjunction may be near-zero vs real opponents; measure firing count ≥100g vs CS-lineage (v51/v62/v78), relax conjunction or lower BLUFF_OPPORTUNITY_THRESHOLD to ~0.42 if inert.

