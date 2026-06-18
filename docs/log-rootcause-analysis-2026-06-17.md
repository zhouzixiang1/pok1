# 日志根因分析报告 — AIVAT 全下方差调整链 + Workflow 审计
> 日期：2026-06-17
> 范围：解析 `web/.claude/agent-memory/post-edit-bug-checker/` 与 `.claude/agent-memory/post-edit-bug-checker/` 下的 bug 记忆文件（"日志"），还原**日志产生的逻辑**、定位**根本原因（非表层）**，并标注**日志时间版本对应的代码 vs 当前代码**的差异。
> 方法：读真实代码 + 真实 judge.py 结算流 + 重建 d2fad78 旧代码做对照 + 合成真实牌局结构数值复算 + 跑 AIVAT 测试套件。

---

## 0. 结论速览（TL;DR）

这些"日志"是 **post-edit-bug-checker hook 在 2026-06-14 ~ 2026-06-16 期间产出的 bug 记忆文件**，分布在两个目录：

| 目录 | 关注子系统 | 记忆文件数 |
|------|-----------|-----------|
| `web/.claude/.../post-edit-bug-checker/` | 演化框架（web/core） | 6 |
| `.claude/.../post-edit-bug-checker/` | 引擎（engine/）+ agent_workers | 3 |

**核心发现（按严重度）：**

1. **AIVAT Phase-1 三连根因**（`aivat-phase1-broken-detection` + `aivat-sidepot-bias`）— 这是日志链的主体。三个根因都被我**独立复核确证**：
   - **根因 A（边池偏置）**：`expected_delta = equity*pot - contrib` 把退还的 side pot 当成"有风险"，注入 `(equity-1)*side_pot` 偏置。**数值复算确证**：硬币 flip（eq=0.5，side=5902）下旧公式输出 **−2951**，正确答案 **0**；当前代码（`main_pot=2*min(contrib)`）输出 **0**，零偏置。
   - **根因 B（realized-delta 读错字段）**：旧 `_hand_realized_delta` 扫 `final_result`，该字段**只在游戏终局帧**由 `make_finish_json` 设置，且值是**整局累计 `total_win_chips`**（`judge.py:574 +=`），不是单手 delta。→ 除最后一手外全部返回 0，最后一手返回整局累计。**逐行比对 `judge.py:483-498` 确证**。
   - **根因 C（split_log off-by-one）**：旧 `split_log_into_hands` 仅按 `matchdata.hand` 变化切分；但 `judge.py:577-578` 在渲染结算帧**之前**就把 `hand +=1`，导致携带 N 手 `temp_result` 的结算帧被错配进 N+1 手。当前代码加 `is_settlement_frame` 显式修正。**diff 确证**。

2. **AIVAT 当前状态：latent（休眠）/ 零回归**。生产 `elo_daemon.py:361` 调 `mirror_battle(...)` **不传 `aivat_enabled`**（默认 `False`），全仓无任何 caller 传 `True`。所以根因 A/B/C 在 Phase-1 提交时是**潜伏 bug**，不会污染线上评分——这是为什么"22 tests pass / 零回归"成立。

3. **修复已落地并测试覆盖**（commit `aa71fa0` 2026-06-16 21:56）。24 个 AIVAT 测试全过；边池测试 `test_stack_mismatch_side_pot_excluded`（cme=20000/copp=6000 → main_pot=12000）正是记忆文件建议补的测试，**已补**。

4. **Workflow 侧 5 个 P0**（来自 43-agent 审计 `docs/evolution-arch-audit-2026-06-16.md`）：`branch_from` 死字段 / Tuner 边界逃逸 / paired net_chips 丢失 / NEW file unlink / EXHAUSTED 过度标注 + `eval_rounds` 全量调度挤压。这些是**另一条独立日志线**（架构审计而非 post-edit），状态见 §4。

---

## 1. 日志是什么、怎么产生的（log-generating workflow）

