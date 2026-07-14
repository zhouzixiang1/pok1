from __future__ import annotations

import copy
import hashlib
import inspect
import json
import socket
import sys
import threading
import asyncio
from pathlib import Path

import pytest

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.national_state import NationalGameState
from ...common_contracts.protocol import StreamDecoder
from ..decisionholdem_like.common_native_entry import CommonA2StrategyRuntime
from ..decisionholdem_like.hunl_blueprint import (
    HUNL_MATERIAL_POLICY_L1_THRESHOLD,
    HUNLBlueprint,
)
from ..decisionholdem_like.hunl_tcp_client import (
    OFFICIAL_RAW_FRAMING,
    SEVER_LINE_FRAMING,
    _connection_close_status,
    encode_client_message,
    run_hunl_tcp_client,
)
from ..tools.run_hunl_tcp_smoke import (
    BACKEND_FILES,
    INFLUENCE_GATE_CONTRACT,
    PACKAGE_ROOT,
    TCP_SEMANTIC_PROJECTION_SCHEMA,
    TCP_SMOKE_SCHEMA,
    _backend_snapshot,
    assert_frozen_tcp_semantic_replay,
    build_tcp_semantic_projection,
    forbidden_backend_imports,
    run_sever_tcp_smoke,
    run_sever_tcp_smoke_sync,
    validate_tcp_semantic_projection,
    validate_backend_snapshot,
)
from ..tools import run_hunl_tcp_smoke as tcp_smoke_tool
from ..tools.train_hunl_blueprint import (
    SCALE_SCHEMA,
    blueprint_nonuniformity_snapshot,
    load_config,
    seed_independence_snapshot,
)


ARTIFACT = PACKAGE_ROOT / "artifacts/hunl_m4_smoke_blueprint.json"
EVIDENCE = PACKAGE_ROOT / "evidence/m4_sever_tcp_70h.json"
SCALE_EVIDENCE = PACKAGE_ROOT / "evidence/m4_scale_gate.json"
CONFIG = load_config(PACKAGE_ROOT / "configs/hunl_m4_smoke.json")


class _FailurePathBlueprint:
    training = {"config": {"seed": 2026071402}}
    digest = "0" * 64


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_official_client_defaults_to_raw_no_delimiter_and_point_three_delay() -> None:
    signature = inspect.signature(run_hunl_tcp_client)
    assert signature.parameters["action_delay_sec"].default == 0.30
    assert signature.parameters["framing"].default == OFFICIAL_RAW_FRAMING
    assert encode_client_message("call", OFFICIAL_RAW_FRAMING) == b"call"
    assert encode_client_message("call", SEVER_LINE_FRAMING) == b"call\n"


def test_official_69_settlement_terminal_eof_is_clean_but_requires_thp() -> None:
    runtime = CommonA2StrategyRuntime(
        "OfficialBoundary",
        HUNLBlueprint.load(ARTIFACT),
        seed=2026071419,
    )
    runtime.session.hands_started = 70
    runtime.session.settlements_received = 69
    runtime.session.current = NationalGameState.new_hand(
        70,
        small_blind=0,
    ).apply_action(Action(ActionKind.FOLD))
    status = _connection_close_status(
        runtime,
        framing=OFFICIAL_RAW_FRAMING,
        saw_eof=True,
    )
    assert status == {
        "certification_claimed": False,
        "clean_connection_close": True,
        "external_thp_state_69_required": True,
        "local_sever_complete_70_hands": False,
        "official_natural_70_boundary": True,
        "official_wire_alone_proves_complete": False,
    }
    with pytest.raises(RuntimeError, match="no EOF"):
        _connection_close_status(
            runtime,
            framing=OFFICIAL_RAW_FRAMING,
            saw_eof=False,
        )


