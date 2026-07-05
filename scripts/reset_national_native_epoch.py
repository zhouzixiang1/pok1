#!/usr/bin/env python3
"""Archive legacy evolution state and start the national-native epoch.

The script is intentionally local-state oriented: archive payloads are ignored
by git, while the resulting active bot namespace under ``bots/national_v<N>/``
can be staged and committed normally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOTS_DIR = ROOT / "bots"
EPOCH = "national_native_v1"
RUNTIME_DIRS = (
    ("web_core_results", ROOT / "web" / "core" / "results"),
    ("root_results", ROOT / "results"),
    ("ladder_results", ROOT / "ladder_results"),
)
LEGACY_BOT_DIR_NAMES = {"mixture_main", "neural_bot"}


def _run_git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_digest(path: Path) -> str:
    h = hashlib.sha256()
    if not path.exists():
        return ""
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = item.relative_to(path).as_posix()
        h.update(rel.encode("utf-8", errors="replace"))
        h.update(b"\0")
        try:
            h.update(_file_digest(item).encode("ascii"))
        except OSError:
            h.update(b"unreadable")
        h.update(b"\0")
    return h.hexdigest()


def _copy_active_seed(src: Path, dst: Path, execute: bool) -> dict:
    if dst.exists():
        raise RuntimeError(f"target already exists: {dst}")
    if execute:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".completed"))
        (dst / ".completed").write_text(f"seeded from {src.name} for {EPOCH}\n", encoding="utf-8")
    return {
        "from": src.name,
        "to": dst.name,
        "has_national_bot": (src / "national_bot.py").exists(),
        "digest": _dir_digest(src),
    }


def _move_path(src: Path, dst: Path, execute: bool) -> bool:
    if not src.exists():
        return False
    if dst.exists():
        raise RuntimeError(f"archive target already exists: {dst}")
    if execute:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    return True


def _legacy_bot_dirs() -> list[Path]:
    result: list[Path] = []
    for path in BOTS_DIR.iterdir() if BOTS_DIR.exists() else []:
        if not path.is_dir():
            continue
        name = path.name
        if name.startswith("claude_v"):
            result.append(path)
        elif name.startswith("bot") and name[3:].isdigit():
            result.append(path)
        elif name in LEGACY_BOT_DIR_NAMES:
            result.append(path)
    return sorted(result, key=lambda p: p.name)


def build_plan(archive_root: Path) -> dict:
    national_sources = [
        path for path in sorted(BOTS_DIR.glob("claude_v*"), key=lambda p: int(p.name.split("_v", 1)[1]))
        if path.is_dir() and (path / "national_bot.py").exists()
    ]
    seed_moves = []
    for idx, src in enumerate(national_sources, start=1):
        seed_moves.append({"src": src, "dst": BOTS_DIR / f"national_v{idx}"})

    legacy_moves = [
        {"src": path, "dst": archive_root / "legacy_bots" / path.name}
        for path in _legacy_bot_dirs()
    ]
    runtime_moves = [
        {"src": src, "dst": archive_root / "payload" / label}
        for label, src in RUNTIME_DIRS
        if src.exists()
    ]
    return {
        "seed_moves": seed_moves,
        "legacy_moves": legacy_moves,
        "runtime_moves": runtime_moves,
    }


def run(*, execute: bool) -> dict:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_root = ROOT / "archive" / "evolution_epochs" / f"{EPOCH}_{stamp}"
    plan = build_plan(archive_root)

    manifest = {
        "epoch": EPOCH,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "git_head": _run_git("rev-parse", "--short=12", "HEAD"),
        "mode": "execute" if execute else "dry-run",
        "active_namespace": {
            "bot_prefix": "national_v",
            "tag_prefix": "national-bot-v",
            "protocol": "national_tcp_native",
            "adapter_allowed_as_pass_condition": False,
        },
        "seed_bots": [],
        "archived_legacy_bots": [],
        "archived_runtime_dirs": [],
    }

    for item in plan["seed_moves"]:
        manifest["seed_bots"].append(_copy_active_seed(item["src"], item["dst"], execute))

    for item in plan["legacy_moves"]:
        moved = _move_path(item["src"], item["dst"], execute)
        if moved:
            manifest["archived_legacy_bots"].append({
                "from": str(item["src"].relative_to(ROOT)),
                "to": str(item["dst"].relative_to(ROOT)),
            })

    for item in plan["runtime_moves"]:
        moved = _move_path(item["src"], item["dst"], execute)
        if moved:
            manifest["archived_runtime_dirs"].append({
                "from": str(item["src"].relative_to(ROOT)),
                "to": str(item["dst"].relative_to(ROOT)),
            })
            if execute:
                item["src"].mkdir(parents=True, exist_ok=True)
                (item["src"] / ".gitkeep").write_text("", encoding="utf-8")

    if execute:
        archive_root.mkdir(parents=True, exist_ok=True)
        (archive_root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (archive_root / "README.md").write_text(
            f"# {EPOCH} archive\n\n"
            "This local directory is gitignored. It contains legacy bot source and "
            "runtime data retired before the national-native reset.\n",
            encoding="utf-8",
        )

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Apply the migration. Omit for dry-run.")
    args = parser.parse_args()
    manifest = run(execute=args.execute)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