### 1.1 这些"日志"的本质

目标所说的"日志"不是 runtime 日志文件，而是 **post-edit-bug-checker hook 的输出物**：每次代码编辑后，hook 派一个对抗性 reviewer agent 复核改动，把发现的深层 bug 写成 markdown 记忆文件存进 `.claude/agent-memory/post-edit-bug-checker/`。所以日志 = **编辑后深度复核报告**，每条都带：
- `description:`（一句话根因）
- 正文：why（机制）/ magnitude（量级）/ how to apply（修复指引）
- 关联引用（`[[related-bug]]`）

### 1.2 AIVAT 日志的产生路径（关键：理解日志怎么来的）

AIVAT 这几条日志是由 `/tmp/aivat_*.py` 这批**验证脚本**跑出来的（我已逐一读过）：
- `/tmp/aivat_verify.py`（12903B）— 主验证，捕获真实 judge log
- `/tmp/aivat_real_paired.py` — paired raw-vs-AIVAT 实跑
- `/tmp/aivat_refined.py` — 合成方差缩减实验
- `/tmp/aivat_zeroreg.log`（139KB）— 零回归实跑日志（**绝大多数是 BrokenPipeError 噪声**，有效输出仅末尾 `OFF net_chips_list: [150.0, -250.0, 150.0] OFF_OK True`）

**日志产生的真实逻辑链（我已用 judge.py 源码确证）**：

```
mirror_battle(save_log=True)
  → _run_one_mirror_game → judge() (engine/judge.py)
     → 每手牌结算时 judge.py:562-586：
         player_chips = get_player_final_chips(result)   # 边池调整后
         result[i].win_chips = chips - mean_chips        # 单手均值中心 delta
         matchdata["total_win_chips"][i] += delta        # 累计【整局】
         if 还有手牌: matchdata["hand"] += 1             # 【渲染前就+1】← off-by-one 根源
                     make_request_json(..., result)      # 把 N 手 delta 挂到 hand=N+1 的帧
         else:       make_finish_json(...)               # final_result = total_win_chips（累计）
  → all_logs 收集每局完整 judge log
  → AIVAT 脚本 split_log_into_hands + _extract_allin_snapshot + _hand_realized_delta
     ↑ 旧实现在这里读错字段 / 切错手 / 用错底池 → 偏置
```

---

## 2. 根本原因（非表层）— AIVAT 三连

### 根因 A：边池偏置（side-pot bias）

| | 表层 | 根因 |
|---|------|------|
| 现象 | AIVAT 调整后均值偏移几千 chips | — |
| 表层归因 | "equity 计算错" / "eligibility 漏了" | ✗ 都不对 |
| **真根因** | — | **judge.py 的 NLHE 规则不强制覆盖方 match 全下**。一方 shove 后，覆盖方可以 **call（不全下）** 留筹码在后，结算时 `get_player_final_chips`（`judge.py:415-435`）把 unmatched side pot **退还**给覆盖方。但旧 AIVAT 公式 `equity*pot - contrib` 里的 `pot` 是 **river 最后 display 的全 pot（含已退还的 side pot）**，把"已退还、无随机事件"的筹码当成"在险"→ 注入偏置 `(equity-1)*side_pot`。|

**量级（记忆文件 40-replay 扫描）**：96% 的 allin-showdown 有非零 side pot，均值 3555 chips，max 19900。这是**系统性、与技能相关**（shove 越大 side pot 越大偏置越大），不是零均值。

**我的独立数值复算**（`/tmp/verify_synthetic_real.py`，复现记忆里的真实牌局 pot=34098/cme=20000/copp=14098/side=5902）：

