"""Unified strength snapshot for leaderboard and evolution decisions.

The daemon keeps several overlapping signals: Glicko-2 ratings, aggregate bot
stats, a head-to-head matrix, and the append-only match history.  Consumers used
to read these files independently, which made sparse H2H snapshots look more
authoritative than the richer match history.  This module normalizes them into
one set of fields for both the dashboard and the evolution tools.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from pathlib import Path
from typing import Any


CURRENT_EVALUATION_EPOCH = "national_tcp_policy_v1"
CURRENT_EXECUTION_MODE = "native_tcp"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _expected_identity(value: Any) -> str | None:
    """Normalize an already-validated current evaluation identity.

    This module never derives or falls back to an identity from a history row.
    Callers that own a current manifest must pass its exact digest; missing or
    malformed authority makes history inadmissible.
    """

    candidate = str(value or "")
    return candidate if _SHA256_RE.fullmatch(candidate) else None


def _default_match_history_file() -> Path:
    import evolution_infra
    return evolution_infra.MATCH_HISTORY_FILE


def _default_replay_dir() -> Path:
    import evolution_infra

    return Path(evolution_infra.RESULTS_DIR) / "match_replay"


def _pair_key(a: str, b: str) -> str:
    return f"{a} vs {b}" if a < b else f"{b} vs {a}"


def _version_key(name: str) -> int:
    m = re.search(r"\d+", name or "")
    return int(m.group()) if m else 0


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _entry_points_for_bot(bot_name: str, key: str, entry: dict[str, Any]) -> tuple[float, int] | None:
    parts = [p.strip() for p in key.split(" vs ")]
    if len(parts) != 2 or bot_name not in parts:
        return None
    games = int(entry.get("games", 0) or 0)
    if games <= 0:
        return None
    draws = int(entry.get("draws", 0) or 0)
    wins = int(entry.get("a_wins", 0) or 0) if parts[0] == bot_name else int(entry.get("b_wins", 0) or 0)
    return wins + 0.5 * draws, games


def h2h_winrate_for_bot(bot_name: str, h2h_data: dict[str, Any]) -> float | None:
    """Equal-opponent H2H win rate, counting draws as half a win."""
    rates: list[float] = []
    for key, entry in (h2h_data or {}).items():
        if not isinstance(entry, dict):
            continue
        points_games = _entry_points_for_bot(bot_name, key, entry)
        if points_games is None:
            continue
        points, games = points_games
        rates.append(points / games)
    if not rates:
        return None
    return sum(rates) / len(rates)


def _add_pair_result(h2h: dict[str, dict[str, Any]], bot_a: str, bot_b: str,
                     wins_a: int, wins_b: int, draws: int) -> None:
    if not bot_a or not bot_b or bot_a == bot_b:
        return
    total = int(wins_a or 0) + int(wins_b or 0) + int(draws or 0)
    if total <= 0:
        return
    key = _pair_key(bot_a, bot_b)
    entry = h2h.setdefault(key, {"games": 0, "a_wins": 0, "b_wins": 0, "draws": 0})
    first, _second = key.split(" vs ")
    if bot_a == first:
        entry["a_wins"] += int(wins_a or 0)
        entry["b_wins"] += int(wins_b or 0)
    else:
        entry["a_wins"] += int(wins_b or 0)
        entry["b_wins"] += int(wins_a or 0)
    entry["draws"] += int(draws or 0)
    entry["games"] += total
    entry["win_rate"] = round((entry["a_wins"] + 0.5 * entry["draws"]) / entry["games"], 4)


def _iter_match_history(path: Path):
    if not path.exists():
        return
    try:
        from evolution_infra import locked_file
        with locked_file(path, "r", encoding="utf-8") as f:
            lines = list(f)
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


_HISTORY_REPLAY_HEADER_FIELDS = (
    "id",
    "timestamp",
    "execution_mode",
    "evaluation_epoch",
    "bot0",
    "bot1",
    "bot0_wins",
    "bot1_wins",
    "draws",
    "evaluation_identity_digest",
    "strength_sample_unit",
    "hands_per_strength_sample",
    "strength_admitted",
    "strength_complete",
    "strength_compliance_passed",
    "strength_sample_count",
    "net_chips_bot0",
    "strength_order",
    "native_match_timing_plan",
    "native_match_timing_plan_digest",
)


def _current_artifact_hashes_for_replay(raw_replay: dict[str, Any]) -> dict[str, str] | None:
    """Bind a raw replay's declared artifacts back to active source bytes.

    ``validate_native_replay`` proves that an execution identity is internally
    well formed.  H2H reconstruction needs the second half of that proof: the
    two declared hashes must still name the actual strict artifact directories
    under this checkout, rather than merely being self-consistent JSON.
    """

    labels = (raw_replay.get("bot0"), raw_replay.get("bot1"))
    if (
        not all(isinstance(label, str) and label for label in labels)
        or labels[0] == labels[1]
    ):
        return None
    try:
        from bot_artifact import hash_path
        import evolution_infra

        bots_root = Path(evolution_infra.PROJECT_ROOT) / "bots"
        return {
            str(label): hash_path(bots_root / str(label))
            for label in labels
        }
    except Exception:
        return None


def _load_verified_history_replay(
    entry: dict[str, Any],
    *,
    expected_evaluation_identity_digest: str,
    replay_dir: Path,
) -> dict[str, Any] | None:
    """Open the exact raw replay behind one history row, or reject it closed."""

    replay_id = entry.get("id")
    expected_sha256 = entry.get("replay_sha256")
    if (
        not isinstance(replay_id, str)
        or not replay_id
        or "/" in replay_id
        or "\\" in replay_id
        or Path(replay_id).name != replay_id
        or not replay_id.endswith(".json")
        or replay_id.startswith(".")
        or not isinstance(expected_sha256, str)
        or _SHA256_RE.fullmatch(expected_sha256) is None
    ):
        return None
    try:
        root_info = replay_dir.lstat()
        if replay_dir.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
            return None
        root = replay_dir.resolve(strict=True)
        candidate = root / replay_id
        candidate_info = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISREG(candidate_info.st_mode):
            return None
        if candidate.resolve(strict=True).parent != root:
            return None
        raw_bytes = candidate.read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != expected_sha256:
            return None
        raw_replay = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw_replay, dict):
        return None
    try:
        from replay_analysis import validate_native_replay

        validation = validate_native_replay(
            raw_replay,
            expected_evaluation_identity_digest=expected_evaluation_identity_digest,
            expected_replay_id=replay_id,
        )
    except Exception:
        return None
    if not validation.accepted:
        return None
    if dict(validation.artifact_hashes) != _current_artifact_hashes_for_replay(
        raw_replay
    ):
        return None
    if any(raw_replay.get(field) != entry.get(field) for field in _HISTORY_REPLAY_HEADER_FIELDS):
        return None
    return raw_replay


def _admitted_70_hand_history_sample(
    entry: dict[str, Any],
    *,
    expected_evaluation_identity_digest: str,
    replay_dir: Path | str | None = None,
) -> list[int] | None:
    """Return proven strength samples, otherwise fail the history row closed."""

    expected_identity = _expected_identity(expected_evaluation_identity_digest)
    if (
        expected_identity is None
        or entry.get("evaluation_epoch") != CURRENT_EVALUATION_EPOCH
        or entry.get("execution_mode") != CURRENT_EXECUTION_MODE
        or entry.get("evaluation_identity_digest") != expected_identity
        or entry.get("strength_sample_unit") != "70_hand_match"
        or int(entry.get("hands_per_strength_sample", 0) or 0) != 70
        or entry.get("strength_admitted") is not True
        or entry.get("strength_complete") is not True
        or entry.get("strength_compliance_passed") is not True
    ):
        return None
    try:
        from national_native import require_native_match_timing_plan

        timing_plan = require_native_match_timing_plan(
            entry.get("native_match_timing_plan"),
            hands=70,
            requested_timeout_sec=None,
        )
        if entry.get("native_match_timing_plan_digest") != timing_plan.digest():
            return None
    except Exception:
        return None
    raw_samples = entry.get("net_chips_bot0")
    if not isinstance(raw_samples, list):
        return None
    try:
        samples = [int(value) for value in raw_samples]
        wins = int(entry.get("bot0_wins", 0) or 0)
        losses = int(entry.get("bot1_wins", 0) or 0)
        draws = int(entry.get("draws", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not samples:
        return None
    if int(entry.get("strength_sample_count", -1) or 0) != len(samples):
        return None
    from strength_order import summarize_70_hand_net_chips

    summary = summarize_70_hand_net_chips(samples)
    if (
        summary["samples"] != wins + losses + draws
        or summary["positive_matches"] != wins
        or summary["negative_matches"] != losses
        or summary["zero_matches"] != draws
    ):
        return None
    resolved_replay_dir = (
        Path(replay_dir) if replay_dir is not None else _default_replay_dir()
    )
    if _load_verified_history_replay(
        entry,
        expected_evaluation_identity_digest=expected_identity,
        replay_dir=resolved_replay_dir,
    ) is None:
        return None
    return samples


def reconstruct_h2h_from_match_history(
    active_bots: list[str] | set[str] | tuple[str, ...],
    match_history_path: Path | str | None = None,
    *,
    expected_evaluation_identity_digest: str,
    replay_dir: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Rebuild active-pool H2H from admitted national strength history only."""
    active = set(active_bots or [])
    expected_identity = _expected_identity(expected_evaluation_identity_digest)
    if len(active) < 2 or expected_identity is None:
        return {}
    path = Path(match_history_path) if match_history_path is not None else _default_match_history_file()
    resolved_replay_dir = (
        Path(replay_dir) if replay_dir is not None else path.parent / "match_replay"
    )
    rebuilt: dict[str, dict[str, Any]] = {}
    for entry in _iter_match_history(path) or []:
        if _admitted_70_hand_history_sample(
            entry,
            expected_evaluation_identity_digest=expected_identity,
            replay_dir=resolved_replay_dir,
        ) is None:
            continue
        bot_a = entry.get("bot0") or entry.get("bot_a")
        bot_b = entry.get("bot1") or entry.get("bot_b")
        if bot_a not in active or bot_b not in active:
            continue
        wins_a = entry.get("bot0_wins", entry.get("bot_a_wins", entry.get("wins_a", 0)))
        wins_b = entry.get("bot1_wins", entry.get("bot_b_wins", entry.get("wins_b", 0)))
        draws = entry.get("draws", 0)
        _add_pair_result(rebuilt, str(bot_a), str(bot_b), int(wins_a or 0), int(wins_b or 0), int(draws or 0))
    return rebuilt


