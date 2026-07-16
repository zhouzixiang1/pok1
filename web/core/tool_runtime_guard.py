"""Runtime git/worktree guard for mutating pipeline MCP tools."""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

from claude_agent_sdk import tool as sdk_tool

from bot_namespace import ACTIVE_BOT_PREFIX, bot_relpath
from evolution_infra import EVOLUTION_BRANCH, PROJECT_ROOT, read_pipeline_checkpoint
from evaluation_contract import build_evaluation_contract, contract_bot_versions, evaluate_head_drift
from repo_state import get_last_snapshot, git_worktree_snapshot, is_generated_bot_dir_entry
from evolution_scope import (
    classify_status_entries,
)
from pipeline_state import head_drift_allowed_tools, head_drift_resume_policy, route_policy

_BOT_DIR_RE = re.compile(rf"^\?\? bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)/$")
# Explicit abandonment is the safe resolution when a checkpoint's evaluation
# contract changed underneath it.  Keep normal pipeline tools fail-closed, but
# allow that cleanup tool to cross the HEAD boundary; branch/worktree guards
# still run and ``_do_abandon_generation`` applies its own stage/cooldown rules.
_HEAD_CHANGE_ALLOWED_TOOLS = {"run_archivist", "abandon_generation"}
_PIPELINE_ROUTE_TOOLS = {
    "prepare_next_gen",
    "run_crossover",
    "run_direction_audit",
    "run_literature_probe",
    "run_master",
    "execute_workers",
    "run_quality_gates",
    "run_review",
    "run_critic",
    "run_precommit_eval",
    "commit_bot",
    "run_archivist",
    "abandon_generation",
}

_SYSTEM_DETERMINISTIC_ROUTE = ContextVar(
    "pok_system_deterministic_route",
    default=None,
)


@contextmanager
def system_deterministic_route_authority(tool_name: str, checkpoint: dict):
    """Authorize one in-process outer deterministic tool call.

    The token is process-local and cannot be supplied by an MCP provider.  It
    is currently required only for the checkpoint-free post-publication
    Archivist boundary, where prompt ownership belongs to the outer loop.
    """

    value = {
        "tool_name": str(tool_name),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "stage": checkpoint.get("stage"),
        "handoff_identity_digest": checkpoint.get(
            "post_publication_handoff_identity_digest"
        ),
        "publication_id": checkpoint.get("post_publication_id"),
    }
    token = _SYSTEM_DETERMINISTIC_ROUTE.set(value)
    try:
        yield
    finally:
        _SYSTEM_DETERMINISTIC_ROUTE.reset(token)


def _json_tool_result(data: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}


def _guard_enabled() -> bool:
    if os.environ.get("POK_DISABLE_TOOL_RUNTIME_GUARD") == "1":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("POK_FORCE_TOOL_RUNTIME_GUARD") != "1":
        return False
    return True


def _candidate_version(tool_name: str, args: dict[str, Any]) -> int | None:
    for key in ("next_v", "version", "target_v"):
        value = args.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    checkpoint = read_pipeline_checkpoint()
    if checkpoint and isinstance(checkpoint.get("next_v"), int):
        return int(checkpoint["next_v"])
    if tool_name in {"cleanup_incomplete", "abandon_generation"}:
        try:
            from evolution_infra import compute_next_generation_v
            return int(compute_next_generation_v())
        except Exception:
            return None
    return None


def _source_version(args: dict[str, Any]) -> int | None:
    value = args.get("source_v")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    checkpoint = read_pipeline_checkpoint()
    if checkpoint and isinstance(checkpoint.get("source_v"), int):
        return int(checkpoint["source_v"])
    return None


def _same_int(left: Any, right: Any) -> bool:
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return False


def _operator_bootstrap_certificate_valid(candidate_v: int | None) -> bool:
    """Fail closed unless the parked candidate has completed authorization."""
    if candidate_v is None:
        return False
    try:
        from official_bootstrap import (
            validate_completed_operator_bootstrap_authorization,
        )
        from official_certification import official_full_certified, read_status

        candidate = PROJECT_ROOT / bot_relpath(int(candidate_v))
        status = read_status(candidate)
        if not official_full_certified(status, candidate):
            return False
        checkpoint = read_pipeline_checkpoint()
        completed = validate_completed_operator_bootstrap_authorization(
            status,
            candidate,
            checkpoint=checkpoint,
        )
        return completed.get("valid") is True
    except Exception:
        return False


