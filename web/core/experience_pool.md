## OPPONENT_MODELING
- Archetype suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors — 'standard' default = zero downside; prefer over adding when facing INERTNESS.
- Archetype conf must respect pool-mandated thresholds (≥0.15) — tighter (e.g. 0.30) without empirical H2H justification risks inertness. (v166)
- AND-gated opponent-aware signals with tight thresholds (5-way AND + 4.5% cap) risk inertness in 70-hand HU — verify reachability at ≥30 games before adding layers. [POSSIBLY EXHAUSTED]
- smooth_rate prior_weight must be reachable BEFORE adding detectors (prior saturated a 50%-folder below the 0.50 gate); raw signals (e.g. large_bet_ratio) are noisier early and diverge from every other model field — smooth or document exemption rationale BEFORE use.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.
- value_maximizer_index gating via formula = PARAMETER_TUNING EXEMPT.

## POSTFLOP_STRATEGY
- **FOLD-SIDE RULE:** Binary `return -1` fold gates EXHAUSTED (12+ gens) — NEVER add a new binary one. Continuous EV-integrated fold margins ARE permitted. Relocating existing guards (v162 fix) is allowed; placement-fix ≠ new gate.
- **STACK-OFF GUARD PLACEMENT INVARIANT:** guards inside `to_call>=my_chips` reached ZERO — relocate to `to_call>0` or opponent_allin branch.
- **Dispatch-order shadow:** wire offensive primitives AFTER downstream tiers (overbet, amplifier, tier). Mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- **River dispatch ORDER:** opponent-aware archetype overrides go AFTER pure-value functions, not before — don't block standard pipeline for calling-station-specific paths.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- strategy_helpers.py near the 2500-line hard cap — future primitives MUST reuse existing dispatch sites/telemetry scaffolding.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Binary fold / line-reading threshold tuning EXHAUSTED (13+ gens). Offense-side bluff axes remain live — distinguish from the dead binary-fold axis.
- Preflop opponent sizing-delta axis historically EXHAUSTED (v146, ~22 gens ago) — meta has shifted to river/postflop; historical context only, not a binding constraint. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- **Firing verification:** `_PersistentBot` now drains stderr in a background thread (A1 fix landed) — stderr telemetry IS visible to daemon grep. Prefer reachability_test (code-reachability proxy) + ≥100g H2H WR-lift as primary signal; stderr grep as secondary confirmation.

## GENERAL
- **✅ RESOLVED (A1):** `battle.py` now drains stderr in a background thread — telemetry verification unblocked. Kept as a dogfood trail; do NOT re-flag stderr as unreadable.
- Master RELIABLE at PLAN-GENERATION — don't reflexively fall back to crossover. Crossover-as-default [POSSIBLY EXHAUSTED] — NEW fn + NEW opp-line signal + birth reqs = new axis.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success. Do NOT reverse a prior gen's master-planned direction on sub-30g noise — wait ≥100g daemon H2H.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains.

## RECENT_LESSONS
- **v169**: Critic evidence: H2H weaknesses: v167 only sub-0.40 matchup is v155 at 0.30 (10 games = noise); all other matchups 0.40-0.70. No concentrated weakness pattern. v169 has 0 rated games. Experience pool: '~50% flat H2H profile across 23 opps (n=10/opp = noise, no concentrated weakness).'; Experience pool refs: POSTFLOP_STRATEGY: 'NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + >=3 wired dispatch sites + ...' — v169 has 1 dispatch site, violating this., RECENT_LESSONS: 'River defensive-margin axis [STALE - no WR-lift]: now 4 gens deep (v157->v166->v167->v168)' — v169 makes it 5., RECENT_LESSONS: 'v166/v167: Offensive arm still missing — build _opponent_sizing_raise_boost() ... requires smoothing large_bet_ratio first.' — ignored for 3rd gen.; Diff refs: strategy_helpers.py L201-274: _river_reraise_tighten() — 7-way AND gate (river + facing_aggression + made 0.55-0.80 + tier!=nut + board_risk + bot_bet>BB + ratio>=2.0), strategy.py L1219-1224: single dispatch site in to_call>0 block call_margin accumulation — same exhausted decision point as v157/v166/v167/v168, strategy.py L1332: 'if realized_rate < pot_odds + call_margin: return -1' — higher call_margin = more folds, adding to the river over-fold tendency
- **v168 = MANUAL batch-fix commit (874dcc7), NOT a pipeline generation — 0 rated games.** Any v166/v167 H2H numbers cited are PARENT data, not v168's. Changes: (1) _river_stackoff_guard buffer 0.10→0.03 (helpers L1073); (2) anti-lock river exclusion broadened made<0.55+medium/large → made<0.59+all bets (strategy L1251-1256); (3) NEW _river_value_raise_reduce() (helpers L200-236): continuous delta [-0.18,0] keyed to RAW large_bet_ratio-0.28, conf 0.10.
- **v168 skipped v167's prerequisite:** v167 archived "smooth large_bet_ratio FIRST (prior_weight 4.0→2.0) THEN build offensive arm"; v168 keyed _river_value_raise_reduce() to RAW large_bet_ratio with no smoothing/exemption-doc — violates the OPPONENT_MODELING smooth-or-document rule. NEXT: smooth the input, or swap to empirical+fallback.
- **v167:** Dead-code removal > adding constants — anti-lock bypass guard removal (L1165) is a logic fix with higher EV per line than margin tweaks.
- **v166/v167:** Offensive arm still missing — build _opponent_sizing_raise_boost() (size DOWN 0.45-0.55x vs large-bet-heavy bluff-catchers), additive to defense NOT a reversal; requires smoothing large_bet_ratio first.
- **River defensive-margin axis [STALE — no WR-lift]:** now 4 gens deep (v157→v166→v167→v168); v167 declared it a BLOCKING signal with diminishing returns, yet v168 continued the same axis. ~50% flat H2H profile across 23 opps (n=10/opp = noise, no concentrated weakness). ≥100g WR-lift required to vindicate; otherwise pivot to the unbuilt offensive arm above. [POSSIBLY EXHAUSTED]

