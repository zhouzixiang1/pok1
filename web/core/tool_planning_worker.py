"""Worker durable execution and quality/repair contract engine.

Extracted from tool_planning.py for maintainability. Contains the group E
(quality/repair contracts) and group F (worker durable execution) code:

- Quality/repair task classification and contract builders
- Rework task synthesis (_synthesize_rework_tasks_from_checkpoint)
- Worker prompt template loading and rendering
- Durable worker effect execution (_run_durable_worker_effect)
- Worker command execution (_execute_workers_command)
- The execute_workers MCP tool entry point

All public symbols are re-exported by tool_planning.py via an explicit
``from tool_planning_worker import (...)`` block, so existing imports of
``tool_planning.<symbol>`` continue to resolve to the same objects.

Monkeypatch compatibility
-------------------------
A number of test suites do ``monkeypatch.setattr(tool_planning, "<name>", ...)``
for symbols whose implementations now live in this module.  Because a Python
function resolves bare globals (LOAD_GLOBAL) against its *own* module's
``__dict__``, simply rebinding the name on ``tool_planning`` would not affect
this module's lookups.  To preserve the historical test contract *without*
modifying any test or rewriting every call site, the affected symbols are
bound here as :class:`_TPCallableProxy` instances.  Each proxy re-reads the
*current* attribute on ``tool_planning`` at call time, so a monkeypatch on
``tool_planning`` is observed live.  All real usages of these symbols in the
extracted body are plain calls (``name(args)``), which the proxy's
``__call__`` forwards correctly.
"""

import ast
from copy import deepcopy
import io
import json
import os
import py_compile
import re
import hashlib
import time
import tokenize
from dataclasses import dataclass
from pathlib import Path

from bot_namespace import bot_name
from tool_runtime_guard import tool

from evolution_core import (
    check_code_size,
    MAX_PRECOMMIT_REWORK_ROUNDS,
    MAX_OFFICIAL_REWORK_ROUNDS,
)
from tool_helpers import (
    _json_tool_result,
    _state_blocked,
    _resolve_version_args,
    PROJECT_ROOT,
    _set_pipeline_status,
    _target_rel,
    _validate_worker_boundaries,
)
from pipeline_state import route_policy
from llm_availability import LLMAvailabilityBlocked
from output_schema import (
    NATIONAL_POLICY_FOCUS_ID,
    POLICY_CONTEXT_SCHEMA_VERSION,
    POLICY_CONTEXT_TOP_LEVEL_FIELDS,
    POLICY_ENTRYPOINTS,
    POLICY_INTENT_KINDS,
    PRECOMPUTE_KEY_SHAPE_PATTERN,
    PRECOMPUTE_MAX_BUILD_MS,
    PRECOMPUTE_MAX_BYTES,
    PRECOMPUTE_MAX_ENTRIES,
    RuntimeContract,
)

import sys

