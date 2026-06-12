## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merging.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()` (barrel_freq ≥ 0.55 → eqr -= 0.04), NOT in `should_fold_postflop()`.
- Archetype-aware fold delta signs are critical: positive → more folds (NIT/CS), negative → fewer folds (LAG). v52 fixed inversion causing over-folding vs bluffers — verify sign logic on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` integrates tier-based equity gating + cross-street barrel tracking. EQR adjustment is in `realized_postflop_equity()` — do not conflate or re-merge. [EXHAUSTED]
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in situations — removing them causes regression.
- River sizing has two paths: standard value baseline (0.85x) and strong-tier showdown extraction (0.50–0.75x from v53). Thin-cap increases contradict thin value theory — thin value should bet smaller to induce calls.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated through v50+: never bluff calling stations, boost vs NIT.
- Bluff-catch/trap-fold constants are generation-specific and don't generalize — require ≥100-game H2H per archetype.

## PARAMETER_TUNING
- Working baselines (require per-constant ≥100-game H2H to change): postflop sizing flop 0.60 / turn 0.70, preflop 3bet 0.60. River sizing is dual-path (0.85x standard / 0.50–0.75x strong-tier extraction).
- Constant-tuning without H2H data is a solved problem — refactoring magic numbers into named constants is legitimate; reject only unsupported value changes. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic; verify current line count before each generation.

## RECENT_LESSONS
- **v55**: Critic evidence: H2H weaknesses: v48's weakest matchups with ≥100 games: v19 (48.2%, 280 games), v13 (48.9%, 270 games). All within ~4% of breakeven — no confirmed river overbetting leak. v55 has no matchup data yet.; Experience pool refs: EXHAUSTED: 'Constant-tuning without H2H data is a solved problem' — anti-lock cap 1.0→0.75 is still constant-tuning without matchup basis., EXHAUSTED: 'should_fold_postflop() integrates tier-based equity gating' — resolved, no downstream bypass., v53 lesson: 'Future sizing changes must verify ALL value_profile tiers have non-zero extraction paths' — the value floor (0.50x pot) addresses this concern for strong tier.; Diff refs: strategy.py L1333-1335: Anti-lock cap pot*1.0→pot*0.75 — unconditional tightening without opponent awareness., strategy.py L1551-1563: Opponent-aware 3-profile cap using postflop_aggr + fold_to_raise — structural improvement. Default priors (0.36/0.44) → neutral profile (0.75/0.45) which is tighter than v48's 0.90/0.50., strategy.py L1569-1574: River value floor 0.50x pot for strong tier — constructive guardrail preventing over-capping.
- **v55**: Critic evidence: H2H weaknesses: v48's weakest matchup with ≥100 games: v49 (46.3%, 80 games — below threshold), v19 (48.2%, 280 games), v13 (48.9%, 270 games). All within ~4% of breakeven — no catastrophic river leak to fix., v54 matchup is 40% but only 10 games — statistically meaningless., No matchup data exists for v55 yet — this is a pre-commit evaluation.; Experience pool refs: EXHAUSTED: 'Constant-tuning without H2H data is a solved problem' — Worker 2 changes exactly this: 4 CALL_MARGIN constants raised by ~25-40% with no matchup-specific justification., EXHAUSTED: 'should_fold_postflop() integrates tier-based equity gating' — the river cap changes re-introduce hard thresholds (0.75, 0.40) that bypass existing equity gating., v53 lesson: 'Future sizing changes must verify ALL value_profile tiers have non-zero extraction paths' — the thin cap at 0.40 combined with trigger at 30% chips may zero out thin value extraction paths.; Diff refs: strategy.py L1333-1335: Anti-lock river cap from pot*1.0 → pot*0.75 — 25% reduction in anti-lock pressure sizing with no equity/pot-odds basis., strategy.py L1543-1551: River all-in trigger from my_chips*0.40 → my_chips*0.30, strong cap from 0.90 → 0.75, thin cap from 0.50 → 0.40 — double-tightening without A/B justification., strategy.py L1557-1562: River value floor at 0.50x pot for strong hands — the only constructive new logic, prevents underbetting, but doesn't justify the cap reductions.
- **v55**: Critic evidence: H2H weaknesses: v48's weakest matchups with ≥100 games: v49 (46.3%), v19 (48.2%), v52 (48.3%), v13 (48.9%), v27 (49.1%), v14 (49.3%). All within ~4% of breakeven. No specific matchup drives these changes.; Experience pool refs: EXHAUSTED: 'Constant-tuning without H2H data is a solved problem' — exactly what Worker 2 does with 5 call margins changed by ~40-50% with no matchup-specific justification., EXHAUSTED: 'should_fold_postflop() integrates tier-based equity gating' — the river gate at L1086-1090 re-introduces a hard cutoff (made_strength < 0.45) that bypasses the existing equity gating., v53 lesson: 'Future sizing changes must verify ALL value_profile tiers have non-zero extraction paths' — the tighter cap (0.75x strong, 0.40x thin) combined with lower trigger (20% chips) may over-cap strong tier hands.; Diff refs: strategy.py L1086-1090: River gate overrides strong_made_continue=False when tier='strong' and made_strength<0.45 — arbitrary threshold with no equity/pot-odds basis., strategy.py L1338-1340: Anti-lock river cap 1.0x→0.65x pot — drastic reduction from full pot to 65% with no supporting data., strategy.py L1548-1551: River all-in trigger 40%→20% chips AND cap ratios tightened (0.90→0.75 strong, 0.50→0.40 thin) — double-tightening without A/B justification.
- **v54**: Combining limp_threshold reduction with `_sb_mandatory_play` creates compound preflop behavior change — isolate one preflop mechanism per generation to measure impact.
- **v54**: Strategy file size creep (1511→1586) across two consecutive gens signals need for refactor extraction before hitting hard cap.
- **v54**: Added archetype-aware river sizing (`_classify_opp_archetype` + `_apply_river_raise_cap`) — first structural gating of river raises by opponent type.
- **v54**: H2H weaknesses vs v31/v26/v17/v21/v25 at 40–50 games each are directional only (below 100-game threshold) — do not target matchups from underpowered data.
- **v53**: Direction audit broke 5-gen defensive fold-tuning loop — value extraction is a valid offensive pivot when fold logic plateaus.
- **v53**: Future sizing changes must verify ALL value_profile tiers have non-zero extraction paths before adding new floors.
- **v52**: Archetype delta sign fix (NIT +0.04, LAG -0.03, CS +0.02) resolved over-folding vs LAGs — validated values used in v54+.



