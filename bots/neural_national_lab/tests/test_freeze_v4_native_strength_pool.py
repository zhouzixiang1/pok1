from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bots.neural_national_lab.tools import freeze_v4_native_strength_pool as freeze
from bots.neural_national_lab.tools import evaluate_v4_native_strength_verdict as verdict
from bots.neural_national_lab.tools import native_tcp_evaluate as native_evaluator
from bots.neural_national_lab.tools import summarize_v4_native_ablations as ablations
from bots.neural_national_lab.tools import v4_native_strength_pool_output


def test_output_validator_supports_package_import() -> None:
    assert callable(v4_native_strength_pool_output.validate_frozen_output_tree)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _write_native_bot(repo: Path, version: int, marker: str) -> Path:
    root = repo / "bots" / f"national_v{version}"
    root.mkdir(parents=True, exist_ok=True)
    (root / "national_bot.py").write_text(
        f"BOT_VERSION = {version}\nMARKER = {marker!r}\n",
        encoding="utf-8",
    )
    return root


def _rating(r: float, rd: float = 50.0) -> dict[str, object]:
    return {
        "r": r,
        "rd": rd,
        "sigma": 0.06,
        "last_period": "2026-07-12T00:00:00Z",
    }


@pytest.fixture
def strength_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Pool Test")
    _git(repo, "config", "user.email", "pool@example.invalid")
    (repo / "README.md").write_text("pool fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    # v1..v5 and v7 are annotated completions.  v6 is deliberately a bare tag;
    # v7 is subsequently reaped through a durable annotated lifecycle tag.
    for version in range(1, 8):
        _write_native_bot(repo, version, f"tag-v{version}")
        _git(repo, "add", f"bots/national_v{version}/national_bot.py")
        _git(repo, "commit", "-m", f"add v{version}")
        if version == 6:
            _git(repo, "tag", "national-bot-v6")
        else:
            _git(
                repo,
                "tag",
                "-a",
                f"national-bot-v{version}",
                "-m",
                f"complete v{version}",
            )

    _git(
        repo,
        "tag",
        "-a",
        "national-reaped-v7",
        "-m",
        "durably reap v7",
    )
    _git(
        repo,
        "tag",
        "-a",
        "national-reaped-registry-v1",
        "-m",
        "durable registry marker",
    )
    _git(repo, "tag", "-a", "national-high-water-v7", "-m", "high water v7")

    # Simulate an allowed mainline execution-tree migration after v1's original
    # completion tag.  Both original tag and current source digests must survive.
    _write_native_bot(repo, 1, "mainline-migrated-v1")
    (repo / "bots" / "national_v1" / "migration.py").write_text(
        "MIGRATED = True\n", encoding="utf-8"
    )
    _git(repo, "add", "bots/national_v1")
    _git(repo, "commit", "-m", "migrate v1 runtime")

    candidate = repo / "neural_candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("CANDIDATE = True\n", encoding="utf-8")
    (candidate / ".completed").write_text("excluded\n", encoding="utf-8")
    (candidate / "__pycache__").mkdir()
    (candidate / "__pycache__" / "cache.pyc").write_bytes(b"excluded")
    v1 = repo / "bots" / "national_v1"
    (v1 / ".completed").write_text("excluded\n", encoding="utf-8")
    (v1 / "__pycache__").mkdir()
    (v1 / "__pycache__" / "cache.pyc").write_bytes(b"excluded")

    ratings = repo / ".evolution_pok" / "web" / "core" / "results" / "glicko_ratings.json"
    ratings.parent.mkdir(parents=True)
    ratings_payload = {
        "national_v1": _rating(1600.0),
        "national_v2": _rating(1550.0),
        "national_v3": _rating(1500.0),
        "national_v4": _rating(1450.0),
        # Latest active completion is intentionally below the top four.
        "national_v5": _rating(1000.0),
        # Bare and reaped versions would otherwise dominate the ranking.
        "national_v6": _rating(2200.0),
        "national_v7": _rating(2100.0),
    }
    ratings.write_text(
        json.dumps(ratings_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(freeze, "ROOT", repo)
    return {"repo": repo, "candidate": candidate, "ratings": ratings, "tmp": tmp_path}


def _args(paths: dict[str, Path], *, out_name: str = "frozen") -> argparse.Namespace:
    return argparse.Namespace(
        candidate=paths["candidate"],
        ratings=paths["ratings"],
        out_dir=paths["tmp"] / out_name,
        pool_size=4,
        seed_blocks=3,
        seed_base=9_100_000,
        seed_stride=1_000,
        opponent_seed_stride=10_000_000,
        bot_seed_base=1_000_000_000,
        bot_seed_stride=10,
        workers=2,
        bootstrap_samples=2_000,
        bootstrap_seed=17,
    )


def _plan_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _resign(payload: dict[str, object]) -> bytes:
    payload["payload_sha256"] = freeze.plan_payload_sha256(payload)
    return _plan_bytes(payload)


def _native_receipt(seed: int) -> dict[str, object]:
    return {
        "returncode": 0,
        "bot_seed": seed,
        "stdout_tail": "",
        "stderr_tail": "",
        "decision_trace": [],
        "process_failures": 0,
        "json_response_stdout": 0,
    }


def _zero_counters() -> dict[str, int]:
    return {field: 0 for field in ablations.ZERO_COUNTER_FIELDS}


def _report_for_frozen_plan(
    plan: dict[str, object], *, mode: str, net_chips: int
) -> bytes:
    candidate = plan["candidate_artifact"]
    opponents = plan["opponent_artifacts"]
    assert isinstance(candidate, dict) and isinstance(opponents, list)
    rows: list[dict[str, object]] = []
    actual_seeds: list[int] = []
    for opponent_index, opponent in enumerate(opponents):
        assert isinstance(opponent, dict)
        for match_index, seed in enumerate(plan["seeds"]):
            assert isinstance(seed, int)
            deck_seed = seed + opponent_index * int(plan["opponent_seed_stride"])
            bot_seed = (
                int(plan["bot_seed_base"])
                + match_index * int(plan["bot_seed_stride"])
                + opponent_index * int(plan["bot_opponent_seed_stride"])
            )
            actual_seeds.append(deck_seed)
            legs: list[dict[str, object]] = []
            for leg_index, leg_name in enumerate(ablations.LEG_NAMES):
                forward = leg_index == 0
                legs.append(
                    {
                        "candidate": candidate["label"],
                        "opponent": opponent["label"],
                        "opponent_path": opponent["snapshot_path"],
                        "match_idx": match_index,
                        "leg": leg_name,
                        "deck_seed_base": deck_seed,
                        "bot_seed_base": bot_seed,
                        "hands_played": 70,
                        "net_chips": net_chips,
                        "hand_net_chips": [net_chips, *([0] * 69)],
                        "passed_compliance": True,
                        "wrapper_used": False,
                        "issues": [],
                        **_zero_counters(),
                        "candidate_native": _native_receipt(
                            bot_seed if forward else bot_seed + 1
                        ),
                        "opponent_native": _native_receipt(
                            bot_seed + 1 if forward else bot_seed
                        ),
                    }
                )
            rows.append(
                {
                    "candidate": candidate["label"],
                    "opponent": opponent["label"],
                    "opponent_path": opponent["snapshot_path"],
                    "match_idx": match_index,
                    "leg": "paired",
                    "deck_seed_base": deck_seed,
                    "bot_seed_base": bot_seed,
                    "hands_played": 140,
                    "net_chips": 2 * net_chips,
                    "hand_net_chips": [2 * net_chips, *([0] * 69)],
                    "passed_compliance": True,
                    "wrapper_used": False,
                    "issues": [],
                    **_zero_counters(),
                    "legs": legs,
                }
            )
    full = mode == "full"
    report = {
        "format": "native_tcp_evaluation_v2",
        "execution_mode": "native_tcp",
        "candidate_ablation": {
            "schema": ablations.ABLATION_SCHEMA,
            "mode": mode,
            "candidate_env_overrides": dict(ablations.MODE_ENV_OVERRIDES[mode]),
            "opponent_env_overrides": dict(ablations.OPPONENT_ENV_OVERRIDES),
            "diagnostic_only": not full,
            "eligible_as_strength_evidence": full,
            "protected_data_read": False,
            "policy_roles_opened": [],
            "deployment_policy_value": False,
            "strength_evidence": False,
        },
        "runtime_contract": plan["runtime_contract"],
        "candidate_path": candidate["snapshot_path"],
        "opponent_paths": [row["snapshot_path"] for row in opponents],
        "hands_per_match": 70,
        "seeds": list(plan["seeds"]),
        "deck_seed_scheme": plan["deck_seed_scheme"],
        "opponent_seed_stride": plan["opponent_seed_stride"],
        "actual_deck_seed_bases": sorted(actual_seeds),
        "execution_artifacts": {
            "candidate": {
                "path": candidate["snapshot_path"],
                "sha256_before": candidate["snapshot_directory_sha256"],
                "sha256_after": candidate["snapshot_directory_sha256"],
                "stable": True,
            },
            "opponents": [
                {
                    "path": row["snapshot_path"],
                    "sha256_before": row["snapshot_directory_sha256"],
                    "sha256_after": row["snapshot_directory_sha256"],
                    "stable": True,
                }
                for row in opponents
            ],
        },
        "workers": plan["workers"],
        "paired": True,
        "requires_native_opponents": True,
        "legacy_debug_wrapper_enabled": False,
        "wrapper_used": False,
        "bot_seed_base": plan["bot_seed_base"],
        "bot_seed_stride": plan["bot_seed_stride"],
        "outcome_bootstrap_samples": plan["bootstrap_samples"],
        "outcome_bootstrap_seed": plan["bootstrap_seed"],
        "trace_decisions": False,
        "force": {"hand": None, "decision": None, "action": None},
        "strength_evidence": {
            "schema": ablations.EVALUATOR_STRENGTH_SCHEMA,
            "criterion": "net_chips_after_70_hands_gt_zero",
            "requested": full,
            "execution_contract_passed": full,
            "outcome_gate_passed": full,
            "passed": full,
            "request_errors": [],
            "result_errors": [],
            "statistical_errors": [],
        },
        "rows": rows,
    }
    return _plan_bytes(report)


def test_freeze_dynamic_pool_latest_binding_and_public_validation(
    strength_repo: dict[str, Path],
) -> None:
    args = _args(strength_repo)
    payload = freeze.freeze_strength_pool(args)
    output = Path(args.out_dir)
    plan_path = output / freeze.PLAN_FILENAME
    raw_plan = plan_path.read_bytes()

    assert payload["schema"] == freeze.SCHEMA
    assert payload["payload_sha256"] == freeze.plan_payload_sha256(payload)
    assert freeze.validate_v4_native_strength_pool_plan_bytes(raw_plan) == payload
    assert payload["selection"]["selected"] == [
        "national_v1",
        "national_v2",
        "national_v3",
        "national_v5",
    ]
    assert payload["selection"]["latest_forced"] is True
    assert payload["selection"]["replaced_for_latest"] == "national_v4"
    assert payload["selection"]["latest_completed_active_tag"] == "national-bot-v5"
    assert [row["label"] for row in payload["selection"]["ranking"]] == [
        "national_v1",
        "national_v2",
        "national_v3",
        "national_v4",
        "national_v5",
    ]
    assert 6 in payload["lifecycle"]["completion_versions"]
    assert 6 not in payload["lifecycle"]["annotated_completion_versions"]
    assert 7 in payload["lifecycle"]["reaped_versions"]
    assert 7 not in payload["lifecycle"]["active_annotated_completion_versions"]

    v1 = payload["opponent_artifacts"][0]
    assert v1["completion_tag"] == "national-bot-v1"
    assert len(v1["tag_object"]) == len(v1["tag_commit"]) == 40
    assert v1["execution_matches_completion_tag"] is False
    assert v1["execution_directory_sha256"] != v1["tag_directory_sha256"]
    assert v1["execution_commit"] == payload["repository"]["origin_main_commit"]

    ratings_raw = strength_repo["ratings"].read_bytes()
    receipt = payload["ratings_snapshot"]
    assert base64.b64decode(receipt["raw_base64"]) == ratings_raw
    assert receipt["sha256"] == hashlib.sha256(ratings_raw).hexdigest()
    assert receipt["fstat"]["st_size"] == len(ratings_raw)

    snapshots = [payload["candidate_artifact"], *payload["opponent_artifacts"]]
    for artifact in snapshots:
        snapshot = Path(artifact["snapshot_path"])
        assert (snapshot / "national_bot.py").is_file()
        if artifact is payload["candidate_artifact"]:
            assert (snapshot / ".completed").is_file()
            assert (snapshot / "__pycache__" / "cache.pyc").is_file()
        assert stat.S_IMODE(snapshot.stat().st_mode) & 0o222 == 0
        assert freeze._tree_digest(snapshot) == artifact["snapshot_directory_sha256"]
    assert stat.S_IMODE(output.stat().st_mode) & 0o222 == 0
    assert stat.S_IMODE(plan_path.stat().st_mode) & 0o222 == 0
    for field, expected in freeze.FALSE_AUTHORITY.items():
        assert payload[field] == expected
    assert (
        "bots/neural_national_lab/tools/evaluate_v4_native_strength_verdict.py"
        in payload["code_artifacts"]
    )
    assert "sever/engine/game.py" in payload["code_artifacts"]

    # A later completion is allowed after this point-in-time plan; every
    # authority ref that existed at freeze time must remain unchanged.
    _write_native_bot(strength_repo["repo"], 8, "later-v8")
    _git(strength_repo["repo"], "add", "bots/national_v8/national_bot.py")
    _git(strength_repo["repo"], "commit", "-m", "later v8")
    _git(
        strength_repo["repo"],
        "tag",
        "-a",
        "national-bot-v8",
        "-m",
        "complete later v8",
    )
    assert freeze.validate_v4_native_strength_pool_plan_bytes(raw_plan) == payload

    freeze._restore_owner_access(output)


def test_real_freeze_reports_and_verdict_replay_end_to_end(
    strength_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    verdict_freeze = sys.modules[
        verdict.validate_v4_native_strength_pool_plan_bytes.__module__
    ]
    monkeypatch.setattr(verdict_freeze, "ROOT", strength_repo["repo"])
    args = _args(strength_repo, out_name="verdict-integration")
    plan = freeze.freeze_strength_pool(args)
    plan_raw = (Path(args.out_dir) / freeze.PLAN_FILENAME).read_bytes()
    for artifact in [plan["candidate_artifact"], *plan["opponent_artifacts"]]:
        assert native_evaluator._directory_digest(
            Path(artifact["snapshot_path"])
        ) == artifact["snapshot_directory_sha256"]
    candidate_path = Path(plan["candidate_artifact"]["snapshot_path"])

    async def fake_pair(
        bot_a: Path, bot_b: Path, hands: int, **kwargs: object
    ) -> dict[str, object]:
        assert kwargs["sanitize_parent_environment"] is True
        bot_a = Path(bot_a)
        bot_b = Path(bot_b)
        candidate_is_a = bot_a == candidate_path
        candidate_env = kwargs[
            "bot_a_env_overrides" if candidate_is_a else "bot_b_env_overrides"
        ]
        assert isinstance(candidate_env, dict)
        candidate_net = 0 if candidate_env.get("POK_V4_DISABLE") == "1" else 1_000
        net_a = candidate_net if candidate_is_a else -candidate_net
        bot_seed = int(kwargs["bot_seed_base"])

        def player(seed: int) -> dict[str, object]:
            return {
                "illegal_actions": 0,
                "timeouts": 0,
                "adapter": {"actions_sent": 0},
                "native": {
                    "returncode": 0,
                    "bot_seed": seed,
                    "stdout_tail": "",
                    "stderr_tail": "",
                    "decision_trace": [],
                    "process_failures": 0,
                    "json_response_stdout": 0,
                },
                "runtime_telemetry": {},
            }

        label_a, label_b = bot_a.name, bot_b.name
        return {
            "bot_a": label_a,
            "bot_b": label_b,
            "bot_seed_base": bot_seed,
            "hands_played": hands,
            "net_chips_a": net_a,
            "net_chips_b": -net_a,
            "settlements": [
                {"earnings": [net_a, -net_a] if index == 0 else [0, 0]}
                for index in range(hands)
            ],
            "passed_compliance": True,
            "wrapper_used": False,
            "issues": [],
            "per_player": {
                label_a: player(bot_seed),
                label_b: player(bot_seed + 1),
            },
        }

    monkeypatch.setattr(native_evaluator, "run_native_tcp_pair", fake_pair)

    async def evaluator_report(mode: str, *, strength: bool) -> bytes:
        evaluator_args = SimpleNamespace(
            candidate=str(candidate_path),
            opponent=[row["snapshot_path"] for row in plan["opponent_artifacts"]],
            hands=70,
            matches=len(plan["seeds"]),
            seeds=",".join(str(seed) for seed in plan["seeds"]),
            seed_base=None,
            seed_stride=None,
            opponent_seed_stride=plan["opponent_seed_stride"],
            bot_seed_base=plan["bot_seed_base"],
            bot_seed_stride=plan["bot_seed_stride"],
            workers=plan["workers"],
            timeout_sec=plan["runtime_contract"]["match_timeout_sec"],
            paired=True,
            candidate_ablation=mode,
            trace_decisions=False,
            force_hand=None,
            force_decision=None,
            force_action=None,
            allow_generated_opponent_entry=False,
            print_rows=False,
            strength_evidence=strength,
            outcome_bootstrap_samples=plan["bootstrap_samples"],
            outcome_bootstrap_seed=plan["bootstrap_seed"],
        )
        payload = await native_evaluator._run(evaluator_args)
        base_seeds = native_evaluator._seeds(evaluator_args)
        request_errors = (
            native_evaluator._strength_request_errors(
                evaluator_args,
                base_seeds,
                opponent_count=len(evaluator_args.opponent),
            )
            if strength else []
        )
        result_errors = (
            native_evaluator._strength_result_errors(
                payload,
                expected_rows=len(evaluator_args.opponent) * len(base_seeds),
                hands_per_leg=70,
            )
            if strength else []
        )
        payload["strength_evidence"] = native_evaluator._strength_evidence_payload(
            requested=strength,
            request_errors=request_errors,
            result_errors=result_errors,
            payload=payload,
        )
        return _plan_bytes(payload)

    async def collect_reports() -> tuple[bytes, bytes]:
        candidate_report, baseline_report = await asyncio.gather(
            evaluator_report("full", strength=True),
            evaluator_report("neural_off", strength=False),
        )
        return candidate_report, baseline_report

    candidate_raw, baseline_raw = asyncio.run(collect_reports())

    artifact = verdict.evaluate_v4_native_strength_verdict(
        plan_raw,
        candidate_raw,
        baseline_raw,
        bootstrap_samples=2_000,
        bootstrap_seed=17,
    )

    assert artifact["development_classic_pool_verdict_passed"] is True
    assert artifact["strength_evidence"] is False
    assert verdict.validate_v4_native_strength_verdict(
        artifact,
        pool_plan_raw=plan_raw,
        candidate_report_raw=candidate_raw,
        baseline_report_raw=baseline_raw,
    ) == artifact
    freeze._restore_owner_access(Path(args.out_dir))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"national_v1":{"r":1500,"rd":50,"sigma":0.06,"last_period":"x"},'
        b'"national_v1":{"r":1400,"rd":50,"sigma":0.06,"last_period":"x"}}',
        b'{"national_v1":{"r":1500,"r":1400,"rd":50,"sigma":0.06,"last_period":"x"}}',
        b'{"national_v1":{"r":NaN,"rd":50,"sigma":0.06,"last_period":"x"}}',
        b'{"v1":{"r":1500,"rd":50,"sigma":0.06,"last_period":"x"}}',
        b'{"national_v1":{"r":1500,"rd":50,"sigma":0.06,"last_period":"x","x":1}}',
        b'{"national_v1":{"r":1500,"rd":50,"last_period":"x"}}',
        b'{"national_v1":{"r":1500,"rd":0,"sigma":0.06,"last_period":"x"}}',
        b'{"national_v1":{"r":1500,"rd":50,"sigma":0.06,"last_period":1}}',
    ],
)
def test_ratings_snapshot_rejects_dirty_json(
    tmp_path: Path, raw: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    ratings = tmp_path / "ratings.json"
    ratings.write_bytes(raw)
    monkeypatch.setattr(freeze, "_canonical_ratings_path", lambda: ratings)
    with pytest.raises(freeze.FreezeError):
        freeze.read_ratings_snapshot(ratings)


def test_ratings_snapshot_uses_one_file_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ratings = tmp_path / "ratings.json"
    ratings.write_text(
        '{"national_v1":{"r":1500,"rd":50,"sigma":0.06,"last_period":"x"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(freeze, "_canonical_ratings_path", lambda: ratings)
    real_open = os.open
    opened = 0

    def tracked_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal opened
        if Path(os.fspath(path)).resolve() == ratings.resolve():
            opened += 1
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(freeze.os, "open", tracked_open)
    snapshot = freeze.read_ratings_snapshot(ratings)
    assert snapshot["bytes"] == len(ratings.read_bytes())
    assert opened == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"seed_stride": 69},
        {"opponent_seed_stride": 200},
        {"bot_seed_stride": 1},
    ],
)
def test_seed_contract_rejects_deck_and_bot_collisions(
    overrides: dict[str, int],
) -> None:
    values = {
        "opponents": 2,
        "seed_blocks": 3,
        "seed_base": 0,
        "seed_stride": 100,
        "opponent_seed_stride": 10_000,
        "bot_seed_base": 1_000_000,
        "bot_seed_stride": 10,
    }
    values.update(overrides)
    with pytest.raises(freeze.FreezeError, match="overlap"):
        freeze._seed_contract(**values)


def test_candidate_basename_cannot_mask_latest_completed_bot(
    strength_repo: dict[str, Path],
) -> None:
    disguised = strength_repo["tmp"] / "national_v5"
    disguised.mkdir()
    (disguised / "national_bot.py").write_text(
        "DISGUISED_CANDIDATE = True\n", encoding="utf-8"
    )
    args = _args(strength_repo, out_name="basename-mask")
    args.candidate = disguised
    payload = freeze.freeze_strength_pool(args)
    assert payload["candidate_artifact"]["repository_completed_version"] is None
    assert "national_v5" in payload["selection"]["selected"]
    assert payload["selection"]["latest_completed_active_version"] == 5
    freeze._restore_owner_access(Path(args.out_dir))


def test_candidate_path_symlinks_are_rejected_before_identity_selection(
    strength_repo: dict[str, Path],
) -> None:
    target = strength_repo["tmp"] / "real-candidate"
    target.mkdir()
    (target / "national_bot.py").write_text("CANDIDATE = True\n", encoding="utf-8")
    linked = strength_repo["tmp"] / "national_v5"
    linked.symlink_to(target, target_is_directory=True)
    args = _args(strength_repo, out_name="symlink-candidate")
    args.candidate = linked

    with pytest.raises(freeze.FreezeError, match="symbolic links"):
        freeze.freeze_strength_pool(args)


def test_repository_candidate_must_match_its_frozen_mainline_tree(
    strength_repo: dict[str, Path],
) -> None:
    candidate = strength_repo["repo"] / "bots" / "national_v5"
    (candidate / "national_bot.py").write_text("DIRTY = True\n", encoding="utf-8")
    args = _args(strength_repo, out_name="dirty-repository-candidate")
    args.candidate = candidate

    with pytest.raises(freeze.FreezeError, match="mainline completed tree"):
        freeze.freeze_strength_pool(args)


def test_completed_opponents_are_materialized_from_origin_main_not_dirty_worktree(
    strength_repo: dict[str, Path],
) -> None:
    dirty_entry = strength_repo["repo"] / "bots" / "national_v1" / "national_bot.py"
    dirty_entry.write_text("ALWAYS_FOLD = True\n", encoding="utf-8")
    args = _args(strength_repo, out_name="committed-opponents")
    payload = freeze.freeze_strength_pool(args)
    v1 = next(
        artifact
        for artifact in payload["opponent_artifacts"]
        if artifact["label"] == "national_v1"
    )
    frozen = Path(v1["snapshot_path"]) / "national_bot.py"
    assert b"ALWAYS_FOLD" not in frozen.read_bytes()
    assert v1["execution_commit"] == _git(
        strength_repo["repo"], "rev-parse", "origin/main"
    )
    freeze._restore_owner_access(Path(args.out_dir))


def test_tree_digest_binds_empty_dirs_metadata_files_and_effective_mode(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    entry = tree / "national_bot.py"
    entry.write_text("X = 1\n", encoding="utf-8")
    initial = freeze._tree_digest(tree)
    (tree / "empty").mkdir()
    with_empty = freeze._tree_digest(tree)
    assert with_empty != initial
    (tree / ".completed").write_text("yes\n", encoding="utf-8")
    with_marker = freeze._tree_digest(tree)
    assert with_marker != with_empty
    (tree / "cache.pyc").write_bytes(b"cache")
    with_cache = freeze._tree_digest(tree)
    assert with_cache != with_marker
    os.chmod(entry, 0o755)
    assert freeze._tree_digest(tree) != with_cache


def test_code_artifact_closure_rejects_symlink_aliases(tmp_path: Path) -> None:
    source = tmp_path / "source"
    tools = source / "tools"
    tools.mkdir(parents=True)
    (tools / "real.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tools / "alias.py").symlink_to(tools / "real.py")

    with pytest.raises(freeze.FreezeError, match="symlinks are forbidden"):
        freeze._code_artifact_hashes(source, (tools,))


def test_noncanonical_ratings_path_is_rejected(
    strength_repo: dict[str, Path],
) -> None:
    arbitrary = strength_repo["tmp"] / "chosen-ratings.json"
    arbitrary.write_bytes(strength_repo["ratings"].read_bytes())
    args = _args(strength_repo, out_name="wrong-ratings")
    args.ratings = arbitrary
    with pytest.raises(freeze.FreezeError, match="canonical evolution snapshot"):
        freeze.freeze_strength_pool(args)


def test_atomic_failure_removes_read_only_temporary_tree(
    strength_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(strength_repo, out_name="atomic-failure")
    def fail_after_read_only_copy(
        commit: str, label: str, destination: Path
    ) -> tuple[str, str]:
        raise freeze.FreezeError("injected snapshot failure")

    monkeypatch.setattr(freeze, "_copy_git_tree_snapshot", fail_after_read_only_copy)
    with pytest.raises(freeze.FreezeError, match="injected"):
        freeze.freeze_strength_pool(args)
    assert not Path(args.out_dir).exists()
    assert list(Path(args.out_dir).parent.glob(f".{Path(args.out_dir).name}.freeze-*")) == []


def test_identity_interrupt_immediately_after_mkdtemp_leaves_no_debris(
    strength_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(strength_repo, out_name="identity-interrupt")

    def interrupt_identity(path: Path) -> tuple[int, int]:
        metadata = path.lstat()
        assert metadata.st_ino > 0
        raise KeyboardInterrupt("identity interrupted")

    monkeypatch.setattr(freeze, "_directory_identity", interrupt_identity)
    with pytest.raises(KeyboardInterrupt, match="identity interrupted"):
        freeze.freeze_strength_pool(args)
    assert not Path(args.out_dir).exists()
    assert list(Path(args.out_dir).parent.glob(f".{Path(args.out_dir).name}.freeze-*")) == []


def test_interrupt_after_staging_mkdir_leaves_no_debris(
    strength_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(strength_repo, out_name="mkdir-interrupt")
    real_mkdir = os.mkdir

    def interrupt_after_mkdir(path: object, mode: int = 0o777, *args: object, **kwargs: object) -> None:
        real_mkdir(path, mode, *args, **kwargs)
        raise KeyboardInterrupt("mkdir interrupted")

    monkeypatch.setattr(freeze.os, "mkdir", interrupt_after_mkdir)
    with pytest.raises(KeyboardInterrupt, match="mkdir interrupted"):
        freeze.freeze_strength_pool(args)
    assert not Path(args.out_dir).exists()
    assert list(Path(args.out_dir).parent.glob(f".{Path(args.out_dir).name}.freeze-*")) == []


def test_publish_interrupt_after_atomic_rename_removes_only_staged_inode(
    strength_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(strength_repo, out_name="publish-interrupt")
    real_publish = freeze._publish_tree_noreplace

    def interrupt_after_rename(source: Path, destination: Path) -> None:
        real_publish(source, destination)
        raise KeyboardInterrupt("delivered after rename")

    monkeypatch.setattr(freeze, "_publish_tree_noreplace", interrupt_after_rename)
    with pytest.raises(KeyboardInterrupt, match="after rename"):
        freeze.freeze_strength_pool(args)
    assert not Path(args.out_dir).exists()
    assert list(Path(args.out_dir).parent.glob(f".{Path(args.out_dir).name}.freeze-*")) == []


def test_publish_is_noreplace_under_concurrent_destination_creation(
    strength_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(strength_repo, out_name="publish-race")
    real_publish = freeze._publish_tree_noreplace

    def create_destination_then_publish(source: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "OWNER").write_text("other\n", encoding="utf-8")
        real_publish(source, destination)

    monkeypatch.setattr(
        freeze, "_publish_tree_noreplace", create_destination_then_publish
    )
    with pytest.raises(freeze.FreezeError, match="already exists"):
        freeze.freeze_strength_pool(args)
    assert (Path(args.out_dir) / "OWNER").read_text(encoding="utf-8") == "other\n"
    assert list(Path(args.out_dir).parent.glob(f".{Path(args.out_dir).name}.freeze-*")) == []


def test_public_validator_rejects_unbound_output_root_entries(
    strength_repo: dict[str, Path],
) -> None:
    args = _args(strength_repo, out_name="root-layout")
    freeze.freeze_strength_pool(args)
    output = Path(args.out_dir)
    raw = (output / freeze.PLAN_FILENAME).read_bytes()
    freeze._restore_owner_access(output)
    (output / "UNBOUND_EXTRA").write_text("unexpected\n", encoding="utf-8")
    freeze._make_read_only(output)

    with pytest.raises(freeze.FreezeError, match="root layout"):
        freeze.validate_v4_native_strength_pool_plan_bytes(raw)
    freeze._restore_owner_access(output)


def test_public_validator_rejects_output_hardlinks_and_xattrs(
    strength_repo: dict[str, Path],
) -> None:
    if not hasattr(os, "setxattr"):
        pytest.skip("extended attributes unavailable")
    args = _args(strength_repo, out_name="output-metadata")
    freeze.freeze_strength_pool(args)
    output = Path(args.out_dir)
    raw = (output / freeze.PLAN_FILENAME).read_bytes()
    freeze._restore_owner_access(output)
    external_link = output.parent / "plan-hardlink"
    os.link(output / freeze.PLAN_FILENAME, external_link)
    os.setxattr(output / "snapshots", b"user.audit", b"1")
    freeze._make_read_only(output)

    with pytest.raises(freeze.FreezeError, match="hard link|extended attributes"):
        freeze.validate_v4_native_strength_pool_plan_bytes(raw)
    external_link.unlink()
    freeze._restore_owner_access(output)
    os.removexattr(output / "snapshots", b"user.audit")


def test_full_tree_validation_finishes_before_publication(
    strength_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(strength_repo, out_name="prepublish-validation")
    real_validate = freeze._validate_frozen_output_tree
    observations: list[bool] = []

    def observe(root: Path, **kwargs: object) -> None:
        observations.append(Path(args.out_dir).exists())
        real_validate(root, **kwargs)

    monkeypatch.setattr(freeze, "_validate_frozen_output_tree", observe)
    freeze.freeze_strength_pool(args)
    assert observations == [False]
    freeze._restore_owner_access(Path(args.out_dir))


def test_live_lifecycle_is_rechecked_after_staging_validation(
    strength_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(strength_repo, out_name="late-lifecycle-drift")
    real_validate = freeze._validate_frozen_output_tree

    def drift_after_validation(root: Path, **kwargs: object) -> None:
        real_validate(root, **kwargs)
        _git(
            strength_repo["repo"],
            "tag",
            "-a",
            "national-high-water-v8",
            "-m",
            "late high water",
        )

    monkeypatch.setattr(freeze, "_validate_frozen_output_tree", drift_after_validation)
    with pytest.raises(freeze.FreezeError, match="selection drifted"):
        freeze.freeze_strength_pool(args)
    assert not Path(args.out_dir).exists()


def test_public_validator_recomputes_selection_and_checks_snapshot_and_tag(
    strength_repo: dict[str, Path],
) -> None:
    args = _args(strength_repo, out_name="validator")
    payload = freeze.freeze_strength_pool(args)
    raw = (Path(args.out_dir) / freeze.PLAN_FILENAME).read_bytes()

    tampered = copy.deepcopy(payload)
    tampered["selection"]["ranking"][0]["r"] = 1.0
    with pytest.raises(freeze.FreezeError, match="recomputed"):
        freeze.validate_v4_native_strength_pool_plan_bytes(
            _resign(tampered), require_snapshots=False
        )

    resigned_seed = copy.deepcopy(payload)
    resigned_seed["bootstrap_seed"] += 1
    with pytest.raises(freeze.FreezeError, match="differs from the sealed plan"):
        freeze.validate_v4_native_strength_pool_plan_bytes(
            _resign(resigned_seed), require_snapshots=True
        )

    candidate_snapshot = Path(payload["candidate_artifact"]["snapshot_path"])
    candidate_entry = candidate_snapshot / "national_bot.py"
    os.chmod(candidate_snapshot, candidate_snapshot.stat().st_mode | stat.S_IWUSR)
    os.chmod(candidate_entry, candidate_entry.stat().st_mode | stat.S_IWUSR)
    candidate_entry.write_text("TAMPERED = True\n", encoding="utf-8")
    assert freeze.validate_v4_native_strength_pool_plan_bytes(
        raw, require_snapshots=False
    ) == payload
    with pytest.raises(freeze.FreezeError, match="snapshot digest"):
        freeze.validate_v4_native_strength_pool_plan_bytes(raw, require_snapshots=True)

    _git(strength_repo["repo"], "tag", "-d", "national-bot-v1")
    with pytest.raises(freeze.FreezeError, match="disappeared or moved"):
        freeze.validate_v4_native_strength_pool_plan_bytes(raw, require_snapshots=False)
    freeze._restore_owner_access(Path(args.out_dir))


def test_freeze_sandwich_rejects_ratings_drift_without_temp_debris(
    strength_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(strength_repo, out_name="ratings-drift")
    real_copy = freeze._copy_git_tree_snapshot
    calls = 0

    def drift_after_last_snapshot(
        commit: str, label: str, destination: Path
    ) -> tuple[str, str]:
        nonlocal calls
        result = real_copy(commit, label, destination)
        calls += 1
        if calls == 4:
            raw = strength_repo["ratings"].read_text(encoding="utf-8")
            strength_repo["ratings"].write_text(raw + "\n", encoding="utf-8")
        return result

    monkeypatch.setattr(freeze, "_copy_git_tree_snapshot", drift_after_last_snapshot)
    with pytest.raises(freeze.FreezeError, match="ratings snapshot drifted"):
        freeze.freeze_strength_pool(args)
    assert not Path(args.out_dir).exists()
    assert list(Path(args.out_dir).parent.glob(f".{Path(args.out_dir).name}.freeze-*")) == []


def test_cli_has_only_unprotected_inputs_and_formal_minima(
    strength_repo: dict[str, Path],
) -> None:
    parser = freeze.build_parser()
    options = set(parser._option_string_actions)
    for forbidden in ("held-out", "policy", "role", "calibration"):
        assert not any(forbidden in option for option in options)
    parsed = parser.parse_args(
        [
            "--candidate",
            str(strength_repo["candidate"]),
            "--ratings",
            str(strength_repo["ratings"]),
            "--out-dir",
            str(strength_repo["tmp"] / "unused"),
        ]
    )
    assert parsed.pool_size == 8
    assert parsed.seed_blocks == 12
    assert 1 <= parsed.workers <= 4
    assert parsed.bootstrap_samples == 20_000
    assert parsed.bootstrap_seed == 20_260_712

    parsed.pool_size = 3
    with pytest.raises(freeze.FreezeError, match="pool_size"):
        freeze.freeze_strength_pool(parsed)
