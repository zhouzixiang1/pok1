# B 路线 M0：算法来源、许可证与可证伪矩阵

冻结日期：2026-07-12（Asia/Shanghai）

路线：项目原生 CFR 蓝图 + 神经反事实叶值 + 在线搜索
实现边界：本目录中的实现为 clean-room 项目代码；未复制论文伪代码、OpenSpiel 源码或其他仓库源码。

## 1. 统一符号

- `H`：历史集合，`Z`：终局历史集合，`I`：信息集，`A(I)`：合法动作。
- `P(h)`：历史 `h` 的行动者；`u_i(z)`：终局 `z` 对玩家 `i` 的效用。
- `sigma_i(I,a)`：玩家 `i` 在 `I` 选择 `a` 的行为策略概率。
- `pi_i^sigma(h)`：玩家 `i` 对到达 `h` 的 reach contribution；`pi_-i^sigma(h)` 包含机会与其他玩家，不包含 `i`。
- `v_i^sigma(I,a)`：信息集动作的反事实价值；`r_i^t(I,a)`、`R_i^T(I,a)`：即时与累计反事实遗憾。
- `sigma_bar^T`：按自身 reach 加权的平均策略；`BR_i(sigma_-i)`：对对方策略的精确最佳响应价值。
- 本项目 `nash_conv = BR_0(sigma_1) + BR_1(sigma_0)`；两人零和 `exploitability = nash_conv / 2`。

## 2. 许可证结论

| 来源类型 | 结论 | 本路线处理 |
|---|---|---|
| 论文、会议页面 | 论文版权不等于软件许可证 | 只依据公开数学描述 clean-room 实现，引用来源，不复制表达性源码 |
| OpenSpiel `6c8edc829962967730e5ff353340df75847fa184` | Apache-2.0 | 只作接口、算法与数值交叉参考；本里程碑不导入、不 vendor、不复制源码 |
| ReBeL `7960a42750f3407ea9eb2c3333d4c2a7961f6df4` | Apache-2.0 | 仅记录相邻路线边界；公开实现不是完整扑克 ReBeL |
| DecisionHoldem `a9ea9a545c7bb24f4e657bc6d1f75af66aa1bb51` | AGPL-3.0 | 禁止复制或派生到本路线；只读论文级算法事实 |
| 本仓库根目录 | 未发现项目级 LICENSE | 本路线不自行替仓库所有者选择发布许可证；所有外部来源保持可追溯 |

## 3. 研究矩阵

### S01 — Counterfactual Regret Minimization（CFR）

- **原始来源**：Zinkevich, Johanson, Bowling, Piccione, *Regret Minimization in Games with Incomplete Information*, NIPS 2007。
  - 作者 PDF：https://martin.zinkevich.org/publications/regretpoker.pdf
  - 作者出版页：https://webdocs.cs.ualberta.ca/~bowling/publications/b2hd-07nips-regretpoker-w-tr.html
- **版本/时间**：NIPS 2007 会议版；2026-07-12 检索。
- **关键公式**：
  - `r_i^t(I,a) = v_i^{sigma^t}(I,a) - v_i^{sigma^t}(I)`；
  - `R_i^T(I,a) = sum_t r_i^t(I,a)`；
  - regret matching：正遗憾和大于零时 `sigma(I,a) ∝ max(R(I,a), 0)`，否则均匀；
  - `sigma_bar_i^T(I,a)` 按 `pi_i^{sigma^t}(I)` 加权。
- **官方代码/许可证**：论文未授予软件许可证；未发现论文绑定的作者官方 CFR 仓库。
- **本项目映射**：`blueprint/mccfr.py` 的 regret matching、平均策略分离和 M3 exact best response。
- **国赛适配**：CFR 理论要求有限、两人零和、完美回忆博弈；未来动作/手牌抽象若破坏完美回忆，理论保证不能原样继承。
- **忠实度**：`paper-faithful clean-room`。
- **可证伪门**：Kuhn 已知均衡的期望值与 exploitability 不正确；或平均策略长期不收敛。

### S02 — External-Sampling MCCFR

