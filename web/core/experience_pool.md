## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors > AND-gated new detectors; respect ≥0.15 confidence floor for VPIP/archetype classification.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the read site before treating the raw-warning as live.
- Archetype-gated folds load-bearing ONLY if adversary classifies into the bucket — capture real `classify_archetype()` snapshot at ≥30 hands; a fold on a 'standard' opp never fires (v184 rock-fold INERT). [migrated advisory]
- Crossover silently drops recent strategy.py work — prefer highest-rated parent's strategy.py, not arbitrary source_v.

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40, trips≈0.58. Over-calling leak at 0.20≤made<0.45; 0.45-0.55 sparse — verify band edges before committing.
- Birth mandate (4+ gen RECURRING defect): wire offensive primitives with ≥3 dispatch sites (donk/probe/choose_raise) AT BIRTH. v182 closed SPR-ship donk/probe coverage; OPEN single-site defect is v185 `_board_texture_bluff_raise` (L1981 only) — do NOT re-wire already-wired primitives.
- NEW detectors need 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- v186 refactor: `_postflop_response_margin()` (opponent.py) unifies 16 scattered call_margin additives; v188 pot-odds-vs-equity framework is its replacement (substitution point OCCUPIED).
- CAP CONSTRAINTS: strategy.py ~2390/2500 (~110 headroom); opponent.py ~1173/1500 (ample, post-v190); strategy_helpers.py 2500/2500 EXACT CAP (untargetable); state.py/postflop.py foundation axis CONSUMED by v187.
- -20k/0%-Fold stack-off leak (19+ gens): FIRST-ATTACKED by v188, CALIBRATED by v189/v190 (opponent-adaptive discount). Post-ship NO -20k loss (worst -14735), leak tail SHRINKING; needs ≥100g validation. FAILED/INERT sub-directions: river-guard relocation FALSIFIED, v184 rock-fold INERT, v180 turn-board-danger UNVALIDATED.
- FOLD-SIDE RULE: bare postflop binary `return -1` folds dead 13+ gens; v183 PARTIALLY reactivated via archetype+EV gating; continuous fold margins safer. [POSSIBLY EXHAUSTED]
- Offense value-sizing UP (value-lead upsizing / turn thin-value / SPR value-ship) — family saturated, NET WR flat (~0.48-0.52): preserved +20k wins are OFFSET by fold-side leaks, so NOT a growth axis. DISTINCT from offense BLUFF axes (see BLUFF_CALIBRATION). [POSSIBLY EXHAUSTED]
- River call-margin delta-ADDs [STALE — no WR-lift] — framework REPLACED by v188; do-not-revisit guardrail only.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Offense-side BLUFF axes (board-texture bluff raise etc.) are LIVE and DISTINCT from the exhausted value-sizing-UP family — do NOT conflate. Add empirical floor fold_to_raise≥0.40 BEFORE confidence≥0.20 gate to avoid bluffing sticky opps (v185 single-site risk vs nemesis v169).

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT only for structural rewrites adding NEW opponent-signal gating; does NOT re-open the saturated value-sizing-UP family.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- Firing verification: reachability_test + ≥100g H2H WR-lift is the ONLY reliable gate. stderr NOT readable (battle.py _PersistentBot stdout-only; v163 blindspot ACTIVE); ≥30g daemon-grep "fired≥5%" steps UNFULFILLABLE — substitute reachability_test.

## GENERAL
- Master RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness.
- Dead-code/guard removal > adding constants — logic fixes yield higher EV per line than margin tweaks.
- Validation thresholds: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Workers MUST grep their own function body to confirm every param/signal gates an outcome. [migrated advisory]
- Plateau (WR ~0.50 all matchups, no single dominant exploit): when H2H shows 45-55%, pursue a new structural axis (offense/texture/archetype/foundation), NOT tighter margins.

