所有关键论点均已核实。`worker_failures.jsonl` 的尾部条目是 worker 编译错误（而非 critic 拒绝）—— 我会妥善处理。现在我已经具备了撰写报告所需的一切信息。

---

# 进化系统停滞根因分析与战力提升方案

> 报告基于对 `web/core/` 的代码事实核验与对 14 项对抗验证后保留/否决的发现的综合。所有 file:line 均经独立 Read/Grep/Bash 确认。当前实测状态: `pipeline_state.json` stage=`reviewed`, next_v=85, source_v=83, generation_attempt=2, critic.passed=False, score=0.0, feedback="Missing required field in assistant message: 'signature'", quality.passed=True, review.passed=True。

## 一、执行摘要

进化系统的停滞由两个**相互独立但相互加剧**的问题构成,必须分开理解:

**产出停滞(卡住不 commit)** 的唯一真实根因是 **claude_agent_sdk 0.2.91 的 "Missing required field: signature" 流式错误**。这是一个**进程级确定性故障**:对同一 prompt 反复触发,`llm_query.py:205-241` 的 3 次签名重试对 Critic 角色完全无效(v85 的 Critic 在 23:23-23:34 共 6 次重试全部崩溃),耗尽后异常落入 `agent_review.py:104-106` 的 `except Exception` 兜底,被静默转换成 `score=0, approved=False, local_optima_warning=False`。基础设施崩溃被伪装成"策略不合格",触发 `tool_gates.py:613-625` 的 generation_attempt++ 与 retry_workers,但 worker 代码本身完全健全(bluff_heavy_call_widen 已通过 quality gate,review 9 分)。v85 因此卡在 `reviewed` 阶段永远过不了 critic,直到运气使 Critic 调用偶发成功才 commit(v84 经历了 2 次崩溃后第 3 次才通过)。**这不是 v85 独有,是 v82/v84/v85 三代 critic 失败的共同根因**,是当前进化"产不出新 bot"的直接原因。

**战力停滞(有 commit 但变强有限)** 的根因是**评估信号丧失梯度**:mirror_battle 的对称互博使所有 bot 胜率被数学性地压成 0.50±0.01(实测 252 个≥200 局配对,0 个 |delta|≥0.05,中位数恰好 0.5000,stdev=0.0098)。加上 Glicko-2 缺乏锚点,30 个 bot 的 rating 全部坍缩到 362-396 的 34 点区间(rd=30-44),最强打最弱的期望胜率仅 0.548。**任何真实的策略改进都无法被检测到**,Master 只能在无信号噪声里选方向。叠加 `experience_pool.md` 第 21 行的 `[POSSIBLY EXHAUSTED]` 常量调优禁令 + `worker_failures.jsonl` 跨代累积的 fold-gate critic 拒绝文本,Master 被迫每代产出"对已存在分类器的 marginal offensive 小改"(v85 的 call-widening bluff-catcher 正是此类),这才是几代 commit 但 rating 不动的物理根因。

**关键澄清**:大量被对抗验证否决的"发现"都是**误诊**——它们把产出停滞归因于"orchestrator LLM 不调下一阶段工具""无机械推进兜底""EXHAUSTED 单向棘轮"等。实测全部为假:`watchdog`(orchestrator.py:513-580)存在且已触发 2 次;orchestrator LLM 在 v85 确实调用了 run_critic(2 次);EXHAUSTED 标记数已从 v79 的 3 个降到 v84 的 1 个(fold 方向在 pool 第 8 行被显式标为 "NOT exhausted")。**真正的产出瓶颈是 SDK signature bug,不是管线设计缺陷。**

---

## 二、停滞根因(按严重度排序)

### 根因 1【产出停滞 · critical→high】Critic 的 `except Exception` 兜底把 SDK 签名错误伪装成策略拒绝,触发整代 worker 重跑

