from pathlib import Path

from national_capability_contract import (
    NATIONAL_CAPABILITY_DETECTOR_VERSION,
    evaluate_national_capabilities,
    national_runtime_feedback_summary,
)
from national_native import NATIVE_BOT_TEMPLATE
from national_runtime_probe_scenarios import LINE_SCENARIO_PAIRS


def _write_bot(root: Path, *, national_bot: str, opponent: str = "", strategy: str = "") -> Path:
    root.mkdir(parents=True)
    (root / "national_bot.py").write_text(national_bot, encoding="utf-8")
    (root / "opponent.py").write_text(opponent, encoding="utf-8")
    (root / "strategy.py").write_text(strategy, encoding="utf-8")
    (root / "main.py").write_text(
        "def sanitize_action(action, state, chips): return int(action)\n",
        encoding="utf-8",
    )
    (root / "state.py").write_text(
        "def reconstruct_state(req): return req\n"
        "def infer_remaining_hands_from_requests(requests): return 1\n",
        encoding="utf-8",
    )
    return root


def _migration_probe_payload(
    *,
    repeatable: bool = True,
    stable_dimensions: set[str] | None = None,
) -> dict:
    def tier(left_label, left_wire, right_label, right_wire):
        return {
            left_label: {"wire": left_wire},
            right_label: {"wire": right_wire},
            "changed": True,
        }

    line_rows = []
    for pair in LINE_SCENARIO_PAIRS:
        line_rows.append({
            "dimension": pair["dimension"],
            "scenario_id": pair["positive"],
            "control_kind": "same_scenario_flag_false",
            "flag": pair["flag"],
            "tiers": {
                "baseline": tier("positive", "raise 600", "negative", "check")
            },
        })
    issues = [] if repeatable else ["runtime_probe_non_repeatable"]
    payload = {
        "schema_version": 10,
        "ok": repeatable,
        "failure_class": "none" if repeatable else "candidate_contract",
        "issues": issues,
        "repeat_count": 2,
        "repeatability_ok": repeatable,
        "evidence_integrity_ok": repeatable,
        "artifacts": [],
        "tracker": {"ok": True, "issues": []},
        "hand_context": {"ok": True, "issues": []},
        "decision_runtime": {},
        "strategy_influence": {
            "ok": repeatable,
            "issues": issues,
            "rows": [],
            "dimensions": {
                "terminal_response": {
                    "ok": True,
                    "changed_pairs": 1,
                    "rows": [{
                        "scenario_id": "preflop_sb_premium",
                        "tiers": {
                            "baseline": tier(
                                "terminal_folder",
                                "raise 600",
                                "terminal_caller",
                                "check",
                            )
                        },
                    }],
                },
                "showdown_range": {
                    "ok": True,
                    "changed_pairs": 1,
                    "rows": [{
                        "scenario_id": "flop_top_pair_facing_bet",
                        "tiers": {
                            "baseline": tier(
                                "tight_showdown",
                                "fold",
                                "loose_showdown",
                                "call",
                            )
                        },
                    }],
                },
                "semantic_lines": {
                    "ok": True,
                    "changed_pairs": 2,
                    "rows": line_rows,
                },
            },
        },
    }
    all_dimensions = {
        "terminal_response",
        "showdown_range",
        "donk",
        "delayed_probe",
    }
    if stable_dimensions is None:
        stable_dimensions = all_dimensions if repeatable else set()

    evidence_by_dimension = {
        "terminal_response": [{
            "dimension": "terminal_response",
            "scenario_id": "preflop_sb_premium",
            "tier": "baseline",
            "left_label": "terminal_folder",
            "right_label": "terminal_caller",
            "left_wire": "raise 600",
            "right_wire": "check",
        }],
        "showdown_range": [{
            "dimension": "showdown_range",
            "scenario_id": "flop_top_pair_facing_bet",
            "tier": "baseline",
            "left_label": "tight_showdown",
            "right_label": "loose_showdown",
            "left_wire": "fold",
            "right_wire": "call",
        }],
        "donk": [{
            "dimension": "donk",
            "scenario_id": "flop_donk_vs_opponent_pfr",
            "tier": "baseline",
            "left_label": "positive",
            "right_label": "negative",
            "left_wire": "raise 600",
            "right_wire": "check",
            "control_kind": "same_scenario_flag_false",
            "flag": "can_donk",
        }],
        "delayed_probe": [{
            "dimension": "delayed_probe",
            "scenario_id": "turn_delayed_probe_vs_opponent_pfr",
            "tier": "baseline",
            "left_label": "positive",
            "right_label": "negative",
            "left_wire": "raise 600",
            "right_wire": "check",
            "control_kind": "same_scenario_flag_false",
            "flag": "can_delayed_probe",
        }],
    }
    repeatability_dimensions = {}
    for dimension in sorted(all_dimensions):
        stable = dimension in stable_dimensions
        repeatability_dimensions[dimension] = {
            "stable": stable,
            "authority_tier": "baseline",
            "evidence_present": True,
            "observations_identical": stable,
            "evidence": evidence_by_dimension[dimension] if stable else [],
            "observation_digests": (
                [f"{dimension}-same", f"{dimension}-same"]
                if stable
                else [f"{dimension}-first", f"{dimension}-second"]
            ),
        }
    payload["migration_evidence_repeatability"] = {
        "schema_version": 1,
        "candidate_fingerprint_unchanged": True,
        "run_count": 2,
        "runs_eligible": True,
        "dimensions": repeatability_dimensions,
    }
    return payload