def test_official_close_rejects_decoder_tail_and_pending_decision() -> None:
    runtime = CommonA2StrategyRuntime(
        "OfficialTail",
        HUNLBlueprint.load(ARTIFACT),
        seed=2026071421,
    )
    runtime.decoder.feed("prefl")
    with pytest.raises(RuntimeError, match="undecoded"):
        _connection_close_status(
            runtime,
            framing=OFFICIAL_RAW_FRAMING,
            saw_eof=True,
        )


def test_route_has_no_top_level_engine_or_botzone_battle_dependency() -> None:
    assert forbidden_backend_imports() == []
    forbidden_text = (
        "engine" + "/battle.py",
        "engine" + ".battle",
        "Botzone" + " JSON stdin",
    )
    for path in PACKAGE_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden_text)


def test_backend_snapshot_covers_complete_transitive_sever_runtime() -> None:
    assert BACKEND_FILES == (
        "sever/engine/game.py",
        "sever/engine/deck.py",
        "sever/engine/evaluator.py",
        "sever/engine/validator.py",
        "sever/engine/thp_recorder.py",
        "sever/server/protocol.py",
        "sever/server/tcp_server.py",
    )
    snapshot = _backend_snapshot()
    assert tuple(snapshot["files"]) == BACKEND_FILES
    assert validate_backend_snapshot(snapshot) == snapshot


def _assert_no_owned_async_tasks() -> None:
    current = asyncio.current_task()
    assert [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ] == []


def test_immediate_client_failure_cleans_event_waiter_and_owned_task(
    monkeypatch,
) -> None:
    finished = threading.Event()

    def fail_immediately(*args, **kwargs):
        finished.set()
        raise RuntimeError("client boom")

    monkeypatch.setattr(tcp_smoke_tool, "_backend_snapshot", lambda: {})
    monkeypatch.setattr(tcp_smoke_tool, "forbidden_backend_imports", lambda: [])
    monkeypatch.setattr(tcp_smoke_tool, "run_hunl_tcp_client", fail_immediately)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="client boom"):
            await run_sever_tcp_smoke(
                _FailurePathBlueprint(),
                deck_root_seed=20260714,
                client_policy_seeds=(2026071403, 2026071404),
                first_accept_timeout_sec=0.05,
            )
        await asyncio.sleep(0)
        assert finished.is_set()
        _assert_no_owned_async_tasks()

    asyncio.run(scenario())


def test_first_accept_timeout_cooperatively_stops_client_thread(
    monkeypatch,
) -> None:
    started = threading.Event()
    finished = threading.Event()

    def wait_without_connecting(*args, **kwargs):
        started.set()
        kwargs["stop_event"].wait()
        finished.set()
        raise RuntimeError("client stopped")

    monkeypatch.setattr(tcp_smoke_tool, "_backend_snapshot", lambda: {})
    monkeypatch.setattr(tcp_smoke_tool, "forbidden_backend_imports", lambda: [])
    monkeypatch.setattr(
        tcp_smoke_tool,
        "run_hunl_tcp_client",
        wait_without_connecting,
    )

    async def scenario() -> None:
        with pytest.raises(TimeoutError, match="seat 0"):
            await run_sever_tcp_smoke(
                _FailurePathBlueprint(),
                deck_root_seed=20260714,
                client_policy_seeds=(2026071403, 2026071404),
                first_accept_timeout_sec=0.01,
            )
        await asyncio.sleep(0)
        assert started.is_set() and finished.is_set()
        _assert_no_owned_async_tasks()

    asyncio.run(scenario())


