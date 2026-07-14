# LLM 进化系统改进计划

## 概述

基于对 web/core/ 下所有 LLM Agent 的深度分析，识别出 20 个问题（5 严重 / 9 中等 / 6 低）。本计划聚焦于**最关键的 6 项改进**，按优先级排序，覆盖数据完整性、进化效率、并发安全三个维度。

***

## 改进 1：save\_ratings() 原子写入（数据完整性）

**问题**：`save_ratings()` 直接 `json.dump()` 写入 `glicko_ratings.json`，进程崩溃时可能损坏整个评分文件。

**文件**：`web/core/elo_daemon.py` 第 91-99 行

**当前代码**：

```python
def save_ratings(ratings, save_num=None):
    ...
    with locked_file(RATINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)
```

**修改方案**：改用 tmp + fsync + os.rename 原子写入模式（与 `write_pipeline_checkpoint()` 保持一致）：

```python
def save_ratings(ratings, save_num=None):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    data = {}
    for name, p in ratings.items():
        d = p.to_dict()
        d["last_period"] = datetime.now().isoformat(timespec="seconds")
        data[name] = d
    # Atomic write: tmp + fsync + rename
    tmp = RATINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(RATINGS_FILE))
    ...
```

**验证**：手动 kill -9 daemon 进程后检查 ratings 文件是否完好。

***

## 改进 2：Worker 零改动检测与强制验证（进化效率）

**问题**：Worker（尤其 Tuner）反复产出零改动，33 次重置记录浪费 $50+ LLM 费用。当前仅在 quality gates 阶段检测代码变更，Worker 内部不检测。

**文件**：`web/core/agent_workers.py` 第 49-136 行 (`_run_single_worker`)

**修改方案**：在 `_run_single_worker()` 中增加 Edit 后验证循环：

1. 在 LLM 查询完成后、编译检查之前，增加"代码变更快照比对"
2. 如果 Worker 声明了 `target_files` 但这些文件无变更，将错误信息注入 prompt 重试
3. 增加重试 prompt 的明确性："你的任务是修改 {target\_files}，但代码完全未变。你必须使用 Edit 工具修改文件。"

在 `_run_single_worker()` 的重试循环中，第 117 行（smoke test 之后、return True 之前）插入：

```python
# Verify target files were actually modified
target_rels = [_target_rel(f, next_v) for f in task.get("target_files", [])]
target_rels = [r for r in target_rels if r]
if target_rels and source_v is not None:
    src_dir = get_bot_dir(source_v)
    unchanged = []
    for rel in target_rels:
        src_file = src_dir / rel
        dst_file = next_dir / rel
        src_text = src_file.read_text() if src_file.exists() else ""
        dst_text = dst_file.read_text() if dst_file.exists() else ""
        if src_text == dst_text:
            unchanged.append(rel)
    if unchanged:
        _last_reason = f"zero changes in target files: {', '.join(unchanged)}"
        base_worker_prompt += (
            f"\n\nCRITICAL: Your target files were NOT modified: {', '.join(unchanged)}. "
            f"You MUST use the Edit tool to change these files. Do NOT just analyze — make actual edits."
        )
        continue
```

**额外修改**：在 `web/core/prompts/worker_prompt.md` 中增加强调：

```
## Mandatory Action
You MUST use the Edit tool to modify at least one of your target_files. 
Simply analyzing the code without making edits is a FAILURE.
After editing, verify your changes by reading the modified file.
```

**验证**：运行一代进化，检查 Worker 失败日志中零改动次数是否减少。

***

## 改进 3：辅助 Agent 并行化（进化效率）

**问题**：`prepare_generation()` 中三个独立 LLM 分析（Stagnation、Match、Performance）串行执行，每代额外等待 3-5 分钟。

**文件**：`web/core/generation_scheduler.py` 第 85-100 行

**当前代码**：

```python
stagnation = await _analyze_stagnation(current_v, active_bots, ratings, ui)
...
match_analysis = await _analyze_recent_matches(current_v, ui)
...
perf = await _run_performance_verification(current_v, ratings, ui)
```

**修改方案**：使用 `asyncio.gather()` 并行执行：

