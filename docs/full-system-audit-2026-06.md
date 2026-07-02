# 全项目合规审计报告

> 审计日期：2026-06-08
> 审计范围：3个游戏引擎 + 14个LLM提示词 + bot_adapter桥接层 + 5份国赛平台规范文档
> 审计方法：5阶段并行workflow（spec提取 → 引擎审计 → 提示词审计 → 桥接审计 → 综合）

## 一、规范文档完整性分析

本审计基于国赛平台5份规范文档（通信协议.docx、非法行为说明.docx、补充说明.docx、德州扑克规则.doc）进行规则提取与完整性评估。

### 1.1 明确且无歧义的规则

以下规则在文档中表述清晰，无解读分歧：

- **比赛格式**：每场70局，每位玩家20000筹码，一局一复位，小盲50/大盲100
- **卡牌编码**：`<suit,rank>` 格式，suit 0-3 对应黑桃/红桃/方块/梅花，rank 0-12 对应 2-A
- **TCP通信协议**：平台为服务端（端口10001），引擎为客户端，先交换 name，后逐局对战
- **行动顺序**：Preflop 小盲先表态，Flop/Turn/River 大盲先表态
- **角色交替**：两个玩家交替担任小盲注和大盲注
- **非法行为处理**：所有非法行为一律按弃牌（fold）处理
- **胜负判定**：按70局总输赢筹码量判定胜负，相同为平局
- **程序出错**：比赛中程序出错判负
- **超时**：每步决策限时60秒，超时按弃牌处理
- **raise 语义**：`raise X` 表示加注**到** X 个筹码（raise-to-total），非增量
- **earnChips 格式**：正数为赢，负数为输，空格分隔
- **oppo_hands**：仅亮牌（showdown）时发送对手手牌
- **13条非法行为规则**（详见下文）

### 1.2 存在歧义的规则

共发现10处文档歧义或缺失：

| 编号 | 歧义点 | 涉及文档 | 影响 |
|------|--------|----------|------|
| A1 | "下注筹码量不得小于200"中的"下注筹码量"是 raise-to-total 还是增量？非法行为说明用"下注筹码量"措辞，补充说明明确为 raise-to-total。两处措辞不一致 | 非法行为说明 + 补充说明 | 低（补充说明已澄清） |
| A2 | Preflop BB 在 SB call 后 raise，raise-to >= 200 是否指总阶段下注？根据补充说明示例，确认为 raise-to-total | 非法行为说明 | 低（已明确） |
| A3 | **"一倍以上"是 >= 2x 还是 > 2x？** 补充说明示例用 raise 400 -> raise 801，暗示 >= 2x（含边界） | 非法行为说明 + 补充说明 | **中**（边界值影响验证逻辑） |
| A4 | call 行为的阶段转换时机。补充说明说"call 行为则进入下一阶段"，但 preflop SB call 后 BB 仍需表态 | 补充说明 | 低（标准规则可推断） |
| A5 | 通信协议中 `bet X` 格式的用途。平台发送 `bet X` 给引擎展示对手行为，但引擎不能发送 bet。协议不对称 | 通信协议 | 低（已理解） |
| A6 | **allin vs raise 边界：下注量等于筹码量时必须 allin，但"下注量"是总阶段下注还是增量？** | 非法行为说明 | **中**（影响临界情况判定） |
| A7 | 第一局谁是小盲？文档未明确说明，需依赖标准 heads-up 规则（庄家/小盲先行动） | 补充说明 + 德州扑克规则 | 低（标准规则） |
| A8 | Postflop 首 raise 最小值 100 是否有具体示例？补充说明仅展示 raise 200 的情况 | 非法行为说明 + 补充说明 | 低（规则本身明确） |
| A9 | **allin 精确匹配 call 的冲突：Rule 11（allin）vs Rule 12（allin 后只能 call/fold）。若对手 allin 5000，己方恰好 5000 筹码，应发 call 还是 allin？** | 非法行为说明 | **中**（两个规则冲突） |
| A10 | "第一个行为"是指该玩家在该阶段的首个行为，还是该阶段的首个行为？上下文暗示前者 | 非法行为说明 | 低（上下文可推断） |

**总结**：13条核心规则中有3条（A3、A6、A9）存在中等影响歧义，其余为低影响。补充说明对 raise-to-total 语义的澄清是关键的消歧依据。

---

## 二、引擎合规状态

### 2.1 合规汇总表

