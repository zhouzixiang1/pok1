# 交接提示词:开发国赛对弈平台 web 复刻(pok-arena)

> 这是一份交接提示词(任务交办,不是项目说明书)。在新的 Claude Code 对话里(工作目录 `~/project/pok-arena/`)完整阅读并执行。

## 你的角色与任务

你是资深 Python 全栈工程师。从零搭建一个独立 web 应用 `pok-arena`,复刻国赛官方 Windows EXE「德州扑克对弈平台」,命令行/终端友好。交付一个能跑通的最小可用版本(MVP),按里程碑迭代。

## 工作环境(关键:这是 worktree,不是独立仓库)

- **当前工作目录**:`~/project/pok-arena/`,它是 **pok1 仓库(`git@github.com:zhouzixiang1/pok1.git`)的一个 git worktree**,检出**孤儿分支 `pok-arena`**(空起点,不含 main 的进化代码)。这个目录里只有你放的文件,**看不到 pok 的 web/sever/engine 等代码**,干净不混淆。
- **共享同一个 git 仓库**:worktree 与 `~/project/pok/`(pok1 主工作树,operator 副本)共享 `.git`。在这里 commit → 进入 `pok-arena` 分支;push → 推到 `origin/pok-arena`(pok1 远端的新分支)。**完全不碰 `main`/进化系统**。
- **引擎代码引用源 = pok1 的 `origin/main`**(不是这个 worktree 的文件,孤儿分支没有它们)。提取:`git -C ~/project/pok show origin/main:<path>` 或 `git -C ~/project/pok archive origin/main <path>`。注意 `~/project/pok/` 工作树当前在 `codex/neural-v141-old-pool` 分支(有未合并神经工作),**不要用它的工作区文件当引用源,要用 `origin/main` 版本**。
- **更新引用源**:`git -C ~/project/pok fetch origin` 后 `git show origin/main:<path>`。**严禁 `git clone --local` 或从 `~/project/pok` 工作区直接拷贝**(会带 codex 分支脏改)。`PROVENANCE.md` 记录的 commit 须 origin/main 可达。

## 项目定位(已与用户确认,不要偏离)

- **单模式:只做「外部引擎接入观赛」**。平台 = TCP 服务器,接两个外部 bot 引擎打 70 局,实时展示牌桌/计时器/结算/THP。
- **不拉本地 bot 子进程,不碰 pok 进化系统**。与进化评分(Glicko、match_history)完全隔离。
- **命令行友好**:不依赖浏览器也能启动平台、连客户端、看状态、导出 THP。
- **比赛现场/演示可用**:健壮性门槛高(断线判负、自动重开、刷新不丢历史、崩溃不丢棋谱等是**必做**)。
- **干净功能等价**:做正确的 60s 超时,**不模拟**官方 EXE 的 timing race 怪癖。
- **单桌单场**:同时刻只接 2 引擎、跑 1 场;多桌 post-MVP。

## 技术栈(已定)

后端:Python + FastAPI + uvicorn(venv,3.12 或 3.13);前端:React + Vite + Tailwind;CLI:`typer`(`--json`);正规 `pyproject.toml`。

## 目录结构(按此搭建)

```
~/project/pok-arena/          ← pok1 的 worktree(孤儿分支 pok-arena)
├── .git                      ← 指向 ~/project/pok/.git 的 worktree 文件
├── HANDOFF.md                ← 本文件(首次 commit 纳入版本控制)
└── arena/                    ← ✅ 你的项目代码
    ├── backend/
    │   ├── engine/           ← copy 自 pok1 origin/main 的 sever/engine/ + sever/server/protocol.py
    │   ├── server/           ← 新写:TCP 平台(子包勿叫 arena 避免与顶层重名)
    │   ├── cli.py            ← typer 入口
    │   └── main.py           ← FastAPI app + uvicorn 协程式启动
    ├── frontend/             ← React + Vite + Tailwind
    └── pyproject.toml
```

## 引擎复用(copy + 记录上游 commit)

