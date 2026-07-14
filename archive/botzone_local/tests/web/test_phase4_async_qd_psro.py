"""Phase 4 regression tests: async QD eval + k=3 median fitness + PSRO MVP.

Covers (per the Phase 4 design spec):
  A. Async QD eval (qd_async_eval): single-flight, worker writes archive, cancel
     flag, outstanding telemetry.
  B. QD k=3 fitness (qd_fitness): median over k samples, 10%-elite reeval
     selection.
  C. map_elites k=3 median merge: like-for-like comparison, backward compat with
     v1 archive, write_behavior_archive preserves k=3 fields.
  D. PSRO meta-solver (psro_meta_solver): fictitious play RPS convergence,
     dominant strategy, payoff symmetry, missing-reverse derivation, uniform.
  E. Legacy MixtureBot: per-hand weighted dispatch, hand pinning, subprocess
     passthrough, missing-config safe fold when an archived component is present.
  F. Engine contract hard gate: PSRO flag OFF default, engine battle signatures
     unchanged, precommit path byte-identical when PSRO off (opponents contain
     no mixture_main).

Pure-function + synthetic-mock tests. The one real-subprocess path (MixtureBot
engine integration smoke) is covered by a manual test documented in the task
output, NOT run here (it would slow the suite). The MixtureBot dispatch tests
use a mock sub-bot so no real bot logic runs.
"""

import asyncio
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "core"))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "server"))

import psro_meta_solver
import qd_fitness
import qd_async_eval
import map_elites
import tool_eval


# ===========================================================================
# D. PSRO meta-solver (offline fixtures)
# ===========================================================================

