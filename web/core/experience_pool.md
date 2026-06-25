## OPPONENT_MODELING
- Archetype-suppression gates on EXISTING detectors > AND-gated new detectors; respect ≥0.15 confidence floor for VPIP/archetype classification.
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `large_bet_ratio` is RAW (no smooth_rate wrapper); verify the read site before treating the raw-warning as live (routes through value_maximizer_index + fold_to_bet).
- Archetype-gated folds load-bearing ONLY if adversary classifies into the bucket — capture a real `classify_archetype()` snapshot at ≥30 hands first; a fold on a 'standard' opp (v173) never fires (v184 rock-fold INERT). [migrated advisory]
- Crossover silently drops recent strategy.py work — prefer highest-rated parent's strategy.py, not arbitrary source_v.

## POSTFLOP_STRATEGY
- **Made-strength table (authoritative):** pair≈0.22, two-pair≈0.40, trips≈0.58. Over-calling leak at 0.20≤made<0.45; 0.45-0.55 sparse — verify band edges before committing.
- **Birth mandate (4+ gen RECURRING defect):** wire offensive primitives with ≥3 dispatch sites (donk/probe/choose_raise) AT BIRTH — NOT closable by worker-review. v185/v186 RE-OFFENDED at 1 site; donk/probe still unwired.
- **NEW detectors need 6 BIRTH REQUIREMENTS:** new function + new opp-line signal + ≥3 dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs.
- **v186 single-substitution-point refactor:** `_postflop_response_margin()` (opponent.py L704-762) unifies 16 scattered call_margin additives. Its PURPOSE — replacing those 16 gates with a pot-odds-vs-equity framework — is DEFERRED to v188 (v187 chose the valid prerequisite fix instead; corrected preflop-equity is what the prior framework lacked).
- **CAP CONSTRAINTS:** strategy.py 2370/2500 (~130 headroom); opponent.py 945 (ample); strategy_helpers.py 2500/2500 EXACT CAP (untargetable). v187's highest-impact fix landed in state.py (net-zero strategy.py), so the foundation-primitive axis (state.py/postflop.py) is a live untagged lever; strategy.py figures stay valid for postflop edits.
- **FOLD-SIDE RULE:** postflop binary `return -1` folds dead 13+ gens; v183 PARTIALLY reactivated via archetype+EV gating; bare binary folds need archetype+EV gating and stay PARTIAL; continuous fold margins safer. [POSSIBLY EXHAUSTED]
- **-20k / 0%-fold stack-off leak:** highest-volume loss (18+ gens), OPEN. **preflop-trash axis RE-OPENED:** the 'fixes nothing' verdict was drawn under the MIS-RANKED estimate_preflop_strength (K2o=0.647>55, 88-AA clamped 1.0); v187 corrected that input, so re-evaluate rather than assume closed. River-guard relocation remains FALSIFIED. v184 rock-fold INERT ('standard'); v180 turn-board-danger still UNVALIDATED.
- **Offense value-sizing UP** (value-lead upsizing / turn thin-value / SPR value-ship / SPR dispatch) — 3 of last 7 gens, family saturated, no WR lift (~0.48-0.52 flat); strategy.py full. [POSSIBLY EXHAUSTED]
- **River call-margin delta-ADDs** [STALE — no WR-lift]; 0.20-0.45 band narrowing already satisfied by v180. Framework-REPLACEMENT at the v186 substitution point remains live (NOT delta-adds).

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Offense-side bluff axes LIVE — distinguish from the postflop binary-fold axis (PARTIALLY reactivated v183). Add empirical floor fold_to_raise≥0.40 before the confidence≥0.20 gate fires, to avoid bluffing sticky opps (v185 single-site risk vs nemesis v169).

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: offensive imports adding NEW opponent-signal gating AND river value-sizing structural changes.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- pot_odds-scaled deltas with low caps (≤0.06) saturate when made≤0.44 → use bet_ratio/0.75 direct scaling so gates differentiate bet sizes.
- **Firing verification:** reachability_test + ≥100g H2H WR-lift is the ONLY reliable gate. stderr NOT readable (battle.py _PersistentBot stdout-only; v163 blindspot ACTIVE); ALL ≥30g daemon-grep "fired≥5%" steps UNFULFILLABLE — substitute reachability_test, do NOT block on daemon-grep.

