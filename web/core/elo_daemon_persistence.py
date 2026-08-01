"""Persistence / state-IO cluster, extracted from ``elo_daemon``.

Holds the rating-pool, H2H, bot-stats, daemon-state and evaluation-cycle
load/save helpers that were previously module-level functions in
``web/core/elo_daemon.py``.  The parent module (``elo_daemon``) keeps thin
delegate shells for every moved symbol so that:

* intra-module callers (``save_cycle``, ``main``, ``admit_internal_match_result``)
  continue to resolve through the parent namespace;
* test-suite direct calls such as
  ``elo_daemon._save_authoritative_evaluation_cycle(...)`` keep working; and
* any future ``monkeypatch.setattr(elo_daemon, "<name>", fake)`` is still
  observed because the delegates forward to this companion at call time.

Implementation contract
-----------------------
The companion imports the parent module as ``_ed`` and reads every
module-level constant / mutable global (``RESULTS_DIR``, ``RATINGS_FILE``,
``daemon_run_id``, ``daemon_last_cycle_manifest_digest``, ...) live through
``_ed.<name>``.  This is required because several of those globals
(``daemon_run_id``, ``daemon_evaluation_identity_digest``, the
``daemon_last_cycle_*`` pointers) are populated by ``main()`` long after
this module is first imported; reading them at import time would freeze a
``None`` snapshot.

Globals that this cluster mutates (``daemon_last_cycle_manifest_digest``,
``daemon_last_cycle_save_num``) are written back through
``_ed.<name> = ...`` so the parent module namespace stays authoritative.

Intra-cluster calls (one moved function calling another) remain bare, since
both caller and callee now live in this module.
"""

from __future__ import annotations

import json
import os
import time
import threading
import logging
from datetime import datetime

import elo_daemon as _ed

# Stable library helpers re-imported here (these are not daemon globals and
# do not change between runs).  Importing them directly keeps call sites
# readable instead of threading every constant through ``_ed.``.
from evolution_infra import (
    read_locked_json,
    write_locked_json,
    append_locked_jsonl,
    locked_file,
)
from glicko2 import Glicko2Player
from bot_action_stats import (
    MAX_ACTION_STATS_CYCLE_LAG,
    compute_all_bot_stats,
    get_global_stats,
)

_log = logging.getLogger("pok.daemon")

# Phase 0 follow-up: bot_action_stats scan runs ~260s for 2000 replays and would
# block the daemon main scheduling loop if called synchronously in save_cycle
# (observed stalling match cadence from ~10s to ~15min). Stats feed only the
# Master-prompt injection — no commit gate depends on them — so an async
# background refresh with a one-cycle-stale read is acceptable. These two
# module-level handles are private to the refresh helper and therefore live
# alongside it in this companion.
_action_stats_thread = None
_last_action_stats_refresh_start = 0.0


def bot_path(bot_name):
    from bot_namespace import ROLE_RATING_POOL, resolve_national_bot_spec

    spec = resolve_national_bot_spec(
        bot_name,
        ROLE_RATING_POOL,
        repo_root=_ed.PROJECT_ROOT,
    )
    if not spec.eligible:
        raise RuntimeError(
            f"rating bot is not a strict published policy artifact: {bot_name}:"
            + ";".join(spec.issues[:8])
        )
    return str(spec.entrypoint)


def _acquire_daemon_writer_lease():
    """Hold the single-writer rating lease for this process lifetime."""
    if _ed._daemon_writer_lease_fd is not None:
        raise RuntimeError("daemon writer lease is already held in this process")
    path = _ed.RESULTS_DIR / ".evaluation_daemon_writer.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(descriptor)
        raise RuntimeError("another rating daemon already holds the writer lease")
    # The lease fd is part of the parent module's authoritative global state
    # (read by _save_authoritative_evaluation_cycle at cycle publication), so
    # write it back through _ed rather than a local.
    _ed._daemon_writer_lease_fd = descriptor


