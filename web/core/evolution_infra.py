"""Shared infrastructure for the poker bot evolution framework.

Contains constants, file utilities, git operations, ratings helpers.
No LLM agent logic — agent functions live in agent_*.py modules.
Runtime operations (daemon, LLM query, code verification) extracted to
daemon_management.py, llm_query.py, and code_verification.py.
"""

import os
import json
import logging
import shutil
import subprocess
import re
import asyncio
import threading
import uuid
import hashlib
import stat
from copy import deepcopy


import fcntl
import time
import contextvars
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("pok.infra")

# ──────────────────────────────────────────────
# One-ahead draft slot override (Phase 5b)
# ──────────────────────────────────────────────
# When the one-ahead draft task (gen N+1) runs concurrently with the primary
# consumer gate chain (gen N), it sets this ContextVar to "draft" for the
# duration of the draft asyncio task.  asyncio.create_task() copies the
# parent's context at creation time, so the override is scoped to exactly
# that task tree and never leaks into the primary loop or sibling tasks.
#
# ``pipeline_state_path`` consults this when its explicit ``slot_id`` argument
# is None, which makes every checkpoint read/write/read-all/projection call
# site (including the dozens inside stage handlers that take no slot_id)
# transparently target the draft slot while the override is active.  An
# explicit non-None ``slot_id`` argument always wins and bypasses the
# override, preserving the byte-identical primary path when callers pass
# slot_id=None from outside a draft task.
_ACTIVE_SLOT_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_ACTIVE_SLOT_OVERRIDE", default=None
)


@contextmanager
def active_slot_override(slot_id):
    """Bind a slot override for the duration of a draft asyncio task.

    Entering sets ``_ACTIVE_SLOT_OVERRIDE`` to ``slot_id`` so that every
    ``pipeline_state_path`` / ``read_pipeline_checkpoint`` /
    ``write_pipeline_checkpoint`` / ``clear_pipeline_checkpoint`` call made by
    code paths that take no explicit ``slot_id`` resolves to the draft slot
    file.  Exiting restores the prior value.  Must be entered inside the draft
    task (not the caller) so the asyncio task context carries the override.
    Yields a token suitable for ``ContextVar.reset`` if needed.
    """
    token = _ACTIVE_SLOT_OVERRIDE.set(slot_id)
    try:
        yield token
    finally:
        _ACTIVE_SLOT_OVERRIDE.reset(token)


@contextmanager
def no_slot_override():
    """Temporarily clear the slot override to read/write the primary slot.

    Used inside a draft task that needs to consult the *primary* checkpoint
    (e.g. to derive the draft's one-ahead ``next_v`` from the primary's sealed
    generation N).  Restores the prior override value on exit.
    """
    token = _ACTIVE_SLOT_OVERRIDE.set(None)
    try:
        yield token
    finally:
        _ACTIVE_SLOT_OVERRIDE.reset(token)


def current_slot_override():
    """Return the active slot override, or None if no override is bound.

    Lets callers (e.g. ``prepare_generation``) detect that they are running
    inside a draft task without inspecting the ContextVar directly.
    """
    try:
        return _ACTIVE_SLOT_OVERRIDE.get()
    except LookupError:
        return None

# Local module imports (same directory)
from glicko2 import Glicko2Player, update_rating_period
from evaluation_contract import build_evaluation_contract
from evolution_scope import classify_status_entries
from publish_reconcile import reconcile_push_refs
from bot_namespace import (
    ACTIVE_BOT_PREFIX,
    ACTIVE_TAG_PREFIX,
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    EVOLUTION_BRANCH,
    FIRST_STRICT_POLICY_VERSION,
    HIGH_WATER_TAG_PREFIX,
    NATIONAL_ENTRYPOINT,
    ROLE_CANDIDATE,
    ROLE_PARENT_SOURCE,
    active_bot_glob,
    bot_name,
    bot_relpath,
    bot_tag,
    bot_tag_glob,
    format_version,
    high_water_tag,
    high_water_tag_glob,
    parse_bot_version,
    parse_tag_version,
    resolve_version_namespace_authority,
    resolve_national_bot_spec,
    strip_bot_path_prefix,
    version_sort_key,
)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
_COPY_IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc')
CANDIDATE_COPY_IGNORE_NAMES = frozenset({"__pycache__", ".completed", ".task_context"})
PROMPTS_DIR = CORE_DIR / "prompts"
RESULTS_DIR = CORE_DIR / "results"
BOTS_DIR = PROJECT_ROOT / "bots"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
WORKER_FAILURES_FILE = RESULTS_DIR / "worker_failures.jsonl"
PIPELINE_STATE_FILE = RESULTS_DIR / "pipeline_state.json"


def pipeline_state_path(slot_id=None):
    """Resolve the checkpoint file path for a slot.

    ``slot_id=None`` (default) returns the primary/canonical checkpoint file
    (backward-compatible with all existing callers).  A non-None slot_id
    returns ``pipeline_state_<slot_id>.json`` for a concurrent generation.

    Phase 5b one-ahead draft: when a draft asyncio task is running and has
    bound ``_ACTIVE_SLOT_OVERRIDE``, a caller-supplied ``slot_id=None`` is
    transparently redirected to the draft slot.  An explicit non-None
    ``slot_id`` always wins.  This keeps the byte-identical primary path for
    every non-draft caller while letting the draft's stage handlers (which
    take no slot_id) target the draft slot file.
    """
    if slot_id is None:
        try:
            override = _ACTIVE_SLOT_OVERRIDE.get()
        except LookupError:
            override = None
        if override is not None:
            slot_id = override
    if slot_id is None:
        return PIPELINE_STATE_FILE
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(slot_id))
    return RESULTS_DIR / f"pipeline_state_{safe}.json"


REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"
ARCHIVE_DIR = RESULTS_DIR / "archive"
POST_PUBLICATION_HANDOFF_DIR = RESULTS_DIR / "post_publication_handoffs"
LLM_COSTS_FILE = RESULTS_DIR / "llm_costs.jsonl"
RATING_HISTORY_FILE = RESULTS_DIR / "rating_history.jsonl"
ABANDONED_VERSIONS_FILE = RESULTS_DIR / "abandoned_versions.jsonl"
REAPED_BOTS_FILE = RESULTS_DIR / "reaped_bots.jsonl"

POST_PUBLICATION_HANDOFF_SCHEMA_VERSION = 2
POST_PUBLICATION_HANDOFF_KIND = "national-policy-post-publication-handoff"
POST_PUBLICATION_HANDOFF_REQUIRED_STEPS = (
    "stability_observation",
    "reap_signal",
    "priority_eval",
    "archive_rotation",
    "log_cleanup",
    "pool_reap",
    "cycle_annotation",
    "housekeeping",
)
POST_PUBLICATION_HANDOFF_STALE_SEC = 15 * 60

ABANDONED_VERSION_RECEIPT_SCHEMA_VERSION = 1
ABANDONED_VERSION_RECEIPT_KIND = "national-policy-abandon-receipt"
_ABANDONED_VERSION_RECEIPT_KEYS = frozenset({
    "schema_version",
    "kind",
    "evaluation_epoch",
    "version",
    "source_v",
    "checkpoint_stage",
    "workflow_run_id",
    "checkpoint_revision",
    "checkpoint_envelope",
    "reason",
    "timestamp",
    "infra_failure",
    "previous_receipt_digest",
    "receipt_digest",
})
_ABANDONED_CHECKPOINT_ENVELOPE_KEYS = frozenset({
    "checkpoint_schema_version",
    "evaluation_epoch",
    "next_v",
    "source_v",
    "parent2_v",
    "generation_mode",
    "epoch_binding",
    "audit_context",
})
_ABANDONED_VERSION_LEDGER_MAX_BYTES = 16 * 1024 * 1024
_ABANDONED_VERSION_REASON_MAX_BYTES = 4 * 1024
_ABANDONED_VERSION_INFRA_FAILURE_MAX_BYTES = 64 * 1024

# Abandoned-version receipt ledger companion.  Hosts the schema-2 abandon
# transaction business (receipt construction, validation, decode/load,
# authority derivation, identity matching, floor/attempts queries).  The
# companion imports ``evolution_infra`` itself (``import evolution_infra as
# _ei``) and resolves cross-references lazily at call time, so this top-level
# import does not create a load-time cycle.  Every moved symbol is re-exposed
# below as a thin delegate shell so legacy ``from evolution_infra import
# <name>`` sites and ``evolution_infra.<name>`` monkeypatches keep working.
import abandoned_version_ledger as _ledger  # noqa: E402

# Atomic, crash-safe, sidecar-locked state-file I/O trust layer.  The bodies
# live in evolution_infra_state_io and are re-exposed below as thin delegate
# shells so legacy ``from evolution_infra import <name>`` sites and test
# monkeypatches on the ``evolution_infra`` namespace keep working.  The
# companion routes its internal cross-references back through this module
# (``_ei.<NAME>``), so monkeypatching ``evolution_infra._atomic_publish_state_text``
# / ``_locked_state_sidecar`` / ``_fsync_directory`` still takes effect even
# when the call originates inside the companion.
import evolution_infra_state_io as _sio  # noqa: E402

