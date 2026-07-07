import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BATTLE_PATH = ROOT / "engine" / "battle.py"
WEB_CORE_BATTLE_PATH = ROOT / "web" / "core" / "engine" / "battle.py"


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


def _run_broken_pipe_probe(tmp_path, battle_path, constructor_args):
    bot = tmp_path / "instant_exit.py"
    bot.write_text("raise SystemExit(0)\n", encoding="utf-8")
    probe = tmp_path / "broken_pipe_probe.py"
    probe.write_text(
        "\n".join(
            [
                "import gc",
                "import importlib.util",
                "import json",
                "import time",
                f"battle_path = {str(battle_path)!r}",
                f"bot_path = {str(bot)!r}",
                f"constructor_args = {constructor_args!r}",
                "spec = importlib.util.spec_from_file_location('battle_under_test', battle_path)",
                "module = importlib.util.module_from_spec(spec)",
                "assert spec.loader is not None",
                "spec.loader.exec_module(module)",
                "bot = module._PersistentBot(bot_path, *constructor_args)",
                "time.sleep(0.05)",
                "result = bot.call({'requests': [], 'responses': []})",
                "bot.close()",
                "del bot",
                "gc.collect()",
                "print(json.dumps({'result': result[:2]}))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(probe)],
        text=True,
        capture_output=True,
        timeout=5,
    )


def test_root_persistent_bot_suppresses_broken_pipe_finalizer_noise(tmp_path):
    result = _run_broken_pipe_probe(tmp_path, BATTLE_PATH, [0.5])
    assert result.returncode == 0, result.stderr
    assert "Exception ignored while finalizing file" not in result.stderr
    assert "BrokenPipeError" not in result.stderr


def test_web_core_persistent_bot_suppresses_broken_pipe_finalizer_noise(tmp_path):
    result = _run_broken_pipe_probe(tmp_path, WEB_CORE_BATTLE_PATH, [])
    assert result.returncode == 0, result.stderr
    assert "Exception ignored while finalizing file" not in result.stderr
    assert "BrokenPipeError" not in result.stderr
