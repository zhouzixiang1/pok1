from pathlib import Path
from types import SimpleNamespace

import pytest

import national_runtime_probe
import national_runtime_probe_worker
from national_native import NATIVE_BOT_TEMPLATE
from national_runtime_probe_scenarios import DECISION_SCENARIOS, LINE_SCENARIO_PAIRS


def _write_probe_bot(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "national_bot.py").write_text(NATIVE_BOT_TEMPLATE, encoding="utf-8")
    (root / "main.py").write_text(
        "def sanitize_action(action, state, chips): return int(action)\n",
        encoding="utf-8",
    )
    (root / "state.py").write_text(
        "def reconstruct_state(req): return dict(req)\n"
        "def infer_remaining_hands_from_requests(requests): return 1\n",
        encoding="utf-8",
    )
    (root / "strategy.py").write_text(
        "import time\n"
        "POSTFLOP_TABLE = {i: i / 31.0 for i in range(32)}\n"
        "def _choose(req):\n"
        "    profile = req.get('opponent_runtime', {})\n"
        "    hand = req.get('hand_runtime', {})\n"
        "    if req.get('public_cards'):\n"
        "        POSTFLOP_TABLE.get(12, 0.0)\n"
        "    if hand.get('can_donk'):\n"
        "        return 600\n"
        "    if hand.get('can_delayed_probe'):\n"
        "        return 500\n"
        "    if profile.get('fold_to_jam_samples', 0) >= 10:\n"
        "        pressure = profile.get('fold_to_raise', 0.0) + profile.get('fold_to_jam_rate', 0.0) - profile.get('river_overcall_freq', 0.0)\n"
        "        return -2 if pressure > 0.5 else 0\n"
        "    revealed = profile.get('showdown_range', {})\n"
        "    if (revealed.get('selection_scope') == 'reached_showdown_only' and revealed.get('confidence', 0.0) > 0.1 and revealed.get('adaptation_weight', 0.0) > 0.0 and revealed.get('samples', 0) >= 10 and revealed.get('tightness', 0.0) > 0.30):\n"
        "        return -1\n"
        "    if profile.get('adaptation_weight', 0.0) > 0.1 and profile.get('vpip', 0.0) > 0.5:\n"
        "        return -2\n"
        "    return 0\n"
        "def get_action(req, current_request_view):\n"
        "    return _choose(req)\n"
        "def get_baseline_action(req, current_request_view):\n"
        "    return _choose(req)\n"
        "def iter_refinements(req, current_request_view, baseline, deadline):\n"
        "    refined = -1 if baseline == 0 and req.get('to_call', 0) > 0 else baseline\n"
        "    for samples in range(1, 17):\n"
        "        if time.monotonic() >= deadline:\n"
        "            return\n"
        "        work = 0\n"
        "        for outer in range(100):\n"
        "            for unit in range(100):\n"
        "                for lane in range(4):\n"
        "                    work += (outer * unit * samples + lane) % 17\n"
        "        if work < 0:\n"
        "            refined = baseline\n"
        "        yield {'action': refined, 'sample_count': samples, 'confidence': samples / 16.0, 'complete': samples == 16}\n",
        encoding="utf-8",
    )
    return root


def _artifact_request():
    return [{
        "location": "strategy.py:1",
        "name": "POSTFLOP_TABLE",
        "kind": "module_mapping",
    }]


def _write_value_sensitive_probe_bot(root: Path) -> Path:
    """Make the existing small mapping change a real postflop wire action."""
    bot = _write_probe_bot(root)
    strategy_path = bot / "strategy.py"
    source = strategy_path.read_text(encoding="utf-8")
    source = source.replace(
        "        POSTFLOP_TABLE.get(12, 0.0)\n",
        "        score = POSTFLOP_TABLE.get(len(req.get('public_cards') or []), 0.0)\n"
        "        if score > 0.0:\n"
        "            return 600\n",
    )
    strategy_path.write_text(source, encoding="utf-8")
    return bot


def _write_packed_value_sensitive_probe_bot(root: Path) -> Path:
    """Use a packed row, matching the planned preflop-equity asset shape."""
    bot = _write_probe_bot(root)
    strategy_path = bot / "strategy.py"
    source = strategy_path.read_text(encoding="utf-8")
    source = source.replace(
        "POSTFLOP_TABLE = {i: i / 31.0 for i in range(32)}",
        "POSTFLOP_TABLE = {i: bytes([i + 1]) for i in range(32)}",
    )
    source = source.replace(
        "        POSTFLOP_TABLE.get(12, 0.0)\n",
        "        packed = POSTFLOP_TABLE.get(len(req.get('public_cards') or []), b'\\x00')\n"
        "        if packed and packed[0] > 128:\n"
        "            return 600\n",
    )
    strategy_path.write_text(source, encoding="utf-8")
    return bot


