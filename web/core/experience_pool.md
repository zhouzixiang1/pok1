## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors ('standard' default = zero downside); respect the ≥0.15 confidence floor for VPIP/archetype classification.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); apply smooth_rate prior_weight BEFORE using it as offensive signal.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- Crossover with an older strategy.py base silently drops recent defensive/offensive work — prefer the highest-rated parent's strategy.py, not an arbitrary source_v.
- Over-calling exploit is unvalidated at current pool strength — WR ~0.50 plateau across all matchups (v176/v177); treat as candidate, not proven. Refresh with ≥100g H2H before re-targeting. [STALE — no WR-lift, plateau observed]

## POSTFLOP_STRATEGY
- **Made-strength score table (authoritative):** pair≈0.22, two-pair≈0.40, trips≈0.58 (HAND_CLASS_SCORE + 0.008×kicker_detail). The 0.45-0.55 band is sparsely populated (gap between two-pair and trips); the dominant one-pair over-calling leak lives at 0.20≤made<0.45. Verify band edges before committing.
- **-20k stack-off leak:** BOTH hypotheses falsified (preflop-trash axis dead; river-guard relocation 12+ gens, 0% fold persists). Retire this as FAILED — do not re-target. [STALE — no WR-lift from either approach, 12+ gens]
- **FOLD-SIDE RULE:** POSTFLOP binary `return -1` fold gates are dead (13+ gens). Continuous EV-integrated fold margins ARE permitted; relocating existing guards is allowed (placement-fix ≠ new gate).
- **River call-margin continuous tighten is PERMITTED** if calibrated to the 0.20-0.45 one-pair range. v177's 0.20-0.55 band overshoots into sparse two-pair overlap; narrow to 0.20-0.45. Stay river-scoped (do NOT re-broaden to flop/turn). [POSSIBLY EXHAUSTED]
- **Dispatch-order shadow:** wire offensive primitives AFTER downstream tiers (overbet/amplifier/value-tier); archetype overrides AFTER pure-value functions; mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- **CAP CONSTRAINTS:** strategy.py adaptive max(2000, src×1.15) ≈ 2300. **strategy_helpers.py at 2500/2500 = EXACT CAP — zero headroom; must extract/refactor before ANY growth.**

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
- **✅ RESOLVED (A1):** `battle.py` now drains stderr in a background thread — telemetry verification unblocked.
- Master is RELIABLE at PLAN-GENERATION but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness. [advisory]
- Dead-code/guard removal > adding constants — anti-lock bypass guard removal (v167) is a logic fix with higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base future work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with `local_optima_warning=true` on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- **Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit** — when H2H shows 45-55% across the board, the correct move is a new structural axis (offense/texture/archetype), not tighter margins on the same decision point.

## RECENT_LESSONS
- **v178**: Bidirectional sizing framework is now live (v171 DOWN + v178 UP): future sizing work targets the made-strength/direction coupling, not a new single-direction primitive — helpers.py cap forces reuse of existing scaffolding.
- **v178**: _value_lead_upsizing_delta is near-inert at default priors (fold_to_bet=0.44, conf=0.15): if reachability <5% at ≥30g, lower the fit-or-fold floor 0.40→0.35 to capture moderately-sticky opponents (ftb 0.40-0.50) before abandoning the axis.
- **v178 归档建议**: Verify _value_lead_upsizing_delta fires ≥5% on the donk/probe postflop paths vs confirmed fit-or-fold opponents at ≥30g daemon data; if near-inert, relax the 0.40 floor to 0.35 per critic advisory rather than widening made-band, since the binding one-pair leak lives at made 0.22-0.40 where UP-sizing is already active.
- **v177:** `_weak_one_pair_river_margin()` targets 0.20-0.55 but overshoots — narrow upper bound to 0.45 (0.45-0.55 is sparse, not a true dead zone).
- **v177:** strategy_helpers.py hit 2500/2500 exact cap — next helpers edit needs extraction/refactoring FIRST; prefer targeting strategy.py/opponent.py.
- **v176:** Verify band edges against made-strength table (pair 0.22 / two-pair 0.40 / trips 0.58); v176's 0.40-0.80 band missed the one-pair leak at 0.22.
- **v176/v177 plateau WR ~0.50 with no <40% matchup** — needs a new structural axis (offense/texture/archetype), not margin refinement. [POSSIBLY EXHAUSTED]

