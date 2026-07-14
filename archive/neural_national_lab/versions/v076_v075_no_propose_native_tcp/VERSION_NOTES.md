# v076_v075_no_propose_native_tcp

Native national TCP neural experiment forked from
`v075_v254_activepos_call_rescue_p040_h16`.

Change:

- Keeps the v075 neural policy and multi-action value model files.
- Disables `multi_action_value_propose_enabled`, removing the direct preflop
  fold-to-call proposal path that underperformed in native TCP sweeps.
- Makes showdown feature extraction accept the native TCP entry's
  `opponent_cards` field instead of accidentally treating the numeric hand
  index as cards.
- Keeps the native `national_bot.py` entrypoint; no `sever/bot_adapter.py`
  bridge is used.

Evaluation target:

- Compare only through native national TCP protocol opponents.
- Require native opponent entries unless explicitly testing legacy fallback.
