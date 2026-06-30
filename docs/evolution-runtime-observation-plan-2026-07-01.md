# 进化系统重启后三代观察计划（2026-07-01）

## 背景

本轮修复前，系统已经暴露出三类根因：

1. `run_precommit_eval` 把 scheduler 120 秒无“完成结果”误判成 scheduler 死锁，却没有区分 job 已经被 daemon 领取并正在运行。v233/v234 都出现了 `scheduler_stall -> parallel fallback`，但迟到的 scheduler 结果随后正常写入。
2. sub-agent guard 把只读父代探索（例如 `ls bots/claude_v224`、`python -c open(...).read()`）误判为越界写操作，导致 crossover/修复阶段产生不必要的 `subagent_guard_block`。
3. `run_archivist` 在 `commit_bot` 之后会移动 tracked bot 到 gitignored graveyard，并写 `web/core/experience_pool.md`，旧流程没有后续提交，因此会制造用户可见脏树。

已落地修复：

- `battle_scheduler.get_job_status()`：非破坏性查看 `pending / claimed / completed / missing`。
- `run_precommit_eval`：改为 claimed-aware stall 判定；claimed job grace 与 job timeout/poll budget 对齐，不再 120 秒 fallback。
- daemon external job 结构化日志：`daemon.external_job_dispatched`、`daemon.external_job_result`。
- sub-agent Bash 写操作识别：允许只读父代探索，继续禁止重定向、删除、移动、写文件和 git 写操作。
- archivist housekeeping commit：经验池更新和 tracked reap 删除走单独提交，避免留脏树。

## 文献和工程依据

- 异步进化评估能减少同步等待，但会带来 evaluation-time bias：短耗时个体得到更多搜索机会。因此 scheduler 不应只追求吞吐，还要记录 job 状态、等待时间和评估覆盖，避免“快完成的对战”主导系统判断。参考：Scott & De Jong, *Evaluation-Time Bias in Asynchronous Evolutionary Algorithms*；Harada, *A frequency-based parent selection for reducing the effect of evaluation time bias in asynchronous parallel multi-objective evolutionary algorithms*。
- 任务队列必须把“已领取/处理中”和“无进展”分开。SQS visibility timeout 文档明确将 received-but-not-deleted 消息视为 in-flight，并要求 timeout 与实际处理时间对齐；Celery 文档也强调消息 ack、幂等和长任务 timeout。这对应本系统的 `claimed` 文件和 external job dispatch/result 日志。
- Glicko-2 的官方说明强调不要只报告单点评分，应同时看 RD 和区间；本系统用户侧展示应优先解释 conservative rating、RD、样本量和 H2H 覆盖。
- paired bootstrap 的核心价值是同一测试集上比较两个系统，并用重采样估计小差异是否可信。precommit 使用 paired net-chips 比单纯 W/L 更适合 NLHE 的高方差评估，但用户侧必须展示 CI、样本数和 caution，不应把 8-40 局当成最终强弱结论。

参考链接：

- https://mason.gmu.edu/~escott8/pdf/2015-gecco-Scott-SW.pdf
- https://arxiv.org/abs/2107.12053
- https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html
- https://docs.celeryq.dev/en/stable/userguide/tasks.html
- https://www.glicko.net/glicko/glicko2.pdf
- https://aclanthology.org/W04-3250.pdf

## 用户侧计划

用户需要看到的是“系统到底在做什么”和“当前风险是什么”，不是只看到一个 stage 卡住。

最低可接受呈现：

- 当 precommit 进入 scheduler 路径时，日志必须出现：
  - `pipeline.precommit_eval.scheduler_start`
  - `pipeline.precommit_eval.scheduler_jobs_submitted`
  - 每约 60 秒一次 `pipeline.precommit_eval.scheduler_waiting`
  - 每个外部 job 的 `daemon.external_job_dispatched`
  - 每个外部 job 的 `daemon.external_job_result`
- 如果 fallback 发生，必须带 `reason`：
  - `jobs_never_claimed`
  - `jobs_missing_from_scheduler_files`
  - `no_scheduler_activity`
  - `claimed_jobs_exceeded_grace`
- 质量失败必须在事件里带 `failed_gates` 和具体字段，如 `smoke_errors`、`critical_failures`、`decision_pass_rate`。
- archivist/reap 后必须能从 git 历史看见 housekeeping 提交，而不是在 `git status` 留下 tracked deletion。

用户侧解释口径：

- `scheduler_waiting` 不是失败，是 job 已提交且仍在等待/运行。
- `precommit_caution` 不是阻塞，是高方差解释；最终是否提交由 paired bootstrap + blocker 决定。
- Glicko 排名看 conservative rating 和 RD，不用单次 precommit W/L 判断“谁最强”。
- 如果系统产生脏树，应先判断是否是 evolution housekeeping；正常情况下修复后不应再出现未提交的 tracked reap 删除。

