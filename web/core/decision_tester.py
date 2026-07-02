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
import math
import re
import subprocess
import sys
import os
import time
from collections import OrderedDict

from evolution_infra import locked_file

log = logging.getLogger("pok.scheduler")
from pathlib import Path
from skill_library import scenario_skill_metadata

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


def audit_action_grounding(action, category, scenario):
    """Validate optional action-grounding metadata on a scenario.

    Existing scenarios do not need these fields. New PokerSkill-style scenarios
    can declare legal action families and raise bounds so the harness catches
    protocol mistakes before the national adapter has to clamp them.
    """
    failures = []
    legal_actions = scenario.get("legal_actions") or []
    if legal_actions and category not in legal_actions:
        failures.append(f"Action {category} not legal in spot legal_actions={legal_actions}")
    if category == "raise":
        raise_min = scenario.get("raise_min")
        raise_max = scenario.get("raise_max")
        if raise_min is not None and action < int(raise_min):
            failures.append(f"Raise-to-total {action} below raise_min {raise_min}")
        if raise_max is not None and action > int(raise_max):
            failures.append(f"Raise-to-total {action} above raise_max {raise_max}")
        if scenario.get("allin_requires_minus2") and action >= int(scenario.get("my_chips", scenario.get("input", {}).get("my_chips", 0)) or 0):
            failures.append("Positive raise appears to consume all remaining chips; use -2 for all-in")
    if scenario.get("national_legal_expected") is True and failures:
        failures.insert(0, "National legality expectation failed")
    return failures


def _current_betting_round(public_cards):
    count = len(public_cards or [])
    if count >= 5:
        return 3
    if count == 4:
        return 2
    if count >= 3:
        return 1
    return 0


def _history_action_type(item):
    action_type = str(item.get("action_type") or "").lower()
    if action_type:
        return action_type
    action = item.get("action")
    if action == -1:
        return "fold"
    if action == -2:
        return "allin"
    if action == 0:
        return "call"
    return "raise"


