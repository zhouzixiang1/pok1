import asyncio
import importlib.util
import json
from pathlib import Path
import runpy
import sys
import time

import pytest

from candidate_hygiene import sanitize_candidate_dir
import evolution_infra
import national_native
from national_native import (
    check_native_contract,
    check_native_stream_decoder,
    ensure_native_entry,
    _completed_active_bots,
    run_legacy_debug_tcp_pair_with_wrappers,
    run_native_tcp_smoke,
    run_native_tcp_pair,
)
from pipeline_state import route_policy
from tool_helpers import _quality_gate_ok
from workflow_profiles import get_workflow_profile, profile_summary


def _write_minimal_strategy_bot(bot_dir: Path) -> None:
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "main.py").write_text(
        "def sanitize_action(action, state, my_chips):\n"
        "    return int(action)\n",
        encoding="utf-8",
    )
    (bot_dir / "state.py").write_text(
        "def infer_remaining_hands_from_requests(requests):\n"
        "    return max(0, 70 - len(requests))\n\n"
        "def reconstruct_state(req):\n"
        "    return dict(req)\n",
        encoding="utf-8",
    )
    (bot_dir / "strategy.py").write_text(
        "def get_action(req, requests):\n"
        "    return 0\n",
        encoding="utf-8",
    )


def _passing_architecture_transition(*_args, **_kwargs):
    capabilities = {
        "schema_version": 2,
        "detector_version": "test",
        "ok": True,
        "required_failures": [],
        "advisory_warnings": [],
        "checks": [],
        "checks_by_id": {},
    }
    return {
        "schema_version": 1,
        "policy_version": "test",
        "ok": True,
        "policy": {},
        "policy_identity_errors": [],
        "regressions": [],
        "selected_focus": None,
        "unresolved_focus_checks": [],
        "source_capabilities": capabilities,
        "candidate_capabilities": capabilities,
    }
def _load_native_entry_module(bot_dir: Path, monkeypatch):
    for module_name in ("main", "state", "strategy", "native_entry_probe"):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.syspath_prepend(str(bot_dir))
    spec = importlib.util.spec_from_file_location("native_entry_probe", bot_dir / "national_bot.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["native_entry_probe"] = module
    spec.loader.exec_module(module)
    return module


def test_national_native_is_default_profile(monkeypatch):
    monkeypatch.delenv("POK_WORKFLOW_PROFILE", raising=False)

    profile = get_workflow_profile()

    assert profile.profile_id == "national_native"
    assert profile.evaluation_protocol == "national"
    assert profile.rating_protocol == "national"
    assert profile.national_execution_mode == "native_tcp"
    assert profile.national_acceptance_hands == 70
    assert "native_tcp" in profile.focus_skill_layers
    assert "national_execution_mode=native_tcp" in profile_summary(profile)


def test_web_launcher_defaults_to_national_native():
    launcher = Path(__file__).resolve().parents[1] / "main.py"

    assert 'setdefault("POK_WORKFLOW_PROFILE", "national_native")' in launcher.read_text(encoding="utf-8")


def test_completed_active_bots_uses_authoritative_active_pool(monkeypatch, tmp_path):
    bots_dir = tmp_path / "bots"
    active = bots_dir / "national_v110"
    stale_completed = bots_dir / "national_v88"
    active.mkdir(parents=True)
    stale_completed.mkdir(parents=True)
    (active / "main.py").write_text("# active\n", encoding="utf-8")
    (active / "national_bot.py").write_text("# native\n", encoding="utf-8")
    (stale_completed / "main.py").write_text("# stale\n", encoding="utf-8")
    (stale_completed / ".completed").write_text("", encoding="utf-8")

    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: ["national_v110"])

    assert _completed_active_bots() == [("national_v110", active.resolve())]


def test_native_resolver_accepts_self_contained_formal_entry_without_main(tmp_path):
    bot_dir = tmp_path / "NativeOnly"
    bot_dir.mkdir()
    (bot_dir / "national_bot.py").write_text("# formal native entry\n", encoding="utf-8")

    assert national_native.resolve_bot(bot_dir) == ("NativeOnly", bot_dir.resolve())
    assert national_native.resolve_bot(bot_dir / "national_bot.py") == (
        "NativeOnly",
        bot_dir.resolve(),
    )


def test_native_entry_contract_allows_template_and_rejects_legacy_tokens(tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)

    entry = ensure_native_entry(bot_dir)

    assert entry.name == "national_bot.py"
    assert check_native_contract(bot_dir) == []
    text = entry.read_text(encoding="utf-8")
    assert "bot_adapter" not in text
    assert '"response"' not in text
    assert "sock.recv" in text
    assert "_split_messages" in text
    assert "POK_OFFICIAL_ACTION_DELAY" in text
    assert "_send_wire_action" in text
    assert "makefile(" not in text
    assert ".readline(" not in text
    assert "msg + \"\\n\"" not in text
    assert "NATIONAL_STREAM_DECODER_VERSION = 2" in text
    assert "NATIONAL_DECISION_RUNTIME_VERSION = 5" in text
    assert 'os.environ["POK_NATIVE_BOT_SEED"]' in text
    assert "random.seed(int(random_seed))" in text
    assert "def _reap_retired_strategy_workers" in text
    assert '_stop_strategy_worker("decision_deadline", wait=False)' in text
    assert "def _strategy_worker_main" in text
    assert "process.terminate()" in text
    assert "iter_refinements" in text
    assert check_native_stream_decoder(bot_dir) == []
    assert check_native_contract(
        bot_dir,
        require_current_decision_runtime=True,
    ) == []

    entry.write_text(
        "from sever.bot_adapter import BotAdapter\n"
        "print({'response': 0})\n",
        encoding="utf-8",
    )

    errors = check_native_contract(bot_dir)
    assert any("bot_adapter" in err or "BotAdapter" in err for err in errors)
    assert any("'response'" in err for err in errors)


