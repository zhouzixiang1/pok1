import asyncio
import copy
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import time

import pytest

import national_native
from bot_artifact import hash_path
from bot_namespace import (
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_EPOCH_RECEIPT,
    ROLE_CANDIDATE,
    build_policy_epoch_receipt,
    build_runtime_manifest,
    resolve_national_bot_spec,
)


POLICY = """\
def get_baseline_decision(context):
    return {"kind": "pass"}


def iter_decisions(context, baseline, deadline):
    if False:
        yield baseline
"""


def test_system_owned_native_runtime_stays_within_publication_hard_cap():
    from code_verification import MAX_LINES_HARD_CAP

    assert len(national_native.NATIVE_BOT_TEMPLATE.splitlines()) <= (
        MAX_LINES_HARD_CAP
    )
    assert "\n\n\n" not in national_native.NATIVE_BOT_TEMPLATE


def _strict_bot(repo: Path, version: int, *, parents=()) -> Path:
    bot = repo / "bots" / f"national_v{version}"
    bot.mkdir(parents=True)
    national_native.ensure_native_entry(bot)
    (bot / "policy.py").write_text(POLICY, encoding="utf-8")
    manifest = build_runtime_manifest(bot)
    (bot / NATIONAL_RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    receipt = build_policy_epoch_receipt(
        bot,
        version,
        parent_versions=list(parents),
    )
    (bot / POLICY_EPOCH_RECEIPT).write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return bot


def test_resolve_bot_accepts_only_active_strict_policy_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot = _strict_bot(tmp_path, 143)

    assert national_native.resolve_bot("national_v143") == (
        "national_v143",
        bot.absolute(),
    )
    assert national_native.resolve_bot(bot / "national_bot.py") == (
        "national_v143",
        bot.absolute(),
    )

    for token in ("143", "v143", "bot143", "claude_v143"):
        with pytest.raises(ValueError):
            national_native.resolve_bot(token)
    archived = tmp_path / "archive" / "national_v142"
    with pytest.raises(ValueError, match="outside the active strict namespace"):
        national_native.resolve_bot(archived)


def test_native_spec_binds_original_artifact_without_copy_or_overlay(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot = _strict_bot(tmp_path, 143)
    before = hash_path(bot)

    spec = national_native._prepare_native_spec("national_v143", bot)

    assert spec.path == bot.absolute()
    assert spec.entry == bot.absolute() / "national_bot.py"
    assert spec.artifact_hash == before == hash_path(bot)
    identity = spec.execution_identity()
    assert identity["mode"] == "direct_content_bound_policy_artifact"
    assert identity["artifact_hash"] == before
    assert not hasattr(spec, "temp_root")


def test_candidate_abi_rejects_every_sixth_artifact_file(tmp_path):
    bot = _strict_bot(tmp_path, 143)
    assert resolve_national_bot_spec(
        bot,
        ROLE_CANDIDATE,
        repo_root=tmp_path,
    ).eligible

    (bot / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    spec = resolve_national_bot_spec(
        bot,
        ROLE_CANDIDATE,
        repo_root=tmp_path,
    )
    assert not spec.eligible
    assert "artifact_extra_file_forbidden:helper.py" in spec.issues


def test_strength_runner_passes_exact_content_bound_artifacts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))
    captured = {}
    progress = []

    class Lease:
        def release(self):
            captured["released"] = True

    async def acquire(*_args, **kwargs):
        captured["capacity_kwargs"] = kwargs
        captured["progress_before_capacity"] = list(progress)
        return Lease()

    async def execute(spec_a, spec_b, **kwargs):
        captured["paths"] = (spec_a.path, spec_b.path)
        captured["runner_kwargs"] = kwargs
        callback = kwargs["progress_callback"]
        await callback({
            "event_type": "engine_started",
            "hand": 1,
            "phase_started_at_epoch": 1_700_000_000.0,
        })
        await callback({
            "event_type": "settle",
            "hand": 70,
        })
        await callback({
            "event_type": "finalizing",
            "hand": 70,
            "phase_started_at_epoch": 1_700_005_000.0,
        })
        return {
            "artifact_execution": {
                "schema_version": 1,
                "mode": "direct_content_bound_policy_artifact",
                "by_player": {
                    spec_a.label: spec_a.execution_identity(),
                    spec_b.label: spec_b.execution_identity(),
                },
            }
        }

    monkeypatch.setattr(national_native, "acquire_match_slots_async", acquire)
    monkeypatch.setattr(national_native, "_run_tcp_server_with_processes", execute)

    async def report(item):
        progress.append(dict(item))

    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=420.0,
    )
    result = asyncio.run(national_native.run_native_strength_pair(
        bot_a,
        bot_b,
        70,
        timeout_sec=420.0,
        timing_plan=timing_plan,
        progress_callback=report,
    ))

    assert captured["paths"] == (bot_a.absolute(), bot_b.absolute())
    assert captured["released"] is True
    assert captured["capacity_kwargs"]["timeout"] == pytest.approx(
        timing_plan.capacity_queue_timeout_us / 1_000_000.0
    )
    assert captured["progress_before_capacity"][0]["event_type"] == "launching"
    assert captured["runner_kwargs"]["timing_plan"] == timing_plan
    assert progress[0]["event_type"] == "launching"
    assert progress[0]["hand"] is None
    assert progress[0]["liveness_phase"] == "launching"
    assert progress[0]["phase_budget_us"] == (
        timing_plan.launch_timeout_us
    )
    assert progress[0]["operation_budget_us"] == (
        timing_plan.first_strict_lease_timeout_us
    )
    engine_started = next(
        item for item in progress if item["event_type"] == "engine_started"
    )
    assert engine_started["liveness_phase"] == "engine_running"
    assert engine_started["phase_started_at_epoch"] == 1_700_000_000.0
    assert engine_started["phase_budget_us"] == timing_plan.effective_timeout_us
    finalizing = next(item for item in progress if item["event_type"] == "finalizing")
    assert finalizing["hand"] == 70
    assert finalizing["liveness_phase"] == "finalizing"
    assert finalizing["phase_budget_us"] == timing_plan.finalization_timeout_us
    assert progress[-1]["terminal"] is True
    assert progress[-1]["event_type"] == "terminal"
    assert sum(1 for item in progress if item.get("terminal") is True) == 1
    assert result["native_match_timing_plan"] == timing_plan.snapshot()
    assert result["native_match_timing_plan_digest"] == timing_plan.digest()
    assert result["native_full_match_liveness_budget"] == (
        timing_plan.liveness_budget_snapshot()
    )
    assert national_native._artifact_execution_is_valid(
        result["artifact_execution"],
        {
            "national_v143": hash_path(bot_a),
            "national_v144": hash_path(bot_b),
        },
    )


