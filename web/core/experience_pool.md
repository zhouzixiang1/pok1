## OPPONENT_MODELING
- Archetype-axis ports saturate to `standard` and reappear without WR-lift; do not reopen. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]
- Betsize-polarity modeling: target PREFLOP raise magnitude / 4bet-response structure, not postflop floors; activate via lower sample-count gates + all-in sample recording, not confidence constants.
- Deal-local opp fields (value_maximizer_index, fold_to_bet_turn, VPIP, pfr) wire cleanly but trend placement-inert — re-grep current bot + prove target-scenario telemetry fires (≥30g) before relying on them.

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Prefer polarized/true equity over raw made_strength vs pot_odds.
- SANCTIONED DEFENSE EXCEPTION (OPEN, NOT a generic floor): a *targeted* gate at a telemetry-confirmed dispatch site is permitted — currently only `_marginal_made_river_fold_gate` qualifies; re-grep + re-verify it live in the CURRENT bot (v276) every gen, since crossover drops gates silently. (The prior `_completed_board_nut_disadvantage_gate` reference was a phantom — 0 hits v254–v276; v254/v269 line anchors are stale.)
- Fold-side GENERIC nudges are FORBIDDEN & dead: `_estimate_bluff_frequency` underbettor floors, choose_raise/value-tier ceilings. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]. The targeted defense exception above is the ONLY sanctioned carve-out; a generic floor/ceiling is not it.
- Value/fold aggression must be gated on live deal-local opp fields from the start; unconditional gates turn -EV fast. Preflop fold-gates likewise need pot_odds>0.30 AND opponent width (pfr≤0.22 tight/unknown) — unconditional folds of dominated hands re-introduce the over-fold leak (cf. v221 mid_pair, v254 `_bb_vs_raise_dominated_floor`); validate vs ACTUAL head_to_head nemeses, not stale critic lists.
- Placement-shadow is chronic (v214 river-guard through v276 fold-gates): require downstream `get_action` dispatch reachability + target telemetry (≥30g, fire-rate ≥5%), not isolated-function presence.
- Fold gates must respect `_postflop_response_margin` / realized-rate coherence; non-all-in direct-fold dispatches bypassing continue-guards over-fold vs mixed-aggression.
- Sibling-gate alignment: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold` share pot-odds/made-strength surfaces; change one alone → inconsistent exploit behavior; touch all atomically.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` / offense mechanisms can drop on ancestry/crossover (v242, v269); re-grep current bot. Telemetry-first (fire-rate ≥5% @≥30g); ≥100g paired net-chip proof required before deployment.
- Board-texture bluff-raise (offense axis, distinct from the fold-floor ban): retired as caution unless ≥100g WR/net-chip revives it. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- large_bet_ratio measures opp BET sizing, not calling tendency; for value-overbet gates fold_to_raise is the correct EV signal.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo for ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire ONLY confidence≥0.10 AND large_bet_ratio≥0.50 AND fold_to_raise<0.50). Offense constructs (`_river_value_raise_construct`, `_semibluff_raise_construct`) drift across gens — re-grep presence+reachability in the CURRENT bot (v276); line anchors go stale within ~10 gens.
- Unconditional value caps (e.g. 0.85x) risk leaving chips vs calling-stations; gate on fold_to_raise>0.45, not universally.
- LOC caps are version-sensitive — re-measure before edits and reclaim LOC before adding logic.

