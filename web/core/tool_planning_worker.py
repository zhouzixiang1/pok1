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
    _expected_worker_backend_contract,
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
    _worker_backend_contract,
    _worker_execution_task_digest,
)


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

def _load_worker_prompt_template(prompts_dir, *, native_tcp=None):
    """Compose the worker harness for the sole national-native profile."""
    prompts_dir = Path(prompts_dir)
    if native_tcp is None:
        from workflow_profiles import get_workflow_profile

        native_tcp = (
            getattr(get_workflow_profile(), "national_execution_mode", "native_tcp")
            == "native_tcp"
        )
    if not native_tcp:
        raise RuntimeError("active Worker execution requires national native TCP")
    common = (prompts_dir / "worker_prompt.md").read_text(encoding="utf-8")
    marker = "{execution_profile_contract}"
    if common.count(marker) != 1:
        raise RuntimeError(
            "worker_prompt.md must contain exactly one execution profile marker"
        )
    profile = (prompts_dir / "worker_profile_national_native.md").read_text(
        encoding="utf-8"
    )
    return common.replace(marker, profile)


def _durable_checkpoint_contract_matches(checkpoint, contract):
    if not isinstance(checkpoint, dict) or not isinstance(contract, dict):
        return False
    checkpoint_workflow_id = str(
        checkpoint.get("workflow_run_id")
        or checkpoint.get("run_id")
        or (
            f"{int(checkpoint.get('next_v'))}#"
            f"{int(checkpoint.get('generation_attempt') or 0)}"
        )
    )
    return (
        checkpoint_workflow_id
        == str(contract.get("workflow_run_id") or "")
        and int(checkpoint.get("checkpoint_revision") or 0)
        == int(contract.get("checkpoint_revision") or 0)
        and str(checkpoint.get("stage") or "")
        == str(contract.get("checkpoint_stage") or "")
    )


def _durable_output_already_projected(checkpoint, projection):
    if not isinstance(checkpoint, dict):
        return False
    contract = projection.get("checkpoint_contract") or {}
    checkpoint_workflow_id = str(
        checkpoint.get("workflow_run_id")
        or checkpoint.get("run_id")
        or ""
    )
    if checkpoint_workflow_id != str(contract.get("workflow_run_id") or ""):
        return False
    receipt = (
        (checkpoint.get("audit_context") or {}).get("durable_worker_output")
        if isinstance(checkpoint.get("audit_context"), dict)
        else None
    )
    expected = projection.get("durable_worker_output") or {}
    return bool(
        isinstance(receipt, dict)
        and receipt.get("artifact_hash") == expected.get("artifact_hash")
        and receipt.get("envelope_digest") == expected.get("envelope_digest")
    )


async def _project_durable_worker_output(worker_workflow, next_dir, state):
    """Project a completed immutable Worker receipt without invoking an LLM."""
    projection = deepcopy(state.get("projection") or {})
    envelope = state.get("envelope") or {}
    next_v = int(envelope.get("next_v"))
    source_v = int(envelope.get("source_v"))
    contract = projection.get("checkpoint_contract") or {}
    checkpoint = _matching_checkpoint(next_v, source_v)
    if _durable_output_already_projected(checkpoint, projection):
        # At the immediate workers_done projection, reconcile a missing or
        # poisoned canonical tree from the immutable artifact.  If downstream
        # gates already advanced the checkpoint, their matching receipt proves
        # this output was published; never rewind candidate bytes that a later
        # authorized stage may have transformed.
        if checkpoint.get("stage") == "workers_done":
            expected_output = str(state.get("output_artifact_hash") or "")
            canonical_exists = Path(next_dir).exists()
            canonical_hash = (
                _complete_artifact_fingerprint(next_dir)
                if canonical_exists
                else ""
            )
            if canonical_exists and canonical_hash != expected_output:
                return _json_tool_result({
                    "error": "DURABLE_WORKER_PROJECTED_ARTIFACT_MISMATCH",
                    "success": False,
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                })
            if not canonical_exists:
                worker_workflow.artifacts.materialize(
                    str(state.get("output_snapshot_hash") or ""),
                    next_dir,
                    expected_destination_digest=None,
                )
            if _complete_artifact_fingerprint(next_dir) != expected_output:
                return _json_tool_result({
                    "error": "DURABLE_WORKER_PROJECTED_ARTIFACT_MISMATCH",
                    "success": False,
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                })
        worker_workflow.projected("workers_done")
        return _json_tool_result({
            "success": True,
            "durable_recovery": (
                "confirmed_existing_worker_projection"
                if checkpoint.get("stage") == "workers_done"
                else "confirmed_downstream_worker_projection"
            ),
            "current_checkpoint_stage": checkpoint.get("stage"),
            "output_artifact_hash": state.get("output_artifact_hash"),
            "next_v": next_v,
            "source_v": source_v,
        })
    if not _durable_checkpoint_contract_matches(checkpoint, contract):
        return _json_tool_result({
            "error": "DURABLE_WORKER_OUTPUT_PROJECTION_CONFLICT",
            "success": False,
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "expected_checkpoint": contract,
            "current_checkpoint": {
                "workflow_run_id": (
                    checkpoint.get("workflow_run_id") if checkpoint else None
                ),
                "checkpoint_revision": (
                    checkpoint.get("checkpoint_revision") if checkpoint else None
                ),
                "stage": checkpoint.get("stage") if checkpoint else None,
            },
            "directive": (
                "The immutable output is safe, but another command advanced the "
                "checkpoint. Do not rewind it or call the LLM; reconcile the actor "
                "history with the current projection."
            ),
        })
    projection_preimage_hash = str(
        envelope.get("projection_preimage_artifact_hash") or ""
    )
    projection_preimage_snapshot = str(
        envelope.get("projection_preimage_snapshot_hash") or ""
    )
    output_hash = str(state.get("output_artifact_hash") or "")
    current_artifact_hash = _complete_artifact_fingerprint(next_dir)
    if (
        Path(next_dir).exists()
        and current_artifact_hash not in {
            projection_preimage_hash,
            output_hash,
        }
    ):
        return _json_tool_result({
            "error": "DURABLE_WORKER_PRE_PROJECTION_ARTIFACT_DRIFT",
            "success": False,
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "expected_output_artifact_hash": output_hash,
            "expected_projection_preimage_artifact_hash": (
                projection_preimage_hash
            ),
            "current_artifact_hash": current_artifact_hash,
            "directive": (
                "The canonical candidate no longer matches either immutable "
                "Worker boundary. Do not overwrite concurrent or operator bytes."
            ),
        })
    materialization_receipt = worker_workflow.artifacts.materialize(
        str(state.get("output_snapshot_hash") or ""),
        next_dir,
        expected_destination_digest=(
            current_artifact_hash if Path(next_dir).exists() else None
        ),
    )
    audit_context = deepcopy(projection.get("audit_context") or {})
    audit_context["durable_worker_output"] = deepcopy(
        projection.get("durable_worker_output") or {}
    )
    projected = write_pipeline_checkpoint(
        next_v,
        source_v,
        "workers_done",
        master_plan=deepcopy(projection.get("master_plan") or {}),
        reviewer_feedback=str(projection.get("reviewer_feedback") or ""),
        worker_failure_count=int(projection.get("worker_failure_count") or 0),
        audit_context=audit_context,
        precommit_rework_count=int(
            projection.get("precommit_rework_count") or 0
        ),
        official_rework_count=int(
            projection.get("official_rework_count") or 0
        ),
        expected_checkpoint_revision=int(contract.get("checkpoint_revision") or 0),
        expected_checkpoint_stage=str(contract.get("checkpoint_stage") or ""),
        expected_workflow_run_id=str(contract.get("workflow_run_id") or ""),
    )
    if not projected:
        current_checkpoint = _matching_checkpoint(next_v, source_v)
        if _durable_output_already_projected(current_checkpoint, projection):
            if (
                current_checkpoint.get("stage") == "workers_done"
                and _complete_artifact_fingerprint(next_dir) != output_hash
            ):
                return _json_tool_result({
                    "error": "DURABLE_WORKER_CONCURRENT_PROJECTION_ARTIFACT_MISMATCH",
                    "success": False,
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "expected_output_artifact_hash": output_hash,
                    "current_artifact_hash": _complete_artifact_fingerprint(next_dir),
                })
            worker_workflow.projected("workers_done")
            return _json_tool_result({
                "success": True,
                "durable_recovery": "confirmed_concurrent_worker_projection",
                "current_checkpoint_stage": current_checkpoint.get("stage"),
                "output_artifact_hash": output_hash,
                "next_v": next_v,
                "source_v": source_v,
            })

        if not materialization_receipt.installed:
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_PREEXISTED_FAILED_CHECKPOINT_CAS",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "output_artifact_hash": output_hash,
                "materialization_receipt_digest": (
                    materialization_receipt.receipt_digest
                ),
                "directive": (
                    "The output bytes predated this command, so this command has "
                    "no authority to roll them back after losing the checkpoint CAS."
                ),
            })

        # Candidate bytes and checkpoint projection are one semantic effect.
        # If the CAS lost, restore the exact immutable preimage, but only while
        # the canonical tree is still the output written by this command.  A
        # different hash proves a concurrent writer and must never be clobbered.
        post_cas_artifact_hash = _complete_artifact_fingerprint(next_dir)
        if post_cas_artifact_hash != output_hash:
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_PROJECTION_CONCURRENT_DRIFT",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "expected_output_artifact_hash": output_hash,
                "current_artifact_hash": post_cas_artifact_hash,
                "directive": (
                    "The checkpoint CAS failed and another writer changed the "
                    "candidate. Preserve both histories for operator reconciliation."
                ),
            })
        try:
            worker_workflow.artifacts.materialize(
                projection_preimage_snapshot,
                next_dir,
                expected_destination_digest=output_hash,
            )
        except BaseException as exc:
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_ROLLBACK_FAILED",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
        restored_hash = _complete_artifact_fingerprint(next_dir)
        if restored_hash != projection_preimage_hash:
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_ROLLBACK_MISMATCH",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "expected_projection_preimage_artifact_hash": (
                    projection_preimage_hash
                ),
                "restored_artifact_hash": restored_hash,
            })
        return _json_tool_result({
            "error": "DURABLE_WORKER_OUTPUT_PROJECTION_FAILED",
            "success": False,
            "action": "retry_same_tool",
            "next_v": next_v,
            "source_v": source_v,
            "output_artifact_hash": state.get("output_artifact_hash"),
            "canonical_artifact_restored": True,
            "restored_artifact_hash": restored_hash,
            "directive": (
                "The immutable Worker output receipt is safe. Retry execute_workers "
                "to project it; the LLM will not be called again."
            ),
        })
    post_commit_artifact_hash = _complete_artifact_fingerprint(next_dir)
    if post_commit_artifact_hash != output_hash:
        return _json_tool_result({
            "error": "DURABLE_WORKER_POST_COMMIT_ARTIFACT_MISMATCH",
            "success": False,
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "expected_output_artifact_hash": output_hash,
            "current_artifact_hash": post_commit_artifact_hash,
        })
    worker_workflow.projected("workers_done")
    return _json_tool_result({
        "success": True,
        "durable_recovery": "projected_existing_worker_output",
        "output_artifact_hash": state.get("output_artifact_hash"),
        "next_v": next_v,
        "source_v": source_v,
    })


async def _project_durable_worker_failure(worker_workflow, state):
    """Project a semantic failure receipt before another Worker cycle can open."""
    projection = deepcopy(state.get("failure_projection") or {})
    envelope = state.get("envelope") or {}
    next_v = int(envelope.get("next_v"))
    source_v = int(envelope.get("source_v"))
    contract = projection.get("checkpoint_contract") or {}
    checkpoint = _matching_checkpoint(next_v, source_v)
    target_stage = str(projection.get("stage") or "repair_planned")
    receipt = (
        (checkpoint.get("audit_context") or {}).get("durable_worker_failure")
        if isinstance(checkpoint, dict)
        and isinstance(checkpoint.get("audit_context"), dict)
        else None
    )
    expected_receipt = projection.get("durable_worker_failure") or {}
    already_projected = bool(
        isinstance(checkpoint, dict)
        and str(
            checkpoint.get("workflow_run_id")
            or checkpoint.get("run_id")
            or ""
        ) == str(contract.get("workflow_run_id") or "")
        and isinstance(receipt, dict)
        and receipt.get("envelope_digest")
        == expected_receipt.get("envelope_digest")
        and receipt.get("semantic_attempt")
        == expected_receipt.get("semantic_attempt")
    )
    if not already_projected:
        if not _durable_checkpoint_contract_matches(checkpoint, contract):
            return _json_tool_result({
                "error": "DURABLE_WORKER_FAILURE_PROJECTION_CONFLICT",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
            })
        audit_context = deepcopy(projection.get("audit_context") or {})
        audit_context["durable_worker_failure"] = expected_receipt
        checkpoint_kwargs = {}
        if target_stage == "direction_audited" and projection.get(
            "runtime_contract_ledger_digest"
        ):
            checkpoint_kwargs = {
                "reset_runtime_contract_ledger": True,
                "expected_runtime_contract_ledger_digest": projection[
                    "runtime_contract_ledger_digest"
                ],
                "runtime_contract_ledger_reset_reason": (
                    "master_plan_rejected_replan"
                ),
            }
        written = write_pipeline_checkpoint(
            next_v,
            source_v,
            target_stage,
            master_plan=deepcopy(projection.get("master_plan") or {}),
            direction_audit=projection.get("direction_audit"),
            reviewer_feedback=str(projection.get("reviewer_feedback") or ""),
            worker_failure_count=int(projection.get("worker_failure_count") or 0),
            audit_context=audit_context,
            precommit_rework_count=int(
                projection.get("precommit_rework_count") or 0
            ),
            official_rework_count=int(
                projection.get("official_rework_count") or 0
            ),
            touch_stage_timestamp=True,
            expected_checkpoint_revision=int(
                contract.get("checkpoint_revision") or 0
            ),
            expected_checkpoint_stage=str(contract.get("checkpoint_stage") or ""),
            expected_workflow_run_id=str(contract.get("workflow_run_id") or ""),
            **checkpoint_kwargs,
        )
        if not written:
            return _json_tool_result({
                "error": "DURABLE_WORKER_FAILURE_PROJECTION_FAILED",
                "success": False,
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
            })
    evidence = projection.get("evidence") or {}
    if target_stage == "direction_audited":
        worker_workflow.supersede(
            "initial_worker_semantic_failure_requires_master_replan",
            evidence,
            stage=target_stage,
        )
    else:
        worker_workflow.failure_projected(target_stage)
    try:
        import logging
        _log = logging.getLogger("pok.planning_worker")
        _log.error(
            "Worker semantic failure projected: failure_class=%s boundary_errors=%s",
            "semantic",
            evidence.get("boundary_errors") or [],
        )
        import event_bus
        event_bus.emit(
            "pipeline.worker_semantic_failure_projected",
            "error",
            "Worker semantic failure projected",
            failure_class="semantic",
            boundary_errors=evidence.get("boundary_errors") or [],
            semantic_attempt=evidence.get("semantic_attempt"),
            next_v=next_v,
            source_v=source_v,
            next_stage=target_stage,
        )
    except Exception:
        pass
    return _json_tool_result({
        "success": False,
        "failure_class": "semantic",
        "next_v": next_v,
        "source_v": source_v,
        "next_stage": target_stage,
        "boundary_errors": evidence.get("boundary_errors") or [],
    })


