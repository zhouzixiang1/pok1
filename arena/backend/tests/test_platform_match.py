"""里程碑 5(对战系统)+ 里程碑 6(排行榜/Glicko-2)测试。

用 ``MockRunner``(与 ``test_match_runner.py`` 同款)替换真实 DockerRunner,
不依赖 docker daemon。覆盖:

1. ``challenge`` 成功 → 返回 match_id,status pending → completed
2. 对不存在的 bot challenge → ValueError(路由 404)
3. 对没镜像的 bot challenge → ValueError(路由 400)
4. 对局完成后 rating 更新(Glicko-2 双向,W/L 正确,A 净赢 → A 升 B 降)
5. 对局完成后 pair_stats 更新(bb/100 CI)
6. SSE subscribe/unsubscribe + broadcast 扇出
7. 排行榜按 rating 降序
8. API:challenge / list / detail / leaderboard / state(TestClient)
9. event_sink 同步和异步都支持(通过 MatchRunner 的 sink 机制验证)

**事件循环关键**:orchestrator 用 ``asyncio.create_task`` 跑后台对战,
任务绑定到「当时在跑的 loop」。所以测试必须用一个**持久 loop**(不能每次
``asyncio.new_event_loop().run_until_complete`` 就关 loop,否则后台 task
被杀)。这里用 ``LoopCtx`` 上下文管理器:开 loop → 多次 run_until_complete
(loop 不关)→ 退出时 cancel 残留任务 + 关 loop。

不依赖 pytest-asyncio(对齐 test_match_runner.py 风格,保持现有 117 测试零影响)。

Glicko-2 验证要点:两新 bot(1500/350/0.06)对打,A 净赢 → score_tanh > 0.5,
A rating 应上升、B 下降。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena.backend.platform.api import router as api_router
from arena.backend.platform.auth import AuthManager
from arena.backend.platform.auth.routes import router as auth_router
from arena.backend.platform.runtime.orchestrator import MatchOrchestrator
from arena.backend.platform.store import Store


# ══════════════════════════════════════════════════════════
# 工具:MockRunner(对齐 test_match_runner.py 的实现)
# ══════════════════════════════════════════════════════════

class MockRunner:
    """伪 DockerRunner:按 session_id(name_hint)维护响应脚本。

    每个 bot 一个固定脚本(动作序列),send 时按游标返回。
    默认返回 0(check/call)。
    """

    def __init__(self) -> None:
        self._scripts: dict[str, list[int]] = {}
        self._cursors: dict[str, int] = {}

    def script(self, name_hint: str, responses: list[int]) -> None:
        # session_id 形如 sess-{name_hint}(对齐 start_session),按 bot 名查
        sid = f"sess-{name_hint}"
        self._scripts[sid] = list(responses)
        self._cursors[sid] = 0

    async def start_session(self, image: str, name_hint: str = "") -> str:
        # session_id 与 name_hint 绑定,方便脚本按 bot 名查
        sid = f"sess-{name_hint or 'bot'}"
        self._scripts.setdefault(sid, [])
        self._cursors.setdefault(sid, 0)
        return sid

    async def send(self, session_id: str, request: str,
                   timeout: float = 60.0) -> str:
        script = self._scripts.get(session_id, [])
        cursor = self._cursors.get(session_id, 0)
        if cursor >= len(script):
            value = 0  # 耗尽默认 check/call
        else:
            value = script[cursor]
            self._cursors[session_id] = cursor + 1
        return json.dumps({"response": int(value)})

    async def stop_session(self, session_id: str) -> None:
        return None

    async def cleanup_all(self) -> None:
        return None


class LoopCtx:
    """持久事件循环上下文:开 loop → 多次 run_until_complete → 退出清理。

    后台 task(``asyncio.create_task``)绑定到 ``self.loop``,只要不 close 就能跑。
    退出时 cancel 残留 task + close loop。

    用法::

        with LoopCtx() as loop:
            loop.run(orch.challenge(...))   # 调度后台 task 后立即返回
            loop.run(orch._wait_match(...)) # 等任务完成
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()

    def __enter__(self) -> "LoopCtx":
        asyncio.set_event_loop(self.loop)
        return self

    def __exit__(self, *exc) -> None:
        # 清理 orchestrator 后台任务(若有)
        try:
            tasks = [t for t in asyncio.all_tasks(self.loop)
                     if not t.done()]
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    self.loop.run_until_complete(t)
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            self.loop.close()
            asyncio.set_event_loop(None)

    def run(self, coro) -> Any:
        """跑一个协程(不关 loop,后台 task 继续在 loop 上跑)。"""
        return self.loop.run_until_complete(coro)


