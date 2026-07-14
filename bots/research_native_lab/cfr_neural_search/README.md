# CFR + Neural Leaf + Online Search：B 路线研究包

本目录独立实现项目原生 B 路线。当前工作树把 Common M0-M2 固定合同 `cc8beed2` 接入 M3 小型博弈正确性底座；尚未实现国赛 HUNL 蓝图、神经叶值、可扩展在线搜索、对手后验或 native TCP Bot。

## 当前能力

- clean-room Kuhn 与 limit Leduc 游戏树；
- external-sampling MCCFR；
- 分离的 cumulative regret 与 average-strategy accumulator；
- `vanilla`、Linear CFR、CFR+、DCFR；
- sample-id 派生 RNG、冻结 batch、确定性 shard 与 canonical merge；
- SHA-256 绑定的原子 checkpoint/shard 文件；
- exact behavioral-policy value、best response、NashConv 和 exploitability；
- exact evaluator 对非空 profile 要求完整 infoset/action schema，拒绝缺行、垃圾行、bool/字符串、负值、非有限值和非归一概率；
- best response 的逐信息集 counterfactual value 证书，以及 Kuhn 纯策略穷举对拍；
- Kuhn full-tree CFR delta 对 External-Sampling 采样均值的独立无偏性检查；
- 哈希绑定 leaf identity 的 depth-limited game wrapper；收据穷举绑定 actor、infoset、合法动作顺序、chance 概率、子节点转移和终局收益；
- Kuhn `check → bet` 公共子树的 `OracleCertifiedKuhnResolveCertificate`：局部 CBV 约束与完整树 exploitability oracle 分开记录；
- 严格 JSON checkpoint/shard schema：SHA 只证明完整性，载入后仍拒绝类型强转、负 average accumulator 和不完整 action delta；
- 受 Common commit/tree/关键文件 SHA 绑定的 NationalGameState/Action/LegalActionSet 策略入口；
- 可选 OpenSpiel 审计工具，对拍 Kuhn/Leduc 树、非均匀策略值和 exploitability；
- Python stdlib-only，可在 Python 3.12/3.14 运行。

同步 batch 是明确的工程适配：所有 shard 从同一冻结策略采样，再按 `(traverser, sample_id)` 规范顺序合并。它保证 shard 调度不改变结果，但不声称与每条 trajectory 后立即更新策略的串行 MCCFR 逐位相同。

## 运行

从仓库根目录执行：

```bash
python -m unittest discover \
  -s bots/research_native_lab/cfr_neural_search/tests \
  -p 'test_*.py' -v

python -m bots.research_native_lab.cfr_neural_search.tools.train_small_game \
  --config bots/research_native_lab/cfr_neural_search/configs/kuhn_m3_linear.json

python -m bots.research_native_lab.cfr_neural_search.tools.train_small_game \
  --config bots/research_native_lab/cfr_neural_search/configs/leduc_m3_linear.json \
  --checkpoint /tmp/leduc-m3-checkpoint.json
```

OpenSpiel 只作为隔离审计依赖，不进入训练或交付运行时：

```bash
PYTHONPATH=/tmp/pok-open-spiel-1.6.15 /usr/bin/python3 -m \
  bots.research_native_lab.cfr_neural_search.tools.crosscheck_open_spiel \
  --expected-version 1.6.15
```

续训必须使用完全相同的 config：

```bash
python -m bots.research_native_lab.cfr_neural_search.tools.train_small_game \
  --config bots/research_native_lab/cfr_neural_search/configs/leduc_m3_linear.json \
  --checkpoint /tmp/leduc-m3-checkpoint.json --resume
```

CLI 的 `batches` 表示本次新增 batch 数，而 checkpoint 内保存累计 `batch_index`。

M3 清单由工具重算本地动态证据和完整文件表；不要手填 artifact SHA。工具只接受当前 imported route 根下的默认清单，绑定所有参与重算模块的精确 `__file__`，并在长计算前后复验 route/Common 双快照；`--write` 漂移时回滚旧清单：

```bash
# 只验证已签入清单
python -m bots.research_native_lab.cfr_neural_search.tools.verify_m3_gate

# 重算冻结训练/状态夹具/Common 绑定/route 文件表，原子写入后立即验证
python -m bots.research_native_lab.cfr_neural_search.tools.verify_m3_gate --write
```

## 目录边界

- `core/`：本路线小型 extensive-game 接口；
- `blueprint/`：小型游戏、MCCFR、checkpoint 和 exact evaluation；
- `online_solver/`：depth-limited leaf 合同与小型 exact safe-resolve 证书；
- `native_runtime/`：仅有 M3 Common-typed 策略入口和 stale-action 绑定；没有 TCP loop；
- `configs/`：冻结的 M3 实验配置；
- `manifests/`：来源/版本/许可证 registry；
- `reports/`：M0/M3 证据；
- `tools/`：可复现 CLI；
- `tests/`：规则、收敛、best response、shard/checkpoint 测试。

`common_contracts/`、`comparison/`、`.evolution_pok` 和 `national_v*` 不属于本路线写权限。

## 下一阶段前的硬门

1. Common `national-research-contract-v1` 已合并且内容绑定；任何绑定文件漂移都会重新打开 M3 集成门；
2. 将 M3 per-sample JSON delta 替换为经证明等价的压缩/并行 reducer 后，才扩大 CFR 状态；
3. M4 先交付 blueprint-only native vertical slice，再考虑神经叶值大资产；
4. 当前 resolver 是 exact Kuhn oracle-filter functional adaptation，不证明局部约束可推广；HUNL 必须另行实现并验证 public-range gadget；
5. `invoke_route_policy` 还没有 HUNL policy 实现，也没有接入 Common decision lease、deadline/resource receipt 或 TCP wire capture，因此不能参赛。
6. 后续 M4 和任何比赛评测只能使用 `sever/` 国赛 TCP/raw socket，或 Common `native_harness`（底层 `sever/engine`）；禁止顶层 `engine/`、`engine/battle.py` 和 Botzone JSON stdin/stdout 对局后端。

来源与许可证见 `reports/m0_research_license_matrix.md`。初始历史结果见 `reports/m3_small_game_report.md`，2026-07-13 复核见 `reports/m3_audit_2026-07-13.md`；Common 集成后的当前门结论、命令和内容哈希见 `reports/m3_common_integration_gate_2026-07-14.md` 与 `manifests/m3_gate_20260714.json`。
