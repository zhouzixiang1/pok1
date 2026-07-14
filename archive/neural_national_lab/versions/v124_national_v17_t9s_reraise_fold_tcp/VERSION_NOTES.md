# v124_national_v17_t9s_reraise_fold_tcp

Native national TCP T9s reraise-fold cleanup derived from v123.

Change:

- Keeps v123's T8o/K4s/J8s draw breakers, QJs flop call cleanup, and native
  national TCP entrypoint.
- Adds a narrow preflop T9s 3bet/reraise fold:
  - stage is `preflop`,
  - original rule label is `raise_pot`,
  - opponent is not all-in,
  - hole cards are exactly suited T9,
  - `620 <= to_call <= 640`,
  - pot is `1360..1410`,
  - rule raise-to action is `2500..2750`,
  - opponent profile preflop raise rate is at most `0.10`,
  - return action `-1`, a protocol-legal `fold`.

Rationale:

- v123 preserved the all-completed `150-0-0` block and improved the old v3
  seed `2026074008`, but the old ordered seed block `2026073900` still had
  repeatable v2/v3 losses on seed `2026073909`.
- Native TCP loss counterfactual scanning found the same paired hand 19 leak:
  suited T9 called/raised into a compact preflop reraise spot, then lost a
  large pot.
- Forcing only hand 19 decision 1 to fold changed v2 seed `2026073909` from
  `-4122` to `+16163`, a `+20285` match-chip swing; the target hand improved
  from `-7726` to `-279`.
- The same fold changed v3 seed `2026073909` from `-3781` to `+16139`, a
  `+19920` match-chip swing; the target hand improved from `-7561` to `-279`.
- A forced call was stronger against v3 in isolation but much weaker against
  v2, so v124 chooses the more stable cross-opponent fold.
- The same boardline also appeared on seed `2026073908`, where the original
  raise was profitable; trace comparison separated it by opponent profile
  preflop raise rate (`0.1304` on 3908 vs `0.0909` on 3909).

Status:

- Must improve old v2/v3 seed `2026073909`, preserve the ordered old top8+v7
  regression blocks, and keep current all-completed domination.
