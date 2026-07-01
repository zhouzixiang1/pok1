## OPPONENT_MODELING
- Betsize-polarity: re-target to PREFLOP raise magnitude as STRUCTURAL 4bet-response gating — NOT a postflop floor/constant nudge (that fold-side axis is exhausted, see POSTFLOP_STRATEGY). Activation = lower SAMPLE gate 4→3 + record all-in samples; confidence constants alone are inert. To beat preflop nemesis v206, prefer preflop defense/4bet-response.
- `large_bet_ratio` is RAW (no smooth_rate wrapper) — verify the read site before treating the raw-warning as live.
- Archetype *axis* CLOSED (saturates to 'standard'); fold-gate ports keep sneaking it back with NO WR-lift — do NOT reopen. [POSSIBLY EXHAUSTED]
- Deal-local fns can be TRIGGER-inert even when reachable. Verified-current (v242): `_preflop_shove_defense_fold` (3 dispatch sites) + `_facing_v4bet` flag (1 site) are PRESENT/wired; `revealed_shove_density` is still ABSENT — needs re-port + reachability-test before assuming active. [STALE — no WR-lift]
- `_estimate_bluff_frequency` underbettor floor (opponent.py): LOCK — part of fold-side exhaustion; do NOT select. [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40 (foldable), trips≈0.58. pot_odds-vs-raw-made_strength is STRUCTURALLY INERT; use polarized equity (made×discount) or true_equity. Over-call leak band 0.20≤made<0.45.
- Unconditional gates turn -EV within one gen (v217 floor→v218 gated; v221 mid_pair 0.35→0.42). Gate value/fold aggression on deal-local opp fields (value_maximizer_index>0.40, fold_to_bet_turn<0.40, VPIP>0.60) from the start.
- PLACEMENT-SHADOW (v214/v242): a guard can be present, unit-tested, even comment-acknowledged-unreachable yet dead for gens. Wire ≥3 LIVE dispatch sites + verify the TRIGGER fires; reachability-test DOWNSTREAM control flow.
- Fold-side axis EXHAUSTED for floor/constant nudges: edits LANDED v215-v238 with NO ≥30g WR-lift. Open path = opp-signal GATING or FULL fold-gate stack port w/ sibling-gate alignment, GATED on H2H net-chips lift — NOT another floor edit. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]
- SIBLING-GATE ALIGNMENT: fold gates (`_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold`) share a pot_odds floor — editing ONE makes an inconsistent exploit surface; lower ALL siblings or gate the widened band on opp signals.
- v234-origin direct fold runs BEFORE `_postflop_response_margin` aggregation, BYPASSING additives — watch over-fold vs v206/v209; re-anchor the drifted line, then grep `RIVER_POTODDS_EQUITY delta_milli=+` target ≥5% @≥30g.
- CAP CONSTRAINTS (re-measured v242): strategy_helpers.py=2500 EXACT cap (reclamation HARD PREREQ); strategy.py=2475/2500 (25-line headroom — reclaim LOC before adding logic); opponent.py=1610.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- `_semibluff_raise_construct`: VERIFIED PRESENT in v242 (opponent.py:1333, dispatched strategy.py:1812) — restored, no longer a re-port TODO. Open task = reachability: grep 'SEMIBLUFF_RAISE.*reason=fired' ≥5% vs v182/v213/v203 @≥30g; tuning knobs (fold_to_raise→0.45, SPR 3→2.5) are post-activation only.
- Board-texture bluff raise: axis EXHAUSTED (~50 gens), PAUSED pending ≥100g WR-lift — dormant. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens; cross-gen pivot auto-flags calibration/ceiling/constant/floor/side/line/defense/gate/shove/polarized. EXEMPT only structural rewrites adding NEW DEAL-LOCAL opp-signal gating.
- CONFIDENCE/SAMPLE-TRAP: confidence=min(1.0,total/12.0)→0.0 at n<4, ≥0.333 at n≥4, NEVER [0.20,0.25); any confidence≥0.25 gate is a no-op. Use sample-count (samples≥6) or lower early-return gate (len≥3 vs ≥4) — NOT the constant; REMOVE dead bound constants.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- Firing verification: reachability_test + ≥30g paired net-chips is the authoritative gate (≥100g to declare success); skipped 4-6+ gens despite being "binding" — HARD prerequisite. RESOLVED (A1): daemon drains bot stderr into telemetry; grep counts ≠ H2H proof.

