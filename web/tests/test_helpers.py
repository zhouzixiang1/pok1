"""Tests for pure helper functions in _helpers.py, cache.py, tool_helpers.py, evolution_infra.py."""

import json
import time
from pathlib import Path

import pytest


# ── _helpers.py ──

class TestBuildRankedRatings:
    def test_empty_data(self):
        from server.routes._helpers import build_ranked_ratings
        assert build_ranked_ratings({}, {}, {}) == []

    def test_basic_ranking(self, sample_ratings, sample_h2h):
        from server.routes._helpers import build_ranked_ratings
        result = build_ranked_ratings(sample_ratings, {}, sample_h2h)
        assert len(result) == 3
        # Ranked by H2H avg WR descending
        assert result[0]["rank"] == 1
        assert "name" in result[0]
        assert "rating" in result[0]
        assert "h2h_avg_wr" in result[0]

    def test_no_h2h_data(self, sample_ratings):
        from server.routes._helpers import build_ranked_ratings
        result = build_ranked_ratings(sample_ratings, {}, {})
        assert len(result) == 3
        assert result[0]["h2h_avg_wr"] is None


class TestBuildMatchMatrix:
    def test_from_h2h(self, sample_h2h):
        from server.routes._helpers import build_match_matrix
        result = build_match_matrix(sample_h2h, {}, {})
        assert "bots" in result
        assert "matrix" in result
        assert result["source"] == "h2h"
        assert len(result["bots"]) == 3

    def test_empty(self):
        from server.routes._helpers import build_match_matrix
        result = build_match_matrix(None, None, None)
        assert result == {"bots": [], "matrix": []}

    def test_legacy_fallback(self):
        from server.routes._helpers import build_match_matrix
        stats = {"pairs": {"claude_v1 vs claude_v2": 10}}
        ratings = {"claude_v1": {"r": 1500}, "claude_v2": {"r": 1500}}
        result = build_match_matrix(None, ratings, stats)
        assert len(result["bots"]) == 2


class TestBuildMatchStats:
    def test_empty(self):
        from server.routes._helpers import build_match_stats
        result = build_match_stats(None)
        assert result["total_games"] == 0

    def test_with_data(self):
        from server.routes._helpers import build_match_stats
        stats = {"pairs": {"a vs b": 50}, "total_games": 50, "total_periods": 5}
        result = build_match_stats(stats)
        assert result["total_games"] == 50
        assert result["total_pairs"] == 1


class TestReadJsonl:
    def test_empty_file(self, tmp_path):
        from server.routes._helpers import read_jsonl
        f = tmp_path / "test.jsonl"
        f.write_text("")
        assert read_jsonl(f) == []

    def test_basic(self, tmp_path):
        from server.routes._helpers import read_jsonl
        f = tmp_path / "test.jsonl"
        f.write_text('{"a": 1}\n{"b": 2}\n')
        result = read_jsonl(f, reverse=False)
        assert len(result) == 2
        assert result[0]["a"] == 1

    def test_limit(self, tmp_path):
        from server.routes._helpers import read_jsonl
        f = tmp_path / "test.jsonl"
        lines = [f'{{"i": {i}}}\n' for i in range(10)]
        f.write_text("".join(lines))
        result = read_jsonl(f, limit=3)
        assert len(result) == 3


class TestDownsample:
    def test_short_list(self):
        from server.routes._helpers import downsample
        data = [{"x": 1}, {"x": 2}]
        assert downsample(data, 10) == data

    def test_long_list(self):
        from server.routes._helpers import downsample
        data = [{"x": i} for i in range(500)]
        result = downsample(data, 100)
        assert len(result) <= 101
        assert result[-1] == data[-1]


# ── cache.py ──

class TestCachedRead:
    def test_cache_hit(self, tmp_path):
        from server.cache import cached_read, _CACHE
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}')
        _CACHE.clear()
        result1 = cached_read("test_key", f)
        result2 = cached_read("test_key", f)
        assert result1 == {"key": "value"}
        assert result2 == result1

    def test_missing_file(self):
        from server.cache import cached_read
        assert cached_read("missing", Path("/nonexistent")) is None


