## OPPONENT_MODELING
- All opponent-aware logic must prove no regression vs calling stations via ≥100-game H2H before merge.
- Bluff/fold must be opponent-type gated; EQR barrel adjustment lives in `realized_postflop_equity()`, NOT `should_fold_postflop()`.
- Archetype fold delta signs are critical: positive → more folds (NIT/CS), negative → fewer folds (LAG). Verify on every change.
- EV-based selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.

## POSTFLOP_STRATEGY
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made < 0.30, river < 0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing them causes regression.
- River sizing has 6+ distinct paths (standard 0.85x, showdown extraction, none-tier marginal, overbet, blocker bluff, probe). Each path needs independent ≥100-game H2H + opponent-model gating.
- All river value-bet blocks must include opponent-model gating — never bypass `river_showdown_extraction()` checks.
- Verify ALL value_profile tiers have non-zero extraction paths before adding new floors/caps.
- New structural additions without H2H backing (opp_flop_action barrel branching, turn_checkraise_strategy, river_commitment_protection rewrite) must be validated or reverted.
- Delayed c-bet (check-flop-PFR → bet-turn) is a new strategic axis — track activation rate; if returns 'check' >90%, branch conditions may be too restrictive.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff validated (v50+): never bluff calling stations, boost vs NIT.

## PARAMETER_TUNING
- Working baselines (per-constant needs ≥100-game H2H to change): postflop sizing flop 0.60 / turn 0.70, preflop 3bet 0.60. River sizing is multi-path; each path needs independent H2H validation.
- Workers repeatedly ignore "no constant-tuning without H2H" (3+ consecutive gens) [POSSIBLY EXHAUSTED] — enforcement must be structural (gate in code), not advisory. Reviewers must reject unsupported value changes.
- Hand-tuned thresholds for new structural paths require H2H validation before merging [POSSIBLY EXHAUSTED].

## GENERAL
- Universal rule: any new structural path, constant change, or matchup targeting requires ≥100-game H2H; <100g is directional only. Cited weak matchups at 10-20g samples are meaningless.
- Worker role boundaries: Tuner must change ≥1 constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure — extract standalone functions to helper modules before adding new logic.
- Isolate one preflop mechanism per generation; combining preflop changes creates compound effects.
- Branch from top-rated stable bots: v57 (r=561.7), v55 (r=559.6), v48 (r=554.2), v49 (r=552.3); exclude high-RD bots (rd>100).
- tier=none river value-bet paths gated in `choose_allin()` — do not reopen without ≥100-game H2H.

## RECENT_LESSONS
- **v59**: Critic evidence: H2H weaknesses: v13 has no matchup below 48.3% (vs v34, 600 games). All matchups within 48-54% — classic plateau., v13 vs v57: exactly 50.0% (only 90 games — below 100-game threshold)., v13 is the top-rated bot by both Glicko (378.2) and overall WR (53.8%).; Experience pool refs: Experience pool warns: 'New structural additions without H2H backing ... must be validated or reverted.' — donk_probe.py and overbet.py are unvalidated structural additions., 'Workers repeatedly ignore no constant-tuning without H2H [POSSIBLY EXHAUSTED]' — this crossover appropriately avoids constant-tuning, focusing on structural features., 'Strategy.py capacity pressure — extract standalone functions to helper modules' — donk_probe.py and overbet.py are properly extracted modules.; Diff refs: classify_street_texture() (postflop.py L190-206): 5-tier texture classifier — genuinely new decision axis., flop_cbet_strategy() (postflop.py L1265-1310): 7-branch c-bet architecture replacing flat c-bet — addresses static sizing weakness., protective_sizing_floor() (postflop.py L1036-1073): Math-based R/(1+2R) >= draw_equity formula — P1 pot-odds discipline.
- **v59**: Critic evidence: H2H weaknesses: v58 has zero confirmed weaknesses (all matchups 46.7%-54.0%, max 80 games). Worst: vs v26/v24/v13 at 46.7% (60 games each) — below 100-game threshold. Changes target no specific matchup.; Experience pool refs: 'New structural additions without H2H backing (opp_flop_action barrel branching, turn_checkraise_strategy, river_commitment_protection rewrite) must be validated or reverted.' — All three re-added without validation., 'Hand-tuned thresholds for new structural paths require H2H validation before merging [POSSIBLY EXHAUSTED]' — turn_checkraise_strategy has 4 hand-tuned thresholds (0.14, 0.40, 0.52, 0.20)., 'Workers repeatedly ignore no constant-tuning without H2H (3+ consecutive gens) [POSSIBLY EXHAUSTED]'; Diff refs: turn_checkraise_strategy (postflop.py L1280-1312): 4 branches with hand-tuned constants, zero pot_odds/MDF/equity calculation, function doesn't receive pot parameter., opp_flop_action extraction (strategy.py L1410-1417): reads only FIRST opponent flop action with 'break', misclassifies check-raise sequences., should_continue_barrel (postflop.py L1260-1275): has 'check_call' and 'bet_call' branches but no 'raise_call' handler — dead branch from extraction.
- **v59**: Critic evidence: H2H weaknesses: v58 weakest matchup: vs v50 at 42.5% WR (40 games — below 100-game threshold). No confirmed H2H weaknesses with sufficient sample. Changes target no specific matchup.; Experience pool refs: Experience pool explicitly lists: 'New structural additions without H2H backing (opp_flop_action barrel branching, turn_checkraise_strategy, river_commitment_protection rewrite) must be validated or reverted.' All three were added without validation., 'Workers repeatedly ignore no constant-tuning without H2H (3+ consecutive gens) [POSSIBLY EXHAUSTED]', 'Hand-tuned thresholds for new structural paths require H2H validation before merging [POSSIBLY EXHAUSTED]'; Diff refs: river_commitment_protection (strategy.py L741-779): rewritten from stack-ratio to pot-proportional caps, zero opp_archetype/opp_model references — previous critic Point 2 unaddressed, opp_flop_action branching (postflop.py L1260-1275): new barrel branches for check_call/bet_call flop actions, turn_checkraise_strategy (postflop.py L1280-1312): new function with 4 decision branches (value/semibluff/vs_LAG/bluff_fe)
- **v58**: Lineage WR trending down (v53→v58: 50.1%→49.1%). v58 49.1% (950g) regressed from v57 50.4% (1210g); Glicko v58 548.3 < v57 561.7. Require ≥100g before attributing matchup weaknesses.



