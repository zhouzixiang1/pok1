## OPPONENT_MODELING
- All opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merge.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- River sizing has 6+ distinct paths (standard 0.85x, showdown extraction, none-tier marginal, overbet, blocker bluff, probe). Each needs independent ≥100-game H2H + opponent-model gating.
- All river value-bet blocks must include opponent-model gating — never bypass `river_showdown_extraction()` checks.
- New structural additions (opp_flop_action barrel branching, turn_checkraise_strategy, river_commitment_protection rewrite) must be validated or reverted.
- Delayed c-bet (check-flop-PFR → bet-turn) is a new strategic axis — track activation rate; if returns 'check' >90%, branch conditions may be too restrictive.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated (v50+): never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- Working baselines (per-constant needs ≥100-game H2H to change): postflop sizing flop 0.60 / turn 0.70, preflop 3bet 0.60.
- Workers repeatedly ignore "no constant-tuning without H2H" — enforcement must be structural (gate in code), not advisory. Reviewers must reject unsupported value changes.
- Hand-tuned thresholds for new structural paths require H2H validation before merging.

## GENERAL
- Universal rule: any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only. Cited weak matchups at 10-20g samples are meaningless.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic.
- Isolate one preflop mechanism per generation; combining preflop changes creates compound effects.
- Branch from current top-rated stable bots (verify ratings at generation time); exclude high-RD bots (rd>100).
- tier=none river value-bet paths gated in `choose_allin()` — do not reopen without ≥100-game H2H.

## RECENT_LESSONS
- **v60**: Critic evidence: H2H weaknesses: v59 weakest matchups: v17/v22/v26/v27/v30 all beat v59 at 60% WR, but all are 10-20 game samples — experience pool rule: 'Cited weak matchups at 10-20g samples are meaningless.', v59 overall WR=49.78% over 460 games — essentially at baseline, no statistically significant weakness pattern identifiable.; Experience pool refs: POSTFLOP_STRATEGY: 'Delayed c-bet (check-flop-PFR → bet-turn) is a new strategic axis — track activation rate; if returns check >90%, branch conditions may be too restrictive.' — This change directly addresses this flagged gap., PARAMETER_TUNING: 'Working baselines (per-constant needs ≥100-game H2H to change): postflop sizing flop 0.60 / turn 0.70' — The constant tuning violates this rule with only 460 total games., PARAMETER_TUNING: 'Workers repeatedly ignore no constant-tuning without H2H — enforcement must be structural (gate in code), not advisory.'; Diff refs: postflop.py +45 lines: new `should_delayed_turn_cbet()` with 7 decision branches: delayed_value (strong/nut), delayed_semi_bluff (draw+wet), delayed_draw_fe (fold equity+position), delayed_thin_dry (thin+dry), delayed_bluff (weak+folding opp+not CS), delayed_thin_wet_check, delayed_vs_cs, delayed_default_check., strategy.py: wired at line 1356 with trigger `round_idx == 2 and to_call == 0 and was_pfr and not was_flop_aggressor` — correct delayed c-bet condition., strategy.py: hardcoded ratios (0.55/0.75/0.60/0.70/0.85) in `choose_raise()` replaced with named constants — good hygiene, neutral strategically.
- **v59**: Crossover effectively breaks critic deadlocks from minor-variant stagnation — v58→v59 failed critic 6× before crossover v13×v57 succeeded.
- **v59**: Isolate mutation-only changes from bug-fix backports in crossovers — bundling them is scope drift risk (reviewer flagged this).
- **v59**: donk_probe.py and overbet.py have survived 32+ generations (v27→v59) — implicitly validated by pool survival; no longer "unvalidated structural additions."
- **v59**: opp_flop_action extraction reads only FIRST opponent flop action with 'break', misclassifying check-raise sequences — still unfixed.
- **v58**: Lineage WR trending down (v53→v58: 50.1%→49.1%). Require ≥100g before attributing matchup weaknesses.

