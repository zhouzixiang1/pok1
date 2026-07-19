from contextlib import contextmanager
import importlib.util
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace

import national_decision_tester as native_tests
import pytest
from managed_bot_executor import (
    EndpointLeaseError,
    IsolationIdentity,
    IsolationUnavailable,
    ManagedExecutorError,
)
from national_native import ensure_native_entry


def _complete_strict_launch_fixture(bot_dir: Path) -> None:
    """Add the two system identity documents required by the five-file ABI."""

    (bot_dir / "national_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
    (bot_dir / "policy_epoch_receipt.json").write_text("{}\n", encoding="utf-8")


def _load_native_entry(bot_dir: Path, monkeypatch):
    for module_name in ("policy", "typed_native_entry_probe"):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.syspath_prepend(str(bot_dir))
    spec = importlib.util.spec_from_file_location(
        "typed_native_entry_probe",
        bot_dir / "national_bot.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["typed_native_entry_probe"] = module
    spec.loader.exec_module(module)
    return module


def test_system_runtime_fixtures_cover_repaired_national_semantics():
    results = native_tests._system_runtime_fixture_results()

    assert results
    assert all(row["passed"] is True for row in results), results
    assert {row["id"] for row in results} == {
        "runtime_sticky_split_no_newline",
        "runtime_omitted_street_call_repairs_pot",
        "runtime_terminal_fold_persists_cross_hand",
        "runtime_showdown_updates_range",
        "runtime_donk_and_delayed_probe_reachable",
        "runtime_postflop_first_pass_maps_to_check",
        "runtime_postflop_facing_check_pass_maps_to_call",
    }


def test_national_decision_result_counts_only_assertion_backed_fixtures(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(native_tests, "check_native_contract", lambda *_a, **_k: [])
    monkeypatch.setattr(
        native_tests,
        "_system_runtime_fixture_results",
        lambda: [{
            "id": "runtime",
            "kind": "system_runtime",
            "critical": True,
            "passed": True,
        }],
    )
    observed = []

    def run_policy(_root: Path, fixture):
        observed.append(fixture.fixture_id)
        return {
            "id": fixture.fixture_id,
            "kind": "candidate_policy_wire",
            "critical": True,
            "passed": True,
        }

    monkeypatch.setattr(native_tests, "_run_policy_fixture", run_policy)

    result = native_tests.run_national_decision_tests(tmp_path)

    assert observed == [fixture.fixture_id for fixture in native_tests.POLICY_FIXTURES]
    assert result["protocol"] == "official_raw_tcp_transcript_v1"
    assert result["passed"] == result["total"] == 1 + len(native_tests.POLICY_FIXTURES)
    assert result["pass_rate"] == 1.0
    assert result["coverage_only_count"] == 0
    assert result["external_scenario_sidecars_loaded"] is False


def test_national_decision_contract_failure_is_critical_and_does_not_run_policy(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        native_tests,
        "check_native_contract",
        lambda *_a, **_k: ["national_bot.py is stale"],
    )
    monkeypatch.setattr(
        native_tests,
        "_run_policy_fixture",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not execute")),
    )

    result = native_tests.run_national_decision_tests(tmp_path)

    assert result["pass_rate"] == 0.0
    assert result["total"] == 1
    assert result["critical_failures"][0]["id"] == "national_native_contract"
    assert "stale" in result["critical_failures"][0]["details"]


@pytest.mark.parametrize(
    ("fixture", "expected_action"),
    (
        (native_tests.POLICY_FIXTURES[0], "call"),
        (native_tests.POLICY_FIXTURES[1], "check"),
        (native_tests.POLICY_FIXTURES[2], "call"),
    ),
    ids=[fixture.fixture_id for fixture in native_tests.POLICY_FIXTURES],
)
def test_policy_fixture_observes_real_isolated_tcp_action(
    tmp_path,
    fixture,
    expected_action,
):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    assert context['schema_version'] == 1\n"
        "    assert set(context) == {'schema_version', 'runtime_version', 'decision_id', 'cards', 'hand', 'betting', 'history', 'line', 'legal', 'opponent', 'deadline'}\n"
        "    assert context['cards']['encoding'] == 'national_tcp_suit_rank_v1'\n"
        "    assert all(set(card) == {'suit', 'rank'} for card in context['cards']['hole'])\n"
        "    assert {'pot', 'hero_stack', 'opponent_stack', 'to_call', 'spr', 'pot_odds'} <= set(context['betting'])\n"
        "    assert context['history']['truncated_count'] >= 0\n"
        "    assert context['deadline']['clock'] == 'time.monotonic'\n"
        "    assert context['legal']['pass_wire_kind'] in {'call', 'check'}\n"
        "    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    if False:\n"
        "        yield baseline\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    _complete_strict_launch_fixture(bot_dir)

    result = native_tests._run_policy_fixture(
        bot_dir,
        fixture,
    )

    assert result["passed"] is True, result
    assert result["action"] == expected_action
    assert result["isolation"]["network"] == "isolated-netns-inherited-exact-peer-only"


def test_template_exposes_only_typed_native_policy_abi():
    template = native_tests.NATIVE_BOT_TEMPLATE

    assert 'importlib.import_module("policy")' in template
    assert "get_baseline_decision(context)" in template
    assert "iter_decisions(context, baseline, deadline)" in template
    assert "def _decision_to_tcp" in template
    assert "def _legalize_policy_decision" in template
    for forbidden in (
        'import_module("main")',
        'import_module("state")',
        'import_module("strategy")',
        "current_request_view",
        "self._requests",
        "self._responses",
        "def _action_to_tcp",
        "def _strategy_action",
    ):
        assert forbidden not in template


def test_current_runtime_contract_requires_both_policy_entrypoints(tmp_path):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    ensure_native_entry(bot_dir)

    missing = native_tests.check_native_contract(
        bot_dir,
        require_current_decision_runtime=True,
    )
    assert any("policy.py missing" in error for error in missing)

    (bot_dir / "policy.py").write_text(
        "def get_baseline_decision(context): return {'kind': 'pass'}\n",
        encoding="utf-8",
    )
    incomplete = native_tests.check_native_contract(
        bot_dir,
        require_current_decision_runtime=True,
    )
    assert any("iter_decisions" in error for error in incomplete)


def test_native_contract_rejects_unbound_candidate_model_file(tmp_path):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    ensure_native_entry(bot_dir)
    (bot_dir / "policy.py").write_text(
        "def get_baseline_decision(context): return {'kind': 'pass'}\n"
        "def iter_decisions(context, baseline, deadline): return iter(())\n",
        encoding="utf-8",
    )
    _complete_strict_launch_fixture(bot_dir)
    (bot_dir / "foreign-model.bin").write_bytes(b"unbound")

    issues = native_tests.check_native_contract(
        bot_dir,
        require_current_stream_decoder=True,
        require_current_decision_runtime=True,
    )

    assert "artifact_extra_file_forbidden:foreign-model.bin" in issues


@pytest.mark.parametrize(
    "required_token",
    (
        "def _match_control_state",
        '"match_control": self._match_control_state(remaining)',
        '"call_closes_allin_runout": bool(',
    ),
)
def test_current_runtime_contract_rejects_missing_v10_context_producer(
    tmp_path,
    required_token,
):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    ensure_native_entry(bot_dir)
    (bot_dir / "policy.py").write_text(
        "def get_baseline_decision(context): return {'kind': 'pass'}\n"
        "def iter_decisions(context, baseline, deadline): return iter(())\n",
        encoding="utf-8",
    )
    entry = bot_dir / "national_bot.py"
    entry.write_text(
        entry.read_text(encoding="utf-8").replace(required_token, "removed", 1),
        encoding="utf-8",
    )

    issues = native_tests.check_native_contract(
        bot_dir,
        require_current_decision_runtime=True,
    )

    assert any(required_token in issue for issue in issues)


@pytest.mark.parametrize(
    "raw_decision",
    (
        {"kind": "call"},
        {"kind": "check"},
        0,
        "call",
        {"kind": "raise"},
        {"kind": "pass", "raise_to": 200},
    ),
    ids=("call", "check", "integer", "string", "raise-missing-total", "pass-with-total"),
)
def test_invalid_candidate_policy_output_converges_at_real_socket_owner(
    tmp_path,
    raw_decision,
):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "policy.py").write_text(
        "DECISION = " + repr(raw_decision) + "\n\n"
        "def get_baseline_decision(context):\n"
        "    return DECISION\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    if False:\n"
        "        yield baseline\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    _complete_strict_launch_fixture(bot_dir)

    result = native_tests._run_policy_fixture(
        bot_dir,
        native_tests.POLICY_FIXTURES[0],
    )

    assert result["passed"] is True, result
    # The SB is facing the blind difference, so the typed socket fallback is fold.
    assert result["action"] == "fold"


def test_legal_candidate_raise_is_not_misclassified_as_a_protocol_fixture_failure(tmp_path):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return {'kind': 'raise', 'raise_to': 208}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    if False:\n"
        "        yield baseline\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    _complete_strict_launch_fixture(bot_dir)

    result = native_tests._run_policy_fixture(
        bot_dir,
        native_tests.POLICY_FIXTURES[0],
    )

    assert result["passed"] is True, result
    assert result["action"] == "raise 208"


def test_exact_official_two_x_raise_boundary_reaches_real_wire(tmp_path):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    assert context['legal']['min_raise_to'] == 400\n"
        "    return {'kind': 'raise', 'raise_to': 400}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    if False:\n"
        "        yield baseline\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    _complete_strict_launch_fixture(bot_dir)

    result = native_tests._run_policy_fixture(
        bot_dir,
        native_tests.POLICY_FIXTURES[2],
    )

    assert result["passed"] is True, result
    assert result["action"] == "raise 400"


@pytest.mark.parametrize("relay_runout", [False, True])
def test_called_allin_runout_never_reenters_policy_or_socket_send(
    monkeypatch,
    tmp_path,
    relay_runout,
):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "policy.py").write_text(
        "def get_baseline_decision(context): return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    if False:\n"
        "        yield baseline\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    monkeypatch.setenv("POK_OFFICIAL_ACTION_DELAY", "0")
    module = _load_native_entry(bot_dir, monkeypatch)
    bot = module.NativeNationalBot("Runout", seat="lower")
    decisions = []

    def pass_once():
        decisions.append(bot._stage)
        return {"kind": "pass"}

    class Wire:
        def __init__(self):
            self.payloads = []

        def sendall(self, payload):
            self.payloads.append(payload)

    wire = Wire()
    bot._policy_decision = pass_once
    try:
        bot.handle("preflop|BIGBLIND|<0,0><1,1>", wire)
        bot.handle("allin", wire)
        assert bot._in_allin_runout is True
        assert bot._pot == 40_000
        assert bot._my_chips == bot._opponent_chips == 0
        if relay_runout:
            # Compatibility with a complete local transcript.  The formal
            # 2021 EXE may instead jump directly to settlement/showdown.
            bot.handle("flop|<0,4><1,5><2,6>", wire)
            bot.handle("turn|<3,7>", wire)
            bot.handle("river|<0,8>", wire)
        bot.handle("earnChips 0", wire)
        bot.handle("oppo_hands|<2,2><3,3>", wire)
    finally:
        bot.close()

    assert decisions == ["preflop"]
    assert wire.payloads == [b"call"]


def test_typed_policy_deadline_keeps_socket_owned_fallback_and_kills_worker(
    monkeypatch,
    tmp_path,
):
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    (bot_dir / "policy.py").write_text(
        "import time\n\n"
        "def get_baseline_decision(context):\n"
        "    time.sleep(0.30)\n"
        "    return {'kind': 'allin'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    if False:\n"
        "        yield baseline\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir)
    monkeypatch.setenv("POK_DECISION_HARD_DEADLINE_SEC", "0.06")
    monkeypatch.setenv("POK_DECISION_REFINEMENT_BUDGET_SEC", "0.05")
    monkeypatch.setenv("POK_DECISION_BASELINE_TARGET_SEC", "0.01")
    module = _load_native_entry(bot_dir, monkeypatch)
    bot = module.NativeNationalBot("Deadline")

    started = time.monotonic()
    decision = bot._policy_decision()

    assert decision == {"kind": "pass"}
    assert time.monotonic() - started < 0.25
    assert bot._last_decision_metrics["timed_out"] is True
    assert bot._strategy_worker_alive() is False
    bot.close()


@pytest.mark.parametrize(
    "infrastructure_error",
    (
        ManagedExecutorError("managed executor unavailable"),
        IsolationUnavailable("mandatory isolation unavailable"),
        EndpointLeaseError("endpoint lease invalid"),
    ),
    ids=("managed-executor", "isolation", "endpoint-lease"),
)
def test_policy_fixture_propagates_host_infrastructure_errors(
    monkeypatch,
    tmp_path,
    infrastructure_error,
):
    def fail_connect(*_args, **_kwargs):
        raise infrastructure_error

    monkeypatch.setattr(native_tests.EndpointLease, "connect", fail_connect)

    with pytest.raises(type(infrastructure_error), match=str(infrastructure_error)):
        native_tests._run_policy_fixture(
            tmp_path,
            native_tests.POLICY_FIXTURES[0],
        )


def _install_candidate_wire_failure_harness(monkeypatch):
    clients: list[socket.socket] = []

    @contextmanager
    def connect(host, port, *, timeout):
        client = socket.create_connection((host, port), timeout=timeout)
        clients.append(client)
        yield object()

    isolation = IsolationIdentity(
        policy_sha256="0" * 64,
        bpf_sha256="1" * 64,
        bpf_size=1,
    )
    monkeypatch.setattr(native_tests.EndpointLease, "connect", connect)
    monkeypatch.setattr(
        native_tests,
        "launch_managed_bot",
        lambda *_args, **_kwargs: SimpleNamespace(process=None, isolation=isolation),
    )
    return clients


@pytest.mark.parametrize(
    ("candidate_outcome", "expected_error"),
    (
        ("bet 200", "illegal national wire action"),
        ("callcall", "illegal national wire action"),
        (TimeoutError("candidate action timeout"), "candidate action timeout"),
        (ConnectionResetError("candidate disconnected"), "candidate disconnected"),
    ),
    ids=("illegal", "repeated", "timeout", "disconnect"),
)
def test_policy_fixture_classifies_candidate_wire_failures_as_scenario_failures(
    monkeypatch,
    tmp_path,
    candidate_outcome,
    expected_error,
):
    clients = _install_candidate_wire_failure_harness(monkeypatch)
    responses = iter(("NationalFixture", candidate_outcome))

    def receive(*_args, **_kwargs):
        outcome = next(responses)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(native_tests, "_recv_until_idle", receive)
    try:
        result = native_tests._run_policy_fixture(
            tmp_path,
            native_tests.POLICY_FIXTURES[0],
        )
    finally:
        for client in clients:
            client.close()

    assert result["passed"] is False
    assert result["kind"] == "candidate_policy_wire"
    assert expected_error in result["details"]


@pytest.mark.parametrize(
    "interrupt",
    (KeyboardInterrupt(), SystemExit()),
    ids=("keyboard-interrupt", "system-exit"),
)
def test_policy_fixture_does_not_capture_process_control_exceptions(
    monkeypatch,
    tmp_path,
    interrupt,
):
    def fail_connect(*_args, **_kwargs):
        raise interrupt

    monkeypatch.setattr(native_tests.EndpointLease, "connect", fail_connect)

    with pytest.raises(type(interrupt)):
        native_tests._run_policy_fixture(
            tmp_path,
            native_tests.POLICY_FIXTURES[0],
        )