class TestPSROMetaSolver:
    def test_fictitious_play_rps_converges_uniform(self):
        # Classic Rock-Paper-Scissors zero-sum game.
        rps = [[0.5, 1.0, 0.0], [0.0, 0.5, 1.0], [1.0, 0.0, 0.5]]
        rng = random.Random(42)
        meta = psro_meta_solver.fictitious_play(rps, ["R", "P", "S"], iterations=5000, rng=rng)
        # Nash for symmetric RPS is uniform (1/3, 1/3, 1/3).
        for v in meta.values():
            assert abs(v - 1 / 3) < 0.05, f"RPS should converge to uniform, got {meta}"

    def test_fictitious_play_dominant_strategy(self):
        # Strategy A dominates (beats everyone); FP should put all mass on A.
        dom = [[0.5, 1.0, 1.0], [0.0, 0.5, 0.9], [0.0, 0.1, 0.5]]
        meta = psro_meta_solver.fictitious_play(dom, ["A", "B", "C"], iterations=3000,
                                                rng=random.Random(7))
        assert meta["A"] > 0.99, f"Dominant A should win all mass, got {meta}"
        assert meta["B"] < 0.01 and meta["C"] < 0.01

    def test_fictitious_play_single_strategy(self):
        meta = psro_meta_solver.fictitious_play([[0.5]], ["only"], iterations=10)
        assert meta == {"only": 1.0}

    def test_fictitious_play_empty(self):
        assert psro_meta_solver.fictitious_play([], [], iterations=10) == {}

    def test_fictitious_play_convergence_stable(self):
        # More iterations should not move the distribution much (converged).
        rps = [[0.5, 1.0, 0.0], [0.0, 0.5, 1.0], [1.0, 0.0, 0.5]]
        m1 = psro_meta_solver.fictitious_play(rps, ["R", "P", "S"], iterations=2000, rng=random.Random(1))
        m2 = psro_meta_solver.fictitious_play(rps, ["R", "P", "S"], iterations=5000, rng=random.Random(1))
        l1 = sum(abs(m1[k] - m2[k]) for k in m1)
        assert l1 < 0.10, f"FP should converge: L1={l1}"

    def test_build_payoff_matrix_symmetric(self):
        # matrix[i][j] + matrix[j][i] == 1 (zero-sum symmetric).
        h2h = {
            "x vs y": {"win_rate": 0.6},
            "y vs z": {"win_rate": 0.4},
            "x vs z": {"win_rate": 0.55},
        }
        pop = ["x", "y", "z"]
        M = psro_meta_solver.build_payoff_matrix(h2h, pop)
        assert len(M) == 3
        for i in range(3):
            for j in range(3):
                assert abs(M[i][j] + M[j][i] - 1.0) < 1e-9, (i, j, M[i][j], M[j][i])
        # forward key direct read
        assert M[0][1] == 0.6  # x vs y

    def test_build_payoff_matrix_missing_reverse(self):
        # Only forward key present for one pair; reverse must derive 1 - wr.
        h2h = {"a vs b": {"win_rate": 0.7}}
        M = psro_meta_solver.build_payoff_matrix(h2h, ["a", "b"])
        assert M[0][1] == 0.7       # a vs b forward
        assert abs(M[1][0] - 0.3) < 1e-9  # b vs a derived

    def test_build_payoff_matrix_both_missing(self):
        h2h = {}
        M = psro_meta_solver.build_payoff_matrix(h2h, ["a", "b"])
        assert M[0][1] == 0.5 and M[1][0] == 0.5  # no info -> symmetric 0.5

    def test_solve_meta_uniform_fallback(self):
        meta = psro_meta_solver.solve_meta({}, ["a", "b", "c"], method="uniform")
        assert meta == {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}

    def test_solve_meta_unknown_method_is_uniform(self):
        # Defensive: unknown method never raises -> uniform.
        meta = psro_meta_solver.solve_meta({}, ["a", "b"], method="bogus")
        assert meta == {"a": 0.5, "b": 0.5}

    def test_uniform_meta_empty(self):
        assert psro_meta_solver.uniform_meta([]) == {}

    def test_sample_meta_distribution(self):
        # Weighted sampling over many draws should approximate the weights.
        meta = {"A": 0.5, "B": 0.3, "C": 0.2}
        rng = random.Random(0)
        counts = {"A": 0, "B": 0, "C": 0}
        for _ in range(3000):
            b = psro_meta_solver.sample_meta(meta, rng=rng)
            counts[b] += 1
        total = sum(counts.values())
        for k, w in meta.items():
            assert abs(counts[k] / total - w) < 0.04, (k, counts[k] / total, w)

    def test_sample_meta_empty(self):
        assert psro_meta_solver.sample_meta({}) is None

    def test_build_mixture_config(self, tmp_path):
        # Create fake sub-bot main.py files.
        paths = {}
        for b in ["claude_v1", "claude_v2", "claude_v3"]:
            p = tmp_path / b / "main.py"
            p.parent.mkdir(parents=True)
            p.write_text("# stub")
            paths[b] = str(p)
        h2h = {"claude_v1 vs claude_v2": {"win_rate": 0.6},
               "claude_v2 vs claude_v3": {"win_rate": 0.55}}
        cfg = psro_meta_solver.build_mixture_config(h2h, list(paths.keys()), paths,
                                                    method="fp", iterations=1000)
        assert set(cfg["strategy_weights"]) <= set(paths)
        assert all(os.path.isfile(p) for p in cfg["bot_paths"].values())
        assert abs(sum(cfg["strategy_weights"].values()) - 1.0) < 1e-6

    def test_build_mixture_config_drops_missing_paths(self, tmp_path):
        paths = {"claude_v1": str(tmp_path / "claude_v1" / "main.py")}
        # path does not exist -> empty mixture (no crash)
        cfg = psro_meta_solver.build_mixture_config({}, ["claude_v1"], paths)
        assert cfg["strategy_weights"] == {}


# ===========================================================================
# B. QD k=3 fitness
# ===========================================================================

