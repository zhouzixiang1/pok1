from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from bot_artifact import canonical_digest, hash_path
from official_platform_harness import OfficialPlatformConfig
from official_certification import (
    _log_target_reached,
    _run_certification_impl,
    _run_certification_with_runner_for_test,
    STATUS_COMPLIANCE_PASS,
    STATUS_CERTIFIED,
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
    STATUS_SMOKE_PASS,
    STATUS_UNCERTIFIED,
    TEST_ONLY_RUNNER_PROVENANCE,
    build_spec,
    cache_key,
    certification_identity,
    certificate_validation,
    official_feedback_summary,
    official_certification_profile_projection,
    official_full_certified,
    official_compliance_verdict,
    official_failure_blocks_parent,
    official_opponent_eligibility,
    publish_certificate_attestation,
    record_local_pass,
    report_validation_issues,
    report_valid_for_spec,
    run_certification,
    read_status,
    select_official_opponent,
    spec_record,
    stable_official_opponent_selection,
    write_status,
)
from official_job_envelope import build_job_envelope


@pytest.fixture(scope="session")
def _official_test_signing_material(tmp_path_factory):
    import official_certificate_signing

    root = tmp_path_factory.mktemp("official-signing")
    key = root / "key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    pending = deepcopy(official_certificate_signing.load_signer_trust_policy())
    pending["current_signer"] = {
        "epoch": pending["current_epoch"],
        "state": "rotation-required",
        "key_fingerprint": None,
        "public_key_sha256": None,
    }
    pending["policy_digest"] = official_certificate_signing._policy_digest(pending)
    pending_path = root / "pending_signer_policy.json"
    pending_path.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
    policy_payload, allowed_payload = (
        official_certificate_signing.build_signer_rotation_material(
            Path(str(key) + ".pub"), trust_policy=pending_path
        )
    )
    allowed = root / "allowed_signers"
    allowed.write_text(allowed_payload, encoding="utf-8")
    policy = root / "signer_policy.json"
    policy.write_text(json.dumps(policy_payload, indent=2) + "\n", encoding="utf-8")
    return key, allowed, policy


@pytest.fixture(autouse=True)
def _disable_live_official_llm(monkeypatch, tmp_path, _official_test_signing_material):
    import official_certificate_signing

    key, allowed, policy = _official_test_signing_material
    monkeypatch.setenv("POK_OFFICIAL_LLM_ANALYSIS", "0")
    monkeypatch.setenv("POK_OFFICIAL_EVIDENCE_STORE", str(tmp_path / "evidence-store"))
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(key))
    monkeypatch.setattr(official_certificate_signing, "DEFAULT_ALLOWED_SIGNERS", allowed)
    monkeypatch.setattr(official_certificate_signing, "DEFAULT_TRUST_POLICY", policy)


class FakeResult:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


def test_formal_certification_requires_the_final_settlement():
    receipt = {
        "log_summary": {
            "hands_started_min": 70,
            "settlements_min": 69,
        }
    }
    assert _log_target_reached(receipt, 70) is False
    receipt["log_summary"]["settlements_min"] = 70
    assert _log_target_reached(receipt, 70) is True


def _run_official_certificate_fixture(
    spec,
    *,
    config: OfficialPlatformConfig,
    runner,
    opponent_selection: dict,
):
    """Build structurally complete, explicitly non-authoritative test evidence."""
    identity = certification_identity(
        spec,
        config,
        runner_provenance=TEST_ONLY_RUNNER_PROVENANCE,
        test_only=True,
    )
    stable_selection = stable_official_opponent_selection(opponent_selection)
    suite_dir = config.results_dir / "pytest-official-suite"
    request = {
        "job_id": "1" * 64,
        "request_digest": "2" * 64,
        "manager_sha256": "3" * 64,
        "spec": spec_record(spec),
        "identity": identity,
        "opponent_selection": stable_selection,
        "source_v": None,
    }
    envelope = build_job_envelope(
        request,
        attempt=1,
        attempt_nonce="4" * 64,
        suite_dir=suite_dir,
    )

    return _run_certification_with_runner_for_test(
        spec,
        config=config,
        force=True,
        runner=runner,
        opponent_selection=opponent_selection,
        job_envelope=envelope,
    )


def _bot(path: Path, body: str = "def act():\n    return 0\n") -> Path:
    path.mkdir(parents=True)
    (path / "main.py").write_text(body, encoding="utf-8")
    (path / "national_bot.py").write_text("import socket\n# raise fold call check allin sock.recv _split_messages\n", encoding="utf-8")
    return path


def _structural_quality_admission(candidate: Path) -> dict:
    """Build a schema-valid current-epoch-shaped receipt for test-only flows."""

    from national_native import NATIONAL_DECISION_RUNTIME_VERSION
    from national_runtime_authority import current_system_native_runtime_identity
    from national_runtime_probe import (
        RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        RUNTIME_PROBE_IDENTITY_DIGEST,
        RUNTIME_PROBE_LIMITS_DIGEST,
        RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION,
        RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT,
        RUNTIME_PROBE_SCENARIO_DIGEST,
        RUNTIME_PROBE_SCHEMA_VERSION,
        runtime_probe_native_template_evidence,
    )
    from official_platform_harness import FORMAL_QUALITY_ADMISSION_SCHEMA_VERSION

    runtime_evidence = runtime_probe_native_template_evidence()

    payload = {
        "schema_version": FORMAL_QUALITY_ADMISSION_SCHEMA_VERSION,
        "kind": "official-formal-quality-admission",
        "candidate_path": str(candidate.resolve()),
        "candidate_hash": hash_path(candidate),
        "checkpoint": {
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": "pytest-certification-workflow",
            "next_v": int(candidate.name.removeprefix("national_v") or 0),
            "source_v": 142,
        },
        "quality_gate_digest": "1" * 64,
        "capability_digest": "2" * 64,
        "dynamic_probe_digest": "3" * 64,
        "runtime_contract_ledger_digest": "4" * 64,
        "runtime_probe_identity": {
            "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
            "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
            "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
            "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
            "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
            "managed_isolation_digest": "8" * 64,
            "repeatability_schema_version": (
                RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION
            ),
            "repeatability_view_contract": (
                RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT
            ),
            "repeatability_evidence_digest": "9" * 64,
            **runtime_evidence,
        },
        "system_runtime_identity": current_system_native_runtime_identity(),
        "system_decision_runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
    }
    return {**payload, "admission_digest": canonical_digest(payload)}


def test_test_runner_envelope_preserves_normal_full_quality_admission(
    tmp_path,
    monkeypatch,
):
    """The pytest-only runner must exercise the production envelope shape."""

    import official_certification as certification

    candidate = _bot(tmp_path / "national_v143")
    opponent = _bot(tmp_path / "national_v142")
    admission = _structural_quality_admission(candidate)
    spec = build_spec(
        "full",
        candidate,
        opponent=opponent,
        quality_admission=admission,
    )
    captured: dict[str, object] = {}

    def fake_impl(_spec, **kwargs):
        captured.update(kwargs)
        return {"status": "captured"}

    monkeypatch.setattr(certification, "_run_certification_impl", fake_impl)

    result = certification._run_certification_with_runner_for_test(
        spec,
        config=_config(tmp_path),
        runner=lambda *_args, **_kwargs: {"report": {}},
    )
    envelope = captured["job_envelope"]

    assert result == {"status": "captured"}
    assert isinstance(envelope, dict)
    assert envelope["candidate_path"] == str(candidate.resolve())
    assert envelope["quality_admission"] == admission
    assert envelope["quality_admission_digest"] == canonical_digest(admission)
    assert envelope["quality_admission_required"] is True


def _config(tmp_path: Path) -> OfficialPlatformConfig:
    exe = tmp_path / "platform.exe"
    exe.write_bytes(b"fake-exe")
    wine = tmp_path / "wine"
    wine.mkdir()
    return OfficialPlatformConfig(
        exe_path=exe,
        wineprefix=wine,
        results_dir=tmp_path / "official",
        lock_path=tmp_path / "official.lock",
    )


def _selection(candidate: Path, opponent: Path) -> dict:
    artifact_hash = hash_path(opponent)
    receipt_payload = {
        "schema_version": 1,
        "kind": "official_full_certificate",
        "role": "official_opponent",
        "bot": opponent.name,
        "artifact_hash": artifact_hash,
        "policy_id": "official-full-v5",
        "certificate_digest": "a" * 64,
    }
    return {
        "selected": True,
        "candidate": str(candidate),
        "opponent": {
            "bot": opponent.name,
            "path": str(opponent.resolve()),
            "artifact_hash": artifact_hash,
            "eligible": True,
            "reason": "official_certified",
            "eligibility_receipt": {
                **receipt_payload,
                "receipt_digest": canonical_digest(receipt_payload),
            },
        },
        "considered": [],
    }


def _job_envelope(spec, suite_dir: Path, selection: dict) -> dict:
    from official_certification import certification_identity
    from official_job_envelope import build_job_envelope

    identity = certification_identity(spec)
    return build_job_envelope(
        {
            "job_id": "1" * 64,
            "request_digest": "2" * 64,
            "manager_sha256": "3" * 64,
            "identity": identity,
            "opponent_selection": selection,
            "source_v": None,
        },
        attempt=1,
        attempt_nonce="4" * 64,
        suite_dir=suite_dir,
    )


def _report(*, target_hands: int, rounds: int, passed=True, issues=None, thp_hands=None):
    receipts = []
    for idx in range(rounds):
        receipts.append({
            "passed": passed,
            "issues": issues or [],
            "target_hands": target_hands,
            "artifacts": {
                "thp_summaries": [{"hand_records": target_hands if thp_hands is None else thp_hands}],
            },
        })
    return {
        "passed": passed,
        "issues": issues or [],
        "report": {
            "summary": {"suite_dir": "/tmp/suite", "rounds_run": rounds},
            "rounds": receipts,
        },
    }