def test_strength_runner_clears_launch_progress_when_capacity_wait_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))
    progress = []

    async def acquire(*_args, **_kwargs):
        raise TimeoutError("capacity unavailable")

    async def report(item):
        progress.append(dict(item))

    monkeypatch.setattr(national_native, "acquire_match_slots_async", acquire)
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=420.0,
    )

    with pytest.raises(TimeoutError, match="capacity unavailable"):
        asyncio.run(national_native.run_native_strength_pair(
            bot_a,
            bot_b,
            70,
            timeout_sec=420.0,
            timing_plan=timing_plan,
            progress_callback=report,
        ))

    assert [row["event_type"] for row in progress] == ["launching", "terminal"]
    assert progress[0]["hand"] is None
    assert progress[0]["phase_budget_us"] == (
        timing_plan.launch_timeout_us
    )
    assert progress[1]["match_identity_digest"] == (
        progress[0]["match_identity_digest"]
    )
    assert progress[1]["terminal_outcome"] == "runner_raised"


def test_strength_runner_publishes_one_outer_terminal_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))
    progress = []

    class Lease:
        def release(self):
            pass

    async def acquire(*_args, **_kwargs):
        return Lease()

    async def execute(spec_a, spec_b, **kwargs):
        return {
            "artifact_execution": {
                "schema_version": 1,
                "mode": "direct_content_bound_policy_artifact",
                "by_player": {
                    spec_a.label: spec_a.execution_identity(),
                    spec_b.label: spec_b.execution_identity(),
                },
            }
        }

    async def report(item):
        progress.append(dict(item))
        return True

    monkeypatch.setattr(national_native, "acquire_match_slots_async", acquire)
    monkeypatch.setattr(national_native, "_run_tcp_server_with_processes", execute)
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=420.0,
    )

    asyncio.run(national_native.run_native_strength_pair(
        bot_a,
        bot_b,
        70,
        timeout_sec=420.0,
        timing_plan=timing_plan,
        progress_callback=report,
    ))

    terminal_rows = [row for row in progress if row.get("terminal") is True]
    assert len(terminal_rows) == 1
    assert terminal_rows[0]["terminal_outcome"] == "runner_returned"
    assert len(terminal_rows[0]["match_identity_digest"]) == 64


