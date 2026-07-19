import asyncio
import concurrent.futures
import copy
import threading
import time

import pytest


def test_observer_cache_coalesces_concurrent_builds_and_returns_deep_copies():
    from server.routes.control import _ObserverSingleflightCache

    cache = _ObserverSingleflightCache(ttl_sec=1.0)
    calls = 0
    lock = threading.Lock()
    start = threading.Barrier(8)

    def builder():
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.05)
        return {"nested": {"value": 1}}

    def read():
        start.wait()
        return cache.get(builder, key="same-authority")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _index: read(), range(8)))
    assert calls == 1
    values[0]["nested"]["value"] = 99
    assert all(value["nested"]["value"] == 1 for value in values[1:])


def test_observer_cache_ttl_key_change_and_failure_are_fail_closed():
    from server.routes.control import _ObserverSingleflightCache

    cache = _ObserverSingleflightCache(ttl_sec=0.02)
    calls = 0

    def builder():
        nonlocal calls
        calls += 1
        return {"call": calls}

    assert cache.get(builder, key="a")["call"] == 1
    assert cache.get(builder, key="a")["call"] == 1
    assert cache.get(builder, key="b")["call"] == 2
    time.sleep(0.03)
    assert cache.get(builder, key="b")["call"] == 3

    failing = _ObserverSingleflightCache(ttl_sec=1.0)
    failures = 0

    def boom():
        nonlocal failures
        failures += 1
        time.sleep(0.03)
        raise ValueError("authority unavailable")

    def failed_read():
        try:
            failing.get(boom, key="same")
        except Exception as exc:  # every waiter must fail; no stale value exists
            return type(exc).__name__
        raise AssertionError("failure was converted into a snapshot")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        names = list(pool.map(lambda _index: failed_read(), range(4)))
    assert failures == 1
    assert set(names).issubset({"ValueError", "RuntimeError"})


def test_observer_cache_invalidation_during_build_never_returns_stale_value():
    from server.routes.control import _ObserverSingleflightCache

    cache = _ObserverSingleflightCache(ttl_sec=1.0)
    entered = threading.Event()
    release = threading.Event()

    def builder():
        entered.set()
        assert release.wait(timeout=2)
        return {"revision": 1}

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cache.get, builder, key="revision-1")
        assert entered.wait(timeout=2)
        cache.invalidate()
        release.set()
        try:
            future.result(timeout=2)
        except RuntimeError as exc:
            assert "invalidated_during_build" in str(exc)
        else:
            raise AssertionError("invalidated observer returned stale revision")


def test_observer_cache_serves_bounded_stale_and_rejects_content_key_drift():
    from server.routes.control import _ObserverSingleflightCache

    cache = _ObserverSingleflightCache(
        ttl_sec=0.01,
        stale_while_revalidate_sec=1.0,
    )
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    assert cache.get(lambda: {"revision": 1}, key="content-a") == {
        "revision": 1
    }
    time.sleep(0.02)

    def slow_remote_refresh():
        entered.set()
        assert release.wait(timeout=2)
        completed.set()
        return {"revision": 2}

    started = time.monotonic()
    # Same local authority gets the bounded prior proof immediately while one
    # background refresh owns the slow remote transaction.
    assert cache.get(slow_remote_refresh, key="content-a") == {"revision": 1}
    assert time.monotonic() - started < 0.1
    assert entered.wait(timeout=1)

    # A local checkpoint/tag/content movement must never consume that stale
    # projection or wait behind its unrelated remote request.
    started = time.monotonic()
    try:
        cache.get(lambda: {"revision": 3}, key="content-b")
    except RuntimeError as exc:
        assert "authority_changed_during_refresh" in str(exc)
    else:
        raise AssertionError("content-key drift consumed stale observer bytes")
    assert time.monotonic() - started < 0.1

    release.set()
    assert completed.wait(timeout=2)
    deadline = time.monotonic() + 2
    while cache._inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.get(lambda: {"revision": 3}, key="content-b") == {
        "revision": 3
    }


def test_observer_cache_hands_drift_to_one_latest_background_refresh():
    from server.routes.control import (
        _ObserverProjectionUnavailable,
        _ObserverSingleflightCache,
    )

    cache = _ObserverSingleflightCache(
        ttl_sec=1.0,
        stale_while_revalidate_sec=1.0,
    )
    old_entered = threading.Event()
    old_release = threading.Event()
    latest_entered = threading.Event()
    latest_release = threading.Event()
    latest_calls = 0

    assert cache.get(lambda: {"revision": 1}, key="content-a") == {
        "revision": 1
    }
    with cache._condition:
        cache._expires_at = time.monotonic() - 0.01

    def old_refresh():
        old_entered.set()
        assert old_release.wait(timeout=2)
        return {"revision": 2}

    def latest_refresh():
        nonlocal latest_calls
        latest_calls += 1
        latest_entered.set()
        assert latest_release.wait(timeout=2)
        return {"revision": 3}

    assert cache.get(old_refresh, key="content-a") == {"revision": 1}
    assert old_entered.wait(timeout=1)
    with pytest.raises(_ObserverProjectionUnavailable) as changed:
        cache.get(latest_refresh, key="content-b")
    assert "authority_changed_during_refresh" in str(changed.value)

    old_release.set()
    assert latest_entered.wait(timeout=1)
    started = time.monotonic()
    with pytest.raises(_ObserverProjectionUnavailable) as refreshing:
        cache.get(latest_refresh, key="content-b")
    assert "refresh_in_progress" in str(refreshing.value)
    assert time.monotonic() - started < 0.1

    latest_release.set()
    deadline = time.monotonic() + 2
    while cache._inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.get(latest_refresh, key="content-b") == {"revision": 3}
    assert latest_calls == 1


