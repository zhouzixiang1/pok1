## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merge.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Barrel/bluff branches have a recurring blind spot for calling_station archetype — workers must check ALL paths, not just the one just fixed.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- Delayed c-bet (check-flop-PFR → bet-turn) fills a structural gap; track activation rate and adjust thresholds if >80% default-check.
- donk_probe.py and overbet.py validated by 34+ generation survival (v27→v61+).
- should_fold_postflop has ~13 fold exits — additional fold paths risk compounding; justify each new exit with H2H.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- RAISE_RATIO changed in v61 without per-constant H2H — lineage declined (v53→v61 WR=48.1%). Current values (FLOP 0.60, TURN 0.70, RIVER 0.85) must each be validated or reverted independently.
- Preflop 3bet sizing is now split across BB_VALUE_3BET_THRESHOLD=0.58, BB_BLUFF_3BET_{LOW=0.38, HIGH=0.56, FREQ=0.30} — tune each with H2H backing.
- Workers have ignored "no constant-tuning without H2H" in 6+ consecutive generations — enforcement must be structural (code-level gate), not advisory. [POSSIBLY EXHAUSTED]
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
- Extra fold branches added outside declared task scope are a recurring pattern — fold changes must be explicitly targeted and tested, not slipped into unrelated tasks.

## RECENT_LESSONS
- **v64**: Critic evidence: H2H weaknesses: v63 worst matchups: v26/v34/v50/v57 at 40% WR, but all at only 10 games — within noise. No specific preflop 3-bet weakness identified from match data.; Experience pool refs: Experience pool warns: 'Isolate one preflop mechanism per generation' — this changes two (thin value + bluff frequency). Also: 'any new structural path requires ≥100-game H2H' — no such evidence provided. However, the change IS genuinely structural (new decision tier), not constant tuning.; Diff refs: strategy.py: New _bb_3bet_polarization() function (lines 690-764) replaces inline BB-vs-raise logic. Three tiers: premium ≥0.60 (unchanged), thin value 0.50-0.60 (NEW, gated on wide_opp check), polar bluff 0.34-0.50 (shifted range + opponent-model-aware frequency). Calling site at line 821 simply delegates.
- **v63**: Auto-call strong/nut vs all-in (fixed v62 bug where only 'nut' exempted). Pot-odds gate added to should_fold_postflop. Dead code removed (evaluate_turn_barrel, ~46 lines). New fold exits (ultra-weak eff_made<0.15, turn small-bet eff_made<0.22) added without H2H — monitor for over-folding vs LAG probes and delayed c-bets.
- **v62**: Three v61 bugs fixed — thin_cap/value_floor conflict, barrel bluff archetype check, river raise cap jam. New `_opponent_flop_action_sequence()` reads full flop action history (untested history[].round==1 parsing — validate against engine/judge.py format before adding complexity).
- **v61**: Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse for future multi-street aggression systems.

