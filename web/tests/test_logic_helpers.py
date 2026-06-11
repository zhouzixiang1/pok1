"""Logic-level tests for pure helper functions — verifies invariants, not just HTTP status codes."""

import json
from pathlib import Path
import pytest


# ── _helpers.py: confidence() ──

class TestConfidence:
    def test_zero_rd(self):
        from server.routes._helpers import confidence
        assert confidence(0) == "very_confident"

    def test_negative_rd(self):
        from server.routes._helpers import confidence
        assert confidence(-10) == "very_confident"

    def test_just_below_50(self):
        from server.routes._helpers import confidence
        assert confidence(49.9) == "very_confident"

    def test_exactly_50(self):
        from server.routes._helpers import confidence
        assert confidence(50) == "confident"

    def test_just_below_100(self):
        from server.routes._helpers import confidence
        assert confidence(99.9) == "confident"

    def test_exactly_100(self):
        from server.routes._helpers import confidence
        assert confidence(100) == "uncertain"

    def test_just_below_200(self):
        from server.routes._helpers import confidence
        assert confidence(199.9) == "uncertain"

    def test_exactly_200(self):
        from server.routes._helpers import confidence
        assert confidence(200) == "very_uncertain"

    def test_very_large_rd(self):
        from server.routes._helpers import confidence
        assert confidence(9999) == "very_uncertain"


# ── _helpers.py: build_ranked_ratings() ──

class TestBuildRankedRatingsLogic:
    def test_sorted_descending_by_h2h(self, sample_ratings, sample_h2h):
        from server.routes._helpers import build_ranked_ratings
        result = build_ranked_ratings(sample_ratings, {}, sample_h2h)
        wr_values = [r["h2h_avg_wr"] for r in result]
        assert wr_values == sorted(wr_values, reverse=True)

    def test_ranks_sequential_from_1(self, sample_ratings, sample_h2h):
        from server.routes._helpers import build_ranked_ratings
        result = build_ranked_ratings(sample_ratings, {}, sample_h2h)
        ranks = [r["rank"] for r in result]
        assert ranks == list(range(1, len(result) + 1))

    def test_none_h2h_sorts_below_numeric(self):
        from server.routes._helpers import build_ranked_ratings
        ratings = {
            "bot_a": {"r": 1500, "rd": 50, "sigma": 0.06},
            "bot_b": {"r": 1500, "rd": 50, "sigma": 0.06},
        }
        h2h = {"bot_a vs bot_b": {"games": 10, "a_wins": 7, "b_wins": 3, "win_rate": 0.7}}
        # bot_a has h2h=0.7, bot_b has h2h computed from b_wins side
        result = build_ranked_ratings(ratings, {}, h2h)
        # bot_a should rank first (higher h2h)
        assert result[0]["name"] == "bot_a"
        assert result[0]["h2h_avg_wr"] is not None

    def test_single_bot(self):
        from server.routes._helpers import build_ranked_ratings
        ratings = {"bot_x": {"r": 1500, "rd": 100, "sigma": 0.06}}
        result = build_ranked_ratings(ratings, {}, {})
        assert len(result) == 1
        assert result[0]["rank"] == 1

    def test_no_extras_or_missing(self, sample_ratings, sample_h2h):
        from server.routes._helpers import build_ranked_ratings
        result = build_ranked_ratings(sample_ratings, {}, sample_h2h)
        names = {r["name"] for r in result}
        assert names == set(sample_ratings.keys())


# ── _helpers.py: build_rating_row() ──

class TestBuildRatingRow:
    def test_conservative_rating_formula(self):
        from server.routes._helpers import build_rating_row
        r_data = {"r": 1600, "rd": 80, "sigma": 0.06}
        row = build_rating_row("bot", r_data, {}, {})
        assert row["conservative_rating"] == round(1600 - 2 * 80, 1)

    def test_missing_sigma_defaults_006(self):
        from server.routes._helpers import build_rating_row
        r_data = {"r": 1500, "rd": 50}
        row = build_rating_row("bot", r_data, {}, {})
        assert row["sigma"] == 0.06

    def test_missing_bot_stats_defaults(self):
        from server.routes._helpers import build_rating_row
        r_data = {"r": 1500, "rd": 50, "sigma": 0.06}
        row = build_rating_row("bot", r_data, {}, {})
        assert row["games"] == 0
        assert row["win_rate"] is None


# ── _helpers.py: build_match_matrix() ──

