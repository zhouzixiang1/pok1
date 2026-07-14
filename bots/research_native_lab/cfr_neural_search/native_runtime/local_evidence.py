"""Deterministic diagnostic-only 70-hand real-TCP blueprint evidence."""

from __future__ import annotations

import asyncio
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from bots.research_native_lab.common_contracts.deal_generator import (
    ALGORITHM_DIGEST,
    ALGORITHM_ID,
    build_70_hand_commitment,
    generate_tcp_deck,
    tcp_card_from_id,
)
from sever.engine.deck import Card, Deck
from sever.engine.game import GameEngine
from sever.server.tcp_server import ClientConnection

from ..blueprint.artifact import BlueprintPolicy, load_blueprint_artifact
from ..blueprint.hunl_game import HUNLTrainingGame
from ..core.identity import file_sha256, payload_sha256, require_sha256
from ..core.strict_io import atomic_json_write, load_hashed_json
from .socket_client import NativeBlueprintClient, NativeClientResult


LOCAL_EVIDENCE_SCHEMA = "route-b-local-native-evidence-v1"
LOCAL_REPRODUCIBILITY_SCHEMA = "route-b-local-native-reproducibility-v1"
SEMANTIC_PROJECTION_SCHEMA = "route-b-local-native-semantic-projection-v1"
BACKEND_RELATIVE_FILES = (
    "sever/engine/game.py",
    "sever/engine/deck.py",
    "sever/engine/evaluator.py",
    "sever/engine/validator.py",
    "sever/engine/thp_recorder.py",
    "sever/server/protocol.py",
    "sever/server/tcp_server.py",
)


def _repository_root() -> Path:
    root = Path(__file__).resolve().parents[4]
    if root.is_symlink() or not (root / "sever" / "engine" / "game.py").is_file():
        raise ValueError("local evidence module resolved outside the repository root")
    return root


def sever_backend_hashes() -> dict[str, str]:
    root = _repository_root()
    result = {name: file_sha256(root / name) for name in BACKEND_RELATIVE_FILES}
    if set(result) != set(BACKEND_RELATIVE_FILES):
        raise AssertionError("local sever provenance is incomplete")
    return result


def _exact_deck(seed: int) -> Deck:
    deck = Deck(seed=0)
    deck.cards = [Card(*tcp_card_from_id(card)) for card in generate_tcp_deck(seed)]
    return deck


@dataclass(frozen=True, slots=True)
class LocalNativeEvidence:
    artifact_sha256: str
    artifact_payload_sha256: str
    source_solver_sha256: str
    backend_files: Mapping[str, str]
    backend_sha256: str
    deck_root_seed: int
    deck_algorithm_id: str
    deck_algorithm_sha256: str
    deck_window_sha256: str
    policy_seeds: tuple[int, int]
    hands: int
    actions: int
    actions_by_side: tuple[int, int]
    illegal_actions: int
    timeouts: int
    earnings: tuple[int, int]
    sides: tuple[Mapping[str, Any], Mapping[str, Any]]
    acceptance: Mapping[str, Any]
    semantic_projection: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