def _release_daemon_writer_lease():
    descriptor = _ed._daemon_writer_lease_fd
    _ed._daemon_writer_lease_fd = None
    if descriptor is None:
        return
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _single_writer_daemon(func):
    def wrapped(*args, **kwargs):
        # CLI help is read-only documentation. It must remain inspectable while
        # the one-time epoch reset is still pending and must not create results
        # state or take the writer lease.
        import sys as _sys

        if not args and not kwargs and any(
            item in {"-h", "--help"} for item in _sys.argv[1:]
        ):
            return func()
        # Fail before creating results/, taking the writer lease, loading
        # aliases, or publishing an empty baseline.  The subprocess repeats
        # the parent-side daemon_management guard so a direct CLI invocation
        # and a reset/Popen race are both closed.
        #
        # The namespace guard runs first: a daemon launched under the wrong
        # namespace (e.g. without POK_CLOUD_RUNTIME=1) would silently validate
        # zero replays and fail closed inside save_cycle with an indirect
        # stored_h2h_raw_history_mismatch crash.  Surface that as an immediate,
        # actionable startup error instead.
        _ed._assert_bot_namespace_matches_env()
        from epoch_authority import require_policy_epoch_initialized

        require_policy_epoch_initialized("elo_daemon.cli")
        _ed.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        _acquire_daemon_writer_lease()
        try:
            return func(*args, **kwargs)
        finally:
            _release_daemon_writer_lease()

    return wrapped


