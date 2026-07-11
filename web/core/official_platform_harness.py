"""Official Windows national-platform harness.

This module drives the real competition executable under Wine/Xvfb, starts two
native TCP bot processes, and records the platform screenshots plus bot logs as
compliance evidence. It is intentionally separate from ``national_native``:
that module is the fast local simulator and strength tracker, while this module
is the official protocol-compliance oracle used when the environment enables the
EXE gate.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from bot_artifact import canonical_digest
from pipeline_schema import NationalAcceptanceResult
from official_attribution import round_topology
from official_bot_sandbox import (
    SealedBotArtifact,
    build_sandboxed_bot_command,
    seal_bot_artifact,
)
from managed_bot_socket import connect_managed_endpoint
from official_execution_profile import (
    execution_profile_identity,
    validate_execution_profile,
)
from blocking_runtime import run_blocking_isolated
from official_platform_resource import acquire_official_platform


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXE_PATH = (
    ROOT
    / "sever"
    / "国赛平台"
    / "德州扑克对弈平台限时一分钟2021版"
    / "德州扑克对弈平台限时一分钟2021版.exe"
)
DEFAULT_WINEPREFIX = Path(
    os.environ.get(
        "POK_OFFICIAL_WINEPREFIX",
        str(Path.home() / ".cache" / "pok_wine_national_platform"),
    )
)
DEFAULT_RESULTS_DIR = ROOT / "web" / "core" / "results" / "official_platform"
WINDOW_TITLE = "德州扑克自对弈平台"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10001
CRITICAL_LOG_PATTERNS = (
    "Traceback",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "BrokenPipeError",
    "TimeoutError",
    "unknown action",
    "unknown command",
    "unknown message",
    "unknown protocol",
    "illegal",
    "invalid action",
    "protocol error",
)
SEND_MSG_RE = re.compile(r"\bSEND\b.*\bmsg='([^']*)'")
RAISE_ACTION_RE = re.compile(r"^raise [1-9]\d*$")
THP_HAND_RE = re.compile(r"\bSTATE:(\d+):")
THP_RECORD_RE = re.compile(
    r"\bSTATE:(\d+):([^:]*):([^:]*):(-?\d+)\|(-?\d+):([^|;]+)\|([^;]+);"
)
THP_FOOTER_RE = re.compile(
    r"\{\[THP\]\[([^\]]+)\]\[([^\]]+)\]\[([^\]]+)\]"
    r"\[([^\]]+)\]\[([^\]]+)\]\}"
)
TERMINAL_COMPLETION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PlatformUiProfile:
    """Window-relative coordinates measured against the 2021 EXE layout."""

    gear_x: int = 72
    gear_y: int = 42
    ip_x: int = 677
    ip_y: int = 343
    start_x: int = 657
    start_y: int = 392
    ok_x: int = 752
    ok_y: int = 392


@dataclass(frozen=True)
class OfficialPlatformConfig:
    exe_path: Path = field(default_factory=lambda: Path(os.environ.get("POK_OFFICIAL_PLATFORM_EXE", DEFAULT_EXE_PATH)))
    wineprefix: Path = field(default_factory=lambda: DEFAULT_WINEPREFIX)
    results_dir: Path = field(default_factory=lambda: Path(os.environ.get("POK_OFFICIAL_RESULTS_DIR", DEFAULT_RESULTS_DIR)))
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    startup_timeout_sec: float = 45.0
    listen_timeout_sec: float = 45.0
    no_progress_timeout_sec: float = 75.0
    round_timeout_sec: float = 900.0
    lock_timeout_sec: float = field(default_factory=lambda: float(os.environ.get("POK_OFFICIAL_LOCK_TIMEOUT_SEC", "900")))
    settlement_grace_sec: float = field(default_factory=lambda: float(os.environ.get("POK_OFFICIAL_SETTLEMENT_GRACE_SEC", "2.0")))
    artifact_grace_sec: float = 20.0
    lock_path: Path = field(default_factory=lambda: Path(os.environ.get("POK_OFFICIAL_LOCK_PATH", "/tmp/pok_official_platform.lock")))
    ui: PlatformUiProfile = field(default_factory=PlatformUiProfile)

    def locale_env(self) -> dict[str, str]:
        return {
            "WINEPREFIX": str(self.wineprefix),
            "LC_ALL": "zh_CN.UTF-8",
            "LANG": "zh_CN.UTF-8",
            "LANGUAGE": "zh_CN:zh",
        }


@dataclass(frozen=True)
class BotLaunchConfig:
    path: Path
    name: str
    seat: str = "auto"
    role: str = ""
    instance_id: str = ""
    python: str = sys.executable
    supports_log: bool = True
    supports_seat: bool = True
    extra_args: tuple[str, ...] = ()
    sealed_artifact: SealedBotArtifact | None = None


@dataclass
class BotLogStats:
    path: str
    exists: bool = False
    bytes: int = 0
    preflop: int = 0
    earnchips: int = 0
    sends: int = 0
    max_hand: int = 0
    net_chips: int = 0
    max_gap_sec: int = 0
    max_decision_sec: float = 0.0
    issues: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)

    def progress_key(self) -> tuple[int, int, int, int]:
        return (self.preflop, self.earnchips, self.sends, self.bytes)


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def resolve_bot_entry(path: str | Path) -> Path:
    """Resolve a native bot directory or script to the executable script path."""
    candidate = Path(path).expanduser().resolve()
    if candidate.is_dir():
        native = candidate / "national_bot.py"
        if native.exists():
            return native
        raise FileNotFoundError(f"no national_bot.py under {candidate}")
    if candidate.exists():
        return candidate
    raise FileNotFoundError(str(candidate))


def build_bot_command(
    bot: BotLaunchConfig,
    *,
    host: str,
    port: int,
    log_path: Path | None = None,
) -> list[str]:
    entry = resolve_bot_entry(bot.path)
    cmd = [
        bot.python,
        str(entry),
        "--host",
        host,
        "--port",
        str(port),
        "--name",
        bot.name,
    ]
    if bot.supports_seat:
        cmd.extend(["--seat", bot.seat])
    if bot.supports_log and log_path is not None:
        cmd.extend(["--log", str(log_path)])
    cmd.extend(bot.extra_args)
    return cmd


def _seconds_from_timestamp(line: str) -> int | None:
    match = re.match(r"\[(\d{2}):(\d{2}):(\d{2})\]", line)
    if not match:
        return None
    hour, minute, second = (int(part) for part in match.groups())
    return hour * 3600 + minute * 60 + second


def _line_gap(prev: int | None, current: int | None) -> int:
    if prev is None or current is None:
        return 0
    if current >= prev:
        return current - prev
    return current + 24 * 3600 - prev


def _sent_action_issue(message: str) -> str | None:
    if message in {"call", "check", "fold", "allin"}:
        return None
    if RAISE_ACTION_RE.fullmatch(message):
        return None
    if message.startswith("bet"):
        return f"illegal_bet_action: msg={message!r}"
    if message.strip() != message:
        return f"protocol_action_whitespace: msg={message!r}"
    if message.startswith("raise"):
        return f"protocol_raise_format: msg={message!r}"
    return f"protocol_action_format: msg={message!r}"


def parse_bot_log(path: str | Path, *, tail_lines: int = 30) -> BotLogStats:
    log_path = Path(path)
    stats = BotLogStats(path=str(log_path), exists=log_path.exists())
    if not log_path.exists():
        stats.issues.append("log_missing")
        return stats
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        stats.issues.append(f"log_read_error: {type(exc).__name__}: {exc}")
        return stats
    stats.bytes = len(text.encode("utf-8", errors="replace"))
    lines = text.splitlines()
    stats.tail = lines[-tail_lines:]

    previous_ts: int | None = None
    for line in lines:
        lower_line = line.lower()
        current_ts = _seconds_from_timestamp(line)
        stats.max_gap_sec = max(stats.max_gap_sec, _line_gap(previous_ts, current_ts))
        if current_ts is not None:
            previous_ts = current_ts
        if "DISPATCH line='preflop|" in line:
            stats.preflop += 1
        if "DISPATCH line='earnChips" in line:
            stats.earnchips += 1
            match = re.search(r"earnChips\s+(-?\d+)", line)
            if match:
                stats.net_chips += int(match.group(1))
        if line.startswith("[") and " SEND " in line and "name_handshake" not in line:
            stats.sends += 1
            send_match = SEND_MSG_RE.search(line)
            if send_match:
                issue = _sent_action_issue(send_match.group(1))
                if issue:
                    stats.issues.append(issue)
            else:
                stats.issues.append(f"send_message_missing_msg_field: {line[:300]}")
        for hand_match in re.finditer(r"\bhand=(\d+)\b", line):
            stats.max_hand = max(stats.max_hand, int(hand_match.group(1)))
        decision_match = re.search(r"DECIDE done .* elapsed=([0-9.]+)s", line)
        if decision_match:
            stats.max_decision_sec = max(stats.max_decision_sec, float(decision_match.group(1)))
        for pattern in CRITICAL_LOG_PATTERNS:
            if pattern.lower() in lower_line:
                stats.issues.append(line[:300])
                break
    return stats


def summarize_round_logs(log_a: Path, log_b: Path) -> dict[str, Any]:
    stats_a = parse_bot_log(log_a)
    stats_b = parse_bot_log(log_b)
    issues = stats_a.issues + stats_b.issues
    for label, stats in (("bot_a", stats_a), ("bot_b", stats_b)):
        if stats.max_gap_sec >= 55 and stats.max_decision_sec < 55:
            issues.append(
                f"official_log_silent_timeout_gap: {label} max_gap_sec={stats.max_gap_sec} "
                f"max_decision_sec={stats.max_decision_sec:.3f}"
            )
    return {
        "bot_a": _jsonable(stats_a),
        "bot_b": _jsonable(stats_b),
        "hands_started_min": min(stats_a.preflop, stats_b.preflop),
        "settlements_min": min(stats_a.earnchips, stats_b.earnchips),
        "issues": issues,
        "progress_key": (stats_a.progress_key(), stats_b.progress_key()),
    }


def check_environment(
    config: OfficialPlatformConfig | None = None,
    *,
    require_formal_sandbox: bool = False,
) -> dict[str, Any]:
    cfg = config or OfficialPlatformConfig()
    required = ("wine", "Xvfb", "xdotool")
    missing = [tool for tool in required if not shutil.which(tool)]
    optional_missing = [tool for tool in ("import", "ss") if not shutil.which(tool)]
    font_file = cfg.wineprefix / "drive_c" / "windows" / "Fonts" / "sourcehansans.ttc"
    issues = []
    if missing:
        issues.append(f"missing_tools: {', '.join(missing)}")
    if not cfg.exe_path.exists():
        issues.append(f"exe_missing: {cfg.exe_path}")
    if not cfg.wineprefix.exists():
        issues.append(f"wineprefix_missing: {cfg.wineprefix}")
    warnings = []
    if optional_missing:
        warnings.append(f"optional_tools_missing: {', '.join(optional_missing)}")
    if not font_file.exists():
        warnings.append("source_han_chinese_font_not_found_in_wineprefix")
    execution_profile = None
    if require_formal_sandbox:
        execution_profile = validate_execution_profile(
            cfg.exe_path,
            probe_sandbox=True,
        )
        issues.extend(execution_profile.get("issues") or [])
    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "config": _jsonable(cfg),
        "execution_profile": execution_profile,
    }


def _env_for_display(config: OfficialPlatformConfig, display: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(config.locale_env())
    env["DISPLAY"] = display
    env["POK_OFFICIAL_ACTION_DELAY"] = os.environ.get("POK_OFFICIAL_ACTION_DELAY", "0.30")
    return env


def _official_wire_probe_enabled() -> bool:
    """Return whether official rounds should capture raw TCP evidence."""
    return os.environ.get("POK_OFFICIAL_WIRE_PROBE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _choose_display() -> str:
    for _ in range(100):
        number = random.randint(40, 199)
        if not Path(f"/tmp/.X{number}-lock").exists():
            return f":{number}"
    raise RuntimeError("could not allocate an Xvfb display")


def _popen(
    cmd: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    stdout,
    stderr,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.Popen:
    managed_group = env.get("POK_OFFICIAL_JOB_PROCESS_GROUP") == "1"
    return subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=False,
        pass_fds=pass_fds,
        # Durable certification jobs own one outer process group so cancellation
        # can reap Wine, Xvfb and both bots together. Standalone/manual rounds
        # retain their per-child groups for the existing cleanup path.
        start_new_session=not managed_group,
    )


def _terminate_process(proc: subprocess.Popen | None, *, grace_sec: float = 3.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    managed_group = os.environ.get("POK_OFFICIAL_JOB_PROCESS_GROUP") == "1"
    if managed_group:
        proc.terminate()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            proc.terminate()
    try:
        proc.wait(timeout=grace_sec)
        return
    except subprocess.TimeoutExpired:
        pass
    if managed_group:
        proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            proc.kill()
    try:
        proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        pass


def _run_quiet(cmd: list[str], *, env: dict[str, str], timeout: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _wait_for_window(env: dict[str, str], *, timeout_sec: float) -> str:
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        try:
            proc = _run_quiet(
                ["xdotool", "search", "--onlyvisible", "--name", WINDOW_TITLE],
                env=env,
                timeout=3,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    window_id = line.strip()
                    if window_id:
                        return window_id
            last_error = (proc.stderr or proc.stdout).strip()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise TimeoutError(f"official platform window not found: {last_error[:200]}")


def _click(env: dict[str, str], window_id: str, x: int, y: int) -> None:
    _run_quiet(["xdotool", "windowactivate", window_id], env=env, timeout=3)
    _run_quiet(["xdotool", "mousemove", "--window", window_id, str(x), str(y), "click", "1"], env=env, timeout=3)


def _type_text(env: dict[str, str], window_id: str, text: str) -> None:
    _run_quiet(["xdotool", "windowactivate", window_id], env=env, timeout=3)
    _run_quiet(["xdotool", "key", "--window", window_id, "ctrl+a"], env=env, timeout=3)
    _run_quiet(["xdotool", "type", "--window", window_id, "--delay", "5", text], env=env, timeout=5)


def _screenshot(env: dict[str, str], output: Path) -> str | None:
    if not shutil.which("import"):
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    proc = _run_quiet(["import", "-window", "root", str(output)], env=env, timeout=10)
    return str(output) if proc.returncode == 0 and output.exists() else None


def _screenshot_policy() -> str:
    return os.environ.get("POK_OFFICIAL_SCREENSHOTS", "minimal").strip().lower()


def _maybe_screenshot(env: dict[str, str], output: Path, phase: str) -> str | None:
    policy = _screenshot_policy()
    if policy in {"0", "none", "off", "false"}:
        return None
    if policy == "all" or phase in {"start", "final"}:
        return _screenshot(env, output)
    return None


def _bot_handshake_seen(path: Path) -> bool:
    try:
        if not path.exists():
            return False
        return "SEND name_handshake" in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _wait_for_bot_handshakes(log_a: Path, log_b: Path, *, timeout_sec: float = 4.0) -> None:
    deadline = time.time() + max(0.0, timeout_sec)
    while time.time() < deadline:
        if _bot_handshake_seen(log_a) and _bot_handshake_seen(log_b):
            return
        time.sleep(0.2)


def _close_window(env: dict[str, str], window_id: str | None) -> None:
    if not window_id:
        return
    _run_quiet(["xdotool", "windowclose", window_id], env=env, timeout=3)


def _wait_for_wine_idle(env: dict[str, str], *, timeout_sec: float) -> None:
    if not shutil.which("wineserver"):
        return
    try:
        _run_quiet(["wineserver", "-w"], env=env, timeout=max(1.0, timeout_sec))
    except subprocess.TimeoutExpired:
        pass


def _kill_wineprefix(env: dict[str, str]) -> None:
    if not shutil.which("wineserver"):
        return
    _run_quiet(["wineserver", "-k"], env=env, timeout=5)


def _port_listening(host: str, port: int) -> bool:
    if not shutil.which("ss"):
        return False
    proc = subprocess.run(
        ["ss", "-ltn"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    needle = f":{port}"
    for line in proc.stdout.splitlines():
        if "LISTEN" in line and needle in line and (host in line or "0.0.0.0" in line or "*" in line):
            return True
    return False


def _wait_for_listen(config: OfficialPlatformConfig) -> None:
    deadline = time.time() + config.listen_timeout_sec
    while time.time() < deadline:
        if _port_listening(config.host, config.port):
            return
        time.sleep(0.5)
    raise TimeoutError(f"official platform did not listen on {config.host}:{config.port}")


def _port_busy_before_start(config: OfficialPlatformConfig) -> bool:
    return _port_listening(config.host, config.port)


def _wait_for_port_free(config: OfficialPlatformConfig, *, timeout_sec: float = 8.0) -> bool:
    deadline = time.time() + max(0.0, timeout_sec)
    while time.time() < deadline:
        if not _port_listening(config.host, config.port):
            return True
        time.sleep(0.25)
    return not _port_listening(config.host, config.port)


def _launch_bot(
    bot: BotLaunchConfig,
    *,
    config: OfficialPlatformConfig,
    env: dict[str, str],
    log_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    port: int | None = None,
) -> subprocess.Popen:
    entry = resolve_bot_entry(bot.path)
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    preconnected: socket.socket | None = None
    pass_fds: tuple[int, ...] = ()
    endpoint_port = config.port if port is None else int(port)
    try:
        if bot.sealed_artifact is not None:
            preconnected = connect_managed_endpoint(
                config.host,
                endpoint_port,
                timeout=min(10.0, config.listen_timeout_sec),
            )
            pass_fds = (preconnected.fileno(),)
            cmd, bot_env = build_sandboxed_bot_command(
                bot.sealed_artifact,
                host=config.host,
                port=endpoint_port,
                name=bot.name,
                seat=bot.seat if bot.supports_seat else None,
                log_path=log_path,
                supports_log=bot.supports_log,
                preconnected_fd=preconnected.fileno(),
                extra_args=bot.extra_args,
            )
            cwd = None
        else:
            cmd = build_bot_command(
                bot,
                host=config.host,
                port=endpoint_port,
                log_path=log_path,
            )
            cwd = entry.parent
            bot_env = env
        proc = _popen(
            cmd,
            cwd=cwd,
            env=bot_env,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
        )
    except BaseException:
        stdout.close()
        stderr.close()
        raise
    finally:
        if preconnected is not None:
            preconnected.close()
    proc._pok_stdout = stdout  # type: ignore[attr-defined]
    proc._pok_stderr = stderr  # type: ignore[attr-defined]
    return proc


def _close_process_files(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    for attr in ("_pok_stdout", "_pok_stderr"):
        handle = getattr(proc, attr, None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def _read_issue_file(path: Path, patterns: tuple[str, ...] = CRITICAL_LOG_PATTERNS) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    issues = []
    for line in text.splitlines():
        lower_line = line.lower()
        terminal_exception = re.match(
            r"^\s*[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\s*:",
            line,
        )
        if any(pattern.lower() in lower_line for pattern in patterns) or terminal_exception:
            issues.append(f"{path.name}: {line[:300]}")
    return issues


def _target_reached(summary: dict[str, Any], target_hands: int) -> bool:
    hands_started = int(summary.get("hands_started_min", 0) or 0)
    settlements = int(summary.get("settlements_min", 0) or 0)
    return hands_started >= target_hands and settlements >= target_hands


def _terminal_socket_boundary(
    log_summary: dict[str, Any],
    wire_summary: dict[str, Any],
    target_hands: int,
) -> bool:
    """Recognize the EXE's natural hand-70 TCP boundary, not a generic -1.

    The 2021 official EXE records hand 70 in THP but omits that hand's final
    ``earnChips`` pair.  This predicate is deliberately exact and is useful
    only while waiting for the independent official THP completion artifact.
    """
    if (
        target_hands != 70
        or not isinstance(wire_summary, dict)
        or not wire_summary
    ):
        return False
    try:
        log_hands = int(log_summary.get("hands_started_min", 0) or 0)
        log_settlements = int(log_summary.get("settlements_min", 0) or 0)
        wire_hands = int(wire_summary.get("hands_started_min", 0) or 0)
        wire_settlements = int(wire_summary.get("settlements_min", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if (log_hands, log_settlements, wire_hands, wire_settlements) != (
        70,
        69,
        70,
        69,
    ):
        return False
    if wire_summary.get("pending_expected_actions"):
        return False
    seats = wire_summary.get("seats")
    if not isinstance(seats, dict) or len(seats) != 2:
        return False
    records_by_label: dict[str, list[dict[str, int]]] = {}
    for label, seat in seats.items():
        if not isinstance(seat, dict):
            return False
        try:
            if int(seat.get("hands_started", 0) or 0) != 70:
                return False
            if int(seat.get("settlements", 0) or 0) != 69:
                return False
        except (TypeError, ValueError, OverflowError):
            return False
        if bool(seat.get("pending_expected_action")):
            return False
        records = seat.get("settlement_records")
        if not isinstance(records, list) or len(records) != 69:
            return False
        normalized: list[dict[str, int]] = []
        for item in records:
            if not isinstance(item, dict):
                return False
            hand = item.get("hand")
            amount = item.get("amount")
            if not isinstance(hand, int) or not isinstance(amount, int):
                return False
            normalized.append({"hand": hand, "amount": amount})
        if [item["hand"] for item in normalized] != list(range(1, 70)):
            return False
        records_by_label[str(label)] = normalized
    labels = sorted(records_by_label)
    if len(labels) != 2:
        return False
    for index in range(69):
        if sum(records_by_label[label][index]["amount"] for label in labels) != 0:
            return False
    return True


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")


class OfficialWireCapture:
    """Lifecycle wrapper for the official EXE TCP probe.

    The main EXE harness is synchronous because it drives Wine, Xvfb, and xdotool.
    The transparent TCP proxy is asyncio-based, so it runs on a private event loop
    thread for the duration of one official round.
    """

    def __init__(self, round_dir: Path, config: OfficialPlatformConfig):
        self.round_dir = Path(round_dir)
        self.config = config
        self.enabled = _official_wire_probe_enabled()
        self.recorder = None
        self.proxy = None
        self.proxy_ports: dict[str, int] = {}
        self.issues: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._startup_error = ""

    @property
    def wire_events_path(self) -> Path:
        return self.round_dir / "wire_events.jsonl"

    @property
    def replay_summary_path(self) -> Path:
        return self.round_dir / "replay_summary.json"

    def start(self) -> dict[str, int]:
        if not self.enabled:
            return {}
        try:
            from official_wire_probe import TcpWireProbe, WireEventRecorder

            self.recorder = WireEventRecorder(self.wire_events_path)
            self.proxy = TcpWireProbe(
                platform_host=self.config.host,
                platform_port=self.config.port,
                recorder=self.recorder,
            )
            self._ready.clear()
            self._stop_requested.clear()
            self._startup_error = ""
            self._thread = threading.Thread(
                target=self._run_loop,
                name="official-wire-probe",
                daemon=True,
            )
            self._thread.start()
            if not self._ready.wait(timeout=8.0):
                raise TimeoutError("wire probe event loop startup timed out")
            if self._startup_error:
                raise RuntimeError(self._startup_error)
            return self.proxy_ports
        except Exception as exc:
            self.issues.append(f"wire_probe_start_error: {type(exc).__name__}: {str(exc)[:300]}")
            self.stop()
            return {}

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def lifecycle() -> None:
            assert self.proxy is not None
            try:
                self.proxy_ports = dict(await self.proxy.start(self.config.host))
            except Exception as exc:
                self._startup_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                try:
                    await self.proxy.stop()
                except Exception:
                    pass
                return
            finally:
                self._ready.set()
            while not self._stop_requested.is_set():
                await asyncio.sleep(0.05)
            await self.proxy.stop()

        try:
            loop.run_until_complete(lifecycle())
        except Exception as exc:
            if not self._ready.is_set():
                self._startup_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            else:
                self.issues.append(
                    f"wire_probe_loop_error: {type(exc).__name__}: {str(exc)[:300]}"
                )
        finally:
            self._ready.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    def summary(self) -> dict[str, Any]:
        if not self.enabled or self.recorder is None:
            return {}
        try:
            from official_wire_probe import replay_events

            return replay_events(list(self.recorder.events))
        except Exception as exc:
            return {
                "events_seen": 0,
                "hands_started_min": 0,
                "settlements_min": 0,
                "issues": [{"kind": "wire_replay_error", "reason": f"{type(exc).__name__}: {exc}"}],
                "warnings": [],
            }

    def write_replay_summary(self) -> dict[str, Any]:
        summary = self.summary()
        if self.enabled:
            _write_json(self.replay_summary_path, summary)
        return summary

    def stop(self) -> None:
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                self.issues.append("wire_probe_stop_error: event loop thread did not stop")
        if self.recorder is not None:
            try:
                self.recorder.close()
            except Exception:
                pass
        self._loop = None
        self._thread = None
        self.proxy = None
        self.recorder = None


def _format_wire_issues(summary: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for issue in summary.get("issues") or []:
        if isinstance(issue, dict):
            kind = issue.get("kind") or "wire_issue"
            conn = issue.get("conn")
            hand = issue.get("hand")
            stage = issue.get("stage")
            message = issue.get("message")
            reason = issue.get("reason", "")
            issues.append(
                f"wire_{kind}: conn={conn} hand={hand} stage={stage} "
                f"msg={message!r} reason={reason}"
            )
        else:
            issues.append(f"wire_issue: {issue}")
    return issues


def _combined_target_reached(log_summary: dict[str, Any], wire_summary: dict[str, Any], target_hands: int) -> bool:
    if wire_summary:
        return _target_reached(log_summary, target_hands) and _target_reached(wire_summary, target_hands)
    return _target_reached(log_summary, target_hands)


def _combined_progress_key(log_summary: dict[str, Any], wire_summary: dict[str, Any]) -> tuple[Any, ...]:
    if wire_summary:
        return (
            log_summary.get("progress_key"),
            wire_summary.get("events_seen", 0),
            wire_summary.get("hands_started_min", 0),
            wire_summary.get("settlements_min", 0),
            len(wire_summary.get("issues") or []),
        )
    return (log_summary.get("progress_key"),)


def _copy_config(cfg: OfficialPlatformConfig, **overrides: Any) -> OfficialPlatformConfig:
    values = {
        "exe_path": cfg.exe_path,
        "wineprefix": cfg.wineprefix,
        "results_dir": cfg.results_dir,
        "host": cfg.host,
        "port": cfg.port,
        "startup_timeout_sec": cfg.startup_timeout_sec,
        "listen_timeout_sec": cfg.listen_timeout_sec,
        "no_progress_timeout_sec": cfg.no_progress_timeout_sec,
        "round_timeout_sec": cfg.round_timeout_sec,
        "lock_timeout_sec": cfg.lock_timeout_sec,
        "settlement_grace_sec": cfg.settlement_grace_sec,
        "artifact_grace_sec": cfg.artifact_grace_sec,
        "lock_path": cfg.lock_path,
        "ui": cfg.ui,
    }
    values.update(overrides)
    return OfficialPlatformConfig(**values)


@contextmanager
def _official_platform_lock(config: OfficialPlatformConfig):
    with acquire_official_platform(
        config.lock_path,
        owner="official-exe-suite",
        timeout=config.lock_timeout_sec,
    ):
        yield


def _platform_thp_dirs(exe_path: Path) -> list[Path]:
    dirs: list[Path] = []
    for path in (exe_path.parent, exe_path.parent.parent):
        resolved = path.resolve()
        if resolved not in dirs:
            dirs.append(resolved)
    return dirs


def _coerce_platform_dirs(platform_dirs: Path | list[Path] | tuple[Path, ...]) -> list[Path]:
    if isinstance(platform_dirs, Path):
        return [platform_dirs]
    return [Path(path) for path in platform_dirs]


def _snapshot_platform_thp_files(platform_dirs: Path | list[Path] | tuple[Path, ...]) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for platform_dir in _coerce_platform_dirs(platform_dirs):
        for path in platform_dir.glob("THP-*.txt"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path.resolve())] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not allocate unique artifact name for {path}")


def _collect_new_thp_files(
    platform_dirs: Path | list[Path] | tuple[Path, ...],
    *,
    before: dict[str, tuple[int, int]],
    artifact_dir: Path,
    wait_sec: float = 0.0,
    stable_sec: float = 0.5,
) -> tuple[list[str], list[str]]:
    artifacts: list[str] = []
    issues: list[str] = []
    deadline = time.time() + max(0.0, wait_sec)
    last_signature: tuple[tuple[str, int, int], ...] = ()
    stable_since: float | None = None

    while True:
        signature: list[tuple[str, int, int]] = []
        for platform_dir in _coerce_platform_dirs(platform_dirs):
            for path in sorted(platform_dir.glob("THP-*.txt")):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                    key = str(path.resolve())
                except OSError:
                    continue
                current = (stat.st_size, stat.st_mtime_ns)
                if before.get(key) != current:
                    signature.append((str(path), stat.st_size, stat.st_mtime_ns))
        current_signature = tuple(signature)
        if current_signature:
            if current_signature == last_signature:
                if stable_since is not None and time.time() - stable_since >= stable_sec:
                    break
            else:
                last_signature = current_signature
                stable_since = time.time()
        if time.time() >= deadline:
            break
        time.sleep(0.2)

    for path_name, _, _ in last_signature:
        path = Path(path_name)
        try:
            if not path.exists():
                continue
            artifact_dir.mkdir(parents=True, exist_ok=True)
            destination = _unique_destination(artifact_dir / path.name)
            shutil.move(str(path), str(destination))
            artifacts.append(str(destination))
        except OSError as exc:
            issues.append(f"thp_collect_error: {path.name}: {type(exc).__name__}: {exc}")
    return artifacts, issues


def _summarize_thp_files(paths: list[str]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for name in paths:
        path = Path(name)
        summary: dict[str, Any] = {"path": str(path), "exists": path.exists(), "hand_records": 0, "bytes": 0}
        if path.exists():
            try:
                raw = path.read_bytes()
                summary["bytes"] = len(raw)
                summary["sha256"] = hashlib.sha256(raw).hexdigest()
                text = raw.decode("gb2312", errors="replace")
                hand_indices = [int(value) for value in THP_HAND_RE.findall(text)]
                summary["hand_records"] = len(hand_indices)
                summary["hand_indices"] = hand_indices
            except OSError as exc:
                summary["issue"] = f"thp_read_error: {type(exc).__name__}: {exc}"
            except Exception as exc:
                summary["issue"] = f"thp_parse_error: {type(exc).__name__}: {exc}"
        summaries.append(summary)
    return summaries


def _canonical_thp_evidence(
    summaries: list[dict[str, Any]],
    *,
    expected_hands: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Select one content identity and require an exact official match length."""
    issues: list[str] = []
    readable = [
        item
        for item in summaries
        if isinstance(item, dict)
        and item.get("exists") is True
        and not item.get("issue")
    ]
    if not readable:
        return None, ["thp_missing_for_full_70_hand_round"]
    if any(not item.get("sha256") for item in readable):
        issues.append("thp_digest_missing")
    content_digests = {
        str(item.get("sha256"))
        for item in readable
        if item.get("sha256")
    }
    if len(content_digests) != 1:
        issues.append(
            "thp_ambiguous_multiple_outputs: "
            f"files={len(readable)} unique_contents={len(content_digests)}"
        )
        return None, issues
    selected = min(readable, key=lambda item: str(item.get("path") or ""))
    try:
        hand_records = int(selected.get("hand_records", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        hand_records = 0
    if hand_records != expected_hands:
        issues.append(
            "thp_hand_count_mismatch: "
            f"hands={hand_records} expected={expected_hands}"
        )
    expected_indices = list(range(expected_hands))
    if selected.get("hand_indices") != expected_indices:
        issues.append(
            "thp_hand_index_sequence_mismatch: "
            f"expected=0..{max(0, expected_hands - 1)}"
        )
    canonical = {
        "path": str(selected.get("path") or ""),
        "sha256": str(selected.get("sha256") or ""),
        "bytes": int(selected.get("bytes", 0) or 0),
        "hand_records": hand_records,
        "duplicate_paths": sorted(
            str(item.get("path") or "")
            for item in readable
            if item is not selected
        ),
    }
    return canonical, issues


def _changed_thp_paths(
    platform_dirs: Path | list[Path] | tuple[Path, ...],
    *,
    before: dict[str, tuple[int, int]],
) -> list[Path]:
    paths: dict[str, Path] = {}
    for platform_dir in _coerce_platform_dirs(platform_dirs):
        for path in platform_dir.glob("THP-*.txt"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                resolved = path.resolve()
                stat = path.stat()
            except OSError:
                continue
            if before.get(str(resolved)) != (stat.st_size, stat.st_mtime_ns):
                paths[str(resolved)] = path
    return [paths[key] for key in sorted(paths)]


def _strict_thp_match(
    text: str,
    *,
    expected_hands: int,
    expected_names: tuple[str, str],
) -> tuple[dict[str, Any] | None, list[str]]:
    issues: list[str] = []
    if len(set(expected_names)) != 2 or any(not name for name in expected_names):
        return None, ["thp_expected_players_invalid"]
    markers = [int(value) for value in THP_HAND_RE.findall(text)]
    if markers != list(range(expected_hands)):
        issues.append("thp_hand_index_sequence_mismatch")
    matches = list(THP_RECORD_RE.finditer(text))
    if len(matches) != expected_hands:
        issues.append(
            "thp_strict_record_count_mismatch: "
            f"records={len(matches)} expected={expected_hands}"
        )
    records: list[dict[str, Any]] = []
    totals = {name: 0 for name in expected_names}
    for match in matches:
        index = int(match.group(1))
        earnings = [int(match.group(4)), int(match.group(5))]
        players = [match.group(6), match.group(7)]
        if not match.group(2) or not match.group(3):
            issues.append(f"thp_record_payload_empty:{index}")
        if sum(earnings) != 0:
            issues.append(f"thp_record_earnings_not_zero_sum:{index}")
        if set(players) != set(expected_names) or len(set(players)) != 2:
            issues.append(f"thp_record_players_mismatch:{index}")
            continue
        earnings_by_player = {
            players[0]: earnings[0],
            players[1]: earnings[1],
        }
        for name, amount in earnings_by_player.items():
            totals[name] += amount
        records.append({
            "index": index,
            "actions": match.group(2),
            "cards": match.group(3),
            "earnings": earnings,
            "players": players,
            "earnings_by_player": earnings_by_player,
        })
    if [item["index"] for item in records] != list(range(expected_hands)):
        issues.append("thp_strict_record_index_sequence_mismatch")
    footers = list(THP_FOOTER_RE.finditer(text))
    if len(footers) != 1:
        issues.append(f"thp_footer_count_mismatch:{len(footers)}")
        footer = None
    else:
        footer = footers[0]
    footer_result = ""
    if footer is not None:
        footer_players = [footer.group(1), footer.group(2)]
        footer_result = footer.group(3)
        if set(footer_players) != set(expected_names) or len(set(footer_players)) != 2:
            issues.append("thp_footer_players_mismatch")
        values = list(totals.values())
        if sum(values) != 0:
            issues.append("thp_match_totals_not_zero_sum")
        elif all(value == 0 for value in values):
            if footer_result != "平局":
                issues.append("thp_footer_draw_result_mismatch")
        else:
            winner = max(totals, key=totals.get)
            amount = totals[winner]
            expected_result = f"{winner}赢得{amount}个筹码"
            if amount <= 0 or footer_result != expected_result:
                issues.append(
                    "thp_footer_result_mismatch: "
                    f"result={footer_result!r} expected={expected_result!r}"
                )
    if issues:
        return None, list(dict.fromkeys(issues))
    return {
        "records": records,
        "match_totals": totals,
        "footer_result": footer_result,
        "footer_timestamp": footer.group(4) if footer is not None else "",
        "footer_event": footer.group(5) if footer is not None else "",
    }, []


def _wire_settlement_prefix(
    wire_summary: dict[str, Any],
    *,
    expected_hands: int,
    expected_names: tuple[str, str],
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    seats = wire_summary.get("seats") if isinstance(wire_summary, dict) else None
    if not isinstance(seats, dict) or len(seats) != 2:
        return None, ["wire_settlement_seats_invalid"]
    by_name: dict[str, list[dict[str, int]]] = {}
    for seat in seats.values():
        if not isinstance(seat, dict):
            return None, ["wire_settlement_seat_invalid"]
        name = str(seat.get("name") or "")
        records = seat.get("settlement_records")
        if name in by_name or name not in expected_names or not isinstance(records, list):
            return None, ["wire_settlement_player_identity_invalid"]
        normalized: list[dict[str, int]] = []
        for item in records:
            if not isinstance(item, dict):
                return None, ["wire_settlement_record_invalid"]
            hand = item.get("hand")
            amount = item.get("amount")
            if not isinstance(hand, int) or not isinstance(amount, int):
                return None, ["wire_settlement_record_invalid"]
            normalized.append({"hand": hand, "amount": amount})
        if [item["hand"] for item in normalized] != list(range(1, expected_hands)):
            return None, ["wire_settlement_hand_sequence_invalid"]
        by_name[name] = normalized
    if set(by_name) != set(expected_names):
        return None, ["wire_settlement_player_set_mismatch"]
    prefix: list[dict[str, Any]] = []
    for hand in range(1, expected_hands):
        earnings = {
            name: by_name[name][hand - 1]["amount"]
            for name in expected_names
        }
        if sum(earnings.values()) != 0:
            return None, [f"wire_settlement_not_zero_sum:{hand}"]
        prefix.append({"hand": hand, "earnings_by_player": earnings})
    return prefix, []


def _terminal_thp_observation(
    platform_dirs: Path | list[Path] | tuple[Path, ...],
    *,
    before: dict[str, tuple[int, int]],
    expected_hands: int,
    expected_names: tuple[str, str],
    wire_summary: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Read, but do not move, a new exact official THP terminal artifact."""
    paths = _changed_thp_paths(platform_dirs, before=before)
    summaries = _summarize_thp_files([str(path) for path in paths])
    canonical, issues = _canonical_thp_evidence(
        summaries,
        expected_hands=expected_hands,
    )
    if canonical is None or issues:
        return None, issues
    path = Path(str(canonical.get("path") or ""))
    try:
        raw = path.read_bytes()
        text = raw.decode("gb2312", errors="replace")
    except OSError as exc:
        return None, [f"terminal_thp_read_error:{type(exc).__name__}"]
    strict_match, strict_issues = _strict_thp_match(
        text,
        expected_hands=expected_hands,
        expected_names=expected_names,
    )
    if strict_match is None or strict_issues:
        return None, strict_issues
    wire_prefix, wire_issues = _wire_settlement_prefix(
        wire_summary,
        expected_hands=expected_hands,
        expected_names=expected_names,
    )
    if wire_prefix is None or wire_issues:
        return None, wire_issues
    thp_prefix = [
        {
            "hand": record["index"] + 1,
            "earnings_by_player": {
                name: record["earnings_by_player"][name]
                for name in expected_names
            },
        }
        for record in strict_match["records"][:-1]
    ]
    if wire_prefix != thp_prefix:
        return None, ["terminal_thp_wire_prefix_earnings_mismatch"]
    final_hand = strict_match["records"][-1]
    payload = {
        "schema_version": TERMINAL_COMPLETION_SCHEMA_VERSION,
        "kind": "official-thp-terminal-hand-observation",
        "target_hands": expected_hands,
        "thp_sha256": str(canonical.get("sha256") or ""),
        "thp_bytes": int(canonical.get("bytes", 0) or 0),
        "hand_records": int(canonical.get("hand_records", 0) or 0),
        "hand_index_digest": canonical_digest(list(range(expected_hands))),
        "wire_prefix_digest": canonical_digest(wire_prefix),
        "thp_prefix_digest": canonical_digest(thp_prefix),
        "final_hand": final_hand,
        "match_totals": strict_match["match_totals"],
        "footer_result": strict_match["footer_result"],
    }
    return {**payload, "observation_digest": canonical_digest(payload)}, []


def _build_terminal_completion_evidence(
    receipt: dict[str, Any],
    observation: dict[str, Any],
    canonical_thp: dict[str, Any],
    *,
    target_hands: int,
) -> dict[str, Any]:
    log_summary = receipt.get("log_summary") or {}
    wire_summary = receipt.get("wire_replay_summary") or {}
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    try:
        wire_events_sha256 = hashlib.sha256(
            Path(str(artifacts.get("wire_events") or "")).read_bytes()
        ).hexdigest()
    except OSError:
        wire_events_sha256 = ""
    payload = {
        "schema_version": TERMINAL_COMPLETION_SCHEMA_VERSION,
        "kind": "official-thp-terminal-settlement",
        "target_hands": target_hands,
        "completed_hands": target_hands,
        "wire_settled_hands": target_hands - 1,
        "log_hands_started": int(log_summary.get("hands_started_min", 0) or 0),
        "log_tcp_settlements": int(log_summary.get("settlements_min", 0) or 0),
        "wire_hands_started": int(wire_summary.get("hands_started_min", 0) or 0),
        "wire_tcp_settlements": int(wire_summary.get("settlements_min", 0) or 0),
        "canonical_thp_sha256": str(canonical_thp.get("sha256") or ""),
        "canonical_thp_bytes": int(canonical_thp.get("bytes", 0) or 0),
        "canonical_thp_hand_records": int(canonical_thp.get("hand_records", 0) or 0),
        "wire_events_sha256": wire_events_sha256,
        "hand_index_digest": str(observation.get("hand_index_digest") or ""),
        "wire_prefix_digest": str(observation.get("wire_prefix_digest") or ""),
        "thp_prefix_digest": str(observation.get("thp_prefix_digest") or ""),
        "final_hand": observation.get("final_hand"),
        "match_totals": observation.get("match_totals"),
        "footer_result": str(observation.get("footer_result") or ""),
        "terminal_observation_digest": str(observation.get("observation_digest") or ""),
        "strength_evaluation": "not_applicable",
    }
    return {**payload, "evidence_digest": canonical_digest(payload)}


def round_completion_issues(
    receipt: dict[str, Any],
    target_hands: int,
) -> list[str]:
    """Validate complete-round evidence, including the EXE hand-70 THP rule."""
    log_summary = receipt.get("log_summary") if isinstance(receipt, dict) else None
    if not isinstance(log_summary, dict):
        return ["official_round_log_summary_missing"]
    if _target_reached(log_summary, target_hands):
        return []
    if target_hands != 70:
        return [
            "official_round_completion_incomplete: "
            f"hands_started={log_summary.get('hands_started_min', 0)} "
            f"settlements={log_summary.get('settlements_min', 0)} "
            f"target={target_hands}"
        ]
    wire_summary = receipt.get("wire_replay_summary")
    if not isinstance(wire_summary, dict) or not _terminal_socket_boundary(
        log_summary,
        wire_summary,
        target_hands,
    ):
        return ["official_terminal_socket_boundary_invalid"]
    evidence = receipt.get("completion_evidence")
    if not isinstance(evidence, dict):
        return ["official_terminal_completion_evidence_missing"]
    issues: list[str] = []
    payload = {key: value for key, value in evidence.items() if key != "evidence_digest"}
    if evidence.get("evidence_digest") != canonical_digest(payload):
        issues.append("official_terminal_completion_evidence_digest_mismatch")
    expected_scalars = {
        "schema_version": TERMINAL_COMPLETION_SCHEMA_VERSION,
        "kind": "official-thp-terminal-settlement",
        "target_hands": 70,
        "completed_hands": 70,
        "wire_settled_hands": 69,
        "log_hands_started": 70,
        "log_tcp_settlements": 69,
        "wire_hands_started": 70,
        "wire_tcp_settlements": 69,
        "canonical_thp_hand_records": 70,
        "hand_index_digest": canonical_digest(list(range(70))),
        "strength_evaluation": "not_applicable",
    }
    for key, value in expected_scalars.items():
        if evidence.get(key) != value:
            issues.append(f"official_terminal_completion_{key}_mismatch")
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    canonical = artifacts.get("canonical_thp") if isinstance(artifacts.get("canonical_thp"), dict) else {}
    if evidence.get("canonical_thp_sha256") != canonical.get("sha256"):
        issues.append("official_terminal_completion_thp_sha256_mismatch")
    if evidence.get("canonical_thp_bytes") != canonical.get("bytes"):
        issues.append("official_terminal_completion_thp_bytes_mismatch")
    wire_events_path = artifacts.get("wire_events")
    try:
        actual_wire_sha256 = hashlib.sha256(Path(str(wire_events_path)).read_bytes()).hexdigest()
    except OSError:
        actual_wire_sha256 = ""
    if len(actual_wire_sha256) != 64 or evidence.get("wire_events_sha256") != actual_wire_sha256:
        issues.append("official_terminal_completion_wire_sha256_mismatch")
    if (
        len(str(evidence.get("wire_prefix_digest") or "")) != 64
        or evidence.get("wire_prefix_digest") != evidence.get("thp_prefix_digest")
    ):
        issues.append("official_terminal_completion_prefix_digest_mismatch")
    final_hand = evidence.get("final_hand") if isinstance(evidence.get("final_hand"), dict) else {}
    if final_hand.get("index") != 69 or not final_hand.get("actions") or not final_hand.get("cards"):
        issues.append("official_terminal_completion_final_hand_invalid")
    earnings = final_hand.get("earnings")
    if (
        not isinstance(earnings, list)
        or len(earnings) != 2
        or any(not isinstance(value, int) for value in earnings)
        or sum(earnings) != 0
    ):
        issues.append("official_terminal_completion_final_earnings_invalid")
    bot_a = receipt.get("bot_a") if isinstance(receipt.get("bot_a"), dict) else {}
    bot_b = receipt.get("bot_b") if isinstance(receipt.get("bot_b"), dict) else {}
    expected_name_order = (
        str(bot_a.get("name") or ""),
        str(bot_b.get("name") or ""),
    )
    expected_names = set(expected_name_order)
    players = final_hand.get("players")
    if (
        len(expected_names) != 2
        or not isinstance(players, list)
        or len(players) != 2
        or set(players) != expected_names
    ):
        issues.append("official_terminal_completion_final_players_invalid")
    try:
        canonical_path = Path(str(canonical.get("path") or ""))
        raw = canonical_path.read_bytes()
        canonical_text = raw.decode("gb2312", errors="replace")
        actual_indices = [int(value) for value in THP_HAND_RE.findall(canonical_text)]
        actual_matches = [
            match
            for match in THP_RECORD_RE.finditer(canonical_text)
            if int(match.group(1)) == 69
        ]
        if hashlib.sha256(raw).hexdigest() != canonical.get("sha256"):
            issues.append("official_terminal_completion_thp_artifact_digest_mismatch")
        if actual_indices != list(range(70)):
            issues.append("official_terminal_completion_thp_indices_invalid")
        if len(actual_matches) != 1:
            issues.append("official_terminal_completion_thp_final_record_missing")
        else:
            actual_match = actual_matches[0]
            actual_final_hand = {
                "index": 69,
                "actions": actual_match.group(2),
                "cards": actual_match.group(3),
                "earnings": [int(actual_match.group(4)), int(actual_match.group(5))],
                "players": [actual_match.group(6), actual_match.group(7)],
                "earnings_by_player": {
                    actual_match.group(6): int(actual_match.group(4)),
                    actual_match.group(7): int(actual_match.group(5)),
                },
            }
            if final_hand != actual_final_hand:
                issues.append("official_terminal_completion_final_hand_thp_mismatch")
        strict_match, strict_issues = _strict_thp_match(
            canonical_text,
            expected_hands=70,
            expected_names=expected_name_order,
        )
        if strict_match is None or strict_issues:
            issues.extend(
                f"official_terminal_completion_{issue}"
                for issue in strict_issues
            )
        else:
            wire_prefix, prefix_issues = _wire_settlement_prefix(
                wire_summary,
                expected_hands=70,
                expected_names=expected_name_order,
            )
            if wire_prefix is None or prefix_issues:
                issues.extend(
                    f"official_terminal_completion_{issue}"
                    for issue in prefix_issues
                )
            else:
                thp_prefix = [
                    {
                        "hand": record["index"] + 1,
                        "earnings_by_player": {
                            name: record["earnings_by_player"][name]
                            for name in expected_name_order
                        },
                    }
                    for record in strict_match["records"][:-1]
                ]
                if wire_prefix != thp_prefix:
                    issues.append("official_terminal_completion_wire_thp_prefix_mismatch")
                if evidence.get("wire_prefix_digest") != canonical_digest(wire_prefix):
                    issues.append("official_terminal_completion_wire_prefix_digest_mismatch")
                if evidence.get("thp_prefix_digest") != canonical_digest(thp_prefix):
                    issues.append("official_terminal_completion_thp_prefix_digest_mismatch")
            if evidence.get("match_totals") != strict_match["match_totals"]:
                issues.append("official_terminal_completion_match_totals_mismatch")
            if evidence.get("footer_result") != strict_match["footer_result"]:
                issues.append("official_terminal_completion_footer_result_mismatch")
    except (OSError, ValueError, TypeError) as exc:
        issues.append(
            "official_terminal_completion_thp_read_error:"
            f"{type(exc).__name__}"
        )
    observation_payload = {
        "schema_version": TERMINAL_COMPLETION_SCHEMA_VERSION,
        "kind": "official-thp-terminal-hand-observation",
        "target_hands": 70,
        "thp_sha256": evidence.get("canonical_thp_sha256"),
        "thp_bytes": evidence.get("canonical_thp_bytes"),
        "hand_records": evidence.get("canonical_thp_hand_records"),
        "hand_index_digest": evidence.get("hand_index_digest"),
        "wire_prefix_digest": evidence.get("wire_prefix_digest"),
        "thp_prefix_digest": evidence.get("thp_prefix_digest"),
        "final_hand": final_hand,
        "match_totals": evidence.get("match_totals"),
        "footer_result": evidence.get("footer_result"),
    }
    if evidence.get("terminal_observation_digest") != canonical_digest(observation_payload):
        issues.append("official_terminal_completion_observation_digest_mismatch")
    return list(dict.fromkeys(issues))


def run_official_round(
    bot_a: BotLaunchConfig,
    bot_b: BotLaunchConfig,
    *,
    target_hands: int = 70,
    round_kind: str = "self_play",
    round_index: int = 1,
    config: OfficialPlatformConfig | None = None,
    out_dir: Path | None = None,
    job_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one official-platform round and return an evidence receipt."""
    cfg = config or OfficialPlatformConfig()
    target_hands = max(1, min(70, int(target_hands)))
    formal_sandbox = bot_a.sealed_artifact is not None or bot_b.sealed_artifact is not None
    environment = check_environment(
        cfg,
        require_formal_sandbox=formal_sandbox,
    )
    started_at = time.time()
    round_id = f"{round_kind}_{round_index:02d}_{_now_id()}"
    round_dir = (out_dir or (cfg.results_dir / round_id)).resolve()
    round_dir.mkdir(parents=True, exist_ok=True)

    receipt: dict[str, Any] = {
        "round_id": round_id,
        "round_kind": round_kind,
        "round_index": round_index,
        "target_hands": target_hands,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "config": _jsonable(cfg),
        "bot_a": _jsonable(bot_a),
        "bot_b": _jsonable(bot_b),
        "environment": environment,
        "artifacts": {},
        "issues": [],
        "passed": False,
        "job_envelope": job_envelope,
        "formal_execution": {
            "sandboxed": formal_sandbox,
            **execution_profile_identity(),
            "bot_a_artifact_hash": (
                bot_a.sealed_artifact.artifact_hash
                if bot_a.sealed_artifact is not None
                else None
            ),
            "bot_b_artifact_hash": (
                bot_b.sealed_artifact.artifact_hash
                if bot_b.sealed_artifact is not None
                else None
            ),
        },
    }
    if job_envelope is not None:
        from official_job_envelope import job_envelope_issues

        receipt["issues"].extend(job_envelope_issues(job_envelope))
    receipt["topology"] = round_topology(receipt)
    if not environment["ok"]:
        receipt["issues"].extend(environment["issues"])
        _write_json(round_dir / "receipt.json", receipt)
        return receipt
    cleanup_env = os.environ.copy()
    cleanup_env.update(cfg.locale_env())
    # The official EXE is a single-instance, timing-sensitive Windows program.
    # Start every round from a clean Wine prefix so previous windows, wineserver
    # children, or stale listeners cannot leak into the next certification round.
    _kill_wineprefix(cleanup_env)
    _wait_for_wine_idle(cleanup_env, timeout_sec=3.0)
    _wait_for_port_free(cfg, timeout_sec=3.0)
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
    platform_log = round_dir / "platform.wine.log"
    log_a = round_dir / "botA.log"
    log_b = round_dir / "botB.log"
    bot_a_stdout = round_dir / "botA.stdout.log"
    bot_a_stderr = round_dir / "botA.stderr.log"
    bot_b_stdout = round_dir / "botB.stdout.log"
    bot_b_stderr = round_dir / "botB.stderr.log"
    screenshots: list[str] = []
    platform_thp_dirs = _platform_thp_dirs(cfg.exe_path)
    thp_snapshot = _snapshot_platform_thp_files(platform_thp_dirs)
    thp_artifacts: list[str] = []
    artifact_issues: list[str] = []
    wire_capture = OfficialWireCapture(round_dir, cfg)
    wire_summary: dict[str, Any] = {}
    terminal_thp_observation: dict[str, Any] | None = None
    terminal_thp_signature = ""
    terminal_thp_stable_since: float | None = None
    terminal_boundary_at: float | None = None
    terminal_probe_issues: list[str] = []

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
        time.sleep(0.8)
        env = _env_for_display(cfg, display)
        platform_env = env
        with platform_log.open("wb") as platform_out:
            wine_proc = _popen(["wine", str(cfg.exe_path)], cwd=cfg.exe_path.parent, env=env, stdout=platform_out, stderr=platform_out)
            window_id = _wait_for_window(env, timeout_sec=cfg.startup_timeout_sec)
            receipt["window_id"] = window_id
            first = _maybe_screenshot(env, round_dir / "screenshots" / "01_start.png", "start")
            if first:
                screenshots.append(first)

            _click(env, window_id, cfg.ui.gear_x, cfg.ui.gear_y)
            time.sleep(0.8)
            _click(env, window_id, cfg.ui.ip_x, cfg.ui.ip_y)
            _type_text(env, window_id, cfg.host)
            second = _maybe_screenshot(env, round_dir / "screenshots" / "02_config.png", "config")
            if second:
                screenshots.append(second)
            _click(env, window_id, cfg.ui.start_x, cfg.ui.start_y)
            _wait_for_listen(cfg)
            proxy_ports = wire_capture.start()
            receipt["wire_probe"] = {
                "enabled": wire_capture.enabled,
                "proxy_ports": proxy_ports,
                "issues": list(wire_capture.issues),
            }
            if wire_capture.issues:
                receipt["issues"].extend(wire_capture.issues)
                raise RuntimeError("; ".join(wire_capture.issues[:3]))

            bot_a_proc = _launch_bot(
                bot_a,
                config=cfg,
                env=env,
                log_path=log_a,
                stdout_path=bot_a_stdout,
                stderr_path=bot_a_stderr,
                port=proxy_ports.get("A") if proxy_ports else None,
            )
            bot_b_proc = _launch_bot(
                bot_b,
                config=cfg,
                env=env,
                log_path=log_b,
                stdout_path=bot_b_stdout,
                stderr_path=bot_b_stderr,
                port=proxy_ports.get("B") if proxy_ports else None,
            )
            _wait_for_bot_handshakes(log_a, log_b, timeout_sec=4.0)
            third = _maybe_screenshot(env, round_dir / "screenshots" / "03_connected.png", "connected")
            if third:
                screenshots.append(third)
            _click(env, window_id, cfg.ui.ok_x, cfg.ui.ok_y)

            last_progress_at = time.time()
            target_reached_at: float | None = None
            last_key = None
            summary: dict[str, Any] = {}
            deadline = started_at + cfg.round_timeout_sec
            while time.time() < deadline:
                summary = summarize_round_logs(log_a, log_b)
                wire_summary = wire_capture.summary()
                key = _combined_progress_key(summary, wire_summary)
                if key != last_key:
                    last_key = key
                    last_progress_at = time.time()
                if summary["issues"]:
                    receipt["issues"].extend(summary["issues"])
                    break
                wire_issues = _format_wire_issues(wire_summary)
                if wire_issues:
                    receipt["issues"].extend(wire_issues)
                    break
                terminal_boundary = _terminal_socket_boundary(
                    summary,
                    wire_summary,
                    target_hands,
                )
                if not _target_reached(summary, target_hands) and not terminal_boundary:
                    # Observe all processes in one poll and attribute the EXE
                    # first. A normal bot exit after the platform closes its
                    # sockets must not be rewritten as a candidate crash.
                    platform_rc = wine_proc.poll()
                    bot_a_rc = bot_a_proc.poll()
                    bot_b_rc = bot_b_proc.poll()
                    if platform_rc is not None:
                        receipt["observed_exit"] = {
                            "subject_domain": "platform",
                            "subject_instance_id": "official_exe",
                            "returncode": platform_rc,
                            "observed_at": datetime.now().isoformat(timespec="milliseconds"),
                            "bot_a_returncode": bot_a_rc,
                            "bot_b_returncode": bot_b_rc,
                        }
                        receipt["issues"].append(f"platform_exited_early: rc={platform_rc}")
                        break
                    if bot_a_rc is not None:
                        subject = receipt["topology"]["connections"]["A"]
                        receipt["observed_exit"] = {
                            "subject_domain": subject["role"],
                            "subject_instance_id": subject["instance_id"],
                            "connection": "A",
                            "returncode": bot_a_rc,
                            "observed_at": datetime.now().isoformat(timespec="milliseconds"),
                        }
                        receipt["issues"].append(f"{bot_a.name}_exited_early: rc={bot_a_rc}")
                        break
                    if bot_b_rc is not None:
                        subject = receipt["topology"]["connections"]["B"]
                        receipt["observed_exit"] = {
                            "subject_domain": subject["role"],
                            "subject_instance_id": subject["instance_id"],
                            "connection": "B",
                            "returncode": bot_b_rc,
                            "observed_at": datetime.now().isoformat(timespec="milliseconds"),
                        }
                        receipt["issues"].append(f"{bot_b.name}_exited_early: rc={bot_b_rc}")
                        break
                if _combined_target_reached(summary, wire_summary, target_hands):
                    if target_reached_at is None:
                        target_reached_at = time.time()
                    if time.time() - target_reached_at >= cfg.settlement_grace_sec:
                        break
                elif terminal_boundary:
                    now = time.time()
                    if terminal_boundary_at is None:
                        terminal_boundary_at = now
                    observation, probe_issues = _terminal_thp_observation(
                        platform_thp_dirs,
                        before=thp_snapshot,
                        expected_hands=target_hands,
                        expected_names=(bot_a.name, bot_b.name),
                        wire_summary=wire_summary,
                    )
                    terminal_probe_issues = probe_issues
                    if observation is not None:
                        signature = str(observation.get("observation_digest") or "")
                        if signature != terminal_thp_signature:
                            terminal_thp_signature = signature
                            terminal_thp_stable_since = now
                        elif (
                            terminal_thp_stable_since is not None
                            and now - terminal_thp_stable_since >= 0.5
                        ):
                            terminal_thp_observation = observation
                            break
                    if now - terminal_boundary_at > cfg.artifact_grace_sec:
                        detail = "; ".join(terminal_probe_issues[:3]) or "no exact THP appeared"
                        receipt["issues"].append(
                            "terminal_thp_timeout: "
                            f"waited={cfg.artifact_grace_sec:g}s detail={detail}"
                        )
                        break
                elif time.time() - last_progress_at > cfg.no_progress_timeout_sec:
                    receipt["issues"].append(
                        f"no_progress_timeout: {cfg.no_progress_timeout_sec:g}s "
                        f"hands_started={summary.get('hands_started_min', 0)} "
                        f"settlements={summary.get('settlements_min', 0)} "
                        f"wire_hands_started={wire_summary.get('hands_started_min', 0) if wire_summary else 0} "
                        f"wire_settlements={wire_summary.get('settlements_min', 0) if wire_summary else 0}"
                    )
                    break
                time.sleep(1.0)
            else:
                summary = summarize_round_logs(log_a, log_b)
                wire_summary = wire_capture.summary()
                receipt["issues"].append(
                    f"round_timeout: {cfg.round_timeout_sec:g}s "
                    f"hands_started={summary.get('hands_started_min', 0)} "
                    f"settlements={summary.get('settlements_min', 0)} "
                    f"wire_hands_started={wire_summary.get('hands_started_min', 0) if wire_summary else 0} "
                    f"wire_settlements={wire_summary.get('settlements_min', 0) if wire_summary else 0}"
                )

            final = _maybe_screenshot(env, round_dir / "screenshots" / "04_final.png", "final")
            if final:
                screenshots.append(final)
            receipt["log_summary"] = summary or summarize_round_logs(log_a, log_b)
            wire_summary = wire_capture.write_replay_summary()
            if wire_summary:
                receipt["wire_replay_summary"] = wire_summary
    except Exception as exc:
        receipt["issues"].append(f"official_round_exception: {type(exc).__name__}: {str(exc)[:500]}")
        if "log_summary" not in receipt:
            receipt["log_summary"] = summarize_round_logs(log_a, log_b)
    finally:
        platform_closed_for_terminal = False
        if terminal_thp_observation is not None and platform_env is not None:
            _close_window(platform_env, window_id)
            platform_closed_for_terminal = True
            time.sleep(2.0)
        for proc in (bot_a_proc, bot_b_proc):
            _terminate_process(proc)
            _close_process_files(proc)
        if wire_capture.enabled:
            wire_summary = wire_capture.write_replay_summary()
            if wire_summary:
                receipt["wire_replay_summary"] = wire_summary
        wire_capture.stop()
        if platform_env is not None and not platform_closed_for_terminal:
            _close_window(platform_env, window_id)
            time.sleep(2.0)
        _terminate_process(wine_proc)
        if platform_env is not None:
            _wait_for_wine_idle(platform_env, timeout_sec=cfg.artifact_grace_sec)
            _kill_wineprefix(platform_env)
            _wait_for_wine_idle(platform_env, timeout_sec=3.0)
            if not _wait_for_port_free(cfg, timeout_sec=5.0):
                receipt["issues"].append(f"port_busy_after_cleanup: {cfg.host}:{cfg.port}")
        _terminate_process(xvfb_proc)
        for proc in (wine_proc, xvfb_proc):
            _close_process_files(proc)
    receipt["log_summary"] = summarize_round_logs(log_a, log_b)
    if wire_capture.enabled and not wire_summary:
        try:
            wire_summary = json.loads(wire_capture.replay_summary_path.read_text(encoding="utf-8"))
        except Exception:
            wire_summary = {}
    if wire_summary:
        receipt["wire_replay_summary"] = wire_summary
    artifact_wait_sec = (
        cfg.artifact_grace_sec
        if _target_reached(receipt.get("log_summary", {}), target_hands)
        or terminal_thp_observation is not None
        else 1.0
    )
    thp_artifacts, artifact_issues = _collect_new_thp_files(
        platform_thp_dirs,
        before=thp_snapshot,
        artifact_dir=round_dir / "thp",
        wait_sec=artifact_wait_sec,
    )
    thp_summaries = _summarize_thp_files(thp_artifacts)
    canonical_thp: dict[str, Any] | None = None
    if target_hands >= 70:
        canonical_thp, thp_issues = _canonical_thp_evidence(
            thp_summaries,
            expected_hands=target_hands,
        )
        receipt["issues"].extend(thp_issues)

    receipt["duration_sec"] = round(time.time() - started_at, 2)
    receipt["bot_returncodes"] = {
        bot_a.name: None if bot_a_proc is None else bot_a_proc.poll(),
        bot_b.name: None if bot_b_proc is None else bot_b_proc.poll(),
        "platform": None if wine_proc is None else wine_proc.poll(),
    }
    receipt["artifacts"] = {
        "round_dir": str(round_dir),
        "receipt": str(round_dir / "receipt.json"),
        "platform_log": str(platform_log),
        "bot_a_log": str(log_a),
        "bot_b_log": str(log_b),
        "bot_a_stdout": str(bot_a_stdout),
        "bot_a_stderr": str(bot_a_stderr),
        "bot_b_stdout": str(bot_b_stdout),
        "bot_b_stderr": str(bot_b_stderr),
        "screenshots": screenshots,
        "platform_thp_dirs": [str(path) for path in platform_thp_dirs],
        "thp_files": thp_artifacts,
        "thp_summaries": thp_summaries,
        "canonical_thp": canonical_thp,
    }
    if wire_capture.enabled:
        receipt["artifacts"]["wire_events"] = str(wire_capture.wire_events_path)
        receipt["artifacts"]["replay_summary"] = str(wire_capture.replay_summary_path)
    if terminal_thp_observation is not None and _terminal_socket_boundary(
        receipt.get("log_summary") or {},
        receipt.get("wire_replay_summary") or {},
        target_hands,
    ):
        if canonical_thp is None:
            receipt["issues"].append("terminal_thp_observation_without_canonical_artifact")
        elif canonical_thp.get("sha256") != terminal_thp_observation.get("thp_sha256"):
            receipt["issues"].append("terminal_thp_observation_artifact_digest_mismatch")
        else:
            receipt["completion_evidence"] = _build_terminal_completion_evidence(
                receipt,
                terminal_thp_observation,
                canonical_thp,
                target_hands=target_hands,
            )
    receipt["issues"].extend((receipt.get("log_summary") or {}).get("issues") or [])
    if wire_summary:
        receipt["issues"].extend(_format_wire_issues(wire_summary))
    if wire_capture.enabled and not wire_summary.get("events_seen"):
        receipt["issues"].append("wire_probe_no_events")
    receipt["issues"].extend(wire_capture.issues)
    receipt["issues"].extend(artifact_issues)
    receipt["issues"].extend(_read_issue_file(bot_a_stdout))
    receipt["issues"].extend(_read_issue_file(bot_a_stderr))
    receipt["issues"].extend(_read_issue_file(bot_b_stdout))
    receipt["issues"].extend(_read_issue_file(bot_b_stderr))
    receipt["issues"].extend(round_completion_issues(receipt, target_hands))
    receipt["issues"] = list(dict.fromkeys(str(issue) for issue in receipt["issues"]))
    receipt["passed"] = not receipt["issues"]
    _write_json(round_dir / "receipt.json", receipt)
    return receipt


RoundRunner = Callable[..., dict[str, Any]]
_PRODUCTION_ROUND_RUNNER = run_official_round


def _same_launch_path(actual: Any, expected: Path) -> bool:
    try:
        return Path(str(actual)).expanduser().resolve() == expected.resolve()
    except Exception:
        return False


def _load_reusable_round(
    round_dir: Path,
    bot_a: BotLaunchConfig,
    bot_b: BotLaunchConfig,
    *,
    target_hands: int,
    round_kind: str,
    round_index: int,
    job_envelope: dict[str, Any] | None = None,
    allow_passed_receipt: bool = True,
) -> dict[str, Any] | None:
    """Return an identity-bound terminal receipt that is safe to resume from.

    A worker restart is process recovery, not a semantic retry. Completed failed
    or inconclusive rounds must remain in the same suite; otherwise retaining
    successful rounds while rerunning failed ones would cherry-pick a pass.
    Explicit terminal retries use a new suite attempt and rerun all rounds.
    """
    path = round_dir / "receipt.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(receipt, dict):
        return None
    if (
        receipt.get("duration_sec") is None
        or receipt.get("round_kind") != round_kind
        or int(receipt.get("round_index", 0) or 0) != round_index
        or int(receipt.get("target_hands", 0) or 0) != target_hands
    ):
        return None
    if receipt.get("job_envelope") != job_envelope:
        return None
    actual_a = receipt.get("bot_a") if isinstance(receipt.get("bot_a"), dict) else {}
    actual_b = receipt.get("bot_b") if isinstance(receipt.get("bot_b"), dict) else {}
    if not _same_launch_path(actual_a.get("path"), bot_a.path):
        return None
    if not _same_launch_path(actual_b.get("path"), bot_b.path):
        return None
    if actual_a.get("role") != bot_a.role or actual_b.get("role") != bot_b.role:
        return None
    if receipt.get("passed") is not True:
        return receipt
    # A formal pass cannot inherit authority from JSON writable by the same
    # operator account. Formal jobs rerun passed slots; only a retained failure
    # may be reused because it can deny certification but cannot create a pass.
    if not allow_passed_receipt:
        return None
    if receipt.get("issues"):
        return None
    if round_completion_issues(receipt, target_hands):
        return None
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    required = (
        "receipt",
        "platform_log",
        "bot_a_log",
        "bot_b_log",
        "bot_a_stdout",
        "bot_a_stderr",
        "bot_b_stdout",
        "bot_b_stderr",
    )
    try:
        if any(
            not Path(str(artifacts.get(key) or "")).is_file()
            or Path(str(artifacts.get(key) or "")).is_symlink()
            for key in required
        ):
            return None
    except Exception:
        return None
    if target_hands >= 70:
        canonical = artifacts.get("canonical_thp")
        if not isinstance(canonical, dict):
            return None
        try:
            if int(canonical.get("hand_records", 0) or 0) != target_hands:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        if len(str(canonical.get("sha256") or "")) != 64:
            return None
    return receipt


def _fresh_formal_execution_dir(round_slot: Path) -> Path:
    execution_dir = round_slot / "executions" / f"run_{time.time_ns()}_{os.getpid()}"
    execution_dir.mkdir(parents=True, exist_ok=False)
    return execution_dir


def _record_formal_slot_receipt(round_slot: Path, receipt: dict[str, Any]) -> None:
    round_slot.mkdir(parents=True, exist_ok=True)
    _write_json(round_slot / "receipt.json", receipt)


def _write_suite_progress(
    suite_dir: Path,
    *,
    rounds: list[dict[str, Any]],
    rounds_requested: int,
    resumed_rounds: int,
) -> None:
    _write_json(suite_dir / "progress.json", {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "rounds_requested": rounds_requested,
        "rounds_completed": len(rounds),
        "rounds_passed": sum(1 for item in rounds if item.get("passed")),
        "resumed_rounds": resumed_rounds,
        "latest_round": (
            {
                "round_kind": rounds[-1].get("round_kind"),
                "round_index": rounds[-1].get("round_index"),
                "passed": rounds[-1].get("passed"),
            }
            if rounds
            else None
        ),
    })


def run_official_acceptance_sync(
    candidate: str | Path,
    *,
    opponent: str | Path | None = None,
    self_play_rounds: int = 1,
    opponent_rounds: int = 1,
    target_hands: int = 70,
    config: OfficialPlatformConfig | None = None,
    results_dir: Path | None = None,
    suite_dir: Path | None = None,
    round_runner: RoundRunner = run_official_round,
    job_envelope: dict[str, Any] | None = None,
) -> NationalAcceptanceResult:
    """Run official EXE compliance rounds.

    This low-level runner defaults to normal 70-hand official rounds for manual
    acceptance use. Automated evolution calls it through official_certification
    with short smoke/compliance specs so the Windows EXE stays a protocol oracle,
    not the strength or generation-tracking harness.
    """
    cfg = config or OfficialPlatformConfig()
    if results_dir is not None:
        cfg = _copy_config(cfg, results_dir=Path(results_dir))
    suite_dir = (
        Path(suite_dir).expanduser().resolve()
        if suite_dir is not None
        else cfg.results_dir / f"acceptance_{_now_id()}"
    )
    suite_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = Path(candidate).expanduser().resolve()
    opponent_path = Path(opponent).expanduser().resolve() if opponent else None
    self_play_rounds = max(0, int(self_play_rounds))
    opponent_rounds = max(0, int(opponent_rounds))
    target_hands = max(1, min(70, int(target_hands)))
    issues: list[str] = []
    rounds: list[dict[str, Any]] = []
    resumed_rounds = 0
    rounds_requested = self_play_rounds + opponent_rounds
    candidate_sealed: SealedBotArtifact | None = None
    opponent_sealed: SealedBotArtifact | None = None
    formal_execution: dict[str, Any] | None = None

    try:
        formal_requested = job_envelope is not None and target_hands == 70
        formal_job = formal_requested and round_runner is _PRODUCTION_ROUND_RUNNER
        if formal_requested and not formal_job:
            raise RuntimeError("formal official job cannot replace the production round runner")
        if formal_job:
            formal_execution = validate_execution_profile(
                cfg.exe_path,
                probe_sandbox=True,
            )
            if not formal_execution.get("ok"):
                raise RuntimeError(
                    "official_formal_execution_unavailable: "
                    + "; ".join(formal_execution.get("issues") or [])
                )
            candidate_sealed = seal_bot_artifact(
                candidate_path,
                suite_dir / "sealed_artifacts" / "candidate",
                expected_hash=str(job_envelope.get("candidate_hash") or ""),
            )
            if opponent_path is not None:
                opponent_sealed = seal_bot_artifact(
                    opponent_path,
                    suite_dir / "sealed_artifacts" / "opponent",
                    expected_hash=str(job_envelope.get("opponent_hash") or ""),
                )
        with _official_platform_lock(cfg):
            for index in range(1, self_play_rounds + 1):
                round_dir = suite_dir / f"self_play_{index:02d}"
                bot_a = BotLaunchConfig(
                    candidate_path,
                    name="BotA",
                    seat="upper",
                    role="candidate",
                    instance_id="candidate_a",
                    sealed_artifact=candidate_sealed,
                )
                bot_b = BotLaunchConfig(
                    candidate_path,
                    name="BotB",
                    seat="lower",
                    role="candidate",
                    instance_id="candidate_b",
                    sealed_artifact=candidate_sealed,
                )
                receipt = _load_reusable_round(
                    round_dir,
                    bot_a,
                    bot_b,
                    target_hands=target_hands,
                    round_kind="self_play",
                    round_index=index,
                    job_envelope=job_envelope,
                    allow_passed_receipt=not formal_job,
                )
                if receipt is None:
                    execution_dir = (
                        _fresh_formal_execution_dir(round_dir)
                        if formal_job
                        else round_dir
                    )
                    round_kwargs = {
                        "target_hands": target_hands,
                        "round_kind": "self_play",
                        "round_index": index,
                        "config": cfg,
                        "out_dir": execution_dir,
                    }
                    if job_envelope is not None:
                        round_kwargs["job_envelope"] = job_envelope
                    receipt = round_runner(
                        bot_a,
                        bot_b,
                        **round_kwargs,
                    )
                    if formal_job:
                        _record_formal_slot_receipt(round_dir, receipt)
                else:
                    resumed_rounds += 1
                rounds.append(receipt)
                _write_suite_progress(
                    suite_dir,
                    rounds=rounds,
                    rounds_requested=rounds_requested,
                    resumed_rounds=resumed_rounds,
                )
                if not receipt.get("passed"):
                    issues.extend(f"self_play_{index}: {issue}" for issue in receipt.get("issues", []) or ["failed"])

            if opponent_rounds and opponent_path is None:
                issues.append("official_acceptance_opponent_missing")
            for index in range(1, opponent_rounds + 1):
                if opponent_path is None:
                    break
                round_dir = suite_dir / f"opponent_{index:02d}"
                candidate_first = index % 2 == 1
                candidate_launch = BotLaunchConfig(
                    candidate_path,
                    name="Candidate",
                    seat="upper" if candidate_first else "lower",
                    role="candidate",
                    instance_id="candidate",
                    sealed_artifact=candidate_sealed,
                )
                opponent_launch = BotLaunchConfig(
                    opponent_path,
                    name="Opponent",
                    seat="lower" if candidate_first else "upper",
                    role="opponent",
                    instance_id="opponent",
                    sealed_artifact=opponent_sealed,
                )
                bot_a = candidate_launch if candidate_first else opponent_launch
                bot_b = opponent_launch if candidate_first else candidate_launch
                receipt = _load_reusable_round(
                    round_dir,
                    bot_a,
                    bot_b,
                    target_hands=target_hands,
                    round_kind="opponent",
                    round_index=index,
                    job_envelope=job_envelope,
                    allow_passed_receipt=not formal_job,
                )
                if receipt is None:
                    execution_dir = (
                        _fresh_formal_execution_dir(round_dir)
                        if formal_job
                        else round_dir
                    )
                    round_kwargs = {
                        "target_hands": target_hands,
                        "round_kind": "opponent",
                        "round_index": index,
                        "config": cfg,
                        "out_dir": execution_dir,
                    }
                    if job_envelope is not None:
                        round_kwargs["job_envelope"] = job_envelope
                    receipt = round_runner(
                        bot_a,
                        bot_b,
                        **round_kwargs,
                    )
                    if formal_job:
                        _record_formal_slot_receipt(round_dir, receipt)
                else:
                    resumed_rounds += 1
                rounds.append(receipt)
                _write_suite_progress(
                    suite_dir,
                    rounds=rounds,
                    rounds_requested=rounds_requested,
                    resumed_rounds=resumed_rounds,
                )
                if not receipt.get("passed"):
                    issues.extend(f"opponent_{index}: {issue}" for issue in receipt.get("issues", []) or ["failed"])
    except Exception as exc:
        issues.append(f"official_acceptance_suite_exception: {type(exc).__name__}: {str(exc)[:500]}")

    passed_rounds = sum(1 for receipt in rounds if receipt.get("passed"))
    failed_rounds = len(rounds) - passed_rounds
    from official_attribution import attribute_suite

    attribution = attribute_suite(rounds)
    suite_passed = not issues and len(rounds) == self_play_rounds + opponent_rounds
    if suite_passed:
        outcome = "passed"
        failure_side = ""
    elif attribution.get("candidate_blocking"):
        outcome = "candidate_failure"
        failure_side = "candidate"
    else:
        outcome = "infrastructure_failure"
        failure_side = "official_platform_or_harness"
    summary = {
        "suite_dir": str(suite_dir),
        "self_play_rounds": self_play_rounds,
        "opponent_rounds": opponent_rounds,
        "target_hands": target_hands,
        "rounds_requested": self_play_rounds + opponent_rounds,
        "rounds_run": len(rounds),
        "passed_rounds": passed_rounds,
        "failed_rounds": failed_rounds,
        "resumed_rounds": resumed_rounds,
        "official_platform": True,
        "attribution": attribution,
        "formal_execution": formal_execution,
    }
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate": str(candidate_path),
        "opponent": str(opponent_path) if opponent_path else None,
        "config": _jsonable(cfg),
        "summary": summary,
        "rounds": rounds,
        "issues": issues,
        "job_envelope": job_envelope,
        "formal_execution": formal_execution,
    }
    _write_json(suite_dir / "summary.json", report)
    return NationalAcceptanceResult(
        candidate=candidate_path.name,
        opponents=[opponent_path.name] if opponent_path else [],
        hands_per_pair=target_hands,
        passed=suite_passed,
        outcome=outcome,
        failure_side=failure_side,
        issues=issues[:20],
        summary=summary,
        matrix={},
        report=report,
    )


async def run_official_acceptance(
    candidate: str | Path,
    *,
    opponent: str | Path | None = None,
    self_play_rounds: int = 1,
    opponent_rounds: int = 1,
    target_hands: int = 70,
    config: OfficialPlatformConfig | None = None,
    results_dir: Path | None = None,
    job_envelope: dict[str, Any] | None = None,
) -> NationalAcceptanceResult:
    return await run_blocking_isolated(
        run_official_acceptance_sync,
        candidate,
        thread_name_prefix="official-exe",
        opponent=opponent,
        self_play_rounds=self_play_rounds,
        opponent_rounds=opponent_rounds,
        target_hands=target_hands,
        config=config,
        results_dir=results_dir,
        job_envelope=job_envelope,
    )


async def run_official_smoke(
    candidate: str | Path,
    *,
    target_hands: int = 5,
    config: OfficialPlatformConfig | None = None,
) -> dict[str, Any]:
    result = await run_official_acceptance(
        candidate,
        opponent=None,
        self_play_rounds=1,
        opponent_rounds=0,
        target_hands=target_hands,
        config=config,
    )
    return {
        "passed": result.passed,
        "execution_mode": "official_windows_platform",
        "hands": target_hands,
        "issues": result.issues,
        "result": result.model_dump(),
    }
