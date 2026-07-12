# Operator generation cost policy

LLM token and USD usage is telemetry by default. The evolution system records
every completed SDK billing result per durable `workflow_run_id`, warns at
`$7`, and continues the same generation. The workflow id is allocated before
prepare-stage analysis and is then adopted by the pipeline checkpoint, so
Combined/Match analysis, Master, Workers, Reviewer, Critic, Orchestrator, and
Archivist work cannot split across session boundaries. Checkpoint hand-offs,
Claude session replacement, and process restart do not reset the durable total.

An operator may opt into a hard stop before starting the process:

```bash
export POK_OPERATOR_MAX_GENERATION_COST_USD=25
```

The value must be finite and greater than zero. `0`, `off`, `none`,
`unlimited`, `disabled`, an empty value, or an unset variable selects the
default monitor-only policy. Other invalid values reject orchestrator startup.

The explicit value is a post-call circuit breaker, not a prepaid billing
reservation. The system checks before every new call and again after every SDK
billing result; reaching the value closes the disposable LLM session,
preserves the pipeline checkpoint, writes a policy-bound runtime receipt, and
stops. Calls that were already running in parallel can finish and make the
final total exceed the configured value. It does not classify the stop as SDK
infrastructure failure or automatically retry. The operator must
change/disable the parent-process setting and explicitly restart.

Each SDK result uses its SDK UUID (with a deterministic compatibility fallback)
as an idempotency key. Empty-output/signature retries are billed individually,
not discarded when a later attempt succeeds. A small atomic write-ahead pending
file is persisted before the append ledger. If the append fails or the process
dies between those writes, the pending amount remains visible after restart:
monitor-only mode reports the accounting fault and continues, while explicit
hard-limit mode fails closed until an operator repairs the accounting state.
An SDK result with token usage but no USD amount is retained as unknown-cost
evidence with the same monitor-versus-hard semantics; it is never silently
treated as free.

The cost setting is read only from the orchestrator parent process. It is not a
prompt field, MCP argument, candidate artifact, or checkpoint field. The
candidate sandbox cannot mutate it. The main-agent workflow guard denies direct
Bash/Edit/Write mutations of the ledger, write-ahead state, policy
implementation, LLM accounting primitive, scheduler, orchestrator, and
launcher.

That workflow guard is defense in depth, not an operating-system security
boundary: the main Orchestrator and its host-side tools run under the same Unix
uid as the evolution service, so the receipt explicitly reports
`same_uid_llm_resistance=false`. A sufficiently obfuscated same-uid write is not
claimed impossible. HEAD-drift gates, exact-file evaluation contracts, and the
operator-owned checkout/restart boundary remain required. The policy
implementation itself is generation-critical and part of active-checkpoint
HEAD-drift evaluation.
