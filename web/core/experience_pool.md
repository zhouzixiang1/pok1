## OPPONENT_MODELING
- Continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + per-street fold_to_bet/call-down + passivity_score; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, NOT fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` for hand-class gates.
- NO archetype classifier (LAG/NIT/CS) on v83 — dropped when lineage re-based on v23/v79. Do NOT confuse `value_profile['tier']` (made-hand STRENGTH: none/thin/strong/nut) with opponent archetype; it cannot substitute for opponent-intent gating.

## POSTFLOP_STRATEGY
- Structural pre-dispatch commitment gate (v73/v75-style, in get_action() before dispatch) is ABSENT on v83 — crossovers re-based on v23. A SPR-awareness comment + fold line exist (strategy.py:515,1248) but are NOT a structural gate. Restoring pot-odds-grounded fold DEFENSE is high-value and under-developed (NOT exhausted).
- Fold/commitment must be pot-odds + opponent-stat grounded, not a raw made_strength threshold; re-verify liveness before reuse.
- Opponent-stat gating needed on value paths (barrel_plan VALUE branch ~postflop.py:1050, river value-bet blocks); add `postflop_aggr<0.30` or strength-tier≠nut exclusion if H2H vs high-aggr lineage regresses ≥100g.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code.
- Audit every action-selection path for raw-ratio bypasses skipping `choose_raise` — high-value bugs.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.
- Turn barrel on `was_flop_aggressor + to_call==0 + opp check` is sound; wire `has_position` for OOP/IP distinctions.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need ≥100g H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Standalone constant/margin tuning of sizing ratios and call thresholds yielded no sustained gain; constants allowed only with structural rationale AND per-constant H2H backing — never standalone. [POSSIBLY EXHAUSTED]

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100g H2H; <100g is directional noise.
- Select crossover parents by H2H win-rate, not raw Glicko r; prioritize diversity over deepening an over-fit lineage.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues.
- Worker role boundaries: Tuner changes ≥1 constant; Architect must not touch constants. Crossover bots need full pipeline (gates→review→critic→commit→archivist).
- Trust early negative Critic signals; structural changes can inflate Critic scores without improving battle — verify H2H.
- Direction-Audit fold-forbiddance is sliding-window, not permanent; default to OFFENSIVE/structural work and surface audit-vs-match conflicts.
- Post-crossover verification mandatory: crossover LLM can derive correctness fixes absent from both parents — verify TOTAL_HANDS, wheel straight, re-raise compliance.
- Detection-without-handler is a recurring dead-code pattern; verify every classifier has a consuming branch.
- v83 is current. LIVE: per-street fold_to_bet/call-down, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, overbet.py, donk_probe.py, line_reading.py polarization. STILL ABSENT: exploit_dispatch, board_range_filter, archetype classifier, structural pre-dispatch commitment gate; bluff_heavy computed-but-unwired.

## RECENT_LESSONS
- **v85**: Critic evidence: H2H weaknesses: v83 weakest: v62 @ 40% (30g), v48/v81 @ 43.3% (30g), v82/v19/v17 @ 45% (40-50g) — all <100g directional noise per pool, but CS-lineage over-fold pattern is documented. v83 overall win_rate 51.39% over 1080g (near plateau).; Experience pool refs: RECENT_LESSONS v83: 'wire dead bluff_heavy branch into river bluff-catch/call-down (mirror of value_heavy fold gate) — DETECTION-WITHOUT-HANDLER recurred; complete the symmetric range-aware loop before adding new detection dims.', RECENT_LESSONS v84: worker deviated to thin-value raise instead of literal pool suggestion; this generation implements the original mirror (call-down widening)., v84 归档建议: 'if bluff_heavy lines never fire vs passive callers (the high-confidence AND gates may never trip), lower the opp_confidence>=0.20 threshold or pivot to pool's mirror proposal (widening call-downs vs bluff_heavy)' — v85 takes the mirror pivot path but does NOT lower thresholds, so the firing-narrowness risk remains.; Diff refs: strategy_helpers.py:221-244 — new bluff_heavy_call_widen() with tier/made_strength/draw/confidence gating, clamp(0.0, 0.08) bound, strategy.py:844-848 — wired additively into call_margin BEFORE the realized_rate < pot_odds + call_margin decision at line 937, so it is pot-odds-grounded, line_reading.py:60-61 — bluff_heavy label requires bluff_opportunity >= 0.55 AND > value_pressure + 0.10 (multi-signal)
- **v84**: DETECTION-WITHOUT-HANDLER pattern recurs across versions (v77 sb_limp_vs_raise, v83 bluff_heavy) — when adding an opponent classifier, ALWAYS wire a consuming action site in the same generation; an unwired label is a guaranteed next-gen task and a critic-local-optima risk if the next worker tries to consume it with constant tuning instead of structural wiring.
- **v84**: Critic 7.0 confirmed: creative reinterpretation of the experience pool's suggested mirror (pool suggested widening call-downs vs bluff_heavy; worker instead raised for thin value) scored same as a literal implementation would have — deviation from pool suggestions is acceptable when the alternative is structurally distinct and arguably higher-EV.
- **v84 归档建议**: Once v84 accumulates ≥100 games in the daemon, compare its turn/river raise-frequency and H2H vs the calling-station lineage (v51/v62/v78) against v83's baseline — if bluff_heavy lines never fire vs passive callers (the high-confidence AND gates may never trip), the next generation should either lower the opp_confidence≥0.20 threshold in line_reading.py or pivot to the pool's original mirror proposal (widening call-down ranges vs bluff_heavy) which addresses the same opponent-strength asymmetry from the defensive side.
- **v84**: Critic evidence: H2H weaknesses: v83 weakest matchups all <30g noise (v81 35% @ 20g, v48/v62 40% @ 20g, v17/v21/v80/v19/v82 45% @ 20g) — cannot reliably identify exploitable patterns at <100g; targets the documented 'bluff_heavy computed-but-unwired' gap instead of H2H-driven targeting. v51/v62 calling-station lineage is the natural target if bluff_heavy detection fires on passive opponents.; Experience pool refs: RECENT_LESSONS v83: 'wire dead bluff_heavy branch into river bluff-catch/call-down (mirror of value_heavy fold gate) — DETECTION-WITHOUT-HANDLER recurred; complete the symmetric range-aware loop before adding new detection dims.', GENERAL: 'Detection-without-handler is a recurring dead-code pattern; verify every classifier has a consuming branch.', GENERAL: 'bluff_heavy computed-but-unwired (fresh instance of the v81 classify_street_texture pattern)'; Diff refs: strategy_helpers.py:25-60 — new bluff_heavy_raise_to_extract() with tier/made_strength/confidence gating + 55% stack cap + 0.45-0.70x ratio scaling, strategy.py:1125-1135 — invoked after anti_lock block, before value_heavy fold gates and weak-pair fold gates; returns None falls through cleanly, line_reading.py:60-61 — bluff_heavy label requires bluff_opportunity >= 0.55 AND > value_pressure+0.10 (high-confidence multi-signal trigger)
- **v83**: wire dead bluff_heavy branch into river bluff-catch/call-down (mirror of value_heavy fold gate) — DETECTION-WITHOUT-HANDLER recurred; complete the symmetric range-aware loop before adding new detection dims.
- **v83**: Validate 3 value_heavy fold gates vs CS lineage v51/v62 at ≥100g (over-fold risk on strong one-pair).
- **v83**: Helper extraction is a safe high-value move near the 1500-line cap (1498→1302 clean, zero logic change); prefer extraction over compression when headroom <50.
- **v82**: passive_exploit.py wired (second_barrel_vs_station, not shadowed by should_probe_bet); validate ≥100g vs aggressive/value matchups (v80/v48/v34 ~45% small-sample); ensure delayed_cbet/river_thin_value branches reachable.
- **v82**: Small-sample H2H (v81 clustered 45–55% @<20g) is DIRECTIONAL NOISE per ≥100g rule — gather ≥100g before targeting aggressive/value matchups (v30/v62/v78 parity).
- **v83**: Crossover dead-code trap recurs — line_reading.bluff_heavy computed but never wired (fresh instance of the v81 classify_street_texture pattern); verify every cross-imported classifier/branch is actually called before commit.



