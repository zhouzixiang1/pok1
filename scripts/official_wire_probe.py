#!/usr/bin/env python3
"""Run the official Windows platform through a raw TCP diagnostic proxy."""

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
    resolve_bot_entry,
)
from official_wire_probe import TcpWireProbe, WireEventRecorder, replay_events  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    base = OfficialPlatformConfig()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, help="Candidate bot directory/script.")
    parser.add_argument("--opponent", required=True, help="Opponent bot directory/script.")
    parser.add_argument("--candidate-kind", choices=("native", "sample"), default="native")
    parser.add_argument("--opponent-kind", choices=("native", "sample"), default="native")
    parser.add_argument("--target-hands", type=int, default=70)
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


def _build_bot_command(
    *,
    bot_path: Path,
    kind: str,
    name: str,
    seat: str,
    host: str,
    port: int,
    log_path: Path,
) -> tuple[list[str], Path]:
    if kind == "sample":
        runner = ROOT / "scripts" / "run_national_sample.py"
        return (
            [
                sys.executable,
                str(runner),
                "--script",
                str(bot_path),
                "--host",
                host,
                "--port",
                str(port),
                "--name",
                name,
                "--seat",
                seat,
                "--log",
                str(log_path),
            ],
            runner.parent,
        )

    entry = resolve_bot_entry(bot_path)
    return (
        [
            sys.executable,
            str(entry),
            "--host",
            host,
            "--port",
            str(port),
            "--name",
            name,
            "--seat",
            seat,
            "--log",
            str(log_path),
        ],
        entry.parent,
    )


def _launch_command(cmd: list[str], *, cwd: Path, env: dict[str, str], stdout_path: Path, stderr_path: Path) -> subprocess.Popen:
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    proc = _popen(cmd, cwd=cwd, env=env, stdout=stdout, stderr=stderr)
    proc._pok_stdout = stdout  # type: ignore[attr-defined]
    proc._pok_stderr = stderr  # type: ignore[attr-defined]
    return proc


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
    env_report = check_environment(cfg)
    round_dir = (cfg.results_dir / time.strftime("probe_%Y%m%d_%H%M%S")).resolve()
    round_dir.mkdir(parents=True, exist_ok=True)
    receipt: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target_hands": max(1, min(70, int(args.target_hands))),
        "candidate": str(Path(args.candidate).expanduser().resolve()),
        "opponent": str(Path(args.opponent).expanduser().resolve()),
        "candidate_kind": args.candidate_kind,
        "opponent_kind": args.opponent_kind,
        "config": _jsonable(cfg),
        "environment": env_report,
        "artifacts": {"round_dir": str(round_dir)},
        "issues": [],
        "passed": False,
    }
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
            candidate_path = Path(args.candidate).expanduser().resolve()
            opponent_path = Path(args.opponent).expanduser().resolve()
            cmd_a, cwd_a = _build_bot_command(
                bot_path=candidate_path,
                kind=args.candidate_kind,
                name="Candidate",
                seat="upper",
                host=cfg.host,
                port=proxy_ports["A"],
                log_path=round_dir / "botA.log",
            )
            cmd_b, cwd_b = _build_bot_command(
                bot_path=opponent_path,
                kind=args.opponent_kind,
                name="Opponent",
                seat="lower",
                host=cfg.host,
                port=proxy_ports["B"],
                log_path=round_dir / "botB.log",
            )
            receipt["commands"] = {"candidate": cmd_a, "opponent": cmd_b}
            bot_a_proc = _launch_command(
                cmd_a,
                cwd=cwd_a,
                env=env,
                stdout_path=round_dir / "botA.stdout.log",
                stderr_path=round_dir / "botA.stderr.log",
            )
            bot_b_proc = _launch_command(
                cmd_b,
                cwd=cwd_b,
                env=env,
                stdout_path=round_dir / "botB.stdout.log",
                stderr_path=round_dir / "botB.stderr.log",
            )
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
