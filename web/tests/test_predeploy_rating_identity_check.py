"""P5 (2026-10-05): pre-deploy rating-identity guard script.

F7+F8+F13 evidence: two regressions (043919c9 changed a SEMANTIC_PATHS
file, af913b91 changed the pinned POK_DAEMON_PAIRS parameter) were both
discovered only through runtime daemon crashes — the guard was operator
memory.  ``scripts/check_rating_identity_predeploy.py`` compares the
deployment baseline against the to-be-deployed revision and exits non-zero
with an explicit archive-and-reset instruction when either the
rating-identity semantic file set or ``POK_DAEMON_PAIRS`` would change.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_rating_identity_predeploy.py"
PYTHON = sys.executable

SEMANTIC_FILES = [
    "sever/engine/game.py",
    "web/core/elo_daemon.py",
]


def _run(repo: Path, *args: str):
    return subprocess.run(
        [PYTHON, str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _git(repo: Path, *args: str):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "sever" / "engine").mkdir(parents=True)
    (repo / "web" / "core").mkdir(parents=True)
    (repo / "deploy" / "tencent-cloud").mkdir(parents=True)
    (repo / "sever" / "engine" / "game.py").write_text("BASELINE\n", encoding="utf-8")
    (repo / "web" / "core" / "elo_daemon.py").write_text("BASELINE\n", encoding="utf-8")
    (repo / "web" / "core" / "unrelated.py").write_text("BASELINE\n", encoding="utf-8")
    (repo / "deploy" / "tencent-cloud" / "env.runtime").write_text(
        "POK_DAEMON_WORKERS=3\nPOK_DAEMON_PAIRS=1\n",
        encoding="utf-8",
    )
    semantic_list = tmp_path / "semantic.txt"
    semantic_list.write_text("\n".join(SEMANTIC_FILES) + "\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "tag", "baseline-tag")
    # A local branch the deploy branch can name as its upstream (git refuses
    # tag refs for --set-upstream-to).
    _git(repo, "branch", "base")
    return repo


def _commit_change(repo: Path, path: str, content: str, message: str):
    (repo / path).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def test_clean_deployment_exits_zero(tmp_path):
    repo = _make_repo(tmp_path)
    _commit_change(repo, "web/core/unrelated.py", "HEAD\n", "unrelated change")
    result = _run(
        repo,
        "--baseline", "baseline-tag",
        "--semantic-paths-file", str(tmp_path / "semantic.txt"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "需归档重置评分" not in result.stdout


def test_semantic_file_change_requires_archive_reset(tmp_path):
    repo = _make_repo(tmp_path)
    _commit_change(
        repo, "sever/engine/game.py", "CHANGED\n", "semantic change"
    )
    result = _run(
        repo,
        "--baseline", "baseline-tag",
        "--semantic-paths-file", str(tmp_path / "semantic.txt"),
    )
    assert result.returncode != 0
    assert "需归档重置评分（evaluation_data_identity）" in result.stdout
    assert "否则 daemon 将 fail-closed" in result.stdout
    assert "sever/engine/game.py" in result.stdout


def test_env_pairs_change_requires_archive_reset(tmp_path):
    repo = _make_repo(tmp_path)
    _commit_change(
        repo,
        "deploy/tencent-cloud/env.runtime",
        "POK_DAEMON_WORKERS=3\nPOK_DAEMON_PAIRS=2\n",
        "pairs bump",
    )
    result = _run(
        repo,
        "--baseline", "baseline-tag",
        "--semantic-paths-file", str(tmp_path / "semantic.txt"),
    )
    assert result.returncode != 0
    assert "POK_DAEMON_PAIRS" in result.stdout
    assert "需归档重置评分（evaluation_data_identity）" in result.stdout


def test_env_workers_change_alone_is_clean(tmp_path):
    """POK_DAEMON_WORKERS is not in the rating-identity runtime profile
    (operator-verified 2026-10-05: workers scaled 1->3 with pairs pinned);
    only the pinned pairs knob trips the guard."""
    repo = _make_repo(tmp_path)
    _commit_change(
        repo,
        "deploy/tencent-cloud/env.runtime",
        "POK_DAEMON_WORKERS=4\nPOK_DAEMON_PAIRS=1\n",
        "workers bump",
    )
    result = _run(
        repo,
        "--baseline", "baseline-tag",
        "--semantic-paths-file", str(tmp_path / "semantic.txt"),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_uncommitted_workspace_env_pairs_trips(tmp_path):
    """The deployed env.runtime is often an uncommitted workspace edit."""
    repo = _make_repo(tmp_path)
    (repo / "deploy" / "tencent-cloud" / "env.runtime").write_text(
        "POK_DAEMON_WORKERS=3\nPOK_DAEMON_PAIRS=2\n", encoding="utf-8"
    )
    result = _run(
        repo,
        "--baseline", "baseline-tag",
        "--head", "baseline-tag",
        "--semantic-paths-file", str(tmp_path / "semantic.txt"),
    )
    assert result.returncode != 0
    assert "POK_DAEMON_PAIRS" in result.stdout


def test_uncommitted_workspace_semantic_change_trips(tmp_path):
    """B3 (red-team): the working-tree diff leg.

    With ``--baseline HEAD --head HEAD`` (the ExecStartPre mode and any
    post-push checklist run) the committed diff is empty by construction;
    an uncommitted SEMANTIC_PATHS edit in the working tree must still trip
    the guard, otherwise 043919c9-class regressions are only catchable in
    the commit-to-push window.  Red-team reproduced exactly this hole
    against the real worktree (uncommitted elo_daemon.py -> rc=0).
    """
    repo = _make_repo(tmp_path)
    (repo / "web" / "core" / "elo_daemon.py").write_text(
        "UNCOMMITTED WORKSPACE EDIT\n", encoding="utf-8"
    )
    result = _run(
        repo,
        "--baseline", "baseline-tag",
        "--head", "baseline-tag",
        "--semantic-paths-file", str(tmp_path / "semantic.txt"),
    )
    assert result.returncode != 0
    assert "需归档重置评分（evaluation_data_identity）" in result.stdout
    assert "web/core/elo_daemon.py" in result.stdout


def test_staged_semantic_change_trips(tmp_path):
    """The staged (index) leg: staged-but-uncommitted edits count too."""
    repo = _make_repo(tmp_path)
    (repo / "sever" / "engine" / "game.py").write_text(
        "STAGED EDIT\n", encoding="utf-8"
    )
    _git(repo, "add", "sever/engine/game.py")
    result = _run(
        repo,
        "--baseline", "baseline-tag",
        "--head", "baseline-tag",
        "--semantic-paths-file", str(tmp_path / "semantic.txt"),
    )
    assert result.returncode != 0
    assert "sever/engine/game.py" in result.stdout


def test_clean_worktree_head_vs_head_is_clean(tmp_path):
    repo = _make_repo(tmp_path)
    result = _run(
        repo,
        "--baseline", "baseline-tag",
        "--head", "baseline-tag",
        "--semantic-paths-file", str(tmp_path / "semantic.txt"),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_script_exists_and_defaults_to_tracking_branch(tmp_path):
    assert SCRIPT.is_file()
    repo = _make_repo(tmp_path)
    _git(repo, "branch", "-M", "deploy")
    _git(repo, "branch", "--set-upstream-to=base", "deploy")
    result = _run(repo, "--semantic-paths-file", str(tmp_path / "semantic.txt"))
    # baseline defaults to the tracking branch; head defaults to HEAD: clean.
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_repo_head_vs_head_catches_uncommitted_semantic_edit():
    """Smoke on the real checkout (B3 red-team verification).

    Before the working-tree diff leg this smoke returned rc=0 on the very
    worktree that carries an uncommitted SEMANTIC_PATHS edit
    (web/core/elo_daemon.py) — deploying it would crash the daemon with
    ``authoritative rating evaluator identity changed``.  The guard must
    catch it.  The probe is self-sufficient: it injects a transient
    semantic edit itself (byte-restored in finally), so it stays meaningful
    regardless of the ambient working-tree state (2026-10-06: it silently
    depended on another workflow's leftover dirty semantic file).
    """
    semantic = ROOT / "web" / "core" / "elo_daemon.py"
    original = semantic.read_bytes()
    try:
        with semantic.open("ab") as fh:
            fh.write(b"\n# predeploy-probe: transient uncommitted semantic edit for the guard smoke\n")
        result = _run(ROOT, "--baseline", "HEAD", "--head", "HEAD")
        assert result.returncode != 0
        assert "web/core/elo_daemon.py" in result.stdout
        assert "需归档重置评分（evaluation_data_identity）" in result.stdout
    finally:
        semantic.write_bytes(original)
