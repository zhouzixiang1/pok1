import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _write_minimal_restart_fixture(tmp_path: Path) -> Path:
    script_src = ROOT / "scripts" / "pok_restart_observe.sh"
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "pok_restart_observe.sh").write_text(
        script_src.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "pokctl.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo pokctl \"$@\"\n",
        encoding="utf-8",
    )
    (scripts / "pok_restart_observe.sh").chmod(0o755)
    (root / "pokctl.sh").chmod(0o755)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "tag", "national-bot-v72"], cwd=root, check=True)
    return root


def test_restart_dry_run_archives_unfinished_results_without_checkpoint(tmp_path):
    root = _write_minimal_restart_fixture(tmp_path)
    (root / "web/core/results/v73/logs").mkdir(parents=True)
    (root / "web/core/results/v73/logs/master_io.txt").write_text("stale\n", encoding="utf-8")
    (root / "bots/national_v73").mkdir(parents=True)
    (root / "bots/national_v73/strategy.py").write_text("# stale\n", encoding="utf-8")

    proc = subprocess.run(
        [
            "bash",
            "scripts/pok_restart_observe.sh",
            "--dry-run",
            "--clear-checkpoint",
            "backup-and-clear",
            "--clear-session",
            "always",
            "--observe-generations",
            "0",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "checking unfinished generation artifacts newer than national-bot-v72" in proc.stdout
    assert "+ mv web/core/results/v73 web/core/results/abandoned/" in proc.stdout
    assert "+ mv bots/national_v73 web/core/results/abandoned/" in proc.stdout


def test_restart_observe_only_does_not_archive_unfinished_results(tmp_path):
    root = _write_minimal_restart_fixture(tmp_path)
    (root / "web/core/results/v73/logs").mkdir(parents=True)
    (root / "bots/national_v73").mkdir(parents=True)

    proc = subprocess.run(
        [
            "bash",
            "scripts/pok_restart_observe.sh",
            "--dry-run",
            "--observe-only",
            "--clear-checkpoint",
            "backup-and-clear",
            "--observe-generations",
            "0",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "observe-only mode: no checkpoint/session/config changes and no restart" in proc.stdout
    assert "checking unfinished generation artifacts" not in proc.stdout