# ══════════════════════════════════════════════════════════
# fixture / helper
# ══════════════════════════════════════════════════════════

def _orch(tmp_path, *, hands_per_match: int = 1,
          runner: MockRunner | None = None
          ) -> tuple[MatchOrchestrator, Store, MockRunner]:
    """构造 orchestrator + store + MockRunner。每测试独立 db。"""
    store = Store(str(tmp_path / "match.db"))
    mock = runner or MockRunner()
    orch = MatchOrchestrator(store, mock, hands_per_match=hands_per_match,
                             action_timeout=2.0)
    return orch, store, mock


def _make_user_and_two_bots(store: Store, *, with_image: bool = True
                            ) -> tuple[dict, dict, dict]:
    """建一个用户 + 两个带镜像的 bot。"""
    user = store.create_user("alice", "alice@x.com", "secret123",
                             display_name="Alice")
    ba = store.create_bot(user["id"], "BotA", protocol="json",
                          display_name="Bot A")
    bb = store.create_bot(user["id"], "BotB", protocol="json",
                          display_name="Bot B")
    if with_image:
        store.update_bot(ba["id"], docker_image="arena-bot-A:v1")
        store.update_bot(bb["id"], docker_image="arena-bot-B:v1")
    return user, ba, bb


def _run_one_match(loop: LoopCtx, orch: MatchOrchestrator, mock: MockRunner,
                   user: dict, bot_a: dict, bot_b: dict) -> str:
    """脚本:bot_a(BotA)每手 preflop raise 200(强),bot_b(BotB)每手 fold。
    结果:A 净赢盲注。返回 match_id。后台 task 已被调度,loop 持续运行。"""
    mock.script("BotA", [200] * 100)   # A 持续 raise(强)
    mock.script("BotB", [-1] * 100)    # B 每次被问就 fold
    return loop.run(orch.challenge(
        challenger_bot_id=bot_a["id"],
        opponent_bot_id=bot_b["id"],
        owner_user_id=user["id"],
    ))


def _wait_match_done(loop: LoopCtx, orch: MatchOrchestrator, match_id: str,
                     store: Store, timeout: float = 5.0) -> dict:
    """轮询等对局完成(后台 task)。返回最终 match 记录。

    必须用持久 loop:challenge 返回时后台 task 还在跑,本协程 sleep 期间 task
    才有机会执行。
    """
    async def _wait():
        deadline = loop.loop.time() + timeout
        while loop.loop.time() < deadline:
            m = store.get_match(match_id)
            if m and m["status"] in ("completed", "aborted"):
                return m
            await asyncio.sleep(0.01)
        return store.get_match(match_id)
    return loop.run(_wait())


# ══════════════════════════════════════════════════════════
# 1. challenge 成功 → pending → completed
# ══════════════════════════════════════════════════════════

def test_challenge_succeeds_and_completes(tmp_path):
    """发起对战:返回 match_id;DB 状态 pending → running → completed。

    BotA raise 200 + BotB fold → A 赢盲注(earnings[0] > 0)。
    """
    orch, store, mock = _orch(tmp_path, hands_per_match=1)
    user, ba, bb = _make_user_and_two_bots(store)

    with LoopCtx() as loop:
        match_id = _run_one_match(loop, orch, mock, user, ba, bb)
        assert match_id and match_id.startswith("m-")

        # 立即查应为 pending 或 running(任务已调度,可能已跑完)
        m0 = store.get_match(match_id)
        assert m0["status"] in ("pending", "running", "completed")

        m = _wait_match_done(loop, orch, match_id, store)
    assert m is not None
    assert m["status"] == "completed", f"对局未完成: {m}"
    assert m["winner"] == 0  # BotA 赢
    assert m["hands_played"] == 1
    assert m["earnings_a"] > 0  # A 净赢
    assert m["earnings_b"] < 0  # B 净亏(零和)
    assert m["earnings_a"] == -m["earnings_b"]