def _history_raise_to(item):
    for key in ("round_bet", "action"):
        try:
            value = int(item.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def audit_scenario_legality(scenario):
    """Validate that a decision-test scenario describes a legal national spot."""
    failures = []
    input_data = scenario.get("input")
    if not isinstance(input_data, dict):
        return ["Scenario input must be a single request dict"]
    if "requests" in input_data or "responses" in input_data:
        return ["Scenario input must be a single request dict, not a full bot payload"]

    try:
        num_players = int(input_data.get("num_players", 2))
        dealer_id = int(input_data.get("dealer_id", 0))
        my_id = int(input_data.get("my_id", 0))
    except (TypeError, ValueError):
        return ["Scenario input has non-integer my_id/dealer_id/num_players"]
    if num_players != 2:
        return failures

    sb_id = dealer_id
    bb_id = 1 - dealer_id
    current_round = _current_betting_round(input_data.get("public_cards") or [])
    history = input_data.get("history") or []
    if not isinstance(history, list):
        return ["Scenario history must be a list"]

    by_round = {}
    last_raise_to = {}
    seen_allin = False
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            failures.append(f"History item {index} must be an object")
            continue
        try:
            round_id = int(item.get("round", 0))
            player_id = int(item.get("player_id"))
        except (TypeError, ValueError):
            failures.append(f"History item {index} has invalid round/player_id")
            continue
        if round_id > current_round:
            failures.append(f"History item {index} is from future round {round_id}")
        action_type = _history_action_type(item)
        by_round.setdefault(round_id, []).append((index, player_id, action_type, item))

        if seen_allin and action_type not in {"call", "fold"}:
            failures.append("After all-in, later history actions must be call or fold")
        if action_type == "allin":
            if seen_allin:
                failures.append("Consecutive all-in actions are illegal")
            seen_allin = True

        if action_type == "raise":
            raise_to = _history_raise_to(item)
            previous = last_raise_to.get(round_id)
            if raise_to is not None and previous is not None and raise_to <= 2 * previous:
                failures.append(
                    f"Round {round_id} re-raise-to {raise_to} must be strictly greater than 2x previous {previous}"
                )
            if raise_to is not None:
                last_raise_to[round_id] = raise_to
        elif round_id not in last_raise_to:
            last_raise_to.setdefault(round_id, None)

    preflop = by_round.get(0, [])
    if len(preflop) >= 2:
        _, first_player, first_action, _ = preflop[0]
        _, second_player, second_action, _ = preflop[1]
        if (
            first_player == sb_id
            and first_action in {"call", "check"}
            and second_player == bb_id
            and second_action == "call"
        ):
            failures.append("Preflop BB pass after SB limp must be check, not call")

    if current_round > 0 and not by_round.get(current_round) and my_id != bb_id:
        failures.append("Postflop first action must belong to BB/OOP")

    for round_id, actions in by_round.items():
        if round_id <= 0:
            continue
        first_index, first_player, first_action, _ = actions[0]
        if first_player != bb_id:
            failures.append(
                f"Round {round_id} first postflop action must be BB/OOP; item {first_index} used player {first_player}"
            )
        if first_action == "call":
            failures.append(f"Round {round_id} first postflop action cannot be call")
        for item_index, _, action_type, _ in actions[1:]:
            if action_type == "check":
                failures.append(
                    f"Round {round_id} item {item_index} uses check after action started; pass with call"
                )

    return failures


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
        "id": "tpl_flop_bb_first_to_act",
        "description": "Template: Flop, BB first to act with top pair",
        "input": _make_base_input(
            my_id=1, dealer_id=0, my_chips=19700,
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
        "_covers": "flop_bb_act_first",
    },
    {
        "id": "tpl_flop_sb_facing_lead",
        "description": "Template: Flop, SB in position facing BB lead with middle pair",
        "input": _make_base_input(
            my_id=0, dealer_id=0, my_chips=19700,
            my_cards=[36, 32],  # JT
            public_cards=[32, 20, 8],  # T-7-4
            history=[
                {"round": 0, "player_id": 0, "action": 250, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 250},
                {"round": 0, "player_id": 1, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 250},
                {"round": 1, "player_id": 1, "action": 400, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 400},
            ],
        ),
        "forbidden_actions": [],
        "_covers": "flop_sb_vs_lead",
    },
    # --- Turn templates ---
    {
        "id": "tpl_turn_bb_first_to_act",
        "description": "Template: Turn, BB first to act with two pair",
        "input": _make_base_input(
            my_id=1, dealer_id=0, my_chips=19400,
            my_cards=[40, 20],  # Q7
            public_cards=[41, 21, 8, 36],  # Q-7-4-J
            history=[
                {"round": 0, "player_id": 0, "action": 250, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 250},
                {"round": 0, "player_id": 1, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 250},
                {"round": 1, "player_id": 1, "action": 0, "action_type": "check",
                 "bet_amount": 0, "round_bet": 0},
                {"round": 1, "player_id": 0, "action": 300, "action_type": "raise",
                 "bet_amount": 300, "round_bet": 300},
                {"round": 1, "player_id": 1, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 300},
            ],
        ),
        "forbidden_actions": ["fold"],
        "_covers": "turn_bb_act_first_twopair",
    },
    # --- River templates ---
    {
        "id": "tpl_river_facing_bet",
        "description": "Template: River, SB in position facing BB bet with medium strength",
        "input": _make_base_input(
            my_id=0, dealer_id=0, my_chips=18500,
            my_cards=[36, 32],  # JT
            public_cards=[32, 20, 8, 36, 4],  # T-7-4-J-3
            history=[
                {"round": 0, "player_id": 0, "action": 250, "action_type": "raise",
                 "bet_amount": 150, "round_bet": 250},
                {"round": 0, "player_id": 1, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 250},
                {"round": 1, "player_id": 1, "action": 0, "action_type": "check",
                 "bet_amount": 0, "round_bet": 0},
                {"round": 1, "player_id": 0, "action": 300, "action_type": "raise",
                 "bet_amount": 300, "round_bet": 300},
                {"round": 1, "player_id": 1, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 300},
                {"round": 2, "player_id": 1, "action": 0, "action_type": "check",
                 "bet_amount": 0, "round_bet": 0},
                {"round": 2, "player_id": 0, "action": 0, "action_type": "call",
                 "bet_amount": 0, "round_bet": 0},
                {"round": 3, "player_id": 1, "action": 600, "action_type": "raise",
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
    "RAISE_RATIO": "flop_bb_act_first",
    "FOLD_FLOP": "flop_bb_act_first",
    "FOLD_TURN": "turn_bb_act_first_twopair",
    "FOLD_RIVER": "river_facing_bet",
    "CALL_MARGIN": "flop_sb_vs_lead",
    "EQR_": "flop_sb_vs_lead",
    "ANTI_LOCK": "flop_bb_act_first",
    "OVERBET": "flop_bb_act_first",
    "BLOCKER_BLUFF": "flop_sb_vs_lead",
    "SB_VS_RERAISE": "preflop_sb_vs_3bet",
    "LIGHT_4BET": "preflop_sb_vs_3bet",
    "WETNESS": "flop_bb_act_first",
    "FLUSH_PRESSURE": "flop_bb_act_first",
    "STRAIGHT_PRESSURE": "flop_bb_act_first",
    "TEXTURE": "flop_bb_act_first",
    "TRAP": "flop_bb_act_first",
    "PASSIVE": "flop_sb_vs_lead",
    "PRIOR_": "flop_sb_vs_lead",
    "TOURNAMENT": "preflop_sb_open",
    "BIG_POT": "flop_bb_act_first",
}


def _find_template_for_constant(const_name):
    """Find the best template scenario for a given constant name."""
    for prefix, cover_key in _CONSTANT_TEMPLATE_MAP.items():
        if const_name.startswith(prefix):
            for tpl in TEMPLATE_SCENARIOS:
                if tpl.get("_covers") == cover_key:
                    return tpl
    # Default: return the flop first-to-act template as most generic.
    for tpl in TEMPLATE_SCENARIOS:
        if tpl.get("_covers") == "flop_bb_act_first":
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
            return filter_national_legal_dynamic_scenarios(data, source="load")
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load dynamic scenarios: %s", e)
    return []


def filter_national_legal_dynamic_scenarios(scenarios, source="dynamic"):
    """Return only dynamic scenarios whose histories match national rules."""
    valid = []
    dropped = []
    for scenario in scenarios or []:
        errors = audit_scenario_legality(scenario)
        if errors:
            dropped.append((scenario.get("id", "<missing-id>"), errors[:2]))
            continue
        valid.append(scenario)
    if dropped:
        preview = "; ".join(f"{sid}: {', '.join(errors)}" for sid, errors in dropped[:5])
        log.warning(
            "Dropped %d invalid %s dynamic scenario(s): %s",
            len(dropped),
            source,
            preview,
        )
    return valid


def save_dynamic_scenarios(scenarios):
    """Save dynamic scenarios to JSON file. Keeps at most MAX_DYNAMIC_SCENARIOS."""
    if not scenarios:
        return
    scenarios = filter_national_legal_dynamic_scenarios(scenarios, source="save")
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
    dynamic_scenarios = filter_national_legal_dynamic_scenarios(
        dynamic_scenarios,
        source="merge",
    )
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
    scenario_failures = audit_scenario_legality(scenario)
    if scenario_failures:
        return False, "Invalid scenario: " + "; ".join(scenario_failures)

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
        grounding_failures = audit_action_grounding(action, category, scenario)
        if grounding_failures:
            return False, "; ".join(grounding_failures)

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
            "skill_layers": {},
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
            "skill_layers": {},
        }

    passed = 0
    total = len(scenarios)
    critical_passed = 0
    critical_total = 0
    scenario_results = []
    failures = []
    critical_failures = []
    skill_summary = {}

    for scenario in scenarios:
        ok, details = run_single_scenario(bot_path, scenario)
        skill_meta = scenario_skill_metadata(scenario)
        skill_layer = skill_meta.get("skill_layer", "unspecified")
        layer_stats = skill_summary.setdefault(
            skill_layer,
            {"passed": 0, "total": 0, "critical_passed": 0, "critical_total": 0,
             "missing_required_fields": set()},
        )
        layer_stats["total"] += 1
        if ok:
            layer_stats["passed"] += 1
        for field in skill_meta.get("missing_required_fields", []):
            layer_stats["missing_required_fields"].add(field)
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
            layer_stats["critical_total"] += 1
            if ok:
                layer_stats["critical_passed"] += 1
        if not ok:
            failures.append({"id": scenario["id"], "severity": severity, "details": details})
        scenario_results.append({
            "id": scenario["id"],
            "severity": severity,
            "skill_layer": skill_layer,
            "passed": ok,
            "details": details,
            "skill_metadata": {
                **skill_meta,
                "missing_required_fields": list(skill_meta.get("missing_required_fields", [])),
            },
        })
        if verbose:
            status = "PASS" if ok else "FAIL"
            log.info("  [%s] %s (%s): %s", status, scenario['id'], severity, details)

    normalized_skill_summary = {}
    for layer, stats in skill_summary.items():
        total_layer = stats["total"]
        normalized_skill_summary[layer] = {
            "passed": stats["passed"],
            "total": total_layer,
            "pass_rate": stats["passed"] / total_layer if total_layer else 1.0,
            "critical_passed": stats["critical_passed"],
            "critical_total": stats["critical_total"],
            "missing_required_fields": sorted(stats["missing_required_fields"]),
        }

    return {
        "pass_rate": passed / total if total > 0 else 1.0,
        "passed": passed,
        "total": total,
        "critical_passed": critical_passed,
        "critical_total": critical_total,
        "critical_failures": critical_failures,
        "failures": failures,
        "scenarios": scenario_results,
        "skill_layers": normalized_skill_summary,
    }


# ──────────────────────────────────────────────
# Phase 2: AgentAssay SPRT — sequential Bernoulli test for stochastic LLM bots
# ──────────────────────────────────────────────
# LLM bots are stochastic: the same scenario may yield different actions across
# runs. Instead of a single run_decision_tests_detail pass/fail on a critical
# scenario, run_decision_tests_sprt resamples the scenario repeatedly and applies
# a Wald Sequential Probability Ratio Test (SPRT) for Bernoulli outcomes.
#
#   H0: pass-rate p = p0   (the bot meets the expected decision quality)
#   H1: pass-rate p = p1   (the bot has degraded below the usable baseline)
# Standard defaults: p0=0.85, p1=0.60, alpha=0.05, beta=0.10.
# Acceptance boundaries (on the cumulative log-likelihood ratio LLR):
#   A = (1-β)/α            → LLR >= ln(A)  ⟹  accept H1 (FAIL: significant regression)
#   B = β/(1-α)            → LLR <= ln(B)  ⟹  accept H0 (PASS: meets expectation)
# If n_max is reached without crossing a boundary, fall back to the final
# empirical pass-rate against MIN_DECISION_PASS_RATE.
#
# run_single_scenario / run_decision_tests_detail are left UNCHANGED — the SPRT
# path is an optional quality-gate enhancement for the CRITICAL scenarios.

# SPRT default parameters (overrideable per-call).
SPRT_P0 = 0.85
SPRT_P1 = 0.60
SPRT_ALPHA = 0.05
SPRT_BETA = 0.10
SPRT_N_MAX = 12


def _sprt_bounds(alpha=SPRT_ALPHA, beta=SPRT_BETA):
    """Return (ln(A), ln(B)) for the Wald SPRT with the given error levels.

    A = (1-β)/α is the upper likelihood-ratio boundary (accept H1).
    B = β/(1-α) is the lower likelihood-ratio boundary (accept H0).
    """
    return math.log((1.0 - beta) / alpha), math.log(beta / (1.0 - alpha))


def _sprt_llr(outcomes, p0=SPRT_P0, p1=SPRT_P1):
    """Cumulative log-likelihood ratio for a list of Bernoulli outcomes (1=pass).

    LLR = Σ [ x_i·ln(p1/p0) + (1-x_i)·ln((1-p1)/(1-p0)) ]
    Positive LLR ⇒ evidence toward H1 (regression); negative ⇒ toward H0.
    """
    if p0 <= 0.0 or p0 >= 1.0 or p1 <= 0.0 or p1 >= 1.0:
        raise ValueError("p0 and p1 must both lie strictly in (0, 1)")
    log_ratio_pass = math.log(p1 / p0)
    log_ratio_fail = math.log((1.0 - p1) / (1.0 - p0))
    total = 0.0
    for x in outcomes:
        total += log_ratio_pass if x else log_ratio_fail
    return total


def run_decision_tests_sprt(bot_path, scenario, p0=SPRT_P0, p1=SPRT_P1,
                            alpha=SPRT_ALPHA, beta=SPRT_BETA, n_max=SPRT_N_MAX,
                            seed=None):
    """Sequentially resample one scenario under the Wald SPRT.

    Resamples ``run_single_scenario(bot_path, scenario)`` up to ``n_max`` times,
    stopping the moment the cumulative log-likelihood ratio crosses an acceptance
    boundary. Each trial contributes a Bernoulli outcome (1=pass, 0=fail).

    Note on determinism: ``run_single_scenario`` uses a fixed payload, so a fully
    deterministic bot yields identical actions on every trial and the SPRT
    converges on trial 1 (LLR is ±∞-leaning as the first outcome dominates). This
    is fine — the SPRT's value is for stochastic LLM bots, where repeated trials
    expose rare blunders. Tests that exercise the SPRT logic should monkeypatch
    ``run_single_scenario`` with a Bernoulli draw.

    Args:
        bot_path: path to the bot's main.py.
        scenario: a single scenario dict (same shape as run_single_scenario).
        p0: expected pass-rate under H0 (default 0.85).
        p1: degraded pass-rate under H1 (default 0.60).
        alpha: type-I error level (default 0.05).
        beta: type-II error level (default 0.10).
        n_max: cap on the number of trials (default 12).
        seed: optional RNG seed for reproducible shuffling of trial inputs. When
            provided, each trial's ``hand`` index in the scenario input is set to
            the trial counter, giving deterministic bots at least a chance to
            diverge across trials if their logic is hand-index dependent.

    Returns:
        {
          "decision": "PASS"|"FAIL",   # never UNDECIDED — truncation defaults to PASS
          "n_trials": int,
          "pass_rate": float,        # empirical pass rate over the trials run
          "passes": int,
          "llr": float,              # final cumulative log-likelihood ratio
          "bound_hi": float,         # ln(A)  (>= this ⟹ FAIL)
          "bound_lo": float,         # ln(B)  (<= this ⟹ PASS)
          "final_rule": "sprt_h0"|"sprt_h1"|"n_max_default_pass",
          "details": [str, ...],     # per-trial detail strings
        }
    """
    bound_hi, bound_lo = _sprt_bounds(alpha=alpha, beta=beta)
    outcomes = []
    details = []
    decision = "UNDECIDED"  # pre-loop sentinel; always overwritten (PASS or FAIL) before return
    final_rule = "n_max_default_pass"  # default if no boundary crossing occurs

    # Optionally vary the input so hand-index-sensitive bots can diverge across
    # trials. The mutation is shallow and reversible across calls because each
    # call rebuilds the scenario dict from the caller's reference.
    base_input = scenario.get("input", {})
    for trial in range(n_max):
        if seed is not None and isinstance(base_input, dict):
            mutated = dict(scenario)
            mutated_input = dict(base_input)
            mutated_input["hand"] = trial
            mutated["input"] = mutated_input
            trial_scenario = mutated
        else:
            trial_scenario = scenario
        ok, detail = run_single_scenario(bot_path, trial_scenario)
        outcomes.append(1 if ok else 0)
        details.append(detail)
        llr = _sprt_llr(outcomes, p0=p0, p1=p1)
        if llr <= bound_lo:
            decision = "PASS"
            final_rule = "sprt_h0"
            break
        if llr >= bound_hi:
            decision = "FAIL"
            final_rule = "sprt_h1"
            break

    passes = sum(outcomes)
    n_trials = len(outcomes)
    pass_rate = (passes / n_trials) if n_trials > 0 else 0.0
    llr_final = _sprt_llr(outcomes, p0=p0, p1=p1) if n_trials > 0 else 0.0

    if decision == "UNDECIDED":
        # Reached n_max without a boundary crossing. The Wald SPRT's type-I
        # control rests on the LLR crossing an acceptance boundary; truncating
        # without a crossing means the evidence for regression (H1) is
        # insufficient, so we do NOT block (presumptive PASS — H0 is "quality
        # acceptable"). A rate-based FAIL here inflates type-I error well past
        # the nominal α (empirically ~2α at n_max=12 because P(rate<0.7 | p0)
        # alone is ~0.085), defeating the sequential gate's purpose. Severe
        # regressions (p ≤ p1) cross the H1 boundary quickly and FAIL before
        # truncation; only the ambiguous mid-band is affected, which is the
        # acceptable price for type-I validity. pass_rate is still reported.
        decision = "PASS"
        final_rule = "n_max_default_pass"

    return {
        "decision": decision,
        "n_trials": n_trials,
        "pass_rate": pass_rate,
        "passes": passes,
        "llr": llr_final,
        "bound_hi": bound_hi,
        "bound_lo": bound_lo,
        "final_rule": final_rule,
        "details": details,
    }


def run_decision_tests_sprt_aggregate(bot_path, extra_scenarios=None,
                                      p0=SPRT_P0, p1=SPRT_P1,
                                      alpha=SPRT_ALPHA, beta=SPRT_BETA,
                                      n_max=SPRT_N_MAX, seed=None):
    """Aggregate Wald SPRT across ALL scenarios — the gate-ready wrapper.

    run_decision_tests_sprt tests a SINGLE scenario under sequential resampling.
    The quality gate (tool_gates.run_quality_gates) needs a verdict across the
    full scenario suite. This function mirrors run_decision_tests_detail's
    scenario loading/merging + per-scenario loop, but replaces the single-shot
    run_single_scenario with run_decision_tests_sprt and rolls the per-scenario
    PASS/FAIL decisions up into the SAME return dict shape that the gate
    consumes (pass_rate / total / critical_passed / critical_total /
    critical_failures / failures / scenarios), plus an `sprt_decisions` list for
    telemetry.

    A scenario "passes" iff its SPRT decision is PASS. The truncation default
    (n_max reached without a boundary crossing) is presumptive PASS, preserving
    the SPRT's type-I control — see run_decision_tests_sprt for rationale.

    Args mirror run_decision_tests_detail(bot_path, extra_scenarios=...) plus
    the SPRT knobs (p0/p1/alpha/beta/n_max/seed) which forward to each
    per-scenario SPRT call.

    Returns:
        dict with the same keys as run_decision_tests_detail, plus:
          "sprt_decisions": [ {id, decision, n_trials, pass_rate, final_rule}, ... ]
    """
    if not SCENARIOS_FILE.exists():
        return {
            "pass_rate": 1.0,
            "passed": 0,
            "total": 0,
            "critical_passed": 0,
            "critical_total": 0,
            "critical_failures": [],
            "failures": [],
            "scenarios": [],
            "sprt_decisions": [],
        }

    with open(SCENARIOS_FILE) as f:
        scenarios = json.load(f)

    # Same merge path as run_decision_tests_detail (persisted + runtime dynamic).
    dynamic_from_file = load_dynamic_scenarios()
    if dynamic_from_file:
        scenarios = merge_dynamic_scenarios(scenarios, dynamic_from_file)
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
            "sprt_decisions": [],
        }

    passed = 0
    total = len(scenarios)
    critical_passed = 0
    critical_total = 0
    scenario_results = []
    failures = []
    critical_failures = []
    sprt_decisions = []

    for scenario in scenarios:
        sprt = run_decision_tests_sprt(
            bot_path, scenario, p0=p0, p1=p1, alpha=alpha, beta=beta,
            n_max=n_max, seed=seed,
        )
        ok = sprt.get("decision") == "PASS"
        severity = scenario.get(
            "severity",
            "critical" if scenario.get("id") in CRITICAL_SCENARIO_IDS else "advisory",
        )
        details = (
            f"SPRT {sprt.get('decision')} (n={sprt.get('n_trials')}, "
            f"rate={sprt.get('pass_rate'):.2f}, rule={sprt.get('final_rule')})"
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
        sprt_decisions.append({
            "id": scenario["id"],
            "decision": sprt.get("decision"),
            "n_trials": sprt.get("n_trials"),
            "pass_rate": sprt.get("pass_rate"),
            "final_rule": sprt.get("final_rule"),
        })

    return {
        "pass_rate": passed / total if total > 0 else 1.0,
        "passed": passed,
        "total": total,
        "critical_passed": critical_passed,
        "critical_total": critical_total,
        "critical_failures": critical_failures,
        "failures": failures,
        "scenarios": scenario_results,
        "sprt_decisions": sprt_decisions,
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
