# 引擎内核 PROVENANCE

本目录的引擎内核代码 **copy 自 pok1 仓库 `origin/main`**(不通过 import 引用),经 import 适配后归位到 arena 包结构。

## 上游来源

- **上游仓库**: `git@github.com:zhouzixiang1/pok1.git`
- **上游 commit**: `a334b9ff4d7ee00b26eb66a607e845640fd0e7cf`
- **commit 日期**: 2026-07-11 20:26:19 +0800
- **commit 主题**: Merge frozen H2H combined analysis fix
- **copy 日期**: 2026-07-11

## 上游路径 → arena 归位

| 上游路径 (origin/main) | arena 归位 | 行数 |
|---|---|---|
| `sever/engine/deck.py` | `engine/deck.py` | 74 |
| `sever/engine/evaluator.py` | `engine/evaluator.py` | 105 |
| `sever/engine/game.py` | `engine/game.py` | 602 |
| `sever/engine/validator.py` | `engine/validator.py` | 146 |
| `sever/engine/thp_recorder.py` | `engine/thp_recorder.py` | 238 |
| `sever/engine/__init__.py` | `engine/__init__.py` | 0 (空) |
| `sever/server/protocol.py` | `engine/protocol.py` | 92 |

> `protocol.py` 在上游属于 `sever/server/` 包(TCP 行格式层);arena 把它归位到 `engine/` 同包,以便 `game.py` 用 `from .protocol import ...` 直接引用。

## 提取命令

```bash
git -C ~/project/pok archive origin/main sever/engine sever/server/protocol.py | tar -x -C /tmp/arena-eng
cp /tmp/arena-eng/sever/engine/*.py /tmp/arena-eng/sever/server/protocol.py arena/backend/engine/
```

## import 适配(copy 后改动,仅改 import/包结构,不改协议语义)

`game.py` 与 `protocol.py` 上游为 try/except 双分支(try=相对 import 供 `sever` 包内用,except=绝对 import 供 `cd sever && python main.py` standalone 兜底)。arena 布局下 except 分支解析不到,已删除;try 分支保留并修正归位:

- **game.py**: 删除 `except ImportError` 整块;`from ..server.protocol import (...)` → `from .protocol import (...)`(protocol.py 已归位同包);其余 4 条相对 import 不变。
- **protocol.py**: 删除 `except ImportError` 块;`from ..engine.deck import (...)` → `from .deck import (...)`(deck.py 同包)。
- 其余 4 文件(deck/evaluator/validator/thp_recorder)零内部跨包依赖,无改动。

## 顺手清理(待定,记于此备查)

- 上游 `game.py` 顶部 `format_opponent_action` 可能是死 import(arena 待 reachability 核实后再删,避免误伤)。

## 硬约束

arena 的 `pyproject.toml` / venv / 启动脚本 / `PYTHONPATH` **绝不可**把 `~/project/pok/` 或 pok1 任何工作树加入 `sys.path`。pok1 顶层另有 `engine/` 包(无 `deck.py`),一旦入 path 会 `ModuleNotFoundError` 或静默串包。**pok1 仅作 copy 源,不是 import 源。**

## re-sync 上游

未来若上游有引擎 bugfix,先 `git -C ~/project/pok fetch origin` 取最新 `origin/main`,re-copy 后**更新本文件的 commit-hash**,并单独成一个 commit(消息如 `chore(engine): re-sync upstream@<short>`)。
