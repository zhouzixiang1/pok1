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
    QUOTA_RESET_AUTHORITY_FALLBACK,
    QUOTA_RESET_AUTHORITY_PROVIDER,
    SERVICE_UNAVAILABLE,
    TRANSPORT_UNAVAILABLE,
    service_unavailable_cooldown_seconds,
)


SCHEMA_VERSION = 2
# Schema 1 records predate ``resume_receipt_history`` (2026-10-05). They stay
# loadable; the first persist after the upgrade rewrites the store as schema 2
# and archives the overwritten record's resume receipt into the new history.
_LEGACY_SCHEMA_VERSIONS = (1,)
RESUME_ENV = "POK_LLM_RESUME_EVIDENCE_DIGEST"
PAUSE_FILENAME = "llm_availability_pause.json"
LOCK_FILENAME = ".llm_availability_pause.lock"
# Bounded FIFO archive of overwritten resume receipts. A Worker availability
# deferral freezes the pause projection it deferred on; when a burst of new
# pauses (e.g. recurring GLM 1302) overwrites the store before the Worker
# resumes, the archived receipts are the only durable proof that the deferred
# evidence was reconciled through an allowed resume path.
# F-A (2026-10-09, v532 wedge): raised 8 -> 64. The 05:01-05:32 GLM storm
# archived 8 rapid resumes and evicted the receipt of the evidence a deferred
# Worker effect still held, wedging every revival on
# ``no_archived_match``. 64 covers a sustained same-day storm; the durable
# validator additionally authorizes through the suppressed-evidence chain
# (store-held ``last_suppressed_evidence_digest`` markers — see
# ``tool_planning_worker_durable``), which unlike the receipts is structurally
# bound to the frozen digest, so the archive is defense-in-depth rather than
# a single point of failure.
RESUME_RECEIPT_HISTORY_CAP = 64
_RESUME_RECEIPT_PROJECTION_FIELDS = (
    "category",
    "evidence_digest",
    "retry_policy",
    "http_status",
    "requires_manual_resume",
    "resumed_at",
    "resume_source",
    "resume_evidence_digest",
    # F-H (2026-10-09 re-review): a suppressed recurrence's evidence digest
    # never becomes a record digest (the suppression branch keeps the old
    # record and only marks the incoming digest), so no resume receipt can
    # ever exist for it.  Carrying the suppression marker into the archived
    # receipt projection keeps that store-held proof durable across record
    # replacement and archive churn (the Worker validator's suppressed
    # evidence chain scans these markers; see
    # ``tool_planning_worker_durable``).
    "last_suppressed_category",
    "last_suppressed_evidence_digest",
)

_AUTO_COOLDOWN_SECONDS = {
    # SERVICE_UNAVAILABLE is deliberately absent: its cooldown follows the
    # shared exponential curve ``service_unavailable_cooldown_seconds`` (P1,
    # 2026-10-05), computed inside the pause lock where the occurrence count
    # is known. TRANSPORT_UNAVAILABLE keeps its flat fixed window.
    TRANSPORT_UNAVAILABLE: 60,
}
_TRUSTED_QUOTA_RESET_AUTHORITIES = frozenset(
    {QUOTA_RESET_AUTHORITY_PROVIDER, QUOTA_RESET_AUTHORITY_FALLBACK}
)
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


#: P3 (2026-10-05, F10): same-category SERVICE_UNAVAILABLE recurrences inside
#: this sliding window after an auto-resumed pause carry the ``occurrences``
#: count forward, so the durable cooldown keeps walking the shared
#: exponential curve toward the 120s cap instead of sawtoothing from 8s on
#: every cleared window (orchestrator journal: 15/31/30/62s zig-zag).
SERVICE_UNAVAILABLE_RECURRENCE_WINDOW_SEC = 1800.0


def _recurrence_carries_count(
    current: dict | None, category: str, timestamp: datetime
) -> bool:
    """Whether a new ``category`` record continues the prior occurrence run.

    Same rule as before for an ACTIVE pause (and for every non-
    SERVICE_UNAVAILABLE category); additionally a just-auto-resumed
    SERVICE_UNAVAILABLE record whose ``last_observed_at`` is inside the
    30-minute sliding window also carries the count (P3).
    """

    if not current or current.get("category") != category:
        return False
    if current.get("active"):
        return True
    if category != SERVICE_UNAVAILABLE:
        return False
    last = _parse_time(current.get("last_observed_at"))
    if last is None:
        return False
    elapsed = (timestamp - last).total_seconds()
    return 0 <= elapsed <= SERVICE_UNAVAILABLE_RECURRENCE_WINDOW_SEC


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
    if not isinstance(value, dict) or value.get("schema_version") not in (
        SCHEMA_VERSION,
        *_LEGACY_SCHEMA_VERSIONS,
    ):
        raise LLMAvailabilityPauseError("invalid LLM availability pause schema")
    return value


