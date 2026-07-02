## OPPONENT_MODELING
- Betsize-polarity modeling: target PREFLOP raise magnitude / 4bet-response structure, not postflop floors; activate via lower sample-count gates + all-in sample recording, not confidence constants.
- Deal-local opp fields wire cleanly but trend placement-inert — re-grep current bot + prove target-scenario telemetry fires (≥30g) before relying on them.
- Archetype-axis ports saturate to `standard` and reappear without WR-lift; do not reopen. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Prefer polarized/true equity over raw made_strength vs pot_odds.
- `_marginal_made_river_fold_gate`: the v242 "dead `paired_board_profile`" premise is RESOLVED — present in v254 (15 hits) with the param heavily wired (70 hits). Only open item: confirm fire-rate ≥5% @≥30g NET-CHIPS vs v200/v238/v208 before declaring the river leak closed.
- `_completed_board_nut_disadvantage_gate` was asserted live (678 reaches) but is ENTIRELY ABSENT in v254 (0 hits) — dropped via crossover ancestry. If revived, re-grep for presence + reachability; do NOT assume it is live.
- Value/fold aggression must be gated on live deal-local opp fields (value_maximizer_index, fold_to_bet_turn, VPIP) from the start; unconditional gates turn -EV fast.
- Placement-shadow is chronic (v214 river-guard, v242/v244/v245/v247 gates): require downstream `get_action` dispatch reachability + target telemetry (≥30g, fire-rate ≥5%), not isolated-function presence.
- Fold gates must respect `_postflop_response_margin` / realized-rate coherence; non-all-in direct-fold dispatches bypassing continue-guards over-fold vs mixed-aggression.
- Sibling-gate alignment: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold` share pot-odds/made-strength surfaces; change one alone → inconsistent exploit behavior.
- Fold-side GENERIC nudges BANNED (closed, exhausted axis — do not reopen): `_estimate_bluff_frequency` underbettor floors, choose_raise/value-tier ceilings are FORBADE & dead. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]
- Sanctioned DEFENSE exception (OPEN, NOT a generic floor): a *targeted* gate at a telemetry-confirmed dispatch site is permitted — but each (`_completed_board_nut_disadvantage_gate`, `_marginal_made_river_fold_gate`) must be re-verified live in the CURRENT bot before use, since crossover drops them silently.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` / offense mechanisms can drop on ancestry/crossover (v242); re-grep current bot. Telemetry-first (fire-rate ≥5% @≥30g); ≥100g paired net-chip proof required before deployment.
- Board-texture bluff-raise (offense axis, distinct from the fold-floor ban): retired as caution unless ≥100g WR/net-chip revives it. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- large_bet_ratio measures opp BET sizing, not calling tendency; for value-overbet gates fold_to_raise is the correct EV signal.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo for ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire ONLY confidence≥0.10 AND large_bet_ratio≥0.50 AND fold_to_raise<0.50). `_river_value_raise_construct` is present at v254 (defined opponent.py:1413, dispatched strategy.py:1806); DEFAULT-PERMIT body-branch reachability still needs a ≥30g check.
- Unconditional value caps (e.g. 0.85x) risk leaving chips vs calling-stations; gate on fold_to_raise>0.45, not universally.
- LOC caps are version-sensitive — re-measure before edits and reclaim LOC before adding logic.

## GENERAL
- Validate payload results, not plan cleanliness; Master produces plausible but strategically wrong rationales — pivot on critic local-optima warnings unless precommit/H2H proves otherwise.
- Trust git diff and head_to_head.json over commit messages/Master claims; verify crossover rationale (parent must lose to targets donor beats).
- Crossover ancestry can silently discard non-base mutations — inventory current functions/dispatch sites before declaring a mechanism new/missing/preserved (see `_completed_board_nut_disadvantage_gate` drop in v254).
- Master-pivot-off-prescribed-priorities is the project's #1 failure mode (3x-recurrent v247–v249): when the pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST before any offense-axis work; offense refinements on a bot with an unfixed documented leak should be REJECTED at direction_audit.
- Post-worker plan-vs-code reconciliation is mandatory; prior gens committed code contradicting the stated plan.
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet — verify the body + call-site arity + per-action branches, not just the trigger. Prefer dead-code removal/dispatch repair over adding constants.
- Precommit timeout fallback can mask weak evidence — distinguish a data-driven pass from a match_timeout retry/pass, and pause daemon interference before precommit.
- Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone; <30g noise, ≥30g actionable, ≥100g durable.
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams; short-stack trash jams can be necessary double-up escapes.

## RECENT_LESSONS
- **v254**: Preflop fold-gates must be conditioned on pot_odds (>0.30) AND opponent width (pfr/VPIP) — unconditional folds of dominated hands re-introduce the over-fold leak seen in v221's unconditional mid_pair tweak (which v222 had to VPIP-gate), since 'playable' hands like T7s retain positive realization vs loose stealers.
- **v254 归档建议**: Gate `_bb_vs_raise_dominated_floor` on pot_odds>0.30 and opponent pfr≤0.22 (tight/unknown only), then validate at ≥30g vs v250's ACTUAL nemeses (v243 0.40, v209/v238/v240/v246/v247 0.45) — not the critic's stale v241/v182/v197 list, which has no supporting entries in head_to_head.json.