def _full_report(
    tmp_path: Path,
    candidate: Path,
    opponent: Path,
    *,
    passed: bool = True,
    issues=None,
    thp_hands: int = 70,
):
    from dataclasses import asdict

    from managed_bot_executor import IsolationIdentity
    import official_platform_harness as harness
    from official_execution_profile import (
        execution_profile_identity,
        load_execution_profile,
    )

    suite = tmp_path / "full-suite"
    execution_profile = {
        "ok": True,
        **execution_profile_identity(),
        "issues": [],
        "observed_tools": {},
    }
    profile = load_execution_profile()
    managed_identity = profile["managed_executor"]
    seccomp = managed_identity["seccomp"]
    isolation = asdict(IsolationIdentity(
        policy_sha256=seccomp["policy_sha256"],
        bpf_sha256=seccomp["bpf_sha256"],
        bpf_size=seccomp["bpf_size"],
    ))
    source_sha256 = managed_identity["source"]["sha256"]

    def causal_wire_fixture(round_dir, bot_a_name, bot_b_name):
        from official_wire_probe import WireEventRecorder, replay_events

        recorder = WireEventRecorder(round_dir / "wire_events.jsonl")

        def record_action(conn, direction, message):
            if direction == "bot_to_server":
                raw_action = recorder.record(
                    conn=conn,
                    direction=direction,
                    raw=message.encode(),
                    messages=[],
                    remaining=message,
                )
                recorder.record(
                    conn=conn,
                    direction=direction,
                    raw=b"",
                    messages=[message],
                    remaining="",
                    event_type="idle_flush",
                    observation_seq=raw_action["observation_seq"],
                    observation_t=raw_action["observation_t"],
                    deferred_parser_mode="client_action",
                )
                return
            recorder.record(
                conn=conn,
                direction=direction,
                raw=message.encode(),
                messages=[message],
                remaining="",
            )

        try:
            for conn, name in (("A", bot_a_name), ("B", bot_b_name)):
                recorder.record(
                    conn=conn,
                    direction="server_to_bot",
                    raw=b"name",
                    messages=["name"],
                    remaining="",
                )
                raw_name = recorder.record(
                    conn=conn,
                    direction="bot_to_server",
                    raw=name.encode("utf-8"),
                    messages=[],
                    remaining=name,
                )
                recorder.record(
                    conn=conn,
                    direction="bot_to_server",
                    raw=b"",
                    messages=[name],
                    remaining="",
                    event_type="idle_flush",
                    observation_seq=raw_name["observation_seq"],
                    observation_t=raw_name["observation_t"],
                    deferred_parser_mode="client_name",
                )
            for hand in range(1, 71):
                a_small = hand % 2 == 1
                recorder.record(
                    conn="A",
                    direction="server_to_bot",
                    raw=(
                        f"preflop|{'SMALLBLIND' if a_small else 'BIGBLIND'}|"
                        "<0,0><1,1>"
                    ).encode(),
                    messages=[
                        f"preflop|{'SMALLBLIND' if a_small else 'BIGBLIND'}|"
                        "<0,0><1,1>"
                    ],
                    remaining="",
                )
                recorder.record(
                    conn="B",
                    direction="server_to_bot",
                    raw=(
                        f"preflop|{'BIGBLIND' if a_small else 'SMALLBLIND'}|"
                        "<2,2><3,3>"
                    ).encode(),
                    messages=[
                        f"preflop|{'BIGBLIND' if a_small else 'SMALLBLIND'}|"
                        "<2,2><3,3>"
                    ],
                    remaining="",
                )
                if a_small:
                    for conn, direction in (
                        ("A", "bot_to_server"),
                        ("B", "server_to_bot"),
                    ):
                        record_action(conn, direction, "call")
                    for conn, direction in (
                        ("B", "bot_to_server"),
                        ("A", "server_to_bot"),
                    ):
                        record_action(conn, direction, "fold")
                else:
                    for conn, direction in (
                        ("B", "bot_to_server"),
                        ("A", "server_to_bot"),
                    ):
                        record_action(conn, direction, "fold")
                if hand <= 69:
                    for conn, amount in (("A", 50), ("B", -50)):
                        message = f"earnChips {amount}"
                        raw_settlement = recorder.record(
                            conn=conn,
                            direction="server_to_bot",
                            raw=message.encode(),
                            messages=[],
                            remaining=message,
                        )
                        recorder.record(
                            conn=conn,
                            direction="server_to_bot",
                            raw=b"",
                            messages=[message],
                            remaining="",
                            event_type="idle_flush",
                            observation_seq=raw_settlement["observation_seq"],
                            observation_t=raw_settlement["observation_t"],
                            deferred_parser_mode="server",
                        )
            recorder.record(
                conn="*",
                direction="probe_lifecycle",
                raw=b"",
                messages=[],
                remaining="",
                event_type="capture_finalized",
            )
            summary = replay_events(recorder.events, finalized=True)
        finally:
            recorder.close()
        assert summary["issues"] == [], json.dumps(summary["issues"])
        (round_dir / "replay_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False),
            encoding="utf-8",
        )
        return summary

    def isolation_row(connection, launch, artifact_hash):
        return {
            "connection": connection,
            "name": launch["name"],
            "role": launch["role"],
            "instance_id": launch["instance_id"],
            "seat": launch["seat"],
            "path": launch["path"],
            "artifact_hash": artifact_hash,
            "endpoint_lease": {"consumed": True, "closed": True},
            "execution_profile": execution_profile_identity(),
            "managed_executor_source_sha256": source_sha256,
            "isolation": isolation,
        }
    receipts = []
    for kind, count in (("self_play", 5), ("opponent", 3)):
        for round_index in range(1, count + 1):
            round_dir = suite / f"{kind}_{round_index:02d}"
            round_dir.mkdir(parents=True, exist_ok=True)
            wire_events = round_dir / "wire_events.jsonl"
            replay_summary = round_dir / "replay_summary.json"
            artifact_paths = {}
            for artifact_name in (
                "receipt",
                "platform_log",
                "bot_a_log",
                "bot_b_log",
                "bot_a_stdout",
                "bot_a_stderr",
                "bot_b_stdout",
                "bot_b_stderr",
            ):
                suffix = ".json" if artifact_name == "receipt" else ".log"
                artifact_path = round_dir / f"{artifact_name}{suffix}"
                artifact_path.write_text("{}\n", encoding="utf-8")
                artifact_paths[artifact_name] = str(artifact_path)
            bot_b_path = candidate if kind == "self_play" else opponent
            bot_a_name = "BotA" if kind == "self_play" else "Candidate"
            bot_b_name = "BotB" if kind == "self_play" else "Opponent"
            thp_path = round_dir / "THP-match.txt"
            screenshot_path = round_dir / "platform.png"
            thp_path.write_text(
                "".join(
                    f"STATE:{hand_no}:f:AhKh|QsQd:50|-50:{bot_a_name}|{bot_b_name};"
                    for hand_no in range(thp_hands)
                )
                + (
                    f"{{[THP][{bot_a_name}][{bot_b_name}]"
                    f"[{bot_a_name}赢得{thp_hands * 50}个筹码]"
                    "[2026-07-11 17:22 合肥][2018 CCGC]}"
                ),
                encoding="gb2312",
            )
            screenshot_path.write_bytes(b"fake-png")
            thp_bytes = thp_path.read_bytes()
            thp_sha256 = hashlib.sha256(thp_bytes).hexdigest()
            thp_summary = {
                "path": str(thp_path),
                "exists": True,
                "hand_records": thp_hands,
                "bytes": len(thp_bytes),
                "sha256": thp_sha256,
            }
            bot_a_hash = hash_path(candidate)
            bot_b_hash = hash_path(bot_b_path)
            bot_a_launch = {
                "path": str(candidate),
                "name": bot_a_name,
                "role": "candidate",
                "instance_id": "candidate_a" if kind == "self_play" else "candidate",
                "seat": "upper",
            }
            bot_b_launch = {
                "path": str(bot_b_path),
                "name": bot_b_name,
                "role": "candidate" if kind == "self_play" else "opponent",
                "instance_id": "candidate_b" if kind == "self_play" else "opponent",
                "seat": "lower",
            }
            wire_summary = causal_wire_fixture(
                round_dir,
                bot_a_name,
                bot_b_name,
            )
            receipt = {
                "round_id": f"{kind}_{round_index:02d}",
                "round_kind": kind,
                "round_index": round_index,
                "passed": passed,
                "issues": issues or [],
                "target_hands": 70,
                "bot_a": bot_a_launch,
                "bot_b": bot_b_launch,
                "environment": {"execution_profile": execution_profile},
                "formal_execution": {
                    "sandboxed": True,
                    **execution_profile_identity(),
                    "bot_a_artifact_hash": bot_a_hash,
                    "bot_b_artifact_hash": bot_b_hash,
                    "bot_isolation": {
                        "schema_version": 1,
                        "authority": "central-managed-executor-process-observation",
                        "connections": {
                            "A": isolation_row("A", bot_a_launch, bot_a_hash),
                            "B": isolation_row("B", bot_b_launch, bot_b_hash),
                        },
                    },
                },
                "wire_probe": {
                    "enabled": True,
                    "issues": [],
                    "causal_order_schema_version": 1,
                    "finalized_replay_required": True,
                },
                "log_summary": {
                    "hands_started_min": 70,
                    "settlements_min": 69,
                    "issues": [],
                },
                "wire_replay_summary": wire_summary,
                "artifacts": {
                    "round_dir": str(round_dir),
                    **artifact_paths,
                    "wire_events": str(wire_events),
                    "replay_summary": str(replay_summary),
                    "thp_files": [str(thp_path)],
                    "screenshots": [str(screenshot_path)],
                    "thp_summaries": [thp_summary],
                    "canonical_thp": {
                        "path": str(thp_path),
                        "sha256": thp_sha256,
                        "bytes": len(thp_bytes),
                        "hand_records": thp_hands,
                        "duplicate_paths": [],
                    },
                },
            }
            if thp_hands == 70:
                observation, observation_issues = harness._terminal_thp_observation(
                    round_dir,
                    before={},
                    expected_hands=70,
                    expected_names=(bot_a_name, bot_b_name),
                    wire_summary=wire_summary,
                )
                assert observation_issues == []
                assert observation is not None
                receipt["completion_evidence"] = harness._build_terminal_completion_evidence(
                    receipt,
                    observation,
                    receipt["artifacts"]["canonical_thp"],
                    target_hands=70,
                )
            receipts.append(receipt)
    return {
        "passed": passed,
        "issues": issues or [],
        "report": {
            "summary": {"suite_dir": str(suite), "rounds_run": 8},
            "rounds": receipts,
            "formal_execution": execution_profile,
        },
    }


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        ("missing_b", "official_formal_bot_isolation_connections_mismatch"),
        ("policy", "official_formal_bot_isolation_A_policy_mismatch"),
        ("source", "official_formal_bot_isolation_A_managed_executor_source_sha256_mismatch"),
        ("lease", "official_formal_bot_isolation_A_endpoint_lease_mismatch"),
        ("instance", "official_formal_bot_isolation_instance_ids_not_unique"),
    ],
)
def test_formal_execution_validator_binds_both_managed_processes(
    tmp_path, mutation, expected_issue
):
    from official_certification import _formal_execution_issues

    candidate = _bot(tmp_path / "national_v200")
    opponent = _bot(tmp_path / "national_v199")
    receipt = _full_report(tmp_path, candidate, opponent)["report"]["rounds"][0]
    assert _formal_execution_issues(receipt) == []

    tampered = deepcopy(receipt)
    connections = tampered["formal_execution"]["bot_isolation"]["connections"]
    if mutation == "missing_b":
        connections.pop("B")
    elif mutation == "policy":
        connections["A"]["isolation"]["bpf_sha256"] = "f" * 64
    elif mutation == "source":
        connections["A"]["managed_executor_source_sha256"] = "f" * 64
    elif mutation == "lease":
        connections["A"]["endpoint_lease"]["closed"] = False
    else:
        connections["B"]["instance_id"] = connections["A"]["instance_id"]

    assert expected_issue in _formal_execution_issues(tampered)


def test_stale_smoke_status_cannot_downgrade_newer_full_certificate(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "bots" / "national_v200")
    opponent = _bot(tmp_path / "bots" / "national_v199")
    config = _config(tmp_path)
    smoke = build_spec("smoke", candidate, opponent=opponent)
    identity = certification_identity(smoke, config)
    certified = write_status(
        candidate,
        STATUS_CERTIFIED,
        mode="full",
        policy_id="official-full-v5",
        certificate_digest="full-certificate",
        certification_identity=identity,
        issues=[],
    )
    assert certified["status"] == STATUS_CERTIFIED

    stale = write_status(
        smoke.candidate,
        STATUS_SMOKE_PASS,
        mode="smoke",
        policy_id=smoke.policy_id,
        certification_identity=identity,
        issues=[],
    )
    assert stale["status"] == STATUS_CERTIFIED
    current = read_status(candidate)
    assert current["status"] == STATUS_INCONCLUSIVE
    assert current["mode"] == "full"
    assert current["certificate_digest"] == "full-certificate"
    assert "official_verdict_ledger_missing" in current["issues"]


