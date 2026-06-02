"""Shared helpers for MCP tool implementations.

UI injection, logging adapters, checkpoint gates, and validation utilities.
"""

import difflib
import fcntl
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("pok.tools")
from typing import Annotated

from evolution_core import (
    BaseUI,
    get_active_bots,
    get_bot_dir,
    load_ratings,
    write_pipeline_checkpoint,
    read_pipeline_checkpoint,
)
from evolution_infra import _target_rel
from glicko2 import Glicko2Player

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ──────────────────────────────────────────────
# UI Injection — Dashboard Integration
# ──────────────────────────────────────────────

_injected_ui = None


def inject_ui(ui):
    """Inject a real WebUI instance so tool events broadcast to Dashboard via SSE."""
    global _injected_ui
    _injected_ui = ui


def _get_ui():
    """Get UI instance: injected WebUI (Dashboard mode) or silent ToolUI (CLI mode)."""
    return _injected_ui if _injected_ui else ToolUI()


# ──────────────────────────────────────────────
# Logging UI Adapter (CLI fallback)
# ──────────────────────────────────────────────

class ToolUI(BaseUI):
    """Silent UI adapter for CLI mode — captures output for tool results only."""

    def __init__(self):
        self.messages = []
        self.costs = []

    def log_history(self, msg, status="info"):
        self.messages.append(f"[{status}] {msg}")

    def set_status(self, msg, is_working=False):
        self.messages.append(f"[status] {msg}")

    def log_io(self, msg, stream_type="default", role=""):
        pass

    def clear_io(self):
        pass

    def update_eval_table(self, ratings, active_bots):
        pass

    def update_daemon_status(self, stats, ratings):
        pass

    def set_header(self, msg):
        pass

    def update_cost(self, role, cost_usd, usage):
        if cost_usd is not None:
            self.costs.append({"role": role, "cost_usd": cost_usd})

    def update_metrics(self, metrics):
        pass

    def get_output(self):
        return "\n".join(self.messages[-20:])


# ──────────────────────────────────────────────
# Common Helpers
# ──────────────────────────────────────────────

def _ratings_summary(ratings, n=10):
    """Get top N bots as a compact summary, sorted by H2H avg win rate."""
    h2h_winrates = load_h2h_avg_winrates()
    sorted_bots = sorted(
        [(name, p) for name, p in ratings.items()],
        key=lambda x: h2h_winrates.get(x[0], 0.0), reverse=True,
    )[:n]
    return [
        {
            "name": name,
            "r": round(p.r, 1),
            "rd": round(p.rd, 1),
            "h2h_avg_wr": round(h2h_winrates.get(name, 0.0), 4),
        }
        for name, p in sorted_bots
    ]


def _json_tool_result(data):
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}


