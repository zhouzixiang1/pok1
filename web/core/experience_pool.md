## OPPONENT_MODELING
- Gate ALL raise/barrel/bluff/value branches by opponent type — raising for value into calling stations is exploitable (the calling_station blind spot spans every action path).
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG); verify each change.
- EQR barrel adjustment belongs in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Per-street fold_to_bet tracking (flop/turn/river) feeding exploit_dispatch() is a REUSABLE OFFENSIVE PATTERN — extend with more signals (raise-size exploitation) rather than rebuilding tracking infra.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- All river value-bet blocks must include opponent-model gating.
- **Fold-mechanism resolution**: `should_fold_postflop()` threshold/exit tuning is CONFIRMED exhausted — never fixed the 0% postflop-fold leak. The working mechanism is `get_action()`-level structural commitment gates wired before all-in dispatch. The v73/v75 gate (made_strength<0.50 + draw_strength<0.18 + archetype≠lag + value tier≠strong/nut) SURVIVED, but it is a RAW made_strength cutoff with NO pot_odds comparison — it holds only because the draw/archetype/tier guards bound it, not because the raw floor is safe. Build future fold work POT-ODDS-GROUNDED (`made_strength + draw_potential < pot_odds_required − 0.05`, floor made_strength 0.40); do not trust the raw <0.50 floor against polarized bluffers. [POSSIBLY EXHAUSTED]
- Turn barrel activation on `was_flop_aggressor + to_call == 0 + opp check` is a sound structural pattern — reuse.
- Delayed c-bet (PFR checks flop, bets turn) is structurally valid; wire `has_position` for OOP vs IP differentiation.
- Keep SPR-aware (tiered) river sizing, not flat SPR≥8 — the flat jam hemorrhaged chips.
- Action-dispatch bypasses are a high-value discovery vector: a turn-barrel once called raw `int(pot*ratio)` instead of `choose_raise`, skipping all value floors/guards. Audit every action-selection path (flop donk, river bet, check-raise sizing) for raw-ratio bypasses.

## BLUFF_CALIBRATION
- Never bluff calling stations, boost bluffs vs NIT (validated — applies to all bluff/4-bet-light/barrel modules).
- Structural bluff modules (4-bet light, barrel) need ≥100-game H2H backing before targeting a matchup.

## PARAMETER_TUNING
- RAISE_RATIO and threshold changes require per-constant H2H validation; batch changes obscure which value helped.
- Constant/margin tuning of fold gates, call thresholds, sizing ratios across many generations yielded no sustained gain. Reject constant-only tasks without structural rationale or H2H backing. [POSSIBLY EXHAUSTED]
- Emergency/commitment jam handling must be pot-odds + opponent-model grounded (e.g. `_emergency_jam_facing_raise_ok`), NOT a raw `made_strength` threshold — do NOT reintroduce raw-threshold jam gating.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g (esp. 10g) is directional noise only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across sample sizes; prioritize crossover diversity over deepening an over-fit lineage.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H effect.
- HARD GATE: Isolate one mechanism per generation, except sanctioned crossover diversity rescues.
- Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- `donk_probe.py` (473 lines) and `overbet.py` (275 lines) are LIVE in v75 — imported at strategy.py:34-35 and called at 1454/1460/1495/1501/1510/1516; re-introduced via the v59 crossover and survived. Treat as active modules needing fresh H2H for any modification — do NOT assume they were pruned.
- If the top lineage declines vs older bots, suspect an anti-lock equity floor (calls/shoves at ~8% equity) before fold discipline — that was the root cause once.
- board_range_filter (opponent-range action-consistency post-filter) targets range-estimation quality; if H2H doesn't improve once games accumulate, it is dead weight to revert.

## RECENT_LESSONS
- **v76**: When adding a new value tier that overlaps an existing early-return guard (e.g., postflop.py:1110 returns made_strength>=0.50), the new tier's membership test MUST exclude the already-handled band or the existing guard must be lowered — otherwise the new branch is dead code (v76 'strong' in graduated-tier tuple at line 1134 was shadowed by line 1110).
- **v76 归档建议**: Fix the shadowed 'strong' branch by lowering the postflop.py:1110 guard from made_strength>=0.50 to >=0.62 so 0.62-0.85 hands flow into the graduated tier — then validate H2H vs v51/v62 (calling stations) where calibrated river value sizing should extract more chips from thin-value hands.
- **v76**: Critic evidence: H2H weaknesses: v75 at plateau: all matchups 40-60% (10-20g samples), overall 50.57% over 350g. No specific <40% opponent to target — structural gap-filling is the correct approach at plateau.; Experience pool refs: 'Constant/margin tuning of fold gates, call thresholds, sizing ratios across many generations yielded no sustained gain [POSSIBLY EXHAUSTED]' — this change is structural (new decision branch), not constant tuning, so it escapes the exhausted pattern., 'Action-dispatch bypasses are a high-value discovery vector' — the graduated tier similarly fills a gap where hands fell through to 0.0., 'Per-street fold_to_bet tracking feeding exploit_dispatch() is a REUSABLE OFFENSIVE PATTERN — extend with more signals' — v76 extends value-extraction offensive pattern.; Diff refs: postflop.py:1100 — threshold widened 0.62→0.85, opening 0.62-0.85 range to value betting, postflop.py:1132-1150 — NEW graduated tier branch for thin-tier 0.62-0.85 hands (previously returned 0.0), strategy.py:1283-1284 — river anti-spew cap: >70% stack raise with non-nut → call
- **v75**: exploit_dispatch() (per-street fold_to_bet → barrel_freq_boost / value_sizing_boost / bluff_suppress, confidence≥0.12) is a REUSABLE OFFENSITIVE PATTERN — extend with more signals rather than rebuilding tracking infra.
- **v75**: value_sizing_boost applies to ALL choose_raise calls incl. thin-value/probe — if calling_station H2H regresses, gate by value_profile tier ('strong'/'nut' only) to prevent bloated thin-value bets.
- **v75**: barrel_freq_boost lowers the bluff threshold to 0.32 — may misfire vs tight-passive (high fold_to_raise from selection bias, not exploitability). Restrict to confirmed calling_station if ≥100g H2H vs v51/v57/v62 regresses.
- **v75**: Direction-Audit fold-forbiddance is now a STABLE constraint (3 gens running); default to OFFENSIVE/structural work. Embed audit constraints into Master context; surface both views when audit and match-analysis conflict.
- **v74**: First offensive value-extraction gen — wired dead `sizing_hint` into `choose_raise` for the turn-barrel dispatch (was bypassing via raw `int(pot*ratio)`) + added flop value floor (0.45x). No fold logic touched. Clean single-pass: Review 8, Critic 7.0, precommit 51-45 (parent parity 12-12).
- **v74**: Only two structural changes moved performance in ~19 generations (the commitment gate + this barrel-dispatch routing); constant-tuning of fold/sizing thresholds is fully exhausted.
- **v74 pipeline**: `run_master` still requires a `direction_audit` param (system schema stale); `commit_bot` `push_ok:false` is recoverable via manual `git push origin main && git push origin bot-v{N}`.


