"""Shared helpers for MCP tool implementations.

UI injection, logging adapters, checkpoint gates, and validation utilities.
"""

import difflib
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("pok.tools")

from bot_namespace import bot_name as active_bot_name, parse_bot_version
from evolution_core import (
    BaseUI,
    get_active_bots,
    get_bot_dir,
    load_ratings,
    write_pipeline_checkpoint,
    read_pipeline_checkpoint,
)
from evolution_infra import _target_rel, read_locked_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ──────────────────────────────────────────────
# Phase 3: FAMOU nemesis slot (advisory probe opponent)
# ──────────────────────────────────────────────
# When True, _select_precommit_opponents appends a single "nemesis_probe"
# opponent (the parent's worst H2H matchup among active bots, win_rate < 0.40
# with games >= PRECOMMIT_NEMESIS_MIN_GAMES) to the parent/top/weak list. The
# nemesis matchup is run for TELEMETRY ONLY: run_precommit_eval excludes it
# from both the blockers list and the aggregate-net-chips regression gate
# (tool_eval.py checks reason == "nemesis_probe" at both accumulate points).
# So flipping this flag ON cannot change the commit verdict — it only adds an
# observation about a probable inherited weakness. Default ON because it is
# non-blocking.
PRECOMMIT_NEMESIS_SLOT = True
PRECOMMIT_NEMESIS_MIN_GAMES = 15         # raised from 4: filter low-sample h2h noise
PRECOMMIT_NEMESIS_WINRATE_THRESHOLD = 0.40  # only probe a real weakness


def _find_nemesis_opponent(subject_name, active_list, h2h, min_games=PRECOMMIT_NEMESIS_MIN_GAMES,
                           exclude=None):
    """Return (opponent_name, win_rate) of subject_name's toughest active opponent.

    Scans the on-disk head_to_head for the subject's lowest win-rate opponent
    among `active_list` with at least `min_games` played. Used by the nemesis
    probe to surface an inherited weakness: the candidate (just committed) has
    no h2h yet, so we probe the PARENT's nemesis instead (weakness inheritance
    is the FAMOU co-evolution pressure we want to measure).

    `exclude` (optional set) skips opponents already chosen for other slots
    (parent/top/weak). This keeps the nemesis a DISTINCT opponent from the
    blocking weak-slot pick — the weak slot already covers the single worst
    matchup as a regression gate, so the nemesis probe adds value by surfacing
    the NEXT-worst opponent as telemetry.

    Returns None when no qualifying opponent exists (all remaining opponents
    above the noise floor, insufficient games, or every candidate excluded).
    """
    excluded = exclude or set()
    best = None  # (win_rate, opp)
    for opp in active_list:
        if opp == subject_name or opp in excluded:
            continue
        stats = _h2h_stats(subject_name, opp, h2h)
        if not stats or stats["games"] < min_games:
            continue
        wr = stats["win_rate"]
        if best is None or wr < best[0]:
            best = (wr, opp)
    if best is None:
        return None
    return (best[1], best[0])


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


def _set_pipeline_status(msg, is_working=True):
    """Update WebUI status message for pipeline stage visibility."""
    _get_ui().set_status(msg, is_working=is_working)


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
    """Get top N bots as a compact summary, sorted by unified strength."""
    strength_scores = load_strength_scores()
    h2h_winrates = load_h2h_avg_winrates()
    sorted_bots = sorted(
        [(name, p) for name, p in ratings.items()],
        key=lambda x: strength_scores.get(x[0], 0.0), reverse=True,
    )[:n]
    return [
        {
            "name": name,
            "r": round(p.r, 1),
            "rd": round(p.rd, 1),
            "leaderboard_score": round(strength_scores.get(name, 0.0), 4),
            "h2h_avg_wr": round(h2h_winrates.get(name, 0.0), 4),
        }
        for name, p in sorted_bots
    ]


def _json_tool_result(data):
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}


def _read_json(path, default):
    result = read_locked_json(path, default)
    if result is default and Path(path).exists():
        log.warning("_read_json: corrupt JSON in %s, returning default", path)
    return result


def _resolve_version_args(args):
    """Get version/source_v from args, falling back to active pipeline checkpoint.

    Prevents KeyError death spiral when the orchestrator LLM calls a tool
    without providing version/source_v parameters.
    """
    v = args.get("version") or args.get("next_v")
    source_v = args.get("source_v")
    if v is None or source_v is None:
        ckpt = read_pipeline_checkpoint()
        if ckpt:
            v = v or ckpt.get("next_v")
            source_v = source_v or ckpt.get("source_v")
    return v, source_v


