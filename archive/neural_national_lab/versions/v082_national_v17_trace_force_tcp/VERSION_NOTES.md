# v082_national_v17_trace_force_tcp

Native national TCP instrumentation forked from
`v079_national_v17_neural_off_control_tcp`.

Purpose:

- Keep the national v17 rulebase behavior unchanged by default.
- Preserve a direct native `national_bot.py` TCP entrypoint; no national adapter
  or Botzone JSON bridge is used.
- Add opt-in decision tracing for native TCP action-value data collection.
- Add opt-in single-decision force hooks for same-seed counterfactual probes.

Environment flags:

- `POK_TRACE_DECISIONS=1` writes `POK_TRACE_DECISION {...}` JSON lines to
  stderr. Each row contains the decision request, reconstructed state, rule
  action, advised action, sanitized action, final action, hand number, and
  hand-local decision index.
- `POK_FORCE_HAND=<1-based hand>` limits forcing to one hand.
- `POK_FORCE_DECISION=<0-based decision index within hand>` limits forcing to
  one decision in that hand.
- `POK_FORCE_ACTION=<final action>` overrides the sanitized final action. Use
  this only inside controlled probes; an invalid forced action is intentionally
  allowed to reach the protocol validator so the probe can detect it.

Interpretation:

- v082 is a data-contract and probe version, not a strength candidate.
- It is the first native TCP route for collecting decision-local
  counterfactual labels directly against strong national rule bots.
