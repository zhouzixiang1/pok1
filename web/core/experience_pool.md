# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING
- Opponent tracking data must be wired into decision logic, not just collected. Scale adjustments (e.g., 0.06 * clamp(...)) outperform flat per-street tweaks. CBet fold-more exploitation has max effect ~0.015 — too small to matter alone. [POSSIBLY EXHAUSTED]

## POSTFLOP_STRATEGY
- Fold discipline is critical — calling-station behavior (0% postflop fold rate) is fatal. Postflop fold gates + EQR tightening drove 1700→1831 (best). [POSSIBLY EXHAUSTED]
- Draw call margins must be grounded in equity math vs pot odds. Use has_draw guards in tier-based fold systems. Avoid overlapping fold gates — always check existing guards before adding new ones.
- Pot-odds calibration via proportional scaling outperforms per-street thresholds. Texture-aware bonuses (+0.03 flush, +0.02 straight draw) add equity-based reasoning on top of raw thresholds.

## BLUFF_CALIBRATION

## PARAMETER_TUNING
- Postflop sizing ratios (flop 0.60, turn 0.70, river 0.85) are well-tuned. Size up only when opponent fold data supports it. [POSSIBLY EXHAUSTED]
- SB open threshold 0.49 calibrated; safe step 0.49→0.45. Sizing coefficient 1.8→2.2 has real pair-sizing impact (+5%); non-pair cap changes negligible.
- Preflop 3bet threshold 0.60 (TT+, AKs) solid. Never call off 100BB with 51% hand vs over-shove.
- BB call_threshold widened 0.42→0.37 — monitor for over-defense.

## GENERAL
- Worker role boundaries CRITICAL: Tuner must change ≥1 constant; Architect must NOT touch constants. Violations waste entire generations.
- Crossover bots need full pipeline (gates→review→critic→commit→archivist) for git tags and version tracking. Crossover formula caps are safer than full table replacement when source bot is weaker.
- sanitize_action(): action=0 (call) must be allowed when facing all-in, preventing forced folds on callable all-ins.

## RECENT_LESSONS
- **v12**: Critic evidence: H2H weaknesses: v11 vs v6: 42% WR (50 games) — v6 exploits v11's lack of preflop defense, v11 vs v2: 48% WR (50 games) — v2 has structured bb_vs_raise handler; Experience pool refs: 'Preflop 3bet threshold 0.60 solid' — confirms value 3bet range, 'BB call_threshold widened 0.42→0.37 — monitor for over-defense' — consistent with 0.37 base, 'Fold-more pattern repeated across multiple gens with diminishing returns [POSSIBLY EXHAUSTED]' — should_fold_postflop() is partially on this pattern; Diff refs: New bb_vs_raise handler (lines 443-472): value 3bet ≥0.60, bluff 3bet 0.38-0.54 with fold_to_raise exploitation, call ≥0.37, New sb_vs_reraise handler (lines 474-489): 4bet ≥0.78, call ≥0.55 at ≤15% stack, fold rest, New should_fold_postflop() (lines 494-533): per-street thresholds with texture bonuses + _fold_conservatism=0.05
- **v12**: Crossover v11×v6 — v11 base + v6 fold discipline. Added bb_vs_raise handler (value 3bet ≥0.60, bluff 3bet 0.38-0.54, call 0.37), sb_vs_reraise (4bet ≥0.78, call ≥0.55), should_fold_postflop() with per-street thresholds (0.20/0.25/0.35 vs medium+large, 0.22/0.28/0.40 vs 2+ bets). Postflop call margins increased: weak_showdown 0.012→0.020, air_hand 0.018→0.028.
- **v12**: H2H weaknesses identified — v11 vs v6: 37-40% WR (v6 fold discipline exploits calling-station tendencies), v11 vs v2: 42.5% (weak preflop defense), v11 vs v5: 30% (v6 beats v5 at 52% — fold discipline is the differentiator).
- **v11**: v4 has no H2H matchup below 50% WR — no confirmed weakness to exploit. v10/v8/v9 are proven losers; crossover from them is risky. Fold-more pattern repeated across multiple gens with diminishing returns. [POSSIBLY EXHAUSTED]

