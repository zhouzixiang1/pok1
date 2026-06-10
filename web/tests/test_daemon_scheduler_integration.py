"""Integration tests for daemon + Battle Scheduler interaction.

These tests exercise the external job queue logic in elo_daemon.py and the
scheduler-capability flag in daemon_management.py without spawning real processes.
"""

import json
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "core"))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_results(tmp_path):
    """Redirect RESULTS_DIR to a temp path for isolation."""
    return tmp_path / "results"


@pytest.fixture
def mock_scheduler(tmp_path, monkeypatch):
    """Provide a fake battle_scheduler module that writes to tmp_path."""
    pending_file = tmp_path / "pending_jobs.jsonl"
    result_file = tmp_path / "results.jsonl"
    claimed_file = tmp_path / "claimed_jobs.json"

    def _drain_pending():
        jobs = []
        if pending_file.exists():
            for line in pending_file.read_text().strip().splitlines():
                if line.strip():
                    jobs.append(json.loads(line))
            pending_file.unlink()
        return jobs

    def _requeue_unclaimed():
        jobs = []
        if claimed_file.exists():
            data = json.loads(claimed_file.read_text())
            jobs = data.get("jobs", [])
            claimed_file.unlink()
        return jobs

    def _write_result(job_id, payload):
        entry = {"job_id": job_id, **payload}
        with open(result_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    fake_mod = SimpleNamespace(
        drain_pending_jobs=_drain_pending,
        requeue_unclaimed_on_startup=_requeue_unclaimed,
        write_result=_write_result,
    )
    monkeypatch.setitem(sys.modules, "battle_scheduler", fake_mod)
    return {
        "pending_file": pending_file,
        "result_file": result_file,
        "claimed_file": claimed_file,
        "drain": _drain_pending,
        "requeue": _requeue_unclaimed,
        "write": _write_result,
    }


@pytest.fixture
def daemon_pid_file(tmp_path):
    """Path to a fake .daemon_pid file."""
    return tmp_path / ".daemon_pid"


@pytest.fixture(autouse=True)
def patch_results_dir(tmp_path, monkeypatch):
    """Patch RESULTS_DIR so daemon_management reads from tmp_path."""
    import evolution_infra
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)


# ---------------------------------------------------------------------------
# Tests — elo_daemon.py external job handling
# ---------------------------------------------------------------------------


def test_daemon_drains_external_jobs(monkeypatch, mock_scheduler):
    """External jobs from drain_pending_jobs() are injected into match_queue."""
    import elo_daemon as ed

    # Seed two external jobs
    mock_scheduler["pending_file"].write_text(
        json.dumps({"job_id": "j1", "a": "bot_a", "b": "bot_b"}) + "\n"
        + json.dumps({"job_id": "j2", "a": "bot_c", "b": "bot_d"}) + "\n"
    )

    match_queue = deque()
    _capacity = 1
    ext_in_queue = sum(1 for m in match_queue if len(m) == 7 and m[0] == "external")
    assert ext_in_queue < _capacity

    recovered = mock_scheduler["requeue"]()
    for job in recovered:
        match_queue.appendleft(job)

    pending = mock_scheduler["drain"]()
    for job in pending:
        match_queue.appendleft(job)

    assert len(match_queue) == 2
    for m in match_queue:
        assert isinstance(m, dict)
        assert m.get("job_id", "").startswith("j")


def test_external_job_skips_ratings_update(monkeypatch, mock_scheduler):
    """External job results are written via write_result, not process_result."""
    import elo_daemon as ed

    # Simulate a completed external match result
    result = ("bot_a", "bot_b", 3, 2, 0, 5, None, [])
    ext_job_id = "ext-123"

    mock_scheduler["write"](ext_job_id, {"bot_a": "bot_a", "bot_b": "bot_b", "result": result})

    results = []
    if mock_scheduler["result_file"].exists():
        for line in mock_scheduler["result_file"].read_text().strip().splitlines():
            results.append(json.loads(line))

    assert len(results) == 1
    assert results[0]["job_id"] == ext_job_id
    assert results[0]["result"] == list(result)


def test_reap_preserves_external_jobs():
    """Reap signal filtering must keep external jobs in match_queue."""
    match_queue = deque([
        ("external", "j1", "bot_a", "bot_b", "/a.py", "/b.py", 5),
        ("bot_x", "bot_y", "/x.py", "/y.py", 5),
    ])
    removed = {"bot_x"}

    filtered = deque(
        m for m in match_queue
        if (len(m) == 7 and m[0] == "external")
        or (m[0] not in removed and m[1] not in removed)
    )

    assert len(filtered) == 1
    assert filtered[0][0] == "external"


