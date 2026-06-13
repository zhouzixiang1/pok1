## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H.
- EQR barrel adjustment belongs in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG); verify on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with a value bonus is exploitable.
- Barrel/bluff branches have a recurring blind spot for the calling_station archetype — check ALL paths.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- `should_fold_postflop` was refactored to ~4 clean exits (v72); adding more defensive fold gates / postflop protection is redundant — consolidate first. [POSSIBLY EXHAUSTED]
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
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- HARD GATE: Isolate one mechanism per generation, except sanctioned crossover diversity rescues.
- Branch from top-rated low-RD bots (rd≤100); if the top lineage is declining vs older bots, prioritize crossover for diversity over deepening an over-fit lineage.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Removed modules (`donk_probe.py`, `overbet.py`) were pruned in the v72 refactor; if reintroducing, require fresh H2H — prior survival data no longer applies.

## RECENT_LESSONS
- **v72**: board_range_filter recomputes estimate_preflop_strength/made_hand_metric/draw_potential per combo that combo_range_weight already computed one call earlier — future Master should thread those values through rather than recompute, halving build_opponent_range cost.
- **v72**: v30 out-rated v61 (389.4 vs 343.1) despite older lineage purely on game count — Glicko r is not comparable across bots with different sample sizes; select crossover parents by H2H win-rate, not raw r.
- **v72 归档建议**: Once v72 accumulates games, verify its H2H specifically against v34/v48/v41 (v30's historically strong matchups where v61 was weakest, e.g. 45.38% vs v34) — if the range filter transferred v30's edge those should clear v61's baselines; if not, the recomputation-heavy board_range_filter is dead weight worth optimizing or reverting.
- **v72**: Critic evidence: H2H weaknesses: v61 vs v34: 45.38% WR (130 games) — v61's WEAKEST matchup. v30 vs v34: 51.13% WR (530 games). ~6% gap directly attributable to range estimation quality., v61 vs v29: 47.27% WR (110 games) — second weakest. v61 vs v21: 47.78% WR (180 games). These opponents likely exploit v61's inferior range estimation., v61 vs v41: 49.0% WR (100 games). v30 vs v41: 51.2% WR (500 games). Secondary target gap.; Experience pool refs: Experience pool tags constant/margin tuning as [POSSIBLY EXHAUSTED] — this crossover avoids that by introducing a structural function, not constant adjustment., RECENT_LESSONS note v30 has more established data (19130 games vs v61's 4300) and higher overall WR (50.67% vs 50.28%)., Opponent modeling lessons confirm: 'EQR barrel adjustment belongs in realized_postflop_equity()' — this change correctly targets range estimation (input), not fold logic (output).; Diff refs: simulation.py:46-81 — board_range_filter() faithfully grafted from v30 (identical logic, only docstring differs). Two filters: preflop (deprioritize trash when opp raised, gated by pfr<0.30 for the aggressive 0.10 factor) and postflop (deprioritize pure air when facing aggression)., simulation.py:93 — wired into build_opponent_range() as post-filter after combo_range_weight() per-combo weighting, before cumulative weight computation., opponent.py:36 — confidence gate 0.15→0.12 for classify_opponent_archetype(). Early archetype activation with Bayesian priors (PRIOR_VPIP=0.58) limits misclassification risk to extreme-behavior opponents only.
- **v71**: Top lineage (v71) declined vs older crossover-source bots; the root cause was the anti-lock 0.08 equity floor allowing calls/shoves at ~8% equity, not fold discipline. When top lineage declines vs older bots, prioritize crossover for diversity over deepening the over-fit lineage.
- **v72**: Sanctioned crossover diversity rescue combined older lineage material (v24/v20/v26/v48/v34/v29) with structural offensive fixes: emergency-jam EV gate via `_emergency_jam_facing_raise_ok` and sizing-exploit adjustment. Avoided the exhausted constant-tuning gate. v72 remains mid-pack (~50% WR) and needs continued offensive improvement vs older bots.
- **v70**: River SPR-aware sizing replaced the binary SPR≥8 jam that hemorrhaged chips. Keep SPR-aware, not flat, river sizing going forward (`spr > 4.0` in v72).


