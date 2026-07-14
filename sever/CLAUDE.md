# National TCP Platform

`sever/` is the sole active poker protocol/runtime platform. It implements the
national competition documents under `国赛平台/`.

## Run and test

```bash
cd sever
python main.py
python main.py --tcp-port 20001 --web-port 28080

cd ..
python -m pytest sever/tests -q
```

Two clients start a match automatically. The default TCP port is `10001`; the
diagnostic dashboard defaults to `18080`.

## Wire rules

- TCP is a byte stream. Official messages contain no `\n`/`\r\n`; never append
  one or treat recv boundaries as message boundaries.
- Client tokens are exactly `raise <amount>`, `fold`, `call`, `check`, or
  `allin`; `bet` is not a wire action.
- `raise <amount>` uses exactly one space and means raise to the total street
  contribution.
- Exact `raise 400` after `raise 200` is official-EXE legal.
- Postflop opening `call` is illegal. After any first action, `check` is
  illegal; a second player closes a checked street with `call`.
- Each decision has a 60 second official limit. The formal bot runtime keeps
  an official-safe send delay and sends one action only while a decision is
  pending.
- After a called all-in there are no further decisions before settlement.
- Cards are `<suit,rank>` with suits Spade=0, Heart=1, Diamond=2, Club=3 and
  ranks 2=0 through Ace=12.

The local server mirrors the official EXE by omitting a peer street-closing
call/check. A new street or settlement boundary proves the unique closure;
clients must apply it before resetting street contributions. Terminal
fold/call and showdown cards must reach the opponent tracker.

At natural hand 70 both the local server and official EXE omit the last
`earnChips` pair while retaining the authoritative internal/THP result.
Certification uses wire settlements 1..69 plus strict THP state 69/footer
proof.

Authoritative controlled observations:

- `../docs/official-raise-boundary-oracle-2026-07-11.md`
- `../docs/official-terminal-settlement-oracle-2026-07-11.md`

## Ownership

- `engine/game.py` — hand/match state machine.
- `engine/validator.py` — national action legality.
- `engine/evaluator.py` and `engine/deck.py` — cards and showdown.
- `engine/thp_recorder.py` — GB2312 national THP output.
- `server/protocol.py` — wire encoding/decoding.
- `server/tcp_server.py` — asyncio match server.
- `web/` — diagnostic FastAPI/SSE surface.

Retired adapters and alternate protocol engines live under `archive/` and must
not be imported or restored as active compatibility paths.