def test_stale_full_result_cannot_overwrite_newer_certificate_but_new_run_can(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "bots" / "national_v200")
    identity = {"candidate_hash": hash_path(candidate)}
    newer = write_status(
        candidate,
        STATUS_CERTIFIED,
        mode="full",
        policy_id="official-full-v5",
        certificate_digest="newer-certificate",
        certification_identity=identity,
        request_started_ns=200,
        issues=[],
    )

    stale = write_status(
        candidate,
        STATUS_INCONCLUSIVE,
        mode="full",
        policy_id="official-full-v5",
        certification_identity=identity,
        request_started_ns=100,
        issues=["stale harness result"],
    )
    later = write_status(
        candidate,
        STATUS_FAILED,
        mode="full",
        policy_id="official-full-v5",
        certification_identity=identity,
        request_started_ns=300,
        issues=["new protocol failure"],
    )

    assert newer["status"] == STATUS_CERTIFIED
    assert stale["status"] == STATUS_CERTIFIED
    assert stale["certificate_digest"] == "newer-certificate"
    assert later["status"] == STATUS_FAILED
    assert later["issues"] == ["new protocol failure"]


def _smoke_report_without_thp(*, target_hands: int = 10, rounds: int = 2):
    receipts = []
    for _idx in range(rounds):
        receipts.append({
            "passed": True,
            "issues": [],
            "target_hands": target_hands,
            "log_summary": {
                "hands_started_min": target_hands,
                "settlements_min": target_hands,
            },
            "artifacts": {
                "thp_summaries": [],
            },
        })
    return {
        "passed": True,
        "issues": [],
        "report": {
            "summary": {"suite_dir": "/tmp/suite", "rounds_run": rounds},
            "rounds": receipts,
        },
    }


def _write_jsonl(path: Path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _smoke_report_with_wire_replay_blocker(tmp_path: Path):
    suite = tmp_path / "suite"
    bad_round = suite / "self_play_01"
    good_round = suite / "opponent_01"
    bad_round.mkdir(parents=True)
    good_round.mkdir(parents=True)
    wire_events = bad_round / "wire_events.jsonl"
    _write_jsonl(
        wire_events,
        [
            {
                "t": 1.0,
                "dt": 0.1,
                "conn": "A",
                "direction": "server_to_bot",
                "messages": ["preflop|SMALLBLIND|<0,3><1,4>"],
            },
            {
                "t": 1.1,
                "dt": 0.2,
                "conn": "A",
                "direction": "bot_to_server",
                "messages": ["check"],
            },
        ],
    )
    receipts = [
        {
            "round_id": "self_play_01",
            "round_kind": "self_play",
            "passed": True,
            "issues": [],
            "target_hands": 10,
            "log_summary": {"hands_started_min": 10, "settlements_min": 10, "issues": []},
            "artifacts": {
                "round_dir": str(bad_round),
                "wire_events": str(wire_events),
                "thp_summaries": [],
            },
        },
        {
            "round_id": "opponent_01",
            "round_kind": "opponent",
            "passed": True,
            "issues": [],
            "target_hands": 10,
            "log_summary": {"hands_started_min": 10, "settlements_min": 10, "issues": []},
            "artifacts": {"round_dir": str(good_round), "thp_summaries": []},
        },
    ]
    return {
        "passed": True,
        "issues": [],
        "report": {
            "summary": {"suite_dir": str(suite), "rounds_run": 2},
            "rounds": receipts,
        },
    }


def test_cache_key_changes_when_inputs_change(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)

    spec = build_spec("smoke", candidate, opponent=opponent)
    first = cache_key(spec, cfg)
    (candidate / "main.py").write_text("def act():\n    return 1\n", encoding="utf-8")
    changed_candidate = cache_key(spec, cfg)
    changed_mode = cache_key(build_spec("full", candidate, opponent=opponent), cfg)

    assert first != changed_candidate
    assert changed_candidate != changed_mode


def test_cache_identity_binds_all_deterministic_platform_dependencies(
    tmp_path, monkeypatch
):
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    dependency = tmp_path / "official_platform_resource.py"
    dependency.write_text("LEASE_VERSION = 1\n", encoding="utf-8")
    monkeypatch.setattr(certification, "PLATFORM_RESOURCE_PATH", dependency)
    spec = build_spec("full", candidate, opponent=opponent)
    cfg = _config(tmp_path)

    first = cache_key(spec, cfg)
    dependency.write_text("LEASE_VERSION = 2\n", encoding="utf-8")
    second = cache_key(spec, cfg)

    assert first != second


def test_full_profile_cannot_be_downgraded_or_run_without_opponent(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")

    with pytest.raises(ValueError, match="profile is immutable"):
        build_spec("full", candidate, opponent=opponent, self_play_rounds=0)
    with pytest.raises(ValueError, match="profile is immutable"):
        build_spec("full", candidate, opponent=opponent, target_hands=1)
    with pytest.raises(ValueError, match="requires an opponent"):
        build_spec("full", candidate)


def test_first_strict_control_spec_is_limited_to_v143(tmp_path):
    candidate = _bot(tmp_path / "national_v144")
    opponent = _bot(tmp_path / "first_strict_control_v1")

    with pytest.raises(ValueError, match="only for national_v143"):
        build_spec(
            "full",
            candidate,
            opponent=opponent,
            bootstrap_control_id="first_strict_control_v1",
        )


def test_retired_bootstrap_root_spec_cannot_resume_as_normal_full_job(tmp_path):
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v143")
    opponent = _bot(tmp_path / "first_strict_control_v1")
    record = certification.spec_record(
        build_spec(
            "full",
            candidate,
            opponent=opponent,
            quality_admission=_structural_quality_admission(candidate),
        )
    )
    record["bootstrap_root_id"] = (
        "national-v141-official-full-v5-signed-ledger-root"
    )

    with pytest.raises(ValueError, match="retired signed-ledger bootstrap"):
        certification._spec_from_mapping(record)


def test_full_certification_checks_signer_before_starting_exe(tmp_path, monkeypatch):
    import official_certificate_signing

    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    monkeypatch.setattr(
        official_certificate_signing,
        "signing_environment_report",
        lambda: {"ok": False, "issues": ["trust root mismatch"]},
    )

    with pytest.raises(RuntimeError, match="signing_preflight_failed"):
        _run_certification_with_runner_for_test(
            spec,
            config=_config(tmp_path),
            runner=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("EXE must not start without a trusted signer")
            ),
        )


def test_production_full_preflight_rejects_missing_verdict_ledger_before_exe(
    tmp_path,
    monkeypatch,
):
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    identity = certification.certification_identity(spec)
    selection = _selection(candidate, opponent)
    monkeypatch.setattr(
        certification,
        "resolve_managed_certification_spec",
        lambda _spec, **_kwargs: (spec, selection),
    )
    monkeypatch.setattr(
        certification,
        "_PRODUCTION_CERTIFICATION_RUNNER",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("EXE must not start without verdict-ledger genesis")
        ),
    )

    with pytest.raises(RuntimeError, match="official_verdict_ledger_preflight_failed"):
        certification.run_identity_bound_certification_job(
            spec,
            expected_identity=identity,
            expected_opponent_selection=selection,
            suite_dir=tmp_path / "suite",
            job_envelope=_job_envelope(spec, tmp_path / "suite", selection),
        )


def test_identity_bound_job_rejects_live_opponent_reselection(tmp_path, monkeypatch):
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v1")
    original = _bot(tmp_path / "national_v2")
    replacement = _bot(tmp_path / "national_v3")
    spec = build_spec("full", candidate, opponent=original)
    identity = certification.certification_identity(spec)
    replacement_spec = build_spec("full", candidate, opponent=replacement)
    monkeypatch.setattr(
        certification,
        "resolve_managed_certification_spec",
        lambda _spec, **_kwargs: (
            replacement_spec,
            _selection(candidate, replacement),
        ),
    )
    monkeypatch.setattr(
        certification,
        "run_official_acceptance_sync",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("EXE must not start")),
    )

    with pytest.raises(RuntimeError, match="opponent_selection_changed"):
        certification.run_identity_bound_certification_job(
            spec,
            expected_identity=identity,
            expected_opponent_selection=_selection(candidate, original),
            suite_dir=tmp_path / "suite",
            job_envelope=_job_envelope(
                spec,
                tmp_path / "suite",
                _selection(candidate, original),
            ),
        )


def test_identity_bound_job_rejects_changed_opponent_authorization_receipt(tmp_path, monkeypatch):
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    identity = certification.certification_identity(spec)
    expected = _selection(candidate, opponent)
    live = json.loads(json.dumps(expected))
    payload = {
        key: value
        for key, value in live["opponent"]["eligibility_receipt"].items()
        if key != "receipt_digest"
    }
    payload["certificate_digest"] = "b" * 64
    live["opponent"]["eligibility_receipt"] = {
        **payload,
        "receipt_digest": canonical_digest(payload),
    }
    monkeypatch.setattr(
        certification,
        "resolve_managed_certification_spec",
        lambda _spec, **_kwargs: (spec, live),
    )
    monkeypatch.setattr(
        certification,
        "run_official_acceptance_sync",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("EXE must not start")),
    )

    with pytest.raises(RuntimeError, match="opponent_eligibility_receipt_changed"):
        certification.run_identity_bound_certification_job(
            spec,
            expected_identity=identity,
            expected_opponent_selection=expected,
            suite_dir=tmp_path / "suite",
            job_envelope=_job_envelope(spec, tmp_path / "suite", expected),
        )


def test_identity_bound_job_runs_exact_prevalidated_spec(tmp_path, monkeypatch):
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    identity = certification.certification_identity(spec)
    selection = _selection(candidate, opponent)
    resolve_calls = []

    def fake_resolve(_spec, **kwargs):
        resolve_calls.append(kwargs)
        return spec, selection

    monkeypatch.setattr(
        certification,
        "resolve_managed_certification_spec",
        fake_resolve,
    )
    seen = {}

    def fake_run(incoming, **kwargs):
        seen["spec"] = incoming
        seen.update(kwargs)
        return {"status": STATUS_CERTIFIED}

    monkeypatch.setattr(certification, "_run_certification_impl", fake_run)

    result = certification.run_identity_bound_certification_job(
        spec,
        expected_identity=identity,
        expected_opponent_selection=selection,
        suite_dir=tmp_path / "suite",
        job_envelope=_job_envelope(spec, tmp_path / "suite", selection),
        force=True,
    )

    assert result["status"] == STATUS_CERTIFIED
    assert seen["spec"] == spec
    assert seen["enforce_opponent_selection"] is False
    assert seen["opponent_selection"] == selection
    assert seen["force"] is True
    assert resolve_calls == [{"exact_opponent_only": True}]