## RECENT_LESSONS
- **v191**: RECURRING DEAD-CODE DISPATCH PATTERN (v182 SPR-ship donk site, v191 donk site): workers add dispatch sites that are structurally unreachable due to round_idx guard mismatches, then claim '>=3 sites' mandate met. Master MUST require worker to verify each new dispatch site fires via reachability_test BEFORE quality_gates — 'sites on paper' is not the same as 'live firing sites'. Carry this forward to ALL future multi-dispatch mandates.
- **v191**: Offense-bluff calibration axis (v185 birth, v190 floor, v191 probe) is now WELL-EXPLORED with diminishing returns; the -20k/0%-fold leak at to_call>0 river/turn has been UNTOUCHED for 19+ generations despite being the documented #1 weakness. Future Master should FORBID further offense-bluff additions until fold-side primitive lands.
- **v191 归档建议**: At >=30g, run reachability_test against the PROBE dispatch site (strategy.py L2247, the only genuinely new firing site) vs sticky calling-station opponents v158/v169/v171 — if firing<5%, raise the made_strength<0.18 ceiling to <0.22 OR lower the confidence floor 0.20->0.15; if WR vs v158 (currently 3-7 for v190) does NOT improve, the fold_to_raise<0.40 floor is too conservative and should drop to 0.35 since the existing +EV gate (fold_equity>bet/(pot+bet)) is already a sufficient guard.
- **v191**: Critic evidence: H2H weaknesses: v190 H2H all 10g samples (<30g noise threshold per experience_pool) — no matchup can be cited as a confirmed weakness. v172 at 0.2 (10g) is sub-noise; nemesis v169 referenced in experience pool mandate is the strategic basis.; Experience pool refs: BLUFF_CALIBRATION: 'Offense-side BLUFF axes are LIVE and DISTINCT from the exhausted value-sizing-UP family — do NOT conflate. Add empirical floor fold_to_raise>=0.40 BEFORE confidence>=0.20 gate to avoid bluffing sticky opps (v185 single-site risk vs nemesis v169).' — EXACTLY fulfilled by v191., POSTFLOP_STRATEGY birth mandate: 'OPEN single-site defect is v185 _board_texture_bluff_raise (L1981 only) — do NOT re-wire already-wired primitives.' — v191 adds 2 dispatch sites (1 live + 1 inert).; Diff refs: opponent.py L648-651: fold_to_raise<0.40 floor added BEFORE the conf<0.20 gate (correct ordering per experience_pool mandate)., strategy.py L2247-2253: probe-path dispatch is the genuinely new firing site (probe fires round_idx 2/3 to_call==0 = exactly the _board_texture_bluff_raise live window)., strategy.py L2190-2196: donk-path dispatch is structurally unreachable — donk is flop-only (_DONK_ROUND=1) but guard requires round_idx in (2,3); worker comment explicitly discloses 'currently inert on flop'.
- **v190**: Opponent-adaptive EV-gate discount — `made×(0.40+bluff_freq)` (opponent.py L706-759/L795-796), anchored at standard bluff_freq=0.25 so majority of matchups are byte-identical to v189 → eliminates regression risk while tuning extreme-opp behavior. Pattern generalizes to future river fold-gates. Overbettor exemption (bypass) preserved.
- **v190 KEY RISK**: discount only matters for ~30-40% non-standard opps (calling_station/rock/aggro conf≥0.15); if most classify 'standard' it's cosmetic. Over-fold WATCH vs standard @≥100g — if WR<0.45 replace scalar with `true_equity = bluff_freq+(1-bluff_freq)*made_strength` (that yields 0.37-0.58 @made=0.30 → gate never fires, so v190 keeps gate LIVE as a defensible engineering choice). Needs real classify_archetype() snapshots @≥30h vs v171/v160 (v189 worst matchups) — but those v189 weaknesses are 10g samples (<30g=noise threshold), unconfirmed.
- **v189**: EV-gates comparing pot_odds vs made_strength are INERT by construction — made_strength is a hand-class ordinal (pair≈0.22/two-pair≈0.40/trips≈0.58) that almost always exceeds pot_odds (~0.27). Any pot-odds-vs-equity fold gate MUST use polarized-range-adjusted equity (made×discount) or derived true_equity, never the raw ordinal. v189 calibrated v188: POLARIZATION_DISCOUNT=0.65, MIN_LEAK_BET_RATIO 0.50→0.40.
- **v188**: Pot-odds-vs-equity framework LIVE at `_postflop_response_margin` — substitution point OCCUPIED. Future fold-side work TUNES target_threshold=0.48 or made-band 0.20-0.50; do NOT add new margins (delta-add family exhausted). Post-ship NO -20k loss any matchup (worst -14735), leak tail SHRINKING, offense preserved; needs ≥100g validation (v187 overall WR=0.4478/230g net loser → validation matters).


