#!/usr/bin/env python3
"""CLI for the FastAPI-backed national Web Arena."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


class ArenaAPI:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload=None, *, timeout: float = 30.0):
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Arena-Token"] = self.token
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Arena API HTTP {exc.code}: {detail[:500]}") from exc
        except URLError as exc:
            raise RuntimeError(f"Arena API unavailable: {exc.reason}") from exc
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def download(self, path: str, output: Path, *, timeout: float = 120.0) -> None:
        headers = {"X-Arena-Token": self.token} if self.token else {}
        request = Request(self.base_url + path, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                output.write_bytes(response.read())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Arena API HTTP {exc.code}: {detail[:500]}") from exc


def _client(args) -> ArenaAPI:
    return ArenaAPI(args.api, args.token or os.environ.get("POK_ARENA_CONTROL_TOKEN", ""))


def _create_payload(args) -> dict:
    mode = "managed_bots" if args.mode == "managed" else "external_tcp"
    if mode == "managed_bots" and (not args.top_bot or not args.bottom_bot):
        raise RuntimeError("managed mode requires --top-bot and --bottom-bot")
    return {
        "mode": mode,
        "host": args.host,
        "port": (
            args.port
            if args.port is not None
            else 0
            if mode == "managed_bots"
            else 10001
        ),
        "hands": args.hands,
        "action_timeout_seconds": args.timeout,
        "official_action_delay": args.official_action_delay,
        "capacity_wait_seconds": args.capacity_timeout,
        "managed_port_override": bool(
            mode == "managed_bots" and args.port is not None
        ),
        "top_bot": args.top_bot,
        "bottom_bot": args.bottom_bot,
    }


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def command_create(args) -> int:
    _print(_client(args).request("POST", "/api/national-arena/sessions", _create_payload(args)))
    return 0


def command_start(args) -> int:
    _print(_client(args).request(
        "POST", f"/api/national-arena/sessions/{args.session_id}/start"
    ))
    return 0


def command_run(args) -> int:
    client = _client(args)
    session = client.request("POST", "/api/national-arena/sessions", _create_payload(args))
    session = client.request(
        "POST", f"/api/national-arena/sessions/{session['session_id']}/start"
    )
    _print(session)
    if not args.wait:
        return 0
    session_id = session["session_id"]
    last_hand = -1
    while True:
        state = client.request("GET", f"/api/national-arena/sessions/{session_id}")
        if int(state.get("hands_completed", 0)) != last_hand:
            last_hand = int(state.get("hands_completed", 0))
            print(
                f"[{state['status']}] hands={last_hand}/{state['hands_total']} "
                f"chips={state['top_total_earnings']}:{state['bottom_total_earnings']}",
                flush=True,
            )
        if state["status"] in {"finished", "failed", "stopped", "quarantined"}:
            _print(state)
            return 0 if state["status"] == "finished" else 2
        time.sleep(max(0.2, args.poll_interval))


def command_status(args) -> int:
    _print(_client(args).request(
        "GET", f"/api/national-arena/sessions/{args.session_id}"
    ))
    return 0


def command_list(args) -> int:
    _print(_client(args).request("GET", "/api/national-arena/sessions"))
    return 0


def command_events(args) -> int:
    _print(_client(args).request(
        "GET",
        f"/api/national-arena/sessions/{args.session_id}/events/history"
        f"?after_event_id={args.after}&limit={args.limit}",
    ))
    return 0


def command_wire(args) -> int:
    _print(_client(args).request(
        "GET",
        f"/api/national-arena/sessions/{args.session_id}/wire/history"
        f"?after_sequence={args.after}&limit={args.limit}",
    ))
    return 0


def command_stop(args) -> int:
    _print(_client(args).request(
        "POST", f"/api/national-arena/sessions/{args.session_id}/stop"
    ))
    return 0


def command_export(args) -> int:
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    _client(args).download(
        f"/api/national-arena/sessions/{args.session_id}/thp",
        output,
    )
    print(output)
    return 0


def command_cleanup_orphans(args) -> int:
    from national_arena.manager import NationalArenaManager
    from national_arena.storage import ArenaStore, DEFAULT_ARENA_ROOT

    async def cleanup():
        manager = NationalArenaManager(ArenaStore(args.store or DEFAULT_ARENA_ROOT))
        await manager.startup()
        recovered = [
            row for row in manager.list_sessions()
            if row.get("failure_reason") == "web_process_restarted"
            or row.get("status") == "quarantined"
        ]
        await manager.shutdown()
        return recovered

    _print({"recovered_sessions": asyncio.run(cleanup())})
    return 0


def command_serve(args) -> int:
    command = [sys.executable, str(ROOT / "web" / "main.py"), "--port", str(args.port)]
    if args.view_only:
        command.append("--view-only")
    if args.no_build:
        command.append("--no-build")
    return subprocess.call(command, cwd=str(ROOT))


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="")


def _add_match_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=("external", "managed"), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Defaults to an ephemeral port for managed mode and 10001 for external mode.",
    )
    parser.add_argument("--hands", type=int, default=70)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--official-action-delay", type=float, default=0.30)
    parser.add_argument("--capacity-timeout", type=float, default=30.0)
    parser.add_argument("--top-bot")
    parser.add_argument("--bottom-bot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="National Web Arena CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--view-only", action="store_true")
    serve.add_argument("--no-build", action="store_true")
    serve.set_defaults(handler=command_serve)

    for name, handler in (("create", command_create), ("run", command_run)):
        command = subparsers.add_parser(name)
        _add_connection_args(command)
        _add_match_args(command)
        if name == "run":
            command.add_argument("--wait", action="store_true")
            command.add_argument("--poll-interval", type=float, default=1.0)
        command.set_defaults(handler=handler)

    for name, handler in (
        ("start", command_start),
        ("status", command_status),
        ("stop", command_stop),
    ):
        command = subparsers.add_parser(name)
        _add_connection_args(command)
        command.add_argument("session_id")
        command.set_defaults(handler=handler)

    listing = subparsers.add_parser("list")
    _add_connection_args(listing)
    listing.set_defaults(handler=command_list)

    for name, handler in (("events", command_events), ("wire", command_wire)):
        command = subparsers.add_parser(name)
        _add_connection_args(command)
        command.add_argument("session_id")
        command.add_argument("--after", type=int, default=0)
        command.add_argument("--limit", type=int, default=1000)
        command.set_defaults(handler=handler)

    export = subparsers.add_parser("export-thp")
    _add_connection_args(export)
    export.add_argument("session_id")
    export.add_argument("--output", required=True)
    export.set_defaults(handler=command_export)

    cleanup = subparsers.add_parser("cleanup-orphans")
    cleanup.add_argument("--store", type=Path)
    cleanup.set_defaults(handler=command_cleanup_orphans)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
