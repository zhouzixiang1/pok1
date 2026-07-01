## OPPONENT_MODELING
- Betsize-polarity should target PREFLOP raise magnitude / 4bet-response structure, not postflop floors; activation = lower sample-count gates + all-in sample recording, not confidence constants.
- Deal-local opp fields (`_preflop_shove_defense_fold`, `_facing_v4bet`, `revealed_shove_density`, `large_bet_ratio`, `opp_bet_ratio`, `fold_to_raise`, `confidence`) wire cleanly but tend placement-inert — re-grep current bot + prove target-scenario telemetry fires before relying on them.
- Archetype-axis ports saturate to `standard` and reappear without WR lift; do not reopen. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Raw made_strength vs pot_odds is structurally weak — prefer polarized/true equity.
- Value/fold aggression must be gated on live deal-local opp fields (value_maximizer_index, fold_to_bet_turn, VPIP) from the start; unconditional gates turn -EV fast.
- Placement-shadow is chronic (v214 river-guard, v242 fold-gate, v244/v245 offense gates): a guard can exist, pass unit tests, and stay dead. Require downstream control-flow reachability + target telemetry (returns None when gate unmet AND fires when met), not function presence.
- Fold gates must respect `_postflop_response_margin` / realized-rate coherence; non-all-in direct-fold dispatches bypassing continue-guards over-fold vs mixed-aggression.
- Sibling-gate alignment: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold` share pot-odds/made-strength surfaces; changing one alone yields inconsistent exploit behavior.
- Fold-side floor/constant nudges (`_estimate_bluff_frequency` underbettor floors, choose_raise/value-tier ceilings) are dead; opp-signal-gated fold restructuring is nominally actionable but has only ever delivered placement-inert results (v214/v242/v244/v245), and direction-audit has FORBADE fold-side in favor of OFFENSE. Live axis = offense mechanisms with ≥30g reachability + ≥100g net-chip proof. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` / offense mechanisms can drop on ancestry/crossover (confirmed v242; re-grep current bot). Telemetry-first diagnosis (fire-rate ≥5% @≥30g) allowed; no deployment without ≥100g paired net-chip proof.
- Board-texture bluff-raise has repeated for many generations without proven lift; pause unless ≥100g WR/net-chip revives it. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands wide enough to be reachable (≥15pp).
- Telemetry/stderr counts are not H2H proof: require reachability + ≥30g paired net-chips to act, ≥100g before declaring success. (stderr IS captured/readable — RESOLVED (A1), no longer needs active tracking.)
- LOC caps are version-sensitive — the ~v217/v218 figures (helpers @2500 cap, strategy @2493) are stale by v246; re-measure before edits and reclaim LOC before adding logic.

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
- **v246**: Gate DIRECTION is load-bearing — a DEFAULT-PERMIT opp-gate (blocks only on confident polarized-shover evidence) preserves status quo for ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire ONLY when confidence>=0.10 AND large_bet_ratio>=0.50 AND fold_to_raise<0.50) and workers checked against the master's intended fire-condition, not just presence.
- **v246**: large_bet_ratio measures opp BET sizing, not calling tendency — for value-overbet gates, fold_to_raise is the correct EV signal; a gate omitting it can't distinguish +EV calling stations from -EV folders.
- **v246 归档建议**: v247 priority — reverse `_river_value_raise_construct` to RESTRICTIVE (confidence>=0.10 AND large_bet_ratio>=0.50 AND fold_to_raise<0.50) AND land Worker 2's `_completed_board_nut_disadvantage_gate` at the gt0_after_margin site (L1661, 678 reaches vs 0 at allin_cover) for the G5H5/G2H13 -20k river all-in defense leaks vs v197/v182/v234.
- **v245**: `_river_value_raise_construct` shipped WITHOUT its declared opp-gate (opp_bet_ratio/fold_to_raise/confidence all dead params) — third placement-shadow-adjacent defect (v214, v242). Workers MUST verify every declared gate condition is an executable body branch returning None when unmet, not just that it fires when met.
- **v245**: stacked offense (river value raise) on v244's `_thin_value_extraction_sizing` WITHOUT the mandated reachability proof (≥5% @≥30g). Do NOT add further offense until v244 thin-value AND v245 river raise both prove fire-rate ≥5% vs nemeses (v195/v184/v214) at ≥30g.
- **v244**: placement-shadow recurring — v242's fold gate fired 0/96 despite correct dispatch; v244 wired dispatch BEFORE forcing gates. MANDATORY reachability telemetry (THIN_VALUE_EXTRACTION fire-rate ≥5% @≥30g vs v233 @0.225 / v206/v235 @0.35) before more offense.