class TestQDFitness:
    def test_evaluate_commit_version_k_median(self, monkeypatch):
        # Mock _run_single_eval to return controlled samples.
        samples = {0: [0.4, 0.6, 0.5]}  # seed-base+run_idx -> per-run rates
        calls = {"n": 0}

        def fake_run(bot_main, opp_main, n_games, seed):
            calls["n"] += 1
            run_idx = seed  # seed_base=0
            return samples[0][run_idx]

        monkeypatch.setattr(qd_fitness, "_run_single_eval", fake_run)
        result = qd_fitness.evaluate_commit_version_k(
            "/fake/main.py", ["/fake/opp.py"], k=3, n_games=8, seed_base=0
        )
        assert result["eval_mode"] == "k3"
        assert result["k"] == 3
        assert result["fitness_samples"] == [0.4, 0.6, 0.5]
        # median of [0.4, 0.6, 0.5] = 0.5
        assert result["fitness_median"] == 0.5
        assert result["completed"] == 3

    def test_evaluate_commit_version_k_multi_opponent_averages(self, monkeypatch):
        # 2 opponents, k=2: per-sample fitness = mean win rate across opponents.
        def fake_run(bot_main, opp_main, n_games, seed):
            # opponent path encodes the win rate
            return 0.6 if "strong" in opp_main else 0.4

        monkeypatch.setattr(qd_fitness, "_run_single_eval", fake_run)
        result = qd_fitness.evaluate_commit_version_k(
            "/fake/main.py", ["/bot/strong", "/bot/weak"], k=2, n_games=8
        )
        # Each run averages (0.6, 0.4) = 0.5 -> samples = [0.5, 0.5]
        assert result["fitness_samples"] == [0.5, 0.5]
        assert result["fitness_median"] == 0.5

    def test_evaluate_commit_version_k_handles_none_samples(self, monkeypatch):
        def fake_run(bot_main, opp_main, n_games, seed):
            return None  # all runs fail
        monkeypatch.setattr(qd_fitness, "_run_single_eval", fake_run)
        result = qd_fitness.evaluate_commit_version_k("/m", ["/o"], k=3, n_games=8)
        assert result["fitness_samples"] == []
        assert result["fitness_median"] == 0.5  # fallback
        assert result["completed"] == 0

    def test_evaluate_commit_version_k_cancel_check_aborts_early(self, monkeypatch):
        # M1: cancel_check must propagate into the internal opponent/run loop
        # (not just be checked before/after evaluate_commit_version_k), so a
        # shutdown / watchdog can break the eval promptly. Partial results are
        # returned.
        evaluated = []

        def fake_run(bot_main, opp_main, n_games, seed):
            evaluated.append(opp_main)
            return 0.5

        monkeypatch.setattr(qd_fitness, "_run_single_eval", fake_run)
        opps = ["/bot/a", "/bot/b", "/bot/c", "/bot/d"]

        def cancel_after_first_opponent():
            # Each opponent runs k=3 times; cancel once opponent a is done so
            # b/c/d are never evaluated.
            return len(evaluated) >= 3

        result = qd_fitness.evaluate_commit_version_k(
            "/m", opps, k=3, n_games=8, cancel_check=cancel_after_first_opponent
        )
        # Only opponent a (3 runs) was evaluated; the loop aborted at opp b.
        assert len(evaluated) == 3
        assert "/bot/b" not in result["per_opponent"]
        assert "/bot/c" not in result["per_opponent"]
        assert "/bot/d" not in result["per_opponent"]
        # Partial result still well-formed.
        assert result["eval_mode"] == "k3"
        assert result["fitness_median"] == 0.5

    def test_reevaluate_top_elites_marks_10pct(self):
        arch = {"niches": {f"k{i}": {"bot": f"claude_v{i}", "fitness": i / 25.0}
                            for i in range(25)}}
        elites = qd_fitness.reevaluate_top_elites(arch, fraction=0.10)
        # ceil(25 * 0.10) = 3
        assert len(elites) == 3
        # highest-fitness bots
        assert elites == ["claude_v24", "claude_v23", "claude_v22"]

    def test_reevaluate_top_elites_min_one(self):
        arch = {"niches": {"k0": {"bot": "claude_v1", "fitness": 0.5},
                            "k1": {"bot": "claude_v2", "fitness": 0.6}}}
        elites = qd_fitness.reevaluate_top_elites(arch, fraction=0.10)
        assert len(elites) == 1
        assert elites[0] == "claude_v2"

    def test_reevaluate_top_elites_empty(self):
        assert qd_fitness.reevaluate_top_elites({}) == []
        assert qd_fitness.reevaluate_top_elites({"niches": {}}) == []

    def test_reevaluate_top_elites_does_not_mutate(self):
        arch = {"niches": {"k0": {"bot": "claude_v1", "fitness": 0.5, "reeval_due": False}}}
        before = json.loads(json.dumps(arch))
        qd_fitness.reevaluate_top_elites(arch)
        assert arch == before  # pure data


# ===========================================================================
# C. map_elites k=3 median merge
# ===========================================================================

