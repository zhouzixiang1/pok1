# Phase 1 + Phase 2 对抗性审计报告

> **审计日期**: 2026-06-10
> **审计方法**: 7-agent Workflow（5 审计 + 2 独立验证）
> **审计范围**: Phase 1（13 任务 P0 修复）+ Phase 2（4 任务流水线优化）
> **代码变更**: 50 文件, +9162/-304 行
> **测试状态**: 463 tests passed, 0 failed, 5.91s

---

## 总览

| 严重级别 | 数量 | 需修复 |
|---------|------|--------|
| **Critical** | 6 | ✅ 全部 |
| **High** | 7 | ✅ 全部 |
| **Medium** | 14 | ⚠️ 选择性修复 |
| **Low** | 12 | ❌ 低优先 |
| **Info** | 13 | — 已确认正确 |

**核心结论**：
1. **Phase 1（Bot P0 修复）** — ✅ 全部正确。Wheel straight、re-raise、sanitize_action、TOTAL_HANDS、circuit breaker、_py_files_changed_between 修复均已验证无误。
2. **Phase 2.2（Worker 并行化）** — ⚠️ 有 1 个 critical bug（失败不回滚）+ 1 个 high bug（UI 竞态）
3. **Phase 2.3（Battle Scheduler）** — 🔴 **不可用**。有 3 个 critical 运行时崩溃 bug（API 签名不匹配、数据格式不匹配、TOCTOU 丢数据），从未在真实环境运行过。

---

## Critical 发现（6 项）

### C1: SCHED-001/EVAL-001 — write_result() 签名不匹配，daemon 必崩
- **文件**: `elo_daemon.py:598-614, 792-799`
- **问题**: `battle_scheduler.write_result()` 接受 1 个 `BattleResult` 参数，但 daemon 传了 2 个位置参数 `(ext_job_id, {...dict...})`
- **影响**: 每次 scheduler job 完成必崩 TypeError，结果丢失
- **修复**: 改为 `write_result(BattleResult(job_id=ext_job_id, wins_a=..., wins_b=..., ...))`

### C2: SCHED-002 — drain_pending_jobs() 返回 dict，daemon 期望 7-tuple
- **文件**: `elo_daemon.py:546, 575, 636, 655`
- **问题**: `drain_pending_jobs()` 返回 `list[dict]`，daemon 的外部任务检测用 `len(m) == 7 and m[0] == "external"` — dict 永远不匹配
- **影响**: 外部任务被加入 match_queue 但永远不会被识别和执行
- **修复**: 转换为 `('external', job['job_id'], job['bot_a_name'], job['bot_b_name'], job['bot_a_path'], job['bot_b_path'], job['n_pairs'])`

### C3: BS-001 — drain_pending_jobs TOCTOU 竞态丢失任务
- **文件**: `battle_scheduler.py:153-219`
- **问题**: 读用 LOCK_SH，写用 LOCK_EX，中间 submit_jobs 追加的任务被 truncate 丢弃
- **影响**: 高并发时静默丢任务
- **修复**: 整个 drain 操作用单一 LOCK_EX

### C4: BS-002 — 全部任务无效时 pending 文件永不截断
- **文件**: `battle_scheduler.py:217-219`
- **问题**: `if valid:` 分支同时控制 claimed append 和 pending truncate。无 valid 任务时 pending 不清空
- **影响**: 过期任务每次 drain 都生成重复 error result，无限增长
- **修复**: 将 truncate 移到 `if valid:` 外面，只要有 pending 就清空

### C5: AW-006 — 并行 Worker 失败不回滚文件，注释是错的
- **文件**: `agent_workers.py:298-301`
- **问题**: `_run_single_worker` 失败返回 False 时，只在 TimeoutError/Exception 时回滚，zero_changes/compile_error 不回滚。外部 gather 注释说"已回滚"但实际没有
- **影响**: 并行执行中一个 worker 失败后，bot 目录包含失败的半成品编辑
- **修复**: `elif not result:` 分支增加和 Exception 相同的回滚逻辑

### C6: AW-006（续）— 并行 Worker 失败后残留脏文件
- 同 C5，另一描述角度

---

## High 发现（7 项）

