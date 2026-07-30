# Abandon death-loop and workflow_run_id reuse — 2026-07-30

**As-of:** 2026-07-30. **Status:** active blockers fixed (A, D); latent issues
documented (B, C). All web tests green (4273 passed, 4 skipped).

## Summary

The autonomous cloud runtime (`pok-evolution`, namespace `national_cloud_v`)
had been cycling generations v12–v17 without publishing past v11 since
2026-07-30 morning. Four distinct defects were root-caused through read-only
audit of the durable journals, abandon ledger, and event log:

- **Defect A (fixed)** — abandoned version numbers get re-selected and reuse a
  dead `workflow_run_id`.
- **Defect D (fixed)** — a refused forced-abandon at an initial worker stage
  loops forever (the live blocker: v17 spun at `master_planned`).
- **Defect B (latent, deferred)** — claim/ledger reason can diverge from the
  durable tombstone reason.
- **Defect C (latent, hardened)** — quality-repair synthesis dead-ends when the
  architecture transition object is absent but the flat capability contract
  already proves a `policy.py`-repairable failure.

The live blocker was **D**, not the earlier-suspected quality death-loop (that
case, v12/v13, was already fixed by `884e6465`). D's trigger is A.

---

## Defect A — re-selected version reuses a dead workflow_run_id

**Root cause.** `web/core/generation_scheduler.py` allocated the workflow
identity as `generation_workflow_id(next_v, attempt=...)`, but the `attempt`
suffix was bumped **only for `FIRST_STRICT_POLICY_VERSION`** (v1 bootstrap):
```python
_workflow_attempt = 1
if _planned_next_v == FIRST_STRICT_POLICY_VERSION and _abandoned_floor < FIRST_STRICT_POLICY_VERSION:
    _workflow_attempt = abandoned_version_attempt_count(FIRST_STRICT_POLICY_VERSION) + 1
```
Every other version was pinned to `workflow-v1`. Because the version selector
treats the abandon ledger as a **monotone high-water floor only**
(`next_v = max(published_high_water, abandoned_floor) + 1`), a re-selected
version number recomputes the identical `generation:N:workflow-v1` and reuses
the dead/terminal journal. The observed consequence was the v17 `master_planned`
loop (defect D) and an earlier v16 crossover `WorkflowConflict: workflow
instance is not running`.

**Fix.** Bump the attempt for **every** re-selected version:
```python
_workflow_attempt = abandoned_version_attempt_count(_planned_next_v) + 1
```
`abandoned_version_attempt_count` returns 0 for a never-abandoned version
(→ `workflow-v1`, byte-identical for v1/v11 and every normal first attempt) and
`K` for a version abandoned `K` times (→ `workflow-v{K+1}`, a fresh journal).
This is exactly the durable per-version retry the ledger's `workflow-vK` naming
already encodes (see `test_failed_reserved_v143_attempt_is_audited_but_does_not_burn_label`
in `test_epoch_authority.py`). Regression test:
`test_re_selected_abandoned_version_gets_fresh_workflow_attempt`
(`test_generation_scheduler.py`).

---

## Defect D — refused forced-abandon loops forever at initial worker stages

**Live blocker.** v17's durable Worker journal `generation:17:workflow-v1` was
terminal (abandoned at `rework_running` under reason
`frozen_rework_repo_baseline_head_mismatch...`). The outer checkpoint was then
re-created at `master_planned` (reusing the same dead run_id — defect A). Every
cycle: the deterministic route dispatched `execute_workers` by stage → the
worker replayed the terminal journal → returned the stored `frozen_rework_`
reason → the route force-abandoned → the `generic_abandon` state guard
**refused** (`frozen_rework_` is authorized only at rework stages, not
`master_planned`) → the checkpoint stayed at `master_planned` → next cycle
repeated. ~20s/iteration, no LLM/probe progress, unbounded.

**Fix (two parts), `web/core/orchestrator_deterministic_route.py`:**

1. **Translation.** When a terminal Worker journal is replayed at an **initial
   worker stage** (not a rework stage), translate the stored reason to the
   abstract `worker_terminal_abandon` classification, which the guard authorizes
   at `master_planned`/`workers_done`/`quality_failed`/... . The concrete journal
   reason stays in the Worker tombstone and the routed `result` payload for
   audit; only the control-plane guard transition uses the classification.
