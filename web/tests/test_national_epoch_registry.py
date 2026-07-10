from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from national_epoch_registry import (
    GitResult,
    GitRepository,
    MIGRATION_MARKER_TAG,
    MigrationError,
    RegistryUnavailableError,
    advance_high_water,
    apply_migration_plan,
    build_migration_plan,
    create_reaped_tombstone,
    effective_target_version,
    load_registry_state,
    parse_legacy_ledger,
    subprocess_git_runner,
)


def _git(repo: Path, *args: str, input_text: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Registry Test")
    _git(repo, "config", "user.email", "registry@example.invalid")
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def _commit_bot(repo: Path, version: int) -> str:
    bot_dir = repo / "bots" / f"national_v{version}"
    bot_dir.mkdir(parents=True)
    (bot_dir / "national_bot.py").write_text(f"VERSION = {version}\n", encoding="utf-8")
    _git(repo, "add", str(bot_dir.relative_to(repo)))
    _git(repo, "commit", "-m", f"evolve national v{version}")
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "-a", f"national-bot-v{version}", "-m", f"complete v{version}", commit)
    return commit


def _write_ledger(path: Path, versions: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps({"bot": f"national_v{version}", "version": version}) + "\n"
            for version in versions
        ),
        encoding="utf-8",
    )


def _registry_tags(repo: Path) -> list[str]:
    output = _git(
        repo,
        "tag",
        "-l",
        "national-reaped-*",
        check=True,
    )
    extra = _git(repo, "tag", "-l", "national-high-water-*", check=True)
    return sorted(filter(None, (output + "\n" + extra).splitlines()))


def test_missing_legacy_ledger_without_marker_is_unavailable(tmp_path):
    repo = _repo(tmp_path)
    ledger = tmp_path / "missing.jsonl"

    state = load_registry_state(repo, legacy_ledger=ledger)

    assert state.available is False
    assert state.source == "unavailable"
    assert state.reaped_versions == frozenset()
    assert "legacy_ledger_missing" in state.diagnostics
    with pytest.raises(RegistryUnavailableError):
        state.require_reaped_versions()


@pytest.mark.parametrize(
    "content, diagnostic",
    [
        ("", "legacy_ledger_empty"),
        ("not-json\n", "invalid_json"),
        ('{"version": 2, "bot": "national_v3"}\n', "version_bot_mismatch"),
        ('{"version": "2", "bot": "national_v2"}\n', "invalid_version"),
        ('{"version": 2}\n\n', "blank_line"),
    ],
)
def test_corrupt_legacy_ledger_never_exposes_partial_versions(
    tmp_path, content: str, diagnostic: str
):
    ledger = tmp_path / "reaped.jsonl"
    ledger.write_text(content, encoding="utf-8")

    parsed = parse_legacy_ledger(ledger)

    assert parsed.available is False
    assert parsed.versions == frozenset()
    assert any(diagnostic in item for item in parsed.diagnostics)


def test_valid_legacy_ledger_is_authoritative_before_marker(tmp_path):
    repo = _repo(tmp_path)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [2, 5])

    state = load_registry_state(repo, legacy_ledger=ledger)

    assert state.available is True
    assert state.source == "legacy_ledger"
    assert state.require_reaped_versions() == frozenset({2, 5})


@pytest.mark.parametrize("legacy_mode", ["missing", "corrupt"])
def test_marker_makes_durable_tags_authoritative(tmp_path, legacy_mode: str):
    repo = _repo(tmp_path)
    commit = _commit_bot(repo, 1)
    _git(repo, "tag", "-a", "national-reaped-v1", "-m", "reaped", commit)
    _git(repo, "tag", "-a", MIGRATION_MARKER_TAG, "-m", "migrated", commit)
    ledger = tmp_path / "reaped.jsonl"
    if legacy_mode == "corrupt":
        ledger.write_text("broken\n", encoding="utf-8")

    state = load_registry_state(repo, legacy_ledger=ledger)

    assert state.available is True
    assert state.source == "durable_tags"
    assert state.require_reaped_versions() == frozenset({1})
    assert any(item.startswith("legacy_diagnostic:") for item in state.diagnostics)


def test_marker_ignores_stale_legacy_membership(tmp_path):
    repo = _repo(tmp_path)
    commit = _commit_bot(repo, 1)
    _git(repo, "tag", "-a", "national-reaped-v1", "-m", "reaped", commit)
    _git(repo, "tag", "-a", MIGRATION_MARKER_TAG, "-m", "migrated", commit)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [1, 9])

    state = load_registry_state(repo, legacy_ledger=ledger)

    assert state.available is True
    assert state.reaped_versions == frozenset({1})
    assert "legacy_diagnostic:legacy_differs_from_durable_tags" in state.diagnostics


def test_migrated_registry_can_repair_high_water_without_legacy_ledger(tmp_path):
    repo = _repo(tmp_path)
    commit = _commit_bot(repo, 4)
    _git(repo, "tag", "-a", MIGRATION_MARKER_TAG, "-m", "migrated", commit)
    ledger = tmp_path / "missing.jsonl"

    plan = build_migration_plan(repo, legacy_ledger=ledger)

    assert plan.ready is True
    assert plan.already_migrated is True
    assert [tag.name for tag in plan.create_tags] == ["national-high-water-v4"]
    apply_migration_plan(plan, repo_root=repo)
    state = load_registry_state(repo, legacy_ledger=ledger)
    assert state.available is True
    assert state.high_water_versions == frozenset({4})


