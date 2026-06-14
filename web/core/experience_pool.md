## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` or `classify_preflop_hand()` for hand-class gates.
- `classify_preflop_hand()` (state.py, 9 buckets incl. broadway_suited for KQs/KJs/QJs/QTs/JTs) is live; all three preflop defense spots now use it — NO saturation-derived preflop gates remain.
- NO archetype classifier (LAG/NIT/CS) is live — dropped on re-base and not restored through v90. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH) with opponent archetype.

## POSTFLOP_STRATEGY
- Fold/commitment must be pot-odds + opponent-stat grounded, not raw made_strength threshold.
- Structural pre-dispatch commitment gate is NOW LIVE as `_spr_commitment_gate()` (v90, strategy.py:601, wired :909 BEFORE must_continue_vs_raise). It resolves the 0% postflop fold leak using commit_ratio/pot_ratio/SPR axes but does NOT yet compute explicit pot-odds or win-rate equity — the equity-grounding refinement remains an open high-value target, NOT exhausted.
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch, river value-bet blocks); add `postflop_aggr<0.30` or tier!=nut exclusion if H2H vs high-aggr lineage regresses >=100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code. (v87 river_value_raise_tier verified: tier=='nut' excluded, fires before thin_static_showdown_control, after paired-board safety guards.)
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need >=100g H2H backing before targeting a matchup.
- Multi-signal AND-gated detectors (bluff_heavy, exploit_dispatch, bluff_heavy_call_widen) risk near-zero firing rates vs real opponents — measure firing count over >=100g; relax conjunction or lower thresholds if inert.
- OPEN TENSION (sole remaining, time-boxed at v89): v86 dropped bluff_suppress from v75's exploit_dispatch. Pool has NOT reconciled whether v86's value-boost offensive sizing is a net gain or needs `bluff_suppress=True when fold_to_bet<0.30` re-added to stop chip bleed vs sticky callers — Master must decide one side, not both.

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
- v90 is current. LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py, bluff_heavy_call_widen(), exploit_dispatch, river_value_raise_tier(), classify_preflop_hand() (incl. broadway_suited), `_spr_commitment_gate()`. STILL ABSENT: board_range_filter, archetype classifier. "bluff_heavy_raise_to_extract" is a phantom module — do not assume it is live.

## RECENT_LESSONS
- **v91**: Critic evidence: H2H weaknesses: v90 loses to calling-station lineage: v48 (40%), v50 (40%) in 10g; also v76/v80/v83 (40%). v47 r=365.6 (bottom 5) but v90 only 70% vs it. Against callers who rarely fold, bigger value bets are the correct exploitative adjustment.; Experience pool refs: PARAMETER_TUNING [POSSIBLY EXHAUSTED] — but this is NOT standalone constant tuning; it's a structural floor with guard clauses per sizing mode. Direction audit explicitly mandated offensive sizing work after 8+ fold-gate attempts exhausted. No prior sizing-floor pattern exists in the pool. v90 SPR gate was fold-side; this is the first revenue-side offensive change.; Diff refs: strategy.py:256-271: 16-line insertion in choose_raise(), between low_ratio computation (line 253-255) and clamp() (line 272). Raises low_ratio to 0.50/0.55/0.60 for tier in (nut,strong) on rounds 1/2/3. Does NOT modify ratio calculation, only the clamp lower bound. Guards: not semi_bluff, not blocker_bluff, not probe_mode, not inducing_value, thin_cap is None.
- **v90**: Structural commitment gate pattern WORKS for the 0% postflop fold leak — `_spr_commitment_gate` placed BEFORE `must_continue_vs_raise` (which forces continuation at made_strength>=0.58) successfully intercepts the override. Future fold-gate work should use this insertion-point pattern, not `should_fold_postflop` threshold tuning (exhausted since v63).
- **v90**: nutted_risk parameter IS consumed (risk variable adjusts strength_cap in gates 1-2) — reviewer flagged it as unused but it actively tightens fold thresholds when opponent likely holds monsters. Future reviewers should trace variable usage through derived locals.
- **v90 (improvement)**: Validate `_spr_commitment_gate` at >=100 daemon games vs calling-station lineage (v47/v48/v50/v62) — the gate folds marginal made hands on scary boards which risks over-folding to passive value-bettors; if H2H win rate drops below 45% vs any CS opponent, loosen gate 2's strength_cap from 0.50->0.55.
- **v89**: Preflop hand-class saturation fix COMPLETE — all three preflop defense spots now use `classify_preflop_hand()`. Future preflop work must target new structural axes (limp/call ranges, 4-bet sizing, blind defense width), not strength thresholds.
- **v89**: v88's 0.65 h2h_avg_wr is inflated by beating weak v13/v29/v34 lineage — validate v89 with >=100g vs CS lineage v47/v48/v50 before trusting the rating climb; check whether broadway_suited's wider sb_vs_iso_raise call path bleeds chips vs calling stations.
- **v88**: Validate preflop 3-bet->call change (mid-pairs QQ- now call, premium+AK/AQ 3-bet) >=100g vs CS lineage (v49/v50) to confirm it doesn't over-fold equity; if it regresses vs CS, gate the mid_pair call with a pot-odds floor.
- **v87**: Graduated river value tier (0.50-0.80x pot for made_strength 0.50-0.82, tier!=nut, conf>=0.10) is a NEW structural branch. Validate >=100g vs passive-caller lineage (v47/v51/v62) — converting thin checks to bets risks value-owning calling stations.
- **v86**: exploit_dispatch AND-gate (call_down_flop_turn>=0.55 AND fold_turn<=0.30) — if firing count is zero post-commit >=100g, relax to single-street threshold like v75's original.

