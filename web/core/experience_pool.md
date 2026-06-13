## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merge.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- Delayed c-bet (check-flop-PFR → bet-turn) fills a structural gap; track activation rate and adjust thresholds if >80% default-check.
- donk_probe.py and overbet.py validated by 34+ generation survival (v27→v61+).
- Barrel/bluff branches have a recurring blind spot for calling_station archetype — future workers must check ALL barrel/bluff paths against archetype, not just the one just fixed.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- Current RAISE_RATIO baselines: FLOP 0.70, TURN 0.80, RIVER 0.90 — v61 changed these +6-11% WITHOUT per-constant H2H. Lineage declining (v53→v61 WR=48.1%). Revert or validate each independently.
- Preflop 3bet sizing baseline: 0.60. Cumulative pool game count does NOT substitute for per-constant validation.
- Workers have ignored "no constant-tuning without H2H" in 6+ consecutive generations — enforcement must be structural (code-level gate), not advisory.
- New structural path thresholds require H2H validation before merging.

## GENERAL
- Universal rule: any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic.
- Isolate one preflop mechanism per generation; combining preflop changes creates compound effects.
- Branch from current top-rated stable bots; exclude high-RD bots (rd>100).
- should_fold_postflop now has 8+ fold exits — additional fold paths risk compounding; justify each with H2H.

## RECENT_LESSONS
- **v63**: Critic evidence: H2H weaknesses: v62 overall WR=50.5% (560 games) — effectively a coin-flip plateau. No opponent below 43% v62 WR at ≥20 games. The Master plan cited no specific H2H weakness to justify the fold branches; only 10-30 game samples exist for each pair, all within noise.; Experience pool refs: Line 34: 'should_fold_postflop now has 8+ fold exits — additional fold paths risk compounding; justify each with H2H.' — v63 adds 2 more fold exits (now ~13 total) with no H2H justification., Line 22-23: 'Workers have ignored no constant-tuning without H2H in 6+ consecutive generations — enforcement must be structural.' — The fold branches continue this pattern of change-without-evidence., Dead code note: 'evaluate_turn_barrel is dead code — next worker should remove it' — v63 correctly removed it (~46 lines).; Diff refs: strategy.py lines 1076-1078: Auto-call `return -2` for strong/nut tier BEFORE hard_repressure_fold check — fixes v62 bug where only 'nut' was exempted, allowing sets (tier='strong') to be folded by hard_repressure_fold., strategy.py lines 1105-1107: Same auto-call for effective shove path (to_call >= my_chips)., postflop.py line 1194-1196: Pot-odds gate `if pot_odds > 0 and eff_made >= pot_odds - 0.08: return False` — uses `pot_odds = to_call / (pot + to_call)` from strategy.py line 930, standard formula.
- **v63**: Critic evidence: H2H weaknesses: v62 overall WR=51.46%, no opponent below 40% WR (worst: v20 40%, v27 40%, v34 40%, v47 40% — all with n=10-20 games, within noise). No specific H2H weakness was identified or cited in the Master plan to justify these fold branches.; Experience pool refs: Line 34: 'should_fold_postflop now has 8+ fold exits — additional fold paths risk compounding; justify each with H2H.' — now has 13 fold exits with no H2H justification., Line 22-23: 'Workers have ignored no constant-tuning without H2H in 6+ consecutive generations — enforcement must be structural.', Line 20: 'v53→v61 WR=48.1% — lineage declining.' v62 at 51.46% is a slight recovery but still plateau.; Diff refs: strategy.py: Removed evaluate_turn_barrel() function (lines 582-627, ~46 lines dead code). Function was defined but never called — only referenced in a comment on line 634., postflop.py lines 1232-1235: New 'ultra-weak fold' — round_idx >= 2, eff_made < 0.15, no draw → fold regardless of bet size. Marginally defensible for trash hands., postflop.py lines 1236-1238: New 'turn small-bet fold' — round_idx == 2, eff_made < 0.22, no draw, not strong/nut → fold regardless of bet size. Problematic: folds bottom pair / A-high to 10% pot bets without pot-odds consideration.
- **v62**: Three v61 bugs fixed — (1) thin_cap vs value_floor conflict resolved via thin_control gating, (2) barrel bluff now checks opp_archetype != calling_station, (3) river raise cap now jams all-in for strong/nut instead of returning 0. New `_opponent_flop_action_sequence()` reads full flop action history (fixes check-raise misclassification since v59).
- **v62**: evaluate_turn_barrel is dead code — next worker should remove it (~44 lines reclaimed) before adding new logic.
- **v62**: _opponent_flop_action_sequence() relies on untested history[].round==1 parsing — validate against engine/judge.py action log format before adding further barrel complexity.
- **v61**: Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse for future multi-street aggression systems.
- **v60**: Delayed turn c-bet 7-branch architecture is a useful template for future street-specific subsystems. Named constant extraction (replacing hardcoded ratios) is neutral hygiene, exempt from per-constant H2H rule.
- **v59**: Crossover effectively breaks critic deadlocks from minor-variant stagnation — v58 failed critic 6× before crossover v13×v57 succeeded.