def test_exact_managed_resolution_skips_unrelated_active_pool_discovery(
    tmp_path,
    monkeypatch,
):
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    selection = _selection(candidate, opponent)
    seen = {}

    def fake_select(incoming, active_bots=None, **kwargs):
        seen["candidate"] = incoming
        seen["active_bots"] = active_bots
        seen.update(kwargs)
        return selection

    monkeypatch.setattr(certification, "select_official_opponent", fake_select)

    resolved, live = certification.resolve_managed_certification_spec(
        spec,
        exact_opponent_only=True,
    )

    assert resolved == spec
    assert live == selection
    assert seen["candidate"] == spec.candidate
    assert seen["preferred"] == spec.opponent
    assert seen["active_bots"] == (spec.opponent,)
    assert seen["allow_bootstrap_grandfather"] is False


def test_identity_bound_job_preserves_exact_opponent_rejection_reason(
    tmp_path,
    monkeypatch,
):
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    identity = certification.certification_identity(spec)
    expected = _selection(candidate, opponent)
    rejected = {
        "selected": False,
        "reason": "no_official_eligible_opponent",
        "considered": [{
            "bot": opponent.name,
            "path": str(opponent.resolve()),
            "eligible": False,
            "reason": "lifecycle_ledger_unavailable",
        }],
    }
    monkeypatch.setattr(
        certification,
        "resolve_managed_certification_spec",
        lambda _spec, **_kwargs: (None, rejected),
    )
    monkeypatch.setattr(
        certification,
        "_run_certification_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("EXE must not start")
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="official_job_opponent_no_longer_eligible.*lifecycle_ledger_unavailable",
    ):
        certification.run_identity_bound_certification_job(
            spec,
            expected_identity=identity,
            expected_opponent_selection=expected,
            suite_dir=tmp_path / "suite",
            job_envelope=_job_envelope(spec, tmp_path / "suite", expected),
        )


def test_full_report_requires_round_identity_and_wire_artifacts(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    report = _full_report(tmp_path, candidate, opponent)
    report["report"]["rounds"][0]["round_kind"] = "opponent"
    report["report"]["rounds"][1].pop("wire_probe")

    issues = report_validation_issues(report, spec)

    assert any("round_kind_mismatch" in issue for issue in issues)
    assert any("full_wire_probe_missing_or_disabled" in issue for issue in issues)


def test_full_report_rejects_causal_wire_contract_downgrade(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    report = _full_report(tmp_path, candidate, opponent)
    probe = report["report"]["rounds"][0]["wire_probe"]
    probe.pop("causal_order_schema_version")
    probe.pop("finalized_replay_required")

    issues = report_validation_issues(report, spec)

    assert any(
        "full_wire_probe_causal_contract_missing_or_invalid" in issue
        for issue in issues
    )


def test_candidate_change_during_certification_is_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("compliance", candidate, opponent=opponent)

    def mutating_runner(*_args, **_kwargs):
        (candidate / "main.py").write_text("def act():\n    return -1\n", encoding="utf-8")
        return FakeResult(_report(target_hands=10, rounds=2))

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=mutating_runner,
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert "candidate_changed_during_official_certification" in result["issues"]


def test_smoke_receipt_cannot_satisfy_full_certification(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    smoke = build_spec("smoke", candidate, opponent=opponent)
    full = build_spec("full", candidate, opponent=opponent)
    payload = _report(target_hands=10, rounds=2)

    assert report_valid_for_spec(payload, smoke) is True
    assert report_valid_for_spec(payload, full) is False


def test_compliance_mode_requires_two_short_protocol_rounds(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    compliance = build_spec("compliance", candidate, opponent=opponent)

    assert compliance.self_play_rounds == 1
    assert compliance.opponent_rounds == 1
    assert compliance.target_hands == 10
    assert report_valid_for_spec(_report(target_hands=10, rounds=2), compliance) is True
    assert report_valid_for_spec(_report(target_hands=70, rounds=2), compliance) is False


def test_bad_receipts_are_not_valid_for_cache(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)

    assert report_valid_for_spec(_report(target_hands=70, rounds=8, issues=["illegal"]), spec) is False
    assert report_valid_for_spec(_report(target_hands=70, rounds=8, thp_hands=69), spec) is False


@pytest.mark.parametrize(
    "report",
    [
        "not-a-report",
        {"passed": True, "issues": [], "report": "not-a-mapping"},
        {"passed": True, "issues": [], "report": {"rounds": "not-a-list"}},
        {"passed": True, "issues": [], "report": {"rounds": [None]}},
        {
            "passed": True,
            "issues": [],
            "report": {"rounds": [{"passed": True, "artifacts": "not-a-mapping"}]},
        },
    ],
)
def test_report_validation_fails_closed_on_malformed_nested_payload(tmp_path, report):
    candidate = _bot(tmp_path / "candidate")
    opponent = _bot(tmp_path / "opponent")
    spec = build_spec("smoke", candidate, opponent=opponent)

    issues = report_validation_issues(report, spec)

    assert issues
    assert report_valid_for_spec(report, spec) is False


def test_unbound_silent_timeout_string_cannot_block_parent_selection():
    status = {
        "status": STATUS_FAILED,
        "issues": ["opponent_1: official_log_silent_timeout_gap: bot_a max_gap_sec=62 max_decision_sec=0.050"],
    }

    verdict = official_compliance_verdict(status)

    assert verdict["blocking"] is False
    assert verdict["classification"] == "inconclusive"
    assert official_failure_blocks_parent(status) is False
    assert "official_deterministic_status_receipt_missing" in verdict["deterministic_receipt_issues"]


def test_short_smoke_can_use_log_progress_when_thp_is_absent(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    smoke = build_spec("smoke", candidate, opponent=opponent)
    full = build_spec("full", candidate, opponent=opponent)
    payload = _smoke_report_without_thp(target_hands=10, rounds=2)

    assert report_valid_for_spec(payload, smoke) is True
    assert report_valid_for_spec(payload, full) is False
    assert any("round_count_mismatch" in issue for issue in report_validation_issues(payload, full))


def test_full_certification_requires_thp_records(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    payload = _smoke_report_without_thp(target_hands=70, rounds=8)

    assert report_valid_for_spec(payload, spec) is False
    assert any("thp_incomplete_for_full_certification" in issue for issue in report_validation_issues(payload, spec))


def test_full_certification_rejects_thp_overrun(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    payload = _full_report(tmp_path, candidate, opponent, thp_hands=71)

    issues = report_validation_issues(payload, spec)

    assert report_valid_for_spec(payload, spec) is False
    assert any("thp_hand_count_mismatch_for_full_certification" in issue for issue in issues)


def test_full_v5_rejects_paired_70_wire_settlement_bypass(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    payload = _full_report(tmp_path, candidate, opponent)
    receipt = payload["report"]["rounds"][0]
    receipt["log_summary"]["settlements_min"] = 70
    receipt["wire_replay_summary"]["settlements_min"] = 70
    for seat, amount in (("A", 50), ("B", -50)):
        row = receipt["wire_replay_summary"]["seats"][seat]
        row["settlements"] = 70
        row["settlement_records"].append({"hand": 70, "amount": amount})

    issues = report_validation_issues(payload, spec)

    assert report_valid_for_spec(payload, spec) is False
    assert any("official_full_v5_natural_terminal_boundary_required" in issue for issue in issues)


def test_full_certification_accepts_exe_terminal_thp_completion_proof(tmp_path):
    import official_platform_harness as harness

    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    payload = _full_report(tmp_path, candidate, opponent)
    receipt = payload["report"]["rounds"][0]
    receipt["bot_a"]["name"] = "BotA"
    receipt["bot_b"]["name"] = "BotB"
    receipt["log_summary"]["settlements_min"] = 69
    receipt["wire_replay_summary"] = {
        "hands_started_min": 70,
        "settlements_min": 69,
        "pending_expected_actions": [],
        "seats": {
            "A": {
                "name": "BotA",
                "hands_started": 70,
                "settlements": 69,
                "settlement_records": [
                    {"hand": hand, "amount": 50}
                    for hand in range(1, 70)
                ],
                "pending_expected_action": False,
            },
            "B": {
                "name": "BotB",
                "hands_started": 70,
                "settlements": 69,
                "settlement_records": [
                    {"hand": hand, "amount": -50}
                    for hand in range(1, 70)
                ],
                "pending_expected_action": False,
            },
        },
    }
    original_thp_path = Path(receipt["artifacts"]["canonical_thp"]["path"])
    thp_path = original_thp_path.with_name("THP-terminal.txt")
    original_thp_path.unlink()
    thp_path.write_text(
        "".join(
            f"STATE:{index}:f:AhKh|QsQd:50|-50:BotA|BotB;"
            for index in range(70)
        )
        + "{[THP][BotA][BotB][BotA赢得3500个筹码][2026-07-11 17:22 合肥][2018 CCGC]}",
        encoding="gb2312",
    )
    summaries = harness._summarize_thp_files([str(thp_path)])
    canonical, canonical_issues = harness._canonical_thp_evidence(
        summaries,
        expected_hands=70,
    )
    assert canonical_issues == []
    receipt["artifacts"]["thp_summaries"] = summaries
    receipt["artifacts"]["thp_files"] = [str(thp_path)]
    receipt["artifacts"]["canonical_thp"] = canonical
    observation, observation_issues = harness._terminal_thp_observation(
        thp_path.parent,
        before={},
        expected_hands=70,
        expected_names=("BotA", "BotB"),
        wire_summary=receipt["wire_replay_summary"],
    )
    assert observation_issues == []
    receipt["completion_evidence"] = harness._build_terminal_completion_evidence(
        receipt,
        observation,
        canonical,
        target_hands=70,
    )

    assert report_validation_issues(payload, spec) == []
    assert report_valid_for_spec(payload, spec) is True
    from official_evidence import build_official_evidence_bundle

    evidence = build_official_evidence_bundle(payload)
    assert evidence["rounds"][0]["completion_evidence"] == receipt["completion_evidence"]

    receipt["completion_evidence"]["canonical_thp_sha256"] = "0" * 64
    issues = report_validation_issues(payload, spec)
    assert report_valid_for_spec(payload, spec) is False
    assert any("official_terminal_completion" in issue for issue in issues)


def test_run_certification_uses_valid_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    def runner(*_args, **_kwargs):
        return FakeResult(_report(target_hands=10, rounds=2))

    first = _run_certification_with_runner_for_test(spec, config=cfg, runner=runner)
    second = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("cache miss")),
    )

    assert first["status"] == STATUS_SMOKE_PASS
    assert second["status"] == STATUS_SMOKE_PASS
    assert second["cache_hit"] is True


def test_full_certification_does_not_delegate_verdict_to_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_full_report(tmp_path, candidate, opponent)),
        opponent_selection=_selection(candidate, opponent),
    )

    assert result["status"] == STATUS_CERTIFIED
    assert result["issues"] == []
    assert result["official_llm_analysis_summary"]["authoritative"] is False
    assert Path(result["certificate_path"]).is_file()
    assert Path(result["certificate_signature_path"]).is_file()


def test_full_certification_survives_llm_transport_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setenv("POK_OFFICIAL_LLM_ANALYSIS", "1")
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)
    monkeypatch.setattr(
        "official_llm_analysis.run_official_llm_analysis_sync",
        lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("analysis backend unavailable")),
    )

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_full_report(tmp_path, candidate, opponent)),
        opponent_selection=_selection(candidate, opponent),
    )

    assert result["status"] == STATUS_CERTIFIED
    assert "TimeoutError" in result["official_llm_analysis_issue"]
    assert result["official_llm_analysis_summary"]["authoritative"] is False
    certificate = json.loads(Path(result["certificate_path"]).read_text(encoding="utf-8"))
    assert "llm_analysis" not in certificate
    assert certificate["deterministic_receipt"]["verdict"]["passed"] is True


