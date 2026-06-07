# `find_current_v()` 深度分析报告

**分析日期**: 2026-06-07
**方法**: 代码审查 + 调用链追踪 + 边界条件分析

---

## 一、函数概览

```python
# evolution_infra.py:321-357
def find_current_v():
    """Find the latest completed bot version.
    Cascading sources: git tags > .completed sentinel files > directory names.
    """
    versions = set()
    # Source 1: git tags (most authoritative) → versions.add(...)
    # Source 2: .completed sentinel files   → versions.add(...)
    # Source 3: any claude_v* directory      → versions.add(...)
    return max(versions) if versions else 0
```

**设计意图**: 返回「最新已完成的 bot 版本号」，用于计算 `next_v = current_v + 1`。

**三级级联发现**:

| 优先级 | 来源 | 权威性 | 场景 |
|--------|------|--------|------|
| 1 | `git tag -l "bot-v*"` | 最高 | 正常运行（commit_bot 创建 tag） |
| 2 | `bots/claude_v*/.completed` 存在 | 中等 | tag 还没创建（commit 流程中断） |
| 3 | `bots/claude_v*/` 目录存在 | 最低 | 初始化/无 tag 无 sentinel |
| 兜底 | 返回 `0` | — | 全新系统 |

---

## 二、调用点全景（12 个调用 + 1 个死代码）

| # | 文件:行 | 调用者 | 用途 | 语义正确？ |
|---|---------|--------|------|-----------|
| 1 | `app.py:34` | `app_state.bootstrap()` | 初始化 AppState | ✅ |
| 2 | `generation_scheduler.py:46` | `prepare_generation()` | 确定 eval 等待目标 | ✅ |
| 3 | `orchestrator.py:395` | recovery fallback | checkpoint 缺 source_v 时的后备 | ⚠️ |
| 4 | `orchestrator_context.py:134` | `build_orchestrator_context()` | 非 gen_ctx 路径构建上下文 | ✅ |
| 5 | `orchestrator_context.py:265` | `_make_precompact_hook()` | 压缩后状态恢复 | ✅ |
| 6 | `tool_status.py:64` | `get_status()` | 状态报告 | ✅ |
| 7 | `tool_status.py:380` | `diagnose_environment()` | 诊断快照 | ✅ |
| 8 | `tool_gates.py:184` | `prepare_next_gen()` | next_v 合理性检查 | ✅ |
| 9 | `tool_bot_management.py:39` | `_do_reap_weakest()` | 保护当前 bot 不被淘汰 | ⚠️ |
| 10 | `tool_bot_management.py:174` | `abandon_generation()` | 清理未完成目录 | ✅ |
| 11 | `evolution_infra.py:689` | `archive_old_logs()` | 计算归档截断版本 | ✅ |
| 12 | `agent_workers.py:14` | **死代码** | 导入但从未调用 | 🗑️ |

---

## 三、架构定位：`current_v` vs `source_v` 的职责分离

系统中有两个不同的「当前版本」概念：

| 概念 | 来源 | 用途 | 可否不同 |
|------|------|------|---------|
| `current_v` | `find_current_v()` | 版本编号（`next_v = current_v + 1`）、状态报告 | — |
| `source_v` | `_decide_strategy()` → Combined Analyst LLM | 实际进化来源（复制哪个 bot 目录） | ✅ 可以不同 |

**数据流**:
```
find_current_v()  →  current_v  →  next_v = current_v + 1  (版本编号)
                                    ↓
_decide_strategy(combined)  →  source_v  (进化来源)
                                    ↓
prepare_next_gen(source_v, next_v)  →  复制 source → next
```

**两者的分离是正确的** — `find_current_v()` 用于「下一个版本号应该是多少」，`source_v` 用于「从哪个版本进化」。Crossover、branch、LLM 推荐源都可能让 `source_v ≠ current_v`。

**但存在两个语义混淆风险点**（见第五节）。

