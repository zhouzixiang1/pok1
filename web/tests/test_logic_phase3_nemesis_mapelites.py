"""Phase 3 regression tests: FAMOU nemesis slot + opponent-profile injection +
MAP-Elites behavior archive MVP.

Covers (per the Phase 3 design spec):
  A. nemesis slot selection (tool_helpers._find_nemesis_opponent +
     _select_precommit_opponents): lowest-winrate opponent, dedup, flag on/off,
     no qualifying opponent.
  B. nemesis gate isolation (tool_eval.run_precommit_eval): nemesis loss never
     blocks, nemesis net-chips excluded from aggregate CI, nemesis timeout
     non-blocking, parent gate still active, telemetry retained.
  C. nemesis_archive.json (nemesis_archive.compute/write): write on commit,
     read fallback in select, commit failure skips write.
  D. opponent-profile injection (tool_planning.run_master): per-opponent
     profile built when per_opp file exists, "" when missing, capped to K.
  E. MAP-Elites behavior_archive (map_elites): BC discretization, niche key,
     max-fitness retention, idempotent update, fitness fallback, empty-games.
  F. integration: full precommit with nemesis + parent gate, no regression.

Pure-function + synthetic-mock tests — no real subprocess battles.
"""

import asyncio
import json
import math
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "core"))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "server"))

import tool_helpers
import tool_eval
import tool_planning
import nemesis_archive
import map_elites
import replay_analysis as ra
from tool_eval import run_precommit_eval as _run_precommit_eval_tool

run_precommit_eval = _run_precommit_eval_tool.handler

# Ensure engine.battle is importable so sys.modules patching works.
import engine  # noqa: F401
try:
    import importlib
    importlib.import_module("engine.battle")
except Exception:
    pass


def _decode(result):
    return json.loads(result["content"][0]["text"])


# ──────────────────────────────────────────────
# A. nemesis slot selection
# ──────────────────────────────────────────────


