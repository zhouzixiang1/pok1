"""里程碑 7(对局回放)测试。

覆盖:

1. ``build_hand_snapshots`` 正确按手分组(2 手 → 2 个快照)
2. 手牌 / 公共牌 / 动作时间线 / settle / final_chips 填充正确
3. 卡牌格式转换:整数、``<s,r>`` 字符串、``[suit,rank]`` 三种输入兼容
4. ``snapshot_at_step`` 中间状态(停在 action 中间 / 阶段边界 / 全程)
5. API ``/api/matches/{id}/replay`` 返回结构正确(TestClient + save_replay)
6. API ``/replay/hands`` 轻量端点 + ``/replay/step`` 逐步端点

事件流样例(2 手):第一手 showdown(双方 check 到底,A 赢),第二手
preflop 弃牌结束。手数用 player_chips=[19950, 19900] 这种代表 SB/BB
已下盲注后的筹码(对齐 match_runner 的 hand_start 事件语义)。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena.backend.platform.api import router as api_router
from arena.backend.platform.auth import AuthManager
from arena.backend.platform.auth.routes import router as auth_router
from arena.backend.platform.runtime.orchestrator import MatchOrchestrator
from arena.backend.platform.runtime.replay import (
    build_hand_snapshots,
    card_to_display,
    snapshot_at_step,
)
from arena.backend.platform.store import Store


# ══════════════════════════════════════════════════════════
# 模拟事件流:2 手对局
# ══════════════════════════════════════════════════════════

def _sample_events() -> list[dict[str, Any]]:
    """构造 2 手对局的事件流(对齐 match_runner._emit 的字段语义)。

    第一手(hand=1):SB=0 BB=1,双方 preflop 都 check,flop 后 SB bet 200
    BB call,turn/river check,showdown → A 赢 200。
    第二手(hand=2):SB=1 BB=0,preflop BB raise → SB fold,BB 赢盲注。
    """
    return [
        # ── 第一手 ─────────────────────────────────────────
        {"type": "hand_start", "hand": 1, "sb_idx": 0, "bb_idx": 1,
         "names": ["BotA", "BotB"],
         "player_chips": [19950, 19900], "pot": 150},
        {"type": "cards_dealt", "hand": 1,
         "hole_cards": [["<0,12>", "<1,12>"], ["<2,5>", "<3,5>"]]},
        {"type": "action", "hand": 1, "player_idx": 0, "action": "call",
         "stage": "preflop", "pot": 150,
         "player_chips": [19900, 19900]},
        {"type": "action", "hand": 1, "player_idx": 1, "action": "check",
         "stage": "preflop", "pot": 150,
         "player_chips": [19900, 19900]},
        {"type": "stage", "stage": "flop", "hand": 1,
         "cards": ["<0,0>", "<1,1>", "<2,2>"]},
        {"type": "action", "hand": 1, "player_idx": 0, "action": "raise",
         "amount": 200, "stage": "flop", "pot": 350,
         "player_chips": [19700, 19900]},
        {"type": "action", "hand": 1, "player_idx": 1, "action": "call",
         "amount": 200, "stage": "flop", "pot": 550,
         "player_chips": [19700, 19700]},
        {"type": "stage", "stage": "turn", "hand": 1, "cards": ["<3,10>"]},
        {"type": "action", "hand": 1, "player_idx": 0, "action": "check",
         "stage": "turn", "pot": 550,
         "player_chips": [19700, 19700]},
        {"type": "action", "hand": 1, "player_idx": 1, "action": "check",
         "stage": "turn", "pot": 550,
         "player_chips": [19700, 19700]},
        {"type": "stage", "stage": "river", "hand": 1, "cards": ["<0,9>"]},
        {"type": "action", "hand": 1, "player_idx": 0, "action": "check",
         "stage": "river", "pot": 550,
         "player_chips": [19700, 19700]},
        {"type": "action", "hand": 1, "player_idx": 1, "action": "check",
         "stage": "river", "pot": 550,
         "player_chips": [19700, 19700]},
        {"type": "settle", "hand": 1, "is_showdown": True,
         "winner_idx": 0, "pot": 550, "earnings": [275, -275],
         "player_chips": [20225, 19700]},

        # ── 第二手 ─────────────────────────────────────────
        {"type": "hand_start", "hand": 2, "sb_idx": 1, "bb_idx": 0,
         "names": ["BotA", "BotB"],
         "player_chips": [20000, 20000], "pot": 150},
        {"type": "cards_dealt", "hand": 2,
         "hole_cards": [["<0,0>", "<0,1>"], ["<1,0>", "<1,1>"]]},
        {"type": "action", "hand": 2, "player_idx": 0, "action": "raise",
         "amount": 300, "stage": "preflop", "pot": 450,
         "player_chips": [19700, 20000]},
        {"type": "action", "hand": 2, "player_idx": 1, "action": "fold",
         "stage": "preflop", "pot": 450,
         "player_chips": [19700, 20000]},
        {"type": "settle", "hand": 2, "is_showdown": False,
         "winner_idx": 0, "pot": 450, "earnings": [450, -450],
         "player_chips": [20450, 19550]},

        {"type": "match_end", "winner": 0, "earnings": [725, -725],
         "hands_played": 2},
    ]


# ══════════════════════════════════════════════════════════
# 1. build_hand_snapshots 分组正确
# ══════════════════════════════════════════════════════════

def test_build_hand_snapshots_groups_by_hand():
    """2 手对局 → 2 个快照,每个 snapshot.hand 正确。"""
    snaps = build_hand_snapshots(_sample_events())
    assert len(snaps) == 2
    assert snaps[0]["hand"] == 1
    assert snaps[1]["hand"] == 2


def test_build_hand_snapshots_basic_fields():
    """hand_start 字段透传:sb/bb/names/initial_chips。"""
    snaps = build_hand_snapshots(_sample_events())
    s1 = snaps[0]
    assert s1["sb_idx"] == 0
    assert s1["bb_idx"] == 1
    assert s1["names"] == ["BotA", "BotB"]
    assert s1["initial_chips"] == [19950, 19900]

    # 第二手 sb/bb 交换
    assert snaps[1]["sb_idx"] == 1
    assert snaps[1]["bb_idx"] == 0
    assert snaps[1]["initial_chips"] == [20000, 20000]


def test_build_hand_snapshots_hole_cards():
    """cards_dealt → hole_cards 填充,格式转成展示 dict。"""
    snaps = build_hand_snapshots(_sample_events())
    hole = snaps[0]["hole_cards"]
    assert len(hole) == 2  # 两个玩家
    assert len(hole[0]) == 2  # 每人两张
    # 第一张是 <0,12> = ♠A
    first = hole[0][0]
    assert first["card"] == "<0,12>"
    assert first["text"] == "♠A"
    assert first["suit"] == 0
    assert first["rank"] == 12


def test_build_hand_snapshots_community_accumulates():
    """flop + turn + river → community 完整累积(3+1+1=5 张)。"""
    snaps = build_hand_snapshots(_sample_events())
    community = snaps[0]["community"]
    assert len(community) == 5
    # flop 3 张
    assert community[0]["card"] == "<0,0>"
    assert community[1]["card"] == "<1,1>"
    assert community[2]["card"] == "<2,2>"
    # turn 1 张
    assert community[3]["card"] == "<3,10>"
    assert community[3]["text"] == "♣Q"
    # river 1 张
    assert community[4]["card"] == "<0,9>"
    assert community[4]["text"] == "♠J"


def test_build_hand_snapshots_actions_timeline():
    """action → actions 列表,记录 player/action/stage/pot/chips_after。"""
    snaps = build_hand_snapshots(_sample_events())
    actions = snaps[0]["actions"]
    # preflop: call + check,flop: raise + call,turn: check+check,river: check+check
    assert len(actions) == 8
    # 第一动作:player0 call,preflop
    a0 = actions[0]
    assert a0["player_idx"] == 0
    assert a0["action"] == "call"
    assert a0["stage"] == "preflop"
    assert a0["pot"] == 150
    assert a0["chips_after"] == [19900, 19900]
    # flop 的 raise
    raise_act = next(a for a in actions if a["action"] == "raise")
    assert raise_act["stage"] == "flop"
    assert raise_act["amount"] == 200


def test_build_hand_snapshots_settle_and_final_chips():
    """settle → settle 字段 + final_chips(=settle 事件的 player_chips)。"""
    snaps = build_hand_snapshots(_sample_events())
    s1 = snaps[0]
    assert s1["settle"] is not None
    assert s1["settle"]["winner_idx"] == 0
    assert s1["settle"]["is_showdown"] is True
    assert s1["settle"]["pot"] == 550
    assert s1["settle"]["earnings"] == [275, -275]
    # final_chips = settle 事件的 player_chips
    assert s1["final_chips"] == [20225, 19700]


def test_build_hand_snapshots_final_chips_derived_when_missing_player_chips():
    """settle 无 player_chips 时,final_chips = initial + earnings。"""
    events = [
        {"type": "hand_start", "hand": 1, "sb_idx": 0, "bb_idx": 1,
         "names": ["A", "B"], "player_chips": [1000, 1000], "pot": 50},
        {"type": "cards_dealt", "hand": 1, "hole_cards": [["<0,0>"], ["<1,0>"]]},
        {"type": "settle", "hand": 1, "is_showdown": False,
         "winner_idx": 0, "pot": 100, "earnings": [50, -50]},
    ]
    snaps = build_hand_snapshots(events)
    assert snaps[0]["final_chips"] == [1050, 950]  # 1000 + earnings


def test_build_hand_snapshots_aborted_hand_no_settle():
    """对局异常中断(无 settle)的手也会输出,settle=None。"""
    events = [
        {"type": "hand_start", "hand": 1, "sb_idx": 0, "bb_idx": 1,
         "names": ["A", "B"], "player_chips": [1000, 1000], "pot": 50},
        {"type": "action", "hand": 1, "player_idx": 0, "action": "call",
         "stage": "preflop", "pot": 100, "player_chips": [950, 1000]},
        # 没有 settle 直接结束
    ]
    snaps = build_hand_snapshots(events)
    assert len(snaps) == 1
    assert snaps[0]["settle"] is None
    # final_chips 兜底为最后已知筹码
    assert snaps[0]["final_chips"] == [950, 1000]


def test_build_hand_snapshots_skips_global_events():
    """match_start / match_end 等 global 事件(无 hand)不生成快照。"""
    events = [
        {"type": "match_start", "match_id": "m-1", "bot_a": "A", "bot_b": "B"},
        {"type": "hand_start", "hand": 1, "sb_idx": 0, "bb_idx": 1,
         "names": ["A", "B"], "player_chips": [1000, 1000], "pot": 50},
        {"type": "settle", "hand": 1, "is_showdown": False,
         "winner_idx": 0, "pot": 50, "earnings": [50, -50],
         "player_chips": [1050, 950]},
        {"type": "match_end", "winner": 0, "earnings": [50, -50],
         "hands_played": 1},
    ]
    snaps = build_hand_snapshots(events)
    assert len(snaps) == 1  # 只有 hand=1 一个


# ══════════════════════════════════════════════════════════
# 2. 卡牌格式转换(整数 / 字符串 / 列表 三种输入)
# ══════════════════════════════════════════════════════════

def test_card_to_display_string_protocol():
    """<suit,rank> 字符串 → 正确解析。"""
    d = card_to_display("<0,12>")
    assert d["card"] == "<0,12>"
    assert d["text"] == "♠A"
    assert d["suit"] == 0
    assert d["rank"] == 12


def test_card_to_display_integer():
    """整数(0-51 编号)→ suit=n//13, rank=n%13。"""
    # n=0 → ♠2,n=12 → ♠A,n=13 → ♥2,n=51 → ♣A
    assert card_to_display(0)["text"] == "♠2"
    assert card_to_display(12)["text"] == "♠A"
    assert card_to_display(13)["text"] == "♥2"
    assert card_to_display(51)["text"] == "♣A"


