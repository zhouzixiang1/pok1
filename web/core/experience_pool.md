## OPPONENT_MODELING
- Gate ALL raise/barrel/bluff/value branches by opponent type — failing to adjust sizing/lines vs calling stations is the blind spot (spans every action path).
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG); verify each change.
- EQR barrel adjustment belongs in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Per-street fold_to_bet tracking (flop/turn/river) → exploit_dispatch() is BUILT & live (v75); extend it with raise-size exploitation signals rather than rebuilding infra.

## POSTFLOP_STRATEGY
- **Fold-mechanism resolution (canonical)**: `should_fold_postflop()` threshold/exit tuning is CONFIRMED exhausted — it never fixed the 0% postflop-fold leak. The working mechanism is `get_action()`-level structural commitment gates wired before all-in dispatch (v73/v75: made_strength<0.50 + draw_strength<0.18 + archetype≠lag + value tier≠strong/nut). It is a RAW made_strength cutoff with NO pot_odds comparison — it holds only because the draw/archetype/tier guards bound it. Build future fold work POT-ODDS-GROUNDED (`made_strength + draw_potential < pot_odds_required − 0.05`, floor made_strength 0.40); do not trust the raw <0.50 floor against polarized bluffers. [POSSIBLY EXHAUSTED]
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- Turn barrel activation on `was_flop_aggressor + to_call == 0 + opp check` is a sound structural pattern — reuse.
- Delayed c-bet (PFR checks flop, bets turn) is structurally valid; wire `has_position` for OOP vs IP differentiation.
- SPR-tiered river sizing (v70 3-tier, live through v76) is a regression GUARD — keep tiered; the flat SPR≥8 jam hemorrhaged chips.
- Action-dispatch bypasses are a high-value discovery vector: a turn-barrel once called raw `int(pot*ratio)` instead of `choose_raise`, skipping value floors/guards. Audit every action-selection path (flop donk, river bet, check-raise sizing) for raw-ratio bypasses.

## BLUFF_CALIBRATION
- Never bluff calling stations; boost bluffs vs NIT (validated — applies to all bluff/4-bet-light/barrel modules).
- Structural bluff modules (4-bet light, barrel) need ≥100-game H2H backing before targeting a matchup.

## PARAMETER_TUNING
- RAISE_RATIO and threshold changes require per-constant H2H validation; batch changes obscure which value helped.
- Constant/margin tuning of sizing ratios and call thresholds across many generations yielded no sustained gain (fold-gate tuning is covered under POSTFLOP_STRATEGY). Reject constant-only tasks without structural rationale or H2H backing. [POSSIBLY EXHAUSTED]
- Emergency/commitment jam handling must be pot-odds + opponent-model grounded (e.g. `_emergency_jam_facing_raise_ok`), NOT a raw `made_strength` threshold — do NOT reintroduce raw-threshold jam gating.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g (esp. 10g) is directional noise only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across sample sizes; prioritize crossover diversity over deepening an over-fit lineage.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H effect.
- HARD GATE: Isolate one mechanism per generation, except sanctioned crossover diversity rescues.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- `donk_probe.py` (473 lines) and `overbet.py` (275 lines) are LIVE in v76 (inherited via the v59 crossover, survived v73→v76), imported at strategy.py:34-35 — treat as active modules needing fresh H2H for any modification, do NOT assume pruned.
- If the top lineage declines vs older bots, suspect an anti-lock equity floor (calls/shoves at ~8% equity) before fold discipline — that was the root cause once.
- board_range_filter (opponent-range action-consistency post-filter), inherited from the v72 crossover (v72 itself reaped, the module survived into the v76 lineage) — if H2H doesn't improve once games accumulate, it is dead weight to revert.

## RECENT_LESSONS
- **v77**: Critic evidence: H2H weaknesses: v77 at 50% over 90 games (10g/matchup) — too few games to confirm specific weakness. v19 (other parent) at 50.3% over 18950 games, confirming a plateau. No <40% opponent to target in the current dataset.; Experience pool refs: Experience pool TODO: 'BB vs 4-bet QQ+ (strength ≥0.72) falls into a call-only path with no 5-bet-jam exception, unlike sb_vs_reraise' — directly addressed by the 5-bet-jam mutation., Experience pool: 'estimate_preflop_strength saturates to 1.0 for ALL pocket pairs' — correctly handled via preflop_hand_profile() instead., Experience pool: 'donk_probe.py and overbet.py are LIVE in v76' — inherited via v75 base, not modified.; Diff refs: strategy.py:758-779 — new sb_limp_vs_raise handler (v75 had zero occurrences of this string in strategy.py, confirming dead path), strategy.py:665-679 — BB 5-bet-jam for QQ+/AK gated by opp_preflop_raises >= 2, opponent.py:311-318 — sb_limp_vs_raise spot detection (already present in v75 but handler was missing)
- **v76**: When adding a value tier that overlaps an existing early-return guard (postflop.py:1110 returns made_strength≥0.50), the new tier MUST exclude the already-handled band OR the guard must be lowered — otherwise the branch is dead code (v76 'strong' in the graduated-tier tuple at line 1134 was shadowed by line 1110). NEXT-GEN FIX: lower the line 1110 guard 0.50→0.62 so 0.62-0.85 hands reach the graduated tier; validate H2H vs v51/v62 (calling stations).
- **v76**: Added graduated river value tier (0.55-0.80x for made_strength 0.62-0.85) + 2 anti-spew caps (>0.70 stack raise w/ non-nut → call). Direction audit was CLEAN — fold-logic forbiddance LIFTED (sliding-window reset, NOT permanent). At plateau: all matchups 40-60% over 350g, no <40% opponent to target — structural gap-filling is the correct approach.
- **v75**: value_sizing_boost applies to ALL choose_raise calls incl. thin-value/probe — if calling_station H2H regresses, gate by value_profile tier ('strong'/'nut' only) to prevent bloated thin-value bets.
- **v75**: barrel_freq_boost lowers the bluff threshold to 0.32 — may misfire vs tight-passive (high fold_to_raise from selection bias, not exploitability); restrict to confirmed calling_station if H2H regresses.
- **v75**: Direction-Audit fold-forbiddance is a sliding-window constraint, not permanent (v76 reset confirms) — default to OFFENSIVE/structural work; surface both views when audit and match-analysis conflict.
- **v74**: First offensive value-extraction gen — wired dead `sizing_hint` into `choose_raise` for the turn-barrel dispatch (was bypassing via raw `int(pot*ratio)`) + added a flop value floor (0.45x). No fold logic touched. Only two structural changes moved performance in ~19 gens (commitment gate + this barrel-dispatch routing); constant-tuning is fully exhausted.
- **v74 pipeline**: `run_master` still requires a `direction_audit` param (system schema stale); `commit_bot` `push_ok:false` is recoverable via manual `git push origin main && git push origin bot-v{N}`.
- **Next-gen target (from latest 4-bet analysis)**: BB vs 4-bet QQ+ (strength ≥0.72) falls into a call-only path with no 5-bet-jam exception, unlike sb_vs_reraise (line 709, strength ≥0.78 premium jam preserved). Add a premium 5-bet-jam exception for QQ+ when pot-odds/equity justify, mirroring the sb_vs_reraise structure.

