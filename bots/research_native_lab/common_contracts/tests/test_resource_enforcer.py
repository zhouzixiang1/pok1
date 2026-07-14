from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from bots.research_native_lab.common_contracts import resource_enforcer as resource_module
from bots.research_native_lab.common_contracts.evaluation import (
    ExecutionReceipt,
    ResourceProfile,
    ResourceReceipt,
    TerminationKind,
)
from bots.research_native_lab.common_contracts.resource_enforcer import (
    DECISION_ENFORCEMENT_EVENT_SCHEMA,
    DECISION_IDENTITY_SCHEMA,
    FORMAL_GLOBAL_LOCK_PATH,
    REQUIRED_CONTROLLERS,
    RESOURCE_ENFORCER_DIGEST,
    SUPERVISOR_BACKEND_KIND,
    ArtifactMaterializationExpectation,
    CgroupV2Ops,
    CleanupPathOutcome,
    CleanupReceipt,
    ConnectionEvidence,
    DecisionEnforcementEvent,
    DiagnosticCgroupRunner,
    EnforcementProfile,
    ExecutionBinding,
    ExecutionRawEvidenceRecord,
    FormalEnforcementUnavailable,
    FormalResourceEnforcer,
    GlobalLeaseTimeout,
    LegLaunchSpec,
    ResourceCleanupError,
    ResourceEnforcementError,
    ResourceRawEvidenceRecord,
    SocketCaptureIdentity,
    SupervisorAttestation,
    SupervisorAttemptJournalEntry,
    SupervisorAttemptJournalSeal,
    SupervisorConsumptionLedgerEntry,
    SupervisorLaunchAuthorization,
    SupervisorLegReceipt,
    TrustedSupervisorContract,
    authorize_signed_supervisor_leg,
    canonical_digest,
    decode_supervisor_attempt_journal_entry,
    decode_supervisor_attempt_journal_seal,
    decode_supervisor_attestation,
    decode_supervisor_consumption_ledger_entry,
    decode_supervisor_launch_authorization,
    decode_supervisor_leg_receipt,
    decision_trace_digest,
    default_thread_environment,
    freeze_artifact_expectation,
    global_sequential_lease,
    probe_resource_enforcer,
    probe_trusted_supervisor,
    required_candidate_sandbox_policy_digest,
    required_controllers_digest,
    thread_environment_digest,
    verify_artifact_materialization,
    verify_raw_evidence_pair,
    verify_signed_supervisor_attempt_journal,
)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FakeCgroupV2Ops(CgroupV2Ops):
    backend_kind = "fake-cgroup-v2-test-only"
    formal_eligible = False

    def __init__(self, mount: Path) -> None:
        super().__init__(
            mount_point=mount,
            mount_id=777,
            reported_root=Path("/sys/fs/cgroup"),
        )
        self.removed: list[Path] = []
        self.fail_config_file: str | None = None
        self.fail_remove_name: str | None = None

    def materialize(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        values = {
            "cgroup.controllers": " ".join(REQUIRED_CONTROLLERS),
            "cgroup.events": "populated 0\nfrozen 0\n",
            "cgroup.kill": "0",
            "cgroup.procs": "",
            "cgroup.subtree_control": "",
            "cgroup.type": "domain",
            "cpu.max": "max 100000",
            "cpu.stat": (
                "usage_usec 0\nuser_usec 0\nsystem_usec 0\nnr_periods 0\n"
                "nr_throttled 0\nthrottled_usec 0\n"
            ),
            "cpuset.cpus": "",
            "cpuset.cpus.effective": "0-7",
            "cpuset.mems": "",
            "cpuset.mems.effective": "0",
            "memory.current": "0",
            "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
            "memory.max": "max",
            "memory.oom.group": "0",
            "memory.peak": "0",
            "memory.swap.current": "0",
            "memory.swap.max": "max",
            "pids.current": "0",
            "pids.events": "max 0\n",
            "pids.max": "max",
            "pids.peak": "0",
        }
        for filename, value in values.items():
            (path / filename).write_text(value, encoding="ascii")

    def create_cgroup(self, path: Path) -> None:
        self.materialize(path)

    def write(self, path: Path, value: str) -> None:
        if self.fail_config_file == path.name:
            path.write_text("tampered", encoding="ascii")
            return
        if path.name == "cgroup.subtree_control":
            enabled = sorted(item.lstrip("+") for item in value.split())
            path.write_text(" ".join(enabled), encoding="ascii")
            return
        path.write_text(value, encoding="ascii")

    def read(self, path: Path) -> str:
        if path.name == "cgroup.procs" and path.exists():
            live = []
            for item in path.read_text(encoding="ascii").split():
                try:
                    pid = int(item)
                    os.kill(pid, 0)
                except (ValueError, ProcessLookupError):
                    continue
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().split()[2]
                except (OSError, IndexError):
                    continue
                if state != "Z":
                    live.append(str(pid))
            return "\n".join(live)
        return path.read_text(encoding="ascii")

    def is_writable(self, _path: Path) -> bool:
        return True

    def remove_cgroup(self, path: Path) -> None:
        if self.fail_remove_name == path.name:
            raise OSError("injected removal failure")
        for entry in path.iterdir():
            entry.unlink()
        path.rmdir()
        self.removed.append(path)


class LyingFormalOps(FakeCgroupV2Ops):
    formal_eligible = True


def _profile(
    *, wall_timeout_ms: int = 61_000
) -> tuple[ResourceProfile, EnforcementProfile]:
    threads = default_thread_environment(2)
    profile = ResourceProfile(
        cpu_affinity_by_connection=((0, 1), (2, 3)),
        cpu_threads_per_connection=2,
        cpu_quota_us=200_000,
        cpu_period_us=100_000,
        max_tasks_per_connection=8,
        thread_environment_digest=thread_environment_digest(threads),
        ram_limit_bytes_per_connection=128 * 1024 * 1024,
        swap_limit_bytes_per_connection=0,
        gpu_devices_by_connection=((), ()),
        vram_limit_bytes_per_connection=0,
        decision_budget_ms=5_000,
        platform_action_timeout_ms=60_000,
        match_wall_timeout_ms=wall_timeout_ms,
        action_send_delay_ms=0,
        enforcer_digest=RESOURCE_ENFORCER_DIGEST,
        cgroup_controllers_digest=required_controllers_digest(),
    )
    return profile, EnforcementProfile.from_evaluation(profile, threads)


def _artifact(
    tmp_path: Path,
    connection: int,
    code: str,
    environment: dict[str, str],
) -> tuple[ArtifactMaterializationExpectation, Path]:
    root = tmp_path / f"artifact-{connection}-{_h(code)[:10]}"
    root.mkdir()
    executable = root / "runner.py"
    executable.write_text(f"#!/usr/bin/python3\n{code}\n", encoding="utf-8")
    executable.chmod(0o700)
    expectation = freeze_artifact_expectation(
        artifact_root=root,
        executable_relative_path="runner.py",
        cwd_relative_path=".",
        argv_tail=(),
        base_environment=environment,
        role_by_relative_path={"runner.py": "runtime"},
        formal_readonly_cas_required=False,
    )
    return expectation, executable


def _binding(
    profile: EnforcementProfile,
    connection: int,
    tmp_path: Path,
    code: str,
    *,
    environment: dict[str, str] | None = None,
) -> ExecutionBinding:
    base = {
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        **(environment or {}),
    }
    expectation, executable = _artifact(tmp_path, connection, code, base)
    materialization = verify_artifact_materialization(expectation)
    return ExecutionBinding.create(
        profile=profile,
        connection_index=connection,
        identity_digest=expectation.identity_digest,
        argv=(str(executable),),
        cwd=executable.parent,
        base_environment=base,
        artifact_expectation=expectation,
        artifact_materialization=materialization,
        launch_contract_digest=expectation.launch_contract_digest,
        actual_policy_seed=100 + connection,
        run_id=_h(f"run-{connection}-{code}"),
        issuer_digest=_h("diagnostic-harness"),
        verifier_digest=_h("diagnostic-harness"),
    )


def _spec(
    profile: EnforcementProfile,
    tmp_path: Path,
    codes: tuple[str, str],
    *,
    leg_run: str = "leg-run",
) -> LegLaunchSpec:
    return LegLaunchSpec(
        leg_plan_digest=_h("leg-plan"),
        leg_run_id=_h(leg_run),
        bindings=(
            _binding(profile, 0, tmp_path, codes[0]),
            _binding(profile, 1, tmp_path, codes[1]),
        ),
    )


def _diagnostic_runner(
    tmp_path: Path,
    profile: EnforcementProfile,
    *,
    ops: FakeCgroupV2Ops | None = None,
    test_wall_timeout_ms: int | None = None,
) -> tuple[DiagnosticCgroupRunner, FakeCgroupV2Ops, Path]:
    mount = tmp_path / "fake-cgroup2"
    root = mount / "delegated"
    ops = ops or FakeCgroupV2Ops(mount)
    ops.materialize(root)
    runner = DiagnosticCgroupRunner(
        delegated_root=root,
        lock_path=tmp_path / "diagnostic.lock",
        profile=profile,
        lease_timeout_sec=0.2,
        _ops=ops,
        _allow_test_backend=True,
        _test_wall_timeout_ms=test_wall_timeout_ms,
    )
    return runner, ops, root


def _decision(
    *,
    connection: int = 0,
    start_ns: int = 1_000_000_000,
    decision_index: int = 0,
    request_seq: int = 10,
    fault_kind: str = "none",
    fault_owner: int | None = None,
) -> DecisionEnforcementEvent:
    capture = _h("capture-session")
    timeout = fault_kind == "timeout"
    action_seq = None if timeout else request_seq + 1
    close_seq = request_seq + 2
    action_digest = None if timeout else _h("action-token")
    decision_id = canonical_digest(
        {
            "action_raw_record_seq": action_seq,
            "action_token_digest": action_digest,
            "capture_session_digest": capture,
            "connection_index": connection,
            "decision_close_raw_record_seq": close_seq,
            "decision_index": decision_index,
            "hand_index": 0,
            "request_raw_record_seq": request_seq,
            "schema": DECISION_IDENTITY_SCHEMA,
            "street": "preflop",
        }
    )
    frozen = start_ns + (5_000_000_000 if timeout else 3_000_000)
    sent = None if timeout else frozen + 1_000
    close_ns = start_ns + 60_000_000_000 if timeout else frozen + 2_000
    return DecisionEnforcementEvent(
        schema=DECISION_ENFORCEMENT_EVENT_SCHEMA,
        decision_id=decision_id,
        capture_session_digest=capture,
        connection_index=connection,
        hand_index=0,
        street="preflop",
        decision_index=decision_index,
        request_raw_record_seq=request_seq,
        action_raw_record_seq=action_seq,
        decision_close_raw_record_seq=close_seq,
        request_token_digest=_h("decision-open-token"),
        action_token_digest=action_digest,
        requested_monotonic_ns=start_ns,
        worker_thawed_monotonic_ns=start_ns + 1_000,
        complete_snapshot_monotonic_ns=start_ns + 2_000,
        worker_frozen_monotonic_ns=frozen,
        action_sent_monotonic_ns=sent,
        decision_close_monotonic_ns=close_ns,
        compute_budget_ms=5_000,
        platform_timeout_ms=60_000,
        selected_snapshot_digest=_h("snapshot"),
        fallback_was_ready_at_request=True,
        opponent_worker_frozen=True,
        hard_stop_fired=timeout,
        fault_kind=fault_kind,
        fault_connection_index=fault_owner,
    )


def _supervisor_contract() -> TrustedSupervisorContract:
    return TrustedSupervisorContract(
        schema="pok-trusted-resource-supervisor-v1",
        supervisor_executable="/usr/libexec/pok-resource-supervisor",
        supervisor_executable_sha256=_h("supervisor"),
        verifier_executable="/usr/libexec/pok-resource-attestation-verify",
        verifier_executable_sha256=_h("verifier"),
        public_key_path="/etc/pok/resource-supervisor.pub",
        public_key_sha256=_h("key"),
        control_socket_path="/run/pok-formal/control.sock",
        control_cgroup_root="/sys/fs/cgroup/pok-formal",
        artifact_cas_root="/var/lib/pok-formal/cas",
        consumption_ledger_root="/var/lib/pok-formal/receipt-ledger-v1",
        attempt_journal_root="/var/lib/pok-formal/attempt-journal-v1",
        global_lock_path=str(FORMAL_GLOBAL_LOCK_PATH),
        service_uid=0,
        evaluator_uid=2001,
        bot_uids_by_connection=(2002, 2003),
        peer_cgroup_auth_required=True,
        preopened_control_fd_required=True,
        private_proc_required=True,
        readonly_artifact_mount_required=True,
        candidate_sandbox_policy_digest=required_candidate_sandbox_policy_digest(),
        separate_network_namespace_required=True,
        separate_ipc_namespace_required=True,
        separate_mount_namespace_required=True,
        private_tmpfs_required=True,
        no_new_privs_required=True,
        seccomp_socket_lockdown_required=True,
        landlock_required=True,
        authenticated_game_fd_only_required=True,
        durable_consumption_ledger_required=True,
        consumption_no_clobber_required=True,
        consumption_fsync_required=True,
        durable_attempt_journal_required=True,
        attempt_journal_no_clobber_required=True,
        attempt_journal_fsync_required=True,
    )


def _signed_stage_fixtures() -> tuple[
    TrustedSupervisorContract,
    SupervisorAttestation,
    SupervisorLaunchAuthorization,
    SupervisorLegReceipt,
    SupervisorConsumptionLedgerEntry,
]:
    contract = _supervisor_contract()
    control = _h("control-session")
    readiness = SupervisorAttestation(
        schema="pok-trusted-resource-supervisor-attestation-v1",
        contract_digest=contract.digest(),
        control_session_digest=control,
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        supervisor_pid=1,
        supervisor_start_ticks=1,
        supervisor_cgroup="/pok-formal-supervisor",
        control_cgroup_inode=11,
        control_cgroup_mount_id=36,
        artifact_cas_inode=12,
        consumption_ledger_root_inode=14,
        attempt_journal_root_inode=15,
        global_lock_inode=13,
        evaluator_uid=2001,
        bot_uids_by_connection=(2002, 2003),
        peer_cgroup_auth_verified=True,
        private_proc_verified=True,
        readonly_artifact_mount_verified=True,
        candidates_cannot_write_cgroupfs=True,
        candidates_cannot_signal_peer=True,
        candidate_sandbox_policy_digest=contract.candidate_sandbox_policy_digest,
        separate_network_namespaces_verified=True,
        separate_ipc_namespaces_verified=True,
        separate_mount_namespaces_verified=True,
        private_tmpfs_verified=True,
        no_new_privs_verified=True,
        seccomp_socket_lockdown_verified=True,
        landlock_verified=True,
        authenticated_game_fd_only_verified=True,
        durable_consumption_ledger_verified=True,
        consumption_no_clobber_verified=True,
        consumption_fsync_verified=True,
        durable_attempt_journal_verified=True,
        attempt_journal_no_clobber_verified=True,
        attempt_journal_fsync_verified=True,
        issued_epoch_ms=1_000,
        expires_epoch_ms=61_000,
        nonce="probe-" + "p" * 59,
        signature_hex="00" * 64,
    )
    pair = (_h("identity-0"), _h("identity-1"))
    materials = (_h("material-0"), _h("material-1"))
    commands = (_h("command-0"), _h("command-1"))
    bases = (_h("base-0"), _h("base-1"))
    launches = (_h("launch-0"), _h("launch-1"))
    issuers = (_h("harness"), _h("harness"))
    verifiers = (_h("harness"), _h("harness"))
    launch = SupervisorLaunchAuthorization(
        schema="pok-trusted-resource-supervisor-launch-authorization-v1",
        contract_digest=contract.digest(),
        readiness_attestation_digest=readiness.payload_digest(),
        control_session_digest=control,
        request_nonce="launch-" + "l" * 57,
        attempt_journal_scope_digest=_h("matrix-plan-scope"),
        attempt_sequence=1,
        previous_attempt_entry_digest="0" * 64,
        leg_plan_digest=_h("leg-plan"),
        leg_run_id=_h("leg-run"),
        profile_digest=_h("profile"),
        ordered_identity_digests=pair,
        ordered_materialization_digests=materials,
        ordered_launch_command_digests=commands,
        ordered_base_environment_digests=bases,
        ordered_launch_environment_digests=launches,
        ordered_issuer_digests=issuers,
        ordered_execution_verifier_digests=verifiers,
        ordered_policy_seeds=(0, 1),
        ordered_process_uids=(2002, 2003),
        issued_epoch_ms=2_000,
        expires_epoch_ms=60_000,
        signature_hex="11" * 64,
    )
    capture = _h("capture-session")
    sockets = (
        SocketCaptureIdentity(
            schema="pok-supervisor-socket-capture-identity-v1",
            capture_session_digest=capture,
            connection_index=0,
            socket_fd=10,
            socket_inode=100,
            socket_cookie=1000,
            network_namespace_inode=200,
            owner_pid=5000,
            owner_start_ticks=50,
            owner_uid=2002,
            owner_cgroup_inode=300,
            local_host="127.0.0.1",
            local_port=10001,
            peer_host="127.0.0.1",
            peer_port=30001,
        ),
        SocketCaptureIdentity(
            schema="pok-supervisor-socket-capture-identity-v1",
            capture_session_digest=capture,
            connection_index=1,
            socket_fd=11,
            socket_inode=101,
            socket_cookie=1001,
            network_namespace_inode=201,
            owner_pid=5001,
            owner_start_ticks=51,
            owner_uid=2003,
            owner_cgroup_inode=301,
            local_host="127.0.0.1",
            local_port=10001,
            peer_host="127.0.0.1",
            peer_port=30002,
        ),
    )
    event = _decision()
    raw_wire = _h("raw-wire")
    semantic = _h("wire-semantic")
    replay = _h("replay-semantic")
    capture_challenge = "capture-" + "c" * 56
    consumption = canonical_digest(
        {
            "capture_challenge": capture_challenge,
            "capture_session_digest": capture,
            "control_session_digest": control,
            "attempt_sequence": launch.attempt_sequence,
            "attempt_journal_scope_digest": launch.attempt_journal_scope_digest,
            "launch_authorization_digest": launch.payload_digest(),
            "leg_run_id": launch.leg_run_id,
            "previous_attempt_entry_digest": launch.previous_attempt_entry_digest,
            "raw_wire_digest": raw_wire,
            "replay_digest": replay,
            "schema": "pok-supervisor-leg-receipt-consumption-key-v1",
            "wire_semantic_digest": semantic,
        }
    )
    entry = SupervisorConsumptionLedgerEntry(
        schema="pok-supervisor-consumption-ledger-entry-v1",
        contract_digest=contract.digest(),
        readiness_attestation_digest=readiness.payload_digest(),
        control_session_digest=control,
        attempt_journal_scope_digest=launch.attempt_journal_scope_digest,
        attempt_sequence=launch.attempt_sequence,
        previous_attempt_entry_digest=launch.previous_attempt_entry_digest,
        launch_authorization_digest=launch.payload_digest(),
        leg_run_id=launch.leg_run_id,
        capture_session_digest=capture,
        receipt_consumption_key=consumption,
        raw_wire_digest=raw_wire,
        wire_semantic_digest=semantic,
        replay_digest=replay,
        ordered_socket_identity_digests=tuple(item.digest() for item in sockets),
        decision_trace_digest=decision_trace_digest((event,)),
        cleanup_receipt_digest=_h("cleanup"),
        termination_kinds=("normal", "normal"),
        receipt_issued_epoch_ms=70_000,
    )
    receipt = SupervisorLegReceipt(
        schema="pok-trusted-resource-supervisor-leg-receipt-v3",
        contract_digest=contract.digest(),
        readiness_attestation_digest=readiness.payload_digest(),
        control_session_digest=control,
        launch_authorization_digest=launch.payload_digest(),
        request_nonce=launch.request_nonce,
        capture_challenge=capture_challenge,
        attempt_journal_scope_digest=launch.attempt_journal_scope_digest,
        attempt_sequence=launch.attempt_sequence,
        previous_attempt_entry_digest=launch.previous_attempt_entry_digest,
        capture_session_digest=capture,
        leg_plan_digest=launch.leg_plan_digest,
        leg_run_id=launch.leg_run_id,
        profile_digest=launch.profile_digest,
        ordered_identity_digests=pair,
        ordered_materialization_digests=materials,
        ordered_launch_command_digests=commands,
        ordered_base_environment_digests=bases,
        ordered_launch_environment_digests=launches,
        ordered_policy_seeds=(0, 1),
        ordered_process_pids=(5000, 5001),
        ordered_process_group_ids=(5000, 5001),
        ordered_process_start_ticks=(50, 51),
        ordered_process_uids=(2002, 2003),
        ordered_cgroup_paths=(
            "/sys/fs/cgroup/pok-formal/leg/connection-0",
            "/sys/fs/cgroup/pok-formal/leg/connection-1",
        ),
        ordered_cgroup_inodes=(300, 301),
        cgroup_mount_id=36,
        ordered_socket_identities=sockets,
        raw_wire_digest=raw_wire,
        wire_semantic_digest=semantic,
        replay_digest=replay,
        execution_raw_record_digests=(_h("execution-0"), _h("execution-1")),
        resource_raw_record_digests=(_h("resource-0"), _h("resource-1")),
        ordered_issuer_digests=issuers,
        ordered_execution_verifier_digests=verifiers,
        decision_events=(event,),
        decision_trace_digest=decision_trace_digest((event,)),
        cleanup_receipt_digest=_h("cleanup"),
        termination_kinds=("normal", "normal"),
        no_pondering_verified=True,
        per_decision_hard_stop_verified=True,
        cleanup_empty_and_removed_verified=True,
        receipt_consumption_key=consumption,
        consumption_ledger_entry_digest=entry.digest(),
        consumption_ledger_entry_inode=900,
        issued_epoch_ms=70_000,
        signature_hex="22" * 64,
    )
    return contract, readiness, launch, receipt, entry


def _authorized_supervisor_leg_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    leg_plan_digest: str | None = None,
    profile_digest: str | None = None,
    ordered_identity_digests: tuple[str, str] | None = None,
    raw_wire_digest: str | None = None,
    wire_semantic_digest: str | None = None,
    replay_digest: str | None = None,
    decision_events: tuple[DecisionEnforcementEvent, ...] | None = None,
):
    """Build a cross-linked bridge while replacing only external host services.

    No production mint exists.  The helper drives the real authorization and
    receipt conversion code, but replaces the unavailable fixed installation,
    signature subprocess, and root-owned ledger readback with their exact
    already-validated test objects.
    """

    contract, readiness, launch, receipt, ledger_entry = _signed_stage_fixtures()
    salt = _h(str(tmp_path.resolve()))
    events = tuple(
        receipt.decision_events if decision_events is None else decision_events
    )
    if not events:
        raise ValueError("test-only signed supervisor fixture requires decision events")
    capture_session_digest = events[0].capture_session_digest
    if any(
        event.capture_session_digest != capture_session_digest for event in events
    ):
        raise ValueError("test-only decision events cross capture sessions")
    sockets = tuple(
        dataclasses.replace(
            item, capture_session_digest=capture_session_digest
        )
        for item in receipt.ordered_socket_identities
    )
    trace_digest = decision_trace_digest(events)
    launch = dataclasses.replace(
        launch,
        request_nonce="launch-" + salt,
        attempt_journal_scope_digest=_h(f"matrix-scope-{salt}"),
        leg_plan_digest=(
            _h(f"leg-plan-{salt}")
            if leg_plan_digest is None
            else leg_plan_digest
        ),
        leg_run_id=_h(f"leg-run-{salt}"),
        profile_digest=(
            launch.profile_digest if profile_digest is None else profile_digest
        ),
        ordered_identity_digests=(
            launch.ordered_identity_digests
            if ordered_identity_digests is None
            else ordered_identity_digests
        ),
    )
    launch.validate(contract, readiness)
    receipt = dataclasses.replace(
        receipt,
        capture_session_digest=capture_session_digest,
        ordered_socket_identities=sockets,
        raw_wire_digest=(
            receipt.raw_wire_digest if raw_wire_digest is None else raw_wire_digest
        ),
        wire_semantic_digest=(
            receipt.wire_semantic_digest
            if wire_semantic_digest is None
            else wire_semantic_digest
        ),
        replay_digest=(
            receipt.replay_digest if replay_digest is None else replay_digest
        ),
        decision_events=events,
        decision_trace_digest=trace_digest,
    )
    _evaluation_profile, profile = _profile()
    runner, _ops, _root = _diagnostic_runner(tmp_path, profile)
    diagnostic = runner.run_leg(_spec(profile, tmp_path, ("pass", "pass")))
    cgroup_root = "/sys/fs/cgroup/pok-formal/leg"
    cleanup = CleanupReceipt(
        schema="pok-resource-cleanup-receipt-v1",
        leg_run_id=launch.leg_run_id,
        trigger="normal",
        started_epoch_ms=3_900,
        finished_epoch_ms=4_000,
        path_outcomes=(
            CleanupPathOutcome(
                f"{cgroup_root}/connection-0", False, True, True, True, None
            ),
            CleanupPathOutcome(
                f"{cgroup_root}/connection-1", False, True, True, True, None
            ),
            CleanupPathOutcome(cgroup_root, False, True, True, True, None),
        ),
        all_empty_and_removed=True,
        triggering_error_digest=None,
    )
    lease = dataclasses.replace(
        diagnostic.lease,
        lock_path=str(FORMAL_GLOBAL_LOCK_PATH),
        lock_inode=readiness.global_lock_inode,
        acquired_epoch_ms=2_500,
        released_epoch_ms=4_500,
    )
    provisional: list[ConnectionEvidence] = []
    for index, source in enumerate(diagnostic.connections):
        provisional.append(
            dataclasses.replace(
                source,
                backend_kind=SUPERVISOR_BACKEND_KIND,
                formal_eligible=True,
                lease=lease,
                leg_run_id=launch.leg_run_id,
                leg_plan_digest=launch.leg_plan_digest,
                profile_digest=launch.profile_digest,
                identity_digest=launch.ordered_identity_digests[index],
                launch_command_digest=launch.ordered_launch_command_digests[index],
                base_environment_digest=launch.ordered_base_environment_digests[index],
                launch_environment_digest=launch.ordered_launch_environment_digests[index],
                artifact_materialization_digest=launch.ordered_materialization_digests[
                    index
                ],
                actual_policy_seed=launch.ordered_policy_seeds[index],
                issuer_digest=launch.ordered_issuer_digests[index],
                execution_verifier_digest=launch.ordered_execution_verifier_digests[
                    index
                ],
                process_pid=receipt.ordered_process_pids[index],
                process_group_id=receipt.ordered_process_group_ids[index],
                process_uid=receipt.ordered_process_uids[index],
                process_start_ticks=receipt.ordered_process_start_ticks[index],
                cgroup_path=receipt.ordered_cgroup_paths[index],
                cgroup_inode=receipt.ordered_cgroup_inodes[index],
                cgroup_mount_id=receipt.cgroup_mount_id,
                started_epoch_ms=3_000 + index,
                finished_epoch_ms=3_800 + index,
                termination_kind="normal",
                exit_code=0,
                cleanup_empty_confirmed=True,
                cleanup_child_removed=True,
                cleanup_receipt_digest=cleanup.digest(),
                cleanup_error=None,
                decision_trace_digest=receipt.decision_trace_digest,
                decision_hard_stop_verified=True,
                no_pondering_verified=True,
                supervisor_leg_receipt_digest=None,
            )
        )
    capture_challenge = "capture-" + salt
    consumption_key = canonical_digest(
        {
            "capture_challenge": capture_challenge,
            "capture_session_digest": receipt.capture_session_digest,
            "control_session_digest": launch.control_session_digest,
            "attempt_sequence": launch.attempt_sequence,
            "attempt_journal_scope_digest": launch.attempt_journal_scope_digest,
            "launch_authorization_digest": launch.payload_digest(),
            "leg_run_id": launch.leg_run_id,
            "previous_attempt_entry_digest": launch.previous_attempt_entry_digest,
            "raw_wire_digest": receipt.raw_wire_digest,
            "replay_digest": receipt.replay_digest,
            "schema": "pok-supervisor-leg-receipt-consumption-key-v1",
            "wire_semantic_digest": receipt.wire_semantic_digest,
        }
    )
    ledger_entry = dataclasses.replace(
        ledger_entry,
        attempt_journal_scope_digest=launch.attempt_journal_scope_digest,
        launch_authorization_digest=launch.payload_digest(),
        leg_run_id=launch.leg_run_id,
        capture_session_digest=receipt.capture_session_digest,
        receipt_consumption_key=consumption_key,
        raw_wire_digest=receipt.raw_wire_digest,
        wire_semantic_digest=receipt.wire_semantic_digest,
        replay_digest=receipt.replay_digest,
        ordered_socket_identity_digests=tuple(
            item.digest() for item in receipt.ordered_socket_identities
        ),
        decision_trace_digest=receipt.decision_trace_digest,
        cleanup_receipt_digest=cleanup.digest(),
    )
    receipt = dataclasses.replace(
        receipt,
        launch_authorization_digest=launch.payload_digest(),
        request_nonce=launch.request_nonce,
        capture_challenge=capture_challenge,
        attempt_journal_scope_digest=launch.attempt_journal_scope_digest,
        leg_plan_digest=launch.leg_plan_digest,
        leg_run_id=launch.leg_run_id,
        profile_digest=launch.profile_digest,
        ordered_identity_digests=launch.ordered_identity_digests,
        cleanup_receipt_digest=cleanup.digest(),
        execution_raw_record_digests=tuple(
            item.execution_raw_evidence_digest() for item in provisional
        ),
        resource_raw_record_digests=tuple(
            item.resource_raw_evidence_digest() for item in provisional
        ),
        receipt_consumption_key=consumption_key,
        consumption_ledger_entry_digest=ledger_entry.digest(),
    )
    ledger_entry.validate(contract, readiness, launch, receipt)
    connections = tuple(
        dataclasses.replace(
            item, supervisor_leg_receipt_digest=receipt.payload_digest()
        )
        for item in provisional
    )

    monkeypatch.setattr(
        TrustedSupervisorContract,
        "from_fixed_file",
        classmethod(lambda cls, path=resource_module.TRUSTED_SUPERVISOR_CONTRACT_PATH: contract),
    )
    monkeypatch.setattr(resource_module, "_verify_external_signature", lambda *_: None)
    monkeypatch.setattr(
        resource_module, "_verify_external_launch_signature", lambda *_: None
    )
    monkeypatch.setattr(
        resource_module, "_verify_external_leg_signature", lambda *_: None
    )
    monkeypatch.setattr(
        resource_module, "_verify_supervisor_installation_identity", lambda *_: None
    )
    monkeypatch.setattr(resource_module, "_verify_supervisor_process", lambda *_: None)
    ledger_path = f"{contract.consumption_ledger_root}/{receipt.receipt_consumption_key}.json"
    monkeypatch.setattr(
        resource_module,
        "_verify_durable_consumption_ledger_entry",
        lambda *_: (ledger_entry, ledger_path),
    )
    bridge = authorize_signed_supervisor_leg(
        connections=connections,
        readiness=readiness,
        launch_authorization=launch,
        leg_receipt=receipt,
        expected_probe_nonce=readiness.nonce,
        expected_launch_nonce=launch.request_nonce,
        expected_capture_challenge=receipt.capture_challenge,
        expected_capture_session_digest=receipt.capture_session_digest,
        expected_socket_identities=receipt.ordered_socket_identities,
        expected_decision_events=receipt.decision_events,
        expected_cleanup_receipt=cleanup,
        expected_raw_wire_digest=receipt.raw_wire_digest,
        expected_wire_semantic_digest=receipt.wire_semantic_digest,
        expected_replay_digest=receipt.replay_digest,
        authorization_epoch_ms=receipt.issued_epoch_ms + 1,
    )
    return bridge, contract, readiness, launch, receipt, ledger_entry, cleanup


