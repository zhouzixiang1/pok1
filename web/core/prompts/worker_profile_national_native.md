<national_native_profile>
This generation's formal submission is `national_bot.py`, a direct client for
the official national TCP server. It must not depend on
`sever/bot_adapter.py`, newline framing, Botzone request/response JSON, or a
legacy subprocess entry. A legacy `main.py` may remain for strategy reuse, but
it is neither the pass condition nor a substitute for native verification.

The Web Arena is only a local debugging and presentation harness. Do not depend
on Arena modules and do not claim Arena success proves official compliance. The
standalone formal entry must still pass the official Windows EXE suite.

Strength is separate from compliance. One strength sample is one complete
70-hand local native TCP match. A positive final net-chip result is a win, a
negative result is a loss, and zero is a draw. Optimize and report the primary
positive/negative/draw outcome first; net-chip magnitude is secondary. Never
use official EXE or Web Arena chip outcomes as strength evidence.

<national_tcp_compatibility>
- Send exactly one raw action for the current pending decision: `raise <amount>`,
  `fold`, `call`, `check`, or `allin`, with no trailing newline or stdout text.
  Never emit a JSON `response` object from the formal entrypoint and never send
  wire token `bet`.
- The EXE uses a TCP byte stream without message boundaries. Preserve a sticky
  packet splitter for concatenated messages such as
  `earnChips -100preflop|...`; fragmented reads must also reassemble safely.
- The EXE may omit a street-closing `call` or `check`. Before processing a new
  street, showdown, settlement, or hand reset, the wrapper must infer only the
  forced missing closer and apply it exactly once to pot, stacks, street bets,
  history, and opponent tracking. If the closer was relayed, do not infer or
  count it again. Complete this repair before clearing street state and before
  building the next strategy request; strategy code must never guess it.
- Preserve `POK_OFFICIAL_ACTION_DELAY`, `_send_wire_action`, and the official
  default near 0.30 seconds. Local strength evaluation may override the delay
  to zero; strategy code must not bypass or relocate wire throttling.
- Never add timeout-rescue loops or unsolicited `call`/`check` sends. After an
  all-in is called, do not act again before settlement.
- A match is 70 hands. Each hand resets both stacks to 20000 with 50/100 blinds.
  Dealer is SB; BB is `1 - dealer_id`. SB acts first preflop; BB acts first on
  flop/turn/river; roles alternate each hand.
- `raise <amount>` is raise-to-total. First preflop raise-to is at least 200,
  first postflop raise-to at least 100, and each re-raise is at least
  `prev * 2` inclusively. The official EXE accepts exact 2x; `prev * 2 + 1`
  may be retained as conservative sizing headroom. A raise must exceed the
  current street bet and fit the stack; a raise using all remaining chips
  becomes `allin`.
- Preserve formal policy `official-full-v5` and both content-pinned 2026-07-11
  oracle boundaries. Exact consecutive 2x
  is legal; `2x + 1` is only conservative headroom. For a natural 70-hand
  official finish, starts 1..70 plus paired TCP settlements 1..69 are complete
  only with no pending action/wire issue and a new strict THP proving
  `STATE:0..69`, the cross-bound first-69 earnings, final zero-sum earnings, and
  footer total. Never fabricate hand-70 `earnChips` and never treat 69
  settlements alone as complete. Official/THP chips and winners remain
  compliance-only and must not enter Glicko, H2H, precommit strength, source
  selection, or experience.
  The exact sources are
  `docs/official-raise-boundary-oracle-2026-07-11.md` and
  `docs/official-terminal-settlement-oracle-2026-07-11.md`.
- Postflop first action cannot be `call`. After any first postflop action,
  `check` is illegal. If the first player checks, the second passes with
  `call`. Preflop BB after an SB limp checks, raises, or folds, never calls.
- After one all-in, the opponent may only call or fold; consecutive all-ins are
  illegal.
</national_tcp_compatibility>

<national_runtime_architecture>
- Use `time.monotonic`. The socket layer owns a 55,000 ms hard deadline and a
  immediately available socket-safe fallback: fold while facing a positive
  amount, otherwise pass with action `0`. Strategy must publish a sanitized
  baseline by 250 ms; bounded refinement may continue only until the 54,000 ms
  refinement deadline. Deadline/error paths return the latest sanitized
  candidate and discard late results.
- The socket-owning process must never execute candidate strategy code. The
  provided persistent strategy worker is a non-daemon, killable child process,
  so a declared bounded fixed CPU pool may use multiple cores. Its descendants
  inherit a POSIX process group or Windows process tree; a missed deadline
  terminates the entire tree and the next decision starts a clean worker.
  Do not replace this boundary with a thread, an executor future, or a
  permanently poisoned fallback mode.
- New national-native strategy code must implement both
  `get_baseline_action(req, current_request_view)` and
  `iter_refinements(req, current_request_view, baseline, deadline)`. The
  iterator yields increasingly informed candidate actions and checks the
  monotonic deadline before every expensive unit. The second argument is a
  bounded current-hand compatibility view, never complete match history.
  Cross-hand evidence comes only from `req['opponent_runtime']`.
  Local native strength evaluation, including precommit, uses a 2.0 s action /
  1.8 s refinement envelope and a 420 s match timeout, while the formal entry
  retains 55 s/54 s. Terminate
  cheap and low-uncertainty decisions early; reserve long formal work for
  ambiguous spots where more batches can change EV/action.