- **原始来源**：Lanctot, Waugh, Zinkevich, Bowling, *Monte Carlo Sampling for Regret Minimization in Extensive Games*, NIPS 2009。
  - 会议页面：https://papers.nips.cc/paper_files/paper/2009/hash/00411460f7c92d2124a67ea0f4cb5f85-Abstract.html
  - PDF：https://papers.nips.cc/paper_files/paper/2009/file/00411460f7c92d2124a67ea0f4cb5f85-Paper.pdf
- **版本/时间**：NIPS 2009 会议版；2026-07-12 检索。
- **关键算法**：更新玩家节点枚举全部动作；机会节点与其他玩家节点按当前分布采样一条分支。采样反事实价值估计在期望上等于 CFR 更新。
- **官方代码符号参考**：OpenSpiel `ExternalSamplingSolver`，见 S12；不是国赛规则真相源。
- **本项目映射**：每个 batch 对两位 traverser 分别执行 external-sampling traversal；机会/对手动作采样，traverser 动作全枚举。
- **国赛适配**：未来必须使用国赛原生状态树；不能把 ACPC 或 OpenSpiel poker 状态直接当作国赛状态。
- **忠实度**：核心 traversal 为 `paper-faithful clean-room`；冻结策略的同步 mini-batch shard 是 `functional adaptation`。
- **可证伪门**：相同冻结策略下 Monte Carlo 更新均值偏离全树 CFR；不同 shard 布局改变合并后状态；增加样本不降低误差/方差。M3 复核已用 20,000 samples/player 对 Kuhn uniform full-tree delta 做独立对拍，最大绝对误差 `0.00605`，测试门为 `0.012`。

### S03 — CFR+

- **原始来源**：Tammelin, Burch, Johanson, Bowling, *Solving Heads-Up Limit Texas Hold'em*, IJCAI 2015。
  - 页面：https://www.ijcai.org/Abstract/15/097
  - PDF：https://www.ijcai.org/Proceedings/15/Papers/097.pdf
- **版本/时间**：IJCAI 2015 会议版；2026-07-12 检索。
- **关键公式**：regret-matching+ 使用 `R^t(I,a) = max(R^{t-1}(I,a) + r^t(I,a), 0)`；输出策略通常采用延迟后的线性平均。
- **官方代码/许可证**：论文不提供可直接复用的软件许可证；OpenSpiel 后续有 Apache-2.0 的 `CFRPlusSolver` 参考实现。
- **本项目映射**：可配置 `update_rule=cfr_plus`，独立保存非负累计 regret 与线性 average-strategy sum。
- **国赛适配**：CFR+ 的全树工程结果不能直接推断 external-sampling + 抽象 HUNL 的收敛速度。
- **忠实度**：`paper-faithful clean-room` 更新式；mini-batch 应用为 `functional adaptation`。
- **可证伪门**：任何累计 regret 变成负值；Kuhn 收敛显著弱于 vanilla/linear 基线；平均延迟配置不影响记录权重。

### S04 — Linear CFR 与 Discounted CFR（DCFR）

- **原始来源**：Brown, Sandholm, *Solving Imperfect-Information Games via Discounted Regret Minimization*, AAAI 2019。
  - 页面：https://ojs.aaai.org/index.php/AAAI/article/view/4007
  - DOI：https://doi.org/10.1609/aaai.v33i01.33011829
  - arXiv：https://arxiv.org/abs/1809.04040
- **版本/时间**：AAAI 2019, 33(01), 1829–1836；arXiv v3（2019-02-21）；2026-07-12 检索。
- **关键公式**：
  - Linear CFR 先做 `R_tilde = R_{t-1} + r_t`，再令 `R_t = R_tilde * t/(t+1)`，并给本轮平均策略贡献权重 `t`；这与按轮次线性加权只差不影响 regret matching 的全局尺度；
  - DCFR 同样先累加即时 regret，再依据 **更新后** `R_tilde` 的符号，分别乘 `t^alpha/(t^alpha+1)` 或 `t^beta/(t^beta+1)`；
  - 本轮平均策略贡献权重为 `t^gamma`；论文常用 `alpha=1.5, beta=0, gamma=2`。
