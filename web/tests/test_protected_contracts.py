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
