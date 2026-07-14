# B 路线 M3 最终门：Common 集成、CFR 正确性与 fail-closed evidence

日期：2026-07-14（Asia/Shanghai）

分支：`codex/research-cfr-neural-search`

审计起点 HEAD：`042272193bca620e25decf8850fc726edf2e6fb9`（Common merge；本报告对应其上的未提交工作树，最终提交由主代理完成）

Common：commit `cc8beed256024fadd1cf89b0e40dcdea6a5c959d`，Git tree `9cfa297b8c61024154990c775962d67aa3f0543b`，合同版本 `national-research-contract-v1`

## 1. 门结论

**B 路线局部 M3 门通过；M4/HUNL 未开始，也未被本报告授权。**

已完成并实测：

- clean-room Kuhn/Leduc extensive game；
- External-Sampling MCCFR；
- Vanilla、Linear CFR、CFR+、DCFR 更新与分离的 regret / average-strategy accumulator；
- deterministic sample/shard/canonical merge、严格 checkpoint/shard、断点续训；
- exact value、best response、NashConv/exploitability 与 OpenSpiel 独立对拍；
- 内容绑定 exact-rollout depth limit；
- exact Kuhn oracle-filter replacement 及明确的局部约束/全局 oracle 负例；
- Common NationalGameState/Action/LegalActionSet 的真实策略入口适配。

尚未实现：HUNL abstraction/blueprint、压缩并行 reducer、神经反事实叶值、public-range resolving gadget、动态 sizing、对手模型、70 手控制器、TCP socket owner 或可参赛 bot。

## 2. CFR/MCCFR 审计结果

### 2.1 External Sampling

- traverser 节点枚举全部 action；chance/opponent 节点按冻结 regret-matching policy 采样；
- 每个 batch 的 shard 从同一冻结 state 计算，sample RNG 由 `(seed, batch_index, traverser, sample_id)` 派生；
- reducer 要求完整唯一 shard/sample 覆盖，并按 `(traverser, sample_id)` + `math.fsum` 规范合并；
- present regret/strategy delta vector 必须覆盖该 infoset 的完整 action set，删掉一个 action 后即使重算 envelope SHA 也拒绝；
- uniform Kuhn 上 20,000 samples/player 与独立 full-tree CFR regret delta 对拍，历史实测最大绝对误差 `0.006049999999989508`，门限 `0.012`。

同步 batch 是明确的工程 adaptation：一次 batch 使用 sample mean 做一次 accumulator 更新；它不声称与每条 trajectory 后立即更新的串行 MCCFR 逐位相同。

### 2.2 更新公式

`test_update_rules_reference.py` 用另一份 literal recurrence 对含多次跨零的 delta 序列逐轮对拍：

- Vanilla：`R_t = R_(t-1) + r_t`；
- CFR+：`R_t = max(0, R_(t-1) + r_t)`；
- Linear：`R_t = (R_(t-1) + r_t) * t/(t+1)`，与 iteration-weighted regret 的统一缩放形式相等；
- DCFR：先算 provisional regret，再按其新符号选择 alpha/beta discount；
- average weights 分别为 `1`、`max(0,t-delay)`、`t`、`t^gamma`。

Kuhn 对 Linear/CFR+/DCFR 均有 `<0.06` exact exploitability 门；Leduc 对三种更新均以 1,000 batch 从 uniform `2.373611111111111` 降到 `<0.75`。regret 与 strategy accumulator 的对象、表和值均分别验证，负 strategy sum 不再被 clamp。

### 2.3 冻结配置重跑

三份 5,000-batch 配置在本次硬化后重跑；算法 state SHA 与 2026-07-12/13 完全相同，说明输入拒绝门没有改变 accumulator 数值语义。

| 配置 | trajectories | exploitability | state SHA-256 | elapsed | max RSS |
|---|---:|---:|---|---:|---:|
| Kuhn Linear | 40,000 | `0.0076135008838882495` | `9d8a7b3178b7b76f022b62938636452863960e9edd657b52b82bf631b5957f3c` | 3.8225s | 19,704 KiB |
| Leduc Linear | 40,000 | `0.09552601297147507` | `91a74660c1118a7523331efbe873bd20a76b8589fbbfc8eef97db41cc2b1bb28` | 57.2524s | 32,184 KiB |
| Leduc DCFR | 40,000 | `0.15524020236904076` | `effd5fe427db657d0ff50b1c91b4d91ba69780bc2246fb7769caec1f6964ff14` | 60.2708s | 28,952 KiB |