def test_same_uid_probe_is_diagnostic_only_and_read_only(tmp_path: Path) -> None:
    mount = tmp_path / "mount"
    root = mount / "delegated"
    ops = FakeCgroupV2Ops(mount)
    ops.materialize(root)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 0:32 / {mount} rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw\n"
    )
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    probe = probe_resource_enforcer(root, mountinfo_path=mountinfo)
    after = {path.name: path.read_bytes() for path in root.iterdir()}
    assert probe.diagnostic_cgroup_ready is True
    assert probe.formal_available is False
    assert "same-uid" in " ".join(probe.reasons)
    assert before == after


def test_live_same_uid_launcher_is_permanently_unavailable(tmp_path: Path) -> None:
    _, profile = _profile()
    with pytest.raises(FormalEnforcementUnavailable, match="external privileged"):
        FormalResourceEnforcer(
            delegated_root=tmp_path / "unused",
            lock_path=FORMAL_GLOBAL_LOCK_PATH,
            profile=profile,
        )
    source = Path(
        "bots/research_native_lab/common_contracts/resource_enforcer.py"
    ).read_text()
    assert "setrlimit(" not in source and "resource.setrlimit" not in source
    assert "def _mint_formal_evidence" not in source


def test_in_process_backend_cannot_claim_formal_eligibility(tmp_path: Path) -> None:
    _, profile = _profile()
    mount = tmp_path / "fake"
    root = mount / "delegated"
    ops = LyingFormalOps(mount)
    ops.materialize(root)
    with pytest.raises(FormalEnforcementUnavailable, match="never claim formal"):
        DiagnosticCgroupRunner(
            delegated_root=root,
            lock_path=tmp_path / "diagnostic.lock",
            profile=profile,
            _ops=ops,
            _allow_test_backend=True,
        )


