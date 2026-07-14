"""Read-only, production-path Claude Agent SDK operator probe.

The probe is deliberately not a one-shot HTTP health check.  It invokes the
same ``run_claude_query`` primitive as evolution roles and requires a real
multi-turn Read/Bash tool loop.  The model cannot access MCP servers and every
Bash command is matched against a complete-command allowlist before execution.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from typing import Any, Awaitable, Callable

from runtime_architecture_policy import OFFICIAL_ORACLE_DOC_DIGESTS


RECEIPT_SCHEMA = "pok.claude_sdk_operator_probe_receipt/v1"
ROLE_NAME = "OPERATOR SDK PROBE"
TRANSPORT_RELATIVE_PATH = "sever/server/transport.py"
READ_RELATIVE_PATHS = tuple(OFFICIAL_ORACLE_DOC_DIGESTS) + (
    TRANSPORT_RELATIVE_PATH,
)
HASH_COMMAND = "sha256sum " + " ".join(READ_RELATIVE_PATHS)
TRANSPORT_SCAN_COMMAND = (
    "rg -n 'writer.write\\(payload\\)|invalid_server_message_delimiter|"
    "take_client_action|idle_flush_sec' sever/server/transport.py"
)
EXACT_BASH_COMMANDS = (HASH_COMMAND, TRANSPORT_SCAN_COMMAND)
SDK_TOOLS = ("Read", "Bash")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


def _git_head(repo_root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


class _MemoryWriter:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.payloads.append(bytes(payload))

    async def drain(self) -> None:
        return None

    def get_extra_info(self, _name: str) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def _transport_behavior_evidence() -> dict[str, Any]:
    from sever.server.transport import NationalProtocolError, NationalTCPClient

    writer = _MemoryWriter()
    client = NationalTCPClient(asyncio.StreamReader(), writer)
    await client.send_message("raise 200")
    rejects: dict[str, bool] = {}
    for label, message in (("lf", "call\n"), ("cr", "call\r")):
        try:
            await client.send_message(message)
        except NationalProtocolError as exc:
            rejects[label] = str(exc) == "invalid_server_message_delimiter"
        else:
            rejects[label] = False
    return {
        "raw_payload_hex": writer.payloads[0].hex() if writer.payloads else None,
        "raw_payload_exact": writer.payloads == [b"raise 200"],
        "rejects_lf": rejects.get("lf", False),
        "rejects_cr": rejects.get("cr", False),
    }


async def collect_local_evidence(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    observed_oracles: dict[str, str] = {}
    for relative, expected in OFFICIAL_ORACLE_DOC_DIGESTS.items():
        path = repo_root / relative
        if not path.is_file():
            raise RuntimeError(f"required oracle missing: {relative}")
        actual = _sha256_bytes(path.read_bytes())
        if actual != expected:
            raise RuntimeError(
                f"official oracle digest mismatch:{relative}:expected={expected}:actual={actual}"
            )
        observed_oracles[relative] = actual

    transport_path = repo_root / TRANSPORT_RELATIVE_PATH
    if not transport_path.is_file():
        raise RuntimeError(f"authoritative transport missing: {TRANSPORT_RELATIVE_PATH}")
    transport_bytes = transport_path.read_bytes()
    transport_text = transport_bytes.decode("utf-8")
    required_source_markers = (
        "writer.write(payload)",
        "invalid_server_message_delimiter",
        "take_client_action",
        "idle_flush_sec",
    )
    missing_markers = [
        marker for marker in required_source_markers if marker not in transport_text
    ]
    if missing_markers:
        raise RuntimeError(
            "authoritative transport lost delimiter-free markers: "
            + ",".join(missing_markers)
        )
    behavior = await _transport_behavior_evidence()
    if not all(
        behavior.get(key)
        for key in ("raw_payload_exact", "rejects_lf", "rejects_cr")
    ):
        raise RuntimeError(f"delimiter-free transport behavior failed: {behavior}")
    return {
        "official_oracle_sha256": observed_oracles,
        "transport": {
            "path": TRANSPORT_RELATIVE_PATH,
            "sha256": _sha256_bytes(transport_bytes),
            "source_markers": list(required_source_markers),
            **behavior,
        },
    }


def build_probe_prompt(repo_root: Path, evidence: dict[str, Any]) -> str:
    absolute_reads = [
        str((repo_root / relative).resolve()) for relative in READ_RELATIVE_PATHS
    ]
    expected_oracles = evidence["official_oracle_sha256"]
    transport_sha = evidence["transport"]["sha256"]
    return f"""# Read-only Claude Agent SDK capability probe

This is an operator-owned capability test, not a coding task. Do not edit,
create, delete, rename, or download anything. Do not invoke Web, curl, wget,
nc, ssh, package managers, network tools, MCP tools, or subagents.

Use the SDK tools in this order, as separate tool calls:
1. Read `{absolute_reads[0]}`.
2. Read `{absolute_reads[1]}`.
3. Read `{absolute_reads[2]}`.
4. Bash exactly: `{HASH_COMMAND}`
5. Bash exactly: `{TRANSPORT_SCAN_COMMAND}`