async def _run_durable_worker_effect(
    worker_workflow,
    envelope,
    next_dir,
    worker_template,
):
    """Run exactly one fenced Worker activity from a frozen envelope."""
    from agent_workers import WorkerInfrastructureError
    from llm_availability import LLMAvailabilityBlocked
    from worker_boundary import (
        diff_file_snapshot,
        restore_complete_artifact_snapshot,
        snapshot_python_files,
    )

    next_v = int(envelope["next_v"])
    source_v = int(envelope["source_v"])
    tasks = deepcopy(envelope.get("tasks") or [])
    reviewer_feedback = str(envelope.get("reviewer_feedback") or "")
    policy = deepcopy(envelope.get("execution_policy") or {})
    contract = envelope.get("checkpoint_contract") or {}
    checkpoint = _matching_checkpoint(next_v, source_v)
    if not _durable_checkpoint_contract_matches(checkpoint, contract):
        worker_workflow.abandon("worker_checkpoint_contract_drift_before_claim")
        return _json_tool_result({
            "error": "DURABLE_WORKER_CHECKPOINT_CONTRACT_DRIFT",
            "success": False,
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
        })
    _eb = checkpoint.get("epoch_binding") or {}
    _source_inherited = bool(_eb.get("source_artifact_inherited", True))
    source_hash = (
        _complete_artifact_fingerprint(next_dir)
        if not _source_inherited
        else _complete_artifact_fingerprint(get_bot_dir(source_v))
    )
    if source_hash != str(envelope.get("source_artifact_hash") or ""):
        worker_workflow.abandon("worker_source_artifact_drift_before_claim")
        return _json_tool_result({
            "error": "DURABLE_WORKER_SOURCE_ARTIFACT_DRIFT",
            "success": False,
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
            "expected_source_hash": envelope.get("source_artifact_hash"),
            "current_source_hash": source_hash,
        })

    _worker_uses_llm = policy.get("executor") != "system_policy_bootstrap_v1"
    if _worker_uses_llm:
        try:
            from llm_availability_store import active_llm_pause

            active_pause = active_llm_pause()
        except Exception as exc:
            return _json_tool_result({
                "error": "LLM_AVAILABILITY_STATE_INVALID",
                "success": False,
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": (
                    "The provider pause record is invalid. No Worker effect "
                    "was claimed."
                ),
            })
        if active_pause is not None:
            state = worker_workflow.state()
            return _json_tool_result({
                "error": "LLM_AVAILABILITY_BLOCKED",
                "success": False,
                "failure_class": "availability",
                "action": "wait_for_llm_availability",
                "next_v": next_v,
                "source_v": source_v,
                "worker_status": state.get("status"),
                "attempt": int(state.get("attempt") or 0),
                "max_attempts": int(state.get("max_attempts") or 0),
                "effect_id": state.get("effect_id"),
                "availability": active_pause,
                "directive": (
                    "The provider pause became active before lease claim. No "
                    "Worker attempt was consumed."
                ),
            })

    lease_owner = f"pid:{os.getpid()}"
    try:
        lease = worker_workflow.request_or_claim(
            owner=lease_owner,
            lease_seconds=3600,
        )
    except Exception as exc:
        return _json_tool_result({
            "error": "DURABLE_WORKER_EFFECT_CLAIM_FAILED",
            "failure_class": "infrastructure",
            "action": "retry_same_tool",
            "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            "next_v": next_v,
            "source_v": source_v,
        })

    workspace = None
    availability_defer_failed = False
    operator_shutdown_observed = False
    try:
        if _worker_uses_llm:
            try:
                from llm_availability_store import active_llm_pause

                active_pause = active_llm_pause()
            except Exception as exc:
                with worker_workflow.store.command_lock(
                    worker_workflow.run_id,
                    blocking=True,
                ):
                    worker_workflow.availability_deferred(
                        lease,
                        {
                            "schema_version": 1,
                            "active": True,
                            "category": "availability_control_invalid",
                            "summary": (
                                "provider pause state could not be read after claim"
                            ),
                            "evidence_digest": hashlib.sha256(
                                (
                                    f"{type(exc).__name__}:"
                                    f"{str(exc)[:300]}"
                                ).encode("utf-8")
                            ).hexdigest(),
                            "persistence_error": (
                                f"{type(exc).__name__}: {str(exc)[:300]}"
                            ),
                        },
                    )
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_STATE_INVALID",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "effect_id": lease.effect_id,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        worker_workflow.state().get("attempt") or 0
                    ),
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
            if active_pause is not None:
                with worker_workflow.store.command_lock(
                    worker_workflow.run_id,
                    blocking=True,
                ):
                    deferred_state = worker_workflow.availability_deferred(
                        lease,
                        active_pause,
                    )
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_BLOCKED",
                    "success": False,
                    "failure_class": "availability",
                    "action": "wait_for_llm_availability",
                    "next_v": next_v,
                    "source_v": source_v,
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        deferred_state.get("attempt") or 0
                    ),
                    "max_attempts": lease.max_attempts,
                    "availability": active_pause,
                    "directive": (
                        "The provider pause became active at the claim boundary. "
                        "The lease was deferred without consuming an attempt."
                    ),
                })
        workspace = worker_workflow.artifacts.workspace_for(
            lease,
            str(envelope.get("prepared_snapshot_hash") or ""),
        )
        task_skipper = None
        if policy.get("quality_skipper"):
            task_skipper = _quality_rework_skipper(
                workspace,
                get_bot_dir(source_v),
                next_v,
                source_v,
                expected_architecture_policy=policy.get(
                    "expected_architecture_policy"
                ),
                master_plan=deepcopy(envelope.get("projection_plan") or {}),
            )
        baseline = snapshot_python_files(workspace)
        ui = _get_ui()
        system_worker_receipt = None
        try:
            if policy.get("executor") == "system_policy_bootstrap_v1":
                from system_strict_bootstrap import (
                    apply_blueprint,
                    bind_worker_effect_receipt,
                )

                worker_snapshots, audit_focus_areas, system_worker_receipt = (
                    apply_blueprint(
                        workspace,
                        checkpoint=checkpoint,
                        envelope=envelope,
                    )
                )
                system_worker_receipt = bind_worker_effect_receipt(
                    system_worker_receipt,
                    effect_id=lease.effect_id,
                    lease_epoch=lease.lease_epoch,
                )
                success = True
                ui.log_history(
                    "Applied the content-bound strict-v1 consumer blueprint "
                    "without invoking an LLM Worker.",
                    "info",
                )
            else:
                success, worker_snapshots, audit_focus_areas = await _execute_workers(
                    tasks,
                    worker_template,
                    workspace,
                    next_v,
                    [],
                    ui,
                    reviewer_feedback=reviewer_feedback,
                    source_v=source_v,
                    force_sequential=bool(policy.get("force_sequential")),
                    task_skipper=task_skipper,
                    worker_effect_identity={
                        "workflow_run_id": str(
                            checkpoint.get("workflow_run_id") or ""
                        ),
                        "envelope_digest": str(
                            envelope.get("envelope_digest") or ""
                        ),
                        "effect_id": str(lease.effect_id),
                        "lease_epoch": int(lease.lease_epoch),
                    },
                )
        except BaseException as exc:
            rollback_error = ""
            try:
                restore_complete_artifact_snapshot(workspace, baseline)
            except BaseException as rollback_exc:
                rollback_error = (
                    f"{type(rollback_exc).__name__}: {str(rollback_exc)[:300]}"
                )
            # Only a contemporaneous cancellation plus the owner-fenced
            # process shutdown edge is attempt-neutral.  An unexpected Claude
            # SIGTERM is also surfaced as CancelledError, but with no shutdown
            # edge it continues through the ordinary failure path below.
            import asyncio as _asyncio
            from llm_query import is_operator_shutdown_requested

            if (
                isinstance(exc, _asyncio.CancelledError)
                and is_operator_shutdown_requested()
            ):
                operator_shutdown_observed = True
                try:
                    shutdown_deadline = time.monotonic() + 10.0
                    with worker_workflow.store.command_lock(
                        worker_workflow.run_id,
                        blocking=True,
                        deadline_monotonic=shutdown_deadline,
                    ):
                        interrupted_state = (
                            worker_workflow.operator_shutdown_interrupted(
                                lease,
                                owner=lease_owner,
                                deadline_monotonic=shutdown_deadline,
                            )
                        )
                except Exception as interrupt_exc:
                    return _json_tool_result({
                        "error": "WORKER_OPERATOR_SHUTDOWN_PERSIST_FAILED",
                        "success": False,
                        "failure_class": "control_plane",
                        "action": "operator_reconcile",
                        "recovery_blocked": True,
                        "checkpoint_preserved": True,
                        "attempt_neutral_persisted": False,
                        "next_v": next_v,
                        "source_v": source_v,
                        "workflow_run_id": worker_workflow.run_id,
                        "effect_id": lease.effect_id,
                        "lease_epoch": lease.lease_epoch,
                        "claimed_attempt": lease.attempt,
                        "message": (
                            f"{type(interrupt_exc).__name__}: "
                            f"{str(interrupt_exc)[:300]}"
                        ),
                        "rollback_error": rollback_error,
                        "validation_errors": [
                            "worker_operator_shutdown_receipt_not_durable"
                        ],
                        "directive": (
                            "The process shutdown edge was observed, but its exact "
                            "attempt-neutral Worker receipt was not durable. Preserve "
                            "the running lease and reconcile it; never translate this "
                            "ambiguity into EffectFailed or abandon the generation."
                        ),
                    })
                return _json_tool_result({
                    "error": "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED",
                    "success": False,
                    "failure_class": "operator_shutdown",
                    "action": "retry_same_tool",
                    "pending": True,
                    "shutdown_requested": True,
                    "checkpoint_preserved": True,
                    "attempt_consumed": False,
                    "attempt_neutral_persisted": True,
                    "next_v": next_v,
                    "source_v": source_v,
                    "workflow_run_id": worker_workflow.run_id,
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        interrupted_state.get("attempt") or 0
                    ),
                    "max_attempts": lease.max_attempts,
                    "rollback_error": rollback_error,
                    "directive": (
                        "The operator stopped this process. The exact Worker lease "
                        "was fenced and returned to the same frozen envelope without "
                        "consuming an attempt; a fresh process may claim it."
                    ),
                })
            if isinstance(exc, LLMAvailabilityBlocked):
                pause_state = exc.pause_state()
                # Fence and release the Worker lease *before* publishing the
                # cross-process pause.  If the process dies immediately after
                # the pause file is fsynced, replay already sees EffectDeferred
                # and the claim's attempt increment has been rolled back.
                try:
                    with worker_workflow.store.command_lock(
                        worker_workflow.run_id,
                        blocking=True,
                    ):
                        deferred_state = (
                            worker_workflow.availability_deferred(
                                lease,
                                pause_state,
                            )
                        )
                except Exception as defer_exc:
                    availability_defer_failed = True
                    return _json_tool_result({
                        "error": "WORKER_AVAILABILITY_DEFER_FAILED",
                        "success": False,
                        "failure_class": "control_plane",
                        "action": "operator_reconcile",
                        "next_v": next_v,
                        "source_v": source_v,
                        "message": (
                            f"{type(defer_exc).__name__}: "
                            f"{str(defer_exc)[:300]}"
                        ),
                        "persistence_error": "",
                        "rollback_error": rollback_error,
                        "directive": (
                            "The LLM availability pause could not be fenced into "
                            "the durable Worker journal. Do not classify or retry "
                            "it as a Worker infrastructure failure."
                        ),
                    })
                persistence_error = ""
                try:
                    from llm_availability_store import persist_llm_pause

                    pause_state = persist_llm_pause(pause_state)
                except Exception as pause_exc:
                    persistence_error = (
                        f"{type(pause_exc).__name__}: {str(pause_exc)[:300]}"
                    )
                    return _json_tool_result({
                        "error": "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
                        "success": False,
                        "failure_class": "control_plane",
                        "action": "operator_reconcile",
                        "next_v": next_v,
                        "source_v": source_v,
                        "effect_id": lease.effect_id,
                        "lease_epoch": lease.lease_epoch,
                        "claimed_attempt": lease.attempt,
                        "restored_attempt": int(
                            deferred_state.get("attempt") or 0
                        ),
                        "max_attempts": lease.max_attempts,
                        "availability": exc.pause_state(),
                        "persistence_error": persistence_error,
                        "rollback_error": rollback_error,
                        "directive": (
                            "The Worker lease is safely deferred and attempt-neutral, "
                            "but the global pause was not published. Reconcile the "
                            "pause record before resuming this exact effect."
                        ),
                    })
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_BLOCKED",
                    "success": False,
                    "failure_class": "availability",
                    "action": "wait_for_llm_availability",
                    "next_v": next_v,
                    "source_v": source_v,
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        deferred_state.get("attempt") or 0
                    ),
                    "max_attempts": lease.max_attempts,
                    "availability": pause_state,
                    "persistence_error": persistence_error,
                    "rollback_error": rollback_error,
                    "directive": (
                        "The provider is unavailable. The Worker lease was "
                        "released without consuming an attempt; resume only "
                        "through the content-bound LLM availability control."
                    ),
                })

            from system_strict_bootstrap import (
                SystemStrictBootstrapError,
            )

            if isinstance(exc, SystemStrictBootstrapError):
                try:
                    with worker_workflow.store.command_lock(worker_workflow.run_id):
                        worker_workflow.execution_failed(
                            lease,
                            list(exc.errors),
                            retryable=False,
                        )
                    worker_workflow.abandon(
                        "system_strict_bootstrap_execution_failed"
                    )
                except Exception:
                    pass
                return _json_tool_result({
                    "error": "SYSTEM_STRICT_BOOTSTRAP_EXECUTION_FAILED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "abandon_generation",
                    "next_v": next_v,
                    "source_v": source_v,
                    "validation_errors": list(exc.errors),
                    "rollback_error": rollback_error,
                    "directive": (
                        "The checked-in blueprint failed its exact workspace or output "
                        "identity. Abandon; never retry it as an LLM Worker."
                    ),
                })
            if isinstance(exc, WorkerInfrastructureError) and not rollback_error:
                with worker_workflow.store.command_lock(worker_workflow.run_id):
                    failed_state = worker_workflow.infrastructure_failed(
                        lease,
                        exc.issues,
                    )
                exhausted = failed_state.get("status") == "exhausted"
                if exhausted:
                    worker_workflow.abandon("worker_infrastructure_exhausted")
                return _json_tool_result({
                    **(
                        {"error": "WORKER_INFRASTRUCTURE_EXHAUSTED"}
                        if exhausted
                        else {}
                    ),
                    "success": False,
                    "failure_class": "infrastructure",
                    "action": (
                        "abandon_generation" if exhausted else "retry_same_tool"
                    ),
                    "attempt": lease.attempt,
                    "max_attempts": lease.max_attempts,
                    "attempt_key": envelope.get("envelope_digest"),
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "next_v": next_v,
                    "source_v": source_v,
                })
            issues = [
                f"{type(exc).__name__}: {str(exc)[:500]}",
                *( [f"rollback: {rollback_error}"] if rollback_error else [] ),
            ]
            with worker_workflow.store.command_lock(worker_workflow.run_id):
                failed_state = worker_workflow.execution_failed(
                    lease,
                    issues,
                    retryable=not bool(rollback_error),
                )
            if rollback_error or failed_state.get("status") == "exhausted":
                worker_workflow.abandon("worker_harness_failure")
            return _json_tool_result({
                "error": (
                    "WORKER_BATCH_EXCEPTION_ROLLBACK_FAILED"
                    if rollback_error
                    else "DURABLE_WORKER_HARNESS_FAILED"
                ),
                "success": False,
                "failure_class": "infrastructure",
                "action": (
                    "abandon_generation"
                    if rollback_error or failed_state.get("status") == "exhausted"
                    else "retry_same_tool"
                ),
                "next_v": next_v,
                "source_v": source_v,
                "message": "; ".join(issues),
            })

        boundary_errors = []
        policy_identity_refresh_receipt = None
        if success:
            changed = diff_file_snapshot(workspace, baseline)
            if not changed:
                success = False
                boundary_errors.append({"type": "worker_zero_artifact_changes"})
        if success:
            boundary_errors = _validate_worker_boundaries(
                tasks,
                source_v,
                next_v,
                worker_snapshots=worker_snapshots,
                candidate_dir=workspace,
                source_artifact_inherited=_source_inherited,
            )
            success = not boundary_errors
        if success:
            # The model-facing boundary has now proved that only policy.py was
            # candidate-written (the deterministic v143 bootstrap has already
            # proved its exact three-file blueprint separately).  Only after
            # that proof may the host remove compiler caches and rebuild the
            # two digest-bound identities.  Cache cleanup is host-owned because
            # the Worker is required to leave ``py_compile`` output in place.
            try:
                from bot_artifact import canonical_digest
                from bot_namespace import (
                    SYSTEM_DERIVED_IDENTITY_FILES,
                    refresh_policy_identity_documents,
                    strict_lineage_parent_versions,
                )

                pre_refresh_changed = sorted(changed)
                expected_pre_refresh = (
                    {"policy.py", *SYSTEM_DERIVED_IDENTITY_FILES}
                    if policy.get("executor") == "system_policy_bootstrap_v1"
                    else {"policy.py"}
                )
                if set(pre_refresh_changed) != expected_pre_refresh:
                    raise RuntimeError(
                        "candidate change set before identity refresh mismatch: "
                        f"expected={sorted(expected_pre_refresh)}:"
                        f"actual={pre_refresh_changed}"
                    )
                _cleanup_worker_transients_before_identity_refresh(workspace)
                lineage_parents = strict_lineage_parent_versions(
                    next_v,
                    source_v,
                    checkpoint.get("parent2_v"),
                )
                identity = refresh_policy_identity_documents(
                    workspace,
                    next_v,
                    parent_versions=lineage_parents,
                )
                final_changed = diff_file_snapshot(workspace, baseline)
                expected_final = {"policy.py", *SYSTEM_DERIVED_IDENTITY_FILES}
                if set(final_changed) != expected_final:
                    raise RuntimeError(
                        "final strict artifact delta mismatch: "
                        f"expected={sorted(expected_final)}:actual={final_changed}"
                    )
                receipt_subject = {
                    "schema_version": 1,
                    "kind": "strict-policy-identity-refresh-v1",
                    "version": next_v,
                    "parent_versions": list(lineage_parents),
                    "candidate_changed_files": ["policy.py"],
                    "system_derived_files": sorted(SYSTEM_DERIVED_IDENTITY_FILES),
                    "final_changed_files": final_changed,
                    "runtime_manifest_digest": identity[
                        "runtime_manifest_digest"
                    ],
                    "epoch_receipt_digest": identity["epoch_receipt_digest"],
                    "envelope_digest": envelope.get("envelope_digest"),
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                }
                policy_identity_refresh_receipt = {
                    **receipt_subject,
                    "receipt_digest": canonical_digest(receipt_subject),
                }
            except Exception as exc:
                rollback_error = ""
                try:
                    restore_complete_artifact_snapshot(workspace, baseline)
                except Exception as rollback_exc:
                    rollback_error = (
                        f"{type(rollback_exc).__name__}: "
                        f"{str(rollback_exc)[:300]}"
                    )
                issue = (
                    "system policy identity refresh failed: "
                    f"{type(exc).__name__}: {str(exc)[:500]}"
                )
                with worker_workflow.store.command_lock(worker_workflow.run_id):
                    failed_state = worker_workflow.execution_failed(
                        lease,
                        [issue, *([f"rollback: {rollback_error}"] if rollback_error else [])],
                        retryable=not bool(rollback_error),
                    )
                if rollback_error or failed_state.get("status") == "exhausted":
                    worker_workflow.abandon("system_policy_identity_refresh_failed")
                return _json_tool_result({
                    "error": "SYSTEM_POLICY_IDENTITY_REFRESH_FAILED",
                    "success": False,
                    "failure_class": "infrastructure",
                    "action": (
                        "abandon_generation"
                        if rollback_error or failed_state.get("status") == "exhausted"
                        else "retry_same_tool"
                    ),
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": issue,
                    "rollback_error": rollback_error,
                })
        if success:
            try:
                _clear_compiled_task_context(workspace)
            except Exception as exc:
                success = False
                boundary_errors.append({
                    "type": "transient_control_artifact_cleanup_failed",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })

        if not success:
            try:
                restore_complete_artifact_snapshot(workspace, baseline)
            except Exception as exc:
                worker_workflow.execution_failed(
                    lease,
                    [f"semantic rollback failed: {type(exc).__name__}: {exc}"],
                    retryable=False,
                )
                worker_workflow.abandon("worker_semantic_rollback_failed")
                return _json_tool_result({
                    "error": "WORKER_BATCH_ROLLBACK_FAILED",
                    "success": False,
                    "action": "abandon_generation",
                    "next_v": next_v,
                    "source_v": source_v,
                })
            evidence = {
                "boundary_errors": boundary_errors,
                "audit_focus_areas": audit_focus_areas,
                "worker_reported_success": False,
            }
            target_stage = (
                "repair_planned" if reviewer_feedback else "direction_audited"
            )
            next_failure_count = int(envelope.get("worker_failure_count") or 0) + 1
            audit_context = deepcopy(envelope.get("audit_context") or {})
            failure_plan = (
                deepcopy(envelope.get("projection_plan") or {})
                if reviewer_feedback
                else {}
            )
            if not reviewer_feedback:
                audit_context["worker_execution_failed_replan"] = {
                    "failed_tasks": [
                        {
                            "worker_id": task.get("worker_id"),
                            "role": task.get("role"),
                            "target_files": task.get("target_files", []),
                        }
                        for task in tasks[:5]
                    ],
                    "worker_failure_count": next_failure_count,
                }
            failure_projection = {
                "schema_version": 1,
                "stage": target_stage,
                "checkpoint_contract": deepcopy(contract),
                "master_plan": failure_plan,
                "direction_audit": checkpoint.get("direction_audit"),
                "reviewer_feedback": reviewer_feedback,
                "worker_failure_count": next_failure_count,
                "audit_context": audit_context,
                "precommit_rework_count": int(
                    envelope.get("precommit_rework_count") or 0
                ),
                "official_rework_count": int(
                    envelope.get("official_rework_count") or 0
                ),
                "runtime_contract_ledger_digest": (
                    _checkpoint_runtime_contract_ledger_digest(checkpoint)
                    if target_stage == "direction_audited"
                    and checkpoint.get("runtime_contract_ledger") is not None
                    else ""
                ),
                "evidence": evidence,
                "durable_worker_failure": {
                    "envelope_digest": envelope.get("envelope_digest"),
                    "semantic_attempt": int(
                        worker_workflow.state().get("semantic_attempt") or 0
                    ) + 1,
                },
            }
            with worker_workflow.store.command_lock(worker_workflow.run_id):
                semantic_state = worker_workflow.semantic_failed(
                    lease,
                    evidence,
                    projection=failure_projection,
                )
                return await _project_durable_worker_failure(
                    worker_workflow,
                    semantic_state,
                )

        try:
            artifact_hash = _complete_artifact_fingerprint(workspace)
            snapshot_hash = worker_workflow.artifacts.capture(workspace)
            if not artifact_hash or artifact_hash != snapshot_hash:
                raise RuntimeError("Worker output snapshot mismatch")
        except Exception as exc:
            worker_workflow.execution_failed(
                lease,
                [f"output capture failed: {type(exc).__name__}: {exc}"],
                retryable=True,
            )
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_CAPTURE_FAILED",
                "success": False,
                "failure_class": "infrastructure",
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
            })
        audit_context = deepcopy(envelope.get("audit_context") or {})
        if audit_focus_areas:
            audit_context["worker_cot_focus_areas"] = audit_focus_areas
        if system_worker_receipt is not None:
            audit_context["system_strict_bootstrap_worker"] = (
                system_worker_receipt
            )
        if policy_identity_refresh_receipt is not None:
            policy_identity_refresh_receipt = {
                **policy_identity_refresh_receipt,
                "output_artifact_hash": artifact_hash,
            }
            from bot_artifact import canonical_digest

            policy_identity_refresh_receipt["receipt_digest"] = canonical_digest({
                key: value
                for key, value in policy_identity_refresh_receipt.items()
                if key != "receipt_digest"
            })
            audit_context["strict_policy_identity_refresh"] = (
                policy_identity_refresh_receipt
            )
        projection = {
            "schema_version": 1,
            "checkpoint_contract": deepcopy(contract),
            "master_plan": deepcopy(envelope.get("projection_plan") or {}),
            "reviewer_feedback": reviewer_feedback,
            "worker_failure_count": int(envelope.get("worker_failure_count") or 0),
            "audit_context": audit_context,
            "precommit_rework_count": int(
                envelope.get("precommit_rework_count") or 0
            ),
            "official_rework_count": int(
                envelope.get("official_rework_count") or 0
            ),
            "durable_worker_output": {
                "artifact_hash": artifact_hash,
                "snapshot_hash": snapshot_hash,
                "envelope_digest": envelope.get("envelope_digest"),
                "effect_id": lease.effect_id,
                "lease_epoch": lease.lease_epoch,
            },
        }
        try:
            with worker_workflow.store.command_lock(worker_workflow.run_id):
                output_state = worker_workflow.output_ready(
                    lease,
                    artifact_hash=artifact_hash,
                    snapshot_hash=snapshot_hash,
                    projection=projection,
                )
                return await _project_durable_worker_output(
                    worker_workflow,
                    next_dir,
                    output_state,
                )
        except Exception as exc:
            try:
                worker_workflow.execution_failed(
                    lease,
                    [f"output receipt failed: {type(exc).__name__}: {exc}"],
                    retryable=True,
                )
            except Exception:
                pass
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_RECEIPT_FAILED",
                "success": False,
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
            })
    finally:
        # Lease-outcome invariant: every path after claim must durably complete,
        # fail, exhaust, or abandon the effect. This guard covers injected
        # failures in workspace creation, validators, receipt construction, and
        # future hooks without relying on each branch remembering cleanup.
        try:
            effect = worker_workflow.store.effect(lease.effect_id)
            if (
                not availability_defer_failed
                and not operator_shutdown_observed
                and effect.get("status") == "running"
                and int(effect.get("lease_epoch") or 0) == int(lease.lease_epoch)
            ):
                worker_workflow.execution_failed(
                    lease,
                    ["Worker activity exited without a durable outcome"],
                    retryable=True,
                )
        except Exception:
            pass
        if workspace is not None:
            try:
                worker_workflow.artifacts.discard_workspace(workspace)
            except Exception:
                pass


