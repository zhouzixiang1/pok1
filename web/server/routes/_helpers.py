"""Shared data-building helpers for route modules.

Pure functions that accept pre-loaded data as parameters — no caching logic.
Each caller retains control of its own cache keys.
"""

import json
import re
from pathlib import Path

from rating_snapshot import build_strength_rows, h2h_winrate_for_bot
from evolution_infra import count_lines


def confidence(rd: float) -> str:
    if rd < 50:
        return "very_confident"
    if rd < 100:
        return "confident"
    if rd < 200:
        return "uncertain"
    return "very_uncertain"


def build_rating_row(name: str, r_data: dict, bot_stats: dict, h2h_data: dict) -> dict:
    r, rd = r_data["r"], r_data["rd"]
    display_rd = round(rd, 1)
    bs = bot_stats.get(name, {})
    wr = h2h_winrate_for_bot(name, h2h_data)
    conservative = r - 2 * rd
    return {
        "name": name,
        "rating": round(r, 1),
        "rd": display_rd,
        "sigma": round(r_data.get("sigma", 0.06), 4),
        "conservative_rating": round(conservative, 1),
        "confidence": confidence(display_rd),
        "last_period": r_data.get("last_period", ""),
        "win_rate": bs.get("win_rate"),
        "games": bs.get("games", 0),
        "h2h_avg_wr": round(wr, 4) if wr is not None else None,
        "h2h_source": "head_to_head",
        "leaderboard_score": round(max(0.0, min(1.0, 0.5 + (conservative - 1500) / 800)), 4),
        "rank_basis": "single_bot_detail",
        "strength_confidence": confidence(display_rd),
    }


def build_ranked_ratings(
    ratings_data: dict,
    bot_stats_data: dict,
    h2h_data: dict,
    *,
    active_bots: list[str] | None = None,
    match_history_path: Path | str | None = None,
) -> list[dict]:
    if not ratings_data:
        return []
    return build_strength_rows(
        ratings_data,
        bot_stats_data,
        h2h_data,
        active_bots=active_bots,
        match_history_path=match_history_path,
    )


def _formal_certification_summary(payload: dict) -> dict | None:
    """Project display-only round counts from an already validated receipt.

    This helper never decides certification authority.  Its caller first asks
    ``official_full_certified`` to validate the signed published certificate,
    ledger entry, deterministic receipt, and candidate bytes.  The projection
    only saves browser clients from reimplementing that trust boundary.
    """

    receipt = payload.get("official_deterministic_receipt")
    if not isinstance(receipt, dict):
        return None
    spec = receipt.get("spec")
    verdict = receipt.get("verdict")
    if not isinstance(spec, dict) or not isinstance(verdict, dict):
        return None

    fields = {
        "self_play_rounds": spec.get("self_play_rounds"),
        "opponent_rounds": spec.get("opponent_rounds"),
        "target_hands": spec.get("target_hands"),
        "rounds_requested": verdict.get("rounds_requested"),
        "rounds_run": verdict.get("rounds_run"),
    }
    if any(type(value) is not int or value < 0 for value in fields.values()):
        return None
    passed_rounds = fields["rounds_run"] if verdict.get("passed") is True else 0
    return {
        **fields,
        "passed_rounds": passed_rounds,
        "failed_rounds": fields["rounds_run"] - passed_rounds,
    }




