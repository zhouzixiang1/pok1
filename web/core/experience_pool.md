## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H.
- EQR barrel adjustment belongs in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG); verify each change.
- EV selectors and value-raise branches must gate raises by opponent type — raising into calling stations with a value bonus is exploitable; check ALL barrel/bluff paths for the calling_station blind spot.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- Two fold-mechanism locations, do NOT conflate: `should_fold_postflop()` threshold/exit tuning is CONFIRMED exhausted across v55–v73 (its v72 refactor never fixed the 0% postflop-fold leak), but `get_action()`-level structural commitment gates (stack_commit wired BEFORE all-in dispatch) ARE the working mechanism. Add fold work to get_action(), not to should_fold_postflop() exits. [POSSIBLY EXHAUSTED]
- Turn barrel activation on `was_flop_aggressor + to_call == 0 + opp check` is a sound structural pattern — reuse.
- Delayed c-bet (PFR checks flop, bets turn) is structurally valid; wire `has_position` for OOP vs IP differentiation.
- Keep SPR-aware (tiered), not flat SPR≥8, river sizing — the flat jam hemorrhaged chips (v70→v72 `spr > 4.0`).

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- RAISE_RATIO and threshold changes require per-constant H2H validation; batch changes obscure which value helped.
- Constant/margin tuning of fold gates, call thresholds, sizing ratios across v55–v73 yielded no sustained gain (re-confirmed at v73). Reject constant-only tasks without structural rationale or H2H backing. [POSSIBLY EXHAUSTED]
- Emergency/commitment jam handling: v72 correctly gates emergency-jam via `_emergency_jam_facing_raise_ok` (pot-odds + opponent-model), NOT a raw `made_strength` threshold. v73 reintroduced a raw `made_strength<0.50` cutoff in the get_action() commitment gate that folds legitimate top-pair calls and is exploitable by polarized bluffers. Next-gen MUST replace the raw threshold with pot-odds-grounded comparison (`made_strength + draw_potential < pot_odds_required − 0.05`, floor 0.40) — do not reintroduce raw-threshold jam gating.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across different sample sizes. When the top lineage declines vs older bots, prioritize crossover for diversity over deepening an over-fit lineage.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H effect.
- HARD GATE: Isolate one mechanism per generation, except sanctioned crossover diversity rescues.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Removed modules (`donk_probe.py`, `overbet.py`) were pruned in v72; reintroducing requires fresh H2H — prior survival data no longer applies.

## RECENT_LESSONS
- **v74**: Critic evidence: H2H weaknesses: v73 overall: 380g, 48.42% WR (low sample). Weakest: v15 30%/10g, v19 35%/20g, v25 35%/20g — all extremely low-sample, likely noise. No specific evidence that value sizing was the problem.; Experience pool refs: 'Constant/margin tuning of sizing ratios across v55-v73 yielded no sustained gain [POSSIBLY EXHAUSTED]' — but this change is structural routing, not constant tuning, 'Turn barrel activation on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern — reuse' — barrel logic endorsed, 'v73: made_strength<0.50 commitment gate folds legitimate top-pair calls, exploitable — next gate must use pot-odds comparison' — #1 leak NOT addressed this gen; Diff refs: strategy.py:1462-1468 (v73) → 1470-1483 (v74): barrel dispatch changed from raw int(pot*barrel_ratio) to choose_raise(..., sizing_hint=barrel['sizing_hint'], nutted_risk_score=..., match_sizing_delta=...), strategy.py:474-494: value sizing floor extended from round_idx>=2 to round_idx>=1, adding flop tier 0.45x, strategy.py:378+405-408: new sizing_hint param overrides round_idx base ratio while preserving all subsequent adjustments
- **v73**: 0% postflop fold persisted through v55–v72 despite threshold tuning AND the EV-based should_fold_postflop refactor — confirmed: only get_action()-level structural commitment gates (stack_commit ≥0.35 wired before all-in dispatch) move the needle; should_fold_postflop threshold approaches are fully exhausted.
- **v73**: made_strength<0.50 in the commitment gate folds legitimate top-pair calls and is exploitable by polarized bluffers — next gate must fire when `made_strength + draw_potential < pot_odds_required − 0.05` and floor made_strength at 0.40.
- **v73 evidence**: 0% postflop fold is the #1 leak — battle_experience.md confirms 0% every street/leg across v13→v72 (~915 pairs, ~26K games); weakest v72 matchups v40 (30%/10g), v14 (36.7%/30g); −19999 losses are the binary-win/loss pattern (~5 big losses/10g even in neutral pairs).
- **v72**: board_range_filter recomputes per-combo metrics (estimate_preflop_strength/made_hand_metric/draw_potential) that combo_range_weight already computed one call earlier — thread through rather than recompute, halving build_opponent_range cost.
- **v72**: Crossover targeted range estimation (input) not fold logic (output) — v61's weakest matchups align with v30's strengths (v34 45.38% vs 51.13%, v29 47.27%, v21 47.78%); ~6% gap attributable to range estimation quality. Verify H2H vs v34/v48/v41 once games accumulate — else board_range_filter is dead weight to optimize/revert.
- **v72**: Sanctioned diversity rescue combined older-lineage material with structural offensive fixes (emergency-jam EV gate via `_emergency_jam_facing_raise_ok`, sizing-exploit adjustment), avoiding the exhausted constant-tuning gate; remains ~50% WR, needs continued offensive improvement vs older bots.
- **v71**: Top-lineage decline vs older bots was caused by the anti-lock 0.08 equity floor (calls/shoves at ~8% equity), not fold discipline — fix the floor, not the folds.

