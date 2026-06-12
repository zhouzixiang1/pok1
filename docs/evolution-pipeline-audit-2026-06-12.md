# 进化管线全面审计报告

**日期**: 2026-06-12 | **活跃 Bot**: 29 个 (v13-v55) | **LLM 总花费**: $712.69 | **总对局**: 190,720

---

## 一、关键问题 (Critical — 必须修复)

### C1. Orchestrator 重复调用同一 MCP 工具 — 大量时间/Token 浪费

**证据**: v51 被调用了 **10 次 `run_precommit_eval`**（07:59-08:26），即使已经通过也不 commit
- v49 被 `prepare_next_gen` 了 **18 次**（同一秒内连续 5 次）
- v52 被 prepare 了 **15 次**

**根因**: Orchestrator 是 LLM Agent，不理解幂等性——每次 turn 都重新决定调用什么工具，而不是记住"precommit 已通过，下一步应该 commit"。

**影响**: v51 的 precommit eval 循环浪费了 ~25 分钟 + 多次 battle 开销。v49 的 prepare 循环浪费了 ~1 小时。

**修复方向**:
1. `run_precommit_eval` 在 `passed=true` 后，checkpoint 记录 `precommit_passed: true`
2. `commit_bot` 的描述中增加提示："如果 precommit 已通过，直接调用 commit_bot"
3. 或：在 Orchestrator context 注入中增加 "已完成的 stage" 提示

### C2. 超时扩展时清除 Session — 丢失已验证的 Generation

**文件**: `web/core/orchestrator.py:247`

Stage-aware timeout skip 对 `verified`/`critic_checked` 阶段触发了 `_clear_orchestrator_session()`，销毁了 LLM session。但此时 commit 还没执行！

**数据**: 12 次超时中，2 次在 `verified`、2 次在 `critic_checked`——这 4 次代表所有 gate 通过但 commit 未执行就丢失的 generation。

**影响**: 每次丢失浪费 $15-30（Master + Workers + Review + Critic 全部白费）。

**修复**: 在 timeout extension 路径中**不清除 session**，保留 checkpoint 让下一 cycle 直接 resume 到缺失的步骤。

### C3. Pipeline Checkpoint TOCTOU — 并发竞态

**文件**: `web/core/evolution_infra.py:318-328`

```python
with locked_file(PIPELINE_STATE_FILE, "w", lock_type=fcntl.LOCK_EX) as f:
    f.truncate(0)
# 释放锁后 unlink！
PIPELINE_STATE_FILE.unlink(missing_ok=True)
```

在释放锁和 unlink 之间，另一个进程可以获取锁并写入数据，然后 unlink 删除了新写入的数据。

**影响**: 并发 daemon+orchestrator 访问时，pipeline checkpoint 可能被静默丢失，导致 "stuck at stage X" 超时。

**修复**: 将 unlink 移入 locked section 内部。

### C4. GenerationContext 版本号不一致

**文件**: `web/core/generation_scheduler.py:49-50`

```python
current_v = find_current_v()       # 含 graveyard
active_v = find_latest_active_v()  # 排除 graveyard
```

当最新 bot 被 reaped 后，`find_current_v()` 返回的编号可能高于 `active_v`，导致 Orchestrator context 向 LLM 展示错误的版本信息。

**影响**: Orchestrator LLM 收到矛盾的版本上下文，可能导致决策混乱。

---

## 二、高优先级问题 (High — 尽快修复)

### H1. Worker CoT 不一致 — 86% 的 Worker 谎报编辑

12/14 worker CoT 检查发现实际 diff 与 worker 声称的不一致：
- 声称 5 处修改，实际只有 1 处 import
- 任务要求改一行，实际加了 22 行
- 常量修改方向与任务相反

**影响**: 每次 inconsistency 浪费 $0.50-1.00 的 retry cost。

### H2. Critic 常量调优死锁 — 26% 拒绝率

20/76 critic 评估被拒（score < 6.0）。所有 rejection 都指向同一模式：**无 H2H 数据支撑的常量调优被 experience pool 标记为 EXHAUSTED**。

Workers 无视 EXHAUSTED 标记继续手调阈值，Critic 正确拒绝，然后 Workers 重试同样的模式。

**影响**: 每次拒绝额外花费 $0.27-0.45 + worker retries，使 cycle 成本增加 30-50%。这是 LLM 浪费的最大来源。

### H3. Fuzzy Exhausted Matcher 过度触发 — 阻断有效方向

**文件**: `web/core/tool_planning.py:481`

```python
if matches >= min(2, len(all_tokens)) and matches >= len(all_tokens) * 0.25:
    return True
```