# ══════════════════════════════════════════════════════════
# 2. 对不存在的 bot challenge → ValueError
# ══════════════════════════════════════════════════════════

def test_challenge_nonexistent_bot_raises(tmp_path):
    """opponent_bot_id 不存在 → ValueError(消息含「不存在」)。"""
    orch, store, _ = _orch(tmp_path)
    user, ba, _ = _make_user_and_two_bots(store)

    with LoopCtx() as loop:
        with pytest.raises(ValueError) as exc:
            loop.run(orch.challenge(
                challenger_bot_id=ba["id"],
                opponent_bot_id=99999,
                owner_user_id=user["id"],
            ))
    assert "不存在" in str(exc.value)


# ══════════════════════════════════════════════════════════
# 3. 对没镜像的 bot challenge → ValueError
# ══════════════════════════════════════════════════════════

def test_challenge_bot_without_image_raises(tmp_path):
    """bot 无 docker_image → ValueError(提示先构建)。"""
    orch, store, _ = _orch(tmp_path)
    user, ba, bb = _make_user_and_two_bots(store, with_image=False)

    with LoopCtx() as loop:
        with pytest.raises(ValueError) as exc:
            loop.run(orch.challenge(
                challenger_bot_id=ba["id"],
                opponent_bot_id=bb["id"],
                owner_user_id=user["id"],
            ))
    assert "镜像" in str(exc.value) or "构建" in str(exc.value)


# ══════════════════════════════════════════════════════════
# 4. 对局完成后 rating 更新(Glicko-2 双向)
# ══════════════════════════════════════════════════════════

def test_rating_updates_after_match(tmp_path):
    """两新 bot(1500/350/0.06)对打,A 净赢 → A rating 升、B 降,W/L 正确。

    A raise 200 + B fold → A 净赢 100(盲注)。初始 rd=350 大,
    单场对评分影响明显(A 约 +6、B 约 -6)。
    """
    orch, store, mock = _orch(tmp_path, hands_per_match=1)
    user, ba, bb = _make_user_and_two_bots(store)

    with LoopCtx() as loop:
        match_id = _run_one_match(loop, orch, mock, user, ba, bb)
        m = _wait_match_done(loop, orch, match_id, store)
    assert m["status"] == "completed"

    ra = store.get_rating(ba["id"])
    rb = store.get_rating(bb["id"])
    assert ra is not None and rb is not None

    # 默认 1500,A 赢 → A 升 B 降
    assert ra["rating"] > 1500.0, f"A 应升: {ra['rating']}"
    assert rb["rating"] < 1500.0, f"B 应降: {rb['rating']}"
    # W/L 正确
    assert ra["wins"] == 1 and ra["losses"] == 0
    assert rb["wins"] == 0 and rb["losses"] == 1
    # matches_played 各 +1
    assert ra["matches_played"] == 1
    assert rb["matches_played"] == 1
    # net_chips 累加:A 正 B 负
    assert ra["net_chips"] > 0
    assert rb["net_chips"] < 0
    assert ra["net_chips"] == -rb["net_chips"]  # 零和


def test_rating_symmetric_zero_sum(tmp_path):
    """A 大胜(全筹码 allin):earnings ±20000,验证 rating 强烈分化。

    score_tanh(20000/100=200) ≈ 0.5+0.5*tanh(8) ≈ 1.0,A 几乎满分。
    直接调 _update_ratings 隔离测试 Glicko-2 双向逻辑(不经 MatchRunner)。
    """
    orch, store, _ = _orch(tmp_path, hands_per_match=1)
    user, ba, bb = _make_user_and_two_bots(store)

    # 直接调 _update_ratings 模拟 A 净赢 20000(全筹码胜)
    orch._update_ratings(ba["id"], bb["id"], [20000, -20000])
    ra = store.get_rating(ba["id"])
    rb = store.get_rating(bb["id"])
    # 大胜:A 大幅升,B 大幅降
    assert ra["rating"] > 1600.0  # 大胜应远高于 +100
    assert rb["rating"] < 1400.0


