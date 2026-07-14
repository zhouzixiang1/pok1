# v080_national_v18_neural_no_propose_tcp

Native national TCP neural experiment forked from `bots/national_v18`.

Change:

- Uses the stronger national v18 rulebase as the runtime base.
- Adds the conservative v076 neural policy/features/weights.
- Calls `apply_neural_advice()` after the rule action and before
  `sanitize_action()`.
- Keeps `multi_action_value_propose_enabled=false`, so the direct preflop
  fold-to-call proposal path remains disabled.
- Keeps direct `national_bot.py` TCP support and does not use
  `sever/bot_adapter.py`.

Interpretation:

- This tests whether the same neural advice is useful when moved from the v17
  base to the current v18 national-native rulebase.