def test_full_certificate_is_not_signed_without_opponent_authorization_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    selection = _selection(candidate, opponent)
    selection["opponent"].pop("eligibility_receipt")

    result = _run_certification_with_runner_for_test(
        build_spec("full", candidate, opponent=opponent),
        config=_config(tmp_path),
        runner=lambda *_args, **_kwargs: FakeResult(
            _full_report(tmp_path, candidate, opponent)
        ),
        opponent_selection=selection,
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert "certificate_path" not in result
    assert any(
        "opponent_eligibility_receipt_missing" in issue
        for issue in result["issues"]
    )


def test_full_certification_keeps_clean_llm_analysis_advisory(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setenv("POK_OFFICIAL_LLM_ANALYSIS", "1")
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    def fake_llm(evidence, *, output_path=None, **_kwargs):
        from official_llm_analysis import normalize_official_analysis

        payload = normalize_official_analysis({
            "analysis_status": "no_findings",
            "hypothesis_class": "none",
            "confidence": 0.91,
            "evidence": [],
            "root_cause_hypothesis": "",
            "repair_guidance": "",
            "prompt_feedback": "",
        }, evidence)
        if output_path:
            Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr("official_llm_analysis.run_official_llm_analysis_sync", fake_llm)
    selection = _selection(candidate, opponent)
    monkeypatch.setattr(
        "official_certification.resolve_managed_certification_spec",
        lambda incoming: (incoming, selection),
    )
    monkeypatch.setattr(
        "official_certification.run_official_acceptance_sync",
        lambda *args, **kwargs: FakeResult(_full_report(tmp_path, candidate, opponent)),
    )

    result = _run_official_certificate_fixture(
        spec,
        config=cfg,
        runner=lambda *args, **kwargs: FakeResult(
            _full_report(tmp_path, candidate, opponent)
        ),
        opponent_selection=selection,
    )
    feedback = official_feedback_summary()

    assert result["status"] == STATUS_CERTIFIED
    assert result["test_only"] is True
    assert result["certification_identity"]["authority_scope"] == "test-only"
    assert official_full_certified(result, candidate, config=cfg) is False
    assert Path(result["certificate_path"]).is_file()
    assert result["official_llm_repair_guidance"] == ""
    assert result["official_llm_prompt_feedback"] == ""
    assert "pending-action validation" not in feedback

    analysis_path = Path(result["official_llm_analysis_path"])
    analysis_bytes = analysis_path.read_bytes()
    analysis_path.unlink()
    validation = certificate_validation(result, candidate=candidate, config=cfg)
    assert validation["valid"] is False
    assert "certificate_test_only_authority_forbidden" in validation["issues"]
    analysis_path.write_bytes(analysis_bytes)

    evidence_path = Path(result["official_evidence_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    retained_path = Path(evidence["rounds"][0]["artifacts"]["wire_events"]["path"])
    retained_bytes = retained_path.read_bytes()
    retained_path.unlink()
    validation = certificate_validation(result, candidate=candidate, config=cfg)
    assert validation["valid"] is False
    assert any(
        issue.startswith("certificate_retained_artifact_") and "wire_events" in issue
        for issue in validation["issues"]
    )
    retained_path.write_bytes(retained_bytes)

    evidence_bytes = evidence_path.read_bytes()
    evidence_path.unlink()
    validation = certificate_validation(result, candidate=candidate, config=cfg)
    assert validation["valid"] is False
    assert "certificate_evidence_missing" in validation["issues"]
    evidence_path.write_bytes(evidence_bytes)

    (candidate / "main.py").write_text("def act():\n    return -1\n", encoding="utf-8")
    validation = certificate_validation(result, candidate=candidate, config=cfg)
    assert validation["valid"] is False
    assert "certificate_identity_stale" in validation["issues"]


def test_failed_certification_persists_evidence_grounded_llm_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setenv("POK_OFFICIAL_LLM_ANALYSIS", "1")
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    def fake_llm(evidence, *, output_path=None, **_kwargs):
        from official_llm_analysis import compact_evidence_for_llm, normalize_official_analysis

        compact = compact_evidence_for_llm(evidence)
        evidence_id = compact["rounds"][0]["wire_replay_summary"]["issues"][0]["evidence_id"]
        payload = normalize_official_analysis({
            "analysis_status": "explained",
            "hypothesis_class": "protocol",
            "confidence": 0.91,
            "evidence": [{"evidence_id": evidence_id}],
            "root_cause_hypothesis": "Small blind sent check without a legal pending action.",
            "repair_guidance": "Validate pending action and blind position before every send.",
            "prompt_feedback": "Require an evidence-tested pending-action guard in the wire layer.",
        }, evidence)
        if output_path:
            Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr("official_llm_analysis.run_official_llm_analysis_sync", fake_llm)
    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_smoke_report_with_wire_replay_blocker(tmp_path)),
    )

    assert result["status"] == STATUS_FAILED
    assert result["official_llm_analysis_summary"]["authority"] == "advisory_only"
    assert result["official_llm_analysis_summary"]["analysis_status"] == "explained"
    assert "pending action" in result["official_llm_repair_guidance"]
    assert "pending-action guard" in result["official_llm_prompt_feedback"]


def test_test_only_certificate_cannot_publish_attestation(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)
    selection = _selection(candidate, opponent)
    result = _run_official_certificate_fixture(
        spec,
        config=cfg,
        runner=lambda *_a, **_k: FakeResult(
            _full_report(tmp_path, candidate, opponent)
        ),
        opponent_selection=selection,
    )

    with pytest.raises(RuntimeError, match="test-only"):
        publish_certificate_attestation(result, candidate, config=cfg)
    assert official_full_certified(result, candidate, config=cfg) is False


def test_certificate_cannot_be_reused_for_same_artifact_under_new_version(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v143")
    clone = tmp_path / "national_v144"
    opponent = _bot(tmp_path / "national_v142")
    cfg = _config(tmp_path)
    spec = build_spec(
        "full",
        candidate,
        opponent=opponent,
        quality_admission=_structural_quality_admission(candidate),
    )
    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_full_report(tmp_path, candidate, opponent)),
        opponent_selection=_selection(candidate, opponent),
    )
    shutil.copytree(candidate, clone)
    assert hash_path(candidate) == hash_path(clone)

    validation = certificate_validation(result, candidate=clone, config=cfg)

    assert validation["valid"] is False
    assert "certificate_candidate_version_mismatch" in validation["issues"]
    with pytest.raises(RuntimeError, match="test-only"):
        publish_certificate_attestation(result, clone, config=cfg)


def test_malformed_nested_certificate_fails_closed_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v143")
    opponent = _bot(tmp_path / "national_v142")
    cfg = _config(tmp_path)
    result = _run_certification_with_runner_for_test(
        build_spec(
            "full",
            candidate,
            opponent=opponent,
            quality_admission=_structural_quality_admission(candidate),
        ),
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_full_report(tmp_path, candidate, opponent)),
        opponent_selection=_selection(candidate, opponent),
    )
    record_path = Path(result["certificate_path"])
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["deterministic_receipt"]["rounds"][0]["target_hands"] = [70]
    record_path.write_text(json.dumps(record), encoding="utf-8")

    validation = certificate_validation(result, candidate=candidate, config=cfg)

    assert validation["valid"] is False
    assert any(
        issue.startswith("certificate_deterministic_round_identity_invalid")
        for issue in validation["issues"]
    )


def test_bound_deterministic_failure_survives_rejected_test_only_pass(tmp_path, monkeypatch):
    cert_root = tmp_path / "cert"
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(cert_root))
    candidate = _bot(tmp_path / "national_v143")
    opponent = _bot(tmp_path / "national_v142")
    cfg = _config(tmp_path)
    full_spec = build_spec(
        "full",
        candidate,
        opponent=opponent,
        quality_admission=_structural_quality_admission(candidate),
    )
    selection = _selection(candidate, opponent)
    certified = _run_official_certificate_fixture(
        full_spec,
        config=cfg,
        runner=lambda *_a, **_k: FakeResult(
            _full_report(tmp_path, candidate, opponent)
        ),
        opponent_selection=selection,
    )
    with pytest.raises(RuntimeError, match="test-only"):
        publish_certificate_attestation(certified, candidate, config=cfg)
    (cert_root / "status" / "national_v143.json").unlink()
    failed = _run_certification_with_runner_for_test(
        build_spec("smoke", candidate, opponent=opponent),
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(
            _smoke_report_with_wire_replay_blocker(tmp_path)
        ),
    )

    restored = read_status(candidate)

    assert failed["status"] == STATUS_FAILED
    assert official_failure_blocks_parent(failed) is True
    assert restored["status"] == STATUS_FAILED
    assert restored["official_deterministic_status_receipt"]["candidate_hash"] == hash_path(candidate)

    import official_certification as certification

    mutable_status = deepcopy(restored)
    mutable_status["issues"] = ["MUTABLE_STATUS_POISON"]
    assert certification._deterministic_status_receipt_issues(
        mutable_status,
        candidate=candidate,
    ) == []

    broken_binding = deepcopy(mutable_status)
    broken_binding["official_evidence_summary"]["deterministic_issues"] = [
        "MUTABLE_SUMMARY_POISON"
    ]
    assert "official_deterministic_status_issues_mismatch" in (
        certification._deterministic_status_receipt_issues(
            broken_binding,
            candidate=candidate,
        )
    )

    forged_receipt = deepcopy(restored)
    forged_verdict = forged_receipt["official_deterministic_status_receipt"][
        "verdict"
    ]
    forged_verdict["issues"] = ["FORGED_RECEIPT_POISON"]
    forged_receipt["official_evidence_summary"]["deterministic_issues"] = [
        "FORGED_RECEIPT_POISON"
    ]
    forged_receipt["official_evidence_summary"]["issue_count"] = 1
    receipt = forged_receipt["official_deterministic_status_receipt"]
    receipt["receipt_digest"] = canonical_digest({
        key: value for key, value in receipt.items() if key != "receipt_digest"
    })
    assert "official_deterministic_status_evidence_verdict_mismatch" in (
        certification._deterministic_status_receipt_issues(
            forged_receipt,
            candidate=candidate,
        )
    )


def test_published_attestation_tampering_invalidates_certificate(tmp_path, monkeypatch):
    published_root = tmp_path / "published"
    monkeypatch.setattr("official_certification.PUBLISHED_CERTIFICATE_DIR", published_root)
    candidate = _bot(tmp_path / "national_v1")
    path = published_root / "national_v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "kind": "official-platform-compliance-attestation",
        "bot": "national_v1",
        "certificate_digest": "tampered",
        "certificate": {},
        "attestation_digest": "tampered",
    }), encoding="utf-8")

    restored = read_status(candidate)
    validation = certificate_validation(restored, candidate=candidate)

    assert validation["valid"] is False
    assert "published_attestation_digest_mismatch" in validation["issues"]


