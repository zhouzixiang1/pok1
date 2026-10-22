import asyncio
import json
import os
import subprocess
import threading
import time

import pytest

from bot_namespace import (
    EVOLUTION_BRANCH,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    bot_tag,
    high_water_tag,
)
from conftest import STRICT_TARGET_V

# Branch-portable publication sequence.  The strict policy epoch's first
# publication is STRICT_TARGET_V (143 on main, 1 on the cloud branch); every
# literal version and tag below is expressed relative to it so the same
# sequence exercises the observer on both branches.  Tag/dir names use the
# namespace-aware helpers because the active tag prefix differs by branch
# (national-bot-v on main, national-cloud-bot-v on cloud).
V0 = STRICT_TARGET_V


def _identity(*, boot="boot-a", contract="a" * 64):
    return {
        "evaluation_epoch": "national_tcp_policy_v1",
        "process_boot_id": boot,
        "process_pid": "4242",
        "process_start_ticks": "777",
        "infrastructure_contract_hash": contract,
        "runtime_config_digest": "c" * 64,
        # Initial head is the pre-strict archived high-water commit (V0 - 1);
        # the first strict publication (V0) must advance the repository head
        # past it.  On main this is 142 -> 143; on cloud 0 -> 1.
        "repository_head": f"{V0 - 1:040x}",
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
                f"refs/tags/{bot_tag(version)}": "a" * 40,
                f"refs/tags/{bot_tag(version)}^{{}}": f"{version:040x}",
                f"refs/tags/{high_water_tag(version)}": "b" * 40,
                f"refs/tags/{high_water_tag(version)}^{{}}": f"{version:040x}",
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
    # Save the real implementation BEFORE the stub replaces it: the
    # twenty-third exact-pin defect (v585, 2026-10-21) hid in the real body
    # for two months precisely because every test consumed this stub.
    monkeypatch.setattr(
        observation,
        "_test_original_live_publication_errors",
        observation._live_publication_errors,
        raising=False,
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
        published_range = range(V0, V0 + 40)
        requested_versions = [
            version
            for version in published_range
            if any(
                f"{bot_tag(version)}" in str(arg)
                for arg in args
            )
        ]
        remote_main = (
            f"{max(requested_versions):040x}"
            if requested_versions
            else "f" * 40
        )
        lines = [f"{remote_main}\trefs/heads/{EVOLUTION_BRANCH}"]
        for version in published_range:
            commit_oid = f"{version:040x}"
            lines.extend((
                f"{'a' * 40}\trefs/tags/{bot_tag(version)}",
                f"{commit_oid}\trefs/tags/{bot_tag(version)}^{{}}",
                f"{'b' * 40}\trefs/tags/{high_water_tag(version)}",
                f"{commit_oid}\trefs/tags/{high_water_tag(version)}^{{}}",
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
            if int(version) == FIRST_STRICT_POLICY_VERSION
            else {
                "schema_version": 1,
                "mode": "singleton_strict_successor_bootstrap",
                "reason": "single_strict_parent_no_peer_pool",
                "source_v": int(source_v),
                "strength_evidence_admitted": False,
                "strength_evidence_weight": 0,
            }
            if int(version) == FIRST_STRICT_POLICY_VERSION + 1
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
                "cycle_active_bots": [bot_name(int(source_v))],
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


def test_cached_projection_prefetch_keeps_current_value_fresh_until_reverified(
    monkeypatch,
):
    import stability_observation as observation

    observation.invalidate_stability_projection_cache()
    clock = [1000.0]
    calls = []
    monkeypatch.setattr(observation, "_now", lambda: clock[0])

    def projection():
        calls.append(clock[0])
        return _projection_fixture(count=len(calls))

    monkeypatch.setattr(observation, "stability_observation_projection", projection)
    assert observation.stability_observation_cached_projection()["verification"][
        "state"
    ] == "pending"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        first = observation.stability_observation_cached_projection()
        if first["verification"]["state"] == "fresh":
            break
        time.sleep(0.01)
    assert first["verification"]["state"] == "fresh"
    old_deadline = first["verification"]["fresh_until"]
    assert old_deadline == 1000.0 + observation.STABILITY_VERIFICATION_TTL_SEC

    clock[0] = old_deadline - observation.STABILITY_VERIFICATION_PREFETCH_LEAD_SEC + 1
    prefetch = observation.stability_observation_cached_projection(
        prefetch_lead_sec=observation.STABILITY_VERIFICATION_PREFETCH_LEAD_SEC,
    )
    assert prefetch["verification"]["state"] == "fresh"
    assert prefetch["count"] == 1

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        refreshed = observation.stability_observation_cached_projection(
            prefetch_lead_sec=observation.STABILITY_VERIFICATION_PREFETCH_LEAD_SEC,
        )
        if refreshed["count"] == 2:
            break
        time.sleep(0.01)
    assert refreshed["verification"]["state"] == "fresh"
    assert refreshed["verification"]["fresh_until"] > old_deadline
    assert calls == [1000.0, clock[0]]

    # Repeated prefetch readers use the same single-flight result rather than
    # opening another verifier before the next lead window.
    observation.stability_observation_cached_projection(
        prefetch_lead_sec=observation.STABILITY_VERIFICATION_PREFETCH_LEAD_SEC,
    )
    assert calls == [1000.0, clock[0]]
    clock[0] = old_deadline + 0.1
    assert observation.stability_observation_cached_projection()["verification"][
        "state"
    ] == "fresh"


def test_orchestrator_stability_maintenance_is_lifecycle_bound(monkeypatch):
    import orchestrator
    from shutdown_manager import ShutdownManager

    calls = []

    async def fake_blocking(function, /, *args, **kwargs):
        calls.append((function, args, kwargs))
        return function(*args)

    monkeypatch.setattr(orchestrator, "run_blocking_isolated", fake_blocking)
    monkeypatch.setattr(
        orchestrator,
        "_stability_projection_maintenance_tick",
        lambda: None,
    )

    async def exercise():
        shutdown = ShutdownManager()
        task = asyncio.create_task(
            orchestrator._stability_projection_maintenance_coroutine(
                shutdown,
                check_interval=60,
            )
        )
        deadline = time.monotonic() + 1
        while not calls and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert calls
        shutdown.request_shutdown()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise())
    assert calls == [
        (
            orchestrator._stability_projection_maintenance_tick,
            (),
            {"thread_name_prefix": "stability-observation-maintenance"},
        )
    ]


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


def test_background_base_exception_releases_single_flight_for_retry(monkeypatch):
    import stability_observation as observation

    class VerifierAbort(BaseException):
        pass

    observation.invalidate_stability_projection_cache()
    monkeypatch.setattr(
        observation,
        "stability_observation_projection",
        lambda: (_ for _ in ()).throw(VerifierAbort("cancelled verifier")),
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
    assert "VerifierAbort" in failed["verification"]["error"]

    observation.invalidate_stability_projection_cache()
    monkeypatch.setattr(
        observation,
        "stability_observation_projection",
        lambda: _projection_fixture(count=2),
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        recovered = observation.stability_observation_cached_projection()
        if recovered["verification"]["state"] == "fresh":
            break
        time.sleep(0.01)
    assert recovered["verification"]["state"] == "fresh"
    assert recovered["count"] == 2


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
    _record(observation, V0)
    current["runtime_config_digest"] = "e" * 64

    projection = observation.stability_observation_projection()

    assert projection["status"] == "reset_required"
    assert projection["count"] == 0
    assert projection["identity_mismatches"] == ["runtime_config_digest"]


def test_repository_head_or_branch_drift_immediately_hides_streak(monkeypatch):
    import stability_observation as observation

    current = _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, V0)
    _record(observation, V0 + 1)

    current["repository_head"] = "e" * 40
    # Twenty-sixth defect (2026-10-22): the recorded head may stay an ANCESTOR
    # of the current HEAD (post-publication deploy advance); only a divergent
    # HEAD is a mismatch.  Pin BOTH directions with a realistic merge-base
    # answer -- the autouse stub tolerates everything and cannot decide
    # ancestry.
    monkeypatch.setattr(
        observation,
        "_git_command_succeeds",
        lambda *args: (
            len(args) >= 4
            and args[0] == "merge-base"
            and args[1] == "--is-ancestor"
            and False
        ),
    )
    projection = observation.stability_observation_projection()
    assert projection["count"] == 0
    assert projection["continuity_valid"] is False
    assert projection["identity_mismatches"] == ["repository_head"]
    assert projection["recorded_repository_head"] == f"{V0 + 1:040x}"
    assert projection["current_repository_head"] == "e" * 40

    # Ancestor direction: a legitimate post-publication deploy advanced HEAD
    # past the recorded publication commit -- the streak must survive.
    advanced = "f" * 40
    current["repository_head"] = advanced
    monkeypatch.setattr(
        observation,
        "_git_command_succeeds",
        lambda *args: (
            len(args) >= 4
            and args[0] == "merge-base"
            and args[1] == "--is-ancestor"
            and args[2] == f"{V0 + 1:040x}"
            and args[3] == advanced
        ),
    )
    kept = observation.stability_observation_projection()
    assert kept["identity_mismatches"] == []
    assert kept["count"] == 2
    assert kept["continuity_valid"] is True

    current["repository_head"] = f"{V0 + 1:040x}"
    current["repository_branch"] = "unexpected-worktree"
    projection = observation.stability_observation_projection()
    assert projection["count"] == 0
    assert projection["identity_mismatches"] == ["repository_branch"]


def test_intervening_repository_commits_persist_reset_before_new_row(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, V0)
    _record(observation, V0 + 1)

    original_git = observation._git

    def drifted_git(*args, **kwargs):
        if args[:2] == ("rev-list", "--count"):
            return "2"
        return original_git(*args, **kwargs)

    monkeypatch.setattr(observation, "_git", drifted_git)
    projection = _record(observation, V0 + 2)

    assert projection["count"] == 1
    assert [row["version"] for row in projection["observations"]] == [V0 + 2]
    assert projection["last_reset_reason"] == "repository_head_drift"
    assert projection["reset_history"][-1]["previous_count"] == 2
    assert projection["reset_history"][-1]["details"]["issues"] == [
        "publication_repository_advance_not_single_commit"
    ]


def test_publication_requires_local_and_remote_main_at_exact_commit(monkeypatch):
    import stability_observation as observation

    current = _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    result, checkpoint = _publication(V0)
    # 2026-10-17 HEAD-advance rule: the autouse daemon-identity fixture stubs
    # ``_git_command_succeeds`` to True (tolerating ANY head drift through the
    # ancestor seam); pin it to a realistic "no fake oid is an ancestor" so a
    # non-ancestral repository head / remote main still fails closed here.
    monkeypatch.setattr(
        observation,
        "_git_command_succeeds",
        lambda *_args: False,
    )

    with pytest.raises(
        observation.StabilityObservationError,
        match="publication_repository_head_mismatch",
    ):
        observation.record_published_generation(
            version=V0,
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
            version=V0,
            publication_result=result,
            publishing_checkpoint=checkpoint,
        )


def test_record_time_remote_main_advance_keeps_row_valid(monkeypatch):
    """Nineteenth exact-pin convergence defect (v579, 2026-10-20).

    The frozen row's ``remote_main_oid`` records origin's tip AT RECORD TIME.
    When an operator merge/push legitimately advanced origin past the
    publication commit before the row was recorded (v579: merge 9a66db1b
    reconciling operator fixes with publication commit 50a02077), the row
    permanently carries a tip != commit_oid and the row validator's exact
    equality returned ``observation_N_remote_main_not_publication_commit`` on
    every later read — count=0, wedging the post-publication handoff's
    stability reproof forever.  The row must stay valid when the recorded tip
    is ancestor-proven (the publication path already proved
    ``merge-base --is-ancestor commit_oid remote_main`` at record time); a
    non-ancestral tip still fails closed.
    """

    import stability_observation as observation

    current = _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    result, checkpoint = _publication(V0)
    current["repository_head"] = result["commit_oid"]
    # v579 shape: origin's tip at record time is an operator merge commit,
    # not the publication commit itself.
    merge_tip = "e" * 40
    result["remote_proof"]["remote_main_oid"] = merge_tip
    projection = observation.record_published_generation(
        version=V0,
        publication_result=result,
        publishing_checkpoint=checkpoint,
    )
    assert projection["count"] == 1
    assert (
        projection["observations"][0]["remote_publication"]["remote_main_oid"]
        == merge_tip
    )

    # Isolate the row-internal check: no live remote/daemon drift noise.
    monkeypatch.setattr(
        observation, "_remote_publication_errors", lambda _state: []
    )
    monkeypatch.setattr(
        observation, "_live_daemon_errors", lambda _state: []
    )

    def ancestor_only(*args):
        return (
            len(args) >= 4
            and args[0] == "merge-base"
            and args[1] == "--is-ancestor"
            and args[2] == result["commit_oid"]
            and args[3] == merge_tip
        )

    monkeypatch.setattr(observation, "_git_command_succeeds", ancestor_only)
    valid = observation.stability_observation_projection()
    assert "observation_0_remote_main_not_publication_commit" not in (
        valid["errors"]
    )
    assert valid["count"] == 1
    assert valid["status"] == "observing"

    # A tip that is NOT an ancestor of the publication commit still fails
    # closed exactly as before.
    monkeypatch.setattr(
        observation, "_git_command_succeeds", lambda *_args: False
    )
    failed = observation.stability_observation_projection()
    assert "observation_0_remote_main_not_publication_commit" in (
        failed["errors"]
    )
    assert failed["count"] == 0
    assert failed["status"] == "reset_required"


def test_first_publication_after_restart_with_head_already_at_commit(monkeypatch):
    """A process/daemon restart after the publication commit exists but before
    it is recorded must not block the first observation row.

    The observer resets on restart and captures repository_head = current HEAD.
    If the publication commit already is HEAD (committed during the prior
    process), the head-advance guard must not reject the first row: there is no
    prior observation in this streak whose head could have "advanced".  The
    preflight already proved current_head == publication_commit, so the same-
    head first row is the correct content-bound anchor.
    """

    import stability_observation as observation

    current = _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    # Simulate the publication commit already being HEAD (as happens when a
    # restart occurs after commit but before stability observation records it).
    current["repository_head"] = f"{V0:040x}"
    projection = _record(observation, V0)
    assert projection["count"] == 1
    assert [row["version"] for row in projection["observations"]] == [V0]
    # The streak is anchored at the publication commit and remains valid.
    assert projection["continuity_valid"] is True


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


def test_runtime_configuration_rejects_daemon_pairs_above_evaluation_cap():
    import stability_observation as observation

    observation.clear_runtime_configuration_binding()
    with pytest.raises(
        observation.StabilityObservationError,
        match="runtime_config_daemon_pairs_invalid",
    ):
        observation.bind_runtime_configuration({
            "daemon_enabled": True,
            "daemon_workers": 4,
            "daemon_pairs": 9,
        })


def test_ten_consecutive_publications_complete_and_duplicate_is_idempotent(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    started = observation.initialize_stability_observation()
    assert started["count"] == 0

    for version in range(V0, V0 + 10):
        projection = _record(observation, version)

    assert projection["count"] == 10
    assert projection["remaining"] == 0
    assert projection["complete"] is True
    continuity_id = projection["continuity_id"]

    duplicate = _record(observation, V0 + 9)
    assert duplicate["count"] == 10
    assert duplicate["continuity_id"] == continuity_id
    assert [row["version"] for row in duplicate["observations"]] == list(
        range(V0, V0 + 10)
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
    for version in range(V0, V0 + 10):
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
    _record(observation, V0)
    _record(observation, V0 + 1)

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
    _record(observation, V0)
    _record(observation, V0 + 1)

    current["repository_branch"] = "unexpected-worktree"
    result, checkpoint = _publication(V0 + 1)
    replay = observation.record_published_generation(
        version=V0 + 1,
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
    _record(observation, V0)

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
    _record(observation, V0)
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
        _record(observation, V0 + 1)


def test_contract_drift_projection_is_read_only_and_fails_closed(monkeypatch):
    import stability_observation as observation

    current = _identity()
    _install_identity(monkeypatch, observation, current)
    observation.initialize_stability_observation()
    _record(observation, V0)
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
    _record(observation, V0)
    _record(observation, V0 + 1)

    gap = _record(observation, V0 + 3)
    assert gap["count"] == 1
    assert gap["observations"][0]["version"] == V0 + 3
    assert gap["last_reset_reason"] == "publication_version_gap"

    repaired = _record(observation, V0 + 4, repaired=True)
    assert repaired["count"] == 1
    assert repaired["observations"][0]["version"] == V0 + 4
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
    _record(observation, V0)
    _record(observation, V0 + 1)

    current_daemon[0] = "e" * 64
    restarted = _record(observation, V0 + 2)

    assert restarted["count"] == 1
    assert restarted["observations"][0]["version"] == V0 + 2
    assert restarted["last_reset_reason"] == "rating_daemon_restart_detected"


def test_live_daemon_or_remote_ref_drift_invalidates_projection(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, V0)

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
        lambda _state: [f"v{V0}:remote_tag_object_mismatch:{bot_tag(V0)}"],
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
    _record(observation, V0)
    state = json.loads(observation.STATE_FILE.read_text(encoding="utf-8"))
    row = state["observations"][0]
    remote = row["remote_publication"]
    lines = [f"{state['repository_head']}\trefs/heads/{EVOLUTION_BRANCH}"]
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

    # Twenty-second exact-pin convergence defect (2026-10-20): the main-head
    # leg is now the convergence-family rule (equal or descending), and a
    # live tip that is not locally resolvable is skipped.  Isolate the one
    # fail-closed shape here: pin the tip-known probe True and every ancestry
    # predicate False, so the swapped remote main is a locally KNOWN
    # non-descendant — the only shape that must still report the token.
    def known_tip_but_non_ancestor(*args):
        return tuple(args[:2]) == ("cat-file", "-e")

    monkeypatch.setattr(
        observation,
        "_git_command_succeeds",
        known_tip_but_non_ancestor,
    )
    lines[0] = f"{'f' * 40}\trefs/heads/{EVOLUTION_BRANCH}"
    assert "remote_main_head_mismatch" in observation._remote_publication_errors(
        state
    )
    lines[0] = f"{state['repository_head']}\trefs/heads/{EVOLUTION_BRANCH}"
    monkeypatch.setattr(
        observation,
        "_git_command_succeeds",
        lambda *args: True,
    )

    completion_index = next(
        index
        for index, line in enumerate(lines)
        if line.endswith(f"\trefs/tags/{bot_tag(V0)}")
    )
    lines[completion_index] = f"{'0' * 40}\trefs/tags/{bot_tag(V0)}"
    errors = observation._remote_publication_errors(state)
    assert f"v{V0}:remote_tag_object_mismatch:{bot_tag(V0)}" in errors


def test_tampered_state_is_never_rendered_as_progress(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    _record(observation, V0)
    state = json.loads(observation.STATE_FILE.read_text(encoding="utf-8"))
    state["observations"][0]["version"] = V0 + 50
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
    _record(observation, V0)
    monkeypatch.setattr(
        observation,
        "_live_publication_errors",
        lambda _state: [f"v{V0}:publication_proof:completion_tag_missing"],
    )

    projection = observation.stability_observation_projection()

    assert projection["continuity_valid"] is False
    assert projection["count"] == 0
    assert projection["observations"] == []
    assert projection["errors"] == [
        f"v{V0}:publication_proof:completion_tag_missing"
    ]


def test_publication_without_remote_or_checkpoint_proof_is_rejected(monkeypatch):
    import stability_observation as observation

    _install_identity(monkeypatch, observation)
    observation.initialize_stability_observation()
    result, checkpoint = _publication(V0)
    result["push_ok"] = False

    with pytest.raises(
        observation.StabilityObservationError,
        match="publication_push_ok_not_proven",
    ):
        observation.record_published_generation(
            version=V0,
            publication_result=result,
            publishing_checkpoint=checkpoint,
        )
    assert observation.stability_observation_projection()["count"] == 0


# --- Twenty-second exact-pin convergence defect (v579 wedge, 2026-10-20) ---
#
# The LIVE remote re-proof (``_remote_publication_errors``) must tolerate an
# operator who legitimately advanced ``origin/<branch>`` past the recorded
# repository head AFTER the row was frozen (v579: origin's tip was the
# reconciling merge 9a66db1b while the runtime's recorded head was the
# publication commit 50a02077), without fetching in the read path and without
# weakening the fail-closed legs.  These fixtures therefore drive REAL
# scratch git repositories (bare origin + operator clone + runtime clone,
# plain local paths, fully offline), point ``evolution_infra.PROJECT_ROOT``
# at the runtime clone — the real ``_git``/``_git_command_succeeds``
# delegates resolve their cwd from it at call time — and write the
# observation row through the REAL ``record_published_generation`` with a
# ``remote_proof`` shaped exactly like ``verify_remote_bot_publication``
# output: annotated tags (tag object oid != peeled commit oid) and a
# merge-commit ``remote_main_oid``.  The autouse fixtures' fake
# ``_git``/``_git_command_succeeds`` are deliberately overridden back to the
# real delegates because rc=128 (object absent locally) vs rc=1 (genuine
# non-ancestor) is exactly the behaviour under test.


def _scratch_git(workdir, *args, check=True):
    """Real offline git plumbing for the scratch publication world."""

    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    })
    result = subprocess.run(
        [
            "git",
            "-C",
            str(workdir),
            "-c",
            "user.email=observer@example.invalid",
            "-c",
            "user.name=observer",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


class _PublicationWorld:
    """Offline scratch topology mirroring the production publication shape."""

    def __init__(self, root):
        branch = EVOLUTION_BRANCH
        root.mkdir(parents=True, exist_ok=True)
        self.branch = branch
        self.origin = root / "origin.git"
        _scratch_git(root, "init", "--bare", "-b", branch, "origin.git")
        # The branch-delete leg (T6) needs the scratch origin to accept
        # deleting its HEAD branch; production origins accept this too.
        _scratch_git(self.origin, "config", "receive.denyDeleteCurrent", "ignore")
        seed = root / "seed"
        _scratch_git(root, "init", "-b", branch, "seed")
        (seed / "README.md").write_text("base\n", encoding="utf-8")
        _scratch_git(seed, "add", "-A")
        _scratch_git(seed, "commit", "-m", "base")
        _scratch_git(seed, "remote", "add", "origin", str(self.origin))
        _scratch_git(seed, "push", "-q", "origin", f"refs/heads/{branch}")
        self.runtime = root / "runtime"
        _scratch_git(
            root, "clone", "-q", "--no-hardlinks", str(self.origin), "runtime"
        )
        self.operator = root / "operator"
        _scratch_git(
            root, "clone", "-q", "--no-hardlinks", str(self.origin), "operator"
        )
        # v{V0} publication: the runtime commits P, creates BOTH annotated
        # completion tags (tag object oid != peeled commit oid), and pushes
        # the branch and the tags to origin.
        (self.runtime / "bot.txt").write_text(
            f"publication v{V0}\n", encoding="utf-8"
        )
        _scratch_git(self.runtime, "add", "-A")
        _scratch_git(self.runtime, "commit", "-m", f"publish v{V0}")
        self.publication_commit = _scratch_git(
            self.runtime, "rev-parse", "HEAD"
        )
        for name in (bot_tag(V0), high_water_tag(V0)):
            _scratch_git(self.runtime, "tag", "-a", name, "-m", name)
        _scratch_git(
            self.runtime,
            "push",
            "-q",
            "origin",
            f"refs/heads/{branch}",
            f"refs/tags/{bot_tag(V0)}",
            f"refs/tags/{high_water_tag(V0)}",
        )
        # Snapshot clone taken while origin's tip is still the publication
        # commit: it knows P and the tags but never any later operator tip.
        self.snapshot_runtime = root / "runtime_without_merge"
        _scratch_git(
            root,
            "clone",
            "-q",
            "--no-hardlinks",
            str(self.origin),
            "runtime_without_merge",
        )
        self.merge_tip = None

    def operator_merge_advances_origin(self):
        """v579 shape: operator fixes diverge from the base, then a merge of
        the publication commit is pushed — origin's tip is a MERGE commit
        (not P) already at record time."""

        _scratch_git(self.operator, "checkout", "-q", "-b", "operator-fixes")
        (self.operator / "fix.txt").write_text(
            "operator fix\n", encoding="utf-8"
        )
        _scratch_git(self.operator, "add", "-A")
        _scratch_git(self.operator, "commit", "-m", "operator fixes")
        _scratch_git(
            self.operator,
            "fetch",
            "-q",
            "--no-tags",
            "origin",
            f"refs/heads/{self.branch}:refs/remotes/origin/{self.branch}",
        )
        _scratch_git(
            self.operator,
            "merge",
            f"refs/remotes/origin/{self.branch}",
            "-m",
            "reconcile operator fixes with publication",
        )
        _scratch_git(
            self.operator,
            "push",
            "-q",
            "origin",
            f"HEAD:refs/heads/{self.branch}",
        )
        self.merge_tip = _scratch_git(self.operator, "rev-parse", "HEAD")

    def operator_advance_origin(self, filename):
        """One more ordinary commit on the operator line, pushed."""

        (self.operator / filename).write_text(f"{filename}\n", encoding="utf-8")
        _scratch_git(self.operator, "add", "-A")
        _scratch_git(self.operator, "commit", "-m", filename)
        tip = _scratch_git(self.operator, "rev-parse", "HEAD")
        _scratch_git(
            self.operator,
            "push",
            "-q",
            "origin",
            f"HEAD:refs/heads/{self.branch}",
        )
        return tip

    def operator_force_push_unrelated_root(self):
        """Force-push an unrelated root commit as the branch tip."""

        _scratch_git(self.operator, "checkout", "-q", "--orphan", "unrelated")
        _scratch_git(self.operator, "rm", "-r", "-q", "-f", "--", ".")
        (self.operator / "unrelated.txt").write_text(
            "unrelated root\n", encoding="utf-8"
        )
        _scratch_git(self.operator, "add", "-A")
        _scratch_git(self.operator, "commit", "-m", "unrelated root")
        _scratch_git(
            self.operator,
            "push",
            "-q",
            "-f",
            "origin",
            f"HEAD:refs/heads/{self.branch}",
        )
        return _scratch_git(self.operator, "rev-parse", "HEAD")

    def operator_retag_completion_tag(self, target):
        """Rewrite the completion tag onto ``target`` (annotated, force).

        The operator clone predates the publication push and the single-ref
        fetches never fetch tags, so pull the live tag down first (this is
        the operator's own privileged action — the RUNTIME must stay
        tagless by fetch construction)."""

        name = bot_tag(V0)
        _scratch_git(
            self.operator,
            "fetch",
            "-q",
            "origin",
            f"+refs/tags/{name}:refs/tags/{name}",
        )
        _scratch_git(self.operator, "tag", "-d", name)
        _scratch_git(self.operator, "tag", "-a", name, target, "-m", "rewritten")
        _scratch_git(
            self.operator, "push", "-q", "-f", "origin", f"refs/tags/{name}"
        )

    def operator_delete_branch(self):
        _scratch_git(
            self.operator, "push", "-q", "origin", "--delete", self.branch
        )

    def runtime_fetch_main(self, *, force=False):
        """The publication path's exact single-ref ``--no-tags`` fetch."""

        leading = "+" if force else ""
        _scratch_git(
            self.runtime,
            "fetch",
            "-q",
            "--no-tags",
            "origin",
            (
                f"{leading}refs/heads/{self.branch}:"
                f"refs/remotes/origin/{self.branch}"
            ),
        )

    def completion_tag_object_oids(self):
        return {
            name: _scratch_git(self.runtime, "rev-parse", f"refs/tags/{name}")
            for name in (bot_tag(V0), high_water_tag(V0))
        }


def _use_real_git(monkeypatch, observation, project_root):
    """Restore the REAL git delegates and steer them at a scratch checkout."""

    import evolution_infra

    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(observation, "_git", evolution_infra._git)
    monkeypatch.setattr(
        observation,
        "_git_command_succeeds",
        evolution_infra._git_command_succeeds,
    )


def _record_scratch_publication(monkeypatch, observation, world):
    """Record the v{V0} row through the REAL record path with a
    verify_remote_bot_publication-shaped remote_proof (merge tip + annotated
    tag object oids, peeled to the publication commit)."""

    current = dict(_identity())
    current["repository_head"] = world.publication_commit
    _install_identity(monkeypatch, observation, current)
    observation.initialize_stability_observation()
    result, checkpoint = _publication(V0)
    tag_objects = world.completion_tag_object_oids()
    result["commit_oid"] = world.publication_commit
    result["remote_proof"] = {
        "valid": True,
        "remote_main_oid": world.merge_tip,
        "remote_refs": {
            f"refs/tags/{bot_tag(V0)}": tag_objects[bot_tag(V0)],
            f"refs/tags/{bot_tag(V0)}^{{}}": world.publication_commit,
            f"refs/tags/{high_water_tag(V0)}": tag_objects[high_water_tag(V0)],
            f"refs/tags/{high_water_tag(V0)}^{{}}": world.publication_commit,
        },
    }
    return observation.record_published_generation(
        version=V0,
        publication_result=result,
        publishing_checkpoint=checkpoint,
    )


def _ready_v579_world(monkeypatch, tmp_path):
    """World at the exact v579 record-time shape + one recorded row."""

    import stability_observation as observation

    world = _PublicationWorld(tmp_path / "world")
    world.operator_merge_advances_origin()
    world.runtime_fetch_main()
    _use_real_git(monkeypatch, observation, world.runtime)
    recorded = _record_scratch_publication(monkeypatch, observation, world)
    assert recorded["count"] == 1
    return observation, world


def test_v579_operator_advance_keeps_live_reproof_green_without_fetch(
    monkeypatch,
    tmp_path,
):
    """Twenty-second exact-pin convergence defect (v579 wedge, 2026-10-20).

    T1 replays the exact v579 shape: origin's tip at record time is the
    operator's reconciling MERGE commit M (not the publication commit P),
    the runtime knows M through the publication path's single-ref fetch, and
    the recorded head is P.  The old exact-equality live re-proof reported
    ``remote_main_head_mismatch`` from the moment the row was recorded and
    permanently wedged the post-publication handoff's stability reproof
    (count=0).  T2: the operator pushes one more ordinary commit F and the
    runtime does NOT fetch — git exits 128 for the locally absent object and
    the old code additionally misread that as a genuine
    ``publication_commit_not_on_remote_main``.  T3: after the producer-style
    fetch the known+descendant legs re-prove cleanly.  In every phase the
    projection must stay green with the row rendered (T7), so the handoff
    reproof's expected-count comparison and publication-row presence check
    both pass.
    """

    observation, world = _ready_v579_world(monkeypatch, tmp_path)

    # T1 — v579 replay: no error may fire (old code: remote_main_head_mismatch).
    projection = observation.stability_observation_projection()
    assert projection["errors"] == []
    assert projection["count"] == 1
    assert projection["status"] == "observing"
    assert [row["version"] for row in projection["observations"]] == [V0]

    # T2 — operator pushes F; the runtime does NOT fetch.
    world.operator_advance_origin("operator-followup.txt")
    projection = observation.stability_observation_projection()
    assert projection["errors"] == []
    assert "remote_main_head_mismatch" not in projection["errors"]
    assert (
        f"v{V0}:publication_commit_not_on_remote_main"
        not in projection["errors"]
    )
    assert projection["count"] == 1
    assert projection["observations"]

    # T3 — runtime fetches F with the producer's exact single-ref fetch.
    world.runtime_fetch_main()
    projection = observation.stability_observation_projection()
    assert projection["errors"] == []
    assert projection["count"] == 1
    assert projection["status"] == "observing"


def test_live_reproof_fails_closed_on_locally_known_unrelated_main(
    monkeypatch,
    tmp_path,
):
    """T4 — fail-closed preserved: a force-pushed unrelated root that the
    runtime HAS fetched is locally known and non-descendant, so BOTH
    main-ancestry legs must still fail closed while the untouched tag
    proofs stay green."""

    observation, world = _ready_v579_world(monkeypatch, tmp_path)

    world.operator_force_push_unrelated_root()
    world.runtime_fetch_main(force=True)
    projection = observation.stability_observation_projection()
    assert "remote_main_head_mismatch" in projection["errors"]
    assert f"v{V0}:publication_commit_not_on_remote_main" in projection[
        "errors"
    ]
    assert not any("remote_tag_" in item for item in projection["errors"])
    assert projection["count"] == 0
    assert projection["status"] == "reset_required"


def test_live_tag_proof_stays_unconditional_when_main_tip_unknown(
    monkeypatch,
    tmp_path,
):
    """T5 — guardrail: the remote tag string checks are the one
    unconditional live proof and never depend on local objects (the
    publication-path fetch is ``--no-tags``, so tag objects are normally
    absent locally).  With the main tip unknown to the runtime, a rewritten
    completion tag — new tag object AND peeled commit pointing at a commit
    the runtime has never fetched — must still fail closed; only the two
    main-ancestry legs may skip."""

    observation, world = _ready_v579_world(monkeypatch, tmp_path)

    followup = world.operator_advance_origin("operator-followup.txt")
    world.operator_retag_completion_tag(followup)
    projection = observation.stability_observation_projection()
    errors = projection["errors"]
    assert f"v{V0}:remote_tag_object_mismatch:{bot_tag(V0)}" in errors
    assert f"v{V0}:remote_tag_peeled_mismatch:{bot_tag(V0)}" in errors
    assert (
        f"v{V0}:remote_tag_object_mismatch:{high_water_tag(V0)}" not in errors
    )
    assert "remote_main_head_mismatch" not in errors
    assert f"v{V0}:publication_commit_not_on_remote_main" not in errors
    assert projection["count"] == 0
    assert projection["status"] == "reset_required"


def test_live_reproof_reports_missing_remote_branch(monkeypatch, tmp_path):
    """T6 — a deleted remote branch still fails closed exactly as before."""

    observation, world = _ready_v579_world(monkeypatch, tmp_path)

    world.operator_delete_branch()
    projection = observation.stability_observation_projection()
    assert projection["errors"] == ["remote_main_missing"]
    assert projection["count"] == 0


def test_frozen_row_remote_main_stays_fail_closed_when_locally_unknown(
    monkeypatch,
    tmp_path,
):
    """T8 — boundary: only the LIVE re-proof degrades.  The frozen row's own
    tip check in ``_state_errors`` keeps the nineteenth defect's fail-closed
    posture: reading the SAME recorded state from a checkout that has P and
    the tags but never fetched the merge tip must report
    ``observation_0_remote_main_not_publication_commit`` (count=0) while the
    live main-ancestry legs skip the locally unknown tip."""

    observation, world = _ready_v579_world(monkeypatch, tmp_path)
    world.operator_advance_origin("operator-followup.txt")

    _use_real_git(monkeypatch, observation, world.snapshot_runtime)
    projection = observation.stability_observation_projection()
    assert "observation_0_remote_main_not_publication_commit" in projection[
        "errors"
    ]
    assert "remote_main_head_mismatch" not in projection["errors"]
    assert (
        f"v{V0}:publication_commit_not_on_remote_main"
        not in projection["errors"]
    )
    assert projection["count"] == 0
    assert projection["status"] == "reset_required"


def test_live_publication_proof_passes_empty_certificate_path(monkeypatch):
    """Twenty-third exact-pin convergence defect (v585, 2026-10-21).

    The certification-removal rewrite (2026-08-08) dropped the REQUIRED
    keyword-only ``certificate_path`` argument from the stability observer's
    ``validate_completion_tag`` call, so every execution of the real
    ``_live_publication_errors`` raised TypeError — surfaced as
    ``live_publication_validation_failed:TypeError`` in the cached projection,
    count=0, wedging v585's post-publication handoff stability reproof for
    hours.  The bug survived two months of green suites because this file's
    autouse fixture stubs the whole function; this regression drives the REAL
    implementation and pins the post-removal contract: native-tier rows carry
    no certificate, so the validator must be called with the EMPTY path
    (skipping the tag's certificate-tree checks while keeping tag/commit/
    metadata validation)."""

    import bot_artifact
    import stability_observation as observation

    captured: dict = {}

    def fake_validate(path_or_token, *, expected_metadata, certificate_path):
        captured["certificate_path"] = certificate_path
        captured["expected_metadata"] = expected_metadata
        return {
            "valid": True,
            "issues": [],
            "identity": {"commit_oid": "a" * 40},
        }

    monkeypatch.setattr(bot_artifact, "validate_completion_tag", fake_validate)
    frozen_evidence = {"schema_version": 1, "mode": "frozen_native_evaluation"}
    monkeypatch.setattr(
        observation,
        "_generation_evidence_binding",
        lambda version, source_v: frozen_evidence,
    )
    state = {
        "observations": [
            {
                "version": V0,
                "candidate_artifact_hash": f"{V0 + 2:064x}",
                "commit_oid": "a" * 40,
                "source_v": V0 - 1,
                "generation_evidence": frozen_evidence,
            }
        ]
    }

    errors = observation._test_original_live_publication_errors(state)

    assert errors == []
    assert captured["certificate_path"] == ""
    assert captured["expected_metadata"] == {
        "official-candidate-hash": f"{V0 + 2:064x}",
    }

    # The validator's own verdict still flows through fail-closed.
    monkeypatch.setattr(
        bot_artifact,
        "validate_completion_tag",
        lambda *_a, **_k: {
            "valid": False,
            "issues": ["completion_tag_not_annotated"],
            "identity": {"commit_oid": "a" * 40},
        },
    )
    failed = observation._test_original_live_publication_errors(state)
    assert f"v{V0}:publication_proof:completion_tag_not_annotated" in failed


def test_live_publication_proof_accepts_staging_candidate_hash_spelling(
    monkeypatch,
):
    """Twenty-fourth exact-pin convergence defect (v598, 2026-10-22).

    Publication tags in the no-certificate era carry MIXED metadata
    spellings: v598's tag writes ``staging-candidate-hash`` while the
    stability observer's live proof asks validate_completion_tag for
    ``official-candidate-hash`` — the VALUE is byte-identical, the exact-key
    loop still failed, and v598's post-publication handoff exhausted its 8
    archivist retries and parked the pipeline for ~3h.  The validator now
    accepts the staging-* spelling as an alias for the official-* key (value
    still exact-match).  Drives the REAL _live_publication_errors with a
    state whose row hash equals the tag's staging-candidate-hash value."""


    import bot_artifact
    import stability_observation as observation

    row_hash = "e" * 64

    def fake_validate(path_or_token, *, expected_metadata, certificate_path):
        # Simulate the validator's post-alias behaviour: the official-*
        # expectation resolves through the staging-* tag spelling.
        assert certificate_path == ""
        got = expected_metadata.get("official-candidate-hash")
        assert got == row_hash
        return {
            "valid": True,
            "issues": [],
            "identity": {"commit_oid": "a" * 40, "tag_artifact_hash": row_hash},
        }

    monkeypatch.setattr(bot_artifact, "validate_completion_tag", fake_validate)
    frozen_evidence = {"schema_version": 1, "mode": "frozen_native_evaluation"}
    monkeypatch.setattr(
        observation,
        "_generation_evidence_binding",
        lambda version, source_v: frozen_evidence,
    )
    state = {
        "observations": [
            {
                "version": V0,
                "candidate_artifact_hash": row_hash,
                "commit_oid": "a" * 40,
                "source_v": V0 - 1,
                "generation_evidence": frozen_evidence,
            }
        ]
    }
    errors = observation._test_original_live_publication_errors(state)
    assert errors == []