def build_bot_summary(
    bot_dir: Path,
    bot_name: str,
    ratings: dict,
    bot_stats_data: dict,
    h2h_data: dict,
    strength_rows: dict[str, dict] | None = None,
) -> dict:
    version_match = re.search(r"\d+", bot_name)
    version = int(version_match.group()) if version_match else 0
    py_files = list(bot_dir.glob("*.py"))
    total_lines = sum(count_lines(f) for f in py_files)
    completed = (bot_dir / ".completed").exists()
    r_data = ratings.get(bot_name)
    rating_info = None
    if r_data:
        r, rd = r_data.get("r", 1500), r_data.get("rd", 350)
        rating_info = {"r": round(r, 1), "rd": round(rd, 1), "conservative": round(r - 2 * rd, 1)}
    bs = bot_stats_data.get(bot_name, {})
    strength = (strength_rows or {}).get(bot_name, {})
    wr = strength.get("h2h_avg_wr")
    if wr is None:
        raw_wr = h2h_winrate_for_bot(bot_name, h2h_data)
        wr = round(raw_wr, 4) if raw_wr is not None else None
    summary = {
        "name": bot_name, "version": version, "completed": completed,
        "total_lines": total_lines, "files": [f.name for f in py_files], "rating": rating_info,
        "win_rate": bs.get("win_rate"), "games": bs.get("games", 0),
        "h2h_avg_wr": wr,
    }
    try:
        from official_certification import official_full_certified, status_payload

        certification = status_payload(bot_dir)
        if not isinstance(certification, dict):
            raise TypeError("official certification status must be an object")
        try:
            formal = bool(official_full_certified(
                certification,
                bot_dir,
                require_published=True,
            ))
        except Exception:
            formal = False
        certification = dict(certification)
        certification.update({
            "formal_certified": formal,
            "formal_authority": "signed_full_v5" if formal else "none",
            "formal_summary": (
                _formal_certification_summary(certification) if formal else None
            ),
        })
        summary["official_certification"] = certification
    except Exception:
        summary["official_certification"] = {
            "bot": bot_name,
            "status": "official-unavailable",
            "status_label": "official-unavailable",
            "issues": ["certification_status_unavailable"],
            "formal_certified": False,
            "formal_authority": "none",
            "formal_summary": None,
        }
    for key in (
        "leaderboard_score", "rank_basis", "strength_confidence", "h2h_coverage",
        "h2h_games", "h2h_opponents", "h2h_opponents_total", "h2h_source",
        "h2h_weighted_wr", "selection_score", "selection_penalty",
        "primary_70_hand_match_score", "secondary_net_chips_total",
        "secondary_net_chips_mean", "strength_sample_count",
        "strength_order_contract", "strength_note",
    ):
        if key in strength:
            summary[key] = strength[key]
    return summary


def build_match_stats(stats_data: dict | None) -> dict:
    if not stats_data:
        return {
            "total_games": 0,
            "total_strength_samples": 0,
            "strength_sample_unit": "70_hand_match",
            "hands_per_strength_sample": 70,
            "total_pairs": 0,
            "total_periods": 0,
            "most_active_pair": "",
            "most_active_count": 0,
        }
    pairs = stats_data.get("pairs", {})
    total_games = stats_data.get("total_games", sum(pairs.values()))
    most_active = max(pairs.items(), key=lambda x: x[1]) if pairs else ("", 0)
    return {
        # ``total_games`` is retained for old dashboard clients only. One
        # daemon row is a complete 70-hand native TCP match, never one hand.
        "total_games": total_games,
        "total_strength_samples": total_games,
        "strength_sample_unit": "70_hand_match",
        "hands_per_strength_sample": 70,
        "total_pairs": len(pairs),
        "total_periods": stats_data.get("total_periods", 0),
        "most_active_pair": most_active[0], "most_active_count": most_active[1],
    }


def _bot_sort_key(name: str) -> int:
    m = re.search(r"\d+", name)
    return int(m.group()) if m else 0


