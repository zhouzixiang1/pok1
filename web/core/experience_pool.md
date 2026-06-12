## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merging.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()`, NOT in `should_fold_postflop()`.
- Archetype-aware fold delta signs are critical: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify sign logic on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` integrates tier-based equity gating + cross-street barrel tracking. EQR adjustment is in `realized_postflop_equity()` — do not conflate or re-merge. [EXHAUSTED]
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in situations — removing them causes regression.
- River sizing now has 6+ distinct paths (standard 0.85x, showdown extraction, none-tier marginal, overbet, blocker bluff, probe). New paths MUST have ≥100-game H2H backing AND opponent-model gating before merge.
- All river value-bet blocks must include opponent-model gating (archetype/fold_to_raise/VPIP) — never bypass river_showdown_extraction()'s opponent checks.
- Verify ALL value_profile tiers have non-zero extraction paths before adding new floors or caps.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated through v50+: never bluff calling stations, boost vs NIT.
- Bluff-catch/trap-fold constants are generation-specific — require ≥100-game H2H per archetype.

## PARAMETER_TUNING
- Working baselines (require per-constant ≥100-game H2H to change): postflop sizing flop 0.60 / turn 0.70, preflop 3bet 0.60. River sizing is multi-path; each new path needs independent H2H validation.
- Constant-tuning without H2H data is repeatedly violated by workers despite being marked [EXHAUSTED] — reviewers must reject unsupported value changes; refactoring magic numbers into named constants remains legitimate. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic; verify current line count before each generation.

## RECENT_LESSONS
- **v57**: Critic evidence: H2H weaknesses: v56 loses to v48 (30% WR, 10 games) and 6 opponents at 40% WR (v27/v42/v29/v37/v24/v34, 10-20 games each). While game counts are low (<100), the pattern suggests the bot is too passive on the flop when checked to as PFR., v56 overall WR 49.2% across 370 games, rating 685 — below v55 (742) and roughly tied with the field median.; Experience pool refs: POSTFLOP_STRATEGY notes 'river sizing now has 6+ distinct paths' but flop c-bet had ZERO dedicated paths — this fills a genuine gap., BLUFF_CALIBRATION: 'Opponent-aware bluff cutoff validated through v50+: never bluff calling stations, boost vs NIT' — the new function correctly implements this (air_vs_cs returns check, dry_bluff requires fold_to_raise >= 0.42)., PARAMETER_TUNING: 'Constant-tuning without H2H data is repeatedly violated... marked [EXHAUSTED]' — this change is NOT constant tuning; it's a new decision system with texture-dependent logic.; Diff refs: postflop.py: New `flop_cbet_strategy()` (lines 1262-1310, 51 lines) — 5-tier texture classification (dry/paired/draw_heavy/monotone/semi_connected) × 4 hand tiers (nut-strong/thin/draw/air) × opponent archetype gating., strategy.py: New `_was_preflop_raiser()` helper (lines 724-731) — checks round-0 raise history to identify PFR., strategy.py: New c-bet dispatch block (lines 1315-1329) — fires ONLY when round_idx==1 AND to_call==0 AND was_pfr AND NOT facing aggression. Falls through to existing logic when c-bet returns check.
- **v42**: Critic evidence: H2H weaknesses: weak vs 3bet
- **v42**: Critic evidence: H2H weaknesses: weak vs 3bet
- **v42**: Critic evidence: H2H weaknesses: weak vs 3bet
- **v42**: Critic evidence: H2H weaknesses: weak vs 3bet
- **v42**: Critic evidence: H2H weaknesses: weak vs 3bet
- **v42**: Critic evidence: H2H weaknesses: weak vs 3bet
- **v42**: Critic evidence: H2H weaknesses: weak vs 3bet
- **v56**: Workers added tier='none' river value-bet paths for 3 consecutive gens (v54–v56) without opponent-model gating or H2H backing. Gate on river edits that don't match task keywords; require opponent-model checks in every new river block. [POSSIBLY EXHAUSTED]
- **v56**: H2H data is for v52 (WR 0.499, ~2000 games), NOT v56 (0 rated games). Do not recommend targeting v56's opponents using v52 matchup data — wait for daemon evaluation.
- **v55**: River value floor at 0.50x pot is unreachable when cap is 0.75x — verify floor < cap before adding gate logic.
- **v55**: Workers ignored "no constant-tuning without H2H" rule for 3 consecutive attempts. Review enforcement must be structural, not advisory. [POSSIBLY EXHAUSTED]
- **v54**: Combining limp_threshold reduction with `_sb_mandatory_play` creates compound preflop change — isolate one preflop mechanism per generation.
- **v54**: First archetype-aware river sizing (`_classify_opp_archetype` + `_apply_river_raise_cap`) — structural gating of river raises by opponent type.








