# 下一轮计划：Phase 2.5 审计修复 + Phase 3 策略引擎

> **基于**: Phase 1+2 对抗性审计报告（6 critical, 7 high, 14 medium）
> **日期**: 2026-06-10
> **预估**: Phase 2.5 ~6-8h | Phase 3 ~24-40h

---

## Phase 2.5: 审计发现修复（P0，~6-8h）

> 修复审计发现的全部 6 个 critical + 7 个 high 问题。
> Battle Scheduler 集成是 Phase 2 的核心产出，但存在 3 个运行时必崩 bug，从未实际运行过。

### 2.5.1 elo_daemon Scheduler 集成修复（Critical C1+C2）

**文件**: `web/core/elo_daemon.py`

| 修复 | 行号 | 描述 |
|------|------|------|
| write_result 签名 | 598-614, 792-799 | 改为构造 `BattleResult` 对象传入 |
| 外部任务格式转换 | 546-584 | drain 返回的 dict 转 7-tuple `('external', job_id, a_name, b_name, a_path, b_path, n_pairs)` |
| tuple 检测一致性 | 636, 655 | 确保 in-flight 任务跟踪使用相同的 tuple 格式 |

**验证**: 添加 `test_elo_daemon_scheduler_integration.py` 测试用例，验证 drain→match→result 完整路径。

### 2.5.2 battle_scheduler 并发安全（Critical C3+C4, High H4+H5+H6）

**文件**: `web/core/battle_scheduler.py`

| 修复 | 描述 |
|------|------|
| drain 全程 LOCK_EX | `drain_pending_jobs` 从读到写用单一 LOCK_EX，消除 TOCTOU |
| truncate 无条件化 | 将 `_write_jsonl_atomic(BATTLE_JOBS_FILE, [])` 移到 `if valid:` 外 |
| submit 原子化 | `submit_jobs` 用 LOCK_EX 原子读+检查+追加 |
| collect LOCK_EX | `collect_results` 和 `cleanup_stale` 全程 LOCK_EX |
| drain 互斥锁 | 添加 `.drain_lock` 文件防止并发 drain |

### 2.5.3 generation_scheduler .get() 修复（High H1）

**文件**: `web/core/generation_scheduler.py:351`
- `ratings[b].get('r', 0)` → `ratings[b].r`

### 2.5.4 tool_eval 变量覆盖修复（High H2）

**文件**: `web/core/tool_eval.py`
- scheduler 部分回退路径前保存 `original_opponents = list(opponents)`
- result 字典使用 `original_opponents`

### 2.5.5 Worker 并行失败回滚（Critical C5）

**文件**: `web/core/agent_workers.py:298-301`
- `elif not result:` 分支添加 source→target 文件回滚
- 修正错误注释

### 2.5.6 daemon_management scheduler_capable flag（High H3）

**文件**: `web/core/daemon_management.py`
- 基于 battle_scheduler import 是否成功设置 flag

### 2.5.7 WebUI 并发保护（High H7）

**文件**: `web/core/agent_workers.py:92-93`
- 并行路径中 worker 跳过 `clear_io()` 和 `set_status()`
- 或给 WebUI 关键方法加 `asyncio.Lock`

### 2.5.8 _write_jsonl_atomic crash safety（Medium BS-006）

**文件**: `web/core/battle_scheduler.py`
- 改为 write-to-tmp + fsync + os.replace 模式（与 evolution_infra.py 一致）

### 完成标准
- [ ] 全部 6 critical 修复，有对应测试
- [ ] 全部 7 high 修复
- [ ] Battle Scheduler 端到端测试通过（daemon 实际处理 scheduler job）
- [ ] Worker 并行失败路径有单元测试验证回滚
- [ ] 463+ 现有测试全部通过
- [ ] 无新增 hang/timeout

---

## Phase 3: 策略引擎深度升级（P1，~24-40h）

> 基于 Phase 2.5 修复后流水线稳定运行的前提下推进。
> 目标：将 bot 策略从 LLM 启发式提升到接近 GTO 的竞赛级别。

### 前置条件

- Phase 2.5 全部修复完成
- 演化系统连续运行 ≥3 代次无 pipeline 崩溃
- Glicko 评级稳定（最新 bot RD < 80）

### 3.1 翻前范围表构建（XL，~8-12h）

**文件**: `bots/claude_v{N}/preflop.py` (新), `constants.py`

**数据来源决策**:
- **首选**: 公开 HU Nash ranges（push/fold charts + opening ranges）
- **备选**: 基于 PokerStove/pypokerengine equity 计算的启发式构建
- **最低**: 当前线性评分系统的参数调优版

**实现**:
1. 构建 169 × 2 位置矩阵（SB/BB）× 5 动作（open/call/3bet/fold/4bet）
2. 基于对手建模动态调整（TAG/LAG/passive）
3. 与现有 `strategy.py` 的 `_preflop_decision` 集成

**完成标准**:
- 翻前范围表覆盖 SB open/3bet/call + BB defend/3bet/4bet
- 至少 169 × 2 位置矩阵
- 决策测试通过率 ≥ 70%

