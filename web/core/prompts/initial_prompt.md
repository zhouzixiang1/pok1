# Game Parameters
- 70 hands per match, 20000 starting chips per hand (一局一复位)
- Blinds: small blind 50, big blind 100
- Heads-up: dealer=SB acts first preflop; BB acts first on all postflop streets
- Players alternate SB/BB roles each hand

# Task
Create a robust, multi-file Texas Hold'em poker bot in the `bots/claude_v1/` directory.

# Requirements
1. **Multi-file Architecture**: 
   - `bots/claude_v1/main.py`: The entry point that reads from stdin and writes to stdout.
   - `bots/claude_v1/preflop.py`: Handles preflop hand evaluation.
   - `bots/claude_v1/postflop.py`: Handles postflop logic and win rate estimation.
2. **Protocol**:
   - Read JSON from `stdin`. Example request:
     `{"requests": [{"my_cards": [12, 35], "public_cards": [3, 22, 48], "history": [], "my_chips": 20000}], "responses": []}`
   - Output JSON to `stdout`. Example response:
     `{"response": 100}`
   - Actions: `0` (call/check), `-1` (fold), `-2` (all-in), `>0` (raise-to-total: 加注到的阶段总额).
   - In `national_primary`, the final precommit gate runs national 70-hand matches. Phase 1 national execution is handled by `sever/bot_adapter.py`; keep this JSON protocol unless the phase plan explicitly assigns TCP-native bot work. Do not output TCP strings from the bot.
   - National rules from `sever/国赛平台/`: 70 hands, 20000 chips reset each hand, blinds 50/100, SB acts first preflop, BB acts first postflop. TCP wire actions are only `raise <amount>`, `fold`, `call`, `check`, `allin`; `bet` is illegal and strategy bets must be represented as positive raise-to-total values.
   - Raise/all-in legality: first preflop raise-to >= 200, first postflop raise-to >= 100, every re-raise must be strictly greater than 2x the previous raise-to (`prev * 2 + 1` minimum), positive raises must not consume all remaining chips, and all-in must be `-2`.
   - Call/check legality: postflop first action cannot be call; postflop after any first action, check is illegal. If the first postflop player checks, the second player passes the street with call, not another check. Preflop BB cannot call after SB limps/calls; BB checks, raises, or folds.
   - All-in legality: after one player all-ins, the opponent may only call or fold; consecutive all-ins are illegal.
3. **Execution**:
   Please create these files and write functional baseline code. Make sure the logic is separated cleanly and the bot does not crash.