def _matching_checkpoint(version, source_v=None):
    ckpt = read_pipeline_checkpoint()
    if not ckpt or ckpt.get("next_v") != version:
        return None
    if source_v is not None and ckpt.get("source_v") != source_v:
        return None
    return ckpt


def _record_gate(version, source_v, gate_name, gate_data, stage=None,
                 master_plan=None, reviewer_feedback=None, generation_attempt=None):
    ckpt = _matching_checkpoint(version, source_v)
    if not ckpt:
        log.warning("_record_gate: no matching checkpoint for v%s/v%s, gate '%s' dropped", version, source_v, gate_name)
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.gate_record_dropped", "error",
                f"Gate '{gate_name}' dropped because no matching checkpoint exists for v{version}/source v{source_v}",
                {"version": version, "source_v": source_v, "gate": gate_name},
            )
        except Exception:
            pass
        return False
    current_stage = ckpt.get("stage", "")
    # Preserve previous critic result when overwriting with a new one
    if gate_name == "critic":
        existing_critic = ckpt.get("gate_results", {}).get("critic")
        if existing_critic and existing_critic.get("score", 0) > 0:
            gate_data = {**gate_data, "prev_critic": existing_critic}
    # Use provided generation_attempt or preserve existing
    if generation_attempt is None:
        generation_attempt = ckpt.get("generation_attempt", 0)
    recorded = write_pipeline_checkpoint(
        version,
        source_v,
        stage or current_stage,
        master_plan=master_plan if master_plan is not None else ckpt.get("master_plan"),
        reviewer_feedback=(
            reviewer_feedback
            if reviewer_feedback is not None
            else ckpt.get("reviewer_feedback", "")
        ),
        generation_attempt=generation_attempt,
        gate_results={gate_name: gate_data},
        direction_audit=ckpt.get("direction_audit"),
    )
    if not recorded:
        log.warning(
            "_record_gate: checkpoint rejected gate '%s' for v%s/v%s at stage '%s'",
            gate_name, version, source_v, stage or current_stage,
        )
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.gate_record_rejected",
                "error",
                f"Gate '{gate_name}' was rejected by checkpoint state machine for v{version}/source v{source_v}",
                {
                    "version": version,
                    "source_v": source_v,
                    "gate": gate_name,
                    "requested_stage": stage or current_stage,
                    "current_stage": current_stage,
                },
            )
        except Exception:
            pass
    return bool(recorded)


def _gate_payload(version, source_v, passed, **extra):
    return {
        "version": version,
        "source_v": source_v,
        "passed": bool(passed),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **extra,
    }


def _state_blocked(message, version, source_v=None, checkpoint=None):
    # Compact gate summary instead of full gate_results (saves ~600+ tokens)
    gate_summary = {}
    if checkpoint:
        for name, gate in (checkpoint.get("gate_results") or {}).items():
            gate_summary[name] = {"passed": gate.get("passed")}
            if gate.get("score") is not None:
                gate_summary[name]["score"] = gate.get("score")
    return _json_tool_result({
        "error": f"STATE BLOCKED: {message}",
        "version": version,
        "source_v": source_v,
        "checkpoint_stage": checkpoint.get("stage") if checkpoint else None,
        "gate_summary": gate_summary,
    })


def _checkpoint_gate(checkpoint, gate_name):
    if not checkpoint:
        return {}
    return (checkpoint.get("gate_results", {}) or {}).get(gate_name, {}) or {}


def _active_workflow_profile_info():
    try:
        from workflow_profiles import get_workflow_profile
        profile = get_workflow_profile()
        return (
            getattr(profile, "profile_id", ""),
            getattr(profile, "national_execution_mode", "adapter"),
        )
    except Exception:
        return "", "adapter"


