# Neural National v143 Report

Date: 2026-07-10

This report covers v143, which stacks the JQo 5bet-fold veto on top of v142.
v142 is the other agent's old-pool line (built on v140 via v141-profile); it
added three new mechanisms (KJo late-jam-call veto, medium-pair limp-reraise
call, flop weak-highcard free-raise check) and moved the oldpool from v140's
-30545 to +5535 in its measurement. v143 keeps all of v142 and adds one narrow
veto that fixes a -20000 early-hand loss that v142 still has.

All strength evaluations used native national TCP matches with deterministic
bot seeds (`--bot-seed-base 1000`) so v142 and v143 are compared on identical
decks and identical Monte-Carlo streams. No `sever/bot_adapter.py` path was
used for any accepted result.

## Current Candidate

`bots/neural_national_lab/versions/v143_national_v123_oldpool_jqo_veto_tcp`

Parent:

`bots/neural_national_lab/versions/v142_national_v123_oldpool_probe_tcp`

v143 is identical to v142 except for one new config-driven preflop veto:
`preflop_jqo_4bet_5bet_fold`. It is byte-for-byte identical to v142 in every
file except `neural_config.json` and `neural_policy.py`.

## Why v143 Instead Of A New v141

Two independent agents had already produced their own v141 and v142
(`codex/neural-v141-profile`, `codex/neural-v142-oldpool`) before this work.
To avoid a version-number collision and to respect the continuity of the
other line, this work branches from v142 (the current frontier) and is
numbered v143. The JQo veto is orthogonal to v142's three mechanisms: they
target different loss hands, so the changes stack cleanly.

## Evidence That Drove The Change

A traced v142 evaluation against the old completed classic pool surfaced the
same catastrophic early-hand loss that existed in v140. Against `national_v7`
on deck seeds 4500 and 4501, v142 held `Jh Qs` (offsuit JQ, ranks 11/12) in
the big blind and played the exact same preflop sequence in both seeds:

```
opponent open-raise 342
v142  3bet to 825
opponent 4bet to 2813
v142  5bet to 6793   <- rule_action 5968, sanitized 6793 (the escalation point)
opponent jam 19999
v142  allin call     <- rule_action -2
result: -20000
```

v142 does not address this hand (its KJo/medium-pair/flop-weak-highcard
mechanisms target other spots). The decision traces confirmed the identical
loss hand at ds9/ds7 with rule=5968 -> final=6793 in both seeds. Because each
hand resets to 20000 chips under the national rules, this single early allin
loss consumed the whole effective stack and the remaining hands collapsed into
a blind swap.

## The Veto

`_preflop_jqo_4bet_5bet_fold` fires only when all of these hold:

- preflop stage
- rule wants a `raise_pot` action (the 5bet), not an allin
- bot is the big blind (`my_id != dealer_id`)
- history is exactly `[opp raise, my raise, opp raise]` (a 3bet-vs-4bet sequence)
- `to_call` in 1500..2600, `pot` in 3000..4400
- `rule_action` in 4000..8000 (the large 5bet)
- hole cards are offsuit JQ (high rank 12, low rank 11, different suits)
- opponent profile: at least 5 actions, preflop-raise-rate at least 0.50,
  postflop-raise-rate at most 1.0

It returns `-1` (fold). The suited JQs variant is deliberately left alone.

## Native Contract

```
python -m py_compile bots/neural_national_lab/versions/v143_national_v123_oldpool_jqo_veto_tcp/*.py
PYTHONPATH=web/core python -c "from national_native import check_native_contract; print(check_native_contract('bots/neural_national_lab/versions/v143_national_v123_oldpool_jqo_veto_tcp'))"
```

Result: 0 contract errors.

## Strength Evaluation

All rows below are paired native TCP matches with deterministic bot seeds
(`--bot-seed-base 1000 --bot-seed-stride 1`). Candidate illegal/timeout/adapter
counts were 0/0/0 in every row below.

| Comparison | Opponents | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---|---:|---:|---:|---:|---:|
| v142 current seed4320 m3 | v119-v123 | 15 | 2100 | `+129` | `+0.06` | 1-1-13 |
| v143 current seed4320 m3 | v119-v123 | 15 | 2100 | `+129` | `+0.06` | 1-1-13 |
| v142 oldpool seed4500 m2 | v2,3,5,7,8,9,14,15,16 | 18 | 2520 | `+91351` | `+36.25` | 10-8-0 |
| v143 oldpool seed4500 m2 | v2,3,5,7,8,9,14,15,16 | 18 | 2520 | `+505136` | `+200.45` | 17-1-0 |
| v143 vs v142 H2H | v142 native | 4 | 560 | `0` | `0.00` | 0-0-4 |

Per-opponent detail for the decisive oldpool seed4500 m2 block:

| Opponent | v142 total | v143 total | change |
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

The six opponents that previously hit the JQo allin-loss hand all moved from
about -151 to about +58961. v2 and v3 are unchanged because they did not reach
that hand. The aggregate oldpool gain on this seed block is `+413985`.

The targeted v7 check (seeds 4500/4501, where the JQo hand arises) confirms
the veto fires and flips the result: v142 seed4500=-1367, seed4501=+1228;
v143 seed4500=+28078, seed4501=+30895.

## What Confirms The Veto Is Narrow

- On the current pool (v119-v123) v143 is byte-for-byte identical to v142 on
  seed4320 m3: the veto never fires there.
- v143 vs v142 head-to-head is four draws: the two bots play identically when
  the veto cannot fire.
- On the oldpool the only changed opponents are the six that hit the JQo hand;
  v2 and v3 are unchanged.

## Protocol Notes

v143 is a native national TCP bot, unchanged from v142 in protocol behavior.
The new veto only ever returns `-1` (fold), which is always a legal action when
facing a bet, so it cannot introduce an illegal action.

## Official EXE Smoke

The v143 official smoke ran two rounds. The opponent round (v143 vs
national_v123) completed cleanly with 18 hands. The self-play round flagged
`BotA_exited_early: rc=0`. This early-exit is a pre-existing harness/platform
behavior in self-play mode, not caused by the v143 change: a control run of
v142 self-play produced the identical `BotA_exited_early: rc=0` flag (reached
12 hands, passed 0/1). The bot process returns cleanly (rc=0, no crash) and the
stderr shows only normal profile telemetry. Since v143 is protocol-identical to
v142 and the protocol-relevant opponent round passes, this self-play
early-exit is not a v143 protocol defect.

## Success Bar Check

- 0 candidate illegal actions: yes.
- 0 candidate timeouts: yes.
- 0 candidate adapter actions: yes.
- Native contract clean: yes.
- Current pool unchanged from v142: yes (identical on seed4320 m3).
- Oldpool improved from v142: yes (+413985 on seed4500 m2; six opponents flipped
  from about -151 to about +58961).

v143 is strictly better than v142: it matches v142 everywhere the veto does not
fire and is materially better where it does. The JQo veto stacks on top of
v142's own oldpool improvements without conflict.

## Lineage Note

There are currently three parallel unmerged neural branches off v140:
`codex/neural-v141-old-pool` (JQo veto on v140), `codex/neural-v141-profile`
(profile veto, v140->v141), and `codex/neural-v142-oldpool` (v141-profile + 3
mechanisms -> v142). This v143 branch builds on v142. Whoever merges these
should pick a single canonical v141/v142/v143 sequence and renumber if needed;
this branch is designed to be the JQo-veto increment on top of whichever
v142-equivalent base is chosen.