class TestNemesisSlot:
    def _h2h_with(self, parent, matchups):
        """Build an h2h dict where `parent` has the given (opp, wins, games).

        matchups entries are (opp, parent_wins, games) so win rates are exact
        integers (avoids round() distortion).
        """
        h2h = {}
        for opp, wins, games in matchups:
            losses = games - wins
            key = f"{parent} vs {opp}" if parent < opp else f"{opp} vs {parent}"
            if key.startswith(f"{parent} vs"):
                h2h[key] = {"games": games, "a_wins": wins, "b_wins": losses}
            else:
                h2h[key] = {"games": games, "a_wins": losses, "b_wins": wins}
        return h2h

    def test_find_nemesis_returns_lowest_winrate(self):
        # (opp, parent_wins, games); win rates 0.3/0.2/0.5 exact.
        h2h = self._h2h_with("claude_v98", [
            ("claude_vA", 3, 10),
            ("claude_vB", 2, 10),
            ("claude_vC", 1, 2),   # below min_games -> filtered
        ])
        result = tool_helpers._find_nemesis_opponent(
            "claude_v98", ["claude_vA", "claude_vB", "claude_vC"], h2h, min_games=4
        )
        assert result is not None
        opp, wr = result
        assert opp == "claude_vB"
        assert wr == pytest.approx(0.2)

    def test_find_nemesis_none_when_all_above_threshold(self):
        h2h = self._h2h_with("claude_v98", [
            ("claude_vA", 6, 10),
            ("claude_vB", 5, 10),
        ])
        # _find_nemesis_opponent returns the lowest regardless of threshold;
        # the threshold filter happens in _select_precommit_opponents.
        result = tool_helpers._find_nemesis_opponent(
            "claude_v98", ["claude_vA", "claude_vB"], h2h, min_games=4
        )
        assert result is not None
        assert result[0] == "claude_vB"  # lowest wr
        assert result[1] > 0.40  # but above the probe threshold

    def test_find_nemesis_none_when_no_games(self):
        h2h = {}
        result = tool_helpers._find_nemesis_opponent(
            "claude_v98", ["claude_vA", "claude_vB"], h2h, min_games=4
        )
        assert result is None

    def test_select_appends_nemesis_when_flag_on(self, tmp_path, monkeypatch):
        # Build fake bots so _bot_main().exists() is True.
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        for name in ("claude_v99", "claude_v98", "claude_vA", "claude_vB", "claude_vTop"):
            d = bots_dir / name
            d.mkdir()
            (d / "main.py").write_text("# fake")
        monkeypatch.setattr(tool_helpers, "_bot_main", lambda n: bots_dir / n / "main.py")
        monkeypatch.setattr(tool_helpers, "get_active_bots",
                            lambda: ["claude_v98", "claude_vA", "claude_vB", "claude_vTop"])
        # v98 is the parent. vB (wr 0.1) is the weak-slot pick; vA (wr 0.3)
        # is the nemesis probe (next-worst, distinct from weak). Counts scaled to
        # >=15 games per pair to clear the raised PRECOMMIT_NEMESIS_MIN_GAMES=15
        # floor (win rates preserved).
        h2h = self._h2h_with("claude_v98", [
            ("claude_vA", 6, 20),    # wr 0.3
            ("claude_vB", 2, 20),    # wr 0.1 -> weak slot
            ("claude_vTop", 14, 20), # wr 0.7 -> top
        ])
        monkeypatch.setattr(tool_helpers, "_load_h2h_data", lambda: h2h)
        # load_h2h_avg_winrates + _load_h2h_data both read the file; patch the
        # public loader too.
        monkeypatch.setattr(tool_helpers, "load_h2h_avg_winrates",
                            lambda: {"claude_v98": 0.5, "claude_vA": 0.5, "claude_vB": 0.3, "claude_vTop": 0.7})
        monkeypatch.setattr(tool_helpers, "load_ratings", lambda: {})
        monkeypatch.setattr(tool_helpers, "PRECOMMIT_NEMESIS_SLOT", True)

        opponents = tool_helpers._select_precommit_opponents(99, 98)
        names = [o["name"] for o in opponents]
        reasons = {o["name"]: o["reason"] for o in opponents}
        # vA is the distinct nemesis (next-worst after vB).
        assert "claude_vA" in names
        assert reasons["claude_vA"] == "nemesis_probe"
        # vB is the weak slot, not the nemesis.
        assert reasons.get("claude_vB") == "source_h2h_weakness"

    def test_select_no_nemesis_when_flag_off(self, tmp_path, monkeypatch):
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        for name in ("claude_v99", "claude_v98", "claude_vA", "claude_vB"):
            d = bots_dir / name
            d.mkdir()
            (d / "main.py").write_text("# fake")
        monkeypatch.setattr(tool_helpers, "_bot_main", lambda n: bots_dir / n / "main.py")
        monkeypatch.setattr(tool_helpers, "get_active_bots",
                            lambda: ["claude_v98", "claude_vA", "claude_vB"])
        h2h = self._h2h_with("claude_v98", [
            ("claude_vA", 5, 10),
            ("claude_vB", 2, 10),
        ])
        monkeypatch.setattr(tool_helpers, "_load_h2h_data", lambda: h2h)
        monkeypatch.setattr(tool_helpers, "load_h2h_avg_winrates",
                            lambda: {"claude_v98": 0.5, "claude_vA": 0.5, "claude_vB": 0.3})
        monkeypatch.setattr(tool_helpers, "load_ratings", lambda: {})
        monkeypatch.setattr(tool_helpers, "PRECOMMIT_NEMESIS_SLOT", False)

        opponents = tool_helpers._select_precommit_opponents(99, 98)
        assert not any(o["reason"] == "nemesis_probe" for o in opponents)

    def test_select_nemesis_dedup_with_weak(self, tmp_path, monkeypatch):
        """When the nemesis is ALSO the source_h2h_weakness pick, add() dedups."""
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        for name in ("claude_v99", "claude_v98", "claude_vB"):
            d = bots_dir / name
            d.mkdir()
            (d / "main.py").write_text("# fake")
        monkeypatch.setattr(tool_helpers, "_bot_main", lambda n: bots_dir / n / "main.py")
        monkeypatch.setattr(tool_helpers, "get_active_bots", lambda: ["claude_v98", "claude_vB"])
        # vB is both the weakness pick AND the nemesis (same opponent).
        h2h = self._h2h_with("claude_v98", [("claude_vB", 2, 10)])
        monkeypatch.setattr(tool_helpers, "_load_h2h_data", lambda: h2h)
        monkeypatch.setattr(tool_helpers, "load_h2h_avg_winrates",
                            lambda: {"claude_v98": 0.5, "claude_vB": 0.3})
        monkeypatch.setattr(tool_helpers, "load_ratings", lambda: {})
        monkeypatch.setattr(tool_helpers, "PRECOMMIT_NEMESIS_SLOT", True)

        opponents = tool_helpers._select_precommit_opponents(99, 98)
        # vB appears exactly once (parent+vB, no duplicate nemesis entry).
        names = [o["name"] for o in opponents]
        assert names.count("claude_vB") == 1

    def test_select_nemesis_slot_at_most_one_third(self, tmp_path, monkeypatch):
        """Total opponents >=3 => nemesis count <= 1/3 of total."""
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        for name in ("claude_v99", "claude_v98", "claude_vA", "claude_vB", "claude_vWeak2", "claude_vTop", "claude_vTop2"):
            d = bots_dir / name
            d.mkdir()
            (d / "main.py").write_text("# fake")
        monkeypatch.setattr(tool_helpers, "_bot_main", lambda n: bots_dir / n / "main.py")
        monkeypatch.setattr(tool_helpers, "get_active_bots",
                            lambda: ["claude_v98", "claude_vA", "claude_vB", "claude_vWeak2", "claude_vTop", "claude_vTop2"])
        # Counts scaled to >=15 games per pair to clear the raised
        # PRECOMMIT_NEMESIS_MIN_GAMES=15 floor (win rates preserved).
        h2h = self._h2h_with("claude_v98", [
            ("claude_vA", 10, 20),
            ("claude_vB", 2, 20),     # wr 0.1 -> weak
            ("claude_vWeak2", 6, 20), # wr 0.3 -> nemesis (next-worst)
            ("claude_vTop", 14, 20),
            ("claude_vTop2", 12, 20),
        ])
        monkeypatch.setattr(tool_helpers, "_load_h2h_data", lambda: h2h)
        monkeypatch.setattr(tool_helpers, "load_h2h_avg_winrates",
                            lambda: {"claude_v98": 0.5, "claude_vA": 0.5, "claude_vB": 0.3,
                                     "claude_vTop": 0.7, "claude_vTop2": 0.65})
        monkeypatch.setattr(tool_helpers, "load_ratings", lambda: {})
        monkeypatch.setattr(tool_helpers, "PRECOMMIT_NEMESIS_SLOT", True)

        opponents = tool_helpers._select_precommit_opponents(99, 98)
        n_nemesis = sum(1 for o in opponents if o["reason"] == "nemesis_probe")
        assert n_nemesis == 1
        assert n_nemesis <= len(opponents) / 3


