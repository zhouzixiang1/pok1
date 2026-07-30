# pok-arena 平台 Wiki

> botzone 风格德州扑克持久化对战平台。本文档是平台的完整使用与开发指南。
> 前端 `/wiki` 页面渲染本文档(精简版),这里是完整版。

## 目录
1. [快速开始](#1-快速开始)
2. [Bot 开发指南](#2-bot-开发指南)
3. [JSON 协议规范](#3-json-协议规范stdin-stdout)
4. [TCP 国赛协议规范](#4-tcp-国赛协议规范)
5. [卡牌编码](#5-卡牌编码)
6. [动作语义](#6-动作语义)
7. [Docker 沙箱说明](#7-docker-沙箱说明)
8. [TCP bot 容器内桥代理原理](#8-tcp-bot-容器内桥代理原理)
9. [游戏规则](#9-游戏规则)
10. [非法行为(13 条)](#10-非法行为13-条)
11. [评分系统 Glicko-2](#11-评分系统-glicko-2)
12. [常见问题](#12-常见问题)

---

## 1. 快速开始

### 平台部署(管理员)
```bash
cd ~/project/pok-arena
cp .env.example .env   # 填写 SMTP_* / POK_PLATFORM_HOST=0.0.0.0
.venv/bin/pip install -e '.[dev]'
(cd arena/frontend && npm install && npm run build)
scripts/platform-ctl.sh start   # 读 .env,默认绑 0.0.0.0:50280
```

公网加固要点:
- 注册/登录需**图形或算术验证码**;注册后须**邮箱验证**才能登录
- 密码重置发邮件验证码(不再明文返回 token);Admin 可编辑邮件模板并试发
- IP 限流、安全响应头、对战并发上限、Docker `--cap-drop=ALL` 等已默认开启
- **不要**把旧 TCP `serve`(50101)一并公网暴露
- SMTP 密码只放 `.env`,勿 commit

生产部署(systemd user unit,不碰系统服务):
```bash
mkdir -p ~/.config/systemd/user
cp deploy/pok-arena-platform.service ~/.config/systemd/user/
# 建议在 unit 里 EnvironmentFile=%h/project/pok-arena/.env
systemctl --user daemon-reload
systemctl --user enable --now pok-arena-platform
journalctl --user -u pok-arena-platform -f
```

### 用户使用流程
1. 打开平台首页 → **注册**(填验证码)→ **查收邮件验证码** → **登录**(再填验证码)
2. **我的 Bot** → 上传 bot 源码包(.zip)+ 填写协议类型(JSON/TCP)和入口文件
3. **发起对战** → 选我的 bot + 选对手(他人的 bot 或平台内置 national_v*)
4. **实时观赛**(SSE)或赛后**对局回放**(逐手/逐步推进)
5. **排行榜**看 Glicko-2 天梯

### TCP 通道(保留,国赛兼容)
原 TCP 观赛平台仍在(端口 50101/50180),用于国赛 EXE 兼容场景:
```bash
.venv/bin/pok-arena serve                    # TCP 平台
.venv/bin/pok-arena connect 127.0.0.1 50101 BotA   # 连接 bot
```
详见 `HANDOFF.md`。新平台与 TCP 通道**完全隔离**(独立 db / 独立端口)。

---

## 2. Bot 开发指南

### 选择协议

| 协议 | 通信方式 | 适用场景 | 推荐? |
|------|---------|---------|-------|
| **JSON(stdin/stdout)** | 平台写 JSON 到 bot stdin,bot 从 stdout 读 JSON | 新平台 Docker 沙箱 | ✅ 推荐(平台原生) |
| **TCP 国赛** | bot 用 socket 连容器内回环 127.0.0.1:50101 | 已有国赛 bot 代码,不想改 | 兼容(容器内自动挂桥) |

**建议**:新写的 bot 用 JSON 协议(更简单,适合沙箱)。已有国赛 TCP bot 可直接上传,平台自动用容器内桥代理翻译,**bot 代码零改动**。

### JSON bot 最小示例(Python)

```python
# main.py
import json, sys

def decide(req, history):
    """根据当前状态决策。返回动作整数。"""
    my_chips = req["my_chips"]
    to_call = max(0, req.get("round_bet", 100) - req.get("my_round_bet", 50))
    # 示例策略:永远 call(或 check)
    return 0  # 0 = call(有 to_call)或 check(无 to_call)

for line in sys.stdin:
    payload = json.loads(line.strip())
    requests = payload["requests"]
    req = requests[-1]  # 最新一个请求是当前决策点
    action = decide(req, requests)
    print(json.dumps({"response": int(action)}))
    sys.stdout.flush()
```

完整策略示例参考 `bots/national_v142/main.py` + `strategy.py`。

### 上传要求
- 打包成 **.zip**,根目录或子目录含**入口文件**(默认 `main.py`,TCP 协议默认 `national_bot.py`)
- 上传时声明:**协议类型**(json/tcp)、**入口文件名**、**运行时语言**(目前仅 python)
- 每次上传生成新**版本号**,排行榜只评最新版
- 限制:源码包 ≤ 50MB,解压后 ≤ 200MB,文件数 ≤ 2000

### 多文件 bot
zip 可含多个 .py 文件(如 `main.py` + `strategy.py` + `card_utils.py`),入口文件用相对 import 即可(`from strategy import get_action`)。

---

## 3. JSON 协议规范(stdin / stdout)

### 通信模型
平台每次需要 bot 决策,向 bot stdin 写**一行 JSON**;bot 从 stdout 回**一行 JSON**。每回合一个请求-响应。

### 请求格式(平台 → bot)

平台发送**累积请求历史**(每次都带上之前所有请求,bot 可从中重建完整状态):
```json
{
  "requests": [
    {第1次请求}, {第2次请求}, ..., {当前请求}
  ]
}
```

每个请求元素字段:
| 字段 | 类型 | 说明 |
|------|------|------|
| `my_id` | int (0/1) | 当前 bot 的座位 |
| `dealer_id` | int (0/1) | 本手 dealer(= 小盲位) |
| `my_cards` | [int, int] | 本 bot 手牌(整数编码,见 §5) |
| `public_cards` | [int, ...] | 公共牌(preflop=[], flop=3张, turn=4, river=5) |
| `history` | [...] | 本手到当前为止所有动作(见下) |
| `hand` | int | 当前手数(从1) |
| `max_hand` | int | 总手数(70) |
| `my_chips` | int | 本 bot 当前剩余筹码 |
| `opponent_chips` | int | 对手剩余筹码 |
| `small_blind` | int | 50 |
| `big_blind` | int | 100 |

`history` 每个元素:
```json
{"round": 0, "player_id": 1, "action": 200, "action_type": "raise"}
```
- `round`:0=preflop, 1=flop, 2=turn, 3=river
- `player_id`:执行动作的玩家(0/1)
- `action`:数值(fold=-1, call/check=0, raise=加注总额, allin=-2)
- `action_type`:"fold"/"call"/"check"/"raise"/"allin"

### 响应格式(bot → 平台)
```json
{"response": <int>}
```
- `-2` = allin(全押)
- `-1` = fold(弃牌)
- `0` = call(有待跟注)或 check(无待跟注),由平台判断
- `>0` = raise 到该**总额**(raise-to-total,见 §6)

---

## 4. TCP 国赛协议规范

> 这是国赛官方 EXE 平台的协议。新平台通过**容器内桥代理**支持,bot 代码零改动。

### 通信模型
bot = TCP 客户端,连容器内 `127.0.0.1:50101`(回环,容器内永远成立)。

### 握手
平台发 `name\n`,bot 回**裸队名 UTF-8 字节无分隔符**。

### server → bot token 词表
- `name` — 询问队名
- `preflop|{SMALLBLIND|BIGBLIND}|<卡牌>` — 发手牌 + 盲注身份
- `flop|<3张卡牌>` / `turn|<1张>` / `river|<1张>` — 公共牌
- `earnChips <int>` — 本手净筹码(可负)
- `oppo_hands|<2张>` — 对手手牌(仅摊牌时)
- 转发对手动作:`call` / `check` / `fold` / `allin` / `raise <总额>`
- `bet` **永不发送**(用 raise 代替)

### bot → server 动作
`raise <总额>` / `fold` / `call` / `check` / `allin`(raise 与筹码量之间**有且仅有一个空格**)

### 默认连接参数
```
--host 127.0.0.1 --port 50101 --name <队名>
```
环境变量优先:`BOT_HOST` / `BOT_PORT` / `BOT_NAME`(平台自动注入 BOT_NAME)。

---

## 5. 卡牌编码

### TCP 协议(文本)
`<suit,rank>` 格式,如 `<0,12>` = 黑桃 A。
- `suit`:0=♠(黑桃), 1=♥(红桃), 2=♦(方块), 3=♣(梅花)
- `rank`:0=2, 1=3, ..., 8=10, 9=J, 10=Q, 11=K, 12=A

### JSON 协议(整数 0-51)
`card = rank * 4 + JUDGE_SUIT`,其中 `JUDGE_SUIT` 由平台 suit 映射:
| 平台 suit | JUDGE_SUIT | 花色 |
|-----------|-----------|------|
| 0(♠) | 2 | 黑桃 |
| 1(♥) | 0 | 红桃 |
| 2(♦) | 1 | 方块 |
| 3(♣) | 3 | 梅花 |

示例:
- ♠A(平台 suit=0, rank=12)→ `12*4 + 2 = 50`
- ♥A(平台 suit=1, rank=12)→ `12*4 + 0 = 48`
- ♣2(平台 suit=3, rank=0)→ `0*4 + 3 = 3`

**注意**:这个映射与 bots 内部的 `card_utils.py` 一致(`card%4`=suit, `card//4+2`=rank)。

---

## 6. 动作语义

### 共通规则
- **筹码**:每手起始 20000,**一局一复位**(不是累计锦标赛)
- **盲注**:小盲 50 / 大盲 100
- **先后手**:preflop 小盲先;flop/turn/river 大盲先。70 手交替大小盲
- **超时**:每动作 60 秒,超时 = fold

### raise(加注)
**raise-to-total 语义**:`raise X` 表示加注到**本街总额 X**(不是增量)。
- 最小:preflop 首 raise ≥ 200,postflop 首 raise ≥ 100
- 再 raise:**≥ 2× 上次 raise 总额**(精确 2× 合法,如 raise 200 → raise 400 合法)
- raise 用尽全部筹码须用 `allin`

### call / check
- `call`:跟注(补齐到对手本街下注额),金额自动计算
- `check`:过牌(仅无待跟注时合法,如 preflop BB 首动作)
- JSON 协议中 call 和 check 都是 `response: 0`,由平台据 to_call 自动判断

### allin(全押)
- 投入全部剩余筹码
- allin 被 call 后:自动发完剩余公共牌(无下注)→ 摊牌
- 连续两个 allin:第二个非法 → fold

### bet
**永不使用**(协议规定用 raise 代替)。发 bet 会被判非法 → fold。

---

## 7. Docker 沙箱说明

用户上传的 bot 在 **Docker 容器**内运行,与平台隔离:

- **基础镜像**:`python:3.12-slim`
- **非 root 运行**:容器内用 `botuser` 用户
- **网络隔离**:`--network=none`(容器无网络,防恶意外联)
- **资源限制**:`--memory=512m` `--cpus=0.5`(防资源滥用)
- **超时**:每决策 60 秒,超时判 fold
- **通信**:stdin/stdout(平台写 JSON 到 bot stdin,读 bot stdout JSON)

镜像命名:`arena-bot-<bot_id>:v<version>`,每次上传新版本重建镜像。

---

## 8. TCP bot 容器内桥代理原理

TCP bot 上传后,平台构建的镜像**额外包含一个 `tcp_bridge.py` 桥进程**。用户 bot 代码**零改动**。

```
Docker 容器内:
  ┌─ tcp_bridge.py(桥)──────┐    ┌── 用户 TCP bot ──┐
  │ 监听 127.0.0.1:50101      │←──│ connect 回环地址  │
  │ stdin←平台(JSON 状态)    │    │ national_bot.py   │
  │ socket→bot(TCP文本)      │───→│ (零改动)          │
  │ socket←bot(TCP动作)      │←───│                   │
  │ stdout→平台(JSON响应)    │    └───────────────────┘
  └───────────────────────────┘
```

- bot 的 `--host 127.0.0.1 --port 50101` 在容器内回环**永远成立**
- 平台只跟桥用 stdin/stdout(JSON)通信,桥负责 JSON↔TCP 文本双向翻译
- 队名通过环境变量 `BOT_NAME` 注入(优先级:env > 命令行 > 默认 "Bot")
- 桥复用平台 `transport.py` 的 token 前缀分帧逻辑(不依赖 `\n`)

---

## 9. 游戏规则

### 一场比赛
- **70 手**对局
- 每手起始 20000 筹码,**一局一复位**
- 小盲 50 / 大盲 100,70 手**交替**大小盲
- 一场胜负 = 70 手**累计净筹码**比较(相同为平局)

### 一手牌流程
1. **发牌**:每方 2 张底牌(暗牌)
2. **Preflop**(翻前):小盲先表态,下盲注(SB 50 / BB 100)
3. **Flop**(翻牌):发 3 张公共牌,大盲先表态
4. **Turn**(转牌):发 1 张公共牌,大盲先表态
5. **River**(河牌):发 1 张公共牌,大盲先表态
6. **摊牌**(Showdown):剩余玩家亮牌比大小

### 牌型大小(从高到低)
同花顺 > 四条 > 葫芦 > 同花 > 顺子 > 三条 > 两对 > 一对 > 高牌
- A 可作最大(顺子 A-K-Q-J-10)或最小(Wheel A-2-3-4-5)
- 平局:底池平分

### allin runout
allin 被 call 后,双方不再决策,自动发完剩余公共牌 → 摊牌。

---

## 10. 非法行为(13 条)

所有非法行为按 **fold** 处理(违规方弃牌,对手赢)。违规方静默(不发 error,避免 native bot 死锁),THP 记录违规方。

1. **bet 永不合法**(用 raise 代替)
2. flop/turn/river **第一个行为**出现 call → 非法
3. preflop 小盲 call 后,**大盲第一个行为**也 call → 大盲非法
4. flop/turn/river **非第一个行为**出现 check → 非法
5. preflop 除大盲第一个行为外出现 check → 非法
6. preflop **小盲第一个 raise** < 200 → 非法
7. preflop **大盲第一个 raise**:
   - 小盲 call 后 < 200 → 非法
   - 小盲 raise 后 < 小盲 raise 的 2× → 非法
8. **连续 raise** < 上次 raise 的 2× → 非法
9. flop/turn/river **第一个 raise** < 100 → 非法
10. raise **超过持有筹码** → 非法(应用 allin)
11. raise **等于全部筹码** → 非法(必须 allin)
12. allin 后再 raise → 非法(只能 call 或 fold)
13. **连续两个 allin** → 第二个非法

---

## 11. 评分系统 Glicko-2

平台用 **Glicko-2** 评分(贴合德扑实际,比 ELO 更优):

- **主评分**:rating(初值 1500)+ RD(不确定度,初值 350)+ vol(波动率,初值 0.06)
- **分数 S = 0.5 + 0.5·tanh(net_bb/scale)**:PokerBench 量级分数,保留「大胜 vs 小胜」的筹码差异(纯 W/L 会丢失这个信息)
- **双向零和更新**:A 的赢 = B 的输,对称更新
- **RD 反映不确定度**:新 bot RD 大(评分不确定),老 bot RD 小(评分稳定)

副指标:
- **bb/100**:每 100 手净大盲数(ACPC mbb/g 口径)
- **95% CI**:置信区间,CI > 0 才「真的更强」

每场比赛结束后双向更新双方 rating + 战绩(W/L/D)+ 净筹码 + 对局数,并重算该对 bot 的 bb/100 CI。

---

## 12. 常见问题

### Q: 我该选 JSON 协议还是 TCP 协议?
**新写 bot 用 JSON**(更简单,stdin/stdout 无网络)。已有国赛 TCP bot 直接上传(TCP 协议),平台自动用容器内桥代理,代码零改动。

### Q: 上传后 bot 不响应 / 超时 fold?
检查:
1. 入口文件名是否正确(JSON 默认 `main.py`,TCP 默认 `national_bot.py`)
2. zip 结构:入口文件应在根目录或某子目录(平台自动找)
3. JSON bot 是否正确读 `requests[-1]` 并返回 `{"response": int}`
4. 容器内非 root 运行,确认不依赖写 `/` 权限

### Q: 为什么我的 TCP bot 连不上?
TCP bot 在容器内连 `127.0.0.1:50101`(回环),不是平台所在机器 IP。bot 代码用默认 `--host 127.0.0.1 --port 50101` 即可,平台会注入 `BOT_NAME` 环境变量。

### Q: 一场比赛多长?
70 手,每手 4 个下注轮,每决策 60 秒超时。两个快速 bot 约 10-30 秒/场;慢决策 bot 可能数分钟。

### Q: 如何调试 bot?
1. 本地用 stdin/stdout 测试 JSON bot:`echo '{"requests":[...]}' | python main.py`
2. 平台「我的 Bot」→ 版本历史查看每次上传
3. 「对局回放」逐手查看每步双方动作和状态

### Q: 内置 bot 是什么?
平台预装了 `national_v115` 到 `national_v142` 共 10 个版本的国赛 bot(TCP 协议),可作对手或难度参考。新用户注册后即可选它们对战。

### Q: 管理员如何重置用户密码?
管理员登录 → 「管理」页面 → 输入用户名/邮箱生成一次性重置 token → 把 token 给用户 → 用户在「重置密码」页用 token 设新密码(24 小时有效)。无邮件服务,故 token 由管理员转交。

### Q: 平台会损坏机器上其他服务吗?
不会。平台:
- 用独立端口 **50280**(避开所有占用)
- 用独立 db `arena_platform.db`
- 用 systemd **--user** unit(不碰系统服务)
- 不修改现有 TCP 通道(端口 50101/50180 冻结只读)

---

## 参考
- `HANDOFF.md` — TCP 通道(国赛 EXE 复刻)完整文档
- `arena/backend/engine/PROVENANCE.md` — 引擎 copy 来源 + 协议决策
- `docs/EXPANSION_PLAN.md` — 平台扩展方案(评分算法调研)
- 权威规则:`sever/国赛平台/` 的规则.doc / 通信协议.docx / 非法行为说明.docx
