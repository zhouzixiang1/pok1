# v118_national_v17_low_trips_flop_call_tcp

Native national TCP low-trips flop pot-raise downshift derived from v117.

Change:

- Keeps v117's weak-wheel-ace preflop large-reraise `call` gate.
- Adds a narrow flop paid-action downshift:
  - stage is `flop`,
  - original rule label is `raise_pot`,
  - facing a non-all-in medium bet with `1100 <= to_call <= 1500`,
  - pot is `2400..2900`,
  - rule raise-to action is `4500..5600`,
  - board is paired low rank `2..5` plus an ace,
  - our hand makes trips with exactly one matching low card,
  - kicker is at most jack and the two hole cards are suited,
  - return action `0`, which is a protocol-legal `call`.

Rationale:

- Fresh native TCP evaluation against `.evolution_pok/bots/national_v2` on
  seeds `2026074100..2026074105` exposed repeated A-2-2 flop over-raises with
  suited J2. v117 raised pot into a strong response and then folded, losing
  around `-5.5k` to `-6.0k` in the target hand.
- Force probes changing that flop decision to `call` improved the six paired
  matches by `+82,801` chips in total. The action is deliberately narrower than
  the force probe because A3o earlier-street probes and J2 preflop fold probes
  were negative or unstable.

Status:

- Must beat v117 on the latest top-rule seed block `2026074100`, then pass
  regression on the older v116/v117 seed blocks `2026074000` and `2026073900`.
