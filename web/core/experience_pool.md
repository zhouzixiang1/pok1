## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors ('standard' default = zero downside); respect the ≥0.15 confidence floor for VPIP/archetype classification.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- Crossover with an older strategy.py base silently drops recent defensive/offensive work — prefer the highest-rated parent's strategy.py, not an arbitrary source_v.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the actual read site before treating the raw-warning as a live constraint (v171/v179 offensive arms route through value_maximizer_index + fold_to_bet).

## POSTFLOP_STRATEGY
- **Made-strength score table (authoritative):** pair≈0.22, two-pair≈0.40, trips≈0.58. Dominant one-pair over-calling leak lives at 0.20≤made<0.45; 0.45-0.55 band is sparse — verify band edges before committing.
- **FOLD-SIDE RULE (canonical):** binary `return -1` postflop fold gates are dead (13+ gens) [POSSIBLY EXHAUSTED]; continuous EV-integrated fold margins ARE permitted. Relocating existing guards is allowed (placement-fix ≠ new gate) but *permitted ≠ proven-effective* — river-guard relocation ran 12+ gens without lifting fold%.
- **-20k / 0%-fold stack-off leak:** two hypotheses FALSIFIED (preflop-trash axis dead; river-guard relocation didn't move fold%) — do NOT re-target those. Leak remains the highest-volume observed loss (v181, 17th gen) and an OPEN target. NOTE: v181 shipped OFFENSE (SPR value-ship), NOT fold work, so v180's turn-board-danger fold hypothesis is now UNVALIDATED and non-active — verify via reachability_test + ≥100g H2H before returning to it.
- **River call-margin tighten as NEW defensive work is exhausted** (5+ gens, v169 critic local_optima). [POSSIBLY EXHAUSTED] — if v177's `_weak_one_pair_river_margin` is touched, narrow 0.20-0.55 → 0.20-0.45 and stay river-scoped.
- **Dispatch-order shadow + birth mandate:** wire offensive primitives AFTER downstream tiers (overbet/amplifier/value-tier); archetype overrides AFTER pure-value. RECURRING BIRTH-DEFECT (v149/158/160/161/163/170/179/181 ship 1-2 sites vs ≥3 mandate) — enforce ≥3 wired dispatch sites incl. donk/probe at worker-review time, not deferred to daemon grep.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- **CAP CONSTRAINTS:** strategy.py adaptive max(2000, src×1.15) ≈ 2388; v181 at ~2361 → only ~27 lines ADAPTIVE headroom (~139 vs the 2500 hard cap is NOT the binding limit). **strategy_helpers.py at 2500/2500 = EXACT CAP — targeting helpers is IMPOSSIBLE; future work MUST target strategy.py/opponent.py or refactor-first BEFORE any growth.**

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Offense-side bluff axes remain LIVE — distinguish from the dead binary postflop-fold axis (canonical statement in POSTFLOP_STRATEGY FOLD-SIDE RULE).

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate for all practical bet sizes when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- **Firing verification:** reachability_test (code-reachability proxy) + ≥100g H2H WR-lift is the ONLY reliable gate. **stderr is NOT readable — battle.py _PersistentBot reads stdout only (v163 blindspot ACTIVE); ALL ≥30g daemon-grep "fired≥5%" steps are UNFULFILLABLE. Substitute reachability_test; do NOT block on daemon-grep.**

## GENERAL
- Master is RELIABLE at PLAN-GENERATION but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness. [advisory]
- Dead-code/guard removal > adding constants — anti-lock bypass guard removal (v167) is a logic fix with higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with `local_optima_warning=true` on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- **Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit** — when H2H shows 45-55% across the board, the correct move is a new structural axis (offense/texture/archetype), not tighter margins. [POSSIBLY EXHAUSTED]

## RECENT_LESSONS
- **v182**: Critic evidence: H2H weaknesses: v181 H2H all n=10 (noise): v153/v154/v155 0.30; v145/v159/v164/v166/v173 0.40; bulk 0.50 = plateau. No confirmed weakness at >=30g, but bulk plateau + experience_pool v181 归档 explicit dispatch-wiring mandate justifies the direction.; Experience pool refs: RECENT_LESSONS v181 归档: 'Wire the critic-suggested probe dispatch for _spr_calibrated_value_ship BEFORE _value_lead_upsizing_delta (strategy.py L2200)' — directly fulfilled by v182 L2227-2244., RECURRING BIRTH-DEFECT (v149/158/160/161/163/170/179/181 ship 1-2 sites vs >=3 mandate) — v182 raises total to 4 sites, closing the defect., POSTFLOP_STRATEGY CAP CONSTRAINTS: strategy.py adaptive cap ~2388; v182 at 2403 exceeds by 15 lines but within 2500 hard cap — headroom tight, future primitives MUST target opponent.py or refactor.; Diff refs: strategy.py L1139-1145: tier-differentiation — `if tier == 'nut': target_chips = my_chips else: target_chips = int(my_chips * 0.75)` at SPR<=2.0., strategy.py L2183-2198: NEW donk-path SPR ship dispatch, fires for round_idx in (2,3), river routes through _river_bet_commit_guard, turn returns directly., strategy.py L2229-2244: NEW probe-path SPR ship dispatch, identical structure to donk path.
- **v181:** SPR-commitment axis now OPEN as a distinct offense primitive (flat threshold override vs v178's continuous delta); future offense sizing can layer on SPR tiers 2.0/3.0/4.0 without re-deriving commitment math.
- **v181 归档:** Wire the critic-suggested probe dispatch for `_spr_calibrated_value_ship` BEFORE `_value_lead_upsizing_delta` (strategy.py L2200) so low-SPR nut hands ship instead of upsizing a fraction; verify whether the relocated `_river_stackoff_guard` (v162) actually fires ≥30g — the 0%-fold leak suggests it stays inert.
- **v181:** Critic evidence — H2H weaknesses (10g, high variance): v164 0.20, v166 0.30, v173/v176/v167 0.40; bulk 0.50 = plateau. The SPR-ship link to these losses is a hypothesis, not confirmed.
- **v181:** strategy.py ~2361/2500 (~27 ADAPTIVE headroom vs ≈2388 cap); one more ~95-line primitive approaches the cap — target opponent.py or refactor-first.
- **v180:** New DEFENSE `_turn_board_danger_margin()` (L1082): continuous fold-margin [+0.04,+0.10] turn flush/straight-danger, 5-way gate (turn r_idx==2 + facing_aggr + made[0.20,0.45) + draw<0.15 + tier∉{nut,strong} + board_danger≥0.65); FIRST turn-axis fold defense — but v181 pivoted to OFFENSE, leaving this fold hypothesis unvalidated.
- **v180 归档:** If inert, widen made-band upper 0.45→0.50 (cheaper, larger population) BEFORE relaxing board_danger 0.65→0.60; add bluffer carve-out (halve delta when opp vpip≥0.60 & aggr≥0.40, conf≥0.20) to avoid over-folding semi-bluffers on flush-draw turns.
- **v179:** `_turn_thin_value_extraction` (L1902) fires BEFORE donk/probe — add delta-returning integration or relocate dispatch AFTER them to hit ≥3-sites mandate; conf_gate<0.01 is a no-op so the real filter is station_gate≥0.15 → raise floor to ≥0.10 if reachability noise-fires.
- **v179 SEMI-DEAD READ:** `fold_to_bet_turn` read only in stderr (not gating) + unused `my_round_bet` param — workers MUST grep their own function body to confirm every param/signal actually gates an outcome.

