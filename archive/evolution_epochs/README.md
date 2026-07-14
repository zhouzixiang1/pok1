# Evolution epoch archives

This directory contains immutable snapshots of retired evolution epochs. Some
snapshots are tracked source trees; large runtime payloads may remain local and
gitignored. Every epoch directory must identify its trust boundary in its own
README.

`national_native_v1` is retired. Its bots used a raw-TCP outer wrapper around an
old JSON/request-response and integer-action strategy ABI, so they are archived
under `national_native_v1/bots/` and are legacy-untrusted.

The active epoch is `national_tcp_policy_v1`:

- active bot directories are created under `bots/national_v<N>/`;
- the candidate ABI is the strict typed policy ABI owned by the raw national TCP
  runtime;
- historical version high-water and `national-bot-v<N>` tag numbering may
  continue from v142 to v143, but that preserves publication identity only and
  does not inherit old bot code or evidence;
- no rating, H2H row, experience, capability claim, prompt evidence, result, or
  certification from a retired epoch enters active discovery or evaluation.

Never add an archive directory to a production `PYTHONPATH`, scan archived bot
names as active candidates, or use an old completion tag by itself as active
epoch eligibility.
