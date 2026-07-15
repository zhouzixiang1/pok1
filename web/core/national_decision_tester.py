"""National-native decision fixtures driven through the real TCP boundary.

Formal candidates are probed as isolated TCP clients: a host-owned server
sends delimiter-free official messages, observes the raw wire action, and
validates it with the national parser and validator.

Complex reducer invariants (sticky/split packets, omitted street closers,
terminal opponent evidence, showdown learning, donk and delayed-probe reach)
are checked against the exact system-owned runtime bytes.  Candidate policy
fixtures then exercise the submitted entry over an inherited socket.  Every
fixture has an explicit assertion; coverage-only cases never contribute to the
pass rate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import select
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from managed_bot_executor import (
    BotTiming,
    EndpointLease,
    EndpointLeaseError,
    IsolationUnavailable,
    ManagedExecutorError,
    launch_managed_bot,
)
from bot_artifact import hash_path
from bot_namespace import STRICT_ARTIFACT_FILES
from national_native import NATIVE_BOT_TEMPLATE, NATIVE_ENTRY, check_native_contract


FIXTURE_SCHEMA_VERSION = 1
FIXTURE_PROTOCOL = "official_raw_tcp_transcript_v1"
_ACTION_KEYWORDS = ("fold", "call", "check", "allin")


@dataclass(frozen=True)
class NationalDecisionFixture:
    fixture_id: str
    seat: str
    chunks: tuple[str, ...]
    game_state: dict[str, Any]
    warmup_exchanges: tuple[tuple[tuple[str, ...], str], ...] = ()
    critical: bool = True


POLICY_FIXTURES: tuple[NationalDecisionFixture, ...] = (
    NationalDecisionFixture(
        fixture_id="native_sb_open_fragmented_no_newline",
        seat="auto",
        chunks=("preflop|SMALL", "BLIND|<0,12>", "<1,11>"),
        game_state={
            "stage": "preflop",
            "actions": [],
            "player_chips": 19_950,
            "player_bet": 50,
            "opponent_bet": 100,
            "is_small_blind": True,
            "is_big_blind": False,
            "allin_occurred": False,
            "player_action_count": 0,
        },
    ),
    NationalDecisionFixture(
        fixture_id="native_bb_vs_limp_sticky_no_newline",
        seat="auto",
        chunks=("preflop|BIGBLIND|<0,9><2,8>call",),
        game_state={
            "stage": "preflop",
            "actions": [("call", None)],
            "player_chips": 19_900,
            "player_bet": 100,
            "opponent_bet": 100,
            "is_small_blind": False,
            "is_big_blind": True,
            "allin_occurred": False,
            "player_action_count": 0,
        },
    ),
    NationalDecisionFixture(
        fixture_id="native_bb_vs_raise_split_numeric_no_newline",
        seat="auto",
        chunks=("preflop|BIGBLIND|<3,7><1,6>raise 2", "00"),
        game_state={
            "stage": "preflop",
            "actions": [("raise", 200)],
            "player_chips": 19_900,
            "player_bet": 100,
            "opponent_bet": 200,
            "is_small_blind": False,
            "is_big_blind": True,
            "allin_occurred": False,
            "player_action_count": 0,
        },
    ),
    NationalDecisionFixture(
        fixture_id="native_postflop_facing_check_passes_with_call",
        seat="auto",
        chunks=("checkflop|<2,0><0,9><3,7>check",),
        warmup_exchanges=((
            ("preflop|SMALLBLIND|<0,12><1,11>",),
            "call",
        ),),
        game_state={
            "stage": "flop",
            "actions": [("check", None)],
            "player_chips": 19_900,
            "player_bet": 0,
            "opponent_bet": 0,
            "is_small_blind": True,
            "is_big_blind": False,
            "allin_occurred": False,
            "player_action_count": 0,
        },
    ),
)


class _CaptureSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))


def _load_system_runtime() -> dict[str, Any]:
    """Execute only repository-owned template bytes in a private namespace."""

    namespace: dict[str, Any] = {
        "__name__": "_pok_system_national_fixture_runtime",
        "__file__": str(Path(__file__).resolve()),
    }
    exec(
        compile(
            NATIVE_BOT_TEMPLATE,
            "<system-owned-national-runtime-fixture>",
            "exec",
        ),
        namespace,
        namespace,
    )
    return namespace


def _system_runtime_fixture_results() -> list[dict[str, Any]]:
    """Run assertion-backed reducer fixtures without candidate-owned imports."""

    runtime = _load_system_runtime()
    Bot = runtime["NativeNationalBot"]
    Decoder = runtime["NationalStreamDecoder"]
    results: list[dict[str, Any]] = []

    def record(fixture_id: str, check) -> None:
        try:
            details = check() or {}
            results.append({
                "id": fixture_id,
                "kind": "system_runtime",
                "critical": True,
                "passed": True,
                "details": details,
            })
        except Exception as exc:  # fail closed; surfaced as gate evidence
            results.append({
                "id": fixture_id,
                "kind": "system_runtime",
                "critical": True,
                "passed": False,
                "details": f"{type(exc).__name__}: {str(exc)[:500]}",
            })

    def decoder_case() -> dict[str, Any]:
        decoder = Decoder()
        emitted: list[str] = []
        for chunk in (
            "earnChips -100preflop|BIGBLIND|<0,12><1,11>raise 2",
            "00call",
        ):
            emitted.extend(decoder.feed(chunk))
        emitted.extend(decoder.flush_idle())
        expected = [
            "earnChips -100",
            "preflop|BIGBLIND|<0,12><1,11>",
            "raise 200",
            "call",
        ]
        assert emitted == expected, (emitted, expected)
        assert decoder.buffer == ""
        return {"emitted": emitted}

    def omitted_raise_call_case() -> dict[str, Any]:
        bot = Bot("Fixture", "sb")
        bot._official_action_delay_sec = 0.0
        bot._policy_decision = lambda: {"kind": "raise", "raise_to": 282}
        sock = _CaptureSocket()
        bot.handle("preflop|SMALLBLIND|<0,12><1,11>", sock)
        bot.handle("flop|<2,0><0,9><3,7>", sock)
        preflop = [row for row in bot._history if row["round"] == 0]
        assert [row["action_type"] for row in preflop] == ["raise", "call"]
        assert preflop[-1].get("inferred") is True
        assert preflop[-1].get("inference_boundary") == "street:flop"
        assert preflop[-1].get("committed") == 182
        assert bot._pot == 564
        # The paid closer must be committed before the street bets are reset.
        # Prove the complete next-decision projection, not merely that an
        # inferred history row exists.
        now = time.monotonic()
        context = bot._build_decision_context(
            decision_id=1,
            hard_deadline=now + 1.0,
            refinement_deadline=now + 0.8,
        )
        betting = context["betting"]
        assert betting == {
            "pot": 564,
            "hero_stack": 19_718,
            "opponent_stack": 19_718,
            "effective_stack": 19_718,
            "hero_street_bet": 0,
            "opponent_street_bet": 0,
            "to_call": 0,
            "spr": round(19_718 / 564.0, 6),
            "pot_odds": 0.0,
        }
        inferred = [
            row for row in context["history"]["actions"]
            if row.get("inferred")
        ]
        assert len(inferred) == 1
        assert inferred[0]["committed"] == 182
        # Re-entering a later boundary is a no-op: the socket reducer must not
        # infer or debit the same closing action twice.
        assert bot._infer_suppressed_terminal_opponent_action("duplicate") is None
        assert bot._pot == 564
        snapshot = bot._opponent_tracker.snapshot()
        assert snapshot["facing_raise_by_street"]["preflop"]["call"] == 1
        return {
            "betting": betting,
            "opponent_chips": bot._opponent_chips,
            "inferred_actions": len(inferred),
        }

    def terminal_fold_case() -> dict[str, Any]:
        bot = Bot("Fixture", "sb")
        bot._official_action_delay_sec = 0.0
        decisions = iter((
            {"kind": "raise", "raise_to": 300},
            {"kind": "allin"},
            {"kind": "pass"},
            {"kind": "pass"},
            {"kind": "pass"},
            {"kind": "raise", "raise_to": 200},
        ))
        bot._policy_decision = lambda: next(decisions)
        sock = _CaptureSocket()

        # Explicit terminal fold after ordinary pressure.
        bot.handle("preflop|SMALLBLIND|<0,12><1,11>", sock)
        bot.handle("fold", sock)
        bot.handle("earnChips 150", sock)

        # Explicit terminal fold after a jam must populate fold_to_jam rather
        # than being flattened into the ordinary raise denominator.
        bot.handle("preflop|SMALLBLIND|<0,10><1,10>", sock)
        bot.handle("fold", sock)
        bot.handle("earnChips 100", sock)

        # Drive a complete check-through line from the BB, then raise river.
        # The official EXE suppresses the river closing call, so settlement is
        # the proof boundary for the paid overcall.
        bot.handle("preflop|BIGBLIND|<0,9><1,8>", sock)
        bot.handle("call", sock)
        bot.handle("flop|<2,7><3,5><0,2>", sock)
        bot.handle("turn|<1,4>", sock)
        bot.handle("river|<2,11>", sock)
        bot.handle("earnChips 0", sock)
        bot.handle("oppo_hands|<3,12><3,11>", sock)

        # Start a fourth hand without running candidate policy.  The snapshot
        # below is therefore connection-lived evidence available to the next
        # decision, not a transient prior-hand object.
        bot._send_decision = lambda _sock: None
        bot.handle("preflop|BIGBLIND|<0,7><1,6>", sock)
        now = time.monotonic()
        context = bot._build_decision_context(
            decision_id=1,
            hard_deadline=now + 1.0,
            refinement_deadline=now + 0.8,
        )
        snapshot = context["opponent"]
        terminal = snapshot["terminal_response"]
        assert snapshot["hands_completed"] == 3
        assert terminal["facing_raise_by_street"]["preflop"]["fold"] == 1
        assert terminal["facing_allin_by_street"]["preflop"]["fold"] == 1
        assert terminal["facing_raise_by_street"]["river"]["call"] == 1
        assert terminal["river_overcall"] > 0.55
        assert snapshot["river_overcall_samples"] == 1
        assert snapshot["fold_to_jam_samples"] == 1
        assert snapshot["recent_hands"][-1]["showdown"] is True
        assert snapshot["showdown_range"]["samples"] == 1
        assert snapshot["showdown_range"]["selection_scope"] == "reached_showdown_only"
        assert 0.0 < snapshot["showdown_range"]["adaptation_weight"] <= 0.65
        return {
            "terminal_response": terminal,
            "showdown_range": snapshot["showdown_range"],
            "hands_completed": snapshot["hands_completed"],
        }

    def showdown_learning_case() -> dict[str, Any]:
        bot = Bot("Fixture", "bb")
        bot._hand_num = 1
        bot._stage = "river"
        bot._my_cards = [(0, 0), (1, 0)]
        bot._public_cards = [(2, 0), (3, 0), (0, 1), (1, 1), (2, 1)]
        bot._opponent_tracker.begin_hand(1, opponent_is_sb=True)
        bot.handle("earnChips -300", None)
        bot.handle("oppo_hands|<0,12><1,12>", None)
        snapshot = bot._opponent_tracker.snapshot()
        assert snapshot["showdowns"] == 1
        assert snapshot["showdown_range"]["samples"] == 1
        assert snapshot["showdown_range"]["bucket_counts"]["premium_pair"] == 1
        return {"showdown_range": snapshot["showdown_range"]}

    def donk_delayed_probe_case() -> dict[str, Any]:
        bot = Bot("Fixture", "bb")
        bot._official_action_delay_sec = 0.0
        captured: list[dict[str, Any]] = []
        actions = iter((
            {"kind": "pass"},
            {"kind": "pass"},
            {"kind": "pass"},
        ))

        def decide() -> dict:
            now = time.monotonic()
            captured.append(bot._build_decision_context(
                decision_id=len(captured) + 1,
                hard_deadline=now + 1.0,
                refinement_deadline=now + 0.8,
            ))
            return next(actions)

        bot._policy_decision = decide
        sock = _CaptureSocket()
        bot.handle("preflop|BIGBLIND|<0,12><1,11>", sock)
        bot.handle("raise 300", sock)
        bot.handle("flop|<2,0><0,9><3,7>", sock)
        flop_context = captured[-1]
        flop_line = flop_context["line"]
        assert flop_line["can_donk"] is True
        assert flop_context["betting"]["pot"] == 600
        bot.handle("turn|<1,3>", sock)
        turn_line = captured[-1]["line"]
        assert turn_line["can_delayed_probe"] is True
        assert turn_line["previous_street"]["opponent_checked_back"] is True
        return {
            "flop_tags": flop_line["line_tags"],
            "turn_tags": turn_line["line_tags"],
        }

    record("runtime_sticky_split_no_newline", decoder_case)
    record("runtime_omitted_street_call_repairs_pot", omitted_raise_call_case)
    record("runtime_terminal_fold_persists_cross_hand", terminal_fold_case)
    record("runtime_showdown_updates_range", showdown_learning_case)
    record("runtime_donk_and_delayed_probe_reachable", donk_delayed_probe_case)
    return results


def _recv_until_idle(sock: socket.socket, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        wait = min(0.10 if data else 0.25, max(0.0, deadline - time.monotonic()))
        ready, _, _ = select.select([sock], [], [], wait)
        if not ready:
            if data:
                break
            continue
        chunk = sock.recv(256)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 256:
            raise RuntimeError("wire action exceeded 256 bytes")
        token = data.decode("utf-8", errors="strict")
        if token in _ACTION_KEYWORDS:
            break
    if not data:
        raise TimeoutError(f"no wire action within {timeout:g}s")
    return data.decode("utf-8", errors="strict")


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)


def _run_policy_fixture(
    bot_dir: Path,
    fixture: NationalDecisionFixture,
) -> dict[str, Any]:
    """Observe one candidate decision from a host-owned national TCP server."""

    # Import by package so the same parser/validator used by the local national
    # server defines legality; only canonical wire actions enter this path.
    from sever.engine.validator import validate_action
    from sever.server.protocol import parse_action

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()[:2]
    server_sock: socket.socket | None = None
    managed = None
    stdout_file = tempfile.TemporaryFile(mode="w+b")
    stderr_file = tempfile.TemporaryFile(mode="w+b")
    try:
        with EndpointLease.connect(str(host), int(port), timeout=2.0) as endpoint:
            server_sock, _ = listener.accept()
            server_sock.settimeout(3.0)
            managed = launch_managed_bot(
                bot_dir,
                endpoint,
                entry_relative=NATIVE_ENTRY,
                name="NationalFixture",
                seat=fixture.seat,
                seed=0xC0DEC,
                timing=BotTiming(
                    action_delay=0.0,
                    hard_deadline=1.5,
                    refinement_budget=1.25,
                    baseline_target=0.10,
                ),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
                host_process_owner="national-decision-fixture",
                expected_artifact_hash=hash_path(bot_dir),
                required_artifact_files=tuple(sorted(STRICT_ARTIFACT_FILES)),
            )
        server_sock.sendall(b"name")
        observed_name = _recv_until_idle(server_sock, timeout=3.0)
        if observed_name != "NationalFixture":
            raise RuntimeError(f"name handshake mismatch: {observed_name!r}")
        for warmup_chunks, expected_action in fixture.warmup_exchanges:
            for index, chunk in enumerate(warmup_chunks):
                server_sock.sendall(chunk.encode("utf-8"))
                if index + 1 < len(warmup_chunks):
                    time.sleep(0.02)
            warmup_action = _recv_until_idle(server_sock, timeout=3.0)
            if warmup_action != expected_action:
                raise AssertionError(
                    f"warmup action mismatch: {warmup_action!r} != {expected_action!r}"
                )
        for index, chunk in enumerate(fixture.chunks):
            server_sock.sendall(chunk.encode("utf-8"))
            if index + 1 < len(fixture.chunks):
                time.sleep(0.02)
        action_raw = _recv_until_idle(server_sock, timeout=3.0)
        action_type, action_amount = parse_action(action_raw)
        legal, reason = validate_action(
            action_type,
            action_amount,
            dict(fixture.game_state),
        )
        if not legal:
            raise AssertionError(
                f"illegal national wire action {action_raw!r}: {reason}"
            )
        if action_type == "unknown" or action_raw.strip() != action_raw:
            raise AssertionError(f"non-canonical national wire action: {action_raw!r}")
        return {
            "id": fixture.fixture_id,
            "kind": "candidate_policy_wire",
            "critical": fixture.critical,
            "passed": True,
            "action": action_raw,
            "action_type": action_type,
            "action_amount": action_amount,
            "fixture": asdict(fixture),
            "isolation": asdict(managed.isolation),
        }
    except (IsolationUnavailable, EndpointLeaseError):
        # These are host-owned isolation/descriptor failures.  Let the quality
        # gate's infrastructure retry overlay decide whether to retry or
        # abandon; they are not evidence against candidate policy quality.
        raise
    except ManagedExecutorError:
        # Keep the broader managed-executor contract fail closed as host
        # infrastructure too.  The two specific subclasses above are split
        # out deliberately so this boundary remains explicit during review.
        raise
    except Exception as exc:
        return {
            "id": fixture.fixture_id,
            "kind": "candidate_policy_wire",
            "critical": fixture.critical,
            "passed": False,
            "details": f"{type(exc).__name__}: {str(exc)[:500]}",
            "fixture": asdict(fixture),
        }
    finally:
        if server_sock is not None:
            try:
                server_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            server_sock.close()
        listener.close()
        _terminate(managed.process if managed is not None else None)
        stdout_file.close()
        stderr_file.close()


def _failure_projection(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": result.get("id", "unknown"),
        "severity": "critical" if result.get("critical") else "normal",
        "details": result.get("details") or "national fixture assertion failed",
        "kind": result.get("kind"),
    }


def run_national_decision_tests(
    bot_dir: str | Path,
    *,
    policy_fixtures: Iterable[NationalDecisionFixture] | None = None,
) -> dict[str, Any]:
    """Run national-only, assertion-backed runtime and policy fixtures."""

    root = Path(bot_dir).resolve()
    contract_errors = check_native_contract(
        root,
        require_current_stream_decoder=True,
        require_current_decision_runtime=True,
    )
    if contract_errors:
        failure = {
            "id": "national_native_contract",
            "kind": "candidate_contract",
            "critical": True,
            "passed": False,
            "details": "; ".join(contract_errors[:5]),
        }
        scenarios = [failure]
    else:
        scenarios = _system_runtime_fixture_results()
        scenarios.extend(
            _run_policy_fixture(root, fixture)
            for fixture in (tuple(policy_fixtures) if policy_fixtures is not None else POLICY_FIXTURES)
        )

    failures = [_failure_projection(item) for item in scenarios if not item.get("passed")]
    critical_failures = [
        item for item in failures if item.get("severity") == "critical"
    ]
    total = len(scenarios)
    passed = sum(1 for item in scenarios if item.get("passed") is True)
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "protocol": FIXTURE_PROTOCOL,
        "pass_rate": passed / total if total else 0.0,
        "passed": passed,
        "total": total,
        "critical_passed": total - len(critical_failures),
        "critical_total": total,
        "critical_failures": critical_failures,
        "failures": failures,
        "scenarios": scenarios,
        "coverage_only_count": 0,
        "external_scenario_sidecars_loaded": False,
    }


__all__ = [
    "FIXTURE_PROTOCOL",
    "FIXTURE_SCHEMA_VERSION",
    "NationalDecisionFixture",
    "POLICY_FIXTURES",
    "run_national_decision_tests",
]
