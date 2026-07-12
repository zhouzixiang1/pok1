from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOL = (
    ROOT
    / "bots"
    / "neural_national_lab"
    / "tools"
    / "longrun_collect_oppmodel.py"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location("longrun_collect_oppmodel", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ratings_bytes() -> bytes:
    return (
        b'{\n  "national_v1": {"r": 1600, "rd": 40},'
        b'\n  "national_v2": {"rating": 1550, "rd": 50},'
        b'\n  "national_v3": {"r": 1500, "rd": 60}\n}\n'
    )


def _minimal_bot(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "national_bot.py").write_text("pass\n", encoding="utf-8")
    return path


def _minimal_collection_args(
    *, candidate: Path, out_dir: Path, ratings: Path, passes: int
) -> list[str]:
    return [
        "--candidate", str(candidate),
        "--out-dir", str(out_dir),
        "--passes", str(passes),
        "--workers", "1",
        "--probe-workers", "1",
        "--hands", "1",
        "--timeout-sec", "1",
        "--ratings", str(ratings),
        "--strongest", "3",
        "--val-opponent", "national_v2",
        "--held-out-opponent", "national_v3",
        "--opponents-per-pass", "3",
        "--max-decisions", "1",
        "--max-alternatives", "1",
    ]


def _install_minimal_pool(tool, monkeypatch, tmp_path: Path):
    opponents = {
        name: _minimal_bot(tmp_path / name)
        for name in ("national_v1", "national_v2", "national_v3")
    }

    def fake_build_pool(_ratings_path, **kwargs):
        assert set(kwargs["frozen_ratings"]) == set(opponents)
        return [
            ("national_v1", str(opponents["national_v1"]), "train"),
            ("national_v2", str(opponents["national_v2"]), "val"),
            ("national_v3", str(opponents["national_v3"]), "held_out"),
        ]

    def fake_freeze(name, source_path, _out_dir):
        digest = tool._directory_digest(source_path)
        return {
            "tag_commit": hashlib.sha1(name.encode("ascii")).hexdigest(),
            "tag_directory_sha256": digest,
            "execution_matches_generation_tag": True,
            "source_path": str(source_path),
            "source_checkout_commit": "b" * 40,
            "snapshot_path": str(source_path),
            "execution_directory_sha256": digest,
        }

    monkeypatch.setattr(tool, "build_pool", fake_build_pool)
    monkeypatch.setattr(tool, "_freeze_opponent", fake_freeze)
    return opponents


def _run_probe_one(tool, tmp_path: Path) -> tuple[Path, Path]:
    candidate = _minimal_bot(tmp_path / "candidate")
    opponent = _minimal_bot(tmp_path / "national_v1")
    out_dir = tmp_path / "data"
    out_dir.mkdir()
    tool.probe_one(
        str(candidate), str(opponent), "train", "national_v1",
        1, 100, 200, str(out_dir), 1, 1, 1, 1,
        str(tmp_path / "ratings.json"), 2, "uniform",
    )
    return out_dir, candidate


def _valid_pass_plan(tool, tmp_path: Path) -> tuple[Path, dict]:
    ratings_path = tmp_path / "plan_ratings.json"
    ratings_path.write_bytes(_ratings_bytes())
    opponent = _minimal_bot(tmp_path / "national_v1")
    digest = tool._directory_digest(opponent)
    plan = {
        "schema_version": tool.PASS_PLAN_SCHEMA_VERSION,
        "pass": 1,
        "seed_scheme": "disjoint_match_blocks_v1",
        "ratings_snapshot": tool._capture_ratings_snapshot(ratings_path),
        "tasks": [{
            "name": "national_v1",
            "opponent_path": str(opponent),
            "split": "train",
            "hands": 1,
            "deck_seed_base": tool._deck_seed_for_task(
                root=tool.DEFAULT_DECK_SEED_BASE,
                pass_index=0,
                task_index=0,
                hands=1,
                guard=tool.DEFAULT_DECK_SEED_GUARD,
            ),
            "deck_seed_last": tool._deck_seed_for_task(
                root=tool.DEFAULT_DECK_SEED_BASE,
                pass_index=0,
                task_index=0,
                hands=1,
                guard=tool.DEFAULT_DECK_SEED_GUARD,
            ),
            "bot_seed_base": tool._bot_seed_for_task(
                root=tool.DEFAULT_BOT_SEED_BASE,
                pass_index=0,
                task_index=0,
            ),
            "tag_commit": "a" * 40,
            "tag_directory_sha256": digest,
            "execution_matches_generation_tag": True,
            "source_path": str(opponent),
            "source_checkout_commit": "b" * 40,
            "execution_directory_sha256": digest,
        }],
    }
    return ratings_path, plan


def test_deck_seed_blocks_do_not_overlap_across_tasks_or_passes() -> None:
    tool = _load_tool()
    hands = 70
    guard = 10
    blocks = []
    for pass_index in range(3):
        for task_index in range(6):
            start = tool._deck_seed_for_task(
                root=5_000_000,
                pass_index=pass_index,
                task_index=task_index,
                hands=hands,
                guard=guard,
            )
            blocks.append(set(range(start, start + hands)))

    assert all(
        left.isdisjoint(right)
        for left_index, left in enumerate(blocks)
        for right in blocks[left_index + 1:]
    )


def test_probe_nonzero_exit_is_not_published(tmp_path: Path, monkeypatch) -> None:
    tool = _load_tool()
    monkeypatch.setattr(
        tool.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=9, stdout="", stderr="fatal probe marker"
        ),
    )

    with pytest.raises(RuntimeError, match="rc=9.*fatal probe marker"):
        _run_probe_one(tool, tmp_path)

    assert not list((tmp_path / "data").glob("cf_*.jsonl"))


def test_probe_timeout_is_not_published(tmp_path: Path, monkeypatch) -> None:
    tool = _load_tool()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired([], 1, stderr="timeout probe marker")

    monkeypatch.setattr(tool.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out.*timeout probe marker"):
        _run_probe_one(tool, tmp_path)
    assert not list((tmp_path / "data").glob("cf_*.jsonl"))


def test_probe_missing_outputs_are_not_published(tmp_path: Path, monkeypatch) -> None:
    tool = _load_tool()
    monkeypatch.setattr(
        tool.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="omitted outputs"):
        _run_probe_one(tool, tmp_path)
    assert not list((tmp_path / "data").glob("cf_*.jsonl"))


def test_probe_noncompliant_summary_is_not_published(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_tool()
    monkeypatch.setenv("PYTHONPATH", "/fixture/existing-pythonpath")

    def noncompliant(cmd, **_kwargs):
        assert _kwargs["env"]["PYTHONPATH"].split(os.pathsep)[:2] == [
            str(ROOT), "/fixture/existing-pythonpath",
        ]
        def argument(flag: str) -> str:
            return cmd[cmd.index(flag) + 1]

        summary = {
            "execution_mode": "native_tcp_counterfactual",
            "candidate_path": str(Path(argument("--candidate")).resolve()),
            "opponent_path": str(Path(argument("--opponent")).resolve()),
            "hands": int(argument("--hands")),
            "deck_seed_base": int(argument("--seed-base")),
            "bot_seed_base": int(argument("--bot-seed-base")),
            "baseline_passed_compliance": False,
        }
        Path(argument("--output")).write_text(json.dumps(summary), encoding="utf-8")
        Path(argument("--jsonl-output")).write_text("\n", encoding="utf-8")
        Path(argument("--behavior-jsonl-output")).write_text("\n", encoding="utf-8")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tool.subprocess, "run", noncompliant)
    with pytest.raises(RuntimeError, match="summary contract failed"):
        _run_probe_one(tool, tmp_path)
    assert not list((tmp_path / "data").glob("cf_*.jsonl"))


def test_probe_invalid_jsonl_is_not_published(tmp_path: Path, monkeypatch) -> None:
    tool = _load_tool()

    def invalid_jsonl(cmd, **_kwargs):
        def argument(flag: str) -> str:
            return cmd[cmd.index(flag) + 1]

        summary = {
            "execution_mode": "native_tcp_counterfactual",
            "candidate_path": str(Path(argument("--candidate")).resolve()),
            "opponent_path": str(Path(argument("--opponent")).resolve()),
            "hands": int(argument("--hands")),
            "deck_seed_base": int(argument("--seed-base")),
            "bot_seed_base": int(argument("--bot-seed-base")),
            "baseline_passed_compliance": True,
            "rows": [],
            "behavior_rows": [],
        }
        Path(argument("--output")).write_text(json.dumps(summary), encoding="utf-8")
        Path(argument("--jsonl-output")).write_text("{broken\n", encoding="utf-8")
        Path(argument("--behavior-jsonl-output")).write_text("\n", encoding="utf-8")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tool.subprocess, "run", invalid_jsonl)
    with pytest.raises(RuntimeError, match="invalid probe JSONL row"):
        _run_probe_one(tool, tmp_path)
    assert not list((tmp_path / "data").glob("cf_*.jsonl"))


def test_deck_and_bot_seed_plans_are_resume_deterministic() -> None:
    tool = _load_tool()
    kwargs = {
        "root": 5_000_000,
        "pass_index": 17,
        "task_index": 5,
        "hands": 70,
        "guard": 10,
    }

    assert tool._deck_seed_for_task(**kwargs) == tool._deck_seed_for_task(**kwargs)
    assert tool._bot_seed_for_task(
        root=1_000_000, pass_index=17, task_index=5
    ) == tool._bot_seed_for_task(
        root=1_000_000, pass_index=17, task_index=5
    )


def test_seed_plan_rejects_task_outside_reserved_pass_slots() -> None:
    tool = _load_tool()

    with pytest.raises(ValueError, match="reserved deck-seed slots"):
        tool._deck_seed_for_task(
            root=5_000_000,
            pass_index=0,
            task_index=tool.DECK_SEED_SLOTS_PER_PASS,
            hands=70,
            guard=10,
        )


def test_execution_snapshot_excludes_runtime_markers_and_is_read_only(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    source = tmp_path / "source"
    source.mkdir()
    (source / "national_bot.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".completed").write_text("runtime\n", encoding="utf-8")
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "national_bot.pyc").write_bytes(b"runtime")
    destination = tmp_path / "snapshot" / "national_v1"

    expected = tool._directory_digest(source)
    tool._copy_opponent_snapshot(source, destination)

    assert tool._directory_digest(destination) == expected
    assert not (destination / ".completed").exists()
    assert not (destination / "__pycache__").exists()
    assert destination.stat().st_mode & 0o222 == 0
    assert (destination / "national_bot.py").stat().st_mode & 0o222 == 0


def test_ratings_snapshot_binds_exact_bytes_and_normalized_rows(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    ratings_path = tmp_path / "glicko_ratings.json"
    raw = _ratings_bytes()
    ratings_path.write_bytes(raw)

    snapshot = tool._capture_ratings_snapshot(ratings_path)
    rows = tool._validate_ratings_snapshot(snapshot, ratings_path)

    assert base64.b64decode(snapshot["ratings_bytes_base64"], validate=True) == raw
    assert snapshot["ratings_sha256"] == hashlib.sha256(raw).hexdigest()
    assert snapshot["ratings"] == rows
    assert rows["national_v1"] == {
        "rating": 1600.0,
        "rd": 40.0,
        "conservative": 1520.0,
    }


@pytest.mark.parametrize(
    "tamper",
    ("bytes", "file_digest", "rows", "path", "snapshot_digest"),
)
def test_ratings_snapshot_tampering_fails_closed(
    tmp_path: Path, tamper: str
) -> None:
    tool = _load_tool()
    ratings_path = tmp_path / "glicko_ratings.json"
    ratings_path.write_bytes(_ratings_bytes())
    snapshot = copy.deepcopy(tool._capture_ratings_snapshot(ratings_path))

    if tamper == "bytes":
        snapshot["ratings_bytes_base64"] = base64.b64encode(b"{}\n").decode("ascii")
    elif tamper == "file_digest":
        snapshot["ratings_sha256"] = "0" * 64
    elif tamper == "rows":
        snapshot["ratings"]["national_v1"]["rating"] = 9999.0
    elif tamper == "path":
        snapshot["ratings_path"] = str(tmp_path / "other.json")
    elif tamper == "snapshot_digest":
        snapshot["snapshot_sha256"] = "0" * 64

    if tamper != "snapshot_digest":
        snapshot["snapshot_sha256"] = tool._canonical_json_sha256({
            key: value
            for key, value in snapshot.items()
            if key != "snapshot_sha256"
        })

    with pytest.raises(RuntimeError):
        tool._validate_ratings_snapshot(snapshot, ratings_path)


@pytest.mark.parametrize(
    "tamper",
    (
        "empty_tasks",
        "non_integer_schema",
        "bad_split",
        "role_mismatch",
        "hands",
        "deck_range",
        "bot_seed",
        "missing_provenance",
        "relative_path",
        "duplicate_opponent",
    ),
)
def test_persisted_pass_plan_rejects_invalid_tasks(
    tmp_path: Path, tamper: str
) -> None:
    tool = _load_tool()
    ratings_path, plan = _valid_pass_plan(tool, tmp_path)
    if tamper == "empty_tasks":
        plan["tasks"] = []
    elif tamper == "non_integer_schema":
        plan["schema_version"] = "2"
    elif tamper == "bad_split":
        plan["tasks"][0]["split"] = "selection"
    elif tamper == "role_mismatch":
        plan["tasks"][0]["split"] = "val"
    elif tamper == "hands":
        plan["tasks"][0]["hands"] = 2
    elif tamper == "deck_range":
        plan["tasks"][0]["deck_seed_last"] += 1
    elif tamper == "bot_seed":
        plan["tasks"][0]["bot_seed_base"] += 1
    elif tamper == "missing_provenance":
        del plan["tasks"][0]["tag_commit"]
    elif tamper == "relative_path":
        plan["tasks"][0]["opponent_path"] = "national_v1"
    elif tamper == "duplicate_opponent":
        plan["tasks"].append(copy.deepcopy(plan["tasks"][0]))

    with pytest.raises(RuntimeError):
        tool._validate_pass_plan(
            plan,
            pass_number=1,
            ratings_path=ratings_path,
            hands=1,
            deck_seed_base=tool.DEFAULT_DECK_SEED_BASE,
            deck_seed_guard=tool.DEFAULT_DECK_SEED_GUARD,
            bot_seed_base=tool.DEFAULT_BOT_SEED_BASE,
            val_opponents=set(),
            held_out_opponents=set(),
        )


def test_pass_completion_uses_plan_ratings_after_live_file_disappears(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_tool()
    candidate = _minimal_bot(tmp_path / "candidate")
    ratings_path = tmp_path / "glicko_ratings.json"
    raw = _ratings_bytes()
    ratings_path.write_bytes(raw)
    out_dir = tmp_path / "collection"
    _install_minimal_pool(tool, monkeypatch, tmp_path)

    capture_calls = 0
    capture = tool._capture_ratings_snapshot

    def counted_capture(path):
        nonlocal capture_calls
        capture_calls += 1
        return capture(path)

    def fake_probe(_candidate, _opponent, _split, name, *_args):
        persisted_plan = json.loads(
            (out_dir / "pass_plans" / "pass_0001.json").read_text(
                encoding="utf-8"
            )
        )
        tool._validate_pass_plan(
            persisted_plan,
            pass_number=1,
            ratings_path=ratings_path,
            hands=1,
            deck_seed_base=tool.DEFAULT_DECK_SEED_BASE,
            deck_seed_guard=tool.DEFAULT_DECK_SEED_GUARD,
            bot_seed_base=tool.DEFAULT_BOT_SEED_BASE,
            val_opponents={"national_v2"},
            held_out_opponents={"national_v3"},
        )
        ratings_path.unlink(missing_ok=True)
        return 0, 0, name

    monkeypatch.setattr(tool, "_capture_ratings_snapshot", counted_capture)
    monkeypatch.setattr(tool, "probe_one", fake_probe)

    assert tool.main(_minimal_collection_args(
        candidate=candidate,
        out_dir=out_dir,
        ratings=ratings_path,
        passes=1,
    )) == 0

    assert capture_calls == 1
    assert not ratings_path.exists()
    plan = json.loads(
        (out_dir / "pass_plans" / "pass_0001.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (out_dir / "collection_manifest.json").read_text(encoding="utf-8")
    )
    snapshot = json.loads(
        (out_dir / "pool_snapshots.jsonl").read_text(encoding="utf-8")
    )
    expected_digest = hashlib.sha256(raw).hexdigest()
    assert manifest["resume_contract"]["schema_version"] == (
        tool.COLLECTION_CONTRACT_SCHEMA_VERSION
    )
    assert plan["schema_version"] == tool.PASS_PLAN_SCHEMA_VERSION
    assert plan["ratings_snapshot"]["ratings_sha256"] == expected_digest
    assert snapshot["ratings_sha256"] == expected_digest
    assert snapshot["ratings_snapshot_sha256"] == plan["ratings_snapshot"]["snapshot_sha256"]
    assert {row["name"]: row["glicko"] for row in snapshot["pool"]} == plan[
        "ratings_snapshot"
    ]["ratings"]


def test_each_fresh_pass_captures_ratings_exactly_once(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_tool()
    candidate = _minimal_bot(tmp_path / "candidate")
    ratings_path = tmp_path / "glicko_ratings.json"
    ratings_path.write_bytes(_ratings_bytes())
    out_dir = tmp_path / "collection"
    _install_minimal_pool(tool, monkeypatch, tmp_path)
    capture_calls = 0
    capture = tool._capture_ratings_snapshot

    def counted_capture(path):
        nonlocal capture_calls
        capture_calls += 1
        return capture(path)

    monkeypatch.setattr(tool, "_capture_ratings_snapshot", counted_capture)
    monkeypatch.setattr(
        tool,
        "probe_one",
        lambda _candidate, _opponent, _split, name, *_args: (0, 0, name),
    )

    assert tool.main(_minimal_collection_args(
        candidate=candidate,
        out_dir=out_dir,
        ratings=ratings_path,
        passes=2,
    )) == 0

    assert capture_calls == 2
    assert (out_dir / "pass_plans" / "pass_0001.json").is_file()
    assert (out_dir / "pass_plans" / "pass_0002.json").is_file()
    manifest_path = out_dir / "collection_manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    with pytest.raises(SystemExit, match="cannot shrink"):
        tool.main(_minimal_collection_args(
            candidate=candidate,
            out_dir=out_dir,
            ratings=ratings_path,
            passes=1,
        ))
    assert manifest_path.read_bytes() == manifest_bytes


def test_concurrency_migrated_collection_has_fixed_requested_passes(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_tool()
    candidate = _minimal_bot(tmp_path / "candidate")
    ratings_path = tmp_path / "glicko_ratings.json"
    ratings_path.write_bytes(_ratings_bytes())
    out_dir = tmp_path / "collection"
    _install_minimal_pool(tool, monkeypatch, tmp_path)
    monkeypatch.setattr(
        tool,
        "probe_one",
        lambda _candidate, _opponent, _split, name, *_args: (0, 0, name),
    )
    first = _minimal_collection_args(
        candidate=candidate, out_dir=out_dir, ratings=ratings_path, passes=1
    )
    assert tool.main(first) == 0
    manifest_path = out_dir / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["concurrency_migration"] = {"fixture": True}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = manifest_path.read_bytes()
    extended = _minimal_collection_args(
        candidate=candidate, out_dir=out_dir, ratings=ratings_path, passes=2
    )

    with pytest.raises(SystemExit, match="reviewed target is fixed"):
        tool.main(extended)

    assert manifest_path.read_bytes() == before


def test_persisted_pass_resumes_without_live_ratings(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_tool()
    candidate = _minimal_bot(tmp_path / "candidate")
    ratings_path = tmp_path / "glicko_ratings.json"
    ratings_path.write_bytes(_ratings_bytes())
    out_dir = tmp_path / "collection"
    _install_minimal_pool(tool, monkeypatch, tmp_path)
    probe_calls = 0

    def fake_probe(_candidate, _opponent, _split, name, *_args):
        nonlocal probe_calls
        probe_calls += 1
        return 0, 0, name

    monkeypatch.setattr(tool, "probe_one", fake_probe)
    args = _minimal_collection_args(
        candidate=candidate,
        out_dir=out_dir,
        ratings=ratings_path,
        passes=1,
    )
    assert tool.main(args) == 0
    initial_manifest = json.loads(
        (out_dir / "collection_manifest.json").read_text(encoding="utf-8")
    )

    first_plan = json.loads(
        (out_dir / "pass_plans" / "pass_0001.json").read_text(encoding="utf-8")
    )
    second_plan = copy.deepcopy(first_plan)
    second_plan["pass"] = 2
    for index, task in enumerate(second_plan["tasks"]):
        task["deck_seed_base"] = tool._deck_seed_for_task(
            root=tool.DEFAULT_DECK_SEED_BASE,
            pass_index=1,
            task_index=index,
            hands=1,
            guard=tool.DEFAULT_DECK_SEED_GUARD,
        )
        task["deck_seed_last"] = task["deck_seed_base"]
        task["bot_seed_base"] = tool._bot_seed_for_task(
            root=tool.DEFAULT_BOT_SEED_BASE,
            pass_index=1,
            task_index=index,
        )
    second_plan_path = out_dir / "pass_plans" / "pass_0002.json"
    second_plan_path.write_text(json.dumps(second_plan), encoding="utf-8")
    ratings_path.unlink()

    def forbidden_capture(_path):
        raise AssertionError("resume unexpectedly read live ratings")

    monkeypatch.setattr(tool, "_capture_ratings_snapshot", forbidden_capture)
    resumed_args = _minimal_collection_args(
        candidate=candidate,
        out_dir=out_dir,
        ratings=ratings_path,
        passes=2,
    )
    assert tool.main(resumed_args) == 0
    assert probe_calls == 6
    resumed_manifest = json.loads(
        (out_dir / "collection_manifest.json").read_text(encoding="utf-8")
    )
    assert resumed_manifest["passes_requested"] == 2
    assert resumed_manifest["ratings_sha256_at_start"] == initial_manifest[
        "ratings_sha256_at_start"
    ]
    snapshots = [
        json.loads(line)
        for line in (out_dir / "pool_snapshots.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [row["pass"] for row in snapshots] == [1, 2]


def test_legacy_plan_without_ratings_snapshot_fails_before_probe_or_live_read(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_tool()
    candidate = _minimal_bot(tmp_path / "candidate")
    ratings_path = tmp_path / "glicko_ratings.json"
    ratings_path.write_bytes(_ratings_bytes())
    out_dir = tmp_path / "collection"
    _install_minimal_pool(tool, monkeypatch, tmp_path)
    probe_calls = 0

    def fake_probe(_candidate, _opponent, _split, name, *_args):
        nonlocal probe_calls
        probe_calls += 1
        return 0, 0, name

    monkeypatch.setattr(tool, "probe_one", fake_probe)
    assert tool.main(_minimal_collection_args(
        candidate=candidate,
        out_dir=out_dir,
        ratings=ratings_path,
        passes=1,
    )) == 0

    first_plan = json.loads(
        (out_dir / "pass_plans" / "pass_0001.json").read_text(encoding="utf-8")
    )
    legacy_plan = {
        "pass": 2,
        "seed_scheme": first_plan["seed_scheme"],
        "tasks": first_plan["tasks"],
    }
    legacy_path = out_dir / "pass_plans" / "pass_0002.json"
    legacy_bytes = json.dumps(legacy_plan, separators=(",", ":")).encode("utf-8")
    legacy_path.write_bytes(legacy_bytes)
    ratings_path.unlink()
    calls_before_resume = probe_calls

    def forbidden_capture(_path):
        raise AssertionError("legacy plan unexpectedly read live ratings")

    monkeypatch.setattr(tool, "_capture_ratings_snapshot", forbidden_capture)
    with pytest.raises(RuntimeError, match="predates frozen ratings evidence"):
        tool.main(_minimal_collection_args(
            candidate=candidate,
            out_dir=out_dir,
            ratings=ratings_path,
            passes=2,
        ))

    assert probe_calls == calls_before_resume
    assert legacy_path.read_bytes() == legacy_bytes


def test_empty_persisted_plan_fails_without_rewrite_or_probe(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_tool()
    candidate = _minimal_bot(tmp_path / "candidate")
    ratings_path = tmp_path / "glicko_ratings.json"
    ratings_path.write_bytes(_ratings_bytes())
    out_dir = tmp_path / "collection"
    _install_minimal_pool(tool, monkeypatch, tmp_path)
    probe_calls = 0

    def fake_probe(_candidate, _opponent, _split, name, *_args):
        nonlocal probe_calls
        probe_calls += 1
        return 0, 0, name

    monkeypatch.setattr(tool, "probe_one", fake_probe)
    assert tool.main(_minimal_collection_args(
        candidate=candidate,
        out_dir=out_dir,
        ratings=ratings_path,
        passes=1,
    )) == 0
    first_plan = json.loads(
        (out_dir / "pass_plans" / "pass_0001.json").read_text(encoding="utf-8")
    )
    empty_plan = copy.deepcopy(first_plan)
    empty_plan["pass"] = 2
    empty_plan["tasks"] = []
    empty_path = out_dir / "pass_plans" / "pass_0002.json"
    empty_bytes = json.dumps(empty_plan, separators=(",", ":")).encode("utf-8")
    empty_path.write_bytes(empty_bytes)
    ratings_path.unlink()
    calls_before_resume = probe_calls

    def forbidden_capture(_path):
        raise AssertionError("empty persisted plan unexpectedly read live ratings")

    monkeypatch.setattr(tool, "_capture_ratings_snapshot", forbidden_capture)
    with pytest.raises(RuntimeError, match="tasks must be a non-empty list"):
        tool.main(_minimal_collection_args(
            candidate=candidate,
            out_dir=out_dir,
            ratings=ratings_path,
            passes=2,
        ))

    assert probe_calls == calls_before_resume
    assert empty_path.read_bytes() == empty_bytes


def test_durable_collection_rejects_fallback_pool() -> None:
    tool = _load_tool()

    with pytest.raises(SystemExit, match="incompatible with frozen match-scope"):
        tool.main([
            "--candidate", "unused",
            "--out-dir", "unused",
            "--allow-fallback-pool",
        ])


def test_schema6_accepts_reviewed_six_by_two_runtime_topology(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_tool()
    candidate = _minimal_bot(tmp_path / "candidate")
    ratings_path = tmp_path / "glicko_ratings.json"
    ratings_path.write_bytes(_ratings_bytes())
    out_dir = tmp_path / "collection"
    _install_minimal_pool(tool, monkeypatch, tmp_path)
    monkeypatch.setattr(
        tool,
        "probe_one",
        lambda _candidate, _opponent, _split, name, *_args: (0, 0, name),
    )
    args = _minimal_collection_args(
        candidate=candidate, out_dir=out_dir, ratings=ratings_path, passes=1
    )
    args[args.index("--workers") + 1] = "6"
    args[args.index("--probe-workers") + 1] = "2"

    assert tool.main(args) == 0

    manifest = json.loads(
        (out_dir / "collection_manifest.json").read_text(encoding="utf-8")
    )
    snapshot = json.loads(
        (out_dir / "pool_snapshots.jsonl").read_text(encoding="utf-8")
    )
    assert manifest["resume_contract"]["schema_version"] == 6
    assert (manifest["resume_contract"]["workers"], manifest["resume_contract"]["probe_workers"]) == (6, 2)
    assert (snapshot["workers"], snapshot["probe_workers"]) == (6, 2)


def test_native_match_concurrency_above_host_capacity_is_rejected() -> None:
    tool = _load_tool()

    with pytest.raises(SystemExit, match="must not exceed 12 native matches"):
        tool.main([
            "--candidate", "unused",
            "--out-dir", "unused",
            "--workers", "6",
            "--probe-workers", "3",
        ])
