# v079_national_v17_neural_off_control_tcp

Native national TCP control forked from
`v078_national_v17_neural_no_propose_tcp`.

Change:

- Uses the same national v17 rulebase snapshot as v078.
- Keeps the same native TCP entrypoint shape and neural files on disk.
- Disables runtime neural advice with `enabled=false`.
- Disables `multi_action_value_enabled` and keeps
  `multi_action_value_propose_enabled=false`.

Interpretation:

- This is the v17-base ablation/control for measuring whether v078's neural
  action advice helps or hurts under native TCP evaluation.