后两项并行运行，耗时只作本机资源记录。固定小预算下 Linear 优于 DCFR，不可外推 HUNL 排名。

## 3. Exact evaluation 与独立 reference

Exact evaluator 现在 fail closed：

- 只有整个 `{}` 是显式 uniform profile；
- 任一非空 profile 必须覆盖完整游戏 infoset schema，额外/缺失 infoset 均拒绝；
- 每行 action key 必须与 legal action 完全相等；
- bool、数字字符串、负值、NaN/Inf、非归一概率均拒绝，不 clamp、不补行、不重归一；
- material-negative best-response improvement/NashConv 抛错；仅 `1e-12` 内浮点负噪声可显式钳零，并同时保留 `raw_player_improvements` 与 `numerical_tolerance_clamped`。

OpenSpiel 只在隔离 `/tmp` 中作审计依赖，不进入训练或交付运行时：

```bash
/home/zzx/.cache/pok-research-py312/bin/python -m pip install \
  --disable-pip-version-check \
  --target /tmp/pok-open-spiel-1.6.15 \
  'open-spiel==1.6.15'

PYTHONPATH=/tmp/pok-open-spiel-1.6.15 \
PYTHONDONTWRITEBYTECODE=1 \
/home/zzx/.cache/pok-research-py312/bin/python -m \
  bots.research_native_lab.cfr_neural_search.tools.crosscheck_open_spiel \
  --expected-version 1.6.15
```

OpenSpiel wheel：`open_spiel-1.6.15-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl`，SHA-256 `7b2814bf39f2ab9302723af1f3934c842cefa9371ec77b1ebf8400fb917df793`，Apache-2.0。隔离解析环境还含 numpy 2.5.1、scipy 1.18.0、absl-py 2.5.0、attrs 26.1.0、ml-collections 1.1.0、PyYAML 6.0.3；它们均不是 route runtime 依赖。缺少可选 `pokerkit_wrapper` 只产生 OpenSpiel 提示，不影响本次游戏。

最终 10/10 checks 为 true。除 Kuhn/Leduc tree、uniform 和非均匀 Leduc 对拍外，工具直接训练冻结的 500-batch Leduc Linear policy，再交给 OpenSpiel 独立 best response：

- 288/288 route infosets；1,000 trajectories；
- state SHA `0b5f2d6094a43cae5104657828b1572a8208328744f0fbdfd7fd0ba4274370a8`；
- route/OpenSpiel value 均为 `(-0.08158124598773397, +0.08158124598773397)`；
- route/OpenSpiel NashConv 均为 `1.9753252933862577`；
- route/OpenSpiel exploitability 均为 `0.9876626466931289`。

最终隔离复跑：wall 4.60s，max RSS 88,168 KiB；可选 `pokerkit_wrapper` 缺失提示不影响 10/10 结果。

`0.9876` 只证明训练后 policy 也能被独立 evaluator 逐值复现并相对 uniform 改善，不代表策略“强”。

## 4. Checkpoint、恢复与 evidence 完整性

- `SolverState` 分离 `regrets` 与非负 `strategy_sum`；current/average export 前再次 validate；
- checkpoint/shard envelope SHA 只证明字节完整性，载入后仍执行严格 schema/type/finite/action coverage 验证；
- JSON bool/float/string 不得冒充 integer，数字字符串/bool 不得冒充 accumulator 数值；未知/缺失字段、重复 JSON key、NaN 常量拒绝；
- 重算合法 SHA 的坏 checkpoint（负 strategy sum、类型漂移）仍拒绝；
- 坏 shard 删除一个 present action delta 或更改 sample identity 后仍拒绝；
- 即使完全绕过 JSON、直接用 `dataclasses.replace` 构造内存 `ShardDelta`/`SampleDelta`，bool/string 冒充 index/count/value 也在排序、取模和聚合之前拒绝；每个失败分支均断言 state canonical bytes 不变；
- apply 先完整校验、在 clone 上事务更新，失败不改变调用方 state bytes；
- 1/4 shard 单批及 40 批 payload/SHA 相同；
- CFR+ 30+50、Linear/DCFR 17+33 恢复结果与不中断及改变 shard layout 完全相同；
- 文件写入使用同目录临时文件、文件 fsync、atomic replace、目录 fsync。

当前 per-sample JSON 是 correctness format，尚不是可扩展 HUNL backend；SHA/严格 schema 也不是对训练语义或来源的签名证明。

## 5. Depth limit 与“safe”边界

### 5.1 Exact rollout leaf

`LeafValueContract` 不再接受 caller 自报 identity 的任意 lambda。M3 唯一权威构造 `rollout_leaf(game=..., policy=...)` 会：

