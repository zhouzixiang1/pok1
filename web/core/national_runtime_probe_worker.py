"""Trusted worker executed inside the national runtime Bubblewrap sandbox."""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import random
import resource
import signal
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from national_runtime_probe_scenarios import (
    DECISION_SCENARIOS,
    RUNTIME_PROBE_SCENARIO_DIGEST,
    RUNTIME_PROBE_SCENARIO_VERSION,
)


PROBE_WORKER_VERSION = 3
MAX_CAPTURE_CHARS = 64 * 1024
LEGAL_WIRE_ACTIONS = {"fold", "call", "check", "allin", "raise"}


class CappedTextIO(io.TextIOBase):
    def __init__(self, limit: int = MAX_CAPTURE_CHARS) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.length = 0
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        text = str(value)
        remaining = max(0, self.limit - self.length)
        if remaining:
            fragment = text[:remaining]
            self.parts.append(fragment)
            self.length += len(fragment)
        if len(text) > remaining:
            self.truncated = True
        return len(text)

    def getvalue(self) -> str:
        return "".join(self.parts)


class MemorySocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload.decode("utf-8", errors="replace"))


class CountingDict(dict):
    def __init__(self, source: dict[Any, Any]) -> None:
        super().__init__(source)
        self._shared_reads = mp.Value("q", 0, lock=True)

    @property
    def reads(self) -> int:
        return int(self._shared_reads.value)

    def _record_read(self) -> None:
        with self._shared_reads.get_lock():
            self._shared_reads.value += 1

    def __getitem__(self, key):
        self._record_read()
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._record_read()
        return super().get(key, default)

    def __contains__(self, key):
        self._record_read()
        return super().__contains__(key)


def _set_limits() -> None:
    limits = (
        (resource.RLIMIT_CPU, (10, 10)),
        (resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024)),
        (resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024)),
        (resource.RLIMIT_NOFILE, (64, 64)),
        (resource.RLIMIT_CORE, (0, 0)),
    )
    for resource_id, value in limits:
        resource.setrlimit(resource_id, value)
    if hasattr(resource, "RLIMIT_NPROC"):
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
        except (ValueError, OSError):
            pass


def _deep_size(value: Any, seen: set[int] | None = None, state: dict | None = None) -> int:
    seen = seen if seen is not None else set()
    state = state if state is not None else {"nodes": 0}
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    state["nodes"] += 1
    if state["nodes"] > 200_000:
        raise ValueError("object_graph_too_large")
    scalar = value is None or isinstance(value, (bool, int, float, str, bytes))
    if scalar:
        return sys.getsizeof(value)
    if not isinstance(value, (dict, list, tuple, set, frozenset)):
        raise TypeError(f"opaque_value_type:{type(value).__name__}")
    total = sys.getsizeof(value)
    if isinstance(value, dict):
        for key, item in value.items():
            total += _deep_size(key, seen, state)
            total += _deep_size(item, seen, state)
    else:
        for item in value:
            total += _deep_size(item, seen, state)
    return total


def _value_shape(value: Any, depth: int = 0) -> str:
    if depth > 4:
        return "..."
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    if isinstance(value, tuple):
        return "tuple[" + ",".join(_value_shape(item, depth + 1) for item in value) + "]"
    return type(value).__name__


class CandidateImports:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules: dict[str, Any] = {}
        self.diagnostics: dict[str, dict[str, Any]] = {}

    def load(self, module_name: str):
        if module_name in self.modules:
            return self.modules[module_name]
        stdout = CappedTextIO()
        stderr = CappedTextIO()
        started = time.monotonic()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            module = importlib.import_module(module_name)
        self.diagnostics[module_name] = {
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "stdout": stdout.getvalue(),
            "stdout_truncated": stdout.truncated,
            "stderr": stderr.getvalue(),
            "stderr_truncated": stderr.truncated,
        }
        self.modules[module_name] = module
        return module

    def candidate_modules(self) -> list[Any]:
        rows = []
        for module in list(sys.modules.values()):
            filename = getattr(module, "__file__", None)
            if not filename:
                continue
            try:
                path = Path(filename).resolve()
            except OSError:
                continue
            if path.parent == self.root:
                rows.append(module)
        return rows


