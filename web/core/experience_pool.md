## OPPONENT_MODELING
- archetype axis (classify_archetype via smooth_rate) CLOSED — saturates to 'standard' (v184 rock-fold INERT); respect ≥0.15 confidence floor; prefer deal-local history signals. Do NOT reopen.
- calldown_profile sample trap: foldy opps never reach n≥4 → use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `_opp_betsize_polarity` (v193, n≥4) OPERATIONAL: n_shove COUNT via True flag, NOT ratio=2.0 (ratio feeds only unread avg_fraction). Deal-local SIGNAL feeding fold-calibration; the dead mechanism was constant-nudge margin tweaks — different mechanism, not a conflict.
- `large_bet_ratio` is RAW (no smooth_rate wrapper) — verify read site before treating the raw-warning as live.
- 4bet trigger-inert (v203 DEEPER layer): opp_pf_raises>=2 is itself RARE in 70h HU — a path-reachable dispatch site can still NEVER fire. Always reachability-test the TRIGGER firing rate, not just the path; widen to opp_pf_raises>=1 + raise-size>15BB proxy if inert.

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40, trips≈0.58. pot_odds-vs-raw-made_strength STRUCTURALLY INERT (ordinal > pot_odds~0.27); MUST use polarized equity (made×discount) or true_equity. Over-call leak band 0.20≤made<0.45.
- **#1 PRIORITY — MULTI-SITE -20k/0%-fold leak (v196→v203, ~7 gens RESILIENT, NOT CLOSED)**: fold-side attacks LANDED (river v200, preflop shove-defense v201, turn/river all-in v202, preflop 4bet v203) yet -19k/-20k blowouts PERSIST. Newly-sanctioned variant (genuinely untried): attack BOTH sites in PARALLEL + fix trigger-inert. If the parallel variant fails @≥30g paired H2H, RETIRE the fold-side axis entirely. [single-site & per-site-sequential fold approaches: STALE — no WR-lift]
- RECONCILED (was contradiction): parametric fold-margin nudge-to-improve is DISPROVEN (v184/v188/v189/v190 all INERT) [STALE — no WR-lift]. DISTINCT and sanctioned: widening-to-UN-inert a proven-inert gate (made[0.20,0.50)→0.50-0.60 TPTK, 0.40 discount→0.45) — ONLY after reachability proves the gate is currently inert. Do not confuse nudge-tuning with un-inerting.
- Birth mandate (RECURRING v182/v185/v191/v193/v201/v202/v203 — 7 gens): wire primitives with ≥3 LIVE dispatch sites AT BIRTH + reachability/H2H-WR @≥30g. DEEPER (v203): verify the TRIGGER CONDITION itself fires (not just the path) — a dead trigger makes a 2-site dispatch inert. self-test logic proofs do NOT count (v202 self-test was dead code).
- CAP CONSTRAINTS (post-v203, authoritative): strategy.py=2499/2500 (1-LINE headroom vs HARD_CAP), opponent.py=1601/2500, strategy_helpers.py=2500/2500 EXACT CAP. LOC reclamation (comment-condensation/helper extraction) is a HARD PREREQ before ANY strategy.py/helpers edit or quality_gates fails on file size.
- `facing_villain_4bet=True` in `_preflop_spr_commitment_gate` STILL DORMANT — v203 attempted fold-marginal-to-4bet via a SEPARATE fn (`_preflop_shove_defense_fold`, removed opponent_allin guard + 2nd site) surfacing trigger-inert doubt. Wiring the ORIGINAL 4bet-NON-allin param remains open; verify v203's axis fires @≥30g before adding more.
- Offense value-sizing-UP (v198 C1-C3 + v199 betsize-polarity arm): FIRING (+26351 vs calling-station v159), embargo LIFTED. [POSSIBLY EXHAUSTED]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Board-texture bluff raise (v185→v191): dispatch LIVE but axis EXHAUSTED, PAUSED pending ≥100g WR-lift. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT only for structural rewrites adding NEW DEAL-LOCAL opponent-signal gating; CLOSED archetype axis does NOT reopen.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- Firing verification: reachability_test + ≥100g H2H WR-lift is the ONLY reliable gate. stderr NOT readable (_PersistentBot stdout-only); daemon-grep "fired≥5%" UNFULFILLABLE — gate must be reachability/H2H-WR, NOT a grep count.