- **机制**:`_run_critic`(`agent_review.py:104-106`)对 `run_claude_query` 包了 `except Exception as e: return {score:0, approved:False, feedback:str(e), local_optima_warning:False}`。`llm_query.py:205-241` 虽有 3 次签名重试,但 v85 Critic 的签名错误是**确定性的**(同一 prompt 反复崩),3 次重试全部失败后 `raise last_sdk_err`,异常落入兜底 → score=0。`tool_gates.py:613-625` 在 not-approved 时 `generation_attempt+=1` 并记 worker_failure,触发 `action: "retry_workers"`,**worker 代码被从 source 重置重写**——一个完全健全的 bot 被当作"策略不合格"无限返工。
- **代码证据**:`agent_review.py:104-106`;`llm_query.py:205-241`(3 次 retry 对 Critic 无效);`tool_gates.py:613-625`(attempt++);`pipeline_state.json` 实测 critic.score=0.0 + feedback 含 "signature"。
- **战力影响**:不直接降低已 commit bot 的战力,但**阻断产出**——v85(健全代码)永远过不了 critic gate,每代浪费 ~$5-7 LLM 预算与 ~10 分钟 cycle。长期看:进化完全停摆,pool 不再增长。
- **严重度**:high(非永久死锁——v84 证明偶发 critic 成功可恢复,但当前 live 卡死)。

### 根因 2【产出停滞 · high】13 次 cycle_timeout 浪费 ~13h 墙钟,每 4.4 个 cycle 才产出 1 个 commit

- **机制**:`CYCLE_TIMEOUT=3600s`(orchestrator.py:233)。system_events.jsonl 记录 13 次 cycle_timeout(7 次在 master_planned,3 次 reviewed)。真实机制不是"单次 Master 调用耗满 1 小时"(Master 中位耗时 415s),而是 **intra-cycle retry 循环**:critic reject → master 重 plan → workers 重写 → quality → review → critic 再 reject,一个 3600s 窗口内跑完多轮完整 pipeline 往返。日志统计:17 次 critic_rejected + 12 master_audit_rejected + 11 master_audit_retry + 10 worker_exhausted_warning = 50 次 intra-cycle 重试轮次 vs 14 commit。
- **代码证据**:`orchestrator.py:233`(CYCLE_TIMEOUT);`orchestrator.py:274`(stage-aware 超时缓解只覆盖 verified/critic_checked,不覆盖 master_planned/reviewed——而 10/13 超时发生在这两阶段)。
- **战力影响**:吞吐量税,每 commit 实际墙钟 ~2.9h。间接加重停滞感。
- **严重度**:high(超时是症状,根因是根因 1 + 根因 3 的 retry 循环)。

### 根因 3【产出+战力停滞 · high】`experience_pool` 与 `worker_failures` 的 fold-gate 拒绝历史形成**自强化禁令循环**

- **机制**:两条独立注入路径把 critic 的"fold-gate 系枚举型拒绝理由"当作持久证据:(1) LLM `direction_audit` 读 git 历史 + `worker_failures.jsonl` critic local_optima 生成 mandatory_constraints;(2) **机械回退** `_build_cross_gen_constraint_block`(`tool_planning.py:732-772`)无条件读 `worker_failures.jsonl` 近 3 条 critic local_optima + pool EXHAUSTED 条目,注入 Master 的 performance_verification。`_load_recent_critic_local_optima`(`tool_planning.py:684-729`)**只有 `g > next_v` 前向过滤,无时间/代际滑窗**,而 `worker_failures.jsonl` 只增不轮转(实测 30 行跨 v49-v85)。结果:v82 的 fold-gate 拒绝文本被反复注入,Master 被迫每代找"NOT a fold gate"的 marginal offensive 方向。
- **代码证据**:`tool_planning.py:684-729`(无滑窗的 local_optima 加载);`tool_planning.py:732-772`(无条件注入);`worker_failures.jsonl` 永久累积;`experience_pool.md:8`(fold 已被标 NOT exhausted——但 critic 文本仍在注入)。
- **战力影响**:fold-gate 是 battle_experience.md 标记的"#1 战力杠杆"(0% postflop fold 跨 v13-v84 全部 7700+ halves 零例外),但被禁令压制无法触碰。**重要修正**:经验池本身已解封 fold(第 8 行 "NOT exhausted"),真正的棘轮在 `worker_failures.jsonl` + 机械注入,而非 pool 的 tag-loss guard。
- **严重度**:medium(形状化方向但不完全瘫痪——v85 仍产出了 call-widening plan;但长期把可达方向空间压缩到 marginal offensive 小改)。

### 根因 4【战力停滞 · medium】mirror_battle 对称互博使所有 bot 胜率压成 0.50±0.01,Glicko 丧失梯度信号