def _resume_receipt_projection(record: dict | None) -> dict | None:
    """Project an inactive record's resume receipt for the bounded history.

    Only records that actually went through a resume path (``resumed_at`` +
    ``resume_source`` both present) carry a receipt; anything else returns
    ``None`` and is never archived. Garbage projections (empty digest or
    category) are likewise refused so the archive cannot grow an entry that
    the Worker receipt validator could never match anyway.
    """

    if not isinstance(record, dict) or record.get("active"):
        return None
    if not str(record.get("resumed_at") or ""):
        return None
    if not str(record.get("resume_source") or ""):
        return None
    if not str(record.get("evidence_digest") or ""):
        return None
    if not str(record.get("category") or ""):
        return None
    return {key: record.get(key) for key in _RESUME_RECEIPT_PROJECTION_FIELDS}


def _carried_resume_receipt_history(record: dict | None) -> list[dict]:
    """Sanitize and bound the history carried forward from an existing record."""

    if not isinstance(record, dict):
        return []
    raw = record.get("resume_receipt_history")
    if not isinstance(raw, list):
        return []
    history = [
        dict(entry)
        for entry in raw
        if isinstance(entry, dict) and _resume_receipt_projection(entry) is not None
    ]
    return history[-RESUME_RECEIPT_HISTORY_CAP:]


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
    authority = str(incoming.get("quota_reset_authority") or "").strip()
    provider_reset = None
    manual = bool(incoming["requires_manual_resume"])
    if category == QUOTA_429:
        # Two trusted sources: an explicit provider timestamp, or a fallback
        # invented only for a confirmed 1308/usage-cap body. Anything else
        # (bare 429, GLM 1302) must not become a multi-hour auto-wait.
        if authority == QUOTA_RESET_AUTHORITY_PROVIDER:
            provider_reset = _trusted_quota_reset(
                incoming.get("provider_reset_at"), now=timestamp
            )
            manual = provider_reset is None
        elif authority == QUOTA_RESET_AUTHORITY_FALLBACK:
            provider_reset = _trusted_quota_reset(
                incoming.get("provider_reset_at"), now=timestamp
            )
            # Classifier always stamps the fallback instant. Missing/stale
            # values fail closed rather than inventing a second wait here.
            manual = provider_reset is None
        else:
            provider_reset = None
            manual = True

    with _PauseLock():
        path = pause_path()
        current = _read_unlocked(path)
        # Receipt archive (schema 2): carry the bounded history forward across
        # every record this store writes, and archive the overwritten record's
        # resume receipt when it actually went through a resume path. The
        # suppressed-recurrence branch below rewrites the same record via a
        # dict copy, so its history is preserved by construction there.
        resume_history = _carried_resume_receipt_history(current)
        archived_receipt = _resume_receipt_projection(current)
        if (
            current
            and current.get("active")
            and str(current.get("category") or "") == QUOTA_429
            and str(current.get("quota_reset_authority") or "").strip()
            not in _TRUSTED_QUOTA_RESET_AUTHORITIES
        ):
            current = dict(current)
            current["active"] = False
            current["resumed_at"] = _iso(timestamp)
            current["resume_source"] = "untrusted_quota_pause_without_reset_authority"
            current["resume_evidence_digest"] = None
            _write_unlocked(path, current)
            archived_receipt = _resume_receipt_projection(current)
            current = None
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
                # P1 (2026-10-05): a same-category SERVICE_UNAVAILABLE
                # recurrence observed while the pause is still active extends
                # its own cooldown along the shared exponential curve, so a
                # sustained 1302/bare-429 burst backs off instead of
                # re-arming the same short window forever. QUOTA_429 (the
                # provider reset timestamp is the sole resume authority) and
                # TRANSPORT_UNAVAILABLE (fixed 60s) keep their contracts.
                if category == SERVICE_UNAVAILABLE and not manual:
                    extended = timestamp + timedelta(
                        seconds=service_unavailable_cooldown_seconds(
                            current["occurrences"]
                        )
                    )
                    existing_due = _parse_time(current.get("auto_resume_at"))
                    if existing_due is None or extended > existing_due:
                        current["auto_resume_at"] = _iso(extended)
                if digest != str(current.get("evidence_digest") or ""):
                    current["last_suppressed_category"] = category
                    current["last_suppressed_evidence_digest"] = digest
                _write_unlocked(path, current)
                return current

        first_observed_at = (
            current.get("first_observed_at")
            if _recurrence_carries_count(current, category, timestamp)
            else incoming.get("observed_at") or _iso(timestamp)
        )
        occurrences = (
            int(current.get("occurrences") or 1) + 1
            if _recurrence_carries_count(current, category, timestamp)
            else 1
        )
        # P1 (2026-10-05): the cooldown is computed HERE — inside the lock,
        # after the occurrence count is known — so SERVICE_UNAVAILABLE walks
        # the shared exponential curve (8 → 16 → ... → 120s) while every other
        # category keeps its fixed window and QUOTA_429 keeps the provider
        # reset as its sole resume authority below.
        cooldown = (
            None
            if manual
            else (
                service_unavailable_cooldown_seconds(occurrences)
                if category == SERVICE_UNAVAILABLE
                else int(_AUTO_COOLDOWN_SECONDS.get(category, 120))
            )
        )
        auto_resume_at = None
        if not manual:
            auto_resume_at = (
                _iso(provider_reset)
                if category == QUOTA_429 and provider_reset is not None
                else _iso(timestamp + timedelta(seconds=cooldown))
            )
        if archived_receipt is not None:
            resume_history.append(archived_receipt)
            resume_history = resume_history[-RESUME_RECEIPT_HISTORY_CAP:]
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
            "quota_reset_authority": authority or None,
            "resume_receipt_history": resume_history,
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
        resume_source = None
        if str(current.get("category") or "") == QUOTA_429:
            authority = str(current.get("quota_reset_authority") or "").strip()
            if authority not in _TRUSTED_QUOTA_RESET_AUTHORITIES:
                # Pre-fix records invented a 5-hour wait from any 429
                # (including GLM 1302 frequency limits). They are not a
                # provider-owned quota window; drop them on reconcile.
                resume_source = "untrusted_quota_pause_without_reset_authority"
            else:
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
        if resume_source is not None:
            pass
        elif manual:
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
        quota_reset_authority=(
            str(state["quota_reset_authority"])
            if state.get("quota_reset_authority")
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


def system_resume_horizon(projection: dict) -> datetime | None:
    """Latest instant a system-owned resume path can still hold a pause live.

    F-A (2026-10-09, v532 receipt-eviction wedge).  Given a *deferral-time*
    pause projection — the frozen ``availability`` record inside a deferred
    Worker effect — return the conservative upper bound of the system-owned
    resume deadline for that pause, or ``None`` when the projection can never
    self-authorize a resume (manual pause, no time basis, unknown category).

    Exact deadline first: a projection frozen from the reconciled store record
    (claim-boundary deferral) carries ``auto_resume_at``.  Exception-path
    deferrals freeze ``pause_state()`` *before* it is persisted, so those carry
    only ``observed_at`` (plus ``provider_reset_at`` for quota); the fallback
    is the category's deadline bound: every cooldown arming rule for that
    category produces a first-armed deadline at or before
    ``observed_at + bound`` (the service curve is capped at 120s for every
    occurrence count, transport is a fixed 60s, trusted quota resets at the
    provider timestamp).  A same-category recurrence can slide a *live*
    record's deadline past that bound, but only while the record stays the
    active one; the durable validator requires the live audit record to be
    inactive before any snapshot authorization, so a slid deadline implies a
    later system-owned resume already elapsed.
    """

    if not isinstance(projection, dict):
        return None
    if bool(projection.get("requires_manual_resume")):
        # Manual pauses resume only through the operator evidence digest;
        # they never self-authorize from a deferral-time snapshot.
        return None
    exact = _parse_time(projection.get("auto_resume_at"))
    if exact is not None:
        return exact
    category = str(projection.get("category") or "")
    if category == QUOTA_429:
        # Trusted quota pauses carry the provider reset timestamp and it is
        # their sole resume authority; anything else must not self-authorize.
        # F-L (2026-10-09 re-review): parse with the store's authoritative
        # host-local rule (``_parse_provider_reset_time``), not the naive->UTC
        # ``_parse_time`` — on a UTC+8 host the old read skewed the horizon
        # by the whole timezone offset against every persisted record.
        return _parse_provider_reset_time(projection.get("provider_reset_at"))
    observed = _parse_time(projection.get("observed_at"))
    if observed is None:
        return None
    if category == SERVICE_UNAVAILABLE:
        # Any occurrence count: cooldown(8) already saturates the 120s cap.
        return observed + timedelta(
            seconds=service_unavailable_cooldown_seconds(8)
        )
    if category == TRANSPORT_UNAVAILABLE:
        return observed + timedelta(
            seconds=_AUTO_COOLDOWN_SECONDS[TRANSPORT_UNAVAILABLE]
        )
    return None


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
    "system_resume_horizon",
]
