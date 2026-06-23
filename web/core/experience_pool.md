## OPPONENT_MODELING
- Archetype suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors — 'standard' default = zero downside; prefer when facing INERTNESS. Tight thresholds (e.g. 0.30) without H2H justification risk inertness; respect pool-mandated ≥0.15.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); apply smooth_rate prior_weight BEFORE using as offensive signal — raw signals are noisier early and diverge from other model fields.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.

## POSTFLOP_STRATEGY
- **FOLD-SIDE RULE:** POSTFLOP binary `return -1` fold gates EXHAUSTED (13+ gens) — never add a new POSTFLOP binary one. **PREFLOP IS EXEMPT AND REMAINS LIVE** (offsuit-trash commitment is a preflop hand-classification problem, not a pot-odds-margin tweak) — a future Master must NOT read "NEVER binary" as covering preflop. Continuous EV-integrated postflop fold margins ARE permitted; relocating existing guards (v162 fix) is allowed (placement-fix ≠ new gate).
- **STACK-OFF GUARD PLACEMENT INVARIANT:** guards nested inside `to_call>=my_chips` reached ZERO — relocate to `to_call>0` or the opponent_allin branch.
- **Dispatch-order shadow:** wire offensive primitives AFTER downstream tiers (overbet, amplifier, value-tier); mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- **River dispatch ORDER:** opponent-aware archetype overrides go AFTER pure-value functions, not before — don't block the standard pipeline for calling-station paths.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- strategy_helpers.py at ~2478 lines (2500 hard cap, ~22L headroom, 99%) — reuse existing dispatch sites/telemetry scaffolding; do NOT add net-new ~80L modules.
- **River defensive-margin call_margin axis [STALE — no WR-lift] [POSSIBLY EXHAUSTED]:** 5 gens v157→v169 adding river call_margin deltas, flat ~50% H2H across 23 opps. v170 correctly pivoted OFF to turn offense. Historical caution — do not regress; ≥100g WR-lift would be needed to vindicate.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Binary postflop fold / line-reading threshold tuning EXHAUSTED (13+ gens). Offense-side bluff axes remain live — distinguish from the dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands. (Note: the proposed preflop tightness gate is hand-classification based, not pot-odds-tuning, so this does not block it.)
- **Firing verification:** reachability_test (code-reachability proxy) + ≥100g H2H WR-lift is PRIMARY; stderr grep is secondary/noisy (stdout-only _PersistentBot limits reliability even after A1 background-drain).

## GENERAL
- **✅ RESOLVED (A1):** `battle.py` now drains stderr in a background thread — telemetry verification unblocked. Do NOT re-flag stderr as unreadable.
- Master RELIABLE at PLAN-GENERATION — don't reflexively fall back to crossover. (Crossover-as-default concern retired: 9 consecutive gens v163→v171 were all master-from-X.)
- Dead-code/guard removal > adding constants — anti-lock bypass guard removal (v167) is a logic fix with higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success. Do NOT reverse a prior gen's master-planned direction on sub-30g noise.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains.

## RECENT_LESSONS
- **v172**: UNCONDITIONAL offsuit gate lacks an opponent-looseness carve-out: vs ultra-loose openers (VPIP>=0.75, conf>=0.30), wheel aces A2o-A5o have ~35% equity + straight-wheel potential — future Master should allow A2o-A5o (+A8o) to defend vs raise only when opp looseness is high, else the gate over-folds exploitable hands.
- **v172 归档建议**: Verify via reachability_test (NOT stderr grep — _PersistentBot is stdout-only) at >=30g that PREFLOP_OFFSUIT_GATE fires at >=5% of bb_vs_raise spots; if <3%, the gate is mostly inert (weak offsuit is a small dealt-hand slice) — then add the A2o-A5o opponent-looseness carve-out and watch the v172-vs-parent 50% line at >=100g for the -20k stack-off swing to shrink.
- **v172 → NEXT GEN (HIGHEST PRIORITY):** The -20k stack-off leak originates PREFLOP (v163 replay: G5H68 59o, G10H39 39-type trash, G7H67 AQ made≥0.55 exempt), NOT flop/turn sizing — v171 DOWN-sizing-vs-stations did NOT shrink already-committed pots in 16g precommit. Sizing-DOWN later streets is the WRONG lever. Build a PREFLOP TIGHTNESS GATE on offsuit trash: `_is_offsuit_commitment_risk()` via preflop_hand_profile discrete fields (high/low/suited/pair, since estimate_preflop_strength saturates A2o=0.75/A9o=1.0); bb_vs_raise fold BEFORE _preflop_steal_defense_widen; sb_open weak-offsuit raise→call (limp). **This is PREFLOP — exempt from the postflop binary-fold ban AND dir-audit.**
- **v171 (verify-before-trust):** Confirm STATION_SIZING firing via reachability_test (NOT stderr grep) at ≥30g; add critic's value-tier carve-out so nut/strong flop raises are exempt from DOWN-sizing.
- **v170 → v171 chain (CLOSED):** DOWN-sizing arm `_opponent_sizing_raise_boost()` FULFILLS the v169→v170 critic chain. Do NOT build a third UP-sizing primitive; reuse OR-gate + confidence-scaling scaffolding across ≥3 sites (choose_raise + donk + probe).
- **v170 (infra):** strategy_helpers.py ~2478/2500 (~22L headroom) — next offense primitive MUST reuse scaffolding/extract-refactor, not add net-new modules.
- **v169 (process):** Critic advisory ≤4.0 with `local_optima_warning=true` on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.