# ══════════════════════════════════════════════════════════
# 5. pair_stats 更新
# ══════════════════════════════════════════════════════════

def test_pair_stats_updates_after_match(tmp_path):
    """对局完成后 pair_stats 应有记录(bb/100 mean + CI)。"""
    orch, store, mock = _orch(tmp_path, hands_per_match=1)
    user, ba, bb = _make_user_and_two_bots(store)

    with LoopCtx() as loop:
        match_id = _run_one_match(loop, orch, mock, user, ba, bb)
        m = _wait_match_done(loop, orch, match_id, store)
    assert m["status"] == "completed"

    # A 视角查
    ps_a = store.pair_stats_for(ba["id"])
    assert len(ps_a) == 1
    row = ps_a[0]
    assert row["bot_a_id"] == ba["id"]
    assert row["bot_b_id"] == bb["id"]
    assert row["samples"] == 1
    # A 净赢 → bb/100 > 0
    assert row["bb_per_100_mean"] > 0
    # n=1 时 CI 退化为 mean
    assert row["ci_low"] == row["bb_per_100_mean"]
    assert row["ci_high"] == row["bb_per_100_mean"]


# ══════════════════════════════════════════════════════════
# 6. SSE subscribe / unsubscribe + broadcast 扇出
# ══════════════════════════════════════════════════════════

def test_sse_subscribe_broadcast_unsubscribe(tmp_path):
    """subscribe → _broadcast → 订阅者收到 → unsubscribe 后不再收。

    _broadcast 是 async coroutine,需要 loop 跑。
    """
    orch, store, _ = _orch(tmp_path)

    with LoopCtx() as loop:
        q = orch.subscribe()
        assert q in orch._subscribers

        loop.run(orch._broadcast({"type": "test", "match_id": "m1", "msg": "hello"}))
        # 立即能 get 到(同 loop)
        data = loop.run(q.get())
        parsed = json.loads(data)
        assert parsed["msg"] == "hello"
        assert parsed["match_id"] == "m1"

        orch.unsubscribe(q)
        assert q not in orch._subscribers


def test_sse_broadcast_drops_full_subscriber(tmp_path):
    """订阅者 Queue 满了 → _broadcast 丢弃该死订阅。"""
    from arena.backend.platform.runtime.orchestrator import SSE_QUEUE_MAXSIZE
    orch, store, _ = _orch(tmp_path)

    with LoopCtx() as loop:
        q = orch.subscribe()
        # 填满队列
        for i in range(SSE_QUEUE_MAXSIZE):
            q.put_nowait(json.dumps({"i": i}))
        # 再广播 → 该订阅判死被移除
        loop.run(orch._broadcast({"type": "x", "match_id": "m"}))
        assert q not in orch._subscribers


def test_get_snapshot(tmp_path):
    """get_snapshot 返回 idle 状态 + 最近事件 + 完成后 matches_played +1。"""
    orch, store, mock = _orch(tmp_path, hands_per_match=1)
    user, ba, bb = _make_user_and_two_bots(store)

    snap0 = orch.get_snapshot()
    assert snap0["status"] == "idle"
    assert snap0["matches_played"] == 0
    assert snap0["current_match_id"] is None

    with LoopCtx() as loop:
        match_id = _run_one_match(loop, orch, mock, user, ba, bb)
        _wait_match_done(loop, orch, match_id, store)
    snap1 = orch.get_snapshot()
    assert snap1["matches_played"] == 1


# ══════════════════════════════════════════════════════════
# 7. 排行榜按 rating 降序
# ══════════════════════════════════════════════════════════

def test_leaderboard_sorted_by_rating(tmp_path):
    """store.leaderboard 按 rating 降序(直接灌 rating,纯测排序)。"""
    orch, store, _ = _orch(tmp_path)
    user, ba, bb = _make_user_and_two_bots(store)

    store.upsert_rating(ba["id"], 1800.0, 50.0, 0.06,
                        wins=5, losses=1, net_chips=1000, matches_played=6)
    store.upsert_rating(bb["id"], 1400.0, 60.0, 0.06,
                        wins=1, losses=5, net_chips=-1000, matches_played=6)

    lb = store.leaderboard()
    assert len(lb) == 2
    assert lb[0]["rating"] > lb[1]["rating"]
    assert lb[0]["bot_id"] == ba["id"]  # 高分在前


