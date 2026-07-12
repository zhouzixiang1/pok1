"""Trusted worker executed inside the national runtime Bubblewrap sandbox."""

from __future__ import annotations

import contextlib
import copy
import hashlib
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
    ACTION_PROFILE_SCENARIO_IDS,
    DECISION_SCENARIOS,
    LINE_SCENARIO_PAIRS,
    RUNTIME_PROBE_SCENARIO_DIGEST,
    RUNTIME_PROBE_SCENARIO_VERSION,
    SHOWDOWN_RANGE_SCENARIO_IDS,
    TERMINAL_RESPONSE_SCENARIO_IDS,
)


PROBE_WORKER_VERSION = 6
MAX_CAPTURE_CHARS = 64 * 1024
LEGAL_WIRE_ACTIONS = {"fold", "call", "check", "allin", "raise"}
PHASE_PATH = Path("/tmp/probe_out/phase.txt")
MAX_TRACKED_LOOKUP_KEYS = 64


def _phase(name: str) -> None:
    try:
        PHASE_PATH.write_text(str(name), encoding="utf-8")
    except OSError:
        pass


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
        # Strategy decisions run in a short-lived worker process.  Fixed-size
        # shared slots retain a deterministic fingerprint of lookup keys across
        # that boundary without a Manager/socket or unbounded key capture.
        self._shared_key_count = mp.Value("i", 0, lock=True)
        self._shared_key_slots = mp.Array("Q", MAX_TRACKED_LOOKUP_KEYS, lock=True)

    @property
    def reads(self) -> int:
        return int(self._shared_reads.value)

    @staticmethod
    def _key_token(key: Any) -> int:
        try:
            text = json.dumps(
                key,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=repr,
            )
        except Exception:
            text = f"{type(key).__name__}:{repr(key)[:512]}"
        return int.from_bytes(
            hashlib.blake2b(
                text.encode("utf-8", errors="replace"), digest_size=8
            ).digest(),
            "big",
        )

    def _record_read(self, key: Any) -> None:
        with self._shared_reads.get_lock():
            self._shared_reads.value += 1
        token = self._key_token(key)
        with self._shared_key_count.get_lock():
            index = int(self._shared_key_count.value)
            if index < MAX_TRACKED_LOOKUP_KEYS:
                with self._shared_key_slots.get_lock():
                    self._shared_key_slots[index] = token
            self._shared_key_count.value = index + 1

    def reset_key_observations(self) -> None:
        with self._shared_key_count.get_lock():
            self._shared_key_count.value = 0

    def key_observation(self) -> dict[str, Any]:
        with self._shared_key_count.get_lock():
            total = max(0, int(self._shared_key_count.value))
        observed = min(total, MAX_TRACKED_LOOKUP_KEYS)
        with self._shared_key_slots.get_lock():
            tokens = sorted({int(self._shared_key_slots[index]) for index in range(observed)})
        canonical = ",".join(f"{token:016x}" for token in tokens)
        return {
            "lookup_key_count": total,
            "lookup_key_tokens": [f"{token:016x}" for token in tokens],
            "lookup_key_fingerprint": hashlib.sha256(
                canonical.encode("ascii")
            ).hexdigest() if tokens else "",
            "lookup_key_capture_truncated": total > MAX_TRACKED_LOOKUP_KEYS,
        }

    def __getitem__(self, key):
        self._record_read(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._record_read(key)
        return super().get(key, default)

    def __contains__(self, key):
        self._record_read(key)
        return super().__contains__(key)


class FixedSnapshotTracker:
    """Decision-only tracker view for one-field-family counterfactuals."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = copy.deepcopy(snapshot)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._snapshot)


def _set_limits() -> None:
    limits = (
        (resource.RLIMIT_CPU, (35, 35)),
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
    showdown = snapshot.get("showdown_range") or {}
    bucket_rates = showdown.get("bucket_rates") or {}
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
        "fold_to_raise": round(float(snapshot.get("fold_to_raise", 0.0) or 0.0), 9),
        "fold_to_jam_rate": round(float(snapshot.get("fold_to_jam_rate", 0.0) or 0.0), 9),
        "fold_to_jam_samples": int(snapshot.get("fold_to_jam_samples", 0) or 0),
        "river_overcall_freq": round(
            float(snapshot.get("river_overcall_freq", 0.0) or 0.0), 9
        ),
        "river_overcall_samples": int(snapshot.get("river_overcall_samples", 0) or 0),
        "showdown_range": {
            "schema_version": int(showdown.get("schema_version", 0) or 0),
            "samples": int(showdown.get("samples", 0) or 0),
            "confidence": round(float(showdown.get("confidence", 0.0) or 0.0), 9),
            "adaptation_weight": round(
                float(showdown.get("adaptation_weight", 0.0) or 0.0), 9
            ),
            "showdown_reach_rate": round(
                float(showdown.get("showdown_reach_rate", 0.0) or 0.0), 9
            ),
            "selection_scope": str(showdown.get("selection_scope") or ""),
            "selection_bias_guard": str(showdown.get("selection_bias_guard") or ""),
            "prior_source": str(showdown.get("prior_source") or ""),
            "tightness": round(float(showdown.get("tightness", 0.0) or 0.0), 9),
            "premium_pair": round(float(bucket_rates.get("premium_pair", 0.0) or 0.0), 9),
            "offsuit_other": round(float(bucket_rates.get("offsuit_other", 0.0) or 0.0), 9),
        },
        "contexts": contexts,
    }


def _new_native_bot(imports: CandidateImports):
    native = imports.load("national_bot")
    bot = native.NativeNationalBot("ProbeB", "lower")
    bot._official_action_delay_sec = 0.0
    return native, bot


def _drive_connection(
    imports: CandidateImports,
    style: str,
    hands: int = 70,
    *,
    showdown_style: str = "mixed",
):
    # Synthetic local lifecycle evidence only.  The official 70-hand EXE may
    # omit hand 70's earnChips and requires the separately cross-bound THP
    # terminal proof; this probe must never be used as formal completion or
    # strength evidence.
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
            action = (
                "raise 300"
                if style == "aggressive"
                else "fold"
                if style == "passive"
                else "call"
            )
            bot.handle(action, sock)
            if hand == hands:
                bot.handle("flop|<0,2><1,4><2,6>", sock)
                bot.handle("raise 700" if style == "aggressive" else "check", sock)
                bot.handle("turn|<3,8>", sock)
                bot.handle("call", sock)
                bot.handle("river|<0,10>", sock)
                bot.handle("raise 3000" if style == "aggressive" else "fold", sock)
            if hand % 5 == 0:
                showdown_message = {
                    "tight": "oppo_hands|<0,12><1,12>",
                    "loose": "oppo_hands|<0,5><1,0>",
                    "mixed": "oppo_hands|<2,2><3,3>",
                }[showdown_style]
                bot.handle(showdown_message, sock)
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


def _drive_terminal_response_profile(imports: CandidateImports, response: str, hands: int = 70):
    _native, bot = _new_native_bot(imports)
    tracker = bot._opponent_tracker
    request_mismatches = 0
    for hand in range(1, hands + 1):
        tracker.begin_hand(hand, opponent_is_sb=bool(hand % 2))
        tracker.observe_action("opponent", "preflop", "call")
        if hand % 3 == 0:
            tracker.observe_action("hero", "river", "allin", amount=20_000)
            tracker.observe_action("opponent", "river", response)
        elif hand % 3 == 1:
            tracker.observe_action("hero", "river", "raise", amount=1_200)
            tracker.observe_action("opponent", "river", response)
        else:
            tracker.observe_action("hero", "flop", "raise", amount=600)
            tracker.observe_action("opponent", "flop", response)
        tracker.observe_settlement(hand, hero_earned=50 if response == "fold" else -50)
        if _profile(tracker.snapshot()) != _profile(bot._request()["opponent_runtime"]):
            request_mismatches += 1
    return bot, {
        "response": response,
        "hands": hands,
        "request_mismatches": request_mismatches,
        "final": _profile(tracker.snapshot()),
    }


def _probe_tracker(imports: CandidateImports):
    result = {"ok": False, "issues": []}
    try:
        native, aggressive_bot, aggressive = _drive_connection(imports, "aggressive")
        _native, passive_bot, passive = _drive_connection(imports, "passive")
        _native, tight_showdown_bot, tight_showdown = _drive_connection(
            imports,
            "neutral",
            showdown_style="tight",
        )
        _native, loose_showdown_bot, loose_showdown = _drive_connection(
            imports,
            "neutral",
            showdown_style="loose",
        )
        terminal_folder_bot, terminal_folder = _drive_terminal_response_profile(
            imports,
            "fold",
        )
        terminal_caller_bot, terminal_caller = _drive_terminal_response_profile(
            imports,
            "call",
        )
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
            if final.get("schema_version", 0) < 4:
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
        for label, row in (
            ("tight_showdown", tight_showdown),
            ("loose_showdown", loose_showdown),
            ("terminal_folder", terminal_folder),
            ("terminal_caller", terminal_caller),
        ):
            if row.get("request_mismatches"):
                result["issues"].append(f"{label}_snapshot_not_injected")
        tight_range = tight_showdown["final"]["showdown_range"]
        loose_range = loose_showdown["final"]["showdown_range"]
        if tight_range.get("schema_version", 0) < 1:
            result["issues"].append("showdown_range_schema_missing")
        if tight_range.get("samples") != 14 or loose_range.get("samples") != 14:
            result["issues"].append("showdown_range_sample_count_wrong")
        if tight_range.get("selection_scope") != "reached_showdown_only":
            result["issues"].append("showdown_range_selection_scope_missing")
        if tight_range.get("selection_bias_guard") != "reach_rate_discount_and_capped_influence":
            result["issues"].append("showdown_range_selection_bias_guard_missing")
        if tight_range.get("prior_source") != "uniform_1326_hole_combinations_v1":
            result["issues"].append("showdown_range_prior_source_unpinned")
        if not (0.0 < tight_range.get("adaptation_weight", 0.0) <= cap):
            result["issues"].append("showdown_range_influence_not_capped")
        if tight_range.get("showdown_reach_rate") != 0.2:
            result["issues"].append("showdown_range_reach_rate_wrong")
        if tight_range.get("tightness", 0.0) <= loose_range.get("tightness", 0.0):
            result["issues"].append("showdown_range_posterior_not_card_sensitive")
        if tight_range.get("premium_pair", 0.0) <= loose_range.get("premium_pair", 0.0):
            result["issues"].append("showdown_premium_pair_posterior_not_updated")
        folder = terminal_folder["final"]
        caller = terminal_caller["final"]
        if folder.get("fold_to_raise", 0.0) <= caller.get("fold_to_raise", 0.0):
            result["issues"].append("terminal_fold_to_raise_not_response_sensitive")
        if folder.get("fold_to_jam_rate", 0.0) <= caller.get("fold_to_jam_rate", 0.0):
            result["issues"].append("terminal_fold_to_jam_not_response_sensitive")
        if folder.get("river_overcall_freq", 1.0) >= caller.get("river_overcall_freq", 0.0):
            result["issues"].append("river_overcall_not_response_sensitive")
        if min(
            folder.get("fold_to_jam_samples", 0),
            caller.get("fold_to_jam_samples", 0),
            folder.get("river_overcall_samples", 0),
            caller.get("river_overcall_samples", 0),
        ) <= 0:
            result["issues"].append("terminal_response_samples_missing")
        result.update({
            "initial": initial,
            "cap": cap,
            "aggressive": aggressive,
            "passive": passive,
            "aggressive_bot": aggressive_bot,
            "passive_bot": passive_bot,
            "tight_showdown": tight_showdown,
            "loose_showdown": loose_showdown,
            "tight_showdown_bot": tight_showdown_bot,
            "loose_showdown_bot": loose_showdown_bot,
            "terminal_folder": terminal_folder,
            "terminal_caller": terminal_caller,
            "terminal_folder_bot": terminal_folder_bot,
            "terminal_caller_bot": terminal_caller_bot,
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
    hero_blind = 50 if bot._is_sb else 100
    opponent_blind = 100 if bot._is_sb else 50
    hero_committed = hero_blind + sum(
        int(record.get("committed", 0) or 0)
        for record in scenario["history"]
        if record.get("player_id") == bot._my_id
    )
    opponent_committed = opponent_blind + sum(
        int(record.get("committed", 0) or 0)
        for record in scenario["history"]
        if record.get("player_id") == bot._opponent_id
    )
    bot._my_chips = max(0, 20_000 - hero_committed)
    bot._opponent_chips = max(0, 20_000 - opponent_committed)
    bot._pot = int(scenario["pot"])
    bot._my_stage_bet = int(scenario["my_stage_bet"])
    bot._opponent_stage_bet = int(scenario["opponent_stage_bet"])
    bot._in_allin_runout = False


def _probe_hand_context(imports: CandidateImports):
    result = {"ok": False, "issues": [], "scenario_contexts": {}}
    try:
        for scenario in DECISION_SCENARIOS:
            expected = scenario.get("expected_hand_runtime")
            if not expected:
                continue
            _native, bot = _new_native_bot(imports)
            try:
                _configure_decision_state(bot, scenario)
                context = bot._request().get("hand_runtime") or {}
                result["scenario_contexts"][scenario["id"]] = context
                for key, value in expected.items():
                    if context.get(key) != value:
                        result["issues"].append(
                            f"{scenario['id']}_{key}_mismatch:"
                            f"expected={value!r}:actual={context.get(key)!r}"
                        )
            finally:
                bot.close()

        _native, line_bot = _new_native_bot(imports)
        line_socket = MemorySocket()
        line_bot._strategy_action = lambda: 0
        original_send = line_bot._send_decision
        line_bot.handle("preflop|BIGBLIND|<0,9><1,8>", line_socket)
        line_bot.handle("raise 300", line_socket)
        line_bot._send_decision = lambda _sock: None
        line_bot.handle("flop|<2,2><1,5><3,7>", line_socket)
        donk_context = line_bot._request().get("hand_runtime") or {}
        original_send(line_socket)
        line_bot._send_decision = lambda _sock: None
        line_bot.handle("turn|<0,10>", line_socket)
        delayed_context = line_bot._request().get("hand_runtime") or {}
        inferred_passes = [
            row for row in line_bot._history
            if row.get("round") == 1
            and row.get("player_id") == line_bot._opponent_id
            and row.get("action_type") == "call"
            and row.get("inferred")
        ]
        result["transcript"] = {
            "wire_actions": list(line_socket.sent),
            "donk": donk_context,
            "delayed_probe": delayed_context,
            "inferred_passes": len(inferred_passes),
        }
        if donk_context.get("can_donk") is not True:
            result["issues"].append("transcript_donk_flag_missing")
        if delayed_context.get("can_delayed_probe") is not True:
            result["issues"].append("transcript_delayed_probe_flag_missing")
        if not (
            (delayed_context.get("previous_street") or {}).get("checked_through")
            and (delayed_context.get("previous_street") or {}).get("opponent_checked_back")
            and len(inferred_passes) == 1
        ):
            result["issues"].append("official_pass_call_semantics_not_preserved")
        line_bot.close()

        _native, terminal_bot = _new_native_bot(imports)
        terminal_socket = MemorySocket()
        terminal_bot._strategy_action = lambda: 282
        terminal_bot.handle("preflop|SMALLBLIND|<0,12><1,11>", terminal_socket)
        terminal_bot._send_decision = lambda _sock: None
        terminal_bot.handle("flop|<2,0><0,9><3,7>", terminal_socket)
        terminal_snapshot = terminal_bot._opponent_tracker.snapshot()
        result["terminal_repair"] = {
            "pot": terminal_bot._pot,
            "opponent_chips": terminal_bot._opponent_chips,
            "fold_to_raise_samples": terminal_snapshot["samples"]["fold_to_raise"],
            "history": copy.deepcopy(terminal_bot._history),
        }
        if terminal_bot._pot != 564 or terminal_bot._opponent_chips != 19_718:
            result["issues"].append("suppressed_terminal_call_did_not_repair_betting_state")
        if terminal_snapshot["samples"]["fold_to_raise"] != 1:
            result["issues"].append("inferred_terminal_call_not_persisted")
        terminal_bot.close()

        _native, fold_bot = _new_native_bot(imports)
        fold_socket = MemorySocket()
        fold_bot._strategy_action = lambda: 282
        fold_bot.handle("preflop|SMALLBLIND|<0,12><1,11>", fold_socket)
        fold_bot.handle("fold", fold_socket)
        fold_bot.handle("earnChips 100", fold_socket)
        fold_bot._send_decision = lambda _sock: None
        fold_bot.handle("preflop|BIGBLIND|<0,7><1,6>", fold_socket)
        next_hand_profile = fold_bot._request()["opponent_runtime"]
        result["terminal_fold"] = _profile(next_hand_profile)
        if next_hand_profile["samples"]["fold_to_raise"] != 1:
            result["issues"].append("relayed_terminal_fold_not_preserved_across_hands")
        fold_bot.close()

        def drive_river_terminal(response: str | None, *, jam: bool = False):
            _native, bot = _new_native_bot(imports)
            sock = MemorySocket()
            actions = iter((0, 0, 0, -2 if jam else 1200))
            bot._strategy_action = lambda: next(actions)
            bot.handle("preflop|SMALLBLIND|<0,12><1,11>", sock)
            bot.handle("flop|<2,0><0,9><3,7>", sock)
            bot.handle("check", sock)
            bot.handle("turn|<1,6>", sock)
            bot.handle("check", sock)
            bot.handle("river|<2,4>", sock)
            bot.handle("check", sock)
            if response is not None:
                bot.handle(response, sock)
            bot.handle("earnChips 0", sock)
            snapshot = bot._request()["opponent_runtime"]
            history = copy.deepcopy(bot._history)
            bot.close()
            return snapshot, history, list(sock.sent)

        relayed_fold, fold_history, fold_wire = drive_river_terminal("fold")
        relayed_call, call_history, call_wire = drive_river_terminal("call")
        inferred_call, inferred_history, inferred_wire = drive_river_terminal(None)
        relayed_jam_call, jam_history, jam_wire = drive_river_terminal("call", jam=True)
        result["real_terminal_transcripts"] = {
            "relayed_fold": {
                "profile": _profile(relayed_fold),
                "history": fold_history,
                "wire": fold_wire,
            },
            "relayed_call": {
                "profile": _profile(relayed_call),
                "history": call_history,
                "wire": call_wire,
            },
            "inferred_call": {
                "profile": _profile(inferred_call),
                "history": inferred_history,
                "wire": inferred_wire,
            },
            "relayed_jam_call": {
                "profile": _profile(relayed_jam_call),
                "history": jam_history,
                "wire": jam_wire,
            },
        }
        if relayed_fold["samples"]["fold_to_raise"] != 1:
            result["issues"].append("real_river_relayed_fold_not_wired")
        if relayed_call["river_overcall_samples"] != 1:
            result["issues"].append("real_river_relayed_call_not_wired")
        inferred_rows = [
            item
            for item in inferred_history
            if item.get("inference_boundary") == "settlement"
            and item.get("action_type") == "call"
        ]
        if inferred_call["river_overcall_samples"] != 1 or len(inferred_rows) != 1:
            result["issues"].append("real_river_omitted_call_not_repaired")
        if relayed_jam_call["fold_to_jam_samples"] != 1:
            result["issues"].append("real_river_relayed_jam_call_not_wired")
    except BaseException as exc:
        result["issues"].append(
            f"hand_context_probe_error:{type(exc).__name__}:{str(exc)[:180]}"
        )
    result["ok"] = not result["issues"]
    return result


def _probe_hanging_action(_req, _current_view):
    while True:
        time.sleep(1.0)


def _timed_formal_action(
    bot,
    scenario: dict[str, Any],
    *,
    hard_deadline_sec: float | None = None,
    baseline_target_sec: float | None = None,
    refinement_budget_sec: float | None = None,
    max_refinement_candidates: int | None = None,
    request_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def alarm(_signum, _frame):
        raise TimeoutError("formal_decision_probe_timeout")

    old_handler = signal.signal(signal.SIGALRM, alarm)
    watchdog_sec = 0.9
    if hard_deadline_sec is not None:
        watchdog_sec = max(watchdog_sec, float(hard_deadline_sec) + 1.0)
    signal.setitimer(signal.ITIMER_REAL, watchdog_sec)
    try:
        if hard_deadline_sec is not None:
            bot._decision_hard_deadline_sec = float(hard_deadline_sec)
        if baseline_target_sec is not None:
            bot._decision_baseline_target_sec = float(baseline_target_sec)
        if refinement_budget_sec is not None:
            bot._decision_refinement_budget_sec = float(refinement_budget_sec)
        bot._strategy_max_refinement_candidates = max_refinement_candidates
        _configure_decision_state(bot, scenario)
        effective_request = bot._request()
        if request_overrides:
            effective_request = copy.deepcopy(effective_request)
            for key, value in request_overrides.items():
                if isinstance(value, dict) and isinstance(effective_request.get(key), dict):
                    effective_request[key].update(copy.deepcopy(value))
                else:
                    effective_request[key] = copy.deepcopy(value)
            bot._request = lambda: copy.deepcopy(effective_request)
        hand_runtime = effective_request.get("hand_runtime") or {}
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
            "hand_runtime": hand_runtime,
            "worker_seed": getattr(bot, "_strategy_worker_seed", None),
        }
    finally:
        try:
            bot.close()
        except BaseException:
            pass
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)


def _action_signature(action: dict[str, Any]) -> tuple:
    """Supplemental sanitized trajectory, independent of reported metadata."""
    metrics = action.get("runtime_metrics") or {}
    trajectory = []
    baseline = metrics.get("strategy_baseline_action")
    if baseline is not None:
        trajectory.append(int(baseline))
    for item in metrics.get("refinement_progress") or []:
        if isinstance(item, dict) and item.get("action") is not None:
            trajectory.append(int(item["action"]))
    return tuple(trajectory), action.get("internal"), action.get("wire")


def _final_wire_action_changed(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Require a completed decision to change what the wrapper would send.

    Intermediate anytime candidates are useful diagnostics, but they are not a
    behavioral adaptation if the final sanitized wire action is unchanged.
    """
    if "error" in left or "error" in right:
        return False
    left_wire = left.get("wire")
    right_wire = right.get("wire")
    return bool(
        isinstance(left_wire, str)
        and isinstance(right_wire, str)
        and left_wire
        and right_wire
        and left_wire != right_wire
    )


def _tier_specs() -> tuple[tuple[str, dict[str, Any]], ...]:
    return (
        ("baseline", {"max_refinement_candidates": 0}),
        (
            "short",
            {
                "hard_deadline_sec": 0.22,
                "baseline_target_sec": 0.05,
                "refinement_budget_sec": 0.12,
            },
        ),
        (
            "long",
            {
                "hard_deadline_sec": 0.68,
                "baseline_target_sec": 0.05,
                "refinement_budget_sec": 0.55,
            },
        ),
    )


def _profile_action(
    imports: CandidateImports,
    scenario: dict[str, Any],
    snapshot: dict[str, Any] | None,
    **kwargs,
) -> dict[str, Any]:
    _native, bot = _new_native_bot(imports)
    if snapshot is not None:
        bot._opponent_tracker = FixedSnapshotTracker(snapshot)
    return _timed_formal_action(bot, scenario, **kwargs)


def _probe_strategy_influence(imports: CandidateImports, tracker_result: dict[str, Any]):
    result = {
        "ok": False,
        "issues": [],
        "rows": [],
        "changed_pairs": 0,
        "dimensions": {},
    }
    try:
        scenarios = {scenario["id"]: scenario for scenario in DECISION_SCENARIOS}
        baseline_snapshot = None
        aggressive_snapshot = tracker_result["aggressive_bot"]._opponent_tracker.snapshot()
        passive_snapshot = tracker_result["passive_bot"]._opponent_tracker.snapshot()
        style_rows = []
        style_changes = 0
        for scenario_id in ACTION_PROFILE_SCENARIO_IDS:
            scenario = scenarios[scenario_id]
            row = {"scenario_id": scenario["id"]}
            for label, snapshot in (
                ("baseline", baseline_snapshot),
                ("aggressive", aggressive_snapshot),
                ("passive", passive_snapshot),
            ):
                try:
                    row[label] = _profile_action(
                        imports,
                        scenario,
                        snapshot,
                        max_refinement_candidates=0,
                    )
                except BaseException as exc:
                    row[label] = {"error": f"{type(exc).__name__}:{str(exc)[:160]}"}
            baseline_wire = row["baseline"].get("wire")
            changed = sum(
                row[label].get("wire") != baseline_wire
                for label in ("aggressive", "passive")
                if "error" not in row[label] and baseline_wire is not None
            )
            style_changes += changed
            style_rows.append(row)
        successful = [
            row for row in style_rows
            if all("error" not in row[label] for label in ("baseline", "aggressive", "passive"))
        ]
        if len(successful) < 2:
            result["issues"].append("strategy_probe_insufficient_formal_actions")
        if style_changes < 1:
            result["issues"].append("opponent_runtime_no_observable_sanitized_action_influence")

        terminal_caller_snapshot = (
            tracker_result["terminal_caller_bot"]._opponent_tracker.snapshot()
        )
        terminal_folder_observed = (
            tracker_result["terminal_folder_bot"]._opponent_tracker.snapshot()
        )
        terminal_folder_snapshot = copy.deepcopy(terminal_caller_snapshot)
        for key in (
            "fold_to_raise",
            "fold_to_jam_rate",
            "fold_to_jam_samples",
            "river_overcall_freq",
            "river_overcall_samples",
            "facing_raise_by_street",
            "facing_allin_by_street",
            "contexts",
            "terminal_response",
        ):
            terminal_folder_snapshot[key] = copy.deepcopy(terminal_folder_observed.get(key))
        for section, keys in (
            ("rates", ("fold_to_raise", "fold_to_allin")),
            ("samples", ("fold_to_raise", "fold_to_allin")),
        ):
            terminal_folder_snapshot[section] = copy.deepcopy(
                terminal_caller_snapshot.get(section) or {}
            )
            for key in keys:
                terminal_folder_snapshot[section][key] = copy.deepcopy(
                    (terminal_folder_observed.get(section) or {}).get(key)
                )
        terminal_rows = []
        terminal_changes = 0
        for scenario_id in TERMINAL_RESPONSE_SCENARIO_IDS:
            scenario = scenarios[scenario_id]
            row = {"scenario_id": scenario_id, "tiers": {}}
            for tier, kwargs in _tier_specs():
                tier_row = {}
                for label, snapshot in (
                    ("terminal_folder", terminal_folder_snapshot),
                    ("terminal_caller", terminal_caller_snapshot),
                ):
                    try:
                        tier_row[label] = _profile_action(
                            imports, scenario, snapshot, **kwargs
                        )
                    except BaseException as exc:
                        tier_row[label] = {
                            "error": f"{type(exc).__name__}:{str(exc)[:160]}"
                        }
                tier_row["trajectory_changed"] = (
                    _action_signature(tier_row["terminal_folder"])
                    != _action_signature(tier_row["terminal_caller"])
                )
                tier_row["changed"] = _final_wire_action_changed(
                    tier_row["terminal_folder"], tier_row["terminal_caller"]
                )
                row["tiers"][tier] = tier_row
            terminal_changes += int(
                any(item.get("changed") for item in row["tiers"].values())
            )
            terminal_rows.append(row)
        if terminal_changes < 1:
            result["issues"].append(
                "terminal_response_runtime_no_observable_sanitized_action_influence"
            )

        loose_snapshot = tracker_result["loose_showdown_bot"]._opponent_tracker.snapshot()
        tight_observed = tracker_result["tight_showdown_bot"]._opponent_tracker.snapshot()
        tight_snapshot = copy.deepcopy(loose_snapshot)
        tight_snapshot["showdown_range"] = copy.deepcopy(tight_observed["showdown_range"])
        showdown_rows = []
        showdown_changes = 0
        for scenario_id in SHOWDOWN_RANGE_SCENARIO_IDS:
            scenario = scenarios[scenario_id]
            row = {"scenario_id": scenario_id, "tiers": {}}
            for tier, kwargs in _tier_specs():
                tier_row = {}
                for label, snapshot in (
                    ("tight_showdown", tight_snapshot),
                    ("loose_showdown", loose_snapshot),
                ):
                    try:
                        tier_row[label] = _profile_action(
                            imports, scenario, snapshot, **kwargs
                        )
                    except BaseException as exc:
                        tier_row[label] = {
                            "error": f"{type(exc).__name__}:{str(exc)[:160]}"
                        }
                tier_row["trajectory_changed"] = (
                    _action_signature(tier_row["tight_showdown"])
                    != _action_signature(tier_row["loose_showdown"])
                )
                tier_row["changed"] = _final_wire_action_changed(
                    tier_row["tight_showdown"], tier_row["loose_showdown"]
                )
                row["tiers"][tier] = tier_row
            showdown_changes += int(
                any(item.get("changed") for item in row["tiers"].values())
            )
            showdown_rows.append(row)
        if showdown_changes < 1:
            result["issues"].append(
                "showdown_range_no_observable_sanitized_action_influence"
            )

        line_rows = []
        line_changes = 0
        for pair in LINE_SCENARIO_PAIRS:
            scenario = scenarios[pair["positive"]]
            positive_context = copy.deepcopy(scenario["expected_hand_runtime"])
            negative_context = copy.deepcopy(positive_context)
            negative_context[pair["flag"]] = False
            row = {"dimension": pair["dimension"], "tiers": {}}
            for tier, kwargs in _tier_specs():
                tier_row = {}
                for label, context in (
                    ("positive", positive_context),
                    ("negative", negative_context),
                ):
                    try:
                        tier_row[label] = _profile_action(
                            imports,
                            scenario,
                            None,
                            request_overrides={"hand_runtime": context},
                            **kwargs,
                        )
                    except BaseException as exc:
                        tier_row[label] = {
                            "error": f"{type(exc).__name__}:{str(exc)[:160]}"
                        }
                tier_row["trajectory_changed"] = (
                    _action_signature(tier_row["positive"])
                    != _action_signature(tier_row["negative"])
                )
                tier_row["changed"] = _final_wire_action_changed(
                    tier_row["positive"], tier_row["negative"]
                )
                row["tiers"][tier] = tier_row
            changed = any(item.get("changed") for item in row["tiers"].values())
            line_changes += int(changed)
            if not changed:
                result["issues"].append(
                    f"{pair['dimension']}_line_no_observable_sanitized_action_influence"
                )
            line_rows.append(row)

        result["rows"] = style_rows
        result["changed_pairs"] = (
            style_changes + terminal_changes + showdown_changes + line_changes
        )
        result["dimensions"] = {
            "action_profile": {
                "ok": style_changes >= 1 and len(successful) >= 2,
                "changed_pairs": style_changes,
                "rows": style_rows,
            },
            "terminal_response": {
                "ok": terminal_changes >= 1,
                "changed_pairs": terminal_changes,
                "rows": terminal_rows,
            },
            "showdown_range": {
                "ok": showdown_changes >= 1,
                "changed_pairs": showdown_changes,
                "rows": showdown_rows,
            },
            "semantic_lines": {
                "ok": line_changes == len(LINE_SCENARIO_PAIRS),
                "changed_pairs": line_changes,
                "rows": line_rows,
            },
        }
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
        "safety_ok": False,
        "safety_issues": [],
        "baseline_ok": False,
        "baseline_issues": [],
        "refinement_ok": False,
        "refinement_issues": [],
        "refinement_evidence": [],
        "budget_scaling": {},
        "baseline_samples_ms": [],
        "fallback_ready_samples_ms": [],
        "timeout_recovery": {},
    }
    metrics_rows = []
    for row in strategy_result.get("rows") or []:
        for label in ("baseline", "aggressive", "passive"):
            action = row.get(label) or {}
            metrics = action.get("runtime_metrics") or {}
            if metrics:
                metrics_rows.append(metrics)
    if not metrics_rows:
        result["safety_issues"].append("decision_runtime_metrics_missing")
    for metrics in metrics_rows:
        try:
            actual_runtime_version = int(metrics.get("runtime_version") or 0)
            if actual_runtime_version != expected_runtime_version:
                result["safety_issues"].append(
                    "decision_runtime_version_mismatch:"
                    f"expected={expected_runtime_version}:actual={actual_runtime_version}"
                )
        except (TypeError, ValueError):
            result["safety_issues"].append("decision_runtime_version_invalid")
        try:
            fallback_ms = metrics.get("socket_fallback_ready_ms")
            if fallback_ms is not None:
                result["fallback_ready_samples_ms"].append(float(fallback_ms))
        except (TypeError, ValueError):
            result["safety_issues"].append("socket_fallback_timing_invalid")
        try:
            baseline_ms = metrics.get("baseline_published_ms")
            if baseline_ms is not None:
                result["baseline_samples_ms"].append(float(baseline_ms))
        except (TypeError, ValueError):
            result["baseline_issues"].append("strategy_baseline_timing_invalid")
    if not result["fallback_ready_samples_ms"]:
        result["safety_issues"].append("socket_fallback_timing_missing")
    elif max(result["fallback_ready_samples_ms"]) > 25.0:
        result["safety_issues"].append("socket_fallback_slower_than_25ms")
    if not result["baseline_samples_ms"]:
        result["baseline_issues"].append("strategy_baseline_never_published")
    elif max(result["baseline_samples_ms"]) > 250.0:
        result["baseline_issues"].append("strategy_baseline_slower_than_250ms")

    try:
        scaling_scenario = next(
            scenario for scenario in DECISION_SCENARIOS
            if scenario["id"] == "river_facing_large_bet"
        )
        _native, short_bot = _new_native_bot(imports)
        _native, long_bot = _new_native_bot(imports)
        fixed_worker_seed = 20260710
        short_bot._strategy_base_seed = fixed_worker_seed
        long_bot._strategy_base_seed = fixed_worker_seed
        short_bot._ensure_strategy_worker()
        long_bot._ensure_strategy_worker()
        # Sample one real multi-fidelity stratum.  The broad probe matrix stays
        # sub-second, but source/candidate strength matches otherwise run near a
        # two-second local ceiling and could never reveal useful anytime work
        # after that cutoff.  A fixed 2s control versus an 8s treatment records
        # the final sanitized action plus system-observed work/CPU/elapsed facts
        # without paying a 55s cost on every scenario.
        short_budget = {
            "hard_deadline_sec": 2.0,
            "baseline_target_sec": 0.20,
            "refinement_budget_sec": 1.8,
        }
        long_budget = {
            "hard_deadline_sec": 8.0,
            "baseline_target_sec": 0.20,
            "refinement_budget_sec": 7.5,
        }
        short_action = _timed_formal_action(
            short_bot,
            scaling_scenario,
            **short_budget,
        )
        long_action = _timed_formal_action(
            long_bot,
            scaling_scenario,
            **long_budget,
        )

        def final_progress(action):
            metrics = action.get("runtime_metrics") or {}
            return {
                "trusted_steps": int(metrics.get("trusted_refinement_steps") or 0),
                "trusted_cpu_ms": round(
                    float(metrics.get("trusted_refinement_cpu_ms") or 0.0), 6
                ),
                "trusted_elapsed_ms": round(
                    float(metrics.get("trusted_refinement_elapsed_ms") or 0.0), 6
                ),
                "iterator_exhausted": bool(
                    metrics.get("refinement_iterator_exhausted")
                ),
                "termination_reason": str(
                    metrics.get("refinement_termination_reason") or ""
                ),
                "action_changes": int(
                    metrics.get("refinement_action_changes") or 0
                ),
                "wire": action.get("wire"),
                "worker_seed": action.get("worker_seed"),
            }

        short_progress = final_progress(short_action)
        long_progress = final_progress(long_action)
        result["budget_scaling"] = {
            "probe_kind": "sampled_multifidelity_2s_vs_8s",
            "short_budget": short_budget,
            "long_budget": long_budget,
            "short": short_progress,
            "long": long_progress,
            "worker_seed_equal": (
                short_progress["worker_seed"] is not None
                and short_progress["worker_seed"] == long_progress["worker_seed"]
            ),
        }
        if short_progress["worker_seed"] != long_progress["worker_seed"]:
            result["refinement_issues"].append(
                "multifidelity_worker_seed_mismatch"
            )
        if long_progress["trusted_steps"] < 8:
            result["refinement_issues"].append("long_budget_refinement_has_no_bounded_work")
        if long_progress["trusted_elapsed_ms"] < 5.0:
            result["refinement_issues"].append(
                "long_budget_refinement_has_no_measured_compute"
            )
        if not (
            long_progress["trusted_steps"] > short_progress["trusted_steps"]
            or (
                short_progress["iterator_exhausted"]
                and long_progress["iterator_exhausted"]
                and short_progress["trusted_steps"] == long_progress["trusted_steps"]
                and long_progress["trusted_steps"] >= 8
            )
        ):
            result["refinement_issues"].append(
                "long_budget_does_not_scale_trusted_work_or_exhaust_finite_batch"
            )
        if max(
            short_progress["action_changes"], long_progress["action_changes"]
        ) < 1:
            result["refinement_issues"].append(
                "refinement_never_changes_sanitized_baseline_action"
            )
        result["refinement_evidence"].append({
            "scenario_id": scaling_scenario["id"],
            "probe_kind": "sampled_multifidelity_2s_vs_8s",
            "short_budget": short_budget,
            "long_budget": long_budget,
            "short": short_progress,
            "long": long_progress,
            "final_sanitized_action_changed": (
                short_progress["wire"] != long_progress["wire"]
            ),
            "candidate_reported_metadata_is_non_authoritative": True,
        })
    except BaseException as exc:
        result["refinement_issues"].append(
            "decision_runtime_refinement_probe_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        )

    try:
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
        timeout_action = None
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
        if not isinstance(timeout_action, dict):
            raise RuntimeError("hanging_strategy_probe_returned_no_action")
        timeout_metrics = timeout_action.get("runtime_metrics") or {}
        if timeout_action.get("wire") != "fold":
            result["safety_issues"].append("positive_to_call_timeout_fallback_not_fold")
        if timeout_metrics.get("timed_out") is not True:
            result["safety_issues"].append("hanging_strategy_not_timed_out")
        if timeout_metrics.get("worker_terminated") is not True:
            result["safety_issues"].append("hanging_strategy_worker_not_terminated")

        recovery_action = _timed_formal_action(timeout_bot, DECISION_SCENARIOS[0])
        recovery_metrics = recovery_action.get("runtime_metrics") or {}
        if int(recovery_metrics.get("decision_id") or 0) <= int(
            timeout_metrics.get("decision_id") or 0
        ):
            result["safety_issues"].append("decision_id_did_not_advance_after_timeout")
        if int(recovery_metrics.get("worker_generation") or 0) <= int(
            timeout_metrics.get("worker_generation") or 0
        ):
            result["safety_issues"].append("worker_not_restarted_after_timeout")
        if recovery_metrics.get("timed_out") is True:
            result["safety_issues"].append("recovery_decision_timed_out")
        result["timeout_recovery"] = {
            "timeout": timeout_action,
            "recovery": recovery_action,
        }
    except BaseException as exc:
        result["safety_issues"].append(
            "decision_runtime_safety_probe_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        )
    result["baseline_samples_ms"] = [
        round(value, 3) for value in result["baseline_samples_ms"][:64]
    ]
    result["fallback_ready_samples_ms"] = [
        round(value, 3) for value in result["fallback_ready_samples_ms"][:64]
    ]
    result["safety_issues"] = list(dict.fromkeys(result["safety_issues"]))
    result["baseline_issues"] = list(dict.fromkeys(result["baseline_issues"]))
    result["refinement_issues"] = list(dict.fromkeys(result["refinement_issues"]))
    result["safety_ok"] = not result["safety_issues"]
    result["baseline_ok"] = not result["baseline_issues"]
    result["refinement_ok"] = not result["refinement_issues"]
    result["issues"] = list(dict.fromkeys([
        *result["safety_issues"],
        *result["baseline_issues"],
        *result["refinement_issues"],
    ]))
    result["ok"] = (
        result["safety_ok"]
        and result["baseline_ok"]
        and result["refinement_ok"]
    )
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


# ``bytes.translate`` keeps a packed precomputed row's type and length while
# mutating it in C rather than spending a Python loop on a multi-megabyte row.
# Packed equity/texture rows are a realistic space-for-time representation, so
# they must receive the same value-sensitive proof as scalar lookup values.
_BYTES_COUNTERFACTUAL_TRANSLATION = bytes.maketrans(
    bytes(range(256)),
    bytes(reversed(range(256))),
)


def _counterfactual_value(value: Any, depth: int = 0) -> Any:
    """Return a same-shape but intentionally different lookup value.

    This is not a poker counterfactual and is never exposed to a bot in a real
    match.  It is a trusted probe mutation: if an artifact is selected as the
    strategy primary, changing its values must be able to change at least one
    final sanitized wire action.  Preserve common container shapes so a real
    consumer keeps running rather than merely failing type checks.
    """
    if depth >= 4:
        return value
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return -value - 1
    if isinstance(value, float):
        return -value - 1.0
    if isinstance(value, bytes):
        return value.translate(_BYTES_COUNTERFACTUAL_TRANSLATION)
    if isinstance(value, bytearray):
        return bytearray(bytes(value).translate(_BYTES_COUNTERFACTUAL_TRANSLATION))
    if isinstance(value, str):
        return value + "__probe_counterfactual__"
    if isinstance(value, tuple):
        return tuple(_counterfactual_value(item, depth + 1) for item in value)
    if isinstance(value, list):
        return [_counterfactual_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            key: _counterfactual_value(item, depth + 1)
            for key, item in value.items()
        }
    return value


def _counterfactual_mapping(value: dict[Any, Any]) -> dict[Any, Any]:
    return {key: _counterfactual_value(item) for key, item in value.items()}


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
                        counting.reset_key_observations()
                        try:
                            _native, probe_bot = _new_native_bot(imports)
                            action = _timed_formal_action(
                                probe_bot,
                                scenario,
                                max_refinement_candidates=0,
                            )
                            consumer_scenarios.append({
                                "scenario_id": scenario["id"],
                                "reads": counting.reads - before_reads,
                                "action": action,
                                **counting.key_observation(),
                            })
                        except BaseException as exc:
                            consumer_scenarios.append({
                                "scenario_id": scenario["id"],
                                "reads": counting.reads - before_reads,
                                "error": f"{type(exc).__name__}:{str(exc)[:140]}",
                                **counting.key_observation(),
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
                key_fingerprints = {
                    str(item.get("lookup_key_fingerprint") or "")
                    for item in row.get("consumer_scenarios") or []
                    if isinstance(item, dict)
                    and "error" not in item
                    and int(item.get("reads") or 0) > 0
                    and int(item.get("lookup_key_count") or 0) > 0
                    and item.get("lookup_key_fingerprint")
                }
                row["lookup_key_fingerprints"] = sorted(key_fingerprints)
                row["lookup_key_varies_across_consumer_scenarios"] = (
                    len(key_fingerprints) >= 2
                )

                counterfactual = CountingDict(_counterfactual_mapping(value))
                changed = _replace_identity(imports, value, counterfactual)
                try:
                    counterfactual_scenarios = []
                    for scenario in DECISION_SCENARIOS:
                        before_reads = counterfactual.reads
                        try:
                            _native, counterfactual_bot = _new_native_bot(imports)
                            action = _timed_formal_action(
                                counterfactual_bot,
                                scenario,
                                max_refinement_candidates=0,
                            )
                            counterfactual_scenarios.append({
                                "scenario_id": scenario["id"],
                                "reads": counterfactual.reads - before_reads,
                                "action": action,
                            })
                        except BaseException as exc:
                            counterfactual_scenarios.append({
                                "scenario_id": scenario["id"],
                                "reads": counterfactual.reads - before_reads,
                                "error": f"{type(exc).__name__}:{str(exc)[:140]}",
                            })
                    row["counterfactual_scenarios"] = counterfactual_scenarios
                    original_by_id = {
                        str(item.get("scenario_id")): item
                        for item in row.get("consumer_scenarios") or []
                        if isinstance(item, dict)
                    }
                    action_influence_scenarios = [
                        item["scenario_id"]
                        for item in counterfactual_scenarios
                        if isinstance(item, dict)
                        and isinstance(original_by_id.get(str(item.get("scenario_id"))), dict)
                        and _final_wire_action_changed(
                            original_by_id[str(item["scenario_id"])].get("action") or {},
                            item.get("action") or {},
                        )
                    ]
                    row["action_influence_scenarios"] = action_influence_scenarios
                    row["value_affects_final_wire"] = bool(action_influence_scenarios)
                finally:
                    _restore_identity(changed, value)

                empty = CountingDict({})
                changed = _replace_identity(imports, value, empty)
                try:
                    fallback_scenarios = []
                    for scenario in DECISION_SCENARIOS:
                        try:
                            _native, fallback_bot = _new_native_bot(imports)
                            fallback_scenarios.append({
                                "scenario_id": scenario["id"],
                                "action": _timed_formal_action(
                                    fallback_bot,
                                    scenario,
                                    max_refinement_candidates=0,
                                ),
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
    for key in (
        "aggressive_bot",
        "passive_bot",
        "tight_showdown_bot",
        "loose_showdown_bot",
        "terminal_folder_bot",
        "terminal_caller_bot",
    ):
        result.pop(key, None)
    for style in (
        "aggressive",
        "passive",
        "tight_showdown",
        "loose_showdown",
        "terminal_folder",
        "terminal_caller",
    ):
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
    os.environ["POK_NATIVE_BOT_SEED"] = "20260710"
    sys.path.insert(0, str(root))
    imports = CandidateImports(root)
    _phase("tracker")
    tracker = _probe_tracker(imports)
    _phase("hand_context")
    hand_context = _probe_hand_context(imports)
    _phase("strategy_influence")
    strategy = _probe_strategy_influence(imports, tracker) if tracker.get("ok") else {
        "ok": False,
        "issues": ["strategy_probe_skipped_tracker_failed"],
        "rows": [],
        "changed_pairs": 0,
    }
    expected_runtime_version = int(spec.get("expected_decision_runtime_version") or 0)
    _phase("decision_runtime")
    decision_runtime = (
        _probe_decision_runtime(
            imports,
            strategy,
            expected_runtime_version=expected_runtime_version,
        )
        if tracker.get("ok") and strategy.get("rows")
        else {
            "ok": False,
            "issues": ["decision_runtime_probe_skipped_strategy_failed"],
            "safety_ok": False,
            "safety_issues": ["decision_runtime_probe_skipped_strategy_failed"],
            "baseline_ok": False,
            "baseline_issues": ["decision_runtime_probe_skipped_strategy_failed"],
            "refinement_ok": False,
            "refinement_issues": ["decision_runtime_probe_skipped_strategy_failed"],
            "baseline_samples_ms": [],
            "fallback_ready_samples_ms": [],
            "timeout_recovery": {},
        }
    )
    _phase("artifacts")
    artifacts = _probe_artifacts(imports, spec.get("artifacts") or [])
    _phase("report")
    issues = [
        *(tracker.get("issues") or []),
        *(hand_context.get("issues") or []),
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
        "hand_context": hand_context,
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
