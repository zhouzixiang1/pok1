## OPPONENT_MODELING
- Archetype classification should be exploited in more decision points; river fold (v34), flop check-raise (v40), flop c-bet still open.
- Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate. (v34, v41 confirmed)
- Archetype fold deltas (0.02–0.04) are structural adjustments to equity variables — monitor H2H impact before iterating further.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- Fold gate extraction is done — no new inline branches. Refactoring existing logic (e.g., `river_raise_response()`) into helpers is still viable.
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations. (v36)
- BB defense floor covers ~48% of hands structurally — validate fold-to-steal rate vs v38 in next daemon cycle. (v39)

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Hash-based randomization for bluff frequency is deterministic and exploitable — prefer game-state entropy. (v37)
- Archetype-aware bluff cutoff (never bluff CS, boost vs NIT) is highest-confidence change from v40.

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, not retuning.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas have failed v30→v38. [EXHAUSTED] [POSSIBLY EXHAUSTED]
- Hand-tuned constants in structural modules are still parameter tuning — wiring pre-existing EXHAUSTED constants into new code violates this rule. [EXHAUSTED] [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Workers chronically ignore EXHAUSTED warnings; add explicit pre-check instructions in worker prompts.
- Evolving from pool's weakest bot adds strategic risk — consider branching from stronger ancestor.
- Strategy.py capacity pressure is ongoing — extract standalone functions to helper modules before adding new logic.

## RECENT_LESSONS
- **v43**: Critic evidence: H2H weaknesses: v42 has only 330 total games at 50.0% win rate — no matchups reach 100-game confidence. All H2H data is directional only.; Experience pool refs: `should_fold_postflop()` is the primary fold gate; exceptions need equity, pot-odds, and confidence validation. — this change adds equity+pot-odds reasoning at the terminal call site., PARAMETER_TUNING: Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas have failed v30→v38. [EXHAUSTED] — the pot*0.05 raise threshold and 0.3 implied odds constant risk falling into this pattern., BLUFF_CALIBRATION: Archetype-aware bluff cutoff (never bluff CS, boost vs NIT) is highest-confidence change from v40 — but this function doesn't check archetype before suggesting raise.; Diff refs: New function select_postflop_facing_bet() (lines 699-750) computes fold/call/raise EV and selects highest., Inserted at line 1219 in the facing-bet path: replaces unconditional 'return 0' with EV-based decision., Parameters has_position and board_texture are received but never used in EV computation.
- **v42**: `protective_sizing_floor()` unused `pot` parameter may not account for pot-growth on later streets — verify math for turn scenarios.
- **v42**: `river_showdown_extraction()` targets wide opponents (VPIP≥0.50) with 25-40% pot bets — evaluate whether thin-value sizing is exploitable by v26 (pool leader) bluff-raises.
- **v41**: Archetype fold deltas integrated as `eff_made = made_strength - archetype_delta` — follow this pattern for future archetype wiring.
- **v41**: Code reorganization from strategy.py → postflop.py freed ~44 lines; continue extracting standalone functions as capacity recurs.
- **v41**: Monitor whether archetype classifier reaches confidence ≥ 0.15 within first 30 hands vs lag/CS — if not, fold adjustments never activate.
- **v40**: LAG check-raise at `made_strength ≥ 0.38` is risky — if 3-bet frequency is high vs LAGs, raise threshold to ≥ 0.45 or add `draw_strength ≥ 0.15` guard.
- **v40**: Bluff threshold adjustments should modify existing equity variables over adding new conditional branches.

