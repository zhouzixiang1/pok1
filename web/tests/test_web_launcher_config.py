import importlib.util
import json
from pathlib import Path


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
