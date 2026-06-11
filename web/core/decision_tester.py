"""
Decision Scenario Tester for Poker Bots.

Runs a set of predefined poker scenarios against a bot and checks if
its actions are reasonable (no catastrophic blunders like folding the nuts).

Dynamic Regression Test Generation (B3):
    Heuristically generates test scenarios from worker diffs by parsing
    changed constants and new conditional branches. No LLM calls needed.

Usage:
    python evolution_workspace/decision_tester.py bots/claude_v11/main.py
    python evolution_workspace/decision_tester.py bots/claude_v11/main.py --verbose
"""

import json
import logging
import re
import subprocess
import sys
import os
import time
from collections import OrderedDict

from evolution_infra import locked_file

log = logging.getLogger("pok.scheduler")
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
SCENARIOS_FILE = WORKSPACE / "test_scenarios.json"
RESULTS_DIR = WORKSPACE / "results"
DYNAMIC_SCENARIOS_FILE = RESULTS_DIR / "dynamic_scenarios.json"

MAX_DYNAMIC_SCENARIOS = 100
TIMEOUT = 10  # seconds per bot decision

# Card encoding: number = card // 4 + 2 (2-14=2-A), suit = card % 4 (0=h,1=d,2=s,3=c)
# Integer 0-51
# Examples: 0=2h, 1=2d, 2=2s, 3=2c, 4=3h, ..., 48=Ah, 49=Ad, 50=As, 51=Ac