- **官方代码/许可证**：论文未绑定作者官方代码；OpenSpiel 的 Apache-2.0 `discounted_cfr.py` 提供后续参考，但文件明确警告其为社区贡献且未确认复现论文结果。
- **本项目映射**：`linear`、`dcfr` 两个可配置 accumulator schedule；参数写入 checkpoint 和 digest。
- **国赛适配**：对同步 batch，`t` 定义为已提交 batch 序号，不伪称与逐 trajectory 原实现逐位相同。
- **忠实度**：公式为 `paper-faithful clean-room`；batch 时间轴为 `functional adaptation`。
- **可证伪门**：正→负或负→正跨零时使用旧符号选择折扣；两轮手算 accumulator 与实现不一致；checkpoint/resume 改变 `t`；默认 DCFR 不能在小博弈产生下降的 exploitability 趋势。

### S05 — DeepStack：continual re-solving 与反事实叶值

- **原始来源**：Moravcik et al., *DeepStack: Expert-Level Artificial Intelligence in Heads-Up No-Limit Poker*, Science 2017。
  - 论文：https://poker.cs.ualberta.ca/publications/17science.pdf
  - 补充材料：https://poker.cs.ualberta.ca/publications/17science-supplementary.pdf
  - 作者页：https://poker.cs.ualberta.ca/17science.html
- **版本/时间**：Science 356(6337), 2017；2026-07-12 检索。
- **关键对象**：自己的 reach range、对手 counterfactual-value vector、公共树和 depth-limited value function；网络输入公共状态与双方范围，输出双方逐私牌反事实价值向量，而非单一 equity。
- **官方代码/资产**：作者页面未提供完整可复现 HUNL 代码、训练数据和模型的软件许可证。
- **本项目映射**：M6 之后的 range-conditioned leaf value 与按街 continual solving；M3 不实现神经叶值。
- **国赛适配**：200BB、国赛动作/发送规则、70 手跨手状态均是额外适配；不能把 DeepStack 的 action abstraction 当成国赛合法动作表。
- **忠实度**：计划为 `paper-faithful clean-room` 核心 + `functional adaptation`。
- **可证伪门**：leaf 只预测胜率/动作；未输出双方范围条件的 CFV；更深求解误差下降但完整策略 exploitability/胜率不改善。

### S06 — Libratus 与 safe/nested subgame solving

- **原始来源**：
  - Brown, Sandholm, *Libratus: The Superhuman AI for No-Limit Poker*, IJCAI 2017：https://www.ijcai.org/Proceedings/2017/772
  - Brown, Sandholm, *Safe and Nested Subgame Solving for Imperfect-Information Games*, arXiv:1705.02955：https://arxiv.org/abs/1705.02955
- **版本/时间**：IJCAI 2017；arXiv 初稿 2017-05-08；2026-07-12 检索。
- **关键公式/对象**：在 top opponent infoset 保留 blueprint counterfactual best-response value；margin `M(I)=CBV_blueprint(I)-CBV_resolved(I)`，Resolve 至少要求非负安全 margin；nested solving 在后续 off-tree 动作处重新建立子博弈。
- **官方代码/资产**：未公开完整 Libratus blueprint、实时求解器和模型；论文版权不构成源码许可证。
- **本项目映射**：M3 现提供 Kuhn `check → bet` 公共子树的 exact-terminal continuation replacement：只允许改变 resolver 的三个 rank-conditioned response，以 top opponent infoset counterfactual best-response value 和完整 exploitability 双重 fail-closed 认证。M7/M8 仍需实现 public-range safe gadget 与 off-tree 实际尺寸注入。
- **国赛适配**：国赛 `raise-to`、精确最小加注和 60 秒 deadline 必须由 NationalGameState 决定。
- **忠实度**：当前 Kuhn 枚举 resolver 为 `functional adaptation`，只称 exact small-game certificate，不称 Libratus gadget 复刻。未来实现若缺少完整 gadget 证明，仍必须标 `functional adaptation`，不能称严格 HUNL safe。
- **可证伪门**：任一 top opponent infoset margin 为负却被接受；改变子树外策略未被拒绝；完整小博弈 exploitability 上升却被接受；off-tree 只做最近邻翻译；搜索越久持续变差。