# Archive rotation plan/receipt companion.  Hosts the schema-2 archive-rotation
# pipeline (build/validate per-generation rotation plan, rotate log files into
# ARCHIVE_DIR, issue+verify digest-signed rotation receipts).  The companion
# imports ``evolution_infra`` itself (``import evolution_infra as _rot`` is
# aliased here as ``_rot``) and resolves cross-references lazily at call time,
# so this top-level import does not create a load-time cycle.  Every moved
# symbol is re-exposed below as a thin delegate shell so legacy
# ``from evolution_infra import <name>`` sites and
# ``evolution_infra.<name>`` monkeypatches keep working.
import evolution_infra_archive_rotation as _rot  # noqa: E402

# Git operations and publication-commit lifecycle companion.  Hosts the git
# wrapper, push enable/required flags, branch ensurance, tag/commit/ref
# checks, ``git_commit_bot``, the intent-bound publication commit creation/
# validation/ref verification flow, ``ensure_bot_git_publication``,
# ``verify_remote_bot_publication`` and the parent/tag discovery helpers.
# The companion imports ``evolution_infra`` itself (``import evolution_infra
# as _gp`` is aliased here as ``_gp``) and resolves cross-references lazily
# at call time, so this top-level import does not create a load-time cycle.
# Every moved symbol is re-exposed below as a thin delegate shell so legacy
# ``from evolution_infra import <name>`` sites and
# ``evolution_infra.<name>`` monkeypatches keep working.
import evolution_infra_git_publication as _gp  # noqa: E402

# Active-bots discovery and version namespace authority companion.  Hosts the
# canonical active bot pool resolution, protocol-fingerprinting, the published
# high-water and the version namespace authority.  The companion imports
# ``evolution_infra`` itself (``import evolution_infra_active_bots as _ab`` is
# aliased here as ``_ab``) and resolves cross-references lazily at call time,
# so this top-level import does not create a load-time cycle.  Every moved
# symbol is re-exposed below as a thin delegate shell so legacy
# ``from evolution_infra import <name>`` sites and
# ``evolution_infra.<name>`` monkeypatches keep working.
import evolution_infra_active_bots as _ab  # noqa: E402

# Remote publication proof + active-pool sentinel companion. Hosts the
# TTL'd single-flight remote publication proof cache, the local-then-remote
# tag/commit verification, the .completed sentinel restore and the in-flight
# publication-version filter. The companion imports ``evolution_infra`` itself
# (``import evolution_infra as _ei``) and resolves cross-references lazily at
# call time, so this top-level import does not create a load-time cycle.
# ``_REMOTE_PUBLICATION_CACHE_TTL_SEC`` stays here (it is read after
# ``importlib.reload(evolution_infra)`` in tests); the companion reads it via
# ``_ei._REMOTE_PUBLICATION_CACHE_TTL_SEC``. Every moved symbol is re-exposed
# below as a thin delegate shell so legacy ``from evolution_infra import
# <name>`` sites and ``evolution_infra.<name>`` monkeypatches keep working.
import evolution_infra_remote_publication as _rpub  # noqa: E402

# Pipeline checkpoint CAS internals companion. Hosts the repo baseline
# capture + HEAD-drift stage logic, the publication-reconciliation /
# identity-replan validators, and the write/read/clear checkpoint bodies.
# The companion imports ``evolution_infra`` itself (``import evolution_infra
# as _ei``) and resolves cross-references lazily at call time, so this
# top-level import does not create a load-time cycle. Every moved symbol is
# re-exposed below as a thin delegate shell so legacy
# ``from evolution_infra import <name>`` sites and
# ``evolution_infra.<name>`` monkeypatches keep working.
import evolution_infra_checkpoint_cas as _ckpt  # noqa: E402

# Generation archiving + post-commit Archivist receipt companion. Hosts the
# structured archive snapshot, the post-commit Archivist receipt
# construction/validation, and the retired consume-before-work stub. The
# companion imports ``evolution_infra`` itself (``import evolution_infra as
# _ei``) and resolves cross-references lazily at call time, so this top-level
# import does not create a load-time cycle. Every moved symbol is re-exposed
# below as a thin delegate shell so legacy ``from evolution_infra import
# <name>`` sites and ``evolution_infra.<name>`` monkeypatches keep working.
import evolution_infra_archiving as _arch  # noqa: E402


class AbandonedVersionLedgerError(RuntimeError):
    """The current-epoch allocation receipt ledger is not trustworthy."""

MAX_ACTIVE_BOTS = 30

# Evaluation & quality thresholds
DAEMON_EVAL_TIMEOUT = 600
MIN_GAMES_FOR_EVAL = 100
EVAL_WAIT_PROGRESS_INTERVAL_SEC = int(os.environ.get("POK_EVAL_WAIT_PROGRESS_INTERVAL_SEC", "30"))
MAX_LINES_PER_FILE = 2000       # Candidate-owned policy.py — base limit
MAX_LINES_HELPER = 1500         # System-owned runtime modules — base limit
MAX_LINES_HARD_CAP = 2500       # Hard cap: no .py file may exceed this, even with adaptive budget
LINE_GROWTH_BUDGET = 0.15       # Adaptive limit = max(base, source_lines * (1 + budget))

# Strip a trailing status annotation the LLM may append to target_files entries,
# e.g. "policy.py (MODIFIED)". Requires a bare keyword inside the brackets so
# legitimate names like "report(2).py" survive.
_TARGET_ANNOTATION_RE = re.compile(
    r"\s*[\(\[](?:NEW|CREATE|DELETE|MODIFIED)[\)\]]\s*$", re.IGNORECASE
)
CORE_STRATEGY_FILES = {"policy.py"}
MIN_DECISION_PASS_RATE = 0.7
MIN_CROSSOVER_DECISION_RATE = 0.6
MAX_WORKER_RETRIES = 4
MAX_MASTER_RETRIES = 3
MAX_CROSSOVER_RETRIES = 3
MAX_GENESIS_RETRIES = 3
MAX_PRECOMMIT_RETRIES = 3   # Max run_precommit_eval attempts against the SAME bot code (resets on worker rework)
MAX_PRECOMMIT_REWORK_ROUNDS = int(os.environ.get("POK_MAX_PRECOMMIT_REWORK_ROUNDS", "3"))
MAX_OFFICIAL_REWORK_ROUNDS = int(os.environ.get("POK_MAX_OFFICIAL_REWORK_ROUNDS", "2"))
MAX_MASTER_AUDIT_RETRIES = 1  # Initial Master plan + one corrective re-plan only
WORKER_TIMEOUT = 1800         # Seconds before a hung worker call is aborted + retried. Generous for GLM variable output speed.
MAX_PARALLEL_WORKERS = 2      # Max simultaneous worker LLM calls — now bounded by the global semaphore (llm_concurrency.py, default 2)

# Prompt size limits — Sonnet supports 200K tokens (~800K chars); leave generous headroom
MAX_PROMPT_CHARS = 700_000

# Pipeline stage constants. Re-exported here for compatibility; authoritative
# definitions live in pipeline_state.py.
from pipeline_state import (
    STAGE_ORDER,
    STAGE_GATE_ALLOWLIST,
    validate_stage_transition,
    validate_runtime_contract_ledger_reset,
    is_rework_reset_transition,
    invalidates_official_job_transition,
)

# EVOLUTION_BRANCH is imported from bot_namespace so the whole runtime shares one
# configurable publication-branch identity (default "main", overridable for an
# isolated deployment branch such as tencent-cloud-runtime via POK_EVOLUTION_BRANCH).
_LOCAL_PUB_REF = f"refs/heads/{EVOLUTION_BRANCH}"
_REMOTE_PUB_REF = f"refs/remotes/origin/{EVOLUTION_BRANCH}"


def is_candidate_copy_ignored_name(name: str) -> bool:
    """Return True for parent artifacts that must not enter a new candidate."""

    return name in CANDIDATE_COPY_IGNORE_NAMES or name.endswith(".pyc")


def candidate_copy_ignore(_src: str, names: list[str]) -> set[str]:
    return {name for name in names if is_candidate_copy_ignored_name(name)}


def copy_bot_tree_for_candidate(source_dir: str | Path, target_dir: str | Path) -> None:
    """Copy a completed parent bot into a mutable candidate directory.

    Runtime/task metadata is deliberately excluded. In particular,
    ``.task_context`` files are generated per version by ``plan_compiler`` and
    must never be inherited from an older source bot.
    """

    shutil.copytree(source_dir, target_dir, ignore=candidate_copy_ignore)

# Watchdog: if no pipeline stage change occurs within this many seconds,
# the orchestrator watchdog will clear the session and restart from checkpoint.
# Must exceed CYCLE_TIMEOUT (now 240 min) to avoid false positives. Generous
# for GLM variable output speed; catches only truly stuck pipelines.
WATCHDOG_TIMEOUT = 28800  # 480 minutes (8 hours)

