# Route B M4：真实 HUNL Blueprint Native Vertical Slice

日期：2026-07-14

## 结论边界

M4 交付的是“CFR blueprint + native TCP”纵向切片，用来证明真实国赛状态、训练、稀疏策略工件和 70 手 TCP 运行链可以内容绑定并确定性复现。它不是神经叶值、在线安全重求解、完整 ReBeL/DecisionHoldem，也不是正式 EXE 认证或强度结论。

本地 TCP 中记录的净筹码只用于检查零和与语义投影，验收权重固定为 0。当前 manifest 的状态必须保持 `diagnostic_only_not_officially_certified`。

## 算法与实现取舍

External-sampling MCCFR 依据 Lanctot 等人的 Monte Carlo CFR 工作；两人 SIMPLE average 更新在对手节点累计当前策略。OpenSpiel 的 external-sampling MCCFR 实现被用作 M3 语义交叉检查，但不是交付依赖，也没有复制其 solver 源码。

- Marc Lanctot et al., *Monte Carlo Sampling for Regret Minimization in Extensive Games*, NeurIPS 2009: https://papers.nips.cc/paper_files/paper/2009/hash/00411460f7c92d2124a67ea0f4cb5f85-Abstract.html
- OpenSpiel MCCFR implementation/reference: https://github.com/google-deepmind/open_spiel/tree/master/open_spiel/algorithms

本实现采用同步 batch：每个 shard 从同一冻结 regret table 采样，随后按 `(traverser, sample_id)` canonical merge。这保证 1-shard/N-shard 与恢复路径逐字节一致，但不声称与“每条 trajectory 后立即更新”的串行 MCCFR 位级相同。

chance RNG 与 policy RNG 分域。一个 batch 内两位 traverser 使用同一 chance stream；不同 batch 直接绑定不同 `batch_index`，没有跨 batch 配对，也没有为了制造非均匀行而重访上一批物理牌。

## 信息集与动作抽象

Preflop registry 覆盖 169 类并验证完整 1,326 组合映射。Postflop equity 输入只含本人手牌与公共牌，在移除已知牌后使用确定性采样；花色置换先规范化，因此同构牌面产生相同结果。信息还包括 hand category、board texture、SPR 与有序公共 action label。

M4 的 exact key 是 perfect-recall v2。只保留当前 postflop bucket 会把“此前看到不同 private/public abstraction、现在落入相同 bucket”的历史合并；修复后 exact key 额外包含每个过去本人决策点的抽象观测与本人 action-id。固定旧碰撞回归和固定可达样本 partition audit 要求：同一个 current exact key 只能对应一个 recall signature。

`inferred_from_boundary` 表示官方传输是否省略了已被边界证明的 closing action。它保留在 Common 审计 history，但不是扑克可观察信息；显式/推断版本必须得到相同 exact/backoff key 和相同策略选择。

动作候选完全由 Common `LegalActionSet` 与 `validate_action` 得出。Raise 采用真实 street raise-to total；min、half-pot、pot、1.5-pot 和 all-in 在 Common Action 层去重。任何 candidate 自行推断的“合法”动作都不能绕过 Common。

## 工件与运行时

Blueprint 只保存有正 average-strategy mass 的 exact row。每个 backoff row 先聚合原始 mass，再归一化一次；不得聚合 regret，也不得平均已归一化的行。运行时依次尝试 exact、四层 backoff，最后才 uniform emergency；命中与 material influence 分开计数。

压缩格式绑定 magic、codec、压缩/解压长度、两个 SHA-256、训练 contract、solver state 和 compiler source。载入限制最大展开尺寸，拒绝 duplicate JSON key、NaN/Infinity/overflow float、尾随压缩流、symlink 和规则/后端漂移。

正式 run contract 包含完整 pinned config、target、CLI hash、Route/Common 双遍稳定源树快照及其明确排除列表。`runtime_outputs/` 和最终生成输出被排除以避免自引用；这些输出随后由 M4 manifest 单独逐字节绑定。失效运行永久注册表不在排除列表中，因此删改 registry 会改变源摘要并使既有 run contract 失配。

## Crash、TOCTOU 与恢复

所有源文件读取使用 `O_NOFOLLOW` regular fd，并在读前后比较 dev/inode/mode/nlink/size/mtime/ctime。完整源树使用 fd-relative 遍历，文件 hash 与全部目录/文件 identity map 做两遍完全比较，拒绝顺序枚举形成的旧 A + 新 B 混合快照。

原子写保留从 `/` 到目标 parent 的 ancestry identity chain；publish 前后重新走原请求路径，拒绝 parent rename 后同名替换导致写入失联旧目录。目标 absent/present、inode 交换、symlink、原位改写和 source tree 增删都有固定回归。

训练每完成一个完整 batch 才事务合并、原子 checkpoint 和 heartbeat。取消、KeyboardInterrupt 或崩溃最多丢失未提交 batch。Resume 必须匹配完整 run contract；已经到 target 时保留现有 checkpoint digest，不为了“完成”重写。

