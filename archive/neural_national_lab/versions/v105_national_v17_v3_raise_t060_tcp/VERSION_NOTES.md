# v105_national_v17_v3_raise_t060_tcp

Native national TCP neural overlay derived from v104.

Change:

- Keeps v104's narrow proposal surface: only preflop `raise_pot` proposals over
  rule `call` or `fold`.
- Raises both proposal threshold/margin values from `0.40` to `0.60`.
- Keeps `allow_fold=false`, no neural all-in, and the same h64 action-value
  head `native_context_action_v102_v3smallneg_plus_current_h64_seed3521.json`.
- Uses the same native national TCP entry point; no adapter is used.

Rationale:

- v104 improved the focused v3/v8/v9/v14 block versus v102, but the wider
  top8+v7 seed block showed pollution on the seven non-v2/v3 opponents.
- Gate scan at `0.60` still retained positive v3 `raise_pot` samples while
  selecting fewer rows overall, so this version tests whether a stricter value
  margin can keep the v3 correction without broad damage.

Status:

- Focused seed block `2026073600`, v3/v8/v9/v14, 12 paired matches /
  1680 hands: `+20304` chips absolute versus v102's `-27357`, a paired
  improvement of `+47661`.
- The focused v3 slice improved from v102's `-21507` to `+18570`.
- Wider current top8 plus v7 seed block `2026073700`, 27 paired matches /
  3780 hands: `-200929` chips absolute versus v102's `-186443`, a paired
  regression of `-14486`.
- Compliance was clean on both reports: 0 illegal actions, 0 timeouts,
  0 adapter actions.
- Verdict: best focused variant in this batch, but not a general upgrade over
  v102. More counterfactual data is needed before further threshold tuning.
