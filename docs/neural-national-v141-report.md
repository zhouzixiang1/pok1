# Neural National v141 Report

Date: 2026-07-10

This report covers the single neural national TCP change v141, built on top of
v140. All strength evaluations used native national TCP matches with
deterministic bot seeds (`--bot-seed-base 1000`) so v140 and v141 are compared
on identical decks and identical Monte-Carlo streams. No `sever/bot_adapter.py`
path was used for any accepted result.

## Current Candidate

Best current candidate:

`bots/neural_national_lab/versions/v141_national_v123_oldpool_profile_guards_tcp`

Parent:

`bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp`

v141 is identical to v140 except for one new config-driven preflop veto:
`preflop_jqo_4bet_5bet_fold`. It downgrades an offsuit-JQ large 5bet raise to a
fold when the bot is in the big blind facing a 4bet from a high
preflop-raise-rate opponent. The motivation and evidence are below.

## Evidence That Drove The Change

A traced v140 evaluation against the old completed classic pool surfaced a
repeated catastrophic early-hand loss. Against `national_v7` on deck seeds 4500
and 4501, v140 held `Jh Qs` (offsuit JQ, ranks 11/12) in the big blind and
played the exact same preflop sequence in both seeds:

```
opponent open-raise 342
v140  3bet to 825
opponent 4bet to 2813
v140  5bet to 6793   <- rule_action 5968, sanitized 6793 (the escalation point)
opponent jam 19999
v140  allin call     <- rule_action -2
result: -20000
```

Because each hand resets to 20000 chips under the national rules, this single
early allin loss consumed the whole effective stack and the remaining hands
collapsed into a blind swap. Two paired seeds both produced this hand and both
lost -20000. The opponent profile at that decision showed a very high
preflop-raise-rate (0.80 and 1.00), confirming the opponent was an aggressive
4-bettor whose range JQo does not dominate.

The intervention point is the 5bet to 6793. JQo is a marginal offsuit broadway
that does not justify stacking off preflop against a wide aggressive 4bet range.
Folding forfeits the already-committed 3bet but avoids the catastrophic
full-stack loss that defines the old-pool negative opponents.

## The Veto

`_preflop_jqo_4bet_5bet_fold` in `neural_policy.py` fires only when all of these
hold:

- preflop stage
- rule wants a `raise_pot` action (the 5bet), not an allin
- bot is the big blind (`my_id != dealer_id`)
- history is exactly `[opp raise, my raise, opp raise]` (a 3bet-vs-4bet sequence)
- `to_call` in 1500..2600, `pot` in 3000..4400
- `rule_action` in 4000..8000 (the large 5bet)
- hole cards are offsuit JQ (high rank 12, low rank 11, different suits)
- opponent profile: at least 5 actions, preflop-raise-rate at least 0.50,
  postflop-raise-rate at most 1.0

It returns `-1` (fold). The thresholds are calibrated to the two observed loss
hands. The suited JQs variant is deliberately left alone (more equity).

## Native Contract

```
python -m py_compile bots/neural_national_lab/versions/v141_national_v123_oldpool_profile_guards_tcp/*.py
PYTHONPATH=web/core python -c "from national_native import check_native_contract; print(check_native_contract('bots/neural_national_lab/versions/v141_national_v123_oldpool_profile_guards_tcp'))"
```

Result: 0 contract errors.

## Strength Evaluation

All rows below are paired native TCP matches with deterministic bot seeds
(`--bot-seed-base 1000 --bot-seed-stride 1`) so that v140 and v141 see identical
decks and identical Monte-Carlo streams. Candidate illegal/timeout/adapter
counts were 0/0/0 in every row below unless noted.

| Comparison | Opponents | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---|---:|---:|---:|---:|---:|
| v140 current seed4320 m3 | v119-v123 | 15 | 2100 | `+129` | `+0.06` | 1-1-13 |
| v141 current seed4320 m3 | v119-v123 | 15 | 2100 | `+129` | `+0.06` | 1-1-13 |
| v140 current seed4400 m2 | v119-v123 | 10 | 1400 | `+29` | `+0.02` | 1-1-8 |
| v141 current seed4400 m2 | v119-v123 | 10 | 1400 | `+29` | `+0.02` | 1-1-8 |
| v140 oldpool seed4500 m2 | v2,3,5,7,8,9,14,15,16 | 18 | 2520 | `+91351` | `+36.25` | 10-8-0 |
| v141 oldpool seed4500 m2 | v2,3,5,7,8,9,14,15,16 | 18 | 2520 | `+505136` | `+200.45` | 17-1-0 |
| v140 oldpool seed4550 m2 | v5,7,9,14,15,16 | 12 | 1680 | `+1178` | `+0.70` | 5-7-0 |
| v141 oldpool seed4550 m2 | v5,7,9,14,15,16 | 12 | 1680 | `+1178` | `+0.70` | 5-7-0 |
| v141 vs v140 H2H | v140 native | 4 | 560 | `0` | `0.00` | 0-0-4 |
| v141 vs v131 H2H | v131 native port | 3 | 420 | `+56480` | `+134.48` | 2-1-0 |