async def _run_async_match(
    artifact_path: Path,
    *,
    deck_root_seed: int,
    policy_seeds: tuple[int, int],
) -> tuple[Any, tuple[NativeClientResult, NativeClientResult], list[dict[str, Any]]]:
    game = HUNLTrainingGame()
    blueprint = load_blueprint_artifact(
        artifact_path,
        game,
        root=artifact_path.parent,
    )
    commitment = build_70_hand_commitment(deck_root_seed)
    connections: list[ClientConnection] = []
    first_connected = asyncio.Event()
    both_connected = asyncio.Event()

    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(connections) >= 2:
            writer.close()
            await writer.wait_closed()
            return
        connections.append(ClientConnection(reader, writer))
        if len(connections) == 1:
            first_connected.set()
        if len(connections) == 2:
            both_connected.set()

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    socket_info = server.sockets[0].getsockname()
    port = int(socket_info[1])

    def run_side(index: int) -> NativeClientResult:
        policy = BlueprintPolicy(blueprint)
        client = NativeBlueprintClient(
            bot_name=f"RouteB{index}",
            policy=policy,
            policy_seed=policy_seeds[index],
            wire_mode="local-sever-lf",
            action_delay_sec=0.0,
        )
        return client.run("127.0.0.1", port, match_timeout_sec=120.0)

    task0 = asyncio.create_task(asyncio.to_thread(run_side, 0))
    await asyncio.wait_for(first_connected.wait(), timeout=10.0)
    task1 = asyncio.create_task(asyncio.to_thread(run_side, 1))
    await asyncio.wait_for(both_connected.wait(), timeout=10.0)
    events: list[dict[str, Any]] = []

    async def broadcast(event: dict[str, Any]) -> None:
        events.append(dict(event))

    async def send(player: int, message: str) -> None:
        await connections[player].send_line(message)

    async def receive(player: int) -> str | None:
        return await connections[player].recv_line(timeout=5.0)

    for index, connection in enumerate(connections):
        await connection.send_line("name")
        name = await connection.recv_line(timeout=5.0)
        if name != f"RouteB{index}":
            raise RuntimeError("native client name handshake was not exact")
        connection.name = name

    engine = GameEngine(
        send_func=send,
        broadcast_func=broadcast,
        recorder=None,
        deck_factory=lambda hand: _exact_deck(commitment.hand_seeds[hand - 1]),
    )
    engine.action_timeout_sec = 5.0
    engine._recv_action = receive
    try:
        await asyncio.wait_for(engine.run_match("RouteB0", "RouteB1"), timeout=120.0)
    finally:
        for connection in connections:
            await connection.close()
        server.close()
        await server.wait_closed()
    results = await asyncio.wait_for(
        asyncio.gather(task0, task1),
        timeout=20.0,
    )
    return blueprint, (results[0], results[1]), events


def run_local_blueprint_match(
    artifact_path: str | Path,
    *,
    deck_root_seed: int,
    policy_seeds: tuple[int, int],
) -> LocalNativeEvidence:
    if type(deck_root_seed) is not int:
        raise TypeError("deck root seed must be an exact integer")
    if (
        type(policy_seeds) is not tuple
        or len(policy_seeds) != 2
        or any(type(seed) is not int for seed in policy_seeds)
        or len(set(policy_seeds)) != 2
        or deck_root_seed in policy_seeds
    ):
        raise ValueError("deck and two policy roots must be distinct exact integers")
    path = Path(artifact_path)
    before_backend = sever_backend_hashes()
    blueprint, results, events = asyncio.run(
        _run_async_match(
            path,
            deck_root_seed=deck_root_seed,
            policy_seeds=policy_seeds,
        )
    )
    after_backend = sever_backend_hashes()
    if after_backend != before_backend:
        raise ValueError("local sever backend changed during 70-hand evidence")
    commitment = build_70_hand_commitment(deck_root_seed)
    deck_window_sha = payload_sha256(
        {
            "algorithm": ALGORITHM_ID,
            "algorithm_sha256": ALGORITHM_DIGEST,
            "deck_digests": list(commitment.deck_digests),
        }
    )
    action_events = [event for event in events if event.get("type") == "action"]
    settlements = [event for event in events if event.get("type") == "settle"]
    illegal = sum(str(event.get("action", "")).startswith("illegal:") for event in action_events)
    timeouts = sum(event.get("action") == "timeout" for event in action_events)
    actions_by_side = tuple(
        sum(event.get("player_idx") == side for event in action_events)
        for side in (0, 1)
    )
    side_payloads = tuple(result.to_payload() for result in results)
    earnings = (int(results[0].cumulative_net_hero), int(results[1].cumulative_net_hero))
    acceptance = {
        "diagnostic_only": True,
        "hands_70": len(settlements) == 70,
        "both_sessions_70": all(result.hands_started == 70 for result in results),
        "zero_illegal": illegal == 0,
        "zero_timeouts": timeouts == 0,
        "zero_process_failures": True,
        "both_sides_materially_nonuniform": all(
            result.materially_nonuniform_decisions > 0 for result in results
        ),
        "both_sides_blueprint_source": all(
            result.exact_hits + result.backoff_hits > 0 for result in results
        ),
        "artifact_has_material_rows": (
            blueprint.statistics["materially_nonuniform_all_rows"] > 0
        ),
        "minimum_two_training_batches": (
            blueprint.resources["training_batches"] >= 2
        ),
        "chips_have_zero_acceptance_weight": True,
    }
    projection = {
        "schema": SEMANTIC_PROJECTION_SCHEMA,
        "artifact_sha256": blueprint.artifact_sha256,
        "artifact_payload_sha256": blueprint.payload_sha256,
        "source_solver_sha256": blueprint.source_solver_sha256,
        "backend_sha256": payload_sha256({"files": before_backend}),
        "deck_root_seed": deck_root_seed,
        "deck_window_sha256": deck_window_sha,
        "policy_seeds": list(policy_seeds),
        "hands": len(settlements),
        "actions": len(action_events),
        "actions_by_side": list(actions_by_side),
        "illegal_actions": illegal,
        "timeouts": timeouts,
        "earnings": list(earnings),
        "sides": [
            {
                key: payload[key]
                for key in (
                    "decisions",
                    "exact_hits",
                    "backoff_hits",
                    "uniform_emergency",
                    "materially_nonuniform_decisions",
                    "hands_started",
                    "settlements_received",
                    "cumulative_net_hero",
                )
            }
            for payload in side_payloads
        ],
    }
    if not all(acceptance.values()):
        failures = sorted(key for key, value in acceptance.items() if not value)
        raise RuntimeError(f"local blueprint evidence gate failed: {failures}")
    return LocalNativeEvidence(
        blueprint.artifact_sha256,
        blueprint.payload_sha256,
        blueprint.source_solver_sha256,
        before_backend,
        projection["backend_sha256"],
        deck_root_seed,
        ALGORITHM_ID,
        ALGORITHM_DIGEST,
        deck_window_sha,
        policy_seeds,
        len(settlements),
        len(action_events),
        actions_by_side,
        illegal,
        timeouts,
        earnings,
        side_payloads,
        acceptance,
        projection,
    )


