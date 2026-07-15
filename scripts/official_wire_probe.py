#!/usr/bin/env python3
"""Run the official Windows platform through a raw TCP diagnostic proxy.

Wire-probe output is diagnostic only: it cannot certify or rate a bot.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, is_dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "web" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

from bot_artifact import canonical_digest, hash_path  # noqa: E402
from bot_namespace import (  # noqa: E402
    ROLE_CANDIDATE,
    STRICT_ARTIFACT_FILES,
    policy_identity_document_errors,
    resolve_national_bot_spec,
)
from managed_bot_executor import EndpointLease  # noqa: E402
from national_runtime_authority import (  # noqa: E402
    current_system_native_runtime_errors,
)
from official_bot_sandbox import (  # noqa: E402
    launch_sandboxed_bot,
    seal_bot_artifact,
)
from official_platform_harness import (  # noqa: E402
    OfficialPlatformConfig,
    _choose_display,
    _click,
    _close_process_files,
    _close_window,
    _copy_config,
    _env_for_display,
    _kill_wineprefix,
    _maybe_screenshot,
    _official_platform_lock,
    _popen,
    _port_busy_before_start,
    _read_issue_file,
    _terminate_process,
    _type_text,
    _wait_for_listen,
    _wait_for_window,
    _wait_for_wine_idle,
    check_environment,
)
from official_wire_probe import TcpWireProbe, WireEventRecorder, replay_events  # noqa: E402


def _diagnostic_target_hands(value: str) -> int:
    try:
        target = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("target hands must be an integer") from exc
    if not 1 <= target <= 69:
        raise argparse.ArgumentTypeError(
            "standalone wire diagnostics support 1..69 hands; a natural "
            "70-hand result requires scripts/official_certify.py full so wire "
            "hands 1..69 can be cross-bound to THP state 69 and the footer"
        )
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    base = OfficialPlatformConfig()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, help="Strict five-file candidate bot directory.")
    parser.add_argument("--opponent", required=True, help="Strict five-file opponent bot directory.")
    parser.add_argument(
        "--target-hands",
        type=_diagnostic_target_hands,
        default=1,
        help="Diagnostic wire settlements to observe (1..69 only; default: 1).",
    )
    parser.add_argument("--results-dir", default=str(ROOT / "web" / "core" / "results" / "official_wire_probe"))
    parser.add_argument("--exe", default=str(base.exe_path))
    parser.add_argument("--wineprefix", default=str(base.wineprefix))
    parser.add_argument("--round-timeout", type=float, default=900.0)
    parser.add_argument("--no-progress-timeout", type=float, default=90.0)
    parser.add_argument("--settlement-grace", type=float, default=2.0)
    parser.add_argument("--stop-on-wire-issue", action="store_true", help="Stop immediately when replay finds a protocol issue.")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> OfficialPlatformConfig:
    base = OfficialPlatformConfig()
    return _copy_config(
        base,
        exe_path=Path(args.exe).expanduser(),
        wineprefix=Path(args.wineprefix).expanduser(),
        results_dir=Path(args.results_dir).expanduser(),
        round_timeout_sec=float(args.round_timeout),
        no_progress_timeout_sec=float(args.no_progress_timeout),
        settlement_grace_sec=float(args.settlement_grace),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def _strict_bot_directory(path: str | Path) -> Path:
    requested = Path(path).expanduser().absolute()
    if requested.is_symlink() or not requested.is_dir():
        raise ValueError(
            "official_wire_probe_requires_strict_bot_directory; "
            "arbitrary script paths are forbidden"
        )
    return requested.resolve()


def _validate_strict_candidate(path: str | Path) -> tuple[Path, dict[str, Any]]:
    """Validate an unpublished-or-published strict candidate without importing it."""

    source = _strict_bot_directory(path)
    repo_root = source.parent.parent
    spec = resolve_national_bot_spec(
        source,
        ROLE_CANDIDATE,
        repo_root=repo_root,
        require_completion=False,
        require_certificate=False,
    )
    issues = list(spec.issues)
    lineage = spec.epoch_receipt.get("lineage") if spec.epoch_receipt else {}
    raw_parents = lineage.get("parent_versions") if isinstance(lineage, dict) else []
    parents = (
        tuple(int(item) for item in raw_parents)
        if isinstance(raw_parents, list)
        and all(isinstance(item, int) and not isinstance(item, bool) for item in raw_parents)
        else ()
    )
    if spec.version is not None:
        issues.extend(
            policy_identity_document_errors(
                source,
                spec.version,
                parent_versions=parents,
            )
        )
    issues.extend(current_system_native_runtime_errors(source))
    issues = list(dict.fromkeys(str(item) for item in issues))
    if issues:
        raise RuntimeError(
            "official_wire_probe_strict_candidate_invalid:"
            + ";".join(issues[:12])
        )
    artifact_hash = hash_path(source)
    validation = {
        "schema_version": 1,
        "role": ROLE_CANDIDATE,
        "label": spec.label,
        "version": spec.version,
        "strict_artifact_files": sorted(STRICT_ARTIFACT_FILES),
        "artifact_hash": artifact_hash,
        "runtime_manifest_digest": canonical_digest(spec.runtime_manifest),
        "policy_epoch_receipt_digest": canonical_digest(spec.epoch_receipt),
        "issues": [],
    }
    validation["validation_digest"] = canonical_digest(validation)
    return source, validation


def _launch_managed_probe_bot(
    *,
    bot_path: Path,
    name: str,
    seat: str,
    host: str,
    port: int,
    log_path: Path,
    sealed_root: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[subprocess.Popen, dict[str, Any]]:
    """Launch one unpublished-or-published strict bot through the formal boundary."""

    source, source_validation = _validate_strict_candidate(bot_path)
    artifact_hash = str(source_validation["artifact_hash"])
    artifact = seal_bot_artifact(
        source,
        sealed_root,
        expected_hash=artifact_hash,
    )
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    try:
        with EndpointLease.connect(host, int(port), timeout=10.0) as endpoint:
            managed = launch_sandboxed_bot(
                artifact,
                endpoint,
                name=name,
                seat=seat,
                log_path=log_path,
                supports_log=True,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        launch_receipt = {
            "mode": "central-managed-sealed-source-projection",
            "source": str(source),
            "sealed_root": str(artifact.root),
            "artifact_hash": artifact.artifact_hash,
            "source_validation": source_validation,
            "endpoint": {"host": host, "port": int(port)},
            "endpoint_lease": {
                "consumed": endpoint.consumed,
                "closed": endpoint.closed,
            },
            "isolation": asdict(managed.isolation),
        }
    except BaseException:
        stdout.close()
        stderr.close()
        raise
    proc = managed.process
    proc._pok_stdout = stdout  # type: ignore[attr-defined]
    proc._pok_stderr = stderr  # type: ignore[attr-defined]
    return proc, launch_receipt


def _target_reached(summary: dict[str, Any], target_hands: int) -> bool:
    return (
        int(summary.get("hands_started_min", 0) or 0) >= target_hands
        and int(summary.get("settlements_min", 0) or 0) >= target_hands
        and not summary.get("pending_expected_actions")
    )


def _format_wire_issues(summary: dict[str, Any]) -> list[str]:
    return [
        f"wire_{issue.get('kind')}: conn={issue.get('conn')} "
        f"hand={issue.get('hand')} stage={issue.get('stage')} "
        f"msg={issue.get('message')!r} reason={issue.get('reason', '')}"
        for issue in summary.get("issues") or []
    ]


async def _run_probe_round(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _config_from_args(args)
    env_report = check_environment(cfg, require_formal_sandbox=True)
    round_dir = (cfg.results_dir / time.strftime("probe_%Y%m%d_%H%M%S")).resolve()
    round_dir.mkdir(parents=True, exist_ok=True)
    receipt: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target_hands": int(args.target_hands),
        "authority": {
            "scope": "wire_diagnostic_only",
            "certification_weight": 0,
            "rating_weight": 0,
            "formal_70_hand_completion_proven": False,
            "formal_70_hand_path": "scripts/official_certify.py full",
            "terminal_oracle_shape": "wire starts 1..70; settlements 1..69; THP states 0..69",
        },
        "candidate": str(Path(args.candidate).expanduser().resolve()),
        "opponent": str(Path(args.opponent).expanduser().resolve()),
        "config": _jsonable(cfg),
        "environment": env_report,
        "artifacts": {"round_dir": str(round_dir)},
        "issues": [],
        "passed": False,
    }
    source_validation: dict[str, Any] = {}
    try:
        candidate_path, candidate_validation = _validate_strict_candidate(
            args.candidate
        )
        opponent_path, opponent_validation = _validate_strict_candidate(
            args.opponent
        )
        source_validation = {
            "candidate": candidate_validation,
            "opponent": opponent_validation,
        }
    except Exception as exc:
        issue = (
            "official_wire_probe_source_validation_failed:"
            f"{type(exc).__name__}:{str(exc)[:500]}"
        )
        receipt["source_validation"] = {
            "issues": [issue],
            "validation_digest": "",
        }
        receipt["issues"].append(issue)
        _write_json(round_dir / "receipt.json", receipt)
        return receipt
    receipt["source_validation"] = source_validation
    if not env_report["ok"]:
        receipt["issues"].extend(env_report["issues"])
        _write_json(round_dir / "receipt.json", receipt)
        return receipt

    if _port_busy_before_start(cfg):
        cleanup_env = os.environ.copy()
        cleanup_env.update(cfg.locale_env())
        _kill_wineprefix(cleanup_env)
        _wait_for_wine_idle(cleanup_env, timeout_sec=3.0)
        if _port_busy_before_start(cfg):
            receipt["issues"].append(f"port_busy_before_start: {cfg.host}:{cfg.port}")
            _write_json(round_dir / "receipt.json", receipt)
            return receipt

    display = _choose_display()
    xvfb_proc: subprocess.Popen | None = None
    wine_proc: subprocess.Popen | None = None
    bot_a_proc: subprocess.Popen | None = None
    bot_b_proc: subprocess.Popen | None = None
    platform_env: dict[str, str] | None = None
    window_id: str | None = None
    recorder = WireEventRecorder(round_dir / "wire_events.jsonl")
    proxy = TcpWireProbe(platform_host=cfg.host, platform_port=cfg.port, recorder=recorder)
    started_at = time.time()

    try:
        xvfb_log = (round_dir / "xvfb.log").open("wb")
        xvfb_proc = _popen(
            ["Xvfb", display, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
            cwd=None,
            env=os.environ.copy(),
            stdout=xvfb_log,
            stderr=xvfb_log,
        )
        xvfb_proc._pok_stdout = xvfb_log  # type: ignore[attr-defined]
        await asyncio.sleep(0.8)

        env = _env_for_display(cfg, display)
        platform_env = env
        platform_log = round_dir / "platform.wine.log"
        with platform_log.open("wb") as platform_out:
            wine_proc = _popen(["wine", str(cfg.exe_path)], cwd=cfg.exe_path.parent, env=env, stdout=platform_out, stderr=platform_out)
            window_id = _wait_for_window(env, timeout_sec=cfg.startup_timeout_sec)
            screenshot = _maybe_screenshot(env, round_dir / "screenshots" / "01_start.png", "start")
            if screenshot:
                receipt["artifacts"]["start_screenshot"] = screenshot
            _click(env, window_id, cfg.ui.gear_x, cfg.ui.gear_y)
            await asyncio.sleep(0.8)
            _click(env, window_id, cfg.ui.ip_x, cfg.ui.ip_y)
            _type_text(env, window_id, cfg.host)
            _click(env, window_id, cfg.ui.start_x, cfg.ui.start_y)
            _wait_for_listen(cfg)

            proxy_ports = await proxy.start(cfg.host)
            receipt["proxy_ports"] = proxy_ports
            bot_a_proc, launch_a = _launch_managed_probe_bot(
                bot_path=candidate_path,
                name="Candidate",
                seat="upper",
                host=cfg.host,
                port=proxy_ports["A"],
                log_path=round_dir / "botA.log",
                sealed_root=round_dir / "managed_inputs" / "candidate",
                stdout_path=round_dir / "botA.stdout.log",
                stderr_path=round_dir / "botA.stderr.log",
            )
            bot_b_proc, launch_b = _launch_managed_probe_bot(
                bot_path=opponent_path,
                name="Opponent",
                seat="lower",
                host=cfg.host,
                port=proxy_ports["B"],
                log_path=round_dir / "botB.log",
                sealed_root=round_dir / "managed_inputs" / "opponent",
                stdout_path=round_dir / "botB.stdout.log",
                stderr_path=round_dir / "botB.stderr.log",
            )
            receipt["managed_launches"] = {
                "candidate": launch_a,
                "opponent": launch_b,
            }
            await asyncio.sleep(2.0)
            _click(env, window_id, cfg.ui.ok_x, cfg.ui.ok_y)

            target_hands = receipt["target_hands"]
            deadline = started_at + cfg.round_timeout_sec
            last_event_count = -1
            last_progress_at = time.time()
            target_reached_at: float | None = None
            summary: dict[str, Any] = {}
            while time.time() < deadline:
                summary = replay_events(recorder.events)
                if summary["events_seen"] != last_event_count:
                    last_event_count = summary["events_seen"]
                    last_progress_at = time.time()
                if summary["issues"] and args.stop_on_wire_issue:
                    receipt["issues"].extend(_format_wire_issues(summary))
                    break
                if bot_a_proc.poll() is not None and not _target_reached(summary, target_hands):
                    receipt["issues"].append(f"candidate_exited_early: rc={bot_a_proc.returncode}")
                    break
                if bot_b_proc.poll() is not None and not _target_reached(summary, target_hands):
                    receipt["issues"].append(f"opponent_exited_early: rc={bot_b_proc.returncode}")
                    break
                if _target_reached(summary, target_hands):
                    if target_reached_at is None:
                        target_reached_at = time.time()
                    if time.time() - target_reached_at >= cfg.settlement_grace_sec:
                        break
                elif time.time() - last_progress_at > cfg.no_progress_timeout_sec:
                    receipt["issues"].append(
                        f"wire_no_progress_timeout: {cfg.no_progress_timeout_sec:g}s "
                        f"hands_started={summary.get('hands_started_min', 0)} "
                        f"settlements={summary.get('settlements_min', 0)} "
                        f"pending={summary.get('pending_expected_actions', [])}"
                    )
                    break
                await asyncio.sleep(0.5)
            else:
                summary = replay_events(recorder.events)
                receipt["issues"].append(
                    f"wire_round_timeout: {cfg.round_timeout_sec:g}s "
                    f"hands_started={summary.get('hands_started_min', 0)} "
                    f"settlements={summary.get('settlements_min', 0)}"
                )

            receipt["wire_summary"] = summary or replay_events(recorder.events)
            final = _maybe_screenshot(env, round_dir / "screenshots" / "04_final.png", "final")
            if final:
                receipt["artifacts"]["final_screenshot"] = final
    finally:
        for proc in (bot_a_proc, bot_b_proc):
            _terminate_process(proc)
            _close_process_files(proc)
        await proxy.stop()
        recorder.close()
        if platform_env is not None:
            _close_window(platform_env, window_id)
            time.sleep(2.0)
        _terminate_process(wine_proc)
        if platform_env is not None:
            _wait_for_wine_idle(platform_env, timeout_sec=cfg.artifact_grace_sec)
            if _port_busy_before_start(cfg):
                _kill_wineprefix(platform_env)
                _wait_for_wine_idle(platform_env, timeout_sec=3.0)
        _terminate_process(xvfb_proc)
        for proc in (wine_proc, xvfb_proc):
            _close_process_files(proc)

    summary = receipt.get("wire_summary") or replay_events(recorder.events)
    receipt["wire_summary"] = summary
    receipt["duration_sec"] = round(time.time() - started_at, 2)
    receipt["artifacts"].update({
        "wire_events": str(round_dir / "wire_events.jsonl"),
        "wire_summary": str(round_dir / "wire_summary.json"),
        "receipt": str(round_dir / "receipt.json"),
        "platform_log": str(round_dir / "platform.wine.log"),
        "bot_a_log": str(round_dir / "botA.log"),
        "bot_b_log": str(round_dir / "botB.log"),
        "bot_a_stdout": str(round_dir / "botA.stdout.log"),
        "bot_a_stderr": str(round_dir / "botA.stderr.log"),
        "bot_b_stdout": str(round_dir / "botB.stdout.log"),
        "bot_b_stderr": str(round_dir / "botB.stderr.log"),
    })
    receipt["issues"].extend(_format_wire_issues(summary))
    receipt["issues"].extend(_read_issue_file(round_dir / "botA.stderr.log"))
    receipt["issues"].extend(_read_issue_file(round_dir / "botB.stderr.log"))
    receipt["issues"] = list(dict.fromkeys(str(issue) for issue in receipt["issues"]))
    receipt["passed"] = not receipt["issues"] and _target_reached(summary, receipt["target_hands"])
    _write_json(round_dir / "wire_summary.json", summary)
    _write_json(round_dir / "receipt.json", receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = _config_from_args(args)
    with _official_platform_lock(cfg):
        receipt = asyncio.run(_run_probe_round(args))
    print(json.dumps({
        "passed": receipt["passed"],
        "issues": receipt["issues"][:20],
        "wire_summary": receipt.get("wire_summary", {}),
        "receipt": receipt["artifacts"].get("receipt"),
    }, ensure_ascii=False, indent=2))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
