"""新平台 Store (``arena/backend/platform/store/db.py``) 测试。

覆盖里程碑 1 的关键功能:建表、用户 / bot / 版本、对局生命周期、回放事件流、
评分天梯、bb/100 pair_stats、会话与密码重置、外键保护、以及从旧 ``arena.db``
迁移到新平台库的 ``migrate_from_legacy``。

风格:纯 pytest 函数式(参考 test_validator.py),每个 test 一个临时 db,互不共享状态。
"""
from __future__ import annotations

import sqlite3

import pytest

from arena.backend.platform.store import (
    PROTO_TCP,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    Store,
)
from arena.backend.platform.store.migrate_from_legacy import migrate


# ── 公用 fixture/helper ────────────────────────────────────────────
def _store(tmp_path) -> Store:
    """每个 test 用独立的临时 db 文件。"""
    return Store(str(tmp_path / "platform.db"))


def _make_user(store: Store, username: str = "alice", email: str | None = None,
               pw: str = "h1") -> dict:
    return store.create_user(username, email or f"{username}@x.com", pw)


# ── 1) 建表 ───────────────────────────────────────────────────────
def test_schema_init(tmp_path):
    """Store 初始化后所有表都应存在。"""
    store = _store(tmp_path)
    conn = sqlite3.connect(str(store.db_path))
    rows = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    expected = {"users", "bots", "matches", "match_replays", "ratings",
                "pair_stats", "sessions", "password_resets", "bot_versions"}
    assert expected.issubset(rows), f"缺表: {expected - rows}"


# ── 2) 用户 CRUD ──────────────────────────────────────────────────
def test_user_create_and_get(tmp_path):
    store = _store(tmp_path)
    u = store.create_user("bob", "bob@x.com", "secret", display_name="Bob")
    assert u["id"] is not None
    assert u["username"] == "bob"
    assert u["display_name"] == "Bob"
    assert u["role"] == "user"
    assert u["is_active"] == 1

    # 按 id / username / email 三种方式查都命中
    assert store.get_user(u["id"])["username"] == "bob"
    assert store.get_user_by_username("bob")["id"] == u["id"]
    assert store.get_user_by_email("bob@x.com")["id"] == u["id"]

    # 更新 password_hash 和 role
    upd = store.update_user(u["id"], password_hash="newhash", role="admin")
    assert upd["password_hash"] == "newhash"
    assert upd["role"] == "admin"
    # 其它字段不动
    assert upd["email"] == "bob@x.com"


# ── 3) 用户唯一约束 ───────────────────────────────────────────────
def test_user_unique_constraints(tmp_path):
    store = _store(tmp_path)
    store.create_user("alice", "a@x.com", "h")
    # username 重复
    with pytest.raises(sqlite3.IntegrityError):
        store.create_user("alice", "other@x.com", "h")
    # email 重复
    with pytest.raises(sqlite3.IntegrityError):
        store.create_user("alice2", "a@x.com", "h")


# ── 4) bot + 版本 ─────────────────────────────────────────────────
def test_bot_create_and_version(tmp_path):
    store = _store(tmp_path)
    u = _make_user(store)
    b = store.create_bot(u["id"], "BotA", protocol=PROTO_TCP)
    assert b["name"] == "BotA"
    assert b["protocol"] == "tcp"
    assert b["current_version"] == 0

    v1 = store.add_bot_version(b["id"], source_path="p1",
                               upload_note="first")
    assert v1["version"] == 1
    v2 = store.add_bot_version(b["id"], source_path="p2",
                               upload_note="second")
    assert v2["version"] == 2

    # current_version 跟着走
    bot_now = store.get_bot(b["id"])
    assert bot_now["current_version"] == 2

    # 版本历史列表(DESC)
    hist = store.list_bot_versions(b["id"])
    assert [h["version"] for h in hist] == [2, 1]
    assert hist[0]["upload_note"] == "second"


# ── 5) bot owner+name 唯一 ────────────────────────────────────────
def test_bot_owner_name_unique(tmp_path):
    store = _store(tmp_path)
    u1 = _make_user(store, "user1", "user1@x.com")
    u2 = _make_user(store, "user2", "user2@x.com")
    store.create_bot(u1["id"], "SameName")

    # 同一用户下重名 -> 冲突
    with pytest.raises(sqlite3.IntegrityError):
        store.create_bot(u1["id"], "SameName")
    # 不同用户同名 -> OK
    b2 = store.create_bot(u2["id"], "SameName")
    assert b2["id"] is not None
    assert store.get_bot_by_owner_name(u2["id"], "SameName")["id"] == b2["id"]


