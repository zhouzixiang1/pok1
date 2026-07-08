# national_v70 国赛平台协议排查报告

日期：2026-07-07

## 结论

已用官方 Windows 平台实际跑通 `D:\University\code\pok\national_v70` 双方自对弈。

本次最终验证轮次为 `work\v70_clean13`：

- 平台：`D:\University\code\pok\国赛平台\国赛平台\德州扑克对弈平台限时一分钟2021版\德州扑克对弈平台限时一分钟2021版.exe`
- 双方程序：`D:\University\code\pok\national_v70\national_bot.py`
- 启动参数：`BotA --seat upper`，`BotB --seat lower`
- 连接方式：平台只填 `127.0.0.1`，上方/下方玩家名由程序通过 `name` 握手发送
- 结果：连续推进到约 61 局，未出现非法动作、异常、断连或协议级 60 秒卡死
- 日志统计：`issues=0`，`earnChips` 双方合计 120 条
- 最长间隔：23 秒，原因是 BotA 第 26 局 preflop 策略计算耗时 `22.310s`，不是平台协议卡死

本次定位到的核心问题不是单一“连不上”，而是 v70 原先同时存在通信入口、TCP 分包、平台动作语义、以及官方平台状态机兼容性四类问题。当前版本已经做了协议适配，可以在官方平台跑起来。

## 官方协议依据

对照 `D:\University\code\pok\国赛平台\国赛平台` 下文档和标准示例 `D:\University\code\pok\national_v70\untitled0-1.py`，平台通信有几个关键点：

- 平台作为 socket server，bot 作为 client。
- bot 首先等待平台发送 `name`，然后回传自己的玩家名。
- 平台消息是短字符串 socket 消息，不是按换行分隔的文本协议。
- bot 发送动作时使用原始字符串，如 `raise 200`、`call`、`fold`、`check`、`allin`。
- 标准示例使用 `sock.recv(...)` 和 `sock.send(sendBuf.encode())`，不依赖 `\n`。
- 文档要求 bot 发送加注动作用 `raise`，不要发送 `bet`；但平台入站消息里可能出现 `bet N`，需要兼容解析。

## 排查过程

### 1. 连接阶段

最开始 v70 连不上，是因为原实现更像换行文本协议：

- 使用 `makefile(...).readline()` 等待换行。
- 发送名字和动作时追加 `\n`。
- 官方平台和 `untitled0-1.py` 都是原始 socket 短消息，不保证结尾有换行。

修复后，平台配置框中只填 `127.0.0.1`，启动两个 bot 后，平台能正确显示：

- 桌面上方玩家：`BotA`
- 桌面下方玩家：`BotB`

之后点击“确定”即可开局。

### 2. TCP 粘包阶段

v3 能连接但会出现非法 `check`，主要风险来自 TCP 没有消息边界。真实日志和模拟中都能出现类似粘包：

- `raise 302call`
- `checkturn|<0,2>`
- `raise 638checkcallcheckraise 570`
- `earnChips -100preflop|BIGBLIND|...`

如果只做一次 `recv().strip()`，状态机会把多个平台消息当成一条消息处理，后续阶段、下注量和行动权都会错位，最终容易发出非法 `check` 或非法 `call`。

当前 v70 已增加短消息拆分器，按平台协议 token 拆成连续消息再逐条处理。

### 3. 行动权和 check/call 阶段

测试确认，postflop 先行动者按盲注关系推断，不是固定“下方玩家先行动”。

修复后的原则：

- preflop 大盲面对小盲补齐时，不再发无意义 `check/call`，而是按平台兼容路径处理。
- 面对对手 `check` 时，若平台期待响应，使用平台可接受的 `call` 闭合这一轮，而不是盲发 `check`。
- 策略返回 0 时，不直接等价为 `check`；必须结合当前 `my_stage_bet`、`opponent_stage_bet` 和阶段判断。

### 4. 官方平台状态机兼容阶段

官方平台对某些理论合法动作链处理不稳定，尤其是多次加注链。实测发现：

- postflop 首动作直接 `check` 在部分街道会触发非法或卡死。
- postflop `raise 100 -> call` 在部分中间街会导致下一街推进异常。
- 更稳定的闭合方式是 `raise 100 -> raise 200 -> call`。
- 但如果继续允许 `raise 100 -> raise 200 -> raise 400 -> call`，下一街可能出现平台不转发后续动作，等待约 60 秒后才结算。

最终兼容策略：

- postflop 首行动不发 `check`，统一用小额 `raise 100`。
- postflop 首次面对对手下注时，用 `raise 200` 触发平台正常进入闭合流程。
- 如果本街自己已经投入过筹码，再面对对手加注时，强制转为 `call`，避免三次加注链。
- postflop 面对下注时的策略性 `fold` 也会被协议层转换成平台更稳定的 `raise/call` 路径。

## 当前已修改文件

`D:\University\code\pok\national_v70\national_bot.py`

关键改动：

