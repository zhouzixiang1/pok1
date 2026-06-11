## OPPONENT_MODELING
- Opponent adjustments must integrate INTO equity-based fold checks, not layer as separate gates — validated by v34/v41 success, v44 rejection (anti-pattern: sizing profile built but never consumed).
- Monitor whether opponent model reaches confidence within first 30 hands — if not, adjustments never activate.

## POSTFLOP_STRATEGY
- `should_fold_postflop()` is the primary fold gate; exceptions need equity, pot-odds, and confidence validation.
- EV-based selectors must wire ALL received params (position, texture, opponent model) into the calculation — adding new params without using old ones is a recurring defect (v43→v44).
- EV selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations.
- SPR-based commitment logic (SPR<3/6 threshold adjustments) is now integrated into `pot_odds_call_threshold()` — extend, don't duplicate.
- Verify helper functions still exist before targeting them in evolution plans (e.g. `river_showdown_extraction()`).

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Bluff-roll determinism: strategy.py/tournament.py fixed (game-state entropy), but `postflop.py:918` still uses old deterministic formula — open fix.
- Opponent-aware bluff cutoff (never bluff calling stations, boost vs NIT) is highest-confidence change from v40.

## PARAMETER_TUNING
- Base postflop sizing ratios stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths. [POSSIBLY EXHAUSTED]
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- **Systemic failure (v30→v44)**: Workers chronically add hand-tuned constants despite EXHAUSTED warnings. Wiring pre-existing EXHAUSTED constants into new code also counts as parameter tuning. Future workers MUST provide per-constant H2H justification ≥100 games. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure ongoing — extract standalone functions to helper modules before adding new logic.

## RECENT_LESSONS
- **v46**: Critic evidence: H2H weaknesses: v45 has only 440 total games; weakest matchups at 40% WR (v44, v32, v27, v37) are all 10-game samples — not statistically significant per experience pool rule 'H2H data below 100 games is directional only'; Experience pool refs: RECENT_LESSONS v45: 'Pot-odds-based fold decisions are the correct structural direction — extend this pattern' — the river fold extends this pattern, PARAMETER_TUNING: 'Systemic failure (v30→v44): Workers chronically add hand-tuned constants despite EXHAUSTED warnings' — the safety margins (0.08/0.04/0.06/0.04) are hand-tuned without ≥100 game H2H justification, GENERAL: 'H2H data below 100 games is directional only' — v45 has no matchups with ≥100 games; Diff refs: strategy.py:600-606 — BB preflop filter: blocks unsuited disconnected hands (gap≥4, high≤J) from BB call range, uses preflop_hand_profile() structural check, strategy.py:1077-1096 — Inline river equity fold: compares made_strength against pot_odds + safety (base 0.08, +0.04 OOP, +0.06 medium/large bet, +0.04 multi-barrel), bypasses anti_lock_call_continue and strong_made_continue guards that protect downstream should_fold_postflop folds
- **v45**: Pot-odds-based fold decisions (equity × EQR vs pot_odds + safety) are the correct structural direction — extend this pattern to other fold gates rather than reintroducing fixed thresholds. v45 consolidates 14 hardcoded fold thresholds into one comparison with EQR adjustment; SPR logic already integrated via spr<3/6 threshold deltas.
- **v45**: H2H data shows broad underperformance vs multiple opponents (10-20 games each, directional only), suggesting systemic postflop defense weakness — address structurally, not via threshold tuning.
- **v44**: Bluff-roll determinism fixed in strategy.py/tournament.py, postflop.py:918 still deterministic — open fix remains.

