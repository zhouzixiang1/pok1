"""Exit-path contract snapshot for ``_execute_workers_command``.

This module captures the COMPLETE enumeration of every ``return`` statement in
``_execute_workers_command`` (76 returns, excluding the 3 returns inside the
nested ``rollback_rework_preparation`` helper).

HISTORY
-------
Originally captured at ``web/core/tool_planning_worker_durable.py:1485-3843``
before the wave-6 Group-F extraction. The verbatim code-move (commit moving
the body to ``tool_planning_worker_phases``) preserved every exit by
construction; the fixture's invariants (76 exits, 64 distinct reasons, 10
abandon reason codes, 8 abandon-spread exits) are unchanged.

Wave-7 phase decomposition: the 76-return monolithic body was split into four
module-level phase sub-functions (``PHASE_FUNCTIONS`` below), orchestrated by a
thin ``_execute_workers_command`` wrapper. Every return is preserved VERBATIM
inside the phase that owns it (no syntax changes to early returns); each phase
falls through to the next by returning a 1-tuple ``(ctx_updates,)`` (the
"continuation trailer"). The orchestrator's 4 ``return result`` statements are
propagation returns (forwarding a phase exit), not new exits. The self-check
therefore walks the orchestrator + the four phases as one call graph and
excludes continuation trailers, propagation returns, and the nested
``rollback_rework_preparation`` closure (which lives inside Phase C). The
aggregate invariants (76 / 64 / 8) are unchanged.

The per-exit ``line`` fields below retain their pre-move ``durable.py`` line
numbers as the canonical historical reference; the ``EXIT_PATHS`` table is
consumed by ``test_worker_exit_path_contract`` for its semantic (return-type +
identity) assertions, not for line-number equality.

PURPOSE
-------
A genuine per-phase decomposition is a deferrable follow-up wave. Until then,
this snapshot is the golden contract: a comparison test
(``test_worker_exit_path_contract`` in ``test_worker_exit_path_contract.py``)
re-derives the exit set from the live function (now in ``tool_planning_worker_phases``)
and asserts it is identical to ``EXPECTED_WORKER_EXIT_REASONS``.

WHAT THIS FILE IS / IS NOT
--------------------------
- IS:  read-only data + a structural self-check. It does NOT import or execute
        the production function. Coupling the fixture to runtime behavior would
        defeat its purpose as a golden snapshot (a runtime import would make
        the "before" and "after" measurements share code paths).
- IS:  a hand-curated, AST-derived enumeration. Every entry was produced by
        parsing the function's AST and then enriched by reading source for the
        dynamic (``IfExp`` / f-string) error expressions.
- IS NOT: the comparison test itself. The comparison test will be authored as
        part of the refactor so it can import the post-refactor function and
        diff against this fixture.

ENUMERATION METHODOLOGY
-----------------------
1. ``ast.parse`` the source, locate ``_execute_workers_command``.
2. Walk all ``ast.Return`` nodes inside it, EXCLUDING returns inside the one
   nested helper ``rollback_rework_preparation`` (lines 2795-2804), whose
   ``return ""`` / ``return f"..."`` exits are internal to rollback and never
   reach the function's caller.
3. For each return, classify by callee:
   - ``json_tool_result``     -> ``_tw._json_tool_result({...})``
   - ``state_blocked``        -> ``_tw._state_blocked(...)``
   - ``project_..._output``   -> ``await _project_durable_worker_output(...)``
   - ``project_..._failure``  -> ``await _project_durable_worker_failure(...)``
   - ``deferred_activity``    -> ``_DeferredWorkerActivity(...)``
   - ``run_durable_effect``   -> ``await _tw._run_durable_worker_effect(...)``
   - ``var_return``           -> bare ``return recovery`` (delegated envelope)
4. For ``json_tool_result`` exits the identity is the ``error`` field value.
   Five exits use ternary ``IfExp`` error expressions (rollback cascades) and
   are recorded as the unordered pair of their two possible codes.
5. ``EXPECTED_WORKER_EXIT_COUNT`` == 76 (total returns in the main function
   body, excluding the nested helper). This is the load-bearing invariant: a
   refactor that adds or removes even one exit path changes the contract.

NOTE ON ACCURACY
----------------
If ``EXPECTED_WORKER_EXIT_COUNT`` no longer matches the live ``return`` count
in the source, the self-check at the bottom (``__main__``) will fail loudly.
Update BOTH the count and the ``EXIT_PATHS`` table together whenever the
production function changes -- but ONLY up to the moment the refactor lands,
at which point this file becomes the frozen "before" snapshot.
"""

from __future__ import annotations

from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Aggregate counts (the load-bearing invariants).
# ---------------------------------------------------------------------------

#: Total number of ``return`` statements in the body of ``_execute_workers_command``,
#: EXCLUDING the nested ``rollback_rework_preparation`` helper (3 returns) which
#: are internal to rollback and never propagate to the caller.
#:
#: Breakdown by return-expression type:
#:   json_tool_result              : 65   (``_tw._json_tool_result({...})``)
#:   state_blocked                 :  2   (``_tw._state_blocked(...)``)
#:   project_durable_worker_output :  2   (``await _project_durable_worker_output(...)``)
#:   project_durable_worker_failure:  2   (``await _project_durable_worker_failure(...)``)
#:   deferred_activity             :  2   (``_DeferredWorkerActivity(...)``)
#:   run_durable_effect            :  2   (``await _tw._run_durable_worker_effect(...)``)
#:   var_return                    :  1   (``return recovery`` -- delegated envelope)
EXPECTED_WORKER_EXIT_COUNT = 76

