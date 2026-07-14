# CFR + Neural Leaf + Online Search：Route B 研究包

本目录是项目原生 Route B。当前里程碑 M4 已把 M3 小型博弈正确性底座扩展为一个真实国赛 HUNL 的 blueprint-only native vertical slice；神经叶值与可扩展在线搜索仍是后续里程碑，不能把当前交付描述成完整的 DecisionHoldem/ReBeL。

## M4 当前能力

- 真实 2 人无限注：每手 20,000，盲注 50/100，preflop/flop/turn/river；下注合法性、街道闭合、all-in runout 和终局收益全部由共享 Common `NationalGameState` 决定。
- 169 类 preflop 抽象，同时保留完整 1,326 手牌组合索引；postflop 使用牌移除正确、花色同构、确定性的 equity bucket，并包含 board texture、SPR 与有序公共行动历史。
- exact infoset 使用 perfect-recall v2：当前抽象观测之外，还编码按顺序的“过去本人抽象观测 + 本人 action-id”；`inferred_from_boundary` 仅是审计元数据，不进入策略键。
- Common-only 动作集：fold/check/call/min raise/0.5 pot/1 pot/1.5 pot/all-in，经 Common 验证并按真实 raise-to 值去重。
- 同步 external-sampling MCCFR，累计 regret 与 average-strategy mass 分离。两位 traverser 在同一 batch 共享 chance CRN；不同 batch 使用独立 counter 域，不为了 influence gate 重放上一批牌。
- 独立进程 shard 从同一冻结状态重建；完整规则、Common、抽象、后端、formal run config、CLI 和源树快照由摘要绑定，canonical reducer 验证精确 sample 覆盖后事务合并。
- 稀疏压缩 blueprint：只保存正 average mass；运行时顺序为 exact → 由原始 average mass 聚合的层级 backoff → uniform emergency，并分别计数。
- native TCP bot 自己持有 socket/decoder/Common session/decision lease/final send。官方模式发送无分隔符 raw action，默认延迟 0.30 秒；本地 `sever` 模式必须显式选择 LF 和 delay 0。
- 真实本地 `sever` 70 手双边诊断，固定牌序与外部 policy seeds，要求两次完整 semantic projection 相同、双边均受非均匀 blueprint 实质影响、零非法/超时/崩溃。筹码结果记录但验收权重恒为 0。

## 训练规模选择

版本化 config 预注册候选 `2, 4, 8, 16, 32, 64`，唯一规则是选择首个 `materially_nonuniform_all_rows >= required_material_rows` 的训练 batch。selector 只读取训练状态与编译后的策略行统计，显式禁止 TCP 结果、筹码、诊断牌序 seed 和外部 policy seed。

2026-07-14 的受审计 batch-0 发现及独立重放均得到同一 first-pass：`2/4/8/16` 的 material row 均为 0，`32` 首次得到 7 行（exact 2、backoff 5，最大 L1 为 `1.6666666666666667`）。Config 现为 `frozen_first_pass`，完整保存五行 observation，`frozen_selected_batches` 与正式训练 target 均为 32；`--discover` 会被拒绝。正式 selector 必须从 batch 0 重跑逐行对拍，正式训练到冻结 target 后还必须让 solver digest 与最后一行全部训练统计完全相等，才能生成 blueprint。

Selector 每个完整 batch 先原子 checkpoint，再发布不可覆盖的
`events/000000000000.json` SHA 链并更新 hashed heartbeat。Heartbeat 的
completed batch/checkpoint 必须精确投影它所指 authoritative event；resume
只信最后 event tip。内存中尚未 checkpoint 的 batch 与 replay 进度只写入
event details。主机中断留下的 event tmp 不会被截断或删除：resume 保留并
绑定其 SHA，再从 durable checkpoint 生成 recovery event。`events.jsonl`
是每次原子重建的审计视图，不是恢复权威。

