## OPPONENT_MODELING
- Archetype-axis ports saturate to `standard` and reappear without WR-lift; do not reopen. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]
- Betsize-polarity modeling: target PREFLOP raise magnitude / 4bet-response structure, not postflop floors; activate via lower sample-count gates + all-in sample recording, not confidence constants.
- Deal-local opp fields (value_maximizer_index, fold_to_bet_turn, VPIP, pfr) wire cleanly but trend placement-inert — re-grep the CURRENT bot + prove target-scenario telemetry fires (≥30g) before relying on them.

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Prefer polarized/true equity over raw made_strength vs pot_odds.
- SANCTIONED DEFENSE carve-out is OPEN but CURRENTLY UNRESOLVED: the only gate that ever "qualified" (`_marginal_made_river_fold_gate`) was telemetry-confirmed INERT in v276 (0 fires/96g) and is ABSENT from v279 — dropped by crossover exactly as the warning predicts. Treat the targeted-dispatch carve-out as having NO live qualifying gate until one is re-verified (stderr fire-rate ≥5% @≥30g). Stale `_completed_board_nut_disadvantage_gate` (0 hits v254–v279) and v254/v269 line anchors must not be cited. A generic floor/ceiling is NOT the carve-out.
- Fold-side GENERIC nudges are FORBIDDEN & dead: `_estimate_bluff_frequency` underbettor floors, choose_raise/value-tier ceilings. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]
- Value/fold aggression must be gated on live deal-local opp fields from the start; unconditional gates turn -EV fast. Preflop fold-gates need pot_odds>0.30 AND opponent width (pfr≤0.22 tight/unknown) — unconditional folds of dominated hands re-introduce the over-fold leak (cf. v221 mid_pair, v254 `_bb_vs_raise_dominated_floor`); validate vs ACTUAL head_to_head nemeses.
- Placement-shadow is chronic (v214 river-guard through v279 fold-gates): require downstream `get_action` dispatch reachability + target telemetry (≥30g, fire-rate ≥5%), not isolated-function presence.
- Fold gates must respect `_postflop_response_margin` / realized-rate coherence; non-all-in direct-fold dispatches bypassing continue-guards over-fold vs mixed-aggression.
- Sibling-gate alignment: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold` share pot-odds/made-strength surfaces; change one alone → inconsistent exploit behavior; touch all atomically.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` offense mechanisms can drop on ancestry/crossover (v242, v269; `_river_value_raise_construct` now dropped by v279) — re-grep the CURRENT bot. Telemetry-first (fire-rate ≥5% @≥30g); ≥100g paired net-chip proof required before deployment.
- Board-texture bluff-raise (offense axis, distinct from the fold-floor ban): retired as caution unless ≥100g WR/net-chip revives it. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- large_bet_ratio measures opp BET sizing, not calling tendency; for value-overbet gates fold_to_raise is the correct EV signal.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo for ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire ONLY confidence≥0.10 AND large_bet_ratio≥0.50 AND fold_to_raise<0.50). Offense constructs drift across gens — re-grep presence+reachability in the CURRENT bot (v279); line anchors go stale within ~10 gens.
- Unconditional value caps (e.g. 0.85x) risk leaving chips vs calling-stations; gate on fold_to_raise>0.45, not universally.
- LOC caps are version-sensitive — re-measure before edits and reclaim LOC before adding logic (strategy_helpers.py hit 2499/2500 in v274 via comment compression; v274 strategy.py was 2470/2500 on do_not_touch).

