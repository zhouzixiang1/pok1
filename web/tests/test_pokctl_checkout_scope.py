import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _install_pokctl(root: Path) -> None:
    shutil.copy2(REPO_ROOT / "pokctl.sh", root / "pokctl.sh")
    (root / "web" / "logs").mkdir(parents=True, exist_ok=True)


def _write_pid_file(root: Path, pid: int) -> None:
    (root / "web" / "logs" / ".server.pid").write_text(f'{{"pid": {pid}}}\n', encoding="utf-8")


def _fake_proc(proc_root: Path, pid: int, cwd: Path, cmd: list[str]) -> None:
    proc_dir = proc_root / str(pid)
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "cwd").symlink_to(cwd, target_is_directory=True)
    (proc_dir / "cmdline").write_bytes(b"\0".join(part.encode("utf-8") for part in cmd) + b"\0")


def _run_pokctl(root: Path, proc_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["POKCTL_PROC_ROOT"] = str(proc_root)
    return subprocess.run(
        ["bash", str(root / "pokctl.sh"), *args],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def test_pokctl_rejects_nested_checkout_relative_web_process(tmp_path):
    outer = tmp_path / "pok"
    nested = outer / ".evolution_pok"
    proc_root = tmp_path / "proc"
    pid = 12345

    _init_git_repo(outer)
    _init_git_repo(nested)
    _install_pokctl(outer)
    _write_pid_file(outer, pid)
    _fake_proc(proc_root, pid, nested, ["python3", "web/main.py", "--port", "8000"])

    result = _run_pokctl(outer, proc_root, "status")

    assert result.returncode == 1
    assert "checkout" in result.stdout


def test_pokctl_accepts_current_checkout_relative_web_process(tmp_path):
    outer = tmp_path / "pok"
    proc_root = tmp_path / "proc"
    pid = 12346

    _init_git_repo(outer)
    _install_pokctl(outer)
    _write_pid_file(outer, pid)
    _fake_proc(proc_root, pid, outer, ["python3", "web/main.py", "--port", "8000"])

    result = _run_pokctl(outer, proc_root, "status")

    assert result.returncode == 0
    assert f"PID: {pid}" in result.stdout


def test_pokctl_accepts_current_checkout_absolute_web_program(tmp_path):
    outer = tmp_path / "pok"
    other = tmp_path / "other"
    proc_root = tmp_path / "proc"
    pid = 12347

    _init_git_repo(outer)
    other.mkdir()
    _install_pokctl(outer)
    _write_pid_file(outer, pid)
    _fake_proc(proc_root, pid, other, ["python3", str(outer / "web/main.py"), "--port", "8000"])

    result = _run_pokctl(outer, proc_root, "status")

    assert result.returncode == 0
    assert f"PID: {pid}" in result.stdout
