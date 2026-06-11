## OPPONENT_MODELING
- Opponent adjustments must integrate INTO equity-based fold checks, not layer as separate gates — validated by v34/v41 success and v44 rejection (anti-pattern).
- Monitor whether opponent model reaches confidence within first 30 hands — if not, adjustments never activate.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- EV-based selectors must wire ALL received parameters (position, texture, opponent model) into the calculation — adding new params without using old ones is a recurring defect (v43→v44).
- EV selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations.
- Replacing arbitrary equity thresholds (0.28, 0.34, 0.35) with pot-odds-derived call thresholds is a structural change (not parameter tuning) — this is the recommended next step for turn-barrel defense.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Bluff-roll determinism partially fixed in v44: strategy.py and tournament.py now use `hand_idx + my_chips` entropy, but `postflop.py:918` still uses old deterministic formula — this remains an open fix.
- Opponent-aware bluff cutoff (never bluff calling stations, boost vs NIT) is highest-confidence change from v40.

## PARAMETER_TUNING
- Base postflop sizing ratios are stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths, not retuning. [POSSIBLY EXHAUSTED]
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- **Systemic failure (v30→v44)**: Workers chronically add hand-tuned constants despite EXHAUSTED warnings. Wiring pre-existing EXHAUSTED constants into new code also counts as parameter tuning. Future workers MUST provide per-constant H2H justification ≥100 games. #1 source of wasted generations. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting specific matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure is ongoing — extract standalone functions to helper modules before adding new logic.

## RECENT_LESSONS
- **v45**: Critic evidence: H2H weaknesses: v44 loses to v29 (40%), v28 (40%), v26 (40%), v16 (40%), v41 (40%), v25 (45%), v34 (45%) — consistent across opponents, suggesting a systemic postflop defense issue rather than a single matchup problem., Most H2H data has only 10-20 games (below 100-game threshold), so directional only. The broad pattern of underperformance is suggestive.; Experience pool refs: EXPERIENCE_POOL > POSTFLOP_STRATEGY: 'Replacing arbitrary equity thresholds (0.28, 0.34, 0.35) with pot-odds-derived call thresholds is a structural change (not parameter tuning) — this is the recommended next step for turn-barrel defense.' — directly implemented., EXPERIENCE_POOL > OPPONENT_MODELING: 'Opponent adjustments must integrate INTO equity-based fold checks, not layer as separate gates' — v45 integrates opponent barrel frequency into the EQR factor rather than a separate if-branch., EXPERIENCE_POOL > POSTFLOP_STRATEGY: 'Fold gates layered as separate equity-threshold checks produce redundancy and over-folding; must integrate into existing thresholds or use pot-odds basis.' — v45 consolidates 14 gates into 1 comparison.; Diff refs: strategy.py:should_fold_postflop — v44 had 14 return True paths with hard-coded thresholds (0.20, 0.22, 0.25, 0.28, 0.30, 0.32, 0.34, 0.35, 0.38, 0.40). v45 replaces with: `return realized_equity < pot_odds + safety` where equity is estimated from made_strength/win_rate and adjusted by EQR., strategy.py:L1278-1286 — New river anti-lock all-in cap: non-nut hands capped at pot-sized raise instead of all-in., strategy.py:L1480-1490 — New river raise cap: non-nut hands raising ≥60% of stack are capped at 1.0x pot (strong tier) or 0.75x pot (thin/none tier).
- **v44**: Bluff-roll determinism fixed in strategy.py/tournament.py (game-state entropy), but postflop.py:918 still deterministic — open fix remains.
- **v44**: Fold gates layered as separate equity-threshold checks produce redundancy and over-folding; must integrate into existing thresholds or use pot-odds basis.
- **v44 (REJECTED, Critic 3.0)**: Two failed attempts violated EXHAUSTED guidance — separate sizing-exploit fold gate was unreachable dead code; `classify_opponent_sizing()` built profile never consumed. Reinforces: opponent sizing exploits must integrate INTO equity checks.
- **v43**: `select_postflop_facing_bet()` had `has_position` and `board_texture` params unused — wire existing params before introducing new ones.
- **v42**: `river_showdown_extraction()` thin-value sizing (25-40% pot vs wide opponents) — verify function still exists before targeting.

