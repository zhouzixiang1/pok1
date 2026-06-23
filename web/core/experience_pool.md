## OPPONENT_MODELING
- Archetype suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors — 'standard' default = zero downside; prefer when facing INERTNESS.
- Archetype conf must respect pool-mandated thresholds (≥0.15); tighter (e.g. 0.30) without H2H justification risks inertness. (v166)
- AND-gated opponent-aware signals with tight thresholds (5-way AND + 4.5% cap) risk inertness in 70-hand HU — verify reachability at ≥30g before adding layers. [POSSIBLY EXHAUSTED]
- `large_bet_ratio` is RAW (no smooth_rate wrapper in code); apply smooth_rate prior_weight BEFORE using as an offensive signal — raw signals are noisier early and diverge from other model fields.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.

## POSTFLOP_STRATEGY
- **FOLD-SIDE RULE:** Binary `return -1` fold gates EXHAUSTED (12+ gens) — NEVER add a new binary one. Continuous EV-integrated fold margins ARE permitted. Relocating existing guards (v162 fix) is allowed; placement-fix ≠ new gate.
- **STACK-OFF GUARD PLACEMENT INVARIANT:** guards inside `to_call>=my_chips` reached ZERO — relocate to `to_call>0` or opponent_allin branch.
- **Dispatch-order shadow:** wire offensive primitives AFTER downstream tiers (overbet, amplifier, tier). Mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- **River dispatch ORDER:** opponent-aware archetype overrides go AFTER pure-value functions, not before — don't block the standard pipeline for calling-station-specific paths.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- strategy_helpers.py at 2409 lines (2500 hard cap, ~91 lines headroom, 96%) — reuse existing dispatch sites/telemetry scaffolding; do NOT add net-new ~80L modules.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Binary fold / line-reading threshold tuning EXHAUSTED (13+ gens). Offense-side bluff axes remain live — distinguish from the dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- **Firing verification:** `_PersistentBot` now drains stderr in a background thread (A1 fix landed) — prefer reachability_test (code-reachability proxy) + ≥100g H2H WR-lift as primary; stderr grep as secondary.

## GENERAL
- **✅ RESOLVED (A1):** `battle.py` now drains stderr in a background thread — telemetry verification unblocked. Kept as dogfood trail; do NOT re-flag stderr as unreadable.
- Master RELIABLE at PLAN-GENERATION — don't reflexively fall back to crossover. Crossover-as-default [POSSIBLY EXHAUSTED].
- Dead-code/guard removal > adding constants — anti-lock bypass guard removal (v167) is a logic fix with higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success. Do NOT reverse a prior gen's master-planned direction on sub-30g noise.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains.

## RECENT_LESSONS
- **v171**: v171 DOWN-sizing-vs-stations did NOT reduce stack-off magnitude in 16g precommit — the -20k leak originates preflop (per v163 memory: G5H68 59o, G10H39 stack-off, G7H67 AQ made≥0.55 exempt), so shrinking flop/turn bet ratios cannot retroactively shrink pots already committed preflop. Sizing-DOWN on later streets is the wrong lever for this leak; preflop tightness or river fold-side is the right one.
- **v171 归档建议 (mixed)**: Before trusting v171's offense, verify STATION_SIZING telemetry fires ≥5% at ≥30g (vmi 0.40 / fold_to_bet 0.40 gates may be inert), and add the critic's value-tier carve-out so nut/strong flop raises are exempt from DOWN-sizing — but recognize the persistent -20k losses trace to preflop commitment (59o/39-type hands per v163 replay G5H68/G10H39), not flop/turn sizing, so a preflop-tightness gate on offsuit trash is the higher-EV next move than further DOWN-sizing refinements.
- **v171**: Critic evidence: H2H weaknesses: v170 H2H: loses to v166 (0.3), v169 (0.4), v157 (0.4) at 10g each; aggregate plateau ~0.50 across 23 matchups — structural exploration warranted per plateau rule; Experience pool refs: RECENT_LESSONS v170: 'Next gen MUST build the complementary DOWN-sizing arm _opponent_sizing_raise_boost()... do NOT build a third UP-sizing primitive', POSTFLOP_STRATEGY: 'NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs' — satisfied, OPPONENT_MODELING: 'AND-gated opponent-aware signals with tight thresholds risk inertness' — OR-gate addresses this; Diff refs: strategy_helpers.py L1104-1153: new _opponent_sizing_raise_boost(), OR-gate max(vmi_factor, fold_factor), continuous delta -0.12*station_factor, clamp[-0.12,-0.01], confidence<0.15 scaling, river excluded (round_idx in 1,2), strategy.py L1757-1759: donk dispatch site (ratio + _donk_fold_boost + _donk_station_delta), strategy.py L1776-1783: probe dispatch site (ratio + _probe_fold_boost + _tp_delta_probe + _probe_station_delta)
- **v170 (critic re-mandate, 2nd consecutive miss):** Next gen MUST build the complementary DOWN-sizing arm `_opponent_sizing_raise_boost()` — size DOWN to 0.45-0.55x pot on turn/river to_call==0 vs large-bet-heavy bluff-catchers, using `opp_reraise_ratio` LOW (≤0.5 = calling-station, already LIVE from v169). v169 critic ordered it; v170 built `_turn_probe_sizing()` (size UP vs capped ranges) instead, which critic explicitly calls COMPLEMENTARY not a substitute — do NOT build a third UP-sizing primitive. Reuse `_turn_probe_sizing()`'s OR-gate + confidence-scaling scaffolding; wire ≥3 sites (choose_raise + donk L1599 + probe L1617) at birth. Plateau context warrants structural exploration: 23 H2H matchups sit 0.40-0.60 at 10g each, v169 aggregate 0.529 @240g.
- **v170:** strategy_helpers.py at 2409/2500 (~91 lines headroom) — next offense primitive must reuse scaffolding/extract-refactor, NOT add net-new ~80L modules.
- **v169 (process):** Critic advisory ≤4.0 with `local_optima_warning=true` on an exhausted axis should trigger mandatory direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- **River defensive-margin axis [STALE — no WR-lift] [POSSIBLY EXHAUSTED]:** 5 gens v157→v169 adding river call_margin deltas, flat ~50% H2H across 23 opps. v170 correctly pivoted OFF to turn offense. Historical caution — do not regress; ≥100g WR-lift would be needed to vindicate.


