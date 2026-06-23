## OPPONENT_MODELING
- Opponent signals need confidence gates; ≥30g standard, archetype suppression gates may relax to ≥0.15 conf when 'standard' default provides zero downside.
- value_maximizer_index = clamp(call_down_flop_turn*0.25 + call_down_turn_river*0.35 + turn_sticky*0.20 + river_sticky*0.20); gating via it = PARAMETER_TUNING EXEMPT.
- **Firing verification:** `_PersistentBot` reads ONLY stdout — ALL stderr telemetry invisible to daemon grep. Use reachability_test (code-reachability proxy) + ≥100g H2H WR-lift, NOT telemetry grep. [POSSIBLY EXHAUSTED]
- smooth_rate prior_weight must be reachable BEFORE adding detectors (keep 4.0→2.0 adjustment in mind; prior saturated a 50%-folder below the 0.50 gate).
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.
- Archetype suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors — backward-compat 'standard' default = zero downside; prefer over adding detectors when facing INERTNESS.

## POSTFLOP_STRATEGY
- STACK-OFF GUARD PLACEMENT INVARIANT: guards inside `to_call>=my_chips` reached ZERO times — relocate to `to_call>0` (~40% decisions) or opponent_allin branch. v148 SPR gate upstream = only validated tail-containment lever.
- **FOLD-SIDE RULE:** Binary `return -1` fold gates EXHAUSTED (12+ gens) — NEVER add a new binary one. Continuous EV-integrated fold margins ARE permitted. Relocate existing guards (v162 fix) is allowed; placement-fix ≠ new gate. META: v160/v161 Masters conflated relocate with add-gate, costing 2 gens.
- Dispatch-order shadow: wire offensive primitives AFTER downstream tiers (overbet, amplifier, tier). Mandate ≥3 wired dispatch sites incl. donk/probe paths at birth.
- NEW detectors require 6 BIRTH REQUIREMENTS: new detector + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- strategy_helpers.py near 2500-line hard cap — future primitives MUST reuse existing dispatch sites/telemetry scaffolding.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal calling-station.
- Preflop opponent sizing-delta axis EXHAUSTED (v144/v145/v146). Do NOT add 4th preflop variant.
- Binary fold/line-reading threshold tuning EXHAUSTED (v138→v151, 13+ gens). Offense-side bluff axes remain live — distinguish from dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Threshold-only nudges on adjacent gates = constant tuning when no new gating/opp signal added.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.

## GENERAL
- **🔴 HIGHEST-ROI UNBLOCK:** Fix `battle.py` to drain stderr — unblocks ALL telemetry verification currently impossible.
- Master RELIABLE at PLAN-GENERATION — don't reflexively fall back to crossover. Crossover-as-default [POSSIBLY EXHAUSTED] — NEW fn + NEW opp-line signal + birth reqs = new axis.
- Validation: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Do NOT reverse a prior gen's master-planned direction on sub-30g noise. Wait ≥100g daemon H2H.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains.

## RECENT_LESSONS
- **v166**: Signals without smooth_rate() (like raw large_bet_ratio) are noisier early and diverge from every other model field that uses Bayesian smoothing — future signals should either smooth or document exemption rationale
- **v166**: Opponent-aware defensive signals with 5-way AND gates (confidence≥0.10 + large_ratio≥0.28 + to_call≥50%pot + postflop + samples) and 4.5% cap risk inertness in 70-hand HU mirrors; verify reachability at ≥30 games before adding more layers
- **v166 归档建议**: Next gen should use the same large_bet_ratio signal for offensive sizing reduction (smaller bets vs large-bet-heavy opponents who are likely bluff-catchers), creating a two-way exploit as the critic recommended — and smooth large_bet_ratio to match the signal consistency of other opponent model fields.
- **v166**: Critic evidence: H2H weaknesses: No v166 H2H data yet (fresh commit). Parent v159 H2H not available in pool (culled at 0.497)., v165 (most recent active bot) rating r=1433 — notably weaker than v162-v164 (r≈1540), suggesting recent gens are plateauing or regressing. Call-margin tightening targets a dimension that may help close the gap.; Experience pool refs: OPPONENT_MODELING: 'Opponent signals need confidence gates; ≥30g standard, archetype suppression gates may relax to ≥0.15 conf.' — v166 uses ≥0.10 conf gate (acceptable, experience-pool-mandated relaxation)., PARAMETER_TUNING: 'choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.' — v166 adds new opponent signal (large_bet_ratio) gating call-margin, so qualifies as exempt., POSTFLOP_STRATEGY: 'NEW detectors require 6 BIRTH REQUIREMENTS' — This is NOT a new detector (no new function in opponent.py, just a new field); it's a call-margin modifier using an existing data source. Birth requirements partially apply.; Diff refs: opponent.py L371-374: `large_bet_ratio = _large_hits / _large_n if _large_n > 0 else 0.32` — computes proportion of samples with ratio≥0.70. Note: NOT smoothed via smooth_rate() unlike all other model fields, uses raw ratio with only a default fallback., strategy_helpers.py L164-197: `_opponent_sizing_call_tighten()` — continuous delta = conf * (large_ratio - 0.28) * 0.12, capped [0, 0.045]. River 1.4x, large-bet 1.2x multipliers., strategy.py L1232-1235: Wired AFTER `_multi_street_calldown_tax` in the call-margin chain, BEFORE `realized_postflop_equity`. Correct placement — accumulates into EV path.
- **v165**: Archetype-gated primitives MUST respect pool confidence thresholds — v165 used conf≥0.30 against pool-mandated 0.15, risking inertness. Enforce pool-mandated params unless empirical H2H justifies tightening.
- **v165**: River dispatch ORDER: opponent-aware archetype overrides go AFTER pure-value functions (overbet, amplifier, tier), not before — don't block standard pipeline for calling-station-specific paths.
- **v164**: Archetype suppression gates on existing detectors are safer than AND-gated new detectors — 'standard' default = zero downside. Prefer this pattern over adding detectors when facing INERTNESS.
- **v163**: Axis novelty (~8th 'first new axis' gen) is NOT a success signal — treat as zero-EV until ≥100g H2H WR-lift vs named opponents. [POSSIBLY EXHAUSTED]
- **v162**: Placement fix of shadowed `_river_stackoff_guard` (L1057, BEFORE early-returns) is allowed — placement-fix ≠ new gate, does NOT trip fold-side ban. Leak PENDING ≥100g H2H confirmation.
- **v161**: 5-way AND gate + single dispatch site = high INERTNESS risk; require ≥3 dispatch sites at birth OR pre-relax one AND condition.