def _pipeline_route_guard(
    *,
    tool_name: str,
    args: dict[str, Any],
    candidate_v: int | None,
    source_v: int | None,
) -> tuple[bool, dict[str, Any]]:
    """Block LLM/tool-call stage skips before the expensive tool body runs."""
    if tool_name not in _PIPELINE_ROUTE_TOOLS:
        return True, {}
    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict) or not checkpoint.get("stage"):
        # commit_bot deliberately clears the active checkpoint before the
        # post-commit Archivist runs.  Preserve that one lifecycle exception,
        # but require the content-bound, exact-source, single-use receipt that
        # commit_bot placed in this generation's archive snapshot. Thus an
        # outer model cannot replay an arbitrary historical bot or source.
        if (
            tool_name == "run_archivist"
            and candidate_v is not None
            and source_v is not None
        ):
            try:
                from evolution_infra import validate_post_commit_archivist_receipt
                from post_publication_handoff import pending_handoff_route

                system_route = _SYSTEM_DETERMINISTIC_ROUTE.get()
                handoff = pending_handoff_route()
                system_owned = bool(
                    isinstance(system_route, dict)
                    and system_route.get("tool_name") == "run_archivist"
                    and system_route.get("stage") == "archived"
                    and system_route.get("next_v") == int(candidate_v)
                    and system_route.get("source_v") == int(source_v)
                    and handoff.get("status") == "pending"
                    and handoff.get("state") == "pending"
                    and handoff.get("owner_scope") == "none"
                    and system_route.get("handoff_identity_digest")
                    == handoff.get("identity_digest")
                    and system_route.get("publication_id")
                    == handoff.get("publication_id")
                )

                receipt_ok, _reason, receipt = (
                    validate_post_commit_archivist_receipt(
                        int(candidate_v),
                        int(source_v),
                    )
                )
                if system_owned and receipt_ok:
                    return True, {
                        "post_commit_archivist": True,
                        "system_deterministic_route": True,
                        "candidate_v": int(candidate_v),
                        "source_v": int(source_v),
                        "receipt_digest": str(
                            (receipt or {}).get("receipt_digest") or ""
                        ),
                    }
            except Exception:
                pass
        payload = {
            "error": "pipeline_route_guard_blocked",
            "blocked": True,
            "reason": "no_active_checkpoint",
            "tool": tool_name,
            "requested_v": candidate_v,
            "requested_source_v": source_v,
            "checkpoint_stage": None,
            "next_tool": "prepare_generation",
            "allowed_tools": ["prepare_generation"],
            "mcp_allowed_tools": [],
            "provider_action": "end_stream",
            "scheduler_owned": True,
            "directive": (
                "No active generation checkpoint exists. End this provider "
                "stream. The outer scheduler alone owns prepare_generation; it "
                "is not an MCP tool. Do not call prepare_next_gen. The scheduler "
                "must bind source, target, crossover parents, and evidence before "
                "any pipeline tool runs."
            ),
        }
        _log_guard_event(
            "pipeline.route_guard_blocked",
            "error",
            f"Blocked {tool_name}: no active generation checkpoint",
            payload,
        )
        return False, payload

    ckpt_next = checkpoint.get("next_v")
    ckpt_source = checkpoint.get("source_v")
    route = route_policy(checkpoint)
    allowed_tools = [str(t) for t in (route.get("allowed_tools") or []) if t]

    if ckpt_next is not None and candidate_v is not None and not _same_int(ckpt_next, candidate_v):
        payload = {
            "error": "pipeline_route_guard_blocked",
            "blocked": True,
            "reason": "active_generation_mismatch",
            "tool": tool_name,
            "requested_v": candidate_v,
            "active_v": ckpt_next,
            "active_source_v": ckpt_source,
            "checkpoint_stage": checkpoint.get("stage"),
            "next_tool": route.get("next_tool"),
            "allowed_tools": allowed_tools,
            "route": route,
            "directive": (
                "A different generation is active. Use the active checkpoint "
                "version/source, or abandon the active generation before "
                "starting another one."
            ),
        }
        _log_guard_event(
            "pipeline.route_guard_blocked",
            "error",
            f"Blocked {tool_name}: requested v{candidate_v} but active checkpoint is v{ckpt_next}",
            payload,
        )
        return False, payload

    if ckpt_source is not None and source_v is not None and not _same_int(ckpt_source, source_v):
        payload = {
            "error": "pipeline_route_guard_blocked",
            "blocked": True,
            "reason": "active_source_mismatch",
            "tool": tool_name,
            "requested_source_v": source_v,
            "active_v": ckpt_next,
            "active_source_v": ckpt_source,
            "checkpoint_stage": checkpoint.get("stage"),
            "next_tool": route.get("next_tool"),
            "allowed_tools": allowed_tools,
            "route": route,
            "directive": "Use the source version recorded in the active checkpoint.",
        }
        _log_guard_event(
            "pipeline.route_guard_blocked",
            "error",
            f"Blocked {tool_name}: requested source v{source_v} but active checkpoint source is v{ckpt_source}",
            payload,
        )
        return False, payload

    if (
        checkpoint.get("stage") == "official_bootstrap_required"
        and tool_name == "commit_bot"
    ):
        operator_finalize = (
            os.environ.get("POK_OPERATOR_FIRST_STRICT_FINALIZE")
            == str(os.getpid())
        )
        if (
            operator_finalize
            and _operator_bootstrap_certificate_valid(candidate_v)
        ):
            # This is not an Orchestrator route.  The explicit runtime-only
            # finalize-first-strict CLI may enter commit_bot after reopening
            # the complete signed certificate; normal route.allowed_tools
            # remains empty while the checkpoint is parked.
            return True, {
                "operator_only_finalize": True,
                "candidate_v": candidate_v,
                "checkpoint_stage": checkpoint.get("stage"),
            }
        payload = {
            "error": "pipeline_route_guard_blocked",
            "blocked": True,
            "reason": (
                "official_bootstrap_certificate_required"
                if operator_finalize
                else "operator_finalize_command_required"
            ),
            "tool": tool_name,
            "requested_v": candidate_v,
            "active_v": ckpt_next,
            "active_source_v": ckpt_source,
            "checkpoint_stage": checkpoint.get("stage"),
            "next_tool": route.get("next_tool"),
            "allowed_tools": allowed_tools,
            "route": route,
            "directive": (
                "The candidate is parked for explicit operator control. Run the "
                "checkpoint-bound bootstrap command first; after it succeeds, use "
                "finalize-first-strict from the autonomous runtime checkout. Direct "
                "Orchestrator commit_bot calls remain forbidden."
            ),
        }
        _log_guard_event(
            "pipeline.route_guard_blocked",
            "warn",
            f"Blocked commit_bot for v{candidate_v}: operator bootstrap certificate is absent or invalid",
            payload,
        )
        return False, payload

    if tool_name in allowed_tools:
        return True, {}

    payload = {
        "error": "pipeline_route_guard_blocked",
        "blocked": True,
        "reason": "wrong_pipeline_stage",
        "tool": tool_name,
        "checkpoint_stage": checkpoint.get("stage"),
        "active_v": ckpt_next,
        "active_source_v": ckpt_source,
        "next_tool": route.get("next_tool"),
        "allowed_tools": allowed_tools,
        "route": route,
        "directive": route.get("directive") or "Call the pipeline tool required by the active checkpoint stage.",
    }
    _log_guard_event(
        "pipeline.route_guard_blocked",
        "error",
        f"Blocked {tool_name} at stage {checkpoint.get('stage')}; next tool is {route.get('next_tool')}",
        payload,
    )
    return False, payload