LIVE_MIGRATION_STRATEGY = """
def _choose(req):
    profile = req.get('opponent_runtime', {})
    hand = req.get('hand_runtime', {})
    if hand.get('can_donk'):
        return 600
    if hand.get('can_delayed_probe'):
        return 500
    terminal_pressure = (
        profile.get('fold_to_raise', 0.0)
        + profile.get('fold_to_jam_rate', 0.0)
        - profile.get('river_overcall_freq', 0.0)
    )
    if terminal_pressure > 0.5:
        return -2
    shown = profile.get('showdown_range', {})
    if (
        shown.get('selection_scope') == 'reached_showdown_only'
        and shown.get('confidence', 0.0) > 0.1
        and shown.get('adaptation_weight', 0.0) > 0.0
        and shown.get('tightness', 0.0) > 0.3
    ):
        return -1
    return 0
def get_action(req, requests): return _choose(req)
def get_baseline_action(req, requests): return _choose(req)
"""


def test_capability_contract_accepts_safe_wire_and_flags_missing_architecture(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v1",
        national_bot="""
import argparse
import sys
import time
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def __init__(self):
        self._requests = []
        self._history = []
        self._showdowns = []
    def _send_wire_action(self, action):
        pass
    def handle(self, msg):
        if msg.startswith('earnChips') or msg.startswith('oppo_hands'):
            self._history.append(msg)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        opponent="""
def build_opponent_model(requests):
    return {'opp_aggression': len(requests)}
""",
        strategy="""
MAX_MONTE_CARLO_SAMPLES = 120
def get_action(req, requests):
    start = time.monotonic()
    return 0
""",
    )

    result = evaluate_national_capabilities(bot)
    warning_names = {item["name"] for item in result["advisory_warnings"]}
    required_names = {item["name"] for item in result["required_failures"]}

    assert result["ok"] is True
    assert result["required_failures"] == []
    assert "incremental_opponent_model" in warning_names

    feedback = national_runtime_feedback_summary(bot, source_label="national_v1")
    assert "National runtime architecture feedback for national_v1" in feedback
    assert "Architecture improvement opportunities" in feedback
    assert "incremental_opponent_model" in feedback
    assert "planning signal only" in feedback


def test_capability_contract_blocks_stdout_pollution_and_missing_throttle(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v2",
        national_bot="""
def main():
    print('debug')
""",
    )

    result = evaluate_national_capabilities(bot)
    failure_names = {item["name"] for item in result["required_failures"]}

    assert result["ok"] is False
    assert "official_safe_wire_send" in failure_names
    assert "clean_diagnostics_channel" in failure_names


def test_capability_contract_detects_precompute_and_incremental_model(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v3",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
import time
PREFLOP_LOOKUP_TABLE = {i: i / 31 for i in range(32)}
MAX_DECISION_SAMPLES = 64
def _choose(req):
    opp_profile = req.get('opponent_runtime', {})
    hand_runtime = req.get('hand_runtime', {})
    baseline = PREFLOP_LOOKUP_TABLE.get(12, 0.0)
    if hand_runtime.get('can_donk'):
        return 600
    if hand_runtime.get('can_delayed_probe'):
        return 500
    if opp_profile.get('fold_to_jam_samples', 0) >= 10:
        pressure = opp_profile.get('fold_to_raise', 0.0) + opp_profile.get('fold_to_jam_rate', 0.0) - opp_profile.get('river_overcall_freq', 0.0)
        return -2 if pressure > 0.5 else 0
    revealed = opp_profile.get('showdown_range', {})
    if (revealed.get('selection_scope') == 'reached_showdown_only'
            and revealed.get('confidence', 0.0) > 0.1
            and revealed.get('adaptation_weight', 0.0) > 0.0
            and revealed.get('samples', 0) >= 10
            and revealed.get('tightness', 0.0) > 0.30):
        return -1
    adaptation = opp_profile.get('adaptation_weight', 0.0)
    adjusted = baseline + adaptation * (0.25 if opp_profile.get('vpip', 0.0) > 0.5 else -0.25)
    deadline = time.monotonic() + 0.05
    for sample in range(64):
        if time.monotonic() >= deadline:
            break
        adjusted += (sample % 2) * 0.0001
    return 400 if adjusted > 0.4 else 0
def get_action(req, current_view):
    return _choose(req)
def get_baseline_action(req, current_view):
    return _choose(req)
def iter_refinements(req, current_view, baseline, deadline):
    refined = -1 if baseline == 0 and req.get('to_call', 0) > 0 else baseline
    for samples in range(1, 17):
        if time.monotonic() >= deadline:
            return
        work = 0
        for outer in range(100):
            for unit in range(100):
                for lane in range(4):
                    work += (outer * unit * samples + lane) % 17
        if work < 0:
            refined = baseline
        yield {'action': refined, 'sample_count': samples, 'confidence': samples / 16.0, 'complete': samples == 16}
""",
    )

    result = evaluate_national_capabilities(bot)
    checks = {item["name"]: item["passed"] for item in result["checks"]}

    assert result["ok"] is True
    assert checks["precompute_lookup_path"] is True
    # The fixture reads a fixed lookup key. It remains a valid acceleration
    # path, but it must not be promoted to a value-sensitive strategy primary.
    assert checks["precompute_runtime_influence"] is False
    assert checks["persistent_match_memory"] is True
    assert checks["incremental_opponent_model"] is True
    assert checks["terminal_response_adaptation"] is True
    assert checks["showdown_range_adaptation"] is True
    assert checks["donk_line_reachability"] is True
    assert checks["delayed_probe_line_reachability"] is True
    assert checks["semantic_line_reachability"] is True

    feedback = national_runtime_feedback_summary(bot, source_label="national_v3")
    assert "precompute_runtime_influence" in feedback
    assert "Already present" in feedback


