"""Orchestrator context building and PreCompact hook.

_build_context assembles the status string injected into the orchestrator prompt.
_make_precompact_hook preserves evolution state across LLM context compaction.
"""

import re
import shlex
import time

from claude_agent_sdk.types import HookMatcher, SyncHookJSONOutput

from bot_namespace import (
    ACTIVE_BOT_PREFIX,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    bot_tag,
    parse_bot_version,
)
from evolution_infra import MAX_PRECOMMIT_RETRIES
from failure_classification import classify_precommit_gate
from pipeline_state import route_policy

# Module-level cycle start time — set by orchestrator._run_one_cycle at cycle start,
# read by _build_context and PreCompact hook for time-budget awareness.
_cycle_start_time = None
CYCLE_TIMEOUT = 5400  # Must match orchestrator.py (实测 mean cycle 56min，见 orchestrator.py:329 注释)

_SAFE_REDIRECT_TARGETS = {"/dev/null", "nul"}
_SAFE_REDIRECT_PREFIXES = ("/tmp/", "/var/tmp/", "$tmpdir/", "${tmpdir}/")
_PYTHON_OPEN_WRITE_RE = re.compile(r"open\([^)]*,\s*['\"][^'\"]*[wax+]")
_PYTHON_WRITE_PATTERNS = (
    ".write_text(", ".unlink(", ".rename(", ".mkdir(", ".rmdir(",
    "shutil.move", "shutil.copy", "shutil.copytree", "shutil.rmtree",
    "os.remove", "os.unlink", "os.rename", "os.replace", "os.makedirs",
)
_BASH_MUTATION_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|rmdir|mv|cp|mkdir|touch|tee|patch)(?:\s|$)"
    r"|(?:^|[;&|]\s*)sed\s+(?:-[^\s;&|]*i[^\s;&|]*|--in-place(?:[=\s]|$))"
    r"|(?:^|[;&|]\s*)cat\s*(?:>|>>)"
    r"|\bgit\s+(?:add|rm|checkout|restore)\b",
    re.IGNORECASE,
)
_GIT_TAG_READONLY_OPTIONS_WITH_VALUE = {
    "--sort", "--format", "--points-at", "--contains", "--no-contains",
    "--merged", "--no-merged", "--column", "--color",
}
_GIT_TAG_READONLY_FLAGS = {
    "-l", "--list", "-n", "--ignore-case", "--no-column", "--no-color",
}
_GIT_TAG_MUTATION_FLAGS = {
    "-a", "--annotate", "-s", "--sign", "-u", "--local-user", "-f",
    "--force", "-d", "--delete",
}
_GIT_TAG_RE = re.compile(r"\bgit\s+tag\b([^;&|]*)", re.IGNORECASE)
_OPERATOR_ONLY_OFFICIAL_BOOTSTRAP_MARKERS = (
    "--acknowledge-one-time-first-strict-control",
    "--acknowledge-publish-first-strict",
    "bootstrap-first-strict",
    "bootstrap_first_strict",
    "finalize-first-strict",
    "finalize_first_strict",
    "official_bootstrap.py",
    "official_bootstrap_control.json",
    "import official_bootstrap",
    "from official_bootstrap",
)
_LLM_AVAILABILITY_CONTROL_MARKERS = (
    "pok_llm_resume_evidence_digest",
    "llm_availability_store",
    "llm_availability_pause.json",
    "reconcile_llm_pause",
    "resume_llm_pause",
    "consume_operator_resume_ack",
    "persist_llm_pause",
)
_STRICT_AUTHORITY_CONTROL_MARKERS = (
    "strict_authority_workflow",
    "strict-role-accepted",
    "accept_role_result",
    "dispatch_call",
    "complete_provider_call",
    "strictroleaccepted",
    "strictproviderresultobserved",
)


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc bodies so quoted code comparisons are not shell redirects."""
    lines = str(command).splitlines()
    output = []
    pending = []
    for line in lines:
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        output.append(line)
        pending.extend(_shell_heredoc_delimiters(line))
    return "\n".join(output)


def _shell_heredoc_delimiters(line: str) -> list[str]:
    delimiters = []
    quote = None
    escaped = False
    i = 0
    text = str(line)
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if text.startswith("<<<", i):
            i += 3
            continue
        if text.startswith("<<", i):
            i += 2
            if i < len(text) and text[i] == "-":
                i += 1
            while i < len(text) and text[i].isspace():
                i += 1
            token, i = _read_shell_token(text, i)
            token = token.strip("'\"")
            if token:
                delimiters.append(token)
            continue
        i += 1
    return delimiters


def _read_shell_token(text: str, start: int) -> tuple[str, int]:
    token = []
    quote = None
    escaped = False
    i = start
    while i < len(text):
        ch = text[i]
        if escaped:
            token.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            else:
                token.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch.isspace() or ch in ";&|":
            break
        token.append(ch)
        i += 1
    return "".join(token), i


def _iter_shell_write_redirect_targets(command: str):
    text = _strip_heredoc_bodies(command)
    quote = None
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue

        op_start = None
        if text.startswith("&>>", i):
            op_start = i + 3
        elif text.startswith("&>", i):
            op_start = i + 2
        elif ch == ">":
            if i + 1 < len(text) and text[i + 1] == "=":
                i += 2
                continue
            op_start = i + (2 if i + 1 < len(text) and text[i + 1] == ">" else 1)

        if op_start is None:
            i += 1
            continue

        j = op_start
        while j < len(text) and text[j].isspace():
            j += 1
        target, end = _read_shell_token(text, j)
        if target:
            yield target
        i = max(end, j + 1)


def _bash_has_file_write_redirect(command: str) -> bool:
    for target in _iter_shell_write_redirect_targets(command):
        target = target.strip("'\"")
        target_low = target.lower()
        if target.startswith("&"):
            continue
        if target_low in _SAFE_REDIRECT_TARGETS:
            continue
        if target_low.startswith(_SAFE_REDIRECT_PREFIXES):
            continue
        return True
    return False


def _python_snippet_is_mutating(command: str) -> bool:
    low = str(command).lower()
    if "python" not in low:
        return False
    if _PYTHON_OPEN_WRITE_RE.search(low):
        return True
    return any(pattern in low for pattern in _PYTHON_WRITE_PATTERNS)


def _bash_has_mutation_command(command: str) -> bool:
    """Detect mutating shell commands without matching harmless prose like 'confirm '."""
    return bool(_BASH_MUTATION_COMMAND_RE.search(str(command)))


def _git_tag_invocation_is_mutating(args: list[str]) -> bool:
    if not args:
        return False

    list_mode = False
    i = 0
    while i < len(args):
        arg = args[i]
        low = arg.lower()

        if low in _GIT_TAG_MUTATION_FLAGS:
            return True
        if low.startswith("--delete=") or low.startswith("--force="):
            return True
        if low in _GIT_TAG_READONLY_FLAGS or low.startswith("-n"):
            if low in {"-l", "--list"}:
                list_mode = True
            i += 1
            continue
        if any(low.startswith(opt + "=") for opt in _GIT_TAG_READONLY_OPTIONS_WITH_VALUE):
            i += 1
            continue
        if low in _GIT_TAG_READONLY_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if list_mode:
            i += 1
            continue
        if low.startswith("-"):
            return True
        return True

    return False


def _git_tag_is_mutating(command: str) -> bool:
    for match in _GIT_TAG_RE.finditer(str(command)):
        rest = match.group(1).strip()
        if not rest:
            continue
        try:
            args = shlex.split(rest)
        except ValueError:
            args = rest.split()
        if _git_tag_invocation_is_mutating(args):
            return True
    return False


def _orchestrator_bash_is_mutation(command: str) -> bool:
    """True if a Bash command writes/deletes/edits files rather than inspecting."""
    low = str(command).lower()
    if _bash_has_file_write_redirect(command):
        return True
    if _python_snippet_is_mutating(command):
        return True
    if _bash_has_mutation_command(command):
        return True
    if "git commit" in low or "git push" in low:
        return True
    if _git_tag_is_mutating(command):
        return True
    return False


def _orchestrator_bash_targets_operator_only_bootstrap(command: str) -> bool:
    """Reject every main-LLM Bash route to the one-time formal ceremony.

    This is intentionally independent of file-mutation detection and the
    current pipeline stage.  Both ``bootstrap-first-strict`` and
    ``finalize-first-strict`` are operator actions whose durable side effects
    are outside the ordinary
    orchestrator tool route, even when its command text contains no shell
    redirect or other syntactic write marker.
    """
    low = str(command).lower().replace("\\", "/")
    return any(
        marker in low for marker in _OPERATOR_ONLY_OFFICIAL_BOOTSTRAP_MARKERS
    )


def _orchestrator_bash_targets_llm_availability_control(command: str) -> bool:
    """Reject main-LLM shell access to provider pause/resume authority."""

    low = str(command).lower().replace("\\", "/")
    return any(marker in low for marker in _LLM_AVAILABILITY_CONTROL_MARKERS)


def _orchestrator_bash_targets_strict_authority_control(command: str) -> bool:
    """Reject shell/import access to first-strict execution authority."""

    low = str(command).lower().replace("\\", "/")
    return any(marker in low for marker in _STRICT_AUTHORITY_CONTROL_MARKERS)


def set_cycle_start_time(t):
    """Called from orchestrator._run_one_cycle to mark the cycle start."""
    global _cycle_start_time
    _cycle_start_time = t


def _get_time_budget_info():
    """Return a string describing cycle time budget, or empty string if not in a cycle."""
    if _cycle_start_time is None:
        return ""
    elapsed = int(time.time() - _cycle_start_time)
    remaining = max(0, CYCLE_TIMEOUT - elapsed)
    pct = int(elapsed / CYCLE_TIMEOUT * 100)
    return (
        f"CYCLE TIME BUDGET: {elapsed}s elapsed / {CYCLE_TIMEOUT}s total "
        f"({remaining}s remaining, {pct}% used). "
        f"{'⚠️ Less than 15 minutes remaining — do NOT start new retry loops.' if remaining < 900 else ''}"
    )


def _inject_master_plan_hint(checkpoint, lines):
    """Inject master plan task summaries into context.

    Critical: the Orchestrator LLM has no Bash/Read tools — it cannot read
    pipeline_state.json on its own.  If we only say "Master plan is saved in
    provider history", the mandatory fresh provider session has no prior task
    list and the
    model spirals calling ToolSearch trying to find Read/Bash.  Instead we
    inline a compact summary for observability while instructing the model to
    pass ``tasks=[]``.  The worker tool then loads the exact checkpoint-owned
    task objects; a prompt preview must never be used to reconstruct them.
    """
    plan = checkpoint.get("master_plan")
    route = route_policy(checkpoint)
    next_tool = route.get("next_tool")
    if not plan:
        if checkpoint.get("parent2_v"):
            lines.append(
                "Crossover checkpoint has no task plan yet. run_crossover may "
                "create only a recombination baseline, never the governed "
                "generation innovation. "
                f"Follow the route policy: next_tool={next_tool}, "
                f"intent={route.get('intent')}. {route.get('directive')}"
            )
            return
        if next_tool == "run_master":
            lines.append(
                "Master plan is not yet checkpoint-owned. Call run_master for "
                "this exact route; do not reconstruct a plan from prompt text."
            )
        else:
            lines.append(
                "No Master plan is checkpoint-owned at this stage. This does "
                f"not authorize run_master or Workers; follow next_tool={next_tool}. "
                f"{route.get('directive')}"
            )
        return
    tasks = plan.get("tasks", [])
    if plan.get("strategy") == "crossover" and checkpoint.get("parent2_v") and not tasks:
        lines.append(
            "Crossover plan is saved and no rework tasks are present. "
            f"Follow the route policy: next_tool={route.get('next_tool')}. {route.get('directive')}"
        )
        return
    if tasks:
        if next_tool == "execute_workers":
            lines.append(
                "Master plan is saved — do NOT call run_master again. "
                "Call execute_workers with tasks=[] so the tool loads the exact "
                "checkpoint-owned tasks. Do not paraphrase or reconstruct them:"
            )
        else:
            lines.append(
                "Master plan is retained as checkpoint evidence. Do NOT call "
                "run_master or execute_workers unless the current route names "
                f"that tool; follow next_tool={next_tool}:"
            )
        for t in tasks:
            wid = t.get("worker_id", "?")
            role = t.get("role", "?")
            targets = ", ".join(t.get("target_files", []))
            lines.append(
                f"  Worker {wid} ({role}): checkpoint targets=[{targets}]"
            )
    else:
        lines.append("Master plan is saved — do NOT call run_master again.")


def _append_no_checkpoint_directive(lines, *, reason="no active checkpoint"):
    """Project the provider's fail-closed checkpoint-free action."""

    lines.append(
        "\nNO ACTIVE PIPELINE CHECKPOINT: "
        f"{reason}. PROVIDER ACTION: end_stream. Make no MCP call. "
        "end_stream is not a tool; finish this response now. The outer "
        "scheduler alone may later call non-MCP prepare_generation to select "
        "and publish a new validated selected checkpoint. Never call "
        "prepare_next_gen without that exact checkpoint."
    )