def test_native_strategy_worker_receives_explicit_replay_seed(
    tmp_path, monkeypatch
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    entry = ensure_native_entry(bot_dir)
    monkeypatch.setenv("POK_NATIVE_BOT_SEED", "4321")
    namespace = runpy.run_path(str(entry), run_name="_native_seed_probe")
    bot = namespace["NativeNationalBot"]("BotA")
    captured = {}

    class Connection:
        def close(self):
            pass

    class Process:
        pid = 9876
        exitcode = None

        def __init__(self, *, target, args, name, daemon):
            captured.update(target=target, args=args, name=name, daemon=daemon)

        def start(self):
            captured["started"] = True

        def is_alive(self):
            return True

    class Context:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is True
            return Connection(), Connection()

        @staticmethod
        def Process(**kwargs):
            return Process(**kwargs)

    bot._mp_context = Context()

    assert bot._ensure_strategy_worker() is True
    assert captured["args"][2] == 4321
    assert bot._strategy_worker_seed == 4321
    assert captured["started"] is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("raise 200", ["raise 200"]),
        ("earnChips -100", ["earnChips -100"]),
        ("raise 200call", ["raise 200", "call"]),
        (
            "earnChips -100preflop|SMALLBLIND|<0,3><1,3>",
            ["earnChips -100", "preflop|SMALLBLIND|<0,3><1,3>"],
        ),
    ],
)
def test_native_stream_decoder_handles_every_byte_split(
    monkeypatch,
    tmp_path,
    raw,
    expected,
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    module = _load_native_entry_module(bot_dir, monkeypatch)

    chunkings = [(raw[:split], raw[split:]) for split in range(1, len(raw))]
    chunkings.append(tuple(raw))
    for chunks in chunkings:
        decoder = module.NationalStreamDecoder()
        messages = []
        for chunk in chunks:
            messages.extend(decoder.feed(chunk))
        messages.extend(decoder.flush_idle())

        assert messages == expected, chunks
        assert decoder.buffer == "", chunks


def test_numeric_stream_tail_waits_for_idle_or_following_token(monkeypatch, tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    module = _load_native_entry_module(bot_dir, monkeypatch)
    decoder = module.NationalStreamDecoder()

    assert decoder.feed("raise 2") == []
    assert decoder.has_pending_numeric is True
    assert decoder.feed("00call") == ["raise 200", "call"]
    assert decoder.has_pending_numeric is False

    assert decoder.feed("earnChips -100") == []
    assert decoder.flush_idle() == ["earnChips -100"]
    assert decoder.buffer == ""


def test_decision_runtime_kills_timed_out_worker_and_restarts_next_decision(
    monkeypatch,
    tmp_path,
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    (bot_dir / "strategy.py").write_text(
        "import time\n"
        "def get_action(req, requests):\n"
        "    time.sleep(0.4)\n"
        "    return 99\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    monkeypatch.setenv("POK_DECISION_HARD_DEADLINE_SEC", "0.05")
    monkeypatch.setenv("POK_DECISION_REFINEMENT_BUDGET_SEC", "0.04")
    monkeypatch.setenv("POK_DECISION_BASELINE_TARGET_SEC", "0.01")
    module = _load_native_entry_module(bot_dir, monkeypatch)
    bot = module.NativeNationalBot("BotA")

    started = time.monotonic()
    first = bot._strategy_action()
    first_elapsed = time.monotonic() - started
    first_source = bot._last_decision_source
    started = time.monotonic()
    second = bot._strategy_action()
    second_elapsed = time.monotonic() - started

    assert first == 0
    assert first_elapsed < 0.25
    assert bot._strategy_worker_alive() is False
    assert first_source == "refinement_deadline_latest_safe"
    assert second == 0
    assert second_elapsed < 0.25
    assert bot._last_decision_source == "refinement_deadline_latest_safe"
    assert bot._strategy_worker_generation == 2


def test_decision_runtime_preserves_strategy_baseline_when_refinement_times_out(
    monkeypatch,
    tmp_path,
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    (bot_dir / "strategy.py").write_text(
        "import time\n"
        "def get_action(req, requests): return -1\n"
        "def get_baseline_action(req, requests): return 7\n"
        "def refine_action(req, requests, baseline, deadline):\n"
        "    time.sleep(0.4)\n"
        "    return 99\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    monkeypatch.setenv("POK_DECISION_HARD_DEADLINE_SEC", "0.08")
    monkeypatch.setenv("POK_DECISION_REFINEMENT_BUDGET_SEC", "0.07")
    monkeypatch.setenv("POK_DECISION_BASELINE_TARGET_SEC", "0.01")
    module = _load_native_entry_module(bot_dir, monkeypatch)
    bot = module.NativeNationalBot("BotA")

    started = time.monotonic()
    action = bot._strategy_action()

    assert action == 7
    assert time.monotonic() - started < 0.3
    assert bot._strategy_worker_alive() is False
    assert bot._last_decision_source == "refinement_deadline_latest_safe"


def test_decision_runtime_discards_result_whose_sanitizer_crosses_deadline(
    monkeypatch,
    tmp_path,
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    (bot_dir / "strategy.py").write_text(
        "import time\n"
        "def get_action(req, requests):\n"
        "    time.sleep(0.025)\n"
        "    return 99\n",
        encoding="utf-8",
    )
    (bot_dir / "main.py").write_text(
        "import time\n"
        "def sanitize_action(action, state, chips):\n"
        "    if action == 99: time.sleep(0.04)\n"
        "    return int(action)\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    monkeypatch.setenv("POK_DECISION_HARD_DEADLINE_SEC", "0.05")
    monkeypatch.setenv("POK_DECISION_REFINEMENT_BUDGET_SEC", "0.04")
    monkeypatch.setenv("POK_DECISION_BASELINE_TARGET_SEC", "0.01")
    module = _load_native_entry_module(bot_dir, monkeypatch)
    bot = module.NativeNationalBot("BotA")

    started = time.monotonic()
    action = bot._strategy_action()

    assert action == 0
    assert time.monotonic() - started < 0.09
    assert bot._last_decision_source == "refinement_deadline_latest_safe"
    assert bot._strategy_worker_alive() is False
    assert len(bot._retired_strategy_processes) <= 1
    bot.close()
    assert bot._retired_strategy_processes == []


def test_decision_runtime_consumes_incremental_candidates_and_reuses_worker(
    monkeypatch,
    tmp_path,
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    (bot_dir / "strategy.py").write_text(
        "def get_action(req, requests): return -1\n"
        "def get_baseline_action(req, requests): return -1\n"
        "def iter_refinements(req, requests, baseline, deadline):\n"
        "    yield {'action': 200, 'sample_count': 32, 'confidence': 0.6}\n"
        "    yield {'action': 400, 'sample_count': 64, 'confidence': 0.8, 'complete': True}\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    monkeypatch.setenv("POK_DECISION_HARD_DEADLINE_SEC", "0.30")
    monkeypatch.setenv("POK_DECISION_REFINEMENT_BUDGET_SEC", "0.25")
    monkeypatch.setenv("POK_DECISION_BASELINE_TARGET_SEC", "0.10")
    module = _load_native_entry_module(bot_dir, monkeypatch)
    bot = module.NativeNationalBot("BotA")

    first = bot._strategy_action()
    worker_pid = bot._strategy_process.pid
    first_metrics = dict(bot._last_decision_metrics)
    second = bot._strategy_action()

    assert first == 400
    assert second == 400
    assert bot._strategy_process.pid == worker_pid
    assert first_metrics["baseline_target_met"] is True
    assert first_metrics["refinement_messages"] == 2
    assert first_metrics["latest_sequence"] == 3
    assert first_metrics["completed"] is True
    assert first_metrics["timed_out"] is False
    bot.close()


@pytest.mark.parametrize(
    ("my_stage_bet", "opponent_stage_bet", "expected", "wire"),
    [
        (0, 500, -1, "fold"),
        (100, 300, -1, "fold"),
        (0, 0, 0, "check"),
    ],
)
def test_socket_fallback_is_risk_safe_before_strategy_runs(
    monkeypatch,
    tmp_path,
    my_stage_bet,
    opponent_stage_bet,
    expected,
    wire,
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    module = _load_native_entry_module(bot_dir, monkeypatch)
    bot = module.NativeNationalBot("BotA")
    bot._stage = "flop"
    bot._my_stage_bet = my_stage_bet
    bot._opponent_stage_bet = opponent_stage_bet

    fallback = bot._socket_safe_fallback_action()

    assert fallback == expected
    assert bot._action_to_tcp(fallback)[0] == wire


def test_opponent_tracker_keeps_terminal_response_and_bayesian_confidence(
    monkeypatch,
    tmp_path,
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    module = _load_native_entry_module(bot_dir, monkeypatch)
    tracker = module.OpponentTracker()

    tracker.begin_hand(1, opponent_is_sb=False)
    tracker.observe_action("hero", "river", "raise", amount=800)
    tracker.observe_action("opponent", "river", "fold")
    tracker.observe_settlement(1, hero_earned=250)
    snapshot = tracker.snapshot()

    assert snapshot["hands_completed"] == 1
    assert snapshot["samples"]["fold_to_raise"] == 1
    assert snapshot["rates"]["fold_to_raise"] < 1.0
    assert snapshot["schema_version"] == 3
    for field in (
        "vpip",
        "pfr",
        "allin_rate",
        "postflop_aggr",
        "postflop_check_rate",
        "fold_to_raise",
        "aggression",
        "avg_raise_bb",
        "raise_samples",
        "flop_aggr",
        "turn_aggr",
        "river_aggr",
    ):
        assert field in snapshot
    assert snapshot["confidence"] < 0.1
    assert snapshot["adaptation_weight"] <= module.OPPONENT_ADAPTATION_CAP
    assert snapshot["recent_hands"][-1]["last_opponent_action"] == "fold"


def test_opponent_tracker_uses_hand_pfr_and_isolates_context_confidence(monkeypatch, tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    module = _load_native_entry_module(bot_dir, monkeypatch)
    tracker = module.OpponentTracker()

    for hand in range(1, 21):
        tracker.begin_hand(hand, opponent_is_sb=False)
        tracker.observe_action("opponent", "preflop", "raise", amount=300)
        tracker.observe_action("hero", "preflop", "raise", amount=700)
        tracker.observe_action("opponent", "preflop", "raise", amount=1500)
        tracker.observe_settlement(hand, hero_earned=0)
    tracker.begin_hand(21, opponent_is_sb=True)
    tracker.observe_action("hero", "river", "raise", amount=1200)
    tracker.observe_action("opponent", "river", "fold", amount=3000)
    tracker.observe_showdown(21, [0, 1], [4, 8, 12, 16, 20])
    tracker.observe_settlement(21, hero_earned=100)

    snapshot = tracker.snapshot()
    preflop = [row for key, row in snapshot["contexts"].items() if key.startswith("preflop|")]
    river = [row for key, row in snapshot["contexts"].items() if key.startswith("river|")]

    assert snapshot["pfr"] <= 1.0
    assert sum(row["samples"] for row in preflop) == 40
    assert sum(row["samples"] for row in river) == 1
    assert max(row["confidence"] for row in preflop) > max(row["confidence"] for row in river)
    assert max(row["adaptation_weight"] for row in river) < module.OPPONENT_ADAPTATION_CAP
    assert snapshot["showdown_hole_classes"]["pair"] == 1


@pytest.mark.parametrize("showdown_first", [True, False])
def test_showdown_and_settlement_are_hand_keyed_in_either_order(
    monkeypatch,
    tmp_path,
    showdown_first,
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    module = _load_native_entry_module(bot_dir, monkeypatch)
    bot = module.NativeNationalBot("Probe")
    bot._hand_num = 1
    bot._my_cards = [0, 1]
    bot._public_cards = [2, 3, 4, 5, 6]
    bot._opponent_tracker.begin_hand(1, opponent_is_sb=True)

    messages = ["oppo_hands|<0,3><1,3>", "earnChips -100"]
    if not showdown_first:
        messages.reverse()
    for message in messages:
        bot.handle(message, None)
    bot.handle("oppo_hands|<0,3><1,3>", None)
    bot._opponent_tracker.observe_settlement(1, hero_earned=-100)

    assert len(bot._showdowns) == 1
    assert bot._showdowns[0]["earned"] == -100
    snapshot = bot._opponent_tracker.snapshot()
    assert snapshot["hands_completed"] == 1
    assert snapshot["showdowns"] == 1
    assert snapshot["recent_hands"][-1]["showdown"] is True


def test_opponent_tracker_state_is_bounded_and_injected_into_request(monkeypatch, tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    module = _load_native_entry_module(bot_dir, monkeypatch)
    bot = module.NativeNationalBot("Probe")

    for hand in range(1, 71):
        bot._opponent_tracker.begin_hand(hand, opponent_is_sb=bool(hand % 2))
        bot._opponent_tracker.observe_action("opponent", "preflop", "call")
        bot._opponent_tracker.observe_settlement(hand, hero_earned=50 if hand % 2 else -50)
    request = bot._request()
    snapshot = request["opponent_runtime"]

    assert snapshot["hands_completed"] == 70
    assert snapshot["total_actions"] == 70
    assert len(snapshot["recent_hands"]) == 8
    assert snapshot["samples"]["preflop_vpip"] == 70
    assert 0.0 < snapshot["confidence"] < 1.0


def test_candidate_requires_current_stream_decoder_without_rejecting_grandfathered_bot(
    tmp_path,
):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    entry = ensure_native_entry(bot_dir)
    immediate_numeric = entry.read_text(encoding="utf-8").replace(
        "        if match.end() == len(buffer) and not flush_numeric:\n"
        "            return None, buffer\n",
        "",
    )
    entry.write_text(immediate_numeric, encoding="utf-8")

    assert check_native_contract(bot_dir) == []
    strict_errors = check_native_contract(
        bot_dir,
        require_current_stream_decoder=True,
    )
    assert any("stream decoder behavior violation" in error for error in strict_errors)

    result = sanitize_candidate_dir(bot_dir, require_native_tcp=True)
    assert result["native_entry_refreshed"] is True
    assert check_native_contract(
        bot_dir,
        require_current_stream_decoder=True,
    ) == []


def test_native_entry_contract_rejects_legacy_newline_protocol(tmp_path):
    bot_dir = tmp_path / "BotA"
    bot_dir.mkdir()
    (bot_dir / "national_bot.py").write_text(
        "import socket\n\n"
        "def main(sock):\n"
        "    stream = sock.makefile('r', encoding='utf-8', newline='\\n')\n"
        "    line = stream.readline()\n"
        "    msg = 'fold'\n"
        "    sock.sendall((msg + '\\n').encode('utf-8'))\n"
        "# required wire tokens: raise fold call check allin\n",
        encoding="utf-8",
    )

    errors = check_native_contract(bot_dir)

    assert any("legacy newline TCP token" in err for err in errors)


def test_native_entry_contract_rejects_sanitizer_exception_pass(tmp_path):
    bot_dir = tmp_path / "BotA"
    bot_dir.mkdir()
    (bot_dir / "national_bot.py").write_text(
        "import socket\n\n"
        "class NativeNationalBot:\n"
        "    def _strategy_action(self):\n"
        "        action = 250\n"
        "        try:\n"
        "            action = self.sanitize_action(action, {}, 20000)\n"
        "        except Exception:\n"
        "            pass\n"
        "        return int(action)\n\n"
        "# required wire tokens: raise fold call check allin\n",
        encoding="utf-8",
    )

    errors = check_native_contract(bot_dir)

    assert any("sanitizer failure" in err for err in errors)


def test_native_entry_contract_rejects_increment_raise_semantics(tmp_path):
    bot_dir = tmp_path / "BotA"
    bot_dir.mkdir()
    (bot_dir / "national_bot.py").write_text(
        "import socket\n\n"
        "def bad(self, amount, needed, action, sock):\n"
        "    sock.recv(1)\n"
        "    committed = min(max(0, amount), self._opponent_chips)\n"
        "    return f\"raise {needed}\", \"raise\", action\n"
        "# required wire tokens: raise fold call check allin\n",
        encoding="utf-8",
    )

    errors = check_native_contract(bot_dir)

    assert any("raise-to-total" in err for err in errors)


def test_native_entry_template_uses_raise_to_total(monkeypatch, tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    module = _load_native_entry_module(bot_dir, monkeypatch)

    bot = module.NativeNationalBot("Probe")
    bot._stage = "preflop"
    bot._my_stage_bet = 50
    bot._opponent_stage_bet = 100
    bot._opponent_chips = 19900

    committed = bot._apply_opponent_action("raise", 200)

    assert committed == 100
    assert bot._opponent_stage_bet == 200
    assert bot._opponent_chips == 19800

    bot = module.NativeNationalBot("Probe")
    bot._stage = "flop"
    bot._my_stage_bet = 100
    bot._my_chips = 19900
    bot._opponent_stage_bet = 200
    bot._history = [{
        "round": 1,
        "player_id": bot._opponent_id,
        "action_type": "raise",
        "action": 200,
        "stage_bet": 200,
    }]

    assert bot._action_to_tcp(300) == ("raise 401", "raise", 401)


def test_native_entry_template_throttles_official_action_send(monkeypatch, tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    module = _load_native_entry_module(bot_dir, monkeypatch)
    sleeps: list[float] = []

    class FakeSock:
        sent: list[bytes] = []

        def sendall(self, payload):
            self.sent.append(payload)

    monkeypatch.setenv("POK_OFFICIAL_ACTION_DELAY", "0.30")
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    bot = module.NativeNationalBot("Probe")
    bot._last_platform_message_at = module.time.perf_counter()
    sock = FakeSock()

    bot._send_wire_action(sock, "call")

    assert sock.sent == [b"call"]
    assert sleeps and 0 < sleeps[0] <= 0.30


def test_candidate_hygiene_removes_completion_and_restores_native_entry(tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    (bot_dir / ".completed").write_text("parent sentinel", encoding="utf-8")
    entry = ensure_native_entry(bot_dir)
    entry.unlink()

    result = sanitize_candidate_dir(bot_dir, require_native_tcp=True)

    assert result["completed_removed"] is True
    assert result["native_entry"] == "national_bot.py"
    assert not (bot_dir / ".completed").exists()
    assert (bot_dir / "national_bot.py").exists()
    assert check_native_contract(bot_dir) == []


def test_candidate_hygiene_overwrites_legacy_native_entry_for_new_candidate(tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    (bot_dir / "national_bot.py").write_text(
        "import socket\n\n"
        "def main(sock):\n"
        "    stream = sock.makefile('r', encoding='utf-8', newline='\\n')\n"
        "    line = stream.readline()\n"
        "    msg = 'fold'\n"
        "    sock.sendall((msg + '\\n').encode('utf-8'))\n"
        "# required wire tokens: raise fold call check allin\n",
        encoding="utf-8",
    )

    result = sanitize_candidate_dir(
        bot_dir,
        require_native_tcp=True,
        overwrite_native_entry=True,
    )
    text = (bot_dir / "national_bot.py").read_text(encoding="utf-8")

    assert result["native_entry"] == "national_bot.py"
    assert check_native_contract(bot_dir) == []
    assert "sock.recv" in text
    assert ".readline(" not in text


def test_native_entry_contract_rejects_missing_round_allin_guard(tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    (bot_dir / "national_bot.py").write_text(
        "import socket\n\n"
        "def _split_messages(buffer):\n"
        "    return [], buffer\n\n"
        "def probe(sock):\n"
        "    sock.recv(1)\n\n"
        "class NativeNationalBot:\n"
        "    def _responding_to_check(self):\n"
        "        return False\n"
        "    def _zero_action(self):\n"
        "        if self._responding_to_check():\n"
        "            return 'call', 'call', None\n"
        "        return 'check', 'check', None\n"
        "    def _action_to_tcp(self, action):\n"
        "        if action == -1:\n"
        "            return 'fold', 'fold', None\n"
        "        if action == -2:\n"
        "            return 'allin', 'allin', None\n"
        "        if action > 0:\n"
        "            return f'raise {action}', 'raise', action\n"
        "        return self._zero_action()\n",
        encoding="utf-8",
    )

    errors = check_native_contract(bot_dir)

    assert any("current-round allin guard" in err for err in errors)


def test_candidate_hygiene_refreshes_stale_native_entry_without_explicit_overwrite(tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)
    ensure_native_entry(bot_dir)
    entry = bot_dir / "national_bot.py"
    stale = entry.read_text(encoding="utf-8").replace(
        "            if self._current_round_has_allin():\n"
        "                return self._zero_action()\n",
        "",
    )
    entry.write_text(stale, encoding="utf-8")
    assert any("current-round allin guard" in err for err in check_native_contract(bot_dir))

    result = sanitize_candidate_dir(bot_dir, require_native_tcp=True)

    text = entry.read_text(encoding="utf-8")
    assert result["native_entry"] == "national_bot.py"
    assert result["native_entry_refreshed"] is True
    assert any("current-round allin guard" in err for err in result["native_entry_contract_errors"])
    assert check_native_contract(bot_dir) == []
    assert "if self._current_round_has_allin():" in text


def test_quality_gate_ok_rejects_adapter_cache_under_native_profile(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")

    old_adapter_checkpoint = {
        "workflow_profile_id": "national_primary",
        "national_execution_mode": "adapter",
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_primary",
                "national_execution_mode": "adapter",
            }
        },
    }
    native_checkpoint = {
        "workflow_profile_id": "national_native",
        "national_execution_mode": "native_tcp",
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
            }
        },
    }

    assert _quality_gate_ok(old_adapter_checkpoint) is False
    assert _quality_gate_ok(native_checkpoint) is True


def test_route_policy_revalidates_old_adapter_quality_under_native_profile(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")

    route = route_policy({
        "stage": "reviewed",
        "next_v": 272,
        "source_v": 187,
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_primary",
                "national_execution_mode": "adapter",
            },
            "review": {"approved": True},
        },
    })

    assert route["next_tool"] == "run_quality_gates"
    assert route["intent"] == "quality_profile_refresh"


def test_native_tcp_pair_runs_without_adapter(tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)
    ensure_native_entry(bot_b)

    result = asyncio.run(run_native_tcp_pair(
        bot_a,
        bot_b,
        hands=2,
        deck_seed_base=1234,
        timeout_sec=30,
    ))

    assert result["execution_mode"] == "native_tcp"
    assert result["hands_played"] == 2
    assert len(result["settlements"]) == 2
    assert all(len(row["earnings"]) == 2 for row in result["settlements"])
    assert result["passed_compliance"] is True
    assert result["issues"] == []
    assert result["wrapper_used"] is False
    assert result["wrapper_used_by_player"] == {"BotA": False, "BotB": False}
    assert all(row["wrapper_used"] is False for row in result["per_player"].values())
    assert all(
        row["adapter"]["actions_sent"] == 0
        for row in result["per_player"].values()
    )


def test_native_tcp_pair_owns_and_releases_shared_capacity(tmp_path, monkeypatch):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)
    ensure_native_entry(bot_b)
    observed = {"acquired": [], "released": 0}

    class Lease:
        def release(self):
            observed["released"] += 1

    async def fake_acquire(owner, count):
        observed["acquired"].append((owner, count))
        return Lease()

    async def fake_run(*_args, **_kwargs):
        return {"execution_mode": "native_tcp", "hands_played": 1}

    monkeypatch.setattr(national_native, "acquire_match_slots_async", fake_acquire)
    monkeypatch.setattr(national_native, "_run_tcp_server_with_processes", fake_run)

    result = asyncio.run(run_native_tcp_pair(bot_a, bot_b, hands=1))

    assert result["hands_played"] == 1
    assert len(observed["acquired"]) == 1
    assert observed["acquired"][0][1] == 1
    assert observed["acquired"][0][0].startswith("native_tcp:BotA:BotB:")
    assert observed["released"] == 1


def test_native_tcp_pair_releases_capacity_when_contract_preparation_fails(
    tmp_path, monkeypatch
):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)
    released = 0

    class Lease:
        def release(self):
            nonlocal released
            released += 1

    async def fake_acquire(_owner, count):
        assert count == 1
        return Lease()

    monkeypatch.setattr(national_native, "acquire_match_slots_async", fake_acquire)

    with pytest.raises(ValueError, match=r"BotB: missing required national_bot\.py"):
        asyncio.run(run_native_tcp_pair(bot_a, bot_b, hands=1))

    assert released == 1


def test_native_tcp_pair_defaults_to_strict_both_and_does_not_create_missing_entry(tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)

    with pytest.raises(ValueError, match=r"BotB: missing required national_bot\.py"):
        asyncio.run(run_native_tcp_pair(bot_a, bot_b, hands=1))

    assert not (bot_b / "national_bot.py").exists()


def test_native_tcp_pair_rejects_invalid_entry_without_rewriting_it(tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)
    invalid_source = "# intentionally invalid native contract\n"
    entry = bot_b / "national_bot.py"
    entry.write_text(invalid_source, encoding="utf-8")

    with pytest.raises(ValueError, match=r"BotB: invalid national_bot\.py"):
        asyncio.run(run_native_tcp_pair(bot_a, bot_b, hands=1))

    assert entry.read_text(encoding="utf-8") == invalid_source


def test_native_tcp_pair_rejects_disabled_native_requirement(tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)
    ensure_native_entry(bot_b)

    with pytest.raises(ValueError, match="run_legacy_debug_tcp_pair_with_wrappers"):
        asyncio.run(run_native_tcp_pair(
            bot_a,
            bot_b,
            hands=1,
            require_native_b=False,
        ))


def test_native_tcp_smoke_runs_without_adapter(tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)
    ensure_native_entry(bot_b)

    report = asyncio.run(run_native_tcp_smoke(
        bot_a,
        opponent_token=bot_b,
        hands=1,
        timeout_sec=30,
    ))

    assert report["execution_mode"] == "native_tcp"
    assert report["passed"] is True
    assert report["issues"] == []
    assert report["result"]["hands_played"] == 1
    assert all(
        row["adapter"]["actions_sent"] == 0
        for row in report["result"]["per_player"].values()
    )


@pytest.mark.parametrize(
    ("failing_label", "expected_outcome", "expected_side"),
    [
        ("Candidate", "candidate_failure", "candidate"),
        ("Opponent", "infrastructure_failure", "opponent"),
    ],
)
def test_native_smoke_attributes_candidate_and_opponent_failures(
    monkeypatch, tmp_path, failing_label, expected_outcome, expected_side
):
    import national_native

    candidate = tmp_path / "Candidate"
    opponent = tmp_path / "Opponent"
    _write_minimal_strategy_bot(candidate)
    _write_minimal_strategy_bot(opponent)

    async def fake_pair(*_args, **_kwargs):
        issue = f"{failing_label}: native_process_returncode=1"
        return {
            "passed_compliance": False,
            "issues": [issue],
            "per_player": {
                "Candidate": {
                    "compliance_issues": [issue] if failing_label == "Candidate" else [],
                },
                "Opponent": {
                    "compliance_issues": [issue] if failing_label == "Opponent" else [],
                },
            },
        }

    monkeypatch.setattr(national_native, "run_native_tcp_pair", fake_pair)
    report = asyncio.run(national_native.run_native_tcp_smoke(
        candidate,
        opponent_token=opponent,
        hands=1,
    ))

    assert report["passed"] is False
    assert report["outcome"] == expected_outcome
    assert report["failure_side"] == expected_side


def test_quality_smoke_gate_uses_native_tcp_backend(monkeypatch, tmp_path):
    import national_native
    import tool_gates

    bot_dir = tmp_path / "BotA"
    bot_dir.mkdir()
    called = {}

    def _legacy_smoke_should_not_run(_bot_dir):
        raise AssertionError("legacy JSON smoke should not run for national_native")

    async def _fake_native_smoke(candidate, *, source_v=None, hands=1, timeout_sec=90):
        called["candidate"] = Path(candidate)
        called["source_v"] = source_v
        called["hands"] = hands
        called["timeout_sec"] = timeout_sec
        return {
            "passed": True,
            "execution_mode": "native_tcp",
            "hands": hands,
            "issues": [],
        }

    monkeypatch.setattr(tool_gates, "run_smoke_test", _legacy_smoke_should_not_run)
    monkeypatch.setattr(national_native, "run_native_tcp_smoke", _fake_native_smoke)

    errors, payload = asyncio.run(tool_gates._run_workflow_smoke_gate(
        bot_dir=bot_dir,
        source_v=12,
        native_tcp_mode=True,
        compile_errors=[],
        import_errors=[],
        protected_contract_errors=[],
        native_contract_errors=[],
        embedded_selftest_errors=[],
    ))

    assert errors == []
    assert payload["execution_mode"] == "native_tcp"
    assert called["candidate"] == bot_dir
    assert called["source_v"] == 12


def test_quality_gate_treats_official_port_busy_as_inconclusive(monkeypatch, tmp_path):
    import code_verification
    import evolution_infra
    import national_native
    import official_certification
    import official_certification_job
    import runtime_architecture_policy
    import tool_gates

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    monkeypatch.setenv("POK_OFFICIAL_SMOKE_GATE", "run")
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))

    project = tmp_path / "project"
    source = project / "bots" / "national_v1"
    child = project / "bots" / "national_v2"
    _write_minimal_strategy_bot(source)
    _write_minimal_strategy_bot(child)
    ensure_native_entry(child)
    (child / "strategy.py").write_text("def get_action(req, requests):\n    return 1\n", encoding="utf-8")
    evolution_infra.write_pipeline_checkpoint(2, 1, "workers_done")

    class FakeAcceptance:
        passed = True
        issues = []
        opponents = ["national_v1"]
        summary = {"pairs": 1}

        def model_dump(self):
            return {
                "passed": True,
                "issues": [],
                "opponents": self.opponents,
                "summary": self.summary,
            }

    async def _fake_native_acceptance(*_args, **_kwargs):
        return FakeAcceptance()

    async def _fake_smoke(*_args, **_kwargs):
        return [], {"passed": True, "execution_mode": "native_tcp", "issues": []}

    monkeypatch.setattr(tool_gates, "PROJECT_ROOT", project)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: project / "bots" / f"national_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["strategy.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
    monkeypatch.setattr(tool_gates, "check_code_size", lambda *_a, **_k: (10, []))
    monkeypatch.setattr(tool_gates, "verify_fixes", lambda _bot_dir: {"mandatory": {"ok": True}})
    monkeypatch.setattr(tool_gates, "detect_position_semantics_errors", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "_run_workflow_smoke_gate", _fake_smoke)
    monkeypatch.setattr(national_native, "check_native_contract", lambda _bot_dir, **_kwargs: [])
    monkeypatch.setattr(national_native, "run_native_acceptance_for_candidate", _fake_native_acceptance)
    monkeypatch.setattr(
        runtime_architecture_policy,
        "evaluate_architecture_transition",
        _passing_architecture_transition,
    )
    monkeypatch.setattr(
        code_verification,
        "run_bot_embedded_self_tests_execution",
        lambda _bot_dir: __import__("gate_execution").GateExecution.passed(
            "embedded_selftests", "embedded_selftest"
        ),
    )
    monkeypatch.setattr(code_verification, "detect_placement_shadow_warnings", lambda _bot_dir: [])
    monkeypatch.setattr(code_verification, "detect_telemetry_fidelity_warnings", lambda _bot_dir: [])
    monkeypatch.setattr(code_verification, "detect_new_function_reachability_warnings", lambda *_a, **_k: [])
    monkeypatch.setattr(tool_gates, "run_decision_test_details", lambda *_a, **_k: {
        "pass_rate": 1.0,
        "passed": 1,
        "total": 1,
        "critical_passed": 1,
        "critical_total": 1,
        "critical_failures": [],
        "failures": [],
        "scenarios": [],
    })
    monkeypatch.setattr(official_certification, "run_certification", lambda *_a, **_k: {
        "status": official_certification.STATUS_INCONCLUSIVE,
        "mode": "smoke",
        "issues": ["self_play_1: port_busy_before_start: 127.0.0.1:10001"],
    })

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 2, "source_v": 1}))
    data = json.loads(result["content"][0]["text"])

    assert data["all_passed"] is True
    assert data["official_smoke_ok"] is True
    assert data["official_smoke_inconclusive"] is True
    assert data["official_smoke_blocking"] is False
    assert "official_smoke" not in data["failed_gates"]
    gates = {gate["name"]: gate for gate in data["scorecard"]["gates"]}
    assert gates["official_smoke"]["blocking"] is False
    assert gates["official_smoke"]["metrics"]["classification"] == "inconclusive"


def test_quality_gate_blocks_official_protocol_violation(monkeypatch, tmp_path):
    import code_verification
    import evolution_infra
    import national_native
    import official_certification
    import official_certification_job
    import runtime_architecture_policy
    import tool_gates

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    monkeypatch.setenv("POK_OFFICIAL_SMOKE_GATE", "run")
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))

    project = tmp_path / "project"
    source = project / "bots" / "national_v1"
    child = project / "bots" / "national_v2"
    _write_minimal_strategy_bot(source)
    _write_minimal_strategy_bot(child)
    ensure_native_entry(child)
    (child / "strategy.py").write_text("def get_action(req, requests):\n    return 1\n", encoding="utf-8")
    evolution_infra.write_pipeline_checkpoint(2, 1, "workers_done")

    class FakeAcceptance:
        passed = True
        issues = []
        opponents = ["national_v1"]
        summary = {"pairs": 1}

        def model_dump(self):
            return {
                "passed": True,
                "issues": [],
                "opponents": self.opponents,
                "summary": self.summary,
            }

    async def _fake_native_acceptance(*_args, **_kwargs):
        return FakeAcceptance()

    async def _fake_smoke(*_args, **_kwargs):
        return [], {"passed": True, "execution_mode": "native_tcp", "issues": []}

    monkeypatch.setattr(tool_gates, "PROJECT_ROOT", project)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: project / "bots" / f"national_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["strategy.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
    monkeypatch.setattr(tool_gates, "check_code_size", lambda *_a, **_k: (10, []))
    monkeypatch.setattr(tool_gates, "verify_fixes", lambda _bot_dir: {"mandatory": {"ok": True}})
    monkeypatch.setattr(tool_gates, "detect_position_semantics_errors", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "_run_workflow_smoke_gate", _fake_smoke)
    monkeypatch.setattr(national_native, "check_native_contract", lambda _bot_dir, **_kwargs: [])
    monkeypatch.setattr(national_native, "run_native_acceptance_for_candidate", _fake_native_acceptance)
    monkeypatch.setattr(
        runtime_architecture_policy,
        "evaluate_architecture_transition",
        _passing_architecture_transition,
    )
    monkeypatch.setattr(
        code_verification,
        "run_bot_embedded_self_tests_execution",
        lambda _bot_dir: __import__("gate_execution").GateExecution.passed(
            "embedded_selftests", "embedded_selftest"
        ),
    )
    monkeypatch.setattr(code_verification, "detect_placement_shadow_warnings", lambda _bot_dir: [])
    monkeypatch.setattr(code_verification, "detect_telemetry_fidelity_warnings", lambda _bot_dir: [])
    monkeypatch.setattr(code_verification, "detect_new_function_reachability_warnings", lambda *_a, **_k: [])
    monkeypatch.setattr(tool_gates, "run_decision_test_details", lambda *_a, **_k: {
        "pass_rate": 1.0,
        "passed": 1,
        "total": 1,
        "critical_passed": 1,
        "critical_total": 1,
        "critical_failures": [],
        "failures": [],
        "scenarios": [],
    })
    from bot_artifact import hash_path

    official_evidence = tmp_path / "official_evidence.json"
    official_evidence.write_text("{}\n", encoding="utf-8")
    official_spec = official_certification.build_spec("smoke", child, opponent=source)
    official_identity = {"candidate_hash": hash_path(child)}
    deterministic = {
        "passed": False,
        "classification": "protocol",
        "blocking": True,
        "inconclusive": False,
        "violation": True,
        "candidate_verdict": "fail",
        "rounds_requested": 2,
        "rounds_run": 1,
        "target_hands": 10,
        "issues": ["self_play_1: protocol_raise_format: msg='raise  200'"],
    }
    evidence_summary = {
        "classification": "protocol",
        "blocking": True,
        "inconclusive": False,
        "violation": True,
    }
    official_status = {
        "bot": child.name,
        "status": official_certification.STATUS_FAILED,
        "mode": "smoke",
        "policy_id": official_spec.policy_id,
        "cache_key": "quality-gate-smoke",
        "certification_identity": official_identity,
        "official_evidence_path": str(official_evidence),
        "official_evidence_summary": evidence_summary,
        "issues": deterministic["issues"],
    }
    official_status["official_deterministic_status_receipt"] = (
        official_certification._build_deterministic_status_receipt(
            official_spec,
            official_identity,
            official_evidence,
            deterministic,
            "quality-gate-smoke",
        )
    )
    monkeypatch.setattr(
        official_certification_job,
        "start_or_poll_job",
        lambda *_a, **_k: {
            "state": "completed",
            "pending": False,
            "status": official_status,
        },
    )
    monkeypatch.setattr(official_certification, "select_official_opponent", lambda *_a, **_k: {
        "selected": True,
        "candidate": str(child),
        "opponent": {
            "bot": source.name,
            "path": str(source),
            "eligible": True,
            "reason": "official_certified",
        },
        "considered": [],
    })

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 2, "source_v": 1}))
    data = json.loads(result["content"][0]["text"])

    assert data["all_passed"] is False
    assert data["official_smoke_ok"] is False
    assert data["official_smoke_blocking"] is True
    assert "official_smoke" in data["failed_gates"]
    gates = {gate["name"]: gate for gate in data["scorecard"]["gates"]}
    assert gates["official_smoke"]["blocking"] is True
    assert gates["official_smoke"]["metrics"]["classification"] == "protocol_violation"


def test_quality_gate_probe_infra_retries_then_stops_without_bot_repair(monkeypatch, tmp_path):
    import code_verification
    import runtime_architecture_policy
    import tool_gates

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    monkeypatch.setenv("POK_OFFICIAL_SMOKE_GATE", "0")
    monkeypatch.setenv("POK_OFFICIAL_REQUIRED", "0")
    monkeypatch.setattr(tool_gates, "QUALITY_INFRA_MAX_ATTEMPTS", 3)

    project = tmp_path / "project"
    source = project / "bots" / "national_v1"
    child = project / "bots" / "national_v2"
    _write_minimal_strategy_bot(source)
    _write_minimal_strategy_bot(child)
    ensure_native_entry(child)
    (child / "strategy.py").write_text(
        "def get_action(req, current_request_view):\n    return 1\n",
        encoding="utf-8",
    )
    evolution_infra.write_pipeline_checkpoint(2, 1, "workers_done")

    capabilities = {
        "schema_version": 2,
        "detector_version": "test",
        "ok": False,
        "conclusive": False,
        "outcome": "infrastructure_failure",
        "infrastructure_failures": [{
            "component": "national_runtime_probe",
            "failure_class": "probe_infra",
            "issues": ["bwrap launch failed"],
        }],
        "required_failures": [],
        "advisory_warnings": [],
        "checks": [],
        "checks_by_id": {},
        "dynamic_runtime_probe": {
            "failure_class": "probe_infra",
            "issues": ["bwrap launch failed"],
        },
    }

    def _infra_transition(*_args, **_kwargs):
        return {
            "schema_version": 1,
            "policy_version": "test",
            "ok": False,
            "conclusive": False,
            "outcome": "infrastructure_failure",
            "failure_class": "infrastructure",
            "policy": {},
            "policy_identity_errors": [],
            "infrastructure_failures": [{
                "component": "national_runtime_probe",
                "side": "candidate",
                "failure_class": "probe_infra",
                "issues": ["bwrap launch failed"],
            }],
            "runtime_probe_infra": [{
                "component": "national_runtime_probe",
                "side": "candidate",
                "issues": ["bwrap launch failed"],
            }],
            "regressions": [],
            "runtime_floor_failures": [],
            "selected_focus": None,
            "unresolved_focus_checks": [],
            "source_capabilities": capabilities,
            "candidate_capabilities": capabilities,
        }

    class FakeAcceptance:
        passed = True
        issues = []
        opponents = ["national_v1"]
        summary = {"pairs": 1}

        def model_dump(self):
            return {
                "passed": self.passed,
                "issues": self.issues,
                "opponents": self.opponents,
                "summary": self.summary,
            }

    async def _fake_native_acceptance(*_args, **_kwargs):
        return FakeAcceptance()

    async def _fake_smoke(*_args, **_kwargs):
        return [], {"passed": True, "execution_mode": "native_tcp", "issues": []}

    monkeypatch.setattr(tool_gates, "PROJECT_ROOT", project)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: project / "bots" / f"national_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["strategy.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
    monkeypatch.setattr(tool_gates, "check_code_size", lambda *_a, **_k: (10, []))
    monkeypatch.setattr(tool_gates, "verify_fixes", lambda _bot_dir: {"mandatory": {"ok": True}})
    monkeypatch.setattr(tool_gates, "detect_position_semantics_errors", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "_run_workflow_smoke_gate", _fake_smoke)
    monkeypatch.setattr(national_native, "check_native_contract", lambda _bot_dir, **_kwargs: [])
    monkeypatch.setattr(national_native, "run_native_acceptance_for_candidate", _fake_native_acceptance)
    monkeypatch.setattr(runtime_architecture_policy, "evaluate_architecture_transition", _infra_transition)
    monkeypatch.setattr(runtime_architecture_policy, "validate_runtime_contract_implementation", lambda *_a, **_k: [])
    monkeypatch.setattr(
        code_verification,
        "run_bot_embedded_self_tests_execution",
        lambda _bot_dir: __import__("gate_execution").GateExecution.passed(
            "embedded_selftests", "embedded_selftest"
        ),
    )
    monkeypatch.setattr(code_verification, "detect_placement_shadow_warnings", lambda _bot_dir: [])
    monkeypatch.setattr(code_verification, "detect_telemetry_fidelity_warnings", lambda _bot_dir: [])
    monkeypatch.setattr(code_verification, "detect_new_function_reachability_warnings", lambda *_a, **_k: [])
    monkeypatch.setattr(tool_gates, "run_decision_test_details", lambda *_a, **_k: {
        "pass_rate": 1.0,
        "passed": 1,
        "total": 1,
        "critical_passed": 1,
        "critical_total": 1,
        "critical_failures": [],
        "failures": [],
        "scenarios": [],
    })
    repair_failures = []
    events = []
    abandon_calls = []
    monkeypatch.setattr(tool_gates, "_record_quality_failure", lambda *args, **kwargs: repair_failures.append((args, kwargs)))
    monkeypatch.setattr(tool_gates, "append_candidate_event", lambda event_type, **kwargs: events.append((event_type, kwargs)))
    import tool_bot_management

    async def _fake_abandon(*_args, **_kwargs):
        abandon_calls.append((_args, _kwargs))
        return {"abandoned": True, "reason": "test probe exhaustion"}

    monkeypatch.setattr(tool_bot_management, "_do_abandon_generation", _fake_abandon)

    stages = []
    for _ in range(3):
        result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 2, "source_v": 1}))
        data = json.loads(result["content"][0]["text"])
        stages.append(data["checkpoint_stage"])
        assert data["all_passed"] is False
        assert data["quality_infrastructure"]["failure_class"] == "infrastructure"
        assert any(item.startswith("quality_infrastructure") for item in data["failed_gates"])
        assert not any(item.startswith("national_capability_contract") for item in data["failed_gates"])

    assert stages == ["workers_done", "workers_done", "abandoned"]
    assert repair_failures == []
    checkpoint = evolution_infra.read_pipeline_checkpoint()
    assert checkpoint["stage"] == "workers_done"
    assert checkpoint["infra_failure"]["attempt"] == 3
    assert checkpoint["infra_failure"]["exhausted"] is True
    assert len(abandon_calls) == 1
    assert [kwargs["stage"] for event, kwargs in events if event == "quality_finished"] == [
        "workers_done",
        "workers_done",
        "workers_done",
    ]


def test_national_protocol_gate_uses_platform_shard_for_native(monkeypatch):
    import code_verification

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(code_verification.subprocess, "run", fake_run)

    assert code_verification.run_national_protocol_tests(native_tcp_mode=True) == []
    assert calls
    assert str(calls[-1][0][3]).endswith("sever/tests/test_national_platform_alignment.py")

    assert code_verification.run_national_protocol_tests(native_tcp_mode=False) == []
    assert str(calls[-1][0][3]).endswith("sever/tests/test_national_alignment.py")


def _raw_probe_native_source(
    *,
    startup: str = "",
    on_small_blind: str = "_send_wire_action(sock, 'fold', last_platform_message_at)",
    delay_sec: float = 0.0,
) -> str:
    return (
        "import argparse\n"
        "import os\n"
        "import random\n"
        "import re\n"
        "import socket\n"
        "import sys\n"
        "import time\n\n"
        "CARD_RE = re.compile(r'<(\\d+),(\\d+)>')\n"
        "ACTION_RE = re.compile(r'^(raise|bet)\\s+(\\d+)')\n"
        "EARN_RE = re.compile(r'^earnChips\\s+-?\\d+')\n\n"
        "DEFAULT_OFFICIAL_ACTION_DELAY_SEC = 0.30\n"
        "OFFICIAL_ACTION_DELAY_ENV = 'POK_OFFICIAL_ACTION_DELAY'\n\n"
        "def _official_action_delay_sec():\n"
        "    raw = os.environ.get(OFFICIAL_ACTION_DELAY_ENV, str(DEFAULT_OFFICIAL_ACTION_DELAY_SEC))\n"
        "    try:\n"
        "        delay = float(raw)\n"
        "    except (TypeError, ValueError):\n"
        "        delay = DEFAULT_OFFICIAL_ACTION_DELAY_SEC\n"
        "    return max(0.0, min(delay, 2.0))\n\n"
        "def _send_wire_action(sock, msg, last_platform_message_at=0.0):\n"
        "    delay = _official_action_delay_sec()\n"
        "    if delay > 0 and last_platform_message_at > 0:\n"
        "        wait_sec = delay - (time.perf_counter() - last_platform_message_at)\n"
        "        if wait_sec > 0:\n"
        "            time.sleep(wait_sec)\n"
        "    if isinstance(msg, str):\n"
        "        msg = msg.encode('utf-8')\n"
        "    sock.sendall(msg)\n\n"
        "def _take_card_message(buffer, prefix, count):\n"
        "    if not buffer.startswith(prefix):\n"
        "        return None, buffer\n"
        "    pos = len(prefix)\n"
        "    for _ in range(count):\n"
        "        match = CARD_RE.match(buffer, pos)\n"
        "        if not match:\n"
        "            return None, buffer\n"
        "        pos = match.end()\n"
        "    return buffer[:pos], buffer[pos:]\n\n"
        "def _take_message(buffer):\n"
        "    buffer = buffer.lstrip('\\r\\n\\t ')\n"
        "    if not buffer:\n"
        "        return None, ''\n"
        "    if buffer.startswith('name'):\n"
        "        return 'name', buffer[4:]\n"
        "    for blind in ('SMALLBLIND', 'BIGBLIND'):\n"
        "        msg, rest = _take_card_message(buffer, f'preflop|{blind}|', 2)\n"
        "        if msg is not None:\n"
        "            return msg, rest\n"
        "    for prefix, count in (('flop|', 3), ('turn|', 1), ('river|', 1), ('oppo_hands|', 2)):\n"
        "        msg, rest = _take_card_message(buffer, prefix, count)\n"
        "        if msg is not None:\n"
        "            return msg, rest\n"
        "    match = EARN_RE.match(buffer)\n"
        "    if match:\n"
        "        return buffer[:match.end()], buffer[match.end():]\n"
        "    match = ACTION_RE.match(buffer)\n"
        "    if match:\n"
        "        return buffer[:match.end()], buffer[match.end():]\n"
        "    for word in ('allin', 'check', 'call', 'fold'):\n"
        "        if buffer.startswith(word):\n"
        "            return word, buffer[len(word):]\n"
        "    return None, buffer\n\n"
        "def _split_messages(buffer):\n"
        "    messages = []\n"
        "    while buffer:\n"
        "        msg, rest = _take_message(buffer)\n"
        "        if msg is None:\n"
        "            return messages, rest\n"
        "        messages.append(msg)\n"
        "        buffer = rest\n"
        "    return messages, ''\n\n"
        f"{startup}\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--host')\n"
        "    parser.add_argument('--port', type=int)\n"
        "    parser.add_argument('--name')\n"
        "    args = parser.parse_args()\n"
        f"    time.sleep({float(delay_sec)!r})\n"
        "    with socket.create_connection((args.host, args.port), timeout=5) as sock:\n"
        "        buffer = ''\n"
        "        while True:\n"
        "            data = sock.recv(4096)\n"
        "            if not data:\n"
        "                return 0\n"
        "            last_platform_message_at = time.perf_counter()\n"
        "            buffer += data.decode('utf-8', 'replace')\n"
        "            messages, buffer = _split_messages(buffer)\n"
        "            for line in messages:\n"
        "                if line == 'name':\n"
        "                    sock.sendall(args.name.encode('utf-8'))\n"
        "                elif line.startswith('preflop|SMALLBLIND|'):\n"
        f"                    {on_small_blind}\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
        "# required wire tokens: raise fold call check allin\n"
    )


def _write_random_probe_native_bot(bot_dir: Path) -> None:
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "main.py").write_text("# native-only seed probe\n", encoding="utf-8")
    (bot_dir / "national_bot.py").write_text(
        _raw_probe_native_source(
            startup="print(f'RANDOM_PROBE {random.random():.12f}', file=sys.stderr, flush=True)",
        ),
        encoding="utf-8",
    )


