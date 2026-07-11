# 根因分析与修复方案

**日期**: 2026-06-12 | **方法**: 6-agent 并行调查 + 2 辅助 agent 交叉验证

---

## 一、根因依赖图

```
Level 0 (系统性约束)
  **H5: LLM 能力天花板** — LLM 无法发明真正新颖的扑克策略
  所有 bot 收敛到 ~50% H2H 胜率，这是所有其他问题的根本约束

Level 1 (设计缺陷 — 放大了天花板效应)
  **H2: Critic 常量调优死锁** — 管线要求 H2H 证据，但接近盈亏平衡的数据无信号
    ├─ Master 提示词无"平台期协议"逃生通道
    ├─ H2H 数据显示所有对局 ~50% WR（来自 H5）
    └─ EXHAUSTED 标签只匹配 `[POSSIBLY EXHAUSTED]`，遗漏 `[EXHAUSTED]`

  **H6: Source 版本振荡** — 系统在 2-3 个高分 bot 间振荡
    ├─ _detect_source_loop 只检测 n=3 连续相同 source
    ├─ 无振荡检测（跨小集合循环）
    └─ recommended_source 被 ~50% WR 噪声驱动

Level 2 (实现 Bug — 造成直接浪费)
  **C2: 超时扩展销毁 Session** — Stage-aware timeout 授予延期但清除 session
    └─ 直接原因: orchestrator.py:247 调用 _clear_orchestrator_session()

  **C1: Orchestrator 重复调用工具** — 无防护阻止 LLM 重复调用同一工具
    ├─ run_precommit_eval 无幂等性守卫（其他工具有）
    ├─ max_turns=None（无限制）
    ├─ 工具返回值无 "NEXT_STEP" 提示
    └─ 被 C2 放大：新 session 无历史，LLM 重新调用工具

  **H7: Calibration 数据全为零** — commit_bot 在 archivist 读取前清除 checkpoint
    └─ 直接原因: tool_commit.py:191 清除在 :380 读取之前

  **H1: Worker CoT 不一致 (86%)** — Workers 叙述意图而非结果
    ├─ 无结构化输出强制
    └─ audit_focus_areas 在 tool_planning.py:645 收集但在 :726 结果中丢弃

Level 3 (次要 / 误报)
  **C3**: TOCTOU — 竞态窗口极窄（仅 API reset 端点触发），LOW
  **C4**: 版本号命名不一致 — 纯语义问题，无数据损坏，LOW
  **H3**: Fuzzy matcher 从未阻止任何 plan（0 次）— 实际是 UNDER-triggering，FALSE ALARM
  **H4**: Fix injection 92% 跳过 — 正常行为，修复通过 copytree 传播，EXPECTED

Level 4 (反馈循环)
  C2 → C1 → timeout 浪费 → C2（复合循环）:
    Session 销毁 → 新 LLM → 重复调用 → 再次超时
```

---

## 二、问题分类总结

| 问题 | 根因/症状? | 关键原因 | 被什么影响 | 与什么复合 |
|------|-----------|---------|-----------|-----------|
| H5 平台期 | **根因** | LLM 能力限制 | — | H2, H6 |
| H2 Critic 死锁 | **根因** | 无平台期逃生机制 | H5 | H2（自强化） |
| H6 Source 振荡 | 症状 | 弱循环检测 | H5 | H2 |
| C2 Session 销毁 | **根因** | 一行 bug | — | C1 |
| C1 重复调用 | 症状+根因 | 无幂等守卫、max_turns=None | C2 放大 | C2 |
| H7 Calibration 零 | **根因** | 清除-先于-读取顺序 | — | H2 |
| H1 CoT 不一致 | 症状 | 审计输出被丢弃 | — | 独立 |
| C3 TOCTOU | 次要 | unlink 在锁外 | — | 独立 |
| C4 版本不匹配 | 次要 | 命名不一致 | — | 独立 |
| H3 Fuzzy Matcher | **误报** | 从未实际阻止 | — | — |
| H4 Fix Injection | **预期行为** | 正常传播 | — | — |

---

## 三、修复集群

### Cluster A: Session 保留 + 工具重复守卫 (C1 + C2)  ⭐ 最高优先级

**根因**: C2 (line 247 不应清除 session) + C1 (run_precommit_eval 无幂等守卫 + max_turns 无限制)

**修改文件**:
| 文件 | 行号 | 改动 |
|------|------|------|
| `orchestrator.py` | 247 | **删除** `_clear_orchestrator_session()` 调用 |
| `orchestrator.py` | 651 | `max_turns=None` → `max_turns=30` |
| `tool_eval.py` | 88-100 | 添加幂等守卫：若 checkpoint stage ≥ "verified"，返回缓存结果 |
| `tool_gates.py` | 各幂等返回 | 添加 `"directive": "ALREADY COMPLETED. Call the NEXT tool."` |

