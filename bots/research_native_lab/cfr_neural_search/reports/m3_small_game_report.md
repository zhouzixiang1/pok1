# B 路线 M3：小型博弈正确性与可恢复训练报告

日期：2026-07-12（Asia/Shanghai）

base：`6ee160c93cee8d0afdad111c4c82bc6ddb6012ca`

分支：`codex/research-cfr-neural-search`

## 1. 阶段结论

- **M0 来源/许可证门：通过。** 原论文、官方页面、OpenSpiel 固定 commit 与许可证已进入可审计矩阵。
- 来源 registry SHA-256：`decd4aff03b70577ede9696f7daa091f9bc745446bcc6416c469f5e13807df8f`。
- **M3 小型博弈算法门：通过。** Kuhn 解析均衡、Leduc 完整树、exact best response、External-Sampling MCCFR、Linear/CFR+/DCFR、checkpoint/resume 和 deterministic shard 均有自动测试与实测证据。2026-07-13 的独立复核又补入 full-tree sampling 对拍、OpenSpiel 非均匀策略对拍、depth-limited leaf 合同和 exact Kuhn safe-replacement 证书。
- **M1/M2 国赛共同合同门：不在本分支所有权内，仍为后续硬前置。** 本结论不授权 HUNL 大训练，不代表 NationalGameState/TCP oracle 已通过。
- **未实现**：HUNL blueprint、神经反事实叶值、可扩展 HUNL safe/continual solving、动态 sizing、对手后验、70 手控制器和 native TCP Bot。新增的小型 safe resolver 不能冒充这些模块。

## 2. P0 DCFR 审核与纠正

首轮实现曾错误地依据旧累计 regret 的符号选择 DCFR discount，并在加入当前 delta 之前折扣。该实现和其 DCFR 数字全部作废，未进入本报告结果。

修正后 checkpoint/shard `FORMAT_VERSION` 升为 2，语义为：

1. `R_tilde = R_{t-1} + r_t`；
2. 依据 **R_tilde 的符号** 选择 `alpha` 或 `beta`；
3. `R_t = R_tilde * t^exponent/(t^exponent+1)`；
4. 平均策略本轮贡献权重为 `t^gamma`；
5. LCFR 使用相同 post-add 顺序，regret factor 为 `t/(t+1)`，平均策略权重为 `t`。

新增方程级测试，不以 exploitability 趋势替代公式验证：

- 正→负：`0 --(+4,t=1)--> +2 --(-5,t=2)--> -1.5`，第二轮必须使用 `beta=0`；
- 负→正：`0 --(-4,t=1)--> -2 --(+5,t=2)--> 3 * 2^1.5/(2^1.5+1)`，第二轮必须使用 `alpha=1.5`；
- LCFR 两轮：`0 --(+2,t=1)--> 1 --(+2,t=2)--> 2`。

实现顺序与固定 OpenSpiel commit `6c8edc829962967730e5ff353340df75847fa184` 的 `discounted_cfr.py` 对照；该 OpenSpiel 文件自身注明其为社区贡献且未保证复现论文实验，因此本项目仍以论文公式和方程测试为最终门。

## 3. 游戏树与 exact evaluation

| 游戏 | 唯一状态 | 终局 | P0/P1 信息集 | 均匀策略 exploitability |
|---|---:|---:|---:|---:|
| Kuhn | 58 | 30 | 6 / 6 | 0.4583333333333333 |
| Leduc | 9457 | 5520 | 144 / 144 | 2.373611111111111 |

Kuhn 解析均衡验证：

- P0 期望值：`-1/18 = -0.055555555555...`；
- P1 期望值：`+1/18`；
- exact NashConv：数值容差内为 0；
- best response 在同一信息集的所有底层历史上强制使用同一动作。

Leduc 使用 6 张物理牌、3 个 rank、每 rank 两份；每人 ante 1，两个下注轮，固定 bet increment 2/4，每轮最多两次 bet/raise。机会树使用物理牌，信息集只暴露己方 rank 与公共信息。

