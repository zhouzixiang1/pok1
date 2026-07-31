"""对战编排器(里程碑 5:对战系统 + 里程碑 6:排行榜/Glicko-2)。

``MatchOrchestrator`` 驱动一场对战完整生命周期:

1. ``challenge``:校验 bot(存在/上架/有镜像)→ ``create_match(pending)``
   → ``asyncio.create_task(_run_match_task)`` 异步跑,立即返回 ``match_id``。
2. ``_run_match_task``:``update_match(running)`` → 调 ``MatchRunner.run_match``
   (``event_sink`` 把每个事件同时 ``append_replay_event`` 入 DB +
   ``_broadcast`` SSE 扇出给前端)→ ``update_match(completed, earnings, winner,
   hands, ended_at)`` → ``_update_ratings`` (Glicko-2 双向 tanh 量级)→
   ``_update_pair_stats`` (bb/100 mean + 95% CI 重算)。
   任意环节异常 → ``update_match(aborted)`` + 广播错误事件,任务不向上抛
   (后台任务无人 await,抛了也丢)。
3. SSE 扇出复用 ``server/match_manager.py`` 的 ``asyncio.Queue`` 模式
   (``subscribe`` 返回 ``maxsize=512`` 的 Queue,``_broadcast`` ``put_nowait``,
   满则丢弃死订阅)。**事件附带 ``match_id``**,SSE 端点按 ``match_id`` 过滤,
   互不串台。
4. ``get_snapshot``:平台当前状态(idle/running、当前 ``match_id``、
   ``matches_played``、最近 30 事件),供 SSE 首帧 + ``/api/state``。

**关键事实**:
- ``MatchRunner.run_match`` 已是 async + ``event_sink`` 支持同步/异步回调,
  本编排器把 sink 实现为 async coroutine,内部串同步 DB + 异步 SSE 广播。
- ``glicko2.py`` 零改动复用:``update`` / ``score_tanh`` / ``bb_per_100`` /
  ``ci_normal`` 全部直接调。
- ``Store`` 提供 ``create_match`` / ``update_match`` / ``append_replay_event``
  / ``upsert_rating`` / ``leaderboard`` / ``list_matches`` / ``get_match``,
  本编排器不重写存储逻辑。

**线程安全**:orchestrator 在单 asyncio loop 内运行;``Store`` 自带
``threading.Lock`` 的短事务,DB 调用是毫秒级同步阻塞,在偶发写入场景可接受。
SSE 扇出纯 asyncio.Queue(同 loop 协程),无锁。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime
from typing import Any

from ...rating import (
    BIG_BLIND,
    DEFAULT_RATING,
    DEFAULT_RD,
    DEFAULT_VOL,
    bb_per_100,
    ci_normal,
    score_tanh,
    update as glicko2_update,
)
from ..store import (
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    Store,
)
from .docker_runner import DockerRunner
from .match_runner import MatchRunner

logger = logging.getLogger(__name__)

# SSE 队列上限(对齐 server/match_manager.SSE_QUEUE_MAXSIZE)
SSE_QUEUE_MAXSIZE = 512
# SSE 最近事件缓存(首帧 snapshot 用)
SNAPSHOT_RECENT_EVENTS = 30
# 默认单场手数(与 match_runner.HANDS_PER_MATCH 对齐)
DEFAULT_HANDS_PER_MATCH = 70


class MatchOrchestrator:
    """对战编排器:管理对战生命周期 + SSE 扇出 + 评分/对统计更新。

    用法::

        store = Store("arena_platform.db")
        runner = DockerRunner()
        orch = MatchOrchestrator(store, runner, hands_per_match=70)
        match_id = await orch.challenge(
            challenger_bot_id=1, opponent_bot_id=2, owner_user_id=10)
        # 前端 GET /api/matches/{match_id}/events 实时观赛
    """

    def __init__(
        self,
        store: Store,
        runner: DockerRunner,
        *,
        hands_per_match: int = DEFAULT_HANDS_PER_MATCH,
        action_timeout: float = 60.0,
        bot_manager: Any = None,
        max_concurrent: int = 2,
    ) -> None:
        self.store = store
        self.runner = runner
        self.hands_per_match = hands_per_match
        self.action_timeout = action_timeout
        # BotManager(可选):内置 bot 无镜像时懒构建用。
        # main.create_platform_app 注入。None 时内置 bot 无镜像则报错。
        self.bot_manager = bot_manager
        self.max_concurrent = max(1, int(max_concurrent))

        # SSE 订阅者(同 loop asyncio.Queue)
        self._subscribers: list[asyncio.Queue[str]] = []
        # 全局事件缓存(最近 N,首帧 snapshot + 排错用)
        self._event_log: list[dict[str, Any]] = []

        # 当前对战状态
        self._current_match_id: str | None = None
        self._matches_played = 0

        # 后台任务句柄(便于停服时 await/cancel)
        self._tasks: set[asyncio.Task] = set()

    # ══════════════════════════════════════════════════════════
    # 公开 API:发起对战
    # ══════════════════════════════════════════════════════════

    async def challenge(
        self,
        *,
        challenger_bot_id: int,
        opponent_bot_id: int,
        owner_user_id: int,
        match_type: str = "challenge",
    ) -> str:
        """发起一场对战。校验后建 ``pending`` 记录并立即异步启动,返回 ``match_id``。

        - 两个 bot 都须存在、``is_active`` 且有 ``docker_image``(没镜像会 400,
          提示先构建)。
        - ``match_id`` 用 ``secrets.token_urlsafe`` 生成(防遍历)。
        - 真正的对战在后台 task 跑,本方法立即返回。

        抛 ``ValueError`` 表示参数错误(调用方/路由层负责转 HTTP 4xx)。
        """
        if challenger_bot_id == opponent_bot_id:
            raise ValueError("不能与自己对战")

        # 并发上限:正在跑的后台任务数
        alive = {t for t in self._tasks if not t.done()}
        self._tasks = alive
        if len(alive) >= self.max_concurrent:
            raise ValueError(
                f"当前对战已满({self.max_concurrent} 场并发上限),请稍后再试")

        bot_a = self._require_playable_bot(challenger_bot_id)
        bot_b = self._require_playable_bot(opponent_bot_id)

        match_id = self._gen_match_id(bot_a["name"], bot_b["name"])
        self.store.create_match(
            match_id, bot_a["id"], bot_b["id"],
            owner_id=owner_user_id,
            total_hands=self.hands_per_match,
            match_type=match_type,
            protocol_a=bot_a["protocol"],
            protocol_b=bot_b["protocol"],
        )
        logger.info("challenge %s: %s(id=%d) vs %s(id=%d) owner=%d",
                    match_id, bot_a["name"], bot_a["id"],
                    bot_b["name"], bot_b["id"], owner_user_id)

        task = asyncio.create_task(
            self._run_match_task(match_id, bot_a, bot_b),
            name=f"match-{match_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return match_id

    # ══════════════════════════════════════════════════════════
    # 后台对战主流程
    # ══════════════════════════════════════════════════════════

    async def _run_match_task(
        self,
        match_id: str,
        bot_a: dict[str, Any],
        bot_b: dict[str, Any],
    ) -> None:
        """后台跑对战:更新状态 → 跑 MatchRunner → 写 DB + SSE → 评分。

        任意异常都吞掉(后台 task 无人 await),改为 ``update_match(aborted)``
        + 广播错误事件,保证不阻塞平台。
        """
        prev_match_id = self._current_match_id
        self._current_match_id = match_id
        started_at = datetime.now().isoformat(timespec="seconds")
        self.store.update_match(match_id, status=STATUS_RUNNING,
                                started_at=started_at)
        await self._broadcast({
            "type": "match_start",
            "match_id": match_id,
            "bot_a": _bot_brief(bot_a),
            "bot_b": _bot_brief(bot_b),
            "hands_per_match": self.hands_per_match,
            "started_at": started_at,
        })

        match = MatchRunner(
            self.runner,
            hands_per_match=self.hands_per_match,
            action_timeout=self.action_timeout,
            event_sink=self._make_event_sink(match_id),
        )
        try:
            result = await match.run_match(
                image_a=bot_a["docker_image"],
                image_b=bot_b["docker_image"],
                name_a=bot_a["name"],
                name_b=bot_b["name"],
            )
        except Exception as exc:
            logger.exception("match %s crashed", match_id)
            self.store.update_match(
                match_id, status=STATUS_ABORTED,
                ended_at=datetime.now().isoformat(timespec="seconds"),
                reason=f"{type(exc).__name__}: {exc}",
            )
            await self._broadcast({
                "type": "match_end", "match_id": match_id,
                "status": STATUS_ABORTED,
                "reason": f"{type(exc).__name__}: {exc}",
            })
            self._current_match_id = prev_match_id
            return

        earnings = list(result.get("earnings", [0, 0]))
        hands = int(result.get("hands_played", 0) or 0)
        winner = result.get("winner")
        ended_at = datetime.now().isoformat(timespec="seconds")
        net_bb_a = float(earnings[0]) / BIG_BLIND if earnings else 0.0
        aborted = bool(result.get("aborted"))
        if aborted:
            # bot 容器中途崩溃 / 通信不可恢复 → 整场中止,如实标 aborted(非 completed)
            final_status = STATUS_ABORTED
            reason = f"bot_session_dead:{result.get('abort_reason') or 'unknown'}"
        else:
            final_status = STATUS_COMPLETED
            reason = "completed"

        self.store.update_match(
            match_id,
            status=final_status,
            hands_played=hands,
            earnings_a=int(earnings[0]) if earnings else 0,
            earnings_b=int(earnings[1]) if len(earnings) > 1 else 0,
            winner=winner,
            reason=reason,
            net_bb_a=net_bb_a,
            ended_at=ended_at,
        )

        # 评分 + 对统计更新:仅正常完成的对局计入 Glicko-2(中止对局不评)。
        # 独立 try:评分失败不影响已完成对局记录。
        if not aborted:
            try:
                self._update_ratings(bot_a["id"], bot_b["id"], earnings)
                self._update_pair_stats(bot_a["id"], bot_b["id"])
            except Exception:
                logger.exception("match %s rating/pair_stats update failed", match_id)

        self._matches_played += 1
        self._current_match_id = prev_match_id
        await self._broadcast({
            "type": "match_end",
            "match_id": match_id,
            "status": final_status,
            "winner": winner,
            "earnings": earnings,
            "hands_played": hands,
            "reason": reason,
            "ended_at": ended_at,
        })
        logger.info("match %s %s: winner=%s earnings=%s hands=%d",
                    match_id, final_status, winner, earnings, hands)

    # ══════════════════════════════════════════════════════════
    # 事件 sink:写 DB replay + SSE 扇出
    # ══════════════════════════════════════════════════════════

    def _make_event_sink(self, match_id: str):
        """构造 event_sink(异步闭包):每个事件 → DB append + SSE 广播。

        MatchRunner 的 ``_emit`` 已支持同步/异步 sink(``iscoroutine`` 判定);
        这里用 async 实现,把同步 DB 调用 + 异步广播串起来。

        每手 ``settle`` 时实时更新 ``matches.hands_played``(修复「中途进度
        不刷新」问题:之前只在整场结束才写,前端轮询 ``/api/matches/{id}``
        看到的 hands_played 一直是 0)。settle 事件的 hand 字段 = 已完成手数。
        """
        async def sink(event: dict[str, Any]) -> None:
            # 事件注入 match_id(SSE 端点按它过滤)
            tagged = {"match_id": match_id, **event}
            # settle 事件:实时更新 hands_played + 最新筹码快照
            # (settle.hand = 当前手数,已完成手数 = 该值;手从 1 计数)
            if event.get("type") == "settle":
                hand = event.get("hand")
                if isinstance(hand, int) and hand > 0:
                    earnings = event.get("earnings")
                    try:
                        update_fields: dict[str, Any] = {"hands_played": hand}
                        # 同时刷最新累计筹码(settle.earnings 是本手净值,
                        # 这里只更新 hands_played;累计 earnings 仍由
                        # _run_match_task 在整场结束时写入,避免中途频繁写)
                        self.store.update_match(match_id, **update_fields)
                    except Exception:
                        logger.exception("update_match hands_played failed match=%s", match_id)
            # DB 持久化(同步,毫秒级)
            try:
                self.store.append_replay_event(match_id, tagged)
            except Exception:
                logger.exception("append_replay_event failed match=%s", match_id)
            # 缓存最近事件(供 snapshot)
            self._event_log.append(tagged)
            if len(self._event_log) > 1000:
                self._event_log = self._event_log[-500:]
            # SSE 扇出
            await self._broadcast(tagged)
        return sink

    # ══════════════════════════════════════════════════════════
    # Glicko-2 评分更新(双向,零和)
    # ══════════════════════════════════════════════════════════

    def _update_ratings(self, bot_a_id: int, bot_b_id: int,
                        earnings: list[int]) -> None:
        """Glicko-2 双向更新。

        - 分数用 ``score_tanh``(保留净筹码量级,零和:≈ 1-S)。
        - 胜负按 ``earnings`` 比较:正=赢、负=输、零=平。
        - ``net_chips`` 累加、``matches_played`` +1。
        - 新 bot 无评分记录时用默认 (1500, 350, 0.06)。
        """
        ra = self.store.get_rating(bot_a_id) or _default_rating()
        rb = self.store.get_rating(bot_b_id) or _default_rating()
        earn_a = int(earnings[0]) if earnings else 0
        earn_b = int(earnings[1]) if len(earnings) > 1 else 0
        score_a = score_tanh(earn_a)
        score_b = score_tanh(earn_b)

        nra = glicko2_update(ra["rating"], ra["rd"], ra["vol"],
                             rb["rating"], rb["rd"], score_a)
        nrb = glicko2_update(rb["rating"], rb["rd"], rb["vol"],
                             ra["rating"], ra["rd"], score_b)

        if earn_a > earn_b:
            wa, la, da = 1, 0, 0
        elif earn_b > earn_a:
            wa, la, da = 0, 1, 0
        else:
            wa, la, da = 0, 0, 1

        self.store.upsert_rating(
            bot_a_id, nra[0], nra[1], nra[2],
            wins=int(ra.get("wins", 0)) + wa,
            losses=int(ra.get("losses", 0)) + la,
            draws=int(ra.get("draws", 0)) + da,
            net_chips=int(ra.get("net_chips", 0)) + earn_a,
            matches_played=int(ra.get("matches_played", 0)) + 1,
        )
        self.store.upsert_rating(
            bot_b_id, nrb[0], nrb[1], nrb[2],
            wins=int(rb.get("wins", 0)) + la,    # A 赢则 B 输
            losses=int(rb.get("losses", 0)) + wa,
            draws=int(rb.get("draws", 0)) + da,
            net_chips=int(rb.get("net_chips", 0)) + earn_b,
            matches_played=int(rb.get("matches_played", 0)) + 1,
        )

    # ══════════════════════════════════════════════════════════
    # 对统计(bb/100 CI)重算
    # ══════════════════════════════════════════════════════════

    def _update_pair_stats(self, bot_a_id: int, bot_b_id: int) -> None:
        """重算该对(A 视角)bb/100 mean + 95% CI。

        扫描所有 ``bot_a vs bot_b`` 历史对局,无论谁先谁后都按 A 视角归一
        (A 是 ``bot_a_id`` 时用 ``earnings_a``,B 是 ``bot_a_id`` 时取反
        ``earnings_a``,因为视角对调)。
        """
        bbs: list[float] = []
        for m in self.store.list_matches(bot_id=bot_a_id, limit=5000):
            if m["bot_a_id"] == bot_a_id and m["bot_b_id"] == bot_b_id:
                # A 视角:earnings_a 即 A 净筹码
                bbs.append(bb_per_100(int(m["earnings_a"] or 0),
                                      int(m["hands_played"] or 0)))
            elif m["bot_a_id"] == bot_b_id and m["bot_b_id"] == bot_a_id:
                # 该场 A 是 bot_b,视角对调 → 取 earnings_b(A 的净筹码)
                bbs.append(bb_per_100(int(m["earnings_b"] or 0),
                                      int(m["hands_played"] or 0)))
        if not bbs:
            return
        mean, lo, hi = ci_normal(bbs)
        self.store.upsert_pair_stats(bot_a_id, bot_b_id,
                                     mean, lo, hi, len(bbs))

    # ══════════════════════════════════════════════════════════
    # SSE 订阅 / 扇出(对齐 server/match_manager 模式)
    # ══════════════════════════════════════════════════════════

    def subscribe(self) -> asyncio.Queue[str]:
        """订阅事件流。返回 ``maxsize=512`` 的 Queue,SSE 端点从它 ``get``。

        满了 ``_broadcast`` 会丢弃该订阅(判死)。
        """
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=SSE_QUEUE_MAXSIZE)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        """取消订阅(SSE 端点断开时调,防泄漏)。"""
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _broadcast(self, event: dict[str, Any]) -> None:
        """向所有订阅者 ``put_nowait`` 事件。满的订阅判死丢弃。

        复用 ``server/match_manager._broadcast`` 模式:遍历订阅,
        ``put_nowait`` 失败(QueueFull)→ 反向 pop 死订阅。
        """
        data = json_dumps(event)
        dead: list[int] = []
        for i, q in enumerate(self._subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(i)
        for i in reversed(dead):
            self._subscribers.pop(i)

    def get_snapshot(self) -> dict[str, Any]:
        """平台当前状态(SSE 首帧 + ``/api/state``)。"""
        return {
            "type": "snapshot",
            "status": "running" if self._current_match_id else "idle",
            "current_match_id": self._current_match_id,
            "matches_played": self._matches_played,
            "hands_per_match": self.hands_per_match,
            "recent_events": list(self._event_log[-SNAPSHOT_RECENT_EVENTS:]),
        }

    async def get_match_status(self, match_id: str) -> dict[str, Any]:
        """单场状态(SSE 首帧)。从 DB 读元数据 + replay 事件。

        不存在的 match 返回 ``{"match_id": ..., "status": "not_found"}``。
        """
        m = self.store.get_match(match_id)
        if m is None:
            return {"match_id": match_id, "status": "not_found"}
        replay = self.store.get_replay(match_id)
        events: list[dict[str, Any]] = []
        if replay and replay.get("events_json"):
            try:
                import json as _json
                events = _json.loads(replay["events_json"])
            except (ValueError, TypeError):
                events = []
        return {
            "type": "snapshot",
            "match_id": match_id,
            "status": m.get("status", STATUS_PENDING),
            "bot_a_id": m.get("bot_a_id"),
            "bot_b_id": m.get("bot_b_id"),
            "winner": m.get("winner"),
            "earnings": [m.get("earnings_a"), m.get("earnings_b")],
            "hands_played": m.get("hands_played", 0),
            "total_hands": m.get("total_hands", self.hands_per_match),
            "events": events,
        }

    # ══════════════════════════════════════════════════════════
    # 辅助
    # ══════════════════════════════════════════════════════════

    def _require_playable_bot(self, bot_id: int) -> dict[str, Any]:
        """校验 bot 可对战:存在 / 上架 / 有镜像。失败抛 ``ValueError``。

        **内置 bot 懒构建**:若 bot 是内置且无镜像但有 source_path,自动构建。
        用户上传的 bot 无镜像则报错(用户应通过上传流程构建)。
        """
        bot = self.store.get_bot(bot_id)
        if bot is None:
            raise ValueError(f"bot {bot_id} 不存在", bot_id)
        if not bot.get("is_active"):
            raise ValueError(f"bot {bot_id} 已下架", bot_id)
        if not bot.get("docker_image"):
            # 内置 bot 懒构建(首次 challenge 时自动建镜像)
            if bot.get("is_builtin") and bot.get("source_path") and self.bot_manager is not None:
                logger.info("builtin bot %s has no image, lazy-building...", bot["name"])
                try:
                    image = self.bot_manager.build_builtin_image(bot_id)
                    bot = self.store.get_bot(bot_id)  # 重新取(含新镜像)
                    logger.info("builtin bot %s image built: %s", bot["name"], image)
                except Exception as exc:
                    raise ValueError(
                        f"内置 bot {bot['name']} 镜像构建失败:{exc}") from exc
            else:
                raise ValueError(
                    f"bot {bot_id}({bot['name']})尚未构建镜像,请先上传/构建",
                    bot_id)
        return bot

    @staticmethod
    def _gen_match_id(name_a: str, name_b: str) -> str:
        """生成 match_id:``m-<token>-<a>_vs_<b>``。token 防遍历+去重。"""
        token = secrets.token_urlsafe(6)
        safe_a = _safe_name(name_a)[:24] or "a"
        safe_b = _safe_name(name_b)[:24] or "b"
        return f"m-{token}-{safe_a}_vs_{safe_b}"

    async def shutdown(self) -> None:
        """停服:等待后台任务结束(给 5s)。测试用。"""
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass


# ══════════════════════════════════════════════════════════
# 模块级辅助
# ══════════════════════════════════════════════════════════

def _default_rating() -> dict[str, Any]:
    """新 bot 默认评分(Glicko-2 初始)。"""
    return {
        "rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL,
        "wins": 0, "losses": 0, "draws": 0,
        "net_chips": 0, "matches_played": 0,
    }


def _bot_brief(bot: dict[str, Any]) -> dict[str, Any]:
    """bot 对外精简视图(避免泄露 source_path/docker_image 等)。"""
    return {
        "id": bot["id"],
        "name": bot["name"],
        "display_name": bot.get("display_name") or bot["name"],
        "protocol": bot.get("protocol", "json"),
    }


def _safe_name(s: str) -> str:
    """把 bot 名清理为 url-safe 片段(只留字母数字._-)。"""
    return "".join(c if c.isalnum() or c in "._-" else "_"
                   for c in (s or ""))


def json_dumps(event: dict[str, Any]) -> str:
    """统一 JSON 序列化(ensure_ascii=False,datetime 等转 str)。"""
    import json
    return json.dumps(event, ensure_ascii=False, default=str)
