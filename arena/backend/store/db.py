"""SQLite 存储层:arena.db。

表:
- users       bot 用户(身份:name/display/team/note/secret/active)
- matches     对战记录(双方/手数/earnings/winner/reason/net_bb/thp_file/log_dir/时间)
- ratings     Glicko-2 评分(rating/rd/vol + W/L/D + net_chips + matches_played)
- pair_stats  每对 (A,B) 的 bb/100 + 95% CI(mbb/g 真值锚)
- admins      管理员(username/password_hash)
- sessions    登录会话(token/expires)

stdlib sqlite3 同步:arena 单进程 asyncio,DB 操作毫秒级 + 每场结束才写入,
event loop 阻塞可忽略;Store 用 threading.Lock 保证线程安全。默认 arena.db 在当前目录。
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DEFAULT_DB_PATH = Path("arena.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    team TEXT DEFAULT '',
    note TEXT DEFAULT '',
    secret TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
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
CREATE TABLE IF NOT EXISTS ratings (
    name TEXT PRIMARY KEY REFERENCES users(name),
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
CREATE TABLE IF NOT EXISTS pair_stats (
    name_a TEXT NOT NULL,
    name_b TEXT NOT NULL,
    bb_per_100_mean REAL NOT NULL,
    ci_low REAL,
    ci_high REAL,
    samples INTEGER NOT NULL,
    last_played_at TEXT NOT NULL,
    PRIMARY KEY (name_a, name_b)
);
CREATE TABLE IF NOT EXISTS admins (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_a ON matches(name_a);
CREATE INDEX IF NOT EXISTS idx_matches_b ON matches(name_b);
CREATE INDEX IF NOT EXISTS idx_matches_time ON matches(started_at);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    """SQLite 存储层。线程安全(Lock);每方法独立短连接短事务。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextlib.contextmanager
    def _tx(self):
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
            c.executescript(_SCHEMA)

    # ── users(bot 用户身份)────────────────────────────────
    def ensure_user(self, name: str, display_name: str | None = None,
                    team: str = "", note: str = "") -> bool:
        """注册 bot 用户(首次见到)。返回是否新建。display_name 默认 = name。"""
        with self._tx() as c:
            if c.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
                return False
            c.execute(
                "INSERT INTO users(name, display_name, team, note, secret, active, "
                "created_at, first_seen_at) VALUES(?,?,?,?,?,?,?,?)",
                (name, display_name or name, team, note, "", 1, _now(), _now()))
            return True

    def get_user(self, name: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
            return dict(r) if r else None

    def list_users(self, active_only: bool = False) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM users"
            if active_only:
                sql += " WHERE active=1"
            return [dict(r) for r in c.execute(sql + " ORDER BY name")]

    def update_user(self, name: str, **fields) -> bool:
        """更新 bot 用户字段(display_name/team/note/secret/active)。"""
        allowed = {"display_name", "team", "note", "secret", "active"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        if not sets:
            return False
        vals.append(name)
        with self._tx() as c:
            return c.execute(
                f"UPDATE users SET {','.join(sets)} WHERE name=?", vals).rowcount > 0

    def delete_user(self, name: str) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM users WHERE name=?", (name,)).rowcount > 0

    # ── matches(对战记录)──────────────────────────────────
    def insert_match(self, m: dict) -> None:
        cols = ("match_id,name_a,name_b,hands_played,total_hands,earnings_a,"
                "earnings_b,winner,reason,net_bb_a,thp_file,log_dir,started_at,ended_at")
        keys = cols.split(",")
        with self._tx() as c:
            c.execute(
                f"INSERT OR REPLACE INTO matches({cols}) VALUES({','.join('?' * 14)})",
                tuple(m.get(k) for k in keys))

    def get_match(self, match_id: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM matches WHERE match_id=?", (match_id,)).fetchone()
            return dict(r) if r else None

    def list_matches(self, user: str | None = None, limit: int = 50,
                     offset: int = 0) -> list[dict]:
        with self._tx() as c:
            if user:
                rows = c.execute(
                    "SELECT * FROM matches WHERE name_a=? OR name_b=? "
                    "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (user, user, limit, offset))
            else:
                rows = c.execute(
                    "SELECT * FROM matches ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (limit, offset))
            return [dict(r) for r in rows]

    def count_matches(self, user: str | None = None) -> int:
        with self._tx() as c:
            if user:
                r = c.execute(
                    "SELECT COUNT(*) FROM matches WHERE name_a=? OR name_b=?",
                    (user, user)).fetchone()
            else:
                r = c.execute("SELECT COUNT(*) FROM matches").fetchone()
            return r[0] if r else 0

    # ── ratings(Glicko-2)──────────────────────────────────
    def get_rating(self, name: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM ratings WHERE name=?", (name,)).fetchone()
            return dict(r) if r else None

    def upsert_rating(self, name: str, rating: float, rd: float, vol: float,
                      wins: int = 0, losses: int = 0, draws: int = 0,
                      net_chips: int = 0, matches_played: int = 0,
                      last_played_at: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO ratings(name, rating, rd, vol, wins, losses, draws, "
                "net_chips, matches_played, last_played_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET rating=excluded.rating, rd=excluded.rd, "
                "vol=excluded.vol, wins=excluded.wins, losses=excluded.losses, "
                "draws=excluded.draws, net_chips=excluded.net_chips, "
                "matches_played=excluded.matches_played, "
                "last_played_at=excluded.last_played_at",
                (name, rating, rd, vol, wins, losses, draws, net_chips,
                 matches_played, last_played_at or _now()))

    def leaderboard(self, limit: int = 50) -> list[dict]:
        with self._tx() as c:
            rows = c.execute(
                "SELECT r.name, r.rating, r.rd, r.vol, r.wins, r.losses, r.draws, "
                "r.net_chips, r.matches_played, r.last_played_at, "
                "u.display_name, u.team "
                "FROM ratings r LEFT JOIN users u ON r.name=u.name "
                "ORDER BY r.rating DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    # ── pair_stats(mbb/g bb/100 + CI 真值锚)───────────────
    def upsert_pair_stats(self, name_a: str, name_b: str, bb_per_100_mean: float,
                          ci_low: float, ci_high: float, samples: int) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO pair_stats(name_a, name_b, bb_per_100_mean, ci_low, "
                "ci_high, samples, last_played_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(name_a, name_b) DO UPDATE SET "
                "bb_per_100_mean=excluded.bb_per_100_mean, ci_low=excluded.ci_low, "
                "ci_high=excluded.ci_high, samples=excluded.samples, "
                "last_played_at=excluded.last_played_at",
                (name_a, name_b, bb_per_100_mean, ci_low, ci_high, samples, _now()))

    def pair_stats_for(self, name: str) -> list[dict]:
        """该 bot 对各对手的 bb/100(两个方向均含)。"""
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM pair_stats WHERE name_a=? OR name_b=? "
                "ORDER BY last_played_at DESC", (name, name))
            return [dict(r) for r in rows]

    # ── admins / sessions(auth 阶段用)─────────────────────
    def get_admin(self, username: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
            return dict(r) if r else None

    def upsert_admin(self, username: str, password_hash: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO admins(username, password_hash, created_at) VALUES(?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash",
                (username, password_hash, _now()))

    def add_session(self, token: str, username: str, expires_at: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO sessions(token, username, expires_at) VALUES(?,?,?)",
                (token, username, expires_at))

    def get_session(self, token: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
            return dict(r) if r else None

    def delete_session(self, token: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
