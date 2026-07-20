# 里程碑 4 组件接口契约

> 本文件是协议适配层 / Docker Runner / TCP 桥三者的接口契约。
> 三者强耦合,实现时严格遵循此契约。

## 数据流总览

```
GameEngine(用 Card 对象跑规则)
   │ 每个 decision point 调 adapter.build_request(player_idx) → str
   ▼
DockerRunner.send(bot_idx, request_str) → 把 request 喂给容器 stdin
   │ 读容器 stdout 一行 → response_str
   ▼
adapter.parse_response(response_str) → (action_type, amount)
   │ 回到 GameEngine._betting_round
```

## 一、BotZoneJsonAdapter(engine/protocol_adapter.py)

### 卡牌编码映射
平台内部用 `Card(suit 0-3=♠♥♦♣, rank 0-12=2-A)`。
bot JSON 协议用整数 0-51:`card = rank * 4 + JUDGE_SUIT`,`JUDGE_SUIT = TCP_TO_JUDGE_SUIT[suit] = {0:2, 1:0, 2:1, 3:3}`。
即平台 Card → JSON 整数:`rank*4 + {0:2,1:0,2:1,3:3}[suit]`。

### 请求格式(每次累积发送完整历史)
平台每需要 bot 决策一次,组装一个 JSON 行发给 bot:
```json
{"requests": [<req_hand1_turn1>, <req_hand1_turn2>, ..., <req_current>]}
```
每个 req 元素字段:
- `my_id`: int (0 或 1,当前 bot 的座位)
- `dealer_id`: int (本手 dealer = SB)
- `my_cards`: [int, int] (本 bot 手牌,整数编码)
- `public_cards`: [int, ...] (当前公共牌,整数编码;preflop=[],flop=3,turn=4,river=5)
- `history`: [{round:int, player_id:int, action:int, action_type:str}, ...] (本手到当前为止所有动作;**不含**当前待决策的动作)
- `hand`: int (当前手数,从1)
- `max_hand`: int (总手数 70)
- `my_chips`: int (本 bot 当前剩余筹码)
- `opponent_chips`: int (对手剩余筹码)
- `small_blind`: int (50)
- `big_blind`: int (100)

action_type 取值:"fold"/"call"/"check"/"raise"/"allin";action 字段:fold=-1,call/check=0,raise=加注到的总额,allin=-2。

### 响应解析
bot 返回 `{"response": int}`:
- -2 = allin
- -1 = fold
- 0 = call 或 check(由平台 game_state 判断,有 to_call 则 call,否则 check)
- >0 = raise 到该总额(raise-to-total)

`parse_response(response_str, game_state) → (action_type: str, amount: int|None)`:
- response=-2 → ("allin", None)
- response=-1 → ("fold", None)
- response=0 → game_state 有 to_call 时 ("call", None),无 ("check", None)
- response>0 → ("raise", response)

### Adapter 接口
```python
class ProtocolAdapter:
    # 状态更新(引擎每个事件后调)
    def on_hand_start(self, hand_num, sb_idx, bb_idx): ...
    def on_hole_cards(self, player_idx, cards: list[Card]): ...
    def on_community(self, cards: list[Card]): ...  # 累积公共牌
    def on_action(self, player_idx, action_type, amount, round_idx): ...
    def on_settle(self, earnings): ...
    # 构造请求/解析响应
    def build_request(self, player_idx, my_chips, opp_chips) -> str: ...  # 返回 JSON 行
    def parse_response(self, response_str, game_state) -> tuple[str, int|None]: ...
```

**TextProtocolAdapter**:包装现有 protocol.py,build_request 返回文本(preflop|.../转发对手动作),parse_response 解析文本动作。**这个适配器主要给 TCP 通道复用,新平台用 JsonAdapter。**

## 二、Docker Runner(runtime/docker_runner.py)

### 容器生命周期
- **预热**(可选):启动时为常用 bot 镜像预起空闲容器池。
- **对局开始**:`start_session(image, protocol) → session_id`,起容器(stdin/stdout pipe)。
- **通信**:`send(session_id, request_str) → response_str`,写 stdin 读 stdout 一行,60s 超时。
- **结束**:`stop_session(session_id)`,docker stop + rm。

### 容器启动
- JSON bot:`docker run -i --rm --network=none --memory=512m --cpus=0.5 <image>`,stdin/stdout 直连。
- TCP bot:镜像 entrypoint 已是 tcp_bridge.py,平台仍用 stdin/stdout 与桥通信;桥在容器内自起 socket server + bot 子进程。

### 资源限制
`--network=none`(隔离网络)、`--memory=512m`、`--cpus=0.5`、决策超时 60s(超时判 fold)。

### 接口
```python
class DockerRunner:
    async def start_session(self, image: str, name_hint: str) -> str: ...
    async def send(self, session_id: str, request: str, timeout: float = 60) -> str: ...
    async def stop_session(self, session_id: str): ...
    async def cleanup_all(self): ...
```

## 三、TCP 桥(runtime/tcp_bridge.py,容器内运行)

### 角色
容器内进程,连接「平台(stdin/stdout JSON)」与「用户 TCP bot(socket 国赛文本)」。
用户 bot 代码零改动,连 `127.0.0.1:50101`(容器内回环)。

### 流程
1. 桥启动 → 监听 `127.0.0.1:50101`。
2. fork/spawn 用户 bot(`--host 127.0.0.1 --port 50101 --name $BOT_NAME`)。
3. bot 连入桥 → 桥收 name(裸队名)。
4. 循环:
   - 读平台 stdin 一行(JSON request)→ 翻译成国赛文本序列(preflop|/flop|/.../转发对手动作)→ 发给 bot socket。
   - 读 bot socket 一个动作(token 前缀分帧,复用 transport.py 的 pop_client_action 逻辑)→ 包成 `{"response": int}` → 写平台 stdout。

### 卡牌/动作翻译(双向)
- 平台 JSON 整数卡牌 → 国赛 `<suit,rank>` 文本:`JUDGE_SUIT_TO_TCP = {2:0,0:1,1:2,3:3}` 反向映射,`suit=JUDGE_SUIT_TO_TCP[card%4], rank=card//4`。
- 平台 JSON 动作历史 → 国赛文本:preflop 发 `preflop|{SMALL|BIG}BLIND|<cards>`;公共牌发 `flop|/turn|/river|`;对手动作转发 `call/check/fold/allin/raise <n>`。
- bot 文本响应 → JSON int:`fold`→-1, `call`/`check`→0, `allin`→-2, `raise N`→N。

### 命令行
```
python tcp_bridge.py --bot-entry national_bot.py [--bot-name NAME] [--listen-host 127.0.0.1] [--listen-port 50101]
```
环境变量优先:`BOT_NAME` > `--bot-name` > "Bot"。
