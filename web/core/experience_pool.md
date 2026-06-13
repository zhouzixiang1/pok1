## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Barrel/bluff branches have recurring blind spot for calling_station archetype — check ALL paths.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- donk_probe.py and overbet.py validated by 45+ generation survival (v27→v71).
- should_fold_postflop has ~11 fold exits — additional paths risk compounding; justify each with H2H.
- Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse.
- Delayed c-bet (PFR checks flop, bets turn) structurally valid; wire `has_position` for OOP vs IP differentiation.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- RAISE_RATIO and threshold changes require per-constant H2H validation; batch changes obscure which value helped.
- Constant/margin tuning of fold gates, call thresholds, sizing ratios attempted across 5+ versions (v55–v63) with no sustained gain. Reject tasks that only adjust these without structural rationale or H2H backing. [EXHAUSTED — hard gate]

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- **HARD GATE: Isolate one mechanism per generation.** Multi-mechanism gens create compound evaluation failures.
- Branch from current top-rated stable bots; exclude high-RD bots (rd>100).
- Extra fold branches added outside declared task scope are a recurring pattern — must be explicitly targeted and tested.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- strategy.py approaching line budget (~1800 lines); consider splitting turn aggression into separate module before next structural addition.

## RECENT_LESSONS
- **v71**: Anti-lock emergency_jam now requires `made_strength >= 0.22` (threaded from preflop/postflop context) — ace-high shoves were dominated by any calling range. Structural constraint, NOT a fold gate or parameter tuning; exhaustion tags don't apply.
- **v71**: v70 lineage declining (439.4 Glicko, 5th place, 40% WR vs older bots). Monitor v71 H2H vs v49/v61/v62 to confirm emergency_jam fix stops late-match chip hemorrhage.
- **v70**: River SPR-tier sizing (jam<3, overbet 3-6, standard>6) replaces binary SPR≥8 jam which caused chip hemorrhage. Follow this tier pattern for future river sizing.
- **v70**: Pair-type fold gates rejected by critic (5.0) as redundant with 3 existing weak-pair protections. Do NOT add more river fold gates. [POSSIBLY EXHAUSTED]
- **v69**: Structural hand-playability checks as preflop SB defense floor. Monitor: if wide SB ranges bleed chips postflop, tighten by removing `low >= 8` condition.
