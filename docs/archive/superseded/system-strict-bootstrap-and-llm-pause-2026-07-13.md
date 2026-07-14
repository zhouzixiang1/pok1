# Deterministic First-Strict Bootstrap And LLM Availability Pause

Date: 2026-07-13

## Decision

The first publishable strict national-native bot is a controlled architecture
migration, not an ordinary LLM strategy generation.  When and only when the
strict publication pool is empty, the scheduler-selected source is the pinned
`national_v142` migration source, and every preparation receipt still matches,
the system installs one checked-in four-file consumer blueprint through the
existing durable Worker workspace.

This path removes model discretion from the Worker bytes of a
correctness-floor migration. It does not remove model governance: Master must
still obtain three independent proposals and two anonymous criterion ballots,
select exactly one proposal, and produce a schema-valid plan. Review and
Critic must each complete a real schema-valid LLM call. The resulting eight
accepted roles (three proposals, two ballots, final Master, Review, and Critic)
are recorded in a separate fenced provider-authority stream; an I/O log or a
checkpoint boolean is never execution evidence. After materialization
the candidate still passes the normal capability/decision/native quality
gates, the local 70-hand precommit contract, and formal signed Windows-EXE
certification before commit and `national-bot-vN` publication.

Separately, a billing/authentication availability failure is durable control
state rather than a strategy failure.  The first specific SDK evidence is
preserved across streamed text, result messages, and trailing generic
exceptions; the orchestrator persists a pause across process restarts.  A
paused LLM activity does not consume a semantic or Worker retry.  Deterministic
checkpoint recovery may continue, but a true LLM stage stays parked until the
operator supplies the exact resume acknowledgement required by the pause
record.

## Content-Bound Bootstrap Authority

The controller is intentionally narrow and fail-closed.  Eligibility binds all
of the following:

- empty strict published pool and the scheduler's protocol-migration selection;
- source version, artifact hash, annotated tag object, and completion tree for
  `national_v142`;
- the frozen prepared-candidate artifact and installed system runtime hashes;
- the universal legacy-consumer migration bundle and its four required
  counterfactual checks;
- the manifest plus exact bytes of `strategy.py`, `opponent.py`,
  `simulation.py`, and `donk_probe.py`;
- the `official-full-v5` policy identifier and the complete two-document
  official-oracle digest map.

The Master plan is not system-built. Its complete three-proposal/two-ballot
packet, selected proposal, structural change, expected diff, reachable chain,
and falsifier are frozen into the checkpoint and Worker envelope. The selected
falsifier must name one of the blueprint's real capability probes, and the
controller independently recomputes the proposal and contract identities.
Worker application still uses a fenced lease-epoch workspace, immutable
input/output artifact identities, and atomic projection. Review and Critic
carry `llm_invoked: true`, role-executed, and schema-valid markers; the system
receipt is only an adjunct content-chain proof and can never replace either
role.

Each strict LLM dispatch is requested and leased before the SDK call. A real
SDK `ResultMessage`, observed in the parent stream processor, completes that
effect; deterministic schema acceptance is a separate `StrictRoleAccepted`
event. The provider prompt, runtime role, model, tool set, output, parsed role
payload, and their digests are bound together. Validation requires exactly one
accepted event for every one of the eight slots, distinct provider, invocation,
and effect identities, and the prescribed stage/revision ordering. Master,
Review, and Critic receive read-only tools; the main orchestrator has only the
typed evolution MCP server and no built-in shell, Python, Read, or write tools.

Accepted role payloads and provider output are retained in the ignored
WorkflowStore authority stream so a crash after acceptance but before
checkpoint projection replays the same call instead of invoking the provider
again or creating a second accepted slot. Replay revalidates the original
effect, prompt/output digests, generation, stage, revision, and semantic
context. It does not claim that a newly rendered retry prompt was the prompt
that produced the already accepted result.

Any authority, proposal binding, manifest, source, prepared artifact, output
artifact, or receipt drift abandons the controlled migration. It never falls
back to an LLM trying to approximate the blueprint. If quality, Review,
Critic, or precommit rejects the fixed bytes, the generation is abandoned; the
blueprint/control plane must be changed and tested in a fresh generation rather
than patched by an in-generation Worker.

## Evaluation Contract

Evaluation contract version 18 makes the following exact files
restart-critical at every checkpoint stage:

- `web/core/system_strict_bootstrap.py`;
- `web/core/bootstrap_assets/strict_v1/manifest.json` and its four declared
  Python assets;
- `web/core/llm_availability.py`;
- `web/core/llm_availability_store.py`;
- `web/core/strict_authority_workflow.py`.

The system-owned first-strict control is also exact-scoped at every stage:

- `web/core/first_strict_control.py`;
- `web/core/first_strict_execution_journal.py`;
- `web/core/bootstrap_assets/first_strict_control_v1/manifest.json` and its
  three declared Python assets.

