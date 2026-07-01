# 国赛平台对齐报告

日期：2026-06-30

## 范围

这个仓库里有两套仍在使用的德州扑克协议，后续改动必须把它们分清楚：

- `engine/` 和 `web/` 演化的是 Botzone/本地 JSON 子进程 bot。bot 从 stdin 读取 JSON，并向 stdout 输出 `{"response": int}`。
- `sever/` 实现的是国赛 TCP 自对弈平台。AI 引擎作为 TCP 客户端连接平台，并发送按行分隔的文本行为。

Web 进化系统本身不是原生 TCP bot 平台。它面向国赛的路径是：先在 `bots/claude_v*/` 下进化 JSON bot，再通过 `sever/bot_adapter.py` 部署到 `sever/` TCP 平台。因此 adapter 是生产兼容边界的一部分。

## 国赛文档确认的事实

当前实现对齐 `sever/国赛平台/` 下的文档：

- 传输方式是 TCP。平台是 server，AI 引擎是 client。
- 默认 TCP 端口是 `10001`；`sever/main.py` 同时启动 `18080` Web 仪表盘。
- 一场比赛 70 手。每手开始时双方筹码都重置为 20000。
- 盲注是 50/100。小盲 preflop 先行动；flop、turn、river 都是大盲先行动。
- 每次决策限时 60 秒。超时按 fold 处理。
- 客户端合法行为 token 是 `raise <amount>`、`fold`、`call`、`check`、`allin`。
- `bet` 不是合法 wire token。协议用 `raise` 表示首次下注和加注。
- `raise X` 表示加注到本街总下注额 `X`，不是额外增加 `X`。
- 解析器只接受恰好一个空格的 `raise <amount>`。行首/行尾空格、Tab、多空格都属于非法协议格式。
- Postflop 首个行为不能是 `call`。Postflop 在任何首个行为之后，`check` 都非法；如果第一个玩家 `check`，第二个玩家要用 `call` 结束该街。
- `allin` 被 `call` 后，服务端发完剩余公共牌并结算该手；客户端在 `earnChips` 前不应再行动。
- 仓库当前统一采用严格 re-raise 规则：再次加注必须大于上一次 raise-to 的 2 倍。这个选择来自 `raise 400` 后需要 `raise 801` 的示例；文档文字本身容易被误读成允许等于 2 倍。

## TCP 全流程

1. `sever/main.py` 启动 TCP server 和 FastAPI/SSE 仪表盘。
2. 每个客户端连接后收到 `name`，然后回复队伍/bot 名称。
3. 第二个客户端连接后，`MatchManager` 自动开始比赛。`/api/start` 仍作为仪表盘兜底控制存在，并会拒绝重复启动正在运行的比赛任务。
4. 每手牌开始时，服务端发送 `preflop|SMALLBLIND|...` 和 `preflop|BIGBLIND|...`，里面包含对应玩家的手牌。
5. 服务端用 `sever/engine/validator.py` 校验每个客户端行为。非法行为统一转成 fold。
6. 对手行为以文本转发。街牌消息使用 `flop|...`、`turn|...`、`river|...`。
7. 如果 river 前 all-in 被 call，服务端自动发完剩余公共牌，把这些公共牌写入 THP，并抑制该手后续决策。
8. 结算阶段向双方发送 `earnChips <amount>`。Showdown 时发送 `oppo_hands|...`。
9. 比赛结束后，`sever/engine/thp_recorder.py` 在 `sever/records/` 下导出 GB2312 编码的 THP 文件。

## THP 对齐

THP 输出采用国赛棋谱结构：

- 文件名格式：`THP-{teamA} vs {teamB}-{winner}胜-{yyyymmddHHMM}-CCGC.txt`，并替换文件系统危险字符。
- 每手记录行：`STATE:N:actions:cards:earnings:players;`。
- 动作：`r{amount}` 表示 bet/raise，`c` 表示 check/call，`f` 表示 fold，街之间用 `/` 分隔。
- 牌面：使用 `Ah`、`Ts` 这类 rank/suit 字符串。
- 手牌按大盲在前、小盲在后记录。
- 每手的 earnings 和 player names 也按该手的大盲在前顺序记录，和手牌顺序一致。
- 导出编码是 GB2312。

## Web 进化系统对齐

Web 应用仍以本地 JSON 子进程 bot 和 mirror battle 为核心。国赛 TCP 对齐这次做的是增加边界和护栏，而不是把进化协议改成 TCP：