def _profile(snapshot: dict[str, Any]) -> dict[str, Any]:
    contexts = {}
    for key, value in sorted((snapshot.get("contexts") or {}).items()):
        if not isinstance(value, dict):
            continue
        contexts[str(key)] = {
            "samples": int(value.get("samples", 0) or 0),
            "confidence": round(float(value.get("confidence", 0.0) or 0.0), 9),
            "adaptation_weight": round(
                float(value.get("adaptation_weight", 0.0) or 0.0), 9
            ),
        }
    return {
        "schema_version": int(snapshot.get("schema_version", 0) or 0),
        "confidence": round(float(snapshot.get("confidence", 0.0) or 0.0), 9),
        "adaptation_weight": round(float(snapshot.get("adaptation_weight", 0.0) or 0.0), 9),
        "hands_completed": int(snapshot.get("hands_completed", 0) or 0),
        "total_actions": int(snapshot.get("total_actions", 0) or 0),
        "recent_hands": len(snapshot.get("recent_hands") or []),
        "vpip": round(float(snapshot.get("vpip", 0.0) or 0.0), 9),
        "pfr": round(float(snapshot.get("pfr", 0.0) or 0.0), 9),
        "postflop_aggr": round(float(snapshot.get("postflop_aggr", 0.0) or 0.0), 9),
        "contexts": contexts,
    }


def _new_native_bot(imports: CandidateImports):
    native = imports.load("national_bot")
    bot = native.NativeNationalBot("ProbeB", "lower")
    bot._official_action_delay_sec = 0.0
    return native, bot


def _drive_connection(imports: CandidateImports, style: str, hands: int = 70):
    native, bot = _new_native_bot(imports)
    sock = MemorySocket()
    tracker_identity = id(bot._opponent_tracker)
    confidence_series = []
    weight_series = []
    request_mismatches = 0
    original_send = bot._send_decision
    bot._send_decision = lambda _sock: None
    try:
        for hand in range(1, hands + 1):
            bot.handle("preflop|BIGBLIND|<0,0><1,1>", sock)
            if id(bot._opponent_tracker) != tracker_identity:
                raise AssertionError(f"tracker_recreated_at_hand:{hand}")
            action = "raise 300" if style == "aggressive" else "fold"
            bot.handle(action, sock)
            if hand == hands:
                bot.handle("flop|<0,2><1,4><2,6>", sock)
                bot.handle("raise 700" if style == "aggressive" else "check", sock)
                bot.handle("turn|<3,8>", sock)
                bot.handle("call", sock)
                bot.handle("river|<0,10>", sock)
                bot.handle("raise 3000" if style == "aggressive" else "fold", sock)
            if hand % 5 == 0:
                bot.handle("oppo_hands|<2,2><3,3>", sock)
            bot.handle("earnChips 50", sock)
            snapshot = bot._opponent_tracker.snapshot()
            request_snapshot = bot._request().get("opponent_runtime") or {}
            if _profile(snapshot) != _profile(request_snapshot):
                request_mismatches += 1
            numbers = _profile(snapshot)
            confidence_series.append(numbers["confidence"])
            weight_series.append(numbers["adaptation_weight"])
    finally:
        bot._send_decision = original_send
    return native, bot, {
        "style": style,
        "hands": hands,
        "tracker_identity_stable": id(bot._opponent_tracker) == tracker_identity,
        "request_mismatches": request_mismatches,
        "confidence_series": confidence_series,
        "weight_series": weight_series,
        "final": _profile(bot._opponent_tracker.snapshot()),
    }