def _worker_availability_resume_receipt_errors(deferred, pause_audit):
    """Validate the global resume receipt against the deferred Worker effect.

    The Worker journal is the authority for *which* provider failure suspended
    this effect.  Absence of an active global pause is therefore necessary but
    not sufficient to resume: the inactive audit record must prove that the
    same evidence was reconciled through the allowed manual/cooldown path.
    """
    errors = []
    if not isinstance(deferred, dict) or not deferred:
        return ["worker_deferred_availability_missing"]

    digest = str(deferred.get("evidence_digest") or "")
    category = str(deferred.get("category") or "")
    manual = bool(deferred.get("requires_manual_resume"))
    if len(digest) != 64 or any(
        ch not in "0123456789abcdef" for ch in digest.lower()
    ):
        errors.append("worker_deferred_evidence_digest_invalid")
    if not category:
        errors.append("worker_deferred_category_missing")
    if not isinstance(pause_audit, dict) or not pause_audit:
        errors.append("global_pause_resume_receipt_missing")
        return errors

    if pause_audit.get("active") is not False:
        errors.append("global_pause_resume_receipt_not_inactive")
    if str(pause_audit.get("source") or "") != "llm_availability":
        errors.append("global_pause_resume_receipt_source_invalid")
    for key in ("category", "evidence_digest", "retry_policy", "http_status"):
        if pause_audit.get(key) != deferred.get(key):
            errors.append(f"global_pause_resume_receipt_{key}_mismatch")
    if bool(pause_audit.get("requires_manual_resume")) != manual:
        errors.append("global_pause_resume_receipt_manual_policy_mismatch")
    if not str(pause_audit.get("resumed_at") or ""):
        errors.append("global_pause_resume_receipt_timestamp_missing")

    resume_source = str(pause_audit.get("resume_source") or "")
    resume_digest = str(pause_audit.get("resume_evidence_digest") or "")
    if manual:
        if resume_source != "operator_evidence_digest":
            errors.append("manual_pause_operator_receipt_missing")
        if resume_digest != digest:
            errors.append("manual_pause_resume_evidence_digest_mismatch")
    else:
        if resume_source != "bounded_cooldown_elapsed":
            errors.append("transient_pause_cooldown_receipt_missing")
        if resume_digest:
            errors.append("transient_pause_unexpected_operator_digest")
        if not str(pause_audit.get("auto_resume_at") or ""):
            errors.append("transient_pause_auto_resume_deadline_missing")
    return errors


@dataclass(frozen=True)
class _DeferredWorkerActivity:
    workflow: object
    envelope: dict
    next_dir: Path
    worker_template: str