---

## 四、发现的边界问题

### 🔴 问题 1：graveyard bot 膨胀版本计数器

**场景**: v10 被淘汰到 `bots/graveyard/`，但 git tag `bot-v10` 仍然存在。

**影响**: `find_current_v()` 从 git tags 找到 `bot-v10`，返回 10。`next_v = 11`。但 v10 已不在活跃池中，其 rating 可能已过时。

**实际代码路径**:
```python
# evolution_infra.py:329-334
tags = _git("tag -l bot-v*")  # 找到 bot-v10（graveyard 中）
# → versions = {1, 2, ..., 10}
# → max(versions) = 10

# generation_scheduler.py:46-49
current_v = find_current_v()  # = 10
bot_name = f"claude_v{current_v}"  # = "claude_v10"
# → wait_for_daemon_eval("claude_v10")  # v10 已被淘汰，不会有新对局
# → eval_ok = False → 返回 None → 本轮跳过
```

**后果**: 系统永远等待已淘汰 bot 的评估数据，**永远无法开始下一代**，直到 degraded_min 降级触发（连续 3 次超时后降到 30 局要求）。

**严重度**: 🔴 高 — 会导致进化停滞

**修复建议**: `find_current_v()` 应该只考虑**活跃 bot**（在 `bots/` 目录下，非 graveyard），或者至少 `prepare_generation()` 应该用 `source_v` 等待评估（因为 source 才是真正需要数据的 bot）。

---

### 🟡 问题 2：`_do_reap_weakest` 保护的是最新版本而非最强 bot

**场景**: v10 刚完成（rating 1500，很弱），v6 是最强 bot（rating 1800）。

**代码**:
```python
# tool_bot_management.py:39
current_bot = f"claude_v{find_current_v()}"  # = "claude_v10"
# v10 被保护，不会被 reap
# 但 v10 可能是最弱 bot，应该被保护的是「当前进化来源」
```

**影响**: 刚完成的弱 bot 被保护不被淘汰，但这恰好符合设计意图 — 新完成的 bot 需要积累足够对局才能评估。

**严重度**: 🟡 低 — 设计意图合理，但命名可能引起误解

---

### 🟡 问题 3：recovery 路径的 fallback 语义偏差

**代码**:
```python
# orchestrator.py:395
current_v=ckpt.get("source_v", find_current_v()),
```

**场景**: checkpoint 损坏缺少 `source_v`，fallback 到 `find_current_v()`。如果最新版本号是已淘汰的 v10，recovery 会用 v10 作为 source_v。

**严重度**: 🟡 低 — 仅在 checkpoint 损坏时触发，且有 degraded 机制兜底

---

### 🟢 问题 4：Source 3（目录名 fallback）可能包含未完成 bot

**场景**: v9 正在被 workers 编辑（stage = `workers_done`），目录存在但无 `.completed` 也无 tag。

**代码路径**: Source 1（无 tag）和 Source 2（无 `.completed`）都跳过，Source 3 会把 v9 纳入 `versions`。如果 v9 > 所有已完成版本，`find_current_v()` 返回 9。

**缓解**: `_cleanup_incomplete()` 在 `prepare_generation()` 开始时清理这种目录（除非有活跃 checkpoint）。所以只有在异常退出后未清理时才会触发。

**严重度**: 🟢 低 — 有 `_cleanup_incomplete` 保护

---

### 🟢 问题 5：`current_v + 1` 与已有目录冲突

**场景**: v9 上次中断（有 checkpoint），新循环 `find_current_v()` 返回 8，`next_v = 9`。但 v9 目录已经存在。

**缓解**: 多处保护 — `_cleanup_incomplete()` 清理、`prepare_next_gen()` 检测并拒绝覆盖已完成 bot、orchestrator_context 警告。

**严重度**: 🟢 低 — 多重保护机制

---

### 🗑️ 问题 6：`agent_workers.py` 死代码导入