The Bash runtime enforces those two complete commands; variants are denied.
After all five tool results are visible, return exactly one JSON object (a
markdown JSON fence is acceptable) with this shape and these evidence values:

{{
  "status": "pass",
  "oracle_sha256": {json.dumps(expected_oracles, sort_keys=True)},
  "transport": {{
    "path": "{TRANSPORT_RELATIVE_PATH}",
    "sha256": "{transport_sha}",
    "delimiter_free_send": true,
    "rejects_crlf": true,
    "stream_framing": true
  }},
  "tool_calls_completed": 5
}}

Do not claim pass until you have actually received every Read and Bash result.
"""


def _resolve_read_path(repo_root: Path, raw_path: object) -> str:
    path = Path(str(raw_path or ""))
    if not path.is_absolute():
        path = repo_root / path
    return str(path.resolve(strict=False))


def validate_tool_trace(
    trace: list[dict[str, Any]],
    repo_root: Path,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    uses = [event for event in trace if event.get("event") == "tool_use"]
    results = [event for event in trace if event.get("event") == "tool_result"]
    if len(uses) < 5:
        raise RuntimeError(f"insufficient tool calls: observed={len(uses)} required>=5")

    unexpected = [
        event.get("tool_name") for event in uses
        if event.get("tool_name") not in SDK_TOOLS
    ]
    if unexpected:
        raise RuntimeError(f"unexpected tools executed: {unexpected}")

    ids = [str(event.get("tool_use_id") or "") for event in uses]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise RuntimeError("tool-use IDs are missing or duplicated")
    result_ids = [str(event.get("tool_use_id") or "") for event in results]
    if any(not value for value in result_ids):
        raise RuntimeError("tool result missing tool_use_id")
    if any(event.get("is_error") for event in results):
        raise RuntimeError("one or more SDK tool results reported is_error")
    if set(result_ids) - set(ids):
        raise RuntimeError("tool result references an unknown tool-use ID")
    missing_results = [tool_id for tool_id in ids if tool_id not in result_ids]
    if missing_results:
        raise RuntimeError(f"tool calls missing results: {missing_results}")

    expected_reads = {
        str((repo_root / relative).resolve()) for relative in READ_RELATIVE_PATHS
    }
    observed_reads = {
        _resolve_read_path(repo_root, event.get("tool_input", {}).get("file_path"))
        for event in uses
        if event.get("tool_name") == "Read"
    }
    if not expected_reads.issubset(observed_reads):
        raise RuntimeError(
            f"required Read calls missing: {sorted(expected_reads - observed_reads)}"
        )

    bash_uses = [event for event in uses if event.get("tool_name") == "Bash"]
    observed_commands = [
        str(event.get("tool_input", {}).get("command", "")).strip()
        for event in bash_uses
    ]
    if any(command not in EXACT_BASH_COMMANDS for command in observed_commands):
        raise RuntimeError("Bash trace contains a command outside the exact allowlist")
    if not set(EXACT_BASH_COMMANDS).issubset(observed_commands):
        raise RuntimeError("required Bash commands were not both executed")

    results_by_id: dict[str, list[str]] = {}
    for event in results:
        results_by_id.setdefault(str(event["tool_use_id"]), []).append(
            str(event.get("content_preview") or "")
        )
    bash_output = {
        str(event.get("tool_input", {}).get("command", "")).strip(): "\n".join(
            results_by_id.get(str(event.get("tool_use_id")), [])
        )
        for event in bash_uses
    }
    hash_output = bash_output.get(HASH_COMMAND, "")
    required_digests = list(evidence["official_oracle_sha256"].values()) + [
        evidence["transport"]["sha256"]
    ]
    if any(digest not in hash_output for digest in required_digests):
        raise RuntimeError("sha256sum tool result does not contain every observed digest")
    scan_output = bash_output.get(TRANSPORT_SCAN_COMMAND, "")
    if any(
        marker not in scan_output
        for marker in (
            "writer.write(payload)",
            "invalid_server_message_delimiter",
            "take_client_action",
            "idle_flush_sec",
        )
    ):
        raise RuntimeError("transport scan result is missing delimiter-free markers")

    return {
        "tool_use_count": len(uses),
        "tool_result_count": len(results),
        "read_count": sum(event.get("tool_name") == "Read" for event in uses),
        "bash_count": len(bash_uses),
        "all_tool_uses_have_results": True,
    }


def validate_model_response(output: str, evidence: dict[str, Any]) -> dict[str, Any]:
    from llm_query import parse_json_output

    parsed = parse_json_output(output)
    if not isinstance(parsed, dict):
        raise RuntimeError("model did not return a JSON object")
    if parsed.get("status") != "pass":
        raise RuntimeError("model response status is not pass")
    if parsed.get("oracle_sha256") != evidence["official_oracle_sha256"]:
        raise RuntimeError("model returned incorrect official oracle SHA-256 values")
    transport = parsed.get("transport")
    if not isinstance(transport, dict):
        raise RuntimeError("model response transport evidence is missing")
    expected_transport = {
        "path": TRANSPORT_RELATIVE_PATH,
        "sha256": evidence["transport"]["sha256"],
        "delimiter_free_send": True,
        "rejects_crlf": True,
        "stream_framing": True,
    }
    if transport != expected_transport:
        raise RuntimeError("model returned incorrect delimiter-free transport evidence")
    try:
        call_count = int(parsed.get("tool_calls_completed"))
    except (TypeError, ValueError):
        call_count = 0
    if call_count < 5:
        raise RuntimeError("model did not acknowledge the required multi-tool loop")
    return parsed


def _failure_payload(category: str, exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "category": category,
        "exception_type": type(exc).__name__,
        "message": str(exc)[:2000],
    }
    issue = getattr(exc, "issue", None)
    if issue is not None and callable(getattr(issue, "as_dict", None)):
        payload["availability_issue"] = _json_safe(issue.as_dict())
    return payload


async def run_operator_probe(
    *,
    repo_root: Path,
    timeout_seconds: float = 300.0,
    model: str = "sonnet",
    query_runner: Callable[..., Awaitable[tuple[str, Any, Any]]] | None = None,
) -> dict[str, Any]:
    """Run the probe and always return one JSON-serialisable fail-closed receipt."""

    started_monotonic = time.monotonic()
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "probe_id": uuid.uuid4().hex,
        "started_at": _utc_now(),
        "status": "fail",
        "repo_root": str(repo_root.resolve()),
        "git_head": _git_head(repo_root),
        "role": ROLE_NAME,
        "model": str(model),
        "timeout_seconds": float(timeout_seconds),
        "sdk_contract": {
            "query_primitive": "web/core/llm_query.py:run_claude_query",
            "tools": list(SDK_TOOLS),
            "mcp_servers": {},
            "strict_mcp_config": True,
            "repo_write_tools_exposed": False,
            "exact_bash_commands": list(EXACT_BASH_COMMANDS),
        },
        "tool_trace": [],
    }

    try:
        before = await collect_local_evidence(repo_root)
        receipt["local_evidence"] = before
    except Exception as exc:
        receipt["failure"] = _failure_payload("local_evidence_invalid", exc)
        receipt["finished_at"] = _utc_now()
        receipt["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 3)
        return receipt

    try:
        if query_runner is None:
            from llm_query import run_claude_query as query_runner
        from llm_query import capture_llm_tool_trace

        prompt = build_probe_prompt(repo_root.resolve(), before)
        temp_base = Path(tempfile.gettempdir()).resolve()
        try:
            temp_base.relative_to(repo_root.resolve())
        except ValueError:
            pass
        else:
            raise RuntimeError(
                "system temporary directory is inside the repository; refusing probe"
            )
        with tempfile.TemporaryDirectory(
            prefix="pok-claude-sdk-probe-",
            dir=str(temp_base),
        ) as temp_dir:
            log_path = Path(temp_dir) / "operator_sdk_probe_io.txt"
            with capture_llm_tool_trace() as trace:
                output, cost_usd, usage = await asyncio.wait_for(
                    query_runner(
                        prompt,
                        [],
                        None,
                        ROLE_NAME,
                        str(log_path),
                        model=model,
                        tools=list(SDK_TOOLS),
                        allowed_write_dir=None,
                        exact_bash_commands=EXACT_BASH_COMMANDS,
                    ),
                    timeout=max(0.001, float(timeout_seconds)),
                )
            receipt["tool_trace"] = _json_safe(trace)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        receipt["tool_trace"] = _json_safe(locals().get("trace", []))
        receipt["failure"] = _failure_payload("timeout", exc)
        receipt["finished_at"] = _utc_now()
        receipt["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 3)
        return receipt
    except Exception as exc:
        receipt["tool_trace"] = _json_safe(locals().get("trace", []))
        category = (
            "provider_availability"
            if type(exc).__name__ == "LLMAvailabilityBlocked" or hasattr(exc, "issue")
            else "sdk_failure"
        )
        receipt["failure"] = _failure_payload(category, exc)
        receipt["finished_at"] = _utc_now()
        receipt["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 3)
        return receipt

    receipt["sdk_result"] = {
        "output_sha256": _sha256_bytes(str(output).encode("utf-8")),
        "output_chars": len(str(output)),
        "output_preview": str(output)[:2000],
        "cost_usd": cost_usd,
        "usage": _json_safe(usage),
    }
    try:
        after = await collect_local_evidence(repo_root)
        if after != before:
            raise RuntimeError("critical probe inputs changed during the SDK call")
        trace_summary = validate_tool_trace(receipt["tool_trace"], repo_root, before)
        parsed = validate_model_response(str(output), before)
    except Exception as exc:
        receipt["failure"] = _failure_payload("evidence_validation", exc)
    else:
        receipt["status"] = "pass"
        receipt["trace_summary"] = trace_summary
        receipt["model_evidence"] = parsed
        receipt.pop("failure", None)

    receipt["finished_at"] = _utc_now()
    receipt["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 3)
    return receipt