def test_effective_target_uses_requested_completion_high_water_and_history(tmp_path):
    repo = _repo(tmp_path)
    _commit_bot(repo, 3)
    commit = _commit_bot(repo, 12)
    _git(repo, "tag", "-a", "national-high-water-v12", "-m", "water", commit)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [3])

    assert effective_target_version(2, repo_root=repo, legacy_ledger=ledger) == 13

    _git(repo, "tag", "-d", "national-bot-v12")
    _git(repo, "tag", "-d", "national-high-water-v12")

    state = load_registry_state(repo, legacy_ledger=ledger)
    assert state.history_high_water == 12
    assert effective_target_version(2, repo_root=repo, state=state) == 13


def test_unmappable_legacy_entry_prevents_every_registry_ref(tmp_path):
    repo = _repo(tmp_path)
    _commit_bot(repo, 1)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [1, 2])
    head_before = _git(repo, "rev-parse", "HEAD")

    plan = build_migration_plan(repo, legacy_ledger=ledger)

    assert plan.ready is False
    assert plan.unmappable_versions == (2,)
    assert plan.create_tags == ()
    with pytest.raises(MigrationError):
        apply_migration_plan(plan, repo_root=repo)
    assert _registry_tags(repo) == []
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_update_ref_failure_leaves_no_partial_registry_refs(tmp_path):
    repo = _repo(tmp_path)
    _commit_bot(repo, 1)
    _commit_bot(repo, 2)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [1, 2])

    def failing_runner(args, *, cwd, input_text=None):
        if tuple(args[:2]) == ("update-ref", "--stdin"):
            return GitResult(1, stderr="injected transaction failure")
        return subprocess_git_runner(args, cwd=cwd, input_text=input_text)

    plan = build_migration_plan(repo, legacy_ledger=ledger, runner=failing_runner)
    assert plan.ready is True

    with pytest.raises(MigrationError, match="atomic registry ref transaction failed"):
        apply_migration_plan(plan, repo_root=repo, runner=failing_runner)

    assert _registry_tags(repo) == []


def test_successful_migration_is_atomic_annotated_idempotent_and_head_safe(tmp_path):
    repo = _repo(tmp_path)
    _commit_bot(repo, 1)
    _commit_bot(repo, 2)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [1, 2])
    head_before = _git(repo, "rev-parse", "HEAD")

    plan = build_migration_plan(repo, legacy_ledger=ledger)
    result = apply_migration_plan(plan, repo_root=repo, now=lambda: 1_700_000_000)

    assert set(result.created_tags) == {
        "national-reaped-v1",
        "national-reaped-v2",
        "national-high-water-v2",
        MIGRATION_MARKER_TAG,
    }
    assert result.head_before == result.head_after == head_before
    for name in result.created_tags:
        assert _git(repo, "cat-file", "-t", f"refs/tags/{name}") == "tag"

    state = load_registry_state(repo, legacy_ledger=ledger)
    assert state.available is True
    assert state.source == "durable_tags"
    assert state.reaped_versions == frozenset({1, 2})
    assert state.high_water_versions == frozenset({2})

    second_plan = build_migration_plan(repo, legacy_ledger=ledger)
    second_result = apply_migration_plan(second_plan, repo_root=repo)
    assert second_plan.already_migrated is True
    assert second_result.created_tags == ()
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_runtime_tombstone_and_high_water_apis_create_annotated_monotonic_tags(tmp_path):
    repo = _repo(tmp_path)
    _commit_bot(repo, 1)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [1])
    migration = build_migration_plan(repo, legacy_ledger=ledger)
    apply_migration_plan(migration, repo_root=repo)
    _commit_bot(repo, 3)

    reaped = create_reaped_tombstone(3, repo_root=repo, legacy_ledger=ledger)
    water = advance_high_water(2, repo_root=repo, legacy_ledger=ledger)

    assert reaped.created_tags == ("national-reaped-v3",)
    assert water.created_tags == ("national-high-water-v3",)
    assert _git(repo, "cat-file", "-t", "refs/tags/national-reaped-v3") == "tag"
    assert _git(repo, "cat-file", "-t", "refs/tags/national-high-water-v3") == "tag"
    state = load_registry_state(repo, legacy_ledger=ledger)
    assert state.reaped_versions == frozenset({1, 3})
    assert max(state.high_water_versions) == 3

    no_regression = advance_high_water(1, repo_root=repo, legacy_ledger=ledger)
    assert no_regression.created_tags == ()


def test_cli_defaults_to_dry_run_then_applies_without_moving_head(tmp_path):
    repo = _repo(tmp_path)
    _commit_bot(repo, 1)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [1])
    head_before = _git(repo, "rev-parse", "HEAD")
    script = Path(__file__).resolve().parents[2] / "scripts" / "migrate_national_epoch_registry.py"

    dry_run = subprocess.run(
        [sys.executable, str(script), "--repo-root", str(repo), "--ledger", str(ledger)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert json.loads(dry_run.stdout)["mode"] == "dry-run"
    assert _registry_tags(repo) == []

    applied = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo-root",
            str(repo),
            "--ledger",
            str(ledger),
            "--apply",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["result"]["head_unchanged"] is True
    assert MIGRATION_MARKER_TAG in _registry_tags(repo)
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_runner_is_injectable_for_read_paths(tmp_path):
    repo = _repo(tmp_path)
    calls: list[tuple[str, ...]] = []

    def recording_runner(args, *, cwd, input_text=None):
        calls.append(tuple(args))
        return subprocess_git_runner(args, cwd=cwd, input_text=input_text)

    git = GitRepository(repo, recording_runner)
    assert git.run("rev-parse", "HEAD").returncode == 0
    assert calls == [("rev-parse", "HEAD")]
