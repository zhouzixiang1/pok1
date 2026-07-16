"""Trusted typed-policy probe executed in the managed candidate sandbox.

Only the current national ABI is exercised: raw delimiter-free official wire
messages enter the system-owned runtime, the runtime builds ``decision_context
v1``, and candidate ``policy.py`` returns typed intents.  No compatibility
state is synthesized inside this worker.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import random
import re
import resource
import signal
import sys
import time
from typing import Any, Callable


sys.path.insert(0, str(Path(__file__).resolve().parent))

from national_runtime_probe_scenarios import (
    DECISION_SCENARIOS,
    LINE_SCENARIO_PAIRS,
    RUNTIME_PROBE_SCENARIO_DIGEST,
    RUNTIME_PROBE_SCENARIO_VERSION,
)


PROBE_WORKER_VERSION = 16
PHASE_PATH = Path("/output/phase.txt")
MAX_CAPTURE_CHARS = 64 * 1024
EXPECTED_CONTEXT_FIELDS = frozenset({
    "schema_version",
    "runtime_version",
    "decision_id",
    "cards",
    "hand",
    "betting",
    "history",
    "line",
    "legal",
    "opponent",
    "deadline",
})
INTENT_KINDS = frozenset({"pass", "fold", "allin", "raise"})
CANONICAL_ACTION_RE = re.compile(r"(?:fold|call|check|allin|raise [0-9]+)\Z")
STABLE_CONTEXT_NORMALIZATION_PATHS = (
    "deadline.hard_monotonic",
    "deadline.refinement_monotonic",
)


def _phase(name: str) -> None:
    try:
        PHASE_PATH.write_text(name, encoding="utf-8")
    except OSError:
        pass


class CappedTextIO(io.TextIOBase):
    def __init__(self, limit: int = MAX_CAPTURE_CHARS) -> None:
        self.limit = int(limit)
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
    """Capture system-owned outbound actions without changing framing."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def sendall(self, payload: bytes) -> None:
        text = payload.decode("ascii", errors="strict")
        if "\r" in text or "\n" in text:
            raise ValueError("official_wire_action_contains_delimiter")
        if not CANONICAL_ACTION_RE.fullmatch(text):
            raise ValueError(f"official_wire_action_not_canonical:{text!r}")
        self.sent.append(text)


class CandidateImports:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules: dict[str, Any] = {}
        self.diagnostics: dict[str, dict[str, Any]] = {}
        # A production probe owns a fresh interpreter.  Clearing these exact
        # closed-ABI names also makes in-process contract tests faithful and
        # prevents an earlier candidate module from being reused accidentally.
        for module_name in ("national_bot", "precompute", "policy"):
            sys.modules.pop(module_name, None)
        importlib.invalidate_caches()

    def load(self, module_name: str):
        if module_name in self.modules:
            return self.modules[module_name]
        stdout = CappedTextIO()
        stderr = CappedTextIO()
        started = time.monotonic()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                module = importlib.import_module(module_name)
        finally:
            self.diagnostics[module_name] = {
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "stdout": stdout.getvalue(),
                "stdout_truncated": stdout.truncated,
                "stderr": stderr.getvalue(),
                "stderr_truncated": stderr.truncated,
            }
        self.modules[module_name] = module
        return module


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


def _new_native_bot(imports: CandidateImports):
    native = imports.load("national_bot")
    bot = native.NativeNationalBot("TypedProbeB", "lower")
    bot._official_action_delay_sec = 0.0
    return native, bot


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _stable_context(context: dict[str, Any]) -> dict[str, Any]:
    stable = copy.deepcopy(context)
    deadline = stable.get("deadline")
    if isinstance(deadline, dict):
        for path in STABLE_CONTEXT_NORMALIZATION_PATHS:
            deadline.pop(path.split(".", 1)[1], None)
    return stable


