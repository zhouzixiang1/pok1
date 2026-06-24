## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors > AND-gated new detectors ('standard' default = zero downside); respect ≥0.15 confidence floor for VPIP/archetype classification.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- Crossover silently drops recent strategy.py work — prefer the highest-rated parent's strategy.py, not an arbitrary source_v.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the read site before treating the raw-warning as live (v171/v179 route through value_maximizer_index + fold_to_bet).
- Archetype-gated folds are only load-bearing if the target adversary actually classifies into that bucket — capture a real `classify_archetype()` snapshot at ≥30 hands before building another; a fold on a 'standard' opponent never fires regardless of made/ev thresholds.

## POSTFLOP_STRATEGY
- **Made-strength table (authoritative):** pair≈0.22, two-pair≈0.40, trips≈0.58. Over-calling leak at 0.20≤made<0.45; 0.45-0.55 sparse — verify band edges before committing.
- **FOLD-SIDE RULE (v183-corrected):** postflop binary `return -1` folds were dead 13+ gens; v183 PARTIALLY reactivated the axis via archetype+EV gating (`_aggro_bluffcatcher_should_fold`, strategy.py L1546). Bare binary postflop folds now require archetype+EV gating AND remain PARTIAL (tight-exploiters like v173 untouched, lag_maniac carve-out added in v184); continuous fold margins remain the safer default. PREFLOP offsuit-gate MECHANISM is live (v172/v173), but its WR-lift on the -20k leak was falsified — distinguish "gate works" from "fixes the leak". [POSSIBLY EXHAUSTED]
- **Offense value-sizing UP on strategy.py to_call==0 block** (value-lead upsizing / turn thin-value / SPR value-ship / SPR dispatch coverage) — 4 of last 6 gens, family saturated, no WR lift (~0.48-0.52 h2h_avg flat); no room for another primitive on strategy.py. [POSSIBLY EXHAUSTED]
- **Dispatch-order shadow + birth mandate:** wire offensive primitives AFTER downstream tiers; the RECURRING birth-defect (ship 1-2 sites vs ≥3 mandate) is CLOSABLE (v182 SPR-ship 2→4 sites). Enforce ≥3 wired dispatch sites incl. donk/probe at worker-review time, not deferred to daemon grep.
- **NEW detectors require 6 BIRTH REQUIREMENTS:** new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- **CAP CONSTRAINTS (v184):** strategy.py ~2423/2500 (~3% headroom); adaptive cap passed quality_gates, so the "exceeds ~2388" framing was a v174-class miscalibration false-alarm. strategy_helpers.py at 2500/2500 = EXACT CAP — targeting helpers IMPOSSIBLE; future work MUST target opponent.py OR refactor-first (share scaffolding between _value_lead_upsizing_delta / _spr_calibrated_value_ship) BEFORE any strategy.py growth.
- **-20k / 0%-fold stack-off leak:** highest-volume observed loss (17th gen), OPEN target. v183 shipped archetype-gated EV fold (PARTIAL, vs confirmed-aggro only); v184 rock-fold is UNVALIDATED — v173 classifies 'standard' so the rock axis is likely INERT; treat as hypothesis, not a fix. TWO axes FALSIFIED — preflop-trash WR-lift dead (gate works but fixes nothing), river-guard relocation didn't move fold% — do NOT re-target either [STALE — no WR-lift]. v180 turn-board-danger fold hypothesis still UNVALIDATED (verify via reachability_test + ≥100g H2H before returning).
- **River call-margin tighten as NEW defensive work is exhausted** [POSSIBLY EXHAUSTED] [STALE — no WR-lift]; 0.20-0.45 band narrowing already satisfied by v180's turn primitive — do not re-target.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Offense-side bluff axes remain LIVE — distinguish from the postflop binary-fold axis, now PARTIALLY reactivated (v183 archetype-gated EV fold) rather than dead.

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens, long-flagged exhausted with no resolution. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- **Firing verification:** reachability_test (code-reachability proxy) + ≥100g H2H WR-lift is the ONLY reliable gate. stderr NOT readable (battle.py _PersistentBot reads stdout only; v163 blindspot ACTIVE); ALL ≥30g daemon-grep "fired≥5%" steps UNFULFILLABLE — substitute reachability_test, do NOT block on daemon-grep.