def run_reproducibility_gate(
    artifact_path: str | Path,
    *,
    deck_root_seed: int,
    policy_seeds: tuple[int, int],
) -> tuple[LocalNativeEvidence, LocalNativeEvidence]:
    first = run_local_blueprint_match(
        artifact_path,
        deck_root_seed=deck_root_seed,
        policy_seeds=policy_seeds,
    )
    second = run_local_blueprint_match(
        artifact_path,
        deck_root_seed=deck_root_seed,
        policy_seeds=policy_seeds,
    )
    if first.semantic_projection != second.semantic_projection:
        raise RuntimeError("fixed native TCP semantic projection is not reproducible")
    return first, second


def save_reproducibility_evidence(
    path: str | Path,
    first: LocalNativeEvidence,
    second: LocalNativeEvidence,
    *,
    root: str | Path | None = None,
) -> str:
    if type(first) is not LocalNativeEvidence or type(second) is not LocalNativeEvidence:
        raise TypeError("local evidence save requires two exact evidence objects")
    if first.semantic_projection != second.semantic_projection:
        raise ValueError("cannot save unequal semantic replay projections")
    payload = {
        "schema": LOCAL_REPRODUCIBILITY_SCHEMA,
        "result_authority": "diagnostic_only",
        "chip_result_acceptance_weight": 0,
        "run_count": 2,
        "semantic_projection_sha256": payload_sha256(first.semantic_projection),
        "runs": [first.to_payload(), second.to_payload()],
    }
    return atomic_json_write(path, payload, root=root)


