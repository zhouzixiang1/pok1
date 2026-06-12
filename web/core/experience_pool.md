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
- **v54**: Critic evidence: H2H weaknesses: v53 vs claude_v31: WR=0.425 (40 games) — worst matchup, v53 vs claude_v26: WR=0.450 (40 games), v53 vs claude_v17/v21/v25: WR=0.460 (50 games each), v53 overall WR=0.498 across 1320 games — below break-even; Experience pool refs: PARAMETER_TUNING section: 'Constant-tuning without H2H data is a solved problem [EXHAUSTED]' — but this change is structural (new functions), not just constant adjustment, v54 RECENT_LESSONS: 'Refactoring magic numbers into named constants is legitimate; reject only unsupported value changes' — jam buffer changes are small and coherent, GENERAL: 'Structural changes can inflate Critic scores without improving battle performance; verify H2H effect' — valid concern, mirror battles will confirm; Diff refs: strategy.py: New _sb_mandatory_play() (lines 543-583) — structural SB playability guard, only folds absolute trash in HU, strategy.py: New _sb_reraise_playable() (lines 585-610) — structural defense vs 3-bets using blocker/playability analysis, strategy.py: open_threshold 0.46→0.43, limp_threshold 0.36→0.22 — wider SB opening range
- **v54**: Added archetype-aware river sizing (`_classify_opp_archetype` + `_apply_river_raise_cap`) — first structural gating of river raises by opponent type, directly addressing prior Critic concern that caps ignored opponent models.
- **v54**: Refactoring magic numbers into named constants is legitimate Architect work; reject only diffs that change constant values without ≥100-game H2H backing.
- **v54**: v47 consistently loses to opponents with superior modeling (v50: 47%, v48: 47.5%, v41: 46.9%) — systematic river sizing leak likely persists.
- **v53**: Direction audit broke 5-gen defensive fold-tuning loop — value extraction is a valid offensive pivot when fold logic plateaus.
- **v53**: Future sizing changes must verify ALL value_profile tiers have non-zero extraction paths before adding new floors.
- **v52**: Archetype delta sign fix (NIT +0.04, LAG -0.03, CS +0.02) resolved over-folding vs LAGs — validated values used in v54+.

