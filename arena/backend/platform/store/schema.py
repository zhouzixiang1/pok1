"""新平台 SQLite schema 定义。

与旧 ``store/db.py``(服务 TCP 通道,保留不动)**完全隔离**:新平台用独立 db
文件 ``arena_platform.db``,从零设计,不背旧表语义包袱。旧 ``arena.db`` 的历史
对战数据通过 ``migrate_from_legacy.py`` 导入。

设计原则:
- **用户/bot 分离**:`users` 存真实账号,`bots` 存用户的 bot(一个用户可多个 bot),
  区别于旧库把「bot 名」当主键混在 users 表。
- **多协议支持**:`bots.protocol` 标记 'json'(BotZone stdin)或 'tcp'(国赛 socket)。
- **版本管理**:每次上传 bot 生成新 `bot_versions`,排行榜只评最新版。
- **外键全部 ON**:matches/ratings/pair_stats 外键到 bots.id。
- **回放独立表**:`match_replays` 存完整事件流 JSON,与 matches 元数据分离。

表关系:
    users 1───* bots 1───* bot_versions
                 │
                 ├── 1───* matches(as bot_a / bot_b)
                 │           │
                 │           └── 1───1 match_replays
                 └── 1───* ratings / pair_stats
    users 1───* sessions / password_resets
"""
from __future__ import annotations

# 完整 schema(CREATE TABLE IF NOT EXISTS,可幂等执行)
# 注意:SQLite 外键需 PRAGMA foreign_keys=ON(Store._tx 已开启)