def _write_fixed_key_value_sensitive_probe_bot(root: Path) -> Path:
    """A table whose values matter, but whose lookup is a fake constant."""
    bot = _write_probe_bot(root)
    strategy_path = bot / "strategy.py"
    source = strategy_path.read_text(encoding="utf-8")
    source = source.replace(
        "POSTFLOP_TABLE = {i: i / 31.0 for i in range(32)}",
        "POSTFLOP_TABLE = {i: 1 for i in range(32)}",
    )
    source = source.replace(
        "        POSTFLOP_TABLE.get(12, 0.0)\n",
        "        score = POSTFLOP_TABLE.get(0, 0)\n"
        "        if score > 0:\n"
        "            return 600\n",
    )
    strategy_path.write_text(source, encoding="utf-8")
    return bot


@pytest.mark.parametrize(
    ("baseline_ms", "expected_issue"),
    [
        (251.0, "strategy_baseline_slower_than_250ms"),
        (None, "strategy_baseline_never_published"),
    ],
)
def test_baseline_latency_or_absence_does_not_poison_killable_safety(
    monkeypatch,
    baseline_ms,
    expected_issue,
):
    class FakeBot:
        def _ensure_strategy_worker(self):
            return None

    strategy = SimpleNamespace(
        get_action=lambda *_args: 0,
        get_baseline_action=lambda *_args: 0,
        iter_refinements=lambda *_args: iter(()),
    )
    imports = SimpleNamespace(load=lambda name: strategy)
    metric = {
        "runtime_version": 7,
        "socket_fallback_ready_ms": 1.0,
        "baseline_published_ms": baseline_ms,
    }
    strategy_result = {
        "rows": [{"baseline": {"runtime_metrics": metric}}],
    }
    actions = iter([
        {
            "wire": "fold",
            "runtime_metrics": {
                "trusted_refinement_steps": 8,
                "trusted_refinement_elapsed_ms": 8.0,
                "refinement_action_changes": 1,
            },
        },
        {
            "wire": "fold",
            "runtime_metrics": {
                "trusted_refinement_steps": 16,
                "trusted_refinement_elapsed_ms": 16.0,
                "refinement_action_changes": 1,
            },
        },
        {
            "wire": "fold",
            "runtime_metrics": {
                "decision_id": 3,
                "worker_generation": 1,
                "timed_out": True,
                "worker_terminated": True,
            },
        },
        {
            "wire": "check",
            "runtime_metrics": {
                "decision_id": 4,
                "worker_generation": 2,
                "timed_out": False,
            },
        },
    ])
    monkeypatch.setattr(
        national_runtime_probe_worker,
        "_new_native_bot",
        lambda _imports: (None, FakeBot()),
    )
    monkeypatch.setattr(
        national_runtime_probe_worker,
        "_timed_formal_action",
        lambda *_args, **_kwargs: next(actions),
    )

    result = national_runtime_probe_worker._probe_decision_runtime(
        imports,
        strategy_result,
        expected_runtime_version=7,
    )

    assert result["safety_ok"] is True
    assert result["safety_issues"] == []
    assert result["baseline_ok"] is False
    assert result["baseline_issues"] == [expected_issue]
    assert result["refinement_ok"] is True
    assert result["issues"] == [expected_issue]
    assert result["ok"] is False


def test_runtime_scenario_bank_uses_coherent_national_transcripts():
    by_id = {scenario["id"]: scenario for scenario in DECISION_SCENARIOS}
    for scenario in DECISION_SCENARIOS:
        hero_blind = 50 if scenario["is_sb"] else 100
        opponent_blind = 100 if scenario["is_sb"] else 50
        committed = [hero_blind, opponent_blind]
        rounds = []
        for record in scenario["history"]:
            assert record["player_id"] in {0, 1}
            rounds.append(record["round"])
            committed[record["player_id"]] += int(record.get("committed", 0) or 0)
        assert rounds == sorted(rounds)
        assert sum(committed) == scenario["pot"], scenario["id"]
        assert max(0, scenario["opponent_stage_bet"] - scenario["my_stage_bet"]) >= 0

    for pair in LINE_SCENARIO_PAIRS:
        positive = by_id[pair["positive"]]
        negative = by_id[pair["negative"]]
        assert positive["stage"] == negative["stage"]
        assert positive["my_cards"] == negative["my_cards"]
        assert positive["public_cards"] == negative["public_cards"]
        assert positive["pot"] == negative["pot"]
        assert positive["expected_hand_runtime"][pair["flag"]] is True
        assert negative["expected_hand_runtime"][pair["flag"]] is False


