"""
Lineage (Evolution Tree) Management.

Tracks parent-child relationships between bot generations,
enabling branching, backtracking, and crossover decisions.
"""

import json
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
BOTS_DIR = WORKSPACE.parent / "bots"
LINEAGE_FILE = WORKSPACE / "results" / "lineage.json"


def load_lineage():
    """Load the evolution tree from lineage.json."""
    if LINEAGE_FILE.exists():
        with open(LINEAGE_FILE) as f:
            return json.load(f)
    return {}


def save_lineage(lineage):
    """Save the evolution tree."""
    LINEAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LINEAGE_FILE, "w") as f:
        json.dump(lineage, f, indent=2)


def record_birth(child_name, parent_name, strategy_tag=""):
    """Record a new bot's parent and strategy tag."""
    lineage = load_lineage()

    lineage[child_name] = {
        "parent": parent_name,
        "strategy_tag": strategy_tag,
        "children": [],
    }

    # Add child to parent's children list
    if parent_name and parent_name in lineage:
        if child_name not in lineage[parent_name]["children"]:
            lineage[parent_name]["children"].append(child_name)

    save_lineage(lineage)

    # Also write metadata files in the bot directory
    bot_dir = BOTS_DIR / child_name
    if bot_dir.exists():
        if parent_name:
            (bot_dir / "parent.txt").write_text(parent_name)
        if strategy_tag:
            (bot_dir / "strategy_tag.txt").write_text(strategy_tag)


def get_parent(bot_name):
    """Get the parent of a bot, or None if it's a root node."""
    lineage = load_lineage()
    return lineage.get(bot_name, {}).get("parent")


def get_children(bot_name):
    """Get all children of a bot."""
    lineage = load_lineage()
    return lineage.get(bot_name, {}).get("children", [])


def get_ancestors(bot_name):
    """Get the full ancestry chain (parent, grandparent, ...)."""
    lineage = load_lineage()
    ancestors = []
    current = bot_name
    while current and current in lineage:
        parent = lineage[current].get("parent")
        if parent:
            ancestors.append(parent)
        current = parent
    return ancestors


def get_strategy_tag(bot_name):
    """Get the strategy tag of a bot."""
    lineage = load_lineage()
    return lineage.get(bot_name, {}).get("strategy_tag", "")


def get_stagnation_count(bot_name, ratings):
    """Count consecutive generations without improvement from parent.

    Returns the number of consecutive ancestors whose rating did not
    exceed their parent's rating.
    """
    from glicko2 import Glicko2Player

    lineage = load_lineage()
    count = 0
    current = bot_name

    while current and current in lineage:
        parent = lineage[current].get("parent")
        if not parent:
            break

        current_rating = ratings.get(current, Glicko2Player()).r
        parent_rating = ratings.get(parent, Glicko2Player()).r

        if current_rating <= parent_rating:
            count += 1
            current = parent
        else:
            break

    return count


def find_best_branch_source(active_bots, ratings, current_bot, min_gap=2):
    """Find the best bot to branch from when stagnation is detected.

    Returns the bot name to branch from, or None if no better option exists.
    Prefers the highest-rated bot that is NOT an ancestor of the current line.
    """
    from glicko2 import Glicko2Player

    ancestors = set(get_ancestors(current_bot))
    ancestors.add(current_bot)

    candidates = [b for b in active_bots if b not in ancestors]
    if not candidates:
        # All bots are ancestors; just pick the highest-rated
        candidates = active_bots

    candidates.sort(key=lambda b: ratings.get(b, Glicko2Player()).r, reverse=True)

    best = candidates[0] if candidates else None
    current_rating = ratings.get(current_bot, Glicko2Player()).r
    best_rating = ratings.get(best, Glicko2Player()).r if best else 0

    # Only suggest branching if the best candidate is meaningfully better
    if best and best_rating > current_rating + min_gap * 5:
        return best
    # Or if we're stagnant, suggest branching from the best regardless
    if best and best != current_bot:
        return best

    return None