def _probe_tracker(imports: CandidateImports):
    result = {"ok": False, "issues": []}
    try:
        native, aggressive_bot, aggressive = _drive_connection(imports, "aggressive")
        _native, passive_bot, passive = _drive_connection(imports, "passive")
        _native, new_connection = _new_native_bot(imports)
        initial = _profile(new_connection._opponent_tracker.snapshot())
        cap = float(getattr(native, "OPPONENT_ADAPTATION_CAP", 1.0))
        for label, row in (("aggressive", aggressive), ("passive", passive)):
            confidences = row["confidence_series"]
            weights = row["weight_series"]
            if not row["tracker_identity_stable"]:
                result["issues"].append(f"{label}_tracker_identity_changed")
            if row["request_mismatches"]:
                result["issues"].append(f"{label}_snapshot_not_injected")
            if row["final"]["hands_completed"] != 70:
                result["issues"].append(f"{label}_hands_completed_not_70")
            if row["final"]["recent_hands"] > 70:
                result["issues"].append(f"{label}_recent_hands_unbounded")
            if not confidences or confidences[-1] <= 0.0:
                result["issues"].append(f"{label}_confidence_no_progress")
            if any(current + 1e-12 < previous for previous, current in zip(confidences, confidences[1:])):
                result["issues"].append(f"{label}_confidence_not_monotonic")
            if any(current + 1e-12 < previous for previous, current in zip(weights, weights[1:])):
                result["issues"].append(f"{label}_adaptation_not_monotonic")
            if weights and max(weights) > cap + 1e-12:
                result["issues"].append(f"{label}_adaptation_above_cap")
            final = row["final"]
            contexts = final.get("contexts") or {}
            if final.get("schema_version", 0) < 3:
                result["issues"].append(f"{label}_context_schema_missing")
            if not contexts or len(contexts) > 128:
                result["issues"].append(f"{label}_contexts_missing_or_unbounded")
            if final.get("vpip", 0.0) > 1.0 or final.get("pfr", 0.0) > 1.0:
                result["issues"].append(f"{label}_preflop_rate_above_one")
            preflop_confidence = max(
                (value["confidence"] for key, value in contexts.items() if key.startswith("preflop|")),
                default=0.0,
            )
            river_confidence = max(
                (value["confidence"] for key, value in contexts.items() if key.startswith("river|")),
                default=0.0,
            )
            if not (0.0 < river_confidence < preflop_confidence):
                result["issues"].append(f"{label}_context_confidence_not_isolated")
        if initial["confidence"] != 0.0 or initial["adaptation_weight"] != 0.0:
            result["issues"].append("new_connection_not_reset")
        result.update({
            "initial": initial,
            "cap": cap,
            "aggressive": aggressive,
            "passive": passive,
            "aggressive_bot": aggressive_bot,
            "passive_bot": passive_bot,
        })
    except BaseException as exc:
        result["issues"].append(f"tracker_probe_error:{type(exc).__name__}:{str(exc)[:180]}")
    result["ok"] = not result["issues"]
    return result


def _configure_decision_state(bot, scenario: dict[str, Any]) -> None:
    bot._is_sb = bool(scenario["is_sb"])
    bot._my_id = 0
    bot._opponent_id = 1
    bot._dealer_id = 0 if bot._is_sb else 1
    bot._hand_num = 21
    bot._my_cards = list(scenario["my_cards"])
    bot._public_cards = list(scenario["public_cards"])
    bot._history = copy.deepcopy(scenario["history"])
    bot._stage = str(scenario.get("stage") or ("preflop" if not bot._public_cards else "flop"))
    bot._my_action_count = 0
    bot._my_chips = 19_900
    bot._opponent_chips = 19_900
    bot._pot = int(scenario["pot"])
    bot._my_stage_bet = int(scenario["my_stage_bet"])
    bot._opponent_stage_bet = int(scenario["opponent_stage_bet"])
    bot._in_allin_runout = False


def _probe_hanging_action(_req, _current_view):
    while True:
        time.sleep(1.0)


def _timed_formal_action(bot, scenario: dict[str, Any]) -> dict[str, Any]:
    def alarm(_signum, _frame):
        raise TimeoutError("formal_decision_probe_timeout")

    old_handler = signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, 0.9)
    try:
        _configure_decision_state(bot, scenario)
        random.seed(20260710)
        action = int(bot._strategy_action())
        wire, _action_type, _amount = bot._action_to_tcp(action)
        head = wire.split(" ", 1)[0]
        if head not in LEGAL_WIRE_ACTIONS:
            raise ValueError(f"illegal_wire_action:{wire!r}")
        return {
            "internal": action,
            "wire": wire,
            "source": bot._last_decision_source,
            "runtime_metrics": dict(getattr(bot, "_last_decision_metrics", {}) or {}),
        }
    finally:
        try:
            bot.close()
        except BaseException:
            pass
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)


