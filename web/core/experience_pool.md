## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` or `classify_preflop_hand()` for hand-class gates.
- `classify_preflop_hand()` (state.py, 9 buckets incl. broadway_suited for KQs/KJs/QJs/QTs/JTs) is live; all three preflop defense spots use it — NO saturation-derived preflop gates remain.
- NO archetype classifier (LAG/NIT/CS) is live — dropped on re-base, not restored. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH) with opponent archetype.

## POSTFLOP_STRATEGY
- Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold.
- Structural pre-dispatch commitment gate is LIVE as `_spr_commitment_gate()` (strategy.py ~617, wired BEFORE `must_continue_vs_raise`). It resolves the 0% postflop fold leak via commit_ratio/pot_ratio/SPR axes but does NOT yet compute explicit pot-odds/equity — equity-grounding remains an open high-value target, NOT exhausted.
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch, river value-bet blocks); add `postflop_aggr<0.30` or tier!=nut exclusion if H2H vs high-aggr lineage regresses >=100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code.
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H backing before targeting a matchup.
- Multi-signal AND-gated detectors (bluff_heavy, exploit_dispatch, bluff_heavy_call_widen) risk near-zero firing rates vs real opponents — measure firing count over >=100g; relax conjunction or lower thresholds if inert.
- OPEN TENSION (time-boxed at v89): v86 dropped `bluff_suppress` from v75's exploit_dispatch. Pool has NOT reconciled whether v86's value-boost offensive sizing is a net gain or needs `bluff_suppress=True when fold_to_bet<0.30` re-added to stop chip bleed vs sticky callers — Master must decide one side, not both.

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
- Graduated river value tier is a recurring structural primitive that keeps getting rebased away (v76→lost at v83→restored v87) — consider a permanent-primitives list that survives crossovers.
- v91 is current. LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py, bluff_heavy_call_widen(), exploit_dispatch, river_value_raise_tier(), classify_preflop_hand() (incl. broadway_suited), `_spr_commitment_gate()`, value-tier sizing floor. STILL ABSENT: board_range_filter, archetype classifier. "bluff_heavy_raise_to_extract" is a phantom module — do not assume it is live.

## RECENT_LESSONS
- **v93**: Critic evidence: H2H weaknesses: v92 vs v25: 20% (10g, noise), v92 vs v88: 20% (10g, noise), v92 vs v48/v49/v17/v89: 40% (10g, noise) — ALL matchups are 10g samples, insufficient for signal per pool rule '<100g is directional noise'; Experience pool refs: v92 RECENT_LESSON: 'All-in fold gates MUST compute pot odds (to_call/(pot+to_call) < made_strength*0.9) before folding — static made_strength thresholds on all-in paths violate the pool's pot-odds grounding rule' — DIRECTLY implemented, POSTFLOP_STRATEGY: 'Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold' — pot-odds DONE, opponent-stat STILL MISSING, GENERAL: 'Helper extraction is a safe high-value move near the 1500-line cap' — extraction is correct and necessary; Diff refs: fold_gates.py:148-157: pot_odds = to_call/(pot+to_call), pot_odds<0.20 early-exit — prevents over-folding to cheap shoves, fold_gates.py:161,164,167,171: each fold condition now requires pot_odds >= made_strength*0.9 — equity-vs-price gating, strategy.py:780,816: call sites updated to pass to_call,pot to _allin_board_texture_fold
- **v92**: All-in fold gates MUST compute pot odds (to_call/(pot+to_call) < made_strength*0.9) before folding — static made_strength thresholds on all-in paths violate the pool's pot-odds grounding rule and risk over-folding vs aggressive bluffers.
- **v92**: strategy.py at 1505/1500 lines is OVER the core limit — next generation MUST extract a helper module (e.g., fold_gates.py) before any further strategy.py additions.
- **v92 归档建议**: Before adding any more fold gates, extract _allin_board_texture_fold and _spr_commitment_gate into a new fold_gates.py module to resolve the 1505/1500 line violation, then add pot-odds grounding to _allin_board_texture_fold specifically for calling-station matchups (v48/v50 at 40-50%) where over-folding to bluffs is the highest-risk regression.
- **v92**: Critic evidence: H2H weaknesses: v91 weakest: v14 (30%), v89 (30%), v24/v82/v88 (40%) — all 10g samples (noise per experience pool). No match data specifically traces losses to over-calling all-ins on scary boards. The Master's plan asserts this pattern but provides no replay evidence.; Experience pool refs: 'Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold' — this gate uses raw made_strength thresholds with NO opponent modeling, 'equity-grounding remains an open high-value target, NOT exhausted' for _spr_commitment_gate — same applies here, gate doesn't compute equity, 'Structural pre-dispatch commitment gate pattern WORKS' — the insertion-point pattern is correct and validated by v90; Diff refs: NEW: _allin_board_texture_fold() strategy.py:692-743 (52 lines), wired at lines 940-946 and 976-982, Fires AFTER existing hard_repressure_fold/paired_board_stackoff['severe'] gates and AFTER value_heavy line_profile gate — good layering, Overlaps with _spr_commitment_gate's scary flag (flush_pressure>=0.75, straight_pressure>=0.75, paired turn/river) but at higher thresholds (1.0 vs 0.75) and different decision path
- **v91**: Value-tier sizing floor (strategy.py:256-271, 0.50/0.55/0.60x floor on tier in nut/strong for rounds 1/2/3) has a NARROW FIRING WINDOW because base ratios (0.60/0.70/0.85) exceed the floor. Validate vs v48/v50 at >=100g; if underbetting persists, raise the turn/river floor above 0.55/0.60 to widen the window.
- **v91**: Critic evidence — v90 loses to calling-station lineage (v48/v50 40% in 10g). Against callers who rarely fold, bigger value bets are the correct exploitative adjustment; this is the first revenue-side offensive change (NOT standalone constant tuning — structural floor with guard clauses).
- **v90**: Structural commitment gate pattern WORKS for the 0% postflop fold leak — `_spr_commitment_gate` BEFORE `must_continue_vs_raise` intercepts the override. Future fold-gate work should use this insertion-point pattern, not `should_fold_postflop` threshold tuning (exhausted since v63).
- **v90**: nutted_risk parameter IS consumed (tightens strength_cap when opponent likely holds monsters) — reviewers must trace derived locals before flagging unused.
- **v90 (improvement)**: Validate `_spr_commitment_gate` at >=100g vs CS lineage (v47/v48/v50/v62); if H2H drops below 45% vs any CS opponent, loosen gate 2's strength_cap 0.50→0.55.
- **v89**: Preflop hand-class saturation fix COMPLETE — all three preflop defense spots use `classify_preflop_hand()`. Future preflop work targets new axes (limp/call ranges, 4-bet sizing, blind defense width), not strength thresholds.