def _write_heartbeat(
    *,
    activity_state: str = "scheduling_matches",
    active_bot_count: int | None = None,
):
    """Refresh the last_heartbeat field in .daemon_pid atomically.

    A fresh heartbeat distinguishes a live process from a stalled rating loop.
    It is process observability only and grants no separate job-queue authority.
    Safe no-op if the pid file is missing/invalid (for example during restart).
    """
    try:
        from evolution_infra import RESULTS_DIR
        pid_file = RESULTS_DIR / ".daemon_pid"
        if not pid_file.exists():
            return
        raw = pid_file.read_text().strip()
        try:
            info = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            info = {}
        # A retiring daemon must never overwrite the PID record of its
        # replacement.  Match both the process PID and the per-launch owner
        # token before publishing through the shared atomic heartbeat path.
        import hashlib

        owner_token = os.environ.get("POK_DAEMON_OWNER_TOKEN", "")
        owner_digest = (
            hashlib.sha256(owner_token.encode("ascii")).hexdigest()
            if owner_token
            else ""
        )
        if (
            not isinstance(info, dict)
            or int(info.get("pid") or 0) != os.getpid()
            or not owner_digest
            or info.get("owner_token_digest") != owner_digest
        ):
            return
        info["last_heartbeat"] = time.time()
        info["activity_state"] = str(activity_state)
        info["minimum_rating_pool_bots"] = _ed.MIN_RATING_POOL_BOTS
        if active_bot_count is not None:
            info["active_bot_count"] = max(0, int(active_bot_count))
        # C3: use a heartbeat-specific temp name to avoid colliding with
        # start_daemon's ".daemon_pid.tmp" when an orphan-cleanup restart races
        # with a heartbeat write (both used the identical path -> torn JSON ->
        # liveness probe false-negative). Also fsync before atomic replace so a
        # crash/power loss can't leave an empty/torn PID file.
        tmp = pid_file.with_suffix(".hb.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, json.dumps(info).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(pid_file))
    except Exception:
        # Heartbeat is advisory; never let it crash the main loop.
        pass


def _reconcile_rating_pool_membership(
    previous_active_bots,
    ratings,
    h2h,
    bot_stats,
    *,
    save_num=0,
    verbose=False,
):
    """Refresh the strict published pool without manufacturing strength data."""
    # Read get_active_bots through the parent module so a test
    # monkeypatch.setattr(elo_daemon, "get_active_bots", ...) is observed.
    active_bots = _ed.get_active_bots()
    # The rating pool requires ROLE_RATING_POOL eligibility (a signed full
    # official certificate).  A published-but-uncertified bot (staging tier)
    # is NOT rating-eligible: bot_path() would raise
    # ``signed_full_official_certificate_required`` and crash the daemon when
    # pick_matches tries to launch it.  Filter to only rating-eligible bots
    # so the daemon degrades gracefully (idle when too few are certified)
    # instead of crash-looping on every published staging bot.
    rating_eligible = []
    for bot in active_bots:
        try:
            bot_path(bot)
        except Exception:
            if verbose:
                _log.info(
                    "Skipping rating-ineligible bot %s (no signed certificate)",
                    bot,
                )
            continue
        rating_eligible.append(bot)
    active_bots = rating_eligible
    added = sorted(set(active_bots) - set(previous_active_bots))
    removed = sorted(set(previous_active_bots) - set(active_bots))
    for bot in active_bots:
        if bot not in ratings:
            ratings[bot] = Glicko2Player(last_play_period=save_num)
            if verbose:
                _log.info("New bot: %s (r=1500, rd=350)", bot)
    for bot in list(ratings):
        if bot not in active_bots:
            del ratings[bot]
            bot_stats.pop(bot, None)
            if verbose:
                _log.info("Retired: %s", bot)
    active_set = set(active_bots)
    h2h = {
        key: value
        for key, value in h2h.items()
        if set(key.split(" vs ")).issubset(active_set)
    }
    return active_bots, h2h, added, removed


def save_ratings(
    ratings,
    save_num=None,
    *,
    h2h_snapshot=None,
    bot_stats_snapshot=None,
):
    from evaluation_data_identity import current_evaluation_digest

    RESULTS_DIR = _ed.RESULTS_DIR
    os.makedirs(RESULTS_DIR, exist_ok=True)
    evaluation_identity_digest = current_evaluation_digest(RESULTS_DIR)
    data = {}
    for name, p in ratings.items():
        d = p.to_dict()
        d["last_period"] = datetime.now().isoformat(timespec="seconds")
        data[name] = d

    h2h_for_history = None
    bot_stats_for_history = None
    strength_rows_for_history = None
    if save_num is not None:
        # Validate the raw-history projection before writing either ratings or
        # rating-history.  A same-coverage but altered cached H2H matrix must
        # halt the period, not leak into a partially updated strength view.
        from rating_snapshot import choose_h2h_source

        h2h_input = (
            dict(h2h_snapshot)
            if h2h_snapshot is not None
            else load_h2h()
        )
        h2h_selection = choose_h2h_source(
            list(ratings.keys()),
            h2h_input,
            _ed.MATCH_HISTORY_FILE,
            expected_evaluation_identity_digest=evaluation_identity_digest,
            replay_dir=_ed.REPLAY_DIR,
        )
        if h2h_selection.get("integrity_ok") is not True:
            raise RuntimeError(
                "rating history H2H integrity invalid:"
                + ";".join(
                    str(issue)
                    for issue in (h2h_selection.get("integrity_issues") or [])[:8]
                )
            )
        h2h_for_history = h2h_selection["h2h"]
        bot_stats_for_history = (
            dict(bot_stats_snapshot)
            if bot_stats_snapshot is not None
            else load_bot_stats()
        )
        try:
            from rating_snapshot import build_strength_rows

            strength_rows_for_history = {
                row["name"]: row
                for row in build_strength_rows(
                    ratings,
                    bot_stats_for_history,
                    h2h_for_history,
                    active_bots=list(ratings.keys()),
                    match_history_path=_ed.MATCH_HISTORY_FILE,
                    h2h_is_authoritative=False,
                    expected_evaluation_identity_digest=(
                        evaluation_identity_digest
                    ),
                    replay_dir=_ed.REPLAY_DIR,
                )
            }
        except Exception as exc:
            raise RuntimeError(
                "rating history strength projection failed closed"
            ) from exc

    write_locked_json(_ed.RATINGS_FILE, data)

    if save_num is not None:
        history_file = RESULTS_DIR / "rating_history.jsonl"
        h2h = h2h_for_history or {}
        bot_stats = bot_stats_for_history or {}
        from tool_helpers import compute_h2h_avg_winrate
        strength_rows = strength_rows_for_history or {}
        win_rates = {}
        for name in ratings:
            wr = compute_h2h_avg_winrate(name, h2h)
            bs = bot_stats.get(name, {})
            games = bs.get("games", 0)
            strength = strength_rows.get(name, {})
            if wr is not None:
                win_rates[name] = {
                    "h2h_avg_wr": round(wr, 4),
                    "games": games,
                    "leaderboard_score": strength.get("leaderboard_score"),
                    "h2h_coverage": strength.get("h2h_coverage"),
                    "h2h_source": strength.get("h2h_source"),
                }
            elif games > 0:
                win_rates[name] = {
                    "games": games,
                    "leaderboard_score": strength.get("leaderboard_score"),
                    "h2h_coverage": strength.get("h2h_coverage"),
                    "h2h_source": strength.get("h2h_source"),
                }
        snapshot = {
            "period": save_num,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "daemon_run_id": _ed.daemon_run_id,  # None for ad-hoc calls; set in main()
            "evaluation_epoch": "national_tcp_policy_v1",
            "execution_mode": "native_tcp",
            "evaluation_identity_digest": evaluation_identity_digest,
            "ratings": {name: {"r": p.r, "rd": p.rd, "sigma": p.sigma} for name, p in ratings.items()},
            "win_rates": win_rates,
        }
        append_locked_jsonl(history_file, snapshot)


def load_stats():
    return read_locked_json(_ed.STATS_FILE, default={"pairs": {}, "total_games": 0})


def save_stats(stats):
    os.makedirs(_ed.RESULTS_DIR, exist_ok=True)
    write_locked_json(_ed.STATS_FILE, stats)


def load_h2h():
    from evaluation_data_identity import ensure_evaluation_data_identity

    RESULTS_DIR = _ed.RESULTS_DIR
    ensure_evaluation_data_identity(RESULTS_DIR)
    return read_locked_json(_ed.H2H_FILE, default={})


def save_h2h(h2h):
    from evaluation_data_identity import ensure_evaluation_data_identity

    RESULTS_DIR = _ed.RESULTS_DIR
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ensure_evaluation_data_identity(RESULTS_DIR)
    write_locked_json(_ed.H2H_FILE, _persistable_h2h(h2h))


def _persistable_h2h(h2h):
    """Canonical H2H payload shared by persistence and derived selection.

    One H2H game is already one complete 70-hand native TCP strength sample;
    dropping it here while match_history retained it made H2H and selection
    disagree inside the same published cycle.
    """
    return {
        key: value
        for key, value in (h2h or {}).items()
        if isinstance(value, dict) and int(value.get("games", 0) or 0) >= 1
    }


def _h2h_with_win_rates(h2h):
    out = {}
    for k, v in (h2h or {}).items():
        entry = dict(v)
        g = int(entry.get("games", 0) or 0)
        if g > 0:
            entry["win_rate"] = round((entry.get("a_wins", 0) + 0.5 * entry.get("draws", 0)) / g, 4)
        else:
            entry["win_rate"] = 0.5
        out[k] = entry
    return out


def load_bot_stats():
    return read_locked_json(_ed.BOT_STATS_FILE, default={})


def save_bot_stats(bot_stats):
    os.makedirs(_ed.RESULTS_DIR, exist_ok=True)
    write_locked_json(_ed.BOT_STATS_FILE, bot_stats)


def _opponent_coverage(bot, active_bots, h2h):
    """Fraction of active opponents this bot has H2H data for."""
    from evolution_infra import pair_key

    n_opponents = 0
    for other in active_bots:
        if other == bot:
            continue
        k = pair_key(bot, other)
        if h2h.get(k, {}).get("games", 0) > 0:
            n_opponents += 1
    total = len(active_bots) - 1
    return n_opponents / total if total > 0 else 1.0


def _current_rating_history_tail(max_rows=10):
    RESULTS_DIR = _ed.RESULTS_DIR
    path = RESULTS_DIR / "rating_history.jsonl"
    if not path.exists():
        return []
    try:
        with locked_file(path, "r", encoding="utf-8") as handle:
            parsed = []
            for line in handle:
                try:
                    row = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(row, dict):
                    parsed.append(row)
    except (OSError, UnicodeDecodeError):
        return []
    if _ed.daemon_run_id is not None:
        tail = []
        for row in reversed(parsed):
            if row.get("daemon_run_id") != _ed.daemon_run_id:
                break
            tail.append(row)
        parsed = list(reversed(tail))
    return parsed[-max(1, int(max_rows)):]


def _project_rating_history_tail(
    active_bots,
    save_num,
    *,
    expected_evaluation_identity_digest,
    max_rows=10,
):
    """Keep frozen trend context inside the current cycle's active pool/cutoff."""
    active = {str(name) for name in active_bots}
    expected_identity = str(expected_evaluation_identity_digest or "")
    if len(expected_identity) != 64:
        return []
    projected = []
    for row in _current_rating_history_tail(max_rows=max_rows):
        if not isinstance(row, dict):
            continue
        if (
            row.get("evaluation_epoch") != "national_tcp_policy_v1"
            or row.get("execution_mode") != "native_tcp"
            or row.get("evaluation_identity_digest") != expected_identity
        ):
            continue
        try:
            if int(row.get("period", -1)) > int(save_num):
                continue
        except (TypeError, ValueError):
            continue
        clone = dict(row)
        clone["ratings"] = {
            str(name): value
            for name, value in (row.get("ratings") or {}).items()
            if str(name) in active
        }
        clone["win_rates"] = {
            str(name): value
            for name, value in (row.get("win_rates") or {}).items()
            if str(name) in active
        }
        projected.append(clone)
    return projected


def _save_authoritative_evaluation_cycle(
    ratings,
    h2h_out,
    bot_stats,
    stats,
    save_num,
    active_bots,
    *,
    _test_only_allow_unleased=False,
):
    """Commit one crash-recoverable evaluation transaction."""
    # Mutate the parent module globals so the rest of the daemon observes the
    # new committed-cycle pointers (recovery / subsequent save_cycle calls).
    from evaluation_bundle import (
        evaluation_cycle_lock,
        publish_evaluation_cycle_manifest,
    )
    from evaluation_data_identity import current_evaluation_digest
    from rating_snapshot import build_strength_rows, choose_h2h_source

    RESULTS_DIR = _ed.RESULTS_DIR
    with evaluation_cycle_lock(RESULTS_DIR, exclusive=True):
        current_identity = current_evaluation_digest(RESULTS_DIR)
        if (
            _ed.daemon_evaluation_identity_digest is not None
            and current_identity != _ed.daemon_evaluation_identity_digest
        ):
            raise RuntimeError(
                "evaluation identity changed while daemon was running; "
                "stale in-memory state cannot cross the migration fence"
            )
        # Check the caller's in-memory/cache projection against all currently
        # retained raw evidence *before* any alias/log write.  This is also the
        # direct-call fence for code paths that bypass ``save_cycle``.
        pre_rotation_h2h = choose_h2h_source(
            list(active_bots),
            _persistable_h2h(h2h_out),
            _ed.MATCH_HISTORY_FILE,
            expected_evaluation_identity_digest=current_identity,
            replay_dir=_ed.REPLAY_DIR,
        )
        if pre_rotation_h2h.get("integrity_ok") is not True:
            raise RuntimeError(
                "cannot publish evaluation cycle with H2H/raw replay mismatch:"
                + ";".join(
                    str(issue)
                    for issue in (pre_rotation_h2h.get("integrity_issues") or [])[:8]
                )
            )

        # History retention changes the evidence cutoff.  Rebuild the stored
        # cache from the retained raw suffix rather than publishing a matrix
        # that contains W/L/D whose replay bytes were just pruned.
        _ed._rotate_jsonl(_ed.MATCH_HISTORY_FILE, _ed.MAX_MATCH_HISTORY_LINES)
        retained_h2h = choose_h2h_source(
            list(active_bots),
            {},
            _ed.MATCH_HISTORY_FILE,
            expected_evaluation_identity_digest=current_identity,
            replay_dir=_ed.REPLAY_DIR,
        )
        if retained_h2h.get("integrity_ok") is not True:
            raise RuntimeError("retained match-history H2H projection is invalid")
        canonical_h2h = _persistable_h2h(retained_h2h["h2h"])
        if isinstance(h2h_out, dict):
            h2h_out.clear()
            h2h_out.update(canonical_h2h)
        cycle_stats = dict(stats or {})
        cycle_stats["total_games"] = sum(
            int((value or {}).get("games", 0) or 0)
            for value in (bot_stats or {}).values()
        ) // 2
        cycle_stats["pairs"] = {
            key: int((value or {}).get("games", 0) or 0)
            for key, value in canonical_h2h.items()
        }
        save_h2h(canonical_h2h)
        save_bot_stats(bot_stats)
        # save_ratings also appends the period history. It runs before the
        # compact selection rows so the cycle manifest is always the final
        # commit marker for every authoritative payload.
        save_ratings(
            ratings,
            save_num=save_num,
            h2h_snapshot=canonical_h2h,
            bot_stats_snapshot=bot_stats,
        )
        save_stats(cycle_stats)
        # The immutable pointer records byte cutoffs for both append-only logs.
        # Rotate before publication so a successful manifest never points at a
        # prefix that is subsequently rewritten shorter.
        _ed._rotate_jsonl(RESULTS_DIR / "rating_history.jsonl", _ed.MAX_RATING_HISTORY_LINES)
        selection_rows = build_strength_rows(
            ratings,
            bot_stats,
            canonical_h2h,
            active_bots=list(active_bots),
            match_history_path=_ed.MATCH_HISTORY_FILE,
            h2h_is_authoritative=False,
            expected_evaluation_identity_digest=current_identity,
            replay_dir=_ed.REPLAY_DIR,
        )
        write_locked_json(
            _ed.SELECTION_SNAPSHOT_FILE,
            {
                "schema_version": 1,
                "save_num": int(save_num),
                "daemon_run_id": str(_ed.daemon_run_id or "adhoc"),
                "active_bots": sorted(str(name) for name in active_bots),
                "rows": selection_rows,
                "rating_history_tail": _project_rating_history_tail(
                    active_bots,
                    save_num,
                    expected_evaluation_identity_digest=current_identity,
                    max_rows=10,
                ),
            },
        )
        manifest = publish_evaluation_cycle_manifest(
            save_num=save_num,
            daemon_run_id=_ed.daemon_run_id,
            active_bots=list(active_bots),
            results_dir=RESULTS_DIR,
            evaluation_identity_digest=current_identity,
            expected_previous_manifest_digest=_ed.daemon_last_cycle_manifest_digest,
            expected_previous_save_num=_ed.daemon_last_cycle_save_num,
            require_predecessor_match=(
                _ed.daemon_evaluation_identity_digest is not None
            ),
            writer_lease_fd=_ed._daemon_writer_lease_fd,
            _test_only_allow_unleased=_test_only_allow_unleased,
            _lock_held=True,
        )
        _ed.daemon_last_cycle_manifest_digest = str(manifest["manifest_digest"])
        _ed.daemon_last_cycle_save_num = int(manifest["save_num"])
        return manifest


def _load_committed_daemon_state():
    """Hydrate daemon memory only from the last immutable committed cycle."""
    RESULTS_DIR = _ed.RESULTS_DIR
    REPLAY_DIR = _ed.REPLAY_DIR
    from evaluation_bundle import (
        MANIFEST_FILENAME,
        recover_published_evaluation_bundle,
    )

    cycle_manifest = RESULTS_DIR / MANIFEST_FILENAME
    if cycle_manifest.exists():
        bundle = recover_published_evaluation_bundle(RESULTS_DIR)
        if not bundle.get("available"):
            raise RuntimeError(
                "committed evaluation cycle is not recoverable: "
                f"{bundle.get('reason')} {bundle.get('issues', [])}"
            )
        try:
            ratings = {
                str(name): Glicko2Player.from_dict(payload)
                for name, payload in bundle["ratings"].items()
            }
        except Exception as exc:
            raise RuntimeError(
                f"committed rating payload cannot be hydrated: {type(exc).__name__}"
            ) from exc
        pending = REPLAY_DIR / ".pending"
        if pending.exists():
            if pending.is_symlink() or not pending.is_dir():
                raise RuntimeError("unsafe staged replay directory during recovery")
            import shutil

            shutil.rmtree(pending)
        committed_replay_ids = set()
        for line in bundle["raw_append_logs"]["match_history"].splitlines():
            try:
                row = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if isinstance(row, dict) and row.get("id"):
                committed_replay_ids.add(str(row["id"]))
        if REPLAY_DIR.is_dir() and not REPLAY_DIR.is_symlink():
            for replay_path in REPLAY_DIR.glob("*.json"):
                if (
                    replay_path.is_file()
                    and not replay_path.is_symlink()
                    and not replay_path.name.startswith(".")
                    and replay_path.name not in committed_replay_ids
                ):
                    replay_path.unlink(missing_ok=True)
        _ed.daemon_last_cycle_manifest_digest = str(bundle["manifest_digest"])
        _ed.daemon_last_cycle_save_num = int(
            bundle["manifest"].get("save_num", 0) or 0
        )
        return (
            ratings,
            dict(bundle["h2h"]),
            dict(bundle["bot_stats"]),
            dict(bundle["daemon_stats"]),
            int(bundle["manifest"].get("save_num", 0) or 0),
        )

    # No pointer means no state has ever committed in this identity.  A crash
    # during the initial baseline/first save may nevertheless leave aliases or
    # an orphan immutable directory.  They are explicitly uncommitted, so reset
    # them to the empty state before publishing a save_num=0 baseline.
    aliases = (
        _ed.RATINGS_FILE,
        _ed.H2H_FILE,
        _ed.BOT_STATS_FILE,
        _ed.SELECTION_SNAPSHOT_FILE,
        _ed.STATS_FILE,
    )
    from evaluation_bundle import APPEND_LOGS, CYCLES_DIRNAME, evaluation_cycle_lock

    with evaluation_cycle_lock(RESULTS_DIR, exclusive=True):
        for path in aliases:
            path.unlink(missing_ok=True)
        for filename in APPEND_LOGS.values():
            (RESULTS_DIR / filename).unlink(missing_ok=True)
        orphan_cycles = RESULTS_DIR / CYCLES_DIRNAME
        if orphan_cycles.exists():
            if orphan_cycles.is_symlink() or not orphan_cycles.is_dir():
                raise RuntimeError("unsafe uncommitted evaluation cycle path")
            import shutil

            shutil.rmtree(orphan_cycles)
        pending = REPLAY_DIR / ".pending"
        if pending.exists():
            if pending.is_symlink() or not pending.is_dir():
                raise RuntimeError("unsafe staged replay directory during reset")
            import shutil

            shutil.rmtree(pending)
        if REPLAY_DIR.is_dir() and not REPLAY_DIR.is_symlink():
            for replay_path in REPLAY_DIR.glob("*.json"):
                if replay_path.is_file() and not replay_path.is_symlink():
                    replay_path.unlink(missing_ok=True)
    _ed.daemon_last_cycle_manifest_digest = None
    _ed.daemon_last_cycle_save_num = None
    return {}, {}, {}, {"pairs": {}, "total_games": 0}, 0


def _refresh_action_stats_async(active_bots):
    """Kick off a background bot_action_stats refresh; non-blocking.

    Skips if a previous scan is still running (avoids thread pile-up when
    save_cycle fires faster than the scan completes). The on-disk
    bot_action_stats.json is updated in the worker via write_locked_json
    (fcntl-protected), so concurrent readers stay safe.
    """
    global _action_stats_thread, _last_action_stats_refresh_start
    if _action_stats_thread is not None and _action_stats_thread.is_alive():
        return
    now = time.monotonic()
    if now - _last_action_stats_refresh_start < _ed.ACTION_STATS_REFRESH_INTERVAL_SEC:
        return
    _last_action_stats_refresh_start = now

    bots_snapshot = list(active_bots)
    RESULTS_DIR = _ed.RESULTS_DIR
    REPLAY_DIR = _ed.REPLAY_DIR

    def _worker():
        try:
            t0 = time.perf_counter()
            from evaluation_bundle import load_published_evaluation_bundle

            committed = load_published_evaluation_bundle(RESULTS_DIR)
            if not committed.get("available"):
                _log.warning(
                    "bot_action_stats skipped: committed evaluation bundle unavailable"
                )
                return
            committed_identity = str(
                committed["manifest"].get("evaluation_identity_digest") or ""
            )
            committed_manifest_digest = str(committed["manifest_digest"])
            allowed_replay_ids = set()
            active_snapshot = set(bots_snapshot)
            for raw_line in committed["raw_append_logs"]["match_history"].splitlines():
                try:
                    row = json.loads(raw_line.decode("utf-8"))
                except Exception:
                    continue
                if (
                    isinstance(row, dict)
                    and row.get("evaluation_identity_digest") == committed_identity
                    and row.get("id")
                    and row.get("bot0") in active_snapshot
                    and row.get("bot1") in active_snapshot
                ):
                    allowed_replay_ids.add(str(row["id"]))
            etag_path = REPLAY_DIR / ".stats_etag.json"
            per_opp = compute_all_bot_stats(
                bots_snapshot,
                REPLAY_DIR,
                force_full=False,
                etag_path=etag_path,
                allowed_replay_ids=allowed_replay_ids,
                expected_evaluation_identity_digest=committed_identity,
            )
            flat = {
                bot: get_global_stats(per_opp, bot)
                for bot in bots_snapshot
                if bot in per_opp
            }
            from evaluation_bundle import evaluation_cycle_lock

            # Publish the flat and per-opponent diagnostic views as one pair so
            # generation snapshot capture cannot combine two different scans.
            with evaluation_cycle_lock(RESULTS_DIR, exclusive=True):
                from evaluation_bundle import (
                    _read_manifest_locked,
                    validated_evaluation_identity_digest,
                )

                if validated_evaluation_identity_digest(RESULTS_DIR) != committed_identity:
                    _log.info(
                        "bot_action_stats scan discarded: evaluation identity advanced"
                    )
                    return
                current_cycle = _read_manifest_locked(
                    RESULTS_DIR / "evaluation_cycle_manifest.json"
                ) or {}
                if str(current_cycle.get("evaluation_identity_digest") or "") != committed_identity:
                    _log.info(
                        "bot_action_stats scan discarded: committed identity advanced"
                    )
                    return
                try:
                    current_save_num = int(current_cycle.get("save_num", -1))
                    source_save_num = int(committed["manifest"].get("save_num", -1))
                except (TypeError, ValueError):
                    return
                cycle_lag = current_save_num - source_save_num
                if cycle_lag < 0 or cycle_lag > MAX_ACTION_STATS_CYCLE_LAG:
                    _log.info(
                        "bot_action_stats scan discarded: cycle lag %s exceeds bound %s",
                        cycle_lag,
                        MAX_ACTION_STATS_CYCLE_LAG,
                    )
                    return
                write_locked_json(RESULTS_DIR / "bot_action_stats.json", flat)
                # Phase 3: also persist the per-opponent breakdown (nested shape).
                write_locked_json(
                    RESULTS_DIR / "bot_action_stats_per_opp.json",
                    per_opp,
                )
                write_locked_json(
                    RESULTS_DIR / "bot_action_stats_source.json",
                    {
                        "evaluation_identity_digest": committed_identity,
                        "source_cycle_manifest_digest": committed_manifest_digest,
                        "source_cycle_save_num": source_save_num,
                        "published_against_cycle_manifest_digest": str(
                            current_cycle.get("manifest_digest") or ""
                        ),
                        "published_against_cycle_save_num": current_save_num,
                        "cycle_lag_at_publish": cycle_lag,
                        "max_cycle_lag": MAX_ACTION_STATS_CYCLE_LAG,
                        "allowed_replay_count": len(allowed_replay_ids),
                        "generated_at": time.time(),
                    },
                )
            _log.info(
                "bot_action_stats scan: %.2fs, %d bots (async incremental etag @ %s)",
                time.perf_counter() - t0, len(flat), etag_path.name,
            )

        except Exception as e:
            _log.warning("Bot action stats computation failed (non-fatal): %s", e)

    _action_stats_thread = threading.Thread(
        target=_worker, daemon=True, name="action-stats-refresh"
    )
    _action_stats_thread.start()