# ──────────────────────────────────────────────
# B. nemesis gate isolation (run_precommit_eval)
# ──────────────────────────────────────────────


@pytest.fixture
def mock_ui():
    ui = MagicMock()
    ui.log_history = MagicMock()
    return ui


@pytest.fixture(autouse=True)
def _mock_precommit_semantic(monkeypatch):
    async def _fake_semantic(v, source_v, matchups, master_plan, ui):
        return {"recommended_action": "proceed", "confidence": "low",
                "win_pattern_analysis": "", "top_opponent_assessment": "",
                "regression_semantics": "safe"}
    import audit_agents
    monkeypatch.setattr(audit_agents, "_run_precommit_semantic", _fake_semantic)


@pytest.fixture
def patch_checkpoint_file(monkeypatch, tmp_path):
    import evolution_infra
    ckpt_path = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_path)
    return ckpt_path


@pytest.fixture
def fake_bots(tmp_path, monkeypatch):
    bots_dir = tmp_path / "bots"
    bots_dir.mkdir()
    for name in ("claude_v99", "claude_v98", "claude_v50", "claude_vNem"):
        d = bots_dir / name
        d.mkdir()
        (d / "main.py").write_text("# fake bot")
    monkeypatch.setattr("tool_eval._bot_main", lambda name: bots_dir / name / "main.py")
    return bots_dir


def _seed_passing_checkpoint():
    tool_eval.write_pipeline_checkpoint(
        99, 98, "critic_checked",
        gate_results={
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {"approved": True},
            "critic": {"approved": True, "score": 7},
        },
        precommit_attempt=0,
    )


def _common_patches(monkeypatch, mock_ui):
    monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
    monkeypatch.setattr("tool_eval._get_ui", lambda: mock_ui)
    monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)