```python
# Three independent LLM analyses — run in parallel
stagnation_result, match_result, perf_result = await asyncio.gather(
    _analyze_stagnation(current_v, active_bots, ratings, ui),
    _analyze_recent_matches(current_v, ui),
    _run_performance_verification(current_v, ratings, ui),
    return_exceptions=True,
)

if shutdown_mgr and shutdown_mgr.is_shutting_down:
    return None

# Unpack results, treating exceptions as failures
stagnation = stagnation_result if not isinstance(stagnation_result, BaseException) else None
match_analysis = match_result if not isinstance(match_result, BaseException) else ""
perf = perf_result if not isinstance(perf_result, BaseException) else ""
```

**注意**：

* 三者共享 `ui` 对象的 `log_history`/`log_io`，需要确保这些方法线程安全（当前 `WebUI.log_io` 使用 `self._lock` 保护 ring buffer，`log_history` 也是线程安全的）

* shutdown 检查移到 gather 之后统一处理

**验证**：测量并行化前后 `prepare_generation()` 的耗时差异。

***

## 改进 4：llm\_query.py 限流重试代码去重 + 误判修复（可靠性）

**问题 A**：`run_claude_query()` 中首次查询和限流重试的流式处理逻辑完全重复（约 50 行）。

**问题 B**：`_is_rate_limited()` 基于字符串匹配，LLM 正常回复中含 "rate limit" 等词时误触发重试。

**文件**：`web/core/llm_query.py`

### 4A：抽取流式处理为内部函数

将第 113-142 行的流式处理逻辑抽取为 `_process_stream()` 内部函数：

```python
async def _process_stream(query_gen, log_file_path, ui, role_name):
    """Process a streaming LLM query, returning (texts, cost, usage)."""
    texts = []
    cost_usd = None
    usage = None
    async for message in query_gen:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
                    with open(log_file_path, "a") as lf:
                        lf.write(block.text + "\n")
                    ui.log_io(block.text, "claude", role_name)
                elif isinstance(block, ThinkingBlock):
                    thinking = block.thinking or "[thinking...]"
                    with open(log_file_path, "a") as lf:
                        lf.write(f"\n[THINKING] {thinking[:2000]}\n")
                    ui.log_io(thinking, "thinking", role_name)
                elif isinstance(block, ToolUseBlock):
                    args_str = json.dumps(block.input, ensure_ascii=False, indent=2)[:2000]
                    with open(log_file_path, "a") as lf:
                        lf.write(f"\n[TOOL_CALL] {block.name}\n[ARGS] {args_str}\n")
                    ui.log_io(f"\n[tool: {block.name}]", "tool", role_name)
                    ui.emit_tool_call(block.name, block.input, role_name)
                elif isinstance(block, ToolResultBlock):
                    content = block.content if isinstance(block.content, str) else (
                        json.dumps(block.content, ensure_ascii=False) if block.content is not None else ""
                    )
                    if content:
                        with open(log_file_path, "a") as lf:
                            lf.write(f"\n[TOOL_RESULT] {content[:3000]}\n")
                        ui.log_io(content[:3000], "tool_result", role_name)
        elif isinstance(message, ResultMessage):
            cost_usd = message.total_cost_usd
            usage = message.usage
    return texts, cost_usd, usage
```

然后 `run_claude_query()` 简化为：

```python
query_gen = claude_query(prompt=full_prompt, options=options)
texts, cost_usd, usage = await _process_stream(query_gen, log_file_path, ui, role_name)

# Rate limit retry
if _is_rate_limited("\n".join(texts)):
    for backoff in [30, 60, 120]:
        ...
        retry_gen = claude_query(prompt=full_prompt, options=options)
        retry_texts, retry_cost, retry_usage = await _process_stream(retry_gen, log_file_path, ui, role_name)
        ...
```

### 4B：限流检测改进

在 `_is_rate_limited()` 中增加上下文验证：

```python
def _is_rate_limited(output: str) -> bool:
    # Must be a short error message, not a long LLM response
    if len(output) > 2000:
        return False
    return (
        "overloaded" in output.lower()
        or "该模型当前访问量过大" in output
        or "rate limit" in output.lower()
        or re.search(r'(?:status["\s:=]+529|HTTP/\d\.?\d?\s+529|error.*529)', output, re.IGNORECASE) is not None
    )
```

**验证**：检查 `llm_costs.jsonl` 中是否有因误判导致的重复调用记录。

***

## 改进 5：inline eval 写入安全性（并发安全）

**问题**：`run_inline_eval()` 在 daemon 停止时直接覆盖 ratings/H2H/bot\_stats 文件，不使用原子写入，且不追加 rating\_history 快照。