async def _execute_workers_command(args, *, actor_lock_owned=False):
    _t0 = time.time()
    tasks = args.get("tasks", [])
    if not isinstance(tasks, list):
        return _json_tool_result({
            "error": "WORKER_TASKS_NOT_LIST",
            "directive": "Pass tasks=[] to load the checkpoint-owned Master plan.",
        })
    tasks_provided = bool(tasks)
    next_v = args.get("next_v")
    source_v = args.get("source_v")
    if next_v is None or source_v is None:
        next_v, source_v = _resolve_version_args(args)
    if next_v is None or source_v is None:
        return _json_tool_result({"error": "Missing next_v/source_v and no active checkpoint"})
    reviewer_feedback = args.get("reviewer_feedback", "")

    _set_pipeline_status(f"Executing workers for v{next_v}")

    next_dir = get_bot_dir(next_v)
    prompts_dir = PROJECT_ROOT / "web" / "core" / "prompts"
    worker_template = _load_worker_prompt_template(prompts_dir)

    ckpt = _matching_checkpoint(next_v, source_v)
    if not ckpt:
        return _state_blocked(
            "execute_workers requires a matching checkpoint from prepare_next_gen.",
            next_v,
            source_v,
        )
    checkpoint_tasks = _checkpoint_master_plan(ckpt).get("tasks", [])
    if not isinstance(checkpoint_tasks, list):
        checkpoint_tasks = []
    critic_refusal = _critic_advisory_rework_refusal(
        ckpt,
        [*checkpoint_tasks, *tasks],
        next_v,
        source_v,
    )
    if critic_refusal:
        return _json_tool_result(critic_refusal)
    _system_bootstrap_executor = False
    from system_strict_bootstrap import is_declared_native_bootstrap

    _declared_system_bootstrap = is_declared_native_bootstrap(ckpt)
    _system_initial_worker_stage = bool(
        ckpt.get("stage") == "master_planned" and not reviewer_feedback
    )
    if _declared_system_bootstrap and not _system_initial_worker_stage:
        return _json_tool_result({
            "error": "SYSTEM_STRICT_BOOTSTRAP_REWORK_FORBIDDEN",
            "success": False,
            "action": "abandon_generation",
            "failure_class": "control_plane",
            "next_v": next_v,
            "source_v": source_v,
            "stage": ckpt.get("stage"),
            "directive": (
                "A content-bound first-migration blueprint may run only once from "
                "master_planned. If quality, Review, Critic, or precommit rejects "
                "it, abandon and change the checked-in blueprint/control contract "
                "in a fresh generation; never fall back to an LLM repair Worker."
            ),
        })

    if _declared_system_bootstrap:
        from system_strict_bootstrap import validate_master_receipt

        _system_worker_errors = validate_master_receipt(
            ckpt,
            candidate_dir=next_dir,
            require_prepared_content=True,
        )
        if _system_worker_errors:
            return _json_tool_result({
                "error": "SYSTEM_STRICT_BOOTSTRAP_WORKER_AUTHORITY_INVALID",
                "success": False,
                "action": "abandon_generation",
                "failure_class": "control_plane",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": _system_worker_errors,
                "directive": (
                    "The fresh-bootstrap system receipt or prepared artifact drifted. "
                    "Abandon this generation; never fall back to an LLM Worker."
                ),
            })
        _system_bootstrap_executor = True
    if (
        not str(ckpt.get("workflow_run_id") or "").strip()
        or int(ckpt.get("checkpoint_revision") or 0) < 1
    ):
        return _json_tool_result({
            "error": "STALE_WORKFLOW_ID_UNSUPPORTED",
            "failure_class": "state_migration",
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
            "directive": (
                "This active checkpoint predates the immutable generation actor "
                "identity. Abandon it while the runtime is stopped and prepare a "
                "new generation; do not migrate a half-executed workflow."
            ),
        })
    _worker_infra, _worker_infra_error = _owned_infrastructure_failure(
        ckpt,
        "execute_workers",
    )
    if _worker_infra_error:
        infra_route = route_policy(ckpt)
        return _state_blocked(
            _worker_infra_error + f"; next tool is {infra_route.get('next_tool')}",
            next_v,
            source_v,
            checkpoint=ckpt,
        )
    from worker_workflow import (
        WorkerWorkflow,
        next_worker_command,
        validate_worker_envelope,
    )

    worker_workflow = WorkerWorkflow.for_checkpoint(ckpt)
    if _worker_infra is not None:
        return _json_tool_result({
            "error": "STALE_WORKER_INFRASTRUCTURE_STATE_UNSUPPORTED",
            "failure_class": "state_migration",
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
            "directive": (
                "This generation was created by the retired Worker overlay state "
                "machine. Abandon it from the stopped runtime and start from a new "
                "baseline; do not translate two authorities into one history."
            ),
        })
    durable_worker_state = worker_workflow.state()
    durable_worker_status = str(durable_worker_state.get("status") or "idle")
    if durable_worker_status == "completed":
        previous_envelope = durable_worker_state.get("envelope") or {}
        previous_contract = previous_envelope.get("checkpoint_contract") or {}
        current_revision = int(ckpt.get("checkpoint_revision") or 0)
        previous_revision = int(previous_contract.get("checkpoint_revision") or 0)
        worker_entry_stages = {
            "master_planned",
            "quality_failed",
            "quality_passed",
            "reviewed",
            "critic_checked",
            "precommit_failed",
            "official_failed",
            "repair_planned",
            "rework_running",
        }
        if (
            ckpt.get("stage") in worker_entry_stages
            and current_revision > previous_revision
            and route_policy(ckpt).get("next_tool") == "execute_workers"
        ):
            work_receipt = hashlib.sha256(
                json.dumps(
                    {
                        "workflow_run_id": ckpt.get("workflow_run_id"),
                        "checkpoint_revision": current_revision,
                        "stage": ckpt.get("stage"),
                        "master_plan": ckpt.get("master_plan") or {},
                        "reviewer_feedback": ckpt.get("reviewer_feedback") or "",
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            durable_worker_state = worker_workflow.open_cycle(
                f"checkpoint_work_receipt:{work_receipt}"
            )
            durable_worker_status = "idle"
    durable_worker_envelope = (
        durable_worker_state.get("envelope")
        if isinstance(durable_worker_state.get("envelope"), dict)
        else {}
    )
    worker_command = next_worker_command(durable_worker_state)
    command_name = str(worker_command.get("command") or "recover")
    if command_name == "reconcile_abandon":
        return _json_tool_result({
            "error": "WORKER_WORKFLOW_ABANDONED",
            "success": False,
            "failure_class": "infrastructure",
            "action": "abandon_generation",
            "worker_abandon_reason": str(
                worker_command.get("reason") or "worker_abandoned"
            ),
            "next_v": next_v,
            "source_v": source_v,
            "stage": ckpt.get("stage"),
            "directive": (
                "The durable Worker journal is terminal while the outer "
                "checkpoint is still active. Reconcile by centrally abandoning "
                "this generation; never reopen or recreate the exhausted effect."
            ),
        })
    durable_worker_resume = command_name != "prepare"
    if durable_worker_resume and durable_worker_envelope:
        envelope_errors = validate_worker_envelope(durable_worker_envelope)
        if envelope_errors:
            worker_workflow.abandon("durable_worker_envelope_invalid")
            return _json_tool_result({
                "error": "DURABLE_WORKER_ENVELOPE_INVALID",
                "validation_errors": envelope_errors,
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
            })
        if (
            int(durable_worker_envelope.get("next_v")) != int(next_v)
            or int(durable_worker_envelope.get("source_v")) != int(source_v)
        ):
            worker_workflow.abandon("durable_worker_identity_mismatch")
            return _json_tool_result({
                "error": "DURABLE_WORKER_IDENTITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
            })
        current_template_hash = hashlib.sha256(
            worker_template.encode("utf-8")
        ).hexdigest()
        if (
            durable_worker_envelope.get("worker_template_hash")
            != current_template_hash
            or durable_worker_envelope.get("backend_contract")
            != _expected_worker_backend_contract(
                ckpt,
                durable_worker_envelope,
            )
        ):
            worker_workflow.abandon("durable_worker_definition_drift")
            return _json_tool_result({
                "error": "DURABLE_WORKER_DEFINITION_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
            })
    _worker_uses_llm = bool(
        (durable_worker_envelope.get("execution_policy") or {}).get(
            "executor"
        )
        != "system_policy_bootstrap_v1"
    )
    if (
        _worker_uses_llm
        and command_name in {
            "request_or_claim_worker",
            "claim_worker",
            "wait_for_llm_availability",
        }
    ):
        try:
            from llm_availability_store import active_llm_pause, load_llm_pause

            _active_pause = active_llm_pause()
            _pause_audit = load_llm_pause()
        except Exception as exc:
            return _json_tool_result({
                "error": "LLM_AVAILABILITY_STATE_INVALID",
                "success": False,
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "worker_status": durable_worker_status,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": (
                    "The durable provider pause record could not be validated. "
                    "Do not claim or fail the Worker effect until that control "
                    "record is reconciled."
                ),
            })
        if _active_pause is not None:
            return _json_tool_result({
                "error": "LLM_AVAILABILITY_BLOCKED",
                "success": False,
                "failure_class": "availability",
                "action": "wait_for_llm_availability",
                "next_v": next_v,
                "source_v": source_v,
                "worker_status": durable_worker_status,
                "attempt": int(durable_worker_state.get("attempt") or 0),
                "max_attempts": int(
                    durable_worker_state.get("max_attempts") or 0
                ),
                "effect_id": durable_worker_state.get("effect_id"),
                "availability": _active_pause,
                "directive": (
                    "The provider pause is still active. No Worker effect was "
                    "claimed and no attempt was consumed."
                ),
            })
        if command_name == "wait_for_llm_availability":
            _deferred_availability = (
                durable_worker_state.get("availability") or {}
            )
            if _deferred_availability.get("persistence_error"):
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "attempt": int(
                        durable_worker_state.get("attempt") or 0
                    ),
                    "effect_id": durable_worker_state.get("effect_id"),
                    "availability": _deferred_availability,
                    "directive": (
                        "The Worker lease was safely deferred, but the global "
                        "pause write failed. Preserve the attempt-neutral effect "
                        "and reconcile the pause record before resuming."
                    ),
                })
            _resume_receipt_errors = _worker_availability_resume_receipt_errors(
                _deferred_availability,
                _pause_audit,
            )
            if _resume_receipt_errors:
                return _json_tool_result({
                    "error": "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "attempt": int(
                        durable_worker_state.get("attempt") or 0
                    ),
                    "effect_id": durable_worker_state.get("effect_id"),
                    "receipt_errors": _resume_receipt_errors,
                    "availability": _deferred_availability,
                    "directive": (
                        "The global pause is not active, but no matching durable "
                        "resume receipt authorizes this deferred Worker effect. "
                        "Preserve the attempt-neutral journal and reconcile the "
                        "exact evidence digest before resuming."
                    ),
                })
            try:
                if actor_lock_owned:
                    durable_worker_state = (
                        worker_workflow.resume_availability_deferred()
                    )
                else:
                    with worker_workflow.store.command_lock(
                        worker_workflow.run_id
                    ):
                        durable_worker_state = (
                            worker_workflow.resume_availability_deferred()
                        )
            except Exception as exc:
                return _json_tool_result({
                    "error": "WORKER_AVAILABILITY_RESUME_FAILED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "directive": (
                        "The provider pause cleared, but its fenced Worker effect "
                        "could not transition back to requested. Do not recreate "
                        "or fail the effect."
                    ),
                })
            durable_worker_status = str(
                durable_worker_state.get("status") or "requested"
            )
            worker_command = next_worker_command(durable_worker_state)
            command_name = str(
                worker_command.get("command") or "recover"
            )
            if command_name != "claim_worker":
                return _json_tool_result({
                    "error": "WORKER_AVAILABILITY_RESUME_INVARIANT_FAILED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "next_command": command_name,
                })
    if command_name == "project_output":
        if actor_lock_owned:
            return await _project_durable_worker_output(
                worker_workflow,
                next_dir,
                durable_worker_state,
            )
        with worker_workflow.store.command_lock(worker_workflow.run_id):
            return await _project_durable_worker_output(
                worker_workflow,
                next_dir,
                durable_worker_state,
            )
    if command_name == "project_failure":
        if actor_lock_owned:
            return await _project_durable_worker_failure(
                worker_workflow,
                durable_worker_state,
            )
        with worker_workflow.store.command_lock(worker_workflow.run_id):
            return await _project_durable_worker_failure(
                worker_workflow,
                durable_worker_state,
            )
    if command_name in {"request_or_claim_worker", "claim_worker"}:
        if actor_lock_owned:
            return _DeferredWorkerActivity(
                workflow=worker_workflow,
                envelope=durable_worker_envelope,
                next_dir=next_dir,
                worker_template=worker_template,
            )
        return await _run_durable_worker_effect(
            worker_workflow,
            durable_worker_envelope,
            next_dir,
            worker_template,
        )
    if command_name == "abandon":
        worker_workflow.abandon("worker_infrastructure_exhausted")
        return _json_tool_result({
            "error": "WORKER_INFRASTRUCTURE_EXHAUSTED",
            "failure_class": "infrastructure",
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
        })
    if command_name == "none":
        return _json_tool_result({
            "error": "WORKER_CYCLE_HAS_NO_PENDING_COMMAND",
            "next_v": next_v,
            "source_v": source_v,
            "stage": ckpt.get("stage"),
            "projected_stage": durable_worker_state.get("projected_stage"),
            "next_tool": route_policy(ckpt).get("next_tool"),
        })
    if _checkpoint_architecture_policy_identity_errors(ckpt):
        if _is_fresh_empty_pool_bootstrap(ckpt):
            return _json_tool_result({
                "error": "FIRST_STRICT_ARCHITECTURE_POLICY_IDENTITY_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
                "directive": (
                    "The fresh first-strict architecture identity drifted. "
                    "Abandon and rematerialize the system blueprint; never "
                    "recover it from numeric high-water source bytes."
                ),
            })
        identity_errors = _checkpoint_architecture_policy_identity_errors(ckpt)
        identity_fingerprint = _identity_replan_fingerprint(identity_errors)
        identity_history = _identity_replan_counts(ckpt)
        identity_consecutive = _identity_replan_consecutive_count(
            identity_history, identity_fingerprint
        )
        if identity_consecutive >= IDENTITY_REPLAN_ABANDON_THRESHOLD:
            log_system_event(
                "pipeline.architecture_policy_identity_replan_abandoned",
                "error",
                (
                    f"Abandoning v{next_v}: identical architecture policy "
                    f"identity error recurred {identity_consecutive} times "
                    f"without progress; recovery is unable to resolve it."
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": ckpt.get("stage"),
                    "identity_errors": identity_errors,
                    "consecutive_count": identity_consecutive,
                    "threshold": IDENTITY_REPLAN_ABANDON_THRESHOLD,
                },
            )
            return _json_tool_result({
                "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_EXHAUSTED",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
                "failure_class": "deterministic",
                "consecutive_count": identity_consecutive,
                "threshold": IDENTITY_REPLAN_ABANDON_THRESHOLD,
                "identity_errors": identity_errors,
                "directive": (
                    "The same architecture policy identity error survived "
                    "multiple replan attempts. Recovery cannot fix a frozen "
                    "vs. recomputed mismatch deterministically; abandon this "
                    "generation and let the planner rebuild on the current "
                    "policy code, or escalate the identity comparator."
                ),
            })
        updated_history = _record_identity_replan_attempt(ckpt, identity_fingerprint)
        # Persist the circuit-breaker counter before recovery runs, so a crash
        # or stage rewrite inside recovery cannot lose the attempt record.
        # Stage is preserved; only the identity_replan_history field advances.
        try:
            write_pipeline_checkpoint(
                next_v,
                source_v,
                ckpt.get("stage"),
                identity_replan_history=updated_history,
            )
        except Exception:
            # Counter persistence is best-effort; the in-memory ckpt copy still
            # carries the update through this call's recovery path.
            pass
        try:
            recovery = _recover_architecture_policy_identity(
                ckpt,
                next_dir,
                get_bot_dir(source_v),
            )
        except Exception as exc:
            log_system_event(
                "pipeline.architecture_policy_identity_replan_failed",
                "error",
                f"Could not reset stale-policy candidate v{next_v}: {type(exc).__name__}: {exc}",
                {"next_v": next_v, "source_v": source_v, "stage": ckpt.get("stage")},
            )
            return _json_tool_result({
                "error": "ARCHITECTURE_POLICY_IDENTITY_RECOVERY_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": "Do not run bot workers; repair checkpoint/source synchronization first.",
            })
        if recovery is not None:
            return recovery
    if ckpt.get("stage") == "master_planned":
        from prepared_baseline_contract import validate_prepared_artifact_contract

        prepared_artifact_contract = (
            (ckpt.get("audit_context") or {}).get("prepared_artifact_contract")
        )
        prepared_artifact_errors = validate_prepared_artifact_contract(
            prepared_artifact_contract,
            prepared_dir=next_dir,
            source_v=source_v,
            next_v=next_v,
            verify_live_content=True,
        )
        if prepared_artifact_errors:
            return _json_tool_result({
                "error": "PREPARED_ARTIFACT_DRIFT_BEFORE_WORKERS",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": prepared_artifact_errors,
                "next_tool": "abandon_generation",
                "directive": (
                    "The candidate changed after Master accepted the frozen prepared "
                    "baseline but before Workers. Abandon and restart; do not grant "
                    "the drift a repair scope."
                ),
            })
    rework_stages = {"quality_failed", "precommit_failed", "official_failed", "repair_planned", "rework_running"}
    checkpoint_work_item = (
        durable_worker_envelope.get("work_item")
        if durable_worker_resume
        and isinstance(durable_worker_envelope.get("work_item"), dict)
        else _checkpoint_master_plan(ckpt).get("work_item")
        if isinstance(_checkpoint_master_plan(ckpt).get("work_item"), dict)
        else {}
    )
    checkpoint_has_frozen_preparation = bool(
        isinstance(checkpoint_work_item, dict)
        and checkpoint_work_item.get("repair_baseline_artifact_hash")
        and checkpoint_work_item.get("prepared_snapshot_hash")
    )
    frozen_rework_resume = bool(
        durable_worker_resume
        and durable_worker_envelope.get("kind") != "initial_worker"
        or (
            ckpt.get("stage") in {"repair_planned", "rework_running"}
            and checkpoint_work_item.get("repair_baseline_artifact_hash")
        )
    )
    prepared_repair_resume_dir = None
    prepared_repair_resume_hash = ""
    if (
        durable_worker_status == "idle"
        and ckpt.get("stage") in {"repair_planned", "rework_running"}
        and isinstance(checkpoint_work_item, dict)
    ):
        prepared_repair_resume_hash = str(
            checkpoint_work_item.get("prepared_snapshot_hash") or ""
        )
        if (
            checkpoint_work_item.get("repair_baseline_artifact_hash")
            and not prepared_repair_resume_hash
        ):
            return _json_tool_result({
                "error": "DURABLE_REPAIR_PREPARATION_RECEIPT_MISSING",
                "failure_class": "state_migration",
                "action": "abandon_generation",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "A repair work item claims a prepared baseline but does not "
                    "bind its immutable snapshot. Do not reconstruct or rerun "
                    "one-time preparation from mutable candidate bytes."
                ),
            })
        if prepared_repair_resume_hash:
            try:
                prepared_repair_resume_dir = worker_workflow.artifacts.path_for(
                    prepared_repair_resume_hash
                )
            except Exception:
                prepared_repair_resume_dir = None
    if ckpt.get("stage") in rework_stages:
        expected_repair_baseline = _checkpoint_repair_baseline_fingerprint(ckpt)
        # Once repair preparation has been captured and projected into the
        # checkpoint, that immutable artifact is the recovery authority.  The
        # canonical candidate intentionally still contains the pre-preparation
        # bytes, so comparing it here would turn a crash between checkpoint
        # publication and WorkerPrepared into a false drift/abandon.
        current_repair_baseline = _complete_artifact_fingerprint(
            prepared_repair_resume_dir
            if prepared_repair_resume_dir is not None
            else next_dir
        )
        if not expected_repair_baseline:
            return _json_tool_result({
                "error": "REPAIR_BASELINE_RECEIPT_MISSING",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "next_tool": "abandon_generation",
                "directive": (
                    "The failed gate/repair plan does not bind the exact complete "
                    "candidate artifact. Abandon; do not infer repair authority "
                    "from file paths or the live diff."
                ),
            })
        if (
            not current_repair_baseline
            or current_repair_baseline != expected_repair_baseline
        ):
            abandon_result = {}
            if frozen_rework_resume:
                abandon_result = await _force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "frozen_rework_baseline_drift",
                    actor_lock_owned=actor_lock_owned,
                )
            else:
                # See REWORK_TASK_AUTHORITY_INVALID: a non-frozen rework stage with a
                # drifted repair baseline cannot be repaired and must be abandoned in
                # tool, or the deterministic router loops on execute_workers by stage.
                abandon_result = await _force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "worker_terminal_abandon_repair_baseline_drift",
                    actor_lock_owned=actor_lock_owned,
                )
            return _json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "expected_artifact_hash": expected_repair_baseline,
                "current_artifact_hash": current_repair_baseline,
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The candidate changed after the gate evidence or repair plan "
                    "was frozen. Abandon; the drift cannot piggyback on a declared "
                    "repair file."
                ),
            })
        canonical_feedback = (
            str(durable_worker_envelope.get("reviewer_feedback") or "")
            if durable_worker_resume
            else _checkpoint_rework_feedback(ckpt)
        )
        if not canonical_feedback:
            abandon_result = await _force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "worker_terminal_abandon_rework_feedback_missing",
                actor_lock_owned=actor_lock_owned,
            )
            return _json_tool_result({
                "error": "REWORK_FEEDBACK_AUTHORITY_MISSING",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The checkpoint/gate receipt contains no canonical repair "
                    "feedback. Caller feedback cannot create repair authority."
                ),
            })
        if reviewer_feedback and not _transport_equivalent_feedback(
            reviewer_feedback,
            canonical_feedback,
        ):
            log_system_event(
                "pipeline.worker_rework_feedback_mismatch",
                "error",
                f"Rejected caller-rewritten rework feedback for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": ckpt.get("stage"),
                    "canonical_feedback_digest": hashlib.sha256(
                        canonical_feedback.encode("utf-8")
                    ).hexdigest(),
                    "supplied_feedback_digest": hashlib.sha256(
                        str(reviewer_feedback).encode("utf-8")
                    ).hexdigest(),
                },
            )
            abandon_result = await _force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "worker_terminal_abandon_rework_feedback_mismatch",
                actor_lock_owned=actor_lock_owned,
            )
            return _json_tool_result({
                "error": "REWORK_FEEDBACK_AUTHORITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "Pass empty reviewer_feedback to load the checkpoint receipt, "
                    "or echo that receipt exactly. Caller-authored feedback cannot "
                    "add files, blockers, or repair instructions."
                ),
            })
        reviewer_feedback = canonical_feedback

        if frozen_rework_resume:
            authoritative_rework_tasks = deepcopy(
                durable_worker_envelope.get("tasks")
                if durable_worker_resume
                else _checkpoint_master_plan(ckpt).get("tasks") or []
            )
            authority_errors = _frozen_rework_task_authority_errors(
                ckpt,
                authoritative_rework_tasks,
            )
        else:
            authoritative_rework_tasks, authority_errors = (
                _authoritative_rework_tasks(
                    ckpt,
                    canonical_feedback,
                )
            )
        if authority_errors:
            abandon_result = {}
            if frozen_rework_resume:
                abandon_result = await _force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "frozen_rework_task_authority_invalid",
                    actor_lock_owned=actor_lock_owned,
                )
            else:
                # Non-frozen rework stage (quality_failed / precommit_failed /
                # official_failed / repair_planned / rework_running) whose checkpoint
                # or gate receipt cannot authorize any worker-writable repair task
                # (e.g. a system-owned precompute/architecture regression that maps
                # to no policy.py edit). Abandon here instead of returning a bare
                # REWORK_TASK_AUTHORITY_INVALID: the deterministic router dispatches
                # execute_workers purely by stage and would otherwise reschedule it
                # forever. The worker_terminal_abandon_ prefix is allowed for every
                # rework stage by forced_rules (pipeline_state.py), unlike the
                # frozen_rework_ prefix which quality_failed rejects.
                abandon_result = await _force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "worker_terminal_abandon_rework_task_authority_invalid",
                    actor_lock_owned=actor_lock_owned,
                )
            return _json_tool_result({
                "error": "REWORK_TASK_AUTHORITY_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "validation_errors": authority_errors,
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The system could not derive signed, file-scoped repair tasks "
                    "from the checkpoint/gate receipt. Do not execute caller tasks."
                ),
            })
        if tasks_provided and _canonical_tasks_digest(tasks) != _canonical_tasks_digest(
            authoritative_rework_tasks
        ):
            unsigned_workers = [
                str(task.get("worker_id") or f"task_{index}")
                for index, task in enumerate(tasks)
                if not _repair_contract_signature(task, next_v)
            ]
            log_system_event(
                "pipeline.worker_rework_task_authority_mismatch",
                "error",
                f"Rejected caller-rewritten rework tasks for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": ckpt.get("stage"),
                    "expected_digest": _canonical_tasks_digest(authoritative_rework_tasks),
                    "supplied_digest": _canonical_tasks_digest(tasks),
                    "unsigned_worker_ids": unsigned_workers,
                },
            )
            return _json_tool_result({
                "error": "REWORK_TASK_AUTHORITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "expected_digest": _canonical_tasks_digest(authoritative_rework_tasks),
                "supplied_digest": _canonical_tasks_digest(tasks),
                "unsigned_worker_ids": unsigned_workers,
                "next_tool": "abandon_generation",
                "directive": (
                    "Pass tasks=[] to load system-synthesized repair tasks, or echo "
                    "the exact canonical list. Extra, shortened, or unsigned tasks "
                    "cannot expand repair authority."
                ),
            })
        tasks = deepcopy(authoritative_rework_tasks)
    declared_scope_violations = _declared_scope_violation_files(
        ckpt,
        reviewer_feedback,
    )
    if declared_scope_violations:
        log_system_event(
            "pipeline.declared_scope_integrity_violation",
            "error",
            f"Refusing repair workers for v{next_v}: undeclared artifact edits",
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
                "violation_files": sorted(declared_scope_violations),
            },
        )
        return _json_tool_result({
            "error": "DECLARED_SCOPE_INTEGRITY_VIOLATION",
            "next_v": next_v,
            "source_v": source_v,
            "violation_files": sorted(declared_scope_violations),
            "next_tool": "abandon_generation",
            "directive": (
                "A failed diff cannot authorize itself through a repair ledger. "
                "Abandon this candidate and restart from a frozen prepared/source "
                "baseline with explicit Master task scope."
            ),
        })
    if not ckpt.get("master_plan") and ckpt.get("stage") not in rework_stages:
        return _json_tool_result({
            "error": "execute_workers requires a master plan. Call run_master first to produce a task plan.",
            "next_v": next_v,
            "source_v": source_v,
        })

    # Initial execution is owned by the accepted Master checkpoint.  The outer
    # orchestrator may echo that list (the MCP schema currently requires a tasks
    # argument) or pass [], but it cannot shorten/rewrite prompts, targets,
    # checks, or runtime contracts.  Rework stages use their separate,
    # deterministic synthesis/replacement routes below.
    if ckpt.get("stage") == "master_planned":
        if reviewer_feedback:
            log_system_event(
                "pipeline.worker_initial_feedback_rejected",
                "error",
                f"Rejected caller feedback on initial worker plan for v{next_v}",
                {"next_v": next_v, "source_v": source_v},
            )
            return _json_tool_result({
                "error": "WORKER_INITIAL_FEEDBACK_FORBIDDEN",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "Initial master_planned execution must use the checkpoint task "
                    "verbatim with empty reviewer_feedback. Feedback is accepted only "
                    "on an explicit review/quality/precommit rework route."
                ),
            })
        _authoritative_tasks = _checkpoint_master_plan(ckpt).get("tasks")
        _authority_errors = _checkpoint_master_task_authority_errors(
            ckpt,
            _authoritative_tasks,
        )
        if _authority_errors:
            log_system_event(
                "pipeline.worker_task_authority_invalid",
                "error",
                f"Checkpoint worker authority invalid for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "errors": _authority_errors,
                },
            )
            return _json_tool_result({
                "error": "WORKER_TASK_AUTHORITY_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": _authority_errors,
                "directive": (
                    "Do not execute workers. The accepted Master task/ledger "
                    "authority must be repaired or the generation abandoned."
                ),
            })
        if tasks_provided and tasks != _authoritative_tasks:
            _expected_digest = _canonical_tasks_digest(_authoritative_tasks)
            _supplied_digest = _canonical_tasks_digest(tasks)
            log_system_event(
                "pipeline.worker_task_plan_mismatch",
                "error",
                f"Rejected caller-rewritten worker tasks for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "expected_digest": _expected_digest,
                    "supplied_digest": _supplied_digest,
                    "expected_worker_ids": [
                        task.get("worker_id") for task in _authoritative_tasks
                        if isinstance(task, dict)
                    ],
                    "supplied_worker_ids": [
                        task.get("worker_id") for task in tasks if isinstance(task, dict)
                    ],
                },
            )
            return _json_tool_result({
                "error": "WORKER_TASK_PLAN_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "expected_digest": _expected_digest,
                "supplied_digest": _supplied_digest,
                "directive": (
                    "Pass tasks=[] to load the checkpoint-owned plan, or pass the "
                    "exact tasks returned by run_master. Do not paraphrase them."
                ),
            })
        if durable_worker_resume:
            durable_tasks = durable_worker_envelope.get("tasks") or []
            if _canonical_tasks_digest(durable_tasks) != _canonical_tasks_digest(
                _authoritative_tasks
            ):
                abandon_result = await _force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "durable_initial_worker_task_drift",
                    actor_lock_owned=actor_lock_owned,
                )
                worker_workflow.abandon("durable_initial_worker_task_drift")
                return _json_tool_result({
                    "error": "DURABLE_INITIAL_WORKER_TASK_DRIFT",
                    "next_v": next_v,
                    "source_v": source_v,
                    **abandon_result,
                })
            _authoritative_tasks = durable_tasks
        tasks = deepcopy(_authoritative_tasks)

    review_rework_checkpoint = _is_review_rework_checkpoint(ckpt)
    official_rework_checkpoint = _is_official_rework_checkpoint(ckpt)
    replace_checkpoint_tasks = ckpt.get("stage") in rework_stages

    if official_rework_checkpoint and not frozen_rework_resume:
        checkpoint_tasks = _checkpoint_master_plan(ckpt).get("tasks", [])
        supplied_tasks = tasks
        tasks = _official_repair_tasks(ckpt, reviewer_feedback)
        replace_checkpoint_tasks = True
        log_system_event(
            "pipeline.official_repair_tasks_forced",
            "warn",
            f"Replaced prior/supplied tasks with deterministic official repair for v{next_v}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
                "old_target_files": sorted(_task_target_filenames(checkpoint_tasks)),
                "supplied_target_files": sorted(_task_target_filenames(supplied_tasks)),
                "new_target_files": sorted(_task_target_filenames(tasks)),
                "worker_id": tasks[0].get("worker_id") if tasks else None,
            },
        )

    # If tasks are not provided, load them from the authoritative checkpoint.
    # Provider sessions are always fresh and never carry task authority in
    # remote conversation history.
    if not tasks:
        plan = _checkpoint_master_plan(ckpt)
        checkpoint_tasks = plan.get("tasks", [])
        precommit_stale_reason = (
            _precommit_repair_task_refresh_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if checkpoint_tasks and _is_precommit_rework_checkpoint(ckpt) else ""
        )
        review_stale_reason = (
            _review_repair_task_refresh_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if checkpoint_tasks and review_rework_checkpoint else ""
        )
        quality_stale_reason = (
            _stale_quality_task_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if (
                checkpoint_tasks
                and not _is_precommit_rework_checkpoint(ckpt)
                and not _is_official_rework_checkpoint(ckpt)
                and not review_rework_checkpoint
            ) else ""
        )
        if ckpt.get("stage") in rework_stages and (
            not checkpoint_tasks
            or quality_stale_reason
            or precommit_stale_reason
            or review_stale_reason
        ):
            tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
            if tasks:
                replace_checkpoint_tasks = bool(checkpoint_tasks)
                event_type = (
                    "pipeline.workers_tasks_refreshed"
                    if checkpoint_tasks else "pipeline.workers_tasks_synthesized"
                )
                if checkpoint_tasks and _is_precommit_rework_checkpoint(ckpt):
                    event_message = (
                        f"Refreshed precommit repair task(s) for v{next_v}: {precommit_stale_reason}"
                    )
                elif checkpoint_tasks and review_stale_reason:
                    event_message = (
                        f"Refreshed review repair task(s) for v{next_v}: {review_stale_reason}"
                    )
                elif quality_stale_reason:
                    event_message = (
                        f"Refreshed quality repair task(s) for v{next_v}: {quality_stale_reason}"
                    )
                else:
                    event_message = (
                        f"Synthesized {len(tasks)} rework task(s) for v{next_v} from checkpoint gate feedback"
                    )
                log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "parent2_v": ckpt.get("parent2_v"),
                        "old_target_files": sorted(_task_target_filenames(checkpoint_tasks)),
                        "new_target_files": sorted(_task_target_filenames(tasks)),
                        "refresh_reason": (
                            precommit_stale_reason
                            or review_stale_reason
                            or quality_stale_reason
                        ),
                        "num_tasks": len(tasks),
                        "task_kind": tasks[0].get("task_kind") if tasks else None,
                    },
                )
        elif checkpoint_tasks:
            tasks = checkpoint_tasks
            log_system_event("pipeline.workers_tasks_from_checkpoint", "info",
                             f"Tasks loaded from checkpoint for v{next_v} (LLM omitted tasks arg)",
                             {"next_v": next_v, "num_tasks": len(tasks)})
        else:
            return _json_tool_result({
                "error": "No tasks provided and checkpoint has no task plan. Call run_master first.",
                "next_v": next_v,
                "source_v": source_v,
                })
        if not tasks:
            return _json_tool_result({
                "error": "No tasks provided and checkpoint has no task plan. Call run_master first.",
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
            })

    if (
        not frozen_rework_resume
        and tasks
        and ckpt.get("stage") in {"quality_failed", "repair_planned", "rework_running"}
        and not _is_precommit_rework_checkpoint(ckpt)
        and not _is_official_rework_checkpoint(ckpt)
        and not review_rework_checkpoint
    ):
        failure_files = _quality_failure_target_files(ckpt, reviewer_feedback)
        task_files = _task_target_filenames(tasks)
        missing_files = sorted(failure_files - task_files)
        quality_stale_reason = _stale_quality_task_reason(tasks, ckpt, reviewer_feedback)
        if missing_files or quality_stale_reason:
            refreshed_tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
            if refreshed_tasks:
                tasks = refreshed_tasks
                replace_checkpoint_tasks = True
                refresh_reason = (
                    f"old task targets missed {missing_files}" if missing_files else quality_stale_reason
                )
                log_system_event(
                    "pipeline.workers_tasks_refreshed",
                    "warn",
                    f"Refreshed quality repair task(s) for v{next_v}; {refresh_reason}",
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "missing_files": missing_files,
                        "refresh_reason": quality_stale_reason,
                        "old_target_files": sorted(task_files),
                        "new_target_files": sorted(_task_target_filenames(refreshed_tasks)),
                        "num_tasks": len(refreshed_tasks),
                    },
                )

    if (
        not frozen_rework_resume
        and tasks
        and _is_precommit_rework_checkpoint(ckpt)
    ):
        precommit_stale_reason = _precommit_repair_task_refresh_reason(tasks, ckpt, reviewer_feedback)
    else:
        precommit_stale_reason = ""
    if tasks and _is_precommit_rework_checkpoint(ckpt) and precommit_stale_reason:
        refreshed_tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
        if refreshed_tasks:
            old_files = sorted(_task_target_filenames(tasks))
            tasks = refreshed_tasks
            replace_checkpoint_tasks = True
            log_system_event(
                "pipeline.workers_tasks_refreshed",
                "warn",
                f"Refreshed precommit repair task(s) for v{next_v}; {precommit_stale_reason}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_target_files": old_files,
                    "new_target_files": sorted(_task_target_filenames(refreshed_tasks)),
                    "num_tasks": len(refreshed_tasks),
                    "task_kind": refreshed_tasks[0].get("task_kind") if refreshed_tasks else None,
                    "refresh_reason": precommit_stale_reason,
                },
            )

    if not frozen_rework_resume and tasks and review_rework_checkpoint:
        review_stale_reason = _review_repair_task_refresh_reason(tasks, ckpt, reviewer_feedback)
    else:
        review_stale_reason = ""
    if tasks and review_rework_checkpoint and review_stale_reason:
        refreshed_tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
        if refreshed_tasks:
            old_files = sorted(_task_target_filenames(tasks))
            tasks = refreshed_tasks
            replace_checkpoint_tasks = True
            log_system_event(
                "pipeline.workers_tasks_refreshed",
                "warn",
                f"Refreshed review repair task(s) for v{next_v}; {review_stale_reason}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_target_files": old_files,
                    "new_target_files": sorted(_task_target_filenames(refreshed_tasks)),
                    "num_tasks": len(refreshed_tasks),
                    "task_kind": refreshed_tasks[0].get("task_kind") if refreshed_tasks else None,
                    "refresh_reason": review_stale_reason,
                },
            )

    if (
        not frozen_rework_resume
        and tasks
        and ckpt.get("stage") in rework_stages
        and not _is_precommit_rework_checkpoint(ckpt)
        and not _is_official_rework_checkpoint(ckpt)
        and not review_rework_checkpoint
    ):
        ordered_tasks = _order_quality_repair_tasks(tasks)
        old_order = [str(task.get("worker_id", idx + 1)) for idx, task in enumerate(tasks)]
        new_order = [str(task.get("worker_id", idx + 1)) for idx, task in enumerate(ordered_tasks)]
        if new_order != old_order:
            tasks = ordered_tasks
            replace_checkpoint_tasks = True
            log_system_event(
                "pipeline.quality_repair_tasks_reordered",
                "info",
                f"Reordered quality repair tasks for v{next_v}; file_size cleanup will run last",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_order": old_order,
                    "new_order": new_order,
                },
            )

    critic_refusal = _critic_advisory_rework_refusal(
        ckpt,
        tasks,
        next_v,
        source_v,
    )
    if critic_refusal:
        return _json_tool_result(critic_refusal)

    task_write_scope_errors = _task_write_scope_errors(tasks, next_v)
    if task_write_scope_errors:
        return _json_tool_result({
            "error": "WORKER_TASK_WRITE_SCOPE_INVALID",
            "next_v": next_v,
            "source_v": source_v,
            "validation_errors": task_write_scope_errors,
            "next_tool": "abandon_generation",
            "directive": (
                "must_change_files is a completion requirement, not write "
                "authority. Every required file must already be in "
                "target_files/files_allowed."
            ),
        })

    # B6 (2026-06-30): redundant-call guard. execute_workers is NOT idempotent —
    # a redundant call (no reviewer_feedback) when workers already ran resets code
    # from source + re-runs every Worker-LLM (the single most expensive pipeline
    # step), wasting cost and mutating already-gated code. Only allow a re-run when
    # there is reviewer_feedback (a legitimate retry-after-reviewer-reject). A pure
    # redundant call must be refused so the orchestrator proceeds to the next gate.
    _b6_stage = ckpt.get("stage")
    if (not reviewer_feedback
            and _b6_stage in ("workers_done", "quality_failed", "quality_passed", "reviewed", "critic_checked", "precommit_failed", "verified")):
        if _b6_stage == "precommit_failed":
            return _json_tool_result({
                "error": (
                    "Precommit failed, but execute_workers was called without reviewer_feedback. "
                    "Pass the exact precommit_eval directive/blockers as reviewer_feedback."
                ),
                "next_v": next_v,
                "source_v": source_v,
                "stage": _b6_stage,
                "intent": {
                    "kind": "rework",
                    "next_tool": "execute_workers",
                    "failure_class": "regression",
                    "authority": "tool:execute_workers",
                    "safe_to_auto_execute": False,
                },
            })
        try:
            log_system_event(
                "pipeline.workers_redundant_call_blocked", "warn",
                f"execute_workers called again for v{next_v} at stage={_b6_stage} with no "
                f"reviewer_feedback — refusing re-run (would reset code + waste Worker-LLM "
                f"cost). Proceed to the next gate instead.",
                {"next_v": next_v, "source_v": source_v, "stage": _b6_stage},
            )
        except Exception:
            pass
        return _json_tool_result({
            "info": (f"Workers already ran for v{next_v} (stage={_b6_stage}). The code is in place. "
                     f"Do NOT call execute_workers again — proceed to the next pipeline gate "
                     f"(run_quality_gates / run_review / run_critic / run_precommit_eval / commit_bot)."),
            "next_v": next_v,
            "source_v": source_v,
            "stage": _b6_stage,
            "redundant_call_blocked": True,
        })

    # Circuit breaker: limit total worker failures per generation
    # Backward compat: old checkpoints used worker_invocation_count instead of worker_failure_count
    failure_count = ckpt.get("worker_failure_count", ckpt.get("worker_invocation_count", 0))
    MAX_WORKER_FAILURES = 6
    if failure_count >= MAX_WORKER_FAILURES:
        try:
            log_system_event('pipeline.circuit_breaker', 'error',
                f'Circuit breaker: {failure_count} worker failures',
                {'next_v': next_v, 'source_v': source_v, 'failure_count': failure_count})
        except Exception:
            pass
        return _json_tool_result({
            "error": f"CIRCUIT BREAKER: {failure_count} worker failures already recorded this generation (max {MAX_WORKER_FAILURES}). Abandon this generation and start a new one.",
            "failure_count": failure_count,
            "next_v": next_v,
            "source_v": source_v,
        })

    # When retrying after workers already ran, actually reset code from source first.
    # Previous claim that code was reset was FALSE — now we actually do it.
    force_sequential_rework = False
    task_skipper = None
    quality_skipper_config = None
    rework_plan_metadata = None
    precommit_rework_count_for_write = None
    official_rework_count_for_write = None
    mechanical_trim_results = []
    rework_preparation_dir = None
    prepared_candidate_dir = next_dir
    durable_preparation_resume = False

    def rollback_rework_preparation():
        if rework_preparation_dir is None:
            return ""
        try:
            worker_workflow.artifacts.discard_workspace(
                rework_preparation_dir
            )
            return ""
        except Exception as rollback_exc:
            return f"{type(rollback_exc).__name__}: {str(rollback_exc)[:300]}"
    existing_prepared_work = (
        (_checkpoint_master_plan(ckpt).get("work_item") or {})
        if isinstance(_checkpoint_master_plan(ckpt).get("work_item"), dict)
        else {}
    )
    existing_prepared_snapshot = str(
        existing_prepared_work.get("prepared_snapshot_hash") or ""
    )
    if (
        durable_worker_status == "idle"
        and ckpt.get("stage") in {"repair_planned", "rework_running"}
        and existing_prepared_snapshot
    ):
        try:
            prepared_candidate_dir = worker_workflow.artifacts.path_for(
                existing_prepared_snapshot
            )
            expected_prepared_hash = str(
                existing_prepared_work.get("repair_baseline_artifact_hash") or ""
            )
            if (
                not expected_prepared_hash
                or _complete_artifact_fingerprint(prepared_candidate_dir)
                != expected_prepared_hash
            ):
                raise RuntimeError("prepared repair snapshot hash mismatch")
            durable_preparation_resume = True
            rework_plan_metadata = deepcopy(existing_prepared_work)
            frozen_worker_input = rework_plan_metadata.get(
                "frozen_worker_input"
            )
            frozen_worker_input_digest = str(
                rework_plan_metadata.get("frozen_worker_input_digest") or ""
            )
            projection_preimage_artifact_hash = str(
                rework_plan_metadata.get(
                    "projection_preimage_artifact_hash"
                )
                or ""
            )
            projection_preimage_snapshot_hash = str(
                rework_plan_metadata.get(
                    "projection_preimage_snapshot_hash"
                )
                or ""
            )
            if not isinstance(frozen_worker_input, dict):
                raise RuntimeError("frozen Worker preparation input missing")
            actual_frozen_input_digest = hashlib.sha256(
                json.dumps(
                    frozen_worker_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if actual_frozen_input_digest != frozen_worker_input_digest:
                raise RuntimeError("frozen Worker preparation input digest mismatch")
            if (
                frozen_worker_input.get("schema_version") != 4
                or frozen_worker_input.get("tasks") != tasks
                or str(frozen_worker_input.get("reviewer_feedback") or "")
                != reviewer_feedback
                or frozen_worker_input.get("worker_template_hash")
                != hashlib.sha256(worker_template.encode("utf-8")).hexdigest()
                or frozen_worker_input.get("backend_contract")
                != _worker_backend_contract()
                or "worker_execution_context" in frozen_worker_input
                or not projection_preimage_artifact_hash
                or not projection_preimage_snapshot_hash
                or frozen_worker_input.get(
                    "projection_preimage_artifact_hash"
                )
                != projection_preimage_artifact_hash
                or frozen_worker_input.get(
                    "projection_preimage_snapshot_hash"
                )
                != projection_preimage_snapshot_hash
            ):
                raise RuntimeError("frozen Worker preparation input contract drift")
            projection_preimage_dir = worker_workflow.artifacts.path_for(
                projection_preimage_snapshot_hash
            )
            if (
                _complete_artifact_fingerprint(projection_preimage_dir)
                != projection_preimage_artifact_hash
            ):
                raise RuntimeError("frozen Worker projection preimage mismatch")
            if (
                _complete_artifact_fingerprint(next_dir)
                != projection_preimage_artifact_hash
            ):
                raise RuntimeError("canonical Worker projection preimage drift")
            precommit_rework_count_for_write = int(
                ckpt.get("precommit_rework_count") or 0
            )
            official_rework_count_for_write = int(
                ckpt.get("official_rework_count") or 0
            )
            task_kinds = {
                str(task.get("task_kind") or "")
                for task in tasks
                if isinstance(task, dict)
            }
            if (
                "quality_repair" in str(
                    existing_prepared_work.get("kind") or ""
                )
                or any("quality_repair" in kind for kind in task_kinds)
            ) and not _is_precommit_rework_checkpoint(
                ckpt
            ) and not _is_official_rework_checkpoint(ckpt):
                force_sequential_rework = True
                quality_skipper_config = {
                    "source_dir": get_bot_dir(source_v),
                    "expected_architecture_policy": (
                        _checkpoint_master_plan(ckpt).get(
                            "architecture_policy"
                        )
                    ),
                    "master_plan": _checkpoint_master_plan(ckpt),
                }
        except Exception as exc:
            return _json_tool_result({
                "error": "DURABLE_REPAIR_PREPARATION_UNAVAILABLE",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
    if (
        frozen_rework_resume
        and reviewer_feedback
        and ckpt.get("stage") in {"repair_planned", "rework_running"}
    ):
        frozen_plan = _checkpoint_master_plan(ckpt)
        frozen_work_item = (
            frozen_plan.get("work_item")
            if isinstance(frozen_plan.get("work_item"), dict)
            else {}
        )
        frozen_rework_kind = str(frozen_work_item.get("kind") or "")
        frozen_task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        is_frozen_quality_rework = (
            "quality_repair" in frozen_rework_kind
            or any("quality_repair" in kind for kind in frozen_task_kinds)
        )
        if (
            is_frozen_quality_rework
            and not _is_precommit_rework_checkpoint(ckpt)
            and not _is_official_rework_checkpoint(ckpt)
        ):
            force_sequential_rework = True
            quality_skipper_config = {
                "source_dir": get_bot_dir(source_v),
                "expected_architecture_policy": (
                    frozen_plan.get("architecture_policy")
                    if isinstance(frozen_plan.get("architecture_policy"), dict)
                    else None
                ),
                "master_plan": frozen_plan,
            }
        if ckpt.get("stage") == "repair_planned":
            rework_plan_metadata = frozen_work_item
    if (
        not frozen_rework_resume
        and not durable_preparation_resume
        and reviewer_feedback
        and ckpt.get("stage") in (
        "workers_done", "quality_failed", "quality_passed", "reviewed", "critic_checked",
        "precommit_failed", "official_failed", "repair_planned", "rework_running"
        )
    ):
        rework_kind = "quality_repair" if ckpt.get("stage") == "quality_failed" else "gate_rework"
        if ckpt.get("stage") == "official_failed":
            rework_kind = "official_repair"
        elif ckpt.get("stage") == "precommit_failed":
            rework_kind = "precommit_repair"
        elif ckpt.get("parent2_v") is not None:
            rework_kind = f"crossover_{rework_kind}"
        existing_work_item = (
            (ckpt.get("master_plan") or {}).get("work_item")
            if isinstance(ckpt.get("master_plan"), dict) else None
        )
        if (
            ckpt.get("stage") in {"repair_planned", "rework_running"}
            and isinstance(existing_work_item, dict)
            and existing_work_item.get("kind")
        ):
            rework_kind = str(existing_work_item.get("kind"))
        task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        if review_rework_checkpoint or any("review_repair" in kind for kind in task_kinds):
            rework_kind = (
                "crossover_review_repair"
                if ckpt.get("parent2_v") is not None or rework_kind.startswith("crossover_")
                else "review_repair"
            )
        elif _is_official_rework_checkpoint(ckpt) or any("official_repair" in kind for kind in task_kinds):
            rework_kind = "official_repair"
        is_precommit_rework = rework_kind == "precommit_repair" or _is_precommit_rework_checkpoint(ckpt)
        is_official_rework = rework_kind == "official_repair" or _is_official_rework_checkpoint(ckpt)
        if is_precommit_rework:
            prior_rework_count = int(ckpt.get("precommit_rework_count") or 0)
            precommit_rework_count_for_write = prior_rework_count + 1
            if precommit_rework_count_for_write > MAX_PRECOMMIT_REWORK_ROUNDS:
                message = (
                    f"PRECOMMIT_REWORK_CIRCUIT_BREAKER: v{next_v} already used "
                    f"{prior_rework_count} precommit repair round(s) (max {MAX_PRECOMMIT_REWORK_ROUNDS}). "
                    "Abandon this generation and start a fresh direction."
                )
                log_system_event(
                    "pipeline.precommit_rework_circuit_breaker",
                    "error",
                    message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "precommit_rework_count": prior_rework_count,
                        "max_rework_rounds": MAX_PRECOMMIT_REWORK_ROUNDS,
                        "task_targets": sorted(_task_target_filenames(tasks)),
                    },
                )
                return _json_tool_result({
                    "error": "PRECOMMIT_REWORK_CIRCUIT_BREAKER",
                    "message": message,
                    "next_v": next_v,
                    "source_v": source_v,
                    "precommit_rework_count": prior_rework_count,
                    "max_rework_rounds": MAX_PRECOMMIT_REWORK_ROUNDS,
                    "directive": "Abandon this generation; repeated precommit repair did not converge.",
                })
        if is_official_rework:
            prior_official_rework_count = int(ckpt.get("official_rework_count") or 0)
            official_rework_count_for_write = prior_official_rework_count + 1
            if official_rework_count_for_write > MAX_OFFICIAL_REWORK_ROUNDS:
                message = (
                    f"OFFICIAL_REWORK_CIRCUIT_BREAKER: v{next_v} already used "
                    f"{prior_official_rework_count} official repair round(s) "
                    f"(max {MAX_OFFICIAL_REWORK_ROUNDS}). Abandon this generation; "
                    "repeated formal certification repair did not converge."
                )
                log_system_event(
                    "pipeline.official_rework_circuit_breaker",
                    "error",
                    message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "official_rework_count": prior_official_rework_count,
                        "max_rework_rounds": MAX_OFFICIAL_REWORK_ROUNDS,
                        "task_targets": sorted(_task_target_filenames(tasks)),
                    },
                )
                abandon_result = await _force_abandon_official_rework_generation(
                    next_v,
                    source_v,
                    actor_lock_owned=actor_lock_owned,
                )
                return _json_tool_result({
                    "error": "OFFICIAL_REWORK_CIRCUIT_BREAKER",
                    "message": message,
                    "next_v": next_v,
                    "source_v": source_v,
                    "official_rework_count": prior_official_rework_count,
                    "max_rework_rounds": MAX_OFFICIAL_REWORK_ROUNDS,
                    "abandoned": bool(abandon_result.get("abandoned")),
                    "abandon_result": abandon_result,
                    "directive": (
                        "This generation was abandoned by the tool layer after "
                        "repeated official repair failed to converge. Start a fresh direction."
                    ),
                })
        source_dir_r = get_bot_dir(source_v)
        try:
            preparation_base = worker_workflow.artifacts.capture(next_dir)
            projection_preimage_artifact_hash = (
                _complete_artifact_fingerprint(next_dir)
            )
            projection_preimage_snapshot_hash = preparation_base
            if projection_preimage_artifact_hash != preparation_base:
                raise RuntimeError(
                    "canonical repair preimage snapshot mismatch"
                )
            preparation_digest = hashlib.sha256(
                json.dumps(
                    {
                        "stage": ckpt.get("stage"),
                        "tasks": tasks,
                        "reviewer_feedback": reviewer_feedback,
                        "source_hash": _complete_artifact_fingerprint(source_dir_r),
                        "precommit_rework_count": precommit_rework_count_for_write,
                        "official_rework_count": official_rework_count_for_write,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            rework_preparation_dir = worker_workflow.artifacts.preparation_workspace(
                run_id=worker_workflow.run_id,
                cycle=int(durable_worker_state.get("cycle") or 0),
                input_digest=preparation_base,
                preparation_digest=preparation_digest,
            )
            prepared_candidate_dir = rework_preparation_dir
        except Exception as exc:
            return _json_tool_result({
                "error": "REWORK_PREPARATION_SNAPSHOT_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "next_tool": "abandon_generation",
                "directive": (
                    "Could not freeze the complete candidate before one-time "
                    "repair preparation. No reset or hygiene mutation was run."
                ),
            })
        reset_before_rework = _should_reset_before_rework(ckpt, tasks)
        if reset_before_rework and source_dir_r.exists() and prepared_candidate_dir.exists():
            _log.info(f"Resetting v{next_v} code from source v{source_v} before worker retry (incremental, preserves NEW files)")
            # Incremental reset: overwrite source files (undo worker edits) but
            # PRESERVE worker-created NEW files absent from source. This avoids
            # wiping NEW files on redundant orchestrator re-calls of execute_workers
            # (which would otherwise cause zero-changes wasted retries).
            try:
                preserved = _incremental_reset_next_dir(
                    prepared_candidate_dir,
                    source_dir_r,
                )
            except Exception as exc:
                rollback_error = rollback_rework_preparation()
                return _json_tool_result({
                    "error": (
                        "REWORK_PREPARATION_ROLLBACK_FAILED"
                        if rollback_error else "REWORK_SOURCE_RESET_FAILED"
                    ),
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "rollback_error": rollback_error,
                    "next_tool": "abandon_generation" if rollback_error else "execute_workers",
                })
            if preserved:
                _log.info("Preserved %d worker-created NEW file(s) across reset: %s",
                          len(preserved), preserved)
        elif not reset_before_rework:
            if rework_kind == "precommit_repair" or _is_precommit_rework_checkpoint(ckpt):
                log_system_event(
                    "pipeline.precommit_repair_in_place",
                    "warn",
                    f"Repairing v{next_v} in place after precommit failure; preserving candidate code",
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )
            elif "review_repair" in rework_kind:
                event_type = (
                    "pipeline.crossover_review_repair_in_place"
                    if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None
                    else "pipeline.review_repair_in_place"
                )
                event_message = (
                    f"Repairing crossover v{next_v} in place after reviewer rejection; preserving fused candidate code"
                    if event_type == "pipeline.crossover_review_repair_in_place"
                    else f"Repairing v{next_v} in place after reviewer rejection; preserving generated candidate code"
                )
                log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )
            else:
                in_place_kind = (
                    "crossover_quality_repair"
                    if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None
                    else "quality_repair"
                )
                event_type = (
                    "pipeline.crossover_quality_repair_in_place"
                    if in_place_kind == "crossover_quality_repair"
                    else "pipeline.quality_repair_in_place"
                )
                event_message = (
                    f"Repairing crossover v{next_v} in place after quality failure; preserving fused candidate code"
                    if in_place_kind == "crossover_quality_repair"
                    else f"Repairing v{next_v} in place after quality failure; preserving generated candidate code"
                )
                log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )

        try:
            from candidate_hygiene import sanitize_candidate_dir
            from workflow_profiles import get_workflow_profile
            execution_mode = getattr(
                get_workflow_profile(), "national_execution_mode", "native_tcp"
            )
            if execution_mode != "native_tcp":
                raise RuntimeError(
                    "active candidate hygiene requires the official native_tcp "
                    f"execution mode, got {execution_mode!r}"
                )
            sanitize_candidate_dir(
                prepared_candidate_dir,
                require_native_tcp=True,
            )
        except Exception as exc:
            rollback_error = rollback_rework_preparation()
            log_system_event(
                "pipeline.candidate_hygiene_failed",
                "error",
                f"Candidate hygiene failed for v{next_v}: {exc}",
                {"next_v": next_v, "source_v": source_v, "stage": ckpt.get("stage")},
            )
            return _json_tool_result({
                "error": (
                    "REWORK_PREPARATION_ROLLBACK_FAILED"
                    if rollback_error else "CANDIDATE_HYGIENE_FAILED"
                ),
                "message": f"Candidate hygiene failed: {exc}",
                "rollback_error": rollback_error,
                "next_v": next_v,
                "source_v": source_v,
                "next_tool": "abandon_generation" if rollback_error else "execute_workers",
            })

        # Write intermediate checkpoint so pipeline state reflects the in-progress retry.
        # Without this, a crash between code reset and worker execution would leave
        # the checkpoint at a stale stage (e.g. "reviewed" or "critic_checked")
        # while the actual code has been wiped back to source.
        retry_plan = _checkpoint_plan_with_tasks(
            ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
        )
        rework_plan_metadata = {
            "kind": rework_kind,
            "source_stage": ckpt.get("stage"),
            "reset_performed": reset_before_rework,
            "route": route_policy(ckpt),
        }
        retry_plan = {
            **retry_plan,
            "work_item": rework_plan_metadata,
        }
        for task in tasks:
            if isinstance(task, dict):
                task.setdefault("task_kind", rework_kind)
        retry_plan = _plan_with_accumulated_repair_scope(ckpt, retry_plan, tasks, next_v)
        task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        is_quality_rework = (
            ckpt.get("stage") == "quality_failed"
            or "quality_repair" in rework_kind
            or any("quality_repair" in kind for kind in task_kinds)
        )
        if (
            is_quality_rework
            and not _is_precommit_rework_checkpoint(ckpt)
            and not _is_official_rework_checkpoint(ckpt)
            and ckpt.get("stage") in {"quality_failed", "repair_planned", "rework_running"}
        ):
            force_sequential_rework = True
            quality_skipper_config = {
                "source_dir": source_dir_r,
                "expected_architecture_policy": (
                    (_checkpoint_master_plan(ckpt).get("architecture_policy"))
                    if isinstance(_checkpoint_master_plan(ckpt).get("architecture_policy"), dict)
                    else None
                ),
                "master_plan": retry_plan,
            }
            try:
                mechanical_trim_results = _apply_mechanical_file_size_trims(
                    tasks,
                    prepared_candidate_dir,
                    source_dir_r,
                    next_v,
                    source_v,
                )
            except Exception as exc:
                rollback_error = rollback_rework_preparation()
                return _json_tool_result({
                    "error": (
                        "REWORK_PREPARATION_ROLLBACK_FAILED"
                        if rollback_error else "REWORK_MECHANICAL_TRIM_FAILED"
                    ),
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "rollback_error": rollback_error,
                    "next_tool": "abandon_generation" if rollback_error else "execute_workers",
                })

        if reset_before_rework:
            reviewer_feedback += (
                f"\n\nNOTE: This is a retry. The code in bots/{bot_name(next_v)}/ has been ACTUALLY RESET "
                f"by the system to the exact national_v{source_v} preimage. The source path remains "
                f"unreadable to this Worker. Any modifications described in the feedback above no "
                f"longer exist in the candidate — re-implement them from the injected contract."
            )
        elif rework_kind == "precommit_repair" or _is_precommit_rework_checkpoint(ckpt):
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place precommit regression repair. The current code in "
                f"bots/{bot_name(next_v)}/ is the candidate that failed precommit; preserve it except "
                f"for targeted EV/matchup regression fixes."
            )
        elif rework_kind == "official_repair" or _is_official_rework_checkpoint(ckpt):
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place official EXE full-certification repair. The current code in "
                f"bots/{bot_name(next_v)}/ passed local gates but failed the real Windows national platform. "
                "Preserve the candidate except for the exact compliance/state-machine/obvious-decision blocker "
                "shown in the official evidence; do not use EXE win/loss as strength tuning evidence."
            )
        elif "review_repair" in rework_kind:
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place Lead Code Reviewer repair. The current code in "
                f"bots/{bot_name(next_v)}/ is the candidate that failed the reviewer hard gate; "
                "preserve it except for the exact code-quality blocker described above."
            )
        else:
            if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None:
                reviewer_feedback += (
                    f"\n\nNOTE: This is an in-place crossover quality repair. The current code in "
                    f"bots/{bot_name(next_v)}/ is the generated crossover candidate and must be preserved "
                    f"except for the exact quality-gate blockers above."
                )
            else:
                reviewer_feedback += (
                    f"\n\nNOTE: This is an in-place quality repair. The current code in "
                    f"bots/{bot_name(next_v)}/ is the generated candidate and must be preserved "
                    f"except for the exact quality-gate blockers above."
                )
        changed_trims = [item for item in mechanical_trim_results if item.get("changed")]
        if changed_trims:
            trim_summary = "; ".join(
                f"{Path(item.get('target', item.get('file', ''))).name}: "
                f"{item.get('before')}L->{item.get('after')}L"
                for item in changed_trims
            )
            reviewer_feedback += (
                "\n\nNOTE: Before LLM workers, the pipeline mechanically removed "
                "non-behavioral Python text (comments/docstrings/blank lines) from "
                f"large file_size targets: {trim_summary}. Continue only if a blocker remains."
            )

        repair_baseline_artifact_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if not repair_baseline_artifact_hash:
            rollback_error = rollback_rework_preparation()
            return _json_tool_result({
                "error": (
                    "REWORK_PREPARATION_ROLLBACK_FAILED"
                    if rollback_error else "REPAIR_BASELINE_ARTIFACT_UNAVAILABLE"
                ),
                "next_v": next_v,
                "source_v": source_v,
                "next_tool": "abandon_generation",
                "rollback_error": rollback_error,
                "directive": (
                    "Could not freeze the complete post-reset/post-hygiene repair "
                    "baseline. Do not execute Workers without a content receipt."
                ),
            })
        prepared_repair_snapshot_hash = worker_workflow.artifacts.capture(
            prepared_candidate_dir
        )
        if prepared_repair_snapshot_hash != repair_baseline_artifact_hash:
            rollback_error = rollback_rework_preparation()
            return _json_tool_result({
                "error": "REPAIR_PREPARATION_SNAPSHOT_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "rollback_error": rollback_error,
            })
        frozen_preparation_input = {
            "schema_version": 4,
            "tasks": deepcopy(tasks),
            "reviewer_feedback": reviewer_feedback,
            "worker_template_hash": hashlib.sha256(
                worker_template.encode("utf-8")
            ).hexdigest(),
            "backend_contract": _worker_backend_contract(),
            "projection_preimage_artifact_hash": (
                projection_preimage_artifact_hash
            ),
            "projection_preimage_snapshot_hash": (
                projection_preimage_snapshot_hash
            ),
        }
        frozen_preparation_input_digest = hashlib.sha256(
            json.dumps(
                frozen_preparation_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        rework_plan_metadata = {
            **rework_plan_metadata,
            "projection_preimage_artifact_hash": (
                projection_preimage_artifact_hash
            ),
            "projection_preimage_snapshot_hash": (
                projection_preimage_snapshot_hash
            ),
            "repair_baseline_artifact_hash": repair_baseline_artifact_hash,
            "prepared_snapshot_hash": prepared_repair_snapshot_hash,
            "frozen_worker_input": frozen_preparation_input,
            "frozen_worker_input_digest": frozen_preparation_input_digest,
        }
        retry_plan = {
            **retry_plan,
            "work_item": rework_plan_metadata,
        }
        retry_plan = _plan_with_accumulated_repair_scope(
            ckpt,
            retry_plan,
            tasks,
            next_v,
        )
        repair_checkpoint_written = write_pipeline_checkpoint(
            next_v,
            source_v,
            "repair_planned",
            master_plan=retry_plan,
            reviewer_feedback=reviewer_feedback,
            worker_failure_count=ckpt.get("worker_failure_count", 0),
            precommit_rework_count=precommit_rework_count_for_write,
            official_rework_count=official_rework_count_for_write,
            repair_baseline_artifact_hash=repair_baseline_artifact_hash,
            expected_checkpoint_revision=int(
                ckpt.get("checkpoint_revision") or 0
            ),
            expected_checkpoint_stage=str(ckpt.get("stage") or ""),
            expected_workflow_run_id=str(ckpt.get("workflow_run_id") or ""),
        )
        if not repair_checkpoint_written:
            rollback_error = rollback_rework_preparation()
            return _json_tool_result({
                "error": (
                    "REWORK_PREPARATION_ROLLBACK_FAILED"
                    if rollback_error else "REPAIR_BASELINE_CHECKPOINT_FAILED"
                ),
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": repair_baseline_artifact_hash,
                "candidate_restored": not rollback_error,
                "rollback_error": rollback_error,
                "directive": (
                    "The system prepared a repair baseline but could not persist its "
                    "content receipt. Do not execute Workers or claim repair authority."
                ),
            })

    if reviewer_feedback and rework_plan_metadata:
        expected_rework_hash = str(
            rework_plan_metadata.get("repair_baseline_artifact_hash") or ""
        )
        current_rework_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if (
            not expected_rework_hash
            or not current_rework_hash
            or current_rework_hash != expected_rework_hash
        ):
            return _json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_rework_hash,
                "current_artifact_hash": current_rework_hash,
                "next_tool": "abandon_generation",
                "directive": (
                    "The candidate changed after the repair baseline receipt was "
                    "written and before Workers. Abandon this generation."
                ),
            })
        running_plan = (
            _checkpoint_plan_with_tasks(
                ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
            )
            if ckpt else {"tasks": tasks}
        )
        running_plan = {**running_plan, "work_item": rework_plan_metadata}
        running_plan = _plan_with_accumulated_repair_scope(ckpt, running_plan, tasks, next_v)
        rework_projection_ckpt = _matching_checkpoint(next_v, source_v)
        if not rework_projection_ckpt:
            return _json_tool_result({
                "error": "REWORK_PROJECTION_CHECKPOINT_MISSING",
                "next_v": next_v,
                "source_v": source_v,
            })
        rework_checkpoint_written = write_pipeline_checkpoint(
            next_v,
            source_v,
            "rework_running",
            master_plan=running_plan,
            reviewer_feedback=reviewer_feedback,
            worker_failure_count=ckpt.get("worker_failure_count", 0) if ckpt else 0,
            precommit_rework_count=precommit_rework_count_for_write,
            official_rework_count=official_rework_count_for_write,
            repair_baseline_artifact_hash=expected_rework_hash,
            expected_checkpoint_revision=int(
                rework_projection_ckpt.get("checkpoint_revision") or 0
            ),
            expected_checkpoint_stage=str(
                rework_projection_ckpt.get("stage") or ""
            ),
            expected_workflow_run_id=str(
                rework_projection_ckpt.get("workflow_run_id") or ""
            ),
        )
        if not rework_checkpoint_written:
            return _json_tool_result({
                "error": "REWORK_RUNNING_CHECKPOINT_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_rework_hash,
                "directive": (
                    "The repair baseline was frozen but the rework-running "
                    "transition could not be persisted. Do not execute Workers."
                ),
            })

        # Recheck immediately before the Worker batch.  This closes the gap in
        # which a self-modifying test or external process edits an otherwise
        # declared repair file after checkpoint publication.
        current_rework_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if current_rework_hash != expected_rework_hash:
            return _json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_rework_hash,
                "current_artifact_hash": current_rework_hash,
                "next_tool": "abandon_generation",
            })

    if frozen_rework_resume and ckpt.get("stage") in rework_stages:
        expected_retry_hash = _checkpoint_repair_baseline_fingerprint(ckpt)
        current_retry_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if (
            not expected_retry_hash
            or not current_retry_hash
            or current_retry_hash != expected_retry_hash
        ):
            abandon_result = await _force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "frozen_rework_pre_worker_drift",
                actor_lock_owned=actor_lock_owned,
            )
            return _json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_retry_hash,
                "current_artifact_hash": current_retry_hash,
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The infrastructure retry candidate no longer matches its "
                    "frozen repair baseline. Abandon without consuming the lease."
                ),
            })

    task_digest = _worker_execution_task_digest(
        tasks,
        reviewer_feedback,
        worker_template,
    )
    if durable_worker_resume:
        durable_input_digest = _worker_execution_task_digest(
            durable_worker_envelope.get("tasks") or [],
            str(durable_worker_envelope.get("reviewer_feedback") or ""),
            worker_template,
        )
        if task_digest != durable_input_digest:
            abandon_result = await _force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "durable_worker_frozen_input_drift",
                actor_lock_owned=actor_lock_owned,
            )
            worker_workflow.abandon("durable_worker_frozen_input_drift")
            return _json_tool_result({
                "error": "DURABLE_WORKER_FROZEN_INPUT_DRIFT",
                "success": False,
                "next_v": next_v,
                "source_v": source_v,
                **abandon_result,
            })

    if durable_worker_status == "idle":
        from worker_workflow import build_worker_envelope

        projection_ckpt = _matching_checkpoint(next_v, source_v)
        if not projection_ckpt:
            return _json_tool_result({
                "error": "DURABLE_WORKER_CHECKPOINT_MISSING_BEFORE_PREPARE",
                "next_v": next_v,
                "source_v": source_v,
            })
        prepared_artifact_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        prepared_snapshot_hash = worker_workflow.artifacts.capture(
            prepared_candidate_dir
        )
        if prepared_artifact_hash != prepared_snapshot_hash:
            return _json_tool_result({
                "error": "DURABLE_WORKER_PREPARED_SNAPSHOT_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "prepared_artifact_hash": prepared_artifact_hash,
                "prepared_snapshot_hash": prepared_snapshot_hash,
                "next_tool": "abandon_generation",
            })
        active_work_item = rework_plan_metadata or (
            (_checkpoint_master_plan(ckpt).get("work_item") or {})
            if isinstance(_checkpoint_master_plan(ckpt).get("work_item"), dict)
            else {}
        )
        worker_kind = str(active_work_item.get("kind") or "initial_worker")
        projection_plan = _checkpoint_plan_with_tasks(
            projection_ckpt,
            tasks,
            replace_existing_tasks=replace_checkpoint_tasks,
        )
        if active_work_item:
            projection_plan = {
                **projection_plan,
                "work_item": active_work_item,
            }
        if reviewer_feedback:
            projection_plan = _plan_with_accumulated_repair_scope(
                projection_ckpt,
                projection_plan,
                tasks,
                next_v,
            )
        projection_preimage_artifact_hash = str(
            active_work_item.get("projection_preimage_artifact_hash")
            or prepared_artifact_hash
        )
        projection_preimage_snapshot_hash = (
            str(
                active_work_item.get("projection_preimage_snapshot_hash")
                or ""
            )
            or prepared_snapshot_hash
        )
        try:
            worker_workflow.artifacts.path_for(
                projection_preimage_snapshot_hash
            )
        except Exception as exc:
            return _json_tool_result({
                "error": "DURABLE_WORKER_PROJECTION_PREIMAGE_UNAVAILABLE",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "projection_preimage_artifact_hash": (
                    projection_preimage_artifact_hash
                ),
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
        checkpoint_contract = {
            "workflow_run_id": str(
                projection_ckpt.get("workflow_run_id")
                or projection_ckpt.get("run_id")
                or worker_workflow.run_id
                or ""
            ),
            "checkpoint_revision": int(
                projection_ckpt.get("checkpoint_revision") or 0
            ),
            "checkpoint_stage": str(projection_ckpt.get("stage") or ""),
        }
        execution_policy = {
            "force_sequential": bool(force_sequential_rework),
            "quality_skipper": quality_skipper_config is not None,
            "expected_architecture_policy": (
                deepcopy(
                    quality_skipper_config.get(
                        "expected_architecture_policy"
                    )
                )
                if isinstance(quality_skipper_config, dict)
                else None
            ),
            **(
                {"executor": "system_policy_bootstrap_v1"}
                if _system_bootstrap_executor
                else {}
            ),
        }
        envelope = build_worker_envelope(
            checkpoint=projection_ckpt,
            kind=worker_kind,
            source_stage=str(projection_ckpt.get("stage") or ""),
            prepared_artifact_hash=prepared_artifact_hash,
            prepared_snapshot_hash=prepared_snapshot_hash,
            source_artifact_hash=(
                prepared_artifact_hash
                if _system_bootstrap_executor
                else _complete_artifact_fingerprint(
                    get_bot_dir(source_v)
                )
            ),
            tasks=tasks,
            reviewer_feedback=reviewer_feedback,
            worker_template_hash=hashlib.sha256(
                worker_template.encode("utf-8")
            ).hexdigest(),
            work_item=active_work_item,
            backend_contract=_expected_worker_backend_contract(
                projection_ckpt,
                {"execution_policy": execution_policy},
            ),
            precommit_rework_count=(
                int(precommit_rework_count_for_write)
                if precommit_rework_count_for_write is not None
                else int(projection_ckpt.get("precommit_rework_count") or 0)
            ),
            official_rework_count=(
                int(official_rework_count_for_write)
                if official_rework_count_for_write is not None
                else int(projection_ckpt.get("official_rework_count") or 0)
            ),
            projection_plan=projection_plan,
            audit_context=deepcopy(projection_ckpt.get("audit_context") or {}),
            execution_policy=execution_policy,
            checkpoint_contract=checkpoint_contract,
            worker_failure_count=int(
                projection_ckpt.get("worker_failure_count") or 0
            ),
            projection_preimage_artifact_hash=(
                projection_preimage_artifact_hash
            ),
            projection_preimage_snapshot_hash=(
                projection_preimage_snapshot_hash
            ),
        )
        durable_worker_state = worker_workflow.prepare(
            envelope,
            max_attempts=1 if _system_bootstrap_executor else 3,
        )
        durable_worker_envelope = durable_worker_state["envelope"]
        durable_worker_status = durable_worker_state["status"]
        if rework_preparation_dir is not None:
            worker_workflow.artifacts.discard_workspace(
                rework_preparation_dir
            )
        if not _system_bootstrap_executor:
            try:
                from llm_availability_store import active_llm_pause

                _active_pause = active_llm_pause()
            except Exception as exc:
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_STATE_INVALID",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "directive": (
                        "Worker preparation is durable, but the provider pause "
                        "record is invalid. No effect was claimed."
                    ),
                })
            if _active_pause is not None:
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_BLOCKED",
                    "success": False,
                    "failure_class": "availability",
                    "action": "wait_for_llm_availability",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "attempt": int(
                        durable_worker_state.get("attempt") or 0
                    ),
                    "max_attempts": int(
                        durable_worker_state.get("max_attempts") or 0
                    ),
                    "effect_id": durable_worker_state.get("effect_id"),
                    "availability": _active_pause,
                    "directive": (
                        "Worker input was frozen, but the provider pause is "
                        "active. No effect was claimed and no attempt was consumed."
                    ),
                })
        if actor_lock_owned:
            return _DeferredWorkerActivity(
                workflow=worker_workflow,
                envelope=durable_worker_envelope,
                next_dir=next_dir,
                worker_template=worker_template,
            )
        return await _run_durable_worker_effect(
            worker_workflow,
            durable_worker_envelope,
            next_dir,
            worker_template,
        )

    return _json_tool_result({
        "error": "DURABLE_WORKER_COMMAND_DISPATCH_INVARIANT",
        "workflow_status": durable_worker_status,
        "next_v": next_v,
        "source_v": source_v,
    })