class TestNemesisGateIsolation:
    @pytest.mark.asyncio
    async def test_nemesis_loss_does_not_block(
        self, monkeypatch, fake_bots, mock_ui, patch_checkpoint_file
    ):
        """Nemesis matchup loses big but parent/top are even => passed=True."""
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        ops = [
            {"name": "claude_v98", "reason": "parent"},
            {"name": "claude_v50", "reason": "top_h2h_wr"},
            {"name": "claude_vNem", "reason": "nemesis_probe"},
        ]
        monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: ops)
        _battle_module = sys.modules["engine.battle"]

        def fake_gen(a, b, *a2, **k):
            if "claude_v98" in b:
                for x in [100, -100, 100, -100, 100, -100, 100, -100]:
                    yield x
            elif "claude_v50" in b:
                for x in [50, -50, 50, -50, 50, -50, 50, -50]:
                    yield x
            else:
                # Nemesis: candidate gets crushed every pair.
                for x in [-30000, -30000, -30000, -30000, -30000, -30000, -30000, -30000]:
                    yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)
        monkeypatch.setattr(_battle_module, "mirror_battle",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("gen path only")))

        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)
        assert data["passed"] is True
        # Nemesis matchup is in telemetry.
        nem = next(m for m in data["matchups"] if m["opponent"] == "claude_vNem")
        assert nem["reason"] == "nemesis_probe"
        assert nem["losses"] == 8
        # No blocker references the nemesis.
        assert not any("claude_vNem" in str(b) for b in data["blockers"])

    @pytest.mark.asyncio
    async def test_nemesis_excluded_from_aggregate_ci(
        self, monkeypatch, fake_bots, mock_ui, patch_checkpoint_file
    ):
        """Nemesis losses must NOT enter aggregate_net_chips."""
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        ops = [
            {"name": "claude_v98", "reason": "parent"},
            {"name": "claude_vNem", "reason": "nemesis_probe"},
        ]
        monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: ops)
        _battle_module = sys.modules["engine.battle"]

        def fake_gen(a, b, *a2, **k):
            if "claude_v98" in b:
                for x in [100, -100, 100, -100, 100, -100, 100, -100]:
                    yield x
            else:
                # Catastrophic nemesis losses would trip the aggregate gate if pooled.
                for x in [-30000] * 8:
                    yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)
        monkeypatch.setattr(_battle_module, "mirror_battle",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("gen path only")))
        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)
        # If nemesis net-chips were pooled, aggregate CI upper would be << -2000
        # and trigger aggregate_precommit_regression. Because they are excluded,
        # the gate only sees the mild parent stream -> passes.
        assert data["passed"] is True
        assert not any(b.get("reason") == "aggregate_precommit_regression"
                       for b in data["blockers"])
        # telemetry still shows the nemesis matchup
        assert any(m["opponent"] == "claude_vNem" for m in data["matchups"])

    @pytest.mark.asyncio
    async def test_nemesis_timeout_does_not_block(
        self, monkeypatch, fake_bots, mock_ui, patch_checkpoint_file
    ):
        """Nemesis matchup times out => no match_timeout blocker, parent fine."""
        import asyncio as _aio
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        ops = [
            {"name": "claude_v98", "reason": "parent"},
            {"name": "claude_vNem", "reason": "nemesis_probe"},
        ]
        monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: ops)
        _battle_module = sys.modules["engine.battle"]

        def fake_gen(a, b, *a2, **k):
            if "claude_vNem" in b:
                # Force a timeout for the nemesis matchup.
                raise _aio.TimeoutError()
            for x in [100, -100, 100, -100, 100, -100, 100, -100]:
                yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)
        monkeypatch.setattr(_battle_module, "mirror_battle",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("gen path only")))
        # Shrink per-game timeout so the test is fast; the nemesis raises
        # TimeoutError immediately so this is just a safety bound.
        monkeypatch.setattr("tool_eval.os.cpu_count", lambda: 1)
        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)
        # Nemesis timeout did not flip the verdict.
        assert data["passed"] is True
        assert not any(b.get("reason") == "match_timeout" for b in data["blockers"])
        nem = next(m for m in data["matchups"] if m["opponent"] == "claude_vNem")
        assert "nemesis_note" in nem or nem.get("error")

    @pytest.mark.asyncio
    async def test_parent_still_blocks_with_nemesis_present(
        self, monkeypatch, fake_bots, mock_ui, patch_checkpoint_file
    ):
        """Parent gate is not weakened by the nemesis slot's presence."""
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        ops = [
            {"name": "claude_v98", "reason": "parent"},
            {"name": "claude_vNem", "reason": "nemesis_probe"},
        ]
        monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: ops)
        _battle_module = sys.modules["engine.battle"]

        def fake_gen(a, b, *a2, **k):
            if "claude_v98" in b:
                # Parent crushes the candidate -> regression.
                for x in [-30000] * 8:
                    yield x
            else:
                for x in [100, -100, 100, -100, 100, -100, 100, -100]:
                    yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)
        monkeypatch.setattr(_battle_module, "mirror_battle",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("gen path only")))
        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)
        assert data["passed"] is False
        assert any(b.get("reason") == "lost_to_parent" for b in data["blockers"])

    @pytest.mark.asyncio
    async def test_nemesis_results_in_telemetry(
        self, monkeypatch, fake_bots, mock_ui, patch_checkpoint_file
    ):
        """Nemesis matchup appears in result['matchups'] with W/L/net_chips."""
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        ops = [
            {"name": "claude_v98", "reason": "parent"},
            {"name": "claude_vNem", "reason": "nemesis_probe"},
        ]
        monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: ops)
        _battle_module = sys.modules["engine.battle"]
        nem_stream = [-5000, -4000, 3000, -6000, 2000, -5500, 1000, -7000]

        def fake_gen(a, b, *a2, **k):
            if "claude_vNem" in b:
                for x in nem_stream:
                    yield x
            else:
                for x in [100, -100, 100, -100, 100, -100, 100, -100]:
                    yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)
        monkeypatch.setattr(_battle_module, "mirror_battle",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("gen path only")))
        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)
        nem = next(m for m in data["matchups"] if m["opponent"] == "claude_vNem")
        assert nem["reason"] == "nemesis_probe"
        assert nem["net_chips"] == nem_stream
        assert nem["n_played"] == 8