def test_native_artifact_preparation_timeout_is_enforced(tmp_path, monkeypatch):
    bot = _strict_bot(tmp_path, 143)
    monkeypatch.setattr(
        national_native,
        "NATIVE_ARTIFACT_PREPARATION_PER_BOT_TIMEOUT_SEC",
        0.01,
    )
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=420.0,
    )

    def slow_prepare(*_args, **_kwargs):
        __import__("time").sleep(0.05)
        return object()

    monkeypatch.setattr(national_native, "_prepare_native_spec", slow_prepare)
    with pytest.raises(
        RuntimeError,
        match="native_artifact_preparation_timeout:national_v143",
    ):
        asyncio.run(national_native._prepare_native_spec_bounded(
            "national_v143",
            bot,
            timing_plan=timing_plan,
        ))


def test_native_startup_watchdog_uses_one_absolute_monotonic_deadline(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))
    spec_a = national_native._prepare_native_spec("national_v143", bot_a)
    spec_b = national_native._prepare_native_spec("national_v144", bot_b)
    plan = replace(
        national_native.build_native_match_timing_plan(
            hands=70,
            requested_timeout_sec=420.0,
        ),
        startup_timeout_us=10_000,
    )
    observed_connect_timeouts = []
    cleanup = {"waited": 0, "killed": 0}

    class Lease:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Endpoint:
        @classmethod
        def connect(cls, *_args, timeout, **_kwargs):
            observed_connect_timeouts.append(timeout)
            return Lease()

    class Socket:
        def getsockname(self):
            return ("127.0.0.1", 12345)

    class Server:
        sockets = [Socket()]

        def close(self):
            pass

        async def wait_closed(self):
            return None

    async def start_server(*_args, **_kwargs):
        return Server()

    class Proc:
        returncode = 0

        def wait(self, timeout):
            cleanup["waited"] += 1

        def kill(self):
            cleanup["killed"] += 1

    @dataclass
    class Isolation:
        mode: str = "test"

    @dataclass
    class Managed:
        process: object
        isolation: object

    def slow_launch(*_args, **_kwargs):
        time.sleep(0.03)
        return Managed(Proc(), Isolation())

    monkeypatch.setattr(national_native, "EndpointLease", Endpoint)
    monkeypatch.setattr(national_native, "launch_managed_bot", slow_launch)
    monkeypatch.setattr(national_native.asyncio, "start_server", start_server)

    result = asyncio.run(national_native._run_tcp_server_with_processes(
        spec_a,
        spec_b,
        hands=70,
        timing_plan=plan,
        deck_seed_base=1,
        bot_seed_base=2,
    ))

    assert result["native_match_timeout_phase"] == "startup_watchdog"
    assert any(
        "NativeMatchStartupTimeout" in issue for issue in result["issues"]
    )
    assert len(observed_connect_timeouts) == 1
    assert 0 < observed_connect_timeouts[0] <= 0.01
    assert cleanup["waited"] == 1
    assert cleanup["killed"] == 0


