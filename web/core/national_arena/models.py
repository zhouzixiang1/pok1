"""Persistent domain models for national Web Arena sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


ARENA_SCHEMA_VERSION = 3
ARENA_MODES = frozenset({"external_tcp", "managed_bots"})
ARENA_RESULT_AUTHORITY = "diagnostic_only"
ARENA_COMPLIANCE_ORACLE = "official_windows_exe"
ACTIVE_ARENA_STATES = frozenset({
    "starting",
    "listening",
    "waiting_for_players",
    "ready",
    "running",
    "stopping",
    "finalizing",
    "quarantined",
})
TERMINAL_ARENA_STATES = frozenset({"finished", "failed", "stopped"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ArenaSession:
    session_id: str
    mode: str
    status: str = "created"
    host: str = "127.0.0.1"
    port: int = 10001
    requested_port: int | None = None
    hands_total: int = 70
    hands_completed: int = 0
    action_timeout_seconds: float = 60.0
    official_action_delay: float = 0.30
    capacity_wait_seconds: float = 30.0
    top_bot: str | None = None
    bottom_bot: str | None = None
    top_player_name: str | None = None
    bottom_player_name: str | None = None
    connected_players: int = 0
    top_total_earnings: int = 0
    bottom_total_earnings: int = 0
    winner: str | None = None
    illegal_actions: list[int] = field(default_factory=lambda: [0, 0])
    timeouts: list[int] = field(default_factory=lambda: [0, 0])
    last_event_id: int = 0
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    failure_reason: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    managed_processes: list[dict[str, Any]] = field(default_factory=list)
    managed_endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    managed_bot_identities: dict[str, dict[str, Any]] = field(default_factory=dict)
    official_certification: dict[str, Any] = field(default_factory=dict)
    schema_version: int = ARENA_SCHEMA_VERSION
    result_authority: str = ARENA_RESULT_AUTHORITY
    affects_glicko: bool = False
    official_exe_certification: bool = False
    compliance_oracle: str = ARENA_COMPLIANCE_ORACLE
    wire_log_complete: bool = True
    cleanup_completed: bool = False
    resource_fence_held: bool = False
    quarantine_reason: str | None = None
    sandbox_profile: str | None = None
    # Arena artifacts are diagnostic-only, but they are still mutable runtime
    # data.  Bind every session to the strict epoch root that authorized its
    # creation so retired/mismatched sessions can never be recovered into a
    # later policy epoch.
    evaluation_epoch: str = ""
    epoch_authority_identity: str = ""
    epoch_reset_receipt_digest: str | None = None
    epoch_authority_state: str = ""
    workflow_run_id: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ARENA_MODES:
            raise ValueError(f"unsupported arena mode: {self.mode}")
        # These are product-authority invariants, not persisted mutable state.
        # Arena output can diagnose a bot but only the official Windows EXE can
        # certify it or make it eligible for the evolution system.
        self.result_authority = ARENA_RESULT_AUTHORITY
        self.affects_glicko = False
        self.official_exe_certification = False
        self.compliance_oracle = ARENA_COMPLIANCE_ORACLE
        if self.requested_port is None:
            self.requested_port = int(self.port)
        if self.status == "quarantined":
            self.cleanup_completed = False
            self.resource_fence_held = True

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_ARENA_STATES

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_ARENA_STATES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ArenaSession":
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in payload.items() if key in fields})