def test_manager_timeout_closes_both_client_sockets_threads_and_match_task(
    monkeypatch,
) -> None:
    started: list[str] = []
    finished: list[str] = []

    def connect_and_wait_for_stop(host, port, *args, **kwargs):
        name = kwargs["name"]
        started.append(name)
        with socket.create_connection((host, port), timeout=1.0):
            kwargs["stop_event"].wait()
        finished.append(name)
        raise RuntimeError("client stopped")

    monkeypatch.setattr(tcp_smoke_tool, "_backend_snapshot", lambda: {})
    monkeypatch.setattr(tcp_smoke_tool, "forbidden_backend_imports", lambda: [])
    monkeypatch.setattr(
        tcp_smoke_tool,
        "run_hunl_tcp_client",
        connect_and_wait_for_stop,
    )

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await run_sever_tcp_smoke(
                _FailurePathBlueprint(),
                deck_root_seed=20260714,
                client_policy_seeds=(2026071403, 2026071404),
                first_accept_timeout_sec=1.0,
                match_timeout_sec=0.02,
            )
        await asyncio.sleep(0)
        assert started == ["RouteA2Smoke0", "RouteA2Smoke1"]
        assert sorted(finished) == ["RouteA2Smoke0", "RouteA2Smoke1"]
        _assert_no_owned_async_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend_file", BACKEND_FILES)
def test_backend_snapshot_rejects_resigned_per_file_drift(backend_file) -> None:
    drifted = copy.deepcopy(_backend_snapshot())
    drifted["files"][backend_file] = "0" * 64
    drifted["tree_sha256"] = _digest(drifted["files"])
    with pytest.raises(ValueError, match="differs from the loaded runtime"):
        validate_backend_snapshot(drifted)


def test_frozen_sever_tcp_evidence_is_content_bound_and_non_strength() -> None:
    payload = json.loads(EVIDENCE.read_text())
    assert payload["schema"] == TCP_SMOKE_SCHEMA
    assert payload["body_sha256"] == _digest(payload["body"])
    body = payload["body"]
    projection = validate_tcp_semantic_projection(payload)
    assert projection["schema"] == TCP_SEMANTIC_PROJECTION_SCHEMA
    assert projection["acceptance_excludes_chip_result"] is True
    assert projection["earnings_are_reproducible_diagnostic_record_only"] is True
    assert body["backend"]["authority"] == (
        "sever.GameEngine over asyncio TCP with explicit sever-local line adapter"
    )
    assert body["backend"]["legacy_botzone_backend_used"] is False
    assert body["hands_played"] == 70
    assert body["illegal_actions"] == body["timeouts"] == 0
    assert body["result_authority"] == "diagnostic_only_not_strength_evidence"
    assert body["transport_framing"] == SEVER_LINE_FRAMING
    assert body["official_raw_no_delimiter_framing_proved"] is False
    assert body["official_terminal_hand_70_proved"] is False
    assert body["influence_gate"]["contract"] == INFLUENCE_GATE_CONTRACT
    assert body["influence_gate"]["passed"] is True
    assert body["influence_gate"]["acceptance_uses_chip_result"] is False
    assert body["influence_gate"]["material_nonuniform_l1_threshold"] == (
        HUNL_MATERIAL_POLICY_L1_THRESHOLD
    )
    assert body["seed_independence"] == seed_independence_snapshot(CONFIG)
    assert sum(body["total_earnings"]) == 0
    assert all(client["complete_70_hands"] for client in body["clients"])
    assert all(client["settlements_received"] == 70 for client in body["clients"])
    assert all(client["trained_derived_policy_decisions"] > 0 for client in body["clients"])
    assert all(client["trained_nonuniform_policy_decisions"] > 0 for client in body["clients"])
    assert all(
        client["max_trained_policy_l1_from_uniform"]
        > HUNL_MATERIAL_POLICY_L1_THRESHOLD
        for client in body["clients"]
    )
    assert all(
        client["trained_exact_decisions"]
        + client["trained_backoff_decisions"]
        + client["uniform_emergency_decisions"]
        == client["decisions"]
        for client in body["clients"]
    )
    assert body["blueprint_sha256"] == HUNLBlueprint.load(ARTIFACT).digest


