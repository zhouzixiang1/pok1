"""Generation-scoped evidence snapshots for LLM planning/audit.

The rating daemon keeps updating live result files while Master and audit LLMs
run. A plan that cites live H2H counts can become "stale" minutes later even if
the cited numbers were correct when the Master read them. This module creates a
stable per-generation H2H snapshot so Master and MasterPlanAudit validate
against the same evidence contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any


SNAPSHOT_DIRNAME = "evidence_snapshot"
H2H_SNAPSHOT_FILENAME = "head_to_head.json"
MANIFEST_FILENAME = "manifest.json"


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
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def ensure_generation_h2h_snapshot(next_v: int | str, *, force: bool = False) -> dict[str, Any]:
    """Create or return the stable H2H snapshot for ``next_v``."""
    infra = _infra()
    snapshot_dir = _snapshot_dir(next_v)
    snapshot_path = snapshot_dir / H2H_SNAPSHOT_FILENAME
    manifest_path = snapshot_dir / MANIFEST_FILENAME
    if snapshot_path.exists() and not force:
        manifest = _read_manifest(manifest_path) or {}
        return {
            **manifest,
            "available": True,
            "h2h_path": str(snapshot_path),
            "h2h_relpath": _repo_rel(snapshot_path),
            "manifest_path": str(manifest_path),
            "manifest_relpath": _repo_rel(manifest_path),
            "reused": True,
        }

    live_path = infra.H2H_FILE
    if not live_path.exists():
        return {
            "available": False,
            "reason": "missing_live_h2h",
            "h2h_path": str(live_path),
            "h2h_relpath": _repo_rel(live_path),
            "live_h2h_relpath": _repo_rel(live_path),
            "reused": False,
        }

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    try:
        with infra.locked_file(live_path, "rb") as handle:
            payload = handle.read()
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

    snapshot_path.write_bytes(payload)
    try:
        parsed = json.loads(payload.decode("utf-8"))
        entry_count = len(parsed) if isinstance(parsed, dict) else 0
    except Exception:
        entry_count = 0
    manifest = {
        "available": True,
        "next_v": int(next_v),
        "created_at": time.time(),
        "h2h_relpath": _repo_rel(snapshot_path),
        "manifest_relpath": _repo_rel(manifest_path),
        "live_h2h_relpath": _repo_rel(live_path),
        "sha256": _sha256(payload),
        "bytes": len(payload),
        "entries": entry_count,
        "reused": False,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        from system_log import log_system_event

        log_system_event(
            "pipeline.h2h_snapshot_created",
            "info",
            f"H2H evidence snapshot created for v{int(next_v)}",
            {k: manifest[k] for k in ("next_v", "h2h_relpath", "sha256", "entries", "bytes")},
        )
    except Exception:
        pass
    return {
        **manifest,
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


def h2h_snapshot_contract_text(next_v: int | str, *, include_json: bool = False, max_chars: int = 60_000) -> str:
    """Return prompt text that binds Master/Audit to the stable H2H snapshot."""
    snapshot = ensure_generation_h2h_snapshot(next_v)
    if not snapshot.get("available"):
        return (
            "Stable H2H snapshot unavailable; live H2H may be used as fallback: "
            f"{snapshot.get('h2h_relpath', 'web/core/results/head_to_head.json')}"
        )
    lines = [
        "Stable H2H evidence snapshot for this generation:",
        f"- Snapshot file: `{snapshot['h2h_relpath']}`",
        f"- Snapshot manifest: `{snapshot['manifest_relpath']}`",
        f"- Source live file at snapshot time: `{snapshot['live_h2h_relpath']}`",
        f"- sha256: `{snapshot.get('sha256', '')}`; entries: {snapshot.get('entries', 0)}; bytes: {snapshot.get('bytes', 0)}",
        "- For verbatim H2H counts in Master plans and MasterPlanAudit, use this snapshot only.",
        "- Do not reject a plan because the live `web/core/results/head_to_head.json` changed after this snapshot was created.",
    ]
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


def validate_h2h_citations_against_snapshot(master_plan: Any, next_v: int | str) -> list[str]:
    """Detect labeled H2H count citations that disagree with the generation snapshot."""
    h2h = load_generation_h2h_snapshot(next_v)
    if not h2h:
        return []
    text = _flatten_text(master_plan)
    errors: list[str] = []
    for key, row in h2h.items():
        if not isinstance(row, dict) or key not in text:
            continue
        start = 0
        while True:
            idx = text.find(key, start)
            if idx < 0:
                break
            window = text[idx:idx + 360]
            cited = {
                "games": _extract_int(r"\bgames?\s*[:=]\s*(\d+)", window),
                "a_wins": _extract_int(r"\ba_wins\s*[:=]\s*(\d+)", window),
                "b_wins": _extract_int(r"\bb_wins\s*[:=]\s*(\d+)", window),
            }
            if cited["games"] is None:
                cited["games"] = _extract_int(r"(?<![\w.])(\d+)\s*(?:g|games|局)\b", window)
            for field, value in cited.items():
                if value is None:
                    continue
                actual = int(row.get(field, 0) or 0)
                if value != actual:
                    errors.append(
                        f"{key} cited {field}={value}, snapshot has {field}={actual}"
                    )
            start = idx + len(key)
    return errors
