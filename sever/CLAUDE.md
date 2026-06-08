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
python bot_adapter.py --bot ../bots/claude_v5 --name test
```

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
- **Raise semantics**: `raise X` = raise TO X (total stage bet), consecutive > 2× previous (strictly greater)
- **Timeout**: 60 seconds per action → fold
- **Illegal action → fold**: 13 rules covering bet/call/check/raise/allin restrictions

## Card Encoding Difference

TCP 协议: `(suit, rank)` where suit=0-3=♠♥♦♣, rank=0-12
engine/judge.py: integer 0-51, `number = card // 4 + 2`, `suit = card % 4` (♥=0,♦=1,♠=2,♣=3)

bot_adapter.py 转换: `card_int = rank * 4 + _TCP_TO_JUDGE_SUIT[tcp_suit]` (经映射表转换)
  映射表: TCP 0=♠→judge 2=♠, TCP 1=♥→judge 0=♥, TCP 2=♦→judge 1=♦, TCP 3=♣→judge 3=♣

## THP Record Format

比赛结束后自动生成国赛标准 THP 棋谱文件到 `records/` 目录。
- 文件命名: `THP-{teamA} vs {teamB}-{winner}胜-{yyyymmddHHMM}.txt`
- 格式: `STATE:N:actions:cards:earnings:players;` (每手一行，GB2312 编码)
- 卡牌: `{rank}{suit}` (rank=23456789TJQKA, suit=shdc)
- 动作: `r{amount}`=raise, `c`=call/check, `f`=fold, 阶段用`/`分隔
- 手牌: BB手牌|SB手牌/flop/turn/river (大盲注在前)
- 筹码: `A赢|B赢` (正=赢, 负=输)
- 文件尾: `{[THP][teamA][teamB][result][datetime][event]}`
- API: `GET /api/record/thp` 列表, `GET /api/record/thp/{filename}` 下载