def _validate_context(
    scenario: dict[str, Any],
    context: dict[str, Any],
) -> list[str]:
    scenario_id = str(scenario["id"])
    issues: list[str] = []
    if set(context) != EXPECTED_CONTEXT_FIELDS:
        issues.append(
            f"{scenario_id}:decision_context_fields_mismatch:"
            f"{sorted(set(context).symmetric_difference(EXPECTED_CONTEXT_FIELDS))}"
        )
    if context.get("schema_version") != 1:
        issues.append(f"{scenario_id}:decision_context_schema_not_v1")
    cards = context.get("cards") or {}
    if cards.get("encoding") != "national_tcp_suit_rank_v1":
        issues.append(f"{scenario_id}:national_card_encoding_mismatch")
    for card in [*(cards.get("hole") or []), *(cards.get("board") or [])]:
        if (
            type(card) is not dict
            or type(card.get("suit")) is not int
            or type(card.get("rank")) is not int
            or not 0 <= card["suit"] <= 3
            or not 0 <= card["rank"] <= 12
        ):
            issues.append(f"{scenario_id}:invalid_native_card:{card!r}")
            break

    expected = scenario.get("expected") or {}
    hand = context.get("hand") or {}
    betting = context.get("betting") or {}
    line = context.get("line") or {}
    legal = context.get("legal") or {}
    comparisons = {
        "street": hand.get("street"),
        "position": hand.get("position"),
        "acts_first_postflop": hand.get("acts_first_postflop"),
        "to_call": betting.get("to_call"),
        "preflop_aggressor": line.get("preflop_aggressor"),
        "street_open": line.get("street_open"),
        "responding_to_check": line.get("responding_to_check"),
        "can_donk": line.get("can_donk"),
        "can_delayed_probe": line.get("can_delayed_probe"),
        "previous_street_checked_through": (
            (line.get("previous_street") or {}).get("checked_through")
        ),
        "previous_street_opponent_checked_back": (
            (line.get("previous_street") or {}).get("opponent_checked_back")
        ),
        "pass_wire_kind": legal.get("pass_wire_kind"),
    }
    for field, expected_value in expected.items():
        if field in comparisons and comparisons[field] != expected_value:
            issues.append(
                f"{scenario_id}:context_{field}_mismatch:"
                f"expected={expected_value!r}:actual={comparisons[field]!r}"
            )
    hero_stack = betting.get("hero_stack")
    opponent_stack = betting.get("opponent_stack")
    pot = betting.get("pot")
    chip_fields_valid = all(
        type(value) is int for value in (hero_stack, opponent_stack, pot)
    )
    if not chip_fields_valid:
        issues.append(f"{scenario_id}:betting_chip_fields_not_integers")
    elif pot != 40_000 - hero_stack - opponent_stack:
        issues.append(f"{scenario_id}:pot_stack_conservation_mismatch")
    to_call = betting.get("to_call")
    if type(to_call) is int and chip_fields_valid:
        expected_spr = round(
            min(hero_stack, opponent_stack) / max(1.0, float(pot)),
            6,
        )
        expected_pot_odds = round(
            to_call / max(1.0, float(pot + to_call)),
            6,
        )
        if betting.get("spr") != expected_spr:
            issues.append(f"{scenario_id}:spr_not_derived_from_authoritative_state")
        if betting.get("pot_odds") != expected_pot_odds:
            issues.append(
                f"{scenario_id}:pot_odds_not_derived_from_authoritative_state"
            )
    position = hand.get("position")
    if line.get("position") != position:
        issues.append(f"{scenario_id}:hand_line_position_disagree")
    if hand.get("acts_first_postflop") is not (position == "big_blind"):
        issues.append(f"{scenario_id}:acts_first_postflop_position_disagree")
    if line.get("hero_in_position_postflop") is not (position == "small_blind"):
        issues.append(f"{scenario_id}:postflop_in_position_flag_disagree")
    match_control = hand.get("match_control")
    match_control_fields = {
        "schema_version",
        "initial_chips",
        "small_blind",
        "big_blind",
        "current_position",
        "current_exposure",
        "future_forced_blinds",
        "forced_fold_loss_bound",
        "hero_net_earned",
        "fold_locks_win",
    }
    if type(match_control) is not dict or set(match_control) != match_control_fields:
        issues.append(f"{scenario_id}:match_control_shape_mismatch")
    elif chip_fields_valid:
        remaining = hand.get("remaining_including_current")
        opponent = context.get("opponent") or {}
        match_result = opponent.get("match_result") or {}
        if type(remaining) is not int or not 1 <= remaining <= 70:
            issues.append(f"{scenario_id}:match_control_remaining_invalid")
        else:
            future_hands = remaining - 1
            pairs, odd = divmod(future_hands, 2)
            future_blinds = pairs * 150
            if odd:
                future_blinds += 100 if position == "small_blind" else 50
            exposure = 20_000 - hero_stack
            hero_net = match_result.get("hero_net_earned")
            expected_control = {
                "schema_version": 1,
                "initial_chips": 20_000,
                "small_blind": 50,
                "big_blind": 100,
                "current_position": position,
                "current_exposure": exposure,
                "future_forced_blinds": future_blinds,
                "forced_fold_loss_bound": exposure + future_blinds,
                "hero_net_earned": hero_net,
                "fold_locks_win": (
                    type(hero_net) is int
                    and hero_net > exposure + future_blinds
                ),
            }
            if match_control != expected_control:
                issues.append(f"{scenario_id}:match_control_derivation_mismatch")
    closing = betting.get("call_closes_allin_runout")
    expected_closing = bool(
        type(to_call) is int
        and to_call > 0
        and chip_fields_valid
        and (opponent_stack == 0 or hero_stack <= to_call)
    )
    if type(closing) is not bool or closing is not expected_closing:
        issues.append(f"{scenario_id}:allin_call_closure_derivation_mismatch")
    history = context.get("history")
    if type(history) is not dict:
        issues.append(f"{scenario_id}:semantic_history_not_mapping")
    else:
        actions = history.get("actions") or []
        for action in actions:
            if type(action) is not dict or not {
                "street",
                "street_index",
                "actor",
                "wire_kind",
                "semantic_kind",
                "committed",
                "stage_bet_after",
                "inferred",
            }.issubset(action):
                issues.append(f"{scenario_id}:semantic_history_action_shape_invalid")
                break
        expected_boundary = expected.get("inferred_boundary")
        if expected_boundary and not any(
            action.get("inferred") is True
            and action.get("inference_boundary") == expected_boundary
            for action in actions
            if type(action) is dict
        ):
            issues.append(
                f"{scenario_id}:omitted_closer_not_inferred_at_"
                f"{expected_boundary}"
            )
    if type(context.get("opponent")) is not dict:
        issues.append(f"{scenario_id}:opponent_snapshot_not_mapping")
    return issues


def _validate_typed_intent(
    raw: Any,
    context: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if type(raw) is not dict or not set(raw).issubset({"kind", "raise_to"}):
        return None, "typed_intent_not_closed_mapping"
    kind = raw.get("kind")
    if type(kind) is not str or kind not in INTENT_KINDS:
        return None, "typed_intent_kind_invalid"
    legal = context.get("legal") or {}
    if kind not in set(legal.get("policy_kinds") or ()):
        return None, f"typed_intent_kind_not_legal:{kind}"
    if kind != "raise":
        if set(raw) != {"kind"}:
            return None, "typed_non_raise_has_raise_to"
        return {"kind": kind}, None
    if set(raw) != {"kind", "raise_to"} or type(raw.get("raise_to")) is not int:
        return None, "typed_raise_shape_invalid"
    minimum = legal.get("min_raise_to")
    maximum = legal.get("max_raise_to")
    target = raw["raise_to"]
    if type(minimum) is not int or type(maximum) is not int or not minimum <= target <= maximum:
        return None, "typed_raise_outside_legal_boundary"
    return {"kind": "raise", "raise_to": target}, None


def _runtime_metric_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "runtime_version": metrics.get("runtime_version"),
        "worker_seed": metrics.get("worker_seed"),
        "socket_fallback_decision": metrics.get("socket_fallback_decision"),
        "baseline_published": metrics.get("baseline_published_ms") is not None,
        "baseline_target_met": bool(metrics.get("baseline_target_met")),
        "policy_baseline_decision": metrics.get("policy_baseline_decision"),
        "refinement_messages": int(metrics.get("refinement_messages") or 0),
        "refinement_decision_changes": int(
            metrics.get("refinement_decision_changes") or 0
        ),
        "trusted_refinement_steps": int(
            metrics.get("trusted_refinement_steps") or 0
        ),
        "trusted_refinement_cpu_ms": round(
            float(metrics.get("trusted_refinement_cpu_ms") or 0.0), 6
        ),
        "trusted_refinement_elapsed_ms": round(
            float(metrics.get("trusted_refinement_elapsed_ms") or 0.0), 6
        ),
        "refinement_iterator_exhausted": bool(
            metrics.get("refinement_iterator_exhausted")
        ),
        "refinement_termination_reason": str(
            metrics.get("refinement_termination_reason") or ""
        ),
        "timed_out": bool(metrics.get("timed_out")),
        "worker_terminated": bool(metrics.get("worker_terminated")),
        "completed": bool(metrics.get("completed")),
    }