# Worker quality-contract engine (Group E): failure-source analysis, contract
# builders, mechanical trimming, and authoritative rework synthesis. This is a
# self-contained leaf extracted to the ``tool_planning_quality_contracts``
# companion; re-exported here so every existing ``from tool_planning_worker
# import <name>`` and ``tool_planning.<name>`` site keeps resolving. Tests
# monkeypatch ``tool_planning`` (never this module), so a plain re-export
# suffices.
from tool_planning_quality_contracts import (  # noqa: F401
    _ARCHITECTURE_CHECK_FILES,
    _ARCHITECTURE_FOCUS_LAYERS,
    _PRECOMMIT_PROTOCOL_EVIDENCE_MARKERS,
    _PRECOMMIT_PROTOCOL_REPAIR_FILES,
    _PRECOMMIT_STRATEGY_REPAIR_FILES,
    _REVERT_FEEDBACK_MARKERS,
    _SCOPE_DRIFT_FEEDBACK_MARKERS,
    _STATE_LEARNING_ORACLE_REFS,
    _apply_mechanical_file_size_trims,
    _architecture_contracts,
    _architecture_default_runtime_contract,
    _architecture_repair_context,
    _architecture_transition_failure_ids,
    _architecture_transition_repair_files,
    _authoritative_rework_tasks,
    _candidate_consumed_precompute_contracts,
    _canonical_tasks_digest,
    _checkpoint_master_plan,
    _checkpoint_master_task_authority_errors,
    _checkpoint_plan_with_tasks,
    _checkpoint_repair_baseline_fingerprint,
    _checkpoint_rework_feedback,
    _checkpoint_work_item,
    _critic_advisory_rework_refusal,
    _declared_scope_violation_files,
    _default_state_learning_contract,
    _detected_artifact_consumer,
    _docstring_line_ranges,
    _extract_quality_failure_files,
    _feedback_quality_contracts,
    _flatten_text_items,
    _format_position_details,
    _frozen_rework_task_authority_errors,
    _generic_quality_contracts,
    _has_legacy_critic_repair_contract,
    _has_scope_drift_marker,
    _int_or_none,
    _is_declared_scope_failure_text,
    _is_file_size_repair_task,
    _is_national_native_contract_failure_text,
    _is_official_rework_checkpoint,
    _is_official_smoke_protocol_failure_text,
    _is_position_semantics_failure_text,
    _is_precommit_rework_checkpoint,
    _is_review_rework_checkpoint,
    _is_runtime_architecture_failure_text,
    _limit_precommit_repair_targets,
    _line_count_contracts,
    _mechanical_trim_python_file,
    _mechanically_trim_python_text,
    _merge_runtime_contract_floor,
    _national_native_contracts,
    _normalize_repair_blocker,
    _official_deterministic_failure_items,
    _official_failure_is_protocol,
    _official_failure_items,
    _official_repair_target_files,
    _official_repair_tasks,
    _official_smoke_contracts,
    _order_quality_repair_tasks,
    _plan_repair_scope_files,
    _plan_with_accumulated_repair_scope,
    _position_contracts,
    _precommit_changed_python_files,
    _precommit_failure_items,
    _precommit_filter_repair_targets,
    _precommit_protocol_compliance_failure,
    _precommit_repair_target_files,
    _precommit_repair_task,
    _precommit_repair_task_refresh_reason,
    _precommit_repair_tasks,
    _primary_feedback_file,
    _quality_contract_signature,
    _quality_contract_signatures,
    _quality_contract_task,
    _quality_task_contract_refresh_reason,
    _quality_failure_items,
    _quality_failure_target_files,
    _quality_repair_contracts,
    _quality_rework_skipper,
    _repair_contract_signature,
    _review_feedback_items,
    _review_primary_feedback_text,
    _review_repair_target_files,
    _review_repair_task_refresh_reason,
    _scope_drift_feedback_files,
    _should_reset_before_rework,
    _split_reviewer_quality_feedback,
    _stale_quality_task_reason,
    _synthesize_rework_tasks_from_checkpoint,
    _target_rel,
    _task_declared_scope_files,
    _task_id_suffix,
    _task_matches_quality_blocker,
    _task_must_change_filenames,
    _task_quality_contract_signatures,
    _task_quality_recheck_blockers,
    _task_target_filenames,
    _task_write_scope_errors,
    _text_line_count,
    _tokenized_comment_and_string_lines,
    _transport_equivalent_feedback,
)
import tool_planning_worker_durable as _dur  # noqa: E402  # Group F durable Worker projection/effect cluster



