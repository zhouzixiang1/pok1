# CFR + Neural Leaf + Online Search：B 路线研究包

本目录独立实现项目原生 B 路线。当前提交完成 M0 调研/许可证冻结和 M3 小型博弈正确性底座；尚未实现国赛 HUNL 蓝图、神经叶值、可扩展在线搜索、对手后验或 native TCP Bot。

## 当前能力

- clean-room Kuhn 与 limit Leduc 游戏树；
- external-sampling MCCFR；
- 分离的 cumulative regret 与 average-strategy accumulator；
- `vanilla`、Linear CFR、CFR+、DCFR；
- sample-id 派生 RNG、冻结 batch、确定性 shard 与 canonical merge；
- SHA-256 绑定的原子 checkpoint/shard 文件；
- exact behavioral-policy value、best response、NashConv 和 exploitability；
- best response 的逐信息集 counterfactual value 证书，以及 Kuhn 纯策略穷举对拍；
- Kuhn full-tree CFR delta 对 External-Sampling 采样均值的独立无偏性检查；
- 哈希绑定 leaf identity 的 depth-limited game wrapper；
- Kuhn `check → bet` 公共子树的 exact-terminal safe replacement 证书；
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

## 目录边界

- `core/`：本路线小型 extensive-game 接口；
- `blueprint/`：小型游戏、MCCFR、checkpoint 和 exact evaluation；
- `online_solver/`：depth-limited leaf 合同与小型 exact safe-resolve 证书；
- `configs/`：冻结的 M3 实验配置；
- `manifests/`：来源/版本/许可证 registry；
- `reports/`：M0/M3 证据；
- `tools/`：可复现 CLI；
- `tests/`：规则、收敛、best response、shard/checkpoint 测试。

`common_contracts/`、`comparison/`、`.evolution_pok` 和 `national_v*` 不属于本路线写权限。

## 下一阶段前的硬门

1. 共同所有者冻结 NationalGameState、TCP 状态重建和国赛合法动作 oracle；
2. 将 M3 per-sample JSON delta 替换为经证明等价的压缩/并行 reducer 后，才扩大 CFR 状态；
3. M4 先交付 blueprint-only native Bot，再生成神经叶值大资产；
4. 当前 safe resolve 只在 Kuhn 上由完整 exact BR 认证；HUNL 必须另行实现并验证 public-range gadget，不能继承此小博弈证书的名义。

来源与许可证见 `reports/m0_research_license_matrix.md`。初始阶段结果见 `reports/m3_small_game_report.md`，重启后严格复核见 `reports/m3_audit_2026-07-13.md`。
