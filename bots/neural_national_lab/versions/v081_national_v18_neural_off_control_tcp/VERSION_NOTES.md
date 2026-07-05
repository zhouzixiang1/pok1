# v081_national_v18_neural_off_control_tcp

Native national TCP control forked from `v080_national_v18_neural_no_propose_tcp`.

Change:

- Uses the same national v18 rulebase snapshot as v080.
- Keeps the same native entrypoint shape and neural files on disk.
- Disables runtime neural advice with `enabled=false`.
- Disables `multi_action_value_enabled` and keeps
  `multi_action_value_propose_enabled=false`.

Interpretation:

- This is the v18-base ablation/control for measuring whether v080's neural
  action advice helps or hurts under paired native TCP evaluation.