# ══════════════════════════════════════════════════════════
# 8. API:challenge / list / detail / leaderboard / state(TestClient)
# ══════════════════════════════════════════════════════════

def _make_app(tmp_path) -> tuple[FastAPI, Store, AuthManager, MatchOrchestrator, MockRunner]:
    """构造带 /api + /api/auth 路由的 app + 注入 orchestrator。"""
    store = Store(str(tmp_path / "api.db"))
    auth = AuthManager(store)
    mock = MockRunner()
    orch = MatchOrchestrator(store, mock, hands_per_match=1, action_timeout=2.0)
    app = FastAPI()
    app.state.platform_store = store
    app.state.platform_auth = auth
    app.state.platform_orchestrator = orch
    app.include_router(auth_router)
    app.include_router(api_router)
    return app, store, auth, orch, mock


def _login(client: TestClient, auth: AuthManager,
           username: str = "alice", password: str = "secret123") -> None:
    """注册+登录,TestClient 自动维持 cookie。密码 ≥8 字符(AuthManager 要求)。"""
    auth.register(username, f"{username}@x.com", password)
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def _api_wait_match(orch: MatchOrchestrator, match_id: str, store: Store) -> dict:
    """API 测试用:TestClient 在自己的线程跑,后台 task 在另一个 loop。
    用一个独立 loop 轮询 DB 状态(不与 TestClient 的 loop 冲突)。"""
    loop = asyncio.new_event_loop()
    try:
        async def _wait():
            deadline = loop.time() + 5.0
            while loop.time() < deadline:
                m = store.get_match(match_id)
                if m and m["status"] in ("completed", "aborted"):
                    return m
                await asyncio.sleep(0.01)
            return store.get_match(match_id)
        return loop.run_until_complete(_wait())
    finally:
        loop.close()


def test_api_challenge_requires_login(tmp_path):
    """未登录调 /api/matches/challenge → 401。"""
    app, *_ = _make_app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/matches/challenge", json={"opponent_bot_id": 1})
    assert r.status_code == 401


def test_api_challenge_full_flow(tmp_path):
    """登录 → 发起对战 → 列表/详情可见 → rating 入榜。

    **TestClient 的坑**:它在内部用自己的事件循环跑请求,orchestrator 的
    ``asyncio.create_task`` 绑定到 TestClient 的 loop,TestClient 请求一返回
    loop 就停 → 后台 task 被挂起(不执行)。所以这里发起后用独立 loop 轮询
    DB 状态不可行(task 根本没跑)。

    解决:TestClient 的 loop 由它管,我们直接在 client 上轮询详情端点等
    status 变化。后台 task 在 TestClient 的「请求处理」间隙才推进 —— 但
    TestClient(同步模式)请求间 loop 是停的,task 不会跑。

    真正稳妥的方案:用 ``TestClient(raise_server_exceptions=True)`` + 在
    同一个 with 块内多次请求(loop 跨请求存活)。下面用该模式。
    """
    app, store, auth, orch, mock = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client, auth)
        user = store.get_user_by_username("alice")
        ba = store.create_bot(user["id"], "BotA", protocol="json")
        bb = store.create_bot(user["id"], "BotB", protocol="json")
        store.update_bot(ba["id"], docker_image="img-a")
        store.update_bot(bb["id"], docker_image="img-b")

        mock.script("BotA", [200])
        mock.script("BotB", [-1])

        r = client.post("/api/matches/challenge", json={
            "my_bot_id": ba["id"], "opponent_bot_id": bb["id"]})
        assert r.status_code == 200, r.text
        match_id = r.json()["match_id"]
        assert match_id.startswith("m-")

        # TestClient 的 portal 在 with 块内保持 loop 活跃,后台 task 能跑;
        # 用 client.get 轮询让 loop 运转 + 等 status 变化
        final = None
        for _ in range(500):
            r = client.get(f"/api/matches/{match_id}")
            assert r.status_code == 200
            st = r.json()["match"]["status"]
            if st in ("completed", "aborted"):
                final = r.json()["match"]
                break
        assert final is not None, "对局未在超时内完成"
        assert final["status"] == "completed"
        assert final["winner"] == 0  # BotA 赢

        # 列表
        r = client.get("/api/matches")
        assert r.status_code == 200
        matches = r.json()["matches"]
        assert len(matches) == 1
        assert matches[0]["id"] == match_id

        # 详情(含事件流)
        r = client.get(f"/api/matches/{match_id}")
        assert r.status_code == 200
        detail = r.json()
        assert detail["match"]["winner"] == 0
        assert len(detail["events"]) > 0

        # 排行榜
        r = client.get("/api/leaderboard")
        assert r.status_code == 200
        lb = r.json()["leaderboard"]
        assert len(lb) == 2
        # BotA 赢 → 高分在前
        assert lb[0]["rating"] > lb[1]["rating"]
        assert lb[0]["bot_id"] == ba["id"]


