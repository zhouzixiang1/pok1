## OPPONENT_MODELING
- Gate ALL raise/barrel/bluff/value branches by opponent type — failing to adjust sizing/lines vs calling stations is the blind spot across every action path.
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H.
- EQR/barrel adjustment belongs in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG); verify each change.
- Dual-signal noise filter (per-street metric AND aggregate metric must deviate the same direction before action) is the validated opponent-modeling pattern — BUT it originated on the v16/v78 lineage and may be ABSENT from crossovers on other lineages (e.g. v79=v23×v17). Re-verify it is LIVE in the current bot's files before extending it to preflop defense / value-sizing signals.

## POSTFLOP_STRATEGY
- **Fold-mechanism (canonical)**: `should_fold_postflop()` threshold/exit tuning never fixed the 0% postflop-fold leak. The WORKING mechanism is a `get_action()`-level structural commitment gate (made_strength<0.50 + draw_strength<0.18 + archetype≠lag + value tier≠strong/nut), pot-odds-grounded (made_strength + draw_potential < pot_odds_required − 0.05; floor made_strength 0.40). v79 RESTORED SPR awareness (SPR>4 uncommitted fold gate, strategy.py:724-729), closing that gap — the structural gate itself remains the absent priority. Don't trust raw <0.50 vs polarized bluffers. [POSSIBLY EXHAUSTED]
- Action-dispatch bypasses are a high-value discovery vector: a turn-barrel once called raw `int(pot*ratio)` instead of `choose_raise`, skipping value floors/guards. Audit every action-selection path (flop donk, river bet, check-raise sizing) for raw-ratio bypasses.
- When adding a value tier that overlaps an early-return guard, the new tier MUST exclude the handled band OR lower the guard — else dead code (shadowed-branch bug, seen v76).
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing causes regression. All river value-bet blocks must include opponent-model gating.
- Turn barrel activation on `was_flop_aggressor + to_call == 0 + opp check` is a sound structural pattern — reuse; wire `has_position` for OOP vs IP.

## BLUFF_CALIBRATION
- Never bluff calling stations; boost bluffs vs NIT (validated across all bluff/4-bet-light/barrel modules).
- Structural bluff modules (4-bet light, barrel, check-raise trap) need ≥100-game H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Constant/margin tuning of sizing ratios and call thresholds across many generations yielded no sustained gain. Reject constant-only tasks without structural rationale or H2H backing; per-constant H2H validation required (batch changes obscure which value helped). [POSSIBLY EXHAUSTED]
- Emergency/commitment jam handling must be pot-odds + opponent-model grounded (e.g. `_emergency_jam_facing_raise_ok`), NOT a raw `made_strength` threshold.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g (esp. 10g) is directional noise only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across sample sizes; prioritize crossover diversity over deepening an over-fit lineage.
- **Version-lineage (v79 current = v23×v17)**: crossovers land on lineages that diverge from prior chains. Do NOT assume features from any other chain are present — the structural commitment gate, exploit_dispatch, donk_probe, overbet, board_range_filter, per-street fold_to_bet, and `_aligned_signal_boost()` may ALL be absent. Re-verify module liveness against the current bot's actual files before wiring/modifying.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants. Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H.
- HARD GATE: Isolate one mechanism per generation, except sanctioned crossover diversity rescues. Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Direction-Audit fold-forbiddance is a sliding-window constraint, NOT permanent — default to OFFENSIVE/structural work; surface both views when audit and match-analysis conflict.
- If top lineage declines vs older bots, suspect an anti-lock equity floor (calls/shoves at ~8% equity) before fold discipline.
- Pre-fix-era bots (≤v23) carry latent correctness bugs: TOTAL_HANDS=50, wheel-straight miss, illegal re-raise (strictly >2x). Crossover source selection must validate bug-fix currency; fixes land at v34+ / card_utils wheel / state.py re-raise.

## RECENT_LESSONS
- **v80**: Critic evidence: H2H weaknesses: v79 vs v51 (CS lineage): 2W-8L=20% in 10g (noise-only, but the only sub-40% signal); vs v48: 4W-6L=40% in 10g. All others 45-60% (plateau confirmed). v80's value-barrel escalation directly risks worsening the v51 CS matchup if 'strong' tier is loose — VALUE branch has NO archetype gate.; Experience pool refs: 'Never bluff calling stations; boost bluffs vs NIT' — barrel_plan's BLUFF branch correctly gates on fold_to_raise>0.52 (CS-suppression), but VALUE branch ignores archetype., 'Multi-street barrel fold (opp_postflop_bet_count≥2, turn eff_made<0.30, river<0.38) is structurally distinct' — v80's barrel_plan is the offensive mirror of this defensive concept, well-isolated., 'Turn barrel activation on was_flop_aggressor + to_call==0 + opp check is a sound structural pattern' — delayed_cbet branch reuses this correctly.; Diff refs: postflop.py:1016-1075 barrel_plan() — new 60-line module; clean abort logic at line 1039 (opp_prior_postflop_raise_count > streets_barreled)., opponent.py:189-195 — new my_postflop_bet_count + was_preflop_raiser tracking. Sound: uses record round < state['round'] to count PRIOR street bets (not current)., strategy.py:914-919 — _barrel_plan computed in get_action before decision tree; correct placement.
- **v79 (current, v23×v17)**: RESTORED SPR commitment awareness (strategy.py:724-729) + added 4-bet light (opp_pfr+fold_to_raise) + check-raise trap (flop_aggr+postflop_aggr) + EQR alignment-boost clamp in realized_postflop_equity (strategy.py:319-346). Autonomous crossover DERIVED all 3 latent bug fixes absent from BOTH parents (TOTAL_HANDS 50→70, wheel, re-raise +1) — crossover LLM CAN derive fixes from buggy parents; VERIFY post-crossover.
- **v79 MONITOR**: EQR clamp at strategy.py:319-346 broadened from air_hands-only to ALL unclassified late-street hands → now caps eqr 0.85 for strong made hands (TPGK, two-pair, sets). If H2H vs CS-lineage (v51/v62) ≥100g shows lost value on coordinated boards, add `made_strength < 0.40` guard. Also: 4-bet light 70% activation is aggressive — verify opp_fold_to_raise gate tightness vs CS.
- **v79**: v78 plateau confirmed at exactly 50.0% (190-190/380g); v23 vs v75 only 40g (55%) = directional noise, no opponent <40% → justified sanctioned diversity crossover (HARD GATE exception).
- **v78 (v23×v16)**: `_aligned_signal_boost()` dual-signal gate was the validated noise-filter (extend to preflop defense + value sizing, NOT fold thresholds). Widened sizing_exploit thresholds (0.55→0.47, 0.20→0.24) — constant tuning, validate ≥100g vs CS before committing. v78 carried the 3 bug fixes.
- **v77**: Detection-without-handler is a recurring dead-code pattern (sb_limp_vs_raise classified but fell through to generic preflop logic) — always verify every spot classifier has a dispatch branch. estimate_preflop_strength saturates to 1.0 for ALL pocket pairs (22=AA) → use preflop_hand_profile for hand-class gates (verify presence before relying).