class TestReadLocked:
    def test_basic(self, tmp_path):
        from server.cache import read_locked
        f = tmp_path / "test.json"
        f.write_text('{"a": 1}')
        assert read_locked(f) == {"a": 1}


# ── tool_helpers.py ──

class TestComputeH2HAvgWinrate:
    def test_basic(self):
        from tool_helpers import compute_h2h_avg_winrate
        h2h = {
            "a vs b": {"games": 10, "a_wins": 6, "b_wins": 4, "win_rate": 0.6},
            "a vs c": {"games": 10, "a_wins": 4, "b_wins": 6, "win_rate": 0.4},
        }
        wr = compute_h2h_avg_winrate("a", h2h)
        assert wr is not None
        assert abs(wr - 0.5) < 0.01

    def test_no_data(self):
        from tool_helpers import compute_h2h_avg_winrate
        assert compute_h2h_avg_winrate("a", {}) is None

    def test_bot_not_in_data(self):
        from tool_helpers import compute_h2h_avg_winrate
        h2h = {"b vs c": {"games": 10, "a_wins": 5, "b_wins": 5}}
        assert compute_h2h_avg_winrate("a", h2h) is None


class TestBotMain:
    def test_valid_version(self, active_bot_version):
        if active_bot_version is None:
            return
        from tool_helpers import _bot_main
        path = _bot_main(f"claude_v{active_bot_version}")
        assert path.name == "main.py"
        assert f"claude_v{active_bot_version}" in str(path)

    def test_graveyard_fallback(self, graveyard_bot_version):
        if graveyard_bot_version is None:
            return
        from tool_helpers import _bot_main
        path = _bot_main(f"claude_v{graveyard_bot_version}")
        assert path.name == "main.py"
        assert path.exists()

    def test_non_numeric(self):
        from tool_helpers import _bot_main
        path = _bot_main("unknown_bot")
        assert path == Path(__file__).resolve().parents[2] / "bots" / "unknown_bot" / "main.py"


class TestSelectPrecommitOpponents:
    def test_basic(self, active_bot_version):
        if active_bot_version is None:
            return
        from tool_helpers import _select_precommit_opponents
        opponents = _select_precommit_opponents(active_bot_version + 1, active_bot_version)
        assert isinstance(opponents, list)
        for opp in opponents:
            assert "name" in opp
            assert "reason" in opp


class TestValidateWorkerBoundaries:
    def test_no_changes(self, monkeypatch, active_bot_version):
        if active_bot_version is None:
            return
        from tool_helpers import _validate_worker_boundaries
        from evolution_infra import get_bot_dir
        monkeypatch.setattr("tool_helpers.get_bot_dir", lambda v: get_bot_dir(active_bot_version))
        errors = _validate_worker_boundaries(
            [{"target_files": ["main.py"], "role": "Algorithmic Logic Architect"}],
            source_v=active_bot_version, next_v=active_bot_version,
        )
        assert errors == []


# ── evolution_infra.py ──

class TestFindCurrentV:
    def test_returns_int(self):
        from evolution_infra import find_current_v
        v = find_current_v()
        assert isinstance(v, int)
        assert v > 0


class TestGetBotDir:
    def test_primary(self, active_bot_version):
        if active_bot_version is None:
            return
        from evolution_infra import get_bot_dir
        d = get_bot_dir(active_bot_version)
        assert d.exists()
        assert f"claude_v{active_bot_version}" in str(d)

    def test_graveyard_fallback(self, graveyard_bot_version):
        if graveyard_bot_version is None:
            return
        from evolution_infra import get_bot_dir
        d = get_bot_dir(graveyard_bot_version)
        assert d.exists()
        assert "graveyard" in str(d) or f"claude_v{graveyard_bot_version}" in str(d)

    def test_nonexistent(self):
        from evolution_infra import get_bot_dir
        d = get_bot_dir(99999)
        assert not d.exists()
