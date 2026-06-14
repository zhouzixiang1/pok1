## OPPONENT_MODELING
- Gate ALL raise/barrel/bluff/value branches by opponent type — failing to adjust sizing/lines vs calling stations is the blind spot across every action path.
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H.
- The LIVE opponent-modeling infra in current v78 is `_aligned_signal_boost()` (per-street metric AND aggregate metric must deviate the same direction before action). Extend THIS for raise-size/value-sizing signals — `exploit_dispatch`/per-street fold_to_bet exist only on the bypassed v75→v77 lineage, NOT in v78.
- EQR barrel adjustment belongs in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG); verify each change.

## POSTFLOP_STRATEGY
- **Fold-mechanism (canonical + v78 gap)**: `should_fold_postflop()` threshold/exit tuning never fixed the 0% postflop-fold leak. The WORKING mechanism is a `get_action()`-level structural commitment gate (made_strength<0.50 + draw_strength<0.18 + archetype≠lag + value tier≠strong/nut). It is ABSENT from current v78 (v23×v16 crossover bypassed the v73-v77 chain, and v78 also REMOVED SPR from should_fold_postflop). Next priority: ADD the gate, pot-odds-grounded (made_strength + draw_potential < pot_odds_required − 0.05, floor made_strength 0.40; restore SPR: made_strength<0.50 + spr>2 → fold; spr<2 → never fold marginal). Don't trust raw <0.50 vs polarized bluffers. [POSSIBLY EXHAUSTED]
- Action-dispatch bypasses are a high-value discovery vector: a turn-barrel once called raw `int(pot*ratio)` instead of `choose_raise`, skipping value floors/guards. Audit every action-selection path (flop donk, river bet, check-raise sizing) for raw-ratio bypasses.
- When adding a value tier that overlaps an early-return guard (made_strength≥0.50 returns first), the new tier MUST exclude the handled band OR lower the guard — else dead code.
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing causes regression. All river value-bet blocks must include opponent-model gating.
- Turn barrel activation on `was_flop_aggressor + to_call == 0 + opp check` is a sound structural pattern — reuse. Delayed c-bet (PFR checks flop, bets turn) is structurally valid; wire `has_position` for OOP vs IP.

## BLUFF_CALIBRATION
- Never bluff calling stations; boost bluffs vs NIT (validated — all bluff/4-bet-light/barrel modules).
- Structural bluff modules (4-bet light, barrel) need ≥100-game H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Constant/margin tuning of sizing ratios and call thresholds across many generations yielded no sustained gain. Reject constant-only tasks without structural rationale or H2H backing; per-constant H2H validation required (batch changes obscure which value helped). [POSSIBLY EXHAUSTED]
- Emergency/commitment jam handling must be pot-odds + opponent-model grounded (e.g. `_emergency_jam_facing_raise_ok`), NOT a raw `made_strength` threshold.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g (esp. 10g) is directional noise only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across sample sizes; prioritize crossover diversity over deepening an over-fit lineage.
- **Version-lineage (v78 current)**: v78 is the v23×v16 crossover on a DIFFERENT lineage from the v59→v75→v77 chain. Do NOT assume v73-v77 features are present — the structural commitment gate, exploit_dispatch, donk_probe.py, overbet.py, board_range_filter, and per-street fold_to_bet tracking are ALL ABSENT in v78. Re-verify module liveness against v78's actual files before wiring/modifying.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants. Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H.
- HARD GATE: Isolate one mechanism per generation, except sanctioned crossover diversity rescues. Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Direction-Audit fold-forbiddance is a sliding-window constraint, NOT permanent — default to OFFENSIVE/structural work; surface both views when audit and match-analysis conflict.
- If top lineage declines vs older bots, suspect an anti-lock equity floor (calls/shoves at ~8% equity) before fold discipline.

