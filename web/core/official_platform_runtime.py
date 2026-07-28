"""Round-runtime infrastructure cluster for the official platform harness.

Moved from ``official_platform_harness`` to keep the Wine/Xvfb display,
process/port, bot-launch, wire-capture and terminal-evidence helpers in a
cohesive unit. Every intra-companion call to a main-side symbol routes through
``_oph.<name>(...)`` so parent-level monkeypatches (seal_bot_artifact,
current_system_native_runtime_errors, launch_sandboxed_bot,
validate_execution_profile, EndpointLease) remain observable, matching the
``official_platform_thp_parse`` companion precedent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Any

import official_platform_harness as _oph


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
    if _oph.RAISE_ACTION_RE.fullmatch(message):
        return None
    if message.startswith("bet"):
        return f"illegal_bet_action: msg={message!r}"
    if message.strip() != message:
        return f"protocol_action_whitespace: msg={message!r}"
    if message.startswith("raise"):
        return f"protocol_raise_format: msg={message!r}"
    return f"protocol_action_format: msg={message!r}"


def parse_bot_log(path: str | Path, *, tail_lines: int = 30) -> _oph.BotLogStats:
    log_path = Path(path)
    stats = _oph.BotLogStats(path=str(log_path), exists=log_path.exists())
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
            send_match = _oph.SEND_MSG_RE.search(line)
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
        for pattern in _oph.CRITICAL_LOG_PATTERNS:
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
        "bot_a": _oph._jsonable(stats_a),
        "bot_b": _oph._jsonable(stats_b),
        "hands_started_min": min(stats_a.preflop, stats_b.preflop),
        "settlements_min": min(stats_a.earnchips, stats_b.earnchips),
        "issues": issues,
        "progress_key": (stats_a.progress_key(), stats_b.progress_key()),
    }


def check_environment(
    config: _oph.OfficialPlatformConfig | None = None,
    *,
    require_formal_sandbox: bool = False,
) -> dict[str, Any]:
    cfg = config or _oph.OfficialPlatformConfig()
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
        execution_profile = _oph.validate_execution_profile(
            cfg.exe_path,
            probe_sandbox=True,
        )
        issues.extend(execution_profile.get("issues") or [])
    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "config": _oph._jsonable(cfg),
        "execution_profile": execution_profile,
    }

def _env_for_display(config: _oph.OfficialPlatformConfig, display: str) -> dict[str, str]:
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
                ["xdotool", "search", "--onlyvisible", "--name", _oph.WINDOW_TITLE],
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


def _wait_for_listen(config: _oph.OfficialPlatformConfig) -> None:
    deadline = time.time() + config.listen_timeout_sec
    while time.time() < deadline:
        if _port_listening(config.host, config.port):
            return
        time.sleep(0.5)
    raise TimeoutError(f"official platform did not listen on {config.host}:{config.port}")


def _port_busy_before_start(config: _oph.OfficialPlatformConfig) -> bool:
    return _port_listening(config.host, config.port)


def _wait_for_port_free(config: _oph.OfficialPlatformConfig, *, timeout_sec: float = 8.0) -> bool:
    deadline = time.time() + max(0.0, timeout_sec)
    while time.time() < deadline:
        if not _port_listening(config.host, config.port):
            return True
        time.sleep(0.25)
    return not _port_listening(config.host, config.port)


def _launch_bot(
    bot: _oph.BotLaunchConfig,
    *,
    config: _oph.OfficialPlatformConfig,
    env: dict[str, str],
    log_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    port: int | None = None,
) -> subprocess.Popen:
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    endpoint_port = config.port if port is None else int(port)
    try:
        artifact = bot.sealed_artifact
        if artifact is None:
            # Manual/diagnostic rounds retain their lower authority, but bot
            # execution still uses the same central process boundary.  A
            # round-local content-bound copy avoids mounting a mutable source.
            source_hash = _oph.hash_path(bot.path)
            artifact = _oph.seal_bot_artifact(
                bot.path,
                stdout_path.parent / "managed_inputs" / stdout_path.stem,
                expected_hash=source_hash,
            )
        runtime_errors = _oph.current_system_native_runtime_errors(artifact.root)
        if runtime_errors:
            raise RuntimeError(
                "non_system_owned_native_runtime_forbidden:official:"
                f"{bot.name}:{runtime_errors[0]}"
            )
        profile = _oph.load_execution_profile()
        source_relative = str(
            ((profile.get("managed_executor") or {}).get("source") or {}).get("path")
            or ""
        )
        source_sha256 = hashlib.sha256(
            (_oph.ROOT / source_relative).read_bytes()
        ).hexdigest()
        profile_identity = _oph.execution_profile_identity()
        managed_group = env.get("POK_OFFICIAL_JOB_PROCESS_GROUP") == "1"
        with _oph.EndpointLease.connect(
            config.host,
            endpoint_port,
            timeout=min(10.0, config.listen_timeout_sec),
        ) as endpoint:
            managed = _oph.launch_sandboxed_bot(
                artifact,
                endpoint,
                name=bot.name,
                seat=bot.seat if bot.supports_seat else None,
                log_path=log_path,
                supports_log=bot.supports_log,
                extra_args=bot.extra_args,
                stdout=stdout,
                stderr=stderr,
                start_new_session=not managed_group,
            )
        proc = managed.process
        proc._pok_managed_isolation = _oph.asdict(managed.isolation)  # type: ignore[attr-defined]
        proc._pok_managed_artifact_hash = artifact.artifact_hash  # type: ignore[attr-defined]
        proc._pok_endpoint_lease = {  # type: ignore[attr-defined]
            "consumed": endpoint.consumed,
            "closed": endpoint.closed,
        }
        proc._pok_managed_executor_source_sha256 = source_sha256  # type: ignore[attr-defined]
        proc._pok_execution_profile_identity = profile_identity  # type: ignore[attr-defined]
    except BaseException:
        stdout.close()
        stderr.close()
        raise
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


def _bot_process_isolation_receipt(
    connection: str,
    bot: _oph.BotLaunchConfig,
    process: subprocess.Popen,
) -> dict[str, Any]:
    return {
        "connection": connection,
        "name": bot.name,
        "role": bot.role,
        "instance_id": bot.instance_id,
        "seat": bot.seat,
        "path": str(Path(bot.path).expanduser().resolve()),
        "artifact_hash": getattr(process, "_pok_managed_artifact_hash", None),
        "endpoint_lease": getattr(process, "_pok_endpoint_lease", None),
        "execution_profile": getattr(
            process,
            "_pok_execution_profile_identity",
            None,
        ),
        "managed_executor_source_sha256": getattr(
            process,
            "_pok_managed_executor_source_sha256",
            None,
        ),
        "isolation": getattr(process, "_pok_managed_isolation", None),
    }


def _read_issue_file(path: Path, patterns: tuple[str, ...] | None = None) -> list[str]:
    if patterns is None:
        patterns = _oph.CRITICAL_LOG_PATTERNS
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
    path.write_text(json.dumps(_oph._jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")


class OfficialWireCapture:
    """Lifecycle wrapper for the official EXE TCP probe.

    The main EXE harness is synchronous because it drives Wine, Xvfb, and xdotool.
    The transparent TCP proxy is asyncio-based, so it runs on a private event loop
    thread for the duration of one official round.
    """

    def __init__(self, round_dir: Path, config: _oph.OfficialPlatformConfig):
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

    def summary(self, *, finalized: bool = False) -> dict[str, Any]:
        if not self.enabled or self.recorder is None:
            return {}
        try:
            if self.proxy is not None:
                return self.proxy.summary(finalized=finalized)
            from official_wire_probe import replay_events

            return replay_events(
                list(self.recorder.events),
                finalized=finalized,
            )
        except Exception as exc:
            return {
                "events_seen": 0,
                "hands_started_min": 0,
                "settlements_min": 0,
                "issues": [{"kind": "wire_replay_error", "reason": f"{type(exc).__name__}: {exc}"}],
                "warnings": [],
            }

    def write_replay_summary(self, *, finalized: bool = False) -> dict[str, Any]:
        summary = self.summary(finalized=finalized)
        if self.enabled:
            _write_json(self.replay_summary_path, summary)
        return summary

    def stop(self) -> dict[str, Any]:
        final_summary: dict[str, Any] = {}
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                self.issues.append("wire_probe_stop_error: event loop thread did not stop")
        if self.recorder is not None:
            try:
                final_summary = self.write_replay_summary(finalized=True)
            except Exception as exc:
                self.issues.append(
                    "wire_probe_final_replay_error: "
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
            try:
                self.recorder.close()
            except Exception:
                pass
        self._loop = None
        self._thread = None
        self.proxy = None
        self.recorder = None
        return final_summary


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


def _copy_config(cfg: _oph.OfficialPlatformConfig, **overrides: Any) -> _oph.OfficialPlatformConfig:
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
    return _oph.OfficialPlatformConfig(**values)


@contextmanager
def _official_platform_lock(config: _oph.OfficialPlatformConfig):
    with _oph.acquire_official_platform(
        config.lock_path,
        owner="official-exe-suite",
        timeout=config.lock_timeout_sec,
    ):
        yield


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not allocate unique artifact name for {path}")



def _terminal_thp_wire_binding(
    strict_match: dict[str, Any],
    wire_summary: dict[str, Any],
    *,
    expected_hands: int,
    expected_names: tuple[str, str],
    omitted_runout_bindings: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Bind the THP terminal action to fold or exact dual-wire showdown proof."""

    records = strict_match.get("records")
    if not isinstance(records, list) or len(records) != expected_hands:
        return None, ["terminal_thp_records_invalid"]
    record = records[-1]
    hand = expected_hands
    actions = str(record.get("actions") or "")
    action_tokens = re.findall(r"r\d+|[cf]", actions)
    terminal_action = action_tokens[-1][0] if action_tokens else ""
    seats = wire_summary.get("seats")
    if not isinstance(seats, dict) or len(seats) != 2:
        return None, ["terminal_thp_wire_seats_invalid"]
    seat_names = {
        str(label): str(seat.get("name") or "")
        for label, seat in seats.items()
        if isinstance(seat, dict)
    }
    if (
        len(seat_names) != 2
        or set(seat_names.values()) != set(expected_names)
        or len(set(seat_names.values())) != 2
    ):
        return None, ["terminal_thp_wire_player_identity_invalid"]
    terminal_omissions = [
        item
        for item in omitted_runout_bindings
        if item.get("hand") == hand
    ]
    terminal_showdowns = {
        label: _oph._single_hand_record(seat.get("showdown_records"), hand=hand)
        for label, seat in seats.items()
    }
    if terminal_action == "f":
        if terminal_omissions or any(
            item is not None for item in terminal_showdowns.values()
        ):
            return None, ["terminal_thp_fold_showdown_conflict"]
        payload = {
            "hand": hand,
            "terminal_kind": "fold",
            "thp_actions": actions,
            "thp_earnings": record.get("earnings"),
        }
        return {**payload, "binding_digest": _oph.canonical_digest(payload)}, []
    if terminal_action != "c":
        return None, ["terminal_thp_action_invalid"]
    if len(terminal_omissions) > 1:
        return None, ["terminal_thp_omission_binding_duplicate"]
    if len(terminal_omissions) == 1:
        payload = {
            "hand": hand,
            "terminal_kind": "omitted_allin_showdown",
            "thp_actions": actions,
            "omitted_runout_binding_digest": terminal_omissions[0].get(
                "binding_digest"
            ),
        }
        return {**payload, "binding_digest": _oph.canonical_digest(payload)}, []

    parsed_cards, card_issue = _oph._parse_thp_card_payload(
        str(record.get("cards") or "")
    )
    if card_issue or parsed_cards is None:
        return None, [f"terminal_thp_showdown_cards_invalid:{card_issue}"]
    full_board = parsed_cards["public_cards"]
    if len(full_board) != 5:
        return None, ["terminal_thp_showdown_board_incomplete"]
    players = record.get("players")
    if not isinstance(players, list) or len(players) != 2:
        return None, ["terminal_thp_showdown_players_invalid"]
    holes_by_name = {
        players[0]: parsed_cards["hole_cards_by_position"]["BIGBLIND"],
        players[1]: parsed_cards["hole_cards_by_position"]["SMALLBLIND"],
    }
    seat_binding_digests: dict[str, str] = {}
    for label in sorted(seat_names):
        seat = seats[label]
        name = seat_names[label]
        blind = _oph._single_hand_record(seat.get("blind_records"), hand=hand)
        if (
            blind is None
            or blind.get("blind") not in {"BIGBLIND", "SMALLBLIND"}
            or players[0 if blind.get("blind") == "BIGBLIND" else 1] != name
        ):
            return None, [f"terminal_thp_showdown_blind_invalid:{label}"]
        peer_name = next(
            candidate for candidate in expected_names if candidate != name
        )
        showdown = terminal_showdowns[label]
        revealed = _oph._normalize_wire_cards(
            showdown.get("opponent_cards") if showdown else None
        )
        if (
            revealed is None
            or sorted(tuple(card) for card in revealed)
            != sorted(tuple(card) for card in holes_by_name[peer_name])
        ):
            return None, [f"terminal_thp_showdown_holes_invalid:{label}"]
        public = _oph._single_hand_record(
            seat.get("public_card_records"),
            hand=hand,
        )
        streets = public.get("streets") if public else None
        if not isinstance(streets, dict):
            return None, [f"terminal_thp_showdown_public_invalid:{label}"]
        observed: list[list[int]] = []
        for street in ("flop", "turn", "river"):
            cards = _oph._normalize_wire_cards(streets.get(street, []))
            if cards is None:
                return None, [f"terminal_thp_showdown_public_invalid:{label}"]
            observed.extend(cards)
        if observed != full_board:
            return None, [f"terminal_thp_showdown_public_mismatch:{label}"]
        seat_binding_digests[label] = _oph.canonical_digest({
            "name": name,
            "blind": blind["blind"],
            "revealed_peer_hole": revealed,
            "public_board": observed,
        })
    payload = {
        "hand": hand,
        "terminal_kind": "full_board_showdown",
        "thp_actions": actions,
        "thp_public_cards": full_board,
        "thp_holes_by_player": holes_by_name,
        "seat_binding_digests": seat_binding_digests,
    }
    return {**payload, "binding_digest": _oph.canonical_digest(payload)}, []


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
    allow_provisional_wire: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Read, but do not move, a new exact official THP terminal artifact."""
    paths = _oph._changed_thp_paths(platform_dirs, before=before)
    summaries = _oph._summarize_thp_files([str(path) for path in paths])
    canonical, issues = _oph._canonical_thp_evidence(
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
    strict_match, strict_issues = _oph._strict_thp_match(
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
    omitted_runout_bindings, omitted_runout_issues = _oph._omitted_allin_thp_bindings(
        strict_match,
        wire_summary,
        expected_hands=expected_hands,
        expected_names=expected_names,
        allow_provisional_wire=allow_provisional_wire,
    )
    if omitted_runout_bindings is None or omitted_runout_issues:
        return None, omitted_runout_issues
    terminal_wire_binding, terminal_wire_issues = _terminal_thp_wire_binding(
        strict_match,
        wire_summary,
        expected_hands=expected_hands,
        expected_names=expected_names,
        omitted_runout_bindings=omitted_runout_bindings,
    )
    if terminal_wire_binding is None or terminal_wire_issues:
        return None, terminal_wire_issues
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
        "schema_version": _oph.TERMINAL_COMPLETION_SCHEMA_VERSION,
        "kind": "official-thp-terminal-hand-observation",
        "target_hands": expected_hands,
        "thp_sha256": str(canonical.get("sha256") or ""),
        "thp_bytes": int(canonical.get("bytes", 0) or 0),
        "hand_records": int(canonical.get("hand_records", 0) or 0),
        "hand_index_digest": _oph.canonical_digest(list(range(expected_hands))),
        "wire_prefix_digest": _oph.canonical_digest(wire_prefix),
        "thp_prefix_digest": _oph.canonical_digest(thp_prefix),
        "final_hand": final_hand,
        "match_totals": strict_match["match_totals"],
        "footer_result": strict_match["footer_result"],
        "omitted_allin_runout_bindings": omitted_runout_bindings,
        "omitted_allin_runout_bindings_digest": _oph.canonical_digest(
            omitted_runout_bindings
        ),
        "terminal_wire_binding": terminal_wire_binding,
        "terminal_wire_binding_digest": terminal_wire_binding[
            "binding_digest"
        ],
    }
    return {**payload, "observation_digest": _oph.canonical_digest(payload)}, []


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
        "schema_version": _oph.TERMINAL_COMPLETION_SCHEMA_VERSION,
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
        "omitted_allin_runout_bindings": observation.get(
            "omitted_allin_runout_bindings"
        ),
        "omitted_allin_runout_bindings_digest": observation.get(
            "omitted_allin_runout_bindings_digest"
        ),
        "terminal_wire_binding": observation.get("terminal_wire_binding"),
        "terminal_wire_binding_digest": observation.get(
            "terminal_wire_binding_digest"
        ),
        "terminal_observation_digest": str(observation.get("observation_digest") or ""),
        "strength_evaluation": "not_applicable",
    }
    return {**payload, "evidence_digest": _oph.canonical_digest(payload)}


def round_completion_issues(
    receipt: dict[str, Any],
    target_hands: int,
    *,
    natural_terminal_only: bool = False,
) -> list[str]:
    """Validate complete-round evidence, including the EXE hand-70 THP rule."""
    log_summary = receipt.get("log_summary") if isinstance(receipt, dict) else None
    if not isinstance(log_summary, dict):
        return ["official_round_log_summary_missing"]
    if _target_reached(log_summary, target_hands) and not (
        natural_terminal_only and target_hands == 70
    ):
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
    if evidence.get("evidence_digest") != _oph.canonical_digest(payload):
        issues.append("official_terminal_completion_evidence_digest_mismatch")
    expected_scalars = {
        "schema_version": _oph.TERMINAL_COMPLETION_SCHEMA_VERSION,
        "kind": "official-thp-terminal-settlement",
        "target_hands": 70,
        "completed_hands": 70,
        "wire_settled_hands": 69,
        "log_hands_started": 70,
        "log_tcp_settlements": 69,
        "wire_hands_started": 70,
        "wire_tcp_settlements": 69,
        "canonical_thp_hand_records": 70,
        "hand_index_digest": _oph.canonical_digest(list(range(70))),
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
        actual_indices = [int(value) for value in _oph.THP_HAND_RE.findall(canonical_text)]
        actual_matches = [
            match
            for match in _oph.THP_RECORD_RE.finditer(canonical_text)
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
        strict_match, strict_issues = _oph._strict_thp_match(
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
                if evidence.get("wire_prefix_digest") != _oph.canonical_digest(wire_prefix):
                    issues.append("official_terminal_completion_wire_prefix_digest_mismatch")
                if evidence.get("thp_prefix_digest") != _oph.canonical_digest(thp_prefix):
                    issues.append("official_terminal_completion_thp_prefix_digest_mismatch")
            if evidence.get("match_totals") != strict_match["match_totals"]:
                issues.append("official_terminal_completion_match_totals_mismatch")
            if evidence.get("footer_result") != strict_match["footer_result"]:
                issues.append("official_terminal_completion_footer_result_mismatch")
            omitted_bindings, omitted_issues = _oph._omitted_allin_thp_bindings(
                strict_match,
                wire_summary,
                expected_hands=70,
                expected_names=expected_name_order,
            )
            if omitted_bindings is None or omitted_issues:
                issues.extend(
                    f"official_terminal_completion_{issue}"
                    for issue in omitted_issues
                )
            else:
                if (
                    evidence.get("omitted_allin_runout_bindings")
                    != omitted_bindings
                ):
                    issues.append(
                        "official_terminal_completion_omitted_runout_bindings_mismatch"
                    )
                if (
                    evidence.get("omitted_allin_runout_bindings_digest")
                    != _oph.canonical_digest(omitted_bindings)
                ):
                    issues.append(
                        "official_terminal_completion_omitted_runout_digest_mismatch"
                    )
                terminal_binding, terminal_issues = _terminal_thp_wire_binding(
                    strict_match,
                    wire_summary,
                    expected_hands=70,
                    expected_names=expected_name_order,
                    omitted_runout_bindings=omitted_bindings,
                )
                if terminal_binding is None or terminal_issues:
                    issues.extend(
                        f"official_terminal_completion_{issue}"
                        for issue in terminal_issues
                    )
                else:
                    if evidence.get("terminal_wire_binding") != terminal_binding:
                        issues.append(
                            "official_terminal_completion_terminal_wire_binding_mismatch"
                        )
                    if (
                        evidence.get("terminal_wire_binding_digest")
                        != terminal_binding.get("binding_digest")
                    ):
                        issues.append(
                            "official_terminal_completion_terminal_wire_digest_mismatch"
                        )
    except (OSError, ValueError, TypeError) as exc:
        issues.append(
            "official_terminal_completion_thp_read_error:"
            f"{type(exc).__name__}"
        )
    observation_payload = {
        "schema_version": _oph.TERMINAL_COMPLETION_SCHEMA_VERSION,
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
        "omitted_allin_runout_bindings": evidence.get(
            "omitted_allin_runout_bindings"
        ),
        "omitted_allin_runout_bindings_digest": evidence.get(
            "omitted_allin_runout_bindings_digest"
        ),
        "terminal_wire_binding": evidence.get("terminal_wire_binding"),
        "terminal_wire_binding_digest": evidence.get(
            "terminal_wire_binding_digest"
        ),
    }
    if evidence.get("terminal_observation_digest") != _oph.canonical_digest(observation_payload):
        issues.append("official_terminal_completion_observation_digest_mismatch")
    return list(dict.fromkeys(issues))