Every first-strict control sample is one complete local native TCP match of
exactly 70 hands. Before launch, the parent leases a repeat-specific effect
bound to the workflow/revision, candidate and control artifacts, immutable
precommit plan and evaluation contract, attempt, and frozen deck/bot seeds.
The native runner binds the resolved candidate/control identities and its
engine-produced terminal replay before the journal can complete the effect.
The runner commits the full events, hand records, and settlements inside the
same fenced SQLite effect/event transaction before returning them to the outer
precommit layer. A read-only content-addressed replay file is then projected
from those committed bytes; if the process dies between the database commit
and file projection, recovery recreates the exact file without rerunning the
match. The one-shot in-memory runner seal is validated before the transaction
and consumed only after it commits. The precommit result carries only a small
receipt reference. Gate validation and publication reread the WorkflowStore
event and replay and reject missing, duplicated, reordered, shortened, or
digest-drifted samples.

The eight control samples are regression-only. They may open the one-time
first-strict precommit route, but always carry zero rating, H2H, source-choice,
official-opponent, and strength authority. A crash after a completed sample
recovers the validated receipt/replay and resumes at the next missing repeat;
it never reruns or double-counts the completed match.

Crossover preparation now has two separate durable boundaries.  The synthesis
effect freezes the full rendered prompt, both immutable parent artifacts, the
complete entry checkpoint, semantic attempt, exact compatibility guidance,
and Parent-A-derived input snapshot before an LLM call can start.  Its
lease-epoch fence accepts only one content-addressed model output; after a
crash, a completed effect is materialized into a fresh private workspace and
all deterministic gates run again without another model call.  The existing
projection journal then separately reconciles that gated workspace with the
canonical candidate and checkpoint CAS.  `web/core/crossover_synthesis.py`
and `web/core/crossover_projection.py` are therefore exact prepare/generation
contract inputs.

The main-Orchestrator root guard is likewise always restart-critical:

- `web/core/orchestrator_context.py`.

The main orchestrator is structurally configured with `tools=[]` while
retaining only the typed `evolution` MCP server. Its `PreToolUse` guard still
rejects `bootstrap-full`, the one-time acknowledgement flag, the pinned
signed-ledger root, and direct `official_bootstrap` API access as defense in
depth. Only an external operator shell can perform the documented recovery
command.

The exact scope matters in both directions.  Blueprint drift must restart an
in-flight generation because it can change candidate bytes and verifier
receipts.  A broad `bootstrap_assets/` prefix is deliberately absent so a
future unrelated experiment cannot interrupt a run.  The mutable pause record
is written under `web/core/results/`; it is runtime state and therefore is not
part of the Git/evaluation content hash.

The two official oracle documents remain always-critical and byte-pinned:

- `docs/official-raise-boundary-oracle-2026-07-11.md` —
  `a83a1ec2680577d71ddb985ddba00c5bcda40817ef2fb92c0c41938dccef3756`;
- `docs/official-terminal-settlement-oracle-2026-07-11.md` —
  `ad96bc4fbe7939597b7a86ff6f9193ed2e50891be9b6b9c074883f5750c23bd9`.

The manifest must declare exactly that complete map.  Omitting an oracle is a
package failure; changing either document is an architecture-policy failure.
No control-plane change authorizes rewriting those documents or substituting a
local platform observation.

## Official EXE Boundary

The official EXE is still a compliance oracle with zero strength weight.  A new
bot requires the signed full-v5 policy: five 70-hand self-play rounds and three
70-hand rounds against an eligible opponent.  The normal automated path cannot
consume the one-time signed-ledger bootstrap root.  If the first strict bot
reaches `official_bootstrap_required`, only the explicit operator recovery
command and its acknowledgement may select that root.

Local Arena, local TCP results, deterministic system receipts, or an LLM review
cannot certify or grandfather the candidate.  Likewise, official EXE chip
outcomes cannot enter Glicko, H2H, or source selection.

## Restart And Resume Semantics

On restart the operator should first verify the dual-checkout state, active
checkpoint, exact-file evaluation contract, and official/Arena leases. The
deterministic recovery route may replay only already-bound no-LLM work (the
quarantined Direction receipt and initial system Worker). Master, Review, and
Critic always remain LLM stages. If the next activity genuinely requires an
LLM and a durable pause is active, the process remains parked; repeated
15-second retries and generation abandonment are both incorrect.

Clearing a billing or authentication pause is an operator assertion about
changed external availability, not a timer. The acknowledgement is bound to
the pause evidence digest so a stale command cannot clear a newer failure.
Only transient rate-limit/service/transport categories use a bounded
system-owned cooldown; they still consume no generation or Worker attempt.

The provider and native journals provide crash fencing and protect the normal
LLM/checkpoint execution path; they are not a cryptographic boundary against
an arbitrary operator-owned process with the same Unix UID. Filesystem and
process ownership remain that outer boundary. In particular, these receipts
must not be described as proof against an operator who can rewrite the SQLite
database, replay store, or running process memory.

## Non-Goals

- This path does not make every future generation deterministic. Only the
  first migration's Worker bytes are deterministic; proposal, Master, Review,
  and Critic use the configured LLM even for that first generation.
- The four-file blueprint is a correctness floor, not a claim of solved HUNL
  strategy or a substitute for local strength evaluation.
- The bounded import-time lookup facts in the blueprint do not authorize large
  candidate-owned tables, import-time external I/O, or bypassing the existing
  system-owned asset ABI requirements.
- Durable availability pause does not broaden operator authority, alter the
  candidate, weaken retry fencing, or relax official certification.
