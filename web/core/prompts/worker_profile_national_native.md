<national_native_profile>
This generation belongs only to `national_tcp_policy_v1` and produces a raw
native national-competition TCP bot. The
formal submission is the system-owned `national_bot.py`; candidate strategy
code lives behind the versioned `policy.py` ABI. The candidate has exactly one
transport owner and exactly one typed policy ABI.

The Web Arena is diagnostic only. Local native matches provide strength
samples only when a complete 70-hand TCP match finishes. Official Windows EXE
runs provide compliance evidence only. Arena and official chip totals never
enter Glicko, H2H, source selection, planning evidence, or precommit strength.

<national_tcp_contract>
- The wire is a delimiter-free raw TCP byte stream. Reads may split one
  platform token or coalesce several tokens. The incremental runtime decoder
  must emit complete protocol tokens before changing state.
- Send exactly one canonical action for the pending decision: `raise <amount>`,
  `fold`, `call`, `check`, or `allin`. The socket owner emits one exact token
  only while the platform is awaiting that decision.
- `raise <amount>` is raise-to-total. The first preflop raise-to is at least
  200, the first postflop raise-to at least 100, and a re-raise is at least
  exactly 2x the preceding raise-to. Exact 2x is legal; `2x + 1` is optional
  strategy headroom. A stack-consuming raise is encoded as `allin`.
- Postflop first pass is `check`; after the first postflop action a pass is
  `call`. After an SB limp, BB passes with `check`. After an all-in, the peer
  may only call or fold and the bot must not act during the runout.
- Preserve the system wire throttle: `POK_OFFICIAL_ACTION_DELAY`, default near
  0.30 seconds, is applied by `_send_wire_action`. Policy code never sleeps or
  sends bytes.
- The official EXE may omit a forced street-closing peer call/check. The socket
  reducer repairs only a boundary-proven closer, exactly once, before clearing
  street contributions or publishing the next decision context. A relayed
  closer suppresses inference.
- Formal completion policy is `official-full-v5`: five 70-hand self-play rounds
  plus three 70-hand rounds against an eligible published strict-policy
  opponent. The sole exception is v143 while the strict pool is empty: it
  parks for the operator-only, one-time `bootstrap-first-strict` control
  `first_strict_control_v1`. No Worker or other LLM role may invoke,
  acknowledge, auto-fallback to, or treat that control as strength, and v144+
  may never use it. Only an operator may later run `finalize-first-strict`
  after the certificate validates; no Worker may publish it. A natural hand 70
  may omit its TCP settlement; completion
  then requires starts 1..70, settlements
  1..69, no pending action/wire issue, and a fresh strict THP proving
  `STATE:0..69`, cross-bound first-69 earnings, final zero-sum earnings, and the
  footer. Never synthesize hand-70 `earnChips`.
- Exact oracle inputs are
  `docs/official-raise-boundary-oracle-2026-07-11.md` and
  `docs/official-terminal-settlement-oracle-2026-07-11.md`.
</national_tcp_contract>

<policy_abi>
The complete candidate decision surface is `policy.py`. The system runtime
imports exactly that module and owns every other artifact in the five-file
submission ABI: `national_bot.py`, `precompute.py`,
`national_runtime_manifest.json`, and `policy_epoch_receipt.json`.

`policy.py` implements:

```python
def get_baseline_decision(decision_context): ...

def iter_decisions(decision_context, baseline, deadline):
    ...
    yield candidate
```

Both entrypoint functions are mandatory. The baseline must finish strictly
under 250 ms; refinement yields are optional, bounded, and
monotonic-deadline-aware. The iterator checks `time.monotonic()` before every
expensive unit and yields only after new work.

A decision is a strict primitive mapping:

- `{"kind": "pass"}` — the socket owner maps this to official `call` or
  `check` from authoritative state.
- `{"kind": "fold"}`
- `{"kind": "allin"}`
- `{"kind": "raise", "raise_to": <positive integer>}`

No other fields or kinds are valid. In particular, policy must not return
`call`, `check`, a bare string, or an integer. The socket owner validates the
mapping again, rejects illegal raise targets, maps `pass`, and emits
the canonical wire token. `RAISE_TO(400)` must mean exactly `raise 400`; no
layer may add the current street contribution again. Exact stack commitment
must use `{"kind": "allin"}`; a stack-consuming `raise` is invalid.