class _TPCallableProxy:
    """Callable proxy that re-reads ``tool_planning.<name>`` on every call.

    ``tool_planning`` re-exports every symbol defined in this module, and it
    also (via its own A-D header imports) exposes the external helper symbols
    the worker body calls.  Binding those names here as proxies means a
    ``monkeypatch.setattr(tool_planning, name, fake)`` issued by a test is
    observed the next time the worker calls ``name(...)``.

    Static analysis confirms every real usage of these names in the extracted
    body is a plain call (zero attribute-access, zero bare non-call loads),
    so ``__call__`` plus attribute forwarding is sufficient.
    """

    __slots__ = ("_name",)

    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def _resolve(self):
        tp = sys.modules["tool_planning"]
        return getattr(tp, object.__getattribute__(self, "_name"))

    def __call__(self, *args, **kwargs):
        return object.__getattribute__(self, "_resolve")()(*args, **kwargs)

    def __getattr__(self, attr):
        return getattr(object.__getattribute__(self, "_resolve")(), attr)

    def __repr__(self):
        try:
            return repr(object.__getattribute__(self, "_resolve")())
        except Exception:
            return f"<_TPCallableProxy name={object.__getattribute__(self, '_name')!r}>"


# Names that test suites historically monkeypatch on ``tool_planning`` and
# that the extracted body calls.  These resolve live through tool_planning so
# monkeypatch.setattr(tool_planning, <name>, ...) keeps working.
_MONKEYPATCHED_TP_SYMBOLS = (
    "_matching_checkpoint",
    "get_bot_dir",
    "log_system_event",
    "write_pipeline_checkpoint",
    "_complete_artifact_fingerprint",
    "_owned_infrastructure_failure",
    "_py_files_changed_between",
    "_get_ui",
    "_execute_workers",
)

for _proxy_name in _MONKEYPATCHED_TP_SYMBOLS:
    globals()[_proxy_name] = _TPCallableProxy(_proxy_name)
del _proxy_name


# A-D symbols (defined in tool_planning.py before the extraction point) that
# the E+F body references via bare LOAD_GLOBAL.  Static analysis confirms none
# of these (other than ``_complete_artifact_fingerprint``, which is already a
# proxy above because tests monkeypatch it) are monkeypatched by the test
# suite, so a single snapshot binding at module-load time is sufficient.
#
# IMPORTANT: Python's module-level ``__getattr__`` does NOT fire for
# LOAD_GLOBAL inside functions -- it only fires for attribute access from
# *other* modules.  Therefore we bind these eagerly via
# :func:`_bootstrap_ad_symbols`, which runs at the end of this module's load.
# At that point ``tool_planning`` is mid-import but has already executed all
# of its A-D top-level definitions, so its ``__dict__`` already contains every
# A-D name we need.
_AD_SNAPSHOT_SYMBOLS = (
    "IDENTITY_REPLAN_ABANDON_THRESHOLD",
    "_ACTIVE_CANDIDATE_WRITABLE_FILES",
    "_checkpoint_architecture_policy_identity_errors",
    "_checkpoint_runtime_contract_ledger_digest",
    "_cleanup_worker_transients_before_identity_refresh",
    "_clear_compiled_task_context",
    "_force_abandon_frozen_worker_generation",
    "_force_abandon_official_rework_generation",
    "_identity_replan_consecutive_count",
    "_identity_replan_counts",
    "_identity_replan_fingerprint",
    "_incremental_reset_next_dir",
    "_is_fresh_empty_pool_bootstrap",
    "_log",
    "_record_identity_replan_attempt",
    "_recover_architecture_policy_identity",
)


def _bootstrap_ad_symbols():
    """Bind the A-D snapshot symbols into this module's globals."""
    tp = sys.modules.get("tool_planning")
    if tp is None:
        return
    _g = globals()
    for _name in _AD_SNAPSHOT_SYMBOLS:
        if hasattr(tp, _name):
            _g[_name] = getattr(tp, _name)


