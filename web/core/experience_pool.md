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
- donk_probe.py and overbet.py validated by 41+ generation survival (v27→v67).
- should_fold_postflop has ~11 fold exits — additional paths risk compounding; justify each with H2H.
- Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse.
- Delayed c-bet (PFR checks flop, bets turn) structurally valid; verify frequencies don't over-bluff on dry textures.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- RAISE_RATIO changes require per-constant H2H validation; batch changes obscure which value helped.
- New structural path thresholds require H2H validation before merging.
- Constant/margin tuning of fold gates, call thresholds, sizing ratios attempted across 5+ versions (historical v55–v63) with no sustained gain. Reject tasks that only adjust these without structural rationale or H2H backing. [EXHAUSTED — hard gate]

## GENERAL
- Universal rule: any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic.
- **HARD GATE: Isolate one mechanism per generation.** Violated at v64 (2 preflop) and v65 (3 mechanisms). Multi-mechanism gens create compound evaluation failures.
- Branch from current top-rated stable bots; exclude high-RD bots (rd>100).
- Extra fold branches added outside declared task scope are a recurring pattern — must be explicitly targeted and tested.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.

## RECENT_LESSONS
- **v69**: Critic evidence: H2H weaknesses: v68 at 49.7% WR (320 games) — below the v62 plateau of ~50.7%. No specific matchup data showing SB fold leak, but the ~1% WR gap suggests room for improvement in preflop spots. No v69 H2H data yet.; Experience pool refs: EXHAUSTED tag on 'constant/margin tuning of fold gates' — but this is NOT constant tuning, it's a new structural mechanism. The pool also notes 'structural exploration warranted' at the plateau, which this satisfies., Pool notes `_bb_defend_vs_raise()` validated over 41+ generations — the new function follows the same pattern., HARD GATE confirmed: one mechanism per generation (v64/v65 violated this; v69 complies).; Diff refs: New function `_sb_open_defense_floor()` (lines 736-759) — 24-line structural hand classifier using preflop_hand_profile., Single call site change (lines 790-792): replaces unconditional `return -1` with conditional `if _sb_open_defense_floor(my_cards): return 0` before `return -1`., Mirrors existing `_bb_defend_vs_raise()` pattern at line 704 — same profile-based approach but slightly wider (3:1 odds justify wider range than BB's ~2:1 on a raise).
- **v68**: River jam gating via `evaluate_river_jam()` — SPR-based (≥8.0 jam, moderate SPR sized bet 1.25–1.40× pot). Monitor vs calling stations (oversized bets extract max value) and passive opponents (may need tighter hand thresholds to avoid turning value into bluff).
- **v68**: v62 plateau at ~50.7% WR (1700g), 97% matchups within 45–55%. Structural exploration warranted but changes must be data-driven, not only theoretically motivated.
- **v67**: Dead code: `sizing_hint` in evaluate_turn_checkraise() ignored by choose_raise(). Wire turn_cr_info as sizing override so bluff CRs get intended 0.45–0.55× instead of generic ~0.75× pot.
- **v67**: strategy.py at 1767 lines (~205 lines headroom). Consider splitting into turn_aggression.py within 2–3 generations.
- **v66**: Delayed c-bet implemented (HARD GATE compliant). Wire `has_position` to differentiate OOP (smaller, merged) vs IP (larger, polarized). Verify not over-bluffing on dry textures.
- **v66**: River value gate (made_strength ≥ 0.38) added but plateau persists at ~49% WR — no clear H2H gain from this mechanism alone.