```
equity=0.50（硬币 flip，真 EV 应为 0）:
  OLD    delta (full pot)  = -2951.0   bias vs true = -2951.0   ← 与记忆文件 "current=-2951 (wrong)" 完全吻合
  CURRENT delta (main pot) =    0.0    bias vs true =    0.0    ← 修复正确
equity=0.82（AA vs KK 类）:
  OLD    bias = -1062.4
  CURRENT bias = 0.0
```

**修复**（当前代码 `aivat.py:248-249,303`）：`matched_contrib = min(contrib_me, contrib_opp)`；`main_pot = 2*matched_contrib`；`expected_delta = equity*main_pot - matched_contrib`。✅ 已落地、已测试。

---

### 根因 B：realized-delta 读错字段（per-hand vs 整局累计）

| | 表层 | 根因 |
|---|------|------|
| 现象 | 非 allin 手的 realized delta 几乎全是 0，最后一手炸到整局总计 | — |
| 表层归因 | "temp_result 解析 bug" | ✗ |
| **真根因** | — | **judge.py 有两个完全不同的结果字段，旧代码混用了**：(1) `temp_result[i].win_chips`（`judge.py:569-573`）= **单手** delta，挂在**下一手首个 display**；(2) `final_result[i].win_chips`（`make_finish_json:487-491`）= **整局累计** `total_win_chips`，只挂在**游戏终局帧**。旧 `_hand_realized_delta`（d2fad78）**只扫 `final_result`**（以为它是单手 delta），实际拿到的是整局累计 → 除最后一手外返回 0，最后一手返回整局总计。|

**证据**：旧代码逐字（`git show d2fad78:engine/aivat.py`）：

```python
def _hand_realized_delta(judge_log_hand, perspective):
    for entry in reversed(judge_log_hand):
        ...
        if isinstance(display, dict) and "final_result" in display:  # ← 只读 final_result
            ...
            return float(wc)   # ← wc = total_win_chips（累计），非单手
    return 0.0
```

而 `judge.py:484,488-491`：`final_result` 来自 `matchdata["total_win_chips"]`，后者在 `judge.py:574` 用 `+=` 累加每手 delta。**记忆文件的 `-20460 chips` 偏置（raw +19650 vs aivat -810）正是这个机制**：realized 读成整局累计，叠加到 aivat_adjust_hand 的非 allin 手返回值上。

**修复**（当前代码 `aivat.py:307-362`）：`_hand_realized_delta` 改读 `temp_result`（单手 delta），`final_result` 降级为终局手 last-resort fallback；`aivat_adjust_game`（`:446-463`）再用 `final_total - sum(realized[:-1])` 反推终局手 delta 防双计。✅ 已落地。

---

### 根因 C：split_log_into_hands off-by-one

| | 表层 | 根因 |
|---|------|------|
| 现象 | per-hand 段落里 `temp_result` 错位（N 手的 delta 出现在 N+1 段） | — |
| **真根因** | — | **judge.py:577-578 在 make_request_json 渲染前就执行 `matchdata["hand"] += 1`**。所以携带 N 手结算结果（`temp_result`）的 display，其 `matchdata.hand` 已经是 N+1。旧 `split_log_into_hands` 纯按 `hand` 字段变化切边界，于是把 N 的结算帧切进了 N+1 段，`temp_result` 跨段错位。|

**修复**（当前代码 `aivat.py:382-433`）：显式 `is_settlement_frame`（display 带 `temp_result` 或 `command=="finish"`）；遇到 settlement 帧且 `hand_idx != current_hand_idx` 时，**追加到当前段但不推进 `current_hand_idx`**，让真正的下一手 action 帧才触发边界。✅ 已落地。

> 注：根因 B 和 C **互相放大**——C 让 temp_result 错段，B 又只读 final_result，两者叠加使 realized 几乎全错。修复需同时修 B+C 才正确（当前代码两者都修了）。

---

## 3. 版本时间线（日志版本 vs 当前代码）