def _probe_strategy_influence(imports: CandidateImports, tracker_result: dict[str, Any]):
    result = {"ok": False, "issues": [], "rows": [], "changed_pairs": 0}
    try:
        _native, baseline_bot = _new_native_bot(imports)
        aggressive_bot = tracker_result["aggressive_bot"]
        passive_bot = tracker_result["passive_bot"]
        for scenario in DECISION_SCENARIOS:
            row = {"scenario_id": scenario["id"]}
            for label, bot in (
                ("baseline", baseline_bot),
                ("aggressive", aggressive_bot),
                ("passive", passive_bot),
            ):
                try:
                    row[label] = _timed_formal_action(bot, scenario)
                except BaseException as exc:
                    row[label] = {"error": f"{type(exc).__name__}:{str(exc)[:160]}"}
            baseline_wire = row["baseline"].get("wire")
            changed = sum(
                row[label].get("wire") != baseline_wire
                for label in ("aggressive", "passive")
                if "error" not in row[label] and baseline_wire is not None
            )
            result["changed_pairs"] += changed
            result["rows"].append(row)
        successful = [
            row for row in result["rows"]
            if all("error" not in row[label] for label in ("baseline", "aggressive", "passive"))
        ]
        if len(successful) < 2:
            result["issues"].append("strategy_probe_insufficient_formal_actions")
        if result["changed_pairs"] < 1:
            result["issues"].append("opponent_runtime_no_observable_sanitized_action_influence")
    except BaseException as exc:
        result["issues"].append(f"strategy_probe_error:{type(exc).__name__}:{str(exc)[:180]}")
    result["ok"] = not result["issues"]
    return result


def _probe_decision_runtime(
    imports: CandidateImports,
    strategy_result: dict[str, Any],
    *,
    expected_runtime_version: int,
):
    result = {
        "ok": False,
        "issues": [],
        "baseline_samples_ms": [],
        "fallback_ready_samples_ms": [],
        "timeout_recovery": {},
    }
    try:
        metrics_rows = []
        for row in strategy_result.get("rows") or []:
            for label in ("baseline", "aggressive", "passive"):
                action = row.get(label) or {}
                metrics = action.get("runtime_metrics") or {}
                if metrics:
                    metrics_rows.append(metrics)
        if not metrics_rows:
            result["issues"].append("decision_runtime_metrics_missing")
        for metrics in metrics_rows:
            actual_runtime_version = int(metrics.get("runtime_version") or 0)
            if actual_runtime_version != expected_runtime_version:
                result["issues"].append(
                    "decision_runtime_version_mismatch:"
                    f"expected={expected_runtime_version}:actual={actual_runtime_version}"
                )
            fallback_ms = metrics.get("socket_fallback_ready_ms")
            if fallback_ms is not None:
                result["fallback_ready_samples_ms"].append(float(fallback_ms))
            baseline_ms = metrics.get("baseline_published_ms")
            if baseline_ms is not None:
                result["baseline_samples_ms"].append(float(baseline_ms))
        if not result["fallback_ready_samples_ms"]:
            result["issues"].append("socket_fallback_timing_missing")
        elif max(result["fallback_ready_samples_ms"]) > 25.0:
            result["issues"].append("socket_fallback_slower_than_25ms")
        if not result["baseline_samples_ms"]:
            result["issues"].append("strategy_baseline_never_published")
        elif max(result["baseline_samples_ms"]) > 250.0:
            result["issues"].append("strategy_baseline_slower_than_250ms")

        strategy = imports.load("strategy")
        missing = object()
        originals = {
            name: getattr(strategy, name, missing)
            for name in (
                "get_action",
                "get_baseline_action",
                "iter_refinements",
                "refine_action",
            )
        }
        strategy.get_action = _probe_hanging_action
        strategy.get_baseline_action = None
        strategy.iter_refinements = None
        strategy.refine_action = None
        timeout_bot = None
        try:
            _native, timeout_bot = _new_native_bot(imports)
            timeout_action = _timed_formal_action(timeout_bot, DECISION_SCENARIOS[1])
        finally:
            for name, value in originals.items():
                if value is missing:
                    try:
                        delattr(strategy, name)
                    except AttributeError:
                        pass
                else:
                    setattr(strategy, name, value)
        timeout_metrics = timeout_action.get("runtime_metrics") or {}
        if timeout_action.get("wire") != "fold":
            result["issues"].append("positive_to_call_timeout_fallback_not_fold")
        if timeout_metrics.get("timed_out") is not True:
            result["issues"].append("hanging_strategy_not_timed_out")
        if timeout_metrics.get("worker_terminated") is not True:
            result["issues"].append("hanging_strategy_worker_not_terminated")

        recovery_action = _timed_formal_action(timeout_bot, DECISION_SCENARIOS[0])
        recovery_metrics = recovery_action.get("runtime_metrics") or {}
        if int(recovery_metrics.get("decision_id") or 0) <= int(
            timeout_metrics.get("decision_id") or 0
        ):
            result["issues"].append("decision_id_did_not_advance_after_timeout")
        if int(recovery_metrics.get("worker_generation") or 0) <= int(
            timeout_metrics.get("worker_generation") or 0
        ):
            result["issues"].append("worker_not_restarted_after_timeout")
        if recovery_metrics.get("timed_out") is True:
            result["issues"].append("recovery_decision_timed_out")
        result["timeout_recovery"] = {
            "timeout": timeout_action,
            "recovery": recovery_action,
        }
    except BaseException as exc:
        result["issues"].append(
            f"decision_runtime_probe_error:{type(exc).__name__}:{str(exc)[:180]}"
        )
    result["baseline_samples_ms"] = [
        round(value, 3) for value in result["baseline_samples_ms"][:64]
    ]
    result["fallback_ready_samples_ms"] = [
        round(value, 3) for value in result["fallback_ready_samples_ms"][:64]
    ]
    result["ok"] = not result["issues"]
    return result


