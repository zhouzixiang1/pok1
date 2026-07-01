## OPPONENT_MODELING
- Archetype *axis* CLOSED (saturates to 'standard'; v184 rock-fold inert). Do NOT reopen — recurring violation = fold-gate *ports* (v215/v219/v221/v224) sneaking it back with NO WR-lift. [POSSIBLY EXHAUSTED]
- Sample-trap: foldy opps never reach betsize_polarity n≥4 (confidence=min(1.0,n/12)→0.0 at n<4). Use empirical rate at n≥3; fall back to pool-wide fold_to_raise when per-street samples<2.
- `_opp_betsize_polarity` WRONG-STREET trap — buckets POSTFLOP raises, useless vs a PREFLOP nemesis like v206; re-target to preflop raise magnitude if attacking preflop aggression.
- `large_bet_ratio` is RAW (no smooth_rate wrapper) — verify the read site before treating the raw-warning as live.
- Deal-local TRIGGER-inert detectors (4bet `_preflop_shove_defense_fold` present v236 L395; `revealed_shove_density` v208/v209): a reachable site can still NEVER fire if the TRIGGER is rare; both exist in v236 but NEVER reachability-verified across ~28 gens — run the ≥30g test or drop. [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40 (foldable post-v205), trips≈0.58. pot_odds-vs-raw-made_strength STRUCTURALLY INERT (ordinal > pot_odds~0.27); MUST use polarized equity (made×discount) or true_equity. Over-call leak band 0.20≤made<0.45.
- Fold-side axis EXHAUSTED for floor/constant nudges: ceiling/floor edits LANDED (v215/v217/v218/v219/v221/v224/v238) but produced NO ≥30g WR-lift; v238 critic (5.0) confirms reversing v192's 0.30→0.33. Open path = opp-signal GATING or FULL fold-gate stack port w/ sibling-gate alignment — both still GATED on actual H2H net-chips lift vs value-heavy shovers. NOT another single-literal floor edit. [STALE — no WR-lift] [POSSIBLY EXHAUSTED]
- Birth mandate (recurring 8+ gens): wire primitives with ≥3 LIVE dispatch sites AT BIRTH + verify TRIGGER fires; logic-proof self-tests do NOT count.
- Unconditional gates turn -EV within one generation (v217 floor→v218 gated; v221 mid_pair 0.35→0.42). Gate value/fold aggression on deal-local opp fields (value_maximizer_index>0.40, fold_to_bet_turn<0.40, VPIP>0.60) from the start.
- PLACEMENT-SHADOW + INERT-GUARD (v213/v214/v209): a guard can be present, unit-tested, comment-acknowledged-unreachable yet dead for gens; scenario gates like `_reached_river` are structurally impossible preflop/flop/turn. Reachability-test DOWNSTREAM control flow.
- v234: direct fold at strategy.py L1622 runs BEFORE `_postflop_response_margin` aggregation, BYPASSING additives — watch over-fold blowouts vs mixed-aggression (v206/v209); grep `RIVER_POTODDS_EQUITY delta_milli=+`, target ≥5% fire-rate @≥30g.
- CAP CONSTRAINTS (re-measure before ANY edit): strategy.py=2485/2500 (15-line headroom — reclaim LOC first); strategy_helpers.py ~2500 exact cap (reclamation HARD PREREQ); opponent.py ~694-line headroom.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- `_semibluff_raise_construct` (v233-origin, restored v236): reachability STILL unverified @≥30g — grep 'SEMIBLUFF_RAISE.*reason=fired', target ≥5% vs v182/v213/v203; if <5% loosen fold_to_raise→0.45 or SPR 3→2.5 (NOT widen made_str); vs preflop nemesis v206 loosen opp_bet_ratio 0.25-0.65→0.20-0.70.
- Board-texture bluff raise (v185→v191): axis EXHAUSTED ~51 gens, PAUSED pending ≥100g WR-lift — dormant. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens; cross-gen pivot auto-flags calibration/ceiling/constant/floor/side/line/defense/gate/shove/polarized (v215-v227). EXEMPT only structural rewrites adding NEW DEAL-LOCAL opp-signal gating.
- INERT-MUTATION TRAP: confidence=min(1.0,total/12.0)→0.0 at n<4, ≥0.333 at n≥4, NEVER [0.20,0.25); any confidence≥0.25 gate is a no-op (v219/v221/v235/v237). Use sample-count thresholds (samples≥6) or lower the early-return gate (len>=3 vs >=4) — NOT the constant; REMOVE dead bound constants rather than carry them inert.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- Firing verification: reachability_test + ≥30g paired net-chips is the ROUTINELY-ACHIEVABLE authoritative gate (≥100g to declare success). Skipped 4-6+ consecutive gens despite being "binding" — HARD prerequisite. RESOLVED (A1): daemon/battle drains bot stderr into telemetry; grep counts ≠ H2H proof.

## GENERAL
- Master is RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥30g WR-lift), not plan cleanliness.
- Dead-code/guard removal > adding constants — each action_type discriminator (raise/allin) must live in its OWN branch (nesting 'allin' inside 'raise' made shove_rate 0 for 5 gens with NO test failure; fixed v194). At plateau (WR~0.50): pursue a NEW structural axis.
- Validation thresholds: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff / head_to_head.json over commit messages/Master plans; MASTER H2H CLAIMS FABRICATED RECURRINGLY (v215/v219/v220/v221/v224 — stated matchups contradict head_to_head.json). VERIFY every crossover H2H rationale against head_to_head.json BEFORE dispatch; valid picks = opponents the parent LOSES to that the donor BEATS.
- Critic advisory ≤4.0 + local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Crossover ancestry SILENTLY discards validated mutations on the non-chosen branch, and a single-mechanism port captures only fractional edge (v235: floor alone → CI-straddling + v203 regression; full stack needed). Master MUST git-inventory + port sibling-lineage critical mutations; restoration counts as novelty.
- PLAN/IMPLEMENTATION DRIFT (v224): a committed DEFENSE tweak can contradict the declared Master OFFENSE port. Require post-worker plan-vs-code reconciliation before review.
- v234 DEAD-CODE-BY-TYPEERROR PATTERN: v220/v221 shipped a direct river-fold call with 8 args against a 7-param fn; the gate TypeError'd on first execution and the bot forfeited silently. Verify ARITY matches the call site before commit.
- Precommit silent 2-attempt retry: first 'FAILED: match_timeout' (n=8 hitting 960s) auto-falls-back to n=4 and can PASS — distinguish timeout-failure from data-driven-failure; SIGSTOP daemon before precommit.
- Evaluate polarized-aggression fixes by net-chips/blowout-frequency, NOT W-L (v204: 5W-3L yet net -25071 = thin wins masking one blowout).