def test_semantic_projection_excludes_only_runtime_timing_fields() -> None:
    body = {
        field: None for field in tcp_smoke_tool._PROJECTION_INPUT_FIELDS
    }
    body["clients"] = [
        {
            "decisions": 7,
            "elapsed_sec": 0.5,
            "max_decision_compute_ms": 0.1,
        }
    ]
    body["server_semantic_events"] = []
    body["total_earnings"] = [0, 0]
    expected = build_tcp_semantic_projection(body)
    body["elapsed_sec"] = 10**9
    body["max_server_decision_wait_ms"] = 10**9
    body["process_peak_rss_kib_at_completion"] = 10**9
    for client in body["clients"]:
        client["elapsed_sec"] = 10**9
        client["max_decision_compute_ms"] = 10**9
    assert build_tcp_semantic_projection(body) == expected
    body["total_earnings"][0] += 1
    assert build_tcp_semantic_projection(body) != expected
    body["unknown_future_semantic_field"] = "must not be silently excluded"
    with pytest.raises(ValueError, match="unknown"):
        build_tcp_semantic_projection(body)


@pytest.mark.parametrize(
    ("blueprint_name", "arguments"),
    (
        ("same.json", ("--output", "same.json")),
        ("same.json", ("--verify-frozen", "same.json")),
        (
            "blueprint.json",
            ("--output", "same.json", "--verify-frozen", "same.json"),
        ),
    ),
)
def test_tcp_cli_never_overwrites_or_reuses_the_blueprint_path(
    monkeypatch,
    blueprint_name,
    arguments,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_hunl_tcp_smoke",
            "--blueprint",
            blueprint_name,
            *arguments,
        ],
    )
    with pytest.raises(SystemExit) as exc:
        tcp_smoke_tool.main()
    assert exc.value.code == 2


def test_frozen_artifact_has_materially_nonuniform_exact_or_backoff_rows() -> None:
    payload = json.loads(SCALE_EVIDENCE.read_text())
    assert payload["schema"] == SCALE_SCHEMA
    assert payload["body_sha256"] == _digest(payload["body"])
    blueprint = HUNLBlueprint.load(ARTIFACT)
    snapshot = blueprint_nonuniformity_snapshot(blueprint)
    assert payload["body"]["policy_nonuniformity"] == snapshot
    assert snapshot["l1_threshold"] == HUNL_MATERIAL_POLICY_L1_THRESHOLD
    assert snapshot["total_materially_nonuniform_rows"] > 0
    table_stats = [snapshot["exact"], *snapshot["trained_backoff"].values()]
    assert max(table["max_l1_from_uniform"] for table in table_stats) > (
        HUNL_MATERIAL_POLICY_L1_THRESHOLD
    )


def test_official_raw_socket_sticky_split_and_no_newline_regression() -> None:
    platform, client = socket.socketpair()
    try:
        runtime = CommonA2StrategyRuntime(
            "RawRouteA2",
            HUNLBlueprint.load(ARTIFACT),
            seed=0,
        )
        platform.sendall(b"namepreflop|SMALLBLIND|<0,12>")
        outputs = runtime.feed(client.recv(4096))
        assert [(event.kind, outgoing) for event, outgoing in outputs] == [
            ("name_requested", "RawRouteA2")
        ]
        client.sendall(encode_client_message(outputs[0][1], OFFICIAL_RAW_FRAMING))
        assert platform.recv(4096) == b"RawRouteA2"

        platform.sendall(b"<0,11>")
        outputs = runtime.feed(client.recv(4096))
        assert len(outputs) == 1 and outputs[0][1] is not None
        client.sendall(encode_client_message(outputs[0][1], OFFICIAL_RAW_FRAMING))
        raw_action = platform.recv(4096)
        assert raw_action in {b"fold", b"call", b"allin", b"raise 200", b"raise 300", b"raise 400"}
        assert b"\n" not in raw_action and b"\r" not in raw_action
        assert runtime._act_if_pending() is None

        decoder = StreamDecoder()
        platform.sendall(b"raise 2")
        assert decoder.feed(client.recv(4096)) == []
        platform.sendall(b"00call")
        assert decoder.feed(client.recv(4096)) == ["raise 200", "call"]
    finally:
        platform.close()
        client.close()