## 进化系统侧计划

硬性不变量：

- bot 代码只能通过 pipeline 工具生成和提交；手工/LLM Bash 不得绕过 `execute_workers`、`run_crossover`、`commit_bot`。
- `run_precommit_eval` 不应在 job 已 claimed 且未超 grace 时 fallback。
- `run_archivist` 后如果有 tracked housekeeping 变更，要么提交，要么记录跳过原因；不能静默留脏。
- 三代观察期间 `git status --short --branch` 在每代完成后应为干净，除非当前代未完成并存在 checkpoint。

后续可选增强：

- 将 `BattleJob.timeout_sec` 进一步带入 daemon in-flight 元数据，实现 worker 侧 job 级主动取消；当前 precommit 侧已按 job timeout/poll budget 避免过早 fallback。
- 结果记录加 `version/source_v/opponent/run_id`，减少迟到 scheduler 结果的归因成本。
- 前端增加 precommit job 表，展示 job id、opponent、pending/claimed/completed、运行时长。

## 三代观察验收标准

从修复后的 `main` 重启系统，观察连续三代，目标版本从当前最高 tag 后开始（当前为 `bot-v234`，预计观察 v235-v237）。

每一代通过标准：

1. 流水线至少达到一个终态：
   - 成功：`pipeline.commit_done` 后 `pipeline.archivist_done`
   - 合法失败：`pipeline.abandoned`、`pipeline.master_audit_exhausted_abandon`、`pipeline.precommit_hard_limit`
2. 若进入 precommit scheduler 路径：
   - 不应在 claimed job 正常运行时出现旧式 120 秒 `scheduler_stall`。
   - 如果出现 `scheduler_stall`，事件必须带 `reason` 和 `scheduler_status`。
3. 不应出现这些系统级崩溃：
   - `orchestrator.crashed`
   - `daemon.crashed` 连续重启耗尽
   - Python scope 类错误：`cannot access local variable 'log_system_event'`
4. 不应出现旧误判：
   - 成功 smoke 但因 cleanup `BrokenPipeError` 被判 `smoke_test` 失败。
   - sub-agent 只读父代探索被 `pipeline.subagent_guard_block` 拦截。
5. 每代结束后工作树应干净；若不干净，要能明确归因到未完成当前代或被记录的 housekeeping skip。

## 观察命令

推荐用脚本启动并观察三代：

```bash
./scripts/pok_restart_observe.sh --no-build --observe-generations 3 --observe-timeout 32400
```

观察期间辅助检查：

```bash
git status --short --branch
python - <<'PY'
import json, time
from pathlib import Path
p = Path('web/core/results/pipeline_state.json')
print('checkpoint_exists', p.exists())
if p.exists():
    d = json.loads(p.read_text())
    print({k: d.get(k) for k in ['stage', 'run_id', 'next_v', 'source_v', 'parent2_v', 'precommit_attempt']})
    print('age', round(time.time() - d.get('last_update_ts', 0), 1))
PY
```

事件检查：

```bash
python - <<'PY'
import json, time
from pathlib import Path
events = []
for line in Path('web/core/results/system_events.jsonl').read_text(errors='replace').splitlines():
    try:
        events.append(json.loads(line))
    except Exception:
        pass
for e in events[-120:]:
    d = e.get('data') or {}
    print(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e.get('ts', 0))), e.get('type'), e.get('severity'), e.get('message', '')[:180])
    show = {k: d.get(k) for k in ['stage', 'run_id', 'next_v', 'source_v', 'version', 'reason', 'scheduler_status', 'failed_gates', 'passed', 'score', 'commit', 'tag'] if k in d}
    if show:
        print(' ', show)
PY
```

## 失败处理

- 若 scheduler fallback 仍发生但 `reason=claimed_jobs_exceeded_grace`：先检查 external job 是否超过 job timeout/poll budget，再决定是否增加 daemon worker 侧主动取消。
- 若 `jobs_never_claimed`：优先看 daemon 是否在 drain pending jobs，检查 `battle_jobs.jsonl`、`battle_jobs.claimed` 和 `daemon.external_job_dispatched`。
- 若 `jobs_missing_from_scheduler_files`：检查 `collect_results` 是否提前消费结果、是否有并发 collector。
- 若出现 tracked deletion 或 `experience_pool.md` 脏项：检查 `pipeline.archivist_git_commit_done` 或 `pipeline.archivist_housekeeping_skip_dirty`，确认是否被预先 dirty 保护跳过。
- 若前端看不清状态：先以后端事件字段为准，再补 UI，不用先改 pipeline。
