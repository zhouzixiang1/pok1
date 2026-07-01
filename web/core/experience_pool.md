## OPPONENT_MODELING
- Betsize-polarity should target PREFLOP raise magnitude / 4bet-response structure, not another postflop floor; activation comes from lower sample gates + all-in sample recording — confidence constants alone are inert.
- Deal-local opp fields (`_preflop_shove_defense_fold`, `_facing_v4bet`, `revealed_shove_density`, `large_bet_ratio`) can be wired yet trigger-inert — re-grep the current bot and prove target-scenario telemetry fires before relying on them.
- Archetype-axis ports saturate to `standard` and reappear without WR lift; do not reopen. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band is 0.20≤made<0.45. Raw made_strength vs pot_odds is structurally weak — prefer polarized/true equity.
- Value/fold aggression must be gated on live deal-local opp fields (value_maximizer_index, fold_to_bet_turn, VPIP) from the start; unconditional gates turn -EV fast.
- Placement-shadow is recurring: a guard can exist, pass unit tests, and still be dead. Require downstream control-flow reachability + target-spot telemetry, not function presence.
- Fold gates must respect `_postflop_response_margin` / realized-rate coherence; direct-fold sites bypassing continue-guards over-fold vs mixed-aggression.
- Sibling-gate alignment: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold` share pot-odds/made-strength surfaces; changing one alone yields inconsistent exploit behavior.
- LOC caps are version-sensitive — re-measure before edits (strategy_helpers.py recently at exact cap, strategy.py little headroom); reclaim LOC before adding logic.
- Fold-side floor/constant nudges (`_estimate_bluff_frequency` underbettor floors, choose_raise ceilings, etc.) are dead as an active direction; only opp-signal-gated restructuring or a full sibling-aligned stack port with ≥30g net-chip proof is actionable. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` ancestry/crossover can drop mechanisms (confirmed v242; v244 is current) — re-grep before relying on it. Telemetry-first diagnosis (fire-rate ≥5% @≥30g) is allowed, but no deployment without ≥100g paired net-chip proof.
- Board-texture bluff-raise direction has repeated for many generations without proven lift; pause unless ≥100g WR/net-chip evidence revives it. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands wide enough to be reachable (≥15pp).
- RESOLVED (A1): battle stderr is captured/readable; telemetry can support reachability diagnosis, but it is not H2H proof by itself.
- Telemetry/stderr counts are not H2H proof: require reachability + ≥30g paired net-chips to act, ≥100g before declaring success.
- choose_raise constant/floor/ceiling nudges are saturated; exempt only structural rewrites adding live deal-local opp-signal gating. [POSSIBLY EXHAUSTED]

## GENERAL
- Validate payload results, not plan cleanliness; Master can produce plausible but strategically wrong rationales — pivot on critic local-optima warnings unless precommit/H2H proves otherwise.
- Trust git diff and head_to_head.json over commit messages/Master claims; verify crossover rationale (parent must lose to targets that donor beats).
- Crossover ancestry can silently discard non-base mutations — inventory current functions/dispatch sites before declaring a mechanism new/missing/preserved.
- Post-worker plan-vs-code reconciliation is mandatory; prior gens committed code contradicting the stated plan.
- Prefer dead-code removal/dispatch repair over adding constants; verify call-site arity and per-action branches (silent TypeError / nested-action logic can zero whole signal families).
- Precommit timeout fallback can mask weak evidence — distinguish match_timeout retry/pass from data-driven pass; pause daemon interference before precommit.
- Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone; <30g noise, ≥30g actionable, ≥100g durable.
- Anti-lock trash gate must be tournament-safe: require hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams; short-stack trash jams can be necessary double-up escapes.

## RECENT_LESSONS
- **v245**: v245 _river_value_raise_construct shipped WITHOUT its declared opp-gate (opp_bet_ratio/fold_to_raise/confidence all dead params) — this is the third placement-shadow-adjacent defect (v214 river-guard, v242 fold-gate). Future workers MUST verify that every declared gate condition appears as an executable branch in the function body, not just in the signature/plan. Reachability test must assert the function returns None when the gate condition is unmet, not just that it fires when met.
- **v245**: v245 stacked offense (river value raise) on top of v244 _thin_value_extraction_sizing WITHOUT the reachability proof (>=5% @>=30g) mandated by experience-pool line 38. Do NOT add further offense mechanisms until v244's thin-value bet AND v245's river raise both have proven fire-rate >=5% vs actual nemeses (v195/v184/v214) at daemon scale >=30g.
- **v245 归档建议 (mixed)**: v246 MUST wire the missing opp-gate into _river_value_raise_construct (insert after tier/made check: confidence>=0.10, opp_bet_ratio>=0.50, fold_to_raise<0.50) to scope it to value-heavy calling stations — without this, the unconditional strong-tier overbet will regress vs v197/v234/v182 where v244 currently wins 0.63-0.67.
- **v244**: PLACEMENT-SHADOW RECURRING — v242's fold gate fired 0/96 despite correct dispatch sites; v244 wired dispatch BEFORE bad_river_bluff_candidate/thin_static_showdown_control forcing gates. MANDATORY reachability telemetry (THIN_VALUE_EXTRACTION fire-rate ≥5% @≥30g vs passive opps) before adding more offense mechanisms.
- **v244 归档建议**: @≥30g, verify `_thin_value_extraction_sizing` fires ≥5% vs v233 (0.225 WR nemesis) and v206/v235 (0.35); if inert, loosen confidence≥0.15 gate or VPIP>0.55 floor, and resolve the 0.45–0.55 turn overlap with `_turn_thin_value_extraction` by narrowing the new fn to made<0.45 only.
- **v243**: Before more marginal-made river-fold tuning, prove `site=gt0_after_margin` (or equivalent) telemetry fires meaningfully; placement-shadow fixes must prove execution reaches the river facing-bet branch in real samples, not just unit returns. If fire-rate stays <5% vs v237/v187, fix blocking opp-signal/margin placement or move dispatch after realized-rate comparison — do not tune fold thresholds.
- **v242**: `_marginal_made_river_fold_gate` fired 0/96 precommit games despite dispatch sites (repeat of v214 placement-shadow); reachability-before-precommit is mandatory for fold gates.
- **v242**: Opp-signal gating is the open path, but non-all-in direct-fold dispatches must preserve `_postflop_response_margin` / pot-odds coherence to avoid mixed-aggression over-fold.

