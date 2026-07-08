# Official Windows Platform Harness

The official national compliance oracle is the Windows EXE:

```text
sever/国赛平台/德州扑克对弈平台限时一分钟2021版/德州扑克对弈平台限时一分钟2021版.exe
```

It is run on Linux through Wine and Xvfb. This harness starts the EXE, configures
the platform UI, launches two independent native TCP bot processes, captures
screenshots, and writes bot/platform logs plus JSON receipts.

## Compliance Authority

The EXE is the authority for national-native protocol legality. It is not the
long-running evolution tracker and must not replace local native TCP ratings,
precommit regression, or multi-generation observation. It differs from the fast
local simulator in one important way: it uses raw short TCP messages, not a
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

## Manual Compliance Command

Long-running evolution does not use this EXE for strength tracking. Ratings,
precommit regression, and multi-generation observation use the local native TCP
harness. The EXE is reserved for protocol legality evidence: no illegal wire
actions, no timeouts, no process crashes, and a complete 70-hand run when the
gate asks for one.

The default automated precommit compliance suite is one complete self-play round
and one complete candidate-vs-reference round, 70 hands per round:

```bash
python scripts/official_platform_acceptance.py \
  --candidate bots/national_v<N> \
  --opponent /home/zzx/project/pok/bots/national_v70 \
  --self-play-rounds 1 \
  --opponent-rounds 1 \
  --target-hands 70
```

The queued helper uses the same shape:

```bash
python scripts/official_certify.py compliance bots/national_v<N> \
  --opponent /home/zzx/project/pok/bots/national_v70
```

## Manual Heavy Compliance Recheck

For a one-off heavy compliance recheck, explicitly run 5 complete self-play
rounds and 3 complete candidate-vs-opponent rounds, 70 hands per round. This is
not the normal evolution tracking loop:

```bash
python scripts/official_platform_acceptance.py \
  --candidate bots/national_v<N> \
  --opponent /home/zzx/project/pok/bots/national_v70 \
  --self-play-rounds 5 \
  --opponent-rounds 3 \
  --target-hands 70
```

When `--candidate` or `--opponent` is a directory, it must contain
`national_bot.py`. To run a standalone reference sample, pass that script path
explicitly.

For a quick environment smoke:

```bash
python scripts/official_platform_acceptance.py --check-env
```

For a quick bot/platform smoke:

```bash
python scripts/official_platform_acceptance.py \
  --candidate /home/zzx/project/pok/bots/national_v70 \
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
- moved official THP records under each full round's `thp/` directory.

The default output root is:

```text
web/core/results/official_platform/
```

This path is runtime evidence and should remain gitignored.

## Gate Integration

The harness is available from `web/core/official_platform_harness.py`. Normal
unit tests and local native TCP checks stay fast. `pokctl.sh` enables the
official runtime compliance defaults for long-running evolution:

```bash
export POK_OFFICIAL_REQUIRED=1
export POK_OFFICIAL_OPPONENT=/home/zzx/project/pok/bots/national_v70
export POK_OFFICIAL_PRECOMMIT_SELF_ROUNDS=1
export POK_OFFICIAL_PRECOMMIT_OPPONENT_ROUNDS=1
export POK_OFFICIAL_PRECOMMIT_TARGET_HANDS=10
export POK_OFFICIAL_SELF_PLAY_ROUNDS=1
export POK_OFFICIAL_OPPONENT_ROUNDS=1
export POK_OFFICIAL_TARGET_HANDS=70
```

With `POK_OFFICIAL_REQUIRED=1`, quality gates enqueue or read the short
official smoke by default; the rating daemon processes that queue in the
background so official EXE ambiguity does not block local native TCP gates.
Precommit also queues or reads a short 1+1 official compliance suite. These
checks exist only to detect explicit protocol/illegal-action evidence from the
official EXE. Strength measurement, regression comparison, and generation
tracking stay on the local native TCP harness. The full 5+3, 70-hand suite is
opt-in and should not be used as the normal generation tracker. More granular
switches are available:

```bash
export POK_OFFICIAL_SMOKE_GATE=queue  # default; use "run" only for manual blocking checks
export POK_OFFICIAL_PRECOMMIT_GATE=1
export POK_OFFICIAL_ACCEPTANCE_GATE=0  # keep official EXE out of strength tracking
```

When enabled, precommit records an `official_platform_compliance` scorecard
entry with `blocking=false`. It is evidence for protocol legality, not the
strength or long-run tracking mechanism. The local native TCP precommit remains
the hard regression gate, while official failures are kept in certification
status so they can block future parent selection when they indicate real
protocol violations.

Official certification status is intentionally three-valued for automation:
`official-*-pass` means the requested official suite completed, `official-failed`
means explicit protocol/illegal-action evidence was found, and
`official-inconclusive` means the official EXE or harness did not provide a
complete answer, such as no-progress timeout, Wine/window trouble, missing THP
export, or an occupied port. Inconclusive official evidence is logged and shown,
but it is not a bot violation and must not replace local native TCP evaluation.
The daemon queue worker can be disabled with `POK_OFFICIAL_QUEUE_WORKER=0` or
throttled with `POK_OFFICIAL_QUEUE_INTERVAL_SEC` / `POK_OFFICIAL_QUEUE_LIMIT`.

The harness uses a process-wide file lock at `/tmp/pok_official_platform.lock`
by default. This keeps official EXE suites serial because the 2021 platform uses
a fixed UI workflow and TCP port. The async web pipeline still calls the harness
through a worker thread, so FastAPI stays responsive while the long suite runs.
Override the lock only for isolated manual experiments:

```bash
export POK_OFFICIAL_LOCK_PATH=/tmp/pok_official_platform_custom.lock
export POK_OFFICIAL_LOCK_TIMEOUT_SEC=900
```

## Current Completion Heuristic

The official EXE has been observed to start all 70 hands while sometimes leaving
only 69 visible `earnChips` dispatches in bot logs after the final hand. The
harness therefore treats a 70-hand round as complete when both bots have seen at
least 70 `preflop` messages and at least 69 settlements, with no critical log
issues and no no-progress timeout. Full 70-hand rounds must also produce an
official THP file with at least 70 `STATE` records; short smoke runs do not
require THP output.

Every bot `SEND ... msg='...'` line is checked against the exact official wire
format: `call`, `check`, `fold`, `allin`, or `raise <positive integer>` with
exactly one space. `bet`, extra spaces, leading/trailing whitespace, and unknown
actions fail the round even if the platform would otherwise silently treat the
move as a fold.