def test_remote_publication_proof_is_singleflight_under_slow_origin(monkeypatch):
    import evolution_infra

    tag_object = "a" * 40
    commit = "b" * 40
    water_object = "c" * 40
    remote_main = "d" * 40
    calls = 0
    calls_lock = threading.Lock()
    release = threading.Event()

    def fake_git(*args, check=True):
        nonlocal calls
        if args[:2] == ("rev-parse", "refs/tags/national-bot-v143"):
            return tag_object
        if args[:2] == ("rev-parse", "refs/tags/national-bot-v143^{commit}"):
            return commit
        if args[:2] == ("rev-parse", "refs/tags/national-high-water-v143"):
            return water_object
        if args[:2] == ("rev-parse", "refs/tags/national-high-water-v143^{commit}"):
            return commit
        if args[:2] == ("rev-parse", "refs/remotes/origin/main"):
            return remote_main
        if args and args[0] == "ls-remote":
            with calls_lock:
                calls += 1
            assert release.wait(timeout=2)
            return "\n".join((
                f"{remote_main}\trefs/heads/main",
                f"{tag_object}\trefs/tags/national-bot-v143",
                f"{commit}\trefs/tags/national-bot-v143^{{}}",
                f"{water_object}\trefs/tags/national-high-water-v143",
                f"{commit}\trefs/tags/national-high-water-v143^{{}}",
            ))
        if args and args[0] == "cat-file":
            return "tag"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(
        evolution_infra,
        "_git_command_succeeds",
        lambda *_args: True,
    )
    evolution_infra._clear_remote_publication_cache()
    start = threading.Barrier(4)

    def read():
        start.wait()
        return evolution_infra._remote_published_completion_versions({143})

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(read) for _index in range(4)]
        deadline = time.monotonic() + 1
        while calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == 1
        release.set()
        assert [future.result(timeout=2) for future in futures] == [
            {143}, {143}, {143}, {143}
        ]
    assert calls == 1
    evolution_infra._clear_remote_publication_cache()


def test_control_health_remains_responsive_during_slow_status_refresh(
    monkeypatch,
):
    from server.routes import control

    status_cache = control._ObserverSingleflightCache(
        ttl_sec=0.01,
        stale_while_revalidate_sec=1.0,
    )
    health_cache = control._ObserverSingleflightCache(ttl_sec=0.01)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    calls = 0

    def status_builder():
        nonlocal calls
        calls += 1
        if calls > 1:
            entered.set()
            assert release.wait(timeout=2)
            completed.set()
        return {"revision": calls}

    monkeypatch.setattr(control, "_OBSERVER_STATUS_CACHE", status_cache)
    monkeypatch.setattr(control, "_OBSERVER_HEALTH_CACHE", health_cache)
    monkeypatch.setattr(control, "_observer_cache_key", lambda **_kwargs: "same")
    monkeypatch.setattr(control, "_fresh_control_status_snapshot", status_builder)
    monkeypatch.setattr(
        control,
        "_health_summary",
        lambda status: {"overall": "healthy", "revision": status["revision"]},
    )

    assert asyncio.run(control.control_health()) == {
        "overall": "healthy",
        "revision": 1,
    }
    time.sleep(0.02)
    started = time.monotonic()
    assert asyncio.run(control.control_health()) == {
        "overall": "healthy",
        "revision": 1,
    }
    assert time.monotonic() - started < 0.1
    assert entered.wait(timeout=1)
    release.set()
    assert completed.wait(timeout=2)


