## OPPONENT_MODELING
- Archetype classification should be exploited at more decision points — river fold (v34), flop check-raise (v40), flop c-bet still open.
- Archetype adjustments must integrate INTO equity-based fold checks, not layer as a separate gate. Pattern: `eff_made = made_strength - archetype_delta`. Validated by v34/v41 success and v44 rejection (attempted the exact anti-pattern).
- Archetype fold deltas (0.02–0.04) are structural adjustments to equity variables — monitor H2H impact before iterating further. [POSSIBLY EXHAUSTED]
- Monitor whether archetype classifier reaches confidence ≥ 0.15 within first 30 hands vs lag/CS — if not, fold adjustments never activate.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- EV-based selectors must wire ALL received parameters (position, texture, archetype) into the calculation — adding new params without using old ones is a recurring defect (v43→v44).
- EV selectors must gate raises by opponent archetype — raising into calling stations with nut/strong value bonus is exploitable.
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- **TODO**: Hash-based randomization for bluff frequency is deterministic and exploitable — verify current bot uses game-state entropy instead; if not, this is an open fix, not a closed lesson.
- Archetype-aware bluff cutoff (never bluff CS, boost vs NIT) is highest-confidence change from v40.

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, not retuning. [POSSIBLY EXHAUSTED]
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- **Systemic failure (v30→v44)**: Workers chronically add hand-tuned constants despite EXHAUSTED warnings — e.g., v44 added 7 constants plus 5 thresholds without EV/pot-odds basis and with <100-game H2H data. Wiring pre-existing EXHAUSTED constants into new code also counts as parameter tuning. Future workers MUST provide per-constant H2H justification ≥100 games. This is the #1 source of wasted generations. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting specific matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Evolving from pool's weakest bot adds strategic risk — consider branching from stronger ancestor.
- Strategy.py capacity pressure is ongoing — extract standalone functions to helper modules before adding new logic.

## RECENT_LESSONS
- **v44**: Critic evidence: H2H weaknesses: v29 weakest: vs v34(0.480), v38(0.483), v37(0.486), v14(0.487), v35(0.487), v36(0.487), v20(0.488), v20 vs same opponents: v34(0.527), v38(0.487), v37(0.500), v14(0.510), v35(0.509), v36(0.520), v20 marginally better vs v34/v36 but also loses to v38 — crossover fold gates not clearly the cause; Experience pool refs: BLUFF_CALIBRATION line 16: 'Hash-based randomization for bluff frequency is deterministic and exploitable — verify current bot uses game-state entropy instead; if not, this is an open fix' — THIS IS NOW FIXED, POSTFLOP_STRATEGY line 8: 'should_fold_postflop() is the primary fold gate; exceptions need equity, pot-odds, and confidence validation' — new gates use arbitrary thresholds not pot-odds, PARAMETER_TUNING line 22: 'Systemic failure (v30→v44): Workers chronically add hand-tuned constants despite EXHAUSTED warnings' — thresholds 0.28/0.34/0.42 are hand-tuned; Diff refs: strategy.py:586-589 — bluff_roll hash fix: adds hand_idx + my_chips entropy (addresses experience pool TODO), strategy.py:678-698 — Three v20 crossover fold gates (SPR commitment, opponent-model-aware, river multi-barrel), strategy.py:1105-1109 — Repeated-raise-trap now folds < 0.25 equity + no draw + medium/large sizing (was unconditional call)
- **v44 (REJECTED, Critic 3.0)**: Two failed attempts both violated EXHAUSTED guidance. Defects: separate sizing-exploit fold gate placed after all return-True gates (unreachable dead code), default return identical to no-op, `classify_opponent_sizing()` builds profile but never effectively consumed. Reinforces: archetype sizing exploits must integrate INTO equity checks, not layer as separate gates.
- **v43**: `select_postflop_facing_bet()` had `has_position` and `board_texture` params unused — wire existing params before introducing new ones.
- **v42**: `river_showdown_extraction()` thin-value sizing (25-40% pot vs wide opponents) — evaluate exploitability by pool leader bluff-raises before iterating. Verify function still exists before targeting.