def _replace_identity(imports: CandidateImports, original: Any, replacement: Any):
    changed = []
    for module in imports.candidate_modules():
        for name, value in list(vars(module).items()):
            if value is original:
                setattr(module, name, replacement)
                changed.append((module, name))
    return changed


def _restore_identity(changed, original: Any) -> None:
    for module, name in changed:
        setattr(module, name, original)


def _probe_artifacts(imports: CandidateImports, artifacts: list[dict[str, Any]]):
    rows = []
    for spec in artifacts:
        row = {**spec, "ok": False, "issues": []}
        try:
            owner_module = Path(spec["owner_file"]).stem
            module = imports.load(owner_module)
            value = getattr(module, spec["name"])
            if not isinstance(value, dict):
                row["issues"].append("artifact_not_inspectable_mapping")
            else:
                row["entries"] = len(value)
                row["deep_bytes"] = _deep_size(value)
                shapes = sorted({_value_shape(key) for key in list(value)[:64]})
                row["observed_key_shape"] = (
                    shapes[0] if len(shapes) == 1 else "mixed[" + ",".join(shapes) + "]"
                )
                diag = imports.diagnostics.get(owner_module) or {}
                row["import_elapsed_ms"] = diag.get("elapsed_ms")
                row["observed_build_phase"] = "module_import"
                if not value:
                    row["issues"].append("artifact_empty")
                if diag.get("stdout") or diag.get("stdout_truncated"):
                    row["issues"].append("module_import_stdout_pollution")

                counting = CountingDict(value)
                changed = _replace_identity(imports, value, counting)
                try:
                    consumer_scenarios = []
                    for scenario in DECISION_SCENARIOS:
                        before_reads = counting.reads
                        try:
                            _native, probe_bot = _new_native_bot(imports)
                            action = _timed_formal_action(probe_bot, scenario)
                            consumer_scenarios.append({
                                "scenario_id": scenario["id"],
                                "reads": counting.reads - before_reads,
                                "action": action,
                            })
                        except BaseException as exc:
                            consumer_scenarios.append({
                                "scenario_id": scenario["id"],
                                "reads": counting.reads - before_reads,
                                "error": f"{type(exc).__name__}:{str(exc)[:140]}",
                            })
                    row["consumer_reads"] = counting.reads
                    row["consumer_scenarios"] = consumer_scenarios
                finally:
                    _restore_identity(changed, value)
                if row.get("consumer_reads", 0) < 1:
                    row["issues"].append("artifact_not_read_by_formal_decision")
                if not any(
                    "error" not in item and item.get("reads", 0) > 0
                    for item in row.get("consumer_scenarios") or []
                ):
                    row["issues"].append("artifact_no_successful_formal_consumer_scenario")

                empty = CountingDict({})
                changed = _replace_identity(imports, value, empty)
                try:
                    fallback_scenarios = []
                    for scenario in DECISION_SCENARIOS:
                        try:
                            _native, fallback_bot = _new_native_bot(imports)
                            fallback_scenarios.append({
                                "scenario_id": scenario["id"],
                                "action": _timed_formal_action(fallback_bot, scenario),
                            })
                        except BaseException as exc:
                            fallback_scenarios.append({
                                "scenario_id": scenario["id"],
                                "error": f"{type(exc).__name__}:{str(exc)[:140]}",
                            })
                    row["fallback_scenarios"] = fallback_scenarios
                    row["fallback_ok"] = all(
                        "error" not in item for item in fallback_scenarios
                    ) and len(fallback_scenarios) == len(DECISION_SCENARIOS)
                    for item in fallback_scenarios:
                        if "error" in item:
                            row["issues"].append(
                                "empty_mapping_fallback_error:"
                                f"{item['scenario_id']}:{item['error']}"
                            )
                finally:
                    _restore_identity(changed, value)
        except BaseException as exc:
            row["issues"].append(f"artifact_probe_error:{type(exc).__name__}:{str(exc)[:180]}")
        row["ok"] = not row["issues"]
        rows.append(row)
    return rows


