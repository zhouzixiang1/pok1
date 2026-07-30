"""4 个已知问题修复的测试。

问题1:orchestrator settle 时实时更新 hands_played
问题2:create_match 填 protocol_a/b(已验证 challenge 流程正确,补测)
问题3:多语言(C++/Java)Dockerfile 模板
问题4:内置 bot 懒构建镜像(build_builtin_image + orchestrator 懒触发)
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena.backend.platform.runtime import make_dockerfile
from arena.backend.platform.runtime.builder import BotBuildError
from arena.backend.platform.auth import AuthManager
from arena.backend.platform.auth.routes import router as auth_router
from arena.backend.platform.runtime.routes import router as bots_router
from arena.backend.platform.store import Store, PROTO_JSON, PROTO_TCP


# ══════════════════════════════════════════════════════════
# 问题 3:多语言 Dockerfile
# ══════════════════════════════════════════════════════════

def test_dockerfile_cpp_json():
    """C++ JSON bot:gcc 编译,运行二进制。"""
    df = make_dockerfile(protocol="json", entry_file="bot.cpp", runtime_lang="cpp")
    assert "FROM gcc:13-slim" in df
    assert "g++ -O2 -std=c++17" in df
    assert "./bot_bin" in df
    assert "USER botuser" in df  # 非 root


def test_dockerfile_java_json():
    """Java JSON bot:javac 编译,java 运行。"""
    df = make_dockerfile(protocol="json", entry_file="Main.java", runtime_lang="java")
    assert "FROM eclipse-temurin" in df
    assert "javac *.java" in df
    assert 'ENTRYPOINT ["java", "Main"]' in df  # classname 从 Main.java 提取
    assert "USER botuser" in df


def test_dockerfile_cpp_tcp():
    """C++ TCP bot:含桥代理 + bot-cmd 指向二进制(且 ENTRYPOINT 全为字符串)。"""
    import json
    import re
    df = make_dockerfile(protocol="tcp", entry_file="bot.cpp", runtime_lang="cpp")
    assert "gcc:13-slim" in df
    assert "tcp_bridge.py" in df  # 含桥
    assert "--bot-cmd" in df
    assert "./bot_bin" in df  # 桥 spawn 二进制
    m = re.search(r"ENTRYPOINT\s+(\[.*\])", df)
    assert m
    arr = json.loads(m.group(1))
    assert all(isinstance(x, str) for x in arr), arr
    assert json.loads(arr[arr.index("--bot-cmd") + 1]) == ["./bot_bin"]


def test_dockerfile_java_tcp():
    """Java TCP bot:含桥代理 + bot-cmd 指向 java 命令。"""
    import json
    import re
    df = make_dockerfile(protocol="tcp", entry_file="Main.java", runtime_lang="java")
    assert "eclipse-temurin" in df
    assert "tcp_bridge.py" in df
    assert "--bot-cmd" in df
    assert "java" in df and "Main" in df
    m = re.search(r"ENTRYPOINT\s+(\[.*\])", df)
    assert m
    arr = json.loads(m.group(1))
    assert all(isinstance(x, str) for x in arr), arr
    assert json.loads(arr[arr.index("--bot-cmd") + 1]) == ["java", "Main"]


def test_dockerfile_unsupported_lang_still_errors():
    """不支持的运行时仍报错。"""
    with pytest.raises(BotBuildError) as e:
        make_dockerfile(protocol="json", entry_file="x.rb", runtime_lang="ruby")
    assert e.value.code == "unsupported_lang"


# ══════════════════════════════════════════════════════════
# 问题 1:orchestrator 实时更新 hands_played
# ══════════════════════════════════════════════════════════

def test_orchestrator_updates_hands_played_on_settle(tmp_path):
    """settle 事件应实时更新 matches.hands_played(问题1 修复验证)。

    用 mock event_sink 模拟:orchestrator 的 _make_event_sink 闭包收到
    settle 事件时调 update_match(hands_played=hand)。
    """
    from arena.backend.platform.runtime.orchestrator import MatchOrchestrator
    store = Store(str(tmp_path / "t.db"))
    # 建 bot + match
    u = store.create_user("user1", "user1@e.com", "!")
    ba = store.create_bot(u["id"], "BotA")
    bb = store.create_bot(u["id"], "BotB")
    # update_bot 设镜像(create_bot 无 docker_image 参数)
    store.update_bot(ba["id"], docker_image="img-a")
    store.update_bot(bb["id"], docker_image="img-b")
    store.create_match("m1", ba["id"], bb["id"], owner_id=u["id"],
                       protocol_a="json", protocol_b="json")

    # 用占位 runner(不实际跑,直接测 sink)
    class FakeRunner:
        async def cleanup_all(self): pass
    orch = MatchOrchestrator(store, FakeRunner())  # type: ignore
    sink = orch._make_event_sink("m1")

    # 初始 hands_played=0
    assert store.get_match("m1")["hands_played"] == 0

    # 模拟第 1 手 settle
    import asyncio
    asyncio.run(sink({"type": "settle", "hand": 1, "earnings": [100, -100]}))
    assert store.get_match("m1")["hands_played"] == 1, "settle 后 hands_played 应实时更新"

    # 第 2 手 settle
    asyncio.run(sink({"type": "settle", "hand": 2, "earnings": [-50, 50]}))
    assert store.get_match("m1")["hands_played"] == 2

    # 非 settle 事件不更新 hands_played
    asyncio.run(sink({"type": "action", "hand": 3, "action": "call"}))
    assert store.get_match("m1")["hands_played"] == 2  # 仍是 2


def test_orchestrator_hands_played_with_mock_match(tmp_path):
    """端到端:mock MatchRunner 跑完,hands_played 中途递增(问题1)。

    用一个简单的 mock runner 类,发 3 个 settle 事件。
    """
    from arena.backend.platform.runtime.orchestrator import MatchOrchestrator
    store = Store(str(tmp_path / "t.db"))
    u = store.create_user("user2", "user2@e.com", "!")
    ba = store.create_bot(u["id"], "A2")
    bb = store.create_bot(u["id"], "B2")
    store.update_bot(ba["id"], docker_image="img-a", is_active=1)
    store.update_bot(bb["id"], docker_image="img-b", is_active=1)

    # Mock MatchRunner:patch run_match 发 settle 事件
    import asyncio
    settle_events = [
        {"type": "settle", "hand": 1, "earnings": [100, -100]},
        {"type": "settle", "hand": 2, "earnings": [-50, 50]},
        {"type": "settle", "hand": 3, "earnings": [200, -200]},
    ]

    class MockMatchRunner:
        def __init__(self, *a, event_sink=None, **kw):
            self.sink = event_sink
        async def run_match(self, **kw):
            for e in settle_events:
                if self.sink:
                    r = self.sink(e)
                    if asyncio.iscoroutine(r):
                        await r
            return {"winner": 0, "earnings": [250, -250], "hands_played": 3, "events": []}

    orch = MatchOrchestrator(store, type("R", (), {"cleanup_all": lambda self: asyncio.sleep(0)})())  # type: ignore
    import arena.backend.platform.runtime.orchestrator as orch_mod
    orig = orch_mod.MatchRunner
    orch_mod.MatchRunner = MockMatchRunner  # type: ignore
    try:
        mid = asyncio.run(orch.challenge(
            challenger_bot_id=ba["id"], opponent_bot_id=bb["id"], owner_user_id=u["id"]))
        # 等后台 task(用 sleep 让它跑完)
        import time
        for _ in range(20):
            if store.get_match(mid)["status"] in ("completed", "aborted"):
                break
            time.sleep(0.1)
        m = store.get_match(mid)
        assert m["status"] == "completed"
        assert m["hands_played"] == 3  # 最终值
    finally:
        orch_mod.MatchRunner = orig  # type: ignore


# ══════════════════════════════════════════════════════════
# 问题 4:内置 bot 懒构建镜像
# ══════════════════════════════════════════════════════════

def test_orchestrator_lazy_build_builtin_without_docker(tmp_path, monkeypatch):
    """内置 bot 无镜像时,_require_playable_bot 调 bot_manager 懒构建。

    不实际 docker build(monkeypatch build_builtin_image 返回假镜像名)。
    """
    from arena.backend.platform.runtime.orchestrator import MatchOrchestrator
    store = Store(str(tmp_path / "t.db"))
    u = store.create_user("sys", "s@e.com", "!", role="admin")
    # 内置 bot,无镜像,有 source_path
    b = store.create_bot(u["id"], "builtin_test", protocol=PROTO_TCP,
                         entry_file="national_bot.py", is_builtin=True, is_public=True)
    store.update_bot(b["id"], source_path="/fake/path", docker_image="", is_active=1)

    class FakeBotManager:
        built = False
        def build_builtin_image(self, bid):
            type(self).built = True
            store.update_bot(bid, docker_image=f"arena-bot-{bid}:builtin")
            return f"arena-bot-{bid}:builtin"

    class FakeRunner:
        async def cleanup_all(self): pass

    orch = MatchOrchestrator(store, FakeRunner(), bot_manager=FakeBotManager())  # type: ignore
    # 无镜像 → 应触发懒构建
    result = orch._require_playable_bot(b["id"])
    assert FakeBotManager.built, "内置 bot 无镜像应触发懒构建"
    assert result["docker_image"] == f"arena-bot-{b['id']}:builtin"


def test_orchestrator_user_bot_no_image_errors(tmp_path):
    """用户上传的 bot(非内置)无镜像 → 报错(不懒构建)。"""
    from arena.backend.platform.runtime.orchestrator import MatchOrchestrator
    store = Store(str(tmp_path / "t.db"))
    u = store.create_user("user3", "user3@e.com", "!")
    b = store.create_bot(u["id"], "UserBot")  # 非内置,无镜像
    store.update_bot(b["id"], docker_image="", is_active=1)

    class FakeBotManager:
        def build_builtin_image(self, bid): raise AssertionError("不应调懒构建")

    class FakeRunner:
        async def cleanup_all(self): pass

    orch = MatchOrchestrator(store, FakeRunner(), bot_manager=FakeBotManager())  # type: ignore
    with pytest.raises(ValueError, match="尚未构建镜像"):
        orch._require_playable_bot(b["id"])


# ══════════════════════════════════════════════════════════
# 问题 2:create_match 填 protocol(challenge 流程已正确,补单测)
# ══════════════════════════════════════════════════════════

def test_create_match_stores_protocol(tmp_path):
    """store.create_match 传 protocol_a/b 应正确存入。"""
    store = Store(str(tmp_path / "t.db"))
    u = store.create_user("user4", "user4@e.com", "!")
    ba = store.create_bot(u["id"], "A4", protocol=PROTO_JSON)
    bb = store.create_bot(u["id"], "B4", protocol=PROTO_TCP)
    store.update_bot(ba["id"], is_active=1)
    store.update_bot(bb["id"], is_active=1)
    m = store.create_match("m_proto", ba["id"], bb["id"],
                           protocol_a="json", protocol_b="tcp")
    assert m["protocol_a"] == "json"
    assert m["protocol_b"] == "tcp"
    # 重新查也正确
    assert store.get_match("m_proto")["protocol_b"] == "tcp"