def build_match_matrix(h2h_data: dict | None, ratings_data: dict | None, stats_data: dict | None) -> dict:
    """Build the current national-policy H2H win-rate matrix.

    Daemon pair counters are scheduling telemetry, not strength evidence.  A
    missing current H2H matrix therefore fails closed as an empty evidence
    result even if ratings or pair counters happen to be present.
    """
    if h2h_data:
        all_bots = set()
        for k in h2h_data:
            parts = k.split(" vs ")
            all_bots.update(parts)
        if ratings_data:
            all_bots &= set(ratings_data.keys())
        bot_names = sorted(all_bots, key=_bot_sort_key)
        idx = {name: i for i, name in enumerate(bot_names)}
        n = len(bot_names)
        wr_matrix = [[None] * n for _ in range(n)]
        for k, v in h2h_data.items():
            parts = k.split(" vs ")
            if len(parts) != 2:
                continue
            a, b = parts[0].strip(), parts[1].strip()
            if a in idx and b in idx:
                i, j = idx[a], idx[b]
                games = int(v.get("games", 0) or 0)
                if games > 0 and (
                    v.get("a_wins") is not None or v.get("draws") is not None
                ):
                    wr = (
                        float(v.get("a_wins", 0) or 0)
                        + 0.5 * float(v.get("draws", 0) or 0)
                    ) / games
                else:
                    wr = v.get("win_rate")
                if wr is not None:
                    wr_matrix[i][j] = round(wr, 4)
                    wr_matrix[j][i] = round(1.0 - wr, 4)
        return {
            "bots": bot_names,
            "matrix": wr_matrix,
            "source": "h2h",
            "evidence_available": True,
        }

    return {
        "bots": [],
        "matrix": [],
        "source": "h2h",
        "evidence_available": False,
    }


