# v085_national_v17_profile_trace_tcp

Native national TCP profile-trace forked from
`v082_national_v17_trace_force_tcp`.

Purpose:

- Preserve v17/v082 rule behavior by default.
- Keep direct national TCP support in `national_bot.py`; no adapter is used.
- Add a protocol-native `opponent_profile` field to each traced request. The
  profile is built only from actions observed during the current TCP match:
  fold/call/check/raise/all-in rates, preflop/postflop action counts, and
  preflop/postflop raise rates.
- Support `POK_TRACE_DECISIONS` and `POK_FORCE_*` exactly like v082, so
  `tools/native_tcp_counterfactual_probe.py` can collect
  `native_context_features = state_features + opponent_profile_features`.

Interpretation:

- v085 is a data-contract version for opponent/style-conditioned native TCP
  action-value learning.
- It is not a strength candidate until a later version loads a profile-aware
  value head and passes paired native TCP evaluation.