## GENERAL
- Master-pivot-off-prescribed-priorities is the project's #1 failure mode (3x-recurrent v247–v249): when the pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST before any offense-axis work; direction_audit + master_plan_audit + quality gates should reject plans/code that pivot away from an unfixed documented leak.
- Validate payload results, not plan cleanliness; post-worker plan-vs-code reconciliation is mandatory — prior gens committed code contradicting the stated plan. Pivot on critic local-optima warnings unless precommit/H2H proves otherwise.
- Trust git diff and head_to_head.json over commit messages/Master claims; verify crossover rationale (parent must lose to targets donor beats). Crossover to a lower-rated donor that loses to the parent's actual nemeses violates the mandate and can silently drop hard-won defensive gates the donor predates — always diff fold-gate presence post-crossover.
- Crossover can emit complete no-ops AND silently discard non-base mutations: add a hard 'crossover-delta' gate rejecting any child byte/AST-identical to its source parent (comment/whitespace-only diff) — v263 (v244×v196) was identical to v244, v196's logic never transferred, yet sailed through every gate green. Inventory current functions/dispatch sites before declaring a mechanism new/missing/preserved.
- Latent engine-convention bugs (sb/bb assignment, raise-to-total semantics) can persist 30+ generations undetected — verify engine/judge.py contract assumptions via reconstruct_state unit tests before trusting downstream strategy logic.
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet — verify body + call-site arity + per-action branches, not just the trigger. Prefer dead-code removal/dispatch repair over adding constants.
- `_PersistentBot` drains stderr and stderr telemetry is readable — use stderr/fire-rate as early reachability evidence, H2H/paired net-chips as final EV proof. Precommit timeout fallback can mask weak evidence — distinguish a data-driven pass from a match_timeout retry/pass, and pause daemon interference before precommit.
- Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone; <30g noise, ≥30g actionable, ≥100g durable.
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams; short-stack trash jams can be necessary double-up escapes.

## RECENT_LESSONS
- **v276**: placement-shadow confirmed chronic — fold gates (`_allin_polarized_equity_fold`, `_river_potodds_equity_margin`, `_preflop_shove_defense_fold`, `_multibarrel_line_fold`) MUST ship with STDERR fire-rate telemetry + ≥30g paired net-chips vs nemeses before a generation can claim strategic progress (v214 river-guard was inert 23 gens; v242 fold-gate fired 0/96g).
- **v276**: Sibling-gate divergence is now a recurring failure mode — `_multibarrel_line_fold` edits without coordinating `_aggro_bluffcatcher_should_fold` / `_rock_value_bet_fold` create inconsistent exploit surfaces vs the same loose-caller signal; fold-side edits MUST touch all sibling gates atomically.
- **v276 归档建议**: v277 MUST first verify the foundational SB/BB preflop_spot fix via ≥30g stderr telemetry (sb_open should fire ~50% of hands as dealer) — without this, all downstream fold-gate analysis sits on a possibly-inverted seat foundation; then target v187/v208/v209 nemeses (WR 0.36–0.38) via an offense-scoped port of a graduated-tier `_allin_polarized_equity_fold` (nut exempt; strong made≥0.45 + overpair AA/KK exempt; TPTK foldable) to stop the documented -20k two-pair stack-offs — NOT another fold-side threshold tweak.
- **v274 (FOUNDATIONAL)**: `next_player(dealer,1/2)` with N_PLAYERS=2 silently INVERTS SB/BB seats in heads-up — my_is_sb/my_is_bb were swapped 80+ generations, breaking sb_open/bb_vs_raise preflop_spot detection; any future preflop analysis must verify sb_open fires ~50% of hands as dealer before trusting downstream sizing deltas.
- **v274**: strategy_helpers.py is at 2499/2500 LOC (1-line headroom) — v274 hit the cap via comment compression at L420-431/L1528-1534; the next edit touching this file MUST reclaim LOC first or split the module, else quality gates will block.
- **v274 归档建议 (mixed)**: Before any further fold-side work in v275+, mandate ≥30g reachability proof that (1) sb_open preflop_spot now fires ≥5% (stderr PREFLOP_OFFSUIT_GATE/sb_open telemetry) confirming the SB/BB fix landed, and (2) `_estimate_bluff_frequency` returns >0.10 vs the field so the `calibrated_equity=made*(0.40+bluff_freq)` basis in `_allin_polarized_equity_fold` does not over-fold TPTK vs mixed-aggression opponents like the v206-type nemeses documented across v219–v242.
