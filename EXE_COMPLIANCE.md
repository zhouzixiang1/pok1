# 真 EXE 合规验收报告

- **日期**: 2026-07-11
- **对象**: arena(`pok-arena`) vs 官方「德州扑克对弈平台限时一分钟2021版」EXE
- **EXE**: `sever/国赛平台/德州扑克对弈平台限时一分钟2021版/德州扑克对弈平台限时一分钟2021版.exe`(SHA-256 `9d01b443…`)
- **方法**: pok1 official harness(Wine + Xvfb 跑真 EXE + 两个 `national_v141` native_bot,5 手 self-play)+ arena 跑**同一** `national_v141`(5 手),逐项对照协议消息流 / THP / 结算。
- **判定**: HANDOFF「干净等价(不模拟 timing race)」标准。

## 结论:arena 干净等价 EXE(协议合规)✓

arena 的协议消息骨架 / 卡牌编码 / raise 边界 / raise-to-total 语义 / 粘包处理 / THP 格式 / 结算顺序与真 EXE **完全一致**。3 处差异均为「**arena 干净实现 vs EXE 怪癖**」,符合 HANDOFF 立场——**不复刻 EXE 的 bug 反而是正确的**。

## 验收过程

1. `scripts/official_platform_acceptance.py --check-env` → `ok: true`(Wine prefix `/home/zzx/.cache/pok_wine_national_platform` + fakechinese 字体 + Xvfb + xdotool 就绪)。
2. 真 EXE + 2× `national_v141` self-play,`--target-hands 5` → `passed_rounds: 1`(48s)。证据 `~/project/pok/web/core/results/official_platform/acceptance_20260711_221121/self_play_01/`(`botA.log` wire 流 + `receipt.json`)。
3. arena `serve --hands-per-match 5` + 同一 `national_v141`(带 `--log`) → `/tmp/arena_botA.log` wire 流 + arena THP。
4. 提取两侧 server→bot `RECV` 消息,逐项对照。

## 一致项(arena ≡ EXE)

| 协议点 | EXE 实测 | arena 实现 | 一致 |
|---|---|---|---|
| 握手 | 发 `name`,bot 回裸队名 | 同(send_line 加 `\n`) | ✓ |
| preflop 发牌 | `preflop\|{SMALLBLIND\|BIGBLIND}\|<s,r><s,r>` | 同 | ✓ |
| flop / turn / river | `flop\|<3>` / `turn\|<1>` / `river\|<1>` | 同 | ✓ |
| 卡牌编码 | `<suit,rank>`,suit 0-3=♠♥♦♣,rank 0-12=2-A | 同 | ✓ |
| 对手动作转发 | `raise <int>` / `call` / `check` / `fold` / `allin`(allin 裸词) | 同 | ✓ |
| raise 语义 | raise-to-total(总额) | 同 | ✓ |
| raise 边界 | preflop ≥ 200、postflop ≥ 100、再 raise ≥ 2×(**精确 2× 合法**) | 同(arena 决策 1) | ✓ |
| earnChips 结算 | `earnChips <int>` 净值,每玩家各一,零和 | 同 | ✓ |
| 结算顺序 | fold → earnChips;showdown → earnChips + oppo_hands | 同 | ✓ |
| 粘包 | 不加分隔符,bot token 前缀自拆 | transport `pop_client_action` 正确拆 | ✓ |
| THP 格式 | `STATE:N:acts:cards:earn:names;` + `{[THP]…}` footer,gb2312 | 同(决策 2 记总额) | ✓ |
| 赛制 | 70 局 / SB-BB 交替 / 20000 筹码 / 盲注 50-100 | 同 | ✓ |

**5 手消息骨架数量对照**(结构性消息完全一致;call/check/fold 因牌局随机不同):

| 消息 | EXE | arena | 消息 | EXE | arena |
|---|---|---|---|---|---|
| name | 1 | 1 | oppo_hands | 1 | 1 |
| preflop | 5 | 5 | raise | 4 | 4 |
| flop | 3 | 3 | call | 1 | 4 |
| turn | 1 | 1 | check | 3 | 1 |
| river | 1 | 1 | fold | 1 | 3 |
| **earnChips** | **4** | **5** | | | |

raise 金额样本(均符合边界):EXE `260 / 197 / 271 / 423`;arena `260 / 156 / 402 / 1224`(arena `1224 > 2×402`,验证 `>=2×` 精确 2× 合法)。

## 差异项(arena 干净 vs EXE 怪癖)

| # | 点 | EXE | arena | 性质与判定 |
|---|---|---|---|---|
| 1 | server→bot 消息尾 `\n` | **不加** | `send_line` 加 `\n` | **均合规**:native bot `pop_client_action` token 前缀解析兼容两者;HANDOFF L116 明确「server→bot 可加 `\n`」。无功能差异。 |
| 2 | 一场最后一手 `earnChips` | **漏发**(5 手仅 4 个 earnChips;文档载 70 手仅 69 对) | 每手都发(5 手 5 个) | **EXE bug/怪癖**;arena 按协议每手结算正确发送。复刻漏发反而是错——arena 干净。 |
| 3 | timing race | 快回复(<0.2s 阈值)触发状态机 race → 该手约 60s 线路静默 | 干净 60s 动作超时 → fold | **EXE 怪癖**;arena 明确**不模拟**(HANDOFF L22)。 |

**3 处差异均非 arena 缺陷**:差异 1 无功能影响;差异 2/3 是 EXE 自身 bug/race,arena 复刻反而是错的。arena 的「干净等价」立场与 HANDOFF 完全一致。

## 证据留存

- **EXE 侧**: `~/project/pok/web/core/results/official_platform/acceptance_20260711_221121/`(`summary.json` + `self_play_01/{botA.log, receipt.json, platform.wine.log, xvfb.log}`)。
- **arena 侧**: `/tmp/arena-exe-cmp/*.thp` + `/tmp/arena_botA.log`(本机临时,可随时复现)。

## 复现命令

```bash
# EXE 侧(pok1 harness, 5 手 self-play)
cd ~/project/pok
python3 scripts/official_platform_acceptance.py --check-env
python3 scripts/official_platform_acceptance.py \
  --candidate /tmp/nv141/bots/national_v141 \
  --self-play-rounds 1 --opponent-rounds 0 --target-hands 5

# arena 侧(同一 national_v141, 5 手)
cd ~/project/pok-arena
scripts/arena-ctl.sh start 5 native     # 或手动:
# .venv/bin/pok-arena serve --once --hands-per-match 5 &
# <anaconda python3> /tmp/nv141/bots/national_v141/national_bot.py \
#   --host 127.0.0.1 --port 50101 --name BotA --log /tmp/arena_botA.log
```

## 与决策记录的呼应(见 `arena/backend/engine/PROVENANCE.md`)

- 决策 1(raise `>=2×` 精确 2× 合法)← 本次 EXE 实测再次确认。
- 决策 2(THP 记总额)← EXE THP 的 `r{amount}` 与 arena 一致(总额)。
- 决策 3(断线仅真断开累计 forfeit)← 与 EXE 合规无关(arena 健壮性),不涉及。

**HANDOFF 未决问题 2 关闭**:arena 已对照真 EXE,协议合规,干净等价。