## RECENT_LESSONS
- **v79**: EQR alignment-boost clamp at strategy.py:319-346 was MOVED from v17's air_hand-only context to ALL unclassified late-street hands — now caps eqr at 0.85 for strong made hands (top-pair-good-kicker, two-pair, sets) that previously returned raw win_rate. If H2H vs CS-lineage (v51/v62) shows lost value on coordinated boards, add `made_strength < 0.40` guard to restrict clamp to weak hands only.
- **v79**: 4-bet light at 70% activation frequency is aggressive — verify opp_fold_to_raise gate is tight enough vs calling stations that don't fold to 4-bets; tighten candidate whitelist if H2H vs NIT/CS shows chip bleed at ≥100 games.
- **v79 归档建议 (improvement)**: Monitor H2H vs CS-lineage (v51/v62) at ≥100 games: the broadened EQR clamp (0.85 cap now applied to strong made hands, not just air_hands) risks lost value vs calling stations on coordinated boards — if regression appears, restrict the clamp with a `made_strength < 0.40` guard at strategy.py:319-346.
- **v79**: Critic evidence: H2H weaknesses: v78 sits at exactly 50.0% (190-190) over 380 games — confirmed plateau; v23 vs v75 only 40 games (55%) which is directional noise. No specific opponent <40% identified, justifying sanctioned diversity crossover per experience_pool HARD GATE exception.; Experience pool refs: 'v78 (next-gen priority): RESTORE SPR commitment awareness — v78 REMOVED spr from should_fold_postflop' — v79 directly addresses this by adding spr parameter + SPR>4 uncommitted fold gate (strategy.py:724-729), 'EQR barrel adjustment belongs in realized_postflop_equity(), NOT should_fold_postflop()' — v79 places alignment boost in realized_postflop_equity (strategy.py:319-346), 'The LIVE opponent-modeling infra in current v78 is _aligned_signal_boost() — Extend THIS for raise-size/value-sizing signals' — v79 extends it with 4-bet light (uses opp_pfr + fold_to_raise) and check-raise trap (uses flop_aggr + postflop_aggr); Diff refs: constants.py:5 TOTAL_HANDS 50→70 (BOT-004 fix absent in v23 base), card_utils.py:39-42 wheel straight A-2-3-4-5 added (BOT-001 fix absent in v23 base), state.py:242 `2 * last_raise_to + 1 - my_round_bet` re-raise strictly >2x (BOT-002 fix absent in v23 base)
- **v78 (current, v23×v16)**: 3 latent correctness bugs in pre-fix-era bots (≤v22) — TOTAL_HANDS=50, wheel straight miss, illegal re-raise. v78 CARRIES the fixes (card_utils.py wheel straight, constants.py TOTAL_HANDS=70, state.py strictly-greater re-raise); future crossover source selection must validate bug-fix currency.
- **v78**: `_aligned_signal_boost()` dual-signal gate is THE validated noise-filter for opponent modeling (extend to preflop defense + value-bet sizing, NOT fold thresholds). Also widened sizing_exploit thresholds (0.55→0.47, 0.20→0.24) — constant tuning, validate ≥100g vs CS-lineage (v51/v62) before committing.
- **v78 (next-gen priority)**: RESTORE SPR commitment awareness — v78 REMOVED spr from should_fold_postflop. Add a get_action()-level structural commitment gate per the POSTFLOP_STRATEGY canonical entry (the working v73/v75 mechanism, currently ABSENT in v78).
- **v77**: Detection-without-handler is a recurring dead-code pattern — sb_limp_vs_raise was classified but fell through to generic preflop logic. Always verify every spot classifier has a corresponding dispatch branch in strategy.py.
- **v77**: estimate_preflop_strength saturates to 1.0 for ALL pocket pairs (22=AA) — use preflop_hand_profile for hand-class-specific gates (verify presence in v78 before relying on it).
- **v76 (bypassed/orphaned)**: graduated river value tier 0.55–0.80x + anti-spew caps is NOT inherited by v78. Retain as documented experiment; re-apply only after H2H confirmation. Cautionary: shadowed-branch bug (value tier under an early-return guard = dead code).


