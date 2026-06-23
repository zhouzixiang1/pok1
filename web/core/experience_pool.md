## OPPONENT_MODELING
- Archetype suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors — 'standard' default = zero downside; prefer over adding when facing INERTNESS.
- Archetype conf must respect pool-mandated thresholds (≥0.15) — tighter (0.30) without empirical H2H justification risks inertness. (v166)
- AND-gated opponent-aware signals with tight thresholds (5-way AND + 4.5% cap) risk inertness in 70-hand HU — verify reachability at ≥30 games before adding layers. [POSSIBLY EXHAUSTED]
- smooth_rate prior_weight must be reachable BEFORE adding detectors (prior saturated a 50%-folder below 0.50 gate).
- Raw signals without smooth_rate() (e.g. large_bet_ratio) are noisier early and diverge from every other model field — future signals should smooth or document exemption rationale.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.
- value_maximizer_index gating via formula = PARAMETER_TUNING EXEMPT.

## POSTFLOP_STRATEGY
- **FOLD-SIDE RULE:** Binary `return -1` fold gates EXHAUSTED (12+ gens) — NEVER add a new binary one. Continuous EV-integrated fold margins ARE permitted. Relocating existing guards (v162 fix) is allowed; placement-fix ≠ new gate.
- STACK-OFF GUARD PLACEMENT INVARIANT: guards inside `to_call>=my_chips` reached ZERO — relocate to `to_call>0` or opponent_allin branch.
- Dispatch-order shadow: wire offensive primitives AFTER downstream tiers (overbet, amplifier, tier). Mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- River dispatch ORDER: opponent-aware archetype overrides go AFTER pure-value functions, not before — don't block standard pipeline for calling-station-specific paths.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- strategy_helpers.py near 2500-line hard cap — future primitives MUST reuse existing dispatch sites/telemetry scaffolding.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal calling-station.
- Preflop opponent sizing-delta axis EXHAUSTED (3 gens). Do NOT add 4th preflop variant. [POSSIBLY EXHAUSTED]
- Binary fold/line-reading threshold tuning EXHAUSTED (13+ gens). Offense-side bluff axes remain live — distinguish from dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- **Firing verification:** `_PersistentBot` reads ONLY stdout — ALL stderr telemetry invisible to daemon grep. Use reachability_test (code-reachability proxy) + ≥100g H2H WR-lift, NOT telemetry grep.

## GENERAL
- **🔴 HIGHEST-ROI UNBLOCK:** Fix `battle.py` to drain stderr — unblocks ALL telemetry verification. [POSSIBLY EXHAUSTED]
- Master RELIABLE at PLAN-GENERATION — don't reflexively fall back to crossover. Crossover-as-default [POSSIBLY EXHAUSTED].
- Validation: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Do NOT reverse a prior gen's master-planned direction on sub-30g noise. Wait ≥100g daemon H2H.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains.

## RECENT_LESSONS
- **v168**: Critic evidence: H2H weaknesses: v167: Flat ~50% profile across 23 opponents, no concentrated weakness (110 games, WR=0.564, sample noise), v166: Similar flat profile (250 games, WR=0.552). No specific opponent exploit identified by H2H data; Experience pool refs: RECENT_LESSONS v167: 'River defensive margin axis is 3 generations deep (v157→v166→v167) with diminishing returns — critic prescribed pivoting to offensive arm but Master continued defense', RECENT_LESSONS v166: 'Next gen should use large_bet_ratio for offensive sizing reduction — requires smoothing large_bet_ratio first', PARAMETER_TUNING: 'Raw signals without smooth_rate() (e.g. large_bet_ratio) are noisier early and diverge from every other model field'; Diff refs: strategy_helpers.py L1073: buffer 0.10→0.03 in _river_stackoff_guard pot-odds gate, strategy.py L1251-1256: anti_lock river exclusion broadened from (made<0.55 AND bet∈{medium,large}) to (made<0.59, all bets), strategy_helpers.py L200-236: NEW _river_value_raise_reduce() — continuous delta [-0.18, 0] keyed to raw large_bet_ratio - 0.28, confidence gate 0.10
- **v167**: River defensive margin axis is 3 generations deep (v157→v166→v167) with diminishing returns — critic and experience_pool prescribed pivoting to offensive arm but Master continued defense. Future Masters should treat repeated same-axis iteration as a blocking signal.
- **v167**: Anti-lock bypass removal (L1165) is a logic fix not a constant tweak — removing dead-code guards has higher EV per line than adding margin constants.
- **v167 归档建议**: Smooth large_bet_ratio signal (reduce prior_weight 4.0→2.0 or use empirical+fallback) as v166 prescribed, then build _opponent_sizing_raise_boost() to size DOWN (0.45-0.55x pot) vs large-bet-heavy bluff-catchers — this offensive arm is completely missing after 3 defensive-only generations.
- **v167**: Critic H2H data: current sample sizes (n=10/opponent) are noise; overall ~50% flat profile across 23 opponents, no concentrated weakness. Experience_pool refs both EXHAUSTED markers and v166's offensive recommendation — v167 implements neither.
- **v166**: Next gen should use large_bet_ratio for offensive sizing reduction (smaller bets vs large-bet-heavy bluff-catchers) — additive to defensive arm, not a reversal. Requires smoothing large_bet_ratio first.
- **v166**: AND-gated opponent-aware defensive signals with 5-way conditions and 4.5% cap = high INERTNESS risk in 70-hand HU; verify reachability at ≥30 games before adding more layers. [POSSIBLY EXHAUSTED]
- **v165**: River dispatch ORDER: opponent-aware archetype overrides go AFTER pure-value functions (overbet, amplifier, tier), not before.

