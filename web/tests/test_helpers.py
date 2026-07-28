"""Tests for pure helper functions in _helpers.py, cache.py, tool_helpers.py, evolution_infra.py."""

import json
import time
from pathlib import Path

from bot_namespace import (
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
)
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V, strict_bot_name


def _strict_artifact(root: Path, version: int) -> Path:
    from bot_namespace import refresh_policy_identity_documents

    root.mkdir(parents=True)
    (root / "national_bot.py").write_text(
        "from policy import decide\n",
        encoding="utf-8",
    )
    (root / "policy.py").write_text(
        "def decide(_context):\n    return {'kind': 'pass'}\n",
        encoding="utf-8",
    )
    (root / "precompute.py").write_text("TABLE = ()\n", encoding="utf-8")
    (root / "national_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "policy_epoch_receipt.json").write_text("{}\n", encoding="utf-8")
    refresh_policy_identity_documents(
        root,
        version,
        parent_versions=() if version == FIRST_STRICT_POLICY_VERSION else (version - 1,),
    )
    return root


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
        assert result == {
            "bots": [],
            "matrix": [],
            "source": "h2h",
            "evidence_available": False,
        }

    def test_pair_counts_are_not_used_when_h2h_is_missing(self):
        from server.routes._helpers import build_match_matrix
        stats = {"pairs": {f"{strict_bot_name()} vs {strict_bot_name(STRICT_TARGET_V + 1)}": 10}}
        ratings = {strict_bot_name(): {"r": 1500}, strict_bot_name(STRICT_TARGET_V + 1): {"r": 1500}}
        result = build_match_matrix(None, ratings, stats)
        assert result["bots"] == []
        assert result["matrix"] == []
        assert result["evidence_available"] is False


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


class TestCachedByMtime:
    def test_reuses_within_same_mtime(self, tmp_path):
        from server.cache import cached_by_mtime, clear_mtime_cache
        src = tmp_path / "src.json"
        src.write_text("{}")
        clear_mtime_cache()
        calls = {"n": 0}

        def producer():
            calls["n"] += 1
            return {"v": calls["n"]}

        a = cached_by_mtime("k", src, producer)
        b = cached_by_mtime("k", src, producer)
        assert a == {"v": 1}
        assert b == {"v": 1}  # cached, producer not re-run
        assert calls["n"] == 1

    def test_invalidates_when_mtime_changes(self, tmp_path):
        import os

        from server.cache import cached_by_mtime, clear_mtime_cache
        src = tmp_path / "src.json"
        src.write_text("{}")
        clear_mtime_cache()
        calls = {"n": 0}

        def producer():
            calls["n"] += 1
            return {"v": calls["n"]}

        cached_by_mtime("k", src, producer)
        # Force a distinct mtime (write + bump mtime past filesystem granularity).
        src.write_text('{"x": 1}')
        future = src.stat().st_mtime_ns + 5_000_000_000
        os.utime(src, ns=(future, future))
        b = cached_by_mtime("k", src, producer)
        assert b == {"v": 2}  # re-computed because mtime changed
        assert calls["n"] == 2

    def test_missing_source_is_cache_miss(self, tmp_path):
        from server.cache import cached_by_mtime, clear_mtime_cache, _MTIME_CACHE
        clear_mtime_cache()
        missing = tmp_path / "absent.json"
        calls = {"n": 0}

        def producer():
            calls["n"] += 1
            return {"empty": True}

        result = cached_by_mtime("k", missing, producer)
        assert result == {"empty": True}
        # Must not cache the missing-source result, so the next poll re-checks.
        assert "k" not in _MTIME_CACHE


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


class TestUpdateH2H:
    def test_batch_update_preserves_game_totals_and_draw_half_winrate(self):
        from evolution_infra import update_h2h

        h2h = {}
        update_h2h(h2h, "bot_a", "bot_b", wins_a=3, wins_b=1, draws=2)

        entry = h2h["bot_a vs bot_b"]
        assert entry["games"] == 6
        assert entry["a_wins"] == 3
        assert entry["b_wins"] == 1
        assert entry["draws"] == 2
        assert entry["win_rate"] == round((3 + 0.5 * 2) / 6, 4)


class TestBotEntry:
    def test_strict_version_uses_policy_runtime_entrypoint(self):
        from tool_helpers import _bot_entry
        path = _bot_entry(strict_bot_name())
        assert path == (
            Path(__file__).resolve().parents[2]
            / "bots"
            / strict_bot_name()
            / "national_bot.py"
        )

    def test_archived_version_is_not_an_entrypoint_fallback(self):
        from tool_helpers import _bot_entry
        # A sub-floor (archived) version label never redirects into an archive
        # directory; it resolves verbatim under bots/.  Use the branch's archived
        # high-water so the label is meaningful on every evolution line.
        archived_label = bot_name(STRICT_SOURCE_V)
        path = _bot_entry(archived_label)
        expected = (
            Path(__file__).resolve().parents[2]
            / "bots"
            / archived_label
            / "national_bot.py"
        )
        assert path == expected
        assert "archive" not in path.parts

    def test_non_numeric(self):
        from tool_helpers import _bot_entry
        path = _bot_entry("unknown_bot")
        assert path == (
            Path(__file__).resolve().parents[2]
            / "bots"
            / "unknown_bot"
            / "national_bot.py"
        )


class TestSelectPrecommitOpponents:
    def test_missing_frozen_selection_evidence_fails_closed(self, monkeypatch):
        import evidence_snapshot
        from tool_helpers import _select_precommit_opponents

        monkeypatch.setattr(
            evidence_snapshot,
            "load_generation_evaluation_snapshot",
            lambda _version: {"available": False, "reason": "missing"},
        )

        assert _select_precommit_opponents(STRICT_TARGET_V + 1, STRICT_TARGET_V) == []


class TestValidateWorkerBoundaries:
    def test_no_changes(self, tmp_path, monkeypatch):
        from tool_helpers import _validate_worker_boundaries

        artifact = _strict_artifact(tmp_path / "bots" / strict_bot_name(), STRICT_TARGET_V)
        monkeypatch.setattr("tool_helpers.get_bot_dir", lambda _version: artifact)
        errors = _validate_worker_boundaries(
            [{"target_files": ["policy.py"], "role": "Algorithmic Logic Architect"}],
            source_v=STRICT_TARGET_V, next_v=STRICT_TARGET_V,
        )
        assert errors == []

    def test_numeric_lineage_uses_worker_snapshot_without_resolving_source(
        self,
        tmp_path,
        monkeypatch,
    ):
        import tool_helpers

        candidate = _strict_artifact(tmp_path / "bots" / strict_bot_name(), STRICT_TARGET_V)
        before = (candidate / "policy.py").read_text(encoding="utf-8")
        (candidate / "policy.py").write_text(
            before + "\nBOOTSTRAP_POLICY = True\n",
            encoding="utf-8",
        )

        def retired_source_is_forbidden(_version):
            raise AssertionError(
                f"numeric-only v{STRICT_SOURCE_V} path must not be resolved"
            )

        monkeypatch.setattr(tool_helpers, "get_bot_dir", retired_source_is_forbidden)
        errors = tool_helpers._validate_worker_boundaries(
            [{
                "target_files": ["policy.py"],
                "role": "Algorithmic Logic Architect",
            }],
            source_v=STRICT_SOURCE_V,
            next_v=STRICT_TARGET_V,
            worker_snapshots={(0, "policy.py"): before},
            candidate_dir=candidate,
            source_artifact_inherited=False,
        )

        assert errors == []

    def test_numeric_lineage_missing_snapshot_fails_without_source_fallback(
        self,
        tmp_path,
        monkeypatch,
    ):
        import tool_helpers

        candidate = _strict_artifact(tmp_path / "bots" / strict_bot_name(), STRICT_TARGET_V)

        def retired_source_is_forbidden(_version):
            raise AssertionError(
                f"numeric-only v{STRICT_SOURCE_V} path must not be resolved"
            )

        monkeypatch.setattr(tool_helpers, "get_bot_dir", retired_source_is_forbidden)
        errors = tool_helpers._validate_worker_boundaries(
            [{
                "target_files": ["policy.py"],
                "role": "Algorithmic Logic Architect",
            }],
            source_v=STRICT_SOURCE_V,
            next_v=STRICT_TARGET_V,
            worker_snapshots=None,
            candidate_dir=candidate,
            source_artifact_inherited=False,
        )

        assert [error["type"] for error in errors] == [
            "worker_boundary_baseline_missing"
        ]

# ── evolution_infra.py ──

class TestFindCurrentV:
    def test_returns_int(self):
        from evolution_infra import find_current_v
        v = find_current_v()
        assert isinstance(v, int)
        # The published high-water sits at or above the branch's archived floor
        # (main: 142, cloud: 0).  An isolated namespace with no paired strict
        # tags legitimately returns ARCHIVED_VERSION_HIGH_WATER, so the portable
        # contract is v >= STRICT_SOURCE_V, not v > 0.
        assert v >= STRICT_SOURCE_V


class TestGetBotDir:
    def test_primary(self, tmp_path, monkeypatch):
        import evolution_infra

        bots_dir = tmp_path / "bots"
        expected = _strict_artifact(bots_dir / strict_bot_name(), STRICT_TARGET_V)
        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)

        assert evolution_infra.get_bot_dir(STRICT_TARGET_V) == expected

    def test_archived_version_has_no_transparent_directory_fallback(self):
        from evolution_infra import BOTS_DIR, get_bot_dir
        d = get_bot_dir(STRICT_SOURCE_V)
        assert d == BOTS_DIR / bot_name(STRICT_SOURCE_V)
        assert "archive" not in d.parts

    def test_nonexistent(self):
        from evolution_infra import get_bot_dir
        d = get_bot_dir(99999)
        assert not d.exists()