2. **Bounded fallback.** If a forced abandon is refused specifically with
   `forced_abandon_reason_stage_not_allowed`, retry exactly once with the
   always-allowed generic `abandon_generation` reason. This guarantees the
   router can never loop forever on a refused forced-abandon. A non-stage-guard
   refusal (e.g. `publication_or_certification_stage_not_disposable`) is NOT
   retried — those stages require genuine reconciliation.

Regression tests: `test_master_planned_terminal_journal_replay_translates_to_authorized_abandon`,
`test_repeated_forced_abandon_refusal_falls_through_to_generic_abandon`,
`test_non_stage_guard_refusal_is_not_retried` (`test_orchestrator_timeout_extension.py`).

---

## Defect B — claim/ledger reason vs durable tombstone reason (latent, deferred)

**Finding.** `_build_recorded_abandon_claim` builds the claim/ledger
`abandon_reason` from the control-plane `reason`, while the durable Worker and
strict-authority journals persist their own terminal reason. When the two
diverge (e.g. a crossover re-abandon where the control plane passes
`crossover_effect_prepare_conflict:v1xv11` but the tombstone persists
`worker_infrastructure_exhausted: ...`), completed-abandon reproof fails with
`completed_abandon_StrictAuthorityAbandoned_outer_reason_mismatch`.

**Why deferred.** The divergence is only reachable via a same-version
re-abandon, which defect A now prevents (re-selection gets a fresh
`workflow-v{K+1}` journal). The two already-finalized "bad" transactions on disk
(v15 `28c24ca1`, v16 `10d52b74`) are **left untouched** — manually editing CAS
state would break the audit chain, and they do not block the running pipeline
(normal generation bypasses them; they only fail on explicit reproof). An
initial fix attempt (claim/ledger reason := strict terminal reason) was reverted:
`abandon_authority` raises on an already-terminal strict instance, so the
authoritative reason is only available on first creation (where it equals the
control-plane reason) — the override would be dead code, and surfacing it
correctly requires deeper changes to the re-abandon path. Filed for a follow-up
once a same-version re-abandon is reproducible under controlled conditions.

---

## Defect C — quality-repair synthesis dead-end (latent, hardened)

**Finding.** `_architecture_contracts`
(`web/core/tool_planning_quality_repair_targets.py`) early-returns `[]` when
`national_architecture_transition` is absent/`ok=True`, even if the flat
`national_capability_contract` already proves a `policy.py`-repairable failure
(`check_id` mapped to `policy.py` in `_ARCHITECTURE_CHECK_FILES` **and**
`evidence.locations` corroborates `policy.py`). That yields
`system_repair_task_synthesis_empty` → terminal abandon instead of a rework
cycle. The active v12/v13 instance of this was already fixed by `884e6465`
(which admitted `typed_runtime_probe`/`precompute_runtime_influence` to the
map); the remaining gap is the absent-transition case.

**Hardening.** Added `_architecture_contracts_from_capability_only`: when the
transition is unusable, synthesize a `policy.py` repair contract from the flat
contract alone. **Fail-closed:** a failing check is repaired only if BOTH the
static map AND the flat contract's own `evidence.locations` corroborate
`policy.py`; a non-`policy.py` failure still returns `[]` (terminal abandon,
unchanged behavior). Regression tests in `test_architecture_rework_authority.py`.

---

## Verification

- Full web test suite: **4273 passed, 4 skipped, 0 failed**.
- Three pre-existing failures (unrelated to the fixes but surfaced by this run)
  were also root-caused and fixed:
  - `test_control_operator_auth.py::test_control_gets_never_persist_events_or_files`
    — RESULTS_DIR not isolated from the concurrently-running production daemon
    (added a `tmp_path` isolation).
  - `test_producer_consumer_workflow_store.py::test_production_entrypoints_do_not_import_inert_slice_modules`
    — `control.py` legitimately became a fourth sanctioned slice2b-activation
    seam in `61b97a40`; added it to `sanctioned_activation_sources` and applied
    the exclusion to the `web/server` glob.
  - `test_llm_availability_store.py::test_bare_429_is_manual_...` — the test
    encoded the pre-P0-2 contract; `61b97a40` intentionally replaced bare-429
    manual-resume with a conservative fallback-window auto-resume. Rewrote the
    test to assert the P0-2 contract (and renamed it).

- Live system: v17 was canonically abandoned (operator `/api/control/abandon`
  with a guard-authorized reason), the loop stopped, and v18 began preparing
  under `generation:18:workflow-v1`. The defect-A/D fixes are committed but not
  yet deployed to `.evolution_pok`; a `git pull --ff-only --tags` +
  `systemctl restart` will pick them up.