## RECENT_LESSONS
- **v238**: AXIS-EXHAUSTION CONFIRMED — 'floor'/constant-only pot_odds threshold nudges in fold gates are pivot-trigger-saturated; v238 reverses v192's documented 0.30→0.33 fix without new evidence. Future Master must use a DIFFERENT mechanism (deal-local opp-signal gating, structural dispatch-wiring port, or board-texture-conditional fold) — NOT another single-literal floor edit.
- **v238**: SIBLING-GATE ALIGNMENT — when multiple fold gates (_multibarrel_line_fold, _aggro_bluffcatcher_should_fold, _rock_value_bet_fold) share a pot_odds floor, editing ONE creates an inconsistent exploit surface. Lower ALL sibling gates consistently with over-fold evidence, or gate the widened band on opp signals rather than firing unconditionally.
- **v238 归档建议**: Close v235's real leak vs v208/v205/v184 (value-heavy multi-barrel, 40-50% pot) by porting the FULL v206 fold-gate stack to all 4 strategy.py dispatch sites and aligning all three sibling gates — then validate at ≥30g with bet-sizing telemetry localizing the leak to the target band before committing.
- **v237**: Exhausted axis LOCK — `_estimate_bluff_frequency` underbettor floor attempted v219/v221/v235/v237 with ZERO ≥30g WR-lift. Master MUST NOT select this axis. Higher-EV path = port the FULL _multibarrel_line_fold/_aggro_bluffcatcher_should_fold/_rock_value_bet_fold stack (4 dispatch sites) from v195/v215.
- **v237**: INERT-MUTATION TRAP 4th recurrence — betsize_polarity confidence is structurally 0 or ≥0.333, never [0.20,0.25); replace ALL confidence thresholds with sample_count≥6 before further calibration.
- **v236**: strategy.py=2485/2500 LOC (15-line headroom) — next strategy.py edit MUST reclaim LOC first; binding constraint on offense-axis evolution.
- **v236**: CROSSOVER FRAGILITY — validated-pending fns silently lost across crossovers (`_semibluff_raise_construct` absent v234/v235, restored v236). Master MUST grep current bot for previously-validated mechanism names before assuming 'new'.
