"""Regression tests for bug fixes — verifies the fixes stay in place."""

import json
import fcntl
from pathlib import Path

import pytest


# ── Bug Fix A1: cache.py — lock released even on JSON parse error ──

class TestReadLockedLockRelease:
    def test_lock_released_on_malformed_json(self, tmp_path):
        """read_locked returns None on malformed JSON, lock still released."""
        from server.cache import read_locked
        f = tmp_path / "bad.json"
        f.write_text("NOT VALID JSON {{{")
        result = read_locked(f)
        assert result is None
        # Lock was released — verify by reading again with a valid file.
        f.write_text('{"ok": true}')
        result = read_locked(f)
        assert result == {"ok": True}

    def test_lock_released_on_valid_json(self, tmp_path):
        """Normal case still works."""
        from server.cache import read_locked
        f = tmp_path / "good.json"
        f.write_text('{"a": 1}')
        result = read_locked(f)
        assert result == {"a": 1}


# ── Bug Fix A2: _helpers.py — count_lines file handle leak ──

class TestCountLinesNoLeak:
    def test_returns_line_count(self, tmp_path):
        from server.routes._helpers import count_lines
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        assert count_lines(f) == 3

    def test_returns_zero_for_missing(self):
        from server.routes._helpers import count_lines
        assert count_lines(Path("/nonexistent/file.py")) == 0

    def test_returns_zero_for_empty(self, tmp_path):
        from server.routes._helpers import count_lines
        f = tmp_path / "empty.py"
        f.write_text("")
        assert count_lines(f) == 0


# ── Bug Fix A3: logs.py — negative tail parameter ──

class TestTailNegativeRejected:
    def test_generation_log_negative_tail(self, client):
        resp = client.get("/api/logs/generations/v30/master_io.txt?tail=-1")
        assert resp.status_code == 422

    def test_orchestrator_log_negative_tail(self, client):
        resp = client.get("/api/logs/orchestrator/orchestrator_20260531_153855.txt?tail=-1")
        assert resp.status_code == 422

    def test_zero_tail_accepted(self, client):
        resp = client.get("/api/logs/generations/v30/master_io.txt?tail=0")
        assert resp.status_code == 200


# ── Bug Fix A4: _helpers.py — downsample ZeroDivisionError ──

class TestDownsampleZeroMaxPoints:
    def test_max_points_zero_no_crash(self):
        from server.routes._helpers import downsample
        data = [{"x": i} for i in range(10)]
        result = downsample(data, max_points=0)
        # Should not crash, returns at least one element
        assert len(result) >= 1

    def test_max_points_negative_no_crash(self):
        from server.routes._helpers import downsample
        data = [{"x": i} for i in range(10)]
        result = downsample(data, max_points=-5)
        assert len(result) >= 1

    def test_max_points_one(self):
        from server.routes._helpers import downsample
        data = [{"x": i} for i in range(10)]
        result = downsample(data, max_points=1)
        assert result[0] == data[0]
        assert result[-1] == data[-1]


# ── Bug Fix A5: evolution_infra.py — _is_shutdown accepts multiple types ──

class TestIsShutdown:
    def test_none(self):
        from evolution_infra import _is_shutdown
        assert _is_shutdown(None) is False

    def test_asyncio_event_unset(self):
        import asyncio
        from evolution_infra import _is_shutdown
        assert _is_shutdown(asyncio.Event()) is False

    def test_asyncio_event_set(self):
        import asyncio
        from evolution_infra import _is_shutdown
        e = asyncio.Event()
        e.set()
        assert _is_shutdown(e) is True

    def test_shutdown_manager_not_shutting_down(self):
        from evolution_infra import _is_shutdown
        from shutdown_manager import ShutdownManager
        mgr = ShutdownManager()
        assert _is_shutdown(mgr) is False

    def test_shutdown_manager_shutting_down(self):
        from evolution_infra import _is_shutdown
        from shutdown_manager import ShutdownManager
        mgr = ShutdownManager()
        mgr.request_shutdown()
        assert _is_shutdown(mgr) is True

    def test_unknown_object(self):
        from evolution_infra import _is_shutdown
        assert _is_shutdown(object()) is False


# ── Bug Fix A6: tool_status.py — git_has_tag called with int not string ──

class TestDiagnoseGitHasTagArg:
    def test_git_has_tag_accepts_int(self):
        from evolution_infra import git_has_tag
        # Should not raise TypeError — accepts int
        result = git_has_tag(99999)
        assert isinstance(result, bool)


# ── Bug Fix A7: cache.py — cached_read handles None from read_locked ──

class TestCachedReadMalformedJson:
    def test_cached_read_returns_none_on_malformed(self, tmp_path):
        from server.cache import cached_read
        f = tmp_path / "bad.json"
        f.write_text("NOT JSON!!!")
        result = cached_read("test_bad", f)
        assert result is None

    def test_cached_read_valid_after_malformed(self, tmp_path):
        from server.cache import cached_read
        f = tmp_path / "recover.json"
        f.write_text("NOT JSON!!!")
        result = cached_read("test_recover", f)
        assert result is None
        f.write_text('{"ok": true}')
        result = cached_read("test_recover", f)
        assert result == {"ok": True}