- `web/core/prompts/worker_prompt.md`、`reviewer_prompt.md`、`master_prompt.md`、`initial_prompt.md`、`crossover_prompt.md` 已明确：进化 bot 仍然是 JSON bot，TCP 部署通过 `sever/bot_adapter.py`。
- 提示词现在禁止依赖 wire-level `bet`、禁止用消耗全部剩余筹码的正数 raise 表示 all-in、禁止假设 TCP postflop `check-check` 合法。
- `web/core/prompts/dynamic_test_generator.md` 的 action history 示例改用 `raise###`，不再使用 `bet###`。
- `web/core/code_verification.py` 暴露 `run_national_protocol_tests()`。
- `web/core/tool_gates.py` 在 `run_quality_gates` 阶段运行 `sever/tests/test_national_alignment.py`，因此 adapter/platform 协议漂移会阻断 bot commit。

现有前端 match replay 和 rating dashboard 仍展示 `web/core/results/` 里的本地 JSON battle 数据。它们不是 THP 解析器，也不声明可视化国赛 TCP 棋谱。

## 已完成的代码修改

- `sever/server/protocol.py`：严格解析行为；只接受精确 `raise <amount>`；`bet` 只识别为非法，不做归一化。
- `sever/engine/validator.py`：postflop 第二个 `check` 非法；postflop 首个 check 后用 `call` 过街；raise 金额必须存在、为正、且大于当前玩家本街已下注额。
- `sever/engine/game.py`：all-in runout 的公共牌写入 THP；删除 postflop `check-check` 快捷结束逻辑。
- `sever/server/tcp_server.py`：读行保留协议非法空格；两个客户端连接后自动开赛；THP 文件名带赛事后缀并做安全化。
- `sever/web/app.py`：`/api/start` 尊重已有 match task，避免重复开赛。
- `sever/engine/thp_recorder.py`：每手 earnings 和 players 使用 BB|SB 顺序。
- `sever/bot_adapter.py`：all-in runout 模式抑制额外行动；postflop 对手 check 后 JSON `0` 映射为 TCP `call`；消耗全部筹码的正数 raise 转成 `allin`。
- `sever/test_client.py`：冒烟客户端在 postflop 对手 check 后发送 `call`，并在 all-in call 后抑制 runout 阶段行动。
- `sever/tests/test_national_alignment.py`：回归测试覆盖解析严格性、validator 边界、THP 顺序/runout、adapter 行为转换、自动开赛。

## 2026-07-01 更新：国赛验收矩阵

本轮补齐了从本地进化产物到国赛平台的可验收路径：

- `engine/judge.py` 和 `web/core/engine/judge.py`：Botzone 整数动作仍保持 `0=call/check`，但 postflop 首个玩家 check 后，第二个玩家用 `0` 过街时，history 记录为 `action_type="call"`，与国赛 TCP 协议保持一致。
- `sever/bot_adapter.py`：增加 adapter telemetry，记录 bot 子进程失败、不可转换动作、实际发送动作数，方便区分“服务器判非法”和“bot/adapter 自身失败”。
- `scripts/national_acceptance_matrix.py`：新增国赛验收矩阵工具。它不走本地 `engine/battle.py`，而是把 Botzone JSON bot 放进 `sever/bot_adapter.py`，再用 `sever/engine/game.py` 与 `sever/engine/validator.py` 的国赛规则实跑 pairwise match。
- `web/core/prompts/`：Master、Worker、Reviewer、Crossover、Initial、Dynamic Test 以及相关审计 prompt 已嵌入 `sever/国赛平台/` 的完整非法行为约束，包括 `bet` 禁用、raise-to-total、严格 re-raise、postflop `check/call`、BB 不能在 SB limp 后 call、all-in 后只能 call/fold 等规则。
- `web/core/decision_tester.py`：旧的 postflop `check/check` 动态场景模板改为 `check/call`，避免质量门继续生成与国赛协议相反的历史样例。

推荐验收命令：

```bash
python scripts/national_acceptance_matrix.py --hands 70 --limit 4 \
  --output results/national_acceptance_matrix.json \
  --markdown results/national_acceptance_matrix.md
```

也可以指定候选：

```bash
python scripts/national_acceptance_matrix.py \
  --bots claude_v243 claude_v242 bot5 \
  --hands 70 \
  --output results/national_acceptance_matrix.json \
  --markdown results/national_acceptance_matrix.md
```

矩阵默认选择“最新 `.completed` 进化 bot + Glicko conservative rating 靠前的 `.completed` bot”，不会把未完成的 `claude_v*` 目录当作正式产物。显式传 `--bots` 时可以人工检查任意目录。