def test_native_startup_watchdog_covers_server_bind_before_any_client_launch(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))
    spec_a = national_native._prepare_native_spec("national_v143", bot_a)
    spec_b = national_native._prepare_native_spec("national_v144", bot_b)
    plan = replace(
        national_native.build_native_match_timing_plan(
            hands=70,
            requested_timeout_sec=420.0,
        ),
        startup_timeout_us=10_000,
    )
    observed = {
        "bind_started": 0,
        "bind_cancelled": 0,
        "launches": 0,
        "removed_log_roots": [],
    }

    async def blocking_start_server(*_args, **_kwargs):
        observed["bind_started"] += 1
        try:
            await asyncio.Future()
        finally:
            observed["bind_cancelled"] += 1

    def forbidden_launch(*_args, **_kwargs):
        observed["launches"] += 1
        raise AssertionError("client launch must not precede a successful bind")

    real_rmtree = national_native.shutil.rmtree

    def tracked_rmtree(path, *args, **kwargs):
        observed["removed_log_roots"].append(Path(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(national_native.asyncio, "start_server", blocking_start_server)
    monkeypatch.setattr(national_native, "launch_managed_bot", forbidden_launch)
    monkeypatch.setattr(national_native.shutil, "rmtree", tracked_rmtree)

    result = asyncio.run(national_native._run_tcp_server_with_processes(
        spec_a,
        spec_b,
        hands=70,
        timing_plan=plan,
        deck_seed_base=1,
        bot_seed_base=2,
    ))

    assert result["native_match_timeout_phase"] == "startup_watchdog"
    assert any(
        "NativeMatchStartupTimeout: native TCP startup watchdog expired" in issue
        for issue in result["issues"]
    )
    assert observed["bind_started"] == 1
    assert observed["bind_cancelled"] == 1
    assert observed["launches"] == 0
    assert len(observed["removed_log_roots"]) == 1
    assert not observed["removed_log_roots"][0].exists()


def test_native_fast_server_bind_oserror_is_not_reclassified_as_timeout(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))
    spec_a = national_native._prepare_native_spec("national_v143", bot_a)
    spec_b = national_native._prepare_native_spec("national_v144", bot_b)
    plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=420.0,
    )
    launches = []

    async def failing_start_server(*_args, **_kwargs):
        raise OSError("test bind failed")

    def forbidden_launch(*_args, **_kwargs):
        launches.append(True)
        raise AssertionError("client launch must not follow a failed bind")

    monkeypatch.setattr(national_native.asyncio, "start_server", failing_start_server)
    monkeypatch.setattr(national_native, "launch_managed_bot", forbidden_launch)

    result = asyncio.run(national_native._run_tcp_server_with_processes(
        spec_a,
        spec_b,
        hands=70,
        timing_plan=plan,
        deck_seed_base=1,
        bot_seed_base=2,
    ))

    assert result["native_match_timeout_phase"] is None
    assert any(
        "OSError: test bind failed" in issue
        for issue in result["issues"]
    )
    assert not any(
        "NativeMatchStartupTimeout" in issue for issue in result["issues"]
    )
    assert launches == []


def test_launch_heartbeat_refreshes_freshness_without_rolling_deadline(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    monkeypatch.setattr(
        national_native,
        "NATIVE_LAUNCH_HEARTBEAT_INTERVAL_SEC",
        0.01,
    )
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))
    progress = []

    class Lease:
        def release(self):
            pass

    async def acquire(*_args, **_kwargs):
        await asyncio.sleep(0.035)
        return Lease()

    async def execute(spec_a, spec_b, **kwargs):
        callback = kwargs["progress_callback"]
        await callback({
            "event_type": "engine_started",
            "hand": 1,
            "phase_started_at_epoch": time.time(),
        })
        await asyncio.sleep(0.03)
        return {
            "artifact_execution": {
                "schema_version": 1,
                "mode": "direct_content_bound_policy_artifact",
                "by_player": {
                    spec_a.label: spec_a.execution_identity(),
                    spec_b.label: spec_b.execution_identity(),
                },
            }
        }

    async def report(item):
        progress.append(dict(item))
        return True

    monkeypatch.setattr(national_native, "acquire_match_slots_async", acquire)
    monkeypatch.setattr(national_native, "_run_tcp_server_with_processes", execute)
    plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=420.0,
    )
    asyncio.run(national_native.run_native_strength_pair(
        bot_a,
        bot_b,
        70,
        timeout_sec=420.0,
        timing_plan=plan,
        progress_callback=report,
    ))

    launching = [row for row in progress if row["event_type"] == "launching"]
    assert len(launching) >= 4
    assert len({row["operation_started_at_epoch"] for row in launching}) == 1
    assert len({row["operation_deadline_epoch"] for row in launching}) == 1
    assert len({row["phase_started_at_epoch"] for row in launching}) == 1
    assert len({row["phase_deadline_epoch"] for row in launching}) == 1
    engine_index = next(
        index for index, row in enumerate(progress)
        if row["event_type"] == "engine_started"
    )
    assert not any(
        row["event_type"] == "launching" for row in progress[engine_index + 1:]
    )