**C1 幂等守卫示例** (添加到 run_precommit_eval):
```python
# Idempotency guard: skip if precommit already passed
_ckpt = _matching_checkpoint(v, source_v)
if _ckpt and _ckpt.get("stage") in ("verified", "archived"):
    precommit_gate = _ckpt.get("gate_results", {}).get("precommit_eval", {})
    if precommit_gate.get("passed") is True:
        return _json_tool_result({
            **precommit_gate,
            "idempotent_cache": True,
            "directive": "Precommit already passed. Call commit_bot next.",
        })
```

**工作量**: 1-2h | **风险**: LOW

---

### Cluster B: 平台期逃生协议 (H2 + H6)  ⭐ 战略核心

**根因**: H2H 数据 ~50% WR 无信号时，Master 默认常量调优，Critic 拒绝，26% 拒绝率

**修改文件**:
| 文件 | 改动 |
|------|------|
| `prompts/master_prompt.md` | 添加 `<plateau_protocol>` section |
| `prompts/critic_prompt.md` | 平台期允许无 H2H 支撑的结构性探索 |
| `generation_scheduler.py` | 增强 `_detect_source_loop` 振荡检测 |
| `agent_workers.py:72` | 匹配 `[EXHAUSTED]` 而不仅是 `[POSSIBLY EXHAUSTED]` |

**Master 平台期协议示例**:
```markdown
<plateau_protocol>
When ALL H2H matchups fall within 45-55% win rate (no exploitable weakness):
1. Acceptable: structural exploration (new decision system, opponent-aware logic)
2. Acceptable: crossover with a structurally different bot
3. Acceptable: aggressive exploration of extreme parameters (2x or 0.5x)
4. FORBIDDEN: small constant adjustments (±5-10%) — this is the EXHAUSTED pattern
</plateau_protocol>
```

**Critic 平台期规则**:
```markdown
At plateaus (all matchups 45-55% WR), structural exploration without specific
H2H backing may score 6-7 if genuinely novel. Constant tuning at plateaus
scores max 5 regardless of elegance.
```

**Source 振荡检测**:
```python
# In _detect_source_loop: check for oscillation across last 8 sources
recent_sources = [s for s in last_n_sources[-8:]]
unique = set(recent_sources)
if len(unique) <= 3 and len(recent_sources) >= 6:
    # Oscillation detected — force a different source
```

**工作量**: 3-4h | **风险**: MEDIUM（提示词变化影响不可预测）

---

### Cluster C: Calibration 数据 + CoT 审计接线 (H7 + H1)

**根因**: H7: commit 清除 checkpoint 先于 archivist 读取。H1: audit_focus_areas 被收集后丢弃。

**修改文件**:
| 文件 | 行号 | 改动 |
|------|------|------|
| `tool_commit.py` | 191 | 将 calibration 记录移到 `clear_pipeline_checkpoint()` 之前 |
| `tool_commit.py` | 378-401 | 删除 archivist 中已死代码的 calibration 块 |
| `tool_planning.py` | 726 | 将 `audit_focus_areas` 加入 execute_workers 返回值 |
| `tool_gates.py` | run_review | 注入 `audit_focus_areas` 到 reviewer 上下文 |

**工作量**: 2-3h | **风险**: LOW

---

### Cluster D: 快速修复 (<30 分钟)

| # | 修复 | 文件:行 | 改动 |
|---|------|---------|------|
| Q1 | C2: 超时不清除 session | `orchestrator.py:247` | 删除 `_clear_orchestrator_session()` |
| Q2 | C1: 限制 max_turns | `orchestrator.py:651` | `None` → `30` |
| Q3 | H4: 修复误导性 severity | `fix_injection.py:186` | `skipped and not applied` → `skipped and applied` |
| Q4 | C3: TOCTOU 修复 | `evolution_infra.py:328` | 将 unlink 移入 locked_file 块内 |
| Q5 | H2: EXHAUSTED 标签匹配 | `agent_workers.py:72` | `"[POSSIBLY EXHAUSTED]"` → `"[EXHAUSTED]" in line` |
| Q6 | C4: 版本号修正 | `generation_scheduler.py:237` | `current_v=active_v` → 保持一致 |
| Q7 | H7: Calibration 顺序 | `tool_commit.py:191` | calibration 记录移到 clear 前 |

---

## 四、实施优先级

| 顺序 | 集群 | 影响 | 风险 | 工作量 | 依赖 |
|------|------|------|------|--------|------|
| 1 | **A: Session + 工具重复守卫** | HIGH — 消除 C1+C2 复合循环 | LOW | 1-2h | 无 |
| 2 | **D: 快速修复** | LOW — 7 个小修 | LOW | 30min | 无（可与 A 并行） |
| 3 | **C: Calibration + CoT** | MEDIUM — 启用反馈循环 | LOW | 2-3h | 无 |
| 4 | **B: 平台期逃生** | HIGH — 解决战略根因 | MEDIUM | 3-4h | 无（可与 A+C 并行） |

**总工作量**: 7-10h | **预期效果**: 
- 消除 ~15% 的 LLM 浪费（$100-150/运行周期）
- 减少 26% Critic 拒绝率
- 打破平台期，恢复进化动量
