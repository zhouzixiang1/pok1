# Evolution Experience Pool
This file contains lessons learned from previous iterations of the poker bot. 
The Master Bot Architect must read this before planning the next generation to avoid repeating past mistakes.

### History of Strategies

#### Generation claude_v6 → claude_v7 (2026-05-25)
- **Performance**: claude_v6 is Rank 1 (ELO 1533.2) but loses badly to claude_v2 (0.2 win rate). Dominates v1/v4/v5 (0.8-1.0) and beats v3 (0.8).
- **Root Cause of v2 Loss**: claude_v6 is missing 5 key algorithmic features that claude_v2 has:
  1. **No bb_vs_raise / sb_vs_reraise preflop spots**: v6 has ZERO dedicated 3bet/4bet logic. When v2 raises preflop, v6 falls through to generic logic, calling too loosely or folding poorly. This is the #1 exploit.
  2. **Overly conservative thin value sizing**: v6 caps thin value bets at ratio 0.30, while v2 uses 0.46+. This leaves significant money on the table with thin value hands.
  3. **Optimistic realized equity**: v6 overestimates equity OOP and facing double barrels (air EQR 0.62 OOP vs v2's 0.56; marginal pair 0.78 vs 0.73). This leads to too many hero calls.
  4. **No CBet tracking**: v2 tracks cbet_rate and fold_to_cbet in opponent model, enabling better flop play. v6 has none of this.
  5. **No concept drift detection**: v2 detects when opponent changes strategy mid-match. v6 uses all-time averages blindly.
- **Lesson**: Preflop spot coverage is critical. Missing dedicated 3bet/4bet logic creates a massive exploit against aggressive preflop opponents like v2.
- **Lesson**: Realized equity discount factors need to be more aggressive OOP and in big pots to avoid burning chips on marginal calls.
- **Lesson**: The thin_cap parameter in choose_raise was WAY too low at 0.30, causing systematic underbetting of thin value hands.

- **v6 -> v7**: 1) Preflop 3bet/4bet logic is essential — without it, aggressive opponents like v2 exploit the hole for massive win rate advantage. 2) thin_cap=0.30 in choose_raise was far too conservative, causing systematic thin value underbetting — v2 uses 0.46+. 3) Realized equity must be more aggressively discounted OOP, facing double barrels, and in big pots. 4) CBet tracking and drift detection in opponent modeling are important for adapting mid-match. 5) Safe exploitation frameworks (interpolating between GTO baseline and exploit) prevent over-adjusting to noise in early hands.
- **v7 -> v8**: ### Generation claude_v7 → claude_v8 (2026-05-25)
- **Performance**: claude_v7 is Rank 3 (Elo 1508.1). Loses badly to v2 and v3 (both 0.2 win rate). Dominates v4/v6 but only 0.6 vs v1, 0.4 vs v5.
- **Root Cause of v2/v3 Loss**: Despite porting 5 features from v2, the remaining gap is the preflop hand evaluation. v2 uses a 169-hand Chen-formula lookup table that accurately values suited connectors, one-gappers, and offsuit broadways. v7's crude linear formula (high/16 + low/28 + bonuses) systematically undervalues suited hands and overvalues disconnected offsuit cards. This corrupts every preflop decision downstream.
- **Lesson**: Preflop hand strength estimation accuracy is foundational. A 10% error in preflop_strength propagates to open thresholds, 3bet decisions, call ranges, and trash hand detection. The Chen lookup table approach is far superior to the simple formula.
- **Lesson**: min_raise calculation matters for preflop aggression. v2's approach (round_raise = max(round_raise, 2*add)) produces larger minimum 3bet sizes that are harder for opponents to defend against. v7's approach (judge_round_raise = max(judge_round_raise, add)) allows smaller raises that give opponents better pot odds.
- **Lesson**: Reducing preflop simulations from 500 to 400 was a net negative. The extra preflop accuracy matters more than postflop accuracy because preflop decisions set up the entire hand.
- **Lesson**: v7's confidence divisor of 30.0 vs v2's 35.0 means v7 trusts its opponent model too early (after ~15 actions vs ~20), leading to premature exploitation adjustments against unknown opponents.
- **v8 -> v9**: ### Generation claude_v8 → claude_v9 (2026-05-25)
- **Performance**: claude_v8 is Rank 3 (Elo 1521.1). Fixed v2/v3 problem (now 0.8 vs both) but NEW critical loss to v4 (0.2 win rate) and v6 (0.4).
- **Root Cause of v4 Loss**: v8's dedicated bb_vs_raise/sb_vs_reraise logic uses FIXED preflop_strength thresholds (0.72 for 3bet, 0.42 for call) that IGNORE opponent tendencies and simulation-based win_rate. v4 has NO dedicated logic for these spots and falls through to the general simulation-based decision path, which is MORE ACCURATE because it considers opponent range. v8 replaced a good general system with a bad specialized one.
- **Lesson**: Dedicated preflop spot logic MUST use the same simulation-based win_rate as the general path, not simple heuristic thresholds. A specialized system is only worth adding if it makes BETTER decisions than the general fallback. Otherwise, letting it fall through is superior.
- **Lesson**: v8's thin_cap of 0.46+0.08*wetness (when to_call==0) allows oversized thin value bets (~59% pot on river). v4 uses 0.30/0.38 and dominates. Thin value bet sizing should be conservative — thin means marginal, and marginal hands should bet small to avoid value-owning yourself.
- **Lesson**: The Chen lookup table HELPED vs v2/v3 (exploit preflop evaluation) but is NEUTRAL vs v4 (exploits decision logic, not evaluation). Different opponents exploit different layers of the bot.
- **v9 -> v10**: ### Generation claude_v9 → claude_v10 (2026-05-25)
- **Performance**: claude_v9 is Rank 4 (Glicko 1528.6). Code analysis reveals v8 and v9 are STRATEGICALLY IDENTICAL — same choose_preflop_spot_action, same realized_postflop_equity, same choose_raise thin_cap, same cbet_rate tracking. Rating gap is Glicko variance.
- **Root Cause of Gap**: v9 has NO new capabilities vs v8. In a mirror match, identical bots draw 50/50. To break the symmetry, v10 must add NEW algorithmic modules that v8 lacks.
- **Lesson**: A dedicated CBet module is the single highest-impact addition. Currently ~40% of postflop spots fall through to generic logic when the bot is the preflop raiser first to act. Structured CBet decisions (bluff on dry boards vs check-back on wet boards) would immediately create an edge.
- **Lesson**: SB opening range at 0.49 threshold is too tight for heads-up. Top players open 80%+ from SB. Wider SB opening exploits BB overfolding and builds bigger pots with position disadvantage.
- **Lesson**: The exploitation framework (gift_balance/safe_exploitation_lambda) is a novel but weak signal. Direct statistical exploitation using fold_to_raise, fold_to_cbet, and postflop_aggr would be more reliable.
- **Lesson**: Turn barrel decisions are currently generic. Adding scare card detection (cards that complete draws or increase board wetness) and structured give-up logic for weak hands on blanks would improve turn play significantly.
- **Lesson**: River bluff-catching should account for blocker effects. Cards in our hand that block opponent value combos make better bluff-catchers. This is an underexploited edge in the current code.
- **v10 -> v11**: ### Generation claude_v10 → claude_v11 (2026-05-25)
- **Performance**: claude_v10 is Rank 1 (Glicko 1582.6, RD 26.5). Margin over v8 is 15.5 points, over v4 is 20.5 points.
- **Structural Gap #1 - CBet Module**: ~40% of postflop decisions are CBet spots (preflop raiser, first to act on flop). Currently NO structured CBet logic exists — the bot falls through to generic initiative path. A CBet module should: (a) CBet bluff frequently on dry boards with air, exploiting opponent fold_to_cbet, (b) check back marginal hands on wet/dynamic boards, (c) size CBets smaller on dry boards (~1/3 pot) and larger on wet boards (~2/3 pot), (d) use opponent fold_to_cbet stat for exploitation.
- **Structural Gap #2 - Turn Barrel**: After flop CBet gets called, turn decisions are generic. Need: (a) scare card detection (cards that complete draws or change board texture), (b) double barrel value with hands that remain strong, (c) give up air on blank turns, (d) barrel scare cards as bluffs.
- **Structural Gap #3 - SB Opening Width**: SB open threshold at 0.49 opens only ~55% of hands. Heads-up theory says open 75-85%. Widening to 0.40-0.42 exploits BB overfolding.
- **Structural Gap #4 - River Blocker-Catch**: Blocker logic only used for bluffing, never for calling. Holding cards that block opponent value combos should increase bluff-catch frequency.
- **Lesson**: The exploitation framework (gift_balance/safe_exploitation_lambda) is functional but conservative. It correctly interpolates between GTO baseline and exploit mode. Keep it.
- **Lesson**: The realized_postflop_equity function in strategy.py now includes big_pot and double_barrel discounts, which is an improvement over the backup version. Keep these.
- **Lesson**: The opponent model's cbet_rate and fold_to_cbet tracking in opponent.py is excellent infrastructure. Now it MUST be used for our own CBet decisions, not just opponent profiling.
- **v11 -> v12**: ### Generation claude_v11 → claude_v12 (2026-05-25)
- **Performance**: claude_v11 is Rank 4 (Glicko 1554.5, RD 25.8). WORSE than v10 (1560.3) by ~6 points and significantly behind v4 (1577.3) by ~23 points.
- **Root Cause of Regression from v10**: v11 added dedicated bb_vs_raise/sb_vs_reraise preflop logic that uses fixed preflop_strength thresholds (0.72/0.42/0.85) INSTEAD of simulation-based win_rate. This is the EXACT same mistake that cost v8 against v4 (documented in v8→v9 experience). The simulation-based general path is MORE ACCURATE because it considers opponent range, position, pot odds, and match context.
- **Lesson (REINFORCED)**: Dedicated preflop spot logic MUST use the same simulation-based win_rate as the general path. A specialized system using fixed thresholds is WORSE than falling through to the general simulation-based path. This is now the THIRD time this lesson has appeared (v8, v9, v11).
- **Lesson**: The thin_cap parameter in choose_raise (0.46+0.08*wetness when to_call==0) is too high. v4's 0.30/0.38 values produce better results. Thin value hands should bet SMALL to avoid value-owning. The sweet spot appears to be around 0.36+0.05*wetness.
- **Lesson**: Opponent model priors matter significantly. v11's vpip=0.52/pfr=0.24 assumes tight opponents, leading to overestimation of opponent hand strength and excessive folding. v4's vpip=0.58/pfr=0.28 is more balanced for the typical opponent pool.
- **Lesson**: The safe_exploitation_lambda framework adds complexity with unclear benefit. The gift_balance signal is noisy and can push decisions in wrong directions. Consider removing it.
- **Lesson**: The CBet module was identified as the #1 structural gap since v10 but was NEVER IMPLEMENTED in v11. This is ~40% of postflop decisions when bot is preflop raiser. Without structured CBet logic, the bot is leaving significant edge on the table.
- **Lesson**: Positive changes in v11 to PRESERVE: Chen lookup table, cbet_rate/fold_to_cbet tracking, drift detection, double_barrel/big_pot discounts in realized_postflop_equity, flop cbet_rate call margin adjustments.