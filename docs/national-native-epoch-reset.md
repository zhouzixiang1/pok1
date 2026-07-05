# National Native Epoch Reset

Date: 2026-07-05

The active evolution epoch is now `national_native_v1`. This is a clean break
from the Botzone/JSON evaluation lineage.

## Active Namespace

- Active bot directories: `bots/national_v<N>/`
- Active completion tags: `national-bot-v<N>`
- Formal entrypoint: `national_bot.py`
- Formal protocol: national TCP, native client
- Legacy adapter: allowed only for explicit regression checks, never as a pass
  condition for new evolved bots

Existing `bot-v*` tags and `claude_v*` directories are legacy history. They must
not determine current version numbers, active pool membership, ratings, H2H,
experience injection, or precommit pass/fail.

## Archived State

Legacy runtime data and old top-level bot directories were moved into local
gitignored payloads under:

```text
archive/evolution_epochs/national_native_v1_<timestamp>/
```

Those payload directories contain a `manifest.json` with exact source-to-archive
paths. They are intentionally not tracked because they can contain multi-GB
runtime data.

## Seed Mapping

Only old bots that already had `national_bot.py` were promoted into the new
namespace:

```text
claude_v274 -> national_v1
claude_v276 -> national_v2
claude_v279 -> national_v3
claude_v283 -> national_v4
claude_v284 -> national_v5
claude_v285 -> national_v6
claude_v286 -> national_v7
claude_v287 -> national_v8
claude_v288 -> national_v9
claude_v290 -> national_v10
claude_v291 -> national_v11
claude_v292 -> national_v12
claude_v293 -> national_v13
claude_v294 -> national_v14
claude_v295 -> national_v15
claude_v296 -> national_v16
```

All other old top-level `bots/bot*`, `bots/claude_v*`, `bots/mixture_main`, and
`bots/neural_bot` directories were archived out of the active `bots/` namespace.
`bots/neural_national_lab/` remains a separate tracked experiment area and is not
part of the active evolution population.

## Operating Rules

1. Start new ratings, H2H, match history, replay analysis, and battle experience
   from this epoch only.
2. Do not read old `web/core/results/`, `results/`, or `ladder_results/` payloads
   into active prompts or gates.
3. Do not create new `claude_v*` directories for evolution.
4. Do not create new `bot-v*` completion tags for evolution.
5. If a legacy bot must be compared, pass it by explicit archived path as a
   diagnostic, not as a default opponent or source.
