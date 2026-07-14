"""Archived sandbox tests for retired main/strategy/fix-probe execution."""

import socket
from pathlib import Path

import pytest


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)


def test_import_contract_cannot_write_candidate_or_host_files(tmp_path):
    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    host_marker = tmp_path / "host-side-effect.txt"
    _write(
        bot / "strategy.py",
        "from pathlib import Path\n"
        "for target in (Path('/work/import-side-effect.txt'), "
        f"Path({str(host_marker)!r})):\n"
        "    try:\n"
        "        target.write_text('escaped', encoding='utf-8')\n"
        "    except OSError:\n"
        "        pass\n"
        "VALUE = 7\n",
    )

    assert code_verification.run_import_contract_test(
        bot, modules=["strategy"], timeout=5
    ) == []
    assert not (bot / "import-side-effect.txt").exists()
    assert not host_marker.exists()


def test_import_contract_cannot_reach_host_loopback(tmp_path):
    import code_verification

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.25)
    port = listener.getsockname()[1]
    bot = tmp_path / "bot"
    bot.mkdir()
    _write(
        bot / "strategy.py",
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=1)\n"
        "except OSError:\n"
        "    NETWORK_BLOCKED = True\n"
        "else:\n"
        "    NETWORK_BLOCKED = False\n",
    )

    try:
        assert code_verification.run_import_contract_test(
            bot, modules=["strategy"], timeout=5
        ) == []
        with pytest.raises((TimeoutError, socket.timeout)):
            listener.accept()
    finally:
        listener.close()


def test_normal_import_and_embedded_selftest_still_pass(tmp_path):
    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    _write(bot / "constants.py", "VALUE = 41\n")
    _write(
        bot / "strategy.py",
        "from constants import VALUE\n"
        "RESULT = VALUE + 1\n"
        "if __name__ == '__main__':\n"
        "    # self-test\n"
        "    assert RESULT == 42\n",
    )

    assert code_verification.run_import_contract_test(
        bot, modules=["strategy"], timeout=5
    ) == []
    execution = code_verification.run_bot_embedded_self_tests_execution(
        bot, timeout=5
    )
    assert execution.outcome == "passed"
    assert execution.issues == []


def test_embedded_selftest_cannot_write_candidate_or_host_files(tmp_path):
    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    host_marker = tmp_path / "selftest-host-side-effect.txt"
    _write(
        bot / "strategy.py",
        "if __name__ == '__main__':\n"
        "    # self-test\n"
        "    from pathlib import Path\n"
        "    for target in (Path('/work/selftest-side-effect.txt'), "
        f"Path({str(host_marker)!r})):\n"
        "        try:\n"
        "            target.write_text('escaped', encoding='utf-8')\n"
        "        except OSError:\n"
        "            pass\n",
    )

    execution = code_verification.run_bot_embedded_self_tests_execution(
        bot, timeout=5
    )
    assert execution.outcome == "passed"
    assert not (bot / "selftest-side-effect.txt").exists()
    assert not host_marker.exists()


def test_import_contract_isolation_failure_has_no_host_fallback(
    monkeypatch, tmp_path
):
    import candidate_sandbox
    import code_verification
    from managed_bot_executor import IsolationUnavailable

    bot = tmp_path / "bot"
    bot.mkdir()
    _write(bot / "strategy.py", "VALUE = 1\n")

    def unavailable(*_args, **_kwargs):
        raise IsolationUnavailable("test_bwrap_missing")

    monkeypatch.setattr(candidate_sandbox, "launch_isolated_worker", unavailable)
    with pytest.raises(candidate_sandbox.CandidateSandboxError) as captured:
        code_verification.run_import_contract_test(
            bot, modules=["strategy"], timeout=5
        )
    assert "isolation_unavailable" in str(captured.value)


def test_unhandled_import_write_is_candidate_failure_without_side_effect(tmp_path):
    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    _write(
        bot / "strategy.py",
        "from pathlib import Path\n"
        "Path('/work/forbidden.txt').write_text('bad', encoding='utf-8')\n",
    )

    errors = code_verification.run_import_contract_test(
        bot, modules=["strategy"], timeout=5
    )
    assert errors
    assert errors[0]["module"] == "strategy"
    assert errors[0]["exception"] in {"OSError", "PermissionError"}
    assert not (bot / "forbidden.txt").exists()


