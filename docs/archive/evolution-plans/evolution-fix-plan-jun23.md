# 修复计划 2026-06-23

> 本文基于 `docs/evolution-arch-prompt-audit-jun23.md` 的 13 个优化点，经 27 agent（13 plan + 13 verify + 1 synthesis）细化为 diff 级实施计划。
> **9 个修复有 plan，其中 4 个 SAFE 可直接落地、5 个有阻断点需先修正、4 个 DEFERRED。**

---

## 0. 总览

### 批次结构

| 批次 | 修复 | 预估工时 | 状态 |
|---|---|---|---|
| Batch 1 — 止血 + 快速闭环 | fix-1-register-literature-probe, fix-11-measurement-plan-cleanup, fix-13-deadcode-cleanup, fix-10-battle-experience-dedup | 2-3h | ⚠️✅✅⚠️ |
| Batch 2 — 校准 + 阻断升级 | fix-2-critic-calibration-async, fix-3-placement-shadow-blocking, fix-8-evidence-gate-extend, fix-9-structure-drift-ast | 4-6h | ⚠️⚠️⚠️✅ |
| Batch 3 — 速度 + 方向治理 | fix-4-event-driven-precommit, fix-5-critic-cross-gen-pivot, fix-7-deepevolve-debug-agent, fix-6-vendi-crossover | 6-10h | ⚠️⚠️✅⚠️ |
| Batch 4 — 治理 + 长期韧性 | fix-12-experience-pool-ratchet | 4-6h | ⚠️ |

### 快速收益（今天可做）

1. **fix-1-register-literature-probe**：1 行改动解锁用户重点需求(Exa 进进化)，145 代零触发的真因是未注册到 mcp_tools
1. **fix-11-measurement-plan-cleanup(a部分)**：删 master_prompt 2 句谎言 + spot_analyzer 1 行死赋值，纯删代码零风险
1. **fix-13-deadcode-cleanup(全部13a-13e)**：5 个小清理(删死代码/过时 prompt 文本/typo 修复)，零功能影响
1. **fix-10-battle-experience-dedup(增量merge rework)**：改为增量 merge + stale 降级，砍 ~40% LLM 成本(但需 rework 为非去重方案)

### 暂缓项

- fix-12-experience-pool-ratchet: 归因信号致命缺陷(commit_bot gate ledger 下 precommit_passed 恒 True) + Ratchet 消融 harsh retirement=-0.019 + 需 N_min>=30。放 Batch 4 最后单独做。
- fix-6-vendi-crossover 的 fingerprint 部分: decision_tester fixture 无 plan 假设的 7 维 key，需 rework 为替代方案(从 match_history replay 统计或 bot 代码 AST 特征抽取)。Batch 3 做但需先完成 rework 设计。
- fix-5-critic-cross-gen-pivot 的跨代 fail 计数: tool_eval.py:968 infra_only_timeout 设 passed=True 但 plan 声称不算 fail 是事实错误，需 grep 确认实际写入行为后才能安全实现。

### 推荐序列 + 决策点

```
Batch 1 (今天可做) → commit+push → daemon ≥30g 验证: run_literature_probe 触发>0? pipeline.quality_failed 无新增? → Batch 2 → daemon ≥30g 验证: placement_shadow 误杀率<5%? critic_calibration 非零 delta 从 2% 升到多少? 1037 tests pass? → Batch 3 → daemon ≥30g 验证: cycle 中位耗时<55min? 同轴重复代数<5? pool Vendi Score 基线? → Batch 4 → daemon ≥100g 验证: experience_pool stale 条目退役后 bot WR 无下降? retired 条目数量(N_min>=30)?
```

### 依赖图

- fix-1-register-literature-probe → 无依赖
- fix-10-battle-experience-dedup → 无依赖
- fix-11-measurement-plan-cleanup → 需先 fix-2-rating-delta-reconcile
- fix-12-experience-pool-ratchet → 需先 fix-1-research-governance-zero-flow, A1-stderr-drain (battle.py:47-105, already landed, enables the dogfood)
- fix-13-deadcode-cleanup → 无依赖
- fix-2-critic-calibration-async → 无依赖
- fix-3-placement-shadow-blocking → 无依赖
- fix-4-event-driven-precommit → 无依赖
- fix-5-critic-cross-gen-pivot → 无依赖
- fix-6-vendi-crossover → 无依赖
- fix-7-deepevolve-debug-agent → 无依赖
- fix-8-evidence-gate-extend → 无依赖
- fix-9-structure-drift-ast → 无依赖

### 共享基建改动

- tool_gates.py:287 all_passed 逻辑 — 仅 fix-3 使用，一次性做(Batch 2 加 and not TRUE SHADOW)。M6 telemetry_fidelity_ok 已在同位置，不与其他 fix 冲突。
- tool_planning.py 跨代状态(direction audit 结果) — fix-4 + fix-5 共用，合批 Batch 3 一次性做。两者都读/写 direction audit 跨代结果，分批做会两次改同一区域。
- tool_eval.py precommit 结果写入 — fix-2(reconcile)在 Batch 2 改 result 文件消费端；fix-4/5(event-driven)在 Batch 3 改 scheduler 内部。位置不同，分批做安全。
- critic_calibration.jsonl 消费链 — fix-11 Batch 1 先删 prompt 谎言；fix-2 Batch 2 再建异步回填。逻辑顺序正确不冲突。
- generation_scheduler.py _pick_crossover_parents — 仅 fix-6 使用，Batch 3 一次性做。
- numpy 依赖(Vendi Score) — fix-6 Batch 3 引入 behavior_diversity.py 需要 numpy，需检查 requirements.txt 是否已有。

### 风险矩阵

- [高风险组合] fix-3(placement_shadow BLOCKING) + fix-5(direction pivot binding) 同时上线: pipeline 卡死风险。两者都是新 BLOCKING gate，同时命中时 cycle 死循环。缓解: 分批(Batch 2 先 fix-3，daemon 30g 验证无误杀后 Batch 3 再 fix-5)。
- [高风险组合] fix-3 + fix-12(Ratchet retire): 间接风险。fix-3 阻断 TRUE-SHADOW → pipeline 更频繁 fail → experience_pool 记录更多失败教训 → fix-12 retire 可能误退役有价值经验。缓解: fix-12 放最后(Batch 4)，有前 3 批 daemon 数据支撑。
- [互为前提] fix-1 → fix-12: fix-12 需要 research_governance 的 score/retire 有实际流量数据。fix-1 注册 run_literature_probe 后才有 candidates → governance 才有数据。
- [互为前提] fix-2 → fix-11(b): fix-11(b) 闭环(Master 预测 vs daemon 实际)依赖 fix-2 异步回填先就位。fix-11(a) 止血部分独立。
- [互为前提] fix-4 → fix-5: fix-5 三重条件中'连续>=2次 precommit fail'需要 fix-4 新增的 stage_durations 精确计数。
- [互为前提] fix-5 → fix-6: fix-6 的 commit novelty gate(binding Vendi Score)需要 fix-5 方向 pivot 先工作，否则双重拒。
- [无冲突] fix-1 + fix-11 + fix-13: 完全独立文件零交叉。
- [无冲突] fix-7 + fix-9: 新建文件 vs 独立 refactor 零交叉。
- [无冲突] fix-8 + fix-9: 不同文件不同层零交叉。

### 测试策略

```
原则: 1037 passed 零回归。每个 fix 的新 test 放 test_logic_*.py 或新建 test_logic_{fix_name}.py。Batch 1 中 fix-1/11/13 无需新 test(纯注册/删代码，现有 test 覆盖)。Batch 2 中 fix-2 需 mock calibration fixture；fix-3 需构造含 TRUE-SHADOW 的临时 bot dir；fix-8 需临时 replay manifest；fix-9 需临时 bot dir 含重命名函数。Batch 3 中 fix-4 需 monkeypatch tool_planning._run_master_analysis；fix-5 需临时 pipeline_state.json fixture；fix-6 需 numpy 内存计算(无外部依赖)；fix-7 需 monkeypatch agent_workers.run_claude_query。Batch 4 中 fix-12 需 mock research_governance + experience_pool fixture。每个 batch commit 前全量 pytest 确认零回归。Batch 2/3 的 BLOCKING gate 额外跑 test_mcp_*.py -k quality。
```

---


## 1. Batch 1 — 止血 + 快速闭环（2-3h）

> 全部 SAFE 或仅需 1 行改动。零风险建模：fix-1 是 1 行注册(解锁 Exa 进进化 145 代零触发的真因)；fix-11 删 prompt 谎言(纯删代码)；fix-13 死代码清理(5 个小清理)；fix-10 需 rework 为增量 merge 而非去重(fingerprint 分布 574×1/51×2 证明去重 skip 0%)。不碰 all_passed/gate ledger/STAGE_ORDER。

### fix-1-register-literature-probe [CRITICAL|architecture|low] ⚠️ 需修正

**fix-1-register-literature-probe: 注册 run_literature_probe 到 orchestrator MCP 工具列表**

run_literature_probe 工具已在 tool_planning.py 完整实现并被 tool_pipeline.py re-export 和 tools.py import，但 tools.py 的 mcp_tools 列表(L68-94)遗漏了它，导致 orchestrator agent 的 MCP session 看不到该工具、145+ 代零触发。修复方法是 tools.py:78 在 run_direction_audit 之后添加 run_literature_probe 一行。其余上下文(stagnation 注入、AVAILABLE TOOLS 文本、governance 逻辑)均已就位无需改动。research_governance_state.json 不存在无 cooldown 残留。STAGE_ORDER 不需改。新增 3 个注册完整性测试。审计结论中"清理 cooldown_until_gen=102"实测不适用(文件不存在)。


<details><summary>当前代码状态（核查后）</summary>

```
tool_planning.py:910 @tool 注册 + 911-1059 完整实现 ✅; tool_pipeline.py:3 re-export ✅; tools.py:32 import ✅; tools.py:68-94 mcp_tools 列表 ❌ 缺 run_literature_probe; orchestrator_context.py:188,236 AVAILABLE TOOLS 文本已列出 ✅; generation_scheduler.py:237 stagnation 注入已就位 ✅; research_governance_state.json 不存在(无 cooldown 残留, _load_state() 返回 {}) ✅; STAGE_ORDER 不需改 ✅
```
</details>


#### 改动点（1 处）

- **`web/core/tools.py`** @ `L78-79 (mcp_tools 列表, run_direction_audit 之后)` [add] — 将 run_literature_probe 注册到 mcp_tools，使 orchestrator agent 的 MCP session 能看到并调用该工具。放在 run_direction_audit 之后保持 analytical pipeline tools 分组逻辑。all_tools = mcp_tools + [...] 自动覆盖，HTTP

<details><summary>完整 diff 详情</summary>


**`web/core/tools.py`** @ `L78-79 (mcp_tools 列表, run_direction_audit 之后)` [add]

现状:
```python
run_direction_audit, commit_bot,
```
改后:
```python
run_direction_audit, run_literature_probe, commit_bot,
```
*将 run_literature_probe 注册到 mcp_tools，使 orchestrator agent 的 MCP session 能看到并调用该工具。放在 run_direction_audit 之后保持 analytical pipeline tools 分组逻辑。all_tools = mcp_tools + [...] 自动覆盖，HTTP 端点也自动可用。*

</details>


#### 测试（3 个）

- `web/tests/test_mcp_pipeline.py::test_name` — mcp_tools 列表包含 run_literature_probe(name 属性匹配)
- `web/tests/test_mcp_pipeline.py::test_name` — mcp_tools 无重复工具名(防误插两遍)
- `web/tests/test_mcp_pipeline.py::test_name` — mcp_tools 工具名集合与期望的 17 个工具完全一致(regression guard)

#### 向后兼容
无调用方需同步改动。mcp_tools 是 tools.py 内部列表，消费方 create_sdk_mcp_server() 和 all_tools 都读列表引用，添加元素即生效。orchestrator_context / generation_scheduler / tool_pipeline 均已就位。


#### 风险
- 注册后 orchestrator 在非 stagnation 时调用: Low。工具描述说 Stagnation-triggered, orchestrator prompt 不会主动调用。governance gate 做最终防御。
- Exa MCP 不可用时工具返回 error: Low。run_literature_probe L984-992 try/except 返回 error JSON, orchestrator 按正常流程继续到 run_master。
- research_governance cooldown 误阻断: Low。首次注册无历史文件 → COOLDOWN_GENS=2 仅在 web-injected gen 失败 precommit 时触发。


#### 验证步骤
```
cd /home/zzx/project/pok/web && python -m pytest tests/test_mcp_pipeline.py -v -k 'literature or mcp_tools'
cd /home/zzx/project/pok/web && python -m pytest tests/ -v (零回归)
cd /home/zzx/project/pok/web/core && python -c "import sys; sys.path.insert(0,'.'); from tools import mcp_tools; names=[t.name for t in mcp_tools]; print('run_literature_probe' in names, len(names))"
```


#### 回滚
从 web/core/tools.py L78 删除 run_literature_probe, 一行即可恢复。mcp_tools 恢复 14 工具。删除 3 个新增测试函数。


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：✅
- 测试充分：✅

**🔴 阻断点：** 无。计划可直接落地,核查全部通过。


**遗漏调用方：** 无遗漏。亲自核查确认:run_literature_probe 已在 tool_planning.py:910-1059 完整实现(含 A6 governance gate should_trigger_web_retrieval + add_candidate + translation_gate),已在 tool_pipeline.py:3 re-export,已在 tools.py:32 import,已在 orchestrator_context.py:188/236 文本列出,generation_scheduler.py:237 已注入 STAGNATION_DETECTED 强制


**修正要点：** 计划可直接落地,核查全部通过。修正/补充要点:

1) 行号核实(全部准确,亲自读代码确认):
- tool_planning.py:910 @tool 注册 + 911-1059 实现 ✓(L995 if False 是无害 stale code 但不在本次 fix scope)
- tool_pipeline.py:3 re-export ✓
- tools.py:32 import ✓ / tools.py:68-94 mcp_tools 列表缺 run_literature_probe ✓(实测当前 16 个工具,无重复)
- tools.py:66 注释写"(16 tools)"——注册后变 17,建议同步把注释改为"(17 tools)"(cosmetic,非阻塞,但否则注释会与代码漂移)
- orchestrator_context.py:188/236 文本已列 ✓ / generation_scheduler.py:237 stagnation 注入 ✓
- 插入点 L78(run_direction_audit 之后)真实存在 ✓,保持 analytical pipeline 分组合理

2) 计划一处措辞需澄清:expected_set=17 是"注册后"的正确值(当前 16 + 1)。计划措辞易误读为"当前应为 17",实际 test_mcp_tools_expected_set 应在改动之后断言 17。test_mcp_tools_includes_literature_probe 和 no_duplicates 不受影响。

3) test_mcp_tools_expected_set 是 brittle regression guard——以后再加任一工具(如将来注册新的 code-layer tool 到 mcp_tools)会迫使维护者改这


**测试缺口：** 测试基本充分但有 2 点注意:(1) test_mcp_tools_expected_set 断言严格 17 个工具——这是 brittle regression guard,未来再加任何工具(比如再注册一个 code-layer tool 到 mcp_tools)就会误报失败。建议改为断言"包含必要子集 + 不含意外工具"而非严格数量相等,或至少在测试注释里标注 17 这个数字的来源。非 blocking。(2) 计划没覆盖 all_tools 自动覆盖的测试——建议补一个 test_all_tools_includes_literature_probe 以确认 HTTP 端点也自动暴露(确认 backward-compat 链路),但这非必需(verify = 改完后 python 验证已确认 all_tools 自动含)。现有 3 个测试抓住了核心行为(存在性/无重复/集合一致性),

### fix-11-measurement-plan-cleanup [MEDIUM|both|low] ✅ SAFE

**删 measurement_plan prompt 谎言 + spot_analyzer 死赋值（止血，闭环 defer fix-2）**

止血级修复（low effort）：删 master_prompt.md:174-175 的"predicted vs actual 写入 RECENT_LESSONS / CREDIT ASSIGNMENT telemetry"承诺句（代码从未兑现），改为诚实表述"measurement_plan 是 Master 自评估记录，当前不回流，闭环见 fix-2"；同步删 spot_analyzer.py:336 的纯死赋值 `expected_changes = master_plan.get(...)`（函数体内零引用）。output_schema.py 的两个 str 字段经核查后决定保留（删除会破坏 test fixture + 历史 plan JSON 向后兼容），仅加注释说明当前不被消费。闭环 (b)（rating_delta 异步回填后把 Master 预测 vs daemon 实际写入 experience_pool RECENT_LESSONS）依赖 fix-2，不在本 fix 实施。本 fix 纯消除 prompt 谎言 + 死代码，零行为变化、零回归风险。


<details><summary>当前代码状态（核查后）</summary>

```
已实际核查（非复述审计）：

1. **master_prompt.md:170-176** `<measurement_plan>` 段确认存在承诺谎言语句。L174-175 字面：`After commit, "predicted vs actual" delta is logged to experience_pool RECENT_LESSONS.` / `This is CREDIT ASSIGNMENT telemetry — does NOT block commit. Use it to learn what works.` —— 承诺代码从未兑现。

2. **output_schema.py:15-21** MasterPlan 类含 `expected_behavior_change: str = ""`（L18）+ `measurement_plan: str = ""`（L20），均为可选 default='' 字段，schema 无任何 constraint（审计称"自由 str / schema 类型谎言"准确）。grep 全 web/core/ 下这两个字段 **无任何 Python 消费点**（仅 schema 定义处 + spot_analyzer.py:336 死赋值 + prompts 文本）。

3. **spot_analyzer.py:330-338** verify_behavior 函数体内 L336 `expected_changes = master_plan.get("expected_behavior_change", {})` —— grep `expected_changes` 在该文件仅此一处，后续 L338-387 循环从未引用，是 **纯死赋值**（审计准确）。注意：master_plan 参数本身仍由 caller run_spot_check 传入，但只有 scenario 级的 `expected_behavior`（L339）被用，master_plan 顶层字段无人读。

4. **tool_gates.py:936-976** `run_spot_check` 注册为 MCP tool，调用 verify_behavior，但 grep prompts/orchestrator.md 无 spot_check 引用——是 **孤立未挂载 tool**（确认 spot_analyzer 实质死代码路径，但本 fix 不删 tool，仅清死赋值）。

5. **"Known Mandatory Fixes" 段** master_prompt.md L209-225 实测 6663 字符（审计 #13e 量级准确），是 M5/M6 已验证 telemetry gate 的承载体——*
```
</details>


#### 改动点（3 处）

- **`web/core/prompts/master_prompt.md`** @ `L170-176 (<measurement_plan> 段)` [modify] — L174-175 的承诺（"After commit, predicted vs actual delta is logged to experience_pool RECENT_LESSONS. This is CREDIT ASSIGNMENT telemetry"）是 prompt 谎言——代码层从未解析 measurement_plan/expect
- **`web/core/spot_analyzer.py`** @ `L330-338 (verify_behavior 函数体)` [delete] — L336 `expected_changes = master_plan.get("expected_behavior_change", {})` 是纯死赋值——grep 整个函数体 `expected_changes` 仅此一处，后续 L338-387 循环从不引用它。删除该行消除死代码；在 docstring 中说明 master_plan 仅为 API
- **`web/core/output_schema.py`** @ `L15-20 (MasterPlan class) — 评估后决定: 不删` [modify] — 审计原文把这两个字段称为 'schema 类型谎言'，但实际删除会破坏向后兼容：(1) 历史 master plan JSON 普遍含这俩 key；(2) test_master_success_return.py:25,27 的 VALID_PLAN fixture 含这俩 key 且 schema 校验通过是 test 前提。它们是可选 default=

<details><summary>完整 diff 详情</summary>


**`web/core/prompts/master_prompt.md`** @ `L170-176 (<measurement_plan> 段)` [modify]

现状:
```python
<measurement_plan> For each worker task, state expected impact: - Target opponent + expected WR delta (e.g. "vs v47: 50%→53%, ≥30 mirror pairs") - Statistic that will confirm (paired net-chips CI lower bound > 0) After commit, "predicted vs actual" delta is logged to experience_pool RECENT_LESSONS. This is CREDIT ASSIGNMENT telemetry — does NOT block commit. Use it to learn what works. </measurement_plan>
```
改后:
```python
<measurement_plan> For each worker task, state expected impact (self-evaluation record only — NOT parsed or reconciled by any pipeline stage today): - Target opponent + expected WR delta (e.g. "vs v47: 50%→53%, ≥30 mirror pairs") - Statistic that will confirm (paired net-chips CI lower bound > 0) NOTE: this prediction is currently NOT compared against the actual post-commit rating delta. Rating delta is not known at commit time (new bot's rd is still 350; see the critic-calibration [...]
```
*L174-175 的承诺（"After commit, predicted vs actual delta is logged to experience_pool RECENT_LESSONS. This is CREDIT ASSIGNMENT telemetry"）是 prompt 谎言——代码层从未解析 measurement_plan/expected_behavior_change、从未把预测与实际比对（grep web/core 下零 Python 消费点）。改为诚实表述，保留 Master 自评估习惯（鼓励 Master 写明 target opponent + 预期 delta 仍有认知价值），但明确标注当前不回流，避免审计/调试时误以为闭环已存在。*


**`web/core/spot_analyzer.py`** @ `L330-338 (verify_behavior 函数体)` [delete]

现状:
```python
def verify_behavior(master_plan: dict, scenarios: list, actual_actions: list) -> dict: """Compare actual bot actions against expected behavior from the master plan.""" issues = [] passed_count = 0 total = len(scenarios) expected_changes = master_plan.get("expected_behavior_change", {}) for scenario, actual in zip(scenarios, actual_actions):
```
改后:
```python
def verify_behavior(master_plan: dict, scenarios: list, actual_actions: list) -> dict: """Compare actual bot actions against expected behavior from the master plan. Note: the master_plan dict is accepted for API symmetry but only the per-scenario `expected_behavior` field is currently used for comparison; `expected_behavior_change`/`measurement_plan` are not reconciled here. """ issues = [] passed_count = 0 total = len(scenarios) for scenario, actual in zip(scenarios, actual_actions):
```
*L336 `expected_changes = master_plan.get("expected_behavior_change", {})` 是纯死赋值——grep 整个函数体 `expected_changes` 仅此一处，后续 L338-387 循环从不引用它。删除该行消除死代码；在 docstring 中说明 master_plan 仅为 API 对称保留、当前只消费 scenario 级 expected_behavior，避免未来读者误以为函数在做 master_plan 级 reconcile。注意 master_plan 参数保留（caller run_spot_check 仍传入），仅删未用赋值，签名不变。*


**`web/core/output_schema.py`** @ `L15-20 (MasterPlan class) — 评估后决定: 不删` [modify]

现状:
```python
class MasterPlan(BaseModel): analysis: str = Field(min_length=10) targeted_failure: str = Field(min_length=5) expected_behavior_change: str = "" do_not_touch: list[str] = [] measurement_plan: str = ""
```
改后:
```python
class MasterPlan(BaseModel): analysis: str = Field(min_length=10) targeted_failure: str = Field(min_length=5) # expected_behavior_change / measurement_plan: free-form self-evaluation # records emitted by the Master. Currently NOT parsed or reconciled by any # pipeline stage (predicted-vs-actual credit-assignment loop is planned, not # wired). Kept as optional str so existing plan history JSON and test # fixtures carrying these keys still validate. expected_behavior_change: str = "" [...]
```
*审计原文把这两个字段称为 'schema 类型谎言'，但实际删除会破坏向后兼容：(1) 历史 master plan JSON 普遍含这俩 key；(2) test_master_success_return.py:25,27 的 VALID_PLAN fixture 含这俩 key 且 schema 校验通过是 test 前提。它们是可选 default='' 字段，保留无害。止血只需改 prompt 措辞（改动1）+ 删死赋值（改动2）。这里仅在 schema 加注释说明字段当前不被消费，纠正未来误判。*

</details>


#### 测试（2 个）

- `web/tests/test_master_success_return.py::test_name` — 护栏测试：确保保留 schema 字段 + 改 prompt 措辞后，含 measurement_plan/expected_behavior_change key 的 Master plan 仍通过 validate_agent_outp
- `web/tests/test_logic_spot_analyzer.py::test_name` — 新增（若文件不存在则新建）：verify_behavior 在 master_plan 含/不含 expected_behavior_change、measurement_plan 为不同值（含 None / 非法类型）时返回结果一致，证明

#### 向后兼容
无需同步改调用方。改动 1 是 prompt 文本，无签名/返回值变化。改动 2 删除的是函数体内局部变量赋值，verify_behavior 签名与返回 dict 结构完全不变。改动 3（defer to fix-2）目前无实施。schema 字段保留，历史 plan JSON + test fixture 不受影响。


#### 依赖：fix-2-rating-delta-reconcile


#### 风险
- prompt 行为漂移：删承诺句后 Master 可能减少写 measurement_plan 的认真程度（既然不回流）。mitigation：新措辞仍明确鼓励写 target_opponent + 预期 delta 作为自评估记录，保留认知价值；且 (b) 闭环落地后会重新强化该字段价值。
- spot_analyzer docstring 改动若后续 fix-2 真建闭环时忘记更新，docstring 会再次变成谎言。mitigation：docstring 明确写 'currently NOT reconciled' + 在 experience_pool RECENT_LESSONS
- 误删 schema 字段的风险（若 implementer 过度执行审计原文 '删 measurement_plan'）。mitigation：本计划明确决定保留 schema 字段并附注释，backward_compat 段说明理由；test_master_success_return.py fi
- run_spot_check 孤立 tool 仍存在——本 fix 只清死赋值未删 tool，可能被视为不彻底。mitigation：删 tool 属独立清理（影响 MCP tool 列表/前端 tool list），不在 fix-11 范围；spot_analyzer 作为可选行为校验能力保留有价


#### 验证步骤
```
grep -n 'CREDIT ASSIGNMENT\|predicted vs actual' web/core/prompts/master_prompt.md —— 应无输出（承诺句已删）
grep -n 'expected_changes' web/core/spot_analyzer.py —— 应无输出（死赋值已删）
grep -rn 'measurement_plan\|expected_behavior_change' web/core/ --include='*.py' | grep -v __pycache__ —— 确认仅剩 output_schema.py 定义处 + 注释，无新增消费点（除非 fix-2 已落地）
cd web && python -m pytest tests/test_master_success_return.py -v —— VALID_PLAN fixture 含 measurement_plan key，schema 仍接受（护栏）
cd web && python -m pytest tests/test_logic_spot_analyzer.py tests/test_mcp_*.py -v —— 若新增 spot_analyzer test，验证 verify_behavior 对 master_plan 顶层字段无依赖
cd web && python -m pytest tests/ -q —— 零回归（当前 1037 passed，应保持）
```