def test_capability_contract_flags_decision_path_runtime_risks(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v4",
        national_bot="""
import argparse
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def __init__(self):
        self._requests = []
        self._history = []
        self._showdowns = []
    def _send_wire_action(self, action):
        pass
    def handle(self, msg):
        if msg.startswith('earnChips') or msg.startswith('oppo_hands'):
            self._history.append(msg)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        strategy="""
import time
MAX_DECISION_SAMPLES = 64
def get_action(req, requests):
    start = time.monotonic()
    debug = open('decision.log', 'a')
    total = 0
    for item in requests:
        total += 1
    lookup = {i: i for i in range(256)}
    return lookup.get(total, 0)
""",
    )

    result = evaluate_national_capabilities(bot)
    warning_names = {item["name"] for item in result["advisory_warnings"]}
    required_names = {item["name"] for item in result["required_failures"]}

    assert "decision_path_no_external_io" in required_names
    assert "decision_path_no_full_history_scan" in warning_names
    assert "decision_path_no_large_runtime_tables" in warning_names
    assert result["decision_path_risks"]["external_io"]
    assert result["decision_path_risks"]["history_scans"]
    assert result["decision_path_risks"]["large_runtime_tables"]


def test_capability_contract_does_not_treat_wire_action_helpers_as_decision_roots(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v5",
        national_bot="""
import argparse
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def __init__(self):
        self._requests = []
        self._history = []
        self._showdowns = []
    def _send_wire_action(self, action):
        pass
    def _action_to_tcp(self, action):
        return 'call'
    def handle(self, msg):
        if msg.startswith('earnChips') or msg.startswith('oppo_hands'):
            self._history.append(msg)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        strategy="""
import time
MAX_DECISION_SAMPLES = 64
def get_action(req, requests):
    start = time.monotonic()
    return 0
""",
    )

    result = evaluate_national_capabilities(bot)

    assert "national_bot.py:_action_to_tcp" not in result["decision_path_risks"]["decision_functions"]
    assert "strategy.py:get_action" in result["decision_path_risks"]["decision_functions"]


def test_import_time_external_policy_read_is_required_failure(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v_import_io",
        national_bot="""
POK_OFFICIAL_ACTION_DELAY = 0.30
def _send_wire_action(action):
    return action
""",
        strategy="""
from pathlib import Path
ACTION = int(Path('/tmp/action').read_text())
def get_action(req, requests):
    return ACTION
""",
    )

    result = evaluate_national_capabilities(bot)
    failures = {item["name"]: item for item in result["required_failures"]}

    assert "decision_path_no_external_io" in failures
    locations = failures["decision_path_no_external_io"]["evidence"]["locations"]
    assert any("strategy.py" in location and "read_text" in location for location in locations)


def test_capability_contract_traces_indirect_decision_path_risks(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v6",
        national_bot="""
import argparse
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def __init__(self):
        self._requests = []
        self._history = []
        self._showdowns = []
    def _send_wire_action(self, action):
        pass
    def handle(self, msg):
        if msg.startswith('earnChips') or msg.startswith('oppo_hands'):
            self._history.append(msg)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        opponent="""
def build_opponent_model(requests):
    profile = {'aggression': 0}
    for item in requests:
        profile['aggression'] += 1
    return profile
""",
        strategy="""
import time
from opponent import build_opponent_model
MAX_DECISION_SAMPLES = 64
def get_action(req, requests):
    start = time.monotonic()
    profile = build_opponent_model(requests)
    return 100 if profile['aggression'] > 10 else 0
""",
    )

    result = evaluate_national_capabilities(bot)
    warning_names = {item["name"] for item in result["advisory_warnings"]}
    scans = result["decision_path_risks"]["history_scans"]

    assert "decision_path_no_full_history_scan" in warning_names
    assert any(
        "strategy.py:get_action->opponent.py:build_opponent_model" in item
        for item in scans
    )

    feedback = national_runtime_feedback_summary(bot, source_label="national_v6")
    assert "Decision path evidence to route into worker tasks" in feedback
    assert "strategy.py:get_action->opponent.py:build_opponent_model" in feedback


def test_capability_contract_traces_indirect_large_runtime_table(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v7",
        national_bot="""
import argparse
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def __init__(self):
        self._requests = []
        self._history = []
        self._showdowns = []
    def _send_wire_action(self, action):
        pass
    def handle(self, msg):
        if msg.startswith('earnChips') or msg.startswith('oppo_hands'):
            self._history.append(msg)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        strategy="""
import time
MAX_DECISION_SAMPLES = 64
def build_lookup():
    return {i: i for i in range(256)}
def get_action(req, requests, runtime_ctx=None):
    start = time.monotonic()
    lookup = build_lookup()
    return lookup.get(1, 0)
""",
    )

    result = evaluate_national_capabilities(bot)
    warning_names = {item["name"] for item in result["advisory_warnings"]}
    tables = result["decision_path_risks"]["large_runtime_tables"]

    assert "decision_path_no_large_runtime_tables" in warning_names
    assert any("strategy.py:get_action->strategy.py:build_lookup" in item for item in tables)


