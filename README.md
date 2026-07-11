# pok-arena

国赛官方德州扑克对弈平台 EXE 的 web 复刻。**平台 = TCP 服务器**,接两个外部 bot 引擎打 70 局,实时牌桌 / 计时器 / 结算 / THP,命令行友好。

不拉本地 bot 子进程、不碰 pok 进化系统(评分 / Glicko / match_history 完全隔离)。

## 快速上手

```bash
# 后端(venv + 依赖)
/usr/bin/python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# 前端构建(可选;不构建则 serve 仅提供 API/SSE,不挂牌桌页)
(cd arena/frontend && npm install && npm run build)

# 启动平台(长驻 TCP 50101 + Web 50180)
.venv/bin/pok-arena serve
# 另开两个终端连引擎(自带协议练习器):
.venv/bin/pok-arena connect 127.0.0.1 50101 BotA
.venv/bin/pok-arena connect 127.0.0.1 50101 BotB
# 浏览器: http://127.0.0.1:50180/
```

一键(进程管理脚本):
```bash
scripts/arena-ctl.sh start 70 connect     # 起 serve + 2 connect
scripts/arena-ctl.sh status | thp list | logs serve 30
scripts/arena-ctl.sh stop
```

真 native_bot(发裸字节无 `\n`,验证 no-`\n` token 解析路径):
```bash
scripts/arena-ctl.sh start 70 native      # 需 NATIVE_BOT_DIR 指向 pok1 national_v141
```

## 架构

```
arena/
├── backend/
│   ├── engine/          德州扑克引擎(copy 自 pok1 origin/main, 见 engine/PROVENANCE.md)
│   │   deck / evaluator / game / validator / thp_recorder + protocol
│   ├── server/          平台侧 TCP
│   │   transport.py       NationalTCPClient(token 前缀解析,不依赖 \n)
│   │   game_runtime.py    NationalTCPGameEngine(桥接 GameEngine ↔ Client)
│   │   tcp_server.py      ArenaTCPServer(接入层:name 握手 / re-arm / 第3连接拒)
│   │   match_manager.py   MatchManager(编排 / 断线判负 / THP 落盘 / SSE 扇出)
│   ├── main.py          FastAPI + uvicorn/TCP 单进程共存 + SSE 端点
│   └── cli.py           typer: serve / connect / thp / status
├── frontend/            React 19 + Vite + Tailwind v4(牌桌页, 中文 UI, SSE 实时)
└── tests/               pytest 协议单测(35)
scripts/                 验收脚本 + arena-ctl 进程管理
```

## 协议要点(与用户确认的决策,见 `arena/backend/engine/PROVENANCE.md`)

- **raise 边界** `>=2×`(精确 2× 合法,raise 200→400 合法)。与 `official-raise-boundary-oracle` + EXE 实测一致。HANDOFF.md 的 `>2×` 为笔误,已修正。
- **THP raise/allin 记总额**(对齐 EXE,非增量)。
- **断线判负**:仅 TCP 真断开(`client.closed`)连续 2 手判 forfeit,60s 超时只当手 fold。
- **端口**:TCP `50101` / Web `50180`(默认 `127.0.0.1`,无鉴权)。
- **native bot 发裸字节无 `\n`**:服务端 token 前缀解析(`pop_client_action`),绝不按 `\n` 分帧(否则死锁,重蹈反面教材 `sever/server/tcp_server.py`)。

## 测试与验收

```bash
.venv/bin/pytest arena/backend/tests/      # 35 协议单测
bash scripts/run_acceptance.sh             # pytest + 断线 / 非法 / 重开 健壮性验收
```

11 项验收标准(HANDOFF L186-198)全部达成:
| # | 标准 | 方式 |
|---|---|---|
| 1 | import 冒烟 | pytest 单测隐含 |
| 2 | 两个 native_bot ≥10 手无死锁(no-`\n`) | arena-ctl start . native(实测 12 手) |
| 3 | connect 跑完一场 + native_bot 验证 | e2e 冒烟 |
| 4 | `/arena` SSE 实时 + snapshot 刷新 | 前端构建 + SSE 首帧验证 |
| 5 | CLI 终端闭环 serve→connect→status→thp | 全程不开浏览器 |
| 6 | serve/connect/thp/status 冒烟 | typer 注册 + e2e |
| 7 | 断线判负 | `accept_disconnect.py` |
| 8 | 非法 bet→对手收 fold / 违规方静默 / THP 记 | `accept_illegal.py` |
| 9 | 重开 re-arm(70 局后第二对自动开赛) | `accept_rearm.py` |
| 10 | 数据隔离(不读写 pok1 运行期/进化数据) | 设计保证 |
| 11 | 安全基线 127.0.0.1 | 默认 host |

## 文档

- `HANDOFF.md` — 任务交办书(完整需求 / 协议 / 约束)。
- `arena/backend/engine/PROVENANCE.md` — 引擎 copy 来源 + arena 协议决策记录。
- 上游:pok1 `origin/main`(仅作 copy 源,**绝不 import**;sys.path 绝不含 pok1)。