- **机制**:daemon 全程用 `mirror_battle`(每手交换底牌打两遍)消除发牌运气。所有 bot 同源(v1→v84,每代仅微调),策略趋同,mirror 互博数学上必然收敛 50/50。实测 252 个≥200 局配对:**0 个 |delta|≥0.05,stdev=0.0098,中位数恰好 0.5000**。Glicko-2 更新量 delta=phi²×Σg(score−e),当所有 e≈0.5 且实际 score≈0.5 时 delta≈0。
- **代码证据**:`elo_daemon.py:360-372`(单局 game winner,非 mirror 配对净胜);`glicko2.py:60-165`(`update_rating_period`);`head_to_head.json` 实测分布。
- **战力影响**:**根本性的**——任何真实改进都无法被检测,Master 据此选方向等于随机走。这是"有 commit 但变强有限"的物理根因。
- **严重度**:medium(信号未完全失效——h2h_avg_wr 仍能区分 v83=0.514 vs v84=0.491,但噪声远大于改进幅度)。

### 根因 5【战力停滞 · medium】Glicko-2 缺锚点,rating 全簇坍缩到 362-396(跨 34 点),最强打最弱期望胜率仅 0.548

- **机制**:新 bot 从 r=1500 开始,但群体在 r≈380,每局 E(1500 beats 380)=0.998 而实际 50/50 → 0.498 惊喜,把新 bot rating 猛拽下来,~100 局收敛到 380。一旦全部同 r,E 压到 0.5,rating 冻结。**这不是 bug 而是 Glicko-2 无锚点的固定点行为**——但后果是绝对 rating 携带零信息。
- **代码证据**:`glicko2.py:60-165`(无均值回归);`glicko_ratings.json` 实测 30 bot 在 352-407;mean 380 比 Glicko 默认 1500 低 1120。
- **战力影响**:任何依赖绝对 rating 阈值的逻辑失效。缓解:`master_prompt.md:142` 已有"ALL H2H 在 45-55% → PLATEAU"检测,reap 用 h2h_avg_wr 而非 rating 排序。故影响有限。
- **严重度**:medium(管线已部分自适应,但信号贫瘠)。

### 根因 6【两者 · low】`run_spot_check` / `spot_analyzer.py` 全模块为死代码,spot-gate 永不触发

- **机制**:`run_spot_check`(`tool_gates.py:726-766`,398 行的 `spot_analyzer.py` 唯一消费者)未在 `tools.py:67-89` 的 mcp_tools 列表或 `tool_pipeline.py` 中注册。orchestrator LLM 永远无法调用。实测 grep 确认。
- **战力影响**:无(它本应被 wiring 为质量门但从未生效)。维护负担 + 审计噪声。
- **严重度**:low(纯清理项)。

---

## 三、战力提升方案(按投入产出比排序)

### 方案 A(ROI 最高,~2h)——修复 Critic 签名崩溃的误判,解除产出停滞

**这是 ROI 最高的一项:不动它,后续所有战力提升都无法落地。**

1. **改 `agent_review.py:104-106`**:把"基础设施崩溃"与"策略拒绝"区分。在 `_run_critic` 内部捕获 `ClaudeSDKError` / 含 "signature"/"missing required field" 的异常,返回一个**新的 distinct 状态** `{critic_infra_error: True}`,而非 `score=0`:
   ```python
   except ClaudeSDKError as e:
       ui.log_history(f"Critic infra crash (SDK): {e}", "error")
       return {"score": None, "approved": None, "critic_infra_error": True,
               "feedback": f"Critic infra crash: {e}", "local_optima_warning": False}
   ```
2. **改 `tool_gates.py:613-625`**:critic 返回 `critic_infra_error` 时,**不**递增 generation_attempt、不记 worker_failure、不触发 retry_workers,而是让 orchestrator 直接重试 run_critic(单独的 critic 重试预算,如 5 次,与 generation_attempt 解耦)。
3. **改 `llm_query.py:205-241`**:3 次签名重试对 Critic 确定性失败无效,需对 prompt 做温和变异——**裁掉 `prev_critic_result` 注入段**(若 critic prompt 含上轮 critic 结果的累积反馈,这会改变 prompt shape 触发的验证缺陷)或缩短 context 到 < 50K chars。验证:对当前 v85 checkpoint 手动重跑 run_critic 并对 prompt 变异,观察是否仍崩。
4. **检查 `claude_agent_sdk` 版本**:若仍 0.2.91 且无升级,signature bug 是 SDK 已知缺陷,考虑 pin 到不含该 bug 的版本(需查 changelog)。

