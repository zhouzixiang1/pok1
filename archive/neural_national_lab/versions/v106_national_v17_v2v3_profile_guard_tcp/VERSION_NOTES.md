# v106_national_v17_v2v3_profile_guard_tcp

Native national TCP neural probe derived from v102.

Change:

- Keeps v102's policy and multi-action value model:
  `native_context_action_current_plus_v2v3_hardneg_h64_seed3421.json`.
- Keeps v102's three preflop proposal branches:
  `raise_pot -> call`, `call -> raise_pot`, and `fold -> raise_pot`.
- Adds a runtime opponent-profile guard for multi-action value proposals:
  require at least 8 observed opponent actions and opponent `raise_rate <= 0.30`.
- Leaves neural fold and all-in disabled.

Trace rationale:

- On native TCP trace seed `2026073700`, v102 changed 26/474 decisions.
- The v2/v3 interventions were small positive samples:
  v2 `+700` across 5 neural-changed decisions, v3 `+700` across 5.
- The v14/v5 interventions were negative:
  v14 `-2984` across 8 neural-changed decisions, v5 `-2984` across 8.
- The negative v14/v5 interventions occurred under high observed aggression
  (`raise_rate` about `0.70..0.82`) or an unreliable one-action profile.
- The positive v2/v3 interventions occurred under low observed aggression
  (`raise_rate` about `0.09..0.13`) after at least 11 observed actions.

Status:

- This is a profile-gated ablation, not a new trained model.
- Expected behavior is to preserve v102's v2/v3 low-aggression fold-to-raise
  probe while suppressing the known high-aggression v14/v5 pollution.
- Must be judged only through native national TCP evaluation; no adapter path is
  used by this version.
