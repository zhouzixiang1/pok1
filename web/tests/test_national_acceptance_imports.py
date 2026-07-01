import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "web" / "core"


def test_national_acceptance_imports_sever_modules_when_web_server_is_loaded(monkeypatch):
    monkeypatch.syspath_prepend(str(CORE))
    for name in list(sys.modules):
        if (
            name == "national_acceptance"
            or name == "bot_adapter"
            or name == "server"
            or name.startswith("server.")
            or name == "engine"
            or name.startswith("engine.")
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)

    web_server = types.ModuleType("server")
    web_server.__path__ = [str(ROOT / "web" / "server")]
    monkeypatch.setitem(sys.modules, "server", web_server)

    national_acceptance = importlib.import_module("national_acceptance")

    assert national_acceptance.BotAdapter.__module__ == "bot_adapter"
    assert national_acceptance.GameEngine.__module__ == "engine.game"
    assert national_acceptance.THPRecorder.__module__ == "engine.thp_recorder"
    assert sys.modules["server"] is web_server