def _drive_scenario(
    imports: CandidateImports,
    scenario: dict[str, Any],
    *,
    expected_runtime_version: int,
    runtime_limits: dict[str, Any] | None = None,
    context_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _native, bot = _new_native_bot(imports)
    for attribute, value in (runtime_limits or {}).items():
        if not attribute.startswith("_") or not hasattr(bot, attribute):
            raise ValueError(f"unsupported_runtime_limit:{attribute}")
        setattr(bot, attribute, value)
    sock = MemorySocket()
    scripted = [copy.deepcopy(item) for item in scenario.get("setup_intents") or ()]
    contexts: list[dict[str, Any]] = []
    target_decisions: list[dict[str, Any]] = []
    original_build = bot._build_decision_context
    original_decide = bot._policy_decision

    def capture_context(**kwargs):
        context = original_build(**kwargs)
        if context_transform is not None:
            context = context_transform(copy.deepcopy(context))
        contexts.append(copy.deepcopy(context))
        return context

    def typed_decision():
        if scripted:
            decision = scripted.pop(0)
            bot._last_decision_source = "typed_probe_setup"
            bot._last_decision_metrics = {
                "runtime_version": expected_runtime_version,
                "probe_setup": True,
            }
            return decision
        decision = original_decide()
        target_decisions.append(copy.deepcopy(decision))
        return decision

    bot._build_decision_context = capture_context
    bot._policy_decision = typed_decision
    try:
        for message in scenario.get("messages") or ():
            if "\r" in message or "\n" in message:
                raise ValueError(
                    f"scenario_contains_delimiter:{scenario.get('id')}:{message!r}"
                )
            bot.handle(message, sock)
        if scripted:
            raise AssertionError(
                f"unused_setup_intents:{scenario.get('id')}:{len(scripted)}"
            )
        if len(contexts) != 1 or len(target_decisions) != 1:
            raise AssertionError(
                f"target_decision_count:{scenario.get('id')}:"
                f"contexts={len(contexts)}:decisions={len(target_decisions)}"
            )
        if not sock.sent:
            raise AssertionError(f"target_wire_missing:{scenario.get('id')}")
        context = contexts[0]
        decision = target_decisions[0]
        system_issues = _validate_context(scenario, context)
        candidate_issues: list[str] = []
        legal_decision, intent_issue = _validate_typed_intent(decision, context)
        if intent_issue:
            system_issues.append(f"{scenario.get('id')}:{intent_issue}")
        metrics = dict(bot._last_decision_metrics or {})
        if metrics.get("runtime_version") != expected_runtime_version:
            system_issues.append(
                f"{scenario.get('id')}:decision_runtime_version_mismatch"
            )
        if metrics.get("baseline_published_ms") is None:
            candidate_issues.append(
                f"{scenario.get('id')}:policy_baseline_not_published"
            )
        elif metrics.get("baseline_target_met") is not True:
            candidate_issues.append(
                f"{scenario.get('id')}:policy_baseline_deadline_missed"
            )
        issues = [*system_issues, *candidate_issues]
        return {
            "id": scenario["id"],
            "ok": not issues,
            "issues": issues,
            "system_issues": system_issues,
            "candidate_issues": candidate_issues,
            "context": _stable_context(context),
            "context_digest": _canonical_digest(_stable_context(context)),
            "decision": legal_decision,
            "wire": sock.sent[-1],
            "setup_wire": sock.sent[:-1],
            "runtime": _runtime_metric_summary(metrics),
        }
    finally:
        bot.close()


def _line_reachability(
    rows: list[dict[str, Any]],
    imports: CandidateImports,
    *,
    expected_runtime_version: int,
) -> dict[str, Any]:
    by_id = {str(row.get("id")): row for row in rows}
    scenarios = {str(item["id"]): item for item in DECISION_SCENARIOS}
    dimensions: dict[str, Any] = {}
    system_issues: list[str] = []
    candidate_issues: list[str] = []
    opportunity_tags = {
        "donk": "donk_opportunity",
        "delayed_probe": "delayed_probe_opportunity",
    }
    for pair in LINE_SCENARIO_PAIRS:
        dimension = str(pair["dimension"])
        flag = str(pair["flag"])
        positive = by_id.get(str(pair["positive"])) or {}
        negative = by_id.get(str(pair["negative"])) or {}
        mixed = by_id.get(str(pair["mixed_identity"])) or {}
        positive_value = bool(
            ((positive.get("context") or {}).get("line") or {}).get(flag)
        )
        negative_value = bool(
            ((negative.get("context") or {}).get("line") or {}).get(flag)
        )
        mixed_value = bool(
            ((mixed.get("context") or {}).get("line") or {}).get(flag)
        )
        producer_reachable = positive_value and not negative_value and mixed_value
        if not producer_reachable:
            system_issues.append(
                f"{dimension}_line_not_reachable_from_official_transcripts:"
                f"positive={positive_value}:negative={negative_value}:"
                f"mixed={mixed_value}"
            )
        positive_scenario = scenarios[str(pair["positive"])]
        mixed_scenario = scenarios[str(pair["mixed_identity"])]
        mixing_class = positive_scenario.get("mixing_class")
        if (
            mixing_class != "structural_air_no_hole_draw"
            or mixed_scenario.get("mixing_class") != mixing_class
        ):
            system_issues.append(f"{dimension}:mixing_class_not_pinned")
        opportunity_tag = opportunity_tags[dimension]
        ablation_scenario = copy.deepcopy(scenarios[str(pair["positive"])])
        ablation_scenario["id"] = (
            f"{ablation_scenario['id']}:matched_{flag}_disabled"
        )
        ablation_scenario.setdefault("expected", {})[flag] = False

        def disable_one_line_predicate(context):
            candidate = copy.deepcopy(context)
            line = candidate.setdefault("line", {})
            line[flag] = False
            tags = line.get("line_tags") or []
            if isinstance(tags, (list, tuple)):
                line["line_tags"] = [
                    item for item in tags if item != opportunity_tag
                ]
            return candidate

        matched = _drive_scenario(
            imports,
            ablation_scenario,
            expected_runtime_version=expected_runtime_version,
            context_transform=disable_one_line_predicate,
        )
        system_issues.extend(
            f"{dimension}:matched_control:{item}"
            for item in matched.get("system_issues") or []
        )
        candidate_issues.extend(
            f"{dimension}:matched_control:{item}"
            for item in matched.get("candidate_issues") or []
        )

        def without_ablation_fields(context):
            candidate = copy.deepcopy(context or {})
            line = candidate.get("line") or {}
            line.pop(flag, None)
            tags = line.get("line_tags") or []
            if isinstance(tags, (list, tuple)):
                line["line_tags"] = [
                    item for item in tags if item != opportunity_tag
                ]
            return candidate

        positive_without_ablation = without_ablation_fields(
            positive.get("context")
        )
        matched_without_ablation = without_ablation_fields(
            matched.get("context")
        )
        positive_without_ablation_digest = _canonical_digest(
            positive_without_ablation
        )
        matched_without_ablation_digest = _canonical_digest(
            matched_without_ablation
        )
        context_ablation_exact = (
            positive_without_ablation == matched_without_ablation
            and positive_without_ablation_digest
            == matched_without_ablation_digest
        )
        if not context_ablation_exact:
            system_issues.append(f"{dimension}:matched_line_ablation_not_exact")

        def without_cards(context):
            candidate = copy.deepcopy(context or {})
            candidate.pop("cards", None)
            return candidate

        positive_without_cards_digest = _canonical_digest(
            without_cards(positive.get("context"))
        )
        mixed_without_cards_digest = _canonical_digest(
            without_cards(mixed.get("context"))
        )
        mixing_context_exact = bool(
            without_cards(positive.get("context"))
            == without_cards(mixed.get("context"))
            and positive_without_cards_digest == mixed_without_cards_digest
        )
        if not mixing_context_exact:
            system_issues.append(f"{dimension}:mixing_identity_context_not_exact")
        positive_wire = positive.get("wire")
        mixed_wire = mixed.get("wire")
        bounded_mixing = bool(
            positive_wire
            and mixed_wire
            and str(positive_wire).startswith("raise ")
            and mixed_wire == "check"
        )
        socket_validated = bool(
            positive_wire
            and negative.get("wire")
            and mixed_wire
            and matched.get("wire")
        )
        consumer_wire_effect = bool(
            positive_wire
            and matched.get("wire")
            and positive_wire != matched.get("wire")
        )
        causal_passed = bool(
            producer_reachable
            and context_ablation_exact
            and mixing_context_exact
            and bounded_mixing
            and consumer_wire_effect
            and socket_validated
        )
        dimensions[dimension] = {
            "ok": producer_reachable,
            "flag": flag,
            "positive_scenario": pair["positive"],
            "negative_scenario": pair["negative"],
            "mixed_identity_scenario": pair["mixed_identity"],
            "mixing_class": mixing_class,
            "positive": positive_value,
            "negative": negative_value,
            "mixed_identity": mixed_value,
            "producer_reachable": producer_reachable,
            "positive_decision": positive.get("decision"),
            "negative_decision": negative.get("decision"),
            "positive_wire": positive_wire,
            "negative_wire": negative.get("wire"),
            "mixed_identity_decision": mixed.get("decision"),
            "mixed_identity_wire": mixed_wire,
            "mixed_identity_context_digest": mixed.get("context_digest"),
            "positive_without_cards_digest": positive_without_cards_digest,
            "mixed_without_cards_digest": mixed_without_cards_digest,
            "mixing_context_exact": mixing_context_exact,
            "bounded_mixing": bounded_mixing,
            "stable_context_normalization_paths": list(
                STABLE_CONTEXT_NORMALIZATION_PATHS
            ),
            "mixing_comparison_ignored_paths": ["cards"],
            "matched_control_decision": matched.get("decision"),
            "matched_control_wire": matched.get("wire"),
            "matched_control_context_digest": matched.get("context_digest"),
            "positive_without_ablation_digest": (
                positive_without_ablation_digest
            ),
            "matched_without_ablation_digest": matched_without_ablation_digest,
            "ablation_paths": [
                f"line.{flag}",
                f"line.line_tags:{opportunity_tag}",
            ],
            "context_ablation_exact": context_ablation_exact,
            "consumer_wire_effect": consumer_wire_effect,
            "policy_changed": consumer_wire_effect,
            "causal_passed": causal_passed,
            "socket_validated": socket_validated,
        }
    issues = [*system_issues, *candidate_issues]
    return {
        "ok": not issues,
        "issues": issues,
        "system_issues": system_issues,
        "candidate_issues": candidate_issues,
        "dimensions": dimensions,
    }


def _probe_persistent_memory(
    imports: CandidateImports,
    *,
    expected_runtime_version: int,
) -> dict[str, Any]:
    _native, bot = _new_native_bot(imports)
    sock = MemorySocket()
    scripted = iter((
        {"kind": "raise", "raise_to": 300},
        {"kind": "raise", "raise_to": 300},
    ))

    def typed_setup_decision():
        decision = copy.deepcopy(next(scripted))
        bot._last_decision_source = "typed_probe_setup"
        bot._last_decision_metrics = {
            "runtime_version": expected_runtime_version,
            "probe_setup": True,
        }
        return decision

    bot._policy_decision = typed_setup_decision
    try:
        messages = (
            "preflop|SMALLBLIND|<0,12><1,12>",
            "fold",
            "earnChips 150",
            "preflop|SMALLBLIND|<0,11><1,11>",
            "flop|<2,8><3,5><0,2>",
            "turn|<1,4>",
            "river|<2,7>",
            "earnChips 0",
            "oppo_hands|<3,12><3,11>",
            "preflop|BIGBLIND|<0,10><1,9>",
        )
        for message in messages:
            if "\r" in message or "\n" in message:
                raise ValueError("memory_probe_message_contains_delimiter")
            bot.handle(message, sock)
        now = time.monotonic()
        context = bot._build_decision_context(
            decision_id=1,
            hard_deadline=now + 1.0,
            refinement_deadline=now + 0.8,
        )
        snapshot = context.get("opponent") or {}
        terminal = snapshot.get("terminal_response") or {}
        showdown = snapshot.get("showdown_range") or {}
        issues = []
        if int(snapshot.get("hands_completed") or 0) != 2:
            issues.append("typed_memory_completed_hand_count_mismatch")
        if int(terminal.get("samples") or 0) < 2:
            issues.append("typed_memory_terminal_response_missing")
        facing_raise = terminal.get("facing_raise") or {}
        if int(facing_raise.get("fold") or 0) < 1:
            issues.append("typed_memory_terminal_fold_missing")
        if int(facing_raise.get("call") or 0) < 1:
            issues.append("typed_memory_boundary_inferred_call_missing")
        if int(showdown.get("samples") or 0) < 1:
            issues.append("typed_memory_showdown_posterior_missing")
        return {
            "ok": not issues,
            "issues": issues,
            "hands_completed": int(snapshot.get("hands_completed") or 0),
            "terminal_response": {
                "samples": int(terminal.get("samples") or 0),
                "fold": int(facing_raise.get("fold") or 0),
                "call": int(facing_raise.get("call") or 0),
                "confidence": terminal.get("confidence"),
            },
            "showdown_range": {
                "samples": int(showdown.get("samples") or 0),
                "selection_scope": showdown.get("selection_scope"),
                "selection_bias_guard": showdown.get("selection_bias_guard"),
            },
            "context_digest": _canonical_digest(_stable_context(context)),
        }
    finally:
        bot.close()


def _profile_context(
    context: dict[str, Any],
    profile: str,
    *,
    influence_enabled: bool = True,
) -> dict[str, Any]:
    candidate = copy.deepcopy(context)
    opponent = candidate.setdefault("opponent", {})
    opponent["confidence"] = 0.8
    opponent["adaptation_weight"] = 0.52
    rates = opponent.setdefault("rates", {})
    terminal = opponent.setdefault("terminal_response", {})
    showdown = opponent.setdefault("showdown_range", {})
    if profile == "aggressive":
        rates.update({"aggression": 0.85, "preflop_vpip": 0.82})
    elif profile == "passive":
        rates.update({"aggression": 0.12, "preflop_vpip": 0.38})
    elif profile == "terminal_folder":
        terminal.update({
            "samples": 24,
            "confidence": 0.75,
            "adaptation_weight": 0.4875,
            "fold_to_raise": 0.82,
            "fold_to_jam": 0.79,
            "river_overcall": 0.18,
        })
    elif profile == "terminal_caller":
        terminal.update({
            "samples": 24,
            "confidence": 0.75,
            "adaptation_weight": 0.4875,
            "fold_to_raise": 0.18,
            "fold_to_jam": 0.16,
            "river_overcall": 0.82,
        })
    elif profile == "tight_showdown":
        showdown.update({
            "samples": 20,
            "confidence": 0.714286,
            "adaptation_weight": 0.35,
            "showdown_reach_rate": 0.75,
            "selection_scope": "reached_showdown_only",
            "selection_bias_guard": "reach_rate_discount_and_capped_influence",
            "tightness": 0.72,
        })
    elif profile == "loose_showdown":
        showdown.update({
            "samples": 20,
            "confidence": 0.714286,
            "adaptation_weight": 0.35,
            "showdown_reach_rate": 0.75,
            "selection_scope": "reached_showdown_only",
            "selection_bias_guard": "reach_rate_discount_and_capped_influence",
            "tightness": 0.18,
        })
    if not influence_enabled:
        # Matched negative control: keep the observed signal values different
        # while removing their system-provided confidence/weight authority.
        # A policy that reads raw sparse counters without the bounded trust
        # contract will still change its wire action and therefore cannot claim
        # causal opponent adaptation.
        opponent["confidence"] = 0.0
        opponent["adaptation_weight"] = 0.0
        terminal["confidence"] = 0.0
        terminal["adaptation_weight"] = 0.0
        if profile in {"tight_showdown", "loose_showdown"}:
            # Preserve a nonzero observed posterior while removing the exact
            # system selection-bias authority.  Stable wire then proves the
            # guard itself, not merely a zero-weight compatibility path.
            showdown["selection_scope"] = "unconditional"
            showdown["selection_bias_guard"] = "missing"
        else:
            showdown["confidence"] = 0.0
            showdown["adaptation_weight"] = 0.0
    return candidate


def _direct_baseline(
    policy: Any,
    context: dict[str, Any],
    *,
    timeout_sec: float = 0.5,
) -> Any:
    def alarm(_signum, _frame):
        raise TimeoutError("typed_policy_baseline_timeout")

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        return policy.get_baseline_decision(copy.deepcopy(context))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def _validate_refinement_item(
    raw: Any,
    context: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    decision = raw
    if type(raw) is dict and "decision" in raw:
        allowed = {
            "decision",
            "sample_count",
            "confidence",
            "reason",
            "complete",
        }
        if not set(raw).issubset(allowed):
            return None, "typed_refinement_envelope_shape_invalid"
        for key in allowed - {"decision"}:
            if key in raw and type(raw[key]) not in (
                str,
                int,
                float,
                bool,
                type(None),
            ):
                return None, f"typed_refinement_metadata_invalid:{key}"
        decision = raw.get("decision")
    return _validate_typed_intent(decision, context)


def _direct_refinements(
    policy: Any,
    context: dict[str, Any],
    baseline: dict[str, Any],
    *,
    timeout_sec: float = 0.5,
    max_items: int = 64,
) -> tuple[list[dict[str, Any]], list[str]]:
    def alarm(_signum, _frame):
        raise TimeoutError("typed_policy_refinement_timeout")

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    decisions: list[dict[str, Any]] = []
    issues: list[str] = []
    try:
        deadline = time.monotonic() + min(0.25, timeout_sec * 0.75)
        iterator = iter(
            policy.iter_decisions(
                copy.deepcopy(context),
                copy.deepcopy(baseline),
                deadline,
            )
        )
        for _index in range(max_items):
            if time.monotonic() >= deadline:
                break
            try:
                raw = next(iterator)
            except StopIteration:
                break
            decision, issue = _validate_refinement_item(raw, context)
            if issue:
                issues.append(issue)
            elif decision is not None:
                decisions.append(decision)
    except BaseException as exc:
        issues.append(f"{type(exc).__name__}:{str(exc)[:160]}")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)
    return decisions, issues


def _probe_policy_entrypoints(
    imports: CandidateImports,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    policy = imports.load("policy")
    observations = []
    issues: list[str] = []
    for row in rows:
        scenario_id = str(row["id"])
        context = row["context"]
        try:
            raw = _direct_baseline(policy, context)
            decision, issue = _validate_typed_intent(raw, context)
        except BaseException as exc:
            decision = None
            issue = f"{type(exc).__name__}:{str(exc)[:160]}"
        if issue:
            issues.append(f"{scenario_id}:candidate_policy_baseline:{issue}")
        refinement_decisions: list[dict[str, Any]] = []
        refinement_issues: list[str] = []
        if decision is not None:
            refinement_decisions, refinement_issues = _direct_refinements(
                policy,
                context,
                decision,
            )
            issues.extend(
                f"{scenario_id}:candidate_policy_refinement:{item}"
                for item in refinement_issues
            )
        observations.append({
            "scenario": scenario_id,
            "decision": decision,
            "refinement_decisions": refinement_decisions,
            "ok": issue is None and not refinement_issues,
            "issue": issue or (refinement_issues[0] if refinement_issues else None),
        })
    return {"ok": not issues, "issues": issues, "rows": observations}


def _probe_match_control_consumer(
    imports: CandidateImports,
    *,
    expected_runtime_version: int,
) -> dict[str, Any]:
    """Require the candidate to consume the exact system lock-win proof."""

    base = next(
        copy.deepcopy(item)
        for item in DECISION_SCENARIOS
        if item["id"] == "preflop_sb_premium"
    )
    controls = (
        ("strict_win", 51, False, "fold"),
        ("equality_boundary", 50, False, "not_fold"),
        ("malformed_proof", 51, True, "not_fold"),
    )
    rows: dict[str, dict[str, Any]] = {}
    system_issues: list[str] = []
    candidate_issues: list[str] = []
    for control_id, lead, malformed, expectation in controls:
        scenario = copy.deepcopy(base)
        scenario["id"] = f"match_control_{control_id}"

        def transform(context, *, selected_lead=lead, is_malformed=malformed):
            candidate = copy.deepcopy(context)
            hand = candidate["hand"]
            betting = candidate["betting"]
            opponent = candidate["opponent"]
            hand["number"] = 70
            hand["total_hands"] = 70
            hand["remaining_including_current"] = 1
            exposure = 20_000 - int(betting["hero_stack"])
            opponent["match_result"]["hero_net_earned"] = selected_lead
            control = {
                "schema_version": 1,
                "initial_chips": 20_000,
                "small_blind": 50,
                "big_blind": 100,
                "current_position": hand["position"],
                "current_exposure": exposure,
                "future_forced_blinds": 0,
                "forced_fold_loss_bound": exposure,
                "hero_net_earned": selected_lead,
                "fold_locks_win": selected_lead > exposure,
            }
            if is_malformed:
                control.pop("future_forced_blinds")
            hand["match_control"] = control
            return candidate

        row = _drive_scenario(
            imports,
            scenario,
            expected_runtime_version=expected_runtime_version,
            context_transform=transform,
        )
        expected_system = (
            {f"{scenario['id']}:match_control_shape_mismatch"}
            if malformed
            else set()
        )
        observed_system = set(row.get("system_issues") or [])
        for issue in sorted(observed_system - expected_system):
            system_issues.append(f"{control_id}:{issue}")
        if observed_system != expected_system:
            missing = sorted(expected_system - observed_system)
            if missing:
                system_issues.append(
                    f"{control_id}:expected_system_issue_missing:{missing}"
                )
        candidate_issues.extend(
            f"{control_id}:{item}"
            for item in row.get("candidate_issues") or []
        )
        wire = row.get("wire")
        expectation_met = (
            wire == "fold" if expectation == "fold" else wire != "fold"
        )
        if not wire or not expectation_met:
            candidate_issues.append(
                f"match_control_consumer_{control_id}_wire_mismatch:"
                f"expected={expectation}:actual={wire!r}"
            )
        rows[control_id] = {
            "expectation": expectation,
            "decision": row.get("decision"),
            "wire": wire,
            "context_digest": row.get("context_digest"),
            "expected_system_issues": sorted(expected_system),
            "observed_system_issues": sorted(observed_system),
            "expectation_met": bool(wire and expectation_met),
        }
    socket_validated = all(row.get("wire") for row in rows.values())
    causal_passed = bool(
        socket_validated
        and rows["strict_win"]["wire"] == "fold"
        and rows["equality_boundary"]["wire"] != "fold"
        and rows["malformed_proof"]["wire"] != "fold"
    )
    return {
        "ok": not system_issues and not candidate_issues and causal_passed,
        "system_issues": system_issues,
        "candidate_issues": candidate_issues,
        "rows": rows,
        "socket_validated": socket_validated,
        "causal_passed": causal_passed,
        "strict_comparison": "hero_net_earned > forced_fold_loss_bound",
    }


def _probe_policy_counterfactuals(
    imports: CandidateImports,
    *,
    expected_runtime_version: int,
) -> dict[str, Any]:
    scenario_by_id = {str(item["id"]): item for item in DECISION_SCENARIOS}
    probes = (
        # This raw-wire spot exposes a legal, socket-validated raise-to on both
        # sides of every counterfactual.  Comparing continuous sizing avoids a
        # brittle threshold-only proof and lets each bounded opponent signal
        # demonstrate actual policy influence at the final wire boundary.
        ("action_profile", "flop_donk_vs_opponent_pfr", "aggressive", "passive"),
        ("terminal_response", "flop_donk_vs_opponent_pfr", "terminal_folder", "terminal_caller"),
        ("showdown_range", "flop_donk_vs_opponent_pfr", "tight_showdown", "loose_showdown"),
    )
    dimensions: dict[str, Any] = {}
    system_issues: list[str] = []
    candidate_issues: list[str] = []
    for dimension, scenario_id, left_name, right_name in probes:
        scenario = scenario_by_id[scenario_id]
        observations: dict[str, dict[str, Any]] = {}
        for control_kind, influence_enabled in (
            ("positive", True),
            ("negative", False),
        ):
            for profile in (left_name, right_name):
                observation_key = f"{control_kind}:{profile}"
                try:
                    row = _drive_scenario(
                        imports,
                        scenario,
                        expected_runtime_version=expected_runtime_version,
                        context_transform=(
                            lambda context, selected=profile, enabled=influence_enabled: (
                                _profile_context(
                                    context,
                                    selected,
                                    influence_enabled=enabled,
                                )
                            )
                        ),
                    )
                except BaseException as exc:
                    row = {
                        "decision": None,
                        "wire": None,
                        "runtime": {},
                        "system_issues": [],
                        "candidate_issues": [
                            f"counterfactual_exception:{type(exc).__name__}:"
                            f"{str(exc)[:160]}"
                        ],
                    }
                system_issues.extend(
                    f"{dimension}:{observation_key}:{item}"
                    for item in row.get("system_issues") or []
                )
                candidate_issues.extend(
                    f"{dimension}:{observation_key}:{item}"
                    for item in row.get("candidate_issues") or []
                )
                observations[observation_key] = {
                    "decision": row.get("decision"),
                    "wire": row.get("wire"),
                    "runtime": row.get("runtime") or {},
                }
        left = observations[f"positive:{left_name}"]
        right = observations[f"positive:{right_name}"]
        negative_left = observations[f"negative:{left_name}"]
        negative_right = observations[f"negative:{right_name}"]
        positive_changed = (
            left["decision"] != right["decision"]
            or left["wire"] != right["wire"]
        )
        positive_wire_effect = bool(
            left["wire"]
            and right["wire"]
            and left["wire"] != right["wire"]
        )
        negative_wire_stable = bool(
            negative_left["wire"]
            and negative_right["wire"]
            and negative_left["wire"] == negative_right["wire"]
        )
        socket_validated = all(
            item.get("wire") for item in observations.values()
        )
        dimensions[dimension] = {
            "scenario": scenario_id,
            "left_profile": left_name,
            "right_profile": right_name,
            "left_decision": left["decision"],
            "right_decision": right["decision"],
            "left_wire": left["wire"],
            "right_wire": right["wire"],
            "negative_left_decision": negative_left["decision"],
            "negative_right_decision": negative_right["decision"],
            "negative_left_wire": negative_left["wire"],
            "negative_right_wire": negative_right["wire"],
            "changed": positive_changed,
            "positive_wire_effect": positive_wire_effect,
            "negative_control_stable": negative_wire_stable,
            "negative_control_kind": (
                "selection_bias_guard_removed"
                if dimension == "showdown_range"
                else "authority_weight_removed"
            ),
            "causal_passed": bool(
                positive_wire_effect
                and negative_wire_stable
                and socket_validated
            ),
            "socket_validated": bool(socket_validated),
        }
    issues = [*system_issues, *candidate_issues]
    return {
        "ok": not issues,
        "issues": issues,
        "system_issues": system_issues,
        "candidate_issues": candidate_issues,
        "dimensions": dimensions,
    }


def _probe_budget_scaled_refinement(
    imports: CandidateImports,
    *,
    expected_runtime_version: int,
) -> dict[str, Any]:
    """Measure candidate anytime work through the trusted decision runtime.

    Both strata use the same official transcript, candidate artifact and worker
    seed.  Candidate-reported sample metadata is deliberately ignored; only
    runtime-counted iterator steps/CPU/elapsed values and sanitized decisions
    can satisfy this advisory capability.
    """

    scenario = next(
        item
        for item in DECISION_SCENARIOS
        if item["id"] == "river_facing_large_bet"
    )
    fixed_worker_seed = 20260710
    strata = {
        "short": {
            "hard_deadline_sec": 2.0,
            "baseline_target_sec": 0.20,
            "refinement_budget_sec": 1.8,
        },
        "long": {
            "hard_deadline_sec": 8.0,
            "baseline_target_sec": 0.20,
            "refinement_budget_sec": 7.5,
        },
    }
    observations: dict[str, Any] = {}
    system_issues: list[str] = []
    for label, budget in strata.items():
        row = _drive_scenario(
            imports,
            scenario,
            expected_runtime_version=expected_runtime_version,
            runtime_limits={
                "_strategy_base_seed": fixed_worker_seed,
                "_decision_hard_deadline_sec": budget["hard_deadline_sec"],
                "_decision_baseline_target_sec": budget["baseline_target_sec"],
                "_decision_refinement_budget_sec": budget[
                    "refinement_budget_sec"
                ],
            },
        )
        system_issues.extend(
            f"budget_{label}:{issue}" for issue in row.get("system_issues") or []
        )
        runtime = row.get("runtime") or {}
        observations[label] = {
            "trusted_steps": int(runtime.get("trusted_refinement_steps") or 0),
            "trusted_cpu_ms": float(
                runtime.get("trusted_refinement_cpu_ms") or 0.0
            ),
            "trusted_elapsed_ms": float(
                runtime.get("trusted_refinement_elapsed_ms") or 0.0
            ),
            "iterator_exhausted": bool(
                runtime.get("refinement_iterator_exhausted")
            ),
            "termination_reason": str(
                runtime.get("refinement_termination_reason") or ""
            ),
            "action_changes": int(
                runtime.get("refinement_decision_changes") or 0
            ),
            "refinement_messages": int(
                runtime.get("refinement_messages") or 0
            ),
            "baseline_published": bool(runtime.get("baseline_published")),
            "baseline_target_met": bool(runtime.get("baseline_target_met")),
            "worker_seed": runtime.get("worker_seed"),
            "decision": row.get("decision"),
            "wire": row.get("wire"),
        }

    short = observations["short"]
    long = observations["long"]
    same_seed = (
        short["worker_seed"] is not None
        and short["worker_seed"] == long["worker_seed"]
    )
    bounded_work = (
        long["trusted_steps"] >= 8
        and long["trusted_cpu_ms"] >= 5.0
        and long["refinement_messages"] >= 1
    )
    scaled_or_exhausted = (
        long["trusted_steps"] > short["trusted_steps"]
        or (
            short["iterator_exhausted"]
            and long["iterator_exhausted"]
            and short["trusted_steps"] == long["trusted_steps"]
            and long["trusted_steps"] >= 8
        )
    )
    changes_action = max(short["action_changes"], long["action_changes"]) >= 1
    capability_issues: list[str] = []
    if not same_seed:
        capability_issues.append("multifidelity_worker_seed_mismatch")
    if not bounded_work:
        capability_issues.append("long_budget_has_no_trusted_bounded_work")
    if not scaled_or_exhausted:
        capability_issues.append(
            "long_budget_does_not_scale_or_exhaust_finite_batch"
        )
    if not changes_action:
        capability_issues.append("refinement_never_changes_sanitized_decision")
    return {
        "probe_kind": "trusted_multifidelity_2s_vs_8s",
        "scenario": scenario["id"],
        "short_budget": strata["short"],
        "long_budget": strata["long"],
        "short": short,
        "long": long,
        "worker_seed_equal": same_seed,
        "bounded_work": bounded_work,
        "scaled_or_exhausted": scaled_or_exhausted,
        "changes_sanitized_decision": changes_action,
        "candidate_reported_metadata_is_non_authoritative": True,
        "ok": not system_issues and not capability_issues,
        "active": bool(long["refinement_messages"] or long["trusted_steps"]),
        "system_issues": system_issues,
        "capability_issues": capability_issues,
    }


def run(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    sys.dont_write_bytecode = True
    random.seed(20260710)
    os.environ["POK_OFFICIAL_ACTION_DELAY"] = "0"
    # Ordinary transcript/causal probes need the strict <250 ms baseline plus
    # enough refinement to expose a typed wire effect; the dedicated 2s/8s
    # multifidelity phase below owns long-work evidence.  Keeping these paths
    # short prevents duplicated profiles from consuming the probe watchdog.
    os.environ["POK_DECISION_HARD_DEADLINE_SEC"] = "0.45"
    os.environ["POK_DECISION_REFINEMENT_BUDGET_SEC"] = "0.30"
    os.environ["POK_DECISION_BASELINE_TARGET_SEC"] = "0.20"
    os.environ["POK_NATIVE_BOT_SEED"] = "20260710"
    sys.path.insert(0, str(root))
    imports = CandidateImports(root)
    expected_runtime_version = int(spec.get("expected_decision_runtime_version") or 0)

    _phase("official_transcripts")
    rows = [
        _drive_scenario(
            imports,
            scenario,
            expected_runtime_version=expected_runtime_version,
        )
        for scenario in DECISION_SCENARIOS
    ]
    _phase("line_reachability")
    line_reachability = _line_reachability(
        rows,
        imports,
        expected_runtime_version=expected_runtime_version,
    )
    _phase("persistent_memory")
    persistent_memory = _probe_persistent_memory(
        imports,
        expected_runtime_version=expected_runtime_version,
    )
    _phase("policy_entrypoints")
    try:
        policy_entrypoints = _probe_policy_entrypoints(imports, rows)
    except BaseException as exc:
        policy_entrypoints = {
            "ok": False,
            "issues": [
                "candidate_policy_import_or_entrypoint:"
                f"{type(exc).__name__}:{str(exc)[:180]}"
            ],
            "rows": [],
        }
    _phase("policy_counterfactuals")
    if policy_entrypoints.get("rows"):
        counterfactuals = _probe_policy_counterfactuals(
            imports,
            expected_runtime_version=expected_runtime_version,
        )
    else:
        counterfactuals = {
            "ok": False,
            "issues": ["candidate_policy_counterfactuals_not_run"],
            "dimensions": {},
        }
    _phase("match_control_consumer")
    try:
        match_control_consumer = _probe_match_control_consumer(
            imports,
            expected_runtime_version=expected_runtime_version,
        )
    except BaseException as exc:
        match_control_consumer = {
            "ok": False,
            "system_issues": [],
            "candidate_issues": [
                "match_control_consumer_exception:"
                f"{type(exc).__name__}:{str(exc)[:180]}"
            ],
            "rows": {},
        }
    _phase("budget_scaled_refinement")
    budget_scaled_refinement = _probe_budget_scaled_refinement(
        imports,
        expected_runtime_version=expected_runtime_version,
    )
    _phase("report")

    system_issues = [
        *(issue for row in rows for issue in row.get("system_issues") or []),
        *(line_reachability.get("system_issues") or []),
        *(persistent_memory.get("issues") or []),
        *(budget_scaled_refinement.get("system_issues") or []),
        *(counterfactuals.get("system_issues") or []),
        *(match_control_consumer.get("system_issues") or []),
    ]
    candidate_issues = [
        *(issue for row in rows for issue in row.get("candidate_issues") or []),
        *(line_reachability.get("candidate_issues") or []),
        *(policy_entrypoints.get("issues") or []),
        *(counterfactuals.get("candidate_issues") or []),
        *(match_control_consumer.get("candidate_issues") or []),
    ]
    for module_name, diagnostic in imports.diagnostics.items():
        if diagnostic.get("stdout"):
            candidate_issues.append(f"candidate_import_stdout:{module_name}")
        if diagnostic.get("stderr"):
            candidate_issues.append(f"candidate_import_stderr:{module_name}")
        if diagnostic.get("stdout_truncated") or diagnostic.get("stderr_truncated"):
            candidate_issues.append(
                f"candidate_import_output_truncated:{module_name}"
            )
        if float(diagnostic.get("elapsed_ms") or 0.0) > float(
            spec.get("max_import_ms") or 2_500.0
        ):
            candidate_issues.append(f"candidate_import_deadline_missed:{module_name}")
    issues = [*system_issues, *candidate_issues]
    return {
        "schema_version": int(spec.get("schema_version") or 0),
        "orchestrator_version": int(spec.get("orchestrator_version") or 0),
        "worker_version": PROBE_WORKER_VERSION,
        "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": spec.get("limits_digest"),
        "worker_digest": spec.get("worker_digest"),
        "probe_identity_digest": spec.get("probe_identity_digest"),
        "policy_abi": spec.get("policy_abi"),
        "spec_digest": spec.get("spec_digest"),
        "code_fingerprint": spec.get("code_fingerprint"),
        "ok": not issues,
        "failure_class": (
            "probe_infra"
            if system_issues
            else "candidate_contract"
            if candidate_issues
            else "none"
        ),
        "issues": issues,
        "system_issues": system_issues,
        "candidate_issues": candidate_issues,
        "official_transcript_decisions": rows,
        "line_reachability": line_reachability,
        "persistent_memory": persistent_memory,
        "policy_entrypoints": policy_entrypoints,
        "policy_counterfactuals": counterfactuals,
        "match_control_consumer": match_control_consumer,
        "budget_scaled_refinement": budget_scaled_refinement,
        "module_diagnostics": imports.diagnostics,
    }


def main() -> int:
    _set_limits()
    root = Path(sys.argv[1]).resolve()
    report_path = Path(sys.argv[2])
    spec_argument = Path(sys.argv[3])
    spec = json.loads(
        spec_argument.read_text(encoding="utf-8")
        if spec_argument.is_file()
        else sys.argv[3]
    )
    try:
        report = run(root, spec)
    except BaseException as exc:
        report = {
            "schema_version": int(spec.get("schema_version") or 0),
            "orchestrator_version": int(
                spec.get("orchestrator_version") or 0
            ),
            "worker_version": PROBE_WORKER_VERSION,
            "scenario_version": RUNTIME_PROBE_SCENARIO_VERSION,
            "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
            "limits_digest": spec.get("limits_digest"),
            "worker_digest": spec.get("worker_digest"),
            "probe_identity_digest": spec.get("probe_identity_digest"),
            "policy_abi": spec.get("policy_abi"),
            "spec_digest": spec.get("spec_digest"),
            "code_fingerprint": spec.get("code_fingerprint"),
            "ok": False,
            "failure_class": "probe_infra",
            "issues": [
                f"typed_runtime_probe_exception:{type(exc).__name__}:{str(exc)[:400]}"
            ],
        }
    report_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