def _absorb_committed_crossover_intent_before_route() -> tuple[bool, dict[str, Any]]:
    """Reduce a committed crossover journal before stage routing can hide it."""

    checkpoint = read_pipeline_checkpoint()
    crossover = (
        ((checkpoint or {}).get("audit_context") or {}).get("crossover")
        if isinstance(checkpoint, dict)
        else None
    )
    if not isinstance(crossover, dict) or not isinstance(
        crossover.get("projection"), dict
    ):
        return True, {}
    try:
        from crossover_projection import absorb_committed_crossover_projection
        from evolution_infra import RESULTS_DIR, get_bot_dir
        from workflow_kernel import WorkflowStore

        store = WorkflowStore(RESULTS_DIR / "workflow" / "events.sqlite3")
        result = absorb_committed_crossover_projection(
            checkpoint=checkpoint,
            target_dir=get_bot_dir(int(checkpoint["next_v"])),
            workflow_store=store,
        )
    except Exception as exc:
        return False, {
            "blocked": True,
            "reason": "crossover_projection_absorber_error",
            "message": f"{type(exc).__name__}: {str(exc)[:240]}",
        }
    if not isinstance(result, dict):
        return True, {}
    if result.get("outcome") == "infrastructure_failure":
        return False, {
            "blocked": True,
            "reason": "crossover_projection_absorber_failed",
            "projection_result": result,
        }
    return True, result