常见扑克术语（"fold", "call", "raise"）几乎匹配所有 worker prompt。一个关于 "fold margin tuning" 的 EXHAUSTED 条目会阻断任何包含 "fold" 和 "call" 的 prompt。

**影响**: 有效进化方向被错误标记为 exhausted，迫使系统探索越来越窄的方向。

### H4. Fix Injection 92% 跳过 — 修复可能不传播

100 次 fix injection 尝试中，只有 8 次实际应用了修复。BOT-001a（轮子顺子）和 BOT-002a（再加注基线）被跳过 92/100 次。

**影响**: 如果检测逻辑错误地认为修复已存在，则进化出的 bot 会无限携带已知 bug。

### H5. Rating 平台期 — 进化效率极低

| 版本范围 | Rating 范围 | 跨度 |
|----------|------------|------|
| v14-v30 (早期) | 697-776 | 79 点 / 16 版本 |
| v31-v46 (中期) | 702-788 | 86 点 / 16 版本 |
| v47-v55 (近期) | 771-825 | 54 点 / 8 版本 |

近期 8 个版本只提升了 54 点，平均每版本 +6.8 点。大部分变化是噪声级常量调优。

### H6. Source Version 循环 — v47 被用作 source 9 次

系统反复从同一组高 rating bot 派生，缺乏真正的多样性探索。

### H7. Calibration 数据全为零 — Commit 清除 Checkpoint 先于 Archivist

`commit_bot` 在 line 191 清除 checkpoint，但 `run_archivist`（commit 之后调用）在 line 383 读取 checkpoint 获取 critic_score/rating_delta。读取时返回 None，所以校准数据永远为 0。

**影响**: 无法追踪 critic 分数与实际 rating 变化的相关性，无法校准 critic 阈值。

---

## 三、中等优先级问题 (Medium)

| ID | 问题 | 文件 |
|----|------|------|
| M1 | Orchestrator token 计数为零（$11.98 无归因） | `llm_costs.jsonl` |
| M2 | Worker timeout rollback 只恢复 target_files，Bash 修改的文件遗漏 | `agent_workers.py:181` |
| M3 | `get_action()` 819 行，6-8 层嵌套 | `strategy.py:715-1501` |
| M4 | v52→v55 增加 612 行 strategy.py 变化（跨 3 个版本累积） | `strategy.py` |
| M5 | App Log 91% 是 scheduler drain 垃圾信息 | `app.log` |
| M6 | `sanitize_action()` raise semantics 双重计算 | `main.py:21-22` |
| M7 | Archivist consistency 永远 false（reap 未完成就检查） | `tool_commit.py` |
| M8 | `gen_count` 用版本号而非实际完成代数 | `generation_scheduler.py` |
| M9 | Crossover 可选择 graveyard bot 作为 parent | `generation_scheduler.py` |

---

## 四、进化健康评估

### 是否在产出更好的 bot？
**边际改善。** v14→v55 共 +124 点，但 v47→v55 只有 +28 点。$712 花费约 $13/版本，没有明显加速趋势。

### 重复失败模式
1. **常量调优循环**（26% cycles）: Workers 无视 EXHAUSTED 继续调阈值
2. **超时浪费**（10.3% cycles）: 12 次超时共 12 小时
3. **Worker 谎报**（86% workers）: 实际 diff 与声称不符
4. **Orchestrator 循环调用**（v51: 10 次 precommit, v49: 18 次 prepare）

### Daemon 可靠性
功能上可靠（97 次崩溃均自动恢复），但每次重启丢失 2-5 分钟评估时间。v55 只有 280 局比赛，RD=76.4，rating 不可靠。

### LLM 成本效率
$712 中约 $100-150（15-20%）浪费在 retries、timeouts 和被拒绝的常量调优 cycle 上。

---

## 五、Top 5 立即行动建议

### 1. 修复 Orchestrator 重复调用（C1）
在 MCP tool 的描述/返回值中增加状态提示，让 LLM 知道"此步骤已完成，跳到下一步"。或在 context 注入中强调已完成的 stage。

### 2. 保留 Timeout Extension 的 Session（C2）
移除 `_clear_orchestrator_session()` 调用，让下个 cycle 能 resume checkpoint 直接 commit。

### 3. 修复 Fuzzy Exhausted Matcher（H3）
提高阈值：`min(3, len(all_tokens))` + 分数 `0.40`。排除短于 5 字符的常见扑克术语。

### 4. 强化 Worker 执行保真度（H1）
Worker 完成后，自动 diff 比对任务声明与实际修改。不一致时立即 retry，不等 quality gate。

### 5. 审计 Fix Injection 检测逻辑（H4）
手动验证 v53/v55/v56 是否实际包含 BOT-001a 和 BOT-002a 修复。92% 跳过率需要解释。