#### 回滚
改动 1：git revert 对应 commit，恢复 master_prompt.md:174-175 原文。改动 2：恢复 spot_analyzer.py:336 `expected_changes = master_plan.get(...)` 行。改动 3：删 output_schema.py L18/L20 间新增的注释块。三处均为纯文本/注释级改动，无数据迁移，回滚零风险。若 (b) 闭环已落地则回滚需连带（但 b 独立于本 commit）。


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：✅
- 测试充分：✅

**遗漏调用方：** 无遗漏。verify_behavior 仅一处 caller (tool_gates.py:964)，签名与返回 dict 结构均不变。expected_changes 是函数内局部死变量，无其他引用。schema 字段保留，无调用方需同步改。


**修正要点：** 核查全部通过，plan 可直接落地。亲自核查结论：(1) master_prompt.md L170-176 确认存在，L174-175 是字面承诺谎言(“After commit, predicted vs actual delta is logged to experience_pool RECENT_LESSONS”+“CREDIT ASSIGNMENT telemetry”)，代码层从未兑现——grep web/core/*.py 下 measurement_plan/expected_behavior_change 仅 2 个 Python 命中(schema 定义 output_schema.py:18,20 + 死赋值 spot_analyzer.py:336)，零真实消费点，tool_planning.py 解析 master plan 后只读 tasks/targeted_failure/worker_id 等从不读这两个字段。(2) spot_analyzer.py:336 `expected_changes = master_plan.get("expected_behavior_change", {})` 是纯死赋值——该变量在整个文件仅此一处，verify_behavior 函数体 L338-387 循环只读 scenario 级 expected_behavior(L339)，master_plan 顶层字段无人读；caller run_spot_check(tool_gates.py:964)仍传 master_plan 参数但签名/返回不变。grep 全 web 树确认 verify_behavior/expected_changes 无任何其他引用。(3) schema 字段保留决定正确：实测 validate_agent_output('


**测试缺口：** 测试基本充分。护栏测试1(test_master_plan_with_measurement_plan_still_validates)有现成 fixture 可直接扩展，覆盖 backward-compat。护栏测试2(test_verify_behavior_ignores_master_plan_toplevel_fields)需新建文件 test_logic_spot_analyzer.py——这正好补上当前 spot_analyzer.py 零单元测试的空白，合理。建议补充一点：新测试除断言 master_plan 顶层字段无关外，应同时断言 actual_actions 含 error(scenario 走 crash 分支)与正常 action 两类路径，确保删死赋值后 verify_behavior 的 issues/passed_count 计数逻辑不回归(当前无任何测试

### fix-13-deadcode-cleanup [LOW|architecture|low] ✅ SAFE

**fix-13 死代码/不一致清理批次 (13a-13e) 实施计划**

fix-13是一批低风险高可读性的代码卫生修复：(1) 删除tool_planning.py中generation_attempt永不自增形成的require_new_plan死代码防护，并清理orchestrator.md中critic advisory与retry-after-rejection的矛盾措辞；(2) 修正generation_scheduler.py中pipeline.prepare→pipeline.prepare_done的错别字，使source-loop/oscillation检测器从永久死代码恢复为活代码；(3) 将master_prompt.md中worker prompt长度的MUST下限6000改为SHOULD target 6000 (soft); hard 12000，与_validate_master_plan的12000硬限制一致；(4) 13d DeepEvolve伪代码不做——加hard gate增加Master失败率，依赖reviewer/critic soft gate即可；(5) 更新tool_gates.py的@tool docstring，删除过时的score≥6=approved，改为ADVISORY ONLY。所有改动后向兼容，无调用方需同步修改。


<details><summary>当前代码状态（核查后）</summary>

```
已逐项核查（以下为关键file:line，全部经grep/read验证）：

13a:
- tool_planning.py:1441 `generation_attempt = ckpt.get("generation_attempt", 0)`
- tool_planning.py:1442 `if reviewer_feedback and generation_attempt >= 1:` — 死代码，condition永不满足
- tool_gates.py:582 `# No generation_attempt increment` (注释明确禁止increment)
- tool_gates.py:594 `generation_attempt=ckpt.get("generation_attempt", 0),  # do NOT increment`
- evolution_infra.py:360 `if reset_generation_attempt: existing_generation_attempt = 0` (run_master调用时清零)
- orchestrator.md:78 `Critic score is ADVISORY ONLY: it does NOT block and does NOT force retry. ALWAYS proceed`
- orchestrator.md:97 `When retrying workers after critic rejection, pass the critic's feedback` — 与78矛盾

13b:
- generation_scheduler.py:531 `evt.get("type") == "pipeline.prepare"` — 错别字
- tool_gates.py:502 `log_system_event("pipeline.prepare_done", ...)` — 生产者实际发布prepare_done
- generation_scheduler.py:424 `_source_loop = _detect_source_loop(n=3)` — 活调用
- generation_scheduler.py:438 `oscillating = _detect_source_oscillation(n=8, max_unique=3)` — 活调用

13c:
- master_prompt.md:83 `Each worker_prompt MUST be under 6000 characters.`
- tool_planning.py:278 `if len(prompt) > 12000: errors.ap
```
</details>


#### 改动点（5 处）

- **`web/core/tool_planning.py`** @ `lines 1439-1448 (entire block, inclusive)` [delete] — generation_attempt永不自增（全仓无increment点+run_master强制reset+test_llm_infra_error.py:194断言NOT incremented），条件>=1永不满足。注释和错误消息与orchestrator.md:78的advisory设计矛盾。无任何测试断言require_new_plan:True。
- **`web/core/prompts/orchestrator.md`** @ `line 97` [modify] — 与第78行矛盾：第78行说critic does NOT force retry，第97行说after critic rejection。改为after reviewer rejection or quality-gate failure，反映真实代码路径，并补充括号说明critic feedback的真正去向。
- **`web/core/generation_scheduler.py`** @ `line 531` [modify] — 生产者tool_gates.py:502发布的是pipeline.prepare_done，消费者过滤pipeline.prepare导致字符串不匹配，_detect_source_loop/_detect_source_oscillation成为永久死代码。修复后两个检测器在source_v真正重复时按设计生效。
- **`web/core/prompts/master_prompt.md`** @ `line 83` [modify] — MUST be under 6000与_validate_master_plan的12000硬限制不符。实质是tiered设计（6000=inline软目标配.task_context溢出;12000=硬上限），MUST措辞使LLM过度压缩。改为SHOULD target 6000 (soft); hard limit 12000准确反映代码行为。
- **`web/core/tool_gates.py`** @ `line 709` [modify] — score>=6=approved是过时描述（tool_gates.py:798无条件approved=True）。新docstring准确反映当前设计：critic advisory，precommit是最终门控。orchestrator.md:68已是一致的（审计误判为第59行），无需改动。

<details><summary>完整 diff 详情</summary>


**`web/core/tool_planning.py`** @ `lines 1439-1448 (entire block, inclusive)` [delete]

现状:
```python
# When critic has rejected, force re-planning on 2nd+ rejection. # Re-using the same plan that the critic already rejected guarantees repeated failure. generation_attempt = ckpt.get("generation_attempt", 0) if reviewer_feedback and generation_attempt >= 1: return _json_tool_result({ "error": f"generation_attempt={generation_attempt}. The critic rejected the plan {generation_attempt} time(s). " f"You MUST call run_master first to generate a NEW plan incorporating the critic feedback, " [...]
```
改后:
```python
[block deleted — next code block '# When retrying after workers already ran...' at line ~1450 remains unchanged]
```
*generation_attempt永不自增（全仓无increment点+run_master强制reset+test_llm_infra_error.py:194断言NOT incremented），条件>=1永不满足。注释和错误消息与orchestrator.md:78的advisory设计矛盾。无任何测试断言require_new_plan:True。*


**`web/core/prompts/orchestrator.md`** @ `line 97` [modify]

现状:
```python
- When retrying workers after critic rejection, pass the critic's `feedback` field **verbatim** as `reviewer_feedback` — do NOT paraphrase or summarize
```
改后:
```python
- When retrying workers after reviewer rejection or quality-gate failure, pass the feedback field **verbatim** as `reviewer_feedback` — do NOT paraphrase or summarize. (Critic feedback is advisory-only; it is injected as improvement hints into the NEXT generation, not as `reviewer_feedback`.)
```
*与第78行矛盾：第78行说critic does NOT force retry，第97行说after critic rejection。改为after reviewer rejection or quality-gate failure，反映真实代码路径，并补充括号说明critic feedback的真正去向。*


**`web/core/generation_scheduler.py`** @ `line 531` [modify]

现状:
```python
if evt.get("type") == "pipeline.prepare":
```
改后:
```python
if evt.get("type") == "pipeline.prepare_done":
```
*生产者tool_gates.py:502发布的是pipeline.prepare_done，消费者过滤pipeline.prepare导致字符串不匹配，_detect_source_loop/_detect_source_oscillation成为永久死代码。修复后两个检测器在source_v真正重复时按设计生效。*


**`web/core/prompts/master_prompt.md`** @ `line 83` [modify]

现状:
```python
Each `worker_prompt` MUST be under 6000 characters. For longer rationale,
```
改后:
```python
Each `worker_prompt` SHOULD target 6000 characters (soft limit); the hard limit is 12000. For longer rationale,
```
*MUST be under 6000与_validate_master_plan的12000硬限制不符。实质是tiered设计（6000=inline软目标配.task_context溢出;12000=硬上限），MUST措辞使LLM过度压缩。改为SHOULD target 6000 (soft); hard limit 12000准确反映代码行为。*


**`web/core/tool_gates.py`** @ `line 709` [modify]

现状:
```python
@tool("run_critic", "Run Poker Strategy Critic on bot changes. Returns score 1-10 and strategic feedback. score ≥ 6 = approved.", {"version": int, "source_v": int, "plan": list, "reviewer_feedback": str, "force_advance": bool})
```
改后:
```python
@tool("run_critic", "Run Poker Strategy Critic on bot changes. Returns score 1-10 and strategic feedback. ADVISORY ONLY: precommit is the final regression gate; score does NOT block the pipeline.", {"version": int, "source_v": int, "plan": list, "reviewer_feedback": str, "force_advance": bool})
```
*score>=6=approved是过时描述（tool_gates.py:798无条件approved=True）。新docstring准确反映当前设计：critic advisory，precommit是最终门控。orchestrator.md:68已是一致的（审计误判为第59行），无需改动。*

</details>


#### 测试（2 个）

- `web/tests/test_mcp_pipeline.py (或追加到test_logic_helpers.py)::test_name` — 验证删除dead guard后，execute_workers在generation_attempt=0且有reviewer_feedback的情况下不返回require_new_plan错误，直接进入后续code block（code r
- `web/tests/test_logic_event_bus.py (或新建)::test_name` — 验证修复typo后_read_source_v_history()正确匹配pipeline.prepare_done事件（返回[100]），而忽略错误类型的pipeline.prepare事件（不返回[200]）。

#### 向后兼容
不需要同步改动调用方。13a: 删除的代码块从未执行（generation_attempt恒为0），无消费者依赖返回结构。13b: _read_source_v_history是私有函数，返回值格式不变。13c/13e: 纯prompt/docstring文本修改，不改API签名或返回值。所有改动均为后向兼容。


#### 风险
- 13a风险：如未来有人添加generation_attempt自增路径，必须同时在execute_workers恢复guard（概率极低，当前全仓无increment点）。缓解：在代码中保留注释说明设计意图。
- 13b风险：_detect_source_loop生效后，pool饱和时可能意外触发强制source切换（当前单调递增序列下不触发）。缓解：触发条件严格（连续3代source_v相同），触发后切换到leader是安全设计。
- 13c风险：Master放松后可能产出更长worker_prompt（6000→12000上限）。缓解：12000硬限制仍在；Worker模型context window远大于此。
- 13d风险（不做）：如未来plan质量下降（不附skeleton），可能需加可选字段。缓解：观察数据，非本fix范围。


#### 验证步骤
```
python -m py_compile web/core/tool_planning.py && python -m py_compile web/core/generation_scheduler.py && python -m py_compile web/core/tool_gates.py
grep -n 'require_new_plan' web/core/tool_planning.py  # 应返回0行（guard已删除）
grep 'pipeline.prepare' web/core/generation_scheduler.py  # 应显示pipeline.prepare_done而非pipeline.prepare
grep 'MUST be under 6000' web/core/prompts/master_prompt.md  # 应返回0行（已改为SHOULD target 6000）
grep 'score.*6.*approved' web/core/tool_gates.py  # 应返回0行（旧字符串已替换）
cd web && python -m pytest tests/ -v --timeout=120  # 预期1037 passed，零回归
```


#### 回滚
所有5个改动均为纯文本/文档修改，git revert即可全部回滚。无数据迁移、无schema变更、无API变更。最低风险回滚点：如果13b修复后_detect_source_loop意外触发，可单独revert该行（一行改动），其余保留。


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：✅
- 测试充分：❌

**修正要点：** 核查结论:plan 可直接落地,无阻断性问题。逐项亲自 Read/Grep 验证如下(全部 file:line 核实):

13a [CONFIRMED DEAD CODE,SAFE TO DELETE]: generation_attempt 永不自增。全仓 grep 写路径只有:(1) generation_attempt=0 默认;(2) generation_attempt=ckpt.get(...)原地保留(tool_gates.py:594/765/830, orchestrator.py:483);(3) evolution_infra.py:343 existing保留;(4) run_master tool_planning.py:892 reset_generation_attempt=True 强制清零。无任何 +1 路径。tool_planning.py:1441-1451 guard `generation_attempt>=1` 永远 False。require_new_plan 仅此一处出现(grep 确认),无消费者,无测试断言。
  ⚠️ plan 行号偏差:plan 说 "delete lines 1439-1448 (inclusive)",实际完整块是 **1439-1451**(2行注释 + guard + return dict 含 next_v/source_v/收尾])。执行者若按 1448 截断会留下悬空 3 行(1449 next_v/1450 source_v/1451 ])导致语法错误。修正:删除 1439-1451 整块。tool_planning.py:1453 起的 stage-in 重置块(reviewer_feedback and ckpt.get('stage') in ('workers_done','r


**测试缺口：** 1) 13a 计划测试用 generation_attempt=0,无法真正 catch 删除(删除前后行为相同,因 guard 永不触发)。需加 attempt=1+reviewer_feedback set 的负向测试,断言 execute_workers 不再返回 require_new_plan——这才是能 catch 该 fix 的测试。test_pipeline_stages.py:_setup_checkpoint(:710) 不写 generation_attempt/reviewer_feedback,需扩展。2) 13b 修复后 _detect_source_loop/_detect_source_oscillation 从永久死代码变为活代码,但计划只测 _read_source_v_history(typo 层),未直接测两检测器的"重新生效"行为(如 monkey

### fix-10-battle-experience-dedup [HIGH|architecture|medium] ⚠️ 需修正

**battle_experience 内容哈希去重 + 空 batch LLM 跳过**

battle_experience 后台 LLM 循环占 44% 总成本，核心浪费是同 pair 多场重复触发 LLM merge。通过 pair-fingerprint 去重（canonical pair + mirror-normalized result bucket，LIMIT=3）在 get_unanalyzed_matches 过滤层跳过高重复 matches，同时显式化空 batch 不计 LLM rate-limit，预计降至 ~60-70% 原调用量。子项 (c) retire 逻辑明确移交 fix-12。


<details><summary>当前代码状态（核查后）</summary>

```
battle_experience.py:54 POLL_INTERVAL=20s, :55 TARGET_BATCH=6, :57 MAX_ANALYSES_PER_HOUR=240(实测远低于上限), :466 _run_llm_update 每 batch 1 次 LLM merge; :353 _apply_batch_results 内 `if not summaries: return` 已阻止空 merge LLM 调用; marker schema(:130 mark_analyzed) 当前 `{fail_count: int}`, 无 fingerprint 记录; llm_costs.jsonl 实测 battle_experience = $29.97/44.0%/194 calls; match_history.jsonl 674 matches, 491 unique pairs, 同(pair+result-bucket) 重复仅 5.3%, whole-pair 重复 27.2%; battle_experience.md 38 行(健康), 无 META 策略内容; TEST: 16 tests in test_logic_battle_experience.py + 1 test in test_rc4_battle_exp_telemetry.py(全部 PASS)。
```
</details>


#### 改动点（5 处）

- **`web/core/battle_experience.py`** @ `line 57-58 (constants block)` [add] — Pair-fingerprint 常量 + 函数: 将 match entry 转化为可重复、mirror-对称的 canonical 字符串。LIMIT=3 同 pair 同 bucket 已分析 3 场才 skip，保留 incremental 价值。bucket 归一化消除 bot0/bot1 视角差异。
- **`web/core/battle_experience.py`** @ `line 130-139 (mark_analyzed)` [modify] — marker schema 扩展: `{fail_count}` → `{fail_count, fp_count: {fingerprint: count}}`。fp_count 记录 per-pair-per-bucket 的累积分析次数，向后兼容(旧 marker 无 fp_count → get() default {})。fingerprint 可
- **`web/core/battle_experience.py`** @ `line 196-199 (filter loop in get_unanalyzed_matches)` [modify] — get_unanalyzed_matches 现有 3 层过滤(done/poison/evicted)后新增第 4 层: pair-fingerprint dedup。扫描全部 markers 的 fp_count 累计该 fingerprint 的历史分析次数，≥LIMIT 则直接 mark analyzed 跳过。fingerprint 在 entry
- **`web/core/battle_experience.py`** @ `line 304-306 (end of _experience_loop batch)` [modify] — _apply_batch_results 内 `if not summaries: return` 已挡住空 merge 的 LLM 调用，但 _analyses_this_hour 计数在外部：全 dedup 时 _analyses_this_hour 不应增加，否则 rate-limit 误触发。加上 `results and` 保护避免空 batch 
- **`web/core/prompts/battle_experience_update.md`** @ `rule 3 (line 13) after existing rule 3` [modify] — battle_experience.md:14 实证同一观察 66 代无 retire 地重复合并（audit #12 引用）。规则 3b 是轻量软约束，不引入 retire 逻辑（retire 归 fix-12），仅抑制 LLM 累积冗余文本。

<details><summary>完整 diff 详情</summary>


**`web/core/battle_experience.py`** @ `line 57-58 (constants block)` [add]

现状:
```python
MAX_ANALYSES_PER_HOUR = 240 # rate-limit defense (non-zero budget ~$5/hr) LLM_TIMEOUT = 300 # P2: was 120
```
改后:
```python
MAX_ANALYSES_PER_HOUR = 240 # rate-limit defense (non-zero budget ~$5/hr) LLM_TIMEOUT = 300 # P2: was 120 # --- pair dedup constants --- DEDUP_PAIR_SAME_RESULT_LIMIT = 3 # skip after N analyses of same (pair, result-bucket) def _pair_fingerprint(entry: dict) -> str: """Canonical pair fingerprint: (sorted pair) + normalized result-bucket. Result normalization: for mirror battles bot0/bot1 are interchangeable, so we take (max(W0,W1), max(D0,D1), max(L0,L1)) as the canonical bucket. This [...]
```
*Pair-fingerprint 常量 + 函数: 将 match entry 转化为可重复、mirror-对称的 canonical 字符串。LIMIT=3 同 pair 同 bucket 已分析 3 场才 skip，保留 incremental 价值。bucket 归一化消除 bot0/bot1 视角差异。*


**`web/core/battle_experience.py`** @ `line 130-139 (mark_analyzed)` [modify]

现状:
```python
def mark_analyzed(match_id: str, *, fail_count: int = 0): """Record a match ID as analyzed (done). Atomic read-merge-write under lock. Args: match_id: the match ID to mark. fail_count: 0 = successfully analyzed (done); >=3 = force-skipped poison. """ markers = _read_markers() markers[match_id] = {"fail_count": fail_count} _write_markers(markers)
```
改后:
```python
def mark_analyzed(match_id: str, *, fail_count: int = 0, fingerprint: str | None = None): """Record a match ID as analyzed (done). Atomic read-merge-write under lock. Args: match_id: the match ID to mark. fail_count: 0 = successfully analyzed (done); >=3 = force-skipped poison. fingerprint: optional canonical fingerprint string (from _pair_fingerprint); when provided AND fail_count==0, the per-fingerprint counter is bumped so that subsequent matches with the same fingerprint can be dedup- [...]
```
*marker schema 扩展: `{fail_count}` → `{fail_count, fp_count: {fingerprint: count}}`。fp_count 记录 per-pair-per-bucket 的累积分析次数，向后兼容(旧 marker 无 fp_count → get() default {})。fingerprint 可选参不破坏现有调用点。*


**`web/core/battle_experience.py`** @ `line 196-199 (filter loop in get_unanalyzed_matches)` [modify]

现状:
```python
# Skip if replay file has been evicted replay_path = REPLAY_DIR / match_id if not replay_path.exists(): continue candidates.append(entry)
```
改后:
```python
# Skip if replay file has been evicted replay_path = REPLAY_DIR / match_id if not replay_path.exists(): continue # --- pair-fingerprint dedup: skip if same (pair, result-bucket) already # analyzed >= DEDUP_PAIR_SAME_RESULT_LIMIT times. We check ALL markers # (not just this match_id's own) to accumulate across different match IDs # of the same pair. ---------------------------------------------------- fp = _pair_fingerprint(entry) if fp: total_fp_count = 0 for m in markers.values(): if [...]
```
*get_unanalyzed_matches 现有 3 层过滤(done/poison/evicted)后新增第 4 层: pair-fingerprint dedup。扫描全部 markers 的 fp_count 累计该 fingerprint 的历史分析次数，≥LIMIT 则直接 mark analyzed 跳过。fingerprint 在 entry 阶段即可计算(只需 bot0/bot1/wins/draws)，不需要读 replay。注意: 新 mark_analyzed(fail_count=0, fingerprint=None) 时传 fingerprint=None 表示这个 skip 不计入 fp_count 避免循环。*


**`web/core/battle_experience.py`** @ `line 304-306 (end of _experience_loop batch)` [modify]

现状:
```python
# One LLM merge per cycle when any summaries were collected. if any(r[2] for r in results): _analyses_this_hour += 1
```
改后:
```python
# Only count as an LLM analysis if at least one summary was # produced. When all matches in the batch were dedup-skipped # (no summaries), no LLM call was made in _apply_batch_results. # (b) Incremental merge: skip LLM entirely when batch is all-dedup. if results and any(r[2] for r in results): _analyses_this_hour += 1
```
*_apply_batch_results 内 `if not summaries: return` 已挡住空 merge 的 LLM 调用，但 _analyses_this_hour 计数在外部：全 dedup 时 _analyses_this_hour 不应增加，否则 rate-limit 误触发。加上 `results and` 保护避免空 batch 计数。*


**`web/core/prompts/battle_experience_update.md`** @ `rule 3 (line 13) after existing rule 3` [modify]

现状:
```python
3. Remove observations that have been contradicted by 3 or more newer matches. Mark them for removal only when the contradiction count is explicit in the evidence.
```
改后:
```python
3. Remove observations that have been contradicted by 3 or more newer matches. Mark them for removal only when the contradiction count is explicit in the evidence. 3b. Avoid unbounded accumulation: if an observation already has strong evidence (>= 5 match confirmations) and new data does not change the conclusion, update only the match count — do NOT re-list all contributing pairs or re-explain the pattern.
```
*battle_experience.md:14 实证同一观察 66 代无 retire 地重复合并（audit #12 引用）。规则 3b 是轻量软约束，不引入 retire 逻辑（retire 归 fix-12），仅抑制 LLM 累积冗余文本。*

</details>


#### 测试（3 个）

- `web/tests/test_logic_battle_experience.py::test_name` — 同 pair 同 result-bucket 写入 4 场 match (LIMIT=3)，get_unanalyzed_matches 不返回第 4 场，且该 match 被自动 mark_analyzed（fail_count=0）。同
- `web/tests/test_logic_battle_experience.py::test_name` — 同 pair 但不同 result bucket（10/10/0 vs 14/6/0）的两场 match 都在 get_unanalyzed_matches 中返回，不受 dedup 影响。验证 mirror-对称归一化（10/10 和 1
- `web/tests/test_logic_battle_experience.py::test_name` — 当所有 matches 被 dedup 跳过（batch 内无 summary）时，_run_llm_update 不被调用，_analyses_this_hour 不增加。用 monkeypatch 在 _run_llm_update 上

#### 向后兼容
marker schema 扩展是向后兼容的(mark_analyzed 新增可选 fingerprint=None, _read_markers 对旧 dict 无 fp_count 当 default {}处理)。mark_analyzed 是公开 API(测试直接调用), 新增可选参不破坏现有调用。get_unanalyzed_matches 返回签名不变(仍是 list[dict])。_experience_loop/_apply_batch_results 内部逻辑改(无公开 API 变化)。唯一需同步: get_unanalyzed_matches 内部依赖新的 marker 结构, 但它是唯一 reader, 不需外部同步。


#### 风险
- 过度去重(whole-pair skip)丢失 incremental 信号: 已缓解 → 设 DEDUP_PAIR_SAME_RESULT_LIMIT=3(非 whole-pair), 同 pair 不同 result-bucket 的 match 仍分析, 同 pair 同 bucket 前 3
- marker 文件 fp_count 膨胀: 最坏 491 unique pair × N result-buckets, 每 bucket ≤5 条目, 实测 bucket 数极少(10/10 主导)→ 每 pair 1-2 bucket × 491 = <1000 keys, 可控。旧 mark
- 去重遍历全部 markers 计算 total_fp_count 有 O(markers) 开销: markers 已 1096 条目, 每 entry fp_count dict 很小(1-2 keys), 遍历常数时间; 但 per-batch 6 matches × 1096 entries 
- DEDUP_PAIR_SAME_RESULT_LIMIT=3 的初值是否合适需实验验证: 若过高则省不够, 若过低则丢信号。LIMIT=3 是保守起点(同 bucket 前 3 场分析后, 第 4 场起 skip)。生产验证: daemon 运行 1h 后 llm_costs.jsonl 中 bat


#### 验证步骤
```
cd web && python -m pytest tests/test_logic_battle_experience.py -v — 新增 3 个测试 + 既有 16 个全 pass
cd web && python -m pytest tests/ -q — 零回归，1037 pass 不变
生产验证: daemon 运行 ≥1h 后 python3 -c "import json; from collections import defaultdict; r=defaultdict(int); [r.update({json.loads(l).get('role','?'): r[json.loads(l).get('role','?')]+1}) for l in open('web/core/results/llm_costs.jsonl') if l.strip()]; print(r.get('battle_experience',0))" — battle_experience 调用数应下降 ~15-25%
```


#### 回滚
marker schema fp_count 是 additive extension(新 marker 读回时 fp_count default {} 处理)。若需回滚: 删除 _pair_fingerprint 函数、get_unanalyzed_matches 的 dedup 过滤块、_experience_loop 的 `results and` 条件、mark_analyzed 的 fingerprint 参; prompt rule 3b 删除。回滚后 system 行为与改动前完全一致(已分析的 marker fp_count 被忽略)。唯一不可逆: 已 skip 的 match


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：❌
- 测试充分：❌

**🔴 阻断点：** 两个阻断性问题,plan 不可直接执行:
(A) 🔴 核心前提被实测数据证伪:fingerprint(pair+result-bucket)分布为 574 fps×1 / 51 fps×2 / 2 fps×3 / 0 fps>3。设 LIMIT=3 → skip 0 matches(0.0%);LIMIT=2 → skip 2(0.3%)。根因:每 evolution gen 造新 bot 版本,pair(v127,v141) 与 (v128,v141) 是不同 fingerprint,同 pair 极少同结果复赛≥3 次。battle_experience 占 45% LLM 成本是 unique 新 pair 的固有吞吐,非重复对局再分析。dedup 命中率≈0,砍 40% 成本不成立(plan risk 节降级到 15-25% 也仍高估,实测=0%)。此 fix 实质 INERT。
(B) 🔴 漏关键调用点:plan 扩展 mark_analyzed 接受 fingerprint 并维护 fp_count,但从未更新成功路径调用点(battle_experience.py:383 _apply_batch_results 成功 mark / :451 _process_one_match 成功 mark)传 fingerprint。fp_count 永不被填充 → layer


**遗漏调用方：** 🔴 BLOCKING: 计划扩展 mark_analyzed 接受 fingerprint 参数并维护 fp_count,但成功路径的调用点从未更新传 fingerprint:
- web/core/battle_experience.py:383 (_apply_batch_results 成功 mark) `mark_analyzed(entry.get("id",""), fail_count=0)` 未传 fingerprint
- web/core/battle_experience.py:451 (_process_one_match legacy 成功 mark) `mark_a


**修正要点：** 【file:line 核查】大部分准确:POLL_INTERVAL=20@54/TARGET_BATCH=6@55/MAX_ANALYSES_PER_HOUR=240@57 ✓;mark_analyzed@130-139 schema{fail_count:int} ✓;get_unanalyzed 三层过滤(done/poison@190/evicted@196-199) ✓;_apply_batch_results `if not summaries: return`@374 ✓;llm_costs battle_experience=196 calls/$30.27≈计划$29.97/194 ✓;match_history 678 matches≈计划674 ✓;494 unique pairs≈计划491 ✓;marker 1104 entries≈计划1096 ✓;battle_experience.md 37 行≈计划38 ✓。唯一错位见 blocking(C)。

【实测数据(亲自跑)】fingerprint=sorted-pair+sorted-wins+draws。LIMIT=3→skip 0/678(0.0%);LIMIT=2→skip 2/678(0.3%);LIMIT=1→skip 55/678(8.1%,但会丢增量信号)。winner-band bucket 同样 0 skip。结论:dedup 对当前 pool 完全无效。

【建议】reject 此 fix 或大幅返工:
1) 放弃内容哈希去重(命中率≈0)。
2) 转 audit #10 真正杠杆 — 子项(B) source_v 增量合并(非全量重写):新 pair 数量无法减少,但可砍 per-call input tokens(battle_experience.md 作 current


**测试缺口：** 1) 三个 proposed test 都测 dedup 命中后行为,但无一个测 fp_count 在真实成功路径上自增(catch 不到漏调用点 bug)。test_dedup_skips_pair_after_threshold 直接构造 fp_count marker 绕过了真实填充路径。
2) test_empty_batch_no_llm_call 测的状态(all dedup-skipped)在真实数据下不可达(dedup 永不触发),且 _run_llm_update 已被 `if not summaries: return` 双重保护 — 该测试 tautological。
3) 缺 backward-compat 测试:旧 marker dict(无 fp_count key)被新 layer-4 filter 读取时是否 crash(marker.get("fp_cou


---


## 2. Batch 2 — 校准 + 阻断升级（4-6h）

> fix-2/3 都涉及 gate/校准层但改动位置无交叉(commit vs quality gates)。fix-8 是纯提取 refactor。fix-9 独立改 fix_injection。4 个合批因为都改质量基础设施且 risk 可控。fix-3 需修正 GRANDFATHER 边界(v166 不存在，真正基线是 v165)；fix-2 需决策 reconciled 后冻结 delta；fix-8 需修正 test 中 None vs {} 语义。复用 M6 telemetry_fidelity 已验证的 BLOCKING 范式。

### fix-2-critic-calibration-async [CRITICAL|architecture|low] ⚠️ 需修正

**critic calibration rating_delta 异步回填:commit 写占位,daemon 评估收敛后原地回填真实 delta**

把 critic_calibration.jsonl 的 rating_delta 计算从 commit 时(此刻新 bot rd=350 → conservative_rating=r-700 → delta 恒为 0/near-zero,实测 90 条仅 2 条非零,两条校准分支 98% 代永不触发,Critic 自校准闭环物理失效)改为:commit 只写占位(rating_delta=null, reconciled=false),daemon 在 save_cycle 末尾(每 20 场或 60s)跑 reconcile_critic_calibration(),当新 bot rd<60 且 games≥60 时用收敛后的 conservative_rating 原地重写该行的真实 delta 并置 reconciled=true。同时修复 agent_review.py:65 的 None crash 隐患(今日被 bare except 吞掉=静默全禁用校准)+ 让校准只消费 reconciled 条目。幂等(reconciled flag)、加 fcntl LOCK_EX(避免与 commit 的 append 竞争)、whole-file 原子替换(tmp+fsync+os.replace)。是 audit #2(CRITICAL 新发现)+ #11(measur


<details><summary>当前代码状态（核查后）</summary>

```
审计结论经核查属实且更精确。实测证据:

1. tool_commit.py:194-210 (commit_bot Meta-3 block): commit 时计算 rating_delta。此刻新 bot 刚 tag,daemon 还没评估,rd 仍=350。代码:`v_cons = vp.conservative_rating() if vp else None`(L198)→ conservative_rating = r - 2*rd = r-700(Glicko2Player.conservative_rating, glicko2.py:60-62)。source bot 已收敛 rd 小,v_cons - s_cons 几乎恒被 -700 主导 → delta 近 0。

2. 实测 critic_calibration.jsonl = 90 行,`grep -v '"rating_delta": 0' | wc -l` = 仅 2 条非零(与审计"88/90 为 0"一致)。最后 10 条全部 rating_delta=0(v156-v165)。

3. agent_review.py:57-84 (critic calibration reader): `recent = [json.loads(l) for l in lines[-10:]]`(L62),需 `len(recent)>=3`(L63),`deltas = [r.get("rating_delta", 0) ...]`(L65),`avg_delta = sum(deltas)/len(deltas)`(L67)。两个分支:`avg_score>7 and avg_delta<0`(L68)或 `avg_score<4 and avg_delta>0`(L76)。因 avg_delta≈0,两分支 98% 代不触发。

4. agent_review.py 隐藏 bug:一旦 rating_delta 是 JSON null,`r.get("rating_delta", 0)` 返回 None(Python None,不是 0,因为 key 存在只是值是 null)→ `sum([...,None,...])` TypeError → 被 L83 bare `except: pass` 吞掉 → 整段 calibration 静默关闭。本 fix 必须同时修这个。

5. 关键不变量(实测确认):90 行 = 90 unique version,无重复(grep '"version"' | sort | uniq -d = 空)。故 reconcile 必须**原地改行**,不能 append 新行,否则 lines[-10:] 窗口被重复 version 污染。

6
```
</details>


#### 改动点（6 处）

- **`web/core/tool_commit.py`** @ `commit_bot() Meta-3 block, L194-210` [modify] — Stop computing a delta guaranteed to be 0/nonsensical at commit time (rd=350 → v_cons=r-700). Write null + reconciled:false so the reconcile pass can find pending entries. Switch t
- **`web/core/agent_review.py`** @ `critic calibration reader, L62-67` [modify] — Two coupled fixes: (a) `or 0` coerces stray null so sum() never crashes (today bare except:pass silently disables ALL calibration on a single null); (b) only count reconciled entri
- **`web/core/elo_daemon.py`** @ `module constants after L203, new function reconcile_critic_calibration() before save_cycle (L607)` [add] — Daemon is the only writer with live converged ratings. In-place rewrite preserves one-line-per-version invariant (verified: 90 lines=90 unique versions) so the reader's lines[-10:]
- **`web/core/elo_daemon.py`** @ `end of save_cycle(), after _refresh_action_stats_async(active_bots) at L668, before `if verbose:` at L670` [modify] — Hook reconcile into existing save cadence so it runs after every rating convergence step, no new timer/thread. Passing in-memory ratings/bot_stats avoids redundant file reads (alre
- **`web/core/elo_daemon.py`** @ `import block L60-68` [modify] — reconcile_critic_calibration uses locked_file (LOCK_EX rewrite). Already exported by evolution_infra:188. json/os already imported at daemon top (used by save_ratings etc.).
- **`web/core/tool_commit.py`** @ `commit_bot() — fold the append into change #1` [modify] — Current commit-side append uses bare open() with NO fcntl lock while daemon reconcile rewrites the whole file under LOCK_EX. A concurrent daemon reconcile during commit append coul

<details><summary>完整 diff 详情</summary>


**`web/core/tool_commit.py`** @ `commit_bot() Meta-3 block, L194-210` [modify]

现状:
```python
rating_delta = 0 ratings_cal = load_ratings() vp = ratings_cal.get(f"claude_v{v}") sp = ratings_cal.get(f"claude_v{source_v}") v_cons = vp.conservative_rating() if vp else None s_cons = sp.conservative_rating() if sp else None if v_cons is not None and s_cons is not None: rating_delta = round(v_cons - s_cons, 1) cal_file = RESULTS_DIR / "critic_calibration.jsonl" cal_entry = json.dumps({ "version": v, "source_v": source_v, "critic_score": critic_score, "rating_delta": rating_delta, [...]
```
改后:
```python
# Commit-time rating_delta is a PLACEHOLDER=null: the new bot's rd is # still 350 (daemon hasn't rated it yet), so conservative_rating() # = r-700 yields a spurious near-zero delta. The real delta is # back-filled asynchronously by elo_daemon.reconcile_critic_calibration() # once the new bot's rd converges (< RECONCILE_RD_THRESHOLD). See fix-2. cal_file = RESULTS_DIR / "critic_calibration.jsonl" cal_entry = { "version": v, "source_v": source_v, "critic_score": critic_score, "rating_delta": [...]
```
*Stop computing a delta guaranteed to be 0/nonsensical at commit time (rd=350 → v_cons=r-700). Write null + reconciled:false so the reconcile pass can find pending entries. Switch to append_locked_jsonl (fcntl LOCK_EX) for atomicity vs daemon's whole-file rewrite.*


**`web/core/agent_review.py`** @ `critic calibration reader, L62-67` [modify]

现状:
```python
recent = [json.loads(l) for l in lines[-10:] if l.strip()] if len(recent) >= 3: scores = [r.get("critic_score", 0) for r in recent] deltas = [r.get("rating_delta", 0) for r in recent] avg_score = sum(scores) / len(scores) avg_delta = sum(deltas) / len(deltas)
```
改后:
```python
recent = [json.loads(l) for l in lines[-10:] if l.strip()] # Only consume reconciled entries (rating_delta back-filled by daemon # after rd converged). Un-reconciled placeholders have rating_delta=null # and would either crash sum() or inject false-zero signal — skip them. recent = [r for r in recent if r.get("reconciled") is True] if len(recent) >= 3: scores = [r.get("critic_score", 0) for r in recent] deltas = [r.get("rating_delta") or 0 for r in recent] avg_score = sum(scores) / [...]
```
*Two coupled fixes: (a) `or 0` coerces stray null so sum() never crashes (today bare except:pass silently disables ALL calibration on a single null); (b) only count reconciled entries so avg_delta reflects real skill change. If <3 reconciled yet, calibration stays silent (correct — no signal) rather than firing on garbage zeros.*


**`web/core/elo_daemon.py`** @ `module constants after L203, new function reconcile_critic_calibration() before save_cycle (L607)` [add]

现状:
```python
PRIORITY_EVAL_FILE = RESULTS_DIR / "priority_eval.json"
```
改后:
```python
PRIORITY_EVAL_FILE = RESULTS_DIR / "priority_eval.json" CRITIC_CALIBRATION_FILE = RESULTS_DIR / "critic_calibration.jsonl" # Reconcile gate: back-fill rating_delta only once the new bot's rating has # converged enough that conservative_rating() (r-2rd) is meaningful. rd<60 ≈ # ±120 confidence band — tighter than eval early-exit (90) so the delta is # stable. RECONCILE_MIN_GAMES is below MIN_GAMES_FOR_EVAL (100) deliberately: # the calibration signal must return WITHIN the 10-gen sliding [...]
```
*Daemon is the only writer with live converged ratings. In-place rewrite preserves one-line-per-version invariant (verified: 90 lines=90 unique versions) so the reader's lines[-10:] window is not corrupted by duplicates. Idempotent via reconciled flag. Triggered from save_cycle → rides existing 60s/20-game cadence, no new thread.*


**`web/core/elo_daemon.py`** @ `end of save_cycle(), after _refresh_action_stats_async(active_bots) at L668, before `if verbose:` at L670` [modify]

现状:
```python
_refresh_action_stats_async(active_bots) if verbose:
```
改后:
```python
_refresh_action_stats_async(active_bots) # fix-2: back-fill rating_delta on un-reconciled critic_calibration entries # now that this save_cycle just updated+persisted ratings. Advisory: # cheap (small file, one pass) and non-blocking on any error. try: reconcile_critic_calibration(ratings, bot_stats) except Exception as _rc_err: log.debug("reconcile_critic_calibration call failed (non-fatal): %s", _rc_err) if verbose:
```
*Hook reconcile into existing save cadence so it runs after every rating convergence step, no new timer/thread. Passing in-memory ratings/bot_stats avoids redundant file reads (already current in save_cycle scope). try/except preserves save_cycle's non-fatal contract.*


**`web/core/elo_daemon.py`** @ `import block L60-68` [modify]

现状:
```python
from evolution_infra import ( ... read_locked_json, write_locked_json, append_locked_jsonl, ... )
```
改后:
```python
from evolution_infra import ( ... read_locked_json, write_locked_json, append_locked_jsonl, locked_file, ... )
```
*reconcile_critic_calibration uses locked_file (LOCK_EX rewrite). Already exported by evolution_infra:188. json/os already imported at daemon top (used by save_ratings etc.).*


**`web/core/tool_commit.py`** @ `commit_bot() — fold the append into change #1` [modify]

现状:
```python
with open(cal_file, "a", encoding="utf-8") as _cf: _cf.write(cal_entry + "\n")
```
改后:
```python
append_locked_jsonl(cal_file, cal_entry) # cal_entry is now a dict, see change #1
```
*Current commit-side append uses bare open() with NO fcntl lock while daemon reconcile rewrites the whole file under LOCK_EX. A concurrent daemon reconcile during commit append could lose the new line. Switching to append_locked_jsonl (fcntl LOCK_EX) serializes with daemon rewrite. (Merged into change #1's new_snippet — listed separately only to flag the lock-upgrade rationale.)*

</details>


#### 测试（7 个）

- `web/tests/test_logic_phase1_fixes.py::test_name` — reconcile_critic_calibration() takes a placeholder entry {version:200,source_v:199,rating_delta:null,reconciled:false} +
- `web/tests/test_logic_phase1_fixes.py::test_name` — Calling reconcile twice on the same file produces identical output (second call makes no change / writes nothing). Guard
- `web/tests/test_logic_phase1_fixes.py::test_name` — Entry with new-bot rd=80 (>=RECONCILE_RD_THRESHOLD) left as rating_delta=null, reconciled=false. Reconcile must not fire
- `web/tests/test_logic_phase1_fixes.py::test_name` — Entry with games < RECONCILE_MIN_GAMES (e.g. 30) left as placeholder even if rd<60. Combined rd+games gate.
- `web/tests/test_logic_phase1_fixes.py::test_name` — Entry whose new bot no longer in ratings left as null/reconciled=false forever (no crash, no corruption). Other entries 
- `web/tests/test_logic_phase1_fixes.py::test_name` — Replicate agent_review.py:62-67 logic: with 3 reconciled (delta<0,score>7) + 2 null placeholders in last 10, reader filt

#### 向后兼容
完全兼容,无需同步改调用方。(1) commit_bot 返回值不变(MCP result 不含 calibration block);(2) critic_calibration.jsonl 仍是 JSONL,新增字段是附加(reconciled/reconcile_ts/reconcile_*),旧 reader(agent_review.py 原 .get('rating_delta',0))对数值仍工作;但本 fix 同步把 reader 改成 reconciled-only + None 强制转换,这是本 fix 的一部分(同进程内 agent_review.py 自身改),无外部调用方需同步;(3) reconcile 函数是 daemon 内部新增,无外部调用方;(4) save_cycle 签名不变;(5) 已 grep 全仓:critic_calibration.json


#### 风险
- RD threshold 60 在 30-bot pool 可能太严:对手~28 个,新 bot 的 rd 可能 plateau 在 70-90 永不 <60 → reconcile 不触发,校准仍死。Mitigation:可观测(log.info 每次 reconcile + log_system
- Whole-file rewrite race:web 进程(手动 /api/control/tool 调 commit_bot)append 与 daemon rewrite 并发会丢数据。Mitigation:两侧都用 locked_file LOCK_EX(commit 侧改 append_l
- 被 reap 的 bot(rd 永远 >60 就被剔除)留下永久 null 行,长期堆积。Mitigation:无害——reader 过滤 reconciled==True;文件受 active-pool churn(~30 reap / 30 gen)自然 bounded;若超 500 行可加 n
- reader 现在需 ≥3 reconciled 才触发(原 ≥3 任意)。部署后头 ~3 代校准静默(少于 3 reconciled)。这是正确的(无信号),但行为变化——然而原分支本就因 delta=0 不触发,无回归,只是延迟生效的正确性。


#### 验证步骤
```
grep -c '"rating_delta": null' web/core/results/critic_calibration.jsonl → after a fresh commit, should show ≥1 (the new placeholder). Proves commit-side change landed.
Wait ~5 min (daemon runs), then: grep -c '"reconciled": true' web/core/results/critic_calibration.jsonl → non-zero and growing; grep '"reconcile_ts"' → timestamps within last few minutes for recently-converged bots. Proves daemon reconcile firing.
Confirm no duplicate versions after reconcile: grep -oP '"version": \K[0-9]+' web/core/results/critic_calibration.jsonl | sort -n | uniq -d | wc -l → must be 0 (in-place rewrite preserved one-line-per-version).
grep 'critic_calibration reconciled' web/logs/*.log → daemon INFO lines showing real deltas back-filled (e.g. 'v165: rating_delta=+47.3 (rd=52.0, 68g vs source v164)'). Proves signal is non-zero now.
Zero-regression: cd web && python -m pytest tests/ -q → expect 1037+ passed (baseline) plus the 7 new tests added.
Manual: after ≥10 generations post-deploy, tail newest gen critic_io.txt and confirm a '# Critic Calibration Note' block appears when avg_score/avg_delta cross threshold (was absent for 88/90 gens historically).
```


#### 回滚
纯增量回滚:(1) 还原 tool_commit.py:194-210 为原 rating_delta 计算 + bare open append;(2) 还原 agent_review.py:62-67 为原 reader(删 reconciled filter + or 0);(3) 删 elo_daemon.py 的 reconcile_critic_calibration 函数 + CRITIC_CALIBRATION_FILE/RECONCILE_* 常量 + save_cycle 里的 3 行调用 + locked_file import。无 schema/迁移需回滚(已写入的 r


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：✅
- 测试充分：✅

**🔴 阻断点：** 无阻断性问题,plan可直接执行。唯一需实施者决策的是:reconcile对"已reconciled=true的行"二次调用时,delta是否用当前ratings重算(会随source bot继续收敛而漂移)还是冻结首次回填值。plan未明确该语义。推荐"reconciled后冻结delta"(读时跳过reconciled==true的行),这样幂等性最强、语义最清晰;补一个test_reconcile_skips_already_reconciled断言即可。这是实施细节而非返工点。


**遗漏调用方：** 无遗漏。grep全仓确认critic_calibration.jsonl仅2处消费:tool_commit.py:202(写)+agent_review.py:59(读),无第三方。commit_bot返回dict不含calibration字段不变;save_cycle签名(L607)不变;reconcile为daemon内部新增无外部调用方;agent_review reader改动同进程自洽。实施注意:append_locked_jsonl(entry)收dict(evolution_infra:249),现状commit侧cal_entry是预先json.dumps的字符串→切换时传参改


**修正要点：** 修正/补充要点:(1)【RD阈值应默认放宽——最大可优化点】实测当前30个active bot仅1个rd<60(最低40.5,其余95-350,中位数~150)。plan默认RECONCILE_RD_THRESHOLD=60几乎永不触发→校准仍死,违背fix目的。建议默认直接设~90(对齐EVAL_RD_THRESHOLD语义但更现实),或用EVAL_RD_MIN_GAMES=20+rd<100组合gate,别部署后等50代再回看。非blocking(代码能跑,默认值偏严)。(2)【首跑bulk rewrite量修正】plan risk#4说首跑~90行,实测90行中66行source_v/version bot已reaped→reconcile跳过,真正回填仅~24行(两端bot活着的)。不影响正确性,实施者不必担心90行同时重写。(3)【历史0值覆盖是期望行为】现存90行rating_delta=0无reconciled key,其中24行两端bot活→首跑覆盖成真实delta,这是期望的复用历史;reader改reconciled-only后这些行首次能进入calibration。无消费者依赖"delta恒0"。(4)【locked_file必须补import】daemon未import(evolution_infra:188),plan change#5已标注,加到L60-68的import块。(5)【reader None-coerce用`or 0`可接受】对delta=0也返回0(falsy),但只统计reconciled entries且其delta已是真实数值,不影响。(6)【gate ledger零影响】grep确认code_verification.py/tool_gates.py无任何calibration引用,本fix不触碰AST/blocking 


**测试缺口：** 测试基本充分(回填/幂等/rd-gate/games-gate/reaped-skip/reader消费侧/source-grep回归)。建议补:(a)test_reconcile_skips_already_reconciled——显式断言已reconciled=true的行二次调用delta不被重算(见blocking_issues语义决策)。(b)可选:reader对"3条reconciled delta<0但其中混入null"的混合窗口正确过滤(现有test_reader_skips_unreconciled已覆盖null coerce,此为冗余补充,非必须)。backward-compat:旧reader行为无需测(本fix同步改reader),plan已正确说明。

### fix-3-placement-shadow-blocking [HIGH|architecture|low] ⚠️ 需修正

**placement_shadow TRUE-SHADOW 升 BLOCKING（仅 gt0，复用 M6 范式 + grandfather v166）**

把 placement_shadow 检测器产出的 TRUE-SHADOW（gt0 kind）从 advisory 升为 BLOCKING：在 tool_gates.py:287 all_passed 加 `and placement_shadow_ok`（placement_shadow_ok = 无 TRUE-SHADOW warning OR v<=grandfather），失败时复用 M6 范式写 worker_failures.jsonl（带 RELOCATE 指令）传给下个 worker。核查发现审计的"活体铁证（10 事件）"在当前日志不可复现，但直接 AST 复测证明 v166（当前 HEAD）确实带 1 个 TRUE-SHADOW（_river_stackoff_guard@L1166，v147 注释自承 unreachable 却未 relocate），故必须加 grandfather=166 豁免 v166 本身。审计指引的"verify_code():498 内联调用消除双路径"子项经核查不适用（verify_code 纯 compile 检查、全仓只有一处 detect 调用点），明确不做。改动 5 处全在 tool_gates.py，零下游修改（all_passed 是下游唯一聚合 key），复用 telemetry_fidelity 已验证的 BLOC


<details><summary>当前代码状态（核查后）</summary>

```
核查后真实 file:line + 可 grep snippet（非复述审计）：

1. 检测器 `detect_placement_shadow_warnings()` 实际在 `code_verification.py:129-235`，kind 分类逻辑 L218-222：`severity = "TRUE SHADOW" if kind == "gt0" else "review"`，warning 字符串 L223-234 含字面量 "placement_shadow ({sev})" → grep "TRUE SHADOW" 命中。review/other 良性 case 在 L219-221 对 `kind=="eq0"` 直接 `continue`（不进 warnings）。故 `"TRUE SHADOW" in w` 是精确的 hard-block 判据。

2. 接入点 `tool_gates.py:141-154`（确认 advisory 现状）：`placement_shadow_warnings = detect_placement_shadow_warnings(bot_dir)` 之后仅 `log_system_event("pipeline.placement_shadow", "warn", ...)`，**无任何 return / 无进 all_passed**。对照同函数 M6 `tool_gates.py:166-178`：`telemetry_fidelity_warnings = detect_telemetry_fidelity_warnings(bot_dir)` → L178 `telemetry_fidelity_ok = len(telemetry_fidelity_warnings) == 0`。

3. all_passed 组成 `tool_gates.py:287-295`（7 条，确认无 placement_shadow）：
```
all_passed = (
    len(compile_errors) == 0
    and len(smoke_errors) == 0
    and decision_ok
    and len(oversized) == 0
    and code_changed
    and fix_ok
    and telemetry_fidelity_ok
)
```
M6 BLOCKING 范式 `tool_gates.py:178`（`telemetry_fidelity_ok=len==0`）→ `tool_gates.py:294`（进 all_passed）→ `tool_gates.py:324-325`（result 持久化 tele
```
</details>


#### 改动点（5 处）

- **`web/core/tool_gates.py`** @ `tool_gates.py:178 之后（紧接 telemetry_fidelity_ok = ... 之后）` [add] — 镜像 M6 的 `telemetry_fidelity_ok = len(...)==0` 模式（同文件 L178）。只把 gt0/TRUE-SHADOW 计入 blocking；review/equ0 良性 case 保持 advisory 避免误伤。grandfather 避免 v166 本身及其恢复路径卡死。
- **`web/core/tool_gates.py`** @ `tool_gates.py:287-295（all_passed 组成）` [modify] — 审计 #3 核心改法：把 TRUE-SHADOW 纳入 all_passed。镜像 M6 注释风格（列名 + 一句根因）。all_passed 是 _quality_gate_ok/tool_helpers 唯一读的聚合 key，加条件即自动生效，无需改下游。
- **`web/core/tool_gates.py`** @ `tool_gates.py:319-320（result dict 的 placement_shadow_warnings 注释+字段）` [modify] — 持久化 placement_shadow_ok 到 result（与 telemetry_fidelity_ok/tool_gates.py:325 对称），让 reviewer/critic/orchestrator 能读到此 gate 的判定结果（M6 同款字段 telemetry_fidelity_ok 在 L325）。idempotency 缓存命中
- **`web/core/tool_gates.py`** @ `tool_gates.py:361 之后（telemetry_fidelity 分支的 _record_quality_failure 块之后，紧接 log_system_event L363 之前）` [add] — 完整复用 M6 BLOCKING 范式：failed_gates_detail 记录 + _record_quality_failure 写 worker_failures.jsonl（category=gate）。RELOCATE 指令通过 _load_recent_failures(5)→worker prompt 链路传到下个 worker。grand
- **`web/core/tool_gates.py`** @ `tool_gates.py:48（DECISION_TEST_SPRT_ENABLED = False 之后，_record_quality_failure def L51 之前）` [add] — 最小祖父豁免。仅豁免 v166 及之前（已发布的带 shadow 的 bot）；v167+ 强制。常量风格镜像 DECISION_TEST_SPRT_ENABLED（模块常量非 env var）。

<details><summary>完整 diff 详情</summary>


**`web/core/tool_gates.py`** @ `tool_gates.py:178 之后（紧接 telemetry_fidelity_ok = ... 之后）` [add]

现状:
```python
telemetry_fidelity_ok = len(telemetry_fidelity_warnings) == 0 smoke_errors = run_smoke_test(bot_dir)
```
改后:
```python
telemetry_fidelity_ok = len(telemetry_fidelity_warnings) == 0 # A3-fix3: placement-shadow TRUE-SHADOW 升 BLOCKING（仅 gt0；review/other 保持 advisory）。 # TRUE SHADOW = detector call-site nested in `if to_call > 0:` block AFTER the # `to_call >= my_chips` early-return → structurally unreachable for stack-covering # all-ins (INERTNESS root cause, v138-v166 复发，v166 _river_stackoff_guard@L1166 仍 shadow)。 # Grandfather: v <= 166 的既有 TRUE-SHADOW 不阻塞（v166 已发布带此 shadow），v167 起强制。 placement_shadow_true = [...]
```
*镜像 M6 的 `telemetry_fidelity_ok = len(...)==0` 模式（同文件 L178）。只把 gt0/TRUE-SHADOW 计入 blocking；review/equ0 良性 case 保持 advisory 避免误伤。grandfather 避免 v166 本身及其恢复路径卡死。*


**`web/core/tool_gates.py`** @ `tool_gates.py:287-295（all_passed 组成）` [modify]

现状:
```python
all_passed = ( len(compile_errors) == 0 and len(smoke_errors) == 0 and decision_ok and len(oversized) == 0 and code_changed # MUST have at least one changed .py file and fix_ok # P1-3: missing mandatory fix blocks the pipeline and telemetry_fidelity_ok # M6: multi-arm detector telemetry must be function-scope (false-INERT prevention) )
```
改后:
```python
all_passed = ( len(compile_errors) == 0 and len(smoke_errors) == 0 and decision_ok and len(oversized) == 0 and code_changed # MUST have at least one changed .py file and fix_ok # P1-3: missing mandatory fix blocks the pipeline and telemetry_fidelity_ok # M6: multi-arm detector telemetry must be function-scope (false-INERT prevention) and placement_shadow_ok # A3-fix3: TRUE-SHADOW detector call-site after to_call>=my_chips early-return blocks (INERTNESS prevention) )
```
*审计 #3 核心改法：把 TRUE-SHADOW 纳入 all_passed。镜像 M6 注释风格（列名 + 一句根因）。all_passed 是 _quality_gate_ok/tool_helpers 唯一读的聚合 key，加条件即自动生效，无需改下游。*


**`web/core/tool_gates.py`** @ `tool_gates.py:319-320（result dict 的 placement_shadow_warnings 注释+字段）` [modify]

现状:
```python
# A3: advisory only (non-blocking). Reviewer/critic/orchestrator can read these. "placement_shadow_warnings": placement_shadow_warnings,
```
改后:
```python
# A3-fix3: TRUE-SHADOW (gt0) is now BLOCKING (see all_passed); review/other stays advisory. "placement_shadow_warnings": placement_shadow_warnings, "placement_shadow_ok": placement_shadow_ok,
```
*持久化 placement_shadow_ok 到 result（与 telemetry_fidelity_ok/tool_gates.py:325 对称），让 reviewer/critic/orchestrator 能读到此 gate 的判定结果（M6 同款字段 telemetry_fidelity_ok 在 L325）。idempotency 缓存命中时也回显此 key。*


**`web/core/tool_gates.py`** @ `tool_gates.py:361 之后（telemetry_fidelity 分支的 _record_quality_failure 块之后，紧接 log_system_event L363 之前）` [add]

现状:
```python
if not telemetry_fidelity_ok: failed_gates_detail.append( f"telemetry_fidelity({'; '.join(w[:120] for w in telemetry_fidelity_warnings[:3])})" ) # Record the M6 telemetry-fidelity violation to worker_failures so the next # worker attempt sees the hoist recipe (function-scope stderr.write + fixture). for w in telemetry_fidelity_warnings: _record_quality_failure( v, "telemetry_fidelity", "multi_arm_detector", f"M6 telemetry-fidelity violation (false-INERT risk): {w[:2000]}", )
```
改后:
```python
if not telemetry_fidelity_ok: failed_gates_detail.append( f"telemetry_fidelity({'; '.join(w[:120] for w in telemetry_fidelity_warnings[:3])})" ) # Record the M6 telemetry-fidelity violation to worker_failures so the next # worker attempt sees the hoist recipe (function-scope stderr.write + fixture). for w in telemetry_fidelity_warnings: _record_quality_failure( v, "telemetry_fidelity", "multi_arm_detector", f"M6 telemetry-fidelity violation (false-INERT risk): {w[:2000]}", ) if not [...]
```
*完整复用 M6 BLOCKING 范式：failed_gates_detail 记录 + _record_quality_failure 写 worker_failures.jsonl（category=gate）。RELOCATE 指令通过 _load_recent_failures(5)→worker prompt 链路传到下个 worker。grandfather 已在 placement_shadow_ok 内处理（v<=166 不会进此分支）。*


**`web/core/tool_gates.py`** @ `tool_gates.py:48（DECISION_TEST_SPRT_ENABLED = False 之后，_record_quality_failure def L51 之前）` [add]

现状:
```python
DECISION_TEST_SPRT_ENABLED = False
```
改后:
```python
DECISION_TEST_SPRT_ENABLED = False # A3-fix3: grandfather 版本 — v <= 此值的 placement-shadow TRUE-SHADOW 不阻塞 quality # gate。v166（当前 HEAD）strategy.py:L1166 `_river_stackoff_guard` 已发布带 TRUE-SHADOW #（nested in `if to_call > 0:` after early-return@L1117）；v167 起从 v166 clone 的 worker # 必须 RELOCATE 该 call-site 才能 pass。设此豁免避免 v166 自身/恢复路径误卡。 PLACEMENT_SHADOW_GRANDFATHER_V = 166
```
*最小祖父豁免。仅豁免 v166 及之前（已发布的带 shadow 的 bot）；v167+ 强制。常量风格镜像 DECISION_TEST_SPRT_ENABLED（模块常量非 env var）。*

</details>


#### 测试（5 个）

- `web/tests/test_logic_code_verification.py::test_name` — detect_placement_shadow_warnings() 对一个嵌套在 `if to_call > 0:` 块内、且该块出现在 `if to_call >= my_chips: return` early-return 之后的 
- `web/tests/test_logic_code_verification.py::test_name` — detector 调用在 `if to_call == 0:`（offense open-bet）块内不产生 warning（kind==eq0 → continue），或在无 early-return 函数内不产生 warning。构造 
- `web/tests/test_logic_code_verification.py::test_name` — 干净 strategy.py（detector call-site 在 early-return 之前）→ 0 warning。构造：def decide():\n    if _river_stackoff_guard(...): ret
- `web/tests/test_mcp_pipeline.py::test_name` — run_quality_gates 对带 TRUE-SHADOW 的 bot（version>166）返回 all_passed=False，result 含 placement_shadow_ok=False，且 _record_qual
- `web/tests/test_mcp_pipeline.py::test_name` — run_quality_gates 对 version=166 带 TRUE-SHADOW 的 bot → all_passed 不因 placement_shadow 失败（grandfather 豁免），placement_shadow

#### 向后兼容
无需同步改调用方。理由：(1) all_passed 是聚合 boolean，新增 placement_shadow_ok 条件后，下游消费方（_quality_gate_ok tool_helpers.py:269-271 读 all_passed、idempotency _idempotency_check 读 approval_key=all_passed、_gate_payload 持久化）全部自动生效，不感知新增子条件。(2) result dict 新增 placement_shadow_ok key 是纯增量字段，现有消费者只做 in-membership/get().None-safe 读取。(3) 函数签名不变（run_quality_gates 仍 args:{version,source_v}）。(4) detect_placement_shadow_warnings


#### 风险
- 祖父误判：v166 已发布带 TRUE-SHADOW，若不加 grandfather，下一 gen v167 从 v166 clone 后 worker 不 relocate 会 fail（预期）但若 worker 多次重试仍不会 relocate（不理解 AST shadow 概念）可能耗尽 MA
- 误伤合法 to_call>0 块内的非 shadow 调用：若 worker 新增的 detector 确实只在 to_call>0 路径有意义（如纯 call-facing guard），会被误标 TRUE-SHADOW。Mitigation：检测器 regex _PLACEMENT_SHADOW
- audit '活体铁证'不可复现（116 文件 0 shadow 事件）→ 若该门历史上从未真正 fire 过（事件丢失或从未触发），可能检测器在真实 pipeline 环境有未发现的 edge case（如 target_files 路径、并行 worker 半写状态）。Mitigation：改动
- grandfather 常量 166 是硬编码，未来需手工抬升。Mitigation：注释明示语义；下次 v166 的 shadow 被 relocate 后可保留 grandfather=166（无害，因为新 bot 已 clean）或随手更新。不构成阻塞。


#### 验证步骤
```
cd /home/zzx/project/pok/web && python -m pytest tests/test_logic_code_verification.py -v -k placement_shadow（新单测 3 case pass）
cd /home/zzx/project/pok/web && python -m pytest tests/test_mcp_pipeline.py -v -k 'true_shadow or grandfather'（新集成测 2 case pass）
cd /home/zzx/project/pok/web && python -m pytest tests/ -v（零回归：基线 1037 passed 仍全绿，新 +5 case → 1042）
cd /home/zzx/project/pok/web && python -c "import sys; sys.path.insert(0,'core'); import tool_gates; assert tool_gates.PLACEMENT_SHADOW_GRANDFATHER_V==166; print('constant ok')"（验证常量导入）
grep -n 'placement_shadow_ok' web/core/tool_gates.py（确认 5 处改动落地：常量定义/L178 计算/L294 all_passed/L321 result 字段/L362 failed_gates 分支）
手动对 v166 复测不阻塞：python3 -c "复用本 plan 核查脚本，detect v166 → 1 TRUE-SHADOW，但 run_quality_gates(v=166) 因 grandfather 不 fail"（可选，需构造 fixture）
```


#### 回滚
单 commit 回滚（git revert）。因改动是纯增量加条件 + 1 常量 + 1 result 字段，revert 后 all_passed 回到 7 条件、placement_shadow 退回 advisory-only（现状）。无数据迁移、无 schema 变更。若线上已用新规则产生若干 worker_failures.jsonl 记录（category=gate, role=placement_shadow），回滚后这些记录自然过期（_load_recent_failures 只读最近 5 条），无需清理。


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：❌
- 测试充分：❌

**🔴 阻断点：** 1. GRANDFATHER 边界根本性错误: 计划称 "v166 已发布带 TRUE-SHADOW，豁免 v<=166"，但实测显示 v166 是未追踪目录(?? bots/claude_v166/)、无 bot-v166 tag、非 current_v(current_v=165)。真正带 TRUE-SHADOW 的已发布版本是 v138-v161(24个带标记bot)，而 v162-v165 已发布且无 TRUE-SHADOW。计划的 166 硬编码是基于错误的事实前提。
   
   建议修正: 去掉整个 grandfather 机制。理由：(a) 已提交 bot 的 quality checkpoint 已有 all_passed=True，idempotency 机制保证不回溯重判(b) v166 未提交未标记，不构成 resume/recover 障碍(c) 无条件 BLOCKING 从 v167 起生效恰好是期望行为——强制 worker relocate shadow。若仍想保守可设 GRANDFATHER_V = current_v()动态获取(=165)，但语义上是多余的因为 v162-v165 无 TRUE-SHADOW。

2. role 参数粘贴错误: 计划中 _record_quality_failure(v, "placement_shadow", 


**遗漏调用方：** 无遗漏调用方。detect_placement_shadow_warnings 仅 tool_gates.py:144 一处调用(verify_code 不调用)。run_quality_gates 的 result dict 通过 _json_tool_result 返回，所有下游 MCP tool consumer 只做 dict.get() 读取，新增 placement_shadow_ok key 无影响。_record_quality_failure 写入 worker_failures.jsonl 被 _load_recent_failures(5) 在 agent_workers


**修正要点：** 1. 修正 grandfather：要么完全移除（推荐，因为 idempotency 已保障不回溯），要么改为 GRANDFATHER_V = 165（当前 current_v，而非计划的 166）。无条件 BLOCKING 对 v167+ 生效是正确行为——这是预期的强制 relocate 机制。
2. 修正 role 参数：_record_quality_failure(v, "placement_shadow", "placement_shadow", w) 而非 "multi_arm_detector"。
3. 新增一个测试用例（计划遗漏）：test_run_quality_gates_old_checkpoint_not_backfilled——构造一个 all_passed=True 的旧 checkpoint（模拟 v165 已提交），验证 run_quality_gates 对新版本 v167 生效但不回溯旧 checkpoint。
4. 检测器结果实测确认：v138-v161 每个有 1 个 TRUE-SHADOW(_river_stackoff_guard)，v162-v165 无 TRUE-SHADOW(9 个 review-only)，v166(未提交)有 1 个 TRUE-SHADOW。审计文档中 "v165 有 6 个 TRUE-SHADOW" 完全不准确——只有 1 个(如果 v165 的话是 0 个，v166 才有 1 个)。
5. 5 处 change 的位置（tool_gates.py L48/178/287-295/319-320/361 之后）经核实均准确存在。


**测试缺口：** 1. 缺少旧 checkpoint 不回溯测试（构造 all_passed=True 旧 checkpoint + 新版带 TRUE-SHADOW 的 bot，验证旧 checkpoint 的 all_passed 仍为 True 且新版被 BLOCKING）。
2. 缺少多 shadow 场景测试（两个不同的 detector 函数名，验证两条 _record_quality_failure 被写入）。
3. 计划中 test_detect_placement_shadow_review_kind_not_true 的断言是 `warnings 为空`——但实际上没有 early-return 的函数根本不会进入检测逻辑（L201 `if not early_return_lines: continue`），所以此 test 测试的是检测器的前置条件而非 eq0 分支。真正的 eq0 测

### fix-8-evidence-gate-extend [MEDIUM|both|low] ⚠️ 需修正

**evidence_gate 扩展到 critic 路径 (worker 防御性 prompt + critic score-cap 复核 fabricated 引用)**

审计锚点 #8 [MEDIUM] 核查后确认准确:_verify_cited_replays(tool_planning.py:198-258)只被 _validate_master_plan(L385)调用,仅覆盖 Master plan 的 GxHx#anchor 引用,worker/critic 路径的同代新捏造引用不被同一 spotlight manifest 复核;worker_prompt.md/critic_prompt.md grep replay/spotlight/cite/anchor 确为 0(只 critic_prompt L53-54 泛指 'cited evidence' 但无 anchored 引用规范)。本 fix:(a) 把 _verify_cited_replays 的比对逻辑抽成纯函数 _check_citations(text_list, anchor_map, label),原 _verify_cited_replays 退化成薄封装(签名不变,L385 调用点零改动),新增 _load_spotlight_anchor_map() helper;(b) run_critic(tool_gates.py)在 score_num 计算后(L793)、_gate_payload(L802) 前,对 critic 的 strategic_ass


<details><summary>当前代码状态（核查后）</summary>

```
核查后审计结论完全准确,行号无漂移。逐项证据:

(1) 唯一锚正则 + 唯一调用点 — web/core/tool_planning.py:195 `_CITATION_RE = re.compile(r"G\d+H\d+(?:#[0-9a-fA-F]{8})?|H\d+(?:#[0-9a-fA-F]{8})?")`;L198 `def _verify_cited_replays(plan)`;唯一调用点 L385 `errors.extend(_verify_cited_replays(plan))` 在 `_validate_master_plan`(L261 def)内。grep 全 web/core 确认 `_verify_cited_replays` 仅 tool_planning.py 一处定义 + 一处调用,_check_citations 当前不存在。

(2) _verify_cited_replays 当前结构(L198-258): 自己读 manifest(L213-217 `results/spotlight_manifest.json`,缺失/损坏 return [] 不阻断)、自己建 anchor_map(L223-225 `anchor_map[c.get("id","")] = c.get("anchor","")`)、自己扫 task 文本(L230-237 拼 worker_prompt+instruction+targeted_failure → `_CITATION_RE.findall` → base 不在 anchor_map → FABRICATED_EVIDENCE;L249-257 anchored 形式 anchor mismatch)。逻辑是可抽纯函数的 text-list → errors 形态。

(3) manifest 生成端 — web/core/replay_spotlight.py:405-436 `manifest_citations`,L410-418 每条 `{id, id_anchored, bot, game, hand, replay_file, anchor}`,anchor = SHA-256 of replay file[:8](L394-403 `_anchor_for`)。L426-432 写 `results/spotlight_manifest.json`(`{"bot":..., "citations":[...]}`,fcntl LOCK_EX)。实测文件存在:web/core/results/spotlight_manifest.json 当前内容 `{"bot": "claude_v159", "citations": [{"id": "G2H67", "
```
</details>


#### 改动点（4 处）

- **`web/core/tool_planning.py`** @ `L195-258 (_CITATION_RE + _verify_cited_replays) → 抽出纯函数 _check_citations` [modify] — 审计锚点确认 _verify_cited_replays 只被 _validate_master_plan (L385) 调用。抽出 _check_citations(text_list, anchor_map, label) 纯函数 + _load_spotlight_anchor_map() helper,使 master 路径(blocking)与 c
- **`web/core/tool_gates.py`** @ `run_critic score-calc block, L788-801 (在 score_num/raw_approved 计算后、_gate_payload 之前)` [modify] — 审计 #8 缺口:critic evidence 字段(h2h_weaknesses/experience_pool_refs/diff_refs)+strategic_assessment+feedback 是同代内新捏造 GxHx 引用的复发面(v127-v143 实证 9x)。复用 _check_citations,命中 fabricated→cap 
- **`web/core/prompts/critic_prompt.md`** @ `<analysis> 块末尾 L55-56 (</analysis> 之前)` [modify] — 审计确认 critic_prompt.md grep replay/spotlight/cite/anchor = 0(仅 L53-54 泛指'cited evidence')。加一句明确引用规范,告知 critic anchored GxHx#anchor 才是合法引用,降低 fabrication 发生率(防御性),且解释为何 score 可能被 cap
- **`web/core/prompts/worker_prompt.md`** @ `<scope_contract> 块 L89-94 (worker_prompt.md)` [modify] — 审计确认 worker_prompt.md grep replay/spotlight/cite/anchor = 0。worker prompt 里的引用其实已被 master-plan 阶段的 _verify_cited_replays 覆盖(master plan 的 worker_prompt 字段被 L233-237 扫描),但 worker LL

<details><summary>完整 diff 详情</summary>


**`web/core/tool_planning.py`** @ `L195-258 (_CITATION_RE + _verify_cited_replays) → 抽出纯函数 _check_citations` [modify]

现状:
```python
_CITATION_RE = re.compile(r"G\d+H\d+(?:#[0-9a-fA-F]{8})?|H\d+(?:#[0-9a-fA-F]{8})?") def _verify_cited_replays(plan): """...Returns a list of BLOCKING error strings...""" errors = [] try: _manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "spotlight_manifest.json") if not os.path.exists(_manifest_path): return errors with open(_manifest_path, encoding="utf-8") as f: manifest = json.load(f) except Exception: return errors anchor_map = {} for c in [...]
```
改后:
```python
_CITATION_RE = re.compile(r"G\d+H\d+(?:#[0-9a-fA-F]{8})?|H\d+(?:#[0-9a-fA-F]{8})?") def _load_spotlight_anchor_map(): """Load spotlight_manifest.json → {citation_id: anchor}. Returns None if the manifest is missing/corrupt (caller must treat None as 'cannot verify, skip').""" try: _manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "spotlight_manifest.json") if not os.path.exists(_manifest_path): return None with open(_manifest_path, encoding="utf-8") as f: [...]
```
*审计锚点确认 _verify_cited_replays 只被 _validate_master_plan (L385) 调用。抽出 _check_citations(text_list, anchor_map, label) 纯函数 + _load_spotlight_anchor_map() helper,使 master 路径(blocking)与 critic 路径(score cap)复用同一 manifest 比对逻辑。_verify_cited_replays 行为不变(L385 调用点无需改),仍是 list[errors]。_check_citations 对空/None anchor_map 返回 [](遵循既有'manifest 缺失不阻断'不变量)。*


**`web/core/tool_gates.py`** @ `run_critic score-calc block, L788-801 (在 score_num/raw_approved 计算后、_gate_payload 之前)` [modify]

现状:
```python
score = data.get("score", 0) try: score_num = float(score) except (TypeError, ValueError): score_num = 0.0 raw_approved = data.get("approved", score_num >= 6) advisory_approved = bool(raw_approved) and score_num >= 6 approved = True force_advanced = bool(force_advance)
```
改后:
```python
score = data.get("score", 0) try: score_num = float(score) except (TypeError, ValueError): score_num = 0.0 raw_approved = data.get("approved", score_num >= 6) # A4 (#8): extend evidence_gate to the Critic path. Scan the critic's # evidence + strategic_assessment + feedback for GxHx#anchor replay # citations and cap the score if any cited replay is fabricated (not in # the spotlight manifest). Advisory — only caps score, does NOT block; # only fires on EXPLICIT GxHx patterns [...]
```
*审计 #8 缺口:critic evidence 字段(h2h_weaknesses/experience_pool_refs/diff_refs)+strategic_assessment+feedback 是同代内新捏造 GxHx 引用的复发面(v127-v143 实证 9x)。复用 _check_citations,命中 fabricated→cap score 6(保持 critic advisory 语义,precommit 仍是最终判官)+ log_system_event('pipeline.fabricated_citation')。严格只对显式 GxHx 模式校验(遵循指引'critic evidence 主要是 h2h/diff refs 非 replay,不要过度校验误杀'),manifest 缺失时 _anchor_map=None→跳过。result dict 透传 fabricated_citation 标志便于 telemetry(L912 附近 result 已用 **data 展开)。*


**`web/core/prompts/critic_prompt.md`** @ `<analysis> 块末尾 L55-56 (</analysis> 之前)` [modify]

现状:
```python
Then score against the criteria below. Ground your score in cited evidence: - Score > 6 requires citing specific H2H weaknesses, experience pool references, or diff evidence - Score > 8 requires citing all three </analysis>
```
改后:
```python
Then score against the criteria below. Ground your score in cited evidence: - Score > 6 requires citing specific H2H weaknesses, experience pool references, or diff evidence - Score > 8 requires citing all three **Replay-hand citation rule**: If you reference a specific replay hand, you MUST cite it by the anchored ID exactly as it appears in the injected Replay Spotlight section (format: `G<game>H<hand>#<8-hex-anchor>`). Do NOT invent hand IDs (e.g. `G3H25`) that do not appear in the [...]
```
*审计确认 critic_prompt.md grep replay/spotlight/cite/anchor = 0(仅 L53-54 泛指'cited evidence')。加一句明确引用规范,告知 critic anchored GxHx#anchor 才是合法引用,降低 fabrication 发生率(防御性),且解释为何 score 可能被 cap。*


**`web/core/prompts/worker_prompt.md`** @ `<scope_contract> 块 L89-94 (worker_prompt.md)` [modify]

现状:
```python
<scope_contract> Before editing, write a short plan: 1. Planned modified files and functions/constants 2. One-sentence statement of what you will not touch Do not broaden scope. Only modify your assigned `target_files`. </scope_contract>
```
改后:
```python
<scope_contract> Before editing, write a short plan: 1. Planned modified files and functions/constants 2. One-sentence statement of what you will not touch Do not broaden scope. Only modify your assigned `target_files`. **Replay-hand citation rule**: If you reference a specific replay hand from the injected Replay Spotlight section, cite it by the anchored ID verbatim (`G<game>H<hand>#<8-hex-anchor>`). Do NOT invent `GxHx` hand IDs — fabricated citations are rejected before your task [...]
```
*审计确认 worker_prompt.md grep replay/spotlight/cite/anchor = 0。worker prompt 里的引用其实已被 master-plan 阶段的 _verify_cited_replays 覆盖(master plan 的 worker_prompt 字段被 L233-237 扫描),但 worker LLM 在执行期可能新写入 instruction/注释。加一句防御性规范,与 critic prompt 对称。注意:本计划不做 worker 运行期 diff 文本的运行时复核(见 current_state 中 13d-equivalent 判断:worker final-diff 校验边际收益低、worker diff 大量真实文本易误杀——仅靠 master-plan gate + prompt 规范兜底)。*

</details>


#### 测试（7 个）

- `web/tests/test_arch_fixes_regression.py::test_name` — _check_citations(['see G3H25#deadbeef fix'], {'G3H25':'aabbccdd'}, label='X') 返回含 'FABRICATED_EVIDENCE' 与 'NOT in the sp
- `web/tests/test_arch_fixes_regression.py::test_name` — _check_citations(['G2H67#65831a0d'], {'G2H67':'65831a0d'}) 返回 [](合法 anchored 引用不报错);且 _check_citations(anything, None/{}
- `web/tests/test_arch_fixes_regression.py::test_name` — _check_citations(['G2H67#ffffffff'], {'G2H67':'65831a0d'}) 返回含 'anchor mismatch' 的 error(防止 hallucinated anchor 篡改真实 han
- `web/tests/test_arch_fixes_regression.py::test_name` — 回归:_validate_master_plan({'tasks':[{worker_prompt:'fix G9H99 hand'}]}) 在 manifest 存在且无 G9H99 时仍返回 errors(确保重构后 master-pl
- `web/tests/test_arch_fixes_regression.py::test_name` — 构造 critic LLM 输出 data={'score':8,'approved':True,'strategic_assessment':'fixes G9H99#deadbeef','evidence':{'h2h_weakness
- `web/tests/test_arch_fixes_regression.py::test_name` — manifest 缺失(_load_spotlight_anchor_map→None)时,critic data={'score':8,'strategic_assessment':'G9H99#deadbeef'} 不被 cap(sco

#### 向后兼容
调用方无需同步改。设计点: (1) _verify_cited_replays 内部重构,签名不变,仍返回 errors list,L385 调用点不变;新增 _check_citations 纯函数无既有调用方;(2) _check_citations 对不存在的 manifest 返回空 list(与 _verify_cited_replays 既有行为一致),不引入新异常;(3) critic score cap 只改 score_num 局部变量,不改 _gate_payload/_record_gate 签名,downstream(orchestrator、experience_pool 写入、telemetry)读到的就是 capped score,无需改;(4) evidence 字段 schema 不变;(5) prompt 改动是 additive(加一句指令不删字段)。


#### 风险
- 误杀风险:Critic 在 h2h_weaknesses/experience_pool_refs/diff_refs 里写含 GxHx 子串的自由文本但无 #anchor,且该 base id 恰好不在 manifest → 被判 fabricated。Mitigation: 仅当 base id
- manifest 缺失场景:spotlight 当代未运行(无 spotlight_manifest.json)→ _load_spotlight_anchor_map 返回 None → critic cap 不生效,fabricated 引用漏检。Mitigation: 这与既有 master-
- manifest 过期:spotlight_manifest.json 只存最新一次 find_critical_hands 的 citations(replay_spotlight.py:427 覆盖写),若 critic 引用的是上一代 spotlight 的合法 hand id 而非当代,会被
- score cap 干扰 critic_calibration:critic_calibration.jsonl 读的是 raw score(agent_review.py:64),若 cap 在 _run_critic 内部应用会污染 calibration 基线。Mitigation: cap 


#### 验证步骤
```
cd /home/zzx/project/pok/web && python -m pytest tests/test_arch_fixes_regression.py -v -k 'check_citations or verify_cited_replays or run_critic'  # 新增 7 个测试全 pass
cd /home/zzx/project/pok/web && python -m pytest tests/ -q  # 零回归,维持 1037 passed (重构 _verify_cited_replays 不改外部行为)
cd /home/zzx/project/pok/web && python -c 'import core.tool_planning as t; print(callable(t._check_citations), callable(t._load_spotlight_anchor_map), callable(t._verify_cited_replays))'  # import 无语法错,三个函数可调用
cd /home/zzx/project/pok/web && python -c 'import core.tool_gates; import inspect; src=inspect.getsource(core.tool_gates.run_critic); assert "fabricated_citation" in src and "_check_citations" in src; print("critic cap wired")'  # 确认 run_critic 内 _check_citations 与 cap 注入
cd /home/zzx/project/pok && grep -n 'Replay-hand citation rule' web/core/prompts/critic_prompt.md web/core/prompts/worker_prompt.md  # 两 prompt 各一处命中
运行 1 代 evolution 后: grep 'fabricated_citation' /home/zzx/project/pok/web/core/results/system_events.jsonl  # 若 critic 真有 fabricated 则可见该事件(可能 0 条=无 fabrication,正常)
```


#### 回滚
单 commit 4 文件改动,回滚方式: git revert <commit>。逐文件回滚:(1) tool_planning.py — 恢复 _verify_cited_replays 为重构前版本(内联 anchor_map 构建+扫描),删 _check_citations/_load_spotlight_anchor_map,L385 调用点不变;(2) tool_gates.py — 删 L793 后插入的 _critic_cite_errors 块,恢复 score_num 直接计算;(3) critic_prompt.md — 删除 Replay-hand citation 


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：✅
- 测试充分：❌

**🔴 阻断点：** 1个需返工的设计矛盾 + 1个需修正的风险论证: (B1) plan 第2个 test test_check_citations_accepts_valid_anchored_citation 断言 "_check_citations(anything, None/{}) 返回 []",把 None(manifest缺失=跳过) 与 {}(manifest存在但citations为空=所有GxHx应判fabricated) 混为一谈。但现存 _verify_cited_replays(tool_planning.py:223-225,anchor_map={}时任何base not in {}→FABRICATED_EVIDENCE)对空dict是阻断的。若按plan实现 _check_citations 把 {} 也当跳过,且 _verify_cited_replays 重构为委托 _check_citations,master-path blocking 语义会变弱(空manifest不再阻断任何引用=引入gate旁路=INERTNESS)。返工要求:_check_citations 必须区分 None(跳过)与 {}(判fabricated),test断言改为 _check_citations(anything,None)==[] 且 _check_citations(['G


**遗漏调用方：** 无遗漏的调用方。核查:_verify_cited_replays 唯一调用点=_validate_master_plan(tool_planning.py:385),grep 全 web/core 确认仅此一处定义+调用;_CITATION_RE 唯一定义点(L195)。critic 分数消费链全部从 tool_gates.py:788-802 的 score_num 局部变量取值:_gate_payload(L809-810 score=score_num)→_record_gate(L822)写checkpoint→tool_commit.py:189 critic_gate.get('s


**修正要点：** 可行且高价值(fabrication 是 v127-v143 实证 9x 复发痛点,experience_pool_audit_io.txt L52194 明确记录第4次连续复发)。核查后修正要点:(R1) B1 必须返工——_check_citations 区分 None(跳过)与 {}(判fabricated),否则master-path重构会削弱既有gate引入INERTNESS旁路,违反零回归。这是唯一blocking-class设计矛盾。(R2) B2 risks#4论证修正:critic_calibration 在 tool_commit.py:189 写入(非agent_review),读checkpoint的capped score——但这是consistent(记录的是最终落盘的score),非污染,论证改为"cap在_record_gate之前应用→checkpoint存capped→calibration读capped,三者一致"。(R3) test_verify_cited_replays_still_blocking 依赖真实manifest文件——建议monkeypatch _load_spotlight_anchor_map 返回 mock dict('G2H67':'65831a0d')消除运行时数据依赖,避免CI清空manifest时退化为no-op。(R4) _run_critic 返回的 data 已经过 validate_agent_output('critic')(agent_review.py:100)Pydantic coerce(score int ge=1 le=10),plan test构造 data={'score':8} 直接喂 run_critic handler 是对的(L100只在'core score in d


**测试缺口：** 3个测试设计缺陷需修正:(T1)test_check_citations_accepts_valid_anchored_citation 断言 _check_citations(anything,None/{}) 返回 []——错误。正确断言:_check_citations(anything,None)→[](manifest缺失跳过),但 _check_citations(['G1H1'],{})→应含 FABRICATED(manifest存在但无citations=所有引用皆fabricated,与 _verify_cited_replays 既有空dict语义一致)。否则重构会削弱 master-path。(T2)test_verify_cited_replays_still_blocking_on_master_plan 依赖真实 core/results/spotlight_

### fix-9-structure-drift-ast [MEDIUM|architecture|low] ✅ SAFE

**Regression guardian diagnosis 落盘 experience_pool（跳过 fix_injection AST 迁移）**

fix-9 has two sub-items: (a) fix_injection AST migration, (b) regression_guardian_diagnosis going into experience_pool. Code verification reveals (a) is ALREADY MITIGATED — fix_verification.py (292 lines) provides authoritative AST/runtime verification for all 3 active fixes (subprocess probes + AST fallbacks), integrated as a BLOCKING quality gate in tool_gates.py:283-294. The audit overstated risk by treating fix_injection as the sole safety mechanism. Recommend SKIP (a). (b) is confirmed unaddressed: guardian_diagnosis goes from _run_regression_guardian() into the MCP result dict back to Or


<details><summary>当前代码状态（核查后）</summary>

```
## (a) fix_injection AST migration — ALREADY SUBSTANTIALLY DONE (audit overstated risk)

The audit says: "fix_injection 维护硬编码 search-and-replace...search 字符串不再匹配 → fix 静默 skipped...定时炸弹"

**Actual code reality (file:line verified):**

1. `fix_injection.py:159` uses `content.replace(patch.search, patch.replace, 1)` — fragile substring.
2. `fix_verification.py` (292 lines) is ALREADY the authoritative AST/runtime fix-present judgment:
   - `_verify_wheel()` (L101-132): subprocess probe `evaluate_5(wheel_cards)` + `_ast_wheel_literal_in_evaluate_5()` AST fallback (L135-155) for `{14,2,3,4,5}`.
   - `_verify_min_raise()` (L180-223): AST locate `min_raise_action` assignment, check `"+ 1"` in formula.
   - `_verify_total_hands()` (L226-258): subprocess probe `TOTAL_HANDS==70` + AST fallback.
3. `tool_gates.py:283-294`: `verify_fixes()` is BLOCKING — `fix_ok` is in `all_passed` condition.
4. Fix application callsites (`tool_gates.py:489`, `tool_planning.py:1469`, `code_verification.py:603`, `agent_review.py:346`) all try-apply silently.
5. `BOT-002b active=False` (fix_injection.py:91) is intentional dead template; tests assert inactive.

## (b) regression_guardian_diagnosis 进 experience_p
```
</details>


#### 改动点（2 处）

- **`web/core/tool_gates.py`** @ `Lines 913-921 in run_critic, insert after the regression_guardian dict assignment, before the try block at L922` [modify] — The core (b) fix. guardian_diagnosis was returned in the tool result to the Orchestrator but had 0 downstream consumers (grep confirms). Now it is written to experience_pool.md REC
- **`web/core/fix_injection.py`** @ `No code change needed — this is a decision NOT to modify fix_injection.py` [modify] — Audit overstated the risk. The 'ticking time bomb' framing assumes fix_injection's substring match is the sole safety mechanism, but fix_verification.py (AST/runtime) is the AUTHOR

<details><summary>完整 diff 详情</summary>


**`web/core/tool_gates.py`** @ `Lines 913-921 in run_critic, insert after the regression_guardian dict assignment, before the try block at L922` [modify]

现状:
```python
if guardian_diagnosis: result["regression_guardian"] = { "severity": guardian_diagnosis.get("severity", "minor"), "failure_stage": guardian_diagnosis.get("failure_stage", "unknown"), "recovery_recommendation": guardian_diagnosis.get("recovery_recommendation", ""), "diagnosis": guardian_diagnosis.get("diagnosis", ""), "root_cause": guardian_diagnosis.get("root_cause", ""), "confidence": guardian_diagnosis.get("confidence", "low"), } try:
```
改后:
```python
if guardian_diagnosis: result["regression_guardian"] = { "severity": guardian_diagnosis.get("severity", "minor"), "failure_stage": guardian_diagnosis.get("failure_stage", "unknown"), "recovery_recommendation": guardian_diagnosis.get("recovery_recommendation", ""), "diagnosis": guardian_diagnosis.get("diagnosis", ""), "root_cause": guardian_diagnosis.get("root_cause", ""), "confidence": guardian_diagnosis.get("confidence", "low"), } # (fix-9b) Write guardian diagnosis to experience_pool so [...]
```
*The core (b) fix. guardian_diagnosis was returned in the tool result to the Orchestrator but had 0 downstream consumers (grep confirms). Now it is written to experience_pool.md RECENT_LESSONS via the existing _append_experience_updates() helper, so the next-generation Master Architect sees regression root-cause constraints as cross-gen memory. The [GUARDIAN] label prefix makes entries greppable and distinguishable from archivist-sourced entries. The write is wrapped in try/except (non-critical; matches existing evidence-write pattern at lines 876-899).*


**`web/core/fix_injection.py`** @ `No code change needed — this is a decision NOT to modify fix_injection.py` [modify]

现状:
```python
### (a) fix_injection AST migration — ALREADY SUBSTANTIALLY DONE (audit conclusion overstated risk) The audit states: "fix_injection 维护硬编码 search-and-replace 补丁注册表...search 字符串不再匹配 → fix 静默 skipped...定时炸弹" Actual code reality: 1. `fix_injection.py` (204 lines) uses substring matching (`content.replace(patch.search, patch.replace, 1)`) — YES, this is fragile. 2. **BUT** `fix_verification.py` (292 lines) already exists as the AUTHORITATIVE fix-present judgment: - `verify_fixes(bot_dir)` runs [...]
```
改后:
```python
# SKIPPED — see rationale. fix_verification.py already provides authoritative # AST/runtime verification as a BLOCKING quality gate. Migrating fix_injection # to AST would be code-cleanliness with no safety improvement, but introduces # refactor risk (the existing substring matching works correctly when applied; # verify_fixes catches any post-refactor mismatches).
```
*Audit overstated the risk. The 'ticking time bomb' framing assumes fix_injection's substring match is the sole safety mechanism, but fix_verification.py (AST/runtime) is the AUTHORITATIVE gate, already BLOCKING in tool_gates.py:283-294. Migrating fix_injection to AST is pure code-cleanliness (try-apply passes are correct; if they skip, verify_fixes catches it). Risk of AST refactor > reward. Recommend SKIP.*

</details>


#### 测试（3 个）

- `web/tests/test_audit_schemas.py::test_name` — When critic score < 4 and guardian returns a diagnosis, verify _append_experience_updates is called with a [GUARDIAN]-pr
- `web/tests/test_audit_schemas.py::test_name` — When _append_experience_updates raises (e.g. file lock failure), run_critic must still return successfully with result['
- `web/tests/test_audit_schemas.py::test_name` — When critic score >= 4 (advisory approved), _append_experience_updates is NOT called

#### 向后兼容
"_append_experience_updates" is an existing internal function in tool_commit.py (not exported to public API); its signature is unchanged. Callers of run_critic are the orchestrator via MCP — the result dict gains no new keys (additive experience_pool.md entry only). Tests already check result['regression_guardian'] exists for low scores; no existing test asserts experience_pool contents after crit


#### 风险
- Fix-injection AST migration (a) is SKIPPED: Audit overstated risk. verify_fixes() is already the authoritative AST/runtime gate integrated as BLOCKING
- Guardian diagnosis consolidation noise: The consolidator LLM runs every 3 gens and merges all RECENT_LESSONS entries. GUARDIAN entries (describing fai
- Guardian rarely fires: Only triggers when critic score < 4. In practice, critic scores have been 4-7 range (advisory accepted). This means the experie
- Consolidator may strip GUARDIAN entries before next Master sees them: Current consolidator processes ALL RECENT_LESSONS uniformly, no tag-based filter


#### 验证步骤
```
cd web && python -m pytest tests/test_audit_schemas.py -v -k guardian
cd web && python -m pytest tests/test_logic_fix_verification.py -v
cd web && python -m pytest tests/ -v
grep -n '_append_experience_updates' web/core/tool_gates.py
```


#### 回滚
Remove the 18 lines added to tool_gates.py after the regression_guardian dict assignment (the _append_experience_updates block). No other files touched. One-line git revert.


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：✅
- 测试充分：✅

**修正要点：** 核查结论: plan 可直接落地, 无阻断问题。

(1) 全部 file:line 已亲自核实准确:
- fix_injection.py:159 = `content.replace(patch.search, patch.replace, 1)` 确为脆弱 substring (verified)
- fix_verification.py: _verify_wheel L101-132 / _ast_wheel_literal_in_evaluate_5 L135-155 / _verify_min_raise L180-223 / _verify_total_hands L226-258 全部 AST+runtime probe 真实存在 (verified)
- tool_gates.py:283-294 verify_fixes() 确在 all_passed BLOCKING 条件中(fix_ok @ L293) (verified)
- 4 个 apply_known_fixes 静默 callsites 全部确认: tool_gates.py:489, agent_review.py:346, tool_planning.py:1469, code_verification.py:603 (verified)
- tool_gates.py:832 guardian_diagnosis=None 初始化 / L833 if not advisory_approved / L845 if score_num<4 嵌套 (verified)
- tool_gates.py:876-899 既有 evidence-write pattern + tool_commit.py:266 _append_experience_updates 签名 (verified


**测试缺口：** 测试总体充分, 可直接扩展 test_audit_schemas.py:219-342 既有 TestRunCriticRegressionGuardianInline 套件 (其 _patch_critic_dependencies harness + _call helper 已 mock 全部 run_critic 依赖)。但需补一项: plan 的 3 个测试只覆盖了 run_critic 路径, 没有断言写入的 experience_pool 内容格式(generation_assessment 值、RECENT_LESSONS section 位置)。建议测试 #1 同时读取 tmp_path 的 experience_pool.md 断言 [GUARDIAN] 字符串与 root_cause/recovery_recommendation 内容真实落盘(参考 test_pip


---


## 3. Batch 3 — 速度 + 方向治理（6-10h）

> fix-4/5 都改 tool_planning.py 和 tool_eval.py 的跨代状态逻辑，合批避免 merge 冲突。fix-4 砍 cycle 耗时(79→~50min)是验证所有其他修复的前提。fix-5 的三重条件依赖 fix-4 的 stage_durations 数据。fix-7(新建 debug_worker_prompt.md)独立但 effort 相近。fix-6 需 rework fingerprint 方案(decision_tester 无 7 维 key)。

### fix-4-event-driven-precommit [HIGH|architecture|low] ⚠️ 需修正

**precommit poll→event-driven + run_master idempotency guard(砍 cycle 耗时)**

本 fix 治理演进速度(实测 79min/cycle, precommit 30.4min mean)。核查后审计结论基本属实但 1 处需修正:scheduler 路径 precommit 30/31 stall(非 9min 浪费, 实际更糟)。落地分两阶段——Phase 1(low/独立): (a) run_master 入口加代码级 idempotency guard(检测 checkpoint 已有 master_plan+stage>=master_planned 则直接返回缓存 plan, 复用 precommit 的 idempotent_cache 范式), 砍掉 10 次 2x run_master 重复 LLM 调用(orchestrator.md:43 prompt-only 禁令对控制流 agent 不可靠, 已证); (c) 把 SCHEDULER_STALL_ROUNDS 从 max(60,n*8)=640s 容忍降到 max(24,n*3)=240s(每次 stall 省 ~6.7min, 仍 > 单 battle 62-145s 避免误触发), + checkpoint 新增 stage_durations 字段支撑 ROI 排序。Phase 2(后续/medium): (b) event-driven precommit——诚实指出 daemon


<details><summary>当前代码状态（核查后）</summary>

```
核查后代码现实(已 grep + Read 实证):

**1. run_master 无缓存命中返回 (tool_planning.py:393-903)**
- 入口 L393 `async def run_master(args)`。已有 guards: next_v 对齐(L414-442, 用 `read_pipeline_checkpoint()` 权威对齐 stale next_v)、master_fail 硬上限(L453-501, `_master_fails >= MAX_MASTER_TOTAL_FAILURES=4` → force-abandon)。
- **关键缺口**: L774 `data = await _run_master_analysis(...)` 每次都执行 LLM 调用, 即使 checkpoint 已存 `master_plan`。成功路径 L887 `write_pipeline_checkpoint(..., "master_planned", master_plan=data, reset_audit_attempt=True)`, 成功返回 L902 `result = {"plan": data, "logs": ui.get_output()}`。
- grep `idempotent|cached.*plan|already.*plan` 在 tool_planning.py: 仅 L555 `# Idempotent: guarded by CROSS_GEN_MARKER`(指 cross-gen 约束块去重, 非 master_plan 缓存)。run_master 入口无任何 `master_plan` 存在性早返回。
- **实证重复**: system_events.jsonl `pipeline.redundant_tool_call` 67 次, 多个 `"Orchestrator called run_master 2x in one cycle"` (run_id 142#0@ts1782031534, 143#0@ts1782035163, stage=direction_audited)。审计 "10 次 2x" 属实。

**2. precommit scheduler stall (tool_eval.py:411-490)**
- L455 `poll_interval = 5.0`; L466 `SCHEDULER_STALL_ROUNDS = max(60, n_games * 8)` (n=16→128 polls×5s=640s 容忍)。
- L478-489 `consecutive_stall >= SCHEDULER_STALL_ROUNDS` → br
```
</details>


#### 改动点（3 处）

- **`web/core/tool_planning.py`** @ `run_master, after L442 (next_v 对齐块之后), before L443 (stagnation_info 读取)` [modify] — 止血: 消除 run_master 2x/cycle 重复 LLM 调用(实测 run_id 142/143 等)。LLM 间歇违反 orchestrator.md:43 的 prompt-only 禁令, prompt-only 约束对控制流 agent 不可靠(审计 #4 已证)。代码级检测 checkpoint.master_plan 存在 + sta
- **`web/core/tool_eval.py`** @ `run_precommit_eval, L466 SCHEDULER_STALL_ROUNDS` [modify] — 降低 scheduler stall 容忍: 实测 31 次 scheduler 路径 precommit 中 30 次 stall(每次 640s=10.7min 空等)然后 fallback serial。将 max(60, n*8) 降为 max(24, n*3) 把空等窗从 640s 砍到 240s, 每次最多省 ~6.7min。240s 仍 > 单
- **`web/core/evolution_infra.py`** @ `write_pipeline_checkpoint signature L297 + state dict L451` [modify] — 为 ROI 排序(审计 #4c)提供 stage 级耗时持久化。当前各 stage elapsed_sec 只在 system_event 一次性 log, 不进 checkpoint, 跨重启/分析不可用。新增 stage_durations dict 字段, 各 pipeline tool 在 _record_gate 或 write_pipeline_

<details><summary>完整 diff 详情</summary>


**`web/core/tool_planning.py`** @ `run_master, after L442 (next_v 对齐块之后), before L443 (stagnation_info 读取)` [modify]

现状:
```python
next_v = _entry_next_v if _entry_ckpt.get("source_v") is not None: source_v = _entry_ckpt["source_v"] except Exception: pass stagnation_info = args.get("stagnation_info", "...") match_analysis = args.get("match_analysis", "")
```
改后:
```python
next_v = _entry_next_v if _entry_ckpt.get("source_v") is not None: source_v = _entry_ckpt["source_v"] except Exception: pass # ── Idempotency guard (fix-4 Phase 1a): if a valid master_plan already # exists in the checkpoint for this (next_v, source_v), return the cached # plan immediately instead of re-burning an LLM call. system_events.jsonl # shows 67x pipeline.redundant_tool_call including multiple 'run_master 2x # in one cycle' (run_id 142#0, 143#0). orchestrator.md:43 forbids re- [...]
```
*止血: 消除 run_master 2x/cycle 重复 LLM 调用(实测 run_id 142/143 等)。LLM 间歇违反 orchestrator.md:43 的 prompt-only 禁令, prompt-only 约束对控制流 agent 不可靠(审计 #4 已证)。代码级检测 checkpoint.master_plan 存在 + stage>=master_planned + 版本匹配则直接返回缓存。放在 next_v 对齐之后(确保用权威 next_v 查 checkpoint)与 master_fail 硬上限之前(缓存命中无需检查失败计数)。*


**`web/core/tool_eval.py`** @ `run_precommit_eval, L466 SCHEDULER_STALL_ROUNDS` [modify]

现状:
```python
poll_interval = 5.0 # root-cause-audit 2026-06-21: 2s→5s 减少 fcntl 锁竞争 ... SCHEDULER_STALL_ROUNDS = max(60, n_games * 8) # 自适应：n_games=16 → 128 polls × 5s ≈ 640s 容忍
```
改后:
```python
SCHEDULER_STALL_ROUNDS = max(24, n_games * 3) # fix-4 Phase 1c: 128→48 polls (n=16). 实测 daemon 完成一个 mirror battle 需 62-145s; 旧 128 polls×5s=640s 容忍窗口过长, 30/31 scheduler 路径 stall→fallback (system_events). 48 polls×5s=240s 仍 > 单 battle 完成时间, 避免误触发但显著缩短空等窗。
```
*降低 scheduler stall 容忍: 实测 31 次 scheduler 路径 precommit 中 30 次 stall(每次 640s=10.7min 空等)然后 fallback serial。将 max(60, n*8) 降为 max(24, n*3) 把空等窗从 640s 砍到 240s, 每次最多省 ~6.7min。240s 仍 > 单 battle 完成时间(62-145s)避免误触发。这是 Phase 1 中成本最低、收益最直接的改动, 不依赖 event-driven 基础设施。*


**`web/core/evolution_infra.py`** @ `write_pipeline_checkpoint signature L297 + state dict L451` [modify]

现状:
```python
def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None, ..., timeout_extensions=None, touch_stage_timestamp=False): ... state = { "next_v": next_v, "source_v": source_v, "stage": stage, ... "last_update_ts": now_ts, }
```
改后:
```python
def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None, ..., timeout_extensions=None, touch_stage_timestamp=False, stage_durations=None): ... # Merge stage_durations (fix-4 Phase 1c): dict of stage_name -> elapsed_sec existing_stage_durations = existing.get("stage_durations", {}) if existing else {} if stage_durations: existing_stage_durations.update(stage_durations) state = { ... "last_update_ts": now_ts, "stage_durations": existing_stage_durations, }
```
*为 ROI 排序(审计 #4c)提供 stage 级耗时持久化。当前各 stage elapsed_sec 只在 system_event 一次性 log, 不进 checkpoint, 跨重启/分析不可用。新增 stage_durations dict 字段, 各 pipeline tool 在 _record_gate 或 write_pipeline_checkpoint 时传入 {stage: elapsed_sec}, 持久化到 checkpoint 供 Master/archivist/cycle 分析消费。向后兼容: 旧 checkpoint 无此字段时 .get({}) 默认空。*

</details>


#### 测试（3 个）

- `web/tests/test_mcp_pipeline.py::test_name` — 预置 checkpoint stage=master_planned + master_plan={tasks:[...]} 后调用 run_master(source_v=99, next_v=100), 断言返回 result['ide
- `web/tests/test_mcp_pipeline.py::test_name` — 无缓存(stage='direction_audited' 或 master_plan=None)时 run_master 正常进入 LLM 路径; monkeypatch _run_master_analysis 返回固定 plan, 断
- `web/tests/test_mcp_pipeline.py::test_name` — stage='archived'/'abandoned'/'timed_out' 时即使有 master_plan 也不命中缓存(因为 stage 不在白名单), 确保 abandoned generation 不复活旧 plan

#### 向后兼容
调用方(orchestrator LLM agent / control.py /api/control/tool/run_master 路由)无需同步改: idempotency 返回结构与正常 run_master 成功路径一致 ({"plan":...,"logs":...,"idempotent_cache":true}), 只是多一个 idempotent_cache 布尔字段(向后兼容, 老消费方忽略即可)。不修改 run_master 的 @tool 签名/参数列表/返回 JSON schema, 仅插入一个早返回分支。stage_durations 是 checkpoint 的纯增量字段(读端 _matching_checkpoint 已用 .get 容错), 不影响任何现有 gate 逻辑。


#### 风险
- idempotency guard 误命中: 若 orchestrator LLM 在 master 已 plan 后因 critic 反馈需要 NEW plan(代码被 rework), 缓存返回旧 plan 会卡死。Mitigation: guard 条件含 stage ∈ {master_pl
- guard 漏判 stale plan: 若 checkpoint 的 master_plan 来自一个已 abandoned 但未清的 generation。Mitigation: 依赖现有 prepare_next_gen/abandon_generation 清 checkpoint 的不变量
- SCHEDULER_STALL_ROUNDS 下调(n*8→n*3)可能在高负载下误触发 fallback: daemon 正常跑 battle 需 62-145s, 若 CPU 饱和一个 battle 超 240s 会被误判 stall。Mitigation: 240s 仍是单 battle 完成
- stage_durations 字段膨胀: 若每个 gate 都写 elapsed_sec, checkpoint dict 增长。Mitigation: 字段是 8 个 stage 名->float 的小 dict(<200 字节), 相比 master_plan(几 KB)可忽略; 且 writ


#### 验证步骤
```
运行全量回归: cd web && python -m pytest tests/ -v (确认 1037 passed 不回归, 新增 idempotency 测试通过)
单测验证 idempotency guard: cd web && python -m pytest tests/test_mcp_pipeline.py::TestRunMasterIdempotency -v
grep 确认 idempotency 命中事件: 启动一次 cycle 后 grep 'master_idempotent_cache_hit' web/core/results/system_events.jsonl, 应出现且对应 run_master 不再有 2x redundant_tool_call
grep 确认 stall 下降: 运行若干代后统计 'scheduler_stall' vs 'scheduler_complete' 比例, 对比改动前(30 stall/1 complete) 应明显改善
验证 stage_durations 持久化: 跑一代到 commit 后 cat web/core/results/pipeline_state.json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("stage_durations",{}))', 应含 master/workers/quality/review/critic/precommit 各 stage 的 elapsed_sec
```


#### 回滚
三个改动均独立可单独 revert。Phase 1a idempotency guard: 删除 tool_planning.py 中新增的 if _idem_ckpt... 块(约 35 行), run_master 回到每次都 LLM 调用的原始行为。Phase 1c SCHEDULER_STALL_ROUNDS: 改回 max(60, n_games*8)。stage_durations: 删除 evolution_infra.py 的 stage_durations 参数和 state dict 字段(读端 .get 容错, 删除写端后旧 checkpoint 的残留字段不影响)。由于


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：✅
- 测试充分：❌

**🔴 阻断点：** Test design needs correction: (1) monkeypatch must target `tool_planning._run_master_analysis` (the module-level import at L17), not `agent_master._run_master_analysis`; (2) tests calling `_handler(args)` directly must parse the MCP wrapper format `json.loads(retval["content"][0]["text"])` to get the inner result; (3) the idempotency guard also needs to monkeypatch `_get_ui()`, `_set_pipeline_status()`, `log_system_event()` and the various data-loading functions (match_analysis, performance_verification, etc.) that run between L443-773 to avoid side effects when the cache miss path is tested. 


**遗漏调用方：** stage_durations callers not specified: the plan adds `stage_durations` kwarg to `write_pipeline_checkpoint` (evolution_infra.py:297) but does not enumerate which 18+ call sites should populate it. After the change, `stage_durations` will always be empty `{}` until callers are updated. This is accept


**修正要点：** 1. Guard insertion point verified: L442=pass(end of try/except), L443=stagnation_info. _matching_checkpoint() is already imported in tool_planning.py(L21) and handles next_v+source_v matching — reuse it. 2. The idempotency guard does NOT call write_pipeline_checkpoint (it's a read-only cache hit), so no gate ledger interaction. 3. SCHEDULER_STALL_ROUNDS: max(24,n*3) with n_games=8 gives 24 rounds x 5s = 120s, which is 0.8x of max single-battle time (145s). Tight but fallback serial is zero-regression, so worst case is less scheduler utilization not correctness issue. 4. stage_durations is purely additive — no existing consumer reads it, .get({}) default prevents KeyError. 5. The plan's test for dead-stage-not-cached should test stage='timed_out' and stage='abandoned' (the unranked stages i


**测试缺口：** Plan's 3 tests cover the core idempotency behavior (cache hit, cache miss, dead stage) but are underspecified: (1) no test for the SCHEDULER_STALL_ROUNDS constant change — acceptable since it's a constant-only change; (2) no test for stage_durations — acceptable as Phase 1 infra; (3) the dead-stage test should include 'timed_out' (the most common dead stage in production) not just 'archived'/'aban

### fix-5-critic-cross-gen-pivot [HIGH|both|medium] ⚠️ 需修正

**critic/direction_audit 跨代方向 pivot 升 binding(三重条件, 不回退字面 hard gate)**

把 tool_planning.py:376-378 的 exhausted-direction advisory 升级为"三重条件才 binding"的 cross-gen pivot gate：confidence=high AND 命中语义轴 AND 历史 ≥2 次连续 precommit fail → 写进 plan_errors(而非 warnings),复用现有 MASTER_EXHAUSTED force-abandon 机制触发 master 重 plan/换 source。新增 fcntl-safe 的 exhausted_history.jsonl 在 precommit 末尾记录 (source_v, axis, confidence, precommit_passed),作为客观 outcome 信号(critic_calibration.jsonl 的 rating_delta 98% 为 0 不可用)。_validate_master_plan 加 direction_audit/source_v 参数,逃生口让"新 fn + 新 opp signal"的结构性 novel plan 永远豁免,绝不回退到字面 token hard gate(会复发 7dc4c3f 修的误杀 bug)。改 6 处(2 新 helper + 1 新 gate fn + 1 


<details><summary>当前代码状态（核查后）</summary>

```
实际核查(file:line 真实可 grep):

1. tool_planning.py:367-378 — exhausted-direction 当前是 advisory:
```
367:    # Check worker prompts against exhausted directions from experience pool.
368:    # This is a HARD constraint: plans matching exhausted directions are rejected.   ← 注释 stale,与实现矛盾
369-378: ... warnings.append(f"Task {i}: worker prompt matches an EXHAUSTED direction from experience pool (advisory). ...")
378:                # advisory only — no longer blocks the plan
```

2. tool_planning.py:1157-1169 — `_EXHAUSTED_DIRECTION_TOKENS` 只剩 3 词 {parameter, tuning, commitment}(其余因误伤被注释删),hard path(`require_direction_token=True`)几乎永不触发:
```
1157: _EXHAUSTED_DIRECTION_TOKENS = frozenset({
1158:     "parameter", "tuning", "commitment",
1159:     # NOTE: "mechanism", "canonical", "archetype", "refactor" REMOVED ...
```

3. tool_planning.py:71-77 — direction_audit_payload 已含 confidence + exhausted_directions + resolved,且写进 checkpoint(direction_audited stage)。`exhausted_directions` 是语义短语列表(direction_auditor_prompt.md:47 例 `["fold threshold tuning", "EQR adjustment"]`)。

4. tool_planning.py:261 — `_validate_master_plan(plan, next_v=None, precomputed_exhausted_keywords=None)` 当前不接收 direction_aud
```
</details>


#### 改动点（7 处）

- **`web/core/tool_planning.py`** @ `near MAX_MASTER_TOTAL_FAILURES (L156), add module-level constants` [add] — 集中三重条件阈值 + 新文件路径 + 逃生口 regex。放 tool_planning 顶部与 MAX_MASTER_TOTAL_FAILURES 并列,语义聚合。RESULTS_DIR 已 import。
- **`web/core/tool_planning.py`** @ `new module-level helpers after _bump_master_fail_count (~L181)` [add] — 跨代状态持久化,复用已验证的 append_locked_jsonl(research_governance.py 同款)+ locked_file(cap + read)。fcntl 与项目约定一致。
- **`web/core/tool_planning.py`** @ `new gate fn before _validate_master_plan (~L260)` [add] — 三重条件 gate 主体。condition(3) 用历史记录里
- **`web/core/tool_planning.py`** @ `_validate_master_plan signature L261` [modify] — 新增 keyword-only 可选参数。默认 None 保持旧行为(零回归)，run_master 调用处会传入。source_v 用于语义轴匹配上下文。
- **`web/core/tool_planning.py`** @ `_validate_master_plan body, replace L366-378 exhausted-keyword block + append cross-gen pivot check` [modify] — 把 audit 建议的三重条件 gate 接到 plan_errors(不是 warnings)。保留原 advisory 块不动(向后兼容 + 仍是软召回)。逃生口 _plan_is_structurally_novel 在 errors.append 之前短路。
- **`web/core/tool_planning.py`** @ `run_master L870 — pass direction_audit + source_v into validate` [modify] — 把已持久化在 checkpoint 的 direction_audit(含 confidence/exhausted_directions)透传给 validate。direction_audit 本地变量在 run_master 作用域已存在(L446-512 解析)。None 时 gate 自动 no-op。
- **`web/core/tool_eval.py`** @ `tool_eval.py run_precommit_eval — record cross-gen exhausted outcome (near L948-955, the A6 block)` [modify] — precommit 是 outcome 信号的天然记录点(passed/failed 已在作用域)。axis 取自本代 direction_audit.exhausted_directions[0](语义轴)。即使 passed=True 也记录(pass 会重置连续 fail 计数,语义正确)。fcntl-safe。

<details><summary>完整 diff 详情</summary>


**`web/core/tool_planning.py`** @ `near MAX_MASTER_TOTAL_FAILURES (L156), add module-level constants` [add]

现状:
```python
# (no constant near MAX_MASTER_TOTAL_FAILURES = 4 at tool_planning.py:156)
```
改后:
```python
# fix-5: cross-gen exhausted-direction PIVOT gate. Unlike the intra-gen # experience-pool keyword check (advisory, tool_planning.py:376-378), this is a # BINDING gate that fires only when THREE conditions all hold, so it cannot # reproduce the 7dc4c3f false-positive bug (literal-token hard gate killed # legitimate novel plans). See docs/evolution-arch-prompt-audit-jun23.md #5. MAX_CROSSGEN_FAIL_RUNS = 2 # >=2 consecutive precommit FAILS on same axis/source MAX_EXHAUSTED_HISTORY = 200 # cap [...]
```
*集中三重条件阈值 + 新文件路径 + 逃生口 regex。放 tool_planning 顶部与 MAX_MASTER_TOTAL_FAILURES 并列,语义聚合。RESULTS_DIR 已 import。*


**`web/core/tool_planning.py`** @ `new module-level helpers after _bump_master_fail_count (~L181)` [add]

现状:
```python
# (no such helper exists; precommit outcome is only surfaced to orchestrator via result dict and research_governance.record_precommit_outcome at tool_eval.py:952)
```
改后:
```python
def _append_exhausted_history(source_v, axis, confidence, precommit_passed, next_v=None): """Append one cross-gen exhausted-direction record. fcntl-safe via append_locked_jsonl.""" try: from evolution_infra import append_locked_jsonl append_locked_jsonl(EXHAUSTED_HISTORY_FILE, { "source_v": source_v, "next_v": next_v, "axis": axis, "confidence": confidence, "precommit_passed": bool(precommit_passed), "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), }) # cap file size: keep last [...]
```
*跨代状态持久化,复用已验证的 append_locked_jsonl(research_governance.py 同款)+ locked_file(cap + read)。fcntl 与项目约定一致。*


**`web/core/tool_planning.py`** @ `new gate fn before _validate_master_plan (~L260)` [add]

现状:
```python
# (no cross-gen check exists; _validate_master_plan only does experience-pool keyword matching at L366-378)
```
改后:
```python
def _check_exhausted_crossgen_pivot(direction_audit, source_v, next_v=None): """fix-5 cross-gen pivot gate. Returns (block: bool, reason: str|None). BINDING only when ALL THREE hold (prevents 7dc4c3f false-positive recurrence): (1) direction_audit confidence == 'high' AND repetition_detected True; AND (2) the proposed plan's direction axis matches an exhausted_directions entry; AND (3) >=MAX_CROSSGEN_FAIL_RUNS consecutive prior precommit FAILS recorded for the SAME semantic axis (cross- [...]
```
*三重条件 gate 主体。condition(3) 用历史记录里*


**`web/core/tool_planning.py`** @ `_validate_master_plan signature L261` [modify]

现状:
```python
def _validate_master_plan(plan, next_v=None, precomputed_exhausted_keywords=None):
```
改后:
```python
def _validate_master_plan(plan, next_v=None, precomputed_exhausted_keywords=None, direction_audit=None, source_v=None):
```
*新增 keyword-only 可选参数。默认 None 保持旧行为(零回归)，run_master 调用处会传入。source_v 用于语义轴匹配上下文。*


**`web/core/tool_planning.py`** @ `_validate_master_plan body, replace L366-378 exhausted-keyword block + append cross-gen pivot check` [modify]

现状:
```python
# Check worker prompts against exhausted directions from experience pool. # This is a HARD constraint: plans matching exhausted directions are rejected. exhausted_keywords = precomputed_exhausted_keywords if precomputed_exhausted_keywords is not None else _extract_exhausted_keywords() if exhausted_keywords: for i, task in enumerate(tasks): prompt_text = ( task.get("worker_prompt", "") + " " + task.get("instruction", "") + " " + str(task.get("targeted_failure", "")) ).lower() if [...]
```
改后:
```python
# Check worker prompts against exhausted directions from experience pool. # (advisory only — 7dc4c3f intentionally demoted from hard gate to avoid # false-positive blocks on legitimate novel plans that share generic words.) exhausted_keywords = precomputed_exhausted_keywords if precomputed_exhausted_keywords is not None else _extract_exhausted_keywords() if exhausted_keywords: for i, task in enumerate(tasks): prompt_text = ( task.get("worker_prompt", "") + " " + task.get("instruction", "") [...]
```
*把 audit 建议的三重条件 gate 接到 plan_errors(不是 warnings)。保留原 advisory 块不动(向后兼容 + 仍是软召回)。逃生口 _plan_is_structurally_novel 在 errors.append 之前短路。*


**`web/core/tool_planning.py`** @ `run_master L870 — pass direction_audit + source_v into validate` [modify]

现状:
```python
plan_errors, plan_warnings = _validate_master_plan(data, next_v=next_v, precomputed_exhausted_keywords=_exhausted_kw)
```
改后:
```python
# fix-5: pass the checkpoint's direction_audit (carries confidence + # exhausted_directions semantic axes) so _validate_master_plan can run the # cross-gen triple-condition pivot gate. Falls back to None (no-op) if absent. _da_for_validate = None try: _ckpt_da = _matching_checkpoint(next_v, source_v) or {} _da_for_validate = _ckpt_da.get("direction_audit") or direction_audit except Exception: _da_for_validate = direction_audit plan_errors, plan_warnings = _validate_master_plan( data, [...]
```
*把已持久化在 checkpoint 的 direction_audit(含 confidence/exhausted_directions)透传给 validate。direction_audit 本地变量在 run_master 作用域已存在(L446-512 解析)。None 时 gate 自动 no-op。*


**`web/core/tool_eval.py`** @ `tool_eval.py run_precommit_eval — record cross-gen exhausted outcome (near L948-955, the A6 block)` [modify]

现状:
```python
# A6 (research_governance, evolution-plan-refresh-jun21): feed the precommit # outcome back into any web-derived candidates applied to this bot version, and # trigger a retrieval cooldown if a web-injected gen FAILED (Ratchet anti-pollution). try: from research_governance import record_precommit_outcome record_precommit_outcome(v, passed, next_v=v) except Exception: pass
```
改后:
```python
# A6 (research_governance, evolution-plan-refresh-jun21): feed the precommit # outcome back into any web-derived candidates applied to this bot version, and # trigger a retrieval cooldown if a web-injected gen FAILED (Ratchet anti-pollution). try: from research_governance import record_precommit_outcome record_precommit_outcome(v, passed, next_v=v) except Exception: pass # fix-5 (audit #5): record cross-gen exhausted-direction outcome so the next # generation's [...]
```
*precommit 是 outcome 信号的天然记录点(passed/failed 已在作用域)。axis 取自本代 direction_audit.exhausted_directions[0](语义轴)。即使 passed=True 也记录(pass 会重置连续 fail 计数,语义正确)。fcntl-safe。*

</details>


#### 测试（7 个）

- `web/tests/test_arch_fixes_regression.py::test_name` — 三重条件全满足时 plan_errors 含 'cross-gen exhausted-direction PIVOT gate' 消息。构造 direction_audit={confidence:'high',repetition_de
- `web/tests/test_arch_fixes_regression.py::test_name` — confidence='low' 或 repetition_detected=False 时 gate 不触发(返回原 errors)。证明不复发 7dc4c3f 误杀:低置信/无重复=不 block。
- `web/tests/test_arch_fixes_regression.py::test_name` — history 含 [fail, pass, fail] 时 consec_fails=1(被 pass 重置)<MAX_CROSSGEN_FAIL_RUNS=2 → 不 block。验证连续计数语义正确。
- `web/tests/test_arch_fixes_regression.py::test_name` — 三重条件全满足但 plan.tasks[*].worker_prompt 含 'def _new_opp_bet_size_profiler' + 'new opponent signal' → _plan_is_structurally_
- `web/tests/test_arch_fixes_regression.py::test_name` — direction_audit=None(默认)时行为与旧版完全一致(只可能因 source-override/boundary 报错,不因 pivot gate 报错)。零回归保证。
- `web/tests/test_logic_mcp.py::test_name` — _append_exhausted_history 写 3 条后 _load_exhausted_history 返回 3 条且字段完整(source_v/axis/confidence/precommit_passed)。再写超过 MAX

#### 向后兼容
_检查函数签名变更: `_validate_master_plan` 新增 keyword-only 参数 `direction_audit=None`(默认 None 保持旧行为)。现有 4 处调用方影响: (1) tool_planning.py:870 run_master 传新参(本 fix 同步改); (2) test_arch_fixes_regression.py 3 处 test 只传 plan/next_v 不传 direction_audit → 默认 None,三重条件不会触发,断言仍 pass(零回归); (3) test_logic_phase3_nemesis_mapelites.py monkeypatch 整个函数为 lambda,不受影响; (4) test_root_cause_fixes.py 只 assert callable/source 文本,不


#### 风险
- 误杀 novel plan 复发(7dc4c3f 反向 bug):mitigation=三重条件 AND(非字面 token)+ 逃生口 _plan_is_structurally_novel(新 fn + 新 opp signal 豁免)+ condition(1) 要求 confidence==
- cross-gen fail 计数语义漂移:consec_fails 用反向遍历 history + pass 即 break。若 daemon 评估延迟导致 precommit infra_timeout(非真 fail,tool_eval.py:968 infra_only_timeout 分支
- exhausted_history.jsonl 无限增长:mitigation=_append_exhausted_history 内置 MAX_EXHAUSTED_HISTORY=200 cap(write-back trim),与 match_replay/exp_audit_io 一致的 ca
- direction_audit 缺失时 gate 静默 no-op:若某代 direction_auditor LLM 崩溃(direction_auditor.py:31 safe-default repetition=False),history 不记录 axis → gate 永不 fire。


#### 验证步骤
```
cd web && python -m pytest tests/test_arch_fixes_regression.py -v -k 'crossgen_pivot or axis_token_overlap' (新增 7 个 test)
cd web && python -m pytest tests/test_logic_mcp.py -v -k 'exhausted_history' (新增 roundtrip test)
cd web && python -m pytest tests/ -v (全量零回归,必须保持 1037 passed;重点看 test_arch_fixes_regression.py 3 个旧 source-override test 仍 pass——证明签名向后兼容)
grep -n 'def _validate_master_plan' web/core/tool_planning.py 确认新签名 (...,direction_audit=None,source_v=None)
grep -n 'exhausted_crossgen_pivot_block' web/core/tool_planning.py web/core/tool_eval.py 确认 gate 接线 + 记录点都在
python -c "import ast; ast.parse(open('web/core/tool_planning.py').read()); ast.parse(open('web/core/tool_eval.py').read()); print('py_compile OK')"
```


#### 回滚
单 commit 回滚 `git revert <commit>` 即可(纯 additive,无 schema/data migration)。exhausted_history.jsonl 残留可保留(无消费者后无害)或 `rm web/core/results/exhausted_history.jsonl`。无 checkpoint schema 变更(direction_audit 字段未动)。回滚后 tool_planning.py:376-378 回到 advisory-only baseline,v138-v165 同轴循环现状不变(即回到审计前状态,无新风险)。


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：❌
- 测试充分：❌

**🔴 阻断点：** 有两个阻断性问题需要在实施前修正：

**BLOCKER 1 (严重): infra_only_timeout 污染 cross-gen fail 计数**
计划的风险分析声称 "tool_eval L968 分支已设 passed=True(infra 不算 fail,只 directive 重试 precommit),故不会污染计数" —— 这是**事实错误**。
实测：`tool_eval.py:905` `passed = len(blockers)==0`，`tool_eval.py:912` `infra_only_timeout = (not passed) and (not regression_blockers) and bool(infra_blockers)`。
L948 `record_precommit_outcome(v, passed)` 在 L968 infra 分支**之前**运行，此时 `passed=False`（因为 infra_blockers 非空，blockers 非空）。
infra_only_timeout 分支从未将 passed 改为 True —— 它只是改了 result["directive"] 和 result["infra_retry"]。
因此：如果第 7 项修改使用同一个 `passed` 变量记录 precomm


**遗漏调用方：** 1. `agent_master.py:128` 调用 `_validate_master_plan(data)` 不传 keyword args — backward_compat 分析遗漏了此调用点（计划列了 4 处，实际是 5 处调用）。虽然 signature 兼容（新参数 keyword-only 且默认 None），但 agent_master 和 run_master 对同一 plan 可能产生不一致判定（agent_master 无 pivot gate，run_master 有），需要明确这是设计意图而非 bug。

2. `tool_eval.py:948` 处已存在的 `


**修正要点：** 核查后的修正要点：

**必须修正:**
1. 计划第 7 项修改中的 cross-gen outcome 记录：必须排除 infra_only_timeout 场景。在 tool_eval.py run_precommit_eval 中，`passed` 在 infra_only_timeout 时为 False（因 infra_blockers 非空导致 blockers 非空），且 infra 分支(L968)在 record 检查点(L948)之后。解决方案：记录时用 `_outcome_is_genuine_fail = not passed and not infra_only_timeout` 作为 crossgen 的 fail 信号。风险分析中 "L968 已设 passed=True" 的陈述是事实错误，必须删除并替换为正确分析。

2. backward_compat 列表必须补充 `agent_master.py:128` 调用点。虽然 signature 兼容，但需要明确声明：agent_master 的验证不含 pivot gate 是 by design（agent_master 的循环 retry 只做硬约束 source-override 检查），不是遗漏。

**建议修正:**
3. 计划提到 "RESULTS_DIR 已 import" 用于模块级常量 —— 实际上 tool_planning.py 中 RESULTS_DIR 是函数内按需 import（L570/584/619/712/948），不是模块级。新增模块级常量时需要在文件顶部添加 `from evolution_infra import RESULTS_DIR`，或沿用函数内 import 模式。建议沿用函数内 import 以保持一致性（避免顶部 circular im


**测试缺口：** 1. **缺少 infra_only_timeout 边界测试**：计划的 7 个测试中没有一个覆盖 infra-only timeout 场景。需要一个测试验证：当 precommit 因 infra timeout 失败（not regression）时，cross-gen exhausted_history 记录中 precommit_passed 应为 True（不算真 fail），consec_fails 计数不受影响。这直接关联 BLOCKER 1。

2. **缺少 agent_master.py:128 路径的回归测试**：计划测试都通过 `_validate_master_plan` 直接调用。没有测试验证 agent_master.py 的调用路径（无 direction_audit 参数）不会因新增参数而报错。虽然 signature 兼容意味着理论上没问题，但应该有

### fix-7-deepevolve-debug-agent [MEDIUM|both|medium] ✅ SAFE

**DeepEvolve Debug 子代理：worker compile_error 时独立 LLM 诊断+结构化 patch 注入**

fix-7 实现 DeepEvolve Debug 子代理：在 worker compile_error 失败时，调用独立的 LLM debug agent（只有 Read/Bash 工具）读取 error + diff + task context，输出结构化 patch_guidance JSON（error_line + root_cause + 具体修复指令），注入为下次 worker attempt 的结构化 reviewer_feedback。只对 compile_error 触发（实测频次 0，但提供崩溃自愈鲁棒性）；invalid_target/zero_changes/timeout 保持现有文本反馈不动（避免过度工程化，这些是行为问题不需要 LLM debug）。MAX_WORKER_RETRIES 不变（budget=4），debug agent 仅在非最后 attempt 时调用。新增 1 个 prompt 模板文件 + 1 个内部函数 + 1 个测试文件，零公共接口变更，零回归风险（debug 失败时回退到现有 CRITICAL FIX 文本）。核查修正：审计说的 compile_error 是 debug agent 最高价值场景正确，但实测当前 0 次发生，所以收益从 medium-high 下调为 medium。


<details><summary>当前代码状态（核查后）</summary>

```
agent_workers.py 当前状态核查完成（实测，非复述审计）：\n\n文件 `/home/zzx/project/pok/web/core/agent_workers.py` (626 行)\n\n- L26-45: `_record_worker_failure()` — 写 worker_failures.jsonl，失败记录入口\n- L178-227: `_reset_target_files_to_source()` — 重置 target 文件到 baseline（重试清理）\n- L229-250: `_unlink_undeclared_new_files()` — 清理 worker 创建的未声明新文件\n- L253-271: `_classify_target_change()` — 分类 target 文件变化\n- L274-461: `_run_single_worker()` — 核心重试循环\n  - L303: `_last_reason = \"unknown\"` 初始化\n  - L304: `_last_failure_type = \"unknown\"` 初始化\n  - L330: `for attempt in range(MAX_WORKER_RETRIES):` — 重试循环开始（MAX_WORKER_RETRIES=4，evolution_infra.py:79）\n  - L348-354: `attempt_note` 文本增量（attempt 编号 + _last_reason + \"DIFFERENT approach\"）\n  - L371-395: timeout 分支（L373 _last_reason, L374 _last_failure_type, L391-394 CRITICAL FIX）\n  - L418-431: invalid_target 分支（L419 _last_reason, L420 _last_failure_type, L421-426 CRITICAL FIX 含 bogus path 指引）\n  - L432-440: zero_changes 分支（L433 _last_reason, L434 _last_failure_type, L435-438 CRITICAL FIX）\n  - **L448-452: compile_error 分支（L449 _last_reason, L450 _last_failure_type, L451 CRITICAL FIX）← 修改目标**\n  - L460: `_record_worker_failure()` — 全部重试失败后记录\n- L464-625: `_execute_wor
```
</details>


#### 改动点（4 处）

- **`web/core/prompts/debug_worker_prompt.md`** @ `(新文件)` [add] — 独立 debug agent 的 prompt 模板。遵循 audit_agents.py 模式，接收 error + diff + task context，输出结构化 JSON。debug agent 只有 Read/Bash 工具(无 Edit)，目的是诊断+建议，不直接改代码。模板参数与 audit_agents.py substitute_temp
- **`web/core/agent_workers.py`** @ `_execute_workers 函数之后 (L626 之后，文件末尾之前)` [add] — 新增 `_run_debug_agent()`，复用 `_run_worker_cot_check` 的完整模式：substitute_template → run_claude_query(prompt, [], ui, ...) → parse_json_output_with_mode → safe_default 兜底。只返回 patch_guida
- **`web/core/agent_workers.py`** @ `L448-452 (compile_error 分支)` [modify] — 在 compile_error 分支注入 debug agent 调用。关键设计决策：(1) 只在 attempt < MAX_WORKER_RETRIES-1 时调用 debug agent（最后一次重试没有下一次来利用 guidance，不浪费 LLM 调用）；(2) debug agent 失败时（patch_guidance 为空）回退到现有 CRI
- **`web/tests/test_logic_debug_agent.py`** @ `(新文件)` [add] — 覆盖两个关键测试路径：(1) compile_error 触发 debug agent + patch_guidance 正确注入；(2) 最后一次 attempt（无下一次重试可受益）时跳过 debug agent 调用（避免浪费 LLM 成本）。遵循现有 test_master_success_return.py 的 mock 模式（monkeypatc

<details><summary>完整 diff 详情</summary>


**`web/core/prompts/debug_worker_prompt.md`** @ `(新文件)` [add]

改后:
```python
<instructions> You are a DEBUG AGENT. A Worker Agent failed to compile code it edited in `bots/claude_v{version}/`. Your job: diagnose the exact error, identify the specific line(s), and output a structured patch suggestion for the NEXT retry attempt. You have Read and Bash tools. Use them to: 1. Read the error message and traceback below 2. Read the file(s) that caused the error to understand the surrounding context 3. Identify the EXACT line(s) causing the compilation error 4. Propose a [...]
```
*独立 debug agent 的 prompt 模板。遵循 audit_agents.py 模式，接收 error + diff + task context，输出结构化 JSON。debug agent 只有 Read/Bash 工具(无 Edit)，目的是诊断+建议，不直接改代码。模板参数与 audit_agents.py substitute_template 一致。*


**`web/core/agent_workers.py`** @ `_execute_workers 函数之后 (L626 之后，文件末尾之前)` [add]

改后:
```python
async def _run_debug_agent(task, worker_id, error_message, code_diff, next_v, next_dir, ui): """Run a DeepEvolve-style debug sub-agent on a compile-error failure. Only called when a worker attempt fails with compile_error. Reads the error, examines the changed code, and returns structured patch guidance that is injected as reviewer_feedback into the next worker attempt. Returns a dict with 'patch_guidance' string (best-effort, empty on failure). On any LLM/parse/infra error returns [...]
```
*新增 `_run_debug_agent()`，复用 `_run_worker_cot_check` 的完整模式：substitute_template → run_claude_query(prompt, [], ui, ...) → parse_json_output_with_mode → safe_default 兜底。只返回 patch_guidance 字符串（结构化补丁建议），失败时返回空字符串 → 调用方回退到现有 CRITICAL FIX 文本。函数是私有的（_ 前缀），不改变任何公共接口。*


**`web/core/agent_workers.py`** @ `L448-452 (compile_error 分支)` [modify]

现状:
```python
if compile_errors: _last_reason = f"compile error: {compile_errors[0][:200]}" _last_failure_type = "compile_error" base_worker_prompt += f"\n\nCRITICAL FIX: Fix syntax error:\n{compile_errors[0]}" continue
```
改后:
```python
if compile_errors: _last_reason = f"compile error: {compile_errors[0][:200]}" _last_failure_type = "compile_error" # DeepEvolve debug agent: structured patch guidance on compile failure. # Only runs if NOT the last attempt (last attempt has no next retry # to benefit from the guidance). Falls back to CRITICAL FIX text # when the debug agent returns empty. if attempt < MAX_WORKER_RETRIES - 1: _dbg_snapshots = worker_snapshots or _local_snapshots or {} _dbg_diff_parts = [] for target in [...]
```
*在 compile_error 分支注入 debug agent 调用。关键设计决策：(1) 只在 attempt < MAX_WORKER_RETRIES-1 时调用 debug agent（最后一次重试没有下一次来利用 guidance，不浪费 LLM 调用）；(2) debug agent 失败时（patch_guidance 为空）回退到现有 CRITICAL FIX 文本（零回归）；(3) 计算 diff 用现有 snapshot 逻辑（worker_snapshots / _local_snapshots），与 _run_worker_cot_check 使用同一数据源；(4) 只对 compile_error 触发，invalid_target / zero_changes / timeout 保持现有文本反馈不动（行为问题不需要 LLM debug）。*


**`web/tests/test_logic_debug_agent.py`** @ `(新文件)` [add]

改后:
```python
import asyncio import json import pytest from unittest.mock import MagicMock import agent_workers from agent_workers import _run_debug_agent class TestRunDebugAgent: """Verify _run_debug_agent: LLM call + JSON parse + patch_guidance extraction.""" @pytest.mark.asyncio async def test_returns_patch_guidance_on_valid_json(self, tmp_path, monkeypatch): import evolution_infra as ei prompt_file = tmp_path / "debug_worker_prompt.md" prompt_file.write_text("debug: {error_message} {code_diff}") [...]
```
*覆盖两个关键测试路径：(1) compile_error 触发 debug agent + patch_guidance 正确注入；(2) 最后一次 attempt（无下一次重试可受益）时跳过 debug agent 调用（避免浪费 LLM 成本）。遵循现有 test_master_success_return.py 的 mock 模式（monkeypatch `agent_workers.run_claude_query`）。每个 _run_debug_agent 边界路径也有独立单元测试。*

</details>


#### 测试（1 个）

- `web/tests/test_logic_debug_agent.py::test_name` — (1) test_returns_patch_guidance_on_valid_json: LLM 返回有效 JSON 时，patch_guidance 被正确提取并包含 DEBUG AGENT DIAGNOSIS 标记。(2) test

#### 向后兼容
无调用方改: `_run_debug_agent` 是 `agent_workers.py` 内部私有函数，不暴露到公共 API。`_run_single_worker` 签名不变，返回值不变(True/False)。`_execute_workers` 接口不变。tool_planning.execute_workers() 无需修改(它是对 `_execute_workers` 的薄包装，接口无变化)。debug prompt 模板是新文件，不影响现有 prompt 加载逻辑。


#### 风险
- LLM 调用成本增加: debug agent 是独立 LLM 调用（sonnet model），每次 compile_error 多一次调用。缓解：compile_error 实测频次=0（当前 5/71 worker 失败中 0 个 compile_error）；即便未来发生，只在 attemp
- PATCH_GUIDANCE 过度自信风险: debug agent 可能给出不正确的 patch guidance（LLM hallucination），误导下一次 worker attempt。缓解：(1) patch guidance 以 '# DEBUG AGENT DIAGNOSIS' 标
- 并发安全: debug agent 在 attempt 循环内 await run_claude_query，在并行 worker 模式下可能担心并发。缓解：_run_single_worker 是 async def，由 asyncio.gather 调度，_WORKER_SEMAPHORE 已在
- prompt 模板参数占位符错误风险: substitute_template 使用 dict 键替换。debug_worker_prompt.md 使用 {error_message}/{code_diff} 等参数名，需与 _run_debug_agent 中的 dict key 完全匹配。缓解


#### 验证步骤
```
cd /home/zzx/project/pok && python -m py_compile web/core/agent_workers.py
cd /home/zzx/project/pok && python -m py_compile web/tests/test_logic_debug_agent.py
cd /home/zzx/project/pok/web && python -m pytest tests/test_logic_debug_agent.py -v
cd /home/zzx/project/pok/web && python -m pytest tests/ -v --tb=short 2>&1 | tail -20  # 零回归验证（当前 1037 passed）
cd /home/zzx/project/pok/web/core && grep -n '_run_debug_agent' agent_workers.py  # 确认函数存在 + 调用点
cd /home/zzx/project/pok/web/core && test -f prompts/debug_worker_prompt.md && echo 'prompt template exists'
```


#### 回滚
1. 删除 `web/core/prompts/debug_worker_prompt.md` 文件\n2. 用 `git diff agent_workers.py` 找到 compile_error 分支修改（L448-452 区域），revert 到原始 `base_worker_prompt += f\"\\n\\nCRITICAL FIX: Fix syntax error:\\n{compile_errors[0]}\"`\n3. 删除 `web/core/agent_workers.py` 末尾新增的 `_run_debug_agent()` 函数\n4. 删除 `web/tes


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：✅
- 测试充分：❌

**遗漏调用方：** 无 missing call sites。核查确认: _run_debug_agent 是 agent_workers.py 内部新增私有函数(_前缀),不暴露公共 API; _run_single_worker 签名(274-276)与返回值(True/False,461)不变; _execute_workers 接口(464-466, 返回三元组)不变; 唯一调用方 tool_planning.execute_workers() (tool_planning.py:1513) 无需改; evolution_core.py:79 仅 re-export _run_single_worker/


**修正要点：** 计划可直接落地,change_is_safe=true(私有函数,零接口变更,空 patch_guidance 回退到现有 CRITICAL FIX 文本=零回归)。核查要点:

1. file:line 核查(实测,非复述):
   - agent_workers.py 实际 625 行(wc -l),plan 说 626 行/末尾 L626 — 纯末行无换行差异,插入点(_execute_workers 之后、文件末尾)明确,非阻断。
   - compile_error 分支实测 L448-452 与 plan 完全一致(448 if compile_errors: / 449 _last_reason / 450 _last_failure_type / 451 base_worker_prompt += CRITICAL FIX / 452 continue)。
   - L330 for attempt in range(MAX_WORKER_RETRIES=4) 核实;MAX_WORKER_RETRIES=4 在 evolution_infra.py:79 核实。
   - L442-447 verify_code 调用(parallel/serial 两路径)核实。

2. 修正 plan 的统计偏差(非阻断):worker_failures.jsonl 实测 category=worker 共 5 条,但分布是 invalid_target=4 + timeout=1,**zero_changes=0**(plan 说 zero_changes=1)。compile_error=0 核实正确。不影响 plan 设计(收益仍为前瞻性 medium)。

3. 替换模板占位符核实:worker_cot_check.md 用 {worker_role}/{wor


**测试缺口：** 计划的单元测试(1-4,覆盖 _run_debug_agent 的 valid-JSON/异常/无字段/prompt-missing)充分且遵循 audit_agents 模式。但两个集成测试(5,6)有显著实现缺口:(a) 必须额外 monkeypatch agent_workers.verify_code 让其在 attempt0 返回 compile_errors、attempt1 返回 [](否则需真实 bot 目录+py_compile,无法纯 mock);plan 的 changes 描述完全没提 verify_code 需要 mock。(b) 集成测试的 LLM mock 须区分 worker 的 run_claude_query(role_name="WORKER...") 与 debug agent 的 run_claude_query(role_name 含 DEBUG

### fix-6-vendi-crossover [HIGH|architecture|medium] ⚠️ 需修正

**behavior_diversity.py (Vendi Score) + crossover 消费 behavior_archive + commit novelty gate**

新建 web/core/behavior_diversity.py(pure-numpy compute_decision_fingerprint: decision_tester fixture 抽 7 维 key→RFF→D=256 向量; vendi_score: 相似矩阵特征值熵 VS=exp(-Σλlogλ), σ 用两两距离中位数动态算), 把 MAP-Elites niche + Glicko r-2*rd 区间接入 _pick_crossover_parents(向后兼容, archive=None 退化旧逻辑)打破同源近亲杂交, 在 commit_bot 加 ADVISORY novelty gate(ΔVS<0.05 AND rating<30 时 log+进 experience_pool 反馈 Master pivot, 不做 binding REJECT 以免重蹈 hard-gate 误杀 novel plan 的历史 bug), post_generation_cleanup 末尾算 fingerprint 追加 fingerprints.jsonl(fcntl-locked append-only JSONL)。诚实修正审计: commit gate 首版 advisory 非 binding(因 VS 在 mirror-battle POMDP 下是


<details><summary>当前代码状态（核查后）</summary>

```
核查后(读真实代码,非复述审计):

1. `web/core/behavior_diversity.py` 不存在(confirmed via ls)。`grep -rn 'vendi\|VS_SCORE\|AutoQD\|RFF\|cwPCA' web/core/ = 0 命中。

2. `web/core/map_elites.py`(412 行):现有 fingerprint 是粗糙的 2 维 agg_factor/vpip 聚合(`build_behavior_archive` L260-334,BC = `_bucket(af)/_bucket(vpip)`)。`behavior_archive.json` 实测 19 niches 全 `eval_mode='single'`,0 个 k=3(confirmed via python json.load):
```
"bot": "claude_v131", "version": 131, "fitness": 0.5016,
"bc": {"agg_bucket": 1, "loose_bucket": 3, "aggression_factor": 0.529, "vpip": 0.514},
"eval_mode": "single", "fitness_median": null
```

3. `web/core/generation_scheduler.py:602-650` `_pick_crossover_parents(ratings, current_v)`:确认只按 `h2h_avg_wr` 排序(L617-621)+ 版本差≥3(L638)选父:
```python
ranked = sorted(active, key=lambda b: h2h.get(b, 0.0), reverse=True)
parent_a = ranked[0]
for candidate in ranked[1:]:
    if abs(vc - va) >= 3: parent_b = candidate; break
if parent_b is None: parent_b = ranked[1]
```
**不消费 behavior_archive,不看 niche,不看 conservative_rating 区间**。

4. `behavior_archive` 唯一消费者:`generation_scheduler.py:354-355`(QD elite re-eval housekeeping,只在 `next_v_planned % QD_REEVAL_EVERY == 0` 时读)。`_decide_strategy`(L383-499)和 Master prom
```
</details>


#### 改动点（3 处）

- **`web/core/behavior_diversity.py`** @ `new file (module-level)` [add] — 审计核心子项 (a)+(b):从 decision_tester fixture 抽 7 维 key(round_idx/made_str_bucket/pot_odds_bucket/to_call_to_pot_bucket/is_opp_aggr/street/action_bucket)→RFF→np.float32[D=256];vendi_sco
- **`web/core/behavior_diversity.py`** @ `module-level (I/O helpers)` [add] — 审计子项 (c):post_generation_cleanup 末尾算 fingerprint 存 fingerprints.jsonl。用 append-only JSONL + fcntl locked_file(既有模式),避免全量重写与 daemon 的 write race。
- **`web/core/generation_scheduler.py`** @ `_pick_crossover_parents (L602-650), signature + parent B selection` [modify] — 审计子项 (d):_pick_crossover_parents 从不同 niche / 不同 Glicko r-2*rd 区间选父。niche 数据来自现成 behavior_archive.json(无需新算),conservative_rating 既有方法。向后兼容:archive=None 时退化为旧逻辑。

<details><summary>完整 diff 详情</summary>


**`web/core/behavior_diversity.py`** @ `new file (module-level)` [add]

改后:
```python
"""Behavior-diversity numerics for mode-collapse monitoring (fix-6, evolution-plan C2). Pure-numpy (numpy 2.4.4 confirmed available, already used by engine/aivat.py): - compute_decision_fingerprint(bot_dir, n_scenarios=200) -> np.ndarray[float32, D=256] - vendi_score(fingerprints) -> float (VS = exp(-sum(lambda * log(lambda)))) POMDP caveat (Gaier 2024): these fingerprints are DECISION fingerprints derived from decision_tester scenario fixtures (observable state-action), NOT true occupancy [...]
```
*审计核心子项 (a)+(b):从 decision_tester fixture 抽 7 维 key(round_idx/made_str_bucket/pot_odds_bucket/to_call_to_pot_bucket/is_opp_aggr/street/action_bucket)→RFF→np.float32[D=256];vendi_score 用两两距离中位数动态算 σ(AutoQD 推荐, scale-invariant)。pure numpy,无 scipy 依赖,所有错误 best-effort 返回零向量/0.0 不崩流水线。*


**`web/core/behavior_diversity.py`** @ `module-level (I/O helpers)` [add]

改后:
```python
FINGERPRINTS_FILE = None # set lazily to avoid import-cycle with evolution_infra def _fp_file(): global FINGERPRINTS_FILE if FINGERPRINTS_FILE is None: from evolution_infra import RESULTS_DIR FINGERPRINTS_FILE = RESULTS_DIR / "fingerprints.jsonl" return FINGERPRINTS_FILE def append_fingerprint(bot_name, fingerprint): """Append one bot's fingerprint to fingerprints.jsonl (JSONL, fcntl-locked). Stores a compact base64-free list form: {bot, dim, vec(list[float]), ts}. Best-effort (never [...]
```
*审计子项 (c):post_generation_cleanup 末尾算 fingerprint 存 fingerprints.jsonl。用 append-only JSONL + fcntl locked_file(既有模式),避免全量重写与 daemon 的 write race。*


**`web/core/generation_scheduler.py`** @ `_pick_crossover_parents (L602-650), signature + parent B selection` [modify]

现状:
```python
def _pick_crossover_parents(ratings, current_v) -> tuple | None: """Select two diverse parents for crossover. Parent A: highest h2h_avg_wr (strongest bot). Parent B: highest h2h_avg_wr with version gap >= 3 from parent A """ from evolution_infra import get_active_bots from tool_helpers import load_h2h_avg_winrates active = get_active_bots() if len(active) < 2: return None h2h = load_h2h_avg_winrates() ranked = sorted(active, key=lambda b: h2h.get(b, 0.0), reverse=True) if len(ranked) < 2: [...]
```
改后:
```python
def _pick_crossover_parents(ratings, current_v, archive=None) -> tuple | None: """Select two diverse parents for crossover. fix-6: parent selection now consults the MAP-Elites behavior_archive so two parents come from DIFFERENT behavioral niches (different aggression/looseness buckets) when possible, instead of pure h2h+version-gap (same-axis inbreeding). Also prefers parents from distinct conservative-rating (r-2*rd) bands. Args: ratings: {bot_name: Glicko2Player} for conservative_rating [...]
```
*审计子项 (d):_pick_crossover_parents 从不同 niche / 不同 Glicko r-2*rd 区间选父。niche 数据来自现成 behavior_archive.json(无需新算),conservative_rating 既有方法。向后兼容:archive=None 时退化为旧逻辑。*

</details>


#### 测试（8 个）

- `web/tests/test_logic_behavior_diversity.py::test_name` — _rff_project(3 keys) 返回 np.float32[256] 且 L2 norm≈1.0; 证明 RFF 维度正确 + 归一化
- `web/tests/test_logic_behavior_diversity.py::test_name` — vendi_score(4 个相同向量)≈1.0(无多样性), vendi_score(4 个正交向量)≈4.0(最大多样性); VS∈[1,N] 边界正确
- `web/tests/test_logic_behavior_diversity.py::test_name` — 10 向量分两簇, VS≈2.0(有效种群大小=2 个行为模式); 证明 σ=median 的 scale-invariance
- `web/tests/test_logic_behavior_diversity.py::test_name` — bot_dir 不含 main.py 时返回零向量[256], 不抛异常(best-effort)
- `web/tests/test_logic_behavior_diversity.py::test_name` — N=1 返回 1.0, N=0 返回 0.0; 边界保护
- `web/tests/test_logic_behavior_diversity.py::test_name` — append_fingerprint 后 load_fingerprints 返回 {bot: vec}, last-write-wins; JSONL 格式正确

#### 向后兼容
新建 behavior_diversity.py 不影响任何调用方。改 `_pick_crossover_parents` 签名增加可选 kwargs(ratings=None, archive=None),两处调用点(generation_scheduler.py:465, :493)需同步传 ratings——但因为是可选参数且默认 None 时退化为旧行为,旧调用方不传也不会崩。commit_bot 新增 novelty gate 是纯新增返回字段(advisory novelty 字段进 result),调用方(orchestrator LLM)无需改,**不改任何函数返回值的现有字段**。fingerprints.jsonl 是纯新增文件。**无 binding gate = 无 gate ledger 变更 = 无 STAGE_GATE_ALLOWLIST 影响**。所有读写带


#### 风险
- numpy 依赖:aivat.py 已用,web/ context 确认可用(numpy 2.4.4)。mitigation:behavior_diversity.py 顶层 try-import numpy,_HAS_NUMPY=False 时所有函数返回零向量/0.0 不崩。
- fingerprint 计算开销:每代 commit 后算 1 个 bot 的 decision fingerprint = 跑 15 个 subprocess(已有 decision_tester 逻辑),~15-30s。mitigation:在 post_generation_cleanup(已
- VS 基线噪声:前几十代 fingerprints.jsonl 样本少(每代 1 行),VS 不稳定。mitigation:commit novelty gate 首版 advisory(不阻断)+ 要求 pool>=5 bots 才计算有意义的 VS(pool<5 时 novelty 字段返回 s
- POMDP fingerprint 噪声(Gaier 2024):decision fingerprint 非真 occupancy,VS 是噪声估计。mitigation:文档注明 advisory-only,所有消费者(选父/commit)只把 VS/niche 当 diversity 信号之一


#### 验证步骤
```
cd /home/zzx/project/pok/web && python -m pytest tests/test_logic_behavior_diversity.py -v (新增 8 测试全过)
cd /home/zzx/project/pok/web && python -m pytest tests/test_logic_phase3_nemesis_mapelites.py tests/test_phase4_async_qd_psro.py -v (现有 map_elites/qd 测试零回归,确认 behavior_diversity 不破坏 map_elites)
cd /home/zzx/project/pok/web && python -m pytest tests/ -v (CLAUDE.md 要求零回归,当前 1037 passed,改后应 >=1037)
python -c "import sys; sys.path.insert(0,'web/core'); from behavior_diversity import vendi_score, compute_decision_fingerprint; import numpy as np; print(vendi_score([np.zeros(256,dtype=np.float32), np.zeros(256,dtype=np.float32)])) (冒烟:相同 fingerprint VS≈1.0)",
grep -rn 'behavior_diversity\|vendi_score\|compute_decision_fingerprint' web/core/generation_scheduler.py web/core/tool_commit.py (确认接线点存在)
手动核查:跑一代 evolution(可选,dry-run) 后 cat web/core/results/fingerprints.jsonl 确认追加了新行, grep 'pipeline.commit_novelty' 在 system_events.jsonl 确认 advisory 事件已记录
```


#### 回滚
纯文件级回滚(无 schema 迁移):`git revert` 本次 commit。删除新建的 web/core/behavior_diversity.py + web/tests/test_logic_behavior_diversity.py。revert generation_scheduler.py 3 处改动的 diff(签名回退 / archive 查询删 / post_cleanup 块删)+ tool_commit.py novelty 块删。results/fingerprints.jsonl 可保留无害(无消费者)或 `rm web/core/results/finger


#### 对抗核查结果

- file:line 准确：❌
- 改法安全：❌
- 测试充分：❌

**🔴 阻断点：** [1. FINGERPRINT EXTRACTION FEASIBILITY: plan 说"从 decision_tester fixture 抽 7 维 key(round_idx/made_str_bucket/pot_odds_bucket/to_call_to_pot_bucket/is_opp_aggr/street/action_bucket)→RFF"。但实际 test_scenarios.json 的 input 中没有 made_str、pot_odds、to_call_to_pot、is_opp_aggr 这些字段——它们是 bot 策略内部状态，不是场景输入的一部分。场景只有 my_cards/public_cards/history(含 round/action/bet_amount)/my_chips。plan 需要明确：(a) 如何从场景输入中推断这 7 维，或 (b) 改为从 bot 运行结果中提取（需要解析 bot stdout），或 (c) 使用更简单的场景级 fingerprint（如 round/street、action_bucket、my_chips ratio）。当前 plan 的 fingerprint 提取描述与实际数据不匹配，需要返工。 2. CALL SITE COVERAGE: plan 说两处调用点(generation_s


**遗漏调用方：** generation_scheduler.py:465 和 :493 是 _pick_crossover_parents 的唯一调用点。如果 plan 要求 _pick_crossover_parents 使用 niche/behavior_archive 数据选父，这两处需要同步修改以传入 archive 参数。plan 的 changes 部分只描述了修改函数签名，没有明确列出调用点的同步改动。此外，qd_async_eval.py:284,371 是 read_behavior_archive 的消费者，但它们与 _pick_crossover_parents 无关，不需要改动。


**修正要点：** [1] BLOCKING: scenario input 缺少 made_str/pot_odds/to_call_to_pot/is_opp_aggr 字段——plan 的 RFF fingerprint 设计需要返工。可行替代：(a) 从 scenario 的 history 中推断 round/street(通过 public_cards 长度)、action_bucket(通过 classify_action)、bet_ratio(通过 history 中的 round_bet/my_chips)；(b) 运行 bot 获取 action 后从 action 推断；(c) 简化为 scenario-level fingerprint(只用 scenario ID 的 hash 作为 1 维)。建议方案 (a) 从现有场景字段中提取 4-5 维可行特征，而非声称的 7 维。 [2] MODERATE: _pick_crossover_parents 签名变更是 backward-compatible 的(新增可选 kwargs)，但 L465 和 L493 的调用点需要明确是否传入 archive。plan 的 changes 部分应补充这两个调用点的修改描述。 [3] ADVISORY GATE PATTERN: plan 正确识别了 novelty gate 为 advisory，不会阻断 commit。但需要明确：advisory novelty 字段应放在 commit_bot 的返回结果中（不影响 gate ledger），而不是作为 gate_results 的一部分（那样会被 gate ledger 逻辑检查）。 [4] RFF IMPLEMENTATION: plan 声称"pure numpy, 无 scipy 依赖"，RFF(随机傅里叶特征)实现确


**测试缺口：** [1] MISSING BACKWARD COMPAT TEST: plan 的 test_pick_crossover_parents_niche_aware 测试了 archive 有 2 个不同 niche 时的行为，以及 archive=None 时的退化，但缺少 archive={}（空 dict）的测试——这是 read_behavior_archive 失败时的实际返回值，需要覆盖。 2] MISSING EDGE CASE: _pick_crossover_parents 当前只按 h2h_avg_wr 排序选 parent A，但 plan 的 niche-aware 修改可能改变 parent A 的选择逻辑（如果 niche 评分影响排序）。plan 需要明确：parent A 是否仍然按 h2h_avg_wr 选择（只改 parent B 的选择逻辑），还是 par


---


## 4. Batch 4 — 治理 + 长期韧性（4-6h）

> 最高风险单独批。归因信号致命缺陷: commit_bot gate ledger 语义下 precommit_passed 恒 True(因为 commit 只在所有 gate pass 后调用)。需重新设计 hook(如 daemon 评估收敛后回填)。Ratchet 消融 harsh retirement = -0.019 主动伤害，N_min 必须 >=30。必须在前 3 批全部稳定 + 有足够 daemon 数据后才能做。

### fix-12-experience-pool-ratchet [MEDIUM|architecture|medium] ⚠️ 需修正

**主经验池接入 Ratchet retire（复用 research_governance 原语，非 rebuild）**

审计 #12 要求主经验池(experience_pool.md)接入 Ratchet outcome-driven retire。核查证实 research_governance.py 的 score_candidate(L100-105)/record_outcome(L250-289)/RETIRE_N_MIN=30/RETIRE_TAU=-0.10 原语完整,但三个经验池文件 ZERO import(已 grep 确认),trim_experience_pool 是纯尾部截断,_consolidate_experience_pool 只有 tag-identity gate(只防标签丢失不防 drift)。实施:新建 experience_attribution.py thin adapter 复用 research_governance 的常量与 ĉ 公式(操作 sidecar JSON 不污染 web_candidates 池),在 _consolidate_experience_pool 的 LLM-call 前 + 写盘后双层 drop retired lessons,在 commit 后用 precommit outcome 喂入 ĉ(trials>=30 AND ĉ<=-0.10 才 retire,严格遵守消融约束 N_min>=30),consolidator


<details><summary>当前代码状态（核查后）</summary>

```
核查后的当前代码状态(证明我读了代码):

1. research_governance.py(342 行)有完整可复用原语,但只管 web_candidates 池:
   - L46-48: `RETIRE_N_MIN = 30` / `RETIRE_TAU = -0.10`(Ratchet A4: N_min=20=-0.019 主动伤害,所以 30)
   - L100-105: `score_candidate(c)` = `(attributed_help - attributed_hurt) / max(trials, 1)`
   - L250-289: `record_outcome(candidate_id, won, hurt_verdict, n_games, bot_version)` — 累加 hurt/help,内部调 retirement check
   - L291-310: `_retire_candidate()` — 标记 retired + append blacklist jsonl + log
   - L52: `WEB_CANDIDATES_FILE = RESULTS_DIR / "web_candidates.json"`(只管这个池)
   - I/O 全走 evolution_infra 的 read_locked_json/write_locked_json/append_locked_jsonl(fcntl)

2. experience_pool.py(40 行)纯尾部截断,无 attribution/retire:
   - L14-15: `MAX_EXPERIENCE_LINES = 120` / `KEEP_EXPERIENCE_LINES = 100`
   - L18-40: `trim_experience_pool(max_entries=8)` — 超过 120 行就 `kept = "\n".join(lines[-100:])` 纯按行截断,无内容判别
   - 被 tool_bot_management.py:236 调用(reap 后)

3. experience_archivist.py `_consolidate_experience_pool`(L42-147)只有 tag-identity 检查:
   - L22-39: `_tag_identity(text)` 用 regex `\[[A-Z ]*EXHAUSTED[^\]]*\]` 提取 EXHAUSTED 标签类
   - L120-138: 写盘前 `pre_tags = _tag_identity(content); post_tags = _tag_identity(consolidated)
```
</details>


#### 改动点（6 处）

- **`web/core/experience_attribution.py`** @ `new file (~120 lines)` [add] — 审计 #12 要求复用 research_governance 原语(score_candidate L100-105 / record_outcome L250-289 / RETIRE_N_MIN=30 RETIRE_TAU=-0.10),不新建 experience_attribution.py 是 plan 旧说法——但 research_gover
- **`web/core/experience_archivist.py`** @ `_consolidate_experience_pool L52-147 (after reading content, before LLM call; before final write)` [modify] — 审计 #12 核心:retire 从 EXHAUSTED tag-identity 检查(只防标签丢失)升级为 ĉ-driven 退役。在 LLM 合并前 drop 掉 retired bullets,防止 consolidator 把死掉的 lesson reword 后重新塞回(这正是 Ratchet 三阶段 drift 的 retrieval-degr
- **`web/core/experience_archivist.py`** @ `_consolidate_experience_pool final-write block (L139-143)` [modify] — LLM consolidator 可能把一条 retired lesson 换个措辞重新写回(retrieval-degradation drift)。在最终写盘前再 filter 一次 retired fingerprints,形成 pre+post 双层防护。best-effort,失败不阻断已通过 tag-identity gate 的写入。
- **`web/core/tool_commit.py`** @ `_append_experience_updates call site (around L395-401) — add post-commit attribution hook` [modify] — retire 需要跨代 outcome 数据喂入(trials/hurt/help)。commit 时点已知 precommit pass/fail(vs parent,是干净的 binary outcome)且已知 archivist 本代产出的 experience_updates 列表(=本代 surface 的 lessons)。这是最低成本的 ĉ 
- **`web/core/experience_pool.md`** @ `L4 (OPPONENT_MODELING) + L28 (GENERAL) — stale stderr entries` [modify] — 审计 #12 漂移实证:experience_pool.md L4/L28 仍说 stderr 不可读,但 A1 本会话已修(battle.py:47-105 实测 drain 线程+snapshot)。这是 Ratchet library-drift 的 stale-retrieval 阶段。dogfood 自己的池:既然给池加了 retire 能力,先手
- **`web/core/prompts/experience_consolidator.md`** @ `<local_optima> section (L26-37)` [modify] — 审计 #12 要求 consolidator prompt 加'重复≥N 代无 WR-lift 降级 stale'。区分两种标记:[POSSIBLY EXHAUSTED]=启发式 tag-identity(现有,3 代重复),[STALE]=outcome-driven(新增,9 代无 WR-lift)。让 LLM 只 mark 不 drop(保持 tag-

<details><summary>完整 diff 详情</summary>


**`web/core/experience_attribution.py`** @ `new file (~120 lines)` [add]

现状:
```python
(no such file — research_governance primitives are not reused by experience_pool path)
```
改后:
```python
"""Experience-pool outcome-driven retirement — Ratchet (arxiv 2605.19576) adapter. Reuses research_governance primitives (score_candidate/RETIRE_N_MIN/RETIRE_TAU) WITHOUT touching the web_candidates pool. Experience-pool lessons are markdown bullets, not candidate dicts, so each lesson is keyed by a normalized fingerprint (first 80 chars of the bullet text, lowercased) and tracked in a sidecar JSON: results/experience_attribution.json = {fingerprint: {source_gen, trials, attributed_help, [...]
```
*审计 #12 要求复用 research_governance 原语(score_candidate L100-105 / record_outcome L250-289 / RETIRE_N_MIN=30 RETIRE_TAU=-0.10),不新建 experience_attribution.py 是 plan 旧说法——但 research_governance 的 record_outcome 操作 web_candidates pool(list of dict),experience_pool.md 是 markdown bullets,数据模型不同。最小侵入的做法是写一个 thin adapter 直接 import RETIRE_N_MIN/RETIRE_TAU/score_candidate 公式,而非把 markdown lessons 塞进 web_candidates.json(会污染 A6 治理状态)。adapter 维护一个 sidecar JSON(results/experience_attribution.json)记录每条 lesson 的 trials/hurt/help/retired,复用同一 ĉ 公式与同一 RETIRE_N_MIN=30/TAU=-0.10 常量(import 而非重定义,保证消融约束 N_min>=30 单一来源)。*


**`web/core/experience_archivist.py`** @ `_consolidate_experience_pool L52-147 (after reading content, before LLM call; before final write)` [modify]

现状:
```python
if not EXPERIENCE_FILE.exists(): return with locked_file(EXPERIENCE_FILE, "r") as ef: content = ef.read() if not content or content.strip() == "": return # Skip only if file is completely empty
```
改后:
```python
if not EXPERIENCE_FILE.exists(): return with locked_file(EXPERIENCE_FILE, "r") as ef: content = ef.read() if not content or content.strip() == "": return # Ratchet outcome-driven retirement (#12): drop bullets whose sidecar # attribution says retired (trials>=RETIRE_N_MIN AND ĉ<=RETIRE_TAU) BEFORE # feeding content to the LLM, so dead lessons don't get re-merged/reworded # back in (the silent-injection-harm drift stage). Best-effort — never blocks. retired_set = set() try: from [...]
```
*审计 #12 核心:retire 从 EXHAUSTED tag-identity 检查(只防标签丢失)升级为 ĉ-driven 退役。在 LLM 合并前 drop 掉 retired bullets,防止 consolidator 把死掉的 lesson reword 后重新塞回(这正是 Ratchet 三阶段 drift 的 retrieval-degradation + silent-injection-harm)。放在 LLM call 之前,且 best-effort 包裹,不破坏现有 tag-identity gate(L120-138)——两个 gate 叠加而非替代。*


**`web/core/experience_archivist.py`** @ `_consolidate_experience_pool final-write block (L139-143)` [modify]

现状:
```python
else: tmp = EXPERIENCE_FILE.with_suffix(".tmp") tmp.write_text(consolidated + "\n", encoding="utf-8") tmp.replace(EXPERIENCE_FILE) ui.log_history("Experience pool consolidated and written back.", "success")
```
改后:
```python
else: # Re-apply Ratchet retire on the LLM output too: the LLM may # have re-included a retired lesson under different wording. try: from experience_attribution import retired_fingerprints, lesson_fingerprint rset = retired_fingerprints() if rset: consolidated = "\n".join( ln for ln in consolidated.split("\n") if ln.strip().startswith("#") or lesson_fingerprint(ln) not in rset ) except Exception: pass tmp = EXPERIENCE_FILE.with_suffix(".tmp") tmp.write_text(consolidated + "\n", [...]
```
*LLM consolidator 可能把一条 retired lesson 换个措辞重新写回(retrieval-degradation drift)。在最终写盘前再 filter 一次 retired fingerprints,形成 pre+post 双层防护。best-effort,失败不阻断已通过 tag-identity gate 的写入。*


**`web/core/tool_commit.py`** @ `_append_experience_updates call site (around L395-401) — add post-commit attribution hook` [modify]

现状:
```python
updates = llm_result.get("experience_updates", []) # ... _append_experience_updates(...) call
```
改后:
```python
updates = llm_result.get("experience_updates", []) if updates: _append_experience_updates(version, updates, ...) # #12 Ratchet attribution: feed the precommit pass/fail outcome back into # the attribution counters for every lesson the archivist surfaced this gen, # so ĉ accumulates across generations and triggers retire at trials>=30. try: from experience_attribution import record_lesson_outcome precommit_passed = bool(ckpt.get("gate_results", {}).get("precommit", {}).get("passed")) for [...]
```
*retire 需要跨代 outcome 数据喂入(trials/hurt/help)。commit 时点已知 precommit pass/fail(vs parent,是干净的 binary outcome)且已知 archivist 本代产出的 experience_updates 列表(=本代 surface 的 lessons)。这是最低成本的 ĉ 闭环:不依赖异步 rating 回填(#2 的工作),直接用 precommit gate 已有信号。record_lesson_outcome 内部复用 research_governance 的 retire 判定(trials>=30 AND ĉ<=-0.10)。*


**`web/core/experience_pool.md`** @ `L4 (OPPONENT_MODELING) + L28 (GENERAL) — stale stderr entries` [modify]

现状:
```python
- **Firing verification:** `_PersistentBot` reads ONLY stdout — ALL stderr telemetry invisible to daemon grep. Use reachability_test (code-reachability proxy) + ≥100g H2H WR-lift, NOT telemetry grep. [POSSIBLY EXHAUSTED]
```
改后:
```python
L4 → "- **Firing verification:** stderr telemetry NOW readable (A1 landed: battle.py:47-105 drains _PersistentBot stderr). Prefer daemon grep `stderr_buf`/telemetry tags for firing-rate; fallback reachability_test + ≥100g H2H WR-lift. [POSSIBLY EXHAUSTED]" L28 → "- **HIGH-VALUE UNBLOCK (DONE):** battle.py stderr drain landed (A1) — telemetry verification is now reachable via daemon grep."
```
*审计 #12 漂移实证:experience_pool.md L4/L28 仍说 stderr 不可读,但 A1 本会话已修(battle.py:47-105 实测 drain 线程+snapshot)。这是 Ratchet library-drift 的 stale-retrieval 阶段。dogfood 自己的池:既然给池加了 retire 能力,先手动清掉这一条已 stale 的条目证明闭环工作。不删整条(保留结构性教训),只把'stderr 不可读'修正为'stderr 现在可读'。*


**`web/core/prompts/experience_consolidator.md`** @ `<local_optima> section (L26-37)` [modify]

现状:
```python
<local_optima> If the same type of lesson appears for 3+ consecutive generations (e.g. 3 gens of constant-tuning in the same direction with no gain), append " [POSSIBLY EXHAUSTED]" to that bullet so Master avoids repeating it. ... Keep the literal "[POSSIBLY EXHAUSTED]" marker verbatim when present.
```
改后:
```python
Add a new <ratchet_retire> block AFTER <local_optima>: <ratchet_retire> 9. STALE DOWNGRADE (outcome-driven, not tag-driven): if a lesson has been restated for ≥3 consolidation cycles (≈9 generations) AND no H2H WR-lift has been attributed to the direction it advocates (no RECENT_LESSONS entry shows a win-rate gain citing that direction), rewrite it to begin with "[STALE — no attributed WR-lift in ≥9 gens]" and condense to one line. This marks it for outcome-driven retirement by the [...]
```
*审计 #12 要求 consolidator prompt 加'重复≥N 代无 WR-lift 降级 stale'。区分两种标记:[POSSIBLY EXHAUSTED]=启发式 tag-identity(现有,3 代重复),[STALE]=outcome-driven(新增,9 代无 WR-lift)。让 LLM 只 mark 不 drop(保持 tag-identity gate 完整),真正的 drop 由 experience_attribution 的 ĉ 判定做(数据驱动,防 LLM 误删)。N=3 cycles≈9 gens 是 RETIRE_N_MIN=30 trials 的合理 LLM 侧对应(每 gen≈3-4 precommit trials)。*

</details>


#### 测试（6 个）

- `web/tests/test_logic_experience_attribution.py::test_name` — 调 record_lesson_outcome(text, won=True) 3 次 + won=False 1 次 → sidecar trials=4, attributed_help=3, attributed_hurt=1; sc
- `web/tests/test_logic_experience_attribution.py::test_name` — feed won=False 20 次(对照 Ratchet 消融 N_min=20 主动伤害)→ status 仍 active;再 feed 到 30 次且 ĉ<=-0.10 → status==retired 且 retired_fi
- `web/tests/test_logic_experience_attribution.py::test_name` — 同一 lesson 前 80 字符相同、后缀不同 → fingerprint 相同;前 80 字符不同 → fingerprint 不同。量化指纹稳定性边界(consolidator reword 前 80 字通常不变)
- `web/tests/test_mcp_experience_consolidation.py::test_name` — 预置 sidecar 标记某 lesson fp 为 retired;_consolidate_experience_pool 跑完后: (a) 喂给 LLM 的 prompt 不含该 lesson(pre-filter),(b) 写回的 
- `web/tests/test_logic_experience_attribution.py::test_name` — ATTRIBUTION_FILE 不存在时 retired_fingerprints()==set() 且 score_lesson()==0.0(首次运行无 sidecar 不崩溃,best-effort 契约)
- `web/tests/test_mcp_experience_consolidation.py::test_name` — commit 路径后 sidecar 里 archivist updates 每条 lesson 都有 trials=1 + won==precommit_passed。验证 ĉ 闭环数据来源(commit→attribution→未来 r

#### 向后兼容
无需改动任何现有调用方。(1) _consolidate_experience_pool 签名不变 (generation_scheduler.py:740 无需改);(2) _append_experience_updates 签名不变 (tool_commit.py:401 无需改);(3) experience_pool.trim_experience_pool 不动;(4) research_governance 原语只被 import 读取,无修改。新增是纯 additive(experience_attribution.py 新文件 + prompts 局部加段)。experience_pool.md 是数据文件非代码,改 L4/L28 不影响任何解析(regex 只认 [POSSIBLY EXHAUSTED] 标签)。唯一需注意:experience_attribution.


#### 依赖：fix-1-research-governance-zero-flow, A1-stderr-drain (battle.py:47-105, already landed, enables the dogfood)


#### 风险
- lesson_fingerprint 只取前 80 字符做指纹 → consolidator reword 后指纹变了,retire 失效。mitigation:pre-merge filter(基于 content 原文)+ prompt 要求 [STALE] 标记保留语义;接受首次 reword
- precommit binary outcome 是粗粒度信号(pass=helped, fail=hurt)——一个 gen 失败可能是 worker 执行问题而非 lesson 方向错。mitigation:RETIRE_N_MIN=30 累积足够样本才 retire,单次失败不会误杀;且 re
- Ratchet 消融 N_min=20 主动伤害 → 若误设 N_min<30 会引入 -0.019 伤害。mitigation:直接 import research_governance.RETIRE_N_MIN(单一来源=30),不重定义;adapter 文件里显式注释禁止改。
- experience_attribution.json sidecar 无界增长(每个 fingerprint 一条)。mitigation:与 experience_pool.md 同生命周期——pool 每 3 代 trim+retire,可在 retire_stale_lessons 后顺便 


#### 验证步骤
```
cd web && python -m pytest tests/test_logic_experience_attribution.py tests/test_mcp_experience_consolidation.py -v (新增测试全绿)
cd web && python -m pytest tests/ -v (零回归,基线 1037 passed)
grep -n 'research_governance' web/core/experience_attribution.py (确认 import RETIRE_N_MIN/RETIRE_TAU 复用而非重定义)
grep -n 'RETIRE_N_MIN' web/core/experience_attribution.py | head (确认只有 import 行,无任何 <30 的本地覆盖)
python -c "import sys; sys.path.insert(0,'web/core'); from experience_attribution import record_lesson_outcome, retired_fingerprints; [record_lesson_outcome('test lesson', won=False, n_games=1) for _ in range(35)]; print('retired after 35 fails:', len(retired_fingerprints()))" (手动验证 retire 在 N_min=30 触发)
手动 dogfood: grep -n 'stderr' web/core/experience_pool.md 确认 L4/L28 不再说'stderr 不可读'
```


#### 回滚
纯 additive 回滚极简:(1) `git revert` 本次 commit;(2) experience_attribution.json sidecar 可保留(只是不再被读,无副作用),或 `rm web/core/results/experience_attribution.json` 清空;(3) experience_pool.md L4/L28 的文案改回(手动或 git checkout);(4) prompts/experience_consolidator.md 的 <ratchet_retire> 块删除。无需迁移数据(experience_pool.md 本身是


#### 对抗核查结果

- file:line 准确：✅
- 改法安全：❌
- 测试充分：❌

**🔴 阻断点：** 🔴🔴 归因信号在所选 hook 点恒为 True(致命缺陷,plan 不可直接执行)。计划把 record_lesson_outcome hook 放在 tool_commit.py:401(run_archivist 内 _append_experience_updates call site),信号源 won=precommit_passed。但 commit_bot 的 gate ledger 在 tool_commit.py:108-112 强制要求 precommit_eval.passed==True 才允许 commit(run_archivist 只在 commit 后运行)。所以到达 L401 时 precommit 必然已 passed → won 恒为 True → attributed_hurt 永不累加 → ĉ=(help-hurt)/trials 恒正 → 永远 ≤ RETIRE_TAU=-0.10 永不触发 → retire 机制结构性 INERT(正是 project #1 INERTNESS failure mode,记忆反复警告)。验证证据:tool_commit.py:108-112 commit_bot 对 precommit.passed!=True 返回 "COMMIT BLOCKED";orchestrator.md stage 顺序 


**遗漏调用方：** _append_experience_updates 有 2 个 call site,plan 只覆盖 1 个:(1) tool_commit.py:401 (archivist 路径,plan 改) ✓;(2) tool_gates.py:892-897 (run_critic 内的 Critic evidence 写池路径,plan 完全没提)。若 attribution hook 只挂 L401,则 Critic evidence 写入的 lesson 不进 attribution 计数,这些 lesson 永远 trials=0 永不 retire。需明确:Critic-evidenc


**修正要点：** 核查结论:file:line 全部真实可核查,但有一个致命逻辑缺陷 + 一个审计冲突,plan 不可直接执行,需返工。

【行号核查(全部亲自核对)】research_governance.py: RETIRE_N_MIN=30@L46 / RETIRE_TAU=-0.10@L48 / score_candidate@L100-105 / record_outcome@L250-289 / _retire_candidate@L291-310 / WEB_CANDIDATES_FILE@L52 全准。experience_pool.py L14-15/L18-40 准。experience_archivist.py: _tag_identity@L22-39 / EXHAUSTED tag-identity gate@L120-138 / final-write block@L139-143(else 分支 L139 + tmp write L140-142)准。battle.py:47-105 _PersistentBot stderr drain 线程+snapshot 实测存在(A1 已落地)准。experience_pool.md L4(OPPONENT_MODELING '_PersistentBot reads ONLY stdout...ALL stderr telemetry invisible...[POSSIBLY EXHAUSTED]')+L28(GENERAL '🔴 HIGHEST-ROI UNBLOCK: Fix battle.py to drain stderr')实测 stale 准。ZERO import(grep research_governance 在 experience_pool/experience_archivist/battl


**测试缺口：** 缺 2 个关键测试:(1) 致命缺陷的直接测试——应有一个测试断言"当 commit 已通过(precommit passed),L401 hook 的 won 恒为 True",把上述 blocking issue 固化为 regression guard。当前 6 个测试里 test_commit_records_lesson_outcome_from_precommit 假设 won==precommit_passed 可变,但生产代码路径上它恒 True,测试通过≠机制 work——这正是 project #1 INERTNESS failure mode(fixture!=live reachability,记忆 v137/v140/v143 反复记录)。(2) 缺 backward-compat 测试:确保叠加的 retire pre/post filter 不破坏现有 tag


---


## 5. 阻断点汇总 + 修正方案


| fix | 阻断点 | 修正方案 |
|---|---|---|
| fix-1-register-literature-probe | 无。计划可直接落地,核查全部通过。 | 计划可直接落地,核查全部通过。修正/补充要点:

1) 行号核实(全部准确,亲自读代码确认):
- tool_planning.py:910 @tool 注册 + 911-1059 实现 ✓(L995 if False 是无害 stale code 但不在本次 fix scope)
- tool_pipeline.py:3 re-export ✓
- tools.py:32 import ✓ /  |
| fix-2-critic-calibration-async | 无阻断性问题,plan可直接执行。唯一需实施者决策的是:reconcile对"已reconciled=true的行"二次调用时,delta是否用当前ratings重算(会随source bot继续收敛而漂移)还是冻结首次回填值。plan未明确该语义。推荐"reconciled后冻结delta"(读时跳过reconciled==true的行),这样幂等性最强、语义最清晰;补一个test_reconc | 修正/补充要点:(1)【RD阈值应默认放宽——最大可优化点】实测当前30个active bot仅1个rd<60(最低40.5,其余95-350,中位数~150)。plan默认RECONCILE_RD_THRESHOLD=60几乎永不触发→校准仍死,违背fix目的。建议默认直接设~90(对齐EVAL_RD_THRESHOLD语义但更现实),或用EVAL_RD_MIN_GAMES=20+rd<100组 |
| fix-3-placement-shadow-blocking | 1. GRANDFATHER 边界根本性错误: 计划称 "v166 已发布带 TRUE-SHADOW，豁免 v<=166"，但实测显示 v166 是未追踪目录(?? bots/claude_v166/)、无 bot-v166 tag、非 current_v(current_v=165)。真正带 TRUE-SHADOW 的已发布版本是 v138-v161(24个带标记bot)，而 v162-v165 | 1. 修正 grandfather：要么完全移除（推荐，因为 idempotency 已保障不回溯），要么改为 GRANDFATHER_V = 165（当前 current_v，而非计划的 166）。无条件 BLOCKING 对 v167+ 生效是正确行为——这是预期的强制 relocate 机制。
2. 修正 role 参数：_record_quality_failure(v, "placeme |
| fix-4-event-driven-precommit | Test design needs correction: (1) monkeypatch must target `tool_planning._run_master_analysis` (the module-level import at L17), not `agent_master._run_master_analysis`; (2) tests calling `_handler(ar | 1. Guard insertion point verified: L442=pass(end of try/except), L443=stagnation_info. _matching_checkpoint() is already imported in tool_planning.py(L21) and handles next_v+source_v matching — reuse  |
| fix-5-critic-cross-gen-pivot | 有两个阻断性问题需要在实施前修正：

**BLOCKER 1 (严重): infra_only_timeout 污染 cross-gen fail 计数**
计划的风险分析声称 "tool_eval L968 分支已设 passed=True(infra 不算 fail,只 directive 重试 precommit),故不会污染计数" —— 这是**事实错误**。
实测：`tool_eval. | 核查后的修正要点：

**必须修正:**
1. 计划第 7 项修改中的 cross-gen outcome 记录：必须排除 infra_only_timeout 场景。在 tool_eval.py run_precommit_eval 中，`passed` 在 infra_only_timeout 时为 False（因 infra_blockers 非空导致 blockers 非空），且 infr |
| fix-6-vendi-crossover | [1. FINGERPRINT EXTRACTION FEASIBILITY: plan 说"从 decision_tester fixture 抽 7 维 key(round_idx/made_str_bucket/pot_odds_bucket/to_call_to_pot_bucket/is_opp_aggr/street/action_bucket)→RFF"。但实际 test_scena | [1] BLOCKING: scenario input 缺少 made_str/pot_odds/to_call_to_pot/is_opp_aggr 字段——plan 的 RFF fingerprint 设计需要返工。可行替代：(a) 从 scenario 的 history 中推断 round/street(通过 public_cards 长度)、action_bucket(通过 class |
| fix-8-evidence-gate-extend | 1个需返工的设计矛盾 + 1个需修正的风险论证: (B1) plan 第2个 test test_check_citations_accepts_valid_anchored_citation 断言 "_check_citations(anything, None/{}) 返回 []",把 None(manifest缺失=跳过) 与 {}(manifest存在但citations为空=所有GxHx | 可行且高价值(fabrication 是 v127-v143 实证 9x 复发痛点,experience_pool_audit_io.txt L52194 明确记录第4次连续复发)。核查后修正要点:(R1) B1 必须返工——_check_citations 区分 None(跳过)与 {}(判fabricated),否则master-path重构会削弱既有gate引入INERTNESS旁路,违反零 |
| fix-10-battle-experience-dedup | 两个阻断性问题,plan 不可直接执行:
(A) 🔴 核心前提被实测数据证伪:fingerprint(pair+result-bucket)分布为 574 fps×1 / 51 fps×2 / 2 fps×3 / 0 fps>3。设 LIMIT=3 → skip 0 matches(0.0%);LIMIT=2 → skip 2(0.3%)。根因:每 evolution gen 造新 bot 版本, | 【file:line 核查】大部分准确:POLL_INTERVAL=20@54/TARGET_BATCH=6@55/MAX_ANALYSES_PER_HOUR=240@57 ✓;mark_analyzed@130-139 schema{fail_count:int} ✓;get_unanalyzed 三层过滤(done/poison@190/evicted@196-199) ✓;_apply_ba |
| fix-12-experience-pool-ratchet | 🔴🔴 归因信号在所选 hook 点恒为 True(致命缺陷,plan 不可直接执行)。计划把 record_lesson_outcome hook 放在 tool_commit.py:401(run_archivist 内 _append_experience_updates call site),信号源 won=precommit_passed。但 commit_bot 的 gate ledge | 核查结论:file:line 全部真实可核查,但有一个致命逻辑缺陷 + 一个审计冲突,plan 不可直接执行,需返工。

【行号核查(全部亲自核对)】research_governance.py: RETIRE_N_MIN=30@L46 / RETIRE_TAU=-0.10@L48 / score_candidate@L100-105 / record_outcome@L250-289 / _re |