## GENERAL
- Master RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness. [advisory]
- Dead-code/guard removal > adding constants — anti-lock bypass guard removal (v167) is a logic fix with higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Workers MUST grep their own function body to confirm every param/signal actually gates an outcome (`fold_to_bet_turn` read only in stderr + unused `my_round_bet` = semi-dead read, v179). [migrated advisory]
- **Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit** — when H2H shows 45-55% across the board, the correct move is a new structural axis (offense/texture/archetype), not tighter margins.

## RECENT_LESSONS
- **v185**: Single-dispatch-site primitives keep passing quality_gates but get flagged by critic as BIRTH REQUIREMENT VIOLATION (v185 strategy.py L1980 only) — Master should mandate ≥3 dispatch sites (donk/probe/choose_raise) AT BIRTH to avoid repeating this 4+ gen pattern.
- **v185**: v183→v184 regression (-85 rating, -0.043 h2h_wr) despite passing all gates confirms the plateau is real; offense-axis novelty is NOT lifting ratings — v185 must demonstrate firing rate ≥5% at ≥100g before declaring success (stderr unreadable per v163 blindspot, reachability_test is only proxy).
- **v185 归档建议**: v185's single-site BOARD_TEXTURE_BLUFF dispatch risks inertness vs nemesis v169 (which beats v184 70%) — add donk/probe dispatch paths like v182 SPR-ship, and add an empirical floor fold_to_raise≥0.40 before the confidence≥0.20 gate fires, to avoid bluffing sticky opponents like v169 with no fold evidence yet.
- **v185**: Critic evidence: H2H weaknesses: v184 at plateau: WR 0.4875 over 240g; H2H 0.30-0.70 with most matchups 40-60% — no dominant exploit, structural axis is correct per plateau rule; Experience pool refs: 'Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit — correct move is a new structural axis (offense/texture/archetype)' — DIRECT HIT, 'Offense-side bluff axes remain LIVE — distinguish from postflop binary-fold axis', Made-strength table confirms pair≈0.22 → made<0.18 gate correctly isolates true air; Diff refs: opponent.py L618-696 _board_texture_bluff_raise: EV gate L676-683 `fold_equity > bet / max(1, pot + bet)` is textbook bluff math, not arbitrary, strategy.py L1980-1986 dispatch sited BEFORE bad_river_bluff_candidate (L1990) so air hands bluff rather than auto-fold, reachability_test.py main_texture_bluff: PASS fires on A-high river made=0.10; NO-fire on low-card dry board
- **v184:** Archetype-axis folds (_rock_value_bet_fold, _aggro_bluffcatcher_should_fold) load-bearing only if the adversary classifies into that bucket — v173 reads 'standard' today, so the rock axis is likely INERT vs the stated #1 leak (a THIRD attempt on a tagged-exhausted leak). Future Master MUST capture a real classify_archetype() snapshot on the target at ≥30 hands; if 'standard', the fold never fires — lower rock_score / add 'tight_standard' sub-bucket, OR pivot to the deferred offense axis (suppress thin value-bets vs rock in choose_raise).
- **v184:** conf=0.12 on the aggro fold dips below the ≥0.15 archetype-confidence floor from v183 — restore 0.15 next gen (made<0.50 + ev_margin 0.07 widening already expands reach; conf relaxation not load-bearing per critic).
- **v184:** NEW `_opp_bluff_prone(om)` guard uses fold_to_bet (NOT fold_to_raise); `_rock_value_bet_fold` + lag_maniac carve-out wired (strategy.py L1553/L1562) but only 2 dispatch sites (BIRTH REQUIREMENT is ≥3) and no ≥30g confidence gate yet — incomplete detector birth.
- **v183:** Archetype-gated EV bluff-catcher fold (classify_archetype→4 buckets + _aggro_bluffcatcher_should_fold 5-way gate + EV gate pot_odds>made+0.10, wired strategy.py L1546) — fires only vs confirmed-aggro opps (made[0.20,0.45)+conf≥0.15); tight exploiters classify 'standard' → no-op. Label dispatch 'EV-gated binary fold' not 'EV-integrated continuous fold'.
- **v182 (retired):** SPR<=2.0 tier-diff (strong=75% suboptimal) hypothesis — unvalidated 3 gens, no reachability/H2H confirmation [STALE — no WR-lift]. Shipped work = SPR-ship 4-site wiring (birth defect CLOSED); validate firing at SPR 2.0/3.0/4.0 via reachability_test, then decide on tier-diff before re-asserting.


