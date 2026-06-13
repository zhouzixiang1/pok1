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
- **HARD GATE: Isolate one mechanism per generation.** Violated at v64 (2 preflop) and v65 (3 mechanisms). Multi-mechanism gens create compound evaluation failures.
- Branch from current top-rated stable bots; exclude high-RD bots (rd>100).
- Extra fold branches added outside declared task scope are a recurring pattern — must be explicitly targeted and tested.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- strategy.py at ~1767 lines (~205 lines headroom). Consider splitting into turn_aggression.py within 2–3 generations.

## RECENT_LESSONS
- **v70**: Critic evidence: H2H weaknesses: v69 at 49.25% WR (400 games), down from v62's 50.67% (1940 games). Losing to v26 (40%, 10g), v15 (40%, 20g), v24 (40%, 20g), v23/v53/v51 (40%, 10g each). While sample sizes are small, the WR trend is downward from v62 baseline.; Experience pool refs: POSTFLOP_STRATEGY: 'All river value-bet blocks must include opponent-model gating.' — new code uses call_happy flag for tier 2/3 sizing. POSTFLOP_STRATEGY: 'Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.' — v69's SPR≥8 unconditional jam was the regression. PARAMETER_TUNING [EXHAUSTED] does NOT apply — this is structural SPR-tier logic, not constant tuning.; Diff refs: strategy.py evaluate_river_jam(): v69 L969-972 'if spr >= 8.0: return -2 (jam)' → v70 3-tier SPR (L967-990) with no-jam at SPR>6. BUG-3 fallback (L1810-1819): v69 auto-jam → v70 SPR-aware sized bet with jam as last resort only.
- **v70**: Critic evidence: H2H weaknesses: No H2H data available for v69/v70 matchups. No confirmed weakness in thin value betting with weak pairs was cited.; Experience pool refs: PARAMETER_TUNING section: 'Constant/margin tuning of fold gates, call thresholds, sizing ratios attempted across 5+ versions (v55–v63) with no sustained gain. Reject tasks that only adjust these without structural rationale or H2H backing. [EXHAUSTED — hard gate]', POSTFLOP_STRATEGY section: 'should_fold_postflop has ~11 fold exits — additional paths risk compounding; justify each with H2H.', v67 TODO still unaddressed: 'Dead code: sizing_hint in evaluate_turn_checkraise() ignored by choose_raise(). Wire turn_cr_info as sizing override so bluff CRs get intended 0.45–0.55× instead of generic ~0.75× pot.'; Diff refs: postflop.py L1109-1115: pair-type quality gate blocks bottom_pair/underpair/board_pair from thin value, plus 0.42 thin tier floor, strategy.py L1784-1793: river_weak_pair_gate checks back unclassified weak hands (made_strength<0.42, draw<0.12, no value profile), strategy.py already has 3 weak-pair gates at L1606-1613 (weak_pair_river, weak_bottom_pair_barrel, weak_pair_after_raise_barrel) that fire BEFORE the new gates
- **v69**: Structural hand-playability checks (pair/suited/high-card/connected) as preflop SB defense floor — sound pattern. Monitor first 100 daemon games: if wide SB ranges bleed chips postflop, tighten by removing `low >= 8` condition. Target was 25–30% preflop fold rate drop — verify actual.
- **v69**: v62 plateau at ~50.7% WR (1700g); v68 at 49.7% WR (320g). HARD GATE compliant (one mechanism). Follows `_bb_defend_vs_raise()` validated pattern.
- **v68**: River jam gating via `evaluate_river_jam()` — SPR-based (≥8.0 jam, moderate SPR sized 1.25–1.40× pot). Monitor vs calling stations (oversized bets) and passive opponents (may need tighter thresholds).
- **v67**: Dead code: `sizing_hint` in evaluate_turn_checkraise() ignored by choose_raise(). Wire turn_cr_info as sizing override so bluff CRs get intended 0.45–0.55× instead of generic ~0.75× pot.