- 构建时验证完整 exact profile；
- 拒绝 bool/字符串/负/非有限/非归一概率；
- snapshot policy；
- 穷举完整有限树，将每个节点的 actor/depth、infoset、合法动作顺序、chance action/概率、action→child 转移和 terminal payoff 与 game type/repr、policy 一起哈希；
- sealed contract 与 game binding 进入 `DepthLimitedGame.name`，跨 game 或任意同名 callable 复用均拒绝；
- 同 type/name/repr/schema 的测试游戏只要改变 chance 概率、terminal payoff 或 action transition，已有 leaf 和新 `DepthLimitedGame` 均 fail closed；
- cutoff return 再拒绝 bool/字符串、非有限和非零和叶值。

它仍是完整私有状态上的 exact tabular rollout，不是双方 range-conditioned counterfactual value vector，更不是神经网络资产接口。未来 neural leaf 必须另建签名内容 receipt。

### 5.2 Oracle-certified Kuhn replacement

`KuhnSafetyConstraint` 冻结 blueprint policy SHA 和 P1 三个 top-infoset CBV 上界。`OracleCertifiedKuhnResolveCertificate` 分开记录：

1. `local_cbv_constraints_satisfied`；
2. `resolver_best_response_invariant`；
3. `global_exploitability_oracle_satisfied`。

acceptance 同时要求三者。plain unsafe、adversarial call vector、subtree 外修改都有负例。完整树 global exploitability 是 acceptance oracle-filter；本实现**没有证明局部 CBV 约束可推广，也没有实现 HUNL public-range gadget**。因此不得把这个类型或“safe”名称继承为未来 HUNL 安全证明。

## 6. Common M0-M2 接入

`native_runtime/common_adapter.py` 是后续策略的强制 seam：

- `invoke_route_policy` 只接受精确 Common `NationalGameState`，交给 policy 的 snapshot 保留精确 Common `LegalActionSet`；
- policy 只能返回精确 Common `Action`；
- subclass 覆盖 `to_wire` / `full_state_id` 的伪造对象拒绝；
- controlled player 必须是精确 int；snapshot 的三个 identity 必须是精确 64 位小写 SHA-256 字符串，legality 必须是精确 Common `LegalActionSet`；
- action 绑定精确 64 位 full-state SHA；AlwaysEqual/equality gadget、str/Action/State/LegalActionSet subclass 均拒绝，state 前进后 stale bind/apply 拒绝；
- raise-to 200/19,999 与 allin keyword 边界由 Common oracle 产生和复验，route 不复制合法性；
- Common serialize/restore 后 projection/legality identity 相同。

集成测试内容绑定 Common commit/tree，并逐文件校验：

| Common 文件 | SHA-256 |
|---|---|
| `__init__.py` | `5b843901602df8299f3fd845b346385fa6ff87c9aa807ef0023abf55ff8ff384` |
| `actions.py` | `69d1f5667f35ef7db3092f8afc358d8fa14f26f430246983caac8d1ac43dacaa` |
| `cards.py` | `492e89baf3b1db4f9b87f62d5f63964e22fdc998e27928c8bcb75bec6df52bce` |
| `constants.py` | `8f7116becae35ccbdf6d1ff5004a7b07dec7b6ac793ecb6d55bd05ffc8818783` |
| `national_state.py` | `6bdb467fcedf114948843419ffdf58abd8c5e545243fb6b63a15d6be6d02dbc4` |
| `protocol.py` | `ec325068ebf905b7dcd30f180e4b6dc941b1260bba788800b48ca7ba798c5a0c` |
| `contracts/national_game_v1.json` | `e23831c0e83349a576658938b450b044cf527a1c4452284b6efa21445c09ffab` |

任一关键字节漂移会重新打开 route M3 integration test。完整 Common tree binding 为 `9cfa297b...543b`。

这个 seam 不是 TCP bot：尚未接 Common protocol decision lease、deadline/resource enforcement、supervisor receipt 或 wire capture；这些不能由 action adapter 假装完成。

## 7. 最终自动验证

```bash
/usr/bin/time -f 'ELAPSED_SECONDS=%e MAX_RSS_KIB=%M' \
  env PYTHONDONTWRITEBYTECODE=1 \
  /home/zzx/.cache/pok-research-py312/bin/python -m pytest \
  -p no:cacheprovider \
  bots/research_native_lab/cfr_neural_search/tests -q
```

结果：Python 3.12，`72 passed, 80 subtests passed in 173.90s`；wall 174.02s；max RSS 49,500 KiB。

