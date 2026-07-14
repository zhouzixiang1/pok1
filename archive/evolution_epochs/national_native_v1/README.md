# Retired epoch: `national_native_v1`

Status: **retired / legacy-untrusted**

Archived: 2026-07-14

This directory preserves the 82 tracked `national_v<N>` bot trees that formerly
lived directly under `bots/`. They are history, not an active compatibility
layer.

Although these bots expose a `national_bot.py` TCP entry point, their candidate
side still imports the old `main.py` / reconstructed request-response state /
integer-action strategy stack. That mixed ABI is not the strict typed policy ABI
of the active `national_tcp_policy_v1` epoch. Legal-looking TCP output therefore
does not make these artifacts valid active bots.

## Hard trust boundary

Files below `bots/` in this archive must never be:

- discovered, imported, copied, branched from, crossed over, repaired, or
  executed by the active evolution system;
- selected as a source, parent, control, opponent, rating participant, Arena
  participant, precommit target, or official-certification target;
- used to seed ratings, H2H, selection scores, opponent statistics, range
  updates, capability claims, prompts, lessons, experience, replays, or quality
  gates;
- edited in place to emulate the new ABI.

Associated runtime products are equally untrusted unless an active-epoch schema
binds them to a new policy bot, exact runtime/parser hashes, replay identity, and
an immutable evaluation snapshot. Old evidence must be archived, never
field-upgraded or injected into prompts.

The former live experience pool is preserved at
`evidence/experience_pool.md`. Its strategy lessons and version references are
retired evidence and have no prompt, planning, opponent-model, or gate role in
`national_tcp_policy_v1`.

Its former maintenance code is preserved under `analysis/` as
`experience_pool.py`, `experience_archivist.py`, and
`experience_attribution.py`; the matching LLM templates are under `prompts/`,
and their regression suites are under `tests/`. The cross-generation keyword
gate and pivot JSONL consumer were removed from active planning code rather
than retained as compatibility shims. Historical `regression_guardian.jsonl`
files are likewise untrusted prose and have no active reader.

The unused free-form first-bot template is preserved as
`prompts/initial_prompt.md`; fresh v143 is materialized from the checked-in
deterministic system bootstrap assets instead. The old mutable parent-pair
denylist implementation is preserved as `analysis/crossover_compat.py`; its
`crossover_incompatibilities.json` cache was never immutable evaluation
evidence and has no selection or retry authority. The active
`crossover_compatibility.md` LLM audit is a separate advisory preparation step
and remains active.

The retired daemon-side bridge is preserved as
`analysis/battle_experience_identity_bridge.py`; its former Markdown merge
templates are `prompts/battle_experience_incremental.md` and
`prompts/battle_experience_update.md`. Its focused tests and the former Web
`/experience` page are archived under `tests/` and `frontend/`. The active
daemon starts no such thread, and the active API/UI exposes no experience
route.

The former arbitrary-path JSONL battle queue is preserved under `control/` as
`battle_scheduler.py` and `server_scheduler_route.py`, with its dedicated tests
under `tests/`. The active Web application registers no `/api/scheduler` route
or SSE event, and the active daemon has no external queue/capability mode: it
schedules only current-pool complete native TCP strength matches. Precommit
evaluation uses its identity-bound direct runner, while official EXE work stays
inside the separate durable certification job manager.

The retired role grants are preserved byte-for-byte at
`authorization/official_grandfathering.json`. They are audit evidence only and
must never be opened by active discovery or authorize any active role. The sole
active authorization contract is `web/core/official_role_policy.json`.

Historical `national-bot-v<N>` tags remain untouched and continue to describe
the commits they originally named. They do not confer active-epoch eligibility.
The new epoch may continue publication numbering at v143 solely to preserve the
global version/tag high-water; it does not copy or inherit v142 code, ratings,
H2H, experience, capability evidence, or certification.

## Inventory

The archived directory names are:

```text
national_v1 national_v2 national_v3 national_v4 national_v5 national_v6
national_v7 national_v8 national_v9 national_v10 national_v11 national_v12
national_v13 national_v14 national_v15 national_v16 national_v17 national_v18
national_v20 national_v27 national_v28 national_v29 national_v30 national_v31
national_v32 national_v33 national_v34 national_v36 national_v37 national_v39
national_v40 national_v42 national_v43 national_v45 national_v46 national_v47
national_v48 national_v49 national_v51 national_v52 national_v53 national_v54
national_v55 national_v57 national_v59 national_v61 national_v62 national_v63
national_v66 national_v67 national_v68 national_v69 national_v70 national_v71
national_v72 national_v73 national_v74 national_v76 national_v77 national_v83
national_v84 national_v85 national_v86 national_v87 national_v88 national_v98
national_v103 national_v110 national_v111 national_v112 national_v113
national_v114 national_v115 national_v117 national_v119 national_v120
national_v121 national_v122 national_v123 national_v135 national_v141
national_v142
```

Count: 82 directories. No archived directory contained `policy.py` at the move
boundary.
