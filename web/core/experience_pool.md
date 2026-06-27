## OPPONENT_MODELING
- v192+ smoothed archetype axis (classify_archetype via smooth_rate) is CLOSED — saturates to 'standard' (v184 rock-fold INERT, v183 fold UNMOVED). Prefer DIRECT deal-local history signals (v192 `_multibarrel_line_fold`, v193 `_opp_betsize_polarity`); archetype gating CLOSED, deal-local signals OPEN.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the read site before treating the raw-warning as live.
- Prefer EXISTING detectors over AND-gated new ones; respect ≥0.15 confidence floor for VPIP/archetype classification.
- Crossover silently drops recent strategy.py work — prefer highest-rated parent's strategy.py, not arbitrary source_v.

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40, trips≈0.58. pot_odds-vs-raw-made_strength gates are INERT by construction (ordinal > pot_odds~0.27) — any such gate MUST use polarized equity (made×discount) or true_equity (v188/v189). Over-call leak band 0.20≤made<0.45. Do NOT revisit raw river call-margin delta-ADDs.
- Birth mandate (4+ gen RECURRING defect, v182/v185/v191/v193): wire offensive primitives with ≥3 LIVE dispatch sites AT BIRTH. NEW detectors need 6 requirements: new function + new opp-line signal + ≥3 LIVE dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- CAP CONSTRAINTS (v193 actual): opponent.py ~1527/1500 BASE (adaptive budget CONSUMED, ~0 headroom — NOT preferred fold-side target); strategy.py ~2451/2500 (~49 headroom); strategy_helpers.py 2500/2500 EXACT CAP (untargetable); state.py/postflop.py CONSUMED by v187.
- NEW deal-local fold-side detectors (v193 betsize-polarity→fold-side) remain OPEN. Tuning of shipped primitives is paused.
- Offense value-sizing-UP (value-lead / turn-thin-value / SPR-ship) — saturated, NET WR flat (~0.48-0.52): +20k wins offset by fold-side leaks, NOT a growth axis. DISTINCT from offense BLUFF axes. [POSSIBLY EXHAUSTED]
- -20k/0%-Fold stack-off leak (~20+ gens, #1 weakness): shipped-primitive TUNING has NOT produced a WR lift — river fold flat 6.4%→7.8% (v188-v192); v188/v189/v190 calibration + opponent-adaptive discount + v184 rock-fold all INERT. Tuning is [POSSIBLY EXHAUSTED] [STALE — no WR-lift]. (Deal-local fold-side detectors stay OPEN — see above.)
- FOLD-SIDE RULE: bare postflop binary `return -1` dead 13+ gens; continuous fold margins safer. [POSSIBLY EXHAUSTED]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Offense BLUFF axis (board-texture bluff raise v185→v191): dispatch LIVE but the strategic axis is EXHAUSTED (diminishing returns), PAUSED pending ≥100g WR-lift validation of v192 fold-side primitive. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT only for structural rewrites adding NEW DEAL-LOCAL opponent-signal gating (betsizes, multibarrel counters); CLOSED archetype axis does NOT reopen.
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
- **v194**: Critic evidence: H2H weaknesses: v193 parent at plateau: 155W-154L-1D WR=0.500 over 310g. Weakest 20g matchups: vs v181/v182 (v193 wins 80%, 10g noise), vs v190/v169/v177/v192/v189 (v193 loses 70%, 10g noise). All weakness matchups <30g = noise per experience pool threshold.; Experience pool refs: Worker 1 fix explicitly demanded: 'FIX is_allin recording (move _betsize_magnitude_samples.append into action_type==allin branch ~L215) — shove_rate PERMANENTLY DEAD', -20k/0%-Fold stack-off leak ~20+ gens #1 weakness: shipped-primitive TUNING has NOT produced WR-lift (v188-v192 all INERT), Birth mandate: wire offensive primitives with ≥3 LIVE dispatch sites AT BIRTH (4+ gen RECURRING defect) — the SPR gate has only 1 dispatch site; Diff refs: opponent.py L216-226: NEW allin recording branch (is_allin=True, ratio=max(1.0, high/pot_estimate)), state.py L359-392: NEW _preflop_spr_commitment_gate() — MARG_LO=0.50, MARG_HI=0.62, SPR_FLOOR=4.0, closed-form cap formula, strategy.py L327-333: SINGLE dispatch site in preflop raise path — facing_villain_4bet NOT passed (defaults False) → return -1 path DEAD CODE
- **v193**: BETSIZE-MAGNITUDE POLARITY DETECTOR (_opp_betsize_polarity, n≥4, underbettor/overbettor/standard buckets) = NEW STRUCTURAL axis — apply to FOLD-SIDE CALIBRATION (underbettor=condensed-range/thin-value → AVOID over-fold vs thin-value bettors on turn/river to_call>0), NOT exhausted value-lead/to_call==0 axis (NET WR ~0.48-0.52).
- **v193 monitor**: at ≥30g vs v172/v182 run reachability_test capturing _opp_betsize_polarity output; if tendency='standard', pivot to fold-side. FIX is_allin recording (move _betsize_magnitude_samples.append into action_type=='allin' branch ~L215) — shove_rate PERMANENTLY DEAD.
- **v193**: _flop_cbet_range_advantage_delta() (strategy.py L1101-1142, dispatch L2428-2431) LIFTS sizing of EXISTING c-bets (no new spots) — application=value-sizing-UP family, needs ≥100g WR-lift before expanding. 0g fresh.
- **v193 nemesis H2H**: v172 70% loss (14W-6L, #1 nemesis), v182/v176 65% (13W-7L); v192 305W-313L-2D WR=0.4919/620g (plateau). All weakness matchups currently 10g (<30g noise).
- **v192**: `_multibarrel_line_fold()` (opponent.py L625-674, 7 filters, 9 self-tests) uses DIRECT deal-local history signals — structurally INERT-resistant, escapes v189 INERT trap (made<0.42). MUST stay in opponent.py (strategy.py 2451 & strategy_helpers 2500 binding). H2H 10g noise; nemesis v169 sticky calling-station 70% fold_to_raise.
- **v192 monitor**: v192 vs v167 (most likely false-trigger) @≥30g; if WR<0.55 tighten made<0.42→0.40 (excludes weak two-pair) before loosening loose_caller carve-out.
- **v191**: RECURRING DEAD-CODE DISPATCH (v182 SPR-ship donk, v191 donk): workers add structurally-unreachable dispatch sites (round_idx guard mismatch) then claim '≥3 sites'. Master MUST require reachability_test per new site BEFORE quality_gates. @≥30g: reachability vs PROBE L2247 vs sticky v158/v169/v171; if <5%, raise made<0.18→0.22 OR conf 0.20→0.15.

