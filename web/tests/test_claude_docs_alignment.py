"""Regression checks for Claude Code auto-loaded repository guidance."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_run_claude_query_uses_project_root_for_claude_md_autoload():
    """Direct sub-agent calls should auto-load the root CLAUDE.md guidance."""
    source = (PROJECT_ROOT / "web" / "core" / "llm_query.py").read_text()

    assert "cwd=str(PROJECT_ROOT)" in source
    assert "workers use relative paths like bots/national_vN" in source
    assert "mcp_servers={}" in source
    assert "strict_mcp_config=True" in source


def test_root_claude_md_keeps_current_module_and_protocol_boundaries():
    text = (PROJECT_ROOT / "CLAUDE.md").read_text()
    normalized = " ".join(text.split())

    required = [
        "Read `AGENTS.md` first",
        "The only active bot architecture is `national_tcp_policy_v1`",
        "Candidate code lives only in `policy.py`",
        "typed `decision_context`",
        "typed `fold/pass/allin/raise_to` intent",
        "Everything below `archive/` is retired",
        "zero execution, evaluation, or prompt-evidence authority",
        "complete 70-hand raw native TCP matches only",
    ]
    missing = [item for item in required if item not in normalized]
    assert not missing
    assert "Botzone/local protocol" not in text
    assert "sever/bot_adapter.py" not in text


def test_directory_claude_md_supplements_match_root_boundaries():
    web_text = (PROJECT_ROOT / "web" / "CLAUDE.md").read_text()
    sever_text = (PROJECT_ROOT / "sever" / "CLAUDE.md").read_text()
    web_normalized = " ".join(web_text.split())
    sever_normalized = " ".join(sever_text.split())

    assert "sole active `national_tcp_policy_v1` architecture" in web_normalized
    assert "Candidate strategy lives in `policy.py`" in web_normalized
    assert "Do not add active execution profiles, adapter fallbacks" in web_normalized
    assert "subprocess-JSON" in web_normalized
    assert "sever/bot_adapter.py" not in web_text

    sever_required = [
        "Official messages contain no `\\n`/`\\r\\n`",
        "raise <amount>",
        "means raise to the total street contribution",
        "Exact `raise 400` after `raise 200`",
        "second player closes a checked street with `call`",
        "omitting a peer street-closing call/check",
        "Terminal fold/call and showdown cards",
        "Retired adapters and alternate protocol engines live under `archive/`",
    ]
    missing = [item for item in sever_required if item not in sever_normalized]
    assert not missing
    assert "newline-delimited" not in sever_text