def test_full_match_liveness_budget_respects_local_strength_envelope():
    one_hand = national_native.build_native_match_timing_plan(
        hands=1,
        requested_timeout_sec=None,
    )
    seventy_hands = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=600.0,
    )
    explicit_higher = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=2_000.0,
    )

    assert one_hand.effective_timeout_us / 1_000_000.0 == 136.5
    assert seventy_hands.requested_timeout_us / 1_000_000.0 == 600.0
    assert seventy_hands.effective_timeout_us / 1_000_000.0 == 5415.0
    assert seventy_hands.snapshot()["national_hand_action_request_cap"] == 34
    assert seventy_hands.snapshot()["engine_action_cap_per_betting_round"] == (
        national_native.MAX_ACTIONS_PER_BETTING_ROUND
    )
    assert seventy_hands.snapshot()["betting_rounds_per_hand"] == 4
    # The one-hand diagnostic uses the same real 34-action derivation as the
    # 70-hand run; the evidence projection must not claim a zero bound.
    assert one_hand.liveness_budget_snapshot()["decision_slots_per_hand"] == 34
    assert one_hand.liveness_budget_snapshot()["betting_rounds_per_hand"] == 4
    assert one_hand.liveness_budget_snapshot()["national_hand_action_request_cap"] == 34
    assert seventy_hands.startup_timeout_us == (
        3 * seventy_hands.connect_timeout_us
        + 2 * seventy_hands.name_timeout_us
    )
    assert seventy_hands.startup_timeout_us == 120_000_000
    assert seventy_hands.cleanup_timeout_us == (
        7 * seventy_hands.process_drain_timeout_us
    )
    assert seventy_hands.execution_timeout_us == (
        seventy_hands.startup_timeout_us
        + seventy_hands.effective_timeout_us
        + seventy_hands.finalization_timeout_us
    )
    assert seventy_hands.first_strict_lease_timeout_us == (
        seventy_hands.capacity_queue_timeout_us
        + seventy_hands.artifact_preparation_timeout_us
        + seventy_hands.execution_timeout_us
    )
    assert seventy_hands.artifact_preparation_timeout_us == (
        2 * seventy_hands.artifact_preparation_per_bot_timeout_us
    )
    assert seventy_hands.launch_timeout_us == (
        seventy_hands.capacity_queue_timeout_us
        + seventy_hands.artifact_preparation_timeout_us
        + seventy_hands.startup_timeout_us
    )
    assert seventy_hands.finalization_timeout_us == (
        seventy_hands.cleanup_timeout_us
        + seventy_hands.post_execution_completion_timeout_us
    )
    assert seventy_hands.first_strict_lease_timeout_us == 5_960_000_000
    assert seventy_hands.capacity_queue_timeout_us > 60_000_000
    assert explicit_higher.effective_timeout_us == (
        seventy_hands.effective_timeout_us
    )


def test_full_match_timing_plan_rejects_parent_or_explicit_timing_overrides(
    monkeypatch,
):
    monkeypatch.setenv("POK_NATIVE_LOCAL_ACTION_DELAY", "2")
    overrides_a = {
        "POK_NATIVE_DECISION_HARD_DEADLINE_SEC": 1.0,
        "POK_NATIVE_DECISION_REFINEMENT_BUDGET_SEC": 0.9,
        "POK_NATIVE_DECISION_BASELINE_TARGET_SEC": 0.2,
    }
    baseline = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=420.0,
    )
    assert national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=420.0,
    ) == baseline
    with pytest.raises(ValueError, match="fixed system"):
        national_native.native_full_match_timeout_budget(
            70,
            420.0,
            bot_a_env_overrides=overrides_a,
        )
    with pytest.raises(ValueError, match="fixed by NativeMatchTimingPlan"):
        national_native._validate_formal_native_env_overrides(
            "bot_a",
            overrides_a,
        )
    forged = baseline.snapshot()
    forged["bot_a"]["hard_deadline_us"] += 1
    with pytest.raises(ValueError, match="does not bind system profile"):
        national_native.require_native_match_timing_plan(
            forged,
            hands=70,
            requested_timeout_sec=420.0,
        )