def _append_post_publication_handoff_directive(lines, handoff):
    """Project a provider-terminal post-publication boundary when present.

    The provider never owns ``run_archivist``.  Returning ``True`` tells the
    caller that a pending/blocked handoff (rather than a generic checkpoint-free
    scheduler boundary) was projected.
    """

    status = handoff.get("status")
    if status == "pending":
        lines.append(
            "POST-PUBLICATION HANDOFF ACTIVE: "
            f"v{handoff.get('version')} from v{handoff.get('source_v')}, "
            f"state={handoff.get('state')}. PROVIDER ACTION: end_stream. "
            "The outer deterministic recovery path alone owns run_archivist; "
            "do not call any MCP tool or prepare/select another generation."
        )
        return True
    if status == "blocked":
        lines.append(
            "POST-PUBLICATION HANDOFF BLOCKED/AMBIGUOUS: "
            + "; ".join(map(str, handoff.get("issues") or []))
            + ". PROVIDER ACTION: end_stream. Make no MCP call. The outer "
            "deterministic recovery path must surface or repair this handoff; "
            "the provider never owns run_archivist or successor preparation."
        )
        return True
    return False


def _project_post_publication_handoff(lines):
    """Append the current handoff boundary, failing closed if unreadable."""

    try:
        from post_publication_handoff import pending_handoff_route

        return _append_post_publication_handoff_directive(
            lines,
            pending_handoff_route(),
        )
    except Exception as exc:
        lines.append(
            "POST-PUBLICATION HANDOFF AUTHORITY UNAVAILABLE: "
            f"{type(exc).__name__}. PROVIDER ACTION: end_stream. Make no MCP "
            "call; outer deterministic recovery must restore this authority."
        )
        return True


