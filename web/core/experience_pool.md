## OPPONENT_MODELING
- Opponent adjustments must integrate INTO equity-based fold checks with passive-bot safeguards — v49 retains opponent-model fold (barrel_freq adjustment in should_fold_postflop). Any changes must prove no regression vs calling stations via ≥100-game H2H.
- Confidence ramp is action-based: `clamp((total_actions - 5) / 35.0, 0, 1)`, reaching full confidence at ~40 opponent actions — design opponent-aware logic around this actual mechanism.

## POSTFLOP_STRATEGY
- v49 removed SPR commitment fold from `should_fold_postflop()` — tier-based equity (hand_strength_tier + estimate_equity_from_tier), opponent-model fold (barrel_freq), and multi-barrel action-sequence fold are all still present. Do NOT re-add these as new features. [POSSIBLY EXHAUSTED]
- EV-based selectors must wire ALL received params (position, texture, opponent model) — adding new params without using old ones is a recurring defect (v43→v44).
- EV selectors must gate raises by opponent type — raising into calling stations with value bonus is exploitable.
- Draw-call margins must be grounded in equity vs pot odds with `has_draw` guards.
- Removing all-in equity checks is dangerous — preserve pot-odds thresholds for shove situations.

## BLUFF_CALIBRATION
- Structural bluff modules (4-bet light, donk/probe, overbet, barrel) need ≥100-game H2H backing before targeting a matchup.
- Opponent-aware bluff cutoff (never bluff calling stations, boost vs NIT) consistently validated v40–v49.

## PARAMETER_TUNING
- Base postflop sizing ratios stable (flop 0.60, turn 0.70, river 0.85); extend via structural paths.
- Preflop 3bet threshold ~0.60 (TT+, AKs) is solid; never call off 100BB with ~51% equity vs over-shove.
- **Systemic failure (v30→v49)**: Workers chronically add hand-tuned constants despite [EXHAUSTED] warnings. Wiring pre-existing EXHAUSTED constants into new code also counts as tuning. Must provide per-constant H2H justification ≥100 games. [EXHAUSTED]

## GENERAL
- Worker role boundaries: Tuner must change at least one constant; Architect must not touch constants.
- Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores are more reliable than retry approvals.
- H2H data below 100 games is directional only; require ≥100-game confirmation before targeting matchups.
- Structural changes can inflate Critic scores without improving battle performance; verify H2H effect.
- Strategy.py capacity pressure ongoing — extract standalone functions to helper modules before adding new logic.
- Verify helper functions still exist before targeting them in evolution plans.

## RECENT_LESSONS
- **v50**: Critic evidence: H2H weaknesses: v47 loses to v30: 45.0% WR (80 games) — v30 is a high-barrel aggressive bot, v47 loses to v21: 45.6% WR (90 games) — v21 has overall 54.3% win rate, v47 loses to v20: 45.6% WR (90 games), v16: 46.0% (100 games), v18: 46.4% (110 games), These losses cluster against aggressive opponents, confirming over-folding vs barrel aggression; Experience pool refs: [POSSIBLY EXHAUSTED] SPR commitment fold removed in v49 — this change adds back un-gated calling vs barrels, v47 action-sequence fold gates 'layered as separate gates' with 'over-folding risk vs passive bots' — bluff-catch partially addresses this but introduces opposite risk (under-folding vs value), H2H data below 100 games is directional only; v49 has 0 rated games per experience pool; Diff refs: strategy.py:588 — BB_VPID_FOLD_ADJUST_SCALE (NameError in v47) replaced with literal 0.04, strategy.py:679-731 — NEW detect_bluff_catch_signal(): 5-factor signal, threshold 0.45, strength window 0.28-0.55, strategy.py:1171-1179 — Bluff-catch override inserted BEFORE inline river fold and action-sequence fold, preempts them with return 0 (call)
- **v49**: Removed SPR commitment fold from `should_fold_postflop()` — the only component actually removed. Tier-based equity, opponent-model fold, and multi-barrel fold remain active in current code. No H2H data exists for v49 (0 rated games, RD=357.83); no performance claims can be validated yet.
- **v47**: Action-sequence fold gates layered as separate gates instead of integrated — retained in v49 but with over-folding risk vs passive bots. Safety margins (0.08/0.04/0.06/0.04) lacked ≥100-game H2H validation.

