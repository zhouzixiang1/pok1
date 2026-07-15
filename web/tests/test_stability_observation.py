import json
import threading
import time

import pytest


def _identity(*, boot="boot-a", contract="a" * 64):
    return {
        "evaluation_epoch": "national_tcp_policy_v1",
        "process_boot_id": boot,
        "process_pid": "4242",
        "process_start_ticks": "777",
        "infrastructure_contract_hash": contract,
        "runtime_config_digest": "c" * 64,
        "repository_head": f"{142:040x}",
        "repository_branch": "main",
    }


def _install_identity(monkeypatch, module, current=None):
    value = current if current is not None else _identity()
    monkeypatch.setattr(module, "_test_identity_state", value, raising=False)
    monkeypatch.setattr(module, "_current_identity", lambda: dict(value))
    return value


def _publication(version, *, repaired=False):
    publication_id = f"{version:064x}"
    commit_oid = f"{version:040x}"
    result = {
        "committed": True,
        "version": version,
        "publication_id": publication_id,
        "commit_oid": commit_oid,
        "push_ok": True,
        "checkpoint_cleared": True,
        "completed_sentinel_written": True,
        "remote_proof": {
            "valid": True,
            "remote_main_oid": commit_oid,
            "remote_refs": {
                f"refs/tags/national-bot-v{version}": "a" * 40,
                f"refs/tags/national-bot-v{version}^{{}}": f"{version:040x}",
                f"refs/tags/national-high-water-v{version}": "b" * 40,
                f"refs/tags/national-high-water-v{version}^{{}}": f"{version:040x}",
            },
        },
    }
    checkpoint = {
        "stage": "publishing",
        "source_v": version - 1,
        "parent2_v": None,
        "workflow_run_id": f"generation:{version}:workflow-v1",
        "workflow_profile_id": "national_native_v1",
        "national_execution_mode": "native_tcp",
        "generation_attempt": 0,
        "precommit_rework_count": 1 if repaired else 0,
        "official_rework_count": 0,
        "repair_baseline_artifact_hash": None,
        "publication_intent": {
            "publication_id": publication_id,
            "official_certificate_digest": f"{version + 1:064x}",
            "candidate_artifact_hash": f"{version + 2:064x}",
            "official_policy_id": "official-full-v5",
            "official_status_digest": "7" * 64,
            "certificate_file_sha256": "8" * 64,
            "certificate_attestation_digest": "9" * 64,
            "final_gate_ledger_digest": f"{version + 3:064x}",
            "remote_publication_required": True,
        },
    }
    return result, checkpoint


def _record(module, version, *, repaired=False):
    result, checkpoint = _publication(version, repaired=repaired)
    identity = getattr(module, "_test_identity_state", None)
    if isinstance(identity, dict):
        identity["repository_head"] = result["commit_oid"]
    return module.record_published_generation(
        version=version,
        publication_result=result,
        publishing_checkpoint=checkpoint,
    )


def _projection_fixture(count=3):
    return {
        "schema_version": 1,
        "kind": "national-tcp-uninterrupted-evolution-observation",
        "authority": "operator_acceptance_only",
        "strategy_evidence_weight": 0,
        "strength_evidence_weight": 0,
        "status": "observing",
        "continuity_valid": True,
        "count": count,
        "target": 10,
        "remaining": 10 - count,
        "complete": False,
        "strength_cycle_ready": False,
        "strength_cycle": {"ready": False, "reason": "target_not_reached"},
        "continuity_id": "cache-test",
        "last_reset_reason": "runtime_process_start",
        "identity_mismatches": [],
        "errors": [],
        "observations": [],
        "recorded_repository_head": "f" * 40,
        "current_repository_head": "f" * 40,
        "recorded_repository_branch": "main",
        "current_repository_branch": "main",
    }