| 引擎 | 位置 | 测试规则数 | 通过 | 失败 | 合规率 |
|------|------|-----------|------|------|--------|
| root engine | engine/judge.py | 18 | 17 | 1 | **94.4%** |
| sever engine | sever/ | 21 | 20 | 1 | **95.2%** ⚠️ |
| web engine | web/core/engine/judge.py | — | — | — | 与 root 同源 |

### 2.2 Root Engine (engine/judge.py) 详细结果

| 规则 | 描述 | 结果 |
|------|------|------|
| A: 卡牌格式 | int 0-51, suit=card%4 | PASS |
| B: 70局/20000筹码/50:100盲注 | 常量定义正确 | PASS |
| B: Preflop SB先, Postflop BB先 | dealer=SB acts first | PASS |
| B: 玩家交替SB/BB | dealer_idx 随 hand 交替 | PASS |
| C: Raise-to-total 语义 | bet>0 = raise-to-total | PASS |
| C: Preflop 首 raise >= 200 | *2=200 | PASS |
| C: Postflop 首 raise >= 100 | *2=100 | PASS |
| C: Re-raise >= 2x | 正确 | PASS |
| D.1-D.13: 13条非法行为规则 | 全部正确实现 | PASS |
| **Postflop pass after first check** | **按国赛 TCP 协议，postflop 首个玩家 check 后，第二个玩家必须用 `call` 过街；第二个 `check` 是非法 wire action** | **✅ 当前结论覆盖旧审计** |

### 2.3 Sever Engine (sever/) 详细结果

> ⚠️ **2026-07-02 规范更正**：本节原审计把 postflop 第二个 `check` 当作合法 `check-check`，这是错误的。以 `sever/国赛平台/` 文档和当前实现为准：TCP wire protocol 区分 `check` 与 `call`，postflop 首个玩家 `check` 后，第二个玩家如要过街必须发送 `call`；第二个 `check` 属于非法行为并按 fold 处理。Botzone/local 整数动作 `0` 仍可表示 call/check，但在该场景应映射/记录为 `call`，不是要求 TCP 接受第二个 `check`。

validator.py 对 postflop 非首个 `check` 的拒绝是当前正确行为：
- BB 先 `check`（is_first_in_stage=True，合法）
- SB 如要过街应发送 `call`
- SB 再发送 `check` 应被拒绝并按非法行动处理

当前应以 `docs/national-platform-alignment-report.md`、`sever/engine/validator.py` 和 `sever/tests/test_national_alignment.py` 为准；本历史审计中的 check-check 修复建议已废弃。

### 2.4 跨引擎差异分析

| 差异点 | Root Engine | Sever Engine | 严重度 |
|--------|-------------|--------------|--------|
| Postflop pass after first check | Botzone int `0` 需按上下文解释为 call/check | TCP 第二个过街动作用 `call`，第二个 `check` 非法 | **当前已对齐** |
| 最小 raise 验证逻辑 | 单一检查 raise_to < last_raise_to * 2 | 分离的首 raise 和 re-raise 检查 | 低（结果等价） |
| 卡牌编码 | int 0-51, suit=card%4 | (suit,rank) 元组 | 信息（bot_adapter 正确转换） |

---

## 三、LLM 提示词审计

共审计14个提示词文件，发现7个问题（0个严重，3个中等，4个轻微）。

### 3.1 逐提示词审计结果

#### master_prompt.md

| 类别 | 发现 | 严重度 |
|------|------|--------|
| 🟡 游戏规则缺失 | Master 编写的 worker prompt 涉及 raise 逻辑、底池赔率计算、preflop/postflop 决策，但自身不包含任何游戏规则参考。当 Master 写出 `return choose_raise(pot_size, my_chips, strength, 0.55, round_raise)` 等具体代码指令时，仅依赖训练知识而非项目权威规则 | 中等 |
| ✅ 内容指导 | GOOD/BAD 示例、worker_guidance 角色边界表、双轨边界示例均设计良好 | 无问题 |

#### worker_prompt.md

| 类别 | 发现 | 严重度 |
|------|------|--------|
| ✅ 游戏规则正确 | 验证节包含完整正确的规则块：`Action encoding: 0=call/check, -1=fold, -2=all-in, >0=raise-to-total (加注到的阶段总额). Game rules: dealer=SB, postflop BB acts first, 70 hands/match, 20000 starting chips, 50/100 blinds.` | 无问题 |
| 🟢 协议描述不完整 | 仅说明基本 JSON 协议，未描述完整 schema（requests/responses/data 字段） | 轻微 |
| ✅ 角色边界 | 定义清晰，有执行规则、示例和 CRITICAL ENFORCEMENT 节 | 无问题 |

#### reviewer_prompt.md

