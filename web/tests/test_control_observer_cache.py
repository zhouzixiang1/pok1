import asyncio
import concurrent.futures
import copy
import threading
import time

import pytest


def test_observer_cache_cooperative_await_same_key_follower_single_builder():
    from server.routes.control import (
        _ObserverSingleflightCache,
    )

    cache = _ObserverSingleflightCache(ttl_sec=1.0)
    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def builder():
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return {"nested": {"value": 1}}

    # The owner starts the build on its own worker thread and blocks inside the
    # builder.  A same-key follower that arrives while the build is in flight no
    # longer fails fast: it cooperatively awaits the single in-flight build and
    # receives the same frozen snapshot, instead of returning 503 to every poll
    # during a long (multi-second) projection build.
    follower_result: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cache.get, builder, key="same-authority")
        # Wait until the owner is confirmed in flight, then submit a follower.
        # The follower cooperatively awaits the in-flight build; it must NOT
        # start a second builder and must NOT fail fast with 503.
        assert entered.wait(timeout=1)
        follower = pool.submit(
            _same_key_follower, cache, builder, "same-authority",
            None, follower_result, ready_immediately=True,
        )
        # While the owner is still blocked, give the follower time to enter and
        # park on the condition.  It cannot have returned: the build has not
        # completed (release is still unset), so a non-cooperative follower would
        # have raised _ObserverProjectionUnavailable here.
        time.sleep(0.1)
        assert "error" not in follower_result
        assert follower_result.get("value") is None
        # Completing the owner build resolves the cooperative follower too.
        release.set()
        first = owner.result(timeout=2)
        follower.result(timeout=2)

    assert calls == 1
    second = follower_result.get("value")
    assert second == {"nested": {"value": 1}}
    # Each caller receives an independent deepcopy; mutating one never mutates
    # the other, and the follower does not invoke the builder itself.
    first["nested"]["value"] = 99
    assert second["nested"]["value"] == 1
    # After the build, the cache serves the frozen value without rebuilding.
    third = cache.get(builder, key="same-authority")
    assert third == {"nested": {"value": 1}}
    assert calls == 1


def _same_key_follower(
    cache, builder, key, started, out, *, ready_immediately=False
):
    """Run cache.get on a worker thread and stash its result/exception."""
    if not ready_immediately and started is not None:
        started.wait(timeout=2)
    try:
        out["value"] = cache.get(builder, key=key)
    except BaseException as exc:  # pragma: no cover - assert path
        out["error"] = exc


def test_observer_cache_same_key_follower_timeout_falls_back_to_retryable_503(
    monkeypatch,
):
    from server.routes.control import (
        _ObserverProjectionUnavailable,
        _ObserverSingleflightCache,
    )
    import server.routes.control as control

    # A same-key follower waits up to _OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC for
    # the in-flight build.  If the build cannot complete within that window, the
    # follower still surfaces a retryable 503 (never blocks the request forever
    # and never returns stale bytes for the wrong authority).
    monkeypatch.setattr(control, "_OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC", 0.1)

    cache = _ObserverSingleflightCache(ttl_sec=1.0)
    entered = threading.Event()
    release = threading.Event()

    def builder():
        entered.set()
        # The owner build blocks on `release` so it stays in flight well beyond
        # the shrunken 0.1s follower window.
        release.wait(timeout=10)
        return {"revision": 1}

    # Owner runs on its own daemon thread and blocks inside the builder.
    owner = threading.Thread(
        target=cache.get, args=(builder,), kwargs={"key": "same-authority"},
        name="owner", daemon=True,
    )
    owner.start()
    assert entered.wait(timeout=1)
    assert cache._inflight is True

    # The same-key follower parks on the condition and, after the 0.1s window
    # with the owner still in flight, raises a retryable refresh_in_progress.
    follower_error: dict = {}
    follower = threading.Thread(
        target=_same_key_follower,
        args=(cache, builder, "same-authority", None, follower_error),
        kwargs={"ready_immediately": True},
        name="follower", daemon=True,
    )
    follower.start()
    follower.join(timeout=2)
    assert not follower.is_alive()

    assert isinstance(follower_error.get("error"), _ObserverProjectionUnavailable)
    assert "refresh_in_progress" in str(follower_error["error"])

    # Let the owner build complete so the daemon thread can exit cleanly.
    release.set()
    owner.join(timeout=2)
    assert not owner.is_alive()


