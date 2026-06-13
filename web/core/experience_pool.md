## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H.
- EQR barrel adjustment belongs in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG); verify on every change.
- EV-based selectors and value-raise branches must gate raises by opponent type — raising into calling stations with a value bonus is exploitable; check ALL barrel/bluff paths for the calling_station blind spot.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- `should_fold_postflop` was refactored to ~4 clean exits (v72); adding defensive fold gates/postflop protection is redundant — consolidate first. [POSSIBLY EXHAUSTED]
- Turn barrel activation gated on `was_flop_aggressor + to_call == 0 + opp check` is a sound structural pattern — reuse.
- Delayed c-bet (PFR checks flop, bets turn) is structurally valid; wire `has_position` for OOP vs IP differentiation.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- RAISE_RATIO and threshold changes require per-constant H2H validation; batch changes obscure which value helped.
- Constant/margin tuning of fold gates, call thresholds, sizing ratios across v55–v63 yielded no sustained gain. Reject constant-only tasks without structural rationale or H2H backing. [POSSIBLY EXHAUSTED]
- Fix the anti-lock equity floor rather than adding fold exits: the 0.08 floor let calls/shoves proceed at ~8% equity. v72 gates emergency-jam via `_emergency_jam_facing_raise_ok` (pot-odds + opponent-model), not a raw `made_strength` threshold.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across bots with different sample sizes (v30 out-rated v61 on game count despite an older lineage). When the top lineage declines vs older bots, prioritize crossover for diversity over deepening an over-fit lineage.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H effect.
- HARD GATE: Isolate one mechanism per generation, except sanctioned crossover diversity rescues.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Removed modules (`donk_probe.py`, `overbet.py`) were pruned in the v72 refactor; if reintroducing, require fresh H2H — prior survival data no longer applies.

## RECENT_LESSONS
- **v73**: Critic evidence: H2H weaknesses: v72 weakest matchups: v40 (30% WR over 10g), v14 (36.7% over 30g), v21/v27/v31/v47 (40% over 10g each); total only 290 games — most matchups at 45-55% plateau. The catastrophic -19999 chip losses cited by the Master are consistent with 'binary win/loss outcomes' pattern in battle_experience.md (~5 big losses per 10 games even in neutral pairs).; Experience pool refs: battle_experience.md: 'Zero postflop fold persists across ALL pairs (0% every street every leg). Span v13→v72, ~915+ pairs, ~26000+ games.' — confirms 0% postflop fold is v72's #1 leak., experience_pool.md: 'Street-by-street postflop folding is the highest-leverage fix.' — directly endorses this direction., experience_pool.md: '`should_fold_postflop` was refactored to ~4 clean exits (v72); adding defensive fold gates/postflop protection is redundant — consolidate first. [POSSIBLY EXHAUSTED]' — flags risk of repeating exhausted pattern, BUT battle_experience shows v72's refactor didn't actually fix the leak.; Diff refs: strategy.py:956-969 — new 14-line commitment gate inserted after archetype threshold adjustments, before opponent_allin handling (line 971) and should_fold_postflop (line 1165). Single structural addition, no constants touched (compliant with Architect role)., Gate fires when stack_commit = to_call/my_chips >= 0.35 AND marginal (tier not strong/nut AND made_strength<0.50 AND draw_strength<0.18) AND opp_archetype != 'lag'. The LAG exemption is sound (LAGs bluff wide, don't fold marginal hands to them)., The anti_lock_pressure exemption correctly avoids folding when folding would gift opponent a chip-lock — preserves v72's defensive mechanism.
- **v72**: board_range_filter recomputes estimate_preflop_strength/made_hand_metric/draw_potential per combo that combo_range_weight already computed one call earlier — thread those values through rather than recompute, halving build_opponent_range cost.
- **v72**: v61's weakest matchups align with v30's strengths — v34 (45.38% vs 51.13%), v29 (47.27%), v21 (47.78%), v41 (49.0% vs 51.2%); ~6% gap attributable to range estimation quality. The crossover correctly targets estimation (input), not fold logic (output).
- **v72**: Once v72 accumulates games, verify H2H vs v34/v48/v41 — if the range filter transferred v30's edge those should clear v61's baselines; if not, the recomputation-heavy board_range_filter is dead weight worth optimizing or reverting.
- **v72**: Sanctioned diversity rescue combined older lineage material with structural offensive fixes (emergency-jam EV gate via `_emergency_jam_facing_raise_ok`, sizing-exploit adjustment), avoiding the exhausted constant-tuning gate. v72 remains mid-pack (~50% WR) — needs continued offensive improvement vs older bots.
- **v71**: Top-lineage decline vs older bots was caused by the anti-lock 0.08 equity floor (calls/shoves at ~8% equity), not fold discipline.
- **v70**: River SPR-aware sizing replaced the binary SPR≥8 jam that hemorrhaged chips. Keep SPR-aware, not flat, river sizing (`spr > 4.0` in v72).

