import importlib.util
from pathlib import Path

from bot_namespace import bot_name


ROOT = Path(__file__).resolve().parents[2]


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "national_arena_cli",
        ROOT / "scripts" / "national_arena.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_arena_cli_run_calls_shared_api_contract(monkeypatch, capsys):
    module = _load_cli()
    calls = []

    class FakeAPI:
        def __init__(self, base_url, token=""):
            calls.append(("init", base_url, token))

        def request(self, method, path, payload=None, timeout=30.0):
            calls.append((method, path, payload))
            if path == "/api/national-arena/sessions":
                return {"session_id": "arena_test", "status": "created"}
            return {"session_id": "arena_test", "status": "starting"}

    monkeypatch.setattr(module, "ArenaAPI", FakeAPI)
    result = module.main([
        "run",
        "--mode", "managed",
        "--top-bot", bot_name(141),
        "--bottom-bot", bot_name(142),
        "--hands", "70",
    ])

    assert result == 0
    create = next(call for call in calls if call[:2] == (
        "POST", "/api/national-arena/sessions"
    ))
    assert create[2]["mode"] == "managed_bots"
    assert create[2]["port"] == 0
    assert create[2]["official_action_delay"] == 0.30
    assert create[2]["capacity_wait_seconds"] == 30.0
    assert create[2]["managed_port_override"] is False
    assert any(call[:2] == (
        "POST", "/api/national-arena/sessions/arena_test/start"
    ) for call in calls)
    assert "arena_test" in capsys.readouterr().out


def test_arena_cli_rejects_managed_run_without_both_bots(capsys):
    module = _load_cli()
    result = module.main(["run", "--mode", "managed", "--top-bot", "national_v1"])
    assert result == 2
    assert "requires --top-bot and --bottom-bot" in capsys.readouterr().err


def test_arena_cli_external_default_keeps_official_port_explicit(monkeypatch):
    module = _load_cli()
    calls = []

    class FakeAPI:
        def __init__(self, _base_url, token=""):
            del token

        def request(self, method, path, payload=None, timeout=30.0):
            del timeout
            calls.append((method, path, payload))
            return {"session_id": "arena_external", "status": "created"}

    monkeypatch.setattr(module, "ArenaAPI", FakeAPI)
    assert module.main(["create", "--mode", "external"]) == 0
    assert calls[0][2]["port"] == 10001


def test_arena_cli_wait_exits_nonzero_for_quarantine(monkeypatch):
    module = _load_cli()

    class FakeAPI:
        def __init__(self, _base_url, token=""):
            del token

        def request(self, method, path, payload=None, timeout=30.0):
            del method, payload, timeout
            if path == "/api/national-arena/sessions":
                return {"session_id": "arena_fenced", "status": "created"}
            if path.endswith("/start"):
                return {"session_id": "arena_fenced", "status": "starting"}
            return {
                "session_id": "arena_fenced",
                "status": "quarantined",
                "hands_completed": 0,
                "hands_total": 70,
                "top_total_earnings": 0,
                "bottom_total_earnings": 0,
            }

    monkeypatch.setattr(module, "ArenaAPI", FakeAPI)
    result = module.main([
        "run",
        "--mode", "managed",
        "--top-bot", "national_v1",
        "--bottom-bot", "national_v1",
        "--wait",
    ])
    assert result == 2
