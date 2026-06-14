## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` or `classify_preflop_hand()` for hand-class gates.
- `classify_preflop_hand()` (state.py, 9 buckets incl. broadway_suited for KQs/KJs/QJs/QTs/JTs) is live; all three preflop defense spots use it — NO saturation-derived preflop gates remain.
- NO archetype classifier (LAG/NIT/CS) is live — dropped on re-base, not restored. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH) with opponent archetype.

## POSTFLOP_STRATEGY
- Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold. Pot-odds is PARTIALLY implemented (v93 `_allin_board_texture_fold`: pot_odds=to_call/(pot+to_call), require pot_odds>=made_strength*0.9 on scary-board folds); opponent-stat grounding (postflop_aggr/fold_to_bet) is STILL MISSING and remains the open high-value target.
- Structural pre-dispatch commitment gate is LIVE as `_spr_commitment_gate()` (strategy.py ~617, wired BEFORE `must_continue_vs_raise`); use this insertion-point pattern, NOT `should_fold_postflop` threshold tuning (exhausted since v63).
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch, river value-bet blocks); add `postflop_aggr<0.30` or tier!=nut exclusion if H2H vs high-aggr lineage regresses >=100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code.
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H backing before targeting a matchup.
- Multi-signal AND-gated detectors (bluff_heavy, exploit_dispatch, bluff_heavy_call_widen) risk near-zero firing rates vs real opponents — measure firing count over >=100g; relax conjunction or lower thresholds if inert.
- v93's pot-odds grounding on all-in fold gates (NOT local-optima, new equity-vs-price axis) effectively supersedes the old bluff_suppress tension — value-boost offensive sizing now has an equity floor as the bleed-control mechanism. Do NOT re-add `bluff_suppress` blindly; instead measure >=100g whether pot-odds grounding alone stops chip bleed vs sticky callers (v48/v50).

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
- Helper extraction is a safe high-value move near the 1500-line cap; prefer extraction over compression when headroom <50 — fold_gates.py pattern (v93) is the default template when strategy.py exceeds limits.
- Graduated river value tier is a recurring structural primitive that keeps getting rebased away (v76→lost at v83→restored v87) — consider a permanent-primitives list that survives crossovers.
- v93 is current. LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py, bluff_heavy_call_widen(), exploit_dispatch, river_value_raise_tier(), classify_preflop_hand() (incl. broadway_suited), `_spr_commitment_gate()`, `_allin_board_texture_fold()` (pot-odds grounded), value-tier sizing floor, fold_gates.py. STILL ABSENT: board_range_filter, archetype classifier. "bluff_heavy_raise_to_extract" is a phantom module — do not assume it is live.

## RECENT_LESSONS
- **v94**: Critic evidence: H2H weaknesses: v93 weakest: 40% vs claude_v29 (10g, noise); 50% vs v91 (10g). No matchup <40% with meaningful sample. Overall v93: 90W-40L (69% WR) at 130g. Thin H2H sample but experience pool provides the strategic basis.; Experience pool refs: Line 9: 'Pot-odds is PARTIALLY implemented (v93); opponent-stat grounding (postflop_aggr/fold_to_bet) is STILL MISSING and remains the open high-value target.' Line 38: 'v93: Pot-odds grounding on all-in fold gates is necessary but insufficient — Critic confirmed opponent-stat grounding (postflop_aggr/fold_to_bet) still missing. Next fold-gate iteration must add opp-stat conditions, not just equity-vs-price comparison.' v94 implements exactly this.; Diff refs: fold_gates.py:124-141 — GATE 4 block, 4-way condition (commit_ratio 0.30-0.49 + confidence>=0.20 + value_heavy + marginal hand). strategy.py:831 — opponent_model threaded into _spr_commitment_gate call site. Reuses value-heavy detector constants (postflop_aggr>=0.42, barrel_freq>=0.50) from should_fold_postflop lines 567-572 for consistency.
- **v93**: Helper extraction to fold_gates.py (strategy.py 1505→1342) resolved the OVER-limit crisis cleanly — use this template by default when strategy.py exceeds limits.
- **v93**: Pot-odds grounding on all-in fold gates is necessary but insufficient — Critic confirmed opponent-stat grounding (postflop_aggr/fold_to_bet) still missing. Next fold-gate iteration must add opp-stat conditions, not just equity-vs-price comparison.
- **v93**: Validate >=100g vs CS lineage (v47/v48/v50) — the 0.20 pot-odds early-exit threshold is hand-tuned and may leak value vs players who cheap-bluff-shove the river with frequency.
- **v91**: Value-tier sizing floor (0.50/0.55/0.60x) has a NARROW FIRING WINDOW (base ratios 0.60/0.70/0.85 exceed floor). Validate vs v48/v50 at >=100g; if underbetting persists, raise turn/river floor above 0.55/0.60 to widen the window. Against calling stations who rarely fold, bigger value bets are the correct exploitative adjustment.
- **v90**: Validate `_spr_commitment_gate` at >=100g vs CS lineage (v47/v48/v50/v62); if H2H drops below 45% vs any CS opponent, loosen gate 2's strength_cap 0.50→0.55. nutted_risk parameter IS consumed — reviewers must trace derived locals before flagging unused.
- **v89**: Preflop hand-class saturation fix COMPLETE — all three preflop defense spots use `classify_preflop_hand()`. Future preflop work targets new axes (limp/call ranges, 4-bet sizing, blind defense width), not strength thresholds.

