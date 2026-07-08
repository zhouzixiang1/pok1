# Official National EXE Platform Analysis

Date: 2026-07-08

Target EXE:

`sever/国赛平台/德州扑克对弈平台限时一分钟2021版/德州扑克对弈平台限时一分钟2021版.exe`

SHA-256:

`9d01b443d4920a7e06a487d87ea1b050ea2ca5359023602f98c3c236c734e81a`

## Scope

This report records the current static and dynamic analysis of the official
Windows self-play platform. The goal is not to replace the platform, patch the
binary, or rely on undocumented behavior for strength evaluation. The goal is
to make official-protocol compliance reproducible from this repository.

## Static Findings

- The executable is a 64-bit Windows GUI PE built on 2021-07-27 with MFC/Visual
  Studio 2013 era runtime strings.
- Network code imports `WS2_32.dll` by ordinal. The observed ordinals map to
  ordinary TCP server functions including `WSAStartup`, `socket`, `bind`,
  `listen`, `accept`, `recv`, `send`, `closesocket`, and `WSAGetLastError`.
- The protocol tokens are stored in plaintext in `.rdata`: `preflop|`,
  `flop|`, `turn|`, `river|`, `earnChips `, `oppo_hands|`, `raise`, `call`,
  `check`, `fold`, and `allin`.
- The THP/export strings are also present near `Dream6View.cpp`, including
  `STATE:`, `23456789TJQKA`, and `shdc`.
- The default port `10001` was not found as an ASCII string. It is likely owned
  by UI/config state or an integer constant rather than a literal string.
- The resource/string layout is normal C++/MFC object data, not a clean table
  that can be safely interpreted without deeper disassembly.

## Document Findings

The official documents confirm the intended protocol:

- Platform is server, bots are TCP clients.
- The platform sends `name`, stage/card messages, opponent actions,
  `earnChips`, and `oppo_hands`.
- Client actions are `raise <amount>`, `fold`, `call`, `check`, `allin`.
- `bet` must not be sent.
- Postflop first `call` is illegal; postflop non-first `check` is illegal.
- A `raise` amount is the target street total, not a delta.
- Each action has a 60 second decision timeout.

The documents do not state that clients must sleep before sending actions.
However, the official sample script in the repository calls `time.sleep(0.3)`
before sending responses.

## Dynamic Probe Setup

Dynamic tests were run through:

- `scripts/official_wire_probe.py`
- `scripts/official_scripted_bot.py`
- Wine/Xvfb official EXE automation
- A transparent two-port TCP proxy that records raw bytes in both directions

The scripted bot is deterministic and strategy-free. It is only a diagnostic
client for reproducing official EXE state-machine behavior.

## Key Dynamic Finding

The official EXE has a timing-sensitive state-machine race. If a bot replies
too quickly after the platform sends a street message or after a betting round
closes, the EXE can silently miss or defer the transition. It then waits for
approximately the official 60 second timeout and eventually settles the hand.

This is not bot compute timeout:

- The bot sends the action immediately.
- The proxy sees no TCP traffic from the platform during the long gap.
- The gap length is about 60 seconds plus UI/settlement overhead.

This is also not just a `raise` legality issue:

- A pure `check/call` line reproduced the long silent gap with zero action
  delay.
- `flop raise/fold` can complete normally with zero delay.
- `flop raise/call, turn raise/call` reproduces the long gap with zero or
  0.1 second delay, but clears with 0.2 to 0.3 second delay.

## Reproduction Matrix

| Scenario | Action delay | Result |
|---|---:|---|
| `check_call_down` | 0.0s | Max silent gap `63.807s`, hidden timeout |
| `check_call_down` | 0.3s | Max silent gap `3.008s`, clean |
| `flop_bet_fold` | 0.0s | Max silent gap `4.720s`, clean |
| `flop_bet_call_turn_bet_call` | 0.0s | Max silent gap `63.707s`, hidden timeout |
| `flop_bet_call_turn_bet_call` | 0.1s | Max silent gap `63.305s`, hidden timeout |
| `flop_bet_call_turn_bet_call` | 0.2s | Max silent gap `3.417s`, clean |
| `flop_bet_call_turn_bet_call` | 0.3s | Max silent gap `3.009s`, clean |

Evidence directories:

- `/tmp/pok_official_scenario_check_call_down/probe_20260708_143657`
- `/tmp/pok_official_scenario_check_call_down_delay03/probe_20260708_144008`
- `/tmp/pok_official_scenario_flop_bet_fold_delay0/probe_20260708_144045`
- `/tmp/pok_official_scenario_bet_call_turn_delay0/probe_20260708_144112`
- `/tmp/pok_official_scenario_bet_call_turn_delay01/probe_20260708_144423`
- `/tmp/pok_official_scenario_bet_call_turn_delay02/probe_20260708_144547`
- `/tmp/pok_official_scenario_bet_call_turn_delay03/probe_20260708_144348`

## Interpretation

The official EXE appears to need a small receive/send settling interval between
platform output and client action input. The official sample's `0.3s` delay is
therefore not accidental. It masks a platform-side race that is not described
as a formal protocol rule.

For this repository, "official-compliant" must mean more than "the action token
is legal." A bot that sends legal actions too quickly can still enter the
official EXE's 60 second timeout path.

## Repository Changes From This Analysis

- Added `scripts/official_scripted_bot.py` as a deterministic diagnostic client.
- Updated `web/core/official_wire_probe.py` so a `>=60s` no-wire interval is
  reported as `platform_silent_timeout_gap` instead of being hidden behind a
  final `passed=true` settlement.
- Updated `web/tests/test_official_wire_probe.py` to lock this behavior.
- Updated the native national bot template so generated `national_bot.py`
  entries keep an official-platform send throttle by default. Local native
  strength evaluation explicitly disables the delay through environment when it
  does not use the Windows EXE.
- Updated the official EXE harness/certification path so log-level 60 second
  silent gaps are reported as `official_log_silent_timeout_gap` and block parent
  selection.

## Engineering Requirements Going Forward

- Native national bot entrypoints should use an official-platform send throttle.
  A conservative default should be `0.25s` to `0.30s` before each action send.
- The throttle belongs in the native TCP entrypoint/wire layer, not in poker
  strategy code.
- The official EXE harness should fail a round that contains
  `platform_silent_timeout_gap`, even if the EXE later emits `earnChips`.
- The native entry contract should reject generated entries that remove
  `POK_OFFICIAL_ACTION_DELAY`, `_send_wire_action`, or the default official
  delay constant.
- Generated bots must not use unsolicited timeout-rescue loops. They should send
  exactly one legal action for the current pending decision; probe/harness code
  should surface 60 second silence instead of masking it with fallback sends.
- Local strength evaluation can still use the local national TCP server, but
  official acceptance must include the real EXE because the timing race is not
  represented by the local server.
- Existing native bots produced without the throttle should be considered
  official-risk until retested through the real EXE.

## Open Questions

- The exact minimum safe delay may depend on Wine, CPU load, and Windows UI
  timing. The measured threshold here is between `0.1s` and `0.2s`; use at least
  `0.25s` in production-facing bots.
- The binary likely has deeper C++ state-machine details, but current evidence
  is already sufficient to change the repository harness and bot wire layer.