#: Distinct (return_type, identity) pairs the function can produce. This is the
#: semantic contract: after the refactor the post-refactor function MUST be
#: able to emit exactly this set of identities for the same inputs. Identities
#: are the ``error`` field for json_tool_result exits, ``<var:critic_refusal>``
#: for the delegated critic-refusal returns, and the return-expression category
#: for the structural (non-json) exits.
EXPECTED_WORKER_EXIT_REASONS = {
    # --- json_tool_result error codes (static) ---
    "WORKER_TASKS_NOT_LIST",
    "Missing next_v/source_v and no active checkpoint",
    "SYSTEM_STRICT_BOOTSTRAP_REWORK_FORBIDDEN",
    "SYSTEM_STRICT_BOOTSTRAP_WORKER_AUTHORITY_INVALID",
    "STALE_WORKFLOW_ID_UNSUPPORTED",
    "STALE_WORKER_INFRASTRUCTURE_STATE_UNSUPPORTED",
    "WORKER_WORKFLOW_ABANDONED",
    "DURABLE_WORKER_ENVELOPE_INVALID",
    "DURABLE_WORKER_IDENTITY_MISMATCH",
    "DURABLE_WORKER_DEFINITION_DRIFT",
    "LLM_AVAILABILITY_STATE_INVALID",
    "LLM_AVAILABILITY_BLOCKED",
    "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
    "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID",
    "WORKER_AVAILABILITY_RESUME_FAILED",
    "WORKER_AVAILABILITY_RESUME_INVARIANT_FAILED",
    "WORKER_INFRASTRUCTURE_EXHAUSTED",
    "WORKER_CYCLE_HAS_NO_PENDING_COMMAND",
    "FIRST_STRICT_ARCHITECTURE_POLICY_IDENTITY_DRIFT",
    "ARCHITECTURE_POLICY_IDENTITY_REPLAN_EXHAUSTED",
    "ARCHITECTURE_POLICY_IDENTITY_RECOVERY_FAILED",
    "PREPARED_ARTIFACT_DRIFT_BEFORE_WORKERS",
    "DURABLE_REPAIR_PREPARATION_RECEIPT_MISSING",
    "REPAIR_BASELINE_RECEIPT_MISSING",
    "REPAIR_BASELINE_ARTIFACT_DRIFT",
    "REWORK_FEEDBACK_AUTHORITY_MISSING",
    "REWORK_FEEDBACK_AUTHORITY_MISMATCH",
    "REWORK_TASK_AUTHORITY_INVALID",
    "REWORK_TASK_AUTHORITY_MISMATCH",
    "DECLARED_SCOPE_INTEGRITY_VIOLATION",
    "execute_workers requires a master plan. Call run_master first to produce a task plan.",
    "WORKER_INITIAL_FEEDBACK_FORBIDDEN",
    "WORKER_TASK_AUTHORITY_INVALID",
    "WORKER_TASK_PLAN_MISMATCH",
    "DURABLE_INITIAL_WORKER_TASK_DRIFT",
    "No tasks provided and checkpoint has no task plan. Call run_master first.",
    "WORKER_TASK_WRITE_SCOPE_INVALID",
    "Precommit failed, but execute_workers was called without reviewer_feedback. Pass the exact precommit_eval directive/blockers as reviewer_feedback.",
    "DURABLE_REPAIR_PREPARATION_UNAVAILABLE",
    "PRECOMMIT_REWORK_CIRCUIT_BREAKER",
    "OFFICIAL_REWORK_CIRCUIT_BREAKER",
    "REWORK_PREPARATION_SNAPSHOT_FAILED",
    "REPAIR_PREPARATION_SNAPSHOT_MISMATCH",
    "REWORK_PROJECTION_CHECKPOINT_MISSING",
    "REWORK_RUNNING_CHECKPOINT_FAILED",
    "DURABLE_WORKER_FROZEN_INPUT_DRIFT",
    "DURABLE_WORKER_CHECKPOINT_MISSING_BEFORE_PREPARE",
    "DURABLE_WORKER_PREPARED_SNAPSHOT_MISMATCH",
    "DURABLE_WORKER_PROJECTION_PREIMAGE_UNAVAILABLE",
    "DURABLE_WORKER_COMMAND_DISPATCH_INVARIANT",
    # --- json_tool_result info-only / dynamic (f-string) error identities ---
    "INFO:redundant_call_blocked",          # L2754: workers already ran
    "CIRCUIT BREAKER (worker_failure_count)",  # L2775: per-generation failure cap
    # --- json_tool_result IfExp (two possible error codes each) ---
    # Each of the five rollback-cascade exits can return one of two codes
    # depending on whether ``rollback_rework_preparation()`` itself errored.
    "REWORK_PREPARATION_ROLLBACK_FAILED | REWORK_SOURCE_RESET_FAILED",          # L3147
    "REWORK_PREPARATION_ROLLBACK_FAILED | CANDIDATE_HYGIENE_FAILED",            # L3232
    "REWORK_PREPARATION_ROLLBACK_FAILED | REWORK_MECHANICAL_TRIM_FAILED",       # L3301
    "REWORK_PREPARATION_ROLLBACK_FAILED | REPAIR_BASELINE_ARTIFACT_UNAVAILABLE",  # L3370
    "REWORK_PREPARATION_ROLLBACK_FAILED | REPAIR_BASELINE_CHECKPOINT_FAILED",    # L3460
    # --- delegated / variable json_tool_result payloads ---
    "<var:critic_refusal>",                  # L1525, L2701: _tw._json_tool_result(critic_refusal)
    # --- non-json structural exit categories ---
    "state_blocked",                         # L1510, L1595
    "project_durable_worker_output",         # L1881, L1887
    "project_durable_worker_failure",        # L1894, L1899
    "deferred_activity",                     # L1905, L3825
    "run_durable_effect",                    # L1911, L3831
    "var_return:recovery",                   # L2025: return recovery (delegated envelope)
}