def test_control_health_maps_expected_authority_drift_to_retryable_503(
    monkeypatch,
    client,
):
    from server.routes import control

    status_cache = control._ObserverSingleflightCache(
        ttl_sec=0.01,
        stale_while_revalidate_sec=1.0,
    )
    health_cache = control._ObserverSingleflightCache(ttl_sec=0.01)
    current_key = ["content-a"]
    old_entered = threading.Event()
    old_release = threading.Event()
    calls = 0

    def status_builder():
        nonlocal calls
        calls += 1
        if calls == 2:
            old_entered.set()
            assert old_release.wait(timeout=2)
        return {"revision": calls}

    monkeypatch.setattr(control, "_OBSERVER_STATUS_CACHE", status_cache)
    monkeypatch.setattr(control, "_OBSERVER_HEALTH_CACHE", health_cache)
    monkeypatch.setattr(
        control,
        "_observer_cache_key",
        lambda **_kwargs: current_key[0],
    )
    monkeypatch.setattr(control, "_fresh_control_status_snapshot", status_builder)
    monkeypatch.setattr(
        control,
        "_health_summary",
        lambda status: {"overall": "healthy", "revision": status["revision"]},
    )
    monkeypatch.setattr(control, "_OBSERVER_HTTP_RETRY_DELAY_SEC", 0.001)

    assert asyncio.run(control.control_health())["revision"] == 1
    time.sleep(0.02)
    assert asyncio.run(control.control_health())["revision"] == 1
    assert old_entered.wait(timeout=1)

    current_key[0] = "content-b"
    started = time.monotonic()
    unavailable = client.get("/api/control/health")
    assert time.monotonic() - started < 0.1
    assert unavailable.status_code == 503
    assert unavailable.headers["Retry-After"] == "1"
    assert unavailable.json()["detail"] == {
        "code": "observer_projection_refreshing",
        "reason": "observer_projection_authority_changed_during_refresh",
        "retryable": True,
        "authority": "strict_epoch_projection",
    }

    old_release.set()
    deadline = time.monotonic() + 2
    while status_cache._inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert asyncio.run(control.control_health())["revision"] == 3


def test_launch_barrier_uses_fresh_projection_not_observer_cache(monkeypatch):
    from server.routes import control

    calls = 0

    def fresh():
        nonlocal calls
        calls += 1
        return {"sample": calls}

    control._invalidate_observer_projection_cache()
    monkeypatch.setattr(control, "_fresh_control_status_snapshot", fresh)
    monkeypatch.setattr(control, "_read_pipeline_health", lambda status: {"sample": status["sample"]})

    assert control._control_status_snapshot()["sample"] == 1
    assert control._control_status_snapshot()["sample"] == 1
    status, pipeline = control._control_launch_authority_snapshot()
    assert status["sample"] == 2
    assert pipeline["sample"] == 2


def _transition_fixture():
    from epoch_authority import first_strict_operator_transition

    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": "official_bootstrap_required",
        "run_id": "143#0",
        "workflow_run_id": "generation:143:workflow-v68",
        "checkpoint_revision": 23,
        "audit_context": {
            "official_bootstrap_request": {
                "candidate_hash": "a" * 64,
                "request_digest": "b" * 64,
            },
        },
    }
    active = {
        key: checkpoint[key]
        for key in (
            "next_v", "source_v", "parent2_v", "stage", "run_id",
            "workflow_run_id", "checkpoint_revision",
        )
    }
    base = first_strict_operator_transition(checkpoint)
    epoch = {
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "fresh_bootstrap_ready",
        "initialized": True,
        "reset_receipt_digest": "c" * 64,
        "active_generation": active,
        "operator_transition": base,
    }
    return checkpoint, epoch, base


def test_control_refines_running_ready_and_failed_transition_only_for_exact_revision(monkeypatch):
    from epoch_authority import first_strict_operator_transition
    from server.routes import control

    checkpoint, epoch, base = _transition_fixture()
    job_id = "d" * 64
    transitions = (
        first_strict_operator_transition(checkpoint, state="bootstrap_running", job_id=job_id),
        first_strict_operator_transition(checkpoint, state="bootstrap_failed", job_id=job_id),
        first_strict_operator_transition(
            checkpoint,
            state="ready_to_finalize",
            job_id=job_id,
            certificate_digest="e" * 64,
        ),
    )
    for transition in transitions:
        monkeypatch.setattr(
            control,
            "_dynamic_first_strict_operator_transition",
            lambda _epoch, value=transition: value,
        )
        assert control._refined_operator_transition(
            epoch,
            resample=lambda: copy.deepcopy(epoch),
        ) == transition

    drifted = copy.deepcopy(epoch)
    drifted["active_generation"]["checkpoint_revision"] += 1
    monkeypatch.setattr(
        control,
        "_dynamic_first_strict_operator_transition",
        lambda _epoch: transitions[0],
    )
    assert control._refined_operator_transition(epoch, resample=lambda: drifted) == base

    wrong_workflow_checkpoint = {**checkpoint, "workflow_run_id": "generation:143:workflow-v67"}
    wrong = first_strict_operator_transition(
        wrong_workflow_checkpoint,
        state="bootstrap_running",
        job_id=job_id,
    )
    monkeypatch.setattr(control, "_dynamic_first_strict_operator_transition", lambda _epoch: wrong)
    assert control._refined_operator_transition(epoch, resample=lambda: epoch) == base
