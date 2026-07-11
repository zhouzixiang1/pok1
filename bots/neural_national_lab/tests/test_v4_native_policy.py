from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import v3_native_policy as v3_policy  # noqa: E402
import v4_native_policy as native_policy  # noqa: E402
import v4_runtime_budget as runtime_budget  # noqa: E402
import win_first_policy_v4 as win_first  # noqa: E402


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _authorized_bot(tmp_path: Path) -> Path:
    bundle = tmp_path / native_policy.BUNDLE_FILENAME
    member = {"weights": [1.0]}
    payload = {
        "schema": "test-bundle-schema",
        "format": "test-bundle-format",
        "members": [member],
        "member_payload_sha256": [hashlib.sha256(
            json.dumps(member, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()],
        "calibration": {},
        "source": {
            "preselection_runtime_budget_payload_sha256": "a" * 64,
        },
        "export_contract": {},
    }
    payload["source"]["runtime_identity_sha256"] = (
        runtime_budget.bundle_runtime_identity_sha256(payload)
    )
    _write(bundle, payload)
    budget_artifact = runtime_budget._artifact(
        bundle_bytes=bundle.stat().st_size,
        bundle_sha256=_sha(bundle),
        runtime_identity_sha256=payload["source"]["runtime_identity_sha256"],
        preselection_runtime_budget_payload_sha256="a" * 64,
        source_collection_complete=True,
        cpu_ns=[1] * runtime_budget.MEASURED_REPEATS,
        wall_ns=[2] * runtime_budget.MEASURED_REPEATS,
        warmups_completed=runtime_budget.WARMUP_ROUNDS,
        errors=[],
    )
    budget_path = tmp_path / native_policy.RUNTIME_BUDGET_FILENAME
    _write(budget_path, budget_artifact)
    extra = tmp_path / "national_bot.py"
    extra.write_text("# native\n", encoding="utf-8")
    donor = (
        TOOLS.parent / "versions" /
        "v140_national_v123_overlay_no_large_commit_veto_tcp"
    )
    derived = {}
    for name in ("strategy.py", "neural_policy.py"):
        path = tmp_path / name
        path.write_bytes((donor / name).read_bytes())
        derived[name] = {"bytes": path.stat().st_size, "sha256": _sha(path)}
    critical = {
        name: {
            "bytes": (donor / name).stat().st_size,
            "sha256": _sha(donor / name),
        }
        for name in native_policy.EXPECTED_STRATEGY_CRITICAL
    }
    evidence = tmp_path / "evidence" / "offline_policy_gate"
    native_build_contract = {"schema": "test-native-build-contract"}
    result = {
        "schema": native_policy.GATE_RESULT_SCHEMA,
        "passed": True,
        "errors": [],
        "native_candidate_build_authorized": True,
        "bundle_bytes": bundle.stat().st_size,
        "bundle_sha256": _sha(bundle),
        "native_build_contract": native_build_contract,
        "preselection_runtime_budget_payload_sha256": "a" * 64,
        "runtime_identity_sha256": payload["source"][
            "runtime_identity_sha256"
        ],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    _write(evidence / "policy_gate_result.json", result)
    common_gate_binding = {
        "bundle_bytes": bundle.stat().st_size,
        "bundle_sha256": _sha(bundle),
        "native_build_contract": native_build_contract,
        "preselection_runtime_budget_payload_sha256": "a" * 64,
        "runtime_identity_sha256": payload["source"][
            "runtime_identity_sha256"
        ],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    _write(evidence / "policy_gate_evaluation.json", {
        "schema": native_policy.GATE_EVALUATION_SCHEMA,
        "source_collection_complete": True,
        "policy_search_performed": False,
        **common_gate_binding,
    })
    _write(evidence / "policy_gate_report.json", {
        "schema": native_policy.GATE_REPORT_SCHEMA,
        "gate_passed": True,
        "gate_errors": [],
        "native_candidate_build_authorized": True,
        "source_collection_complete": True,
        **common_gate_binding,
    })
    gate_files = {
        name: {
            "bytes": (evidence / name).stat().st_size,
            "sha256": _sha(evidence / name),
        }
        for name in (
            "policy_gate_evaluation.json",
            "policy_gate_result.json",
            "policy_gate_report.json",
        )
    }
    _write(evidence / "artifact_manifest.json", {
        "schema": native_policy.GATE_ARTIFACT_SCHEMA,
        "files": gate_files,
        "native_candidate_build_authorized": True,
        **common_gate_binding,
    })
    authorization = {
        "bundle_bytes": bundle.stat().st_size,
        "bundle_sha256": _sha(bundle),
        "gate_artifact_manifest_sha256": _sha(evidence / "artifact_manifest.json"),
        "gate_evaluation_sha256": _sha(evidence / "policy_gate_evaluation.json"),
        "gate_result_sha256": _sha(evidence / "policy_gate_result.json"),
        "gate_report_sha256": _sha(evidence / "policy_gate_report.json"),
        "native_build_contract": native_build_contract,
        "preselection_runtime_budget_payload_sha256": "a" * 64,
        "runtime_identity_sha256": payload["source"][
            "runtime_identity_sha256"
        ],
        "final_runtime_budget_payload_sha256": budget_artifact[
            "payload_sha256"
        ],
        "final_runtime_budget_file_sha256": _sha(budget_path),
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    _write(tmp_path / native_policy.BUILD_MANIFEST_FILENAME, {
        "schema": native_policy.BUILD_SCHEMA,
        "candidate_artifacts": {
            path.name: {"bytes": path.stat().st_size, "sha256": _sha(path)}
            for path in (bundle, budget_path, extra)
        },
        "authorization": authorization,
        "native_build_contract": native_build_contract,
        "strategy_donor": {
            "sha256": native_policy.EXPECTED_STRATEGY_DONOR_SHA256,
            "critical_files": critical,
        },
        "strategy_donor_derived_files": derived,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_strength_evidence": False,
        "official_exe_accepted": False,
        "deployment_eligible": False,
    })
    return tmp_path


def _request() -> dict:
    return {
        "num_players": 2,
        "dealer_id": 0,
        "my_id": 0,
        "my_chips": 19_950,
        "opponent_chips": 19_900,
        "my_cards": [0, 5],
        "public_cards": [],
        "history": [],
        "hand": 0,
        "max_hand": 70,
        "remaining_hands": 70,
        "total_win_chips": [0, 0],
        "opponent_showdowns": [],
        "cross_hand_sequence": [],
        "opponent_profile": {
            "confidence": 0.5,
            "actions_total_norm": 0.25,
            "fold_rate": 0.2,
            "call_rate": 0.3,
            "check_rate": 0.2,
            "raise_rate": 0.2,
            "allin_rate": 0.1,
            "aggression": 0.3,
            "preflop_actions_norm": 0.2,
            "preflop_raise_rate": 0.25,
            "postflop_actions_norm": 0.1,
            "postflop_raise_rate": 0.2,
        },
        "my_stage_bet": 50,
        "opponent_stage_bet": 100,
        "pot": 150,
        "to_call": 50,
        "opponent_allin": False,
    }


def _state() -> dict:
    return {
        "round": 0,
        "round_bet": 100,
        "round_raise": 100,
        "min_raise_action": 151,
        "my_round_bet": 50,
        "to_call": 50,
        "pot": 150,
        "stacks": [19_950, 19_900],
        "opponent_allin": False,
    }


def _policy() -> dict:
    return {
        "schema": win_first.POLICY_SCHEMA,
        "selection_priority": win_first.SELECTION_PRIORITY,
        "min_positive_probability_lcb": 0.5,
        "min_probability_uplift_lcb": 0.0,
        "chip_margin": 0.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.5,
        "response_weight": 0.0,
        "min_hand_lcb": 0.0,
        "use_lower": True,
    }


class _Runtime:
    policy = _policy()

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.value_inputs = None
        self.outcome_inputs = None
        self.selection = None

    def predict_values(self, **inputs):
        if self.fail:
            raise RuntimeError("v4 inference failed")
        self.value_inputs = inputs
        lower = [0.0] * 6
        lower[3] = 10.0
        return {
            field: {"lower": list(lower)}
            for field in (
                "delta_vs_rule",
                "tail_delta_vs_rule",
                "match_delta_vs_rule",
            )
        }

    def predict_match_outcomes(self, **inputs):
        self.outcome_inputs = inputs
        return win_first.aggregate_member_probabilities(
            [[0.1, 0.1, 0.2, 0.8, 0.1, 0.1]],
            uncertainty_std_weight=0.0,
        )

    def select_candidate(
        self, values, outcomes, candidates, *, rule_label_id
    ):
        self.selection = {
            "values": values,
            "outcomes": outcomes,
            "candidates": candidates,
            "rule_label_id": rule_label_id,
        }
        return win_first.select_candidate(
            self.policy,
            outcomes,
            values,
            candidates,
            rule_label_id=rule_label_id,
        )


def test_native_v4_uses_shared_win_first_selector_and_exact_context() -> None:
    runtime = _Runtime()
    native = native_policy.NativeV4Policy(runtime)
    captured = {
        "schema": v3_policy.STRATEGY_CONTEXT_SCHEMA,
        "features": [0.25] * 66,
    }

    action = native.advise(_request(), _state(), 0, captured)

    assert action > _state()["round_bet"]
    assert runtime.value_inputs == runtime.outcome_inputs
    assert len(runtime.value_inputs["state"]) == 81
    assert len(runtime.value_inputs["profile"]) == 12
    assert len(runtime.value_inputs["rule_action"]) == 6
    assert len(runtime.value_inputs["strategy_context"]) == 66
    assert runtime.value_inputs["strategy_context"] == [0.0] * 66
    assert runtime.selection["rule_label_id"] == 1
    assert native.last_decision["used"] is True


def test_native_v4_inference_failure_returns_same_sanitized_rule() -> None:
    native = native_policy.NativeV4Policy(_Runtime(fail=True))

    assert native.advise(_request(), _state(), 201, {}) == 201
    assert native.last_decision["rule_action"] == 201
    assert native.last_decision["used"] is False
    assert "v4 inference failed" in native.last_decision["error"]


def test_native_v4_does_not_resanitize_observed_raise_total(monkeypatch) -> None:
    native = native_policy.NativeV4Policy(_Runtime(fail=True))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("baseline must not be sanitized again")

    monkeypatch.setattr(native_policy, "sanitize_stage_total", forbidden)
    assert native.advise(_request(), _state(), 201, {}) == 201
    assert calls == []


def test_native_v4_rejects_action_that_changes_during_final_sanitize(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    native = native_policy.NativeV4Policy(runtime)
    monkeypatch.setattr(
        native_policy,
        "candidate_actions",
        lambda request, state, safe: [{"label_id": 3, "action": 50_000}],
    )

    assert native.advise(_request(), _state(), 0, {}) == 0
    assert native.last_decision["used"] is False
    assert "changed during final sanitization" in native.last_decision["error"]


def test_native_v4_disable_env_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("POK_V4_DISABLE", "1")

    assert native_policy.NativeV4Policy.load("missing.json") is None


def test_native_v4_load_accepts_path_and_authorized_payload_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class RuntimeFactory:
        def __new__(cls, payload):
            calls.append(("payload", payload))
            return _Runtime()

        @classmethod
        def load(cls, path):
            calls.append(("path", path))
            return _Runtime()

    monkeypatch.setattr(
        native_policy, "OpponentMultiTaskEnsembleRuntimeV4", RuntimeFactory
    )
    payload = {"authorized": "snapshot"}

    from_path = native_policy.NativeV4Policy.load("bundle.json")
    from_snapshot = native_policy.NativeV4Policy.load(payload)

    assert isinstance(from_path, native_policy.NativeV4Policy)
    assert isinstance(from_snapshot, native_policy.NativeV4Policy)
    assert calls == [("path", "bundle.json"), ("payload", payload)]


@pytest.mark.parametrize("malformed", ["authorization", "gate_result"])
def test_formal_native_loader_returns_none_for_malformed_json_subobjects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: str,
) -> None:
    bot = _authorized_bot(tmp_path)
    manifest_path = bot / native_policy.BUILD_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if malformed == "authorization":
        manifest["authorization"] = []
    else:
        result_path = (
            bot / "evidence" / "offline_policy_gate" /
            "policy_gate_result.json"
        )
        _write(result_path, [])
        manifest["authorization"]["gate_result_sha256"] = _sha(result_path)
        artifact_path = (
            bot / "evidence" / "offline_policy_gate" /
            "artifact_manifest.json"
        )
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["files"]["policy_gate_result.json"] = {
            "bytes": result_path.stat().st_size,
            "sha256": _sha(result_path),
        }
        _write(artifact_path, artifact)
        manifest["authorization"]["gate_artifact_manifest_sha256"] = _sha(
            artifact_path
        )
    _write(manifest_path, manifest)
    model_loads = []
    monkeypatch.setattr(
        native_policy.NativeV4Policy,
        "load",
        lambda source: model_loads.append(source),
    )

    assert native_policy.load_native_v4_policy(bot) is None
    assert model_loads == []


def test_formal_native_loader_uses_one_bundle_snapshot_after_hash_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _authorized_bot(tmp_path)
    bundle_path = bot / native_policy.BUNDLE_FILENAME
    original_payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    alternate_payload = json.loads(json.dumps(original_payload))
    alternate_payload["selected_policy"] = {"same_identity_alternate": True}
    assert runtime_budget.bundle_runtime_identity_sha256(
        alternate_payload
    ) == runtime_budget.bundle_runtime_identity_sha256(original_payload)

    original_snapshot = native_policy._file_snapshot
    snapshot_calls = []
    swapped = False

    def snapshot_then_swap(root: Path, name, *, top_level=False):
        nonlocal swapped
        raw = original_snapshot(root, name, top_level=top_level)
        snapshot_calls.append(name)
        if name == native_policy.BUNDLE_FILENAME and not swapped:
            swapped = True
            _write(bundle_path, alternate_payload)
        return raw

    monkeypatch.setattr(native_policy, "_file_snapshot", snapshot_then_swap)
    loaded_payloads = []
    sentinel = object()

    def load_snapshot(source):
        loaded_payloads.append(source)
        return sentinel

    monkeypatch.setattr(native_policy.NativeV4Policy, "load", load_snapshot)

    assert native_policy.load_native_v4_policy(bot) is sentinel
    assert swapped is True
    assert snapshot_calls.count(native_policy.BUNDLE_FILENAME) == 1
    assert snapshot_calls.count(native_policy.RUNTIME_BUDGET_FILENAME) == 1
    for name in (
        "artifact_manifest.json",
        "policy_gate_evaluation.json",
        "policy_gate_result.json",
        "policy_gate_report.json",
    ):
        assert snapshot_calls.count(f"evidence/offline_policy_gate/{name}") == 1
    assert len(loaded_payloads) == 1
    assert isinstance(loaded_payloads[0], dict)
    assert "selected_policy" not in loaded_payloads[0]
    assert json.loads(bundle_path.read_text(encoding="utf-8"))[
        "selected_policy"
    ] == alternate_payload["selected_policy"]


def test_formal_native_loader_rejects_post_build_bundle_rehash(
    tmp_path: Path, monkeypatch,
) -> None:
    bot = _authorized_bot(tmp_path)
    sentinel = object()
    monkeypatch.setattr(
        native_policy.NativeV4Policy, "load", lambda path: sentinel
    )
    assert native_policy.load_native_v4_policy(bot) is sentinel

    bundle = bot / native_policy.BUNDLE_FILENAME
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    payload["members"][0]["weights"] = [999.0]
    payload["member_payload_sha256"][0] = hashlib.sha256(
        json.dumps(
            payload["members"][0], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    _write(bundle, payload)
    assert native_policy.load_native_v4_policy(bot) is None


@pytest.mark.parametrize(
    "tamper",
    ["missing", "content", "manifest_contract", "authorization_identity"],
)
def test_formal_native_loader_falls_back_on_missing_or_tampered_budget_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str,
) -> None:
    bot = _authorized_bot(tmp_path)
    budget_path = bot / native_policy.RUNTIME_BUDGET_FILENAME
    if tamper == "missing":
        budget_path.unlink()
    elif tamper == "content":
        payload = json.loads(budget_path.read_text(encoding="utf-8"))
        payload["measurements"]["max_full_decision_cpu_ns"] += 1
        _write(budget_path, payload)
    elif tamper == "manifest_contract":
        manifest_path = bot / native_policy.BUILD_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["candidate_artifacts"][
            native_policy.RUNTIME_BUDGET_FILENAME
        ] = None
        _write(manifest_path, manifest)
    else:
        manifest_path = bot / native_policy.BUILD_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["authorization"]["runtime_identity_sha256"] = "f" * 64
        _write(manifest_path, manifest)
    model_loads = []
    monkeypatch.setattr(
        native_policy.NativeV4Policy,
        "load",
        lambda path: model_loads.append(path),
    )

    assert native_policy.load_native_v4_policy(bot) is None
    assert model_loads == []


def test_formal_native_loader_validates_sidecar_without_rerunning_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _authorized_bot(tmp_path)
    benchmark_calls = []

    def forbidden_benchmark(*args, **kwargs):
        benchmark_calls.append((args, kwargs))
        raise AssertionError("native startup must not rerun the runtime benchmark")

    for name in (
        "measure_runtime_budget",
        "measure_bundle_runtime_budget",
        "measure_bundle_runtime_budget_subprocess",
    ):
        monkeypatch.setattr(runtime_budget, name, forbidden_benchmark)
    sentinel = object()
    monkeypatch.setattr(
        native_policy.NativeV4Policy, "load", lambda path: sentinel
    )

    assert native_policy.load_native_v4_policy(bot) is sentinel
    assert benchmark_calls == []


def test_formal_native_loader_rejects_candidate_file_tamper(
    tmp_path: Path, monkeypatch,
) -> None:
    bot = _authorized_bot(tmp_path)
    monkeypatch.setattr(
        native_policy.NativeV4Policy, "load", lambda path: object()
    )
    (bot / "national_bot.py").write_text("# drift\n", encoding="utf-8")
    assert native_policy.load_native_v4_policy(bot) is None


@pytest.mark.parametrize("name", ["strategy.py", "neural_policy.py"])
def test_formal_native_loader_rejects_baseline_file_tamper(
    tmp_path: Path, name: str,
) -> None:
    bot = _authorized_bot(tmp_path)
    (bot / name).write_text("# drift\n", encoding="utf-8")
    assert native_policy.load_native_v4_policy(bot) is None


def test_formal_native_loader_rejects_donor_path_traversal(
    tmp_path: Path,
) -> None:
    bot = _authorized_bot(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}_outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    manifest_path = bot / native_policy.BUILD_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["strategy_donor_derived_files"][f"../{outside.name}"] = {
        "bytes": outside.stat().st_size,
        "sha256": _sha(outside),
    }
    _write(manifest_path, manifest)
    assert native_policy.load_native_v4_policy(bot) is None
    outside.unlink()


def test_formal_native_loader_rejects_donor_symlink(tmp_path: Path) -> None:
    bot = _authorized_bot(tmp_path)
    strategy = bot / "strategy.py"
    copy = bot / "strategy_copy.py"
    copy.write_bytes(strategy.read_bytes())
    strategy.unlink()
    strategy.symlink_to(copy.name)
    assert native_policy.load_native_v4_policy(bot) is None