def _entry_allowed(line: str, candidate_v: int | None) -> bool:
    stripped = line.strip()
    match = _BOT_DIR_RE.match(stripped)
    if match:
        return candidate_v is not None and int(match.group("version")) == int(candidate_v)
    return False


def _snapshot() -> dict[str, Any]:
    """Capture enough status lines for in-place shared worktrees."""
    try:
        return git_worktree_snapshot(max_lines=10000)
    except TypeError:
        # Tests commonly monkeypatch git_worktree_snapshot with a zero-arg lambda.
        return git_worktree_snapshot()


def _contract_versions_for_candidate(candidate_v: int | None) -> list[int] | None:
    if candidate_v is None:
        return None
    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        return None
    try:
        if int(checkpoint.get("next_v") or -1) != int(candidate_v):
            return None
    except Exception:
        return None
    return contract_bot_versions(candidate_v=candidate_v, checkpoint=checkpoint)


def _evaluation_contract_for_candidate(candidate_v: int | None) -> dict[str, Any] | None:
    if candidate_v is None:
        return None
    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        return None
    try:
        if int(checkpoint.get("next_v") or -1) != int(candidate_v):
            return None
    except Exception:
        return None
    try:
        return build_evaluation_contract(
            PROJECT_ROOT,
            candidate_v=candidate_v,
            source_v=checkpoint.get("source_v"),
            checkpoint=checkpoint,
        )
    except Exception:
        return None


def _scope(snapshot: dict[str, Any], candidate_v: int | None) -> dict[str, Any]:
    return classify_status_entries(
        snapshot.get("entries") or [],
        candidate_v,
        contract_bot_versions=_contract_versions_for_candidate(candidate_v),
        evaluation_contract=_evaluation_contract_for_candidate(candidate_v),
    )


def _unexpected_entries(snapshot: dict[str, Any], candidate_v: int | None) -> list[str]:
    return list(_scope(snapshot, candidate_v).get("blocking_entries") or [])


def _ignored_entries(snapshot: dict[str, Any], candidate_v: int | None) -> list[str]:
    return list(_scope(snapshot, candidate_v).get("ignored_entries") or [])


def _checkpoint_repo_baseline(candidate_v: int | None) -> dict[str, Any] | None:
    if candidate_v is None:
        return None
    checkpoint = read_pipeline_checkpoint()
    if not checkpoint:
        return None
    try:
        if int(checkpoint.get("next_v") or -1) != int(candidate_v):
            return None
    except Exception:
        return None
    baseline = checkpoint.get("repo_baseline")
    return dict(baseline) if isinstance(baseline, dict) else None