```bash
python --version
/usr/bin/time -f 'ELAPSED_SECONDS=%e MAX_RSS_KIB=%M' \
  env PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest -p no:cacheprovider \
  bots/research_native_lab/cfr_neural_search/tests -q
```

结果：Python 3.14.4，`72 passed, 80 subtests passed in 156.77s`；wall 156.93s；max RSS 81,928 KiB。

Common critical-path 联合门：

```bash
env PYTHONDONTWRITEBYTECODE=1 \
/home/zzx/.cache/pok-research-py312/bin/python -m pytest \
  -p no:cacheprovider \
  bots/research_native_lab/common_contracts/tests/test_national_state.py \
  bots/research_native_lab/common_contracts/tests/test_protocol.py \
  bots/research_native_lab/cfr_neural_search/tests/test_common_adapter.py -q
```

结果：`35 passed, 16 subtests passed in 0.21s`；wall 0.32s；max RSS 33,912 KiB。

另有 31 个 Python 文件 `py_compile` 全过；`configs/` / `manifests/` JSON 全部 `json.tool` 解析通过。

清单不是手工维护的 SHA 表。以下命令会重算三组 5,000-batch 冻结训练、500-batch Leduc route 侧证据、两组快速状态夹具、Common 完整 Git tree/关键文件以及除清单自身外的完整 route 文件集合，再用同目录临时文件 + fsync + atomic replace + 目录 fsync 写入，最后立即验证：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m \
  bots.research_native_lab.cfr_neural_search.tools.verify_m3_gate --write
```

生成器只接受当前 imported route 根下的默认 manifest；tool、evaluation、mccfr、small_games、core.game、common_adapter 及 Common package/actions/cards/constants/state/protocol 的 resolved `__file__` 必须逐个命中该 route/Common 根，复制树或 symlink 路径直接拒绝。约两分钟的数值重算前后会对完整 route file map、Common tree/critical files 和 solver input digest 做双快照；write 前后再次比较，混合时点证据会失败，且已替换清单会原子恢复旧 bytes。verify 也在重算前后复验。

测试覆盖 render→verify、外部复制树/module-root mismatch、计算中 snapshot drift、write rollback，以及额外文件、漏报文件、symlink 和内容漂移失败。生成器自身和测试自身也进入 file map；清单唯一排除项是 `manifests/m3_gate_20260714.json`，因为文件不能包含自身 SHA。

一次完整 `--write`→verify 实测收据：42 个 route 文件；Common tree、三份 5,000-batch state、两份快速 fixture 和 500-batch reference state 全匹配；wall 128.02s，max RSS 49,892 KiB。最终清单必须由同一命令在所有报告/代码定稿后重建，最终收据以清单和交接记录为准。

## 8. 当前关键 SHA

完整、机器可复验的当前 SHA 和动态证据以生成后的 `manifests/m3_gate_20260714.json` 为准。

## 9. 许可证与 clean-room 边界

- 实现为本项目 clean-room Python，未复制外部 solver 源码；
- 论文只作数学参考，不授予软件代码许可；
- OpenSpiel Apache-2.0 只作隔离 read-only numerical oracle；
- DecisionHoldem AGPL 只保留边界记录，未复制其代码或派生实现；
- runtime/stdlb 路径不 import OpenSpiel/numpy/scipy。

来源、版本、许可和用途以 `manifests/source_registry.json` / `reports/m0_research_license_matrix.md` 为准。

## 10. M4 前硬缺口

1. 用 compact/mmap/多进程 backend 替换 per-sample JSON 前，必须证明与 M3 reducer 等价，并保留 deterministic resume；
2. 先做小规模 blueprint-only native vertical slice，接上 Common decision lease、deadline/resource、wire capture；
3. HUNL action/card abstraction 要版本化并有 off-tree translation falsifier；
4. neural leaf 必须有 range-conditioned target、独立数据 split、签名 artifact receipt 和 blueprint-only ablation；
5. online resolve 必须另行实现 public-range gadget/约束；Kuhn global oracle-filter 不得复用为证明；
6. 未完成以上门之前，不启动大规模 HUNL 训练，不创建 national bot，不做强度结论。
7. M4 及任何比赛评测只允许 `sever/` 国赛 TCP/raw socket 或 Common `native_harness`（底层 `sever/engine`）；顶层 `engine/`、`engine/battle.py` 与 Botzone JSON stdin/stdout 对局后端已废止且禁止使用。

最终判定：**M3 correctness/integration pass；M4、M5+ 与 HUNL strength 均为 not started。**