# MCP servers to block for sub-agents (keep zai-mcp-server for vision, block the rest)
_BLOCKED_MCP_TOOLS = [
    "mcp__web-reader__webReader",
    "mcp__web-search-prime__web_search_prime",
    "mcp__zread__get_repo_structure",
    "mcp__zread__read_file",
    "mcp__zread__search_doc",
    # P1 (2026-06-29): disable built-in Task tools for the orchestrator. Task
    # sub-agents do NOT inherit the PreToolUse guard hook, so the LLM could
    # spawn a Task sub-agent whose Bash/Edit writes bot code / pipeline state
    # without any gate check — a full bypass of the pipeline guard. The
    # orchestrator's work is done via MCP tools (run_master/execute_workers/...),
    # so it never legitimately needs Task sub-agents.
    "Task", "TaskCreate", "TaskUpdate", "TaskOutput", "TaskList", "TaskGet",
]

# Adaptive semaphores keyed by api_concurrency level — created on first use
# inside the event loop. level 0 = MAX_PARALLEL_WORKERS(满并发); 503/限速时 level
# 升 → limit = max(1, base >> level) 自动降 worker 并发。不同 level 用不同
# Semaphore 实例(换实例时旧 in-flight worker 自然完成,平滑降级,不丢计数)。
# 这是真正的 LLM 并发源:workers 通过 run_claude_query 打 gateway。
_WORKER_SEMAPHORE: "dict[int, asyncio.Semaphore]" = {}


def _get_worker_semaphore() -> "asyncio.Semaphore":
    """Delegate to evolution_infra_state_io."""
    return _sio._get_worker_semaphore()


# ──────────────────────────────────────────────
# Atomic, crash-safe, sidecar-locked state-file I/O trust layer.
#
# The real bodies live in evolution_infra_state_io (imported as ``_sio``).
# Each function below is a thin delegate shell that preserves the original
# signature so legacy ``from evolution_infra import <name>`` import sites and
# test monkeypatches on the ``evolution_infra`` namespace continue to work.
# Internal callers inside this module resolve these names through the module
# namespace (i.e. the shell), so monkeypatching
# ``evolution_infra._atomic_publish_state_text`` / ``_locked_state_sidecar`` /
# ``_fsync_directory`` still takes effect everywhere.  The companion routes its
# own internal cross-references back here via ``_ei.<NAME>``.
# ──────────────────────────────────────────────


def _thread_lock_for(path) -> threading.RLock:
    """Delegate to evolution_infra_state_io."""
    return _sio._thread_lock_for(path)


@contextmanager
def _locked_file_os(path, mode='r', lock_type=None, encoding=None):
    """Delegate to evolution_infra_state_io."""
    with _sio._locked_file_os(path, mode=mode, lock_type=lock_type, encoding=encoding) as v:
        yield v


@contextmanager
def locked_file(path, mode='r', lock_type=None, encoding=None):
    """Delegate to evolution_infra_state_io."""
    with _sio.locked_file(path, mode=mode, lock_type=lock_type, encoding=encoding) as v:
        yield v


def _fsync_directory(path):
    """Delegate to evolution_infra_state_io."""
    return _sio._fsync_directory(path)


def _fsync_regular_state_file_and_parent(path):
    """Delegate to evolution_infra_state_io."""
    return _sio._fsync_regular_state_file_and_parent(path)


def _sidecar_lock_path(path):
    """Delegate to evolution_infra_state_io."""
    return _sio._sidecar_lock_path(path)


def _assert_safe_state_parent(path):
    """Delegate to evolution_infra_state_io."""
    return _sio._assert_safe_state_parent(path)


def _preflight_state_sidecar(path):
    """Delegate to evolution_infra_state_io."""
    return _sio._preflight_state_sidecar(path)


def _assert_open_regular_path(path, handle, *, label):
    """Delegate to evolution_infra_state_io."""
    return _sio._assert_open_regular_path(path, handle, label=label)


@contextmanager
def _locked_state_sidecar(path, *, lock_type):
    """Delegate to evolution_infra_state_io."""
    with _sio._locked_state_sidecar(path, lock_type=lock_type) as v:
        yield v


@contextmanager
def bot_publication_lock(*, results_dir=None):
    """Delegate to evolution_infra_state_io."""
    with _sio.bot_publication_lock(results_dir=results_dir) as v:
        yield v


def _read_regular_state_text(path, *, allow_missing):
    """Delegate to evolution_infra_state_io."""
    return _sio._read_regular_state_text(path, allow_missing=allow_missing)


def _atomic_publish_state_text(path, raw):
    """Delegate to evolution_infra_state_io."""
    return _sio._atomic_publish_state_text(path, raw)


def read_locked_json(path, default=None):
    """Delegate to evolution_infra_state_io."""
    return _sio.read_locked_json(path, default=default)


def read_and_maybe_unlink_locked_text(path, should_unlink):
    """Delegate to evolution_infra_state_io."""
    return _sio.read_and_maybe_unlink_locked_text(path, should_unlink)


def write_locked_json(path, data, indent=2):
    """Delegate to evolution_infra_state_io."""
    return _sio.write_locked_json(path, data, indent=indent)


def append_locked_jsonl(path, entry):
    """Delegate to evolution_infra_state_io."""
    return _sio.append_locked_jsonl(path, entry)


def update_h2h(h2h, bot_a, bot_b, wins_a, wins_b, draws=0):
    """Update H2H dict for a pair of bots.

    Accepts either one-game increments or aggregated match totals. Older callers
    use it per game; manual inline evaluation passes a whole mirror-battle
    summary, so games must advance by wins + losses + draws, not by one call.
    """
    key = pair_key(bot_a, bot_b)
    entry = h2h.setdefault(key, {"games": 0, "a_wins": 0, "b_wins": 0, "draws": 0})
    total = int(wins_a or 0) + int(wins_b or 0) + int(draws or 0)
    if total <= 0:
        return
    entry["games"] += total
    if bot_a < bot_b:
        entry["a_wins"] += wins_a
        entry["b_wins"] += wins_b
    else:
        entry["a_wins"] += wins_b
        entry["b_wins"] += wins_a
    entry["draws"] += draws
    entry["win_rate"] = round((entry["a_wins"] + 0.5 * entry["draws"]) / entry["games"], 4)


def update_bot_stats(bot_stats, name, wins, losses, draws=0):
    """Update per-bot stats dict. Creates entry if not present."""
    entry = bot_stats.setdefault(name, {"wins": 0, "losses": 0, "draws": 0, "games": 0, "win_rate": 0.0})
    entry["wins"] += wins
    entry["losses"] += losses
    entry["draws"] += draws
    entry["games"] += wins + losses + draws
    if entry["games"] > 0:
        entry["win_rate"] = round(
            (entry["wins"] + 0.5 * entry["draws"]) / entry["games"],
            4,
        )


def substitute_template(template, replacements):
    """Replace {key} placeholders in a template string. Warns on unreplaced placeholders."""
    result = template
    for key, value in replacements.items():
        result = result.replace(f"{{{key}}}", str(value))
    remaining = set(re.findall(r'\{([a-z_]+)\}', result))
    if remaining:
        # root-cause-audit 2026-06-21: worker_prompt 等 template 含 Python f-string 示例代码，
        # {d}/{n_calls}/{bp}/{cr} 等是合法代码变量非模板占位符，贪婪正则误报(曾 268 WARNING/
        # 周期，worker 实际收到完整正确代码)。降为 debug：开发期开 DEBUG 可见真未替换占位符，
        # 生产日志静默。
        log.debug("Unreplaced template placeholders (likely f-string code vars): %s", remaining)
    return result


# ──────────────────────────────────────────────
# Pipeline Checkpoint (Process Recovery)
# ──────────────────────────────────────────────

def _capture_repo_baseline(stage, *, next_v=None, source_v=None, checkpoint=None):
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt._capture_repo_baseline(stage, next_v=next_v, source_v=source_v, checkpoint=checkpoint)




def _prune_gate_results_for_stage(stage, gate_results):
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt._prune_gate_results_for_stage(stage, gate_results)


def _stage_refreshes_repo_baseline(old_stage, new_stage, gate_results=None) -> bool:
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt._stage_refreshes_repo_baseline(old_stage, new_stage, gate_results=gate_results)


def _publication_checkpoint_reconciliation_allowed(checkpoint, authority):
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt._publication_checkpoint_reconciliation_allowed(checkpoint, authority)


def partial_publication_checkpoint_recovery_allowed(
    checkpoint,
    *,
    namespace_authority,
):
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt.partial_publication_checkpoint_recovery_allowed(
        checkpoint,
        namespace_authority=namespace_authority,
    )



def _identity_replan_replacement_contract_errors(
    *,
    replacement,
    next_v,
    source_v,
    workflow_run_id,
    checkpoint_revision,
    checkpoint_stage,
    epoch_binding,
):
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt._identity_replan_replacement_contract_errors(
        replacement=replacement,
        next_v=next_v,
        source_v=source_v,
        workflow_run_id=workflow_run_id,
        checkpoint_revision=checkpoint_revision,
        checkpoint_stage=checkpoint_stage,
        epoch_binding=epoch_binding,
    )



