#!/usr/bin/env python3
"""Long-running counterfactual data collector for the opponent-aware value net.

Designed to run detached under ``nohup`` for hours. Each pass:
  1. Reads the live strongest classic pool from .evolution_pok glicko ratings.
  2. Uses stable opponent-level train/val/held-out partitions. An opponent is
     never rotated across splits within one collection run.
  3. Rotates early/middle/late hand windows so cross-hand features receive
     actual match-history coverage. Probes emit hand, tail, and match deltas.
  4. Runs port-isolated probes with at most four concurrent workers.
  5. Appends annotated rows to cumulative train/val/held_out JSONL and logs
     progress.

Usage (detached):
    nohup python bots/neural_national_lab/tools/longrun_collect_oppmodel.py \
      --candidate bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp \
      --out-dir bots/neural_national_lab/data/oppmodel/longrun \
      --passes 60 --workers 4 > collect.log 2>&1 &

Check progress:
    tail -f bots/neural_national_lab/data/oppmodel/longrun/progress.log
    wc -l bots/neural_national_lab/data/oppmodel/longrun/cf_*.jsonl
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
_APPEND_LOCK = threading.Lock()
_TAG_DIGEST_CACHE: dict[tuple[str, str], str] = {}
DECK_SEED_SLOTS_PER_PASS = 1024
DEFAULT_DECK_SEED_BASE = 5_000_000
DEFAULT_DECK_SEED_GUARD = 10
DEFAULT_BOT_SEED_BASE = 1_000_000


def _operator_root() -> Path:
    try:
        common_dir = subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        root = Path(common_dir).resolve().parent
        return root.parent if root.name == ".evolution_pok" else root
    except (OSError, subprocess.SubprocessError):
        return ROOT


OPERATOR_ROOT = _operator_root()
DEFAULT_RATINGS = OPERATOR_ROOT / ".evolution_pok" / "web" / "core" / "results" / "glicko_ratings.json"
FALLBACK_POOL = [
    "national_v135", "national_v114", "national_v73", "national_v121",
    "national_v122", "national_v120", "national_v119", "national_v123",
]


def _resolve(p: str) -> Path:
    raw = Path(p)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def live_strongest(
    ratings_path: Path, n: int = 12, *, allow_fallback: bool = False
) -> list[str]:
    """Read the live strongest classic bots by conservative Glicko."""
    try:
        with ratings_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        rows = []
        for name, value in data.items():
            if not isinstance(value, dict) or not name.startswith("national_v"):
                continue
            rating = value.get("rating", value.get("r"))
            rd = value.get("rd")
            if rating is None or rd is None:
                continue
            rows.append((name, float(rating) - 2.0 * float(rd)))
        if not rows:
            raise ValueError("ratings file contains no usable national bots")
        rows.sort(key=lambda x: x[1], reverse=True)
        return [k for k, _ in rows[:n]]
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if allow_fallback:
            return list(FALLBACK_POOL[:n])
        raise RuntimeError(f"cannot read live Glicko ratings at {ratings_path}: {exc}") from exc


def probe_one(candidate: str, opponent_dir: str, split: str, name: str,
              hands: int, seed_base: int, bot_seed_base: int, out_dir: str,
              timeout_sec: int, min_hand: int, max_decisions: int,
              max_alternatives: int, ratings_path: str, probe_workers: int,
              decision_sampling: str) -> tuple[int, int, str]:
    """Run one probe and append value and opponent-response rows."""
    tag = f"{split}_{name}_s{seed_base}_b{bot_seed_base}"
    tmp_jsonl = Path(out_dir) / f"_tmp_{tag}.jsonl"
    tmp_behavior = Path(out_dir) / f"_tmp_behavior_{tag}.jsonl"
    cmd = [
        sys.executable, str(TOOLS / "native_tcp_counterfactual_probe.py"),
        "--candidate", candidate, "--opponent", opponent_dir,
        "--hands", str(hands), "--seed-base", str(seed_base),
        "--bot-seed-base", str(bot_seed_base),
        "--min-hand", str(min_hand),
        "--max-decisions", str(max_decisions),
        "--max-alternatives", str(max_alternatives), "--stage", "any",
        "--probe-workers", str(probe_workers),
        "--decision-sampling", decision_sampling,
        "--timeout-sec", str(timeout_sec),
        "--output", str(Path(out_dir) / f"_tmp_{tag}.json"),
        "--jsonl-output", str(tmp_jsonl),
        "--behavior-jsonl-output", str(tmp_behavior),
    ]
    try:
        forced_waves = math.ceil(max_decisions * max_alternatives / max(1, probe_workers))
        subprocess.run(cmd, cwd=str(ROOT), timeout=timeout_sec * (1 + forced_waves) + 60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    def append_annotated(tmp_path: Path, cumulative_name: str) -> int:
        if not tmp_path.exists():
            return 0
        cum = Path(out_dir) / cumulative_name
        annotated: list[dict] = []
        with open(tmp_path, "r", encoding="utf-8") as src:
            for line in src:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                obj["_split"] = split
                obj["_opponent_label"] = name
                obj["_seed_base"] = seed_base
                obj["_bot_seed_base"] = bot_seed_base
                obj["_collection_hands"] = hands
                obj["_min_hand"] = min_hand
                obj["_ratings_path"] = ratings_path
                annotated.append(obj)
        def row_key(obj: dict) -> tuple:
            return (
                str(obj.get("_opponent_label") or obj.get("opponent") or ""),
                obj.get("deck_seed_base"),
                obj.get("bot_seed_base"),
                obj.get("hand"),
                obj.get("hand_decision_index"),
            )
        appended = 0
        with _APPEND_LOCK:
            existing: dict[tuple, dict] = {}
            if cum.exists():
                with cum.open("r", encoding="utf-8") as current:
                    for line in current:
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        existing[row_key(obj)] = obj
            pending = []
            for obj in annotated:
                key = row_key(obj)
                previous = existing.get(key)
                if previous is not None:
                    if previous != obj:
                        raise RuntimeError(
                            f"non-deterministic duplicate row in {cumulative_name}: {key}"
                        )
                    continue
                existing[key] = obj
                pending.append(json.dumps(obj, separators=(",", ":")) + "\n")
            if pending:
                with cum.open("a", encoding="utf-8") as dst:
                    dst.writelines(pending)
                appended = len(pending)
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return appended

    rows = append_annotated(tmp_jsonl, f"cf_{split}.jsonl")
    behavior_rows = append_annotated(
        tmp_behavior, f"opponent_actions_{split}.jsonl"
    )
    try:
        (Path(out_dir) / f"_tmp_{tag}.json").unlink(missing_ok=True)
    except Exception:
        pass
    return rows, behavior_rows, name


def _stable_split(name: str) -> str:
    bucket = int(hashlib.sha256(name.encode("ascii")).hexdigest()[:8], 16) % 10
    if bucket == 0:
        return "held_out"
    if bucket == 1:
        return "val"
    return "train"


def _completed_tag_commit(name: str) -> str | None:
    try:
        version = int(name.removeprefix("national_v"))
    except ValueError:
        return None
    tag = f"national-bot-v{version}^{{commit}}"
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--verify", tag],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        subprocess.run(
            ["git", "cat-file", "-e", f"{commit}:bots/{name}/national_bot.py"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return commit
    except (OSError, subprocess.SubprocessError):
        return None


def _ratings_snapshot(path: Path) -> dict[str, dict[str, float]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    result = {}
    for name, values in payload.items():
        if not isinstance(values, dict) or not name.startswith("national_v"):
            continue
        rating = values.get("rating", values.get("r"))
        rd = values.get("rd")
        if rating is None or rd is None:
            continue
        result[name] = {
            "rating": float(rating),
            "rd": float(rd),
            "conservative": float(rating) - 2.0 * float(rd),
        }
    return result


def _completed_passes(out_dir: Path) -> int:
    snapshots = out_dir / "pool_snapshots.jsonl"
    completed = 0
    if not snapshots.exists():
        return completed
    with snapshots.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                completed = max(completed, int(json.loads(line).get("pass", 0) or 0))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    return completed


def _directory_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(
        item for item in path.rglob("*")
        if (
            item.is_file()
            and "__pycache__" not in item.parts
            and item.name != ".completed"
            and item.suffix != ".pyc"
        )
    ):
        digest.update(str(file_path.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _tag_directory_digest(name: str, commit: str) -> str:
    cache_key = (name, commit)
    cached = _TAG_DIGEST_CACHE.get(cache_key)
    if cached is not None:
        return cached
    prefix = f"bots/{name}/"
    try:
        raw_paths = subprocess.check_output(
            [
                "git",
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                commit,
                "--",
                f"bots/{name}",
            ],
            cwd=ROOT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot inspect tagged bot {name}@{commit}") from exc
    paths = sorted(
        path.decode("utf-8")
        for path in raw_paths.split(b"\0")
        if path
    )
    if not paths or any(not path.startswith(prefix) for path in paths):
        raise RuntimeError(f"tagged bot tree is empty or malformed: {name}@{commit}")
    digest = hashlib.sha256()
    for git_path in paths:
        try:
            content = subprocess.check_output(
                ["git", "show", f"{commit}:{git_path}"],
                cwd=ROOT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"cannot read tagged bot file {git_path}@{commit}"
            ) from exc
        digest.update(git_path.removeprefix(prefix).encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    value = digest.hexdigest()
    _TAG_DIGEST_CACHE[cache_key] = value
    return value


def _copy_opponent_snapshot(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    temporary = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}"
    )
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        temporary,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".completed"),
    )
    for item in sorted(temporary.rglob("*"), reverse=True):
        item.chmod(item.stat().st_mode & ~0o222)
    temporary.chmod(temporary.stat().st_mode & ~0o222)
    try:
        temporary.replace(destination)
    except FileExistsError:
        shutil.rmtree(temporary)


def _freeze_opponent(
    name: str,
    source_path: Path,
    out_dir: Path,
) -> dict[str, str | bool]:
    commit = _completed_tag_commit(name)
    if commit is None:
        raise RuntimeError(f"opponent has no completed immutable tag: {name}")
    source_digest = _directory_digest(source_path)
    tag_digest = _tag_directory_digest(name, commit)
    registry_path = out_dir / "opponent_snapshots" / "registry.json"
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    else:
        registry = {"schema": "opponent_execution_snapshot_v1", "opponents": {}}
    opponents = registry.setdefault("opponents", {})
    previous = opponents.get(name)
    if previous is not None:
        snapshot_path = Path(str(previous["snapshot_path"])).resolve()
        if (
            not snapshot_path.is_dir()
            or _directory_digest(snapshot_path)
            != previous.get("execution_directory_sha256")
        ):
            raise RuntimeError(f"frozen opponent snapshot is corrupt: {name}")
        return dict(previous)
    snapshot_path = (
        out_dir
        / "opponent_snapshots"
        / source_digest
        / name
    ).resolve()
    _copy_opponent_snapshot(source_path, snapshot_path)
    snapshot_digest = _directory_digest(snapshot_path)
    if snapshot_digest != source_digest:
        raise RuntimeError(
            f"opponent snapshot digest mismatch for {name}: "
            f"source={source_digest} snapshot={snapshot_digest}"
        )
    try:
        source_checkout_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=source_path,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"cannot resolve source checkout commit for {name}"
        ) from exc
    frozen: dict[str, str | bool] = {
        "tag_commit": commit,
        "tag_directory_sha256": tag_digest,
        "execution_matches_generation_tag": source_digest == tag_digest,
        "source_path": str(source_path),
        "source_checkout_commit": source_checkout_commit,
        "snapshot_path": str(snapshot_path),
        "execution_directory_sha256": snapshot_digest,
    }
    opponents[name] = frozen
    _write_json_atomic(registry_path, registry)
    return frozen


def _verify_frozen_opponent(entry: dict) -> None:
    path = Path(str(entry["opponent_path"])).resolve()
    expected = str(entry["execution_directory_sha256"])
    if not path.is_dir() or _directory_digest(path) != expected:
        raise RuntimeError(
            f"persisted opponent snapshot changed for {entry.get('name')}"
        )


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


def _deck_seed_for_task(
    *,
    root: int,
    pass_index: int,
    task_index: int,
    hands: int,
    guard: int,
) -> int:
    if pass_index < 0:
        raise ValueError("pass index must be non-negative")
    if task_index < 0 or task_index >= DECK_SEED_SLOTS_PER_PASS:
        raise ValueError("task index exceeds reserved deck-seed slots")
    if hands <= 0 or guard < 0:
        raise ValueError("hands must be positive and guard non-negative")
    block_span = int(hands) + int(guard)
    block_index = pass_index * DECK_SEED_SLOTS_PER_PASS + task_index
    return int(root) + block_index * block_span


def _bot_seed_for_task(*, root: int, pass_index: int, task_index: int) -> int:
    if pass_index < 0:
        raise ValueError("pass index must be non-negative")
    if task_index < 0 or task_index >= DECK_SEED_SLOTS_PER_PASS:
        raise ValueError("task index exceeds reserved bot-seed slots")
    return int(root) + pass_index * DECK_SEED_SLOTS_PER_PASS + task_index


def build_pool(
    ratings_path: Path,
    *,
    strongest: int,
    allow_fallback: bool,
    val_opponents: set[str],
    held_out_opponents: set[str],
) -> list[tuple[str, str, str]]:
    """Build a live pool with stable opponent-level partitions."""
    strong = live_strongest(ratings_path, strongest, allow_fallback=allow_fallback)
    old = ["national_v2", "national_v3", "national_v5", "national_v7",
           "national_v8", "national_v9", "national_v14", "national_v16"]
    explicit_bots = sorted(val_opponents | held_out_opponents)
    all_bots = list(dict.fromkeys(strong + old + explicit_bots))
    explicit = bool(val_opponents or held_out_opponents)
    pool = []
    ratings_checkout = ratings_path.parents[3]
    if not (ratings_checkout / "bots").is_dir():
        ratings_checkout = ROOT
    for name in all_bots:
        if name in held_out_opponents:
            split = "held_out"
        elif name in val_opponents:
            split = "val"
        elif explicit:
            split = "train"
        else:
            split = _stable_split(name)
        path = str((ratings_checkout / "bots" / name).resolve())
        if (
            Path(path).exists()
            and (Path(path) / ".completed").exists()
            and _completed_tag_commit(name) is not None
        ):
            pool.append((name, path, split))
    if not any(split == "val" for _, _, split in pool):
        raise RuntimeError("opponent partition has no validation bot")
    if not any(split == "held_out" for _, _, split in pool):
        raise RuntimeError("opponent partition has no held-out bot")
    return pool


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--passes", type=int, default=40)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--probe-workers", type=int, default=1)
    ap.add_argument("--hands", type=int, default=16)
    ap.add_argument("--timeout-sec", type=int, default=55)
    ap.add_argument("--ratings", default=str(DEFAULT_RATINGS))
    ap.add_argument("--strongest", type=int, default=16)
    ap.add_argument("--allow-fallback-pool", action="store_true")
    ap.add_argument("--val-opponent", action="append", default=[])
    ap.add_argument("--held-out-opponent", action="append", default=[])
    ap.add_argument("--opponents-per-pass", type=int, default=8)
    ap.add_argument("--max-decisions", type=int, default=6)
    ap.add_argument("--max-alternatives", type=int, default=2)
    ap.add_argument("--decision-sampling", choices=("first", "uniform"), default="uniform")
    ap.add_argument("--deck-seed-base", type=int, default=DEFAULT_DECK_SEED_BASE)
    ap.add_argument("--deck-seed-guard", type=int, default=DEFAULT_DECK_SEED_GUARD)
    ap.add_argument("--bot-seed-base", type=int, default=DEFAULT_BOT_SEED_BASE)
    ap.add_argument(
        "--hand-windows",
        default="0.0,0.4,0.7",
        help="Comma-separated fractions used to rotate the minimum sampled hand.",
    )
    args = ap.parse_args(argv)
    args.workers = max(1, min(4, int(args.workers)))
    args.probe_workers = max(1, min(4, int(args.probe_workers)))
    if args.workers * args.probe_workers > 4:
        raise SystemExit("--workers * --probe-workers must not exceed 4 native matches")
    if args.hands <= 0 or args.deck_seed_guard < 0:
        raise SystemExit("--hands must be positive and --deck-seed-guard non-negative")
    ratings_path = Path(args.ratings).expanduser().resolve()
    fractions = [float(value) for value in args.hand_windows.split(",") if value.strip()]
    if not fractions or any(value < 0.0 or value > 1.0 for value in fractions):
        raise SystemExit("--hand-windows values must be within [0, 1]")
    val_opponents = set(args.val_opponent)
    held_out_opponents = set(args.held_out_opponent)
    overlap = val_opponents & held_out_opponents
    if overlap:
        raise SystemExit(f"opponents cannot be both val and held-out: {sorted(overlap)}")
    out_dir = _resolve(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = open(out_dir / ".collector.lock", "w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit(f"collector already running for {out_dir}") from exc
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    plog = open(out_dir / "progress.log", "a", encoding="utf-8")
    def log(msg):
        line = f"[{time.strftime('%Y%m%dT%H%M%S')}] {msg}"
        print(line, flush=True)
        plog.write(line + "\n"); plog.flush()

    total_rows = {"train": 0, "val": 0, "held_out": 0}
    total_behavior = {"train": 0, "val": 0, "held_out": 0}
    for split in total_rows:
        value_path = out_dir / f"cf_{split}.jsonl"
        behavior_path = out_dir / f"opponent_actions_{split}.jsonl"
        total_rows[split] = sum(1 for _ in open(value_path)) if value_path.exists() else 0
        total_behavior[split] = (
            sum(1 for _ in open(behavior_path)) if behavior_path.exists() else 0
        )
    start_pass = _completed_passes(out_dir)
    candidate_path = _resolve(args.candidate)
    candidate_digest = _directory_digest(candidate_path)
    candidate_execution_path = (
        out_dir
        / "candidate_snapshot"
        / candidate_digest
        / candidate_path.name
    ).resolve()
    _copy_opponent_snapshot(candidate_path, candidate_execution_path)
    if _directory_digest(candidate_execution_path) != candidate_digest:
        raise RuntimeError("candidate execution snapshot digest mismatch")
    resume_contract = {
        "schema_version": 4,
        "candidate": str(candidate_path),
        "candidate_sha256": candidate_digest,
        "candidate_execution_path": str(candidate_execution_path),
        "candidate_snapshot_sha256": candidate_digest,
        "ratings_path": str(ratings_path),
        "workers": args.workers,
        "probe_workers": args.probe_workers,
        "hands": args.hands,
        "timeout_sec": args.timeout_sec,
        "strongest": args.strongest,
        "val_opponents": sorted(val_opponents),
        "held_out_opponents": sorted(held_out_opponents),
        "opponents_per_pass": args.opponents_per_pass,
        "max_decisions": args.max_decisions,
        "max_alternatives": args.max_alternatives,
        "decision_sampling": args.decision_sampling,
        "hand_windows": fractions,
        "deck_seed_scheme": "disjoint_match_blocks_v1",
        "deck_seed_base": args.deck_seed_base,
        "deck_seed_guard": args.deck_seed_guard,
        "deck_seed_slots_per_pass": DECK_SEED_SLOTS_PER_PASS,
        "bot_seed_base": args.bot_seed_base,
        "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "probe_sha256": hashlib.sha256(
            (TOOLS / "native_tcp_counterfactual_probe.py").read_bytes()
        ).hexdigest(),
        "cross_hand_sequence_sha256": hashlib.sha256(
            (TOOLS / "cross_hand_sequence.py").read_bytes()
        ).hexdigest(),
    }
    manifest_path = out_dir / "collection_manifest.json"
    if manifest_path.exists():
        try:
            previous_contract = json.loads(
                manifest_path.read_text(encoding="utf-8")
            ).get("resume_contract")
        except (OSError, json.JSONDecodeError):
            previous_contract = None
        if previous_contract != resume_contract:
            raise SystemExit(
                f"resume contract mismatch for {out_dir}; use a new output directory"
            )
    collection_config = {
        "resume_contract": resume_contract,
        "passes_requested": args.passes,
        "start_pass": start_pass,
        "ratings_sha256_at_start": hashlib.sha256(ratings_path.read_bytes()).hexdigest(),
    }
    _write_json_atomic(manifest_path, collection_config)
    t_global = time.time()
    log(f"START: pass={start_pass + 1}/{args.passes} existing_values={total_rows} "
        f"existing_behavior={total_behavior}")
    for ps in range(start_pass, args.passes):
        pass_plan_path = out_dir / "pass_plans" / f"pass_{ps + 1:04d}.json"
        if pass_plan_path.exists():
            pass_plan = json.loads(pass_plan_path.read_text(encoding="utf-8"))
            if (
                int(pass_plan.get("pass", 0) or 0) != ps + 1
                or pass_plan.get("seed_scheme") != "disjoint_match_blocks_v1"
            ):
                raise RuntimeError(f"invalid persisted pass plan: {pass_plan_path}")
            plan_entries = list(pass_plan.get("tasks") or [])
            pool = [
                (
                    str(entry["name"]),
                    str(entry["opponent_path"]),
                    str(entry["split"]),
                )
                for entry in plan_entries
            ]
        else:
            full_pool = build_pool(
                ratings_path,
                strongest=max(1, args.strongest),
                allow_fallback=args.allow_fallback_pool,
                val_opponents=val_opponents,
                held_out_opponents=held_out_opponents,
            )
            limit = max(0, int(args.opponents_per_pass))
            if 0 < limit < len(full_pool):
                train_pool = [row for row in full_pool if row[2] == "train"]
                val_pool = [row for row in full_pool if row[2] == "val"]
                held_pool = [row for row in full_pool if row[2] == "held_out"]
                pool = []
                if val_pool:
                    pool.append(val_pool[ps % len(val_pool)])
                if held_pool and len(pool) < limit:
                    pool.append(held_pool[ps % len(held_pool)])
                remaining = max(0, limit - len(pool))
                if train_pool and remaining:
                    start = (ps * remaining) % len(train_pool)
                    rotated_train = train_pool[start:] + train_pool[:start]
                    pool.extend(rotated_train[:remaining])
            else:
                pool = full_pool
            plan_entries = []
        fraction = fractions[ps % len(fractions)]
        min_hand = max(1, min(args.hands, 1 + int((args.hands - 1) * fraction)))
        if len(pool) > DECK_SEED_SLOTS_PER_PASS:
            raise RuntimeError(
                f"pool has {len(pool)} tasks but only "
                f"{DECK_SEED_SLOTS_PER_PASS} seed slots are reserved"
            )
        tasks = []
        if not plan_entries:
            for i, (name, path, split) in enumerate(pool):
                source_path = Path(path).resolve()
                provenance = _freeze_opponent(name, source_path, out_dir)
                opponent_path = Path(str(provenance["snapshot_path"])).resolve()
                deck_seed_base = _deck_seed_for_task(
                    root=args.deck_seed_base,
                    pass_index=ps,
                    task_index=i,
                    hands=args.hands,
                    guard=args.deck_seed_guard,
                )
                bot_seed_base = _bot_seed_for_task(
                    root=args.bot_seed_base,
                    pass_index=ps,
                    task_index=i,
                )
                plan_entries.append({
                    "name": name,
                    "opponent_path": str(opponent_path),
                    "split": split,
                    "hands": args.hands,
                    "deck_seed_base": deck_seed_base,
                    "deck_seed_last": deck_seed_base + args.hands - 1,
                    "bot_seed_base": bot_seed_base,
                    **{
                        key: value
                        for key, value in provenance.items()
                        if key != "snapshot_path"
                    },
                })
            pass_plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(pass_plan_path, {
                "pass": ps + 1,
                "seed_scheme": "disjoint_match_blocks_v1",
                "tasks": plan_entries,
            })
        for entry in plan_entries:
            name = str(entry["name"])
            _verify_frozen_opponent(entry)
            opponent_path = Path(str(entry["opponent_path"])).resolve()
            tasks.append((
                name,
                str(opponent_path),
                str(entry["split"]),
                int(entry["hands"]),
                int(entry["deck_seed_base"]),
                int(entry["bot_seed_base"]),
            ))
        t0 = time.time()
        pass_rows = 0
        pass_behavior = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(probe_one, str(candidate_execution_path), p, s, n, h, sb, bsb,
                              str(out_dir), args.timeout_sec, min_hand,
                              args.max_decisions, args.max_alternatives,
                              str(ratings_path), args.probe_workers,
                              args.decision_sampling): (n, s)
                    for n, p, s, h, sb, bsb in tasks}
            for fut in as_completed(futs):
                try:
                    rows, behavior_rows, name = fut.result()
                except Exception as exc:
                    failed_name, failed_split = futs[fut]
                    log(f"ERROR: probe={failed_name} split={failed_split}: {exc}")
                    raise
                pass_rows += rows
                pass_behavior += behavior_rows
                total_rows["train" if futs[fut][1] == "train" else futs[fut][1]] += rows if futs[fut][1] in total_rows else 0
                total_behavior["train" if futs[fut][1] == "train" else futs[fut][1]] += behavior_rows if futs[fut][1] in total_behavior else 0
        # recount cumulative from disk for accuracy
        for sp in total_rows:
            cf = out_dir / f"cf_{sp}.jsonl"
            total_rows[sp] = sum(1 for _ in open(cf)) if cf.exists() else 0
            behavior = out_dir / f"opponent_actions_{sp}.jsonl"
            total_behavior[sp] = sum(1 for _ in open(behavior)) if behavior.exists() else 0
        rating_rows = _ratings_snapshot(ratings_path)
        with open(out_dir / "pool_snapshots.jsonl", "a", encoding="utf-8") as snapshot_file:
            snapshot_file.write(json.dumps({
                "pass": ps + 1,
                "ratings_path": str(ratings_path),
                "ratings_sha256": hashlib.sha256(ratings_path.read_bytes()).hexdigest(),
                "min_hand": min_hand,
                "hands": args.hands,
                "workers": args.workers,
                "probe_workers": args.probe_workers,
                "decision_sampling": args.decision_sampling,
                "pool": [{
                    "name": name,
                    "split": split,
                    "tag_commit": next(
                        entry["tag_commit"]
                        for entry in plan_entries
                        if entry["name"] == name
                    ),
                    "execution_directory_sha256": next(
                        entry["execution_directory_sha256"]
                        for entry in plan_entries
                        if entry["name"] == name
                    ),
                    "source_checkout_commit": next(
                        entry["source_checkout_commit"]
                        for entry in plan_entries
                        if entry["name"] == name
                    ),
                    "glicko": rating_rows.get(name),
                    "deck_seed_base": deck_seed_base,
                    "deck_seed_last": deck_seed_base + args.hands - 1,
                    "bot_seed_base": bot_seed_base,
                } for (
                    name,
                    _,
                    split,
                    _,
                    deck_seed_base,
                    bot_seed_base,
                ) in tasks],
            }, separators=(",", ":")) + "\n")
        _write_json_atomic(out_dir / "collector_state.json", {
            "completed_passes": ps + 1,
            "total_rows": total_rows,
            "total_behavior_rows": total_behavior,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        log(f"pass {ps+1}/{args.passes}: pool={len(pool)} min_hand={min_hand} "
            f"rows_this_pass={pass_rows} "
            f"behavior_this_pass={pass_behavior} cumul={total_rows} "
            f"behavior_cumul={total_behavior} dt={time.time()-t0:.0f}s "
            f"elapsed={time.time()-t_global:.0f}s")
    log(f"DONE: values={total_rows} value_total={sum(total_rows.values())} "
        f"behavior={total_behavior} behavior_total={sum(total_behavior.values())} "
        f"elapsed={time.time()-t_global:.0f}s")
    plog.close()
    lock_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