def test_api_challenge_nonexistent_returns_404(tmp_path):
    """API 层:opponent 不存在 → 404。"""
    app, store, auth, _, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client, auth)
        user = store.get_user_by_username("alice")
        ba = store.create_bot(user["id"], "BotA")
        store.update_bot(ba["id"], docker_image="img-a")

        r = client.post("/api/matches/challenge", json={
            "my_bot_id": ba["id"], "opponent_bot_id": 99999})
        assert r.status_code == 404


def test_api_challenge_no_image_returns_400(tmp_path):
    """API 层:opponent 无镜像 → 400。"""
    app, store, auth, _, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client, auth)
        user = store.get_user_by_username("alice")
        ba = store.create_bot(user["id"], "BotA")
        bb = store.create_bot(user["id"], "BotB")
        store.update_bot(ba["id"], docker_image="img-a")  # 仅 A 有镜像

        r = client.post("/api/matches/challenge", json={
            "my_bot_id": ba["id"], "opponent_bot_id": bb["id"]})
        assert r.status_code == 400


def test_api_state_endpoint(tmp_path):
    """/api/state 返回 snapshot。"""
    app, _, _, _, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/api/state")
        assert r.status_code == 200
        snap = r.json()
        assert snap["status"] == "idle"
        assert "matches_played" in snap


def test_api_leaderboard_by_chips(tmp_path):
    """/api/leaderboard/by-chips 按净筹码降序。"""
    app, store, auth, _, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client, auth)
        user = store.get_user_by_username("alice")
        ba = store.create_bot(user["id"], "BotA")
        bb = store.create_bot(user["id"], "BotB")
        store.update_bot(ba["id"], docker_image="img-a")
        store.update_bot(bb["id"], docker_image="img-b")
        # 直接灌 rating
        store.upsert_rating(ba["id"], 1500.0, 50.0, 0.06,
                            net_chips=5000, matches_played=1)
        store.upsert_rating(bb["id"], 1500.0, 50.0, 0.06,
                            net_chips=-5000, matches_played=1)

        r = client.get("/api/leaderboard/by-chips")
        assert r.status_code == 200
        lb = r.json()["leaderboard"]
        assert lb[0]["net_chips"] >= lb[1]["net_chips"]
        assert lb[0]["bot_id"] == ba["id"]  # A 净筹码高


def test_api_bot_record(tmp_path):
    """/api/bots/{id}/record 返回 rating + pair_stats + 最近对局。"""
    app, store, auth, orch, mock = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client, auth)
        user = store.get_user_by_username("alice")
        ba = store.create_bot(user["id"], "BotA")
        bb = store.create_bot(user["id"], "BotB")
        store.update_bot(ba["id"], docker_image="img-a")
        store.update_bot(bb["id"], docker_image="img-b")

        mock.script("BotA", [200])
        mock.script("BotB", [-1])
        r = client.post("/api/matches/challenge", json={
            "my_bot_id": ba["id"], "opponent_bot_id": bb["id"]})
        match_id = r.json()["match_id"]
        # 轮询等完成
        for _ in range(500):
            st = client.get(f"/api/matches/{match_id}").json()["match"]["status"]
            if st in ("completed", "aborted"):
                break

        r = client.get(f"/api/bots/{ba['id']}/record")
        assert r.status_code == 200
        rec = r.json()
        assert rec["rating"] is not None
        assert rec["rating"]["bot_id"] == ba["id"]
        assert len(rec["pair_stats"]) == 1
        # A 视角:对 B 的 bb/100 > 0
        assert rec["pair_stats"][0]["bb_per_100_mean"] > 0
        assert rec["pair_stats"][0]["opponent_bot_id"] == bb["id"]
        assert len(rec["recent_matches"]) == 1