def _read_json(path, default):
    from evolution_infra import locked_file
    try:
        if not Path(path).exists():
            return default
        with locked_file(path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        log.warning("_read_json: corrupt JSON in %s, returning default", path)
        return default
    except Exception:
        return default


def _matching_checkpoint(version, source_v=None):
    ckpt = read_pipeline_checkpoint()
    if not ckpt or ckpt.get("next_v") != version:
        return None
    if source_v is not None and ckpt.get("source_v") != source_v:
        return None
    return ckpt


def _record_gate(version, source_v, gate_name, gate_data, stage=None,
                 master_plan=None, reviewer_feedback=None):
    ckpt = _matching_checkpoint(version, source_v)
    if not ckpt:
        log.warning("_record_gate: no matching checkpoint for v%s/v%s, gate '%s' dropped", version, source_v, gate_name)
        return False
    current_stage = ckpt.get("stage", "")
    # Preserve previous critic result when overwriting with a new one
    if gate_name == "critic":
        existing_critic = ckpt.get("gate_results", {}).get("critic")
        if existing_critic and existing_critic.get("score", 0) > 0:
            gate_data = {**gate_data, "prev_critic": existing_critic}
    write_pipeline_checkpoint(
        version,
        source_v,
        stage or current_stage,
        master_plan=master_plan if master_plan is not None else ckpt.get("master_plan"),
        reviewer_feedback=(
            reviewer_feedback
            if reviewer_feedback is not None
            else ckpt.get("reviewer_feedback", "")
        ),
        generation_attempt=ckpt.get("generation_attempt", 0),
        gate_results={gate_name: gate_data},
    )
    return True


def _gate_payload(version, source_v, passed, **extra):
    return {
        "version": version,
        "source_v": source_v,
        "passed": bool(passed),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **extra,
    }


def _state_blocked(message, version, source_v=None, checkpoint=None):
    return _json_tool_result({
        "error": f"STATE BLOCKED: {message}",
        "version": version,
        "source_v": source_v,
        "checkpoint_stage": checkpoint.get("stage") if checkpoint else None,
        "gate_results": checkpoint.get("gate_results", {}) if checkpoint else {},
    })


def _checkpoint_gate(checkpoint, gate_name):
    if not checkpoint:
        return {}
    return (checkpoint.get("gate_results", {}) or {}).get(gate_name, {}) or {}


def _quality_gate_ok(checkpoint):
    quality = _checkpoint_gate(checkpoint, "quality")
    return quality.get("all_passed") is True and quality.get("critical_scenarios_passed") is True


def _review_gate_ok(checkpoint):
    return _checkpoint_gate(checkpoint, "review").get("approved") is True


def _critic_gate_ok(checkpoint):
    critic = _checkpoint_gate(checkpoint, "critic")
    try:
        score = float(critic.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    return critic.get("approved") is True and score >= 6


def _bot_main(bot_name):
    v_str = bot_name.replace("claude_v", "")
    try:
        v = int(v_str)
    except ValueError:
        return PROJECT_ROOT / "bots" / bot_name / "main.py"
    return get_bot_dir(v) / "main.py"


def _load_h2h_data():
    return _read_json(PROJECT_ROOT / "web" / "core" / "results" / "head_to_head.json", {})


def _h2h_stats(bot_name, opponent, h2h):
    for key, value in h2h.items():
        parts = key.split(" vs ")
        if len(parts) != 2 or bot_name not in parts or opponent not in parts:
            continue
        a, b = parts
        games = value.get("games", 0)
        if games <= 0:
            return None
        bot_wins = value.get("a_wins", 0) if bot_name == a else value.get("b_wins", 0)
        opp_wins = value.get("b_wins", 0) if bot_name == a else value.get("a_wins", 0)
        return {
            "wins": bot_wins,
            "losses": opp_wins,
            "games": games,
            "win_rate": bot_wins / games,
        }
    return None


def compute_h2h_avg_winrate(bot_name, h2h_data):
    """Equal-weighted average win rate across all H2H opponents."""
    opponent_rates = []
    for key, value in h2h_data.items():
        parts = key.split(" vs ")
        if len(parts) != 2 or bot_name not in parts:
            continue
        games = value.get("games", 0)
        if games <= 0:
            continue
        bot_wins = value.get("a_wins", 0) if parts[0] == bot_name else value.get("b_wins", 0)
        opponent_rates.append(bot_wins / games)
    if not opponent_rates:
        return None
    return sum(opponent_rates) / len(opponent_rates)


def compute_opponent_coverage(bot_name, h2h_data, active_bots):
    """Fraction of active opponents with H2H data (games > 0)."""
    opponents_with_data = 0
    total = 0
    for other in active_bots:
        if other == bot_name:
            continue
        total += 1
        for key, value in h2h_data.items():
            parts = key.split(" vs ")
            if len(parts) == 2 and bot_name in parts and other in parts and value.get("games", 0) > 0:
                opponents_with_data += 1
                break
    return opponents_with_data / total if total > 0 else 1.0


def _batch_compute_h2h_winrates(h2h_data, active_bots):
    """Compute H2H avg win rates for all active bots in a single pass over h2h_data.

    Returns dict mapping bot_name -> list of per-opponent win rates (for averaging).
    """
    bot_rates = {name: [] for name in active_bots}
    for key, value in h2h_data.items():
        parts = key.split(" vs ")
        if len(parts) != 2:
            continue
        a, b = parts
        games = value.get("games", 0)
        if games <= 0:
            continue
        if a in bot_rates:
            bot_rates[a].append(value.get("a_wins", 0) / games)
        if b in bot_rates:
            bot_rates[b].append(value.get("b_wins", 0) / games)
    return bot_rates


def load_h2h_avg_winrates():
    """Load H2H avg win rates for all bots. Falls back to bot_stats then Glicko r.

    Returns dict mapping bot_name -> float (average win rate across H2H opponents).
    """
    h2h_data = _load_h2h_data()
    bot_stats_data = _read_json(PROJECT_ROOT / "web" / "core" / "results" / "bot_stats.json", {})
    ratings = load_ratings()

    active = set(get_active_bots())
    bot_rates = _batch_compute_h2h_winrates(h2h_data, active)

    result = {}
    for bot_name in active:
        rates = bot_rates.get(bot_name, [])
        if rates:
            result[bot_name] = sum(rates) / len(rates)
        else:
            bs = bot_stats_data.get(bot_name, {})
            if bs.get("games", 0) > 0:
                result[bot_name] = bs.get("win_rate", 0.5)
            else:
                p = ratings.get(bot_name)
                if p:
                    result[bot_name] = max(0.0, min(1.0, 0.5 + (p.r - 1500) / 1000.0))
                else:
                    result[bot_name] = 0.5
    return result


def _batch_compute_opponent_coverage(h2h_data, active_bots):
    """Compute opponent coverage for all active bots in a single pass."""
    active_set = set(active_bots)
    opponent_counts = {name: 0 for name in active_set}
    for key, value in h2h_data.items():
        parts = key.split(" vs ")
        if len(parts) != 2:
            continue
        a, b = parts
        if value.get("games", 0) > 0:
            if a in active_set and b in active_set:
                opponent_counts[a] += 1
                opponent_counts[b] += 1
    return opponent_counts


def load_h2h_avg_winrates_with_coverage():
    """Like load_h2h_avg_winrates but returns coverage metadata per bot."""
    h2h_data = _load_h2h_data()

    active = set(get_active_bots())
    active_list = list(active)

    wrs = load_h2h_avg_winrates()
    opponent_counts = _batch_compute_opponent_coverage(h2h_data, active_list)
    n_total = len(active_list) - 1

    result = {}
    for bot_name in active:
        n_eval = opponent_counts.get(bot_name, 0)
        cov = n_eval / n_total if n_total > 0 else 1.0
        result[bot_name] = {
            "h2h_avg_wr": wrs.get(bot_name, 0.5),
            "opponent_coverage": cov,
            "opponents_evaluated": n_eval,
            "opponents_total": n_total,
        }
    return result


def _select_precommit_opponents(version, source_v, max_top=3, max_weak=2):
    candidate = f"claude_v{version}"
    parent = f"claude_v{source_v}"
    active = [b for b in get_active_bots() if b != candidate and _bot_main(b).exists()]
    ratings = load_ratings()
    h2h = _load_h2h_data()

    selected = []
    reasons = {}

    def add(name, reason):
        if name == candidate or name in selected or not _bot_main(name).exists():
            return
        selected.append(name)
        reasons[name] = reason

    add(parent, "parent")

    h2h_winrates = load_h2h_avg_winrates()
    top = sorted(
        active,
        key=lambda name: h2h_winrates.get(name, 0.0),
        reverse=True,
    )
    for name in top[:max_top]:
        add(name, "top_h2h_wr")

    source_name = parent
    weak = []
    for opp in active:
        stats = _h2h_stats(source_name, opp, h2h)
        if stats and stats["win_rate"] < 0.40:
            weak.append((stats["win_rate"], opp))
    for _, name in sorted(weak)[:max_weak]:
        add(name, "source_h2h_weakness")

    return [{"name": name, "reason": reasons[name]} for name in selected]




def _py_files_changed_between(source_dir, next_dir):
    rels = set()
    for base in (source_dir, next_dir):
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rels.add(path.relative_to(base).as_posix())

    changed = []
    for rel in sorted(rels):
        src = source_dir / rel
        dst = next_dir / rel
        src_text = src.read_text() if src.exists() else ""
        dst_text = dst.read_text() if dst.exists() else ""
        if src_text != dst_text:
            changed.append(rel)
    return changed


_NUMERIC_LITERAL_RE = re.compile(
    r"(?<![A-Za-z_])[-+]?(?:\d+\.\d+|\d+|\.\d+)(?:[eE][-+]?\d+)?"
)


def _numbers_only_changed(before, after):
    return _NUMERIC_LITERAL_RE.sub("<NUM>", before) == _NUMERIC_LITERAL_RE.sub("<NUM>", after)


def _validate_worker_boundaries(tasks, source_v, next_v):
    source_dir = get_bot_dir(source_v)
    next_dir = get_bot_dir(next_v)
    all_targets = set()
    errors = []

    for task in tasks:
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if rel:
                all_targets.add(rel)

    changed_files = _py_files_changed_between(source_dir, next_dir)
    for rel in changed_files:
        if rel not in all_targets:
            errors.append({
                "type": "target_file_violation",
                "file": rel,
                "message": "Worker modified a Python file outside declared target_files.",
            })

    # Check for new files created outside target_files
    if source_dir.exists() and next_dir.exists():
        source_files = {p.relative_to(source_dir).as_posix() for p in source_dir.rglob("*.py")}
        next_files = {p.relative_to(next_dir).as_posix() for p in next_dir.rglob("*.py")}
        new_files = next_files - source_files
        for rel in new_files:
            if rel not in all_targets:
                errors.append({
                    "type": "new_file_violation",
                    "file": rel,
                    "message": "Worker created a new file outside declared target_files.",
                })

    for task in tasks:
        role = str(task.get("role", ""))
        if "Hyperparameter Tuner" not in role:
            continue
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if not rel:
                continue
            src = source_dir / rel
            dst = next_dir / rel
            before = src.read_text() if src.exists() else ""
            after = dst.read_text() if dst.exists() else ""
            if before != after and not _numbers_only_changed(before, after):
                diff = "\n".join(difflib.unified_diff(
                    before.splitlines(),
                    after.splitlines(),
                    fromfile=f"v{source_v}/{rel}",
                    tofile=f"v{next_v}/{rel}",
                    lineterm="",
                ))
                errors.append({
                    "type": "hyperparameter_boundary_violation",
                    "file": rel,
                    "message": "Hyperparameter Tuner changed non-numeric text or structure.",
                    "diff_excerpt": diff[:1200],
                })

    return errors
