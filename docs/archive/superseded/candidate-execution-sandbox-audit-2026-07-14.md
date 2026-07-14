# Candidate execution sandbox audit (2026-07-14)

## Trust boundary

Every bot directory under `bots/` is untrusted, LLM-produced input. Reading or
parsing it on the host is allowed; importing or executing it in an authoritative
gate is not. The central execution boundary is
`web/core/candidate_sandbox.py`, backed by `managed_bot_executor.py`.

The boundary fails closed and has no ordinary-subprocess fallback. It provides:

- Bubblewrap user, pid, IPC, UTS, cgroup, and network namespaces;
- a compiled seccomp policy that denies INET socket creation and connection;
- a read-only candidate mount at `/work`;
- a cleared, allowlisted environment and isolated `/proc` and `/dev`;
- a bounded tmpfs at `/tmp` as the only anonymous writable filesystem;
- `prlimit` bounds for address space, processes, descriptors, file size, and
  core dumps;
- host wall-clock timeout plus process-group termination and reaping.
- anonymous host-owned stdout/stderr sinks with `RLIMIT_FSIZE` and a 1 MiB
  parent-side capture cap, avoiding unbounded pipe buffering;
- a per-run 256-bit completion nonce carried on a host-owned pipe and accepted
  only from a fresh output file written by the trusted postlude. Candidate
  stdout, JSON, success text, and exit status have no completion authority.
  Cross-thread frame/trace/profile entry points and native-introspection
  imports are removed or audit-denied before candidate Python starts, so code
  cannot steal the nonce from the trusted parent frame and call `os._exit(0)`.

An unavailable isolation primitive is an infrastructure failure. It cannot be
reclassified as a candidate miss or a passing gate.

## Executing entry points

| Entry point | Current boundary | Authority |
|---|---|---|
| `code_verification.run_import_contract_test` (`main`, `strategy`, `postflop`, `opponent`, `state`) | central candidate sandbox; trusted completion receipt required | quality/review blocker |
| embedded `strategy.py`, `postflop.py`, `opponent.py` self-tests | central candidate sandbox; trusted completion receipt required | quality blocker |
| legacy JSON decision scenarios | central candidate sandbox per scenario | quality blocker in legacy profile |
| legacy JSON smoke (`smoke_tester.py` and its child battles) | a minimal staged harness (smoke runner, local engine, stable reference only) runs inside one central sandbox | quality/review blocker in legacy profile |
| mandatory runtime fix probes (`card_utils`, `constants`) | central candidate sandbox | quality blocker |
| national runtime capability probe | existing `launch_isolated_worker` boundary with candidate read-only at `/inputs/bot` | quality blocker |
| native TCP smoke, precommit, daemon matches | existing `launch_managed_bot` boundary | native strength authority |
| Arena and official EXE bot clients | existing central managed/official bot boundary; exact system runtime required | Arena is diagnostic; signed official result is compliance only |
| native stream-decoder behavioral check | executes a private system-template copy, never candidate bytes | static/runtime contract blocker |
| legacy spot analyzer | central candidate sandbox | explicitly `diagnostic_zero_authority` |

## Non-authoritative legacy surfaces

`web/core/engine/battle.py`, `qd_fitness.py`, `exploitability_prober.py`, and the
top-level local `engine/` remain operator/archival Botzone utilities. Some still
launch supplied scripts directly when called as standalone legacy tools. They
are not part of the `national_native_v1` quality, rating, precommit, official, or
publication authority. They must not be reused as evidence by a national-native
gate. The active inline-eval and spot-check APIs label their results diagnostic
and have zero rating/publication weight.

The `national_native` profile also disables the Botzone-derived behavior
fingerprint/novelty advisory, legacy MAP-Elites niche fallback,
`exploitability_prober`, and QD post-commit launch. Native decision fixtures are
owned by `national_decision_tester.py`: they send delimiter-free fragmented and
sticky messages over an inherited loopback TCP stream to the exact national
entry, then validate the raw response with `sever.server.protocol` and
`sever.engine.validator`. They never load dynamic Botzone scenario sidecars.
Host isolation or endpoint failures propagate as infrastructure failures;
illegal, repeated, timed-out, or disconnected candidate actions remain
candidate gate failures.

## Counterexamples

`web/tests/test_candidate_execution_sandbox.py` proves that import-time and
self-test code cannot write the candidate tree or a host path, cannot connect to
a host loopback listener, cannot pass by exiting before the trusted completion
receipt, and cannot steal that receipt through Python 3.12's cross-thread trace
API. Normal imports/self-tests still pass. The suite also proves that a missing
managed isolation primitive raises and does not fall back to host execution.
