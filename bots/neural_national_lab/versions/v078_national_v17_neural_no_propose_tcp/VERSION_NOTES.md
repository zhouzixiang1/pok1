# v078_national_v17_neural_no_propose_tcp

Native national TCP neural experiment forked from `bots/national_v17`.

Change:

- Uses the strong national-native v17 rulebase as the runtime base.
- Adds the v076 neural feature/policy files and weights.
- Calls `apply_neural_advice()` after the rule action and before
  `sanitize_action()`.
- Keeps `multi_action_value_propose_enabled=false`, so the risky direct
  preflop fold-to-call proposal path remains disabled.
- Keeps a direct `national_bot.py` TCP entrypoint and does not use
  `sever/bot_adapter.py`.

Interpretation:

- This is the first neural lab version moved onto the active national-native
  rulebase instead of the older v254/Botzone-era base.
- It should be evaluated only through native TCP protocol opponents.
