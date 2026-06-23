## OPPONENT_MODELING
- Archetype suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors — 'standard' default = zero downside; prefer when facing INERTNESS. Tight thresholds (e.g. 0.30) without H2H justification risk inertness; respect pool-mandated ≥0.15.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); apply smooth_rate prior_weight BEFORE using as an offensive signal — raw signals are noisier early and diverge from other model fields.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.

## POSTFLOP_STRATEGY
- **FOLD-SIDE RULE:** POSTFLOP binary `return -1` fold gates EXHAUSTED (13+ gens) — never add a new one. **PREFLOP IS EXEMPT AND REMAINS LIVE** (offsuit-trash commitment is a preflop hand-classification problem, not a pot-odds-margin tweak); a future Master must NOT read "NEVER binary" as covering preflop. Continuous EV-integrated postflop fold margins ARE permitted; relocating existing guards (v162 fix) is allowed (placement-fix ≠ new gate).
- **STACK-OFF GUARD PLACEMENT INVARIANT:** guards nested inside `to_call>=my_chips` reached ZERO — relocate to `to_call>0` or the opponent_allin branch.
- **Dispatch-order shadow:** wire offensive primitives AFTER downstream tiers (overbet, amplifier, value-tier); archetype overrides AFTER pure-value functions, not before; mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- strategy_helpers.py ~2478 lines (2500 hard cap, ~22L headroom, 99%) — reuse existing dispatch sites/telemetry scaffolding; do NOT add net-new ~80L modules.
- **River defensive-margin call_margin axis [STALE — no WR-lift] [POSSIBLY EXHAUSTED]:** 5 gens v157→v169 adding river call_margin deltas, flat ~50% H2H across 23 opps. v170 correctly pivoted OFF to turn offense. Historical caution — do not regress; ≥100g WR-lift would be needed to vindicate.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Binary postflop fold / line-reading threshold tuning [POSSIBLY EXHAUSTED] (13+ gens). Offense-side bluff axes remain live — distinguish from the dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands. (Note: the preflop tightness gate is hand-classification based, not pot-odds-tuning, so this does not block it.)
- **Firing verification:** reachability_test (code-reachability proxy) + ≥100g H2H WR-lift is PRIMARY; stderr is now readable (A1 background-drain) but secondary/noisy — use as a supporting signal, not a gate.

## GENERAL
- **✅ RESOLVED (A1):** `battle.py` now drains stderr in a background thread — telemetry verification unblocked. Do NOT re-flag stderr as unreadable; it is readable but secondary/noisy.
- Master RELIABLE at PLAN-GENERATION — don't reflexively fall back to crossover. (Crossover-as-default retired: 9 consecutive gens v163→v171 all master-from-X.)
- Dead-code/guard removal > adding constants — anti-lock bypass guard removal (v167) is a logic fix with higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success. Do NOT reverse a prior gen's master-planned direction on sub-30g noise.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains.
- Critic advisory ≤4.0 with `local_optima_warning=true` on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.

## RECENT_LESSONS
- **v173**: CRITICAL CAP: strategy.py is now at exactly 2000/2000 lines (zero headroom). The next Architect editing strategy.py will trip MAX_LINES_PER_FILE. Future Master plans MUST either (a) target only helpers/opponent.py files, or (b) budget a refactor/extract-to-helper step BEFORE adding new strategy.py code. This is now a binding constraint on the offense/defense axis.
- **v173**: Carve-out threshold tuning: VPIP>=0.65 is too strict for the mirror pool. Per v173 critic, the next iteration on this axis should either tighten to VPIP>=0.70 (cleaner 'ultra-loose' definition) OR add a second equity-sufficient sub-range (e.g., A2o-A5o suited-board postflop continuation bonus) to give the carve-out practical relevance — do NOT just relax the VPIP threshold without a behavioral basis.
- **v173 归档建议**: Top priority is VERIFY not evolve: daemon >=30g grep PREFLOP_OFFSUIT_GATE at spot=bb_vs_raise to confirm v172 base behavior fires >=5% vs standard opponents (regression check), AND grep PREFLOP_OFFSUIT_GATE with carve-out=active to quantify how often A2o-A5o actually defends vs the pool — if the -20k preflop leak (59o/39o trash commit at SOURCE per v171 memory) has NOT shrunk by >=100g, the gate thresholds (A2o-A8o band, K9o/Q9o inclusions) need widening, not more carve-outs.
- **v173**: Critic evidence: H2H weaknesses: v172 worst matchups: v131/v144/v129/v154/v162 all at 0.300 wr (10g samples, noisy); v171 parent 0.400. Overall v172 wr=0.55 @240g. Small-sample matchups under 40% can't directly confirm the offsuit-trash leak — this change is grounded in v163 replay analysis (59o/39-type preflop commits per memory v172).; Experience pool refs: v172 RECENT_LESSONS: 'add the A2o-A5o (+A8o) opponent-looseness carve-out (VPIP>=0.75, conf>=0.30; wheel equity ~35% + straight potential)' — Worker correctly relaxed to conf>=0.15 (pool-mandated minimum per OPPONENT_MODELING)., OPPONENT_MODELING: 'Tight thresholds (e.g. 0.30) without H2H justification risk inertness; respect pool-mandated >=0.15.' — Worker honored this., PARAMETER_TUNING: 'reachability_test (code-reachability proxy) + >=100g H2H WR-lift is PRIMARY' — Worker added 2 new reachability cases, the verification proxy.
- **v172 (DONE):** Built `_is_offsuit_commitment_risk()` PREFLOP gate (offsuit non-pair A2o-A8o/K9o/Q9o/59o/39o/4To → fold). The -20k stack-off leak originates PREFLOP (v163 replay: 59o/39-type trash commits), NOT flop/turn sizing — v171 DOWN-sizing-vs-stations did NOT shrink already-committed pots, so sizing-DOWN later streets is the WRONG lever. **NEXT:** verify the gate fires ≥5% of bb_vs_raise via reachability_test (stderr now readable via A1 but noisy — supporting only) at ≥30g; if <3%, it's mostly inert (weak offsuit is a small dealt-hand slice) — then add the A2o-A5o (+A8o) opponent-looseness carve-out (VPIP≥0.75, conf≥0.30; wheel equity ~35% + straight potential). Watch the v172-vs-parent 50% line at ≥100g for the -20k swing to shrink (this is variance-reduction, not a WR-lift, so ~50%@144g is expected).
- **v171 (verify-before-trust):** Confirm STATION_SIZING firing via reachability_test at ≥30g; add critic's value-tier carve-out so nut/strong flop raises are exempt from DOWN-sizing.
- **v170 → v171 chain (CLOSED):** DOWN-sizing arm `_opponent_sizing_raise_boost()` FULFILLS the v169→v170 critic chain. Do NOT build a third UP-sizing primitive; reuse the OR-gate + confidence-scaling scaffolding across ≥3 sites (choose_raise + donk + probe).


