## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` for hand-class gates.
- NO archetype classifier (LAG/NIT/CS) on v83 — dropped on re-base. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH) with opponent archetype.

## POSTFLOP_STRATEGY
- Structural pre-dispatch commitment gate (v73/v75-style) is ABSENT on v83; restoring pot-odds-grounded fold DEFENSE is high-value and under-developed (NOT exhausted). Priority note: fold-defense and offensive call-widening address DIFFERENT paths (defensive fold vs offensive call-down); next-gen should pick ONE mechanism per HARD GATE, not oscillate.
- Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold.
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch, river value-bet blocks); add `postflop_aggr<0.30` or tier≠nut exclusion if H2H vs high-aggr lineage regresses ≥100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code.
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need ≥100g H2H backing before targeting a matchup.
- Multi-signal AND-gated detectors (e.g. bluff_heavy: post_aggr<=0.28 AND barrel_freq<=0.30 AND fold_to_bet>=0.40 AND bluff_opportunity>=0.55) risk near-zero firing rates vs real opponents — measure firing count over ≥100g before tuning; relax conjunction or lower thresholds if inert.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds yielded no sustained gain; constants allowed only with structural rationale AND per-constant H2H backing — never standalone. [POSSIBLY EXHAUSTED]

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100g H2H; <100g is directional noise.
- Select crossover parents by H2H win-rate, not raw Glicko r; prioritize diversity over deepening an over-fit lineage.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues.
- Worker role boundaries: Tuner changes ≥1 constant; Architect must not touch constants. Crossover bots need full pipeline.
- Trust early negative Critic signals; structural changes can inflate Critic scores without improving battle — verify H2H.
- Direction-Audit fold-forbiddance is sliding-window, not permanent; default to OFFENSIVE/structural work and surface audit-vs-match conflicts.
- Post-crossover verification mandatory: crossover LLM can derive correctness fixes absent from both parents — verify TOTAL_HANDS, wheel straight, re-raise compliance.
- DETECTION-WITHOUT-HANDLER is a recurring dead-code pattern (v77 sb_limp, v81 classify_street_texture, v83 bluff_heavy); ALWAYS wire a consuming action site in the same generation — an unwired label is a guaranteed next-gen task and a critic-local-optima risk.
- Helper extraction is a safe high-value move near the 1500-line cap; prefer extraction over compression when headroom <50.
- v85 is current. LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py, bluff_heavy_call_widen(), bluff_heavy_raise_to_extract(). STILL ABSENT: exploit_dispatch, board_range_filter, archetype classifier, structural pre-dispatch commitment gate.

## RECENT_LESSONS
- **v86**: Critic evidence: H2H weaknesses: v85 vs claude_v16: WR=0.400 (10g), v85 vs claude_v82: WR=0.400 (10g) — both <45% but <100g directional noise per experience pool; Experience pool refs: Experience pool line 33: 'STILL ABSENT: exploit_dispatch' — this gen directly addresses it, Experience pool line 18: 'Multi-signal AND-gated detectors risk near-zero firing rates vs real opponents — measure firing count over ≥100g' — v86's 2-signal AND-gate for value_boost falls in this risk zone, Experience pool line 2: '_aligned_signal_boost() and EQR clamp are validated noise filters; extend to preflop defense and value sizing' — value_boost via sizing_exploit_delta is aligned with this guidance; Diff refs: strategy_helpers.py:223-245 — new exploit_dispatch() with 2-signal AND-gates, strategy.py:1270-1271 — exploit_dispatch called before raise condition, strategy.py:1272 — exploit['should_barrel'] added to raise condition OR-chain
- **v85**: bluff_heavy_call_widen() wired (strategy_helpers.py:221-244, strategy.py:844-848) — pot-odds-grounded, clamp(0.0,0.08) bound. Firing-rate risk: depends on 4-signal conjunction that may be near-zero vs real opponents. Measure firing count over ≥100g vs CS-lineage (v51/v62/v78); if zero firings, relax conjunction or lower BLUFF_OPPORTUNITY_THRESHOLD to ~0.42 before further tuning.
- **v85**: v83 overall win_rate 51.39% over 1080g (near plateau); CS-lineage over-fold pattern documented but all H2H weaknesses (v62 @40%, v48/v81 @43.3%) are <100g directional noise.
- **v84**: bluff_heavy_raise_to_extract() wired (strategy_helpers.py:25-60, strategy.py:1125-1135) — raised for thin value instead of literal pool mirror; Critic 7.0 accepted structurally-distinct reinterpretation. Validate ≥100g vs CS-lineage before further exploit work.
- **v83**: line_reading.py polarization classifier added; 3 value_heavy fold gates in strategy.py — validate vs CS lineage v51/v62 at ≥100g (over-fold risk on strong one-pair). Helper extraction resolved headroom crisis (1498→1302).