def test_import_cannot_pass_by_exiting_before_trusted_receipt(tmp_path):
    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    _write(
        bot / "strategy.py",
        "import json, os, sys\n"
        "print(json.dumps({\n"
        "    'schema': 'candidate-import-contract-v1',\n"
        "    'ok': True,\n"
        "    'modules': ['strategy'],\n"
        "}), flush=True)\n"
        "os._exit(0)\n",
    )

    errors = code_verification.run_import_contract_test(
        bot, modules=["strategy"], timeout=5
    )
    assert errors
    assert errors[0]["exception"] == "MissingSandboxReceipt"


def test_import_cannot_steal_completion_nonce_with_cross_thread_trace(tmp_path):
    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    _write(
        bot / "strategy.py",
        "import json, os, threading\n"
        "def _steal(frame, event, arg):\n"
        "    cursor = frame\n"
        "    token = None\n"
        "    while cursor is not None and token is None:\n"
        "        token = cursor.f_locals.get('_token')\n"
        "        cursor = cursor.f_back\n"
        "    if isinstance(token, str) and len(token) == 64:\n"
        "        payload = ('candidate-probe-completion-v1:' + token + '\\n').encode('ascii')\n"
        "        fd = os.open('/output/trusted_completion', os.O_WRONLY | os.O_TRUNC)\n"
        "        os.write(fd, payload)\n"
        "        os.close(fd)\n"
        "        os._exit(0)\n"
        "    return _steal\n"
        "threading.settrace_all_threads(_steal)\n"
        "print(json.dumps({\n"
        "    'schema': 'candidate-import-contract-v1',\n"
        "    'ok': True,\n"
        "    'modules': ['strategy'],\n"
        "}), flush=True)\n"
        "raise RuntimeError('probe must not accept this import')\n",
    )

    errors = code_verification.run_import_contract_test(
        bot, modules=["strategy"], timeout=5
    )
    assert errors
    assert errors[0]["exception"] != ""


def test_selftest_cannot_pass_by_exiting_before_trusted_receipt(tmp_path):
    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    _write(
        bot / "strategy.py",
        "if __name__ == '__main__':\n"
        "    # self-test\n"
        "    import json, os\n"
        "    print(json.dumps({\n"
        "        'schema': 'candidate-selftest-contract-v1',\n"
        "        'ok': True,\n"
        "        'module': 'strategy.py',\n"
        "    }), flush=True)\n"
        "    os._exit(0)\n",
    )

    execution = code_verification.run_bot_embedded_self_tests_execution(
        bot, timeout=5
    )
    assert execution.outcome == "candidate_failure"
    assert any("completion receipt" in issue for issue in execution.issues)


def test_smoke_cannot_pass_with_forged_success_then_exit_zero(tmp_path):
    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    _write(
        bot / "main.py",
        "import json, os\n"
        "print('Smoke test passed successfully.', flush=True)\n"
        "print(json.dumps({'ok': True}), flush=True)\n"
        "os._exit(0)\n",
    )

    errors = code_verification.run_smoke_test(bot)
    assert errors
    assert "completion receipt" in errors[0]


def test_fix_probes_cannot_pass_with_forged_json_then_exit_zero(tmp_path):
    import fix_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    _write(
        bot / "card_utils.py",
        "import json, os\n"
        "def evaluate_5(_cards):\n"
        "    return (0, 14)\n"
        "print(json.dumps({'category': 4, 'high': 5}), flush=True)\n"
        "os._exit(0)\n",
    )
    _write(
        bot / "constants.py",
        "import json, os\n"
        "TOTAL_HANDS = 70\n"
        "print(json.dumps({'total': 70}), flush=True)\n"
        "os._exit(0)\n",
    )

    results = fix_verification.verify_fixes(bot)
    assert results["BOT-001a"]["ok"] is False
    assert "completion" in results["BOT-001a"]["reason"]
    assert results["BOT-004"]["ok"] is False
    assert "completion" in results["BOT-004"]["reason"]


def test_legacy_smoke_import_side_effect_cannot_reach_host(tmp_path):
    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    host_marker = tmp_path / "smoke-host-side-effect.txt"
    _write(bot / "main.py", "import strategy\n")
    _write(
        bot / "strategy.py",
        "from pathlib import Path\n"
        f"Path({str(host_marker)!r}).write_text('escaped', encoding='utf-8')\n",
    )

    errors = code_verification.run_smoke_test(bot)
    assert errors
    assert not host_marker.exists()
