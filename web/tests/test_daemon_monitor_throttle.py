"""Regression tests for the daemon monitor throttle and bundle cache.

The monitor thread used to poll every 3 seconds and rebuild the strict
evaluation read projection on every tick, SHA256-verifying the whole replay
corpus each time.  These tests pin:

- ``POK_DAEMON_MONITOR_INTERVAL_SEC`` parsing (default 30, clamp [3, 600],
  invalid values fall back to the default);
- the fingerprint-gated ``monitor_strict_evaluation_bundle`` read projection
  (cache hit on unchanged inputs, rebuild on any fingerprinted change, and
  failures are never cached);
- ``WebUI.update_daemon_status`` consuming the monitor-supplied
  ``strict_bundle`` instead of re-loading, while the ``strict_bundle=None``
  direct path stays uncached.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_monitor_bundle_cache():
    """Keep the module-global monitor cache from leaking across tests."""

    import daemon_management

    daemon_management.reset_monitor_strict_bundle_cache()
    yield
    daemon_management.reset_monitor_strict_bundle_cache()


def _make_results_dir(tmp_path):
    """Minimal results directory the fingerprint scanner can consume."""

    root = tmp_path / "results"
    (root / "evaluation_cycles").mkdir(parents=True)
    (root / "match_replay").mkdir()
    for name in (
        "policy_epoch_reset_receipt.json",
        "evaluation_data_manifest.json",
        "evaluation_cycle_manifest.json",
    ):
        (root / name).write_text("{}", encoding="utf-8")
    return root


def test_monitor_interval_defaults_to_30_seconds(monkeypatch):
    import daemon_management

    monkeypatch.delenv(
        daemon_management.DAEMON_MONITOR_INTERVAL_ENV, raising=False
    )

    assert daemon_management.daemon_monitor_interval_from_env() == 30.0


def test_monitor_interval_env_override_and_clamp(monkeypatch):
    import daemon_management

    parse = daemon_management.daemon_monitor_interval_from_env
    env = daemon_management.DAEMON_MONITOR_INTERVAL_ENV

    monkeypatch.setenv(env, "45")
    assert parse() == 45.0
    # Clamp boundaries are inclusive.
    monkeypatch.setenv(env, "3")
    assert parse() == 3.0
    monkeypatch.setenv(env, "600")
    assert parse() == 600.0
    # Below the floor clamps up instead of falling back.
    monkeypatch.setenv(env, "0.5")
    assert parse() == 3.0
    monkeypatch.setenv(env, "-5")
    assert parse() == 3.0
    # Above the ceiling clamps down.
    monkeypatch.setenv(env, "9999")
    assert parse() == 600.0


def test_monitor_interval_invalid_values_fall_back_to_default(monkeypatch):
    import daemon_management

    parse = daemon_management.daemon_monitor_interval_from_env
    env = daemon_management.DAEMON_MONITOR_INTERVAL_ENV

    for raw in ("nan", "inf", "-inf", "", "soon", "3s"):
        monkeypatch.setenv(env, raw)
        assert parse() == 30.0, raw


def test_monitor_thread_interval_constant_stays_in_clamped_range():
    import daemon_management

    assert (
        daemon_management.DAEMON_MONITOR_INTERVAL_MIN_SEC
        <= daemon_management.DAEMON_MONITOR_INTERVAL_SEC
        <= daemon_management.DAEMON_MONITOR_INTERVAL_MAX_SEC
    )
    # The import-time constant must stay derived from the env parser, not a
    # separately hardcoded value (monkeypatch restores the environment before
    # this runs, so it still matches the import-time value here).
    assert (
        daemon_management.DAEMON_MONITOR_INTERVAL_SEC
        == daemon_management.daemon_monitor_interval_from_env()
    )


def test_monitor_bundle_cache_hits_until_fingerprint_changes(
    tmp_path, monkeypatch
):
    import daemon_management
    import evaluation_bundle

    root = _make_results_dir(tmp_path)
    calls = []

    def _loader(load_root, *args, **kwargs):
        calls.append(load_root)
        return {"available": True, "build": len(calls) - 1}

    monkeypatch.setattr(
        evaluation_bundle, "load_current_strict_evaluation_bundle", _loader
    )
    monkeypatch.setattr(
        daemon_management, "_monitor_pool_fingerprint", lambda: ("stub",)
    )

    first = daemon_management.monitor_strict_evaluation_bundle(root)
    assert first == {"available": True, "build": 0}
    assert calls == [root]

    # Unchanged fingerprint: served from cache, no second expensive load.
    second = daemon_management.monitor_strict_evaluation_bundle(root)
    assert second is first
    assert len(calls) == 1

    # A size change on a fingerprinted top-level manifest rebuilds.
    (root / "evaluation_cycle_manifest.json").write_text(
        '{"committed_cycle": 2}', encoding="utf-8"
    )
    third = daemon_management.monitor_strict_evaluation_bundle(root)
    assert third == {"available": True, "build": 1}
    assert len(calls) == 2

    # A pure mtime_ns bump (no size change) rebuilds too.
    receipt = root / "policy_epoch_reset_receipt.json"
    newer_ns = receipt.stat().st_mtime_ns + 1_000_000
    os.utime(receipt, ns=(newer_ns, newer_ns))
    fourth = daemon_management.monitor_strict_evaluation_bundle(root)
    assert fourth == {"available": True, "build": 2}
    assert len(calls) == 3

    # A new raw replay entering the fingerprinted match_replay tree rebuilds.
    (root / "match_replay" / "hand_0001.json").write_text(
        "{}", encoding="utf-8"
    )
    fifth = daemon_management.monitor_strict_evaluation_bundle(root)
    assert fifth == {"available": True, "build": 3}
    assert len(calls) == 4

    # A new payload under evaluation_cycles rebuilds as well.
    (root / "evaluation_cycles" / "cycle_0002.json").write_text(
        "{}", encoding="utf-8"
    )
    sixth = daemon_management.monitor_strict_evaluation_bundle(root)
    assert sixth == {"available": True, "build": 4}
    assert len(calls) == 5

    # And the fresh build is cached again on the next tick.
    assert daemon_management.monitor_strict_evaluation_bundle(root) is sixth
    assert len(calls) == 5


def test_monitor_bundle_ignores_hidden_staging_entries(tmp_path, monkeypatch):
    """Hidden staging churn between cycle commits must not defeat the cache.

    The rating daemon stages each completed 70-hand match under
    ``match_replay/.pending/`` (admitted to the tree top level only at the
    next ``save_cycle``) and publishes cycles through a transient
    ``evaluation_cycles/.cycle-*`` temp directory.  The load chain never
    consumes either (replay ids are non-hidden top-level names, cycle
    directories are regex-validated), so they must not flip the fingerprint
    and trigger a full replay re-verification on every active-daemon tick.
    """

    import daemon_management
    import evaluation_bundle

    root = _make_results_dir(tmp_path)
    calls = []

    def _loader(load_root, *args, **kwargs):
        calls.append(load_root)
        return {"available": True, "build": len(calls) - 1}

    monkeypatch.setattr(
        evaluation_bundle, "load_current_strict_evaluation_bundle", _loader
    )
    monkeypatch.setattr(
        daemon_management, "_monitor_pool_fingerprint", lambda: ("stub",)
    )

    first = daemon_management.monitor_strict_evaluation_bundle(root)
    assert first == {"available": True, "build": 0}
    assert calls == [root]

    # Daemon activity between commits: staged replay, cycle publication
    # staging, and a stray hidden file are all invisible to the projection.
    pending = root / "match_replay" / ".pending"
    pending.mkdir()
    staged = pending / "match_stage_1.json"
    staged.write_text("{}", encoding="utf-8")
    staging_cycle = root / "evaluation_cycles" / ".cycle-tmp0"
    staging_cycle.mkdir()
    (staging_cycle / "glicko_ratings.json").write_text("{}", encoding="utf-8")
    (root / "match_replay" / ".stray").write_text("{}", encoding="utf-8")

    assert daemon_management.monitor_strict_evaluation_bundle(root) is first
    assert len(calls) == 1

    # save_cycle admission (staged -> committed top-level replay) still
    # rebuilds, because the committed bytes are what the loader verifies.
    staged.rename(root / "match_replay" / "match_stage_1.json")
    second = daemon_management.monitor_strict_evaluation_bundle(root)
    assert second == {"available": True, "build": 1}
    assert len(calls) == 2


def test_monitor_bundle_failure_is_not_cached(tmp_path, monkeypatch):
    import daemon_management
    import evaluation_bundle

    root = _make_results_dir(tmp_path)
    state = {"mode": "raise", "calls": 0}

    def _loader(load_root, *args, **kwargs):
        state["calls"] += 1
        mode = state["mode"]
        if mode == "raise":
            raise RuntimeError("evidence load failed")
        if mode == "reason":
            # The real loader reports most failures as reason dicts instead
            # of raising (transient pool discovery included); those must not
            # be cached either.
            return {"available": False, "reason": "active_pool_unavailable"}
        return {"available": True, "build": state["calls"]}

    monkeypatch.setattr(
        evaluation_bundle, "load_current_strict_evaluation_bundle", _loader
    )
    monkeypatch.setattr(
        daemon_management, "_monitor_pool_fingerprint", lambda: ("stub",)
    )

    failed = daemon_management.monitor_strict_evaluation_bundle(root)
    assert failed == {"available": False}
    assert state["calls"] == 1

    # A failed build must not be cached: the next tick retries even though
    # every fingerprinted input is unchanged.
    assert daemon_management.monitor_strict_evaluation_bundle(root) == {
        "available": False
    }
    assert state["calls"] == 2

    # A reason-dict failure is still a failure: it is served but never
    # cached, so every tick retries.
    state["mode"] = "reason"
    unavailable = daemon_management.monitor_strict_evaluation_bundle(root)
    assert unavailable == {
        "available": False,
        "reason": "active_pool_unavailable",
    }
    assert state["calls"] == 3
    daemon_management.monitor_strict_evaluation_bundle(root)
    assert state["calls"] == 4

    state["mode"] = "ok"
    recovered = daemon_management.monitor_strict_evaluation_bundle(root)
    assert recovered == {"available": True, "build": 5}
    assert state["calls"] == 5

    # Once a build succeeds it is cached again as usual.
    assert daemon_management.monitor_strict_evaluation_bundle(root) is recovered
    assert state["calls"] == 5


def test_reset_monitor_strict_bundle_cache_forces_rebuild(tmp_path, monkeypatch):
    import daemon_management
    import evaluation_bundle

    root = _make_results_dir(tmp_path)
    calls = []

    def _loader(load_root, *args, **kwargs):
        calls.append(load_root)
        return {"available": True, "build": len(calls)}

    monkeypatch.setattr(
        evaluation_bundle, "load_current_strict_evaluation_bundle", _loader
    )
    monkeypatch.setattr(
        daemon_management, "_monitor_pool_fingerprint", lambda: ("stub",)
    )

    first = daemon_management.monitor_strict_evaluation_bundle(root)
    assert daemon_management.monitor_strict_evaluation_bundle(root) is first
    assert len(calls) == 1

    daemon_management.reset_monitor_strict_bundle_cache()

    rebuilt = daemon_management.monitor_strict_evaluation_bundle(root)
    assert len(calls) == 2
    assert rebuilt is not first
    assert rebuilt == {"available": True, "build": 2}


def _stub_ok_loader(monkeypatch, evaluation_bundle):
    """Hermetic always-succeeding loader for the fingerprint tests."""

    import daemon_management

    def _loader(load_root, *args, **kwargs):
        return {"available": True, "build": 1}

    monkeypatch.setattr(
        evaluation_bundle, "load_current_strict_evaluation_bundle", _loader
    )
    monkeypatch.setattr(
        daemon_management, "_monitor_pool_fingerprint", lambda: ()
    )


def test_monitor_bundle_rebuilds_when_active_pool_changes(tmp_path, monkeypatch):
    import daemon_management
    import evaluation_bundle

    root = _make_results_dir(tmp_path)
    _stub_ok_loader(monkeypatch, evaluation_bundle)
    pools = iter([("bot_v10",), ("bot_v10",), ("bot_v11",)])
    monkeypatch.setattr(
        daemon_management, "_monitor_pool_fingerprint", lambda: next(pools)
    )

    first = daemon_management.monitor_strict_evaluation_bundle(root)
    assert daemon_management.monitor_strict_evaluation_bundle(root) is first

    # A pool change with no evidence change must still drop the cached build:
    # the loader would now fail closed (pool mismatch / empty pool) for the
    # new pool instead of serving the previous pool's bundle.
    second = daemon_management.monitor_strict_evaluation_bundle(root)
    assert second is not first


def test_monitor_bundle_rebuilds_when_semantic_sources_change(
    tmp_path, monkeypatch
):
    import daemon_management
    import evaluation_bundle
    import evaluation_data_identity

    root = _make_results_dir(tmp_path)
    _stub_ok_loader(monkeypatch, evaluation_bundle)
    source = tmp_path / "engine_source.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    monkeypatch.setattr(evaluation_data_identity, "ROOT", tmp_path)
    monkeypatch.setattr(
        evaluation_data_identity, "SEMANTIC_PATHS", ("engine_source.py",)
    )

    first = daemon_management.monitor_strict_evaluation_bundle(root)
    assert daemon_management.monitor_strict_evaluation_bundle(root) is first

    # A git sync rewrites identity-relevant source files (new mtime) without
    # touching the results root; the cached build must not survive it, or the
    # dashboard would keep showing a bundle the loader now rejects with
    # cycle_manifest_evaluation_identity_invalid.
    newer_ns = source.stat().st_mtime_ns + 1_000_000
    os.utime(source, ns=(newer_ns, newer_ns))
    assert daemon_management.monitor_strict_evaluation_bundle(root) is not first


def test_monitor_bundle_rebuilds_when_epoch_archive_claim_changes(
    tmp_path, monkeypatch
):
    import daemon_management
    import evaluation_bundle
    import system_strict_bootstrap

    root = _make_results_dir(tmp_path)
    _stub_ok_loader(monkeypatch, evaluation_bundle)
    # The receipt resolves archive_root against the results root itself for
    # any root not shaped like web/core/results.
    archive_root = root / "epoch_archive"
    archive_root.mkdir()
    claim = (
        archive_root / system_strict_bootstrap.POLICY_EPOCH_RESET_CLAIM_FILENAME
    )
    claim.write_text(
        '{"kind": "national_tcp_policy_epoch_reset_claim"}', encoding="utf-8"
    )
    (root / "policy_epoch_reset_receipt.json").write_text(
        '{"archive_root": "epoch_archive"}', encoding="utf-8"
    )

    first = daemon_management.monitor_strict_evaluation_bundle(root)
    assert daemon_management.monitor_strict_evaluation_bundle(root) is first

    # The load chain cross-binds the reset receipt to the durable archive
    # claim; rewriting the claim without touching the receipt must still
    # invalidate the cached projection.
    claim.write_text('{"kind": "tampered"}', encoding="utf-8")
    assert daemon_management.monitor_strict_evaluation_bundle(root) is not first


def test_monitor_bundle_cache_is_keyed_per_results_root(tmp_path, monkeypatch):
    import daemon_management
    import evaluation_bundle

    root_a = _make_results_dir(tmp_path / "a")
    root_b = _make_results_dir(tmp_path / "b")
    # Pin identical content, size, and mtime on every fingerprinted file so
    # both roots produce byte-identical fingerprints; only the cache key may
    # tell them apart.
    fixed_ns = 1_700_000_000_000_000_000
    for root in (root_a, root_b):
        for path in sorted(root.rglob("*")):
            if path.is_file():
                os.utime(path, ns=(fixed_ns, fixed_ns))
    monkeypatch.setattr(
        daemon_management, "_monitor_pool_fingerprint", lambda: ()
    )

    def _loader(load_root, *args, **kwargs):
        return {"available": True, "root": str(load_root)}

    monkeypatch.setattr(
        evaluation_bundle, "load_current_strict_evaluation_bundle", _loader
    )

    first_a = daemon_management.monitor_strict_evaluation_bundle(root_a)
    assert daemon_management.monitor_strict_evaluation_bundle(root_a) is first_a

    # root_b produces the identical fingerprint but must never be served
    # root_a's cached object: it gets (and then keeps) its own build.
    first_b = daemon_management.monitor_strict_evaluation_bundle(root_b)
    assert first_b is not first_a
    assert first_b["root"] == str(root_b)
    assert daemon_management.monitor_strict_evaluation_bundle(root_b) is first_b

    # Re-entering root_a rebuilds (single-slot cache) instead of serving the
    # entry built for root_b.
    reentered_a = daemon_management.monitor_strict_evaluation_bundle(root_a)
    assert reentered_a is not first_b
    assert reentered_a["root"] == str(root_a)


def test_web_ui_update_daemon_status_uses_passed_bundle_without_reload(
    monkeypatch,
):
    import evaluation_bundle
    from web_ui import EventBroadcaster, WebUI

    def _must_not_load(*args, **kwargs):
        raise AssertionError(
            "a monitor-supplied strict_bundle must not trigger a reload"
        )

    monkeypatch.setattr(
        evaluation_bundle,
        "load_current_strict_evaluation_bundle",
        _must_not_load,
    )
    ui = WebUI(EventBroadcaster())
    emitted = []
    ui._emit = lambda event_type, payload: emitted.append((event_type, payload))

    ui.update_daemon_status(
        {"pairs": {"stale:pair": 99}},
        {"stale_bot": {}},
        strict_bundle={
            "available": True,
            "daemon_stats": {
                "pairs": {"a:b": 3, "c:d": 4},
                "total_periods": 5,
                "total_games": 9,
            },
            "ratings": {"bot_a": {}, "bot_b": {}},
        },
    )

    assert emitted == [
        (
            "daemon_stats",
            {
                "total_matches": 7,
                "total_periods": 5,
                "total_games": 9,
                "n_bots": 2,
            },
        )
    ]

    # An unavailable (or non-dict) bundle still fails closed to zeros without
    # touching the loader.
    for bad_bundle in ({"available": False}, "garbage"):
        emitted.clear()
        ui.update_daemon_status(
            {"pairs": {"stale:pair": 99}},
            {"stale_bot": {}},
            strict_bundle=bad_bundle,
        )
        assert emitted == [
            (
                "daemon_stats",
                {
                    "total_matches": 0,
                    "total_periods": 0,
                    "total_games": 0,
                    "n_bots": 0,
                },
            )
        ]


def test_web_ui_update_daemon_status_none_bundle_keeps_direct_load(monkeypatch):
    """Guards the intentionally unchanged non-monitor path.

    This pins existing behavior (direct load when ``strict_bundle`` is
    omitted, fail-closed zeros when that load raises) rather than new
    throttling behavior; it must keep passing no matter how the monitor
    projection evolves.
    """
    import evaluation_bundle
    from web_ui import EventBroadcaster, WebUI

    calls = []

    def _loader(*args, **kwargs):
        calls.append(args)
        return {
            "available": True,
            "daemon_stats": {"pairs": {"a:b": 2}},
            "ratings": {"bot_a": {}},
        }

    monkeypatch.setattr(
        evaluation_bundle, "load_current_strict_evaluation_bundle", _loader
    )
    ui = WebUI(EventBroadcaster())
    emitted = []
    ui._emit = lambda event_type, payload: emitted.append((event_type, payload))

    # Non-monitor callers omit strict_bundle and keep the direct read path.
    ui.update_daemon_status({}, {})
    assert len(calls) == 1
    assert emitted == [
        (
            "daemon_stats",
            {
                "total_matches": 2,
                "total_periods": 0,
                "total_games": 0,
                "n_bots": 1,
            },
        )
    ]

    # The explicit None form is exactly the omitted form: the same direct,
    # uncached reload (a pre-throttle two-argument signature rejects it).
    ui.update_daemon_status({}, {}, strict_bundle=None)
    assert calls == [(), ()]
    assert len(emitted) == 2
    assert emitted[-1] == emitted[0]

    def _boom(*args, **kwargs):
        raise RuntimeError("evidence load failed")

    monkeypatch.setattr(
        evaluation_bundle, "load_current_strict_evaluation_bundle", _boom
    )
    ui.update_daemon_status({"pairs": {"a:b": 2}}, {})
    assert len(calls) == 2
    assert emitted[-1] == (
        "daemon_stats",
        {
            "total_matches": 0,
            "total_periods": 0,
            "total_games": 0,
            "n_bots": 0,
        },
    )


def test_monitor_thread_waits_configured_interval(monkeypatch):
    import daemon_management
    import epoch_authority
    import evolution_infra

    class FakeStopEvent:
        def __init__(self):
            self.waits = []
            self._stopped = False

        def is_set(self):
            return self._stopped

        def wait(self, seconds):
            self.waits.append(seconds)
            # Model a stop arriving during the first tick so the monitor
            # loop exits instead of ticking forever.
            self._stopped = True
            return True

    class FakeUI:
        def __init__(self):
            self.updates = []
            self.history = []

        def log_history(self, msg, status="info"):
            self.history.append((status, msg))

        def update_daemon_status(self, stats, ratings, strict_bundle=None):
            self.updates.append((stats, ratings, strict_bundle))

    # Hermetic tick: no epoch requirement, no real evidence load, no real
    # daemon stats I/O.
    monkeypatch.setattr(
        epoch_authority,
        "require_policy_epoch_initialized",
        lambda operation: {"initialized": True},
    )
    monkeypatch.setattr(
        daemon_management,
        "monitor_strict_evaluation_bundle",
        lambda results_dir=None: {"available": False},
    )
    monkeypatch.setattr(evolution_infra, "load_daemon_stats", lambda: {"pairs": {}})
    monkeypatch.setattr(evolution_infra, "load_ratings", lambda: {})

    stop_event = FakeStopEvent()
    ui = FakeUI()
    # A sentinel interval, not the constant itself: asserting against the
    # constant would stay green if the loop regressed to a hardcoded 3s wait
    # whenever the operator env also asked for 3.  monkeypatch restores the
    # module attribute afterwards.
    monkeypatch.setattr(daemon_management, "DAEMON_MONITOR_INTERVAL_SEC", 7.5)
    old_proc = daemon_management.daemon_proc
    old_shutdown = daemon_management._daemon_shutting_down
    try:
        daemon_management.daemon_proc = None
        daemon_management._daemon_shutting_down = False
        daemon_management.daemon_monitor_thread(
            ui, stop_event, daemon_workers=1, daemon_pairs=1
        )
    finally:
        daemon_management.daemon_proc = old_proc
        daemon_management._daemon_shutting_down = old_shutdown

    assert ui.history == []
    assert stop_event.waits == [7.5]
    assert len(ui.updates) == 1
    stats, ratings, strict_bundle = ui.updates[0]
    assert stats == {"pairs": {}}
    assert ratings == {}
    assert strict_bundle == {"available": False}
