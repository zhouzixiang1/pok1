# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling (flop/turn/river aggr, barrel_freq) is infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]
- CBet fold-more exploitation max effect ~0.015; betsize exploit ±0.04 too small alone. [POSSIBLY EXHAUSTED]
- Light 4-bet and check-raise trap use structural reads (PFR + aggression), not threshold micro-adjustments.
- Weakest matchups are passive bots (v4/v5/v6/v8). Exploitative turn/river adjustments must be priority.
- Per-street big-bet tracking (≥6/8/10 BB) with smooth_rate priors is useful as data input, not fold gate.

## POSTFLOP_STRATEGY
- should_fold_postflop() is THE single fold gate. Any fold/call override BEFORE it bypasses all guards — SPR, sizing_fold, equity manipulation all failed this way. No exceptions.
- Overlapping fold gates with close thresholds (0.25 vs 0.28) create redundancy — use unified threshold tables or priority-ordered gates.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems.
- repeated_raise_trap: 3-tier fold/call/raise logic leaks ~51% vs v4. Verify branch consistency within same decision block.
- Check-raise trap on dry flops for strong/nut hands needs safety threshold on opponent confidence before trapping.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) well-tuned. Size up only when opponent fold data supports it. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%).
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense.
- Fold margin / clamp value tuning repeatedly attempted with no measurable gain. [POSSIBLY EXHAUSTED]

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking.
- sanitize_action(): action=0 (call) must be allowed when facing all-in.
- Crossover strategy and fold-discipline injection both exhausted. [POSSIBLY EXHAUSTED]
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.
- H2H weakness data unreliable with small samples (10-20 games). Targeted changes need ≥100-game backing.

## RECENT_LESSONS
- **v21**: Critic evidence: H2H weaknesses: v18 vs v4: WR=0.560 (50g) — passive calling station, underperforming vs top-rated bot, v18 vs v8: WR=0.571 (70g) — passive bot, similar underperformance, v18 vs v6: WR=0.600 (60g) — passive bot, moderate edge only, v18 vs v16: WR=0.480 (50g) — weakest matchup, though change doesn't specifically target aggressive opponents; Experience pool refs: EXPERIENCE_POOL: 'Weakest matchups are passive bots (v4/v5/v6/v8). Exploitative turn/river adjustments must be priority.' — directly addressed by passive_exploit_thin_value, EXPERIENCE_POOL: 'Fold margin / clamp value tuning repeatedly attempted with no measurable gain. [POSSIBLY EXHAUSTED]' — this change goes beyond tuning via structural bypass of thin_static_showdown_control, EXPERIENCE_POOL: 'Crossover strategy and fold-discipline injection both exhausted. [POSSIBLY EXHAUSTED]' — risk flag, though the thin value mutation is a novel structural path; Diff refs: state.py lines 88-97: classify_preflop_gap_hand() identifies Broadway+low hands with gap≥5 (J6s, K5o, A6o, etc.) as trouble holdings, strategy.py lines 483-488: SB open — gap Broadway hands limp instead of raise (suited: strength≥limp_threshold-0.05, offsuit: strength≥open_threshold), strategy.py lines 587-594: New sb_limp_vs_raise spot — tight range (premiums≥0.62 call, playable≥0.35 with pot_odds<0.25 call, gap Broadway folds)
- **v21**: Critic evidence: H2H weaknesses: v18's weakest matchups: v12 (wr=0.327, 110g), v10 (wr=0.400, 100g), v11 (wr=0.408, 120g) — all with ≥100 game samples, v18 beats passive bots v4-v6 at only 56-60% — room for improvement vs calling stations, v15 rates 1722 vs v18's 1666 — v15 excels vs passive bots (v4: 60%, v8: 62.9%, v2: 62.9%); Experience pool refs: EXPERIENCE_POOL: 'Weakest matchups are passive bots (v4/v5/v6/v8). Exploitative turn/river adjustments must be priority.' — directly addressed by passive_exploit_thin_value, EXPERIENCE_POOL: 'Fold margin / clamp value tuning repeatedly attempted with no measurable gain. [POSSIBLY EXHAUSTED]' — this change goes beyond tuning via structural bypass of thin_static_showdown_control, EXPERIENCE_POOL: 'Crossover strategy and fold-discipline injection both exhausted. [POSSIBLY EXHAUSTED]' — risk flag, though the thin value mutation is a novel structural path; Diff refs: strategy.py line 91: Clamp widened (-0.09, 0.11) → (-0.12, 0.15) — from v15, enables stronger exploitative adjustments, strategy.py lines 293-318: New EQR air-hand block using barrel_freq, avg_river_raise_bb, river_aggr with _aligned_signal_boost for signal reliability. Clamped (0.45, 0.85) to prevent runaway, strategy.py lines 1188-1200: passive_exploit_thin_value bypasses thin_static_showdown_control on turn vs confirmed passive opponents (7 guard conditions), allowing thin value bets instead of check-back
- **v21**: passive_exploit_thin_value bypasses thin_static_showdown_control for passive opponents on turn (to_call==0, confidence≥0.25, postflop_aggr≤0.30, vpip≥0.50, made_strength≥0.40, nutted_risk≤0.05) → bets 70% pot instead of checking back. New decision paths avoid exhausted fold-margin tuning.
- **v20**: Crossover of single-file-differing parents (v15 vs v18: only strategy.py) is clean and low-risk — target future crossovers where divergence is concentrated in 1-2 files.
- **v20**: Critic rejected preflop changes (SB suitedness gate, BB call_floor 0.25) — H2H evidence all 10-20 game samples, unreliable. Future preflop work needs ≥100-game backing.
- **v19**: SB limp-then-face-raise misclassified as sb_vs_reraise — keep limp-raise and raise-reraise paths distinct. passive_opponent_exploit_bonus (capped 0.08, confidence≥0.20) added; gate wider thresholds behind higher confidence (≥0.35) if regresses vs aggressive bots.


