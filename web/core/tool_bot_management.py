"""Bot lifecycle management: MCP reaping/abandonment and guarded cleanup."""

import fcntl
import hashlib
import json
import math
import os
from contextlib import nullcontext
from pathlib import Path
import sqlite3
import stat
import subprocess
import time
from typing import TypedDict

from bot_namespace import FIRST_STRICT_POLICY_VERSION, bot_name, parse_bot_version
from tool_runtime_guard import tool

from evolution_core import (
    get_active_bots, get_bot_dir, load_ratings,
    clear_pipeline_checkpoint, git_has_tag, git_dir_is_committed,
    MAX_ACTIVE_BOTS, RESULTS_DIR,
    Glicko2Player,
)
from tool_helpers import (
    load_h2h_avg_winrates, load_strength_scores, PROJECT_ROOT,
)
from system_log import log_system_event

from evolution_infra import (
    EVOLUTION_BRANCH,
    MAX_PRECOMMIT_RETRIES,
    BOTS_DIR,
    append_abandoned_version_receipt,
    bot_publication_lock,
    git_has_publication_ref,
    load_abandoned_version_receipts,
    recorded_abandon_receipt_for_checkpoint,
    read_pipeline_checkpoint,
    record_reaped_bot,
    _fsync_regular_state_file_and_parent,
    _git as _evolution_git,
)
from pipeline_state import generic_abandon_block
from bot_artifact import canonical_digest
from bot_namespace import EVALUATION_EPOCH
from epoch_authority import (
    schema2_abandon_quarantine_contract,
    schema2_abandon_receipt_identity,
    schema2_abandon_transaction_preimage,
    schema3_abandon_transaction_preimage,
    validate_abandon_claim_structure,
    validate_abandon_finalize_receipt,
    validate_abandon_ledger_history,
)

# A4 (2026-06-30): rate-limit state for abandon_generation. [timestamp, reason].
_LAST_ABANDON_TS = [0.0, ""]
_TERMINAL_REASON_MAX_CHARS = 1000
_FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY = (
    "first_strict_control_execution_scope"
)


def _fence_first_strict_control_execution(
    checkpoint: dict,
    *,
    reason: str,
) -> dict:
    """Terminalize only the journal authority frozen by this checkpoint."""

    audit_context = checkpoint.get("audit_context") or {}
    if not isinstance(audit_context, dict):
        raise RuntimeError("first_strict_execution_audit_context_invalid")
    precommit_plan = audit_context.get("precommit_eval_plan")
    plan_opponents = (
        precommit_plan.get("opponents")
        if isinstance(precommit_plan, dict)
        else []
    )
    declares_control = any(
        isinstance(item, dict)
        and item.get("authority") == "system_first_strict_control"
        for item in (plan_opponents or [])
    )
    scope_present = _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY in audit_context
    precommit_attempt = checkpoint.get("precommit_attempt")
    if not declares_control and not scope_present:
        return {
            "present": False,
            "reason": "first_strict_execution_scope_not_declared",
        }
    if declares_control != scope_present:
        if declares_control and (
            type(precommit_attempt) is not int or precommit_attempt < 1
        ):
            return {
                "present": False,
                "reason": "first_strict_execution_not_started",
            }
        raise RuntimeError("first_strict_execution_plan_scope_mismatch")
    try:
        from first_strict_execution_journal import abandon_control_execution
        from precommit_eval_contract import (
            build_evaluation_contract,
            opponents_from_plan,
        )
        from tool_eval import _validate_first_strict_control_execution_scope
        from tool_gates import _bot_code_fingerprint

        version = int(checkpoint["next_v"])
        source_v = int(checkpoint["source_v"])
        candidate_name = bot_name(version)
        code_fingerprint = _bot_code_fingerprint(get_bot_dir(version))
        opponents = opponents_from_plan(precommit_plan)
        evaluation_contract = build_evaluation_contract(
            precommit_plan,
            candidate_code_fingerprint=code_fingerprint,
        )
        normalized_scope, scope_error = (
            _validate_first_strict_control_execution_scope(
                audit_context.get(
                    _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
                ),
                v=version,
                candidate_name=candidate_name,
                code_fingerprint=code_fingerprint,
                opponents=opponents,
                precommit_plan=precommit_plan,
                evaluation_contract=evaluation_contract,
                workflow_run_id=str(
                    checkpoint.get("workflow_run_id") or ""
                ),
                precommit_attempt=int(precommit_attempt),
            )
        )
        if scope_error or normalized_scope is None:
            raise RuntimeError(scope_error or "scope normalization failed")
        receipt = abandon_control_execution(
            normalized_scope,
            reason=reason,
        )
    except Exception as exc:
        raise RuntimeError(
            "first_strict_execution_fence_invalid:"
            f"{type(exc).__name__}:{str(exc)[:300]}"
        ) from exc
    return {
        "present": True,
        "abandoned": True,
        "scope": normalized_scope,
        "terminal_receipt": receipt,
        "proof_digest": canonical_digest({
            "scope": normalized_scope,
            "terminal_receipt": receipt,
        }),
    }