def test_zero_stale_cache_changed_key_never_waits_for_old_builder():
    from server.routes.control import (
        _ObserverProjectionUnavailable,
        _ObserverSingleflightCache,
    )

    cache = _ObserverSingleflightCache(
        ttl_sec=1.0,
        stale_while_revalidate_sec=0.0,
    )
    old_entered = threading.Event()
    old_release = threading.Event()
    latest_calls = 0

    def old_builder():
        old_entered.set()
        assert old_release.wait(timeout=2)
        return {"revision": 1}

    def latest_builder():
        nonlocal latest_calls
        latest_calls += 1
        return {"revision": 2}

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(cache.get, old_builder, key="content-a")
        assert old_entered.wait(timeout=1)
        started = time.monotonic()
        with pytest.raises(_ObserverProjectionUnavailable) as changed:
            cache.get(latest_builder, key="content-b")
        assert "authority_changed_during_refresh" in str(changed.value)
        assert time.monotonic() - started < 0.1
        old_release.set()
        with pytest.raises(_ObserverProjectionUnavailable):
            old.result(timeout=2)

    deadline = time.monotonic() + 2
    while cache._inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.get(latest_builder, key="content-b") == {"revision": 2}
    assert latest_calls == 1


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
    assert set(names).issubset({
        "ValueError",
        "RuntimeError",
        "_ObserverProjectionUnavailable",
    })


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
    # A follower for a *different* (superseding) key still fails closed: the
    # in-flight build belongs to the old authority and the new build has not
    # started yet, so the new authority's bytes must never be served here.
    with pytest.raises(_ObserverProjectionUnavailable) as changed:
        cache.get(latest_refresh, key="content-b")
    assert "authority_changed_during_refresh" in str(changed.value)

    old_release.set()
    assert latest_entered.wait(timeout=1)
    # Now the superseding (content-b) build is in flight.  A follower for the
    # *same* (new) key cooperatively awaits it and receives revision 3 once it
    # completes, instead of returning refresh_in_progress.
    follower_result: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        follower = pool.submit(
            _same_key_follower, cache, latest_refresh, "content-b",
            None, follower_result, ready_immediately=True,
        )
        # The superseding (content-b) build is still in flight (latest_release is
        # unset), so the same-key follower parks and awaits it instead of either
        # failing fast or starting a second build.
        time.sleep(0.1)
        assert "error" not in follower_result
        assert follower_result.get("value") is None
        latest_release.set()
        follower.result(timeout=2)

    assert follower_result.get("value") == {"revision": 3}
    # The superseding build ran exactly once; the cooperative follower did not
    # invoke it again.
    assert latest_calls == 1


def test_remote_publication_proof_is_singleflight_under_slow_origin(monkeypatch):
    import evolution_infra
    from bot_namespace import (
        ACTIVE_TAG_PREFIX,
        EVOLUTION_BRANCH,
        HIGH_WATER_TAG_PREFIX,
        bot_tag,
        high_water_tag,
    )
    from conftest import STRICT_TARGET_V

    version = STRICT_TARGET_V
    completion_tag = bot_tag(version)
    high_water = high_water_tag(version)
    tag_object = "a" * 40
    commit = "b" * 40
    water_object = "c" * 40
    remote_main = "d" * 40
    calls = 0
    calls_lock = threading.Lock()
    release = threading.Event()

    def fake_git(*args, check=True):
        nonlocal calls
        if args[:2] == ("rev-parse", f"refs/tags/{completion_tag}"):
            return tag_object
        if args[:2] == ("rev-parse", f"refs/tags/{completion_tag}^{{commit}}"):
            return commit
        if args[:2] == ("rev-parse", f"refs/tags/{high_water}"):
            return water_object
        if args[:2] == ("rev-parse", f"refs/tags/{high_water}^{{commit}}"):
            return commit
        if args[:2] == ("rev-parse", f"refs/remotes/origin/{EVOLUTION_BRANCH}"):
            return remote_main
        if args and args[0] == "ls-remote":
            with calls_lock:
                calls += 1
            assert release.wait(timeout=2)
            return "\n".join((
                f"{remote_main}\trefs/heads/{EVOLUTION_BRANCH}",
                f"{tag_object}\trefs/tags/{completion_tag}",
                f"{commit}\trefs/tags/{completion_tag}^{{}}",
                f"{water_object}\trefs/tags/{high_water}",
                f"{commit}\trefs/tags/{high_water}^{{}}",
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
        return evolution_infra._remote_published_completion_versions({version})

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(read) for _index in range(4)]
        deadline = time.monotonic() + 1
        while calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == 1
        release.set()
        assert [future.result(timeout=2) for future in futures] == [
            {version}, {version}, {version}, {version}
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
    monkeypatch.setattr(control, "_observer_control_status_snapshot", status_builder)
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
    monkeypatch.setattr(control, "_observer_control_status_snapshot", status_builder)
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

    observer_calls = 0
    fresh_calls = 0

    def fresh():
        nonlocal fresh_calls
        fresh_calls += 1
        return {"sample": f"fresh-{fresh_calls}"}

    def observer():
        nonlocal observer_calls
        observer_calls += 1
        return {"sample": f"observer-{observer_calls}"}

    control._invalidate_observer_projection_cache()
    monkeypatch.setattr(control, "_fresh_control_status_snapshot", fresh)
    monkeypatch.setattr(control, "_observer_control_status_snapshot", observer)
    monkeypatch.setattr(control, "_read_pipeline_health", lambda status: {"sample": status["sample"]})

    assert control._control_status_snapshot()["sample"] == "observer-1"
    assert control._control_status_snapshot()["sample"] == "observer-1"
    status, pipeline = control._control_launch_authority_snapshot()
    assert status["sample"] == "fresh-1"
    assert pipeline["sample"] == "fresh-1"
    assert observer_calls == 1
    assert fresh_calls == 1


def test_control_status_builders_separate_fresh_launch_from_cached_observer(
    monkeypatch,
):
    from server.routes import control

    observed = []
    monkeypatch.setattr(control.app_state, "to_dict", lambda: {"base": True})

    def sync(state, *, ledger_fresh=True):
        observed.append(ledger_fresh)
        return {**state, "ledger_fresh": ledger_fresh}

    monkeypatch.setattr(control, "_sync_evolution_fields", sync)

    assert control._fresh_control_status_snapshot()["ledger_fresh"] is True
    assert control._observer_control_status_snapshot()["ledger_fresh"] is False
    assert observed == [True, False]


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
