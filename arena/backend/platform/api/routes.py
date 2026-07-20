"""对战 + 排行榜 API 路由(里程碑 5 + 6)。

挂载到主 app 的 ``/api`` 前缀(里程碑 8 的 main.py 集成)。

端点:
- POST /api/matches/challenge      发起对战(选对手)。需登录。
- GET  /api/matches                对局列表(分页 + 按 owner/bot/status 筛选)
- GET  /api/matches/{id}           对局详情(元数据 + replay events)
- GET  /api/matches/{id}/replay    对局回放(逐手快照 + 原始事件,里程碑 7)
- GET  /api/matches/{id}/replay/hands  逐手快照(轻量,回放器首屏)
- GET  /api/matches/{id}/replay/step   逐步回放(查某 step 的中间状态)
- GET  /api/matches/{id}/events    SSE 实时观赛(首帧 snapshot + 事件流 + keepalive)
- GET  /api/state                  平台当前状态(snapshot)
- GET  /api/leaderboard            天梯(按 rating 降序)
- GET  /api/leaderboard/by-chips   副榜(按净筹码降序)
- GET  /api/bots/{id}/record       某 bot 战绩(rating + 对各对手 bb/100 + 最近对局)

**SSE 关键**(参考 ``server/main.py:arena_events``):
- ``StreamingResponse`` + ``media_type="text/event-stream"``
- 首帧 ``data: <snapshot>\\n\\n``;循环里按 ``match_id`` 过滤事件(只推该对局)
- 15s 无事件 → ``": keepalive\\n\\n"`` 维持连接
- ``request.is_disconnected()`` 检测断开 → ``unsubscribe`` 防泄漏
- ``finally`` 兜底 unsubscribe(任何路径都清订阅)

orchestrator 在 ``request.app.state.platform_orchestrator`` 注入(main.py 启动时);
store 在 ``request.app.state.platform_store``(或从 orchestrator.store 取)。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import require_user
from ..runtime.orchestrator import MatchOrchestrator
from ..runtime.replay import build_hand_snapshots, snapshot_at_step

logger = logging.getLogger(__name__)

# SSE 配置(对齐 server/match_manager.SSE_KEEPALIVE_SEC)
SSE_KEEPALIVE_SEC = 15.0

router = APIRouter(prefix="/api", tags=["matches"])


# ══════════════════════════════════════════════════════════
# 依赖注入
# ══════════════════════════════════════════════════════════

def _get_orchestrator(request: Request) -> MatchOrchestrator:
    """从 app.state 取 MatchOrchestrator(里程碑 8 main.py 注入)。"""
    orch = getattr(request.app.state, "platform_orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="对战编排器未启用")
    return orch


# ══════════════════════════════════════════════════════════
# 请求模型
# ══════════════════════════════════════════════════════════

class ChallengeReq(BaseModel):
    """发起对战请求。``my_bot_id`` 缺省时取该用户第一个上架 bot。"""
    opponent_bot_id: int
    my_bot_id: int | None = None
    match_type: str = Field("challenge", max_length=32)


# ══════════════════════════════════════════════════════════
# POST /api/matches/challenge — 发起对战
# ══════════════════════════════════════════════════════════

@router.post("/matches/challenge")
async def challenge(req: ChallengeReq, request: Request,
                    user: dict = Depends(require_user)) -> JSONResponse:
    """发起一场对战。需登录。

    - ``my_bot_id`` 缺省时,自动从该用户的 bot 中挑一个上架且公开的。
    - 校验失败(不存在/下架/无镜像)→ 400/404。
    - 成功返回 ``{"match_id": ..., "status": "pending"}``,对战在后台异步跑,
      前端可立即连 ``/api/matches/{id}/events`` 观赛。
    """
    orch = _get_orchestrator(request)
    store = orch.store

    # 解析 my_bot_id
    my_bot_id = req.my_bot_id
    if my_bot_id is None:
        mine = store.list_bots(owner_id=user["id"], active_only=True,
                               include_builtin=False)
        if not mine:
            raise HTTPException(status_code=400,
                                detail="你还没有上架的 bot,请先上传一个")
        my_bot_id = mine[0]["id"]

    # 调用方校验:my_bot 须属于当前用户(防越权用别人的 bot 发起)
    my_bot = store.get_bot(my_bot_id)
    if my_bot is None:
        raise HTTPException(status_code=404, detail="bot 不存在")
    if my_bot["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权使用该 bot")

    try:
        match_id = await orch.challenge(
            challenger_bot_id=my_bot_id,
            opponent_bot_id=req.opponent_bot_id,
            owner_user_id=user["id"],
            match_type=req.match_type,
        )
    except ValueError as exc:
        # ValueError 可能带多 args:(message, bot_id) 或仅 message
        msg = exc.args[0] if exc.args else str(exc)
        # bot 不存在 → 404;其余(下架/无镜像/自打自)→ 400
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return JSONResponse({"match_id": match_id, "status": "pending"})


# ══════════════════════════════════════════════════════════
# GET /api/matches — 对局列表(分页 + 筛选)
# ══════════════════════════════════════════════════════════

@router.get("/matches")
async def list_matches(request: Request,
                       owner_id: int | None = Query(None),
                       bot_id: int | None = Query(None),
                       status: str | None = Query(None),
                       limit: int = Query(50, ge=1, le=500),
                       offset: int = Query(0, ge=0),
                       user: dict = Depends(require_user)) -> JSONResponse:
    """对局列表。支持 owner/bot/status 筛选 + 分页。"""
    orch = _get_orchestrator(request)
    matches = orch.store.list_matches(
        owner_id=owner_id, bot_id=bot_id, status=status,
        limit=limit, offset=offset)
    total = orch.store.count_matches(owner_id=owner_id, bot_id=bot_id,
                                     status=status)
    return JSONResponse({"matches": [_match_view(m) for m in matches],
                         "total": total, "limit": limit, "offset": offset})


# ══════════════════════════════════════════════════════════
# GET /api/matches/{id} — 对局详情
# ══════════════════════════════════════════════════════════

@router.get("/matches/{match_id}")
async def match_detail(match_id: str, request: Request,
                       user: dict = Depends(require_user)) -> JSONResponse:
    """对局详情:match 元数据 + replay 事件流。"""
    orch = _get_orchestrator(request)
    m = orch.store.get_match(match_id)
    if m is None:
        raise HTTPException(status_code=404, detail="对局不存在")
    replay = orch.store.get_replay(match_id)
    events: list[dict[str, Any]] = []
    if replay and replay.get("events_json"):
        try:
            events = json.loads(replay["events_json"])
        except (ValueError, TypeError):
            events = []
    return JSONResponse({"match": _match_view(m), "events": events})


# ══════════════════════════════════════════════════════════
# GET /api/matches/{id}/replay — 对局回放(逐手快照 + 原始事件)
# ══════════════════════════════════════════════════════════

@router.get("/matches/{match_id}/replay")
async def match_replay(match_id: str, request: Request,
                       user: dict = Depends(require_user)) -> JSONResponse:
    """对局回放数据。

    返回::

        {
          "match": {...},          # 对局元数据(_match_view)
          "snapshots": [...],      # 逐手快照(build_hand_snapshots)
          "events": [...]          # 原始事件流(供逐步回放)
        }

    前端回放器用 ``snapshots`` 按「手」切换、用 ``events`` 按「步」推进。
    match 不存在或无 replay 记录 → 404。
    """
    orch = _get_orchestrator(request)
    m = orch.store.get_match(match_id)
    if m is None:
        raise HTTPException(status_code=404, detail="对局不存在")
    events = _load_replay_events(orch.store, match_id)
    if events is None:
        raise HTTPException(status_code=404, detail="该对局尚无回放数据")
    snapshots = build_hand_snapshots(events)
    return JSONResponse({
        "match": _match_view(m),
        "snapshots": snapshots,
        "events": events,
    })


# ══════════════════════════════════════════════════════════
# GET /api/matches/{id}/replay/hands — 逐手快照(轻量,回放器首屏)
# ══════════════════════════════════════════════════════════

@router.get("/matches/{match_id}/replay/hands")
async def match_replay_hands(match_id: str, request: Request,
                             user: dict = Depends(require_user)) -> JSONResponse:
    """逐手快照列表(轻量,不含原始事件)。

    回放器首屏用——前端拿到快照序列即可渲染手数列表 / 大纲导航,按需再
    拉 ``/replay`` 拿完整事件做逐步回放。

    返回 ``{"match_id": ..., "snapshots": [...], "hand_count": N}``。
    """
    orch = _get_orchestrator(request)
    m = orch.store.get_match(match_id)
    if m is None:
        raise HTTPException(status_code=404, detail="对局不存在")
    events = _load_replay_events(orch.store, match_id)
    if events is None:
        raise HTTPException(status_code=404, detail="该对局尚无回放数据")
    snapshots = build_hand_snapshots(events)
    return JSONResponse({
        "match_id": match_id,
        "snapshots": snapshots,
        "hand_count": len(snapshots),
    })


# ══════════════════════════════════════════════════════════
# GET /api/matches/{id}/replay/hands — 逐步回放(查询某 step 的中间状态)
# ══════════════════════════════════════════════════════════

@router.get("/matches/{match_id}/replay/step")
async def match_replay_step(match_id: str, request: Request,
                            step: int = Query(0, ge=0),
                            user: dict = Depends(require_user)) -> JSONResponse:
    """查逐步回放的某个中间状态(``snapshot_at_step``)。

    Query 参数 ``step`` 是事件索引(0..len)。前端进度条 / 上一步下一步
    时用,定位到某个中间状态。

    返回 ``{"match_id": ..., **snapshot_at_step(events, step)}``。
    """
    orch = _get_orchestrator(request)
    m = orch.store.get_match(match_id)
    if m is None:
        raise HTTPException(status_code=404, detail="对局不存在")
    events = _load_replay_events(orch.store, match_id)
    if events is None:
        raise HTTPException(status_code=404, detail="该对局尚无回放数据")
    snap = snapshot_at_step(events, step)
    return JSONResponse({"match_id": match_id, **snap})


# ══════════════════════════════════════════════════════════
# GET /api/matches/{id}/events — SSE 实时观赛
# ══════════════════════════════════════════════════════════

@router.get("/matches/{match_id}/events")
async def match_events(match_id: str, request: Request) -> StreamingResponse:
    """SSE 实时观赛。

    - 首帧 ``data: <match snapshot>\\n\\n``(含已落盘事件,接续观看不丢历史)。
    - 之后按 ``match_id`` 过滤 orchestrator 广播的事件(只推该对局)。
    - 15s 无事件 → ``: keepalive\\n\\n`` 维持连接。
    - 客户端断开(``is_disconnected``)或取消则退出,``finally`` unsubscribe。

    公开端点(不强制登录):观赛不应阻塞未登录用户。
    """
    orch = _get_orchestrator(request)
    queue = orch.subscribe()
    snapshot = await orch.get_match_status(match_id)

    async def gen():
        try:
            yield f"data: {json.dumps(snapshot, ensure_ascii=False, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    raw = await asyncio.wait_for(
                        queue.get(), timeout=SSE_KEEPALIVE_SEC)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                # 按 match_id 过滤:非本对局事件跳过
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    event = {}
                if event.get("match_id") != match_id:
                    continue
                yield f"data: {raw}\n\n"
        finally:
            orch.unsubscribe(queue)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ══════════════════════════════════════════════════════════
# GET /api/state — 平台状态
# ══════════════════════════════════════════════════════════

@router.get("/state")
async def get_state(request: Request) -> JSONResponse:
    """当前平台状态(snapshot)。"""
    orch = _get_orchestrator(request)
    return JSONResponse(orch.get_snapshot())


# ══════════════════════════════════════════════════════════
# GET /api/leaderboard — 天梯(按 rating 降序)
# ══════════════════════════════════════════════════════════

@router.get("/leaderboard")
async def leaderboard(request: Request,
                      limit: int = Query(50, ge=1, le=500),
                      user: dict = Depends(require_user)) -> JSONResponse:
    """天梯:按 Glicko-2 rating 降序。"""
    orch = _get_orchestrator(request)
    rows = orch.store.leaderboard(limit)
    return JSONResponse({"leaderboard": [_rating_view(r) for r in rows]})


# ══════════════════════════════════════════════════════════
# GET /api/leaderboard/by-chips — 副榜(按净筹码降序)
# ══════════════════════════════════════════════════════════

@router.get("/leaderboard/by-chips")
async def leaderboard_by_chips(request: Request,
                               limit: int = Query(50, ge=1, le=500),
                               user: dict = Depends(require_user)) -> JSONResponse:
    """副榜:按净筹码(net_chips)降序。

    从 ``leaderboard()`` 拉全量后内存排序(数据量小,无需额外索引)。
    """
    orch = _get_orchestrator(request)
    rows = orch.store.leaderboard(500)
    rows_sorted = sorted(rows, key=lambda r: int(r.get("net_chips", 0)),
                         reverse=True)[:limit]
    return JSONResponse({"leaderboard": [_rating_view(r) for r in rows_sorted]})


# ══════════════════════════════════════════════════════════
# GET /api/bots/{id}/record — 某 bot 战绩
# ══════════════════════════════════════════════════════════

@router.get("/bots/{bot_id}/record")
async def bot_record(bot_id: int, request: Request,
                     user: dict = Depends(require_user)) -> JSONResponse:
    """某 bot 战绩:rating + 对各对手 bb/100 + 最近对局。"""
    orch = _get_orchestrator(request)
    store = orch.store
    bot = store.get_bot(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="bot 不存在")
    rating = store.get_rating(bot_id)
    pair_stats = store.pair_stats_for(bot_id)
    recent = store.list_matches(bot_id=bot_id, limit=20)
    return JSONResponse({
        "bot": _bot_brief_with_owner(bot),
        "rating": _rating_view(rating) if rating else None,
        "pair_stats": [_pair_stats_view(ps, bot_id) for ps in pair_stats],
        "recent_matches": [_match_view(m) for m in recent],
    })


# ══════════════════════════════════════════════════════════
# 视图辅助(对外脱敏 + 计算字段)
# ══════════════════════════════════════════════════════════

def _load_replay_events(store, match_id: str) -> list[dict[str, Any]] | None:
    """从 match_replays.events_json 加载事件流。

    - 没有 match_replays 记录 → 返回 ``None``(路由层据此 404)。
    - events_json 解析失败 → 返回空列表(空回放,非 404)。
    """
    replay = store.get_replay(match_id)
    if replay is None:
        return None
    if not replay.get("events_json"):
        return []
    try:
        events = json.loads(replay["events_json"])
    except (ValueError, TypeError):
        return []
    if not isinstance(events, list):
        return []
    return events


def _match_view(m: dict[str, Any]) -> dict[str, Any]:
    """match 记录对外视图:保留元数据,去内部杂项。"""
    return {
        "id": m.get("id"),
        "bot_a_id": m.get("bot_a_id"),
        "bot_b_id": m.get("bot_b_id"),
        "bot_a_name": m.get("bot_a_name"),
        "bot_b_name": m.get("bot_b_name"),
        "bot_a_display": m.get("bot_a_display"),
        "bot_b_display": m.get("bot_b_display"),
        "owner_id": m.get("owner_id"),
        "status": m.get("status"),
        "match_type": m.get("match_type"),
        "total_hands": m.get("total_hands"),
        "hands_played": m.get("hands_played"),
        "earnings_a": m.get("earnings_a"),
        "earnings_b": m.get("earnings_b"),
        "winner": m.get("winner"),
        "reason": m.get("reason"),
        "started_at": m.get("started_at"),
        "ended_at": m.get("ended_at"),
        "created_at": m.get("created_at"),
    }


def _rating_view(r: dict[str, Any] | None) -> dict[str, Any] | None:
    """rating 记录对外视图:bot_id + 展示名 + 评分明细。"""
    if r is None:
        return None
    return {
        "bot_id": r.get("bot_id"),
        "rating": r.get("rating"),
        "rd": r.get("rd"),
        "vol": r.get("vol"),
        "wins": r.get("wins", 0),
        "losses": r.get("losses", 0),
        "draws": r.get("draws", 0),
        "net_chips": r.get("net_chips", 0),
        "matches_played": r.get("matches_played", 0),
        "last_played_at": r.get("last_played_at"),
        "bot_name": r.get("bot_name"),
        "bot_display": r.get("bot_display"),
        "owner_name": r.get("owner_name"),
        "owner_display": r.get("owner_display"),
        "is_builtin": bool(r.get("is_builtin", 0)),
    }


def _pair_stats_view(ps: dict[str, Any], viewer_bot_id: int) -> dict[str, Any]:
    """pair_stats 视图:从 viewer_bot_id 视角规范化(bb/100 正=我对该对手盈利)。

    数据库里 (bot_a_id, bot_b_id) 是固定方向;若 viewer 是 bot_b,需把
    bb/100 取反、CI 镜像,让前端始终看到「我 vs 对手」的视角。
    """
    a_id = ps.get("bot_a_id")
    b_id = ps.get("bot_b_id")
    mean = float(ps.get("bb_per_100_mean") or 0.0)
    ci_low = ps.get("ci_low")
    ci_high = ps.get("ci_high")
    if a_id == viewer_bot_id:
        opponent_id = b_id
    else:
        opponent_id = a_id
        mean = -mean
        ci_low, ci_high = (None if ci_high is None else -ci_high,
                           None if ci_low is None else -ci_low)
    return {
        "opponent_bot_id": opponent_id,
        "bb_per_100_mean": mean,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "samples": ps.get("samples", 0),
        "last_played_at": ps.get("last_played_at"),
    }


def _bot_brief_with_owner(bot: dict[str, Any]) -> dict[str, Any]:
    """bot 简要视图(record 端点用)。"""
    return {
        "id": bot["id"],
        "name": bot["name"],
        "display_name": bot.get("display_name") or bot["name"],
        "owner_id": bot.get("owner_id"),
        "protocol": bot.get("protocol", "json"),
        "is_builtin": bool(bot.get("is_builtin", 0)),
    }