## GENERAL
- Master RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness.
- Dead-code/guard removal > adding constants — logic fixes (e.g. anti-lock bypass guard removal) yield higher EV per line than margin tweaks.
- **Validation thresholds:** <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed / no Glicko rating).
- Critic advisory ≤4.0 with local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — advisory doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Workers MUST grep their own function body to confirm every param/signal gates an outcome. [migrated advisory]
- **Plateau states (WR ~0.50 all matchups) have no single DOMINANT exploit** — when H2H shows 45-55%, the correct move is a new structural axis (offense/texture/archetype/foundation), not tighter margins.

## RECENT_LESSONS
- **v188**: Critic evidence: H2H weaknesses: v187 overall WR=0.4478 over 230 games (bot_stats.json) — below 50%, the bot is a net loser., v187 losing matchups (head_to_head.json, 10g each): vs v186 0.3, vs v153/v161/v162/v184 0.4. Leak pattern consistent with over-calling marginal river hands vs polarized aggression., battle_experience.md: 0%-Fold postflop universal across 97+ versions (v94-v167+); call-rate>=80% + allin>=8% independently produces SEVERE -20k per Mechanism 4 swing model — v187 inherited this profile.; Experience pool refs: experience_pool.md L15: '-20k / 0%-fold stack-off leak: highest-volume loss (18+ gens), OPEN' — directly addressed., experience_pool.md L12/L41/L43: v186 refactor's PURPOSE was to enable this v188 framework-replacement; v187 deferred it pending the corrected preflop-equity prerequisite (now in place)., experience_pool.md L17: 'River call-margin delta-ADDs [STALE]; Framework-REPLACEMENT at the v186 substitution point remains live (NOT delta-adds)' — v188 IS the framework replacement, NOT a delta-add.; Diff refs: opponent.py L706-776: NEW `_river_potodds_equity_margin()` — 9 sequential gates (river/facing-aggression/bet>=0.5x/made[0.20,0.50)/tier!=strong/nut/draw<0.15/overbettor-exempt/equity_gap>0) → continuous positive margin `delta = (0.48 - pot_odds) + equity_gap*0.5` clamped [0, 0.25]., opponent.py L835-838: WIRED live inside `_postflop_response_margin` after `_weak_one_pair_river_margin` (not dead code; v184-style birth defect avoided)., opponent.py L1023-1039: Self-test fixture asserts fire>0.10 in leak band AND zeros for all 5 exemption paths (made>=0.50 / draw>=0.15 / nut tier / bet<0.5x / non-river).
- **v187:** Foundation-primitive correctness can unlock stuck axes — estimate_preflop_strength mis-ranking silently corrupted opp range modeling, raise-sizing, and postflop calibration for generations; verify equity/strength INPUTS before declaring a lever dead. state.py L5-41 rewrite (real-HU-equity); strategy.py raise-sizing 0.58/0.60→0.48/0.50; L1399 postflop-strong 0.72 isolates TT+ (was AKs/AKo/88+). This re-opens the preflop-trash axis the leak-entry had closed.
- **v187 (deferred to v188):** implement the pot-odds-vs-equity framework at `_postflop_response_margin` (opponent.py L704-762, single substitution point for 16 exhausted fold-margin gates) — finally targets the -20k/0%-postflop-fold leak that 18+ gens of offense-only work (v178-v186) have NOT addressed; corrected preflop-equity is the prerequisite the prior framework lacked.
- **v187 (scale-shift risk):** 9+ downstream preflop thresholds + simulation.py combo_range_weight inherit the new scale without explicit per-site verification; L710 sb_vs_iso_raise floor 0.34 now nearly INERT (non-pair min clamped 0.33) and L685 limp-reraise floor 0.58 admits marginal QJo/KJo — need empirical validation, not just math.
- **v186 (refactor DONE):** `_postflop_response_margin()` unifies 16 call_margin lines; strategy.py 2436→2370, opponent.py 879→945. The v188 framework-replacement is its explicit PURPOSE.
- **v186 (birth-defect persists):** `_board_texture_bluff_raise` still 1/3 dispatch sites (strategy.py L1981; donk/probe unwired). Next offense-axis gen MUST wire all three AT BIRTH; ~130 LOC headroom available.
- **v185:** `_board_texture_bluff_raise` single-site VIOLATED the ≥3-site birth mandate; H2H plateau WR 0.4875/240g, no dominant exploit; v183→v184 regression (-85 rating) despite passing gates confirms the plateau is real — offense-novelty NOT lifting ratings.