任何失效 selector workspace 都先获得不可覆盖的 `INVALIDATED.json`，再把
同一 marker、workspace 身份和 checkpoint/trace 摘要登记到受源快照约束的
`manifests/invalidated_selector_runs/`。入口、resume、正式训练与发布都会
同时检查 marker 和永久 registry；即使 runtime marker 被删，registry 仍
阻断复用。发布还会在读取前后及写 manifest 前复查，最终 manifest 记录
registry 的完整文件清单与摘要。
Invalidator、publish、render、write 与 verify 还共享
`runtime_outputs/.m4-publication-invalidation.lock` 的进程/跨进程独占租约，
把 registry 变更和发布事务线性化；长训练不持有该锁，仍在 batch 边界主动
检查失效状态。

## 可复现命令

从仓库根目录执行。所有 workspace 必须预先创建在本目录的 `runtime_outputs/` 下；checkpoint、heartbeat、CANCEL 和临时 artifact 均不提交。

```bash
# frozen config 从 0 正式重放 selector；可在完整 batch 边界续跑
python -m bots.research_native_lab.cfr_neural_search.tools.select_hunl_scale \
  --workspace bots/research_native_lab/cfr_neural_search/runtime_outputs/m4-selector-final
python -m bots.research_native_lab.cfr_neural_search.tools.select_hunl_scale \
  --workspace bots/research_native_lab/cfr_neural_search/runtime_outputs/m4-selector-final \
  --resume

# 正式 blueprint 训练；每个完整 batch 原子 checkpoint + heartbeat
python -m bots.research_native_lab.cfr_neural_search.tools.train_hunl_blueprint \
  --workspace bots/research_native_lab/cfr_neural_search/runtime_outputs/m4-training
python -m bots.research_native_lab.cfr_neural_search.tools.train_hunl_blueprint \
  --workspace bots/research_native_lab/cfr_neural_search/runtime_outputs/m4-training \
  --resume

# 发布 compact artifact/selector/native evidence，并原子写入当前 M4 manifest
python -m bots.research_native_lab.cfr_neural_search.tools.verify_m4_gate \
  --publish \
  --training-workspace bots/research_native_lab/cfr_neural_search/runtime_outputs/m4-training \
  --selector-workspace bots/research_native_lab/cfr_neural_search/runtime_outputs/m4-selector-final

# 不重新跑对局，只严格验证所有内容绑定
python -m bots.research_native_lab.cfr_neural_search.tools.verify_m4_gate
```

测试：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider \
  bots/research_native_lab/cfr_neural_search/tests -q
```

## 输出与权限边界

- `runtime_outputs/`：忽略的 crash-safe 工作区；永不作为交付证据。
- `artifacts/m4/blueprint.rbbp`：受审计 compact blueprint。
- `artifacts/m4/training_scale_selection.json`：正式 selector 的 hashed evidence envelope。
- `artifacts/m4/local_native_evidence.json`：两次真实 70 手 TCP 的 hashed diagnostic envelope。
- `artifacts/m4/selector_events/`：正式 selector 的完整权威 no-clobber event 文件树。
- `artifacts/m4/selector_events.jsonl`、`selector_heartbeat.json`：由权威链逐字节重建的视图，以及精确指向链尖的最终 completed heartbeat。
- `manifests/invalidated_selector_runs/`：不可覆盖的失效运行永久注册表；属于源输入，不是生成输出。
- `manifests/m4_gate_20260714.json`：当前唯一 M4 本地门权威；显式声明没有 official EXE certification 和 strength 权重。
- `manifests/m3_gate_20260714.json`：M3 历史快照，不再描述当前能力或当前源树。

顶层 `engine/`、Botzone JSON stdin/stdout、Web Arena 和 official EXE 筹码都不得进入本路线 M4 强度或正确性结论。正式比赛资格仍须走仓库既有的 signed official EXE full certification 流程。

发布覆盖现有 M4 输出前会稳定备份全部标量文件及权威 event tree。任一
blueprint/event/selection/JSONL/heartbeat/evidence/manifest 阶段失败，都会
原子恢复旧集合，并逐字节和逐树复验；旧 manifest 最后恢复。

设计、来源和已知限制见 `reports/m4_hunl_blueprint_native_2026-07-14.md`。
