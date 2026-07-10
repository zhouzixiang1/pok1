from pathlib import Path

from national_capability_contract import evaluate_national_capabilities, national_runtime_feedback_summary


def _write_bot(root: Path, *, national_bot: str, opponent: str = "", strategy: str = "") -> Path:
    root.mkdir(parents=True)
    (root / "national_bot.py").write_text(national_bot, encoding="utf-8")
    (root / "opponent.py").write_text(opponent, encoding="utf-8")
    (root / "strategy.py").write_text(strategy, encoding="utf-8")
    return root


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
class OpponentTracker:
    def __init__(self):
        self.match_profile = {}
    def update_opponent(self, event):
        self.match_profile['actions'] = self.match_profile.get('actions', 0) + 1
""",
        strategy="""
import time
PREFLOP_LOOKUP_TABLE = {(12, 12): 1.0}
BOARD_TEXTURE_CACHE = {}
MAX_DECISION_SAMPLES = 64
def get_action(req, requests, runtime_ctx=None):
    elapsed = time.monotonic()
    opp_profile = (runtime_ctx or {}).get('opponent_tracker')
    return 0 if opp_profile is None else 100
""",
    )

    result = evaluate_national_capabilities(bot)
    checks = {item["name"]: item["passed"] for item in result["checks"]}

    assert result["ok"] is True
    assert checks["precompute_lookup_path"] is True
    assert checks["incremental_opponent_model"] is True

    feedback = national_runtime_feedback_summary(bot, source_label="national_v3")
    assert "No advisory runtime-architecture gaps" in feedback
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
    lookup = {i: i for i in range(100)}
    return lookup.get(total, 0)
""",
    )

    result = evaluate_national_capabilities(bot)
    warning_names = {item["name"] for item in result["advisory_warnings"]}

    assert "decision_path_no_external_io" in warning_names
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
    return {i: i for i in range(100)}
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
