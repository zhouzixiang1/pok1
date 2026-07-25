from pathlib import Path
import subprocess

import bot_artifact
from bot_artifact import published_bot_identity
from bot_namespace import EVOLUTION_BRANCH, bot_name, bot_tag


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    # The publication authority checks ``refs/heads/{EVOLUTION_BRANCH}``
    # (main on the canonical branch, tencent-cloud-runtime on the cloud line).
    # Seed the repo on that branch so the identity resolver finds the commit.
    _git(repo, "init", "-b", EVOLUTION_BRANCH)
    _git(repo, "config", "user.name", "Official Identity Test")
    _git(repo, "config", "user.email", "official-identity@example.invalid")
    return repo


def _write_bot(repo: Path, body: str = "print('ready')\n") -> Path:
    bot = repo / "bots" / bot_name(1)
    bot.mkdir(parents=True, exist_ok=True)
    (bot / "national_bot.py").write_text(body, encoding="utf-8")
    return bot


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "bots")
    _git(repo, "commit", "-m", message)


def test_published_identity_requires_annotated_tag_on_main(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    bot = _write_bot(repo)
    _commit_all(repo, "add bot")
    _git(repo, "tag", "-a", bot_tag(1), "-m", "complete")
    monkeypatch.setattr(bot_artifact, "ROOT", repo)

    identity = published_bot_identity(bot)

    assert identity["published"] is True
    assert identity["tag_type"] == "tag"
    assert identity["tag_commit_on_main"] is True
    assert identity["completion_tree_matches_main"] is True


def test_published_identity_rejects_lightweight_tag(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    bot = _write_bot(repo)
    _commit_all(repo, "add bot")
    _git(repo, "tag", bot_tag(1))
    monkeypatch.setattr(bot_artifact, "ROOT", repo)

    identity = published_bot_identity(bot)

    assert identity["published"] is False
    assert "missing_annotated_completion_tag" in identity["issues"]


def test_published_identity_rejects_tag_commit_outside_main(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    (repo / "README").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "main root")
    _git(repo, "switch", "-c", "candidate")
    bot = _write_bot(repo)
    _commit_all(repo, "candidate bot")
    _git(repo, "tag", "-a", bot_tag(1), "-m", "complete")
    _git(repo, "switch", EVOLUTION_BRANCH)
    bot = _write_bot(repo)
    _commit_all(repo, "independent main bot")
    monkeypatch.setattr(bot_artifact, "ROOT", repo)

    identity = published_bot_identity(bot)

    assert identity["published"] is False
    assert identity["completion_tree_matches_main"] is True
    assert "completion_tag_commit_not_on_main" in identity["issues"]


def test_published_identity_rejects_main_tree_drift_after_tag(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    bot = _write_bot(repo)
    _commit_all(repo, "add bot")
    _git(repo, "tag", "-a", bot_tag(1), "-m", "complete")
    (bot / "national_bot.py").write_text("print('changed')\n", encoding="utf-8")
    _commit_all(repo, "change bot after completion")
    monkeypatch.setattr(bot_artifact, "ROOT", repo)

    identity = published_bot_identity(bot)

    assert identity["published"] is False
    assert identity["tag_commit_on_main"] is True
    assert "completion_tag_bot_tree_differs_from_main" in identity["issues"]