def __getattr__(name):
    """Defensive fallback for direct ``import tool_planning_worker`` use.

    Only reached for attribute access (``module.name``), never for LOAD_GLOBAL
    inside functions.  Caches into globals() so subsequent LOAD_GLOBALs find it.
    """
    if name in _AD_SNAPSHOT_SYMBOLS:
        tp = sys.modules.get("tool_planning")
        if tp is not None and hasattr(tp, name):
            value = getattr(tp, name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Begin extracted body (originally tool_planning.py lines 6074-13236).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Group F (durable worker projection/effect) has been extracted to the
# ``tool_planning_worker_durable`` companion.  Each entry below is a thin
# delegate that forwards to the companion; keyword-only params, async-ness,
# and (for execute_workers) the @tool decorator are preserved.  External
# symbols these bodies call are reached in the companion via ``_tw.<name>``,
# so monkeypatch compatibility is unchanged.
# ---------------------------------------------------------------------------


def _load_worker_prompt_template(prompts_dir, *, native_tcp=None):
    return _dur._load_worker_prompt_template(prompts_dir, native_tcp=native_tcp)


def _durable_checkpoint_contract_matches(checkpoint, contract):
    return _dur._durable_checkpoint_contract_matches(checkpoint, contract)


def _durable_output_already_projected(checkpoint, projection):
    return _dur._durable_output_already_projected(checkpoint, projection)


async def _project_durable_worker_output(worker_workflow, next_dir, state):
    return await _dur._project_durable_worker_output(
        worker_workflow, next_dir, state
    )


async def _project_durable_worker_failure(worker_workflow, state):
    return await _dur._project_durable_worker_failure(worker_workflow, state)


async def _run_durable_worker_effect(
    worker_workflow,
    envelope,
    next_dir,
    worker_template,
):
    return await _dur._run_durable_worker_effect(
        worker_workflow, envelope, next_dir, worker_template
    )


def _worker_execution_task_digest(
    tasks,
    reviewer_feedback,
    worker_template,
):
    return _dur._worker_execution_task_digest(
        tasks, reviewer_feedback, worker_template
    )


def _worker_backend_contract():
    return _dur._worker_backend_contract()


def _expected_worker_backend_contract(checkpoint, envelope=None):
    return _dur._expected_worker_backend_contract(checkpoint, envelope)


def _worker_availability_resume_receipt_errors(deferred, pause_audit):
    return _dur._worker_availability_resume_receipt_errors(deferred, pause_audit)


# ``_DeferredWorkerActivity`` is a frozen dataclass defined in the companion.
# Alias it into this module's namespace so every historical
# ``from tool_planning_worker import _DeferredWorkerActivity`` (and the
# ``isinstance`` / construction sites) keeps resolving to one shared class
# object.
_DeferredWorkerActivity = _dur._DeferredWorkerActivity


async def _execute_workers_command(args, *, actor_lock_owned=False):
    from tool_planning_worker_phases import _execute_workers_command as _impl
    return await _impl(args, actor_lock_owned=actor_lock_owned)


@tool("execute_workers", "Execute worker tasks to modify bot code. Each task has worker_id, role, target_files, worker_prompt.", {"tasks": list, "next_v": int, "source_v": int, "reviewer_feedback": str})
async def execute_workers(args):
    return await _dur.execute_workers(args)


# ---------------------------------------------------------------------------
# End of extracted body.  Bind A-D snapshot symbols now that tool_planning's
# A-D top-level code has finished executing (we are imported from
# tool_planning's bottom, so its __dict__ is already populated).
# ---------------------------------------------------------------------------
_bootstrap_ad_symbols()

# Bootstrap the quality-contracts companion's parent-module data constants
# (_ACTIVE_CANDIDATE_WRITABLE_FILES, _is_fresh_empty_pool_bootstrap) now that
# tool_planning's __dict__ is fully populated. The companion's monkeypatched
# symbols resolve live via _TPCallableProxy; only these immutable data
# constants need a one-shot snapshot.
try:
    from tool_planning_quality_contracts import _bootstrap_qc_snapshot_symbols as _qc_bootstrap

    _qc_bootstrap()
except Exception:
    pass
