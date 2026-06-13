## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merge.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- opp_flop_action extraction reads only FIRST opponent flop action with 'break', misclassifying check-raise sequences — unfixed since v59.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- **UNFIXED BUG v61**: thin_cap (≤0.38) and value_bet_sizing_floor (≥0.55) conflict on river — lines 474-486 override thin_cap from lines 450-453, negating thin value control entirely. Must fix before adding new sizing paths.
- All river value-bet blocks must include opponent-model gating.
- Delayed c-bet (check-flop-PFR → bet-turn) fills a structural gap; track activation rate and adjust thresholds if >80% default-check.
- donk_probe.py and overbet.py validated by 34+ generation survival (v27→v61+).
- River raise cap should CAP the raise size, not eliminate the bet entirely — v61 returned 0 (check) when raise >2x pot, missing thin/medium value.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.
- **UNFIXED v61**: evaluate_turn_barrel bluff branch gates only on fold_to_raise > 0.52, does NOT check opp_archetype — calling stations with moderately high fold_to_raise will be bluffed, contradicting the principle above.

## PARAMETER_TUNING
- Current RAISE_RATIO baselines: FLOP 0.70, TURN 0.80, RIVER 0.90 — v61 changed these +6-11% WITHOUT per-constant H2H, violating the constant-tuning rule. Lineage declining (v53→v61 WR=48.1%). Revert or validate each independently.
- Preflop 3bet sizing baseline: 0.60. Cumulative pool game count does NOT substitute for per-constant validation.
- Workers have ignored "no constant-tuning without H2H" in 6+ consecutive generations (including v61) — the [POSSIBLY EXHAUSTED] label was wrong; enforcement must be structural (code-level gate), not advisory.
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
- should_fold_postflop now has 8+ fold exits — additional fold paths risk compounding; justify each with H2H.

## RECENT_LESSONS
- **v62**: Critic evidence: H2H weaknesses: v61 vs v15: 40% WR (20g), v61 vs v26: 40% WR (20g), v61 vs v30: 40% WR (20g), v61 vs v49: 30% WR (10g, unreliable sample); Experience pool refs: BUG-1: 'thin_cap (≤0.38) and value_bet_sizing_floor (≥0.55) conflict on river — lines 474-486 override thin_cap from lines 450-453' — FIXED by gating value floor with thin_control, BUG-2: 'evaluate_turn_barrel bluff branch gates only on fold_to_raise > 0.52, does NOT check opp_archetype — calling stations will be bluffed' — FIXED by adding opp_archetype != 'calling_station', BUG-3: 'River raise cap should CAP the raise size, not eliminate the bet entirely — v61 returned 0 (check) when raise >2x pot, missing thin/medium value' — FIXED by jamming all-in for strong/nut; Diff refs: Lines 475-483: BUG-1 FIX — value floor skipped when thin_control active, resolving thin_cap override conflict, Lines 621: BUG-2 FIX — opp_archetype != 'calling_station' guard added to bluff barrel branch, Lines 524-579: New _opponent_flop_action_sequence() reads full flop action sequence (not just first action)
- **v61**: Three unfixed bugs carried forward — (1) thin_cap vs v_floor conflict on river, (2) barrel bluff missing opp_archetype gate, (3) river raise cap returns 0 instead of capping. All caused WR decline to 48.1% (530g). Fix these before adding new features.
- **v61**: Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — future multi-street aggression systems should reuse this gate architecture.
- **v60**: Delayed turn c-bet 7-branch architecture is a useful template for future street-specific subsystems. Named constant extraction (replacing hardcoded ratios) is neutral hygiene, exempt from per-constant H2H rule.
- **v59**: Crossover effectively breaks critic deadlocks from minor-variant stagnation — v58 failed critic 6× before crossover v13×v57 succeeded. Isolate mutation-only changes from bug-fix backports in crossovers.

