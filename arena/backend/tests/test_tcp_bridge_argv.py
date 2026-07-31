"""TCP 桥 argv_style 参数传递测试。

验证修复「桥 spawn bot 传位置参数,但内置 national_v* 用 argparse 旗标会崩溃」:

1. ``make_dockerfile`` 生成的 ENTRYPOINT 含正确的 ``--argv-style``。
2. 桥 ``_parse_args`` 能解析 ``--argv-style``。
3. ``_spawn_bot`` 按 argv_style 构造的 cmd 形式正确:
   - flags: ``--host H --port P --name N``
   - positional: ``H P N``
   - env: 不带连接参数(只靠 GUOSAI_*)

纯逻辑测试,不依赖 docker / 真起子进程。
"""
from __future__ import annotations

from arena.backend.platform.runtime import tcp_bridge
from arena.backend.platform.runtime.builder import make_dockerfile


# ── 改动三:Dockerfile ENTRYPOINT 透传 argv_style ────────────────

def test_dockerfile_tcp_entrypoint_has_argv_style_flags():
    """flags 风格的 ENTRYPOINT 含 --argv-style flags。"""
    df = make_dockerfile(protocol="tcp", entry_file="national_bot.py",
                         runtime_lang="python", argv_style="flags")
    assert '"--argv-style", "flags"' in df, df
    assert "tcp_bridge.py" in df


def test_dockerfile_tcp_entrypoint_has_argv_style_positional():
    """positional 风格的 ENTRYPOINT 含 --argv-style positional。"""
    df = make_dockerfile(protocol="tcp", entry_file="main.py",
                         runtime_lang="python", argv_style="positional")
    assert '"--argv-style", "positional"' in df, df


def test_dockerfile_json_ignores_argv_style():
    """JSON 协议不走桥,ENTRYPOINT 不含 --argv-style。"""
    df = make_dockerfile(protocol="json", entry_file="main.py",
                         runtime_lang="python", argv_style="flags")
    assert "--argv-style" not in df
    assert "ENTRYPOINT" in df


# ── 改动二:桥 _parse_args 解析 --argv-style ─────────────────────

def test_parse_args_default_argv_style():
    """无 --argv-style 时默认 flags。"""
    args = tcp_bridge._parse_args(["--bot-entry", "national_bot.py"])
    assert args.argv_style == "flags"


def test_parse_args_explicit_argv_style():
    args = tcp_bridge._parse_args(
        ["--bot-entry", "national_bot.py", "--argv-style", "positional"])
    assert args.argv_style == "positional"


def test_parse_args_rejects_invalid_argv_style():
    """非法 argv_style 被 argparse choices 拒绝(模拟关键修复点)。"""
    import pytest
    with pytest.raises(SystemExit):
        tcp_bridge._parse_args(
            ["--bot-entry", "x.py", "--argv-style", "bogus"])


# ── 改动二:_spawn_bot 按风格构造 cmd(核心修复)──────────────────

def _cmd_for_style(monkeypatch, style: str) -> str:
    """构造一个桥并捕获 _spawn_bot 实际执行的 cmd(不真起进程)。

    用 monkeypatch 把 asyncio.create_subprocess_exec 替换成记录 cmd 的桩。
    """
    import asyncio
    captured: dict = {}

    async def _fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        captured["env"] = kwargs.get("env", {})

        class _FakeProc:
            returncode = None
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    bridge = tcp_bridge.TCPBridge(
        bot_entry="national_bot.py", bot_name="BotV117",
        listen_host="127.0.0.1", listen_port=50101, argv_style=style)
    asyncio.new_event_loop().run_until_complete(bridge._spawn_bot())
    return " ".join(captured["cmd"])


def test_spawn_cmd_flags_style(monkeypatch):
    """flags 风格:cmd 含 --host/--port/--name(适配 national_v* argparse)。"""
    cmd = _cmd_for_style(monkeypatch, "flags")
    assert "--host 127.0.0.1" in cmd, cmd
    assert "--port 50101" in cmd, cmd
    assert "--name BotV117" in cmd, cmd
    # 不应含裸的位置参数 127.0.0.1(会被 argparse 拒)
    # (cmd 里 127.0.0.1 只跟在 --host 后)


def test_spawn_cmd_positional_style(monkeypatch):
    """positional 风格:cmd 含裸位置参数(兼容旧 uploads bot)。"""
    cmd = _cmd_for_style(monkeypatch, "positional")
    assert "127.0.0.1 50101 BotV117" in cmd, cmd
    assert "--host" not in cmd, cmd


def test_spawn_cmd_env_style(monkeypatch):
    """env 风格:cmd 不含连接参数(只靠 GUOSAI_* env)。"""
    cmd = _cmd_for_style(monkeypatch, "env")
    assert "127.0.0.1" not in cmd, cmd
    assert "--host" not in cmd, cmd
    assert "BotV117" not in cmd, cmd


def test_spawn_sets_guosai_env_regardless_of_style(monkeypatch):
    """所有风格都设 GUOSAI_* 环境变量(兜底)。"""
    import asyncio
    captured: dict = {}

    async def _fake_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env", {})

        class _FakeProc:
            returncode = None
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    for style in ("flags", "positional", "env"):
        captured.clear()
        bridge = tcp_bridge.TCPBridge(
            bot_entry="x.py", bot_name="Bot",
            listen_host="127.0.0.1", listen_port=50101, argv_style=style)
        asyncio.new_event_loop().run_until_complete(bridge._spawn_bot())
        assert captured["env"].get("GUOSAI_HOST") == "127.0.0.1"
        assert captured["env"].get("GUOSAI_PORT") == "50101"
        assert captured["env"].get("GUOSAI_NAME") == "Bot"
