# v084_national_v17_native_state_value_call_probe_tcp

Runtime-trigger probe forked from
`v083_national_v17_native_state_value_tcp`.

Change:

- Uses the same native state-value h16 model as v083.
- Keeps the native national TCP entrypoint and does not use the national
  adapter.
- Lowers only the fold-to-call proposal thresholds so the value head can
  produce actual behavior differences in paired native TCP evaluation:
  `multi_action_value_propose_min=0.04`,
  `multi_action_value_propose_margin_vs_rule=0.02`,
  and paid-call cap 220 chips.
- Still does not allow neural raise or all-in proposals.

Interpretation:

- v084 is a trigger probe. It is expected to be evaluated against v082/v083
  controls before any larger run.
- A zero diff would mean the gate still never fires; a negative diff means the
  seed data is not yet robust enough for runtime use.
