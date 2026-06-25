## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors > AND-gated new detectors; respect ≥0.15 confidence floor for VPIP/archetype classification.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the read site before treating the raw-warning as live.
- Archetype-gated folds load-bearing ONLY if adversary classifies into the bucket — capture a real `classify_archetype()` snapshot at ≥30 hands; a fold on a 'standard' opp never fires (v184 rock-fold INERT). [migrated advisory]
- Crossover silently drops recent strategy.py work — prefer highest-rated parent's strategy.py, not arbitrary source_v.

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40, trips≈0.58. Over-calling leak at 0.20≤made<0.45; 0.45-0.55 sparse — verify band edges before committing.
- Birth mandate (4+ gen RECURRING defect): wire offensive primitives with ≥3 dispatch sites (donk/probe/choose_raise) AT BIRTH — NOT closable by worker-review. v185/v186 RE-OFFENDED at 1 site; donk/probe still unwired.
- NEW detectors need 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- v186 refactor: `_postflop_response_margin()` (opponent.py) unifies 16 scattered call_margin additives. Substitution point now OCCUPIED by v188 pot-odds-vs-equity framework (no longer deferred/live-TODO).
- CAP CONSTRAINTS: strategy.py 2370/2500 (~130 headroom); opponent.py 945 (ample); strategy_helpers.py 2500/2500 EXACT CAP (untargetable). state.py/postflop.py foundation-primitive axis is a live untagged lever.
- -20k / 0%-Fold stack-off leak: 19+ gens, FIRST-ATTACKED by v188 (pot-odds-vs-equity river fold at the substitution point). Post-v188: NO -20k loss in any matchup (worst -14735), leak tail SHRINKING, offense preserved (+19940/+19712/+16955); needs ≥100g validation. preflop-trash axis RE-OPENED by v187's corrected equity. River-guard relocation FALSIFIED; v184 rock-fold INERT; v180 turn-board-danger UNVALIDATED.
- FOLD-SIDE RULE: postflop binary `return -1` folds dead 13+ gens; v183 PARTIALLY reactivated via archetype+EV gating; bare binary folds need archetype+EV gating and stay PARTIAL; continuous fold margins safer. [POSSIBLY EXHAUSTED]
- Offense value-sizing UP (value-lead upsizing / turn thin-value / SPR value-ship) — family saturated, no WR lift (~0.48-0.52 flat); strategy.py full. DISTINCT from offense BLUFF axes (see BLUFF_CALIBRATION). [POSSIBLY EXHAUSTED]
- River call-margin delta-ADDs [STALE — no WR-lift]; 0.20-0.45 band narrowing already satisfied by v180. Framework-REPLACEMENT is DONE (v188), not live.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Offense-side BLUFF axes (board-texture bluff raise etc.) are LIVE and DISTINCT from the exhausted value-sizing-UP family — do NOT conflate the two. Add empirical floor fold_to_raise≥0.40 before the confidence≥0.20 gate fires to avoid bluffing sticky opps (v185 single-site risk vs nemesis v169).

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- Firing verification: reachability_test + ≥100g H2H WR-lift is the ONLY reliable gate. stderr NOT readable (battle.py _PersistentBot stdout-only; v163 blindspot ACTIVE); ALL ≥30g daemon-grep "fired≥5%" steps UNFULFILLABLE — substitute reachability_test, do NOT block on daemon-grep.

## GENERAL
- Master RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness.
- Dead-code/guard removal > adding constants — logic fixes (e.g. anti-lock bypass guard removal) yield higher EV per line than margin tweaks.
- Validation thresholds: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Workers MUST grep their own function body to confirm every param/signal gates an outcome. [migrated advisory]
- Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit — when H2H shows 45-55%, the correct move is a new structural axis (offense/texture/archetype/foundation), not tighter margins.

## RECENT_LESSONS
- **v189**: Critic evidence: H2H weaknesses: v188 has only 120 games at 50% WR (insufficient sample); targets the 19+-gen #1 leak (-20k/0%-fold) confirmed across experience_pool + v188 memory: 'NO -20k loss in ANY matchup (worst -14735), leak tail SHRINKING'. The leak band is river marginal made hands facing >=0.5x pot bets — exactly what this gate targets.; Experience pool refs: 'River call-margin delta-ADDs [STALE]; Framework-REPLACEMENT is DONE (v188), not live.' — v189 is the FIRST calibration of that framework, not a delta-add repeat. 'FOLD-SIDE RULE: continuous fold margins safer' — v189 keeps continuous. v188 memory critic mandate #1: 'made_strength is HAND-CLASS score... NOT true equity vs polarized range... may OVER-FOLD vs bluff-heavy STANDARD opponents... replace proxy with true_equity = bluff_freq + (1-bluff_freq)*made_strength' — v189 addresses the calibration half with a simpler scalar discount.; Diff refs: opponent.py L782-784: `calibrated_equity = made_strength * POLARIZATION_DISCOUNT; equity_gap = pot_odds - calibrated_equity` (the core INERT fix). L745/L756: MIN_LEAK_BET_RATIO=0.40 (floor lowered from 0.50). L741 comment explicitly frames 0.65 as tunable 0.60-0.70. reachability_test.py adds dead-band regression guard L1058-1068 asserting made=0.30@0.6x now fires (was DEAD in v188).
- **v188**: Pot-odds-vs-equity framework LIVE at `_postflop_response_margin` — substitution point OCCUPIED. Future fold-side work must TUNE target_threshold=0.48 (CONSTANT A) or made-band 0.20-0.50, NOT add new margins; delta-add family exhausted and this framework is the replacement.
- **v188**: Leak band 0.20-0.50 STACKS additively with v177 _weak_one_pair_river_margin (0.20-0.55) — combined ~0.30. If over-folding at ≥100g, compute true_equity = bluff_freq + (1-bluff_freq)*made_strength rather than treating made_strength (hand-class score: pair=0.22, two-pair=0.40) as calibrated equity vs a polarized range.
- **v188**: NO -20k loss in any matchup post-shipping (worst -14735); leak tail SHRINKING, offense preserved (+19940/+19712/+16955). Verify H2H vs v165/v167 (calling-station archetype) at ≥100g; v187 overall WR=0.4478/230g (net loser), so validation matters.
- **v187**: Foundation-primitive correctness can unlock stuck axes — estimate_preflop_strength mis-ranking silently corrupted opp range modeling/raise-sizing/postflop calibration for generations; verify equity/strength INPUTS before declaring a lever dead. Re-opens preflop-trash axis.
- **v187 (scale-shift risk)**: 9+ downstream preflop thresholds + simulation.py combo_range_weight inherit new scale without per-site verification; L710 sb_vs_iso_raise floor 0.34 nearly INERT; L685 limp-reraise floor 0.58 admits marginal QJo/KJo — need empirical validation.
- **v186 (refactor DONE)**: `_postflop_response_margin()` unifies 16 call_margin lines; strategy.py 2436→2370, opponent.py 879→945. v188 framework-replacement was its explicit PURPOSE.
- **v186 (birth-defect persists)**: `_board_texture_bluff_raise` still 1/3 dispatch sites (strategy.py L1981; donk/probe unwired). Next offense-axis gen MUST wire all three AT BIRTH; ~130 LOC headroom.

