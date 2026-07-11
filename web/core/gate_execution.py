"""Typed execution outcome for trusted quality and compliance runners."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field


GateExecutionOutcome = Literal[
    "passed",
    "candidate_failure",
    "infrastructure_failure",
    "inconclusive",
    "skipped",
]
GateExecutionSide = Literal["candidate", "opponent", "server", "harness", "system"]


class GateExecution(BaseModel):
    """One runner invocation before it is projected into a pipeline gate."""

    schema_version: int = 1
    outcome: GateExecutionOutcome
    side: GateExecutionSide
    component: str
    phase: str
    retryable: bool = False
    issues: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    identity: dict[str, Any] = Field(default_factory=dict)

    @property
    def identity_digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.identity,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()

    @property
    def is_infrastructure(self) -> bool:
        return self.outcome == "infrastructure_failure"

    def get(self, key: str, default=None):
        """Small mapping-compatible bridge for existing result aggregation."""
        return getattr(self, key, default)

    @classmethod
    def infrastructure(
        cls,
        component: str,
        phase: str,
        issues,
        *,
        side: GateExecutionSide = "harness",
        identity: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> "GateExecution":
        normalized = [str(item)[:500] for item in (issues or [])]
        return cls(
            outcome="infrastructure_failure",
            side=side,
            component=str(component),
            phase=str(phase),
            retryable=retryable,
            issues=normalized or ["unspecified infrastructure failure"],
            evidence=evidence or {},
            identity=identity or {},
        )

    @classmethod
    def passed(
        cls,
        component: str,
        phase: str,
        *,
        evidence: dict[str, Any] | None = None,
        identity: dict[str, Any] | None = None,
    ) -> "GateExecution":
        return cls(
            outcome="passed",
            side="candidate",
            component=component,
            phase=phase,
            evidence=evidence or {},
            identity=identity or {},
        )

    @classmethod
    def candidate_failure(
        cls,
        component: str,
        phase: str,
        issues,
        *,
        evidence: dict[str, Any] | None = None,
        identity: dict[str, Any] | None = None,
    ) -> "GateExecution":
        return cls(
            outcome="candidate_failure",
            side="candidate",
            component=component,
            phase=phase,
            issues=[str(item)[:500] for item in (issues or [])],
            evidence=evidence or {},
            identity=identity or {},
        )
