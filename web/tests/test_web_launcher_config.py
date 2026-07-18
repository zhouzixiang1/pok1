import builtins
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_launcher():
    path = ROOT / "web" / "main.py"
    spec = importlib.util.spec_from_file_location("pok_web_launcher_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_launcher_respects_persisted_daemon_disabled(tmp_path):
    from server.state import AppState

    config_path = tmp_path / "app_config.json"
    state = AppState(config_file=config_path)
    state.update_config(
        daemon_enabled=False,
        daemon_workers=3,
        daemon_pairs=4,
    )

    projected = _load_launcher().apply_cli_runtime_overrides(
        state,
        no_daemon=False,
    )

    assert projected["daemon_enabled"] is False
    assert projected["daemon_workers"] == 3
    assert projected["daemon_pairs"] == 4
    assert json.loads(config_path.read_text(encoding="utf-8"))[
        "daemon_enabled"
    ] is False


def test_explicit_no_daemon_is_process_local(tmp_path):
    from server.state import AppState

    config_path = tmp_path / "app_config.json"
    state = AppState(config_file=config_path)
    state.update_config(
        daemon_enabled=True,
        daemon_workers=2,
        daemon_pairs=5,
    )

    projected = _load_launcher().apply_cli_runtime_overrides(
        state,
        no_daemon=True,
    )

    assert projected["daemon_enabled"] is False
    assert json.loads(config_path.read_text(encoding="utf-8"))[
        "daemon_enabled"
    ] is True


def _configure_static_receipt_paths(monkeypatch, launcher, tmp_path):
    static_dir = tmp_path / "static"
    receipt = static_dir / ".pok-static-build-receipt.json"
    verifier = tmp_path / "static-build-receipt.mjs"
    monkeypatch.setattr(launcher, "STATIC_DIR", static_dir)
    monkeypatch.setattr(launcher, "STATIC_BUILD_RECEIPT", receipt)
    monkeypatch.setattr(launcher, "STATIC_BUILD_RECEIPT_VERIFIER", verifier)
    return static_dir, receipt, verifier


def _patch_main_launch_dependencies(monkeypatch, launcher):
    import logging_config
    import uvicorn

    observed = {}
    monkeypatch.setattr(
        logging_config,
        "configure_logging",
        lambda **kwargs: observed.setdefault("logging", kwargs),
    )
    monkeypatch.setattr(
        launcher,
        "apply_cli_runtime_overrides",
        lambda _state, *, no_daemon: observed.setdefault("no_daemon", no_daemon),
    )
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda *args, **kwargs: observed.setdefault("uvicorn", (args, kwargs)),
    )
    return observed


def test_no_build_static_receipt_uses_shared_source_verifier(monkeypatch, tmp_path):
    launcher = _load_launcher()
    static_dir, receipt, verifier = _configure_static_receipt_paths(
        monkeypatch,
        launcher,
        tmp_path,
    )
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("dashboard\n", encoding="utf-8")
    receipt.write_text("{}\n", encoding="utf-8")
    verifier.write_text("// verifier fixture\n", encoding="utf-8")

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="verified\n", stderr="")

    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda command: "/test-bin/node" if command == "node" else None,
    )
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher.verify_frontend_static_receipt() is True
    assert calls == [
        (
            [
                "/test-bin/node",
                str(verifier),
                "--verify",
                str(receipt),
            ],
            {
                "cwd": str(launcher.PROJECT_ROOT),
                "capture_output": True,
                "text": True,
                "check": False,
            },
        )
    ]


def test_no_build_static_receipt_refuses_missing_static_before_node(monkeypatch, tmp_path):
    launcher = _load_launcher()
    _configure_static_receipt_paths(monkeypatch, launcher, tmp_path)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda _command: pytest.fail(
            "Node lookup must not happen without static output"
        ),
    )

    assert launcher.verify_frontend_static_receipt() is False


def test_no_build_static_receipt_refuses_verifier_rejection(monkeypatch, tmp_path):
    launcher = _load_launcher()
    static_dir, receipt, verifier = _configure_static_receipt_paths(
        monkeypatch,
        launcher,
        tmp_path,
    )
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("dashboard\n", encoding="utf-8")
    receipt.write_text("stale\n", encoding="utf-8")
    verifier.write_text("// verifier fixture\n", encoding="utf-8")
    monkeypatch.setattr(launcher.shutil, "which", lambda _command: "/test-bin/node")
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="static bundle receipt does not match current frontend build inputs\n",
        ),
    )

    assert launcher.verify_frontend_static_receipt() is False


def test_no_build_launcher_refuses_receipt_failure_before_server_import(monkeypatch):
    launcher = _load_launcher()
    monkeypatch.setattr(launcher, "verify_frontend_static_receipt", lambda: False)
    monkeypatch.setattr(
        launcher,
        "build_frontend",
        lambda: pytest.fail("--no-build must not enter the normal build path"),
    )
    monkeypatch.setattr(launcher.sys, "argv", ["web/main.py", "--no-build"])

    original_import = builtins.__import__
    forbidden_imports = []

    def import_guard(name, *args, **kwargs):
        if name == "uvicorn" or name == "server" or name.startswith("server."):
            forbidden_imports.append(name)
            raise AssertionError(f"--no-build failure imported {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_guard)

    with pytest.raises(SystemExit) as stopped:
        launcher.main()

    assert stopped.value.code == 1
    assert forbidden_imports == []


def test_valid_no_build_receipt_reaches_uvicorn_only_after_preflight(monkeypatch):
    launcher = _load_launcher()
    observed = _patch_main_launch_dependencies(monkeypatch, launcher)
    monkeypatch.setattr(launcher, "verify_frontend_static_receipt", lambda: True)
    monkeypatch.setattr(
        launcher,
        "build_frontend",
        lambda: pytest.fail("valid --no-build must not rebuild the frontend"),
    )
    monkeypatch.setattr(
        launcher.sys,
        "argv",
        [
            "web/main.py",
            "--no-build",
            "--host",
            "127.0.0.1",
            "--port",
            "18765",
        ],
    )

    launcher.main()

    assert observed["no_daemon"] is False
    assert observed["uvicorn"] == (
        ("server.app:app",),
        {"host": "127.0.0.1", "port": 18765, "reload": False},
    )


def test_normal_launcher_build_path_does_not_require_existing_static_receipt(monkeypatch):
    launcher = _load_launcher()
    observed = _patch_main_launch_dependencies(monkeypatch, launcher)
    builds = []
    monkeypatch.setattr(launcher, "build_frontend", lambda: builds.append(True) or True)
    monkeypatch.setattr(
        launcher,
        "verify_frontend_static_receipt",
        lambda: pytest.fail("normal build owns the receipt creation path"),
    )
    monkeypatch.setattr(launcher.sys, "argv", ["web/main.py", "--port", "18766"])

    launcher.main()

    assert builds == [True]
    assert observed["uvicorn"] == (
        ("server.app:app",),
        {"host": "0.0.0.0", "port": 18766, "reload": False},
    )