# ── 6) 对局生命周期 ──────────────────────────────────────────────
def test_match_lifecycle(tmp_path):
    store = _store(tmp_path)
    u = _make_user(store)
    ba = store.create_bot(u["id"], "BotA")
    bb = store.create_bot(u["id"], "BotB")

    # 创建(pending)
    m = store.create_match("m1", ba["id"], bb["id"], owner_id=u["id"],
                           match_type="challenge",
                           protocol_a="json", protocol_b="json")
    assert m["status"] == STATUS_PENDING
    assert m["winner"] is None

    # 更新(running)
    m = store.update_match("m1", status=STATUS_RUNNING, started_at="2026-01-01T00:00")
    assert m["status"] == STATUS_RUNNING

    # 更新(completed)
    m = store.update_match("m1", status=STATUS_COMPLETED,
                           hands_played=70, earnings_a=300, earnings_b=-300,
                           winner=0, reason="completed", ended_at="2026-01-01T01:00")
    assert m["winner"] == 0
    assert m["earnings_a"] == 300

    # get
    assert store.get_match("m1")["winner"] == 0
    assert store.get_match("nope") is None

    # list:再建一局用于筛选
    store.create_match("m2", ba["id"], bb["id"], owner_id=u["id"])

    by_owner = store.list_matches(owner_id=u["id"])
    assert {r["id"] for r in by_owner} == {"m1", "m2"}
    # JOIN 出来的 bot 名
    assert by_owner[0]["bot_a_name"] in {"BotA", "BotB"}

    by_bot = store.list_matches(bot_id=bb["id"])
    assert len(by_bot) == 2

    by_status = store.list_matches(status=STATUS_COMPLETED)
    assert {r["id"] for r in by_status} == {"m1"}

    # count 也对得上
    assert store.count_matches(owner_id=u["id"]) == 2
    assert store.count_matches(status=STATUS_COMPLETED) == 1


# ── 7) 回放事件流追加 ─────────────────────────────────────────────
def test_replay_append(tmp_path):
    store = _store(tmp_path)
    u = _make_user(store)
    ba = store.create_bot(u["id"], "A")
    bb = store.create_bot(u["id"], "B")
    store.create_match("rm", ba["id"], bb["id"])

    import json
    store.append_replay_event("rm", {"t": "hand_start", "i": 1})
    store.append_replay_event("rm", {"t": "action", "act": "raise"})
    store.append_replay_event("rm", {"t": "settle"})

    rep = store.get_replay("rm")
    assert rep is not None
    evs = json.loads(rep["events_json"])
    assert len(evs) == 3
    assert evs[0]["t"] == "hand_start"
    assert evs[2]["t"] == "settle"

    # 不存在的 match -> None
    assert store.get_replay("ghost") is None


# ── 8) 评分 upsert + 天梯 ─────────────────────────────────────────
def test_rating_upsert_and_leaderboard(tmp_path):
    store = _store(tmp_path)
    u = _make_user(store, "owner", "owner@x.com")
    ba = store.create_bot(u["id"], "Strong")
    bb = store.create_bot(u["id"], "Weak")

    store.upsert_rating(ba["id"], 1800.0, 50.0, 0.06,
                        wins=5, losses=1, net_chips=1000, matches_played=6)
    store.upsert_rating(bb["id"], 1400.0, 60.0, 0.06,
                        wins=1, losses=5, net_chips=-1000, matches_played=6)

    # 单查
    assert store.get_rating(ba["id"])["rating"] == 1800.0

    # 天梯降序 + JOIN 出 owner_name
    lb = store.leaderboard()
    assert len(lb) == 2
    assert lb[0]["bot_id"] == ba["id"]                      # 高分在前
    assert lb[0]["rating"] > lb[1]["rating"]
    assert lb[0]["owner_name"] == "owner"

    # upsert 更新覆盖(不是新增一行)
    store.upsert_rating(ba["id"], 1900.0, 40.0, 0.05)
    assert store.get_rating(ba["id"])["rating"] == 1900.0
    assert len(store.leaderboard()) == 2


