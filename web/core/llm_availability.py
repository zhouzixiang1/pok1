"""Pure LLM availability classification and pause-state projection.

SDK/proxy failures are sometimes split across several stream messages.  In
particular, the useful HTTP error can arrive in an assistant ``TextBlock``, be
followed by ``ResultMessage(is_error=True, subtype="success")``, and then be
replaced by the SDK's generic ``error result: success`` exception.  This module
keeps those observations together and applies one deterministic priority order.

The module intentionally performs no I/O.  ``build_llm_pause_state`` returns a
JSON-serialisable value which the orchestrator may persist at its own state
boundary; classification itself never edits pipeline state or sleeps.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import re
from typing import Iterable, Optional


BILLING_CYCLE_LIMIT = "billing_cycle_usage_limit"
INVALID_AUTH = "invalid_auth"
QUOTA_429 = "quota_429"
SERVICE_UNAVAILABLE = "service_unavailable"
TRANSPORT_UNAVAILABLE = "transport_unavailable"

_HTTP_STATUS_RE = re.compile(
    r"(?:http(?:/\d(?:\.\d)?)?\s*|status(?:\s+code)?[\s:=]*|api\s+error[\s:=]*)"
    r"(?P<status>401|403|429|503|529)\b",
    re.IGNORECASE,
)
_PROVIDER_RESET_RE = re.compile(
    r"(?:限额将在\s*|(?:quota|limit)\s+(?:will\s+)?resets?"
    r"(?:\s+(?:at|on))?\s*[:=]?\s*)"
    r"(?P<reset>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)

_TRANSPORT_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ECONNREFUSED", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "EHOSTDOWN", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "ENETRESET", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "ETIMEDOUT", None),
    )
    if value is not None
)


@dataclass(frozen=True)
class LLMAvailabilityIssue:
    """A classified provider-availability failure."""

    category: str
    summary: str
    http_status: Optional[int]
    retry_policy: str
    requires_manual_resume: bool
    evidence_digest: str
    provider_reset_at: Optional[str] = None

    @property
    def persistent_pause(self) -> bool:
        """All availability failures may be persisted by the orchestrator."""
        return True

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "summary": self.summary,
            "http_status": self.http_status,
            "retry_policy": self.retry_policy,
            "requires_manual_resume": self.requires_manual_resume,
            "persistent_pause": self.persistent_pause,
            "evidence_digest": self.evidence_digest,
            "provider_reset_at": self.provider_reset_at,
        }


class LLMAvailabilityBlocked(RuntimeError):
    """Typed boundary raised when an LLM provider is unavailable."""

    def __init__(self, issue: LLMAvailabilityIssue, *, role: str | None = None):
        self.issue = issue
        self.role = str(role) if role else None
        prefix = f"{self.role}: " if self.role else ""
        super().__init__(f"{prefix}LLM unavailable [{issue.category}]: {issue.summary}")

    def pause_state(self, *, observed_at: str | None = None) -> dict:
        return build_llm_pause_state(
            self.issue,
            role=self.role,
            observed_at=observed_at,
        )


async def gather_llm_fail_fast(*awaitables):
    """Gather ordered results while making provider pauses fail fast.

    The evolution pipeline deliberately aggregates ordinary role failures so it
    can apply its bounded schema-repair policy.  A provider availability pause
    is different: once any role observes it, continuing sibling SDK streams can
    only spend money and race additional writes after the global pause.  Cancel
    every unfinished sibling immediately and await their cleanup before
    propagating the typed pause.  Awaiting the cancelled tasks is essential:
    ``run_claude_query`` closes its SDK generator in ``finally``.

    Results retain input order and ordinary exceptions are returned as values,
    matching ``asyncio.gather(..., return_exceptions=True)``.  External task
    cancellation also drains all children before it escapes.
    """

    if not awaitables:
        return []

    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    indexes = {task: index for index, task in enumerate(tasks)}
    results = [None] * len(tasks)
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # A simultaneous sibling cancellation must not mask the global
            # provider pause that triggered this completion batch.
            for task in done:
                if not task.cancelled() and isinstance(
                    task.exception(), LLMAvailabilityBlocked
                ):
                    raise task.exception()
            for task in done:
                index = indexes[task]
                if task.cancelled():
                    raise asyncio.CancelledError()
                exception = task.exception()
                results[index] = exception if exception is not None else task.result()
        return results
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _normalise_statuses(statuses: Iterable[object]) -> tuple[int, ...]:
    result = set()
    for value in statuses or ():
        try:
            status = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            result.add(status)
    return tuple(sorted(result))


def _bounded_evidence(parts: Iterable[object]) -> tuple[str, ...]:
    values = []
    remaining = 12_000
    for part in parts or ():
        if part is None or remaining <= 0:
            continue
        value = str(part).strip()
        if not value:
            continue
        value = value[: min(4_000, remaining)]
        values.append(value)
        remaining -= len(value)
    return tuple(values)


def _status_from_text(text: str) -> tuple[int, ...]:
    return tuple(sorted({int(match.group("status")) for match in _HTTP_STATUS_RE.finditer(text)}))


def looks_like_provider_error_envelope(text: str) -> bool:
    """Recognize a terse provider envelope, not prose discussing an outage.

    Some SDK/proxy versions surface the provider failure in an
    ``AssistantMessage`` before an uninformative failed result.  That text is
    usable only when it has the shape of the provider envelope itself.  In
    particular, a sentence which merely contains words such as ``overloaded``
    or ``rate limit`` is deliberately excluded.
    """

    value = str(text or "").lstrip()
    return bool(
        re.match(
            r"^(?:api\s+error\s*[:=]?\s*|http(?:/\d(?:\.\d)?)?\s*|"
            r"status(?:\s+code)?\s*[:=]?\s*)?(?:401|403|429|503|529)\b",
            value,
            re.IGNORECASE,
        )
        or re.match(r"^request rejected \(429\)", value, re.IGNORECASE)
        or re.match(
            r"^(?:error\s*:\s*)?(?:(?:model|service|provider)\s+)?"
            r"(?:is\s+)?overloaded(?:\s*[,;:.!-]?\s*(?:please\s+retry|try\s+again))?[.!]?$",
            value,
            re.IGNORECASE,
        )
        or re.match(
            r"^(?:error\s*:\s*)?rate limit(?:ed| reached)(?:[.!]|\s*)$",
            value,
            re.IGNORECASE,
        )
        or (value.startswith("已达到") and "使用上限" in value)
        or value.startswith("该模型当前访问量过大")
        or (value.startswith("所有供应商") and "熔断" in value)
    )


def _digest(category: str, statuses: tuple[int, ...], evidence: tuple[str, ...]) -> str:
    canonical = json.dumps(
        {
            "category": category,
            "statuses": list(statuses),
            "evidence": list(evidence),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _provider_reset_evidence(evidence: tuple[str, ...]) -> str | None:
    """Return an explicit provider reset timestamp, never a guessed cooldown."""

    matches = [
        match.group("reset").strip()
        for item in evidence
        for match in _PROVIDER_RESET_RE.finditer(item)
    ]
    return matches[-1] if matches else None


def _issue(
    category: str,
    summary: str,
    statuses: tuple[int, ...],
    evidence: tuple[str, ...],
) -> LLMAvailabilityIssue:
    preferred_status = {
        BILLING_CYCLE_LIMIT: 403,
        INVALID_AUTH: 401,
        QUOTA_429: 429,
        SERVICE_UNAVAILABLE: 529,
    }.get(category)
    http_status = preferred_status if preferred_status in statuses else (statuses[0] if statuses else None)
    provider_reset_at = (
        _provider_reset_evidence(evidence) if category == QUOTA_429 else None
    )
    if category in {BILLING_CYCLE_LIMIT, INVALID_AUTH}:
        retry_policy = "manual_resume"
        manual = True
    elif category == QUOTA_429:
        # A bare 429 does not prove when the quota window ends. Guessing five
        # minutes caused repeated paid retries against multi-hour quota caps.
        # Only an explicit provider timestamp permits automatic reconciliation.
        retry_policy = (
            "resume_after_quota_reset"
            if provider_reset_at
            else "manual_resume_without_provider_reset"
        )
        manual = provider_reset_at is None
    else:
        retry_policy = "bounded_backoff"
        manual = False
    return LLMAvailabilityIssue(
        category=category,
        summary=summary,
        http_status=http_status,
        retry_policy=retry_policy,
        requires_manual_resume=manual,
        evidence_digest=_digest(category, statuses, evidence),
        provider_reset_at=provider_reset_at,
    )


def classify_llm_availability(
    evidence: Iterable[object] = (),
    *,
    statuses: Iterable[object] = (),
    exception: BaseException | None = None,
) -> LLMAvailabilityIssue | None:
    """Classify accumulated stream/exception evidence using a fixed priority.

    Priority is billing-cycle exhaustion, invalid authentication, 429 quota,
    529/503 service availability, then transport.  An uninformative trailing
    exception such as ``error result: success`` contributes evidence but cannot
    replace a higher-priority diagnosis observed earlier in the stream.
    """

    parts = list(evidence or ())
    if exception is not None:
        parts.append(f"{type(exception).__name__}: {exception}")
    bounded = _bounded_evidence(parts)
    joined = "\n".join(bounded)
    lower = joined.lower()
    all_statuses = set(_normalise_statuses(statuses))
    all_statuses.update(_status_from_text(joined))
    status_tuple = tuple(sorted(all_statuses))

    billing = (
        "usage limit for this billing cycle" in lower
        or "usage limit for the current billing cycle" in lower
        or ("billing cycle" in lower and ("usage limit" in lower or "limit reached" in lower))
        or ("账单周期" in joined and ("使用上限" in joined or "限额" in joined))
        or ("结算周期" in joined and ("使用上限" in joined or "限额" in joined))
    )
    if billing:
        return _issue(
            BILLING_CYCLE_LIMIT,
            "provider billing-cycle usage limit reached",
            status_tuple,
            bounded,
        )

    auth = (
        401 in all_statuses
        or "authentication_error" in lower
        or "authentication failed" in lower
        or "failed to authenticate" in lower
        or "invalid api key" in lower
        or "invalid x-api-key" in lower
        or "invalid bearer" in lower
        or "unauthorized" in lower
        or "expired api key" in lower
        or "token has expired" in lower
        or (
            403 in all_statuses
            and (
                "forbidden" in lower
                or "permission denied" in lower
                or "access denied" in lower
                or "invalid token" in lower
            )
        )
        or re.search(
            r"(?:\bauth(?:entication)?\b|\bapi\s+key\b).{0,48}\b(?:401|403)\b"
            r"|\b(?:401|403)\b.{0,48}(?:\bauth(?:entication)?\b|\bapi\s+key\b)",
            lower,
        ) is not None
    )
    if auth:
        return _issue(
            INVALID_AUTH,
            "provider authentication is invalid or forbidden",
            status_tuple,
            bounded,
        )

    quota = (
        429 in all_statuses
        or "request rejected (429)" in lower
        or "too many requests" in lower
        or "quota exceeded" in lower
        or ("已达到" in joined and "使用上限" in joined)
        or re.search(
            r"(?:\berror\b|\bfailed\b|\brejected\b|\bquota\b).{0,48}\b429\b"
            r"|\b429\b.{0,48}(?:\berror\b|\bfailed\b|\brejected\b|\bquota\b)",
            lower,
        ) is not None
    )
    if quota:
        return _issue(
            QUOTA_429,
            "provider quota window is exhausted",
            status_tuple,
            bounded,
        )

    unavailable = (
        529 in all_statuses
        or 503 in all_statuses
        or "overloaded" in lower
        or "service unavailable" in lower
        or "temporarily unavailable" in lower
        or "该模型当前访问量过大" in joined
        or ("所有供应商" in joined and "熔断" in joined)
        or re.search(
            r"(?:\berror\b|\bfailed\b|\bprovider\b|\brequest\b).{0,48}\b(?:503|529)\b"
            r"|\b(?:503|529)\b.{0,48}(?:\berror\b|\bfailed\b|\bunavailable\b|\boverloaded\b)",
            lower,
        ) is not None
    )
    if unavailable:
        return _issue(
            SERVICE_UNAVAILABLE,
            "provider service is overloaded or unavailable",
            status_tuple,
            bounded,
        )

    # ``OSError`` also represents local failures such as ENOSPC/EACCES.  Those
    # are infrastructure faults and must never create a durable provider pause.
    # Classify only the network-specific subclasses/errno values here; textual
    # evidence below still covers SDK wrappers which erase the original errno.
    transport_exception = isinstance(exception, (ConnectionError, TimeoutError))
    if isinstance(exception, OSError) and not transport_exception:
        transport_exception = getattr(exception, "errno", None) in _TRANSPORT_ERRNOS
    transport_text = any(
        marker in lower
        for marker in (
            "connection reset",
            "connection refused",
            "connection aborted",
            "network is unreachable",
            "temporary failure in name resolution",
            "name or service not known",
            "dns lookup",
            "broken pipe",
            "tls handshake",
            "socket timeout",
            "connect timeout",
            "read timeout",
        )
    )
    if transport_exception or transport_text:
        return _issue(
            TRANSPORT_UNAVAILABLE,
            "provider transport is unavailable",
            status_tuple,
            bounded,
        )
    return None


def build_llm_pause_state(
    issue: LLMAvailabilityIssue,
    *,
    role: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Project an issue into a pure, JSON-serialisable persistent pause record."""

    if not isinstance(issue, LLMAvailabilityIssue):
        raise TypeError("issue must be an LLMAvailabilityIssue")
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "active": True,
        "source": "llm_availability",
        "observed_at": timestamp,
        "role": str(role) if role else None,
        **issue.as_dict(),
    }


