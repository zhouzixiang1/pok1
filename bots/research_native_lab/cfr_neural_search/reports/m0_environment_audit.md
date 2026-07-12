# B 路线 M0：本机与工作树审计

采集时间：2026-07-12（Asia/Shanghai）。本文件是本次现场探测，不沿用旧快照。

## Git 边界

- 工作树：`/home/zzx/project/pok/.codex_worktrees/cfr-neural-search`
- 分支：`codex/research-cfr-neural-search`
- base / `origin/main`：`6ee160c93cee8d0afdad111c4c82bc6ddb6012ca`
- 初始工作树：干净。
- 所有写入限制在 `bots/research_native_lab/cfr_neural_search/`。
- 不修改 `.evolution_pok`、`common_contracts`、`comparison`、正式 `national_v*` 或外层 checkout。

## 现场硬件

- CPU：13th Gen Intel Core i9-13900HX。
- 拓扑：1 socket，24 物理核，32 逻辑 CPU，单 NUMA node。
- 指令集：含 AVX2、FMA、AVX-VNNI；本阶段 stdlib Python 实现不依赖这些扩展。
- RAM：62 GiB，总可用约 55 GiB；swap 8 GiB，采集时近乎未使用。
- GPU：NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB VRAM；采集时约 7766 MiB 空闲。
- NVIDIA driver：595.71.05。
- 工作盘：1.8 TiB，总可用约 1.6 TiB。

## 软件环境

- 默认 `python`：Python 3.14.4。
- 正式系统 `/usr/bin/python3`：Python 3.12.3。
- PyTorch：2.12.0+cu132；CUDA runtime 13.2；`torch.cuda.is_available() == True`。
- NumPy：2.4.4。
- M3 小型博弈底座只使用 Python stdlib，确保可在 `/usr/bin/python3` 运行。

## 本阶段资源策略

- M3 不启动 GPU 训练、不生成 HUNL 资产。
- correctness suite 先于任何规模扩张。
- deterministic shard 先验证调度无关性；真正多进程吞吐留到状态/数据格式冻结后的后续阶段。
- 大规模阶段默认保留系统与平台资源，不依赖 swap；所有长任务必须 checkpoint/resume。

## 已知基础设施边界

- OpenSpiel 当前只作外部 reference，不作为运行依赖。
- 本仓库 `poker_assets.py` 只是 1326 组合/169 类元数据 mmap ABI 原型，不是 evaluator、equity table 或 blueprint loader。
- 国赛 exact oracle 与 TCP 状态重建由共同合同所有者负责；B 路线在本里程碑不越权修改 `common_contracts`。
- Python strategy worker 的 daemon 子进程限制将在产品化阶段通过受测的 C++ 线程池或独立 supervisor 处理；M3 不提前搭建未验证 runtime。

## M0 结论

硬件足以进行后续多核 CFR 与单 GPU 叶值训练，但当前阶段门只允许小型博弈正确性。环境无阻塞项。