# ── 9) pair_stats ─────────────────────────────────────────────────
def test_pair_stats(tmp_path):
    store = _store(tmp_path)
    u = _make_user(store)
    ba = store.create_bot(u["id"], "A")
    bb = store.create_bot(u["id"], "B")

    store.upsert_pair_stats(ba["id"], bb["id"],
                            bb_per_100_mean=12.5, ci_low=2.0, ci_high=23.0,
                            samples=100)

    rows = store.pair_stats_for(ba["id"])
    assert len(rows) == 1
    assert rows[0]["bb_per_100_mean"] == 12.5
    assert rows[0]["samples"] == 100
    # 另一方查也能拿到
    rows_b = store.pair_stats_for(bb["id"])
    assert len(rows_b) == 1

    # upsert 覆盖
    store.upsert_pair_stats(ba["id"], bb["id"], 20.0, 10.0, 30.0, 200)
    assert len(store.pair_stats_for(ba["id"])) == 1
    assert store.pair_stats_for(ba["id"])[0]["samples"] == 200


# ── 10) 会话 + 密码重置(一次性) ──────────────────────────────────
def test_session_and_password_reset(tmp_path):
    store = _store(tmp_path)
    u = _make_user(store)

    # session 增删查
    store.add_session("tok1", u["id"], expires_at="2099-01-01T00:00")
    s = store.get_session("tok1")
    assert s is not None and s["user_id"] == u["id"]
    assert store.delete_session("tok1") is True
    assert store.get_session("tok1") is None
    assert store.delete_session("tok1") is False              # 已删

    # 批量删某用户 session
    store.add_session("t2", u["id"], "2099-01-01T00:00")
    store.add_session("t3", u["id"], "2099-01-01T00:00")
    assert store.delete_user_sessions(u["id"]) == 2
    assert store.get_session("t2") is None

    # 密码重置 token 一次性
    store.add_password_reset("rtok", u["id"], expires_at="2099-01-01T00:00")
    assert store.get_password_reset("rtok") is not None
    store.mark_password_reset_used("rtok")
    # used 后再查 -> None
    assert store.get_password_reset("rtok") is None


# ── 11) 外键保护 ──────────────────────────────────────────────────
def test_fk_protection(tmp_path):
    store = _store(tmp_path)
    u = _make_user(store)
    ba = store.create_bot(u["id"], "A")
    bb = store.create_bot(u["id"], "B")
    store.create_match("fm", ba["id"], bb["id"], owner_id=u["id"])

    # 有对局引用 bot -> 物理删 bot 失败
    with pytest.raises(sqlite3.IntegrityError):
        store.delete_bot(ba["id"])

    # 有对局引用的用户的 bot 间接被引用 -> 删用户也失败(FK 失败)
    with pytest.raises(sqlite3.IntegrityError):
        store.delete_user(u["id"])

    # 但软封禁(置 is_active=0)不受影响
    banned = store.update_user(u["id"], is_active=0)
    assert banned["is_active"] == 0

    # 没有任何对局引用的 bot 可以删:建第三个 bot
    bc = store.create_bot(u["id"], "C")
    assert store.delete_bot(bc["id"]) is True
    assert store.get_bot(bc["id"]) is None


# ── 12) migrate_from_legacy ───────────────────────────────────────
_LEGACY_SCHEMA = """
CREATE TABLE users (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    team TEXT DEFAULT '',
    note TEXT DEFAULT '',
    secret TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE matches (
    match_id TEXT PRIMARY KEY,
    name_a TEXT NOT NULL,
    name_b TEXT NOT NULL,
    hands_played INTEGER NOT NULL,
    total_hands INTEGER NOT NULL,
    earnings_a INTEGER NOT NULL,
    earnings_b INTEGER NOT NULL,
    winner INTEGER,
    reason TEXT NOT NULL,
    net_bb_a REAL NOT NULL,
    thp_file TEXT DEFAULT '',
    log_dir TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL
);
CREATE TABLE ratings (
    name TEXT PRIMARY KEY,
    rating REAL NOT NULL,
    rd REAL NOT NULL,
    vol REAL NOT NULL,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    draws INTEGER DEFAULT 0,
    net_chips INTEGER DEFAULT 0,
    matches_played INTEGER DEFAULT 0,
    last_played_at TEXT
);
CREATE TABLE pair_stats (
    name_a TEXT NOT NULL,
    name_b TEXT NOT NULL,
    bb_per_100_mean REAL NOT NULL,
    ci_low REAL,
    ci_high REAL,
    samples INTEGER NOT NULL,
    last_played_at TEXT NOT NULL,
    PRIMARY KEY (name_a, name_b)
);
CREATE TABLE admins (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                     created_at TEXT NOT NULL);
CREATE TABLE sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL,
                       expires_at TEXT NOT NULL);
"""