从 **pok1 的 `origin/main`** 提取并 copy(不是 import、不是软链)到 `arena/backend/engine/`:

- `sever/engine/{deck,evaluator,game,validator,thp_recorder,__init__}.py` + `sever/server/protocol.py`
- 提取并归位(archive 会还原 `sever/` 路径树,`protocol.py` 落在 sibling `sever/server/`,需手动归位):
  ```
  cd /tmp && rm -rf eng && mkdir eng && cd eng && \
  git -C ~/project/pok archive origin/main sever/engine sever/server/protocol.py | tar -x && \
  mkdir -p ~/project/pok-arena/arena/backend/engine && \
  cp sever/engine/*.py sever/server/protocol.py ~/project/pok-arena/arena/backend/engine/ && \
  cd ~ && rm -rf /tmp/eng
  ```
  (`sever/engine/__init__.py` 是空文件,会被带入作为 `engine/__init__.py`;`protocol.py` 从 `sever/server/` 归位到 `engine/`。)
- 行数以 origin/main 为准(`git -C ~/project/pok show origin/main:sever/engine/game.py | wc -l`):`game.py` 约 **602** 行,`protocol.py` 约 **92** 行(旧版 573/89 已过期)。`protocol.py` 原属 `server/` 包(TCP 线格式层),放 `engine/` 并加注释标明。

**copy 后必须做**:
1. 写 `arena/backend/engine/PROVENANCE.md`:`copy 自 pok1@<origin/main commit-hash> + 日期 + 上游路径`。commit-hash = `git -C ~/project/pok rev-parse origin/main`。
2. **import 适配**(game.py 与 protocol.py 都是 **try/except 双分支**结构,实测自 origin/main):
   - **game.py**:try 块**已是相对 import**(`from .deck/.evaluator/.validator/.thp_recorder` + `from ..server.protocol import (...)`)。protocol.py 移入 engine/ 同包后,只需把 try 内 `from ..server.protocol import (...)` 改成 `from .protocol import (...)`(1 处);其余 4 条已相对无需改。**整段删除 except ImportError 回退块**(绝对 `from engine.X`/`from server.protocol`,是 `cd sever && python main.py` 的 standalone 兜底,arena 布局下解析不到,留着误导)。
   - **protocol.py**:也是 try/except(`try: from ..engine.deck import ...` / `except: from engine.deck import ...`)。把 try 内 `from ..engine.deck import` 改成 `from .deck import`;同样删除 except 回退块。
   - 其余 4 文件(deck/evaluator/validator/thp_recorder)零内部依赖。无环 DAG,无循环。需改的语义点共 4 处(game.py 与 protocol.py 各 try+except),**不是「6 行」**。
3. **补父级 `__init__.py`**:`arena/__init__.py`、`arena/backend/__init__.py`(`engine/__init__.py` 随 copy 带入),保证 `python -m arena.backend.cli` 入口下相对 import 成立。
4. **不改协议语义**,只改 import/包结构。允许顺手清理:删 `game.py` 死的 `format_opponent_action` import、在 `protocol.py` 注明 docstring「\n 结尾」是原 pok server 整体行为——清理项记 PROVENANCE。

**🔴 硬约束**:arena 的 `pyproject.toml`/venv/启动脚本/`PYTHONPATH` **绝不可**把 `~/project/pok/` 或 pok1 任何工作树加入 `sys.path`。pok1 顶层另有一个 `engine/` 包(`judge.py` 等,无 `deck.py`);一旦入 path,`from engine.deck` 会撞顶层 engine(ModuleNotFoundError)或静默串包。**pok1 仅作 copy 源,不是 import 源。**

## TCP 平台:复用三件套(跨 3 个模块,不要从零写)

pok1 的平台侧 TCP 实现被 native 验收流水线实测在用,但**分散在 `web/core/` 的 3 个模块**(不是都在 national_native.py),名字也与旧版不同。提取后改 import + 注入 host/port 即可用:

