"""P4 (2026-10-05): daemon crash observability + nanny stop-loss and re-arm.

F13+F8 evidence: five consecutive rating-daemon crashes left ZERO bytes of
the failing daemon's own output on disk (``_drain_stdout`` dropped every
line at log.debug), so the EvaluationDataIdentityError could only be
attributed by a 20:00 A/B experiment; after the nanny gave up
(restart_count > 5) the daemon was simply absent for 22 minutes
(19:39-20:01) with zero matches produced until someone restarted the
service.

Contracts under test:

* ``_drain_stdout`` keeps a bounded tail of the daemon's stdout/stderr and,
  when the daemon exits non-zero, appends that tail to
  ``results/daemon_crash.log`` (the file ``elo_daemon.main`` already uses
  for its own tracebacks) with a typed event — replacing the silent
  log.debug discard.
* A deterministic rating-identity failure class
  (``EvaluationDataIdentityError`` texts — runtime profile changed,
  evaluator identity changed, namespace mismatch) stops the nanny's retry
  loop on FIRST classification instead of burning five ~20s restart
  cycles: it emits a typed event and parks in the re-arm wait.
* Giving up no longer ends the monitor thread: the nanny re-arms after a
  bounded interval (default 15 minutes) and emits periodic liveness
  alerts while parked, instead of requiring a service restart.
"""

from __future__ import annotations

import io
import threading

import pytest

import daemon_management as dm


@pytest.fixture(autouse=True)
def isolated_results(monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "RESULTS_DIR", tmp_path)
    dm._reset_daemon_output_tail()
    yield
    dm._reset_daemon_output_tail()


class _FakeProc:
    def __init__(self, lines, returncode):
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self._returncode = returncode
        self.pid = 424242

    def poll(self):
        return self._returncode


IDENTITY_CRASH_LINES = [
    "INFO: Starting rating daemon (workers=3, pairs=1)",
    "Traceback (most recent call last):",
    '  File "elo_daemon.py", line 1340, in main',
    "    identity_manifest = ensure_evaluation_data_identity(",
    "web.core.evaluation_data_identity.EvaluationDataIdentityError: "
    "rating daemon runtime profile changed; archive and restart ratings",
]


# ── a) stdout tail persistence ────────────────────────────────────────────


def test_drain_stdout_persists_tail_on_nonzero_exit():
    proc = _FakeProc(IDENTITY_CRASH_LINES, returncode=1)
    dm._drain_stdout(proc)
    crash_log = dm.RESULTS_DIR / "daemon_crash.log"
    assert crash_log.is_file()
    persisted = crash_log.read_text(encoding="utf-8")
    assert "EvaluationDataIdentityError" in persisted
    assert "rating daemon runtime profile changed" in persisted
    assert "workers=3" in persisted  # head of the tail is retained too


def test_drain_stdout_writes_nothing_on_clean_exit():
    proc = _FakeProc(["INFO: all good"], returncode=0)
    dm._drain_stdout(proc)
    crash_log = dm.RESULTS_DIR / "daemon_crash.log"
    assert not crash_log.exists()


def test_drain_stdout_tail_is_bounded():
    proc = _FakeProc([f"line-{i}" for i in range(5000)], returncode=1)
    dm._drain_stdout(proc)
    persisted = (dm.RESULTS_DIR / "daemon_crash.log").read_text(encoding="utf-8")
    body_lines = [
        line
        for line in persisted.splitlines()
        if line.startswith("line-")
    ]
    assert len(body_lines) <= dm._DAEMON_OUTPUT_TAIL_MAX_LINES
    assert "line-4999" in persisted  # the BOUND retains the newest lines
    assert "line-0" not in persisted


def test_drain_stdout_appends_to_existing_crash_log():
    crash_log = dm.RESULTS_DIR / "daemon_crash.log"
    crash_log.parent.mkdir(parents=True, exist_ok=True)
    crash_log.write_text("previous crash entry\n", encoding="utf-8")
    dm._drain_stdout(_FakeProc(["boom"], returncode=1))
    persisted = crash_log.read_text(encoding="utf-8")
    assert persisted.startswith("previous crash entry")
    assert "boom" in persisted


# ── b) deterministic identity-failure stop-loss ───────────────────────────


