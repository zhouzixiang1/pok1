#!/usr/bin/env python3
"""Bind and verify one transient collector systemd invocation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable


RUNNING_UNIT_SCHEMA = "systemd_collector_running_unit_v2"
QUIESCENCE_SCHEMA = "systemd_collector_quiescence_v3"
PROCESS_MARKERS = (
    "longrun_collect_oppmodel.py", "native_tcp_counterfactual_probe.py",
)
UNIT_NAME = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
INVOCATION_ID = re.compile(r"^[0-9a-f]{32}$")
BOOT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
MAX_CAPTURE_AGE_NS = 15 * 60 * 1_000_000_000
SYSTEMD_PROPERTIES = (
    "LoadState", "Transient", "KillMode", "Restart", "MainPID",
    "ActiveState", "ControlGroup", "InvocationID", "ExecStart", "CollectMode",
    "WorkingDirectory", "Id",
)
QUIESCENT_PROPERTIES = SYSTEMD_PROPERTIES[:7] + ("CollectMode",)
SETTLE_PROPERTIES = SYSTEMD_PROPERTIES + ("Result",)


def _canonical_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise RuntimeError(f"{field} must be a sha256 digest")
    return value


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError(f"{field} must be an integer >= {minimum}")
    return value


def _systemd_show(
    collector_unit: str, properties: tuple[str, ...]
) -> dict[str, str]:
    if not isinstance(collector_unit, str) or not UNIT_NAME.fullmatch(collector_unit):
        raise RuntimeError("collector unit must be an explicit .service name")
    command = [
        "systemctl", "--user", "show", collector_unit,
        *[f"--property={name}" for name in properties],
    ]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("cannot verify collector systemd unit") from exc
    observed = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in observed:
            raise RuntimeError("collector systemd properties are malformed")
        observed[key] = value
    if set(observed) != set(properties):
        raise RuntimeError("collector systemd properties are incomplete")
    return observed


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError as exc:
        raise RuntimeError("cannot bind the collector boot identity") from exc
    if not BOOT_ID.fullmatch(value):
        raise RuntimeError("collector boot identity is malformed")
    return value


def _proc_identity(main_pid: int) -> dict[str, Any]:
    root = Path("/proc") / str(main_pid)
    try:
        status = (root / "status").read_text(encoding="utf-8")
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        process_uid = int(uid_line.split()[1])
        raw_argv = (root / "cmdline").read_bytes()
        stat = (root / "stat").read_text(encoding="utf-8")
        cgroup = (root / "cgroup").read_text(encoding="utf-8")
        process_cwd = (root / "cwd").resolve(strict=True)
    except (OSError, StopIteration, ValueError) as exc:
        raise RuntimeError("cannot bind the running collector process") from exc
    closing = stat.rfind(")")
    fields = stat[closing + 2:].split() if closing >= 0 else []
    if len(fields) <= 19:
        raise RuntimeError("collector process stat is malformed")
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise RuntimeError("collector process start time is malformed") from exc
    cgroups = []
    for line in cgroup.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[2].startswith("/"):
            raise RuntimeError("collector process cgroup is malformed")
        cgroups.append(parts[2])
    argv = [os.fsdecode(item) for item in raw_argv.split(b"\0") if item]
    scripts = [
        item for item in argv if Path(item).name == "longrun_collect_oppmodel.py"
    ]
    if len(scripts) != 1:
        raise RuntimeError("collector process script identity is malformed")
    script_path = Path(scripts[0])
    if not script_path.is_absolute():
        script_path = process_cwd / script_path
    try:
        script_path = script_path.resolve(strict=True)
        script_sha256 = hashlib.sha256(script_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError("cannot bind the collector script bytes") from exc
    return {
        "process_argv": argv,
        "process_scan_uid": process_uid,
        "process_start_ticks": start_ticks,
        "process_state": fields[0],
        "process_cgroups": sorted(set(cgroups)),
        "process_cwd": str(process_cwd),
        "collector_script_path": str(script_path),
        "collector_script_sha256": script_sha256,
    }


def _validate_argv(argv: Any, source_dir: str) -> None:
    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise RuntimeError("collector process argv is malformed")
    scripts = [
        index for index, item in enumerate(argv)
        if Path(item).name == "longrun_collect_oppmodel.py"
    ]
    out_flags = [index for index, item in enumerate(argv) if item == "--out-dir"]
    if (
        len(scripts) != 1
        or len(out_flags) != 1
        or out_flags[0] + 1 >= len(argv)
        or str(Path(argv[out_flags[0] + 1]).resolve()) != source_dir
    ):
        raise RuntimeError("collector process argv is not source-bound")


def validate_running_unit_receipt(
    receipt: Any, *, require_current_machine: bool = False
) -> None:
    required = {
        "schema", "collector_unit", "source_dir", "load_state", "transient",
        "kill_mode", "restart", "collect_mode", "main_pid", "active_state",
        "control_group", "working_directory",
        "invocation_id", "exec_start", "process_argv", "process_scan_uid",
        "process_start_ticks", "process_cgroups", "boot_id",
        "process_state",
        "process_cwd", "collector_script_path", "collector_script_sha256",
        "captured_monotonic_ns", "stop_requested_by_tool", "migration_intent",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise RuntimeError("collector running-unit receipt fields changed")
    unsigned = dict(receipt)
    recorded = _digest(unsigned.pop("receipt_sha256"), field="running-unit receipt")
    source = str(receipt.get("source_dir") or "")
    intent = receipt.get("migration_intent")
    if (
        recorded != _canonical_sha256(unsigned)
        or receipt.get("schema") != RUNNING_UNIT_SCHEMA
        or not UNIT_NAME.fullmatch(str(receipt.get("collector_unit") or ""))
        or not Path(source).is_absolute()
        or receipt.get("load_state") != "loaded"
        or receipt.get("transient") != "yes"
        or receipt.get("kill_mode") != "control-group"
        or receipt.get("restart") != "no"
        or receipt.get("collect_mode") != "inactive"
        or receipt.get("active_state") != "active"
        or not INVOCATION_ID.fullmatch(str(receipt.get("invocation_id") or ""))
        or not BOOT_ID.fullmatch(str(receipt.get("boot_id") or ""))
        or receipt.get("stop_requested_by_tool") is not True
        or receipt.get("process_state") not in {"T", "t"}
        or not isinstance(intent, dict)
        or not intent
        or intent.get("source_dir") != source
        or not isinstance(receipt.get("exec_start"), str)
    ):
        raise RuntimeError("collector running-unit receipt is not authoritative")
    main_pid = _integer(receipt.get("main_pid"), field="running main_pid", minimum=1)
    _integer(
        receipt.get("process_start_ticks"), field="process_start_ticks", minimum=1
    )
    captured = _integer(
        receipt.get("captured_monotonic_ns"), field="captured_monotonic_ns", minimum=1
    )
    _validate_argv(receipt.get("process_argv"), source)
    exec_argv = f"argv[]={' '.join(receipt['process_argv'])} ;"
    script_arg = next(
        item for item in receipt["process_argv"]
        if Path(item).name == "longrun_collect_oppmodel.py"
    )
    process_cwd = str(receipt.get("process_cwd") or "")
    script_path = Path(script_arg)
    if not script_path.is_absolute():
        script_path = Path(process_cwd) / script_path
    control_group = receipt.get("control_group")
    cgroups = receipt.get("process_cgroups")
    if (
        exec_argv not in receipt["exec_start"]
        or not Path(process_cwd).is_absolute()
        or receipt.get("working_directory") != process_cwd
        or str(script_path.resolve()) != receipt.get("collector_script_path")
        or _digest(
            receipt.get("collector_script_sha256"), field="collector script"
        ) != intent.get("source_collector_sha256")
        or not isinstance(control_group, str)
        or not control_group.startswith("/")
        or ".." in Path(control_group).parts
        or not isinstance(cgroups, list)
        or cgroups != sorted(set(cgroups))
        or control_group not in cgroups
        or any(
            not isinstance(group, str)
            or not group.startswith("/")
            or ".." in Path(group).parts
            for group in cgroups
        )
    ):
        raise RuntimeError("collector running control group is invalid")
    process_uid = _integer(receipt.get("process_scan_uid"), field="process_scan_uid")
    if require_current_machine:
        age = time.monotonic_ns() - captured
        if (
            receipt["boot_id"] != _boot_id()
            or process_uid != os.getuid()
            or age < 0
            or age > MAX_CAPTURE_AGE_NS
        ):
            raise RuntimeError("collector running-unit receipt is stale")
    if main_pid <= 0:
        raise RuntimeError("collector running-unit pid changed")


def _capture_running_unit_snapshot(
    collector_unit: str, source_dir: Path, migration_intent: dict[str, Any]
) -> dict[str, Any]:
    first = _systemd_show(collector_unit, SYSTEMD_PROPERTIES)
    try:
        main_pid = int(first["MainPID"])
    except ValueError as exc:
        raise RuntimeError("collector systemd MainPID is malformed") from exc
    if (
        first["LoadState"] != "loaded"
        or first["Transient"] != "yes"
        or first["KillMode"] != "control-group"
        or first["Restart"] != "no"
        or first["CollectMode"] != "inactive"
        or first["ActiveState"] != "active"
        or main_pid <= 0
    ):
        raise RuntimeError("collector systemd unit is not safely running")
    process = _proc_identity(main_pid)
    second = _systemd_show(collector_unit, SYSTEMD_PROPERTIES)
    second_process = _proc_identity(main_pid)
    if first != second or process != second_process:
        raise RuntimeError("collector invocation changed during capture")
    return {
        "schema": RUNNING_UNIT_SCHEMA,
        "collector_unit": collector_unit,
        "source_dir": str(source_dir.resolve()),
        "load_state": first["LoadState"],
        "transient": first["Transient"],
        "kill_mode": first["KillMode"],
        "restart": first["Restart"],
        "collect_mode": first["CollectMode"],
        "main_pid": main_pid,
        "active_state": first["ActiveState"],
        "control_group": first["ControlGroup"],
        "working_directory": first["WorkingDirectory"],
        "invocation_id": first["InvocationID"],
        "exec_start": first["ExecStart"],
        **process,
        "boot_id": _boot_id(),
        "captured_monotonic_ns": time.monotonic_ns(),
        "migration_intent": migration_intent,
    }


def write_running_unit_receipt(path: Path, receipt: dict[str, Any]) -> None:
    validate_running_unit_receipt(receipt, require_current_machine=True)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError("running-unit receipt path already exists")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as exc:
        raise RuntimeError("running-unit receipt path already exists") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _proc_matches(source_dir: Path) -> list[int]:
    matches = []
    uid = os.getuid()
    source = str(source_dir.resolve()).encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
            uid_line = next(
                line for line in status.splitlines() if line.startswith("Uid:")
            )
            if int(uid_line.split()[1]) != uid:
                continue
            cmdline = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, StopIteration, ValueError) as exc:
            raise RuntimeError(f"cannot inspect process {entry.name}") from exc
        if source in cmdline and any(
            marker.encode() in cmdline for marker in PROCESS_MARKERS
        ):
            matches.append(int(entry.name))
    return sorted(matches)


def _cgroup_processes(control_groups: set[str]) -> tuple[bool, set[int]]:
    present = False
    pids: set[int] = set()
    for control_group in control_groups:
        if not control_group.startswith("/") or ".." in Path(control_group).parts:
            raise RuntimeError("collector control group path is invalid")
        cgroup = Path("/sys/fs/cgroup") / control_group.lstrip("/")
        exists = cgroup.exists()
        present = present or exists
        if exists:
            try:
                for path in cgroup.rglob("cgroup.procs"):
                    pids.update(int(line) for line in path.read_text().splitlines() if line)
            except (OSError, ValueError) as exc:
                raise RuntimeError("cannot inspect collector control group") from exc
    return present, pids


def _proc_state(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        raise RuntimeError(f"cannot inspect process state {pid}") from exc
    closing = stat.rfind(")")
    fields = stat[closing + 2:].split() if closing >= 0 else []
    if not fields:
        raise RuntimeError("collector process state is malformed")
    return fields[0]


def _wait_control_group_stopped(preflight: dict[str, str]) -> None:
    control_group = preflight["ControlGroup"]
    main_pid = int(preflight["MainPID"])
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = _systemd_show(preflight["Id"], SYSTEMD_PROPERTIES)
        if any(
            current[field] != preflight[field]
            for field in (
                "LoadState", "Transient", "KillMode", "Restart", "MainPID",
                "ActiveState", "ControlGroup", "InvocationID", "ExecStart",
                "CollectMode", "WorkingDirectory",
            )
        ):
            raise RuntimeError("collector invocation changed while freezing")
        present, pids = _cgroup_processes({control_group})
        states = {_proc_state(pid) for pid in pids}
        if present and main_pid in pids and states and states <= {"T", "t"}:
            return
        time.sleep(0.01)
    raise RuntimeError("collector control group did not freeze")


def _resume_frozen_collector(preflight: dict[str, str]) -> None:
    collector_unit = preflight["Id"]
    try:
        subprocess.run(
            ["systemctl", "--user", "kill", "--kill-whom=all",
             "--signal=SIGCONT", collector_unit],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("collector remains frozen after failed cutover") from exc
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = _systemd_show(collector_unit, SYSTEMD_PROPERTIES)
        present, pids = _cgroup_processes({preflight["ControlGroup"]})
        states = {_proc_state(pid) for pid in pids}
        if (
            current["InvocationID"] == preflight["InvocationID"]
            and current["MainPID"] == preflight["MainPID"]
            and current["ActiveState"] == "active"
            and present
            and int(preflight["MainPID"]) in pids
            and None not in states
            and not states.intersection({"T", "t"})
        ):
            return
        time.sleep(0.01)
    raise RuntimeError("collector remains frozen after failed cutover")


def verify_collector_quiescence(
    collector_unit: str, source_dir: Path, running_unit: dict[str, Any]
) -> dict[str, Any]:
    validate_running_unit_receipt(running_unit, require_current_machine=True)
    if (
        running_unit["collector_unit"] != collector_unit
        or running_unit["source_dir"] != str(source_dir.resolve())
    ):
        raise RuntimeError("collector running-unit binding changed")
    first = _systemd_show(collector_unit, QUIESCENT_PROPERTIES)
    loaded = (
        first["LoadState"] == "loaded"
        and first["Transient"] == "yes"
        and first["KillMode"] == "control-group"
        and first["Restart"] == "no"
        and first["CollectMode"] == "inactive"
        and first["MainPID"] == "0"
        and first["ActiveState"] == "inactive"
    )
    garbage_collected = (
        first["LoadState"] == "not-found"
        and first["MainPID"] == "0"
        and first["ActiveState"] == "inactive"
    )
    if not loaded and not garbage_collected:
        raise RuntimeError("collector systemd unit is not quiescent")
    control_groups = {
        group for group in (first["ControlGroup"], running_unit["control_group"])
        if group
    }
    cgroup_present, cgroup_pids = _cgroup_processes(control_groups)
    if cgroup_pids:
        raise RuntimeError("collector control group still has processes")
    process_matches = _proc_matches(source_dir)
    if process_matches:
        raise RuntimeError("collector or native probe process is still running")
    second = _systemd_show(collector_unit, QUIESCENT_PROPERTIES)
    if first != second or _proc_matches(source_dir):
        raise RuntimeError("collector quiescence changed during verification")
    second_present, second_pids = _cgroup_processes(control_groups)
    if second_pids or second_present != cgroup_present:
        raise RuntimeError("collector control group changed during verification")
    return {
        "schema": QUIESCENCE_SCHEMA,
        "collector_unit": collector_unit,
        "source_dir": str(source_dir.resolve()),
        "load_state": first["LoadState"],
        "transient": running_unit["transient"],
        "kill_mode": running_unit["kill_mode"],
        "restart": running_unit["restart"],
        "collect_mode": running_unit["collect_mode"],
        "main_pid": 0,
        "active_state": first["ActiveState"],
        "control_group": running_unit["control_group"],
        "unit_disposition": (
            "garbage_collected" if garbage_collected else "loaded_inactive"
        ),
        "running_unit": running_unit,
        "control_groups_checked": sorted(control_groups),
        "control_group_present": cgroup_present,
        "cgroup_process_count": 0,
        "process_scan_uid": os.getuid(),
        "process_markers": list(PROCESS_MARKERS),
        "matching_process_count": len(process_matches),
    }


def _reset_failed_bound_unit(
    preflight: dict[str, str], source_dir: Path
) -> None:
    """Clear SIGKILL's failed state only for the bound, empty invocation."""
    current = _systemd_show(preflight["Id"], SETTLE_PROPERTIES)
    if current["LoadState"] == "not-found":
        return
    if current["InvocationID"] != preflight["InvocationID"]:
        raise RuntimeError("collector invocation changed while settling")
    if current["ActiveState"] != "failed":
        return
    if (
        any(
            current[field] != preflight[field]
            for field in (
                "LoadState", "Transient", "KillMode", "Restart",
                "InvocationID", "CollectMode", "WorkingDirectory", "Id",
            )
        )
        or current["MainPID"] != "0"
        or current["Result"] != "signal"
        or current["ControlGroup"] not in {"", preflight["ControlGroup"]}
        or current["ExecStart"].split(" ; stop_time=", 1)[0]
        != preflight["ExecStart"].split(" ; stop_time=", 1)[0]
    ):
        raise RuntimeError("collector invocation changed while settling")
    _present, pids = _cgroup_processes({preflight["ControlGroup"]})
    if pids or _proc_matches(source_dir):
        return
    second = _systemd_show(preflight["Id"], SETTLE_PROPERTIES)
    second_present, second_pids = _cgroup_processes(
        {preflight["ControlGroup"]}
    )
    if (
        second != current
        or second_present != _present
        or second_pids != pids
        or _proc_matches(source_dir)
    ):
        raise RuntimeError("collector invocation changed while settling")
    try:
        subprocess.run(
            ["systemctl", "--user", "reset-failed", preflight["Id"]],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("cannot settle the stopped collector unit") from exc


def validate_quiescence_receipt(receipt: Any) -> None:
    required = {
        "schema", "collector_unit", "source_dir", "load_state", "transient",
        "kill_mode", "restart", "collect_mode", "main_pid", "active_state",
        "control_group",
        "unit_disposition", "running_unit", "control_groups_checked",
        "control_group_present", "cgroup_process_count", "process_scan_uid",
        "process_markers", "matching_process_count",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise RuntimeError("collector quiescence receipt fields changed")
    running = receipt.get("running_unit")
    validate_running_unit_receipt(running)
    disposition = receipt.get("unit_disposition")
    groups = receipt.get("control_groups_checked")
    if (
        receipt.get("schema") != QUIESCENCE_SCHEMA
        or receipt.get("collector_unit") != running["collector_unit"]
        or receipt.get("source_dir") != running["source_dir"]
        or receipt.get("transient") != "yes"
        or receipt.get("kill_mode") != "control-group"
        or receipt.get("restart") != "no"
        or receipt.get("collect_mode") != "inactive"
        or receipt.get("active_state") != "inactive"
        or receipt.get("process_scan_uid") != os.getuid()
        or receipt.get("process_markers") != list(PROCESS_MARKERS)
        or not isinstance(receipt.get("control_group_present"), bool)
        or receipt.get("control_group") != running["control_group"]
        or not isinstance(groups, list)
        or not groups
        or groups != sorted(set(groups))
        or running["control_group"] not in groups
        or (disposition == "loaded_inactive" and receipt.get("load_state") != "loaded")
        or (
            disposition == "garbage_collected"
            and receipt.get("load_state") != "not-found"
        )
        or disposition not in {"loaded_inactive", "garbage_collected"}
    ):
        raise RuntimeError("collector quiescence receipt is not authoritative")
    for field in ("main_pid", "cgroup_process_count", "matching_process_count"):
        if _integer(receipt.get(field), field=field) != 0:
            raise RuntimeError("collector quiescence receipt is not zero")


def stop_bound_collector(
    collector_unit: str, source_dir: Path,
    migration_intent_factory: Callable[[], dict[str, Any]],
    receipt_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    preflight = _systemd_show(collector_unit, SYSTEMD_PROPERTIES)
    if (
        preflight["LoadState"] != "loaded"
        or preflight["Transient"] != "yes"
        or preflight["KillMode"] != "control-group"
        or preflight["Restart"] != "no"
        or preflight["CollectMode"] != "inactive"
        or preflight["ActiveState"] != "active"
        or preflight["Id"] != collector_unit
        or not preflight["MainPID"].isdigit()
        or int(preflight["MainPID"]) <= 0
    ):
        raise RuntimeError("collector systemd unit is not safely running")
    frozen = False
    stop_accepted = False
    try:
        subprocess.run(
            ["systemctl", "--user", "kill", "--kill-whom=all", "--signal=SIGSTOP",
             collector_unit],
            check=True, capture_output=True, text=True, timeout=10,
        )
        frozen = True
        _wait_control_group_stopped(preflight)
        migration_intent = migration_intent_factory()
        snapshot = _capture_running_unit_snapshot(
            collector_unit, source_dir, migration_intent
        )
        unsigned = {**snapshot, "stop_requested_by_tool": True}
        running = {**unsigned, "receipt_sha256": _canonical_sha256(unsigned)}
        write_running_unit_receipt(receipt_path, running)
        subprocess.run(
            ["systemctl", "--user", "kill", "--kill-whom=all", "--signal=SIGKILL",
             collector_unit],
            check=True, capture_output=True, text=True, timeout=10,
        )
        stop_accepted = True
    except Exception as exc:
        if frozen and not stop_accepted:
            try:
                _resume_frozen_collector(preflight)
            except RuntimeError as resume_error:
                raise resume_error from exc
        if isinstance(exc, (OSError, subprocess.SubprocessError)):
            raise RuntimeError(
                "cannot stop the bound collector control group"
            ) from exc
        raise
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            return running, verify_collector_quiescence(
                collector_unit, source_dir, running
            )
        except RuntimeError:
            _reset_failed_bound_unit(preflight, source_dir)
            time.sleep(0.1)
    raise RuntimeError("collector control group did not become quiescent")