def test_published_status_projects_the_bound_verdict_ledger_identity(
    tmp_path,
    monkeypatch,
):
    import official_certification as certification
    import official_verdict_ledger

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    published = {
        "bot": "national_v143",
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": "a" * 64,
    }
    ledger_entry = {
        "entry_digest": "b" * 64,
        "certificate_digest": "a" * 64,
        "policy_id": "official-full-v5",
        "outcome": STATUS_CERTIFIED,
    }
    monkeypatch.setattr(
        certification,
        "_published_status",
        lambda *_args, **_kwargs: dict(published),
    )
    monkeypatch.setattr(certification, "hash_path", lambda _path: "c" * 64)
    monkeypatch.setattr(
        official_verdict_ledger,
        "latest_authoritative_verdict",
        lambda _candidate_hash: {"valid": True, "entry": ledger_entry},
    )

    restored = read_status(candidate)

    assert restored["status"] == STATUS_CERTIFIED
    assert restored["official_verdict_ledger_entry"] == ledger_entry


@pytest.mark.parametrize(
    ("bootstrap_control_id", "profile", "opponent_authority"),
    (
        (None, "official-full-v5", "strict_published_pool"),
        ("first_strict_control_v1", "first_strict_control_v1", "system_control"),
    ),
)
def test_formal_profile_projection_is_derived_from_validated_certificate_spec(
    tmp_path,
    monkeypatch,
    bootstrap_control_id,
    profile,
    opponent_authority,
):
    import official_certification as certification

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    spec = {
        "mode": "full",
        "policy_id": "official-full-v5",
        "candidate": str(candidate),
        "opponent": str(tmp_path / "opponent"),
        "self_play_rounds": 5,
        "opponent_rounds": 3,
        "target_hands": 70,
        "round_timeout_sec": 900.0,
        "no_progress_timeout_sec": 75.0,
    }
    if bootstrap_control_id is not None:
        spec["bootstrap_control_id"] = bootstrap_control_id
    monkeypatch.setattr(certification, "official_compliance_verdict", lambda _status: {
        "ok": True,
        "classification": "pass",
        "blocking": False,
        "inconclusive": False,
    })
    monkeypatch.setattr(
        certification,
        "certificate_validation",
        lambda *_args, **_kwargs: {
            "valid": True,
            "issues": [],
            "spec": spec,
        },
    )

    projection = official_certification_profile_projection(
        {
            "status": STATUS_CERTIFIED,
            "mode": "full",
            "policy_id": "official-full-v5",
            "test_only": False,
            "certification_identity": {
                "test_only": False,
                "authority_scope": "production",
                "runner_provenance": "official-exe",
            },
            "certification_profile": "FORGED",
        },
        candidate,
        require_published=True,
    )

    assert projection["certification_profile"] == profile
    assert projection["opponent_authority"] == opponent_authority
    assert projection["strength_evidence_weight"] == 0
    assert projection["strategy_evidence_weight"] == 0
    assert projection["formal_summary"] == {
        "self_play_rounds": 5,
        "opponent_rounds": 3,
        "target_hands": 70,
        "rounds_requested": 8,
        "rounds_run": 8,
        "passed_rounds": 8,
        "failed_rounds": 0,
    }


def test_formal_profile_projection_fails_closed_without_valid_certificate(
    tmp_path,
    monkeypatch,
):
    import official_certification as certification

    candidate = tmp_path / "national_v144"
    candidate.mkdir()
    monkeypatch.setattr(
        certification,
        "official_compliance_verdict",
        lambda _status: {
            "ok": True,
            "classification": "pass",
            "blocking": False,
            "inconclusive": False,
        },
    )
    monkeypatch.setattr(
        certification,
        "certificate_validation",
        lambda *_args, **_kwargs: {"valid": False, "issues": ["tampered"]},
    )

    assert official_certification_profile_projection(
        {
            "status": STATUS_CERTIFIED,
            "mode": "full",
            "policy_id": "official-full-v5",
            "test_only": False,
            "certification_identity": {
                "test_only": False,
                "authority_scope": "production",
                "runner_provenance": "official-exe",
            },
            "certification_profile": "FORGED",
        },
        candidate,
    ) == {}


def test_published_first_strict_selection_allows_checkout_relocation_only(
    tmp_path,
    monkeypatch,
):
    import official_bootstrap
    from bot_namespace import EVALUATION_EPOCH
    from first_strict_control import CONTROL_AUTHORITY, CONTROL_ID

    current_candidate = tmp_path / "current" / "bots" / "national_v143"
    current_control_path = tmp_path / "current" / "control" / ("c" * 64)
    current_policy_path = (
        tmp_path / "current" / "web" / "core" / "official_bootstrap_control.json"
    )
    binding = {
        "schema_version": 1,
        "kind": "official-first-strict-candidate-binding",
        "epoch": EVALUATION_EPOCH,
        "candidate": str(current_candidate),
        "candidate_label": "national_v143",
        "candidate_version": 143,
        "candidate_hash": "a" * 64,
        "source_artifact_inherited": False,
    }
    binding["candidate_binding_digest"] = canonical_digest(binding)
    control = {
        "schema_version": 1,
        "authority": CONTROL_AUTHORITY,
        "control_id": CONTROL_ID,
        "artifact_hash": "c" * 64,
        "manifest_sha256": "d" * 64,
        "path": str(current_control_path),
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
    }
    control["identity_digest"] = canonical_digest(control)
    control_receipt = {
        "schema_version": 1,
        "kind": "first-strict-control-receipt",
        "control": control,
    }
    control_receipt["receipt_digest"] = canonical_digest(control_receipt)
    policy = {
        "path": str(current_policy_path),
        "file_sha256": "e" * 64,
        "contract_digest": "f" * 64,
        "policy_id": "official-first-strict-control-bootstrap-v1",
        "epoch": EVALUATION_EPOCH,
    }
    monkeypatch.setattr(official_bootstrap, "_policy_identity", lambda: deepcopy(policy))
    monkeypatch.setattr(
        official_bootstrap,
        "materialize_control",
        lambda: current_control_path,
    )
    monkeypatch.setattr(
        official_bootstrap,
        "control_identity",
        lambda *_a, **_k: deepcopy(control),
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_control_receipt",
        lambda *_a, **_k: [],
    )
    selection = official_bootstrap._expected_selection(
        binding,
        control_receipt,
        [],
    )

    relocated = deepcopy(selection)
    old_root = Path("/srv/evolution/.evolution_pok")
    old_binding = deepcopy(binding)
    old_binding["candidate"] = str(old_root / "bots" / "national_v143")
    old_binding["candidate_binding_digest"] = canonical_digest({
        key: value
        for key, value in old_binding.items()
        if key != "candidate_binding_digest"
    })
    old_control = deepcopy(control)
    old_control["path"] = str(old_root / "control" / ("c" * 64))
    old_control["identity_digest"] = canonical_digest({
        key: value for key, value in old_control.items() if key != "identity_digest"
    })
    old_control_receipt = deepcopy(control_receipt)
    old_control_receipt["control"] = old_control
    old_control_receipt["receipt_digest"] = canonical_digest({
        key: value
        for key, value in old_control_receipt.items()
        if key != "receipt_digest"
    })
    old_policy = deepcopy(policy)
    old_policy["path"] = str(
        old_root / "web" / "core" / "official_bootstrap_control.json"
    )
    old_authorization = deepcopy(relocated["bootstrap_control_receipt"])
    old_authorization.update({
        "bootstrap_policy": old_policy,
        "candidate_binding": old_binding,
        "first_strict_control_receipt": old_control_receipt,
        "first_strict_control_receipt_digest": old_control_receipt["receipt_digest"],
        "control_identity_digest": old_control["identity_digest"],
    })
    old_authorization["receipt_digest"] = canonical_digest({
        key: value
        for key, value in old_authorization.items()
        if key != "receipt_digest"
    })
    relocated.update({
        "candidate": old_binding["candidate"],
        "candidate_binding": old_binding,
        "bootstrap_control_receipt": old_authorization,
    })
    relocated["opponent"]["path"] = old_control["path"]
    relocated["opponent"]["eligibility_receipt"] = old_authorization
    ledger_entry = {
        "bootstrap_control_id": CONTROL_ID,
        "bootstrap_control_receipt_digest": old_authorization["receipt_digest"],
        "outcome": "official-certified",
        "policy_id": "official-full-v5",
        "mode": "full",
        "authoritative": True,
        "blocking": False,
        "classification": "pass",
        "entry_digest": "9" * 64,
    }

    issues, _expected, consumption = (
        official_bootstrap._published_selection_validation(
            relocated,
            binding,
            [ledger_entry],
            allow_consumed=True,
        )
    )
    assert issues == []
    assert consumption["consumed"] is True

    tampered = deepcopy(relocated)
    tampered_binding = tampered["candidate_binding"]
    tampered_binding["candidate_hash"] = "0" * 64
    tampered_binding["candidate_binding_digest"] = canonical_digest({
        key: value
        for key, value in tampered_binding.items()
        if key != "candidate_binding_digest"
    })
    tampered["bootstrap_control_receipt"]["candidate_binding"] = tampered_binding
    tampered["bootstrap_control_receipt"]["receipt_digest"] = canonical_digest({
        key: value
        for key, value in tampered["bootstrap_control_receipt"].items()
        if key != "receipt_digest"
    })
    tampered["opponent"]["eligibility_receipt"] = tampered[
        "bootstrap_control_receipt"
    ]
    tampered_issues, _expected, _consumption = (
        official_bootstrap._published_selection_validation(
            tampered,
            binding,
            [],
            allow_consumed=True,
        )
    )
    assert "official_bootstrap_candidate_binding_content_mismatch" in tampered_issues


def test_bootstrap_certificate_validation_uses_current_checkout_candidate(
    tmp_path,
    monkeypatch,
):
    import official_bootstrap
    import official_certification as certification
    from first_strict_control import CONTROL_ID

    signed_candidate = Path("/srv/evolution/.evolution_pok/bots/national_v143")
    current_candidate = tmp_path / "bots" / "national_v143"
    opponent = tmp_path / "control"
    spec = build_spec(
        "full",
        signed_candidate,
        opponent=opponent,
        bootstrap_control_id=CONTROL_ID,
    )
    identity = {"opponent_hash": "b" * 64}
    selection = {
        "selected": True,
        "bootstrap_control_id": CONTROL_ID,
        "opponent": {
            "eligible": True,
            "path": str(opponent.resolve()),
            "artifact_hash": "b" * 64,
            "reason": "first_strict_control_bootstrap",
        },
    }
    observed = []
    monkeypatch.setattr(
        official_bootstrap,
        "validate_first_strict_control_selection",
        lambda _selection, _control, candidate, **_kwargs: (
            observed.append(Path(candidate))
            or {"valid": True, "issues": []}
        ),
    )

    assert certification._opponent_selection_issues(
        selection,
        spec,
        identity,
        allow_consumed_bootstrap=True,
        candidate_path=current_candidate,
    ) == []
    assert observed == [current_candidate]