def test_capability_contract_does_not_accept_keywords_or_empty_cache_as_precompute(tmp_path):
    result_fields = ", ".join(f"{i!r}: {i}" for i in range(34))
    bot = _write_bot(
        tmp_path / "national_v8",
        national_bot="""
import argparse
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def _send_wire_action(self, action):
        pass
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        opponent="""
def match_profile():
    return {'actions': 0}
""",
        strategy=f"""
import time
# A precompute lookup cache is desirable, but this comment is not an artifact.
BOARD_TEXTURE_CACHE = {{}}
MAX_DECISION_SAMPLES = 64
def get_action(req, requests):
    deadline = time.monotonic() + 0.1
    result = {{{result_fields}}}
    for action in req.get('history', []):
        result[0] += 1
    return result[0] if deadline else 0
""",
    )

    result = evaluate_national_capabilities(bot)
    checks = result["checks_by_id"]

    assert checks["precompute_lookup_path"]["passed"] is False
    assert checks["incremental_opponent_model"]["passed"] is False
    assert checks["decision_path_no_full_history_scan"]["passed"] is True
    assert checks["decision_path_no_large_runtime_tables"]["passed"] is True


def test_template_tracker_requires_a_strategy_consumer(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v9",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
import time
MAX_DECISION_SAMPLES = 64
def get_action(req, requests):
    deadline = time.monotonic() + 0.1
    return 0 if deadline else -1
""",
    )

    result = evaluate_national_capabilities(bot)
    checks = result["checks_by_id"]

    assert checks["persistent_match_memory"]["passed"] is True
    assert checks["incremental_opponent_model"]["passed"] is False
    assert result["incremental_model_evidence"]["provider_complete"] is True
    assert result["incremental_model_evidence"]["consumed_by_decision"] is False


def test_opaque_lru_cache_does_not_satisfy_measured_precompute_floor(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v10",
        national_bot="""
import argparse
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def _send_wire_action(self, action):
        pass
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        strategy="""
from functools import lru_cache
import time
@lru_cache(maxsize=4096)
def equity_lookup(key):
    return key / 100.0
for _key in range(20):
    equity_lookup(_key)
def get_action(req, requests):
    deadline = time.monotonic() + 0.1
    return 100 if equity_lookup(50) > 0.4 and deadline else 0
""",
    )

    result = evaluate_national_capabilities(bot)

    assert result["checks_by_id"]["precompute_lookup_path"]["passed"] is False
    dynamic_rows = result["precompute_evidence"]["dynamic_probe_artifacts"]
    assert dynamic_rows[0]["issues"] == ["artifact_not_inspectable_mapping"]
    assert result["precompute_evidence"]["consumed_artifacts"][0]["name"] == "equity_lookup"


def test_capability_contract_emits_stable_structured_evidence(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v11",
        national_bot="""
import argparse
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def _send_wire_action(self, action):
        pass
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        strategy="def get_action(req, requests): return 0\n",
    )

    result = evaluate_national_capabilities(bot)

    assert result["schema_version"] == 8
    assert result["detector_version"] == NATIONAL_CAPABILITY_DETECTOR_VERSION
    assert set(result["checks_by_id"]) == {item["check_id"] for item in result["checks"]}
    for check in result["checks"]:
        assert check["name"] == check["check_id"]
        assert 0.0 <= check["confidence"] <= 1.0
        assert set(check["evidence"]) == {"summary", "locations", "facts"}


def test_noop_tracker_methods_do_not_prove_persistent_match_memory(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v12",
        national_bot="""
import argparse
from collections import deque
POK_OFFICIAL_ACTION_DELAY = 0.30
OPPONENT_ADAPTATION_CAP = 0.65
class OpponentTracker:
    def __init__(self): self.recent = deque(maxlen=8)
    def begin_hand(self, hand): pass
    def observe_action(self, action): pass
    def observe_settlement(self, earned): pass
    def observe_showdown(self, cards): pass
    def snapshot(self):
        return {'confidence': 0.5, 'adaptation_weight': OPPONENT_ADAPTATION_CAP}
class NativeNationalBot:
    def __init__(self): self._opponent_tracker = OpponentTracker()
    def _send_wire_action(self, action): pass
    def request(self): return {'opponent_runtime': self._opponent_tracker.snapshot()}
    def handle(self, msg):
        self._opponent_tracker.begin_hand(1)
        self._opponent_tracker.observe_action(msg)
        self._opponent_tracker.observe_settlement(0)
        self._opponent_tracker.observe_showdown([])
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        strategy="""
def get_action(req, requests):
    profile = req.get('opponent_runtime', {})
    return 100 if profile.get('adaptation_weight', 0) else 0
""",
    )

    result = evaluate_national_capabilities(bot)

    assert result["checks_by_id"]["persistent_match_memory"]["passed"] is False
    assert result["incremental_model_evidence"]["provider"]["action_updates"] is False


def test_unused_opponent_runtime_read_does_not_prove_strategy_consumption(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def get_action(req, requests):
    profile = req.get('opponent_runtime', {})
    confidence = profile.get('confidence', 0.0)
    return 0
""",
    )

    result = evaluate_national_capabilities(bot)

    assert result["checks_by_id"]["persistent_match_memory"]["passed"] is True
    assert result["checks_by_id"]["incremental_opponent_model"]["passed"] is False