```
2026-06-14 ─ commit 2608f36  全量修复架构审计 P0+P1（含 AIVAT 工作开始）
2026-06-16 10:18 ─ commit 9613f70  docs: 43-agent workflow 架构审计报告（5 P0）
2026-06-16 ~19:00 ─ 【生成 /tmp/aivat_*.py 验证脚本，捕获真实 judge log】
2026-06-16 ~19:14-19:32 ─ 写 aivat-phase1-broken-detection 记忆（指出 d2fad78 的 B+C 两根因）
2026-06-16 21:05 ─ 写 aivat-sidepot-bias 记忆（指出修复后遗留的根因 A）
                       ↑ 此时 commit aa71fa0 尚未提交
2026-06-16 21:56 ─ commit aa71fa0  fix(aivat): repair real-log all-in adjustment
                       ↑ 吸收了 A+B+C 三根因修复 + 补边池测试，22→24 测试
当前 (2026-06-17) ─ 工作树干净（AIVAT 相关），24 测试全过
```

**关键对应关系：**

| 记忆文件（日志） | 写于 | 针对的代码版本 | 当前代码状态 |
|---|---|---|---|
| `aivat-phase1-broken-detection` | 06-16 19:18 | `d2fad78`（Phase-1 原版） | **已修**（aa71fa0）— B+C 两根因都已落地 |
| `aivat-sidepot-bias` | 06-16 21:05 | aa71fa0 **修复中**（边池尚未处理） | **已修**（aa71fa0 后续包含）— `main_pot=2*min(contrib)` 已在 |
| `phase2-sprt-truncation` | 06-16 23:23 | Phase-2 decision_tester | 自包含，30 测试过；`run_decision_tests_sprt` **尚未接入 tool_gates**（潜伏） |
| `phase0-schema-change-test-staleness` | 06-16 14:52 | Phase-0 未提交 diff | 4 测试仍 fail（预期，schema 驱动）；`get_global_stats` total_hands undercount 未修 |
| `attempt-top-reset-wipes-sequential-sibling` | 06-16 23:23 | B-group 未提交 diff | 未修（sequential-overlap 下删前序 worker 编辑） |
| `exploitability-probe-never-ran-fix` | 06-14 | 已修（8 代 blackout 根因：silent shutdown + nested-fork deadlock） | ✅ 已修 |
| `orchestrator-cost-negative-handler-conflation` | 06-14 | 诊断噪声非 bug | 接受现状（上游有精确信号） |
| `rc2b-classify-target-change-empty-dst-bug` | 06-16 | B-group 未提交 | 未修（1 测试 fail，影响有限） |
| `engine-judge-reraise-architectural-bug` | 06-09 | engine/judge.py（长期存在） | 未修（架构性，修改需注意盲注路径） |

---

## 4. Workflow 侧日志线（43-agent 架构审计）

这条线与 AIVAT **独立**，是 2026-06-16 由 43 个 agent（6 路代码深读 + 6 路文献 + 6 路综合 + 24 路对抗验证）产出的 `docs/evolution-arch-audit-2026-06-16.md`（1046 行）。其方法论值得注意：**每条建议都派一个"默认怀疑"的对抗 agent 用 Bash/Read 核实真实代码**，筛掉了大量过度声称（如 fix_injection 静默回归、draw 计分偏差都被判为 reject/降级）。

**5 个确证 P0（对抗验证 sound）**：
1. **paired net_chips 丢失**（最大单点缺陷）：`BattleResult` dataclass 无 `net_chips` 字段 → 生产 scheduler path 上 `res.get('net_chips')` 恒 `[]`，precommit 数值门退化为二元 W/L。NLHE 重尾 all-in 噪声被误判。
2. **NEW file unlink**：`agent_workers` 仅在 `src.exists` 时回滚 → 超时后 partial NEW file 污染 retry。
3. **EXHAUSTED 过度标注**：单代新机制被标 `[POSSIBLY EXHAUSTED]`（违反自己"3+ 连续代"规则），经 worker hard ban 自我拒绝 Master 授权的新方向。
4. **branch_from 死字段**：`run_master`/`_validate_master_plan` 从不读 `plan['branch_from']`，但 prompt 鼓励 Master 输出 → Master 以为换了祖先、Workers 实际仍从旧 source_v 演化。
5. **decision_tester fail-closed + raise-to-total validator**。