def test_card_to_display_list_pair():
    """[suit, rank] 列表 → 正确解析。"""
    d = card_to_display([2, 8])
    assert d["card"] == "<2,8>"
    assert d["text"] == "♦10"  # rank 8 = "10"
    assert d["suit"] == 2


def test_card_to_display_readable_text_reverse():
    """可读格式(如 '♠A')反向解析。"""
    d = card_to_display("♠A")
    assert d["card"] == "<0,12>"
    assert d["text"] == "♠A"
    assert d["rank"] == 12


def test_card_to_display_invalid_fallback():
    """无效输入不抛,返回 fallback。"""
    d = card_to_display("garbage")
    assert d["card"] == "garbage"
    assert d["text"] == "?"
    assert d["suit"] is None
    # 整数超出 0-51
    d2 = card_to_display(999)
    assert d2["suit"] is None
    assert d2["text"] == "?"


def test_card_to_display_idempotent_on_dict():
    """已是 display dict 透传(幂等)。"""
    src = {"card": "<1,5>", "text": "♥7", "suit": 1, "rank": 5}
    assert card_to_display(src) == src


def test_cards_in_events_int_format_also_works():
    """事件里卡牌用整数也能正确解析到快照。"""
    events = [
        {"type": "hand_start", "hand": 1, "sb_idx": 0, "bb_idx": 1,
         "names": ["A", "B"], "player_chips": [100, 100], "pot": 10},
        {"type": "cards_dealt", "hand": 1,
         "hole_cards": [[0, 12], [13, 25]]},  # 整数格式
        {"type": "stage", "stage": "flop", "hand": 1, "cards": [26, 27, 28]},
        {"type": "settle", "hand": 1, "is_showdown": True,
         "winner_idx": 0, "pot": 10, "earnings": [10, -10],
         "player_chips": [110, 90]},
    ]
    snaps = build_hand_snapshots(events)
    # 整数 0 → ♠2,12 → ♠A
    assert snaps[0]["hole_cards"][0][0]["text"] == "♠2"
    assert snaps[0]["hole_cards"][0][1]["text"] == "♠A"
    # flop 整数卡牌:26 = 26//13=2(♦), rank=0(2)
    assert len(snaps[0]["community"]) == 3
    assert snaps[0]["community"][0]["text"] == "♦2"
    assert snaps[0]["community"][1]["text"] == "♦3"
    assert snaps[0]["community"][2]["text"] == "♦4"


