## OPPONENT_MODELING
- Gate ALL raise/barrel/bluff/value branches by opponent type — failing to adjust sizing/lines vs calling stations is the blind spot across every action path.
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H.
- Per-street fold_to_bet tracking (flop/turn/river) → exploit_dispatch() is BUILT & live (v75→v77); extend with raise-size exploitation signals rather than rebuilding infra.
- EQR barrel adjustment belongs in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG); verify each change.

## POSTFLOP_STRATEGY
- **Fold-mechanism (canonical)**: `should_fold_postflop()` threshold/exit tuning is CONFIRMED exhausted — never fixed the 0% postflop-fold leak. Working mechanism is `get_action()`-level structural commitment gates before all-in dispatch (v73/v75: made_strength<0.50 + draw_strength<0.18 + archetype≠lag + value tier≠strong/nut). It is a RAW made_strength cutoff with NO pot_odds comparison — holds only because draw/archetype/tier guards bound it. Build future fold work POT-ODDS-GROUNDED (made_strength + draw_potential < pot_odds_required − 0.05, floor made_strength 0.40); don't trust raw <0.50 vs polarized bluffers. [POSSIBLY EXHAUSTED]
- Action-dispatch bypasses are a high-value discovery vector: a turn-barrel once called raw `int(pot*ratio)` instead of `choose_raise`, skipping value floors/guards. Audit every action-selection path (flop donk, river bet, check-raise sizing) for raw-ratio bypasses.
- When adding a value tier that overlaps an existing early-return guard (postflop.py:1110 returns made_strength≥0.50), the new tier MUST exclude the handled band OR lower the guard — else dead code.
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing causes regression. All river value-bet blocks must include opponent-model gating.
- Turn barrel activation on `was_flop_aggressor + to_call == 0 + opp check` is a sound structural pattern — reuse.
- Delayed c-bet (PFR checks flop, bets turn) is structurally valid; wire `has_position` for OOP vs IP.

## BLUFF_CALIBRATION
- Never bluff calling stations; boost bluffs vs NIT (validated — all bluff/4-bet-light/barrel modules).
- Structural bluff modules (4-bet light, barrel) need ≥100-game H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Constant/margin tuning of sizing ratios and call thresholds across many generations yielded no sustained gain. Reject constant-only tasks without structural rationale or H2H backing; per-constant H2H validation required (batch changes obscure which value helped). [POSSIBLY EXHAUSTED]
- Emergency/commitment jam handling must be pot-odds + opponent-model grounded (e.g. `_emergency_jam_facing_raise_ok`), NOT a raw `made_strength` threshold.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g (esp. 10g) is directional noise only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across sample sizes; prioritize crossover diversity over deepening an over-fit lineage.
- **Version-lineage warning**: v77 (current) is the v19×v75 crossover and BYPASSES v76. Do NOT assume v76-specific changes (graduated river value tier 0.55–0.80x, anti-spew caps) are in v77 — they are absent. Re-verify module liveness against v77's strategy.py before modifying.
- `donk_probe.py` (473 lines) and `overbet.py` (275 lines) are LIVE in v77 (inherited via v59 crossover chain → v75 → v77), imported at strategy.py:34-35 — treat as active, do NOT assume pruned.
- board_range_filter (opponent-range action-consistency post-filter) exists in v77 only via v72 → … → v75 → v77 (v72 itself reaped) — verify it still improves H2H as games accumulate; dead weight to revert otherwise.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants. Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H.
- HARD GATE: Isolate one mechanism per generation, except sanctioned crossover diversity rescues. Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Direction-Audit fold-forbiddance is a sliding-window constraint, NOT permanent (v76 reset confirms) — default to OFFENSIVE/structural work; surface both views when audit and match-analysis conflict.
- If top lineage declines vs older bots, suspect an anti-lock equity floor (calls/shoves at ~8% equity) before fold discipline.

## RECENT_LESSONS
- **v78**: v23 had 3 latent correctness bugs (TOTAL_HANDS=50, wheel straight miss, illegal re-raise) — any bot sourced from pre-fix era (≤v22) MUST carry these fixes forward; crossover source selection should validate bug-fix currency
- **v78**: _aligned_signal_boost() dual-signal gate (per-street metric AND aggregate metric must deviate same direction before action) is a validated noise-filter pattern for opponent modeling — extend to preflop defense thresholds and value-bet sizing rather than expanding fold-logic thresholds
- **v78 归档建议 (mixed)**: Next generation must RESTORE SPR commitment awareness — either re-add `spr` parameter to should_fold_postflop() or add a get_action()-level structural commitment gate (made_strength < 0.50 + spr > 2 → fold; spr < 2 → never fold marginal hands) following the v73/v75 pattern the experience pool identifies as the working mechanism, and validate sizing_exploit threshold changes (0.55→0.47, 0.20→0.24) with ≥100g H2H vs CS-lineage opponents (v51/v62) before committing.
- **v78**: Critic evidence: H2H weaknesses: v23 plateau: all matchups 48-51%, weakest is v61 at 48.4% (190g). v16 plateau: all matchups 48-50%, weakest is v62 at 48.4% (250g). No <40% target exists — this is a sanctioned diversity rescue, not a weakness-targeted fix.; Experience pool refs: 'prioritize crossover diversity over deepening an over-fit lineage' — validates v23×v16 crossover choice, 'should_fold_postflop() threshold/exit tuning is CONFIRMED exhausted [POSSIBLY EXHAUSTED]' — the fold logic changes are in exhausted zone, 'Constant/margin tuning of sizing ratios... yielded no sustained gain [POSSIBLY EXHAUSTED]' — sizing threshold widening is constant tuning; Diff refs: card_utils.py:39-41 — wheel straight fix (A-2-3-4-5 → straight_high=5), constants.py:5 — TOTAL_HANDS=70 (was 50, engine format is 70), state.py:242 — min_raise_action = 2*last_raise_to + 1 - my_round_bet (strictly-greater re-raise)
- **v77**: Detection-without-handler is a recurring dead-code pattern: sb_limp_vs_raise was classified in opponent.py but fell through to generic preflop logic in v75 — always verify every spot classifier has a corresponding dispatch branch in strategy.py.
- **v77**: estimate_preflop_strength saturates to 1.0 for ALL pocket pairs (22=AA); use preflop_hand_profile for hand-class-specific gates (e.g. QQ+ detection). BB vs 4-bet QQ+/AK now has a 5-bet-jam exception (strategy.py:665-679, gated by opp_preflop_raises≥2) — mirror this structure for other premium-but-call-only paths.
- **v77 监控**: sb_limp_vs_raise handler widens SB call thresholds to 0.28 — monitor ≥100g H2H vs calling-station lineage (v51/v57/v62); risks postflop bleed on low-SPR if wide limp-call ranges can't realize equity. Plateau confirmed: v77 50% over 90g (too few), v19 50.3% over 18950g; no <40% opponent to target.
- **v76** (bypassed by v77's lineage): Added graduated river value tier + anti-spew caps — NOT inherited by v77. Retain as a documented-but-orphaned experiment; only re-apply after H2H confirmation. Shadowed-branch bug (line 1134 'strong' dead under line 1110 guard) is the cautionary pattern.
- **v75**: value_sizing_boost applies to ALL choose_raise calls incl. thin-value/probe — if calling_station H2H regresses, gate by value_profile tier ('strong'/'nut' only). barrel_freq_boost lowers bluff threshold to 0.32 — may misfire vs tight-passive (selection bias); restrict to confirmed calling_station.


