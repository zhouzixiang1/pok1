"""新平台 SQLite 存储层。

与旧 ``store/db.py``(TCP 通道)隔离,服务新平台的用户/bot/对战/回放/评分。
线程安全模型沿用旧库(threading.Lock + 每方法独立短连接),arena 单进程 asyncio,
DB 操作毫秒级 + 偶发写入,event loop 阻塞可忽略。

所有时间戳用 ISO 格式(秒精度),通过 ``_now()`` 统一。
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import (
    SCHEMA,
    TPL_RESET_PASSWORD,
    TPL_VERIFY_EMAIL,
    TPL_WELCOME,
)

DEFAULT_DB_PATH = Path("arena_platform.db")


def _now() -> str:
    """统一时间戳格式(ISO 秒精度)。"""
    return datetime.now().isoformat(timespec="seconds")


class Store:
    """新平台 SQLite 存储。线程安全(Lock);每方法独立短连接短事务。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextlib.contextmanager
    def _tx(self):
        """事务上下文:开外键 + commit + 关连接。"""
        with self._lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._tx() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            self._seed_email_templates(c)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """对已有库做增量列/表迁移(CREATE IF NOT EXISTS 不改旧表结构)。"""
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "email_verified" not in cols:
            c.execute(
                "ALTER TABLE users ADD COLUMN email_verified "
                "INTEGER NOT NULL DEFAULT 0")
            # 历史账号视为已验证,避免阻断现有用户
            c.execute("UPDATE users SET email_verified=1")

    def _seed_email_templates(self, c: sqlite3.Connection) -> None:
        defaults = [
            (TPL_VERIFY_EMAIL,
             "【pok-arena】邮箱验证码",
             "<p>你好 {{username}},</p>"
             "<p>你的邮箱验证码是 <strong>{{code}}</strong>,"
             "{{expires_minutes}} 分钟内有效。</p>"
             "<p>如非本人操作请忽略本邮件。</p>",
             "你好 {{username}},\n你的邮箱验证码是 {{code}},"
             "{{expires_minutes}} 分钟内有效。\n如非本人操作请忽略。"),
            (TPL_RESET_PASSWORD,
             "【pok-arena】密码重置验证码",
             "<p>你好 {{username}},</p>"
             "<p>你正在重置密码,验证码 <strong>{{code}}</strong>,"
             "{{expires_minutes}} 分钟内有效。</p>"
             "<p>如非本人操作请立即忽略并检查账号安全。</p>",
             "你好 {{username}},\n你正在重置密码,验证码 {{code}},"
             "{{expires_minutes}} 分钟内有效。"),
            (TPL_WELCOME,
             "【pok-arena】欢迎加入",
             "<p>你好 {{username}},欢迎加入 pok-arena 德州扑克对战平台!</p>"
             "<p>请先完成邮箱验证,然后上传 bot 并发起对战。</p>",
             "你好 {{username}},欢迎加入 pok-arena!\n请先完成邮箱验证。"),
        ]
        now = _now()
        for key, subject, html, text in defaults:
            c.execute(
                "INSERT OR IGNORE INTO email_templates"
                "(key, subject, body_html, body_text, updated_at) "
                "VALUES(?,?,?,?,?)",
                (key, subject, html, text, now))

    # ══════════════════════════════════════════════════════════
    # 用户(真实账号)— 里程碑 2 认证用
    # ══════════════════════════════════════════════════════════

    def create_user(self, username: str, email: str, password_hash: str,
                    *, display_name: str = "", role: str = "user") -> dict:
        """注册用户。username/email 唯一冲突抛 IntegrityError。返回新建用户。"""
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO users(username, email, password_hash, role, "
                "display_name, created_at) VALUES(?,?,?,?,?,?)",
                (username, email, password_hash, role,
                 display_name or username, _now()))
            uid = cur.lastrowid
            return self._row_to_dict(c.execute(
                "SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def get_user(self, user_id: int) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return self._row_to_dict(r)

    def get_user_by_username(self, username: str) -> dict | None:
        with self._tx() as c:
            r = c.execute(
                "SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return self._row_to_dict(r)

    def get_user_by_email(self, email: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            return self._row_to_dict(r)

    def update_user(self, user_id: int, **fields) -> dict | None:
        """可更新字段含 email_verified。"""
        allowed = {"password_hash", "email", "display_name", "role",
                   "is_active", "last_login_at", "email_verified"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        if not sets:
            return self.get_user(user_id)
        vals.append(user_id)
        with self._tx() as c:
            c.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)
            return self._row_to_dict(c.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def list_users(self, *, role: str | None = None,
                   active_only: bool = False) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM users WHERE 1=1"
            params: list[Any] = []
            if role:
                sql += " AND role=?"
                params.append(role)
            if active_only:
                sql += " AND is_active=1"
            sql += " ORDER BY created_at"
            return [self._row_to_dict(r) for r in c.execute(sql, params)]

    def delete_user(self, user_id: int) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount > 0

    # ══════════════════════════════════════════════════════════
    # Bot(用户上传)— 里程碑 3 上传用
    # ══════════════════════════════════════════════════════════

    def create_bot(self, owner_id: int, name: str, *, protocol: str = "json",
                   entry_file: str = "main.py", runtime_lang: str = "python",
                   display_name: str = "", description: str = "",
                   is_builtin: bool = False, is_public: bool = True) -> dict:
        """创建 bot 记录。owner+name 唯一冲突抛 IntegrityError。"""
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO bots(owner_id, name, display_name, description, "
                "protocol, entry_file, runtime_lang, is_builtin, is_public, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (owner_id, name, display_name or name, description,
                 protocol, entry_file, runtime_lang,
                 1 if is_builtin else 0, 1 if is_public else 0, _now(), _now()))
            bid = cur.lastrowid
            return self._row_to_dict(c.execute(
                "SELECT * FROM bots WHERE id=?", (bid,)).fetchone())

    def get_bot(self, bot_id: int) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            return self._row_to_dict(r)

    def get_bot_by_owner_name(self, owner_id: int, name: str) -> dict | None:
        with self._tx() as c:
            r = c.execute(
                "SELECT * FROM bots WHERE owner_id=? AND name=?",
                (owner_id, name)).fetchone()
            return self._row_to_dict(r)

    def update_bot(self, bot_id: int, **fields) -> dict | None:
        """可更新:display_name/description/docker_image/source_path/
        current_version/is_public/is_active/entry_file/protocol。"""
        allowed = {"display_name", "description", "docker_image", "source_path",
                   "current_version", "is_public", "is_active",
                   "entry_file", "protocol", "updated_at"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        if not sets:
            return self.get_bot(bot_id)
        # updated_at 自动刷新
        if "updated_at" not in fields:
            sets.append("updated_at=?")
            vals.append(_now())
        vals.append(bot_id)
        with self._tx() as c:
            c.execute(f"UPDATE bots SET {','.join(sets)} WHERE id=?", vals)
            return self._row_to_dict(c.execute(
                "SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone())

    def list_bots(self, *, owner_id: int | None = None,
                  public_only: bool = False, active_only: bool = True,
                  include_builtin: bool = True) -> list[dict]:
        """列 bot(选对手/我的 bot 用)。默认含内置 bot、仅上架。"""
        with self._tx() as c:
            sql = "SELECT * FROM bots WHERE 1=1"
            params: list[Any] = []
            if owner_id is not None:
                sql += " AND owner_id=?"
                params.append(owner_id)
            if public_only:
                sql += " AND is_public=1"
            if active_only:
                sql += " AND is_active=1"
            if not include_builtin:
                sql += " AND is_builtin=0"
            sql += " ORDER BY is_builtin DESC, name"
            return [self._row_to_dict(r) for r in c.execute(sql, params)]

    def delete_bot(self, bot_id: int) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM bots WHERE id=?", (bot_id,)).rowcount > 0

    # ══════════════════════════════════════════════════════════
    # Bot 版本历史
    # ══════════════════════════════════════════════════════════

    def add_bot_version(self, bot_id: int, *, source_path: str,
                        docker_image: str = "", upload_note: str = "",
                        checksum: str = "") -> dict:
        """新增版本号自增。返回新版本记录。"""
        with self._tx() as c:
            row = c.execute(
                "SELECT MAX(version) AS mv FROM bot_versions WHERE bot_id=?",
                (bot_id,)).fetchone()
            next_ver = (row["mv"] or 0) + 1
            cur = c.execute(
                "INSERT INTO bot_versions(bot_id, version, source_path, "
                "docker_image, upload_note, checksum, uploaded_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (bot_id, next_ver, source_path, docker_image,
                 upload_note, checksum, _now()))
            vid = cur.lastrowid
            # 更新 bot 当前版本
            c.execute("UPDATE bots SET current_version=?, updated_at=? WHERE id=?",
                      (next_ver, _now(), bot_id))
            return self._row_to_dict(c.execute(
                "SELECT * FROM bot_versions WHERE id=?", (vid,)).fetchone())

    def list_bot_versions(self, bot_id: int) -> list[dict]:
        with self._tx() as c:
            return [self._row_to_dict(r) for r in c.execute(
                "SELECT * FROM bot_versions WHERE bot_id=? ORDER BY version DESC",
                (bot_id,))]

    # ══════════════════════════════════════════════════════════
    # 对局记录
    # ══════════════════════════════════════════════════════════

    def create_match(self, match_id: str, bot_a_id: int, bot_b_id: int, *,
                     owner_id: int | None = None, total_hands: int = 70,
                     match_type: str = "challenge",
                     protocol_a: str = "json", protocol_b: str = "json") -> dict:
        with self._tx() as c:
            c.execute(
                "INSERT INTO matches(id, bot_a_id, bot_b_id, owner_id, "
                "total_hands, match_type, protocol_a, protocol_b, status, "
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (match_id, bot_a_id, bot_b_id, owner_id, total_hands,
                 match_type, protocol_a, protocol_b, "pending", _now()))
            return self._row_to_dict(c.execute(
                "SELECT * FROM matches WHERE id=?", (match_id,)).fetchone())

    def get_match(self, match_id: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
            return self._row_to_dict(r)

    def update_match(self, match_id: str, **fields) -> dict | None:
        """可更新:hands_played/earnings_a/earnings_b/winner/reason/
        net_bb_a/status/started_at/ended_at。"""
        allowed = {"hands_played", "earnings_a", "earnings_b", "winner",
                   "reason", "net_bb_a", "status", "started_at", "ended_at"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        if not sets:
            return self.get_match(match_id)
        vals.append(match_id)
        with self._tx() as c:
            c.execute(f"UPDATE matches SET {','.join(sets)} WHERE id=?", vals)
            return self._row_to_dict(c.execute(
                "SELECT * FROM matches WHERE id=?", (match_id,)).fetchone())

    def list_matches(self, *, owner_id: int | None = None,
                     bot_id: int | None = None, status: str | None = None,
                     limit: int = 50, offset: int = 0) -> list[dict]:
        with self._tx() as c:
            sql = ("SELECT m.*, ba.name AS bot_a_name, bb.name AS bot_b_name, "
                   "ba.display_name AS bot_a_display, bb.display_name AS bot_b_display "
                   "FROM matches m "
                   "JOIN bots ba ON m.bot_a_id=ba.id "
                   "JOIN bots bb ON m.bot_b_id=bb.id WHERE 1=1")
            params: list[Any] = []
            if owner_id is not None:
                sql += " AND m.owner_id=?"
                params.append(owner_id)
            if bot_id is not None:
                sql += " AND (m.bot_a_id=? OR m.bot_b_id=?)"
                params.extend([bot_id, bot_id])
            if status:
                sql += " AND m.status=?"
                params.append(status)
            sql += " ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            return [self._row_to_dict(r) for r in c.execute(sql, params)]

    def count_matches(self, *, owner_id: int | None = None,
                      bot_id: int | None = None,
                      status: str | None = None) -> int:
        with self._tx() as c:
            sql = "SELECT COUNT(*) FROM matches WHERE 1=1"
            params: list[Any] = []
            if owner_id is not None:
                sql += " AND owner_id=?"
                params.append(owner_id)
            if bot_id is not None:
                sql += " AND (bot_a_id=? OR bot_b_id=?)"
                params.extend([bot_id, bot_id])
            if status:
                sql += " AND status=?"
                params.append(status)
            r = c.execute(sql, params).fetchone()
            return r[0] if r else 0

    # ══════════════════════════════════════════════════════════
    # 对局回放(事件流 JSON)
    # ══════════════════════════════════════════════════════════

    def save_replay(self, match_id: str, *, events_json: str = "[]",
                    hands_json: str = "[]", thp_text: str = "") -> None:
        """upsert 回放。事件流逐手累积追加时用此覆盖。"""
        with self._tx() as c:
            c.execute(
                "INSERT INTO match_replays(match_id, events_json, hands_json, "
                "thp_text, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET "
                "events_json=excluded.events_json, hands_json=excluded.hands_json, "
                "thp_text=excluded.thp_text, updated_at=excluded.updated_at",
                (match_id, events_json, hands_json, thp_text, _now()))

    def append_replay_event(self, match_id: str, event: dict) -> None:
        """追加单个事件到 events_json(增量,避免每次重传全量)。"""
        import json
        with self._tx() as c:
            r = c.execute(
                "SELECT events_json FROM match_replays WHERE match_id=?",
                (match_id,)).fetchone()
            events = []
            if r:
                try:
                    events = json.loads(r["events_json"])
                except (json.JSONDecodeError, TypeError):
                    events = []
            events.append(event)
            c.execute(
                "INSERT INTO match_replays(match_id, events_json, updated_at) "
                "VALUES(?,?,?) ON CONFLICT(match_id) DO UPDATE SET "
                "events_json=excluded.events_json, updated_at=excluded.updated_at",
                (match_id, json.dumps(events, ensure_ascii=False), _now()))

    def get_replay(self, match_id: str) -> dict | None:
        with self._tx() as c:
            r = c.execute(
                "SELECT * FROM match_replays WHERE match_id=?",
                (match_id,)).fetchone()
            return self._row_to_dict(r)

    # ══════════════════════════════════════════════════════════
    # Glicko-2 评分(算法在 rating/glicko2.py,此处只存取)
    # ══════════════════════════════════════════════════════════

    def get_rating(self, bot_id: int) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM ratings WHERE bot_id=?", (bot_id,)).fetchone()
            return self._row_to_dict(r)

    def upsert_rating(self, bot_id: int, rating: float, rd: float, vol: float,
                      *, wins: int = 0, losses: int = 0, draws: int = 0,
                      net_chips: int = 0, matches_played: int = 0,
                      last_played_at: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO ratings(bot_id, rating, rd, vol, wins, losses, "
                "draws, net_chips, matches_played, last_played_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bot_id) DO UPDATE SET rating=excluded.rating, "
                "rd=excluded.rd, vol=excluded.vol, wins=excluded.wins, "
                "losses=excluded.losses, draws=excluded.draws, "
                "net_chips=excluded.net_chips, "
                "matches_played=excluded.matches_played, "
                "last_played_at=excluded.last_played_at",
                (bot_id, rating, rd, vol, wins, losses, draws,
                 net_chips, matches_played, last_played_at or _now()))

    def leaderboard(self, limit: int = 50) -> list[dict]:
        """天梯:JOIN bots + users 取展示名,按 rating 降序。"""
        with self._tx() as c:
            rows = c.execute(
                "SELECT r.bot_id, r.rating, r.rd, r.vol, r.wins, r.losses, "
                "r.draws, r.net_chips, r.matches_played, r.last_played_at, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                "b.protocol, b.is_builtin, u.username AS owner_name, "
                "u.display_name AS owner_display "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "WHERE b.is_active=1 "
                "ORDER BY r.rating DESC LIMIT ?", (limit,))
            return [self._row_to_dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════
    # 每对 bb/100 CI
    # ══════════════════════════════════════════════════════════

    def upsert_pair_stats(self, bot_a_id: int, bot_b_id: int,
                          bb_per_100_mean: float, ci_low: float | None,
                          ci_high: float | None, samples: int) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO pair_stats(bot_a_id, bot_b_id, bb_per_100_mean, "
                "ci_low, ci_high, samples, last_played_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(bot_a_id, bot_b_id) DO UPDATE SET "
                "bb_per_100_mean=excluded.bb_per_100_mean, ci_low=excluded.ci_low, "
                "ci_high=excluded.ci_high, samples=excluded.samples, "
                "last_played_at=excluded.last_played_at",
                (bot_a_id, bot_b_id, bb_per_100_mean, ci_low, ci_high,
                 samples, _now()))

    def pair_stats_for(self, bot_id: int) -> list[dict]:
        """该 bot 对各对手的 bb/100(两个方向均含)。"""
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM pair_stats WHERE bot_a_id=? OR bot_b_id=? "
                "ORDER BY last_played_at DESC", (bot_id, bot_id))
            return [self._row_to_dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════
    # 会话(session)— 里程碑 2 认证用
    # ══════════════════════════════════════════════════════════

    def add_session(self, token: str, user_id: int, expires_at: str, *,
                    ip_addr: str = "", user_agent: str = "") -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO sessions(token, user_id, expires_at, "
                "created_at, ip_addr, user_agent) VALUES(?,?,?,?,?,?)",
                (token, user_id, expires_at, _now(), ip_addr, user_agent))

    def get_session(self, token: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
            return self._row_to_dict(r)

    def delete_session(self, token: str) -> bool:
        with self._tx() as c:
            return c.execute(
                "DELETE FROM sessions WHERE token=?", (token,)).rowcount > 0

    def delete_user_sessions(self, user_id: int) -> int:
        """登出该用户所有会话(改密码/封禁时用)。"""
        with self._tx() as c:
            cur = c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return cur.rowcount

    # ══════════════════════════════════════════════════════════
    # 密码重置 token
    # ══════════════════════════════════════════════════════════

    def add_password_reset(self, token: str, user_id: int, expires_at: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO password_resets(token, user_id, expires_at, "
                "created_at) VALUES(?,?,?,?)",
                (token, user_id, expires_at, _now()))

    def get_password_reset(self, token: str) -> dict | None:
        with self._tx() as c:
            r = c.execute(
                "SELECT * FROM password_resets WHERE token=? AND used_at IS NULL",
                (token,)).fetchone()
            return self._row_to_dict(r)

    def mark_password_reset_used(self, token: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE password_resets SET used_at=? WHERE token=?", (_now(), token))

    # ══════════════════════════════════════════════════════════
    # 邮箱验证码 / 模板 / 出站审计
    # ══════════════════════════════════════════════════════════

    def add_email_code(self, user_id: int, purpose: str, code: str,
                       expires_at: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_codes(user_id, purpose, code, expires_at, "
                "created_at) VALUES(?,?,?,?,?)",
                (user_id, purpose, code, expires_at, _now()))

    def get_latest_email_code(self, user_id: int, purpose: str) -> dict | None:
        with self._tx() as c:
            r = c.execute(
                "SELECT * FROM email_codes WHERE user_id=? AND purpose=? "
                "AND used_at IS NULL ORDER BY id DESC LIMIT 1",
                (user_id, purpose)).fetchone()
            return self._row_to_dict(r)

    def mark_email_code_used(self, code_id: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE email_codes SET used_at=? WHERE id=?", (_now(), code_id))

    def list_email_templates(self) -> list[dict]:
        with self._tx() as c:
            return [self._row_to_dict(r) for r in c.execute(
                "SELECT * FROM email_templates ORDER BY key")]

    def get_email_template(self, key: str) -> dict | None:
        with self._tx() as c:
            r = c.execute(
                "SELECT * FROM email_templates WHERE key=?", (key,)).fetchone()
            return self._row_to_dict(r)

    def upsert_email_template(self, key: str, *, subject: str,
                              body_html: str, body_text: str) -> dict:
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_templates"
                "(key, subject, body_html, body_text, updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "subject=excluded.subject, body_html=excluded.body_html, "
                "body_text=excluded.body_text, updated_at=excluded.updated_at",
                (key, subject, body_html, body_text, _now()))
            return self._row_to_dict(c.execute(
                "SELECT * FROM email_templates WHERE key=?", (key,)).fetchone())

    def add_email_outbox(self, to_addr: str, subject: str, *,
                         template_key: str = "", status: str = "sent",
                         error: str = "") -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_outbox"
                "(to_addr, subject, template_key, status, error, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (to_addr, subject, template_key, status, error, _now()))

    def list_email_outbox(self, *, limit: int = 50) -> list[dict]:
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM email_outbox ORDER BY id DESC LIMIT ?",
                (limit,))
            return [self._row_to_dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════
    # 辅助
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None
