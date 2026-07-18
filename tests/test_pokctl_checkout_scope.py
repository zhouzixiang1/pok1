import os
import shutil
import subprocess
from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pokctl.sh").exists():
            return parent
    raise RuntimeError("Could not locate repository root")


REPO_ROOT = _repo_root()


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _install_pokctl(root: Path) -> None:
    shutil.copy2(REPO_ROOT / "pokctl.sh", root / "pokctl.sh")
    (root / "web" / "logs").mkdir(parents=True, exist_ok=True)


def _write_source_bound_static_bundle(root: Path) -> None:
    """Create a minimal valid frontend receipt, then let callers make it stale."""

    frontend = root / "web" / "frontend"
    script = frontend / "scripts" / "static-build-receipt.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        REPO_ROOT / "web" / "frontend" / "scripts" / "static-build-receipt.mjs",
        script,
    )
    source_files = {
        "index.html": "<div id='root'></div>\n",
        "package.json": "{}\n",
        "package-lock.json": "{}\n",
        "postcss.config.js": "export default {}\n",
        "tsconfig.json": "{}\n",
        "tsconfig.app.json": "{}\n",
        "tsconfig.node.json": "{}\n",
        "vite.config.ts": "export default {}\n",
        "banner.png": "not-a-real-png\n",
        "src/main.tsx": "export const revision = 1;\n",
        "public/favicon.png": "not-a-real-png\n",
    }
    for relative, contents in source_files.items():
        path = frontend / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    dist = frontend / "dist"
    dist.mkdir()
    receipt = dist / ".pok-static-build-receipt.json"
    subprocess.run(
        ["node", str(script), "--write", str(receipt)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    static = root / "web" / "server" / "static"
    (static / "assets").mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    shutil.copy2(receipt, static / receipt.name)


def _write_pid_file(root: Path, pid: int) -> None:
    (root / "web" / "logs" / ".server.pid").write_text(f'{{"pid": {pid}}}\n', encoding="utf-8")


def _fake_proc(proc_root: Path, pid: int, cwd: Path, cmd: list[str]) -> None:
    proc_dir = proc_root / str(pid)
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "cwd").symlink_to(cwd, target_is_directory=True)
    (proc_dir / "cmdline").write_bytes(b"\0".join(part.encode("utf-8") for part in cmd) + b"\0")


def _run_pokctl(
    root: Path,
    proc_root: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["POKCTL_PROC_ROOT"] = str(proc_root)
    env.update(extra_env or {})
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


def test_pokctl_no_build_requires_existing_static_bundle(tmp_path):
    outer = tmp_path / "pok"
    proc_root = tmp_path / "proc"

    _init_git_repo(outer)
    _install_pokctl(outer)

    result = _run_pokctl(outer, proc_root, "start", "--no-build")

    assert result.returncode == 1
    assert "--no-build requested" in result.stdout
    assert "web/server/static/index.html" in result.stdout


def test_pokctl_restart_refuses_stale_no_build_receipt_before_stop(tmp_path):
    outer = tmp_path / "pok"
    proc_root = tmp_path / "proc"
    pid = 12349

    _init_git_repo(outer)
    _install_pokctl(outer)
    _write_source_bound_static_bundle(outer)
    # The receipt represents revision 1, while the live source tree now has
    # revision 2. A restart must fail before it can signal the owned process.
    (outer / "web" / "frontend" / "src" / "main.tsx").write_text(
        "export const revision = 2;\n", encoding="utf-8"
    )
    _write_pid_file(outer, pid)
    _fake_proc(proc_root, pid, outer, ["python3", "web/main.py", "--port", "8000"])

    result = _run_pokctl(outer, proc_root, "restart", "--no-build")

    assert result.returncode == 1
    assert "frontend static receipt validation failed; refusing restart before stop" in result.stdout
    assert "does not match current frontend build inputs" in result.stdout
    assert "正在停止服务" not in result.stdout
    assert (outer / "web" / "logs" / ".server.pid").exists()


def test_pokctl_rejects_missing_web_dependencies_for_explicit_python(tmp_path):
    outer = tmp_path / "pok"
    proc_root = tmp_path / "proc"
    selected_python = outer / "missing-project-python"

    _init_git_repo(outer)
    _install_pokctl(outer)
    _write_source_bound_static_bundle(outer)

    result = _run_pokctl(
        outer,
        proc_root,
        "start",
        "--no-build",
        extra_env={"POK_PYTHON": str(selected_python)},
    )

    assert result.returncode == 1
    assert str(selected_python) in result.stdout
    assert "POK_PYTHON" in result.stdout
    assert not (outer / "web" / "logs" / ".server.pid").exists()


def test_pokctl_restart_preserves_owned_server_when_override_is_invalid(tmp_path):
    outer = tmp_path / "pok"
    proc_root = tmp_path / "proc"
    pid = 12348
    selected_python = outer / "missing-project-python"

    _init_git_repo(outer)
    _install_pokctl(outer)
    _write_pid_file(outer, pid)
    _fake_proc(proc_root, pid, outer, ["python3", "web/main.py", "--port", "8000"])

    result = _run_pokctl(
        outer,
        proc_root,
        "restart",
        extra_env={"POK_PYTHON": str(selected_python)},
    )

    assert result.returncode == 1
    assert str(selected_python) in result.stdout
    assert (outer / "web" / "logs" / ".server.pid").exists()