def national_chip_metrics_from_match_history(
    active_bots: list[str] | set[str] | tuple[str, ...],
    match_history_path: Path | str | None = None,
    *,
    expected_evaluation_identity_digest: str,
    replay_dir: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate the secondary chip signal from complete 70-hand samples only."""

    from strength_order import summarize_70_hand_net_chips

    active = set(active_bots or [])
    expected_identity = _expected_identity(expected_evaluation_identity_digest)
    if expected_identity is None:
        return {}
    path = Path(match_history_path) if match_history_path is not None else _default_match_history_file()
    resolved_replay_dir = (
        Path(replay_dir) if replay_dir is not None else path.parent / "match_replay"
    )
    samples_by_bot: dict[str, list[int]] = {name: [] for name in active}
    for entry in _iter_match_history(path) or []:
        bot_a = str(entry.get("bot0") or "")
        bot_b = str(entry.get("bot1") or "")
        if bot_a not in active or bot_b not in active or bot_a == bot_b:
            continue
        samples = _admitted_70_hand_history_sample(
            entry,
            expected_evaluation_identity_digest=expected_identity,
            replay_dir=resolved_replay_dir,
        )
        if samples is None:
            continue
        samples_by_bot[bot_a].extend(samples)
        samples_by_bot[bot_b].extend(-value for value in samples)
    return {
        name: summarize_70_hand_net_chips(samples)
        for name, samples in samples_by_bot.items()
        if samples
    }


def h2h_coverage(h2h_data: dict[str, Any], active_bots: list[str] | set[str] | tuple[str, ...]) -> dict[str, Any]:
    active = set(active_bots or [])
    total_pairs = len(active) * (len(active) - 1) // 2
    covered_pairs = 0
    per_bot = {name: 0 for name in active}
    for key, entry in (h2h_data or {}).items():
        if not isinstance(entry, dict) or int(entry.get("games", 0) or 0) <= 0:
            continue
        parts = [p.strip() for p in key.split(" vs ")]
        if len(parts) != 2 or parts[0] not in active or parts[1] not in active:
            continue
        covered_pairs += 1
        per_bot[parts[0]] += 1
        per_bot[parts[1]] += 1
    return {
        "covered_pairs": covered_pairs,
        "total_pairs": total_pairs,
        "coverage": covered_pairs / total_pairs if total_pairs else 1.0,
        "per_bot": per_bot,
    }


def filter_h2h_to_active(
    h2h_data: dict[str, Any] | None,
    active_bots: list[str] | set[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Return only valid H2H rows whose two endpoints are currently active."""
    active = set(active_bots or [])
    filtered = {}
    for key, entry in (h2h_data or {}).items():
        if not isinstance(entry, dict):
            continue
        parts = [part.strip() for part in str(key).split(" vs ")]
        if len(parts) != 2 or parts[0] == parts[1]:
            continue
        if parts[0] not in active or parts[1] not in active:
            continue
        filtered[str(key)] = entry
    return filtered


def _canonical_active_h2h_projection(
    h2h_data: dict[str, Any] | None,
    active_bots: list[str] | set[str] | tuple[str, ...],
) -> tuple[dict[str, dict[str, Any]] | None, list[str]]:
    """Normalize an active-pool H2H projection without trusting its rows.

    Stored H2H is only a cache/projection of verified raw match history.  This
    helper is intentionally strict so a same-coverage but altered W/L/D row
    cannot look authoritative merely because its shape is plausible.
    """

    active = set(active_bots or [])
    normalized: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for raw_key, raw_entry in (h2h_data or {}).items():
        parts = [part.strip() for part in str(raw_key).split(" vs ")]
        # Inactive rows have zero current authority and are deliberately not a
        # reason to reject the active pool.
        if len(parts) != 2 or any(part not in active for part in parts):
            continue
        if parts[0] == parts[1] or not isinstance(raw_entry, dict):
            issues.append(f"h2h_active_row_invalid:{raw_key}")
            continue
        fields: dict[str, int] = {}
        for field in ("games", "a_wins", "b_wins", "draws"):
            value = raw_entry.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                issues.append(f"h2h_active_count_invalid:{raw_key}:{field}")
                break
            fields[field] = value
        if len(fields) != 4:
            continue
        if fields["games"] != (
            fields["a_wins"] + fields["b_wins"] + fields["draws"]
        ) or fields["games"] <= 0:
            issues.append(f"h2h_active_count_mismatch:{raw_key}")
            continue
        raw_key_rate = round(
            (fields["a_wins"] + 0.5 * fields["draws"])
            / fields["games"],
            4,
        )
        # Validate `win_rate` in the stored key's own A-vs-B orientation
        # before canonicalizing pair order below.
        if "win_rate" in raw_entry:
            raw_rate = raw_entry.get("win_rate")
            try:
                observed_rate = float(raw_rate)
            except (TypeError, ValueError, OverflowError):
                issues.append(f"h2h_active_win_rate_invalid:{raw_key}")
                continue
            if (
                isinstance(raw_rate, bool)
                or not math.isfinite(observed_rate)
                or abs(observed_rate - raw_key_rate) > 1e-9
            ):
                issues.append(f"h2h_active_win_rate_mismatch:{raw_key}")
                continue
        canonical_key = _pair_key(parts[0], parts[1])
        if canonical_key in normalized:
            issues.append(f"h2h_active_pair_duplicate:{canonical_key}")
            continue
        if parts[0] != canonical_key.split(" vs ", 1)[0]:
            fields["a_wins"], fields["b_wins"] = (
                fields["b_wins"],
                fields["a_wins"],
            )
        normalized[canonical_key] = {
            **fields,
            "win_rate": round(
                (fields["a_wins"] + 0.5 * fields["draws"])
                / fields["games"],
                4,
            ),
        }
    return (None if issues else normalized), issues


def choose_h2h_source(
    active_bots: list[str] | set[str] | tuple[str, ...],
    stored_h2h: dict[str, Any] | None,
    match_history_path: Path | str | None = None,
    *,
    expected_evaluation_identity_digest: str,
    replay_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Use verified raw-history H2H and report any stored-cache divergence."""
    active = list(active_bots or [])
    expected_identity = _expected_identity(expected_evaluation_identity_digest)
    if expected_identity is None:
        empty: dict[str, dict[str, Any]] = {}
        empty_coverage = h2h_coverage(empty, active)
        return {
            "h2h": empty,
            "stored_h2h": empty,
            "source": "match_history_identity_invalid",
            "coverage": empty_coverage,
            "stored_coverage": empty_coverage,
            "rebuilt_coverage": empty_coverage,
            "integrity_ok": False,
            "integrity_issues": ["match_history_identity_invalid"],
        }
    stored, stored_issues = _canonical_active_h2h_projection(
        stored_h2h,
        active,
    )
    stored_cov = h2h_coverage(stored or {}, active)
    rebuilt = reconstruct_h2h_from_match_history(
        active,
        match_history_path,
        expected_evaluation_identity_digest=expected_identity,
        replay_dir=replay_dir,
    )
    rebuilt_cov = h2h_coverage(rebuilt, active)
    integrity_issues = list(stored_issues)
    # An empty process/file cache is recoverable: rebuild it from raw history.
    # Any non-empty active projection must match exactly, not merely have the
    # same pair coverage, before a publication may treat it as coherent.
    if stored and stored != rebuilt:
        integrity_issues.append("stored_h2h_raw_history_mismatch")
    return {
        "h2h": rebuilt,
        "stored_h2h": stored or {},
        "source": "match_history_rebuilt",
        "coverage": rebuilt_cov,
        "stored_coverage": stored_cov,
        "rebuilt_coverage": rebuilt_cov,
        "integrity_ok": not integrity_issues,
        "integrity_issues": integrity_issues,
    }


def _rating_fields(raw: Any) -> tuple[float, float, float, str]:
    if isinstance(raw, dict):
        return (
            float(raw.get("r", 1500.0)),
            float(raw.get("rd", 350.0)),
            float(raw.get("sigma", 0.06)),
            str(raw.get("last_period", "")),
        )
    return (
        float(getattr(raw, "r", 1500.0)),
        float(getattr(raw, "rd", 350.0)),
        float(getattr(raw, "sigma", 0.06)),
        str(getattr(raw, "last_period", "")),
    )


def _confidence_label(rd: float) -> str:
    if rd < 50:
        return "very_confident"
    if rd < 100:
        return "confident"
    if rd < 200:
        return "uncertain"
    return "very_uncertain"


def _strength_confidence(coverage: float, h2h_games: int, rd: float, opponents_total: int) -> str:
    target_games = max(100, opponents_total * 10)
    sample_ok = h2h_games >= target_games
    if coverage >= 0.8 and sample_ok and rd < 100:
        return "high"
    if coverage >= 0.4 and h2h_games >= max(50, target_games // 2) and rd < 180:
        return "medium"
    return "low"


def _selection_score(score: float, strength_confidence: str) -> tuple[float, float]:
    """Score used by evolution mechanics when choosing top opponents/parents.

    Keep the public leaderboard score intact, but discount low-confidence rows so
    a high point estimate with weak evidence cannot become a mechanical parent or
    top-opponent pick ahead of a similarly strong, better-established bot.
    """
    penalty = 0.03 if strength_confidence == "low" else 0.0
    return _clamp(score - penalty), penalty


def _strength_note(
    *,
    confidence: str,
    coverage: float,
    h2h_games: int,
    h2h_opponents: int,
    opponents_total: int,
    rd: float,
    basis: str,
) -> str:
    labels = {"high": "高", "medium": "中", "low": "低"}
    note = (
        f"强度置信={labels.get(confidence, confidence)}；"
        f"H2H覆盖 {h2h_opponents}/{opponents_total}，"
        f"{h2h_games} 局，RD={rd:.1f}，依据={basis}"
    )
    if confidence == "low":
        note += "；进化选择分已降权"
    return note


def _score_components(
    r: float,
    rd: float,
    h2h_avg_wr: float | None,
    h2h_games: int,
    coverage: float,
    opponents_total: int,
    stats_wr: float | None,
) -> tuple[float, str]:
    target_games = max(100, opponents_total * 10)
    h2h_reliability = 0.0
    if h2h_avg_wr is not None:
        h2h_reliability = min(1.0, coverage / 0.8) * min(1.0, h2h_games / target_games)

    rating_score = _clamp(0.5 + (r - 1500.0) / 800.0)
    uncertainty_penalty = _clamp(rd / 700.0, 0.0, 0.5)
    conservative_score = _clamp(rating_score - uncertainty_penalty)
    stats_score = _clamp(float(stats_wr)) if stats_wr is not None else 0.5
    h2h_score = _clamp(h2h_avg_wr) if h2h_avg_wr is not None else 0.5

    h2h_weight = 0.55 * h2h_reliability
    rating_weight = 0.80 - 0.45 * h2h_reliability
    stats_weight = 1.0 - h2h_weight - rating_weight
    score = h2h_score * h2h_weight + conservative_score * rating_weight + stats_score * stats_weight

    if h2h_reliability >= 0.95:
        basis = "active_h2h_plus_conservative"
    elif h2h_reliability > 0.0:
        basis = "mixed_low_h2h_coverage"
    else:
        basis = "conservative_glicko_fallback"
    return _clamp(score), basis


def build_strength_rows(
    ratings_data: dict[str, Any],
    bot_stats_data: dict[str, Any] | None = None,
    stored_h2h: dict[str, Any] | None = None,
    active_bots: list[str] | set[str] | tuple[str, ...] | None = None,
    match_history_path: Path | str | None = None,
    *,
    h2h_is_authoritative: bool = False,
    expected_evaluation_identity_digest: str | None = None,
    replay_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return dashboard-ready rows sorted by composite leaderboard strength."""
    if not ratings_data:
        return []
    active = list(active_bots or ratings_data.keys())
    active = [name for name in active if name in ratings_data]
    expected_identity = _expected_identity(expected_evaluation_identity_digest)
    if expected_identity is None:
        # A cached H2H matrix has no authority without an exact current
        # evaluation identity.  In particular, never let an old process or
        # migration-incomplete checkout sort parents/opponents from stored
        # W/L/D merely because the cache happens to be parseable.
        empty_h2h: dict[str, dict[str, Any]] = {}
        selected = {
            "h2h": empty_h2h,
            "source": "match_history_identity_invalid",
            "coverage": h2h_coverage(empty_h2h, active),
            "integrity_ok": False,
            "integrity_issues": ["match_history_identity_invalid"],
        }
    else:
        selected = choose_h2h_source(
            active,
            stored_h2h or {},
            match_history_path,
            expected_evaluation_identity_digest=expected_identity,
            replay_dir=replay_dir,
        )
        if selected.get("integrity_ok") is not True:
            # Never blend a stored cache with raw evidence that disagrees.
            # The dashboard may still show conservative Glicko data, but its
            # H2H score/capability has zero authority until publication repairs
            # the exact raw-history projection.
            empty_h2h: dict[str, dict[str, Any]] = {}
            selected = {
                **selected,
                "h2h": empty_h2h,
                "source": "match_history_integrity_failed",
                "coverage": h2h_coverage(empty_h2h, active),
            }
    h2h = selected["h2h"]
    coverage_meta = selected["coverage"]
    opponents_total = max(0, len(active) - 1)
    bot_stats_data = bot_stats_data or {}
    chip_metrics = (
        national_chip_metrics_from_match_history(
            active,
            match_history_path,
            expected_evaluation_identity_digest=expected_identity,
            replay_dir=replay_dir,
        )
        if expected_identity is not None
        else {}
    )

    rows: list[dict[str, Any]] = []
    for name in active:
        r, rd, sigma, last_period = _rating_fields(ratings_data.get(name, {}))
        bs = bot_stats_data.get(name, {}) if isinstance(bot_stats_data, dict) else {}
        per_opponent: list[float] = []
        total_points = 0.0
        total_games = 0
        for key, entry in (h2h or {}).items():
            if not isinstance(entry, dict):
                continue
            points_games = _entry_points_for_bot(name, key, entry)
            if points_games is None:
                continue
            points, games = points_games
            per_opponent.append(points / games)
            total_points += points
            total_games += games
        h2h_avg_wr = sum(per_opponent) / len(per_opponent) if per_opponent else None
        h2h_weighted_wr = total_points / total_games if total_games else None
        h2h_opponents = int(coverage_meta["per_bot"].get(name, 0))
        h2h_coverage_ratio = h2h_opponents / opponents_total if opponents_total > 0 else 1.0
        stats_wr = bs.get("win_rate") if isinstance(bs, dict) else None
        score, basis = _score_components(
            r=r,
            rd=rd,
            h2h_avg_wr=h2h_avg_wr,
            h2h_games=total_games,
            coverage=h2h_coverage_ratio,
            opponents_total=opponents_total,
            stats_wr=stats_wr,
        )
        strength_conf = _strength_confidence(h2h_coverage_ratio, total_games, rd, opponents_total)
        selection_score, selection_penalty = _selection_score(score, strength_conf)
        chip_summary = chip_metrics.get(name, {})
        conservative = r - 2 * rd
        display_rd = round(rd, 1)
        row = {
            "name": name,
            "rating": round(r, 1),
            "rd": display_rd,
            "sigma": round(sigma, 4),
            "conservative_rating": round(conservative, 1),
            "confidence": _confidence_label(display_rd),
            "last_period": last_period,
            "win_rate": stats_wr,
            "games": bs.get("games", 0) if isinstance(bs, dict) else 0,
            "h2h_avg_wr": round(h2h_avg_wr, 4) if h2h_avg_wr is not None else None,
            "h2h_weighted_wr": round(h2h_weighted_wr, 4) if h2h_weighted_wr is not None else None,
            "h2h_games": total_games,
            "h2h_opponents": h2h_opponents,
            "h2h_opponents_total": opponents_total,
            "h2h_coverage": round(h2h_coverage_ratio, 4),
            "h2h_source": selected["source"],
            "h2h_source_coverage": round(float(coverage_meta["coverage"]), 4),
            "leaderboard_score": round(score, 4),
            "selection_score": round(selection_score, 4),
            "selection_penalty": round(selection_penalty, 4),
            "primary_70_hand_match_score": (
                round(h2h_weighted_wr, 4)
                if h2h_weighted_wr is not None
                else stats_wr
            ),
            "secondary_net_chips_total": chip_summary.get("secondary_net_chips_total"),
            "secondary_net_chips_mean": (
                round(float(chip_summary["secondary_net_chips_mean"]), 2)
                if chip_summary.get("secondary_net_chips_mean") is not None
                else None
            ),
            "strength_sample_count": int(chip_summary.get("samples", 0) or 0),
            "strength_order_contract": [
                "70_hand_positive_result",
                "net_chips_magnitude",
            ],
            "rank_basis": basis,
            "strength_confidence": strength_conf,
            "strength_note": _strength_note(
                confidence=strength_conf,
                coverage=h2h_coverage_ratio,
                h2h_games=total_games,
                h2h_opponents=h2h_opponents,
                opponents_total=opponents_total,
                rd=rd,
                basis=basis,
            ),
        }
        rows.append(row)

    from strength_order import strength_order_key

    rows.sort(
        key=lambda row: (*strength_order_key(row), _version_key(row["name"])),
        reverse=True,
    )
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return rows


def strength_score_map(
    ratings_data: dict[str, Any],
    bot_stats_data: dict[str, Any] | None = None,
    stored_h2h: dict[str, Any] | None = None,
    active_bots: list[str] | set[str] | tuple[str, ...] | None = None,
    match_history_path: Path | str | None = None,
    *,
    expected_evaluation_identity_digest: str | None = None,
) -> dict[str, float]:
    return {
        row["name"]: float(row["leaderboard_score"])
        for row in build_strength_rows(
            ratings_data,
            bot_stats_data,
            stored_h2h,
            active_bots,
            match_history_path,
            expected_evaluation_identity_digest=expected_evaluation_identity_digest,
        )
    }