### S07 — Deep CFR

- **原始来源**：Brown, Lerer, Gross, Sandholm, *Deep Counterfactual Regret Minimization*, ICML 2019。
  - 页面：https://proceedings.mlr.press/v97/brown19b.html
  - PDF：https://proceedings.mlr.press/v97/brown19b/brown19b.pdf
- **版本/时间**：PMLR 97:793–802, 2019；2026-07-12 检索。
- **关键对象**：advantage memory、strategy memory、reservoir sampling；advantage network 近似 CFR 的累计/平均优势，另一个网络近似平均策略。
- **官方代码/许可证**：论文页未链接作者官方仓库；OpenSpiel 提供后续 Apache-2.0 实现，但不是论文作者训练资产。
- **本项目映射**：作为未来“表格蓝图是否必须神经化”的对照，不进入 M3 核心。
- **国赛适配**：Deep CFR 的 advantage approximation 不能替代本路线的反事实叶值网络；两种网络目标必须分开。
- **忠实度**：当前仅 `source-faithful` 调研；实现为 deferred。
- **可证伪门**：将 leaf-value loss 与 advantage regression 混为同一目标；reservoir/迭代权重错误；tabular 基准未验证便神经化。

### S08 — VR-MCCFR

- **原始来源**：Schmid et al., *Variance Reduction in MCCFR for Extensive Form Games Using Baselines*, AAAI 2019。
  - 页面：https://ojs.aaai.org/index.php/AAAI/article/view/4048
  - DOI：https://doi.org/10.1609/aaai.v33i01.33012157
  - arXiv：https://arxiv.org/abs/1809.03057
- **版本/时间**：AAAI 2019, 33(01), 2157–2164；2026-07-12 检索。
- **关键公式**：对 sampled action 使用 control-variate 估计：`v_hat_b(a)=b(a)+1[a sampled]/q(a) * (v_hat(child)-b(a))`，再按目标策略聚合；任意 baseline 下保持无偏，完美 baseline 时方差为零。
- **官方代码/许可证**：论文未绑定独立官方仓库；OpenSpiel 可作后续符号参考。
- **本项目映射**：M4 后作为 ES-MCCFR 的可选 variance-reduction 消融；M3 保留无 baseline 的清晰基线。
- **国赛适配**：baseline 可来自 blueprint/leaf network，但必须记录 sampling policy `q` 并验证无偏。
- **忠实度**：当前 `source-faithful` 调研；实现 deferred。
- **可证伪门**：零 baseline 不还原 MCCFR；估计出现系统偏差；方差报告未按同一冻结策略和共同随机数比较。

### S09 — OX-Search

- **原始来源**：Ge et al., *Safe and Robust Subgame Exploitation in Imperfect Information Games*, ICML 2024。
  - 页面：https://proceedings.mlr.press/v235/ge24b.html
  - PDF：https://raw.githubusercontent.com/mlresearch/v235/main/assets/ge24b/ge24b.pdf
- **版本/时间**：PMLR 235:15255–15270, 2024；2026-07-12 检索。
- **关键对象**：adaptation safety、inaccurate-opponent-model robustness、Opponent eXploitation Search；安全比较对象是“不做对手适配的在线搜索策略”，不是假设一个完美不可利用的 blueprint。
- **官方代码/许可证**：PMLR 主页面未链接作者官方代码；无软件许可证证据，禁止从非官方复现静默复制。
- **本项目映射**：M9 的置信度门控 safe exploitation 设计参考；必须保留无 posterior 的在线搜索基线。
- **国赛适配**：只使用当前 70 手内合法观测，禁止身份、时延、外部数据库和未授权 pondering。
- **忠实度**：计划为 `paper-faithful clean-room` 或在缺细节处明确 `functional adaptation`。
- **可证伪门**：heldout/nemesis 上适配策略比同计算预算非适配搜索更可利用；模型失配时没有回退。

