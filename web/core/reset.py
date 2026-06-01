"""Reset evolution state to baseline (v1-v6 only)."""

import json
import os
import shutil
import glob
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
RESULTS_DIR = CORE_DIR / "results"
BOTS_DIR = PROJECT_ROOT / "bots"
EXPERIENCE_FILE = CORE_DIR / "experience_pool.md"
LOGS_DIR = CORE_DIR.parent / "logs"

RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
WORKER_FAILURES_FILE = RESULTS_DIR / "worker_failures.jsonl"
PIPELINE_STATE_FILE = RESULTS_DIR / "pipeline_state.json"
ORCHESTRATOR_SESSION_FILE = RESULTS_DIR / "orchestrator_session.json"
RATING_HISTORY_FILE = RESULTS_DIR / "rating_history.jsonl"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"
MATCH_REPLAY_DIR = RESULTS_DIR / "match_replay"
COMMENTARY_DIR = RESULTS_DIR / "commentary"

EXPERIENCE_TEMPLATE = """\
# Evolution Experience Pool
Lessons from previous iterations. Read before planning next generation.

## OPPONENT_MODELING

## POSTFLOP_STRATEGY

## BLUFF_CALIBRATION

## PARAMETER_TUNING

## GENERAL

## RECENT_LESSONS
"""


def _find_max_version():
    """Find the highest claude_v{N} version number in bots/."""
    max_v = 0
    if BOTS_DIR.exists():
        for d in os.listdir(BOTS_DIR):
            if d.startswith("claude_v") and os.path.isdir(BOTS_DIR / d):
                try:
                    v = int(d.split("_v")[1])
                    max_v = max(max_v, v)
                except (ValueError, IndexError):
                    pass
    return max_v


def _delete_bot_dirs(keep_versions):
    """Delete bots/claude_v{N} for N > keep_versions. Returns list of deleted versions."""
    deleted = []
    for d in sorted(os.listdir(BOTS_DIR)):
        if d.startswith("claude_v") and os.path.isdir(BOTS_DIR / d):
            try:
                v = int(d.split("_v")[1])
                if v > keep_versions:
                    shutil.rmtree(BOTS_DIR / d)
                    deleted.append(v)
            except (ValueError, IndexError):
                pass
    return sorted(deleted)


def _delete_git_tags(keep_versions):
    """Delete bot-v{N} tags for N > keep_versions. Returns list of deleted tags."""
    deleted = []
    import subprocess
    try:
        result = subprocess.run(
            ["git", "tag", "-l", "bot-v*"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False,
        )
        for tag in result.stdout.strip().splitlines():
            tag = tag.strip()
            if not tag:
                continue
            try:
                v = int(tag.split("-v")[1])
                if v > keep_versions:
                    subprocess.run(
                        ["git", "tag", "-d", tag],
                        cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False,
                    )
                    deleted.append(tag)
            except (ValueError, IndexError):
                pass
    except Exception:
        pass
    return sorted(deleted)


def _reset_json_file(path, default_content):
    """Write default_content to a JSON file."""
    from evolution_infra import locked_file
    with locked_file(path, "w") as f:
        json.dump(default_content, f)


def _delete_file(path):
    """Delete a file if it exists."""
    if path.exists():
        path.unlink()


def _clear_directory(path):
    """Delete all files/subdirs inside a directory, keeping the directory itself."""
    if path.exists():
        for item in os.listdir(path):
            item_path = path / item
            if item_path.is_dir():
                shutil.rmtree(item_path)
            else:
                item_path.unlink()


def _delete_version_log_dirs(keep_versions):
    """Delete results/v{N}/ log directories for N > keep_versions."""
    deleted = []
    if RESULTS_DIR.exists():
        for d in os.listdir(RESULTS_DIR):
            if d.startswith("v") and os.path.isdir(RESULTS_DIR / d):
                try:
                    v = int(d[1:])
                    if v > keep_versions:
                        shutil.rmtree(RESULTS_DIR / d)
                        deleted.append(v)
                except ValueError:
                    pass
    return sorted(deleted)


def _clear_orchestrator_logs():
    """Delete orchestrator log files from web/logs/."""
    deleted = 0
    if LOGS_DIR.exists():
        for f in glob.glob(str(LOGS_DIR / "orchestrator_*.txt")):
            os.unlink(f)
            deleted += 1
    return deleted


def reset_evolution(keep_versions=6):
    """Reset evolution state, keeping only v1-keep_versions as baselines.

    Returns a dict with details of what was reset.
    """
    result = {
        "stopped_daemon": False,
        "deleted_bot_dirs": [],
        "deleted_tags": [],
        "reset_files": [],
        "cleared_dirs": [],
        "deleted_log_dirs": [],
        "deleted_orch_logs": 0,
    }

    # Step 1: Stop daemon if running
    try:
        from evolution_core import stop_daemon
        stop_daemon()
        result["stopped_daemon"] = True
    except Exception:
        pass

    # Give daemon time for final save
    import time
    time.sleep(2)

    # Step 2: Delete v7+ bot directories
    result["deleted_bot_dirs"] = _delete_bot_dirs(keep_versions)

    # Step 3: Delete git tags
    result["deleted_tags"] = _delete_git_tags(keep_versions)

    # Step 4: Reset results data files
    _reset_json_file(RATINGS_FILE, {})
    result["reset_files"].append("glicko_ratings.json")

    _reset_json_file(STATS_FILE, {"pairs": {}, "total_games": 0})
    result["reset_files"].append("elo_daemon_stats.json")

    _reset_json_file(H2H_FILE, {})
    result["reset_files"].append("head_to_head.json")

    _reset_json_file(BOT_STATS_FILE, {})
    result["reset_files"].append("bot_stats.json")

    for f in [RATING_HISTORY_FILE, MATCH_HISTORY_FILE, WORKER_FAILURES_FILE,
              PIPELINE_STATE_FILE, ORCHESTRATOR_SESSION_FILE]:
        _delete_file(f)
        result["reset_files"].append(f.name)

    # Step 5: Clear directories
    _clear_directory(MATCH_REPLAY_DIR)
    result["cleared_dirs"].append("match_replay/")

    _clear_directory(COMMENTARY_DIR)
    result["cleared_dirs"].append("commentary/")

    result["deleted_log_dirs"] = _delete_version_log_dirs(keep_versions)

    # Step 6: Reset experience pool
    from evolution_infra import locked_file
    with locked_file(EXPERIENCE_FILE, "w") as f:
        f.write(EXPERIENCE_TEMPLATE)
    result["reset_files"].append("experience_pool.md")

    # Step 7: Clear orchestrator logs
    result["deleted_orch_logs"] = _clear_orchestrator_logs()

    return result
