import os
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


def _write_interpreter_handoff_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = _write_minimal_restart_fixture(tmp_path)
    calls = root / "calls.log"
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    project_python = fake_bin / "project-python"
    bare_python = fake_bin / "python"
    project_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf 'project-python %s\\n' \"$*\" >> \"${FAKE_PYTHON_CALLS:?}\"\n"
        "payload=\"$(cat)\"\n"
        "if [[ \"$payload\" == *'config_transaction'* ]]; then\n"
        "  mkdir -p \"$(dirname \"$2\")\"\n"
        "  printf '{\\\"daemon_enabled\\\":true}\\n' > \"$2\"\n"
        "  printf '{\\\"config_transaction\\\":\\\"committed\\\"}\\n'\n"
        "elif [[ \"$payload\" == *'health check failed'* ]]; then\n"
        "  printf 'health ok\\n'\n"
        "fi\n",
        encoding="utf-8",
    )
    bare_python.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'bare-python %s\\n' \"$*\" >> \"${FAKE_PYTHON_CALLS:?}\"\n"
        "exit 97\n",
        encoding="utf-8",
    )
    project_python.chmod(0o755)
    bare_python.chmod(0o755)
    (root / "pokctl.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf 'pokctl %s\\n' \"$*\" >> \"${POKCTL_CALLS:?}\"\n"
        "case \"${1:-}\" in\n"
        "  resolve-python) printf '%s\\n' \"${POK_PYTHON:?}\" ;;\n"
        "  verify-frontend-static) [ \"${VERIFY_FRONTEND_RECEIPT:-1}\" = \"1\" ] ;;\n"
        "  stop|start) : ;;\n"
        "  *) echo \"unexpected pokctl command: $*\" >&2; exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (root / "pokctl.sh").chmod(0o755)
    static = root / "web/server/static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("ok\n", encoding="utf-8")
    return root, project_python, calls


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
            "8",
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
        ("--daemon-pairs", "0", "integer in [1, 8]"),
        ("--daemon-pairs", "9", "integer in [1, 8]"),
        ("--daemon-pairs", "not-an-int", "integer in [1, 8]"),
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
    assert "./pokctl.sh resolve-python" in source
    assert '"$RUNTIME_PYTHON" - "$RESULTS_DIR/app_config.json"' in source
    assert '"$RUNTIME_PYTHON" - "$HOST" "$PORT" "$RUN_LOG"' in source
    assert '"$RUNTIME_PYTHON" - "$RESULTS_DIR/events.jsonl"' in source
    assert source.index("RUNTIME_PYTHON=") < source.index("run ./pokctl.sh stop")
    assert "./pokctl.sh stop" in source
    assert "./pokctl.sh start" in source
    assert "evaluation sample budget 1..8" in source
    assert "not a Bot strength verdict" in source


def test_restart_helper_preflights_the_shared_interpreter_before_stop():
    process_control = (ROOT / "pokctl.sh").read_text(encoding="utf-8")
    restart = (ROOT / "scripts" / "pok_restart_observe.sh").read_text(
        encoding="utf-8"
    )

    assert "cmd_resolve_python()" in process_control
    assert "resolve-python does not accept arguments" in process_control
    assert "require_web_python >&2" in process_control
    assert "printf '%s\\n' \"$PYTHON\"" in process_control
    assert "durable config writer import preflight failed before stopping service" in restart
    assert "--no-build frontend receipt preflight failed before stopping service" in restart
    assert "./pokctl.sh verify-frontend-static" in restart
    assert restart.index("./pokctl.sh resolve-python") < restart.index(
        "run ./pokctl.sh stop"
    )
    assert restart.index("./pokctl.sh verify-frontend-static") < restart.index(
        "run ./pokctl.sh stop"
    )
    assert "ignoring --no-build" not in restart


def test_restart_uses_resolved_python_for_config_and_health_before_stop(tmp_path):
    root, project_python, calls = _write_interpreter_handoff_fixture(tmp_path)
    env = {
        **os.environ,
        "POK_PYTHON": str(project_python),
        "POKCTL_CALLS": str(calls),
        "FAKE_PYTHON_CALLS": str(calls),
        "PATH": f"{project_python.parent}:{os.environ['PATH']}",
    }

    proc = subprocess.run(
        [
            "bash",
            "scripts/pok_restart_observe.sh",
            "--no-build",
            "--host",
            "127.0.0.1",
            "--port",
            "18765",
            "--observe-generations",
            "0",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert (root / "web/core/results/app_config.json").exists()
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("pokctl resolve-python") for line in call_lines)
    assert any(line.startswith("pokctl stop") for line in call_lines)
    assert any(line.startswith("pokctl start") for line in call_lines)
    assert any(line.startswith("project-python - ") for line in call_lines)
    assert not any(line.startswith("bare-python") for line in call_lines)


def test_restart_refuses_missing_resolved_python_before_stop(tmp_path):
    root, _project_python, calls = _write_interpreter_handoff_fixture(tmp_path)
    missing_python = root / "missing-project-python"
    env = {
        **os.environ,
        "POK_PYTHON": str(missing_python),
        "POKCTL_CALLS": str(calls),
        "FAKE_PYTHON_CALLS": str(calls),
    }

    proc = subprocess.run(
        [
            "bash",
            "scripts/pok_restart_observe.sh",
            "--no-build",
            "--observe-generations",
            "0",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode != 0
    assert "durable config writer import preflight failed before stopping service" in proc.stderr
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("pokctl resolve-python") for line in call_lines)
    assert not any(line.startswith("pokctl stop") for line in call_lines)
    assert not any(line.startswith("pokctl start") for line in call_lines)
    assert not (root / "web/core/results/app_config.json").exists()


def test_restart_refuses_stale_no_build_receipt_before_stop(tmp_path):
    root, project_python, calls = _write_interpreter_handoff_fixture(tmp_path)
    env = {
        **os.environ,
        "POK_PYTHON": str(project_python),
        "POKCTL_CALLS": str(calls),
        "FAKE_PYTHON_CALLS": str(calls),
        "VERIFY_FRONTEND_RECEIPT": "0",
    }

    proc = subprocess.run(
        [
            "bash",
            "scripts/pok_restart_observe.sh",
            "--no-build",
            "--observe-generations",
            "0",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode != 0
    assert "frontend receipt preflight failed before stopping service" in proc.stderr
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("pokctl verify-frontend-static") for line in call_lines)
    assert not any(line.startswith("pokctl stop") for line in call_lines)
    assert not any(line.startswith("pokctl start") for line in call_lines)
    assert not (root / "web/core/results/app_config.json").exists()