# ══════════════════════════════════════════════════════════
# 3. snapshot_at_step 中间状态
# ══════════════════════════════════════════════════════════

def test_snapshot_at_step_zero():
    """step=0 → 空状态(total_events 正确,event=None)。"""
    events = _sample_events()
    snap = snapshot_at_step(events, 0)
    assert snap["step"] == 0
    assert snap["total_events"] == len(events)
    assert snap["hand"] is None
    assert snap["community_so_far"] == []
    assert snap["actions_so_far"] == []
    assert snap["event"] is None


def test_snapshot_at_step_full():
    """step=len → 全部回放完毕,最后一事件 = match_end。"""
    events = _sample_events()
    snap = snapshot_at_step(events, len(events))
    assert snap["step"] == len(events)
    # match_end 无 hand 字段 → hand 保留最后一次 hand_start 的值
    assert snap["event"]["type"] == "match_end"
    # actions_so_far 在新手牌开始时重置,所以末尾应是第 2 手的动作
    assert len(snap["actions_so_far"]) == 2  # raise + fold


def test_snapshot_at_step_middle_of_hand1_actions():
    """停在第 1 手 preflop 两个动作之后:action 数 = 2,community 空。"""
    events = _sample_events()
    # 前 5 个事件:hand_start, cards_dealt, action(call), action(check), stage(flop)
    # step=4 停在第二个 action 之后(还没发 flop)
    snap = snapshot_at_step(events, 4)
    assert snap["hand"] == 1
    assert snap["stage"] == "preflop"
    assert len(snap["actions_so_far"]) == 2  # call + check
    assert snap["community_so_far"] == []  # 还没 flop