def test_truthy_only_adaptation_does_not_prove_bounded_strategy_adjustment(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13b",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def get_action(req, requests):
    profile = req.get('opponent_runtime', {})
    if profile.get('adaptation_weight', 0.0):
        return 100
    return 0
""",
    )

    result = evaluate_national_capabilities(bot)

    assert result["checks_by_id"]["persistent_match_memory"]["passed"] is True
    assert result["checks_by_id"]["incremental_opponent_model"]["passed"] is False
    assert result["incremental_model_evidence"]["consumer_locations"] == []


def test_numerically_noop_adaptation_fails_dynamic_action_influence(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13c",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def get_action(req, requests):
    profile = req.get('opponent_runtime', {})
    weight = profile.get('adaptation_weight', 0.0)
    adjusted = weight * 0.000001
    return int(adjusted)
""",
    )

    result = evaluate_national_capabilities(bot)

    assert result["incremental_model_evidence"]["consumer_locations"]
    assert result["checks_by_id"]["incremental_opponent_model"]["passed"] is False
    influence = result["dynamic_runtime_probe"]["strategy_influence"]
    assert influence["changed_pairs"] == 0
    assert "opponent_runtime_no_observable_sanitized_action_influence" in influence["issues"]


def test_action_style_influence_cannot_substitute_for_terminal_showdown_or_lines(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13d",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def get_action(req, requests):
    profile = req.get('opponent_runtime', {})
    weighted_vpip = profile.get('adaptation_weight', 0.0) * profile.get('vpip', 0.0)
    return -2 if weighted_vpip > 0.2 else 0
""",
    )

    result = evaluate_national_capabilities(bot)
    checks = result["checks_by_id"]

    assert checks["incremental_opponent_model"]["passed"] is True
    assert checks["terminal_response_adaptation"]["passed"] is False
    assert checks["showdown_range_adaptation"]["passed"] is False
    assert checks["donk_line_reachability"]["passed"] is False
    assert checks["delayed_probe_line_reachability"]["passed"] is False
    assert checks["semantic_line_reachability"]["passed"] is False
    dimensions = result["dynamic_runtime_probe"]["strategy_influence"]["dimensions"]
    assert dimensions["action_profile"]["ok"] is True
    assert dimensions["terminal_response"]["ok"] is False
    assert dimensions["showdown_range"]["ok"] is False
    assert dimensions["semantic_lines"]["ok"] is False


def test_dummy_refinement_yield_of_baseline_fails_budget_scaled_contract(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13e",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
import time
def get_action(req, requests): return 0
def get_baseline_action(req, requests): return 0
def iter_refinements(req, requests, baseline, deadline):
    if time.monotonic() < deadline:
        yield baseline
""",
    )

    result = evaluate_national_capabilities(bot)
    runtime = result["dynamic_runtime_probe"]["decision_runtime"]

    assert result["checks_by_id"]["incremental_refinement_protocol"]["passed"] is True
    assert result["checks_by_id"]["budget_scaled_refinement"]["passed"] is False
    assert result["checks_by_id"]["killable_decision_runtime"]["passed"] is True
    assert runtime["safety_ok"] is True
    assert runtime["safety_issues"] == []
    assert runtime["refinement_ok"] is False
    assert "long_budget_refinement_has_no_bounded_work" in runtime["refinement_issues"]
    assert "refinement_never_changes_sanitized_baseline_action" in runtime["refinement_issues"]


def test_candidate_reported_complete_cannot_fake_trusted_refinement_work(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13f",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
import time
def get_action(req, requests): return 0
def get_baseline_action(req, requests): return 0
def iter_refinements(req, requests, baseline, deadline):
    if time.monotonic() < deadline:
        yield {'action': -1, 'sample_count': 8, 'confidence': 1.0, 'complete': True}
""",
    )

    result = evaluate_national_capabilities(bot)
    runtime = result["dynamic_runtime_probe"]["decision_runtime"]

    assert result["checks_by_id"]["budget_scaled_refinement"]["passed"] is False
    assert runtime["budget_scaling"]["short"]["trusted_steps"] == 1
    assert runtime["budget_scaling"]["long"]["trusted_steps"] == 1
    assert "long_budget_refinement_has_no_bounded_work" in runtime["refinement_issues"]


def test_history_length_cannot_substitute_for_hand_runtime_flag_ablation(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13g",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def get_action(req, requests):
    hand_runtime = req.get('hand_runtime', {})
    _ = hand_runtime.get('can_donk')
    _ = hand_runtime.get('can_delayed_probe')
    if len(req.get('history', [])) == 2:
        return 600
    if len(req.get('history', [])) == 4:
        return 500
    return 0
def get_baseline_action(req, requests): return get_action(req, requests)
""",
    )

    result = evaluate_national_capabilities(bot)
    semantic = result["dynamic_runtime_probe"]["strategy_influence"]["dimensions"][
        "semantic_lines"
    ]

    assert semantic["ok"] is False
    assert semantic["changed_pairs"] == 0
    assert result["checks_by_id"]["donk_line_reachability"]["passed"] is False
    assert result["checks_by_id"]["delayed_probe_line_reachability"]["passed"] is False
    assert result["checks_by_id"]["semantic_line_reachability"]["passed"] is False


