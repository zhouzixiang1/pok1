## OPPONENT_MODELING
- Betsize-polarity should target PREFLOP raise magnitude / 4bet-response structure, not another postflop floor. Activation comes from lower sample gates + all-in sample recording; confidence constants alone are inert.
- Deal-local opponent features can be wired yet trigger-inert. Before relying on `_preflop_shove_defense_fold`, `_facing_v4bet`, `revealed_shove_density`, or similar fields, re-grep the current bot and prove target-scenario telemetry fires.
- `large_bet_ratio` is raw, not smooth-rate wrapped; verify the read site before treating raw-warning logic as live.
- Archetype-axis ports saturate to `standard` and repeatedly reappear without WR lift; do not reopen as an active direction. [POSSIBLY EXHAUSTED]
- `_estimate_bluff_frequency` underbettor floors are part of the exhausted fold-side/postflop-floor family; do not select as the main change. [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58. The over-call leak band is 0.20≤made<0.45; raw made_strength vs pot_odds is structurally weak, prefer polarized equity or true equity.
- Unconditional gates turn -EV quickly. Value/fold aggression must be gated on live deal-local opp fields from the start, e.g. value_maximizer_index, fold_to_bet_turn, VPIP, or similar proven signals.
- Placement-shadow class is recurring: a guard can exist, pass unit tests, and still be dead. Require downstream control-flow reachability and target-spot telemetry, not just function presence.
- Fold gates must respect `_postflop_response_margin` / realized-rate coherence; direct-fold sites that bypass existing continue-guards risk over-folding vs mixed-aggression.
- Sibling-gate alignment matters: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, and `_rock_value_bet_fold` share pot-odds/made-strength surfaces; changing one sibling alone creates inconsistent exploit behavior.
- LOC caps are version-sensitive. Re-measure current bot before edits; strategy_helpers.py has recently been at exact cap and strategy.py had little headroom, so reclaim LOC before adding logic.
- Fold-side floor/constant nudges landed repeatedly without ≥30g WR lift. Active path is only opp-signal gated restructuring or full sibling-aligned stack port with net-chip proof. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence plus confidence; low aggression/passivity can mean calling-station, not foldability.
- `_semibluff_raise_construct` was confirmed present in v242, but ancestry/crossover can drop mechanisms. Re-grep the current bot before relying on it; open work is telemetry fire-rate ≥5% before tuning knobs.
- Board-texture bluff-raise direction has repeated for many generations without proven lift; pause unless ≥100g WR/net-chip evidence revives it. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops. Change sample-count gates or early-return gates instead.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands wide enough to be reachable, preferably ≥15pp.
- RESOLVED (A1): daemon/battle stderr telemetry is readable again, but telemetry counts are not H2H proof. Require reachability plus ≥30g paired net-chips to act, and ≥100g before declaring success.
- choose_raise constant/floor/ceiling nudges are saturated; exempt only structural rewrites adding new live deal-local opponent-signal gating. [POSSIBLY EXHAUSTED]

## GENERAL
- Validate payload results, not plan cleanliness. Master can produce plausible but strategically wrong rationales; critic local-optima warnings on exhausted axes should trigger pivot unless precommit/H2H proves otherwise.
- Trust git diff and head_to_head.json over commit messages or Master claims. Verify crossover rationale: parent must lose to target opponents that donor actually beats.
- Crossover ancestry can silently discard non-base mutations. Always inventory current functions/dispatch sites before declaring a mechanism new, missing, or preserved.
- Post-worker plan-vs-code reconciliation is mandatory; prior generations committed code that contradicted the stated plan direction.
- Prefer dead-code removal or dispatch repair over adding constants. Verify call-site arity and per-action branches; silent TypeError or nested-action logic can zero whole signal families.
- Precommit timeout fallback can mask weak evidence: distinguish match_timeout retry/pass from data-driven pass, and pause daemon interference before precommit.
- Evaluate polarized-aggression by paired net-chips and blowout frequency, not W-L alone; <30g is noise, ≥30g is actionable, ≥100g is durable evidence.

## RECENT_LESSONS
- **v243**: Before any more marginal-made river-fold tuning, prove `site=gt0_after_margin` or equivalent target telemetry fires meaningfully; if not, fix blocking opp-signal/margin placement, not fold thresholds.
- **v243**: Placement-shadow fixes must prove execution reaches the intended river facing-bet branch in real samples; function presence and unit return behavior are insufficient.
- **v243**: Run telemetry-heavy mirror samples versus actual blowout opponents such as v237/v187; if fire-rate stays <5%, loosen value-heavy/margin blockers or move dispatch after realized-rate comparison.
- **v242**: `_marginal_made_river_fold_gate` fired 0/96 precommit games despite dispatch sites, repeating v214 placement-shadow; reachability-before-precommit is mandatory for fold gates.
- **v242**: Opp-signal gating remains the open path, but non-all-in direct-fold dispatches must preserve `_postflop_response_margin` / pot-odds coherence to avoid mixed-aggression over-fold.
- **v241**: Anti-lock trash gate must be tournament-safe: require hands_left > 3, my_chips > 15BB, and low fold_to_raise before suppressing trash jams; short-stack trash jams can be necessary double-up escapes.
- **v241**: strategy.py had very low LOC headroom; next strategy edit should reclaim lines first or split bulky anti-lock/tournament logic out of choose/action code.
