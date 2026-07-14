"""Structured pipeline records shared by gates, candidates, and reports.

These models are intentionally small and serializable. They do not replace the
existing MCP tool payloads yet; tools can expose backward-compatible dicts while
also writing these records to the candidate ledger.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


GateStatus = Literal["passed", "failed", "skipped", "error"]
StageStatus = Literal["pending", "running", "passed", "failed", "skipped", "error"]
ArtifactKind = Literal["log", "json", "jsonl", "diff", "replay", "report", "workspace", "other"]
EventSource = Literal["runtime", "test", "backfill"]


class ArtifactRef(BaseModel):
    """A durable artifact emitted by a stage or gate."""

    kind: ArtifactKind = "other"
    path: str
    label: str = ""
    sha256: str = ""
    size_bytes: int | None = None
    hidden: bool = False


class PipelineEnvelope(BaseModel):
    """Typed message envelope for agent, tool, and gate handoffs."""

    message_type: str
    stage: str = ""
    candidate_id: str = ""
    run_id: str = ""
    source: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)


class StageContract(BaseModel):
    """Code-level contract for one pipeline stage."""

    name: str
    blocking: bool = True
    retryable: bool = False
    idempotency_key_fields: list[str] = Field(default_factory=lambda: ["candidate_id", "stage"])
    required_inputs: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    hard_gates: list[str] = Field(default_factory=list)
    soft_gates: list[str] = Field(default_factory=list)


class StageRunRecord(BaseModel):
    """One execution attempt for a pipeline stage."""

    candidate_id: str
    stage: str
    status: StageStatus
    run_id: str = ""
    attempt: int = 0
    profile_id: str = "default"
    prompt_profile_id: str = ""
    model_id: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    gates: list["GateResult"] = Field(default_factory=list)
    failure_class: str = ""
    failures: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    duration_sec: float = 0.0


class GateResult(BaseModel):
    """One executable validation stage."""

    name: str
    status: GateStatus
    blocking: bool = True
    metrics: dict[str, Any] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    replay_cmd: str = ""
    hidden: bool = False
    duration_sec: float = 0.0
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @classmethod
    def from_bool(
        cls,
        name: str,
        passed: bool,
        *,
        blocking: bool = True,
        metrics: dict[str, Any] | None = None,
        failures: list[str] | None = None,
        artifacts: dict[str, Any] | None = None,
        replay_cmd: str = "",
        hidden: bool = False,
        started_at: float | None = None,
    ) -> "GateResult":
        now = time.time()
        started = now if started_at is None else started_at
        return cls(
            name=name,
            status="passed" if passed else "failed",
            blocking=blocking,
            metrics=metrics or {},
            failures=failures or [],
            artifacts=artifacts or {},
            replay_cmd=replay_cmd,
            hidden=hidden,
            started_at=started,
            finished_at=now,
            duration_sec=max(0.0, now - started),
        )


class ScoreCard(BaseModel):
    """Aggregate verdict for a pipeline phase."""

    name: str
    gates: list[GateResult] = Field(default_factory=list)
    primary_score: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(g.passed or not g.blocking for g in self.gates)

    @property
    def failed_gates(self) -> list[str]:
        return [g.name for g in self.gates if not g.passed and g.blocking]

    def add(self, gate: GateResult) -> GateResult:
        self.gates.append(gate)
        return gate


class NationalAcceptanceResult(BaseModel):
    """National TCP in-process acceptance summary for one candidate."""

    candidate: str
    opponents: list[str] = Field(default_factory=list)
    hands_per_pair: int
    passed: bool
    outcome: Literal["passed", "candidate_failure", "infrastructure_failure"] = "passed"
    failure_side: str = ""
    issues: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    matrix: dict[str, Any] = Field(default_factory=dict)
    report: dict[str, Any] = Field(default_factory=dict)


class CandidateRecord(BaseModel):
    """One append-only candidate ledger event."""

    candidate_id: str
    event_type: str
    event_source: EventSource = "runtime"
    version: int | None = None
    source_v: int | None = None
    profile_id: str = "default"
    workflow_profile_id: str = ""
    prompt_profile_id: str = ""
    model_id: str = ""
    run_id: str = ""
    stage_attempt: int = 0
    stage: str = ""
    parent_ids: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    skill_layers: list[str] = Field(default_factory=list)
    diff_hash: str = ""
    gate: str = ""
    scorecard: dict[str, Any] = Field(default_factory=dict)
    gate_results: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    failure_class: str = ""
    artifacts: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)


class WorkflowProfile(BaseModel):
    """Versioned evolution workflow profile.

    Profiles are deliberately conservative in v1: they configure budgets and
    hardening level, not arbitrary generated workflow code.
    """

    profile_id: str
    description: str = ""
    max_workers: int = Field(default=3, ge=1, le=3)
    evaluation_protocol: Literal["national"] = "national"
    rating_protocol: Literal["national"] = "national"
    national_acceptance_hands: int = Field(default=20, ge=1, le=70)
    national_acceptance_hard: bool = True
    national_acceptance_timeout_sec: int = Field(default=300, ge=30, le=3600)
    national_precommit_hands: int = Field(default=70, ge=1, le=70)
    national_precommit_matches: int = Field(default=1, ge=1, le=8)
    national_rating_hands: int = Field(default=70, ge=1, le=70)
    national_rating_matches: int = Field(default=1, ge=1, le=8)
    national_execution_mode: Literal["native_tcp"] = "native_tcp"
    eval_wait_min_games: int = Field(default=100, ge=1, le=1000)
    eval_wait_rd_threshold: float = Field(default=90.0, ge=1, le=350)
    eval_wait_rd_min_games: int = Field(default=30, ge=1, le=1000)
    hidden_scenarios_enabled: bool = False
    allowed_path_prefixes: list[str] = Field(default_factory=lambda: ["bots/"])
    focus_skill_layers: list[str] = Field(default_factory=list)
