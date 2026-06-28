## OPPONENT_MODELING
- archetype axis CLOSED (saturates to 'standard'; v184 rock-fold INERT); ≥0.15 confidence floor, prefer deal-local history. Do NOT reopen.
- calldown_profile sample trap: foldy opps never reach n≥4 → empirical rate at n≥3, fall back to pool-wide fold_to_raise when per-street samples<2.
- `_opp_betsize_polarity` (n≥4): n_shove COUNT via True flag, NOT ratio=2.0 (ratio feeds only unread avg_fraction); deal-local signal feeding fold-calibration.
- `large_bet_ratio` is RAW (no smooth_rate wrapper) — verify read site before treating the raw-warning as live.
- v208 LANDED NEW deal-local `revealed_shove_density` detector (opp.py L127-154 per-hand twc-delta attribution; PRIMARY early-return in `_estimate_bluff_frequency` L954-986: shove_rate→[0.10,0.40] air-freq + revealed blend, shrinkage-cap 0.50) — FIRST consumption of shove_rate as PRIMARY discount driver. LANDED-BUT-UNVALIDATED (no firing test yet).
- 4bet trigger-inert PRINCIPLE: a reachable dispatch site can still NEVER fire if the TRIGGER is rare — reachability-test the TRIGGER, not just the path. v206 raised this on `_preflop_shove_defense_fold`; the proxy's reachability remains the live open question.

