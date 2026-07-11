#!/usr/bin/env python3
"""Append-only opponent exposure ledger for neural-policy evidence roles."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator


SCHEMA = "opponent_exposure_ledger_v1"
FINAL_BLIND_ROLE = "final_blind"
EXPOSURE_ROLES = (
    "train",
    "early_stop",
    "model_calibration",
    "policy_selection",
    "policy_gate",
    "development_native_eval",
    "regression",
    FINAL_BLIND_ROLE,
)
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LEDGER = (
    ROOT
    / "bots"
    / "neural_national_lab"
    / "data"
    / "oppmodel"
    / "exposure_ledger.json"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_opponents(opponents: list[str] | tuple[str, ...]) -> list[str]:
    values = sorted({str(value).strip() for value in opponents if str(value).strip()})
    if not values:
        raise ValueError("at least one opponent is required")
    return values


def _normalize_sha256(
    value: str | None, *, field: str, required: bool = False
) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a 64-character SHA-256 hex digest")
    return normalized


def _new_ledger() -> dict[str, Any]:
    return {"schema": SCHEMA, "events": []}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _new_ledger()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA or not isinstance(payload.get("events"), list):
        raise ValueError(f"invalid exposure ledger: {path}")
    return payload


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


@contextmanager
def _locked(path: Path) -> Iterator[dict[str, Any]]:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        payload = _load(path)
        yield payload
        _write_atomic(path, payload)
        fcntl.flock(lock, fcntl.LOCK_UN)


def _append_event(
    payload: dict[str, Any],
    *,
    event: str,
    role: str,
    run_id: str,
    opponents: list[str],
    candidate_sha256: str | None = None,
    artifact_sha256: str | None = None,
) -> dict[str, Any]:
    row = {
        "sequence": len(payload["events"]) + 1,
        "timestamp_utc": _utc_now(),
        "event": event,
        "role": role,
        "run_id": run_id,
        "opponents": opponents,
        "candidate_sha256": candidate_sha256,
        "artifact_sha256": artifact_sha256,
    }
    payload["events"].append(row)
    return row


def _derived(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for event in payload["events"]:
        for opponent in event.get("opponents") or []:
            item = state.setdefault(opponent, {
                "reservation": None,
                "exposures": [],
            })
            kind = event.get("event")
            if kind == "reserve" and event.get("role") == FINAL_BLIND_ROLE:
                item["reservation"] = {
                    "run_id": event.get("run_id"),
                    "sequence": event.get("sequence"),
                    "candidate_sha256": event.get("candidate_sha256"),
                }
            elif kind == "release" and event.get("role") == FINAL_BLIND_ROLE:
                reservation = item.get("reservation")
                if reservation and reservation.get("run_id") == event.get("run_id"):
                    item["reservation"] = None
            elif kind == "open":
                item["exposures"].append({
                    "role": event.get("role"),
                    "run_id": event.get("run_id"),
                    "sequence": event.get("sequence"),
                    "candidate_sha256": event.get("candidate_sha256"),
                    "artifact_sha256": event.get("artifact_sha256"),
                })
                if event.get("role") == FINAL_BLIND_ROLE:
                    item["reservation"] = None
    return state


def reserve_final_blind(
    path: Path,
    *,
    opponents: list[str] | tuple[str, ...],
    run_id: str,
    candidate_sha256: str | None = None,
) -> dict[str, Any]:
    names = _normalize_opponents(opponents)
    run_id = str(run_id).strip()
    if not run_id:
        raise ValueError("run_id is required")
    candidate_sha256 = _normalize_sha256(
        candidate_sha256, field="candidate_sha256", required=True
    )
    with _locked(path) as payload:
        state = _derived(payload)
        for name in names:
            item = state.get(name) or {"reservation": None, "exposures": []}
            if item["exposures"]:
                raise ValueError(f"opponent already exposed and not blind: {name}")
            reservation = item["reservation"]
            if reservation and reservation["run_id"] != run_id:
                raise ValueError(
                    f"opponent reserved by another final-blind run: {name}"
                )
            if reservation and reservation.get("candidate_sha256") != candidate_sha256:
                raise ValueError(
                    f"final-blind reservation candidate mismatch: {name}"
                )
        already_reserved = all(
            ((state.get(name) or {}).get("reservation") or {}).get("run_id")
            == run_id
            for name in names
        )
        if already_reserved:
            return {"changed": False, "opponents": names, "run_id": run_id}
        event = _append_event(
            payload,
            event="reserve",
            role=FINAL_BLIND_ROLE,
            run_id=run_id,
            opponents=names,
            candidate_sha256=candidate_sha256,
        )
        return {"changed": True, "event": event}


def release_final_blind(
    path: Path,
    *,
    opponents: list[str] | tuple[str, ...],
    run_id: str,
) -> dict[str, Any]:
    names = _normalize_opponents(opponents)
    run_id = str(run_id).strip()
    with _locked(path) as payload:
        state = _derived(payload)
        for name in names:
            item = state.get(name) or {"reservation": None, "exposures": []}
            reservation = item["reservation"]
            if not reservation or reservation["run_id"] != run_id:
                raise ValueError(f"final-blind reservation not owned by run: {name}")
            if item["exposures"]:
                raise ValueError(f"cannot release an opened opponent: {name}")
        event = _append_event(
            payload,
            event="release",
            role=FINAL_BLIND_ROLE,
            run_id=run_id,
            opponents=names,
        )
        return {"changed": True, "event": event}


def open_exposure(
    path: Path,
    *,
    role: str,
    opponents: list[str] | tuple[str, ...],
    run_id: str,
    candidate_sha256: str | None = None,
    artifact_sha256: str | None = None,
) -> dict[str, Any]:
    role = str(role)
    if role not in EXPOSURE_ROLES:
        raise ValueError(f"unsupported exposure role: {role}")
    names = _normalize_opponents(opponents)
    run_id = str(run_id).strip()
    if not run_id:
        raise ValueError("run_id is required")
    candidate_sha256 = _normalize_sha256(
        candidate_sha256,
        field="candidate_sha256",
        required=role == FINAL_BLIND_ROLE,
    )
    artifact_sha256 = _normalize_sha256(
        artifact_sha256,
        field="artifact_sha256",
        required=role == FINAL_BLIND_ROLE,
    )
    with _locked(path) as payload:
        exact = [
            event for event in payload["events"]
            if (
                event.get("event") == "open"
                and event.get("role") == role
                and event.get("run_id") == run_id
                and event.get("opponents") == names
                and event.get("candidate_sha256") == candidate_sha256
                and event.get("artifact_sha256") == artifact_sha256
            )
        ]
        if exact and role != FINAL_BLIND_ROLE:
            return {"changed": False, "event": exact[-1]}
        state = _derived(payload)
        for name in names:
            item = state.get(name) or {"reservation": None, "exposures": []}
            reservation = item["reservation"]
            final_exposures = [
                row for row in item["exposures"]
                if row["role"] == FINAL_BLIND_ROLE
            ]
            if role == FINAL_BLIND_ROLE:
                if not reservation or reservation["run_id"] != run_id:
                    raise ValueError(
                        f"final-blind opponent was not reserved by this run: {name}"
                    )
                if reservation.get("candidate_sha256") != candidate_sha256:
                    raise ValueError(
                        f"final-blind reservation candidate mismatch: {name}"
                    )
                if item["exposures"]:
                    raise ValueError(f"opponent was exposed before final blind: {name}")
            else:
                if reservation:
                    raise ValueError(f"opponent is reserved for final blind: {name}")
                if final_exposures and role != "regression":
                    raise ValueError(
                        f"final-blind opponent may only become regression data: {name}"
                    )
        event = _append_event(
            payload,
            event="open",
            role=role,
            run_id=run_id,
            opponents=names,
            candidate_sha256=candidate_sha256,
            artifact_sha256=artifact_sha256,
        )
        return {"changed": True, "event": event}


def status(path: Path) -> dict[str, Any]:
    path = path.resolve()
    payload = _load(path)
    return {
        "schema": SCHEMA,
        "path": str(path),
        "events": len(payload["events"]),
        "opponents": _derived(payload),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    subparsers = parser.add_subparsers(dest="command", required=True)

    reserve = subparsers.add_parser("reserve-final")
    reserve.add_argument("--run-id", required=True)
    reserve.add_argument("--candidate-sha256")
    reserve.add_argument("--opponent", action="append", required=True)

    release = subparsers.add_parser("release-final")
    release.add_argument("--run-id", required=True)
    release.add_argument("--opponent", action="append", required=True)

    opened = subparsers.add_parser("open")
    opened.add_argument("--role", choices=EXPOSURE_ROLES, required=True)
    opened.add_argument("--run-id", required=True)
    opened.add_argument("--candidate-sha256")
    opened.add_argument("--artifact-sha256")
    opened.add_argument("--opponent", action="append", required=True)

    subparsers.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "reserve-final":
        result = reserve_final_blind(
            args.ledger,
            opponents=args.opponent,
            run_id=args.run_id,
            candidate_sha256=args.candidate_sha256,
        )
    elif args.command == "release-final":
        result = release_final_blind(
            args.ledger, opponents=args.opponent, run_id=args.run_id
        )
    elif args.command == "open":
        result = open_exposure(
            args.ledger,
            role=args.role,
            opponents=args.opponent,
            run_id=args.run_id,
            candidate_sha256=args.candidate_sha256,
            artifact_sha256=args.artifact_sha256,
        )
    else:
        result = status(args.ledger)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