@pytest.fixture(autouse=True)
def live_daemon_identity(monkeypatch):
    import stability_observation as observation

    monkeypatch.setattr(
        observation,
        "_daemon_process_identity",
        lambda: "d" * 64,
    )
    monkeypatch.setattr(observation, "_live_publication_errors", lambda _state: [])
    monkeypatch.setattr(observation, "_daemon_heartbeat_errors", lambda: [])
    def fake_remote_refs(*args):
        if args == ("rev-parse", "--verify", "HEAD"):
            return "f" * 40
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args[:2] == ("rev-list", "--count"):
            return "1"
        requested_versions = [
            version
            for version in range(143, 180)
            if any(
                f"national-bot-v{version}" in str(arg)
                for arg in args
            )
        ]
        remote_main = (
            f"{max(requested_versions):040x}"
            if requested_versions
            else "f" * 40
        )
        lines = [f"{remote_main}\trefs/heads/main"]
        for version in range(143, 180):
            commit_oid = f"{version:040x}"
            lines.extend((
                f"{'a' * 40}\trefs/tags/national-bot-v{version}",
                f"{commit_oid}\trefs/tags/national-bot-v{version}^{{}}",
                f"{'b' * 40}\trefs/tags/national-high-water-v{version}",
                f"{commit_oid}\trefs/tags/national-high-water-v{version}^{{}}",
            ))
        return "\n".join(lines)

    monkeypatch.setattr(observation, "_git", fake_remote_refs)
    monkeypatch.setattr(
        observation,
        "_git_command_succeeds",
        lambda *_args: True,
    )
    monkeypatch.setattr(observation, "_owner_process_errors", lambda _state: [])
    monkeypatch.setattr(
        observation,
        "_generation_evidence_binding",
        lambda version, source_v, publishing_checkpoint=None: (
            {
                "schema_version": 1,
                "mode": "fresh_strict_v143_bootstrap",
                "reason": "empty_strict_policy_pool",
                "source_v": int(source_v),
                "strength_evidence_admitted": False,
                "strength_evidence_weight": 0,
            }
            if int(version) == 143
            else {
                "schema_version": 1,
                "mode": "singleton_strict_v144_bootstrap",
                "reason": "single_strict_parent_no_peer_pool",
                "source_v": int(source_v),
                "strength_evidence_admitted": False,
                "strength_evidence_weight": 0,
            }
            if int(version) == 144
            else {
                "schema_version": 1,
                "mode": "frozen_native_evaluation",
                "reason": "verified_complete_frozen_cutoff",
                "source_v": int(source_v),
                "strength_evidence_admitted": True,
                "strength_evidence_weight": 1,
                "generation_snapshot_manifest_digest": "1" * 64,
                "evaluation_identity_digest": "2" * 64,
                "cycle_manifest_digest": "3" * 64,
                "cycle_save_num": int(version),
                "cycle_daemon_run_id": "daemon-run",
                "cycle_active_bots": [f"national_v{source_v}"],
                "selection_sha256": "4" * 64,
                "match_history_index_sha256": "5" * 64,
                "replay_spotlight_sha256": "6" * 64,
            }
        ),
    )
    monkeypatch.setattr(
        observation,
        "_strength_cycle_readiness",
        lambda _state: {
            "ready": True,
            "reason": "latest_bot_admitted_to_current_native_cycle",
        },
    )


def test_missing_observation_fails_closed_and_has_zero_evidence_weight(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)

    projection = observation.stability_observation_projection()

    assert projection["status"] == "not_started"
    assert projection["continuity_valid"] is False
    assert projection["count"] == 0
    assert projection["complete"] is False
    assert projection["strategy_evidence_weight"] == 0
    assert projection["strength_evidence_weight"] == 0


def test_current_identity_binds_attached_branch_and_exact_head(monkeypatch):
    import stability_observation as observation

    monkeypatch.setattr(
        observation,
        "build_evaluation_contract",
        lambda *_args, **_kwargs: {"hash": "a" * 64},
    )
    monkeypatch.setattr(
        observation,
        "runtime_configuration_identity",
        lambda: {"digest": "c" * 64, "config": {}},
    )
    branch = ["main"]

    def repository_git(*args, **_kwargs):
        if args == ("rev-parse", "--verify", "HEAD"):
            return "b" * 40
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return branch[0]
        raise AssertionError(args)

    monkeypatch.setattr(observation, "_git", repository_git)
    identity = observation._current_identity()
    assert identity["repository_head"] == "b" * 40
    assert identity["repository_branch"] == "main"

    branch[0] = "HEAD"
    with pytest.raises(
        observation.StabilityObservationError,
        match="repository_branch_unavailable",
    ):
        observation._current_identity()


