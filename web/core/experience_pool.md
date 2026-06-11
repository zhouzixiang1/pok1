## OPPONENT_MODELING
- Structural barrel modules remain viable — v31's `should_barrel_turn` validated despite parameter-delta exhaustion.
- Wiring `opponent_model` into street-specific decisions (barrel, sizing) is the confirmed incremental path — v33 validated.
- Archetype classification should be exploited in more decision points; river fold (v34), flop check-raise (v40), flop c-bet still open.
- Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate. (v34)
- Archetype fold deltas (0.02–0.04) are structural adjustments to equity variables, not constant-tuning — but monitor whether they produce measurable H2H impact before iterating further.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- Fold gate sprawl is EXHAUSTED — v39 extracted `_bb_defend_vs_raise()` and `_handle_repeated_raise()`. Further extraction of `river_raise_response()` still viable, but do NOT add inline branches. [EXHAUSTED]
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Dry-flop check-raise traps need opponent-confidence safety thresholds.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations. (v36)
- BB defense floor covers ~48% of hands structurally — validate fold-to-steal rate vs v38 in next daemon cycle. (v39)

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel continuation) need ≥100-game H2H backing before targeting a matchup.
- Hash-based randomization for bluff frequency is deterministic and exploitable — prefer game-state entropy (pot size, hand number, opponent pattern). (v37)
- Before iterating on specific bluff modules, verify H2H vs top opponents with ≥50 mirror games — if win rate doesn't improve, the leak is likely elsewhere.
- Archetype-aware bluff cutoff (never bluff CS, boost vs NIT) is highest-confidence change from v40.

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
- `thin_control` gate exempts nut/strong tiers; strong postflop raises floored at 0.50 pot.
- Strategy.py capacity pressure is ongoing — extract standalone functions to helper modules (e.g., postflop.py) before adding new logic.

## RECENT_LESSONS
- **v42**: Critic evidence: H2H weaknesses: v41 overall win_rate=0.4973 (370 games) — essentially break-even. Weakest matchups: v18 (WR=0.200, 10 games), v24/v26/v10 (WR=0.400, 10 games each). Sample sizes too small for confident targeting but direction (more aggressive value extraction, better draw protection) is consistent with a break-even bot needing to find +EV edges.; Experience pool refs: POSTFLOP_STRATEGY: 'structural modules need ≥100-game H2H backing' — both modules are structural and testable. BLUFF_CALIBRATION: 'base postflop sizing ratios are stable (flop 0.60, turn 0.70); extend via structural paths, not retuning' — protective sizing extends sizing via a structural floor, not retuning base ratios. PARAMETER_TUNING: 'hand-tuned constants in structural modules are still parameter tuning' — the draw equity estimates (0.35, 0.31, 0.20, 0.17) are grounded in poker math (outs×2/4 rule), not arbitrary thresholds. RECENT_LESSONS: 'code reorganization from strategy.py → postflop.py freed ~44 lines; continue extracting standalone functions to helper modules' — both new functions are correctly placed in postflop.py.; Diff refs: postflop.py: `protective_sizing_floor()` (L1038-1086) — solves R/(1+2R)≥equity+margin for min sizing ratio. Only for strong/nut tier, flop/turn. Draw equity estimates match poker math (flush draw 2 cards ≈35%, straight draw 2 cards ≈31%, flush draw 1 card ≈20%, straight 1 card ≈17%). strategy.py L467-472: wired as floor in compute_raise_size, only overrides if computed floor > current ratio., postflop.py: `river_showdown_extraction()` (L1089-1145) — targets medium-strength (0.35-0.62) non-strong/nut hands on river when to_call==0. Requires opponent confidence≥0.20, VPIP≥0.50, call_freq≥0.50. Archetype-aware: skips weak-medium vs NITs. Sizing 0.25-0.40 pot with board texture adjustments. strategy.py L1317-1331: early return after thin_static_showdown_control check, before overbet evaluation — correct placement since medium hands shouldn't overbet.
- **v41**: Archetype fold deltas (0.02–0.04) integrated correctly as `eff_made = made_strength - archetype_delta`, modifying existing fold check variable rather than adding new gates — follow this pattern for future archetype wiring.
- **v41**: Code reorganization from strategy.py → postflop.py freed ~44 lines in the near-capacity strategy.py; continue extracting standalone functions to helper modules as capacity pressure recurs.
- **v41**: Monitor whether archetype classifier reaches confidence ≥ 0.15 within first 30 hands vs lag/CS opponents — if not, fold threshold adjustments never activate and the generation is effectively v40 with a file move.
- **v40**: LAG check-raise at `made_strength ≥ 0.38` is risky — if 3-bet frequency is high vs LAGs, raise threshold to ≥ 0.45 or add `draw_strength ≥ 0.15` guard.
- **v40**: Bluff threshold adjustments should modify existing equity variables over adding new conditional branches.
- **v39**: The repeated-raise unconditional-call bug may have suppressed value raises with strong non-nut hands — compare showdown raise frequencies in v39 vs v38 replays.
- **v38**: H2H weaknesses vs v27 (~30% WR), v34/v22/v26/v16/v2 (~40% WR) with no decision-point analysis — investigate with verbose mirror games before structural changes. Data-first over assumption-first.

