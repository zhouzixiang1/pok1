"""Orchestrator context building and PreCompact hook.

_build_context assembles the status string injected into the orchestrator prompt.
_make_precompact_hook preserves evolution state across LLM context compaction.
"""

import json
import re
import shlex
import time

from claude_agent_sdk.types import HookMatcher, SyncHookJSONOutput

from bot_namespace import ACTIVE_BOT_PREFIX, bot_name, bot_tag_glob
from evolution_infra import locked_file, RESULTS_DIR, MAX_PRECOMMIT_RETRIES
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
    session history", a fresh (non-resumed) session has NO history and the
    model spirals calling ToolSearch trying to find Read/Bash.  Instead we
    inline a compact summary of each task so execute_workers(tasks=...)
    can be called correctly.
    """
    plan = checkpoint.get("master_plan")
    route = route_policy(checkpoint)
    if not plan:
        if checkpoint.get("parent2_v"):
            lines.append(
                "Crossover checkpoint has no task plan because bot code is already generated. "
                f"Follow the route policy: next_tool={route.get('next_tool')}, "
                f"intent={route.get('intent')}. {route.get('directive')}"
            )
            return
        lines.append("WARNING: Master plan NOT in checkpoint — call run_master first, then execute_workers.")
        return
    tasks = plan.get("tasks", [])
    if plan.get("strategy") == "crossover" and checkpoint.get("parent2_v") and not tasks:
        lines.append(
            "Crossover plan is saved and no rework tasks are present. "
            f"Follow the route policy: next_tool={route.get('next_tool')}. {route.get('directive')}"
        )
        return
    if tasks:
        lines.append(
            "Master plan is saved — do NOT call run_master again. "
            "Pass these tasks to execute_workers:"
        )
        for t in tasks:
            wid = t.get("worker_id", "?")
            role = t.get("role", "?")
            targets = ", ".join(t.get("target_files", []))
            prompt_preview = t.get("worker_prompt", "")[:200]
            lines.append(
                f"  Worker {wid} ({role}): targets=[{targets}], "
                f"prompt=\"{prompt_preview}...\""
            )
    else:
        lines.append("Master plan is saved — do NOT call run_master again.")


def _load_guardian_insights(max_entries=3):
    """Load recent regression_guardian.jsonl entries for context injection.

    Returns a formatted string block with up to *max_entries* recent guardian
    diagnoses, or empty string if the file doesn't exist or has no entries.
    fix-9: surfaces guardian_diagnosis to Master context (previously "written
    and forgotten" — zero downstream consumers).
    """
    try:
        import evolution_infra as _ei
        _gf = _ei.RESULTS_DIR / "regression_guardian.jsonl"
        if not _gf.exists():
            return ""
        _entries = []
        with open(_gf, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line:
                    try:
                        _entries.append(json.loads(_line))
                    except Exception:
                        pass
        _recent = _entries[-max_entries:] if len(_entries) > max_entries else _entries
        if not _recent:
            return ""
        _blocks = []
        for _e in _recent:
            _ver = _e.get("version", "?")
            _score = _e.get("score", "?")
            _diag = _e.get("diagnosis", "")[:300]
            _rc = _e.get("root_cause", "")
            _rec = _e.get("recovery_recommendation", "")
            _block = f"v{_ver} (score={_score}): {_diag}"
            if _rc:
                _block += f"\n  Root cause: {_rc[:200]}"
            if _rec:
                _block += f"\n  Recommendation: {_rec[:200]}"
            _blocks.append(_block)
        return (
            "\nREGRESSION GUARDIAN INSIGHTS (recent critic score<4 diagnoses):\n"
            + "\n".join(f"  - {b}" for b in _blocks)
            + "\nAvoid these pitfalls in the next generation.\n"
        )
    except Exception:
        return ""


def _format_checkpoint_info(checkpoint, lines):
    """Append pipeline checkpoint details to *lines*.

    Extracts the common formatting shared between the gen_ctx and
    non-gen_ctx code paths in ``_build_context``.
    """
    stage = checkpoint.get("stage", "unknown")
    route = route_policy(checkpoint)
    hint = route.get("directive") or "inspect checkpoint context and continue with the matching MCP pipeline tool"
    lines.append(
        f"\nPIPELINE CHECKPOINT: v{checkpoint['next_v']} (from v{checkpoint['source_v']}) "
        f"reached stage='{stage}'. Next MCP tool: {route.get('next_tool')}. {hint}"
    )
    gen_attempt = checkpoint.get("generation_attempt", 0)
    if gen_attempt > 0:
        lines.append(
            f"INTRA-GEN RETRIES: {gen_attempt} previous critic rejection(s). "
            f"{'MAX RETRIES REACHED — do NOT retry workers again. Abandon this generation.' if gen_attempt >= 2 else 'You may retry workers at most 1 more time.'}"
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
        get_active_bots, load_ratings,
        get_bot_dir, git_has_tag, _load_recent_failures, _git,
        find_current_v, find_max_committed_v, find_abandoned_version_floor,
        compute_next_generation_v,
    )
    from glicko2 import Glicko2Player

    # If GenerationContext is provided, build streamlined context
    if gen_ctx is not None:
        lines = [
            f"Current generation: v{gen_ctx.current_v}",
            f"Next generation: v{gen_ctx.next_v}",
            f"Strategy: {gen_ctx.strategy}",
            f"Source bot: {bot_name(gen_ctx.source_v)}",
            f"Active bots: {len(get_active_bots())}",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if gen_ctx.strategy == "crossover" and gen_ctx.crossover_parents:
            lines.append(f"Crossover parents: {bot_name(gen_ctx.crossover_parents[0])} x {bot_name(gen_ctx.crossover_parents[1])}")

        # Tool reference — prevents ToolSearch when session is fresh/resumed
        lines.append("\nAVAILABLE TOOLS (call by exact name):")
        lines.append("  prepare_next_gen(source_v, next_v) — copy source bot dir")
        lines.append("  run_direction_audit(source_v, next_v) — detect repetitive evolution directions")
        lines.append("  run_master(source_v, next_v, stagnation_info, match_analysis, performance_verification, direction_audit, research_proposals) — plan worker tasks")
        lines.append("  execute_workers(tasks, next_v, source_v, reviewer_feedback) — modify bot code in parallel when target_files do not overlap (max 3), otherwise serial")
        lines.append("  run_quality_gates(version, source_v) — full hard gates: code_changed, declared_scope, compile/runtime import, protected contracts, smoke, national protocol/acceptance, decision, size, fix verification, telemetry fidelity, reachability")
        lines.append("  run_review(version, source_v, plan) — code quality review (boundaries, size, correctness)")
        lines.append("  run_critic(version, source_v, plan, reviewer_feedback, force_advance) — advisory strategic assessment; precommit_eval is the final regression gate")
        lines.append("  run_precommit_eval(version, source_v, n_games) — workflow final regression check; national_primary uses adapter-backed national matches, national_native uses native TCP national matches")
        lines.append("  commit_bot(version, source_v, strategy, review_approved=true) — git commit + tag (requires all gates passed)")
        lines.append("  run_archivist(version, source_v) — archive + cleanup after commit")
        lines.append("  run_crossover(parent_a, parent_b, target_v) — merge two parent bots (alternative to master+workers)")
        lines.append("  run_literature_probe(source_v, next_v, h2h_weakness, stagnation_info) — web-search ONE codable strategy hypothesis for the bot's biggest H2H weakness (governance-gated: auto-skips on cooldown). MANDATORY when stagnation analysis shows is_stagnant:true.")

        if gen_ctx.stagnation_info:
            lines.append(f"\nStagnation analysis:\n{gen_ctx.stagnation_info}")
        if gen_ctx.match_analysis:
            lines.append(f"\nMatch analysis:\n{gen_ctx.match_analysis}")
        if gen_ctx.replay_spotlight:
            lines.append(f"\nReplay spotlight:\n{gen_ctx.replay_spotlight}")
        if gen_ctx.performance_verification:
            lines.append(f"\nPerformance verification:\n{gen_ctx.performance_verification}")

        # Eval round summary (deterministic cross-generation performance data)
        try:
            from eval_rounds import EvalRoundManager
            _erm = EvalRoundManager()
            source_bot_name = bot_name(gen_ctx.source_v)
            eval_summary = _erm.get_last_round_summary(source_bot_name)
            if eval_summary:
                lines.append(f"\n{eval_summary}")
        except Exception:
            pass
        # fix-9: inject regression guardian insights into gen_ctx Master context
        _guardian = _load_guardian_insights(max_entries=3)
        if _guardian:
            lines.append(_guardian)
        if one_gen:
            lines.append("MODE: Run exactly ONE generation, then stop.")
        else:
            lines.append("MODE: Execute this generation using the pipeline tools.")
        # Pipeline checkpoint still relevant for resume
        try:
            from evolution_core import read_pipeline_checkpoint
            checkpoint = read_pipeline_checkpoint()
            if checkpoint:
                _format_checkpoint_info(checkpoint, lines)
        except Exception:
            pass
        return "\n".join(lines)

    active_bots = get_active_bots()
    ratings = load_ratings()
    current_v = find_current_v()
    max_committed_v = find_max_committed_v()
    abandoned_floor = find_abandoned_version_floor()
    next_v = compute_next_generation_v(
        current_v=current_v,
        max_committed_v=max_committed_v,
        abandoned_floor=abandoned_floor,
    )

    lines = [
        f"Current generation: v{current_v}",
        f"Next generation will be: v{next_v}",
        f"Active bots: {len(active_bots)}",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    # Tool reference — prevents ToolSearch in non-gen_ctx path
    lines.append("\nAVAILABLE TOOLS (call by exact name):")
    lines.append("  prepare_next_gen | run_direction_audit | run_literature_probe | run_master | execute_workers | run_quality_gates | run_review | run_critic | run_precommit_eval | commit_bot | run_archivist | run_crossover")

    current_bot_name = bot_name(current_v)

    # Current bot action stats (fold/call/raise frequencies by street)
    bot_action_stats_file = RESULTS_DIR / "bot_action_stats.json"
    if bot_action_stats_file.exists():
        try:
            with locked_file(bot_action_stats_file, "r") as f:
                action_stats = json.load(f)
            bot_stats = action_stats.get(current_bot_name)
            if bot_stats:
                lines.append(f"\nCurrent bot action stats ({current_bot_name}):")
                for street in ("preflop", "flop", "turn", "river"):
                    st = bot_stats.get(street)
                    if st:
                        total = st.get("total", 0)
                        if total > 0:
                            fold_pct = st.get("fold", 0) / total * 100
                            call_pct = st.get("call", 0) / total * 100
                            raise_pct = st.get("raise", 0) / total * 100
                            lines.append(
                                f"  {street}: total={total}, fold={fold_pct:.1f}%, call={call_pct:.1f}%, raise={raise_pct:.1f}%"
                            )
        except Exception:
            pass

    # Current bot rating reliability
    cur_p = ratings.get(current_bot_name)
    if cur_p:
        # Load bot_stats for games-based reliability
        bot_stats_file = RESULTS_DIR / "bot_stats.json"
        games = 0
        wr = 0.0
        if bot_stats_file.exists():
            try:
                with locked_file(bot_stats_file, "r") as f:
                    bs = json.load(f)
                games = bs.get(current_bot_name, {}).get("games", 0)
                wr = bs.get(current_bot_name, {}).get("win_rate", 0.0)
            except Exception:
                pass
        reliable = "RELIABLE" if games >= 100 else f"UNRELIABLE ({games}/100 games — wait for more matches)"
        # Compute H2H avg win rate for the current bot
        try:
            from tool_helpers import load_h2h_avg_winrates, load_strength_scores
            h2h_wrs = load_h2h_avg_winrates()
            strength_scores = load_strength_scores()
            h2h_wr = h2h_wrs.get(current_bot_name, 0.5)
            score = strength_scores.get(current_bot_name, 0.5)
            h2h_str = f"leaderboard_score={score:.4f}, h2h_avg_wr={h2h_wr:.2%}"
        except Exception:
            h2h_str = "leaderboard_score=N/A, h2h_avg_wr=N/A"
        lines.append(f"Current bot {current_bot_name}: {h2h_str}, r={cur_p.r:.1f}, rd={cur_p.rd:.1f}, wr={wr:.0%} ({games} games) [{reliable}]")

    # Incomplete bot detection — previous cycle may have been interrupted
    next_dir = get_bot_dir(next_v)
    if next_dir.exists() and not (next_dir / ".completed").exists():
        lines.append(
            f"WARNING: {bot_name(next_v)} directory exists but is NOT completed "
            f"(previous cycle was interrupted). Decide: resume workers or clean up and restart."
        )

    # Recent completed generations (from git tags)
    try:
        tag_output = _git("tag", "-l", bot_tag_glob(), "--sort=-version:refname", check=False)
        recent_tags = [t.strip() for t in tag_output.splitlines() if t.strip()][:5]
        if recent_tags:
            lines.append(f"Recent completed gens: {', '.join(recent_tags)}")
    except Exception:
        pass

    # Recent worker failures
    try:
        recent_failures = _load_recent_failures(3)
        if recent_failures:
            lines.append("Recent worker failures (last 3):")
            for f in recent_failures:
                lines.append(f"  - Gen {f['gen']} Worker {f['worker_id']} ({f.get('role', 'unknown')}): {f['error'][:120]}")
    except Exception:
        pass

    # Pipeline checkpoint — tell Orchestrator exactly where a killed cycle left off
    try:
        from evolution_core import read_pipeline_checkpoint
        checkpoint = read_pipeline_checkpoint()
        if checkpoint:
            _format_checkpoint_info(checkpoint, lines)
    except Exception:
        pass

    # Environment anomaly detection
    anomalies = []
    if next_dir.exists() and not (next_dir / ".completed").exists():
        anomalies.append("incomplete bot directory")
    try:
        from evolution_core import _load_recent_failures
        if _load_recent_failures(1):
            anomalies.append("recent worker failures")
    except Exception:
        pass
    if anomalies:
        lines.append(
            f"ENVIRONMENT ANOMALIES DETECTED: {', '.join(anomalies)}."
        )

    # fix-9: inject regression guardian insights into non-gen_ctx Master context
    _guardian = _load_guardian_insights(max_entries=3)
    if _guardian:
        lines.append(_guardian)

    if one_gen:
        lines.append("MODE: Run exactly ONE generation, then stop.")
    elif dry_run:
        lines.append("MODE: DRY RUN — only check status, do NOT modify anything.")
    else:
        lines.append("MODE: Continuous evolution. After completing one generation, immediately start the next.")

    # Cycle time budget — helps Orchestrator avoid starting retry loops near timeout
    time_budget = _get_time_budget_info()
    if time_budget:
        lines.append(time_budget)

    return "\n".join(lines)


def _make_precompact_hook():
    """Return hooks dict that injects evolution state before Claude compacts context."""
    async def handler(hook_input, tool_use_id, context) -> SyncHookJSONOutput:
        from evolution_core import read_pipeline_checkpoint, find_current_v
        lines = ["=== EVOLUTION STATE — PRESERVE DURING COMPACTION ==="]
        try:
            current_v = find_current_v()
            lines.append(f"Current completed bot: {bot_name(current_v)}")
            checkpoint = read_pipeline_checkpoint()
            if checkpoint:
                stage = checkpoint.get("stage", "unknown")
                route = route_policy(checkpoint)
                next_step = route.get("next_tool") or "inspect checkpoint context"
                lines.append(
                    f"ACTIVE GENERATION: v{checkpoint['next_v']} (from v{checkpoint['source_v']}), "
                    f"stage={stage}. Next tool: {next_step}. "
                    f"{route.get('directive')} DO NOT restart this generation — continue from this stage."
                )
                _inject_master_plan_hint(checkpoint, lines)
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
      - worker_failures.jsonl: truncate to reset the H6 cross-gen breaker.
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
        "cross_gen_exhausted_history.jsonl",
        "abandoned_versions.jsonl",
    )
    _ACTIONABLE_ROUTE_STAGES = {
        "master_planned",
        "quality_failed",
        "precommit_failed",
        "repair_planned",
        "rework_running",
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
                "Recovery: do NOT retry the denied direct mutation. Inspect the supplied "
                "checkpoint context, then continue using MCP pipeline tools only."
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
        if stage not in _ACTIONABLE_ROUTE_STAGES:
            return None, {}
        route = route_policy(checkpoint) if checkpoint else {}
        next_step = route.get("next_tool")
        if next_step != "execute_workers":
            return None, {}
        next_v = checkpoint.get("next_v")
        source_v = checkpoint.get("source_v")
        parent2_v = checkpoint.get("parent2_v")
        return (
            f"Actionable checkpoint route is locked: v{next_v} from v{source_v}, "
            f"stage={stage}, next MCP tool={next_step}. Built-in Bash/Edit/Write "
            f"are disabled at this stage because they delay or bypass deterministic "
            f"recovery. Call execute_workers with the checkpoint gate feedback now. "
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