def test_cached_projection_is_nonblocking_and_coalesces_remote_verification(
    monkeypatch,
):
    import stability_observation as observation

    observation.invalidate_stability_projection_cache()
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_projection():
        calls.append("verify")
        started.set()
        assert release.wait(timeout=2)
        return _projection_fixture()

    monkeypatch.setattr(observation, "stability_observation_projection", slow_projection)
    before = time.monotonic()
    first = observation.stability_observation_cached_projection()
    elapsed = time.monotonic() - before
    assert elapsed < 0.2
    assert first["verification"]["state"] == "pending"
    assert first["count"] == 0
    assert started.wait(timeout=1)

    second = observation.stability_observation_cached_projection()
    assert second["verification"]["state"] == "pending"
    assert calls == ["verify"]
    release.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        fresh = observation.stability_observation_cached_projection()
        if fresh["verification"]["state"] == "fresh":
            break
        time.sleep(0.01)
    assert fresh["verification"]["state"] == "fresh"
    assert fresh["count"] == 3
    assert fresh["verification"]["fresh_until"] > fresh["verification"]["checked_at"]
    assert calls == ["verify"]


def test_cache_invalidation_waits_for_obsolete_verifier_before_refresh(
    monkeypatch,
):
    import stability_observation as observation

    observation.invalidate_stability_projection_cache()
    first_started = threading.Event()
    first_release = threading.Event()
    second_started = threading.Event()
    second_release = threading.Event()
    guard = threading.Lock()
    calls = 0
    active = 0
    max_active = 0

    def controlled_projection():
        nonlocal calls, active, max_active
        with guard:
            calls += 1
            call_number = calls
            active += 1
            max_active = max(max_active, active)
        try:
            if call_number == 1:
                first_started.set()
                assert first_release.wait(timeout=2)
            else:
                second_started.set()
                assert second_release.wait(timeout=2)
            return _projection_fixture()
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(
        observation,
        "stability_observation_projection",
        controlled_projection,
    )
    assert observation.stability_observation_cached_projection()["verification"][
        "state"
    ] == "pending"
    assert first_started.wait(timeout=1)

    observation.invalidate_stability_projection_cache()
    # The invalidation makes the old result unusable, but cannot steal the
    # worker slot while that remote verification is still running.
    assert observation.stability_observation_cached_projection()["verification"][
        "state"
    ] == "pending"
    assert calls == 1
    assert second_started.is_set() is False

    first_release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not second_started.is_set():
        observation.stability_observation_cached_projection()
        time.sleep(0.01)
    assert second_started.is_set()
    assert calls == 2
    assert max_active == 1

    second_release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = observation.stability_observation_cached_projection()
        if result["verification"]["state"] == "fresh":
            break
        time.sleep(0.01)
    assert result["verification"]["state"] == "fresh"


def test_cached_projection_expires_to_zero_before_background_refresh(monkeypatch):
    import stability_observation as observation

    observation.invalidate_stability_projection_cache()
    clock = [1000.0]
    monkeypatch.setattr(observation, "_now", lambda: clock[0])
    monkeypatch.setattr(
        observation,
        "stability_observation_projection",
        lambda: _projection_fixture(count=4),
    )
    assert observation.stability_observation_cached_projection()["verification"][
        "state"
    ] == "pending"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        fresh = observation.stability_observation_cached_projection()
        if fresh["verification"]["state"] == "fresh":
            break
        time.sleep(0.01)
    assert fresh["count"] == 4

    clock[0] += observation.STABILITY_VERIFICATION_TTL_SEC + 1
    stale = observation.stability_observation_cached_projection()
    assert stale["verification"]["state"] == "stale"
    assert stale["count"] == 0
    assert stale["continuity_valid"] is False
    assert stale["complete"] is False


