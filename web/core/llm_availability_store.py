"""Durable control-plane pause for classified LLM availability failures.

The availability classifier is pure; this module owns the small persistent
state machine used by the orchestrator and Worker workflow.  Manual failures
(billing-cycle exhaustion and invalid authentication) never self-heal.  A
restart may resume them only when the operator supplies the exact evidence
digest through ``POK_LLM_RESUME_EVIDENCE_DIGEST`` at the parent-process startup
boundary.  That acknowledgement is removed from the environment before any SDK
child starts; ordinary runtime reads never consult it.  Transient failures
retain an auditable pause record but may resume after a bounded, system-owned
cooldown.

The state lives under the runtime ``RESULTS_DIR`` and is deliberately outside
the generation checkpoint.  Pausing the provider must not mutate candidate
identity, gate results, or consume a Worker attempt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import evolution_infra
from llm_availability import (
    LLMAvailabilityBlocked,
    LLMAvailabilityIssue,
    QUOTA_429,
    SERVICE_UNAVAILABLE,
    TRANSPORT_UNAVAILABLE,
)


SCHEMA_VERSION = 1
RESUME_ENV = "POK_LLM_RESUME_EVIDENCE_DIGEST"
PAUSE_FILENAME = "llm_availability_pause.json"
LOCK_FILENAME = ".llm_availability_pause.lock"

_AUTO_COOLDOWN_SECONDS = {
    SERVICE_UNAVAILABLE: 120,
    TRANSPORT_UNAVAILABLE: 60,
}
_CATEGORY_PRIORITY = {
    TRANSPORT_UNAVAILABLE: 1,
    SERVICE_UNAVAILABLE: 2,
    QUOTA_429: 3,
    "invalid_auth": 4,
    "billing_cycle_usage_limit": 5,
}


class LLMAvailabilityPauseError(RuntimeError):
    """The durable pause record is invalid or cannot be safely updated."""


def _results_dir() -> Path:
    # Resolve dynamically so isolated tests and alternate runtime roots can
    # monkeypatch evolution_infra.RESULTS_DIR without reimporting this module.
    return Path(evolution_infra.RESULTS_DIR)


def pause_path() -> Path:
    return _results_dir() / PAUSE_FILENAME


def _lock_path() -> Path:
    return _results_dir() / LOCK_FILENAME


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_provider_reset_time(value: object) -> datetime | None:
    """Parse an explicit provider timestamp; naive values use host local time."""

    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # The current provider's Chinese reset timestamp is local wall time,
        # matching the legacy rate-limiter contract. ``astimezone`` attaches
        # the configured host timezone before normalising to UTC.
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _trusted_quota_reset(
    value: object,
    *,
    now: datetime,
) -> datetime | None:
    reset = _parse_provider_reset_time(value)
    if reset is None:
        return None
    # Permit small clock skew, but reject stale or absurd reset claims instead
    # of turning them into an automatic resume authority.
    if reset < now - timedelta(seconds=60):
        return None
    if reset > now + timedelta(days=31):
        return None
    return reset


def _read_unlocked(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LLMAvailabilityPauseError(
            f"invalid LLM availability pause record: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise LLMAvailabilityPauseError("invalid LLM availability pause schema")
    return value


def _write_unlocked(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class _PauseLock:
    def __enter__(self):
        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self._handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self._handle, fcntl.LOCK_UN)
        self._handle.close()


def load_llm_pause() -> dict | None:
    """Load the last pause projection, including inactive audit records."""

    with _PauseLock():
        value = _read_unlocked(pause_path())
    return dict(value) if value is not None else None


def _normalise_pause_input(value: LLMAvailabilityBlocked | LLMAvailabilityIssue | dict) -> dict:
    if isinstance(value, LLMAvailabilityBlocked):
        return value.pause_state()
    if isinstance(value, LLMAvailabilityIssue):
        from llm_availability import build_llm_pause_state

        return build_llm_pause_state(value)
    if isinstance(value, dict):
        result = dict(value)
    else:
        raise TypeError("pause value must be an availability issue, exception, or dict")
    required = {
        "category",
        "summary",
        "retry_policy",
        "requires_manual_resume",
        "evidence_digest",
    }
    if not required.issubset(result):
        missing = ", ".join(sorted(required - set(result)))
        raise LLMAvailabilityPauseError(f"pause state missing required fields: {missing}")
    return result


def persist_llm_pause(
    value: LLMAvailabilityBlocked | LLMAvailabilityIssue | dict,
    *,
    now: datetime | None = None,
) -> dict:
    """Persist a classified pause without allowing weaker evidence to replace it."""

    incoming = _normalise_pause_input(value)
    timestamp = _utc_now(now)
    category = str(incoming["category"])
    digest = str(incoming["evidence_digest"])
    provider_reset = (
        _trusted_quota_reset(incoming.get("provider_reset_at"), now=timestamp)
        if category == QUOTA_429
        else None
    )
    manual = bool(incoming["requires_manual_resume"])
    if category == QUOTA_429:
        # A bare 429 has no safe automatic retry time. Only a validated reset
        # timestamp carried by provider-owned evidence can clear this pause.
        manual = provider_reset is None
    cooldown = None if manual else int(_AUTO_COOLDOWN_SECONDS.get(category, 120))

    with _PauseLock():
        path = pause_path()
        current = _read_unlocked(path)
        if current and current.get("active"):
            old_priority = _CATEGORY_PRIORITY.get(str(current.get("category")), 0)
            new_priority = _CATEGORY_PRIORITY.get(category, 0)
            same_category = str(current.get("category")) == category
            current_reset = (
                _parse_time(current.get("provider_reset_at"))
                if category == QUOTA_429 and same_category
                else None
            )
            quota_reset_upgrade = bool(
                category == QUOTA_429
                and provider_reset is not None
                and (current_reset is None or provider_reset > current_reset)
            )
            if old_priority > new_priority or (
                old_priority == new_priority
                and same_category
                and not quota_reset_upgrade
            ):
                current = dict(current)
                current["last_observed_at"] = _iso(timestamp)
                current["occurrences"] = int(current.get("occurrences") or 1) + 1
                if digest != str(current.get("evidence_digest") or ""):
                    current["last_suppressed_category"] = category
                    current["last_suppressed_evidence_digest"] = digest
                _write_unlocked(path, current)
                return current

        first_observed_at = (
            current.get("first_observed_at")
            if current and current.get("active") and current.get("category") == category
            else incoming.get("observed_at") or _iso(timestamp)
        )
        occurrences = (
            int(current.get("occurrences") or 1) + 1
            if current and current.get("active") and current.get("category") == category
            else 1
        )
        auto_resume_at = None
        if not manual:
            auto_resume_at = (
                _iso(provider_reset)
                if category == QUOTA_429 and provider_reset is not None
                else _iso(timestamp + timedelta(seconds=cooldown))
            )
        state = {
            "schema_version": SCHEMA_VERSION,
            "active": True,
            "source": "llm_availability",
            "category": category,
            "summary": str(incoming["summary"]),
            "http_status": incoming.get("http_status"),
            "retry_policy": str(incoming["retry_policy"]),
            "requires_manual_resume": manual,
            "persistent_pause": True,
            "evidence_digest": digest,
            "provider_reset_at": (
                _iso(provider_reset) if provider_reset is not None else None
            ),
            "role": incoming.get("role"),
            "first_observed_at": first_observed_at,
            "last_observed_at": _iso(timestamp),
            "occurrences": occurrences,
            "auto_resume_at": auto_resume_at,
        }
        _write_unlocked(path, state)
        return state


def _reconcile_llm_pause(
    *,
    now: datetime | None = None,
    operator_resume_digest: str | None = None,
) -> dict | None:
    """Internal projection update for startup acknowledgement or cooldown."""

    timestamp = _utc_now(now)
    supplied = str(operator_resume_digest or "").strip()

    with _PauseLock():
        path = pause_path()
        current = _read_unlocked(path)
        if not current or not current.get("active"):
            return dict(current) if current else None

        manual = bool(current.get("requires_manual_resume"))
        reset = None
        if str(current.get("category") or "") == QUOTA_429:
            reset = _parse_time(current.get("provider_reset_at"))
            # Schema-1 records created by the old fixed-five-minute policy have
            # no provider_reset_at. Treat them as manual instead of honoring
            # their guessed auto_resume_at.
            manual = reset is None
            if manual and (
                current.get("requires_manual_resume") is not True
                or current.get("auto_resume_at") is not None
            ):
                current = dict(current)
                current["requires_manual_resume"] = True
                current["retry_policy"] = "manual_resume_without_provider_reset"
                current["auto_resume_at"] = None
                _write_unlocked(path, current)
        resume_source = None
        if manual:
            if supplied and supplied == str(current.get("evidence_digest") or ""):
                resume_source = "operator_evidence_digest"
            elif supplied:
                current = dict(current)
                current["last_rejected_resume_at"] = _iso(timestamp)
                current["last_rejected_resume_digest"] = supplied
                _write_unlocked(path, current)
                return current
        else:
            due_at = (
                reset
                if str(current.get("category") or "") == QUOTA_429
                else _parse_time(current.get("auto_resume_at"))
            )
            if due_at is not None and timestamp >= due_at:
                resume_source = (
                    "provider_quota_reset_elapsed"
                    if str(current.get("category") or "") == QUOTA_429
                    else "bounded_cooldown_elapsed"
                )

        if resume_source is None:
            return dict(current)

        resumed = dict(current)
        resumed["active"] = False
        resumed["resumed_at"] = _iso(timestamp)
        resumed["resume_source"] = resume_source
        resumed["resume_evidence_digest"] = (
            supplied if resume_source == "operator_evidence_digest" else None
        )
        _write_unlocked(path, resumed)
        return resumed


def consume_operator_resume_ack_from_env(
    *, now: datetime | None = None
) -> dict | None:
    """Consume the one-shot operator acknowledgement at process startup.

    This is the *only* path that reads ``RESUME_ENV``.  The value is popped
    before durable state is inspected, so neither SDK subprocesses nor later
    in-process role calls inherit usable resume authority.  Callers must invoke
    this once at the parent launcher boundary before any LLM work is spawned.
    """

    supplied = os.environ.pop(RESUME_ENV, "").strip()
    return _reconcile_llm_pause(
        now=now,
        operator_resume_digest=supplied or None,
    )


def reconcile_llm_pause(*, now: datetime | None = None) -> dict | None:
    """Apply only a due system-owned transient cooldown.

    Manual acknowledgement is intentionally unavailable at runtime.  In
    particular, setting ``RESUME_ENV`` after startup has no effect here.
    """

    return _reconcile_llm_pause(now=now)


def active_llm_pause(*, now: datetime | None = None) -> dict | None:
    state = reconcile_llm_pause(now=now)
    if state and state.get("active"):
        return state
    return None


def is_llm_paused(*, now: datetime | None = None) -> bool:
    return active_llm_pause(now=now) is not None


def blocked_from_pause_state(
    state: dict,
    *,
    role: str | None = None,
) -> LLMAvailabilityBlocked:
    """Rehydrate the typed exception at any process-local LLM call boundary."""

    if not isinstance(state, dict) or not state.get("active"):
        raise LLMAvailabilityPauseError("cannot block from an inactive pause state")
    issue = LLMAvailabilityIssue(
        category=str(state.get("category") or "transport_unavailable"),
        summary=str(state.get("summary") or "provider unavailable"),
        http_status=(
            int(state["http_status"])
            if state.get("http_status") is not None
            else None
        ),
        retry_policy=str(state.get("retry_policy") or "manual_resume"),
        requires_manual_resume=bool(state.get("requires_manual_resume")),
        evidence_digest=str(state.get("evidence_digest") or ""),
        provider_reset_at=(
            str(state.get("provider_reset_at"))
            if state.get("provider_reset_at")
            else None
        ),
    )
    if not issue.evidence_digest:
        raise LLMAvailabilityPauseError("active pause has no evidence digest")
    return LLMAvailabilityBlocked(issue, role=role or state.get("role"))


def raise_if_llm_paused(*, role: str | None = None) -> None:
    state = active_llm_pause()
    if state is not None:
        raise blocked_from_pause_state(state, role=role)


def pause_wait_seconds(state: dict, *, now: datetime | None = None) -> float | None:
    """Return remaining automatic wait; ``None`` denotes a manual stop."""

    timestamp = _utc_now(now)
    if bool(state.get("requires_manual_resume")):
        return None
    if str(state.get("category") or "") == QUOTA_429 and _parse_time(
        state.get("provider_reset_at")
    ) is None:
        return None
    due_at = _parse_time(state.get("auto_resume_at"))
    if due_at is None:
        return 0.0
    return max(0.0, (due_at - timestamp).total_seconds())


__all__ = [
    "LLMAvailabilityPauseError",
    "RESUME_ENV",
    "active_llm_pause",
    "blocked_from_pause_state",
    "consume_operator_resume_ack_from_env",
    "is_llm_paused",
    "load_llm_pause",
    "pause_path",
    "pause_wait_seconds",
    "persist_llm_pause",
    "reconcile_llm_pause",
    "raise_if_llm_paused",
]