def test_dead_literals_and_runtime_repr_cannot_fake_migration_consumers(
    tmp_path,
    monkeypatch,
):
    import national_runtime_probe

    bot = _write_bot(
        tmp_path / "national_v13g_dead_migration",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def _choose(req):
    profile = req.get('opponent_runtime', {})
    hand = req.get('hand_runtime', {})
    _dead_labels = (
        'fold_to_raise', 'fold_to_jam_rate', 'river_overcall_freq',
        'terminal_response', 'contexts', 'showdown_range', 'selection_scope',
        'confidence', 'adaptation_weight', 'tightness', 'bucket_rates',
        'can_donk', 'can_delayed_probe',
    )
    return 600 if (len(repr(profile)) + len(repr(hand))) % 2 else 0
def get_action(req, requests): return _choose(req)
def get_baseline_action(req, requests): return _choose(req)
""",
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: _migration_probe_payload(repeatable=True),
    )

    result = evaluate_national_capabilities(bot)
    evidence = result["incremental_model_evidence"]
    checks = result["checks_by_id"]

    assert evidence["source_rooted_live_access_paths"] == {}
    assert evidence["decision_field_locations"]["can_donk"]
    assert evidence["decision_field_locations"]["showdown_range"]
    for check_id in (
        "terminal_response_adaptation",
        "showdown_range_adaptation",
        "donk_line_reachability",
        "delayed_probe_line_reachability",
    ):
        assert checks[check_id]["passed"] is False
        assert checks[check_id]["evidence"]["facts"]["source_rooted_paths"] == []


def test_nonrepeatable_first_run_cannot_prove_migration_consumers(
    tmp_path,
    monkeypatch,
):
    import national_runtime_probe

    bot = _write_bot(
        tmp_path / "national_v13g_nonrepeatable",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy=LIVE_MIGRATION_STRATEGY,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: _migration_probe_payload(repeatable=False),
    )

    result = evaluate_national_capabilities(bot)
    checks = result["checks_by_id"]

    assert result["dynamic_runtime_probe"]["repeatability_ok"] is False
    for check_id in (
        "terminal_response_adaptation",
        "showdown_range_adaptation",
        "donk_line_reachability",
        "delayed_probe_line_reachability",
    ):
        assert checks[check_id]["passed"] is False
        assert (
            checks[check_id]["evidence"]["facts"]["dynamic_evidence"][
                "integrity_ok"
            ]
            is False
        )


def test_global_probe_jitter_does_not_block_stable_migration_dimensions(
    tmp_path,
    monkeypatch,
):
    import national_runtime_probe

    bot = _write_bot(
        tmp_path / "national_v13g_global_jitter",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy=LIVE_MIGRATION_STRATEGY,
    )
    stable_dimensions = {
        "terminal_response",
        "showdown_range",
        "donk",
        "delayed_probe",
    }
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: _migration_probe_payload(
            repeatable=False,
            stable_dimensions=stable_dimensions,
        ),
    )

    result = evaluate_national_capabilities(bot)
    checks = result["checks_by_id"]

    assert result["dynamic_runtime_probe"]["repeatability_ok"] is False
    for check_id in (
        "terminal_response_adaptation",
        "showdown_range_adaptation",
        "donk_line_reachability",
        "delayed_probe_line_reachability",
    ):
        assert checks[check_id]["passed"] is True


def test_one_nonrepeatable_migration_dimension_fails_without_poisoning_others(
    tmp_path,
    monkeypatch,
):
    import national_runtime_probe

    bot = _write_bot(
        tmp_path / "national_v13g_donk_jitter",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy=LIVE_MIGRATION_STRATEGY,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: _migration_probe_payload(
            repeatable=False,
            stable_dimensions={
                "terminal_response",
                "showdown_range",
                "delayed_probe",
            },
        ),
    )

    checks = evaluate_national_capabilities(bot)["checks_by_id"]

    assert checks["terminal_response_adaptation"]["passed"] is True
    assert checks["showdown_range_adaptation"]["passed"] is True
    assert checks["delayed_probe_line_reachability"]["passed"] is True
    assert checks["donk_line_reachability"]["passed"] is False
    assert (
        checks["donk_line_reachability"]["evidence"]["facts"][
            "dynamic_evidence"
        ]["integrity_ok"]
        is False
    )


def test_repeatable_specific_final_wire_and_live_paths_prove_all_migrations(
    tmp_path,
    monkeypatch,
):
    import national_runtime_probe

    bot = _write_bot(
        tmp_path / "national_v13g_trusted_migration",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy=LIVE_MIGRATION_STRATEGY,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: _migration_probe_payload(repeatable=True),
    )

    result = evaluate_national_capabilities(bot)
    paths = result["incremental_model_evidence"]["source_rooted_live_access_paths"]
    checks = result["checks_by_id"]

    assert "hand_runtime.can_donk" in paths
    assert "hand_runtime.can_delayed_probe" in paths
    assert "opponent_runtime.fold_to_raise" in paths
    assert "opponent_runtime.showdown_range.tightness" in paths
    for check_id in (
        "terminal_response_adaptation",
        "showdown_range_adaptation",
        "donk_line_reachability",
        "delayed_probe_line_reachability",
    ):
        assert checks[check_id]["passed"] is True
        dynamic = checks[check_id]["evidence"]["facts"]["dynamic_evidence"]
        assert dynamic["integrity_ok"] is True
        assert dynamic["left_wire"] != dynamic["right_wire"]


def test_donk_and_delayed_probe_emit_independent_capability_checks(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13g_split",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def _choose(req):
    hand_runtime = req.get('hand_runtime', {})
    return 600 if hand_runtime.get('can_donk') else 0
def get_action(req, requests): return _choose(req)
def get_baseline_action(req, requests): return _choose(req)
""",
    )

    result = evaluate_national_capabilities(bot)
    checks = result["checks_by_id"]

    assert checks["donk_line_reachability"]["passed"] is True
    assert checks["delayed_probe_line_reachability"]["passed"] is False
    # Compatibility aggregate remains available but is no longer a primary.
    assert checks["semantic_line_reachability"]["passed"] is False