def test_recomputed_unkeyed_digests_cannot_bypass_certificate_signature(tmp_path, monkeypatch):
    import official_certification as certification

    cert_root = tmp_path / "cert"
    published_root = tmp_path / "published"
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(cert_root))
    monkeypatch.setattr(certification, "PUBLISHED_CERTIFICATE_DIR", published_root)
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)
    selection = _selection(candidate, opponent)
    monkeypatch.setattr(
        certification,
        "resolve_managed_certification_spec",
        lambda incoming: (incoming, selection),
    )
    monkeypatch.setattr(
        certification,
        "run_official_acceptance_sync",
        lambda *_a, **_k: FakeResult(_full_report(tmp_path, candidate, opponent)),
    )
    result = _run_official_certificate_fixture(
        spec,
        config=cfg,
        runner=lambda *_a, **_k: FakeResult(
            _full_report(tmp_path, candidate, opponent)
        ),
        opponent_selection=selection,
    )
    record = json.loads(Path(result["certificate_path"]).read_text(encoding="utf-8"))
    signature = Path(result["certificate_signature_path"]).read_text(encoding="utf-8")
    path = published_root / "national_v1.json"
    path.parent.mkdir(parents=True)
    wrapper = {
        "schema_version": certification.PUBLISHED_ATTESTATION_SCHEMA_VERSION,
        "kind": "official-platform-compliance-attestation",
        "bot": candidate.name,
        "published_at": certification.now_iso(),
        "certificate_digest": record["certificate_digest"],
        "signature": signature,
        "signature_sha256": hashlib.sha256(signature.encode("utf-8")).hexdigest(),
        "issuer": record["issuer"],
        "raw_evidence_retention": "content-addressed-local-archive",
        "certificate": record,
    }
    wrapper["attestation_digest"] = certification._attestation_payload_digest(wrapper)
    record["deterministic_receipt"]["verdict"]["passed"] = False
    record["certificate_digest"] = certification._certificate_payload_digest(record)
    wrapper["certificate_digest"] = record["certificate_digest"]
    wrapper["attestation_digest"] = certification._attestation_payload_digest(wrapper)
    path.write_text(json.dumps(wrapper), encoding="utf-8")
    status = {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "policy_id": record["policy_id"],
        "certificate_path": str(path),
        "certificate_digest": record["certificate_digest"],
        "certification_identity": record["identity"],
    }

    validation = certificate_validation(status, candidate=candidate)

    assert validation["valid"] is False
    assert any("official_certificate_signature_invalid" in issue for issue in validation["issues"])


def test_compliance_certification_has_distinct_status(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("compliance", candidate, opponent=opponent)

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
    )

    assert result["status"] == STATUS_COMPLIANCE_PASS
    assert result["mode"] == "compliance"


def test_run_certification_writes_official_evidence_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
    )

    evidence_path = Path(result["official_evidence_path"])
    assert evidence_path.exists()
    assert result["official_evidence_summary"]["classification"] == "pass"
    assert result["official_evidence_summary"]["blocking"] is False
    assert result["official_evidence_summary"]["strength_evaluation"] == "not_applicable"
    analysis_path = Path(result["official_llm_analysis_path"])
    assert analysis_path.exists()
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert analysis["analysis_source"] == "default"
    assert analysis["notes"] == ["llm_disabled"]
    assert result["official_llm_analysis_summary"]["strength_evaluation"] == "not_applicable"


def test_mutable_evidence_summary_cannot_override_deterministic_authority():
    status = {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "issues": [],
        "official_evidence_summary": {
            "classification": "communication",
            "blocking": True,
            "inconclusive": False,
            "violation": True,
        },
    }

    verdict = official_compliance_verdict(status)

    assert verdict["ok"] is True
    assert verdict["blocking"] is False
    assert verdict["classification"] == "passed_or_pending"
    assert official_full_certified(status) is False


def test_run_certification_evidence_blocking_overrides_raw_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_smoke_report_with_wire_replay_blocker(tmp_path)),
    )

    assert result["status"] == STATUS_FAILED
    assert result["official_evidence_summary"]["blocking"] is True
    assert result["official_evidence_summary"]["classification"] == "protocol"
    assert any("wire_replay: illegal_check" in issue for issue in result["issues"])
    assert official_failure_blocks_parent(result)


def test_run_certification_evidence_error_is_inconclusive_not_certified(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    def boom(*_args, **_kwargs):
        raise RuntimeError("evidence disk failure")

    monkeypatch.setattr("official_certification.build_official_evidence_bundle", boom)

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=70, rounds=8)),
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert official_full_certified(result) is False
    assert any("official_evidence_error" in issue for issue in result["issues"])


def test_evidence_archive_failure_is_inconclusive_and_preserves_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    def archive_boom(*_args, **_kwargs):
        raise OSError("archive store unavailable")

    monkeypatch.setattr("official_certification.build_evidence_archive", archive_boom)
    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_full_report(tmp_path, candidate, opponent)),
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert Path(result["official_evidence_path"]).is_file()
    assert Path(result["official_llm_analysis_path"]).is_file()
    assert "archive store unavailable" in result["official_evidence_error"]


def test_run_certification_optional_llm_analysis_is_advisory(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setenv("POK_OFFICIAL_LLM_ANALYSIS", "1")
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    def fake_llm_analysis(evidence, *, output_path=None, **_kwargs):
        from official_llm_analysis import normalize_official_analysis

        payload = normalize_official_analysis({
            "analysis_status": "no_findings",
            "hypothesis_class": "none",
            "confidence": 0.51,
            "evidence": [],
        }, evidence)
        if output_path:
            Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    import official_llm_analysis

    monkeypatch.setattr(official_llm_analysis, "run_official_llm_analysis_sync", fake_llm_analysis)

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
    )

    assert result["status"] == STATUS_SMOKE_PASS
    assert Path(result["official_llm_analysis_path"]).exists()
    assert result["official_llm_analysis_summary"]["analysis_source"] == "llm"
    assert result["official_llm_analysis_summary"]["authoritative"] is False
    assert result["official_llm_analysis_summary"]["strength_evaluation"] == "not_applicable"


def test_run_certification_runs_llm_analysis_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.delenv("POK_OFFICIAL_LLM_ANALYSIS", raising=False)
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)
    calls = {"count": 0}

    def fake_llm_analysis(evidence, *, output_path=None, **_kwargs):
        calls["count"] += 1
        from official_llm_analysis import normalize_official_analysis

        payload = normalize_official_analysis({
            "analysis_status": "no_findings",
            "hypothesis_class": "none",
            "confidence": 0.88,
            "evidence": [],
        }, evidence)
        if output_path:
            Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    import official_llm_analysis

    monkeypatch.setattr(official_llm_analysis, "run_official_llm_analysis_sync", fake_llm_analysis)

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
    )

    assert calls["count"] == 1
    assert result["status"] == STATUS_SMOKE_PASS
    assert result["official_llm_analysis_summary"]["analysis_source"] == "llm"
    assert result["official_llm_analysis_summary"]["confidence"] == 0.88


def test_inconclusive_status_includes_non_violation_validation_issues(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_smoke_report_without_thp(target_hands=70, rounds=8)),
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert result["issues"]
    assert any("thp_incomplete_for_full_certification" in issue for issue in result["issues"])
    assert result["official_evidence_summary"]["classification"] == "harness"
    assert result["official_evidence_summary"]["inconclusive"] is True


def test_full_round_incomplete_without_actor_evidence_is_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)
    receipt = {
        "passed": False,
        "issues": ["BotA_exited_early: rc=0", "thp_missing_for_full_70_hand_round"],
        "target_hands": 70,
        "log_summary": {
            "hands_started_min": 33,
            "settlements_min": 32,
            "bot_a": {"net_chips": -19466},
            "bot_b": {"net_chips": 19466},
            "issues": [],
        },
        "artifacts": {"thp_summaries": []},
    }
    report = {
        "passed": False,
        "issues": [],
        "report": {
            "summary": {"suite_dir": str(tmp_path / "suite"), "rounds_run": 8},
            "rounds": [receipt for _ in range(8)],
        },
    }

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(report),
    )
    verdict = official_compliance_verdict(result)

    assert result["status"] == STATUS_INCONCLUSIVE
    assert verdict["blocking"] is False
    assert verdict["classification"] == "inconclusive"
    assert result["official_evidence_summary"]["classification"] == "harness"
    assert result["official_evidence_summary"]["blocking"] is False
    assert any("official_full_round_incomplete_after_progress" in issue for issue in result["issues"])


def test_unattributed_protocol_string_is_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    result = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(
            _report(
                target_hands=10,
                rounds=2,
                passed=False,
                issues=["self_play_1: protocol_raise_format: msg='raise  200'"],
            )
        ),
    )

    assert result["status"] == STATUS_INCONCLUSIVE
    assert official_failure_blocks_parent(result) is False
    assert official_compliance_verdict(result)["classification"] == "inconclusive"


