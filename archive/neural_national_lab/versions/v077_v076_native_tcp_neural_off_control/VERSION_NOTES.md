# v077_v076_native_tcp_neural_off_control

Native national TCP ablation/control forked from
`v076_v075_no_propose_native_tcp`.

Change:

- Disables runtime neural advice with `enabled=false`.
- Disables `multi_action_value_enabled` and keeps
  `multi_action_value_propose_enabled=false`.
- Retains the v076 native TCP showdown feature fix, so the bot can complete
  full 70-hand native matches after showdowns.
- Keeps the native `national_bot.py` entrypoint; no `sever/bot_adapter.py`
  bridge is used.

Interpretation:

- This is not a neural-success version. It is the protocol-native control that
  showed the current v075/v076 neural action overrides are negative transfer
  against native national opponents.
- Use it as the current performance baseline while collecting native TCP
  counterfactual/action-value data for the next neural model.
