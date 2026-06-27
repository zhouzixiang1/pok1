## OPPONENT_MODELING
- v192+ smoothed archetype axis (classify_archetype via smooth_rate) is CLOSED — saturates to 'standard' (v184 rock-fold INERT, v183 fold UNMOVED). Prefer DIRECT deal-local history signals (v192 `_multibarrel_line_fold`, v193 `_opp_betsize_polarity`); archetype gating CLOSED, deal-local signals OPEN.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the read site before treating the raw-warning as live.
- Prefer EXISTING detectors over AND-gated new ones; respect ≥0.15 confidence floor for VPIP/archetype classification.
- Crossover silently drops recent strategy.py work — prefer highest-rated parent's strategy.py, not arbitrary source_v.

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40, trips≈0.58. pot_odds-vs-raw-made_strength gates are INERT by construction (ordinal > pot_odds~0.27) — any such gate MUST use polarized equity (made×discount) or true_equity (v188/v189). Over-call leak band 0.20≤made<0.45. Do NOT revisit raw river call-margin delta-ADDs.
- Birth mandate (4+ gen RECURRING defect v182/v185/v191/v193): wire offensive primitives with ≥3 LIVE dispatch sites AT BIRTH. NEW detectors need 6 requirements: new function + new opp-line signal + ≥3 LIVE dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- CAP CONSTRAINTS (v194 actual): opponent.py ~1538/1500 BASE (adaptive budget CONSUMED, ~0 headroom — NOT preferred fold-side target); strategy.py ~2463/2500 (~37 headroom); strategy_helpers.py 2500/2500 EXACT CAP (untargetable); state.py/postflop.py CONSUMED by v187.
- NEW deal-local fold-side detectors: v193 betsize_polarity INFRASTRUCTURE now LIVE (v194 fixed is_allin → shove_rate records); only the fold-side PAYOFF remains OPEN. Tuning of shipped primitives is paused.
- Offense value-sizing-UP (value-lead / turn-thin-value / SPR-ship / c-bet-range-advantage v193) — saturated, NET WR flat (~0.48-0.52): +20k wins offset by fold-side leaks, NOT a growth axis. Pause expansion (incl. v193 _flop_cbet_range_advantage_delta). [POSSIBLY EXHAUSTED]
- -20k/0%-Fold stack-off leak (~20+ gens, #1 weakness): shipped-primitive TUNING has NOT produced a WR lift — river fold flat 6.4%→7.8% (v188-v192); v188/v189/v190 calibration + opponent-adaptive discount + v184 rock-fold all INERT. Tuning [POSSIBLY EXHAUSTED] [STALE — no WR-lift]. (Deal-local fold-side detectors + the v194 facing_villain_4bet path stay OPEN.)
- FOLD-SIDE RULE: bare postflop binary `return -1` dead 13+ gens; continuous fold margins safer. [POSSIBLY EXHAUSTED]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Offense BLUFF axis (board-texture bluff raise v185→v191): dispatch LIVE but the strategic axis is EXHAUSTED (diminishing returns), PAUSED pending ≥100g WR-lift validation. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT only for structural rewrites adding NEW DEAL-LOCAL opponent-signal gating (betsizes, multibarrel counters, facing_villain_4bet path); CLOSED archetype axis does NOT reopen.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- Firing verification: reachability_test + ≥100g H2H WR-lift is the ONLY reliable gate. stderr NOT readable (battle.py _PersistentBot stdout-only; v163 blindspot ACTIVE); ≥30g daemon-grep "fired≥5%" UNFULFILLABLE — substitute reachability_test.

## GENERAL
- Master is RELIABLE at plan-generation but reliability ≠ correctness: validate the axis PAYLOAD (≥100g WR-lift), not just plan cleanliness.
- Dead-code/guard removal > adding constants — logic fixes yield higher EV per line than margin tweaks.
- Validation thresholds: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Workers MUST grep their own dispatch sites + run reachability_test to confirm every param/signal gates an outcome before claiming coverage. [migrated advisory]
- Plateau (WR ~0.50): pursue a new structural axis, NOT tighter margins.
- If ALL active axes are validation-paused (fold-side tuning=v192 pending; offense-bluff=paused; value-sizing-UP + choose_raise constants=exhausted), the ONLY permitted direction is a NEW deal-local opponent-history detector axis not yet attempted — do NOT re-tune blocked axes.

## RECENT_LESSONS
- **v195**: Critic evidence: H2H weaknesses: v195 has 0 H2H games (fresh). v193 parent's weakest matchups are all <30g noise: v173 30%, v176 30%, v181/v182 20%, v189/v190/v192 70% loss. v194 vs v193 = 6-4 at 10g (noise). No ≥30g paired evidence supports a specific nemesis being an 'underbettor'.; Experience pool refs: POSTFLOP_STRATEGY: 'NEW deal-local fold-side detectors: v193 betsize_polarity INFRASTRUCTURE now LIVE (v194 fixed is_allin → shove_rate records); only the fold-side PAYOFF remains OPEN. Tuning of shipped primitives is paused.' — v195 implements this OPEN payoff., RECENT_LESSONS v193: 'apply to FOLD-SIDE CALIBRATION (underbettor=condensed/thin-value → AVOID over-fold vs thin-value bettors on turn/river to_call>0), NOT the exhausted value-lead/to_call==0 axis.' — exact mandate implemented., RECENT_LESSONS v194 monitor: 'verify via reachability whether v172/v173/v174 now classify overbettor with shove_rate unblocked, else betsize_polarity stays INERT vs the actual nemeses.' — UNADDRESSED by v195 (only synthetic-input self-tests, no live-classification reachability).; Diff refs: opponent.py L957-960: `if (_polarity.get('confidence', 0.0) >= 0.25 and _polarity.get('tendency') == 'underbettor'): base = max(base, 0.30)` — the fold-side calibration floor, opponent.py L221-234: allin-recording bugfix with hard-coded ratio=2.0 (vs v194's actual ratio), opponent.py L939-942: PRE-EXISTING _opp_bluff_prone floor at 0.30 — same magnitude, different signal (composite bluff-prone vs betsize polarity). Overlap may be redundant for opponents triggering both.
- **v194**: Derived-stat recording bug — checking action_type=='allin' INSIDE the raise branch makes shove_rate permanently 0; verify each action_type discriminator lives in its own branch, not nested under a sibling branch.
- **v194**: A function parameter never passed True at its only call site is dead code even if 'forward-looking' — require a second LIVE dispatch site before claiming a behavior is wired (e.g. _preflop_spr_commitment_gate's facing_villain_4bet).
- **v194 归档建议**: Wire the dormant facing_villain_4bet=True path in _preflop_spr_commitment_gate (detect opp_round_raises>=2 preflop in strategy.py choose_raise) — folding marginal hands (22-66, AQo/AJo/KQo, the actual MARG_HI=0.62 band) to a 4-bet is the highest-EV part of this gate and a direct attack on the 20+-gen -20k/0%-fold leak; also verify via reachability whether v172/v173/v174 now classify 'overbettor' with shove_rate unblocked, else betsize_polarity stays INERT vs the actual nemeses.
- **v194**: Critic evidence — v193 parent at plateau 155W-154L-1D WR=0.500 over 310g; all weakest 20g matchups (v190/v169/v177/v192/v189 ~70% loss) are <30g = noise.
- **v193**: _opp_betsize_polarity (n≥4, underbettor/overbettor/standard buckets) = NEW STRUCTURAL axis — apply to FOLD-SIDE CALIBRATION (underbettor=condensed/thin-value → AVOID over-fold vs thin-value bettors on turn/river to_call>0), NOT the exhausted value-lead/to_call==0 axis.
- **v193 monitor**: @≥30g vs v172/v182 run reachability_test capturing _opp_betsize_polarity output; if tendency='standard', pivot to fold-side. (is_allin fix now DONE in v194.)
- **v192**: `_multibarrel_line_fold()` (opponent.py L625-674, 7 filters, 9 self-tests) uses DIRECT deal-local history signals — structurally INERT-resistant, escapes v189 trap (made<0.42). MUST stay in opponent.py. H2H 10g noise; nemesis v169 sticky calling-station 70% fold_to_raise.
- **v192 monitor**: v192 vs v167 (most likely false-trigger) @≥30g; if WR<0.55 tighten made<0.42→0.40 (excludes weak two-pair) before loosening loose_caller carve-out.