**文件**：`web/core/tool_eval.py` 第 309-326 行

**修改方案**：

1. Ratings 写入改用原子 tmp+rename 模式
2. 追加 rating\_history 快照（与 daemon 的 `save_ratings()` 行为一致）

```python
# Save updated ratings (atomic)
from datetime import datetime as _dt
data = {}
for name, p in ratings.items():
    d = p.to_dict()
    d["last_period"] = _dt.now().isoformat(timespec="seconds")
    data[name] = d
tmp = RATINGS_FILE.with_suffix(".tmp")
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.flush()
    os.fsync(f.fileno())
os.replace(str(tmp), str(RATINGS_FILE))

# Append rating history snapshot (consistent with daemon)
history_file = RESULTS_DIR / "rating_history.jsonl"
snapshot = {
    "period": f"inline_v{v}",
    "timestamp": _dt.now().isoformat(timespec="seconds"),
    "ratings": {name: {"r": p.r, "rd": p.rd} for name, p in ratings.items()},
    "source": "inline_eval",
}
with locked_file(history_file, "a") as f:
    f.write(json.dumps(snapshot) + "\n")
```

**验证**：运行 inline eval 后检查 rating\_history.jsonl 是否包含新快照。

***

## 改进 6：write\_pipeline\_checkpoint() 增加 fsync（数据完整性）

**问题**：`write_pipeline_checkpoint()` 写入 tmp 文件后未 fsync 就 rename，极端情况下数据未落盘。

**文件**：`web/core/evolution_infra.py` 第 224-226 行

**当前代码**：

```python
tmp = PIPELINE_STATE_FILE.with_suffix(".tmp")
tmp.write_text(json.dumps(state, indent=2))
os.replace(str(tmp), str(PIPELINE_STATE_FILE))
```

**修改方案**：

```python
tmp = PIPELINE_STATE_FILE.with_suffix(".tmp")
with open(tmp, "w") as f:
    f.write(json.dumps(state, indent=2))
    f.flush()
    os.fsync(f.fileno())
os.replace(str(tmp), str(PIPELINE_STATE_FILE))
```

**验证**：代码审查确认 fsync 在 rename 之前执行。

***

## 不在本次计划中的改进（记录但延后）

| 问题                                     | 原因                           |
| -------------------------------------- | ---------------------------- |
| v8 评估场次不足                              | 需要运行 daemon 增加比赛，非代码修改       |
| `force_advance` 审计日志                   | 低优先级，当前使用频率极低                |
| experience\_pool 并发写入                  | Consolidator 每 3 代运行一次，冲突概率低 |
| `archive_rotate_files()` TOCTOU        | 影响范围仅为日志条目，不影响核心评分           |
| `_validate_worker_boundaries` 正则精度     | Tuner 在注释中隐藏代码的风险极低          |
| `replay_analysis.py` 未完成的 `history` 处理 | 功能影响未知，需进一步调查                |

***

## 修改文件清单

| 文件                                  | 改进项                   | 修改类型 |
| ----------------------------------- | --------------------- | ---- |
| `web/core/elo_daemon.py`            | #1 save\_ratings 原子写入 | 修改函数 |
| `web/core/agent_workers.py`         | #2 Worker 零改动检测       | 修改函数 |
| `web/core/prompts/worker_prompt.md` | #2 增加强制编辑提示           | 修改文档 |
| `web/core/generation_scheduler.py`  | #3 辅助 Agent 并行化       | 修改函数 |
| `web/core/llm_query.py`             | #4 代码去重 + 限流改进        | 重构函数 |
| `web/core/tool_eval.py`             | #5 inline eval 安全写入   | 修改函数 |
| `web/core/evolution_infra.py`       | #6 checkpoint fsync   | 修改函数 |

***

## 验证步骤

1. **单元验证**：每个改进完成后，检查修改文件的语法正确性 (`python -c "import py_compile; py_compile.compile('file.py')"`)
2. **集成验证**：运行 `python web/main.py --tui --no-daemon` 启动 TUI 模式，执行一代 dry-run 确认无崩溃
3. **数据验证**：检查 `web/core/results/glicko_ratings.json` 格式正确，`pipeline_state.json` 正常读写
4. **回归验证**：确认 daemon 正常运行 (`python web/core/elo_daemon.py --pairs 2 --workers 4 -v`)，评分更新正常

