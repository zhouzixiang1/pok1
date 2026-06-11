## OPPONENT_MODELING
- Archetype classification should be exploited in more decision points; river fold (v34), flop check-raise (v40), flop c-bet still open.
- Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate. (v34, v41 confirmed)
- Archetype fold deltas (0.02–0.04) are structural adjustments to equity variables — monitor H2H impact before iterating further.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- New EV-based decision branches (e.g., `select_postflop_facing_bet()`) are acceptable when replacing unconditional returns with grounded EV computation — but must actually use all received parameters in the calculation. (v43)
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations. (v36)

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Hash-based randomization for bluff frequency is deterministic and exploitable — prefer game-state entropy. (v37)
- Archetype-aware bluff cutoff (never bluff CS, boost vs NIT) is highest-confidence change from v40.

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, not retuning.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- Fold margin, clamp, EQR, SPR-commitment guard, sizing_aggr deltas have failed v30→v38. [EXHAUSTED] [POSSIBLY EXHAUSTED]
- Hand-tuned constants in structural modules are still parameter tuning — wiring pre-existing EXHAUSTED constants into new code violates this rule. [EXHAUSTED] [POSSIBLY EXHAUSTED]
- New constants introduced in EV branches (e.g., pot*0.05, 0.3 implied odds in v43) risk the same EXHAUSTED pattern — avoid adding magic numbers without H2H backing.

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
- **v43**: EV-based action selectors must gate raises by opponent archetype — raising into calling stations with nut/strong value bonus is exploitable; Critic flagged this from pool's "never bluff CS" lesson.
- **v43**: `select_postflop_facing_bet()` receives `has_position` and `board_texture` as parameters but never uses them in EV computation — wiring these is needed to discount OOP raises and adjust fold-equacy on wet textures.
- **v42**: `river_showdown_extraction()` targets wide opponents (VPIP≥0.50) with 25-40% pot bets — evaluate whether thin-value sizing is exploitable by pool leader bluff-raises.
- **v41**: Archetype fold deltas integrated as `eff_made = made_strength - archetype_delta` — follow this pattern for future archetype wiring.
- **v41**: Monitor whether archetype classifier reaches confidence ≥ 0.15 within first 30 hands vs lag/CS — if not, fold adjustments never activate.
