# Official EXE Wire Probe

`scripts/official_wire_probe.py` runs the Windows national platform through a
transparent TCP proxy. Use it when a bot passes local native TCP tests but the
official EXE reports illegal `check`, illegal `allin`, or no response within
60 seconds. Both inputs must be strict five-file national-policy directories;
they may be unpublished diagnostic candidates, but arbitrary script paths are
not executable inputs.

Before either connection is consumed, the probe content-binds and seals the
five source files, validates the tracked formal execution profile, and launches
the bot through the same managed sandbox and endpoint-lease boundary used by
official certification. The host bot directory is never mounted.

The probe records both raw byte streams and a replay summary:

```bash
python scripts/official_wire_probe.py \
  --candidate bots/national_v<N> \
  --opponent bots/national_v<M> \
  --target-hands 3 \
  --results-dir /tmp/pok_wire_probe
```

This standalone proxy is deliberately limited to `--target-hands 1..69`
(default `1`).  The official EXE emits no wire settlement for natural hand 70,
so the proxy alone cannot prove that hand.  A request for 70 is rejected rather
than weakened to a generic `target - 1` rule.  Full completion must use
`python scripts/official_certify.py full ...`, which cross-binds wire starts
1..70 and settlements 1..69 to strict THP states 0..69 and the footer.

The system-owned scripted client remains a low-level parser fixture; it is not
an accepted bot-path substitute for this probe. The original platform sample
is archived and is not wrapped or launched by active tooling.

Important artifacts:

- `wire_events.jsonl`: timestamped raw TCP chunks, parsed messages, and leftover
  partial buffers for each direction.
- `wire_summary.json`: replayed protocol state, illegal action classifications,
  response timings, pending expected actions, and platform silent gaps.
- `receipt.json`: managed launch/artifact/isolation identity, environment,
  diagnostic pass/fail, zero certification/rating weight, and artifact paths.

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