def test_showdown_baseline_influence_is_not_masked_by_common_refinement(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13h",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
import time
def _choose(req):
    shown = req.get('opponent_runtime', {}).get('showdown_range', {})
    if (shown.get('selection_scope') == 'reached_showdown_only'
            and shown.get('confidence', 0.0) > 0.0
            and shown.get('adaptation_weight', 0.0) > 0.0
            and shown.get('tightness', 0.0) > 0.3):
        return -1
    return 0
def get_action(req, requests): return _choose(req)
def get_baseline_action(req, requests): return _choose(req)
def iter_refinements(req, requests, baseline, deadline):
    for step in range(16):
        if time.monotonic() >= deadline:
            return
        yield {'action': -1, 'sample_count': step + 1}
""",
    )

    result = evaluate_national_capabilities(bot)
    showdown = result["dynamic_runtime_probe"]["strategy_influence"]["dimensions"][
        "showdown_range"
    ]

    assert showdown["ok"] is True
    assert any(
        row["tiers"]["baseline"]["changed"] for row in showdown["rows"]
    )


def test_intermediate_refinement_only_does_not_prove_final_wire_influence(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v13i",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
import time
def _influence(req):
    profile = req.get('opponent_runtime', {})
    terminal = profile.get('terminal_response', {})
    shown = profile.get('showdown_range', {})
    hand = req.get('hand_runtime', {})
    return bool(
        (terminal.get('confidence', 0.0) > 0.0 and profile.get('fold_to_raise', 0.0) > 0.5)
        or (shown.get('selection_scope') == 'reached_showdown_only'
            and shown.get('confidence', 0.0) > 0.0
            and shown.get('adaptation_weight', 0.0) > 0.0
            and shown.get('tightness', 0.0) > 0.3)
        or hand.get('can_donk')
        or hand.get('can_delayed_probe')
    )
def get_action(req, requests):
    _influence(req)
    return 0
def get_baseline_action(req, requests):
    _influence(req)
    return 0
def iter_refinements(req, requests, baseline, deadline):
    if time.monotonic() >= deadline:
        return
    yield -1 if _influence(req) else baseline
    if time.monotonic() < deadline:
        yield baseline
""",
    )

    result = evaluate_national_capabilities(bot)
    dimensions = result["dynamic_runtime_probe"]["strategy_influence"]["dimensions"]

    assert dimensions["terminal_response"]["ok"] is False
    assert dimensions["showdown_range"]["ok"] is False
    assert dimensions["semantic_lines"]["ok"] is False
    for dimension in ("terminal_response", "showdown_range", "semantic_lines"):
        assert any(
            tier.get("trajectory_changed") and not tier.get("changed")
            for row in dimensions[dimension]["rows"]
            for tier in row["tiers"].values()
        )


def test_truthy_deadline_label_does_not_prove_bounded_decision_runtime(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v14",
        national_bot="""
import argparse
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def _send_wire_action(self, action): pass
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        strategy="""
import time
MAX_SAMPLES = 64
def get_action(req, requests):
    baseline = 0
    deadline = time.monotonic() + 1.0
    return baseline if deadline else -1
""",
    )

    result = evaluate_national_capabilities(bot)

    assert result["checks_by_id"]["decision_time_budget_visible"]["passed"] is False
    assert result["decision_time_evidence"]["deadline_checks"] == []


def test_killable_runtime_and_fast_baseline_use_disjoint_dynamic_facts(
    tmp_path,
    monkeypatch,
):
    import national_runtime_probe

    bot = _write_bot(
        tmp_path / "national_v14b",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def get_action(req, requests): return 0
def get_baseline_action(req, requests): return 0
""",
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "ok": False,
            "issues": ["strategy_baseline_slower_than_250ms"],
            "artifacts": [],
            "tracker": {"ok": False, "issues": []},
            "hand_context": {"ok": False, "issues": []},
            "strategy_influence": {"ok": False, "issues": [], "rows": []},
            "decision_runtime": {
                "ok": False,
                "safety_ok": True,
                "safety_issues": [],
                "baseline_ok": False,
                "baseline_issues": ["strategy_baseline_slower_than_250ms"],
                "refinement_ok": True,
                "refinement_issues": [],
                "baseline_samples_ms": [300.0],
                "fallback_ready_samples_ms": [1.0],
                "timeout_recovery": {"timeout": {}, "recovery": {}},
            },
        },
    )

    result = evaluate_national_capabilities(bot)
    killable = result["checks_by_id"]["killable_decision_runtime"]
    baseline = result["checks_by_id"]["fast_strategy_baseline"]

    assert killable["passed"] is True
    assert killable["evidence"]["facts"]["safety_ok"] is True
    assert killable["evidence"]["facts"]["safety_issues"] == []
    assert baseline["passed"] is False
    assert baseline["evidence"]["facts"]["baseline_ok"] is False
    assert baseline["evidence"]["facts"]["baseline_issues"] == [
        "strategy_baseline_slower_than_250ms"
    ]