def _build_legacy_db(path: str) -> None:
    """构造一个迷你旧 arena.db(只插少量样例行)。"""
    c = sqlite3.connect(path)
    c.executescript(_LEGACY_SCHEMA)
    now = "2026-01-01T00:00"
    c.executemany(
        "INSERT INTO users(name, display_name, team, note, secret, active, "
        "created_at, first_seen_at) VALUES(?,?,?,?,?,?,?,?)",
        [("BotX", "BotX", "", "", "", 1, now, now),
         ("BotY", "BotY", "TY", "", "", 1, now, now)],
    )
    c.execute(
        "INSERT INTO matches(match_id, name_a, name_b, hands_played, "
        "total_hands, earnings_a, earnings_b, winner, reason, net_bb_a, "
        "thp_file, log_dir, started_at, ended_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("BotX_vs_BotY", "BotX", "BotY", 70, 70,
         300, -300, 0, "completed", 3.0, "", "", now, now),
    )
    c.executemany(
        "INSERT INTO ratings(name, rating, rd, vol, wins, losses, draws, "
        "net_chips, matches_played, last_played_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [("BotX", 1600.0, 50.0, 0.06, 1, 0, 0, 300, 1, now),
         ("BotY", 1400.0, 50.0, 0.06, 0, 1, 0, -300, 1, now)],
    )
    c.execute(
        "INSERT INTO pair_stats(name_a, name_b, bb_per_100_mean, ci_low, "
        "ci_high, samples, last_played_at) VALUES(?,?,?,?,?,?,?)",
        ("BotX", "BotY", 4.3, -1.2, 9.8, 70, now),
    )
    # admins / sessions 不应被迁移
    c.execute(
        "INSERT INTO admins(username, password_hash, created_at) VALUES(?,?,?)",
        ("admin", "hashed", now),
    )
    c.execute(
        "INSERT INTO sessions(token, username, expires_at) VALUES(?,?,?)",
        ("stok", "admin", now),
    )
    c.commit()
    c.close()


def test_migrate_from_legacy(tmp_path):
    legacy = str(tmp_path / "arena.db")
    platform = str(tmp_path / "arena_platform.db")
    _build_legacy_db(legacy)

    stats = migrate(legacy, platform)
    assert stats["users"] == 2
    assert stats["bots"] == 2
    assert stats["matches"] == 1
    assert stats["ratings"] == 2
    assert stats["pair_stats"] == 1

    store = Store(platform)

    # legacy 用户:不可登录、email 占位、role=user
    ux = store.get_user_by_username("BotX")
    assert ux is not None
    assert ux["email"] == "BotX@legacy.local"
    assert ux["password_hash"] == "!legacy_migrated"
    assert ux["role"] == "user"
    assert ux["is_active"] == 0

    # bot:protocol=tcp、is_builtin=0、owner 指向 legacy 用户
    bx = store.get_bot_by_owner_name(ux["id"], "BotX")
    assert bx is not None
    assert bx["protocol"] == "tcp"
    assert bx["is_builtin"] == 0

    # match:name -> bot_id 映射正确,status/type/protocol 全部转换
    m = store.get_match("BotX_vs_BotY")
    assert m is not None
    assert m["bot_a_id"] == bx["id"]
    assert m["status"] == "completed"
    assert m["match_type"] == "exhibition"
    assert m["protocol_a"] == "tcp"
    assert m["earnings_a"] == 300

    # ratings / pair_stats 都按 bot_id 落位
    assert store.get_rating(bx["id"])["rating"] == 1600.0
    by = store.get_bot_by_owner_name(
        store.get_user_by_username("BotY")["id"], "BotY")
    ps = store.pair_stats_for(bx["id"])
    assert len(ps) == 1
    assert ps[0]["bb_per_100_mean"] == 4.3
    assert ps[0]["samples"] == 70

    # admins / sessions 没迁过来
    assert store.list_users(role="admin") == []

    # 幂等:再跑一遍不报错,数据不重复
    stats2 = migrate(legacy, platform)
    assert stats2["users"] == 0           # 用户名唯一,跳过
    assert stats2["bots"] == 0            # (owner,name) 唯一,跳过
    # matches/ratings/pair_stats 走 upsert 覆盖,数据量不变
    assert store.count_matches() == 1
    assert len(store.leaderboard()) == 2

    # 缺失旧库文件 -> 空统计不抛错
    empty = migrate(str(tmp_path / "no_such.db"), platform)
    assert empty == {"users": 0, "bots": 0, "matches": 0,
                     "ratings": 0, "pair_stats": 0}