## 4. 冻结配置结果

所有结果均为修正 DCFR 语义后重新运行。`exploitability = NashConv / 2`。

### Kuhn Linear CFR

- 配置：`configs/kuhn_m3_linear.json`
- 配置 SHA-256：`751782b57eefec23cf84ae38e143402c6e2e557efba9a8761c0f8249e8374b76`
- 5000 batch，4 samples/player，4 deterministic shards；40000 trajectories。
- exploitability：`0.0076135008838882495`。
- on-policy P0 value：`-0.055583034136558684`。
- node touches：`291457`。
- 内部训练耗时：`4.6976s`；wall `4.77s`；峰值 RSS `28616 KiB`。
- final state SHA-256：`9d8a7b3178b7b76f022b62938636452863960e9edd657b52b82bf631b5957f3c`。

### Leduc Linear CFR

- 配置：`configs/leduc_m3_linear.json`
- 配置 SHA-256：`a8a0a3a4c17595cb714536cec37c9e3095b511df5da5b92a4c7dc2505997c6a6`
- 5000 batch，4 samples/player，4 deterministic shards；40000 trajectories。
- exploitability：`0.09552601297147513`，相对均匀策略下降约 95.98%。
- on-policy P0 value：`-0.08952530437324557`。
- node touches：`898403`；发现全部 `288` 个双方信息集。
- 内部训练耗时：`106.3705s`；wall `107.13s`；峰值 RSS `40116 KiB`。
- final state SHA-256：`91a74660c1118a7523331efbe873bd20a76b8589fbbfc8eef97db41cc2b1bb28`。

### Leduc DCFR

- 配置：`configs/leduc_m3_dcfr.json`
- 配置 SHA-256：`bff9f7d9ba3a19201be6c1b56a17cd13445232e3860cd481960ced34ca514fff`
- 5000 batch，4 samples/player，4 deterministic shards；40000 trajectories。
- 参数：`alpha=1.5, beta=0, gamma=2`。
- exploitability：`0.15524020236904074`，相对均匀策略下降约 93.46%。
- on-policy P0 value：`-0.09962854488810863`。
- node touches：`892964`；发现全部 `288` 个双方信息集。
- 内部训练耗时：`89.3292s`；wall `89.69s`；峰值 RSS `39704 KiB`。
- final state SHA-256：`effd5fe427db657d0ff50b1c91b4d91ba69780bc2246fb7769caec1f6964ff14`。

本固定预算下 Linear 优于 DCFR；这只是 M3 小型同步-batch实现的结果，不推断未来 HUNL 排名。

## 5. 三种更新的独立 Kuhn 冒烟

共同设置：seed 17，5000 batch，1 sample/player，sampled averaging。

| 更新 | exploitability | P0 value | final state SHA-256 |
|---|---:|---:|---|
| Linear | 0.011372912155103998 | -0.05525286609494445 | `67c7258057c7867ee0d128af9769875c75fce24fa18b88ea64d0e3720b82a7e4` |
| CFR+ | 0.015111528339227617 | -0.05552023617191615 | `4bbc24fbed29408be5a72845e0c00d4e3d79bda083219e671f2e59cd7c4fb54a` |
| DCFR | 0.018195419817400207 | -0.0537853334069302 | `8ed45b0aede1ebfd92d924f404b2975215547398815f84ceab972a47cd4d3065` |

三者均通过 `< 0.06` 的自动 Kuhn exploitability 门。此表中的 DCFR 是 P0 修正后重跑结果。

## 6. Checkpoint 与 shard 合同

