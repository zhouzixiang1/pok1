import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BATTLE_PATH = ROOT / "engine" / "battle.py"


def _load_battle_module():
    spec = importlib.util.spec_from_file_location("root_engine_battle", BATTLE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_persistent_bot_drains_stderr_pipe(tmp_path):
    battle = _load_battle_module()
    bot = tmp_path / "stderr_spammer.py"
    bot.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "while True:",
                "    line = sys.stdin.readline()",
                "    if not line:",
                "        break",
                "    for _ in range(512):",
                "        sys.stderr.write('telemetry ' + ('x' * 1024) + '\\n')",
                "    sys.stderr.flush()",
                "    sys.stdout.write(json.dumps({'response': 0}) + '\\n')",
                "    sys.stdout.flush()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    proc = battle._PersistentBot(str(bot), decision_timeout_sec=2.0)
    try:
        for _ in range(2):
            action, verdict, data = proc.call({"requests": [], "responses": []})
            assert (action, verdict, data) == (0, "OK", None)
    finally:
        proc.close()
