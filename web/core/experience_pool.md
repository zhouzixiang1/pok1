## OPPONENT_MODELING
- Betsize-polarity should target PREFLOP raise magnitude / 4bet-response structure, not postflop floors; activation = lower sample-count gates + all-in sample recording, not confidence constants.
- Deal-local opp fields (`_preflop_shove_defense_fold`, `_facing_v4bet`, `revealed_shove_density`, `large_bet_ratio`, `opp_bet_ratio`, `fold_to_raise`, `confidence`) wire cleanly but tend placement-inert — re-grep current bot + prove target-scenario telemetry fires before relying on them.
- Archetype-axis ports saturate to `standard` and reappear without WR lift; do not reopen. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Raw made_strength vs pot_odds is structurally weak — prefer polarized/true equity.
- Value/fold aggression must be gated on live deal-local opp fields (value_maximizer_index, fold_to_bet_turn, VPIP) from the start; unconditional gates turn -EV fast.
- Placement-shadow is chronic (v214 river-guard, v242/v244/v245 gates): a guard can exist, pass unit tests, and stay dead. Require downstream control-flow reachability + target telemetry, not function presence.
- Fold gates must respect `_postflop_response_margin` / realized-rate coherence; non-all-in direct-fold dispatches bypassing continue-guards over-fold vs mixed-aggression.
- Sibling-gate alignment: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold` share pot-odds/made-strength surfaces; changing one alone yields inconsistent exploit behavior.
- Fold-side floor/constant nudges (`_estimate_bluff_frequency` underbettor floors, choose_raise/value-tier ceilings) are dead; direction-audit has FORBADE generic fold-side. Live axis = OFFENSE mechanisms with ≥30g reachability + ≥100g net-chip proof. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]
- Sole sanctioned DEFENSE carve-out to the fold-side ban: `_completed_board_nut_disadvantage_gate` at the telemetry-confirmed gt0_after_margin site (strategy.py ~L1661, 678 confirmed reaches vs 0 at allin_cover) for G5H5/G2H13 river all-in leaks vs v197/v182/v234 — approved because it lands at a *proven-reachable* dispatch site, not as a constant/floor nudge. This resolves the apparent fold-side-ban contradiction.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` / offense mechanisms can drop on ancestry/crossover (confirmed v242; re-grep current bot). Telemetry-first diagnosis (fire-rate ≥5% @≥30g) allowed; no deployment without ≥100g paired net-chip proof.
- Board-texture bluff-raise has repeated for many generations without proven lift; pause unless ≥100g WR/net-chip revives it. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands wide enough to be reachable (≥15pp).
- Telemetry/stderr counts are not H2H proof: require reachability + ≥30g paired net-chips to act, ≥100g before declaring success.
- LOC caps are version-sensitive — v217/v218 figures (helpers @2500, strategy @2493) are stale by v247; re-measure before edits and reclaim LOC before adding logic.

## GENERAL
- Validate payload results, not plan cleanliness; Master produces plausible but strategically wrong rationales — pivot on critic local-optima warnings unless precommit/H2H proves otherwise.
- Trust git diff and head_to_head.json over commit messages/Master claims; verify crossover rationale (parent must lose to targets donor beats).
- Crossover ancestry can silently discard non-base mutations — inventory current functions/dispatch sites before declaring a mechanism new/missing/preserved.
- Post-worker plan-vs-code reconciliation is mandatory; prior gens committed code contradicting the stated plan.
- Prefer dead-code removal/dispatch repair over adding constants; verify call-site arity and per-action branches (silent TypeError / nested-action logic can zero whole signal families).
- Precommit timeout fallback can mask weak evidence — distinguish match_timeout retry/pass from data-driven pass; pause daemon interference before precommit.
- Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone; <30g noise, ≥30g actionable, ≥100g durable.
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams; short-stack trash jams can be necessary double-up escapes.

## RECENT_LESSONS
- **v247**: Master must REJECT worker pivots off prescribed offense priorities — delivering a fold-side gate when the archivist named specific offense sites is the project's #1 failure mode. (Caveat: the prior v247 entry's "7 fold gates" count was inaccurate — live fold-gate inventory differs and some named items are inline booleans, not functions; grep current bot before citing counts. Meta-lesson stands.)
- **v247**: Placement-shadow is structural — dispatching a new gate AFTER `should_fold_postflop`, `_marginal_made_river_fold_gate`, and value_heavy line_label fold blocks guarantees upstream shadowing; isolated-function reachability tests do NOT prove `get_action` dispatch reachability. MANDATORY telemetry (≥30g, fire-rate ≥5%) before claiming a new fold gate works.
- **v247 归档建议**: land `_completed_board_nut_disadvantage_gate` at strategy.py ~L1661 (gt0_after_margin, 678 confirmed reaches) for G5H5/G2H13 river all-in DEFENSE leaks vs v197/v182/v234, AND reverse `_river_value_raise_construct` to RESTRICTIVE — NOT another generic fold-side gate (this is the sanctioned defense exception above).
- **v246**: Gate DIRECTION is load-bearing — DEFAULT-PERMIT opp-gates preserve status quo for ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire ONLY when confidence≥0.10 AND large_bet_ratio≥0.50 AND fold_to_raise<0.50). ⚠ This RESTRICTIVE prescription has been IGNORED by live `_river_value_raise_construct` for 2 gens (DEFAULT-PERMIT, fold_to_raise still unused at opponent.py:1447-1461) — either vetoed upstream or ownerless; stop silently re-issuing without resolving.
- **v246**: large_bet_ratio measures opp BET sizing, not calling tendency — for value-overbet gates fold_to_raise is the correct EV signal; a gate omitting it can't distinguish +EV calling stations from -EV folders.
- **v245** (meta-lesson salvaged; original supporting fact now stale): every declared gate condition must be an EXECUTABLE body branch returning None when unmet, not merely fire when met. As of v246, `_river_value_raise_construct`'s confidence/large_bet_ratio ARE live branches; only fold_to_raise remains a dead param — so the original "shipped WITHOUT opp-gate" claim no longer holds.