**这些与 AIVAT 的关系**：P0#1（paired net_chips）正是 AIVAT 要服务的"precommit 数值门"——AIVAT Phase-2 的目标就是给这个门提供方差缩减后的数值。所以 **AIVAT 的潜伏 bug 一旦在 Phase-2 接入就会激活**，必须先修 A+B+C（已完成）才能安全 wire-in。这也是 `phase2-sprt-truncation` 记忆里"NOT yet wired into tool_gates"的同一处缺口。

---

## 5. 完成度审计（against 目标）

| 目标要求 | 证据 | 状态 |
|---|---|---|
| 分析日志 | 9 个记忆文件全读 + /tmp 验证脚本全读 | ✅ |
| workflow 调研 | 43-agent 审计文档 + post-edit-bug-checker 机制 | ✅ |
| 理解日志产生的逻辑 | §1.2 还原 mirror_battle→judge→AIVAT 完整链 | ✅ |
| 定位根本原因（非表层） | §2 三连根因，每条区分"表层 vs 真根因" | ✅ |
| 注意日志时间版本 vs 当前代码 | §3 时间线表 + 逐文件"针对版本→当前状态"映射 | ✅ |
| 形成总结报告 | 本文件 `docs/log-rootcause-analysis-2026-06-17.md` | ✅ |

**复核动作（非仅文档断言）：**
- 数值复算边池偏置（OLD −2951 vs CURRENT 0）— `/tmp/verify_synthetic_real.py`
- 逐行比对 `judge.py:415-498, 562-586` 确证 temp_result/final_result/hand++ 三机制
- `git show d2fad78` 重建旧代码做 OLD-vs-CURRENT diff
- 跑 `web/tests/test_logic_aivat.py` → 24 passed
- grep 确证 `aivat_enabled` 生产路径无人传 True（latent）

---

## 6. 给后续工作的建议（按优先级）

1. **P0 — 接入前必须先做的 gate**：在把 AIVAT wire-in 到 `tool_gates` / precommit 数值门前，确认 `phase0-schema-change-test-staleness` 里 `get_global_stats` 的 `total_hands` undercount（`= last_opponent.total_hands` 应为 `+=`）已修，否则评分基数就错。
2. **P0 — workflow P0#1（paired net_chips）**：给 `BattleResult` 加 `net_chips` 字段并穿透 scheduler，否则 AIVAT 即便修好也无消费方。
3. **P1 — `phase2-sprt-truncation` 接入**：`run_decision_tests_sprt` 目前只被自己测试套件跑，未进 tool_gates；接入时记得删除 `UNDECIDED`/`rate_fallback` 死代码。
4. **P1 — `attempt-top-reset-wipes-sequential-sibling`**：sequential-overlap 模式下 attempt-top reset 会删前序 worker 编辑；改用 per-task input snapshot（`worker_snapshots`）或 gate 到 `parallel_mode or single_task`。
5. **P2 — `rc2b-classify-target-change-empty-dst-bug`**：empty-new-file 被误判 unchanged，重排 guard 顺序即可（1 测试）。
6. **观察 — `engine-judge-reraise-architectural-bug`**：judge.py 单一 `last_raise_to` 同时管首加注/再加注，任何 re-raise 比较符改动都会同时影响盲注路径；改时必须豁免盲注或分离规则（与 `sever/validator.py` 的规则 6/7/9 vs 8 对齐）。

---

*报告生成于 2026-06-17。所有数值结论均由本会话内的源码核对 + 数值复算支撑，非仅转述记忆文件。*
