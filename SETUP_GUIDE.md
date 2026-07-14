# Setup Guide — National TCP Policy

## 1. Environment

```bash
cd /home/zzx/project/pok
python3 --version
git fetch --tags origin
python3 -m venv .venv
source .venv/bin/activate
pip install -r web/requirements.txt -r sever/requirements.txt
pip install pytest 'claude-agent-sdk==0.2.91'
```

There is no root `requirements.txt`; using that retired command leaves the Web
and Agent SDK runtime only partially installed. The SDK version above matches
the currently tested streaming/signature workarounds in `web/core/`.

Frontend dependencies are installed by the normal first `web/main.py` launch,
or manually with:

```bash
cd web/frontend
npm ci
npm run build
```

## 2. Choose the correct checkout

Use `/home/zzx/project/pok` for code, prompts, tests, and docs. The autonomous
service runs from `/home/zzx/project/pok/.evolution_pok`. Never copy files
between the two; synchronize through `origin/main` at a safe point.

## 3. Verify the national platform

```bash
python -m pytest sever/tests -q

# Terminal 1
cd sever && python main.py

# Optional diagnostic clients in two other terminals
cd sever && python test_client.py 127.0.0.1 10001 BotA
cd sever && python test_client.py 127.0.0.1 10001 BotB
```

The platform listens on TCP `10001` and serves its diagnostic dashboard on
`18080` by default.

## 4. Run the evolution web app

```bash
python web/main.py
python web/main.py --view-only
python web/main.py --no-daemon
python web/main.py --no-build   # only after static assets already exist
```

Standalone commands:

```bash
python web/core/orchestrator.py --one-gen
python web/core/orchestrator.py --dry-run
python web/core/elo_daemon.py --once
```

## 5. Test the control plane

```bash
cd web
python -m pytest tests -q

cd frontend
npm run build
```

## 6. Candidate contract

An active candidate is `bots/national_v<N>/` with the current
`national_tcp_policy_v1` manifest. Candidate decisions and any pure helper
functions live inside the single `policy.py` artifact. Separate candidate
helper modules are forbidden. Policies return typed `pass`, `fold`, `allin`, or `raise`
intent; they never send TCP, return integer actions, or reconstruct another
protocol history.

The system runtime owns packet splitting, authoritative state, implicit
street-close completion, terminal/showdown tracking, fallback, deadline,
legality, action throttle, and the socket.

The reset preserves `national-bot-v142` only as the numeric high-water and
targets `national_v143` first. Do not select or repair an untagged higher
directory such as old-wrapper `national_v155`; the runtime reset archives it as
stale unpublished debris.

## 7. Diagnostic Arena

```bash
python scripts/national_arena.py serve --view-only
python scripts/national_arena.py run --mode managed \
  --top-bot national_v<N> --bottom-bot national_v<M> --hands 70 --wait
```

Arena output is diagnostic only and never updates ratings or satisfies a gate.

## 8. Official Windows EXE checks

```bash
python scripts/official_platform_acceptance.py \
  --candidate bots/national_v<N> --opponent bots/national_v<M> \
  --self-play-rounds 1 --opponent-rounds 1 --target-hands 70

python scripts/official_certify.py doctor
python scripts/official_certify.py full bots/national_v<N> --wait-if-busy
```

Every published bot needs a content-bound signed full certificate. The
only qualifying normal profile is `official-full-v5`: five 70-hand self-play
rounds plus three 70-hand opponent rounds. The v143-only operator bootstrap
uses the current system-owned `first_strict_control_v1`; it is a one-time
empty-pool ceremony, not a normal selection fallback. Arena, smoke, compliance,
and EXE chip outcomes cannot replace the full certificate or become strength
evidence.

## 9. Claude Agent SDK operator probe

After synchronizing the runtime checkout and before restarting autonomous
evolution, run the production-path SDK probe once:

```bash
python scripts/claude_sdk_operator_probe.py --timeout-seconds 300 --pretty
```

This is a billed Claude Agent SDK call, not a `curl` health check. It requires
three separate `Read` calls and two separate `Bash` calls, verifies the exact
pinned SHA-256 values of both official oracle documents, and inspects
`sever/server/transport.py`. The Bash hook accepts only the two commands printed
in the receipt; network commands and shell variants are denied. MCP is disabled,
no write-capable SDK tools are exposed, the temporary role log is outside the
repository, and stdout is one machine-readable receipt. Exit status is zero only
when every tool has a non-error result, the hashes match, delimiter-free send
behavior is proven locally, and the model returns the matching evidence. Timeout,
provider availability, missing tools, or bad evidence fail closed.

Do not run this command in unit tests. The regression suite uses a mocked SDK
wrapper and never starts a paid request:

```bash
cd web && python -m pytest tests/test_operator_sdk_probe.py -q
```

## 10. Archived history

Retired facilities live under `archive/`. Do not install them, import them,
place them on `PYTHONPATH`, run their tests as active gates, or reuse their
ratings/experience. The archive is for historical inspection only.

## 11. Important references

- `AGENTS.md`
- `docs/national-tcp-policy-epoch.md`
- `docs/national-runtime-architecture-policy.md`
- `docs/evolution-dual-checkout-sync-policy.md`
- `docs/official-certification-policy.md`
- `docs/official-raise-boundary-oracle-2026-07-11.md`
- `docs/official-terminal-settlement-oracle-2026-07-11.md`