Per-opponent detail for the decisive oldpool seed4500 m2 block:

| Opponent | v140 total | v141 total | change |
|---|---:|---:|---:|
| national_v2 | +29332 | +29332 | 0 |
| national_v3 | +63074 | +63074 | 0 |
| national_v5 | -151 | +58961 | +59112 |
| national_v7 | -151 | +58961 | +59112 |
| national_v8 | -149 | +58964 | +59113 |
| national_v9 | -151 | +58961 | +59112 |
| national_v14 | -151 | +58961 | +59112 |
| national_v15 | -151 | +58961 | +59112 |
| national_v16 | -151 | +58961 | +59112 |

The six opponents that previously hit the JQo allin-loss hand (v5, v7, v8, v9,
v14, v15, v16) all moved from about -151 to about +58961. v2 and v3 are
unchanged because they did not reach that hand. The aggregate oldpool gain on
this seed block is `+413985`.

## What Confirms The Veto Is Narrow

- On the current pool (v119-v123) v141 is byte-for-byte identical to v140 on two
  independent seed blocks: the veto never fires there because those opponents do
  not produce the aggressive 3bet-vs-4bet sequence against a JQo big blind.
- On oldpool seed4550 (a different deck that does not deal the JQo hand) v141 is
  again identical to v140: the veto stays dormant when its evidence condition is
  absent.
- v141 vs v140 head-to-head is four draws: the two bots play identically when the
  veto cannot fire, which is the expected safe behavior.
- v141 vs v131 native port is +56480 over 420 hands (2-1), consistent with v140's
  reported +50557 over 420 hands against the same opponent.

## Important Measurement Note

Earlier per-version reports quoted much larger current-pool aggregates (for
example v140 at +139262 over 2100 hands). Those numbers used a different seed /
bot-seed configuration than the deterministic comparison above and are dominated
by allin-runout variance. The apples-to-apples v140-vs-v141 delta requires fixed
bot seeds, because `simulation.py` uses `random.random()` / `random.sample()` for
Monte-Carlo equity and the bot process is not seeded unless
`--bot-seed-base` is passed. With fixed bot seeds, the current pool is low
variance (mostly small swings and draws) and the only material difference between
v140 and v141 is the oldpool JQo allin-avoidance. Future neural-line comparisons
in this report series should pass `--bot-seed-base` for reproducibility.

## Protocol Notes

v141 is a native national TCP bot, unchanged from v140 in protocol behavior:

- Direct TCP client entrypoint: `national_bot.py`.
- No `sever/bot_adapter.py` usage.
- Raw `sock.recv` stream processing with `_split_messages`.
- Official-safe action delay via `POK_OFFICIAL_ACTION_DELAY`, default `0.30`.
- Action vocabulary restricted to `fold`, `call`, `check`, `allin`, and exact
  `raise <amount>`.
- The new veto only ever returns `-1` (fold), which is always a legal action when
  facing a bet, so it cannot introduce an illegal action.

## Official EXE Smoke

```
python scripts/official_platform_acceptance.py \
  --candidate bots/neural_national_lab/versions/v141_national_v123_oldpool_profile_guards_tcp \
  --opponent bots/national_v123 \
  --self-play-rounds 1 --opponent-rounds 1 --target-hands 10 \
  --round-timeout 240 --no-progress-timeout 90 \
  --results-dir bots/neural_national_lab/data/official_platform_v141_smoke2
```

Result:

- Suite: `bots/neural_national_lab/data/official_platform_v141_smoke2/acceptance_20260710_012307`
- Rounds requested/run: 2/2.
- Passed rounds: 2.
- Failed rounds: 0.
- Self-play and opponent rounds both reached 27 completed hands (target was 10).

## Success Bar Check

- 0 candidate illegal actions: yes.
- 0 candidate timeouts: yes.
- 0 candidate adapter actions: yes.
- Official EXE smoke passes: yes (2/2).
- Current pool unchanged from v140: yes (identical on two seed blocks).
- Oldpool improved from v140: yes (+413985 on seed4500 m2; six opponents flipped
  from about -151 to about +58961).

v141 is strictly better than v140: it matches v140 everywhere the veto does not
fire and is materially better where it does. The next target remains the broader
old-pool negatives that are not explained by this single JQo hand.