# ---------------------------------------------------------------------------
# Highest-risk exits: the abandon paths.
# ---------------------------------------------------------------------------
#
# These are the exits that mutate durable generation state by calling a
# ``_force_abandon_*`` helper before returning, spreading its result into the
# json_tool_result via ``**abandon_result``. They are the highest-risk paths
# for the refactor because (a) they have irreversible side effects, (b) the
# abandon reason code must be preserved exactly so the downstream router /
# forced_rules layer keeps dispatching correctly, and (c) several are branched
# on a frozen-vs-non-frozen rework flag that the refactor must not collapse.
#
# Each entry maps the json_tool_result return line to the abandon helper reason
# code(s) that can flow into ``**abandon_result`` at that exit.

ABANDON_PATHS: Dict[int, Dict] = {
    2154: {
        "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
        "next_tool": "abandon_generation",
        "reasons": [
            "frozen_rework_baseline_drift",
            "worker_terminal_abandon_repair_baseline_drift",
        ],
        "branch": "frozen_rework_resume ? frozen_rework_baseline_drift : worker_terminal_abandon_repair_baseline_drift",
        "trigger": "not current_repair_baseline or current_repair_baseline != expected_repair_baseline",
    },
    2181: {
        "error": "REWORK_FEEDBACK_AUTHORITY_MISSING",
        "next_tool": "abandon_generation",
        "reasons": ["worker_terminal_abandon_rework_feedback_missing"],
        "trigger": "not canonical_feedback (no canonical repair feedback in checkpoint/gate)",
    },
    2219: {
        "error": "REWORK_FEEDBACK_AUTHORITY_MISMATCH",
        "next_tool": "abandon_generation",
        "reasons": ["worker_terminal_abandon_rework_feedback_mismatch"],
        "trigger": "reviewer_feedback and not _transport_equivalent_feedback(...)",
    },
    2277: {
        "error": "REWORK_TASK_AUTHORITY_INVALID",
        "next_tool": "abandon_generation",
        "reasons": [
            "frozen_rework_task_authority_invalid",
            "worker_terminal_abandon_rework_task_authority_invalid",
        ],
        "branch": "frozen_rework_resume ? frozen_rework_task_authority_invalid : worker_terminal_abandon_rework_task_authority_invalid",
        "trigger": "authority_errors (cannot derive signed file-scoped repair tasks)",
    },
    2455: {
        "error": "DURABLE_INITIAL_WORKER_TASK_DRIFT",
        "next_tool": "<spread via abandon_result>",  # no explicit next_tool; action=abandon_generation
        "reasons": ["durable_initial_worker_task_drift"],
        "trigger": "durable_tasks digest != authoritative initial tasks digest",
    },
    3074: {
        "error": "OFFICIAL_REWORK_CIRCUIT_BREAKER",
        "next_tool": "<none> (abandon_result carries abandoned flag)",
        "reasons": ["<official_rework_generation_abandoned>"],
        "helper": "_force_abandon_official_rework_generation",
        "trigger": "official_rework_count_for_write > MAX_OFFICIAL_REWORK_ROUNDS",
    },
    3579: {
        "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
        "next_tool": "abandon_generation",
        "reasons": ["frozen_rework_pre_worker_drift"],
        "trigger": "current rework artifact hash != frozen expected hash before worker dispatch",
    },
    3612: {
        "error": "DURABLE_WORKER_FROZEN_INPUT_DRIFT",
        "next_tool": "<spread via abandon_result>",
        "reasons": ["durable_worker_frozen_input_drift"],
        "trigger": "task_digest != durable_input_digest",
    },
}

#: Distinct abandon reason codes that can flow through ``**abandon_result``.
#: Preserving this exact set is the strongest invariant for the refactor,
#: because ``pipeline_state.forced_rules`` gates routing on the
#: ``worker_terminal_abandon_*`` / ``frozen_rework_*`` prefixes.
EXPECTED_ABANDON_REASONS = {
    "frozen_rework_baseline_drift",
    "worker_terminal_abandon_repair_baseline_drift",
    "worker_terminal_abandon_rework_feedback_missing",
    "worker_terminal_abandon_rework_feedback_mismatch",
    "frozen_rework_task_authority_invalid",
    "worker_terminal_abandon_rework_task_authority_invalid",
    "durable_initial_worker_task_drift",
    "frozen_rework_pre_worker_drift",
    "durable_worker_frozen_input_drift",
    "<official_rework_generation_abandoned>",  # _force_abandon_official_rework_generation (no reason kwarg)
}


# ---------------------------------------------------------------------------
# Full per-exit table.
# ---------------------------------------------------------------------------
#
# One row per ``return`` in source order. Fields:
#   line     -- source line number of the ``return``
#   type     -- return-expression category (one of the keys in RETURN_TYPE_COUNTS)
#   identity -- canonical externally-observable identity (error code for
#               json_tool_result; the category name for structural exits)
#   next_tool-- value of the ``next_tool`` dict key, or "" if absent. Dynamic
#               values are recorded as the producing expression in angle brackets.
#   action   -- value of the ``action`` dict key, or "" if absent.
#   trigger  -- one-line summary of the governing if/elif/except condition.

