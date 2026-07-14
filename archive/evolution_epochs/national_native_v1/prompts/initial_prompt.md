# National Bot Bootstrap

Create the first fresh strict-policy heads-up No-Limit Hold'em bot under
`bots/national_v{version}/` for the national competition TCP platform. The
archived version high-water chooses this label only; no archived source,
ratings, planning evidence, or certification may be inherited.

## Required artifact

- `national_bot.py` — system-owned raw TCP runtime and sole formal entrypoint.
- `policy.py` — candidate-owned decision policy.
- `precompute.py` — system-owned bounded pure poker facts.
- `national_runtime_manifest.json` — system-owned runtime/file identity.
- `policy_epoch_receipt.json` — system-owned fresh-lineage receipt.

These five files are the complete artifact. `policy.py` is the only
candidate-owned writable file; candidate helper modules, assets, a second
transport entrypoint, or an alternate action ABI are contract violations.

## Policy ABI

`policy.py` implements both a fast
`get_baseline_decision(decision_context)` and bounded
`iter_decisions(decision_context, baseline, deadline)` (the iterator may finish
without yielding when no refinement is useful).
Return exactly one typed intent mapping:

- `{"kind": "pass"}`
- `{"kind": "fold"}`
- `{"kind": "allin"}`
- `{"kind": "raise", "raise_to": <positive integer>}`

The socket runtime alone maps `pass` to official `call`/`check`, validates
legal actions, and sends the raw wire token. Policy must use the `allin` intent
for exact stack commitment; a stack-consuming `raise` is invalid. `raise_to` is
the exact street total; never add the current street contribution again.

## National protocol

- 70 hands per match; each hand resets both stacks to 20000; blinds 50/100.
- Dealer/SB acts first preflop; BB acts first postflop; seats alternate.
- TCP has no newline framing or message boundaries. Handle arbitrary fragments
  and sticky packets.
- Send only `raise <amount>`, `fold`, `call`, `check`, or `allin`; never `bet`.
- First preflop raise-to is at least 200, first postflop raise-to at least 100,
  and every re-raise is at least exact 2x the prior raise-to. Exact 2x is legal.
- Preserve the official action-send delay in the socket layer and never send an
  unsolicited timeout-rescue action.
- The runtime must repair only boundary-proven omitted terminal call/check
  actions before clearing street state.

The baseline must be legal and finish under 250 ms. Expensive refinement must
be bounded and check the monotonic deadline. The implementation must remain
stdlib-only and must not perform candidate-owned network, subprocess, or file
I/O during decisions.