class TestMapElitesK3Merge:
    def test_build_archive_prefers_k3_median(self):
        # Two bots same niche: single(0.55) vs k3(median 0.5) -> single wins on
        # the like-for-like comparison (0.55 scalar > 0.5 median).
        single = {"bot": "claude_v1", "version": 1, "fitness": 0.55,
                  "fitness_median": None, "eval_mode": "single"}
        k3 = {"bot": "claude_v2", "version": 2, "fitness": 0.5,
              "fitness_median": 0.5, "fitness_samples": [0.4, 0.6, 0.5],
              "eval_mode": "k3"}
        assert map_elites._better_niche_occupant(single, k3) is True   # 0.55 > 0.5
        assert map_elites._better_niche_occupant(k3, single) is False

    def test_k3_median_beats_lower_single(self):
        single = {"fitness": 0.45, "fitness_median": None, "eval_mode": "single"}
        k3 = {"fitness": 0.6, "fitness_median": 0.6, "eval_mode": "k3"}
        assert map_elites._better_niche_occupant(k3, single) is True

    def test_build_archive_backward_compat_v1_entries(self):
        # v1 archive entries (no new fields) must still yield a readable fitness.
        v1 = {"bot": "claude_v1", "fitness": 0.7}
        assert map_elites._niche_fitness_value(v1) == 0.7
        # malformed entry -> fallback
        assert map_elites._niche_fitness_value({"bot": "x"}) == 0.5

    def test_build_behavior_archive_emits_single_mode(self, monkeypatch, tmp_path):
        # A single-eval build should mark entries eval_mode="single".
        fp = {"aggression_factor": 1.0, "vpip": 0.3, "total_actions": 5}
        monkeypatch.setattr(map_elites, "_scan_behavior_fingerprints",
                            lambda replays, bots: {"claude_v1": fp})
        arch = map_elites.build_behavior_archive(tmp_path, ["claude_v1"],
                                                  h2h_winrates={"claude_v1": 0.6})
        niches = arch["niches"]
        assert len(niches) == 1
        entry = next(iter(niches.values()))
        assert entry["eval_mode"] == "single"
        assert entry["fitness_median"] is None
        assert entry["fitness"] == 0.6

    def test_write_behavior_archive_preserves_k3_fields(self, monkeypatch, tmp_path):
        # Patch archive file to tmp, seed a prior k=3 entry, rebuild as single,
        # confirm the k=3 median survives for the same occupant bot.
        archive_file = tmp_path / "behavior_archive.json"
        monkeypatch.setattr(map_elites, "BEHAVIOR_ARCHIVE_FILE", archive_file)
        # Use a fingerprint that maps to agg1_loose1: af in [0.5,1.0), vpip in
        # [0.15,0.30). af=0.7 -> agg1, vpip=0.2 -> loose1.
        niche = "agg1_loose1"
        prior = {
            "version": 1, "updated_at": "t", "bc_note": "n",
            "niches": {niche: {
                "bot": "claude_v1", "version": 1, "fitness": 0.55,
                "bc": {}, "last_eval": "t",
                "fitness_median": 0.62, "fitness_samples": [0.5, 0.62, 0.7],
                "eval_mode": "k3",
            }},
        }
        archive_file.write_text(json.dumps(prior))

        fp = {"aggression_factor": 0.7, "vpip": 0.2, "total_actions": 5}
        monkeypatch.setattr(map_elites, "_scan_behavior_fingerprints",
                            lambda replays, bots: {"claude_v1": fp})
        map_elites.write_behavior_archive(tmp_path, ["claude_v1"],
                                          h2h_winrates={"claude_v1": 0.55})
        out = json.loads(archive_file.read_text())
        entry = out["niches"][niche]
        assert entry["eval_mode"] == "k3"
        assert entry["fitness_median"] == 0.62
        assert entry["fitness_samples"] == [0.5, 0.62, 0.7]


# ===========================================================================
# A. Async QD eval (fire-and-forget)
# ===========================================================================