EXIT_PATHS: List[Dict] = [
    {"line": 1489, "type": "json_tool_result", "identity": "WORKER_TASKS_NOT_LIST", "next_tool": "", "action": "", "trigger": "not isinstance(tasks, list)"},
    {"line": 1499, "type": "json_tool_result", "identity": "Missing next_v/source_v and no active checkpoint", "next_tool": "", "action": "", "trigger": "next_v is None or source_v is None (after _resolve_version_args)"},
    {"line": 1510, "type": "state_blocked", "identity": "state_blocked", "next_tool": "", "action": "", "trigger": "not ckpt (no matching checkpoint)"},
    {"line": 1525, "type": "json_tool_result", "identity": "<var:critic_refusal>", "next_tool": "", "action": "", "trigger": "critic_refusal (from _critic_advisory_rework_refusal)"},
    {"line": 1534, "type": "json_tool_result", "identity": "SYSTEM_STRICT_BOOTSTRAP_REWORK_FORBIDDEN", "next_tool": "", "action": "abandon_generation", "trigger": "_declared_system_bootstrap and not _system_initial_worker_stage"},
    {"line": 1559, "type": "json_tool_result", "identity": "SYSTEM_STRICT_BOOTSTRAP_WORKER_AUTHORITY_INVALID", "next_tool": "", "action": "abandon_generation", "trigger": "_system_worker_errors (validate_master_receipt)"},
    {"line": 1577, "type": "json_tool_result", "identity": "STALE_WORKFLOW_ID_UNSUPPORTED", "next_tool": "", "action": "abandon_generation", "trigger": "missing workflow_run_id or checkpoint_revision < 1"},
    {"line": 1595, "type": "state_blocked", "identity": "state_blocked", "next_tool": "", "action": "", "trigger": "_worker_infra_error (owned infrastructure failure)"},
    {"line": 1609, "type": "json_tool_result", "identity": "STALE_WORKER_INFRASTRUCTURE_STATE_UNSUPPORTED", "next_tool": "", "action": "abandon_generation", "trigger": "_worker_infra is not None (stale infra)"},
    {"line": 1670, "type": "json_tool_result", "identity": "WORKER_WORKFLOW_ABANDONED", "next_tool": "", "action": "abandon_generation", "trigger": "command_name == 'reconcile_abandon'"},
    {"line": 1692, "type": "json_tool_result", "identity": "DURABLE_WORKER_ENVELOPE_INVALID", "next_tool": "", "action": "abandon_generation", "trigger": "envelope_errors (reconcile_abandon path)"},
    {"line": 1704, "type": "json_tool_result", "identity": "DURABLE_WORKER_IDENTITY_MISMATCH", "next_tool": "", "action": "abandon_generation", "trigger": "workflow_run_id / source_v mismatch (reconcile_abandon)"},
    {"line": 1723, "type": "json_tool_result", "identity": "DURABLE_WORKER_DEFINITION_DRIFT", "next_tool": "", "action": "abandon_generation", "trigger": "worker_template hash drift (reconcile_abandon)"},
    {"line": 1749, "type": "json_tool_result", "identity": "LLM_AVAILABILITY_STATE_INVALID", "next_tool": "", "action": "operator_reconcile", "trigger": "except Exception as exc (availability state inspection)"},
    {"line": 1765, "type": "json_tool_result", "identity": "LLM_AVAILABILITY_BLOCKED", "next_tool": "", "action": "wait_for_llm_availability", "trigger": "_active_pause is not None (request_or_claim_worker)"},
    {"line": 1789, "type": "json_tool_result", "identity": "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED", "next_tool": "", "action": "operator_reconcile", "trigger": "_deferred_availability.get('persistence_error')"},
    {"line": 1813, "type": "json_tool_result", "identity": "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID", "next_tool": "", "action": "operator_reconcile", "trigger": "_resume_receipt_errors"},
    {"line": 1847, "type": "json_tool_result", "identity": "WORKER_AVAILABILITY_RESUME_FAILED", "next_tool": "", "action": "operator_reconcile", "trigger": "except Exception as exc (resume)"},
    {"line": 1869, "type": "json_tool_result", "identity": "WORKER_AVAILABILITY_RESUME_INVARIANT_FAILED", "next_tool": "", "action": "operator_reconcile", "trigger": "command_name != 'claim_worker' after resume"},
    {"line": 1881, "type": "project_durable_worker_output", "identity": "project_durable_worker_output", "next_tool": "", "action": "", "trigger": "actor_lock_owned (claim_worker success, lock held)"},
    {"line": 1887, "type": "project_durable_worker_output", "identity": "project_durable_worker_output", "next_tool": "", "action": "", "trigger": "with command_lock(...) (claim_worker success, acquiring lock)"},
    {"line": 1894, "type": "project_durable_worker_failure", "identity": "project_durable_worker_failure", "next_tool": "", "action": "", "trigger": "actor_lock_owned (claim_worker failure, lock held)"},
    {"line": 1899, "type": "project_durable_worker_failure", "identity": "project_durable_worker_failure", "next_tool": "", "action": "", "trigger": "with command_lock(...) (claim_worker failure, acquiring lock)"},
    {"line": 1905, "type": "deferred_activity", "identity": "deferred_activity", "next_tool": "", "action": "", "trigger": "actor_lock_owned (deferred worker activity, lock held)"},
    {"line": 1911, "type": "run_durable_effect", "identity": "run_durable_effect", "next_tool": "", "action": "", "trigger": "fall-through (claim_worker dispatch, acquiring lock)"},
    {"line": 1919, "type": "json_tool_result", "identity": "WORKER_INFRASTRUCTURE_EXHAUSTED", "next_tool": "", "action": "abandon_generation", "trigger": "command_name == 'abandon'"},
    {"line": 1927, "type": "json_tool_result", "identity": "WORKER_CYCLE_HAS_NO_PENDING_COMMAND", "next_tool": "<route_policy(ckpt).get('next_tool')>", "action": "", "trigger": "command_name == 'none'"},
    {"line": 1937, "type": "json_tool_result", "identity": "FIRST_STRICT_ARCHITECTURE_POLICY_IDENTITY_DRIFT", "next_tool": "", "action": "abandon_generation", "trigger": "architecture_policy_identity_errors AND _is_fresh_empty_pool_bootstrap"},
    {"line": 1972, "type": "json_tool_result", "identity": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_EXHAUSTED", "next_tool": "", "action": "abandon_generation", "trigger": "identity_consecutive >= IDENTITY_REPLAN_ABANDON_THRESHOLD"},
    {"line": 2017, "type": "json_tool_result", "identity": "ARCHITECTURE_POLICY_IDENTITY_RECOVERY_FAILED", "next_tool": "", "action": "", "trigger": "except Exception as exc (identity recovery)"},
    {"line": 2025, "type": "var_return", "identity": "var_return:recovery", "next_tool": "", "action": "", "trigger": "recovery is not None (identity recovery succeeded)"},
    {"line": 2040, "type": "json_tool_result", "identity": "PREPARED_ARTIFACT_DRIFT_BEFORE_WORKERS", "next_tool": "abandon_generation", "action": "", "trigger": "prepared_artifact_errors"},
    {"line": 2088, "type": "json_tool_result", "identity": "DURABLE_REPAIR_PREPARATION_RECEIPT_MISSING", "next_tool": "", "action": "abandon_generation", "trigger": "durable_preparation_resume AND missing prepared receipt contract"},
    {"line": 2120, "type": "json_tool_result", "identity": "REPAIR_BASELINE_RECEIPT_MISSING", "next_tool": "abandon_generation", "action": "", "trigger": "not expected_repair_baseline (rework stage)"},
    {"line": 2154, "type": "json_tool_result", "identity": "REPAIR_BASELINE_ARTIFACT_DRIFT", "next_tool": "abandon_generation", "action": "", "trigger": "not current_repair_baseline or current != expected (abandon_result spread)", "abandon": True},
    {"line": 2181, "type": "json_tool_result", "identity": "REWORK_FEEDBACK_AUTHORITY_MISSING", "next_tool": "abandon_generation", "action": "", "trigger": "not canonical_feedback (abandon_result spread)", "abandon": True},
    {"line": 2219, "type": "json_tool_result", "identity": "REWORK_FEEDBACK_AUTHORITY_MISMATCH", "next_tool": "abandon_generation", "action": "", "trigger": "reviewer_feedback not transport-equivalent (abandon_result spread)", "abandon": True},
    {"line": 2277, "type": "json_tool_result", "identity": "REWORK_TASK_AUTHORITY_INVALID", "next_tool": "abandon_generation", "action": "", "trigger": "authority_errors (abandon_result spread)", "abandon": True},
    {"line": 2311, "type": "json_tool_result", "identity": "REWORK_TASK_AUTHORITY_MISMATCH", "next_tool": "abandon_generation", "action": "", "trigger": "tasks_provided AND canonical digest mismatch"},
    {"line": 2343, "type": "json_tool_result", "identity": "DECLARED_SCOPE_INTEGRITY_VIOLATION", "next_tool": "abandon_generation", "action": "", "trigger": "declared_scope_violations"},
    {"line": 2356, "type": "json_tool_result", "identity": "execute_workers requires a master plan. Call run_master first to produce a task plan.", "next_tool": "", "action": "", "trigger": "not master_plan AND stage not in rework_stages (pre-rework guard)"},
    {"line": 2375, "type": "json_tool_result", "identity": "WORKER_INITIAL_FEEDBACK_FORBIDDEN", "next_tool": "", "action": "", "trigger": "reviewer_feedback on initial worker stage"},
    {"line": 2401, "type": "json_tool_result", "identity": "WORKER_TASK_AUTHORITY_INVALID", "next_tool": "", "action": "", "trigger": "_authority_errors (initial worker task authority)"},
    {"line": 2432, "type": "json_tool_result", "identity": "WORKER_TASK_PLAN_MISMATCH", "next_tool": "", "action": "", "trigger": "tasks_provided AND tasks != _authoritative_tasks"},
    {"line": 2455, "type": "json_tool_result", "identity": "DURABLE_INITIAL_WORKER_TASK_DRIFT", "next_tool": "", "action": "", "trigger": "durable_tasks digest != authoritative initial tasks digest (abandon_result spread)", "abandon": True},
    {"line": 2566, "type": "json_tool_result", "identity": "No tasks provided and checkpoint has no task plan. Call run_master first.", "next_tool": "", "action": "", "trigger": "else: not tasks AND no checkpoint plan (rework path)"},
    {"line": 2572, "type": "json_tool_result", "identity": "No tasks provided and checkpoint has no task plan. Call run_master first.", "next_tool": "", "action": "", "trigger": "not tasks (initial worker path)"},
    {"line": 2701, "type": "json_tool_result", "identity": "<var:critic_refusal>", "next_tool": "", "action": "", "trigger": "critic_refusal (post-rework re-check)"},
    {"line": 2705, "type": "json_tool_result", "identity": "WORKER_TASK_WRITE_SCOPE_INVALID", "next_tool": "abandon_generation", "action": "", "trigger": "task_write_scope_errors"},
    {"line": 2728, "type": "json_tool_result", "identity": "Precommit failed, but execute_workers was called without reviewer_feedback. Pass the exact precommit_eval directive/blockers as reviewer_feedback.", "next_tool": "", "action": "", "trigger": "_b6_stage == 'precommit_failed'"},
    {"line": 2754, "type": "json_tool_result", "identity": "INFO:redundant_call_blocked", "next_tool": "", "action": "", "trigger": "not reviewer_feedback AND workers already ran (idempotency block)"},
    {"line": 2775, "type": "json_tool_result", "identity": "CIRCUIT BREAKER (worker_failure_count)", "next_tool": "", "action": "", "trigger": "failure_count >= MAX_WORKER_FAILURES (6)"},
    {"line": 2929, "type": "json_tool_result", "identity": "DURABLE_REPAIR_PREPARATION_UNAVAILABLE", "next_tool": "", "action": "abandon_generation", "trigger": "except Exception as exc (repair preparation unavailable)"},
    {"line": 3037, "type": "json_tool_result", "identity": "PRECOMMIT_REWORK_CIRCUIT_BREAKER", "next_tool": "", "action": "", "trigger": "precommit_rework_count_for_write > MAX_PRECOMMIT_REWORK_ROUNDS"},
    {"line": 3074, "type": "json_tool_result", "identity": "OFFICIAL_REWORK_CIRCUIT_BREAKER", "next_tool": "", "action": "", "trigger": "official_rework_count_for_write > MAX_OFFICIAL_REWORK_ROUNDS (abandon_result spread)", "abandon": True},
    {"line": 3122, "type": "json_tool_result", "identity": "REWORK_PREPARATION_SNAPSHOT_FAILED", "next_tool": "abandon_generation", "action": "", "trigger": "except Exception as exc (snapshot capture)"},
    {"line": 3147, "type": "json_tool_result", "identity": "REWORK_PREPARATION_ROLLBACK_FAILED | REWORK_SOURCE_RESET_FAILED", "next_tool": "abandon_generation | execute_workers", "action": "", "trigger": "except Exception as exc (_incremental_reset_next_dir); error is IfExp on rollback_error"},
    {"line": 3232, "type": "json_tool_result", "identity": "REWORK_PREPARATION_ROLLBACK_FAILED | CANDIDATE_HYGIENE_FAILED", "next_tool": "abandon_generation | execute_workers", "action": "", "trigger": "except Exception as exc (candidate hygiene); error is IfExp on rollback_error"},
    {"line": 3301, "type": "json_tool_result", "identity": "REWORK_PREPARATION_ROLLBACK_FAILED | REWORK_MECHANICAL_TRIM_FAILED", "next_tool": "abandon_generation | execute_workers", "action": "", "trigger": "except Exception as exc (mechanical trim); error is IfExp on rollback_error"},
    {"line": 3370, "type": "json_tool_result", "identity": "REWORK_PREPARATION_ROLLBACK_FAILED | REPAIR_BASELINE_ARTIFACT_UNAVAILABLE", "next_tool": "abandon_generation", "action": "", "trigger": "not repair_baseline_artifact_hash; error is IfExp on rollback_error"},
    {"line": 3389, "type": "json_tool_result", "identity": "REPAIR_PREPARATION_SNAPSHOT_MISMATCH", "next_tool": "", "action": "", "trigger": "prepared_repair_snapshot_hash != repair_baseline_artifact_hash"},
    {"line": 3460, "type": "json_tool_result", "identity": "REWORK_PREPARATION_ROLLBACK_FAILED | REPAIR_BASELINE_CHECKPOINT_FAILED", "next_tool": "", "action": "", "trigger": "not repair_checkpoint_written; error is IfExp on rollback_error"},
    {"line": 3488, "type": "json_tool_result", "identity": "REPAIR_BASELINE_ARTIFACT_DRIFT", "next_tool": "abandon_generation", "action": "", "trigger": "prepared_candidate hash drift after prepare"},
    {"line": 3510, "type": "json_tool_result", "identity": "REWORK_PROJECTION_CHECKPOINT_MISSING", "next_tool": "", "action": "", "trigger": "not rework_projection_ckpt"},
    {"line": 3536, "type": "json_tool_result", "identity": "REWORK_RUNNING_CHECKPOINT_FAILED", "next_tool": "", "action": "", "trigger": "not rework_checkpoint_written"},
    {"line": 3554, "type": "json_tool_result", "identity": "REPAIR_BASELINE_ARTIFACT_DRIFT", "next_tool": "abandon_generation", "action": "", "trigger": "current_rework_hash != expected_rework_hash (post-prepare drift)"},
    {"line": 3579, "type": "json_tool_result", "identity": "REPAIR_BASELINE_ARTIFACT_DRIFT", "next_tool": "abandon_generation", "action": "", "trigger": "current rework hash != frozen expected hash before dispatch (abandon_result spread)", "abandon": True},
    {"line": 3612, "type": "json_tool_result", "identity": "DURABLE_WORKER_FROZEN_INPUT_DRIFT", "next_tool": "", "action": "", "trigger": "task_digest != durable_input_digest (abandon_result spread)", "abandon": True},
    {"line": 3625, "type": "json_tool_result", "identity": "DURABLE_WORKER_CHECKPOINT_MISSING_BEFORE_PREPARE", "next_tool": "", "action": "", "trigger": "not projection_ckpt"},
    {"line": 3637, "type": "json_tool_result", "identity": "DURABLE_WORKER_PREPARED_SNAPSHOT_MISMATCH", "next_tool": "abandon_generation", "action": "", "trigger": "prepared_artifact_hash != prepared_snapshot_hash"},
    {"line": 3684, "type": "json_tool_result", "identity": "DURABLE_WORKER_PROJECTION_PREIMAGE_UNAVAILABLE", "next_tool": "", "action": "operator_reconcile", "trigger": "except Exception as exc (projection preimage unavailable)"},
    {"line": 3788, "type": "json_tool_result", "identity": "LLM_AVAILABILITY_STATE_INVALID", "next_tool": "", "action": "operator_reconcile", "trigger": "except Exception as exc (availability state inspection, durable path)"},
    {"line": 3803, "type": "json_tool_result", "identity": "LLM_AVAILABILITY_BLOCKED", "next_tool": "", "action": "wait_for_llm_availability", "trigger": "_active_pause is not None (durable path)"},
    {"line": 3825, "type": "deferred_activity", "identity": "deferred_activity", "next_tool": "", "action": "", "trigger": "actor_lock_owned (deferred worker activity, durable path, lock held)"},
    {"line": 3831, "type": "run_durable_effect", "identity": "run_durable_effect", "next_tool": "", "action": "", "trigger": "fall-through (durable dispatch, acquiring lock)"},
    {"line": 3838, "type": "json_tool_result", "identity": "DURABLE_WORKER_COMMAND_DISPATCH_INVARIANT", "next_tool": "", "action": "", "trigger": "final fall-through (unknown command_name invariant)"},
]


# ---------------------------------------------------------------------------
# Derived aggregates (kept as module-level constants for easy import).
# ---------------------------------------------------------------------------

RETURN_TYPE_COUNTS: Dict[str, int] = {
    "json_tool_result": 65,
    "state_blocked": 2,
    "project_durable_worker_output": 2,
    "project_durable_worker_failure": 2,
    "deferred_activity": 2,
    "run_durable_effect": 2,
    "var_return": 1,
}

#: Number of exits that spread a ``**abandon_result`` into the returned dict.
#: These correspond 1:1 to the entries in ``ABANDON_PATHS``.
ABANDON_EXIT_COUNT = 8

#: Lines of the ``_force_abandon_frozen_worker_generation`` / 
#: ``_force_abandon_official_rework_generation`` calls (NOT the return lines).
FORCE_ABANDON_CALL_LINES = (2138, 2148, 2175, 2213, 2254, 2271, 2448, 3069, 3573, 3605)


# ---------------------------------------------------------------------------
# Internal consistency: the per-exit table must agree with the aggregates.
# ---------------------------------------------------------------------------

def _table_count_by_type() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in EXIT_PATHS:
        counts[row["type"]] = counts.get(row["type"], 0) + 1
    return counts


def _table_abandon_count() -> int:
    return sum(1 for row in EXIT_PATHS if row.get("abandon"))


# ---------------------------------------------------------------------------
# Self-check: re-parse the production source and verify the return count.
# ---------------------------------------------------------------------------
#
# Run via ``python -m web.tests.worker_exit_path_fixture`` or directly
# ``python web/tests/worker_exit_path_fixture.py`` from the repo root. It does
# NOT import the production module -- it reads the source file as text and
# parses it with ``ast`` so the fixture stays decoupled from runtime behavior.
#
# Wave-7 anchor update: the dispatch body was phase-decomposed. The thin
# ``_execute_workers_command`` orchestrator now sequences four module-level
# phase sub-functions (``PHASE_FUNCTIONS`` below). The 76-exit count is taken
# across the whole call graph: orchestrator + 4 phase functions, EXCLUDING:
#   - the per-phase "continuation trailers" (returns whose value is a 1-tuple
#     ``(ctx_updates,)`` -- these signal "fall through to the next phase" and
#     never reach the caller),
#   - the orchestrator's 4 propagation returns (``return result`` after a phase
#     returned an early value -- these forward a phase exit, they are not new
#     exits themselves), and
#   - the nested ``rollback_rework_preparation`` closure inside Phase C.

_PROD_REL_PATH = "web/core/tool_planning_worker_phases.py"
_TARGET_FUNCTION = "_execute_workers_command"
_TARGET_FUNC_START = 2419
_TARGET_FUNC_END = 2453

#: The four module-level phase sub-functions that own the real exit paths.
#: The self-check walks each of these plus the orchestrator as one call graph.
PHASE_FUNCTIONS = (
    "_execute_workers_phase_a_preamble",
    "_execute_workers_phase_b_rework_synthesis",
    "_execute_workers_phase_c_rework_preparation",
    "_execute_workers_phase_d_projection",
)

_NESTED_FUNC_NAME = "rollback_rework_preparation"
# Nested-helper range, now inside Phase C (was 1352-1361 in the pre-wave-7
# monolith). Updated by the wave-7 phase decomposition; re-derived by the AST
# walk so this is only a documented anchor, not a hard assertion.
_NESTED_FUNC_RANGE = None  # set lazily by the AST walk in _run_self_check


def _resolve_repo_root() -> str:
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    # /home/ubuntu/pok1/web/tests/worker_exit_path_fixture.py -> repo root is two levels up.
    return os.path.dirname(os.path.dirname(here))


def _is_continuation_return(node) -> bool:
    """True if ``node`` is a phase-continuation trailer (returns a 1-tuple)."""
    import ast
    val = node.value
    return isinstance(val, ast.Tuple) and len(val.elts) == 1


def _is_orchestrator_propagation_return(node, func_name: str) -> bool:
    """True if ``node`` is the orchestrator's ``return result`` (forwards a phase exit).

    The orchestrator (``_execute_workers_command``) has exactly 4 propagation
    returns of the bare name ``result``; they are not new exits. We detect them
    by return value being ``ast.Name(id='result')`` inside the orchestrator.
    """
    import ast
    if func_name != _TARGET_FUNCTION:
        return False
    val = node.value
    return isinstance(val, ast.Name) and val.id == "result"


def _count_returns_in_target_function(repo_root: str) -> Tuple[int, list, list, list]:
    """Return (count, exit_return_line_numbers, nested_return_line_numbers, nested_func_names).

    Walks the orchestrator PLUS the four phase sub-functions as one call graph.
    Excludes:
      * returns inside any nested function (``rollback_rework_preparation``),
      * phase-continuation trailers (1-tuple ``(ctx,)`` returns),
      * orchestrator propagation returns (``return result``).
    """
    import ast
    import os

    full = os.path.join(repo_root, _PROD_REL_PATH)
    with open(full, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)

    # Locate the orchestrator + the four phase functions.
    targets = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in (_TARGET_FUNCTION, *PHASE_FUNCTIONS)
        ):
            targets[node.name] = node

    missing = {n for n in (_TARGET_FUNCTION, *PHASE_FUNCTIONS) if n not in targets}
    if missing:
        raise RuntimeError(
            f"Missing functions in {_PROD_REL_PATH}: {sorted(missing)}; "
            f"fixture is stale."
        )

    orchestrator = targets[_TARGET_FUNCTION]
    # Sanity: the orchestrator must still live where we expect.
    if orchestrator.lineno != _TARGET_FUNC_START:
        raise RuntimeError(
            f"{_TARGET_FUNCTION} now starts at line {orchestrator.lineno}, "
            f"expected {_TARGET_FUNC_START}. The refactor may have landed; "
            f"re-derive this fixture."
        )

    # Collect nested function ranges across ALL phase functions (the rollback
    # closure lives inside Phase C). Returns inside these are excluded.
    nested_ranges: List[Tuple[int, int, str]] = []
    for fname, fnode in targets.items():
        for node in ast.walk(fnode):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node is not fnode
            ):
                nested_ranges.append((node.lineno, node.end_lineno, node.name))

    def _in_nested(line: int) -> bool:
        for start, end, _name in nested_ranges:
            if start < line <= end:
                return True
        return False

    # Collect real exit returns across the call graph.
    exit_returns: List[int] = []
    nested_returns: List[int] = []
    for fname, fnode in targets.items():
        for node in ast.walk(fnode):
            if not isinstance(node, ast.Return):
                continue
            if _in_nested(node.lineno):
                nested_returns.append(node.lineno)
                continue
            # Skip phase-continuation trailers (1-tuple returns).
            if fname in PHASE_FUNCTIONS and _is_continuation_return(node):
                continue
            # Skip orchestrator propagation returns (``return result``).
            if fname == _TARGET_FUNCTION and _is_orchestrator_propagation_return(node, fname):
                continue
            exit_returns.append(node.lineno)

    exit_returns.sort()
    nested_returns.sort()
    nested_func_names = sorted({name for (_s, _e, name) in nested_ranges})
    return len(exit_returns), exit_returns, nested_returns, nested_func_names


