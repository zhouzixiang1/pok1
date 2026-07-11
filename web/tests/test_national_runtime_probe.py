from pathlib import Path

import national_runtime_probe
from national_native import NATIVE_BOT_TEMPLATE
from national_runtime_probe_scenarios import DECISION_SCENARIOS


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
        "POSTFLOP_TABLE = {i: i / 31.0 for i in range(32)}\n"
        "def get_action(req, current_request_view):\n"
        "    profile = req.get('opponent_runtime', {})\n"
        "    if req.get('public_cards'):\n"
        "        POSTFLOP_TABLE.get(12, 0.0)\n"
        "    if profile.get('adaptation_weight', 0.0) > 0.1 and profile.get('vpip', 0.0) > 0.5:\n"
        "        return -2\n"
        "    return 0\n",
        encoding="utf-8",
    )
    return root


def _artifact_request():
    return [{
        "location": "strategy.py:1",
        "name": "POSTFLOP_TABLE",
        "kind": "module_mapping",
    }]


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

    artifact = first["artifacts"][0]
    assert artifact["entries"] == 32
    assert len(artifact["consumer_scenarios"]) == len(DECISION_SCENARIOS)
    assert artifact["consumer_scenarios"][0]["reads"] == 0
    assert artifact["consumer_scenarios"][1]["reads"] == 0
    assert artifact["consumer_scenarios"][2]["reads"] > 0
    assert artifact["fallback_ok"] is True
    assert len(artifact["fallback_scenarios"]) == len(DECISION_SCENARIOS)
    assert all("error" not in row for row in artifact["fallback_scenarios"])


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
    assert len(calls) == 4


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
