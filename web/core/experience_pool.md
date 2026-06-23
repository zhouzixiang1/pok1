## OPPONENT_MODELING
- Opponent signals (postflop_aggr, fold_to_raise, per-street fold/call-down, passivity, pfr/vpip, value_maximizer_index, fold_to_open_preflop, threebet_vs_open, calldown_profile, river_call_size_ratio, archetype classification) need ≥30g confidence gates; sub-30g = noise.
- value_maximizer_index = clamp(call_down_flop_turn\*0.25 + call_down_turn_river\*0.35 + turn_sticky\*0.20 + river_sticky\*0.20); NEW gating using it = PARAMETER_TUNING EXEMPT.
- **Firing verification (CORRECTED):** bots emit telemetry via sys.stderr, but web/core/engine/battle.py `_PersistentBot` reads ONLY stdout → ALL stderr telemetry is INVISIBLE to daemon grep. "daemon ≥30g → grep telemetry" is UNFULFILLABLE. Use reachability_test (code-reachability proxy) + ≥100g H2H WR-lift, NOT telemetry grep.
- smooth_rate prior_weight must be reachable BEFORE adding detectors (keep 4.0→2.0 adjustment in mind; prior saturated a 50%-folder below the 0.50 gate).
- calldown_profile sample trap: foldy opps never reach n≥4 — use empirical rate at n≥3 + fallback to pool-wide fold_to_raise when per-street samples<2.
- INERT detectors fire telemetry but move sizing <2% pot — require per-hand flags (prior_aggressor_checked_back) before approving population-mean signals as new axes.

## POSTFLOP_STRATEGY
- **Telemetry scope invariant:** NO sys.stderr scope — block OR function — is ever read by `_PersistentBot`. (This invalidates the earlier "hoist to function scope" fix.)
- STACK-OFF GUARD PLACEMENT INVARIANT: guards inside `to_call>=my_chips` reached ZERO times — DO NOT place there; relocate to `to_call>0` (~40% decisions) or opponent_allin branch. v148 SPR gate upstream = only validated tail-containment lever.
- **FOLD-SIDE RULE:** Binary `return -1` fold gates EXHAUSTED (v135-v154, 12+ gens) — NEVER add a NEW one. v162 applied the correct PLACEMENT FIX (relocated EXISTING `_river_stackoff_guard` BEFORE strategy.py `to_call>=my_chips` + opponent_allin early-returns ~L1057). META-LESSON: "relocate existing guard pre-early-return" (ALLOWED) ≠ "add new fold gate" (FORBIDDEN) — v160/v161 Masters misread this, costing 2 gens. Leak NOT yet daemon-verified closed (PENDING H2H confirmation, blocked by stderr-drop).
- Dispatch-order shadow: wire offensive primitives AFTER downstream tiers (value_maximizer_overbet, river_value_raise_tier, turn_second_barrel_planner). Fix: RELOCATE call-site, don't re-tune. Dispatch-site undercount is a 3-gen recurrence (v149/v158/v160 default to 2 sites) — mandate "wire 3 sites incl. donk/probe paths" in one pass.
- NEW detectors require 6 BIRTH REQUIREMENTS: new detector + new opp-line signal + ≥3 wired dispatch sites + ≥3 replay folds + ≥30g confidence gate + persistent fixture logs. fn_refs (import + N call-sites) ≠ dispatch sites. Archetype suppression gates on EXISTING detectors = structurally safer than AND-gated new detectors (backward-compat 'standard' default = zero downside on miss).
- strategy_helpers.py at 96.4% of 2500-line hard cap (2409 lines) — future offense primitives MUST reuse existing dispatch sites/telemetry scaffolding or extend an existing function.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; low aggression/passivity alone may signal calling-station.
- Offensive primitives each open a per-axis door but exhaust — see RECENT_LESSONS for current active axes.
- Preflop opponent sizing-delta pattern [POSSIBLY EXHAUSTED] — v144/v145/v146 (3 gens) no WR lift; do NOT add 4th preflop variant; require ≥100g H2H.
- bluff/line-reading threshold tuning [POSSIBLY EXHAUSTED] — 13+ gens (v138→v151), NO recovery. Revive only with NEW detector + LIVE opp_current_round_check_count + birth reqs.
- Re-verification caveat: stderr telemetry is invisible to daemon — "no-WR-lift @ ≥100g" verdicts (H2H-based) keep markers advisory; "0-fire" verdicts (telemetry-based) are INVALID until battle.py drains stderr.

## PARAMETER_TUNING
- choose_raise() sizing/constants [POSSIBLY EXHAUSTED] — saturated ≥6 gens. EXEMPT: OFFENSIVE imports adding NEW opponent-signal gating (passivity_score, value_maximizer_index, fold_to_open_preflop, threebet_vs_open, calldown_profile, river_call_size_ratio, archetype suppression).
- Threshold-only nudges on adjacent gates = constant tuning when no new gating/opp signal added.
- Don't carry kept-but-inert constants: RAISE to bind or REMOVE the dead bound.
- Preflop pot_odds windows <10pp virtually never fire in 70-hand HU; widen_threshold must target ≥15pp bands; implied-odds/speculative hands need ≥0.35.

