## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors are structurally safer than AND-gated new detectors ('standard' default = zero downside); respect the ≥0.15 confidence floor for VPIP/archetype classification.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- Crossover with an older strategy.py base silently drops recent defensive/offensive work — prefer the highest-rated parent's strategy.py, not an arbitrary source_v.
- `large_bet_ratio` is RAW (no smooth_rate wrapper), but v171/v179 offensive arms route through value_maximizer_index + fold_to_bet and may not consume it raw — verify the actual read site before treating the raw-warning as a live constraint.
- Over-calling exploit unvalidated at the WR~0.50 plateau (v176→v179); treat as a candidate, not proven; refresh with ≥100g H2H before re-targeting. [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- **Made-strength score table (authoritative):** pair≈0.22, two-pair≈0.40, trips≈0.58. Dominant one-pair over-calling leak lives at 0.20≤made<0.45; 0.45-0.55 band is sparse — verify band edges before committing.
- **FOLD-SIDE RULE:** binary `return -1` postflop fold gates are dead (13+ gens); continuous EV-integrated fold margins ARE permitted. Relocating existing guards is allowed (placement-fix ≠ new gate) but *permitted ≠ proven-effective* — river-guard relocation ran 12+ gens without lifting fold%.
- **-20k / 0%-fold stack-off leak:** the two leading HYPOTHESES are FALSIFIED (preflop-trash axis dead; river-guard relocation didn't move fold%) — do NOT re-target those two. The leak ITSELF is still the highest-volume observed loss (v179, 15th gen) and remains an OPEN target needing a NEW structural hypothesis, not the two already disproven.
- **River call-margin tighten as NEW defensive work is exhausted** (5+ gens, v169 critic local_optima). [POSSIBLY EXHAUSTED] — if v177's `_weak_one_pair_river_margin` is touched as a one-time correctness fix, narrow 0.20-0.55 → 0.20-0.45 and stay river-scoped.
- **Dispatch-order shadow:** wire offensive primitives AFTER downstream tiers (overbet/amplifier/value-tier); archetype overrides AFTER pure-value; mandate ≥3 wired dispatch sites incl. donk/probe at birth.
- NEW detectors require 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- **CAP CONSTRAINTS:** strategy.py adaptive max(2000, src×1.15) ≈ 2388 at v179 (2160 used, headroom ok). **strategy_helpers.py at 2500/2500 = EXACT CAP — zero headroom; extract/refactor BEFORE any growth.**

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
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with `local_optima_warning=true` on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- **Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit** — when H2H shows 45-55% across the board, the correct move is a new structural axis (offense/texture/archetype), not tighter margins on the same decision point.

## RECENT_LESSONS
- **v180**: CRITICAL: critic follow-up #1 (daemon grep TURN_BOARD_DANGER_MARGIN reason=fired ≥5% @≥30g) is UNFULFILLABLE — battle.py _PersistentBot reads stdout only, never stderr (v163 blindspot confirmed active); all stderr telemetry is invisible to daemon grep, so reachability_test.py is the ONLY firing-rate proxy. A future Master that blocks on this daemon-grep validation will spin indefinitely — treat any critic/daemon-grep validation step as unreachable and substitute reachability_test.
- **v180**: strategy.py is now 2241/2500 (259 lines headroom); strategy_helpers.py remains at exact 2500 cap. Future turn/river-scoped fold-margin primitives should target strategy.py (like v180 did), NOT strategy_helpers.py.
- **v180 归档建议**: Before relaxing v180's board_danger 0.65→0.60 threshold per critic item 1, note daemon-grep validation is structurally impossible (battle.py stdout-only); use reachability_test as proxy — if firing is inert, widen made-band upper 0.45→0.50 (cheaper, larger population) and add the critic's bluffer carve-out (halve delta when opp_model vpip≥0.60 AND aggression_freq≥0.40 with conf≥0.20) to prevent over-folding to semi-bluffers on flush-draw turn boards vs aggro archetypes.
- **v180**: Critic evidence: H2H weaknesses: v179 weakest matchups (win_rate as v179): claude_v153 0.20 (2W-8L), claude_v178 0.30, claude_v145 0.30 — all consistent with -20k stack-off leaks losing entire stacks. v179 overall WR 0.5174 (230g) on a ~0.50 plateau with no <40% outlier except the -20k swing hands., No single opponent matchup cited by Master — the change targets a board-texture pattern (flush/straight-dangerous turn) rather than a specific opponent. This is appropriate for a texture-driven defense but limits per-opponent measurability.; Experience pool refs: experience_pool L11: '-20k / 0%-fold stack-off leak: the two leading HYPOTHESES are FALSIFIED... remains an OPEN target needing a NEW structural hypothesis, not the two already disproven.' v180 provides that new hypothesis (turn-street board-danger)., experience_pool L12: 'River call-margin tighten as NEW defensive work is exhausted [POSSIBLY EXHAUSTED].' v180 is TURN-scoped, exempt from this exhaustion., experience_pool L8: 'pair≈0.22, two-pair≈0.40, trips≈0.58. Dominant one-pair over-calling leak lives at 0.20≤made<0.45; 0.45-0.55 band is sparse.' v180 uses exactly [0.20, 0.45) band — correct application of this lesson.; Diff refs: strategy.py L1082-1141: NEW _turn_board_danger_margin() — continuous delta [0.04, 0.10] with 5-way gate (turn + facing_aggr + made[0.20,0.45) + draw<0.15 + tier∉{nut,strong} + board_danger≥0.65). EV scaling: 0.10 × made_weakness × danger_factor × pot_odds_factor., strategy.py L1326: jam_buffer += 1.5 × delta (1.5x multiplier survives anti_lock's -0.10 reduction → net +0.05 tighter). Correct EV reasoning for commitment decisions., strategy.py L1400: shove_buffer += 1.5 × delta (parallel to jam_buffer for stack-covering calls).
- **v179 RECURRING BIRTH-DEFECT:** Nth generation (v149/v158/v160/v161/v163/v170/v179) shipped with only 1-2 dispatch sites vs the ≥3-sites mandate. Future Master must enforce ≥3 dispatch sites at worker-review time, not defer to daemon grep — the pattern recurs because workers add the primary dispatch then stop.
- **v179:** `_turn_thin_value_extraction` (strategy.py L1902) fires BEFORE donk (L1962)/probe (L1982) for turn to_call==0 — add delta-returning integration at those sites (or relocate dispatch AFTER them) to satisfy the ≥3-sites mandate and avoid shadow; the conf_gate<0.01 floor is a no-op so the real filter is station_gate≥0.15 → raise conf floor to ≥0.10 if reachability shows noise-firing at <30g.
- **v179 SEMI-DEAD READ:** reviewer flagged `fold_to_bet_turn` read from opponent_model but only used in stderr (not gating) + unused `my_round_bet` param. Workers should grep their own function body for every param/signal they read to confirm it actually gates an outcome.
- **v178:** Bidirectional sizing framework live (v171 DOWN + v178 UP); future sizing targets the made-strength/direction coupling, not a new single-direction primitive — helpers.py cap forces reuse of existing scaffolding.
- **v178:** `_value_lead_upsizing_delta` near-inert at default priors (fold_to_bet=0.44, conf=0.15); if reachability <5% at ≥30g, lower the fit-or-fold floor 0.40→0.35 and verify UP-sizing fires ≥5% on donk/probe paths vs confirmed fit-or-fold opps.
- **v177:** `_weak_one_pair_river_margin()` overshoots (0.20-0.55) — narrow upper bound to 0.45 (0.45-0.55 is sparse, not a true dead zone); corrective fix to existing shipped code, not a new tighten lever.
- **v177 plateau:** WR ~0.50 with no <40% matchup — needs a new structural axis (offense/texture/archetype), not margin refinement. [POSSIBLY EXHAUSTED]