@tool("execute_workers", "Execute worker tasks to modify bot code. Each task has worker_id, role, target_files, worker_prompt.", {"tasks": list, "next_v": int, "source_v": int, "reviewer_feedback": str})
async def execute_workers(args):
    """Serialize deterministic preparation, then run the leased LLM outside it.

    Only idle/completed histories can perform one-time preparation or open a
    new cycle.  They enter the generation actor before replaying again.  The
    resulting Worker activity is returned as an internal dispatch token so the
    expensive model call never holds the actor lock and a central abandon can
    fence it immediately.
    """
    next_v = args.get("next_v") or args.get("version")
    source_v = args.get("source_v")
    if next_v is None or source_v is None:
        next_v, source_v = _resolve_version_args(args)
    checkpoint = (
        _matching_checkpoint(next_v, source_v)
        if next_v is not None and source_v is not None
        else None
    )
    if not isinstance(checkpoint, dict):
        return await _execute_workers_command(args)

    try:
        from worker_workflow import WorkerWorkflow
        from workflow_kernel import WorkflowBusy

        workflow = WorkerWorkflow.for_checkpoint(checkpoint)
        try:
            with workflow.store.command_lock(workflow.run_id):
                result = await _execute_workers_command(
                    args,
                    actor_lock_owned=True,
                )
        except WorkflowBusy:
            return _json_tool_result({
                "error": "WORKER_COMMAND_BUSY",
                "failure_class": "infrastructure",
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "Another process is publishing the deterministic Worker "
                    "preparation for this generation. Retry without editing the "
                    "candidate or rebuilding the prompt."
                ),
            })
        if isinstance(result, _DeferredWorkerActivity):
            return await _run_durable_worker_effect(
                result.workflow,
                result.envelope,
                result.next_dir,
                result.worker_template,
            )
        return result
    except WorkflowBusy:
        return _json_tool_result({
            "error": "WORKER_COMMAND_BUSY",
            "failure_class": "infrastructure",
            "action": "retry_same_tool",
            "next_v": next_v,
            "source_v": source_v,
        })


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
