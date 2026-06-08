# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Per-street profiling (flop/turn/river aggr, barrel_freq) is infrastructure — tune coefficients, don't rebuild. [POSSIBLY EXHAUSTED]
- CBet fold-more exploitation max effect ~0.015; betsize exploit ±0.04 too small alone. [POSSIBLY EXHAUSTED]
- Light 4-bet and check-raise trap use opponent PFR + aggression reads — structural features, not threshold micro-adjustments.
- Weakest matchups are passive bots (v4/v5/v6/v8). Exploitative adjustments on turn/river must be priority. v19's passive_opponent_exploit_bonus (capped 0.08, confidence-gated ≥0.20) is correct pivot direction — verify with ≥100 games.
- Per-street big-bet tracking (≥6/8/10 BB) with smooth_rate priors is useful infrastructure — keep as data input, not fold gate.

## POSTFLOP_STRATEGY
- should_fold_postflop() is the single fold gate. Any fold/call override placed BEFORE it bypasses all guards and produces dead parameters — SPR commitment, sizing_fold, equity manipulation all failed this way. All fold gates must live INSIDE should_fold_postflop, no exceptions.
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems. Avoid overlapping fold gates.
- repeated_raise_trap: 3-tier fold/call/raise logic leaks ~51% vs v4. All fold/raise guards must verify branch consistency within same decision block.
- Check-raise trap on dry flops for strong/nut hands returns 0 (check) — needs safety threshold on opponent confidence before trapping.
- Removing a double-dip (opponent model applied twice) is architecturally correct but neutral for performance — implicit compensation may be lost.

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
- Crossover strategy (v8→v14) and v6 fold-discipline injection both exhausted. [POSSIBLY EXHAUSTED]
- Trust early negative critic signals — first-rejection scores often more accurate than retry approvals. Preflop cap removal risks over-inflating AK/AQ.
- H2H weakness data unreliable with small samples (10-20 games). Targeted changes need targeted evidence, not assumed weaknesses.

## RECENT_LESSONS
- **v20**: Critic evidence: H2H weaknesses: v15 vs v18: 46% win rate (50 games) — v18's fold discipline exploits v15's calling station, v15 vs v14: 49.2% (120 games) — reliable sample showing vulnerability to aggressive opponents, v15 vs v6 (passive): 50.8% (130 games) — sizing mutation targets this gap; Experience pool refs: should_fold_postflop() is the single fold gate — all fold gates must live INSIDE it, no exceptions (v20 correctly places SPR/opponent-model gates inside), Fold margin / clamp value tuning repeatedly attempted with no measurable gain [POSSIBLY EXHAUSTED] — v20's changes are structural, not threshold tuning, v18: SPR/sizing_fold gates before should_fold_postflop created dead parameters — v20 fixes this by placing inside the function; Diff refs: should_fold_postflop() signature expanded with opponent_model/spr params; 3 new fold blocks inside function body (lines 619-639 of v20), repeated_raise_trap: added fold path for made_strength < 0.25, draw_strength < 0.14, medium/large trap_size (line 987-989 of v20), choose_raise(): calling-station value sizing +0.06 ratio for strong/nut hands vs passive callers (lines 405-409 of v20)
- **v20**: Critic rejected two preflop changes (SB suitedness gate for limp, BB small-raise call_floor 0.25). H2H evidence all 10-20 game samples — unreliable. Changes targeted preflop defense space where fold-margin tuning is exhausted. Future preflop work must either target turn/river exploitation (where weakest matchups are) or have ≥100-game H2H backing.
- **v19**: SB limp-then-face-raise was misclassified as sb_vs_reraide (0.78/0.55 thresholds) — keep limp-raise and raise-reraise paths distinct. passive_opponent_exploit_bonus + sb_limp_vs_raise handler added. After ≥100 games, if passive exploit works but regresses vs aggressive bots (v2/v7/v17), gate wider thresholds behind higher confidence (≥0.35). Potentially exploitable by opponents that shift aggression after small-sample passive signals.
- **v18**: Clamp narrowing is fold-margin tuning (exhausted pattern). SPR/sizing_fold gates before should_fold_postflop created dead parameters. Double-dip barrel penalty removal correct but neutral.

