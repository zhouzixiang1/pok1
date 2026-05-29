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
   - Actions: `0` (call/check), `-1` (fold), `-2` (all-in), `>0` (raise amount).
3. **Execution**:
   Please create these files and write functional baseline code. Make sure the logic is separated cleanly and the bot does not crash.
