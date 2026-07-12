# arena 平台扩展方案:日志 + 用户身份 + 天梯 + 历史 + 用户管理 + 认证

> 2026-07-12 定稿并完成。从"单场观赛平台"升级为"持久化对战平台"。
> **状态:8 阶段全部实现 + 端到端验证通过(serve + 前端托管 + API + admin 全链路)。**
> 评分经 3 方调研(国赛官方 / ACPC 学术竞技 / PokerBench 扑克天梯)定稿。

## 一、评分算法(贴合德扑实际)

### 调研依据
- **国赛官方**:`德州扑克规则.doc` L21-24 — 一场胜负 = 70 局累计筹码比较(W/L/D,D 仅严格相等);出错/断线 → 强制 L。**官方无赛事级评分**(无 ELO/Glicko/积分/淘汰赛制),arena 天梯属自建增强。
- **ACPC(Annual Computer Poker Competition)**:`mbb/g`(milli big blinds per game)= 期望筹码速率(真值),带 95% CI;`mbb/g ÷ 10 = bb/100`。两套冠军:Total Bankroll(鼓励剥削)+ Instant Runoff(鼓励 Nash)。
- **PokerBench**(PokerBench/arXiv):Glicko-2(τ=0.5) + `S=0.5+0.5·tanh(m)`(m=净筹码差/BB 均值)代替二元 W/L,**保留量级**;镜像种子对抵消座位/发牌运气(扑克版 duplicate bridge)。
- 德扑本质:零和 + 连续筹码 + 高随机性 → 纯 W/L 信息损失大(赢手数 ≠ 赢筹码),需筹码量级 + 方差控制。

### 定稿:组合评分
1. **主评分 Glicko-2**(τ=0.5,自实现 `arena/backend/rating/glicko2.py`,~120 行,无依赖)
   - **分数 S = 0.5 + 0.5·tanh(m)**,m = 该场净筹码差 ÷ BIG_BLIND(=100) 的均值 → 保留"大胜 vs 小胜"量级,避免 W-L 掩盖 net-chips
   - 初值 rating=1500 / RD=350 / vol=0.06
   - RD 天然反映新 bot 不确定度(新 bot RD 大,老 bot RD 小)
2. **副指标 mbb/g(bb/100)+ 95% CI**(`pair_stats` 表,每对 (A,B))
   - mean_net_bb_per_100 ± 1.96·SE,作"真的更强"的真值锚(CI>0 才显著)
3. **胜负口径(国赛一致)**:单场 70 局累计筹码 → W/L/D;断线/出错(forfeit)→ 记 L
4. **方差控制(后期可选)**:镜像座位/发牌 —— 同一副牌两次(A 先 SB / B 先 SB),抵消运气

## 二、数据模型(SQLite `arena.db`,stdlib sqlite3)

```sql
users(name PK, display_name, team, note, secret, active,
      created_at, first_seen_at)                       -- bot 用户(身份)
matches(match_id PK, name_a, name_b, hands_played, total_hands,
        earnings_a, earnings_b, winner, reason, net_bb_a,
        thp_file, log_dir, started_at, ended_at)        -- 对战记录
ratings(name PK→users, rating, rd, vol,
        wins, losses, draws, net_chips,
        matches_played, last_played_at)                 -- Glicko-2 评分 + 战绩
pair_stats(name_a, name_b PK, bb_per_100_mean, ci_low, ci_high,
           samples, last_played_at)                     -- 每对 mbb/g + CI
admins(username PK, password_hash, created_at)          -- 管理员
sessions(token PK, username, expires_at)                -- 登录会话
```
- DB 存元数据(查询/聚合/排序);文件存详情:`records/<match_id>.thp` + `logs/<match_id>/{events.jsonl, result.json[, wire_a.log, wire_b.log]}`

## 三、用户管理 + 认证