def _format_checkpoint_info(checkpoint, lines):
    """Append pipeline checkpoint details to *lines*.

    Extracts the common formatting shared between the gen_ctx and
    non-gen_ctx code paths in ``_build_context``.
    """
    stage = checkpoint.get("stage", "unknown")
    route = route_policy(checkpoint)
    hint = route.get("directive") or "inspect checkpoint context and continue with the matching MCP pipeline tool"
    if route.get("next_tool") == "run_archivist":
        lines.append(
            f"\nPIPELINE CHECKPOINT: v{checkpoint['next_v']} "
            f"(from v{checkpoint['source_v']}) reached stage='{stage}'. "
            "PROVIDER ACTION: end_stream. The outer deterministic recovery "
            "path alone owns run_archivist; make no MCP call and do not "
            "prepare/select a successor."
        )
        return
    lines.append(
        f"\nPIPELINE CHECKPOINT: v{checkpoint['next_v']} (from v{checkpoint['source_v']}) "
        f"reached stage='{stage}'. Next MCP tool: {route.get('next_tool')}. {hint}"
    )
    if not route.get("next_tool"):
        lines.append(
            "NO AUTHORIZED CHECKPOINT ROUTE: PROVIDER ACTION: end_stream. "
            "Make no MCP call; the outer recovery loop must validate or surface "
            "this checkpoint state."
        )
    if (
        stage in {"selected", "preparing"}
        and route.get("next_tool") == "prepare_next_gen"
    ):
        preparation_kind = (
            "the first materialization of the already-selected candidate"
            if stage == "selected"
            else (
                "recovery of the interrupted preparation only while no "
                "unbound target preimage exists"
            )
        )
        lines.append(
            "PREPARE ROUTE AUTHORIZATION: prepare_next_gen may be called only "
            "for this exact runtime-validated checkpoint identity and its exact "
            f"source_v/next_v. This route owns {preparation_kind}; it does not "
            "select or start a generation. If target bytes exist without the "
            "exact prepared-artifact contract, the system-owned prepare route "
            "must canonically abandon/quarantine this checkpoint instead of "
            "adopting, deleting, or continuing those bytes."
        )
    else:
        lines.append(
            "prepare_next_gen is NOT authorized at this checkpoint stage. "
            "Follow only the checkpoint's current Next MCP tool."
        )
    if route.get("next_tool") == "abandon_generation":
        lines.append(
            "CANONICAL ABANDON ROUTE: call only the authorized owner tool, then "
            "end_stream. Outer recovery accepts termination only from exactly "
            "one canonical current-head result returned by that current "
            "authorized owner tool, whether flattened or nested, with "
            "workflow_run_id, abandoned=true, cleared_checkpoint=true, "
            "abandon_transaction_id, "
            "abandon_receipt_digest, finalize_receipt_digest, and "
            "abandon_checkpoint_identity. Duplicate flattened/nested results, "
            "a missing checkpoint, or bare success are not terminal proof. "
            "The result must bind one pending route-mutating ToolUse by its "
            "explicit id/parent id, or by the bounded sole-pending SDK form; "
            "unknown, reused, swapped-owner, multi-pending, or unsettled ids "
            "block recovery."
        )
    if stage == "timed_out":
        lines.append(
            "TIMEOUT ACTIVE LEASE: the only legal recovery is the "
            "checkpoint-routed canonical abandon_generation owner. It is not "
            "a dead/restartable checkpoint and cannot be overwritten by a new "
            "generation. Never call prepare_next_gen for timed_out."
        )
    elif stage == "infra_timed_out":
        lines.append(
            "INFRASTRUCTURE TIMEOUT ACTIVE LEASE: retry only "
            "run_precommit_eval. The tool must first re-prove the complete "
            "candidate fingerprint, current quality/review/critic gate "
            "identities, and quality fingerprint = repair baseline = live "
            "bytes, then exact-CAS back to critic_checked. Any mismatch keeps "
            "the overlay blocked; do not prepare or strategically rework it."
        )
    gen_attempt = checkpoint.get("generation_attempt", 0)
    if gen_attempt > 0:
        lines.append(
            f"INTRA-GEN ATTEMPTS: {gen_attempt}. "
            "Follow the checkpoint route and the latest tool directive exactly; "
            "do not maintain a private retry counter."
        )
    _inject_master_plan_hint(checkpoint, lines)
    # Precommit retry status — bot code is unchanged across precommit attempts, so
    # retrying run_precommit_eval gives the SAME result. Surface this so the LLM
    # does not loop on precommit; it must rework the bot or abandon instead.
    precommit_attempt = checkpoint.get("precommit_attempt", 0)
    if precommit_attempt > 0:
        precommit_gate = checkpoint.get("gate_results", {}).get("precommit_eval")
        failure_class = classify_precommit_gate(precommit_gate)
        last_result = ""
        if precommit_gate is not None:
            _pw = precommit_gate.get("total_wins", 0)
            _pl = precommit_gate.get("total_losses", 0)
            _pd = precommit_gate.get("total_draws", 0)
            _nopp = precommit_gate.get("n_opponents")
            if _nopp is None:
                _nopp = len(precommit_gate.get("opponents", []) or [])
            last_result = f"last: {_pw}W-{_pl}L-{_pd}D vs {_nopp} opps"
        if failure_class == "infra_timeout":
            lines.append(
                f"PRECOMMIT STATUS: {precommit_attempt}/{MAX_PRECOMMIT_RETRIES} attempts. "
                f"{last_result}. Last failure was INFRASTRUCTURE-only; retry run_precommit_eval "
                f"on the same code with the tool's reduced n_games policy."
            )
        elif failure_class in {"regression", "failed_unknown"}:
            lines.append(
                f"PRECOMMIT STATUS: {precommit_attempt}/{MAX_PRECOMMIT_RETRIES} attempts. "
                f"{last_result}. Bot code is unchanged across attempts — retrying run_precommit_eval "
                f"gives the SAME result. Rework the bot with execute_workers using the exact "
                f"precommit feedback; do NOT abandon before the hard limit."
            )
        else:
            lines.append(
                f"PRECOMMIT STATUS: {precommit_attempt}/{MAX_PRECOMMIT_RETRIES} attempts. "
                f"{last_result}. Follow route policy next tool: "
                f"{route_policy(checkpoint).get('next_tool') or 'inspect checkpoint'}."
            )
        if precommit_attempt >= MAX_PRECOMMIT_RETRIES:
            lines.append("PRECOMMIT HARD LIMIT reached — abandon this generation.")
    last_update = checkpoint.get("last_update_ts")
    if last_update:
        age = int(time.time() - last_update)
        lines.append(f"Last checkpoint activity: {age}s ago")