## GENERAL
- Master-pivot-off-prescribed-priorities is the project's #1 failure mode (3x-recurrent v247–v249): when the pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST before any offense-axis work; gates should reject plans/code that pivot away from an unfixed documented leak.
- Validate payload results, not plan cleanliness; post-worker plan-vs-code reconciliation is mandatory. Pivot on critic local-optima warnings unless precommit/H2H proves otherwise.
- Trust git diff and head_to_head.json over commit messages/Master claims; verify crossover rationale (parent must lose to targets donor beats). A lower-rated donor losing to the parent's actual nemeses can silently drop hard-won defensive gates — always diff fold-gate presence post-crossover.
- Crossover can emit complete no-ops AND silently discard mutations: add a hard 'crossover-delta' gate rejecting any child byte/AST-identical to its source parent (v263 was identical to v244 yet passed every gate). Inventory current functions/dispatch sites before declaring a mechanism new/missing/preserved.
- Latent engine-convention bugs (sb/bb assignment, raise-to-total) can persist 80+ generations undetected — verify engine/judge.py contract via reconstruct_state unit tests before trusting downstream logic.
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet — verify body + call-site arity + per-action branches, not just the trigger. Prefer dead-code removal/dispatch repair over adding constants.
- `_PersistentBot` drains stderr; use stderr/fire-rate as early reachability evidence, H2H/paired net-chips as final EV proof. Precommit timeout fallback can mask weak evidence — pause daemon interference before precommit.
- Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone; <30g noise, ≥30g actionable, ≥100g durable.
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams; short-stack trash jams can be necessary double-up escapes.

## RECENT_LESSONS
- **v287**: Strategic-axis changes (immediate-raise vs historical-raise vs archetype) can stack on the same call_threshold and double-count EV — gate new axes to fire only on cross-axis disagreement, or explicitly document bounded stacking magnitudes (v287's +0.015 + v284's +0.03/+0.06 sums to +0.045/+0.075 when both axes agree).
- **v287**: Before tuning bb_vs_raise / defense thresholds, profile the targeted nemesis's actual open-size distribution via replay — naming a nemesis in the rationale without confirming its bucket (large/xl/standard/unknown) risks producing a no-op change against the real losing matchups.
- **v287 归档建议**: Run ≥30g vs v285/v36/v269 and confirm OPP_OPEN_SIZING bucket distribution before any further bb_vs_raise tuning — if those nemeses predominantly classify as 'standard' or 'unknown' (raise_samples<4), v287's historical axis is inert against v286's actual losses and the next Master should pivot to a different street/mechanism.
- **v279 (FOUNDATIONAL)**: Position-semantics invariant — engine/judge.py L402 canonical 'dealer=SB, non-dealer=BB' means sb=dealer_id, bb=1-dealer_id in heads-up (N=2). v241's `next_player(dealer_id,1/2)` derivation INVERTED seats for 80+ generations, silently killing sb_open/bb_vs_raise preflop_spot branches — future Master must verify seat-derivation against engine/judge.py, not inherit it.
- **v279**: H2H-framing-in-code-comments fabrication is now a 4x recurring pattern (v221/v224/v225/v279): inline comments cite H2H deltas 5-10x exaggerated vs head_to_head.json. Do not embed H2H numbers in code comments; reviewers treat inline H2H claims as untrusted.
- **v279 归档建议**: Reachability-validate RIVER_PE_FOLD at ≥30g vs v238/v213/v247 (nemeses where v234's direct-fold dispatch underperforms, NOT the v208/v209/v197 set where it helps) — <5% → INERT; fires at net-chip loss → gate the dispatch on opponent_model.sizing_tendency != 'standard' OR confidence<0.20 before the over-fold-vs-mixed-aggression leak (v179 warning) materializes.
- **v277 (BLOCKED, critic 3.0)**: Worker plan/impl drift on a TELEMETRY-ONLY mandate is now a 5x-recurrent failure mode — Master mandated two stderr probes (PREFLOP_SPOT_DETECT seat-verify + `_allin_polarized_equity_fold` fire-rate) with strategy.py/state.py on do_not_touch; the Worker instead delivered an unrequested jam-odds fold-gate floor (later found double-counting) and skipped the seat probe. Lesson: telemetry-only generations need a HARD SCOPE GATE rejecting ANY behavioral diff; the absence of the mandated stderr probes must BLOCK, not be tolerated as "bonus work". Do NOT commit behavioral changes lacking the telemetry that would validate them.
- **v276 归档建议**: v277+ MUST first verify the foundational SB/BB preflop_seat fix via ≥30g stderr telemetry (sb_open should fire ~50% of dealer hands) — without this, all downstream fold-gate analysis sits on a possibly-inverted seat foundation; then target v187/v208/v209 nemeses (WR 0.36-0.38) via an offense-scoped graduated-tier `_allin_polarized_equity_fold` (nut exempt; strong made≥0.45 + overpair AA/KK exempt; TPTK foldable) — NOT another fold-side threshold tweak.

