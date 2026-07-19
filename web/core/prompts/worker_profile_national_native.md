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
- The 2021 EXE may omit the remaining called-all-in board and proceed directly
  to settlement/`oppo_hands`. Never fabricate those unseen cards; the
  system-owned replay must cross-bind complementary actions and exact all-in
  net settlement, while strict THP exact-prefix-or-five-card
  board/action/blind/hole/earnings binding must cover either the exact observed wire
  prefix or a full board. A live
  deferred-action warning never changes final policy legality.
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
  `docs/official-raise-boundary-oracle-2026-07-11.md`,
  `docs/official-terminal-settlement-oracle-2026-07-11.md`, and
  `docs/official-allin-runout-wire-oracle-2026-07-19.md`.
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

Both entrypoint functions are mandatory. `deadline.baseline_target_ms` is the
authoritative per-decision target (native precommit can set it to 200 ms; 250
ms remains the formal ceiling); refinement yields are optional, bounded, and
monotonic-deadline-aware. The iterator checks `time.monotonic()` before every
expensive unit and yields only after new work.

The current checked-in strict policy uses a fixed deterministic 192/256/96
flop/turn/river baseline schedule, with two direct
`precompute.evaluate_seven` evaluations per sample (at most 512 on a valid
board). A compact prior is only an invalid/degraded-input fallback or a
refinement starting point; do not replace valid-board baseline sampling with
one. The static gate rejects evaluator aliases (including imported, closure,
or default aliases), `itertools.combinations`, and nested deck-pair sweeps in
the baseline. The dynamic gate rejects more than 800 top-level evaluator calls.
Full `C(45,2)` river enumeration belongs only to bounded,
deadline-checked `iter_decisions` batches after a legal baseline is published.

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
  `street_index`, `position`, `acts_first_postflop`, and the system-derived
  `match_control` proof;
- `betting.pot`, both stacks and street bets, `effective_stack`, `to_call`,
  `spr`, `pot_odds`, and `call_closes_allin_runout`;
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

Preserve the checked-in strict baseline unless the compiled Worker contract
explicitly selects one mechanism and supplies a causal replacement: calibrated
system 169-class preflop equity; `preflop_spot`-specific raise-to-total sizing;
exact-stack `allin`; strict lock-win folding only from a complete consistent
`hand.match_control`; flop/turn position realization only for nonclosing calls;
and opponent current-action range tilt computed on the current board rather
than a sampled runout. The boolean
`betting.call_closes_allin_runout` overrides action-text guesses. Missing,
malformed, or contradictory control fields must produce the neutral behavior,
not a reconstructed compatibility path.
</policy_abi>

<runtime_architecture>
- The socket-owning process never executes candidate policy. The official
  `name` handshake is a real system-owned protocol boundary: it replies with
  the team name after initiating a persistent, killable, non-daemon worker
  launch. That wire evidence is a launch-initiated proof, never a proof that
  policy import or worker readiness completed. Candidate code must not move,
  duplicate, or claim this startup path; the first decision wall-clock begins
  at the decision and includes any unfinished policy import. A missed deadline terminates the complete process group/tree before a clean worker is started.
- The socket owner has an immediately available typed fallback: fold while
  facing a positive amount, otherwise pass. Formal timing uses a 55 s hard
  deadline and 54 s refinement budget; local native strength uses a 2.0 s
  action / 1.8 s refinement envelope. The system derives each complete
  70-hand native match liveness identity from that envelope and the active
  validator's tight 20,000-chip 34-request hand bound. The generic 100-request
  street guard is a separate system safety backstop. `NativeMatchTimingPlan`,
  its digest, the engine-only progress heartbeat, and the one bounded
  orchestrator extension are system infrastructure, never strategy knobs. A
  cap hit or whole-match timeout is fail-closed evidence, never a normal street
  closure or a candidate-controlled retry. Candidate policy must not assume,
  reduce, bypass, or claim authority over a fixed match timer.
- Candidate imports and decisions may not perform network access, external
  process execution, candidate-owned file I/O, unbounded allocation, or
  unbounded simulation. Expensive loops need a hard cap and a deadline check.
- `precompute.py` is system-owned, read-only pure import-time data. Reuse its 1,326 hole
  combinations, 8,192 rank masks, and 21 five-of-seven selections before
  doing equivalent work. Candidate-owned tables/assets and candidate file I/O
  are outside this epoch ABI. A future file-backed model/table is allowed only
  outside the Bot directory through a system-owned, manifest-and-receipt-bound
  asset broker with byte/query caps, no-follow verification, common launch-path
  resolution, and an observed influence gate; it is an infrastructure change,
  never a Worker edit. Until that ABI is admitted, the policy has no external
  asset access.
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
- Structural-air line bluffs must remain bounded and non-fixed. Prove two
  pinned no-hole-draw identities on the same real line and identical stable
  non-card context after removing only the two absolute monotonic deadline
  fields: one socket-valid raise, one passive `check`, and a matched ablation
  of only the enabling line predicate/opportunity tag. `allin` remains
  aggressive and cannot serve as the passive identity. A 100%
  raise-on-line branch fails quality even if its first example is reachable.
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
