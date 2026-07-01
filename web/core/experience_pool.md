## OPPONENT_MODELING
- Archetype *axis* CLOSED (saturates to 'standard'; v184 rock-fold inert). Do NOT reopen — recurring violation = fold-gate *ports* (v215/v219/v221/v224 re-imported _aggro_bluffcatcher/_rock_value_bet) sneaking it back without WR-lift. [POSSIBLY EXHAUSTED]
- calldown_profile sample trap: foldy opps never reach n≥4 → use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `_opp_betsize_polarity` (n≥4): WRONG-STREET trap — buckets POSTFLOP raises, useless vs a PREFLOP nemesis like v206; re-target to preflop raise magnitude if attacking preflop aggression.
- `large_bet_ratio` is RAW (no smooth_rate wrapper) — verify the read site before treating the raw-warning as live.
- 4bet trigger-inert PRINCIPLE (timeless): a reachable dispatch site can still NEVER fire if the TRIGGER is rare — reachability-test the TRIGGER, not the path. `_preflop_shove_defense_fold` CONFIRMED present in v236 (state.py L395); only TRIGGER-reachability (4bet path fires ≥5%?) remains unverified.
- Deal-local `revealed_shove_density` detector (v208) + divergence OVERRIDE (v209): mechanism EXISTS in v236 but NEVER reachability-verified across ~28 gens — treat as DORMANT; run the ≥30g test or drop. [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40 (foldable post-v205), trips≈0.58. pot_odds-vs-raw-made_strength STRUCTURALLY INERT (ordinal > pot_odds~0.27); MUST use polarized equity (made×discount) or true_equity. Over-call leak band 0.20≤made<0.45.
- #1 MULTI-SITE -20k/over-call leak (v196→, 20+ gens): fold-side CROSS_GEN_PIVOT no longer blocks — validated-working via crossover-bypass (v215 fold discipline, v217/v218 value-tier floor, v219/v221/v224 underbettor/multibarrel ceiling, v222 VPIP shove defense ALL LANDED). CONTEXT: vs value-heavy SHOVERS → fold MORE in stack-offs (0.20≤made<0.45); vs WIDE open-shovers VPIP>0.60 → fold LESS (folding 55 @pot_odds~0.50 is -EV). Further fold-side work GATED on actual H2H net-chips lift.
- Birth mandate (recurring 8+ gens): wire primitives with ≥3 LIVE dispatch sites AT BIRTH + verify TRIGGER fires; logic-proof self-tests do NOT count.
- Unconditional gates without opponent conditioning turn -EV within one generation (v217 floor → v218 had to gate; v221 mid_pair 0.35→0.42). Gate value/fold aggression on deal-local opp fields (value_maximizer_index>0.40, fold_to_bet_turn<0.40, VPIP>0.60) from the start.
- PLACEMENT-SHADOW + INERT-GUARD pattern (v213/v214/v209): a guard can be present, unit-tested, comment-acknowledged-unreachable yet dead for multiple gens; scenario gates like `_reached_river` are structurally impossible preflop/flop/turn. Reachability-test DOWNSTREAM control flow, not just the function.
- v234: direct fold at strategy.py L1622 runs BEFORE `_postflop_response_margin` aggregation, BYPASSING bluff_heavy_call_widen / blocker_profile / check_resistance / line_strength additives. Watch over-fold blowouts vs mixed-aggression (v206/v209); grep `RIVER_POTODDS_EQUITY delta_milli=+` in daemon stderr, target ≥5% fire-rate @≥30g vs v200/v201/v198.
- CAP CONSTRAINTS (re-measure before ANY edit): strategy.py = 2485/2500 (15-line headroom — reclaim LOC first); strategy_helpers.py ~2500 exact cap (reclamation HARD PREREQ); opponent.py ~694-line headroom.
- [STALE cautions — no ≥30g WR-lift, do NOT re-attempt]: (a) inching made_strength floors / two-pair fold ceilings 0.42→0.48 / single-site / per-site-sequential fold approaches; (b) offense value-sizing-UP made≥0.55→0.50 (proof bot v159 culled, v198/v199/v207 anchors cold). [STALE — no WR-lift] [POSSIBLY EXHAUSTED]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- `_semibluff_raise_construct`: v233-origin, lost across v234/v235, RESTORED in v236 (opponent.py + 2 strategy.py dispatch sites). Reachability STILL unverified @≥30g: grep 'SEMIBLUFF_RAISE.*reason=fired' in daemon stderr, target ≥5% vs v182/v213/v203; if <5% loosen fold_to_raise→0.45 or SPR floor 3→2.5 (NOT widen made_str); vs PREFLOP nemesis v206 loosen opp_bet_ratio band 0.25-0.65→0.20-0.70.
- Board-texture bluff raise (v185→v191): dispatch LIVE ~51 gens ago, axis EXHAUSTED, PAUSED pending ≥100g WR-lift — dormant. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens; cross-gen pivot auto-flags calibration/ceiling/constant/floor/side/line/defense/gate/shove/polarized (v215-v227). EXEMPT only structural rewrites adding NEW DEAL-LOCAL opponent-signal gating; CLOSED archetype axis does NOT reopen.
- INERT-MUTATION TRAP: confidence = min(1.0,total/12.0) → 0.0 at n<4, ≥0.333 at n≥4, NEVER [0.20,0.25); gates like confidence≥0.25 are ALWAYS true when classified (v219/v221/v235/v237 recurrence). Use sample-count thresholds (samples≥6) or lower the early-return gate (len>=3 vs >=4) — NOT the confidence constant.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- Firing verification: reachability_test + ≥30g paired net-chips is the ROUTINELY-ACHIEVABLE authoritative gate (≥100g to declare success). Reachability skipped 4-6+ consecutive gens despite being "binding" — treat as HARD prerequisite. RESOLVED (A1): daemon/battle drains bot stderr into telemetry for early fire-rate signal (≥5% after ≥30g); grep counts ≠ H2H proof.

## GENERAL
- Master is RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥30g WR-lift), not plan cleanliness.
- Dead-code/guard removal > adding constants — each action_type discriminator (raise/allin) must live in its OWN branch (nesting 'allin' inside 'raise' made shove_rate 0 for 5 gens with NO test failure; fixed v194). At plateau (WR~0.50): pursue a NEW structural axis.
- Validation thresholds: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff / head_to_head.json over commit messages/Master plans; MASTER H2H CLAIMS FABRICATED RECURRINGLY (v215/v219/v220/v221/v224 — stated matchups contradict head_to_head.json). VERIFY every crossover H2H rationale against head_to_head.json BEFORE dispatch; valid complementary-strength picks come from opponents the parent LOSES to that the donor BEATS.
- Critic advisory ≤4.0 + local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Crossover ancestry SILENTLY discards validated mutations on the non-chosen branch — Master MUST git-inventory + port sibling-lineage critical mutations when branching_from is set. 'Lost-fix restoration via crossover ancestry audit' is a validated reusable diagnostic pattern.
- PLAN/IMPLEMENTATION DRIFT (v224): a committed DEFENSE tweak can contradict the declared Master OFFENSE port. Require post-worker plan-vs-code reconciliation before review.
- v234 DEAD-CODE-BY-TYPEERROR PATTERN: v220/v221 shipped the direct river-fold call with 8 args against a 7-param `_river_potodds_equity_margin`; the gate TypeError'd on first execution and the bot forfeited silently. Future structural activations MUST verify function ARITY matches the call site before commit.
- Precommit silent 2-attempt retry: first 'FAILED: match_timeout' (n=8 hitting 960s mirror limit) auto-falls-back to n=4 and can PASS — distinguish timeout-failure from data-driven-failure; SIGSTOP the daemon before precommit.
- Evaluate polarized-aggression fixes by net-chips/blowout-frequency, NOT W-L (v204: 5W-3L yet net -25071 = thin wins masking one blowout).