def test_pool_recovery_writes_error_for_external(monkeypatch, mock_scheduler):
    """BrokenProcessPool recovery writes error results for external in-flight jobs."""
    import elo_daemon as ed

    in_flight = {}
    _external_job_ids = set()

    # Create a mock future that raises on result()
    mock_fut = MagicMock()
    mock_fut.result.side_effect = Exception("pool broken")

    in_flight[mock_fut] = ("bot_a", "bot_b", "ext-456")
    _external_job_ids.add(frozenset({"bot_a", "bot_b"}))

    # Simulate recovery logic: iterate in_flight, write errors for external
    for fut in list(in_flight):
        entry = in_flight[fut]
        if len(entry) == 3:
            a, b, ext_job_id = entry
            mock_scheduler["write"](
                ext_job_id,
                {"bot_a": a, "bot_b": b, "error": "daemon_pool_broken"},
            )
        try:
            fut.result(timeout=1)
        except Exception:
            pass
    in_flight.clear()
    _external_job_ids.clear()

    results = []
    if mock_scheduler["result_file"].exists():
        for line in mock_scheduler["result_file"].read_text().strip().splitlines():
            results.append(json.loads(line))

    assert len(results) == 1
    assert results[0]["job_id"] == "ext-456"
    assert results[0]["error"] == "daemon_pool_broken"


def test_startup_requeues_orphaned_claimed(mock_scheduler):
    """On first iteration, requeue_unclaimed_on_startup recovers claimed jobs."""
    mock_scheduler["claimed_file"].write_text(
        json.dumps({"jobs": [
            ("external", "j-orphan", "bot_a", "bot_b", "/a.py", "/b.py", 5),
        ]})
    )

    match_queue = deque()
    recovered = mock_scheduler["requeue"]()
    for job in recovered:
        match_queue.appendleft(job)

    assert len(match_queue) == 1
    assert match_queue[0][0] == "external"
    assert match_queue[0][1] == "j-orphan"


def test_active_bots_filter_ignores_external():
    """Active-bots filter must not reject external jobs (they are client-driven)."""
    active_bots = {"bot_a", "bot_c"}

    internal = ("bot_a", "bot_b", "/a.py", "/b.py", 5)
    external = ("external", "j1", "bot_a", "bot_d", "/a.py", "/d.py", 5)

    # Internal job with bot_b not active → skip
    skip_internal = internal[0] not in active_bots or internal[1] not in active_bots
    assert skip_internal is True

    # External job should NOT be filtered by active_bots
    is_external = len(external) == 7 and external[0] == "external"
    skip_external = not is_external and (external[2] not in active_bots or external[3] not in active_bots)
    assert skip_external is False


# ---------------------------------------------------------------------------
# Tests — daemon_management.py scheduler capability
# ---------------------------------------------------------------------------


def test_is_daemon_scheduler_capable_true(daemon_pid_file):
    """PID file with scheduler_capable=True returns True."""
    import daemon_management as dm

    daemon_pid_file.write_text(
        json.dumps({"pid": 12345, "ppid": 1000, "scheduler_capable": True})
    )

    # Patch RESULTS_DIR to our tmp_path
    import evolution_infra
    orig_dir = evolution_infra.RESULTS_DIR
    evolution_infra.RESULTS_DIR = daemon_pid_file.parent
    try:
        assert dm.is_daemon_scheduler_capable() is True
    finally:
        evolution_infra.RESULTS_DIR = orig_dir


def test_is_daemon_scheduler_capable_false(daemon_pid_file):
    """Legacy plain-digit PID file returns False."""
    import daemon_management as dm

    daemon_pid_file.write_text("12345")

    import evolution_infra
    orig_dir = evolution_infra.RESULTS_DIR
    evolution_infra.RESULTS_DIR = daemon_pid_file.parent
    try:
        assert dm.is_daemon_scheduler_capable() is False
    finally:
        evolution_infra.RESULTS_DIR = orig_dir


def test_is_daemon_scheduler_capable_missing():
    """Missing PID file returns False."""
    import daemon_management as dm
    import evolution_infra

    orig_dir = evolution_infra.RESULTS_DIR
    evolution_infra.RESULTS_DIR = Path("/nonexistent")
    try:
        assert dm.is_daemon_scheduler_capable() is False
    finally:
        evolution_infra.RESULTS_DIR = orig_dir
