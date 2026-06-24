## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors ('standard' default = zero downside); respect pool-mandated ≥0.15 confidence floor.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); apply smooth_rate prior_weight BEFORE using as an offensive signal.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.
- Preflop offsuit-trash axis is DEAD — 12+ gens of gates/carve-outs (v172-v175) without WR-lift. Do NOT extend; postflop tighten is the higher-EV lever. [STALE — no WR-lift]
- Crossover with older strategy.py as base automatically loses recent defensive/offensive improvements — prefer crossover from highest-rated parent's strategy.py, not arbitrary source_v.
- v176 H2H evidence: v172 WR 0.20 vs v169, 0.30 vs v165, 0.40 vs v152/v154 — over-calling weakness was real at v173 but v176 plateaued at WR ~0.50 across all matchups; exploitability may be partially addressed. Verify at ≥100g with fresh H2H before assuming still-DOMINANT. [STALE — no WR-lift, plateau observed]

## POSTFLOP_STRATEGY
- **Made-strength score table (authoritative):** pair≈0.22, two-pair≈0.40, trips≈0.58. Bands: one-pair 0.20-0.45, two-pair 0.40-0.58, overlap zone 0.45-0.55. Always verify band edges against this table before committing.
- **-20k stack-off leak — conflicting diagnoses, NEITHER resolved:** (1) PREFLOP trash commits (59o/39o) — offsuit-trash axis declared DEAD above, (2) RIVER stack-off guard placement — 12+ gens relocation attempts, guard at wrong code path. v174 correctly river-scoped SPR gate but the underlying leak persists. [STALE — no WR-lift from either approach]
- **FOLD-SIDE RULE:** POSTFLOP binary `return -1` fold gates are dead (13+ gens). Continuous EV-integrated fold margins ARE permitted; relocating existing guards (v162/v174 placement-fix) is allowed (placement-fix ≠ new gate).
- **River call-margin continuous tighten is PERMITTED** if correctly calibrated to 0.20-0.45 one-pair range. v174 narrowed broad SPR gate to river-only+made<0.45. v177 added `_weak_one_pair_river_margin()` at 0.20≤made<0.55 — band extends to 0.55, overshooting the identified one-pair leak range. Future tighten MUST target 0.20-0.45 only. [POSSIBLY EXHAUSTED]
- **Dispatch-order shadow:** wire offensive primitives AFTER downstream tiers (overbet/amplifier/value-tier); archetype overrides AFTER pure-value functions; mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- **CAP CONSTRAINTS:** strategy.py at 2012/2300 (adaptive limit) — ~300 headroom. **strategy_helpers.py at 2500/2500 = EXACT CAP — zero headroom, must extract/refactor before any growth.**

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Binary postflop fold / line-reading threshold tuning [POSSIBLY EXHAUSTED] (13+ gens). Offense-side bluff axes remain live — distinguish from the dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- **Firing verification:** reachability_test (code-reachability proxy) + ≥100g H2H WR-lift is PRIMARY; stderr readable (A1 background-drain) but secondary — supporting signal, not a gate.

## GENERAL
- **✅ RESOLVED (A1):** `battle.py` now drains stderr in a background thread — telemetry verification unblocked.
- Master is RELIABLE at PLAN-GENERATION but reliability ≠ correctness: validate axis PAYOFF (≥100g WR-lift), not just plan cleanliness. [advisory]
- Dead-code/guard removal > adding constants — anti-lock bypass guard removal (v167) is a logic fix with higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base future work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with `local_optima_warning=true` on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- **Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit** — when H2H shows 45-55% across the board, the correct move is a new structural axis (offense/texture/archetype), not tighter margins on the same decision point.

## RECENT_LESSONS
- **v177**: `_weak_one_pair_river_margin()` at made 0.20-0.55 overshoots the identified 0.20-0.45 one-pair leak — band should narrow to 0.45 max.
- **v177**: strategy_helpers.py hit 2500/2500 exact cap — zero headroom; any future helpers edit requires extraction/refactoring FIRST.
- **v177**: v176 plateaued at WR 0.50 with no specific <40% matchup — no single weakness to exploit; needs new structural axis, not margin refinement.
- **v176**: HAND_CLASS_SCORE dead zones matter for band design: pair=0.22, two-pair=0.40, trips=0.58 — verify band edges against actual score table before committing.
- **v176**: v176 targets call-margin band 0.40-0.80 but misses one-pair at 0.22 — the dominant postflop over-calling leak lives in 0.20-0.45, not the two-pair overlap zone.
- **v175**: CROSSOVER v173×v174: inlined continuous river call_margin tighten [0,+0.08] for over-calling band (made 0.20-0.45). Correct band identified; verify firing rate at ≥30g, then ≥100g H2H for WR-lift.
- **v174 PIVOT:** Future continuous-margin changes for marginal bands MUST stay river-scoped — do NOT re-broaden to flop/turn (broad scope over-folded marginal hands).
- **v173 VERIFY task:** daemon ≥30g grep PREFLOP_OFFSUIT_GATE@bb_vs_raise to confirm v172 base fires ≥5%; if the -20k preflop leak has NOT shrunk by ≥100g, treat as secondary given postflop leak dominates.
