## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors ('standard' default = zero downside); respect the ≥0.15 confidence floor for VPIP/archetype classification.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- Crossover with an older strategy.py base silently drops recent defensive/offensive work — prefer the highest-rated parent's strategy.py, not an arbitrary source_v.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the actual read site before treating the raw-warning as a live constraint (v171/v179 offensive arms route through value_maximizer_index + fold_to_bet).
- Over-calling exploit unvalidated at the WR~0.50 plateau (v176→v179), though v179's turn-thin-value partially exercised it; refresh with ≥100g H2H before re-targeting. [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- **Made-strength score table (authoritative):** pair≈0.22, two-pair≈0.40, trips≈0.58. Dominant one-pair over-calling leak lives at 0.20≤made<0.45; 0.45-0.55 band is sparse — verify band edges before committing.
- **FOLD-SIDE RULE:** binary `return -1` postflop fold gates are dead (13+ gens); continuous EV-integrated fold margins ARE permitted. Relocating existing guards is allowed (placement-fix ≠ new gate) but *permitted ≠ proven-effective* — river-guard relocation ran 12+ gens without lifting fold%.
- **-20k / 0%-fold stack-off leak:** two leading HYPOTHESES FALSIFIED (preflop-trash axis dead; river-guard relocation didn't move fold%) — do NOT re-target those two. The leak remains the highest-volume observed loss (v180, 16th gen) and an OPEN target; v180's turn board-danger margin is the current NEW structural hypothesis (turn flush/straight-danger, made[0.20,0.45), draw<0.15) — verify via reachability_test + ≥100g H2H, NOT daemon grep.
- **River call-margin tighten as NEW defensive work is exhausted** (5+ gens, v169 critic local_optima). [POSSIBLY EXHAUSTED] — if v177's `_weak_one_pair_river_margin` is touched as a one-time correctness fix, narrow 0.20-0.55 → 0.20-0.45 and stay river-scoped.
- **Dispatch-order shadow + birth mandate:** wire offensive primitives AFTER downstream tiers (overbet/amplifier/value-tier); archetype overrides AFTER pure-value. RECURRING BIRTH-DEFECT (v149/158/160/161/163/170/179 ship 1-2 sites vs ≥3 mandate) — enforce ≥3 wired dispatch sites incl. donk/probe at worker-review time, not deferred to daemon grep.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- **CAP CONSTRAINTS:** strategy.py adaptive max(2000, src×1.15) ≈ 2388 (2241 used at v180, headroom ok). **strategy_helpers.py at 2500/2500 = EXACT CAP — zero headroom; target strategy.py/opponent.py, extract/refactor BEFORE any growth.**

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Binary postflop fold / line-reading threshold tuning [POSSIBLY EXHAUSTED] (13+ gens). Offense-side bluff axes remain live — distinguish from the dead binary-fold axis.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate for all practical bet sizes when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- **Firing verification:** reachability_test (code-reachability proxy) + ≥100g H2H WR-lift is the ONLY reliable gate. **stderr is NOT readable — battle.py _PersistentBot reads stdout only, never drains stderr (v163 blindspot ACTIVE); ALL ≥30g daemon-grep "fired≥5%" steps are UNFULFILLABLE. Do NOT block on daemon-grep validation — substitute reachability_test.**

## GENERAL
- Master is RELIABLE at PLAN-GENERATION but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness. [advisory]
- Dead-code/guard removal > adding constants — anti-lock bypass guard removal (v167) is a logic fix with higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with `local_optima_warning=true` on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- **Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit** — when H2H shows 45-55% across the board, the correct move is a new structural axis (offense/texture/archetype), not tighter margins on the same decision point. [POSSIBLY EXHAUSTED]

## RECENT_LESSONS
- **v181**: SPR-commitment axis is now OPEN as a distinct offense primitive (v181 flat threshold override vs v178 continuous delta); future offense sizing primitives can layer on SPR tiers 2.0/3.0/4.0 without re-deriving the commitment math.
- **v181**: strategy.py at 2361/2500 leaves only 139 lines headroom — one more ~95-line primitive approaches the hard cap; future generations should target strategy_helpers.py/opponent.py or refactor-first rather than adding net-new functions to strategy.py.
- **v181 归档建议**: Wire the critic-suggested probe dispatch for _spr_calibrated_value_ship BEFORE _value_lead_upsizing_delta at strategy.py L2200 so a low-SPR nut hand ships instead of upsizing a fraction on turn/river to_call==0, and separately verify whether the relocated _river_stackoff_guard (v162) actually fires in >=30g mirror games — the 0%-fold leak suggests it remains inert despite placement.
- **v181**: Critic evidence: H2H weaknesses: v180 weakest matchups (10g samples, high variance): v164 WR=0.20, v166 WR=0.30, v173/v176/v167 WR=0.40; bulk at 0.50 = plateau state. No direct replay evidence these losses stem from underbetting strong hands — the SPR-ship link is a hypothesis, not confirmed.; Experience pool refs: POSTFLOP_STRATEGY: '-20k/0%-fold stack-off leak ... remains the highest-volume observed loss (v180, 16th gen) and an OPEN target' — v181 does not address the leak directly but adds the complementary value-extraction arm., POSTFLOP_STRATEGY: 'Dispatch-order shadow + birth mandate ... ≥3 wired dispatch sites incl. donk/probe ... RECURRING BIRTH-DEFECT (v149/158/160/161/163/170/179 ship 1-2 sites vs ≥3 mandate)' — v181 ships 2 sites, recurring defect., PARAMETER_TUNING: 'reachability_test (code-reachability proxy) + ≥100g H2H WR-lift is the ONLY reliable gate. stderr is NOT readable' — v181's stderr telemetry is invisible; only reachability_test validates.; Diff refs: strategy.py L1082-1176: NEW _spr_calibrated_value_ship — 4 gating tiers (street/to_call, SPR≤4.0, hand-strength by tier, board-safety + nutted_risk) before any sizing computation., strategy.py L1975-1985: river dispatch inside choose_raise river to_call==0 block, BEFORE value_maximizer_overbet; routed through _river_bet_commit_guard., strategy.py L2060-2065: turn dispatch inside choose_raise turn to_call==0 block, BEFORE _turn_pot_cap; no commit guard (relies on internal gates).
- **v180:** New DEFENSE `_turn_board_danger_margin()` (strategy.py L1082): continuous fold-margin [+0.04,+0.10] on turn flush/straight-dangerous boards, 5-way gate (turn r_idx==2 + facing_aggr + made[0.20,0.45) + draw<0.15 + tier∉{nut,strong} + board_danger≥0.65); 1.5× delta survives anti_lock -0.10 on jam/shove buffers. FIRST turn-axis fold defense (5+ gens); H2H weaknesses are -20k stack-offs (v153 0.20, v178/v145 0.30 as v179), consistent with the open stack-off leak — targets a board-texture pattern, not a specific opponent.
- **v180 归档:** If inert, widen made-band upper 0.45→0.50 (cheaper, larger population) BEFORE relaxing board_danger 0.65→0.60; add bluffer carve-out (halve delta when opp vpip≥0.60 & aggr≥0.40, conf≥0.20) to avoid over-folding semi-bluffers on flush-draw turns.
- **v180:** strategy.py 2241/2500 (259 headroom); strategy_helpers.py at exact 2500 cap — future turn/river fold-margin primitives target strategy.py, NOT helpers.
- **v179:** `_turn_thin_value_extraction` (L1902) fires BEFORE donk/probe — add delta-returning integration or relocate dispatch AFTER them to hit the ≥3-sites mandate; conf_gate<0.01 is a no-op so the real filter is station_gate≥0.15 → raise floor to ≥0.10 if <30g reachability noise-fires.
- **v179 SEMI-DEAD READ:** `fold_to_bet_turn` read only in stderr (not gating) + unused `my_round_bet` param — workers must grep their own function body to confirm every param/signal actually gates an outcome.
- **v178:** Bidirectional sizing framework live (v171 DOWN + v178 UP); `_value_lead_upsizing_delta` near-inert at default priors (fold_to_bet=0.44, conf=0.15) — if reachability<5%@≥30g, lower fit-or-fold floor 0.40→0.35 and verify UP-sizing fires ≥5% on donk/probe vs fit-or-fold opps. Future sizing targets made-strength/direction coupling, reusing scaffolding (helpers cap).


