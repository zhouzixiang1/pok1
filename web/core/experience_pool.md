## OPPONENT_MODELING
- Archetype/classify_archetype() gates load-bearing ONLY if adversary classifies into the bucket; smooth_rate prior-saturation returns 'standard' for most opps (v184 rock-fold INERT). Capture real snapshot at ≥30h BEFORE more archetype-axis work. v192+ AVOIDS classify_archetype, using direct history signals instead.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the read site before treating the raw-warning as live.
- Archetype-suppression gates on EXISTING detectors > AND-gated new detectors; respect ≥0.15 confidence floor for VPIP/archetype classification.
- Crossover silently drops recent strategy.py work — prefer highest-rated parent's strategy.py, not arbitrary source_v.

## POSTFLOP_STRATEGY
- Made-strength table (authoritative): pair≈0.22, two-pair≈0.40, trips≈0.58. pot_odds-vs-made_strength comparisons are INERT by construction (ordinal almost always > pot_odds~0.27) — any such gate MUST use polarized equity (made×discount) or true_equity, never raw ordinal (v189/v188). Over-call leak band 0.20≤made<0.45; 0.45-0.55 sparse.
- Birth mandate (4+ gen RECURRING defect): wire offensive primitives with ≥3 LIVE dispatch sites AT BIRTH. v185 `_board_texture_bluff_raise` still NOT closed — only 2 LIVE sites (choose_raise + probe L2247); donk L2190 is DEAD CODE (should_donk_bet FLOP-only vs round_idx∈{2,3}). Needs 1 more LIVE site.
- NEW detectors need 6 BIRTH REQUIREMENTS: new function + new opp-line signal + ≥3 LIVE dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- v186 `_postflop_response_margin()` (opponent.py) unifies 16 scattered call_margin additives; v188 pot-odds-vs-equity framework replaced it (substitution point OCCUPIED).
- CAP CONSTRAINTS: strategy.py ~2410/2500 (~90 headroom); opponent.py ~1173/1500 (ample); strategy_helpers.py 2500/2500 EXACT CAP (untargetable); state.py/postflop.py foundation CONSUMED by v187.
- -20k/0%-Fold stack-off leak (~20+ gens, #1 weakness): fold-side IN FLIGHT via v188-v192 (pot-odds-vs-equity + opponent-adaptive discount + line-evidence fold); PAUSED on NEW fold-side experiments until ≥100g WR-lift validation. Post-ship NO -20k loss, tail SHRINKING. FAILED/INERT: river-guard relocation FALSIFIED, v184 rock-fold INERT, v180 turn-board-danger UNVALIDATED.
- FOLD-SIDE RULE: bare postflop binary `return -1` dead 13+ gens; v183 PARTIALLY reactivated via archetype+EV gating; continuous fold margins safer. [POSSIBLY EXHAUSTED]
- Offense value-sizing-UP (value-lead / turn-thin-value / SPR-ship) — saturated, NET WR flat (~0.48-0.52): +20k wins offset by fold-side leaks, NOT a growth axis. DISTINCT from offense BLUFF axes. [POSSIBLY EXHAUSTED]
- River call-margin delta-ADDs [STALE — no WR-lift] — REPLACED by v188; do-not-revisit guardrail only.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Offense BLUFF axis (board-texture bluff raise v185→v190→v191) LIVE & DISTINCT from exhausted value-sizing-UP family — do NOT conflate. WELL-EXPLORED, diminishing returns; PAUSED pending fold-side primitive (v192 line-evidence fold) landing + ≥100g validation. fold_to_raise≥0.40 floor-before-conf-gate INCORPORATED into v191. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT only for structural rewrites adding NEW opponent-signal gating; does NOT re-open value-sizing-UP family.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- Firing verification: reachability_test + ≥100g H2H WR-lift is the ONLY reliable gate. stderr NOT readable (battle.py _PersistentBot stdout-only; v163 blindspot ACTIVE); ≥30g daemon-grep "fired≥5%" UNFULFILLABLE — substitute reachability_test.

## GENERAL
- Master RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness.
- Dead-code/guard removal > adding constants — logic fixes yield higher EV per line than margin tweaks.
- Validation thresholds: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Workers MUST grep their own function body to confirm every param/signal gates an outcome. [migrated advisory]
- Plateau (WR ~0.50): pursue a new structural axis, NOT tighter margins.
- GRIDLOCK ESCAPE: fold-side (paused, validating v188-v192), offense-bluff (paused pending fold-side), value-sizing-UP + choose_raise constants (exhausted) — if all are validation-paused, the ONLY permitted direction is a NEW structural detector axis not yet attempted (novel opponent-history signal), NOT re-tuning blocked axes.

## RECENT_LESSONS
- **v192**: Critic evidence: H2H weaknesses: -20k/0%-fold stack-off leak: #1 weakness for 20+ gens (experience_pool L14); two-pair bluff-catchers (made~0.40, tier='strong') are the documented leak pattern; Experience pool refs: v192 RECENT_LESSONS (L41): 'NEW fold-side line-evidence axis — AVOIDS classify_archetype trap. Uses made<0.42 cutoff → escapes v189 INERT trap', OPPONENT_MODELING L2: 'v192+ AVOIDS classify_archetype, using direct history signals instead', GRIDLOCK ESCAPE L38: 'fold-side (paused, validating v188-v192)... ONLY permitted direction is a NEW structural detector axis'; Diff refs: opponent.py L625-709: NEW _multibarrel_line_fold() with 7 condition filters, strategy.py L1565-1568: dispatch in all-in block (critical -20k path), strategy.py L1607-1610: dispatch in to_call>0 block
- **v192**: NEW fold-side line-evidence axis `_multibarrel_line_fold()` (opponent.py L625-674, 7 condition filters, 9 self-tests) using DIRECT history signals (spot_info L1074/1079/1107/1111), AVOIDS classify_archetype trap. Uses made<0.42 cutoff (not pot_odds-vs-made comparison) → escapes v189 INERT trap. This IS the fold-side primitive that, on ≥100g validation, unblocks offense-bluff. H2H all 10g (<30g noise) — nemesis v169 sticky calling-station 70% fold_to_raise.
- **v191**: RECURRING DEAD-CODE DISPATCH (v182 SPR-ship donk, v191 donk): workers add structurally-unreachable dispatch sites (round_idx guard mismatch), claim '≥3 sites' met. Master MUST require reachability_test per new site BEFORE quality_gates. @≥30g: reachability vs PROBE L2247 (only genuinely new firing site) vs sticky v158/v169/v171 — if <5%, raise made<0.18→0.22 OR conf 0.20→0.15.
- **v190**: Opponent-adaptive EV-gate discount `made×(0.40+bluff_freq)` (opponent.py L706-759), anchored at standard bluff_freq=0.25 → byte-identical v189 majority, zero regression risk. KEY RISK: cosmetic if most classify 'standard'; over-fold WATCH @≥100g — if WR<0.45 vs standard, replace scalar with `true_equity=bluff_freq+(1-bluff_freq)*made_strength`.