def _write_trace_probe_native_bot(bot_dir: Path) -> None:
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "main.py").write_text("# native-only trace probe\n", encoding="utf-8")
    (bot_dir / "national_bot.py").write_text(
        _raw_probe_native_source(
            startup="import json",
            on_small_blind=(
                "print('POK_TRACE_DECISION ' + json.dumps({'type': 'decision', 'hand': 1, 'final_action': -1}), "
                "file=sys.stderr, flush=True) if os.environ.get('POK_TRACE_DECISIONS') == '1' else None; "
                "_send_wire_action(sock, 'fold', last_platform_message_at)"
            ),
        ),
        encoding="utf-8",
    )


def _write_delay_connect_native_bot(bot_dir: Path, delay_sec: float) -> None:
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "main.py").write_text("# native-only delay probe\n", encoding="utf-8")
    (bot_dir / "national_bot.py").write_text(
        _raw_probe_native_source(delay_sec=delay_sec),
        encoding="utf-8",
    )


def test_native_tcp_pair_can_seed_bot_process_random(tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_random_probe_native_bot(bot_a)
    _write_random_probe_native_bot(bot_b)

    first = asyncio.run(run_native_tcp_pair(
        bot_a,
        bot_b,
        hands=2,
        require_native_a=True,
        require_native_b=True,
        deck_seed_base=1234,
        bot_seed_base=4321,
        timeout_sec=30,
    ))
    second = asyncio.run(run_native_tcp_pair(
        bot_a,
        bot_b,
        hands=2,
        require_native_a=True,
        require_native_b=True,
        deck_seed_base=1234,
        bot_seed_base=4321,
        timeout_sec=30,
    ))

    assert first["bot_seed_base"] == 4321
    assert first["per_player"]["BotA"]["native"]["bot_seed"] == 4321
    assert first["per_player"]["BotB"]["native"]["bot_seed"] == 4322
    assert first["per_player"]["BotA"]["native"]["stderr_tail"] == second["per_player"]["BotA"]["native"]["stderr_tail"]
    assert first["per_player"]["BotB"]["native"]["stderr_tail"] == second["per_player"]["BotB"]["native"]["stderr_tail"]


def test_native_tcp_pair_parses_decision_trace(monkeypatch, tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_trace_probe_native_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_b)
    monkeypatch.setenv("POK_TRACE_DECISIONS", "1")

    result = asyncio.run(run_native_tcp_pair(
        bot_a,
        bot_b,
        hands=1,
        require_native_a=True,
        require_native_b=True,
        deck_seed_base=1234,
        timeout_sec=30,
    ))

    trace = result["per_player"]["BotA"]["native"]["decision_trace"]
    assert trace == [{"type": "decision", "hand": 1, "final_action": -1}]
    assert result["passed_compliance"] is True


def test_native_bot_log_parser_summarizes_decision_runtime():
    report = national_native._parse_native_bot_log(
        "[10:00:00] DECIDE start name=BotA hand=1 stage=preflop act_cnt=0\n"
        "[10:00:00] DECIDE done action=0 elapsed=0.125s\n"
        "[10:00:00] SEND name=BotA hand=1 stage=preflop act_cnt=0 msg='call'\n"
        "[10:00:01] OFFICIAL_ACTION_DELAY wait=0.250s target=0.300s\n"
        "[10:00:02] DECIDE start name=BotA hand=11 stage=flop act_cnt=0\n"
        "[10:00:02] DECIDE done action=-1 elapsed=0.500s\n"
    )

    assert report["decision_latency"]["count"] == 2
    assert report["decision_latency"]["max_sec"] == 0.5
    assert report["decision_latency"]["by_stage"]["preflop"]["count"] == 1
    assert report["decision_latency"]["by_hand_bucket"]["11-20"]["max_sec"] == 0.5
    assert report["official_action_delay"]["count"] == 1
    assert report["official_action_delay"]["target_sec"] == 0.3
    assert report["send_count"] == 1


def test_native_tcp_pair_captures_template_runtime_telemetry(tmp_path, monkeypatch):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)
    ensure_native_entry(bot_b)
    runtime_log_dir = tmp_path / "runtime-logs"
    real_mkdtemp = national_native.tempfile.mkdtemp

    def controlled_mkdtemp(*args, **kwargs):
        if kwargs.get("prefix") == "pok_native_logs_":
            runtime_log_dir.mkdir()
            return str(runtime_log_dir)
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(national_native.tempfile, "mkdtemp", controlled_mkdtemp)

    result = asyncio.run(run_native_tcp_pair(
        bot_a,
        bot_b,
        hands=2,
        require_native_a=True,
        require_native_b=True,
        deck_seed_base=1234,
        timeout_sec=30,
    ))

    assert result["passed_compliance"] is True
    telemetry = result["per_player"]["BotA"]["runtime_telemetry"]
    assert telemetry["bot_log_supported"] is True
    assert telemetry["server_action_latency"]["count"] >= 1
    assert telemetry["server_action_latency"]["budget_sec"] == 30.0
    assert telemetry["bot_log"]["decision_latency"]["count"] >= 1
    assert telemetry["bot_log"]["decision_latency"]["budget_sec"] == 60.0
    assert "bot_log_tail" not in result["per_player"]["BotA"]["native"]
    assert not runtime_log_dir.exists()