合规判定：

- `illegal_actions == 0`：服务器没有按国赛 validator 判任何行为非法。
- `timeouts == 0`：国赛引擎没有等不到行动。
- `bot_failures == 0`：adapter 调用 Botzone 子进程没有失败或超时。
- `invalid_actions == 0`：bot 输出可以被 adapter 转成整数动作。
- `hands_played == hands_requested`：每组对局完成指定手数。

强度判读：

- `net_chips` 是该 bot 在矩阵所有 pairwise match 中的累计净筹码。
- `Pairwise Net Chips Per Hand` 表示行 bot 对列 bot 的每手平均净筹码，括号里是该组对局的合规状态。
- 小手数矩阵只适合做冒烟验收；强度结论至少使用 70 手国赛完整局，并建议多次运行或扩大候选组来降低方差。

## 验证

当前验证命令：

```bash
python -m py_compile scripts/national_acceptance_matrix.py sever/bot_adapter.py engine/judge.py web/core/engine/judge.py sever/tests/test_national_alignment.py
python -m py_compile sever/main.py sever/server/tcp_server.py sever/server/protocol.py sever/engine/game.py sever/engine/validator.py sever/engine/thp_recorder.py sever/test_client.py
python -m py_compile web/core/code_verification.py web/core/evolution_core.py web/core/evolution_infra.py web/core/tool_gates.py
python -m pytest sever/tests/test_national_alignment.py -q
python -m pytest sever/tests -q
python scripts/national_acceptance_matrix.py --bots claude_v243 claude_v242 --hands 70 --output /tmp/national_acceptance_matrix_v243_v242_70.json --markdown /tmp/national_acceptance_matrix_v243_v242_70.md
```

本报告更新时的结果：py_compile 通过，国赛对齐测试文件和 `sever/tests` 均为 `13 passed`；`claude_v243` vs `claude_v242` 的 70 手国赛矩阵合规 PASS，非法/超时/bot 失败/不可转换动作均为 0。

## 剩余边界

- Web 进化流水线仍用 `engine/battle.py` 评估本地 JSON bot；它不会把每个候选 bot 都跑一场真实 TCP match。新增国赛协议测试门用于保护共享 adapter/platform 语义。
- Dashboard replay UI 面向本地 JSON battle replay，不是 THP 文件浏览器。
- 连接时队伍名校验仍偏宽松。当前导出文件名会做安全化；如果需要完全模拟参考平台，可再增加严格队名校验。

## 改代码规范

后续我改这个仓库时，默认按下面这套规范执行；如果你有不同偏好，可以直接覆盖其中任何一条。

1. 先确认边界，再动代码。`engine/`、`web/`、`sever/`、`rl/` 不能混成一个模型；涉及协议、进化提示词、质量门、文档时，要一起检查影响面。
2. 动手前先看 `git status --short --branch`，识别已有脏项。已有的用户改动、未完成 bot 目录、gitlink 噪声一律不回滚、不清理、不顺手提交。
3. 改代码前先从 `main` 开任务分支，默认命名 `codex/<task-name>`；在分支内完成修改和提交，再切回 `main` 合并并 push。
4. 只改当前任务需要的文件。除非你明确要求，不做顺手重构、不改无关风格、不碰运行产物和生成目录。
5. 手工代码修改保持小步、可审查。优先沿用当前模块风格和已有 helper；只有真实降低复杂度或匹配现有架构时才新增抽象。
6. 协议类变更必须有回归测试。像 TCP action、下注语义、card mapping、THP、adapter 这类边界，只改代码不加测试不算完成。
7. Web 进化相关变更要同时检查提示词和质量门。不能只修 Python 代码，却让 Master/Worker/Reviewer 继续传播旧规则。
8. 提交前至少跑和改动范围匹配的验证。窄改动跑 py_compile/目标 pytest；跨协议或质量门改动要跑 `sever/tests` 和相关 Web import/调用冒烟。
9. 暂存只显式列出本次任务文件，不用 `git add -A`。`web/core/results/`、`web/logs/`、`web/frontend/dist/`、`web/server/static/`、`results/*.json`、`ladder_results/`、`bots/graveyard/`、`.completed` 等运行/生成产物默认不提交。
10. 需要提交时，提交信息描述行为变化；提交后切回 `main` 做 `git merge --no-ff codex/<task-name>`，再 push `main`。推送失败就报告具体错误，不伪装完成。
11. 最终汇报要说明改了什么、验证了什么、分支名、合并/推送结果，以及仍然存在但未触碰的无关脏项。
