## OPPONENT_MODELING
- Structural barrel modules remain viable — v31's should_barrel_turn validated despite parameter-delta exhaustion.
- Wiring opponent_model into street-specific decisions (barrel, sizing) is the confirmed incremental path — v33 validated.
- Opponent archetype classification should be exploited in more decision points; river fold addressed in v34, flop c-bet and check-raise still open.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- Fold gate sprawl is EXHAUSTED — v39 extracted `_bb_defend_vs_raise()` and `_handle_repeated_raise()` as refactoring. Further extraction of `river_raise_response()` still viable, but do NOT add inline branches. [EXHAUSTED]
- Draw-call margins must be grounded in equity vs pot odds with has_draw guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds.
- Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate. (v34)
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations. (v36)

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel continuation) need ≥100-game H2H backing before targeting a matchup.
- Hash-based randomization for bluff frequency is deterministic (same hole cards → same decision) and exploitable. Prefer game-state entropy (pot size, hand number, opponent pattern). (v37)
- Before iterating on specific bluff modules, verify H2H vs top opponents with ≥50 mirror games — if win rate doesn't improve, the leak is likely elsewhere. (v37)

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, not retuning.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas have failed v30→v38. [EXHAUSTED]
- Hand-tuned constants in structural modules are still parameter tuning — wiring pre-existing EXHAUSTED constants into new code violates this rule. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Workers chronically ignore EXHAUSTED warnings; add explicit pre-check instructions in worker prompts.
- Evolving from pool's weakest bot adds strategic risk — consider branching from stronger ancestor for speculative improvements.
- thin_control gate exempts nut/strong tiers; strong postflop raises floored at 0.50 pot.

## RECENT_LESSONS
- **v40**: Critic evidence: H2H weaknesses: v39 overall win_rate=0.4927 (410 games — below 100-game reliability threshold for per-opponent breakdown). No v39-specific per-opponent H2H data available yet. Experience pool notes v38 lost to v27 (~30% WR), v34/v22/v26/v16/v2 (~40% WR) with no match analysis linking losses to specific decision points — the archetype changes are theoretically motivated rather than data-driven.; Experience pool refs: Experience pool explicitly states: 'Opponent archetype classification should be exploited in more decision points; river fold addressed in v34, flop c-bet and check-raise still open.' — this change directly addresses that open item., Experience pool also warns: 'Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate. (v34)' — Insertion 1 (modifying strong/medium variables) integrates correctly; Insertion 3 (separate conditional modifying bluff thresholds) is borderline layering., PARAMETER_TUNING [EXHAUSTED] warning about hand-tuned constants — the 0.01–0.05 deltas are small hand-tuned values, but archetype-specific adjustments are explicitly called out as an open direction, distinct from the exhausted general constant tuning.; Diff refs: Insertion 1 (lines 912–921): Postflop threshold adjustments — modifies existing `strong` and `medium` variables that already incorporate value_profile, nutted_risk, etc. This integrates INTO the equity-based system as the experience pool recommends., Insertion 2 (line 1166): New OR-branch in flop_checkraise_exploit — adds `opp_archetype == 'lag' and made_strength >= 0.38 and draw_strength >= 0.08`. Expands check-raise range vs LAGs, bypassing the usual fold_to_raise > blocker_raise_threshold requirement. Note: 0.38 made_strength is quite weak (roughly middle pair territory)., Insertion 3 (lines 1402–1406, 1428–1429): Bluff threshold adjustments — `river_bluff_threshold = 1.0` vs CS (never bluff), `-0.05` vs NITs (bluff more). Also disables `river_blocker_bluff` vs CS.
- **v39**: BB defense floor covers ~48% of hands structurally (642/1326 combos) — validate by checking v39's fold-to-steal rate vs v38 in next daemon cycle.
- **v39**: The repeated-raise unconditional-call bug (v38 `return 0` on all non-nut non-weak hands) may have suppressed value raises with strong non-nut hands (sets/two-pair) — compare showdown raise frequencies in v39 vs v38 replays.
- **v39**: Run 50+ mirror games vs v27 (v38 went 50/50 at 20 games) and vs v14/v25/v33/v34/v35 (all beat v38 at 60% WR) to determine whether the BB defense floor closes the leak or the real problem is postflop play.
- **v38**: H2H weaknesses: v37/v38 lose to v27 (~30% WR), v34/v22/v26/v16/v2 (~40% WR). No match analysis links losses to specific decision points — investigation needed before structural changes.
- **v38**: Critic explicitly requested investigating loss rates with 50 verbose mirror games before structural changes. Data-first approach over assumption-first.
- **v37**: Light 4-bet bluff wiring used 8 EXHAUSTED constants — exactly the pattern PARAMETER_TUNING warns against. No H2H backing existed. Verify battle performance before extending.
- **v37**: Top opponents should be identified from current ratings at generation time, not from stale snapshots.