def read_jsonl(path: Path, limit: int | None = None, reverse: bool = True) -> list[dict]:
    if not path.exists():
        return []
    from evolution_infra import locked_file
    entries = []
    with locked_file(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if reverse:
        entries.reverse()
    if limit is not None:
        entries = entries[:limit]
    return entries


def _jsonl_bytes(payload: bytes) -> list[dict]:
    """Parse an immutable JSONL payload without consulting a live alias."""

    try:
        text = payload.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return []
    entries: list[dict] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def _strict_reset_receipt(results_dir: Path) -> dict | None:
    """Return the validated execute receipt for the sole active epoch."""

    try:
        from system_strict_bootstrap import load_policy_epoch_reset_receipt

        receipt, errors = load_policy_epoch_reset_receipt(results_dir)
    except Exception:
        return None
    if errors or not isinstance(receipt, dict):
        return None
    return receipt


def _strict_published_active_pool() -> list[str] | None:
    """Resolve the published active pool through the core trust boundary."""

    try:
        from bot_namespace import (
            FIRST_STRICT_POLICY_VERSION,
            parse_bot_version,
            version_sort_key,
        )
        from evolution_infra import get_published_active_bots_read_only

        active = list(get_published_active_bots_read_only())
    except Exception:
        return None
    if len(active) != len(set(active)):
        return None
    for name in active:
        version = parse_bot_version(name)
        if version is None or version < FIRST_STRICT_POLICY_VERSION:
            return None
    return sorted(active, key=version_sort_key)


def _filter_strict_match_rows(
    rows: list[dict],
    *,
    active_bots: set[str],
    evaluation_identity_digest: str,
) -> list[dict]:
    """Admit only complete current-identity matches between active bots."""

    from bot_namespace import EVALUATION_EPOCH

    accepted: list[dict] = []
    for row in reversed(rows):
        samples = row.get("net_chips_bot0")
        if (
            row.get("execution_mode") != "native_tcp"
            or row.get("evaluation_epoch") != EVALUATION_EPOCH
            or row.get("evaluation_identity_digest")
            != evaluation_identity_digest
            or row.get("bot0") not in active_bots
            or row.get("bot1") not in active_bots
            or row.get("bot0") == row.get("bot1")
            or row.get("strength_sample_unit") != "70_hand_match"
            or row.get("hands_per_strength_sample") != 70
            or row.get("strength_admitted") is not True
            or row.get("strength_complete") is not True
            or row.get("strength_compliance_passed") is not True
            or not isinstance(row.get("id"), str)
            or not isinstance(samples, list)
            or row.get("strength_sample_count") != len(samples)
            or not samples
        ):
            continue
        accepted.append(row)
    return accepted


def load_strict_strength_snapshot(results_dir: Path) -> dict:
    """Load one fail-closed dashboard projection of current strength evidence.

    HTTP readers never reopen the mutable compatibility aliases.  The returned
    data is the intersection of the validated epoch-reset receipt, the strict
    published active pool, and the last immutable evaluation-cycle bundle.
    This intentionally yields no ratings while the first strict bot has not yet
    been published.
    """

    try:
        from evaluation_bundle import load_current_strict_evaluation_bundle

        bundle = load_current_strict_evaluation_bundle(Path(results_dir))
    except Exception:
        return {"available": False, "reason": "evaluation_bundle_unavailable"}
    if not isinstance(bundle, dict):
        return {"available": False, "reason": "evaluation_bundle_unavailable"}
    if bundle.get("available") is not True:
        return {
            "available": False,
            "reason": str(bundle.get("reason") or "evaluation_bundle_unavailable"),
        }
    receipt = bundle.get("epoch_reset_receipt") or {}
    active = list(bundle.get("active_bots") or [])
    manifest = bundle.get("manifest")
    if not isinstance(manifest, dict):
        return {"available": False, "reason": "evaluation_manifest_missing"}
    identity = str(manifest.get("evaluation_identity_digest") or "")
    if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
        return {"available": False, "reason": "evaluation_identity_invalid"}

    active_set = set(active)
    rating_history: list[dict] = []
    for row in _jsonl_bytes((bundle.get("raw_append_logs") or {}).get("rating_history", b"")):
        # Old rows are never upgraded by filename, period, or bot number.  The
        # daemon stamped identity is mandatory, then fields are projected onto
        # the currently published pool so reaped bots do not reappear in charts.
        if row.get("evaluation_identity_digest") != identity:
            continue
        ratings = row.get("ratings")
        if not isinstance(ratings, dict):
            continue
        projected_ratings = {
            name: value
            for name, value in ratings.items()
            if name in active_set and isinstance(value, dict)
        }
        if not projected_ratings:
            continue
        win_rates = row.get("win_rates")
        projected_win_rates = {
            name: value
            for name, value in (win_rates.items() if isinstance(win_rates, dict) else ())
            if name in active_set and isinstance(value, dict)
        }
        clone = dict(row)
        clone["ratings"] = projected_ratings
        clone["win_rates"] = projected_win_rates
        rating_history.append(clone)

    match_rows = _jsonl_bytes(
        (bundle.get("raw_append_logs") or {}).get("match_history", b"")
    )
    selection = bundle.get("selection") or {}
    selection_rows = selection.get("rows") if isinstance(selection, dict) else None
    if not isinstance(selection_rows, list):
        return {"available": False, "reason": "evaluation_selection_rows_missing"}
    return {
        "available": True,
        "epoch": receipt.get("epoch"),
        "epoch_reset_receipt_digest": receipt.get("receipt_digest"),
        "active_bots": active,
        "evaluation_identity_digest": identity,
        "evaluation_manifest_digest": bundle.get("manifest_digest"),
        "ratings": bundle.get("ratings") or {},
        "h2h": bundle.get("h2h") or {},
        "bot_stats": bundle.get("bot_stats") or {},
        "daemon_stats": bundle.get("daemon_stats") or {},
        "selection_rows": [dict(row) for row in selection_rows if isinstance(row, dict)],
        "rating_history": rating_history,
        "match_history": _filter_strict_match_rows(
            match_rows,
            active_bots=active_set,
            evaluation_identity_digest=identity,
        ),
    }


def load_strict_pipeline_checkpoint(
    results_dir: Path,
    checkpoint_path: Path,
) -> dict | None:
    """Return only an epoch-bound, non-abandoned current checkpoint."""

    receipt = _strict_reset_receipt(Path(results_dir))
    if receipt is None:
        return None
    try:
        from checkpoint_schema import (
            FRESH_BOOTSTRAP_MODE,
            PUBLISHED_STRICT_PARENT_MODE,
            checkpoint_epoch_errors,
        )
        from evolution_infra import locked_file

        path = Path(checkpoint_path)
        if path.is_symlink() or not path.is_file():
            return None
        with locked_file(path, "r", encoding="utf-8") as handle:
            checkpoint = json.load(handle)
        if checkpoint_epoch_errors(checkpoint):
            return None
    except Exception:
        return None
    if checkpoint.get("stage") == "abandoned":
        return None
    workflow_run_id = checkpoint.get("workflow_run_id")
    if not isinstance(workflow_run_id, str) or not workflow_run_id.strip():
        return None

    binding = checkpoint.get("epoch_binding") or {}
    mode = binding.get("mode")
    active = _strict_published_active_pool()
    if active is None:
        return None
    if mode == FRESH_BOOTSTRAP_MODE:
        if binding.get("policy_epoch_reset_receipt_digest") != receipt.get(
            "receipt_digest"
        ):
            return None
        # The only legal non-empty state is the short publication handoff in
        # which the fresh v143 target itself has entered the active pool.
        target = f"national_v{checkpoint.get('next_v')}"
        if active and active != [target]:
            return None
    elif mode == PUBLISHED_STRICT_PARENT_MODE:
        if not active:
            return None
        identities = binding.get("published_parent_identities")
        if not isinstance(identities, list):
            return None
        parent_names = {
            row.get("bot") for row in identities if isinstance(row, dict)
        }
        if not parent_names or not parent_names.issubset(set(active)):
            return None
    else:
        return None
    return checkpoint


def read_strict_worker_failures(
    path: Path,
    *,
    results_dir: Path,
    checkpoint_path: Path,
    limit: int | None = None,
) -> list[dict]:
    """Read only failures explicitly bound to the current strict workflow."""

    checkpoint = load_strict_pipeline_checkpoint(results_dir, checkpoint_path)
    if checkpoint is None:
        return []
    from bot_namespace import EVALUATION_EPOCH

    workflow_run_id = checkpoint["workflow_run_id"]
    generation = checkpoint.get("next_v")
    accepted: list[dict] = []
    for row in read_jsonl(Path(path), limit=None):
        if (
            row.get("evaluation_epoch") != EVALUATION_EPOCH
            or row.get("workflow_run_id") != workflow_run_id
            or row.get("gen") != generation
            or row.get("category") not in {"worker", "gate"}
        ):
            continue
        accepted.append(row)
        if limit is not None and len(accepted) >= limit:
            break
    return accepted


def strict_observable_generation_versions(
    results_dir: Path,
    checkpoint_path: Path,
) -> set[int]:
    """Published strict generations plus the exact current strict workflow."""

    if _strict_reset_receipt(Path(results_dir)) is None:
        return set()
    from bot_namespace import parse_bot_version

    active = _strict_published_active_pool()
    if active is None:
        return set()
    versions = {
        version
        for name in active
        if (version := parse_bot_version(name)) is not None
    }
    checkpoint = load_strict_pipeline_checkpoint(results_dir, checkpoint_path)
    if checkpoint is not None and type(checkpoint.get("next_v")) is int:
        versions.add(checkpoint["next_v"])
    return versions


def downsample(entries: list[dict], max_points: int = 200) -> list[dict]:
    max_points = max(1, max_points)
    if len(entries) <= max_points:
        return entries
    step = max(1, len(entries) // max_points)
    sampled = entries[::step]
    if entries[-1] is not sampled[-1] and entries[-1] not in sampled:
        sampled.append(entries[-1])
    return sampled


def list_generation_dirs(
    results_dir: Path,
    *,
    allowed_versions: set[int] | None = None,
) -> list[dict]:
    if not results_dir.exists():
        return []
    versions = []
    dirs = sorted(
        (p for p in results_dir.iterdir()
         if p.is_dir() and p.name.startswith("v") and (p / "logs").is_dir()),
        key=lambda p: _bot_sort_key(p.name),
    )
    for p in dirs:
        version = int(p.name[1:])
        if allowed_versions is not None and version not in allowed_versions:
            continue
        files = sorted(f.name for f in (p / "logs").iterdir() if f.is_file())
        versions.append({"version": p.name, "files": files})
    return versions
