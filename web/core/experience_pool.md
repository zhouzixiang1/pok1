## OPPONENT_MODELING
- Opponent signals need confidence gates; ≥30g standard, archetype suppression gates may relax to ≥0.15 conf when 'standard' default provides zero downside.
- Archetype suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors — 'standard' default = zero downside; prefer over adding detectors when facing INERTNESS.
- value_maximizer_index = clamp(call_down_flop_turn*0.25 + call_down_turn_river*0.35 + turn_sticky*0.20 + river_sticky*0.20); gating via it = PARAMETER_TUNING EXEMPT.
- smooth_rate prior_weight must be reachable BEFORE adding detectors (keep 4.0→2.0 adjustment in mind; prior saturated a 50%-folder below the 0.50 gate).
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.
- Raw signals without smooth_rate() (e.g. large_bet_ratio) are noisier early and diverge from every other model field — future signals should either smooth or document exemption rationale. (v166)
- AND-gated opponent-aware signals with tight thresholds (5-way AND + 4.5% cap) risk inertness in 70-hand HU mirrors — verify reachability at ≥30 games before adding layers. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- STACK-OFF GUARD PLACEMENT INVARIANT: guards inside `to_call>=my_chips` reached ZERO times — relocate to `to_call>0` or opponent_allin branch. v148 SPR gate upstream = only validated tail-containment lever.
- **FOLD-SIDE RULE:** Binary `return -1` fold gates EXHAUSTED (12+ gens) — NEVER add a new binary one. Continuous EV-integrated fold margins ARE permitted. Relocate existing guards (v162 fix) is allowed; placement-fix ≠ new gate.
- Dispatch-order shadow: wire offensive primitives AFTER downstream tiers (overbet, amplifier, tier). Mandate ≥3 wired dispatch sites incl. donk/probe paths at birth.
- River dispatch ORDER: opponent-aware archetype overrides go AFTER pure-value functions, not before — don't block standard pipeline for calling-station-specific paths. (v165)
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- strategy_helpers.py near 2500-line hard cap — future primitives MUST reuse existing dispatch sites/telemetry scaffolding.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal calling-station.
- Preflop opponent sizing-delta axis EXHAUSTED (3 gens). Do NOT add 4th preflop variant.
- Binary fold/line-reading threshold tuning EXHAUSTED (13+ gens). Offense-side bluff axes remain live — distinguish from dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Threshold-only nudges on adjacent gates = constant tuning when no new gating/opp signal added.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- **Firing verification:** `_PersistentBot` reads ONLY stdout — ALL stderr telemetry invisible to daemon grep. Use reachability_test (code-reachability proxy) + ≥100g H2H WR-lift, NOT telemetry grep.

## GENERAL
- **🔴 HIGHEST-ROI UNBLOCK:** Fix `battle.py` to drain stderr — unblocks ALL telemetry verification currently impossible. reachability_test is a workaround, not a fix.
- Master RELIABLE at PLAN-GENERATION — don't reflexively fall back to crossover. Crossover-as-default [POSSIBLY EXHAUSTED] — NEW fn + NEW opp-line signal + birth reqs = new axis.
- Validation: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Do NOT reverse a prior gen's master-planned direction on sub-30g noise. Wait ≥100g daemon H2H.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains.

## RECENT_LESSONS
- **v167**: Critic evidence: H2H weaknesses: v166 vs v145: WR=0.10 (1W/9L, n=10) — but n=10 is noise, v166 vs v151: WR=0.20 (2W/8L, n=10), v166 vs v150: WR=0.30 (3W/7L, n=10), v166 vs v160: WR=0.30 (3W/7L, n=10), Overall H2H: flat ~50% profile across 23 opponents, no concentrated weakness; Experience pool refs: PARAMETER_TUNEX [POSSIBLY EXHAUSTED]: 'choose_raise() constant-only nudges saturated ≥6 gens', POSTFLOP_STRATEGY: 'Binary return -1 fold gates EXHAUSTED (12+ gens) — NEVER add a new binary one. Continuous EV-integrated fold margins ARE permitted.', v166 RECENT_LESSONS: 'Next gen should use large_bet_ratio for offensive sizing reduction...Requires smoothing large_bet_ratio first.' — v167 does neither; Diff refs: strategy.py L1165: removed `and not anti_lock_pressure` from stackoff guard condition, strategy.py L1213-1215: `call_margin += 0.10` for river made<0.55 medium/large bet, strategy.py L1250-1252: skips `-0.07` anti-lock reduction for same river marginal hands
- **v166**: Archetype suppression-gate conf must respect pool-mandated thresholds (≥0.15) — using tighter (0.30) without empirical H2H justification risks inertness. (corrected from v165)
- **v166**: Next gen should use large_bet_ratio for offensive sizing reduction (smaller bets vs large-bet-heavy bluff-catchers), creating a two-way exploit as critic recommended — additive to v166's defensive arm, not a reversal. Requires smoothing large_bet_ratio first.
- **v166**: AND-gated opponent-aware defensive signals with 5-way conditions and 4.5% cap = high INERTNESS risk in 70-hand HU; verify reachability at ≥30 games before adding more layers. [POSSIBLY EXHAUSTED]
- **v165**: River dispatch ORDER: opponent-aware archetype overrides go AFTER pure-value functions (overbet, amplifier, tier), not before — don't block standard pipeline for calling-station-specific paths.