def load_reproducibility_evidence(
    path: str | Path,
    *,
    root: str | Path | None = None,
) -> Mapping[str, Any]:
    payload = load_hashed_json(path, root=root)
    if type(payload) is not dict or set(payload) != {
        "schema",
        "result_authority",
        "chip_result_acceptance_weight",
        "run_count",
        "semantic_projection_sha256",
        "runs",
    }:
        raise ValueError("local reproducibility evidence differs from strict schema")
    if (
        payload["schema"] != LOCAL_REPRODUCIBILITY_SCHEMA
        or payload["result_authority"] != "diagnostic_only"
        or type(payload["chip_result_acceptance_weight"]) is not int
        or payload["chip_result_acceptance_weight"] != 0
        or type(payload["run_count"]) is not int
        or payload["run_count"] != 2
    ):
        raise ValueError("local reproducibility evidence authority/count drifted")
    runs = payload["runs"]
    if type(runs) is not list or len(runs) != 2 or any(type(run) is not dict for run in runs):
        raise TypeError("local reproducibility evidence must contain two run objects")
    projections = [run.get("semantic_projection") for run in runs]
    if any(type(value) is not dict for value in projections) or projections[0] != projections[1]:
        raise ValueError("stored semantic replay projections differ")
    if payload_sha256(projections[0]) != payload["semantic_projection_sha256"]:
        raise ValueError("stored semantic projection digest mismatch")
    require_sha256(
        payload["semantic_projection_sha256"],
        "stored semantic projection digest",
    )
    expected_run_keys = set(LocalNativeEvidence.__dataclass_fields__)
    expected_side_keys = set(NativeClientResult.__dataclass_fields__)
    expected_acceptance_keys = {
        "diagnostic_only",
        "hands_70",
        "both_sessions_70",
        "zero_illegal",
        "zero_timeouts",
        "zero_process_failures",
        "both_sides_materially_nonuniform",
        "both_sides_blueprint_source",
        "artifact_has_material_rows",
        "minimum_two_training_batches",
        "chips_have_zero_acceptance_weight",
    }
    expected_projection_keys = {
        "schema",
        "artifact_sha256",
        "artifact_payload_sha256",
        "source_solver_sha256",
        "backend_sha256",
        "deck_root_seed",
        "deck_window_sha256",
        "policy_seeds",
        "hands",
        "actions",
        "actions_by_side",
        "illegal_actions",
        "timeouts",
        "earnings",
        "sides",
    }
    for run in runs:
        if set(run) != expected_run_keys:
            raise ValueError("stored local run differs from strict evidence schema")
        for field in (
            "artifact_sha256",
            "artifact_payload_sha256",
            "source_solver_sha256",
            "backend_sha256",
            "deck_algorithm_sha256",
            "deck_window_sha256",
        ):
            require_sha256(run[field], f"stored local run {field}")
        backend_files = run["backend_files"]
        if (
            type(backend_files) is not dict
            or set(backend_files) != set(BACKEND_RELATIVE_FILES)
            or any(type(name) is not str for name in backend_files)
        ):
            raise ValueError("stored local run has incomplete sever backend files")
        for name, digest in backend_files.items():
            require_sha256(digest, f"stored sever backend {name}")
        if payload_sha256({"files": backend_files}) != run["backend_sha256"]:
            raise ValueError("stored sever backend digest disagrees with files")
        if (
            run["deck_algorithm_id"] != ALGORITHM_ID
            or run["deck_algorithm_sha256"] != ALGORITHM_DIGEST
        ):
            raise ValueError("stored deck algorithm identity drifted")
        seeds = run["policy_seeds"]
        deck_seed = run["deck_root_seed"]
        if (
            type(deck_seed) is not int
            or type(seeds) is not list
            or len(seeds) != 2
            or any(type(seed) is not int for seed in seeds)
            or len({deck_seed, *seeds}) != 3
        ):
            raise ValueError("stored deck/policy roots are not distinct exact integers")
        commitment = build_70_hand_commitment(deck_seed)
        expected_window = payload_sha256(
            {
                "algorithm": ALGORITHM_ID,
                "algorithm_sha256": ALGORITHM_DIGEST,
                "deck_digests": list(commitment.deck_digests),
            }
        )
        if run["deck_window_sha256"] != expected_window:
            raise ValueError("stored 70-hand deck window digest drifted")
        for field in ("hands", "actions", "illegal_actions", "timeouts"):
            if type(run[field]) is not int or run[field] < 0:
                raise ValueError(f"stored local run {field} must be nonnegative exact int")
        actions_by_side = run["actions_by_side"]
        earnings = run["earnings"]
        if (
            type(actions_by_side) is not list
            or len(actions_by_side) != 2
            or any(type(value) is not int or value < 0 for value in actions_by_side)
            or sum(actions_by_side) != run["actions"]
            or type(earnings) is not list
            or len(earnings) != 2
            or any(type(value) is not int for value in earnings)
            or sum(earnings) != 0
        ):
            raise ValueError("stored local action/earning totals are inconsistent")
        sides = run["sides"]
        if type(sides) is not list or len(sides) != 2:
            raise ValueError("stored local run must contain exactly two sides")
        projected_sides: list[dict[str, Any]] = []
        for index, side in enumerate(sides):
            if type(side) is not dict or set(side) != expected_side_keys:
                raise ValueError("stored native side differs from strict result schema")
            if (
                side["bot_name"] != f"RouteB{index}"
                or side["wire_mode"] != "local-sever-lf"
                or type(side["action_delay_sec"]) not in (int, float)
                or not math.isfinite(float(side["action_delay_sec"]))
                or side["action_delay_sec"] != 0
                or side["policy_seed"] != seeds[index]
                or side["completion_authority"]
                != "diagnostic_local_wire_complete"
                or side["requires_external_thp"] is not False
                or side["wire_complete"] is not True
            ):
                raise ValueError("stored native side authority/wire identity drifted")
            for field in (
                "decisions",
                "exact_hits",
                "backoff_hits",
                "uniform_emergency",
                "materially_nonuniform_decisions",
                "hands_started",
                "settlements_received",
            ):
                if type(side[field]) is not int or side[field] < 0:
                    raise ValueError(f"stored native side {field} is invalid")
            if (
                side["decisions"] != actions_by_side[index]
                or side["exact_hits"]
                + side["backoff_hits"]
                + side["uniform_emergency"]
                != side["decisions"]
                or side["materially_nonuniform_decisions"] > side["decisions"]
                or side["hands_started"] != 70
                or side["settlements_received"] != 70
                or type(side["cumulative_net_hero"]) is not int
                or side["cumulative_net_hero"] != earnings[index]
            ):
                raise ValueError("stored native side counters are inconsistent")
            expected_close = {
                "hands_started": 70,
                "wire_settlements": 70,
                "natural_70_boundary": True,
                "hand_70_terminal_wire_state": True,
                "requires_thp_state_69": False,
                "wire_alone_proves_complete": True,
            }
            if side["close_evidence"] != expected_close:
                raise ValueError("stored local EOF evidence is not exact 70/70 shape")
            projected_sides.append(
                {
                    key: side[key]
                    for key in (
                        "decisions",
                        "exact_hits",
                        "backoff_hits",
                        "uniform_emergency",
                        "materially_nonuniform_decisions",
                        "hands_started",
                        "settlements_received",
                        "cumulative_net_hero",
                    )
                }
            )
        acceptance = run.get("acceptance")
        if type(acceptance) is not dict or set(acceptance) != expected_acceptance_keys or not all(
            value is True for value in acceptance.values()
        ):
            raise ValueError("stored local evidence acceptance is incomplete")
        projection = run["semantic_projection"]
        if type(projection) is not dict or set(projection) != expected_projection_keys:
            raise ValueError("stored semantic projection differs from strict schema")
        expected_projection = {
            "schema": SEMANTIC_PROJECTION_SCHEMA,
            "artifact_sha256": run["artifact_sha256"],
            "artifact_payload_sha256": run["artifact_payload_sha256"],
            "source_solver_sha256": run["source_solver_sha256"],
            "backend_sha256": run["backend_sha256"],
            "deck_root_seed": deck_seed,
            "deck_window_sha256": run["deck_window_sha256"],
            "policy_seeds": seeds,
            "hands": run["hands"],
            "actions": run["actions"],
            "actions_by_side": actions_by_side,
            "illegal_actions": run["illegal_actions"],
            "timeouts": run["timeouts"],
            "earnings": earnings,
            "sides": projected_sides,
        }
        if projection != expected_projection:
            raise ValueError("stored semantic projection is not the full run projection")
    return payload


def save_local_evidence(
    path: str | Path,
    evidence: LocalNativeEvidence,
    *,
    root: str | Path | None = None,
) -> str:
    if type(evidence) is not LocalNativeEvidence:
        raise TypeError("save requires exact LocalNativeEvidence")
    payload = {"schema": LOCAL_EVIDENCE_SCHEMA, "evidence": evidence.to_payload()}
    return atomic_json_write(path, payload, root=root)


def load_local_evidence(
    path: str | Path,
    *,
    root: str | Path | None = None,
) -> Mapping[str, Any]:
    payload = load_hashed_json(path, root=root)
    if type(payload) is not dict or set(payload) != {"schema", "evidence"}:
        raise ValueError("local evidence envelope has wrong schema")
    if payload["schema"] != LOCAL_EVIDENCE_SCHEMA or type(payload["evidence"]) is not dict:
        raise ValueError("unsupported local evidence")
    return payload["evidence"]