def test_identity_failure_classifier_matches_canonical_texts():
    for text in (
        "EvaluationDataIdentityError: rating daemon runtime profile changed; "
        "archive and restart ratings",
        "EvaluationDataIdentityError: authoritative rating evaluator identity "
        "changed; archive and restart ratings",
        "EvaluationDataIdentityError: evaluation identity manifest is unreadable",
        "FATAL: daemon namespace mismatch: configured ACTIVE_BOT_PREFIX="
        "'national_v' but on-disk bot directories use a different namespace",
    ):
        assert dm._deterministic_daemon_failure_class(text) == (
            "evaluation_identity"
        ), text


def test_identity_failure_classifier_ignores_transient_errors():
    for text in (
        "ProcessPool broken (recovery 1/3)",
        "OSError: [Errno 28] No space left on device",
        "stored H2H does not exactly match verified raw match history",
        "",
    ):
        assert dm._deterministic_daemon_failure_class(text) is None


def test_monitor_gives_up_immediately_on_identity_failure(monkeypatch, tmp_path):
    """First classification stops the retry burn (F8: ~1.7min of idle loops)."""

    import epoch_authority

    started = []
    events = []
    rearm_waited = []

    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized",
                        lambda *a, **k: None)
    monkeypatch.setattr(dm, "DAEMON_MONITOR_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(dm, "_DAEMON_REARM_INTERVAL_SEC", 3600.0)
    monkeypatch.setattr(dm, "log_system_event", lambda *a, **k: events.append(a))
    monkeypatch.setattr(dm, "start_daemon",
                        lambda workers=None, pairs=5: started.append(1) or None)
    monkeypatch.setattr(
        dm, "_wait_for_daemon_rearm", lambda stop_event: rearm_waited.append(1)
    )
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "load_daemon_stats", lambda: {})
    monkeypatch.setattr(evolution_infra, "load_ratings", lambda: {})
    monkeypatch.setattr(dm, "monitor_strict_evaluation_bundle",
                        lambda: {"available": False})
    monkeypatch.setattr(dm, "_daemon_shutting_down", False)

    import stability_observation

    monkeypatch.setattr(
        stability_observation, "reset_stability_observation", lambda *a, **k: None
    )

    class _UI:
        def log_history(self, *a, **k):
            pass

        def update_daemon_status(self, *a, **k):
            pass

    dm._reset_daemon_output_tail()
    dm._record_daemon_output_line(
        "EvaluationDataIdentityError: rating daemon runtime profile changed"
    )
    proc = _FakeProc([], returncode=1)
    proc.pid = 777
    with dm._daemon_lock:
        dm.daemon_proc = proc
    stop_event = threading.Event()
    thread = threading.Thread(
        target=dm.daemon_monitor_thread,
        args=(_UI(), stop_event),
        kwargs={"daemon_workers": 3, "daemon_pairs": 1},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    # The identity crash must NOT consume the generic 5-retry budget…
    assert started == []
    gave_up = [e for e in events if e[0] == "daemon.deterministic_failure_gave_up"]
    assert gave_up, events
    assert gave_up[0][3]["failure_class"] == "evaluation_identity"
    # …and the monitor must have parked in the re-arm wait instead of exiting.
    assert rearm_waited == [1]
    with dm._daemon_lock:
        dm.daemon_proc = None


# ── c) re-arm after giving up ─────────────────────────────────────────────


def test_rearm_wait_resumes_and_alerts(monkeypatch):
    """The parked nanny wakes, alerts, and re-arms without a service restart."""

    dm._reset_daemon_output_tail()
    woke = {"n": 0}

    class _Event:
        def __init__(self):
            self._set = False

        def is_set(self):
            return False

        def wait(self, timeout=None):
            woke["n"] += 1
            return False  # interval elapsed, service still running

    events = []
    monkeypatch.setattr(dm, "log_system_event",
                        lambda *a, **k: events.append(a))
    monkeypatch.setattr(dm, "_DAEMON_REARM_INTERVAL_SEC", 0.01)
    result = dm._wait_for_daemon_rearm(_Event())
    assert result is True  # re-arm after one interval
    assert woke["n"] == 1
    alerted = [e for e in events if e[0] == "daemon.auto_restart_rearm_wait"]
    assert alerted


def test_rearm_wait_exits_on_service_stop():
    class _StoppedEvent:
        def is_set(self):
            return True

        def wait(self, timeout=None):
            return True

    dm._reset_daemon_output_tail()
    assert dm._wait_for_daemon_rearm(_StoppedEvent()) is False
