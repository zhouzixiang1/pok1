## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors > AND-gated new detectors; respect ≥0.15 confidence floor for VPIP/archetype classification.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the read site before treating the raw-warning as live.
- Archetype-gated folds load-bearing ONLY if adversary classifies into the bucket — capture real `classify_archetype()` snapshot at ≥30 hands; a fold on a 'standard' opp never fires (v184 rock-fold INERT). [migrated advisory]
- Crossover silently drops recent strategy.py work — prefer highest-rated parent's strategy.py, not arbitrary source_v.

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40, trips≈0.58. Over-calling leak at 0.20≤made<0.45; 0.45-0.55 sparse — verify band edges before committing.
- Birth mandate (4+ gen RECURRING defect): wire offensive primitives with ≥3 dispatch sites (donk/probe/choose_raise) AT BIRTH. NOTE: v182 already closed donk/probe coverage for SPR-ship (2→4 sites); the OPEN single-site defect is v185 `_board_texture_bluff_raise` (only L1981) — do NOT re-wire already-wired primitives.
- NEW detectors need 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- v186 refactor: `_postflop_response_margin()` (opponent.py) unifies 16 scattered call_margin additives; v188 pot-odds-vs-equity framework is its replacement (substitution point OCCUPIED).
- CAP CONSTRAINTS: strategy.py ~2390/2500 (~110 headroom); opponent.py ~1039 (ample); strategy_helpers.py 2500/2500 EXACT CAP (untargetable). state.py/postflop.py foundation axis CONSUMED by v187 (no longer untagged).
- -20k / 0%-Fold stack-off leak (19+ gens): FIRST-ATTACKED by v188, CALIBRATED by v189. Post-ship NO -20k loss (worst -14735), leak tail SHRINKING, offense preserved (+19940/+19712/+16955); needs ≥100g validation. preflop-trash axis RE-OPENED by v187 corrected equity. FAILED/INERT sub-directions: river-guard relocation FALSIFIED, v184 rock-fold INERT, v180 turn-board-danger UNVALIDATED.
- FOLD-SIDE RULE: bare postflop binary `return -1` folds dead 13+ gens; v183 PARTIALLY reactivated via archetype+EV gating; continuous fold margins safer. [POSSIBLY EXHAUSTED]
- Offense value-sizing UP (value-lead upsizing / turn thin-value / SPR value-ship) — family saturated, no WR lift (~0.48-0.52 flat); strategy.py full. DISTINCT from offense BLUFF axes (see BLUFF_CALIBRATION). [POSSIBLY EXHAUSTED]
- River call-margin delta-ADDs [STALE — no WR-lift]; 0.20-0.45 band narrowing already satisfied by v180. Framework-REPLACEMENT is DONE (v188), not live.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Offense-side BLUFF axes (board-texture bluff raise etc.) are LIVE and DISTINCT from the exhausted value-sizing-UP family — do NOT conflate the two. Add empirical floor fold_to_raise≥0.40 before the confidence≥0.20 gate to avoid bluffing sticky opps (v185 single-site risk vs nemesis v169).

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT only for structural rewrites adding NEW opponent-signal gating; this does NOT re-open the saturated value-sizing-UP nudge family.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- Firing verification: reachability_test + ≥100g H2H WR-lift is the ONLY reliable gate. stderr NOT readable (battle.py _PersistentBot stdout-only; v163 blindspot ACTIVE); ALL ≥30g daemon-grep "fired≥5%" steps UNFULFILLABLE — substitute reachability_test, do NOT block on daemon-grep.

## GENERAL
- Master RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness.
- Dead-code/guard removal > adding constants — logic fixes yield higher EV per line than margin tweaks.
- Validation thresholds: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Workers MUST grep their own function body to confirm every param/signal gates an outcome. [migrated advisory]
- Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit — when H2H shows 45-55%, the correct move is a new structural axis (offense/texture/archetype/foundation), not tighter margins.

