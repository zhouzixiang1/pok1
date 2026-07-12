"""Operator-owned, generation-scoped LLM cost accounting.

Cost is observability by default.  A hard stop exists only when the operator
sets ``POK_OPERATOR_MAX_GENERATION_COST_USD`` in the parent process before the
orchestrator starts.  The value is deliberately not accepted from prompts,
MCP arguments, candidate files, or checkpoints.

Usage is appended to a system-owned ledger keyed by ``workflow_run_id``.  This
keeps one generation's total stable across disposable Claude sessions,
deterministic checkpoint hand-offs, and process restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from typing import Mapping

from evolution_infra import RESULTS_DIR, locked_file


POLICY_SCHEMA_VERSION = 1
POLICY_ID = "operator-owned-generation-cost-v1"
HARD_LIMIT_ENV = "POK_OPERATOR_MAX_GENERATION_COST_USD"
DEFAULT_WARNING_USD = 7.0
COST_LEDGER_FILE = RESULTS_DIR / "generation_cost_ledger.jsonl"
COST_PENDING_FILE = RESULTS_DIR / "generation_cost_pending.json"

_DISABLED_VALUES = frozenset({"", "0", "off", "none", "unlimited", "disabled"})
_state_lock = threading.RLock()
_runtime_policy: "GenerationCostPolicy | None" = None
_active_scope: "GenerationCostScope | None" = None
_accounting_errors: dict[str, str] = {}


class CostPolicyConfigurationError(ValueError):
    """The operator supplied an invalid hard-limit value."""


class OperatorGenerationCostLimitExceeded(RuntimeError):
    """The explicit operator limit fired, or its accounting became unavailable."""

    def __init__(self, message: str, *, status: Mapping[str, object] | None = None):
        super().__init__(message)
        self.status = dict(status or {})


def _canonical_digest(payload: Mapping[str, object]) -> str:
    raw = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _implementation_sha256() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


def generation_workflow_id(next_v: int) -> str:
    """Return the one durable identity allocated before prepare-stage LLM work.

    National bot version numbers are never reused: committed, tagged, and
    centrally abandoned versions all raise the next-version floor.  Binding the
    workflow identity to that unique version therefore lets prepare analysis,
    checkpointed work, disposable SDK sessions, and process restarts share the
    same cost scope without a second mutable identity registry.
    """

    version = int(next_v)
    if version <= 0:
        raise ValueError("next_v must be positive")
    return f"generation:{version}:workflow-v1"


def sdk_result_event_id(result: object, *, source: str, attempt: int = 0) -> str:
    """Build a replay-stable billing event id from one SDK ResultMessage."""

    sdk_uuid = str(getattr(result, "uuid", None) or "").strip()
    if sdk_uuid:
        return f"sdk-result:{sdk_uuid}"
    body = {
        "source": str(source),
        "session_id": str(getattr(result, "session_id", None) or ""),
        "subtype": str(getattr(result, "subtype", None) or ""),
        "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
        "duration_api_ms": int(getattr(result, "duration_api_ms", 0) or 0),
        "num_turns": int(getattr(result, "num_turns", 0) or 0),
        "total_cost_usd": getattr(result, "total_cost_usd", None),
        "usage": getattr(result, "usage", None),
        "result_sha256": hashlib.sha256(
            str(getattr(result, "result", None) or "").encode("utf-8")
        ).hexdigest(),
        # Compatibility discriminator when an older SDK omits the result UUID.
        "attempt": int(attempt),
    }
    return "sdk-result-fallback:" + _canonical_digest(body)


@dataclass(frozen=True)
class GenerationCostPolicy:
    warning_usd: float = DEFAULT_WARNING_USD
    hard_limit_usd: float | None = None
    configuration_source: str = "operator_process_environment"

    @property
    def enforcement_mode(self) -> str:
        return "operator_hard_limit" if self.hard_limit_usd is not None else "monitor_only"

    def receipt(self) -> dict:
        body = {
            "schema_version": POLICY_SCHEMA_VERSION,
            "policy_id": POLICY_ID,
            "enforcement_mode": self.enforcement_mode,
            "warning_usd": float(self.warning_usd),
            "hard_limit_usd": (
                float(self.hard_limit_usd)
                if self.hard_limit_usd is not None
                else None
            ),
            "configuration_source": self.configuration_source,
            "operator_env_name": HARD_LIMIT_ENV,
            "configuration_from_llm_input": False,
            "same_uid_llm_resistance": False,
            "candidate_sandbox_mutable": False,
            "workflow_guarded_paths": True,
            "limit_semantics": "post_call_circuit_breaker",
            "parallel_overshoot_possible": True,
            "billing_event_idempotent": True,
            "write_ahead_pending_path": str(COST_PENDING_FILE),
            "implementation_sha256": _implementation_sha256(),
        }
        return {**body, "receipt_sha256": _canonical_digest(body)}


@dataclass(frozen=True)
class GenerationCostScope:
    generation_id: str
    policy: GenerationCostPolicy
    activated_at: float

    def receipt(self, *, spent_before_usd: float, ledger_errors: tuple[str, ...] = ()) -> dict:
        body = {
            **self.policy.receipt(),
            "generation_id": self.generation_id,
            "spent_before_usd": round(float(spent_before_usd), 6),
            "ledger_errors": list(ledger_errors),
            "ledger_path": str(COST_LEDGER_FILE),
        }
        return {**body, "binding_sha256": _canonical_digest(body)}


def load_operator_generation_cost_policy(
    environ: Mapping[str, str] | None = None,
) -> GenerationCostPolicy:
    """Parse the operator setting without accepting any pipeline/LLM input.

    Unset and explicit disable spellings select monitor-only mode.  Any other
    configured value must be finite and strictly positive; typos fail startup
    rather than silently creating an unintended budget policy.
    """

    source = os.environ if environ is None else environ
    raw = source.get(HARD_LIMIT_ENV)
    if raw is None or str(raw).strip().lower() in _DISABLED_VALUES:
        return GenerationCostPolicy()
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise CostPolicyConfigurationError(
            f"{HARD_LIMIT_ENV} must be a finite positive USD amount or 'off'; got {raw!r}"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise CostPolicyConfigurationError(
            f"{HARD_LIMIT_ENV} must be finite and > 0; got {raw!r}"
        )
    return GenerationCostPolicy(hard_limit_usd=value)


def configure_runtime_cost_policy(policy: GenerationCostPolicy) -> GenerationCostPolicy:
    """Freeze the policy selected by the operator-facing process boundary."""

    if not isinstance(policy, GenerationCostPolicy):
        raise TypeError("runtime cost policy must be GenerationCostPolicy")
    global _runtime_policy
    with _state_lock:
        _runtime_policy = policy
    return policy


def runtime_cost_policy() -> GenerationCostPolicy:
    with _state_lock:
        if _runtime_policy is not None:
            return _runtime_policy
    return configure_runtime_cost_policy(load_operator_generation_cost_policy())


def generation_identity(checkpoint: Mapping[str, object] | None, gen_ctx=None) -> str:
    """Return the durable generation identity already owned by the checkpoint."""

    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    workflow_run_id = str(
        checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or ""
    ).strip()
    if workflow_run_id:
        return workflow_run_id

    next_v = checkpoint.get("next_v")
    generation_attempt = checkpoint.get("generation_attempt", 0)
    if next_v is None and gen_ctx is not None:
        next_v = getattr(gen_ctx, "next_v", None)
    if next_v is not None:
        # Current checkpoints are allocated before prepare-stage analysis.  The
        # attempt suffix remains only for legacy diagnostic callers that supply
        # no durable workflow id.
        if int(generation_attempt or 0) == 0:
            return generation_workflow_id(int(next_v))
        return f"generation:{int(next_v)}:legacy-attempt:{int(generation_attempt)}"
    return f"diagnostic:{os.getpid()}:{time.time_ns()}"


def activate_generation_cost_scope(
    generation_id: str,
    policy: GenerationCostPolicy | None = None,
) -> GenerationCostScope:
    clean_id = str(generation_id or "").strip()
    if not clean_id:
        raise ValueError("generation_id is required")
    scope = GenerationCostScope(
        generation_id=clean_id,
        policy=policy or runtime_cost_policy(),
        activated_at=time.time(),
    )
    global _active_scope
    with _state_lock:
        _active_scope = scope
    return scope


def current_generation_cost_scope() -> GenerationCostScope | None:
    with _state_lock:
        return _active_scope


def deactivate_generation_cost_scope(generation_id: str | None = None) -> None:
    global _active_scope
    with _state_lock:
        if (
            _active_scope is not None
            and generation_id is not None
            and _active_scope.generation_id != str(generation_id)
        ):
            return
        _active_scope = None


def _read_generation_entries(generation_id: str) -> tuple[list[dict], tuple[str, ...]]:
    path = Path(COST_LEDGER_FILE)
    if not path.exists():
        return [], ()
    entries: list[dict] = []
    errors: list[str] = []
    try:
        with locked_file(path, "r", encoding="utf-8") as handle:
            for line_no, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    errors.append(f"malformed_line:{line_no}")
                    continue
                if not isinstance(entry, dict):
                    errors.append(f"non_object_line:{line_no}")
                    continue
                if str(entry.get("generation_id") or "") == generation_id:
                    entries.append(entry)
    except OSError as exc:
        errors.append(f"ledger_read_failed:{type(exc).__name__}:{exc}")
    return entries, tuple(errors)


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically replace one small system-owned accounting state file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        except (AttributeError, OSError):
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _pending_lock_path() -> Path:
    path = Path(COST_PENDING_FILE)
    return path.with_suffix(path.suffix + ".lock")


def _load_pending_state_unlocked(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": POLICY_SCHEMA_VERSION, "pending": {}}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {"schema_version": POLICY_SCHEMA_VERSION, "pending": {}}
    state = json.loads(raw)
    if not isinstance(state, dict) or not isinstance(state.get("pending"), dict):
        raise ValueError("pending accounting state is not an object map")
    return state


def _persist_pending_usage(entry: Mapping[str, object]) -> bool:
    """Write-ahead one billed SDK result before touching the append ledger."""

    path = Path(COST_PENDING_FILE)
    event_id = str(entry.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("pending usage requires event_id")
    with locked_file(_pending_lock_path(), "a+", encoding="utf-8"):
        state = _load_pending_state_unlocked(path)
        pending = dict(state.get("pending") or {})
        is_new = event_id not in pending
        pending[event_id] = dict(entry)
        _atomic_write_json(
            path,
            {"schema_version": POLICY_SCHEMA_VERSION, "pending": pending},
        )
    return is_new


def _clear_pending_usage(event_id: str) -> None:
    path = Path(COST_PENDING_FILE)
    with locked_file(_pending_lock_path(), "a+", encoding="utf-8"):
        state = _load_pending_state_unlocked(path)
        pending = dict(state.get("pending") or {})
        if str(event_id) not in pending:
            return
        pending.pop(str(event_id), None)
        _atomic_write_json(
            path,
            {"schema_version": POLICY_SCHEMA_VERSION, "pending": pending},
        )


def _read_pending_generation_entries(
    generation_id: str,
) -> tuple[list[dict], tuple[str, ...]]:
    path = Path(COST_PENDING_FILE)
    if not path.exists():
        return [], ()
    try:
        with locked_file(_pending_lock_path(), "a+", encoding="utf-8"):
            state = _load_pending_state_unlocked(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return [], (f"pending_state_read_failed:{type(exc).__name__}:{exc}",)
    entries = [
        dict(entry)
        for entry in (state.get("pending") or {}).values()
        if isinstance(entry, dict)
        and str(entry.get("generation_id") or "") == str(generation_id)
    ]
    return entries, ()


def generation_cost_status(scope: GenerationCostScope | None = None) -> dict:
    scope = scope or current_generation_cost_scope()
    if scope is None:
        return {
            "active": False,
            "generation_id": None,
            "spent_usd": 0.0,
            "accounting_ok": True,
        }
    entries, read_errors = _read_generation_entries(scope.generation_id)
    pending_entries, pending_errors = _read_pending_generation_entries(
        scope.generation_id
    )
    errors = [*read_errors, *pending_errors]
    spent = 0.0
    ledger_event_ids: set[str] = set()
    for entry in entries:
        if entry.get("kind") != "usage":
            continue
        event_id = str(entry.get("event_id") or entry.get("entry_id") or "")
        if event_id:
            ledger_event_ids.add(event_id)
        if entry.get("cost_known") is False:
            errors.append(f"unknown_cost:{event_id or 'unknown'}")
        try:
            value = float(entry.get("cost_usd"))
        except (TypeError, ValueError):
            errors.append(f"invalid_cost:{entry.get('entry_id', 'unknown')}")
            continue
        if not math.isfinite(value) or value < 0:
            errors.append(f"invalid_cost:{entry.get('entry_id', 'unknown')}")
            continue
        spent += value
    unresolved_pending = 0
    for entry in pending_entries:
        event_id = str(entry.get("event_id") or "")
        if event_id and event_id in ledger_event_ids:
            continue
        if entry.get("cost_known") is False:
            errors.append(f"unknown_pending_cost:{event_id or 'unknown'}")
        try:
            value = float(entry.get("cost_usd"))
        except (TypeError, ValueError):
            errors.append(f"invalid_pending_cost:{event_id or 'unknown'}")
            continue
        if not math.isfinite(value) or value < 0:
            errors.append(f"invalid_pending_cost:{event_id or 'unknown'}")
            continue
        # The SDK already reported this as billed.  Count it even though the
        # append ledger failed, and retain an accounting error so explicit hard
        # mode remains parked across rebind/restart.
        spent += value
        unresolved_pending += 1
        errors.append(f"pending_usage_not_committed:{event_id or 'unknown'}")
    with _state_lock:
        in_memory_error = _accounting_errors.get(scope.generation_id)
        if in_memory_error:
            errors.append(in_memory_error)
    hard_limit = scope.policy.hard_limit_usd
    return {
        "active": True,
        "generation_id": scope.generation_id,
        "spent_usd": round(spent, 6),
        "warning_usd": scope.policy.warning_usd,
        "warning_reached": spent >= scope.policy.warning_usd,
        "hard_limit_usd": hard_limit,
        # Kept under the historical field name for API compatibility.  The
        # circuit breaker fires on reaching the configured threshold.
        "hard_limit_exceeded": bool(hard_limit is not None and spent >= hard_limit),
        "enforcement_mode": scope.policy.enforcement_mode,
        "policy_receipt": scope.policy.receipt(),
        "accounting_ok": not errors,
        "accounting_errors": errors,
        "pending_usage_count": unresolved_pending,
    }


def record_generation_cost(
    role: str,
    cost_usd: float | None,
    usage: Mapping[str, object] | None = None,
    *,
    source: str,
    event_id: str | None = None,
) -> dict:
    """Append one billed call to the active generation ledger."""

    scope = current_generation_cost_scope()
    if scope is None:
        return {**generation_cost_status(scope), "recorded": False}
    cost_known = cost_usd is not None
    if cost_known:
        try:
            value = float(cost_usd)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid cost_usd {cost_usd!r}") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"cost_usd must be finite and >= 0; got {cost_usd!r}")
    else:
        # Preserve token evidence and fail closed in explicit hard mode when the
        # SDK reports a Result without a usable USD amount.
        value = 0.0
    clean_event_id = str(event_id or "").strip() or f"legacy:{uuid.uuid4().hex}"
    entry_id = hashlib.sha256(
        f"{scope.generation_id}\0{clean_event_id}".encode("utf-8")
    ).hexdigest()

    entry = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "kind": "usage",
        "entry_id": entry_id,
        "event_id": clean_event_id,
        "generation_id": scope.generation_id,
        "role": str(role or "unknown"),
        "cost_usd": round(value, 9),
        "cost_known": cost_known,
        "input_tokens": int((usage or {}).get("input_tokens", 0) or 0),
        "output_tokens": int((usage or {}).get("output_tokens", 0) or 0),
        "usage": json.loads(
            json.dumps(dict(usage or {}), ensure_ascii=False, default=str)
        ),
        "source": str(source),
        "policy_receipt_sha256": scope.policy.receipt()["receipt_sha256"],
        "ts": time.time(),
    }

    pending_persisted = False
    pending_new = False
    try:
        pending_new = _persist_pending_usage(entry)
        pending_persisted = True
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        with _state_lock:
            _accounting_errors[scope.generation_id] = (
                f"pending_state_write_failed:{type(exc).__name__}:{exc}"
            )

    recorded = False
    duplicate = False
    try:
        path = Path(COST_LEDGER_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_file(path, "a+", encoding="utf-8") as handle:
            handle.seek(0)
            for raw in handle:
                try:
                    existing = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(existing, dict)
                    and existing.get("kind") == "usage"
                    and str(existing.get("generation_id") or "")
                    == scope.generation_id
                    and str(existing.get("event_id") or "") == clean_event_id
                ):
                    duplicate = True
                    break
            if not duplicate:
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                recorded = True
    except OSError as exc:
        with _state_lock:
            _accounting_errors[scope.generation_id] = (
                f"ledger_write_failed:{type(exc).__name__}:{exc}"
            )
    else:
        if pending_persisted:
            try:
                _clear_pending_usage(clean_event_id)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # A stale pending copy is harmless: status de-duplicates it
                # against the committed event id.
                pass
        with _state_lock:
            _accounting_errors.pop(scope.generation_id, None)
    return {
        **generation_cost_status(scope),
        "recorded": recorded,
        "duplicate": duplicate,
        "pending_only": bool(not duplicate and not recorded and pending_new),
    }


def claim_generation_cost_notice(scope: GenerationCostScope, notice: str) -> bool:
    """Persistently claim a once-per-generation warning/trip notice."""

    clean_notice = str(notice or "").strip()
    if not clean_notice:
        raise ValueError("notice is required")
    path = Path(COST_LEDGER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with locked_file(path, "a+", encoding="utf-8") as handle:
            handle.seek(0)
            for raw in handle:
                try:
                    entry = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(entry, dict)
                    and entry.get("kind") == "notice"
                    and str(entry.get("generation_id") or "") == scope.generation_id
                    and str(entry.get("notice") or "") == clean_notice
                    and str(entry.get("policy_receipt_sha256") or "")
                    == scope.policy.receipt()["receipt_sha256"]
                ):
                    return False
            entry = {
                "schema_version": POLICY_SCHEMA_VERSION,
                "kind": "notice",
                "entry_id": uuid.uuid4().hex,
                "generation_id": scope.generation_id,
                "notice": clean_notice,
                "policy_receipt_sha256": scope.policy.receipt()["receipt_sha256"],
                "ts": time.time(),
            }
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError:
        # A warning may repeat if its notice cannot be persisted.  Enforcement
        # still fails closed through generation_cost_status.accounting_ok.
        return True


def assert_operator_cost_limit_available(scope: GenerationCostScope | None = None) -> dict:
    """Fail only for an explicitly enabled hard limit."""

    scope = scope or current_generation_cost_scope()
    status = generation_cost_status(scope)
    if scope is None or scope.policy.hard_limit_usd is None:
        return status
    if not status.get("accounting_ok"):
        raise OperatorGenerationCostLimitExceeded(
            "operator hard cost limit cannot be enforced because accounting is unavailable",
            status=status,
        )
    if status.get("hard_limit_exceeded"):
        raise OperatorGenerationCostLimitExceeded(
            f"generation spend ${status['spent_usd']:.2f} reached/exceeded operator limit "
            f"${scope.policy.hard_limit_usd:.2f}",
            status=status,
        )
    return status