def _identity_replan_live_materialization_errors(
    replacement,
    *,
    candidate_dir=None,
    artifact_root=None,
):
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt._identity_replan_live_materialization_errors(
        replacement,
        candidate_dir=candidate_dir,
        artifact_root=artifact_root,
    )



def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None,
                               reviewer_feedback="", generation_attempt=0,
                               gate_results=None, worker_failure_count=None,
                               worker_invocation_count=None,
                               parent2_v=None, direction_audit=None,
                               audit_context=None, reset_generation_attempt=False,
                               replace_audit_context=False,
                               audit_context_replacement_reason=None,
                               audit_attempt=None, reset_audit_attempt=False,
                               precommit_attempt=None, reset_precommit_attempt=False,
                               precommit_rework_count=None,
                               official_rework_count=None,
                               timeout_extensions=None, touch_stage_timestamp=False,
                               literature_probe=None, prepare_scope_files=None,
                               clear_reviewer_feedback=False,
                               infra_failure=None, clear_infra_failure=False,
                               infra_failure_owner=None,
                               expected_infra_failure_digest=None,
                               official_job=None, clear_official_job=False,
                               expected_official_job_id=None,
                               repair_baseline_artifact_hash=None,
                               clear_repair_baseline_artifact_hash=False,
                               reset_runtime_contract_ledger=False,
                               expected_runtime_contract_ledger_digest=None,
                               runtime_contract_ledger_reset_reason=None,
                               publication_intent=None,
                               expected_checkpoint_revision=None,
                               expected_checkpoint_stage=None,
                               expected_workflow_run_id=None,
                               workflow_run_id=None,
                               terminal_gate_outcome=None,
                               review_attempt_journal=None,
                               identity_replan_history=None,
                               candidate_artifact_hash=None,
                               candidate_manifest_digest=None,
                               charter_digest=None,
                               slot_id=None):
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt.write_pipeline_checkpoint(next_v=next_v, source_v=source_v, stage=stage, master_plan=master_plan, reviewer_feedback=reviewer_feedback, generation_attempt=generation_attempt, gate_results=gate_results, worker_failure_count=worker_failure_count, worker_invocation_count=worker_invocation_count, parent2_v=parent2_v, direction_audit=direction_audit, audit_context=audit_context, reset_generation_attempt=reset_generation_attempt, replace_audit_context=replace_audit_context, audit_context_replacement_reason=audit_context_replacement_reason, audit_attempt=audit_attempt, reset_audit_attempt=reset_audit_attempt, precommit_attempt=precommit_attempt, reset_precommit_attempt=reset_precommit_attempt, precommit_rework_count=precommit_rework_count, official_rework_count=official_rework_count, timeout_extensions=timeout_extensions, touch_stage_timestamp=touch_stage_timestamp, literature_probe=literature_probe, prepare_scope_files=prepare_scope_files, clear_reviewer_feedback=clear_reviewer_feedback, infra_failure=infra_failure, clear_infra_failure=clear_infra_failure, infra_failure_owner=infra_failure_owner, expected_infra_failure_digest=expected_infra_failure_digest, official_job=official_job, clear_official_job=clear_official_job, expected_official_job_id=expected_official_job_id, repair_baseline_artifact_hash=repair_baseline_artifact_hash, clear_repair_baseline_artifact_hash=clear_repair_baseline_artifact_hash, reset_runtime_contract_ledger=reset_runtime_contract_ledger, expected_runtime_contract_ledger_digest=expected_runtime_contract_ledger_digest, runtime_contract_ledger_reset_reason=runtime_contract_ledger_reset_reason, publication_intent=publication_intent, expected_checkpoint_revision=expected_checkpoint_revision, expected_checkpoint_stage=expected_checkpoint_stage, expected_workflow_run_id=expected_workflow_run_id, workflow_run_id=workflow_run_id, terminal_gate_outcome=terminal_gate_outcome, review_attempt_journal=review_attempt_journal, identity_replan_history=identity_replan_history, candidate_artifact_hash=candidate_artifact_hash, candidate_manifest_digest=candidate_manifest_digest, charter_digest=charter_digest, slot_id=slot_id)



def read_pipeline_checkpoint(slot_id=None):
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt.read_pipeline_checkpoint(slot_id=slot_id)



def clear_pipeline_checkpoint(
    *,
    expected_workflow_run_id=None,
    expected_next_v=None,
    expected_source_v=None,
    expected_checkpoint_revision=None,
    expected_checkpoint_stage=None,
    slot_id=None,
):
    """Delegate to evolution_infra_checkpoint_cas."""
    return _ckpt.clear_pipeline_checkpoint(
        expected_workflow_run_id=expected_workflow_run_id,
        expected_next_v=expected_next_v,
        expected_source_v=expected_source_v,
        expected_checkpoint_revision=expected_checkpoint_revision,
        expected_checkpoint_stage=expected_checkpoint_stage,
        slot_id=slot_id,
    )


def read_all_pipeline_checkpoints():
    """Read all active checkpoint slots as {slot_id_or_'primary': dict}.

    ``slot_id=None`` (the primary/canonical checkpoint) is returned under the
    key ``"primary"``.  Any ``pipeline_state_<slot_id>.json`` files found in
    ``RESULTS_DIR`` are returned keyed by their slot_id.  Missing or corrupt
    slots are skipped.
    """
    result = {}
    primary = read_pipeline_checkpoint()
    if primary is not None:
        result["primary"] = primary
    for path in RESULTS_DIR.glob("pipeline_state_*.json"):
        # Extract slot_id from filename (stem strips the .json suffix).
        slot_id = path.stem.removeprefix("pipeline_state_")
        ckpt = _ckpt.read_pipeline_checkpoint(slot_id=slot_id)
        if ckpt is not None:
            result[slot_id] = ckpt
    return result




# ──────────────────────────────────────────────
# UI Interface
# ──────────────────────────────────────────────

class BaseUI:
    def log_history(self, msg, status="info"): pass
    def set_status(self, msg, is_working=False): pass
    def log_io(self, msg, stream_type="default", role=""): pass
    def clear_io(self): pass
    def update_eval_table(self, ratings, active_bots): pass
    def update_daemon_status(self, stats, ratings): pass
    def set_header(self, msg): pass
    def update_cost(self, role, cost_usd, usage): pass
    def begin_generation_cost(self, generation_id, spent_usd, policy_receipt=None): pass
    def reset_gen_cost(self): pass
    def update_metrics(self, metrics): pass
    def emit_tool_call(self, tool_name: str, args: dict, role: str = ""): pass


class NullUI(BaseUI):
    """Explicit no-op UI for headless infrastructure entry points."""


_NULL_UI = NullUI()


def resolve_ui(ui=None):
    """Return a concrete UI object while preserving an injected UI unchanged."""
    return _NULL_UI if ui is None else ui


# ──────────────────────────────────────────────
# Bot Directory & Status
# ──────────────────────────────────────────────