def _gate_matches_active_workflow(checkpoint, gate):
    """Reject cached gate results produced under another workflow profile.

    Profile drift is especially dangerous for the national-native migration:
    a candidate that passed the old adapter-backed national gate must not be
    allowed to proceed as if it had passed the direct TCP native gate.
    """
    active_profile_id, active_execution_mode = _active_workflow_profile_info()
    if not active_profile_id:
        return True

    checkpoint_profile_id = str((checkpoint or {}).get("workflow_profile_id") or "")
    if checkpoint_profile_id and checkpoint_profile_id != active_profile_id:
        return False

    gate_profile_id = str(gate.get("workflow_profile_id") or gate.get("profile_id") or "")
    if gate_profile_id and gate_profile_id != active_profile_id:
        return False
    if not gate_profile_id and active_profile_id != "default":
        return False

    gate_execution_mode = str(gate.get("national_execution_mode") or "")
    if active_execution_mode == "native_tcp":
        return (
            gate_execution_mode == "native_tcp"
            and gate.get("national_native_contract_ok") is True
        )
    if gate_execution_mode and gate_execution_mode != active_execution_mode:
        return False
    return True


def _quality_gate_ok(checkpoint):
    quality = _checkpoint_gate(checkpoint, "quality")
    return (
        quality.get("all_passed") is True
        and quality.get("critical_scenarios_passed") is True
        and _gate_matches_active_workflow(checkpoint, quality)
    )


def _review_gate_ok(checkpoint):
    return _checkpoint_gate(checkpoint, "review").get("approved") is True


def _critic_gate_ok(checkpoint):
    critic = _checkpoint_gate(checkpoint, "critic")
    if critic.get("approved") is not True:
        return False
    if critic.get("raw_approved") is False or critic.get("advisory_approved") is False:
        return False
    score = critic.get("score", critic.get("advisory_score"))
    if score is None:
        return True
    try:
        return float(score) >= 6.0
    except (TypeError, ValueError):
        return False


def _bot_main(bot_name):
    version = parse_bot_version(str(bot_name))
    if version is None:
        return PROJECT_ROOT / "bots" / str(bot_name) / "main.py"
    return get_bot_dir(version) / "main.py"


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
        draws = value.get("draws", 0)
        return {
            "wins": bot_wins,
            "losses": opp_wins,
            "draws": draws,
            "games": games,
            "win_rate": (bot_wins + 0.5 * draws) / games,
        }
    return None


def compute_h2h_avg_winrate(bot_name, h2h_data):
    """Equal-weighted average win rate across all H2H opponents.

    Draws count as half a win, matching the Glicko update semantics.
    """
    from rating_snapshot import h2h_winrate_for_bot
    return h2h_winrate_for_bot(bot_name, h2h_data)


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
            bot_rates[a].append((value.get("a_wins", 0) + 0.5 * value.get("draws", 0)) / games)
        if b in bot_rates:
            bot_rates[b].append((value.get("b_wins", 0) + 0.5 * value.get("draws", 0)) / games)
    return bot_rates


def _match_history_file():
    import evolution_infra
    return evolution_infra.MATCH_HISTORY_FILE


def _rating_rows_for_active():
    from rating_snapshot import build_strength_rows
    h2h_data = _load_h2h_data()
    bot_stats_data = _read_json(PROJECT_ROOT / "web" / "core" / "results" / "bot_stats.json", {})
    ratings = load_ratings()
    active = list(get_active_bots())
    return build_strength_rows(
        ratings,
        bot_stats_data,
        h2h_data,
        active_bots=active,
        match_history_path=_match_history_file(),
    )


def load_strength_scores():
    """Load unified leaderboard strength scores for active bots."""
    rows = _rating_rows_for_active()
    if rows:
        return {row["name"]: row["leaderboard_score"] for row in rows}
    return {name: 0.5 for name in get_active_bots()}


def load_selection_scores():
    """Load confidence-discounted scores for evolution mechanics."""
    rows = _rating_rows_for_active()
    if rows:
        return {
            row["name"]: row.get("selection_score", row.get("leaderboard_score", 0.5))
            for row in rows
        }
    return {name: 0.5 for name in get_active_bots()}


