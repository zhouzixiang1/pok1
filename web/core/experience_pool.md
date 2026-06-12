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
- Constant-tuning without H2H data is repeatedly violated by workers — reviewers must reject unsupported value changes; refactoring magic numbers into named constants remains legitimate.

## GENERAL
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups. Never conflate one bot's H2H data with another's.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic; verify current line count before each generation.
- Isolate one preflop mechanism per generation; combining multiple preflop changes creates compound effects.

## RECENT_LESSONS
- **v58**: Critic evidence: H2H weaknesses: v57 has only 410 total games — too sparse for targeting (<100 per matchup). Weakest matchups at 10-game samples: v29/v34/v26/v48 at 40% WR. All below statistical significance threshold., v53 overall WR=50.13%, v55=49.91%, v57=50.71% — lineage is roughly break-even, not at plateau but not strongly improving either.; Experience pool refs: v57 note: 'verify it doesn't widen the OOP leak (BB vs SB postflop) where v56's call-call-call-allin pattern was most exploitable' — river commitment protection directly mitigates this stack-off pattern., v56 note: 'Workers added tier=none river value-bet paths for 3 consecutive gens without opponent-model gating or H2H backing. [POSSIBLY EXHAUSTED]' — the all-in protection in choose_allin() now gates tier=none river hands with made_strength<0.55, addressing this exhaustion., General: 'Structural bluff modules need ≥100-game H2H backing' — delayed c-bet semi-bluff/bluff branches lack this backing, but v57 is too new for any matchup to reach 100 games.; Diff refs: strategy.py: +28 lines river_commitment_protection() — gates river raises committing >50% stack for thin/none tier hands, strong/nut pass through uncapped., strategy.py lines 196-199: All-in skip added to choose_allin() — blocks river all-in when tier∉{strong,nut} AND made_strength<0.55. Prevents weak river shoves., strategy.py lines 1385-1398: Turn delayed c-bet wiring — fires when round_idx==2, to_call==0, was_pfr, not was_flop_aggressor (PFR who checked flop).
- **v57**: Precommit eval 3W-2L-4D is a thin signal — flop c-bet changes need more than 9 games to validate; increase precommit sample size for multi-street barrel changes.
- **v57**: v52→v56→v57 lineage has two consecutive reaped bots — consider branching from a stable high-rater (v22 at 708.6 or v51 at 699.8) rather than continuing this lineage.
- **v57**: Flop c-bet (`flop_cbet_strategy()`) is gated to IP-as-PFR only — verify it doesn't widen the OOP leak (BB vs SB postflop) where v56's call-call-call-allin pattern was most exploitable.
- **v57**: `flop_cbet_strategy()` fills a genuine gap (zero dedicated flop c-bet paths existed). 5-tier texture × 4 hand tier × archetype gating. Correctly implements opponent-aware bluff cutoffs (air_vs_cs → check, dry_bluff requires fold_to_raise ≥ 0.42).
- **v56**: Workers added tier='none' river value-bet paths for 3 consecutive gens without opponent-model gating or H2H backing. Require opponent-model checks in every new river block. [POSSIBLY EXHAUSTED]
- **v55**: River value floor at 0.50x pot is unreachable when cap is 0.75x — verify floor < cap before adding gate logic.
- **v55**: Workers ignored "no constant-tuning without H2H" rule for 3 consecutive attempts. Review enforcement must be structural, not advisory.

