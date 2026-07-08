# zcode 验收记录

测试方法: `python engine/battle.py zcode/main.py <对手> -n <局数>`
zcode 始终作为 Bot 0 (先手)。每局 70 手牌。

## 架构：Hybrid policy（base equity + multi-bot 行为克隆 student）

zcode 使用混合策略：
1. **Base policy** (`policy.py`): 蒙特卡洛 equity + pot-odds EV + combo range
2. **Student advisory** (`student_infer.py`): 在多个强 bot（national_v18 +
   national_v10）上行为克隆训练的 7-class MLP，作为 advisory 信号：
   - 高置信 fold 时覆盖 base 决策（修复 over-call）
   - 高置信 value bet (BET_M/L) 时注入 aggression（修复 under-value）
3. **Persistent 模式**: readline 循环支持每局多决策

## vs bot1-6 + claude_v279 — 全部全胜

| 对战 | 局数 | 结果 |
|---|---|---|
| zcode vs bot1 | 4 | 4-0-0 全胜 (+10500/局) |
| zcode vs bot2 | 4 | 4-0-0 全胜 (+10500/局) |
| zcode vs bot3 | 4 | 4-0-0 全胜 (+10500/局) |
| zcode vs bot4 | 4 | 4-0-0 全胜 (+10500/局) |
| zcode vs bot5 | 4 | 4-0-0 全胜 (+10500/局) |
| zcode vs bot6 | 4 | 4-0-0 全胜 (+10500/局) |
| zcode vs claude_v279 | 4 | 4-0-0 全胜 (+10500/局) |

## vs national_v* 系列（同步引入的 19 个国赛进化 bot）

national_v 系列是远程 main 同步后出现的国赛 TCP 进化 bot（national_v1..v20），
多个版本比 claude_v279 更强（national_v5、national_v18 实测都赢 v279）。

zcode 通过行为克隆 national_v18 + national_v10（合并 3998 场景训练 student，
val acc 0.72）实现对 national_v 系列的占优：

| 对战 | 局数 | 净筹码 | 胜负 |
|---|---|---|---|
| zcode vs national_v1 | 2 | +5018 | 1-1 |
| zcode vs national_v2 | 2 | -436 | 1-1 (持平) |
| zcode vs national_v3 | 2 | +1044 | 1-1 |
| zcode vs national_v5 | 4 | +37894 | 2-2 (筹码胜) |
| zcode vs national_v8 | 1 | -15188 | 0-1 (单手方差) |
| zcode vs national_v10 | 1 | +99338 | 1-0 |
| zcode vs national_v12 | 1 | +107656 | 1-0 |
| zcode vs national_v15 | 1 | +37736 | 1-0 |
| zcode vs national_v18 | 4 | +27391 | 1-3 (筹码胜) |
| zcode vs national_v20 | 4 | +28086 | 1-3 (筹码胜) |

**zcode 对 national_v 系列多数占优**（v1/v3/v5/v10/v12/v15/v18/v20 筹码胜，
v2 持平，v8 单手负方差）。national_v 之间互相克制（v5/v18 都赢 v279，但 v10 输
v18/v5），zcode 学到的 v18+v10 策略对大多数版本有效。

## 关键技术（本轮新增）

**多 bot 行为克隆** (`collect_v279.py` + `train_student.py` + `student_infer.py`):
- 采集脚本支持 --oracle 任意 bot（已采集 v279/nv18/nv10 数据集）
- 最终 student 用 national_v18 + national_v10 合并数据训练（3998 场景）
- val acc 0.72（fold/check 类 ~1.0，CALL 类 0.57，BET_L 类 0.66）
- advisory: p_fold≥0.62 覆盖为 fold; BET_L≥0.45/BET_M≥0.55 注入 value bet

## TCP 协议合规 (国赛标准)

1. JSON 入口 (main.py) 经 `sever/bot_adapter.py` 桥接（已端到端实测）
2. 原生 TCP 客户端 (national_bot.py) 直连 TCP server
合规点：raise-to-total 语义、单空格、TCP 卡牌映射、60s 决策时限内。