## POSTFLOP_STRATEGY
- Made-strength table (authoritative v205): pair≈0.22, two-pair≈0.40 [BOUNDARY/foldable post-v205, floor→0.45], trips≈0.58. pot_odds-vs-raw-made_strength STRUCTURALLY INERT (ordinal > pot_odds~0.27); MUST use polarized equity (made×discount) or true_equity. Over-call leak band 0.20≤made<0.45.
- **#1 MULTI-SITE -20k/0%-fold leak (v196→v208, ~12 gens RESILIENT)**: v206 closed preflop-half (raise-SIZE>=12BB trigger); v208 LANDED a NEW deal-local revealed_shove_density detector attacking it from the opponent-modeling axis (see OPPONENT_MODELING). POSTFLOP sibling-site (turn all-in round_idx==2/3) STILL bleeding (v206 -23165 vs v193; v207/v208 -16k/-17k vs v186/v174/v195) → ≥2-site leak. ⚠ Single consolidated next-move: if detector fires ≥5% @≥30g AND -20k persists, attack preflop-4bet/turn-allin SIBLING sites via crossover-bypass or keyword-purge reframe — fold-side GATE attacks are CROSS_GEN_PIVOT-BLOCKED (dir-audit forbids fold-port ~8 gens: v198/v199/v200-abandon/v207), NOT plain master+workers. BINDING GATE: reachability-test @≥30g before ANY threshold movement; do NOT inch the made_strength floor.
- Birth mandate (RECURRING 8+ gens; v204/v206 FIRST COMPLIANT: pair_profile param + ≥3 dispatch sites): wire primitives with ≥3 LIVE dispatch sites AT BIRTH + reachability/H2H-WR @≥30g; verify the TRIGGER CONDITION itself fires (dead trigger → multi-site dispatch inert); self-test logic proofs do NOT count.
- CAP CONSTRAINTS (POST-v208, RE-MEASURE before any edit): strategy.py≈2486/2500 (~14-line headroom), opponent.py≈1782/2500 (~718 headroom), strategy_helpers.py=2500/2500 EXACT CAP (reclamation HARD PREREQ for helpers edits only). Comment-condensation must NOT touch behavioral code.
- `facing_villain_4bet=True` in `_preflop_spr_commitment_gate` STILL DORMANT — original 4bet-NON-allin param unwired; verify the graduated gate fires @≥30g first.
- Preflop shove-defense over-fold risk (v201): premiums-only exploitable vs wide/open-shovers (VPIP>0.60) — folding 55 at pot_odds~0.50 is -EV. Gate marginal/small-pair fold on pfr/vpip once n≥30.
- Offense value-sizing-UP (v198/v199/v207): proof bot v159 CULLED → UNVERIFIABLE; v207 restored value_hand_skip yet precommit still showed blowouts. Do NOT inch made>=0.55→0.50 without ≥30g paired WR-lift. [POSSIBLY EXHAUSTED]
- Single-site / per-site-sequential fold approaches AND inching made_strength floors (v204 0.35→0.45; proposed 0.55→0.50) without ≥30g WR-lift evidence [STALE — no WR-lift]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal a calling-station.
- Board-texture bluff raise (v185→v191): dispatch LIVE but axis EXHAUSTED, PAUSED pending ≥100g WR-lift. [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- choose_raise() constant-only nudges [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT only structural rewrites adding NEW DEAL-LOCAL opponent-signal gating; CLOSED archetype axis does NOT reopen.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp rarely fire in 70-hand HU; widen_threshold must target ≥15pp bands.
- Firing verification: reachability_test + ≥100g H2H WR-lift is the ONLY reliable gate. stderr NOT readable (_PersistentBot stdout-only); daemon-grep "fired≥5%" UNFULFILLABLE — gate must be reachability/H2H-WR, NOT a grep count.

## GENERAL
- Master RELIABLE at plan-generation but reliability ≠ correctness: validate axis PAYLOAD (≥100g WR-lift), not just plan cleanliness.
- Dead-code/guard removal > adding constants — each action_type discriminator (raise/allin) must live in its OWN branch (nesting 'allin' inside 'raise' made shove_rate 0 for 5 gens w/ NO test failure; fixed v194); reachability-test the DOWNSTREAM consumer after any recording-site change. Plateau (WR~0.50): pursue a NEW structural axis.
- Validation thresholds: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Trust git diff over commit messages/Master plans; direct H2H authoritative over transitive chains. Do NOT base work on unvalidated bots (no .completed/no Glicko rating).
- Critic advisory ≤4.0 + local_optima_warning=true on an exhausted axis mandates a direction_audit pivot — doesn't gate commit (precommit authoritative) but mandates pivot enforcement.
- Crossover ancestry silently discards validated mutations on the non-chosen branch — Master MUST git-inventory + port sibling-lineage critical mutations when branching_from is set (v197/v199 regressed v196 fold; v200 lost v198 value_hand_skip; v207 restored it). 'Lost-fix restoration via crossover ancestry audit' is a validated reusable diagnostic pattern.
- Workers MUST grep own dispatch sites + run reachability_test to confirm every param/signal gates an outcome before claiming coverage.
- Precommit silent 2-attempt retry: a first 'FAILED: match_timeout' (n=8 hitting 960s mirror limit) auto-falls-back to n=4 and can PASS — distinguish timeout-failure from data-driven-failure; SIGSTOP the daemon before precommit (n_played=0 timeouts = CPU contention, NOT regression).
- Evaluate polarized-aggression fixes by net-chips/blowout-frequency, NOT W-L (v204: 5W-3L yet net -25071 vs v193 = razor-thin wins masking one blowout).
- Active-axis prescription RETIRED: v208 EXECUTED the deal-local-detector direction (revealed_shove_density) — the prior "ONLY permitted = new deal-local detector" is superseded. Validate v208 firing @≥30g before spawning further new axes; do NOT re-recommend the executed prescription.

## RECENT_LESSONS
- **v209**: Critic evidence: H2H weaknesses: v208 vs v190 WR=0.300 n=20 (nemesis), v208 vs v205 WR=0.350 n=20, v208 vs v186 WR=0.400 n=20, v208 vs v179 WR=0.400 n=20 — all consistent with value-heavy-shover leak the detector was designed to attack; Experience pool refs: OPPONENT_MODELING: v208 'LANDED-BUT-UNVALIDATED (no firing test yet)' — v209 closes this gap, RECENT_LESSONS v208: 'Reachability_test SKIPPED AGAIN (v204/v205/v208 pattern)' — v209 finally runs it, RECENT_LESSONS v208: 'Linear shove_rate→air-frequency mapping ... wide-merged-value shovers break it — when revealed_shove_density diverges from the shove_rate prediction, the revealed signal MUST OVERRIDE, not blend at 0.50 cap' — v209 implements exactly this OVERRIDE; Diff refs: opponent.py L383-388: _reached_river guard REMOVED, _prev_hand_was_shove_call = _opp_shoved_this AND _we_called_shove (no river guard), opponent.py L982-993: NEW divergence-aware OVERRIDE — if _rsd_n>=3 AND abs(_raw_air - _base)>=0.15 -> weight=min(0.85, 0.55+(_rsd_n-3)/12); else gentle blend@0.50 cap preserved, opponent.py L1783-1796: OVERRIDE self-test (wide-merged-value shover: bf=0.188 < old-blend 0.22 AND < proxy 0.34 — verified arithmetic)
- **v208**: Reachability_test SKIPPED AGAIN (v204/v205/v208 pattern) despite pool mandate — future Masters MUST measure shove_rate>=4-samples activation AND revealed_shove_density n>=2 activation in actual nemesis matchups BEFORE moving thresholds; inert detectors are the #1 local-optima trap.
- **v208**: Linear shove_rate→air-frequency mapping assumes high shove_rate = bluff-heavy; wide-merged-value shovers (jam TPTK+/overpairs wide) break it — when revealed_shove_density diverges from the shove_rate prediction, the revealed signal MUST OVERRIDE, not blend at 0.50 cap.
- **v208 归档建议**: @≥30g evaluate by NET-CHIPS/blowout-freq vs v174/v195 (NOT W-L); if -20k stack-offs persist WITH detector firing ≥5%, pivot to preflop-4bet/turn-allin sibling sites rather than inching the discount formula.
- **v208**: Critic H2H — v207 vs v174 WR=0.35 n=20 (-16953/-16850/-14999, replay 20260628_144555); v207 vs v195 WR=0.35 n=20 (-11819/-13124, replays 20260628_142042/151659); v207 vs v193 WR=0.45 n=20 (9-11) — Master cited stale 0.20 figure.
- **v207**: Crossover ancestry can SILENTLY drop validated mutations (v200 v194×v190 lost v198 value_hand_skip) — git-inventory sibling-lineage mutations before planning crossovers.
- **v207**: Postflop all-in stack-off vs v186 (-19751) is NOT closable from offense/sizing — v198/v199/v207 all failed to move it. Do NOT inch made>=0.55→0.50 without ≥30g paired WR-lift.
- **v207 归档建议 (mixed)**: Attack postflop turn all-in no-draw fold (round_idx==2 opponent_allin, AA exempt, pair+draw pass) — BUT fold-gate axis is CROSS_GEN_PIVOT-blocked, needs keyword-purge reframe or crossover-bypass.
- **v206**: LANDED FIRST preflop-half attack — trigger from raise-COUNT (inert opp_pf_raises>=2) to raise-SIZE (>=12BB), 2 dispatch sites reachable. Beat parent yet STILL -23165 vs v193 from a POSTFLOP site → ≥2-site leak. NEXT: reachability-test 12BB fires ≥5% @≥30g vs v193/v201; VPIP>0.60 small-pair continue gate unaddressed.
- **v206**: try-1 rejection CONFIRMS parametric fold-margin nudges (discount/target/clamp) are DISPROVEN-INERT (v184/v188/v189/v190) — sanctioned fold-side lever is now STRUCTURAL (new dispatch-site/trigger/function), NOT constant-tuning.