def test_snapshot_at_step_after_flop():
    """停在 flop stage 之后:community 应有 3 张。"""
    events = _sample_events()
    # step=5 包含 flop stage 事件
    snap = snapshot_at_step(events, 5)
    assert snap["hand"] == 1
    assert snap["stage"] == "flop"
    assert len(snap["community_so_far"]) == 3
    assert snap["community_so_far"][0]["card"] == "<0,0>"


def test_snapshot_at_step_after_turn_river():
    """停在 river 后:community 完整 5 张。"""
    events = _sample_events()
    # 找到 river stage 的索引
    river_idx = next(i for i, e in enumerate(events)
                     if e.get("stage") == "river")
    snap = snapshot_at_step(events, river_idx + 1)
    assert snap["stage"] == "river"
    assert len(snap["community_so_far"]) == 5


def test_snapshot_at_step_new_hand_resets_accumulators():
    """第 2 手 hand_start 重置 community + actions 累积。"""
    events = _sample_events()
    # 找到第 2 手 hand_start 的索引(它后面跟着 cards_dealt)
    hand2_start_idx = next(
        i for i, e in enumerate(events)
        if e.get("type") == "hand_start" and e.get("hand") == 2)
    snap = snapshot_at_step(events, hand2_start_idx + 1)
    assert snap["hand"] == 2
    # 第 2 手刚开始,community 应该重置为空(不是第 1 手的 5 张)
    assert snap["community_so_far"] == []
    assert snap["actions_so_far"] == []


