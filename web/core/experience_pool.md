## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` or `classify_preflop_hand()` for hand-class gates.
- `classify_preflop_hand()` (state.py, 9 buckets incl. broadway_suited) is live across ALL preflop spots (defense + sb_open + bb_vs_limp as of v95). Preflop saturation fix COMPLETE; future work targets limp/call ranges, 4-bet sizing, blind defense width.
- NO archetype classifier (LAG/NIT/CS) is live — dropped on re-base, not restored. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH) with opponent archetype.

## POSTFLOP_STRATEGY
- Fold/commitment is pot-odds + opponent-stat grounded (both LIVE as of v94). Fold-gate axis has 4 dimensions: commit v90 / board v92 / pot-odds v93 / opp-stat v94. Next: VALIDATE GATE 4 at >=100g vs CS lineage v47/v48/v50 (over-fold risk in the 0.30-0.49 commit band).
- Pot-odds grounding lives in `_allin_board_texture_fold()` (fold_gates.py: require pot_odds>=made_strength*0.9 on scary-board folds). Opponent-stat grounding in GATE 4 of `_spr_commitment_gate()` (fold marginal 0.30-0.49 band ONLY when postflop_aggr>=0.42 OR barrel_freq>=0.50, conf>=0.20).
- Structural pre-dispatch commitment gate `_spr_commitment_gate()` (fold_gates.py, wired BEFORE `must_continue_vs_raise`) is the insertion-point pattern; do NOT use `should_fold_postflop` threshold tuning (exhausted since v63). [POSSIBLY EXHAUSTED]
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch, river value-bet blocks); add `postflop_aggr<0.30` or tier!=nut exclusion if H2H vs high-aggr lineage regresses >=100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code.
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents. Widening bluffs vs sticky calling-station callers (v47/v48/v50) = direct chip loss.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H backing before targeting a matchup.
- Multi-signal AND-gated detectors (bluff_heavy, exploit_dispatch, bluff_heavy_call_widen) risk near-zero firing rates vs real opponents — measure firing count over >=100g; relax conjunction or lower thresholds if inert.
- Pot-odds grounding (v93, fully live) is the chip-bleed control mechanism — measure >=100g whether it alone stops bleed vs sticky callers before re-adding `bluff_suppress`.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds yielded no sustained gain; constants allowed only with structural rationale AND per-constant H2H backing — never standalone. Structural bucket replacement (not threshold tuning) is the correct response to exhaustion. [POSSIBLY EXHAUSTED]

## GENERAL
- Any new structural path, constant change, or matchup targeting requires >=100g H2H; <100g is directional noise.
- Select crossover parents by H2H win-rate, not raw Glicko r; prioritize diversity over deepening an over-fit lineage.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues.
- Worker role boundaries: Tuner changes >=1 constant; Architect must not touch constants. Crossover bots need full pipeline.
- Trust early negative Critic signals; structural changes can inflate Critic scores without improving battle — verify H2H.
- Post-crossover verification mandatory: crossover LLM can derive correctness fixes absent from both parents — verify TOTAL_HANDS, wheel straight, re-raise compliance.
- DETECTION-WITHOUT-HANDLER is a recurring dead-code pattern (v77 sb_limp, v81 classify_street_texture, v83 bluff_heavy); ALWAYS wire a consuming action site in the same generation.
- Helper extraction is a safe high-value move near the 1500-line cap; fold_gates.py (v93, strategy.py 1505→1342) is the default template when strategy.py exceeds limits.
- Graduated river value tier keeps getting rebased away (v76→lost v83→restored v87) — consider a permanent-primitives list that survives crossovers.
- v95 is current. LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py, bluff_heavy_call_widen(), exploit_dispatch, river_value_raise_tier(), classify_preflop_hand() (all preflop spots), `_spr_commitment_gate()` incl. GATE 4 opp-stat fold, `_allin_board_texture_fold()` (pot-odds grounded), value-tier sizing floor, fold_gates.py. STILL ABSENT: board_range_filter, archetype classifier.

## RECENT_LESSONS
- **v95**: Preflop saturation fix EXTENDED to sb_open + bb_vs_limp (strategy.py:380-429) — bucket replacement (raise/limp/iso buckets from classify_preflop_hand), no saturation gates remain anywhere. BUT: removed match_adjust/confidence/loose_bonus → sb_open NO LONGER adapts to opponent VPIP/chip context (regression in exploitative adaptation). v94 weakest H2H v85/v47/v81 all 30% (10g noise); v94 overall WR 58.3% (290g) is competitive, not broken.
- **v95 critic flags**: KQo reclassified RAISE→LIMP; T9s/76s excluded from bb_vs_limp iso (standard iso-raises vs limpers); tightening iso vs sticky calling-station lineage v47 (limp-call stations) is the WRONG adjustment. Validate preflop range tightening vs CS lineage >=100g.
- **v94**: GATE 4 opp-stat fold SHIPPED in `_spr_commitment_gate()` fold_gates.py:124-143 — folds marginal 0.30-0.49 commit band ONLY when postflop_aggr>=0.42 OR barrel_freq>=0.50 (conf>=0.20), preserving call-downs vs passive opponents. Completes the opp-stat grounding target from v93.
- **v94 archive note**: Validate GATE 4 at >=100g vs CS lineage v47/v48/v50 — the 0.30-0.49 commit band is exactly where CS extract thin value. If H2H<45% vs any CS bot, loosen strength_cap 0.52→0.56 or gate on barrel_freq only (postflop_aggr false-positives vs passive call-bet-call stations).
- **v93**: Pot-odds grounding on all-in fold gates was necessary but insufficient alone — v94 layered opp-stat conditions on top. Fold-gate axis now 4-dimensional (commit/board/pot-odds/opp-stat).
