## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merge.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Barrel/bluff branches have recurring blind spot for calling_station archetype — workers must check ALL paths.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- donk_probe.py and overbet.py validated by 38+ generation survival (v27→v64+).
- should_fold_postflop has ~13 fold exits — additional fold paths risk compounding; justify each new exit with H2H.
- Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse for future multi-street aggression.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- RAISE_RATIO changes require per-constant H2H validation; batch changes without individual testing obscure which value helped.
- BB 3-bet uses `_bb_3bet_polarization()` with three tiers (premium/thin-value/polar-bluff) — tune each tier independently with H2H backing.
- New structural path thresholds require H2H validation before merging.
- Constant/margin tuning of fold gates, call thresholds, and sizing ratios attempted across 5+ versions (v55–v63) with no sustained gain. Reject any task that only adjusts these without structural rationale or H2H backing. [EXHAUSTED — hard gate]

## GENERAL
- Universal rule: any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic.
- **HARD GATE: Isolate one mechanism per generation.** Violated at v64 (2 preflop mechanisms) and v65 (3: postflop + BB defense + SB fold). Multi-mechanism gens create compound evaluation failures — Master must enforce via task scoping.
- Branch from current top-rated stable bots; exclude high-RD bots (rd>100).
- Extra fold branches added outside declared task scope are a recurring pattern — fold changes must be explicitly targeted and tested.

## RECENT_LESSONS
- **v65**: Critic evidence: H2H weaknesses: v61 shows no matchups below 45% — lowest is vs claude_v29 at 45.0% (40 games, unreliable sample). All matchups are 45–55%. No specific weakness targeted.; Experience pool refs: EXHAUSTED tag: 'Constant/margin tuning of fold gates, call thresholds, and sizing ratios attempted across 5+ versions (v55–v63) with no sustained gain.' — While this is structural, the 0.44/0.48/0.22 thresholds are still hand-tuned., v65 recent lesson: 'Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.', 'Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic.' — This adds 95 lines to strategy.py (1585→1656).; Diff refs: evaluate_flop_cbet() at strategy.py:575-614: 4-branch c-bet evaluator with value (strong/nut), semi-bluff (draw_quality≥0.14 + FE), bluff (dry only, non-CS, fold_to_raise>0.48), check-back. Missing v57's thin value, positional awareness, paired/semi-connected bluffs., _was_preflop_raiser() at strategy.py:521-528: Scans history for preflop raise/allin. Near-identical to v57's version., Wiring at strategy.py:1537-1554: Fires on round_idx==1, to_call==0, was_pfr, not anti_lock, not donk/probe eligible. Returns early with pot-fraction sizing.
- **v65**: Master task 'lower SB fold threshold' NOT implemented — only dead-code guard and constant threshold added without H2H. BB defense `if preflop_strength >= 0.30` is DEAD CODE (estimate_preflop_strength min=0.319). v64 weakest matchups v13/v27 at 40% WR (10g each, unreliable sample). Near-plateau: 97% matchups within 45–55%.
- **v64**: spot_info parameter unused in `_bb_3bet_polarization()` — dead parameters in hot paths indicate incomplete wiring. Thin-value 3-bet tier uses -0.05 sizing delta vs wide opponents (counterintuitive: thinner value needs larger sizing vs callers); validate via H2H.
- **v63**: Auto-call strong/nut vs all-in (fixed v62 bug). New fold exits (ultra-weak <0.15, turn small-bet <0.22) added without H2H — monitor over-folding vs LAG probes.

