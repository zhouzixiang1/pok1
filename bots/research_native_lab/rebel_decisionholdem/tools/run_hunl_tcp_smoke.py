"""Run a deterministic 70-hand route-A2 smoke on sever GameEngine/TCP sockets."""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import importlib
import json
import os
import resource
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sever.engine.deck import Deck
from sever.engine.game import GameEngine
from sever.server.tcp_server import ClientConnection

from ..decisionholdem_like.hunl_blueprint import (
    HUNL_MATERIAL_POLICY_L1_THRESHOLD,
    HUNLBlueprint,
)
from ..decisionholdem_like.hunl_tcp_client import (
    SEVER_LINE_FRAMING,
    run_hunl_tcp_client,
)
from ..decisionholdem_like.secure_files import (
    atomic_json_write,
    canonical_bytes,
    pretty_json_bytes,
    stable_read_path,
    stable_selected_file_map,
    strict_json_loads,
)
from .train_hunl_blueprint import seed_independence_snapshot_from_roots


TCP_SMOKE_SCHEMA = "route-a2-hunl-sever-tcp-smoke-v5"
TCP_SEMANTIC_PROJECTION_SCHEMA = "route-a2-hunl-tcp-semantic-projection-v1"
INFLUENCE_GATE_CONTRACT = "route-a2-predeclared-trained-policy-influence-v1"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[2]
BACKEND_FILES = (
    "sever/engine/game.py",
    "sever/engine/deck.py",
    "sever/engine/evaluator.py",
    "sever/engine/validator.py",
    "sever/engine/thp_recorder.py",
    "sever/server/protocol.py",
    "sever/server/tcp_server.py",
)
_BACKEND_MODULE_FILES = {
    path.removesuffix(".py").replace("/", "."): REPOSITORY_ROOT / path
    for path in BACKEND_FILES
}
_CLIENT_TIMING_FIELDS = {"elapsed_sec", "max_decision_compute_ms"}
_SERVER_TIMING_FIELDS = {
    "deadline_epoch_ms",
    "decision_wait_sec",
    "timeout_budget_sec",
}
_SEMANTIC_BODY_FIELDS = (
    "action_timeout_sec",
    "backend",
    "blueprint_sha256",
    "card_encoding",
    "deck_root_seed",
    "hands_played",
    "illegal_actions",
    "influence_gate",
    "first_accept_timeout_sec",
    "local_sever_settlements_per_client",
    "match_timeout_sec",
    "official_raw_no_delimiter_framing_proved",
    "official_terminal_hand_70_proved",
    "result_authority",
    "server_action_events",
    "seed_independence",
    "timeouts",
    "transport_framing",
    "total_earnings",
)
_TIMING_BODY_FIELDS = (
    "elapsed_sec",
    "max_server_decision_wait_ms",
    "process_peak_rss_kib_at_completion",
)
_PROJECTION_INPUT_FIELDS = frozenset(
    (*_SEMANTIC_BODY_FIELDS, *_TIMING_BODY_FIELDS, "clients", "server_semantic_events")
)
_PROJECTION_BOUND_FIELDS = frozenset(
    (*_PROJECTION_INPUT_FIELDS, "semantic_projection", "semantic_projection_sha256")
)


def _canonical_bytes(value: object) -> bytes:
    return canonical_bytes(value)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _atomic_json(path: Path, payload: object) -> None:
    atomic_json_write(path, payload)


def _strip_server_timing(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_server_timing(item)
            for key, item in sorted(value.items())
            if key not in _SERVER_TIMING_FIELDS
        }
    if isinstance(value, list):
        return [_strip_server_timing(item) for item in value]
    return value


def _semantic_server_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_strip_server_timing(event) for event in events]


