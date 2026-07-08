# Official EXE Wire Probe

`scripts/official_wire_probe.py` runs the Windows national platform through a
transparent TCP proxy. Use it when a bot passes local native TCP tests but the
official EXE reports illegal `check`, illegal `allin`, or no response within
60 seconds.

The probe records both raw byte streams and a replay summary:

```bash
python scripts/official_wire_probe.py \
  --candidate bots/national_v103 \
  --opponent bots/national_v70 \
  --target-hands 3 \
  --results-dir /tmp/pok_wire_probe
```

To run the root official sample without editing it:

```bash
python scripts/official_wire_probe.py \
  --candidate untitled0-1.py --candidate-kind sample \
  --opponent bots/national_v70 \
  --target-hands 1 \
  --results-dir /tmp/pok_wire_probe_sample
```

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
- A large `max_platform_silent_gap_sec` with no `pending_expected_actions` is
  platform silence, not bot no-response evidence.

The normal official acceptance harness remains the pass/fail compliance gate.
This probe is for root-cause evidence when the EXE and local simulator disagree.
