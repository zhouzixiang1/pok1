"""Regression checks for Claude Code auto-loaded repository guidance."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_run_claude_query_uses_project_root_for_claude_md_autoload():
    """Direct sub-agent calls should auto-load the root CLAUDE.md guidance."""
    source = (PROJECT_ROOT / "web" / "core" / "llm_query.py").read_text()

    assert "cwd=str(PROJECT_ROOT)" in source
    assert "workers use relative paths like bots/claude_vN" in source
    assert "mcp_servers={}" in source
    assert "strict_mcp_config=True" in source


def test_root_claude_md_keeps_current_module_and_protocol_boundaries():
    text = (PROJECT_ROOT / "CLAUDE.md").read_text()

    required = [
        "`engine/` — local Botzone-style subprocess battle engine",
        "`web/` — unified evolution system",
        "`sever/` — national competition TCP self-play platform",
        "`rl/` — reinforcement learning experiments",
        "The old `web/tui.py` Textual TUI no longer exists",
        "Botzone/local protocol",
        "National competition protocol",
        "sever/bot_adapter.py",
        "raise-to-total",
        "postflop",
        "all-in",
        "Claude Code therefore auto-loads this root `CLAUDE.md`",
        "empty strict MCP config",
        "do not auto-start user/global MCP servers",
    ]
    missing = [item for item in required if item not in text]
    assert not missing


def test_directory_claude_md_supplements_match_root_boundaries():
    web_text = (PROJECT_ROOT / "web" / "CLAUDE.md").read_text()
    sever_text = (PROJECT_ROOT / "sever" / "CLAUDE.md").read_text()

    assert "evolution target remains Botzone/local JSON bots" in web_text
    assert "national TCP deployment goes through `sever/bot_adapter.py`" in web_text
    assert "do not mix local JSON battle and TCP protocols" not in web_text.lower()

    sever_required = [
        "Transport",
        "TCP Socket",
        "raise <amount>",
        "Raise semantics",
        "Postflop pass",
        "All-in runout",
        "Illegal action → fold",
        "_TCP_TO_JUDGE_SUIT",
    ]
    missing = [item for item in sever_required if item not in sever_text]
    assert not missing
