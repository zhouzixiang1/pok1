# v087_national_v17_native_context_early_veto_tcp

Narrowed opponent-conditioned native TCP value probe forked from
`v086_national_v17_native_context_value_fc_tcp`.

Change:

- Keeps the native national TCP entrypoint and the v085 opponent-profile
  request data.
- Uses the same `native_context_value_h16_seed1301.json` tiny MLP as v086.
- Disables fold-to-call proposals for this version.
- Keeps only a very narrow preflop raise-to-fold veto:
  small blind first action, rule action is a raise, to-call is at most 60
  chips, observed opponent actions are 2 to 3, and observed opponent raise
  rate is zero.
- Adds config-driven profile/scope gates in `neural_policy.py` so later
  versions can bound opponent-conditioned value heads without rewriting the
  policy logic.

Interpretation:

- v086 showed a local positive example but failed broad paired evaluation
  because the veto fired too often later in matches and introduced large tail
  losses.
- v087 tests whether the one interpretable early-match positive pattern can be
  isolated without those long-match tail losses.
