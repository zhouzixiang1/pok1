"""Tests for rate_limiter.py — 429 quota exhaustion handler."""

import json
import os
import time
import tempfile
import pytest
from pathlib import Path


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "rate_limit_state.json"


@pytest.fixture
def rl(state_file):
    from rate_limiter import RateLimiter
    return RateLimiter(state_file=state_file)


class TestParse429:
    def test_full_chinese_error_with_reset_time(self, rl):
        error = "API Error: Request rejected (429) · [1308][已达到 5 小时的使用上限。您的限额将在 2099-12-31 23:59:59 重置。][abc123]"
        assert rl.parse_429(error) is True
        assert rl.is_blocked()
        assert rl.wait_seconds() > 0

    def test_chinese_error_compact_format(self, rl):
        error = "限额将在2099-06-08 17:56:48重置"
        assert rl.parse_429(error) is True

    def test_request_rejected_429_no_reset_time(self, rl):
        error = "Request rejected (429)"
        assert rl.parse_429(error) is True
        # Should default to ~300s wait
        assert rl.wait_seconds() > 250

    def test_chinese_pattern_no_reset_time(self, rl):
        error = "已达到5小时的使用上限。"
        assert rl.parse_429(error) is True
        assert rl.wait_seconds() > 250

    def test_past_reset_time_ignored(self, rl):
        error = "限额将在2020-01-01 00:00:00重置"
        assert rl.parse_429(error) is False

    def test_non_429_text_rejected(self, rl):
        assert rl.parse_429("normal output text") is False
        assert rl.parse_429("") is False

    def test_long_text_rejected(self, rl):
        error = "Request rejected (429)" + "x" * 3000
        assert rl.parse_429(error) is False

    def test_actual_log_format(self, rl):
        error = "API Error: Request rejected (429) · [1308][已达到 5 小时的使用上限。您的限额将在 2099-06-07 16:20:12 重置。][202606071605416416a95cac2e4566]"
        assert rl.parse_429(error) is True
        assert rl.is_blocked()

    def test_parse_preserves_latest(self, rl):
        # First parse sets a time far in the future
        rl.parse_429("限额将在2099-01-01 00:00:00重置")
        assert rl.wait_seconds() > 10000000
        # Second parse with closer time should override
        rl.parse_429("限额将在2099-06-01 12:00:00重置")
        # The second parse should have updated
        assert rl.reset_time_str().startswith("2099-06-01")


class TestIsBlocked:
    def test_not_blocked_initially(self, rl):
        assert not rl.is_blocked()

    def test_blocked_after_parse(self, rl):
        rl.parse_429("限额将在2099-12-31 23:59:59重置")
        assert rl.is_blocked()

    def test_auto_clear_after_reset(self, rl, state_file):
        # Set reset time to 1 second in the past
        rl._reset_time = time.time() - 1
        rl._save_state()
        assert not rl.is_blocked()

    def test_wait_seconds_zero_when_not_blocked(self, rl):
        assert rl.wait_seconds() == 0.0


class TestResetTimeStr:
    def test_empty_when_not_blocked(self, rl):
        assert rl.reset_time_str() == ""

    def test_formatted_when_blocked(self, rl):
        rl.parse_429("限额将在2099-06-08 14:30:00重置")
        s = rl.reset_time_str()
        assert "2099" in s
        assert "06" in s
        assert "14:30" in s


class TestPersistence:
    def test_save_and_load(self, state_file):
        from rate_limiter import RateLimiter
        rl1 = RateLimiter(state_file=state_file)
        rl1.parse_429("限额将在2099-12-31 23:59:59重置")
        saved_ts = rl1._reset_time

        # Create new instance loading from same file
        rl2 = RateLimiter(state_file=state_file)
        assert rl2.is_blocked()
        assert abs(rl2._reset_time - saved_ts) < 1.0

    def test_expired_state_cleared_on_load(self, state_file):
        # Write an expired state directly
        state_file.write_text(json.dumps({"reset_time": time.time() - 100}))
        from rate_limiter import RateLimiter
        rl = RateLimiter(state_file=state_file)
        assert not rl.is_blocked()

    def test_missing_state_file(self, tmp_path):
        from rate_limiter import RateLimiter
        rl = RateLimiter(state_file=tmp_path / "nonexistent.json")
        assert not rl.is_blocked()

    def test_corrupt_state_file(self, state_file):
        state_file.write_text("not json")
        from rate_limiter import RateLimiter
        rl = RateLimiter(state_file=state_file)
        assert not rl.is_blocked()

    def test_atomic_write(self, state_file):
        from rate_limiter import RateLimiter
        rl = RateLimiter(state_file=state_file)
        rl.parse_429("限额将在2099-12-31 23:59:59重置")
        # File should exist and be valid JSON
        data = json.loads(state_file.read_text())
        assert "reset_time" in data
        assert data["reset_time"] > 0


class TestClear:
    def test_manual_clear(self, rl):
        rl.parse_429("限额将在2099-12-31 23:59:59重置")
        assert rl.is_blocked()
        rl.clear()
        assert not rl.is_blocked()
        assert rl.wait_seconds() == 0.0


class TestIsQuotaExceeded:
    def test_import(self):
        from llm_query import _is_quota_exceeded
        assert callable(_is_quota_exceeded)

    def test_429_pattern(self):
        from llm_query import _is_quota_exceeded
        assert _is_quota_exceeded("Request rejected (429)")

    def test_chinese_pattern(self):
        from llm_query import _is_quota_exceeded
        assert _is_quota_exceeded("已达到5小时的使用上限。")

    def test_actual_error(self):
        from llm_query import _is_quota_exceeded
        error = "API Error: Request rejected (429) · [1308][已达到 5 小时的使用上限。您的限额将在 2026-06-07 16:20:12 重置。][abc]"
        assert _is_quota_exceeded(error)

    def test_normal_text(self):
        from llm_query import _is_quota_exceeded
        assert not _is_quota_exceeded("Worker 1 done")
        assert not _is_quota_exceeded("")

    def test_long_text(self):
        from llm_query import _is_quota_exceeded
        assert not _is_quota_exceeded("Request rejected (429)" + "x" * 3000)


class TestIsRateLimited:
    def test_now_detects_429(self):
        from llm_query import _is_rate_limited
        assert _is_rate_limited("Request rejected (429)")

    def test_still_detects_529(self):
        from llm_query import _is_rate_limited
        assert _is_rate_limited("overloaded")
        assert _is_rate_limited("该模型当前访问量过大")
        assert _is_rate_limited("rate limit reached")

    def test_long_text_false(self):
        from llm_query import _is_rate_limited
        assert not _is_rate_limited("Request rejected (429)" + "x" * 3000)
