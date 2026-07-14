# Official EXE Wire Probe

`scripts/official_wire_probe.py` runs the Windows national platform through a
transparent TCP proxy. Use it when a bot passes local native TCP tests but the
official EXE reports illegal `check`, illegal `allin`, or no response within
60 seconds.

The probe records both raw byte streams and a replay summary:

```bash
python scripts/official_wire_probe.py \
  --candidate bots/national_v<N> \
  --opponent bots/national_v<M> \
  --target-hands 3 \
  --results-dir /tmp/pok_wire_probe
```

For a deterministic diagnostic peer, use the maintained raw-stream client:

```bash
python scripts/official_wire_probe.py \
  --candidate bots/national_v<N> \
  --opponent scripts/official_scripted_bot.py \
  --target-hands 1 \
  --results-dir /tmp/pok_wire_probe_scripted
```

The original platform sample is archived and is not wrapped or launched by
active tooling.

Important artifacts:

- `wire_events.jsonl`: timestamped raw TCP chunks, parsed messages, and leftover
  partial buffers for each direction.
- `wire_summary.json`: replayed protocol state, illegal action classifications,
  response timings, pending expected actions, and platform silent gaps.
- `receipt.json`: command, environment, pass/fail, and artifact paths.

Interpretation:

- `wire_illegal_check`: usually means a bot sent `check` as the second postflop
  pass after an opponent check. It must send `call`.
- `wire_illegal_allin`: usually means the bot sent `allin` after the current
  betting round already had an all-in. It must call or fold.
- `pending_bot_response_timeout`: the replay believed it was the bot's turn and
  no action arrived within the configured timeout.
- `unsolicited_client_action`: the bot sent an action while the replay had no
  pending platform request. This is useful for diagnosing fallback timers that
  send extra `call`/`check` during official EXE silence.
- A large `max_platform_silent_gap_sec` with no `pending_expected_actions` is
  platform silence, not bot no-response evidence.

The low-level acceptance harness can classify a diagnostic EXE suite, but it
does not issue publication authority. The only formal compliance gate is the
signed `official-full-v5` path in `scripts/official_certify.py` (5 self-play +
3 eligible-opponent rounds, 70 hands each). This probe is root-cause evidence
when the EXE and local simulator disagree; it cannot certify or rate a bot.