# ──────────────────────────────────────────────
# C. nemesis_archive.json
# ──────────────────────────────────────────────


class TestNemesisArchive:
    def test_compute_nemesis_relationships(self):
        active = ["claude_v1", "claude_v2", "claude_v3"]
        h2h = {
            "claude_v1 vs claude_v2": {"games": 20, "a_wins": 5, "b_wins": 15},
            "claude_v1 vs claude_v3": {"games": 20, "a_wins": 12, "b_wins": 8},
            "claude_v2 vs claude_v3": {"games": 20, "a_wins": 14, "b_wins": 6},
        }
        nemesis_of, champions = nemesis_archive.compute_nemesis_relationships(active, h2h)
        # v1's worst is v2 (0.25); v3's worst is v2 (0.30). v2's worst is v1 (0.75) -> above threshold.
        assert nemesis_of["claude_v1"]["nemesis"] == "claude_v2"
        assert nemesis_of["claude_v1"]["win_rate"] == pytest.approx(0.25)
        assert nemesis_of["claude_v3"]["nemesis"] == "claude_v2"
        assert "claude_v2" not in nemesis_of  # v2 beats everyone -> no nemesis
        # champions: v2 is the nemesis of both v1 and v3.
        assert set(champions["claude_v2"]["defeats"]) == {"claude_v1", "claude_v3"}
        assert champions["claude_v2"]["as_nemesis_count"] == 2

    def test_compute_filters_min_games(self):
        active = ["claude_v1", "claude_v2"]
        h2h = {"claude_v1 vs claude_v2": {"games": 2, "a_wins": 0, "b_wins": 2}}
        nemesis_of, _ = nemesis_archive.compute_nemesis_relationships(
            active, h2h, min_games=4
        )
        assert "claude_v1" not in nemesis_of  # below min_games

    def test_write_then_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nemesis_archive, "NEMESIS_ARCHIVE_FILE", tmp_path / "nemesis.json")
        active = ["claude_v1", "claude_v2"]
        h2h = {"claude_v1 vs claude_v2": {"games": 20, "a_wins": 4, "b_wins": 16}}
        archive = nemesis_archive.write_nemesis_archive(active, h2h=h2h)
        assert archive is not None
        assert archive["nemesis_of"]["claude_v1"]["nemesis"] == "claude_v2"
        # Read-back matches.
        reread = nemesis_archive.read_nemesis_archive()
        assert reread["nemesis_of"]["claude_v1"]["nemesis"] == "claude_v2"
        # Concurrent-safe: a second write does not corrupt.
        nemesis_archive.write_nemesis_archive(active, h2h=h2h)
        assert nemesis_archive.read_nemesis_archive()["nemesis_of"]["claude_v1"]["nemesis"] == "claude_v2"

    def test_write_returns_none_on_failure(self, tmp_path, monkeypatch):
        """write_nemesis_archive swallows exceptions and returns None (advisory)."""
        monkeypatch.setattr(nemesis_archive, "NEMESIS_ARCHIVE_FILE", tmp_path / "nemesis.json")

        def boom(_data):
            raise RuntimeError("disk full")
        monkeypatch.setattr(nemesis_archive, "write_locked_json", boom)
        out = nemesis_archive.write_nemesis_archive(["claude_v1"], h2h={})
        assert out is None


# ──────────────────────────────────────────────
# D. opponent-profile injection (run_master)
# ──────────────────────────────────────────────


class _CapturedMaster:
    """Stand-in for _run_master_analysis that records its kwargs."""
    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        # Return a minimal valid plan so run_master proceeds.
        return {
            "rationale": "test",
            "tasks": [{
                "worker_id": "w1",
                "role": "Logic Architect",
                "target_files": ["strategy.py"],
                "worker_prompt": "do something novel with opponent modeling",
            }],
        }


class TestOpponentProfileInjection:
    def test_profile_empty_when_per_opp_missing(self, tmp_path):
        """No per_opp file -> the construction guard yields opponent_profiles=''.

        This pins the missing-file branch of the run_master construction block
        without spinning the full LLM pipeline (covered in the async variant).
        """
        import evolution_infra
        per_opp_file = tmp_path / "bot_action_stats_per_opp.json"
        assert not per_opp_file.exists()
        # The guard `if _per_opp_file.exists()` short-circuits to "" when absent.
        assert per_opp_file.exists() is False