def load_h2h_avg_winrates():
    """Load H2H avg win rates for all active bots from the unified snapshot.

    Returns dict mapping bot_name -> float (average win rate across H2H opponents).
    """
    rows = _rating_rows_for_active()
    result = {}
    for row in rows:
        bot_name = row["name"]
        if row.get("h2h_avg_wr") is not None:
            result[bot_name] = row["h2h_avg_wr"]
        else:
            result[bot_name] = row.get("win_rate") if row.get("win_rate") is not None else row.get("leaderboard_score", 0.5)
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
    rows = _rating_rows_for_active()
    result = {}
    for row in rows:
        bot_name = row["name"]
        result[bot_name] = {
            "h2h_avg_wr": row.get("h2h_avg_wr", 0.5),
            "leaderboard_score": row.get("leaderboard_score", 0.5),
            "selection_score": row.get("selection_score", row.get("leaderboard_score", 0.5)),
            "selection_penalty": row.get("selection_penalty", 0.0),
            "rank_basis": row.get("rank_basis", ""),
            "strength_confidence": row.get("strength_confidence", "low"),
            "strength_note": row.get("strength_note", ""),
            "h2h_source": row.get("h2h_source", "head_to_head"),
            "opponent_coverage": row.get("h2h_coverage", 0.0),
            "opponents_evaluated": row.get("h2h_opponents", 0),
            "opponents_total": row.get("h2h_opponents_total", 0),
            "h2h_games": row.get("h2h_games", 0),
        }
    return result


def _select_precommit_opponents(version, source_v, max_top=2, max_weak=1):
    """Select opponents for precommit eval. Default: 1 parent + 2 top + 1 weak = 4 opponents max.

    With mirror_battle taking ~10-15 min per opponent and a 3600s cycle timeout,
    4 opponents ≈ 40-60 min which fits within the limit.
    """
    candidate = active_bot_name(version)
    parent = active_bot_name(source_v)
    active = [b for b in get_active_bots() if b != candidate and _bot_main(b).exists()]
    ratings = load_ratings()
    h2h = _load_h2h_data()
    try:
        from rating_snapshot import choose_h2h_source
        h2h_selection = choose_h2h_source(active, h2h, _match_history_file())
        h2h = h2h_selection["h2h"]
    except Exception:
        h2h_selection = {"source": "head_to_head"}

    selected = []
    reasons = {}

    def add(name, reason):
        if name == candidate or name in selected or not _bot_main(name).exists():
            return
        selected.append(name)
        reasons[name] = reason

    add(parent, "parent")

    strength_scores = load_strength_scores()
    selection_scores = load_selection_scores()
    top = sorted(
        active,
        key=lambda name: selection_scores.get(name, strength_scores.get(name, 0.0)),
        reverse=True,
    )
    for name in top[:max_top]:
        add(name, "top_strength")

    source_name = parent
    weak = []
    for opp in active:
        stats = _h2h_stats(source_name, opp, h2h)
        if stats and stats["win_rate"] < 0.40:
            weak.append((stats["win_rate"], opp))
    for _, name in sorted(weak)[:max_weak]:
        add(name, "source_h2h_weakness")

    # ── Phase 3: FAMOU nemesis probe (advisory, non-blocking) ──
    # Append ONE opponent that most reliably beats the parent (the candidate's
    # likely inherited weakness). The matchup is tagged reason="nemesis_probe"
    # and run_precommit_eval treats that reason as telemetry-only: it is
    # excluded from the blockers list and from aggregate_net_chips, so a nemesis
    # loss cannot trip the commit gate (see tool_eval.py). Subject = parent
    # (not the candidate) because the candidate has no h2h history yet.
    # Fallback signal: if the live h2h scan finds no qualifying nemesis, consult
    # the nemesis_archive.json snapshot (written on commit_bot) for the parent's
    # recorded nemesis. Live h2h wins on freshness.
    if PRECOMMIT_NEMESIS_SLOT and len(selected) >= 2:
        # Exclude already-selected opponents (parent/top/weak) so the nemesis is
        # a DISTINCT matchup — the weak slot already gates the single worst one;
        # the nemesis adds the NEXT-worst opponent as non-blocking telemetry.
        nemesis = _find_nemesis_opponent(parent, active, h2h, exclude=set(selected))
        if nemesis is None:
            # Archive fallback.
            try:
                archive = _read_json(
                    PROJECT_ROOT / "web" / "core" / "results" / "nemesis_archive.json",
                    {},
                )
                rec = (archive.get("nemesis_of") or {}).get(parent)
                if rec and rec.get("nemesis") and rec["nemesis"] not in selected:
                    nemesis = (rec["nemesis"], float(rec.get("win_rate", 1.0)))
            except Exception:
                nemesis = None
        if nemesis is not None:
            nemesis_opp, nemesis_wr = nemesis
            # Only probe when the signal is a genuine weakness (below the same
            # threshold the weak-slot uses) and the opponent is not already
            # selected (add() dedups, but we check wr so a wr>=0.40 archive
            # entry from a stale snapshot does not inject a non-weakness probe).
            if nemesis_wr < PRECOMMIT_NEMESIS_WINRATE_THRESHOLD:
                add(nemesis_opp, "nemesis_probe")

    try:
        from system_log import log_system_event
        coverage = load_h2h_avg_winrates_with_coverage()
        details = []
        for name in selected:
            cov = coverage.get(name, {})
            pair_stats = _h2h_stats(parent, name, h2h) if name != parent else None
            details.append({
                "name": name,
                "reason": reasons.get(name),
                "leaderboard_score": round(strength_scores.get(name, 0.0), 4),
                "selection_score": round(selection_scores.get(name, strength_scores.get(name, 0.0)), 4),
                "h2h_avg_wr": round(cov.get("h2h_avg_wr", 0.0), 4),
                "h2h_coverage": round(cov.get("opponent_coverage", 0.0), 4),
                "h2h_games": cov.get("h2h_games", 0),
                "strength_confidence": cov.get("strength_confidence", "low"),
                "h2h_source": cov.get("h2h_source", h2h_selection.get("source", "head_to_head")),
                "pair_vs_parent": pair_stats,
            })
        log_system_event(
            "pipeline.precommit_opponents_selected",
            "info",
            f"Selected {len(selected)} precommit opponents for {candidate}",
            {
                "candidate": candidate,
                "parent": parent,
                "h2h_source": h2h_selection.get("source", "head_to_head"),
                "opponents": details,
            },
        )
    except Exception:
        pass

    return [{"name": name, "reason": reasons[name]} for name in selected]