1. **`NationalTCPClient`**(`web/core/national_transport.py`,~255 行):服务端 bot 连接包装。含 `send_line`(自动加 `\n`)、`recv_line`(带超时返回 None)、`recv_name`(name 握手)、`recv_action`/`_finish_action`(bot→server 动作的 token 解析),以及模块级 `pop_client_action`(粘包拆分)。
2. **`NationalTCPGameEngine`**(`web/core/national_game_runtime.py`,~71 行):`GameEngine` 子类,覆写 `_send_to_client`/`_recv_action`/`_record_event`,与要 copy 的 `game.py` 配套。**🔴 注意它用 `from sever.engine.{deck,game,thp_recorder} import` 绝对路径**(假设 pok1 顶层 `sever/` 在 sys.path),copy 到 arena 后**必须改成相对 import**(且 arena 的 sys.path 绝不可含 pok1,见硬约束)。
3. **`_run_tcp_server_with_processes`**(`web/core/national_native.py` 约 L2187-2483,~296 行):`asyncio.start_server` + 双 bot 接入 + name 握手 + 比赛生命周期 + events 收集。**🔴 注意它原绑定 `127.0.0.1:0`(随机端口,acceptance 跑完即弃)且硬编码**,复用时要把 host/port 改为从 CLI 参数注入(`50101`/`127.0.0.1`)。

提取(各自取):
```
git -C ~/project/pok show origin/main:web/core/national_transport.py
git -C ~/project/pok show origin/main:web/core/national_game_runtime.py
git -C ~/project/pok show origin/main:web/core/national_native.py   # 只取 _run_tcp_server_with_processes 及其依赖
```
**注意**:national_native.py 全文约 **3109 行**、含大量 pok 耦合;三件套合计约 518 行(非"~140"),要剥的 pok 专用 import 约 **9 个**:`eval_stats`/`bot_namespace`/`national_runtime_telemetry`/`national_bot_launcher`/`national_game_runtime`/`national_transport`/`pipeline_schema`/`runtime_capacity`/`strength` 等。复用仍值得(省掉状态机/阶段流转/allin runout/超时→fold/THP/事件流从零写),但**不是「剥 import 即用」**——需改 national_game_runtime 的绝对 import + 改 _run_tcp_server_with_processes 的 host/port 注入。

解析方向别搞混:**平台/server 侧**(bot→server 出站)用 `NationalTCPClient.recv_action` + `pop_client_action`(纯 token 前缀,不涉及卡牌);**bot 侧**(server→bot 入站)用 national_native.py 的模块级 `_split_messages`(L251)/`_take_message`(L221,带 `flush_numeric` 参数,前缀+卡牌正则),或用 `bots/national_v141/national_bot.py` 的同名函数(无 `flush_numeric`,更简)二选一。

## 协议契约(字节级,不可违背)

**TCP 角色**:平台 = TCP 服务器,bot = 客户端。**TCP 端口默认 `50101`**。

**握手**:连接后平台发 `name`,bot 回**裸队名 UTF-8 字节无分隔符**。解法(national_native 启发式):首次拿到非动作前缀字节即视为完整 name 返回(loopback 单包假设)。平台只在两客户端都连入后才发 `name` 开局。**name 要求 ASCII**;同名→后连者拒,空名→拒断。

**server → bot token 词表(逐字)**:`name`;`preflop|{SMALLBLIND|BIGBLIND}|<s,r><s,r>`;`flop|<s,r><s,r><s,r>`;`turn|<s,r>`;`river|<s,r>`;`earnChips <int>`(可负);`oppo_hands|<s,r><s,r>`(仅 showdown);转发对手 `call`/`check`/`fold`/`allin`/`raise <int>`。`bet` **永不发送**。

**卡牌**:`<suit,rank>`,无空格,如 `<0,12>`=黑桃 A。`suit ∈ {0=♠,1=♥,2=♦,3=♣}`;`rank 0..12=2..A`。与 pok1 顶层 `engine/judge.py`(`♥0♦1♠2♣3`)**不同**(♣=3 恰同,其余重排;映射见 `national_native.TCP_TO_JUDGE_SUIT`)。**不要混入 judge.py 序**。