### H1: SCHED-003 — .get() 调用在 Glicko2Player 对象上必崩
- **文件**: `generation_scheduler.py:351`
- **问题**: `ratings[b].get('r', 0)` — Glicko2Player 无 .get() 方法
- **影响**: source-v 循环检测触发时 AttributeError（外层 try 吞掉但逻辑失效）
- **修复**: 改为 `ratings[b].r`

### H2: EVAL-002 — scheduler 部分回退后 opponents 变量被覆盖
- **文件**: `tool_eval.py:239, 396`
- **问题**: `opponents = missing_opponents` 修改变量后，result['opponents'] 只列 missing 而非完整列表
- **影响**: checkpoint 和 result 中的对手信息不完整
- **修复**: 保存 `original_opponents = list(opponents)` 先

### H3: EVAL-003 — scheduler_capable=True 硬编码
- **文件**: `daemon_management.py:85`
- **问题**: PID 文件始终写 `scheduler_capable=True`，但 daemon 的 battle_scheduler import 可能失败
- **影响**: precommit eval 走 scheduler 路径但 daemon 无法处理
- **修复**: 基于 import 成功与否设置 flag

### H4: BS-003 — collect_results 与 write_result 竞态丢数据
- **文件**: `battle_scheduler.py:260-271`
- **问题**: collect 读 LOCK_SH、写 LOCK_EX，中间 write_result 追加的新 result 被 truncate 丢弃
- **修复**: collect 和 cleanup 用 LOCK_EX 从头开始

### H5: BS-004 — submit_jobs 并发可超出 MAX_PENDING_JOBS
- **文件**: `battle_scheduler.py:123-138`
- **问题**: count check 用 LOCK_SH，append 用 LOCK_EX，并发提交可双双通过检查
- **修复**: 用 LOCK_EX 原子读+检查+追加

### H6: BS-005 — 并发 drain 会重复认领任务
- **文件**: `battle_scheduler.py:143-222`
- **问题**: 无机制防止两个 daemon 同时 drain，导致同一任务被两个 daemon 执行
- **修复**: 添加 drain lock 文件或 claim-token

### H7: AW-002 — 共享 WebUI 无并发保护
- **文件**: `agent_workers.py:92-93`
- **问题**: 并行 worker 共享 UI 实例，`clear_io()` 互相覆盖
- **影响**: SSE 输出混乱、history 消息丢失（GIL 保证 list.append 原子但截断不安全）
- **修复**: 并行路径中跳过 clear_io/set_status，或加 asyncio.Lock

---

## 确认正确的 Phase 1 修复

| 修复 | 状态 | 验证 |
|------|------|------|
| BOT-001 Wheel straight (4 sites) | ✅ | 所有 4 处正确识别 A-2-3-4-5 |
| BOT-002 Re-raise +1 | ✅ | 严格 > 2x，首次加注保守 1 chip 可接受 |
| BOT-003 Sanitize call(0) | ✅ | 引擎自动处理 short-stack all-in |
| BOT-004 TOTAL_HANDS=70 | ✅ | 一致性正确 |
| PIPE-001 Circuit breaker | ✅ | 用实际 failure_count，非 len(tasks) |
| PIPE-002 Git timeout | ✅ | (未在此次审计中再次验证但 Phase 1 已修) |
| PIPE-003 Checkpoint lock | ✅ | (同上) |
| _py_files_changed_between | ✅ | 早期返回位置正确，所有调用方兼容 |

---

## 修复优先级排序

### P0 — 立即修复（阻塞 scheduler 正常运行）
1. **C1** + **C2**: elo_daemon scheduler 集成的 API 签名/数据格式修复
2. **C3** + **C4**: battle_scheduler 并发安全修复
3. **H1**: generation_scheduler .get() → .r
4. **C5**: worker 并行失败回滚

### P1 — 尽快修复（影响可靠性）
5. **H2**: opponents 变量覆盖
6. **H3**: scheduler_capable flag 正确设置
7. **H7**: WebUI 并发保护
8. **H4** + **H5** + **H6**: scheduler 完整并发安全加固

### P2 — 后续优化
9. MEDIUM 中选最重要的：BS-006 (crash safety), BS-007 (空路径), AW-003 (verify_code 范围)

---

*审计完成时间: 2026-06-10 | 方法: 7-agent Workflow | 总工具调用 ~160 次*
