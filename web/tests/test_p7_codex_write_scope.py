"""P7 fixes (2026-10-04 audit): codex transport write scope + misclassification.

Root cause under test (special-investigation confirmed, v494/v500/v509):
the codex transport hard-coded ``-s read-only`` for every process, so Workers
produced contract-compliant patches whose every ``apply_patch`` was rejected by
the sandbox, while the zero-change detector compared only lease-snapshot bytes
and classified "wrote-but-rejected" as "model made zero changes", burning all
4 retries on educating the model. Crossover degraded the same way, silently
publishing a byte-identical parent-A copy.

Covers:

- **P7-1** — ``allowed_write_dir`` flows from the dispatch into
  :class:`CodexExecTransport`; ``build_argv`` selects ``-s workspace-write`` +
  ``sandbox_workspace_write.writable_roots`` only when the role declared
  Edit/Write tools AND a resolvable write scope exists; otherwise the argv
  stays exactly ``-s read-only`` (byte-identical to the pre-P7 shape for
  read-only roles).
- **P7-2** — a zero-change Worker attempt whose output/io log carries sandbox
  write-rejection signatures is classified ``llm_infrastructure`` (raises
  :class:`WorkerInfrastructureError`, no model-retry burn); a genuinely lazy
  zero-change attempt keeps the historical ``zero_changes`` retry contract.
- **P7-3** — under ``POK_LLM_TRANSPORT=codex``, a write-capable role with a
  declared-but-unresolvable write root fails fast at dispatch with an
  actionable error + ``pipeline.codex_write_scope_unresolvable`` system event,
  never a silent read-only degradation, and never a 1302/1308-style pause.
- **P7-4** — the worker verification template must say ``python3 -m
  py_compile`` (the codex sandbox has no ``python``; exit 127 in
  results/v509/logs/worker_1_io.txt:564).
- **P7-5** — an accepted crossover whose tree is byte-identical to parent A
  emits ``pipeline.crossover_degraded_to_parent_copy`` carrying the sandbox
  rejection markers found in the crossover io log.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions

import llm_query
import llm_query_codex as lcx


def llm_query_codex_module():
    import llm_query_retry

    return llm_query_retry._cx


def _real(path) -> str:
    return os.path.realpath(str(path))


# ---------------------------------------------------------------------------
# P7-1: build_argv selects workspace-write only for declared write scopes
# ---------------------------------------------------------------------------


def _write_options():
    return ClaudeAgentOptions(tools=["Bash", "Read", "Edit"])


def _readonly_options():
    return ClaudeAgentOptions(tools=["Bash", "Read"])


def test_build_argv_workspace_write_with_declared_write_dir(tmp_path):
    argv = lcx.CodexExecTransport(
        "p", _write_options(), allowed_write_dir={"dirs": [tmp_path]}
    ).build_argv()
    assert argv[argv.index("-s") + 1] == "workspace-write"
    overrides = [a for a in argv if a.startswith("sandbox_workspace_write.")]
    assert len(overrides) == 1
    key, _, value = overrides[0].partition("=")
    assert key == "sandbox_workspace_write.writable_roots"
    assert json.loads(value) == [_real(tmp_path)]


def test_build_argv_workspace_write_with_file_scope_uses_parent_dir(tmp_path):
    argv = lcx.CodexExecTransport(
        "p",
        _write_options(),
        allowed_write_dir={"files": [tmp_path / "policy.py"]},
    ).build_argv()
    assert argv[argv.index("-s") + 1] == "workspace-write"
    value = [
        a.partition("=")[2]
        for a in argv
        if a.startswith("sandbox_workspace_write.writable_roots=")
    ][0]
    assert json.loads(value) == [_real(tmp_path)]


def test_build_argv_stays_read_only_without_write_tools_or_scope(tmp_path):
    # Read-only role tools: unchanged read-only argv even with a scope.
    argv = lcx.CodexExecTransport(
        "p", _readonly_options(), allowed_write_dir={"dirs": [tmp_path]}
    ).build_argv()
    assert argv[argv.index("-s") + 1] == "read-only"
    assert not [a for a in argv if a.startswith("sandbox_workspace_write.")]

    # Write-capable tools but NO declared scope: read-only (the claude path
    # installs a read-only mutation guard for exactly this shape).
    argv = lcx.CodexExecTransport("p", _write_options()).build_argv()
    assert argv[argv.index("-s") + 1] == "read-only"
    assert not [a for a in argv if a.startswith("sandbox_workspace_write.")]

    # Empty normalized scope is equivalent to no scope.
    argv = lcx.CodexExecTransport(
        "p", _write_options(), allowed_write_dir={"dirs": [], "files": []}
    ).build_argv()
    assert argv[argv.index("-s") + 1] == "read-only"


def test_build_argv_declared_but_unresolvable_scope_fails_closed(tmp_path):
    """A write-capable role with a declared scope that resolves to zero
    existing directory roots must fail closed instead of silently degrading
    to read-only (the exact v509 failure mode)."""
    missing = tmp_path / "missing" / "lease"
    with pytest.raises(RuntimeError) as excinfo:
        lcx.CodexExecTransport(
            "p", _write_options(), allowed_write_dir={"dirs": [missing]}
        ).build_argv()
    text = str(excinfo.value)
    assert "codex" in text.lower()
    assert "write" in text.lower()
    # Actionable config error, never an LLM availability pause.
    from llm_availability import classify_llm_availability

    assert classify_llm_availability(exception=excinfo.value) is None


def test_codex_writable_roots_resolver_contract(tmp_path):
    assert lcx.codex_writable_roots({"dirs": [tmp_path]}, tools=["Edit"]) == [
        _real(tmp_path)
    ]
    # No write tools -> no roots, regardless of scope.
    assert lcx.codex_writable_roots({"dirs": [tmp_path]}, tools=["Bash"]) == []
    # tools omitted -> the scope alone does not grant roots (fail closed).
    assert lcx.codex_writable_roots({"dirs": [tmp_path]}) == []
    # Scalar path form is accepted (callers may pass str/Path).
    assert lcx.codex_writable_roots(str(tmp_path), tools=["Edit", "Write"]) == [
        _real(tmp_path)
    ]
    # Nonexistent dir is filtered out.
    assert (
        lcx.codex_writable_roots(
            {"dirs": [tmp_path / "nope"]}, tools=["Edit"]
        )
        == []
    )


def test_retry_loop_forwards_allowed_write_dir_to_codex_transport(
    monkeypatch, tmp_path
):
    """P7-1 dispatch seam: the retry-loop chokepoint passes the normalized
    allowed_write_dir into new_codex_exec_transport under the codex
    transport (the claude branch keeps its hooks-based write guard)."""
    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    captured = {}

    def fake_new_transport(full_prompt, options, allowed_write_dir=None):
        captured["scope"] = allowed_write_dir
        captured["options"] = options
        return lcx.CodexExecTransport(full_prompt, options)

    def fake_codex_query(prompt=None, *, options=None, transport=None):
        async def _gen():
            yield 1

        return _gen()

    async def fake_process_stream(query_gen, log_file_path, ui, role_name):
        return (["ok"], 0.0, {}, {})

    monkeypatch.setattr(
        llm_query_codex_module(), "new_codex_exec_transport", fake_new_transport
    )
    monkeypatch.setattr(
        llm_query_codex_module(), "codex_query", fake_codex_query
    )
    monkeypatch.setattr(llm_query, "_process_stream", fake_process_stream)

    scope = {"dirs": [tmp_path]}
    texts, _cost, _usage = asyncio.run(
        llm_query._run_stream_with_signature_retry(
            "prompt",
            ClaudeAgentOptions(tools=["Bash", "Read", "Edit"]),
            "/tmp/none.log",
            None,
            "role",
            allowed_write_dir=scope,
        )
    )
    assert texts == ["ok"]
    assert captured["scope"] == scope


# ---------------------------------------------------------------------------
# P7-3: dispatch-level fail-fast for unresolvable codex write roots
# ---------------------------------------------------------------------------


def _capture_system_events(monkeypatch):
    events = []

    import system_log

    def _capture(event_type, severity, message, data=None, **extra):
        events.append((event_type, severity, message, {**(data or {}), **extra}))

    monkeypatch.setattr(system_log, "log_system_event", _capture)
    return events


def test_codex_write_scope_assert_fails_fast_on_unresolvable_root(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    events = _capture_system_events(monkeypatch)

    missing = tmp_path / "lease" / "workspace"
    with pytest.raises(RuntimeError) as excinfo:
        llm_query._assert_codex_write_scope_ready(
            "WORKER 1 (Opponent Modeler)",
            ["Bash", "Read", "Edit"],
            {"dirs": [missing], "files": [missing / "policy.py"]},
        )
    text = str(excinfo.value)
    assert "workspace-write" in text or "writable_roots" in text
    assert str(missing) in text
    # The operator-visible event names the role, scope, and the fix.
    assert events, "fail-fast must log a system event"
    category, severity, message, fields = events[0]
    assert category == "pipeline.codex_write_scope_unresolvable"
    assert severity == "error"
    assert fields.get("role") == "WORKER 1 (Opponent Modeler)"
    assert fields.get("transport") == "codex"
    # Never an availability pause.
    from llm_availability import classify_llm_availability

    assert classify_llm_availability(exception=excinfo.value) is None


def test_codex_write_scope_assert_passes_for_valid_shapes(
    monkeypatch, tmp_path
):
    events = _capture_system_events(monkeypatch)

    # claude transport: never checked at all.
    monkeypatch.delenv("POK_LLM_TRANSPORT", raising=False)
    llm_query._assert_codex_write_scope_ready(
        "WORKER 1", ["Bash", "Read", "Edit"], {"dirs": [tmp_path / "missing"]}
    )
    assert events == []

    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    # Resolvable root: passes.
    llm_query._assert_codex_write_scope_ready(
        "WORKER 1", ["Bash", "Read", "Edit"], {"dirs": [tmp_path]}
    )
    # Read-only role tools: a declared scope is not required to resolve.
    llm_query._assert_codex_write_scope_ready(
        "MASTER", ["Read"], {"dirs": [tmp_path / "missing"]}
    )
    # Write tools without a declared scope: the claude path read-only guard
    # shape — read-only sandbox is correct, not a failure.
    llm_query._assert_codex_write_scope_ready("ROLE", ["Bash", "Read", "Edit"], None)
    assert events == []


def test_run_claude_query_invokes_codex_write_scope_preflight(
    monkeypatch, tmp_path
):
    """Wiring: run_claude_query calls the preflight before dispatch (proved
    with the zero-tool CYCLE ARCHIVIST harness, where the check trivially
    passes but must be invoked with the role's tools)."""
    import cycle_archivist
    from bot_namespace import bot_name, bot_tag

    class _UI:
        def log_history(self, *_a, **_k):
            return None

        def log_io(self, *_a, **_k):
            return None

        def emit_tool_call(self, *_a, **_k):
            return None

        def update_cost(self, *_a, **_k):
            return None

    calls = []

    async def fake_stream(
        full_prompt, options, log_file_path, ui, role_name, **kwargs
    ):
        return ["ok"], 0.0, {}

    monkeypatch.setattr(
        llm_query, "_run_stream_with_signature_retry", fake_stream
    )
    monkeypatch.setattr(
        llm_query,
        "_assert_codex_write_scope_ready",
        lambda role_name, tools, allowed_write_dir, **kwargs: calls.append(
            (role_name, list(tools or []), allowed_write_dir)
        ),
    )
    import orchestrator_cost_policy
    import llm_availability_store
    import rate_limiter

    monkeypatch.setattr(
        orchestrator_cost_policy,
        "assert_operator_cost_limit_available",
        lambda: None,
    )
    monkeypatch.setattr(
        llm_availability_store, "raise_if_llm_paused", lambda **_k: None
    )
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)

    snapshot = cycle_archivist._cycle_archivist_prompt_projection(
        {
            "evaluation_epoch": "national_tcp_policy_v1",
            "bot_name": bot_name(149),
            "git_tag": bot_tag(149),
            "publication_identity": {
                "publication_id": "1" * 64,
                "commit_oid": "2" * 40,
                "candidate_artifact_hash": "3" * 64,
            },
            "strength_evidence_identity": {"marker": "p7 preflight wiring"},
            "review_score": 9,
            "critic_score": 8,
            "precommit_passed": True,
            "post_publication_handoff": {
                "identity_digest": "4" * 64,
                "publication_id": "1" * 64,
            },
        },
        version=149,
        source_v=143,
    )
    rendered_prompt = llm_query.render_llm_prompt(
        "CYCLE ARCHIVIST",
        producer=cycle_archivist._render_cycle_archivist_provider_prompt,
        renderer_inputs={"snapshot": snapshot, "version": 149, "source_v": 143},
    )
    output, _cost, _usage = asyncio.run(
        llm_query.run_claude_query(
            rendered_prompt,
            [],
            _UI(),
            "CYCLE ARCHIVIST",
            tmp_path / "p7_io.txt",
        )
    )
    assert output == "ok"
    assert calls and calls[0][0] == "CYCLE ARCHIVIST"
    assert calls[0][1] == []