def test_cached_projection_reports_background_failure_without_raising(monkeypatch):
    import stability_observation as observation

    observation.invalidate_stability_projection_cache()
    monkeypatch.setattr(
        observation,
        "stability_observation_projection",
        lambda: (_ for _ in ()).throw(RuntimeError("origin unavailable")),
    )
    assert observation.stability_observation_cached_projection()["verification"][
        "state"
    ] == "pending"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        failed = observation.stability_observation_cached_projection()
        if failed["verification"]["state"] == "failed":
            break
        time.sleep(0.01)
    assert failed["verification"]["state"] == "failed"
    assert "origin unavailable" in failed["verification"]["error"]
    assert failed["count"] == 0


def test_cached_projection_drops_green_on_epoch_or_head_authority_change(
    monkeypatch,
):
    import stability_observation as observation

    observation.invalidate_stability_projection_cache()
    head = ["a" * 40]

    def local_git(*args):
        if args == ("rev-parse", "--verify", "HEAD"):
            return head[0]
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        raise AssertionError(args)

    def verified_projection():
        projection = _projection_fixture(count=10)
        projection.update({
            "status": "complete",
            "remaining": 0,
            "complete": True,
            "strength_cycle_ready": True,
            "strength_cycle": {"ready": True, "reason": "admitted"},
            "recorded_repository_head": head[0],
            "current_repository_head": head[0],
            "recorded_repository_branch": "main",
            "current_repository_branch": "main",
        })
        return projection

    monkeypatch.setattr(observation, "_git", local_git)
    monkeypatch.setattr(
        observation,
        "stability_observation_projection",
        verified_projection,
    )

    def wait_fresh(authority):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            value = observation.stability_observation_cached_projection(
                expected_epoch_authority_digest=authority,
            )
            if value["verification"]["state"] == "fresh":
                return value
            time.sleep(0.01)
        raise AssertionError("stability projection did not become fresh")

    first_authority = "1" * 64
    assert observation.stability_observation_cached_projection(
        expected_epoch_authority_digest=first_authority,
    )["verification"]["state"] == "pending"
    first = wait_fresh(first_authority)
    assert first["complete"] is True

    second_authority = "2" * 64
    epoch_moved = observation.stability_observation_cached_projection(
        expected_epoch_authority_digest=second_authority,
    )
    assert epoch_moved["verification"]["state"] == "pending"
    assert epoch_moved["count"] == 0
    assert epoch_moved["complete"] is False
    assert epoch_moved["verification"]["authority"][
        "epoch_stream_authority_digest"
    ] == second_authority
    assert wait_fresh(second_authority)["complete"] is True

    head[0] = "b" * 40
    head_moved = observation.stability_observation_cached_projection(
        expected_epoch_authority_digest=second_authority,
    )
    assert head_moved["verification"]["state"] == "pending"
    assert head_moved["count"] == 0
    assert head_moved["complete"] is False
    assert head_moved["verification"]["authority"][
        "repository_head"
    ] == head[0]
    assert wait_fresh(second_authority)["complete"] is True


def test_runtime_config_digest_is_identity_bearing_and_drift_resets_count(
    monkeypatch,
):
    import stability_observation as observation

    current = _identity()
    _install_identity(monkeypatch, observation, current)
    observation.initialize_stability_observation()
    _record(observation, 143)
    current["runtime_config_digest"] = "e" * 64

    projection = observation.stability_observation_projection()

    assert projection["status"] == "reset_required"
    assert projection["count"] == 0
    assert projection["identity_mismatches"] == ["runtime_config_digest"]


def test_repository_head_or_branch_drift_immediately_hides_streak(monkeypatch):
    import stability_observation as observation

    current = _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, 143)
    _record(observation, 144)

    current["repository_head"] = "e" * 40
    projection = observation.stability_observation_projection()
    assert projection["count"] == 0
    assert projection["continuity_valid"] is False
    assert projection["identity_mismatches"] == ["repository_head"]
    assert projection["recorded_repository_head"] == f"{144:040x}"
    assert projection["current_repository_head"] == "e" * 40

    current["repository_head"] = f"{144:040x}"
    current["repository_branch"] = "unexpected-worktree"
    projection = observation.stability_observation_projection()
    assert projection["count"] == 0
    assert projection["identity_mismatches"] == ["repository_branch"]