**raise = raise-to-total**:恰好一个空格,X 是本街总额。最小:preflop 首 raise ≥ 200、postflop 首 raise ≥ 100、再 raise **严格 >2×** 上次(raise 400 后最小 801)。raise 用尽筹码须 `allin`。多空格判非法。

**13 条非法行为**(全按 fold,`validator.py` 已实现)。preflop check 精确条件:仅 BB 首动作**且无待跟注**时合法。

**先后手**:preflop SB 先;flop/turn/river BB 先。70 局交替大小盲。20000 筹码,盲注 50/100,每手复位。

**结算**:`earnChips` 净值;showdown 先 `earnChips` 再 `oppo_hands`;fold 不发 `oppo_hands`。

**allin runout**:allin 被 call 后只补公共牌 + `earnChips`(+ showdown `oppo_hands`),不再索取动作。

**计时**:每动作 60s,超时 = fold。

**bet 严格复刻官方**:保留 `bet` 交 `parse_action`+`validator`,规则 1 判非法→fold。**严禁把 bet 重写成 raise**——平台侧 bot→server 动作解析用 `NationalTCPClient.recv_action`(`web/core/national_transport.py`),不要在里面加 bet→raise 改写(注:national_native.py 的 bot 侧解析有 bet→raise 兼容逻辑,平台侧不要照搬)。

**非法/超时通知**(字节级):违规方记 fold,**向对手转发 `fold`**;**违规方静默不发 error/illegal**(多发会让 native bot 前缀解析死锁);THP 记违规方。`_recv_action` 契约:成功返回动作字符串、超时/断开返回 **None**(不抛),`GameEngine._betting_round` 据此 fold。

**bot→server 解析**:必须 token 前缀驱动,**不能要求 `\n`**。**不要照搬** `sever/server/tcp_server.py` 的 `recv_line`(`git -C ~/project/pok show origin/main:sever/server/tcp_server.py` 查看)。server→bot 可加 `\n`。

**座位**:先连 = 桌面下方(p0),后连 = 桌面上方(p1)。**第 3 连接**:发 `error: match full` 关闭。

## 比赛生命周期(长驻)

- 一场 70 局结束**自动 re-arm**:清旧 client、回 listening 等下一对 bot。
- CLI `serve` 加 `--max-matches N`/`--once`。
- `sever/server/tcp_server.py` 反面教材(`git -C ~/project/pok show origin/main:sever/server/tcp_server.py` 查看):一场结束 close 双连接且不清 clients 列表,不要继承。

## 健壮性 / 异常路径(现场可用门槛)

- **断线判负**:某 bot 断开 → 该手 fold + 连续 2 手无响应 → 判该方 **forfeit 整场**,SSE `match_end`+`reason=disconnected`,关双方。**不要**「静默 fold 满 70 局」。
- **无 reconnect**:MVP 不支持断线重连。
- **SSE 快照 + 重连**:`/api/arena/events` 握手先发 `snapshot`(当前手+累计+已完成手摘要+当前手明文状态);EventSource 重连靠 snapshot 恢复。
- **逐手事件缓存**:MatchManager 维护 per-hand event log(每手={hand_num, sb/bb, 双方初始筹码, 每动作 stage/action/amount/pot_after/chips_after/decision_wait_sec, community, settle})。
- **THP 增量落盘**:每手 settle 后 `append` THP,崩溃不丢整场。
- **单桌单场**:MatchManager 单例;多桌 post-MVP。

## 运维 / 可观测 / 安全

- **bind host**:`serve` 默认 `--host 127.0.0.1`;`--host 0.0.0.0` 显式告警。**不做鉴权**,默认本机。
- **日志**:`--log-level`/`--log-file`,JSONL;`decision_wait_sec` 遥测落盘。
- **THP 存储**:`--records-dir` 默认 `~/.local/share/pok-arena/records`。

## 端口(已选定,避开本机所有占用)

