import ast
import json
import re
import subprocess
import sys
from pathlib import Path


def parse_diff(parent_dir: str, current_dir: str) -> list:
    """Run diff between two bot directories and identify changed .py files and functions."""
    result = subprocess.run(
        ["diff", "-ruN", parent_dir, current_dir],
        capture_output=True,
        text=True,
    )
    diff_output = result.stdout

    changes = []
    current_file = None
    current_hunk = []

    file_pattern = re.compile(r"^\+\+\+\s+(\S+)")
    hunk_pattern = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff_output.splitlines():
        m = file_pattern.match(line)
        if m:
            if current_file and current_hunk:
                changes.append(_process_hunk(current_file, current_hunk))
            current_file = m.group(1)
            current_hunk = []
            continue

        if hunk_pattern.match(line):
            if current_file and current_hunk:
                changes.append(_process_hunk(current_file, current_hunk))
                current_hunk = []
            current_hunk.append(line)
            continue

        if current_hunk is not None and line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            current_hunk.append(line)

    if current_file and current_hunk:
        changes.append(_process_hunk(current_file, current_hunk))

    # Flatten and deduplicate
    flat = []
    seen = set()
    for item in changes:
        if isinstance(item, list):
            for sub in item:
                key = (sub["file"], sub.get("function", sub.get("type")))
                if key not in seen:
                    seen.add(key)
                    flat.append(sub)
        else:
            key = (item["file"], item.get("function", item.get("type")))
            if key not in seen:
                seen.add(key)
                flat.append(item)
    return flat


def _process_hunk(filepath: str, hunk_lines: list) -> list:
    """Parse a diff hunk to find changed function signatures or file-level changes."""
    if not filepath.endswith(".py"):
        return [{"file": filepath, "type": "non_python"}]

    added_lines = [ln[1:] for ln in hunk_lines if ln.startswith("+") and not ln.startswith("+++")]
    removed_lines = [ln[1:] for ln in hunk_lines if ln.startswith("-") and not ln.startswith("---")]

    changed_funcs = []
    all_changed = added_lines + removed_lines

    for line in all_changed:
        stripped = line.strip()
        if stripped.startswith("def "):
            func_match = re.match(r"def\s+(\w+)\s*\(", stripped)
            if func_match:
                changed_funcs.append({
                    "file": filepath,
                    "type": "function_changed",
                    "function": func_match.group(1),
                })

    if not changed_funcs:
        changed_funcs.append({"file": filepath, "type": "code_changed"})

    return changed_funcs


def generate_test_scenarios(changed_functions: list, bot_code: dict) -> list:
    """Generate 2-3 poker scenarios per changed function."""
    scenarios = []

    for change in changed_functions:
        if change.get("type") == "non_python":
            continue

        func_name = change.get("function")
        file_path = change["file"]

        # Heuristic scenario generation based on function name
        base = {
            "target_file": file_path,
            "target_function": func_name,
            "description": f"Test scenario for {func_name or 'changed code'} in {Path(file_path).name}",
        }

        if func_name and "fold" in func_name.lower():
            scenarios.append({
                **base,
                "hand_state": {
                    "stage": "river",
                    "hole_cards": [12, 25],  # marginal hand
                    "board": [0, 13, 26, 39, 1],
                    "pot": 2000,
                    "to_call": 1500,
                    "stack": 5000,
                    "position": "BB",
                },
                "expected_behavior": "fold",
            })
            scenarios.append({
                **base,
                "hand_state": {
                    "stage": "turn",
                    "hole_cards": [48, 49],
                    "board": [0, 13, 26, 39],
                    "pot": 800,
                    "to_call": 200,
                    "stack": 8000,
                    "position": "SB",
                },
                "expected_behavior": "call_or_raise",
            })
        elif func_name and ("raise" in func_name.lower() or "bet" in func_name.lower() or "size" in func_name.lower()):
            scenarios.append({
                **base,
                "hand_state": {
                    "stage": "flop",
                    "hole_cards": [48, 49],
                    "board": [0, 13, 26],
                    "pot": 300,
                    "to_call": 0,
                    "stack": 10000,
                    "position": "SB",
                },
                "expected_behavior": "raise",
            })
            scenarios.append({
                **base,
                "hand_state": {
                    "stage": "preflop",
                    "hole_cards": [48, 49],
                    "board": [],
                    "pot": 150,
                    "to_call": 50,
                    "stack": 10000,
                    "position": "BB",
                },
                "expected_behavior": "raise",
            })
        elif func_name and "call" in func_name.lower():
            scenarios.append({
                **base,
                "hand_state": {
                    "stage": "river",
                    "hole_cards": [48, 49],
                    "board": [0, 13, 26, 39, 2],
                    "pot": 2000,
                    "to_call": 500,
                    "stack": 6000,
                    "position": "BB",
                },
                "expected_behavior": "call",
            })
            scenarios.append({
                **base,
                "hand_state": {
                    "stage": "flop",
                    "hole_cards": [12, 25],
                    "board": [0, 13, 26],
                    "pot": 500,
                    "to_call": 400,
                    "stack": 4000,
                    "position": "SB",
                },
                "expected_behavior": "fold",
            })
        else:
            # Generic scenarios for any changed function
            scenarios.append({
                **base,
                "hand_state": {
                    "stage": "flop",
                    "hole_cards": [48, 49],
                    "board": [0, 13, 26],
                    "pot": 300,
                    "to_call": 0,
                    "stack": 10000,
                    "position": "SB",
                },
                "expected_behavior": "any_legal",
            })
            scenarios.append({
                **base,
                "hand_state": {
                    "stage": "turn",
                    "hole_cards": [12, 25],
                    "board": [0, 13, 26, 39],
                    "pot": 1000,
                    "to_call": 500,
                    "stack": 5000,
                    "position": "BB",
                },
                "expected_behavior": "any_legal",
            })
            scenarios.append({
                **base,
                "hand_state": {
                    "stage": "river",
                    "hole_cards": [36, 37],
                    "board": [0, 13, 26, 39, 1],
                    "pot": 2000,
                    "to_call": 1000,
                    "stack": 3000,
                    "position": "SB",
                },
                "expected_behavior": "any_legal",
            })

    return scenarios