## GENERAL
- Master RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness.
- Dead-code/guard removal > adding constants — logic fixes yield higher EV/line than margin tweaks. Comment-only LOC-condensation must NOT touch behavioral code (even an inert ratio/sample change violates the worker contract). Plateau (WR~0.50): pursue a NEW structural axis.
- Validation thresholds: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed/no Glicko rating).
- Critic advisory ≤4.0 with local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Each action_type discriminator (raise/allin) must live in its OWN branch — nesting 'allin' inside 'raise' makes shove_rate permanently 0 (fixed v194). ratio=2.0 was NOT a corrupter.
- Recording-instrumentation bugs are SILENT: v194's allin check nested in `if action_type=='raise'` killed shove_rate 5 gens with NO test/smoke failure. Always reachability-test the DOWNSTREAM consumer after any recording-site change — detector unit tests pass while the prod feed stays empty.
- Crossover ancestry silently discards validated mutations on the non-chosen branch — Master MUST inventory+port sibling-lineage critical mutations (esp. fold-side/leak-attack) as worker tasks when branching_from is set (RECURRING: v197, v199 both regressed v196's fold).
- Workers MUST grep their own dispatch sites + run reachability_test to confirm every param/signal gates an outcome before claiming coverage.
- Precommit silent 2-attempt retry: a first 'FAILED: match_timeout' (n=8 hitting 960s mirror limit) auto-falls-back to n=4 and can PASS — distinguish timeout-failure from data-driven-failure; the leak manifests in chip-distribution (stack-offs), not pure win-rate.
- If ALL active axes are validation-paused, the ONLY permitted direction is a NEW deal-local opponent-history detector axis not yet attempted — do NOT re-tune blocked axes.

## RECENT_LESSONS
- **v204**: Critic evidence: H2H weaknesses: v203 weakest: vs v164 (0.30 WR, 10g), vs v186/v190 (0.40 WR, 10g); #1 priority leak is 0%-fold/-20k stack-off documented 23+ gens; Experience pool refs: '#1 PRIORITY MULTI-SITE -20k/0%-fold leak (~7 gens RESILIENT)' sanctions 'fix trigger-inert' as newly-approved variant; '4bet trigger-inert (v203 DEEPER layer)' lesson: reachability-test TRIGGER rate not just path; Diff refs: opponent.py L994-1007: graduated tier exemption in _river_potodds_equity_margin (was blanket 'strong','nut' return 0.0); opponent.py L1076-1086: same graduation in _allin_polarized_equity_fold; strategy.py L1535+L1659: pair_profile wired through 2 dispatch sites; HAND_CLASS_SCORE[1]=0.22 for ALL one-pair hands confirms made_strength does not differentiate TPTK from bottom pair
- **v203 (preflop 4bet-arm rewire)**: Added 2nd dispatch site (sb_vs_reraise, removed opponent_allin guard) → 3 total sites. KEY LESSON: a 2nd site does NOT guarantee firing — the TRIGGER opp_pf_raises>=2 is itself rare in 70h HU; future Masters MUST reachability_test the TRIGGER firing rate, not just the dispatch path. strategy.py=2499/2500 (1-line headroom) makes LOC reclamation a HARD PREREQ for v204+. Gates: crit9-9, size strat2499, precommit PASS (after daemon SIGSTOP). vs v202 0W-4L tiny (-52); vs v182 3W-1L (+1525); residual POSTFLOP all-in leak NOT in scope.
- **v203 next (v204 priority)**: (1) reachability opp_pf_raises>=2 fires ≥5% vs v194/v188, else widen to opp_pf_raises>=1 + raise-size>15BB proxy; (2) LOC reclamation HARD prereq; (3) attack postflop all-in round_idx>=2 (OTHER half of multi-site -20k leak); (4) pfr/vpip gate in `_preflop_shove_defense_fold` vs wide 4bet bluffers (VPIP>0.60 @ n≥30 H2H).
- **v202 (turn/river all-in polarized-equity fold)**: 6th-consecutive single-dispatch birth-defect — the ≥2-site mandate is NOT enforced at birth. -20k/0%-fold leak confirmed MULTI-SITE & RESILIENT: v202's single turn/river gate did NOT close it (precommit -19911 vs v201, -19758 vs v194). Future gens attack BOTH sites in parallel.
- **v201 (preflop shove-defense) over-fold risk**: treating all preflop shoves as premiums-only is exploitable vs wide/open-shovers (VPIP>0.60) — folding 55 at pot_odds~0.50 is -EV. Gate marginal/small-pair fold on pfr/vpip or a dedicated shove-width signal once n≥30 H2H.