def test_formal_lock_path_is_fixed_but_diagnostic_lock_is_explicit(tmp_path: Path) -> None:
    lock = tmp_path / "diagnostic.lock"
    with pytest.raises(FormalEnforcementUnavailable, match="fixed"):
        with global_sequential_lease(lock, timeout_sec=0.01):
            pass
    with global_sequential_lease(
        lock, timeout_sec=0.1, _allow_diagnostic_path=True
    ) as first:
        with pytest.raises(GlobalLeaseTimeout):
            with global_sequential_lease(
                lock, timeout_sec=0.01, _allow_diagnostic_path=True
            ):
                pass
    with global_sequential_lease(
        lock, timeout_sec=0.1, _allow_diagnostic_path=True
    ) as second:
        assert first.lease_id != second.lease_id


def test_artifact_materialization_binds_actual_files_and_executable(tmp_path: Path) -> None:
    base = {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"}
    expectation, executable = _artifact(tmp_path, 0, "pass", base)
    receipt = verify_artifact_materialization(expectation)
    receipt._assert_verified()
    assert receipt.identity_digest == expectation.identity_digest
    assert receipt.executable_inode == executable.stat().st_ino
    executable.write_text("#!/usr/bin/python3\nraise SystemExit(9)\n")
    with pytest.raises(ResourceEnforcementError, match="drifted"):
        verify_artifact_materialization(expectation)


def test_formal_artifact_requires_root_owned_readonly_cas_and_elf(tmp_path: Path) -> None:
    base = {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"}
    expectation, _ = _artifact(tmp_path, 0, "pass", base)
    formal = dataclasses.replace(expectation, formal_readonly_cas_required=True)
    with pytest.raises(FormalEnforcementUnavailable, match="fixed privileged supervisor"):
        verify_artifact_materialization(formal)


def test_diagnostic_runner_records_typed_cross_link_and_durable_cleanup(
    tmp_path: Path,
) -> None:
    evaluation_profile, profile = _profile()
    runner, ops, root = _diagnostic_runner(tmp_path, profile)
    leg_run = _h("stats-leg")
    paths = [root / f"leg-{leg_run}" / f"connection-{index}" for index in range(2)]
    codes = []
    for index, path in enumerate(paths):
        codes.append(
            "from pathlib import Path;"
            f"p=Path({str(path)!r});"
            f"(p/'memory.peak').write_text('{1000 + index}');"
            f"(p/'pids.peak').write_text('{3 + index}');"
            "(p/'cpu.stat').write_text('usage_usec 9\\nuser_usec 4\\n"
            "system_usec 5\\nnr_periods 2\\nnr_throttled 1\\n"
            f"throttled_usec {7 + index}\\n')"
        )
    evidence = runner.run_leg(_spec(profile, tmp_path, tuple(codes), leg_run="stats-leg"))
    assert evidence.cleanup_parent_removed is True
    assert evidence.cleanup_error is None
    assert evidence.cleanup_receipt.all_empty_and_removed is True
    assert evidence.cleanup_receipt.trigger == "normal"
    assert len(evidence.cleanup_receipt.path_outcomes) == 3
    assert not (root / f"leg-{leg_run}").exists()
    assert [path.name for path in ops.removed[-3:]] == [
        "connection-0",
        "connection-1",
        f"leg-{leg_run}",
    ]
    for index, connection in enumerate(evidence.connections):
        assert connection.formal_eligible is False
        assert connection.cleanup_receipt_digest == evidence.cleanup_receipt.digest()
        assert connection.observed_peak_memory_bytes == 1000 + index
        assert connection.observed_max_tasks == 3 + index
        assert connection.cpu_throttled_usec == 7 + index
        execution_raw = connection.execution_raw_record()
        resource_raw = connection.resource_raw_record()
        assert isinstance(execution_raw, ExecutionRawEvidenceRecord)
        assert isinstance(resource_raw, ResourceRawEvidenceRecord)
        verify_raw_evidence_pair(execution_raw, resource_raw)
        assert execution_raw.digest() != resource_raw.digest()
        with pytest.raises(FormalEnforcementUnavailable):
            connection.to_formal_receipts()

        execution_kwargs = connection.execution_receipt_kwargs()
        execution_kwargs["termination_kind"] = TerminationKind(
            execution_kwargs["termination_kind"]
        )
        execution = ExecutionReceipt(**execution_kwargs)
        resource = ResourceReceipt(
            **connection.resource_receipt_kwargs(execution.digest())
        )
        resource.verify(execution=execution, profile=evaluation_profile)
        with pytest.raises(ValueError, match="frozen resource enforcer"):
            execution._assert_formal_enforcer_authority()
        with pytest.raises(ValueError, match="frozen resource enforcer"):
            resource._assert_formal_enforcer_authority()


def test_copy_replace_and_backend_label_cannot_create_formal_authority(
    tmp_path: Path,
) -> None:
    _, profile = _profile()
    runner, _, _ = _diagnostic_runner(tmp_path, profile)
    evidence = runner.run_leg(_spec(profile, tmp_path, ("pass", "pass")))
    original = evidence.connections[0]
    variants = (
        dataclasses.replace(
            original,
            formal_eligible=True,
            backend_kind=SUPERVISOR_BACKEND_KIND,
            supervisor_leg_receipt_digest=_h("forged-supervisor-receipt"),
            decision_hard_stop_verified=True,
            no_pondering_verified=True,
        ),
        copy.copy(original),
        copy.deepcopy(original),
    )
    for variant in variants:
        with pytest.raises(FormalEnforcementUnavailable, match="capability"):
            variant.to_formal_receipts()


def test_match_wall_stop_is_infrastructure_not_double_candidate_timeout(
    tmp_path: Path,
) -> None:
    _, profile = _profile()
    runner, _, _ = _diagnostic_runner(
        tmp_path, profile, test_wall_timeout_ms=100
    )
    started = time.monotonic()
    evidence = runner.run_leg(
        _spec(
            profile,
            tmp_path,
            (
                "import time; time.sleep(30)",
                "import time; time.sleep(30)",
            ),
            leg_run="wall-stop",
        )
    )
    assert time.monotonic() - started < 5
    for connection in evidence.connections:
        assert connection.termination_kind == "infrastructure"
        assert connection.deadline_kill_count == 0
        assert connection.infrastructure_wall_kill_count == 1
        assert connection.timeout_kill_used_cgroup_kill is True
        assert connection.timeout_kill_used_process_group is True


def test_configuration_tamper_and_cleanup_failure_fail_closed(tmp_path: Path) -> None:
    _, profile = _profile()
    tamper_mount = tmp_path / "tamper" / "fake-cgroup2"
    tamper_ops = FakeCgroupV2Ops(tamper_mount)
    tamper_ops.materialize(tamper_mount / "delegated")
    tamper_ops.fail_config_file = "memory.max"
    runner, _, _ = _diagnostic_runner(tmp_path / "tamper", profile, ops=tamper_ops)
    with pytest.raises(ResourceEnforcementError, match="readback mismatch"):
        runner.run_leg(_spec(profile, tmp_path / "tamper", ("pass", "pass")))

    cleanup_root = tmp_path / "cleanup"
    cleanup_ops = FakeCgroupV2Ops(cleanup_root / "fake-cgroup2")
    cleanup_ops.fail_remove_name = "connection-1"
    runner, _, _ = _diagnostic_runner(cleanup_root, profile, ops=cleanup_ops)
    with pytest.raises(ResourceCleanupError) as caught:
        runner.run_leg(_spec(profile, cleanup_root, ("pass", "pass")))
    failed = caught.value.evidence
    assert failed.cleanup_error is not None
    assert failed.cleanup_receipt.trigger == "cleanup_failure"
    assert failed.cleanup_receipt.all_empty_and_removed is False
    assert failed.connections[1].cleanup_child_removed is False
    with pytest.raises(FormalEnforcementUnavailable):
        failed.connections[0].to_formal_receipts()


def test_decision_fault_owner_is_exact_and_infrastructure_is_uncharged() -> None:
    normal = _decision()
    timeout = _decision(
        connection=1,
        start_ns=7_000_000_000,
        decision_index=1,
        request_seq=20,
        fault_kind="timeout",
        fault_owner=1,
    )
    assert decision_trace_digest((normal, timeout)) == decision_trace_digest(
        (normal, timeout)
    )
    with pytest.raises(ValueError, match="acting connection"):
        _decision(fault_kind="timeout", fault_owner=1)
    with pytest.raises(ValueError, match="cannot be charged"):
        _decision(fault_kind="infrastructure", fault_owner=0)
    infrastructure = _decision(
        fault_kind="infrastructure", fault_owner=None
    )
    assert infrastructure.fault_connection_index is None


def test_timeout_decision_has_no_synthetic_client_action_token() -> None:
    timeout = _decision(fault_kind="timeout", fault_owner=0)
    assert timeout.action_raw_record_seq is None
    assert timeout.action_token_digest is None
    assert timeout.action_sent_monotonic_ns is None
    assert timeout.decision_close_raw_record_seq > timeout.request_raw_record_seq
    assert timeout.decision_close_monotonic_ns == (
        timeout.requested_monotonic_ns + 60_000_000_000
    )

    with pytest.raises(ValueError, match="must not claim a client action token"):
        dataclasses.replace(
            timeout,
            action_raw_record_seq=timeout.request_raw_record_seq + 1,
            action_token_digest=_h("peer-fold-is-not-a-client-token"),
            action_sent_monotonic_ns=timeout.decision_close_monotonic_ns,
        )


def test_normal_decision_requires_real_action_and_strict_close_boundary() -> None:
    normal = _decision()
    assert normal.action_raw_record_seq is not None
    assert normal.action_token_digest is not None
    assert normal.action_sent_monotonic_ns is not None
    assert (
        normal.request_raw_record_seq
        < normal.action_raw_record_seq
        < normal.decision_close_raw_record_seq
    )

    with pytest.raises(ValueError, match="requires a client action token"):
        dataclasses.replace(
            normal,
            action_raw_record_seq=None,
            action_token_digest=None,
            action_sent_monotonic_ns=None,
        )
    with pytest.raises(ValueError, match="must follow its request"):
        dataclasses.replace(
            normal,
            decision_close_raw_record_seq=normal.request_raw_record_seq,
        )
    with pytest.raises(ValueError, match="decision ID"):
        dataclasses.replace(
            normal,
            decision_close_raw_record_seq=normal.decision_close_raw_record_seq + 1,
        )
    with pytest.raises(ValueError, match="decision ID"):
        dataclasses.replace(normal, action_token_digest=_h("different-action-token"))


@pytest.mark.parametrize(
    ("fault_kind", "fault_owner"),
    (
        ("crash", 0),
        ("resource", 0),
        ("protocol", 0),
        ("infrastructure", None),
    ),
)
def test_non_timeout_fault_may_close_without_a_client_token(
    fault_kind: str,
    fault_owner: int | None,
) -> None:
    timeout = _decision(fault_kind="timeout", fault_owner=0)
    tokenless = dataclasses.replace(
        timeout,
        fault_kind=fault_kind,
        fault_connection_index=fault_owner,
        hard_stop_fired=False,
        decision_close_monotonic_ns=timeout.worker_frozen_monotonic_ns + 2_000,
    )
    assert tokenless.action_raw_record_seq is None
    assert tokenless.action_token_digest is None
    assert tokenless.action_sent_monotonic_ns is None

    with pytest.raises(ValueError, match="all present or all null"):
        dataclasses.replace(
            tokenless,
            action_token_digest=_h("partial-token-triple"),
        )

    with pytest.raises(ValueError, match="fault-free decision requires"):
        dataclasses.replace(
            tokenless,
            fault_kind="none",
            fault_connection_index=None,
        )

    with pytest.raises(ValueError, match="timeout attribution takes precedence"):
        dataclasses.replace(
            tokenless,
            decision_close_monotonic_ns=(
                tokenless.requested_monotonic_ns
                + tokenless.platform_timeout_ms * 1_000_000
            ),
        )


def test_cleanup_receipt_is_complete_ordered_and_exclusive(tmp_path: Path) -> None:
    root = "/sys/fs/cgroup/pok-formal/leg-abc"
    outcomes = (
        CleanupPathOutcome(f"{root}/connection-0", False, True, True, True, None),
        CleanupPathOutcome(f"{root}/connection-1", True, True, True, True, None),
        CleanupPathOutcome(root, False, True, True, True, None),
    )
    receipt = CleanupReceipt(
        schema="pok-resource-cleanup-receipt-v1",
        leg_run_id=_h("cleanup-leg"),
        trigger="normal",
        started_epoch_ms=1,
        finished_epoch_ms=2,
        path_outcomes=outcomes,
        all_empty_and_removed=True,
        triggering_error_digest=None,
    )
    target = tmp_path / "cleanup.json"
    receipt.write_exclusive(target)
    assert json.loads(target.read_text())["all_empty_and_removed"] is True
    with pytest.raises(FileExistsError):
        receipt.write_exclusive(target)
    with pytest.raises(ValueError, match="ordered"):
        dataclasses.replace(receipt, path_outcomes=(outcomes[1], outcomes[0], outcomes[2]))


def test_supervisor_contract_requires_root_service_distinct_low_privilege_uids() -> None:
    contract = _supervisor_contract()
    contract.validate()
    with pytest.raises(ValueError, match="uid 0"):
        dataclasses.replace(contract, service_uid=2000).validate()
    with pytest.raises(ValueError, match="distinct"):
        dataclasses.replace(contract, bot_uids_by_connection=(2002, 2002)).validate()
    with pytest.raises(ValueError, match="private_proc_required"):
        dataclasses.replace(contract, private_proc_required=False).validate()


def test_signed_authority_has_distinct_prelaunch_and_postrun_stages() -> None:
    contract, readiness, launch, receipt, ledger_entry = _signed_stage_fixtures()
    readiness.validate(contract, now_epoch_ms=2_000)
    launch.validate(contract, readiness)
    receipt.validate(contract, readiness, launch)
    ledger_entry.validate(contract, readiness, launch, receipt)
    launch_fields = {field.name for field in dataclasses.fields(launch)}
    assert "raw_wire_digest" not in launch_fields
    assert "replay_digest" not in launch_fields
    assert receipt.raw_wire_digest == _h("raw-wire")
    assert tuple(item.connection_index for item in receipt.ordered_socket_identities) == (
        0,
        1,
    )
    assert receipt.decision_events[0].request_raw_record_seq == 10
    assert receipt.decision_events[0].action_raw_record_seq == 11
    assert receipt.decision_events[0].decision_close_raw_record_seq == 12
    with pytest.raises(ValueError, match="unknown supervisor leg receipt schema"):
        dataclasses.replace(
            receipt, schema="pok-trusted-resource-supervisor-leg-receipt-v1"
        ).validate(contract, readiness, launch)


def test_authorize_signed_supervisor_leg_emits_one_formal_pair_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        bridge,
        contract,
        readiness,
        launch,
        receipt,
        ledger_entry,
        cleanup,
    ) = _authorized_supervisor_leg_fixture(monkeypatch, tmp_path)
    payload = bridge.replay_binding_payload()
    assert payload["supervisor_contract_digest"] == contract.digest()
    assert payload["readiness_attestation_digest"] == readiness.payload_digest()
    assert payload["launch_authorization_digest"] == launch.payload_digest()
    assert payload["supervisor_leg_receipt_digest"] == receipt.payload_digest()
    assert payload["receipt_consumption_key"] == receipt.receipt_consumption_key
    assert payload["consumption_ledger_entry_digest"] == ledger_entry.digest()
    assert payload["capture_session_digest"] == receipt.capture_session_digest
    assert payload["socket_identity_digests"] == tuple(
        item.digest() for item in receipt.ordered_socket_identities
    )
    assert payload["raw_wire_digest"] == receipt.raw_wire_digest
    assert payload["wire_semantic_digest"] == receipt.wire_semantic_digest
    assert payload["raw_replay_digest"] == receipt.replay_digest
    assert payload["decision_events"] == tuple(
        dataclasses.asdict(item) for item in receipt.decision_events
    )
    assert payload["supervisor_fault_events"] == ()
    assert payload["cleanup_receipt_digest"] == cleanup.digest()
    assert payload["termination_kinds"] == ("normal", "normal")

    pairs = bridge.formal_receipts()
    assert len(pairs) == 2
    for execution, resource in pairs:
        execution._assert_formal_enforcer_authority()
        resource._assert_formal_enforcer_authority()
    with pytest.raises(FormalEnforcementUnavailable, match="already emitted"):
        bridge.formal_receipts()


def test_authorized_supervisor_test_fixture_rebinds_all_requested_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identities = (_h("override-identity-0"), _h("override-identity-1"))
    event = _decision()
    (
        bridge,
        contract,
        readiness,
        launch,
        receipt,
        ledger_entry,
        _cleanup,
    ) = _authorized_supervisor_leg_fixture(
        monkeypatch,
        tmp_path,
        leg_plan_digest=_h("override-leg-plan"),
        profile_digest=_h("override-profile"),
        ordered_identity_digests=identities,
        raw_wire_digest=_h("override-raw-wire"),
        wire_semantic_digest=_h("override-wire-semantic"),
        replay_digest=_h("override-replay"),
        decision_events=(event,),
    )
    payload = bridge.replay_binding_payload()
    assert launch.leg_plan_digest == _h("override-leg-plan")
    assert launch.profile_digest == _h("override-profile")
    assert launch.ordered_identity_digests == identities
    assert receipt.raw_wire_digest == _h("override-raw-wire")
    assert receipt.wire_semantic_digest == _h("override-wire-semantic")
    assert receipt.replay_digest == _h("override-replay")
    assert receipt.decision_events == (event,)
    assert ledger_entry.raw_wire_digest == receipt.raw_wire_digest
    assert ledger_entry.wire_semantic_digest == receipt.wire_semantic_digest
    assert ledger_entry.replay_digest == receipt.replay_digest
    assert payload["raw_wire_digest"] == receipt.raw_wire_digest
    receipt.validate(contract, readiness, launch)
    ledger_entry.validate(contract, readiness, launch, receipt)


def test_postrun_receipt_rejects_wire_replay_socket_and_challenge_grafts() -> None:
    contract, readiness, launch, receipt, _ledger_entry = _signed_stage_fixtures()
    with pytest.raises(ValueError, match="consumption key"):
        dataclasses.replace(receipt, raw_wire_digest=_h("other-wire")).validate(
            contract, readiness, launch
        )
    with pytest.raises(ValueError, match="socket identities"):
        changed_socket = dataclasses.replace(
            receipt.ordered_socket_identities[0],
            capture_session_digest=_h("other-capture"),
        )
        dataclasses.replace(
            receipt,
            ordered_socket_identities=(
                changed_socket,
                receipt.ordered_socket_identities[1],
            ),
        ).validate(contract, readiness, launch)
    with pytest.raises(ValueError, match="owned by the signed candidate"):
        wrong_owner = dataclasses.replace(
            receipt.ordered_socket_identities[0], owner_pid=5999
        )
        dataclasses.replace(
            receipt,
            ordered_socket_identities=(
                wrong_owner,
                receipt.ordered_socket_identities[1],
            ),
        ).validate(contract, readiness, launch)
    with pytest.raises(ValueError, match="invalid or reused"):
        dataclasses.replace(
            receipt, capture_challenge=launch.request_nonce
        ).validate(contract, readiness, launch)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(dataclasses.asdict(value), sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_signed_supervisor_decoders_round_trip_exact_current_fields() -> None:
    contract, readiness, launch, receipt, ledger_entry = _signed_stage_fixtures()
    decoded_readiness = decode_supervisor_attestation(_json_bytes(readiness))
    decoded_launch = decode_supervisor_launch_authorization(_json_bytes(launch))
    decoded_receipt = decode_supervisor_leg_receipt(_json_bytes(receipt))
    decoded_ledger = decode_supervisor_consumption_ledger_entry(
        _json_bytes(ledger_entry)
    )
    assert decoded_readiness == readiness
    assert decoded_launch == launch
    assert decoded_receipt == receipt
    assert decoded_ledger == ledger_entry
    decoded_readiness.validate(contract, now_epoch_ms=2_000)
    decoded_launch.validate(contract, decoded_readiness)
    decoded_receipt.validate(contract, decoded_readiness, decoded_launch)
    decoded_ledger.validate(
        contract, decoded_readiness, decoded_launch, decoded_receipt
    )
    historical = dataclasses.replace(
        readiness, boot_id="00000000-0000-0000-0000-000000000001"
    )
    with pytest.raises(ValueError, match="not from this boot"):
        historical.validate(contract, now_epoch_ms=2_000)
    historical.validate(
        contract, now_epoch_ms=2_000, require_current_boot=False
    )

    payload = dataclasses.asdict(readiness)
    payload.pop("attempt_journal_root_inode")
    with pytest.raises(FormalEnforcementUnavailable, match="unknown or missing"):
        decode_supervisor_attestation(
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )


def test_attempt_journal_chain_covers_completed_and_failed_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, readiness, launch, receipt, _ledger_entry = _signed_stage_fixtures()
    observation = _h("formal-observation")
    first = SupervisorAttemptJournalEntry(
        schema="pok-supervisor-attempt-journal-entry-v1",
        contract_digest=contract.digest(),
        readiness_attestation_digest=readiness.payload_digest(),
        control_session_digest=launch.control_session_digest,
        attempt_journal_scope_digest=launch.attempt_journal_scope_digest,
        attempt_sequence=launch.attempt_sequence,
        previous_attempt_entry_digest=launch.previous_attempt_entry_digest,
        launch_authorization_digest=launch.payload_digest(),
        leg_plan_digest=launch.leg_plan_digest,
        leg_run_id=launch.leg_run_id,
        terminal_state="completed",
        supervisor_leg_receipt_digest=receipt.payload_digest(),
        capture_session_digest=receipt.capture_session_digest,
        receipt_consumption_key=receipt.receipt_consumption_key,
        cleanup_receipt_digest=receipt.cleanup_receipt_digest,
        raw_wire_digest=receipt.raw_wire_digest,
        wire_semantic_digest=receipt.wire_semantic_digest,
        replay_digest=receipt.replay_digest,
        replay_verification_digest=observation,
        issued_epoch_ms=71_000,
        signature_hex="33" * 64,
    )
    first.validate(contract, launch)
    second_launch = dataclasses.replace(
        launch,
        request_nonce="retry-" + "r" * 58,
        attempt_sequence=2,
        previous_attempt_entry_digest=first.payload_digest(),
        leg_run_id=_h("leg-run-retry"),
        issued_epoch_ms=3_000,
        signature_hex="44" * 64,
    )
    second_launch.validate(contract, readiness)
    second = SupervisorAttemptJournalEntry(
        schema="pok-supervisor-attempt-journal-entry-v1",
        contract_digest=contract.digest(),
        readiness_attestation_digest=readiness.payload_digest(),
        control_session_digest=second_launch.control_session_digest,
        attempt_journal_scope_digest=second_launch.attempt_journal_scope_digest,
        attempt_sequence=second_launch.attempt_sequence,
        previous_attempt_entry_digest=second_launch.previous_attempt_entry_digest,
        launch_authorization_digest=second_launch.payload_digest(),
        leg_plan_digest=second_launch.leg_plan_digest,
        leg_run_id=second_launch.leg_run_id,
        terminal_state="launch_failed",
        supervisor_leg_receipt_digest=None,
        capture_session_digest=None,
        receipt_consumption_key=None,
        cleanup_receipt_digest=None,
        raw_wire_digest=None,
        wire_semantic_digest=None,
        replay_digest=None,
        replay_verification_digest=None,
        issued_epoch_ms=72_000,
        signature_hex="55" * 64,
    )
    second.validate(contract, second_launch)
    entries = (first, second)
    chain = canonical_digest(
        {
            "attempt_journal_scope_digest": launch.attempt_journal_scope_digest,
            "entry_payload_digests": tuple(item.payload_digest() for item in entries),
            "schema": "pok-supervisor-attempt-journal-chain-v1",
        }
    )
    seal = SupervisorAttemptJournalSeal(
        schema="pok-supervisor-attempt-journal-seal-v1",
        contract_digest=contract.digest(),
        attempt_journal_scope_digest=launch.attempt_journal_scope_digest,
        first_attempt_sequence=1,
        last_attempt_sequence=2,
        entry_count=2,
        first_previous_entry_digest="0" * 64,
        head_entry_digest=second.payload_digest(),
        ordered_entry_chain_digest=chain,
        closed=True,
        issued_epoch_ms=73_000,
        signature_hex="66" * 64,
    )
    seal.validate(
        contract, entries, expected_scope_digest=launch.attempt_journal_scope_digest
    )
    assert decode_supervisor_attempt_journal_entry(_json_bytes(first)) == first
    assert decode_supervisor_attempt_journal_seal(_json_bytes(seal)) == seal
    monkeypatch.setattr(
        resource_module,
        "_verify_signed_supervisor_attempt_journal_material",
        lambda **_: contract,
    )
    capability = verify_signed_supervisor_attempt_journal(
        readiness_by_digest={readiness.payload_digest(): readiness},
        launch_authorizations=(launch, second_launch),
        entries=entries,
        seal=seal,
        expected_scope_digest=launch.attempt_journal_scope_digest,
    )
    projection = capability.projection_payload()
    assert projection["entry_digests"] == tuple(
        item.payload_digest() for item in entries
    )
    assert projection["head_entry_digest"] == second.payload_digest()
    copied = dataclasses.replace(capability)
    with pytest.raises(FormalEnforcementUnavailable, match="copied"):
        copied._assert_authorized()
    with pytest.raises(ValueError, match="previous-entry chain"):
        broken = dataclasses.replace(
            second, previous_attempt_entry_digest=_h("omitted-first-row")
        )
        seal.validate(
            contract,
            (first, broken),
            expected_scope_digest=launch.attempt_journal_scope_digest,
        )


def test_current_host_has_no_installed_formal_supervisor_authority() -> None:
    probe = probe_trusted_supervisor()
    assert probe.formal_available is False
    assert probe.contract_path == "/etc/pok/formal-resource-supervisor-v1.json"
    assert probe.reasons


def test_gpu_visibility_without_enforceable_vram_backend_is_rejected() -> None:
    _, profile = _profile()
    gpu = dataclasses.replace(
        profile,
        gpu_devices_by_connection=(("0",), ("1",)),
        vram_limit_bytes_per_connection=1024,
    )
    with pytest.raises(FormalEnforcementUnavailable, match="VRAM"):
        gpu.validate()