def test_snapshot_at_step_chips_so_far():
    """chips_so_far 反映最近一次 player_chips。"""
    events = _sample_events()
    # step=3(第 1 个 action 之后)→ player_chips=[19900, 19900]
    snap = snapshot_at_step(events, 3)
    assert snap["chips_so_far"] == [19900, 19900]


def test_snapshot_at_step_clamps_out_of_range():
    """step 超出 [0, len] 被夹到边界。"""
    events = _sample_events()
    snap = snapshot_at_step(events, -5)
    assert snap["step"] == 0
    snap2 = snapshot_at_step(events, 99999)
    assert snap2["step"] == len(events)


# ══════════════════════════════════════════════════════════
# 4. API /api/matches/{id}/replay
# ══════════════════════════════════════════════════════════

def _make_app(tmp_path) -> tuple[FastAPI, Store, AuthManager]:
    """构造带 /api + /api/auth 路由的 app(注入 orchestrator/store)。

    orchestrator 不会真的跑对战(测试里直接 save_replay 灌事件流),所以
    runner 用 stub。
    """
    from arena.backend.platform.auth.captcha import CaptchaStore
    store = Store(str(tmp_path / "replay_api.db"))
    auth = AuthManager(store)
    captcha = CaptchaStore()
    # stub runner(orchestrator 不会跑对战,但 __init__ 要一个)
    class _StubRunner:
        async def start_session(self, *a, **kw): return "x"
        async def stop_session(self, *a, **kw): return None
        async def cleanup_all(self): return None
    orch = MatchOrchestrator(store, _StubRunner(), hands_per_match=1,
                             action_timeout=1.0)
    app = FastAPI()
    app.state.platform_store = store
    app.state.platform_auth = auth
    app.state.platform_captcha = captcha
    app.state.platform_orchestrator = orch
    app.include_router(auth_router)
    app.include_router(api_router)
    return app, store, auth


def _login(client: TestClient, auth: AuthManager) -> None:
    """注册+验证邮箱+登录。"""
    auth.register("alice", "alice@x.com", "secret123")
    auth.store.update_user(
        auth.store.get_user_by_username("alice")["id"], email_verified=1)
    captcha = client.app.state.platform_captcha
    cid, answer, _ = captcha.create()
    r = client.post("/api/auth/login", json={
        "username": "alice", "password": "secret123",
        "captcha_id": cid, "captcha_answer": answer})
    assert r.status_code == 200, r.text


def _setup_match_and_replay(store: Store, events: list[dict],
                            match_id: str = "m-test-replay-1"
                            ) -> tuple[str, dict]:
    """建 match + 写 replay 事件流(复用已登录的 alice 用户)。

    必须在 ``_login`` 之后调(那时 alice 已存在)。返回 (match_id, match_dict)。
    """
    user = store.get_user_by_username("alice")
    assert user is not None, "_login 必须先调"
    ba = store.create_bot(user["id"], "BotA", protocol="json")
    bb = store.create_bot(user["id"], "BotB", protocol="json")
    m = store.create_match(match_id, ba["id"], bb["id"],
                           owner_id=user["id"], total_hands=2)
    store.save_replay(match_id,
                      events_json=json.dumps(events, ensure_ascii=False))
    return match_id, m


