"""Generation-scoped evidence snapshots for LLM planning/audit.

The rating daemon keeps updating live result files while Master and audit LLMs
run. A plan that cites live H2H counts can become "stale" minutes later even if
the cited numbers were correct when the Master read them. This module creates a
stable per-generation H2H snapshot so Master and MasterPlanAudit validate
against the same evidence contract.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator


SNAPSHOT_DIRNAME = "evidence_snapshot"
H2H_SNAPSHOT_FILENAME = "head_to_head.json"
MANIFEST_FILENAME = "manifest.json"
SNAPSHOT_SCHEMA_VERSION = 2


def _infra():
    import evolution_infra

    return evolution_infra


def _snapshot_dir(next_v: int | str) -> Path:
    infra = _infra()
    return infra.RESULTS_DIR / f"v{int(next_v)}" / SNAPSHOT_DIRNAME


def _repo_rel(path: Path) -> str:
    infra = _infra()
    try:
        return str(path.resolve().relative_to(infra.PROJECT_ROOT.resolve())).replace("\\", "/")
    except Exception:
        return str(path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _evaluation_identity_digest(results_dir: Path) -> str:
    path = results_dir / "evaluation_data_manifest.json"
    payload = _read_manifest(path) or {}
    return str(payload.get("manifest_digest") or "missing")


@contextmanager
def _snapshot_lock(next_v: int | str) -> Iterator[None]:
    parent = _snapshot_dir(next_v).parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(parent / ".evidence_snapshot.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _snapshot_paths(next_v: int | str) -> tuple[Path, Path, Path]:
    directory = _snapshot_dir(next_v)
    return directory, directory / H2H_SNAPSHOT_FILENAME, directory / MANIFEST_FILENAME


def _validate_existing_snapshot(next_v: int | str) -> tuple[dict[str, Any] | None, list[str]]:
    directory, snapshot_path, manifest_path = _snapshot_paths(next_v)
    issues: list[str] = []
    if not directory.is_dir() or directory.is_symlink():
        return None, ["snapshot_directory_missing_or_unsafe"]
    if not snapshot_path.is_file() or snapshot_path.is_symlink():
        issues.append("snapshot_payload_missing_or_unsafe")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        issues.append("snapshot_manifest_missing_or_unsafe")
    manifest = _read_manifest(manifest_path)
    if manifest is None:
        issues.append("snapshot_manifest_invalid_json")
        return None, issues
    if manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        issues.append("snapshot_schema_mismatch")
    if manifest.get("next_v") != int(next_v):
        issues.append("snapshot_version_mismatch")
    claimed_digest = str(manifest.get("manifest_digest") or "")
    actual_digest = _canonical_digest({
        key: value for key, value in manifest.items() if key != "manifest_digest"
    })
    if claimed_digest != actual_digest:
        issues.append("snapshot_manifest_digest_mismatch")
    if issues or not snapshot_path.is_file():
        return None, issues
    try:
        payload = snapshot_path.read_bytes()
        parsed = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        return None, [*issues, f"snapshot_payload_invalid:{type(exc).__name__}"]
    if not isinstance(parsed, dict):
        issues.append("snapshot_payload_not_object")
    if manifest.get("sha256") != _sha256(payload):
        issues.append("snapshot_payload_digest_mismatch")
    if manifest.get("bytes") != len(payload):
        issues.append("snapshot_payload_size_mismatch")
    if manifest.get("entries") != (len(parsed) if isinstance(parsed, dict) else 0):
        issues.append("snapshot_entry_count_mismatch")
    current_identity = _evaluation_identity_digest(_infra().RESULTS_DIR)
    if manifest.get("evaluation_identity_digest") != current_identity:
        issues.append("snapshot_evaluation_identity_mismatch")
    return (manifest if not issues else None), issues


def _write_file_durable(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def ensure_generation_h2h_snapshot(next_v: int | str, *, force: bool = False) -> dict[str, Any]:
    """Create or return the stable H2H snapshot for ``next_v``."""
    infra = _infra()
    snapshot_dir, snapshot_path, manifest_path = _snapshot_paths(next_v)
    live_path = infra.H2H_FILE

    with _snapshot_lock(next_v):
        if snapshot_dir.exists() and not force:
            manifest, issues = _validate_existing_snapshot(next_v)
            if manifest is None:
                return {
                    "available": False,
                    "reason": "snapshot_integrity_failure",
                    "issues": issues,
                    "h2h_path": str(snapshot_path),
                    "h2h_relpath": _repo_rel(snapshot_path),
                    "manifest_path": str(manifest_path),
                    "manifest_relpath": _repo_rel(manifest_path),
                    "reused": True,
                }
            return {
                **manifest,
                "available": True,
                "h2h_path": str(snapshot_path),
                "h2h_relpath": _repo_rel(snapshot_path),
                "manifest_path": str(manifest_path),
                "manifest_relpath": _repo_rel(manifest_path),
                "reused": True,
            }
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)

        try:
            if live_path.exists():
                with infra.locked_file(live_path, "rb") as handle:
                    payload = handle.read()
                    live_stat = os.fstat(handle.fileno())
            else:
                payload = b"{}"
                live_stat = None
            parsed = json.loads(payload.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("live H2H payload is not an object")
        except Exception as exc:
            return {
                "available": False,
                "reason": f"read_live_h2h_failed:{type(exc).__name__}",
                "error": str(exc),
                "h2h_path": str(live_path),
                "h2h_relpath": _repo_rel(live_path),
                "live_h2h_relpath": _repo_rel(live_path),
                "reused": False,
            }

        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(tempfile.mkdtemp(
            prefix=f".{SNAPSHOT_DIRNAME}-",
            dir=snapshot_dir.parent,
        ))
        try:
            temporary_snapshot = temporary_dir / H2H_SNAPSHOT_FILENAME
            temporary_manifest = temporary_dir / MANIFEST_FILENAME
            _write_file_durable(temporary_snapshot, payload)
            manifest = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "available": True,
                "next_v": int(next_v),
                "created_at": time.time(),
                "h2h_relpath": _repo_rel(snapshot_path),
                "manifest_relpath": _repo_rel(manifest_path),
                "live_h2h_relpath": _repo_rel(live_path),
                "evaluation_identity_digest": _evaluation_identity_digest(infra.RESULTS_DIR),
                "live_cutoff": {
                    "mtime_ns": int(live_stat.st_mtime_ns) if live_stat else None,
                    "size": int(live_stat.st_size) if live_stat else 0,
                },
                "sha256": _sha256(payload),
                "bytes": len(payload),
                "entries": len(parsed),
            }
            manifest["manifest_digest"] = _canonical_digest(manifest)
            _write_file_durable(
                temporary_manifest,
                json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
            )
            directory_fd = os.open(temporary_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(temporary_dir, snapshot_dir)
            parent_fd = os.open(snapshot_dir.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir, ignore_errors=True)

    try:
        from system_log import log_system_event

        log_system_event(
            "pipeline.h2h_snapshot_created",
            "info",
            f"H2H evidence snapshot created for v{int(next_v)}",
            {k: manifest[k] for k in ("next_v", "h2h_relpath", "sha256", "entries", "bytes", "manifest_digest")},
        )
    except Exception:
        pass
    return {
        **manifest,
        "available": True,
        "reused": False,
        "h2h_path": str(snapshot_path),
        "manifest_path": str(manifest_path),
    }


def load_generation_h2h_snapshot(next_v: int | str) -> dict[str, Any]:
    snapshot = ensure_generation_h2h_snapshot(next_v)
    if not snapshot.get("available"):
        return {}
    try:
        return json.loads(Path(snapshot["h2h_path"]).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _row_versions(key: str) -> tuple[str | None, str | None]:
    match = _H2H_KEY_RE.search(str(key or ""))
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _row_win_rate(row: dict[str, Any]) -> float | None:
    try:
        if row.get("win_rate") is not None:
            return float(row.get("win_rate"))
        games = int(row.get("games", 0) or 0)
        if games <= 0:
            return None
        return float(row.get("a_wins", 0) or 0) / games
    except Exception:
        return None


def build_h2h_prompt_summary(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    max_rows: int = 64,
    confirmed_games: int = 10,
) -> str:
    """Return a compact, citation-safe H2H summary for prompts.

    The full snapshot remains the source of truth. This summary gives Master and
    audit roles the exact row keys/counts they need most often without forcing a
    long live-file read or encouraging sparse-sample overclaims.
    """
    h2h = load_generation_h2h_snapshot(next_v)
    if not h2h:
        return "Compact H2H summary unavailable; stable snapshot has no rows."

    source = str(source_v) if source_v is not None else None
    rows: list[dict[str, Any]] = []
    for key, row in h2h.items():
        if not isinstance(row, dict):
            continue
        a_v, b_v = _row_versions(str(key))
        games = int(row.get("games", 0) or 0)
        a_wins = int(row.get("a_wins", 0) or 0)
        b_wins = int(row.get("b_wins", 0) or 0)
        wr_a = _row_win_rate(row)
        if wr_a is None:
            continue
        perspective = None
        source_wr = None
        source_wins = None
        source_losses = None
        if source and a_v == source:
            perspective = f"v{source}"
            source_wr = wr_a
            source_wins = a_wins
            source_losses = b_wins
        elif source and b_v == source:
            perspective = f"v{source}"
            source_wr = 1.0 - wr_a
            source_wins = b_wins
            source_losses = a_wins

        sample_class = "sparse"
        if games >= confirmed_games:
            if source_wr is not None and source_wr < 0.40:
                sample_class = "confirmed_weakness"
            elif source_wr is not None and source_wr > 0.60:
                sample_class = "confirmed_strength"
            else:
                sample_class = "adequate_context"

        rows.append({
            "key": str(key),
            "games": games,
            "a_wins": a_wins,
            "b_wins": b_wins,
            "win_rate": wr_a,
            "source_match": perspective is not None,
            "source_wr": source_wr,
            "source_wins": source_wins,
            "source_losses": source_losses,
            "sample_class": sample_class,
            "canonical_citation": (
                f"{key}: games={games}, a_wins={a_wins}, "
                f"b_wins={b_wins}, win_rate={wr_a:.4f}"
            ),
        })

    if source:
        rows.sort(key=lambda r: (
            not r["source_match"],
            {
                "confirmed_weakness": 0,
                "adequate_context": 1,
                "confirmed_strength": 2,
                "sparse": 3,
            }.get(r["sample_class"], 4),
            r["source_wr"] if r["source_wr"] is not None else 0.5,
            -r["games"],
        ))
    else:
        rows.sort(key=lambda r: -r["games"])
    if source:
        source_rows = [r for r in rows if r["source_match"]]
        other_rows = [r for r in rows if not r["source_match"]]
        rows = source_rows + other_rows[:max(0, max_rows - len(source_rows))]
    rows = rows[:max_rows]

    lines = [
        "Compact source-focused H2H summary from the stable snapshot:",
        f"- Adequate/confirmed matchup claims require games >= {confirmed_games}; otherwise label sparse/advisory.",
        "- Quote row key, games, a_wins, b_wins, and win_rate exactly when citing a matchup.",
        "- Prefer the canonical_citation text below; do not derive matchup records from live H2H or match_history.",
    ]
    for r in rows:
        base = (
            f"- {r['key']}: games={r['games']}, a_wins={r['a_wins']}, "
            f"b_wins={r['b_wins']}, win_rate={r['win_rate']:.4f}, "
            f"class={r['sample_class']}"
        )
        if r["source_match"]:
            base += (
                f", source_wr={r['source_wr']:.4f}, "
                f"source_record={r['source_wins']}W/{r['source_losses']}L"
            )
        base += f", canonical_citation=\"{r['canonical_citation']}\""
        lines.append(base)
    return "\n".join(lines)


def h2h_citation_repair_guidance(
    next_v: int | str,
    citation_errors: list[str],
    *,
    source_v: int | str | None = None,
    max_rows: int = 12,
) -> str:
    """Return concrete snapshot rows to repair rejected H2H citations.

    Audit rejection feedback is often too negative ("the numbers are wrong")
    without giving the Master a replacement fact. This helper maps citation
    errors back to exact snapshot rows so the retry prompt contains the row key
    and counts to use verbatim.
    """
    h2h = load_generation_h2h_snapshot(next_v)
    if not h2h or not citation_errors:
        return ""

    wanted: list[str] = []
    seen: set[str] = set()
    for err in citation_errors:
        for key in re.findall(r"\(key ([^)]+)\)", str(err)):
            if key in h2h and key not in seen:
                wanted.append(key)
                seen.add(key)
        for alias_match in _H2H_KEY_RE.finditer(str(err)):
            a_v, b_v = alias_match.group(1), alias_match.group(2)
            for key in (f"national_v{a_v} vs national_v{b_v}", f"national_v{b_v} vs national_v{a_v}"):
                if key in h2h and key not in seen:
                    wanted.append(key)
                    seen.add(key)

    rows: list[str] = []
    for key in wanted[:max_rows]:
        row = h2h.get(key)
        if not isinstance(row, dict):
            continue
        games = int(row.get("games", 0) or 0)
        a_wins = int(row.get("a_wins", 0) or 0)
        b_wins = int(row.get("b_wins", 0) or 0)
        win_rate = _row_win_rate(row)
        if win_rate is None:
            win_rate = 0.0
        line = (
            f"- canonical_citation: {key}: games={games}, "
            f"a_wins={a_wins}, b_wins={b_wins}, win_rate={win_rate:.4f}"
        )
        a_v, b_v = _row_versions(key)
        if source_v is not None and str(source_v) in {a_v, b_v}:
            source = str(source_v)
            if a_v == source:
                source_wins, source_losses, source_wr = a_wins, b_wins, win_rate
            else:
                source_wins, source_losses, source_wr = b_wins, a_wins, 1.0 - win_rate
            line += f" (v{source} perspective: {source_wins}W/{source_losses}L, wr={source_wr:.4f})"
        rows.append(line)

    if not rows:
        return ""
    return "\n".join([
        "Use these exact stable snapshot rows to repair the rejected H2H citations:",
        *rows,
        "Do not replace them with live H2H, match_history, replay-window, or daemon-updated counts.",
    ])


def h2h_snapshot_contract_text(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    include_json: bool = False,
    max_chars: int = 60_000,
) -> str:
    """Return prompt text that binds Master/Audit to the stable H2H snapshot."""
    snapshot = ensure_generation_h2h_snapshot(next_v)
    if not snapshot.get("available"):
        return (
            "Stable H2H snapshot unavailable or failed integrity checks. Do not "
            "read live H2H or make matchup-count claims for this generation. "
            f"Reason: {snapshot.get('reason', 'unknown')}"
        )
    lines = [
        "Stable H2H evidence snapshot for this generation:",
        f"- Snapshot file: `{snapshot['h2h_relpath']}`",
        f"- Snapshot manifest: `{snapshot['manifest_relpath']}`",
        f"- sha256: `{snapshot.get('sha256', '')}`; entries: {snapshot.get('entries', 0)}; bytes: {snapshot.get('bytes', 0)}",
        "- For verbatim H2H counts in Master plans and MasterPlanAudit, use this snapshot only.",
        "- Live H2H may drift after snapshot creation; planning and audit must ignore that drift.",
    ]
    try:
        lines.extend(["", build_h2h_prompt_summary(next_v, source_v=source_v)])
    except Exception:
        pass
    if include_json:
        try:
            text = Path(snapshot["h2h_path"]).read_text(encoding="utf-8")
        except Exception:
            text = "{}"
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [snapshot truncated for prompt budget]"
        lines.extend(["", "Snapshot JSON:", "```json", text, "```"])
    return "\n".join(lines)


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_flatten_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_flatten_text(item) for item in value)
    return str(value or "")


def _extract_int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


_H2H_KEY_RE = re.compile(r"\bnational_v(\d+)\s+vs\s+national_v(\d+)\b", re.IGNORECASE)
_WL_RE = re.compile(r"(?<![\w.])(\d+)\s*W\s*(?:[/:\-]|,)?\s*(\d+)\s*L\b", re.IGNORECASE)


def _h2h_key_aliases(key: str) -> list[tuple[str, str, str]]:
    """Return textual aliases and perspective for a snapshot H2H key."""
    match = _H2H_KEY_RE.search(str(key or ""))
    if not match:
        return [(str(key or ""), "", "")]
    a_v, b_v = match.group(1), match.group(2)
    aliases = [
        (f"national_v{a_v} vs national_v{b_v}", a_v, b_v),
        (f"v{a_v} vs v{b_v}", a_v, b_v),
        (f"national_v{b_v} vs national_v{a_v}", b_v, a_v),
        (f"v{b_v} vs v{a_v}", b_v, a_v),
    ]
    seen: set[str] = set()
    deduped: list[tuple[str, str, str]] = []
    for alias, first, second in aliases:
        low = alias.lower()
        if low in seen:
            continue
        seen.add(low)
        deduped.append((alias, first, second))
    return deduped


def validate_h2h_citations_against_snapshot(master_plan: Any, next_v: int | str) -> list[str]:
    """Detect labeled H2H count citations that disagree with the generation snapshot."""
    h2h = load_generation_h2h_snapshot(next_v)
    if not h2h:
        return []
    text = _flatten_text(master_plan)
    errors: list[str] = []
    for key, row in h2h.items():
        if not isinstance(row, dict):
            continue
        seen_spans: set[tuple[int, int]] = set()
        for alias, first_v, second_v in _h2h_key_aliases(str(key)):
            if not alias:
                continue
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])", re.IGNORECASE)
            for match in pattern.finditer(text):
                span = match.span()
                if span in seen_spans:
                    continue
                seen_spans.add(span)
                window = text[span[0]:span[0] + 360]
                cited = {
                    "games": _extract_int(r"\bgames?\s*[:=]\s*(\d+)", window),
                    "a_wins": _extract_int(r"\ba_wins\s*[:=]\s*(\d+)", window),
                    "b_wins": _extract_int(r"\bb_wins\s*[:=]\s*(\d+)", window),
                }
                if cited["games"] is None:
                    cited["games"] = _extract_int(r"(?<![\w.])(\d+)\s*(?:g|games|局)\b", window)

                wl_match = _WL_RE.search(window)
                if wl_match:
                    wins = int(wl_match.group(1))
                    losses = int(wl_match.group(2))
                    cited["games"] = cited["games"] if cited["games"] is not None else wins + losses
                    key_match = _H2H_KEY_RE.search(str(key))
                    key_a = key_match.group(1) if key_match else first_v
                    if first_v == key_a:
                        cited["a_wins"] = cited["a_wins"] if cited["a_wins"] is not None else wins
                        cited["b_wins"] = cited["b_wins"] if cited["b_wins"] is not None else losses
                    else:
                        cited["a_wins"] = cited["a_wins"] if cited["a_wins"] is not None else losses
                        cited["b_wins"] = cited["b_wins"] if cited["b_wins"] is not None else wins

                for field, value in cited.items():
                    if value is None:
                        continue
                    actual = int(row.get(field, 0) or 0)
                    if value != actual:
                        errors.append(
                            f"{alias} cited {field}={value}, snapshot has {field}={actual} (key {key})"
                        )
    return errors