def build_tcp_semantic_projection(body: dict[str, object]) -> dict[str, object]:
    """Project only deterministic poker/protocol semantics, never wall-clock data."""

    fields = frozenset(body)
    if fields not in (_PROJECTION_INPUT_FIELDS, _PROJECTION_BOUND_FIELDS):
        missing = sorted(_PROJECTION_INPUT_FIELDS - fields)
        unknown = sorted(fields - _PROJECTION_BOUND_FIELDS)
        raise ValueError(
            f"TCP semantic projection body fields are invalid; "
            f"missing={missing}, unknown={unknown}"
        )
    raw_clients = body["clients"]
    if not isinstance(raw_clients, list) or any(
        not isinstance(client, dict) for client in raw_clients
    ):
        raise ValueError("TCP semantic projection clients are invalid")
    clients = [
        {
            key: value
            for key, value in sorted(client.items())
            if key not in _CLIENT_TIMING_FIELDS
        }
        for client in raw_clients
    ]
    events = body["server_semantic_events"]
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ValueError("TCP semantic server event projection is invalid")
    projection = {
        "acceptance_excludes_chip_result": True,
        "clients": clients,
        "deterministic_body": {
            field: body[field] for field in _SEMANTIC_BODY_FIELDS
        },
        "earnings_are_reproducible_diagnostic_record_only": True,
        "schema": TCP_SEMANTIC_PROJECTION_SCHEMA,
        "server_events": events,
    }
    return strict_json_loads(_canonical_bytes(projection))


def validate_tcp_semantic_projection(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "body",
        "body_sha256",
        "schema",
    }:
        raise ValueError("TCP smoke evidence wrapper fields are invalid")
    if payload["schema"] != TCP_SMOKE_SCHEMA:
        raise ValueError("TCP smoke evidence schema mismatch")
    body = payload["body"]
    if not isinstance(body, dict):
        raise ValueError("TCP smoke evidence body is invalid")
    if frozenset(body) != _PROJECTION_BOUND_FIELDS:
        raise ValueError("TCP smoke evidence body fields are not exact")
    if payload["body_sha256"] != _sha256_bytes(_canonical_bytes(body)):
        raise ValueError("TCP smoke evidence content hash mismatch")
    projection = build_tcp_semantic_projection(body)
    if (
        body.get("semantic_projection") != projection
        or body.get("semantic_projection_sha256")
        != _sha256_bytes(_canonical_bytes(projection))
    ):
        raise ValueError("TCP smoke semantic projection binding mismatch")
    return projection


def assert_frozen_tcp_semantic_replay(
    frozen: object,
    replay: object,
) -> dict[str, object]:
    """Require a real fixed-seed rerun to reproduce the frozen semantic record."""

    frozen_projection = validate_tcp_semantic_projection(frozen)
    replay_projection = validate_tcp_semantic_projection(replay)
    if replay_projection != frozen_projection:
        raise RuntimeError("fixed-seed TCP semantic replay differs from frozen evidence")
    return replay_projection


def forbidden_backend_imports() -> list[str]:
    """Reject top-level legacy engine imports without banning sever.engine."""

    failures: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                if module == "engine" or module.startswith("engine."):
                    failures.append(
                        f"{path.relative_to(PACKAGE_ROOT).as_posix()}:{node.lineno}:{module}"
                    )
    return failures


class _DeterministicSeverManager:
    """Minimal route-owned socket adapter around the unmodified sever engine."""

    def __init__(self, deck_root_seed: int, action_timeout_sec: float):
        self.deck_root_seed = deck_root_seed
        self.action_timeout_sec = action_timeout_sec
        self.clients: list[ClientConnection] = []
        self.events: list[dict[str, Any]] = []
        self.engine: GameEngine | None = None
        self.match_task: asyncio.Task[None] | None = None
        self.first_client_accepted = asyncio.Event()
        self.done = asyncio.Event()
        self.error: BaseException | None = None

    def _deck(self, hand_number: int) -> Deck:
        material = _canonical_bytes(
            ["route-a2-sever-deck-v1", self.deck_root_seed, hand_number]
        )
        return Deck(seed=int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))

    async def broadcast(self, event: dict[str, Any]) -> None:
        self.events.append(json.loads(json.dumps(event, sort_keys=True)))

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self.clients) >= 2:
            writer.close()
            await writer.wait_closed()
            return
        self.clients.append(ClientConnection(reader, writer))
        if len(self.clients) == 1:
            self.first_client_accepted.set()
        if len(self.clients) == 2 and self.match_task is None:
            self.match_task = asyncio.create_task(self._run_match())

    async def _send(self, player: int, message: str) -> None:
        if not await self.clients[player].send_line(message):
            raise ConnectionError("sever failed to send a platform token")

    async def _receive(self, player: int) -> str | None:
        return await self.clients[player].recv_line(timeout=self.action_timeout_sec)

    async def _run_match(self) -> None:
        try:
            await self._send(0, "name")
            await self._send(1, "name")
            names = [
                await self.clients[0].recv_line(timeout=10.0),
                await self.clients[1].recv_line(timeout=10.0),
            ]
            if any(name is None for name in names):
                raise RuntimeError("sever did not receive both route client names")
            self.engine = GameEngine(
                send_func=self._send,
                broadcast_func=self.broadcast,
                recorder=None,
                deck_factory=self._deck,
            )
            self.engine.action_timeout_sec = self.action_timeout_sec
            self.engine._recv_action = self._receive
            await self.engine.run_match(str(names[0]), str(names[1]))
        except BaseException as exc:
            self.error = exc
        finally:
            await asyncio.gather(
                *(client.close() for client in self.clients),
                return_exceptions=True,
            )
            self.done.set()


