from protected_contracts import check_bot_protocol_contract


def test_protected_contract_allows_json_bot(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "main.py").write_text(
        "import json\nprint(json.dumps({'response': 0}))\n",
        encoding="utf-8",
    )

    assert check_bot_protocol_contract(bot) == []


def test_protected_contract_rejects_tcp_text_stdout(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "main.py").write_text("print('raise 200')\n", encoding="utf-8")

    errors = check_bot_protocol_contract(bot)
    assert errors
    assert "TCP action text" in errors[0]


def test_protected_contract_allows_internal_action_labels(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "main.py").write_text(
        "import json\nfrom strategy import bucket\n"
        "print(json.dumps({'response': 0, 'data': bucket()}))\n",
        encoding="utf-8",
    )
    (bot / "strategy.py").write_text(
        "def bucket():\n"
        "    return 'call'\n"
        "def _private_bucket_action():\n"
        "    return 'fold'\n",
        encoding="utf-8",
    )

    assert check_bot_protocol_contract(bot) == []


def test_protected_contract_rejects_entrypoint_return_text(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "main.py").write_text(
        "from strategy import get_action\n"
        "print(get_action())\n",
        encoding="utf-8",
    )
    (bot / "strategy.py").write_text(
        "def get_action():\n"
        "    return 'fold'\n",
        encoding="utf-8",
    )

    errors = check_bot_protocol_contract(bot)
    assert errors
    assert "get_action()" in errors[0]


def test_protected_contract_allows_stderr_debug_text(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "main.py").write_text(
        "import json, sys\n"
        "print('fold', file=sys.stderr)\n"
        "print(json.dumps({'response': 0}))\n",
        encoding="utf-8",
    )

    assert check_bot_protocol_contract(bot) == []
