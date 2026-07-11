#!/usr/bin/env python3
"""Build a native v3 candidate only from a passing, hash-bound policy gate."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import py_compile
import re
import shutil
import tempfile
from typing import Any

from opponent_multitask_ensemble_runtime_v3 import (
    ENSEMBLE_FORMAT,
    OpponentMultiTaskEnsembleRuntimeV3,
)


ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
VERSIONS = ROOT / "bots" / "neural_national_lab" / "versions"
DEFAULT_STRATEGY_DONOR = (
    VERSIONS / "v152_national_v140_strategy_context_trace_tcp"
)
DEFAULT_TRANSPORT_DONOR = (
    VERSIONS / "v151_national_v150_temporal_multitask_shadow_tcp"
)
BUILD_SCHEMA = "opponent_multitask_v3_native_candidate_build_v1"
BUNDLE_SCHEMA = "opponent_multitask_stdlib_ensemble_export_v1"
GATE_RESULT_SCHEMA = "policy_gate_result_v1"
GATE_EVALUATION_SCHEMA = "opponent_multitask_v3_policy_gate_evaluation_v1"
GATE_REPORT_SCHEMA = "opponent_multitask_v3_policy_gate_report_v1"
GATE_ARTIFACT_SCHEMA = "opponent_multitask_v3_policy_gate_artifacts_v1"
VERSION_RE = re.compile(r"^v\d+_[a-z0-9_]+$")
COPIED_TOOL_MODULES = (
    "feature_spec.py",
    "decision_context_features.py",
    "hand_context_features.py",
    "history_feature_schema.py",
    "state_feature_schema.py",
    "model_input_schema.py",
    "opponent_profile_schema.py",
    "cross_hand_sequence.py",
    "strategy_context_schema.py",
    "opponent_multitask_runtime_v3.py",
    "opponent_multitask_ensemble_runtime_v3.py",
    "v3_native_policy.py",
)
FORBIDDEN_RUNTIME_IMPORTS = {"bot_adapter", "numpy", "torch", "sever"}


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(
        entry
        for entry in path.rglob("*")
        if entry.is_file()
        and "__pycache__" not in entry.parts
        and item_suffix(entry) != ".pyc"
    ):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def item_suffix(path: Path) -> str:
    return path.suffix.lower()


def _load_json(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {field}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    return value


def _digest(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return text


def _verify_manifest_files(root: Path, manifest: dict[str, Any]) -> None:
    expected = {
        "policy_gate_evaluation.json",
        "policy_gate_result.json",
        "policy_gate_report.json",
    }
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != expected:
        raise ValueError("policy gate artifact file set is invalid")
    for name, contract in files.items():
        path = root / name
        if (
            not path.is_file()
            or not isinstance(contract, dict)
            or int(contract.get("bytes", -1)) != path.stat().st_size
            or _digest(contract.get("sha256"), field=f"{name} sha256")
            != _sha256(path)
        ):
            raise ValueError(f"policy gate artifact changed: {name}")


def verify_build_authorization(
    gate_dir: Path, bundle_path: Path
) -> dict[str, Any]:
    gate_root = gate_dir.resolve()
    bundle_path = bundle_path.resolve()
    artifact = _load_json(gate_root / "artifact_manifest.json", field="gate manifest")
    evaluation_path = gate_root / "policy_gate_evaluation.json"
    result_path = gate_root / "policy_gate_result.json"
    report_path = gate_root / "policy_gate_report.json"
    evaluation = _load_json(evaluation_path, field="gate evaluation")
    result = _load_json(result_path, field="gate result")
    report = _load_json(report_path, field="gate report")
    bundle = _load_json(bundle_path, field="v3 ensemble bundle")
    _verify_manifest_files(gate_root, artifact)

    selected = evaluation.get("selected_policy")
    selected_sha = _canonical_sha256(selected) if isinstance(selected, dict) else None
    result_sha = _sha256(result_path)
    run_id = result.get("run_id")
    role_manifest_sha = result.get("role_manifest_sha256")
    if (
        artifact.get("schema") != GATE_ARTIFACT_SCHEMA
        or artifact.get("native_candidate_build_authorized") is not True
        or artifact.get("deployment_policy_value") is not False
        or artifact.get("strength_evidence") is not False
        or result.get("schema") != GATE_RESULT_SCHEMA
        or result.get("passed") is not True
        or result.get("errors") != []
        or result.get("native_candidate_build_authorized") is not True
        or result.get("selected_policy_sha256") != selected_sha
        or result.get("evaluation_report_sha256") != _canonical_sha256(evaluation)
        or result.get("deployment_policy_value") is not False
        or result.get("strength_evidence") is not False
        or evaluation.get("schema") != GATE_EVALUATION_SCHEMA
        or evaluation.get("config") != selected
        or evaluation.get("source_collection_complete") is not True
        or evaluation.get("policy_search_performed") is not False
        or evaluation.get("deployment_policy_value") is not False
        or evaluation.get("strength_evidence") is not False
        or report.get("schema") != GATE_REPORT_SCHEMA
        or report.get("run_id") != run_id
        or report.get("role_manifest_sha256") != role_manifest_sha
        or report.get("gate_passed") is not True
        or report.get("gate_errors") != []
        or report.get("native_candidate_build_authorized") is not True
        or report.get("gate_result_sha256") != result_sha
        or report.get("selected_policy_sha256") != selected_sha
        or report.get("candidate_sha256") != result.get("candidate_sha256")
        or report.get("selection_result_sha256")
        != result.get("selection_result_sha256")
        or report.get("source_collection_complete") is not True
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
        or artifact.get("candidate_sha256") != result.get("candidate_sha256")
        or artifact.get("run_id") != run_id
    ):
        raise ValueError("policy gate does not authorize a native candidate build")

    source = bundle.get("source")
    if (
        bundle.get("schema") != BUNDLE_SCHEMA
        or bundle.get("format") != ENSEMBLE_FORMAT
        or not isinstance(source, dict)
        or source.get("run_id") != run_id
        or source.get("role_manifest_sha256") != role_manifest_sha
        or bundle.get("selected_policy") != selected
        or source.get("selected_policy_sha256") != selected_sha
        or source.get("policy_selection_passed") is not True
        or source.get("source_collection_complete") is not True
        or source.get("policy_candidate_sha256") != result.get("candidate_sha256")
        or source.get("policy_result_sha256")
        != result.get("selection_result_sha256")
        or source.get("deployment_policy_value") is not False
        or source.get("strength_evidence") is not False
        or bundle.get("deployment_policy_value") is not False
        or bundle.get("strength_evidence") is not False
    ):
        raise ValueError("v3 bundle is not bound to the passing policy gate")
    runtime = OpponentMultiTaskEnsembleRuntimeV3.load(bundle_path)
    if runtime is None or runtime.policy is None or runtime.policy != selected:
        raise ValueError("v3 bundle failed strict selected-policy loading")
    return {
        "gate_dir": str(gate_root),
        "gate_artifact_manifest_sha256": _sha256(
            gate_root / "artifact_manifest.json"
        ),
        "gate_evaluation_sha256": _sha256(evaluation_path),
        "gate_result_sha256": result_sha,
        "gate_report_sha256": _sha256(report_path),
        "candidate_sha256": _digest(
            result.get("candidate_sha256"), field="candidate_sha256"
        ),
        "selection_result_sha256": _digest(
            result.get("selection_result_sha256"),
            field="selection_result_sha256",
        ),
        "selected_policy_sha256": _digest(
            selected_sha, field="selected_policy_sha256"
        ),
        "bundle_path": str(bundle_path),
        "bundle_sha256": _sha256(bundle_path),
        "run_id": run_id,
        "role_manifest_sha256": _digest(
            role_manifest_sha, field="role_manifest_sha256"
        ),
        "policy_gate_opponents": report.get("policy_gate_opponents"),
        "offline_only": True,
        "native_strength_evidence": False,
    }


def _patched_response_schema(source: Path) -> str:
    text = source.read_text(encoding="utf-8")
    start = text.index("def _load_validator():")
    end = text.index("\n\n\ndef _mapping", start)
    return text[:start] + "import national_validator as _VALIDATOR" + text[end:]


def _decision_methods() -> str:
    return '''    def _trace_decision(
        self,
        *,
        req: dict,
        state: dict,
        decision_index: int,
        rule_action: int,
        advised_action: int,
        sanitized_action: int,
        final_action: int,
        forced: bool,
        strategy_context: dict | None,
        v3_decision: dict | None,
    ) -> None:
        if not self._trace_enabled:
            return
        row = {
            "type": "decision",
            "name": self.name,
            "hand": self._hand_num,
            "decision_serial": self._decision_serial,
            "hand_decision_index": decision_index,
            "stage": self._stage,
            "round": self._round_num(),
            "is_small_blind": self._is_sb,
            "rule_action": int(rule_action),
            "advised_action": int(advised_action),
            "sanitized_action": int(sanitized_action),
            "final_action": int(final_action),
            "forced": bool(forced),
            "force_action": self._force_action,
            "request": req,
            "state": state,
        }
        if strategy_context is not None:
            row["strategy_context"] = strategy_context
        if v3_decision is not None:
            row["v3_decision"] = v3_decision
        print(TRACE_PREFIX + json.dumps(row, separators=(",", ":")), file=sys.stderr, flush=True)

    def _strategy_action(self, decision_index: int) -> int:
        req = self._request()
        self._requests.append(req)
        try:
            rule_action = int(self.get_action(req, list(self._requests)))
        except Exception:
            traceback.print_exc(file=sys.stderr)
            rule_action = 0
        try:
            strategy_context = self.consume_strategy_context()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            strategy_context = None
        try:
            state = self.reconstruct_state(req)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return 0

        advised_action = rule_action
        if self.apply_neural_advice is not None:
            try:
                advised_action = int(
                    self.apply_neural_advice(req, state, rule_action)
                )
            except Exception:
                traceback.print_exc(file=sys.stderr)
                advised_action = rule_action
        try:
            safe_rule_action = int(
                self.sanitize_action(advised_action, state, req["my_chips"])
            )
        except Exception:
            traceback.print_exc(file=sys.stderr)
            try:
                safe_rule_action = int(
                    self.sanitize_action(rule_action, state, req["my_chips"])
                )
            except Exception:
                traceback.print_exc(file=sys.stderr)
                safe_rule_action = 0
        safe_rule_action = self.sanitize_stage_total(
            safe_rule_action, state, req["my_chips"], fallback=0
        )
        final_action = safe_rule_action
        if self.v3_policy is not None:
            try:
                final_action = int(self.v3_policy.advise(
                    req, state, safe_rule_action, strategy_context
                ))
            except Exception:
                traceback.print_exc(file=sys.stderr)
                final_action = safe_rule_action
        final_action = self.sanitize_stage_total(
            final_action,
            state,
            req["my_chips"],
            fallback=safe_rule_action,
        )
        forced = False
        if self._should_force(decision_index):
            final_action = self.sanitize_stage_total(
                self._force_action,
                state,
                req["my_chips"],
                fallback=safe_rule_action,
            )
            forced = True
        v3_decision = (
            dict(self.v3_policy.last_decision)
            if self.v3_policy is not None
            and isinstance(self.v3_policy.last_decision, dict)
            else None
        )
        try:
            self._trace_decision(
                req=req,
                state=state,
                decision_index=decision_index,
                rule_action=rule_action,
                advised_action=advised_action,
                sanitized_action=safe_rule_action,
                final_action=final_action,
                forced=forced,
                strategy_context=strategy_context,
                v3_decision=v3_decision,
            )
        except Exception:
            traceback.print_exc(file=sys.stderr)
        self._decision_serial += 1
        return int(final_action)

'''


def _patch_national_bot(source: Path) -> str:
    text = source.read_text(encoding="utf-8")
    text = text.replace(
        "        from strategy import get_action\n",
        "        from strategy import get_action\n"
        "        from strategy_trace import consume_strategy_context\n"
        "        from v3_native_policy import (\n"
        "            load_native_v3_policy, sanitize_stage_total\n"
        "        )\n",
        1,
    )
    assignment = (
        "        self.get_action = get_action\n"
        "        self.apply_neural_advice = apply_neural_advice\n"
        "        self.reconstruct_state = reconstruct_state\n"
    )
    replacement = (
        "        self.get_action = get_action\n"
        "        self.consume_strategy_context = consume_strategy_context\n"
        "        self.apply_neural_advice = apply_neural_advice\n"
        "        self.reconstruct_state = reconstruct_state\n"
        "        self.sanitize_stage_total = sanitize_stage_total\n"
        "        self.v3_policy = load_native_v3_policy(BOT_DIR)\n"
    )
    if assignment not in text:
        raise ValueError("transport donor initialization contract changed")
    text = text.replace(assignment, replacement, 1)
    start = text.index("    def _trace_decision(\n")
    end = text.index("    def _current_round_has_allin", start)
    text = text[:start] + _decision_methods() + text[end:]
    return text


def _copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".completed"),
    )


def _copy_runtime_modules(target: Path) -> None:
    for name in COPIED_TOOL_MODULES:
        shutil.copy2(TOOLS / name, target / name)
    shutil.copy2(ROOT / "sever" / "engine" / "validator.py", target / "national_validator.py")
    (target / "opponent_response_schema.py").write_text(
        _patched_response_schema(TOOLS / "opponent_response_schema.py"),
        encoding="utf-8",
    )


def _verify_runtime_imports(target: Path) -> None:
    for path in target.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".", 1)[0]]
            forbidden = FORBIDDEN_RUNTIME_IMPORTS.intersection(names)
            if forbidden:
                raise ValueError(
                    f"candidate runtime imports forbidden dependencies in {path.name}: "
                    f"{sorted(forbidden)}"
                )


def _compile_candidate(target: Path) -> None:
    for path in sorted(target.glob("*.py")):
        py_compile.compile(str(path), doraise=True)
    shutil.rmtree(target / "__pycache__", ignore_errors=True)


def build_candidate(
    *,
    strategy_donor: Path,
    transport_donor: Path,
    bundle_path: Path,
    gate_dir: Path,
    output: Path,
) -> dict[str, Any]:
    strategy_donor = strategy_donor.resolve()
    transport_donor = transport_donor.resolve()
    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    for donor, name in (
        (strategy_donor, "strategy donor"),
        (transport_donor, "transport donor"),
    ):
        if not donor.is_dir() or not (donor / "national_bot.py").is_file():
            raise ValueError(f"invalid {name}: {donor}")
    authorization = verify_build_authorization(gate_dir, bundle_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    shutil.rmtree(temporary)
    try:
        _copy_tree(strategy_donor, temporary)
        for stale in ("TRACE_VERSION.md", "trace_manifest.json", "VERSION_NOTES.md"):
            (temporary / stale).unlink(missing_ok=True)
        (temporary / "national_bot.py").write_text(
            _patch_national_bot(transport_donor / "national_bot.py"),
            encoding="utf-8",
        )
        _copy_runtime_modules(temporary)
        shutil.copy2(bundle_path.resolve(), temporary / "v3_ensemble_bundle.json")
        evidence = temporary / "evidence" / "offline_policy_gate"
        evidence.mkdir(parents=True)
        for name in (
            "artifact_manifest.json",
            "policy_gate_evaluation.json",
            "policy_gate_result.json",
            "policy_gate_report.json",
        ):
            shutil.copy2(gate_dir.resolve() / name, evidence / name)
        _verify_runtime_imports(temporary)
        _compile_candidate(temporary)
        runtime = OpponentMultiTaskEnsembleRuntimeV3.load(
            temporary / "v3_ensemble_bundle.json"
        )
        if runtime is None or runtime.policy is None:
            raise ValueError("copied candidate bundle failed strict loading")
        manifest = {
            "schema": BUILD_SCHEMA,
            "candidate": output.name,
            "strategy_donor": {
                "path": str(strategy_donor),
                "sha256": _directory_sha256(strategy_donor),
            },
            "transport_donor": {
                "path": str(transport_donor),
                "sha256": _directory_sha256(transport_donor),
            },
            "authorization": authorization,
            "runtime_contract": {
                "entry": "national_bot.py",
                "native_tcp": True,
                "adapter": False,
                "official_action_delay_default_sec": 0.30,
                "stream_numeric_coalescing": True,
                "sanitized_rule_fallback": True,
                "stdlib_only": True,
                "ablation_env": [
                    "POK_V3_DISABLE",
                    "POK_V3_DISABLE_CROSS_HAND",
                    "POK_V3_DISABLE_RISK_MATCH",
                ],
            },
            "native_strength_evidence": False,
            "official_exe_accepted": False,
            "deployment_eligible": False,
        }
        (temporary / "V3_BUILD_MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (temporary / "VERSION_NOTES.md").write_text(
            "# Native opponent-aware v3 candidate\n\n"
            "This directory was generated only after the protected offline policy "
            "gate passed. It is not strength evidence until independent native TCP, "
            "ablation, and official EXE gates pass. See `V3_BUILD_MANIFEST.json`.\n",
            encoding="utf-8",
        )
        temporary.replace(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def _formal_output(path: Path) -> Path:
    output = path.resolve()
    if output.parent != VERSIONS.resolve() or not VERSION_RE.fullmatch(output.name):
        raise ValueError(
            f"formal output must be a new vNNN_<description> directory under {VERSIONS}"
        )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-donor", type=Path, default=DEFAULT_STRATEGY_DONOR)
    parser.add_argument("--transport-donor", type=Path, default=DEFAULT_TRANSPORT_DONOR)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--policy-gate-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_candidate(
            strategy_donor=args.strategy_donor,
            transport_donor=args.transport_donor,
            bundle_path=args.bundle,
            gate_dir=args.policy_gate_dir,
            output=_formal_output(args.output),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
