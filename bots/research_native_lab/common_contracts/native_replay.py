"""Strict verification of captured ``run_native_tcp_pair`` JSON evidence.

Only :func:`verify_native_replay` can construct :class:`VerifiedNativeReplay`.
The token is intentionally a capability object rather than another caller
supplied ``passed=True`` field.  It verifies the complete captured event list;
the convenience ``settlements`` and ``per_player`` summaries are merely
cross-checks.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from .actions import Action, ActionKind
from .cards import tcp_card_to_int
from .constants import HANDS_PER_MATCH, INITIAL_CHIPS
from .deal_generator import (
    DealWindowCommitment,
    canonical_deck_digest,
    generate_tcp_deck,
    tcp_card_from_id,
)
from .national_state import (
    IllegalActionError,
    NationalGameState,
    StateInvariantError,
)
from .native_wire import (
    WireEvidenceError,
    is_structured_wire_capture,
    verify_decision_enforcement_events,
    verify_structured_wire_capture,
)


MAX_NATIVE_REPLAY_BYTES = 16 * 1024 * 1024
MAX_NATIVE_REPLAY_EVENTS = 100_000
NATIVE_REPLAY_VERIFIER_ID = "pok-native-tcp-json-replay-verifier-v1"
NATIVE_REPLAY_VERIFIER_SPEC = (
    "strict-utf8-json;size<=16777216;duplicate-keys=reject;nan-inf=reject;"
    "mode=native_tcp;wrapper=false;hands_requested=70;events=complete;"
    "blinds=alternating;cards=frozen-full-deck-prefix;settlements=zero-sum;"
    "summaries=match_end+top-level+per-player;clean-compliance=true+empty;"
    "clean-native-process=return0+failure0+jsonstdout0;"
    "actions=exact-national-state-replay+pot+stacks+bets+monotonic-cell-deadlines;"
    "clock=monotonic-ns-primary+epoch-ms-correlation;"
    "telemetry=decision-index+latency-ns+search-nodes+fallback+snapshot-tier;"
    "telemetry-authority=trusted-worker-trace-or-explicit-unavailable;"
    "settlements=recomputed-terminal-utility+winner;"
    "client-order=mandatory-exact-ordered-execution-binding;"
    "execution-binding=leg+identity+run+process+cgroup+resource+raw-wire+"
    "external-supervisor-postrun-receipt+socket-session+replay+"
    "durable-attempt-sequence+consumption-ledger;"
    "wire-evidence=v3-ingress-records+token-spans+global-decision-causality+"
    "signed-decision-event-v3-open-nullable-token-close-binding+"
    "explicit-null-tokenless-fault;"
    "result-finalization=match-end-derived;"
    "partial-fault=explicit-nonfinal+owner-consistent+action-adjudication-exact;"
    "capability=instance-sealed+evidence-bound"
)
NATIVE_REPLAY_VERIFIER_DIGEST = hashlib.sha256(
    NATIVE_REPLAY_VERIFIER_SPEC.encode("ascii")
).hexdigest()

_TOKEN = object()
_BINDING_TOKEN = object()
_CONSUMED_FORMAL_SUPERVISOR_BINDINGS: set[str] = set()
_CARD_PATTERN = re.compile(r"<([0-3]),([0-9]|1[0-2])>")
_STAGES = ("preflop", "flop", "turn", "river")
_DEALT_STAGE_SLICES = {
    "flop": (4, 7),
    "turn": (7, 8),
    "river": (8, 9),
}
_EVENT_TYPES = {
    "client_order",
    "hand_start",
    "cards_dealt",
    "action_requested",
    "stage",
    "action",
    "settle",
    "match_end",
}
_SNAPSHOT_TIERS = {
    "safe-fallback",
    "first-search",
    "mid-budget",
    "stable-refinement",
    "deep-refinement",
    "timeout-no-snapshot",
    "fault-no-action",
    "telemetry-unavailable",
}
_TELEMETRY_SOURCES = {
    "trusted_worker_trace",
    "harness_arrival_only",
}


class ReplayVerificationError(ValueError):
    """Captured JSON is not a self-consistent native replay."""


class PartialFaultKind(str, Enum):
    CRASH = "crash"
    TIMEOUT = "timeout"
    ILLEGAL_ACTION = "illegal_action"
    RESOURCE_OVERRUN = "resource_overrun"
    PROTOCOL = "protocol"
    INFRASTRUCTURE = "infrastructure"


_TOKENLESS_ACTION_FAULTS = {
    "timeout": PartialFaultKind.TIMEOUT,
    "fault:crash": PartialFaultKind.CRASH,
    "fault:resource_overrun": PartialFaultKind.RESOURCE_OVERRUN,
    "fault:protocol": PartialFaultKind.PROTOCOL,
    "fault:infrastructure": PartialFaultKind.INFRASTRUCTURE,
}
_TOKENLESS_OUTCOMES = {
    action: kind.value for action, kind in _TOKENLESS_ACTION_FAULTS.items()
}


@dataclass(frozen=True, slots=True)
class PartialReplayFault:
    """Explicit external claim that permits verification of a replay prefix."""

    kind: PartialFaultKind
    owner_connection: int | None
    evidence_digest: str
    hand_number: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PartialFaultKind):
            raise ValueError("partial replay fault kind is invalid")
        if self.kind is PartialFaultKind.INFRASTRUCTURE:
            if self.owner_connection is not None:
                raise ValueError("infrastructure faults cannot name a connection")
        elif type(self.owner_connection) is not int or self.owner_connection not in (0, 1):
            raise ValueError("candidate faults must name connection 0 or 1")
        if self.hand_number is not None:
            _strict_int(
                self.hand_number,
                "partial fault hand",
                minimum=1,
                maximum=HANDS_PER_MATCH,
            )
        object.__setattr__(
            self,
            "evidence_digest",
            _require_digest(self.evidence_digest, "partial fault evidence"),
        )


@dataclass(frozen=True, slots=True, init=False)
class ReplayExecutionBinding:
    """Opaque binding between one replay capture and one concrete TCP leg.

    Formal bindings can only be issued after both execution receipts have
    passed their resource-enforcer capability checks.  The development issuer
    exists for deterministic verifier fixtures, is explicitly marked
    diagnostic-only, and can never satisfy formal strength verification.
    """

    leg_plan_digest: str
    connection_identity_digests: tuple[str, str]
    run_ids_by_connection: tuple[str, str]
    process_tree_ids_by_connection: tuple[str, str]
    cgroup_paths_by_connection: tuple[str, str]
    resource_profile_digest: str
    decision_budget_ms: int
    platform_action_timeout_ms: int
    action_send_delay_ms: int
    raw_wire_digest: str
    authority: str
    supervisor_contract_digest: str | None
    supervisor_readiness_attestation_digest: str | None
    supervisor_launch_authorization_digest: str | None
    supervisor_leg_receipt_digest: str | None
    supervisor_attempt_journal_scope_digest: str | None
    supervisor_attempt_sequence: int | None
    supervisor_previous_attempt_entry_digest: str | None
    supervisor_leg_run_id: str | None
    supervisor_receipt_consumption_key: str | None
    supervisor_consumption_ledger_entry_digest: str | None
    supervisor_consumption_ledger_entry_inode: int | None
    supervisor_consumption_ledger_entry_path: str | None
    supervisor_control_session_digest: str | None
    supervisor_capture_session_digest: str | None
    supervisor_socket_identity_digests: tuple[str, str] | None
    supervisor_wire_semantic_digest: str | None
    supervisor_replay_digest: str | None
    supervisor_decision_trace_digest: str | None
    supervisor_fault_event_digest: str | None
    supervisor_termination_kinds: tuple[str, str] | None
    supervisor_cleanup_receipt_digest: str | None
    binding_digest: str
    _capability: Any = field(repr=False, compare=False, hash=False)

    def __init__(
        self,
        *,
        _token: object,
        leg_plan_digest: str,
        connection_identity_digests: tuple[str, str],
        run_ids_by_connection: tuple[str, str],
        process_tree_ids_by_connection: tuple[str, str],
        cgroup_paths_by_connection: tuple[str, str],
        resource_profile_digest: str,
        decision_budget_ms: int,
        platform_action_timeout_ms: int,
        action_send_delay_ms: int,
        raw_wire_digest: str,
        authority: str,
        supervisor_contract_digest: str | None = None,
        supervisor_readiness_attestation_digest: str | None = None,
        supervisor_launch_authorization_digest: str | None = None,
        supervisor_leg_receipt_digest: str | None = None,
        supervisor_attempt_journal_scope_digest: str | None = None,
        supervisor_attempt_sequence: int | None = None,
        supervisor_previous_attempt_entry_digest: str | None = None,
        supervisor_leg_run_id: str | None = None,
        supervisor_receipt_consumption_key: str | None = None,
        supervisor_consumption_ledger_entry_digest: str | None = None,
        supervisor_consumption_ledger_entry_inode: int | None = None,
        supervisor_consumption_ledger_entry_path: str | None = None,
        supervisor_control_session_digest: str | None = None,
        supervisor_capture_session_digest: str | None = None,
        supervisor_socket_identity_digests: tuple[str, str] | None = None,
        supervisor_wire_semantic_digest: str | None = None,
        supervisor_replay_digest: str | None = None,
        supervisor_decision_trace_digest: str | None = None,
        supervisor_fault_event_digest: str | None = None,
        supervisor_termination_kinds: tuple[str, str] | None = None,
        supervisor_cleanup_receipt_digest: str | None = None,
    ) -> None:
        if _token is not _BINDING_TOKEN:
            raise TypeError("ReplayExecutionBinding is issued only by a binding factory")
        if authority not in {"development_diagnostic_only", "formal_enforcer_bound"}:
            raise ValueError("unknown replay execution binding authority")
        identities = _digest_pair(
            connection_identity_digests,
            "connection identity",
        )
        runs = _digest_pair(run_ids_by_connection, "connection run ID")
        processes = _nonempty_string_pair(
            process_tree_ids_by_connection,
            "connection process tree",
        )
        cgroups = _nonempty_string_pair(
            cgroup_paths_by_connection,
            "connection cgroup",
        )
        if any(not path.startswith("/sys/fs/cgroup/") for path in cgroups):
            raise ValueError("replay binding cgroups must be absolute cgroup-v2 paths")
        if len(set(identities)) != 2 or len(set(runs)) != 2:
            raise ValueError("replay binding requires two distinct identities and runs")
        if len(set(processes)) != 2 or len(set(cgroups)) != 2:
            raise ValueError("replay binding requires isolated process trees and cgroups")
        values = {
            "leg_plan_digest": _require_digest(leg_plan_digest, "leg plan"),
            "connection_identity_digests": identities,
            "run_ids_by_connection": runs,
            "process_tree_ids_by_connection": processes,
            "cgroup_paths_by_connection": cgroups,
            "resource_profile_digest": _require_digest(
                resource_profile_digest,
                "resource profile",
            ),
            "decision_budget_ms": _binding_int(
                decision_budget_ms,
                "decision budget",
                minimum=1,
                maximum=54_000,
            ),
            "platform_action_timeout_ms": _binding_int(
                platform_action_timeout_ms,
                "platform action timeout",
                minimum=60_000,
                maximum=60_000,
            ),
            "action_send_delay_ms": _binding_int(
                action_send_delay_ms,
                "action send delay",
                minimum=0,
                maximum=59_999,
            ),
            "raw_wire_digest": _require_digest(raw_wire_digest, "raw wire"),
            "authority": authority,
        }
        if values["decision_budget_ms"] + values["action_send_delay_ms"] >= 60_000:
            raise ValueError("compute budget plus send delay must precede platform timeout")
        supervisor_arguments: dict[str, Any] = {
            "supervisor_contract_digest": supervisor_contract_digest,
            "supervisor_readiness_attestation_digest": supervisor_readiness_attestation_digest,
            "supervisor_launch_authorization_digest": supervisor_launch_authorization_digest,
            "supervisor_leg_receipt_digest": supervisor_leg_receipt_digest,
            "supervisor_attempt_journal_scope_digest": supervisor_attempt_journal_scope_digest,
            "supervisor_attempt_sequence": supervisor_attempt_sequence,
            "supervisor_previous_attempt_entry_digest": supervisor_previous_attempt_entry_digest,
            "supervisor_leg_run_id": supervisor_leg_run_id,
            "supervisor_receipt_consumption_key": supervisor_receipt_consumption_key,
            "supervisor_consumption_ledger_entry_digest": supervisor_consumption_ledger_entry_digest,
            "supervisor_consumption_ledger_entry_inode": supervisor_consumption_ledger_entry_inode,
            "supervisor_consumption_ledger_entry_path": supervisor_consumption_ledger_entry_path,
            "supervisor_control_session_digest": supervisor_control_session_digest,
            "supervisor_capture_session_digest": supervisor_capture_session_digest,
            "supervisor_socket_identity_digests": supervisor_socket_identity_digests,
            "supervisor_wire_semantic_digest": supervisor_wire_semantic_digest,
            "supervisor_replay_digest": supervisor_replay_digest,
            "supervisor_decision_trace_digest": supervisor_decision_trace_digest,
            "supervisor_fault_event_digest": supervisor_fault_event_digest,
            "supervisor_termination_kinds": supervisor_termination_kinds,
            "supervisor_cleanup_receipt_digest": supervisor_cleanup_receipt_digest,
        }
        if authority == "development_diagnostic_only":
            if any(value is not None for value in supervisor_arguments.values()):
                raise ValueError(
                    "development replay binding cannot carry formal supervisor claims"
                )
            normalized_supervisor = supervisor_arguments
        else:
            missing = sorted(
                name for name, value in supervisor_arguments.items() if value is None
            )
            if missing:
                raise ValueError(
                    "formal replay binding lacks signed post-run supervisor fields: "
                    + ", ".join(missing)
                )
            normalized_supervisor = {
                name: _require_digest(value, name.replace("_", " "))
                for name, value in supervisor_arguments.items()
                if name
                not in {
                    "supervisor_socket_identity_digests",
                    "supervisor_termination_kinds",
                    "supervisor_attempt_sequence",
                    "supervisor_consumption_ledger_entry_inode",
                    "supervisor_consumption_ledger_entry_path",
                }
            }
            normalized_supervisor["supervisor_socket_identity_digests"] = _digest_pair(
                supervisor_socket_identity_digests,  # type: ignore[arg-type]
                "supervisor socket identity",
            )
            kinds = tuple(supervisor_termination_kinds or ())
            allowed_kinds = {
                "normal",
                "crash",
                "timeout",
                "resource",
                "protocol",
                "infrastructure",
            }
            if len(kinds) != 2 or any(kind not in allowed_kinds for kind in kinds):
                raise ValueError("formal replay binding has invalid termination kinds")
            normalized_supervisor["supervisor_termination_kinds"] = kinds
            normalized_supervisor["supervisor_attempt_sequence"] = _binding_int(
                supervisor_attempt_sequence,  # type: ignore[arg-type]
                "supervisor attempt sequence",
                minimum=1,
                maximum=(1 << 63) - 1,
            )
            normalized_supervisor[
                "supervisor_consumption_ledger_entry_inode"
            ] = _binding_int(
                supervisor_consumption_ledger_entry_inode,  # type: ignore[arg-type]
                "supervisor consumption ledger inode",
                minimum=1,
                maximum=(1 << 63) - 1,
            )
            ledger_path = supervisor_consumption_ledger_entry_path
            if (
                not isinstance(ledger_path, str)
                or not ledger_path.startswith("/")
                or "\x00" in ledger_path
            ):
                raise ValueError(
                    "formal replay binding has invalid consumption ledger path"
                )
            normalized_supervisor[
                "supervisor_consumption_ledger_entry_path"
            ] = ledger_path
        sealed_payload: dict[str, Any] = {
            "schema": "pok-native-replay-execution-binding-v1",
            **values,
        }
        if authority == "formal_enforcer_bound":
            sealed_payload = {
                "schema": "pok-native-replay-execution-binding-v2",
                "capture_binding": sealed_payload,
                "postrun_supervisor_binding": normalized_supervisor,
            }
        binding_digest = _digest_payload(sealed_payload)
        for name, value in values.items():
            object.__setattr__(self, name, value)
        for name, value in normalized_supervisor.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "binding_digest", binding_digest)

        def issued_instance(
            candidate: object,
            owner: object = self,
            sealed: str = binding_digest,
        ) -> bool:
            return (
                candidate is owner
                and isinstance(candidate, ReplayExecutionBinding)
                and candidate.binding_digest == sealed
                and candidate._unchecked_digest() == sealed
            )

        object.__setattr__(self, "_capability", issued_instance)

    @classmethod
    def for_development(
        cls,
        *,
        raw_wire: bytes,
        **kwargs: Any,
    ) -> "ReplayExecutionBinding":
        """Issue a sealed, explicitly non-formal binding for verifier tests."""

        return cls(
            _token=_BINDING_TOKEN,
            raw_wire_digest=_raw_bytes_digest(raw_wire, "raw wire"),
            authority="development_diagnostic_only",
            **kwargs,
        )

    @classmethod
    def from_formal_execution_receipts(
        cls,
        *,
        leg_plan: object,
        execution_receipts: Sequence[object],
        resource_receipts: Sequence[object],
        resource_profile: object,
        raw_wire: bytes,
    ) -> "ReplayExecutionBinding":
        """Deprecated insecure issuer retained only to fail closed.

        Execution/resource receipts do not bind the socket capture, raw replay,
        or post-run result.  Accepting them alone permits evidence grafting.
        Formal callers must use :meth:`from_authorized_supervisor_leg`.
        """

        raise ValueError(
            "formal replay binding must directly consume an externally signed "
            "AuthorizedSupervisorLeg; execution/resource receipts alone are insufficient"
        )

    @classmethod
    def from_authorized_supervisor_leg(
        cls,
        *,
        leg_plan: object,
        execution_receipts: Sequence[object],
        resource_receipts: Sequence[object],
        resource_profile: object,
        authorized_supervisor_leg: object,
        raw_wire: bytes,
        raw_replay: bytes,
    ) -> "ReplayExecutionBinding":
        """Bind one post-run signed supervisor leg to its exact wire/replay.

        The supervisor capability must already have emitted the two formal
        execution/resource receipt pairs.  This verifier independently hashes
        both byte streams, derives wire semantics again, checks the exact
        connection evidence, and consumes the signed receipt key once.
        """

        from .evaluation import (
            ExecutionReceipt,
            LegPlan,
            ResourceProfile,
            ResourceReceipt,
        )
        from .resource_enforcer import AuthorizedSupervisorLeg

        if not isinstance(leg_plan, LegPlan):
            raise ValueError("formal replay binding requires a typed LegPlan")
        if not isinstance(resource_profile, ResourceProfile):
            raise ValueError("formal replay binding requires a typed ResourceProfile")
        if not isinstance(authorized_supervisor_leg, AuthorizedSupervisorLeg):
            raise ValueError(
                "formal replay binding requires an AuthorizedSupervisorLeg"
            )
        authorized_supervisor_leg._assert_authorized()
        supervisor = authorized_supervisor_leg.replay_binding_payload()
        consumption_key = _require_digest(
            supervisor["receipt_consumption_key"],
            "supervisor receipt consumption key",
        )
        if consumption_key in _CONSUMED_FORMAL_SUPERVISOR_BINDINGS:
            raise ValueError("supervisor leg was already consumed by replay binding")
        raw_wire_digest = _raw_bytes_digest(raw_wire, "raw wire")
        raw_replay_digest = _raw_bytes_digest(raw_replay, "raw replay")
        if raw_wire_digest != supervisor["raw_wire_digest"]:
            raise ValueError("signed supervisor receipt belongs to different raw wire")
        if raw_replay_digest != supervisor["replay_digest"]:
            raise ValueError("signed supervisor receipt belongs to different raw replay")
        replay_payload, _canonical = _load_strict_json(raw_replay)
        if not is_structured_wire_capture(raw_wire):
            raise ValueError("formal supervisor replay requires structured v3 wire capture")
        try:
            wire_verification = verify_structured_wire_capture(
                raw_wire,
                replay_payload,
            )
        except WireEvidenceError as exc:
            raise ValueError(
                "signed supervisor wire does not derive its replay"
            ) from exc
        if (
            wire_verification.semantic_binding_digest
            != supervisor["wire_semantic_digest"]
        ):
            raise ValueError(
                "signed supervisor wire semantic digest differs from independent verification"
            )
        try:
            decision_binding = verify_decision_enforcement_events(
                raw_wire,
                replay_payload,
                supervisor["supervisor_decision_events"],
            )
        except (KeyError, TypeError, WireEvidenceError) as exc:
            raise ValueError(
                "signed supervisor decision trace is not derived from exact wire records"
            ) from exc
        if (
            decision_binding.capture_session_digest
            != supervisor["capture_session_digest"]
        ):
            raise ValueError(
                "signed supervisor decision trace belongs to another capture session"
            )
        receipts = tuple(execution_receipts)
        if len(receipts) != 2 or any(
            not isinstance(item, ExecutionReceipt) for item in receipts
        ):
            raise ValueError("formal replay binding requires two execution receipts")
        if tuple(item.connection_index for item in receipts) != (0, 1):
            raise ValueError("execution receipts must be ordered by connection")
        resources = tuple(resource_receipts)
        if len(resources) != 2 or any(
            not isinstance(item, ResourceReceipt) for item in resources
        ):
            raise ValueError("formal replay binding requires two resource receipts")
        if tuple(item.connection_index for item in resources) != (0, 1):
            raise ValueError("resource receipts must be ordered by connection")
        for index, receipt in enumerate(receipts):
            receipt._assert_formal_enforcer_authority()
            if receipt.leg_plan_digest != leg_plan.digest():
                raise ValueError("execution receipt belongs to another leg")
            if receipt.identity_digest != leg_plan.connection_to_identity[index]:
                raise ValueError("execution receipt changed the planned connection identity")
            resource = resources[index]
            resource._assert_formal_enforcer_authority()
            resource.verify(execution=receipt, profile=resource_profile)
            evidence = authorized_supervisor_leg.connections[index]
            if (
                receipt.raw_evidence_digest
                != evidence.execution_raw_evidence_digest()
                or resource.raw_evidence_digest
                != evidence.resource_raw_evidence_digest()
                or receipt.run_id != evidence.run_id
                or receipt.process_tree_id != f"pgid:{evidence.process_group_id}"
                or receipt.cgroup_path != evidence.cgroup_path
                or resource.cgroup_inode != evidence.cgroup_inode
            ):
                raise ValueError(
                    "formal execution/resource receipt differs from signed supervisor connection"
                )
        binding = cls(
            _token=_BINDING_TOKEN,
            leg_plan_digest=leg_plan.digest(),
            connection_identity_digests=leg_plan.connection_to_identity,
            run_ids_by_connection=tuple(item.run_id for item in receipts),
            process_tree_ids_by_connection=tuple(
                item.process_tree_id for item in receipts
            ),
            cgroup_paths_by_connection=tuple(item.cgroup_path for item in receipts),
            resource_profile_digest=resource_profile.digest(),
            decision_budget_ms=resource_profile.decision_budget_ms,
            platform_action_timeout_ms=resource_profile.platform_action_timeout_ms,
            action_send_delay_ms=resource_profile.action_send_delay_ms,
            raw_wire_digest=raw_wire_digest,
            authority="formal_enforcer_bound",
            supervisor_contract_digest=supervisor["supervisor_contract_digest"],
            supervisor_readiness_attestation_digest=supervisor[
                "readiness_attestation_digest"
            ],
            supervisor_launch_authorization_digest=supervisor[
                "launch_authorization_digest"
            ],
            supervisor_leg_receipt_digest=supervisor[
                "supervisor_leg_receipt_digest"
            ],
            supervisor_attempt_journal_scope_digest=supervisor[
                "attempt_journal_scope_digest"
            ],
            supervisor_attempt_sequence=supervisor["attempt_sequence"],
            supervisor_previous_attempt_entry_digest=supervisor[
                "previous_attempt_entry_digest"
            ],
            supervisor_leg_run_id=supervisor["leg_run_id"],
            supervisor_receipt_consumption_key=consumption_key,
            supervisor_consumption_ledger_entry_digest=supervisor[
                "consumption_ledger_entry_digest"
            ],
            supervisor_consumption_ledger_entry_inode=supervisor[
                "consumption_ledger_entry_inode"
            ],
            supervisor_consumption_ledger_entry_path=supervisor[
                "consumption_ledger_entry_path"
            ],
            supervisor_control_session_digest=supervisor["control_session_digest"],
            supervisor_capture_session_digest=supervisor["capture_session_digest"],
            supervisor_socket_identity_digests=tuple(
                supervisor["socket_identity_digests"]
            ),
            supervisor_wire_semantic_digest=supervisor["wire_semantic_digest"],
            supervisor_replay_digest=supervisor["replay_digest"],
            supervisor_decision_trace_digest=supervisor["decision_trace_digest"],
            supervisor_fault_event_digest=supervisor[
                "supervisor_fault_event_digest"
            ],
            supervisor_termination_kinds=tuple(supervisor["termination_kinds"]),
            supervisor_cleanup_receipt_digest=supervisor[
                "cleanup_receipt_digest"
            ],
        )
        captured_binding = _mapping(
            replay_payload.get("execution_binding"),
            "execution_binding",
        )
        if captured_binding != binding.capture_payload():
            raise ValueError(
                "raw replay captured a different execution binding than the signed leg"
            )
        _CONSUMED_FORMAL_SUPERVISOR_BINDINGS.add(consumption_key)
        return binding

    def _capture_payload(self) -> dict[str, Any]:
        return {
            "schema": "pok-native-replay-execution-binding-v1",
            "leg_plan_digest": self.leg_plan_digest,
            "connection_identity_digests": list(self.connection_identity_digests),
            "run_ids_by_connection": list(self.run_ids_by_connection),
            "process_tree_ids_by_connection": list(
                self.process_tree_ids_by_connection
            ),
            "cgroup_paths_by_connection": list(self.cgroup_paths_by_connection),
            "resource_profile_digest": self.resource_profile_digest,
            "decision_budget_ms": self.decision_budget_ms,
            "platform_action_timeout_ms": self.platform_action_timeout_ms,
            "action_send_delay_ms": self.action_send_delay_ms,
            "raw_wire_digest": self.raw_wire_digest,
            "authority": self.authority,
        }

    def _unchecked_digest(self) -> str:
        capture = self._capture_payload()
        if self.authority == "development_diagnostic_only":
            return _digest_payload(capture)
        return _digest_payload(
            {
                "schema": "pok-native-replay-execution-binding-v2",
                "capture_binding": capture,
                "postrun_supervisor_binding": {
                    "supervisor_contract_digest": self.supervisor_contract_digest,
                    "supervisor_readiness_attestation_digest": self.supervisor_readiness_attestation_digest,
                    "supervisor_launch_authorization_digest": self.supervisor_launch_authorization_digest,
                    "supervisor_leg_receipt_digest": self.supervisor_leg_receipt_digest,
                    "supervisor_attempt_journal_scope_digest": self.supervisor_attempt_journal_scope_digest,
                    "supervisor_attempt_sequence": self.supervisor_attempt_sequence,
                    "supervisor_previous_attempt_entry_digest": self.supervisor_previous_attempt_entry_digest,
                    "supervisor_leg_run_id": self.supervisor_leg_run_id,
                    "supervisor_receipt_consumption_key": self.supervisor_receipt_consumption_key,
                    "supervisor_consumption_ledger_entry_digest": self.supervisor_consumption_ledger_entry_digest,
                    "supervisor_consumption_ledger_entry_inode": self.supervisor_consumption_ledger_entry_inode,
                    "supervisor_consumption_ledger_entry_path": self.supervisor_consumption_ledger_entry_path,
                    "supervisor_control_session_digest": self.supervisor_control_session_digest,
                    "supervisor_capture_session_digest": self.supervisor_capture_session_digest,
                    "supervisor_socket_identity_digests": self.supervisor_socket_identity_digests,
                    "supervisor_wire_semantic_digest": self.supervisor_wire_semantic_digest,
                    "supervisor_replay_digest": self.supervisor_replay_digest,
                    "supervisor_decision_trace_digest": self.supervisor_decision_trace_digest,
                    "supervisor_fault_event_digest": self.supervisor_fault_event_digest,
                    "supervisor_termination_kinds": self.supervisor_termination_kinds,
                    "supervisor_cleanup_receipt_digest": self.supervisor_cleanup_receipt_digest,
                },
            }
        )

    def _assert_bound(self, *, formal_required: bool = False) -> None:
        guard = self._capability
        if not callable(guard) or guard(self) is not True:
            raise TypeError("replay execution binding was copied, forged, or altered")
        if formal_required and self.authority != "formal_enforcer_bound":
            raise TypeError("formal replay requires an enforcer-bound execution binding")

    def capture_payload(self) -> dict[str, Any]:
        """Return the exact public fields the trusted harness must capture."""

        self._assert_bound()
        return self._capture_payload()

    def connection_binding_digests(self) -> tuple[str, str]:
        self._assert_bound()
        return tuple(
            _digest_payload(
                {
                    "schema": "pok-native-replay-connection-binding-v1",
                    "connection_index": index,
                    "identity_digest": self.connection_identity_digests[index],
                    "run_id": self.run_ids_by_connection[index],
                    "process_tree_id": self.process_tree_ids_by_connection[index],
                    "cgroup_path": self.cgroup_paths_by_connection[index],
                }
            )
            for index in range(2)
        )  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class FormalReplayExecutionContext:
    """Atomic result of consuming one signed supervisor leg for replay.

    Receipt emission and replay binding intentionally happen in one helper so
    production callers cannot accidentally retain a receipts-only formal path.
    The embedded objects keep their own instance/content capabilities.
    """

    execution_binding: ReplayExecutionBinding
    execution_receipts: tuple[Any, Any]
    resource_receipts: tuple[Any, Any]


def bind_authorized_supervisor_replay(
    *,
    leg_plan: object,
    resource_profile: object,
    authorized_supervisor_leg: object,
    raw_wire: bytes,
    raw_replay: bytes,
) -> FormalReplayExecutionContext:
    """Consume one signed post-run leg and return its inseparable context."""

    from .resource_enforcer import AuthorizedSupervisorLeg

    if not isinstance(authorized_supervisor_leg, AuthorizedSupervisorLeg):
        raise ValueError("formal replay context requires AuthorizedSupervisorLeg")
    pairs = authorized_supervisor_leg.formal_receipts()
    executions = tuple(pair[0] for pair in pairs)
    resources = tuple(pair[1] for pair in pairs)
    binding = ReplayExecutionBinding.from_authorized_supervisor_leg(
        leg_plan=leg_plan,
        execution_receipts=executions,
        resource_receipts=resources,
        resource_profile=resource_profile,
        authorized_supervisor_leg=authorized_supervisor_leg,
        raw_wire=raw_wire,
        raw_replay=raw_replay,
    )
    return FormalReplayExecutionContext(
        execution_binding=binding,
        execution_receipts=executions,  # type: ignore[arg-type]
        resource_receipts=resources,  # type: ignore[arg-type]
    )


@dataclass(frozen=True, slots=True, init=False)
class VerifiedNativeReplay:
    """Opaque verifier output; direct construction requires a private token."""

    verifier_id: str
    verifier_digest: str
    execution_binding_digest: str
    execution_binding_authority: str
    leg_plan_digest: str
    connection_identity_digests: tuple[str, str]
    run_ids_by_connection: tuple[str, str]
    process_tree_ids_by_connection: tuple[str, str]
    cgroup_paths_by_connection: tuple[str, str]
    resource_profile_digest: str
    raw_wire_digest: str
    supervisor_contract_digest: str | None
    supervisor_readiness_attestation_digest: str | None
    supervisor_launch_authorization_digest: str | None
    supervisor_leg_receipt_digest: str | None
    supervisor_attempt_journal_scope_digest: str | None
    supervisor_attempt_sequence: int | None
    supervisor_previous_attempt_entry_digest: str | None
    supervisor_leg_run_id: str | None
    supervisor_receipt_consumption_key: str | None
    supervisor_consumption_ledger_entry_digest: str | None
    supervisor_consumption_ledger_entry_inode: int | None
    supervisor_consumption_ledger_entry_path: str | None
    supervisor_control_session_digest: str | None
    supervisor_capture_session_digest: str | None
    supervisor_socket_identity_digests: tuple[str, str] | None
    supervisor_wire_semantic_digest: str | None
    supervisor_replay_digest: str | None
    supervisor_decision_trace_digest: str | None
    supervisor_fault_event_digest: str | None
    supervisor_termination_kinds: tuple[str, str] | None
    supervisor_cleanup_receipt_digest: str | None
    wire_semantics_verified: bool
    wire_semantic_binding_digest: str
    decision_budget_ms: int
    platform_action_timeout_ms: int
    action_send_delay_ms: int
    raw_digest: str
    canonical_digest: str
    verification_evidence_digest: str
    full_deck_digests: tuple[str, ...]
    event_digests: tuple[str, ...]
    hands_started: int
    hands_played: int
    settlement_count: int
    net_chips_by_connection: tuple[int, int]
    timeout_count_by_connection: tuple[int, int]
    illegal_action_count_by_connection: tuple[int, int]
    decision_wait_ns_by_connection: tuple[tuple[int, ...], tuple[int, ...]]
    search_nodes_by_connection: tuple[int, int]
    fallback_decisions_by_connection: tuple[int, int]
    decision_trace_digest_by_connection: tuple[str, str]
    telemetry_complete_by_connection: tuple[bool, bool]
    hand70_evidence_digest: str | None
    result_finalized_epoch_ms: int | None
    partial_fault: PartialReplayFault | None
    _capability: Any = field(repr=False, compare=False, hash=False)

    def __init__(
        self,
        *,
        _token: object,
        execution_binding: ReplayExecutionBinding,
        raw_digest: str,
        canonical_digest: str,
        verification_evidence_digest: str,
        full_deck_digests: tuple[str, ...],
        event_digests: tuple[str, ...],
        hands_started: int,
        hands_played: int,
        settlement_count: int,
        net_chips_by_connection: tuple[int, int],
        timeout_count_by_connection: tuple[int, int],
        illegal_action_count_by_connection: tuple[int, int],
        decision_wait_ns_by_connection: tuple[tuple[int, ...], tuple[int, ...]],
        search_nodes_by_connection: tuple[int, int],
        fallback_decisions_by_connection: tuple[int, int],
        decision_trace_digest_by_connection: tuple[str, str],
        telemetry_complete_by_connection: tuple[bool, bool],
        wire_semantics_verified: bool,
        wire_semantic_binding_digest: str,
        hand70_evidence_digest: str | None,
        result_finalized_epoch_ms: int | None,
        partial_fault: PartialReplayFault | None,
    ) -> None:
        if _token is not _TOKEN:
            raise TypeError(
                "VerifiedNativeReplay is issued only by verify_native_replay"
            )
        values = {
            "verifier_id": NATIVE_REPLAY_VERIFIER_ID,
            "verifier_digest": NATIVE_REPLAY_VERIFIER_DIGEST,
            "execution_binding_digest": execution_binding.binding_digest,
            "execution_binding_authority": execution_binding.authority,
            "leg_plan_digest": execution_binding.leg_plan_digest,
            "connection_identity_digests": (
                execution_binding.connection_identity_digests
            ),
            "run_ids_by_connection": execution_binding.run_ids_by_connection,
            "process_tree_ids_by_connection": (
                execution_binding.process_tree_ids_by_connection
            ),
            "cgroup_paths_by_connection": (
                execution_binding.cgroup_paths_by_connection
            ),
            "resource_profile_digest": execution_binding.resource_profile_digest,
            "raw_wire_digest": execution_binding.raw_wire_digest,
            "supervisor_contract_digest": execution_binding.supervisor_contract_digest,
            "supervisor_readiness_attestation_digest": execution_binding.supervisor_readiness_attestation_digest,
            "supervisor_launch_authorization_digest": execution_binding.supervisor_launch_authorization_digest,
            "supervisor_leg_receipt_digest": execution_binding.supervisor_leg_receipt_digest,
            "supervisor_attempt_journal_scope_digest": execution_binding.supervisor_attempt_journal_scope_digest,
            "supervisor_attempt_sequence": execution_binding.supervisor_attempt_sequence,
            "supervisor_previous_attempt_entry_digest": execution_binding.supervisor_previous_attempt_entry_digest,
            "supervisor_leg_run_id": execution_binding.supervisor_leg_run_id,
            "supervisor_receipt_consumption_key": execution_binding.supervisor_receipt_consumption_key,
            "supervisor_consumption_ledger_entry_digest": execution_binding.supervisor_consumption_ledger_entry_digest,
            "supervisor_consumption_ledger_entry_inode": execution_binding.supervisor_consumption_ledger_entry_inode,
            "supervisor_consumption_ledger_entry_path": execution_binding.supervisor_consumption_ledger_entry_path,
            "supervisor_control_session_digest": execution_binding.supervisor_control_session_digest,
            "supervisor_capture_session_digest": execution_binding.supervisor_capture_session_digest,
            "supervisor_socket_identity_digests": execution_binding.supervisor_socket_identity_digests,
            "supervisor_wire_semantic_digest": execution_binding.supervisor_wire_semantic_digest,
            "supervisor_replay_digest": execution_binding.supervisor_replay_digest,
            "supervisor_decision_trace_digest": execution_binding.supervisor_decision_trace_digest,
            "supervisor_fault_event_digest": execution_binding.supervisor_fault_event_digest,
            "supervisor_termination_kinds": execution_binding.supervisor_termination_kinds,
            "supervisor_cleanup_receipt_digest": execution_binding.supervisor_cleanup_receipt_digest,
            "wire_semantics_verified": wire_semantics_verified,
            "wire_semantic_binding_digest": _require_digest(
                wire_semantic_binding_digest,
                "wire semantic binding",
            ),
            "decision_budget_ms": execution_binding.decision_budget_ms,
            "platform_action_timeout_ms": (
                execution_binding.platform_action_timeout_ms
            ),
            "action_send_delay_ms": execution_binding.action_send_delay_ms,
            "raw_digest": raw_digest,
            "canonical_digest": canonical_digest,
            "verification_evidence_digest": verification_evidence_digest,
            "full_deck_digests": full_deck_digests,
            "event_digests": event_digests,
            "hands_started": hands_started,
            "hands_played": hands_played,
            "settlement_count": settlement_count,
            "net_chips_by_connection": net_chips_by_connection,
            "timeout_count_by_connection": timeout_count_by_connection,
            "illegal_action_count_by_connection": illegal_action_count_by_connection,
            "decision_wait_ns_by_connection": decision_wait_ns_by_connection,
            "search_nodes_by_connection": search_nodes_by_connection,
            "fallback_decisions_by_connection": fallback_decisions_by_connection,
            "decision_trace_digest_by_connection": (
                decision_trace_digest_by_connection
            ),
            "telemetry_complete_by_connection": telemetry_complete_by_connection,
            "hand70_evidence_digest": hand70_evidence_digest,
            "result_finalized_epoch_ms": result_finalized_epoch_ms,
            "partial_fault": partial_fault,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)

        # The closure binds this exact issued instance.  A shallow/deep copy
        # carries a guard that still points at the original and therefore
        # cannot act as a second capability.
        def issued_instance(
            candidate: object,
            owner: object = self,
            sealed_evidence: str = verification_evidence_digest,
        ) -> bool:
            return (
                candidate is owner
                and getattr(candidate, "verification_evidence_digest", None)
                == sealed_evidence
            )

        object.__setattr__(self, "_capability", issued_instance)

    def _assert_verified(self) -> None:
        """Assert this exact instance is an untampered verifier capability."""

        guard = self._capability
        if not callable(guard) or guard(self) is not True:
            raise TypeError("native replay capability was copied or forged")
        if (
            self.verifier_id != NATIVE_REPLAY_VERIFIER_ID
            or self.verifier_digest != NATIVE_REPLAY_VERIFIER_DIGEST
        ):
            raise TypeError("native replay verifier identity was altered")
        try:
            expected = _digest_payload(
                _verification_evidence_payload(
                    execution_binding_digest=self.execution_binding_digest,
                    execution_binding_authority=self.execution_binding_authority,
                    leg_plan_digest=self.leg_plan_digest,
                    connection_identity_digests=self.connection_identity_digests,
                    run_ids_by_connection=self.run_ids_by_connection,
                    process_tree_ids_by_connection=(
                        self.process_tree_ids_by_connection
                    ),
                    cgroup_paths_by_connection=self.cgroup_paths_by_connection,
                    resource_profile_digest=self.resource_profile_digest,
                    raw_wire_digest=self.raw_wire_digest,
                    supervisor_contract_digest=self.supervisor_contract_digest,
                    supervisor_readiness_attestation_digest=self.supervisor_readiness_attestation_digest,
                    supervisor_launch_authorization_digest=self.supervisor_launch_authorization_digest,
                    supervisor_leg_receipt_digest=self.supervisor_leg_receipt_digest,
                    supervisor_attempt_journal_scope_digest=self.supervisor_attempt_journal_scope_digest,
                    supervisor_attempt_sequence=self.supervisor_attempt_sequence,
                    supervisor_previous_attempt_entry_digest=self.supervisor_previous_attempt_entry_digest,
                    supervisor_leg_run_id=self.supervisor_leg_run_id,
                    supervisor_receipt_consumption_key=self.supervisor_receipt_consumption_key,
                    supervisor_consumption_ledger_entry_digest=self.supervisor_consumption_ledger_entry_digest,
                    supervisor_consumption_ledger_entry_inode=self.supervisor_consumption_ledger_entry_inode,
                    supervisor_consumption_ledger_entry_path=self.supervisor_consumption_ledger_entry_path,
                    supervisor_control_session_digest=self.supervisor_control_session_digest,
                    supervisor_capture_session_digest=self.supervisor_capture_session_digest,
                    supervisor_socket_identity_digests=self.supervisor_socket_identity_digests,
                    supervisor_wire_semantic_digest=self.supervisor_wire_semantic_digest,
                    supervisor_replay_digest=self.supervisor_replay_digest,
                    supervisor_decision_trace_digest=self.supervisor_decision_trace_digest,
                    supervisor_fault_event_digest=self.supervisor_fault_event_digest,
                    supervisor_termination_kinds=self.supervisor_termination_kinds,
                    supervisor_cleanup_receipt_digest=self.supervisor_cleanup_receipt_digest,
                    wire_semantics_verified=self.wire_semantics_verified,
                    wire_semantic_binding_digest=(
                        self.wire_semantic_binding_digest
                    ),
                    decision_budget_ms=self.decision_budget_ms,
                    platform_action_timeout_ms=self.platform_action_timeout_ms,
                    action_send_delay_ms=self.action_send_delay_ms,
                    raw_digest=self.raw_digest,
                    canonical_digest=self.canonical_digest,
                    full_deck_digests=self.full_deck_digests,
                    event_digests=self.event_digests,
                    hands_started=self.hands_started,
                    hands_played=self.hands_played,
                    settlement_count=self.settlement_count,
                    net_chips_by_connection=self.net_chips_by_connection,
                    timeout_count_by_connection=self.timeout_count_by_connection,
                    illegal_action_count_by_connection=(
                        self.illegal_action_count_by_connection
                    ),
                    decision_wait_ns_by_connection=(
                        self.decision_wait_ns_by_connection
                    ),
                    search_nodes_by_connection=self.search_nodes_by_connection,
                    fallback_decisions_by_connection=(
                        self.fallback_decisions_by_connection
                    ),
                    decision_trace_digest_by_connection=(
                        self.decision_trace_digest_by_connection
                    ),
                    telemetry_complete_by_connection=(
                        self.telemetry_complete_by_connection
                    ),
                    hand70_evidence_digest=self.hand70_evidence_digest,
                    result_finalized_epoch_ms=self.result_finalized_epoch_ms,
                    partial_fault=self.partial_fault,
                )
            )
        except Exception as exc:
            raise TypeError("native replay capability fields are invalid") from exc
        if expected != self.verification_evidence_digest:
            raise TypeError("native replay capability evidence was altered")

    @property
    def clean_complete(self) -> bool:
        return self.partial_fault is None and self.settlement_count == HANDS_PER_MATCH

    @property
    def raw_replay_digest(self) -> str:
        return self.raw_digest

    @property
    def canonical_replay_digest(self) -> str:
        return self.canonical_digest

    @property
    def match_trace_digest(self) -> str:
        return self.canonical_digest

    @property
    def actual_dealt_prefix_digests(self) -> tuple[str, ...]:
        return self.full_deck_digests

    @property
    def verified_event_digests(self) -> tuple[str, ...]:
        return self.event_digests

    @property
    def net_chips_connection0(self) -> int:
        return self.net_chips_by_connection[0]


@dataclass(slots=True)
class _PendingAction:
    player_idx: int
    stage: str
    requested_epoch_ms: int
    compute_deadline_epoch_ms: int
    platform_deadline_epoch_ms: int
    requested_monotonic_ns: int
    compute_deadline_monotonic_ns: int
    platform_deadline_monotonic_ns: int


@dataclass(slots=True)
class _HandReplay:
    hand_number: int
    sb_idx: int
    bb_idx: int
    expected_deck: tuple[int, ...]
    full_deck_digest: str
    event_indices: list[int]
    hole_cards_by_connection: tuple[tuple[int, int], tuple[int, int]] | None = None
    board: tuple[int, ...] = ()
    current_stage: str = "preflop"
    next_dealt_stage: int = 0
    pending_action: _PendingAction | None = None
    settlement: dict[str, Any] | None = None
    state: NationalGameState | None = None


def _strict_int(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReplayVerificationError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    return _strict_int(value, name, minimum=0, maximum=(1 << 63) - 1)


def _finite_number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayVerificationError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ReplayVerificationError(
            f"{name} must be finite and in [{minimum}, {maximum}]"
        )
    return number


def _require_digest(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a hexadecimal SHA-256 digest")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a hexadecimal SHA-256 digest") from exc
    if len(decoded) != 32:
        raise ValueError(f"{name} must be a 32-byte SHA-256 digest")
    return value.lower()


def _raw_bytes_digest(value: Any, name: str) -> str:
    if type(value) is not bytes or not value:
        raise ValueError(f"{name} must be non-empty raw bytes")
    return hashlib.sha256(value).hexdigest()


def _binding_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _nonempty_string_pair(value: Any, name: str) -> tuple[str, str]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f"{name} must contain exactly two strings")
    pair = tuple(value)
    if any(not isinstance(item, str) or not item for item in pair):
        raise ValueError(f"{name} must contain exactly two non-empty strings")
    return pair  # type: ignore[return-value]


def _digest_pair(value: Any, name: str) -> tuple[str, str]:
    pair = _nonempty_string_pair(value, name)
    return tuple(
        _require_digest(item, f"{name} {index}")
        for index, item in enumerate(pair)
    )  # type: ignore[return-value]


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReplayVerificationError(f"{name} must be a JSON object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReplayVerificationError(f"{name} must be a JSON array")
    return value


def _pair_ints(value: Any, name: str, *, signed: bool = True) -> tuple[int, int]:
    items = _list(value, name)
    if len(items) != 2:
        raise ReplayVerificationError(f"{name} must contain exactly two integers")
    minimum = -(HANDS_PER_MATCH * INITIAL_CHIPS) if signed else 0
    maximum = HANDS_PER_MATCH * INITIAL_CHIPS
    return (
        _strict_int(items[0], f"{name}[0]", minimum=minimum, maximum=maximum),
        _strict_int(items[1], f"{name}[1]", minimum=minimum, maximum=maximum),
    )


def _pair_strings(value: Any, name: str) -> tuple[str, str]:
    items = _list(value, name)
    if len(items) != 2 or any(not isinstance(item, str) or not item for item in items):
        raise ReplayVerificationError(f"{name} must contain two non-empty strings")
    return items[0], items[1]


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    items = _list(value, name)
    if any(not isinstance(item, str) or not item for item in items):
        raise ReplayVerificationError(
            f"{name} must contain only non-empty strings"
        )
    return tuple(items)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _verification_evidence_payload(
    *,
    execution_binding_digest: str,
    execution_binding_authority: str,
    leg_plan_digest: str,
    connection_identity_digests: Sequence[str],
    run_ids_by_connection: Sequence[str],
    process_tree_ids_by_connection: Sequence[str],
    cgroup_paths_by_connection: Sequence[str],
    resource_profile_digest: str,
    raw_wire_digest: str,
    supervisor_contract_digest: str | None,
    supervisor_readiness_attestation_digest: str | None,
    supervisor_launch_authorization_digest: str | None,
    supervisor_leg_receipt_digest: str | None,
    supervisor_attempt_journal_scope_digest: str | None,
    supervisor_attempt_sequence: int | None,
    supervisor_previous_attempt_entry_digest: str | None,
    supervisor_leg_run_id: str | None,
    supervisor_receipt_consumption_key: str | None,
    supervisor_consumption_ledger_entry_digest: str | None,
    supervisor_consumption_ledger_entry_inode: int | None,
    supervisor_consumption_ledger_entry_path: str | None,
    supervisor_control_session_digest: str | None,
    supervisor_capture_session_digest: str | None,
    supervisor_socket_identity_digests: Sequence[str] | None,
    supervisor_wire_semantic_digest: str | None,
    supervisor_replay_digest: str | None,
    supervisor_decision_trace_digest: str | None,
    supervisor_fault_event_digest: str | None,
    supervisor_termination_kinds: Sequence[str] | None,
    supervisor_cleanup_receipt_digest: str | None,
    wire_semantics_verified: bool,
    wire_semantic_binding_digest: str,
    decision_budget_ms: int,
    platform_action_timeout_ms: int,
    action_send_delay_ms: int,
    raw_digest: str,
    canonical_digest: str,
    full_deck_digests: Sequence[str],
    event_digests: Sequence[str],
    hands_started: int,
    hands_played: int,
    settlement_count: int,
    net_chips_by_connection: Sequence[int],
    timeout_count_by_connection: Sequence[int],
    illegal_action_count_by_connection: Sequence[int],
    decision_wait_ns_by_connection: Sequence[Sequence[int]],
    search_nodes_by_connection: Sequence[int],
    fallback_decisions_by_connection: Sequence[int],
    decision_trace_digest_by_connection: Sequence[str],
    telemetry_complete_by_connection: Sequence[bool],
    hand70_evidence_digest: str | None,
    result_finalized_epoch_ms: int | None,
    partial_fault: PartialReplayFault | None,
) -> dict[str, Any]:
    return {
        "verifier_id": NATIVE_REPLAY_VERIFIER_ID,
        "verifier_digest": NATIVE_REPLAY_VERIFIER_DIGEST,
        "execution_binding_digest": execution_binding_digest,
        "execution_binding_authority": execution_binding_authority,
        "leg_plan_digest": leg_plan_digest,
        "connection_identity_digests": list(connection_identity_digests),
        "run_ids_by_connection": list(run_ids_by_connection),
        "process_tree_ids_by_connection": list(process_tree_ids_by_connection),
        "cgroup_paths_by_connection": list(cgroup_paths_by_connection),
        "resource_profile_digest": resource_profile_digest,
        "raw_wire_digest": raw_wire_digest,
        "supervisor_contract_digest": supervisor_contract_digest,
        "supervisor_readiness_attestation_digest": supervisor_readiness_attestation_digest,
        "supervisor_launch_authorization_digest": supervisor_launch_authorization_digest,
        "supervisor_leg_receipt_digest": supervisor_leg_receipt_digest,
        "supervisor_attempt_journal_scope_digest": supervisor_attempt_journal_scope_digest,
        "supervisor_attempt_sequence": supervisor_attempt_sequence,
        "supervisor_previous_attempt_entry_digest": supervisor_previous_attempt_entry_digest,
        "supervisor_leg_run_id": supervisor_leg_run_id,
        "supervisor_receipt_consumption_key": supervisor_receipt_consumption_key,
        "supervisor_consumption_ledger_entry_digest": supervisor_consumption_ledger_entry_digest,
        "supervisor_consumption_ledger_entry_inode": supervisor_consumption_ledger_entry_inode,
        "supervisor_consumption_ledger_entry_path": supervisor_consumption_ledger_entry_path,
        "supervisor_control_session_digest": supervisor_control_session_digest,
        "supervisor_capture_session_digest": supervisor_capture_session_digest,
        "supervisor_socket_identity_digests": (
            None
            if supervisor_socket_identity_digests is None
            else list(supervisor_socket_identity_digests)
        ),
        "supervisor_wire_semantic_digest": supervisor_wire_semantic_digest,
        "supervisor_replay_digest": supervisor_replay_digest,
        "supervisor_decision_trace_digest": supervisor_decision_trace_digest,
        "supervisor_fault_event_digest": supervisor_fault_event_digest,
        "supervisor_termination_kinds": (
            None
            if supervisor_termination_kinds is None
            else list(supervisor_termination_kinds)
        ),
        "supervisor_cleanup_receipt_digest": supervisor_cleanup_receipt_digest,
        "wire_semantics_verified": wire_semantics_verified,
        "wire_semantic_binding_digest": wire_semantic_binding_digest,
        "decision_budget_ms": decision_budget_ms,
        "platform_action_timeout_ms": platform_action_timeout_ms,
        "action_send_delay_ms": action_send_delay_ms,
        "raw_digest": raw_digest,
        "canonical_digest": canonical_digest,
        "full_deck_digests": list(full_deck_digests),
        "event_digests": list(event_digests),
        "hands_started": hands_started,
        "hands_played": hands_played,
        "settlement_count": settlement_count,
        "net_chips_by_connection": list(net_chips_by_connection),
        "timeout_count_by_connection": list(timeout_count_by_connection),
        "illegal_action_count_by_connection": list(
            illegal_action_count_by_connection
        ),
        "decision_wait_ns_by_connection": [
            list(values) for values in decision_wait_ns_by_connection
        ],
        "search_nodes_by_connection": list(search_nodes_by_connection),
        "fallback_decisions_by_connection": list(
            fallback_decisions_by_connection
        ),
        "decision_trace_digest_by_connection": list(
            decision_trace_digest_by_connection
        ),
        "telemetry_complete_by_connection": list(
            telemetry_complete_by_connection
        ),
        "hand70_evidence_digest": hand70_evidence_digest,
        "result_finalized_epoch_ms": result_finalized_epoch_ms,
        "partial_fault": (
            None
            if partial_fault is None
            else {
                **asdict(partial_fault),
                "kind": partial_fault.kind.value,
            }
        ),
    }


def _event_digest(index: int, event: Mapping[str, Any]) -> str:
    payload = (
        NATIVE_REPLAY_VERIFIER_ID.encode("ascii")
        + b"\x00event\x00"
        + index.to_bytes(8, "big")
        + _canonical_bytes(event)
    )
    return hashlib.sha256(payload).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayVerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> None:
    raise ReplayVerificationError(f"non-finite JSON number is forbidden: {token}")


def _load_strict_json(raw_json: bytes) -> tuple[dict[str, Any], bytes]:
    if type(raw_json) is not bytes:
        raise ReplayVerificationError("native replay input must be raw JSON bytes")
    if not raw_json or len(raw_json) > MAX_NATIVE_REPLAY_BYTES:
        raise ReplayVerificationError(
            f"native replay size must be in [1, {MAX_NATIVE_REPLAY_BYTES}] bytes"
        )
    try:
        text = raw_json.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReplayVerificationError("native replay is not strict UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except ReplayVerificationError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ReplayVerificationError("native replay is not valid strict JSON") from exc
    payload = _mapping(value, "native replay root")
    try:
        canonical = _canonical_bytes(payload)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ReplayVerificationError("native replay cannot be canonicalized") from exc
    return payload, canonical


def _parse_card(value: Any, name: str) -> int:
    if not isinstance(value, str):
        raise ReplayVerificationError(f"{name} must be a TCP card string")
    match = _CARD_PATTERN.fullmatch(value)
    if match is None:
        raise ReplayVerificationError(f"{name} is not canonical <suit,rank>")
    return tcp_card_to_int(int(match.group(1)), int(match.group(2)))


def _parse_cards(value: Any, count: int, name: str) -> tuple[int, ...]:
    items = _list(value, name)
    if len(items) != count:
        raise ReplayVerificationError(f"{name} must contain exactly {count} cards")
    cards = tuple(_parse_card(item, f"{name}[{index}]") for index, item in enumerate(items))
    if len(set(cards)) != len(cards):
        raise ReplayVerificationError(f"{name} contains duplicate cards")
    return cards


def _normalize_hand_seeds(
    source: Sequence[int] | DealWindowCommitment,
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    seeds = tuple(source.hand_seeds if isinstance(source, DealWindowCommitment) else source)
    if len(seeds) != HANDS_PER_MATCH:
        raise ReplayVerificationError("native replay requires exactly 70 hand seeds")
    decks: list[str] = []
    for index, seed in enumerate(seeds, 1):
        try:
            deck = generate_tcp_deck(seed)
        except ValueError as exc:
            raise ReplayVerificationError(f"invalid seed for hand {index}") from exc
        decks.append(canonical_deck_digest(deck))
    if len(set(seeds)) != HANDS_PER_MATCH:
        raise ReplayVerificationError("native replay hand seeds repeat")
    if len(set(decks)) != HANDS_PER_MATCH:
        raise ReplayVerificationError("native replay full decks repeat")
    deck_digests = tuple(decks)
    if isinstance(source, DealWindowCommitment) and source.deck_digests != deck_digests:
        raise ReplayVerificationError("deal commitment deck digests do not match its seeds")
    return seeds, deck_digests


def _settlement_from_event(event: Mapping[str, Any], hand: int) -> dict[str, Any]:
    earnings = _pair_ints(event.get("earnings"), f"hand {hand} settlement earnings")
    if sum(earnings) != 0:
        raise ReplayVerificationError(f"hand {hand} settlement is not zero-sum")
    winner = event.get("winner_idx")
    if winner is not None:
        winner = _strict_int(
            winner,
            f"hand {hand} winner",
            minimum=0,
            maximum=1,
        )
    is_showdown = event.get("is_showdown")
    if type(is_showdown) is not bool:
        raise ReplayVerificationError(f"hand {hand} showdown marker must be boolean")
    reason = event.get("reason", "")
    if not isinstance(reason, str):
        raise ReplayVerificationError(f"hand {hand} settlement reason must be a string")
    expected_winner = 0 if earnings[0] > 0 else 1 if earnings[0] < 0 else None
    if winner != expected_winner:
        raise ReplayVerificationError(
            f"hand {hand} winner disagrees with zero-sum earnings"
        )
    return {
        "hand": hand,
        "earnings": [earnings[0], earnings[1]],
        "pot": _strict_int(
            event.get("pot"),
            f"hand {hand} settlement pot",
            minimum=0,
            maximum=2 * INITIAL_CHIPS,
        ),
        "is_showdown": is_showdown,
        "winner_idx": winner,
        "reason": reason,
    }


def _require_player_chips(
    event: Mapping[str, Any],
    state: NationalGameState,
    name: str,
) -> None:
    observed = _pair_ints(event.get("player_chips"), f"{name} player_chips", signed=False)
    if observed != state.stacks:
        raise ReplayVerificationError(
            f"{name} player_chips differ from exact betting-state stacks"
        )


def _require_absent(event: Mapping[str, Any], fields: Sequence[str], name: str) -> None:
    present = sorted(field for field in fields if field in event)
    if present:
        raise ReplayVerificationError(
            f"{name} contains fields forbidden for this action: {present}"
        )


def _action_from_event(
    event: Mapping[str, Any],
    state: NationalGameState,
    *,
    hand_number: int,
) -> tuple[Action, str]:
    """Return the semantic engine action and the captured outcome kind.

    Timeout and illegal wire input are both adjudicated as folds by the local
    national engine.  For an ``illegal:...`` event we independently prove that
    the raw wire text is either malformed or illegal in the exact common state;
    a legal action cannot be relabelled as an illegal candidate fault.
    """

    action_text = event.get("action")
    if not isinstance(action_text, str) or not action_text:
        raise ReplayVerificationError("action text must be non-empty")
    tokenless_outcome = _TOKENLESS_OUTCOMES.get(action_text)
    if tokenless_outcome is not None:
        return Action(ActionKind.FOLD), tokenless_outcome
    if action_text.startswith("illegal:"):
        raw = action_text[len("illegal:") :]
        if not raw:
            raise ReplayVerificationError("illegal action lacks raw action text")
        try:
            parsed = Action.from_wire(raw)
        except ValueError:
            parsed = None
        if parsed is not None and state.validate_action(parsed)[0]:
            raise ReplayVerificationError(
                f"hand {hand_number} labels an independently legal action as illegal"
            )
        reason = event.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ReplayVerificationError("illegal action event lacks validator reason")
        return Action(ActionKind.FOLD), "illegal"
    try:
        if action_text == "raise":
            amount = _strict_int(
                event.get("amount"),
                f"hand {hand_number} raise-to amount",
                minimum=1,
                maximum=INITIAL_CHIPS,
            )
            return Action(ActionKind.RAISE, amount), "raise"
        return Action(ActionKind(action_text)), action_text
    except (ValueError, TypeError) as exc:
        raise ReplayVerificationError(f"unknown native action {action_text!r}") from exc


def _apply_and_verify_action_event(
    event: Mapping[str, Any],
    state: NationalGameState,
    *,
    hand_number: int,
    pending: _PendingAction,
    execution_binding: ReplayExecutionBinding,
) -> tuple[NationalGameState, str]:
    decision_wait_sec = _finite_number(
        event.get("decision_wait_sec"),
        f"hand {hand_number} decision wait",
        minimum=0.0,
        maximum=60.001,
    )
    timeout_budget = _finite_number(
        event.get("timeout_budget_sec"),
        f"hand {hand_number} timeout budget",
        minimum=60.0,
        maximum=60.0,
    )
    if timeout_budget != 60.0:  # defensive against future coercion changes
        raise ReplayVerificationError("formal native action timeout is not exactly 60 seconds")

    event_budget = _strict_int(
        event.get("decision_budget_ms"),
        f"hand {hand_number} action decision budget",
        minimum=1,
        maximum=54_000,
    )
    event_platform_timeout = _strict_int(
        event.get("platform_action_timeout_ms"),
        f"hand {hand_number} action platform timeout",
        minimum=60_000,
        maximum=60_000,
    )
    event_send_delay = _strict_int(
        event.get("action_send_delay_ms"),
        f"hand {hand_number} action send delay",
        minimum=0,
        maximum=59_999,
    )
    if (
        event_budget != execution_binding.decision_budget_ms
        or event_platform_timeout != execution_binding.platform_action_timeout_ms
        or event_send_delay != execution_binding.action_send_delay_ms
    ):
        raise ReplayVerificationError("action timing envelope differs from execution binding")

    action_epoch_ms = _strict_int(
        event.get("action_epoch_ms"),
        f"hand {hand_number} action timestamp",
        minimum=pending.requested_epoch_ms,
        maximum=pending.platform_deadline_epoch_ms + 1_000,
    )
    action_monotonic_ns = _strict_int(
        event.get("action_monotonic_ns"),
        f"hand {hand_number} action monotonic timestamp",
        minimum=pending.requested_monotonic_ns,
        maximum=pending.platform_deadline_monotonic_ns + 1_000_000_000,
    )
    elapsed_ns = action_monotonic_ns - pending.requested_monotonic_ns
    reported_wait_ns = _strict_int(
        event.get("decision_wait_ns"),
        f"hand {hand_number} decision wait nanoseconds",
        minimum=0,
        maximum=60_001_000_000,
    )
    elapsed_ms = elapsed_ns // 1_000_000
    reported_wait_ms = _strict_int(
        event.get("decision_wait_ms"),
        f"hand {hand_number} decision wait milliseconds",
        minimum=0,
        maximum=60_001,
    )
    if (
        reported_wait_ns != elapsed_ns
        or reported_wait_ms != elapsed_ms
        or abs(decision_wait_sec - elapsed_ns / 1_000_000_000) > 0.000001
    ):
        raise ReplayVerificationError(
            f"hand {hand_number} decision wait disagrees with monotonic timestamps"
        )

    actor = state.actor
    if actor is None:
        raise ReplayVerificationError("action event occurs without a pending actor")
    semantic, outcome = _action_from_event(
        event,
        state,
        hand_number=hand_number,
    )
    telemetry_source = event.get("telemetry_source")
    if telemetry_source not in _TELEMETRY_SOURCES:
        raise ReplayVerificationError("unknown decision telemetry authority")
    if outcome in set(_TOKENLESS_OUTCOMES.values()):
        _require_absent(
            event,
            (
                "compute_finished_epoch_ms",
                "send_not_before_epoch_ms",
                "compute_finished_monotonic_ns",
                "send_not_before_monotonic_ns",
                "wire_token_sequence",
                "wire_source_record_sequence",
            ),
            f"hand {hand_number} tokenless fault",
        )
        if (
            outcome == PartialFaultKind.TIMEOUT.value
            and action_monotonic_ns < pending.platform_deadline_monotonic_ns
        ):
            raise ReplayVerificationError(
                f"hand {hand_number} timeout occurred before the platform deadline"
            )
        if (
            outcome != PartialFaultKind.TIMEOUT.value
            and action_monotonic_ns >= pending.platform_deadline_monotonic_ns
        ):
            raise ReplayVerificationError(
                f"hand {hand_number} tokenless non-timeout fault reached the "
                "platform deadline; timeout attribution takes precedence"
            )
    else:
        if telemetry_source == "trusted_worker_trace":
            _require_absent(
                event,
                ("compute_finished_epoch_ms", "send_not_before_epoch_ms"),
                f"hand {hand_number} monotonic timing",
            )
            compute_finished_monotonic_ns = _strict_int(
                event.get("compute_finished_monotonic_ns"),
                f"hand {hand_number} compute-finished monotonic timestamp",
                minimum=pending.requested_monotonic_ns,
                maximum=pending.compute_deadline_monotonic_ns,
            )
            send_not_before_monotonic_ns = _strict_int(
                event.get("send_not_before_monotonic_ns"),
                f"hand {hand_number} send-not-before monotonic timestamp",
                minimum=pending.requested_monotonic_ns,
                maximum=pending.platform_deadline_monotonic_ns,
            )
            if send_not_before_monotonic_ns != (
                compute_finished_monotonic_ns
                + execution_binding.action_send_delay_ms * 1_000_000
            ):
                raise ReplayVerificationError(
                    f"hand {hand_number} send throttle differs from the frozen delay"
                )
            if not send_not_before_monotonic_ns <= action_monotonic_ns <= (
                pending.compute_deadline_monotonic_ns
                + execution_binding.action_send_delay_ms * 1_000_000
            ):
                raise ReplayVerificationError(
                    f"hand {hand_number} action missed its frozen cell budget"
                )
        else:
            _require_absent(
                event,
                (
                    "compute_finished_epoch_ms",
                    "send_not_before_epoch_ms",
                    "compute_finished_monotonic_ns",
                    "send_not_before_monotonic_ns",
                ),
                f"hand {hand_number} arrival-only telemetry",
            )
            if execution_binding.action_send_delay_ms != 0:
                raise ReplayVerificationError(
                    "arrival-only capture cannot attest a positive send delay"
                )
            if action_monotonic_ns > pending.compute_deadline_monotonic_ns:
                raise ReplayVerificationError(
                    f"hand {hand_number} action missed its frozen cell budget"
                )
    before = state
    try:
        after = before.apply_action(semantic)
    except (IllegalActionError, StateInvariantError) as exc:
        raise ReplayVerificationError(
            f"hand {hand_number} action violates the exact national state"
        ) from exc

    _require_player_chips(event, after, f"hand {hand_number} action")
    contribution = (
        after.total_contributions[actor] - before.total_contributions[actor]
    )
    if outcome == "raise":
        needed = _strict_int(
            event.get("needed"),
            f"hand {hand_number} raise contribution",
            minimum=1,
            maximum=INITIAL_CHIPS,
        )
        if needed != contribution or event.get("pot") != after.pot:
            raise ReplayVerificationError(
                f"hand {hand_number} raise amount/pot differ from exact transition"
            )
    elif outcome == "call":
        amount = _strict_int(
            event.get("amount"),
            f"hand {hand_number} call contribution",
            minimum=0,
            maximum=INITIAL_CHIPS,
        )
        if amount != contribution or event.get("pot") != after.pot:
            raise ReplayVerificationError(
                f"hand {hand_number} call amount/pot differ from exact transition"
            )
        _require_absent(event, ("needed",), f"hand {hand_number} call")
    elif outcome == "allin":
        amount = _strict_int(
            event.get("amount"),
            f"hand {hand_number} allin contribution",
            minimum=1,
            maximum=INITIAL_CHIPS,
        )
        if amount != contribution or event.get("pot") != after.pot:
            raise ReplayVerificationError(
                f"hand {hand_number} allin amount/pot differ from exact transition"
            )
        _require_absent(event, ("needed",), f"hand {hand_number} allin")
    else:
        _require_absent(
            event,
            ("amount", "needed", "pot"),
            f"hand {hand_number} {outcome}",
        )
        if contribution != 0:
            raise ReplayVerificationError(
                f"hand {hand_number} non-chip action changed contribution"
            )
    return after, outcome


def _validate_partial_fault(
    fault: PartialReplayFault,
    *,
    hands_started: int,
    settlement_count: int,
    match_end_hands: int | None,
    observed_faults: set[tuple[PartialFaultKind, int | None, int]],
) -> None:
    if settlement_count == HANDS_PER_MATCH or match_end_hands == HANDS_PER_MATCH:
        raise ReplayVerificationError("a complete result cannot have a post-hoc fault")
    if fault.hand_number is not None:
        if hands_started == 0 or fault.hand_number != hands_started:
            raise ReplayVerificationError(
                "a partial fault must refer to the final started hand"
            )
    if fault.kind in (PartialFaultKind.TIMEOUT, PartialFaultKind.ILLEGAL_ACTION):
        if fault.hand_number is None or fault.owner_connection is None:
            raise ReplayVerificationError("action faults require hand and connection")
    if observed_faults or fault.kind in (
        PartialFaultKind.TIMEOUT,
        PartialFaultKind.ILLEGAL_ACTION,
    ):
        if fault.hand_number is None:
            raise ReplayVerificationError(
                "captured action fault requires a final hand number"
            )
        key = (fault.kind, fault.owner_connection, fault.hand_number)
        if observed_faults != {key}:
            raise ReplayVerificationError(
                "claimed action fault differs from captured fault adjudication"
            )


def verify_native_replay(
    raw_json: bytes,
    hand_seeds: Sequence[int] | DealWindowCommitment,
    *,
    execution_binding: ReplayExecutionBinding,
    raw_wire: bytes,
    partial_fault: PartialReplayFault | None = None,
) -> VerifiedNativeReplay:
    """Verify one capture against frozen deals and its concrete execution leg."""

    if not isinstance(execution_binding, ReplayExecutionBinding):
        raise ReplayVerificationError(
            "native replay requires a typed execution binding"
        )
    try:
        execution_binding._assert_bound()
    except (TypeError, ValueError) as exc:
        raise ReplayVerificationError("native replay execution binding is invalid") from exc
    try:
        captured_wire_digest = _raw_bytes_digest(raw_wire, "raw wire")
    except ValueError as exc:
        raise ReplayVerificationError("native replay requires non-empty raw wire bytes") from exc
    if captured_wire_digest != execution_binding.raw_wire_digest:
        raise ReplayVerificationError(
            "raw wire bytes differ from the sealed execution binding"
        )
    if partial_fault is not None and not isinstance(partial_fault, PartialReplayFault):
        raise ReplayVerificationError("partial_fault must be a PartialReplayFault")
    if execution_binding.authority == "formal_enforcer_bound":
        termination_kinds = execution_binding.supervisor_termination_kinds
        if termination_kinds is None:
            raise ReplayVerificationError(
                "formal replay lacks signed supervisor termination facts"
            )
        if partial_fault is None:
            if termination_kinds != ("normal", "normal"):
                raise ReplayVerificationError(
                    "clean replay contradicts signed supervisor termination facts"
                )
        elif partial_fault.kind is PartialFaultKind.INFRASTRUCTURE:
            if "infrastructure" not in termination_kinds:
                raise ReplayVerificationError(
                    "infrastructure fault is absent from signed supervisor facts"
                )
        elif partial_fault.owner_connection is not None:
            expected_termination = {
                PartialFaultKind.CRASH: "crash",
                PartialFaultKind.TIMEOUT: "timeout",
                PartialFaultKind.RESOURCE_OVERRUN: "resource",
                PartialFaultKind.PROTOCOL: "protocol",
            }.get(partial_fault.kind)
            if (
                expected_termination is not None
                and termination_kinds[partial_fault.owner_connection]
                != expected_termination
            ):
                raise ReplayVerificationError(
                    "candidate fault contradicts signed supervisor termination facts"
                )
    payload, canonical = _load_strict_json(raw_json)
    if is_structured_wire_capture(raw_wire):
        try:
            wire_verification = verify_structured_wire_capture(raw_wire, payload)
        except WireEvidenceError as exc:
            raise ReplayVerificationError(
                f"raw wire evidence does not derive the replay: {exc}"
            ) from exc
        wire_semantics_verified = True
        wire_semantic_binding_digest = (
            wire_verification.semantic_binding_digest
        )
    else:
        if execution_binding.authority == "formal_enforcer_bound":
            raise ReplayVerificationError(
                "formal native replay requires structured v3 raw-wire evidence"
            )
        wire_semantics_verified = False
        wire_semantic_binding_digest = _digest_payload(
            {
                "schema": "pok-native-opaque-wire-development-marker-v1",
                "raw_wire_digest": captured_wire_digest,
            }
        )
    seeds, planned_deck_digests = _normalize_hand_seeds(hand_seeds)

    captured_binding = _mapping(
        payload.get("execution_binding"),
        "execution_binding",
    )
    if captured_binding != execution_binding.capture_payload():
        raise ReplayVerificationError(
            "captured execution binding differs from the sealed leg/process/wire binding"
        )

    if payload.get("execution_mode") != "native_tcp":
        raise ReplayVerificationError("replay execution_mode must be native_tcp")
    if payload.get("wrapper_used") is not False:
        raise ReplayVerificationError("native replay cannot use a wrapper")
    _strict_int(
        payload.get("hands_requested"),
        "hands_requested",
        minimum=HANDS_PER_MATCH,
        maximum=HANDS_PER_MATCH,
    )
    bot_a = payload.get("bot_a")
    bot_b = payload.get("bot_b")
    if (
        not isinstance(bot_a, str)
        or not bot_a
        or not isinstance(bot_b, str)
        or not bot_b
        or bot_a == bot_b
    ):
        raise ReplayVerificationError("native replay requires two distinct bot labels")

    wrapper_by_player = _mapping(
        payload.get("wrapper_used_by_player"),
        "wrapper_used_by_player",
    )
    if set(wrapper_by_player) != {bot_a, bot_b} or any(
        value is not False for value in wrapper_by_player.values()
    ):
        raise ReplayVerificationError("per-player wrapper markers must both be false")

    events = _list(payload.get("events"), "events")
    if len(events) > MAX_NATIVE_REPLAY_EVENTS:
        raise ReplayVerificationError("native replay event count exceeds the frozen limit")
    event_digests: list[str] = []
    for index, event in enumerate(events):
        row = _mapping(event, f"event {index}")
        event_digests.append(_event_digest(index, row))

    hands: list[_HandReplay] = []
    active: _HandReplay | None = None
    normalized_settlements: list[dict[str, Any]] = []
    timeout_counts = [0, 0]
    illegal_counts = [0, 0]
    decision_wait_ns: list[list[int]] = [[], []]
    search_nodes = [0, 0]
    fallback_decisions = [0, 0]
    telemetry_complete = [True, True]
    decision_event_indices: list[list[int]] = [[], []]
    observed_faults: set[tuple[PartialFaultKind, int | None, int]] = set()
    engine_names: tuple[str, str] | None = None
    match_end_hands: int | None = None
    match_end_total: tuple[int, int] | None = None
    match_end_event_index: int | None = None
    result_finalized_epoch_ms: int | None = None
    client_order_seen = False
    last_timing_epoch_ms = 0
    last_timing_monotonic_ns = 0

    for index, raw_event in enumerate(events):
        event = _mapping(raw_event, f"event {index}")
        event_type = event.get("type")
        if event_type not in _EVENT_TYPES:
            raise ReplayVerificationError(f"event {index} has unknown type {event_type!r}")
        if match_end_event_index is not None:
            raise ReplayVerificationError("events occur after match_end")

        if event_type == "client_order":
            if client_order_seen or hands or index != 0:
                raise ReplayVerificationError(
                    "client_order must occur exactly once as the first event"
                )
            requested_order = _pair_strings(event.get("order"), "client_order.order")
            connection_order = _pair_strings(
                event.get("connection_order"),
                "client_order.connection_order",
            )
            identities = _digest_pair(
                event.get("connection_identity_digests"),
                "client_order connection identities",
            )
            runs = _digest_pair(
                event.get("run_ids_by_connection"),
                "client_order run IDs",
            )
            processes = _nonempty_string_pair(
                event.get("process_tree_ids_by_connection"),
                "client_order process trees",
            )
            cgroups = _nonempty_string_pair(
                event.get("cgroup_paths_by_connection"),
                "client_order cgroups",
            )
            connection_bindings = _digest_pair(
                event.get("connection_binding_digests"),
                "client_order connection bindings",
            )
            if (
                requested_order != (bot_a, bot_b)
                or connection_order != (bot_a, bot_b)
                or identities != execution_binding.connection_identity_digests
                or runs != execution_binding.run_ids_by_connection
                or processes != execution_binding.process_tree_ids_by_connection
                or cgroups != execution_binding.cgroup_paths_by_connection
                or connection_bindings
                != execution_binding.connection_binding_digests()
            ):
                raise ReplayVerificationError(
                    "client_order differs from the exact frozen connection mapping"
                )
            client_order_seen = True
            continue

        if event_type == "match_end":
            match_end_event_index = index
            match_end_hands = _strict_int(
                event.get("hands_played"),
                "match_end hands_played",
                minimum=0,
                maximum=HANDS_PER_MATCH,
            )
            match_end_total = _pair_ints(
                event.get("total_earnings"),
                "match_end total_earnings",
            )
            names = _pair_strings(event.get("names"), "match_end names")
            if engine_names is not None and names != engine_names:
                raise ReplayVerificationError("match_end player names changed")
            if match_end_hands == HANDS_PER_MATCH:
                result_finalized_epoch_ms = _strict_int(
                    event.get("result_finalized_epoch_ms"),
                    "match_end result finalization timestamp",
                    minimum=max(1, last_timing_epoch_ms),
                    maximum=(1 << 63) - 1,
                )
            else:
                _require_absent(
                    event,
                    ("result_finalized_epoch_ms",),
                    "partial match_end",
                )
            continue

        hand_number = _strict_int(
            event.get("hand"),
            f"event {index} hand",
            minimum=1,
            maximum=HANDS_PER_MATCH,
        )

        if event_type == "hand_start":
            if active is not None:
                raise ReplayVerificationError("a new hand starts before prior settlement")
            expected_hand = len(hands) + 1
            if hand_number != expected_hand:
                raise ReplayVerificationError("hand_start numbers are not contiguous")
            sb_idx = _strict_int(
                event.get("sb_idx"),
                f"hand {hand_number} sb_idx",
                minimum=0,
                maximum=1,
            )
            bb_idx = _strict_int(
                event.get("bb_idx"),
                f"hand {hand_number} bb_idx",
                minimum=0,
                maximum=1,
            )
            expected_sb = (hand_number - 1) % 2
            if (sb_idx, bb_idx) != (expected_sb, 1 - expected_sb):
                raise ReplayVerificationError(
                    f"hand {hand_number} does not alternate small/big blinds"
                )
            names = _pair_strings(event.get("names"), f"hand {hand_number} names")
            if engine_names is None:
                engine_names = names
            elif names != engine_names:
                raise ReplayVerificationError("player names changed between hands")
            chips = _pair_ints(
                event.get("player_chips"),
                f"hand {hand_number} starting chips",
                signed=False,
            )
            expected_chips = [INITIAL_CHIPS, INITIAL_CHIPS]
            expected_chips[sb_idx] -= 50
            expected_chips[bb_idx] -= 100
            if chips != tuple(expected_chips):
                raise ReplayVerificationError(
                    f"hand {hand_number} blind chip snapshot is inconsistent"
                )
            if event.get("pot") != 150:
                raise ReplayVerificationError(f"hand {hand_number} starting pot is not 150")
            expected_deck = tuple(
                tcp_card_to_int(*tcp_card_from_id(card_id))
                for card_id in generate_tcp_deck(seeds[hand_number - 1])
            )
            active = _HandReplay(
                hand_number=hand_number,
                sb_idx=sb_idx,
                bb_idx=bb_idx,
                expected_deck=expected_deck,
                full_deck_digest=planned_deck_digests[hand_number - 1],
                event_indices=[index],
            )
            hands.append(active)
            continue

        if active is None or hand_number != active.hand_number:
            raise ReplayVerificationError(
                f"event {index} is not attached to the active hand"
            )
        active.event_indices.append(index)

        if event_type == "cards_dealt":
            if active.hole_cards_by_connection is not None:
                raise ReplayVerificationError("duplicate cards_dealt event")
            rows = _list(event.get("hole_cards"), "cards_dealt.hole_cards")
            if len(rows) != 2:
                raise ReplayVerificationError("hole_cards must contain two players")
            actual = (
                _parse_cards(rows[0], 2, "connection 0 hole cards"),
                _parse_cards(rows[1], 2, "connection 1 hole cards"),
            )
            if len(set(actual[0] + actual[1])) != 4:
                raise ReplayVerificationError("hole cards repeat within one hand")
            expected: list[tuple[int, int] | None] = [None, None]
            expected[active.sb_idx] = tuple(active.expected_deck[0:2])
            expected[active.bb_idx] = tuple(active.expected_deck[2:4])
            if actual != (expected[0], expected[1]):
                raise ReplayVerificationError(
                    f"hand {hand_number} hole cards differ from frozen deck"
                )
            active.hole_cards_by_connection = actual
            try:
                active.state = NationalGameState.new_hand(
                    hand_number,
                    small_blind=active.sb_idx,
                    hole_cards=actual,
                )
            except (ValueError, StateInvariantError) as exc:
                raise ReplayVerificationError(
                    f"hand {hand_number} cannot initialize the exact national state"
                ) from exc
            continue

        if active.hole_cards_by_connection is None or active.state is None:
            raise ReplayVerificationError("hand event occurs before cards_dealt")

        if event_type == "action_requested":
            if active.pending_action is not None:
                raise ReplayVerificationError("action requests overlap")
            player_idx = _strict_int(
                event.get("player_idx"),
                "action_requested player_idx",
                minimum=0,
                maximum=1,
            )
            stage = event.get("stage")
            if stage != active.current_stage:
                raise ReplayVerificationError("action request uses the wrong stage")
            if active.state.actor != player_idx or active.state.street.value != stage:
                raise ReplayVerificationError(
                    "action request actor/stage differs from exact national state"
                )
            request_pot = _strict_int(
                event.get("pot"),
                f"hand {hand_number} request pot",
                minimum=0,
                maximum=2 * INITIAL_CHIPS,
            )
            request_bets = _pair_ints(
                event.get("player_bets"),
                f"hand {hand_number} request player_bets",
                signed=False,
            )
            if request_pot != active.state.pot or request_bets != active.state.street_bets:
                raise ReplayVerificationError(
                    "action request pot/bets differ from exact national state"
                )
            timeout_budget = _finite_number(
                event.get("timeout_budget_sec"),
                f"hand {hand_number} request timeout budget",
                minimum=60.0,
                maximum=60.0,
            )
            if timeout_budget != 60.0:
                raise ReplayVerificationError(
                    "formal native request timeout is not exactly 60 seconds"
                )
            decision_budget_ms = _strict_int(
                event.get("decision_budget_ms"),
                f"hand {hand_number} request decision budget",
                minimum=1,
                maximum=54_000,
            )
            platform_timeout_ms = _strict_int(
                event.get("platform_action_timeout_ms"),
                f"hand {hand_number} request platform timeout",
                minimum=60_000,
                maximum=60_000,
            )
            action_send_delay_ms = _strict_int(
                event.get("action_send_delay_ms"),
                f"hand {hand_number} request action send delay",
                minimum=0,
                maximum=59_999,
            )
            if (
                decision_budget_ms != execution_binding.decision_budget_ms
                or platform_timeout_ms
                != execution_binding.platform_action_timeout_ms
                or action_send_delay_ms != execution_binding.action_send_delay_ms
            ):
                raise ReplayVerificationError(
                    "action request timing envelope differs from execution binding"
                )
            requested_epoch_ms = _strict_int(
                event.get("requested_epoch_ms"),
                f"hand {hand_number} request timestamp",
                minimum=max(1, last_timing_epoch_ms),
                maximum=(1 << 63) - 1,
            )
            compute_deadline_epoch_ms = _strict_int(
                event.get("compute_deadline_epoch_ms"),
                f"hand {hand_number} compute deadline",
                minimum=1,
                maximum=(1 << 63) - 1,
            )
            platform_deadline_epoch_ms = _strict_int(
                event.get("deadline_epoch_ms"),
                f"hand {hand_number} request deadline",
                minimum=1,
                maximum=(1 << 63) - 1,
            )
            requested_monotonic_ns = _strict_int(
                event.get("requested_monotonic_ns"),
                f"hand {hand_number} request monotonic timestamp",
                minimum=max(1, last_timing_monotonic_ns),
                maximum=(1 << 63) - 1,
            )
            compute_deadline_monotonic_ns = _strict_int(
                event.get("compute_deadline_monotonic_ns"),
                f"hand {hand_number} compute monotonic deadline",
                minimum=1,
                maximum=(1 << 63) - 1,
            )
            platform_deadline_monotonic_ns = _strict_int(
                event.get("platform_deadline_monotonic_ns"),
                f"hand {hand_number} platform monotonic deadline",
                minimum=1,
                maximum=(1 << 63) - 1,
            )
            if compute_deadline_epoch_ms != requested_epoch_ms + decision_budget_ms:
                raise ReplayVerificationError(
                    "compute deadline is not derived from the frozen cell budget"
                )
            if platform_deadline_epoch_ms != requested_epoch_ms + platform_timeout_ms:
                raise ReplayVerificationError(
                    "platform deadline is not derived from the exact 60-second timeout"
                )
            if compute_deadline_monotonic_ns != (
                requested_monotonic_ns + decision_budget_ms * 1_000_000
            ):
                raise ReplayVerificationError(
                    "monotonic compute deadline is not derived from the frozen cell budget"
                )
            if platform_deadline_monotonic_ns != (
                requested_monotonic_ns + platform_timeout_ms * 1_000_000
            ):
                raise ReplayVerificationError(
                    "monotonic platform deadline is not the exact 60-second timeout"
                )
            active.pending_action = _PendingAction(
                player_idx=player_idx,
                stage=stage,
                requested_epoch_ms=requested_epoch_ms,
                compute_deadline_epoch_ms=compute_deadline_epoch_ms,
                platform_deadline_epoch_ms=platform_deadline_epoch_ms,
                requested_monotonic_ns=requested_monotonic_ns,
                compute_deadline_monotonic_ns=compute_deadline_monotonic_ns,
                platform_deadline_monotonic_ns=platform_deadline_monotonic_ns,
            )
            last_timing_epoch_ms = requested_epoch_ms
            last_timing_monotonic_ns = requested_monotonic_ns
            continue

        if event_type == "action":
            player_idx = _strict_int(
                event.get("player_idx"),
                "action player_idx",
                minimum=0,
                maximum=1,
            )
            stage = event.get("stage")
            if active.pending_action is None or (
                active.pending_action.player_idx,
                active.pending_action.stage,
            ) != (player_idx, stage):
                raise ReplayVerificationError("action does not match its pending request")
            if active.state.actor != player_idx or active.state.street.value != stage:
                raise ReplayVerificationError(
                    "action actor/stage differs from exact national state"
                )
            decision_index = _strict_int(
                event.get("decision_index"),
                f"connection {player_idx} decision index",
                minimum=0,
                maximum=MAX_NATIVE_REPLAY_EVENTS,
            )
            if decision_index != len(decision_wait_ns[player_idx]):
                raise ReplayVerificationError(
                    "decision indices must be contiguous per connection"
                )
            nodes = _nonnegative_int(
                event.get("search_nodes"),
                f"connection {player_idx} search nodes",
            )
            fallback_used = event.get("fallback_used")
            if type(fallback_used) is not bool:
                raise ReplayVerificationError("fallback_used must be boolean")
            snapshot_tier = event.get("snapshot_tier")
            if snapshot_tier not in _SNAPSHOT_TIERS:
                raise ReplayVerificationError("unknown anytime snapshot tier")
            if fallback_used != (snapshot_tier == "safe-fallback"):
                raise ReplayVerificationError(
                    "fallback marker disagrees with the selected snapshot tier"
                )
            telemetry_source = event.get("telemetry_source")
            if telemetry_source not in _TELEMETRY_SOURCES:
                raise ReplayVerificationError("unknown decision telemetry authority")
            if telemetry_source == "harness_arrival_only":
                if nodes != 0 or fallback_used or snapshot_tier != "telemetry-unavailable":
                    raise ReplayVerificationError(
                        "arrival-only capture cannot invent search/fallback/snapshot telemetry"
                    )
                telemetry_complete[player_idx] = False
            elif snapshot_tier == "telemetry-unavailable":
                raise ReplayVerificationError(
                    "trusted worker trace cannot mark telemetry unavailable"
                )
            active.state, outcome = _apply_and_verify_action_event(
                event,
                active.state,
                hand_number=hand_number,
                pending=active.pending_action,
                execution_binding=execution_binding,
            )
            if telemetry_source == "trusted_worker_trace":
                if (outcome == PartialFaultKind.TIMEOUT.value) != (
                    snapshot_tier == "timeout-no-snapshot"
                ):
                    raise ReplayVerificationError(
                        "timeout action disagrees with its snapshot tier"
                    )
                non_timeout_tokenless = outcome in {
                    PartialFaultKind.CRASH.value,
                    PartialFaultKind.RESOURCE_OVERRUN.value,
                    PartialFaultKind.PROTOCOL.value,
                    PartialFaultKind.INFRASTRUCTURE.value,
                }
                if non_timeout_tokenless != (snapshot_tier == "fault-no-action"):
                    raise ReplayVerificationError(
                        "tokenless fault disagrees with its snapshot tier"
                    )
            decision_wait_ns[player_idx].append(
                _nonnegative_int(
                    event.get("decision_wait_ns"),
                    f"connection {player_idx} decision wait",
                )
            )
            search_nodes[player_idx] += nodes
            fallback_decisions[player_idx] += int(fallback_used)
            decision_event_indices[player_idx].append(index)
            last_timing_epoch_ms = _strict_int(
                event.get("action_epoch_ms"),
                f"hand {hand_number} action timestamp",
                minimum=last_timing_epoch_ms,
                maximum=(1 << 63) - 1,
            )
            last_timing_monotonic_ns = _strict_int(
                event.get("action_monotonic_ns"),
                f"hand {hand_number} action monotonic timestamp",
                minimum=last_timing_monotonic_ns,
                maximum=(1 << 63) - 1,
            )
            if outcome == PartialFaultKind.TIMEOUT.value:
                timeout_counts[player_idx] += 1
                observed_faults.add(
                    (PartialFaultKind.TIMEOUT, player_idx, hand_number)
                )
            elif outcome == "illegal":
                illegal_counts[player_idx] += 1
                observed_faults.add(
                    (PartialFaultKind.ILLEGAL_ACTION, player_idx, hand_number)
                )
            elif outcome in {
                PartialFaultKind.CRASH.value,
                PartialFaultKind.RESOURCE_OVERRUN.value,
                PartialFaultKind.PROTOCOL.value,
                PartialFaultKind.INFRASTRUCTURE.value,
            }:
                kind = PartialFaultKind(outcome)
                observed_faults.add(
                    (
                        kind,
                        None
                        if kind is PartialFaultKind.INFRASTRUCTURE
                        else player_idx,
                        hand_number,
                    )
                )
            active.pending_action = None
            continue

        if event_type == "stage":
            if active.pending_action is not None:
                raise ReplayVerificationError("stage changes with a pending action")
            stage = event.get("stage")
            dealt_stages = ("flop", "turn", "river")
            if (
                active.next_dealt_stage >= len(dealt_stages)
                or stage != dealt_stages[active.next_dealt_stage]
            ):
                raise ReplayVerificationError("board stages are missing or out of order")
            start, end = _DEALT_STAGE_SLICES[stage]
            cards = _parse_cards(
                event.get("cards"),
                end - start,
                f"hand {hand_number} {stage} cards",
            )
            if cards != active.expected_deck[start:end]:
                raise ReplayVerificationError(
                    f"hand {hand_number} {stage} differs from frozen deck"
                )
            if not active.state.chance_pending:
                raise ReplayVerificationError(
                    f"hand {hand_number} {stage} arrives before the betting round closes"
                )
            try:
                active.state = active.state.apply_chance(cards)
            except (ValueError, StateInvariantError) as exc:
                raise ReplayVerificationError(
                    f"hand {hand_number} {stage} violates exact chance progression"
                ) from exc
            if active.state.street.value != stage:
                raise ReplayVerificationError(
                    f"hand {hand_number} exact state reached the wrong street"
                )
            _require_player_chips(event, active.state, f"hand {hand_number} {stage}")
            known = (
                active.hole_cards_by_connection[0]
                + active.hole_cards_by_connection[1]
                + active.board
                + cards
            )
            if len(set(known)) != len(known):
                raise ReplayVerificationError("cards repeat within one hand")
            active.board += cards
            active.current_stage = stage
            active.next_dealt_stage += 1
            continue

        if event_type == "settle":
            if active.pending_action is not None:
                raise ReplayVerificationError("hand settles with a pending action")
            settlement = _settlement_from_event(event, hand_number)
            if not active.state.is_terminal:
                raise ReplayVerificationError(
                    f"hand {hand_number} settles before exact terminal state"
                )
            expected_showdown = active.state.terminal_reason == "showdown"
            if settlement["is_showdown"] is not expected_showdown:
                raise ReplayVerificationError(
                    f"hand {hand_number} settlement terminal kind differs from exact state"
                )
            if settlement["pot"] != active.state.pot:
                raise ReplayVerificationError(
                    f"hand {hand_number} settlement pot differs from exact state"
                )
            try:
                expected_utility = active.state.terminal_utility()
            except (ValueError, StateInvariantError) as exc:
                raise ReplayVerificationError(
                    f"hand {hand_number} terminal utility cannot be recomputed"
                ) from exc
            if tuple(settlement["earnings"]) != expected_utility:
                raise ReplayVerificationError(
                    f"hand {hand_number} settlement earnings differ from exact terminal utility"
                )
            if expected_showdown:
                expected_winner = (
                    0
                    if expected_utility[0] > 0
                    else 1
                    if expected_utility[1] > 0
                    else None
                )
            else:
                expected_winner = active.state.winner
            if settlement["winner_idx"] != expected_winner:
                raise ReplayVerificationError(
                    f"hand {hand_number} winner disagrees with exact terminal utility"
                )
            _require_player_chips(event, active.state, f"hand {hand_number} settlement")
            if settlement["is_showdown"]:
                if len(active.board) != 5:
                    raise ReplayVerificationError("showdown does not contain a full board")
                if (
                    _strict_int(
                        event.get("sb_idx"),
                        "showdown sb_idx",
                        minimum=0,
                        maximum=1,
                    )
                    != active.sb_idx
                    or _strict_int(
                        event.get("bb_idx"),
                        "showdown bb_idx",
                        minimum=0,
                        maximum=1,
                    )
                    != active.bb_idx
                ):
                    raise ReplayVerificationError("showdown blind roles changed")
                sb_cards = _parse_cards(event.get("sb_cards"), 2, "showdown SB cards")
                bb_cards = _parse_cards(event.get("bb_cards"), 2, "showdown BB cards")
                community = _parse_cards(
                    event.get("community"),
                    5,
                    "showdown community",
                )
                if sb_cards != active.expected_deck[0:2]:
                    raise ReplayVerificationError("showdown SB cards changed")
                if bb_cards != active.expected_deck[2:4]:
                    raise ReplayVerificationError("showdown BB cards changed")
                if community != active.board:
                    raise ReplayVerificationError("showdown community changed")
            elif "community" in event:
                community = _parse_cards(
                    event.get("community"),
                    len(active.board),
                    "fold community",
                )
                if community != active.board:
                    raise ReplayVerificationError("fold community changed")
            active.settlement = settlement
            normalized_settlements.append(settlement)
            active = None
            continue

        raise ReplayVerificationError(f"event {index} is not valid in a hand")

    if any(hand.hole_cards_by_connection is None for hand in hands):
        raise ReplayVerificationError(
            "every hand_start must bind a cards_dealt event"
        )
    if not client_order_seen:
        raise ReplayVerificationError(
            "native replay requires a mandatory first client_order event"
        )

    hands_started = len(hands)
    settlement_count = len(normalized_settlements)
    computed_total = (
        sum(row["earnings"][0] for row in normalized_settlements),
        sum(row["earnings"][1] for row in normalized_settlements),
    )
    if sum(computed_total) != 0:
        raise ReplayVerificationError("aggregate settlement earnings are not zero-sum")

    top_hands = _strict_int(
        payload.get("hands_played"),
        "top-level hands_played",
        minimum=0,
        maximum=HANDS_PER_MATCH,
    )
    if top_hands != hands_started:
        raise ReplayVerificationError("top-level hands_played differs from hand_start events")
    top_total = (
        _strict_int(
            payload.get("net_chips_a"),
            "net_chips_a",
            minimum=-(HANDS_PER_MATCH * INITIAL_CHIPS),
            maximum=HANDS_PER_MATCH * INITIAL_CHIPS,
        ),
        _strict_int(
            payload.get("net_chips_b"),
            "net_chips_b",
            minimum=-(HANDS_PER_MATCH * INITIAL_CHIPS),
            maximum=HANDS_PER_MATCH * INITIAL_CHIPS,
        ),
    )
    if top_total != computed_total:
        raise ReplayVerificationError("top-level net chips differ from settlements")
    if match_end_hands is not None:
        if match_end_hands != hands_started or match_end_total != computed_total:
            raise ReplayVerificationError("match_end differs from captured settlements")
    if partial_fault is not None:
        # Run this before compliance-summary checks so a complete result can
        # never be relabelled as partial merely by dirtying its summary flags.
        _validate_partial_fault(
            partial_fault,
            hands_started=hands_started,
            settlement_count=settlement_count,
            match_end_hands=match_end_hands,
            observed_faults=observed_faults,
        )

    top_passed = payload.get("passed_compliance")
    if type(top_passed) is not bool:
        raise ReplayVerificationError("passed_compliance must be boolean")
    top_issues = _string_list(payload.get("issues"), "issues")
    if top_passed is not (len(top_issues) == 0):
        raise ReplayVerificationError(
            "top-level compliance flag contradicts issues"
        )
    if partial_fault is None:
        if top_passed is not True or top_issues:
            raise ReplayVerificationError(
                "clean replay must pass top-level compliance with no issues"
            )
        if observed_faults or any(timeout_counts) or any(illegal_counts):
            raise ReplayVerificationError(
                "clean replay contains a captured fault adjudication"
            )
    elif top_passed is not False or not top_issues:
        raise ReplayVerificationError(
            "partial fault replay must expose non-clean top-level compliance"
        )

    top_settlements = _list(payload.get("settlements"), "settlements")
    if len(top_settlements) != settlement_count:
        raise ReplayVerificationError("top-level settlement count differs from events")
    for index, (top, actual) in enumerate(
        zip(top_settlements, normalized_settlements),
        1,
    ):
        top_row = _mapping(top, f"settlements[{index - 1}]")
        if set(top_row) != set(actual) or top_row != actual:
            raise ReplayVerificationError(
                f"top-level settlement {index} differs from its event"
            )

    per_player = _mapping(payload.get("per_player"), "per_player")
    if set(per_player) != {bot_a, bot_b}:
        raise ReplayVerificationError("per_player labels differ from bot_a/bot_b")
    for player_idx, label in enumerate((bot_a, bot_b)):
        row = _mapping(per_player[label], f"per_player[{label}]")
        if row.get("wrapper_used") is not False:
            raise ReplayVerificationError(f"{label} reports wrapper use")
        earnings = _strict_int(
            row.get("earnings"),
            f"{label} earnings",
            minimum=-(HANDS_PER_MATCH * INITIAL_CHIPS),
            maximum=HANDS_PER_MATCH * INITIAL_CHIPS,
        )
        if earnings != computed_total[player_idx]:
            raise ReplayVerificationError(f"{label} earnings differ from settlements")
        reported_timeouts = _nonnegative_int(
            row.get("timeouts"),
            f"{label} timeouts",
        )
        if reported_timeouts != timeout_counts[player_idx]:
            raise ReplayVerificationError(f"{label} timeout count differs from events")
        if (
            _nonnegative_int(row.get("illegal_actions"), f"{label} illegal actions")
            != illegal_counts[player_idx]
        ):
            raise ReplayVerificationError(
                f"{label} illegal-action count differs from events"
            )
        player_passed = row.get("passed_compliance")
        if type(player_passed) is not bool:
            raise ReplayVerificationError(
                f"{label} passed_compliance must be boolean"
            )
        player_issues = _string_list(
            row.get("compliance_issues"),
            f"{label} compliance_issues",
        )
        if player_passed is not (len(player_issues) == 0):
            raise ReplayVerificationError(
                f"{label} compliance flag contradicts its issues"
            )
        if any(issue not in top_issues for issue in player_issues):
            raise ReplayVerificationError(
                f"{label} compliance issue is absent from top-level issues"
            )

        native = _mapping(row.get("native"), f"{label} native process")
        returncode = native.get("returncode")
        if returncode is not None and type(returncode) is not int:
            raise ReplayVerificationError(
                f"{label} native returncode must be integer or null"
            )
        process_failures = _nonnegative_int(
            native.get("process_failures"),
            f"{label} native process_failures",
        )
        json_stdout = _nonnegative_int(
            native.get("json_response_stdout"),
            f"{label} native json_response_stdout",
        )
        native_clean = (
            returncode in (0, None)
            and process_failures == 0
            and json_stdout == 0
        )
        if player_passed and not native_clean:
            raise ReplayVerificationError(
                f"{label} clean compliance contradicts native process evidence"
            )

        if partial_fault is None:
            if (
                player_passed is not True
                or player_issues
                or returncode != 0
                or process_failures != 0
                or json_stdout != 0
            ):
                raise ReplayVerificationError(
                    f"clean replay requires clean native evidence for {label}"
                )
            continue

        owner = partial_fault.owner_connection
        if owner is None:
            # Infrastructure faults need not dirty either candidate, but any
            # player presented as clean must still have clean process facts.
            continue
        if player_idx != owner:
            if player_passed is not True or player_issues or not native_clean:
                raise ReplayVerificationError(
                    "partial fault dirties the non-owner connection"
                )
            continue
        if player_passed is not False or not player_issues:
            raise ReplayVerificationError(
                "partial fault owner is marked clean"
            )
        if partial_fault.kind is PartialFaultKind.CRASH:
            if returncode in (0, None) or process_failures == 0:
                raise ReplayVerificationError(
                    "crash fault contradicts native process evidence"
                )
        elif partial_fault.kind in (
            PartialFaultKind.TIMEOUT,
            PartialFaultKind.ILLEGAL_ACTION,
        ):
            if returncode != 0 or process_failures != 0 or json_stdout != 0:
                raise ReplayVerificationError(
                    "action fault is mixed with contradictory process failure"
                )

    events_tail = _list(payload.get("events_tail"), "events_tail")
    if events_tail != events[-20:]:
        raise ReplayVerificationError("events_tail differs from the complete event list")

    if partial_fault is None:
        if (
            hands_started != HANDS_PER_MATCH
            or settlement_count != HANDS_PER_MATCH
            or active is not None
            or match_end_event_index is None
            or match_end_hands != HANDS_PER_MATCH
            or hands[-1].hand_number != HANDS_PER_MATCH
        ):
            raise ReplayVerificationError(
                "clean native replay requires 70 settlements and hand 70 match_end"
            )

    verified_full_decks = tuple(
        hand.full_deck_digest for hand in hands
    )
    if len(verified_full_decks) != hands_started:
        raise ReplayVerificationError("verified deck prefix length is inconsistent")

    hand70_evidence_digest: str | None = None
    if partial_fault is None:
        hand70 = hands[-1]
        hand70_evidence_digest = _digest_payload(
            {
                "domain": "native-replay-hand70-evidence-v1",
                "full_deck_digest": hand70.full_deck_digest,
                "hand_event_digests": [
                    event_digests[index] for index in hand70.event_indices
                ],
                "match_end_event_digest": event_digests[match_end_event_index],
                "net_chips_by_connection": list(computed_total),
                "settlement": hand70.settlement,
            }
        )

    raw_digest = hashlib.sha256(raw_json).hexdigest()
    canonical_digest = hashlib.sha256(canonical).hexdigest()
    decision_trace_digests = tuple(
        _digest_payload(
            {
                "domain": "native-replay-decision-trace-v1",
                "connection_index": connection,
                "identity_digest": execution_binding.connection_identity_digests[
                    connection
                ],
                "run_id": execution_binding.run_ids_by_connection[connection],
                "action_event_digests": [
                    event_digests[index]
                    for index in decision_event_indices[connection]
                ],
            }
        )
        for connection in range(2)
    )
    evidence = _verification_evidence_payload(
        execution_binding_digest=execution_binding.binding_digest,
        execution_binding_authority=execution_binding.authority,
        leg_plan_digest=execution_binding.leg_plan_digest,
        connection_identity_digests=(
            execution_binding.connection_identity_digests
        ),
        run_ids_by_connection=execution_binding.run_ids_by_connection,
        process_tree_ids_by_connection=(
            execution_binding.process_tree_ids_by_connection
        ),
        cgroup_paths_by_connection=execution_binding.cgroup_paths_by_connection,
        resource_profile_digest=execution_binding.resource_profile_digest,
        raw_wire_digest=execution_binding.raw_wire_digest,
        supervisor_contract_digest=execution_binding.supervisor_contract_digest,
        supervisor_readiness_attestation_digest=execution_binding.supervisor_readiness_attestation_digest,
        supervisor_launch_authorization_digest=execution_binding.supervisor_launch_authorization_digest,
        supervisor_leg_receipt_digest=execution_binding.supervisor_leg_receipt_digest,
        supervisor_attempt_journal_scope_digest=execution_binding.supervisor_attempt_journal_scope_digest,
        supervisor_attempt_sequence=execution_binding.supervisor_attempt_sequence,
        supervisor_previous_attempt_entry_digest=execution_binding.supervisor_previous_attempt_entry_digest,
        supervisor_leg_run_id=execution_binding.supervisor_leg_run_id,
        supervisor_receipt_consumption_key=execution_binding.supervisor_receipt_consumption_key,
        supervisor_consumption_ledger_entry_digest=execution_binding.supervisor_consumption_ledger_entry_digest,
        supervisor_consumption_ledger_entry_inode=execution_binding.supervisor_consumption_ledger_entry_inode,
        supervisor_consumption_ledger_entry_path=execution_binding.supervisor_consumption_ledger_entry_path,
        supervisor_control_session_digest=execution_binding.supervisor_control_session_digest,
        supervisor_capture_session_digest=execution_binding.supervisor_capture_session_digest,
        supervisor_socket_identity_digests=execution_binding.supervisor_socket_identity_digests,
        supervisor_wire_semantic_digest=execution_binding.supervisor_wire_semantic_digest,
        supervisor_replay_digest=execution_binding.supervisor_replay_digest,
        supervisor_decision_trace_digest=execution_binding.supervisor_decision_trace_digest,
        supervisor_fault_event_digest=execution_binding.supervisor_fault_event_digest,
        supervisor_termination_kinds=execution_binding.supervisor_termination_kinds,
        supervisor_cleanup_receipt_digest=execution_binding.supervisor_cleanup_receipt_digest,
        wire_semantics_verified=wire_semantics_verified,
        wire_semantic_binding_digest=wire_semantic_binding_digest,
        decision_budget_ms=execution_binding.decision_budget_ms,
        platform_action_timeout_ms=(
            execution_binding.platform_action_timeout_ms
        ),
        action_send_delay_ms=execution_binding.action_send_delay_ms,
        raw_digest=raw_digest,
        canonical_digest=canonical_digest,
        full_deck_digests=verified_full_decks,
        event_digests=event_digests,
        hands_started=hands_started,
        hands_played=settlement_count,
        settlement_count=settlement_count,
        net_chips_by_connection=computed_total,
        timeout_count_by_connection=timeout_counts,
        illegal_action_count_by_connection=illegal_counts,
        decision_wait_ns_by_connection=decision_wait_ns,
        search_nodes_by_connection=search_nodes,
        fallback_decisions_by_connection=fallback_decisions,
        decision_trace_digest_by_connection=decision_trace_digests,
        telemetry_complete_by_connection=telemetry_complete,
        hand70_evidence_digest=hand70_evidence_digest,
        result_finalized_epoch_ms=result_finalized_epoch_ms,
        partial_fault=partial_fault,
    )
    verification_evidence_digest = _digest_payload(evidence)
    return VerifiedNativeReplay(
        _token=_TOKEN,
        execution_binding=execution_binding,
        raw_digest=raw_digest,
        canonical_digest=canonical_digest,
        verification_evidence_digest=verification_evidence_digest,
        full_deck_digests=verified_full_decks,
        event_digests=tuple(event_digests),
        hands_started=hands_started,
        hands_played=settlement_count,
        settlement_count=settlement_count,
        net_chips_by_connection=computed_total,
        timeout_count_by_connection=tuple(timeout_counts),
        illegal_action_count_by_connection=tuple(illegal_counts),
        decision_wait_ns_by_connection=(
            tuple(decision_wait_ns[0]),
            tuple(decision_wait_ns[1]),
        ),
        search_nodes_by_connection=tuple(search_nodes),
        fallback_decisions_by_connection=tuple(fallback_decisions),
        decision_trace_digest_by_connection=decision_trace_digests,
        telemetry_complete_by_connection=tuple(telemetry_complete),
        wire_semantics_verified=wire_semantics_verified,
        wire_semantic_binding_digest=wire_semantic_binding_digest,
        hand70_evidence_digest=hand70_evidence_digest,
        result_finalized_epoch_ms=result_finalized_epoch_ms,
        partial_fault=partial_fault,
    )
