"""
Decision Scenario Tester for Poker Bots.

Runs a set of predefined poker scenarios against a bot and checks if
its actions are reasonable (no catastrophic blunders like folding the nuts).

Usage:
    python evolution_workspace/decision_tester.py bots/claude_v11/main.py
    python evolution_workspace/decision_tester.py bots/claude_v11/main.py --verbose
"""

import json
import subprocess
import sys
import os
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
SCENARIOS_FILE = WORKSPACE / "test_scenarios.json"

TIMEOUT = 10  # seconds per bot decision


def classify_action(action):
    """Convert numeric action to category string."""
    if action == -1:
        return "fold"
    elif action == -2:
        return "allin"
    elif action == 0:
        return "call"
    else:
        return "raise"


def run_single_scenario(bot_path, scenario):
    """Run a bot against a single scenario. Returns (passed, details)."""
    # Build the payload the bot expects
    payload = {
        "requests": [scenario["input"]],
        "responses": [],
    }

    try:
        bot_path_abs = os.path.abspath(bot_path)
        proc = subprocess.run(
            [sys.executable, bot_path_abs],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=os.path.dirname(bot_path_abs),
        )
        if proc.returncode != 0:
            return False, f"Bot crashed: {proc.stderr.strip()[:200]}"

        result = json.loads(proc.stdout.strip())
        action = int(result.get("response", -1))
        category = classify_action(action)

        # Check forbidden actions
        if category in scenario.get("forbidden_actions", []):
            return False, f"Forbidden action: {category} (action={action})"

        # Check expected actions (if specified)
        expected = scenario.get("expected_actions")
        if expected and category not in expected:
            return False, f"Action {category} not in expected {expected} (action={action})"

        return True, f"OK ({category}, action={action})"

    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except json.JSONDecodeError as e:
        return False, f"Invalid output: {e}"
    except Exception as e:
        return False, f"Error: {e}"


def run_decision_tests(bot_path, verbose=False):
    """Run all test scenarios against a bot. Returns pass rate (0.0 - 1.0)."""
    if not SCENARIOS_FILE.exists():
        if verbose:
            print("[DECISION TESTER] No scenarios file found, skipping.")
        return 1.0

    with open(SCENARIOS_FILE) as f:
        scenarios = json.load(f)

    if not scenarios:
        return 1.0

    passed = 0
    total = len(scenarios)

    for scenario in scenarios:
        ok, details = run_single_scenario(bot_path, scenario)
        if ok:
            passed += 1
        if verbose:
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {scenario['id']}: {details}")

    return passed / total if total > 0 else 1.0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python decision_tester.py <bot_main.py> [--verbose]")
        sys.exit(1)

    bot_path = sys.argv[1]
    verbose = "--verbose" in sys.argv

    rate = run_decision_tests(bot_path, verbose=verbose)
    print(f"\nDecision test pass rate: {rate:.0%} ({int(rate * 100)}%)")
    sys.exit(0 if rate >= 0.7 else 1)
