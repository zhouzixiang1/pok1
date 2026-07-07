# Official Windows Platform Harness

The official national acceptance oracle is the Windows EXE:

```text
sever/国赛平台/德州扑克对弈平台限时一分钟2021版/德州扑克对弈平台限时一分钟2021版.exe
```

It is run on Linux through Wine and Xvfb. This harness starts the EXE, configures
the platform UI, launches two independent native TCP bot processes, captures
screenshots, and writes bot/platform logs plus JSON receipts.

## Protocol Authority

The EXE is the authority for national-native bot acceptance. It differs from the
fast local simulator in one important way: it uses raw short TCP messages, not a
newline-delimited text protocol. Native bots must:

- wait for `name`, then send the raw team/player name without relying on `\n`;
- send raw actions `raise <amount>`, `fold`, `call`, `check`, `allin`;
- never send `bet`;
- parse inbound sticky packets such as `earnChips -100preflop|...` and
  `raise 200call`;
- keep `raise <amount>` as raise-to-total for the current street.

## Prerequisites

The host needs:

```bash
wine
Xvfb
xdotool
```

`import` from ImageMagick is optional but recommended for screenshots. The
known-good Wine prefix is:

```text
/home/zzx/.cache/pok_wine_national_platform
```

That prefix should contain the fake Chinese font mapping installed by
`winetricks -q fakechinese`; otherwise screenshots may show broken Chinese UI
text.

## Manual Acceptance Command

The formal full acceptance suite is 5 complete self-play rounds and 3 complete
candidate-vs-opponent rounds, 70 hands per round:

```bash
python scripts/official_platform_acceptance.py \
  --candidate bots/national_v<N> \
  --opponent /home/zzx/project/pok/ref/national_v70 \
  --self-play-rounds 5 \
  --opponent-rounds 3 \
  --target-hands 70
```

When `--candidate` or `--opponent` is a directory, it must contain
`national_bot.py`. To run a standalone reference sample, pass that script path
explicitly.

For a quick environment smoke:

```bash
python scripts/official_platform_acceptance.py \
  --candidate /home/zzx/project/pok/ref/national_v70 \
  --self-play-rounds 1 \
  --opponent-rounds 0 \
  --target-hands 5
```

Each run writes:

- `summary.json` for the whole suite;
- `receipt.json` for each round;
- `botA.log`, `botB.log`, stdout/stderr captures;
- `platform.wine.log`;
- screenshots under each round's `screenshots/` directory when ImageMagick is
  available.

The default output root is:

```text
web/core/results/official_platform/
```

This path is runtime evidence and should remain gitignored.

## Gate Integration

The harness is available from `web/core/official_platform_harness.py`. It is off
by default so normal unit tests and local simulator checks stay fast. Enable it
for the evolution process with:

```bash
export POK_OFFICIAL_REQUIRED=1
export POK_OFFICIAL_OPPONENT=/home/zzx/project/pok/ref/national_v70
export POK_OFFICIAL_SELF_PLAY_ROUNDS=5
export POK_OFFICIAL_OPPONENT_ROUNDS=3
export POK_OFFICIAL_TARGET_HANDS=70
```

More granular switches are available:

```bash
export POK_OFFICIAL_SMOKE_GATE=1
export POK_OFFICIAL_ACCEPTANCE_GATE=1
export POK_OFFICIAL_PRECOMMIT_GATE=1
```

When enabled, precommit appends an `official_platform_acceptance` blocker on
failure, so the bot cannot be committed.

## Current Completion Heuristic

The official EXE has been observed to start all 70 hands while sometimes leaving
only 69 visible `earnChips` dispatches in bot logs after the final hand. The
harness therefore treats a 70-hand round as complete when both bots have seen at
least 70 `preflop` messages and at least 69 settlements, with no critical log
issues and no no-progress timeout.