**为什么提升战力**:不直接提升单 bot 战力,但**解除进化停摆**,让 v85(健全代码)能 commit,pool 恢复增长。无此修复,方案 B-F 全部无法落地。

---

### 方案 B(ROI 高,~4h)——给 `worker_failures.jsonl` 加滑窗轮转,打破 fold-gate 禁令自强化

**针对根因 3,直接恢复 Master 的可达方向空间。**

1. **改 `tool_planning.py:684-729` `_load_recent_critic_local_optima`**:加**代际滑窗** `g >= next_v - 8`(只取近 8 代),替代当前仅有 `g > next_v` 前向过滤:
   ```python
   # 原:if g > next_v: continue
   # 改:if not (next_v - 8 <= g <= next_v): continue  # 8-gen sliding window
   ```
2. **加轮转**:在 `tool_commit.py` commit 成功后(`archive_rotate_files` 附近),对 `worker_failures.jsonl` 做行数轮转——保留最近 60 行(约 8 代),超出移到 `archive/`。可复用 `evolution_infra.py:814` 的 `archive_rotate_files` 模式。
3. **改 `experience_pool.md`**:把第 21 行的 `[POSSIBLY EXHAUSTED]` 常量调优条目降级——battle_experience.md 实测显示 "fold MORE preflop" 是唯一胜率杠杆(preflop fold 50-65% 区间决定胜负),但当前审计只禁 postflop fold gate。**显式区分**:postflop fold gate(确已 EXHAUSTED,0% 触发)vs **preflop range tightening(高价值,未探索)**。在 pool 加一条:"PREFLOP range tightening (narrowing open/defense ranges) is UNDER-EXPLORED and high-value — distinct from postflop fold gates."

**为什么提升战力**:解除 fold-gate 禁令循环后,Master 可探索 battle_experience 标记的"#1 战力杠杆"(preflop fold rate),这是 v74-v84 marginal offensive 小改触及不到的真正结构性弱点。

---

### 方案 C(ROI 高,~6h)——引入外部锚点 bot,恢复 Glicko 梯度信号

**针对根因 4+5,这是"战力提升"的核心——无信号则无进化。**

1. **引入固定参考 bot**:在 `web/core/reference_bots/`(已有 bot1-bot6)选 2-3 个风格鲜明的**异质 bot**(超紧 NIT / 超松 CS / 凶猛 LAG)作为 Glicko 锚点,强制 daemon 每轮让 pool bot 与之对局。改 `elo_daemon.py:563` `pick_matches`:每轮固定注入 20% 的"(pool bot, 锚点 bot)"配对。
2. **改评估口径**:在 `combined_analyst.py` 的停滞检测与 Master 的方向选择中,**优先使用 vs 锚点 bot 的 H2H 胜率**而非 pool 内 mirror H2H。锚点 bot 风格固定、非镜像,胜率信号有真实梯度(一个改进 bluff 的 bot vs 超紧 NIT 会有可测胜率变化)。
3. **(可选)非镜像评估通道**:在 `eval_rounds.py` 增加一个"非 mirror 锚点轮"——用 `battle()`(非 mirror)让 pool bot vs 锚点 bot,记录原始胜率方差,作为 mirror 信号的校准。

**为什么提升战力**:这是**唯一能恢复进化梯度**的方案。mirror 对称互博在数学上无法区分同源 bot;异质锚点引入非对称信号,Master 才能真正检测"哪个改动变强了"。预期效果:几代内 rating 重新分散,h2h_avg_wr 信号可区分真实改进与噪声。

---

### 方案 D(ROI 中,~3h)——减小 Master prompt 预算 + 削减 intra-cycle retry,降低 cycle_timeout 浪费

**针对根因 2。**

1. **改 `evolution_infra.py` `MAX_PROMPT_CHARS=700000`**:降到 200000。Master prompt 过大导致单次调用慢(实测 master_done 中位 415s,最大 1845s),且长 prompt 加重 signature bug 触发率。Master 真正需要的核心输入是:ratings + experience_pool + match_analysis + cross-gen 约束;其余(battle_experience/exploitability/eval_round_summary)可截断或移到可选附件。
2. **改 `orchestrator.py:274` 的 stage-aware 超时扩展**:把覆盖范围从 `verified/critic_checked` 扩展到 `master_planned/reviewed`(超时高发阶段),grant ONE extension + checkpoint 恢复,而非整 cycle 废弃。
3. **改 `orchestrator.py:154-162` 冗余调用检测**:从"仅 warn"升级为"第 2 次 run_master 直接 abort 并从 checkpoint 恢复",省掉 ~$1/次的浪费。