SCHEMA = """
-- ════════════════════════════════════════════════════════════
-- 用户(真实账号)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,          -- 登录名(3-32 字符,字母数字_)
    email           TEXT    NOT NULL UNIQUE,          -- 邮箱(重置密码用)
    password_hash   TEXT    NOT NULL,                 -- pbkdf2_sha256$iters$salt$hash
    role            TEXT    NOT NULL DEFAULT 'user',  -- 'user' | 'admin'
    display_name    TEXT    NOT NULL DEFAULT '',      -- 昵称(可中文)
    is_active       INTEGER NOT NULL DEFAULT 1,       -- 0=封禁
    email_verified  INTEGER NOT NULL DEFAULT 0,       -- 0=未验证邮箱,禁止登录
    created_at      TEXT    NOT NULL,
    last_login_at   TEXT,
    CONSTRAINT chk_username CHECK (length(username) >= 3),
    CONSTRAINT chk_role     CHECK (role IN ('user', 'admin'))
);

-- ════════════════════════════════════════════════════════════
-- Bot(用户上传的程序)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS bots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,                 -- bot 名(同一用户内唯一)
    display_name    TEXT    NOT NULL DEFAULT '',
    description     TEXT    NOT NULL DEFAULT '',
    protocol        TEXT    NOT NULL DEFAULT 'json',  -- 'json'(stdin)| 'tcp'(socket)
    entry_file      TEXT    NOT NULL DEFAULT 'main.py', -- 入口文件
    runtime_lang    TEXT    NOT NULL DEFAULT 'python', -- 'python'|'cpp'|'java'
    docker_image    TEXT    NOT NULL DEFAULT '',      -- 构建出的镜像名(里程碑3填)
    source_path     TEXT    NOT NULL DEFAULT '',      -- 源码包路径(bot_uploads/)
    current_version INTEGER NOT NULL DEFAULT 0,       -- 当前生效版本号
    is_builtin      INTEGER NOT NULL DEFAULT 0,       -- 1=平台预置(national_v*)
    is_public       INTEGER NOT NULL DEFAULT 1,       -- 1=他人可选为对手
    is_active       INTEGER NOT NULL DEFAULT 1,       -- 0=下架
    argv_style      TEXT    NOT NULL DEFAULT 'flags', -- TCP bot 连接参数风格:flags(--host/--port)|positional(host port name)|env(只读 GUOSAI_*)
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(owner_id, name),
    CONSTRAINT chk_protocol CHECK (protocol IN ('json', 'tcp')),
    CONSTRAINT chk_lang     CHECK (runtime_lang IN ('python', 'cpp', 'java')),
    CONSTRAINT chk_argv     CHECK (argv_style IN ('flags', 'positional', 'env'))
);

-- ════════════════════════════════════════════════════════════
-- Bot 版本历史(每次上传一条)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS bot_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,                 -- 版本号(从1递增)
    source_path     TEXT    NOT NULL,                 -- 该版本源码包路径
    docker_image    TEXT    NOT NULL DEFAULT '',      -- 该版本镜像(可能为空)
    upload_note     TEXT    NOT NULL DEFAULT '',      -- 用户填的版本说明
    checksum        TEXT    NOT NULL DEFAULT '',      -- sha256(防重复上传)
    uploaded_at     TEXT    NOT NULL,
    UNIQUE(bot_id, version)
);

-- ════════════════════════════════════════════════════════════
-- 对局记录
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS matches (
    id              TEXT    PRIMARY KEY,              -- match_id(时间戳+双方名)
    bot_a_id        INTEGER NOT NULL REFERENCES bots(id),
    bot_b_id        INTEGER NOT NULL REFERENCES bots(id),
    owner_id        INTEGER REFERENCES users(id) ON DELETE SET NULL, -- 发起者;用户注销后置 NULL 保留历史
    hands_played    INTEGER NOT NULL DEFAULT 0,
    total_hands     INTEGER NOT NULL DEFAULT 70,
    earnings_a      INTEGER NOT NULL DEFAULT 0,       -- A 净筹码(累计)
    earnings_b      INTEGER NOT NULL DEFAULT 0,
    winner          INTEGER,                          -- 0=A赢/1=B赢/NULL=平局
    reason          TEXT    NOT NULL DEFAULT 'completed', -- completed|disconnected|error|aborted
    net_bb_a        REAL    NOT NULL DEFAULT 0,
    match_type      TEXT    NOT NULL DEFAULT 'challenge', -- challenge|ladder|exhibition
    status          TEXT    NOT NULL DEFAULT 'pending',   -- pending|running|completed|aborted
    protocol_a      TEXT    NOT NULL,                 -- 冗余存当时协议(便于回放)
    protocol_b      TEXT    NOT NULL,
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT    NOT NULL,
    CONSTRAINT chk_winner CHECK (winner IN (0, 1) OR winner IS NULL),
    CONSTRAINT chk_status CHECK (status IN ('pending','running','completed','aborted')),
    CONSTRAINT chk_type   CHECK (match_type IN ('challenge','ladder','exhibition'))
);

-- ════════════════════════════════════════════════════════════
-- 对局回放(完整事件流 JSON,逐手可推进)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS match_replays (
    match_id        TEXT    PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
    events_json     TEXT    NOT NULL DEFAULT '[]',    -- 完整事件流(hand_start/action/settle...)
    hands_json      TEXT    NOT NULL DEFAULT '[]',    -- 逐手快照(里程碑7回放器用)
    thp_text        TEXT    NOT NULL DEFAULT '',      -- 国赛 THP 格式(导出用)
    updated_at      TEXT    NOT NULL
);

-- ════════════════════════════════════════════════════════════
-- Glicko-2 评分(每个 bot 一行,算法复用 rating/glicko2.py)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS ratings (
    bot_id          INTEGER PRIMARY KEY REFERENCES bots(id) ON DELETE CASCADE,
    rating          REAL    NOT NULL DEFAULT 1500.0,
    rd              REAL    NOT NULL DEFAULT 350.0,
    vol             REAL    NOT NULL DEFAULT 0.06,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    draws           INTEGER NOT NULL DEFAULT 0,
    net_chips       INTEGER NOT NULL DEFAULT 0,
    matches_played  INTEGER NOT NULL DEFAULT 0,
    last_played_at  TEXT
);

-- ════════════════════════════════════════════════════════════
-- 每对 (A,B) 的 bb/100 + 95% CI(ACPC mbb/g 真值锚)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS pair_stats (
    bot_a_id        INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    bot_b_id        INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    bb_per_100_mean REAL    NOT NULL DEFAULT 0,
    ci_low          REAL,
    ci_high         REAL,
    samples         INTEGER NOT NULL DEFAULT 0,
    last_played_at  TEXT    NOT NULL,
    PRIMARY KEY (bot_a_id, bot_b_id)
);

-- ════════════════════════════════════════════════════════════
-- 登录会话(cookie token)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    ip_addr         TEXT    NOT NULL DEFAULT '',
    user_agent      TEXT    NOT NULL DEFAULT ''
);

-- ════════════════════════════════════════════════════════════
-- 密码重置(一次性 token;邮件验证码为主,admin 兜底仍可用 token)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS password_resets (
    token           TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at      TEXT    NOT NULL,
    used_at         TEXT,
    created_at      TEXT    NOT NULL
);

-- ════════════════════════════════════════════════════════════
-- 邮箱验证码(注册验证 / 密码重置)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS email_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose         TEXT    NOT NULL,                 -- verify | reset
    code            TEXT    NOT NULL,                 -- 6 位数字
    expires_at      TEXT    NOT NULL,
    used_at         TEXT,
    created_at      TEXT    NOT NULL,
    CONSTRAINT chk_purpose CHECK (purpose IN ('verify', 'reset'))
);

-- ════════════════════════════════════════════════════════════
-- 邮件模板(管理员可编辑)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS email_templates (
    key             TEXT    PRIMARY KEY,              -- verify_email|reset_password|welcome
    subject         TEXT    NOT NULL,
    body_html       TEXT    NOT NULL DEFAULT '',
    body_text       TEXT    NOT NULL DEFAULT '',
    updated_at      TEXT    NOT NULL
);

-- ════════════════════════════════════════════════════════════
-- 出站邮件审计(轻量)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS email_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    to_addr         TEXT    NOT NULL,
    subject         TEXT    NOT NULL,
    template_key    TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'sent',  -- sent|failed
    error           TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL
);

-- ════════════════════════════════════════════════════════════
-- 索引
-- ════════════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_bots_owner       ON bots(owner_id);
CREATE INDEX IF NOT EXISTS idx_bot_versions_bot ON bot_versions(bot_id);
CREATE INDEX IF NOT EXISTS idx_matches_bot_a    ON matches(bot_a_id);
CREATE INDEX IF NOT EXISTS idx_matches_bot_b    ON matches(bot_b_id);
CREATE INDEX IF NOT EXISTS idx_matches_owner    ON matches(owner_id);
CREATE INDEX IF NOT EXISTS idx_matches_status   ON matches(status);
CREATE INDEX IF NOT EXISTS idx_matches_time     ON matches(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_email_codes_user ON email_codes(user_id, purpose);
"""


# 常量:角色
ROLE_USER = "user"
ROLE_ADMIN = "admin"

# 常量:协议
PROTO_JSON = "json"
PROTO_TCP = "tcp"

# 常量:TCP bot 连接参数风格(容器内桥 spawn bot 的 argv 形式)
ARGV_FLAGS = "flags"            # --host/--port/--name 旗标(内置 national_v* 风格)
ARGV_POSITIONAL = "positional"  # 位置参数 host port name(uploads 风格)
ARGV_ENV = "env"                # 不传 argv,只设 GUOSAI_* 环境变量

# 常量:对局状态
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"

# 常量:对局类型
TYPE_CHALLENGE = "challenge"      # 用户主动选对手
TYPE_LADDER = "ladder"            # 天梯排位(里程碑6)
TYPE_EXHIBITION = "exhibition"    # 表演赛

# 邮件模板 key
TPL_VERIFY_EMAIL = "verify_email"
TPL_RESET_PASSWORD = "reset_password"
TPL_WELCOME = "welcome"

# 邮箱验证码用途
CODE_VERIFY = "verify"
CODE_RESET = "reset"