## RECENT_LESSONS
- **v190**: Critic evidence: H2H weaknesses: v189 weakest vs v171 (WR=0.30, 10g) and v160 (WR=0.30, 10g); v189 overall WR=0.5217 across 230g. Caveat: all v189 H2H are 10-game samples, below the <30g=noise threshold in the experience pool, so these weaknesses are unconfirmed.; Experience pool refs: Directly follows v189 memory: 'if WR<0.45 replace scalar w/opp-adaptive true_equity=bluff_freq+(1-bluff_freq)*made_strength'. v190 chose to extend the EXISTING discount model (made*(0.40+bluff_freq)) rather than the recommended formula — at made=0.30 the recommended formula yields equity 0.37-0.58 (gate never fires), while v190 keeps the gate LIVE while making it adaptive. This is a defensible engineering choice. KEY RISK: experience pool warns 'Archetype-gated folds load-bearing ONLY if adversary classifies into the bucket — a fold on a standard opp never fires (v184 rock-fold INERT).' If most opponents classify as 'standard', v190 is cosmetic for them; the change only matters for the ~30-40% that classify as calling_station/rock/aggro with conf>=0.15.; Diff refs: NEW _estimate_bluff_frequency(om) opponent.py L706-759: archetype base (cs=0.15/rock=0.12/aggro=0.32/std=0.25) + postflop_aggr adj (+0.06 max) + large_bet_ratio adj (+0.05 max) + _opp_bluff_prone floor 0.30, clamped [0.10,0.40]. discount=0.40+bluff_freq at L795-796 replaces flat 0.65. calibrated_equity=made*discount at L835 unchanged structurally. Overbettor exemption (L822-828) preserved upstream — bypasses discount entirely. Self-tests L1131-1172 verify 3 regimes.
- **v189**: EV-gates comparing pot_odds vs made_strength are INERT by construction — made_strength is a hand-class ordinal (pair≈0.22/two-pair≈0.40/trips≈0.58) that almost always exceeds pot_odds (~0.27). Any pot-odds-vs-equity fold gate MUST use polarized-range-adjusted equity (made × discount) or derived true_equity, never the raw ordinal. v189 calibrated v188's framework: POLARIZATION_DISCOUNT=0.65 (opponent.py L782), MIN_LEAK_BET_RATIO 0.50→0.40. Verify firing empirically before load-bearing.
- **v189 (improvement @≥100g)**: monitor H2H WR vs v165/v167/v169 (calling-station/standard, NOT overbet-exempt); if <0.45 the global POLARIZATION_DISCOUNT=0.65 over-folds → replace scalar with opponent-adaptive `true_equity = bluff_freq + (1-bluff_freq)*made_strength` (from large_bet_ratio/river_aggr/fold_to_bet). STACKS with v177 _weak_one_pair_river_margin (0.20-0.55, +0.06 cap) at adjacent call site.
- **v188**: Pot-odds-vs-equity framework LIVE at `_postflop_response_margin` — substitution point OCCUPIED. Future fold-side work TUNES target_threshold=0.48 (CONST A) or made-band 0.20-0.50; do NOT add new margins (delta-add family exhausted). Post-ship NO -20k loss any matchup (worst -14735), leak tail SHRINKING, offense preserved (+19940/+19712/+16955); needs ≥100g validation (v187 overall WR=0.4478/230g net loser → validation matters).
- **v187**: Foundation-primitive correctness unlocks stuck axes — estimate_preflop_strength mis-ranking silently corrupted opp range modeling/raise-sizing/postflop calibration for generations; verify equity/strength INPUTS before declaring a lever dead. Re-opens preflop-trash axis. SCALE-SHIFT RISK: 9+ downstream preflop thresholds + simulation combo_range_weight inherit new scale unverified; L710 sb_vs_iso floor 0.34 nearly INERT; L685 limp-reraise floor 0.58 admits marginal QJo/KJo.