def test_immutable_rating_cycle_uses_shared_full_match_budget(
    tmp_path, monkeypatch
):
    import bot_artifact
    import elo_daemon

    captured = {}

    async def run_pair(*_args, **kwargs):
        captured.update(kwargs)
        plan = kwargs["timing_plan"]
        return {
            "hands_played": 70,
            "passed_compliance": True,
            "issues": [],
            "artifact_execution": {},
            "net_chips_a": 0,
            "native_match_timing_plan": plan.snapshot(),
            "native_match_timing_plan_digest": plan.digest(),
            "native_full_match_liveness_budget": plan.liveness_budget_snapshot(),
            "native_match_timeout_phase": None,
            "native_terminal_abort": None,
        }

    monkeypatch.setattr(national_native, "run_native_strength_pair", run_pair)
    monkeypatch.setattr(
        national_native,
        "_artifact_execution_is_valid",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: "fixture-hash")

    result = elo_daemon._run_national_rating_match(
        "national_v143",
        "national_v144",
        tmp_path / "national_v143" / "national_bot.py",
        tmp_path / "national_v144" / "national_bot.py",
        {
            "national_hands": 70,
            "national_matches": 1,
            "national_execution_mode": "native_tcp",
            "native_match_timing_plan": national_native.build_native_match_timing_plan(
                hands=70,
                requested_timeout_sec=None,
            ).snapshot(),
            "native_match_timing_plan_digest": national_native.build_native_match_timing_plan(
                hands=70,
                requested_timeout_sec=None,
            ).digest(),
        },
        persist_strength=False,
    )

    assert captured["timeout_sec"] is None
    assert captured["timing_plan"].digest() == (
        national_native.build_native_match_timing_plan(
            hands=70,
            requested_timeout_sec=None,
        ).digest()
    )
    assert result[6] is None


def test_full_match_liveness_timeout_remains_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))

    class Lease:
        def release(self):
            pass

    async def acquire(*_args, **_kwargs):
        return Lease()

    async def execute(*_args, **_kwargs):
        return {
            "hands_played": 69,
            "passed_compliance": False,
            "issues": ["native_tcp_match_error=TimeoutError: "],
            "native_match_timeout_phase": "whole_match_liveness",
        }

    monkeypatch.setattr(national_native, "acquire_match_slots_async", acquire)
    monkeypatch.setattr(national_native, "_run_tcp_server_with_processes", execute)

    result = asyncio.run(
        national_native.run_native_strength_pair(bot_a, bot_b, 70, timeout_sec=600.0)
    )

    assert result["passed_compliance"] is False
    assert any(
        issue.startswith("native_full_match_liveness_budget_exceeded:")
        for issue in result["issues"]
    )


def test_handshake_timeout_is_not_mislabeled_as_full_match_liveness(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))

    class Lease:
        def release(self):
            pass

    async def acquire(*_args, **_kwargs):
        return Lease()

    async def execute(*_args, **_kwargs):
        return {
            "hands_played": 0,
            "passed_compliance": False,
            "issues": ["native_tcp_match_error=TimeoutError: name handshake"],
            "native_match_timeout_phase": None,
        }

    monkeypatch.setattr(national_native, "acquire_match_slots_async", acquire)
    monkeypatch.setattr(national_native, "_run_tcp_server_with_processes", execute)

    result = asyncio.run(
        national_native.run_native_strength_pair(bot_a, bot_b, 70, timeout_sec=600.0)
    )

    assert result["passed_compliance"] is False
    assert not any(
        issue.startswith("native_full_match_liveness_budget_exceeded:")
        for issue in result["issues"]
    )


