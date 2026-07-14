# v104_national_v17_v3_raise_guard_tcp

Native national TCP neural overlay derived from v103.

Change:

- Keeps the v103 h64 multi-action value head:
  `native_context_action_v102_v3smallneg_plus_current_h64_seed3521.json`.
- Removes the v103 defensive `fold/call` proposals against rule raises.
- Keeps only high-threshold preflop `raise_pot` proposals over `call` and
  `fold`, both at threshold/margin `0.40`.
- Restores `allow_fold=false` so the base neural classifier cannot introduce
  fold overrides outside the proposal path.
- Uses the same native national TCP entry point as v102/v103; no adapter is
  used.

Rationale:

- v103 improved the focused v3/v8/v9/v14 seed block overall versus v102, but
  it caused a large v3 regression.
- Training-row inspection showed v3's positive high-confidence signal is mostly
  in `raise_pot` proposals over `call/fold`, while defensive `fold/call`
  proposals are sparse or neutral for v3.
- v104 isolates the narrower hypothesis that the new model's high-confidence
  pressure raises are useful, while v103's defensive branch is the v3 poison.

Status:

- Focused seed block `2026073600`, v3/v8/v9/v14, 12 paired matches /
  1680 hands: `+17157` chips absolute versus v102's `-27357`, a paired
  improvement of `+44514`.
- The focused v3 slice improved from v102's `-21507` to `+15423`.
- Wider current top8 plus v7 seed block `2026073700`, 27 paired matches /
  3780 hands: `-200623` chips absolute versus v102's `-186443`, a paired
  regression of `-14180`.
- Compliance was clean on both reports: 0 illegal actions, 0 timeouts,
  0 adapter actions.
- Verdict: good focused v3 recovery, but not a general upgrade over v102.
