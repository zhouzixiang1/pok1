import asyncio
from pathlib import Path

from candidate_hygiene import sanitize_candidate_dir
from national_native import (
    check_native_contract,
    ensure_native_entry,
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


def test_native_entry_contract_allows_template_and_rejects_legacy_tokens(tmp_path):
    bot_dir = tmp_path / "BotA"
    _write_minimal_strategy_bot(bot_dir)

    entry = ensure_native_entry(bot_dir)

    assert entry.name == "national_bot.py"
    assert check_native_contract(bot_dir) == []
    text = entry.read_text(encoding="utf-8")
    assert "bot_adapter" not in text
    assert '"response"' not in text

    entry.write_text(
        "from sever.bot_adapter import BotAdapter\n"
        "print({'response': 0})\n",
        encoding="utf-8",
    )

    errors = check_native_contract(bot_dir)
    assert any("bot_adapter" in err or "BotAdapter" in err for err in errors)
    assert any("'response'" in err for err in errors)


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
        require_native_a=True,
        require_native_b=True,
        deck_seed_base=1234,
        timeout_sec=30,
    ))

    assert result["execution_mode"] == "native_tcp"
    assert result["hands_played"] == 2
    assert len(result["settlements"]) == 2
    assert all(len(row["earnings"]) == 2 for row in result["settlements"])
    assert result["passed_compliance"] is True
    assert result["issues"] == []
    assert all(
        row["adapter"]["actions_sent"] == 0
        for row in result["per_player"].values()
    )


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


def _write_random_probe_native_bot(bot_dir: Path) -> None:
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "main.py").write_text("# native-only seed probe\n", encoding="utf-8")
    (bot_dir / "national_bot.py").write_text(
        "import argparse\n"
        "import random\n"
        "import socket\n"
        "import sys\n\n"
        "print(f'RANDOM_PROBE {random.random():.12f}', file=sys.stderr, flush=True)\n\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--host')\n"
        "    parser.add_argument('--port', type=int)\n"
        "    parser.add_argument('--name')\n"
        "    args = parser.parse_args()\n"
        "    with socket.create_connection((args.host, args.port), timeout=5) as sock:\n"
        "        stream = sock.makefile('r', encoding='utf-8', newline='\\n')\n"
        "        while True:\n"
        "            line = stream.readline()\n"
        "            if not line:\n"
        "                return 0\n"
        "            line = line.rstrip('\\r\\n')\n"
        "            if line == 'name':\n"
        "                sock.sendall((args.name + '\\n').encode('utf-8'))\n"
        "            elif line.startswith('preflop|SMALLBLIND|'):\n"
        "                sock.sendall(b'fold\\n')\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
        "# required wire tokens: raise fold call check allin\n",
        encoding="utf-8",
    )


def _write_trace_probe_native_bot(bot_dir: Path) -> None:
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "main.py").write_text("# native-only trace probe\n", encoding="utf-8")
    (bot_dir / "national_bot.py").write_text(
        "import argparse\n"
        "import json\n"
        "import os\n"
        "import socket\n"
        "import sys\n\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--host')\n"
        "    parser.add_argument('--port', type=int)\n"
        "    parser.add_argument('--name')\n"
        "    args = parser.parse_args()\n"
        "    with socket.create_connection((args.host, args.port), timeout=5) as sock:\n"
        "        stream = sock.makefile('r', encoding='utf-8', newline='\\n')\n"
        "        while True:\n"
        "            line = stream.readline()\n"
        "            if not line:\n"
        "                return 0\n"
        "            line = line.rstrip('\\r\\n')\n"
        "            if line == 'name':\n"
        "                sock.sendall((args.name + '\\n').encode('utf-8'))\n"
        "            elif line.startswith('preflop|SMALLBLIND|'):\n"
        "                if os.environ.get('POK_TRACE_DECISIONS') == '1':\n"
        "                    row = {'type': 'decision', 'hand': 1, 'final_action': -1}\n"
        "                    print('POK_TRACE_DECISION ' + json.dumps(row), file=sys.stderr, flush=True)\n"
        "                sock.sendall(b'fold\\n')\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
        "# required wire tokens: raise fold call check allin\n",
        encoding="utf-8",
    )


def _write_delay_connect_native_bot(bot_dir: Path, delay_sec: float) -> None:
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "main.py").write_text("# native-only delay probe\n", encoding="utf-8")
    (bot_dir / "national_bot.py").write_text(
        "import argparse\n"
        "import socket\n"
        "import time\n\n"
        f"DELAY_SEC = {float(delay_sec)!r}\n\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--host')\n"
        "    parser.add_argument('--port', type=int)\n"
        "    parser.add_argument('--name')\n"
        "    args = parser.parse_args()\n"
        "    time.sleep(DELAY_SEC)\n"
        "    with socket.create_connection((args.host, args.port), timeout=5) as sock:\n"
        "        stream = sock.makefile('r', encoding='utf-8', newline='\\n')\n"
        "        while True:\n"
        "            line = stream.readline()\n"
        "            if not line:\n"
        "                return 0\n"
        "            line = line.rstrip('\\r\\n')\n"
        "            if line == 'name':\n"
        "                sock.sendall((args.name + '\\n').encode('utf-8'))\n"
        "            elif line.startswith('preflop|SMALLBLIND|'):\n"
        "                sock.sendall(b'fold\\n')\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
        "# required wire tokens: raise fold call check allin\n",
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


def test_native_tcp_pair_refreshes_unsafe_legacy_opponent_entry(tmp_path):
    bot_a = tmp_path / "BotA"
    bot_b = tmp_path / "BotB"
    _write_minimal_strategy_bot(bot_a)
    _write_minimal_strategy_bot(bot_b)
    ensure_native_entry(bot_a)
    (bot_b / "national_bot.py").write_text(
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

    result = asyncio.run(run_native_tcp_pair(
        bot_a,
        bot_b,
        hands=2,
        require_native_a=True,
        require_native_b=False,
        deck_seed_base=5678,
        timeout_sec=30,
    ))

    assert result["execution_mode"] == "native_tcp"
    assert result["hands_played"] == 2
    assert result["passed_compliance"] is True