def _head_change_allowed_for_checkpoint_resume(
    *,
    tool_name: str,
    candidate_v: int | None,
    baseline_head: str,
    current_head: str,
    snapshot: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Allow safe checkpoint continuation after infrastructure HEAD changes.

    A checkpoint may legitimately survive a codebase update: a saved master
    plan must continue with the initial workers, and a failed checkpoint must
    continue with ``execute_workers`` plus the recorded gate failures. Blocking
    those paths leaves the service unable to start. Post-quality checkpoints
    can also continue through reviewer/critic/precommit/commit after the
    candidate has been revalidated on the current HEAD. We only allow the exact
    next tool for the checkpoint stage on the canonical branch, for the active
    checkpoint version, and when the worktree has no unexpected entries beyond
    that candidate bot directory.
    """
    if not baseline_head or not current_head or baseline_head == current_head:
        return False, {}
    checkpoint = read_pipeline_checkpoint()
    if not checkpoint:
        return False, {}
    try:
        if candidate_v is None or int(checkpoint.get("next_v") or -1) != int(candidate_v):
            return False, {}
    except Exception:
        return False, {}
    stage = str(checkpoint.get("stage") or "")
    allowed_tools = head_drift_allowed_tools(stage)
    if tool_name not in allowed_tools:
        return False, {}
    resume_policy = head_drift_resume_policy(stage) or {}
    current_branch = _branch_name(str(snapshot.get("branch") or ""))
    branch_alias_allowed = _branch_alias_allowed_for_tool(tool_name, snapshot)
    if current_branch != EVOLUTION_BRANCH and not branch_alias_allowed:
        return False, {}
    unexpected = _unexpected_entries(snapshot, candidate_v)
    if unexpected:
        return False, {"unexpected_entries": unexpected[:40]}
    return True, {
        "stage": stage,
        "candidate_v": candidate_v,
        "baseline_head": baseline_head,
        "current_head": current_head,
        "branch": snapshot.get("branch"),
        "branch_alias_allowed": branch_alias_allowed,
        "allowed_tools": sorted(allowed_tools),
        "resume_kind": resume_policy.get("resume_kind", "checkpoint"),
    }


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _log_guard_event(event_type: str, severity: str, message: str, data: dict[str, Any]) -> None:
    try:
        from system_log import log_system_event
        log_system_event(event_type, severity, message, data)
    except Exception:
        pass


def _branch_name(branch_status: str) -> str:
    return (branch_status or "").split("...", 1)[0].split()[0]


def _runtime_expected_head() -> str:
    return os.environ.get("POK_RUNTIME_EXPECTED_HEAD", "").strip()


def _branch_alias_allowed_for_tool(tool_name: str, snapshot: dict[str, Any]) -> bool:
    """Allow non-commit tools when only the branch name changed.

    The orchestrator stores its startup HEAD in ``POK_RUNTIME_EXPECTED_HEAD``.
    A branch switch to another name at that same commit does not change the
    files seen by planners, workers, or evaluators. ``commit_bot`` remains
    canonical-branch-only so a final accepted bot is not committed elsewhere.
    """
    if tool_name == "commit_bot":
        return False
    current_branch = _branch_name(str(snapshot.get("branch") or ""))
    if not current_branch or current_branch == EVOLUTION_BRANCH:
        return False
    expected_head = _runtime_expected_head()
    current_head = str(snapshot.get("head") or "").strip()
    return bool(expected_head and current_head and current_head == expected_head)


def _unrelated_head_drift_allowed(
    *,
    candidate_v: int | None,
    source_v: int | None = None,
    baseline_head: str,
    current_head: str,
) -> tuple[bool, dict[str, Any]]:
    checkpoint = read_pipeline_checkpoint()
    allowed, payload = evaluate_head_drift(
        PROJECT_ROOT,
        baseline_head,
        current_head,
        candidate_v=candidate_v,
        source_v=source_v,
        checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
    )
    contract_paths = list(payload.get("head_contract_paths") or [])
    candidate_prefix = bot_relpath(candidate_v) + "/" if candidate_v is not None else ""
    candidate_entries = [
        f"?? {path}" for path in contract_paths
        if candidate_prefix and path.startswith(candidate_prefix)
    ]
    blocking_entries = [
        f"?? {path}" for path in contract_paths
        if not candidate_prefix or not path.startswith(candidate_prefix)
    ]
    payload["head_candidate_entries"] = candidate_entries[:40]
    payload["head_blocking_entries"] = blocking_entries[:40]
    return allowed, payload


def _branch_head_drift_unrelated_allowed(
    *,
    tool_name: str,
    candidate_v: int | None,
    source_v: int | None,
    snapshot: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    if tool_name == "commit_bot":
        return False, {}
    current_branch = _branch_name(str(snapshot.get("branch") or ""))
    if not current_branch or current_branch == EVOLUTION_BRANCH:
        return False, {}
    expected_head = _runtime_expected_head()
    current_head = str(snapshot.get("head") or "").strip()
    if not expected_head or not current_head or expected_head == current_head:
        return False, {}
    allowed, payload = _unrelated_head_drift_allowed(
        candidate_v=candidate_v,
        source_v=source_v,
        baseline_head=expected_head,
        current_head=current_head,
    )
    if not allowed:
        return False, payload
    return True, {
        "tool": tool_name,
        "candidate_v": candidate_v,
        "source_v": source_v,
        "branch": snapshot.get("branch"),
        "expected_branch": EVOLUTION_BRANCH,
        "runtime_expected_head": expected_head,
        "head": current_head,
        **payload,
    }


def ensure_runtime_git_guard(tool_name: str, args: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    """Ensure mutating pipeline tools run on the canonical branch and clean codebase."""
    args = args or {}
    if not _guard_enabled():
        return True, {"guard": "disabled"}

    candidate_v = _candidate_version(tool_name, args)
    source_v = _source_version(args)
    before = _snapshot()
    current_branch = _branch_name(str(before.get("branch") or ""))
    branch_alias_allowed = _branch_alias_allowed_for_tool(tool_name, before)
    branch_head_drift_unrelated_allowed, branch_head_drift_payload = _branch_head_drift_unrelated_allowed(
        tool_name=tool_name,
        candidate_v=candidate_v,
        source_v=source_v,
        snapshot=before,
    )

    if before.get("truncated"):
        payload = {
            "blocked": True,
            "reason": "worktree_snapshot_truncated",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": before.get("branch"),
            "head": before.get("head"),
            "entry_count": before.get("entry_count"),
            "entries": (before.get("entries") or [])[:40],
            "directive": "The worktree has too many dirty entries to audit safely. Stop and inspect the full git status before retrying.",
        }
        _log_guard_event(
            "repo.runtime_guard_blocked",
            "error",
            "Runtime git guard blocked truncated worktree snapshot",
            payload,
        )
        return False, payload

    if (
        current_branch
        and current_branch != EVOLUTION_BRANCH
        and not branch_alias_allowed
        and not branch_head_drift_unrelated_allowed
    ):
        unexpected = _unexpected_entries(before, candidate_v)
        payload = {
            "blocked": True,
            "reason": "branch_drift",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": before.get("branch"),
            "expected_branch": EVOLUTION_BRANCH,
            "head": before.get("head"),
            "unexpected_entries": unexpected[:40],
            "directive": (
                "Runtime evolution tools must run from the canonical evolution "
                "branch. Stop the service, inspect this branch/worktree, and "
                "switch branches explicitly before restarting; the guard will "
                "not auto-checkout."
            ),
        }
        _log_guard_event(
            "repo.runtime_guard_blocked",
            "error",
            f"Runtime git guard blocked branch drift before {tool_name}",
            payload,
        )
        return False, payload
    if branch_alias_allowed:
        _log_guard_event(
            "repo.runtime_guard_branch_alias_allowed",
            "warn",
            f"Runtime git guard allowed {tool_name} on branch alias with unchanged HEAD",
            {
                "tool": tool_name,
                "candidate_v": candidate_v,
                "branch": before.get("branch"),
                "expected_branch": EVOLUTION_BRANCH,
                "runtime_expected_head": _runtime_expected_head(),
                "head": before.get("head"),
            },
        )
    elif branch_head_drift_unrelated_allowed:
        _log_guard_event(
            "repo.runtime_guard_branch_head_drift_unrelated_allowed",
            "warn",
            f"Runtime git guard allowed {tool_name} on branch with unrelated HEAD drift",
            branch_head_drift_payload,
        )

    snapshot = _snapshot()
    if snapshot.get("truncated"):
        payload = {
            "blocked": True,
            "reason": "worktree_snapshot_truncated",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": snapshot.get("branch"),
            "head": snapshot.get("head"),
            "entry_count": snapshot.get("entry_count"),
            "entries": (snapshot.get("entries") or [])[:40],
            "directive": "The worktree has too many dirty entries to audit safely. Stop and inspect the full git status before retrying.",
        }
        _log_guard_event(
            "repo.runtime_guard_blocked",
            "error",
            "Runtime git guard blocked truncated worktree snapshot",
            payload,
        )
        return False, payload

    snapshot_branch = _branch_name(str(snapshot.get("branch") or ""))
    snapshot_branch_alias_allowed = _branch_alias_allowed_for_tool(tool_name, snapshot)
    snapshot_branch_head_drift_unrelated_allowed, snapshot_branch_head_drift_payload = (
        _branch_head_drift_unrelated_allowed(
            tool_name=tool_name,
            candidate_v=candidate_v,
            source_v=source_v,
            snapshot=snapshot,
        )
    )
    branch_alias_allowed = branch_alias_allowed or snapshot_branch_alias_allowed
    branch_head_drift_unrelated_allowed = (
        branch_head_drift_unrelated_allowed or snapshot_branch_head_drift_unrelated_allowed
    )
    if (
        snapshot_branch
        and snapshot_branch != EVOLUTION_BRANCH
        and not snapshot_branch_alias_allowed
        and not snapshot_branch_head_drift_unrelated_allowed
    ):
        unexpected = _unexpected_entries(snapshot, candidate_v)
        payload = {
            "blocked": True,
            "reason": "branch_drift",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": snapshot.get("branch"),
            "expected_branch": EVOLUTION_BRANCH,
            "head": snapshot.get("head"),
            "unexpected_entries": unexpected[:40],
            "directive": (
                "Runtime evolution tools must run from the canonical evolution "
                "branch, except for non-commit tools on a branch alias with the "
                "unchanged runtime HEAD."
            ),
        }
        _log_guard_event(
            "repo.runtime_guard_blocked",
            "error",
            f"Runtime git guard blocked branch drift before {tool_name}",
            payload,
        )
        return False, payload
    if snapshot_branch_head_drift_unrelated_allowed and not branch_alias_allowed:
        _log_guard_event(
            "repo.runtime_guard_branch_head_drift_unrelated_allowed",
            "warn",
            f"Runtime git guard allowed {tool_name} on branch with unrelated HEAD drift",
            snapshot_branch_head_drift_payload,
        )

    baseline = _checkpoint_repo_baseline(candidate_v) or get_last_snapshot() or {}
    baseline_head = baseline.get("head") or ""
    current_head = snapshot.get("head") or ""
    head_drift_allowance: dict[str, Any] = {}
    enforce_head_stability = tool_name != "prepare_generation" and candidate_v is not None
    if (
        enforce_head_stability
        and tool_name not in _HEAD_CHANGE_ALLOWED_TOOLS
        and baseline_head
        and current_head
        and baseline_head != current_head
    ):
        unrelated_allowed, unrelated_payload = _unrelated_head_drift_allowed(
            candidate_v=candidate_v,
            source_v=source_v,
            baseline_head=baseline_head,
            current_head=current_head,
        )
        if unrelated_allowed:
            _log_guard_event(
                "repo.runtime_guard_head_drift_unrelated_allowed",
                "warn",
                f"Runtime git guard allowed {tool_name} after unrelated HEAD change",
                {
                    "tool": tool_name,
                    "candidate_v": candidate_v,
                    "baseline_head": baseline_head,
                    "current_head": current_head,
                    **unrelated_payload,
                },
            )
            head_drift_allowance = {
                "guard": "ok",
                "head_drift_unrelated_allowed": True,
                "candidate_v": candidate_v,
                "baseline_head": baseline_head,
                "current_head": current_head,
                **unrelated_payload,
            }
        else:
            allowed, allowed_payload = _head_change_allowed_for_checkpoint_resume(
                tool_name=tool_name,
                candidate_v=candidate_v,
                baseline_head=baseline_head,
                current_head=current_head,
                snapshot=snapshot,
            )
            if allowed:
                _log_guard_event(
                    "repo.runtime_guard_head_drift_repair_allowed",
                    "warn",
                    f"Runtime git guard allowed {tool_name} after infrastructure HEAD change",
                    allowed_payload,
                )
                head_drift_allowance = {
                    "guard": "ok",
                    "head_drift_resume_allowed": True,
                    "head_drift_repair_allowed": allowed_payload.get("resume_kind") == "repair",
                    **allowed_payload,
                }
            else:
                payload = {
                    "blocked": True,
                    "reason": "head_changed_during_generation",
                    "tool": tool_name,
                    "candidate_v": candidate_v,
                    "baseline_head": baseline_head,
                    "current_head": current_head,
                    "baseline_source": "checkpoint" if baseline.get("captured_stage") else "process_snapshot",
                    "branch": snapshot.get("branch"),
                    **unrelated_payload,
                    "directive": "A git commit changed the runtime code during this generation. Abandon and restart from a fresh baseline.",
                }
                _log_guard_event("repo.runtime_guard_blocked", "error", "Runtime git guard blocked HEAD drift", payload)
                return False, payload

    unexpected = _unexpected_entries(snapshot, candidate_v)
    if unexpected:
        ignored = _ignored_entries(snapshot, candidate_v)
        payload = {
            "blocked": True,
            "reason": "unexpected_worktree_entries",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": snapshot.get("branch"),
            "head": snapshot.get("head"),
            "unexpected_entries": unexpected[:40],
            "ignored_entries": ignored[:40],
            "ignored_count": len(ignored),
            "generated_bot_dirs": [
                line for line in snapshot.get("entries", []) or []
                if is_generated_bot_dir_entry(line)
            ][:40],
            "directive": "Unexpected repository changes appeared during evolution. Stop, inspect, then abandon or clean before retrying.",
        }
        _log_guard_event("repo.runtime_guard_blocked", "error", "Runtime git guard blocked unexpected worktree entries", payload)
        return False, payload

    absorbed_ok, absorbed_payload = (
        _absorb_committed_crossover_intent_before_route()
    )
    if not absorbed_ok:
        payload = {
            "error": "pipeline_crossover_projection_recovery_blocked",
            "tool": tool_name,
            "candidate_v": candidate_v,
            **absorbed_payload,
        }
        _log_guard_event(
            "pipeline.crossover_projection_absorber_blocked",
            "error",
            "Runtime guard could not reconcile a committed crossover intent",
            payload,
        )
        return False, payload

    route_ok, route_payload = _pipeline_route_guard(
        tool_name=tool_name,
        args=args,
        candidate_v=candidate_v,
        source_v=source_v,
    )
    if not route_ok:
        return False, route_payload

    ignored = _ignored_entries(snapshot, candidate_v)
    return True, {
        "guard": "ok",
        "tool": tool_name,
        "candidate_v": candidate_v,
        "branch": snapshot.get("branch"),
        "head": snapshot.get("head"),
        "branch_alias_allowed": branch_alias_allowed,
        "branch_head_drift_unrelated_allowed": branch_head_drift_unrelated_allowed,
        "ignored_entries": ignored[:40],
        "ignored_count": len(ignored),
        **(
            {"crossover_projection_absorber": absorbed_payload}
            if absorbed_payload
            else {}
        ),
        **head_drift_allowance,
    }


def tool(name: str, description: str, input_schema: dict[str, Any]):
    """claude_agent_sdk.tool wrapper with runtime git/worktree validation."""
    def decorator(func: Callable[..., Any]):
        @functools.wraps(func)
        async def guarded(args: dict[str, Any]):
            ok, payload = ensure_runtime_git_guard(name, args)
            if not ok:
                return _json_tool_result({
                    "error": "runtime_git_guard_blocked",
                    **payload,
                })
            result = func(args)
            if inspect.isawaitable(result):
                return await result
            return result

        return sdk_tool(name, description, input_schema)(guarded)

    return decorator