def test_native_acceptance_summary_includes_runtime_telemetry(tmp_path):
    candidate = tmp_path / "Candidate"
    opponent = tmp_path / "Opponent"
    _write_minimal_strategy_bot(candidate)
    _write_minimal_strategy_bot(opponent)
    ensure_native_entry(candidate)
    ensure_native_entry(opponent)

    result = asyncio.run(national_native.run_native_acceptance_for_candidate(
        candidate,
        opponent_tokens=[opponent],
        hands=2,
        timeout_sec=30,
    ))

    assert result.passed is True
    runtime = result.summary["runtime_telemetry"]
    assert runtime["server_action_latency"]["count"] >= 1
    assert runtime["bot_decision_latency"]["count"] >= 1
    assert runtime["matches_with_bot_log"] == 1


def test_native_acceptance_and_precommit_require_native_entries_for_both_players(monkeypatch, tmp_path):
    candidate = tmp_path / "Candidate"
    opponent = tmp_path / "Opponent"
    _write_minimal_strategy_bot(candidate)
    _write_minimal_strategy_bot(opponent)
    calls = []

    async def fake_native_pair(bot_a, bot_b, hands, **kwargs):
        label_a = Path(bot_a).name
        label_b = Path(bot_b).name
        calls.append(kwargs)
        return {
            "bot_a": label_a,
            "bot_b": label_b,
            "hands_played": hands,
            "net_chips_a": 100,
            "net_chips_b": -100,
            "net_chips_a_per_hand": 100.0 / hands,
            "passed_compliance": True,
            "wrapper_used": False,
            "issues": [],
            "per_player": {
                label_a: {
                    "earnings": 100,
                    "illegal_actions": 0,
                    "timeouts": 0,
                    "runtime_telemetry": {},
                    "native": {},
                },
                label_b: {
                    "earnings": -100,
                    "illegal_actions": 0,
                    "timeouts": 0,
                    "runtime_telemetry": {},
                    "native": {},
                },
            },
        }

    monkeypatch.setattr(national_native, "run_native_tcp_pair", fake_native_pair)

    acceptance = asyncio.run(national_native.run_native_acceptance_for_candidate(
        candidate,
        opponent_tokens=[opponent],
        hands=1,
        timeout_sec=5,
    ))
    precommit = asyncio.run(national_native.run_native_precommit(
        candidate,
        [{"name": "Opponent", "path": str(opponent), "reason": "parent"}],
        hands=1,
        parent_label="Opponent",
        sample_plan=[{
            "opponent": "Opponent",
            "opponent_index": 0,
            "repeat": 1,
            "deck_seed_base": 777,
            "bot_seed_base": 888,
        }],
        parent_loss_threshold=-999_999,
        aggregate_loss_threshold=-999_999,
    ))

    assert acceptance.report["wrapper_used"] is False
    assert precommit["wrapper_used"] is False
    assert len(calls) == 2
    assert all(call["require_native_a"] is True for call in calls)
    assert all(call["require_native_b"] is True for call in calls)
    assert all(call["bot_seed_base"] is not None for call in calls)
    assert calls[-1]["deck_seed_base"] == 777
    assert calls[-1]["bot_seed_base"] == 888


