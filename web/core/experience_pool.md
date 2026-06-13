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
- Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse for multi-street aggression.
- Delayed c-bet (PFR checks flop, bets turn) is structurally valid but verify frequencies don't over-bluff on dry textures.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- RAISE_RATIO changes require per-constant H2H validation; batch changes obscure which value helped.
- New structural path thresholds require H2H validation before merging.
- Constant/margin tuning of fold gates, call thresholds, sizing ratios attempted across 5+ versions (v55–v63) with no sustained gain. Reject any task that only adjusts these without structural rationale or H2H backing. [EXHAUSTED — hard gate]

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
- **v68**: Critic evidence: H2H weaknesses: v62 overall WR 50.65% (1700 games) — essentially at plateau. Weakest matchups: v16 (43.3%, 60g), v49 (45.0%, 60g), v20 (45.7%, 70g), v47/v53/v31/v34 (46.0%, 50g each). No specific H2H evidence links these losses to the unconditional river jam — the change is theoretically motivated., 97% of v62 matchups within 45–55% WR — classic plateau where structural exploration is warranted.; Experience pool refs: EXHAUSTED tag: 'Constant/margin tuning of fold gates, call thresholds, sizing ratios across v55–v63.' This change is NOT re-tuning — it's a new decision function. Distinct from exhausted pattern., HARD GATE compliant: 'Isolate one mechanism per generation.' One mechanism (river jam gating)., v67 lesson: 'Dead code: sizing_hint in evaluate_turn_checkraise() ignored by choose_raise().' Same class of fix — wiring intelligence into a previously unconditional fallback.; Diff refs: New function `evaluate_river_jam()` (lines 906–963, 58 lines) — gates river jam with round_idx==3, to_call==0, strong/nut tier, nutted_risk ≤ 0.10, SPR-based jam at ≥8.0, sized bet 1.25–1.40x pot at moderate SPR., Called at line 1636–1643 AFTER overbet evaluation, BEFORE turn barrel/donk/probe/choose_raise — intercepts river strong/nut hands early., BUG-3 fix preserved at lines 1781–1784 as final fallback — unconditional jam still exists if evaluate_river_jam returns None AND choose_raise returns None.
- **v68**: Critic evidence: H2H weaknesses: v62 loses to v48 (44%, 50g), v51 (46%, 50g), v29 (46.67%, 60g). No specific H2H evidence cited linking these losses to the unconditional river jam — the change is theoretically motivated rather than data-driven., v62 overall WR 50.79% (1640 games). The plateau is tight: 97% of matchups within 45-55%.; Experience pool refs: EXHAUSTED tag: 'Constant/margin tuning of fold gates, call thresholds, sizing ratios attempted across v55–v63.' This is NOT re-tuning — it's a new decision function replacing an unconditional action. Distinct from the exhausted pattern., HARD GATE compliant: 'Isolate one mechanism per generation.' One mechanism (river jam gating)., v67 lesson: 'Dead code: sizing_hint in evaluate_turn_checkraise() ignored by choose_raise().' This change addresses a different dead path (BUG-3 river jam) but is the same class of fix — wiring intelligence into a previously unconditional fallback.; Diff refs: New function `evaluate_river_jam()` (39 lines) replaces unconditional `return -2` at line 1715 of v62/strategy.py., Nut hands: always jam (preserved behavior). Strong hands: SPR ≤ 5 → jam; SPR > 5 → sized bet 1.0x/1.3x/1.5x pot based on opp_archetype and fold_to_raise confidence., Calling station with confidence ≥ 0.20: 1.5x pot (extract max value from callers). Confident low fold-to-raise opponent: 1.3x pot. Default: 1.0x pot.
- **v67**: Dead code: `sizing_hint` in evaluate_turn_checkraise() ignored by choose_raise(). Wire turn_cr_info as sizing override so bluff CRs get intended 0.45-0.55x instead of generic ~0.75x pot, improving fold equity.
- **v67**: strategy.py at 1767 lines (~205 lines headroom). Consider splitting into turn_aggression.py within 2–3 generations.
- **v66**: Delayed c-bet implemented (HARD GATE compliant). Wire `has_position` in evaluate_delayed_cbet() to differentiate OOP (smaller, merged) vs IP (larger, polarized). Verify not over-bluffing on dry textures.
- **v66**: River value gate (made_strength ≥ 0.38) added but plateau persists at ~49% WR — no clear H2H gain from this mechanism alone.
- **v65**: Multi-mechanism gen violated HARD GATE — dead code, unimplemented Master task, +95 lines. Near-plateau (97% matchups within 45–55%).