def _backend_snapshot() -> dict[str, object]:
    for module_name, expected in _BACKEND_MODULE_FILES.items():
        module = importlib.import_module(module_name)
        actual_value = getattr(module, "__file__", None)
        if not isinstance(actual_value, str):
            raise ValueError(f"sever backend module has no origin: {module_name}")
        actual = Path(actual_value)
        if (
            actual != expected
            or actual.is_symlink()
            or actual.resolve(strict=True) != expected
        ):
            raise ValueError(
                f"sever backend provenance mismatch for {module_name}: {actual}"
            )
    files = stable_selected_file_map(REPOSITORY_ROOT, BACKEND_FILES)
    return {
        "files": files,
        "tree_sha256": _sha256_bytes(_canonical_bytes(files)),
    }


def validate_backend_snapshot(snapshot: object) -> dict[str, object]:
    """Fail closed on omission or byte drift of any transitive sever backend file."""

    current = _backend_snapshot()
    if snapshot != current:
        raise ValueError("sever backend snapshot differs from the loaded runtime")
    return current


async def run_sever_tcp_smoke(
    blueprint: HUNLBlueprint,
    *,
    deck_root_seed: int,
    client_policy_seeds: tuple[int, int] | list[int],
    action_timeout_sec: float = 5.0,
    first_accept_timeout_sec: float = 10.0,
    match_timeout_sec: float = 120.0,
) -> dict[str, object]:
    started = time.perf_counter()
    if type(deck_root_seed) is not int or deck_root_seed < 0:
        raise ValueError("deck_root_seed must be a nonnegative integer")
    if type(action_timeout_sec) not in (int, float) or action_timeout_sec <= 0:
        raise ValueError("action_timeout_sec must be a positive number")
    if (
        type(first_accept_timeout_sec) not in (int, float)
        or first_accept_timeout_sec <= 0
    ):
        raise ValueError("first_accept_timeout_sec must be a positive number")
    if type(match_timeout_sec) not in (int, float) or match_timeout_sec <= 0:
        raise ValueError("match_timeout_sec must be a positive number")
    seed_independence = seed_independence_snapshot_from_roots(
        training_seed=blueprint.training["config"]["seed"],
        deck_seed=deck_root_seed,
        policy_seeds=client_policy_seeds,
    )
    failures = forbidden_backend_imports()
    if failures:
        raise RuntimeError("legacy top-level engine dependency detected: " + ",".join(failures))
    backend_before = _backend_snapshot()
    manager = _DeterministicSeverManager(deck_root_seed, action_timeout_sec)
    server = await asyncio.start_server(manager.handle, "127.0.0.1", 0)
    assert server.sockets
    port = int(server.sockets[0].getsockname()[1])
    stop_event = threading.Event()
    first_client: asyncio.Task[Any] | None = None
    second_client: asyncio.Task[Any] | None = None
    first_accepted: asyncio.Task[bool] | None = None
    telemetry: list[Any]
    try:
        first_client = asyncio.create_task(
            asyncio.to_thread(
                run_hunl_tcp_client,
                "127.0.0.1",
                port,
                name="RouteA2Smoke0",
                blueprint=blueprint,
                seed=client_policy_seeds[0],
                action_delay_sec=0.0,
                framing=SEVER_LINE_FRAMING,
                match_timeout_sec=float(match_timeout_sec) + 5.0,
                stop_event=stop_event,
            )
        )
        first_accepted = asyncio.create_task(manager.first_client_accepted.wait())
        done, _ = await asyncio.wait(
            {first_client, first_accepted},
            timeout=float(first_accept_timeout_sec),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if first_accepted not in done:
            if first_client in done:
                await first_client
            raise TimeoutError("route client 0 was not accepted into seat 0")
        await first_accepted
        if len(manager.clients) != 1:
            raise RuntimeError("seat-0 acceptance boundary is not unique")
        second_client = asyncio.create_task(
            asyncio.to_thread(
                run_hunl_tcp_client,
                "127.0.0.1",
                port,
                name="RouteA2Smoke1",
                blueprint=blueprint,
                seed=client_policy_seeds[1],
                action_delay_sec=0.0,
                framing=SEVER_LINE_FRAMING,
                match_timeout_sec=float(match_timeout_sec) + 5.0,
                stop_event=stop_event,
            )
        )
        await asyncio.wait_for(
            manager.done.wait(), timeout=float(match_timeout_sec)
        )
        telemetry = list(await asyncio.gather(first_client, second_client))
    finally:
        stop_event.set()
        server.close()
        await server.wait_closed()
        if first_accepted is not None and not first_accepted.done():
            first_accepted.cancel()
        if first_accepted is not None:
            await asyncio.gather(first_accepted, return_exceptions=True)
        if manager.match_task is not None and not manager.match_task.done():
            manager.match_task.cancel()
        if manager.match_task is not None:
            await asyncio.gather(manager.match_task, return_exceptions=True)
        await asyncio.gather(
            *(client.close() for client in manager.clients),
            return_exceptions=True,
        )
        owned_clients = [
            task for task in (first_client, second_client) if task is not None
        ]
        if owned_clients:
            await asyncio.gather(*owned_clients, return_exceptions=True)
    if manager.error is not None:
        raise RuntimeError("sever match failed") from manager.error
    if manager.engine is None:
        raise RuntimeError("sever match did not create GameEngine")
    backend_after = _backend_snapshot()
    if backend_after != backend_before:
        raise RuntimeError("sever backend changed during the TCP smoke")
    if HUNLBlueprint(blueprint.payload).digest != blueprint.digest:
        raise RuntimeError("HUNL blueprint source binding changed during the TCP smoke")
    action_events = [event for event in manager.events if event.get("type") == "action"]
    illegal = [
        event
        for event in action_events
        if str(event.get("action", "")).startswith("illegal:")
    ]
    timeouts = [event for event in action_events if event.get("action") == "timeout"]
    match_end = [event for event in manager.events if event.get("type") == "match_end"]
    if (
        manager.engine.hand_num != 70
        or manager.engine.match_over
        or illegal
        or timeouts
        or len(match_end) != 1
        or any(not item.complete_70_hands for item in telemetry)
    ):
        raise RuntimeError("sever TCP smoke did not meet the complete clean 70-hand gate")
    influence_passed = all(
        item.trained_derived_policy_decisions >= 1
        and item.trained_nonuniform_policy_decisions >= 1
        and (
            item.trained_exact_decisions
            + item.trained_backoff_decisions
            + item.uniform_emergency_decisions
            == item.decisions
        )
        for item in telemetry
    )
    if not influence_passed:
        raise RuntimeError(
            "predeclared influence gate requires each client to consume at least one "
            "trained-derived non-uniform policy"
        )
    max_server_wait = max(
        (float(event.get("decision_wait_sec", 0.0)) for event in action_events),
        default=0.0,
    )
    body = {
        "action_timeout_sec": float(action_timeout_sec),
        "backend": {
            "authority": (
                "sever.GameEngine over asyncio TCP with explicit sever-local line adapter"
            ),
            "legacy_botzone_backend_used": False,
            "snapshot": backend_before,
        },
        "blueprint_sha256": blueprint.digest,
        "card_encoding": (
            "sever TCP <suit,rank> is decoded explicitly by Common cards.py; "
            "no integer suit reuse"
        ),
        "clients": [asdict(item) for item in telemetry],
        "deck_root_seed": deck_root_seed,
        "elapsed_sec": time.perf_counter() - started,
        "hands_played": manager.engine.hand_num,
        "illegal_actions": len(illegal),
        "influence_gate": {
            "acceptance_uses_chip_result": False,
            "contract": INFLUENCE_GATE_CONTRACT,
            "minimum_trained_derived_decisions_per_client": 1,
            "minimum_trained_nonuniform_policy_decisions_per_client": 1,
            "material_nonuniform_l1_threshold": HUNL_MATERIAL_POLICY_L1_THRESHOLD,
            "passed": influence_passed,
            "smoke_deck_or_opponent_specific_training": False,
            "trained_nonuniform_policy_decisions_reported": True,
        },
        "first_accept_timeout_sec": float(first_accept_timeout_sec),
        "local_sever_settlements_per_client": [
            item.settlements_received for item in telemetry
        ],
        "match_timeout_sec": float(match_timeout_sec),
        "max_server_decision_wait_ms": max_server_wait * 1000.0,
        "official_raw_no_delimiter_framing_proved": False,
        "official_terminal_hand_70_proved": False,
        "process_peak_rss_kib_at_completion": resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_maxrss,
        "result_authority": "diagnostic_only_not_strength_evidence",
        "server_action_events": len(action_events),
        "server_semantic_events": _semantic_server_events(manager.events),
        "seed_independence": seed_independence,
        "timeouts": len(timeouts),
        "transport_framing": SEVER_LINE_FRAMING,
        "total_earnings": list(manager.engine.total_earnings),
    }
    projection = build_tcp_semantic_projection(body)
    body["semantic_projection"] = projection
    body["semantic_projection_sha256"] = _sha256_bytes(
        _canonical_bytes(projection)
    )
    result = {
        "body": body,
        "body_sha256": _sha256_bytes(_canonical_bytes(body)),
        "schema": TCP_SMOKE_SCHEMA,
    }
    validate_tcp_semantic_projection(result)
    return result


def run_sever_tcp_smoke_sync(
    blueprint: HUNLBlueprint,
    *,
    deck_root_seed: int,
    client_policy_seeds: tuple[int, int] | list[int],
    action_timeout_sec: float = 5.0,
    first_accept_timeout_sec: float = 10.0,
    match_timeout_sec: float = 120.0,
) -> dict[str, object]:
    return asyncio.run(
        run_sever_tcp_smoke(
            blueprint,
            deck_root_seed=deck_root_seed,
            client_policy_seeds=client_policy_seeds,
            action_timeout_sec=action_timeout_sec,
            first_accept_timeout_sec=first_accept_timeout_sec,
            match_timeout_sec=match_timeout_sec,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blueprint", type=Path, required=True)
    parser.add_argument("--deck-root-seed", type=int, default=20260714)
    parser.add_argument(
        "--client-policy-seeds",
        type=int,
        nargs=2,
        default=(2026071403, 2026071404),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--verify-frozen",
        type=Path,
        help="run a real fixed-seed replay and compare its semantic projection",
    )
    args = parser.parse_args()
    paths = {
        "--blueprint": args.blueprint,
        "--output": args.output,
        "--verify-frozen": args.verify_frozen,
    }
    seen: dict[Path, str] = {}
    for label, raw_path in paths.items():
        if raw_path is None:
            continue
        path = _absolute_path(raw_path)
        previous = seen.get(path)
        if previous is not None:
            parser.error(f"{label} path overlaps {previous}: {path}")
        seen[path] = label
    blueprint_bytes = stable_read_path(args.blueprint)
    blueprint = HUNLBlueprint(strict_json_loads(blueprint_bytes))
    frozen_bytes = (
        None
        if args.verify_frozen is None
        else stable_read_path(args.verify_frozen)
    )
    result = run_sever_tcp_smoke_sync(
        blueprint,
        deck_root_seed=args.deck_root_seed,
        client_policy_seeds=args.client_policy_seeds,
    )
    if stable_read_path(args.blueprint) != blueprint_bytes:
        raise RuntimeError("blueprint input changed during the TCP smoke")
    if args.verify_frozen is not None:
        if stable_read_path(args.verify_frozen) != frozen_bytes:
            raise RuntimeError("frozen TCP evidence changed during semantic replay")
        frozen = strict_json_loads(frozen_bytes)
        assert_frozen_tcp_semantic_replay(frozen, result)
    if args.output is not None:
        _atomic_json(args.output, result)
        if stable_read_path(args.output) != pretty_json_bytes(result):
            raise RuntimeError("published TCP smoke evidence failed exact byte readback")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