Selector 的 durable frontier 只由 checkpoint 与逐事件 no-clobber 原子文件
组成。每个 event 重复绑定 run/source/config/checkpoint identity，并通过
previous SHA 形成连续链；heartbeat 只能投影链中某个已验证事件，不能声明
更前 batch。若 save 已完成而 event/heartbeat 未完成，resume 以最新 event
tip 为旧前沿，稳定载入更新的 checkpoint 后写入
`batch_checkpoint_recovered`。若主机在 event 临时 inode 写入时退出，残留
tmp 原样保留并由下一条 resume event 记录其 SHA。JSONL 只是从权威事件
前缀原子重建的审计视图，损坏/落后都不能推进恢复前沿。

正式发布不把 JSONL 当作证据根。它先验证并复制 `events/*.json` 的完整
连续文件树，绑定每个文件 SHA、树摘要、链尖 sequence/event SHA 和 durable
batch/checkpoint；然后要求 JSONL 与该权威链的 canonical bytes 完全相等。
运行时 heartbeat 可以安全落后，但 published `completed` heartbeat 必须
精确引用最终链尖及其 durable pair，否则发布和后续验证都失败。

失效采用双重 no-clobber 权威：workspace 的任何同名 `INVALIDATED.json`
目录项（普通文件、目录、悬空 symlink 等）立即阻断；合法 marker 的完整副本
同时登记到 `manifests/invalidated_selector_runs/`。因此删除 runtime marker
不会恢复资格。selector resume、formal train、freeze/export/publish 均
fail-closed，发布在读取 selector 前后与写 manifest 前重复检查；M4 manifest
还记录全 registry snapshot/hash，并拒绝其中出现的 selector checkpoint。
Invalidation 与 publish/render/write/verify 使用同一个 rooted、no-follow、
inode-bound `flock`，并在单进程内可重入；因此 marker/registry 与最终发布
具有明确线性化边界，而不是仅依赖“最后一次检查”的竞态窗口。

覆盖已有 M4 发布前，publisher 对全部 scalar outputs 和 flat authoritative
event directory 做稳定双遍备份。事件目录的两轮 names、`fstatat`、内容 hash、
文件 identity 与目录 metadata 都在同一个 held fd 上完成，空目录、嵌套目录、
symlink 与 A/B rename/ABA 均拒绝。任何后续异常都会恢复旧 payload 集合，
删除本次新增 event 文件，并在最后恢复旧 manifest；恢复后再次比较每个
scalar byte、event manifest 和 event byte。

早先的 `m4-selector-discovery-formal-7582f569` 缺少现行 heartbeat/event-log
契约，已以 `missing_heartbeat_log_contract` 永久失效。它只保留作审计，不得
冻结、恢复或发布；后续发现必须从新的冻结源码摘要和全新 workspace 开始。

## Training-only selector

Config 固定候选 `2/4/8/16/32/64`、唯一 metric、阈值和 first-pass 规则。绑定源码摘要 `cd74ebb4e5f9700cc14a3c192e9044079273040efaf17aa31190a41c6965bf41` 的 batch-0 发现，以及随后不写 workspace 的独立确定性重放，都得到严格相同的五行前缀：`2/4/8/16` 的 material row 为 0，`32` 首次得到 7 行（exact 2、backoff 5，最大 L1 `1.6666666666666667`），terminal solver SHA 为 `b5b063fce594de37231d9dc8c116409156fec6e974765048f2996110c6fe8a52`。Config 因此冻结为 `frozen_first_pass`、target 32，并保存所有失败候选与首过候选的完整统计和 solver SHA；发现模式现被拒绝。正式 selector 仍须从 0 重跑逐行完全相等，不能从较晚 checkpoint 跳过失败候选。

正式 blueprint 训练使用新的 frozen run contract 再从 0 开始。到 selected target 后，它必须重算同一 observation，并与冻结 trace 最后一行（包括 solver digest、row counts、material counts、L1、trajectory/node/info-row counters）完全相同，之后才允许写 artifact。

## Native 证据

两位 Route-B client 连接真实本地 `sever` TCP server，使用 Common 固定 70 手 deck commitment。每位 client 单线程拥有 decoder、session、pending decision lease 和 socket send；没有 unsolicited rescue action。Local 模式显式 LF/delay 0，official 模式 raw/no delimiter，默认 0.30 秒。

两次运行的 semantic projection 必须完全相等，并绑定：artifact/payload/solver、七个 `sever` 后端文件、deck algorithm/window、policy seeds、70 hands、全部 action 与 side count、illegal/timeout、双方 exact/backoff/uniform/material counters 及 earnings。双方都必须有 material nonuniform decision，且 illegal/timeout/process failure 为零。

官方自然 EOF 的 70 手 terminal + 69 个 wire settlement 只产生 `requires_thp_state_69`；它是干净连接结束形状，不是完成证明或 certification。外部 THP state 69/footer 与 signed official full certificate 仍是独立必需条件。

## 后续工作

M5 之前仍需单独设计并验证：可提交的 system-owned equity/blueprint packager、神经 counterfactual leaf value、public-range gadget/safe resolving、deadline-aware search budget、对手 posterior，以及与 Route A 在同一 native 70-hand outcome contract 下的无偏对比。当前 M4 不用这些未来模块掩盖 blueprint-only 的真实边界。