class TestQDAsyncEval:
    def test_qd_eval_single_flight(self, monkeypatch):
        # Two consecutive launches: the second must skip (single-flight).
        qd_async_eval._qd_eval_running.clear()
        qd_async_eval._qd_cancel.clear()

        # Make the worker block so the first launch stays "running".
        block_evt = threading.Event()

        def slow_worker():
            block_evt.wait(timeout=5)
            qd_async_eval._qd_eval_running.clear()

        monkeypatch.setattr(qd_async_eval, "_qd_eval_worker", slow_worker, raising=False)
        # Directly exercise the single-flight guard at the launch level by
        # setting the flag manually (simulating an in-flight worker).
        qd_async_eval._qd_eval_running.set()
        events = []
        import system_log
        monkeypatch.setattr(system_log, "log_system_event",
                            lambda *a, **k: events.append(a[0]))
        launched = qd_async_eval.launch_qd_eval(99, 90, shutdown_mgr=None)
        assert launched is False
        assert "pipeline.qd_eval_skipped" in events
        # cleanup
        qd_async_eval._qd_eval_running.clear()

    def test_qd_eval_shutdown_skip(self, monkeypatch):
        import system_log
        events = []
        monkeypatch.setattr(system_log, "log_system_event",
                            lambda *a, **k: events.append(a[0]))
        qd_async_eval._qd_eval_running.clear()
        shutdown = MagicMock()
        shutdown.is_shutting_down = True
        launched = qd_async_eval.launch_qd_eval(99, 90, shutdown_mgr=shutdown)
        assert launched is False
        assert "pipeline.qd_eval_skipped" in events

    def test_qd_eval_worker_writes_archive(self, monkeypatch, tmp_path):
        # Patch archive file + verify the worker merges k=3 fields.
        archive_file = tmp_path / "behavior_archive.json"
        monkeypatch.setattr(map_elites, "BEHAVIOR_ARCHIVE_FILE", archive_file)
        prior = {"version": 1, "updated_at": "t", "bc_note": "n",
                 "niches": {"agg1_loose1": {
                     "bot": "national_v55", "version": 55, "fitness": 0.5,
                     "bc": {}, "last_eval": "t", "eval_mode": "single"}}}
        archive_file.write_text(json.dumps(prior))

        # Fake candidate main.py exists.
        from evolution_infra import BOTS_DIR
        # Use a real bot dir that exists via symlinked bots in conftest.
        # We point get_bot_dir(55) at a tmp file by patching get_bot_dir.
        monkeypatch.setattr("evolution_infra.get_bot_dir",
                            lambda v: tmp_path / f"national_v{v}")
        (tmp_path / "national_v55" / "main.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "national_v55" / "main.py").write_text("# stub")

        # Mock the eval + opponent selection.
        monkeypatch.setattr(qd_async_eval, "_select_eval_opponents",
                            lambda src, max_opponents=3: [str(tmp_path / "opp.py")])
        monkeypatch.setattr(qd_fitness, "evaluate_commit_version_k",
                            lambda *a, **k: {"fitness_samples": [0.4, 0.6, 0.5],
                                              "fitness_median": 0.5, "k": 3,
                                              "n_games": 8, "eval_mode": "k3",
                                              "completed": 3})

        import system_log
        monkeypatch.setattr(system_log, "log_system_event", lambda *a, **k: None)

        qd_async_eval._qd_eval_running.clear()
        qd_async_eval._qd_cancel.clear()
        launched = qd_async_eval.launch_qd_eval(55, 50, shutdown_mgr=None)
        assert launched is True
        # Wait for the worker daemon thread to finish (poll up to 5s).
        deadline = time.time() + 5
        while qd_async_eval._qd_eval_running.is_set() and time.time() < deadline:
            time.sleep(0.05)
        assert not qd_async_eval._qd_eval_running.is_set(), "worker did not finish"

        out = json.loads(archive_file.read_text())
        entry = out["niches"]["agg1_loose1"]
        assert entry["eval_mode"] == "k3"
        assert entry["fitness_median"] == 0.5
        assert entry["fitness_samples"] == [0.4, 0.6, 0.5]

    def test_qd_eval_watchdog_cancel_keeps_result(self, monkeypatch, tmp_path):
        # When the cancel flag is set (e.g. by the watchdog timeout), the worker
        # must NOT merge results into the archive. We simulate this by making the
        # evaluation SLOW and a short watchdog timeout that fires the cancel
        # mid-eval; the worker then discards the result.
        archive_file = tmp_path / "behavior_archive.json"
        monkeypatch.setattr(map_elites, "BEHAVIOR_ARCHIVE_FILE", archive_file)
        prior = {"version": 1, "updated_at": "t", "bc_note": "n",
                 "niches": {"agg1_loose1": {
                     "bot": "national_v88", "version": 88, "fitness": 0.5,
                     "bc": {}, "last_eval": "t", "eval_mode": "single"}}}
        archive_file.write_text(json.dumps(prior))

        monkeypatch.setattr("evolution_infra.get_bot_dir",
                            lambda v: tmp_path / f"national_v{v}")
        (tmp_path / "national_v88").mkdir(parents=True, exist_ok=True)
        (tmp_path / "national_v88" / "main.py").write_text("# stub")
        monkeypatch.setattr(qd_async_eval, "_select_eval_opponents",
                            lambda src, max_opponents=3: [str(tmp_path / "opp.py")])

        def slow_eval(*a, **k):
            # Sleep longer than the watchdog timeout so cancel fires mid-eval.
            time.sleep(1.0)
            return {"fitness_samples": [0.4, 0.6, 0.5], "fitness_median": 0.5,
                    "k": 3, "n_games": 8, "eval_mode": "k3", "completed": 3}
        monkeypatch.setattr(qd_fitness, "evaluate_commit_version_k", slow_eval)
        import system_log
        monkeypatch.setattr(system_log, "log_system_event", lambda *a, **k: None)

        qd_async_eval._qd_eval_running.clear()
        qd_async_eval._qd_cancel.clear()
        # 0.2s watchdog -> fires during the 1s slow_eval -> cancel is_set() after.
        launched = qd_async_eval.launch_qd_eval(88, 80, shutdown_mgr=None, timeout_sec=0.2)
        assert launched is True
        deadline = time.time() + 6
        while qd_async_eval._qd_eval_running.is_set() and time.time() < deadline:
            time.sleep(0.05)
        assert not qd_async_eval._qd_eval_running.is_set(), "worker did not finish"
        # 9fa730f root-cause fix: a watchdog timeout that fires AFTER the eval already
        # produced usable fitness samples KEEPS the result. The old code discarded it
        # (treating watchdog == shutdown), causing 100% QD-eval cancellation and zero k3
        # archive entries. Now watchdog+samples fall through to the archive merge, so the
        # entry is updated to k3 with the median (NOT left as the stale single-eval entry).
        out = json.loads(archive_file.read_text())
        entry = out["niches"]["agg1_loose1"]
        assert entry["eval_mode"] == "k3"
        assert entry.get("fitness_median") == 0.5
        qd_async_eval._qd_cancel.clear()

    def test_outstanding_async_tasks_count(self, monkeypatch):
        # Synthesize a system_events tail with one unmatched start.
        events = [
            {"ts": 1, "type": "pipeline.qd_eval_start"},
            {"ts": 2, "type": "pipeline.qd_eval_done"},
            {"ts": 3, "type": "pipeline.qd_eval_start"},  # unmatched
        ]
        monkeypatch.setattr(qd_async_eval, "_read_system_events_tail", lambda max_lines=2000: events)
        assert qd_async_eval.outstanding_async_tasks() == 1

    def test_outstanding_async_tasks_zero_when_matched(self, monkeypatch):
        events = [
            {"ts": 1, "type": "pipeline.qd_eval_start"},
            {"ts": 2, "type": "pipeline.qd_eval_done"},
        ]
        monkeypatch.setattr(qd_async_eval, "_read_system_events_tail", lambda max_lines=2000: events)
        assert qd_async_eval.outstanding_async_tasks() == 0

    def test_cancel_qd_eval_sets_flag(self):
        qd_async_eval._qd_cancel.clear()
        qd_async_eval.cancel_qd_eval()
        assert qd_async_eval._qd_cancel.is_set()
        qd_async_eval._qd_cancel.clear()

    def test_qd_eval_excludes_candidate_self_match(self, monkeypatch, tmp_path):
        # M2: the candidate (already committed + active by the time
        # post_generation_cleanup runs) must not be selected as its own
        # opponent — a self mirror_battle yields ~0.5 win-rate samples that
        # would inflate the median fitness.
        archive_file = tmp_path / "behavior_archive.json"
        monkeypatch.setattr(map_elites, "BEHAVIOR_ARCHIVE_FILE", archive_file)
        prior = {"version": 1, "updated_at": "t", "bc_note": "n",
                 "niches": {"agg1_loose1": {
                     "bot": "claude_v55", "version": 55, "fitness": 0.5,
                     "bc": {}, "last_eval": "t", "eval_mode": "single"}}}
        archive_file.write_text(json.dumps(prior))

        monkeypatch.setattr("evolution_infra.get_bot_dir",
                            lambda v: tmp_path / f"claude_v{v}")
        (tmp_path / "claude_v55").mkdir(parents=True, exist_ok=True)
        cand_main = tmp_path / "claude_v55" / "main.py"
        cand_main.write_text("# stub")
        opp_main = str(tmp_path / "opp.py")

        # _select_eval_opponents returns the candidate path + a real opponent.
        monkeypatch.setattr(qd_async_eval, "_select_eval_opponents",
                            lambda src, max_opponents=3: [str(cand_main), opp_main])

        captured = {}
        def capture_eval(bot_main, opponents, **k):
            captured["opponents"] = list(opponents)
            return {"fitness_samples": [0.5], "fitness_median": 0.5, "k": 1,
                    "n_games": 8, "eval_mode": "k3", "completed": 1}
        monkeypatch.setattr(qd_fitness, "evaluate_commit_version_k", capture_eval)
        import system_log
        monkeypatch.setattr(system_log, "log_system_event", lambda *a, **k: None)

        qd_async_eval._qd_eval_running.clear()
        qd_async_eval._qd_cancel.clear()
        qd_async_eval.launch_qd_eval(55, 50, shutdown_mgr=None)
        deadline = time.time() + 5
        while qd_async_eval._qd_eval_running.is_set() and time.time() < deadline:
            time.sleep(0.05)

        # Candidate filtered out; only the real opponent reached the evaluator.
        assert str(cand_main) not in captured.get("opponents", [str(cand_main)])
        assert opp_main in captured["opponents"]


# ===========================================================================
# E. Legacy MixtureBot dispatch (offline, mock sub-bots)
# ===========================================================================

class TestMixtureBot:
    @pytest.fixture
    def mixture_main(self):
        active = _PROJECT_ROOT / "bots" / "mixture_main" / "main.py"
        if active.exists():
            return active
        archives = sorted(_PROJECT_ROOT.glob("archive/evolution_epochs/*/legacy_bots/mixture_main/main.py"))
        if archives:
            return archives[-1]
        pytest.skip("legacy mixture_main is archived out of the active national bot namespace")

    @pytest.fixture
    def write_config(self, mixture_main):
        """Write a mixture_config.json pointing at tmp sub-bots; cleanup after."""
        cfg_path = mixture_main.parent / "mixture_config.json"

        def _write(weights, paths):
            cfg = {"strategy_weights": weights, "bot_paths": paths}
            cfg_path.write_text(json.dumps(cfg))
            return cfg_path
        yield _write
        # cleanup: remove config so other tests / production never see it
        if cfg_path.exists():
            cfg_path.unlink()

    def _make_sub_bot(self, tmp_path, name, response_tag=None):
        """Create a stub sub-bot main.py that echoes its name in `data`."""
        p = tmp_path / name / "main.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        tag = response_tag if response_tag is not None else name
        p.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    line=line.strip()\n"
            "    if not line: continue\n"
            "    json.loads(line)\n"
            f"    print(json.dumps({{'response': 200, 'data': '{tag}'}}))\n"
            "    sys.stdout.flush()\n"
        )
        return str(p)

    def test_mixture_bot_missing_config_skips(self, mixture_main):
        # No config present -> every decision is a safe fold.
        # Ensure config is absent.
        cfg_path = mixture_main.parent / "mixture_config.json"
        had = cfg_path.exists()
        if had:
            cfg_path.unlink()
        try:
            payload = json.dumps({"requests": [{"hand": 0, "my_id": 0}], "responses": []}) + "\n"
            proc = subprocess.run([sys.executable, str(mixture_main)],
                                  input=payload, capture_output=True, text=True, timeout=20)
            out = json.loads(proc.stdout.strip())
            assert out["response"] == -1  # safe fold
        finally:
            pass

    def test_mixture_bot_weighted_choice_distribution(self, mixture_main, write_config, tmp_path):
        sub_a = self._make_sub_bot(tmp_path, "claude_vA")
        sub_b = self._make_sub_bot(tmp_path, "claude_vB")
        sub_c = self._make_sub_bot(tmp_path, "claude_vC")
        write_config({"claude_vA": 0.5, "claude_vB": 0.3, "claude_vC": 0.2},
                     {"claude_vA": sub_a, "claude_vB": sub_b, "claude_vC": sub_c})

        # 200 hands, 1 decision each (hand changes every line).
        lines = []
        for hand in range(200):
            lines.append(json.dumps({"requests": [{"hand": hand, "my_id": 0}], "responses": []}))
        payload = "\n".join(lines) + "\n"
        proc = subprocess.run([sys.executable, str(mixture_main)],
                              input=payload, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        outs = [json.loads(l) for l in proc.stdout.strip().split("\n") if l]
        assert len(outs) == 200
        counts = {"claude_vA": 0, "claude_vB": 0, "claude_vC": 0}
        for o in outs:
            tag = o.get("data")
            assert tag in counts, f"unexpected tag {tag}"
            counts[tag] += 1
        total = sum(counts.values())
        for bot, w in [("claude_vA", 0.5), ("claude_vB", 0.3), ("claude_vC", 0.2)]:
            assert abs(counts[bot] / total - w) < 0.10, (bot, counts[bot] / total, w)

    def test_mixture_bot_per_hand_pin_and_switch(self, mixture_main, write_config, tmp_path):
        sub_a = self._make_sub_bot(tmp_path, "claude_vA")
        sub_b = self._make_sub_bot(tmp_path, "claude_vB")
        write_config({"claude_vA": 0.5, "claude_vB": 0.5},
                     {"claude_vA": sub_a, "claude_vB": sub_b})
        # 30 decisions across 3 hands (10 decisions each).
        lines = []
        for hand in range(3):
            for _ in range(10):
                lines.append(json.dumps({"requests": [{"hand": hand, "my_id": 0}], "responses": []}))
        payload = "\n".join(lines) + "\n"
        proc = subprocess.run([sys.executable, str(mixture_main)],
                              input=payload, capture_output=True, text=True, timeout=60)
        outs = [json.loads(l).get("data") for l in proc.stdout.strip().split("\n") if l]
        # Within each hand, all 10 decisions share the same sub-bot.
        for hand in range(3):
            chunk = outs[hand * 10:(hand + 1) * 10]
            assert len(set(chunk)) == 1, f"hand {hand} not pinned: {set(chunk)}"

    def test_mixture_bot_subprocess_passesthrough_action(self, mixture_main, write_config, tmp_path):
        # Sub-bot returns a specific action; MixtureBot must forward it.
        sub = self._make_sub_bot(tmp_path, "claude_vX", response_tag="X")
        # Overwrite sub-bot to return a raise-to-333.
        Path(sub).write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    line=line.strip()\n"
            "    if not line: continue\n"
            "    json.loads(line)\n"
            "    print(json.dumps({'response': 333, 'data': 'X'}))\n"
            "    sys.stdout.flush()\n"
        )
        write_config({"claude_vX": 1.0}, {"claude_vX": sub})
        payload = json.dumps({"requests": [{"hand": 0, "my_id": 0}], "responses": []}) + "\n"
        proc = subprocess.run([sys.executable, str(mixture_main)],
                              input=payload, capture_output=True, text=True, timeout=20)
        out = json.loads(proc.stdout.strip())
        assert out["response"] == 333
        assert out.get("data") == "X"


