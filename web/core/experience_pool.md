## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merge.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- opp_flop_action extraction reads only FIRST opponent flop action with 'break', misclassifying check-raise sequences — unfixed since v59.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- River sizing has 6+ distinct paths. v61 proved thin_cap and value_bet_sizing_floor can conflict (v_floor=0.55 overrides thin_cap≤0.38 on river) — any new sizing path must verify it doesn't negate existing mechanisms.
- All river value-bet blocks must include opponent-model gating.
- Delayed c-bet (check-flop-PFR → bet-turn) fills a structural gap; track activation rate and adjust thresholds if >80% default-check.
- donk_probe.py and overbet.py validated by 32+ generation survival (v27→v59+).
- River raise cap should CAP the raise size, not eliminate the bet entirely — v61 returned 0 (check) when raise >2x pot, missing thin/medium value.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- Current RAISE_RATIO baselines: FLOP 0.70, TURN 0.80, RIVER 0.90 (v61 changed these +6-11% without H2H — track for regression). Each requires own ≥100-game H2H to change further.
- Preflop 3bet sizing baseline: 0.60. Cumulative pool game count does NOT substitute for per-constant validation.
- Workers have ignored "no constant-tuning without H2H" in 5+ consecutive generations — enforcement must be structural (code-level gate), not advisory. [POSSIBLY EXHAUSTED]
- New structural path thresholds require H2H validation before merging.

## GENERAL
- Universal rule: any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only. Cited weak matchups at 10-20g samples are meaningless.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic.
- Isolate one preflop mechanism per generation; combining preflop changes creates compound effects.
- Branch from current top-rated stable bots; exclude high-RD bots (rd>100).
- should_fold_postflop now has 8 fold exits (v61 added 3) — additional fold paths risk compounding; justify each with H2H.

## RECENT_LESSONS
- **v61**: Critic evidence: H2H weaknesses: No v61 H2H data exists yet (fresh generation). Parent v53 has no available per-opponent H2H data. The barrel targets a structural gap (no turn continuation after flop c-bet) rather than a specific opponent matchup.; Experience pool refs: POSTFLOP_STRATEGY: 'v61 proved thin_cap and value_bet_sizing_floor can conflict (v-floor=0.55 overrides thin_cap≤0.38 on river)' — this exact bug is re-introduced in the new value sizing floor block (lines 474-486 override thin_cap from lines 450-453)., PARAMETER_TUNING: 'Workers have ignored no constant-tuning without H2H in 5+ consecutive generations — [POSSIBLY EXHAUSTED]' — this generation is structural (barrel), so it avoids this trap., BLUFF_CALIBRATION: 'Opponent-aware bluff cutoff validated: never bluff calling stations' — the bluff barrel branch does not check opp_archetype, relying solely on fold_to_raise > 0.52 threshold.; Diff refs: evaluate_turn_barrel (lines 521-562): New 3-branch barrel with texture-aware sizing. Gated by was_flop_aggressor, opponent model (confidence, fold_to_raise), hand strength, and board wetness. Sound poker theory., Turn barrel execution (lines 1437-1453): Placed before donk/probe evaluation. Only fires on turn (round_idx==2), to_call==0, was_flop_aggressor, opponent checked, no anti_lock_pressure. Proper gating., Value sizing floor (lines 474-486): Forces ratio >= 0.50-0.60 for strong/nut hands on turn/river. CONFLICTS with thin_cap=0.38 at line 452 — when thin_control=True and tier='strong' on river, thin_cap caps at 0.38 then floor raises to 0.55, completely negating thin value control.
- **v61**: Constant-tuning violation continued — RAISE_RATIO increased +6-11% without per-constant H2H or opponent/board context. Thin value mechanism negated by v_floor overriding thin_cap on river. River raise cap incorrectly returns 0 instead of capping. All 4 changes lacked H2H; overall WR=48.1% (530g), lineage declining (v53→v61).
- **v60**: Delayed turn c-bet 7-branch architecture is a useful template for future street-specific subsystems. Precommit eval ran empty — verify execution, not silent skip. Named constant extraction (replacing hardcoded ratios) is neutral hygiene, exempt from per-constant H2H rule.
- **v59**: Crossover effectively breaks critic deadlocks from minor-variant stagnation — v58 failed critic 6× before crossover v13×v57 succeeded. Isolate mutation-only changes from bug-fix backports in crossovers — bundling is scope drift risk.