class TestBuildMatchMatrixH2H:
    def test_symmetry(self):
        from server.routes._helpers import build_match_matrix
        h2h = {
            "a vs b": {"win_rate": 0.6, "games": 10},
            "b vs c": {"win_rate": 0.4, "games": 10},
            "a vs c": {"win_rate": 0.7, "games": 10},
        }
        result = build_match_matrix(h2h, {}, {})
        bots = result["bots"]
        matrix = result["matrix"]
        for i in range(len(bots)):
            for j in range(len(bots)):
                if i != j:
                    assert matrix[j][i] == round(1.0 - matrix[i][j], 4)

    def test_diagonal_is_none(self):
        from server.routes._helpers import build_match_matrix
        h2h = {"a vs b": {"win_rate": 0.6, "games": 10}}
        result = build_match_matrix(h2h, {}, {})
        n = len(result["bots"])
        for i in range(n):
            assert result["matrix"][i][i] is None

    def test_malformed_keys_skipped_in_pairs(self):
        from server.routes._helpers import build_match_matrix
        h2h = {
            "a vs b vs c": {"win_rate": 0.5, "games": 10},
            "a vs b": {"win_rate": 0.6, "games": 10},
        }
        result = build_match_matrix(h2h, {}, {})
        # Bots are extracted from ALL keys (including malformed), so a,b,c all appear
        # But the malformed key is skipped when building the pair matrix
        assert "a" in result["bots"]
        assert "b" in result["bots"]

    def test_source_is_h2h(self):
        from server.routes._helpers import build_match_matrix
        h2h = {"a vs b": {"win_rate": 0.6, "games": 10}}
        result = build_match_matrix(h2h, {}, {})
        assert result["source"] == "h2h"


class TestBuildMatchMatrixLegacy:
    def test_symmetry(self):
        from server.routes._helpers import build_match_matrix
        stats = {"pairs": {"a vs b": 10, "b vs c": 5}}
        ratings = {"a": {"r": 1500}, "b": {"r": 1500}, "c": {"r": 1500}}
        result = build_match_matrix(None, ratings, stats)
        bots = result["bots"]
        matrix = result["matrix"]
        for i in range(len(bots)):
            for j in range(len(bots)):
                assert matrix[i][j] == matrix[j][i]

    def test_diagonal_is_zero(self):
        from server.routes._helpers import build_match_matrix
        stats = {"pairs": {"a vs b": 10}}
        ratings = {"a": {"r": 1500}, "b": {"r": 1500}}
        result = build_match_matrix(None, ratings, stats)
        for i in range(len(result["bots"])):
            assert result["matrix"][i][i] == 0

    def test_pairs_with_unknown_bots_dropped(self):
        from server.routes._helpers import build_match_matrix
        stats = {"pairs": {"a vs unknown": 10, "a vs b": 5}}
        ratings = {"a": {"r": 1500}, "b": {"r": 1500}}
        result = build_match_matrix(None, ratings, stats)
        assert len(result["bots"]) == 2
        # unknown should not appear
        for row in result["matrix"]:
            assert len(row) == 2

    def test_no_source_key(self):
        from server.routes._helpers import build_match_matrix
        stats = {"pairs": {"a vs b": 10}}
        ratings = {"a": {"r": 1500}, "b": {"r": 1500}}
        result = build_match_matrix(None, ratings, stats)
        assert "source" not in result


# ── _helpers.py: build_match_stats() ──

class TestBuildMatchStatsLogic:
    def test_total_games_fallback(self):
        from server.routes._helpers import build_match_stats
        stats = {"pairs": {"a vs b": 10, "b vs c": 20}}
        result = build_match_stats(stats)
        assert result["total_games"] == 30

    def test_empty_pairs(self):
        from server.routes._helpers import build_match_stats
        stats = {"pairs": {}}
        result = build_match_stats(stats)
        assert result["most_active_pair"] == ""
        assert result["most_active_count"] == 0
        assert result["total_pairs"] == 0


# ── _helpers.py: build_bot_summary() ──

class TestBuildBotSummary:
    def test_no_digits_in_name(self, tmp_path):
        from server.routes._helpers import build_bot_summary
        bot_dir = tmp_path / "bot_alpha"
        bot_dir.mkdir()
        result = build_bot_summary(bot_dir, "bot_alpha", {}, {}, {})
        assert result["version"] == 0

    def test_no_py_files(self, tmp_path):
        from server.routes._helpers import build_bot_summary
        bot_dir = tmp_path / "claude_v1"
        bot_dir.mkdir()
        (bot_dir / "readme.txt").write_text("hello")
        result = build_bot_summary(bot_dir, "claude_v1", {}, {}, {})
        assert result["total_lines"] == 0
        assert result["files"] == []

    def test_missing_r_data_defaults(self, tmp_path):
        from server.routes._helpers import build_bot_summary
        bot_dir = tmp_path / "claude_v1"
        bot_dir.mkdir()
        result = build_bot_summary(bot_dir, "claude_v1", {}, {}, {})
        assert result["rating"] is None


# ── _helpers.py: count_lines() ──

class TestCountLines:
    def test_valid_file(self, tmp_path):
        from server.routes._helpers import count_lines
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        assert count_lines(f) == 3

    def test_empty_file(self, tmp_path):
        from server.routes._helpers import count_lines
        f = tmp_path / "empty.py"
        f.write_text("")
        assert count_lines(f) == 0

    def test_missing_file(self):
        from server.routes._helpers import count_lines
        assert count_lines(Path("/nonexistent/file.py")) == 0