### S10 — Bayes' Bluff：扑克对手后验

- **原始来源**：Southey et al., *Bayes' Bluff: Opponent Modelling in Poker*, UAI 2005。
  - 作者页：https://webdocs.cs.ualberta.ca/~bowling/publications/b2hd-05uai.html
  - PDF：https://webdocs.cs.ualberta.ca/~mbowling/papers/05uai.pdf
- **版本/时间**：UAI 2005, 550–558；2026-07-12 检索。
- **关键公式**：`p(beta | O) ∝ p(O | beta) p(beta)`；将游戏随机性、隐藏牌与对手策略参数的不确定性分离。
- **官方代码/许可证**：未提供可复用作者官方代码或训练资产许可证。
- **本项目映射**：M9 的平滑行动似然、showdown 回溯、未摊牌删失证据和变化检测。
- **国赛适配**：单场仅 70 手且只有 showdown 暴露 `oppo_hands`；必须输出后验校准和置信区间，不能把 fold 当完整标签。
- **忠实度**：基础后验为 `paper-faithful clean-room`，删失/变化检测为 `inspired extension`。
- **可证伪门**：后验在合成已知对手上不校准；使用比赛当时不可见底牌；少量样本令 exploit 门控立即饱和。

### S11 — Robust/safe opponent exploitation

- **原始来源**：
  - Johanson, Zinkevich, Bowling, *Computing Robust Counter-Strategies*, NIPS 2007：https://papers.nips.cc/paper_files/paper/2007/file/6e7b33fdea3adc80ebd648fffb665bb8-Paper.pdf
  - Johanson, Bowling, *Data Biased Robust Counter Strategies*, AISTATS 2009：https://proceedings.mlr.press/v5/johanson09a.html
  - Ganzfried, Sandholm, *Safe Opponent Exploitation*, ACM TEAC 2015：https://www.cs.cmu.edu/~sandholm/www/safeExploitation.teac15.pdf
- **版本/时间**：NIPS 2007/AISTATS 2009/TEAC 2015；2026-07-12 检索。
- **关键对象**：Restricted Nash Response 用参数 `p` 在固定对手模型与不受限对手之间构造修改博弈；DBR 用观测数据偏置但保留稳健性；safe exploitation 要求最坏情况收益不低于指定安全基准。
- **官方代码/许可证**：未发现与这些论文绑定且许可明确的作者代码。
- **本项目映射**：posterior confidence 决定从安全策略向 best response 的混合上限；异常立即回退。
- **国赛适配**：安全基准必须在相同国赛动作、相同时间预算和同一完整 70 手目标下比较。
- **忠实度**：RNR 基础可 `paper-faithful clean-room`；70 手 match-win 风险门控为 `inspired extension`。
- **可证伪门**：增加 exploit 权重没有单调的安全损失边界；强动态对手下低于冻结安全基准；只报告训练对手收益。

### S12 — OpenSpiel 代码参考

- **官方仓库**：https://github.com/google-deepmind/open_spiel
- **冻结 commit**：`6c8edc829962967730e5ff353340df75847fa184`（2026-07-12 通过 `git ls-remote ... HEAD` 获取）。
- **许可证**：Apache-2.0；固定版本 LICENSE：https://raw.githubusercontent.com/google-deepmind/open_spiel/6c8edc829962967730e5ff353340df75847fa184/LICENSE
- **核对符号**：
  - `open_spiel/python/algorithms/external_sampling_mccfr.py::ExternalSamplingSolver`
  - `open_spiel/python/algorithms/cfr.py::CFRSolver` / `CFRPlusSolver`
  - `open_spiel/python/algorithms/discounted_cfr.py::DCFRSolver` / `LCFRSolver`
  - `open_spiel/python/algorithms/best_response.py::BestResponsePolicy`
  - `open_spiel/python/algorithms/exploitability.py::nash_conv` / `exploitability`
  - `open_spiel/games/kuhn_poker`、`open_spiel/games/leduc_poker`
