#!/usr/bin/env python3
"""One-time reset to a fresh strict TCP policy epoch (version-1 floor).

On the tencent-cloud-runtime branch the version floor is
ARCHIVED_VERSION_HIGH_WATER=0 / FIRST_STRICT_POLICY_VERSION=1, so this reset
initializes an empty strict epoch whose first target is national_cloud_v1.
No bot source is copied into the new epoch.  The immutable tag/high-water
namespace supplies version authority (archived high-water -> first strict),
while old runtime outputs are moved to an explicitly untrusted archive and
fresh result directories are created empty.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from bot_namespace import (  # noqa: E402
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    EVOLUTION_BRANCH,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    parse_bot_version,
    resolve_version_namespace_authority,
)
from bot_artifact import canonical_digest  # noqa: E402
from log_epoch import (  # noqa: E402
    LOG_EPOCH_MARKER_FILENAME,
    build_log_epoch_marker,
)


RUNTIME_DIRS = (
    ("web_core_results", CORE / "results"),
    ("root_results", ROOT / "results"),
    ("ladder_results", ROOT / "ladder_results"),
    ("web_logs", ROOT / "web" / "logs"),
)

RESET_RECEIPT_FILENAME = "policy_epoch_reset_receipt.json"
RESET_CLAIM_FILENAME = "reset_claim.json"
ARCHIVE_RESET_RECEIPT_FILENAME = "reset_receipt.json"
RUNTIME_CHECKOUT_NAME = ".evolution_pok"
RESET_ARCHIVE_RELATIVE = Path(
    "archive/evolution_epochs/national_native_v1/runtime_legacy_untrusted"
)


def _reset_archive_base() -> Path:
    return ROOT / RESET_ARCHIVE_RELATIVE


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.strip()


def _version_authority_high_water() -> int:
    try:
        return int(resolve_version_namespace_authority(_git).high_water)
    except RuntimeError:
        # An empty namespace (no paired completion/high-water tags yet) is the
        # legitimate bootstrap start for an isolated deployment namespace such
        # as national_cloud_v on tencent-cloud-runtime: it has no history, so it
        # is treated as sitting at the archived high-water floor, ready for a
        # fresh first-strict reset.
        return ARCHIVED_VERSION_HIGH_WATER


def _canonical_json_bytes(payload: dict) -> bytes:
    return (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        count = os.write(descriptor, view[offset:])
        if count <= 0:
            raise OSError("policy epoch reset write made no progress")
        offset += int(count)


def _write_json_exclusive(path: Path, payload: dict) -> None:
    """Create a durable no-clobber reset marker."""

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, _canonical_json_bytes(payload))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _replace_json(path: Path, payload: dict) -> None:
    """Atomically replace this reset's own claim with its final receipt."""

    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        _write_json_exclusive(temporary, payload)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def _reset_lock():
    """Serialize the one legal reset attempt within this checkout."""

    archive_base = _reset_archive_base()
    archive_base.mkdir(parents=True, exist_ok=True)
    lock_path = archive_base / ".policy_epoch_reset.lock"
    if lock_path.is_symlink():
        raise RuntimeError("policy epoch reset lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _prior_reset_evidence() -> list[str]:
    """Return any completed or interrupted reset marker.

    An interrupted attempt is intentionally not auto-resumable.  Its claim
    proves that filesystem moves may already have happened, so an operator must
    inspect/recover that attempt rather than minting a replacement receipt.
    """

    evidence: list[Path] = []
    live = CORE / "results" / RESET_RECEIPT_FILENAME
    if os.path.lexists(live):
        evidence.append(live)
    archive_base = _reset_archive_base()
    if archive_base.is_dir() and not archive_base.is_symlink():
        for child in sorted(archive_base.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            for name in (RESET_CLAIM_FILENAME, ARCHIVE_RESET_RECEIPT_FILENAME):
                candidate = child / name
                if os.path.lexists(candidate):
                    evidence.append(candidate)
    return [str(path) for path in evidence]


def _runtime_checkout_identity_errors() -> list[str]:
    """Require the autonomous clone, clean tracked bytes, and synced main."""

    errors: list[str] = []
    resolved_root = ROOT.resolve()
    if resolved_root.name != RUNTIME_CHECKOUT_NAME:
        errors.append(
            "policy_epoch_reset_requires_autonomous_runtime_checkout:"
            f"expected_name={RUNTIME_CHECKOUT_NAME}:actual={resolved_root}"
        )
    try:
        top = Path(_git("rev-parse", "--show-toplevel")).resolve()
    except Exception as exc:
        errors.append(
            f"policy_epoch_reset_git_root_unavailable:{type(exc).__name__}"
        )
    else:
        if top != resolved_root:
            errors.append("policy_epoch_reset_git_root_mismatch")
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != EVOLUTION_BRANCH:
            errors.append(f"policy_epoch_reset_requires_publication_branch:{branch}")
        if _git("status", "--porcelain", "--untracked-files=no"):
            errors.append("policy_epoch_reset_tracked_worktree_not_clean")
        head = _git("rev-parse", "HEAD")
        remote_ref = f"refs/remotes/origin/{EVOLUTION_BRANCH}"
        try:
            remote_main = _git("rev-parse", remote_ref)
        except Exception:
            remote_main = ""
        if not head or head != remote_main:
            errors.append(
                f"policy_epoch_reset_runtime_not_synced_to_origin:{EVOLUTION_BRANCH}"
            )
    except Exception as exc:
        errors.append(
            f"policy_epoch_reset_git_sync_unavailable:{type(exc).__name__}"
        )
    return list(dict.fromkeys(errors))


def build_plan(stamp: str) -> dict:
    archive_root = (
        _reset_archive_base() / stamp
    )
    archived_bot_dirs = []
    bots_dir = ROOT / "bots"
    for path in sorted(bots_dir.iterdir()) if bots_dir.is_dir() else []:
        version = parse_bot_version(path.name)
        if version is None:
            continue
        if path.is_symlink():
            raise RuntimeError(f"refusing policy reset with symlink bot path: {path}")
        if not path.is_dir():
            continue
        # The immutable Git tag/high-water namespace is the sole version
        # authority. On the cloud branch the archived high-water is 0, so a
        # directory named v1+ cannot be a published strict bot unless its
        # paired completion/high-water tags exist; an untracked directory such
        # as a stale main-namespace national_v143/v156 inherited from main is
        # an unfinished old-epoch candidate. Archive it together with its
        # checkpoint instead of letting an untracked directory block or advance
        # the fresh national_cloud_v1 bootstrap.
        archived_bot_dirs.append(
            {
                "source": path,
                "destination": archive_root / "bot_debris" / path.name,
                "disposition": (
                    "retired_epoch_bot"
                    if version <= ARCHIVED_VERSION_HIGH_WATER
                    else "stale_unpublished_high_version_candidate"
                ),
            }
        )
    return {
        "archive_root": archive_root,
        "runtime": [
            {
                "label": label,
                "source": source,
                "destination": archive_root / label,
            }
            for label, source in RUNTIME_DIRS
            if source.exists()
            and any(child.name != ".gitkeep" for child in source.iterdir())
        ],
        "archived_bot_dirs": archived_bot_dirs,
    }


def run(*, execute: bool, acknowledge_runtime_checkout: bool = False) -> dict:
    if EVALUATION_EPOCH != "national_tcp_policy_v1":
        raise RuntimeError(
            f"refusing reset for unexpected active epoch {EVALUATION_EPOCH!r}"
        )
    high_water = _version_authority_high_water()
    if high_water < ARCHIVED_VERSION_HIGH_WATER:
        raise RuntimeError(
            f"immutable version authority does not cover archived high-water "
            f"{ARCHIVED_VERSION_HIGH_WATER}"
        )
    if high_water > ARCHIVED_VERSION_HIGH_WATER:
        raise RuntimeError(
            "strict policy versions already exist; this one-time reset cannot rerun"
        )
    if execute:
        if not acknowledge_runtime_checkout:
            raise RuntimeError(
                "executing the epoch reset requires --acknowledge-runtime-checkout"
            )
        checkout_errors = _runtime_checkout_identity_errors()
        if checkout_errors:
            raise RuntimeError("; ".join(checkout_errors))

    # Omitted --execute is a genuinely read-only plan.  Only the mutating path
    # creates/locks the durable one-time authority directory.
    with (_reset_lock() if execute else nullcontext()):
        prior_evidence = _prior_reset_evidence()
        if execute and prior_evidence:
            raise RuntimeError(
                "policy epoch reset already completed or was interrupted; "
                "refusing to mint a second receipt: " + ", ".join(prior_evidence)
            )

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        plan = build_plan(stamp)
        archive_relative = str(plan["archive_root"].relative_to(ROOT))
        claim_payload = {
            "schema_version": 1,
            "kind": "national_tcp_policy_epoch_reset_claim",
            "epoch": EVALUATION_EPOCH,
            "created_at": datetime.now().isoformat(timespec="microseconds"),
            "git_head": _git("rev-parse", "HEAD"),
            "archive_root": archive_relative,
            "first_target_version": FIRST_STRICT_POLICY_VERSION,
            "checkout_role": "autonomous_evolution_runtime",
            "one_time": True,
        }
        claim = {**claim_payload, "claim_digest": canonical_digest(claim_payload)}
        receipt = {
            "schema_version": 2,
            "kind": "national_tcp_policy_epoch_reset",
            "epoch": EVALUATION_EPOCH,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "execute" if execute else "dry_run",
            "git_head": claim["git_head"],
            "archive_root": archive_relative,
            "execution_scope": {
                "checkout_role": "autonomous_evolution_runtime",
                "one_time": True,
                "prior_reset_evidence_required_empty": True,
                "claim_digest": claim["claim_digest"],
            },
            "archived_version_high_water": ARCHIVED_VERSION_HIGH_WATER,
            "version_authority_high_water": high_water,
            "first_target_version": FIRST_STRICT_POLICY_VERSION,
            "source_code_inherited": False,
            "seed_bot": None,
            "active_namespace": {
                "bot": bot_name(FIRST_STRICT_POLICY_VERSION),
                "protocol": "official-national-raw-tcp-v1",
                "policy_abi": "national-tcp-policy-runtime-v1",
            },
            "archived_runtime": [],
            "archived_bot_debris": [],
        }
        for item in plan["runtime"]:
            receipt["archived_runtime"].append(
                {
                    "label": item["label"],
                    "from": str(item["source"].relative_to(ROOT)),
                    "to": str(item["destination"].relative_to(ROOT)),
                    "trust": "legacy_untrusted_not_for_prompt_or_rating",
                }
            )
        for item in plan["archived_bot_dirs"]:
            receipt["archived_bot_debris"].append(
                {
                    "from": str(item["source"].relative_to(ROOT)),
                    "to": str(item["destination"].relative_to(ROOT)),
                    "trust": "archived_non_executable",
                    "disposition": item["disposition"],
                }
            )
        receipt["receipt_digest"] = canonical_digest(receipt)

        if execute:
            archive_root = plan["archive_root"]
            archive_root.mkdir(parents=True, exist_ok=False)
            _write_json_exclusive(archive_root / RESET_CLAIM_FILENAME, claim)
            reset_receipt = CORE / "results" / RESET_RECEIPT_FILENAME
            _write_json_exclusive(reset_receipt, claim)
            try:
                for item in plan["runtime"]:
                    item["destination"].parent.mkdir(parents=True, exist_ok=True)
                    item["destination"].mkdir(parents=True, exist_ok=False)
                    for child in item["source"].iterdir():
                        if child.name in {".gitkeep", RESET_RECEIPT_FILENAME}:
                            continue
                        shutil.move(
                            str(child), str(item["destination"] / child.name)
                        )
                for item in plan["archived_bot_dirs"]:
                    item["destination"].parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(item["source"]), str(item["destination"]))
                logs_dir = ROOT / "web" / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                _write_json_exclusive(
                    logs_dir / LOG_EPOCH_MARKER_FILENAME,
                    build_log_epoch_marker(receipt),
                )
                _write_json_exclusive(
                    archive_root / ARCHIVE_RESET_RECEIPT_FILENAME,
                    receipt,
                )
                _replace_json(reset_receipt, receipt)
            except BaseException as exc:
                raise RuntimeError(
                    "policy epoch reset interrupted after the durable one-time "
                    "claim; inspect the claim/archive and recover manually"
                ) from exc
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "One-time national_tcp_policy_v1 runtime reset: archive old "
            "outputs and stale bot directories as legacy-untrusted, preserve "
            "the archived version-authority high-water (0 on this branch), and "
            "initialize an empty strict epoch whose first target is "
            "national_cloud_v1. No bot, rating, H2H, experience, or other "
            "evidence is migrated."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the one-time archive/reset. Omit for a read-only plan.",
    )
    parser.add_argument(
        "--acknowledge-runtime-checkout",
        action="store_true",
        help=(
            "Required with --execute; confirms this command runs from the "
            "stopped .evolution_pok autonomous runtime checkout."
        ),
    )
    args = parser.parse_args()
    print(json.dumps(
        run(
            execute=args.execute,
            acknowledge_runtime_checkout=args.acknowledge_runtime_checkout,
        ),
        indent=2,
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