def _run_self_check() -> None:
    global _NESTED_FUNC_RANGE
    repo_root = _resolve_repo_root()
    count, lines, nested, nested_names = _count_returns_in_target_function(repo_root)

    # Capture the rollback closure's current range for the printed report.
    import ast, os
    full = os.path.join(repo_root, _PROD_REL_PATH)
    tree = ast.parse(open(full).read())
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == _NESTED_FUNC_NAME):
            _NESTED_FUNC_RANGE = (node.lineno, node.end_lineno)
            break

    failures = []

    if count != EXPECTED_WORKER_EXIT_COUNT:
        failures.append(
            f"EXPECTED_WORKER_EXIT_COUNT={EXPECTED_WORKER_EXIT_COUNT} but source "
            f"has {count} exit-path returns across the orchestrator + "
            f"{len(PHASE_FUNCTIONS)} phase functions (excluding nested, "
            f"continuation trailers, and orchestrator propagation returns)."
        )

    # The per-exit table length must match the count too.
    if len(EXIT_PATHS) != EXPECTED_WORKER_EXIT_COUNT:
        failures.append(
            f"EXIT_PATHS table has {len(EXIT_PATHS)} rows but "
            f"EXPECTED_WORKER_EXIT_COUNT={EXPECTED_WORKER_EXIT_COUNT}."
        )

    if count != len(EXIT_PATHS):
        failures.append(
            f"Source has {count} exit returns but EXIT_PATHS table has "
            f"{len(EXIT_PATHS)} rows (count drifted)."
        )

    # Aggregate counts must agree with the table.
    table_by_type = _table_count_by_type()
    for rtype, expected in RETURN_TYPE_COUNTS.items():
        actual = table_by_type.get(rtype, 0)
        if actual != expected:
            failures.append(
                f"RETURN_TYPE_COUNTS[{rtype!r}]={expected} but table has {actual}."
            )
    extra_types = set(table_by_type) - set(RETURN_TYPE_COUNTS)
    if extra_types:
        failures.append(f"Unexpected return types in table: {sorted(extra_types)}")

    # Abandon-path accounting.
    if _table_abandon_count() != ABANDON_EXIT_COUNT:
        failures.append(
            f"ABANDON_EXIT_COUNT={ABANDON_EXIT_COUNT} but table flags "
            f"{_table_abandon_count()} rows with abandon=True."
        )
    if len(ABANDON_PATHS) != ABANDON_EXIT_COUNT:
        failures.append(
            f"ABANDON_PATHS has {len(ABANDON_PATHS)} entries but "
            f"ABANDON_EXIT_COUNT={ABANDON_EXIT_COUNT}."
        )

    # Distinct-reason set must be internally consistent with the table.
    table_identities = {row["identity"] for row in EXIT_PATHS}
    if table_identities != EXPECTED_WORKER_EXIT_REASONS:
        only_in_table = table_identities - EXPECTED_WORKER_EXIT_REASONS
        only_in_set = EXPECTED_WORKER_EXIT_REASONS - table_identities
        failures.append(
            f"EXPECTED_WORKER_EXIT_REASONS disagrees with EXIT_PATHS identities.\n"
            f"  only in table: {sorted(only_in_table)}\n"
            f"  only in set:   {sorted(only_in_set)}"
        )

    # Sanity: the nested helper we expect to exclude must still be there.
    # After the wave-7 phase decomposition the helper lives inside Phase C.
    if _NESTED_FUNC_NAME not in set(nested_names):
        failures.append(
            f"Nested helper {_NESTED_FUNC_NAME!r} not found inside any phase "
            f"function; the rollback cascade exits cannot be accurately excluded."
        )
    if not nested:
        print(
            f"NOTE: no nested-function returns detected across the call graph "
            f"(rollback helper may have been inlined)."
        )

    if failures:
        print("SELF-CHECK FAILED:")
        for f in failures:
            print("  - " + f)
        raise SystemExit(1)

    print("SELF-CHECK PASSED:")
    print(f"  orchestrator    : {_PROD_REL_PATH}::{_TARGET_FUNCTION} (lines "
          f"{_TARGET_FUNC_START}-{_TARGET_FUNC_END})")
    print(f"  phase functions : {', '.join(PHASE_FUNCTIONS)}")
    print(f"  exit returns    : {count}  (matches EXPECTED_WORKER_EXIT_COUNT)")
    print(f"  nested excludes : {len(nested)} return(s) from "
          f"{_NESTED_FUNC_NAME!r} at {nested}")
    print(f"  distinct reasons: {len(EXPECTED_WORKER_EXIT_REASONS)}")
    print(f"  abandon exits   : {ABANDON_EXIT_COUNT}  (highest-risk paths)")
    print(f"  by type         : {RETURN_TYPE_COUNTS}")


if __name__ == "__main__":
    _run_self_check()