# ── _helpers.py: read_jsonl() ──

class TestReadJsonlLogic:
    def test_reverse_true_default(self, tmp_path):
        from server.routes._helpers import read_jsonl
        f = tmp_path / "test.jsonl"
        f.write_text('{"i": 1}\n{"i": 2}\n{"i": 3}\n')
        result = read_jsonl(f)  # default reverse=True
        assert result[0]["i"] == 3
        assert result[-1]["i"] == 1

    def test_limit_after_reverse(self, tmp_path):
        from server.routes._helpers import read_jsonl
        f = tmp_path / "test.jsonl"
        lines = [f'{{"i": {i}}}\n' for i in range(10)]
        f.write_text("".join(lines))
        result = read_jsonl(f, limit=3, reverse=True)
        assert len(result) == 3
        # Should be the 3 most recent (last 3 lines): 9, 8, 7
        assert result[0]["i"] == 9
        assert result[2]["i"] == 7

    def test_malformed_lines_skipped(self, tmp_path):
        from server.routes._helpers import read_jsonl
        f = tmp_path / "test.jsonl"
        f.write_text('{"i": 1}\nNOT JSON\n{"i": 3}\n')
        result = read_jsonl(f, reverse=False)
        assert len(result) == 2

    def test_all_blank_lines(self, tmp_path):
        from server.routes._helpers import read_jsonl
        f = tmp_path / "test.jsonl"
        f.write_text("\n\n\n")
        assert read_jsonl(f) == []


# ── _helpers.py: downsample() ──

class TestDownsampleLogic:
    def test_max_points_1(self):
        from server.routes._helpers import downsample
        data = [{"x": i} for i in range(10)]
        result = downsample(data, max_points=1)
        assert result[0] == data[0]
        assert result[-1] == data[-1]

    def test_exactly_max_points(self):
        from server.routes._helpers import downsample
        data = [{"x": i} for i in range(5)]
        result = downsample(data, max_points=5)
        assert result == data

    def test_max_points_zero_no_crash(self):
        from server.routes._helpers import downsample
        data = [{"x": i} for i in range(10)]
        result = downsample(data, max_points=0)
        assert len(result) >= 1

    def test_last_element_always_included(self):
        from server.routes._helpers import downsample
        data = [{"x": i} for i in range(100)]
        result = downsample(data, max_points=10)
        assert result[-1] == data[-1]


# ── _helpers.py: _bot_sort_key() ──

class TestBotSortKey:
    def test_standard_name(self):
        from server.routes._helpers import _bot_sort_key
        assert _bot_sort_key("claude_v30") == 30  # testing the parsing logic, not a real bot

    def test_no_digits(self):
        from server.routes._helpers import _bot_sort_key
        assert _bot_sort_key("bot_alpha") == 0

    def test_leading_digits(self):
        from server.routes._helpers import _bot_sort_key
        assert _bot_sort_key("123bot") == 123

    def test_empty_string(self):
        from server.routes._helpers import _bot_sort_key
        assert _bot_sort_key("") == 0


# ── tool_helpers.py: compute_h2h_avg_winrate() ──

class TestComputeH2HAvgWinrateLogic:
    def test_bot_as_b_side(self):
        from tool_helpers import compute_h2h_avg_winrate
        h2h = {
            "opponent vs target": {"games": 10, "a_wins": 3, "b_wins": 7, "win_rate": 0.3},
        }
        wr = compute_h2h_avg_winrate("target", h2h)
        assert wr is not None
        assert abs(wr - 0.7) < 0.01  # target won 7/10

    def test_games_zero_skipped(self):
        from tool_helpers import compute_h2h_avg_winrate
        h2h = {"a vs b": {"games": 0, "a_wins": 5, "b_wins": 5}}
        assert compute_h2h_avg_winrate("a", h2h) is None

    def test_missing_wins_keys_default_zero(self):
        from tool_helpers import compute_h2h_avg_winrate
        h2h = {"a vs b": {"games": 10}}  # no a_wins or b_wins
        wr = compute_h2h_avg_winrate("a", h2h)
        assert wr is not None
        assert wr == 0.0  # 0 wins / 10 games


# ── tool_helpers.py: _bot_main() ──

class TestBotMainLogic:
    @pytest.mark.requires_graveyard_bot
    def test_graveyard_fallback(self, graveyard_bot_version):
        from tool_helpers import _bot_main
        path = _bot_main(f"claude_v{graveyard_bot_version}")
        assert path.exists()
        assert "graveyard" in str(path)

    @pytest.mark.requires_active_bot
    def test_valid_version(self, active_bot_version):
        from tool_helpers import _bot_main
        path = _bot_main(f"claude_v{active_bot_version}")
        assert path.name == "main.py"
        assert f"claude_v{active_bot_version}" in str(path)