| 类别 | 发现 | 严重度 |
|------|------|--------|
| 🟡 游戏规则缺失 | Reviewer 检查"代码正确性"包括 bot 是否输出有效 JSON，但无游戏规则参考。无法验证 raise 值使用 raise-to-total 还是 raise-by-increment 语义，可能批准错误代码 | 中等 |

#### critic_prompt.md

| 类别 | 发现 | 严重度 |
|------|------|--------|
| 🟢 游戏规则缺失 | Critic 评估策略质量涉及 equity/pot-odds/fold-equity，但无规则参考 | 轻微 |
| ✅ 分析清单 | 回归检查项"No regression: AA/KK/QQ still raises preflop"设计良好 | 无问题 |

#### orchestrator.md

| 类别 | 发现 | 严重度 |
|------|------|--------|
| ✅ 管道描述 | 阶段表、门控要求、重试规则完整正确 | 无问题 |

#### crossover_prompt.md

| 类别 | 发现 | 严重度 |
|------|------|--------|
| ✅ 游戏规则正确 | 包含正确的规则块 | 无问题 |
| 🟢 文件输出不明确 | 仅说"Write the full Python code"但未指定哪些文件 | 轻微 |

#### initial_prompt.md

| 类别 | 发现 | 严重度 |
|------|------|--------|
| ✅ 游戏规则正确 | Action encoding 正确 | 无问题 |
| 🟡 游戏参数缺失 | 从零创建 bot 但未提及：70局/场、20000起始筹码、盲注50/100、庄家=SB、postflop BB先行动 | 中等 |

#### 其他提示词

direction_auditor_prompt.md、combined_analyst.md、archivist.md、match_analyst.md、performance_analyst.md、stagnation_analyzer.md、experience_consolidator.md — 均不涉及游戏规则或代码语义，无问题。

### 3.2 跨提示词系统性问题

| 问题 | 涉及提示词 | 严重度 |
|------|-----------|--------|
| 游戏规则仅在 2/9 个管道提示词（worker_prompt, crossover_prompt）中正确描述 | master, reviewer, critic, 等 | 中等 |
| JSON 协议格式描述不完整 | initial_prompt, worker_prompt, crossover_prompt | 轻微 |
| Reviewer 无法验证行动语义（验证盲区） | reviewer_prompt + worker_prompt | 轻微 |

---

## 四、Bot 适配器桥接审计

bot_adapter.py 共执行37项检查：**32项通过，5项失败**。

### 4.1 行动转换

| 检查项 | 结果 |
|--------|------|
| judge.py -1 → TCP 'fold' | ✅ PASS |
| judge.py -2 → TCP 'allin' | ✅ PASS |
| judge.py >0 → TCP 'raise {value}' | ✅ PASS |
| judge.py 0 → TCP 'call'/'check' (上下文判断) | ✅ PASS |
| 非整数 response 处理 | 🔴 FAIL（无类型保护，TypeError 崩溃） |

### 4.2 卡牌转换

| 检查项 | 结果 |
|--------|------|
| TCP suit → judge suit 映射 | ✅ PASS |
| 正向转换 tcp_card_to_int | ✅ PASS（52张卡逐一验证） |
| 反向转换 int_to_tcp_card_str | ✅ PASS |

### 4.3 筹码追踪

| 检查项 | 结果 |
|--------|------|
| 盲注扣除 SB=-50, BB=-100 | ✅ PASS |
| 每局筹码重置 | ✅ PASS |
| Raise 筹码追踪 | ✅ PASS |
| Allin 筹码追踪 | ✅ PASS |
| **Preflop SB call 筹码扣除** | **🟡 FAIL：少扣 50 筹码** |

### 4.4 响应性（行动顺序）

全部13项检查 ✅ 通过。

### 4.5 威胁模型

| 威胁 | 结果 |
|------|------|
| Bot 返回无效 JSON | ✅ PASS |
| Bot 进程崩溃 | ✅ PASS |
| 网络断开 | ✅ PASS |
| **Bot 返回非整数 response** | **🔴 FAIL：TypeError 崩溃** |
| **Bot 挂起/无超时** | **🔴 FAIL：readline() 无超时保护** |

---

## 五、发现的问题清单

### 🔴 严重（会导致比赛出错）

| 编号 | 组件 | 问题 |
|------|------|------|
| **S1（已废弃）** | engine/judge.py + web/core/engine/judge.py + sever/engine/validator.py | **旧结论错误**：postflop 第二个 TCP `check` 不是合法动作；正确过街动作为 `call`。当前实现应保持 validator 拒绝第二个 `check`，并确保 adapter/local JSON `0` 在该上下文映射为 `call`。 |