- 原始 socket `recv(4096)`，不再使用 `readline()`。
- 发送 `name` 和动作时不追加换行。
- 新增 `--log`，记录收包、拆包、决策、发包。
- 新增 `--seat {auto,upper,lower}`，用于明确桌面位置提示。
- 支持平台入站 `bet N`，内部按 `raise` 处理。
- 修正平台 `raise N` 的筹码语义和本地状态同步。
- 新增 TCP 粘包拆分。
- 修正 `check/call/fold/raise` 的合法化适配。
- 限制 postflop 三次加注链，避免官方平台 60 秒等待。

语法检查通过：

```powershell
python -m py_compile D:\University\code\pok\national_v70\national_bot.py
```

## 最终复测步骤

平台启动必须使用 exe 所在目录作为工作目录，否则可能缺资源或触发 Visual C++ 运行库问题：

```powershell
$exe='D:\University\code\pok\国赛平台\国赛平台\德州扑克对弈平台限时一分钟2021版\德州扑克对弈平台限时一分钟2021版.exe'
$wd=Split-Path $exe
Start-Process -FilePath $exe -WorkingDirectory $wd
```

平台 UI 操作：

1. 点击齿轮。
2. 上方玩家、下方玩家不用填。
3. 对弈平台 IP 填 `127.0.0.1`。
4. 点击“开始连接”。
5. 启动两个 bot。
6. 等平台自动显示 `BotA` 和 `BotB` 后，点击“确定”。

bot 启动命令：

```powershell
cd D:\University\code\pok\national_v70
python national_bot.py --name BotA --seat upper --host 127.0.0.1 --log C:\Users\32143\Documents\Codex\2026-07-07\computer-use\work\v70_clean13\botA.log
```

```powershell
cd D:\University\code\pok\national_v70
python national_bot.py --name BotB --seat lower --host 127.0.0.1 --log C:\Users\32143\Documents\Codex\2026-07-07\computer-use\work\v70_clean13\botB.log
```

## clean13 验证摘录

最终统计：

```text
botA.log max_hand=61 sends=110 earn_lines=120 issues=0 max_gap=23s max_decision=22.31s
botB.log max_hand=61 sends=96  earn_lines=120 issues=0 max_gap=23s max_decision=4.031s
```

最长间隔来源：

```text
[13:55:10] DECIDE start name=BotA hand=26 stage=preflop ...
[13:55:33] DECIDE done action=-1 elapsed=22.310s
```

这说明当时是 BotA 策略计算慢，平台并没有断联，也不是协议未转发。动作随后正常发送并继续推进。

正常 postflop 闭合样例：

```text
BotB river first action: raise 100
BotA responds:          raise 200
BotB closes:            call
earnChips follows
```

三次加注链风险已规避：

```text
old risky path: raise 100 -> raise 200 -> raise 400 -> call
new stable path: raise 100 -> raise 200 -> call
```

## 后续演进过程建议

### v71：协议稳定版

目标：把当前协议适配固化，保证 70 局完整跑完。

建议：

- 固定保存 `botA.log`、`botB.log` 和平台右侧牌局序列截图。
- 每次只改协议层一个点，避免策略和协议问题混在一起。
- 验收标准是官方平台完整 70 局，无非法动作、无 60 秒协议卡死、无异常退出。

### v72：回放测试器

目标：把平台 transcript 变成离线可复现测试。

建议：

- 从日志提取 `RECV raw`、`DISPATCH line`、`SEND msg`。
- 建 fake-platform 回放器，覆盖 `name/preflop/flop/turn/river/raise/call/check/fold/allin/earnChips/oppo_hands`。
- 把粘包样例固定成单元测试，防止后续改策略时破坏协议层。

### v73：下注语义校准

目标：进一步校准 `raise N`、本街下注量、总 pot、all-in runout。

建议：

- 对照平台显示筹码和 bot 内部 `my_stage_bet/opponent_stage_bet/pot`。
- 对 all-in 后连续发牌、摊牌、`oppo_hands` 做单独 transcript。
- 确保策略看到的 `to_call` 与真实待跟注一致。

### v74：策略恢复评估

目标：协议稳定后再评估 v70 策略本身。

建议：

- v70 自对弈多轮，每轮 70 局。
- v70 对 `untitled0-1.py` 多轮。
- 分开统计净胜筹码、非法动作、超时、all-in 频率、平均决策时间。
- 协议修改和策略修改分开提交。

### v75+：策略进化

目标：在稳定协议上做策略增强。

建议顺序：

- 先做 transcript 回放和离线指标，不直接上复杂训练。
- 固定合法下注尺度集合，再逐步扩展下注尺度。
- 把下注意愿和下注大小分开优化。
- 对慢决策增加时间预算保护，避免再次接近一分钟平台限制。

## 当前判断

`national_v70` 当前已经从“连不上/非法 check”推进到“可在官方平台连续运行”的状态。下一步重点不应再盲目改策略，而应先把这套协议适配固化成 v71，并补上回放测试，避免后续策略迭代重新引入协议错误。