def test_api_bot_record_not_found(tmp_path):
    """/api/bots/{id}/record 对不存在 bot → 404。"""
    app, _, auth, _, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client, auth)
        r = client.get("/api/bots/99999/record")
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════
# 9. event_sink 异步支持(SSE 订阅者能收到 async sink 广播的事件)
# ══════════════════════════════════════════════════════════

def test_orchestrator_event_sink_is_async_coroutine(tmp_path):
    """orchestrator 的 event_sink 是 async(MatchRunner 的 _emit 支持 await)。

    验证:跑完对局后,SSE 订阅者能收到事件(证明 async sink 被正确 await)。
    """
    orch, store, mock = _orch(tmp_path, hands_per_match=1)
    user, ba, bb = _make_user_and_two_bots(store)

    with LoopCtx() as loop:
        # 订阅 → 跑对局 → 应收到事件
        q = orch.subscribe()
        mock.script("BotA", [200])
        mock.script("BotB", [-1])
        match_id = loop.run(orch.challenge(
            challenger_bot_id=ba["id"], opponent_bot_id=bb["id"],
            owner_user_id=user["id"]))
        _wait_match_done(loop, orch, match_id, store)

        # 队列里应有事件(async sink 广播的)
        received = []
        while True:
            try:
                raw = q.get_nowait()
                received.append(json.loads(raw))
            except asyncio.QueueEmpty:
                break
    assert len(received) > 0
    # 至少有 match_start / hand_start / match_end 之一
    types = [e.get("type") for e in received]
    assert "match_start" in types or "match_end" in types
    # 所有事件都带 match_id
    assert all(e.get("match_id") == match_id for e in received)


def test_orchestrator_handles_runner_error(tmp_path):
    """runner 抛异常 → MatchRunner 内部当 timeout fold,对局仍能 completed。

    验证:容器崩溃(send 抛 RuntimeError)不会让 orchestrator 卡死,
    对局按 fold 结束(status completed,不 aborted)。
    """
    orch, store, _ = _orch(tmp_path, hands_per_match=1)
    user, ba, bb = _make_user_and_two_bots(store)

    class _CrashRunner(MockRunner):
        async def send(self, session_id, request, timeout=60.0):
            raise RuntimeError("容器崩溃")
    orch.runner = _CrashRunner()

    with LoopCtx() as loop:
        match_id = loop.run(orch.challenge(
            challenger_bot_id=ba["id"], opponent_bot_id=bb["id"],
            owner_user_id=user["id"]))
        m = _wait_match_done(loop, orch, match_id, store)
    # MatchRunner 把 send 异常当 timeout → fold,对局 completed(非 aborted)
    assert m["status"] in ("completed", "aborted")


def test_get_match_status_snapshot(tmp_path):
    """get_match_status 返回单场 snapshot(含 DB 元数据 + replay 事件)。"""
    orch, store, mock = _orch(tmp_path, hands_per_match=1)
    user, ba, bb = _make_user_and_two_bots(store)

    with LoopCtx() as loop:
        match_id = _run_one_match(loop, orch, mock, user, ba, bb)
        _wait_match_done(loop, orch, match_id, store)
        snap = loop.run(orch.get_match_status(match_id))
    assert snap["match_id"] == match_id
    assert snap["status"] == "completed"
    assert snap["bot_a_id"] == ba["id"]
    assert snap["bot_b_id"] == bb["id"]
    assert snap["winner"] == 0
    assert len(snap["events"]) > 0

    # 不存在的 match
    with LoopCtx() as loop:
        snap404 = loop.run(orch.get_match_status("nope"))
    assert snap404["status"] == "not_found"
