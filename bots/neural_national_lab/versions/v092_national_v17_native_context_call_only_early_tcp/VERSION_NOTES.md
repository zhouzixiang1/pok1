# v092_national_v17_native_context_call_only_early_tcp

Early-profile call-only native TCP value probe forked from
`v091_national_v17_native_context_call_only_tcp`.

Change:

- Keeps the same h32 native-context value model and native national TCP
  entrypoint.
- Keeps neural fold disabled and allows only `raise_pot -> call` proposals.
- Restricts value-head intervention to the early profile window:
  `2 <= opponent_actions_total <= 4`.

Interpretation:

- v091 showed that call-only advice had useful early positive samples against
  `national_v10`, `national_v18`, and `national_v3`, but large late-history
  losses against `national_v1` and `national_v2`.
- v092 tests whether those late-history losses can be removed without
  returning to a no-op.