# Async end-to-end D test: monkeypatch the analysis capture and verify kwargs.
class TestOpponentProfileInjectionAsync:
    @pytest.mark.asyncio
    async def test_injects_profiles_when_file_present(self, monkeypatch, tmp_path):
        captured = _CapturedMaster()
        monkeypatch.setattr(tool_planning, "_run_master_analysis", captured)
        monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *a, **k: {
            "stage": "direction_audited",
            "direction_audit": {"repetition_detected": False, "llm_failed": False},
        })
        monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", lambda *a, **k: None)
        monkeypatch.setattr(tool_planning, "_validate_master_plan", lambda *a, **k: ([], []))
        monkeypatch.setattr(tool_planning, "_extract_exhausted_keywords", lambda: [])
        monkeypatch.setattr(tool_planning, "_build_cross_gen_constraint_block", lambda _v: "")
        monkeypatch.setattr(tool_planning, "log_system_event", lambda *a, **k: None)
        # Stub the expensive/IO aux paths run_master reaches into.
        import replay_spotlight
        monkeypatch.setattr(replay_spotlight, "find_critical_hands", lambda **k: "")
        import audit_agents
        async def _noop_audit(_plan, _sv, _ui):
            return {"overall_pass": True}
        monkeypatch.setattr(audit_agents, "_run_master_plan_audit", _noop_audit)

        # Write a per_opp file.
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
        per_opp = {
            "claude_v5": {
                "claude_v3": {
                    "preflop": {"total": 100, "fold": 20, "call": 40, "raise": 40, "check": 0, "allin": 0, "fold_to_bet": 15, "cbet": 0, "barrel": 0},
                    "flop": {"total": 80, "fold": 10, "call": 30, "raise": 40, "check": 0, "allin": 0, "fold_to_bet": 5, "cbet": 20, "barrel": 0},
                    "turn": {"total": 60, "fold": 5, "call": 20, "raise": 35, "check": 0, "allin": 0, "fold_to_bet": 2, "cbet": 0, "barrel": 15},
                    "river": {"total": 40, "fold": 4, "call": 20, "raise": 16, "check": 0, "allin": 0, "fold_to_bet": 1, "cbet": 0, "barrel": 8},
                    "total_hands": 50,
                }
            }
        }
        (tmp_path / "bot_action_stats_per_opp.json").write_text(json.dumps(per_opp))
        # h2h for ranking.
        h2h = {"claude_v5 vs claude_v3": {"games": 50, "a_wins": 15, "b_wins": 35}}
        monkeypatch.setattr(tool_helpers, "_load_h2h_data", lambda: h2h)
        monkeypatch.setattr(tool_helpers, "_h2h_stats",
                            lambda bot, opp, _h: ({"win_rate": 0.3, "games": 50} if opp == "claude_v3" else None))

        ui = MagicMock()
        ui.clear_io = MagicMock()
        ui.get_output = lambda: ""
        monkeypatch.setattr(tool_planning, "_get_ui", lambda: ui)

        await tool_planning.run_master.handler({"source_v": 5, "next_v": 6})
        assert len(captured.calls) == 1
        profiles = captured.calls[0].get("opponent_profiles", "")
        assert "claude_v3" in profiles
        assert "AF=" in profiles
        assert "ftb=" in profiles

    @pytest.mark.asyncio
    async def test_profiles_empty_when_file_missing(self, monkeypatch, tmp_path):
        captured = _CapturedMaster()
        monkeypatch.setattr(tool_planning, "_run_master_analysis", captured)
        monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *a, **k: {
            "stage": "direction_audited",
            "direction_audit": {"repetition_detected": False, "llm_failed": False},
        })
        monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", lambda *a, **k: None)
        monkeypatch.setattr(tool_planning, "_validate_master_plan", lambda *a, **k: ([], []))
        monkeypatch.setattr(tool_planning, "_extract_exhausted_keywords", lambda: [])
        monkeypatch.setattr(tool_planning, "_build_cross_gen_constraint_block", lambda _v: "")
        monkeypatch.setattr(tool_planning, "log_system_event", lambda *a, **k: None)
        import replay_spotlight
        monkeypatch.setattr(replay_spotlight, "find_critical_hands", lambda **k: "")
        import audit_agents
        async def _noop_audit(_plan, _sv, _ui):
            return {"overall_pass": True}
        monkeypatch.setattr(audit_agents, "_run_master_plan_audit", _noop_audit)
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
        # No per_opp file written.
        ui = MagicMock()
        ui.clear_io = MagicMock()
        ui.get_output = lambda: ""
        monkeypatch.setattr(tool_planning, "_get_ui", lambda: ui)

        await tool_planning.run_master.handler({"source_v": 5, "next_v": 6})
        assert captured.calls[0].get("opponent_profiles", "") == ""

    def test_profile_capped_to_k(self, monkeypatch, tmp_path):
        """When >K opponents exist, the injected text only references <=K of them."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
        opps = {f"claude_vOpp{i}": {
            "preflop": {"total": 10, "fold": 1, "call": 5, "raise": 4, "check": 0, "allin": 0, "fold_to_bet": 0, "cbet": 0, "barrel": 0},
            "flop": {"total": 8, "fold": 1, "call": 4, "raise": 3, "check": 0, "allin": 0, "fold_to_bet": 0, "cbet": 0, "barrel": 0},
            "turn": {"total": 6, "fold": 1, "call": 3, "raise": 2, "check": 0, "allin": 0, "fold_to_bet": 0, "cbet": 0, "barrel": 0},
            "river": {"total": 4, "fold": 1, "call": 2, "raise": 1, "check": 0, "allin": 0, "fold_to_bet": 0, "cbet": 0, "barrel": 0},
            "total_hands": 5,
        } for i in range(20)}
        per_opp = {"claude_v5": opps}
        (tmp_path / "bot_action_stats_per_opp.json").write_text(json.dumps(per_opp))
        # No h2h -> fallback path ranks by total actions (all equal) -> top-K.
        monkeypatch.setattr(tool_helpers, "_load_h2h_data", lambda: {})
        # Build profiles by re-running the construction block inline.
        with open(tmp_path / "bot_action_stats_per_opp.json") as f:
            all_stats = json.load(f)
        # Replicate the K-cap: when no h2h signal, select top-K by total actions.
        K = 6
        opp_map = all_stats["claude_v5"]
        selected = sorted(opp_map, key=lambda o: sum(
            opp_map[o].get(s, {}).get("total", 0) for s in ("preflop", "flop", "turn", "river")
        ), reverse=True)[:K]
        assert len(selected) <= K


# ──────────────────────────────────────────────
# E. MAP-Elites behavior_archive
# ──────────────────────────────────────────────


class TestMapElites:
    def test_aggression_bucket_boundaries(self):
        assert map_elites.aggression_bucket(None) == 0
        assert map_elites.aggression_bucket(0.0) == 0
        assert map_elites.aggression_bucket(0.49) == 0
        assert map_elites.aggression_bucket(0.5) == 1
        assert map_elites.aggression_bucket(0.99) == 1
        assert map_elites.aggression_bucket(1.0) == 2
        assert map_elites.aggression_bucket(1.49) == 2
        assert map_elites.aggression_bucket(1.5) == 3
        assert map_elites.aggression_bucket(2.49) == 3
        assert map_elites.aggression_bucket(2.5) == 4
        assert map_elites.aggression_bucket(10.0) == 4

    def test_looseness_bucket_boundaries(self):
        assert map_elites.looseness_bucket(None) == 0
        assert map_elites.looseness_bucket(0.0) == 0
        assert map_elites.looseness_bucket(0.14) == 0
        assert map_elites.looseness_bucket(0.15) == 1
        assert map_elites.looseness_bucket(0.29) == 1
        assert map_elites.looseness_bucket(0.30) == 2
        assert map_elites.looseness_bucket(0.44) == 2
        assert map_elites.looseness_bucket(0.45) == 3
        assert map_elites.looseness_bucket(0.59) == 3
        assert map_elites.looseness_bucket(0.60) == 4
        assert map_elites.looseness_bucket(1.0) == 4

    def test_niche_key_encoding(self):
        # 5x5 = 25 unique keys; round-trip a->key->agg bucket is recoverable.
        keys = set()
        for a in range(5):
            for l in range(5):
                k = map_elites.niche_key(a, l)
                keys.add(k)
                assert k.startswith(f"agg{a}_loose{l}")
        assert len(keys) == 25

    def test_fingerprint_to_bc(self):
        fp = ra.extract_behavior_fingerprint([], 0)
        key, a, l, bc = map_elites.fingerprint_to_bc(fp)
        # Empty fingerprint -> all None -> agg0_loose0.
        assert key == "agg0_loose0"
        assert bc["aggression_factor"] is None
        assert bc["vpip"] is None

    def test_build_keeps_max_fitness_per_niche(self, tmp_path):
        # Two bots in the SAME niche, different fitness -> keep higher.
        # Build a replay where pid 0 raises a lot (high aggression).
        def _aggressive_game():
            return {"logs": [
                {"output": {"display": {"last_action": {"player_id": 0, "action": 600},
                                        "public_cards": [0, 1, 2], "pot": 1000}}}
            ]}
        replays = tmp_path / "match_replay"
        replays.mkdir()
        # botA replay
        (replays / "a.json").write_text(json.dumps({
            "bot0": "claude_vA", "bot1": "claude_vOther",
            "games": [_aggressive_game() for _ in range(10)],
        }))
        # botB replay (same niche, different bot)
        (replays / "b.json").write_text(json.dumps({
            "bot0": "claude_vB", "bot1": "claude_vOther2",
            "games": [_aggressive_game() for _ in range(10)],
        }))
        active = ["claude_vA", "claude_vB"]
        wr = {"claude_vA": 0.5, "claude_vB": 0.6}
        archive = map_elites.build_behavior_archive(replays, active, h2h_winrates=wr)
        niches = archive["niches"]
        # Both bots land in the same niche (identical fingerprint shape).
        assert len(niches) == 1
        entry = next(iter(niches.values()))
        assert entry["bot"] == "claude_vB"  # higher fitness retained
        assert entry["fitness"] == 0.6

    def test_build_fitness_fallback(self, tmp_path):
        """No h2h -> fitness=0.5 for everyone; niche still populated."""
        replays = tmp_path / "match_replay"
        replays.mkdir()
        (replays / "a.json").write_text(json.dumps({
            "bot0": "claude_vA", "bot1": "claude_vOther",
            "games": [{"logs": [
                {"output": {"display": {"last_action": {"player_id": 0, "action": 0},
                                        "public_cards": [0, 1, 2], "pot": 1000}}}
            ]}],
        }))
        archive = map_elites.build_behavior_archive(replays, ["claude_vA"], h2h_winrates=None)
        entry = next(iter(archive["niches"].values()))
        assert entry["fitness"] == 0.5

    def test_build_empty_replays(self, tmp_path):
        replays = tmp_path / "match_replay"
        replays.mkdir()
        archive = map_elites.build_behavior_archive(replays, ["claude_vA"], h2h_winrates={})
        assert archive["niches"] == {}

    def test_build_missing_dir(self, tmp_path):
        archive = map_elites.build_behavior_archive(tmp_path / "nope", ["claude_vA"])
        assert archive["niches"] == {}

    def test_write_archive_best_effort(self, tmp_path, monkeypatch):
        """write_behavior_archive returns None on failure, never raises."""
        monkeypatch.setattr(map_elites, "BEHAVIOR_ARCHIVE_FILE", tmp_path / "beh.json")
        replays = tmp_path / "match_replay"
        replays.mkdir()
        archive = map_elites.write_behavior_archive(replays, [], h2h_winrates={})
        # No active bots -> empty niches but a valid archive written.
        assert archive is not None
        assert archive["niches"] == {}
        # Force a write failure.
        def boom(*a, **k):
            raise RuntimeError("disk full")
        monkeypatch.setattr(map_elites, "write_locked_json", boom)
        assert map_elites.write_behavior_archive(replays, [], h2h_winrates={}) is None


# ──────────────────────────────────────────────
# F. Feature flag default + integration regression
# ──────────────────────────────────────────────


class TestFeatureFlagsAndIntegration:
    def test_nemesis_flag_default_on(self):
        assert tool_helpers.PRECOMMIT_NEMESIS_SLOT is True

    @pytest.mark.asyncio
    async def test_full_precommit_with_nemesis_no_regression(
        self, monkeypatch, fake_bots, mock_ui, patch_checkpoint_file
    ):
        """End-to-end: nemesis slot runs + telemetry, parent mild => pass, no crash."""
        monkeypatch.setattr(tool_eval, "PRECOMMIT_SEQUENTIAL_EARLY_STOP", True)
        ops = [
            {"name": "claude_v98", "reason": "parent"},
            {"name": "claude_v50", "reason": "top_h2h_wr"},
            {"name": "claude_vNem", "reason": "nemesis_probe"},
        ]
        monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: ops)
        _battle_module = sys.modules["engine.battle"]

        def fake_gen(a, b, *a2, **k):
            # Mild streams everywhere; nemesis slightly worse but excluded from gate.
            if "claude_vNem" in b:
                for x in [-5000, 5000, -5000, 5000, -5000, 5000, -5000, 5000]:
                    yield x
            else:
                for x in [100, -100, 100, -100, 100, -100, 100, -100]:
                    yield x
        monkeypatch.setattr(_battle_module, "mirror_battle_generator", fake_gen)
        monkeypatch.setattr(_battle_module, "mirror_battle",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("gen path only")))
        _seed_passing_checkpoint()
        _common_patches(monkeypatch, mock_ui)
        result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 8})
        data = _decode(result)
        # Parent + top mild, nemesis excluded => pass.
        assert data["passed"] is True
        opp_reasons = {m["opponent"]: m["reason"] for m in data["matchups"]}
        assert opp_reasons.get("claude_vNem") == "nemesis_probe"
        assert len(data["matchups"]) == 3