def _strip_runtime_objects(tracker: dict[str, Any]) -> dict[str, Any]:
    result = dict(tracker)
    result.pop("aggressive_bot", None)
    result.pop("passive_bot", None)
    for style in ("aggressive", "passive"):
        row = result.get(style)
        if isinstance(row, dict):
            row = dict(row)
            confidences = row.pop("confidence_series", [])
            weights = row.pop("weight_series", [])
            row["confidence_points"] = [
                confidences[index] for index in (0, 9, 34, 69) if index < len(confidences)
            ]
            row["weight_points"] = [
                weights[index] for index in (0, 9, 34, 69) if index < len(weights)
            ]
            result[style] = row
    return result


def run(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    sys.dont_write_bytecode = True
    random.seed(20260710)
    os.environ["POK_OFFICIAL_ACTION_DELAY"] = "0"
    os.environ["POK_DECISION_HARD_DEADLINE_SEC"] = "0.70"
    os.environ["POK_DECISION_REFINEMENT_BUDGET_SEC"] = "0.55"
    os.environ["POK_DECISION_BASELINE_TARGET_SEC"] = "0.08"
    os.environ["POK_DECISION_BUDGET_SEC"] = "0.70"
    sys.path.insert(0, str(root))
    imports = CandidateImports(root)
    tracker = _probe_tracker(imports)
    strategy = _probe_strategy_influence(imports, tracker) if tracker.get("ok") else {
        "ok": False,
        "issues": ["strategy_probe_skipped_tracker_failed"],
        "rows": [],
        "changed_pairs": 0,
    }
    expected_runtime_version = int(spec.get("expected_decision_runtime_version") or 0)
    decision_runtime = (
        _probe_decision_runtime(
            imports,
            strategy,
            expected_runtime_version=expected_runtime_version,
        )
        if strategy.get("ok")
        else {
            "ok": False,
            "issues": ["decision_runtime_probe_skipped_strategy_failed"],
            "baseline_samples_ms": [],
            "fallback_ready_samples_ms": [],
            "timeout_recovery": {},
        }
    )
    artifacts = _probe_artifacts(imports, spec.get("artifacts") or [])
    issues = [
        *(tracker.get("issues") or []),
        *(strategy.get("issues") or []),
        *(decision_runtime.get("issues") or []),
        *(
            issue
            for row in artifacts
            for issue in row.get("issues") or []
        ),
    ]
    return {
        "schema_version": int(spec.get("schema_version") or 1),
        "worker_version": PROBE_WORKER_VERSION,
        "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
        "spec_digest": spec.get("spec_digest"),
        "code_fingerprint": spec.get("code_fingerprint"),
        "ok": not issues,
        "issues": issues,
        "artifacts": artifacts,
        "tracker": _strip_runtime_objects(tracker),
        "strategy_influence": strategy,
        "decision_runtime": decision_runtime,
        "module_diagnostics": imports.diagnostics,
    }


def main() -> int:
    _set_limits()
    root = Path(sys.argv[1]).resolve()
    report_path = Path(sys.argv[2])
    spec = json.loads(sys.argv[3])
    try:
        report = run(root, spec)
    except BaseException as exc:
        report = {
            "schema_version": int(spec.get("schema_version") or 1),
            "worker_version": PROBE_WORKER_VERSION,
            "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
            "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
            "spec_digest": spec.get("spec_digest"),
            "code_fingerprint": spec.get("code_fingerprint"),
            "ok": False,
            "failure_class": "probe_infra",
            "issues": [f"probe_crash:{type(exc).__name__}:{str(exc)[:240]}"],
        }
    report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