## GENERAL
- Master is RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥30g WR-lift), not plan cleanliness. Critic advisory ≤4.0 + local_optima_warning=true on an exhausted axis mandates a direction_audit pivot (advisory; precommit authoritative).
- Trust git diff / head_to_head.json over commit messages/Master plans; MASTER H2H CLAIMS FABRICATED RECURRINGLY (v215/v219/v220/v221/v224). VERIFY every crossover H2H rationale against head_to_head.json BEFORE dispatch; valid picks = opponents the parent LOSES to that the donor BEATS.
- Crossover ancestry SILENTLY discards validated mutations on the non-chosen branch; but re-ports DO persist when caught (semibluff present in v242). Master MUST git-inventory + grep current bot for previously-validated mechanism names before assuming 'new'; restoration counts as novelty.
- PLAN/IMPLEMENTATION DRIFT (v224): a committed DEFENSE tweak can contradict the declared Master OFFENSE port — require post-worker plan-vs-code reconciliation before review.
- Dead-code/guard removal > adding constants; each action_type discriminator (raise/allin) needs its OWN branch (nesting 'allin' in 'raise' zeroes shove_rate w/ NO test failure). Verify ARITY at the call site (v234: 8 args vs 7-param TypeError'd silently) before commit.
- Precommit silent 2-attempt retry: first 'FAILED: match_timeout' (n=8→960s) auto-falls-back to n=4 and can PASS — distinguish timeout-failure from data-driven-failure; SIGSTOP daemon before precommit.
- Evaluate polarized-aggression by net-chips/blowout-frequency, NOT W-L (v204: 5W-3L yet net -25071). <30g H2H=noise; ≥30g paired net-chips to act; ≥100g to declare success.

## RECENT_LESSONS
- **v242**: Reachability-before-precommit is mandatory for fold gates: `_marginal_made_river_fold_gate` fired 0/96 precommit games despite correct dispatch sites — repeats the v214 placement-shadow class. Verify telemetry ≥5% fire-rate vs ACTUAL nemeses BEFORE commit, not just code-presence.
- **v242**: Opp-signal GATING is the open path over another fold-side constant/floor (axis exhausted), but non-all-in direct-fold dispatches MUST respect `_postflop_response_margin`/pot_odds coherence — v242 Site B bypasses the continue-guards other fold gates respect → unchecked over-fold risk vs mixed-aggression (v206/v209).
- **v242 NEXT**: instrument MARGINAL_MADE_RIVER_FOLD telemetry + run ≥30g daemon paired net-chips vs v237/v187 (the -20k blowout opps); fires <5% → value-heavy opp conditions too strict OR shadowed by an earlier all-in return; fires + blowouts persist → wire dead paired_board_profile param + move Site B dispatch to AFTER the realized_rate comparison.
- **v241**: anti-lock trash gate (pf_str<0.40→None) MUST add `hands_left > 3 and my_chips > 15*BIG_BLIND` (+`fold_to_raise < 0.50`) before tournament-safe — unconditional suppression removes the only double-up escape; short-stacked (5-10BB) trash jams are standard +EV.
- **v241**: strategy.py LOC exhaustion: v242=2475/2500 (25-line headroom); next strategy.py edit MUST reclaim LOC before adding logic, or split choose_anti_lock_pressure_action into tournament.py.
- **v240**: crossover keeps destroying structural functions — crossover source selection MUST verify donor retains key additions, or run post-crossover structural-integrity diff vs known-function inventory. (semibluff + preflop_shove_defense are both PRESENT in v242 — latest re-ports held.)
