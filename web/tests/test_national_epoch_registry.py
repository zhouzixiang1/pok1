from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from national_epoch_registry import (
    GitResult,
    GitRepository,
    HIGH_WATER_TAG_PREFIX,
    MIGRATION_MARKER_TAG,
    MigrationError,
    REAPED_TAG_PREFIX,
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
from bot_namespace import bot_name, bot_tag, high_water_tag


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
    bot_dir = repo / "bots" / bot_name(version)
    bot_dir.mkdir(parents=True)
    (bot_dir / "national_bot.py").write_text(f"VERSION = {version}\n", encoding="utf-8")
    _git(repo, "add", str(bot_dir.relative_to(repo)))
    _git(repo, "commit", "-m", f"evolve national v{version}")
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "-a", bot_tag(version), "-m", f"complete v{version}", commit)
    return commit


def _write_ledger(path: Path, versions: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps({"bot": bot_name(version), "version": version}) + "\n"
            for version in versions
        ),
        encoding="utf-8",
    )


def _registry_tags(repo: Path) -> list[str]:
    # The migration marker tag (national-reaped-registry-v1) shares the
    # ``national-reaped-`` stem with the per-version reaped tombstones
    # (national-reaped-vN), so glob that stem to capture both families.
    reaped_stem = REAPED_TAG_PREFIX.removesuffix("v").rstrip("-")
    output = _git(
        repo,
        "tag",
        "-l",
        f"{reaped_stem}-*",
        check=True,
    )
    extra = _git(repo, "tag", "-l", f"{HIGH_WATER_TAG_PREFIX}*", check=True)
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
        ('{"version": 2, "bot": "%s"}\n' % bot_name(3), "version_bot_mismatch"),
        ('{"version": "2", "bot": "%s"}\n' % bot_name(2), "invalid_version"),
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
    assert [tag.name for tag in plan.create_tags] == [f"{HIGH_WATER_TAG_PREFIX}4"]
    apply_migration_plan(plan, repo_root=repo)
    state = load_registry_state(repo, legacy_ledger=ledger)
    assert state.available is True
    assert state.high_water_versions == frozenset({4})


def test_effective_target_uses_requested_completion_high_water_and_history(tmp_path):
    repo = _repo(tmp_path)
    _commit_bot(repo, 3)
    commit = _commit_bot(repo, 12)
    _git(repo, "tag", "-a", high_water_tag(12), "-m", "water", commit)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [3])

    assert effective_target_version(2, repo_root=repo, legacy_ledger=ledger) == 13

    _git(repo, "tag", "-d", bot_tag(12))
    _git(repo, "tag", "-d", high_water_tag(12))

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
        f"{REAPED_TAG_PREFIX}1",
        f"{REAPED_TAG_PREFIX}2",
        f"{HIGH_WATER_TAG_PREFIX}2",
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

    assert reaped.created_tags == (f"{REAPED_TAG_PREFIX}3",)
    assert water.created_tags == (f"{HIGH_WATER_TAG_PREFIX}3",)
    assert _git(repo, "cat-file", "-t", f"refs/tags/{REAPED_TAG_PREFIX}3") == "tag"
    assert _git(repo, "cat-file", "-t", f"refs/tags/{HIGH_WATER_TAG_PREFIX}3") == "tag"
    state = load_registry_state(repo, legacy_ledger=ledger)
    assert state.reaped_versions == frozenset({1, 3})
    assert max(state.high_water_versions) == 3

    no_regression = advance_high_water(1, repo_root=repo, legacy_ledger=ledger)
    assert no_regression.created_tags == ()


def test_orphan_high_water_tag_from_epoch_reset_does_not_block_lower_advancement(
    tmp_path,
):
    """An orphan high-water tag left by an interrupted epoch reset must not
    poison the namespace floor for a fresh lower-numbered publication.

    Reproduces the v1 publication blocker caused by a stale
    ``national-cloud-high-water-v143`` tag (pointing at the "archive stale
    candidate (reset)" commit) surviving an epoch reset that removed its paired
    completion tag.  Per ``resolve_version_namespace_authority``'s pairing
    contract, a high-water tag with no matching completion tag is an
    interrupted effect and carries no version authority.
    """

    repo = _repo(tmp_path)
    # Establish the migrated registry with a real paired v1 publication.
    _commit_bot(repo, 1)
    ledger = tmp_path / "reaped.jsonl"
    _write_ledger(ledger, [1])
    migration = build_migration_plan(repo, legacy_ledger=ledger)
    apply_migration_plan(migration, repo_root=repo)

    # Simulate the epoch-reset leftover: a stale bot directory (v143) that is
    # archived/removed, with an orphan high-water tag left pointing at the
    # removal commit and NO matching completion tag.
    stale_dir = repo / "bots" / bot_name(143)
    stale_dir.mkdir(parents=True)
    (stale_dir / "national_bot.py").write_text("VERSION = 143\n", encoding="utf-8")
    _git(repo, "add", str(stale_dir.relative_to(repo)))
    _git(repo, "commit", "-m", "seed stale v143")
    # Remove the seed directory (the reset archive step).
    _git(repo, "rm", "-r", str(stale_dir.relative_to(repo)))
    _git(repo, "commit", "-m", "archive stale v143 candidate (reset)")
    reset_commit = _git(repo, "rev-parse", "HEAD")
    # Orphan high-water tag with NO paired completion tag — the leftover.
    _git(repo, "tag", "-a", high_water_tag(143), reset_commit, "-m", "stale water")

    state = load_registry_state(repo, legacy_ledger=ledger)
    assert 143 in state.high_water_versions
    assert 143 not in state.paired_high_water_versions
    assert state.paired_high_water_versions == frozenset({1})
    # The stale v143 seed directory was git-rm'd, so it is no longer tracked at
    # HEAD; only the still-published v1 directory counts, so the history
    # high-water floor is 1 (not poisoned by the archived seed).
    assert state.history_high_water == 1
    assert any(
        "orphan_high_water_tags_without_completion_pair" in d for d in state.diagnostics
    )

    # The real lower-numbered publication (v2) must still advance normally and
    # create its high-water tag rather than being short-circuited by the orphan.
    _commit_bot(repo, 2)
    water = advance_high_water(2, repo_root=repo, legacy_ledger=ledger)
    assert water.created_tags == (f"{HIGH_WATER_TAG_PREFIX}2",)
    assert _git(repo, "cat-file", "-t", f"refs/tags/{HIGH_WATER_TAG_PREFIX}2") == "tag"

    # The orphan tag is left in place for audit; only its authority is ignored.
    assert _git(repo, "cat-file", "-t", f"refs/tags/{HIGH_WATER_TAG_PREFIX}143") == "tag"

    # effective_target_version must also ignore the orphan when allocating.
    assert effective_target_version(3, repo_root=repo, legacy_ledger=ledger) == 3


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
