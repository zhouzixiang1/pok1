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
- donk_probe.py and overbet.py validated by 39+ generation survival (v27→v65+).
- should_fold_postflop has ~13 fold exits — additional fold paths risk compounding; justify each new exit with H2H.
- Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse for future multi-street aggression.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- RAISE_RATIO changes require per-constant H2H validation; batch changes obscure which value helped.
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
- **v66**: Critic evidence: H2H weaknesses: v65 overall WR 49.1% over 450g — near-plateau. All individual matchups are 10-20g noise samples (40-60% range). No specific H2H weakness identifiable, but plateau warrants structural exploration per scoring rules.; Experience pool refs: v65 lesson: 'Check-back branch may surrender too many pots vs calling stations — consider texture-aware delayed c-bet on turn vs passive opponents.' — directly implemented., POSTFLOP_STRATEGY: 'Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse for future multi-street aggression.' — followed., GENERAL HARD GATE: 'Isolate one mechanism per generation.' — single mechanism (delayed c-bet).; Diff refs: New function evaluate_delayed_cbet() at lines 617-657: three paths (value/semi-bluff/bluff) with opponent-model and texture gating., Call site at line 1548-1564: fires on turn when PFR checked flop, opponent checked behind, to_call==0, no anti-lock pressure. Returns raise amount before falling through to donk/probe logic., Mutually exclusive with evaluate_turn_barrel() which requires was_flop_aggressor=True (line 540), while delayed c-bet requires was_pfr_turn AND NOT was_flop_aggressor.
- **v66**: Critic evidence: H2H weaknesses: v65 overall: 49.14% WR over 350 games (bot_stats.json) — no statistically significant weakness identified, All individual v65 H2H matchups are 10-20 game samples: v13 40% (10g), v20 40% (10g), v22 40% (10g), v30 40% (10g), v53 40% (10g) — all noise, cannot identify river aggression as a leak; Experience pool refs: PARAMETER_TUNING: 'Constant/margin tuning of fold gates, call thresholds, and sizing ratios attempted across 5+ versions (v55–v63) with no sustained gain. Reject any task that only adjusts these without structural rationale or H2H backing. [EXHAUSTED — hard gate]', v65 lesson: 'Near-plateau: 97% matchups within 45–55%, no specific weakness targeted.', should_fold_postflop: 'has ~13 fold exits — additional fold paths risk compounding; justify each new exit with H2H'; Diff refs: New function river_value_gate() at lines 1663-1696: gates voluntary river raises (round_idx==3, to_call==0) by made_strength >= 0.38, Call site at line 1618: inserted BEFORE existing `win_rate >= medium or semi_bluff or blocker_bluff...` gate, Does NOT block river_showdown_extraction() (runs at line 1460, before the gate) or bluff paths (blocker_bluff/semi_bluff pass through)
- **v65**: Multi-mechanism gen violated HARD GATE. BB defense `preflop_strength >= 0.30` is dead code (min=0.319). Master task 'lower SB fold threshold' not implemented. evaluate_flop_cbet() missing v57's thin value/positional awareness. +95 lines to strategy.py despite capacity warning.
- **v65**: Check-back branch may surrender too many pots vs calling stations — consider texture-aware delayed c-bet on turn vs passive opponents.
- **v65**: Near-plateau: 97% matchups within 45–55%, no specific weakness targeted. PARAMETER_TUNING EXHAUSTED hard gate takes priority over any threshold-only tuning regardless of hand-tuned origin.
- **v64**: spot_info parameter unused in `_bb_3bet_polarization()` — dead parameters in hot paths signal incomplete wiring. Thin-value 3-bet sizing delta (-0.05 vs wide) is counterintuitive; validate via H2H.
- **v63**: Auto-call strong/nut vs all-in fixed (v62 bug). New fold exits (ultra-weak <0.15, turn small-bet <0.22) added without H2H — monitor over-folding vs LAG probes.