- **本项目映射**：求解与运行时保持 stdlib-only 且没有 OpenSpiel import。隔离审计工具使用官方 wheel `1.6.15`（wheel SHA-256 见 `manifests/m3_audit_dependencies.json`），对拍树规模、均匀策略，以及确定性非均匀 Leduc 策略的 value/NashConv/exploitability。
- **国赛适配**：OpenSpiel 不是国赛规则 oracle；未来必须与 `sever/engine/validator.py` 和官方边界文档差分。
- **忠实度**：`source-faithful` 调研，项目实现 `clean-room`。
- **可证伪门**：与 OpenSpiel 对拍必须固定同一规则参数；结果不一致不得解释成“国赛差异”而跳过定位。当前七项对拍全部通过，但这不把 OpenSpiel 提升为国赛规则 oracle。

### S13 — ReBeL 相邻边界

- **原始来源**：Brown et al., *Combining Deep Reinforcement Learning and Search for Imperfect-Information Games*, NeurIPS 2020。
  - 论文：https://papers.nips.cc/paper_files/paper/2020/hash/c61f571dbd2fb949d3fe5ae1608dd48b-Abstract.html
  - 官方仓库：https://github.com/facebookresearch/rebel/tree/7960a42750f3407ea9eb2c3333d4c2a7961f6df4
- **冻结 commit/许可证**：`7960a42750f3407ea9eb2c3333d4c2a7961f6df4`，Apache-2.0。
- **可用性**：公开仓库主要提供 Liar's Dice 示例，不含完整 HUNL 代码和扑克模型。
- **本项目映射**：仅用于区分 B 路线；B 不以 ReBeL PBS 自博弈闭环冒充自己的蓝图训练。
- **忠实度**：`source-faithful` 调研；无 B 路线实现。
- **可证伪门**：B 与 A1 共享训练后策略/网络或声称复刻完整扑克 ReBeL。

### S14 — DecisionHoldem 相邻许可证边界

- **原始来源**：Li et al., *DecisionHoldem: Safe Depth-Limited Solving with Opponent Ranges*, arXiv:2201.11580：https://arxiv.org/abs/2201.11580
- **官方仓库**：https://github.com/AI-Decision/DecisionHoldem/tree/a9ea9a545c7bb24f4e657bc6d1f75af66aa1bb51
- **冻结 commit/许可证**：`a9ea9a545c7bb24f4e657bc6d1f75af66aa1bb51`，AGPL-3.0。
- **可用性**：关键 blueprint/聚类资产与完整实时搜索细节不齐全。
- **本项目映射**：只依据论文事实作方法对照；本路线不得复制、翻译或派生该仓库代码。
- **忠实度**：`source-faithful` 调研；无源码使用。
- **可证伪门**：出现无法由本路线设计记录解释、但与 AGPL 仓库结构/表达高度一致的代码。

## 4. 本阶段实现决策

1. M3 只实现 Kuhn/Leduc、小型博弈 exact best response 与 external-sampling MCCFR；不创建 HUNL 蓝图、模型或大资产。
2. regret 与 average strategy 物理分离；平均策略是交付策略，当前 regret-matching 策略只用于下一 batch 采样。
3. deterministic shard 使用“同一冻结策略 + 按 sample id 派生 RNG + canonical merge”的同步 batch；它是明确记录的工程适配。
4. `linear`、`cfr_plus`、`dcfr` 的公式时间轴、参数和 batch 序号进入 checkpoint digest。
5. M3 的 exact exploitability 是后续 NationalGameState、safe solve、leaf value 和在线搜索的不可绕过正确性门。

## 5. M0 阶段门

- **通过**：上述来源、版本、许可证、公式、本项目映射、国赛差异和 falsifier 已冻结。
- **未解决但不阻塞 M3**：DeepStack/Libratus/OX-Search 没有完整许可明确的官方 HUNL 资产；未来均须 clean-room。
- **硬边界**：DecisionHoldem AGPL 源码不得进入 B；OpenSpiel 只能作为 Apache 参考且不是国赛 oracle。
