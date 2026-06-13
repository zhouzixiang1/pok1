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
- Delayed c-bet (PFR checks flop, bets turn after check-behind) is structurally valid but verify frequencies don't over-bluff on dry textures.

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
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly in next gen.

## RECENT_LESSONS
- **v67**: Critic evidence: H2H weaknesses: v66 has only 380 total games at 49.7% WR — insufficient sample for specific weakness identification, Lowest matchups (v20, v25, v31, v13, v61) are at exactly 40% WR but with only 10 games each — noise, not signal, No specific H2H pattern cited to justify non-PFR turn check-raise as a weakness; change is exploratory structural addition; Experience pool refs: PARAMETER_TUNING EXHAUSTED hard gate — this change correctly avoids constant tuning and adds structural logic instead, HARD GATE: Isolate one mechanism per generation — COMPLIANT (single new function, no fold/call/constant changes), 'Turn barrel activation gated on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse for future multi-street aggression' — this change extends that pattern to non-PFR turn aggression; Diff refs: New function `evaluate_turn_checkraise()` (lines 617–656): 3 branches with opponent-model gating, archetype filtering (no bluff vs calling_station), fold equity requirements, Wired into raise gate at round_idx==2 with `not was_pfr` guard (lines 1358–1366) — only activates for non-PFR on turn, `pressure_line=flop_checkraise_exploit or turn_checkraise` passed to choose_raise (line 1394) — sizing determined by choose_raise, not by evaluate_turn_checkraise's sizing_hint
- **v66**: Delayed c-bet implemented as single mechanism (HARD GATE compliant). Critic initially rejected then passed — verify sizing/frequency not over-bluffing on dry textures.
- **v66**: Wire `has_position` in `evaluate_delayed_cbet()` to differentiate OOP delayed c-bet (smaller, merged) from IP (larger, polarized).
- **v66**: River value gate (`made_strength >= 0.38`) added but plateau persists at ~49% WR — no clear H2H gain from this mechanism alone.
- **v65**: Multi-mechanism gen violated HARD GATE — dead code (`preflop_strength >= 0.30`), unimplemented Master task, missing v57 flop c-bet features, +95 lines to strategy.py.
- **v65**: Near-plateau (97% matchups within 45–55%) — PARAMETER_TUNING EXHAUSTED hard gate takes priority over threshold-only tuning.
- **v64**: `spot_info` parameter unused in `_bb_3bet_polarization()`; thin-value 3-bet sizing delta is counterintuitive — validate via H2H.

