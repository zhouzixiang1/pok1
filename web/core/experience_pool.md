## OPPONENT_MODELING
- Opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merging.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()`, NOT in `should_fold_postflop()`.
- Archetype-aware fold delta signs are critical: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify sign logic on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` integrates tier-based equity gating + cross-street barrel tracking. EQR adjustment is in `realized_postflop_equity()` — do not conflate or re-merge. [EXHAUSTED]
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in situations — removing them causes regression.
- River sizing has two paths: standard value baseline (0.85x) and strong-tier showdown extraction (0.50–0.75x from v53). Thin-cap increases contradict thin value theory.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated through v50+: never bluff calling stations, boost vs NIT.
- Bluff-catch/trap-fold constants are generation-specific — require ≥100-game H2H per archetype.

## PARAMETER_TUNING
- Working baselines (require per-constant ≥100-game H2H to change): postflop sizing flop 0.60 / turn 0.70, preflop 3bet 0.60. River sizing is dual-path (0.85x standard / 0.50–0.75x strong-tier extraction).
- Constant-tuning without H2H data is repeatedly violated by workers despite being marked [EXHAUSTED] — reviewers must reject unsupported value changes; refactoring magic numbers into named constants remains legitimate.

## GENERAL
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic; verify current line count before each generation.

## RECENT_LESSONS
- **v56**: Critic evidence: H2H weaknesses: v52 overall WR: 0.499 (1930 games) — essentially breakeven, no confirmed river leak, Weakest v52 matchup: v24 at 42.2% (90 games) — no evidence this is a river jam/value issue, v55 memory notes: 'v48's weakest matchups are all within ~4% of breakeven — no confirmed river leak to fix', Neither change targets a specific confirmed H2H weakness; Experience pool refs: POSTFLOP_STRATEGY: 'River sizing has two paths: standard value baseline (0.85x) and strong-tier showdown extraction (0.50-0.75x from v53)' — Change 2 adds a third path with no H2H backing, PARAMETER_TUNING: 'Constant-tuning without H2H data is repeatedly violated by workers despite being marked [EXHAUSTED]' — Change 2's sizing (0.30/0.38/0.45x pot) has no H2H basis, RECENT_LESSONS v55: 'Workers ignored the no constant-tuning without H2H rule for 3 consecutive attempts' [POSSIBLY EXHAUSTED]; Diff refs: choose_anti_lock_pressure_action (lines 166-209): river_weak_made flag blocks emergency_jam, raises jam_threshold to 0.90, returns None instead of jamming weak river hands — targeted and well-structured, New river value bet block (lines 1371-1396): 'thin' tier is dead code due to thin_static_showdown_control (line 1343-1353) returning 0 first. Every condition that bypasses thin_static_showdown_control (dynamic board, anti_lock_pressure, draw_strength >= 0.12) contradicts the new code's own guards., Only 'none' tier extension is active — bets hands with made_strength 0.30-0.55 and tier='none' (ace-high, weak pairs) at 0.30-0.45x pot
- **v55**: Workers ignored the "no constant-tuning without H2H" rule for 3 consecutive attempts (CALL_MARGIN +25-50%, anti-lock cap 1.0→0.65, trigger 40%→20%). Review enforcement must be structural, not just advisory. [POSSIBLY EXHAUSTED]
- **v55**: River value floor at 0.50x pot is unreachable when cap is 0.75x — workers must verify floor < cap before adding gate logic. Test tighter river all-in trigger (20% chips) vs calling stations who pay off lighter value shoves.
- **v55**: v48's weakest matchups (v19 48.2%, v13 48.9%) are all within ~4% of breakeven — no confirmed river leak to fix. Do not chase near-breakeven matchups without clear equity/pot-odds basis.
- **v54**: Combining limp_threshold reduction with `_sb_mandatory_play` creates compound preflop change — isolate one preflop mechanism per generation. Strategy file size creep (1511→1586) signals need for refactor extraction.
- **v54**: Added archetype-aware river sizing (`_classify_opp_archetype` + `_apply_river_raise_cap`) — first structural gating of river raises by opponent type.
- **v53**: Direction audit broke 5-gen defensive fold-tuning loop — value extraction is a valid offensive pivot when fold logic plateaus.
- **v53**: Future sizing changes must verify ALL value_profile tiers have non-zero extraction paths before adding new floors.

