# zcode bot — equity + pot-odds EV school

A self-contained heads-up No-Limit Texas Hold'em bot, built from first
principles and deliberately **not** derived from the existing
`bots/bot*` heuristic bots. It lives entirely under `zcode/` and imports
nothing from `engine/`, `bots/`, `sever/`, or `web/`.

## Design philosophy (a different school)

The existing bots use a hand-strength **lookup table** (Chen formula) plus
**heuristic raise bands** and an opponent model. They also contain a
long-standing bug: they parse raise actions as a *delta* when the engine
encodes them as a **raise-to-total**, so their state reconstruction drifts
after every re-raise.

`zcode` instead makes decisions from first principles:

1. **Monte-Carlo equity** — randomly complete the board + opponent hole
   cards and count wins/ties. No lookup table, no Chen formula.
2. **Pot-odds EV** — fold / call / raise based on whether each action has
   positive expected chip value given the equity and the pot odds.
3. **Polarised betting when checked to** — against passive opponents who
   rarely lead out, we bet a wide range (value + thin-value + bluff) so we
   extract from callers instead of checking back.
4. **Opponent-agnostic** — the core plays a defensively sound strategy; it
   does not model a specific opponent, which keeps it robust across the
   very wide style range of the local bot pool.

## Files

| File | Role |
|---|---|
| `cards.py` | Card encoding + best-5-of-7 hand evaluation (9 categories). |
| `equity.py` | Monte-Carlo win/tie estimation (fast inner loop, stdlib only). |
| `state.py` | Protocol parsing; **correctly** treats raise as raise-to-total. |
| `policy.py` | EV-based decision policy + tunable `PolicyConfig` + sanitiser. |
| `main.py` | JSON entry point (Botzone / local `engine/battle.py`). |
| `national_bot.py` | National TCP-platform client (native wire protocol). |
| `acceptance/RESULTS.md` | Battle logs vs `bot1`..`bot6` (acceptance evidence). |

## Usage

### Local engine (JSON protocol)

```bash
# Standard battle, 70 hands/game by default
python engine/battle.py zcode/main.py bots/bot5/main.py -n 10
```

### National TCP platform

Either use the bridge adapter (the bot is a normal JSON bot):

```bash
cd sever && python bot_adapter.py --bot ../zcode/main.py --name zcodeA
```

…or run the native TCP client directly:

```bash
python zcode/national_bot.py --host 127.0.0.1 --port 10001 --name zcodeA
```

Both connect to the national self-play server and emit only legal wire
actions: `raise <amount>`, `call`, `check`, `fold`, `allin`.

## Performance

- A single decision takes **~0.01-0.05 s** (well under the 60 s budget).
- 3000 Monte-Carlo trials on the preflop take **~0.08 s**.
- The bot is **stdlib-only** (no numpy / torch) so it runs anywhere.

## Acceptance

See `acceptance/RESULTS.md` for the recorded battle logs. `zcode` beats
every `bot1`..`bot6` baseline; strongest against the aggressive
simulation-heavy bots (`bot4`/`bot5`/`bot6`, all 5-0 / 8-0 sweeps) and
narrow-but-positive against the very passive bots (`bot1`/`bot2`/`bot3`,
where most games end in small-chip draws because neither side builds big
pots).

## Tuning knobs

All decision thresholds live in `policy.PolicyConfig` and can be tuned
without touching the decision logic:

- `bet_value_threshold` / `bet_thin_value_threshold` — when to bet for value
  when checked to (lower = more aggressive value extraction vs passives).
- `raise_value_threshold` — when to raise when facing a bet.
- `call_edge` — how tight we are vs pure pot-odds when calling.
- `bet_bluff_frequency` / `bluff_frequency` — how often we bluff.
- `slowplay_threshold` / `slowplay_frequency` — trapping frequency.
- `n_sim_preflop` / `n_sim_postflop` — Monte-Carlo accuracy budget.