def test_intervening_repository_commits_persist_reset_before_new_row(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, 143)
    _record(observation, 144)

    original_git = observation._git

    def drifted_git(*args, **kwargs):
        if args[:2] == ("rev-list", "--count"):
            return "2"
        return original_git(*args, **kwargs)

    monkeypatch.setattr(observation, "_git", drifted_git)
    projection = _record(observation, 145)

    assert projection["count"] == 1
    assert [row["version"] for row in projection["observations"]] == [145]
    assert projection["last_reset_reason"] == "repository_head_drift"
    assert projection["reset_history"][-1]["previous_count"] == 2
    assert projection["reset_history"][-1]["details"]["issues"] == [
        "publication_repository_advance_not_single_commit"
    ]


def test_publication_requires_local_and_remote_main_at_exact_commit(monkeypatch):
    import stability_observation as observation

    current = _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    result, checkpoint = _publication(143)

    with pytest.raises(
        observation.StabilityObservationError,
        match="publication_repository_head_mismatch",
    ):
        observation.record_published_generation(
            version=143,
            publication_result=result,
            publishing_checkpoint=checkpoint,
        )

    current["repository_head"] = result["commit_oid"]
    result["remote_proof"]["remote_main_oid"] = "f" * 40
    with pytest.raises(
        observation.StabilityObservationError,
        match="publication_remote_main_head_mismatch",
    ):
        observation.record_published_generation(
            version=143,
            publication_result=result,
            publishing_checkpoint=checkpoint,
        )


def test_runtime_configuration_binding_hashes_exact_effective_values():
    import stability_observation as observation

    observation.clear_runtime_configuration_binding()
    first = observation.bind_runtime_configuration({
        "daemon_enabled": True,
        "daemon_workers": 4,
        "daemon_pairs": 5,
    })
    second = observation.bind_runtime_configuration({
        "daemon_enabled": True,
        "daemon_workers": 4,
        "daemon_pairs": 6,
    })

    assert first["config"]["daemon_pairs"] == 5
    assert second["config"]["daemon_pairs"] == 6
    assert first["digest"] != second["digest"]