# ---------------------------------------------------------------------------
# P7-2: sandbox write-rejection signatures vs genuine zero-change laziness
# ---------------------------------------------------------------------------


SANDBOX_REJECTION_IO = """[509#0] The edit target is enforced read-only by the execution sandbox.
[509#0] [TOOL_RESULT source=ToolResultBlock is_error=True] {"type": "command_execution", "command": "apply_patch", "exit_code": 1}
[509#0] BLOCKED: apply_patch rejected both absolute and workspace-relative writes with: `writing is blocked by read-only sandbox`.
"""


# ---------------------------------------------------------------------------
# B1 (review block, 2026-10-04): the sandbox writable surface must converge to
# the declared write scope — spawn cwd pinned to the primary write root
# ---------------------------------------------------------------------------


def test_spawn_cwd_converges_to_primary_write_root(monkeypatch, tmp_path):
    """Review-confirmed defect: codex workspace-write makes the ENTIRE cwd
    tree writable (writable_roots is ADDITIVE, not a replacement — proven by
    the reviewer's live CLI run writing inside cwd but outside the declared
    roots). With cwd at the repository root the whole service checkout
    (web/core contracts, .git, gitignored pipeline state) entered the
    sandbox write surface while the claude-path PreToolUse write guard does
    not apply to codex. The fix: for a write-role dispatch the spawn cwd is
    the FIRST resolved write root (the lease workspace), so the writable
    surface collapses to the declared scope union plus the codex-built-in
    system temp tree."""
    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: "/opt/codex/bin/codex")

    lease = tmp_path / "lease"
    second_root = tmp_path / "second_root"
    lease.mkdir()
    second_root.mkdir()
    captured = {}

    class _Proc:
        pid = 424245
        returncode = None

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(lcx.asyncio, "create_subprocess_exec", fake_exec)

    transport = lcx.CodexExecTransport(
        "p",
        ClaudeAgentOptions(tools=["Bash", "Read", "Edit"]),
        allowed_write_dir={"dirs": [lease, second_root]},
    )
    asyncio.run(transport.spawn())
    assert captured["cwd"] == _real(lease), (
        "spawn cwd must be the primary declared write root, not the repo "
        "working tree (B1: cwd tree is sandbox-writable under "
        "workspace-write)"
    )
    # Both declared roots stay in the writable_roots override (the secondary
    # root is outside the cwd workspace and needs the explicit grant).
    value = [
        a.partition("=")[2]
        for a in captured["argv"]
        if a.startswith("sandbox_workspace_write.writable_roots=")
    ][0]
    assert json.loads(value) == [_real(lease), _real(second_root)]