`decision_context` is a bounded schema-versioned snapshot produced once by the
socket reducer. Consume its declared sections and names directly:

- `cards.encoding`, `cards.hole`, and `cards.board`;
- `hand.number`, `total_hands`, `remaining_including_current`, `street`,
  `street_index`, `position`, and `acts_first_postflop`;
- `betting.pot`, both stacks and street bets, `effective_stack`, `to_call`,
  `spr`, and `pot_odds`;
- bounded semantic `history.actions` and `history.truncated_count`;
- `line.preflop_aggressor`, `preflop_spot`, `street_open`,
  `responding_to_check`, `can_donk`, `can_delayed_probe`, and current/previous
  street summaries;
- `legal.policy_kinds`, `pass_wire_kind`, exact `min_raise_to`/
  `max_raise_to`, and `raise_boundary`;
- bounded `opponent`, including `terminal_response` and selection-aware
  `showdown_range` evidence;
- `deadline.hard_monotonic`, `refinement_monotonic`, and trusted budget values.

Do not reconstruct these values from raw TCP text, rescan full-match history,
or infer omitted actions in policy code.
</policy_abi>

<runtime_architecture>
- The socket-owning process never executes candidate policy. A persistent,
  killable, non-daemon worker owns policy imports and any declared fixed CPU
  pool. A missed deadline terminates the complete process group/tree before a
  clean worker is started.
- The socket owner has an immediately available typed fallback: fold while
  facing a positive amount, otherwise pass. Formal timing uses a 55 s hard
  deadline and 54 s refinement budget; local native strength uses a 2.0 s
  action / 1.8 s refinement envelope and 420 s match timeout.
- Candidate imports and decisions may not perform network access, external
  process execution, candidate-owned file I/O, unbounded allocation, or
  unbounded simulation. Expensive loops need a hard cap and a deadline check.
- `precompute.py` is system-owned, read-only pure import-time data. Reuse its 1,326 hole
  combinations, 8,192 rank masks, and 21 five-of-seven selections before
  doing equivalent work. Candidate-owned tables/assets and candidate file I/O
  are outside this epoch ABI; a proposal that needs a new asset is blocked on
  an infrastructure-owned packager/manifest change, not a Worker edit.
- Match-level opponent state persists for one TCP connection and updates
  incrementally from actions, inferred terminal responses, showdown cards, and
  settlement. Hand state resets each hand. Sparse contexts retain explicit
  priors and capped influence.
- Showdown observations are selected evidence, not an unbiased range sample.
  Keep `reached_showdown_only`, effective sample/confidence, prior, and capped
  adaptation explicit, then prove a reachable decision consumer.
- Donk reachability: hero BB calls an SB raise and acts first on the flop.
  Delayed probe reachability: hero BB calls an SB raise, checks flop, the
  in-position aggressor passes (wire `call`, possibly boundary-inferred), and
  hero acts first on turn. Use reducer-owned
  `decision_context.line.previous_street.checked_through`,
  `opponent_checked_back`, and `decision_context.line.can_delayed_probe`;
  never look for an official-invalid postflop `check/check`.
- A staged-compute primary must show at least eight trusted refinement steps,
  at least 5 ms measured long-tier work, and a predeclared socket-validated intent
  difference or true equal finite exhaustion. Candidate-reported confidence or
  sample counts are diagnostics, not trusted work evidence.
</runtime_architecture>

<verification>
Workers may inspect the lease candidate and compile the exact edited file. The
trusted quality gate exclusively owns imports, candidate execution, native TCP
smoke and dynamic tests. Worker-visible verification is:

1. `python -m py_compile {candidate_path}/policy.py`
2. Read every edited region and report the intended reachable consumer.
3. Leave import/native-contract/smoke/self-test execution to the system gate.
4. The system boundary, not the Worker, proves that only `policy.py` changed,
   every system artifact is byte-identical, and the import graph is exactly the
   system runtime calling the typed policy ABI. Do not open a historical parent
   or reconstruct another diff.

The resulting system quality receipt must show `check_native_contract` ran with
`require_current_stream_decoder=True` and
`require_current_decision_runtime=True`; these are gate-owned settings, not
Worker Bash commands.

Local Arena output is diagnostic only and cannot certify strategy semantics.
The quality gate replays delimiter-free fragmented and coalesced TCP
transcripts and checks typed-intent causal effects.
</verification>
</national_native_profile>