**代码**: `agent_workers.py:14` 导入了 `find_current_v` 但从未使用。

**严重度**: 🗑️ 无影响 — 纯代码清洁度

---

## 五、语义风险点

### 风险点 A：「最新版本」vs「当前进化目标」的混淆

`find_current_v()` 返回的是「版本号最高的已完成 bot」，但在 crossover/branch 场景下，**实际进化来源** (`source_v`) 可能是完全不同的版本。

**关键**: 当前代码中 `find_current_v()` 的所有 12 个调用点语义都正确 — 没有任何地方错误地用它代替 `source_v`。`source_v` 通过 `GenerationContext` → pipeline checkpoint → `_resolve_version_args()` 正确传递。

**但有一个隐患**: `prepare_generation()` 用 `find_current_v()` 的返回值等待 eval：
```python
# generation_scheduler.py:49
bot_name = f"claude_v{current_v}"  # 用 current_v 等待 eval
# 但 source_v 可能 != current_v（crossover 场景）
# source_v 对应的 bot 才需要最新的评估数据
```
这不会导致错误（等待所有 bot 的 eval 是合理的），但如果 source_v 的数据不足，系统不会注意到。

### 风险点 B：版本号单调递增的假设

`next_v = current_v + 1` 假设版本号永远递增。如果手动删除了某些 tag 或 bot 目录，可能出现版本号跳跃，但不会破坏功能。

---

## 六、潜在改进建议

### 改进 1：区分「版本编号器」和「活跃最新 bot」

当前 `find_current_v()` 承担两个职责：
1. 确定下一个版本号（`next_v = current_v + 1`）
2. 找到「当前 bot」用于 eval 等待和保护

建议拆分：
```python
def find_next_version():
    """版本编号器 — 返回 next_v = max(所有已完成版本) + 1"""
    # 只看 git tags（最权威的完成标记）
    ...

def find_latest_active_bot():
    """活跃最新 bot — 用于 eval 等待和 reap 保护"""
    # 只看 bots/ 目录（排除 graveyard）
    ...
```

### 改进 2：graveyard tag 清理

被淘汰到 graveyard 的 bot 的 git tag 应该保留（用于 lineage 追溯），但 `find_current_v()` 应排除 graveyard bot。可以改为只看 Source 1（tags）中的版本是否在活跃目录中：

```python
# Source 1 改进：只计活跃 bot 的 tag
for tag in tags:
    v = int(tag.replace("bot-v", ""))
    if (BOTS_DIR / f"claude_v{v}").exists():  # 排除 graveyard
        versions.add(v)
```

### 改进 3：`prepare_generation()` eval 等待目标

应该同时等待 `source_v` 对应 bot 的评估数据（如果 source_v != current_v）：
```python
eval_bots = {current_v}
if source_v and source_v != current_v:
    eval_bots.add(source_v)
# 等待所有 eval_bots 的数据充足
```

### 改进 4：清理死代码

移除 `agent_workers.py:14` 的未使用导入。

---

## 七、总结

| 维度 | 评估 |
|------|------|
| **功能正确性** | ✅ 12 个调用点语义都正确，无 `current_v`/`source_v` 混淆 |
| **边界处理** | ⚠️ graveyard bot 膨胀版本号可能导致 eval 等待超时 |
| **健壮性** | ✅ `return 0` 兜底、多重保护机制、checkpoint 恢复 |
| **可维护性** | ⚠️ 函数承担两个职责（版本编号 + 活跃 bot 定位），拆分更好 |
| **代码清洁** | 🗑️ 1 个死代码导入 |

**整体评级**: 函数本身设计合理，三级级联发现提供了良好的鲁棒性。主要的架构风险不在函数本身，而在调用者对其返回值的**隐含假设**（版本号最高的已完成 bot = 需要等待 eval 的 bot）。graveyard 场景是唯一可能导致系统停滞的实际风险。