def test_probe_measures_postflop_consumer_across_scenario_bank_and_caches(tmp_path):
    bot = _write_probe_bot(tmp_path / "national_v1")
    national_runtime_probe.clear_runtime_probe_cache()

    first = national_runtime_probe.run_national_runtime_probe(
        bot,
        static_artifacts=_artifact_request(),
    )
    second = national_runtime_probe.run_national_runtime_probe(
        bot,
        static_artifacts=_artifact_request(),
    )

    assert first["ok"] is True
    assert first["cache_hit"] is False
    assert second["ok"] is True
    assert second["cache_hit"] is True
    assert second["cache_key"] == first["cache_key"]
    multifidelity = first["decision_runtime"]["budget_scaling"]
    assert multifidelity["probe_kind"] == "sampled_multifidelity_2s_vs_8s"
    assert multifidelity["short_budget"]["hard_deadline_sec"] == 2.0
    assert multifidelity["long_budget"]["hard_deadline_sec"] == 8.0
    assert multifidelity["worker_seed_equal"] is True

    artifact = first["artifacts"][0]
    assert artifact["entries"] == 32
    assert len(artifact["consumer_scenarios"]) == len(DECISION_SCENARIOS)
    assert artifact["consumer_scenarios"][0]["reads"] == 0
    assert artifact["consumer_scenarios"][1]["reads"] == 0
    assert artifact["consumer_scenarios"][2]["reads"] > 0
    assert artifact["fallback_ok"] is True
    assert len(artifact["fallback_scenarios"]) == len(DECISION_SCENARIOS)
    assert all("error" not in row for row in artifact["fallback_scenarios"])
    assert artifact["value_affects_final_wire"] is False
    assert national_runtime_probe.validate_dynamic_precompute_contract(
        first,
        name="POSTFLOP_TABLE",
        owner_file="strategy.py",
        build_phase="module_import",
        max_build_ms=2_500,
        max_entries=65_536,
        max_bytes=8 * 1024 * 1024,
        key_shape="int",
        fallback="legal_baseline",
        require_action_influence=True,
    ) == ["dynamic_precompute_value_no_final_wire_influence"]


def test_probe_requires_value_sensitive_final_wire_counterfactual_for_primary(tmp_path):
    bot = _write_value_sensitive_probe_bot(tmp_path / "national_v_value_sensitive")
    national_runtime_probe.clear_runtime_probe_cache()

    result = national_runtime_probe.run_national_runtime_probe(
        bot,
        static_artifacts=_artifact_request(),
    )

    # This fixture deliberately changes the existing generic strategy's line
    # controls, so its unrelated strategy-influence gate can fail.  The
    # artifact assertion below is intentionally scoped to the precompute
    # counterfactual contract.
    assert result["failure_class"] != "probe_infra"
    artifact = result["artifacts"][0]
    assert artifact["value_affects_final_wire"] is True
    assert artifact["action_influence_scenarios"]
    assert artifact["lookup_key_varies_across_consumer_scenarios"] is True
    assert national_runtime_probe.validate_dynamic_precompute_contract(
        result,
        name="POSTFLOP_TABLE",
        owner_file="strategy.py",
        build_phase="module_import",
        max_build_ms=2_500,
        max_entries=65_536,
        max_bytes=8 * 1024 * 1024,
        key_shape="int",
        fallback="legal_baseline",
        require_action_influence=True,
    ) == []