def test_real_unmodified_sever_game_engine_completes_70_socket_hands_cleanly() -> None:
    result = run_sever_tcp_smoke_sync(
        HUNLBlueprint.load(ARTIFACT),
        deck_root_seed=CONFIG["tcp_deck_root_seed"],
        client_policy_seeds=CONFIG["tcp_client_policy_seeds"],
    )
    body = result["body"]
    assert body["hands_played"] == 70
    assert body["illegal_actions"] == 0
    assert body["timeouts"] == 0
    assert body["server_action_events"] > 0
    assert sum(body["total_earnings"]) == 0
    assert all(item["trained_derived_policy_decisions"] > 0 for item in body["clients"])
    assert all(item["trained_nonuniform_policy_decisions"] > 0 for item in body["clients"])
    assert all(
        item["max_trained_policy_l1_from_uniform"]
        > HUNL_MATERIAL_POLICY_L1_THRESHOLD
        for item in body["clients"]
    )
    hand_start = next(
        event
        for event in body["semantic_projection"]["server_events"]
        if event["type"] == "hand_start"
    )
    assert hand_start["names"] == ["RouteA2Smoke0", "RouteA2Smoke1"]
    frozen = json.loads(EVIDENCE.read_text())
    assert_frozen_tcp_semantic_replay(frozen, result)


def test_second_client_is_not_created_before_seat_zero_acceptance(
    monkeypatch,
) -> None:
    original_client = tcp_smoke_tool.run_hunl_tcp_client
    original_to_thread = tcp_smoke_tool.asyncio.to_thread
    release_zero = threading.Event()
    second_started = threading.Event()
    created_clients: list[str] = []

    async def scenario() -> dict[str, object]:
        loop = asyncio.get_running_loop()
        zero_entered = asyncio.Event()

        def controlled_client(*args, **kwargs):
            if kwargs["name"] == "RouteA2Smoke0":
                loop.call_soon_threadsafe(zero_entered.set)
                if not release_zero.wait(timeout=1.0):
                    raise TimeoutError("test did not release route client zero")
            else:
                second_started.set()
            return original_client(*args, **kwargs)

        def track_client_creation(function, /, *args, **kwargs):
            if function is controlled_client:
                created_clients.append(kwargs["name"])
            return original_to_thread(function, *args, **kwargs)

        monkeypatch.setattr(
            tcp_smoke_tool,
            "run_hunl_tcp_client",
            controlled_client,
        )
        monkeypatch.setattr(
            tcp_smoke_tool.asyncio,
            "to_thread",
            track_client_creation,
        )
        smoke_task = asyncio.create_task(
            run_sever_tcp_smoke(
                HUNLBlueprint.load(ARTIFACT),
                deck_root_seed=CONFIG["tcp_deck_root_seed"],
                client_policy_seeds=CONFIG["tcp_client_policy_seeds"],
            )
        )
        try:
            await asyncio.wait_for(zero_entered.wait(), timeout=1.0)
            assert created_clients == ["RouteA2Smoke0"]
            assert second_started.is_set() is False
        except BaseException:
            release_zero.set()
            await asyncio.gather(smoke_task, return_exceptions=True)
            raise
        release_zero.set()
        return await smoke_task

    result = asyncio.run(scenario())
    assert created_clients == ["RouteA2Smoke0", "RouteA2Smoke1"]
    assert second_started.is_set() is True
    hand_start = next(
        event
        for event in result["body"]["semantic_projection"]["server_events"]
        if event["type"] == "hand_start"
    )
    assert hand_start["names"] == ["RouteA2Smoke0", "RouteA2Smoke1"]