- A v4 state-learning task selects exactly one primary innovation: one bounded
  work primitive, one opponent-profile dimension, or one line control. Implement
  that primary and preserve parent capabilities; other visible dimensions are
  shadow/advisory, not an invitation to rewrite them all in one generation.
- For all line/history-derived decisions, consume authoritative
  `req['hand_runtime']`: `preflop_aggressor`, `preflop_spot`, `hero_position`,
  `previous_street` (`checked_through`/`opponent_checked_back`), `can_donk`,
  `can_delayed_probe`, `street_open`, `spr`, and `pot_odds`. Do not reconstruct
  these values from `current_request_view`, archived `requests`, or ad hoc
  action scans. Do not scan complete match history during a decision.
- Consume the standard `precompute.py` pure-fact tables before adding another
  artifact. It builds all 1,326 hole-combination facts, 8,192 straight-mask
  lookups, and 21 five-of-seven index tuples once per worker lifetime. Never
  rebuild these spaces in a decision. Prefer additional bounded module-import precomputation
  in inspectable mappings for domain facts such as preflop
  buckets, board masks, range weights, and evaluator shortcuts.
  A formal artifact declares key shape, exact `module.function` consumer,
  module-import build phase, maximum entries/bytes/build time, and
  `legal_baseline` behavior when empty. Opaque LRU caches, warmed-on-first-use
  work, dead consumers, network/file I/O, and large decision-time table builds
  do not satisfy the contract.
- The process persists for all 70 hands. Maintain bounded match-level opponent
  state incrementally from opponent actions, `oppo_hands`, and `earnChips`.
  Hand state resets each hand; match state resets only on a new TCP connection.
  Persist every terminal opponent fold/call, whether relayed or inferred, even
  if no later hero decision occurs in that hand. Do not let hand reset discard
  an unflushed opportunity/response counter.
  Use explicit priors, context-specific confidence, and a capped adaptation
  weight. Context at minimum distinguishes street, position, facing-action
  kind, and size bucket. Sparse contexts stay close to the parent action; a
  high global sample count must not create false confidence in an unseen river
  or large-bet context.
- A showdown is not merely a log row. Update bounded
  `req['opponent_runtime']['showdown_range']` weights from the pinned uniform
  1,326-combination prior, effective sample count/confidence, showdown reach
  rate, and capped adaptation weight. Preserve the explicit
  `reached_showdown_only` scope and selection-bias guard: revealed hands are a
  selected subset.
  Wire the posterior into a reachable range/equity/action consumer; raw
  showdown counts or telemetry with no action influence are incomplete.
- Every new or materially changed strategy module needs dynamic evidence for
  `producer -> consumer -> sanitized action -> telemetry`: name the functions,
  replay a real national transcript, declare the exact firing tuple, compare a
  one-predicate control, and show both an action difference and nonzero consumed
  telemetry. A helper that passes only an isolated unit test is still dead.
- Donk reachability transcript: hero BB observes an SB raise, calls, and acts
  first on the flop. Use `hand_runtime.preflop_aggressor`, `hero_position`,
  `street_open`, and `can_donk`; the control changes only the aggressor/line
  predicate. Delayed-probe reachability transcript: hero BB calls an SB raise,
  checks the flop, the in-position aggressor passes with official wire `call`
  (possibly inferred when the EXE omits it), and hero acts first on the turn.
  Use `previous_street.checked_through`,
  `previous_street.opponent_checked_back`, and `can_delayed_probe`; never require
  a literal postflop `check/check` or hero in-position for this line.
- The legal baseline must complete strictly under 250 ms. Compare fixed-seed
  budget tiers. The system worker—not
  candidate metadata—records iterator steps, worker CPU/elapsed time, true
  `StopIteration`, termination reason, and each sanitized action trajectory.
  Candidate `sample_count`, confidence, and `complete` are reported diagnostics
  only. A selected staged-compute primary needs at least eight trusted steps and
  5 ms measured long-tier work, then either larger-budget scaling or true equal
  finite exhaustion. At least one predeclared scenario must change after
  sanitization into the hypothesized improved action. `iter_refinements` may not yield the unmodified input baseline,
  emit empty candidates, repeat cached output, or rely on self-reported
  completion as evidence.
- Do not rescan complete history, perform external I/O, write files, build
  unbounded tables, or run unbounded simulation in a decision. Every expensive
  loop requires a hard cap and deadline check.
- If official EXE feedback is cited, fix the identified protocol,
  communication, state-machine, timeout, or obvious decision error before any
  strategy tuning. EXE win/loss is not strength evidence.
</national_runtime_architecture>

<profile_verification>
Run all of these without writing probe artifacts:
1. `python -m py_compile bots/national_v{version}/*.py`
2. `(cd bots/national_v{version} && python -B -c "import national_bot")`
3. `PYTHONPATH=web/core python -B -c "from national_native import check_native_contract; e=check_native_contract('bots/national_v{version}', require_current_stream_decoder=True, require_current_decision_runtime=True); print(e); raise SystemExit(bool(e))"`
4. Inspect the formal native diff and confirm no adapter import, JSON response,
   newline-framed socket read, stdout diagnostic, or full-history decision scan.
The trusted quality gate owns the sandbox runtime capability probe and local
70-hand TCP battle; do not replace either with a Botzone `main.py` smoke.
</profile_verification>
</national_native_profile>
