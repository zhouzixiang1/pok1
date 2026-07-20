"""从旧 ``arena.db`` 迁移到新平台库 ``arena_platform.db``。

旧库(服务 TCP 通道,``store/db.py``)的语义是「bot 身份」:`users` 表主键就是 bot 名,
没有真正的用户账号概念。新平台(``platform/store/``)把用户和 bot 分离,本脚本负责把
历史数据一次性导入,做到:

- 旧 ``users``(name=队名 / bot 名)→ 新库 **同时建一条 legacy 用户 + 一条 bot**:
  * 用户:``username`` 用旧 name,``email`` 用 ``<name>@legacy.local``,
    ``password_hash`` 用占位符 ``!legacy_migrated``,``role='user'``,``is_active=0``
    (legacy 账号不可登录,仅作历史归属)。
  * bot:``name`` 用旧 name,``protocol='tcp'``(旧库都是 TCP 时代),
    ``is_builtin=0``,owner 指向上面那个 legacy 用户。
- 旧 ``matches`` → 新 ``matches`` 表:``name_a/name_b`` 经 name→bot_id 映射,
  ``earnings/hands/winner/reason`` 直接搬,``status='completed'``,
  ``match_type='exhibition'``,``protocol_a/b='tcp'``。
- 旧 ``ratings`` → 新 ``ratings`` 表(主键从 name 改成 bot_id)。
- 旧 ``pair_stats`` → 新 ``pair_stats`` 表(主键从 name 对改成 bot_id 对)。
- 旧 ``admins`` / ``sessions`` **不迁移**(新平台有独立认证体系)。

幂等:重复跑不报错。唯一性判断用旧 name:用户按 username、bot 按 (owner,name)、
match 按 id、ratings/pair_stats 用 ``INSERT OR REPLACE``(覆盖式 upsert,以最新值为准)。

旧库文件不存在或所需表缺失时,**优雅返回空统计,不抛异常**(允许新库无历史可迁的场景)。
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from .db import Store, _now
from .schema import PROTO_TCP

# legacy 用户不可登录的密码占位符(任何 PBKDF2 哈希都不会是这个形式)
_LEGACY_PW = "!legacy_migrated"


def _legacy_email(name: str) -> str:
    """生成 legacy 用户的占位 email(同 name 派生,保证唯一)。"""
    # name 里可能含特殊字符,做最小清洗(只留 alnum / _ / -)
    safe = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in name)
    return f"{safe}@legacy.local"


def _legacy_username(name: str) -> str:
    """生成 legacy 用户的 username。

    旧 users.name 是 PK,本身唯一;新 users.username 要求 >=3 字符。
    极端短 name 补下划线兜底,保证不触发 CHECK。
    """
    safe = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in name)
    while len(safe) < 3:
        safe += "_"
    return safe


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return r is not None


def migrate(legacy_db_path: str | Path = "arena.db",
            platform_db_path: str | Path = "arena_platform.db") -> dict[str, int]:
    """从旧库迁移到新平台库。

    参数:
        legacy_db_path:   旧 ``arena.db`` 路径。
        platform_db_path: 新 ``arena_platform.db`` 路径(不存在会自动建)。

    返回:
        迁移统计 ``{'users': N, 'bots': N, 'matches': N, 'ratings': N, 'pair_stats': N}``。
        旧库不存在或缺表时返回全 0。

    幂等:可重复执行,重复行不会新增(用 name 做唯一性判断)。
    """
    stats = {"users": 0, "bots": 0, "matches": 0, "ratings": 0, "pair_stats": 0}

    legacy_path = Path(legacy_db_path)
    if not legacy_path.exists():
        # 旧库不存在:优雅返回空统计
        return stats

    # 先确保目标库 schema 就绪(Store.__init__ 会建表)
    store = Store(platform_db_path)

    # 打开旧库只读连接(便于排查 / 不污染源)
    legacy = sqlite3.connect(str(legacy_path))
    legacy.row_factory = sqlite3.Row

    try:
        # ── 1) users(legacy bot 身份)→ 新库 users + bots ──────────────
        # 同时建立 name -> (user_id, bot_id) 映射,后续 matches/ratings 复用。
        name_map: dict[str, dict[str, int]] = {}
        if _table_exists(legacy, "users"):
            for u in legacy.execute("SELECT * FROM users"):
                name = u["name"]
                uname = _legacy_username(name)
                email = _legacy_email(name)

                # 用户:username 唯一,已存在则跳过(幂等)
                with store._tx() as c:  # noqa: SLF001 — 复用 Store 事务上下文
                    row = c.execute(
                        "SELECT id FROM users WHERE username=?", (uname,)
                    ).fetchone()
                    if row is None:
                        cur = c.execute(
                            "INSERT INTO users(username, email, password_hash, "
                            "role, display_name, is_active, created_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (uname, email, _LEGACY_PW, "user",
                             u["display_name"] or name, 0,
                             u["created_at"] or _now()),
                        )
                        user_id = cur.lastrowid
                        stats["users"] += 1
                    else:
                        user_id = row["id"]

                    # bot:(owner_id, name) 唯一,已存在则跳过
                    brow = c.execute(
                        "SELECT id FROM bots WHERE owner_id=? AND name=?",
                        (user_id, name),
                    ).fetchone()
                    if brow is None:
                        bcur = c.execute(
                            "INSERT INTO bots(owner_id, name, display_name, "
                            "description, protocol, entry_file, runtime_lang, "
                            "is_builtin, is_public, is_active, created_at, "
                            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (user_id, name, u["display_name"] or name,
                             "", PROTO_TCP, "main.py", "python", 0, 1,
                             u["active"] if "active" in u.keys() else 1,
                             u["created_at"] or _now(),
                             u["created_at"] or _now()),
                        )
                        bot_id = bcur.lastrowid
                        stats["bots"] += 1
                    else:
                        bot_id = brow["id"]

                name_map[name] = {"user_id": user_id, "bot_id": bot_id}

        # ── 2) matches → 新 matches ───────────────────────────────────
        if _table_exists(legacy, "matches"):
            for m in legacy.execute("SELECT * FROM matches"):
                a = name_map.get(m["name_a"])
                b = name_map.get(m["name_b"])
                if a is None or b is None:
                    # 双方 bot 必须已建好;缺失则跳过该局(不应发生)
                    continue
                mid = m["match_id"]
                started = m["started_at"] or _now()
                ended = m["ended_at"] or started
                with store._tx() as c:  # noqa: SLF001
                    # INSERT OR REPLACE 实现幂等覆盖
                    c.execute(
                        "INSERT OR REPLACE INTO matches("
                        "id, bot_a_id, bot_b_id, owner_id, hands_played, "
                        "total_hands, earnings_a, earnings_b, winner, reason, "
                        "net_bb_a, match_type, status, protocol_a, protocol_b, "
                        "started_at, ended_at, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (mid, a["bot_id"], b["bot_id"], None,
                         m["hands_played"], m["total_hands"],
                         m["earnings_a"], m["earnings_b"],
                         m["winner"], m["reason"], m["net_bb_a"],
                         "exhibition", "completed", PROTO_TCP, PROTO_TCP,
                         started, ended, started),
                    )
                stats["matches"] += 1

        # ── 3) ratings → 新 ratings(bot_id 为主键) ──────────────────
        if _table_exists(legacy, "ratings"):
            for rt in legacy.execute("SELECT * FROM ratings"):
                entry = name_map.get(rt["name"])
                if entry is None:
                    continue
                bot_id = entry["bot_id"]
                store.upsert_rating(
                    bot_id,
                    rt["rating"], rt["rd"], rt["vol"],
                    wins=rt["wins"], losses=rt["losses"], draws=rt["draws"],
                    net_chips=rt["net_chips"],
                    matches_played=rt["matches_played"],
                    last_played_at=rt["last_played_at"],
                )
                stats["ratings"] += 1

        # ── 4) pair_stats → 新 pair_stats(bot_id 对为主键) ───────────
        if _table_exists(legacy, "pair_stats"):
            for ps in legacy.execute("SELECT * FROM pair_stats"):
                a = name_map.get(ps["name_a"])
                b = name_map.get(ps["name_b"])
                if a is None or b is None:
                    continue
                store.upsert_pair_stats(
                    a["bot_id"], b["bot_id"],
                    ps["bb_per_100_mean"],
                    ps["ci_low"], ps["ci_high"], ps["samples"],
                )
                stats["pair_stats"] += 1
    finally:
        legacy.close()

    return stats


# ── CLI 入口 ────────────────────────────────────────────────────────
def _main() -> None:
    parser = argparse.ArgumentParser(
        description="把旧 arena.db 的 bot 身份/对战/评分数据迁移到新平台库 "
                    "arena_platform.db。幂等,可重复执行。"
    )
    parser.add_argument(
        "--legacy", default="arena.db",
        help="旧库路径(默认 arena.db)",
    )
    parser.add_argument(
        "--platform", default="arena_platform.db",
        help="新平台库路径(默认 arena_platform.db)",
    )
    args = parser.parse_args()

    stats = migrate(args.legacy, args.platform)
    print("迁移完成,统计:")
    print(f"  users      : {stats['users']}")
    print(f"  bots       : {stats['bots']}")
    print(f"  matches    : {stats['matches']}")
    print(f"  ratings    : {stats['ratings']}")
    print(f"  pair_stats : {stats['pair_stats']}")


if __name__ == "__main__":
    _main()
