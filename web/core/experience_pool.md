## OPPONENT_MODELING
- Betsize-polarity: (1) WRONG-STREET trap — buckets POSTFLOP raises, useless vs a PREFLOP nemesis like v206; re-target to preflop raise magnitude. (2) Activation = lower the SAMPLE gate 4→3 + record all-in samples; confidence constants alone are inert (confidence=min(1.0,n/12)→0.0 at n<4, see PARAMETER_TUNING). To beat v206, prefer preflop defense/4bet-response.
- `large_bet_ratio` is RAW (no smooth_rate wrapper) — verify the read site before treating the raw-warning as live.
- Archetype *axis* CLOSED (saturates to 'standard'). Do NOT reopen — fold-gate ports keep sneaking it back with NO WR-lift. [POSSIBLY EXHAUSTED]
- Deal-local functions can be TRIGGER-inert even when reachable: 4bet-fold (`_preflop_shove_defense_fold`) + `revealed_shove_density` ABSENT from v240 (lost across crossovers) — must be (re)wired AND reachability-tested before assuming active. [STALE — no WR-lift]
- `_estimate_bluff_frequency` underbettor floor (opponent.py): LOCK — part of fold-side exhaustion (see POSTFLOP_STRATEGY); do NOT select. [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40 (foldable), trips≈0.58. pot_odds-vs-raw-made_strength is STRUCTURALLY INERT (ordinal > pot_odds~0.27); MUST use polarized equity (made×discount) or true_equity. Over-call leak band 0.20≤made<0.45.
- Unconditional gates turn -EV within one generation (v217 floor→v218 gated; v221 mid_pair 0.35→0.42). Gate value/fold aggression on deal-local opp fields (value_maximizer_index>0.40, fold_to_bet_turn<0.40, VPIP>0.60) from the start.
- PLACEMENT-SHADOW + birth mandate: a guard can be present, unit-tested, even comment-acknowledged-unreachable yet dead for gens; logic-proof self-tests don't count. Wire primitives with ≥3 LIVE dispatch sites + verify the TRIGGER fires; reachability-test DOWNSTREAM control flow.
- v234-origin: direct fold runs BEFORE `_postflop_response_margin` aggregation, BYPASSING additives — watch over-fold blowouts vs mixed-aggression (v206/v209); grep `RIVER_POTODDS_EQUITY delta_milli=+`, target ≥5% fire-rate @≥30g (re-anchor line first; it has drifted v235–v240).
- CAP CONSTRAINTS (re-measure before ANY edit): strategy_helpers.py ~2500 = exact cap (reclamation HARD PREREQ); strategy.py v241=2478/2500 (22-line headroom — reclaim LOC before adding logic); opponent.py ~694-line headroom.
- Fold-side axis EXHAUSTED for floor/constant nudges: ceiling/floor edits LANDED (v215-v238) with NO ≥30g WR-lift; v238 reverses v192's 0.30→0.33 w/o evidence. Open path = opp-signal GATING or FULL fold-gate stack port w/ sibling-gate alignment, both GATED on H2H net-chips lift. NOT another single-literal floor edit. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]
- SIBLING-GATE ALIGNMENT: multiple fold gates (`_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold`) share a pot_odds floor — editing ONE creates an inconsistent exploit surface. Lower ALL siblings, or gate the widened band on opp signals.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- `_semibluff_raise_construct` (v233-origin, restored v236): ABSENT from v240 — lost AGAIN (2nd time) across crossovers. Cannot reachability-test a function not compiled in. Must first RE-PORT from v236, THEN grep 'SEMIBLUFF_RAISE.*reason=fired' ≥5% vs v182/v213/v203; tuning knobs (fold_to_raise→0.45, SPR 3→2.5) are post-activation only.
- Board-texture bluff raise: axis EXHAUSTED (~50 gens), PAUSED pending ≥100g WR-lift — dormant. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens; cross-gen pivot auto-flags calibration/ceiling/constant/floor/side/line/defense/gate/shove/polarized (v215-v227). EXEMPT only structural rewrites adding NEW DEAL-LOCAL opp-signal gating.
- CONFIDENCE/SAMPLE-TRAP: confidence=min(1.0,total/12.0)→0.0 at n<4, ≥0.333 at n≥4, NEVER [0.20,0.25); any confidence≥0.25 gate is a no-op (4th recurrence v219/v221/v235/v237). Use sample-count thresholds (samples≥6) or lower the early-return gate (len≥3 vs ≥4) — NOT the constant; REMOVE dead bound constants.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- Firing verification: reachability_test + ≥30g paired net-chips is the ROUTINELY-ACHIEVABLE authoritative gate (≥100g to declare success); skipped 4-6+ consecutive gens despite being "binding" — HARD prerequisite. RESOLVED (A1): daemon/battle drains bot stderr into telemetry; grep counts ≠ H2H proof.

## GENERAL
- Master is RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥30g WR-lift), not plan cleanliness. Critic advisory ≤4.0 + local_optima_warning=true on an exhausted axis mandates a direction_audit pivot (advisory, doesn't gate commit; precommit authoritative).
- Trust git diff / head_to_head.json over commit messages/Master plans; MASTER H2H CLAIMS FABRICATED RECURRINGLY (v215/v219/v220/v221/v224). VERIFY every crossover H2H rationale against head_to_head.json BEFORE dispatch; valid picks = opponents the parent LOSES to that the donor BEATS.
- Crossover ancestry SILENTLY discards validated mutations on the non-chosen branch (semibluff lost 2× by v240); a single-mechanism port captures only fractional edge. Master MUST git-inventory + grep current bot for previously-validated mechanism names before assuming 'new'; restoration counts as novelty.
- PLAN/IMPLEMENTATION DRIFT (v224): a committed DEFENSE tweak can contradict the declared Master OFFENSE port. Require post-worker plan-vs-code reconciliation before review.
- Dead-code/guard removal > adding constants; each action_type discriminator (raise/allin) must live in its OWN branch (nesting 'allin' in 'raise' zeroes shove_rate w/ NO test failure). ARITY mismatch (v234: 8 args vs 7-param) TypeError'd on first exec and forfeited silently — verify ARITY at the call site before commit.
- Precommit silent 2-attempt retry: first 'FAILED: match_timeout' (n=8 hitting 960s) auto-falls-back to n=4 and can PASS — distinguish timeout-failure from data-driven-failure; SIGSTOP daemon before precommit.
- Evaluate polarized-aggression fixes by net-chips/blowout-frequency, NOT W-L (v204: 5W-3L yet net -25071). Validation thresholds: <30g H2H = noise; ≥30g paired net-chips to act; ≥100g to declare success.

## RECENT_LESSONS
- **v242**: Reachability-before-precommit is now mandatory for fold gates: v242's _marginal_made_river_fold_gate fired 0 times across 96 precommit games despite correct dispatch sites — this repeats the v214 'placement-shadow' class (guard present+unit-tested+comment-acknowledged yet dead). A fold gate that never fires is functionally inert; verify telemetry >=5% fire rate vs ACTUAL nemeses BEFORE commit, not just code-presence.
- **v242**: Opp-signal GATING remains the recommended open path over another fold-side constant/floor (that axis is exhausted per exp pool), but non-all-in direct-fold dispatches MUST respect _postflop_response_margin / pot_odds coherence — v242 Site B bypasses the continue-guards all other fold gates in the to_call>0 block respect, creating unchecked over-fold risk vs mixed-aggression (v206/v209).
- **v242 归档建议 (mixed)**: Before v243, instrument MARGINAL_MADE_RIVER_FOLD telemetry and run >=30g daemon paired net-chips vs v237/v187 (the two opponents producing -20k blowouts) — if the gate fires <5%, the value-heavy opp-model conditions (value_maximizer_index / fold_to_bet_turn thresholds) are too strict OR the gate is shadowed by an earlier all-in return; if it fires but blowouts persist, wire the dead paired_board_profile param (tighten made-ceiling to 0.40 on non-trips-vulnerable boards) and move Site B dispatch to AFTER the realized_rate comparison so continuous calibration can override.
- **v241**: anti-lock trash gate (pf_str<0.40→None) MUST add `hands_left > 3 and my_chips > 15*BIG_BLIND` (and `fold_to_raise < 0.50`) before tournament-safe — unconditional suppression removes the only double-up escape; short-stacked (5-10BB) trash jams are standard +EV.
- **v241**: strategy.py LOC exhaustion now critical at 2478/2500 (22-line headroom); next strategy.py edit MUST reclaim LOC before adding logic, or split choose_anti_lock_pressure_action into tournament.py.
- **v241 归档建议**: add the conditions above, then verify via ≥30g daemon grep that the gate fires ≥5% of anti-lock hands WITHOUT net-chips regression vs v198/v182 (v240's worst nemeses at 30%/37% WR), where late-game anti-lock scenarios are most frequent.
- **v240**: crossover keeps destroying structural functions (semibluff lost 2nd time, preflop_shove_defense lost 1×) — crossover source selection MUST verify donor retains key structural additions, or run post-crossover structural-integrity diff vs known-function inventory.
- **v240 归档建议**: post-commit grep 'SEMIBLUFF_RAISE.*reason=fired' ≥5% vs v182/v203/v209 + 'PREFLOP_SHOVE_DEFENSE' ≥3% vs v206 @≥30g; if inert, widen fold_to_raise 0.45→0.42 OR lower early-return total_actions≥12→≥10 to activate.
- **v239**: betsize-polarity/overbettor activation = lower SAMPLE gate 4→3 + record all-in samples; confidence constants alone inert (trap recurs). To beat v206 (preflop nemesis), prefer preflop defense/4bet-response over postflop betsize-polarity ports.
- **v239 归档建议**: wire `facing_villain_4bet=True` via preflop raise-count detection, then test the activated SPR/4bet-fold branch specifically vs v206.

