## OPPONENT_MODELING
- Betsize-polarity is a modeling target: aim at PREFLOP raise magnitude / 4bet-response structure, not postflop floors; activate via lower sample-count gates + all-in sample recording, not confidence constants.
- Deal-local opp fields wire cleanly but trend placement-inert — re-grep current bot + prove target-scenario telemetry fires before relying on them.
- Archetype-axis ports saturate to `standard` and reappear without WR lift; do not reopen. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Prefer polarized/true equity over raw made_strength vs pot_odds.
- `_marginal_made_river_fold_gate`: v242 was 0-fires/96g inert; v248–v250 prescribed wiring its dead `paired_board_profile` + moving SiteB after realized_rate — RE-CONFIRM at v250 whether it has actually landed/fires (≥30g) before treating as a live leak or as closed.
- Value/fold aggression must be gated on live deal-local opp fields (value_maximizer_index, fold_to_bet_turn, VPIP) from the start; unconditional gates turn -EV fast.
- Placement-shadow is chronic (v214 river-guard, v242/v244/v245/v247 gates): require downstream `get_action` dispatch reachability + target telemetry (≥30g, fire-rate ≥5%), not isolated-function presence.
- Fold gates must respect `_postflop_response_margin` / realized-rate coherence; non-all-in direct-fold dispatches bypassing continue-guards over-fold vs mixed-aggression.
- Sibling-gate alignment: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold` share pot-odds/made-strength surfaces; change one alone → inconsistent exploit behavior.
- Fold-side POLICY — GENERIC nudges BANNED (closed): `_estimate_bluff_frequency` underbettor floors, choose_raise/value-tier ceilings are FORBADE & dead. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]
- Fold-side POLICY — sanctioned DEFENSE exception (OPEN, distinct from the ban): a *targeted* gate at a telemetry-confirmed dispatch site stays permitted — `_completed_board_nut_disadvantage_gate` (strategy.py ~L1661, gt0_after_margin, 678 reaches) for G5H5/G2H13 river leaks, plus repairing `_marginal_made_river_fold_gate` per above. This is NOT a generic floor/constant.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` / offense mechanisms can drop on ancestry/crossover (v242); re-grep current bot. Telemetry-first (fire-rate ≥5% @≥30g); ≥100g paired net-chip proof required before deployment.
- Board-texture bluff-raise (offense axis, distinct from the fold-floor ban): retired as caution unless ≥100g WR/net-chip revives it. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- large_bet_ratio measures opp BET sizing, not calling tendency; for value-overbet gates fold_to_raise is the correct EV signal. (v246, advisory)
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo for ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire ONLY confidence≥0.10 AND large_bet_ratio≥0.50 AND fold_to_raise<0.50). `_river_value_raise_construct` DEFAULT-PERMIT was last confirmed at v246 (4 gens stale) — re-grep at v250 to confirm whether still live before re-issuing.
- RESOLVED (A1): bot stderr telemetry is readable/captured but is NOT H2H proof — require reachability + ≥30g paired net-chips to act, ≥100g to declare success.
- LOC caps are version-sensitive — re-measure before edits and reclaim LOC before adding logic.

## GENERAL
- Validate payload results, not plan cleanliness; Master produces plausible but strategically wrong rationales — pivot on critic local-optima warnings unless precommit/H2H proves otherwise.
- Trust git diff and head_to_head.json over commit messages/Master claims; verify crossover rationale (parent must lose to targets donor beats).
- Crossover ancestry can silently discard non-base mutations — inventory current functions/dispatch sites before declaring a mechanism new/missing/preserved.
- Post-worker plan-vs-code reconciliation is mandatory; prior gens committed code contradicting the stated plan.
- Prefer dead-code removal/dispatch repair over adding constants; verify call-site arity and per-action branches.
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet, not merely fire when met — verify the body, not just the trigger.
- Precommit timeout fallback can mask weak evidence — distinguish a data-driven pass from a match_timeout retry/pass, and pause daemon interference before precommit.
- Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone; <30g noise, ≥30g actionable, ≥100g durable.
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams; short-stack trash jams can be necessary double-up escapes.

## RECENT_LESSONS
- **v250**: Pot-odds equity fold paths that bypass opp-model gates require a principled equity estimate; made_strength*(0.40+bluff_freq) over-discounts polarized equity ~2x (made=0.25→0.16 vs true ~0.30) and produces systematic over-folds. Use eq ≈ bf*0.92 + (1-bf)*0.10 for made<0.30 polarized spots, OR gate firing on opp signals (large_bet_ratio≥0.45 AND confidence≥0.20) rather than firing unconditionally.
- **v250**: The v242 diagnosis (opp-signal GATING open, NOT another floor/constant) remains the correct direction; v250's bypass trades one inertness for an over-fold risk. Future Master should NOT re-attempt bypass — instead recalibrate the vmi≤0.40/ftb≥0.40 thresholds that caused the gate to never fire.
- **v250 归档建议 (mixed)**: Before v251, run ≥30g river telemetry vs v237 and v207 (bluff-heavy opponents v249 beats 0.60/0.65) — if fragile_two_pair hands in [0.38,0.45) fold where v249 was calling, the unconditional cal_eq bypass is leaking; either restrict firing to large_bet_ratio≥0.45 OR replace the formula with bf*0.92+(1-bf)*0.10 rather than reverting Change 2 wholesale.
- **v249 (3x-recurrent v247–v249)**: Master-pivot-off-prescribed-priorities is the project's #1 failure mode. When the pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST before any offense-axis work; offense-axis refinements on a bot with an unfixed documented fold-side leak should be REJECTED at direction_audit, not merely scored low by critic.
- **v249**: `_tier_opp_sizing_directive` is a legitimate structural improvement but NOT the current bottleneck — shelve until `_marginal_made_river_fold_gate` / `_completed_board_nut_disadvantage_gate` river leaks are confirmed fixed at ≥30g NET-CHIPS vs v200/v238/v208; do NOT spend another generation on sizing caps until then.
- **v248–v249 归档建议**: v250 MUST land the v248-prescribed fold-side fix: wire `_marginal_made_river_fold_gate`'s dead `paired_board_profile` to TIGHTEN made_strength ceiling 0.45→0.38 when trips_vulnerable/fragile_two_pair/weakened (the 0.20≤made<0.45 over-call leak band vs v200/v238/v208), with a RESTRICTIVE opp-gate (vmi>0.40 OR ftb_turn<0.40, confidence≥0.25) applied BEFORE the paired_board tightening per the v246 default-permit lesson — validate at ≥30g on NET-CHIPS, not W-L.
- **v248**: Unconditional value caps (0.85x) risk leaving chips vs calling-stations (low fold_to_raise); if reachability shows the cap binding vs high-VPIP bots, gate on fold_to_raise>0.45, not universally.
