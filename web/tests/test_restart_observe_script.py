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


def test_restart_rejects_manual_checkpoint_and_session_cleanup_options(tmp_path):
    root = _write_minimal_restart_fixture(tmp_path)
    (root / "web/core/results/v73/logs").mkdir(parents=True)
    (root / "web/core/results/v73/logs/master_io.txt").write_text("stale\n", encoding="utf-8")
    (root / "bots/national_v73").mkdir(parents=True)
    (root / "bots/national_v73/strategy.py").write_text("# stale\n", encoding="utf-8")

    for option, value in (
        ("--clear-checkpoint", "backup-and-clear"),
        ("--clear-session", "always"),
    ):
        proc = subprocess.run(
            ["bash", "scripts/pok_restart_observe.sh", option, value],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 2
        assert f"Unknown option: {option}" in proc.stderr

    assert (root / "web/core/results/v73").is_dir()
    assert (root / "bots/national_v73").is_dir()


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
            "--observe-generations",
            "0",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "observe-only mode: no checkpoint/candidate/config changes and no restart" in proc.stdout
    assert "checking unfinished generation artifacts" not in proc.stdout


def test_restart_dry_run_stops_before_atomic_config_and_starts(tmp_path):
    root = _write_minimal_restart_fixture(tmp_path)

    proc = subprocess.run(
        [
            "bash",
            "scripts/pok_restart_observe.sh",
            "--dry-run",
            "--daemon-workers",
            "4",
            "--daemon-pairs",
            "7",
            "--observe-generations",
            "0",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )

    stop_at = proc.stdout.index("+ ./pokctl.sh stop")
    config_at = proc.stdout.index("persisting daemon config atomically")
    start_at = proc.stdout.index("+ ./pokctl.sh start")
    assert stop_at < config_at < start_at
    assert not (root / "web/core/results/app_config.json").exists()


def test_restart_rejects_out_of_range_daemon_config(tmp_path):
    root = _write_minimal_restart_fixture(tmp_path)

    for option, value, expected in (
        ("--daemon-workers", "0", "integer in [1, 12]"),
        ("--daemon-workers", "13", "integer in [1, 12]"),
        ("--daemon-pairs", "0", "integer in [1, 20]"),
        ("--daemon-pairs", "21", "integer in [1, 20]"),
        ("--daemon-pairs", "not-an-int", "integer in [1, 20]"),
    ):
        proc = subprocess.run(
            ["bash", "scripts/pok_restart_observe.sh", option, value],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 2
        assert expected in proc.stderr


def test_restart_uses_shared_durable_config_writer():
    source = (ROOT / "scripts" / "pok_restart_observe.sh").read_text(
        encoding="utf-8"
    )

    assert "from server.state import AppState" in source
    assert "state.update_config(" in source
    assert "path.write_text" not in source
    assert "./pokctl.sh stop" in source
    assert "./pokctl.sh start" in source