def test_first_strict_runner_journals_liveness_budget_before_outer_idempotent_completion(
    tmp_path, monkeypatch
):
    """The budget must be sealed before the first-strict journal commits it."""
    import first_strict_execution_journal as execution_journal

    def spec(label: str, folder: str) -> national_native.NativeBotSpec:
        path = tmp_path / folder
        path.mkdir()
        entry = path / "national_bot.py"
        entry.write_text("# system-owned test entry\n", encoding="utf-8")
        entry_digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        filler = "a" * 64
        return national_native.NativeBotSpec(
            label=label,
            path=path,
            entry=entry,
            artifact_hash=hash_path(path),
            entry_digest=entry_digest,
            policy_digest=filler,
            precompute_digest=filler,
            runtime_manifest_digest=filler,
            artifact_contract_digest=filler,
            epoch_receipt_digest=filler,
        )

    candidate = spec("national_v143", "candidate")
    control = spec("first_strict_control_v1", "control")
    scope = {
        "workflow_run_id": "generation:143:liveness-journal-test",
        "checkpoint_revision": 1,
        "candidate_version": 143,
        "candidate_label": candidate.label,
        "candidate_artifact_hash": candidate.artifact_hash,
        "control_id": control.label,
        "control_artifact_hash": control.artifact_hash,
        "control_receipt_digest": "b" * 64,
        "precommit_plan_digest": "c" * 64,
        "evaluation_contract_digest": "d" * 64,
        "native_match_timing_plan_digest": national_native.build_native_match_timing_plan(
            hands=70,
            requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
        ).digest(),
        "precommit_attempt": 1,
    }
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    settlements = []
    records = []
    events = []
    for hand in range(1, 71):
        settlement = {
            "hand": hand,
            "earnings": [1, -1],
            "pot": 2,
            "is_showdown": False,
            "winner_idx": 0,
            "reason": "fold",
        }
        settlements.append(settlement)
        records.append({"hand": hand, "settlement": dict(settlement)})
        events.append({"type": "settle", **settlement})

    async def fake_executor(spec_a, spec_b, **kwargs):
        assert kwargs["deck_seed_base"] == 17
        assert kwargs["bot_seed_base"] == 1_000_000_017
        return {
            "execution_mode": "native_tcp",
            "bot_a": spec_a.label,
            "bot_b": spec_b.label,
            "hands_requested": 70,
            "hands_played": 70,
            "deck_seed_base": 17,
            "bot_seed_base": 1_000_000_017,
            "net_chips_a": 70,
            "net_chips_b": -70,
            "passed_compliance": True,
            "issues": [],
            "settlements": settlements,
            "hand_records": records,
            "events": events,
            "artifact_execution": {
                "schema_version": 1,
                "mode": "direct_content_bound_policy_artifact",
                "by_player": {
                    spec_a.label: spec_a.execution_identity(),
                    spec_b.label: spec_b.execution_identity(),
                },
            },
        }

    monkeypatch.setattr(
        execution_journal,
        "CONTROL_EXECUTION_ROOT",
        tmp_path / "execution-journal",
    )
    # Create the ticket only after redirecting the durable authority.
    ticket = execution_journal.begin_control_execution(
        scope=scope,
        repeat=1,
        deck_seed_base=17,
        bot_seed_base=1_000_000_017,
        timing_plan=timing_plan,
    )
    runner, validate, consume = national_native._build_first_strict_runner_authority(
        fake_executor
    )
    monkeypatch.setattr(
        national_native,
        "_validate_first_strict_runner_execution_seal",
        validate,
    )
    monkeypatch.setattr(
        national_native,
        "_consume_first_strict_runner_execution_seal",
        consume,
    )

    execution = asyncio.run(runner(
        candidate,
        control,
        hands=70,
        timing_plan=timing_plan,
        deck_seed_base=17,
        bot_seed_base=1_000_000_017,
        capture_events=True,
        control_execution_ticket=ticket,
    ))

    assert execution["native_match_timing_plan"] == timing_plan.snapshot()
    # This is the real outer completion call used by precommit.  It is only
    # valid when the runner had already journaled the identical augmented body.
    receipt = execution_journal.complete_control_execution(ticket, execution=execution)
    evidence, issues = execution_journal.read_control_execution_receipt(
        receipt,
        expected_scope=scope,
    )
    assert issues == []
    assert evidence is not None
    assert evidence["execution"] == execution

    aborted = copy.deepcopy(execution)
    aborted["native_terminal_abort"] = {
        "code": "national_20000_chip_hand_action_limit_exceeded"
    }
    aborted["issues"] = []
    aborted["passed_compliance"] = True
    terminal_issues, _proof = execution_journal._terminal_execution_issues(
        aborted,
        deck_seed_base=17,
        bot_seed_base=1_000_000_017,
        timing_plan=timing_plan,
    )
    assert "first_strict_execution_native_terminal_abort_present" in terminal_issues

    drifted = copy.deepcopy(execution)
    drifted["native_match_timing_plan"]["effective_timeout_us"] += 1
    with pytest.raises(
        execution_journal.FirstStrictExecutionJournalError,
        match="first_strict_execution_completed_replay_binding_invalid",
    ):
        execution_journal.complete_control_execution(ticket, execution=drifted)