# ===========================================================================
# F. Engine contract hard gate
# ===========================================================================

class TestEngineContract:
    def test_psro_flag_off_default(self, monkeypatch):
        # Reload evolution_infra without env var -> PSRO_ENABLED False.
        monkeypatch.delenv("POK_PSRO_ENABLED", raising=False)
        import importlib
        import evolution_infra
        importlib.reload(evolution_infra)
        assert evolution_infra.PSRO_ENABLED is False

    def test_psro_flag_on_with_env(self, monkeypatch):
        monkeypatch.setenv("POK_PSRO_ENABLED", "1")
        import importlib
        import evolution_infra
        importlib.reload(evolution_infra)
        assert evolution_infra.PSRO_ENABLED is True
        # restore
        monkeypatch.delenv("POK_PSRO_ENABLED", raising=False)
        importlib.reload(evolution_infra)

    def test_engine_battle_signatures_unchanged(self):
        # The 2-player contract signatures must be byte-for-byte unchanged.
        # Import BOTH engine copies (top-level + web/core) and assert the
        # 2-player Popen contract (battle/mirror_battle take bot0_path, bot1_path).
        import inspect
        import importlib

        # Top-level engine/battle.py (the real contract).
        top_engine = _PROJECT_ROOT / "engine"
        if str(top_engine) not in sys.path:
            sys.path.insert(0, str(top_engine))
        eb = importlib.import_module("battle")
        sig_battle = inspect.signature(eb.battle)
        sig_mirror = inspect.signature(eb.mirror_battle)
        params_battle = list(sig_battle.parameters)
        assert params_battle[0] == "bot0_path"
        assert params_battle[1] == "bot1_path"
        params_mirror = list(sig_mirror.parameters)
        assert params_mirror[0] == "bot0_path"
        assert params_mirror[1] == "bot1_path"
        assert sig_mirror.parameters["n_games"].default == 50
        # mirror_battle still returns the documented 5-tuple via its return stmt;
        # verify by code object constant presence (defensive).
        src = inspect.getsource(eb.mirror_battle)
        assert "net_chips_list" in src  # 5th tuple element unchanged

    def test_precommit_no_mixture_when_psro_off(self, monkeypatch):
        # When PSRO_ENABLED is False, the mixture opponent must NOT be injected.
        # We test the helper directly: _maybe_add_mixture_opponent checks the flag.
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "PSRO_ENABLED", False)
        result = tool_eval._maybe_add_mixture_opponent(99, 90)
        assert result is None

    def test_nonblocking_reasons_cover_mixture(self):
        # The mixture reason must be in the non-blocking set.
        assert tool_eval._is_nonblocking_reason("psro_meta_opponent") is True
        assert tool_eval._is_nonblocking_reason("nemesis_probe") is True
        assert tool_eval._is_nonblocking_reason("top_opponent") is False
        assert tool_eval._is_nonblocking_reason(None) is False