def test_cold_cache_or_discarded_table_read_is_not_precompute_evidence(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v15",
        national_bot="""
import argparse
POK_OFFICIAL_ACTION_DELAY = 0.30
class NativeNationalBot:
    def _send_wire_action(self, action): pass
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
""",
        strategy="""
from functools import lru_cache
LOOKUP_TABLE = {i: i for i in range(32)}
@lru_cache(maxsize=128)
def cached_fact(key): return key * 2
def get_action(req, requests):
    LOOKUP_TABLE.get(3)
    cached_fact(4)
    return 0
""",
    )

    result = evaluate_national_capabilities(bot)

    assert result["checks_by_id"]["precompute_lookup_path"]["passed"] is False
    assert result["precompute_evidence"]["consumed_artifacts"] == []


def test_dead_branch_cache_warmup_does_not_prove_precompute(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v16",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
from functools import lru_cache
@lru_cache(maxsize=128)
def equity_lookup(key): return key / 100.0
if False:
    equity_lookup(50)
def get_action(req, requests):
    return 100 if equity_lookup(50) > 0.4 else 0
""",
    )

    result = evaluate_national_capabilities(bot)

    artifact = result["precompute_evidence"]["artifacts"][0]
    assert artifact["built_before_first_decision"] is False
    assert result["checks_by_id"]["precompute_lookup_path"]["passed"] is False


def test_same_named_local_does_not_consume_foreign_precompute_artifact(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v17",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def get_action(req, requests):
    PREFLOP_LOOKUP_TABLE = {i: i / 31 for i in range(32)}
    return 100 if PREFLOP_LOOKUP_TABLE.get(12) else 0
""",
    )
    (bot / "constants.py").write_text(
        "PREFLOP_LOOKUP_TABLE = {i: i / 31 for i in range(32)}\n",
        encoding="utf-8",
    )

    result = evaluate_national_capabilities(bot)

    foreign = next(
        item for item in result["precompute_evidence"]["artifacts"]
        if item["location"].startswith("constants.py:")
    )
    assert foreign["consumed_by_decision"] is False


def test_full_match_alias_passed_to_helper_is_detected(tmp_path):
    bot = _write_bot(
        tmp_path / "national_v18",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="""
def summarize(events):
    return len(events)
def get_action(req, requests):
    events = requests
    return summarize(events)
""",
    )

    result = evaluate_national_capabilities(bot)

    assert result["checks_by_id"]["decision_path_no_full_history_scan"]["passed"] is False
    assert any("full_match_argument" in item for item in result["decision_path_risks"]["history_scans"])


def test_runtime_probe_infrastructure_is_inconclusive_not_candidate_debt(tmp_path, monkeypatch):
    import national_runtime_probe

    bot = _write_bot(
        tmp_path / "national_v19",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="def get_action(req, requests): return 0\n",
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_a, **_k: {
            "schema_version": 1,
            "ok": False,
            "failure_class": "probe_infra",
            "issues": ["bwrap unavailable"],
            "artifacts": [],
            "tracker": {"ok": False, "issues": ["not_run"]},
            "strategy_influence": {"ok": False, "issues": ["not_run"]},
        },
    )

    result = evaluate_national_capabilities(bot)

    assert result["ok"] is False
    assert result["conclusive"] is False
    assert result["outcome"] == "infrastructure_failure"
    assert result["required_failures"] == []
    for check_id in (
        "precompute_lookup_path",
        "persistent_match_memory",
        "incremental_opponent_model",
    ):
        assert result["checks_by_id"][check_id]["passed"] is None
    assert not any(
        item["check_id"] in {
            "precompute_lookup_path",
            "persistent_match_memory",
            "incremental_opponent_model",
        }
        for item in result["advisory_warnings"]
    )


def test_capability_source_read_error_is_infrastructure_not_missing_code(
    tmp_path, monkeypatch
):
    bot = _write_bot(
        tmp_path / "national_v20",
        national_bot=NATIVE_BOT_TEMPLATE,
        strategy="def get_action(req, requests): return 0\n",
    )
    target = (bot / "strategy.py").resolve()
    original = Path.read_text

    def fail_one(path, *args, **kwargs):
        if path.resolve() == target:
            raise OSError("simulated storage failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_one)
    result = evaluate_national_capabilities(bot)

    assert result["outcome"] == "infrastructure_failure"
    assert result["conclusive"] is False
    assert result["required_failures"] == []
    assert result["infrastructure_failures"][0]["component"] == "capability_source_reader"