### 3.2 对手建模加速（M，~3-4h）

**文件**: `bots/claude_v{N}/opponent.py`

- 降低先验权重 4-8 → 1-2
- 前 10 手 fast start 分类（LAG/TAG/passive/unknown）
- 对手画像缓存跨 session（如果规则允许）

### 3.3 几何下注尺度 + 超池（M，~4-5h）

**文件**: `bots/claude_v{N}/strategy.py` 或新 `sizing.py`

- 几何尺度（geometric sizing）: 三街等比增长
- 超池下注（overbet）: nuts 场景 + 极化范围
- Pot-commitment 感知: 避免微小 raise 被 all-in
- 与现有 `_bet_sizing` 集成

### 3.4 位置感知增强（M，~3-4h）

**文件**: `bots/claude_v{N}/strategy.py`, `postflop.py`

- SB donk-bet / check-raise 频率
- BB 漂浮跟注 / 延迟加注
- OOP vs IP 策略分离

### 3.5 蒙特卡洛方差缩减（M，~3-4h）

**文件**: `bots/claude_v{N}/simulation.py`

- 对偶变量（antithetic variates）采样
- 分层采样（按 hand strength bucket）
- 目标：1000 次缩减采样 vs 10000 次基准 MAE < 2%

### 3.6 河牌极化范围策略（M，~3-4h）

**文件**: `bots/claude_v{N}/strategy.py` (river 部分)

- GTO 频率参考（基于手牌强度分桶）
- Blocker 选择（卡住对手 value 组合）
- OBFUSCATION 模式（隐藏策略信息）

### 3.7 安全对手利用框架（XL，~8-12h，可分步）

**文件**: `bots/claude_v{N}/exploit.py` (新)

**分步实现**:
1. **Step 1（~3h）**: 基于对手统计的线性利用 — 对 fold 过多的对手增加 bluff，对 call 过多的对手增加 value
2. **Step 2（~3h）**: 在线偏离检测 — 检测对手策略偏离 Nash 均衡
3. **Step 3（~3h）**: 安全边界 — 限制最大偏离 Nash 的程度，防止被反制
4. **Step 4（~3h）**: 近似 Nash 均衡 — 基于频率的简单近似

### 完成标准
- [ ] 翻前范围表覆盖 ≥ 169 × 2 位置
- [ ] 对手模型 10 手内产生有用信号
- [ ] 下注尺度含几何 + 超池 + pot-commitment
- [ ] 蒙特卡洛 1000 次缩减 vs 10000 次基准 MAE < 2%
- [ ] 河牌策略含极化范围 + blocker + 频率控制
- [ ] 60s 决策超时内完成（< 5s/决策）
- [ ] 全部 463+ 现有测试通过

---

## 实施顺序

```
Phase 2.5（~6-8h）：审计修复 ← 立即开始
    ├── 2.5.1-2.5.3: elo_daemon + scheduler 集成修复（最关键）
    ├── 2.5.4-2.5.5: tool_eval + worker 回滚
    ├── 2.5.6-2.5.7: daemon flag + UI 并发
    └── 2.5.8: crash safety
    ↓
Phase 3（~24-40h）：策略引擎升级
    ├── 3.1: 翻前范围表（前置，后续依赖）
    ├── 3.2 + 3.4: 对手建模 + 位置感知（可与 3.1 并行）
    ├── 3.3 + 3.5: 下注尺度 + 蒙特卡洛（依赖 3.1）
    ├── 3.6: 河牌策略（依赖 3.1）
    └── 3.7: 对手利用框架（最后，依赖 3.1-3.6）
```

**总预估**: ~30-48h 编码

---

## 风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Scheduler 修复引入新 bug | 中 | 演化暂停 | 修复后立即端到端测试 |
| Worker 并行回滚仍有边缘情况 | 低 | bot 脏文件 | 串行降级路径始终可用 |
| 翻前范围表数据源不足 | 中高 | 策略质量受限 | 启发式构建备选 |
| 策略过于复杂超时 | 中 | 60s 弃牌 | 每步加 timer + fallback |
| 3.7 Nash 近似不收敛 | 中高 | 利用效果差 | Step 1 线性利用足够对付弱对手 |

---

## 与原集成计划的差异

| 原计划任务 | 当前状态 | 变化 |
|-----------|---------|------|
| Phase 1 (1.1-1.13) | ✅ 全部完成 | 无变化 |
| Phase 2.1 看门狗 | ✅ 已实现 | 新增 watchdog_coroutine + WATCHDOG_TIMEOUT |
| Phase 2.2 Worker 并行 | ⚠️ 已实现但需修复 C5 | 增加 2.5.5 回滚修复 |
| Phase 2.3 Battle Scheduler | 🔴 已实现但 3 critical bugs | 增加 2.5.1-2.5.3 修复 |
| Phase 2.4 冒烟测试优化 | ✅ 已完成 | 无变化 |
| Phase 2.5 死代码清理 | ⬜ 未做 | 合并到 2.5.8 |
| Phase 3 (3.1-3.7) | ⬜ 未开始 | 不变 |