def test_record_local_pass_does_not_clear_failed_status(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")

    write_status(candidate, STATUS_FAILED, mode="smoke", issues=["protocol_raise_format"])
    result = record_local_pass(candidate)

    assert result["status"] == STATUS_FAILED
    assert result["issues"] == ["protocol_raise_format"]


def test_read_status_without_file_is_uncertified(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")

    result = read_status(candidate)
    verdict = official_compliance_verdict(result)

    assert result["status"] == STATUS_UNCERTIFIED
    assert result["updated_at"] is None
    assert verdict["classification"] == "uncertified"
    assert verdict["blocking"] is False


def test_record_local_pass_writes_local_status_for_uncertified(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")

    result = record_local_pass(candidate)
    verdict = official_compliance_verdict(result)

    assert result["status"] == "local-pass"
    assert result["issues"] == []
    assert verdict["classification"] == "local_pass"


def test_official_opponent_eligibility_requires_full_certificate_and_blocks_failure(tmp_path, monkeypatch):
    import official_certification as certification

    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setattr(
        "official_certification.epoch_lifecycle_eligibility",
        lambda version: {"eligible": True, "reason": "national_tcp_policy_epoch_active", "version": version},
    )
    monkeypatch.setattr(
        certification,
        "strict_role_eligibility",
        lambda *_args, **_kwargs: {
            "eligible": False,
            "issues": ["signed_full_official_certificate_required"],
        },
    )
    historical = _bot(tmp_path / "national_v143")
    opponent = _bot(tmp_path / "national_v144")

    bootstrap = official_opponent_eligibility(
        historical,
        allow_bootstrap_grandfather=True,
    )
    assert bootstrap["eligible"] is False
    assert bootstrap["reason"] == "official_full_certificate_required"
    assert bootstrap["bootstrap_requested_but_disabled"] is True

    _run_certification_with_runner_for_test(
        build_spec("smoke", historical, opponent=opponent),
        config=_config(tmp_path),
        runner=lambda *_args, **_kwargs: FakeResult(
            _smoke_report_with_wire_replay_blocker(tmp_path)
        ),
    )
    failed = official_opponent_eligibility(historical)
    assert failed["eligible"] is False
    assert failed["reason"] == "blocking_official_failure"


def test_official_opponent_eligibility_uses_strict_role_resolver(tmp_path, monkeypatch):
    import official_certification as certification

    historical = _bot(tmp_path / "national_v143")
    calls = []
    monkeypatch.setattr(
        certification,
        "epoch_lifecycle_eligibility",
        lambda version: {"eligible": True, "reason": "national_tcp_policy_epoch_active", "version": version},
    )
    monkeypatch.setattr(certification, "read_status", lambda _path: {})
    monkeypatch.setattr(certification, "official_full_certified", lambda *_a, **_k: False)
    monkeypatch.setattr(
        certification,
        "strict_role_eligibility",
        lambda candidate, role: calls.append((candidate, role)) or {
            "eligible": False,
            "issues": ["signed_full_official_certificate_required"],
        },
    )

    result = official_opponent_eligibility(
        historical,
        allow_bootstrap_grandfather=True,
        target_version=143,
    )

    assert result["eligible"] is False
    assert result["reason"] == "official_full_certificate_required"
    assert result["bootstrap_requested_but_disabled"] is True
    assert calls == [(historical, "official_opponent")]


def test_select_official_opponent_rejects_grandfather_result_even_when_requested(
    tmp_path,
    monkeypatch,
):
    import official_certification as certification
    import evolution_infra
    import national_native
    import national_runtime_authority

    candidate = _bot(tmp_path / "national_v143")
    historical = _bot(tmp_path / "national_v70")
    (historical / ".completed").touch()
    monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())
    monkeypatch.setattr(national_native, "check_native_contract", lambda _path, **_kwargs: [])
    monkeypatch.setattr(
        national_runtime_authority,
        "current_system_native_runtime_errors",
        lambda _path: [],
    )
    monkeypatch.setattr(
        certification,
        "published_bot_identity",
        lambda path: {
            "published": True,
            "artifact_hash": f"hash-{Path(path).name}",
            "tag": f"tag-{Path(path).name}",
            "tag_object": f"tag-object-{Path(path).name}",
            "issues": [],
        },
    )
    monkeypatch.setattr(
        certification,
        "official_opponent_eligibility",
        lambda *_a, **_k: {
            "eligible": True,
            "reason": "content_bound_grandfather_grant",
            "priority": 1,
        },
    )

    result = select_official_opponent(
        candidate,
        [str(historical)],
        allow_bootstrap_grandfather=True,
    )

    assert result["selected"] is False
    assert result["reason"] == "no_official_eligible_opponent"
    assert result["considered"][0]["eligible"] is False
    assert result["considered"][0]["reason"] == "official_full_certificate_required"
    assert result["considered"][0]["rejected_authorization_reason"] == "content_bound_grandfather_grant"


def test_select_official_opponent_uses_certified_candidate_only(tmp_path, monkeypatch):
    import evolution_infra
    import national_native
    import national_runtime_authority
    import official_certification

    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v134")
    bootstrap = _bot(tmp_path / "national_v70")
    certified = _bot(tmp_path / "national_v120")
    (bootstrap / ".completed").touch()
    (certified / ".completed").touch()
    monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())
    monkeypatch.setattr(national_native, "check_native_contract", lambda _path, **_kwargs: [])
    monkeypatch.setattr(
        national_runtime_authority,
        "current_system_native_runtime_errors",
        lambda _path: [],
    )
    monkeypatch.setattr(
        official_certification,
        "published_bot_identity",
        lambda path: {
            "published": True,
            "artifact_hash": f"hash-{Path(path).name}",
            "tag": f"tag-{Path(path).name}",
            "tag_object": f"tag-object-{Path(path).name}",
            "issues": [],
        },
    )
    monkeypatch.setattr(
        official_certification,
        "official_opponent_eligibility",
        lambda path, **_kwargs: {
            "eligible": Path(path).name == "national_v120",
            "reason": "official_certified" if Path(path).name == "national_v120" else "official_full_certificate_required",
            "priority": 0 if Path(path).name == "national_v120" else 1,
        },
    )

    result = select_official_opponent(
        candidate,
        [str(bootstrap), str(certified)],
        allow_bootstrap_grandfather=True,
    )

    assert result["selected"] is True
    assert result["opponent"]["bot"] == "national_v120"
    assert result["opponent"]["reason"] == "official_certified"


def test_full_certificate_rejects_grandfathered_opponent_receipt(tmp_path):
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v143")
    opponent = _bot(tmp_path / "national_v142")
    spec = build_spec(
        "full",
        candidate,
        opponent=opponent,
        quality_admission=_structural_quality_admission(candidate),
    )
    identity = certification_identity(spec)
    selection = _selection(candidate, opponent)
    receipt = selection["opponent"]["eligibility_receipt"]
    receipt["kind"] = "content_bound_grandfather_grant"
    receipt["receipt_digest"] = canonical_digest({
        key: value for key, value in receipt.items() if key != "receipt_digest"
    })
    selection["opponent"]["reason"] = "content_bound_grandfather_grant"

    issues = certification._opponent_selection_issues(selection, spec, identity)

    assert "certificate_official_opponent_reason_invalid" in issues
    assert "certificate_official_opponent_eligibility_receipt_kind_mismatch" in issues


def test_readiness_counts_unique_certified_artifacts_not_version_copies(tmp_path, monkeypatch):
    import evolution_infra
    import national_native
    import national_runtime_authority
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v145")
    (candidate / "candidate_only.py").write_text("VALUE = 145\n", encoding="utf-8")
    first = _bot(tmp_path / "national_v143")
    second = tmp_path / "national_v144"
    shutil.copytree(first, second)
    for path in (first, second):
        (path / ".completed").touch()
    shared_hash = hash_path(first)
    assert shared_hash == hash_path(second)

    monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())
    monkeypatch.setattr(national_native, "check_native_contract", lambda _path: [])
    monkeypatch.setattr(
        national_runtime_authority,
        "current_system_native_runtime_errors",
        lambda _path: [],
    )
    monkeypatch.setattr(certification, "read_status", lambda _path: {
        "status": STATUS_CERTIFIED,
        "mode": "full",
        "policy_id": certification.FULL_POLICY_ID,
        "certification_identity": {"candidate_hash": shared_hash},
    })
    monkeypatch.setattr(certification, "official_full_certified", lambda *_a, **_k: True)
    monkeypatch.setattr(certification, "published_bot_identity", lambda path: {
        "published": True,
        "artifact_hash": shared_hash,
        "tag": f"national-bot-v{Path(path).name.removeprefix('national_v')}",
        "tag_object": "tag-object",
    })
    observed_readiness = []

    def eligible(_path, **kwargs):
        observed_readiness.append(kwargs["certified_alternatives"])
        return {"eligible": True, "reason": "official_certified", "priority": 0}

    monkeypatch.setattr(certification, "official_opponent_eligibility", eligible)

    result = select_official_opponent(candidate, [str(first), str(second)])

    assert result["selected"] is True
    assert result["readiness"]["certified_alternatives"] == 1
    assert observed_readiness == [1, 1]


def test_official_opponent_rejects_different_version_with_same_artifact(tmp_path, monkeypatch):
    import evolution_infra
    import national_native
    import official_certification as certification

    candidate = _bot(tmp_path / "national_v145")
    clone = tmp_path / "national_v142"
    shutil.copytree(candidate, clone)
    (clone / ".completed").touch()
    shared_hash = hash_path(candidate)
    assert hash_path(clone) == shared_hash

    monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())
    monkeypatch.setattr(national_native, "check_native_contract", lambda _path: [])
    monkeypatch.setattr(certification, "published_bot_identity", lambda path: {
        "published": True,
        "artifact_hash": shared_hash,
        "tag": "national-bot-v142",
        "tag_object": "a" * 40,
        "issues": [],
    })

    result = select_official_opponent(candidate, [str(clone)])

    assert result["selected"] is False
    assert result["considered"][0]["reason"] == "candidate_artifact_clone"


def test_managed_certification_blocks_before_runner_without_eligible_opponent(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)
    monkeypatch.setattr("official_certification.select_official_opponent", lambda *_a, **_k: {
        "selected": False,
        "reason": "no_official_eligible_opponent",
        "considered": [],
    })

    with pytest.raises(RuntimeError, match="official_certification_job"):
        run_certification(spec)
    assert not (tmp_path / "cert" / "status" / "national_v1.json").exists()


def test_production_certification_apis_do_not_expose_runner_injection():
    import official_certification as certification

    assert "runner" not in inspect.signature(run_certification).parameters
    assert not hasattr(certification, "enqueue_certification")
    assert not hasattr(certification, "process_certification_queue")
    assert not hasattr(certification, "queue_snapshot")


def test_internal_full_impl_rejects_spoofed_official_runner(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)

    with pytest.raises(RuntimeError, match="bound production official-EXE runner"):
        _run_certification_impl(
            spec,
            config=_config(tmp_path),
            runner=lambda *_a, **_k: FakeResult(
                _full_report(tmp_path, candidate, opponent)
            ),
            runner_provenance="official-exe",
            enforce_opponent_selection=False,
        )


def test_full_injected_runner_artifact_is_explicitly_test_only(tmp_path, monkeypatch):
    from official_verdict_ledger import append_verdict, ledger_path

    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    result = _run_certification_with_runner_for_test(
        build_spec("full", candidate, opponent=opponent),
        config=cfg,
        runner=lambda *_a, **_k: FakeResult(
            _full_report(tmp_path, candidate, opponent)
        ),
        opponent_selection=_selection(candidate, opponent),
    )

    validation = certificate_validation(result, candidate=candidate, config=cfg)

    assert result["status"] == STATUS_CERTIFIED
    assert result["test_only"] is True
    assert result["certification_identity"]["runner_provenance"] == TEST_ONLY_RUNNER_PROVENANCE
    assert result["certification_identity"]["authority_scope"] == "test-only"
    assert official_full_certified(result, candidate, config=cfg) is False
    assert validation["valid"] is False
    assert "certificate_test_only_authority_forbidden" in validation["issues"]
    with pytest.raises(ValueError, match="status_test_only"):
        append_verdict(result)
    assert not ledger_path().exists()


def test_test_runner_cache_cannot_satisfy_production_certification(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)
    report = _report(target_hands=10, rounds=2)

    injected = _run_certification_with_runner_for_test(
        spec,
        config=cfg,
        runner=lambda *_a, **_k: FakeResult(report),
    )

    selection = _selection(candidate, opponent)
    official_calls = []
    monkeypatch.setattr(
        "official_certification.resolve_managed_certification_spec",
        lambda incoming: (incoming, selection),
    )

    def official_runner(*_args, **_kwargs):
        official_calls.append(True)
        return FakeResult(report)

    monkeypatch.setattr("official_certification.run_official_acceptance_sync", official_runner)
    production = run_certification(spec, config=cfg)

    assert official_calls == [True]
    assert production["cache_hit"] is False
    assert production["cache_key"] != injected["cache_key"]
    assert production["certification_identity"]["runner_provenance"] == "official-exe"


def test_record_local_pass_preserves_inconclusive_official_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")

    write_status(candidate, STATUS_INCONCLUSIVE, mode="smoke", issues=["self_play_1: port_busy_before_start: 127.0.0.1:10001"])
    result = record_local_pass(candidate)

    assert result["status"] == STATUS_INCONCLUSIVE
    assert result["issues"] == ["self_play_1: port_busy_before_start: 127.0.0.1:10001"]


def test_unbound_failure_strings_never_block_parent_selection():
    protocol_failure = {
        "status": STATUS_FAILED,
        "issues": ["self_play_1: protocol_raise_format: msg='raise  200'"],
    }
    infra_failure = {
        "status": STATUS_INCONCLUSIVE,
        "issues": ["self_play_1: port_busy_before_start: 127.0.0.1:10001"],
    }
    legacy_infra_failure = {
        "status": STATUS_FAILED,
        "issues": ["official_acceptance_suite_exception: FileNotFoundError: wine"],
    }
    empty_failure = {"status": STATUS_INCONCLUSIVE, "issues": []}

    assert not official_failure_blocks_parent(protocol_failure)
    assert official_compliance_verdict(protocol_failure)["classification"] == "inconclusive"
    assert not official_failure_blocks_parent(infra_failure)
    assert official_compliance_verdict(infra_failure)["classification"] == "inconclusive"
    assert not official_failure_blocks_parent(legacy_infra_failure)
    assert official_compliance_verdict(legacy_infra_failure)["classification"] == "inconclusive"
    assert not official_failure_blocks_parent(empty_failure)
    assert official_compliance_verdict(empty_failure)["inconclusive"] is True