def test_native_tcp_pair_reorders_clients_by_bot_label(tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_delay_connect_native_bot(bot_a, 0.5)
    _write_delay_connect_native_bot(bot_b, 0.0)

    result = asyncio.run(run_native_tcp_pair(
        bot_a,
        bot_b,
        hands=1,
        require_native_a=True,
        require_native_b=True,
        deck_seed_base=1234,
        timeout_sec=30,
    ))

    assert result["passed_compliance"] is True
    assert result["bot_a"] == "BotA"
    assert result["bot_b"] == "BotB"
    assert any(
        event.get("type") == "client_order"
        and event.get("order") == ["BotA", "BotB"]
        and event.get("connection_order") == ["BotB", "BotA"]
        for event in result["events_tail"]
    )


def test_native_tcp_pair_disambiguates_duplicate_labels(tmp_path):
    bot_a = tmp_path / "A" / "Same"
    bot_b = tmp_path / "B" / "Same"
    _write_delay_connect_native_bot(bot_a, 0.0)
    _write_delay_connect_native_bot(bot_b, 0.0)

    result = asyncio.run(run_native_tcp_pair(
        bot_a,
        bot_b,
        hands=1,
        require_native_a=True,
        require_native_b=True,
        deck_seed_base=1234,
        timeout_sec=30,
    ))

    assert result["bot_a"] == "Same_A"
    assert result["bot_b"] == "Same_B"
    assert sorted(result["per_player"]) == ["Same_A", "Same_B"]
    assert result["passed_compliance"] is True


def test_legacy_debug_tcp_pair_wraps_unsafe_opponent_without_rewriting_it(tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)
    entry = bot_b / "national_bot.py"
    invalid_source = (
        "import socket\n\n"
        "class NativeNationalBot:\n"
        "    def _strategy_action(self):\n"
        "        action = 250\n"
        "        try:\n"
        "            action = self.sanitize_action(action, {}, 20000)\n"
        "        except Exception:\n"
        "            pass\n"
        "        return int(action)\n\n"
        "# required wire tokens: raise fold call check allin\n"
    )
    entry.write_text(invalid_source, encoding="utf-8")

    result = asyncio.run(run_legacy_debug_tcp_pair_with_wrappers(
        bot_a,
        bot_b,
        hands=2,
        deck_seed_base=5678,
        timeout_sec=30,
    ))

    assert result["execution_mode"] == "native_tcp"
    assert result["hands_played"] == 2
    assert result["passed_compliance"] is True
    assert result["wrapper_used"] is True
    assert result["wrapper_used_by_player"] == {"BotA": False, "BotB": True}
    assert result["per_player"]["BotB"]["wrapper_used"] is True
    assert entry.read_text(encoding="utf-8") == invalid_source
