## OPPONENT_MODELING
- Archetype suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors — 'standard' default = zero downside; prefer over adding when facing INERTNESS.
- Archetype conf must respect pool-mandated thresholds (≥0.15) — tighter (e.g. 0.30) without empirical H2H justification risks inertness. (v166)
- AND-gated opponent-aware signals with tight thresholds (5-way AND + 4.5% cap) risk inertness in 70-hand HU — verify reachability at ≥30 games before adding layers. [POSSIBLY EXHAUSTED]
- large_bet_ratio is RAW (no smooth_rate wrapper exists in code); smooth_rate prior_weight must be applied BEFORE using it as an offensive signal. Raw signals are noisier early and diverge from every other model field.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.

## POSTFLOP_STRATEGY
- **FOLD-SIDE RULE:** Binary `return -1` fold gates EXHAUSTED (12+ gens) — NEVER add a new binary one. Continuous EV-integrated fold margins ARE permitted. Relocating existing guards (v162 fix) is allowed; placement-fix ≠ new gate.
- **STACK-OFF GUARD PLACEMENT INVARIANT:** guards inside `to_call>=my_chips` reached ZERO — relocate to `to_call>0` or opponent_allin branch.
- **Dispatch-order shadow:** wire offensive primitives AFTER downstream tiers (overbet, amplifier, tier). Mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- **River dispatch ORDER:** opponent-aware archetype overrides go AFTER pure-value functions, not before — don't block standard pipeline for calling-station-specific paths.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- strategy_helpers.py at 2289 lines (2500 hard cap, 211 lines headroom) — reuse existing dispatch sites/telemetry scaffolding where possible.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Binary fold / line-reading threshold tuning EXHAUSTED (13+ gens). Offense-side bluff axes remain live — distinguish from the dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- **Firing verification:** `_PersistentBot` now drains stderr in a background thread (A1 fix landed) — prefer reachability_test (code-reachability proxy) + ≥100g H2H WR-lift as primary signal; stderr grep as secondary confirmation.

## GENERAL
- **✅ RESOLVED (A1):** `battle.py` now drains stderr in a background thread — telemetry verification unblocked. Kept as a dogfood trail; do NOT re-flag stderr as unreadable.
- Master RELIABLE at PLAN-GENERATION — don't reflexively fall back to crossover. Crossover-as-default [POSSIBLY EXHAUSTED].
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success. Do NOT reverse a prior gen's master-planned direction on sub-30g noise.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains.

## RECENT_LESSONS
- **v170**: Critic evidence: H2H weaknesses: v169 aggregate 0.529 (240g). All 23 H2H matchups at 0.40-0.60 plateau with only 10g each — no specific opponent <40% at meaningful sample. Plateau context = structural exploration warranted.; Experience pool refs: v169 mandate: 'PIVOT OFFENSE — build _opponent_sizing_raise_boost() sizing DOWN 0.45-0.55x pot vs large-bet-heavy bluff-catchers, reuse opp_reraise_ratio (LOW=calling station)'. v170 pivots offense but uses a DIFFERENT axis (turn probe UP vs weak/capped) instead of sizing DOWN vs bluff-catchers. The specific _opponent_sizing_raise_boost remains unbuilt., River defensive-margin axis [STALE, POSSIBLY EXHAUSTED]: 5 gens v157→v169, flat ~50% H2H. v170 correctly pivots OFF this axis to turn offense.; Diff refs: strategy_helpers.py: _turn_probe_sizing() new function (~95L, L1008-1103). Signals: opp_checked_this_street (LIVE, direct action) OR flop_aggr < 0.42 (smoothed prior_weight 5.0 — sticky at low samples, but OR-gated with live signal). Continuous delta [0.10, 0.22] scaled by made_strength and board wetness., strategy.py L1734: probe sizing added to barrel-abandon late dispatch (turn only, returns 0 on river)., strategy.py L1776: probe sizing added to probe bet dispatch.
- **v169**: 5 consecutive gens (v157→v169) adding river defensive call_margin deltas with flat WR 0.48–0.54 — axis [STALE — no WR-lift] [POSSIBLY EXHAUSTED]; next gen MUST pivot to offensive arm as 3-generation-standing critic mandate.
- **v169 归档建议**: Pivot offense: build `_opponent_sizing_raise_boost()` sizing DOWN 0.45–0.55x pot vs large-bet-heavy bluff-catchers, reuse v169's `opp_reraise_ratio` signal (LOW = calling station), wired ≥3 dispatch sites (choose_raise + donk L1599 + probe L1617) at birth.
- **v169**: Critic advisory score ≤4.0 with `local_optima_warning=true` on exhausted axis should trigger mandatory direction_audit pivot — advisory doesn't gate commit (precommit is authoritative) but mandates pivot enforcement.
- **v168 batch-fix**: (1) stack-off guard buffer 0.10→0.03; (2) anti-lock river exclusion broadened; (3) NEW `_river_value_raise_reduce()` keyed to RAW large_bet_ratio-0.28. **⚠ violates smoothing rule** — large_bet_ratio is raw, no smooth_rate wrapper; smooth it or swap to empirical+fallback before building offense.
- **v167**: Dead-code removal > adding constants — anti-lock bypass guard removal is a logic fix with higher EV per line than margin tweaks.
- **River defensive-margin axis [STALE — no WR-lift]:** 5 gens deep (v157→v169), flat ~50% H2H profile across 23 opps. ≥100g WR-lift required to vindicate; otherwise the unbuilt offensive arm above takes priority. [POSSIBLY EXHAUSTED]