### 两类身份
- **bot 用户**(程序身份):bot 名,admin 预注册 或 自动注册
- **管理员**(人):web 登录后台管 bot 用户 + 全局查看

### 认证三层
| 层 | 方式 | 范围 |
|---|---|---|
| 管理员(web) | 密码 → session token(cookie);初始 `pok-arena admin set-password` 或 env `POK_ARENA_ADMIN_PASSWORD` | /admin 用户 CRUD + 删对局 + 全局查看 |
| bot 连接(TCP) | 默认自动注册(裸 name,兼容国赛);可选 `--require-registration`(仅预注册 bot 名可连) | 不改官方协议(裸 name) |
| API 访问 | 天梯/历史/对局**公开只读**;管理端点需 admin token | 公开观赏 + 管理隔离 |

> 不用 token 改 bot 连接协议(官方裸 name,改了不兼容)。bot 身份用 bot 名 + admin 预注册/自动注册 + 可选白名单。

### 用户管理界面 `/admin`(登录后)
- bot 用户列表(name/display/team/active/rating/战绩) + 编辑(display/team/note/重置 secret/停用)
- 注册新 bot / 删除 / 停用
- 全局对局 + 用户 + 天梯查看

## 四、日志功能(serve 默认持久化)

每场 serve 自动写 `logs/<match_id>/`:
- `events.jsonl`(默认)— serve 事件流(hand_start/cards_dealt/action_requested/action/settle/match_end)
- `result.json`(默认)— 结果摘要
- `wire_a.log` / `wire_b.log`(**`--wire-log` 可选**,量大)— bot↔平台 wire(RECV/SEND)
- 旋转:`pok-arena clean --keep 1000`(保留最近 N 场)

## 五、API / CLI / 前端

### API(FastAPI)
- 公开只读:`GET /api/leaderboard?limit=` `GET /api/users/{name}` `GET /api/matches?user=&limit=&offset=` `GET /api/matches/{id}`
- admin(需 token):`POST /api/admin/login` `GET/POST/PUT/DELETE /api/admin/users[/{name}]`

### CLI(typer)
- `leaderboard [--top N] [--json]` `user <name> [--json]` `history [--user] [--limit] [--json]` `match <id>`
- `register <name> [--display] [--team]` `admin set-password` `clean --keep N`

### 前端(React,全部)
- `/leaderboard`(天梯:rank/bot/rating/RD/bb-100 CI/战绩/净筹码)
- `/user/:name`(用户:rating 卡 + 战绩 + 对各对手 bb/100 + 历史对局)
- `/history`(对局列表,筛选/分页)
- `/match/:id`(对局详情:events 时间线 + THP)
- `/login` + `/admin`(管理员登录 + bot 用户 CRUD + 全局查看)

## 六、隔离原则(与 HANDOFF 一致)
- arena 天梯/评分/用户数据**完全自包含**(arena.db + 文件),不读写 pok 任何数据
- sys.path 绝不含 pok1;Glicko-2 自实现,不搬 pok 代码
- arena 评分是**平台自带展示性评分**,独立于 pok 进化评分(符合 HANDOFF「与进化评分隔离」)

## 七、实现阶段
1. **存储层**:`arena/backend/store/{db.py, repository.py}`(SQLite schema + CRUD)
2. **评分**:`arena/backend/rating/glicko2.py`(Glicko-2 + tanh 量级 + 单测)+ bb/100 CI
3. **MatchManager 集成**:每场结束 → 写 DB(users/matches/ratings/pair_stats)+ 评分更新 + 日志(events.jsonl)
4. **serve 默认日志**:每场写 `logs/<match_id>/`(+ `--wire-log` 开关)
5. **认证**:`arena/backend/auth.py`(pbkdf2 密码哈希 + session + FastAPI dependency)
6. **API**:公开只读 + admin 端点
7. **CLI**:leaderboard/user/history/match/register/admin/clean
8. **前端**:天梯/用户/历史/对局/admin/login 六页
9. **测试 + 文档**