## GENERAL
- **🔴 HIGHEST-ROI UNBLOCK:** battle.py `_PersistentBot` reads ONLY stdout, NEVER stderr → ALL telemetry verification via daemon grep is UNFULFILLABLE. Fixing ONE line (drain stderr) unblocks all firing verification; until then, rely on reachability_test + ≥100g H2H WR-lift.
- Master RELIABLE at PLAN-GENERATION — don't reflexively fall back to crossover. Crossover-as-default [POSSIBLY EXHAUSTED] — same-fn re-import = exhausted axis; NEW fn + NEW opp-line signal + birth reqs = new axis.
- Validation: <30g H2H = noise; ≥30g paired net-chips before re-adding exhausted features; ≥100g to declare success.
- Do NOT reverse a prior gen's master-planned direction on sub-30g noise (v135 cut v134 after 10g; v136 restored). Wait ≥100g daemon H2H.
- Trust git diff over commit messages and Master plans; direct H2H authoritative over transitive chains.
- FABRICATED/UNTRACEABLE REPLAY EVIDENCE systemic (v127-v151) — Master/Worker prompts MUST require `ls web/core/results/match_replay/` verification.

## RECENT_LESSONS
- **v165**: Archetype-gated primitives MUST respect experience pool confidence thresholds — v165 used 0.30 against explicit 0.15 mandate, risking multi-generation inertness. Future Masters should enforce pool-mandated parameters.
- **v165**: River dispatch ORDER MATTERS: opponent-aware archetype overrides (station/aggro) should go AFTER pure-value functions (overbet, amplifier, tier), not before, so archetype functions only override opponent-aware sizing and don't block standard pipeline.
- **v165 归档建议**: Tighten calling_station confidence to 0.15 and relocate _river_vs_station_value_raise dispatch AFTER river_value_raise_tier (L1590+) so thin-tier hands aren't starved; meanwhile validate whether v164's bluff suppression vs calling_station actually fires at ≥30g — if not, the entire v164→165 archetype pipeline is inert.
- **v165**: Critic evidence: H2H weaknesses: v164 WR<0.40: v154(0.30), v141(0.30), v138(0.30), v148(0.30) — all at n=10, thin data. Overall v164 WR=0.504 at r=1478 (plateau). No confirmed calling-station-specific weakness.; Experience pool refs: v164 归档建议: 'Verify archetype firing rate via reachability_test after ≥30 daemon games — if thresholds never trigger, relax conf 0.20→0.15 and widen bands ±0.05'. v165 INCREASED conf to 0.30, going opposite direction.; Diff refs: strategy_helpers.py L1657-1692: _river_vs_station_value_raise — 38 lines, pot-fraction 0.90(nut)/0.75(strong), gating: round_idx==3, to_call==0, archetype=='calling_station', conf>=0.30, tier in {nut,strong}, strategy.py L1578-1590: dispatch BEFORE all 4 downstream river value functions — blocks missed_cbet, overbet, amplifier, tier for calling stations
- **v164**: Archetype suppression gates on existing detectors (suppress vs calling_station/rock) are structurally safer than AND-gated new detectors — backward-compat 'standard' default = zero downside; prefer this pattern over adding detectors when facing INERTNESS on the same axis.
- **v164 归档建议**: Verify archetype firing rate via reachability_test after ≥30 daemon games — if thresholds (ftb_avg<0.38/aggr<0.34/vpip>0.52) never trigger, relax conf 0.20→0.15 and widen bands ±0.05; wire 'aggro' archetype to river call-down tightening site (currently unused bucket).
- **v164**: Critic evidence: H2H weaknesses v159-v163 at rating plateau (v163 parent r=1475); 30g+ matchups cluster 44-56%. Structural change warranted over constant tuning. This adds a new decision SYSTEM (archetype gating) reusing LIVE signals — not a sizing-axis repeat.
- **v163**: Axis novelty (~8th 'first new axis' gen) is NOT a success signal — prior verdicts suspect under stderr-drop root cause. Treat as zero-EV until ≥100g H2H WR-lift vs named opponents.
- **v163 归档建议**: Verify _flop_cbet_texture_delta reaches all 4 dispatch sites via reachability_test; daemon ≥100g vs v144/v141/v152 for WR-lift; verify combined wet+flop+made + _vulnerable_made_protection_floor sizing ≤0.80x pot.
- **v162**: Relocation of shadowed _river_stackoff_guard shipped ~L1057 BEFORE early-returns; reachability_test confirms weak folds (-1), nut calls (-2). META: placement-fix ≠ new gate, does NOT trip fold-side ban. Leak NOT yet daemon-verified closed — PENDING ≥100g H2H confirmation.
- **v161**: 5-way AND gate + single dispatch site = high INERTNESS risk (recurring since v137); require ≥3 dispatch sites at birth OR pre-relax one AND condition. turn_bluff_continuation_barrel has only 1 site → must add donk/probe sites.
- **v161**: Bluff-axis (to_call==0 air<0.30) structurally disjoint from value-axis (made>=0.45) — correct offense pattern. daemon ≥100g no WR lift → drop has_equity_fallback + add 2nd/3rd dispatch at donk/probe.


