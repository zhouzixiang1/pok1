"""
Decision Scenario Tester for Poker Bots.

Runs a set of predefined poker scenarios against a bot and checks if
its actions are reasonable (no catastrophic blunders like folding the nuts).

Usage:
    python evolution_workspace/decision_tester.py bots/claude_v11/main.py
    python evolution_workspace/decision_tester.py bots/claude_v11/main.py --verbose
"""

import json
import logging
import subprocess
import sys
import os

log = logging.getLogger("pok.scheduler")
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
SCENARIOS_FILE = WORKSPACE / "test_scenarios.json"

TIMEOUT = 10  # seconds per bot decision

CRITICAL_SCENARIO_IDS = {
    # Premium preflop hands and unavoidable continues
    "preflop_aa_first_act",
    "preflop_kk_first_act",
    "preflop_qq_facing_raise",
    "preflop_aks_facing_allin",
    "preflop_jj_facing_3bet",
    # Strong made hands / high-equity postflop continues
    "flop_top_set_safe_board",
    "river_nut_flush_facing_bet",
    "flop_nut_straight_dry_board",
    "turn_two_pair_facing_bet",
    "river_full_house_facing_raise",
    "flop_flush_draw_facing_cbet",
    # Explicit blunder guard from the v29 failure mode
    "river_missed_draw_facing_big_bet",
}


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
    result = run_decision_tests_detail(bot_path, verbose=verbose)
    return result["pass_rate"]


def run_decision_tests_detail(bot_path, verbose=False, extra_scenarios=None):
    """Run all scenarios and return detailed critical/advisory gate data."""
    if not SCENARIOS_FILE.exists():
        if verbose:
            log.warning("No scenarios file found, skipping.")
        return {
            "pass_rate": 1.0,
            "passed": 0,
            "total": 0,
            "critical_passed": 0,
            "critical_total": 0,
            "critical_failures": [],
            "failures": [],
            "scenarios": [],
        }

    with open(SCENARIOS_FILE) as f:
        scenarios = json.load(f)

    # P0-3: Merge LLM-generated dynamic test scenarios
    if extra_scenarios:
        scenarios.extend(extra_scenarios)

    if not scenarios:
        return {
            "pass_rate": 1.0,
            "passed": 0,
            "total": 0,
            "critical_passed": 0,
            "critical_total": 0,
            "critical_failures": [],
            "failures": [],
            "scenarios": [],
        }

    passed = 0
    total = len(scenarios)
    critical_passed = 0
    critical_total = 0
    scenario_results = []
    failures = []
    critical_failures = []

    for scenario in scenarios:
        ok, details = run_single_scenario(bot_path, scenario)
        severity = scenario.get(
            "severity",
            "critical" if scenario.get("id") in CRITICAL_SCENARIO_IDS else "advisory",
        )
        if ok:
            passed += 1
            if severity == "critical":
                critical_passed += 1
        elif severity == "critical":
            critical_failures.append({"id": scenario["id"], "details": details})
        if severity == "critical":
            critical_total += 1
        if not ok:
            failures.append({"id": scenario["id"], "severity": severity, "details": details})
        scenario_results.append({
            "id": scenario["id"],
            "severity": severity,
            "passed": ok,
            "details": details,
        })
        if verbose:
            status = "PASS" if ok else "FAIL"
            log.info("  [%s] %s (%s): %s", status, scenario['id'], severity, details)

    return {
        "pass_rate": passed / total if total > 0 else 1.0,
        "passed": passed,
        "total": total,
        "critical_passed": critical_passed,
        "critical_total": critical_total,
        "critical_failures": critical_failures,
        "failures": failures,
        "scenarios": scenario_results,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python decision_tester.py <bot_main.py> [--verbose]")
        sys.exit(1)

    bot_path = sys.argv[1]
    verbose = "--verbose" in sys.argv

    from logging_config import configure_logging
    configure_logging()

    result = run_decision_tests_detail(bot_path, verbose=verbose)
    rate = result["pass_rate"]
    log.info("Decision test pass rate: %.0f%% (%d%%)", rate * 100, int(rate * 100))
    if result["critical_failures"]:
        log.error("Critical failures: %d", len(result["critical_failures"]))
        for failure in result["critical_failures"]:
            log.error("  - %s: %s", failure['id'], failure['details'])
    sys.exit(0 if rate >= 0.7 and not result["critical_failures"] else 1)
