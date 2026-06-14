## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` for hand-class gates.
- NO archetype classifier (LAG/NIT/CS) on v86 — dropped on re-base and not restored. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH) with opponent archetype.

## POSTFLOP_STRATEGY
- Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold.
- Structural pre-dispatch commitment gate (v73/v75-style) is ABSENT on v86; restoring pot-odds-grounded fold DEFENSE is high-value and under-developed (NOT exhausted). NOTE: fold-defense and offensive call-widening address DIFFERENT paths — next-gen should pick ONE mechanism per HARD GATE; the pool has not yet resolved this oscillation, so call it out explicitly rather than leaving the tension implicit.
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch, river value-bet blocks); add `postflop_aggr<0.30` or tier≠nut exclusion if H2H vs high-aggr lineage regresses ≥100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code.
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need ≥100g H2H backing before targeting a matchup.
- Multi-signal AND-gated detectors (bluff_heavy, exploit_dispatch) risk near-zero firing rates vs real opponents — measure firing count over ≥100g before tuning; relax conjunction or lower thresholds if inert.
- v86 dropped bluff_suppress from v75's exploit_dispatch — re-add `bluff_suppress=True when fold_to_bet<0.30` to avoid chip bleed vs sticky callers. The pool has NOT reconciled whether v86's value-boost offensive sizing is a net gain or needs this defensive re-balancing — address both sides together.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds yielded no sustained gain; constants allowed only with structural rationale AND per-constant H2H backing — never standalone. [POSSIBLY EXHAUSTED]

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100g H2H; <100g is directional noise.
- Select crossover parents by H2H win-rate, not raw Glicko r; prioritize diversity over deepening an over-fit lineage.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues.
- Worker role boundaries: Tuner changes ≥1 constant; Architect must not touch constants. Crossover bots need full pipeline.
- Trust early negative Critic signals; structural changes can inflate Critic scores without improving battle — verify H2H.
- Direction-Audit fold-forbiddance is sliding-window, not permanent; default to OFFENSIVE/structural work, but fold-DEFENSE (pot-odds-grounded) is explicitly high-value — surface audit-vs-match conflicts rather than treating "default offense" as absolute.
- Post-crossover verification mandatory: crossover LLM can derive correctness fixes absent from both parents — verify TOTAL_HANDS, wheel straight, re-raise compliance.
- DETECTION-WITHOUT-HANDLER is a recurring dead-code pattern (v77 sb_limp, v81 classify_street_texture, v83 bluff_heavy); ALWAYS wire a consuming action site in the same generation — an unwired label is a guaranteed next-gen task and a critic-local-optima risk.
- Helper extraction is a safe high-value move near the 1500-line cap; prefer extraction over compression when headroom <50.
- v86 is current. LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py, bluff_heavy_call_widen(), exploit_dispatch (v86 re-added). STILL ABSENT: board_range_filter, archetype classifier, structural pre-dispatch commitment gate. NOTE: "bluff_heavy_raise_to_extract" is a phantom module (never carried forward into v85/v86) — do not assume it is live.

## RECENT_LESSONS
- **v87**: Critic evidence: H2H weaknesses: v86 overall 51.94% over 360g (near plateau); weakest H2H: v49 10%(10g), v53/v61/v29/v41/v31 40%(10g) — all <100g directional noise, no specific matchup targeted; Experience pool refs: POSTFLOP_STRATEGY: 'New value tiers must not overlap early-return guards' — verified: tier=='nut' excluded, fires before thin_static_showdown_control; 'Opponent-stat gating needed on value paths' — satisfied via confidence>=0.10 and fold_to_bet_river adjustment; PARAMETER_TUNING [POSSIBLY EXHAUSTED] — this is NOT constant tuning, it's a new structural decision branch; v76 memory confirms graduated river tier was lost in rebase, making this re-introduction legitimate; Diff refs: strategy_helpers.py:270-305 new river_value_raise_tier() with graduated 0.50-0.80x sizing; strategy.py:1143-1149 wired BEFORE thin_static_showdown_control (line 1153), converting thin-hand checks to value raises; fires AFTER bad_river_value_bet/bad_stackoff_overpair guards (lines 1139-1142) preserving paired-board safety
- **v86**: exploit_dispatch AND-gate (call_down_flop_turn≥0.55 AND fold_turn≤0.30) requires 7/10 call-downs AND ≤2/10 folds simultaneously — if firing count is zero post-commit ≥100g, relax to single-street threshold like v75's original (fold_to_bet<0.30 → value_boost). Also re-add bluff_suppress (fold_to_bet<0.30) dropped from v75 to stop chip bleed vs sticky callers.
- **v86**: v85 H2H weaknesses (v16 @40%, v82 @40%, 10g) are <100g directional noise; v83 overall win_rate 51.39% over 1080g near plateau.
- **v85**: bluff_heavy_call_widen() wired (strategy_helpers.py:221-244, strategy.py:844-848) — pot-odds-grounded, clamp(0.0,0.08). Firing-rate risk: 4-signal conjunction may be near-zero vs real opponents. Measure firing count over ≥100g vs CS-lineage (v51/v62/v78); if zero, relax conjunction or lower BLUFF_OPPORTUNITY_THRESHOLD to ~0.42.
- **v83**: line_reading.py polarization classifier added; 3 value_heavy fold gates in strategy.py — validate vs CS lineage v51/v62 at ≥100g (over-fold risk on strong one-pair). Helper extraction resolved headroom crisis (1498→1302).