- **平台 TCP:`50101`**;**Web:`50180`**。都在 49152–65535 私有段(本机该段空闲)。已避开本机占用(22/53/631/3350/3389/8020/10081/10082/15721/18081/35701/36117/36165/41615/43711/44113/44933/46319/46503)与 pok 的 8000/10001/18080/5173。

## CLI 设计(typer)

入口:`python -m arena.backend.cli`(`pyproject.toml` 注册 `pok-arena`)。子命令:

- `serve [--host 127.0.0.1] [--tcp-port 50101] [--web-port 50180] [--max-matches N|--once] [--records-dir] [--log-file] [--log-level]` — TCP+web 同进程;FastAPI 托管 `arena/frontend/dist`,开发期可另起 `npm run dev` 代理。
- `connect <host> <port> <name>` — **无策略协议自测客户端(protocol exerciser)**,内置最小 call/check(照搬 test_client),recv 依赖服务端 `\n`;只验证 `\n` 路径,no-`\n` 路径必须用真 native bot 验证。
- `thp <match-id> [--out]` / `thp list` — match-id 由 MatchManager 在 `match_start` 分配(时间戳+双方名),写 `index.json`。
- `status [--host 127.0.0.1] [--port 50180] [--json]` — HTTP GET `/api/state`。
- 全局 `--json`。**不做** `run`/`list` bot 池命令。

## git(本 worktree 的用法)