def count_lines(path):
    try:
        with open(path, "r", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def pair_key(a, b):
    return f"{a} vs {b}" if a < b else f"{b} vs {a}"


def get_bot_dir(version):
    """Return only the canonical active-namespace directory.

    Archived/reaped trees are never a transparent source, candidate or
    opponent fallback.  Audit callers must address an archive explicitly.
    """

    return BOTS_DIR / bot_name(version)


def get_logs_dir(version):
    d = RESULTS_DIR / f"v{version}" / "logs"
    os.makedirs(d, exist_ok=True)
    return d


def _tagged_bot_versions():
    """Return strict-policy versions backed by completion tags.

    Completion tags through the archived high-water remain immutable audit
    evidence, but they are not members of ``national_tcp_policy_v1``.
    """
    tag_versions = set()
    for tag in _git("tag", "-l", bot_tag_glob(), check=False).strip().splitlines():
        version = parse_tag_version(tag)
        if version is not None and version >= FIRST_STRICT_POLICY_VERSION:
            tag_versions.add(version)
    return tag_versions


def _registry_may_be_virgin() -> bool:
    """True when the durable reaped registry is legitimately empty.

    A fresh cloud checkout (no ``national-cloud-bot-v*`` completion tags and no
    ``national-reaped-registry-v1`` migration marker) has never reaped any bot,
    so ``parse_legacy_ledger`` correctly reports ``legacy_ledger_missing`` and
    ``load_reaped_bot_versions`` raises ``RegistryUnavailableError``. That is the
    *expected* empty state, not a registry failure: returning an empty reaped set
    here keeps the fail-closed registry contract (corrupt tags, missing marker
    after migration, etc. still raise) while suppressing the operator-noise
    ERROR that ``elo_daemon``'s 3s active-bot poll would otherwise log forever.

    Once any strict bot publishes, ``_tagged_bot_versions()`` is non-empty and
    this helper returns False, restoring the strict fail-closed log path.
    """
    if _tagged_bot_versions():
        return False
    try:
        marker = _git(
            "tag",
            "-l",
            "national-reaped-registry-v1",
            check=False,
        ).strip()
    except Exception:
        marker = ""
    return not marker


def _bot_version_from_name(bot_name):
    return parse_bot_version(bot_name)


def load_reaped_bot_versions():
    """Return durable reaped versions, raising when lifecycle state is unavailable."""
    from national_epoch_registry import load_registry_state

    state = load_registry_state(
        PROJECT_ROOT,
        legacy_ledger=REAPED_BOTS_FILE,
        include_history=False,
    )
    return set(state.require_reaped_versions())


def record_reaped_bot(bot_name, *, reason="", data=None):
    """Durably tombstone a bot before recording the advisory runtime ledger."""
    version = _bot_version_from_name(bot_name)
    if version is None:
        raise ValueError(f"invalid national bot label: {bot_name}")
    push_enabled = evolution_git_push_enabled()
    push_required = evolution_git_push_required()
    if push_required and not push_enabled:
        raise RuntimeError(
            "durable reaping requires EVOLUTION_GIT_PUSH=1 in the evolution runtime"
        )
    from national_epoch_registry import REAPED_TAG_PREFIX, create_reaped_tombstone

    mutation = create_reaped_tombstone(
        version,
        repo_root=PROJECT_ROOT,
        legacy_ledger=REAPED_BOTS_FILE,
    )
    tombstone_tag = f"{REAPED_TAG_PREFIX}{version}"
    pushed = False
    if push_enabled or push_required:
        pushed = git_push_refs(tombstone_tag)
        if push_required and not pushed:
            raise RuntimeError(f"failed to publish durable reaped tombstone {tombstone_tag}")
    try:
        from official_eligibility import clear_registry_state_cache

        clear_registry_state_cache()
    except Exception:
        pass
    entry = {
        "ts": time.time(),
        "bot": bot_name,
        "version": version,
        "reason": reason,
        "data": data or {},
        "registry": {
            "tombstone_tag": tombstone_tag,
            "created_tags": list(mutation.created_tags),
            "pushed": pushed,
        },
    }
    os.makedirs(RESULTS_DIR, exist_ok=True)
    append_locked_jsonl(REAPED_BOTS_FILE, entry)
    return entry


# TTL for the remote publication proof cache. Read-only observer requests
# (/api/bots, control status/health) re-resolve the published pool frequently;
# a short TTL amplifies a slow origin into an ASGI outage by re-running
# ``git ls-remote origin`` on every poll. Default 60s keeps the read path
# off the network during a burst. Effect/launch boundaries still call
# ``_clear_remote_publication_cache()`` to force a fresh remote proof, so a
# longer TTL never weakens publication validation.
_REMOTE_PUBLICATION_CACHE_TTL_SEC = float(
    os.environ.get("POK_REMOTE_PUBLICATION_CACHE_TTL", "60.0")
)

def _clear_remote_publication_cache():
    """Delegate to evolution_infra_remote_publication."""
    return _rpub._clear_remote_publication_cache()


def _remote_published_completion_versions(tag_versions) -> set[int]:
    """Delegate to evolution_infra_remote_publication."""
    return _rpub._remote_published_completion_versions(tag_versions)


def _ensure_completed_sentinels_for_tagged_bots(tag_versions=None, reaped_versions=None):
    """Delegate to evolution_infra_remote_publication."""
    return _rpub._ensure_completed_sentinels_for_tagged_bots(tag_versions=tag_versions, reaped_versions=reaped_versions)


def _incomplete_checkpoint_publication_versions(tag_versions) -> set[int]:
    """Delegate to evolution_infra_remote_publication."""
    return _rpub._incomplete_checkpoint_publication_versions(tag_versions)


def active_native_contract_filter_enabled() -> bool:
    """Delegate to evolution_infra_active_bots."""
    return _ab.active_native_contract_filter_enabled()


def _bot_protocol_fingerprint(bot_dir: Path) -> tuple[tuple, ...]:
    """Delegate to evolution_infra_active_bots."""
    return _ab._bot_protocol_fingerprint(bot_dir)


def active_bot_protocol_errors(
    version: int,
    *,
    quarantine_health: dict | None = None,
) -> list[str]:
    """Delegate to evolution_infra_active_bots."""
    return _ab.active_bot_protocol_errors(
        version,
        quarantine_health=quarantine_health,
    )


def is_active_bot_protocol_eligible(
    version: int,
    *,
    quarantine_health: dict | None = None,
) -> bool:
    """Delegate to evolution_infra_active_bots."""
    return _ab.is_active_bot_protocol_eligible(
        version,
        quarantine_health=quarantine_health,
    )


_ORIGINAL_IS_ACTIVE_BOT_PROTOCOL_ELIGIBLE = is_active_bot_protocol_eligible


def _protocol_eligible_for_discovery(version: int, quarantine_health: dict | None) -> bool:
    """Delegate to evolution_infra_active_bots."""
    return _ab._protocol_eligible_for_discovery(version, quarantine_health)


def _target_rel(path, version):
    """Delegate to evolution_infra_active_bots."""
    return _ab._target_rel(path, version)


def _discover_active_bots(
    *,
    repair_completed_sentinels: bool,
    require_completed_sentinel: bool = True,
    ledger_fresh: bool = True,
) -> list[str]:
    """Delegate to evolution_infra_active_bots."""
    return _ab._discover_active_bots(
        repair_completed_sentinels=repair_completed_sentinels,
        require_completed_sentinel=require_completed_sentinel,
        ledger_fresh=ledger_fresh,
    )


def get_active_bots():
    """Delegate to evolution_infra_active_bots."""
    return _ab.get_active_bots()


def get_active_bots_read_only(*, ledger_fresh: bool = True):
    """Delegate to evolution_infra_active_bots."""
    return _ab.get_active_bots_read_only(ledger_fresh=ledger_fresh)


def get_published_active_bots_read_only(*, ledger_fresh: bool = True):
    """Delegate to evolution_infra_active_bots."""
    return _ab.get_published_active_bots_read_only(ledger_fresh=ledger_fresh)


def _official_parent_eligible(
    bot_dir: Path,
    *,
    ledger_fresh: bool = True,
) -> bool:
    """Delegate to evolution_infra_active_bots."""
    return _ab._official_parent_eligible(bot_dir, ledger_fresh=ledger_fresh)


_ORIGINAL_OFFICIAL_PARENT_ELIGIBLE = _official_parent_eligible


def version_namespace_authority():
    """Delegate to evolution_infra_active_bots."""
    return _ab.version_namespace_authority()


def find_current_v():
    """Delegate to evolution_infra_active_bots."""
    return _ab.find_current_v()


def find_latest_active_v():
    """Delegate to evolution_infra_active_bots."""
    return _ab.find_latest_active_v()


# ──────────────────────────────────────────────
# Ratings
# ──────────────────────────────────────────────

def load_ratings():
    """Load Glicko-2 ratings with shared lock."""
    from evaluation_data_identity import ensure_evaluation_data_identity

    ensure_evaluation_data_identity(RESULTS_DIR)
    data = read_locked_json(RATINGS_FILE)
    if not data:
        return {}
    try:
        return {name: Glicko2Player.from_dict(d) for name, d in data.items()}
    except Exception:
        return {}


def load_daemon_stats():
    """Load daemon stats."""
    return read_locked_json(STATS_FILE, default={"pairs": {}, "total_games": 0})


def _is_shutdown(event) -> bool:
    """Check if a shutdown signal is set. Accepts asyncio.Event, ShutdownManager, or None."""
    if event is None:
        return False
    if hasattr(event, 'is_set'):
        return event.is_set()
    if hasattr(event, 'is_shutting_down'):
        return event.is_shutting_down
    return False


def _log_eval_wait_event(event_type: str, severity: str, message: str, **data) -> None:
    try:
        from system_log import log_system_event
        log_system_event(event_type, severity, message, data)
    except Exception:
        pass


async def wait_for_daemon_eval(
    bot_name,
    timeout=DAEMON_EVAL_TIMEOUT,
    min_games=MIN_GAMES_FOR_EVAL,
    ui=None,
    shutdown_event=None,
    rd_threshold=90,
    rd_min_games=30,
):
    """Wait for daemon to evaluate a new bot (async, non-blocking).

    Returns True when either:
      - games >= min_games (hard threshold), OR
      - rd < EVAL_RD_THRESHOLD and games >= EVAL_RD_MIN_GAMES (confidence-based early exit)
    Returns False on timeout or shutdown signal.
    """
    from daemon_management import daemon_proc, _daemon_lock

    # RD-based confidence early-exit. Now that decay_rd uses the official Glicko-2
    # formula (no 150 floor), RD genuinely converges as a bot accumulates games.
    # Defaults keep the historical 90/30 gate; workflow profiles may relax or
    # tighten those values for rating backends with different sample semantics.
    EVAL_RD_THRESHOLD = float(rd_threshold)
    EVAL_RD_MIN_GAMES = int(rd_min_games)

    start = time.time()
    cached_bot_stats = None
    bot_stats_mtime = 0
    ratings_mtime = 0
    cached_rd = None
    last_log = start
    last_daemon_dead_log = 0.0

    _log_eval_wait_event(
        "pipeline.eval_wait_start",
        "info",
        f"Waiting for {bot_name} evaluation ({min_games} games or low RD)",
        bot=bot_name,
        min_games=min_games,
        timeout_sec=timeout,
        rd_threshold=EVAL_RD_THRESHOLD,
        rd_min_games=EVAL_RD_MIN_GAMES,
    )

    while time.time() - start < timeout:
        if _is_shutdown(shutdown_event):
            games = (cached_bot_stats or {}).get(bot_name, {}).get("games", 0)
            _log_eval_wait_event(
                "pipeline.eval_wait_shutdown",
                "warn",
                f"Evaluation wait interrupted for {bot_name} during shutdown",
                bot=bot_name,
                games=games,
                min_games=min_games,
                elapsed_sec=round(time.time() - start, 2),
            )
            return False

        if BOT_STATS_FILE.exists():
            mt = os.path.getmtime(BOT_STATS_FILE)
            if mt != bot_stats_mtime:
                bot_stats_mtime = mt
                cached_bot_stats = read_locked_json(BOT_STATS_FILE, default={})
        if cached_bot_stats is None:
            cached_bot_stats = {}

        games = cached_bot_stats.get(bot_name, {}).get("games", 0)
        if games >= min_games:
            _log_eval_wait_event(
                "pipeline.eval_wait_ready",
                "success",
                f"{bot_name} evaluation ready: {games}/{min_games} games",
                bot=bot_name,
                games=games,
                min_games=min_games,
                elapsed_sec=round(time.time() - start, 2),
                reason="min_games",
            )
            return True

        # RD-based early exit
        if games >= EVAL_RD_MIN_GAMES and RATINGS_FILE.exists():
            mt = os.path.getmtime(RATINGS_FILE)
            if mt != ratings_mtime:
                ratings_mtime = mt
                try:
                    ratings = load_ratings()
                    player = ratings.get(bot_name)
                    cached_rd = player.rd if player else None
                except Exception:
                    cached_rd = None
            if cached_rd is not None and cached_rd < EVAL_RD_THRESHOLD:
                if ui:
                    ui.log_history(f"{bot_name} 评估就绪: rd={cached_rd:.1f} (<{EVAL_RD_THRESHOLD}), {games} 场", "success")
                _log_eval_wait_event(
                    "pipeline.eval_wait_ready",
                    "success",
                    f"{bot_name} evaluation ready: rd={cached_rd:.1f}, games={games}",
                    bot=bot_name,
                    games=games,
                    min_games=min_games,
                    rd=round(cached_rd, 2),
                    rd_threshold=EVAL_RD_THRESHOLD,
                    elapsed_sec=round(time.time() - start, 2),
                    reason="rd_threshold",
                )
                return True

        if time.time() - last_log >= EVAL_WAIT_PROGRESS_INTERVAL_SEC:
            elapsed = int(time.time() - start)
            rd_info = f", rd={cached_rd:.1f}" if cached_rd else ""
            if ui:
                ui.log_history(f"等待 {bot_name} 评估: {games}/{min_games} 场 ({elapsed}s{rd_info})", "info")
            _log_eval_wait_event(
                "pipeline.eval_wait_progress",
                "info",
                f"Waiting for {bot_name} evaluation: {games}/{min_games} games ({elapsed}s{rd_info})",
                bot=bot_name,
                games=games,
                min_games=min_games,
                rd=round(cached_rd, 2) if cached_rd is not None else None,
                elapsed_sec=elapsed,
                progress_interval_sec=EVAL_WAIT_PROGRESS_INTERVAL_SEC,
            )
            last_log = time.time()

        # Check daemon health every iteration — daemon may crash after producing
        # partial results, leaving us waiting the full timeout.
        # (Not gated by the 30s log interval — crashes need fast detection.)
        with _daemon_lock:
            proc = daemon_proc
        if proc is not None and proc.poll() is not None:
            if games >= min_games:
                if ui:
                    ui.log_history(f"Daemon 已终止 (rc={proc.returncode})，但已有 {games} 场 (≥{min_games})，继续", "warn")
                _log_eval_wait_event(
                    "pipeline.eval_wait_ready",
                    "success",
                    f"Daemon exited but {bot_name} already has {games}/{min_games} games",
                    bot=bot_name,
                    games=games,
                    min_games=min_games,
                    daemon_returncode=proc.returncode,
                    elapsed_sec=round(time.time() - start, 2),
                    reason="min_games_after_daemon_exit",
                )
                return True
            else:
                if ui:
                    ui.log_history(f"Daemon 已终止 (rc={proc.returncode})，仅 {games}/{min_games} 场，等待重启...", "error")
                if time.time() - last_daemon_dead_log >= EVAL_WAIT_PROGRESS_INTERVAL_SEC:
                    _log_eval_wait_event(
                        "pipeline.eval_wait_daemon_dead",
                        "warn",
                        f"Daemon exited while waiting for {bot_name}: {games}/{min_games} games",
                        bot=bot_name,
                        games=games,
                        min_games=min_games,
                        daemon_returncode=proc.returncode,
                        elapsed_sec=round(time.time() - start, 2),
                    )
                    last_daemon_dead_log = time.time()
                # Don't return False — daemon_monitor_thread may restart it.
                # Continue waiting until timeout expires.


        await asyncio.sleep(5)
    if ui:
        games = cached_bot_stats.get(bot_name, {}).get("games", 0)
        ui.log_history(f"评估超时 {bot_name}: 仅 {games}/{min_games} 场 ({int(time.time()-start)}s)", "warn")
    games = (cached_bot_stats or {}).get(bot_name, {}).get("games", 0)
    _log_eval_wait_event(
        "pipeline.eval_wait_timeout",
        "warn",
        f"Evaluation wait timed out for {bot_name}: {games}/{min_games} games",
        bot=bot_name,
        games=games,
        min_games=min_games,
        rd=round(cached_rd, 2) if cached_rd is not None else None,
        elapsed_sec=round(time.time() - start, 2),
        timeout_sec=timeout,
    )
    return False


# ──────────────────────────────────────────────
# Git Helpers
# ──────────────────────────────────────────────

def _git(*args, check=True):
    """Delegate to evolution_infra_git_publication."""
    return _gp._git(*args, check=check)


def _git_explicit_presence(*args):
    """Delegate to evolution_infra_git_publication."""
    return _gp._git_explicit_presence(*args)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def evolution_git_push_enabled() -> bool:
    """Delegate to evolution_infra_git_publication."""
    return _gp.evolution_git_push_enabled()


def evolution_git_push_required() -> bool:
    """Delegate to evolution_infra_git_publication."""
    return _gp.evolution_git_push_required()


def git_publish_status() -> dict:
    """Delegate to evolution_infra_git_publication."""
    return _gp.git_publish_status()


def ensure_publish_ready_for_new_generation() -> tuple[bool, dict]:
    """Delegate to evolution_infra_git_publication."""
    return _gp.ensure_publish_ready_for_new_generation()


def git_push_refs(*refs: str) -> bool:
    """Delegate to evolution_infra_git_publication."""
    return _gp.git_push_refs(*refs)


def _git_ensure_main_branch():
    """Delegate to evolution_infra_git_publication."""
    return _gp._git_ensure_main_branch()


def git_has_tag(version):
    """Delegate to evolution_infra_git_publication."""
    return _gp.git_has_tag(version)


def git_has_publication_ref(version):
    """Delegate to evolution_infra_git_publication."""
    return _gp.git_has_publication_ref(version)


def git_dir_is_committed(version):
    """Delegate to evolution_infra_git_publication."""
    return _gp.git_dir_is_committed(version)


def find_max_committed_v():
    """Delegate to evolution_infra_git_publication."""
    return _gp.find_max_committed_v()


def _abandoned_receipt_digest(payload):
    """Delegate to abandoned_version_ledger."""
    return _ledger._abandoned_receipt_digest(payload)


def _canonical_abandon_json_bytes(payload, *, label):
    """Delegate to abandoned_version_ledger."""
    return _ledger._canonical_abandon_json_bytes(payload, label=label)


def _validate_abandon_receipt_bounded_fields(reason, infra_failure):
    """Delegate to abandoned_version_ledger."""
    return _ledger._validate_abandon_receipt_bounded_fields(reason, infra_failure)


def _abandoned_checkpoint_envelope(checkpoint):
    """Delegate to abandoned_version_ledger."""
    return _ledger._abandoned_checkpoint_envelope(checkpoint)


def _validate_abandoned_checkpoint(checkpoint, *, project_root):
    """Delegate to abandoned_version_ledger."""
    return _ledger._validate_abandoned_checkpoint(checkpoint, project_root=project_root)


def _build_abandoned_version_receipt(
    checkpoint,
    *,
    reason,
    infra_failure=None,
    timestamp=None,
    previous_receipt_digest=None,
    project_root=None,
):
    """Delegate to abandoned_version_ledger."""
    return _ledger._build_abandoned_version_receipt(
        checkpoint,
        reason=reason,
        infra_failure=infra_failure,
        timestamp=timestamp,
        previous_receipt_digest=previous_receipt_digest,
        project_root=project_root,
    )


def _abandoned_version_receipt_errors(
    receipt,
    *,
    expected_previous_digest,
    project_root,
):
    """Delegate to abandoned_version_ledger."""
    return _ledger._abandoned_version_receipt_errors(
        receipt,
        expected_previous_digest=expected_previous_digest,
        project_root=project_root,
    )


def _decode_abandoned_version_receipts(
    raw,
    *,
    allow_empty,
    project_root,
):
    """Delegate to abandoned_version_ledger."""
    return _ledger._decode_abandoned_version_receipts(
        raw,
        allow_empty=allow_empty,
        project_root=project_root,
    )


def load_abandoned_version_receipts(*, path=None, project_root=None):
    """Delegate to abandoned_version_ledger."""
    return _ledger.load_abandoned_version_receipts(path=path, project_root=project_root)


def _abandon_authority_from_receipts(
    receipts,
    *,
    published_high_water,
    retryable_first_strict,
):
    """Delegate to abandoned_version_ledger."""
    return _ledger._abandon_authority_from_receipts(
        receipts,
        published_high_water=published_high_water,
        retryable_first_strict=retryable_first_strict,
    )


def abandoned_version_authority(
    *,
    initialization=None,
    published_high_water=None,
    path=None,
    project_root=None,
):
    """Delegate to abandoned_version_ledger."""
    return _ledger.abandoned_version_authority(
        initialization=initialization,
        published_high_water=published_high_water,
        path=path,
        project_root=project_root,
    )


def checkpoint_allocation_authority(*, expected_next_v=None):
    """Resolve system authority and optionally assert one requested successor."""

    from epoch_authority import policy_epoch_initialization

    published = int(find_current_v())
    initialization = policy_epoch_initialization(current_v=published)
    if not initialization.get("initialized"):
        raise AbandonedVersionLedgerError(
            "checkpoint allocation requires an initialized policy epoch"
        )
    abandoned = abandoned_version_authority(
        initialization=initialization,
        published_high_water=published,
    )
    authority = {
        "published_high_water": published,
        "abandoned_receipt_floor": int(abandoned["floor"]),
        "abandoned_receipt_head_digest": abandoned["head_digest"],
        "allocation_floor": max(published, int(abandoned["floor"])),
    }
    if expected_next_v is not None and (
        type(expected_next_v) is not int
        or expected_next_v != authority["allocation_floor"] + 1
    ):
        raise AbandonedVersionLedgerError(
            "checkpoint target is not the live allocation successor"
        )
    return authority


def _receipt_identity_matches_checkpoint(receipt, checkpoint):
    """Delegate to abandoned_version_ledger."""
    return _ledger._receipt_identity_matches_checkpoint(receipt, checkpoint)


def recorded_abandon_receipt_for_checkpoint(
    checkpoint,
    *,
    path=None,
    project_root=None,
):
    """Delegate to abandoned_version_ledger."""
    return _ledger.recorded_abandon_receipt_for_checkpoint(
        checkpoint,
        path=path,
        project_root=project_root,
    )


def append_abandoned_version_receipt(
    checkpoint,
    *,
    reason,
    infra_failure=None,
    timestamp=None,
    path=None,
    project_root=None,
):
    """Append one checkpoint-bound receipt after validating the whole chain."""

    path = Path(path) if path is not None else Path(ABANDONED_VERSIONS_FILE)
    project_root = Path(project_root) if project_root is not None else PROJECT_ROOT
    # Validate before opening in append mode so an unauthorized call cannot
    # manufacture even an empty ledger claim.
    _validate_abandoned_checkpoint(checkpoint, project_root=project_root)
    normalized_reason = str(reason or "").strip()
    _validate_abandon_receipt_bounded_fields(normalized_reason, infra_failure)
    try:
        with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
            existed = os.path.lexists(path)
            raw = _read_regular_state_text(path, allow_missing=True)
            if existed and not raw:
                raise AbandonedVersionLedgerError(
                    "abandon receipt ledger is empty"
                )
            receipts = _decode_abandoned_version_receipts(
                raw,
                allow_empty=not existed,
                project_root=project_root,
            )
            matching = [
                receipt
                for receipt in receipts
                if _receipt_identity_matches_checkpoint(receipt, checkpoint)
            ]
            if matching:
                if len(matching) != 1 or matching[0] is not receipts[-1]:
                    raise AbandonedVersionLedgerError(
                        "abandon receipt identity is not the unique chain head"
                    )
                prior = matching[0]
                if (
                    prior.get("reason") != normalized_reason
                    or prior.get("infra_failure") != infra_failure
                ):
                    raise AbandonedVersionLedgerError(
                        "abandon receipt identity already exists with different payload"
                    )
                # Clear/fsync failure may replay the exact terminal command.
                # Re-prove both the published inode and its parent.  A prior
                # atomic replace may have succeeded before the directory fsync
                # raised, so returning the row without this retry could permit
                # candidate/checkpoint destruction on a non-durable receipt.
                _fsync_regular_state_file_and_parent(path)
                return dict(prior)
            previous_digest = (
                receipts[-1]["receipt_digest"] if receipts else None
            )
            published = int(find_current_v())
            retryable_first_strict = published < FIRST_STRICT_POLICY_VERSION
            authority = _abandon_authority_from_receipts(
                receipts,
                published_high_water=published,
                retryable_first_strict=retryable_first_strict,
            )
            from checkpoint_schema import live_checkpoint_allocation_authority_errors

            live_errors = live_checkpoint_allocation_authority_errors(
                checkpoint,
                published_high_water=published,
                abandoned_receipt_floor=int(authority["floor"]),
                abandoned_receipt_head_digest=authority["head_digest"],
            )
            if live_errors:
                raise AbandonedVersionLedgerError(
                    "abandon checkpoint allocation authority changed: "
                    + "; ".join(live_errors)
                )
            receipt = _build_abandoned_version_receipt(
                checkpoint,
                reason=normalized_reason,
                infra_failure=infra_failure,
                timestamp=timestamp,
                previous_receipt_digest=previous_digest,
                project_root=project_root,
            )
            if receipts and receipt["version"] < receipts[-1]["version"]:
                raise AbandonedVersionLedgerError(
                    "abandon receipt version would regress the durable chain"
                )
            encoded_row = _canonical_abandon_json_bytes(
                receipt,
                label="abandon receipt",
            ) + b"\n"
            final_bytes = raw.encode("utf-8") + encoded_row
            if len(final_bytes) > _ABANDONED_VERSION_LEDGER_MAX_BYTES:
                raise AbandonedVersionLedgerError(
                    "abandon receipt ledger would exceed byte limit"
                )
            _atomic_publish_state_text(
                path,
                final_bytes.decode("utf-8"),
            )
        return receipt
    except AbandonedVersionLedgerError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise AbandonedVersionLedgerError(
            f"abandon receipt append failed: {type(exc).__name__}: {exc}"
        ) from exc


def find_abandoned_version_floor():
    """Delegate to abandoned_version_ledger."""
    return _ledger.find_abandoned_version_floor()


def abandoned_version_attempt_count(version):
    """Delegate to abandoned_version_ledger."""
    return _ledger.abandoned_version_attempt_count(version)


def compute_next_generation_v(current_v=None, max_committed_v=None, abandoned_floor=None):
    """Return the next free current-epoch label from authoritative inputs only.

    ``max_committed_v`` remains accepted for call-site compatibility but is
    intentionally ignored.  A bare commit is operator-reconciliation evidence,
    never published high-water or an allocation receipt.
    """

    del max_committed_v
    if current_v is None:
        current_v = find_current_v()
    if abandoned_floor is None:
        abandoned_floor = find_abandoned_version_floor()
    return max(int(current_v), int(abandoned_floor)) + 1


def publish_runtime_expected_head(reason: str = "", version=None) -> str:
    """Delegate to evolution_infra_git_publication."""
    return _gp.publish_runtime_expected_head(reason=reason, version=version)


def _require_national_epoch_registry_for_commit():
    """Delegate to evolution_infra_git_publication."""
    return _gp._require_national_epoch_registry_for_commit()


def _advance_national_epoch_high_water(version):
    """Delegate to evolution_infra_git_publication."""
    return _gp._advance_national_epoch_high_water(version)


def git_commit_bot(
    version,
    source_v,
    strategy_tag,
    rating_info="",
    parent2_v=None,
    *,
    official_certificate,
):
    """Delegate to evolution_infra_git_publication."""
    return _gp.git_commit_bot(
        version,
        source_v,
        strategy_tag,
        rating_info=rating_info,
        parent2_v=parent2_v,
        official_certificate=official_certificate,
    )


def _git_command_succeeds(*args: str) -> bool:
    """Delegate to evolution_infra_git_publication."""
    return _gp._git_command_succeeds(*args)


def _git_blob_bytes(ref: str, relative_path: str) -> bytes:
    """Delegate to evolution_infra_git_publication."""
    return _gp._git_blob_bytes(ref, relative_path)


def _publication_commit_paths(intent: dict) -> tuple[str, str]:
    """Delegate to evolution_infra_git_publication."""
    return _gp._publication_commit_paths(intent)


def _validate_publication_certificate_file(intent: dict) -> None:
    """Delegate to evolution_infra_git_publication."""
    return _gp._validate_publication_certificate_file(intent)


def _validate_existing_publication_commit(intent: dict, commit_oid: str) -> None:
    """Delegate to evolution_infra_git_publication."""
    return _gp._validate_existing_publication_commit(intent, commit_oid)


def _resolve_existing_publication_commit(intent: dict) -> str:
    """Delegate to evolution_infra_git_publication."""
    return _gp._resolve_existing_publication_commit(intent)


def _git_with_index(index_path: Path, *args: str) -> str:
    """Delegate to evolution_infra_git_publication."""
    return _gp._git_with_index(index_path, *args)


def _publication_commit_object(tree_oid: str, parent_oid: str, message: str) -> str:
    """Delegate to evolution_infra_git_publication."""
    return _gp._publication_commit_object(tree_oid, parent_oid, message)


def _validate_frozen_publication_tree(
    intent: dict,
    *,
    tree_oid: str,
    parent_oid: str,
) -> None:
    """Delegate to evolution_infra_git_publication."""
    return _gp._validate_frozen_publication_tree(
        intent, tree_oid=tree_oid, parent_oid=parent_oid
    )


def _create_publication_commit(intent: dict) -> str:
    """Delegate to evolution_infra_git_publication."""
    return _gp._create_publication_commit(intent)


def _validate_local_publication_refs(intent: dict, commit_oid: str) -> dict:
    """Delegate to evolution_infra_git_publication."""
    return _gp._validate_local_publication_refs(intent, commit_oid)


def remote_completion_ref_snapshot() -> dict[str, str]:
    """Delegate to evolution_infra_git_publication."""
    return _gp.remote_completion_ref_snapshot()


@contextmanager
def _publication_checkpoint_linearization_lock():
    """Delegate to evolution_infra_git_publication."""
    with _gp._publication_checkpoint_linearization_lock() as v:
        yield v


def _push_first_strict_publication(
    intent: dict,
    commit_oid: str,
    local_refs: dict,
    *,
    pre_push_authority,
) -> bool:
    """Delegate to evolution_infra_git_publication."""
    return _gp._push_first_strict_publication(
        intent,
        commit_oid,
        local_refs,
        pre_push_authority=pre_push_authority,
    )


def ensure_bot_git_publication(
    publication_intent: dict,
    *,
    official_certificate: dict,
    pre_push_authority=None,
) -> dict:
    """Delegate to evolution_infra_git_publication."""
    return _gp.ensure_bot_git_publication(
        publication_intent,
        official_certificate=official_certificate,
        pre_push_authority=pre_push_authority,
    )


def verify_remote_bot_publication(
    publication_intent: dict,
    *,
    local_state: dict | None = None,
) -> dict:
    """Delegate to evolution_infra_git_publication."""
    return _gp.verify_remote_bot_publication(
        publication_intent, local_state=local_state
    )


def git_get_parent(version):
    """Delegate to evolution_infra_git_publication."""
    return _gp.git_get_parent(version)


# ──────────────────────────────────────────────
# Generation Archiving
# ──────────────────────────────────────────────

def archive_generation(version, source_v, ckpt):
    """Delegate to evolution_infra_archiving."""
    return _arch.archive_generation(version, source_v, ckpt)


def _post_commit_archivist_receipt_validation(
    snapshot,
    version,
    source_v,
    *,
    require_pending=True,
):
    """Delegate to evolution_infra_archiving."""
    return _arch._post_commit_archivist_receipt_validation(
        snapshot, version, source_v, require_pending=require_pending
    )


def validate_post_commit_archivist_receipt(version, source_v):
    """Delegate to evolution_infra_archiving."""
    return _arch.validate_post_commit_archivist_receipt(version, source_v)


def consume_post_commit_archivist_receipt(version, source_v):
    """Delegate to evolution_infra_archiving."""
    return _arch.consume_post_commit_archivist_receipt(version, source_v)


# Rotation-plan record schema constants.  Authoritative definitions live in
# evolution_infra_archive_rotation (moved there with the rotation business).
# Re-exported here as aliases so legacy ``evolution_infra._ROTATION_*`` reads
# (e.g. tests) and the companion's own ``_ei._ROTATION_*`` references keep
# resolving.
from evolution_infra_archive_rotation import (  # noqa: F401, E402
    _ROTATION_PLAN_KIND,
    _ROTATION_WATERMARK_KIND,
    _ROTATION_SET_PLAN_KIND,
    _ROTATION_PLAN_KEYS,
    _ROTATION_WATERMARK_KEYS,
    _ROTATION_RECEIPT_KEYS,
    _ROTATION_SET_PLAN_KEYS,
    _ROTATION_SOURCE_SNAPSHOT_KEYS,
    _ROTATION_SUBJECT_KEYS,
)


def _rotation_rules():
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._rotation_rules()


def _rotation_digest(raw: bytes) -> str:
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._rotation_digest(raw)


def _rotation_paths(source_path: Path, version: int):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._rotation_paths(source_path, version)


def _rotation_set_plan_path(version: int):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._rotation_set_plan_path(version)


def _rotation_record(path: Path, *, kind: str, keys: frozenset[str]):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._rotation_record(path, kind=kind, keys=keys)


def _write_rotation_record(
    path: Path,
    payload: dict,
    *,
    kind: str,
    keys: frozenset[str],
):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._write_rotation_record(path, payload, kind=kind, keys=keys)


def _read_rotation_archive(path: Path):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._read_rotation_archive(path)


def _publish_rotation_archive(path: Path, raw: bytes):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._publish_rotation_archive(path, raw)


def _base_rotation_watermark(source_path: Path):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._base_rotation_watermark(source_path)


def _validate_rotation_plan(
    plan: dict,
    *,
    version: int,
    source_path: Path,
    raw: bytes,
    require_completed: bool,
    require_archive: bool,
):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._validate_rotation_plan(
        plan,
        version=version,
        source_path=source_path,
        raw=raw,
        require_completed=require_completed,
        require_archive=require_archive,
    )


def _load_rotation_watermark(source_path: Path, raw: bytes):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._load_rotation_watermark(source_path, raw)


def _rotation_receipt(plan: dict):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._rotation_receipt(plan)


def _rotation_digest_value(value):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._rotation_digest_value(value)


def _completed_rotation_plan_digest(subject):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._completed_rotation_plan_digest(subject)


def _rotation_subject_receipt(subject):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._rotation_subject_receipt(subject)


def _validate_archive_rotation_plan_shape(
    rotation_plan,
    *,
    version,
    publication_id=None,
):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._validate_archive_rotation_plan_shape(
        rotation_plan, version=version, publication_id=publication_id
    )


def expected_archive_rotation_receipts(
    rotation_plan,
    *,
    version,
    publication_id=None,
):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot.expected_archive_rotation_receipts(
        rotation_plan, version=version, publication_id=publication_id
    )


def _read_archive_rotation_plan_authority(version, *, missing_ok=False):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._read_archive_rotation_plan_authority(version, missing_ok=missing_ok)


def _publish_archive_rotation_plan_authority(plan):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot._publish_archive_rotation_plan_authority(plan)


def build_archive_rotation_plan(version, publication_id):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot.build_archive_rotation_plan(version, publication_id)


def validate_archive_rotation_plan(
    rotation_plan,
    *,
    version,
    publication_id=None,
):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot.validate_archive_rotation_plan(
        rotation_plan, version=version, publication_id=publication_id
    )


def archive_rotate_files(version, rotation_plan):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot.archive_rotate_files(version, rotation_plan)


def validate_archive_rotation_receipts(version, receipts, *, rotation_plan):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot.validate_archive_rotation_receipts(
        version, receipts, rotation_plan=rotation_plan
    )


def archive_old_logs(keep_generations=5):
    """Delegate to evolution_infra_archive_rotation."""
    return _rot.archive_old_logs(keep_generations=keep_generations)


# ──────────────────────────────────────────────
# Re-exports from extracted modules
# ──────────────────────────────────────────────

from daemon_management import (  # noqa: F401, E402
    daemon_proc, _daemon_lock, _atexit_registered, _daemon_shutting_down,
    start_daemon, stop_daemon, is_daemon_alive, daemon_monitor_thread,
    _drain_stdout,
)
from llm_query import (  # noqa: F401, E402
    _is_rate_limited, _is_quota_exceeded, _trim_to_budget,
    run_claude_query, parse_json_output,
)
from rate_limiter import rate_limiter, RateLimiter  # noqa: F401, E402
from code_verification import (  # noqa: F401, E402
    verify_code, check_code_size, run_smoke_test,
    run_decision_test_details, run_national_protocol_tests,
    run_import_contract_test,
)