def test_ten_consecutive_publications_complete_and_duplicate_is_idempotent(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    started = observation.initialize_stability_observation()
    assert started["count"] == 0

    for version in range(143, 153):
        projection = _record(observation, version)

    assert projection["count"] == 10
    assert projection["remaining"] == 0
    assert projection["complete"] is True
    continuity_id = projection["continuity_id"]

    duplicate = _record(observation, 152)
    assert duplicate["count"] == 10
    assert duplicate["continuity_id"] == continuity_id
    assert [row["version"] for row in duplicate["observations"]] == list(
        range(143, 153)
    )


def test_ten_publications_wait_for_latest_native_strength_cycle(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    monkeypatch.setattr(
        observation,
        "_strength_cycle_readiness",
        lambda _state: {"ready": False, "reason": "latest_bot_has_no_sample"},
    )
    observation.initialize_stability_observation()
    for version in range(143, 153):
        _record(observation, version)

    projection = observation.stability_observation_projection()

    assert projection["count"] == 10
    assert projection["status"] == "awaiting_strength_cycle"
    assert projection["strength_cycle_ready"] is False
    assert projection["complete"] is False


def test_process_restart_resets_existing_streak(monkeypatch):
    import stability_observation as observation

    current = _identity()
    _install_identity(monkeypatch, observation, current)
    observation.initialize_stability_observation()
    _record(observation, 143)
    _record(observation, 144)

    current["process_boot_id"] = "boot-b"
    monkeypatch.setattr(
        observation,
        "_owner_process_errors",
        lambda _state: ["owner_process_unavailable:ProcessLookupError"],
    )
    reset = observation.initialize_stability_observation("runtime_process_start")

    assert reset["continuity_valid"] is True
    assert reset["count"] == 0
    assert reset["last_reset_reason"] == "runtime_process_start"
    assert reset["reset_history"][-1]["previous_count"] == 2
    assert "process_boot_id" in reset["reset_history"][-1]["details"][
        "identity_mismatches"
    ]


def test_identity_drift_replay_persists_reset_without_recounting_duplicate(
    monkeypatch,
):
    import stability_observation as observation

    current = _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, 143)
    _record(observation, 144)

    current["repository_branch"] = "unexpected-worktree"
    result, checkpoint = _publication(144)
    replay = observation.record_published_generation(
        version=144,
        publication_result=result,
        publishing_checkpoint=checkpoint,
    )

    assert replay["count"] == 0
    assert replay["observations"] == []
    assert replay["last_reset_reason"] == "publication_observer_reinitialized"
    assert replay["reset_history"][-1]["previous_count"] == 2
    assert replay["reset_history"][-1]["details"]["identity_mismatches"] == [
        "repository_branch"
    ]
    persisted = json.loads(observation.STATE_FILE.read_text(encoding="utf-8"))
    assert persisted["observations"] == []


def test_foreign_view_only_reader_does_not_replace_live_owner(monkeypatch):
    import stability_observation as observation

    current = _identity()
    _install_identity(monkeypatch, observation, current)
    observation.initialize_stability_observation()
    _record(observation, 143)

    current.update({
        "process_boot_id": "view-only-boot",
        "process_pid": "5252",
        "process_start_ticks": "888",
    })
    projection = observation.stability_observation_projection()

    assert projection["continuity_valid"] is True
    assert projection["count"] == 1
    assert projection["identity_mismatches"] == []


def test_second_live_writer_cannot_take_over_observation(monkeypatch):
    import stability_observation as observation

    current = _identity()
    _install_identity(monkeypatch, observation, current)
    observation.initialize_stability_observation()
    _record(observation, 143)
    current.update({
        "process_boot_id": "second-writer",
        "process_pid": "6262",
        "process_start_ticks": "999",
    })

    with pytest.raises(
        observation.StabilityObservationError,
        match="owner_process_still_alive",
    ):
        observation.initialize_stability_observation()
    with pytest.raises(
        observation.StabilityObservationError,
        match="owner_process_still_alive",
    ):
        observation.reset_stability_observation("orchestrator_restart")
    with pytest.raises(
        observation.StabilityObservationError,
        match="owner_process_still_alive",
    ):
        _record(observation, 144)


def test_contract_drift_projection_is_read_only_and_fails_closed(monkeypatch):
    import stability_observation as observation

    current = _identity()
    _install_identity(monkeypatch, observation, current)
    observation.initialize_stability_observation()
    _record(observation, 143)
    before = observation.STATE_FILE.read_bytes()

    current["infrastructure_contract_hash"] = "b" * 64
    projection = observation.stability_observation_projection()

    assert projection["status"] == "reset_required"
    assert projection["continuity_valid"] is False
    assert projection["count"] == 0
    assert projection["identity_mismatches"] == [
        "infrastructure_contract_hash"
    ]
    assert observation.STATE_FILE.read_bytes() == before


def test_version_gap_and_generation_repair_each_restart_count(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, 143)
    _record(observation, 144)

    gap = _record(observation, 146)
    assert gap["count"] == 1
    assert gap["observations"][0]["version"] == 146
    assert gap["last_reset_reason"] == "publication_version_gap"

    repaired = _record(observation, 147, repaired=True)
    assert repaired["count"] == 1
    assert repaired["observations"][0]["version"] == 147
    assert repaired["last_reset_reason"] == "generation_repair_detected"


def test_rating_daemon_identity_change_restarts_count(monkeypatch):
    import stability_observation as observation

    current_daemon = ["d" * 64]
    _install_identity(monkeypatch, observation)
    monkeypatch.setattr(
        observation,
        "_daemon_process_identity",
        lambda: current_daemon[0],
    )
    observation.initialize_stability_observation()
    _record(observation, 143)
    _record(observation, 144)

    current_daemon[0] = "e" * 64
    restarted = _record(observation, 145)

    assert restarted["count"] == 1
    assert restarted["observations"][0]["version"] == 145
    assert restarted["last_reset_reason"] == "rating_daemon_restart_detected"


def test_live_daemon_or_remote_ref_drift_invalidates_projection(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, 143)

    monkeypatch.setattr(
        observation,
        "_live_daemon_errors",
        lambda _state: ["rating_daemon_process_identity_mismatch"],
    )
    daemon_drift = observation.stability_observation_projection()
    assert daemon_drift["count"] == 0
    assert daemon_drift["status"] == "reset_required"
    assert "rating_daemon_process_identity_mismatch" in daemon_drift["errors"]

    monkeypatch.setattr(observation, "_live_daemon_errors", lambda _state: [])
    monkeypatch.setattr(
        observation,
        "_remote_publication_errors",
        lambda _state: ["v143:remote_tag_object_mismatch:national-bot-v143"],
    )
    remote_drift = observation.stability_observation_projection()
    assert remote_drift["count"] == 0
    assert remote_drift["status"] == "reset_required"
    assert any("remote_tag_object_mismatch" in item for item in remote_drift["errors"])

    monkeypatch.setattr(observation, "_remote_publication_errors", lambda _state: [])
    monkeypatch.setattr(
        observation,
        "_live_daemon_errors",
        lambda _state: ["rating_daemon_heartbeat_stale:121.0s"],
    )
    stale_heartbeat = observation.stability_observation_projection()
    assert stale_heartbeat["count"] == 0
    assert "rating_daemon_heartbeat_stale:121.0s" in stale_heartbeat["errors"]


def test_remote_publication_reopens_exact_annotated_refs_without_fetch(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, 143)
    state = json.loads(observation.STATE_FILE.read_text(encoding="utf-8"))
    row = state["observations"][0]
    remote = row["remote_publication"]
    lines = [f"{state['repository_head']}\trefs/heads/main"]
    for name, identity in remote["refs"].items():
        lines.extend((
            f"{identity['object_oid']}\trefs/tags/{name}",
            f"{identity['peeled_commit_oid']}\trefs/tags/{name}^{{}}",
        ))
    monkeypatch.setattr(observation, "_git", lambda *args: "\n".join(lines))
    monkeypatch.setattr(
        observation,
        "_git_command_succeeds",
        lambda *args: True,
    )

    assert observation._remote_publication_errors(state) == []

    lines[0] = f"{'f' * 40}\trefs/heads/main"
    assert "remote_main_head_mismatch" in observation._remote_publication_errors(
        state
    )
    lines[0] = f"{state['repository_head']}\trefs/heads/main"

    completion_index = next(
        index
        for index, line in enumerate(lines)
        if line.endswith("\trefs/tags/national-bot-v143")
    )
    lines[completion_index] = f"{'0' * 40}\trefs/tags/national-bot-v143"
    errors = observation._remote_publication_errors(state)
    assert "v143:remote_tag_object_mismatch:national-bot-v143" in errors


def test_tampered_state_is_never_rendered_as_progress(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, 143)
    state = json.loads(observation.STATE_FILE.read_text(encoding="utf-8"))
    state["observations"][0]["version"] = 152
    observation.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    projection = observation.stability_observation_projection()

    assert projection["continuity_valid"] is False
    assert projection["count"] == 0
    assert projection["observations"] == []
    assert "state_digest_mismatch" in projection["errors"]


def test_live_tag_or_certificate_drift_is_never_rendered_as_progress(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, 143)
    monkeypatch.setattr(
        observation,
        "_live_publication_errors",
        lambda _state: ["v143:publication_proof:completion_tag_missing"],
    )

    projection = observation.stability_observation_projection()

    assert projection["continuity_valid"] is False
    assert projection["count"] == 0
    assert projection["observations"] == []
    assert projection["errors"] == [
        "v143:publication_proof:completion_tag_missing"
    ]


def test_publication_without_remote_or_checkpoint_proof_is_rejected(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    result, checkpoint = _publication(143)
    result["push_ok"] = False

    with pytest.raises(
        observation.StabilityObservationError,
        match="publication_push_ok_not_proven",
    ):
        observation.record_published_generation(
            version=143,
            publication_result=result,
            publishing_checkpoint=checkpoint,
        )
    assert observation.stability_observation_projection()["count"] == 0