def _fsync_parent_directory(path) -> None:
    descriptor = os.open(
        str(path.parent),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _candidate_tree_manifest(path: Path) -> dict:
    """Bind the exact disposable candidate preimage without following links."""

    root = Path(path)
    metadata = os.lstat(root)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        raise RuntimeError("candidate_not_single_link_directory")
    entries: list[dict] = []
    total_bytes = 0
    for child in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = child.relative_to(root).as_posix()
        if len(entries) >= 10_000 or len(Path(relative).parts) > 32:
            raise RuntimeError("candidate_manifest_entry_or_depth_limit")
        child_stat = os.lstat(child)
        if stat.S_ISLNK(child_stat.st_mode):
            raise RuntimeError(f"candidate_unsafe_entry:{relative}")
        if stat.S_ISDIR(child_stat.st_mode):
            entries.append({"path": relative, "kind": "directory"})
            continue
        if not stat.S_ISREG(child_stat.st_mode):
            raise RuntimeError(f"candidate_special_entry:{relative}")
        if child_stat.st_nlink != 1:
            raise RuntimeError(f"candidate_hardlink_entry:{relative}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(child, flags)
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > 64 * 1024 * 1024:
                    raise RuntimeError("candidate_manifest_too_large")
            raw = b"".join(chunks)
            opened_after = os.fstat(descriptor)
            live = os.lstat(child)
            if (
                opened.st_nlink != 1
                or opened_after.st_nlink != 1
                or live.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (opened_after.st_dev, opened_after.st_ino)
                or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (
                    opened_after.st_size,
                    opened_after.st_mtime_ns,
                    opened_after.st_ctime_ns,
                )
                or (opened_after.st_dev, opened_after.st_ino)
                != (live.st_dev, live.st_ino)
                or opened.st_size != len(raw)
            ):
                raise RuntimeError(f"candidate_changed_while_read:{relative}")
        finally:
            os.close(descriptor)
        entries.append({
            "path": relative,
            "kind": "file",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    return {
        "manifest_digest": canonical_digest(entries),
        "entry_count": len(entries),
        "total_bytes": total_bytes,
    }


def _write_json_exclusive_durable(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise RuntimeError("abandon_transaction_parent_unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        raw = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("abandon transaction write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_parent_directory(path)


def _read_json_regular(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, 1024 * 1024 + 1)
        opened_after = os.fstat(descriptor)
        live = os.lstat(path)
        if (
            len(raw) > 1024 * 1024
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(opened_after.st_mode)
            or not stat.S_ISREG(live.st_mode)
            or opened.st_nlink != 1
            or opened_after.st_nlink != 1
            or live.st_nlink != 1
            or opened.st_size != len(raw)
            or (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                opened_after.st_dev,
                opened_after.st_ino,
                opened_after.st_size,
                opened_after.st_mtime_ns,
                opened_after.st_ctime_ns,
            )
            or (
                opened_after.st_dev,
                opened_after.st_ino,
                opened_after.st_size,
                opened_after.st_mtime_ns,
                opened_after.st_ctime_ns,
            ) != (
                live.st_dev,
                live.st_ino,
                live.st_size,
                live.st_mtime_ns,
                live.st_ctime_ns,
            )
        ):
            raise RuntimeError("abandon_transaction_json_unsafe")
    finally:
        os.close(descriptor)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("abandon_transaction_json_not_object")
    return value


def _ensure_durable_json(path: Path, payload: dict) -> None:
    if os.path.lexists(path):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            live_before = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not stat.S_ISREG(live_before.st_mode)
                or live_before.st_nlink != 1
                or (opened.st_dev, opened.st_ino, opened.st_size)
                != (live_before.st_dev, live_before.st_ino, live_before.st_size)
            ):
                raise RuntimeError("abandon_transaction_json_unsafe")
            raw = os.read(descriptor, 1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RuntimeError("abandon_transaction_json_unsafe")
            observed = json.loads(raw.decode("utf-8"))
            if observed != payload:
                raise RuntimeError("abandon_transaction_claim_conflict")
            os.fsync(descriptor)
            opened_after = os.fstat(descriptor)
            live_after = os.lstat(path)
            if (
                opened_after.st_nlink != 1
                or live_after.st_nlink != 1
                or opened.st_size != len(raw)
                or (
                    opened_after.st_dev,
                    opened_after.st_ino,
                    opened_after.st_size,
                    opened_after.st_mtime_ns,
                    opened_after.st_ctime_ns,
                ) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
                or (
                    live_after.st_dev,
                    live_after.st_ino,
                    live_after.st_size,
                    live_after.st_mtime_ns,
                    live_after.st_ctime_ns,
                ) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
            ):
                raise RuntimeError("abandon_transaction_json_changed")
        finally:
            os.close(descriptor)
        _fsync_parent_directory(path)
        return
    _write_json_exclusive_durable(path, payload)


def _checkpoint_transaction_identity(checkpoint: dict) -> dict:
    return {
        "digest": canonical_digest(checkpoint),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "stage": checkpoint.get("stage"),
        "workflow_run_id": str(
            checkpoint.get("workflow_run_id")
            or checkpoint.get("run_id")
            or ""
        ),
        "checkpoint_revision": checkpoint.get("checkpoint_revision"),
    }


def _current_abandon_git_state(version: int) -> dict:
    """Capture every Git predicate whose absence permits quarantine."""

    target = int(version)
    head = _evolution_git("rev-parse", "HEAD")
    tracked_status = _evolution_git(
        "status",
        "--porcelain",
        "--untracked-files=no",
    )
    state = {
        "head": head,
        "tracked_worktree_clean": tracked_status == "",
        "candidate_tracked": bool(git_dir_is_committed(target)),
        "publication_refs": {
            f"national-bot-v{target}": bool(git_has_publication_ref(target)),
            f"national-high-water-v{target}": bool(
                git_has_publication_ref(target)
            ),
        },
    }
    if (
        state["tracked_worktree_clean"] is not True
        or state["candidate_tracked"] is not False
        or any(state["publication_refs"].values())
    ):
        raise RuntimeError("recorded_abandon_git_state_not_disposable")
    return state


def _abandon_ledger_claim(checkpoint: dict, reason: str) -> dict:
    rows = load_abandoned_version_receipts(
        path=Path(RESULTS_DIR) / "abandoned_versions.jsonl",
        project_root=PROJECT_ROOT,
    )
    return {
        "path_contract": "RESULTS_DIR/abandoned_versions.jsonl",
        "prior_receipt_count": len(rows),
        "prior_receipt_head_digest": (
            rows[-1]["receipt_digest"] if rows else None
        ),
        "receipt_identity": schema2_abandon_receipt_identity(
            _checkpoint_transaction_identity(checkpoint),
            str(reason),
        ),
    }


def _assert_safe_existing_transaction_chain(transaction_dir: Path) -> None:
    """Reject links/special files in every existing derived path ancestor."""

    results = Path(RESULTS_DIR)
    try:
        relative = transaction_dir.relative_to(results)
    except ValueError as exc:
        raise RuntimeError("abandon_transaction_path_escaped_results") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(results, flags)
    try:
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise RuntimeError("abandon_transaction_path_not_canonical")
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        raise RuntimeError("abandon_transaction_directory_unsafe") from exc
    finally:
        os.close(descriptor)


def _validate_active_abandon_claim(claim: dict) -> dict:
    """Reopen all live authority; never trust a merely re-signed sidecar."""

    validate_abandon_claim_structure(claim)
    _validate_claim_first_strict_execution_fence(claim)
    checkpoint_identity = claim["checkpoint"]
    version = int(checkpoint_identity["next_v"])
    current_git = _current_abandon_git_state(version)
    if current_git != claim["git_state"]:
        raise RuntimeError("recorded_abandon_active_git_state_changed")

    transaction_dir, quarantine = _claim_transaction_paths(claim)
    _assert_safe_existing_transaction_chain(transaction_dir)
    transaction_claim = transaction_dir / "claim.json"
    if not os.path.lexists(transaction_claim):
        raise RuntimeError("recorded_abandon_transaction_claim_missing")
    if _read_json_regular(transaction_claim) != claim:
        raise RuntimeError("recorded_abandon_transaction_claim_mismatch")

    candidate = Path(get_bot_dir(version))
    state = _validate_claim_candidate_state(claim, candidate, quarantine)
    rows = load_abandoned_version_receipts(
        path=Path(RESULTS_DIR) / "abandoned_versions.jsonl",
        project_root=PROJECT_ROOT,
    )
    abandon_receipt = validate_abandon_ledger_history(
        claim,
        rows,
        require_active_head=True,
    )

    from evolution_core import PIPELINE_STATE_FILE

    checkpoint_path = Path(PIPELINE_STATE_FILE)
    checkpoint_exists = os.path.lexists(checkpoint_path)
    if checkpoint_exists:
        checkpoint = read_pipeline_checkpoint()
        if (
            not isinstance(checkpoint, dict)
            or canonical_digest(checkpoint) != checkpoint_identity["digest"]
            or _checkpoint_transaction_identity(checkpoint) != checkpoint_identity
        ):
            raise RuntimeError("recorded_abandon_active_checkpoint_changed")
        if abandon_receipt is None and state not in {"source", "absent"}:
            raise RuntimeError("recorded_abandon_phase_invalid_before_ledger")
    else:
        if abandon_receipt is None:
            raise RuntimeError("recorded_abandon_receipt_missing_after_checkpoint_clear")
        if claim["candidate"]["present"] is True and state != "quarantine":
            raise RuntimeError("recorded_abandon_source_invalid_after_checkpoint_clear")
        if claim["candidate"]["present"] is False and state != "absent":
            raise RuntimeError("recorded_abandon_absent_phase_invalid")

    finalize_path = transaction_dir / "receipt.json"
    if os.path.lexists(finalize_path):
        validate_abandon_finalize_receipt(
            claim,
            _read_json_regular(finalize_path),
            rows,
        )
    return claim


def _validate_claim_first_strict_execution_fence(
    claim: dict,
) -> dict | None:
    """Reopen the schema-3 journal receipt bound before checkpoint removal."""

    if claim.get("schema_version") != 3:
        return None
    fence = claim.get("first_strict_execution_fence")
    if not isinstance(fence, dict):
        raise RuntimeError("recorded_abandon_first_strict_fence_missing")
    try:
        from first_strict_execution_journal import (
            read_abandoned_control_execution,
        )

        observed = read_abandoned_control_execution(
            fence.get("scope"),
            reason=str(claim.get("abandon_reason") or ""),
            expected_terminal_receipt=fence.get("terminal_receipt"),
        )
    except Exception as exc:
        raise RuntimeError(
            "recorded_abandon_first_strict_fence_unverifiable:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        ) from exc
    if observed != fence.get("terminal_receipt"):
        raise RuntimeError("recorded_abandon_first_strict_fence_changed")
    return fence


def _validate_abandon_workflow_fences(
    *,
    workflow_run_id: str,
    abandon_reason: str,
    require_worker_outer_reason: bool,
    require_strict_authority: bool = True,
) -> dict:
    """Read-only proof of the Worker prefix and optional strict child fence."""

    from strict_authority_workflow import (
        DEFINITION_VERSION,
        authority_run_id,
        strict_authority_abandon_event_identity,
    )
    from worker_workflow import (
        WORKER_WORKFLOW_DEFINITION_VERSION,
        replay_worker_events,
        worker_abandon_event_identity,
    )
    from workflow_kernel import (
        KERNEL_SCHEMA_VERSION,
        WorkflowEvent,
        canonical_json,
        content_digest,
    )

    workflow_run_id = str(workflow_run_id or "")
    if not workflow_run_id:
        raise RuntimeError("completed_abandon_workflow_run_id_missing")
    strict_run_id = authority_run_id(workflow_run_id)
    database = Path(RESULTS_DIR) / "workflow" / "events.sqlite3"
    if not os.path.lexists(database):
        raise RuntimeError("completed_abandon_workflow_database_missing")
    metadata = os.lstat(database)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise RuntimeError("completed_abandon_workflow_database_unsafe")
    expected = Path(RESULTS_DIR).resolve() / "workflow" / "events.sqlite3"
    try:
        resolved = database.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            "completed_abandon_workflow_database_unavailable"
        ) from exc
    if resolved != expected:
        raise RuntimeError("completed_abandon_workflow_database_escaped")

    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    # The recorded claim carries the outer control-plane reason.  A Worker can
    # already be terminal before the outer actor fences the generation (for
    # example when the deterministic bootstrap executor rejects its own fixed
    # output).  In that case its journal intentionally retains the bounded
    # inner execution reason while the strict-authority child is fenced with
    # the outer reason.  Do not conflate the two identities: doing so makes a
    # correctly quarantined terminal generation impossible to re-prove after a
    # process restart.
    claim_outer_reason = str(abandon_reason)
    if not claim_outer_reason:
        raise RuntimeError("completed_abandon_outer_reason_missing")
    outer_reason = claim_outer_reason[:1000]
    try:
        connection.execute("PRAGMA query_only=ON")
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != KERNEL_SCHEMA_VERSION:
            raise RuntimeError("completed_abandon_workflow_schema_invalid")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("completed_abandon_workflow_foreign_key_invalid")
        connection.execute("BEGIN")

        def bounded_terminal_reason(
            payload: dict,
            *,
            event_type: str,
        ) -> str:
            reason = payload.get("reason")
            if (
                not isinstance(reason, str)
                or not reason
                # Both terminal producers slice with ``[:1000]``.  Exactly
                # 1000 characters is a valid immutable legacy/current event;
                # only a longer payload is malformed.
                or len(reason) > _TERMINAL_REASON_MAX_CHARS
            ):
                raise RuntimeError(
                    f"completed_abandon_{event_type}_reason_invalid"
                )
            return reason

        def terminal_projection(
            run_id: str,
            event_type: str,
            expected_definition_version: int,
            *,
            require_outer_reason: bool,
        ) -> dict:
            instance = connection.execute(
                "SELECT definition_version, stream_version, status, fence_epoch "
                "FROM workflow_instances WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if instance is None:
                raise RuntimeError(
                    f"completed_abandon_{event_type}_instance_missing"
                )
            history = connection.execute(
                "SELECT seq, event_type, schema_version, payload, "
                "payload_digest, causation_id FROM workflow_events "
                "WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
            stream_version = int(instance["stream_version"])
            if (
                len(history) != stream_version
                or [int(row["seq"]) for row in history]
                != list(range(1, stream_version + 1))
            ):
                raise RuntimeError(
                    f"completed_abandon_{event_type}_history_sequence_invalid"
                )
            decoded_history = []
            for row in history:
                try:
                    row_payload = json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        f"completed_abandon_{event_type}_history_payload_invalid"
                    ) from exc
                row_digest = hashlib.sha256(
                    canonical_json(row_payload).encode("utf-8")
                ).hexdigest()
                if (
                    int(row["schema_version"]) != 1
                    or row["payload_digest"] != row_digest
                ):
                    raise RuntimeError(
                        f"completed_abandon_{event_type}_history_digest_invalid"
                    )
                decoded_history.append((row, row_payload))
            events = [
                item
                for item in decoded_history
                if item[0]["event_type"] == event_type
            ]
            if len(events) != 1:
                raise RuntimeError(
                    f"completed_abandon_{event_type}_event_count_invalid"
                )
            event, payload = events[0]
            if (
                int(instance["definition_version"])
                != int(expected_definition_version)
                or instance["status"] != "abandoned"
                or int(instance["fence_epoch"]) < 1
                or int(event["seq"]) != stream_version
            ):
                raise RuntimeError(
                    f"completed_abandon_{event_type}_terminal_invalid"
                )
            terminal_reason = bounded_terminal_reason(
                payload,
                event_type=event_type,
            )
            if require_outer_reason and terminal_reason != outer_reason:
                raise RuntimeError(
                    f"completed_abandon_{event_type}_outer_reason_mismatch"
                )
            if event_type == "WorkerAbandoned":
                worker_events = [
                    WorkflowEvent(
                        run_id=run_id,
                        seq=int(row["seq"]),
                        event_type=str(row["event_type"]),
                        schema_version=int(row["schema_version"]),
                        payload=row_payload,
                        payload_digest=str(row["payload_digest"]),
                        causation_id=str(row["causation_id"]),
                    )
                    for row, row_payload in decoded_history
                ]
                try:
                    worker_state = replay_worker_events(
                        run_id,
                        worker_events,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "completed_abandon_WorkerAbandoned_replay_invalid"
                    ) from exc
                cycle = int(worker_state.get("cycle") or 0)
                expected_payload, expected_causation = (
                    worker_abandon_event_identity(
                        run_id,
                        reason=terminal_reason,
                        cycle=cycle,
                    )
                )
                expected_causations = {expected_causation}
                if (
                    terminal_reason == outer_reason
                    and claim_outer_reason != terminal_reason
                ):
                    expected_causations.add(
                        f"worker-abandoned:{run_id}:cycle-{cycle}:"
                        f"{content_digest(claim_outer_reason)}"
                    )
                if (
                    payload != expected_payload
                    or event["causation_id"] not in expected_causations
                ):
                    raise RuntimeError(
                        "completed_abandon_WorkerAbandoned_reason_unbound"
                    )
            if event_type == "StrictAuthorityAbandoned":
                expected_payload, expected_causation = (
                    strict_authority_abandon_event_identity(
                        {"workflow_run_id": workflow_run_id},
                        reason=terminal_reason,
                    )
                )
                if payload != expected_payload:
                    raise RuntimeError(
                        "completed_abandon_StrictAuthorityAbandoned_binding_invalid"
                    )
                if event["causation_id"] != expected_causation:
                    raise RuntimeError(
                        "completed_abandon_StrictAuthorityAbandoned_reason_unbound"
                    )
            live_effects = connection.execute(
                "SELECT COUNT(*) FROM effects WHERE run_id = ? "
                "AND status NOT IN ('completed', 'exhausted', 'abandoned')",
                (run_id,),
            ).fetchone()[0]
            if int(live_effects) != 0:
                raise RuntimeError(
                    f"completed_abandon_{event_type}_effects_still_live"
                )
            return {
                "run_id": run_id,
                "stream_version": int(instance["stream_version"]),
                "fence_epoch": int(instance["fence_epoch"]),
                "terminal_event": event_type,
                "terminal_reason": terminal_reason,
            }

        main = terminal_projection(
            workflow_run_id,
            "WorkerAbandoned",
            WORKER_WORKFLOW_DEFINITION_VERSION,
            require_outer_reason=require_worker_outer_reason,
        )
        strict = (
            terminal_projection(
                strict_run_id,
                "StrictAuthorityAbandoned",
                DEFINITION_VERSION,
                require_outer_reason=True,
            )
            if require_strict_authority
            else None
        )
        connection.rollback()
    finally:
        connection.close()
    after = os.lstat(database)
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        raise RuntimeError("completed_abandon_workflow_database_changed")
    proof = {"worker": main}
    if strict is not None:
        proof["strict_authority"] = strict
    return proof


def _validate_completed_abandon_workflow_fences(claim: dict) -> dict:
    """Reprove historical/finalized claim fences, including legacy Worker reasons."""

    return _validate_abandon_workflow_fences(
        workflow_run_id=str(claim["checkpoint"]["workflow_run_id"]),
        abandon_reason=str(claim["abandon_reason"]),
        require_worker_outer_reason=False,
        require_strict_authority=True,
    )


def _terminal_gate_abandon_identity(
    checkpoint: dict,
    *,
    reason: str,
) -> tuple[str, str]:
    if not isinstance(checkpoint, dict):
        raise RuntimeError("terminal_gate_abandon_checkpoint_invalid")
    outcome = checkpoint.get("terminal_gate_outcome")
    if not isinstance(outcome, dict):
        raise RuntimeError("terminal_gate_abandon_outcome_missing")
    workflow_run_id = str(checkpoint.get("workflow_run_id") or "")
    receipt_digest = str(outcome.get("receipt_digest") or "")
    expected_reason = f"terminal_gate_outcome:{receipt_digest}"
    if (
        checkpoint.get("stage")
        not in {"quality_rejected", "review_rejected", "critic_rejected"}
        or outcome.get("workflow_run_id") != workflow_run_id
        or outcome.get("terminal_stage") != checkpoint.get("stage")
        or len(receipt_digest) != 64
        or any(char not in "0123456789abcdef" for char in receipt_digest)
        or str(reason) != expected_reason
    ):
        raise RuntimeError("terminal_gate_abandon_fence_identity_invalid")
    return workflow_run_id, expected_reason


def validate_terminal_gate_abandon_fences(
    checkpoint: dict,
    *,
    reason: str,
) -> dict:
    """Prove the exact already-fenced lifecycle of one terminal gate receipt.

    This is the narrow bridge needed by canonical abandon's second state guard
    and by a crash retry after both journals were fenced.  It does not create,
    repair, or relax either journal.
    """

    workflow_run_id, expected_reason = _terminal_gate_abandon_identity(
        checkpoint,
        reason=reason,
    )
    return _validate_abandon_workflow_fences(
        workflow_run_id=workflow_run_id,
        abandon_reason=expected_reason,
        require_worker_outer_reason=True,
        require_strict_authority=True,
    )


def terminal_gate_abandon_fence_proof_if_present(
    checkpoint: dict,
    *,
    reason: str,
) -> dict | None:
    """Return exact terminal fence proof, or ``None`` before fencing begins.

    Seeing either journal already abandoned is an irreversible lifecycle
    boundary.  The exact Worker-first prefix may finish the strict fence after
    a crash; every mismatched prefix or strict-first/partial shape fails closed
    rather than being mistaken for the ordinary pre-fence validation pass.
    """

    from strict_authority_workflow import DEFINITION_VERSION, authority_run_id

    if not isinstance(checkpoint, dict):
        raise RuntimeError("terminal_gate_abandon_checkpoint_invalid")
    workflow_run_id, expected_reason = _terminal_gate_abandon_identity(
        checkpoint,
        reason=reason,
    )
    strict_run_id = authority_run_id(workflow_run_id)
    database = Path(RESULTS_DIR) / "workflow" / "events.sqlite3"
    if not os.path.lexists(database):
        return None
    metadata = os.lstat(database)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise RuntimeError("terminal_gate_abandon_workflow_database_unsafe")
    expected = Path(RESULTS_DIR).resolve() / "workflow" / "events.sqlite3"
    resolved = database.resolve(strict=True)
    if resolved != expected:
        raise RuntimeError("terminal_gate_abandon_workflow_database_escaped")
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=30.0,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT run_id, definition_version, stream_version, status, "
            "fence_epoch FROM workflow_instances "
            "WHERE run_id IN (?, ?)",
            (workflow_run_id, strict_run_id),
        ).fetchall()
        strict_event_count = int(connection.execute(
            "SELECT COUNT(*) FROM workflow_events WHERE run_id = ?",
            (strict_run_id,),
        ).fetchone()[0])
        strict_effect_count = int(connection.execute(
            "SELECT COUNT(*) FROM effects WHERE run_id = ?",
            (strict_run_id,),
        ).fetchone()[0])
    finally:
        connection.close()
    instances = {str(row[0]): row for row in rows}
    worker = instances.get(workflow_run_id)
    strict = instances.get(strict_run_id)
    worker_abandoned = worker is not None and str(worker[3]) == "abandoned"
    strict_abandoned = strict is not None and str(strict[3]) == "abandoned"
    if not worker_abandoned and not strict_abandoned:
        return None
    exact_legacy_strict_tombstone = bool(
        strict is not None
        and int(strict[1]) == DEFINITION_VERSION
        and int(strict[2]) == 0
        and str(strict[3]) == "abandoned"
        and int(strict[4]) == 0
        and strict_event_count == 0
        and strict_effect_count == 0
    )
    if worker_abandoned and exact_legacy_strict_tombstone:
        # This is recoverable only as the exact Worker-first prefix.  Never
        # expose a strict_authority key: authority_summary must still reject
        # review/critic receipts until abandon_authority appends the missing
        # canonical terminal event inside the action boundary.
        return _validate_abandon_workflow_fences(
            workflow_run_id=workflow_run_id,
            abandon_reason=expected_reason,
            require_worker_outer_reason=True,
            require_strict_authority=False,
        )
    if worker_abandoned and not strict_abandoned:
        if (
            strict is not None
            and (
                int(strict[1]) != DEFINITION_VERSION
                or (
                    int(strict[2]) == 0
                    and int(strict[4]) == 0
                    and strict_event_count == 0
                    and strict_effect_count == 0
                )
            )
        ):
            raise RuntimeError(
                "terminal_gate_abandon_strict_prefix_invalid"
            )
        return _validate_abandon_workflow_fences(
            workflow_run_id=workflow_run_id,
            abandon_reason=expected_reason,
            require_worker_outer_reason=True,
            require_strict_authority=False,
        )
    return validate_terminal_gate_abandon_fences(
        checkpoint,
        reason=reason,
    )


def validate_completed_abandon_handoff(
    checkpoint: dict,
    result: dict,
) -> dict:
    """Reprove the exact finalized abandon returned to one provider stream."""

    if not isinstance(checkpoint, dict) or not isinstance(result, dict):
        raise RuntimeError("completed_abandon_handoff_material_invalid")
    transaction_id = str(result.get("abandon_transaction_id") or "")
    if (
        len(transaction_id) != 64
        or any(char not in "0123456789abcdef" for char in transaction_id)
    ):
        raise RuntimeError("completed_abandon_transaction_id_invalid")
    transaction_dir = (
        Path(RESULTS_DIR)
        / "policy_epoch_abandon_transactions"
        / transaction_id
    )
    _assert_safe_existing_transaction_chain(transaction_dir)
    claim = _read_json_regular(transaction_dir / "claim.json")
    if claim.get("transaction_id") != transaction_id:
        raise RuntimeError("completed_abandon_transaction_identity_mismatch")
    baseline_identity = _checkpoint_transaction_identity(checkpoint)
    terminal_identity = claim.get("checkpoint")
    if not isinstance(terminal_identity, dict):
        raise RuntimeError("completed_abandon_checkpoint_identity_invalid")
    if any(
        terminal_identity.get(field) != baseline_identity.get(field)
        for field in ("workflow_run_id", "next_v", "source_v")
    ):
        raise RuntimeError("completed_abandon_checkpoint_identity_mismatch")
    baseline_revision = baseline_identity.get("checkpoint_revision")
    terminal_revision = terminal_identity.get("checkpoint_revision")
    if (
        type(baseline_revision) is not int
        or baseline_revision < 1
        or type(terminal_revision) is not int
        or terminal_revision < baseline_revision
    ):
        raise RuntimeError("completed_abandon_checkpoint_revision_invalid")
    _validate_active_abandon_claim(claim)
    workflow_fences = _validate_completed_abandon_workflow_fences(claim)
    finalize_path = transaction_dir / "receipt.json"
    if not os.path.lexists(finalize_path):
        raise RuntimeError("completed_abandon_finalize_receipt_missing")
    finalize_receipt = _read_json_regular(finalize_path)
    rows = load_abandoned_version_receipts(
        path=Path(RESULTS_DIR) / "abandoned_versions.jsonl",
        project_root=PROJECT_ROOT,
    )
    abandon_receipt = validate_abandon_ledger_history(
        claim,
        rows,
        require_active_head=True,
    )
    validate_abandon_finalize_receipt(
        claim,
        finalize_receipt,
        rows,
    )
    from evolution_core import PIPELINE_STATE_FILE

    live_claim = Path(RESULTS_DIR) / "policy_epoch_reconciliation_claim.json"
    if os.path.lexists(PIPELINE_STATE_FILE) or os.path.lexists(live_claim):
        raise RuntimeError("completed_abandon_terminal_paths_still_live")
    expected_result = {
        "abandoned": True,
        "cleared_checkpoint": True,
        "workflow_run_id": baseline_identity["workflow_run_id"],
        "abandon_transaction_id": transaction_id,
        "abandon_receipt_digest": abandon_receipt.get("receipt_digest"),
        "finalize_receipt_digest": finalize_receipt.get("receipt_digest"),
        "abandon_checkpoint_identity": terminal_identity,
        "first_strict_execution_fence": (
            claim.get("first_strict_execution_fence")
            if claim.get("schema_version") == 3
            else result.get("first_strict_execution_fence")
        ),
    }
    for field, value in expected_result.items():
        if result.get(field) != value:
            raise RuntimeError(f"completed_abandon_result_{field}_mismatch")
    return {
        "transaction_id": transaction_id,
        "abandon_receipt_digest": abandon_receipt["receipt_digest"],
        "finalize_receipt_digest": finalize_receipt["receipt_digest"],
        "checkpoint_identity": terminal_identity,
        "workflow_fences": workflow_fences,
        "first_strict_execution_fence": claim.get(
            "first_strict_execution_fence"
        ),
    }


def _historical_head_is_ancestor(
    ancestor_head: str,
    descendant_head: str,
) -> bool:
    """Return whether a recorded commit is in the checked-out main lineage."""

    if not ancestor_head or not descendant_head:
        return False
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor_head, descendant_head],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    return proc.returncode == 0


def _historical_completed_abandon_source_proof(claim: dict) -> dict:
    """Bind a finalized abandon to a clean, fetched descendant of its main.

    A completed terminal transaction has no live checkpoint to replay.  The
    only safe source transition is therefore from the exact commit recorded in
    the immutable claim to the current, fetched ``origin/main`` descendant.
    This is deliberately narrower than normal active-checkpoint head drift:
    it proves historical termination only and cannot resume the cleared plan.
    """

    recorded_head = str((claim.get("git_state") or {}).get("head") or "")
    remote_main_ref = f"refs/remotes/origin/{EVOLUTION_BRANCH}"
    try:
        resolved_recorded = _evolution_git(
            "rev-parse", f"{recorded_head}^{{commit}}"
        )
        current_head = _evolution_git("rev-parse", "HEAD")
        remote_main_head = _evolution_git("rev-parse", remote_main_ref)
        current_branch = _evolution_git("branch", "--show-current")
        tracked_status = _evolution_git(
            "status", "--porcelain", "--untracked-files=no"
        )
    except Exception as exc:
        raise RuntimeError(
            "historical_completed_abandon_git_identity_unavailable"
        ) from exc

    if resolved_recorded != recorded_head:
        raise RuntimeError("historical_completed_abandon_recorded_head_invalid")
    if current_branch != EVOLUTION_BRANCH:
        raise RuntimeError("historical_completed_abandon_not_on_main")
    if current_head != remote_main_head:
        raise RuntimeError("historical_completed_abandon_main_not_fetched")
    if tracked_status:
        raise RuntimeError("historical_completed_abandon_tracked_worktree_dirty")
    if not _historical_head_is_ancestor(recorded_head, current_head):
        raise RuntimeError("historical_completed_abandon_main_not_descendant")
    return {
        "recorded_git_head": recorded_head,
        "current_git_head": current_head,
        "remote_main_ref": remote_main_ref,
        "remote_main_head": remote_main_head,
        "source_descendant_verified": True,
    }


def reprove_historical_completed_abandon(transaction_id: str) -> dict:
    """Read-only reproof for one finalized, checkpoint-free abandon.

    This recovery path intentionally has no ``checkpoint`` or provider-result
    argument.  It may consume only the existing schema-2 claim, finalized
    receipt, append-only ledger, and fenced workflow journals.  It refuses a
    live/unfinalized transaction, a later ledger head, a resurrected candidate,
    or a source checkout that is not a clean fetched descendant of the exact
    recorded main commit.  It never clears, rewrites, or synthesizes runtime
    state; a caller may use the returned proof solely to authorize a fresh
    post-terminal prepare on current main.
    """

    if not _is_autonomous_runtime_checkout():
        raise RuntimeError(
            "historical_completed_abandon_requires_autonomous_runtime_checkout"
        )
    transaction_id = str(transaction_id or "")
    if (
        len(transaction_id) != 64
        or any(char not in "0123456789abcdef" for char in transaction_id)
    ):
        raise RuntimeError("historical_completed_abandon_transaction_id_invalid")

    transaction_dir = (
        Path(RESULTS_DIR)
        / "policy_epoch_abandon_transactions"
        / transaction_id
    )
    _assert_safe_existing_transaction_chain(transaction_dir)
    claim = _read_json_regular(transaction_dir / "claim.json")
    validate_abandon_claim_structure(claim)
    _validate_claim_first_strict_execution_fence(claim)
    if claim.get("transaction_id") != transaction_id:
        raise RuntimeError(
            "historical_completed_abandon_transaction_identity_mismatch"
        )

    from evolution_core import PIPELINE_STATE_FILE

    live_claim = Path(RESULTS_DIR) / "policy_epoch_reconciliation_claim.json"
    if os.path.lexists(PIPELINE_STATE_FILE) or os.path.lexists(live_claim):
        raise RuntimeError("historical_completed_abandon_terminal_paths_live")

    finalize_path = transaction_dir / "receipt.json"
    if not os.path.lexists(finalize_path):
        raise RuntimeError("historical_completed_abandon_finalize_receipt_missing")
    finalize_receipt = _read_json_regular(finalize_path)
    rows = load_abandoned_version_receipts(
        path=Path(RESULTS_DIR) / "abandoned_versions.jsonl",
        project_root=PROJECT_ROOT,
    )
    # Unlike generic historical receipt validation, terminal handoff must be
    # the current ledger tip.  A later abandon means a successor lifecycle has
    # already consumed this boundary and this reproof is not actionable.
    abandon_receipt = validate_abandon_ledger_history(
        claim,
        rows,
        require_active_head=True,
    )
    if abandon_receipt is None:
        raise RuntimeError("historical_completed_abandon_receipt_missing")
    validate_abandon_finalize_receipt(
        claim,
        finalize_receipt,
        rows,
    )

    version = int(claim["checkpoint"]["next_v"])
    candidate = Path(get_bot_dir(version))
    _transaction_dir, quarantine = _claim_transaction_paths(claim)
    candidate_state = _validate_claim_candidate_state(
        claim,
        candidate,
        quarantine,
    )
    expected_candidate_state = (
        "quarantine" if claim["candidate"]["present"] else "absent"
    )
    if candidate_state != expected_candidate_state:
        raise RuntimeError(
            "historical_completed_abandon_candidate_not_finalized"
        )
    if git_dir_is_committed(version) or git_has_publication_ref(version):
        raise RuntimeError("historical_completed_abandon_candidate_published")

    source = _historical_completed_abandon_source_proof(claim)
    workflow_fences = _validate_completed_abandon_workflow_fences(claim)
    return {
        "kind": "national-policy-historical-completed-abandon-reproof-v1",
        # This is evidence that a prior workflow is terminal, not a scheduler
        # capability.  The next generation must still be freshly prepared by
        # the normal outer loop under the current source/evaluation contract.
        "authority": "completed_abandon_terminal_evidence_only",
        "prepare_authorized": False,
        "next_tool": None,
        "transaction_id": transaction_id,
        "checkpoint_identity": dict(claim["checkpoint"]),
        "abandon_receipt_digest": abandon_receipt["receipt_digest"],
        "finalize_receipt_digest": finalize_receipt["receipt_digest"],
        "workflow_fences": workflow_fences,
        "first_strict_execution_fence": claim.get(
            "first_strict_execution_fence"
        ),
        "source": source,
    }


def _load_live_abandon_claim() -> dict | None:
    path = Path(RESULTS_DIR) / "policy_epoch_reconciliation_claim.json"
    if not os.path.lexists(path):
        return None
    claim = _read_json_regular(path)
    return _validate_active_abandon_claim(claim)


def _ensure_transaction_directory(path: Path) -> None:
    results = Path(RESULTS_DIR)
    if results.is_symlink() or not results.is_dir():
        raise RuntimeError("abandon_results_directory_unsafe")
    cursor = results
    relative = path.relative_to(results)
    for part in relative.parts:
        cursor = cursor / part
        if os.path.lexists(cursor):
            metadata = os.lstat(cursor)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("abandon_transaction_directory_unsafe")
            continue
        os.mkdir(cursor, 0o700)
        _fsync_parent_directory(cursor)


def _candidate_claim_preimage(version: int) -> tuple[Path, dict]:
    candidate = Path(get_bot_dir(version))
    if not os.path.lexists(candidate):
        return candidate, {
            "present": False,
            "path": f"bots/{bot_name(version)}",
            "manifest_digest": None,
            "entry_count": 0,
            "total_bytes": 0,
        }
    metadata = os.lstat(candidate)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("candidate_not_regular_directory")
    if os.path.lexists(candidate / ".completed"):
        raise RuntimeError("candidate_has_completed_sentinel")
    if git_has_publication_ref(version):
        raise RuntimeError("candidate_has_publication_ref")
    if git_dir_is_committed(version):
        raise RuntimeError("candidate_is_git_tracked")
    return candidate, {
        "present": True,
        "path": f"bots/{bot_name(version)}",
        **_candidate_tree_manifest(candidate),
    }


def _build_recorded_abandon_claim(
    checkpoint: dict,
    *,
    reason: str,
    first_strict_execution_fence: dict | None = None,
) -> tuple[dict, Path, Path]:
    if not _is_autonomous_runtime_checkout():
        raise RuntimeError("abandon_requires_autonomous_runtime_checkout")
    checkpoint_identity = _checkpoint_transaction_identity(checkpoint)
    version = int(checkpoint_identity["next_v"])
    candidate, candidate_identity = _candidate_claim_preimage(version)
    git_state = _current_abandon_git_state(version)
    ledger = _abandon_ledger_claim(checkpoint, str(reason))
    schema_version = 3 if first_strict_execution_fence is not None else 2
    claim_identity = {
        "checkpoint": checkpoint_identity,
        "abandon_reason": str(reason),
        "candidate": candidate_identity,
        "quarantine": schema2_abandon_quarantine_contract(),
        "ledger": ledger,
        "git_state": git_state,
    }
    if schema_version == 3:
        claim_identity["first_strict_execution_fence"] = (
            first_strict_execution_fence
        )
    transaction_id = canonical_digest(
        schema3_abandon_transaction_preimage(claim_identity)
        if schema_version == 3
        else schema2_abandon_transaction_preimage(claim_identity)
    )
    transaction_dir = (
        Path(RESULTS_DIR)
        / "policy_epoch_abandon_transactions"
        / transaction_id
    )
    claim_payload = {
        "schema_version": schema_version,
        "kind": "national-policy-recorded-abandon-finalize-claim",
        "evaluation_epoch": EVALUATION_EPOCH,
        "git_head": git_state["head"],
        "git_state": git_state,
        "checkout_role": "autonomous_evolution_runtime",
        "transaction_id": transaction_id,
        "checkpoint": checkpoint_identity,
        "abandon_reason": str(reason),
        "candidate": candidate_identity,
        "quarantine": schema2_abandon_quarantine_contract(),
        "ledger": ledger,
    }
    if schema_version == 3:
        claim_payload["first_strict_execution_fence"] = (
            first_strict_execution_fence
        )
    claim = {
        **claim_payload,
        "claim_digest": canonical_digest(claim_payload),
    }
    validate_abandon_claim_structure(claim)
    return claim, candidate, transaction_dir


def _is_autonomous_runtime_checkout() -> bool:
    return Path(PROJECT_ROOT).resolve().name == ".evolution_pok"


def _claim_transaction_paths(claim: dict) -> tuple[Path, Path]:
    transaction_id = str(claim.get("transaction_id") or "")
    if (
        len(transaction_id) != 64
        or any(char not in "0123456789abcdef" for char in transaction_id)
    ):
        raise RuntimeError("recorded_abandon_transaction_id_invalid")
    transaction_dir = (
        Path(RESULTS_DIR)
        / "policy_epoch_abandon_transactions"
        / transaction_id
    )
    return transaction_dir, transaction_dir / "candidate"


def _validate_claim_candidate_state(
    claim: dict,
    candidate: Path,
    quarantine: Path,
) -> str:
    expected = claim["candidate"]
    source_exists = os.path.lexists(candidate)
    quarantine_exists = os.path.lexists(quarantine)
    if source_exists and quarantine_exists:
        raise RuntimeError("candidate_exists_at_source_and_quarantine")
    if expected.get("present") is not True:
        if source_exists or quarantine_exists:
            raise RuntimeError("unexpected_candidate_for_absent_preimage")
        return "absent"
    if not source_exists and not quarantine_exists:
        raise RuntimeError("claimed_candidate_disappeared")
    observed_path = candidate if source_exists else quarantine
    observed = _candidate_tree_manifest(observed_path)
    for field in ("manifest_digest", "entry_count", "total_bytes"):
        if observed.get(field) != expected.get(field):
            raise RuntimeError(f"claimed_candidate_preimage_drifted:{field}")
    return "source" if source_exists else "quarantine"


def _claim_abandon_receipt(claim: dict) -> dict | None:
    rows = load_abandoned_version_receipts(
        path=Path(RESULTS_DIR) / "abandoned_versions.jsonl",
        project_root=PROJECT_ROOT,
    )
    receipt = validate_abandon_ledger_history(
        claim,
        rows,
        require_active_head=True,
    )
    if receipt is None:
        return None
    # The prior append may have completed its atomic replace and then raised
    # while syncing the parent directory.  A recovery attempt must re-prove
    # both the exact ledger inode and its directory before it is allowed to
    # quarantine candidate bytes or clear the checkpoint.
    _fsync_regular_state_file_and_parent(
        Path(RESULTS_DIR) / "abandoned_versions.jsonl"
    )
    return dict(receipt)


def _finalize_checkpoint_abandon_transaction(
    checkpoint: dict | None,
    *,
    reason: str,
    infra_failure: dict | None,
    timestamp: float,
    recorded_abandon_receipt: dict | None,
    first_strict_execution_fence: dict | None,
    clear_pipeline_state,
) -> dict:
    """Run the publication-linearized durable abandon state machine."""

    live_claim_path = Path(RESULTS_DIR) / "policy_epoch_reconciliation_claim.json"
    claim = _load_live_abandon_claim()
    if claim is None:
        if not isinstance(checkpoint, dict):
            raise RuntimeError("recorded_abandon_claim_missing")
        claim, candidate, transaction_dir = _build_recorded_abandon_claim(
            checkpoint,
            reason=reason,
            first_strict_execution_fence=first_strict_execution_fence,
        )
        _ensure_transaction_directory(transaction_dir)
        _ensure_durable_json(transaction_dir / "claim.json", claim)
        # The typed launch barrier and candidate preimage are durable before
        # the allocation receipt or any source-path mutation.
        _ensure_durable_json(live_claim_path, claim)
    else:
        if isinstance(checkpoint, dict):
            checkpoint_identity = _checkpoint_transaction_identity(checkpoint)
            if claim.get("checkpoint") != checkpoint_identity:
                raise RuntimeError("recorded_abandon_claim_checkpoint_mismatch")
        else:
            checkpoint_identity = claim["checkpoint"]
        if claim.get("abandon_reason") != str(reason):
            raise RuntimeError("recorded_abandon_claim_reason_mismatch")
        claimed_fence = claim.get("first_strict_execution_fence")
        if (
            first_strict_execution_fence is None
            and not isinstance(checkpoint, dict)
        ):
            first_strict_execution_fence = claimed_fence
        if claimed_fence != first_strict_execution_fence:
            raise RuntimeError(
                "recorded_abandon_first_strict_fence_mismatch"
            )
        transaction_dir, _ = _claim_transaction_paths(claim)
        candidate = Path(get_bot_dir(int(checkpoint_identity["next_v"])))
        _ensure_transaction_directory(transaction_dir)
        _ensure_durable_json(transaction_dir / "claim.json", claim)

    # The two claim copies are durable, but durability is not permission to
    # append the irreversible allocation receipt.  Reopen the LIVE copy, the
    # transaction copy, Git predicates, candidate preimage, ledger prefix and
    # exact checkpoint after the final claim write.  This closes the same-call
    # window in which any of them could drift between claim construction and
    # the first ledger append.
    claim = _validate_active_abandon_claim(claim)
    transaction_dir, quarantine = _claim_transaction_paths(claim)
    durable_chain_receipt = _claim_abandon_receipt(claim)
    if recorded_abandon_receipt is not None and (
        durable_chain_receipt is None
        or durable_chain_receipt.get("receipt_digest")
        != recorded_abandon_receipt.get("receipt_digest")
    ):
        raise RuntimeError("recorded_abandon_receipt_changed_after_claim")
    abandon_receipt = durable_chain_receipt
    if abandon_receipt is None:
        if not isinstance(checkpoint, dict):
            raise RuntimeError("recorded_abandon_receipt_missing_after_checkpoint_clear")
        abandon_receipt = append_abandoned_version_receipt(
            checkpoint,
            reason=reason,
            infra_failure=infra_failure,
            timestamp=timestamp,
            path=Path(RESULTS_DIR) / "abandoned_versions.jsonl",
            project_root=PROJECT_ROOT,
        )

    state = _validate_claim_candidate_state(claim, candidate, quarantine)
    if state == "source":
        version = int(claim["checkpoint"]["next_v"])
        # Re-run every publication predicate immediately before the rename.
        if _current_abandon_git_state(version) != claim["git_state"]:
            raise RuntimeError("recorded_abandon_active_git_state_changed")
        if os.path.lexists(candidate / ".completed"):
            raise RuntimeError("candidate_has_completed_sentinel")
        if os.stat(candidate.parent).st_dev != os.stat(transaction_dir).st_dev:
            raise RuntimeError("candidate_quarantine_not_same_filesystem")
        os.replace(candidate, quarantine)
        _fsync_parent_directory(candidate)
        _fsync_parent_directory(quarantine)
        if _validate_claim_candidate_state(claim, candidate, quarantine) != "quarantine":
            raise RuntimeError("candidate_quarantine_not_durable")
        state = "quarantine"
    elif state == "quarantine":
        _fsync_parent_directory(candidate)
        _fsync_parent_directory(quarantine)

    from evolution_core import PIPELINE_STATE_FILE

    checkpoint_path = Path(PIPELINE_STATE_FILE)
    if _current_abandon_git_state(
        int(claim["checkpoint"]["next_v"])
    ) != claim["git_state"]:
        raise RuntimeError("recorded_abandon_active_git_state_changed")
    if os.path.lexists(checkpoint_path):
        cleared = bool(clear_pipeline_state(
            expected_workflow_run_id=claim["checkpoint"]["workflow_run_id"],
            expected_next_v=claim["checkpoint"]["next_v"],
            expected_source_v=claim["checkpoint"]["source_v"],
            expected_checkpoint_revision=claim["checkpoint"]["checkpoint_revision"],
            expected_checkpoint_stage=claim["checkpoint"]["stage"],
        ))
        if not cleared:
            raise RuntimeError("checkpoint_identity_conflict")
    else:
        # Retry the directory durability proof after an unlink→fsync failure.
        _fsync_parent_directory(checkpoint_path)

    receipt_payload = {
        "schema_version": int(claim.get("schema_version") or 0),
        "kind": "national-policy-recorded-abandon-finalize",
        "evaluation_epoch": EVALUATION_EPOCH,
        "mode": "execute",
        "claim_digest": claim["claim_digest"],
        "workflow_run_id": claim["checkpoint"]["workflow_run_id"],
        "abandon_receipt_digest": abandon_receipt["receipt_digest"],
        "checkpoint_cleared": True,
        "candidate_state": state,
        "candidate_manifest_digest": claim["candidate"]["manifest_digest"],
    }
    if claim.get("schema_version") == 3:
        receipt_payload["first_strict_execution_fence_digest"] = (
            claim["first_strict_execution_fence"]["proof_digest"]
        )
    finalize_receipt = {
        **receipt_payload,
        "receipt_digest": canonical_digest(receipt_payload),
    }
    _ensure_durable_json(transaction_dir / "receipt.json", finalize_receipt)
    live_claim_path.unlink(missing_ok=True)
    _fsync_parent_directory(live_claim_path)
    return {
        "abandon_receipt": abandon_receipt,
        "finalize_receipt": finalize_receipt,
        "transaction_id": transaction_dir.name,
        "checkpoint_identity": dict(claim["checkpoint"]),
        "removed_directory": (
            bot_name(int(claim["checkpoint"]["next_v"]))
            if claim["candidate"]["present"] is True
            else None
        ),
        "first_strict_execution_fence": claim.get(
            "first_strict_execution_fence"
        ),
    }


def _generic_abandon_stage_block(checkpoint, reason):
    """Return a state-machine refusal payload for unsafe generic abandons."""
    return generic_abandon_block(
        checkpoint,
        reason=reason,
        max_precommit_retries=MAX_PRECOMMIT_RETRIES,
    )


def expected_abandon_identity(checkpoint: dict) -> dict:
    """Return the full checkpoint CAS identity required by forced callers."""

    if not isinstance(checkpoint, dict):
        raise RuntimeError("forced_abandon_checkpoint_identity_unavailable")
    workflow = str(
        checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or ""
    )
    if (
        not workflow
        or type(checkpoint.get("next_v")) is not int
        or type(checkpoint.get("source_v")) is not int
        or type(checkpoint.get("checkpoint_revision")) is not int
        or not isinstance(checkpoint.get("stage"), str)
        or not checkpoint["stage"]
    ):
        raise RuntimeError("forced_abandon_checkpoint_identity_incomplete")
    return {
        "expected_workflow_run_id": workflow,
        "expected_next_v": checkpoint["next_v"],
        "expected_source_v": checkpoint["source_v"],
        "expected_checkpoint_revision": checkpoint["checkpoint_revision"],
        "expected_checkpoint_stage": checkpoint["stage"],
    }


class ReapWeakestInput(TypedDict):
    pass


REAP_SELECTION_POLICY = "conservative_glicko_v1"
_REAP_SNAPSHOT_KEYS = {
    "schema_version",
    "kind",
    "selection_policy",
    "max_active_bots",
    "active_bots",
    "active_pool_digest",
    "priority_bot",
    "bot_inputs",
    "bot_inputs_digest",
    "snapshot_digest",
}
_REAP_BOT_INPUT_KEYS = {
    "bot",
    "rating_r_hex",
    "rating_rd_hex",
    "games",
    "leaderboard_score_hex",
    "h2h_avg_wr_hex",
}


def _finite_float_hex(value, *, field: str) -> str:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"reap_selection_{field}_invalid") from exc
    if not math.isfinite(normalized):
        raise RuntimeError(f"reap_selection_{field}_non_finite")
    return normalized.hex()


def _decode_finite_float_hex(value, *, field: str) -> float:
    if not isinstance(value, str):
        raise RuntimeError(f"reap_selection_{field}_not_hex")
    try:
        normalized = float.fromhex(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"reap_selection_{field}_not_hex") from exc
    if not math.isfinite(normalized) or normalized.hex() != value:
        raise RuntimeError(f"reap_selection_{field}_not_canonical")
    return normalized


def _is_strict_canonical_bot_name(value) -> bool:
    if not isinstance(value, str):
        return False
    version = parse_bot_version(value)
    return (
        version is not None
        and version >= FIRST_STRICT_POLICY_VERSION
        and value == bot_name(version)
    )


def _validate_reap_selection_snapshot(snapshot: dict) -> dict[str, dict]:
    """Validate and decode one immutable conservative-Glicko preimage."""

    if not isinstance(snapshot, dict) or set(snapshot) != _REAP_SNAPSHOT_KEYS:
        raise RuntimeError("reap_selection_snapshot_keys_invalid")
    if (
        type(snapshot.get("schema_version")) is not int
        or snapshot["schema_version"] != 1
        or snapshot.get("kind") != "strict-active-pool-selection-snapshot"
        or snapshot.get("selection_policy") != REAP_SELECTION_POLICY
        or type(snapshot.get("max_active_bots")) is not int
        or snapshot["max_active_bots"] < 1
        or not isinstance(snapshot.get("active_bots"), list)
        or not isinstance(snapshot.get("bot_inputs"), list)
    ):
        raise RuntimeError("reap_selection_snapshot_contract_invalid")
    active_bots = snapshot["active_bots"]
    if (
        active_bots != sorted(active_bots)
        or len(active_bots) != len(set(active_bots))
        or any(not _is_strict_canonical_bot_name(name) for name in active_bots)
        or snapshot.get("active_pool_digest") != canonical_digest(active_bots)
    ):
        raise RuntimeError("reap_selection_snapshot_pool_invalid")
    priority_bot = snapshot.get("priority_bot")
    if priority_bot is not None and (
        not isinstance(priority_bot, str) or priority_bot not in active_bots
    ):
        raise RuntimeError("reap_selection_snapshot_priority_invalid")
    rows = snapshot["bot_inputs"]
    if (
        len(rows) != len(active_bots)
        or snapshot.get("bot_inputs_digest") != canonical_digest(rows)
    ):
        raise RuntimeError("reap_selection_snapshot_inputs_invalid")
    decoded: dict[str, dict] = {}
    for expected_name, row in zip(active_bots, rows):
        if not isinstance(row, dict) or set(row) != _REAP_BOT_INPUT_KEYS:
            raise RuntimeError("reap_selection_snapshot_input_keys_invalid")
        if row.get("bot") != expected_name:
            raise RuntimeError("reap_selection_snapshot_input_order_invalid")
        if type(row.get("games")) is not int or row["games"] < 0:
            raise RuntimeError("reap_selection_snapshot_games_invalid")
        decoded[expected_name] = {
            "r": _decode_finite_float_hex(
                row.get("rating_r_hex"), field="rating_r"
            ),
            "rd": _decode_finite_float_hex(
                row.get("rating_rd_hex"), field="rating_rd"
            ),
            "games": row["games"],
            "leaderboard_score": _decode_finite_float_hex(
                row.get("leaderboard_score_hex"), field="leaderboard_score"
            ),
            "h2h_avg_wr": _decode_finite_float_hex(
                row.get("h2h_avg_wr_hex"), field="h2h_avg_wr"
            ),
        }
    unsigned = {
        key: value for key, value in snapshot.items() if key != "snapshot_digest"
    }
    if snapshot.get("snapshot_digest") != canonical_digest(unsigned):
        raise RuntimeError("reap_selection_snapshot_digest_invalid")
    return decoded


def _capture_reap_selection_snapshot(
    active_bots=None,
    *,
    max_active_bots: int | None = None,
) -> dict:
    """Freeze every input used by the active conservative-Glicko policy."""

    from evaluation_bundle import evaluation_cycle_lock
    from tool_helpers import _read_json

    cap = MAX_ACTIVE_BOTS if max_active_bots is None else max_active_bots
    if type(cap) is not int or cap < 1:
        raise RuntimeError("reap_selection_max_active_bots_invalid")
    with evaluation_cycle_lock(RESULTS_DIR, exclusive=False):
        names = sorted(
            get_active_bots() if active_bots is None else list(active_bots)
        )
        if (
            len(names) != len(set(names))
            or any(not _is_strict_canonical_bot_name(name) for name in names)
        ):
            raise RuntimeError("reap_selection_active_pool_invalid")
        ratings = load_ratings()
        h2h_winrates = load_h2h_avg_winrates()
        strength_scores = load_strength_scores()
        bot_stats = _read_json(RESULTS_DIR / "bot_stats.json", {})
        priority_data = _read_json(RESULTS_DIR / "priority_eval.json", {})
        priority_bot = (
            priority_data.get("bot")
            if isinstance(priority_data, dict)
            else None
        )
        if priority_bot not in names:
            priority_bot = None
        rows = []
        for name in names:
            rating = ratings.get(name, Glicko2Player())
            try:
                games = int((bot_stats.get(name) or {}).get("games", 0) or 0)
            except (AttributeError, TypeError, ValueError) as exc:
                raise RuntimeError("reap_selection_games_invalid") from exc
            if games < 0:
                raise RuntimeError("reap_selection_games_invalid")
            rows.append({
                "bot": name,
                "rating_r_hex": _finite_float_hex(
                    getattr(rating, "r", None), field="rating_r"
                ),
                "rating_rd_hex": _finite_float_hex(
                    getattr(rating, "rd", None), field="rating_rd"
                ),
                "games": games,
                "leaderboard_score_hex": _finite_float_hex(
                    strength_scores.get(name, 0.0), field="leaderboard_score"
                ),
                "h2h_avg_wr_hex": _finite_float_hex(
                    h2h_winrates.get(name, 0.0), field="h2h_avg_wr"
                ),
            })
    snapshot = {
        "schema_version": 1,
        "kind": "strict-active-pool-selection-snapshot",
        "selection_policy": REAP_SELECTION_POLICY,
        "max_active_bots": cap,
        "active_bots": names,
        "active_pool_digest": canonical_digest(names),
        "priority_bot": priority_bot,
        "bot_inputs": rows,
        "bot_inputs_digest": canonical_digest(rows),
    }
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    _validate_reap_selection_snapshot(snapshot)
    return snapshot


def _select_reap_candidate_from_snapshot(
    snapshot: dict,
    active_bots=None,
) -> dict:
    """Purely select the next target from one validated frozen preimage."""

    inputs = _validate_reap_selection_snapshot(snapshot)
    active_bots = list(
        snapshot["active_bots"] if active_bots is None else active_bots
    )
    if (
        len(active_bots) != len(set(active_bots))
        or not set(active_bots).issubset(inputs)
    ):
        raise RuntimeError("reap_selection_runtime_pool_invalid")
    cap = snapshot["max_active_bots"]
    if len(active_bots) <= cap:
        return {"candidate": None, "pool_size": len(active_bots)}

    current_bot = max(
        active_bots,
        key=lambda name: parse_bot_version(name) or -1,
    )

    # Exclude the current/latest source and the newest few active bots; they are
    # either being evolved from or still need fresh evaluation.
    protected_recent = set()
    if len(active_bots) > cap + 3:
        protected_recent = set(sorted(
            active_bots,
            key=lambda name: parse_bot_version(name) or -1,
        )[-3:])
    protected_names = {current_bot, *protected_recent}
    if snapshot["priority_bot"] in active_bots:
        protected_names.add(snapshot["priority_bot"])

    evaluated_candidates = []
    zero_game_candidates = []
    for name in active_bots:
        if name in protected_names:
            continue
        row = inputs[name]
        candidate = (name, row["r"], row["rd"], row["games"])
        if row["games"] == 0:
            zero_game_candidates.append(candidate)
            continue
        evaluated_candidates.append(candidate)

    # Soft overflow avoids untested candidates; hard overflow selects old
    # zero-game candidates before allowing the pool to grow without bound.
    if len(active_bots) <= cap + 3:
        candidates = evaluated_candidates
    else:
        candidates = evaluated_candidates + zero_game_candidates
    if not candidates:
        return {
            "candidate": None,
            "reason": "All remaining bots are current, recent, priority, or protected untested",
            "protected": sorted(protected_names),
        }

    protected = {name for name, _r, _rd, games in candidates if games < 600}
    if len(active_bots) <= cap + 3:
        candidates = [row for row in candidates if row[0] not in protected]
        if not candidates:
            return {
                "candidate": None,
                "reason": "all_protected",
                "remaining": len(active_bots),
                "protected_count": len(protected),
            }

    candidates.sort(key=lambda row: (
        row[1] - 2 * row[2],
        row[3],
        parse_bot_version(row[0]) or 0,
    ))
    name, rating_r, rating_rd, _games = candidates[0]
    frozen = inputs[name]
    return {
        "candidate": name,
        "selection_key": "conservative_glicko",
        "conservative_rating": round(rating_r - 2 * rating_rd, 1),
        "leaderboard_score": round(frozen["leaderboard_score"], 4),
        "h2h_avg_wr": round(frozen["h2h_avg_wr"], 4),
        "rating": {"r": round(rating_r, 1), "rd": round(rating_rd, 1)},
        "active_pool": sorted(active_bots),
    }


def _select_reap_candidate(active_bots=None) -> dict:
    """Return the exact next reap target without performing a side effect."""

    snapshot = _capture_reap_selection_snapshot(active_bots)
    return _select_reap_candidate_from_snapshot(snapshot, active_bots)


async def _do_reap_weakest(
    quiet: bool = False,
    *,
    expected_culled: str | None = None,
    selection_snapshot: dict | None = None,
) -> dict:
    """Core reaping logic, optionally fenced to a preplanned target."""

    active_bots = get_active_bots()
    snapshot = (
        _capture_reap_selection_snapshot(active_bots)
        if selection_snapshot is None
        else selection_snapshot
    )
    selection = _select_reap_candidate_from_snapshot(snapshot, active_bots)
    culled_name = selection.get("candidate")
    if not culled_name:
        return {
            "reaped": False,
            **{key: value for key, value in selection.items() if key != "candidate"},
        }
    if expected_culled is not None and culled_name != expected_culled:
        return {
            "reaped": False,
            "reason": "planned_reap_target_mismatch",
            "expected_culled": expected_culled,
            "actual_culled": culled_name,
        }
    conservative = float(selection["conservative_rating"])

    # Serialize concurrent reaps on a stable sidecar; a mutable data inode is
    # not a valid lock authority when atomic replacement is allowed elsewhere.
    from evolution_infra import _locked_state_sidecar

    with _locked_state_sidecar(
        RESULTS_DIR / ".reap-transaction",
        lock_type=fcntl.LOCK_EX,
    ):
        locked_active = get_active_bots()
        locked_selection = _select_reap_candidate_from_snapshot(
            snapshot, locked_active
        )
        locked_culled = locked_selection.get("candidate")
        if locked_culled != culled_name or (
            expected_culled is not None and locked_culled != expected_culled
        ):
            return {
                "reaped": False,
                "reason": "planned_reap_target_changed_under_lock",
                "expected_culled": expected_culled or culled_name,
                "actual_culled": locked_culled,
            }
        try:
            bot_src = PROJECT_ROOT / "bots" / culled_name
            if not bot_src.exists():
                return {"reaped": False, "reason": f"{culled_name} already moved"}
            # Publish the durable tombstone before mutating runtime metadata.
            # A failed tag/push must leave the sentinel intact so the operator
            # can retry without an ambiguous half-reaped state.
            record_reaped_bot(
                culled_name,
                reason="max_active_bots",
                data={
                    "selection_key": "conservative_glicko",
                    "conservative_rating": selection["conservative_rating"],
                    "leaderboard_score": selection["leaderboard_score"],
                    "h2h_avg_wr": selection["h2h_avg_wr"],
                    "quiet": quiet,
                },
            )
            sentinel = bot_src / ".completed"
            if os.path.lexists(sentinel):
                metadata = os.lstat(sentinel)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    raise RuntimeError("reap_completed_sentinel_unsafe")
                sentinel.unlink()
                from evolution_infra import _fsync_directory

                _fsync_directory(sentinel.parent)
        finally:
            pass

    reap_signal = RESULTS_DIR / ".reap_signal"
    from evolution_infra import _atomic_publish_state_text

    with _locked_state_sidecar(reap_signal, lock_type=fcntl.LOCK_EX):
        _atomic_publish_state_text(reap_signal, f"{time.time():.6f}\n")

    log_system_event(
        "bot.reaped",
        "info" if quiet else "warn",
        (
            f"{'Auto-reaped' if quiet else 'Reaped'} {culled_name} by conservative Glicko "
            f"(r-2rd={conservative:.1f}, leaderboard={selection['leaderboard_score']:.4f}, "
            f"h2h_wr={selection['h2h_avg_wr']:.2%})"
        ),
        {
            "culled": culled_name,
            "remaining": len(active_bots) - 1,
            "selection_key": "conservative_glicko",
            "conservative_rating": round(conservative, 1),
            "leaderboard_score": selection["leaderboard_score"],
            "h2h_avg_wr": selection["h2h_avg_wr"],
            "quiet": quiet,
        },
    )

    return {
        "reaped": True,
        "culled": culled_name,
        "selection_key": "conservative_glicko",
        "conservative_rating": selection["conservative_rating"],
        "leaderboard_score": selection["leaderboard_score"],
        "h2h_avg_wr": selection["h2h_avg_wr"],
        "rating": selection["rating"],
        "remaining": len(active_bots) - 1,
        "reap_mode": "deactivate_completed_sentinel",
    }


def _mcp_result(data: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


@tool("reap_weakest", "Check if bot pool exceeds MAX_ACTIVE_BOTS and cull the weakest bot by conservative rating, reporting unified strength.", {})
async def reap_weakest(args):
    result = await _do_reap_weakest(quiet=args.get("quiet", False) if isinstance(args, dict) else False)
    return _mcp_result(result)


async def cleanup_incomplete(args: dict | None = None):
    """Fail closed instead of scanning arbitrary incomplete bot directories.

    The old helper enumerated ``bots/`` and inferred deletion authority from a
    raw checkpoint.  That made retired v155 debris actionable.  The only safe
    cleanup is now the normal fenced abandon transaction for an explicitly
    named, currently validated strict workflow.  The helper is deliberately
    not registered in either the MCP or HTTP tool catalogs; these checks remain
    as defence in depth for direct/internal calls.
    """

    try:
        from epoch_authority import require_policy_epoch_initialized

        epoch = require_policy_epoch_initialized("cleanup_incomplete")
    except Exception as exc:
        state = getattr(exc, "state", None)
        return _mcp_result({
            "cleaned": False,
            "error": "policy_epoch_not_initialized",
            "epoch": state if isinstance(state, dict) else None,
        })

    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        return _mcp_result({
            "cleaned": False,
            "error": "strict_checkpoint_required",
        })
    next_v = checkpoint.get("next_v")
    revision = checkpoint.get("checkpoint_revision")
    workflow_run_id = checkpoint.get("workflow_run_id")
    if (
        type(next_v) is not int
        or type(revision) is not int
        or not isinstance(workflow_run_id, str)
        or not workflow_run_id.strip()
    ):
        return _mcp_result({
            "cleaned": False,
            "error": "strict_checkpoint_identity_missing",
        })

    try:
        from checkpoint_schema import strict_checkpoint_event_identity

        strict_checkpoint_event_identity(
            checkpoint,
            expected_gen=next_v,
            project_root=PROJECT_ROOT,
        )
    except Exception as exc:
        return _mcp_result({
            "cleaned": False,
            "error": "strict_checkpoint_invalid",
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
        })

    request = args if isinstance(args, dict) else {}
    requested_identity = (
        request.get("workflow_run_id"),
        request.get("next_v"),
        request.get("checkpoint_revision"),
    )
    current_identity = (workflow_run_id, next_v, revision)
    if requested_identity != current_identity:
        return _mcp_result({
            "cleaned": False,
            "error": "explicit_cleanup_identity_mismatch",
            "requested": {
                "workflow_run_id": request.get("workflow_run_id"),
                "next_v": request.get("next_v"),
                "checkpoint_revision": request.get("checkpoint_revision"),
            },
            "current": {
                "workflow_run_id": workflow_run_id,
                "next_v": next_v,
                "checkpoint_revision": revision,
            },
        })

    candidate = get_bot_dir(next_v)
    bot_root = BOTS_DIR
    expected_candidate = bot_root / bot_name(next_v)
    try:
        candidate_parent = candidate.parent.resolve(strict=True)
        bot_root_resolved = bot_root.resolve(strict=True)
    except OSError as exc:
        return _mcp_result({
            "cleaned": False,
            "error": "candidate_scope_unavailable",
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
        })
    if (
        candidate != expected_candidate
        or candidate_parent != bot_root_resolved
        or candidate.name != bot_name(next_v)
    ):
        return _mcp_result({
            "cleaned": False,
            "error": "candidate_outside_current_workflow_scope",
        })
    if not candidate.exists():
        return _mcp_result({
            "cleaned": False,
            "reason": "current_candidate_absent",
            "candidate": candidate.name,
            "workflow_run_id": workflow_run_id,
            "epoch": epoch.get("evaluation_epoch"),
        })
    if candidate.is_symlink() or not candidate.is_dir():
        return _mcp_result({
            "cleaned": False,
            "error": "current_candidate_path_unsafe",
        })
    if (candidate / ".completed").exists() or git_has_tag(next_v):
        return _mcp_result({
            "cleaned": False,
            "error": "current_candidate_is_published_or_completed",
        })
    if git_dir_is_committed(next_v):
        return _mcp_result({
            "cleaned": False,
            "error": "current_candidate_is_git_tracked",
        })

    result = await _do_abandon_generation(
        reason="cleanup_incomplete_exact_workflow",
        expected_workflow_run_id=workflow_run_id,
        expected_next_v=next_v,
        expected_source_v=checkpoint.get("source_v"),
        expected_checkpoint_revision=revision,
        expected_checkpoint_stage=checkpoint.get("stage"),
    )
    return _mcp_result({
        "cleaned": bool(
            result.get("abandoned") is True
            and result.get("removed_directory") == candidate.name
        ),
        "candidate": candidate.name,
        "workflow_run_id": workflow_run_id,
        "epoch": epoch.get("evaluation_epoch"),
        "abandon_result": result,
    })


class AbandonGenerationInput(TypedDict):
    pass


@tool("abandon_generation", "Clear pipeline checkpoint and remove incomplete next-gen directory. Use when a generation is stuck and needs to be restarted.", {})
async def abandon_generation(args):
    checkpoint = read_pipeline_checkpoint()
    outcome = (
        checkpoint.get("terminal_gate_outcome")
        if isinstance(checkpoint, dict)
        else None
    )
    if isinstance(outcome, dict):
        try:
            from gate_outcome import terminal_outcome_abandon_reason

            reason = terminal_outcome_abandon_reason(outcome)
            identity = expected_abandon_identity(checkpoint)
            result = await _do_abandon_generation(
                reason=reason,
                _bypass_rate_limit=True,
                expected_terminal_gate_outcome_digest=outcome.get(
                    "receipt_digest"
                ),
                **identity,
            )
        except Exception as exc:
            result = {
                "abandoned": False,
                "blocked": True,
                "reason": "terminal_gate_abandon_identity_invalid",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
    else:
        result = await _do_abandon_generation(reason="abandon_generation")
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


async def _do_abandon_generation(
    reason: str = "abandon_generation",
    *,
    _actor_lock_owned: bool = False,
    _publication_lock_owned: bool = False,
    _bypass_rate_limit: bool = False,
    expected_workflow_run_id: str | None = None,
    expected_next_v: int | None = None,
    expected_source_v: int | None = None,
    expected_checkpoint_revision: int | None = None,
    expected_checkpoint_stage: str | None = None,
    expected_terminal_gate_outcome_digest: str | None = None,
) -> dict:
    """Core abandon logic — clears the pipeline checkpoint and removes the
    incomplete next-gen directory.

    Shared by the ``abandon_generation`` MCP tool and forced-abandon paths
    (notably ``MASTER_EXHAUSTED`` in run_master, B2 v125 fix) so the latter no
    longer relies on the orchestrator LLM obeying a plain-text directive.

    ``_bypass_rate_limit`` is reserved for system-owned fail-closed paths that
    have already proved the current immutable candidate cannot be retried.  It
    does not bypass checkpoint identity, workflow fencing, or stage guards.

    Returns the abandon result dict (also written as a ``pipeline.abandoned``
    system event). Provider conversation history is never resumable; callers
    recover only from the checkpoint and durable transaction receipts.
    """
    from evolution_core import PIPELINE_STATE_FILE

    def expected_identity_conflict(candidate):
        if not any(value is not None for value in (
            expected_workflow_run_id,
            expected_next_v,
            expected_source_v,
            expected_checkpoint_revision,
            expected_checkpoint_stage,
        )):
            return None
        if not isinstance(candidate, dict):
            current = None
            mismatch = True
        else:
            current = {
                "workflow_run_id": str(
                    candidate.get("workflow_run_id")
                    or candidate.get("run_id")
                    or ""
                ),
                "next_v": candidate.get("next_v"),
                "source_v": candidate.get("source_v"),
                "checkpoint_revision": candidate.get("checkpoint_revision"),
                "stage": candidate.get("stage"),
            }
            mismatch = bool(
                (
                    expected_workflow_run_id is not None
                    and current["workflow_run_id"]
                    != str(expected_workflow_run_id)
                )
                or (
                    expected_next_v is not None
                    and current["next_v"] != int(expected_next_v)
                )
                or (
                    expected_source_v is not None
                    and current["source_v"] != int(expected_source_v)
                )
                or (
                    expected_checkpoint_revision is not None
                    and current["checkpoint_revision"]
                    != int(expected_checkpoint_revision)
                )
                or (
                    expected_checkpoint_stage is not None
                    and current["stage"] != str(expected_checkpoint_stage)
                )
                or (
                    expected_terminal_gate_outcome_digest is not None
                    and (
                        candidate.get("terminal_gate_outcome") or {}
                    ).get("receipt_digest")
                    != str(expected_terminal_gate_outcome_digest)
                )
            )
        if not mismatch:
            return None
        return {
            "abandoned": False,
            "reason": "expected_checkpoint_identity_mismatch",
            "action": "stale_rejection_ignored",
            "expected_checkpoint": {
                "workflow_run_id": expected_workflow_run_id,
                "next_v": expected_next_v,
                "source_v": expected_source_v,
                "checkpoint_revision": expected_checkpoint_revision,
                "stage": expected_checkpoint_stage,
                "terminal_gate_outcome_digest": (
                    expected_terminal_gate_outcome_digest
                ),
            },
            "current_checkpoint": current,
            "directive": (
                "The rejection belongs to an older checkpoint identity. Preserve "
                "the current generation and ignore this stale cleanup request."
            ),
        }

    try:
        live_abandon_claim = _load_live_abandon_claim()
    except Exception as exc:
        try:
            log_system_event(
                "pipeline.abandon_claim_invalid",
                "error",
                "Refused cleanup because the durable abandon claim failed revalidation",
                {"error": f"{type(exc).__name__}: {str(exc)[:500]}"},
            )
        except Exception:
            pass
        return {
            "abandoned": False,
            "reason": str(exc).split(":", 1)[0],
            "action": "operator_reconcile",
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
    if (
        reason not in {"abandon_generation", "cleanup_incomplete_exact_workflow"}
        and live_abandon_claim is None
        and any(value is None for value in (
            expected_workflow_run_id,
            expected_next_v,
            expected_source_v,
            expected_checkpoint_revision,
            expected_checkpoint_stage,
        ))
    ):
        return {
            "abandoned": False,
            "reason": "forced_abandon_checkpoint_identity_required",
            "action": "stale_rejection_ignored",
        }
    checkpoint_exists = PIPELINE_STATE_FILE.exists()
    checkpoint = read_pipeline_checkpoint() if checkpoint_exists else None
    if checkpoint_exists and not isinstance(checkpoint, dict):
        return {
            "abandoned": False,
            "reason": "checkpoint_corrupt",
            "action": "operator_reconcile",
            "directive": (
                "The pipeline checkpoint exists but cannot be decoded or "
                "normalized. Preserve it for diagnosis; do not infer a version "
                "or delete a candidate from directory names."
            ),
        }
    identity_conflict = expected_identity_conflict(
        checkpoint
        if isinstance(checkpoint, dict)
        else (
            live_abandon_claim.get("checkpoint")
            if isinstance(live_abandon_claim, dict)
            else checkpoint
        )
    )
    if identity_conflict:
        return identity_conflict
    terminal_stages = {
        "quality_rejected", "review_rejected", "critic_rejected",
    }
    if isinstance(checkpoint, dict) and checkpoint.get("stage") in terminal_stages:
        outcome = checkpoint.get("terminal_gate_outcome")
        try:
            from gate_outcome import terminal_outcome_abandon_reason

            canonical_terminal_reason = terminal_outcome_abandon_reason(outcome)
        except Exception as exc:
            return {
                "abandoned": False,
                "reason": "terminal_gate_outcome_identity_invalid",
                "action": "operator_reconcile",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
        if (
            reason != canonical_terminal_reason
            or expected_terminal_gate_outcome_digest
            != outcome.get("receipt_digest")
        ):
            return {
                "abandoned": False,
                "reason": "terminal_gate_abandon_authority_mismatch",
                "action": "operator_reconcile",
            }
    if expected_terminal_gate_outcome_digest is not None:
        outcome = (
            checkpoint.get("terminal_gate_outcome")
            if isinstance(checkpoint, dict)
            else None
        )
        if not isinstance(outcome, dict) or outcome.get(
            "receipt_digest"
        ) != str(expected_terminal_gate_outcome_digest):
            return {
                "abandoned": False,
                "reason": "terminal_gate_outcome_identity_mismatch",
                "action": "operator_reconcile",
            }
        try:
            from gate_outcome import validate_terminal_gate_outcome

            terminal_errors = validate_terminal_gate_outcome(
                checkpoint,
                outcome,
                candidate_dir=get_bot_dir(int(checkpoint["next_v"])),
            )
        except Exception as exc:
            terminal_errors = [
                "terminal_outcome_abandon_validation_error:"
                f"{type(exc).__name__}"
            ]
        if terminal_errors:
            return {
                "abandoned": False,
                "reason": "terminal_gate_outcome_invalid",
                "action": "operator_reconcile",
                "issues": terminal_errors,
            }
    infra_failure = (
        dict(checkpoint.get("infra_failure"))
        if isinstance(checkpoint, dict) and isinstance(checkpoint.get("infra_failure"), dict)
        else None
    )
    recorded_abandon_receipt = None
    if isinstance(checkpoint, dict):
        try:
            recorded_abandon_receipt = recorded_abandon_receipt_for_checkpoint(
                checkpoint,
                path=RESULTS_DIR / "abandoned_versions.jsonl",
                project_root=PROJECT_ROOT,
            )
        except Exception:
            recorded_abandon_receipt = None
        if recorded_abandon_receipt is not None and (
            recorded_abandon_receipt.get("reason") != str(reason).strip()
            or recorded_abandon_receipt.get("infra_failure") != infra_failure
        ):
            return {
                "abandoned": False,
                "reason": "recorded_abandon_receipt_payload_mismatch",
                "action": "operator_reconcile",
                "abandon_receipt_digest": recorded_abandon_receipt.get(
                    "receipt_digest"
                ),
            }
    elif isinstance(live_abandon_claim, dict):
        if reason != str(live_abandon_claim.get("abandon_reason") or ""):
            return {
                "abandoned": False,
                "reason": "recorded_abandon_claim_reason_mismatch",
                "action": "operator_reconcile",
            }
        recorded_abandon_receipt = _claim_abandon_receipt(live_abandon_claim)
    blocked = (
        None
        if recorded_abandon_receipt is not None
        else _generic_abandon_stage_block(checkpoint, reason)
    )
    if blocked:
        try:
            log_system_event(
                "pipeline.abandon_refused_state_guard",
                "warn",
                blocked["directive"],
                blocked,
            )
        except Exception:
            pass
        return blocked

    # A4 (2026-06-30): rate-limit abandons to prevent evolution-DoS / version-space
    # leak. A rogue or stuck LLM could spam abandon_generation, monotonically
    # incrementing next_v via the abandoned_versions floor and never letting any
    # generation reach the gates. Enforce a 60s cooldown between abandons.
    import time as _t
    now = _t.time()
    if (
        recorded_abandon_receipt is None
        and not _bypass_rate_limit
        and (now - _LAST_ABANDON_TS[0]) < 60
    ):
        try:
            log_system_event(
                "pipeline.abandon_rate_limited", "warn",
                f"abandon_generation rate-limited (cooldown {60 - (now - _LAST_ABANDON_TS[0]):.0f}s remaining). "
                f"Recent abandon was {_LAST_ABANDON_TS[1]}.",
                {"cooldown_remaining": 60 - (now - _LAST_ABANDON_TS[0]),
                 "last_abandon_reason": _LAST_ABANDON_TS[1]},
            )
        except Exception:
            pass
        return {"abandoned": False, "rate_limited": True,
                "reason": f"abandon cooldown active ({60 - (now - _LAST_ABANDON_TS[0]):.0f}s remaining)"}
    workflow_fenced = False
    workflow_run_id = None
    first_strict_execution_fence = None
    if isinstance(checkpoint, dict):
        try:
            from worker_workflow import (
                WorkerWorkflow,
                workflow_run_id as checkpoint_workflow_run_id,
            )

            workflow = WorkerWorkflow.for_checkpoint(checkpoint)
            workflow_run_id = workflow.run_id
            # The actor terminal event and effect cancellation happen before any
            # mutable projection or candidate cleanup. Completion/projector paths
            # use the same short lock, so a late Worker can never recreate an
            # abandoned candidate after rmtree.
            def fence_latest_checkpoint():
                nonlocal recorded_abandon_receipt, first_strict_execution_fence
                latest = read_pipeline_checkpoint()
                if not isinstance(latest, dict):
                    raise RuntimeError(
                        "checkpoint disappeared or became unreadable before fence"
                    )
                latest_identity_conflict = expected_identity_conflict(latest)
                if latest_identity_conflict:
                    return latest_identity_conflict, None
                if checkpoint_workflow_run_id(latest) != workflow.run_id:
                    raise RuntimeError(
                        "checkpoint workflow identity changed before fence"
                    )
                if recorded_abandon_receipt is not None:
                    recorded_abandon_receipt = (
                        recorded_abandon_receipt_for_checkpoint(
                            latest,
                            path=RESULTS_DIR / "abandoned_versions.jsonl",
                            project_root=PROJECT_ROOT,
                        )
                    )
                    if recorded_abandon_receipt is None:
                        raise RuntimeError(
                            "recorded abandon receipt no longer matches checkpoint"
                        )
                latest_block = _generic_abandon_stage_block(latest, reason)
                if latest_block and recorded_abandon_receipt is None:
                    return latest_block, None
                first_strict_execution_fence = (
                    _fence_first_strict_control_execution(
                        latest,
                        reason=reason,
                    )
                )
                workflow.abandon(
                    reason,
                    accept_existing_reason=(
                        latest.get("stage") not in terminal_stages
                    ),
                )
                from strict_authority_workflow import abandon_authority

                strict_fence = abandon_authority(latest, reason=reason)
                if strict_fence.get("present") and not strict_fence.get(
                    "abandoned"
                ):
                    raise RuntimeError(
                        "strict authority child journal was not abandoned"
                    )
                return None, latest

            if _actor_lock_owned:
                blocked_after_lock, latest_checkpoint = fence_latest_checkpoint()
            else:
                with workflow.store.command_lock(
                    workflow.run_id,
                    blocking=True,
                ):
                    blocked_after_lock, latest_checkpoint = (
                        fence_latest_checkpoint()
                    )
            if blocked_after_lock:
                log_system_event(
                    "pipeline.abandon_refused_state_guard",
                    "warn",
                    blocked_after_lock["directive"],
                    blocked_after_lock,
                )
                return blocked_after_lock
            checkpoint = latest_checkpoint
            infra_failure = (
                dict(checkpoint.get("infra_failure"))
                if isinstance(checkpoint.get("infra_failure"), dict)
                else None
            )
            workflow_fenced = True
        except Exception as exc:
            log_system_event(
                "pipeline.abandon_workflow_fence_failed",
                "error",
                "Refused generation cleanup because the durable actor could not be fenced",
                {
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "workflow_run_id": workflow_run_id,
                    "first_strict_execution_fence": (
                        first_strict_execution_fence
                    ),
                },
            )
            return {
                "abandoned": False,
                "reason": "workflow_fence_failed",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                "workflow_run_id": workflow_run_id,
                "first_strict_execution_fence": first_strict_execution_fence,
            }
    cleared_checkpoint = False
    removed_dir = None
    abandon_receipt = None
    finalize_receipt = None
    abandon_transaction_id = None
    abandon_checkpoint_identity = None
    abandoned_v = (
        checkpoint.get("next_v")
        if isinstance(checkpoint, dict)
        else (
            live_abandon_claim.get("checkpoint", {}).get("next_v")
            if isinstance(live_abandon_claim, dict)
            else None
        )
    )

    if not isinstance(checkpoint, dict) and live_abandon_claim is None:
        # A bare directory name is never cleanup authority. Re-prove the
        # checkpoint-parent durability after any prior unlink/fsync failure,
        # then leave candidate bytes untouched for operator inspection.
        try:
            _fsync_parent_directory(Path(PIPELINE_STATE_FILE))
        except OSError as exc:
            return {
                "abandoned": False,
                "reason": "checkpoint_parent_fsync_failed",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
        return {
            "abandoned": False,
            "reason": "no_checkpoint_cleanup_authority",
            "cleared_checkpoint": False,
            "abandoned_v": None,
        }

    lock_context = (
        nullcontext()
        if _publication_lock_owned
        else bot_publication_lock(results_dir=RESULTS_DIR)
    )
    try:
        with lock_context:
            latest = read_pipeline_checkpoint() if os.path.lexists(PIPELINE_STATE_FILE) else None
            if isinstance(latest, dict):
                latest_conflict = expected_identity_conflict(latest)
                if latest_conflict:
                    return latest_conflict
                latest_receipt = recorded_abandon_receipt_for_checkpoint(
                    latest,
                    path=Path(RESULTS_DIR) / "abandoned_versions.jsonl",
                    project_root=PROJECT_ROOT,
                )
                if latest_receipt is not None and (
                    latest_receipt.get("reason") != str(reason).strip()
                    or latest_receipt.get("infra_failure")
                    != (
                        dict(latest.get("infra_failure"))
                        if isinstance(latest.get("infra_failure"), dict)
                        else None
                    )
                ):
                    raise RuntimeError(
                        "recorded_abandon_receipt_payload_mismatch"
                    )
                recorded_abandon_receipt = latest_receipt
                infra_failure = (
                    dict(latest.get("infra_failure"))
                    if isinstance(latest.get("infra_failure"), dict)
                    else None
                )
                latest_block = _generic_abandon_stage_block(latest, reason)
                if latest_block and recorded_abandon_receipt is None:
                    return latest_block
                checkpoint = latest
            elif live_abandon_claim is None:
                raise RuntimeError("checkpoint_disappeared_before_abandon_claim")
            transaction = _finalize_checkpoint_abandon_transaction(
                checkpoint if isinstance(checkpoint, dict) else None,
                reason=reason,
                infra_failure=infra_failure,
                timestamp=now,
                recorded_abandon_receipt=recorded_abandon_receipt,
                first_strict_execution_fence=(
                    first_strict_execution_fence
                    if isinstance(first_strict_execution_fence, dict)
                    and first_strict_execution_fence.get("present") is True
                    else None
                ),
                clear_pipeline_state=clear_pipeline_checkpoint,
            )
            abandon_receipt = transaction["abandon_receipt"]
            finalize_receipt = transaction["finalize_receipt"]
            abandon_transaction_id = transaction["transaction_id"]
            abandon_checkpoint_identity = transaction["checkpoint_identity"]
            removed_dir = transaction["removed_directory"]
            if transaction.get("first_strict_execution_fence") is not None:
                first_strict_execution_fence = transaction[
                    "first_strict_execution_fence"
                ]
            cleared_checkpoint = True
    except Exception as exc:
        log_system_event(
            "pipeline.abandon_transaction_incomplete",
            "error",
            "Durable abandon transaction remains fenced for exact recovery",
            {
                "reason": reason,
                "workflow_run_id": workflow_run_id,
                "abandoned_v": abandoned_v,
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            },
        )
        return {
            "abandoned": False,
            "reason": str(exc).split(":", 1)[0],
            "workflow_fenced": workflow_fenced,
            "workflow_run_id": workflow_run_id,
            "first_strict_execution_fence": first_strict_execution_fence,
            "abandoned_v": abandoned_v,
            "removed_directory": (
                bot_name(int(abandoned_v))
                if abandoned_v is not None
                and not os.path.lexists(get_bot_dir(int(abandoned_v)))
                else None
            ),
            "abandon_receipt_digest": (
                recorded_abandon_receipt.get("receipt_digest")
                if isinstance(recorded_abandon_receipt, dict)
                else None
            ),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }

    log_system_event("pipeline.abandoned", "warn",
                     f"Abandoned generation ({reason}, dir={removed_dir})",
                     {"removed_dir": removed_dir, "cleared_checkpoint": cleared_checkpoint,
                      "reason": reason, "abandoned_v": abandoned_v,
                      "infra_failure": infra_failure,
                      "workflow_fenced": workflow_fenced,
                      "workflow_run_id": workflow_run_id,
                      "first_strict_execution_fence": (
                          first_strict_execution_fence
                      )})
    # A4: update rate-limit timestamp on successful abandon.
    _LAST_ABANDON_TS[0] = now
    _LAST_ABANDON_TS[1] = reason

    # An abandoned generation breaks the uninterrupted-delivery streak even
    # though it carries no strength or strategy authority.  Keep this
    # acceptance update downstream of the fenced, durable cleanup.
    try:
        from stability_observation import reset_stability_observation

        reset_stability_observation(
            "generation_abandoned",
            details={
                "reason": reason,
                "abandoned_v": abandoned_v,
                "workflow_run_id": workflow_run_id,
            },
        )
    except Exception as exc:
        try:
            log_system_event(
                "pipeline.stability_observation_failed",
                "error",
                "Generation was abandoned but stability observation reset failed",
                {
                    "reason": reason,
                    "abandoned_v": abandoned_v,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                },
            )
        except Exception:
            pass

    return {
        "abandoned": True,
        "cleared_checkpoint": cleared_checkpoint,
        "removed_directory": removed_dir,
        "reason": reason,
        "infra_failure": infra_failure,
        "abandoned_v": abandoned_v,
        "workflow_fenced": workflow_fenced,
        "workflow_run_id": workflow_run_id,
        "first_strict_execution_fence": first_strict_execution_fence,
        "abandon_receipt_digest": (
            abandon_receipt.get("receipt_digest")
            if isinstance(abandon_receipt, dict)
            else None
        ),
        "finalize_receipt_digest": (
            finalize_receipt.get("receipt_digest")
            if isinstance(finalize_receipt, dict)
            else None
        ),
        "abandon_transaction_id": abandon_transaction_id,
        "abandon_checkpoint_identity": abandon_checkpoint_identity,
    }