### 🟡 中等（可能导致策略错误）

| 编号 | 组件 | 问题 |
|------|------|------|
| **M1** | bot_adapter.py | **Preflop SB call 筹码少扣 50**：diff=0 未扣除，bot 看到 my_chips=19950 而非 19900 |
| **M2** | master_prompt.md | **缺少游戏规则参考**：Master 写涉及 raise 逻辑的 worker prompt 时无权威规则依据 |
| **M3** | reviewer_prompt.md | **缺少行动语义验证**：Reviewer 无法发现 raise-by-increment 错误代码 |
| **M4** | initial_prompt.md | **缺少游戏参数**：从零创建 bot 缺少关键参数 |
| **M5** | bot_adapter.py | **非整数 response 导致崩溃**：_convert_action 无 int() 类型保护 |
| **M6** | bot_adapter.py | **readline() 无超时**：Bot 挂起时 adapter 永久阻塞 |
| **M7** | 规范文档歧义 | **"一倍以上"边界不明确**：>= 2x 还是 > 2x？（两引擎均实现为 >= 2x） |

### 🟢 轻微（文档/代码风格问题）

| 编号 | 组件 | 问题 |
|------|------|------|
| L1 | critic_prompt.md | 缺少游戏规则参考（影响较小） |
| L2 | crossover_prompt.md | 未指定需输出哪些文件 |
| L3 | worker_prompt.md | JSON 协议格式描述不完整 |
| L4 | initial_prompt.md | 协议示例省略 data 字段和 responses 数组 |
| L5 | 规范文档 | allin vs raise 边界"下注的量"定义模糊 |
| L6 | 规范文档 | Rule 11 vs Rule 12 在精确匹配场景冲突 |

---

## 六、修复建议

### S1: Postflop 第二个 `check` 旧审计结论已废弃

**当前要求**:
- 国赛 TCP：postflop 首个玩家可 `check`；第二个玩家若也不下注，必须发送 `call` 过街。
- 国赛 TCP：第二个玩家发送 `check` 是非法格式/非法行为，应按 fold 处理。
- Botzone/local：整数动作 `0` 仍按上下文解释为 call/check；在“对手已 check、自己过街”场景中应记录为 call 语义。
- adapter、decision tests、prompt 都应禁止假设 TCP `check-check` 合法。

**不得再执行的旧修复**:
- 不要放宽 `sever/engine/validator.py` 去接受第二个 postflop `check`。
- 不要把 TCP wire 行为改回 `check-check` 过街。
- 若发现 bot 输出 JSON `0`，应由 adapter 按上下文转为 TCP `call`。

### M1: Preflop SB call 筹码少扣 50 (bot_adapter.py)

**修复**：在 `_update_chips('call')` 中为 preflop SB 首次 call 增加特殊处理，opp_bet 应为 100（BB 盲注）而非默认的自身 stage_bet。

### M2-M4: 提示词增加游戏规则块

在 master_prompt.md 和 reviewer_prompt.md 增加简短规则参考块（与 worker_prompt.md 中已有的规则块一致），消除验证盲区。initial_prompt.md 增加游戏参数节。

### M5: _convert_action 类型保护

增加 `try: action_int = int(action)` 类型转换，无效输入按 fold 处理。

### M6: send_and_recv 增加超时

参考 engine/battle.py 的 _PersistentBot，为 readline() 增加 60s 超时保护。

### M7: 规范文档歧义

在 CLAUDE.md 中明确记录 "一倍以上" 解读为 >= 2x，与补充说明示例一致。

---

## 七、系统整体评估

| 模块 | 合规率 | 说明 |
|------|--------|------|
| Sever 引擎 (TCP 服务器) | 以当前国赛对齐测试为准 | validator 拒绝第二个 postflop `check` 是正确行为 |
| Root 引擎 (engine/judge.py) | **94.4%** (17/18) | 1个严重 bug |
| Bot 适配器 (bot_adapter.py) | **86.5%** (32/37) | 1个筹码 bug + 2个健壮性 |
| LLM 提示词 (14个文件) | 0严重 / 3中等 | 核心提示词规则正确 |
| 规范文档 | **80%** 明确 | 3条有中等影响歧义 |

### 风险评估

- **已废弃风险**：旧 S1（postflop check-check 误判）不再成立；真正风险是提示词或 adapter 再次假设 TCP 第二个 `check` 合法。
- **中等风险**：M1（SB call 筹码少扣 50）每局 SB 首次 call 均触发，偏差约 0.25%
- **系统性风险**：M2+M3 组合形成验证盲区——Worker 有正确规则，但 Reviewer 无法发现语义错误
