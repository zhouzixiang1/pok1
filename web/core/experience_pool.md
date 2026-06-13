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
- donk_probe.py and overbet.py validated by 44+ generation survival (v27→v70).
- should_fold_postflop has ~11 fold exits — additional paths risk compounding; justify each with H2H.
- Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse.
- Delayed c-bet (PFR checks flop, bets turn) structurally valid; wire `has_position` for OOP vs IP differentiation.
- Dead code: `sizing_hint` in evaluate_turn_checkraise() ignored by choose_raise() — wire turn_cr_info as sizing override for bluff CRs (0.45–0.55× pot).

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
- **HARD GATE: Isolate one mechanism per generation.** Violated at v64 (2 preflop) and v65 (3 mechanisms). Multi-mechanism gens create compound evaluation failures.
- Branch from current top-rated stable bots; exclude high-RD bots (rd>100).
- Extra fold branches added outside declared task scope are a recurring pattern — must be explicitly targeted and tested.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- strategy.py approaching line budget (~1800 lines); consider splitting turn aggression into separate module before next structural addition.

## RECENT_LESSONS
- **v71**: Critic evidence: H2H weaknesses: v70 is at 40.0% WR vs claude_v17, v13, v16, v20, v49, v61, v62, v34 (10 games each) and 45.0% vs v15 (20 games), indicating a declining trend against older bots., v70 Glicko rating is 439.4 (5th place) with RD 85.6, down from the top cluster, consistent with the v69/v70 lineage leaking chips.; Experience pool refs: v70 lesson: 'River SPR-tier sizing (jam<3, overbet 3-6, standard>6) replaces binary SPR≥8 jam which caused chip hemorrhage (-15829 on missed-draw shove).' The current change tightens the same anti-lock/jam path., Pair-type fold gates are marked [POSSIBLY EXHAUSTED]; this change is not a fold gate—it constrains an aggressive jam—so the exhaustion tag does not apply., Parameter tuning of fold gates/sizing ratios across v55–v63 is [EXHAUSTED]; this is a structural constraint, not parameter tuning.; Diff refs: strategy.py `choose_anti_lock_pressure_action`: new `made_strength` parameter threaded through all call sites (preflop=0.0, postflop=made_hand_metric)., New `weak_emergency` condition requires `made_strength >= 0.22` in addition to `weak_showdown`, `high_fold_pressure`, and `hands_left <= 6`., Emergency jam now uses the narrower `weak_emergency` instead of the previous inline `(weak_showdown and high_fold_pressure and hands_left <= 6)`.
- **v70**: River SPR-tier sizing (jam<3, overbet 3-6, standard>6) replaces binary SPR≥8 jam which caused chip hemorrhage (-15829 on missed-draw shove). Follow this tier pattern for future river sizing.
- **v70**: Pair-type fold gates are exhausted — critic rejected (5.0) as redundant with 3 existing weak-pair protections. Do NOT add more river fold gates. [POSSIBLY EXHAUSTED]
- **v70**: v69 H2H WR trending down (49.25% vs v62's 50.67%). Losing to older bots (v26/v15/v24 at 40% WR, small samples). Monitor if SPR-tier sizing stops the decline.
- **v69**: Structural hand-playability checks as preflop SB defense floor. Monitor first 100 daemon games: if wide SB ranges bleed chips postflop, tighten by removing `low >= 8` condition.
- **v68**: River jam gating via `evaluate_river_jam()` — SPR-based. Monitor vs calling stations (oversized bets) and passive opponents (may need tighter thresholds).