- RNG seed 由 `(base_seed, batch_index, traverser, sample_id)` 经 BLAKE2b 派生；不依赖 worker 调度顺序。
- 每个 shard 绑定冻结 state digest、batch index、shard index/count 和 samples/player。
- reducer 必须收到完整且不重复的 shard/sample 覆盖；缺失、重复、错误 base digest 均 fail closed。
- sample delta 按 `(traverser, sample_id)` 排序后使用 `math.fsum` 规范归并。
- 所有 sample action/vector/finiteness 先完整预验证；NaN、负 strategy delta、未知 action 和 action drift 均在 staging 前拒绝。
- reducer 在完整 `SolverState` clone 上更新；仅在 staged state 通过完整 `validate()` 后交换到调用方。
- `validate()` 拒绝 `regrets`/`strategy_sum` 中未在 `actions` 注册的 orphan infoset。
- 1 shard 与 4 shard 对同一 batch 产生完全相同 payload 和 SHA-256。
- 30 batch → 原子 checkpoint → load → 50 batch，与不中断 80 batch payload/SHA 完全相同。
- checkpoint 和 shard 外层均绑定 SHA-256；篡改 payload 会被拒绝。
- 注入 NaN 或 action drift 的失败 apply 后，原 state digest 与 canonical payload bytes 均逐字节不变。

当前 per-sample JSON delta 是 correctness format，不是 HUNL 最终存储格式。后续压缩/并行 reducer 必须先证明与此规范 reducer 等价。

## 7. 验证命令

```bash
python -m py_compile $(rg --files bots/research_native_lab/cfr_neural_search -g '*.py')

python -m unittest discover \
  -s bots/research_native_lab/cfr_neural_search/tests \
  -p 'test_*.py' -v

/usr/bin/python3 -m unittest discover \
  -s bots/research_native_lab/cfr_neural_search/tests \
  -p 'test_*.py' -v

python -m bots.research_native_lab.cfr_neural_search.tools.train_small_game \
  --config bots/research_native_lab/cfr_neural_search/configs/kuhn_m3_linear.json

python -m bots.research_native_lab.cfr_neural_search.tools.train_small_game \
  --config bots/research_native_lab/cfr_neural_search/configs/leduc_m3_linear.json

python -m bots.research_native_lab.cfr_neural_search.tools.train_small_game \
  --config bots/research_native_lab/cfr_neural_search/configs/leduc_m3_dcfr.json
```

初次提交结果：默认 Python 3.14.4 为 21/21 tests passed（7.936s）；系统 Python 3.12.3 为 21/21 passed（10.629s）。2026-07-13 严格复核扩展为 38 tests，准确命令、数值和依赖哈希见 `m3_audit_2026-07-13.md`；旧 18-test 运行不作为最终证据。

## 8. 已知限制与下一门

1. 同步 mini-batch MCCFR 是明确的 `functional adaptation`，不是逐 trajectory 更新的逐位复刻。
2. 目前 shard 计算为单进程顺序执行；本里程碑证明可合并性，不宣称多核加速已经交付。
3. Leduc 已用隔离安装的 OpenSpiel 1.6.15 对拍树、均匀策略和确定性非均匀策略；该依赖仍不进入运行时，且官方 wheel 与 source registry 固定 commit 是两份分别记录的证据。
4. exact best response 只适合小型树，不是未来 HUNL exploitability oracle。
5. 没有创建、训练或提交任何 HUNL 大资产。
6. 下一步必须等待共同 M1/M2 的 NationalGameState、合法动作和 TCP 状态合同冻结，然后才能实现 B blueprint-only Bot；不能直接跳到神经网络。
7. 当前 depth-limited leaf 是完整私有状态上的 tabular correctness interface；当前 safe certificate 依赖 Kuhn 完整 exact BR。二者都不是 range-conditioned HUNL leaf/gadget，M7 前必须重新证明。

## 9. 阶段门判定

**M3 局部正确性门通过；B 路线整体仍不具备晋级或大训练条件。**

P0 DCFR 更新顺序已有公式测试、代码修正和 Kuhn/Leduc 重跑闭环。若后续独立 OpenSpiel 对拍发现规则或数值差异，应重新打开 M3，而不是解释为可忽略误差。