def _build_context(one_gen=False, dry_run=False, gen_ctx=None):
    """Build context string injected into the orchestrator prompt.

    When gen_ctx (GenerationContext) is provided, injects pre-computed analysis
    data from the code-layer scheduler instead of raw status data.
    """
    from evolution_core import (
        get_active_bots,
        get_bot_dir,
    )
    # If GenerationContext is provided, build streamlined context
    if gen_ctx is not None:
        protocol_bootstrap_no_strength = gen_ctx.strategy in {
            "fresh_policy_bootstrap",
            "singleton_strict_bootstrap",
        }
        lines = [
            f"Version authority high-water: v{gen_ctx.current_v}",
            f"Next generation: v{gen_ctx.next_v}",
            f"Strategy: {gen_ctx.strategy}",
            f"Active bots: {len(get_active_bots())}",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if gen_ctx.strategy == "fresh_policy_bootstrap":
            lines.append(
                "Source bot: NONE. v142 is archived version authority only and "
                "must not be opened, copied, executed, or mined for strategy."
            )
        else:
            lines.append(f"Source bot: {bot_name(gen_ctx.source_v)}")
        if gen_ctx.strategy == "crossover" and gen_ctx.crossover_parents:
            lines.append(f"Crossover parents: {bot_name(gen_ctx.crossover_parents[0])} x {bot_name(gen_ctx.crossover_parents[1])}")

        # Tool reference — a capability catalog is not route authorization.
        lines.append("\nMCP CAPABILITY CATALOG (not route authorization):")
        lines.append("  prepare_next_gen(source_v, next_v) — first-materialize an exact runtime-validated selected checkpoint or idempotently recover its exact preparing checkpoint; never select/start a generation")
        lines.append("  run_direction_audit(source_v, next_v) — detect repetitive evolution directions")
        lines.append("  run_master(source_v, next_v, stagnation_info, match_analysis, performance_verification, direction_audit, research_proposals) — plan worker tasks")
        lines.append("  execute_workers(tasks, next_v, source_v, reviewer_feedback) — after Master, pass tasks=[] to load the exact checkpoint-owned plan; modifies bot code in parallel when target_files do not overlap (max 3), otherwise serial")
        lines.append("  run_quality_gates(version, source_v) — full hard gates: code_changed, declared_scope, compile/runtime import, protected contracts, smoke, national protocol/acceptance, decision, size, fix verification, telemetry fidelity, reachability")
        lines.append("  run_review(version, source_v, plan) — code quality review (boundaries, size, correctness)")
        lines.append("  run_critic(version, source_v, plan, reviewer_feedback, force_advance) — required schema-valid advisory strategy assessment; it does not accept, reject, or schedule repair")
        lines.append("  run_precommit_eval(version, source_v, n_games) — final local regression check over complete 70-hand native TCP matches")
        lines.append("  commit_bot(version, source_v, strategy, review_approved=true) — git commit + tag (requires all gates passed)")
        lines.append("  run_archivist(version, source_v) — outer deterministic recovery capability only; the provider must end_stream after commit/post-publication handoff")
        lines.append("  run_crossover(parent_a, parent_b, target_v) — prepare a two-parent baseline only; direction audit, optional research, Master planning, and Workers still follow")
        lines.append("  run_literature_probe(source_v, next_v, h2h_weakness, stagnation_info) — web-search ONE codable strategy hypothesis for the bot's biggest H2H weakness (governance-gated: auto-skips on cooldown). MANDATORY when stagnation analysis shows is_stagnant:true.")
        lines.append("  abandon_generation(...) — callable only when the exact checkpoint route names it; other owner tools may perform centralized abandon, and intent/bare success is not terminal proof")
        lines.append("  prepare_generation is deliberately absent: it is non-MCP and outer-scheduler-owned")

        if protocol_bootstrap_no_strength:
            if gen_ctx.strategy == "fresh_policy_bootstrap":
                lines.append(
                    "\nPROTOCOL BOOTSTRAP NO-STRENGTH: the prepared v143 "
                    "artifact, epoch reset receipt, and pinned national protocol "
                    "contracts are the only planning inputs. Historical ratings, "
                    "H2H, replays, lessons, failures, official prose, and v142 "
                    "source bytes are unavailable."
                )
            else:
                lines.append(
                    "\nSINGLETON STRICT BOOTSTRAP NO-STRENGTH: the sole strict "
                    "published parent and checkpoint-owned preparation receipt are "
                    "the only code lineage inputs. No peer-cycle rating, H2H, "
                    "replay, lesson, failure, or official prose is admissible."
                )
        elif gen_ctx.stagnation_info:
            lines.append(f"\nStagnation analysis:\n{gen_ctx.stagnation_info}")
        if not protocol_bootstrap_no_strength and gen_ctx.match_analysis:
            lines.append(f"\nMatch analysis:\n{gen_ctx.match_analysis}")
        if not protocol_bootstrap_no_strength and gen_ctx.replay_spotlight:
            lines.append(f"\nReplay spotlight:\n{gen_ctx.replay_spotlight}")
        if not protocol_bootstrap_no_strength and gen_ctx.performance_verification:
            lines.append(f"\nPerformance verification:\n{gen_ctx.performance_verification}")

        if one_gen:
            lines.append("MODE: Run exactly ONE generation, then stop.")
        else:
            lines.append("MODE: Execute this generation using the pipeline tools.")
        # Pipeline checkpoint is the sole route authority for this provider.
        checkpoint = None
        checkpoint_error = None
        try:
            from evolution_core import read_pipeline_checkpoint
            checkpoint = read_pipeline_checkpoint()
        except Exception as exc:
            checkpoint_error = f"checkpoint authority unreadable ({type(exc).__name__})"
        handoff_boundary = _project_post_publication_handoff(lines)
        if checkpoint and not handoff_boundary:
            _format_checkpoint_info(checkpoint, lines)
        elif not checkpoint and not handoff_boundary:
            _append_no_checkpoint_directive(
                lines,
                reason=checkpoint_error or "no live checkpoint was supplied",
            )
        return "\n".join(lines)

    from epoch_authority import (
        strict_epoch_projection,
        unpublished_candidate_versions,
    )

    epoch = strict_epoch_projection()
    active_bots = list(epoch["active_bots"])
    current_v = int(epoch["current_v"])
    next_v = int(epoch["next_v"])

    lines = [
        f"Version authority high-water: v{current_v}",
        f"Next generation will be: v{next_v}",
        f"Active bots: {len(active_bots)}",
        f"Evaluation epoch: {epoch['evaluation_epoch']} ({epoch['state']})",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    if not epoch["initialized"]:
        lines.append(
            "STRICT EPOCH NOT INITIALIZED: mutable ratings, H2H, abandoned "
            "versions, candidate directories, and the retired checkpoint are "
            "excluded from planning and numbering authority."
        )
        if epoch.get("operator_command"):
            lines.append(
                "OPERATOR ACTION REQUIRED (the LLM must not execute it): "
                f"{epoch['operator_command']}"
            )
        elif epoch.get("operator_action"):
            lines.append(
                f"OPERATOR RECOVERY REQUIRED: {epoch['operator_action']}"
            )

    # Capability catalog — the current checkpoint route remains authoritative.
    lines.append("\nMCP CAPABILITY CATALOG (not route authorization):")
    lines.append("  prepare_next_gen | run_direction_audit | run_literature_probe | run_master | execute_workers | run_quality_gates | run_review | run_critic | run_precommit_eval | commit_bot | run_archivist | run_crossover | abandon_generation")
    lines.append("  prepare_generation is non-MCP and exclusively owned by the outer scheduler")
    lines.append("  run_archivist is outer deterministic recovery only; a provider that observes its route must end_stream")

    strict_active_versions = sorted(
        (
            (version, name)
            for name in active_bots
            if (version := parse_bot_version(str(name))) is not None
            and version >= FIRST_STRICT_POLICY_VERSION
        ),
        reverse=True,
    )
    current_bot_name = strict_active_versions[0][1] if strict_active_versions else ""
    if not current_bot_name:
        lines.append(
            "Strict active source: NONE. Archived completion high-water is not a "
            "strategy source or prompt-evidence identity."
        )

    # The no-GenerationContext path is used by status-only dry runs.  It must
    # not reopen mutable ratings/action/history files and accidentally create a
    # second prompt-evidence authority.  Normal evolution receives the exact
    # generation snapshot through ``gen_ctx`` above.

    # Incomplete bot detection — previous cycle may have been interrupted
    next_dir = get_bot_dir(next_v)
    if next_dir.exists() and not (next_dir / ".completed").exists():
        lines.append(
            f"WARNING: {bot_name(next_v)} directory exists but is NOT completed "
            f"(previous cycle was interrupted). Decide: resume workers or clean up and restart."
        )

    # Recent completed generations from the strict published identity resolver.
    # Raw tag enumeration would mix archived v1..v142 identities into the
    # current policy epoch and is therefore not prompt authority.
    try:
        from national_runtime_authority import strict_published_bot_names

        strict_versions = sorted({
            version
            for name in strict_published_bot_names()
            if (version := parse_bot_version(name)) is not None
            and version >= FIRST_STRICT_POLICY_VERSION
        }, reverse=True)
        recent_tags = [bot_tag(version) for version in strict_versions[:5]]
        if recent_tags:
            lines.append(f"Recent completed gens: {', '.join(recent_tags)}")
    except Exception:
        pass

    # Pipeline checkpoint — tell Orchestrator exactly where a killed cycle left off
    checkpoint = None
    checkpoint_error = None
    try:
        from evolution_core import read_pipeline_checkpoint

        if epoch.get("active_generation"):
            checkpoint = read_pipeline_checkpoint()
        elif epoch.get("ignored_checkpoint"):
            ignored = epoch["ignored_checkpoint"]
            lines.append(
                "RETIRED CHECKPOINT EVIDENCE (NOT ACTIVE): "
                f"v{ignored.get('next_v')} from v{ignored.get('source_v')}, "
                f"stage={ignored.get('stage')}; it will be archived by the "
                "operator reset and cannot route any pipeline tool."
            )
    except Exception as exc:
        checkpoint_error = f"checkpoint authority unreadable ({type(exc).__name__})"
    handoff_boundary = _project_post_publication_handoff(lines)

    if checkpoint and not handoff_boundary:
        _format_checkpoint_info(checkpoint, lines)
    elif not checkpoint and not handoff_boundary:
        _append_no_checkpoint_directive(
            lines,
            reason=checkpoint_error or "strict epoch projection has no live checkpoint",
        )

    debris = unpublished_candidate_versions()
    if debris:
        lines.append(
            "UNPUBLISHED CANDIDATE DEBRIS (NOT VERSION AUTHORITY): "
            + ", ".join(f"v{version}" for version in debris)
        )

    # Environment anomaly detection
    anomalies = []
    if next_dir.exists() and not (next_dir / ".completed").exists():
        anomalies.append("incomplete bot directory")
    if anomalies:
        lines.append(
            f"ENVIRONMENT ANOMALIES DETECTED: {', '.join(anomalies)}."
        )

    if one_gen:
        lines.append("MODE: Run exactly ONE generation, then stop.")
    elif dry_run:
        lines.append("MODE: DRY RUN — only check status, do NOT modify anything.")
    else:
        lines.append(
            "MODE: Continuous evolution is owned by the outer loop. Advance only "
            "the current checkpoint; after terminal completion, end this provider "
            "stream so the outer scheduler can decide whether to start the next."
        )

    # Cycle time budget — helps Orchestrator avoid starting retry loops near timeout
    time_budget = _get_time_budget_info()
    if time_budget:
        lines.append(time_budget)

    return "\n".join(lines)


def _make_precompact_hook():
    """Return hooks dict that injects evolution state before Claude compacts context."""
    async def handler(hook_input, tool_use_id, context) -> SyncHookJSONOutput:
        from evolution_core import read_pipeline_checkpoint
        from epoch_authority import strict_epoch_projection
        lines = ["=== EVOLUTION STATE — PRESERVE DURING COMPACTION ==="]
        try:
            epoch = strict_epoch_projection()
            current_v = int(epoch["current_v"])
            lines.append(f"Version authority high-water: {bot_name(current_v)}")
            checkpoint = read_pipeline_checkpoint() if epoch.get("active_generation") else None
            if checkpoint is not None:
                stage = checkpoint.get("stage", "unknown")
                route = route_policy(checkpoint)
                next_step = route.get("next_tool") or "inspect checkpoint context"
                if route.get("next_tool") == "run_archivist":
                    lines.append(
                        f"ACTIVE POST-PUBLICATION CHECKPOINT: v{checkpoint['next_v']} "
                        f"(from v{checkpoint['source_v']}), stage={stage}. "
                        "PROVIDER ACTION: end_stream. Outer deterministic "
                        "recovery alone owns run_archivist; make no MCP call."
                    )
                elif not route.get("next_tool"):
                    lines.append(
                        f"ACTIVE BLOCKED CHECKPOINT: v{checkpoint['next_v']} "
                        f"(from v{checkpoint['source_v']}), stage={stage}. "
                        "PROVIDER ACTION: end_stream. Make no MCP call; outer "
                        "recovery must validate or surface this state."
                    )
                else:
                    lines.append(
                        f"ACTIVE GENERATION: v{checkpoint['next_v']} (from v{checkpoint['source_v']}), "
                        f"stage={stage}. Next tool: {next_step}. "
                        f"{route.get('directive')} DO NOT restart this generation — continue from this stage."
                    )
                    _inject_master_plan_hint(checkpoint, lines)
            elif epoch.get("ignored_checkpoint"):
                ignored = epoch["ignored_checkpoint"]
                lines.append(
                    "NO ACTIVE GENERATION. Retired checkpoint "
                    f"v{ignored.get('next_v')} is incompatible evidence only; "
                    "do not resume it or treat it as a version floor."
                )
            else:
                handoff_boundary = _project_post_publication_handoff(lines)
                if not handoff_boundary:
                    _append_no_checkpoint_directive(
                        lines,
                        reason="compaction projection has no live checkpoint",
                    )
            if not epoch["initialized"]:
                lines.append(
                    "Strict policy epoch is not initialized; operator reset is "
                    "required before any generation tool may run."
                )
        except Exception:
            pass
        # Cycle time budget for compaction survival
        time_budget = _get_time_budget_info()
        if time_budget:
            lines.append(time_budget)
        return SyncHookJSONOutput(reason="\n".join(lines))
    return {"PreCompact": [HookMatcher(matcher="*", hooks=[handler])]}


def _make_bot_dir_guard_hook():
    """P1 root-cause fix (2026-06-29): PreToolUse hook that BLOCKS the
    orchestrator LLM from bypassing pipeline gates by directly mutating bot
    code OR critical pipeline state files with its built-in Bash/Edit/Write tools.

    Root cause of the v218 gate-bypass: the H6 cross-gen circuit breaker tripped
    and execute_workers returned an error, but the orchestrator LLM then used its
    built-in Bash tool (6 calls in one cycle) to hand-edit active bot code.
    This completely bypassed execute_workers and every gate it enforces (circuit
    breaker, boundary validation, CoT audit, file_size check). The bot then
    passed quality gates on the hand-edited code and was committed — defeating
    the entire pipeline contract.

    Multi-agent exploration (2026-06-29) found additional bypass vectors that
    this hook now also covers:
      - pipeline_state.json: forge stage="verified" + all-passed gate_results
        to skip every gate and commit arbitrary code.
      - worker_failures.jsonl: truncate the operator-visible failure audit trail.
      - git commit/tag/push: bypass commit_bot's gate checks.
    The orchestrator LLM's task-orchestration is done via MCP tools, so these
    built-in file-mutation paths are never legitimately needed.

    The fix is structural: bot code AND pipeline-critical state may ONLY be
    written by the designated MCP tools. Read-only Bash (grep/cat/git status) is
    still permitted during open-ended planning. Once the checkpoint is at an
    actionable route stage, built-in tools are also blocked so the model cannot
    spend an active cycle inspecting instead of calling the required MCP tool.
    """
    import os
    # Bot code lives under PROJECT_ROOT/bots/{ACTIVE_BOT_PREFIX}*. Resolve once.
    try:
        from evolution_infra import PROJECT_ROOT, RESULTS_DIR
        _bots_root = str((PROJECT_ROOT / "bots").resolve())
        _results_root = str(RESULTS_DIR.resolve())
    except Exception:
        _bots_root = None
        _results_root = None

    # Pipeline-critical state files that, if forged by the LLM, let it bypass
    # gates or reset protective breakers. These must only be written by the
    # designated tools (write_pipeline_checkpoint / commit_bot / agent_workers),
    # never by the orchestrator's Bash/Edit.
    _PROTECTED_STATE_FILES = (
        "pipeline_state.json",
        "worker_failures.jsonl",
        "circuit_breaker_state.json",
        "priority_eval.json",
        "glicko_ratings.json",
        "bot_stats.json",
        "abandoned_versions.jsonl",
        # Operator-owned cost authority.  The main LLM may inspect these files,
        # but cannot truncate the durable ledger, erase write-ahead failures, or
        # rewrite the enforcement/launch code for the next process restart.
        "generation_cost_ledger.jsonl",
        "generation_cost_pending.json",
        "orchestrator_cost_policy.py",
        "llm_query.py",
        "generation_scheduler.py",
        "orchestrator.py",
        "llm_availability_pause.json",
        "llm_availability_store.py",
        "pokctl.sh",
    )
    _HARD_ROUTE_TOOLS = {
        "execute_workers",
        "prepare_next_gen",
        "run_crossover",
        "run_quality_gates",
        "run_review",
        "run_critic",
        "run_precommit_eval",
        "commit_bot",
        "run_archivist",
    }
    def _targets_protected(text):
        """True if the command/text references a protected path:
        active bot code OR a pipeline-critical state file."""
        if not text:
            return False
        low = str(text).lower()
        # Bot code dir
        if f"bots/{ACTIVE_BOT_PREFIX}" in low or f"bots\\{ACTIVE_BOT_PREFIX}" in low:
            return True
        if _bots_root and _bots_root.lower() in low:
            return True
        # Pipeline-critical state files (match by filename anywhere in path/cmd)
        for sf in _PROTECTED_STATE_FILES:
            if sf in low:
                return True
        return False

    def _current_stage_directive():
        """Return a compact MCP recovery directive for denied direct mutations."""
        try:
            from evolution_core import read_pipeline_checkpoint
            checkpoint = read_pipeline_checkpoint() or {}
        except Exception:
            checkpoint = {}
        stage = checkpoint.get("stage")
        next_v = checkpoint.get("next_v")
        source_v = checkpoint.get("source_v")
        route = route_policy(checkpoint) if checkpoint else {}
        next_step = route.get("next_tool") if stage else None
        if not stage or not next_step:
            return (
                "Recovery: no active checkpoint route exists. Do NOT retry the "
                "denied direct mutation and do not call any MCP tool. PROVIDER "
                "ACTION: end_stream. The outer scheduler alone owns non-MCP "
                "prepare_generation."
            ), {"stage": stage, "next_v": next_v, "source_v": source_v, "next_step": next_step}
        return (
            f"Recovery: current checkpoint is v{next_v} from v{source_v}, "
            f"stage={stage}. Do NOT retry the denied Bash/Edit/Write call. "
            f"NEXT MCP TOOL: {next_step}. {route.get('directive', '')}"
        ), {"stage": stage, "next_v": next_v, "source_v": source_v, "next_step": next_step}

    def _actionable_route_directive():
        """Return a hard-route directive when built-in tools would delay recovery."""
        try:
            from evolution_core import read_pipeline_checkpoint
            checkpoint = read_pipeline_checkpoint() or {}
        except Exception:
            checkpoint = {}
        stage = checkpoint.get("stage")
        route = route_policy(checkpoint) if checkpoint else {}
        next_step = route.get("next_tool")
        if not stage or next_step not in _HARD_ROUTE_TOOLS:
            return None, {}
        next_v = checkpoint.get("next_v")
        source_v = checkpoint.get("source_v")
        parent2_v = checkpoint.get("parent2_v")
        return (
            f"Actionable checkpoint route is locked: v{next_v} from v{source_v}, "
            f"stage={stage}, next MCP tool={next_step}. Built-in Bash/Edit/Write "
            f"are disabled at this stage because they delay or bypass deterministic "
            f"recovery. Call {next_step} with the checkpoint context now. "
            f"{route.get('directive', '')}"
        ), {
            "stage": stage,
            "next_v": next_v,
            "source_v": source_v,
            "parent2_v": parent2_v,
            "next_step": next_step,
            "route_intent": route.get("intent"),
        }

    async def handler(hook_input, tool_use_id, context):
        try:
            tool_name = hook_input.get("tool_name", "") if isinstance(hook_input, dict) else ""
            tool_input = hook_input.get("tool_input", {}) if isinstance(hook_input, dict) else {}

            blocked_reason = None
            blocked_command = ""
            blocked_data = {}
            if tool_name == "Bash":
                cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
                if _orchestrator_bash_targets_operator_only_bootstrap(cmd):
                    blocked_command = str(cmd)
                    blocked_data = {"operator_action_required": True}
                    blocked_reason = (
                        "The first-strict formal ceremony is operator-only. The main "
                        "Orchestrator may never invoke bootstrap-first-strict or "
                        "finalize-first-strict, acknowledge either transition, or call "
                        "official_bootstrap through Bash. "
                        "Stop automatic evolution and wait for an explicit external "
                        "operator command."
                    )
                elif _orchestrator_bash_targets_llm_availability_control(cmd):
                    blocked_command = str(cmd)
                    blocked_data = {"operator_action_required": True}
                    blocked_reason = (
                        "LLM availability pause/resume authority is operator-owned. "
                        "The main Orchestrator may not inspect or import the pause "
                        "store, set the resume environment variable, or call a "
                        "reconcile/resume API through Bash. A manual acknowledgement "
                        "is accepted only once at parent-process startup before any "
                        "SDK child is created."
                    )
                elif _orchestrator_bash_targets_strict_authority_control(cmd):
                    blocked_command = str(cmd)
                    blocked_data = {"operator_action_required": True}
                    blocked_reason = (
                        "First-strict LLM execution authority is system-owned. "
                        "The Orchestrator may not import its WorkflowStore backend, "
                        "dispatch/complete provider effects, or append accepted-role "
                        "events through Bash. Use only the typed pipeline tools."
                    )
                else:
                    directive, route_data = _actionable_route_directive()
                    if directive:
                        blocked_command = str(cmd)
                        blocked_data = route_data
                        blocked_reason = directive
                    elif _targets_protected(cmd) and _orchestrator_bash_is_mutation(cmd):
                        blocked_command = str(cmd)
                        directive, blocked_data = _current_stage_directive()
                        blocked_reason = (
                            "Bash command targets a protected path (bot code or pipeline state) "
                            "and appears to mutate files. Bot code may ONLY be modified via "
                            "execute_workers or run_crossover; pipeline state only via designated "
                            "tools; git commits only via commit_bot. Direct mutations bypass all "
                            "pipeline gates. " + directive
                        )
            elif tool_name in ("Edit", "Write", "NotebookEdit"):
                file_path = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
                directive, route_data = _actionable_route_directive()
                if directive:
                    blocked_command = str(file_path)
                    blocked_data = route_data
                    blocked_reason = directive
                elif _targets_protected(file_path):
                    blocked_command = str(file_path)
                    directive, blocked_data = _current_stage_directive()
                    blocked_reason = (
                        tool_name + " targets a protected path (" + str(file_path) +
                        "). Bot code may ONLY be modified via execute_workers or run_crossover; "
                        "pipeline-critical state files may ONLY be written by designated tools. "
                        "Direct edits bypass all pipeline gates (circuit breaker, boundary "
                        "validation, CoT, gate_results integrity). " + directive
                    )

            if blocked_reason:
                from system_log import log_system_event
                try:
                    preview_limit = 1000
                    event_data = {
                        "tool": tool_name,
                        "reason": blocked_reason[:1000],
                        "tool_use_id": str(tool_use_id)[:64],
                        "command_preview": blocked_command[:preview_limit],
                        "command_truncated": len(blocked_command) > preview_limit,
                    }
                    event_data.update(blocked_data)
                    log_system_event(
                        "pipeline.guard_block", "error",
                        "BLOCKED " + tool_name + " from bypassing pipeline route: "
                        + blocked_reason[:140],
                        event_data,
                    )
                except Exception:
                    pass
                return SyncHookJSONOutput(
                    hookSpecificOutput={
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": blocked_reason,
                    }
                )
        except Exception:
            pass
        return SyncHookJSONOutput()

    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[handler]),
            HookMatcher(matcher="Edit", hooks=[handler]),
            HookMatcher(matcher="Write", hooks=[handler]),
            HookMatcher(matcher="NotebookEdit", hooks=[handler]),
        ]
    }