def test_spawn_cwd_stays_project_root_without_write_scope(monkeypatch, tmp_path):
    """Guard: read-only dispatches keep the historical project-root cwd —
    read-only never writes anywhere, and the repo-root cwd preserves codex's
    AGENTS.md discovery exactly as before B1."""
    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: "/opt/codex/bin/codex")

    captured = {}

    class _Proc:
        pid = 424246
        returncode = None

    async def fake_exec(*argv, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(lcx.asyncio, "create_subprocess_exec", fake_exec)

    options = ClaudeAgentOptions(tools=["Bash", "Read", "Edit"])
    transport = lcx.CodexExecTransport("p", options)
    asyncio.run(transport.spawn())
    assert captured["cwd"] == lcx._project_root(options)

    read_only_role = lcx.CodexExecTransport(
        "p", options, allowed_write_dir={"dirs": [tmp_path]}
    )
    # tools without Edit/Write: read-only argv AND project-root cwd
    read_only_role = lcx.CodexExecTransport(
        "p", ClaudeAgentOptions(tools=["Bash", "Read"]), allowed_write_dir={"dirs": [tmp_path]}
    )
    asyncio.run(read_only_role.spawn())
    assert captured["cwd"] == lcx._project_root(read_only_role._options)


def test_writable_roots_relative_paths_resolve_against_base_dir(tmp_path):
    """O2 (review note): a bare relative file name must resolve against the
    dispatch base dir, never widen to the process cwd via the former
    ``dirname(...) or "."`` fallback."""
    roots = lcx.codex_writable_roots(
        {"files": ["policy.py"]}, tools=["Edit"], base_dir=str(tmp_path)
    )
    assert roots == [_real(tmp_path)]
    # Relative directory form resolves against base_dir as well.
    (tmp_path / "nested").mkdir()
    assert lcx.codex_writable_roots(
        {"dirs": ["nested"]}, tools=["Edit"], base_dir=str(tmp_path)
    ) == [_real(tmp_path / "nested")]


def test_worker_rejection_scan_reads_only_this_attempt_increment(tmp_path):
    """O1 (review note): the io log accumulates across attempts; the scan
    must read only the increment written since this attempt began, so a
    PREVIOUS attempt's rejection text cannot misclassify a genuinely lazy
    follow-up attempt as infrastructure."""
    from agent_workers import _worker_sandbox_rejection_hits

    io = tmp_path / "worker_1_io.txt"
    io.write_text(SANDBOX_REJECTION_IO, encoding="utf-8")
    stale_size = io.stat().st_size

    # Nothing appended during this attempt: the stale prefix must NOT match.
    assert (
        _worker_sandbox_rejection_hits(
            "I read the file and decided not to change it.", io, since_offset=stale_size
        )
        == []
    )
    # Fresh rejection text appended during this attempt DOES match.
    with io.open("a", encoding="utf-8") as handle:
        handle.write(
            "[509#1] apply_patch failed: writing is blocked by read-only sandbox\n"
        )
    hits = _worker_sandbox_rejection_hits("BLOCKED", io, since_offset=stale_size)
    assert hits == ["read-only sandbox"]
    # Default offset 0 keeps the whole-tail behaviour for other callers.
    assert _worker_sandbox_rejection_hits("", io)


def _lease_tree(tmp_path):
    lease = tmp_path / "lease_workspace"
    lease.mkdir()
    (lease / "policy.py").write_text(
        "def decide(_context):\n    return {'kind': 'pass'}\n",
        encoding="utf-8",
    )
    return lease


def _patched_query(calls, output):
    from unittest.mock import patch

    async def fake_query(prompt, *args, **kwargs):
        calls.append(prompt)
        return output, 0.0, {}

    return patch("agent_workers.run_claude_query", side_effect=fake_query)


def _patched_query_writing_io(calls, output, io_file, io_text):
    """Fake provider call that appends tool-error text to the role io log
    DURING the attempt — the real timing (_process_stream logs rejected
    tool results while the stream runs, i.e. after the attempt's offset was
    recorded, before the zero-change check reads it back)."""

    from unittest.mock import patch

    async def fake_query(prompt, *args, **kwargs):
        calls.append(prompt)
        with open(io_file, "a", encoding="utf-8") as handle:
            handle.write(io_text)
        return output, 0.0, {}

    return patch("agent_workers.run_claude_query", side_effect=fake_query)


def _null_ui():
    class _UI:
        def log_history(self, *_a, **_k):
            return None

        def log_io(self, *_a, **_k):
            return None

        def set_status(self, *_a, **_k):
            return None

        def clear_io(self):
            return None

    return _UI()


def _worker_task():
    return {
        "worker_id": 1,
        "role": "Opponent Modeler",
        "target_files": ["policy.py"],
        "worker_prompt": "Edit the opponent-model term in policy.py.",
        "task_kind": "feature_work",
    }


def test_zero_change_worker_with_sandbox_rejection_is_infrastructure(
    tmp_path,
):
    """The exact v509 shape: model produced the full patch, the sandbox
    rejected every write, the lease bytes did not move. This is
    llm_infrastructure (WorkerInfrastructureError), NOT zero_changes — and
    the retry loop must not burn the remaining model attempts."""
    from agent_workers import WorkerInfrastructureError, _run_single_worker

    lease = _lease_tree(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    io_file = logs_dir / "worker_1_io.txt"
    io_file.write_text("[509#0] attempt starts\n", encoding="utf-8")

    calls = []
    blocked_output = (
        "**BLOCKED**\n- `apply_patch` attempts failed (`read-only sandbox`)."
        "\n- changed_files: none"
    )
    events = []
    import system_log
    from unittest.mock import patch

    def _capture(event_type, severity, message, data=None, **extra):
        events.append((event_type, severity, {**(data or {}), **extra}))

    with (
        _patched_query_writing_io(calls, blocked_output, io_file, SANDBOX_REJECTION_IO),
        patch("agent_workers.get_logs_dir", return_value=logs_dir),
        patch.object(system_log, "log_system_event", _capture),
    ):
        with pytest.raises(WorkerInfrastructureError) as excinfo:
            asyncio.run(
                _run_single_worker(
                    _worker_task(),
                    0,
                    "worker_prompt.md",
                    lease,
                    509,
                    [],
                    _null_ui(),
                    "",
                    source_v=508,
                )
            )
    assert "sandbox" in str(excinfo.value).lower()
    # Exactly ONE provider call: no model-retry burn on an infra failure.
    assert len(calls) == 1
    # Operator-visible event fired.
    assert any(
        category.startswith("pipeline.worker_writes_sandbox_rejected")
        for category, _severity, _fields in events
    )


def test_stale_rejection_text_from_earlier_io_does_not_reclassify_laziness(
    tmp_path,
):
    """O1 end-to-end: rejection text that predates THIS attempt (written
    before the attempt began, e.g. by an earlier different failure path)
    must not flip a genuinely lazy zero-change attempt into
    llm_infrastructure."""
    from agent_workers import _run_single_worker

    lease = _lease_tree(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    # Stale prefix: an old rejection transcript left in the io log.
    (logs_dir / "worker_1_io.txt").write_text(SANDBOX_REJECTION_IO, encoding="utf-8")

    calls = []
    recorded = []
    from unittest.mock import patch

    with (
        _patched_query(calls, "I read the file and decided not to change it."),
        patch("agent_workers.get_logs_dir", return_value=logs_dir),
        patch(
            "agent_workers._record_worker_failure",
            side_effect=lambda *args, **kwargs: recorded.append((args, kwargs)),
        ),
    ):
        result = asyncio.run(
            _run_single_worker(
                _worker_task(),
                0,
                "worker_prompt.md",
                lease,
                509,
                [],
                _null_ui(),
                "",
                source_v=508,
            )
        )
    assert result is False
    assert recorded[0][1].get("failure_type") == "zero_changes"
    from evolution_infra import MAX_WORKER_RETRIES

    assert len(calls) == MAX_WORKER_RETRIES


def test_zero_change_worker_without_signatures_keeps_retry_contract(tmp_path):
    """Genuine laziness (no sandbox rejection markers) must keep the
    historical zero_changes handling: retries are used and the failure is
    recorded as a real implementation failure (returns False)."""
    from agent_workers import _run_single_worker

    class _UI:
        def log_history(self, *_a, **_k):
            return None

        def log_io(self, *_a, **_k):
            return None

        def set_status(self, *_a, **_k):
            return None

        def clear_io(self):
            return None

    lease = _lease_tree(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "worker_1_io.txt").write_text(
        "[509#0] Analyzed the file; the change is not needed.\n", encoding="utf-8"
    )

    calls = []
    recorded = []
    from unittest.mock import patch

    with (
        _patched_query(calls, "I read the file and decided not to change it."),
        patch("agent_workers.get_logs_dir", return_value=logs_dir),
        patch(
            "agent_workers._record_worker_failure",
            side_effect=lambda *args, **kwargs: recorded.append((args, kwargs)),
        ),
    ):
        result = asyncio.run(
            _run_single_worker(
                task := {
                    "worker_id": 1,
                    "role": "Tuner",
                    "target_files": ["policy.py"],
                    "worker_prompt": "Edit the sizing table in policy.py.",
                    "task_kind": "feature_work",
                },
                0,
                "worker_prompt.md",
                lease,
                509,
                [],
                _UI(),
                "",
                source_v=508,
            )
        )
    assert result is False
    assert recorded, "genuine zero-change must still be recorded as a failure"
    assert recorded[0][1].get("failure_type") == "zero_changes"
    # The full retry budget was used before giving up (4 attempts).
    from evolution_infra import MAX_WORKER_RETRIES

    assert len(calls) == MAX_WORKER_RETRIES


def test_sandbox_rejection_scanner_markers():
    from worker_boundary import scan_sandbox_write_rejections

    # All four signatures observed in the v509 io evidence.
    assert scan_sandbox_write_rejections("writing is blocked by read-only sandbox")
    assert scan_sandbox_write_rejections("OSError: [Errno 30] Read-only file system")
    assert scan_sandbox_write_rejections("patch rejected by user approval settings")
    # Case-insensitive on the errno/file-system phrasing.
    assert scan_sandbox_write_rejections("touch: cannot touch 'policy.py': Read-only file system")
    # No false positive on benign output.
    assert scan_sandbox_write_rejections("changed_files: policy.py (modified)") == []
    assert scan_sandbox_write_rejections("") == []


# ---------------------------------------------------------------------------
# P7-4: worker verification template must use python3 (sandbox has no python)
# ---------------------------------------------------------------------------


def test_worker_profile_verification_uses_python3():
    template = (
        Path(__file__).resolve().parents[1]
        / "core"
        / "prompts"
        / "worker_profile_national_native.md"
    ).read_text(encoding="utf-8")
    assert "python3 -m py_compile" in template
    assert "python -m py_compile" not in template


def test_all_worker_visible_compile_instructions_use_python3():
    """Every prompt surface that teaches a Worker to compile must say
    python3: the runtime sandbox (codex read-only/workspace-write) exposes
    only python3, and the bare `python` form died with exit 127 in the v509
    io evidence."""
    core = Path(__file__).resolve().parents[1] / "core"
    template = (core / "prompts" / "worker_prompt.md").read_text(encoding="utf-8")
    assert "python3 -m py_compile" in template
    assert "python -m py_compile" not in template
    for source_name in (
        "tool_planning_quality_contracts.py",
        "tool_planning_quality_repair_targets.py",
        "tool_planning_quality_rework.py",
    ):
        source = (core / source_name).read_text(encoding="utf-8")
        assert "python -m py_compile" not in source, source_name


# ---------------------------------------------------------------------------
# P7-5: crossover silent parent-copy degradation is evented
# ---------------------------------------------------------------------------


def test_crossover_parent_copy_degradation_event_carries_sandbox_markers(
    monkeypatch, tmp_path
):
    import agent_review

    events = _capture_system_events(monkeypatch)

    parent = tmp_path / "parent_a"
    parent.mkdir()
    (parent / "policy.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    (target / "policy.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    io_log = tmp_path / "crossover_io.txt"
    io_log.write_text(
        "Target remains byte-identical to Parent A. apply_patch rejected writes "
        "with: `writing is blocked by read-only sandbox`.\n",
        encoding="utf-8",
    )

    agent_review._log_crossover_parent_copy_degradation(
        target_dir=target,
        frozen_parent_a_dir=parent,
        io_log_file=io_log,
        target_v=509,
        parent_a_v=508,
        parent_b_v=507,
        attempt=1,
    )
    assert len(events) == 1
    category, severity, message, fields = events[0]
    assert category == "pipeline.crossover_degraded_to_parent_copy"
    assert severity == "warn"
    assert fields.get("target_v") == 509
    assert fields.get("parent_a") == 508
    assert fields.get("parent_b") == 507
    # The transport-fault signal is surfaced, not swallowed.
    assert any(
        "read-only sandbox" in str(marker) for marker in fields.get(
            "sandbox_rejection_markers"
        ) or []
    )


def test_crossover_parent_copy_degradation_silent_when_policy_changed(
    monkeypatch, tmp_path
):
    import agent_review

    events = _capture_system_events(monkeypatch)

    parent = tmp_path / "parent_a"
    parent.mkdir()
    (parent / "policy.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    (target / "policy.py").write_text("def f():\n    return 2\n", encoding="utf-8")

    agent_review._log_crossover_parent_copy_degradation(
        target_dir=target,
        frozen_parent_a_dir=parent,
        io_log_file=tmp_path / "absent_io.txt",
        target_v=509,
        parent_a_v=508,
        parent_b_v=507,
        attempt=1,
    )
    assert events == []


def test_crossover_success_path_calls_degradation_logger():
    """Source-level wiring: the crossover success path (after the
    crossover_files_changed audit event) must invoke the degradation logger,
    so a parent-copy acceptance can never again pass silently."""
    source = (
        Path(__file__).resolve().parents[1] / "core" / "agent_review.py"
    ).read_text(encoding="utf-8")
    anchor = source.index("pipeline.crossover_files_changed")
    tail = source[anchor:]
    call_index = tail.find("_log_crossover_parent_copy_degradation(")
    assert call_index != -1, (
        "crossover success path must call _log_crossover_parent_copy_degradation "
        "after the crossover_files_changed event"
    )