CRITICAL_SCENARIO_IDS = {
    # Premium preflop hands — must never fold
    "preflop_aa_first_act",
    "preflop_kk_first_act",
    "preflop_qq_facing_raise",
    "preflop_aks_facing_allin",
    "preflop_jj_facing_3bet",
    # Nuts / extremely strong made hands — must never fold
    "flop_top_set_safe_board",
    "river_nut_flush_facing_bet",
    "flop_nut_straight_dry_board",
    "river_full_house_facing_raise",
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


# ──────────────────────────────────────────────
# Dynamic Regression Test Generation (B3)
# ──────────────────────────────────────────────

def _make_base_input(my_id=0, dealer_id=0, my_chips=20000, my_cards=None,
                     public_cards=None, history=None, hand=0):
    """Build a minimal valid bot input dict."""
    return {
        "my_id": my_id,
        "dealer_id": dealer_id,
        "num_players": 2,
        "my_chips": my_chips,
        "my_cards": my_cards or [],
        "public_cards": public_cards or [],
        "history": history or [],
        "hand": hand,
        "max_hand": 70,
        "total_win_chips": [0, 0],
        "total_win_games": [0, 0],
    }


# ── Template Scenarios ────────────────────────────────────────────────────────
# Cover common game situations so diff-generated scenarios can build on top.

TEMPLATE_SCENARIOS = [
    # --- Preflop templates ---
    {
        "id": "tpl_preflop_sb_strong_open",
        "description": "Template: SB with strong hand, first to act preflop",
        "input": _make_base_input(
            my_id=0, dealer_id=0, my_chips=20000,
            my_cards=[48, 44],  # AK offsuit
        ),
        "forbidden_actions": ["fold"],
        "_covers": "preflop_sb_open",
    },
    {
        "id": "tpl_preflop_bb_facing_raise",
        "description": "Template: BB facing SB raise, medium hand",
        "input": _make_base_input(
            my_id=1, dealer_id=0, my_chips=19900,
            my_cards=[40, 36],  # QJ offsuit
            history=[
                {"round": 0, "player_id": 0, "action": 250, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 250},
            ],
        ),
        "forbidden_actions": [],
        "_covers": "preflop_bb_vs_raise",
    },
    {
        "id": "tpl_preflop_sb_facing_3bet",
        "description": "Template: SB facing 3bet from BB",
        "input": _make_base_input(
            my_id=0, dealer_id=0, my_chips=19750,
            my_cards=[44, 40],  # KQ
            history=[
                {"round": 0, "player_id": 0, "action": 250, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 250},
                {"round": 0, "player_id": 1, "action": 700, "action_type": "raise",
                 "bet_amount": 600, "round_bet": 700},
            ],
        ),
        "forbidden_actions": [],
        "_covers": "preflop_sb_vs_3bet",
    },
    # --- Flop templates ---
    {
        "id": "tpl_flop_sb_check_or_bet",
        "description": "Template: Flop, SB first to act with top pair",
        "input": _make_base_input(
            my_id=0, dealer_id=0, my_chips=19700,
            my_cards=[44, 40],  # KQ
            public_cards=[40, 20, 4],  # Q-7-3 rainbow
            history=[
                {"round": 0, "player_id": 0, "action": 250, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 250},
                {"round": 0, "player_id": 1, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 250},
            ],
        ),
        "forbidden_actions": ["fold"],
        "_covers": "flop_sb_act_first",
    },
    {
        "id": "tpl_flop_bb_facing_cbet",
        "description": "Template: Flop, BB facing cbet with middle pair",
        "input": _make_base_input(
            my_id=1, dealer_id=0, my_chips=19700,
            my_cards=[36, 32],  # JT
            public_cards=[32, 20, 8],  # T-7-4
            history=[
                {"round": 0, "player_id": 0, "action": 250, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 250},
                {"round": 0, "player_id": 1, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 250},
                {"round": 1, "player_id": 0, "action": 400, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 400},
            ],
        ),
        "forbidden_actions": [],
        "_covers": "flop_bb_vs_cbet",
    },
    # --- Turn templates ---
    {
        "id": "tpl_turn_act_first",
        "description": "Template: Turn, first to act with two pair",
        "input": _make_base_input(
            my_id=0, dealer_id=0, my_chips=19400,
            my_cards=[40, 20],  # Q7
            public_cards=[41, 21, 8, 36],  # Q-7-4-J
            history=[
                {"round": 0, "player_id": 0, "action": 250, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 250},
                {"round": 0, "player_id": 1, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 250},
                {"round": 1, "player_id": 0, "action": 0, "action_type": "check",
                 "bet_amount": 0, "round_bet": 0},
                {"round": 1, "player_id": 1, "action": 300, "action_type": "raise",
                 "bet_amount": 300, "round_bet": 300},
                {"round": 1, "player_id": 0, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 300},
            ],
        ),
        "forbidden_actions": ["fold"],
        "_covers": "turn_act_first_twopair",
    },
    # --- River templates ---
    {
        "id": "tpl_river_facing_bet",
        "description": "Template: River, facing bet with medium strength",
        "input": _make_base_input(
            my_id=1, dealer_id=0, my_chips=18500,
            my_cards=[36, 32],  # JT
            public_cards=[32, 20, 8, 36, 4],  # T-7-4-J-3
            history=[
                {"round": 0, "player_id": 0, "action": 250, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 250},
                {"round": 0, "player_id": 1, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 250},
                {"round": 1, "player_id": 0, "action": 0, "action_type": "check",
                 "bet_amount": 0, "round_bet": 0},
                {"round": 1, "player_id": 1, "action": 300, "action_type": "raise",
                 "bet_amount": 300, "round_bet": 300},
                {"round": 1, "player_id": 0, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 300},
                {"round": 2, "player_id": 0, "action": 0, "action_type": "check",
                 "bet_amount": 0, "round_bet": 0},
                {"round": 2, "player_id": 1, "action": 0, "action_type": "check",
                 "bet_amount": 0, "round_bet": 0},
                {"round": 3, "player_id": 0, "action": 600, "action_type": "raise",
                 "bet_amount": 600, "round_bet": 600},
            ],
        ),
        "forbidden_actions": [],
        "_covers": "river_facing_bet",
    },
]

# Map constant name prefixes to templates that exercise the relevant code path.
_CONSTANT_TEMPLATE_MAP = {
    "SB_OPEN": "preflop_sb_open",
    "BB_ISO": "preflop_bb_vs_raise",
    "BB_CALL": "preflop_bb_vs_raise",
    "BB_VALUE_3BET": "preflop_bb_vs_raise",
    "BB_BLUFF_3BET": "preflop_bb_vs_raise",
    "RAISE_RATIO": "flop_sb_act_first",
    "FOLD_FLOP": "flop_sb_act_first",
    "FOLD_TURN": "turn_act_first_twopair",
    "FOLD_RIVER": "river_facing_bet",
    "CALL_MARGIN": "flop_bb_vs_cbet",
    "EQR_": "flop_bb_vs_cbet",
    "ANTI_LOCK": "flop_sb_act_first",
    "OVERBET": "flop_sb_act_first",
    "BLOCKER_BLUFF": "flop_sb_vs_3bet",
    "SB_VS_RERAISE": "preflop_sb_vs_3bet",
    "LIGHT_4BET": "preflop_sb_vs_3bet",
    "WETNESS": "flop_sb_act_first",
    "FLUSH_PRESSURE": "flop_sb_act_first",
    "STRAIGHT_PRESSURE": "flop_sb_act_first",
    "TEXTURE": "flop_sb_act_first",
    "TRAP": "flop_sb_act_first",
    "PASSIVE": "flop_bb_vs_cbet",
    "PRIOR_": "flop_bb_vs_cbet",
    "TOURNAMENT": "preflop_sb_open",
    "BIG_POT": "flop_sb_act_first",
}


def _find_template_for_constant(const_name):
    """Find the best template scenario for a given constant name."""
    for prefix, cover_key in _CONSTANT_TEMPLATE_MAP.items():
        if const_name.startswith(prefix):
            for tpl in TEMPLATE_SCENARIOS:
                if tpl.get("_covers") == cover_key:
                    return tpl
    # Default: return the flop template as most generic
    for tpl in TEMPLATE_SCENARIOS:
        if tpl.get("_covers") == "flop_sb_act_first":
            return tpl
    return TEMPLATE_SCENARIOS[0]


def load_dynamic_scenarios():
    """Load dynamic scenarios from the JSON file if it exists."""
    if not DYNAMIC_SCENARIOS_FILE.exists():
        return []
    try:
        with locked_file(DYNAMIC_SCENARIOS_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load dynamic scenarios: %s", e)
    return []


def save_dynamic_scenarios(scenarios):
    """Save dynamic scenarios to JSON file. Keeps at most MAX_DYNAMIC_SCENARIOS."""
    if not scenarios:
        return
    # Cap at max
    scenarios = scenarios[-MAX_DYNAMIC_SCENARIOS:]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with locked_file(DYNAMIC_SCENARIOS_FILE, "w") as f:
            json.dump(scenarios, f, indent=2, ensure_ascii=False)
    except OSError as e:
        log.warning("Failed to save dynamic scenarios: %s", e)


def merge_dynamic_scenarios(base_scenarios, dynamic_scenarios):
    """Merge dynamic scenarios into base scenarios.

    Deduplicates by id: if a dynamic scenario has the same id as a base scenario,
    the dynamic one replaces it. Otherwise dynamic are appended.
    """
    if not dynamic_scenarios:
        return base_scenarios
    base_ids = {s.get("id") for s in base_scenarios}
    merged = list(base_scenarios)
    for ds in dynamic_scenarios:
        if ds.get("id") not in base_ids:
            merged.append(ds)
    return merged


# ── Diff Parsing ──────────────────────────────────────────────────────────────

# Match Python assignment lines: CONSTANT_NAME = <value>
_CONST_ASSIGN_RE = re.compile(
    r"^([A-Z][A-Z0-9_]+)\s*=\s*(.+)$"
)

# Match changed values in unified diff lines: "-OLD = val" / "+NEW = val"
_DIFF_OLD_CONST_RE = re.compile(
    r"^-([A-Z][A-Z0-9_]+)\s*=\s*(.+)$"
)
_DIFF_NEW_CONST_RE = re.compile(
    r"^\+([A-Z][A-Z0-9_]+)\s*=\s*(.+)$"
)

# Match new if/elif branches
_DIFF_NEW_BRANCH_RE = re.compile(
    r"^\+\s*(if|elif)\s+(.+):$"
)


def _parse_numeric(val_str):
    """Try to parse a numeric value from a string, returning float or None."""
    val_str = val_str.strip().split("#")[0].strip().rstrip(",")
    try:
        return float(val_str)
    except ValueError:
        return None


def parse_constant_changes(diff_text):
    """Parse a unified diff to find changed constants.

    Returns list of dicts: {
        name: str, old_value: str, new_value: str,
        old_numeric: float|None, new_numeric: float|None
    }
    """
    changes = []
    old_consts = {}
    new_consts = {}

    for line in diff_text.splitlines():
        m = _DIFF_OLD_CONST_RE.match(line)
        if m:
            old_consts[m.group(1)] = m.group(2).strip()
        m = _DIFF_NEW_CONST_RE.match(line)
        if m:
            new_consts[m.group(1)] = m.group(2).strip()

    # Find constants that changed
    for name in sorted(set(old_consts.keys()) & set(new_consts.keys())):
        old_val = old_consts[name]
        new_val = new_consts[name]
        if old_val != new_val:
            changes.append({
                "name": name,
                "old_value": old_val,
                "new_value": new_val,
                "old_numeric": _parse_numeric(old_val),
                "new_numeric": _parse_numeric(new_val),
            })

    # Also include brand-new constants (in new but not in old)
    for name in sorted(set(new_consts.keys()) - set(old_consts.keys())):
        val = new_consts[name]
        changes.append({
            "name": name,
            "old_value": None,
            "new_value": val,
            "old_numeric": None,
            "new_numeric": _parse_numeric(val),
        })

    return changes


def parse_new_branches(diff_text):
    """Parse a unified diff to find new if/elif branches.

    Returns list of dicts: {keyword: "if"|"elif", condition: str}
    """
    branches = []
    for line in diff_text.splitlines():
        m = _DIFF_NEW_BRANCH_RE.match(line)
        if m:
            branches.append({
                "keyword": m.group(1),
                "condition": m.group(2).strip(),
            })
    return branches


def generate_scenarios_from_diff(diff_text, source_dir=None, target_dir=None):
    """Generate heuristic test scenarios from a worker diff.

    No LLM call — purely pattern matching. Creates scenarios that exercise
    code paths affected by constant changes and new conditional branches.

    Args:
        diff_text: Unified diff string between source and target bot.
        source_dir: Path to source bot directory (optional, for context).
        target_dir: Path to target bot directory (optional, for context).

    Returns:
        List of scenario dicts, each with:
            id, description, input, forbidden_actions, expected_actions,
            severity, source_generation, created_at
    """
    scenarios = []
    ts = time.time()

    # 1. Generate scenarios from constant changes
    const_changes = parse_constant_changes(diff_text)
    for change in const_changes:
        const_name = change["name"]
        tpl = _find_template_for_constant(const_name)

        # Determine expected behavior from the constant change
        expected_actions, forbidden_actions = _infer_expectations_from_change(change)

        scenario = {
            "id": f"dyn_const_{const_name.lower()}_{int(ts)}",
            "description": (
                f"Dynamic: {const_name} changed "
                f"from {change['old_value']} to {change['new_value']}"
            ),
            "input": dict(tpl["input"]),  # copy template input
            "forbidden_actions": forbidden_actions,
            "expected_actions": expected_actions,
            "severity": "advisory",
            "source_generation": "dynamic_const",
            "created_at": ts,
        }
        scenarios.append(scenario)

    # 2. Generate scenarios from new branches
    new_branches = parse_new_branches(diff_text)
    for i, branch in enumerate(new_branches[:10]):  # cap at 10 branch scenarios
        cond = branch["condition"]
        # Choose template based on branch context
        tpl = _infer_template_from_condition(cond)

        # For new branches, we want to ensure no crash and reasonable action
        scenario = {
            "id": f"dyn_branch_{i}_{int(ts)}",
            "description": (
                f"Dynamic: new {branch['keyword']} branch: "
                f"{cond[:80]}"
            ),
            "input": dict(tpl["input"]),
            "forbidden_actions": [],
            "expected_actions": [],
            "severity": "advisory",
            "source_generation": "dynamic_branch",
            "created_at": ts,
        }
        scenarios.append(scenario)

    # Cap total scenarios generated from a single diff
    return scenarios[:20]


def _infer_expectations_from_change(change):
    """Infer expected/forbidden actions from how a constant changed.

    Returns (expected_actions, forbidden_actions).
    """
    const_name = change["name"]
    old_num = change["old_numeric"]
    new_num = change["new_numeric"]

    forbidden = []
    expected = []

    # Fold threshold changes: lower values = tighter (fold more)
    if "FOLD" in const_name:
        if new_num is not None and old_num is not None:
            if new_num > old_num:
                # Threshold raised: expect more folding tolerance
                expected = ["call", "fold"]
            else:
                # Threshold lowered: expect tighter play
                expected = ["call", "fold"]
                forbidden = ["allin"]
        else:
            forbidden = ["allin"]

    # Raise/sizing changes
    elif any(kw in const_name for kw in ("RAISE", "BET", "OVERBET", "SIZING")):
        forbidden = ["fold"]

    # Aggression/bluff changes
    elif any(kw in const_name for kw in ("BLUFF", "AGGR", "SEMI_BLUFF", "BLOCKER")):
        forbidden = ["allin"]

    # Threshold changes for calling
    elif "CALL" in const_name or "EQR" in const_name:
        expected = ["call", "fold", "raise"]
        forbidden = ["allin"]

    # Default: just check it doesn't crash
    else:
        expected = []
        forbidden = []

    return expected, forbidden


def _infer_template_from_condition(condition):
    """Pick the best template scenario based on condition text."""
    cond_lower = condition.lower()

    if "preflop" in cond_lower or "round" in cond_lower and "0" in cond_lower:
        for tpl in TEMPLATE_SCENARIOS:
            if "preflop" in tpl.get("_covers", ""):
                return tpl

    if "river" in cond_lower or "round" in cond_lower and "3" in cond_lower:
        for tpl in TEMPLATE_SCENARIOS:
            if "river" in tpl.get("_covers", ""):
                return tpl

    if "turn" in cond_lower or "round" in cond_lower and "2" in cond_lower:
        for tpl in TEMPLATE_SCENARIOS:
            if "turn" in tpl.get("_covers", ""):
                return tpl

    if "flop" in cond_lower or "round" in cond_lower and "1" in cond_lower:
        for tpl in TEMPLATE_SCENARIOS:
            if "flop" in tpl.get("_covers", ""):
                return tpl

    # Default: use the most common template (flop SB act first)
    return _find_template_for_constant("FOLD_FLOP")


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

    # B3: Merge persisted dynamic regression scenarios from file
    dynamic_from_file = load_dynamic_scenarios()
    if dynamic_from_file:
        scenarios = merge_dynamic_scenarios(scenarios, dynamic_from_file)

    # Merge runtime extra_scenarios (from LLM or heuristic generation)
    if extra_scenarios:
        scenarios = merge_dynamic_scenarios(scenarios, extra_scenarios)

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
