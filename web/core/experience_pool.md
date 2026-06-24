## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors ('standard' default = zero downside); respect the ≥0.15 confidence floor for VPIP/archetype classification.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); apply smooth_rate prior_weight BEFORE using it as an offensive signal.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- Crossover with an older strategy.py base silently drops recent defensive/offensive work — prefer the highest-rated parent's strategy.py, not an arbitrary source_v.
- Over-calling exploit unvalidated at current pool strength (WR ~0.50 plateau v176→v178); treat as candidate, not proven; refresh with ≥100g H2H before re-targeting. [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- **Made-strength score table (authoritative):** pair≈0.22, two-pair≈0.40, trips≈0.58. Dominant one-pair over-calling leak lives at 0.20≤made<0.45; 0.45-0.55 band is sparse — verify band edges before committing.
- **FOLD-SIDE RULE:** binary `return -1` postflop fold gates are dead (13+ gens); continuous EV-integrated fold margins ARE permitted. Relocating existing guards is allowed (placement-fix ≠ new gate), but *permitted ≠ proven-effective*: river-guard relocation ran 12+ gens without lifting fold%, so treat it as allowed-but-unproven, not a recommended lever.
- **-20k stack-off leak:** BOTH leading hypotheses falsified (preflop-trash axis dead; river-guard relocation didn't move fold%). Retire as FAILED — do not re-target. [STALE — no WR-lift, 12+ gens]
- **River call-margin tighten as NEW defensive work is exhausted** (5+ gens, v169 critic flagged local_optima). [POSSIBLY EXHAUSTED] — if v177's `_weak_one_pair_river_margin` band is ever touched as a one-time correctness fix, narrow 0.20-0.55 → 0.20-0.45 (0.45-0.55 overshoots into sparse two-pair) and stay river-scoped.
- **Dispatch-order shadow:** wire offensive primitives AFTER downstream tiers (overbet/amplifier/value-tier); archetype overrides AFTER pure-value functions; mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- **CAP CONSTRAINTS:** strategy.py adaptive max(2000, src×1.15) ≈ 2300 (headroom ok). **strategy_helpers.py at 2500/2500 = EXACT CAP — zero headroom; extract/refactor BEFORE any growth.**

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Binary postflop fold / line-reading threshold tuning [POSSIBLY EXHAUSTED] (13+ gens). Offense-side bluff axes remain live — distinguish from the dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate for all practical bet sizes when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- **Firing verification:** reachability_test (code-reachability proxy) + ≥100g H2H WR-lift is PRIMARY; stderr is now readable (A1) but secondary — supporting signal, not a gate.

## GENERAL
- Master is RELIABLE at PLAN-GENERATION but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness. [advisory]
- Dead-code/guard removal > adding constants — anti-lock bypass guard removal (v167) is a logic fix with higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base future work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with `local_optima_warning=true` on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- **Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit** — when H2H shows 45-55% across the board, the correct move is a new structural axis (offense/texture/archetype), not tighter margins on the same decision point.

## RECENT_LESSONS
- **v179**: Critic evidence: H2H weaknesses: v178 parent has multiple 0.30 WR matchups (v152, v162, v165, v173 at 10g each) and many at 0.40 — consistent with under-charging thin value in close pots; v179 has no H2H data yet (brand new, pending daemon evaluation); Experience pool refs: POSTFLOP_STRATEGY: 'v178 bidirectional sizing framework live; future sizing targets the made-strength/direction coupling, not a new single-direction primitive' — v179 follows this direction, POSTFLOP_STRATEGY: 'Dispatch-order shadow: wire offensive primitives AFTER downstream tiers; mandate >=3 wired dispatch sites incl. donk/probe at birth' — v179 VIOLATES the >=3 dispatch birth requirement (only 1 site), GENERAL: 'Plateau states (WR ~0.50) have no single DOMINANT exploit — the correct move is a new structural axis (offense/texture/archetype)' — v179 is a new offense axis (turn thin-value vs standard stations); Diff refs: strategy.py L949-1020: new _turn_thin_value_extraction() — turn to_call==0, made 0.45-0.68, calling-station gate (vmi>=0.35 OR turn_call_size>=0.45), sizing 0.45-0.72x, tier!='nut' guard, strategy.py L1897-1907: single dispatch site in choose_raise turn block, AFTER value_maximizer_overbet/turn_second_barrel/missed_cbet, BEFORE thin_static_showdown_control check (L1908), opponent.py L371-387: turn_call_size_ratio and flop_call_size_ratio signals parallel to river_call_size_ratio, >=2 sample threshold, default 0.50
- **v178:** Bidirectional sizing framework live (v171 DOWN + v178 UP); future sizing targets the made-strength/direction coupling, not a new single-direction primitive — helpers.py cap forces reuse of existing scaffolding.
- **v178:** `_value_lead_upsizing_delta` near-inert at default priors (fold_to_bet=0.44, conf=0.15); if reachability <5% at ≥30g, lower the fit-or-fold floor 0.40→0.35 to capture moderately-sticky opps (ftb 0.40-0.50) before abandoning the axis.
- **v178:** Verify UP-sizing fires ≥5% on donk/probe postflop paths vs confirmed fit-or-fold opps at ≥30g; if near-inert, relax the 0.40 floor (not widen made-band) — the binding one-pair leak is at made 0.22-0.40 where UP-sizing is already active.
- **v177:** `_weak_one_pair_river_margin()` overshoots (0.20-0.55) — narrow upper bound to 0.45 (0.45-0.55 is sparse, not a true dead zone); corrective fix to existing shipped code, not a new tighten lever.
- **v176/v177 plateau:** WR ~0.50 with no <40% matchup — needs a new structural axis (offense/texture/archetype), not margin refinement. [POSSIBLY EXHAUSTED]

