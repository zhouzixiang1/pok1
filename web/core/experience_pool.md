## OPPONENT_MODELING
- Betsize-polarity is a *modeling* target: aim it at PREFLOP raise magnitude / 4bet-response structure, not postflop floors; activation = lower sample-count gates + all-in sample recording, not confidence constants.
- Deal-local opp fields (`_preflop_shove_defense_fold`, `fold_to_raise`, `opp_bet_ratio`, `confidence`, etc.) wire cleanly but tend placement-inert — re-grep current bot + prove target-scenario telemetry fires before relying on them.
- Archetype-axis ports saturate to `standard` and reappear without WR lift; do not reopen. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Raw made_strength vs pot_odds is structurally weak — prefer polarized/true equity. The live leak is `_marginal_made_river_fold_gate` (made<0.45 generic, v242/v248) — exactly this unoperationalized surface.
- Value/fold aggression must be gated on live deal-local opp fields (value_maximizer_index, fold_to_bet_turn, VPIP) from the start; unconditional gates turn -EV fast.
- Placement-shadow is chronic (v214 river-guard, v242/v244/v245/v247 gates): a guard can exist, pass unit tests, and stay dead. Require downstream `get_action` dispatch reachability + target telemetry (≥30g, fire-rate ≥5%), not isolated-function presence.
- Fold gates must respect `_postflop_response_margin` / realized-rate coherence; non-all-in direct-fold dispatches bypassing continue-guards over-fold vs mixed-aggression.
- Sibling-gate alignment: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold` share pot-odds/made-strength surfaces; change one alone → inconsistent exploit behavior.
- Fold-side POLICY boundary (sharpened): GENERIC floor/constant nudges (`_estimate_bluff_frequency` underbettor floors, choose_raise/value-tier ceilings) are FORBADE & dead [STALE — no WR-lift] [POSSIBLY EXHAUSTED]. Note: "no postflop FLOORS/constant nudges" ≠ "no postflop FOLD GATES" — the one sanctioned exception is a *targeted* defense gate at a telemetry-confirmed dispatch site, NOT a generic nudge: `_completed_board_nut_disadvantage_gate` (strategy.py ~L1661, gt0_after_margin, 678 reaches) for G5H5/G2H13 river leaks, plus repairing `_marginal_made_river_fold_gate` (wire `paired_board_profile` / move SiteB after realized_rate). [POSSIBLY EXHAUSTED]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` / offense mechanisms can drop on ancestry/crossover (confirmed v242); re-grep current bot. Telemetry-first (fire-rate ≥5% @≥30g) before deployment; ≥100g paired net-chip proof required.
- Board-texture bluff-raise (offense axis, distinct from the fold-floor ban above): retired as caution unless ≥100g WR/net-chip revives it. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- large_bet_ratio measures opp BET sizing, not calling tendency; for value-overbet gates fold_to_raise is the correct EV signal — a gate omitting it can't distinguish +EV calling stations from -EV folders. (v246, advisory.)
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo for ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire ONLY confidence≥0.10 AND large_bet_ratio≥0.50 AND fold_to_raise<0.50). ⚠ live `_river_value_raise_construct` stayed DEFAULT-PERMIT through v246 — stop re-issuing without resolving. (v246, advisory.)
- Telemetry/stderr counts are not H2H proof: require reachability + ≥30g paired net-chips to act, ≥100g before declaring success.
- LOC caps are version-sensitive — re-measure before edits and reclaim LOC before adding logic.

## GENERAL
- Validate payload results, not plan cleanliness; Master produces plausible but strategically wrong rationales — pivot on critic local-optima warnings unless precommit/H2H proves otherwise.
- Trust git diff and head_to_head.json over commit messages/Master claims; verify crossover rationale (parent must lose to targets donor beats).
- Crossover ancestry can silently discard non-base mutations — inventory current functions/dispatch sites before declaring a mechanism new/missing/preserved.
- Post-worker plan-vs-code reconciliation is mandatory; prior gens committed code contradicting the stated plan.
- Prefer dead-code removal/dispatch repair over adding constants; verify call-site arity and per-action branches.
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet, not merely fire when met — verify the body, not just the trigger.
- Precommit timeout fallback can mask weak evidence — distinguish match_timeout retry/pass from data-driven pass; pause daemon interference before precommit.
- Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone; <30g noise, ≥30g actionable, ≥100g durable.
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams; short-stack trash jams can be necessary double-up escapes.

## RECENT_LESSONS
- **v247-v249 (3x-recurrent)**: Master-pivot-off-prescribed-priorities is the project's #1 failure mode. When the experience pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST before any offense-axis work; offense-axis refinements on a bot with an unfixed documented fold-side leak should be REJECTED at direction_audit, not merely scored low by critic.
- **v249**: `_tier_opp_sizing_directive` is a legitimate structural improvement but NOT the current bottleneck — shelve until `_marginal_made_river_fold_gate` / `_completed_board_nut_disadvantage_gate` river leaks are confirmed fixed at ≥30g NET-CHIPS vs v200/v238/v208; do NOT spend another generation on sizing caps until then.
- **v248-v249 归档建议**: v250 MUST land the v248-prescribed fold-side fix: wire `_marginal_made_river_fold_gate`'s dead `paired_board_profile` param to TIGHTEN made_strength ceiling 0.45→0.38 when trips_vulnerable/fragile_two_pair/weakened (the 0.20≤made<0.45 over-call leak band vs v200/v238/v208), with a RESTRICTIVE opp-gate (vmi>0.40 OR ftb_turn<0.40, confidence≥0.25) BEFORE the paired_board tightening per the v246 default-permit lesson — validate at ≥30g on NET-CHIPS, not W-L.
- **v248**: Unconditional value caps (0.85x) risk leaving chips vs calling-stations (low fold_to_raise); if reachability shows the cap binding vs high-VPIP bots, gate on fold_to_raise>0.45, not universally.
- **v247**: Land `_completed_board_nut_disadvantage_gate` at strategy.py ~L1661 (gt0_after_margin, 678 confirmed reaches) for G5H5/G2H13 river all-in DEFENSE leaks vs v197/v182/v234, AND reverse `_river_value_raise_construct` to RESTRICTIVE — the sanctioned defense exception.