class LLMAvailabilityTrace:
    """Bounded accumulator for one SDK stream attempt."""

    def __init__(self) -> None:
        self._assistant_evidence: list[str] = []
        self._provider_evidence: list[str] = []
        self._statuses: set[int] = set()
        self._saw_result = False
        self._result_failed = False
        self._credible_success_result = False

    def observe_text(self, text: object) -> None:
        if (
            text is not None
            and sum(len(item) for item in self._assistant_evidence) < 12_000
        ):
            self._assistant_evidence.append(str(text)[:4_000])

    def _observe_provider_text(self, text: object) -> None:
        if (
            text is not None
            and sum(len(item) for item in self._provider_evidence) < 12_000
        ):
            self._provider_evidence.append(str(text)[:4_000])

    def observe_result(self, result: object) -> None:
        self._saw_result = True
        status = getattr(result, "api_error_status", None)
        try:
            if status is not None:
                self._statuses.add(int(status))
        except (TypeError, ValueError):
            pass
        is_error = bool(getattr(result, "is_error", False))
        subtype = getattr(result, "subtype", None)
        self._result_failed = self._result_failed or is_error or bool(subtype and subtype != "success")
        try:
            num_turns = int(getattr(result, "num_turns", 0) or 0)
        except (TypeError, ValueError):
            num_turns = 0
        self._credible_success_result = self._credible_success_result or bool(
            not is_error
            and (subtype in {None, "success"})
            and num_turns > 0
        )
        errors = getattr(result, "errors", None) or ()
        for error in errors:
            self._observe_provider_text(error)
        result_text = getattr(result, "result", None)
        if result_text:
            self._observe_provider_text(result_text)

    def classify(
        self,
        *,
        exception: BaseException | None = None,
        require_failure_signal: bool = True,
    ) -> LLMAvailabilityIssue | None:
        # Result diagnostics and transport exceptions are provider-owned. Model
        # prose is not: an ordinary answer may discuss quotas or outages. Use
        # Assistant text only when the SDK independently reports the same HTTP
        # status for this failed ResultMessage.
        matching_assistant = tuple(
            item
            for item in self._assistant_evidence
            if (self._result_failed or exception is not None)
            and (
                set(_status_from_text(item)).intersection(self._statuses)
                or looks_like_provider_error_envelope(item)
            )
        )
        issue = classify_llm_availability(
            (*self._provider_evidence, *matching_assistant),
            statuses=self._statuses,
            exception=exception,
        )
        if issue is None:
            return None
        # Avoid treating an ordinary successful model answer which merely talks
        # about quotas as a provider outage.  Explicit HTTP codes, an error
        # ResultMessage, or a matching transport exception are failure signals.
        exception_issue = (
            classify_llm_availability((), exception=exception)
            if exception is not None
            else None
        )
        text_statuses = {
            status
            for item in (*self._provider_evidence, *self._assistant_evidence)
            for status in _status_from_text(item)
        }
        strong_manual_provider_error = bool(
            not self._credible_success_result
            and (
                (issue.category == BILLING_CYCLE_LIMIT and 403 in text_statuses)
                or (
                    issue.category == INVALID_AUTH
                    and text_statuses.intersection({401, 403})
                )
            )
        )
        failure_signal = bool(
            self._statuses
            or self._result_failed
            or exception_issue is not None
            or (exception is not None and not self._saw_result)
            or strong_manual_provider_error
        )
        return issue if (failure_signal or not require_failure_signal) else None

    def blocked(
        self,
        *,
        role: str | None = None,
        exception: BaseException | None = None,
        require_failure_signal: bool = True,
    ) -> LLMAvailabilityBlocked | None:
        issue = self.classify(
            exception=exception,
            require_failure_signal=require_failure_signal,
        )
        return LLMAvailabilityBlocked(issue, role=role) if issue is not None else None


__all__ = [
    "BILLING_CYCLE_LIMIT",
    "INVALID_AUTH",
    "QUOTA_429",
    "SERVICE_UNAVAILABLE",
    "TRANSPORT_UNAVAILABLE",
    "LLMAvailabilityIssue",
    "LLMAvailabilityBlocked",
    "LLMAvailabilityTrace",
    "gather_llm_fail_fast",
    "looks_like_provider_error_envelope",
    "classify_llm_availability",
    "build_llm_pause_state",
]
