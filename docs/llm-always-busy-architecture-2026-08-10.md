# LLM 持续消费架构 — 生产者/消费者分离设计

> 2026-08-10. 目标：让 LLM 信号量始终有工作可消费，将所有等待型工作
> （native match、eval_wait、文件 I/O）抽象为独立消费者，LLM 作为持续
> 供给的生产者。这是 option B（流水线深并行）的架构参考。

## 1. 当前架构（修改前）

```
单条串行流水线（Slice-2b OFF）或弱并行（Slice-2b ON, max_ahead=1）：

PRIMARY LANE ────────────────────────────────────────────────────────▶
  prepare → direction_audit → master(LLM) → workers(LLM) → [SEAL]
                                                                │
CONSUMER LANE ◀────────────────────────────────────────────────┘
  quality(native) → review(LLM) → critic(LLM) → precommit(native 30-90min) → commit

DRAFT LANE（best-effort, parks at workers_done）:
  prepare(N+1) → audit(N+1) → master(N+1,LLM) → workers(N+1,LLM) → [PARK & WAIT]
```

### LLM 空闲窗口（三个结构性缺陷）

| 缺陷 | 窗口 | 原因 | 持续时间 |
|------|------|------|----------|
| 1 | draft parks at workers_done | draft 跑完 prepare→workers 后停下等 consumer promote gen N | 30-90 min（整个 precommit 期间） |
| 2 | consumer 串行 gate chain | review/critic(LLM) 完成后进 precommit(native)，该线 LLM 空闲 | 30-90 min |
| 3 | producer launch 45s 轮询 | `_parked_sleep(45s)` 轮询而非事件驱动 | 累积 ~45s/gap |

### 已确认的可复用基础设施

全部已存在，option B = 拓宽接缝，非重写：

- **多 slot 并行**：`draft1/draft2/...`（`evolution_infra.py:60-82`）
- **ContextVar slot 隔离**：`active_slot_override(slot_id)`（`evolution_infra.py:85`）
- **SQLite 原子版本分配**：`reserve_draft_version`（`producer_consumer_slice2b.py:860`）
- **Consumer gate chain as async task**：`_run_gate_chain`（`activation.py:407`）
- **Stage→handler 分派表**：`_deterministic_route_handler_and_args`（`stage_routing.py:295`）
- **Off-event-loop native 执行**：`run_async_off_event_loop`（`blocking_runtime.py:79`）

### 关键约束

单个 Claude SDK session **不能**并行 tool call（`ClaudeAgentOptions` 无
`parallel_tool_use`，SDK MCP bridge 串行）。并行的单位是 **(stage × slot)**，
每个 LLM stage 开自己的 `claude_query` session。

## 2. 已部署的修复（阶段 0/0.5/0.6）

| Commit | 修复 | 效果 |
|--------|------|------|
| `3d5f7a0a` | Master abandon CAS race（信号层） | 消除 v161/v106 livelock：tool 层发信号，loop 在 quiescent checkpoint 上执行 abandon |
| `1f541774` | eval_wait draft 容忍 stale readiness | LLM 在 rating 重建期不再空转：draft 可用过期 snapshot 启动 master/workers |
| `0f6975f4` | draft preimage 直接清理 | draft slot 清理自己的 stale 目录（不能用 primary-scoped abandon） |
| `84ccedaa` | 前端：草稿视图/英文泄漏/轮询频率 | draft-only 情况不再显示"系统已停止"；角色中文标签；轮询降频 |

## 3. 目标架构（option B 终态）

```
SLOT MANAGER（持续调度，事件驱动）
├── Slot 0 (primary):  gen N:  ...→verified→commit_bot→[publish]→ cleanup
├── Slot 1 (draft1):   gen N+1: prepare→audit→master→workers→quality→review→critic→precommit→[verified]
├── Slot 2 (draft2):   gen N+2: prepare→audit→master→workers→quality→review→critic→precommit→[verified]
└── Filler (shadow):   gen N+3: prepare→audit→master→workers→[seal when slot frees]

LLM 信号量 (8 permits):
  时刻都有 master/workers/review/critic/filler 竞争 permits，零空闲。

Native match pool (background worker threads):
  precommit(N) + precommit(N+1) 并行，不阻塞 LLM。

Publication (serial, behind barrier):
  gen N commit → promote draft1 → gen N+1 commit → promote draft2 → ...
```

## 4. 阶段 1/2 改动点（设计完成，待实施）

### 阶段 1：Draft 驱动完整 gate chain

**消除缺陷 1**（draft parks at workers_done）：

- `_run_draft_cycle`（`orchestrator_loop_phases.py:~2008`）：到达 workers_done 时
  **不 return**，而是触发该 slot 自己的 seal + consumer gate chain。
- `_slice2b_seal_at_workers_done`（`det_route.py:858`）：加 `source_slot_id` 参数，
  从 draft slot 读 checkpoint 而非 ambient primary。
- `_promote_draft_to_primary`（`det_route.py:1179`）：
  - line 1208 `if draft.get("stage") != "workers_done"` → 接受 `verified`/`critic_checked`
  - line 1236 `"stage": "workers_done"` → collapse consumer slot 的 gate_results 到 primary
- `_maybe_promote_draft_to_primary`（`generation_scheduler.py:71`）：同样的两处改动
- `_reconcile_orphan_draft_at_boot`（`loop_phases.py:~1612`）：不 reap 有 consumer 的 draft
- `POK_SLICE2B_MAX_AHEAD` 1→2

**效果**：consumer 的 precommit(30-90min) 窗口由 draft 的 review/critic(LLM) 填充。
利用率 ~40% → ~65-75%。

### 阶段 2：事件驱动 launch + LLM 饥饿填充

**消除缺陷 2 和 3**：

- `_parked_sleep(45s)` → `await asyncio.wait_for(slot_freed_event.wait(), timeout=45)`
- 新增 `llm_semaphore_has_capacity()` 谓词：信号量空闲时启动 filler draft
- 每 cycle 记录利用率 timeline

**效果**：利用率 → ~80-90%。

## 5. 不做的事

- 不重写 orchestrator 主循环（stream observer 结构不变）
- 不改 SDK 并行模型（确认不能单 session 并行）
- 不改 checkpoint CAS / publication authority（commit_bot 保持单点串行）
- 不改 native match 执行模型（仍 off-event-loop worker thread）