**为什么提升战力**:吞吐量提升 ~30%,同样的 LLM 预算产出更多代,加速方案 B/C 的迭代验证。

---

### 方案 E(ROI 低,~1h)——删除死代码 `run_spot_check` + `spot_analyzer.py` 或正确 wiring

**针对根因 6。**

- **选项 1(推荐)**:删除 `tool_gates.py:726-766` 的 `run_spot_check` + 整个 `spot_analyzer.py`(398 行),减少维护噪声。
- **选项 2**:若意图保留 spot-gate,把它加入 `tools.py:67-89` 的 `mcp_tools` 列表 + `tool_pipeline.py` 的 re-export,并在 `run_quality_gates` 的 `all_passed` 条件里加入 `verify_behavior` 结果。但注意:`spot_analyzer.py:275-327` 的 `_build_requests` 用负数 marker 注入请求,与真实 bot 协议不符,需先修复。

**为什么提升战力**:无直接战力影响,纯清理。优先级最低。

---

## 四、优先级路线图

### P0(本周,立即)——方案 A:解除产出停摆
- **做什么**:改 `agent_review.py:104-106`(区分 critic_infra_error)+ `tool_gates.py:613-625`(infra error 不计 attempt)+ 排查 `claude_agent_sdk` 版本 + 对 critic prompt 做变异实验。
- **预期效果**:v85 能 commit,后续每代不再被 SDK signature bug 卡死。pool 恢复增长。
- **为什么先做**:不解除产出停滞,方案 B-E 无法被验证(没有新 commit 进 pool)。

### P1(本周,P0 完成后)——方案 B + 方案 D
- **方案 B**:滑窗 + 轮转 + 解禁 preflop range tightening。预期:Master 方向空间扩宽,能触及 battle_experience 标记的 #1 战力杠杆。
- **方案 D**:prompt 预算 200K + 超时扩展覆盖。预期:吞吐量 +30%,加速迭代。
- **顺序理由**:B 恢复方向多样性,D 提供迭代预算,二者无依赖可并行,但都依赖 P0 解除停摆。

### P2(下周,P1 完成后)——方案 C:引入外部锚点 bot
- **做什么**:选 2-3 个异质 reference bot 作 Glicko 锚点,改 `pick_matches` 注入 20% 锚点配对,Master 方向选择优先用 vs 锚点 H2H。
- **预期效果**:恢复进化梯度信号。几代内 rating 重新分散,真实战力改进可被检测。
- **顺序理由**:这是**唯一能根治战力停滞**的方案,但工程量最大(需确认锚点 bot 与池内 bot 风格足够异质),且需 P1 的方向空间与吞吐量先就位,否则即便有信号 Master 也无方向可选。

### P3(空闲)——方案 E:清理死代码

---

### 关键风险提示

1. **不要修复"不存在的问题"**:对抗验证已否决 18 项误诊(orchestrator 无机械推进、EXHAUSTED 单向棘轮、68% bot 被丢弃、battle_scheduler 是死路径等)。这些误诊会诱导负责人投入精力改管线设计,但管线设计**不是**瓶颈——SDK signature bug + 评估信号缺失才是。报告中的根因 1-6 已逐项对抗验证,可放心执行。

2. **方案 A 的 prompt 变异需先实验**:在改代码前,先在 v85 checkpoint 上手动重跑 run_critic 并对 prompt 裁剪 prev_critic_result 注入段,验证 signature 错误是否消失。若消失,说明是 prompt-shape-sensitive 而非全局 deterministic,修复路径只需 prompt 调整(更轻量);若不消失,需升级 SDK。

3. **方案 C 的锚点选择是成败关键**:锚点 bot 必须风格鲜明且稳定不变,否则信号本身漂移。建议从 `ref/neuron_poker` 的 EquityPlayer(tight/loose 两档)+ bot1-bot6 中挑,先跑 100 局验证其 vs 池内 bot 的胜率确有方差(非 50/50)。