def _py_files_changed_between(source_dir, next_dir):
    if not next_dir.exists():
        return []
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


def normalize_worker_role(role):
    """Normalize a worker role string into a canonical category.

    Returns one of 'architect', 'tuner', 'other'. Case-insensitive substring
    matching so that all Tuner variants ('Tuner', 'HP Tuner', 'Hyperparameter
    Tuner', etc.) collapse to 'tuner', matching the planning-layer logic in
    tool_planning._validate_master_plan. Tuner is checked before Architect so
    that a mixed role string (e.g. 'Hyperparameter Tuner (Architect-assisted)')
    resolves to the stricter 'tuner' boundary rather than escaping it. Unknown/
    empty roles resolve to 'other' without raising, so callers can treat any
    LLM-emitted role safely.
    """
    role = str(role or "").lower()
    if "tuner" in role or "hyperparameter" in role or role == "hp tuner":
        return "tuner"
    if "architect" in role:
        return "architect"
    return "other"


def _validate_worker_boundaries(tasks, source_v, next_v, worker_snapshots=None):
    """Validate that workers respected their role boundaries.

    Args:
        tasks: List of worker task dicts with role, target_files, etc.
        source_v: Source bot version number.
        next_v: Target bot version number.
        worker_snapshots: Optional dict mapping (task_idx, file_rel) -> file content
            before that worker ran. Enables accurate per-worker boundary checking
            when multiple workers share a target file.
    """
    source_dir = get_bot_dir(source_v)
    next_dir = get_bot_dir(next_v)
    all_targets = set()
    errors = []

    for task in tasks:
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if rel:
                all_targets.add(rel)

    # Without per-worker snapshots, fall back to the historical whole-candidate
    # source diff. With snapshots, per-worker boundary checks have already
    # isolated each worker's actual writes. Re-running the whole-candidate diff
    # during in-place quality repair would falsely blame earlier sibling repair
    # edits on the current one.
    if not worker_snapshots:
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

    for task_idx, task in enumerate(tasks):
        role = str(task.get("role", ""))
        if normalize_worker_role(role) != "tuner":
            continue
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if not rel:
                continue
            # Use worker snapshot if available: this compares the file state BEFORE
            # this worker ran vs AFTER, isolating this worker's changes from those
            # of preceding workers (who may have modified the same shared file).
            # Falls back to source version for backward compatibility.
            if worker_snapshots and (task_idx, rel) in worker_snapshots:
                before = worker_snapshots[(task_idx, rel)]
            else:
                src = source_dir / rel
                before = src.read_text() if src.exists() else ""
            dst = next_dir / rel
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
