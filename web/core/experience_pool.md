## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H; never bluff calling stations, boost bluffs vs NIT.
- EQR barrel adjustment belongs in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG); verify each change.
- Gate ALL raise/barrel/bluff/value branches by opponent type — raising for value into calling stations is exploitable (the calling_station blind spot spans every action path).

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- **Fold-mechanism resolution (was self-contradictory)**: `should_fold_postflop()` threshold/exit tuning is CONFIRMED exhausted — it never fixed the 0% postflop-fold leak. `get_action()`-level structural commitment gates ARE the working mechanism (wired before all-in dispatch) — but ONLY when POT-ODDS-GROUNDED. A raw `made_strength` cutoff (e.g. <0.50) folds legitimate top-pair calls and is exploitable by polarized bluffers, so it gets rolled back. Build fold work in `get_action()` as `made_strength + draw_potential < pot_odds_required − 0.05` (floor made_strength 0.40), NOT a raw made_strength range. [POSSIBLY EXHAUSTED]
- Turn barrel activation on `was_flop_aggressor + to_call == 0 + opp check` is a sound structural pattern — reuse.
- Delayed c-bet (PFR checks flop, bets turn) is structurally valid; wire `has_position` for OOP vs IP differentiation.
- Keep SPR-aware (tiered) river sizing, not flat SPR≥8 — the flat jam hemorrhaged chips.
- Action-dispatch bypasses are a high-value discovery vector: a turn-barrel once called raw `int(pot*ratio)` instead of `choose_raise`, skipping all value floors/guards. Audit every action-selection path (flop donk, river bet, check-raise sizing) for raw-ratio bypasses.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated: never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- RAISE_RATIO and threshold changes require per-constant H2H validation; batch changes obscure which value helped.
- Constant/margin tuning of fold gates, call thresholds, sizing ratios across many generations yielded no sustained gain. Reject constant-only tasks without structural rationale or H2H backing. [POSSIBLY EXHAUSTED]
- Emergency/commitment jam handling must be pot-odds + opponent-model grounded (e.g. `_emergency_jam_facing_raise_ok`), NOT a raw `made_strength` threshold — do NOT reintroduce raw-threshold jam gating.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across sample sizes; when the top lineage declines vs older bots, prioritize crossover diversity over deepening an over-fit lineage.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H effect.
- HARD GATE: Isolate one mechanism per generation, except sanctioned crossover diversity rescues.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Removed modules (`donk_probe.py`, `overbet.py`) were pruned; reintroducing requires fresh H2H — prior survival data no longer applies.
- If the top lineage declines vs older bots, suspect an anti-lock equity floor (calls/shoves at ~8% equity) before fold discipline — that was the root cause once.
- board_range_filter (opponent-range action-consistency post-filter) targets range-estimation quality; if H2H doesn't improve once games accumulate, it is dead weight to revert.

## RECENT_LESSONS
- **v75**: exploit_dispatch() architecture (per-street fold_to_bet tracking → barrel/value/bluff signal dispatch) is a REUSABLE OFFENSITIVE PATTERN for opponent-exploitative play — future gens can extend it with more signals (e.g., raise-size exploitation) rather than rebuilding tracking infrastructure
- **v75**: value_sizing_boost currently applies to ALL choose_raise calls including thin-value and probe raises — if calling_station H2H regresses, gate by value_profile tier ('strong'/'nut' only) to prevent bloated thin-value bets
- **v75 归档建议**: After daemon converges v75 to rd<80, prioritize ≥100-game H2H samples vs v51, v57, and v62 specifically — if barrel_freq_boost lowers the bluff threshold to 0.32 and misfires against tight-passive opponents whose high fold_to_raise reflects selection bias rather than exploitability, the next gen should restrict barrel_freq_boost to confirmed calling_station archetype only.
- **v75**: Critic evidence: H2H weaknesses: v74 overall: 380g, 50.26% WR — dead-even with pool, no dominant weakness, v51 vs v74: 0.70 (10g only — directional noise, not confirmed), v57 vs v74: 0.60, v62 vs v74: 0.60, v13 vs v74: 0.60 (all 10g — too small per pool rule '<100g is directional only'); Experience pool refs: OPPONENT_MODELING: 'Gate ALL raise/barrel/bluff/value branches by opponent type — raising for value into calling stations is exploitable (the calling_station blind spot spans every action path).' — v75 directly implements this at street granularity., OPPONENT_MODELING: 'never bluff calling stations, boost bluffs vs NIT' — implemented via bluff_suppress flag (street_fold<0.30) and barrel_freq_boost (street_fold>0.50)., POSTFLOP_STRATEGY: '[POSSIBLY EXHAUSTED] should_fold_postflop threshold/exit tuning never fixed the 0% postflop-fold leak; get_action()-level structural gates are the working mechanism.' — v75 correctly avoids fold logic entirely, working the offensive axis instead.; Diff refs: opponent.py: new exploit_dispatch() (lines 56-81) — translates per-street fold_to_bet into 3 offensive signals gated by confidence>=0.12., opponent.py: new fold_to_bet_flop/turn/river tracking (lines 188-199) — incremented only inside `if pending_my_pressure:` block, so semantics match fold_to_raise (folds vs my bets) at street granularity. Priors 0.44/0.40/0.36 with weight 3.0 are reasonable (fold-to-bet rises by street as hands realize)., strategy.py:794 — `exploit = exploit_dispatch(opponent_model, round_idx)` computed once per get_action().
- **v74**: First offensive value-extraction gen. Wired dead `sizing_hint` into `choose_raise` for the turn-barrel dispatch (was bypassing via raw `int(pot*ratio)`) + added a flop value floor (0.45x). No fold logic touched. Clean single-pass: Review 8, Critic 7.0, precommit 51-45 (parent parity 12-12).
- **v74**: Only two structural changes moved performance in ~19 generations — the commitment gate (prior gen) and this barrel-dispatch routing. Constant-tuning of fold/sizing thresholds is fully exhausted.
- **v74 open tension**: The 0% postflop-fold leak is still the #1 leak (battle_experience shows ~0% fold every street across the lineage, ~26K games; −19999 losses are a binary pattern of a few big losses/10g even in neutral pairs). Next gen should harden postflop fold-to-raise for made hands facing turn/river bets ≥35% pot — but via the POT-ODDS-GROUNDED `get_action()` mechanism (see POSTFLOP_STRATEGY), NOT a raw made_strength range, which this pool warns is exploitable. Resolve the Direction-Audit conflict (audit forbade fold work on a false premise) first.
- **v74**: Direction Audit can OVERRIDE match analysis and FORBID exhausted directions (it forbade all fold logic this gen on a false "bot folds ~22.6%" premise). Embed audit constraints into Master context; when audit and match-analysis conflict, surface both.
- **v74 pipeline**: `run_master` still requires a `direction_audit` parameter (system schema stale); `commit_bot` `push_ok:false` is recoverable via manual `git push origin main && git push origin bot-v{N}`.