- **本目录是 pok1 的 worktree**,不是独立 git 仓库,**不需要 `git init`**。共享 `~/project/pok/.git`。
- **开发流**:在 `~/project/pok-arena/` 直接 `git add`/`commit` → `pok-arena` 分支(孤儿分支,首次 commit 是它第一个提交)。
- **推送**:`git push -u origin pok-arena` → 在 pok1 远端建 `pok-arena` 分支。**完全不碰 `main`**。
- **不需要 `.gitignore` 忽略 pok/**:本 worktree 没有 pok clone(引用源是 pok1 本身,经 `git show origin/main:` 访问)。只忽略常规产物(`__pycache__/`、`venv/`、`node_modules/`、`dist/`、`*.pyc`)。
- **不要在 `~/project/pok/` 主工作树做 pok-arena 开发**(那是 codex 分支+脏项);只在 `~/project/pok-arena/` worktree 开发。

**提交规范**(分支/message/粒度/边界):
- **分支策略**:日常直接在 `pok-arena` 分支提交(它是本项目的主分支,孤儿独立,等价于本项目的 main);只在隔离大功能时从 `pok-arena` 开 `codex/<task>` 临时分支,完成 merge 回 `pok-arena` 再删任务分支。
- **commit message**:Conventional Commits 前缀 + 简明描述,一个逻辑改动一个 commit。示例:`feat(server): 接入 token 解析 TCP 服务器`、`fix(engine): 修正 raise-to-total 最小加注边界`、`feat(cli): 加 serve/connect/thp/status`、`docs: 更新验收清单`、`chore: 初始化 pyproject+venv`、`refactor: 剥离 national_native pok 专用 import`。
- **提交粒度**:小步提交,每个可独立回溯的改动一次,不要攒一大坨;协议/parser/card mapping/THP 等关键语义改动**单独成 commit**,并在 message 写清改了什么语义(便于将来对照真 EXE)。
- **push 时机**:每个 commit 后 `git push`(备份 + GitHub 可追溯);首次 `git push -u origin pok-arena` 建立远端分支与上游跟踪。
- **不提交**(必须写进 `.gitignore`):`.env` / 密钥 / 凭证 / token、`venv/`、`node_modules/`、`dist/`、`__pycache__/`、`*.pyc`、`.DS_Store`、IDE 配置(`.idea/`、`.vscode/`)、本地 SQLite/日志(`*.log`、`~/.local/share/pok-arena/` 的运行产物如确需本地化也忽略)。
- **禁止操作**:`git push --force` 到公共 `pok-arena` 分支(自己刚推的修正可用 `--force-with-lease`);不 `git reset`/`checkout`/`restore` 无关改动;commit/push 因 credentials/远端/网络失败时,报告确切错误、保留 worktree 原样、不强行覆盖。
- **PROVENANCE 同步**:引擎 re-copy 同步上游 bugfix 时,先 `git -C ~/project/pok fetch origin` 取最新 `origin/main`,re-copy 后**更新 `arena/backend/engine/PROVENANCE.md` 的 commit-hash**,并单独成一个 commit(消息如 `chore(engine): re-sync upstream@<short>`),保留与上游的可追溯链。
- **提交前自检**:`git status` 看有无无关文件混入(尤其运行产物);只 `git add` 本次任务相关文件,**不用 `git add -A`/`git add .`** 除非确实要全量快照。

## 开发规范

- **不动 pok1 的 main 或其他分支**:只在 `pok-arena` 分支(本 worktree)开发。
- 引擎只 copy 不 import pok1(见硬约束)。
- Python venv + `pyproject.toml` 锁依赖。
- 协议/parser/card/THP 改动**必须配套测试**。
- 前端中文 UI。

## 官方 EXE 真实功能(复刻基线,来自 Wine/Xvfb 实测)

窗口「德州扑克自对弈平台」,1286×598。菜单 4 项:`文件(F)`/`视图(V)`/`游戏控制(C)`/`帮助(H)`。工具栏:文件夹、齿轮(=建立连接)。建立连接对话框:`桌面上方玩家:`/`桌面下方玩家:`/`对弈平台IP地址:`,按钮 `开始连接`+`确定`,端口默认 10001。操作流:点齿轮→输 IP→开始连接→两引擎连入→确定→自动开 70 局。权威文档在 pok1 `sever/国赛平台/`(`德州扑克规则.doc`、`通信协议.docx`、`非法行为说明.docx`、`自对弈平台使用及通信协议补充说明.docx`、棋谱标准 PDF)。这些是**二进制**,提取需落地+转换:`git -C ~/project/pok show origin/main:sever/国赛平台/通信协议.docx > /tmp/x.docx && pandoc -f docx -t plain /tmp/x.docx`(pandoc 已装);旧 `德州扑克规则.doc`(OLE)用 `libreoffice --headless --convert-to txt /tmp/x.doc`;PDF 用 `pdftotext`。

## 验收标准(MVP)

1. **运行时 import 冒烟** `python -c "from arena.backend.engine import game, protocol, validator, thp_recorder, deck, evaluator"`通过 + 单测/协议测试全过。(`py_compile` 只查语法。)
2. **同时连入两个引擎客户端**(两个 native_bot,或 1 native + 1 connect),跑完 ≥10 手无死锁,native bot(裸字节无 `\n`)正常收发——证明 no-`\n` token 解析正确。(单 bot 假死锁,必须两个。)
3. 两个 `connect` 跑完一场,再用真 native bot 验证 token 解析。
4. `/arena` SSE 实时牌桌/筹码/计时器/历史;**刷新靠 snapshot 不丢**。
5. **CLI 纯终端闭环**:`serve`→两个 `connect`→`status --json` 观察 ≥10 手→`thp` 导出,全程不开浏览器。
6. `serve`/`connect`/`thp`/`status` 各冒烟(可机械化判定)。
7. **断线验收**:kill 一个 bot → 连续 2 手后另一方 forfeit + SSE `reason=disconnected`。
8. **非法验收**:故意发 `bet` 的 client → 对手收 `fold`、违规方静默、THP 记违规方。
9. **重开验收**:70 局后连第二对 bot 能自动开赛。
10. **数据隔离**:不读写 pok1 任何运行期/进化数据。
11. **安全基线**:默认 bind 127.0.0.1。

## 未决问题(开发中与用户确认)

1. **THP 字节对齐**:thp_recorder 对 raise/allin 记增量,与真 EXE 导出 diff;不一致则定增量还是总额。注意 gb2312 编码。
2. **真 EXE 合规验收**:用户要「干净等价」(不模拟 timing race),若现场需协议合规证据,是否用 pok1 official harness + 真 EXE 跑一轮。

## 参考文件入口(pok1 仓库 origin/main,用 `git -C ~/project/pok show origin/main:<path>` 访问)

- `docs/official-exe-platform-analysis.md` — EXE 静态+动态分析(timing race 背景,本项目不模拟)
- `sever/engine/{game,validator,deck,evaluator,thp_recorder}.py` + `sever/server/protocol.py` — **要 copy 的引擎内核**
- `web/core/national_transport.py` — **NationalTCPClient**(平台侧 bot 连接 + bot→server token 解析 `recv_action` + name 握手 `recv_name`,~255 行)
- `web/core/national_game_runtime.py` — **NationalTCPGameEngine**(GameEngine 子类,~71 行;注意它 `from sever.engine.{deck,game,thp_recorder} import` 绝对路径,copy 后须改相对)
- `web/core/national_native.py` — `_run_tcp_server_with_processes`(约 L2187-2483,~296 行;全文 3109 行含大量 pok 耦合,提取时剥 9 个 pok 专用 import)+ bot 侧解析 `_split_messages`(L251)/`_take_message`(L221)
- `sever/main.py` — **FastAPI + asyncio TCP 单进程共存范本**(`asyncio.gather`+`uvicorn.Server.serve()`,勿用阻塞 `uvicorn.run`)
- `sever/web/app.py` — **SSE 桥接范本**(broadcast 注入+`StreamingResponse`+`asyncio.Queue`)
- `sever/server/tcp_server.py` — **反面教材**:`recv_line` 按 `\n` 切死锁 native bot;单场结束不清 clients
- `sever/test_client.py` — 参考 TCP 客户端(发 `\n`,canned 跟随器)
- `sever/国赛平台/*.docx/.pdf` — 权威协议文档
- `bots/national_v141/national_bot.py` — **自包含单文件** native bot(547 行,仅 stdlib,不 import 同目录其它 .py),验收连通性直接用:`git -C ~/project/pok show origin/main:bots/national_v141/national_bot.py > /tmp/BotA.py && python /tmp/BotA.py --host 127.0.0.1 --port 50101 --name BotA`。**注意**:`bots/national_v*/` 是 glob,`git show` 不展开,必须指定具体版本;其它版本(如 national_v142)多文件依赖,须整目录 `git -C ~/project/pok archive origin/main bots/national_v142 | tar -x` 提取。
- `AGENTS.md` — pok1 的 AI agent 地图(仅供理解上下文,**arena 只取 sever/ 国赛 TCP 部分,不搬进化/web 概念**)

## 开始方式

1. `git -C ~/project/pok rev-parse origin/main` 记录上游 commit。
2. 在本 worktree 建 `arena/` 骨架 + `pyproject.toml` + venv(3.12/3.13)+ 父级 `__init__.py`。
3. 按「引擎复用」节的归位命令提取引擎到 `arena/backend/engine/`(注意 `protocol.py` 从 `sever/server/` 归位 + 删 archive 残留的 `sever/`),写 `PROVENANCE.md`,按 try/except 双分支真实结构改 import(改 4 处 + 删 except 块,非「6 行」),跑运行时 import 冒烟 `python -c "from arena.backend.engine import game,protocol,validator,thp_recorder,deck,evaluator"`。
4. 从 3 个模块取平台三件套:`git -C ~/project/pok show origin/main:web/core/national_transport.py`(NationalTCPClient)、`:web/core/national_game_runtime.py`(NationalTCPGameEngine,把 `from sever.engine` 改相对)、`:web/core/national_native.py`(_run_tcp_server_with_processes,改 host/port 注入 + 剥 9 个 pok import),作为平台 server(`arena/backend/server/`)接入 `game.py`。
5. 加 `MatchManager`(match-id、re-arm、断线判负、per-hand event log、THP 增量落盘)+ SSE(snapshot)+ 前端牌桌页 + CLI。
6. 按验收标准逐条验。
7. `git add && git commit` → `git push -u origin pok-arena`(首次推送建远端分支)。

遇到方向性选择停下来问用户;纯实现细节自行决定并在提交说明里写清。