def test_probe_rejects_constant_key_lookup_even_when_mutated_values_change_wire(tmp_path):
    bot = _write_fixed_key_value_sensitive_probe_bot(tmp_path / "national_v_fixed_key")
    national_runtime_probe.clear_runtime_probe_cache()

    result = national_runtime_probe.run_national_runtime_probe(
        bot,
        static_artifacts=_artifact_request(),
    )

    assert result["failure_class"] != "probe_infra"
    artifact = result["artifacts"][0]
    assert artifact["value_affects_final_wire"] is True
    assert artifact["lookup_key_varies_across_consumer_scenarios"] is False
    assert national_runtime_probe.validate_dynamic_precompute_contract(
        result,
        name="POSTFLOP_TABLE",
        owner_file="strategy.py",
        build_phase="module_import",
        max_build_ms=2_500,
        max_entries=65_536,
        max_bytes=8 * 1024 * 1024,
        key_shape="int",
        fallback="legal_baseline",
        require_action_influence=True,
        require_key_variation=True,
    ) == ["dynamic_precompute_lookup_key_static"]


def test_probe_counterfactual_mutates_packed_bytes_rows_for_live_action_proof(tmp_path):
    bot = _write_packed_value_sensitive_probe_bot(tmp_path / "national_v_packed_value")
    national_runtime_probe.clear_runtime_probe_cache()

    result = national_runtime_probe.run_national_runtime_probe(
        bot,
        static_artifacts=_artifact_request(),
    )

    assert result["failure_class"] != "probe_infra"
    artifact = result["artifacts"][0]
    assert artifact["value_affects_final_wire"] is True
    assert artifact["action_influence_scenarios"]
    assert artifact["lookup_key_varies_across_consumer_scenarios"] is True
    assert national_runtime_probe.validate_dynamic_precompute_contract(
        result,
        name="POSTFLOP_TABLE",
        owner_file="strategy.py",
        build_phase="module_import",
        max_build_ms=2_500,
        max_entries=65_536,
        max_bytes=8 * 1024 * 1024,
        key_shape="int",
        fallback="legal_baseline",
        require_action_influence=True,
    ) == []


def test_probe_infrastructure_failure_is_never_cached(monkeypatch, tmp_path):
    bot = _write_probe_bot(tmp_path / "national_v2")
    national_runtime_probe.clear_runtime_probe_cache()
    calls = []

    def _infra(*_args, **_kwargs):
        calls.append(True)
        return {
            "ok": False,
            "failure_class": "probe_infra",
            "issues": ["bwrap unavailable"],
        }

    monkeypatch.setattr(national_runtime_probe, "_run_once", _infra)

    first = national_runtime_probe.run_national_runtime_probe(bot)
    second = national_runtime_probe.run_national_runtime_probe(bot)

    assert first["failure_class"] == "probe_infra"
    assert second["failure_class"] == "probe_infra"
    assert len(calls) == 2


def test_probe_cache_is_bound_to_code_fingerprint(monkeypatch, tmp_path):
    bot = _write_probe_bot(tmp_path / "national_v3")
    national_runtime_probe.clear_runtime_probe_cache()
    calls = []

    def _passing(_root, spec, _timeout):
        calls.append(spec["code_fingerprint"])
        return {
            "ok": True,
            "failure_class": "none",
            "issues": [],
            "schema_version": 2,
            "worker_version": 1,
            "scenario_version": 1,
            "scenario_digest": spec["scenario_digest"],
            "spec_digest": spec["spec_digest"],
            "code_fingerprint": spec["code_fingerprint"],
            "artifacts": [],
            "tracker": {},
            "strategy_influence": {},
        }

    monkeypatch.setattr(national_runtime_probe, "_run_once", _passing)
    first = national_runtime_probe.run_national_runtime_probe(bot)
    cached = national_runtime_probe.run_national_runtime_probe(bot)
    (bot / "strategy.py").write_text(
        (bot / "strategy.py").read_text(encoding="utf-8") + "\nNEW_POLICY = 1\n",
        encoding="utf-8",
    )
    changed = national_runtime_probe.run_national_runtime_probe(bot)

    assert first["cache_hit"] is False
    assert cached["cache_hit"] is True
    assert changed["cache_hit"] is False
    assert first["code_fingerprint"] != changed["code_fingerprint"]
    assert len(calls) == 4


def test_probe_cache_fingerprint_covers_nested_binary_artifacts(tmp_path):
    import national_runtime_probe

    bot = tmp_path / "national_v1"
    bot.mkdir()
    (bot / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
    tables = bot / "tables"
    tables.mkdir()
    policy = tables / "equity.bin"
    policy.write_bytes(b"\x00\xffpolicy-one")

    before = national_runtime_probe._bot_code_fingerprint(bot)
    policy.write_bytes(b"\x00\xffpolicy-two")

    assert national_runtime_probe._bot_code_fingerprint(bot) != before