def test_api_replay_returns_full_structure(tmp_path):
    """GET /replay 返回 {match, snapshots, events} 三段。"""
    app, store, auth = _make_app(tmp_path)
    events = _sample_events()

    with TestClient(app) as client:
        _login(client, auth)
        match_id, _ = _setup_match_and_replay(store, events)
        r = client.get(f"/api/matches/{match_id}/replay")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "match" in body
        assert "snapshots" in body
        assert "events" in body
        # 元数据
        assert body["match"]["id"] == match_id
        # 快照:2 手 → 2 个 snapshot
        assert len(body["snapshots"]) == 2
        assert body["snapshots"][0]["hand"] == 1
        # 原始事件全量返回
        assert len(body["events"]) == len(events)


def test_api_replay_hand1_snapshot_fields(tmp_path):
    """GET /replay 第 1 手快照字段完整(hole_cards/community/actions/settle)。"""
    app, store, auth = _make_app(tmp_path)
    events = _sample_events()

    with TestClient(app) as client:
        _login(client, auth)
        match_id, _ = _setup_match_and_replay(store, events)
        r = client.get(f"/api/matches/{match_id}/replay")
        s1 = r.json()["snapshots"][0]
        # 必填字段都存在
        for k in ("hand", "sb_idx", "bb_idx", "names",
                  "initial_chips", "hole_cards", "community",
                  "actions", "settle", "final_chips"):
            assert k in s1, f"快照缺字段 {k}"
        assert len(s1["community"]) == 5
        assert len(s1["actions"]) == 8
        assert s1["settle"]["winner_idx"] == 0
        assert s1["final_chips"] == [20225, 19700]


def test_api_replay_hands_lightweight(tmp_path):
    """GET /replay/hands 只返回 snapshots(轻量,不含 events)。"""
    app, store, auth = _make_app(tmp_path)
    events = _sample_events()

    with TestClient(app) as client:
        _login(client, auth)
        match_id, _ = _setup_match_and_replay(store, events)
        r = client.get(f"/api/matches/{match_id}/replay/hands")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["match_id"] == match_id
        assert body["hand_count"] == 2
        assert len(body["snapshots"]) == 2
        assert "events" not in body  # 轻量端点不返回原始事件


def test_api_replay_step_endpoint(tmp_path):
    """GET /replay/step?step=N 返回中间状态。"""
    app, store, auth = _make_app(tmp_path)
    events = _sample_events()

    with TestClient(app) as client:
        _login(client, auth)
        match_id, _ = _setup_match_and_replay(store, events)
        # step=0
        r = client.get(f"/api/matches/{match_id}/replay/step?step=0")
        assert r.status_code == 200
        body = r.json()
        assert body["step"] == 0
        assert body["hand"] is None

        # step=5(包含 flop stage)
        r = client.get(f"/api/matches/{match_id}/replay/step?step=5")
        body = r.json()
        assert body["hand"] == 1
        assert body["stage"] == "flop"
        assert len(body["community_so_far"]) == 3


def test_api_replay_404_no_match(tmp_path):
    """对不存在的 match_id → 404。"""
    app, _, auth = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client, auth)
        r = client.get("/api/matches/m-not-exists/replay")
        assert r.status_code == 404


def test_api_replay_404_no_replay_record(tmp_path):
    """match 存在但无 replay 记录 → 404。"""
    app, store, auth = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client, auth)
        user = store.get_user_by_username("alice")
        ba = store.create_bot(user["id"], "BotA", protocol="json")
        bb = store.create_bot(user["id"], "BotB", protocol="json")
        store.create_match("m-no-replay", ba["id"], bb["id"],
                           owner_id=user["id"])
        r = client.get("/api/matches/m-no-replay/replay")
        assert r.status_code == 404


def test_api_replay_requires_login(tmp_path):
    """未登录调 /replay → 401。"""
    app, _, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/api/matches/whatever/replay")
        assert r.status_code == 401