def run_bot_scenario(bot_path: str, scenario: dict) -> dict:
    """Run a bot against a specific game state scenario and capture its action."""
    hand_state = scenario["hand_state"]

    # Build a minimal judge-compatible request payload
    requests = _build_requests(hand_state)
    responses = []
    data = ""

    payload = json.dumps({"requests": requests, "responses": responses, "data": data}) + "\n"

    result = subprocess.run(
        [sys.executable, bot_path],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )

    if result.returncode != 0:
        return {
            "action": None,
            "error": result.stderr.strip() or f"exit code {result.returncode}",
            "raw_stdout": result.stdout.strip(),
        }

    try:
        output = json.loads(result.stdout.strip().splitlines()[-1])
        action = output.get("response")
        return {"action": action, "error": None, "raw_stdout": result.stdout.strip()}
    except Exception as e:
        return {
            "action": None,
            "error": str(e),
            "raw_stdout": result.stdout.strip(),
        }


def _build_requests(hand_state: dict) -> list:
    """Build a minimal request sequence for the bot subprocess protocol."""
    stage = hand_state["stage"]
    hole_cards = hand_state["hole_cards"]
    board = hand_state.get("board", [])
    pot = hand_state.get("pot", 0)
    to_call = hand_state.get("to_call", 0)
    stack = hand_state.get("stack", 10000)
    position = hand_state.get("position", "SB")

    requests = []

    # Game start
    requests.append(0)

    # Preflop
    if position == "SB":
        requests.append(1)
    else:
        requests.append(2)

    requests.append(hole_cards[0])
    requests.append(hole_cards[1])

    # Opponent actions before our turn (simplified: assume check/call to us)
    if stage == "preflop":
        if position == "BB":
            requests.append(50)  # SB called
    elif stage in ("flop", "turn", "river"):
        if position == "BB":
            requests.append(0)  # SB checked

    # Board cards
    if stage in ("flop", "turn", "river"):
        for c in board[:3]:
            requests.append(c)
    if stage in ("turn", "river"):
        if len(board) > 3:
            requests.append(board[3])
    if stage == "river":
        if len(board) > 4:
            requests.append(board[4])

    # Our turn with pot / to_call info encoded as action history
    # Bot protocol: requests are just integers; we append a synthetic "action" marker
    # that our bot can interpret.  Since real bots use the full request stream,
    # we append pot and to_call as negative markers so the bot can detect them.
    requests.append(-10)  # marker: scenario mode
    requests.append(pot)
    requests.append(to_call)
    requests.append(stack)

    return requests


def verify_behavior(master_plan: dict, scenarios: list, actual_actions: list) -> dict:
    """Compare actual bot actions against expected behavior from the master plan."""
    issues = []
    passed_count = 0
    total = len(scenarios)

    expected_changes = master_plan.get("expected_behavior_change", {})

    for scenario, actual in zip(scenarios, actual_actions):
        expected = scenario.get("expected_behavior", "any_legal")
        action = actual.get("action")
        error = actual.get("error")

        if error:
            issues.append({
                "scenario": scenario.get("description"),
                "issue": f"Bot crashed: {error}",
            })
            continue

        if expected == "any_legal":
            passed_count += 1
            continue

        if expected == "fold":
            if action == -1:
                passed_count += 1
            else:
                issues.append({
                    "scenario": scenario.get("description"),
                    "issue": f"Expected fold (-1), got {action}",
                })
        elif expected == "call_or_raise":
            if action is not None and action >= 0:
                passed_count += 1
            else:
                issues.append({
                    "scenario": scenario.get("description"),
                    "issue": f"Expected call/raise (>=0), got {action}",
                })
        elif expected == "raise":
            if action is not None and action > 0:
                passed_count += 1
            else:
                issues.append({
                    "scenario": scenario.get("description"),
                    "issue": f"Expected raise (>0), got {action}",
                })
        elif expected == "call":
            if action == 0:
                passed_count += 1
            else:
                issues.append({
                    "scenario": scenario.get("description"),
                    "issue": f"Expected call (0), got {action}",
                })
        else:
            passed_count += 1

    passed = passed_count == total and not issues
    confidence = "high" if passed else ("medium" if passed_count >= total * 0.7 else "low")

    return {
        "passed": passed,
        "issues": issues,
        "confidence": confidence,
        "passed_count": passed_count,
        "total": total,
    }