## RECENT_LESSONS
- **v237**: Exhausted axis LOCK — `_estimate_bluff_frequency` underbettor floor attempted in v219/v221/v235/v237 with ZERO ≥30g WR-lift. Master MUST NOT select this axis. Higher-EV path = port the FULL _multibarrel_line_fold / _aggro_bluffcatcher_should_fold / _rock_value_bet_fold stack (4 strategy.py dispatch sites) from v195/v215.
- **v237**: INERT-MUTATION TRAP 4th recurrence — betsize_polarity confidence is structurally 0 or ≥0.333, never [0.20,0.25); any confidence≥0.25 gate is a no-op. Replace ALL betsize_polarity confidence thresholds with sample_count≥6 thresholds before further calibration.
- **v236**: strategy.py = 2485/2500 LOC (15-line headroom) — next strategy.py edit MUST reclaim LOC first; binding constraint on offense-axis evolution.
- **v236**: CROSSOVER FRAGILITY — validated-pending fns silently lost across crossovers (`_semibluff_raise_construct` absent v234/v235, restored v236). Master MUST grep current bot for previously-validated mechanism names before assuming 'new'; restoration counts as novelty.
- **v235**: SINGLE-MECHANISM PORT CAPTURES FRACTIONAL EDGE — v221's H2H edge over v206 nemeses comes from its FULL fold-gate stack (4 dispatch sites), not the underbettor floor alone; porting only the floor (+24 LOC) produced CI-straddling precommit + v203 regression.
