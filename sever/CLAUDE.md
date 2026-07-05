# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

德州扑克自对弈平台（国赛版）。TCP 竞赛服务器，支持两个 AI 引擎通过 Socket 对弈 70 局德州扑克，附带 Web 实时展示仪表板。

严格按照以下协议文档实现：
- `../sever/国赛平台/通信协议.docx` — 核心通信协议
- `../sever/国赛平台/非法行为说明.docx` — 13 条非法行为规则
- `../sever/国赛平台/自对弈平台使用及通信协议补充说明.docx` — 平台操作 + raise-to-total 语义
- `../sever/国赛平台/德州扑克规则.doc` — 比赛规则（70局、20000筹码、一局一复位）

## Commands

```bash
# 启动平台（TCP :10001 + Web :18080）
python main.py
python main.py --tcp-port 20001 --web-port 28080

# 启动测试客户端（需要2个）
python test_client.py 127.0.0.1 10001 BotA
python test_client.py 127.0.0.1 10001 BotB

# Bot 桥接（将本地 bot 连接到 TCP 服务器）
python bot_adapter.py --bot ../archive/evolution_epochs/<epoch>/legacy_bots/claude_v5 --name legacy-test

# 协议对齐回归测试
python -m pytest tests -q
```

The active autonomous evolution service on this machine runs from
`/home/zzx/project/pok/.evolution_pok`. Use `sever/` here as the national TCP
platform implementation and regression target; do not start a second evolution
loop from the outer `/home/zzx/project/pok` checkout while `.evolution_pok` is
active.

## Architecture

```
main.py                    # 入口：并发启动 TCP + Web
engine/
  deck.py                  # Card(suit,rank) + Deck
  evaluator.py             # 手牌评估（9级牌型 + kicker）
  game.py                  # GameEngine：单手牌局生命周期 + THP 记录
  validator.py             # 13 条非法行为规则验证
  thp_recorder.py          # THP 棋谱记录器（国赛标准格式）
server/
  tcp_server.py            # asyncio TCP 服务器 + MatchManager + THP 导出
  protocol.py              # 消息编解码
web/
  app.py                   # FastAPI + SSE 仪表板 + THP 下载 API
  static/                  # 前端（HTML/CSS/JS）
records/                   # THP 棋谱文件输出目录
```

## Key Protocol Rules

- **Transport**: TCP Socket, platform=server(:10001), engine=client
- **Card format**: `<suit,rank>` where suit 0-3=♠♥♦♣, rank 0-12=2-A
- **Match**: 70 hands, 20000 chips per hand (reset each hand), blinds 50/100
- **Action order**: Preflop SB first; Flop/Turn/River BB first
- **Client actions**: `raise <amount>`, `fold`, `call`, `check`, `allin`; `raise` 与金额之间有且只有一个空格，`bet` 永远非法
- **Raise semantics**: `raise X` = raise TO X (total stage bet), consecutive > 2× previous (strictly greater)
- **Postflop pass**: postflop 第一个行动不能 `call`；第一个玩家 `check` 后，第二个玩家必须用 `call` 结束该街，不能再发 `check`
- **All-in runout**: `allin` 被 `call` 后只发剩余公共牌、`earnChips` 和必要的 `oppo_hands`，客户端不得继续行动
- **Timeout**: 60 seconds per action → fold
- **Illegal action → fold**: 13 rules covering bet/call/check/raise/allin restrictions
- **Match start**: 第二个客户端连接后自动开赛；Web `/api/start` 仅作为仪表盘控制/兜底，比赛进行中会拒绝重复启动

## Card Encoding Difference

TCP 协议: `(suit, rank)` where suit=0-3=♠♥♦♣, rank=0-12
engine/judge.py: integer 0-51, `number = card // 4 + 2`, `suit = card % 4` (♥=0,♦=1,♠=2,♣=3)

bot_adapter.py 转换: `card_int = rank * 4 + _TCP_TO_JUDGE_SUIT[tcp_suit]` (经映射表转换)
  映射表: TCP 0=♠→judge 2=♠, TCP 1=♥→judge 0=♥, TCP 2=♦→judge 1=♦, TCP 3=♣→judge 3=♣

## THP Record Format

比赛结束后自动生成国赛标准 THP 棋谱文件到 `records/` 目录。
- 文件命名: `THP-{teamA} vs {teamB}-{winner}胜-{yyyymmddHHMM}-CCGC.txt`，文件名会替换路径危险字符
- 格式: `STATE:N:actions:cards:earnings:players;` (每手一行，GB2312 编码)
- 卡牌: `{rank}{suit}` (rank=23456789TJQKA, suit=shdc)
- 动作: `r{amount}`=raise, `c`=call/check, `f`=fold, 阶段用`/`分隔
- 手牌: BB手牌|SB手牌/flop/turn/river (大盲注在前)
- 筹码和参赛者: 按本手 BB|SB 顺序记录，和手牌顺序一致
- 文件尾: `{[THP][teamA][teamB][result][datetime][event]}`
- API: `GET /api/record/thp` 列表, `GET /api/record/thp/{filename}` 下载

## Git And Change Hygiene

The working tree may already contain user changes, generated match records, bot generations, or dirty gitlinks. Check `git status --short --branch` before editing and again before committing.

Do not revert, reset, restore, clean, or checkout unrelated files unless the user explicitly asks for that exact destructive operation.

`sever/` 改代码规范：

- 先对照 `sever/国赛平台/` 文档确认协议事实，再改 TCP 解析、validator、game flow、adapter 或 THP。
- 改代码前先从 `main` 开任务分支，默认命名 `codex/<task-name>`；在分支内完成修改和提交，再切回 `main` 合并并 push。
- 只改当前任务需要的文件，不顺手重构、不统一无关风格、不碰 `records/` 等运行产物。
- 协议行为、下注语义、postflop check/call、all-in runout、card mapping、THP 顺序等变更必须配套 `sever/tests` 回归测试。
- 改 adapter 时必须记住它只服务 legacy Botzone/local JSON bot；新进化 bot 的正式提交形态应原生支持国赛 TCP 协议，不应依赖 adapter 生成 TCP 文本。
- 最终汇报必须说明改了什么、跑了什么验证、提交/推送结果，以及哪些已有脏项未触碰。

Stage only files changed for the current task. Do not use `git add -A` unless the user explicitly asks for a full repository snapshot. Generated `records/`, runtime logs, bot generation sentinels, and unrelated bot directories should not be staged unless the task is specifically about them.

After a task that changes files, commit and push task-related changes:

```bash
git switch main
git pull --ff-only
git switch -c codex/<task-name>
git add <files you changed>
git commit -m "<descriptive message>"
git switch main
git merge --no-ff codex/<task-name>
git push
```

If the repository was dirty before the task, mention that in the final response and do not mix unrelated files into the commit.
