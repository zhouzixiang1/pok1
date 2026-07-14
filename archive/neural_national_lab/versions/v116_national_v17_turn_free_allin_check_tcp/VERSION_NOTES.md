# v116_national_v17_turn_free_allin_check_tcp

Native national TCP turn free-all-in value-check experiment derived from v115.

Change:

- Keeps v115's flop free-action value-check gate.
- Adds a narrow turn all-in veto:
  - stage is `turn`,
  - original rule label is `allin`,
  - `to_call == 0`, so action `0` is a protocol-legal `check`,
  - pot is at least `8000`,
  - value head score for `call`/check is at least `0.8`,
  - value head score for `allin` is at most `-0.3`,
  - check is at least `1.0` above all-in.
- The veto returns action `0` and remains native national TCP.

Rationale:

- v115's remaining v2 seed block `2026074000` still had a single `-20000`
  turn free all-in on seed `2026074001`, hand 35.
- At that decision, the pot was `9344`, `to_call` was `0`, the value head
  scored check at `0.830` and all-in at `-0.387`.
- A force probe changing only that decision to check improved the paired match
  from `-17937` to `-2609`, a `+15328` chip gain.
- Scanning the full v115-v2 trace found no other free all-in decision and no
  other trigger under these thresholds.

Status:

- Must beat v115 on v2 seed block `2026074000`, then pass current-top8+v7 on
  seed blocks `2026074000` and `2026073900`.
